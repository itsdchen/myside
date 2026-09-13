"""Experiment: a binary + a bundle of conffiles, pushed to a HostPool.

Identity: <YYYYMMDD>_<name>_<binhash8>
    The caller supplies `name`; we compute date and binary hash.

Lifecycle:
    Experiment.create(name, binary_path, confs) → instance
    instance.push(pool)                          → rsync to all hosts (idempotent)
    instance.run(pool, jobs, run_id=None)        → dispatch, fetch back, return RunResult

The Experiment object is local-side. The hosts hold copies of its data under
/opt/pktrade/experiments/<id>/. Multiple experiments coexist; the per-host
cleanup cron removes ones with no activity in 30+ days.

═════════════════════════════════════════════════════════════════════════
Reproducibility note (2026-06-03)
─────────────────────────────────────────────────────────────────────────
Sims are bit-identical Hetzner-↔-Hetzner across all boxes in the pool —
the swarmhost is internally consistent and that's the production baseline.

Sims may NOT be bit-identical Hetzner-vs-local-dev when the local glibc
differs from Hetzner's (Jammy = glibc 2.35). In an 11-day smoke test the
3 divergent dates were all Mondays (session-boundary days); Tue–Fri were
bit-perfect. Two suspected causes (per dchen):
  1. Random order sizing — different sequence → different fills propagate
  2. std::priority_queue tie-breaking when many events share a timestamp
     (common at Sunday-evening CME re-open into Monday)
Magnitudes seen ranged from $0.11 to a sign-flip on Memorial Day.

For day-to-day work: treat Hetzner output as canonical. Don't trust
exact-cent comparisons between local and remote. If reproducibility ever
becomes critical, options are: run local in a Jammy container, or pin
order-sizing RNG to a deterministic seed.
═════════════════════════════════════════════════════════════════════════
"""

import datetime
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import _ssh
from .dispatch import Host, Job, JobResult, dispatch


REMOTE_BASE = "/opt/pktrade/experiments"
LOCAL_REGISTRY = Path.home() / ".cache" / "pktrade-remote" / "experiments"
LOCAL_RESULTS = Path.home() / "scratch" / "remote-sims"


@dataclass
class HostPool:
    """A collection of Hosts to dispatch across. Load from hosts.yaml."""
    hosts: list

    @classmethod
    def from_yaml(cls, path: Path) -> "HostPool":
        """Load a HostPool from an `overmind/swarmhost/hosts.yaml`-style file."""
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        entries = data.get("hosts") or []
        if not entries:
            raise ValueError(f"No hosts listed in {path}")
        hosts = [Host(alias=h["alias"], cores=int(h["cores"])) for h in entries]
        return cls(hosts=hosts)

    def total_cores(self) -> int:
        return sum(h.cores for h in self.hosts)

    def reachable(self, timeout: float = 5.0) -> "HostPool":
        """Return a new HostPool containing only hosts that respond to a quick SSH ping.

        Each host gets a 2-second `ssh <alias> true` (parallel across hosts).
        Hosts that fail to respond are dropped. Useful as a pre-flight check
        before dispatch so a stale `hosts.yaml` entry doesn't kill the sweep
        with timeouts mid-run.

        Returns a fresh HostPool — does not mutate self. Logs/prints which
        hosts were dropped.
        """
        from concurrent.futures import ThreadPoolExecutor

        def ping(h):
            try:
                _ssh.run(h.alias, "true", timeout=timeout, check=True)
                return (h, True, None)
            except _ssh.SshError as e:
                return (h, False, e.stderr.strip() or f"rc={e.returncode}")

        with ThreadPoolExecutor(max_workers=max(1, len(self.hosts))) as ex:
            results = list(ex.map(ping, self.hosts))

        live = []
        for h, ok, err in results:
            if ok:
                live.append(h)
            else:
                print(f"[swarmhost] dropping unreachable host {h.alias}: {err}")
        return HostPool(hosts=live)


@dataclass
class RunResult:
    """Outcome of one Experiment.run() invocation."""
    experiment_id: str
    run_id: str
    job_results: list = field(default_factory=list)
    fetched_to: Optional[Path] = None  # local dir holding fetched outputs

    @property
    def failures(self) -> list:
        return [r for r in self.job_results if not r.ok]

    @property
    def successes(self) -> list:
        return [r for r in self.job_results if r.ok]


