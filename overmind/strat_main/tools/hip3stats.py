#! /usr/bin/env python

"""
hip3stats.py - Hyperliquid xyz Market Stats Tracker

Computes per-symbol, per-day market stats (spread, volume, volatility) for all
xyz perp symbols and publishes to market_stats.json for the GitHub Pages dashboard.

Data sources:
  1. Historical days with gzpbf data: runs the symstats C++ binary for precise
     time-weighted spread, volume, and mid-price stats.
  2. Completed days without histdata: uses Hyperliquid REST API (daily + 5-min
     candles for volume/volatility, cached once finalized).
  3. Today (live): accumulates data across runs. Each cycle fetches new 5-min
     candles (for volatility), an l2Book snapshot (for spread), and the daily
     candle (for volume/trades). State persists in a "today file" that
     finalizes on day rollover into the permanent cache.

USAGE
=====

    python hip3stats.py                  # Run continuously (default 10 min interval)
    python hip3stats.py --once           # Run once and exit
    python hip3stats.py --interval 300   # Custom interval in seconds
    python hip3stats.py --days 14        # Look back 14 days instead of default 10
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(script_dir, "../util"))

import chron
import pathing


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
DATA_DIR = os.path.expanduser("~/tardis_datasets/gzpbf")
CACHE_FILE = os.path.expanduser("~/tradefi/itsdchen.github.io/market_stats_cache.json")
TODAY_FILE = os.path.expanduser("~/tradefi/itsdchen.github.io/market_stats_today.json")
OUTPUT_FILE = os.path.expanduser("~/tradefi/itsdchen.github.io/market_stats.json")
GIT_DIR = os.path.expanduser("~/tradefi/itsdchen.github.io")
SYMSTATS_BIN = os.path.join(pathing.bindir(), "symstats")

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Hyperliquid API helpers
# ---------------------------------------------------------------------------

def post_info(payload, timeout=15):
    """POST to Hyperliquid info endpoint with simple 429 backoff."""
    resp = requests.post(HYPERLIQUID_INFO_URL, json=payload, timeout=timeout)
    if resp.status_code == 429:
        print("Rate limited (429), backing off 60s...")
        time.sleep(60)
        resp = requests.post(HYPERLIQUID_INFO_URL, json=payload, timeout=timeout)
    return resp


def fetch_all_mids():
    """Fetch allMids for xyz dex. Returns dict of symbol -> mid price float."""
    try:
        resp = post_info({"type": "allMids", "dex": "xyz"})
        if resp.status_code == 200:
            return {sym: float(px) for sym, px in resp.json().items()}
    except Exception as e:
        print(f"Error fetching allMids: {e}")
    return {}


def fetch_candles(sym, interval, start_ms, end_ms):
    """Fetch candles for a symbol at given interval. Returns list of candle dicts."""
    try:
        resp = post_info({
            "type": "candleSnapshot",
            "req": {"coin": sym, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        })
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"Error fetching {interval} candles for {sym}: {e}")
    return []


def fetch_l2_book(sym):
    """Fetch L2 orderbook snapshot. Returns best bid/ask spread info."""
    try:
        resp = post_info({"type": "l2Book", "coin": sym, "dex": "xyz"})
        if resp.status_code == 200:
            data = resp.json()
            levels = data.get("levels", [[], []])
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []
            if bids and asks:
                bb = float(bids[0]["px"])
                ba = float(asks[0]["px"])
                mid = (bb + ba) / 2.0
                spread = ba - bb
                spread_bps = (spread / mid) * 10000 if mid > 0 else 0
                return {"spread": spread, "mid": mid, "spread_bps": spread_bps}
    except Exception as e:
        print(f"Error fetching l2Book for {sym}: {e}")
    return None


def candle_to_utc_date(candle):
    """Convert candle timestamp (ms) to YYYYMMDD in UTC."""
    t = candle["t"] / 1000
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 5-min candle volatility: avg range in bps
# ---------------------------------------------------------------------------

def compute_candle_range_bps(candle):
    """Compute (high - low) / mid * 10000 for a single candle."""
    h = float(candle["h"])
    l = float(candle["l"])
    if h <= 0 or l <= 0:
        return None
    mid = (h + l) / 2.0
    if mid == 0:
        return None
    return (h - l) / mid * 10000


def compute_avg_range_bps(candles_5m):
    """Average 5-min candle range in bps across a list of candles."""
    ranges = []
    for c in candles_5m:
        r = compute_candle_range_bps(c)
        if r is not None:
            ranges.append(r)
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


def fetch_5m_volatility_for_day(sym, date_str):
    """Fetch all 5-min candles for a given UTC day and compute avg range bps."""
    day_start = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = int(day_end.timestamp() * 1000)
    candles = fetch_candles(sym, "5m", start_ms, end_ms)
    if not candles:
        return None
    return compute_avg_range_bps(candles)


# ---------------------------------------------------------------------------
# symstats runner (historical days)
# ---------------------------------------------------------------------------

def run_symstats(sym, date_str):
    """Run the symstats C++ binary for a symbol on a given date.

    Returns dict with stats, or None on failure.
    """
    cmd = [
        SYMSTATS_BIN,
        "--book", "Hyperliquid",
        "--symbol", sym,
        "--date", date_str,
        "--csv",
        "--start-time", "00:00:00 America/New_York",
        "--end-time", "23:59:00 America/New_York",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return None

        lines = result.stdout.strip().split("\n")
        if len(lines) < 2:
            return None

        headers = lines[0].split(",")
        values = lines[1].split(",")
        row = dict(zip(headers, values))

        num_trades = int(row.get("num_trades", 0))
        vol_shares = float(row.get("vol", 0))
        avg_mid = float(row.get("avg_mid", 0))
        spread_pct = float(row.get("spread_pct", 0))
        med_trdsz = float(row.get("med_trdsz", 0))
        med_inside = float(row.get("med_inside_liq", 0))
        n_midchanges = int(row.get("n_midchanges", 0))

        if math.isnan(avg_mid) or avg_mid == 0:
            return None

        vol_notional = vol_shares * avg_mid
        spread_bps = spread_pct * 10000

        return {
            "source": "symstats",
            "num_trades": num_trades,
            "volume_ntl": round(vol_notional, 2),
            "avg_mid": round(avg_mid, 4),
            "spread_bps": round(spread_bps, 2),
            "med_trade_sz": round(med_trdsz, 4),
            "med_inside_liq": round(med_inside, 4),
            "n_midchanges": n_midchanges,
        }
    except subprocess.TimeoutExpired:
        print(f"  symstats timed out for {sym} {date_str}")
        return None
    except Exception as e:
        print(f"  symstats error for {sym} {date_str}: {e}")
        return None


def dates_with_histdata(sym, date_list):
    """Return set of dates that have both l2Book and trades gzpbf files."""
    good = set()
    sym_file = sym.replace("/", "_")
    for d in date_list:
        l2_path = os.path.join(DATA_DIR, "Hyperliquid", f"{sym_file}_l2Book_{d}.gzpbf")
        trades_path = os.path.join(DATA_DIR, "Hyperliquid", f"{sym_file}_trades_{d}.gzpbf")
        if os.path.exists(l2_path) and os.path.exists(trades_path):
            good.add(d)
    return good


# ---------------------------------------------------------------------------
# Completed-day stats from API (for days without histdata, not today)
# ---------------------------------------------------------------------------

def compute_completed_day_stats(sym, date_str):
    """Compute stats for a completed (non-today) day using API candles.

    Fetches the daily candle for volume/trades and 5-min candles for volatility.
    """
    # Daily candle for volume/trades
    day_start = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = int(day_end.timestamp() * 1000)

    daily_candles = fetch_candles(sym, "1d", start_ms, end_ms)
    if not daily_candles:
        return None

    c = daily_candles[0]
    o, h, l, close = float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
    vol_shares = float(c["v"])
    n_trades = int(c["n"])
    avg_px = (o + h + l + close) / 4.0

    if avg_px == 0:
        return None

    vol_notional = vol_shares * avg_px

    # 5-min candle volatility
    vol_bps = fetch_5m_volatility_for_day(sym, date_str)

    stat = {
        "source": "api",
        "num_trades": n_trades,
        "volume_ntl": round(vol_notional, 2),
        "avg_mid": round(avg_px, 4),
    }
    if vol_bps is not None:
        stat["volatility_bps"] = round(vol_bps, 2)

    return stat


# ---------------------------------------------------------------------------
# Today's intraday accumulation
# ---------------------------------------------------------------------------

def load_today_state():
    """Load the today accumulation file."""
    try:
        with open(TODAY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_today_state(state):
    """Save the today accumulation file."""
    with open(TODAY_FILE, "w") as f:
        json.dump(state, f, indent=1)


def update_today_for_symbol(sym, today_state):
    """Fetch new intraday data for a symbol and update today_state in place.

    today_state is the per-symbol dict containing:
      - candle_ranges: list of 5-min candle range values (bps)
      - last_candle_t: timestamp (ms) of last processed 5-min candle
      - spread_samples: list of [timestamp_s, spread_bps]
      - latest_daily: {volume_ntl, num_trades, avg_mid} from daily candle
    """
    now_ms = int(time.time() * 1000)
    today_utc = datetime.now(timezone.utc).strftime("%Y%m%d")

    # If UTC day has rolled over since this cycle started, skip the update.
    # Otherwise late-processed symbols would overwrite latest_daily with the
    # new day's near-zero volume, and the next cycle's finalize_yesterday
    # would cache that as the old day's total.
    state_date = today_state.get("_date")
    if state_date and state_date != today_utc:
        return 0

    day_start = datetime.strptime(today_utc, "%Y%m%d").replace(tzinfo=timezone.utc)
    day_start_ms = int(day_start.timestamp() * 1000)

    if sym not in today_state:
        today_state[sym] = {
            "candle_ranges": [],
            "last_candle_t": day_start_ms,
            "spread_samples": [],
            "latest_daily": None,
        }

    ts = today_state[sym]

    # 1. Fetch new 5-min candles since last processed
    new_candles = fetch_candles(sym, "5m", ts["last_candle_t"], now_ms)
    new_count = 0
    for c in new_candles:
        ct = c["t"]
        # Skip candles we've already processed (at or before last_candle_t)
        if ct <= ts["last_candle_t"]:
            continue
        # Skip the currently-forming candle (its end time is in the future)
        if c["T"] > now_ms:
            continue
        r = compute_candle_range_bps(c)
        if r is not None:
            ts["candle_ranges"].append(round(r, 2))
            new_count += 1
        ts["last_candle_t"] = ct

    # 2. Take a spread snapshot
    spread_info = fetch_l2_book(sym)
    if spread_info:
        ts["spread_samples"].append([
            round(time.time()),
            round(spread_info["spread_bps"], 2),
        ])

    # 3. Fetch latest daily candle for volume/trades
    daily_candles = fetch_candles(sym, "1d", day_start_ms, now_ms)
    if daily_candles:
        # Take the candle matching today
        for c in daily_candles:
            if candle_to_utc_date(c) == today_utc:
                o, h, l, close = float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
                avg_px = (o + h + l + close) / 4.0
                ts["latest_daily"] = {
                    "volume_ntl": round(float(c["v"]) * avg_px, 2),
                    "num_trades": int(c["n"]),
                    "avg_mid": round(avg_px, 4),
                }
                break

    return new_count


def summarize_today(today_state, sym):
    """Produce a display-ready stats dict from today's accumulated state."""
    ts = today_state.get(sym)
    if not ts:
        return None

    stat = {"source": "live"}

    # Volume/trades from daily candle
    if ts.get("latest_daily"):
        stat["volume_ntl"] = ts["latest_daily"]["volume_ntl"]
        stat["num_trades"] = ts["latest_daily"]["num_trades"]
        stat["avg_mid"] = ts["latest_daily"]["avg_mid"]

    # Volatility: average of accumulated 5-min candle ranges
    if ts.get("candle_ranges"):
        stat["volatility_bps"] = round(
            sum(ts["candle_ranges"]) / len(ts["candle_ranges"]), 2
        )

    # Spread: time-weighted average of snapshots
    samples = ts.get("spread_samples", [])
    if len(samples) >= 2:
        # Time-weight: each sample's spread is weighted by time until next sample
        weighted_sum = 0.0
        total_time = 0.0
        for i in range(len(samples) - 1):
            dt = samples[i + 1][0] - samples[i][0]
            if dt > 0:
                weighted_sum += samples[i][1] * dt
                total_time += dt
        if total_time > 0:
            stat["spread_bps"] = round(weighted_sum / total_time, 2)
    elif len(samples) == 1:
        # Only one sample so far, use it directly
        stat["spread_bps"] = samples[0][1]

    return stat if ("volume_ntl" in stat or "spread_bps" in stat) else None


