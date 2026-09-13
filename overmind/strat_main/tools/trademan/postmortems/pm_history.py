#!/usr/bin/env python
"""Per-symbol PnL history from local acct_*.csv files.

Walks ~/scratch/tradeperf/{strat_path}/, filters to one symbol, and emits a
daily CSV plus optional baseline-vs-bad summary stats.
"""
import argparse
import csv
import os
import sys
from datetime import datetime
from glob import glob
from pathlib import Path

DEFAULT_LOCAL_BASE = os.path.expanduser("~/scratch/tradeperf")

NUMERIC_COLS = [
    "net_pnl", "closed_pnl", "commission", "funding", "min_pnl_seen",
    "shs_traded", "shs_sent", "fill_rate", "reachable_fill_rate",
    "ioc_fill_rate", "times_traded", "times_flipped", "open_pos",
]


def load_history(strat_dir: Path, symbol: str):
    rows = []
    for acct in sorted(strat_dir.glob("acct_*.csv")):
        date_str = acct.stem.split("_", 1)[1]
        try:
            with acct.open() as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if r.get("sym") != symbol:
                        continue
                    out = {"date": date_str}
                    for c in NUMERIC_COLS:
                        v = r.get(c, "")
                        try:
                            out[c] = float(v) if v != "" else 0.0
                        except ValueError:
                            out[c] = 0.0
                    rows.append(out)
        except Exception as e:
            print(f"WARN: failed reading {acct}: {e}", file=sys.stderr)
    return rows


def parse_range(s: str):
    a, b = s.split(":")
    datetime.strptime(a, "%Y%m%d")
    datetime.strptime(b, "%Y%m%d")
    return a, b


def in_range(date_str: str, rng):
    return rng[0] <= date_str <= rng[1]


def write_csv(rows, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date"] + NUMERIC_COLS)
        for r in rows:
            w.writerow([r["date"]] + [r[c] for c in NUMERIC_COLS])


def summarize(rows):
    n = len(rows)
    if n == 0:
        return None
    tot_pnl = sum(r["net_pnl"] for r in rows)
    tot_closed = sum(r["closed_pnl"] for r in rows)
    tot_comm = sum(r["commission"] for r in rows)
    tot_fund = sum(r["funding"] for r in rows)
    tot_shs = sum(r["shs_traded"] for r in rows)
    tot_sent = sum(r["shs_sent"] for r in rows)
    tot_trades = sum(r["times_traded"] for r in rows)
    tot_flips = sum(r["times_flipped"] for r in rows)
    pos_days = sum(1 for r in rows if r["net_pnl"] > 0)
    neg_days = sum(1 for r in rows if r["net_pnl"] < 0)
    fill_rate = (tot_shs / tot_sent) if tot_sent > 0 else 0.0
    return {
        "days": n,
        "pos_days": pos_days,
        "neg_days": neg_days,
        "net_pnl_total": tot_pnl,
        "net_pnl_per_day": tot_pnl / n,
        "closed_pnl_per_day": tot_closed / n,
        "commission_per_day": tot_comm / n,
        "funding_per_day": tot_fund / n,
        "shs_traded_per_day": tot_shs / n,
        "times_traded_per_day": tot_trades / n,
        "times_flipped_per_day": tot_flips / n,
        "fill_rate_agg": fill_rate,
        "min_pnl_seen_worst": min(r["min_pnl_seen"] for r in rows),
    }


def fmt(v):
    if isinstance(v, float):
        if abs(v) >= 100:
            return f"{v:,.2f}"
        return f"{v:.4f}"
    return str(v)


def print_summary(label, s):
    if s is None:
        print(f"{label}: no rows")
        return
    print(f"\n=== {label} ===")
    for k, v in s.items():
        print(f"  {k:<25} {fmt(v)}")


def print_diff(base, bad):
    if not base or not bad:
        return
    print("\n=== DELTA (bad - baseline) ===")
    for k in base:
        try:
            d = bad[k] - base[k]
            print(f"  {k:<25} {fmt(d)}")
        except TypeError:
            pass


def cumulative_curve(rows):
    cum = 0.0
    out = []
    for r in rows:
        cum += r["net_pnl"]
        out.append((r["date"], r["net_pnl"], cum))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strat-path", required=True,
                   help="e.g. gf3/combined_equities_bfx3/usday")
    p.add_argument("--symbol", required=True, help="e.g. xyz:MRVL")
    p.add_argument("--local-base", default=DEFAULT_LOCAL_BASE)
    p.add_argument("--out", default=None, help="Output CSV path")
    p.add_argument("--baseline", default=None, help="YYYYMMDD:YYYYMMDD")
    p.add_argument("--bad", default=None, help="YYYYMMDD:YYYYMMDD")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--print-curve", action="store_true",
                   help="Print date, daily pnl, cumulative pnl")
    args = p.parse_args()

    strat_dir = Path(args.local_base) / args.strat_path
    if not strat_dir.is_dir():
        print(f"ERR: strat dir not found: {strat_dir}", file=sys.stderr)
        sys.exit(1)

    rows = load_history(strat_dir, args.symbol)
    if not rows:
        print(f"No rows for {args.symbol} in {strat_dir}", file=sys.stderr)
        sys.exit(2)

    print(f"Loaded {len(rows)} days for {args.symbol} in {args.strat_path}")
    print(f"Date range: {rows[0]['date']} – {rows[-1]['date']}")

    if args.out:
        write_csv(rows, Path(args.out))
        print(f"Wrote {args.out}")

    if args.print_curve:
        print("\ndate       daily_pnl   cumulative")
        for d, p_, c in cumulative_curve(rows):
            print(f"{d}  {p_:>10.2f}  {c:>12.2f}")

    if args.summary:
        full = summarize(rows)
        print_summary("FULL HISTORY", full)
        if args.baseline:
            br = parse_range(args.baseline)
            base = summarize([r for r in rows if in_range(r["date"], br)])
            print_summary(f"BASELINE {args.baseline}", base)
        else:
            base = None
        if args.bad:
            br = parse_range(args.bad)
            bad = summarize([r for r in rows if in_range(r["date"], br)])
            print_summary(f"BAD {args.bad}", bad)
        else:
            bad = None
        if base and bad:
            print_diff(base, bad)


if __name__ == "__main__":
    main()
