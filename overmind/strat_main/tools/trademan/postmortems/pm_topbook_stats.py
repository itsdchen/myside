#!/usr/bin/env python
"""Topbook (US equity) spread / volume / intraday-vol via Databento.

Pulls ohlcv-1m (volume, close px → 5-min returns) and cbbo-1s (spread).
Caches per-(symbol,window) results to ~/scratch/postmortems/_topbook_cache/.
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import databento as db
import pytz

DB_PX_FACTOR = 1_000_000_000
DB_SECRET_FILE = Path(os.path.expanduser("~/.creds/.DataBento.creds.json"))
CACHE = Path(os.path.expanduser("~/scratch/postmortems/_topbook_cache"))
ET = pytz.timezone("America/New_York")

# US equity session for vol/spread: regular hours 09:30 – 16:00 ET
SESS_START_H, SESS_START_M = 9, 30
SESS_END_H, SESS_END_M = 16, 0


def load_api_key():
    with DB_SECRET_FILE.open() as f:
        return json.load(f)["api_key"]


def daterange(start: str, end: str):
    d = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    while d <= e:
        yield d
        d += timedelta(days=1)


def cache_path(symbol: str, start: str, end: str, dataset: str) -> Path:
    sym_safe = symbol.replace(":", "_").replace("/", "_")
    return CACHE / f"{sym_safe}_{dataset}_{start}_{end}.json"


def fetch_ohlcv_returns(client, dataset: str, symbol: str, start: str, end: str):
    """Fetch 1-min OHLCV closes per session day and return:
       total_volume, total_notional, list of 5-min log returns."""
    closes_by_day = {}
    total_vol = 0
    total_notional = 0.0
    days_with_data = 0

    for d in daterange(start, end):
        day_str = d.strftime("%Y-%m-%d")
        s_dt = ET.localize(datetime.combine(d.date(), datetime.strptime(f"{SESS_START_H:02d}:{SESS_START_M:02d}", "%H:%M").time()))
        e_dt = ET.localize(datetime.combine(d.date(), datetime.strptime(f"{SESS_END_H:02d}:{SESS_END_M:02d}", "%H:%M").time()))
        try:
            data = client.timeseries.get_range(
                dataset=dataset,
                schema="ohlcv-1m",
                stype_in="raw_symbol",
                symbols=[symbol],
                start=s_dt.isoformat(),
                end=e_dt.isoformat(),
            )
        except Exception as e:
            print(f"WARN: ohlcv fetch failed for {symbol} {day_str}: {e}", file=sys.stderr)
            continue
        day_closes = []
        for msg in data:
            ts_ms = int(msg.ts_event / 1_000_000)
            close = float(msg.close) / DB_PX_FACTOR
            vol = int(msg.volume)
            if close <= 0:
                continue
            day_closes.append((ts_ms, close))
            total_vol += vol
            total_notional += vol * close
        if day_closes:
            days_with_data += 1
            day_closes.sort()
            closes_by_day[day_str] = day_closes

    # Compute 5-min log returns, intra-day only (no overnight gaps).
    returns_5m = []
    for day_str, closes in closes_by_day.items():
        # Take every 5th minute close. The 1-min bars are at minute boundaries.
        # Bucket by floor(ts_ms / (5*60*1000)) to align to 5-min grid.
        by_bucket = {}
        for ts_ms, px in closes:
            b = ts_ms // (5 * 60 * 1000)
            by_bucket[b] = px  # last write wins; minutes 0,1,2,3,4 in bucket b
        buckets = sorted(by_bucket.items())
        for i in range(1, len(buckets)):
            b0, p0 = buckets[i - 1]
            b1, p1 = buckets[i]
            if b1 - b0 != 1:  # gap, skip
                continue
            if p0 > 0 and p1 > 0:
                returns_5m.append(math.log(p1 / p0))

    return total_vol, total_notional, returns_5m, days_with_data


def fetch_spread(client, dataset: str, schema_bbo: str, symbol: str, start: str, end: str):
    """Fetch cbbo-1s for the session window each day, compute avg spread bps."""
    spreads_bps_all = []
    for d in daterange(start, end):
        s_dt = ET.localize(datetime.combine(d.date(), datetime.strptime(f"{SESS_START_H:02d}:{SESS_START_M:02d}", "%H:%M").time()))
        e_dt = ET.localize(datetime.combine(d.date(), datetime.strptime(f"{SESS_END_H:02d}:{SESS_END_M:02d}", "%H:%M").time()))
        try:
            data = client.timeseries.get_range(
                dataset=dataset,
                schema=schema_bbo,
                stype_in="raw_symbol",
                symbols=[symbol],
                start=s_dt.isoformat(),
                end=e_dt.isoformat(),
            )
        except Exception as e:
            print(f"WARN: bbo fetch failed for {symbol} {d}: {e}", file=sys.stderr)
            continue
        for msg in data:
            bid = ask = None
            if hasattr(msg, "levels") and len(msg.levels) > 0:
                lev = msg.levels[0]
                bid = float(lev.bid_px) / DB_PX_FACTOR if lev.bid_px else None
                ask = float(lev.ask_px) / DB_PX_FACTOR if lev.ask_px else None
            elif hasattr(msg, "bid_px") and hasattr(msg, "ask_px"):
                bid = float(msg.bid_px) / DB_PX_FACTOR if msg.bid_px else None
                ask = float(msg.ask_px) / DB_PX_FACTOR if msg.ask_px else None
            if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
                continue
            mid = (bid + ask) / 2
            if mid <= 0:
                continue
            spr = ask - bid
            if spr / mid > 0.10:  # filter wide/stale
                continue
            spreads_bps_all.append(spr / mid * 1e4)
    return spreads_bps_all


def stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def annualize_5m(stdev_5m):
    # US equity intraday: 78 5-min bars per session, ~252 sessions/yr
    return stdev_5m * math.sqrt(78 * 252)


def compute_window(symbol: str, start: str, end: str, dataset: str = "XNAS.BASIC",
                   skip_spread: bool = False, force: bool = False):
    cache_p = cache_path(symbol, start, end, dataset)
    if cache_p.exists() and not force:
        with cache_p.open() as f:
            return json.load(f)

    api_key = load_api_key()
    client = db.Historical(key=api_key)
    schema_bbo = "cbbo-1s" if dataset == "XNAS.BASIC" else "bbo-1s"

    print(f"Fetching ohlcv for {symbol} {start}-{end} ({dataset})...", file=sys.stderr)
    total_vol, total_notional, rets_5m, days_with_data = fetch_ohlcv_returns(
        client, dataset, symbol, start, end)

    avg_spread_bps = None
    n_spread = 0
    if not skip_spread:
        print(f"Fetching cbbo for {symbol} {start}-{end} ({dataset})...", file=sys.stderr)
        spreads = fetch_spread(client, dataset, schema_bbo, symbol, start, end)
        n_spread = len(spreads)
        if spreads:
            avg_spread_bps = sum(spreads) / n_spread

    sd_5m = stdev(rets_5m)
    out = {
        "symbol": symbol,
        "window": f"{start}-{end}",
        "dataset": dataset,
        "days_with_data": days_with_data,
        "total_volume_shs": total_vol,
        "total_notional": total_notional,
        "vol_shs_per_day": total_vol / max(days_with_data, 1),
        "notional_per_day": total_notional / max(days_with_data, 1),
        "avg_spread_bps": avg_spread_bps,
        "n_spread_samples": n_spread,
        "n_returns_5m": len(rets_5m),
        "ret_5m_stdev": sd_5m,
        "ret_5m_stdev_bps": sd_5m * 1e4,
        "ret_stdev_annualized": annualize_5m(sd_5m),
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    with cache_p.open("w") as f:
        json.dump(out, f, indent=2)
    return out


def fmt(v):
    if v is None:
        return "—"
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
    p.add_argument("--symbol", required=True, help="Equity ticker, e.g. MRVL")
    p.add_argument("--baseline", required=True, help="YYYYMMDD:YYYYMMDD")
    p.add_argument("--bad", required=True, help="YYYYMMDD:YYYYMMDD")
    p.add_argument("--dataset", default="XNAS.BASIC", choices=["XNAS.BASIC", "EQUS.MINI"])
    p.add_argument("--skip-spread", action="store_true", help="Skip cbbo (cheaper)")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    b_start, b_end = args.baseline.split(":")
    x_start, x_end = args.bad.split(":")
    base = compute_window(args.symbol, b_start, b_end, args.dataset, args.skip_spread, args.force)
    bad = compute_window(args.symbol, x_start, x_end, args.dataset, args.skip_spread, args.force)

    print_window(f"BASELINE {args.baseline}", base)
    print_window(f"BAD {args.bad}", bad)

    print("\n=== DELTA (bad / baseline) ===")
    for k in base:
        if isinstance(base[k], str) or base[k] is None:
            continue
        try:
            if base[k] == 0:
                ratio = float("inf")
            else:
                ratio = bad[k] / base[k]
            print(f"  {k:<25} {ratio:.3f}x")
        except (TypeError, ZeroDivisionError):
            pass


if __name__ == "__main__":
    main()