def finalize_yesterday(today_state, cache):
    """If today_state is from a previous day, finalize it into cache and reset."""
    if not today_state:
        return today_state, False

    state_date = today_state.get("_date")
    today_utc = datetime.now(timezone.utc).strftime("%Y%m%d")

    if not state_date or state_date == today_utc:
        return today_state, False

    # Day rolled over. Finalize yesterday's data into cache.
    print(f"Day rollover: finalizing {state_date} -> cache")
    for sym, ts in today_state.items():
        if sym.startswith("_"):
            continue
        summary = summarize_today(today_state, sym)
        if summary:
            summary["source"] = "live_final"
            if sym not in cache:
                cache[sym] = {}
            cache[sym][state_date] = summary
            print(f"  {sym}: cached for {state_date}")

    # Return fresh state for today
    return {"_date": today_utc}, True


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=1)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def discover_symbols():
    """Discover xyz symbols from allMids API."""
    mids = fetch_all_mids()
    if mids:
        syms = sorted(mids.keys())
        print(f"Discovered {len(syms)} xyz symbols from API")
        return syms

    # Fallback: scan histdata directory
    seen = set()
    hdir = os.path.join(DATA_DIR, "Hyperliquid")
    if os.path.isdir(hdir):
        for fname in os.listdir(hdir):
            if fname.startswith("xyz:") and "_l2Book_" in fname:
                sym = fname.split("_l2Book_")[0]
                seen.add(sym)
    syms = sorted(seen)
    print(f"Discovered {len(syms)} xyz symbols from histdata directory")
    return syms


