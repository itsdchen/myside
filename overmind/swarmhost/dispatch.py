"""Dispatch pktrade jobs across a pool of Hetzner hosts.

Each host has a per-host concurrency cap (its `cores`). Jobs are pulled
from a single shared queue by per-host worker threads, so a slow host
naturally takes fewer jobs and a fast host takes more without us having
to pre-shard.

A single job is a `Job` (experiment_id + conf filename + date). The
worker SSHes to the host and invokes pktrade with paths constructed
from the experiment's on-remote layout. Outputs land under
`/opt/pktrade/experiments/<exp>/runs/<run-id>/<date>/`.

Failures are recorded with the exact remote cmdline so the user can
reproduce locally (just rewrite paths to the local checkout).
"""

import dataclasses
import queue
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import _ssh


REMOTE_BASE = "/opt/pktrade/experiments"
_ACTIVE_STOPS = set()
_ACTIVE_STOPS_LOCK = threading.Lock()


def request_stop_all() -> None:
    """Ask all in-process dispatch loops to stop submitting more jobs."""
    with _ACTIVE_STOPS_LOCK:
        stops = list(_ACTIVE_STOPS)
    for stop in stops:
        stop.set()


@dataclass(frozen=True)
class Host:
    """A single Hetzner worker."""
    alias: str
    cores: int


@dataclass(frozen=True)
class Job:
    """One (experiment, conf, date) tuple to run on some host."""
    experiment_id: str
    conf: str   # filename within the experiment's conf/ dir, e.g. "xyz_armory.json"
    date: str   # YYYYMMDD


@dataclass
class JobResult:
    """Outcome of one Job's invocation."""
    job: Job
    host: str
    returncode: int
    remote_cmd: str         # exact cmd that ran (or would have run)
    run_id: str             # run directory component used in remote_cmd
    stderr_tail: str = ""   # last lines of stderr on failure, for debugging
    started: float = 0.0
    finished: float = 0.0
    pid: Optional[int] = None
    pgid: Optional[int] = None
    remote_out_dir: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class DispatchResult:
    """Aggregate result for one .dispatch() call."""
    run_id: str
    results: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return [r for r in self.results if not r.ok]

    @property
    def successes(self) -> list:
        return [r for r in self.results if r.ok]


def _paths_for_job(job: Job, run_id: str, binary_name: str) -> dict:
    conf_stem = job.conf.rsplit(".", 1)[0]
    exp_root = f"{REMOTE_BASE}/{job.experiment_id}"
    out_dir = f"{exp_root}/runs/{run_id}/{conf_stem}/{job.date}"
    return {
        "conf_stem": conf_stem,
        "exp_root": exp_root,
        "out_dir": out_dir,
        "bin_path": f"{exp_root}/bin/{binary_name}",
        "conf_path": f"{exp_root}/conf/{job.conf}",
        "acct_file": f"{out_dir}/acct_{job.date}.csv",
        "trd_file": f"{out_dir}/trades_{job.date}.csv",
        "pid_file": f"{out_dir}/job.pid",
        "pgid_file": f"{out_dir}/job.pgid",
        "status_file": f"{out_dir}/job.status",
        "stdout_file": f"{out_dir}/stdout.log",
        "stderr_file": f"{out_dir}/stderr.log",
        "launcher_file": f"{out_dir}/launcher.log",
    }


def _build_cmd(job: Job, run_id: str, binary_name: str) -> str:
    """Construct the exact sim-binary cmdline that runs on remote.

    `binary_name` is the basename of the experiment's binary (typically
    "pktrade" but could be "pkclimber" or any other jammy-built binary —
    the experiment preserves whatever filename was passed in).

    Layout reminder (from the plan):
        /opt/pktrade/experiments/<exp>/bin/<binary_name>
        /opt/pktrade/experiments/<exp>/conf/<conf>
        /opt/pktrade/experiments/<exp>/runs/<run-id>/<date>/

    The launcher cd's into the out-dir before invoking the binary so any
    glog files it writes to CWD land inside the run output dir rather
    than the user's $HOME.

    Argument shape (--date / --conf / --acct / --trd / --out-dir) is
    pktrade's; pkclimber etc. are assumed-compatible. Adjust if a future
    binary needs different flags.
    """
    p = _paths_for_job(job, run_id, binary_name)
    return (
        f"mkdir -p {p['out_dir']} && cd {p['out_dir']} && "
        f"{p['bin_path']} --date {job.date} --conf {p['conf_path']} "
        f"--acct {p['acct_file']} --trd {p['trd_file']} --out-dir {p['out_dir']}"
    )


