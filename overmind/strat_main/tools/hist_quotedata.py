#! /usr/bin/env python

"""

Thing that streams in historical data from our third party providers,
parses them, stores them in nice gzpbfs.

The nice thing is there isn't that much data, I think. Not as bad when we're
just streaming in quotes.

Example commands:

# Regular mode - fetch specific symbols and dates
./hist_quotedata.py --start 20251107 --end 20251111 --market TopBookCme --symbols NQ --compress --upload



# Backfill mode - automatically detect and fill missing data for all symbols
./hist_quotedata.py --backfill --compress --upload

./hist_quotedata.py --backfill --symbols TSLA,NVDA,PLTR,HOOD,META,MSFT,AAPL,GOOGL,AMZN,MSTR,COIN,AMD --compress --upload

# Backfill mode - specific symbols and date range
./hist_quotedata.py --backfill --symbols NQ,TSLA --start 20251115 --end 20251130 --compress --upload

# Force backfill - regenerate data even if it already exists in S3
./hist_quotedata.py --backfill --force_backfill --symbols NQ --start 20251115 --end 20251120 --compress --upload

# Using different data sources for equities:
# --source polygon        (default) Polygon.io SIP data
# --source databento      Databento EQUS.MINI (SIP consolidated)
# --source databentonasdaq Databento XNAS.BASIC (NASDAQ Basic direct feed)
./hist_quotedata.py --start 20251107 --end 20251111 --market TopBookEquity --symbols TSLA --source databento --compress --upload

"""

import asyncio
import heapq
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

import boto3

# For running stuff in directory and through pybin
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(script_dir, ".."))
sys.path.insert(0, os.path.join(script_dir, "../util"))
sys.path.insert(0, os.path.join(script_dir, "../pyfeed"))

import symbolizer
import chron
import datetime
import gzip

import websockets
from tenacity import wait_fixed, retry
import traceback

from typing import List

import time

import logging
from logging.handlers import RotatingFileHandler
from logging import handlers
import sys

import mdmsg_pb2

import chron
import pytz

import databento as db
from databento import SymbolMappingMsg, MBP1Msg, TradeMsg # Add other types as needed (e.g., OHLCVMsg)
from polygon import RESTClient

import symbolizer
import md_exists

# Pyth feed IDs for historical data (Benchmarks API)
# https://insights.pyth.network/price-feeds
pyth_names_to_addresses = {
    "PLATINUM": "0x398e4bbc7cbf89d6648c21e08019d878967677753b3096799595c78f805a34e5",
    "PALLADIUM": "0x80367e9664197f37d89a07a804dffd2101c479c7c4e8490501bc9d9e1e7f9021",
}

PYTH_BENCHMARKS_URL = "https://benchmarks.pyth.network"


# Helper functions for writing delimited protobufs (similar to C++'s SerializeDelimitedToZeroCopyStream)
def _write_varint(value, file_handle):
    """Write a varint (variable-length integer) to file."""
    while value > 0x7f:
        file_handle.write(bytes([(value & 0x7f) | 0x80]))
        value >>= 7
    file_handle.write(bytes([value & 0x7f]))


def write_delimited_protobuf(message, file_handle):
    """
    Write a length-delimited protobuf message to a file handle.
    This mimics C++'s SerializeDelimitedToZeroCopyStream behavior.
    """
    # Serialize the message
    serialized = message.SerializeToString()
    # Write the size as a varint, then the message
    _write_varint(len(serialized), file_handle)
    file_handle.write(serialized)


def databento_msg_to_dict(msg):
    """
    Convert a Databento message object to a dictionary.
    Handles different message types (MBP1, Trade, etc.)

    Right now, I just want to deal with basic messages. 
    """
    msg_dict = {}

    # Common fields across message types
    if hasattr(msg, 'ts_event'):
        msg_dict['ts_event'] = int(msg.ts_event)
        msg_dict['ts_event_ms'] = int(msg.ts_event / 1_000_000)
    if hasattr(msg, 'ts_recv'):
        msg_dict['ts_recv'] = int(msg.ts_recv)
    if hasattr(msg, 'instrument_id'):
        msg_dict['instrument_id'] = int(msg.instrument_id)
    if hasattr(msg, 'publisher_id'):
        msg_dict['publisher_id'] = int(msg.publisher_id)
    if hasattr(msg, 'rtype'):
        msg_dict['rtype'] = int(msg.rtype)

    # MBP-1 specific fields (top of book)
    if isinstance(msg, MBP1Msg):
        msg_dict['msg_type'] = 'mbp1'

        # Bid side (index 0 is top of book)
        if hasattr(msg, 'levels') and len(msg.levels) > 0:
            bid = msg.levels[0]
            msg_dict['bid_px'] = int(bid.bid_px) if bid.bid_px else None
            msg_dict['bid_sz'] = int(bid.bid_sz) if bid.bid_sz else None
            msg_dict['bid_ct'] = int(bid.bid_ct) if bid.bid_ct else None

            # Ask side
            msg_dict['ask_px'] = int(bid.ask_px) if bid.ask_px else None
            msg_dict['ask_sz'] = int(bid.ask_sz) if bid.ask_sz else None
            msg_dict['ask_ct'] = int(bid.ask_ct) if bid.ask_ct else None

        if hasattr(msg, 'action'):
            msg_dict['action'] = str(msg.action)
        if hasattr(msg, 'side'):
            msg_dict['side'] = str(msg.side)
        if hasattr(msg, 'flags'):
            msg_dict['flags'] = int(msg.flags)
        if hasattr(msg, 'depth'):
            msg_dict['depth'] = int(msg.depth)
        if hasattr(msg, 'ts_in_delta'):
            msg_dict['ts_in_delta'] = int(msg.ts_in_delta)
        if hasattr(msg, 'sequence'):
            msg_dict['sequence'] = int(msg.sequence)

    # Trade message fields
    elif isinstance(msg, TradeMsg):
        msg_dict['msg_type'] = 'trade'
        if hasattr(msg, 'price'):
            msg_dict['price'] = int(msg.price)
        if hasattr(msg, 'size'):
            msg_dict['size'] = int(msg.size)
        if hasattr(msg, 'action'):
            msg_dict['action'] = str(msg.action)
        if hasattr(msg, 'side'):
            msg_dict['side'] = str(msg.side)
        if hasattr(msg, 'flags'):
            msg_dict['flags'] = int(msg.flags)
        if hasattr(msg, 'depth'):
            msg_dict['depth'] = int(msg.depth)
        if hasattr(msg, 'ts_in_delta'):
            msg_dict['ts_in_delta'] = int(msg.ts_in_delta)
        if hasattr(msg, 'sequence'):
            msg_dict['sequence'] = int(msg.sequence)

    # Symbol mapping messages
    elif isinstance(msg, SymbolMappingMsg):
        msg_dict['msg_type'] = 'symbol_mapping'
        if hasattr(msg, 'stype_in_symbol'):
            msg_dict['stype_in_symbol'] = str(msg.stype_in_symbol)
        if hasattr(msg, 'stype_out_symbol'):
            msg_dict['stype_out_symbol'] = str(msg.stype_out_symbol)
        if hasattr(msg, 'start_ts'):
            msg_dict['start_ts'] = int(msg.start_ts)
        if hasattr(msg, 'end_ts'):
            msg_dict['end_ts'] = int(msg.end_ts)

    else:
        # Generic fallback - try to capture all attributes
        msg_dict['msg_type'] = 'unknown'
        for attr in dir(msg):
            if not attr.startswith('_') and not callable(getattr(msg, attr)):
                try:
                    val = getattr(msg, attr)
                    # Convert to JSON-serializable types
                    if isinstance(val, (int, float, str, bool, type(None))):
                        msg_dict[attr] = val
                    else:
                        msg_dict[attr] = str(val)
                except:
                    pass

    return msg_dict


