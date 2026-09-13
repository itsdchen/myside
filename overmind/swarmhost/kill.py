#!/usr/bin/env python3
"""Kill all pktrade processes on the pool that belong to a given experiment.

Used when:
  - You Ctrl+C the local SimVariations / run.py — the dispatcher dies but
    the in-flight pktrade processes on remote keep running.
  - A sweep is wedged and you want to free the cores without rebooting.

How it works: each pktrade process is invoked as
    /opt/pktrade/experiments/<experiment_id>/bin/<binary> ...
so we `pkill -f /opt/pktrade/experiments/<experiment_id>/` on every host in
the pool. Experiment IDs include a date + hostname + binhash so the match
is unique to your experiment — won't touch anyone else's processes.

Example usage:
    # Dry-run: see what would be killed.
    ./kill.py 20260603_GoingMerry_simvar-sv-stress_fd82fec8 --dry-run

    # For real.
    ./kill.py 20260603_GoingMerry_simvar-sv-stress_fd82fec8

    # Custom inventory.
    ./kill.py <exp_id> --hosts /path/to/hosts.yaml

Args:
    experiment_id: the id printed by Experiment.create() (e.g. shown in
        SimVariations logs as "experiment: ..."). Also visible at
        ~/scratch/remote-sims/<experiment_id>/.
    --hosts: optional path to a hosts.yaml. Defaults to
        overmind/swarmhost/hosts.yaml relative to this script.
    --dry-run: list matching processes per host without killing.
"""

import argparse
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from overmind.swarmhost.experiment import HostPool, kill_experiment  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment_id",
                    help="Experiment id to kill (e.g. 20260603_GoingMerry_my-sweep_abc12345).")
    ap.add_argument("--hosts", type=Path,
                    default=_THIS_DIR / "hosts.yaml",
                    help="Path to hosts.yaml inventory.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List matching processes per host without killing.")
    args = ap.parse_args()

    pool = HostPool.from_yaml(args.hosts)
    try:
        total_matched = kill_experiment(args.experiment_id, pool,
                                        dry_run=args.dry_run, verbose=True)
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(2)

    print()
    if args.dry_run:
        print(f"DRY RUN — would kill {total_matched} process(es) across the pool.")
    else:
        print(f"sent TERM+KILL to {total_matched} process(es) across the pool.")


if __name__ == "__main__":
    main()
