#!/usr/bin/env python3
"""
Historical Closing Volume Analyzer using Polygon API

Fetches historical trade data or aggregates from Polygon for a specific day
and analyzes closing print volume for US equities.

This script can analyze closing volume using:
- Tick-by-tick trades
- 1-second aggregates
- 1-minute aggregates
- Custom timeframe aggregates

Usage examples:
  # Analyze TSLA closing volume on 2024-10-25 using 1-second bars
  ./hist_polygon_volumes.py --symbol TSLA --date 2024-10-25 --mode aggs --timespan 1s

  # Analyze using tick-by-tick trades
  ./hist_polygon_volumes.py --symbol TSLA --date 2024-10-25 --mode trades

  # Analyze using 1-minute aggregates
  ./hist_polygon_volumes.py --symbol TSLA --date 2024-10-25 --mode aggs --timespan 1m

  This is kind of a throwaway script to check what kind of data we get from polygon. 
  My takeaway here is that the 1s bars do NOT give us the closing print - it might be filtered out
  or completely unreported. OK, cool. 
  
  """

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, time as dt_time
from typing import List, Dict

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


class ClosingVolumeAnalyzer:
    """Analyzes historical closing volume using Polygon API."""

    def __init__(self, api_key: str):
        """
        Initialize the analyzer.

        Args:
            api_key: Polygon.io API key
        """
        self.api_key = api_key
        self.client = RESTClient(api_key)
        self.et_tz = pytz.timezone("America/New_York")

        # Define closing window (typically 3:59-4:02 PM ET to catch late prints)
        self.closing_window_start = dt_time(15, 59, 0)
        self.closing_window_end = dt_time(16, 2, 0)

    def is_in_closing_window(self, timestamp_ms: int) -> bool:
        """
        Check if timestamp falls within the closing window.

        Args:
            timestamp_ms: Unix timestamp in milliseconds

        Returns:
            True if within closing window
        """
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.et_tz)
        bar_time = dt.time()
        return self.closing_window_start <= bar_time <= self.closing_window_end

    def analyze_with_trades(self, symbol: str, date_str: str) -> Dict:
        """
        Analyze closing volume using tick-by-tick trades.

        Args:
            symbol: Stock symbol (e.g., "TSLA")
            date_str: Date in YYYY-MM-DD format

        Returns:
            Dictionary with analysis results
        """
        logger.info(f"Analyzing {symbol} on {date_str} using tick-by-tick trades")

        # Parse date and create time window
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Extended window: 9:00 AM to 5:00 PM ET
        start_dt = self.et_tz.localize(datetime.combine(date_obj, dt_time(15, 50, 0)))
        end_dt = self.et_tz.localize(datetime.combine(date_obj, dt_time(16, 10, 0)))

        start_ns = int(start_dt.timestamp() * 1_000_000_000)
        end_ns = int(end_dt.timestamp() * 1_000_000_000)

        logger.info(f"Fetching trades from {start_dt} to {end_dt}")

        # Fetch trades
        all_trades = []
        closing_trades = []
        total_volume = 0
        closing_volume = 0
        total_notional = 0
        closing_notional = 0

        try:
            x = 0
            for trade in self.client.list_trades(
                ticker=symbol,
                timestamp_gte=start_ns,
                timestamp_lt=end_ns,
                limit=50000
            ):
                x += 1
                # Convert nanoseconds to milliseconds
                timestamp_ms = trade.sip_timestamp // 1_000_000
                size = trade.size
                price = trade.price

                all_trades.append({
                    "timestamp": timestamp_ms,
                    "price": price,
                    "size": size,
                })


                total_volume += size
                total_notional += size * price

                # Check if in closing window
                if self.is_in_closing_window(timestamp_ms):
                    closing_trades.append({
                        "timestamp": timestamp_ms,
                        "price": price,
                        "size": size,
                    })
                    closing_volume += size
                    closing_notional += size * price

                dt_str = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.et_tz).strftime('%H:%M:%S.%f')[:-3]
                print("timestamp {} trd {} @ {}  closing_trds_len {}".format(dt_str, size, trade.price, len(closing_trades)))

            logger.info(f"Fetched {len(all_trades)} total trades")
            logger.info(f"Found {len(closing_trades)} trades in closing window")

            # Sort trades by timestamp (ascending) since API may return in reverse order
            all_trades.sort(key=lambda x: x['timestamp'])
            closing_trades.sort(key=lambda x: x['timestamp'])

        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
            return {}

        # Calculate statistics
        closing_pct = (closing_volume / total_volume * 100) if total_volume > 0 else 0
        avg_price = total_notional / total_volume if total_volume > 0 else 0
        closing_avg_price = closing_notional / closing_volume if closing_volume > 0 else 0

        return {
            "symbol": symbol,
            "date": date_str,
            "mode": "trades",
            "total_trades": len(all_trades),
            "total_volume": total_volume,
            "total_notional": total_notional,
            "avg_price": avg_price,
            "closing_trades": len(closing_trades),
            "closing_volume": closing_volume,
            "closing_notional": closing_notional,
            "closing_avg_price": closing_avg_price,
            "closing_pct_of_day": closing_pct,
            "trades_detail": closing_trades[-25:] if closing_trades else []  # Last 25 for inspection
        }

    def analyze_with_aggregates(self, symbol: str, date_str: str,
                               timespan: str = "second", multiplier: int = 1) -> Dict:
        """
        Analyze closing volume using aggregate bars.

        Args:
            symbol: Stock symbol (e.g., "TSLA")
            date_str: Date in YYYY-MM-DD format
            timespan: Timespan for aggregates ("second", "minute", "hour")
            multiplier: Multiplier for timespan (e.g., 1 for 1-second bars)

        Returns:
            Dictionary with analysis results
        """
        logger.info(f"Analyzing {symbol} on {date_str} using {multiplier}-{timespan} aggregates")

        # Parse date and create time window
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Extended window: 9:00 AM to 5:00 PM ET
        start_dt = self.et_tz.localize(datetime.combine(date_obj, dt_time(15, 50, 0)))
        end_dt = self.et_tz.localize(datetime.combine(date_obj, dt_time(16, 10, 0)))

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        logger.info(f"Fetching aggregates from {start_dt} to {end_dt}")

        # Fetch aggregates
        all_bars = []
        closing_bars = []
        total_volume = 0
        closing_volume = 0
        total_notional = 0
        closing_notional = 0

        try:
            for agg in self.client.list_aggs(
                ticker=symbol,
                multiplier=multiplier,
                timespan=timespan,
                from_=start_ms,
                to=end_ms,
                limit=50000
            ):
                bar_data = {
                    "timestamp": agg.timestamp,
                    "open": agg.open,
                    "high": agg.high,
                    "low": agg.low,
                    "close": agg.close,
                    "volume": agg.volume,
                    "vwap": agg.vwap if hasattr(agg, 'vwap') else None,
                    "transactions": agg.transactions if hasattr(agg, 'transactions') else None,
                }

                all_bars.append(bar_data)
                total_volume += agg.volume

                # Calculate notional using VWAP if available, else close
                price = agg.vwap if hasattr(agg, 'vwap') and agg.vwap else agg.close
                total_notional += agg.volume * price

                # Check if in closing window
                if self.is_in_closing_window(agg.timestamp):
                    closing_bars.append(bar_data)
                    closing_volume += agg.volume
                    closing_notional += agg.volume * price

            logger.info(f"Fetched {len(all_bars)} total bars")
            logger.info(f"Found {len(closing_bars)} bars in closing window")

            # Sort bars by timestamp (ascending) since API may return in reverse order
            all_bars.sort(key=lambda x: x['timestamp'])
            closing_bars.sort(key=lambda x: x['timestamp'])

        except Exception as e:
            logger.error(f"Error fetching aggregates: {e}")
            return {}

        # Calculate statistics
        closing_pct = (closing_volume / total_volume * 100) if total_volume > 0 else 0
        avg_px = closing_notional / closing_volume if closing_volume > 0 else 0

        return {
            "symbol": symbol,
            "date": date_str,
            "mode": f"{multiplier}-{timespan} aggregates",
            "total_bars": len(all_bars),
            "total_volume": total_volume,
            "total_notional": total_notional,
            "closing_bars": len(closing_bars),
            "closing_volume": closing_volume,
            "closing_notional": closing_notional,
            "closing_avg_price": avg_px,
            "closing_pct_of_day": closing_pct,
            "bars_detail": closing_bars  # All closing bars for inspection
        }

    def print_results(self, results: Dict):
        """
        Print results in a formatted way.

        Args:
            results: Analysis results dictionary
        """
        if not results:
            logger.error("No results to display")
            return

        print("\n" + "="*80)
        print(f"CLOSING VOLUME ANALYSIS: {results['symbol']} on {results['date']}")
        print(f"Analysis Mode: {results['mode']}")
        print("="*80)

        if "total_trades" in results:
            # Trade-based analysis
            print(f"\nTotal Trades (Full Day): {results['total_trades']:,}")
            print(f"Total Volume (Full Day): {results['total_volume']:,}")
            print(f"Total Notional (Full Day): ${results['total_notional']:,.2f}")
            print(f"Average Price (Full Day): ${results['avg_price']:.2f}")
            print(f"\nClosing Window Trades: {results['closing_trades']:,}")
            print(f"Closing Window Volume: {results['closing_volume']:,}")
            print(f"Closing Window Notional: ${results['closing_notional']:,.2f}")
            print(f"Closing Avg Price: ${results['closing_avg_price']:.2f}")
            print(f"Closing as % of Day: {results['closing_pct_of_day']:.2f}%")

            if results['trades_detail']:
                print(f"\nLast {min(len(results['trades_detail']), 10)} Closing Trades:")
                for trade in results['trades_detail'][-10:]:  # Show last 10
                    dt = datetime.fromtimestamp(trade['timestamp'] / 1000, tz=self.et_tz)
                    print(f"  {dt.strftime('%H:%M:%S.%f')[:-3]} | "
                          f"${trade['price']:.2f} | Size: {trade['size']:,}")

        elif "total_bars" in results:
            # Aggregate-based analysis
            print(f"\nTotal Bars (Full Day): {results['total_bars']:,}")
            print(f"Total Volume (Full Day): {results['total_volume']:,}")
            print(f"Total Notional (Full Day): ${results['total_notional']:,.2f}")
            print(f"\nClosing Window Bars: {results['closing_bars']:,}")
            print(f"Closing Window Volume: {results['closing_volume']:,}")
            print(f"Closing Window Notional: ${results['closing_notional']:,.2f}")
            print(f"Closing Avg Price: ${results['closing_avg_price']:.2f}")
            print(f"Closing as % of Day Volume: {results['closing_pct_of_day']:.2f}%")

            if results['bars_detail']:
                print(f"\nClosing Window Bars Detail ({len(results['bars_detail'])} bars):")
                print(f"{'Time':<12} {'Open':<10} {'High':<10} {'Low':<10} {'Close':<10} {'Volume':<12} {'VWAP':<10}")
                print("-"*80)
                for bar in results['bars_detail']:
                    dt = datetime.fromtimestamp(bar['timestamp'] / 1000, tz=self.et_tz)
                    vwap_str = f"${bar['vwap']:.2f}" if bar['vwap'] else "N/A"
                    vol_str = f"{bar['volume']:,}"
                    print(f"{dt.strftime('%H:%M:%S'):<12} "
                          f"${bar['open']:<9.2f} "
                          f"${bar['high']:<9.2f} "
                          f"${bar['low']:<9.2f} "
                          f"${bar['close']:<9.2f} "
                          f"{vol_str:<12} "
                          f"{vwap_str:<10}")

        print("="*80)

    def save_results(self, results: Dict, output_file: str):
        """
        Save results to JSON file.

        Args:
            results: Analysis results dictionary
            output_file: Output file path
        """
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info(f"Results saved to {output_file}")