def get_date_range(num_days):
    """Return list of date strings for the last num_days days, including today."""
    et_now = datetime.now(ET)
    dates = []
    for i in range(num_days - 1, -1, -1):
        d = et_now - timedelta(days=i)
        dates.append(d.strftime("%Y%m%d"))
    return dates


def compute_all_stats(symbols, date_range):
    """Compute stats for all symbols across date_range.

    Historical days: symstats (if histdata) or API candles (cached).
    Today: accumulated intraday data from today_state.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    cache = load_cache()
    today_state = load_today_state()

    # Handle day rollover
    today_state, rolled = finalize_yesterday(today_state, cache)
    if rolled:
        save_cache(cache)

    # Ensure today_state has date marker
    today_state["_date"] = today_str

    stats = {}  # sym -> {date -> stats_dict}

    for i, sym in enumerate(symbols):
        print(f"[{i+1}/{len(symbols)}] {sym}...")
        sym_stats = {}
        cache_key = sym
        if cache_key not in cache:
            cache[cache_key] = {}

        hist_dates = dates_with_histdata(sym, date_range)

        for d in date_range:
            if d == today_str:
                # Today: update intraday accumulation
                new_candles = update_today_for_symbol(sym, today_state)
                summary = summarize_today(today_state, sym)
                if summary:
                    sym_stats[d] = summary
                    n_samples = len(today_state.get(sym, {}).get("candle_ranges", []))
                    n_spreads = len(today_state.get(sym, {}).get("spread_samples", []))
                    vol_str = f"vol={summary.get('volatility_bps', '?')}bps" if 'volatility_bps' in summary else "vol=?"
                    sprd_str = f"sprd={summary.get('spread_bps', '?')}bps" if 'spread_bps' in summary else "sprd=?"
                    print(f"  {d}: LIVE - {vol_str}, {sprd_str}, {n_samples} candles, {n_spreads} spread samples (+{new_candles} new)")
                continue

            # Check cache first
            if d in cache.get(cache_key, {}):
                sym_stats[d] = cache[cache_key][d]
                continue

            # Try symstats for histdata days
            if d in hist_dates:
                result = run_symstats(sym, d)
                if result:
                    # Also fetch 5-min volatility to fill the gap
                    vol_bps = fetch_5m_volatility_for_day(sym, d)
                    if vol_bps is not None:
                        result["volatility_bps"] = round(vol_bps, 2)
                    sym_stats[d] = result
                    cache[cache_key][d] = result
                    vol_str = f"vol={result.get('volatility_bps', '?')}bps"
                    print(f"  {d}: symstats - sprd={result['spread_bps']:.1f}bps, {vol_str}, ${result['volume_ntl']:,.0f}")
                    continue

            # API fallback for completed day
            result = compute_completed_day_stats(sym, d)
            if result:
                sym_stats[d] = result
                cache[cache_key][d] = result
                vol_str = f"vol={result.get('volatility_bps', '?')}bps"
                print(f"  {d}: API - {vol_str}, ${result['volume_ntl']:,.0f}")

        if sym_stats:
            stats[sym] = sym_stats

    save_cache(cache)
    save_today_state(today_state)
    return stats


def build_output(stats, date_range):
    """Build the final JSON payload for the dashboard."""
    et_now = datetime.now(ET)
    return {
        "dates": date_range,
        "symbols": stats,
        "lastUpdated": et_now.isoformat(),
    }


def publish(payload):
    """Write market_stats.json and push to GitHub Pages."""
    with open(OUTPUT_FILE, "w") as f:
        json.dump(payload, f, indent=1)

    try:
        subprocess.run(["git", "-C", GIT_DIR, "add",
                        "market_stats.json", "market_stats_cache.json", "market_stats_today.json"],
                       check=True)
        subprocess.run(["git", "-C", GIT_DIR, "commit", "-m", "Update market stats"], check=False)
        subprocess.run(["git", "-C", GIT_DIR, "push"], check=True)
        print("Pushed market_stats.json to GitHub Pages")
    except subprocess.CalledProcessError as e:
        print(f"Git push failed: {e}")


def run_once(num_days=10):
    """Single pass: compute and publish."""
    print(f"\n{'='*60}")
    print(f"hip3stats run @ {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"{'='*60}")

    symbols = discover_symbols()
    if not symbols:
        print("No symbols found, skipping")
        return

    date_range = get_date_range(num_days)
    print(f"Date range: {date_range[0]} -> {date_range[-1]} ({len(date_range)} days)")

    stats = compute_all_stats(symbols, date_range)
    payload = build_output(stats, date_range)
    publish(payload)

    print(f"\nDone. {len(stats)} symbols published.")


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid xyz market stats tracker")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=900, help="Update interval in seconds (default 900)")
    parser.add_argument("--days", type=int, default=10, help="Number of days to look back (default 10)")
    args = parser.parse_args()

    if args.once:
        run_once(num_days=args.days)
        return

    while True:
        now_et = datetime.now(ET)
        h, m = now_et.hour, now_et.minute
        mins = h * 60 + m
        # Skip 9:20-9:40 ET and 17:50-18:10 ET to avoid competing with trading
        if (560 <= mins <= 580) or (1070 <= mins <= 1090):
            resume = "9:41" if mins <= 580 else "18:11"
            print(f"\nBlackout window ({now_et.strftime('%H:%M')} ET) - skipping until {resume} ET...")
            time.sleep(60)
            continue

        try:
            run_once(num_days=args.days)
        except Exception as e:
            print(f"Error in run_once: {e}")
            import traceback
            traceback.print_exc()

        print(f"\nSleeping {args.interval}s until next update...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
