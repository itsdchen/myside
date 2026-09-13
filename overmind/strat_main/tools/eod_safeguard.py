#!/usr/bin/env python3
r"""EOD safeguard: verify the trade-machine processes restarted cleanly at 18:00.

Intended to run from cron a minute or two after the 18:00 restart boundary, e.g.

    1 18 * * * /home/ubuntu/.venvs/v1/bin/python /home/ubuntu/scripts/eod_safeguard.py \
        --wsgateway 1 --pymultifeed 1 --pkmultifeed 1

It checks the following and, if any fail, reports them (see the exit behavior
below):

  (1) Exactly the expected number of each of wsgateway.py, pymultifeed.py and
      pkmultifeed* are running (counts passed as args, since machines differ).
  (2) Every one of those processes -- plus every pktrade* process, if any --
      was (re)started at the 18:00 boundary, i.e. is younger than a cutoff that
      adapts to how long after 18:00 the check runs (so moving the cron time
      doesn't require touching the script). An older process means it never
      restarted (the multi-day hang we're guarding against).
  (3) Every pktrade* process carries a -i/--strat-id, and no two share the same
      one (a duplicate strat id means a stale instance didn't die at EOD).

On success it is silent (exit 0) so cron doesn't email daily; on failure it
prints a summary (which cron emails via MAILTO), optionally sends a ntfy alert
if --ntfy is given, and exits 1.
"""

import argparse
import os
import socket
import sys
import time
from datetime import datetime, timedelta

import psutil

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils

# The daily restart boundary the processes are launched at. Fixed at 18:00; the
# cron time for THIS check may differ, which is why the cutoff is derived from
# "now minus this boundary" rather than being a flat age.
RESTART_HOUR = 18
RESTART_MINUTE = 0

# Python scripts matched by a <prefix>*.py argv basename run under a python
# interpreter (so a renamed pymultifeed_ansem.py still counts as pymultifeed);
# C++ binaries matched by process-name prefix.
PY_PREFIXES = ("wsgateway", "pymultifeed")
BIN_PREFIXES = ("pkmultifeed", "pktrade")

# pktrade processes carry a unique strategy id, passed as -i/--strat-id <id>.
# gflags treats - and _ interchangeably in flag names, so accept both long forms.
STRAT_ID_FLAGS = ("-i", "--strat-id", "--strat_id")


def _is_python(basename):
    """True if an executable basename looks like a Python interpreter (python3)."""
    return basename.startswith("python")


def scan_processes():
    """One pass over all processes, bucketing matches by label.

    Returns a dict: label -> list of dicts {pid, create_time}. Labels are the
    prefixes above.

    Matching rules (see module docstring for why):
      - .py scripts: the process is a python interpreter running a <prefix>*.py.
        We gate on cmdline[0] (the interpreter), not comm, because a script run
        directly via an absolute-path shebang (e.g. ./wsgateway.py) reports comm
        as the *script name* while cmdline[0] is still the interpreter; comm is
        also folded into the candidates for that case. This still excludes the
        `sh -c "... python feed/foo.py ..."` cron wrappers (cmdline[0]=sh) and
        editors/pagers that merely have the file open (cmdline[0]=emacs/less).
      - name prefixes: comm starts with the prefix. The sh/bash launch wrappers
        have comm sh/bash, so they're excluded automatically.
    """
    buckets = {label: [] for label in PY_PREFIXES + BIN_PREFIXES}
    mypid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            pid = info["pid"]
            if pid == mypid:
                continue
            name = info["name"] or ""
            cmdline = info["cmdline"] or []
            entry = {"pid": pid, "create_time": info["create_time"],
                     "cmdline": cmdline}

            if cmdline and _is_python(os.path.basename(cmdline[0])):
                bases = {name} | {os.path.basename(tok) for tok in cmdline[1:]}
                matched = False
                for prefix in PY_PREFIXES:
                    if any(b.startswith(prefix) and b.endswith(".py")
                           for b in bases):
                        buckets[prefix].append(entry)
                        matched = True
                        break
                if matched:
                    continue

            for prefix in BIN_PREFIXES:
                if name.startswith(prefix):
                    buckets[prefix].append(entry)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return buckets


