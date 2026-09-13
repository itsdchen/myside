#!/usr/bin/env python3
"""
TradeRecorder: Record all book trades with delayed feature snapshots.

Records every book trade (aggregated by transact_t) along with market features
looked up at configurable delays (1s for maker, 1.5s for taker), plus 30s/300s
markouts. Output CSVs are consumed by oracle_dp.py for DP optimization.

Subcommands:
    run           Run trade recording sims (creates trade_record CSVs)
    analyze       Run oracle DP analysis on existing CSVs

Usage:
    python pybin/TradeRecorder.py run HOOD \
        --start 20260227 --end 20260227 \
        --workdir ~/scratch/vol_research/claude_iter/trade_recorder_test

    python pybin/TradeRecorder.py analyze \
        --workdir ~/scratch/vol_research/claude_iter/trade_recorder_test
"""

import argparse
import json
import os
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

PK_BIN = os.path.join(bindir(), "pktrade")


# HIP3 symbol -> (remote_symbol, remote_book) for the reference signal.
# Symbols not in this table default to (name, "TopBookEquity").
REMOTE_REF = {
    "CL":         ("CL",  "TopBookCme"),
    "COPPER":     ("HG",  "TopBookCme"),
    "NATGAS":     ("NG",  "TopBookCme"),
    "XYZ100":     ("NQ",  "TopBookCme"),
    "BRENTOIL":   ("BZ",  "TopBookCme"),
    # Spot gold/silver feeds have known reliability issues. CME GC/SI are
    # more reliable through our vendor, though there's a futures/spot basis.
    "SILVER":     ("SI",  "TopBookCme"),
    "GOLD":       ("GC",  "TopBookCme"),
    "PLATINUM":   ("PLATINUM", "TopBookEquity"),
}


def make_trade_recorder_config(commodity, maker_delay_s=1.0, taker_delay_s=1.5):
    """Generate a TradeRecorder config for a HIP3 commodity symbol."""
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
                    "max_orders": 0,
                    "base_currency": "USDT",
                    "global_inherit": True,
                    "load_overnight_pos": False,
                    "min_pnl": -1000000,
                    "fv_limit": 2,
                    "timeout_minfv": 900,
                },
                "ordex": {
                    "type": "TradeRecorder",
                    "markets": ["Hyperliquid"],
                    "trade_caller": f"remote_tradecall_tempo_{name}",
                    "snapshot_tempo": f"snapshot_tempo_{name}",
                    "local_sig": f"local_mid_{name}",
                    "remote_sig": f"remote_mid_{name}",
                    "hardcoded_min_tick": 0.01,
                    "maker_delay_s": maker_delay_s,
                    "taker_delay_s": taker_delay_s,
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
                    },
                    {
                        "name": f"snapshot_tempo_{name}",
                        "type": "TimeTempo",
                        "thresh": 0.1,
                    },
                ],
                "extra_subs": [],
            }
        ],
    }


def run_one(commodity, workdir, start, end, maker_delay_s=1.0, taker_delay_s=1.5):
    """Run trade recording sims for one commodity. Returns True on success."""
    os.makedirs(workdir, exist_ok=True)

    conf = make_trade_recorder_config(commodity, maker_delay_s, taker_delay_s)
    conf_path = os.path.join(workdir, "pk_trade_recorder.json")
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


def cmd_run(args):
    """Run trade recording sims."""
    commodity = args.commodity.upper()
    return run_one(
        commodity, args.workdir, args.start, args.end,
        maker_delay_s=args.maker_delay,
        taker_delay_s=args.taker_delay,
    )


def cmd_analyze(args):
    """Run oracle DP analysis on existing trade record CSVs."""
    oracle_dp_path = os.path.join(script_dir, "oracle_dp.py")
    cmd = [
        sys.executable, oracle_dp_path,
        "--workdir", args.workdir,
        "--max-pos", str(args.max_pos),
    ]
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="TradeRecorder: Record book trades with delayed features",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Subcommand")

    # run
    p_run = sub.add_parser("run", help="Run trade recording sims")
    p_run.add_argument("commodity", help="Commodity name (e.g. HOOD)")
    p_run.add_argument("--start", required=True, help="Start date YYYYMMDD")
    p_run.add_argument("--end", required=True, help="End date YYYYMMDD")
    p_run.add_argument("--workdir", required=True, help="Output directory")
    p_run.add_argument("--maker-delay", type=float, default=1.0, help="Maker delay seconds (default: 1.0)")
    p_run.add_argument("--taker-delay", type=float, default=1.5, help="Taker delay seconds (default: 1.5)")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Run oracle DP analysis")
    p_analyze.add_argument("--workdir", required=True, help="Directory with trade_record CSVs")
    p_analyze.add_argument("--max-pos", type=int, default=5, help="Max position for oracle DP (default: 5)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        ok = cmd_run(args)
    elif args.command == "analyze":
        ok = cmd_analyze(args)
    else:
        parser.print_help()
        sys.exit(1)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
