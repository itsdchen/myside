#!/usr/bin/env python3
"""Half-hour symstats market-data cache.

Builds and queries a per-(sym, date) cache of market summary statistics
sliced into 48 half-hour buckets (00:00–23:59). Each bucket is one
symstats invocation. The cache is the data product; lookups aggregate
overlapping buckets over arbitrary time windows (handles wrap-around).

Cache layout: {cache_root}/{book}/{sym}_{date}.json
Bucket keys are HH:MM:00 strings (NY time). The last bucket (23:30:00)
runs through 23:59:59 — we drop the final second.

Usage as CLI:
  python symstats_cache.py populate --syms xyz:JPY,xyz:COPPER --dates 20260420:20260423
  python symstats_cache.py lookup --sym xyz:JPY --date 20260423 --start 09:30:00 --end 16:00:00
"""

import argparse
import json
import math
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util.pathing import bindir
from tools import md_exists

SYMSTATS_BIN = os.path.join(bindir(), "symstats")
DEFAULT_BOOK = "Hyperliquid"
DEFAULT_DATADIR = os.path.expanduser("~/tardis_datasets/gzpbf")
DEFAULT_CACHE_ROOT = os.path.expanduser("~/scratch/tradeperf/_symstats")
TZ = "America/New_York"
N_BUCKETS = 48
# Missing-data sentinels older than this get retried — S3 may have ingested
# the date since we first asked.
SENTINEL_TTL_DAYS = 0.5


# ── Misc helpers ──────────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ── Bucket helpers ────────────────────────────────────────────────────────────

def _bucket_starts():
    """48 half-hour bucket start times as 'HH:MM:00' strings."""
    return [f"{hh:02d}:{mm:02d}:00" for hh in range(24) for mm in (0, 30)]


def _bucket_end(start_hms):
    """End HH:MM:SS for a bucket starting at start_hms (exclusive of next start)."""
    h, m, _ = map(int, start_hms.split(":"))
    m += 30
    if m >= 60:
        m -= 60
        h += 1
    if h >= 24:
        return "23:59:59"  # last bucket clipped from 24:00:00
    return f"{h:02d}:{m:02d}:00"


# ── Cache I/O ─────────────────────────────────────────────────────────────────

def _cache_path(cache_root, book, sym, date):
    return os.path.join(cache_root, book, f"{sym}_{date}.json")


def _load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _is_complete(cache):
    """Cache entry is 'complete' if it has all 48 buckets, OR is a missing
    sentinel that's still within its TTL. Old missing-sentinels are treated
    as incomplete so they get retried — S3 may have ingested the date since.
    """
    if not cache:
        return False
    if cache.get("missing"):
        pa = cache.get("populated_at")
        if not pa:
            return True  # legacy sentinel without timestamp; keep
        try:
            age_days = (datetime.now() - datetime.fromisoformat(pa)).total_seconds() / 86400.0
        except (TypeError, ValueError):
            return True
        return age_days < SENTINEL_TTL_DAYS
    return len(cache.get("buckets", {})) == N_BUCKETS


# ── Symstats invocation ───────────────────────────────────────────────────────

def _parse_csv_row(stdout):
    """Parse the two-line CSV symstats produces. Returns dict (notional units) or None.

    Symstats reports vol / sizes in symbol units; this converts to notional
    using avg_mid so all stored magnitudes are in dollars and stay comparable
    across symbols with very different per-unit prices.
    """
    lines = stdout.strip().split("\n")
    if len(lines) < 2:
        return None
    headers = lines[0].split(",")
    values = lines[1].split(",")
    row = dict(zip(headers, values))

    def f(k):
        v = row.get(k, "")
        try:
            x = float(v)
            return None if math.isnan(x) else x
        except (TypeError, ValueError):
            return None

    avg_mid = f("avg_mid")
    vol = f("vol") or 0.0
    med_trdsz = f("med_trdsz") or 0.0
    med_inside = f("med_inside_liq") or 0.0
    px = avg_mid if (avg_mid is not None and avg_mid > 0) else 0.0
    return {
        "num_trades": int(f("num_trades") or 0),
        "vol_notl": vol * px,
        "med_trdsz_notl": med_trdsz * px,
        "med_inside_notl": med_inside * px,
        "avg_mid": avg_mid,
        "spread_pct": f("spread_pct"),
        "n_midchanges": int(f("n_midchanges") or 0),
    }