def _maybe_list(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def polygon_quote_to_dict(quote):
    """Convert a Polygon quote object into a JSON-serializable dict."""
    return {
        "symbol": getattr(quote, "symbol", None),
        "sip_timestamp": getattr(quote, "sip_timestamp", None),
        "participant_timestamp": getattr(quote, "participant_timestamp", None),
        "sequence_number": getattr(quote, "sequence_number", None),
        "bid_price": getattr(quote, "bid_price", None),
        "bid_size": getattr(quote, "bid_size", None),
        "ask_price": getattr(quote, "ask_price", None),
        "ask_size": getattr(quote, "ask_size", None),
        "conditions": _maybe_list(getattr(quote, "conditions", None)),
        "indicators": _maybe_list(getattr(quote, "indicators", None)),
        "tape": getattr(quote, "tape", None),
        "bid_exchange": getattr(quote, "bid_exchange", None),
        "ask_exchange": getattr(quote, "ask_exchange", None),
    }


def polygon_trade_to_dict(trade):
    """Convert a Polygon trade object into a JSON-serializable dict."""
    return {
        "symbol": getattr(trade, "symbol", None),
        "sip_timestamp": getattr(trade, "sip_timestamp", None),
        "participant_timestamp": getattr(trade, "participant_timestamp", None),
        "sequence_number": getattr(trade, "sequence_number", None),
        "price": getattr(trade, "price", None),
        "size": getattr(trade, "size", None),
        "exchange": getattr(trade, "exchange", None),
        "conditions": _maybe_list(getattr(trade, "conditions", None)),
        "tape": getattr(trade, "tape", None),
        "trf_timestamp": getattr(trade, "trf_timestamp", None),
        "trade_id": getattr(trade, "trade_id", None),
    }


def build_polygon_quote_pb(symbol: str, quote) -> mdmsg_pb2.PbMessage:
    """Create PbMessage for a Polygon quote."""
    pb_msg = mdmsg_pb2.PbMessage()
    pb_msg.market = mdmsg_pb2.PbMarket.PBMARKET_TOPBOOK_EQUITY
    pb_msg.symbol_id = symbol

    pb_quote = pb_msg.quote
    seq = getattr(quote, "sequence_number", None)
    if seq is not None:
        pb_quote.update_id = int(seq)

    bid_px = getattr(quote, "bid_price", None)
    ask_px = getattr(quote, "ask_price", None)
    bid_sz = getattr(quote, "bid_size", None)
    ask_sz = getattr(quote, "ask_size", None)

    if bid_px is not None:
        pb_quote.best_bid_px = str(bid_px)
    if bid_sz is not None:
        pb_quote.best_bid_qty = str(bid_sz)
    if ask_px is not None:
        pb_quote.best_ask_px = str(ask_px)
    if ask_sz is not None:
        pb_quote.best_ask_qty = str(ask_sz)

    if bid_px is not None and ask_px is not None:
        pb_quote.mid_px = round((float(bid_px) + float(ask_px)) / 2.0, 6)

    sip_ts = getattr(quote, "sip_timestamp", None)
    if sip_ts is not None:
        pb_quote.transact_time = int(sip_ts // 1_000_000)
        pb_msg.rx_timestamp = pb_quote.transact_time + 120

    return pb_msg


def build_polygon_trade_pb(symbol: str, trade) -> mdmsg_pb2.PbMessage:
    """Create PbMessage for a Polygon trade."""
    pb_msg = mdmsg_pb2.PbMessage()
    pb_msg.market = mdmsg_pb2.PbMarket.PBMARKET_TOPBOOK_EQUITY
    pb_msg.symbol_id = symbol

    pb_trade = pb_msg.book_trade

    price = getattr(trade, "price", None)
    size = getattr(trade, "size", None)
    if price is not None:
        pb_trade.px = str(price)
    if size is not None:
        pb_trade.qty = str(size)

    trade_id = getattr(trade, "trade_id", None)
    if trade_id is None:
        trade_id = getattr(trade, "sequence_number", None)
    if trade_id is not None:
        pb_trade.trade_id = int(trade_id)

    sip_ts = getattr(trade, "sip_timestamp", None)
    if sip_ts is not None:
        pb_trade.trade_time = int(sip_ts // 1_000_000)
	# Approx one-way travel time. 
        pb_msg.rx_timestamp = pb_trade.trade_time + 120

    return pb_msg

#Quote(ask_exchange=48, ask_price=79.349, ask_size=None, bid_exchange=48, bid_price=79.1552, bid_size=None, conditions=None, indicators=None, participant_timestamp=1766959200000000000, sequence_number=None, sip_timestamp=None, tape=None, trf_timestamp=None)
def polygonfx_quote_to_dict(symbol, quote):
    """Convert a Polygon quote object into a JSON-serializable dict."""
    return {
        "symbol": symbol,
        "participant_timestamp": getattr(quote, "participant_timestamp", None),
        "bid_price": quote.bid_price,
        "ask_price": quote.ask_price,
    }

def build_polygonfx_quote_pb(symbol: str, quote) -> mdmsg_pb2.PbMessage:
    """Create PbMessage for a Polygon quote."""
    pb_msg = mdmsg_pb2.PbMessage()
    pb_msg.market = mdmsg_pb2.PbMarket.PBMARKET_TOPBOOK_EQUITY
    pb_msg.symbol_id = symbol

    pb_quote = pb_msg.quote

    bid_px = getattr(quote, "bid_price", None)
    ask_px = getattr(quote, "ask_price", None)

    if bid_px is not None:
        pb_quote.best_bid_px = str(bid_px)
    if ask_px is not None:
        pb_quote.best_ask_px = str(ask_px)


    if bid_px is not None and ask_px is not None:
        pb_quote.mid_px = round((float(bid_px) + float(ask_px)) / 2.0, 6)

    ts = getattr(quote, "participant_timestamp", None)
    if ts is not None:
        pb_quote.transact_time = int(ts // 1_000_000)
        # Seems like approx 1-way delta t. 
        pb_msg.rx_timestamp = pb_quote.transact_time + 1000

    return pb_msg




def store_cme(symbols: List[str], creds_file: str, out_dir: str, dates: List[str], compress: bool, do_upload: bool):
    """
    Store historical CME data from Databento.

    Args:
        symbols: List of CME symbols to retrieve (e.g., ["ESZ5", "NQZ5"])
        creds_file: Path to Databento credentials JSON file
        out_dir: Directory to save output files
        dates: List of dates in YYYYMMDD format
        compress: Whether to compress output
    """
    # First, let's get the credentials.
    with open(creds_file, "r") as f:
        creds = json.load(f)
        api_key = creds["api_key"]

    # Initialize historical client
    client = db.Historical(key=api_key)


    # Used for uploading. 
    #(mkt, sym, channel, date)  -> file path. 
    # channel is just the one, quotes
    written_files = {}

    # For each date, retrieve data for the full UTC calendar day (00:00:00 to 23:59:59 UTC)
    # Include Sunday in your dates list to capture Sunday night CME trading
    for date_str in dates:
        print(f"Processing date: {date_str}")

        # Parse the date
        date_obj = datetime.datetime.strptime(date_str, "%Y%m%d")

        # Each date represents a full UTC calendar day from 00:00:00 UTC to 23:59:59 UTC
        # Start from the date itself at 00:00 UTC
        start_date = date_obj

        # End date is the next day at 00:00 UTC to capture the full UTC day
        end_date = date_obj + datetime.timedelta(days=1)

        # Format dates for Databento API (YYYY-MM-DD)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        print(f"  Fetching data from {start_str} to {end_str} (UTC)")


        # Process each symbol
        for symbol in symbols:

            # Check if this symbol needs roll blending on this date.
            contracts = symbolizer.get_all_contracts_for_date(symbol, date_str)
            is_blend = len(contracts) == 2

            if is_blend:
                front_sym, front_w = contracts[0]
                next_sym, next_w = contracts[1]
                print(f"    Symbol: {symbol} (roll blend: {front_sym}*{front_w} + {next_sym}*{next_w})")
            else:
                dbsym = contracts[0][0]
                print(f"    Symbol: {dbsym}")


            # Rate limiting how often we store messages.
            # This echoes what we do in pymultifeed
            last_recorded_t = 0
            record_interval_ms = 10

            try:
                db_px_convert = 1000000000

                if is_blend:
                    # Roll blend: fetch both contracts and merge
                    front_data = client.timeseries.get_range(
                        dataset="GLBX.MDP3",
                        start=start_str,
                        end=end_str,
                        symbols=[front_sym],
                        schema="mbp-1",
                        stype_in="raw_symbol",
                    )
                    next_data = client.timeseries.get_range(
                        dataset="GLBX.MDP3",
                        start=start_str,
                        end=end_str,
                        symbols=[next_sym],
                        schema="mbp-1",
                        stype_in="raw_symbol",
                    )

                    # Collect MBP1 messages from both, tagged with leg.
                    # Prefilter each leg to ~record_interval_ms granularity to keep lists small.
                    def _sample(data, interval_ns):
                        last_t = -(interval_ns + 1)
                        for m in data:
                            if isinstance(m, MBP1Msg) and m.ts_event - last_t >= interval_ns:
                                last_t = m.ts_event
                                yield m
                    interval_ns = record_interval_ms * 1_000_000
                    front_msgs = [(m, "front") for m in _sample(front_data, interval_ns)]
                    next_msgs = [(m, "next") for m in _sample(next_data, interval_ns)]
                    # Both lists are already sorted by ts_event, so heapq.merge is O(n) vs sorted()'s O(n log n).
                    # Note: get_range() materializes a DBNStore so generators wouldn't save memory here.
                    all_msgs = heapq.merge(front_msgs, next_msgs, key=lambda x: x[0].ts_event)
                    print(f"      Fetched {len(front_msgs)} front + {len(next_msgs)} next messages")

                    # Build blended quote tuples: (bid_px, ask_px, bid_sz, ask_sz, ts_event_ms)
                    front_latest = None  # (bid, ask, bid_sz, ask_sz)
                    next_latest = None
                    blended_quotes = []

                    for msg, leg in all_msgs:
                        bid_px = float(msg.levels[0].bid_px) / db_px_convert
                        ask_px = float(msg.levels[0].ask_px) / db_px_convert
                        bid_sz = msg.levels[0].bid_sz
                        ask_sz = msg.levels[0].ask_sz
                        ts_event_ms = int(msg.ts_event / 1000000)

                        if leg == "front":
                            front_latest = (bid_px, ask_px, bid_sz, ask_sz)
                        else:
                            next_latest = (bid_px, ask_px, bid_sz, ask_sz)

                        if front_latest is None or next_latest is None:
                            continue

                        b_bid = front_w * front_latest[0] + next_w * next_latest[0]
                        b_ask = front_w * front_latest[1] + next_w * next_latest[1]
                        b_bid_sz = front_latest[2] + next_latest[2]
                        b_ask_sz = front_latest[3] + next_latest[3]

                        # # Debug: print raw leg values
                        # print(f"        t={ts_event_ms} front=({front_latest[0]:.4f},{front_latest[1]:.4f}) next=({next_latest[0]:.4f},{next_latest[1]:.4f}) -> blend=({b_bid:.4f},{b_ask:.4f})")

                        blended_quotes.append((b_bid, b_ask, b_bid_sz, b_ask_sz, ts_event_ms))
                else:
                    # Single contract: fetch data normally
                    data = client.timeseries.get_range(
                        dataset="GLBX.MDP3",
                        start=start_str,
                        end=end_str,
                        symbols=[dbsym],
                        schema="mbp-1",
                        stype_in="raw_symbol",
                    )

                # Determine output filename
                output_filename = f"{symbol}_quotes_{date_str}"

                if compress:
                    # Write to compressed protobuf format
                    output_path = os.path.join(out_dir, f"{output_filename}.gzpbf")

                    record_count = 0

                    id_key = ("TopBookCme", symbol, "quotes", date_str)

                    with gzip.open(output_path, 'wb') as f:
                        if is_blend:
                            # Write blended quotes
                            for b_bid, b_ask, b_bid_sz, b_ask_sz, ts_event_ms in blended_quotes:
                                if ts_event_ms - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = ts_event_ms

                                one_msg_pb = mdmsg_pb2.PbMessage()
                                one_msg_pb.market = 9  # TOPBOOK_CME
                                one_msg_pb.symbol_id = symbol

                                pbquote = one_msg_pb.quote
                                pbquote.best_bid_px = str(b_bid)
                                pbquote.best_bid_qty = str(b_bid_sz)
                                pbquote.best_ask_px = str(b_ask)
                                pbquote.best_ask_qty = str(b_ask_sz)
                                pbquote.mid_px = round((b_bid + b_ask) / 2, 6)
                                pbquote.transact_time = ts_event_ms

                                one_msg_pb.rx_timestamp = ts_event_ms + 85

                                write_delimited_protobuf(one_msg_pb, f)
                                record_count += 1
                        else:
                            # Write single-contract quotes (existing logic)
                            for msg in data:
                                if not isinstance(msg, MBP1Msg):
                                    continue

                                one_msg_pb = mdmsg_pb2.PbMessage()
                                one_msg_pb.market = 9  # TOPBOOK_CME
                                one_msg_pb.symbol_id = symbol

                                pbquote = one_msg_pb.quote

                                pbquote.best_bid_px = str(float(msg.levels[0].bid_px)/db_px_convert)
                                pbquote.best_bid_qty = str(msg.levels[0].bid_sz)
                                pbquote.best_ask_px = str(float(msg.levels[0].ask_px)/db_px_convert)
                                pbquote.best_ask_qty = str(msg.levels[0].ask_sz)
                                pbquote.mid_px = round((float(msg.levels[0].bid_px)/db_px_convert + float(msg.levels[0].ask_px)/db_px_convert) / 2, 6)
                                pbquote.transact_time = int(msg.ts_event / 1000000)

                                one_msg_pb.rx_timestamp = pbquote.transact_time + 85

                                if pbquote.transact_time - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = pbquote.transact_time

                                write_delimited_protobuf(one_msg_pb, f)
                                record_count += 1
                else:
                    # Write to uncompressed JSONL format
                    output_path = os.path.join(out_dir, f"{output_filename}.jsonl")
                    with open(output_path, 'w') as f:
                        record_count = 0
                        if is_blend:
                            for b_bid, b_ask, b_bid_sz, b_ask_sz, ts_event_ms in blended_quotes:
                                if ts_event_ms - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = ts_event_ms

                                msg_dict = {
                                    "symbol": symbol,
                                    "best_bid_px": b_bid,
                                    "best_ask_px": b_ask,
                                    "best_bid_sz": b_bid_sz,
                                    "best_ask_sz": b_ask_sz,
                                    "mid_px": round((b_bid + b_ask) / 2, 6),
                                    "ts_event_ms": ts_event_ms,
                                }
                                f.write(json.dumps(msg_dict) + '\n')
                                record_count += 1
                        else:
                            for msg in data:
                                msg_dict = databento_msg_to_dict(msg)

                                if msg_dict["ts_event_ms"] - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = msg_dict["ts_event_ms"]

                                f.write(json.dumps(msg_dict) + '\n')
                                record_count += 1

                print(f"      Saved {record_count} records to: {output_path}")

                # Databento returns an empty iterator for closed sessions. Do not
                # retain or upload an empty capture, which md_exists would treat
                # as usable market data.
                if record_count == 0:
                    os.remove(output_path)
                    print("      No quote records; skipped empty capture")
                    continue

                if compress:
                    written_files[id_key] = output_path

                # Also save metadata about the request
                metadata = {
                    "symbol": symbol,
                    "date": date_str,
                    "start": start_str,
                    "end": end_str,
                    "dataset": "GLBX.MDP3",
                    "schema": "mbp-1",
                    "record_count": record_count,
                }
                if is_blend:
                    metadata["roll_blend"] = {
                        "front": front_sym, "front_weight": front_w,
                        "next": next_sym, "next_weight": next_w,
                    }

                metadata_path = os.path.join(out_dir, f"{output_filename}.metadata.json")
                with open(metadata_path, "w") as f:
                    json.dump(metadata, f, indent=2)

            except Exception as e:
                print(f"      Error fetching data for {symbol} on {date_str}: {e}")
                traceback.print_exc()
                continue
    # First, print out the outputted files. 
    print("We wrote out these files;")
    print(written_files)


    # Then, if we elect to upload, upload to s3. 

    if do_upload:
        # Do the s3 stuff... 
        s3 = boto3.Session(profile_name='l1').client("s3")

        for one_file_props, one_file_path in written_files.items():
            dest_bucket = "l1-pktrade-capture"

            object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                    MKT=one_file_props[0],
                    SYM=one_file_props[1],
                    CHANNEL=one_file_props[2],
                    DATE=one_file_props[3],
                )

            print(f"Uploading {one_file_path} to s3://{dest_bucket}/{object_name}")
            with open(one_file_path, 'rb') as f:
                s3.upload_fileobj(f, dest_bucket, object_name)
            print(f"  Upload complete")

    print("Done processing all dates and symbols.")
    

def build_databento_equity_quote_pb(symbol: str, msg, db_px_convert: int) -> mdmsg_pb2.PbMessage:
    """Create PbMessage for a Databento equity quote (MBP1 or CBBO message)."""
    pb_msg = mdmsg_pb2.PbMessage()
    pb_msg.market = mdmsg_pb2.PbMarket.PBMARKET_TOPBOOK_EQUITY
    pb_msg.symbol_id = symbol

    pb_quote = pb_msg.quote

    # Extract bid/ask based on message format
    bid_px = None
    ask_px = None
    bid_sz = None
    ask_sz = None

    # MBP1Msg (mbp-1 schema) has levels array
    if hasattr(msg, 'levels') and len(msg.levels) > 0:
        level = msg.levels[0]
        bid_px = float(level.bid_px) / db_px_convert if level.bid_px else None
        ask_px = float(level.ask_px) / db_px_convert if level.ask_px else None
        bid_sz = level.bid_sz if hasattr(level, 'bid_sz') else None
        ask_sz = level.ask_sz if hasattr(level, 'ask_sz') else None
    # CBBO messages (cmbp-1 schema) have bid_px/ask_px directly
    elif hasattr(msg, 'bid_px') and hasattr(msg, 'ask_px'):
        bid_px = float(msg.bid_px) / db_px_convert if msg.bid_px else None
        ask_px = float(msg.ask_px) / db_px_convert if msg.ask_px else None
        bid_sz = msg.bid_sz if hasattr(msg, 'bid_sz') else None
        ask_sz = msg.ask_sz if hasattr(msg, 'ask_sz') else None

    if bid_px is not None:
        pb_quote.best_bid_px = str(bid_px)
    if bid_sz is not None:
        pb_quote.best_bid_qty = str(bid_sz)
    if ask_px is not None:
        pb_quote.best_ask_px = str(ask_px)
    if ask_sz is not None:
        pb_quote.best_ask_qty = str(ask_sz)

    if bid_px is not None and ask_px is not None:
        pb_quote.mid_px = round((bid_px + ask_px) / 2.0, 6)

    # ts_event is in nanoseconds, convert to milliseconds
    if hasattr(msg, 'ts_event') and msg.ts_event:
        pb_quote.transact_time = int(msg.ts_event // 1_000_000)
        # Approx one-way travel time (similar to Polygon)
        pb_msg.rx_timestamp = pb_quote.transact_time + 120

    return pb_msg


def build_databento_equity_trade_pb(symbol: str, msg, db_px_convert: int) -> mdmsg_pb2.PbMessage:
    """Create PbMessage for a Databento equity trade."""
    pb_msg = mdmsg_pb2.PbMessage()
    pb_msg.market = mdmsg_pb2.PbMarket.PBMARKET_TOPBOOK_EQUITY
    pb_msg.symbol_id = symbol

    pb_trade = pb_msg.book_trade

    price = None
    size = None

    if hasattr(msg, 'price'):
        price = float(msg.price) / db_px_convert if msg.price else None
    if hasattr(msg, 'size'):
        size = msg.size

    if price is not None:
        pb_trade.px = str(price)
    if size is not None:
        pb_trade.qty = str(size)

    # ts_event is in nanoseconds, convert to milliseconds
    if hasattr(msg, 'ts_event') and msg.ts_event:
        pb_trade.trade_time = int(msg.ts_event // 1_000_000)
        pb_msg.rx_timestamp = pb_trade.trade_time + 120

    return pb_msg


def store_equity_databento(symbols: List[str], creds_file: str, out_dir: str, dates: List[str],
                           compress: bool, do_upload: bool, dataset: str = "EQUS.MINI"):
    """
    Store historical equity data from Databento.

    Args:
        symbols: List of equity symbols (e.g., ["TSLA", "SPY"])
        creds_file: Path to Databento credentials JSON file
        out_dir: Directory to save output files
        dates: List of dates in YYYYMMDD format
        compress: Whether to compress output
        do_upload: Whether to upload to S3
        dataset: Databento dataset - "EQUS.MINI" (SIP) or "XNAS.BASIC" (NASDAQ Basic)
    """
    with open(creds_file, "r") as f:
        creds = json.load(f)
        api_key = creds["api_key"]

    client = db.Historical(key=api_key)
    et_tz = pytz.timezone("America/New_York")

    # Select schema based on dataset
    # XNAS.BASIC doesn't support mbp-1, use cmbp-1 (consolidated mbp-1) instead
    if dataset == "XNAS.BASIC":
        schema = "cmbp-1"
    else:
        schema = "mbp-1"

    db_px_convert = 1_000_000_000
    written_files = {}

    for date_str in dates:
        print(f"Processing date: {date_str}")
        date_obj = datetime.datetime.strptime(date_str, "%Y%m%d")

        # Use ET timezone for equity market hours
        start_str = date_obj.strftime("%Y-%m-%d")
        end_str = (date_obj + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        print(f"  Fetching Databento {dataset} data from {start_str} to {end_str} (schema: {schema})")

        for symbol in symbols:
            print(f"    Symbol: {symbol}")

            # Rate limiting
            last_recorded_t = 0
            record_interval_ms = 10

            # Quotes
            quote_filename = f"{symbol}_quotes_{date_str}"
            quote_count = 0
            wrote_quotes = False

            if compress:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.gzpbf")
                try:
                    total_quotes_received = 0
                    data = client.timeseries.get_range(
                        dataset=dataset,
                        start=start_str,
                        end=end_str,
                        symbols=[symbol],
                        schema=schema,
                        stype_in="raw_symbol",
                    )

                    with gzip.open(quote_output_path, "wb") as f:
                        for msg in data:
                            # Skip non-quote messages
                            if isinstance(msg, TradeMsg):
                                continue
                            if isinstance(msg, SymbolMappingMsg):
                                continue

                            total_quotes_received += 1
                            quote_pb = build_databento_equity_quote_pb(symbol, msg, db_px_convert)

                            # Rate limiting check
                            if quote_pb.quote.transact_time > 0:
                                if quote_pb.quote.transact_time - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = quote_pb.quote.transact_time

                            write_delimited_protobuf(quote_pb, f)
                            quote_count += 1

                    wrote_quotes = True
                    print(f"      Total quotes received: {total_quotes_received}, written: {quote_count}")
                except Exception as e:
                    print(f"      Error fetching quotes for {symbol} on {date_str}: {e}")
                    traceback.print_exc()
            else:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.jsonl")
                try:
                    total_quotes_received = 0
                    data = client.timeseries.get_range(
                        dataset=dataset,
                        start=start_str,
                        end=end_str,
                        symbols=[symbol],
                        schema=schema,
                        stype_in="raw_symbol",
                    )

                    with open(quote_output_path, "w") as f:
                        for msg in data:
                            if isinstance(msg, TradeMsg):
                                continue
                            if isinstance(msg, SymbolMappingMsg):
                                continue

                            total_quotes_received += 1
                            msg_dict = databento_msg_to_dict(msg)

                            ts_ms = msg_dict.get("ts_event_ms")
                            if ts_ms is not None and ts_ms > 0:
                                if ts_ms - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = ts_ms

                            f.write(json.dumps(msg_dict) + "\n")
                            quote_count += 1

                    wrote_quotes = True
                    print(f"      Total quotes received: {total_quotes_received}, written: {quote_count}")
                except Exception as e:
                    print(f"      Error fetching quotes for {symbol} on {date_str}: {e}")
                    traceback.print_exc()

            if wrote_quotes:
                print(f"      Saved {quote_count} quotes to: {quote_output_path}")
                quote_metadata = {
                    "symbol": symbol,
                    "date": date_str,
                    "channel": "quotes",
                    "dataset": dataset,
                    "schema": schema,
                    "record_count": quote_count,
                    "source": f"Databento.{dataset}",
                }
                quote_metadata_path = os.path.join(out_dir, f"{quote_filename}.metadata.json")
                with open(quote_metadata_path, "w") as meta_file:
                    json.dump(quote_metadata, meta_file, indent=2)

                if compress:
                    id_key = ("TopBookEquity", symbol, "quotes", date_str)
                    written_files[id_key] = quote_output_path

            # Trades - fetch separately with trades schema
            last_recorded_t = 0
            trade_filename = f"{symbol}_trades_{date_str}"
            trade_count = 0
            wrote_trades = False

            if compress:
                trade_output_path = os.path.join(out_dir, f"{trade_filename}.gzpbf")
                try:
                    total_trades_received = 0
                    trade_data = client.timeseries.get_range(
                        dataset=dataset,
                        start=start_str,
                        end=end_str,
                        symbols=[symbol],
                        schema="trades",
                        stype_in="raw_symbol",
                    )

                    with gzip.open(trade_output_path, "wb") as f:
                        for msg in trade_data:
                            if not isinstance(msg, TradeMsg):
                                continue

                            total_trades_received += 1
                            trade_pb = build_databento_equity_trade_pb(symbol, msg, db_px_convert)

                            if trade_pb.book_trade.trade_time > 0:
                                if trade_pb.book_trade.trade_time - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = trade_pb.book_trade.trade_time

                            write_delimited_protobuf(trade_pb, f)
                            trade_count += 1

                    wrote_trades = True
                    print(f"      Total trades received: {total_trades_received}, written: {trade_count}")
                except Exception as e:
                    print(f"      Error fetching trades for {symbol} on {date_str}: {e}")
                    traceback.print_exc()
            else:
                trade_output_path = os.path.join(out_dir, f"{trade_filename}.jsonl")
                try:
                    total_trades_received = 0
                    trade_data = client.timeseries.get_range(
                        dataset=dataset,
                        start=start_str,
                        end=end_str,
                        symbols=[symbol],
                        schema="trades",
                        stype_in="raw_symbol",
                    )

                    with open(trade_output_path, "w") as f:
                        for msg in trade_data:
                            if not isinstance(msg, TradeMsg):
                                continue

                            total_trades_received += 1
                            msg_dict = databento_msg_to_dict(msg)

                            ts_ms = msg_dict.get("ts_event_ms")
                            if ts_ms is not None and ts_ms > 0:
                                if ts_ms - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = ts_ms

                            f.write(json.dumps(msg_dict) + "\n")
                            trade_count += 1

                    wrote_trades = True
                    print(f"      Total trades received: {total_trades_received}, written: {trade_count}")
                except Exception as e:
                    print(f"      Error fetching trades for {symbol} on {date_str}: {e}")
                    traceback.print_exc()

            if wrote_trades:
                print(f"      Saved {trade_count} trades to: {trade_output_path}")
                trade_metadata = {
                    "symbol": symbol,
                    "date": date_str,
                    "channel": "trades",
                    "dataset": dataset,
                    "schema": "trades",
                    "record_count": trade_count,
                    "source": f"Databento.{dataset}",
                }
                trade_metadata_path = os.path.join(out_dir, f"{trade_filename}.metadata.json")
                with open(trade_metadata_path, "w") as meta_file:
                    json.dump(trade_metadata, meta_file, indent=2)

                if compress:
                    id_key = ("TopBookEquity", symbol, "trades", date_str)
                    written_files[id_key] = trade_output_path

    print("We wrote out these files;")
    print(written_files)

    if do_upload:
        s3 = boto3.Session(profile_name='l1').client("s3")
        for one_file_props, one_file_path in written_files.items():
            dest_bucket = "l1-pktrade-capture"
            object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                MKT=one_file_props[0],
                SYM=one_file_props[1],
                CHANNEL=one_file_props[2],
                DATE=one_file_props[3],
            )
            print(f"Uploading {one_file_path} to s3://{dest_bucket}/{object_name}")
            with open(one_file_path, "rb") as f:
                s3.upload_fileobj(f, dest_bucket, object_name)
            print("  Upload complete")

    print("Done processing all dates and symbols.")


def store_equity_databento_boats(symbols: List[str], creds_file: str, out_dir: str,
                                 dates: List[str], compress: bool, do_upload: bool,
                                 s3_market_name: str = "TopBookEquity_Boats"):
    """
    Merge XNAS.BASIC (cmbp-1) for the daytime/extended-hours window with OCEA.MEMOIR (mbp-1)
    for the 8pm-4am ET BOATS overnight window, producing a single TopBookEquity_Boats stream
    per symbol-date. BOATS doesn't run Fri night → Sat morning or Sat night → Sun morning, so
    we just emit XNAS.BASIC for those nights and leave the gap.

    out_dir: local market directory where gzpbf files are written (caller sets this up).
    s3_market_name: name of the S3 market dir when uploading. Defaults to "TopBookEquity_Boats"
        for the standalone variant flow. The backfill flow passes "TopBookEquity" so the
        merged stream lands in the canonical S3 location.
    """
    with open(creds_file, "r") as f:
        api_key = json.load(f)["api_key"]
    client = db.Historical(key=api_key)

    db_px_convert = 1_000_000_000
    written_files = {}

    for date_str in dates:
        print(f"Processing date (boats): {date_str}")
        date_obj = datetime.datetime.strptime(date_str, "%Y%m%d")
        start_str = date_obj.strftime("%Y-%m-%d")
        end_str = (date_obj + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        for symbol in symbols:
            print(f"    Symbol: {symbol}")
            quote_filename = f"{symbol}_quotes_{date_str}"
            if not compress:
                print(f"      Skipping {symbol} {date_str}: boats variant only supports --compress (gzpbf)")
                continue
            quote_output_path = os.path.join(out_dir, f"{quote_filename}.gzpbf")

            def fetch(dataset, schema):
                try:
                    return list(client.timeseries.get_range(
                        dataset=dataset,
                        start=start_str,
                        end=end_str,
                        symbols=[symbol],
                        schema=schema,
                        stype_in="raw_symbol",
                    ))
                except Exception as e:
                    print(f"      Error fetching {dataset}/{schema} for {symbol} on {date_str}: {e}")
                    traceback.print_exc()
                    return []

            xnas_msgs = fetch("XNAS.BASIC", "cmbp-1")
            ocea_msgs = fetch("OCEA.MEMOIR", "mbp-1")

            def is_quote(m):
                if isinstance(m, TradeMsg) or isinstance(m, SymbolMappingMsg):
                    return False
                return True

            merged = [m for m in xnas_msgs if is_quote(m)] + [m for m in ocea_msgs if is_quote(m)]
            merged.sort(key=lambda m: getattr(m, "ts_event", 0))

            last_recorded_t = 0
            record_interval_ms = 10
            quote_count = 0
            with gzip.open(quote_output_path, "wb") as f:
                for msg in merged:
                    quote_pb = build_databento_equity_quote_pb(symbol, msg, db_px_convert)
                    if quote_pb.quote.transact_time > 0:
                        if quote_pb.quote.transact_time - last_recorded_t < record_interval_ms:
                            continue
                        last_recorded_t = quote_pb.quote.transact_time
                    write_delimited_protobuf(quote_pb, f)
                    quote_count += 1

            xnas_n = sum(1 for m in xnas_msgs if is_quote(m))
            ocea_n = sum(1 for m in ocea_msgs if is_quote(m))
            print(f"      XNAS.BASIC quotes: {xnas_n}, OCEA.MEMOIR quotes: {ocea_n}, written: {quote_count}")
            print(f"      Saved to: {quote_output_path}")

            metadata = {
                "symbol": symbol,
                "date": date_str,
                "channel": "quotes",
                "variant": "boats",
                "sources": ["Databento.XNAS.BASIC.cmbp-1", "Databento.OCEA.MEMOIR.mbp-1"],
                "xnas_quote_count": xnas_n,
                "ocea_quote_count": ocea_n,
                "record_count": quote_count,
            }
            with open(os.path.join(out_dir, f"{quote_filename}.metadata.json"), "w") as mf:
                json.dump(metadata, mf, indent=2)

            id_key = (s3_market_name, symbol, "quotes", date_str)
            written_files[id_key] = quote_output_path

    print(written_files)

    if do_upload:
        s3 = boto3.Session(profile_name='l1').client("s3")
        for one_file_props, one_file_path in written_files.items():
            dest_bucket = "l1-pktrade-capture"
            object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                MKT=one_file_props[0], SYM=one_file_props[1],
                CHANNEL=one_file_props[2], DATE=one_file_props[3],
            )
            print(f"Uploading {one_file_path} to s3://{dest_bucket}/{object_name}")
            with open(one_file_path, "rb") as f:
                s3.upload_fileobj(f, dest_bucket, object_name)
            print("  Upload complete")

    print("Done processing all boats dates and symbols.")


def store_equity(symbols: List[str], creds_file: str, out_dir: str, dates: List[str], compress: bool, do_upload: bool):
    with open(creds_file, "r") as f:
        creds = json.load(f)
        api_key = creds["api_key"]

    client = RESTClient(api_key)
    et_tz = pytz.timezone("America/New_York")

    written_files = {}

    for date_str in dates:
        print(f"Processing date: {date_str}")
        date_obj = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        start_dt = et_tz.localize(datetime.datetime.combine(date_obj, datetime.time(0, 0, 0)))
        end_dt = start_dt + datetime.timedelta(days=1)

        start_ns = int(start_dt.timestamp() * 1_000_000_000)
        end_ns = int(end_dt.timestamp() * 1_000_000_000)

        print(f"  Fetching SIP data from {start_dt.isoformat()} to {end_dt.isoformat()} (ET)")

        for symbol in symbols:
            print(f"    Symbol: {symbol}")

            # Rate limiting how often we store messages.
            # This echoes what we do in pymultifeed
            last_recorded_t = 0
            record_interval_ms = 10

            # Quotes (top of book)
            quote_filename = f"{symbol}_quotes_{date_str}"
            quote_count = 0
            wrote_quotes = False
            if compress:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.gzpbf")
                try:
                    total_quotes_received = 0
                    with gzip.open(quote_output_path, "wb") as f:
                        for quote in client.list_quotes(
                            ticker=symbol,
                            timestamp_gte=start_ns,
                            timestamp_lt=end_ns,
                            limit=50000,
                            order='asc',
                        ):
                            total_quotes_received += 1
                            quote_pb = build_polygon_quote_pb(symbol, quote)

                            # Rate limiting check
                            if quote_pb.quote.transact_time > 0:
                                if quote_pb.quote.transact_time - last_recorded_t < record_interval_ms:
                                    continue  # Skip this message, too soon since last publish
                                last_recorded_t = quote_pb.quote.transact_time

                            write_delimited_protobuf(quote_pb, f)
                            quote_count += 1
                    wrote_quotes = True
                    print(f"      Total quotes received: {total_quotes_received}, written: {quote_count}")
                except Exception as e:
                    print(f"      Error fetching quotes for {symbol} on {date_str}: {e}")
                    traceback.print_exc()
            else:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.jsonl")
                try:
                    total_quotes_received = 0
                    with open(quote_output_path, "w") as f:
                        for quote in client.list_quotes(
                            ticker=symbol,
                            timestamp_gte=start_ns,
                            timestamp_lt=end_ns,
                            limit=50000,
                            order='asc',
                        ):
                            total_quotes_received += 1
                            quote_dict = polygon_quote_to_dict(quote)

                            # Rate limiting check (sip_timestamp is in nanoseconds, convert to ms)
                            sip_ts = quote_dict.get("sip_timestamp")
                            if sip_ts is not None and sip_ts > 0:
                                sip_ts_ms = int(sip_ts // 1_000_000)
                                # Don'r skip so we see. 
                                #print("{} {} {}".format(sip_ts_ms, last_recorded_t,sip_ts_ms - last_recorded_t ))
                                if sip_ts_ms - last_recorded_t < record_interval_ms:
                                    continue  # Skip this message, too soon since last publish
                                last_recorded_t = sip_ts_ms

                            #print(quote_dict)

                            f.write(json.dumps(quote_dict) + "\n")
                            quote_count += 1
                    wrote_quotes = True
                    print(f"      Total quotes received: {total_quotes_received}, written: {quote_count}")
                except Exception as e:
                    print(f"      Error fetching quotes for {symbol} on {date_str}: {e}")
                    traceback.print_exc()

            if wrote_quotes:
                print(f"      Saved {quote_count} quotes to: {quote_output_path}")
                quote_metadata = {
                    "symbol": symbol,
                    "date": date_str,
                    "channel": "quotes",
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "record_count": quote_count,
                    "source": "Polygon.SIP",
                }
                quote_metadata_path = os.path.join(out_dir, f"{quote_filename}.metadata.json")
                with open(quote_metadata_path, "w") as meta_file:
                    json.dump(quote_metadata, meta_file, indent=2)

                if compress:
                    id_key = ("TopBookEquity", symbol, "quotes", date_str)
                    written_files[id_key] = quote_output_path

            # Trades
            # Reset rate limiting for trades
            last_recorded_t = 0
            record_interval_ms = 10

            trade_filename = f"{symbol}_trades_{date_str}"
            trade_count = 0
            wrote_trades = False
            if compress:
                trade_output_path = os.path.join(out_dir, f"{trade_filename}.gzpbf")
                try:
                    total_trades_received = 0
                    with gzip.open(trade_output_path, "wb") as f:
                        for trade in client.list_trades(
                            ticker=symbol,
                            timestamp_gte=start_ns,
                            timestamp_lt=end_ns,
                            limit=50000,
                            order='asc',
                        ):
                            total_trades_received += 1
                            trade_pb = build_polygon_trade_pb(symbol, trade)

                            # Rate limiting check
                            if trade_pb.book_trade.trade_time > 0:
                                if trade_pb.book_trade.trade_time - last_recorded_t < record_interval_ms:
                                    continue  # Skip this message, too soon since last publish
                                last_recorded_t = trade_pb.book_trade.trade_time

                            write_delimited_protobuf(trade_pb, f)
                            trade_count += 1
                    wrote_trades = True
                    print(f"      Total trades received: {total_trades_received}, written: {trade_count}")
                except Exception as e:
                    print(f"      Error fetching trades for {symbol} on {date_str}: {e}")
                    traceback.print_exc()
            else:
                trade_output_path = os.path.join(out_dir, f"{trade_filename}.jsonl")
                try:
                    total_trades_received = 0
                    with open(trade_output_path, "w") as f:
                        for trade in client.list_trades(
                            ticker=symbol,
                            timestamp_gte=start_ns,
                            timestamp_lt=end_ns,
                            limit=50000,
                            order='asc',
                        ):
                            total_trades_received += 1
                            trade_dict = polygon_trade_to_dict(trade)

                            # Rate limiting check (sip_timestamp is in nanoseconds, convert to ms)
                            sip_ts = trade_dict.get("sip_timestamp")
                            if sip_ts is not None and sip_ts > 0:
                                sip_ts_ms = int(sip_ts // 1_000_000)
                                if sip_ts_ms - last_recorded_t < record_interval_ms:
                                    continue  # Skip this message, too soon since last publish
                                last_recorded_t = sip_ts_ms

                            f.write(json.dumps(trade_dict) + "\n")
                            trade_count += 1
                    wrote_trades = True
                    print(f"      Total trades received: {total_trades_received}, written: {trade_count}")
                except Exception as e:
                    print(f"      Error fetching trades for {symbol} on {date_str}: {e}")
                    traceback.print_exc()

            if wrote_trades:
                print(f"      Saved {trade_count} trades to: {trade_output_path}")
                trade_metadata = {
                    "symbol": symbol,
                    "date": date_str,
                    "channel": "trades",
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "record_count": trade_count,
                    "source": "Polygon.SIP",
                }
                trade_metadata_path = os.path.join(out_dir, f"{trade_filename}.metadata.json")
                with open(trade_metadata_path, "w") as meta_file:
                    json.dump(trade_metadata, meta_file, indent=2)

                if compress:
                    id_key = ("TopBookEquity", symbol, "trades", date_str)
                    written_files[id_key] = trade_output_path

    print("We wrote out these files;")
    print(written_files)

    if do_upload:
        s3 = boto3.Session(profile_name='l1').client("s3")
        for one_file_props, one_file_path in written_files.items():
            dest_bucket = "l1-pktrade-capture"
            object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                MKT=one_file_props[0],
                SYM=one_file_props[1],
                CHANNEL=one_file_props[2],
                DATE=one_file_props[3],
            )
            print(f"Uploading {one_file_path} to s3://{dest_bucket}/{object_name}")
            with open(one_file_path, "rb") as f:
                s3.upload_fileobj(f, dest_bucket, object_name)
            print("  Upload complete")

    print("Done processing all dates and symbols.")

# PolygonFX, which uses a different path. 
def store_fx(symbols: List[str], creds_file: str, out_dir: str, dates: List[str], compress: bool, do_upload: bool):
    with open(creds_file, "r") as f:
        creds = json.load(f)
        api_key = creds["api_key"]

    client = RESTClient(api_key)
    et_tz = pytz.timezone("America/New_York")

    written_files = {}

    for date_str in dates:
        print(f"Processing date: {date_str}")
        date_obj = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        start_dt = et_tz.localize(datetime.datetime.combine(date_obj, datetime.time(0, 0, 0)))
        end_dt = start_dt + datetime.timedelta(days=1)

        start_ns = int(start_dt.timestamp() * 1_000_000_000)
        end_ns = int(end_dt.timestamp() * 1_000_000_000)

        print(f"  Fetching polygonfx data from {start_dt.isoformat()} to {end_dt.isoformat()} (ET)")

        for symbol in symbols:
            print(f"    Symbol: {symbol}")
            polygon_name = symbolizer.polygonfx_easyname_to_feed[symbol]

            # Rate limiting how often we store messages.
            # This echoes what we do in pymultifeed
            last_recorded_t = 0
            record_interval_ms = 20

            # Quotes (top of book)
            quote_filename = f"{symbol}_quotes_{date_str}"
            quote_count = 0
            wrote_quotes = False
            if compress:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.gzpbf")
                try:
                    total_quotes_received = 0
                    with gzip.open(quote_output_path, "wb") as f:
                        for quote in client.list_quotes(
                            ticker=polygon_name,
                            timestamp_gte=start_ns,
                            timestamp_lt=end_ns,
                            limit=50000,
                            order='asc',
                        ):
                            total_quotes_received += 1
                            quote_pb = build_polygonfx_quote_pb(symbol, quote)

                            # Rate limiting check
                            if quote_pb.quote.transact_time > 0:
                                if quote_pb.quote.transact_time - last_recorded_t < record_interval_ms:
                                    continue  # Skip this message, too soon since last publish
                                last_recorded_t = quote_pb.quote.transact_time

                            write_delimited_protobuf(quote_pb, f)
                            quote_count += 1
                    wrote_quotes = True
                    print(f"      Total quotes received: {total_quotes_received}, written: {quote_count}")
                except Exception as e:
                    print(f"      Error fetching quotes for {symbol} on {date_str}: {e}")
                    traceback.print_exc()
            else:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.jsonl")
                try:
                    total_quotes_received = 0
                    with open(quote_output_path, "w") as f:
                        for quote in client.list_quotes(
                            ticker=polygon_name,
                            timestamp_gte=start_ns,
                            timestamp_lt=end_ns,
                            limit=50000,
                            order='asc',
                        ):
                            total_quotes_received += 1
                            quote_dict = polygonfx_quote_to_dict(symbol, quote)

                            # Rate limiting check (sip_timestamp is in nanoseconds, convert to ms)
                            ts = quote_dict.get("participant_timestamp")
                            if ts is not None and ts > 0:
                                ts = int(ts // 1_000_000)
                                # Don'r skip so we see. 
                                #print("{} {} {}".format(sip_ts_ms, last_recorded_t,sip_ts_ms - last_recorded_t ))
                                if ts - last_recorded_t < record_interval_ms:
                                    continue  # Skip this message, too soon since last publish
                                last_recorded_t = ts

                            #print(quote_dict)

                            f.write(json.dumps(quote_dict) + "\n")
                            quote_count += 1
                    wrote_quotes = True
                    print(f"      Total quotes received: {total_quotes_received}, written: {quote_count}")
                except Exception as e:
                    print(f"      Error fetching quotes for {symbol} on {date_str}: {e}")
                    traceback.print_exc()

            if wrote_quotes:
                print(f"      Saved {quote_count} quotes to: {quote_output_path}")
                quote_metadata = {
                    "symbol": symbol,
                    "date": date_str,
                    "channel": "quotes",
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "record_count": quote_count,
                    "source": "Polygon.FX",
                }
                quote_metadata_path = os.path.join(out_dir, f"{quote_filename}.metadata.json")
                with open(quote_metadata_path, "w") as meta_file:
                    json.dump(quote_metadata, meta_file, indent=2)

                if compress:
                    id_key = ("TopBookEquity", symbol, "quotes", date_str)
                    written_files[id_key] = quote_output_path


    print("We wrote out these files;")
    print(written_files)

    if do_upload:
        s3 = boto3.Session(profile_name='l1').client("s3")
        for one_file_props, one_file_path in written_files.items():
            dest_bucket = "l1-pktrade-capture"
            object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                MKT=one_file_props[0],
                SYM=one_file_props[1],
                CHANNEL=one_file_props[2],
                DATE=one_file_props[3],
            )
            print(f"Uploading {one_file_path} to s3://{dest_bucket}/{object_name}")
            with open(one_file_path, "rb") as f:
                s3.upload_fileobj(f, dest_bucket, object_name)
            print("  Upload complete")

    print("Done processing all dates and symbols.")



def build_pyth_quote_pb(symbol: str, price: float, publish_time_s: int) -> mdmsg_pb2.PbMessage:
    """Create PbMessage for a Pyth price update. Bid=Ask=Mid since Pyth only provides a single price."""
    pb_msg = mdmsg_pb2.PbMessage()
    pb_msg.market = mdmsg_pb2.PbMarket.PBMARKET_TOPBOOK_EQUITY
    pb_msg.symbol_id = symbol

    pb_quote = pb_msg.quote
    px_str = str(price)
    pb_quote.best_bid_px = px_str
    pb_quote.best_bid_qty = "1"
    pb_quote.best_ask_px = px_str
    pb_quote.best_ask_qty = "1"
    pb_quote.mid_px = round(price, 6)

    # Pyth publish_time is in seconds, convert to ms
    pb_quote.transact_time = publish_time_s * 1000
    # Looking at our pymultifeed slowdowns. 
    pb_msg.rx_timestamp = pb_quote.transact_time + 1500

    return pb_msg


async def _fetch_pyth_window(session, feed_id, ts, interval):
    """Fetch a single 60s window from Pyth Benchmarks API, with retry on 429."""
    url = f"{PYTH_BENCHMARKS_URL}/v1/updates/price/{ts}/{interval}"
    for attempt in range(3):
        try:
            async with session.get(url, params={"ids": feed_id, "parsed": "true"}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return ts, await resp.json()
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(1)
    return ts, None


async def _fetch_pyth_day(feed_id, day_start_ts, day_end_ts, batch_size=30, batch_interval=10.0):
    """
    Fetch all 60s windows for a day in batches.
    Pyth rate limit is 30 requests per 10 seconds.
    Fire batch_size requests concurrently, then wait until batch_interval seconds
    have elapsed since the batch started. ~8 min per day.
    """
    # Build list of (ts, interval) pairs
    windows = []
    cur_ts = day_start_ts
    while cur_ts < day_end_ts:
        interval = min(60, day_end_ts - cur_ts)
        windows.append((cur_ts, interval))
        cur_ts += interval

    results = []
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(windows), batch_size):
            batch = windows[i:i + batch_size]
            batch_start = time.time()

            tasks = [_fetch_pyth_window(session, feed_id, ts, iv) for ts, iv in batch]
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)

            # Wait until batch_interval has elapsed since batch start
            elapsed = time.time() - batch_start
            if elapsed < batch_interval and i + batch_size < len(windows):
                await asyncio.sleep(batch_interval - elapsed)

    # Sort by timestamp to ensure chronological order
    results.sort(key=lambda x: x[0])
    return results


def store_pyth(symbols: List[str], out_dir: str, dates: List[str], compress: bool, do_upload: bool):
    """
    Store historical Pyth price data from the Benchmarks API.
    Writes to TopBookEquity directory, similar to how FX data is handled.
    """
    written_files = {}

    for date_str in dates:
        print(f"Processing date: {date_str}")
        date_obj = datetime.datetime.strptime(date_str, "%Y%m%d")

        # Full UTC day
        day_start_ts = int(date_obj.replace(tzinfo=datetime.timezone.utc).timestamp())
        day_end_ts = day_start_ts + 86400

        for symbol in symbols:
            print(f"    Symbol: {symbol}")

            if symbol not in pyth_names_to_addresses:
                print(f"      Error: No Pyth feed ID for {symbol}, skipping")
                continue

            feed_id = pyth_names_to_addresses[symbol]
            last_recorded_t = 0
            record_interval_ms = 20

            quote_filename = f"{symbol}_quotes_{date_str}"
            quote_count = 0
            total_updates_received = 0

            if compress:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.gzpbf")
                open_fn = lambda p: gzip.open(p, "wb")
            else:
                quote_output_path = os.path.join(out_dir, f"{quote_filename}.jsonl")
                open_fn = lambda p: open(p, "w")

            try:
                # Fetch all windows concurrently
                print(f"      Fetching from Pyth Benchmarks API ({(day_end_ts - day_start_ts) // 60} requests)...")
                results = asyncio.run(_fetch_pyth_day(feed_id, day_start_ts, day_end_ts))

                errors = sum(1 for _, data in results if data is None)
                if errors > 0:
                    print(f"      Warning: {errors} requests failed")

                with open_fn(quote_output_path) as f:
                    for window_ts, updates in results:
                        if updates is None:
                            continue

                        for update in updates:
                            parsed_list = update.get("parsed", [])
                            for parsed in parsed_list:
                                total_updates_received += 1

                                price_data = parsed.get("price", {})
                                px_raw = float(price_data.get("price", 0))
                                px_expo = int(price_data.get("expo", 0))
                                publish_time_s = int(price_data.get("publish_time", 0))

                                price = px_raw * (10 ** px_expo)

                                publish_time_ms = publish_time_s * 1000
                                if publish_time_ms - last_recorded_t < record_interval_ms:
                                    continue
                                last_recorded_t = publish_time_ms

                                if compress:
                                    pb = build_pyth_quote_pb(symbol, price, publish_time_s)
                                    write_delimited_protobuf(pb, f)
                                else:
                                    record = {
                                        "symbol": symbol,
                                        "price": price,
                                        "publish_time": publish_time_s,
                                        "publish_time_ms": publish_time_ms,
                                    }
                                    f.write(json.dumps(record) + "\n")

                                quote_count += 1

                print(f"      Total updates received: {total_updates_received}, written: {quote_count}")
                print(f"      Saved {quote_count} quotes to: {quote_output_path}")

                metadata = {
                    "symbol": symbol,
                    "date": date_str,
                    "channel": "quotes",
                    "record_count": quote_count,
                    "source": "Pyth.Benchmarks",
                }
                metadata_path = os.path.join(out_dir, f"{quote_filename}.metadata.json")
                with open(metadata_path, "w") as meta_file:
                    json.dump(metadata, meta_file, indent=2)

                if compress:
                    id_key = ("TopBookEquity", symbol, "quotes", date_str)
                    written_files[id_key] = quote_output_path

            except Exception as e:
                print(f"      Error processing {symbol} on {date_str}: {e}")
                traceback.print_exc()
                continue

    print("We wrote out these files;")
    print(written_files)

    if do_upload:
        s3 = boto3.Session(profile_name='l1').client("s3")
        for one_file_props, one_file_path in written_files.items():
            dest_bucket = "l1-pktrade-capture"
            object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                MKT=one_file_props[0],
                SYM=one_file_props[1],
                CHANNEL=one_file_props[2],
                DATE=one_file_props[3],
            )
            print(f"Uploading {one_file_path} to s3://{dest_bucket}/{object_name}")
            with open(one_file_path, "rb") as f_up:
                s3.upload_fileobj(f_up, dest_bucket, object_name)
            print("  Upload complete")

    print("Done processing all dates and symbols.")


def backfill_missing_data(symbols: List[str] = None, start: str = None, end: str = None,
                          creds_dir: str = None, out_dir: str = None,
                          compress: bool = True, upload: bool = False,
                          equity_source: str = "polygon",
                          force: bool = False,
                          market: str = None):
    """
    Backfill missing historical data for symbols.

    Args:
        symbols: List of symbols to backfill. If None, uses all symbols from symbolizer.syms_to_market
        start: Start date (YYYYMMDD). If None, uses go-live date for each symbol
        end: End date (YYYYMMDD). If None, uses today
        creds_dir: Directory containing credentials files
        out_dir: Base output directory for data files
        compress: Whether to compress output
        upload: Whether to upload to S3
        equity_source: Data source for equities - "polygon", "databento", or "databentonasdaq"
        force: If True, regenerate data even if it already exists in S3
        market: If set, only backfill symbols belonging to this market (e.g. "TopBookCme")
    """
    import getpass
    from collections import defaultdict

    if creds_dir is None:
        creds_dir = "/home/{}/.creds/".format(getpass.getuser())

    if out_dir is None:
        out_dir = "/home/{}/tardis_datasets/".format(getpass.getuser())

    if compress:
        local_dir = os.path.join(out_dir, "gzpbf")
    else:
        local_dir = os.path.join(out_dir, "raw")

    # Default to all symbols if not specified
    if symbols is None:
        symbols = list(symbolizer.syms_to_market.keys())
        print(f"Using all symbols from symbolizer: {symbols}")

    # Default end date to today if not specified
    if end is None:
        end = chron.get_date_easy("TODAY")
        print(f"Using end date: {end}")

    # Group symbols by market and determine date ranges
    market_to_symbols_dates = defaultdict(lambda: defaultdict(list))

    for symbol in symbols:
        if symbol not in symbolizer.syms_to_market:
            print(f"Warning: {symbol} not found in symbolizer.syms_to_market, skipping")
            continue

        symbol_market = symbolizer.syms_to_market[symbol]

        if market is not None and symbol_market != market:
            continue

        # Determine start date for this symbol
        if start is None:
            # Use go-live date
            if symbol in symbolizer.syms_go_live_dates:
                symbol_start = symbolizer.syms_go_live_dates[symbol]
            else:
                print(f"Warning: {symbol} not found in syms_go_live_dates, skipping")
                continue
        else:
            # Use provided start date, but respect go-live date as minimum
            if symbol in symbolizer.syms_go_live_dates:
                go_live = symbolizer.syms_go_live_dates[symbol]
                symbol_start = max(start, go_live)
                if start < go_live:
                    print(f"Note: Adjusting start date for {symbol} from {start} to {go_live} (go-live date)")
            else:
                symbol_start = start

        # Build the date range for this symbol
        dates = chron.dates_list(symbol_start, end, dates_method="ALLDAYS")
        print(end)
        print(dates)

        # Store symbol and its dates for this market
        market_to_symbols_dates[symbol_market][symbol] = dates

    # Now check what's missing for each market and backfill
    for market, symbols_dates in market_to_symbols_dates.items():
        print(f"\n{'='*80}")
        print(f"Processing market: {market}")
        print(f"{'='*80}")

        # Build syms_books_to_dates structure for md_exists.missing_data_files()
        syms_books_to_dates = {}
        for symbol, dates in symbols_dates.items():
            syms_books_to_dates[(symbol, market)] = dates

        # Check what's missing
        print(f"Checking for missing files in {local_dir}...")
        missing_info = md_exists.missing_data_files(syms_books_to_dates, local_dir)

        # files_to_copy_props = files that MIGHT be in S3 (based on market whitelist)
        # missing = files that don't exist locally
        potentially_in_s3 = missing_info["files_to_copy_props"]
        files_to_generate_list = list(missing_info["missing"])

        if force:
            # Force mode: regenerate everything that's missing locally, even if in S3
            print(f"Force mode enabled - will regenerate all {len(potentially_in_s3)} files even if in S3")
            for file_path in potentially_in_s3.keys():
                if file_path not in files_to_generate_list:
                    files_to_generate_list.append(file_path)
        else:
            # Actually check S3 to see which files exist
            print(f"Checking S3 for {len(potentially_in_s3)} potentially available files...")
            s3 = boto3.Session(profile_name='l1').client("s3")
            src_bucket = "l1-pktrade-capture"

            actually_in_s3 = {}
            not_in_s3 = {}

            for file_path, file_props in potentially_in_s3.items():
                object_name = "gzpbf/{MKT}/{SYM}_{CHANNEL}_{DATE}.gzpbf".format(
                    MKT=file_props["mkt"],
                    SYM=file_props["sym"],
                    CHANNEL=file_props["channel"],
                    DATE=file_props["date"],
                )

                try:
                    # Check if object exists in S3
                    s3.head_object(Bucket=src_bucket, Key=object_name)
                    actually_in_s3[file_path] = file_props
                except:
                    # File doesn't exist in S3, needs to be generated
                    not_in_s3[file_path] = file_props
                    files_to_generate_list.append(file_path)

            if actually_in_s3:
                print(f"Found {len(actually_in_s3)} files actually in S3 (can download if needed)")
                print(f"  Example: {list(actually_in_s3.keys())[:3]}")

            if not_in_s3:
                print(f"Found {len(not_in_s3)} files NOT in S3 (need to generate)")
                print(f"  Example: {list(not_in_s3.keys())[:3]}")

        if not files_to_generate_list:
            print(f"No files need generation for {market} (all exist locally or in S3)")
            continue

        # Parse the missing file paths to extract symbol and date
        # Format: "market/symbol_channel_date.gzpbf"
        symbol_dates_to_fetch = defaultdict(set)
        for file_path in files_to_generate_list:
            # Extract from path like "TopBookCme/NQ_quotes_20251120.gzpbf"
            parts = file_path.split("/")
            if len(parts) == 2:
                filename = parts[1].replace(".gzpbf", "")
                # Split by underscore: symbol_channel_date
                file_parts = filename.split("_")
                if len(file_parts) >= 3:
                    sym = file_parts[0]
                    date = file_parts[2]
                    symbol_dates_to_fetch[sym].add(date)

        print(f"Found {len(files_to_generate_list)} files to generate for {market}")
        print(f"Symbols to backfill: {list(symbol_dates_to_fetch.keys())}")

        # Fetch data for this market
        if market == "TopBookCme":
            creds_file = os.path.join(creds_dir, ".DataBento.creds.json")
            market_out_dir = os.path.join(local_dir, "TopBookCme")

            if not os.path.exists(market_out_dir):
                os.makedirs(market_out_dir)

            # Fetch each symbol only for its specific missing dates
            for symbol, dates_set in symbol_dates_to_fetch.items():
                dates_to_fetch = sorted(dates_set)
                print(f"Fetching CME data for {symbol} across {len(dates_to_fetch)} dates: {dates_to_fetch}")
                store_cme([symbol], creds_file, market_out_dir, dates_to_fetch, compress, upload)

        elif market == "TopBookEquity":
            polygon_creds_file = os.path.join(creds_dir, ".Polygon.creds.json")
            fx_creds_file = os.path.join(creds_dir, ".PolygonFX.creds.json")
            databento_creds_file = os.path.join(creds_dir, ".DataBento.creds.json")
            market_out_dir = os.path.join(local_dir, "TopBookEquity")

            if not os.path.exists(market_out_dir):
                os.makedirs(market_out_dir)

            # Symbols we don't yet have a backfill source for. Revisit
            # (e.g. add a Pyth feed ID) when we want to bring them online.
            backfill_skip_symbols = {"NIKKEI225", "KS", "KOSPI200"}

            # Fetch each symbol only for its specific missing dates
            for symbol, dates_set in symbol_dates_to_fetch.items():
                dates_to_fetch = sorted(dates_set)

                if symbol in backfill_skip_symbols:
                    print(f"Skipping {symbol}: no backfill source configured")
                    continue

                # Determine if this is a Pyth-only, FX, or regular equity symbol
                if symbol in symbolizer.pyth_only_symbols:
                    print(f"Fetching Pyth data for {symbol} across {len(dates_to_fetch)} dates: {dates_to_fetch}")
                    store_pyth([symbol], market_out_dir, dates_to_fetch, compress, upload)
                elif symbol in symbolizer.polygonfx_easyname_to_feed:
                    print(f"Fetching FX data for {symbol} across {len(dates_to_fetch)} dates: {dates_to_fetch}")
                    store_fx([symbol], fx_creds_file, market_out_dir, dates_to_fetch, compress, upload)
                else:
                    print(f"Fetching Equity data for {symbol} (source: {equity_source}) across {len(dates_to_fetch)} dates: {dates_to_fetch}")
                    if equity_source == "polygon":
                        store_equity([symbol], polygon_creds_file, market_out_dir, dates_to_fetch, compress, upload)
                    elif equity_source == "databento":
                        store_equity_databento([symbol], databento_creds_file, market_out_dir, dates_to_fetch, compress, upload, dataset="EQUS.MINI")
                    elif equity_source == "databentonasdaq":
                        # Default: write the merged XNAS.BASIC daytime + OCEA.MEMOIR overnight
                        # (boats) stream into the canonical TopBookEquity/ dir. S3 upload
                        # also goes to gzpbf/TopBookEquity/ (s3_market_name="TopBookEquity").
                        store_equity_databento_boats(
                            [symbol], databento_creds_file, market_out_dir,
                            dates_to_fetch, compress, upload,
                            s3_market_name="TopBookEquity",
                        )

        else:
            print(f"Warning: Market {market} not supported for backfill, skipping")

    print(f"\n{'='*80}")
    print("Backfill complete!")
    print(f"{'='*80}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Backfill mode: automatically detect and fill missing data")
    parser.add_argument("--force-backfill", action="store_true",
                        help="Force regeneration even if data exists in S3 (use with --backfill)")
    parser.add_argument("--market", required=False,
                        help="Market to fetch data for (required in non-backfill mode)")
    parser.add_argument("--symbols", required=False,
                        help="Comma-separated symbols (in backfill mode, defaults to all symbols)")
    parser.add_argument("--creds", default="/home/{}/.creds/".format(getpass.getuser()))

    parser.add_argument("--outdir", default="/home/{}/tardis_datasets/".format(getpass.getuser()))
    parser.add_argument("--start", required=False,
                        help="Start date YYYYMMDD (in backfill mode, defaults to go-live dates)")
    parser.add_argument("--end", required=False,
                        help="End date YYYYMMDD (in backfill mode, defaults to today)")
    parser.add_argument("--compress", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--source", required=False, default="databentonasdaq",
                        choices=["polygon", "databento", "databentonasdaq"],
                        help="Data source for equities: polygon (default), databento (EQUS.MINI SIP), databentonasdaq (XNAS.BASIC NASDAQ)")
    parser.add_argument("--variant", required=False, default="",
                        choices=["", "Boats"],
                        help="Equity variant suffix. 'Boats' merges XNAS.BASIC daytime + OCEA.MEMOIR overnight into TopBookEquity_Boats/. Pass the same string to the C++ sim's --equity-variant.")
    args = parser.parse_args()

    # Handle backfill mode
    if args.backfill:
        symbols = args.symbols.split(",") if args.symbols else None
        backfill_missing_data(
            symbols=symbols,
            start=args.start,
            end=args.end,
            creds_dir=args.creds,
            out_dir=args.outdir,
            compress=args.compress,
            upload=args.upload,
            equity_source=args.source,
            force=args.force_backfill,
            market=args.market,
        )
        return

    # Non-backfill mode requires market, symbols, start, end
    if not args.market or not args.symbols or not args.start or not args.end:
        parser.error("In non-backfill mode, --market, --symbols, --start, and --end are required")

    # Let's get the dates.
    dates = chron.dates_list(args.start, args.end, dates_method="ALLDAYS")

    if args.market == "TopBookCme":
        creds_file = os.path.join(args.creds, ".DataBento.creds.json")

        if args.compress:
            out_dir = os.path.join(args.outdir, "gzpbf")
        else:
            out_dir = os.path.join(args.outdir, "raw")
        # Make this match the market name in mdapi.h
        out_dir = os.path.join(out_dir, "TopBookCme")

        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        # Do stuff for
        store_cme(args.symbols.split(","), creds_file, out_dir, dates, args.compress, args.upload)
    elif args.market == "TopBookEquity":
        if args.compress:
            out_dir = os.path.join(args.outdir, "gzpbf")
        else:
            out_dir = os.path.join(args.outdir, "raw")
        market_dir_name = f"TopBookEquity_{args.variant}" if args.variant else "TopBookEquity"
        out_dir = os.path.join(out_dir, market_dir_name)

        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        if args.variant == "Boats":
            creds_file = os.path.join(args.creds, ".DataBento.creds.json")
            store_equity_databento_boats(args.symbols.split(","), creds_file, out_dir, dates, args.compress, args.upload)
        elif args.source == "polygon":
            creds_file = os.path.join(args.creds, ".Polygon.creds.json")
            store_equity(args.symbols.split(","), creds_file, out_dir, dates, args.compress, args.upload)
        elif args.source == "databento":
            creds_file = os.path.join(args.creds, ".DataBento.creds.json")
            store_equity_databento(args.symbols.split(","), creds_file, out_dir, dates, args.compress, args.upload, dataset="EQUS.MINI")
        elif args.source == "databentonasdaq":
            creds_file = os.path.join(args.creds, ".DataBento.creds.json")
            store_equity_databento(args.symbols.split(","), creds_file, out_dir, dates, args.compress, args.upload, dataset="XNAS.BASIC")
    elif args.market == "FX":
        # Also uses the TopBookEquity dir, but has a different upstream path. 
        # We use polygonfx data. 

        # I think the polygonfx timing is something like 1s delayed to tokyo, fwiw. 
        creds_file = os.path.join(args.creds, ".PolygonFX.creds.json")

        if args.compress:
            out_dir = os.path.join(args.outdir, "gzpbf")
        else:
            out_dir = os.path.join(args.outdir, "raw")
        # Make this match the market name in mdapi.h 
        out_dir = os.path.join(out_dir, "TopBookEquity")
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        store_fx(args.symbols.split(","), creds_file, out_dir, dates, args.compress, args.upload)
    elif args.market == "Pyth":
        # Writes to TopBookEquity dir, same pattern as FX.
        # No creds needed - Pyth Benchmarks API is public.
        if args.compress:
            out_dir = os.path.join(args.outdir, "gzpbf")
        else:
            out_dir = os.path.join(args.outdir, "raw")
        out_dir = os.path.join(out_dir, "TopBookEquity")
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        store_pyth(args.symbols.split(","), out_dir, dates, args.compress, args.upload)


if __name__ == "__main__":
    main()
