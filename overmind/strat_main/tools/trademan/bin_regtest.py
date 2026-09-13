"""Binary regression testing: compare sim results between two pktrade builds.

Runs the same strats/dates with a base binary (from a clean repo) and the test
binary (from this repo), then compares PnL, volume, and sharpe head-to-head.

Usage:
    python trade_manager.py bin-regtest \\
        --base-repo /path/to/clean/retraded \\
        [--days 7] [--date-range 20260325:20260402] \\
        [--strat usday] [--group exotic] \\
        [--auto-all] \\
        [--tol-pnl-pct 10] [--tol-vol-pct 15]

Arguments:
    --base-repo      Path to a clean checkout of the repo (reference build)
    --days           Number of recent trading days to sim (default: 7)
    --date-range     Explicit date range START:END (YYYYMMDD:YYYYMMDD)
    --strat          Filter strats by substring
    --group          Filter groups by substring
    --auto-all       Skip interactive selection, run all discovered strats
    --tol-pnl-pct    PnL tolerance threshold in percent (default: 10)
    --tol-vol-pct    Volume tolerance threshold in percent (default: 15)
    --aliases        Comma-separated SSH aliases (default: all from config)
    --skip-fetch     Skip remote file fetch (use local data only)
    --skip-md        Skip market data download


Example cmdline:
> trade_manager.py bin-regtest --base-repo /home/david/tradefi/retraded_cron/ --days 2 --aliases gf1 --strat '^usday$' --auto-all --skip-fetch

"""

import io
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── Path setup for imports from strat_main ───────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util.pathing import bindir
from util.sim import sim_one_day