def _run_symstats(sym, book, date, start_hms, end_hms, datadir):
    """Run symstats for one [start, end] window. Returns dict or None on failure."""
    cmd = [
        SYMSTATS_BIN,
        "--book", book,
        "--symbol", sym,
        "--date", date,
        "--start-time", f"{start_hms} {TZ}",
        "--end-time", f"{end_hms} {TZ}",
        "--csv",
    ]
    if datadir:
        cmd += ["--datadir", datadir]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return _parse_csv_row(result.stdout)


# ── Histdata presence ─────────────────────────────────────────────────────────

def _have_histdata(sym, book, date, datadir):
    """True if both l2Book and trades gzpbfs are present locally."""
    l2 = os.path.join(datadir, book, f"{sym}_l2Book_{date}.gzpbf")
    tr = os.path.join(datadir, book, f"{sym}_trades_{date}.gzpbf")
    return os.path.exists(l2) and os.path.exists(tr)


def ensure_histdata(pairs, book, datadir):
    """Try to download missing histdata for (sym, date) pairs via md_exists.

    Best-effort: failures (no boto3, S3 issues, missing keys) are caught and
    logged. Caller should still re-check with `_have_histdata` per pair.
    """
    if not pairs:
        return
    syms_books_to_dates = {}
    for sym, date in pairs:
        syms_books_to_dates.setdefault((sym, book), []).append(date)
    try:
        md_exists.check_for_data(syms_books_to_dates, datadir, dl_automatically=True)
    except Exception as e:
        print(f"  [warn] md_exists.check_for_data failed: {e}", file=sys.stderr)


# ── Populate ──────────────────────────────────────────────────────────────────

def populate(sym, date, book=DEFAULT_BOOK, cache_root=DEFAULT_CACHE_ROOT,
             datadir=DEFAULT_DATADIR, force=False, n_parallel=8):
    """Populate the half-hour cache for one (sym, date).

    Returns "complete" / "missing" / "skipped".
    """
    path = _cache_path(cache_root, book, sym, date)
    if not force:
        existing = _load_cache(path)
        if _is_complete(existing):
            return "skipped"

    if not _have_histdata(sym, book, date, datadir):
        _save_cache(path, {"missing": True, "reason": "no histdata",
                           "populated_at": _now_iso()})
        return "missing"

    starts = _bucket_starts()

    def _one(start_hms):
        end_hms = _bucket_end(start_hms)
        return start_hms, _run_symstats(sym, book, date, start_hms, end_hms, datadir)

    buckets = {}
    with ThreadPoolExecutor(max_workers=n_parallel) as pool:
        for start_hms, stats in pool.map(_one, starts):
            if stats is not None:
                buckets[start_hms] = stats

    if not buckets:
        _save_cache(path, {"missing": True, "reason": "all buckets failed",
                           "populated_at": _now_iso()})
        return "missing"

    _save_cache(path, {
        "sym": sym, "book": book, "date": date,
        "populated_at": _now_iso(),
        "buckets": buckets,
    })
    return "complete"


def populate_many(pairs, book=DEFAULT_BOOK, cache_root=DEFAULT_CACHE_ROOT,
                  datadir=DEFAULT_DATADIR, force=False, n_parallel=8, verbose=True):
    """Populate cache for many (sym, date) pairs.

    Skips already-complete entries. Calls ensure_histdata once for the batch.
    """
    pairs = list(pairs)
    # Skip already-complete cache entries
    if not force:
        pending = [p for p in pairs
                   if not _is_complete(_load_cache(_cache_path(cache_root, book, *p)))]
    else:
        pending = pairs
    if not pending:
        if verbose:
            print(f"  All {len(pairs)} pair(s) already cached.")
        return

    # Of the pending ones, identify those still missing histdata locally
    missing_local = [(s, d) for s, d in pending if not _have_histdata(s, book, d, datadir)]
    if missing_local:
        if verbose:
            print(f"  {len(missing_local)} pair(s) missing histdata; trying md_exists download...")
        ensure_histdata(missing_local, book, datadir)

    counts = {"complete": 0, "missing": 0, "skipped": 0}
    for i, (sym, date) in enumerate(pending):
        status = populate(sym, date, book=book, cache_root=cache_root,
                          datadir=datadir, force=force, n_parallel=n_parallel)
        counts[status] += 1
        if verbose:
            print(f"  [{i+1}/{len(pending)}] {sym} {date}: {status}")
    if verbose:
        print(f"  Summary: complete={counts['complete']}  missing={counts['missing']}  "
              f"skipped={counts['skipped']}")


