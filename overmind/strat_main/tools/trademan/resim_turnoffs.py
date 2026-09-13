#!/usr/bin/env python3
"""Resim turned-off symbols to see if recent market behavior would've been better.

For each symbol that was recently disabled in a config, creates a temp config
with only that symbol enabled and runs sims over the last N trading days.
Reports per-symbol PnL, sharpe, fill rate, etc.
"""

import argparse
import commentjson
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util.chron import dates_list
from util.pathing import bindir
from coverage_report import find_turnoffs, LOCAL_BASE

PK_BIN = os.path.join(bindir(), "pktrade")
DEFAULT_SIM_DIR = os.path.expanduser("~/scratch/resim_turnoffs")


def last_n_weekdays(n, from_date=None):
    """Return the last N weekdays as YYYYMMDD strings, ending yesterday."""
    if from_date is None:
        from_date = datetime.today()
    # Start from yesterday
    d = from_date - timedelta(days=1)
    days = []
    while len(days) < n:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    days.reverse()
    return days


def make_single_sym_config(config_path, symbol, out_path):
    """Copy config but enable only the specified symbol, disable all others."""
    with open(config_path) as f:
        cfg = commentjson.load(f)

    for trader in cfg.get("pktraders", []):
        if trader.get("traded_symbol") == symbol:
            trader["enabled"] = True
        else:
            trader["enabled"] = False

    with open(out_path, "w") as f:
        json.dump(cfg, f, indent=2)


def run_single_resim(pk_bin, sim_dir, conf_path, dates, label):
    """Run sim for all dates and return list of acct file paths."""
    import subprocess

    os.makedirs(sim_dir, exist_ok=True)
    acct_files = []
    cmds = []

    for date in dates:
        acct_file = os.path.join(sim_dir, f"acct_{label}_{date}.csv")
        acct_files.append(acct_file)
        cmd = (f"{pk_bin} --date {date} --conf {conf_path} "
               f"--acct {acct_file} --trd /dev/null > /dev/null 2>&1")
        cmds.append(cmd)

    # Run in parallel batches
    batch_size = min(len(cmds), 10)
    for i in range(0, len(cmds), batch_size):
        batch = cmds[i:i + batch_size]
        procs = [subprocess.Popen(c, shell=True) for c in batch]
        for p in procs:
            p.wait()

    return acct_files


def parse_acct_files(acct_files, symbol):
    """Parse acct CSVs and return stats for the target symbol."""
    import pandas as pd
    import numpy as np

    rows = []
    for f in acct_files:
        if not os.path.exists(f) or os.path.getsize(f) == 0:
            continue
        try:
            df = pd.read_csv(f)
            sym_rows = df[df["sym"] == symbol]
            if not sym_rows.empty:
                rows.append(sym_rows)
        except Exception:
            continue

    if not rows:
        return None

    all_rows = pd.concat(rows)
    n_days = len(all_rows)
    if n_days == 0:
        return None

    avg_pnl = all_rows["net_pnl"].mean()
    total_pnl = all_rows["net_pnl"].sum()
    avg_comm = all_rows["commission"].mean()
    pct_positive = (all_rows["net_pnl"] > 0).sum() / n_days

    sharpe = 0
    if n_days > 1 and all_rows["net_pnl"].std() > 0:
        sharpe = avg_pnl / all_rows["net_pnl"].std()

    total_traded = all_rows["shs_traded"].sum()
    total_sent = all_rows["shs_sent"].sum()
    fill_rate = total_traded / total_sent if total_sent > 0 else 0

    avg_trades = all_rows["times_traded"].mean()
    med_trades = all_rows["times_traded"].median()

    return {
        "n_days": n_days,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "avg_comm": avg_comm,
        "sharpe": sharpe,
        "pct_positive": pct_positive,
        "fill_rate": fill_rate,
        "avg_trades": avg_trades,
        "med_trades": med_trades,
    }


