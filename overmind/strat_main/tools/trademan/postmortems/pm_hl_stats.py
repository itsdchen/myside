#!/usr/bin/env python
"""HL spread / volume / intraday-vol stats for a (symbol, window).

Uses two pktrade binaries:
  symstats  — aggregate stats over a date range (spread, volume, etc.)
  returner  — per-minute HL mids and 1-min returns, one date at a time.

Caches returner outputs under ~/scratch/postmortems/_returner_cache/.
"""
import argparse
import csv
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Derive the repo root from this script's location:
#   {REPO}/overmind/strat_main/tools/trademan/postmortems/pm_hl_stats.py
# Override with PKTRADE_REPO env var.
REPO = Path(os.environ.get("PKTRADE_REPO",
                           str(Path(__file__).resolve().parents[5])))
SYMSTATS_BIN = REPO / "bin" / "symstats"
RETURNER_BIN = REPO / "bin" / "returner"
CACHE = Path(os.path.expanduser("~/scratch/postmortems/_returner_cache"))


def _safe(sym: str) -> str:
    return sym.replace(":", "_").replace("/", "_")


def run_symstats(symbol: str, start: str, end: str, book: str = "Hyperliquid") -> dict:
    cmd = [str(SYMSTATS_BIN), "--book", book, "--symbol", symbol,
           "--date", start, "--end-date", end, "--csv"]
    out = subprocess.check_output(cmd, text=True)
    rows = list(csv.DictReader(out.splitlines()))
    if not rows:
        raise RuntimeError(f"symstats returned no rows for {symbol} {start}-{end}")
    r = rows[0]
    out = {
        "symbol": r["symbol"],
        "num_trades": int(r["num_trades"]),
        "vol_shs": float(r["vol"]),
        "med_trdsz": float(r["med_trdsz"]),
        "med_inside_liq": float(r["med_inside_liq"]),
        "avg_mid": float(r["avg_mid"]),
        "avg_sprd": float(r["avg_sprd"]),
        "spread_pct": float(r["spread_pct"]),
        "spread_bps": float(r["spread_pct"]) * 1e4,
        "n_midchanges": int(r["n_midchanges"]),
    }
    # Depth stat columns (available once symstats binary is rebuilt with the
    # depth-for-notional stat). Silently absent on older CSV outputs.
    for k in ("depth_bps_bid_30k", "depth_bps_ask_30k",
              "depth_bps_bid_60k", "depth_bps_ask_60k",
              "depth_bps_bid_250k", "depth_bps_ask_250k",
              "depth_bps_bid_2M", "depth_bps_ask_2M"):
        if k in r and r[k] not in ("", "nan", "-nan"):
            try:
                out[k] = float(r[k])
            except ValueError:
                pass
    return out


def daterange(start: str, end: str, weekdays_only: bool = False):
    """Yield dates from start to end inclusive.

    weekdays_only=True skips Saturday (weekday=5) and Sunday (weekday=6).
    Strategies only run on weekdays, so if you want per-day averages that
    reflect what the strat sees, set weekdays_only=True and call
    `run_symstats` per-weekday (see `compute_window_weekdays`).
    """
    d = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    while d <= e:
        if not weekdays_only or d.weekday() < 5:
            yield d.strftime("%Y%m%d")
        d += timedelta(days=1)


def run_returner(symbol: str, date: str, force: bool = False) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"{_safe(symbol)}_{date}.csv"
    if out.exists() and out.stat().st_size > 0 and not force:
        return out
    cmd = [str(RETURNER_BIN), "--symbol", symbol, "--date", date,
           "--out", str(out)]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def load_mids(path: Path):
    rows = []
    with path.open() as f:
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) < 3:
                continue
            try:
                ts_s = int(parts[1])
                mid = float(parts[2])
            except ValueError:
                continue
            if mid > 0:
                rows.append((ts_s, mid))
    return rows


