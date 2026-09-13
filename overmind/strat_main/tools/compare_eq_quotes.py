#!/usr/bin/env python

"""
Compare Databento quotes against Polygon (SIP) quotes.

For each Databento quote, finds the closest Polygon quote within 100ms
and calculates the difference in mid prices.

Usage:
    # Single file comparison:
    ./compare_eq_quotes.py --databento SPY_DATABENTO_20250115.csv --polygon SPY_POLYGON_20250115.csv
    ./compare_eq_quotes.py --databento SPY_DATABENTO_20250115.csv --polygon SPY_POLYGON_20250115.csv --output comparison.csv
    ./compare_eq_quotes.py --databento SPY_DATABENTO_20250115.csv --polygon SPY_POLYGON_20250115.csv --start 9:30 --end 16:00

    # Date range comparison (aggregates stats across all days):
    ./compare_eq_quotes.py --dbdir ./databento/ --polydir ./polygon/ --symbol SPY --start-date 20250113 --end-date 20250118
    ./compare_eq_quotes.py --dbdir ./databento/ --polydir ./polygon/ --symbol SPY --start-date 20250113 --end-date 20250118 --weekdays
    ./compare_eq_quotes.py --dbdir ./databento/ --polydir ./polygon/ --symbol SPY --start-date 20250113 --end-date 20250118 --dbsource DATABENTO

Output:
    - Prints summary statistics (aggregated across all days in range mode)
    - Single file mode: writes matched comparison CSV: {databento_file}_vs_polygon.csv
"""

import argparse
import bisect
import datetime
import os
import sys

script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(script_dir, ".."))
sys.path.insert(0, os.path.join(script_dir, "../util"))

import pytz

import chron

ET_TZ = pytz.timezone("America/New_York")

# Max time difference to consider a match (in milliseconds)
MAX_TIME_DIFF_MS = 100

# Price sanity bounds - filter out obvious bad data
MIN_PRICE = 0.01       # Minimum valid price
MAX_PRICE = 100000.0   # Maximum valid price (covers most equities)
MAX_SPREAD_PCT = 10.0  # Maximum spread as % of mid (filter crossed/wide markets)

# Default regular trading hours (Eastern time)
DEFAULT_START_TIME = "9:30"
DEFAULT_END_TIME = "16:00"


def parse_time_arg(time_str: str) -> tuple:
    """Parse a time string like '9:30' or '16:00' into (hour, minute)."""
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format '{time_str}', expected H:MM or HH:MM")
    return int(parts[0]), int(parts[1])


def is_within_time_range(timestamp_ms: int, start_time: str, end_time: str) -> bool:
    """Check if timestamp is within the specified time range (ET)."""
    dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000, tz=ET_TZ)
    time_of_day = dt.hour * 60 + dt.minute  # minutes since midnight
    start_h, start_m = parse_time_arg(start_time)
    end_h, end_m = parse_time_arg(end_time)
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    return start_mins <= time_of_day < end_mins


def is_valid_quote(bb: float, ba: float) -> bool:
    """Check if a quote passes sanity checks."""
    # Basic bounds check
    if bb <= 0 or ba <= 0:
        return False
    if bb > MAX_PRICE or ba > MAX_PRICE:
        return False
    if bb < MIN_PRICE or ba < MIN_PRICE:
        return False

    # Crossed market check
    if bb > ba:
        return False

    # Wide spread check
    mid = (bb + ba) / 2
    spread = ba - bb
    if mid > 0 and (spread / mid) * 100 > MAX_SPREAD_PCT:
        return False

    return True


def parse_timestamp(ts_str: str) -> int:
    """Parse timestamp string to milliseconds since epoch."""
    # Format: "2025-01-15 09:30:00.123"
    dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
    dt = ET_TZ.localize(dt)
    return int(dt.timestamp() * 1000)


