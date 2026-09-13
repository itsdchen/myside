#! /usr/bin/env python
import asyncio
import aiohttp

import argparse
import getpass
import requests
import sys
import time
import json
import os
from requests.exceptions import HTTPError
from urllib.parse import urlencode

# For running stuff in directory and through pybin
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(script_dir, ".."))
sys.path.insert(0, os.path.join(script_dir, "../util"))

#import gateway_pb2
import datetime

import websockets
from tenacity import wait_fixed, retry
import traceback

from typing import List

import time

import logging
from logging.handlers import RotatingFileHandler
from logging import handlers
import sys

import chron

import databento as db
from databento import SymbolMappingMsg, MBP1Msg, TradeMsg # Add other types as needed (e.g., OHLCVMsg)

"""

Just gonna write a few things to test out databento and realted futures calls.
I'm also gonna test out diff equities endpoints too.

Some going wisdom

- MBP-1 (market by price) provides every order book event that updates the top price level, also known as the best bid and offer (BBO). This includes every trade and changes to book depth, alongside total size and order count at the BBO.






"""
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


#######################################################3
# CME Futures commands


# Just streams from a symobol.


def stream():
    live_client = db.Live(key=API_KEY)

    # Subscribe to the BBO-1s schema for the continuous NQ contract
    live_client.subscribe(
        dataset="GLBX.MDP3",
        schema="bbo-1s",
        symbols="NQ.c.0",
        stype_in="continuous",
    )

    # Add a print callback
    live_client.add_callback(print)

    # Start the live client to begin streaming
    live_client.start()

    # Run the stream for 15 seconds before closing
    live_client.block_for_close(timeout=5)



def streamraw():
    live_client = db.Live(key=API_KEY)

    # Subscribe to the BBO-1s schema for the continuous NQ contract
    live_client.subscribe(
        dataset="GLBX.MDP3",
        schema="mbp-1",
        symbols="ESZ5",
        stype_in="raw_symbol",
    )

    # Add a print callback
    live_client.add_callback(print)

    # Start the live client to begin streaming
    live_client.start()

    # Run the stream for 15 seconds before closing
    live_client.block_for_close(timeout=5)


# Streaming spy and exploring the schemas

# Guess this is the best one.
def streamspy():
    live_client = db.Live(key=API_KEY)
    live_client.subscribe(
        dataset="EQUS.MINI",
        schema="mbp-1",
        stype_in="raw_symbol",
        symbols="SPY",
    )


    # Add a print callback
    live_client.add_callback(print)

    # Start the live client to begin streaming
    live_client.start()

    # Run the stream for 15 seconds before closing
    live_client.block_for_close(timeout=5)


def get_schemas():
    hist_client = db.Historical(key=API_KEY)
    print(hist_client.metadata.list_datasets())

    schemas = hist_client.metadata.list_schemas(dataset="EQUS.SUMMARY")
    print(schemas)


# We can get symbol info too
# OK, I guess
# This is fuckkng retarded.
def get_symbol_info():
    hist_client = db.Historical(key=API_KEY)

    resolved = hist_client.symbology.resolve(
        dataset="GLBX.MDP3",
        stype_in="raw_symbol",
        stype_out="instrument_id",
        symbols=["ESU5", "ESH6"],
        start_date="2025-01-01",
        end_date="2025-07-22"    )
    print(resolved)



# Weird, looks like they don't have the forward looking
# contracts for a bunch of symbols.
def get_symbol_list():
    client = db.Historical(key=API_KEY)

    # Request definition data
    data = client.timeseries.get_range(
        dataset="GLBX.MDP3",
        start="2025-05-19",
        symbols="ZN.FUT",
        stype_in="parent",
        schema="definition",
    )

    # Convert to DataFrame
    df = data.to_df()

    # Filter out spreads and sort by expiration
    df = df[df["instrument_class"] == db.InstrumentClass.FUTURE]
    df = df.set_index("expiration").sort_index()

    # All columns are:
    ##Index(['ts_event', 'rtype', 'publisher_id', 'instrument_id', 'raw_symbol',
     #  'security_update_action', 'instrument_class', 'min_price_increment',
      # 'display_factor', 'activation', 'high_limit_price', 'low_limit_price',
      # 'max_price_variation', 'trading_reference_price', 'unit_of_measure_qty',
      # 'min_price_increment_amount', 'price_ratio', 'inst_attrib_value',
      # 'underlying_id', 'raw_instrument_id', 'market_depth_implied',
      # 'market_depth', 'market_segment_id', 'max_trade_vol', 'min_lot_size',
      # 'min_lot_size_block', 'min_lot_size_round_lot', 'min_trade_vol',
      # 'contract_multiplier', 'decay_quantity', 'original_contract_size',
      # 'trading_reference_date', 'appl_id', 'maturity_year',
      # 'decay_start_date', 'channel_id', 'currency', 'settl_currency',
      # 'secsubtype', 'group', 'exchange', 'asset', 'cfi', 'security_type',
      # 'unit_of_measure', 'underlying', 'strike_price_currency',
      # 'strike_price', 'match_algorithm', 'md_security_trading_status',
      # 'main_fraction', 'price_display_format', 'settl_price_type',
      # 'sub_fraction', 'underlying_product', 'maturity_month', 'maturity_day',
      # 'maturity_week', 'user_defined_instrument', 'contract_multiplier_unit',
      # 'flow_schedule_type', 'tick_rule', 'symbol'],

    wanted_cols = ['raw_symbol', 'maturity_year', 'asset', 'maturity_day', 'symbol']

    #print(df.columns)
    print(df[wanted_cols])

    #print(df[["instrument_id", "raw_symbol"]])


# Now, let's try to subscribe to live data for a raw symbol




def main():
    parser = argparse.ArgumentParser()
    # File that says what to subscribe to
    parser.add_argument("--mode", required=True)
    parser.add_argument("--creds", default="/home/{}/.creds/".format(getpass.getuser()))
    args = parser.parse_args()


    if args.mode == "stream":
        stream()
    elif args.mode == "info":
        get_symbol_info()
        pass
    elif args.mode == "list":
        get_symbol_list()
    elif args.mode == "streamraw":
        streamraw()
    elif args.mode == "streamspy":
        streamspy()
    elif args.mode == "schemas":
        get_schemas()





if __name__ == "__main__":
    main()