def parse_timespan_arg(timespan_str: str) -> tuple:
    """
    Parse timespan argument like "1s", "5m", "1h" into multiplier and timespan.

    Args:
        timespan_str: String like "1s", "5m", "1h"

    Returns:
        Tuple of (multiplier, timespan)
    """
    import re
    match = re.match(r'(\d+)([smh])', timespan_str.lower())
    if not match:
        raise ValueError(f"Invalid timespan format: {timespan_str}. Use format like '1s', '5m', '1h'")

    multiplier = int(match.group(1))
    unit = match.group(2)

    unit_map = {
        's': 'second',
        'm': 'minute',
        'h': 'hour'
    }

    return multiplier, unit_map[unit]


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze historical closing print volume using Polygon API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze TSLA on 2024-10-25 using 1-second bars
  %(prog)s --symbol TSLA --date 2024-10-25 --mode aggs --timespan 1s

  # Analyze using tick-by-tick trades
  %(prog)s --symbol TSLA --date 2024-10-25 --mode trades

  # Analyze using 1-minute aggregates
  %(prog)s --symbol TSLA --date 2024-10-25 --mode aggs --timespan 1m
        """
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="Stock symbol to analyze (e.g., TSLA, SPY)"
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Date to analyze in YYYY-MM-DD format (e.g., 2024-10-25)"
    )
    parser.add_argument(
        "--mode",
        default="aggs",
        choices=["trades", "aggs"],
        help="Analysis mode: 'trades' for tick-by-tick, 'aggs' for aggregates (default: aggs)"
    )
    parser.add_argument(
        "--timespan",
        default="1s",
        help="Timespan for aggregates (e.g., 1s, 5s, 1m, 5m) - only used with --mode aggs"
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

    # Create analyzer
    analyzer = ClosingVolumeAnalyzer(api_key)

    # Run analysis
    try:
        if args.mode == "trades":
            results = analyzer.analyze_with_trades(args.symbol, args.date)
        else:  # aggs
            multiplier, timespan = parse_timespan_arg(args.timespan)
            results = analyzer.analyze_with_aggregates(
                args.symbol, args.date, timespan=timespan, multiplier=multiplier
            )

        # Print results
        analyzer.print_results(results)

        # Save to file if requested
        if args.output:
            analyzer.save_results(results, args.output)

    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