def load_quotes(csv_path: str, clean: bool = True, start_time: str = None, end_time: str = None) -> list:
    """Load quotes from CSV file. Returns list of (timestamp_ms, bb, ba, mid).

    Args:
        csv_path: Path to CSV file
        clean: Filter out invalid quotes (bad prices, crossed markets, etc.)
        start_time: Start time filter (e.g., "9:30"), None to disable
        end_time: End time filter (e.g., "16:00"), None to disable
    """
    quotes = []
    skipped_invalid = 0
    skipped_outside_range = 0
    filter_by_time = start_time is not None and end_time is not None
    with open(csv_path, "r") as f:
        header = f.readline().strip()
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            ts_str = parts[0]
            try:
                ts_ms = parse_timestamp(ts_str)
                bb = float(parts[1])
                ba = float(parts[2])
                mid = float(parts[3])

                if clean and not is_valid_quote(bb, ba):
                    skipped_invalid += 1
                    continue

                if filter_by_time and not is_within_time_range(ts_ms, start_time, end_time):
                    skipped_outside_range += 1
                    continue

                quotes.append((ts_ms, bb, ba, mid))
            except (ValueError, IndexError):
                skipped_invalid += 1
                continue

    if skipped_invalid > 0:
        print(f"  Skipped {skipped_invalid} invalid quotes")
    if skipped_outside_range > 0:
        print(f"  Skipped {skipped_outside_range} quotes outside time range")
    return quotes


def find_closest_quote(target_ts: int, quotes: list, timestamps: list) -> tuple:
    """
    Find the closest quote to target_ts within MAX_TIME_DIFF_MS.
    Returns (quote, time_diff_ms) or (None, None) if no match within threshold.
    """
    # Binary search for insertion point
    idx = bisect.bisect_left(timestamps, target_ts)

    best_quote = None
    best_diff = MAX_TIME_DIFF_MS + 1

    # Check the quote at idx and idx-1 (the two closest candidates)
    for i in [idx - 1, idx]:
        if 0 <= i < len(quotes):
            diff = abs(timestamps[i] - target_ts)
            if diff < best_diff:
                best_diff = diff
                best_quote = quotes[i]

    if best_diff <= MAX_TIME_DIFF_MS:
        return best_quote, best_diff
    return None, None


def compare_quotes(db_quotes: list, poly_quotes: list, verbose: bool = True) -> dict:
    """
    Compare Databento quotes against Polygon quotes.

    Returns dict with:
        - db_count, poly_count, matched, unmatched
        - mid_diffs, mid_diffs_bps, abs_mid_diffs_bps, time_diffs (lists)
        - comparisons (list of detailed comparison dicts)
    """
    if not db_quotes or not poly_quotes:
        return None

    # Extract timestamps for binary search
    poly_timestamps = [q[0] for q in poly_quotes]

    if verbose:
        print(f"  Comparing {len(db_quotes)} DB quotes against {len(poly_quotes)} Polygon quotes...")

    # Statistics
    matched = 0
    unmatched = 0
    mid_diffs = []
    mid_diffs_bps = []
    abs_mid_diffs_bps = []
    time_diffs = []
    comparisons = []

    for db_ts, db_bb, db_ba, db_mid in db_quotes:
        poly_quote, time_diff = find_closest_quote(db_ts, poly_quotes, poly_timestamps)

        if poly_quote is None:
            unmatched += 1
            continue

        matched += 1
        poly_ts, poly_bb, poly_ba, poly_mid = poly_quote

        # Calculate mid difference (Databento - Polygon)
        mid_diff = db_mid - poly_mid
        mid_diff_bps = (mid_diff / poly_mid) * 10000 if poly_mid != 0 else 0

        mid_diffs.append(mid_diff)
        mid_diffs_bps.append(mid_diff_bps)
        abs_mid_diffs_bps.append(abs(mid_diff_bps))
        time_diffs.append(time_diff)

        comparisons.append({
            "db_ts": db_ts,
            "poly_ts": poly_ts,
            "time_diff_ms": time_diff,
            "db_mid": db_mid,
            "poly_mid": poly_mid,
            "mid_diff": mid_diff,
            "mid_diff_bps": mid_diff_bps,
            "db_bb": db_bb,
            "db_ba": db_ba,
            "poly_bb": poly_bb,
            "poly_ba": poly_ba,
        })

    if verbose:
        match_rate = matched / len(db_quotes) * 100 if db_quotes else 0
        print(f"  Matched: {matched}, Unmatched: {unmatched} ({match_rate:.1f}% match rate)")

    return {
        "db_count": len(db_quotes),
        "poly_count": len(poly_quotes),
        "matched": matched,
        "unmatched": unmatched,
        "mid_diffs": mid_diffs,
        "mid_diffs_bps": mid_diffs_bps,
        "abs_mid_diffs_bps": abs_mid_diffs_bps,
        "time_diffs": time_diffs,
        "comparisons": comparisons,
    }