def _local_codename() -> str:
    """Return the local distro codename (e.g. 'jammy', 'noble'), '' on failure."""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def select_remote_binary(repo_root: Optional[Path] = None) -> Path:
    """Pick which local pktrade binary to ship to the (jammy) swarmhost pool.

    Rule: we always need a binary that runs on Jammy (the hetz pool's distro).
      - Local IS jammy → use `bin/pktrade` (native local build, no docker).
      - Local is anything else (e.g. noble/24.04) → use `jammy.bin/pktrade`
        (cross-built via ./jammybuild.sh inside a jammy container).

    Falls back to whichever binary actually exists if the natural choice
    isn't built yet.
    """
    if repo_root is None:
        # experiment.py lives at <repo>/overmind/swarmhost/experiment.py
        repo_root = Path(__file__).resolve().parents[2]
    native = repo_root / "bin" / "pktrade"
    jammy = repo_root / "jammy.bin" / "pktrade"
    if _local_codename() == "jammy":
        if native.exists():
            return native
        return jammy
    # Not jammy locally — `bin/pktrade` would have wrong glibc for hetz.
    return jammy


def select_local_pktrade(repo_root: Optional[Path] = None) -> Path:
    """Pick the local pktrade to use for in-process tasks like `pktrade --dry-run`.

    Always prefer the native local build — it matches the local glibc and
    runs without going through docker.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]
    native = repo_root / "bin" / "pktrade"
    jammy = repo_root / "jammy.bin" / "pktrade"
    return native if native.exists() else jammy


def kill_experiment(experiment_id: str, pool: "HostPool", dry_run: bool = False,
                    verbose: bool = False) -> int:
    """Terminate all pktrade processes for `experiment_id` on every host in `pool`.

    Sends TERM, waits 2s, sends KILL to stragglers. Match is scoped to
    `/opt/pktrade/experiments/<experiment_id>/` so it can't hit other
    experiments / other users.

    Args:
        experiment_id: full id (e.g. 20260603_GoingMerry_my-sweep_a1b2c3d4).
        pool: HostPool to act on.
        dry_run: if True, list matches without killing.
        verbose: if True, print per-host activity.

    Returns the number of processes that were targeted across the pool
    (best-effort; counted before TERM).
    """
    if "/" in experiment_id or " " in experiment_id or len(experiment_id) < 8:
        raise ValueError(f"refusing suspicious experiment_id: {experiment_id!r}")
    pattern = f"/opt/pktrade/experiments/{experiment_id}/"

    def kill_one_host(host):
        verbose_lines = []
        list_cmd = (
            f"echo PROCS_START; ps -eo pid,pgid,cmd --no-headers | "
            f"grep -F {pattern!r} | grep -v ' grep '; echo PROCS_END"
        )
        try:
            res = _ssh.run(host.alias, list_cmd, timeout=10, check=False)
        except _ssh.SshError:
            return 0, [f"[{host.alias}] unreachable; skipping"]
        proc_lines = []
        section = None
        for line in res.stdout.splitlines():
            if line == "PROCS_START":
                section = line
                continue
            if line == "PROCS_END":
                section = None
                continue
            if not line.strip():
                continue
            if section == "PROCS_START":
                proc_lines.append(line)

        pgids = []
        for line in proc_lines:
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[1].isdigit():
                pgids.append(parts[1])
        pgids = sorted(set(pgids), key=int)

        matched = len(proc_lines)
        if matched == 0:
            return 0, [f"[{host.alias}] no matching processes"]
        if verbose:
            if pgids:
                verbose_lines.append(
                    f"[{host.alias}] {len(pgids)} live matching process group(s):")
                for pgid in pgids:
                    verbose_lines.append(f"  -{pgid}")
            verbose_lines.append(f"[{host.alias}] {len(proc_lines)} matching process(es):")
            for l in proc_lines:
                verbose_lines.append(f"  {l}")
        if dry_run:
            return matched, verbose_lines
        kill_groups = ""
        if pgids:
            neg_pgids = " ".join(f"-{pgid}" for pgid in pgids)
            kill_groups = (
                f"kill -TERM {neg_pgids} 2>/dev/null; sleep 1; "
                f"kill -KILL {neg_pgids} 2>/dev/null; "
            )
        kill_cmd = (kill_groups +
                    f"pkill -TERM -f {pattern!r}; sleep 1; "
                    f"pkill -KILL -f {pattern!r} 2>/dev/null; true")
        try:
            _ssh.run(host.alias, kill_cmd, timeout=12, check=False)
        except _ssh.SshError as e:
            if verbose:
                verbose_lines.append(f"[{host.alias}] kill command failed: {e.stderr.strip()}")
        return matched, verbose_lines

    from concurrent.futures import ThreadPoolExecutor

    total = 0
    with ThreadPoolExecutor(max_workers=max(1, len(pool.hosts))) as ex:
        for matched, verbose_lines in ex.map(kill_one_host, pool.hosts):
            total += matched
            if verbose:
                for line in verbose_lines:
                    print(line)
    return total


def check_binary_freshness(binary_path: Path, source_root: Optional[Path] = None) -> Optional[str]:
    """If any source file is newer than `binary_path`, return a warning string.

    Catches the "I edited pktrade but forgot to ./jammybuild.sh" footgun
    that's otherwise invisible until you wonder why a sim doesn't reflect
    your change.

    Args:
        binary_path: e.g. jammy.bin/pktrade.
        source_root: dir to scan for .cc/.cpp/.h/.hpp. Defaults to the repo's
            `src/` (computed from the binary path: same parent as bin dir).

    Returns: a one-line warning string if stale, or None if up-to-date /
        couldn't check. Caller logs/prints it.
    """
    binary_path = Path(binary_path)
    if not binary_path.exists():
        return None
    if source_root is None:
        # binary is at <repo>/jammy.bin/pktrade; src is at <repo>/src/
        source_root = binary_path.parent.parent / "src"
    if not source_root.exists():
        return None
    bin_mtime = binary_path.stat().st_mtime
    suffixes = (".cc", ".cpp", ".h", ".hpp")
    newest_src = 0.0
    newest_path = None
    for root, _dirs, files in os.walk(source_root):
        for fname in files:
            if not fname.endswith(suffixes):
                continue
            p = os.path.join(root, fname)
            try:
                m = os.stat(p).st_mtime
            except OSError:
                continue
            if m > newest_src:
                newest_src = m
                newest_path = p
    if newest_src > bin_mtime:
        delta_h = (newest_src - bin_mtime) / 3600.0
        return (f"WARNING: {binary_path.name} is older than source "
                f"{newest_path} (by {delta_h:.1f}h). "
                f"Did you forget `./jammybuild.sh`?")
    return None


class Experiment:
    """A binary + conf bundle, identified by `<date>_<name>_<binhash>`.

    Use Experiment.create(...) — don't call __init__ directly.
    """

    def __init__(self, experiment_id: str, name: str, binary_path: Path,
                 confs: list, binhash: str):
        self.experiment_id = experiment_id
        self.name = name
        self.binary_path = Path(binary_path)
        self.confs = [Path(c) for c in confs]
        self.binhash = binhash
        # Preserve the local binary's basename — used as install name on
        # remote ("bin/<binary_name>") and in the dispatch cmdline. Lets
        # pkclimber / other jammy-built binaries flow through unchanged.
        self.binary_name = self.binary_path.name

    @classmethod
    def create(cls, name: str, binary_path: Path, confs: list) -> "Experiment":
        """Make a new Experiment.

        Args:
            name: short label — caller's responsibility to make unique enough
                that two simultaneous experiments don't collide. Example:
                "armory-latency-sweep" or "pnlclimb-v3".
            binary_path: path to a local jammy-built binary, typically
                `jammy.bin/pktrade` but `jammy.bin/pkclimber` etc. also work.
                Must exist; we hash it (and preserve its basename) to
                disambiguate experiments and to invoke the right binary on remote.
            confs: list of conffile paths to bundle. Can be 1 or thousands.

        Returns: Experiment instance, also persisted to LOCAL_REGISTRY so it
            can be looked up by experiment_id later.
        """
        binary_path = Path(binary_path)
        if not binary_path.exists():
            raise FileNotFoundError(f"binary not found: {binary_path}")
        confs = [Path(c) for c in confs]
        for c in confs:
            if not c.exists():
                raise FileNotFoundError(f"conf not found: {c}")

        binhash = _sha256_file(binary_path)[:8]
        date_str = datetime.date.today().strftime("%Y%m%d")
        slug = _slug(name)
        # Include local hostname so two users on different machines invoking
        # Experiment.create with the same `name` + binary on the same day
        # get distinct experiment dirs on remote (no silent conf bundle merge).
        import socket
        origin = _slug(socket.gethostname().split(".")[0])
        experiment_id = f"{date_str}_{origin}_{slug}_{binhash}"

        exp = cls(
            experiment_id=experiment_id, name=name,
            binary_path=binary_path, confs=confs, binhash=binhash,
        )
        exp._save_manifest()
        return exp

    def push(self, pool: HostPool) -> None:
        """Rsync binary and conf bundle to each host in `pool`.

        Idempotent — rsync skips files whose checksum (well, size+mtime by
        default) matches. After the first call to each host, subsequent
        pushes only transfer diffs.

        Pushes happen sequentially per-host. For 2-3 hosts this is fine;
        for larger pools we'd parallelize, but per-host bandwidth is the
        limit anyway.
        """
        with tempfile.TemporaryDirectory() as staging:
            staging = Path(staging)
            # Stage layout mirrors the remote layout so rsync can push the
            # whole tree in one shot.
            stage_root = staging / self.experiment_id
            (stage_root / "bin").mkdir(parents=True)
            (stage_root / "conf").mkdir()
            shutil.copy2(self.binary_path, stage_root / "bin" / self.binary_name)
            for c in self.confs:
                shutil.copy2(c, stage_root / "conf" / c.name)
            # Write a tiny manifest into the experiment dir on remote.
            with open(stage_root / "manifest.json", "w") as f:
                json.dump(self._manifest_dict(), f, indent=2)

            for host in pool.hosts:
                # Trailing slash on source = "copy contents into dest dir".
                _ssh.rsync_push(host.alias, stage_root,
                                f"{REMOTE_BASE}/")

    def run(self, pool: HostPool, jobs: list, run_id: Optional[str] = None,
            fetch: bool = True, cleanup_after_fetch: bool = False,
            progress_cb=None, progress_interval_s: float = 60.0) -> RunResult:
        """Dispatch `jobs` across the pool, optionally fetch results back.

        Args:
            pool: HostPool to use. Workers per host = host.cores.
            jobs: iterable of (conf_filename, date) tuples. Each tuple
                becomes one pktrade invocation on some host.
            run_id: identifier for this batch of jobs. Defaults to a UTC
                timestamp. Outputs land under runs/<run_id>/.
            fetch: if True (default), pull output CSVs back to LOCAL_RESULTS
                after dispatch completes.
            cleanup_after_fetch: if True, delete the remote runs/<run_id>/
                dir on each host after a successful fetch. Useful for big
                sweeps where you don't want output to linger 30 days. No-op
                when fetch=False.
            progress_cb: optional callback passed through to dispatch().
            progress_interval_s: minimum seconds between progress callbacks.

        Returns: RunResult with per-job outcomes and (if fetch=True) the
            local directory containing fetched outputs.
        """
        if run_id is None:
            # Append local hostname so two users on different machines don't
            # collide on default run_ids (timestamp is 1-second resolution).
            import socket
            host = socket.gethostname().split(".")[0]
            run_id = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + f"_{host}"

        # Sanity-check conffile names — they need to exist in self.confs.
        known_confs = {c.name for c in self.confs}
        job_objs = []
        for conf_name, date in jobs:
            if conf_name not in known_confs:
                raise ValueError(
                    f"conf {conf_name!r} not part of experiment "
                    f"{self.experiment_id!r}. Known confs: {sorted(known_confs)[:5]}..."
                )
            job_objs.append(Job(experiment_id=self.experiment_id,
                                conf=conf_name, date=date))

        dr = dispatch(pool.hosts, job_objs, run_id=run_id,
                      binary_name=self.binary_name,
                      progress_cb=progress_cb,
                      progress_interval_s=progress_interval_s)

        fetched_to = None
        if fetch:
            fetched_to = self._fetch_run(pool, run_id)
            if cleanup_after_fetch and not dr.failures:
                self._cleanup_remote_run(pool, run_id)

        rr = RunResult(
            experiment_id=self.experiment_id, run_id=run_id,
            job_results=dr.results, fetched_to=fetched_to,
        )
        self._save_run_manifest(rr)
        return rr

    # ── internals ────────────────────────────────────────────────────────

    def _fetch_run(self, pool: HostPool, run_id: str) -> Path:
        """Rsync the runs/<run-id>/ subdir back from every host.

        Output dirs from each host merge into one local dir — pktrade per-date
        outputs are already namespaced by date, so two hosts working on
        different dates don't collide.
        """
        local_dir = LOCAL_RESULTS / self.experiment_id / run_id
        local_dir.mkdir(parents=True, exist_ok=True)
        remote = f"{REMOTE_BASE}/{self.experiment_id}/runs/{run_id}/"
        for host in pool.hosts:
            try:
                _ssh.rsync_pull(host.alias, remote, local_dir)
            except _ssh.SshError as e:
                # If a host didn't produce output (e.g. all its jobs failed),
                # the remote dir won't exist and rsync exits 23. Log and continue.
                if "No such file" in (e.stderr or ""):
                    continue
                raise
        return local_dir

    def _cleanup_remote_run(self, pool: HostPool, run_id: str) -> None:
        """Delete the runs/<run-id>/ dir on each host. Safe to call repeatedly."""
        remote = f"{REMOTE_BASE}/{self.experiment_id}/runs/{run_id}"
        for host in pool.hosts:
            _ssh.run(host.alias, f"rm -rf {remote}", check=False)

    def _manifest_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "binhash": self.binhash,
            "binary": str(self.binary_path),
            "confs": [str(c) for c in self.confs],
            "created": datetime.datetime.utcnow().isoformat() + "Z",
            # git SHA is best-effort — if we're not in a repo or git isn't
            # installed, we just omit it.
            "git_sha": _git_sha(),
        }

    def _save_manifest(self) -> None:
        LOCAL_REGISTRY.mkdir(parents=True, exist_ok=True)
        path = LOCAL_REGISTRY / f"{self.experiment_id}.json"
        with open(path, "w") as f:
            json.dump(self._manifest_dict(), f, indent=2)

    def _save_run_manifest(self, rr: RunResult) -> None:
        """Per-run manifest with reproducer cmdlines for failed jobs."""
        out_dir = LOCAL_RESULTS / self.experiment_id / rr.run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "experiment_id": rr.experiment_id,
            "run_id": rr.run_id,
            "total_jobs": len(rr.job_results),
            "succeeded": len(rr.successes),
            "failed": len(rr.failures),
            "failures": [
                {
                    "host": jr.host,
                    "conf": jr.job.conf,
                    "date": jr.job.date,
                    "pid": jr.pid,
                    "pgid": jr.pgid,
                    "remote_out_dir": jr.remote_out_dir,
                    "returncode": jr.returncode,
                    "remote_cmd": jr.remote_cmd,
                    "local_reproducer": _to_local_cmd(jr, self),
                    "stderr_tail": jr.stderr_tail,
                }
                for jr in rr.failures
            ],
        }
        with open(out_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)


# ── module-level helpers ─────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    """Hash a file's contents — used to disambiguate experiments by binary."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug(name: str) -> str:
    """Sanitize an experiment name for use in a directory name."""
    return "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in name)


