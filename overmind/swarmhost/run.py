#!/usr/bin/env python3
"""CLI wrapper around overmind.swarmhost.Experiment for one-off runs.

For batch tooling (SimVariations, PnlClimber), call the Python API
directly — this CLI is for ad-hoc invocations.

Example usage:
    # Single conf, date range, over the boxes in hosts.yaml.
    ./run.py \\
        --name armory-feb \\
        --binary /home/david/tradefi/retraded_sched/jammy.bin/pktrade \\
        --conf overmind/modelbuild_confs/xyz_armory.json \\
        --dates 20260101:20260131 \\
        --hosts overmind/swarmhost/hosts.yaml

    # Single conf, single date, also pushes to remote and cleans up after fetch.
    ./run.py --name smoke --binary jammy.bin/pktrade \\
        --conf overmind/modelbuild_confs/xyz_armory.json \\
        --dates 20260101 \\
        --hosts overmind/swarmhost/hosts.yaml \\
        --cleanup-after-fetch

Args:
    --name: short experiment label. Caller's responsibility to make unique
        enough that two simultaneous experiments don't collide.
    --binary: path to a local jammy.bin/pktrade.
    --conf: path to a single conffile (CLI is single-conf — use the Python
        API for many-conf bundles).
    --dates: either YYYYMMDD or YYYYMMDD:YYYYMMDD (inclusive range, weekdays).
    --hosts: path to a hosts.yaml inventory file.
    --run-id: optional explicit run-id. Defaults to a UTC timestamp.
    --cleanup-after-fetch: delete the remote run dir after pulling outputs.
    --no-fetch: skip the result-pull step (leave outputs on remote).
"""

import argparse
import sys
from pathlib import Path

# Add the repo root to sys.path so we can import overmind.* when run as a
# script. Works both for direct execution and `python -m`.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from overmind.swarmhost.experiment import Experiment, HostPool, check_binary_freshness, kill_experiment  # noqa: E402


def _parse_dates(spec: str) -> list:
    """Accept YYYYMMDD or YYYYMMDD:YYYYMMDD. Returns list of weekday strings.

    Mirrors the existing repo convention (util.chron.dates_list) by emitting
    weekdays only — we don't sim weekends.
    """
    from datetime import date, timedelta
    if ":" in spec:
        a, b = spec.split(":")
        start = date(int(a[:4]), int(a[4:6]), int(a[6:8]))
        end = date(int(b[:4]), int(b[4:6]), int(b[6:8]))
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:  # 0–4 = Mon–Fri
                out.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        return out
    return [spec]


def main():
    ap = argparse.ArgumentParser(
        description="Run pktrade sims on remote Hetzner boxes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--name", required=True, help="Short experiment label.")
    ap.add_argument("--binary", required=True, type=Path,
                    help="Path to local jammy.bin/pktrade.")
    ap.add_argument("--conf", required=True, type=Path,
                    help="Path to a single conffile.")
    ap.add_argument("--dates", required=True,
                    help="YYYYMMDD or YYYYMMDD:YYYYMMDD (inclusive, weekdays only).")
    ap.add_argument("--hosts", required=True, type=Path,
                    help="Path to a hosts.yaml inventory.")
    ap.add_argument("--run-id", default=None,
                    help="Optional explicit run-id (default: UTC timestamp).")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip the result-pull step (leave outputs on remote).")
    ap.add_argument("--cleanup-after-fetch", action="store_true",
                    help="Delete the remote runs/<run-id>/ dir after a successful fetch.")
    ap.add_argument("--no-stage-mktdata", action="store_true",
                    help="Skip auto-staging mktdata for the conf via mktdata.stage_for_conf. "
                         "Default ON — uses pktrade --dry-run on the conf to discover what "
                         "files are needed, then rsyncs them per host.")
    args = ap.parse_args()

    # Stale-binary check: warn (don't block) if any source is newer than the binary.
    stale = check_binary_freshness(args.binary)
    if stale:
        print(stale, file=sys.stderr)

    pool = HostPool.from_yaml(args.hosts)
    # Pre-flight reachability — fail fast if no host is up.
    pool = pool.reachable()
    if not pool.hosts:
        print("no reachable hosts in pool", file=sys.stderr)
        sys.exit(1)
    dates = _parse_dates(args.dates)

    print(f"Creating experiment: name={args.name!r} binary={args.binary} conf={args.conf.name}")
    exp = Experiment.create(name=args.name, binary_path=args.binary, confs=[args.conf])
    print(f"  experiment_id = {exp.experiment_id}")

    # Auto-stage mktdata (default ON). Uses pktrade --dry-run to figure out
    # which gzpbf files the conf needs; rsyncs them per host in parallel.
    if not args.no_stage_mktdata:
        from overmind.swarmhost.mktdata import stage_for_conf
        from concurrent.futures import ThreadPoolExecutor
        print(f"Staging mktdata on {len(pool.hosts)} host(s) for {len(dates)} date(s)...")
        with ThreadPoolExecutor(max_workers=len(pool.hosts)) as ex:
            futs = {ex.submit(stage_for_conf, h.alias, args.conf, dates, args.binary): h.alias
                    for h in pool.hosts}
            for fut, alias in [(f, futs[f]) for f in futs]:
                try:
                    n = fut.result()
                    print(f"  {alias}: {n} files (rsync skips already-synced)")
                except Exception as e:
                    print(f"  {alias}: stage FAILED: {e}", file=sys.stderr)

    print(f"Pushing to {len(pool.hosts)} host(s)...")
    exp.push(pool)

    # On Ctrl+C during dispatch, kill remote procs for this experiment so
    # they don't keep burning cores after the local dispatcher dies.
    import signal as _signal
    def _sigint_handler(signum, frame):
        print(f"\nSIGINT — killing remote pktrade procs for {exp.experiment_id}", file=sys.stderr)
        try:
            kill_experiment(exp.experiment_id, pool, verbose=False)
        except Exception as e:
            print(f"kill on Ctrl+C failed: {e}", file=sys.stderr)
        raise KeyboardInterrupt()
    _signal.signal(_signal.SIGINT, _sigint_handler)

    jobs = [(args.conf.name, d) for d in dates]
    print(f"Dispatching {len(jobs)} job(s) across {pool.total_cores()} core(s).")
    result = exp.run(pool, jobs, run_id=args.run_id,
                     fetch=not args.no_fetch,
                     cleanup_after_fetch=args.cleanup_after_fetch)

    print()
    print(f"Done. run_id = {result.run_id}")
    print(f"  succeeded: {len(result.successes)} / {len(result.job_results)}")
    if result.failures:
        print(f"  failed:    {len(result.failures)}")
        for jr in result.failures[:5]:
            print(f"    {jr.host} conf={jr.job.conf} date={jr.job.date} rc={jr.returncode}")
        if len(result.failures) > 5:
            print(f"    ... +{len(result.failures) - 5} more (see manifest.json)")
    if result.fetched_to:
        print(f"  fetched to: {result.fetched_to}")


if __name__ == "__main__":
    main()