def restart_boundary_epoch(now):
    """Epoch seconds of the most recent RESTART_HOUR:RESTART_MINUTE boundary.

    If the check somehow runs before today's boundary, anchor to yesterday's so
    the age check degrades safely (large cutoff, no false alarms) instead of
    going negative.
    """
    anchor = now.replace(hour=RESTART_HOUR, minute=RESTART_MINUTE,
                         second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor.timestamp()


def extract_strat_id(cmdline):
    """Return the -i/--strat-id value from a pktrade argv, or None if absent.

    Handles the space-separated form (`-i foo`) and the `=` form
    (`--strat-id=foo`). A flag present with no following value counts as absent.
    """
    for idx, tok in enumerate(cmdline):
        for flag in STRAT_ID_FLAGS:
            if tok == flag:
                if idx + 1 < len(cmdline) and not cmdline[idx + 1].startswith("-"):
                    return cmdline[idx + 1]
                return None
            if tok.startswith(flag + "="):
                return tok[len(flag) + 1:]
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wsgateway", type=int, required=True, choices=(0, 1),
                        help="Expected count of wsgateway.py processes (0 or 1).")
    parser.add_argument("--pymultifeed", type=int, required=True, choices=(0, 1),
                        help="Expected count of pymultifeed.py processes (0 or 1).")
    parser.add_argument("--pkmultifeed", type=int, required=True, choices=(0, 1),
                        help="Expected count of pkmultifeed* processes (0 or 1).")
    parser.add_argument("--buffer-secs", type=int, default=60,
                        help="Slack added on top of (now - 18:00) for the age "
                             "cutoff (default 60). At 18:01 this yields ~120s.")
    parser.add_argument("--ntfy", nargs="?", const="all", default=None,
                        choices=list(email_utils.NTFY_TOPICS.keys()),
                        help="ntfy recipient for issues (default: all)")
    args = parser.parse_args()

    now = datetime.now()
    now_epoch = time.time()
    boundary = restart_boundary_epoch(now)
    cutoff = (now_epoch - boundary) + args.buffer_secs

    buckets = scan_processes()
    failures = []

    # (1) Count checks.
    expected = {
        "wsgateway": args.wsgateway,
        "pymultifeed": args.pymultifeed,
        "pkmultifeed": args.pkmultifeed,
    }
    for label, want in expected.items():
        got = len(buckets[label])
        if got != want:
            pids = sorted(e["pid"] for e in buckets[label])
            failures.append(
                f"COUNT {label}: expected {want}, found {got} (pids={pids})")

    # (2) Age checks across every matched process, pktrade included.
    for label in PY_PREFIXES + BIN_PREFIXES:
        for e in buckets[label]:
            age = now_epoch - e["create_time"]
            if age > cutoff:
                started = datetime.fromtimestamp(e["create_time"])
                failures.append(
                    f"AGE {label} pid={e['pid']}: {age:.0f}s old "
                    f"(> cutoff {cutoff:.0f}s), started {started:%Y-%m-%d %H:%M:%S} "
                    f"-- did not restart at {RESTART_HOUR:02d}:{RESTART_MINUTE:02d}")

    # (3) Every pktrade* has a -i/--strat-id, and no two share the same one.
    strat_id_to_pids = {}
    for e in buckets["pktrade"]:
        sid = extract_strat_id(e["cmdline"])
        if not sid:
            failures.append(
                f"STRAT_ID pktrade pid={e['pid']}: missing -i/--strat-id")
        else:
            strat_id_to_pids.setdefault(sid, []).append(e["pid"])
    for sid, pids in strat_id_to_pids.items():
        if len(pids) > 1:
            failures.append(
                f"STRAT_ID duplicate '{sid}' shared by pids={sorted(pids)}")

    host = socket.gethostname()
    if not failures:
        return 0

    body = (f"EOD safeguard found {len(failures)} problem(s) on {host} at "
            f"{now:%Y-%m-%d %H:%M:%S} (age cutoff {cutoff:.0f}s):\n\n"
            + "\n".join(failures))
    print(body)

    if args.ntfy:
        try:
            email_utils.send_ntfy_alert(
                msg=body, title=f"EOD SAFEGUARD FAILED on {host}", to=args.ntfy)
        except Exception as exc:  # alerting must never mask the exit code
            print(f"(send_ntfy_alert failed: {exc})")

    return 1


if __name__ == "__main__":
    sys.exit(main())