def print_stats(stats: dict, title: str = "SUMMARY STATISTICS"):
    """Print summary statistics from comparison results."""
    print(f"\n{'='*60}")
    print(title)
    print(f"{'='*60}")
    #print(f"Databento quotes:     {stats['db_count']}")
    #print(f"Polygon quotes:       {stats['poly_count']}")
    print(f"Matched:              {stats['matched']}")
    #print(f"Unmatched:            {stats['unmatched']}")
    if stats['db_count'] > 0:
        print(f"Match rate:           {stats['matched']/stats['db_count']*100:.2f}%")

    matched = stats['matched']
    if matched > 0:
        mid_diffs = stats['mid_diffs']
        mid_diffs_bps = stats['mid_diffs_bps']
        abs_mid_diffs_bps = stats['abs_mid_diffs_bps']
        time_diffs = stats['time_diffs']

        avg_time_diff = sum(time_diffs) / len(time_diffs)
        avg_mid_diff = sum(mid_diffs) / len(mid_diffs)
        avg_mid_diff_bps = sum(mid_diffs_bps) / len(mid_diffs_bps)
        avg_abs_mid_diff_bps = sum(abs_mid_diffs_bps) / len(abs_mid_diffs_bps)

        # Sort for percentiles
        sorted_abs_bps = sorted(abs_mid_diffs_bps)
        p50_idx = int(len(sorted_abs_bps) * 0.50)
        p90_idx = int(len(sorted_abs_bps) * 0.90)
        p95_idx = int(len(sorted_abs_bps) * 0.95)
        p99_idx = int(len(sorted_abs_bps) * 0.99)

        #print(f"\n{'='*60}")
        #print("TIME DIFF STATS (ms)")
        #print(f"{'='*60}")
        #print(f"Avg time diff:        {avg_time_diff:.2f} ms")
        #print(f"Max time diff:        {max(time_diffs)} ms")

        print(f"\n{'='*60}")
        print("MID PRICE DIFF STATS (Databento - Polygon)")
        print(f"{'='*60}")

        print(f"Avg mid diff (skew?):         ${avg_mid_diff:.6f} ({avg_mid_diff_bps:.3f} bps)")
        print(f"Avg |mid diff|:       {avg_abs_mid_diff_bps:.3f} bps")
        #print(f"Max |mid diff|:       {max(abs_mid_diffs_bps):.3f} bps")
        #print(f"Min mid diff:         {min(mid_diffs_bps):.3f} bps")
        #print(f"Max mid diff:         {max(mid_diffs_bps):.3f} bps")

        print(f"\n{'='*60}")
        print("|MID DIFF| PERCENTILES (bps)")
        print(f"{'='*60}")
        print(f"P50:                  {sorted_abs_bps[p50_idx]:.3f} bps")
        print(f"P90:                  {sorted_abs_bps[p90_idx]:.3f} bps")
        print(f"P95:                  {sorted_abs_bps[p95_idx]:.3f} bps")
        print(f"P99:                  {sorted_abs_bps[p99_idx]:.3f} bps")

        # Count how many are off by more than certain thresholds
        off_1bps = sum(1 for x in abs_mid_diffs_bps if x > 1)
        off_5bps = sum(1 for x in abs_mid_diffs_bps if x > 5)
        off_10bps = sum(1 for x in abs_mid_diffs_bps if x > 10)
        off_50bps = sum(1 for x in abs_mid_diffs_bps if x > 50)

        print(f"\n{'='*60}")
        print("ERROR RATE BY THRESHOLD")
        print(f"{'='*60}")
        print(f"> 1 bps off:          {off_1bps} ({off_1bps/matched*100:.2f}%)")
        print(f"> 5 bps off:          {off_5bps} ({off_5bps/matched*100:.2f}%)")
        print(f"> 10 bps off:         {off_10bps} ({off_10bps/matched*100:.2f}%)")
        print(f"> 50 bps off:         {off_50bps} ({off_50bps/matched*100:.2f}%)")


