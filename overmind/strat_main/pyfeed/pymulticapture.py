#! /usr/bin/env python

"""

Capture all databento messages to dated log files.

This script connects to databento live feeds (equities and CME) and logs all messages
to dated log files for later analysis. It captures:
- MBP1 messages (market by price)
- Trade messages
- Status messages (market open/close, etc)
- Error messages
- System messages
- Symbol mapping messages

Output files:
- databento_eq.YYYYMMDD.log - All equity messages
- databento_cme.YYYYMMDD.log - All CME futures messages
- pymulticapture.YYYYMMDD.log - Application status/info

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

# This needs to be just copied over.
#from strat_main.util import symbolizer
import symbolizer

import datetime

from tenacity import wait_fixed, retry
import traceback

import time
import uvloop

import logging
from logging.handlers import RotatingFileHandler
from logging import handlers
import sys

import chron
import pytz

import databento as db
from databento import SymbolMappingMsg, MBP1Msg, TradeMsg, StatusMsg, ErrorMsg, SystemMsg # Add other types as needed (e.g., OHLCVMsg)
from databento.common.enums import RecordFlags as RF


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


DB_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.DataBento.creds.json")
try:
    with open(DB_SECRET_FILE_PATH, "r") as f:
        secrets = json.load(f)
        API_KEY = secrets["api_key"]
    #logger.info("Databento API key loaded successfully.")
except FileNotFoundError:
    #logger.error(f"Credentials file not found at {SECRET_FILE_PATH}. Please create it.")
    sys.exit(f"Credentials file not found at {DB_SECRET_FILE_PATH} . Please create it.")
except KeyError:
    #logger.error(f"API key not found within {SECRET_FILE_PATH}. Ensure it contains 'api_key'.")
    sys.exit(f"API key not found within {DB_SECRET_FILE_PATH} . Ensure it contains 'api_key'.")
except Exception as e:

    sys.exit(f"Error loading credentials: {e}")

# For reference on handling special messages/values. 
# https://databento.com/docs/standards-and-conventions/common-fields-enums-types#ts-event?historical=python&live=python&reference=python


class MultiCapture:
    def __init__(self, conf, creds_dir, date_str, end_secs):
        self.creds_dir = creds_dir

        self.conf = conf

        #current_date = datetime.date.today()
        #date_string = current_date.strftime("%Y%m%d")

        self.logger = getLogger("pymulticapture", "pymulticapture.{}.log".format(date_str))

        # Separate logger for raw databento messages
        self.db_eq_logger = getLogger("databento_eq_capture", "databento_eq.{}.log".format(date_str))
        self.db_cme_logger = getLogger("databento_cme_capture", "databento_cme.{}.log".format(date_str))

        with open(self.conf) as f:
            subs_json = commentjson.loads(f.read())

        # No ZMQ publishing for capture mode
        self._loop = None

        self.end_secs = end_secs

        # OK, now go through and manage hyperliquids.
        self.n_messages = 0

        self.dbento_eq_syms = []
        self.dbento_cme_syms = []

        # Pointers to current "market open" index for fast lookups
        self.eq_interval_idx = 0
        self.cme_interval_idx = 0

        start_time = time.time()
        et_tz = pytz.timezone('America/New_York')

        for one_market, market_dict in subs_json["subscriptions"].items():
            if one_market == "DataBentoEquities":
                self.dbento_eq_syms = market_dict["symbols"]
            if one_market == "DataBentoCME":
                converted_cme_syms = []
                # Convert from nice name to databento names.
                for one_sym in market_dict["symbols"]:
                    converted_sym = symbolizer.ours_to_databento_dated(one_sym, date_str)
                    converted_cme_syms.append(converted_sym)
                self.dbento_cme_syms = converted_cme_syms


    @retry(wait=wait_fixed(1.5))
    async def init_databento_eq(self):
        if len(self.dbento_eq_syms) > 0:
            try:
                print("DatabentoEq - connecting to {}".format(self.dbento_eq_syms))

                self.db_client = db.Live(key=API_KEY)
                # TODO: let this be customizeable.
                self.db_client.subscribe(
                    dataset="EQUS.MINI",
                    schema="mbp-1",
                    stype_in="raw_symbol",
                    symbols=self.dbento_eq_syms,
                )

                self.db_client.add_callback(self.process_db_cb_eq)

                self.sym_to_idx_eq = {}
                self.idx_to_sym_eq = {}

                self.db_client.start()
                #  Can give it a timeout here which is the #seconds to run.
                #self.db_client.block_for_close()
                await self.db_client.wait_for_close()
            except Exception as e:
                self.logger.error(f"DataBento EQ error: {e}")
                self.logger.error(traceback.format_exc())
                raise


    def process_db_cb_eq(self, db_record):
        now_ms = time.time()

        try:
            # Log ALL message types to capture file
            self.db_eq_logger.info(str(db_record))
            self.n_messages += 1

            # Handle symbol mapping
            if isinstance(db_record, SymbolMappingMsg):
                self.sym_to_idx_eq[db_record.stype_in_symbol] = db_record.instrument_id
                self.idx_to_sym_eq[db_record.instrument_id] = db_record.stype_in_symbol
                self.logger.info(f"EQSymbolMapping: {db_record.stype_in_symbol} -> {db_record.instrument_id}")

            # Handle status messages (market open/close, etc)
            elif isinstance(db_record, StatusMsg):
                # Check if status message has instrument_id
                if hasattr(db_record, 'instrument_id') and db_record.instrument_id in self.idx_to_sym_eq:
                    sym = self.idx_to_sym_eq[db_record.instrument_id]
                    self.logger.info(f"EQStatus: {sym} {db_record}")
                else:
                    self.logger.info(f"EQStatus: {db_record}")

            # Handle error messages
            elif isinstance(db_record, ErrorMsg):
                # Check if error message has instrument_id
                if hasattr(db_record, 'instrument_id') and db_record.instrument_id in self.idx_to_sym_eq:
                    sym = self.idx_to_sym_eq[db_record.instrument_id]
                    self.logger.info(f"EQError: {sym} {db_record}")
                else:
                    self.logger.info(f"EQError: {db_record}")

            # Handle system messages
            elif isinstance(db_record, SystemMsg):
                # Check if system message has instrument_id
                if hasattr(db_record, 'instrument_id') and db_record.instrument_id in self.idx_to_sym_eq:
                    sym = self.idx_to_sym_eq[db_record.instrument_id]
                    self.logger.info(f"EQSystem: {sym} {db_record}")
                else:
                    self.logger.info(f"EQSystem: {db_record}")

            # Handle trade messages
            elif isinstance(db_record, TradeMsg):
                if db_record.instrument_id in self.idx_to_sym_eq:
                    sym = self.idx_to_sym_eq[db_record.instrument_id]
                    if self.n_messages % 1000 == 0:
                        self.logger.info(f"EQTrade: {sym} {db_record}")

            # Handle market data messages
            elif isinstance(db_record, MBP1Msg):
                if db_record.instrument_id in self.idx_to_sym_eq:
                    sym = self.idx_to_sym_eq[db_record.instrument_id]
                    if self.n_messages % 3000 == 0:
                        self.logger.info(f"EQMBP1: {sym} {db_record}")
                # Capture everything, even undefined/bad book states - that's valuable info!

            # Check for end of day
            if time.time() > self.end_secs:
                self.logger.info("EOD, Ending feed")
                sys.exit()

        except Exception as e:
            self.logger.error(f"Error processing EQ record: {e}")
            self.logger.error(traceback.format_exc())

    # Not sure yet if I can do two different clients, but feels like I might need to.
    @retry(wait=wait_fixed(0.5))
    async def init_databento_cme(self):
        print("Databento - connecting to {}".format(self.dbento_cme_syms))
        if len(self.dbento_cme_syms) > 0:
            try:
                self.db_client_cme = db.Live(key=API_KEY)
                # TODO: let this be customizeable.
                self.db_client_cme.subscribe(
                    dataset="GLBX.MDP3",
                    schema="mbp-1",
                    stype_in="raw_symbol",
                    #stype_in="continuous",
                    symbols=self.dbento_cme_syms,
                )

                self.db_client_cme.add_callback(self.process_db_cb_cme)

                self.sym_to_idx_cme = {}
                self.idx_to_sym_cme = {}

                self.db_client_cme.start()
                #  Can give it a timeout here which is the #seconds to run.
                #self.db_client.block_for_close()
                await self.db_client_cme.wait_for_close()
            except Exception as e:
                self.logger.error(f"DataBento CME error: {e}")
                self.logger.error(traceback.format_exc())

                raise  # This will trigger the @retry decorator


    def process_db_cb_cme(self, db_record):
        try:
            # Log ALL message types to capture file
            self.db_cme_logger.info(str(db_record))
            self.n_messages += 1

            # Handle symbol mapping
            if isinstance(db_record, SymbolMappingMsg):
                self.sym_to_idx_cme[db_record.stype_in_symbol] = db_record.instrument_id
                self.idx_to_sym_cme[db_record.instrument_id] = db_record.stype_in_symbol
                self.logger.info(f"CME SymbolMapping: {db_record.stype_in_symbol} -> {db_record.instrument_id}")

            # Handle status messages (market open/close, etc)
            elif isinstance(db_record, StatusMsg):
                # Check if status message has instrument_id
                if hasattr(db_record, 'instrument_id') and db_record.instrument_id in self.idx_to_sym_cme:
                    sym = self.idx_to_sym_cme[db_record.instrument_id]
                    self.logger.info(f"CME Status: {sym} {db_record}")
                else:
                    self.logger.info(f"CME Status: {db_record}")

            # Handle error messages
            elif isinstance(db_record, ErrorMsg):
                # Check if error message has instrument_id
                if hasattr(db_record, 'instrument_id') and db_record.instrument_id in self.idx_to_sym_cme:
                    sym = self.idx_to_sym_cme[db_record.instrument_id]
                    self.logger.warning(f"CME Error: {sym} {db_record}")
                else:
                    self.logger.warning(f"CME Error: {db_record}")

            # Handle system messages
            elif isinstance(db_record, SystemMsg):
                # Check if system message has instrument_id
                if hasattr(db_record, 'instrument_id') and db_record.instrument_id in self.idx_to_sym_cme:
                    sym = self.idx_to_sym_cme[db_record.instrument_id]
                    self.logger.info(f"CME System: {sym} {db_record}")
                else:
                    self.logger.info(f"CME System: {db_record}")

            # Handle trade messages
            elif isinstance(db_record, TradeMsg):
                if db_record.instrument_id in self.idx_to_sym_cme:
                    sym = self.idx_to_sym_cme[db_record.instrument_id]
                    if self.n_messages % 1000 == 0:
                        self.logger.info(f"CME Trade: {sym}")

            # Handle market data messages
            elif isinstance(db_record, MBP1Msg):
                if db_record.instrument_id not in self.idx_to_sym_cme:
                    return

                # This is the databento sym, something like "ES.v.0"
                sym = self.idx_to_sym_cme[db_record.instrument_id]

                # Converts it to something like "ES".
                our_sym = symbolizer.databento_to_ours(sym)

                if self.n_messages % 3000 == 0:
                    self.logger.info(f"CME MBP1: {our_sym}")

            # Check for end of day
            if time.time() > self.end_secs:
                self.logger.info("EOD, Ending feed")
                sys.exit()

        except Exception as e:
            self.logger.error(f"Error processing CME record: {e}")
            self.logger.error(traceback.format_exc())

    # Start up
    async def run(self):
        print("Trying to run")
        self._loop = asyncio.get_running_loop()
        await asyncio.gather(self.init_databento_eq(), self.init_databento_cme())



    # We might need to have multiple websockets too.


# The way to run this now:
# Give it a conf, give it creds (to the appropriate thing)
# Give it
async def main():
    parser = argparse.ArgumentParser()
    # File that says what to subscribe to
    parser.add_argument("--how", default=False, action="store_true")
    parser.add_argument("--conf", required=True)
    # Dir where we keep our credentials
    parser.add_argument("--creds", default="/home/{}/.creds/".format(getpass.getuser()))

    parser.add_argument("--date", required=False, default="TOMORROW")

    # example: "08:00:00 America/New_York"
    parser.add_argument("--end-label", required=False, default="PKDayEnd")
    args = parser.parse_args()

    if args.how:
        print("How: ")
        print("./pymulticapture.py --conf feed.json --creds /home/ubuntu/.creds/ --date TOMORROW --end PKDayEnd")
        sys.exit()

    # End like this.
    if "2" in args.date:
        # We can give it a date label or a date string like 20250501
        date_str = args.date
    else:
        date_str = chron.get_date_easy(args.date)

    end_str = chron.get_end_time(args.end_label, date_str)

    # Give ourselves a 5 second gap
    end_secs = chron.timestr_to_secs(end_str) - 5

    pycapture = MultiCapture(args.conf, args.creds, date_str, end_secs)
    await pycapture.run()


if __name__ == "__main__":
    uvloop.run(main())
    # I did a bunch of bs to make polygon work here.
