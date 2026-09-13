#!/usr/bin/env python3
"""Print a snapshot of each host in the swarmhost pool.

Tells you at a glance:
  - Which hosts are reachable.
  - How busy they are (load, free memory).
  - How much disk pktrade + mktdata are using.
  - How many `pktrade` processes are running right now.
  - How many distinct experiments are sitting on each box.

Example usage:
    ./overmind/swarmhost/status.py                  # table, default hosts.yaml
    ./overmind/swarmhost/status.py --verbose        # +list active experiments
    ./overmind/swarmhost/status.py --hosts /path/to/hosts.yaml

Args:
    --hosts: optional path to hosts.yaml. Default: overmind/swarmhost/hosts.yaml.
    --verbose: also list active experiment dirs per host.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from overmind.swarmhost import _ssh  # noqa: E402
from overmind.swarmhost.experiment import HostPool  # noqa: E402


# Single shell snippet executed on each host. Emits key=value lines so we
# don't need jq on the remote and parsing is trivial.
#
# Also captures hints of compromise: top CPU consumers, processes whose
# binary lives in /dev/shm or /tmp/.<hidden> (classic miner hiding spots),
# 1-minute load. Caller flags suspicious combos.
_PROBE = r"""
echo "host=$(hostname)"
echo "uptime=$(uptime -p 2>/dev/null | sed 's/^up //')"
echo "load=$(uptime | awk -F'load average: ' '{print $2}')"
echo "load_1min=$(uptime | awk -F'load average: ' '{print $2}' | awk -F, '{gsub(/ /,"",$1); print $1}')"
echo "cpu_count=$(nproc)"
echo "mem_free=$(free -h | awk '/^Mem:/ {print $7}')"
echo "disk_root=$(df -h / 2>/dev/null | awk 'NR==2 {print $5 " of " $2}')"
echo "md_size=$(du -sh ~/tardis_datasets/gzpbf 2>/dev/null | cut -f1)"
echo "n_pktrade=$(ps -eo cmd --no-headers | grep -F /opt/pktrade/experiments/ | grep -v grep | wc -l)"
echo "n_experiments=$(ls -1 /opt/pktrade/experiments 2>/dev/null | wc -l)"
# Top 3 non-kernel processes by CPU — fast read for the caller to inspect.
echo "top_cpu_start"
ps -eo pcpu,user,pid,cmd --no-headers --sort=-pcpu | head -3
echo "top_cpu_end"
# Suspicious-location procs: /dev/shm or /tmp/.<hidden_dot_name>.
# These are classic crypto-miner / persistence spots.
echo "suspicious_start"
ls -l /proc/*/exe 2>/dev/null | awk '{print $11}' | grep -E '^(/dev/shm|/tmp/\.)' | sort -u
echo "suspicious_end"
if [ -d /opt/pktrade/experiments ]; then
    echo "experiments_list_start"
    ls -1 /opt/pktrade/experiments 2>/dev/null
    echo "experiments_list_end"
fi
""".strip()

# Process names that are almost always crypto miners (case-insensitive
# substring match against top-CPU cmdlines).
_MINER_NAMES = (
    "xmrig", "kdevtmpfsi", "kinsing", "netai", "minerd", "cpuminer",
    "perfctl", "kthrotlds", "ddgs",
)


def _probe_one(alias: str, timeout: float = 10.0) -> dict:
    """Run _PROBE on `alias`, return parsed dict. Marks unreachable hosts."""
    try:
        res = _ssh.run(alias, _PROBE, timeout=timeout, check=False)
    except _ssh.SshError as e:
        return {"alias": alias, "reachable": False, "err": e.stderr.strip()}
    out = {"alias": alias, "reachable": True, "experiments": [],
           "top_cpu": [], "suspicious_paths": []}
    section = None
    for line in res.stdout.splitlines():
        if line in ("experiments_list_start", "top_cpu_start", "suspicious_start"):
            section = line.replace("_start", "")
            continue
        if line in ("experiments_list_end", "top_cpu_end", "suspicious_end"):
            section = None
            continue
        if section == "experiments_list":
            if line.strip():
                out["experiments"].append(line.strip())
            continue
        if section == "top_cpu":
            if line.strip():
                out["top_cpu"].append(line.strip())
            continue
        if section == "suspicious":
            if line.strip():
                out["suspicious_paths"].append(line.strip())
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip()
    return out


def _detect_anomalies(row: dict) -> list:
    """Return a list of human-readable anomaly strings for one host's probe.

    Each item is a one-liner the caller can print under a host's row to
    flag potential compromise. Empty list = host looks normal.

    Heuristics (deliberately loose — false positives are fine, false
    negatives are not):
      - Any process running from /dev/shm/* or /tmp/.<hidden>.
      - Any top-CPU process whose cmdline matches a known miner name.
      - 1-minute load above 2× cpu_count while NO pktrade procs are running
        (something else is eating the box).
    """
    flags = []
    for path in row.get("suspicious_paths", []):
        flags.append(f"process running from suspicious path: {path}")
    for top in row.get("top_cpu", []):
        low = top.lower()
        for miner in _MINER_NAMES:
            if miner in low:
                flags.append(f"top-CPU proc matches known miner '{miner}': {top}")
                break
    try:
        load1 = float(row.get("load_1min", "0"))
        cpus = int(row.get("cpu_count", "1") or "1")
        n_pkt = int(row.get("n_pktrade", "0") or "0")
        if cpus > 0 and load1 > 2 * cpus and n_pkt == 0:
            flags.append(
                f"load 1min ({load1:.1f}) > 2× cores ({cpus}) "
                f"but 0 pktrade procs — something else is eating cores"
            )
    except ValueError:
        pass
    return flags


def _render_once(pool, verbose: bool):
    """Probe + print once. Returns nothing — for caller-driven loop reuse."""
    with ThreadPoolExecutor(max_workers=max(1, len(pool.hosts))) as ex:
        rows = list(ex.map(lambda h: _probe_one(h.alias), pool.hosts))
    _print_status(rows, verbose=verbose)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hosts", type=Path, default=_THIS_DIR / "hosts.yaml")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Also list active experiment dirs per host.")
    ap.add_argument("--watch", type=float, default=None, metavar="SECONDS",
                    help="Re-probe + reprint every SECONDS (Ctrl+C to quit). "
                         "Smoother than `watch -n N status.py` — no shell respawn.")
    args = ap.parse_args()

    pool = HostPool.from_yaml(args.hosts)
    if not pool.hosts:
        print("no hosts in inventory", file=sys.stderr)
        sys.exit(1)

    if args.watch is not None:
        # \033[2J = clear screen, \033[H = cursor home. Avoid full clear if
        # output isn't a TTY (e.g. piped to a file).
        clear = "\033[2J\033[H" if sys.stdout.isatty() else ""
        try:
            while True:
                print(clear, end="")
                print(f"swarmhost status — refreshing every {args.watch}s "
                      f"({datetime.now().strftime('%H:%M:%S')})  [Ctrl+C to quit]\n")
                _render_once(pool, verbose=args.verbose)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nstopped.")
        return

    # Single-shot path: probe + print once and exit.
    _render_once(pool, verbose=args.verbose)


def _print_status(rows, verbose=False):
    """Print anomaly warnings (if any) + the per-host status table.

    Pulled out of main so --watch can reuse it on every refresh tick.
    """
    # Detect anomalies + print a loud warning block FIRST so it's not buried
    # below the table. False positives are fine; false negatives are not.
    anomaly_rows = [(r, _detect_anomalies(r)) for r in rows if r.get("reachable")]
    if any(flags for _, flags in anomaly_rows):
        print("!" * 72)
        print("!! SUSPICIOUS ACTIVITY DETECTED — possible compromise. Investigate.")
        print("!! See playbook §7 (Recover a compromised host).")
        print("!" * 72)
        for r, flags in anomaly_rows:
            if not flags:
                continue
            print(f"\n[{r['alias']}]")
            for f in flags:
                print(f"  !! {f}")
            if r.get("top_cpu"):
                print(f"  top CPU procs:")
                for t in r["top_cpu"]:
                    print(f"    {t}")
        print()

    # Table layout. Keep narrow so it fits in a normal terminal.
    cols = [("alias", 10), ("load", 18), ("mem free", 8), ("md size", 8),
            ("disk /", 14), ("pkt procs", 10), ("exps", 5), ("uptime", 18)]
    header = "  ".join(f"{name:<{w}}" for name, w in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        if not r["reachable"]:
            print(f"{r['alias']:<10}  UNREACHABLE  ({r.get('err', '')})")
            continue
        line = "  ".join([
            f"{r['alias']:<10}",
            f"{r.get('load', '?'):<18}",
            f"{r.get('mem_free', '?'):<8}",
            f"{r.get('md_size', '-'):<8}",
            f"{r.get('disk_root', '?'):<14}",
            f"{r.get('n_pktrade', '0'):<10}",
            f"{r.get('n_experiments', '0'):<5}",
            f"{r.get('uptime', '?')[:18]:<18}",
        ])
        print(line)

    if verbose:
        print()
        for r in rows:
            if not r.get("reachable"):
                continue
            exps = r.get("experiments") or []
            print(f"[{r['alias']}] {len(exps)} experiment dir(s):")
            for e in exps:
                print(f"  {e}")


if __name__ == "__main__":
    main()