def write_comparison_csv(comparisons: list, output_path: str):
    """Write matched comparison results to CSV."""
    with open(output_path, "w") as f:
        f.write("timestamp,db_mid,poly_mid,delta,delta_bps\n")
        for c in comparisons:
            ts_str = datetime.datetime.fromtimestamp(c["db_ts"] / 1000, tz=ET_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            f.write(f"{ts_str},{c['db_mid']},{c['poly_mid']},{c['mid_diff']:.6f},{c['mid_diff_bps']:.3f}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Compare Databento quotes against Polygon (SIP) quotes"
    )
    # Single file mode
    parser.add_argument("--databento", required=False, help="Databento CSV file (single file mode)")
    parser.add_argument("--polygon", required=False, help="Polygon (SIP) CSV file (single file mode)")

    # Directory/range mode
    parser.add_argument("--dbdir", required=False, help="Directory containing Databento CSV files")
    parser.add_argument("--polydir", required=False, help="Directory containing Polygon CSV files")
    parser.add_argument("--symbol", required=False, help="Symbol for directory mode (e.g., SPY)")
    parser.add_argument("--start-date", required=False, help="Start date for range (YYYYMMDD)")
    parser.add_argument("--end-date", required=False, help="End date for range (YYYYMMDD, exclusive)")
    parser.add_argument("--weekdays", action="store_true", help="Only compare weekdays (skip weekends)")
    parser.add_argument("--dbsource", default=None, choices=["DATABENTO", "DATABENTONASDAQ"],
                        help="Databento source name in filenames (default: auto-detect, tries both)")

    # Common options
    parser.add_argument("--output", required=False, help="Output detailed comparison CSV file")
    parser.add_argument("--no-clean", action="store_true", help="Disable data cleaning/filtering")
    parser.add_argument("--all-hours", action="store_true", help="Include extended hours (default: RTH only)")
    parser.add_argument("--start", default=DEFAULT_START_TIME, help=f"Start time filter (default: {DEFAULT_START_TIME})")
    parser.add_argument("--end", default=DEFAULT_END_TIME, help=f"End time filter (default: {DEFAULT_END_TIME})")
    args = parser.parse_args()

    # Determine mode
    single_file_mode = args.databento is not None and args.polygon is not None
    dir_mode = args.dbdir is not None and args.polydir is not None

    if not single_file_mode and not dir_mode:
        parser.error("Must specify either (--databento and --polygon) or (--dbdir, --polydir, --symbol, --start-date, --end-date)")
    if single_file_mode and dir_mode:
        parser.error("Cannot mix single file mode and directory mode")
    if dir_mode and (not args.symbol or not args.start_date or not args.end_date):
        parser.error("Directory mode requires --symbol, --start-date, and --end-date")

    clean = not args.no_clean
    if args.all_hours:
        start_time = None
        end_time = None
    else:
        start_time = args.start
        end_time = args.end

    if clean:
        print(f"Data cleaning enabled (min_price={MIN_PRICE}, max_price={MAX_PRICE}, max_spread={MAX_SPREAD_PCT}%)")
    if start_time and end_time:
        print(f"Time filter: {start_time} - {end_time} ET")

    if single_file_mode:
        # Single file comparison (original behavior)
        print(f"\nLoading Databento quotes from {args.databento}...")
        db_quotes = load_quotes(args.databento, clean=clean, start_time=start_time, end_time=end_time)
        print(f"  Loaded {len(db_quotes)} valid quotes")

        print(f"Loading Polygon quotes from {args.polygon}...")
        poly_quotes = load_quotes(args.polygon, clean=clean, start_time=start_time, end_time=end_time)
        print(f"  Loaded {len(poly_quotes)} valid quotes")

        if not db_quotes or not poly_quotes:
            print("Error: No quotes loaded from one or both files")
            sys.exit(1)

        stats = compare_quotes(db_quotes, poly_quotes, verbose=False)
        print_stats(stats)

        # Write output CSV
        if stats and stats['comparisons']:
            base_name = os.path.splitext(os.path.basename(args.databento))[0]
            matched_output = f"{base_name}_vs_polygon.csv"
            print(f"\nWriting matched quotes to {matched_output}...")
            write_comparison_csv(stats['comparisons'], matched_output)
            print(f"  Wrote {len(stats['comparisons'])} matched records")

    else:
        # Directory/range mode - aggregate across days
        dates_method = "WEEKDAYS" if args.weekdays else "ALLDAYS"
        dates = chron.dates_list(args.start_date, args.end_date, dates_method=dates_method)

        # Determine which databento sources to try
        if args.dbsource:
            db_sources = [args.dbsource]
        else:
            db_sources = ["DATABENTO", "DATABENTONASDAQ"]

        print(f"\nComparing {args.symbol}: {args.start_date} to {args.end_date} ({len(dates)} days, {dates_method})")
        print(f"  Databento dir: {args.dbdir} (source: {args.dbsource or 'auto-detect'})")
        print(f"  Polygon dir:   {args.polydir}")

        # Aggregate statistics
        total_db_count = 0
        total_poly_count = 0
        total_matched = 0
        total_unmatched = 0
        all_mid_diffs = []
        all_mid_diffs_bps = []
        all_abs_mid_diffs_bps = []
        all_time_diffs = []
        all_comparisons = []

        days_processed = 0
        days_skipped = 0

        for date_str in dates:
            # Try to find databento file (try multiple source names)
            db_file = None
            for src in db_sources:
                candidate = os.path.join(args.dbdir, f"{args.symbol}_{src}_{date_str}.csv")
                if os.path.exists(candidate):
                    db_file = candidate
                    break

            poly_file = os.path.join(args.polydir, f"{args.symbol}_POLYGON_{date_str}.csv")

            if not db_file:
                tried = ", ".join(f"{args.symbol}_{src}_{date_str}.csv" for src in db_sources)
                print(f"\n[{date_str}] Skipping - Databento file not found (tried: {tried})")
                days_skipped += 1
                continue
            if not os.path.exists(poly_file):
                print(f"\n[{date_str}] Skipping - Polygon file not found: {poly_file}")
                days_skipped += 1
                continue

            print(f"\n[{date_str}] Processing...")

            try:
                db_quotes = load_quotes(db_file, clean=clean, start_time=start_time, end_time=end_time)
                poly_quotes = load_quotes(poly_file, clean=clean, start_time=start_time, end_time=end_time)

                if not db_quotes or not poly_quotes:
                    print(f"  Skipping - no valid quotes")
                    days_skipped += 1
                    continue

                stats = compare_quotes(db_quotes, poly_quotes, verbose=True)

                if stats:
                    total_db_count += stats['db_count']
                    total_poly_count += stats['poly_count']
                    total_matched += stats['matched']
                    total_unmatched += stats['unmatched']
                    all_mid_diffs.extend(stats['mid_diffs'])
                    all_mid_diffs_bps.extend(stats['mid_diffs_bps'])
                    all_abs_mid_diffs_bps.extend(stats['abs_mid_diffs_bps'])
                    all_time_diffs.extend(stats['time_diffs'])
                    all_comparisons.extend(stats['comparisons'])
                    days_processed += 1

            except Exception as e:
                print(f"  Error: {e}")
                days_skipped += 1
                continue

        # Print aggregate statistics
        if days_processed > 0:
            aggregate_stats = {
                "db_count": total_db_count,
                "poly_count": total_poly_count,
                "matched": total_matched,
                "unmatched": total_unmatched,
                "mid_diffs": all_mid_diffs,
                "mid_diffs_bps": all_mid_diffs_bps,
                "abs_mid_diffs_bps": all_abs_mid_diffs_bps,
                "time_diffs": all_time_diffs,
            }
            print_stats(aggregate_stats, f"AGGREGATE STATISTICS ({days_processed} days, {days_skipped} skipped)")
        else:
            print(f"\nNo days successfully processed (skipped {days_skipped})")


if __name__ == "__main__":
    main()