# ── Lookup ────────────────────────────────────────────────────────────────────

def lookup(sym, date, start_hms, end_hms, book=DEFAULT_BOOK,
           cache_root=DEFAULT_CACHE_ROOT):
    """Aggregate market stats over [start_hms, end_hms] for one (sym, date).

    Handles wrap-around (end < start crosses midnight). Returns dict with
    num_trades, vol, avg_mid, mkt_notl, med_trdsz, med_trdsz_notl,
    med_inside_liq, med_inside_liq_notl, n_buckets — or None if no data.
    """
    path = _cache_path(cache_root, book, sym, date)
    cache = _load_cache(path)
    if not cache or cache.get("missing"):
        return None
    buckets = cache.get("buckets", {})
    if not buckets:
        return None

    wraps = end_hms < start_hms

    def in_window(b_start):
        b_end = _bucket_end(b_start)
        # Bucket overlap test: bucket [b_start, b_end) vs requested window
        if wraps:
            # crosses midnight: window is [start_hms, 24:00) ∪ [00:00, end_hms]
            # include if bucket overlaps either part
            return b_end > start_hms or b_start < end_hms
        # non-wrap: bucket overlaps [start_hms, end_hms]
        return b_start < end_hms and b_end > start_hms

    chosen = [b for b in buckets if in_window(b)]
    if not chosen:
        return None

    total_trades = 0
    total_vol_notl = 0.0
    weighted_avg_mid = 0.0
    weighted_med_trdsz = 0.0
    weighted_med_inside = 0.0
    weighted_spread_pct = 0.0
    weight = 0
    for b in chosen:
        s = buckets[b]
        nt = s.get("num_trades", 0) or 0
        total_trades += nt
        total_vol_notl += s.get("vol_notl", 0.0) or 0.0
        if nt > 0:
            am = s.get("avg_mid")
            if am is not None and am > 0:
                weighted_avg_mid += am * nt
            weighted_med_trdsz += (s.get("med_trdsz_notl") or 0.0) * nt
            weighted_med_inside += (s.get("med_inside_notl") or 0.0) * nt
            sp = s.get("spread_pct")
            if sp is not None:
                weighted_spread_pct += sp * nt
            weight += nt

    avg_mid = weighted_avg_mid / weight if weight > 0 else 0.0
    med_trdsz_notl = weighted_med_trdsz / weight if weight > 0 else 0.0
    med_inside_notl = weighted_med_inside / weight if weight > 0 else 0.0
    spread_pct = weighted_spread_pct / weight if weight > 0 else 0.0

    return {
        "num_trades": total_trades,
        "vol_notl": total_vol_notl,
        "avg_mid": avg_mid,
        "med_trdsz_notl": med_trdsz_notl,
        "med_inside_notl": med_inside_notl,
        "spread_pct": spread_pct,
        "n_buckets": len(chosen),
    }


# ── Auto-populate from local configs ──────────────────────────────────────────

def _enabled_syms_from_local_configs(local_base, book):
    """Enumerate enabled symbols on `book` by scanning locally-cached live
    pk_*.json configs (the undated ones — `pk_*.json.YYYYMMDD` are dated
    backups and excluded by the glob since they don't end in `.json`).
    """
    import glob as globmod
    import json
    try:
        import commentjson
    except ImportError:
        commentjson = None
    syms = set()
    pattern = os.path.join(local_base, "*", "*", "*", "pk_*.json")
    for f in globmod.glob(pattern):
        try:
            with open(f) as fp:
                text = fp.read()
            try:
                cfg = json.loads(text)
            except json.JSONDecodeError:
                if commentjson is None:
                    continue
                cfg = commentjson.loads(text)
        except Exception:
            continue
        for trader in cfg.get("pktraders", []):
            if not trader.get("enabled"):
                continue
            markets = trader.get("ordex", {}).get("markets", [])
            if book in markets:
                sym = trader.get("traded_symbol", "")
                if sym:
                    syms.add(sym)
    return syms


