#!/usr/bin/env python3
"""
HIP3 pairwise beta/R^2 grids — one-minute mid returns via bin/returner.

Runs `bin/returner` (Hyperliquid-only 1-min mid sampler) across every HIP3
symbol over N weekdays, aligns timestamps, and computes pairwise OLS beta
and R^2 over three time-of-day windows (all in America/New_York):

  - all_day:  00:00-24:00
  - us_day:   09:30-16:00
  - asia:     19:00-02:00 (next day)

For symbols S (row) and R (column):
  beta  = cov(S, R) / var(R)   — i.e. regression slope when S = a + b*R
  r2    = cor(S, R) ** 2

Usage
─────
  # Full run (all HIP3 symbols, 14 weekdays ending day-before-yesterday)
  hip3_corr.py

  # Smoke test on a subset
  hip3_corr.py --symbols SMSN,SKHX,HYUNDAI,EWY,NQ,BTC

  # Fewer days / custom output
  hip3_corr.py --days 7 --out-dir ~/scratch/my_corr/

Outputs
───────
  ~/scratch/hip3_corr/YYYYMMDD/
      beta_all_day.csv  r2_all_day.csv
      beta_us_day.csv   r2_us_day.csv
      beta_asia.csv     r2_asia.csv
      coverage.csv      — per-symbol count of minutes per period

Symbols without a leading "xyz:" or other exchange prefix are passed to
returner verbatim (so BTC, NQ resolve to Hyperliquid perps; xyz:SMSN to
the HIP3 equity).
"""

import argparse
import concurrent.futures as cf
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
_RETRADED_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
_TRADEMAN_DIR = os.path.join(_SCRIPT_DIR, "trademan")
if _TRADEMAN_DIR not in sys.path:
    sys.path.insert(0, _TRADEMAN_DIR)

RETURNER_BIN = os.path.join(_RETRADED_ROOT, "bin", "returner")
MD_EXISTS = os.path.join(_SCRIPT_DIR, "md_exists.py")

DEFAULT_CACHE = os.path.expanduser("~/scratch/hip3_corr/cache")
DEFAULT_OUT_ROOT = os.path.expanduser("~/scratch/hip3_corr")

# (name, start_hhmm_et, end_hhmm_et, crosses_midnight)
PERIODS = [
    ("all_day", (0, 0), (24, 0), False),
    ("us_day", (9, 30), (16, 0), False),
    ("asia", (19, 0), (2, 0), True),
]


def _weekday_dates(n, skip_last=1):
    """Return the N most recent weekdays (YYYYMMDD), ending `skip_last` days ago.

    skip_last=1 means exclude today; data for the previous day usually isn't
    fully available yet.
    """
    out = []
    d = date.today() - timedelta(days=skip_last)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return list(reversed(out))


def _cache_path(cache_dir, sym, date_str):
    safe = sym.replace(":", "_")
    return os.path.join(cache_dir, safe, f"{date_str}.csv")


def _fetch_one(sym, date_str, cache_dir, market, force):
    """Ensure market data exists and run returner for one (symbol, date).

    Writes to cache. Returns path if successful, None otherwise.
    """
    out_path = _cache_path(cache_dir, sym, date_str)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0 and not force:
        return out_path

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    try:
        subprocess.run(
            [sys.executable, MD_EXISTS,
             "--sym", sym, "--market", market,
             "--start", date_str, "--end", date_str],
            input="y\n", text=True, timeout=600,
            capture_output=True,
        )
    except Exception as e:
        print(f"  [warn] md_exists failed for {sym} {date_str}: {e}", file=sys.stderr)
        return None

    try:
        subprocess.run(
            [RETURNER_BIN, "--symbol", sym, "--date", date_str, "--out", out_path],
            timeout=300, capture_output=True,
        )
    except Exception as e:
        print(f"  [warn] returner failed for {sym} {date_str}: {e}", file=sys.stderr)
        return None

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return None
    return out_path


