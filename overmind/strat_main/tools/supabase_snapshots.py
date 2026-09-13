#!/usr/bin/env python
"""
Supabase Snapshots Publisher

Listens to ZMQ messages from pyfeed and periodically publishes
snapshots of related mids to Supabase.



"""

import asyncio
import argparse
import datetime as dt
import json
import logging
import os
import pytz
import requests
import sys
import time
import zmq
import zmq.asyncio

from collections import defaultdict

# Add parent directory to path for imports
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../pyfeed"))

import mdmsg_pb2
from util import chron
from util import sbcreds
from util import symbolizer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SupabaseSnapshotPublisher:
    """Handles ZMQ subscription and periodic snapshot publishing to Supabase."""

    def __init__(self, credsfile: str, snapshot_interval: int = 60, end_secs: int = None):
        """
        Initialize the snapshot publisher.

        Args:
            snapshot_interval: Interval in seconds between snapshots
            end_secs: End time in unix epoch seconds (for computing valid intervals)
        """
        self._credsfile = credsfile
        self.zmq_endpoints = ["ipc:///tmp/feed-sock", "ipc:///tmp/feed-sock-cpp"]
        self.snapshot_interval = snapshot_interval

        # ZMQ context and socket
        self.zmq_context = zmq.asyncio.Context()
        self.zmq_socket = None

        # Data storage for snapshots
        self.market_data = defaultdict(dict)  # {symbol: {field: value}}
        self.last_snapshot_time = time.time()

        # No supabase client, but instead want request sessions to write these with.

        # Mapping of symbols that go with each other.
        # Stuff like ES<>BTC, etc.
        # I'll keep the left side as the traded product, for consistency
        self.sym_pairs = [
            ("NQ", "BTC"),

            ("TSLA", "NQ"),
            ("TSLA", "BTC"),

            ("NVDA", "NQ"),
            ("NVDA", "BTC"),

            ("HOOD", "NQ"),
            ("HOOD", "BTC"),

            ("INTC", "NQ"),
            ("INTC", "BTC"),

            ("PLTR", "NQ"),
            ("PLTR", "BTC"),

            ("COIN", "NQ"),
            ("COIN", "BTC"),

            ("META", "NQ"),
            ("META", "BTC"),

            ("AAPL", "NQ"),
            ("AAPL", "BTC"),

            ("MSFT", "NQ"),
            ("MSFT", "BTC"),

            ("ORCL", "NQ"),
            ("ORCL", "BTC"),

            ("GOOGL", "NQ"),
            ("GOOGL", "BTC"),

            ("AMZN", "NQ"),
            ("AMZN", "BTC"),

            ("AMD", "NQ"),
            ("AMD", "BTC"),

            ("COST", "NQ"),
            ("COST", "BTC"),

            ("NFLX", "NQ"),
            ("NFLX", "BTC"),

            ("CRCL", "NQ"),
            ("CRCL", "BTC"),

            ("MSTR", "NQ"),
            ("MSTR", "BTC"),

            ("SNDK", "NQ"),
            ("SNDK", "BTC"),

            ("MU", "NQ"),
            ("MU", "BTC"),

            ("LLY", "NQ"),
            ("LLY", "BTC"),

            ("BABA", "NQ"),
            ("BABA", "BTC"),

            ("RIVN", "NQ"),
            ("RIVN", "BTC"),

            ("BIRD", "NQ"),
            ("BIRD", "BTC"),

            ("TSM", "NQ"),
            ("TSM", "BTC"),

            ("USAR", "NQ"),
            ("USAR", "BTC"),

            ("URNM", "NQ"),
            ("URNM", "BTC"),

            ("EWY", "NQ"),
            ("EWY", "BTC"),

            ("EWJ", "NQ"),
            ("EWJ", "BTC"),

            ("GME", "NQ"),
            ("GME", "BTC"),

            ("LITE", "NQ"),
            ("LITE", "BTC"),

            ("XLE", "NQ"),
            ("XLE", "BTC"),

            ("BX", "NQ"),
            ("BX", "BTC"),

            ("MRVL", "NQ"),
            ("MRVL", "BTC"),

            ("RKLB", "NQ"),
            ("RKLB", "BTC"),

            ("DRAM", "NQ"),
            ("DRAM", "BTC"),

            ("EWZ", "NQ"),
            ("EWZ", "BTC"),

            ("ZM", "NQ"),
            ("ZM", "BTC"),

            ("EBAY", "NQ"),
            ("EBAY", "BTC"),

            ("ARM", "NQ"),
            ("ARM", "BTC"),

            ("EWT", "NQ"),
            ("EWT", "BTC"),

            ("BB", "NQ"),
            ("BB", "BTC"),

            ("IBM", "NQ"),
            ("IBM", "BTC"),

            ("DELL", "NQ"),
            ("DELL", "BTC"),

            ("AVGO", "NQ"),
            ("AVGO", "BTC"),

            ("QNT", "NQ"),
            ("QNT", "BTC"),

            ("NOW", "NQ"),
            ("NOW", "BTC"),

            ("NBIS", "NQ"),
            ("NBIS", "BTC"),

            ("WDC", "NQ"),
            ("WDC", "BTC"),

            ("SPCX", "NQ"),
            ("SPCX", "BTC"),

            ("NOK", "NQ"),
            ("NOK", "BTC"),

            ("SMH", "NQ"),
            ("SMH", "BTC"),

            ("BE", "NQ"),
            ("BE", "BTC"),

            ("QCOM", "NQ"),
            ("QCOM", "BTC"),

            ("STRC", "NQ"),
            ("STRC", "BTC"),

            ("BOT", "NQ"),
            ("BOT", "BTC"),

            ("AMAT", "NQ"),
            ("AMAT", "BTC"),

            ("SHAZ", "NQ"),
            ("SHAZ", "BTC"),

            ("SKHY", "NQ"),
            ("SKHY", "BTC"),

            ("KSTR", "NQ"),
            ("KSTR", "BTC"),

            ("GEV", "NQ"),
            ("GEV", "BTC"),
            ("KORU", "NQ"),
            ("KORU", "BTC"),

            ("CBRS", "NQ"),
            ("CBRS", "BTC"),

            ("PURR", "NQ"),
            ("PURR", "BTC"),

            ("GOLD", "BTC"),
            ("SILVER", "BTC"),
            ("PLATINUM", "BTC"),
            ("PALLADIUM", "BTC"),

            ("EUR", "BTC"),
            ("JPY", "BTC"),
            ("GBP", "BTC"),

            ("CL", "BTC"),
            ("HG", "BTC"),
            ("NG", "BTC"),
            ("BZ", "BTC"),
            ("GC", "BTC"),
            ("SI", "BTC"),

            ("SMSN", "BTC"),
            ("SKHX", "BTC"),
            ("SKHX", "EWY"),
            ("HYUNDAI", "BTC"),

            ("KOSPI200", "KS"),
            ("KS", "BTC"),

            ("NIKKEI225", "NIY"),
            ("NIY", "BTC"),
        ]

        # Import symbol to market mapping from symbolizer
        self.syms_to_mkt = symbolizer.syms_to_market


        # I don't really mind writing the same data twice tbh.
        self.sym_mappings = {}

        #self.syms_to_mids = {}
        #self.syms_to_last_update_t = {}

        self.last_cme_t_ns = 0
        self.last_equity_t_ns = 0

        # For loop checking.
        self.n_msgs_received = 0

        # Counters for skipped messages
        self.n_skipped_no_data = 0
        self.n_skipped_cme_hours = 0
        self.n_skipped_equity_hours = 0

        # Only update mid-mid map if the price updates were within this many secs of each other.
        # eg. if this is set to 5, and we have two prices that updated within 10s of each other
        # don't do the update because price can be out of date.
        self.tolerance_secs = 1

        self.last_t_update = int(time.time())

        # Market session tracking - similar to pymultifeed
        # List of (start_ns, end_ns) tuples for valid session intervals (in ns)
        self.cme_valid_intervals = []
        self.eq_valid_intervals = []

        # Pointer to current "market open" index for fast lookups
        self.cme_interval_idx = 0
        self.eq_interval_idx = 0

        # Pre-compute valid trading intervals if start/end times provided
        start_secs = int(time.time())
        if end_secs is not None:
            self._precompute_cme_intervals(start_secs, end_secs)
            self._precompute_eq_intervals(start_secs, end_secs)

        # Asia-hour syms where we don't want to filter by CME or USEQ session
        self.syms_to_ignore_session = [
            "SMSN",
            "SKHX",
            "HYUNDAI",
            "KOSPI200",
            "KS",
            "NIKKEI225",
            "NIY"
        ]

    def _precompute_cme_intervals(self, start_time: int, end_secs: int):
        et_tz = pytz.timezone("America/New_York")

        current_date = dt.datetime.fromtimestamp(start_time, tz=et_tz).date()
        end_date = dt.datetime.fromtimestamp(end_secs, tz=et_tz).date()

        date = current_date
        # Add one extra day to handle overnight sessions
        while date <= end_date + dt.timedelta(days=1):
            if date.weekday() == 6:  # Sunday
                # Sunday: 6 PM onwards is open
                session_start = et_tz.localize(dt.datetime.combine(date, dt.time(18, 0)))
                session_end = et_tz.localize(dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0, 0)))
                self.cme_valid_intervals.append((
                    int(session_start.timestamp() * 1_000_000_000),
                    int(session_end.timestamp() * 1_000_000_000)
                ))
                logger.info(f"CME session (Sunday): {session_start} to {session_end}")
            elif date.weekday() == 5:  # Saturday - closed all day
                pass
            elif date.weekday() == 4:  # Friday
                # Friday: Only midnight to 5 PM
                session_start = et_tz.localize(dt.datetime.combine(date, dt.time(0, 0)))
                friday_end = et_tz.localize(dt.datetime.combine(date, dt.time(17, 0)))
                self.cme_valid_intervals.append((
                    int(session_start.timestamp() * 1_000_000_000),
                    int(friday_end.timestamp() * 1_000_000_000)
                ))
                logger.info(f"CME session (Friday): {session_start} to {friday_end}")
            else:  # Monday-Thursday
                # Midnight to 5 PM
                session_start = et_tz.localize(dt.datetime.combine(date, dt.time(0, 0)))
                maint_start = et_tz.localize(dt.datetime.combine(date, dt.time(17, 0)))
                self.cme_valid_intervals.append((
                    int(session_start.timestamp() * 1_000_000_000),
                    int(maint_start.timestamp() * 1_000_000_000)
                ))

                # 6 PM to midnight
                maint_end = et_tz.localize(dt.datetime.combine(date, dt.time(18, 0)))
                day_end = et_tz.localize(dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0, 0)))
                self.cme_valid_intervals.append((
                    int(maint_end.timestamp() * 1_000_000_000),
                    int(day_end.timestamp() * 1_000_000_000)
                ))
                logger.info(f"CME session (Weekday): {session_start} to {maint_start}, {maint_end} to {day_end}")

            date += dt.timedelta(days=1)

        logger.info(f"Generated {len(self.cme_valid_intervals)} CME trading intervals")

    def _is_in_cme_session(self, ts_event_ns: int) -> bool:
        if self.cme_interval_idx < len(self.cme_valid_intervals):
            start_ns, end_ns = self.cme_valid_intervals[self.cme_interval_idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            # If past current interval, advance pointer and check again
            if ts_event_ns > end_ns:
                self.cme_interval_idx += 1
                return self._is_in_cme_session(ts_event_ns)
        return False


    def _precompute_eq_intervals(self, start_time: int, end_secs: int):
        et_tz = pytz.timezone("America/New_York")

        current_date = dt.datetime.fromtimestamp(start_time, tz=et_tz).date()
        end_date = dt.datetime.fromtimestamp(end_secs, tz=et_tz).date()

        date = current_date
        # Add one extra day to handle overnight sessions
        while date <= end_date + dt.timedelta(days=1):
            if date.weekday() < 5:  # Monday=0, Friday=4
                market_open = et_tz.localize(dt.datetime.combine(date, dt.time(4, 00)))
                market_close = et_tz.localize(dt.datetime.combine(date, dt.time(20, 0)))
                start_ns = int(market_open.timestamp() * 1_000_000_000)
                end_ns = int(market_close.timestamp() * 1_000_000_000)
                self.eq_valid_intervals.append((start_ns, end_ns))
                logger.info(f"US Equity session: {market_open} to {market_close}")

            date += dt.timedelta(days=1)

        logger.info(f"Generated {len(self.eq_valid_intervals)} USEq trading intervals")


    # Similar to the cme session, but is way stricter about the 9:30-16:00 period. 
    # We don't take pre- and post- market prints with the same amount of belief. 
    def _is_in_equity_session(self, ts_event_ns):
        if self.eq_interval_idx < len(self.eq_valid_intervals):
            start_ns, end_ns = self.eq_valid_intervals[self.eq_interval_idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            if ts_event_ns > end_ns:
                self.eq_interval_idx += 1
                return self._is_in_equity_session(ts_event_ns)
        return False


    async def setup_zmq_subscription(self):
        """Set up ZMQ subscription socket."""
        try:
            self.zmq_socket = self.zmq_context.socket(zmq.SUB)
            for zmq_endpoint in self.zmq_endpoints:
                self.zmq_socket.connect(zmq_endpoint)
                logger.info(f"Connected to ZMQ endpoint: {zmq_endpoint}")

            # Subscribe to all messages (empty string subscribes to everything)
            self.zmq_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        except Exception as e:
            logger.error(f"Failed to set up ZMQ subscription: {e}")
            raise

    def process_message(self, raw_message: bytes):
        """
        Process incoming ZMQ message.

        Args:
            raw_message: Raw bytes from ZMQ
        """
        try:
            # Parse protobuf message
            pb_msg = mdmsg_pb2.PbMessage()
            pb_msg.ParseFromString(raw_message)

            symbol = pb_msg.symbol_id

            self.n_msgs_received += 1
            if (self.n_msgs_received % 2000 == 0):
                logger.info("MSG received {}".format(symbol))
            # Filter CME symbols during market stop periods
            # Check if this symbol requires market hours filtering
            if symbol in self.syms_to_mkt and symbol not in self.syms_to_ignore_session:
                market = self.syms_to_mkt[symbol]
                if market == "TopBookCme":
                    # Use ts_event from the message if available (in nanoseconds)
                    # If not available, convert current timestamp to nanoseconds
                    if hasattr(pb_msg, 'ts_event') and pb_msg.ts_event > 0:
                        ts_event_ns = pb_msg.ts_event
                    else:
                        ts_event_ns = int(time.time() * 1_000_000_000)

                    # Don't go backwards in time
                    # But maybe give ourselves some wiggle room if it's approx the same. 
                    if ts_event_ns < self.last_cme_t_ns - 100:
                        logger.info("Received msg {} too far in the past, last_cme_time {} this_event_time {} | msg {}".format(
                            self.last_cme_t_ns - ts_event_ns,
                            self.last_cme_t_ns, 
                            ts_event_ns,
                            pb_msg
                        ))
                        return
                    self.last_cme_t_ns = ts_event_ns
                    

                    # Skip messages outside CME trading hours
                    if not self._is_in_cme_session(ts_event_ns):
                        self.n_skipped_cme_hours += 1
                        if self.n_skipped_cme_hours % 50 == 0:
                            logger.info(f"Skipped {self.n_skipped_cme_hours} messages outside CME trading hours (latest: {symbol})")
                        return
                elif market == "TopBookEquity": 
                    # If not available, convert current timestamp to nanoseconds
                    if hasattr(pb_msg, 'ts_event') and pb_msg.ts_event > 0:
                        ts_event_ns = pb_msg.ts_event
                    else:
                        ts_event_ns = int(time.time() * 1_000_000_000)

                    # Don't go backwards in time
                    # But maybe give ourselves some wiggle room if it's approx the same. 
                    if ts_event_ns < self.last_equity_t_ns - 100:
                        logger.info("Received msg {} too far in the past, last_equity_t_ns {} this_event_time {} | msg {}".format(
                            self.last_equity_t_ns - ts_event_ns,
                            self.last_equity_t_ns, 
                            ts_event_ns,
                            pb_msg
                        ))
                        return
                    self.last_equity_t_ns = ts_event_ns
                    # Skip messages outside useq hours
                    if not self._is_in_equity_session(ts_event_ns):
                        self.n_skipped_equity_hours += 1
                        if self.n_skipped_equity_hours % 100 == 0:
                            logger.info(f"Skipped {self.n_skipped_equity_hours} messages outside equity trading hours (latest: {symbol})")
                        return


            mid = 0
            timestamp = int(time.time())

            if pb_msg.HasField("quote"):
                mid = pb_msg.quote.mid_px
                #print("{}: mid is {}".format(symbol, mid))
            elif pb_msg.market == mdmsg_pb2.PBMARKET_HYPERLIQUID and pb_msg.HasField("book_snapshot"):
                #print(pb_msg)
                if pb_msg.book_snapshot.bids and pb_msg.book_snapshot.asks:
                    mid = (float(pb_msg.book_snapshot.bids[0].px) +
                           float(pb_msg.book_snapshot.asks[0].px)) / 2
                else:
                    #print("Empty book for {} on at least 1 side".format(symbol))
                    return
                # Go through the bids and asks.
                #print((" {} {} ".format(symbol, mid)))
            else:
                # Probably a trade message.
                return

            #print(pb_msg)
            self.market_data[symbol].update({
                'mid': mid,
                'timestamp': timestamp,
            })

            return

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def publish_snapshot(self):
        """
        Publish updated, synced pairs to supabase

        For pairs where we can't find both pairs, we gotta log an error (eg. we didn't subscribe)
        For pairs where one is out of date, we also pass

        """
        logger.info("Trying to publish snapshot")

        # Update our timing.
        self.last_snapshot_time = int(time.time())

        # list of dicts: "sym1" "px1" "sym2" "px2"
        update_pairs = []
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for one_sym_pair in self.sym_pairs:
            sym_one = one_sym_pair[0]
            sym_two = one_sym_pair[1]

            if sym_one not in self.market_data:
                logger.info("{}: didnt find mid data for {}, not snapshotting".format(now_str, sym_one))
                continue
            if sym_two not in self.market_data:
                logger.info("{}: didnt find mid data for {}, not snapshotting".format(now_str, sym_two))
                continue
            # If they were too far apart.

            delta_t = self.market_data[sym_one]["timestamp"] - self.market_data[sym_two]["timestamp"]
            if abs(delta_t) > self.tolerance_secs:
                logger.info("{}: Difference between last_recorded_times ({} {}) vs ({} {}) is too much: {}".format(
                    now_str, sym_one, self.market_data[sym_one]["timestamp"], sym_two, self.market_data[sym_two]["timestamp"],
                    delta_t

                ))
                continue
            # Otherwise, let's update them.

            update_pairs.append({
                "sym_1": sym_one,
                "px_1": self.market_data[sym_one]["mid"],
                "sym_2": sym_two,
                "px_2": self.market_data[sym_two]["mid"],
            })


        # After this, try to update supabase correctly.

        logger.info(json.dumps(update_pairs, indent=4))
        #logger.info("But all state is like {}".format(self.market_data))

        # Get creds for Supabase using proper authorization for writes
        creds = sbcreds.SupabaseCredentials(credsfile=self._credsfile, auth=True)

        # Bulk insert all rows in a single request
        if update_pairs:
            #logger.info("Inserting {} rows".format(len(update_pairs)))
            r = creds.post("snapshot", update_pairs)
            if r.status_code != 201:
                logger.info("Bulk insert failed: %s", r.text)
        else:
            logger.info("No rows to insert")

        return

    def should_publish_snapshot(self) -> bool:
        """Check if it's time to publish a new snapshot."""
        return (time.time() - self.last_snapshot_time) >= self.snapshot_interval

    async def message_receiver_loop(self):
        """Main loop for receiving and processing ZMQ messages."""
        logger.info("Starting message receiver loop")

        while True:
            try:
                # Non-blocking receive with timeout
                if await self.zmq_socket.poll(timeout=100):  # 100ms timeout
                    raw_message = await self.zmq_socket.recv()
                    self.process_message(raw_message)


                # Check if we should publish a snapshot
                if self.should_publish_snapshot():
                    await self.publish_snapshot()

            except asyncio.CancelledError:
                logger.info("Message receiver loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in message receiver loop: {e}")
                await asyncio.sleep(1)  # Brief pause before retrying

    async def run(self):
        """Main run method to start the publisher."""
        try:
            # Set up ZMQ subscription
            await self.setup_zmq_subscription()

            # TODO: Initialize Supabase client
            # self.supabase_client = await self.setup_supabase_client()

            # Start the message receiver loop
            await self.message_receiver_loop()

        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down...")
        except Exception as e:
            logger.error(f"Fatal error in run loop: {e}")
            raise
        finally:
            await self.cleanup()

    async def cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up resources")

        if self.zmq_socket:
            self.zmq_socket.close()

        if self.zmq_context:
            self.zmq_context.term()

        # TODO: Clean up Supabase client
        # if self.supabase_client:
        #     await self.supabase_client.close()


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Supabase Snapshot Publisher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--snapshot-interval",
        type=int,
        default=60 * 5,
        help="Interval in seconds between snapshots"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )
    parser.add_argument(
        "--end-date",
        default="TOMORROW",
        choices=["TODAY", "TOMORROW"],
        help="In case we run manually and need it to end today"
    )
    sbcreds.add_sb_creds_arg(parser)
    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # End date is going to be tomorrow's 18:00 ET. 
    date_str = chron.get_date_easy(args.end_date)
    # Get start and end times for the day
    end_str = chron.get_end_time("PKDayEnd", date_str)
    end_secs = chron.timestr_to_secs(end_str) - 5

    # Create and run publisher with start/end times for session computation
    publisher = SupabaseSnapshotPublisher(
        credsfile=args.sb_creds,
        snapshot_interval=args.snapshot_interval,
        end_secs=end_secs
    )

    # Run until end time
    try:
        # Start the publisher in the background
        run_task = asyncio.create_task(publisher.run())

        # Wait until end time
        while time.time() < end_secs:
            await asyncio.sleep(1)

        # Cancel the publisher
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

        logger.info("Reached end time, shutting down")
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    finally:
        await publisher.cleanup()


if __name__ == "__main__":
    # Use asyncio to run the main coroutine
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
    except Exception as e:
        logger.error(f"Application error: {e}")
        sys.exit(1)
