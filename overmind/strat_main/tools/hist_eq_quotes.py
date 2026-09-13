#!/usr/bin/env python

"""
Fetch historical equity quotes from Polygon or Databento and write to file.

Usage:
    # Single date:
    ./hist_eq_quotes.py --symbol SPY --date 20250115 --source POLYGON
    ./hist_eq_quotes.py --symbol SPY --date 20250115 --source DATABENTO
    ./hist_eq_quotes.py --symbol SPY --date 20250115 --source DATABENTONASDAQ
    ./hist_eq_quotes.py --symbol SPY --date 20250115 --source DATABENTO --output spy_quotes.csv

    # Date range (outputs {symbol}_{source}_{date}.csv for each day):
    ./hist_eq_quotes.py --symbol SPY --start-date 20250113 --end-date 20250118 --source POLYGON --outdir ./quotes/
    ./hist_eq_quotes.py --symbol SPY --start-date 20250113 --end-date 20250118 --source POLYGON --outdir ./quotes/ --weekdays

Sources:
    POLYGON         - Polygon.io SIP data
    DATABENTO       - Databento EQUS.MINI (SIP consolidated, mbp-1 schema)
    DATABENTONASDAQ - Databento XNAS.BASIC (NASDAQ direct feed, cmbp-1 schema)

Output format (CSV):
    timestamp,bb,ba,mid
    2025-01-15 09:30:00.123,590.25,590.27,590.26
"""

import argparse
import datetime
import json
import os
import sys

# For running stuff in directory and through pybin
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(script_dir, ".."))
sys.path.insert(0, os.path.join(script_dir, "../util"))

import pytz

import chron

ET_TZ = pytz.timezone("America/New_York")


def ms_to_eastern_str(timestamp_ms: int) -> str:
    """Convert milliseconds timestamp to Eastern time string."""
    dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000, tz=ET_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Keep only 3 decimal places (ms)


# Price sanity bounds - filter out obvious bad data
MIN_PRICE = 0.01       # Minimum valid price
MAX_PRICE = 100000.0   # Maximum valid price (covers most equities)
MAX_SPREAD_PCT = 10.0  # Maximum spread as % of mid (filter crossed/wide markets)


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


# Databento imports
import databento as db
from databento import MBP1Msg

# Polygon imports
from polygon import RESTClient


# Load credentials
DB_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.DataBento.creds.json")
POLYGON_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.Polygon.creds.json")


def load_databento_key():
    try:
        with open(DB_SECRET_FILE_PATH, "r") as f:
            secrets = json.load(f)
            return secrets["api_key"]
    except FileNotFoundError:
        sys.exit(f"Credentials file not found at {DB_SECRET_FILE_PATH}")
    except KeyError:
        sys.exit(f"API key not found in {DB_SECRET_FILE_PATH}")


def load_polygon_key():
    try:
        with open(POLYGON_SECRET_FILE_PATH, "r") as f:
            secrets = json.load(f)
            return secrets["api_key"]
    except FileNotFoundError:
        sys.exit(f"Credentials file not found at {POLYGON_SECRET_FILE_PATH}")
    except KeyError:
        sys.exit(f"API key not found in {POLYGON_SECRET_FILE_PATH}")