def _git_sha() -> Optional[str]:
    """Return short git SHA of CWD's repo, or None if unavailable."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=2.0,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _to_local_cmd(jr: JobResult, exp: "Experiment") -> str:
    """Rewrite a remote pktrade cmdline so the user can run it locally.

    Swaps the remote experiment paths back to the local source paths
    captured at experiment-creation time. The output dir is left as a
    /tmp path so it doesn't stomp on anything the user has locally.
    """
    remote_exp = f"{REMOTE_BASE}/{exp.experiment_id}"
    conf_stem = jr.job.conf.rsplit(".", 1)[0]
    remote_out = f"{remote_exp}/runs/{jr.run_id}/{conf_stem}/{jr.job.date}"
    local_out = f"/tmp/pktrade-repro/{exp.experiment_id}/{jr.job.date}"
    local_conf = None
    for c in exp.confs:
        if c.name == jr.job.conf:
            local_conf = str(c)
            break
    cmd = jr.remote_cmd
    cmd = cmd.replace(f"{remote_exp}/bin/{exp.binary_name}", str(exp.binary_path))
    if local_conf:
        cmd = cmd.replace(f"{remote_exp}/conf/{jr.job.conf}", local_conf)
    cmd = cmd.replace(remote_out, local_out)
    return cmd
