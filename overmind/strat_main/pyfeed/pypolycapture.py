#! /usr/bin/env python

"""

Capture all polygon.io messages to dated log files.

This script connects to polygon.io live feeds (equities) and logs all messages
to dated log files for later analysis. It captures:
- Aggregates (second bars)
- Quotes (best bid/offer updates)
- Trades (individual executions)
- LULD (Limit Up/Limit Down events)
- Imbalances (auction imbalance data)

Output files:
- polygon_eq.YYYYMMDD.log - All equity messages
- pypolycapture.YYYYMMDD.log - Application status/info

"""

import asyncio

import argparse
import getpass
import sys
import time
import json
import commentjson
import os

sys.path.append("{}/../util".format(os.path.dirname(os.path.abspath(__file__))))

import datetime

from tenacity import wait_fixed, retry
import traceback

import time

import logging
from logging.handlers import RotatingFileHandler
from logging import handlers
import sys

import chron
import pytz

from polygon import WebSocketClient
from polygon.websocket.models import WebSocketMessage, EquityAgg, EquityQuote, EquityTrade, LimitUpLimitDown, Imbalance, Feed, Market

from typing import List


def getLogger(log_name, file_name):
    log = logging.getLogger(log_name)
    log.setLevel(logging.DEBUG)

    format = logging.Formatter("%(asctime)s - %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(format)
    log.addHandler(ch)

    fh = handlers.RotatingFileHandler(file_name, maxBytes=(1048576*5), backupCount=7)
    fh.setFormatter(format)
    log.addHandler(fh)
    return log


POLYGON_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.Polygon.creds.json")
try:
    with open(POLYGON_SECRET_FILE_PATH, "r") as f:
        secrets = json.load(f)
        API_KEY = secrets["api_key"]
except FileNotFoundError:
    sys.exit(f"Credentials file not found at {POLYGON_SECRET_FILE_PATH}. Please create it.")
except KeyError:
    sys.exit(f"API key not found within {POLYGON_SECRET_FILE_PATH}. Ensure it contains 'api_key'.")
except Exception as e:
    sys.exit(f"Error loading credentials: {e}")


class PolyCapture:
    def __init__(self, conf, creds_dir, date_str, end_secs):
        self.creds_dir = creds_dir

        self.conf = conf

        self.logger = getLogger("pypolycapture", "pypolycapture.{}.log".format(date_str))

        # Separate logger for raw polygon messages
        self.poly_eq_logger = getLogger("polygon_eq_capture", "polygon_eq.{}.log".format(date_str))

        with open(self.conf) as f:
            subs_json = commentjson.loads(f.read())

        self.end_secs = end_secs

        self.n_messages = 0

        self.polygon_eq_syms = []

        for one_market, market_dict in subs_json["subscriptions"].items():
            if one_market == "PolygonEquities":
                self.polygon_eq_syms = market_dict["symbols"]

        # Symbol mapping dictionaries
        self.sym_to_info = {}

        # Track connection state
        self.is_connected = False

    @retry(wait=wait_fixed(1.5))
    async def init_polygon_eq(self):
        if len(self.polygon_eq_syms) > 0:
            try:
                self.logger.info("PolygonEq - connecting to {}".format(self.polygon_eq_syms))

                # Create WebSocket client
                self.ws_client = WebSocketClient(
                    api_key=API_KEY,
                    feed=Feed.RealTime,
                    market=Market.Stocks
                )

                # Subscribe to different message types based on configuration
                subscriptions = []
                for sym in self.polygon_eq_syms:
                    subscriptions.append(f"Q.{sym}")  # Quotes
                    # I think we'll probably just want the Aggregates, but who knows. 
                    subscriptions.append(f"T.{sym}")  # Trades
                    subscriptions.append(f"A.{sym}")  # Aggregates (second bars)
                    subscriptions.append(f"LULD.{sym}")  # Limit Up/Limit Down
                    subscriptions.append(f"NOI.{sym}")  # Net Order Imbalance

                self.logger.info(f"Subscribing to: {subscriptions}")
                self.ws_client.subscribe(*subscriptions)

                # Run the client with our callback
                # Note: polygon's run() is blocking, so we need to handle it differently
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._run_polygon_client)

            except Exception as e:
                self.logger.error(f"Polygon EQ error: {e}")
                self.logger.error(traceback.format_exc())
                raise

    def _run_polygon_client(self):
        """Run polygon client in a blocking fashion"""
        self.ws_client.run(self.process_polygon_cb_eq)

    def process_polygon_cb_eq(self, msgs: List[WebSocketMessage]):
        """Process messages from Polygon WebSocket"""
        try:
            for msg in msgs:
                # Log ALL message types to capture file
                self.poly_eq_logger.info(str(msg))
                self.n_messages += 1

                # Handle different message types
                if isinstance(msg, EquityQuote):
                    sym = msg.symbol if hasattr(msg, 'symbol') else 'UNKNOWN'
                    if self.n_messages % 1000 == 0:
                        self.logger.info(f"EQQuote: {sym} bid={msg.bid_price} ask={msg.ask_price} bsize={msg.bid_size} asize={msg.ask_size}")

                elif isinstance(msg, EquityTrade):
                    sym = msg.symbol if hasattr(msg, 'symbol') else 'UNKNOWN'
                    if self.n_messages % 500 == 0:
                        self.logger.info(f"EQTrade: {sym} price={msg.price} size={msg.size}")

                elif isinstance(msg, EquityAgg):
                    sym = msg.symbol if hasattr(msg, 'symbol') else 'UNKNOWN'
                    if self.n_messages % 100 == 0:
                        self.logger.info(f"EQAgg: {sym} o={msg.open} h={msg.high} l={msg.low} c={msg.close} v={msg.volume}")

                elif isinstance(msg, LimitUpLimitDown):
                    sym = msg.symbol if hasattr(msg, 'symbol') else 'UNKNOWN'
                    # Always log LULD events - they're important!
                    high_limit = msg.high_limit if hasattr(msg, 'high_limit') else 'N/A'
                    low_limit = msg.low_limit if hasattr(msg, 'low_limit') else 'N/A'
                    tape = msg.tape if hasattr(msg, 'tape') else 'N/A'
                    self.logger.info(f"LULD: {sym} high_limit={high_limit} low_limit={low_limit} tape={tape}")

                elif isinstance(msg, Imbalance):
                    sym = msg.symbol if hasattr(msg, 'symbol') else 'UNKNOWN'
                    # Always log Imbalance events - they're important for auctions!
                    auction_type = msg.auction_type if hasattr(msg, 'auction_type') else 'N/A'
                    imbalance_qty = msg.imbalance_quantity if hasattr(msg, 'imbalance_quantity') else 'N/A'
                    paired_qty = msg.paired_quantity if hasattr(msg, 'paired_quantity') else 'N/A'
                    clearing_price = msg.book_clearing_price if hasattr(msg, 'book_clearing_price') else 'N/A'
                    self.logger.info(f"Imbalance: {sym} auction_type={auction_type} imb_qty={imbalance_qty} paired_qty={paired_qty} clearing_px={clearing_price}")

                else:
                    # Log any other message types
                    self.logger.info(f"PolygonMsg: {type(msg).__name__} {msg}")

                # Check for end of day
                if time.time() > self.end_secs:
                    self.logger.info("EOD, Ending feed")
                    sys.exit()

        except Exception as e:
            self.logger.error(f"Error processing Polygon record: {e}")
            self.logger.error(traceback.format_exc())

    async def run(self):
        self.logger.info("Starting PolyCapture")
        await self.init_polygon_eq()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--how", default=False, action="store_true")
    parser.add_argument("--conf", required=True, help="Configuration file with symbols to subscribe to")
    parser.add_argument("--creds", default="/home/{}/.creds/".format(getpass.getuser()))
    parser.add_argument("--date", required=False, default="TOMORROW")
    parser.add_argument("--end-label", required=False, default="PKDayEnd")
    args = parser.parse_args()

    if args.how:
        print("How: ")
        print("./pypolycapture.py --conf feed.json --creds /home/ubuntu/.creds/ --date TOMORROW --end-label PKDayEnd")
        print("")
        print("Configuration file should have structure:")
        print('{')
        print('  "subscriptions": {')
        print('    "PolygonEquities": {')
        print('      "symbols": ["SPY", "QQQ", "IWM"]')
        print('    }')
        print('  }')
        print('}')
        sys.exit()

    # Parse date
    if "2" in args.date:
        date_str = args.date
    else:
        date_str = chron.get_date_easy(args.date)

    end_str = chron.get_end_time(args.end_label, date_str)
    end_secs = chron.timestr_to_secs(end_str) - 5

    polycapture = PolyCapture(args.conf, args.creds, date_str, end_secs)
    await polycapture.run()


if __name__ == "__main__":
    asyncio.run(main())