def fetch_databento_quotes(symbol: str, date_str: str, output_file: str, dataset: str = "EQUS.MINI"):
    """Fetch historical quotes from Databento and write to CSV.

    Args:
        symbol: Stock symbol (e.g., SPY)
        date_str: Date in YYYYMMDD format
        output_file: Output CSV file path
        dataset: Databento dataset - "EQUS.MINI", "XNAS.ITCH", or "XNAS.BASIC"
    """
    api_key = load_databento_key()
    client = db.Historical(key=api_key)

    # Parse date and create date range
    date_obj = datetime.datetime.strptime(date_str, "%Y%m%d")
    start_str = date_obj.strftime("%Y-%m-%d")
    end_str = (date_obj + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    # Select schema based on dataset
    # XNAS.BASIC doesn't support mbp-1, use cmbp-1 (consolidated mbp-1) instead
    if dataset == "XNAS.BASIC":
        schema = "cmbp-1"
    else:
        schema = "mbp-1"

    print(f"Fetching Databento quotes for {symbol} from {start_str} to {end_str} (dataset: {dataset}, schema: {schema})")

    # Databento price conversion factor (prices are in fixed-point)
    db_px_convert = 1_000_000_000

    try:
        data = client.timeseries.get_range(
            dataset=dataset,
            start=start_str,
            end=end_str,
            symbols=[symbol],
            schema=schema,
            stype_in="raw_symbol",
        )

        count = 0
        skipped = 0
        with open(output_file, "w") as f:
            f.write("timestamp,bb,ba,mid\n")

            for msg in data:
                bid_px = None
                ask_px = None

                # Try to extract bid/ask - handle different message formats
                # MBP1Msg (mbp-1 schema) has levels array
                if hasattr(msg, 'levels') and len(msg.levels) > 0:
                    level = msg.levels[0]
                    bid_px = float(level.bid_px) / db_px_convert if level.bid_px else None
                    ask_px = float(level.ask_px) / db_px_convert if level.ask_px else None
                # CBBO messages (cmbp-1 schema) have bid_px/ask_px directly
                elif hasattr(msg, 'bid_px') and hasattr(msg, 'ask_px'):
                    bid_px = float(msg.bid_px) / db_px_convert if msg.bid_px else None
                    ask_px = float(msg.ask_px) / db_px_convert if msg.ask_px else None
                else:
                    continue

                if bid_px is None or ask_px is None:
                    skipped += 1
                    continue

                # Validate quote
                if not is_valid_quote(bid_px, ask_px):
                    skipped += 1
                    continue

                # ts_event is in nanoseconds
                timestamp_ms = int(msg.ts_event / 1_000_000)
                timestamp_str = ms_to_eastern_str(timestamp_ms)
                mid = round((bid_px + ask_px) / 2, 6)

                f.write(f"{timestamp_str},{bid_px},{ask_px},{mid}\n")
                count += 1

                if count % 100000 == 0:
                    print(f"  Processed {count} quotes...")

        print(f"Wrote {count} quotes to {output_file} (skipped {skipped} invalid)")

    except Exception as e:
        print(f"Error fetching Databento data: {e}")
        raise


def fetch_polygon_quotes(symbol: str, date_str: str, output_file: str):
    """Fetch historical quotes from Polygon and write to CSV."""
    api_key = load_polygon_key()
    client = RESTClient(api_key)

    # Parse date and create timestamp range
    date_obj = datetime.datetime.strptime(date_str, "%Y%m%d").date()
    start_dt = ET_TZ.localize(datetime.datetime.combine(date_obj, datetime.time(0, 0, 0)))
    end_dt = start_dt + datetime.timedelta(days=1)

    start_ns = int(start_dt.timestamp() * 1_000_000_000)
    end_ns = int(end_dt.timestamp() * 1_000_000_000)

    print(f"Fetching Polygon quotes for {symbol} from {start_dt.isoformat()} to {end_dt.isoformat()}")

    try:
        count = 0
        skipped = 0
        with open(output_file, "w") as f:
            f.write("timestamp,bb,ba,mid\n")

            for quote in client.list_quotes(
                ticker=symbol,
                timestamp_gte=start_ns,
                timestamp_lt=end_ns,
                limit=50000,
                order='asc',
            ):
                bid_px = getattr(quote, "bid_price", None)
                ask_px = getattr(quote, "ask_price", None)
                sip_ts = getattr(quote, "sip_timestamp", None)

                if bid_px is None or ask_px is None or sip_ts is None:
                    skipped += 1
                    continue

                # Validate quote
                if not is_valid_quote(bid_px, ask_px):
                    skipped += 1
                    continue

                timestamp_ms = int(sip_ts / 1_000_000)
                timestamp_str = ms_to_eastern_str(timestamp_ms)
                mid = round((bid_px + ask_px) / 2, 6)

                f.write(f"{timestamp_str},{bid_px},{ask_px},{mid}\n")
                count += 1

                if count % 100000 == 0:
                    print(f"  Processed {count} quotes...")

        print(f"Wrote {count} quotes to {output_file} (skipped {skipped} invalid)")

    except Exception as e:
        print(f"Error fetching Polygon data: {e}")
        raise


def fetch_for_date(symbol: str, date_str: str, source: str, output_file: str):
    """Fetch quotes for a single date."""
    if source == "DATABENTO":
        fetch_databento_quotes(symbol, date_str, output_file, dataset="EQUS.MINI")
    elif source == "DATABENTONASDAQ":
        fetch_databento_quotes(symbol, date_str, output_file, dataset="XNAS.BASIC")
    elif source == "POLYGON":
        fetch_polygon_quotes(symbol, date_str, output_file)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch historical equity quotes from Polygon or Databento"
    )
    parser.add_argument("--symbol", required=True, help="Stock symbol (e.g., SPY)")
    parser.add_argument("--date", required=False, help="Single date in YYYYMMDD format")
    parser.add_argument("--start-date", required=False, help="Start date for range (YYYYMMDD)")
    parser.add_argument("--end-date", required=False, help="End date for range (YYYYMMDD, exclusive)")
    parser.add_argument("--weekdays", action="store_true", help="Only fetch weekdays (skip weekends)")
    parser.add_argument(
        "--source",
        required=True,
        choices=["DATABENTO", "DATABENTONASDAQ", "POLYGON"],
        help="Data source: DATABENTO (SIP via EQUS.MINI), DATABENTONASDAQ (XNAS.BASIC), or POLYGON (SIP)"
    )
    parser.add_argument(
        "--output",
        required=False,
        help="Output file path for single date (default: {symbol}_{source}_{date}.csv)"
    )
    parser.add_argument(
        "--outdir",
        required=False,
        help="Output directory for date range (files named {symbol}_{source}_{date}.csv)"
    )
    args = parser.parse_args()

    # Validate arguments
    has_single_date = args.date is not None
    has_date_range = args.start_date is not None and args.end_date is not None

    if not has_single_date and not has_date_range:
        parser.error("Must specify either --date or both --start-date and --end-date")
    if has_single_date and has_date_range:
        parser.error("Cannot specify both --date and --start-date/--end-date")
    if has_date_range and not args.outdir:
        parser.error("--outdir is required when using date range")
    if has_single_date and args.outdir:
        parser.error("--outdir is only valid with date range, use --output for single date")

    print(f"Source: {args.source}")
    print(f"Symbol: {args.symbol}")

    if has_single_date:
        # Single date mode
        if args.output:
            output_file = args.output
        else:
            output_file = f"{args.symbol}_{args.source}_{args.date}.csv"

        print(f"Date: {args.date}")
        print(f"Output: {output_file}")
        print()

        fetch_for_date(args.symbol, args.date, args.source, output_file)
    else:
        # Date range mode
        dates_method = "WEEKDAYS" if args.weekdays else "ALLDAYS"
        dates = chron.dates_list(args.start_date, args.end_date, dates_method=dates_method)

        print(f"Date range: {args.start_date} to {args.end_date} ({len(dates)} days, {dates_method})")
        print(f"Output dir: {args.outdir}")
        print()

        # Create output directory if it doesn't exist
        os.makedirs(args.outdir, exist_ok=True)

        for i, date_str in enumerate(dates):
            output_file = os.path.join(args.outdir, f"{args.symbol}_{args.source}_{date_str}.csv")
            print(f"\n[{i+1}/{len(dates)}] Processing {date_str}...")
            try:
                fetch_for_date(args.symbol, date_str, args.source, output_file)
            except Exception as e:
                print(f"  Error processing {date_str}: {e}")
                continue

        print(f"\nCompleted {len(dates)} days")


if __name__ == "__main__":
    main()