def build_resim_report(results):
    """Build the resim results report."""
    lines = []
    lines.append("")
    lines.append("=" * 120)
    lines.append("RESIM RESULTS — would turned-off symbols have been profitable?")
    lines.append("=" * 120)
    lines.append("")

    if not results:
        lines.append("No results to report.")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"{'Symbol':<16} {'Config':<45} {'Days':>4} {'TotPnL':>9} {'AvgPnL':>9} "
        f"{'Sharpe':>7} {'Win%':>5} {'AvgTrd':>7} {'FillRt':>7}"
    )
    lines.append("-" * 120)

    # Group by symbol
    by_sym = defaultdict(list)
    for r in results:
        by_sym[r["symbol"]].append(r)

    for sym in sorted(by_sym):
        entries = sorted(by_sym[sym], key=lambda r: -r["stats"]["total_pnl"])
        for i, r in enumerate(entries):
            s = r["stats"]
            sym_col = sym if i == 0 else ""
            lines.append(
                f"{sym_col:<16} {r['config']:<45} {s['n_days']:>4} "
                f"{s['total_pnl']:>9.1f} {s['avg_pnl']:>9.1f} "
                f"{s['sharpe']:>7.2f} {s['pct_positive']:>5.0%} "
                f"{s['avg_trades']:>7.1f} {s['fill_rate']:>7.2%}"
            )

    # Summary: best candidates
    all_with_stats = [r for r in results if r["stats"]["total_pnl"] > 0]
    if all_with_stats:
        lines.append("")
        lines.append("=" * 120)
        lines.append("POSITIVE PnL candidates (consider re-enabling):")
        for r in sorted(all_with_stats, key=lambda r: -r["stats"]["total_pnl"]):
            s = r["stats"]
            lines.append(
                f"  {r['symbol']:<16} {r['config']:<45} "
                f"totPnL={s['total_pnl']:>8.1f}  sharpe={s['sharpe']:.2f}"
            )

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Resim turned-off symbols")
    parser.add_argument("--days", type=int, default=10,
                        help="Number of trading days to sim (default: 10)")
    parser.add_argument("--turnoff-days", type=int, default=60,
                        help="How far back to look for turnoffs (default: 60)")
    parser.add_argument("--sim-dir", default=DEFAULT_SIM_DIR,
                        help="Directory for sim output")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Only resim a specific symbol (e.g. xyz:TSLA)")
    parser.add_argument("--email", action="store_true",
                        help="Email the report")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be simmed without running")
    args = parser.parse_args()

    print("Finding turnoff candidates...")
    turnoffs = find_turnoffs(LOCAL_BASE, max_days=args.turnoff_days)
    if args.symbol:
        turnoffs = [t for t in turnoffs if t["symbol"] == args.symbol]

    if not turnoffs:
        print("No turnoff candidates found.")
        return

    print(f"Found {len(turnoffs)} turnoff(s) across "
          f"{len(set(t['symbol'] for t in turnoffs))} symbols")

    dates = last_n_weekdays(args.days)
    print(f"Sim dates: {dates[0]} to {dates[-1]} ({len(dates)} days)")

    if args.dry_run:
        print("\nDry run — would sim these:")
        for t in turnoffs:
            print(f"  {t['symbol']:<20} {t['config']}")
        return

    os.makedirs(args.sim_dir, exist_ok=True)
    results = []

    for i, t in enumerate(turnoffs):
        sym = t["symbol"]
        config_path = t["config_path"]
        label = f"{sym.replace(':', '_')}_{t['strat']}"

        if not os.path.exists(config_path):
            print(f"  [{i+1}/{len(turnoffs)}] SKIP {sym} — config not found: {config_path}")
            continue

        print(f"  [{i+1}/{len(turnoffs)}] {sym} via {t['config']}...", end=" ", flush=True)

        # Create temp config with only this symbol enabled
        run_dir = os.path.join(args.sim_dir, label)
        os.makedirs(run_dir, exist_ok=True)
        tmp_conf = os.path.join(run_dir, "resim_conf.json")
        make_single_sym_config(config_path, sym, tmp_conf)

        # Run sims
        acct_files = run_single_resim(PK_BIN, run_dir, tmp_conf, dates, label)

        # Parse results
        stats = parse_acct_files(acct_files, sym)
        if stats:
            results.append({
                "symbol": sym,
                "config": t["config"],
                "stats": stats,
            })
            print(f"totPnL={stats['total_pnl']:.1f}  sharpe={stats['sharpe']:.2f}")
        else:
            print("no data")

    report = build_resim_report(results)
    print(report)

    if args.email:
        from util import email_utils
        subject = f"Resim Turnoffs — {datetime.today().strftime('%Y-%m-%d')}"
        email_utils.send_mail(subject=subject, body=report, monospace=True)
        print("Report emailed.")


if __name__ == "__main__":
    main()
