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

from polygon import RESTClient, WebSocketClient
from polygon.websocket.models import WebSocketMessage, EquityAgg, EquityQuote, EquityTrade, Feed, Market

"""

Testing out polygon.io data for SPY and other equities.

Polygon.io provides real-time and historical market data.

Some notes:
- Aggregates (bars): OHLCV data at various timeframes
- Quotes: Best bid/offer updates
- Trades: Individual trade executions

"""

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


#######################################################
# Equity streaming functions


# Stream real-time aggregates (second bars)
def stream_aggs():
    """Stream second-level aggregates for SPY"""

    def handle_msg(msgs: List[WebSocketMessage]):
        for m in msgs:
            print(m)

    # Use Feed.RealTime for real-time, or Feed.Delayed for 15-min delayed
    client = WebSocketClient(api_key=API_KEY, feed=Feed.RealTime, market=Market.Stocks)
    client.subscribe("A.SPY")  # A = aggregates (second bars)
    client.run(handle_msg)


# Stream real-time quotes (BBO updates)
def stream_quotes():
    """Stream best bid/offer quotes for SPY"""

    print("About to stream quotes for SPY")

    def handle_msg(msgs: List[WebSocketMessage]):
        print(f"Received {len(msgs)} messages")
        for m in msgs:
            print(f"Message type: {type(m)}")
            print(m)

    # Use Feed.RealTime for real-time, or Feed.Delayed for 15-min delayed
    client = WebSocketClient(api_key=API_KEY, feed=Feed.RealTime, market=Market.Stocks)
    print("WebSocket client created")
    client.subscribe("Q.SPY")  # Q = quotes
    print("Subscribed to Q.SPY")
    client.run(handle_msg)


# Stream real-time trades
def stream_trades():
    """Stream individual trades for SPY"""

    def handle_msg(msgs: List[WebSocketMessage]):
        for m in msgs:
            print(m)

    # Use Feed.RealTime for real-time, or Feed.Delayed for 15-min delayed
    client = WebSocketClient(api_key=API_KEY, feed=Feed.RealTime, market=Market.Stocks)
    client.subscribe("T.SPY")  # T = trades
    client.run(handle_msg)


# Stream multiple symbols
def stream_multi():
    """Stream quotes for multiple symbols"""

    def handle_msg(msgs: List[WebSocketMessage]):
        for m in msgs:
            print(m)

    # Use Feed.RealTime for real-time, or Feed.Delayed for 15-min delayed
    client = WebSocketClient(api_key=API_KEY, feed=Feed.RealTime, market=Market.Stocks)
    client.subscribe("Q.SPY", "Q.QQQ", "Q.IWM")
    client.run(handle_msg)



# Get ticker details
def get_ticker_info():
    """Get detailed information about SPY"""

    client = RESTClient(api_key=API_KEY)
    details = client.get_ticker_details("SPY")

    print(f"Ticker Details for SPY:")
    print(f"  Name: {details.name}")
    print(f"  Market: {details.market}")
    print(f"  Primary Exchange: {details.primary_exchange}")
    print(f"  Type: {details.type}")
    print(f"  Currency: {details.currency_name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True,
                       choices=["stream_aggs", "stream_quotes", "stream_trades", "stream_multi",
                               "hist_bars", "latest_quote", "latest_trade", "ticker_info"])
    parser.add_argument("--creds", default="/home/{}/.creds/".format(getpass.getuser()))
    args = parser.parse_args()

    if args.mode == "stream_aggs":
        stream_aggs()
    elif args.mode == "stream_quotes":
        stream_quotes()
    elif args.mode == "stream_trades":
        stream_trades()
    elif args.mode == "stream_multi":
        stream_multi()
    elif args.mode == "latest_quote":
        get_latest_quote()
    elif args.mode == "latest_trade":
        get_latest_trade()
    elif args.mode == "ticker_info":
        get_ticker_info()


if __name__ == "__main__":
    main()