def resampled_returns(all_mids, period_s: int):
    """Compute log returns at `period_s` granularity from concatenated mids."""
    if not all_mids:
        return []
    rets = []
    # Snap to period boundaries: for each minute, only keep if ts % period == 0
    bucketed = [(ts, mid) for ts, mid in all_mids if ts % period_s == 0]
    bucketed.sort()
    for i in range(1, len(bucketed)):
        ts0, m0 = bucketed[i - 1]
        ts1, m1 = bucketed[i]
        # Only emit a return if buckets are adjacent (within tolerance)
        if ts1 - ts0 != period_s:
            continue
        if m0 <= 0 or m1 <= 0:
            continue
        rets.append(math.log(m1 / m0))
    return rets


def stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def annualize_intraday(stdev_per_period, period_s):
    # HL trades 24/7; 365 days * 86400 s = 31,536,000 s/yr
    periods_per_year = 31_536_000 / period_s
    return stdev_per_period * math.sqrt(periods_per_year)


def compute_window(symbol: str, start: str, end: str, period_s: int = 300,
                   skip_returner: bool = False, force: bool = False,
                   weekdays_only: bool = False):
    """Compute HL micro-structure stats over [start, end].

    If weekdays_only=True: calls symstats per-weekday and sums / averages
    numerators, then divides by weekday count. This matches what strategies
    that only run weekdays see. Note: `spread_bps_avg`, `med_trdsz`, and
    `med_inside_liq` are re-averaged across per-day symstats calls (mean
    of daily values, not the aggregate) — a small approximation vs a true
    weekday-only aggregation inside the binary.
    """
    if weekdays_only:
        return _compute_window_weekdays(symbol, start, end, period_s,
                                        skip_returner, force)
    ss = run_symstats(symbol, start, end)
    notional = ss["vol_shs"] * ss["avg_mid"]
    n_days = sum(1 for _ in daterange(start, end))
    out = {
        "symbol": symbol,
        "window": f"{start}-{end}",
        "days": n_days,
        "spread_bps_avg": ss["spread_bps"],
        "avg_sprd_abs": ss["avg_sprd"],
        "avg_mid": ss["avg_mid"],
        "num_trades": ss["num_trades"],
        "vol_shs": ss["vol_shs"],
        "vol_notional": notional,
        "vol_notional_per_day": notional / n_days,
        "med_trdsz": ss["med_trdsz"],
        "med_inside_liq": ss["med_inside_liq"],
        "n_midchanges": ss["n_midchanges"],
        "n_midchanges_per_day": ss["n_midchanges"] / n_days,
    }
    if not skip_returner:
        all_mids = []
        for d in daterange(start, end):
            try:
                p = run_returner(symbol, d, force=force)
                all_mids.extend(load_mids(p))
            except subprocess.CalledProcessError as e:
                print(f"WARN: returner failed for {symbol} {d}: {e}", file=sys.stderr)
        rets = resampled_returns(all_mids, period_s)
        sd = stdev(rets)
        out["ret_period_s"] = period_s
        out["n_returns"] = len(rets)
        out["ret_stdev"] = sd
        out["ret_stdev_bps"] = sd * 1e4
        out["ret_stdev_annualized"] = annualize_intraday(sd, period_s)
    return out