def _build_detached_submit_cmd(job: Job, run_id: str, binary_name: str) -> str:
    p = _paths_for_job(job, run_id, binary_name)
    pktrade_cmd = (
        f"timeout 3600 {shlex.quote(p['bin_path'])} "
        f"--date {shlex.quote(job.date)} "
        f"--conf {shlex.quote(p['conf_path'])} "
        f"--acct {shlex.quote(p['acct_file'])} "
        f"--trd {shlex.quote(p['trd_file'])} "
        f"--out-dir {shlex.quote(p['out_dir'])}"
    )
    inner = (
        f"cd {shlex.quote(p['out_dir'])} || exit 111; "
        "echo $$ > job.pid; "
        "echo $$ > job.pgid; "
        f"{pktrade_cmd} > {shlex.quote(p['stdout_file'])} 2> {shlex.quote(p['stderr_file'])}; "
        "rc=$?; "
        "echo $rc > job.status; "
        "exit $rc"
    )
    return (
        f"mkdir -p {shlex.quote(p['out_dir'])} && "
        f"rm -f {shlex.quote(p['status_file'])} {shlex.quote(p['pid_file'])} {shlex.quote(p['pgid_file'])} && "
        f"setsid sh -c {shlex.quote(inner)} > {shlex.quote(p['launcher_file'])} 2>&1 < /dev/null & "
        "echo $!"
    )


def _read_remote_job_state(host: str, job: Job, run_id: str, binary_name: str):
    p = _paths_for_job(job, run_id, binary_name)
    cmd = (
        f"if [ -f {shlex.quote(p['status_file'])} ]; then "
        f"  echo STATUS=$(cat {shlex.quote(p['status_file'])}); "
        f"  echo PID=$(cat {shlex.quote(p['pid_file'])} 2>/dev/null || true); "
        f"  echo PGID=$(cat {shlex.quote(p['pgid_file'])} 2>/dev/null || true); "
        f"  echo STDERR_START; tail -c 400 {shlex.quote(p['stderr_file'])} 2>/dev/null || true; echo; echo STDERR_END; "
        "else "
        f"  pid=$(cat {shlex.quote(p['pid_file'])} 2>/dev/null || true); "
        f"  pgid=$(cat {shlex.quote(p['pgid_file'])} 2>/dev/null || true); "
        "  if [ -z \"$pid\" ] && [ -z \"$pgid\" ]; then "
        "    echo STATUS=MISSING; "
        "  elif [ -n \"$pgid\" ] && ! kill -0 -$pgid 2>/dev/null; then "
        "    echo STATUS=-1; "
        "  else "
        "    echo STATUS=RUNNING; "
        "  fi; "
        "  echo PID=$pid; echo PGID=$pgid; "
        "fi"
    )
    res = _ssh.run(host, cmd, timeout=30, check=False)
    status = "RUNNING"
    pid = None
    pgid = None
    stderr_tail = ""
    in_stderr = False
    stderr_lines = []
    for line in res.stdout.splitlines():
        if line == "STDERR_START":
            in_stderr = True
            continue
        if line == "STDERR_END":
            in_stderr = False
            continue
        if in_stderr:
            stderr_lines.append(line)
            continue
        if line.startswith("STATUS="):
            status = line.split("=", 1)[1].strip()
        elif line.startswith("PID="):
            val = line.split("=", 1)[1].strip()
            pid = int(val) if val.isdigit() else None
        elif line.startswith("PGID="):
            val = line.split("=", 1)[1].strip()
            pgid = int(val) if val.isdigit() else None
    stderr_tail = "\n".join(stderr_lines)[-400:]
    return status, pid, pgid, stderr_tail


def _worker(host: Host, job_q: queue.Queue, results: list, results_lock: threading.Lock,
            run_id: str, binary_name: str, stop: threading.Event) -> None:
    """Per-slot worker thread. Each host gets `host.cores` of these."""
    while not stop.is_set():
        try:
            job = job_q.get_nowait()
        except queue.Empty:
            return
        if stop.is_set():
            job_q.task_done()
            return
        cmd = _build_cmd(job, run_id, binary_name)
        submit_cmd = _build_detached_submit_cmd(job, run_id, binary_name)
        paths = _paths_for_job(job, run_id, binary_name)
        started = time.time()
        pid = None
        pgid = None
        stderr_tail = ""
        try:
            res = _ssh.run(host.alias, submit_cmd, timeout=30, check=True)
            launcher_pid = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
            if launcher_pid.isdigit():
                pid = int(launcher_pid)
                pgid = int(launcher_pid)
        except _ssh.SshError as e:
            # The submit command is intentionally detached. If the SSH
            # transport dies after the remote shell launched the process but
            # before stdout gets back to us, the job may still be running.
            # Fall through into polling and let the durable pid/status files
            # tell us whether submission actually landed.
            stderr_tail = e.stderr[-400:] if e.stderr else str(e)[-400:]

        rc = None
        missing_since = time.time()
        while not stop.is_set():
            try:
                status, pid_read, pgid_read, stderr_tail = _read_remote_job_state(
                    host.alias, job, run_id, binary_name)
                pid = pid_read or pid
                pgid = pgid_read or pgid
                if status == "MISSING":
                    if time.time() - missing_since > 30:
                        rc = -1
                        break
                elif status != "RUNNING":
                    rc = int(status) if status.lstrip("-").isdigit() else -1
                    break
            except _ssh.SshError as e:
                # Transport failures can happen during laptop sleep or
                # transient network breaks. The remote process is detached,
                # so keep polling instead of converting that into a job
                # failure.
                stderr_tail = e.stderr[-400:] if e.stderr else str(e)[-400:]
            time.sleep(5)

        if rc is None:
            rc = -2
        finished = time.time()
        jr = JobResult(
            job=job, host=host.alias, returncode=rc, remote_cmd=cmd,
            run_id=run_id, stderr_tail=stderr_tail, started=started, finished=finished,
            pid=pid, pgid=pgid, remote_out_dir=paths["out_dir"],
        )
        with results_lock:
            results.append(jr)
        job_q.task_done()


