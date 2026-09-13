"""Diagnose recent vs prior trailing windows for a (strat, sym) pair.

Compares HL micro-structure (and optionally topbook) between the last N
trading days and the prior M trading days, and flags dimensions that
shifted enough to suggest a market change.

Designed to be invoked from `trade_manager.py diagnose` and also runnable
standalone.
"""
import csv
import os
import sys
from pathlib import Path

# Allow running as a script from this directory.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pm_history  # noqa: E402
import pm_hl_stats  # noqa: E402


# (key, low_thresh, high_thresh) — ratio recent/prior below low or above high → flag.
# Calibrated 2026-06-19 on a 44-pair sweep: activity-driven metrics moved too easily
# at 2x, so widened to 3x; tighter-signal metrics (spread, vol_notional, ret_stdev)
# kept narrower.
HL_SHIFT_THRESHOLDS = [
    ("spread_bps_avg",        0.5,  2.0),
    ("vol_notional_per_day",  0.33, 3.0),
    ("med_inside_liq",        0.33, 3.0),
    ("n_midchanges_per_day",  0.33, 3.0),
    ("ret_stdev_annualized",  0.67, 1.5),
]

ACCT_SHIFT_THRESHOLDS = [
    ("net_pnl_per_day",       None, None),  # always report
    ("times_traded_per_day",  0.33, 3.0),
    ("times_flipped_per_day", 0.33, 3.0),
    ("shs_traded_per_day",    0.33, 3.0),
]

# Pair is only reported (under --alert-only) when at least this many metrics shifted.
DEFAULT_MIN_SHIFTS = 3


def list_acct_dates(local_base: Path, strat_path: str):
    strat_dir = Path(local_base) / strat_path
    out = []
    for f in sorted(strat_dir.glob("acct_*.csv")):
        d = f.stem.split("_", 1)[1]
        out.append(d)
    return out


def split_trailing(dates, recent_n, prior_n):
    """Return (prior_window, recent_window) date lists."""
    if len(dates) < recent_n + 1:
        return None, None
    recent = dates[-recent_n:]
    prior_start = max(0, len(dates) - recent_n - prior_n)
    prior = dates[prior_start:len(dates) - recent_n]
    return prior, recent


def acct_summary_for(strat_dir: Path, symbol: str, window_dates):
    rows = pm_history.load_history(strat_dir, symbol)
    sub = [r for r in rows if r["date"] in set(window_dates)]
    return pm_history.summarize(sub)


def ratio(bad, good):
    try:
        if good is None or bad is None:
            return None
        if good == 0:
            return float("inf") if bad else 0.0
        return bad / good
    except (TypeError, ZeroDivisionError):
        return None


def fmt_ratio(r):
    if r is None:
        return "—"
    if r == float("inf"):
        return "inf"
    return f"{r:.2f}x"