def _compute_window_weekdays(symbol, start, end, period_s, skip_returner, force):
    """Weekday-only per-symbol aggregation. Calls symstats once per weekday
    and combines the daily results — safe against binary internals summing
    across weekends."""
    days = list(daterange(start, end, weekdays_only=True))
    n_days = len(days)
    if n_days == 0:
        raise RuntimeError(f"No weekdays in {start}:{end}")
    dailies = []
    for d in days:
        try:
            row = run_symstats(symbol, d, d)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            print(f"WARN: symstats failed for {symbol} {d}: {e}", file=sys.stderr)
            continue
        # Skip zero-trade / no-data days. symstats writes `-nan` for avg_mid/
        # spread on days with num_trades=0 which poisons the average.
        if row["num_trades"] == 0 or not math.isfinite(row.get("avg_mid", float("nan"))):
            continue
        dailies.append(row)
    if not dailies:
        raise RuntimeError(f"symstats returned no rows for {symbol} on any weekday in {start}:{end}")
    # Sums for volume-like columns
    total_trades = sum(x["num_trades"] for x in dailies)
    total_vol_shs = sum(x["vol_shs"] for x in dailies)
    total_midchanges = sum(x["n_midchanges"] for x in dailies)
    # Means for rate/depth columns (weighted equally per weekday)
    avg_mid = sum(x["avg_mid"] for x in dailies) / len(dailies)
    spread_bps = sum(x["spread_bps"] for x in dailies) / len(dailies)
    avg_sprd = sum(x["avg_sprd"] for x in dailies) / len(dailies)
    med_trdsz = sum(x["med_trdsz"] for x in dailies) / len(dailies)
    med_inside_liq = sum(x["med_inside_liq"] for x in dailies) / len(dailies)
    notional = total_vol_shs * avg_mid
    out = {
        "symbol": symbol,
        "window": f"{start}-{end} (weekdays)",
        "days": n_days,
        "days_with_data": len(dailies),
        "spread_bps_avg": spread_bps,
        "avg_sprd_abs": avg_sprd,
        "avg_mid": avg_mid,
        "num_trades": total_trades,
        "vol_shs": total_vol_shs,
        "vol_notional": notional,
        "vol_notional_per_day": notional / n_days,
        "med_trdsz": med_trdsz,
        "med_inside_liq": med_inside_liq,
        "n_midchanges": total_midchanges,
        "n_midchanges_per_day": total_midchanges / n_days,
    }
    if not skip_returner:
        all_mids = []
        for d in days:
            try:
                p = run_returner(symbol, d, force=force)
                all_mids.extend(load_mids(p))
            except subprocess.CalledProcessError as e:
                print(f"WARN: returner failed for {symbol} {d}: {e}", file=sys.stderr)
        rets = resampled_returns(all_mids, period_s)
        sd = stdev(rets)
        out["ret_period_s"] = period_s
        out["n_returns"] = len(rets)
        out["ret_stdev"] = sd
        out["ret_stdev_bps"] = sd * 1e4
        # Weekday-only annualization: 252 * 86400 s/yr
        periods_per_year = 252 * 86400 / period_s
        out["ret_stdev_annualized"] = sd * math.sqrt(periods_per_year)
    return out


def fmt(v):
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f"{v:,.2f}"
        return f"{v:.4f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def print_window(label, w):
    print(f"\n=== {label} ===")
    for k, v in w.items():
        print(f"  {k:<25} {fmt(v)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True, help="e.g. xyz:MRVL")
    p.add_argument("--baseline", required=True, help="YYYYMMDD:YYYYMMDD")
    p.add_argument("--bad", required=True, help="YYYYMMDD:YYYYMMDD")
    p.add_argument("--period-s", type=int, default=300, help="Return resampling period (default 300=5min)")
    p.add_argument("--skip-returner", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--weekdays-only", action="store_true",
                   help="Iterate per-weekday and sum instead of one aggregate symstats call. "
                        "Excludes weekend HL activity which strategies don't see. Slower "
                        "(one symstats call per weekday) but avoids weekend bias in per-day averages.")
    args = p.parse_args()

    b_start, b_end = args.baseline.split(":")
    x_start, x_end = args.bad.split(":")

    base = compute_window(args.symbol, b_start, b_end, args.period_s,
                          args.skip_returner, args.force, args.weekdays_only)
    bad = compute_window(args.symbol, x_start, x_end, args.period_s,
                         args.skip_returner, args.force, args.weekdays_only)

    print_window(f"BASELINE {args.baseline}", base)
    print_window(f"BAD {args.bad}", bad)

    print("\n=== DELTA (bad / baseline) ===")
    for k in base:
        try:
            if isinstance(base[k], str):
                continue
            if base[k] == 0:
                ratio = float("inf")
            else:
                ratio = bad[k] / base[k]
            print(f"  {k:<25} {ratio:.3f}x")
        except (TypeError, ZeroDivisionError):
            pass


if __name__ == "__main__":
    main()