def populate_from_local_configs(local_base, days, book=DEFAULT_BOOK,
                                cache_root=DEFAULT_CACHE_ROOT,
                                datadir=DEFAULT_DATADIR, n_parallel=16,
                                verbose=True):
    """Auto-populate symstats cache from currently-fetched live configs.

    Walks `pk_*.json` live configs under local_base, collects enabled symbols
    on `book`, and populates the cache for the last `days` calendar days.
    Already-cached complete entries are skipped; stale missing-sentinels
    (older than SENTINEL_TTL_DAYS) get retried automatically via _is_complete.
    """
    syms = _enabled_syms_from_local_configs(local_base, book)
    if not syms:
        if verbose:
            print(f"  symstats: no enabled {book} symbols found in local configs")
        return
    from datetime import datetime as _dt, timedelta as _td
    dates = [(_dt.today() - _td(days=i)).strftime("%Y%m%d") for i in range(days)]
    pairs = [(s, d) for s in sorted(syms) for d in dates]
    if verbose:
        print(f"  symstats: checking {len(syms)} {book} sym(s) × {days} day(s) "
              f"= {len(pairs)} pair(s)")
    populate_many(pairs, book=book, cache_root=cache_root, datadir=datadir,
                  n_parallel=n_parallel, verbose=verbose)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_dates_arg(s):
    """Accept 'YYYYMMDD,YYYYMMDD,...' or 'YYYYMMDD:YYYYMMDD' range."""
    from datetime import datetime, timedelta
    if ":" in s:
        start, end = s.split(":")
        d = datetime.strptime(start, "%Y%m%d")
        end_d = datetime.strptime(end, "%Y%m%d")
        out = []
        while d <= end_d:
            out.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        return out
    return [x.strip() for x in s.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="Half-hour symstats cache")
    sub = parser.add_subparsers(dest="cmd")

    p_pop = sub.add_parser("populate", help="Populate cache for sym/date pairs")
    p_pop.add_argument("--syms", required=True, help="Comma-separated symbols")
    p_pop.add_argument("--dates", required=True,
                       help="Comma-separated dates or START:END range (YYYYMMDD)")
    p_pop.add_argument("--book", default=DEFAULT_BOOK)
    p_pop.add_argument("--datadir", default=DEFAULT_DATADIR)
    p_pop.add_argument("--cache-root", default=DEFAULT_CACHE_ROOT)
    p_pop.add_argument("--force", action="store_true")
    p_pop.add_argument("--parallel", type=int, default=8,
                       help="Parallel symstats invocations per (sym, date)")

    p_look = sub.add_parser("lookup", help="Aggregate stats for a (sym, date, window)")
    p_look.add_argument("--sym", required=True)
    p_look.add_argument("--date", required=True)
    p_look.add_argument("--start", required=True, help="HH:MM:SS")
    p_look.add_argument("--end", required=True, help="HH:MM:SS")
    p_look.add_argument("--book", default=DEFAULT_BOOK)
    p_look.add_argument("--cache-root", default=DEFAULT_CACHE_ROOT)

    args = parser.parse_args()
    if args.cmd == "populate":
        syms = [x.strip() for x in args.syms.split(",") if x.strip()]
        dates = _parse_dates_arg(args.dates)
        pairs = [(s, d) for s in syms for d in dates]
        populate_many(pairs, book=args.book, cache_root=args.cache_root,
                      datadir=args.datadir, force=args.force,
                      n_parallel=args.parallel, verbose=True)
    elif args.cmd == "lookup":
        result = lookup(args.sym, args.date, args.start, args.end,
                        book=args.book, cache_root=args.cache_root)
        if result is None:
            print("No data")
        else:
            for k, v in result.items():
                if isinstance(v, float):
                    print(f"  {k}: {v:,.4f}")
                else:
                    print(f"  {k}: {v}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
