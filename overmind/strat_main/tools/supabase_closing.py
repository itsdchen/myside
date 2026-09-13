#!/usr/bin/env python
"""
Polygon Closing Volume Analyzer

Consumes 1-minute aggregate bars published by the shared market-data feed
(delivered over ZMQ) to identify large closing prints near 4:00 PM ET.

This script collects 1-minute volume data and identifies the
closing auction volume, which typically shows up as a large
print in the last minute of trading.

It writes this to the db, and then postmarket traders can use it to inform themselves how to trade. 

"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
import datetime as dt
from typing import List

import pytz
import zmq
import zmq.asyncio

# Add parent directories to path for imports
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

import mdmsg_pb2
from util import sbcreds

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ClosingVolumeAnalyzer:
    """Analyzes 1-minute aggregate volume data to identify closing prints."""

    def __init__(self, symbols: List[str], output_file: str = None, credsfile: str = None):
        """
        Initialize the closing volume analyzer.

        Args:
            symbols: List of symbols to track (e.g., ["SPY", "QQQ"])
            output_file: Optional file path to save results
            credsfile: Optional Supabase credentials file for database upload
        """
        self.symbols = symbols
        self.output_file = output_file
        self._credsfile = credsfile
        self.zmq_endpoints = ["ipc:///tmp/feed-sock", "ipc:///tmp/feed-sock-cpp"]

        # ET timezone for market hours
        self.et_tz = pytz.timezone("America/New_York")

        # Storage for minute bars: {symbol: [{timestamp, open, high, low, close, volume, ...}, ...]}
        self.minute_bars = defaultdict(list)

        # Track closing prints: {symbol: {timestamp, volume, close_price, ...}}
        self.closing_prints = {}

        # Market close time (4:02 PM ET - extended to catch late prints)
        self.market_close = dt.time(16, 2, 0)

        # Time window to look for closing print (e.g., 3:58-4:02 PM)
        self.closing_window_start = dt.time(15, 58, 0)

        # Auto-shutdown flag
        self.should_stop = False
        self.zmq_context = zmq.asyncio.Context()
        self.zmq_socket = None

        # Just a tracker just in case. 
        self.n_seen = 0


    def is_near_close(self, timestamp_ms: int) -> bool:
        """
        Check if the given timestamp is near market close (3:58-4:02 PM ET).

        Args:
            timestamp_ms: Unix timestamp in milliseconds

        Returns:
            True if within the closing window
        """
        one_dt = dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=self.et_tz)
        bar_time = one_dt.time()

        return self.closing_window_start <= bar_time <= self.market_close

    def is_past_closing_window(self) -> bool:
        """
        Check if current wall-clock time is past the closing window (after 4:02 PM ET).

        Returns:
            True if current time is past the closing window
        """
        # Get current time in ET timezone (same pattern as is_near_close)
        one_dt = dt.datetime.fromtimestamp(time.time(), tz=self.et_tz)
        current_time = one_dt.time()

        return current_time > self.market_close

    def process_pb_message(self, pb_msg: mdmsg_pb2.PbMessage):
        """Process a protobuf market data message coming from the ZMQ feed."""

        if self.is_past_closing_window():
            if not self.should_stop:
                logger.info("Closing window has passed (after 4:02 PM ET). Shutting down...")
                self.should_stop = True
            return


        symbol = pb_msg.symbol_id
        if symbol not in self.symbols:
            return

        if not pb_msg.HasField("book_trade"):
            return

        if pb_msg.market != mdmsg_pb2.PBMARKET_TOPBOOK_EQUITY:
            return

        trade = pb_msg.book_trade

        # Convert numeric payload safely; Polygon aggregates are published as strings
        try:
            qty = float(trade.qty) if trade.qty else 0.0
        except ValueError:
            logger.debug(f"Skipping trade with non-numeric volume for {symbol}: {trade.qty}")
            return

        try:
            px = float(trade.px) if trade.px else None
        except ValueError:
            px = None

        timestamp_ms = int(trade.trade_time) if trade.trade_time else int(time.time() * 1000)

        one_trd = {
            "symbol": symbol,
            "start_timestamp": timestamp_ms,
            "qty": qty,
            "px": px,
            "transactions": None,
        }

        self.process_aggregate(one_trd)

    def setup_zmq_subscription(self):
        """Connect to the shared market-data ZMQ feed."""
        try:
            self.zmq_socket = self.zmq_context.socket(zmq.SUB)
            for zmq_endpoint in self.zmq_endpoints:
                self.zmq_socket.connect(zmq_endpoint)
                logger.info(f"Connected to ZMQ endpoint: {zmq_endpoint}")
            self.zmq_socket.setsockopt_string(zmq.SUBSCRIBE, "")
        except Exception as exc:
            logger.error(f"Failed to set up ZMQ subscription: {exc}")
            raise

    def handle_raw_message(self, raw_message: bytes):
        """Parse the raw protobuf payload and dispatch for processing."""
        try:
            pb_msg = mdmsg_pb2.PbMessage()
            pb_msg.ParseFromString(raw_message)
        except Exception as exc:
            logger.error(f"Failed to parse protobuf message: {exc}")
            return

        self.process_pb_message(pb_msg)

    async def message_loop(self):
        """Main loop for consuming ZMQ messages until the closing window ends."""
        logger.info("Starting ZMQ message loop")

        while not self.should_stop:
            try:
                if self.is_past_closing_window():
                    if not self.should_stop:
                        logger.info("Closing window has passed (after 4:02 PM ET). Shutting down...")
                        self.should_stop = True
                    break

                if await self.zmq_socket.poll(timeout=1000):
                    raw_message = await self.zmq_socket.recv()
                    self.handle_raw_message(raw_message)
            except asyncio.CancelledError:
                logger.info("Message loop cancelled")
                raise
            except Exception as exc:
                logger.error(f"Error while processing ZMQ message: {exc}")
                await asyncio.sleep(1)

    def cleanup(self):
        """Release ZMQ resources."""
        logger.info("Cleaning up resources")

        if self.zmq_socket:
            try:
                self.zmq_socket.close(0)
            except Exception as exc:
                logger.debug(f"Error while closing ZMQ socket: {exc}")
            finally:
                self.zmq_socket = None

        if self.zmq_context:
            try:
                self.zmq_context.term()
            except Exception as exc:
                logger.debug(f"Error while terminating ZMQ context: {exc}")
            finally:
                self.zmq_context = None

    def process_aggregate(self, bar: dict):
        """Process a single minute bar extracted from the market data feed."""

        symbol = bar["symbol"]
        timestamp_ms = bar["start_timestamp"]
        qty_raw = bar.get("qty", 0)
        px = bar.get("px", 0)

        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            qty = 0

        # Convert timestamp to ET
        dt_et = dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=self.et_tz)
        time_str = dt_et.strftime("%Y-%m-%d %H:%M:%S %Z")

        # Create bar data
        bar_data = {
            "timestamp": timestamp_ms,
            "time_str": time_str,
            "qty": qty,
            "px": px,
        }


        # Store the bar
        self.minute_bars[symbol].append(bar_data)

        # Check if this is near market close
        is_closing = self.is_near_close(timestamp_ms)

        qty_str = f"{qty:,}" if qty else "0"
        vwap_str = f"${px:.2f}" if isinstance(px, (int, float)) else "n/a"

        # Log the bar
        if self.n_seen % 100 == 0:
            logger.info(
                f"{symbol} | {time_str} | Volume: {qty_str}  | "
                f"VWAP: {vwap_str} | {'CLOSING WINDOW' if is_closing else ''}"
            )

        # If this is in the closing window, check if it's the largest volume
        if is_closing:
            # Check if this is a larger print than previously seen
            if symbol not in self.closing_prints or qty > self.closing_prints[symbol]["qty"]:
                self.closing_prints[symbol] = bar_data
                logger.info(
                    f"*** NEW CLOSING PRINT for {symbol}: {qty_str} shares at {vwap_str} ***"
                )

    def get_summary(self) -> dict:
        """
        Get summary of closing prints and volume data.

        Returns:
            Dictionary with summary statistics
        """
        summary = {
            "closing_prints": {},
            "total_bars": {},
            "average_volumes": {},
        }

        for symbol in self.symbols:
            if symbol in self.closing_prints:
                summary["closing_prints"][symbol] = self.closing_prints[symbol]

            if symbol in self.minute_bars:
                bars = self.minute_bars[symbol]
                summary["total_bars"][symbol] = len(bars)

                if bars:
                    avg_volume = sum(b["qty"] for b in bars) / len(bars)
                    summary["average_volumes"][symbol] = avg_volume

                    # Calculate closing print as % of average volume
                    if symbol in self.closing_prints and avg_volume:
                        closing_vol = self.closing_prints[symbol]["qty"]
                        summary["closing_prints"][symbol]["pct_of_avg"] = (closing_vol / avg_volume * 100)

        return summary

    def print_summary(self):
        """Print a summary of the analysis."""
        summary = self.get_summary()

        print("\n" + "="*80)
        print("CLOSING VOLUME ANALYSIS SUMMARY")
        print("="*80)

        for symbol in self.symbols:
            print(f"\n{symbol}:")
            print(f"  Total 1-min bars received: {summary['total_bars'].get(symbol, 0)}")

            if symbol in summary["average_volumes"]:
                print(f"  Average 1-min volume: {summary['average_volumes'][symbol]:,.0f} shares")

            if symbol in summary["closing_prints"]:
                closing = summary["closing_prints"][symbol]
                print(f"\n  CLOSING PRINT:")
                print(f"    Time: {closing['time_str']}")
                print(f"    Volume: {closing['qty']:,} shares")

                price = closing.get('close')
                price_str = f"${price:.2f}" if isinstance(price, (int, float)) else "n/a"
                print(f"    Price: {price_str}")

                vwap_val = closing.get('vwap')
                vwap_str = f"${vwap_val:.2f}" if isinstance(vwap_val, (int, float)) else "n/a"
                print(f"    VWAP: {vwap_str}")

                trades = closing.get('num_trades')
                trades_str = trades if trades is not None else "n/a"
                print(f"    Trades: {trades_str}")

                pct_avg = closing.get('pct_of_avg')
                pct_str = f"{pct_avg:.1f}%" if isinstance(pct_avg, (int, float)) else "n/a"
                print(f"    % of avg volume: {pct_str}")
            else:
                print(f"  No closing print identified yet")

        print("\n" + "="*80)

    def save_results(self):
        """Save results to file if output_file is specified."""
        if not self.output_file:
            return

        summary = self.get_summary()
        summary["minute_bars"] = dict(self.minute_bars)

        with open(self.output_file, 'w') as f:
            json.dump(summary, f, indent=2)

        logger.info(f"Results saved to {self.output_file}")

    def upload_to_supabase(self):
        """Upload closing prints to Supabase database."""
        if not self._credsfile:
            logger.info("No Supabase credentials provided, skipping database upload")
            return

        if not self.closing_prints:
            logger.info("No closing prints to upload")
            return

        # Get credentials for Supabase with auth for writes
        creds = sbcreds.SupabaseCredentials(credsfile=self._credsfile, auth=True)

        # Prepare rows for insertion
        # Table columns: symbol (text), date (int), px (numeric), qty (numeric)
        rows = []
        for symbol, print_data in self.closing_prints.items():
            # Get the date from the timestamp in YYYYMMDD format
            one_dt = dt.datetime.fromtimestamp(print_data["timestamp"] / 1000, tz=self.et_tz)
            date_int = int(one_dt.strftime("%Y%m%d"))

            row = {
                "symbol": symbol,
                "date": date_int,
                "px": print_data["px"],
                "qty": print_data["qty"]
            }
            rows.append(row)

        logger.info(f"Uploading {len(rows)} closing print(s) to Supabase")
        logger.info(f"Data: {json.dumps(rows, indent=2)}")

        # Bulk insert all rows
        r = creds.post("closing_print", rows)
        if r.status_code != 201:
            logger.error(f"Supabase upload failed: {r.text}")
        else:
            logger.info(f"Successfully uploaded {len(rows)} closing print(s) to Supabase")

    async def run(self):
        """Start consuming minute bars from the shared ZMQ feed."""

        self.should_stop = False
        if self.zmq_context is None:
            self.zmq_context = zmq.asyncio.Context()

        symbol_list = ", ".join(self.symbols)
        logger.info(
            f"Monitoring {symbol_list} for closing prints using ZMQ feed at {self.zmq_endpoint}"
        )
        logger.info("Monitoring for closing prints from 3:59-4:02 PM ET (extended to catch late prints)...")

        try:
            self.setup_zmq_subscription()
            await self.message_loop()
            logger.info("\nGenerating summary...")
            self.print_summary()
            self.save_results()
            self.upload_to_supabase()

        except asyncio.CancelledError:
            logger.info("Run cancelled")
            raise
        except Exception as exc:
            logger.error(f"Fatal error in run loop: {exc}")
            raise
        finally:
            self.cleanup()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze 1-minute volume aggregates to identify closing prints",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--symbols",
        default="SPY",
        help="Symbols to track (e.g., SPY QQQ IWM)"
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
    sbcreds.add_sb_creds_arg(parser)

    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Create and run analyzer
    analyzer = ClosingVolumeAnalyzer(
        symbols=args.symbols.split(","),
        output_file=args.output,
        credsfile=args.sb_creds,
    )

    try:
        asyncio.run(analyzer.run())
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
    except Exception as exc:
        logger.error(f"Application error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