def run_bin_regtest(args, config):
    """Run binary regression test: base vs test pktrade builds."""
    from sim_eval_select import interactive_select
    from sim_eval_compare import load_acct_file
    from sim_eval import (
        _parse_dates, _parse_aliases, _fetch_all,
        _download_market_data, _find_dated_config,
    )

    # ── 0. Verify both binaries exist ────────────────────────────────────
    base_repo = os.path.expanduser(args.base_repo)
    base_bin = os.path.join(base_repo, "bin", "pktrade")
    test_bin = os.path.join(bindir(), "pktrade")

    if not os.path.isfile(base_bin):
        print(f"ERROR: Base binary not found: {base_bin}")
        print("  Build pktrade in the base repo first.")
        return
    if not os.path.isfile(test_bin):
        print(f"ERROR: Test binary not found: {test_bin}")
        print("  Build pktrade in this repo first.")
        return

    print(f"Base binary: {base_bin}")
    print(f"Test binary: {test_bin}")

    # ── 1. Parse dates ───────────────────────────────────────────────────
    # Default to 7 days if nothing specified
    if not getattr(args, "date", None) and not getattr(args, "date_range", None) and not getattr(args, "days", None):
        args.days = 7
    dates = _parse_dates(args)
    date_strs = [d.strftime("%Y%m%d") for d in dates]
    print(f"Dates: {date_strs[0]} .. {date_strs[-1]} ({len(dates)} day(s))")

    local_base = getattr(args, "local_base", None) or config["local_base"]
    aliases = _parse_aliases(args, config)

    # ── 2. Fetch live data (for configs) ─────────────────────────────────
    if not args.skip_fetch:
        print()
        print("=" * 80)
        print("STEP 1: Fetching configs from remote machines")
        print("=" * 80)
        _fetch_all(aliases, dates, local_base, config["remote_base"])
    else:
        print("[skip-fetch] Using local files only.")

    # ── 3. Discover strats ───────────────────────────────────────────────
    print()
    print("=" * 80)
    print("STEP 2: Discovering strats")
    print("=" * 80)

    from trade_manager import load_acct_data
    acct_data = load_acct_data(local_base, aliases=aliases,
                               date_from=date_strs[0], date_to=date_strs[-1])
    if acct_data.empty:
        print("No acct data found. Run without --skip-fetch?")
        return

    # Apply filters
    strat_filter = getattr(args, "strat", None)
    group_filter = getattr(args, "group", None)
    if strat_filter:
        acct_data = acct_data[acct_data["strat"].str.contains(strat_filter, case=False)]
    if group_filter:
        acct_data = acct_data[acct_data["group"].str.contains(group_filter, case=False)]
    if acct_data.empty:
        print("No data after filtering.")
        return

    # Deduplicate to (alias, group, strat) level — we don't need per-sym selection
    strats = (acct_data[["alias", "group", "strat"]]
              .drop_duplicates()
              .sort_values(["alias", "group", "strat"])
              .to_dict("records"))
    print(f"Found {len(strats)} strat(s).")

    # For interactive select, build pairs (reuse existing selector format)
    pairs = (acct_data[["alias", "group", "strat", "sym"]]
             .drop_duplicates()
             .sort_values(["alias", "group", "strat", "sym"])
             .to_dict("records"))

    # ── 4. Select strats ─────────────────────────────────────────────────
    if args.auto_all:
        selected = pairs
        print(f"Auto-selected all {len(strats)} strat(s).")
    else:
        selected = interactive_select(pairs)

    if not selected:
        print("Nothing selected.")
        return

    # Reduce back to unique strats from selection
    selected_strats = list({(p["alias"], p["group"], p["strat"]) for p in selected})
    selected_strats.sort()

    # ── 5. Download market data ──────────────────────────────────────────
    if not args.skip_md:
        print()
        print("=" * 80)
        print("STEP 3: Downloading market data")
        print("=" * 80)
        _download_market_data(selected, dates, 0, local_base)
    else:
        print("[skip-md] Skipping market data download.")

    # ── 6. Run sims (base and test) ──────────────────────────────────────
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("/tmp", "binregtest", run_id)
    base_dir = os.path.join(out_dir, "base")
    test_dir = os.path.join(out_dir, "test")

    print()
    print("=" * 80)
    print("STEP 4: Running simulations")
    print("=" * 80)
    print(f"Output dir: {out_dir}")

    base_results = _run_regtest_sims(
        base_bin, "base", base_dir, selected_strats, dates, local_base)
    test_results = _run_regtest_sims(
        test_bin, "test", test_dir, selected_strats, dates, local_base)

    # ── 7. Compare and report ────────────────────────────────────────────
    print()
    print("=" * 80)
    print("STEP 5: Comparing results")
    print("=" * 80)

    tol_pnl = getattr(args, "tol_pnl_pct", 10) or 10
    tol_vol = getattr(args, "tol_vol_pct", 15) or 15

    report_text = _build_regtest_report(
        base_results, test_results, date_strs,
        tol_pnl_pct=tol_pnl, tol_vol_pct=tol_vol,
    )
    print()
    print(report_text, end="")

    # Save report to file
    report_path = os.path.join(out_dir, "report.txt")
    with open(report_path, "w") as f:
        f.write(report_text)
    print(f"\nReport saved to: {report_path}")
    print(f"Artifacts in:    {out_dir}")


# ── Sim runner ───────────────────────────────────────────────────────────────

def _run_regtest_sims(pk_bin, label, sim_base, strat_keys, dates, local_base):
    """Run sims for all strat×date combinations with a given binary.

    Args:
        pk_bin:      Path to pktrade binary
        label:       "base" or "test" (for logging)
        sim_base:    Output directory root for this binary's results
        strat_keys:  List of (alias, group, strat) tuples
        dates:       List of datetime objects
        local_base:  Path to local tradeperf data (for configs)

    Returns:
        List of result dicts with keys: alias, group, strat, date, acct_path
    """
    from sim_eval import _find_dated_config

    results = []
    for alias, group, strat in strat_keys:
        strat_sim_dir = os.path.join(sim_base, alias, group, strat)
        os.makedirs(strat_sim_dir, exist_ok=True)

        for date in sorted(dates):
            date_str = date.strftime("%Y%m%d")
            config_path = _find_dated_config(local_base, alias, group, strat, date_str)
            if not config_path:
                print(f"  [{label}] [warn] No config for {alias}/{group}/{strat} on {date_str}")
                continue

            print(f"  [{label}] {alias}/{group}/{strat}  date={date_str}")
            acct_path = sim_one_day(
                pk_bin, strat_sim_dir, config_path, date_str,
                acct_suffix=f"regtest_{date_str}",
                trd_suffix="",
                print_cmd=False,
            )
            results.append({
                "alias": alias,
                "group": group,
                "strat": strat,
                "date": date_str,
                "acct_path": acct_path,
            })

    return results