def dispatch(hosts: list, jobs: list, run_id: str, binary_name: str = "pktrade",
             stop_on_first_failure: bool = False, progress_cb=None,
             progress_interval_s: float = 60.0) -> DispatchResult:
    """Fan `jobs` out across `hosts`, return aggregate result.

    Jobs are pre-assigned to hosts round-robin so even a tiny batch fans out
    (a shared queue would let whichever host's threads got scheduled first
    grab everything). Within a host, the `host.cores` worker threads share
    that host's queue.

    Trade-off: if some jobs take much longer than others, the assigned host
    can't shed work to an idle peer (no work-stealing). For sims of roughly
    equal duration — the common case — this is fine. If skew becomes a
    problem, add a shared "spillover" queue that workers fall back to once
    their host's primary queue is empty.

    Args:
        hosts: list of Host instances. Each contributes `cores` worker slots.
        jobs: list of Job instances. Assigned round-robin to hosts.
        run_id: shared run identifier; appears in output paths.
        binary_name: basename of the experiment's binary (typically "pktrade",
            but "pkclimber" etc. work — the Experiment preserves whatever
            filename was used at create-time).
        stop_on_first_failure: if True, signals all workers to drain after
            the first non-zero exit. Default False — we usually want to know
            about all failures, not just one.
        progress_cb: optional callback called periodically with a dict
            containing completed/running/queued counts. Used by callers to
            heartbeat long-running remote chunks.
        progress_interval_s: minimum seconds between progress_cb calls.

    Returns: DispatchResult with one JobResult per job that ran.
    """
    # One queue per host; jobs round-robin by index.
    per_host_q: dict = {h.alias: queue.Queue() for h in hosts}
    for i, j in enumerate(jobs):
        per_host_q[hosts[i % len(hosts)].alias].put(j)

    results: list = []
    results_lock = threading.Lock()
    stop = threading.Event()
    total_jobs = len(jobs)
    with _ACTIVE_STOPS_LOCK:
        _ACTIVE_STOPS.add(stop)

    def emit_progress(force=False):
        if progress_cb is None:
            return
        with results_lock:
            completed = len(results)
            failed = len([r for r in results if not r.ok])
        queued = sum(q.qsize() for q in per_host_q.values())
        running = max(0, total_jobs - completed - queued)
        try:
            progress_cb({
                "run_id": run_id,
                "total_jobs": total_jobs,
                "completed_jobs": completed,
                "failed_jobs": failed,
                "queued_jobs": queued,
                "running_jobs": running,
                "force": force,
            })
        except Exception:
            # Heartbeat/status callbacks are observability only. Do not let a
            # local status-file problem kill an otherwise healthy remote chunk.
            pass

    try:
        threads = []
        for host in hosts:
            q = per_host_q[host.alias]
            for _ in range(host.cores):
                t = threading.Thread(
                    target=_worker,
                    args=(host, q, results, results_lock, run_id, binary_name, stop),
                    daemon=True,
                )
                t.start()
                threads.append(t)

        emit_progress(force=True)
        last_progress = time.time()
        if stop_on_first_failure:
            # Poll until queue drains or a failure shows up.
            while any(not q.empty() for q in per_host_q.values()):
                time.sleep(0.5)
                now = time.time()
                if now - last_progress >= progress_interval_s:
                    emit_progress()
                    last_progress = now
                with results_lock:
                    if any(not r.ok for r in results):
                        stop.set()
                        break

        for t in threads:
            while t.is_alive():
                t.join(timeout=1.0)
                now = time.time()
                if now - last_progress >= progress_interval_s:
                    emit_progress()
                    last_progress = now
        emit_progress(force=True)
    finally:
        stop.set()
        with _ACTIVE_STOPS_LOCK:
            _ACTIVE_STOPS.discard(stop)

    return DispatchResult(run_id=run_id, results=results)
