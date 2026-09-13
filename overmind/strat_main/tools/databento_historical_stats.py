#!/usr/bin/env python
"""
Databento Historical Statistics Analyzer

Fetches historical data from Databento and computes aggregated statistics
per half-hour bucket for US equity symbols over a date range.

Replacement for polygon_historical_stats.py using Databento as the data source.

For a given symbol and date range, calculates aggregated statistics:
- Average bid-ask spread (averaged across days)
- Average spread in basis points (averaged across days)
- Volume (total across all days and as % of total volume)
- Notional traded value

Time range: 4:00 AM to 8:00 PM ET (full trading day including pre/post market)

Uses ohlcv-1m bars for volume/price data and cbbo-1s (1-second consolidated BBO
snapshots) for spread calculations. Unlike the Polygon version, this fetches full
half-hour BBO data rather than sampling the first N minutes, since 1-second
snapshots are already lightweight (~58K records/day).

Sample command line:

./databento_historical_stats.py --symbol NVDA --start-date 2025-10-04 --end-date 2025-11-04 > ~/tradefi/retraded/overmind/for_live/per_eq_stats/nvda_stats.txt

./databento_historical_stats.py --symbol SPY --start-date 2025-10-04 --end-date 2025-11-04 --dataset EQUS.MINI

"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

import datetime as dt

from typing import List, Dict

import pytz
import databento as db

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Databento prices are fixed-point, divide by this factor
DB_PX_FACTOR = 1_000_000_000

# Credentials
DB_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.DataBento.creds.json")


class HistoricalStatsAnalyzer:
    """Analyzes historical Databento data and computes half-hour statistics."""

    def __init__(self, api_key: str, dataset: str = "XNAS.BASIC"):
        """
        Initialize the analyzer.

        Args:
            api_key: Databento API key
            dataset: Databento dataset (XNAS.BASIC or EQUS.MINI)
        """
        self.api_key = api_key
        self.client = db.Historical(key=api_key)
        self.et_tz = pytz.timezone("America/New_York")
        self.dataset = dataset

        # Select BBO schema based on dataset
        # XNAS.BASIC doesn't support bbo-1s, use cbbo-1s instead
        if dataset == "XNAS.BASIC":
            self.schema_bbo = "cbbo-1s"
        else:
            self.schema_bbo = "bbo-1s"
        self.schema_ohlcv = "ohlcv-1m"

        # Time range: 4:00 AM to 8:00 PM ET
        self.start_hour = 4
        self.end_hour = 20

    def get_half_hour_bucket(self, dt_obj: dt.datetime) -> str:
        """Get the half-hour bucket label for a given datetime."""
        hour = dt_obj.hour
        minute = 30 if dt_obj.minute >= 30 else 0
        return f"{hour:02d}:{minute:02d}"

    def generate_bucket_labels(self) -> List[str]:
        """Generate all half-hour bucket labels for the time range."""
        buckets = []
        for hour in range(self.start_hour, self.end_hour):
            buckets.append(f"{hour:02d}:00")
            buckets.append(f"{hour:02d}:30")
        return buckets

    def analyze_symbol(self, symbol: str, date_str: str) -> Dict:
        """
        Analyze a symbol for a given date.

        Args:
            symbol: Stock symbol (e.g., "NVDA")
            date_str: Date in YYYY-MM-DD format

        Returns:
            Dictionary with statistics per half-hour bucket
        """
        logger.info(f"Analyzing {symbol} for {date_str}")

        date_obj = dt.datetime.strptime(date_str, "%Y-%m-%d").date()

        # Create time range for Databento queries
        start_dt = self.et_tz.localize(dt.datetime.combine(date_obj, dt.time(self.start_hour, 0, 0)))
        end_dt = self.et_tz.localize(dt.datetime.combine(date_obj, dt.time(self.end_hour, 0, 0)))

        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

        # Fetch 1-minute OHLCV bars for volume and price data
        logger.info(f"Fetching {self.schema_ohlcv} bars from {start_dt} to {end_dt}")
        aggs = self.fetch_ohlcv(symbol, start_iso, end_iso)

        # Fetch BBO snapshots for spread data
        logger.info(f"Fetching {self.schema_bbo} data")
        quotes = self.fetch_bbo(symbol, start_iso, end_iso)

        # Bucket data by half-hour
        logger.info("Bucketing data into half-hour intervals")
        bucketed_aggs = self.bucket_data(aggs)
        bucketed_quotes = self.bucket_data(quotes)

        # Calculate statistics per bucket
        logger.info("Calculating statistics per bucket")
        stats = self.calculate_statistics(bucketed_aggs, bucketed_quotes)

        return stats

    def fetch_ohlcv(self, symbol: str, start_iso: str, end_iso: str) -> List[Dict]:
        """
        Fetch 1-minute OHLCV bars from Databento.

        Args:
            symbol: Stock symbol
            start_iso: Start time as ISO string
            end_iso: End time as ISO string

        Returns:
            List of OHLCV bar dicts
        """
        aggs = []
        try:
            data = self.client.timeseries.get_range(
                dataset=self.dataset,
                schema=self.schema_ohlcv,
                stype_in="raw_symbol",
                symbols=[symbol],
                start=start_iso,
                end=end_iso,
            )

            for msg in data:
                ts_ms = int(msg.ts_event / 1_000_000)

                open_px = float(msg.open) / DB_PX_FACTOR
                high_px = float(msg.high) / DB_PX_FACTOR
                low_px = float(msg.low) / DB_PX_FACTOR
                close_px = float(msg.close) / DB_PX_FACTOR
                volume = int(msg.volume)

                # Skip bars with zero or invalid prices
                if close_px <= 0:
                    continue

                aggs.append({
                    "timestamp_ms": ts_ms,
                    "open": open_px,
                    "high": high_px,
                    "low": low_px,
                    "close": close_px,
                    "volume": volume,
                })

            logger.info(f"Fetched {len(aggs)} OHLCV bars")
        except Exception as e:
            logger.error(f"Error fetching OHLCV data: {e}")

        return aggs

    def fetch_bbo(self, symbol: str, start_iso: str, end_iso: str) -> List[Dict]:
        """
        Fetch BBO (best bid/offer) snapshots from Databento.

        Args:
            symbol: Stock symbol
            start_iso: Start time as ISO string
            end_iso: End time as ISO string

        Returns:
            List of BBO quote dicts
        """
        quotes = []
        try:
            data = self.client.timeseries.get_range(
                dataset=self.dataset,
                schema=self.schema_bbo,
                stype_in="raw_symbol",
                symbols=[symbol],
                start=start_iso,
                end=end_iso,
            )

            for msg in data:
                ts_ms = int(msg.ts_event / 1_000_000)

                bid_px = None
                ask_px = None

                # Handle different message formats
                # mbp-1 / bbo-1s have levels array
                if hasattr(msg, 'levels') and len(msg.levels) > 0:
                    level = msg.levels[0]
                    bid_px = float(level.bid_px) / DB_PX_FACTOR if level.bid_px else None
                    ask_px = float(level.ask_px) / DB_PX_FACTOR if level.ask_px else None
                # cbbo-1s has bid_px/ask_px directly
                elif hasattr(msg, 'bid_px') and hasattr(msg, 'ask_px'):
                    bid_px = float(msg.bid_px) / DB_PX_FACTOR if msg.bid_px else None
                    ask_px = float(msg.ask_px) / DB_PX_FACTOR if msg.ask_px else None
                else:
                    continue

                if bid_px is None or ask_px is None:
                    continue
                if bid_px <= 0 or ask_px <= 0:
                    continue
                if bid_px > ask_px:  # crossed market
                    continue
                # Filter wide/stale quotes (spread > 10% of mid)
                mid = (bid_px + ask_px) / 2
                if mid > 0 and (ask_px - bid_px) / mid > 0.10:
                    continue

                quotes.append({
                    "timestamp_ms": ts_ms,
                    "bid_price": bid_px,
                    "ask_price": ask_px,
                })

            logger.info(f"Fetched {len(quotes)} BBO records")
        except Exception as e:
            logger.error(f"Error fetching BBO data: {e}")

        return quotes

    def bucket_data(self, records: List[Dict]) -> Dict[str, List[Dict]]:
        """Bucket records by half-hour intervals based on timestamp_ms."""
        bucketed = defaultdict(list)
        for rec in records:
            one_dt = dt.datetime.fromtimestamp(rec["timestamp_ms"] / 1000, tz=self.et_tz)
            bucket = self.get_half_hour_bucket(one_dt)
            bucketed[bucket].append(rec)
        return bucketed

    def calculate_statistics(self, bucketed_aggs: Dict[str, List[Dict]],
                           bucketed_quotes: Dict[str, List[Dict]]) -> Dict:
        """
        Calculate statistics for each half-hour bucket.

        Args:
            bucketed_aggs: OHLCV bars bucketed by half-hour
            bucketed_quotes: BBO quotes bucketed by half-hour

        Returns:
            Dictionary with statistics per bucket
        """
        # Calculate total daily volume for percentage calculation
        total_volume = 0
        total_notional = 0.0
        for bucket_aggs in bucketed_aggs.values():
            for agg in bucket_aggs:
                total_volume += agg["volume"]
                total_notional += agg["volume"] * agg["close"]

        # Generate all bucket labels (even if empty)
        all_buckets = self.generate_bucket_labels()

        stats = {}
        for bucket in all_buckets:
            aggs = bucketed_aggs.get(bucket, [])
            quotes = bucketed_quotes.get(bucket, [])

            if not aggs and not quotes:
                stats[bucket] = {
                    "avg_spread": None,
                    "avg_spread_bps": None,
                    "volume": 0,
                    "notional_traded": 0.0,
                    "pct_of_daily_volume": 0.0,
                }
                continue

            # Calculate spread statistics from BBO quotes
            avg_spread = None
            avg_spread_bps = None

            if quotes:
                spreads = []
                spreads_bps = []

                for quote in quotes:
                    bid = quote["bid_price"]
                    ask = quote["ask_price"]

                    if bid > 0 and ask > 0:
                        spread = ask - bid
                        spreads.append(spread)

                        mid = (bid + ask) / 2
                        if mid > 0:
                            spread_bps = (spread / mid) * 10000
                            spreads_bps.append(spread_bps)

                if spreads:
                    avg_spread = sum(spreads) / len(spreads)
                if spreads_bps:
                    avg_spread_bps = sum(spreads_bps) / len(spreads_bps)

            # Calculate volume statistics from OHLCV bars
            volume = 0
            notional = 0.0

            if aggs:
                for agg in aggs:
                    volume += agg["volume"]
                    notional += agg["volume"] * agg["close"]

            pct_of_daily = (volume / total_volume * 100) if total_volume > 0 else 0.0

            stats[bucket] = {
                "avg_spread": avg_spread,
                "avg_spread_bps": avg_spread_bps,
                "volume": volume,
                "notional_traded": notional,
                "pct_of_daily_volume": pct_of_daily,
            }

        return stats

    def print_statistics(self, symbol: str, date_str: str, stats: Dict):
        """Print statistics in a formatted table."""
        print("\n" + "="*85)
        print(f"HISTORICAL STATISTICS: {symbol} on {date_str}")
        print("="*85)
        print(f"{'Time':<10} {'Avg Spread':<12} {'Spread(bps)':<12} {'Volume':<15} {'Notional($)':<15} {'% Daily Vol':<12}")
        print("-"*85)

        for bucket in self.generate_bucket_labels():
            s = stats[bucket]

            avg_spread = f"${s['avg_spread']:.4f}" if s['avg_spread'] else "N/A"
            spread_bps = f"{s['avg_spread_bps']:.2f}" if s['avg_spread_bps'] else "N/A"
            volume = f"{s['volume']:,}" if s['volume'] else "0"
            notional = f"${s['notional_traded']:,.0f}" if s['notional_traded'] else "$0"
            pct_vol = f"{s['pct_of_daily_volume']:.2f}%" if s['pct_of_daily_volume'] else "0.00%"

            print(f"{bucket:<10} {avg_spread:<12} {spread_bps:<12} {volume:<15} {notional:<15} {pct_vol:<12}")

        print("="*85)

    def aggregate_multi_day_stats(self, all_stats: List[Dict]) -> Dict:
        """
        Aggregate statistics across multiple days.

        Args:
            all_stats: List of statistics dictionaries, one per day

        Returns:
            Aggregated statistics dictionary
        """
        aggregated = {}
        all_buckets = self.generate_bucket_labels()

        for bucket in all_buckets:
            spreads = []
            spreads_bps = []
            volumes = []
            notionals = []

            for day_stats in all_stats:
                if bucket in day_stats:
                    s = day_stats[bucket]

                    if s['avg_spread'] is not None:
                        spreads.append(s['avg_spread'])
                    if s['avg_spread_bps'] is not None:
                        spreads_bps.append(s['avg_spread_bps'])
                    if s['volume'] is not None:
                        volumes.append(s['volume'])
                    if s['notional_traded'] is not None:
                        notionals.append(s['notional_traded'])

            total_volume = sum(volumes) if volumes else 0
            total_notional = sum(notionals) if notionals else 0

            aggregated[bucket] = {
                "avg_spread": sum(spreads) / len(spreads) if spreads else None,
                "avg_spread_bps": sum(spreads_bps) / len(spreads_bps) if spreads_bps else None,
                "total_volume": total_volume,
                "total_notional_traded": total_notional,
                "avg_volume": sum(volumes) / len(volumes) if volumes else 0,
            }

        # Calculate percentage of total volume for each bucket
        total_all_volume = sum(s["total_volume"] for s in aggregated.values())
        for bucket in all_buckets:
            if total_all_volume > 0:
                aggregated[bucket]["pct_of_total_volume"] = (
                    aggregated[bucket]["total_volume"] / total_all_volume * 100
                )
            else:
                aggregated[bucket]["pct_of_total_volume"] = 0.0

        return aggregated

    def print_aggregated_statistics(self, symbol: str, start_date: str, end_date: str, stats: Dict, num_days: int):
        """Print aggregated statistics in a formatted table."""
        print("\n" + "="*85)
        print(f"AGGREGATED HISTORICAL STATISTICS: {symbol} from {start_date} to {end_date} ({num_days} days)")
        print("="*85)
        print(f"{'Time':<10} {'Avg Spread':<12} {'Spread(bps)':<12} {'Total Vol':<15} {'Notional($)':<15} {'% Total Vol':<12}")
        print("-"*85)

        for bucket in self.generate_bucket_labels():
            s = stats[bucket]

            avg_spread = f"${s['avg_spread']:.4f}" if s['avg_spread'] else "N/A"
            spread_bps = f"{s['avg_spread_bps']:.2f}" if s['avg_spread_bps'] else "N/A"
            volume = f"{s['total_volume']:,}" if s['total_volume'] else "0"
            notional = f"${s['total_notional_traded']:,.0f}" if s['total_notional_traded'] else "$0"
            pct_vol = f"{s['pct_of_total_volume']:.2f}%" if s['pct_of_total_volume'] else "0.00%"

            print(f"{bucket:<10} {avg_spread:<12} {spread_bps:<12} {volume:<15} {notional:<15} {pct_vol:<12}")

        print("="*85)

    def save_statistics(self, symbol: str, date_str: str, stats: Dict, output_file: str):
        """Save statistics to JSON file."""
        output = {
            "symbol": symbol,
            "date": date_str,
            "statistics": stats
        }

        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2)

        logger.info(f"Statistics saved to {output_file}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze historical Databento data with half-hour statistics",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="Stock symbol to analyze (e.g., NVDA)"
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="Start date in YYYY-MM-DD format (e.g., 2024-10-20)"
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="End date in YYYY-MM-DD format (e.g., 2024-10-24)"
    )
    parser.add_argument(
        "--dataset",
        default="XNAS.BASIC",
        choices=["XNAS.BASIC", "EQUS.MINI"],
        help="Databento dataset (XNAS.BASIC = NASDAQ direct, EQUS.MINI = SIP consolidated)"
    )
    parser.add_argument(
        "--output",
        help="Output file to save results (JSON format)"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )

    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Load Databento API key
    try:
        with open(DB_SECRET_FILE_PATH, "r") as f:
            secrets = json.load(f)
            api_key = secrets["api_key"]
    except FileNotFoundError:
        logger.error(f"Credentials file not found at {DB_SECRET_FILE_PATH}")
        sys.exit(1)
    except KeyError:
        logger.error(f"'api_key' not found in {DB_SECRET_FILE_PATH}")
        sys.exit(1)

    # Parse date range
    start_date = dt.datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_date = dt.datetime.strptime(args.end_date, "%Y-%m-%d").date()

    if start_date > end_date:
        logger.error("Start date must be before or equal to end date")
        sys.exit(1)

    # Create analyzer
    analyzer = HistoricalStatsAnalyzer(api_key, dataset=args.dataset)

    # Collect statistics for each day in the range
    all_stats = []
    current_date = start_date

    logger.info(f"Analyzing {args.symbol} from {args.start_date} to {args.end_date} (dataset: {args.dataset})")

    while current_date <= end_date:
        date_str = current_date.strftime("%Y-%m-%d")
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing {date_str}")
        logger.info(f"{'='*60}")

        try:
            stats = analyzer.analyze_symbol(args.symbol, date_str)
            all_stats.append(stats)
        except Exception as e:
            logger.error(f"Error analyzing {date_str}: {e}")

        current_date += dt.timedelta(days=1)

    if not all_stats:
        logger.error("No statistics collected for any day")
        sys.exit(1)

    logger.info(f"\nAggregating statistics across {len(all_stats)} days...")
    aggregated_stats = analyzer.aggregate_multi_day_stats(all_stats)

    # Print aggregated results
    analyzer.print_aggregated_statistics(args.symbol, args.start_date, args.end_date, aggregated_stats, len(all_stats))

    # Save to file if requested
    if args.output:
        output = {
            "symbol": args.symbol,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "num_days": len(all_stats),
            "dataset": args.dataset,
            "aggregated_statistics": aggregated_stats
        }

        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2)

        logger.info(f"Aggregated statistics saved to {args.output}")


if __name__ == "__main__":
    main()