def _load_prices(cache_file, sym):
    """Load returner CSV into a price series indexed by ET timestamp.

    Returner columns (no header): time_str, time_s (unix utc seconds), px, ret.
    We keep the mid price (px); returns are derived after resampling.
    """
    try:
        df = pd.read_csv(cache_file, header=None,
                         names=["time_str", "time_s", "px", "ret"])
    except Exception:
        return None
    if df.empty:
        return None
    ts_utc = pd.to_datetime(df["time_s"], unit="s", utc=True)
    ts_et = ts_utc.dt.tz_convert("America/New_York")
    out = pd.DataFrame({sym: df["px"].values}, index=ts_et)
    out = out[~out.index.duplicated(keep="first")]
    return out


def _prices_to_returns(prices_df, freq):
    """Resample last-price per bucket, then convert to pct-change returns.

    freq: a pandas offset string like "1min", "5min", "15min".
    Returns a DataFrame of returns aligned to bucket end-times.
    """
    resampled = prices_df.resample(freq).last()
    return resampled.pct_change()


def _filter_period(df, start_hhmm, end_hhmm, crosses_midnight):
    """Keep rows whose ET wall-clock time falls inside [start, end) per period."""
    sh, sm = start_hhmm
    eh, em = end_hhmm
    mins = df.index.hour * 60 + df.index.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if crosses_midnight:
        mask = (mins >= start_m) | (mins < end_m)
    elif end_m == 24 * 60:
        mask = np.ones(len(df), dtype=bool)
    else:
        mask = (mins >= start_m) & (mins < end_m)
    return df[mask]