# ── Comparison and reporting ─────────────────────────────────────────────────

def _build_regtest_report(base_results, test_results, date_strs,
                          tol_pnl_pct=10, tol_vol_pct=15):
    """Build the regression test comparison report.

    Compares base vs test sim results with:
    - Per-strat aggregate stats (PnL, volume, sharpe)
    - Head-to-head per-date breakdown (+/-/= with magnitude)
    - Tolerance flags for large deviations

    Args:
        base_results:  List of result dicts from base binary sims
        test_results:  List of result dicts from test binary sims
        date_strs:     List of date strings
        tol_pnl_pct:   PnL tolerance threshold (percent)
        tol_vol_pct:   Volume tolerance threshold (percent)

    Returns:
        Report text string
    """
    from sim_eval_compare import load_acct_file

    out = io.StringIO()
    out.write("=" * 80 + "\n")
    out.write("BINARY REGRESSION TEST REPORT\n")
    out.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    out.write(f"  Dates: {date_strs[0]} .. {date_strs[-1]} ({len(date_strs)}d)\n")
    out.write(f"  Tolerances: pnl={tol_pnl_pct}%  vol={tol_vol_pct}%\n")
    out.write("=" * 80 + "\n")

    # Index results by (alias, group, strat, date)
    base_by_key = {}
    for r in base_results:
        base_by_key[(r["alias"], r["group"], r["strat"], r["date"])] = r
    test_by_key = {}
    for r in test_results:
        test_by_key[(r["alias"], r["group"], r["strat"], r["date"])] = r

    # Get all strats
    all_strats = sorted(set(
        (r["alias"], r["group"], r["strat"]) for r in base_results + test_results
    ))

    any_flags = False

    for alias, group, strat in all_strats:
        strat_label = f"[{alias}] {group}/{strat}"
        out.write(f"\n{'─' * 80}\n")
        out.write(f"  {strat_label}\n")
        out.write(f"{'─' * 80}\n")

        # Collect per-date stats
        day_stats = []
        for ds in date_strs:
            key = (alias, group, strat, ds)
            base_r = base_by_key.get(key)
            test_r = test_by_key.get(key)
            if not base_r or not test_r:
                continue

            base_acct = load_acct_file(base_r["acct_path"])
            test_acct = load_acct_file(test_r["acct_path"])
            if base_acct is None or test_acct is None:
                continue

            # Aggregate across all symbols for this day
            base_pnl = base_acct["net_pnl"].sum() if "net_pnl" in base_acct.columns else 0
            test_pnl = test_acct["net_pnl"].sum() if "net_pnl" in test_acct.columns else 0
            base_vol = base_acct["shs_traded"].sum() if "shs_traded" in base_acct.columns else 0
            test_vol = test_acct["shs_traded"].sum() if "shs_traded" in test_acct.columns else 0
            base_trades = base_acct["times_traded"].sum() if "times_traded" in base_acct.columns else 0
            test_trades = test_acct["times_traded"].sum() if "times_traded" in test_acct.columns else 0

            day_stats.append({
                "date": ds,
                "base_pnl": base_pnl,
                "test_pnl": test_pnl,
                "base_vol": base_vol,
                "test_vol": test_vol,
                "base_trades": base_trades,
                "test_trades": test_trades,
            })

        if not day_stats:
            out.write("  No comparable data.\n")
            continue

        # ── Per-date h2h table ───────────────────────────────────────
        hdr = (f"  {'date':<10} {'base_pnl':>10} {'test_pnl':>10} {'diff':>10} "
               f"{'result':>6} {'base_vol':>10} {'test_vol':>10} {'vol_diff%':>9}")
        out.write(hdr + "\n")
        out.write("  " + "-" * 77 + "\n")

        pnl_wins = 0  # test won
        pnl_losses = 0  # base won
        pnl_draws = 0
        pnl_diffs = []
        flags = []

        for ds in day_stats:
            pnl_diff = ds["test_pnl"] - ds["base_pnl"]
            pnl_diffs.append(pnl_diff)

            # Determine h2h result (use a small absolute threshold for "draw")
            avg_abs = (abs(ds["base_pnl"]) + abs(ds["test_pnl"])) / 2
            if avg_abs > 0 and abs(pnl_diff) / avg_abs < 0.02:
                result = "="
                pnl_draws += 1
            elif pnl_diff > 0:
                result = "+"
                pnl_wins += 1
            else:
                result = "-"
                pnl_losses += 1

            # Volume diff percent
            vol_diff_pct = 0
            if ds["base_vol"] > 0:
                vol_diff_pct = (ds["test_vol"] - ds["base_vol"]) / ds["base_vol"] * 100

            # Check tolerances
            day_flags = []
            if avg_abs > 0:
                pnl_diff_pct = abs(pnl_diff) / avg_abs * 100
                if pnl_diff_pct > tol_pnl_pct:
                    day_flags.append(f"pnl:{pnl_diff_pct:+.1f}%")
                    any_flags = True
            if abs(vol_diff_pct) > tol_vol_pct:
                day_flags.append(f"vol:{vol_diff_pct:+.1f}%")
                any_flags = True

            flag_str = " !" + ",".join(day_flags) if day_flags else ""
            flags.append(day_flags)

            out.write(f"  {ds['date']:<10} {ds['base_pnl']:>10,.2f} {ds['test_pnl']:>10,.2f} "
                      f"{pnl_diff:>+10,.2f} {result:>6} "
                      f"{ds['base_vol']:>10,.0f} {ds['test_vol']:>10,.0f} "
                      f"{vol_diff_pct:>+8.1f}%{flag_str}\n")

        # ── Aggregate stats ──────────────────────────────────────────
        base_pnls = [d["base_pnl"] for d in day_stats]
        test_pnls = [d["test_pnl"] for d in day_stats]
        base_vols = [d["base_vol"] for d in day_stats]
        test_vols = [d["test_vol"] for d in day_stats]
        n_days = len(day_stats)

        base_total_pnl = sum(base_pnls)
        test_total_pnl = sum(test_pnls)
        base_avg_pnl = base_total_pnl / n_days
        test_avg_pnl = test_total_pnl / n_days

        # Sharpe (daily, not annualized)
        base_sharpe = _sharpe(base_pnls)
        test_sharpe = _sharpe(test_pnls)

        base_total_vol = sum(base_vols)
        test_total_vol = sum(test_vols)

        out.write("  " + "-" * 77 + "\n")
        out.write(f"\n  Summary ({n_days}d):\n")
        out.write(f"    {'':15} {'base':>12} {'test':>12} {'diff':>12}\n")
        out.write(f"    {'total pnl':15} {base_total_pnl:>12,.2f} {test_total_pnl:>12,.2f} "
                  f"{test_total_pnl - base_total_pnl:>+12,.2f}\n")
        out.write(f"    {'avg daily pnl':15} {base_avg_pnl:>12,.2f} {test_avg_pnl:>12,.2f} "
                  f"{test_avg_pnl - base_avg_pnl:>+12,.2f}\n")
        out.write(f"    {'sharpe':15} {base_sharpe:>12.3f} {test_sharpe:>12.3f} "
                  f"{test_sharpe - base_sharpe:>+12.3f}\n")
        out.write(f"    {'total volume':15} {base_total_vol:>12,.0f} {test_total_vol:>12,.0f} "
                  f"{test_total_vol - base_total_vol:>+12,.0f}\n")
        out.write(f"\n  H2H (test vs base): {pnl_wins}W / {pnl_losses}L / {pnl_draws}D")
        if pnl_diffs:
            avg_diff = np.mean(pnl_diffs)
            out.write(f"  avg diff: {avg_diff:+,.2f}/day")
        out.write("\n")

    # ── Overall verdict ──────────────────────────────────────────────────
    out.write("\n" + "=" * 80 + "\n")
    if any_flags:
        out.write(f"RESULT: TOLERANCE EXCEEDED (pnl>{tol_pnl_pct}% or vol>{tol_vol_pct}%)\n")
        out.write("  Review flagged days (marked with !) above.\n")
    else:
        out.write("RESULT: WITHIN TOLERANCE\n")
    out.write("=" * 80 + "\n")

    return out.getvalue()


def _sharpe(pnls):
    """Compute daily Sharpe ratio (not annualized) from a list of PnLs."""
    if len(pnls) < 2:
        return 0.0
    avg = np.mean(pnls)
    std = np.std(pnls, ddof=1)
    if std == 0:
        return 0.0
    return avg / std
