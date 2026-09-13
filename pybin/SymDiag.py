#!/usr/bin/env python3
"""
SymDiag: Reusable symbol diagnostic tool.

Run unconstrained fill-diagnostic sims and analyze markouts binned by
features (vol, spread, premium, etc.) for any HIP3 commodity.

Subcommands:
    run           Run diagnostic sims (creates fill_diag CSVs in workdir)
    analyze       Analyze existing fill_diag CSVs
    run+analyze   Both in one shot
    report        Cross-symbol comparison report from analysis JSONs

Supports comma-separated symbols for batch runs:
    python pybin/SymDiag.py run+analyze SILVER,GOLD,NVDA \
        --start 20260120 --end 20260215 \
        --workdir /tmp/diag_batch

    This creates /tmp/diag_batch/{silver,gold,nvda}/ subdirectories,
    runs sims + analysis for each, copies analysis JSONs into
    /tmp/diag_batch/report/, and auto-generates a cross-symbol report.

Single-symbol usage (backward compatible):
    python pybin/SymDiag.py run+analyze SILVER \
        --start 20260120 --end 20260215 \
        --workdir /tmp/diag_silver

    python pybin/SymDiag.py analyze --workdir /tmp/diag_silver
    python pybin/SymDiag.py analyze --workdir /tmp/diag_silver --horizon 60s --n-bins 5

    python pybin/SymDiag.py report --diag-dir /tmp/diag_batch/report [-o report.txt]

See overmind/strat_main/util/diag_report.py for the full workflow documentation.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

##################################
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)
parent_dir = os.path.join(script_dir, "..")
sys.path.append(parent_dir)
sys.path.insert(0, os.path.join(parent_dir, "overmind", "strat_main"))
##################################

from util.chron import dates_avail
from util.pathing import bindir
from util.sim import sim_dates
from util.fill_diag import run_full_analysis
from util.diag_report import load_diag_jsons, gen_report

PK_BIN = os.path.join(bindir(), "pktrade")


# HIP3 symbol -> (remote_symbol, remote_book) for the reference signal.
# Symbols not in this table default to (name, "TopBookEquity").
REMOTE_REF = {
    "CL":      ("CL",  "TopBookCme"),
    "COPPER":  ("HG",  "TopBookCme"),
    "GOLD":    ("GC",  "TopBookCme"),
    "NATGAS":  ("NG",  "TopBookCme"),
    "SILVER":  ("SI",  "TopBookCme"),
    "XYZ100":  ("NQ",  "TopBookCme"),
}


def make_diag_config(commodity):
    """Generate a DiagnosticsMM config for a HIP3 commodity symbol.

    Creates a full pktrade config with unconstrained risk limits,
    appropriate signal/tempo definitions, and DiagnosticsMM ordex.
    """
    name = commodity.upper()
    remote_sym, remote_book = REMOTE_REF.get(name, (name, "TopBookEquity"))
    return {
        "settings": {
            "commissions": {
                "tiers": {"Hyperliquid": "HIP3_3"},
                "promo_tier": "Silver",
                "use_promo": True,
            },
            "start_t": "18:00:00 America/New_York",
            "end_t": "17:59:55 America/New_York",
        },
        "simulation": {
            "Hyperliquid": {
                "cxl_frac_ahead": 0.3,
                "use_one_way_latency": False,
                "sim_latency_secs": 1.0,
            }
        },
        "pktraders": [
            {
                "traded_symbol": f"xyz:{name}",
                "enabled": True,
                "size_mult": 1,
                "risk": {
                    "max_notional": 1000000,
                    "max_position": "1000000",
                    "max_orders": 20,
                    "base_currency": "USDT",
                    "global_inherit": True,
                    "load_overnight_pos": False,
                    "min_pnl": -1000000,
                    "fv_limit": 2,
                    "timeout_minfv": 900,
                },
                "ordex": {
                    "type": "DiagnosticsMM",
                    "markets": ["Hyperliquid"],
                    "trade_caller": f"remote_tradecall_tempo_{name}",
                    "max_pos": "10000",
                    "order_size": "1",
                    "tgt_order_notional": 4000,
                    "tgt_maxpos_notional": 9000000,
                    "hardcoded_min_tick": 0.01,
                    "max_back_levels": 2,
                    "rung_spacing_mult": 0.5,
                    "per_backlevel_rung_spacing_mult": 1,
                    "front_order_random_upper_limit": 1.1,
                    "front_order_random_lower_limit": 1,
                    "order_random_upper_limit": 1.1,
                    "order_random_lower_limit": 1,
                    "ms_between_place": 50,
                    "ms_between_cxl": 50,
                    "local_sig": f"local_mid_{name}",
                    "remote_sig": f"remote_mid_{name}",
                    "min_ord_lifetime": 0.5,
                    "premium_ema_coef": 1,
                    "premium_tdc_s": 100,
                    "place_thresh": 0.0003,
                    "cancel_buffer": 0.27,
                    "mm_gtc": False,
                    "ladder_one_sided": True,
                    "per_order_widen_frac": 0,
                    # Research instrumentation — periodic state-dump for the
                    # heavy_spikes_0506 investigation. Set to 0 to disable.
                    "periodic_dump_ms": 100,
                },
                "signals": [
                    {
                        "name": f"local_mid_{name}",
                        "type": "SigMid",
                        "symbol": f"xyz:{name}",
                        "books": ["Hyperliquid"],
                        "use_flagged": True,
                        "sigformer_tag": "top_level_signal",
                    },
                    {
                        "name": f"rel_mid_{name}",
                        "type": "SigMid",
                        "symbol": "BTC",
                        "books": ["Hyperliquid"],
                        "use_flagged": True,
                        "sigformer_tag": "top_level_signal",
                    },
                    {
                        "name": f"remote_mid_{name}",
                        "type": "SigQuoteMid",
                        "symbol": remote_sym,
                        "books": [remote_book],
                        "use_flagged": True,
                        "sigformer_tag": "top_level_signal",
                    },
                ],
                "tempos": [
                    {
                        "name": f"remote_tradecall_tempo_{name}",
                        "type": "FinalTempo",
                        "symbol": "BTC",
                        "markets": ["Hyperliquid"],
                    }
                ],
                "extra_subs": [],
            }
        ],
    }


def parse_commodities(commodity_str):
    """Parse comma-separated commodity names into a list."""
    return [c.strip().upper() for c in commodity_str.split(",") if c.strip()]


def preflight_data_check(commodities, start, end, base_workdir):
    """Write pktrade configs for all commodities, run --dry-run to discover
    subscriptions, then check data existence. Reports all missing data upfront."""
    from util.chron import dates_list
    from util import mktdata

    print(f"\n{'='*60}")
    print(f"  PRE-FLIGHT DATA CHECK")
    print(f"{'='*60}")

    data_dir = os.path.join(os.getenv("HOME"), "tardis_datasets", "gzpbf")
    dates = dates_list(start, end, dates_method="WEEKDAYS")
    if not dates:
        print("  No dates in range.")
        return

    single = len(commodities) == 1

    for commodity in commodities:
        # Write the config.
        conf = make_diag_config(commodity)
        conf_dir = base_workdir if single else os.path.join(base_workdir, commodity.lower())
        os.makedirs(conf_dir, exist_ok=True)
        conf_path = os.path.join(conf_dir, "pk_diag.json")
        with open(conf_path, "w") as f:
            json.dump(conf, f, indent=2)

        # Discover subscriptions via pktrade --dry-run.
        dryrun_cmd = f"{PK_BIN} --date {dates[0]} --conf {conf_path} --dry-run"
        result = subprocess.run(dryrun_cmd, shell=True, capture_output=True, text=True)

        sym_book_pairs = set()
        for line in result.stdout.split("\n"):
            line = line.strip()
            if not line or line[0] != "(" or line[-1] != ")":
                continue
            line = line.replace("(", "").replace(")", "")
            parts = line.split(",")
            bk = parts[0].strip()
            sym = parts[1].strip().replace("/", "_")
            if "USDT" in sym:
                continue
            sym_book_pairs.add((sym, bk))

        if not sym_book_pairs:
            print(f"\n  {commodity}: dry-run returned no subscriptions — check config")
            continue

        # Check data files per (sym, market).
        print(f"\n  {commodity}:")
        for sym, mkt in sorted(sym_book_pairs):
            channels = mktdata.books_to_channels.get(mkt, [])
            if not channels:
                continue
            channel = channels[0]
            missing_dates = set()
            for date in dates:
                fpath = os.path.join(data_dir, mkt, f"{sym}_{channel}_{date}.gzpbf")
                if not os.path.exists(fpath):
                    missing_dates.add(date)
            n_missing = len(missing_dates)
            n_total = len(dates)
            if n_missing == 0:
                print(f"    {mkt:20s} {sym:20s}  {n_total}/{n_total} OK")
            else:
                sample = sorted(missing_dates)[:5]
                extra = "..." if n_missing > 5 else ""
                print(f"    {mkt:20s} {sym:20s}  MISSING {n_missing}/{n_total}: {', '.join(sample)}{extra}")

    print(f"\n{'='*60}\n")


def run_one(commodity, workdir, start, end):
    """Run diagnostic sims for one commodity. Returns True on success."""
    os.makedirs(workdir, exist_ok=True)

    conf = make_diag_config(commodity)
    conf_path = os.path.join(workdir, "pk_diag.json")
    with open(conf_path, "w") as f:
        json.dump(conf, f, indent=2)
    print(f"  Config: {conf_path}")

    symbol = f"xyz:{commodity}"
    print(f"  Resolving dates {start} - {end} for {symbol}...")
    result = dates_avail(start, end, symbol, "Hyperliquid", dates_method="WEEKDAYS")
    dates = result["good_dates"]
    bad = result["bad_dates"]
    if bad:
        print(f"    Dates without data ({len(bad)}): {', '.join(bad[:5])}{'...' if len(bad) > 5 else ''}")
    if not dates:
        print(f"  ERROR: No available dates found for {commodity}")
        return False
    print(f"    {len(dates)} simulation dates: {dates[0]} to {dates[-1]}")

    print(f"  Running {len(dates)} sims...")
    sim_dates(PK_BIN, workdir, conf_path, dates, full_output=True)
    print(f"  Sims complete for {commodity}.")
    return True


def analyze_one(workdir, horizon="30s", n_bins=10):
    """Analyze one workdir. Returns True on success."""
    if not os.path.isdir(workdir):
        print(f"  ERROR: workdir {workdir} does not exist")
        return False

    df, results = run_full_analysis(workdir, horizon=horizon, n_bins=n_bins, print_output=True)

    out_path = os.path.join(workdir, "diag_analysis.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Wrote analysis to {out_path}")
    return True


def cmd_run(args):
    """Run diagnostic sims (single or batch)."""
    commodities = parse_commodities(args.commodity)

    # Pre-flight: check data availability for all symbols upfront.
    preflight_data_check(commodities, args.start, args.end, args.workdir)

    if len(commodities) == 1:
        return run_one(commodities[0], args.workdir, args.start, args.end)

    # Batch: subdirectory per symbol.
    all_ok = True
    for commodity in commodities:
        sym_dir = os.path.join(args.workdir, commodity.lower())
        print(f"\n{'='*60}")
        print(f"  {commodity}")
        print(f"{'='*60}")
        ok = run_one(commodity, sym_dir, args.start, args.end)
        if not ok:
            all_ok = False
    return all_ok


def cmd_analyze(args):
    """Analyze existing fill_diag results (single or batch)."""
    horizon = getattr(args, "horizon", "30s")
    n_bins = getattr(args, "n_bins", 10)

    # Check if workdir has subdirectories with fill_diag CSVs (batch mode).
    workdir = args.workdir
    import glob
    direct_csvs = glob.glob(os.path.join(workdir, "fill_diag_*.csv"))

    if direct_csvs:
        # Single-symbol mode: CSVs directly in workdir.
        return analyze_one(workdir, horizon=horizon, n_bins=n_bins)

    # Batch mode: look for subdirectories.
    subdirs = sorted([
        d for d in os.listdir(workdir)
        if os.path.isdir(os.path.join(workdir, d))
        and glob.glob(os.path.join(workdir, d, "fill_diag_*.csv"))
    ])
    if not subdirs:
        print(f"ERROR: No fill_diag CSVs found in {workdir} or its subdirectories")
        return False

    print(f"Found {len(subdirs)} symbol directories: {', '.join(subdirs)}")
    all_ok = True
    for sub in subdirs:
        sym_dir = os.path.join(workdir, sub)
        print(f"\n{'='*60}")
        print(f"  {sub.upper()}")
        print(f"{'='*60}")
        ok = analyze_one(sym_dir, horizon=horizon, n_bins=n_bins)
        if not ok:
            all_ok = False

    # Auto-generate report if multiple symbols.
    if len(subdirs) > 1:
        _auto_report(workdir, subdirs)

    return all_ok


def _auto_report(workdir, subdirs):
    """Collect analysis JSONs and generate cross-symbol report."""
    report_dir = os.path.join(workdir, "report")
    os.makedirs(report_dir, exist_ok=True)

    # Copy analysis JSONs into report dir.
    n_copied = 0
    for sub in subdirs:
        src = os.path.join(workdir, sub, "diag_analysis.json")
        if os.path.exists(src):
            dst = os.path.join(report_dir, f"{sub}.json")
            shutil.copy2(src, dst)
            n_copied += 1

    if n_copied < 2:
        return

    data = load_diag_jsons(report_dir)
    if not data:
        return

    print(f"\n{'='*60}")
    print(f"  CROSS-SYMBOL REPORT ({len(data)} symbols)")
    print(f"{'='*60}")

    report = gen_report(data)
    report_path = os.path.join(report_dir, "report.txt")
    with open(report_path, "w") as f:
        f.write(report)
    print(report)
    print(f"\nReport written to {report_path}")


def cmd_report(args):
    """Generate cross-symbol report from analysis JSONs."""
    diag_dir = args.diag_dir
    if not os.path.isdir(diag_dir):
        print(f"ERROR: {diag_dir} does not exist")
        return False

    data = load_diag_jsons(diag_dir)
    if not data:
        print(f"ERROR: No .json files found in {diag_dir}")
        return False

    print(f"Loaded {len(data)} symbols: {', '.join(data.keys())}")
    report = gen_report(data)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"Report written to {args.output}")
    print()
    print(report)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="SymDiag: Reusable symbol diagnostic tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Subcommand")

    # run
    p_run = sub.add_parser("run", help="Run diagnostic sims")
    p_run.add_argument("commodity", help="Commodity name(s), comma-separated (e.g. SILVER or SILVER,GOLD,NVDA)")
    p_run.add_argument("--start", required=True, help="Start date YYYYMMDD")
    p_run.add_argument("--end", required=True, help="End date YYYYMMDD")
    p_run.add_argument("--workdir", required=True, help="Output directory")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Analyze existing results")
    p_analyze.add_argument("--workdir", required=True, help="Directory with fill_diag CSVs (or parent of symbol subdirs)")
    p_analyze.add_argument("--horizon", default="30s", help="Markout horizon for binning (default: 30s)")
    p_analyze.add_argument("--n-bins", type=int, default=10, help="Number of bins (default: 10, deciles)")

    # run+analyze
    p_both = sub.add_parser("run+analyze", help="Run sims then analyze")
    p_both.add_argument("commodity", help="Commodity name(s), comma-separated (e.g. SILVER or SILVER,GOLD,NVDA)")
    p_both.add_argument("--start", required=True, help="Start date YYYYMMDD")
    p_both.add_argument("--end", required=True, help="End date YYYYMMDD")
    p_both.add_argument("--workdir", required=True, help="Output directory")
    p_both.add_argument("--horizon", default="30s", help="Markout horizon for binning (default: 30s)")
    p_both.add_argument("--n-bins", type=int, default=10, help="Number of bins (default: 10, deciles)")

    # report
    p_report = sub.add_parser("report", help="Generate cross-symbol report from analysis JSONs")
    p_report.add_argument("--diag-dir", required=True, help="Directory containing diag_analysis.json files")
    p_report.add_argument("--output", "-o", help="Output file path (also prints to stdout)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        ok = cmd_run(args)
    elif args.command == "analyze":
        ok = cmd_analyze(args)
    elif args.command == "run+analyze":
        ok = cmd_run(args)
        if ok:
            ok = cmd_analyze(args)
    elif args.command == "report":
        ok = cmd_report(args)
    else:
        parser.print_help()
        sys.exit(1)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
