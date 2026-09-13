#!/usr/bin/env python
"""
Polygon Historical Statistics Analyzer

Fetches historical data from Polygon and computes aggregated statistics
per half-hour bucket for US equity symbols over a date range.

For a given symbol and date range, calculates aggregated statistics:
- Average bid-ask spread (averaged across days)
- Average bid/ask (averaged across days)
- Volume (total across all days and as % of total volume)
- High and low price (max/min across all days)

Time range: 4:00 AM to 8:00 PM ET (full trading day including pre/post market)

Quote sampling: Fetches quotes from only the first N minutes of each half-hour
bucket (default: 2 minutes) for performance.

Sample command line. 

./polygon_historical_stats.py --symbol NVDA --start-date 2025-10-04 --end-date 2025-11-04 --quote-sample-minutes 3 > ~/tradefi/retraded/overmind/for_live/per_eq_stats/nvda_stats.txt

"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

import datetime as dt

from typing import List, Dict, Tuple

import pytz

# Add parent directories to path for imports
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from polygon import RESTClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HistoricalStatsAnalyzer:
    """Analyzes historical Polygon data and computes half-hour statistics."""

    def __init__(self, api_key: str, quote_sample_minutes: int = 2):
        """
        Initialize the analyzer.

        Args:
            api_key: Polygon.io API key
            quote_sample_minutes: Number of minutes to sample quotes from each half-hour bucket (default: 2)
        """
        self.api_key = api_key
        self.client = RESTClient(api_key)
        self.et_tz = pytz.timezone("America/New_York")
        self.quote_sample_minutes = quote_sample_minutes

        # Time range: 4:00 AM to 8:00 PM ET
        self.start_hour = 4
        self.end_hour = 20

    def get_half_hour_bucket(self, dt_obj: dt.datetime) -> str:
        """
        Get the half-hour bucket for a given datetime.

        Args:
            dt_obj: Datetime in ET timezone

        Returns:
            String representation of bucket (e.g., "09:30")
        """
        hour = dt_obj.hour
        minute = 30 if dt_obj.minute >= 30 else 0
        return f"{hour:02d}:{minute:02d}"

    def generate_bucket_labels(self) -> List[str]:
        """
        Generate all half-hour bucket labels for the time range.

        Returns:
            List of bucket labels (e.g., ["04:00", "04:30", "05:00", ...])
        """
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
            Dictionary with analysis results
        """
        logger.info(f"Analyzing {symbol} for {date_str}")

        # Parse date
        date_obj = dt.datetime.strptime(date_str, "%Y-%m-%d").date()

        # Get start and end timestamps in milliseconds
        start_dt = self.et_tz.localize(dt.datetime.combine(date_obj, dt.time(self.start_hour, 0, 0)))
        end_dt = self.et_tz.localize(dt.datetime.combine(date_obj, dt.time(self.end_hour, 0, 0)))

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        # Fetch 1-minute aggregates (bars) for volume and price data
        logger.info(f"Fetching 1-minute aggregates from {start_dt} to {end_dt}")
        aggs = self.fetch_aggregates(symbol, start_ms, end_ms)

        # Bucket the aggregates by half-hour
        logger.info("Bucketing aggregates into half-hour intervals")
        bucketed_aggs = self.bucket_aggregates(aggs)

        # Fetch sampled quotes for spread data (much faster than fetching all quotes)
        logger.info(f"Fetching sampled quotes ({self.quote_sample_minutes} min per bucket)")
        bucketed_quotes = self.fetch_sampled_quotes(symbol, date_obj)

        # Calculate statistics per bucket
        logger.info("Calculating statistics per bucket")
        stats = self.calculate_statistics(bucketed_aggs, bucketed_quotes)

        return stats

    def fetch_aggregates(self, symbol: str, start_ms: int, end_ms: int) -> List[Dict]:
        """
        Fetch 1-minute aggregates from Polygon.

        Args:
            symbol: Stock symbol
            start_ms: Start timestamp in milliseconds
            end_ms: End timestamp in milliseconds

        Returns:
            List of aggregate bars
        """
        aggs = []
        try:
            # Fetch 1-minute bars
            for agg in self.client.list_aggs(
                ticker=symbol,
                multiplier=1,
                timespan="minute",
                from_=start_ms,
                to=end_ms,
                limit=50000
            ):
                aggs.append({
                    "timestamp": agg.timestamp,
                    "open": agg.open,
                    "high": agg.high,
                    "low": agg.low,
                    "close": agg.close,
                    "volume": agg.volume,
                    "vwap": agg.vwap if hasattr(agg, 'vwap') else None,
                    "transactions": agg.transactions if hasattr(agg, 'transactions') else None,
                })

            logger.info(f"Fetched {len(aggs)} aggregate bars")
        except Exception as e:
            logger.error(f"Error fetching aggregates: {e}")

        return aggs

    def fetch_sampled_quotes(self, symbol: str, date_obj) -> Dict[str, List[Dict]]:
        """
        Fetch quotes sampled from the first N minutes of each half-hour bucket.
        This is much faster than fetching all quotes for the entire day.

        Args:
            symbol: Stock symbol
            date_obj: Date object

        Returns:
            Dictionary mapping bucket labels to lists of quotes
        """
        bucketed_quotes = defaultdict(list)

        # Generate all bucket labels
        bucket_labels = self.generate_bucket_labels()

        for bucket_label in bucket_labels:
            # Parse the bucket start time (e.g., "09:30" -> hour=9, minute=30)
            hour, minute = map(int, bucket_label.split(':'))

            # Create start and end times for the sample window
            sample_start = self.et_tz.localize(dt.datetime.combine(date_obj, dt.time(hour, minute, 0)))
            sample_end = sample_start + dt.timedelta(minutes=self.quote_sample_minutes)

            start_ms = int(sample_start.timestamp() * 1000)
            end_ms = int(sample_end.timestamp() * 1000)

            # Fetch quotes for this sample window
            quotes = []
            try:
                for quote in self.client.list_quotes(
                    ticker=symbol,
                    timestamp_gte=start_ms * 1_000_000,  # Convert to nanoseconds
                    timestamp_lt=end_ms * 1_000_000,
                    limit=50000
                ):
                    # Convert nanoseconds to milliseconds
                    timestamp_ms = quote.sip_timestamp // 1_000_000

                    quotes.append({
                        "timestamp": timestamp_ms,
                        "bid_price": quote.bid_price,
                        "ask_price": quote.ask_price,
                        "bid_size": quote.bid_size,
                        "ask_size": quote.ask_size,
                    })

                if quotes:
                    logger.debug(f"Bucket {bucket_label}: Fetched {len(quotes)} quotes")

            except Exception as e:
                logger.error(f"Error fetching quotes for bucket {bucket_label}: {e}")

            bucketed_quotes[bucket_label] = quotes

        total_quotes = sum(len(quotes) for quotes in bucketed_quotes.values())
        logger.info(f"Fetched {total_quotes} total quotes across all sample windows")

        return bucketed_quotes

    def bucket_aggregates(self, aggs: List[Dict]) -> Dict[str, List[Dict]]:
        """
        Bucket aggregates by half-hour intervals.

        Args:
            aggs: List of aggregate bars

        Returns:
            Dictionary mapping bucket labels to lists of bars
        """
        bucketed = defaultdict(list)

        for agg in aggs:
            one_dt = dt.datetime.fromtimestamp(agg["timestamp"] / 1000, tz=self.et_tz)
            bucket = self.get_half_hour_bucket(one_dt)
            bucketed[bucket].append(agg)

        return bucketed

    def calculate_statistics(self, bucketed_aggs: Dict[str, List[Dict]],
                           bucketed_quotes: Dict[str, List[Dict]]) -> Dict:
        """
        Calculate statistics for each half-hour bucket.

        Args:
            bucketed_aggs: Aggregates bucketed by half-hour
            bucketed_quotes: Quotes bucketed by half-hour

        Returns:
            Dictionary with statistics per bucket
        """
        # Calculate total daily volume for percentage calculation
        total_volume = 0
        total_notional = 0.0
        for bucket_aggs in bucketed_aggs.values():
            for agg in bucket_aggs:
                total_volume += agg["volume"]
                total_notional += agg["volume"] * agg["vwap"] if agg["vwap"] else agg["volume"] * agg["close"]

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

            # Calculate spread statistics from quotes
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

                        # Calculate spread in basis points (bps)
                        mid = (bid + ask) / 2
                        if mid > 0:
                            spread_bps = (spread / mid) * 10000
                            spreads_bps.append(spread_bps)

                if spreads:
                    avg_spread = sum(spreads) / len(spreads)
                if spreads_bps:
                    avg_spread_bps = sum(spreads_bps) / len(spreads_bps)

            # Calculate volume statistics from aggregates
            volume = 0
            notional = 0.0

            if aggs:
                for agg in aggs:
                    volume += agg["volume"]

                    # Calculate notional using VWAP if available, else close
                    price = agg["vwap"] if agg["vwap"] else agg["close"]
                    notional += agg["volume"] * price

            # Calculate percentage of daily volume
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
        """
        Print statistics in a formatted table.

        Args:
            symbol: Stock symbol
            date_str: Date string
            stats: Statistics dictionary
        """
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
            # Collect all values for this bucket across all days
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

            # Calculate aggregated statistics
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
        """
        Print aggregated statistics in a formatted table.

        Args:
            symbol: Stock symbol
            start_date: Start date string
            end_date: End date string
            stats: Aggregated statistics dictionary
            num_days: Number of days in the range
        """
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
        """
        Save statistics to JSON file.

        Args:
            symbol: Stock symbol
            date_str: Date string
            stats: Statistics dictionary
            output_file: Output file path
        """
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
        description="Analyze historical Polygon data with half-hour statistics",
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
        "--output",
        help="Output file to save results (JSON format)"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )
    parser.add_argument(
        "--quote-sample-minutes",
        type=int,
        default=2,
        help="Number of minutes to sample quotes from each half-hour bucket (default: 2)"
    )

    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Load Polygon API key
    polygon_creds_file = os.path.expanduser("~/.creds/.Polygon.creds.json")
    try:
        with open(polygon_creds_file, "r") as f:
            secrets = json.load(f)
            api_key = secrets["api_key"]
    except FileNotFoundError:
        logger.error(f"Credentials file not found at {polygon_creds_file}")
        sys.exit(1)
    except KeyError:
        logger.error(f"'api_key' not found in {polygon_creds_file}")
        sys.exit(1)

    # Parse date range
    start_date = dt.datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_date = dt.datetime.strptime(args.end_date, "%Y-%m-%d").date()

    # Validate date range
    if start_date > end_date:
        logger.error("Start date must be before or equal to end date")
        sys.exit(1)

    # Create analyzer
    analyzer = HistoricalStatsAnalyzer(api_key, quote_sample_minutes=args.quote_sample_minutes)

    # Collect statistics for each day in the range
    all_stats = []
    current_date = start_date

    logger.info(f"Analyzing {args.symbol} from {args.start_date} to {args.end_date}")

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

        # Move to next day
        current_date += dt.timedelta(days=1)

    # Aggregate statistics across all days
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
            "aggregated_statistics": aggregated_stats
        }

        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2)

        logger.info(f"Aggregated statistics saved to {args.output}")


if __name__ == "__main__":
    main()