def _pairwise_beta_r2(returns_df):
    """Compute pairwise OLS beta and R^2 for all columns.

    Skips any column with < 30 non-null rows. Each pair (S, R) uses only
    rows where both S and R are non-null.

    Returns (beta_df, r2_df, count_series).
    """
    cols = list(returns_df.columns)
    n = len(cols)
    beta = np.full((n, n), np.nan)
    r2 = np.full((n, n), np.nan)
    count = {}

    arr = returns_df.values
    is_valid = ~np.isnan(arr)

    col_valid_count = is_valid.sum(axis=0)
    for j, c in enumerate(cols):
        count[c] = int(col_valid_count[j])

    for i in range(n):
        for j in range(n):
            mask = is_valid[:, i] & is_valid[:, j]
            if mask.sum() < 30:
                continue
            s = arr[mask, i]
            r = arr[mask, j]
            var_r = r.var()
            if var_r == 0:
                continue
            cov_sr = ((s - s.mean()) * (r - r.mean())).mean()
            b = cov_sr / var_r
            var_s = s.var()
            r2_val = (cov_sr ** 2) / (var_r * var_s) if var_s > 0 else np.nan
            beta[i, j] = b
            r2[i, j] = r2_val

    beta_df = pd.DataFrame(beta, index=cols, columns=cols)
    r2_df = pd.DataFrame(r2, index=cols, columns=cols)
    count_ser = pd.Series(count, name="minutes")
    return beta_df, r2_df, count_ser


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", type=str, default=None,
                    help="Comma-separated symbol list. Default: all HIP3 (xyz:*) symbols.")
    ap.add_argument("--days", type=int, default=14,
                    help="Number of weekdays to include (default 14).")
    ap.add_argument("--skip-last", type=int, default=1,
                    help="Skip this many most-recent days (default 1 — yesterday's data often not ready).")
    ap.add_argument("--market", type=str, default="Hyperliquid",
                    help="Market arg for md_exists (default Hyperliquid).")
    ap.add_argument("--out-dir", type=str, default=None,
                    help="Output dir (default ~/scratch/hip3_corr/YYYYMMDD/).")
    ap.add_argument("--cache-dir", type=str, default=DEFAULT_CACHE,
                    help=f"Returner output cache (default {DEFAULT_CACHE}).")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel workers for md_exists+returner (default 8).")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even if cache files exist.")
    ap.add_argument("--resample", type=str, default="1min",
                    help="Comma-separated resample frequencies, e.g. '1min,5min,15min'. "
                         "Each frequency produces its own beta_/r2_ CSV set.")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        from coverage_report import fetch_all_xyz_symbols
        print("Fetching HIP3 symbol list…")
        symbols = sorted(fetch_all_xyz_symbols())
        if not symbols:
            sys.exit("Could not fetch HIP3 symbols.")

    dates = _weekday_dates(args.days, skip_last=args.skip_last)
    print(f"Symbols ({len(symbols)}): {', '.join(symbols)}")
    print(f"Dates ({len(dates)}): {dates[0]} → {dates[-1]}")

    # Fan out fetches
    tasks = [(s, d) for s in symbols for d in dates]
    print(f"\nFetching returner data — {len(tasks)} (sym, date) tasks, {args.workers} workers…")
    cache_paths = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        fut_to_key = {
            ex.submit(_fetch_one, s, d, args.cache_dir, args.market, args.force): (s, d)
            for s, d in tasks
        }
        done = 0
        for fut in cf.as_completed(fut_to_key):
            s, d = fut_to_key[fut]
            try:
                path = fut.result()
            except Exception as e:
                print(f"  [err] {s} {d}: {e}", file=sys.stderr)
                path = None
            cache_paths[(s, d)] = path
            done += 1
            if done % 25 == 0 or done == len(tasks):
                missing = sum(1 for p in cache_paths.values() if not p)
                print(f"  {done}/{len(tasks)} done, {missing} missing")

    # Assemble per-symbol price series across all dates
    per_sym_frames = {}
    for s in symbols:
        parts = []
        for d in dates:
            p = cache_paths.get((s, d))
            if not p:
                continue
            df = _load_prices(p, s)
            if df is not None and not df.empty:
                parts.append(df)
        if parts:
            per_sym_frames[s] = pd.concat(parts).sort_index()
        else:
            print(f"  [warn] no data for {s}; dropping")

    kept = list(per_sym_frames.keys())
    if len(kept) < 2:
        sys.exit(f"Only {len(kept)} symbols with data — need at least 2.")

    # Outer-join all symbol prices on timestamp
    all_px = pd.concat(per_sym_frames.values(), axis=1)
    all_px.columns = kept

    if args.out_dir:
        out_dir = os.path.expanduser(args.out_dir)
    else:
        today = date.today().strftime("%Y%m%d")
        out_dir = os.path.join(DEFAULT_OUT_ROOT, today)
    os.makedirs(out_dir, exist_ok=True)

    freqs = [f.strip() for f in args.resample.split(",") if f.strip()]
    coverage_rows = []

    for freq in freqs:
        print(f"\n── Resample: {freq} ──")
        returns_df = _prices_to_returns(all_px, freq)
        for name, start, end, crosses in PERIODS:
            sub = _filter_period(returns_df, start, end, crosses)
            beta_df, r2_df, count_ser = _pairwise_beta_r2(sub)
            beta_df.round(6).to_csv(os.path.join(out_dir, f"beta_{name}_{freq}.csv"))
            r2_df.round(6).to_csv(os.path.join(out_dir, f"r2_{name}_{freq}.csv"))
            for sym, cnt in count_ser.items():
                coverage_rows.append({"freq": freq, "period": name, "symbol": sym, "bars": cnt})
            print(f"  {name}: {len(sub)} bars across {len(kept)} symbols")

    cov_df = pd.DataFrame(coverage_rows)
    cov_df.to_csv(os.path.join(out_dir, "coverage.csv"), index=False)

    print(f"\nWrote: {out_dir}")
    for freq in freqs:
        for name, _, _, _ in PERIODS:
            print(f"  beta_{name}_{freq}.csv  r2_{name}_{freq}.csv")
    print(f"  coverage.csv")


if __name__ == "__main__":
    main()