def fmt_val(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f"{v:,.2f}"
        return f"{v:.4f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def evaluate_shifts(prior_d: dict, recent_d: dict, thresholds):
    rows = []
    for key, lo, hi in thresholds:
        g = prior_d.get(key)
        b = recent_d.get(key)
        r = ratio(b, g)
        flagged = False
        if r is not None and r != float("inf") and lo is not None and hi is not None:
            if r <= lo or r >= hi:
                flagged = True
        rows.append((key, g, b, r, flagged))
    return rows


def print_table(label, rows):
    print(f"  {label}")
    print(f"    {'metric':<26}{'prior':>14}{'recent':>14}{'ratio':>10}  flag")
    for key, g, b, r, flagged in rows:
        flag = "  !!" if flagged else ""
        print(f"    {key:<26}{fmt_val(g):>14}{fmt_val(b):>14}{fmt_ratio(r):>10}{flag}")


def diagnose_one(local_base: Path, strat_path: str, symbol: str,
                 recent_n: int, prior_n: int, with_topbook: bool,
                 alert_only: bool, topbook_dataset: str = "XNAS.BASIC",
                 min_shifts: int = DEFAULT_MIN_SHIFTS):
    dates = list_acct_dates(local_base, strat_path)
    if not dates:
        print(f"  no acct data for {strat_path}")
        return

    prior, recent = split_trailing(dates, recent_n, prior_n)
    if not prior or not recent:
        print(f"  not enough trailing data for {strat_path} (have {len(dates)} days)")
        return

    prior_start, prior_end = prior[0], prior[-1]
    recent_start, recent_end = recent[0], recent[-1]

    print(f"\n=== {strat_path} :: {symbol} ===")
    print(f"  prior : {prior_start}-{prior_end} ({len(prior)} days)")
    print(f"  recent: {recent_start}-{recent_end} ({len(recent)} days)")

    # Acct-side
    strat_dir = Path(local_base) / strat_path
    acct_prior = acct_summary_for(strat_dir, symbol, prior) or {}
    acct_recent = acct_summary_for(strat_dir, symbol, recent) or {}
    acct_rows = evaluate_shifts(acct_prior, acct_recent, ACCT_SHIFT_THRESHOLDS)

    # HL-side (uses symstats binary range + cached returner outputs)
    try:
        hl_prior = pm_hl_stats.compute_window(symbol, prior_start, prior_end)
        hl_recent = pm_hl_stats.compute_window(symbol, recent_start, recent_end)
        hl_rows = evaluate_shifts(hl_prior, hl_recent, HL_SHIFT_THRESHOLDS)
    except Exception as e:
        print(f"  WARN: HL stats failed: {e}")
        hl_rows = []

    # Topbook (optional, opt-in)
    tb_rows = []
    if with_topbook:
        try:
            import pm_topbook_stats  # local import, only if needed
            # Topbook needs a bare ticker; strip 'xyz:' prefix
            tb_sym = symbol.split(":", 1)[-1]
            tb_prior = pm_topbook_stats.compute_window(tb_sym, prior_start, prior_end, topbook_dataset)
            tb_recent = pm_topbook_stats.compute_window(tb_sym, recent_start, recent_end, topbook_dataset)
            tb_rows = evaluate_shifts(tb_prior, tb_recent, [
                ("avg_spread_bps", 0.5, 2.0),
                ("notional_per_day", 0.5, 2.0),
                ("ret_stdev_annualized", 0.67, 1.5),
            ])
        except Exception as e:
            print(f"  WARN: topbook stats failed: {e}")

    n_shifts = sum(1 for *_, f in acct_rows + hl_rows + tb_rows if f)
    any_flagged = n_shifts > 0
    if alert_only and n_shifts < min_shifts:
        print(f"  ({n_shifts} shift(s); --alert-only min={min_shifts} suppressing detail)")
        return

    print_table("ACCT", acct_rows)
    if hl_rows:
        print_table("HL", hl_rows)
    if tb_rows:
        print_table("TOPBOOK", tb_rows)

    if any_flagged:
        flagged_keys = [k for k, *_, f in acct_rows + hl_rows + tb_rows if f]
        print(f"  FLAGGED: {', '.join(flagged_keys)}")
    else:
        print("  no significant shifts")


def parse_pairs(spec: str):
    """Parse 'alias/group/strat:xyz:SYMBOL[,...]' into list of (strat_path, symbol).

    Strat paths do not contain colons, so the first ':' separates strat_path
    from the symbol (which itself may contain ':' like 'xyz:MRVL')."""
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"expected 'strat_path:symbol' in '{chunk}'")
        sp, sym = chunk.split(":", 1)
        out.append((sp, sym))
    return out


def run_diagnose(args, config):
    local_base = Path(getattr(args, "local_base", None) or config["local_base"])
    pairs = parse_pairs(args.pairs)
    for strat_path, sym in pairs:
        diagnose_one(
            local_base, strat_path, sym,
            recent_n=args.days_recent,
            prior_n=args.days_prior,
            with_topbook=args.with_topbook,
            alert_only=args.alert_only,
            topbook_dataset=args.topbook_dataset,
            min_shifts=getattr(args, "min_shifts", DEFAULT_MIN_SHIFTS),
        )


def diagnose_flagged_rows(local_base, flagged_rows, recent_n, prior_n,
                          with_topbook=False, alert_only=False,
                          topbook_dataset="XNAS.BASIC",
                          min_shifts=DEFAULT_MIN_SHIFTS):
    """Run diagnose on a list of flagged rows from compute_flagged_pairs().

    Each row needs: alias, group, strat, sym, reasons.
    """
    local_base = Path(local_base)
    if not flagged_rows:
        print("No flagged pairs to diagnose.")
        return
    print(f"Diagnosing {len(flagged_rows)} flagged (strat, sym) pair(s)...")
    for r in flagged_rows:
        strat_path = f"{r['alias']}/{r['group']}/{r['strat']}"
        reasons = ",".join(r.get("reasons", []))
        print(f"\n# {strat_path} :: {r['sym']}  reasons=[{reasons}]")
        diagnose_one(
            local_base, strat_path, r["sym"],
            recent_n=recent_n,
            prior_n=prior_n,
            with_topbook=with_topbook,
            alert_only=alert_only,
            topbook_dataset=topbook_dataset,
            min_shifts=min_shifts,
        )


# Allow standalone use.
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", required=True,
                   help="Comma-separated 'alias/group/strat:xyz:SYMBOL' pairs")
    p.add_argument("--days-recent", type=int, default=5)
    p.add_argument("--days-prior", type=int, default=10)
    p.add_argument("--with-topbook", action="store_true",
                   help="Also pull topbook stats from Databento (costs credits)")
    p.add_argument("--topbook-dataset", default="XNAS.BASIC",
                   choices=["XNAS.BASIC", "EQUS.MINI"])
    p.add_argument("--alert-only", action="store_true",
                   help="Suppress detail when fewer than --min-shifts metrics shifted")
    p.add_argument("--min-shifts", type=int, default=DEFAULT_MIN_SHIFTS,
                   help=f"Minimum shifted metrics to print under --alert-only (default {DEFAULT_MIN_SHIFTS})")
    p.add_argument("--local-base", default=os.path.expanduser("~/scratch/tradeperf"))
    args = p.parse_args()
    run_diagnose(args, {"local_base": args.local_base})


if __name__ == "__main__":
    main()
