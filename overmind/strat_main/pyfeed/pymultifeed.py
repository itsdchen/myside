#! /usr/bin/env python

"""

Similar to pkmultifeed, but written in python, and just for hyperliquid.


Right now: don't have to deal with rolls.
Soon: will need to do this properly.

The E-mini S&P 500 futures contract, denoted by "ES", expires quarterly on the third
Friday of March, June, September, and December. These expirations are also known
as the quarterly expirations. Additionally, the E-mini S&P 500 futures offers weekly
options with Wednesday and Friday expirations.

For now, I'm going to keep it going

Note to self, if we need to figure out polygonfx symbol names, it's here:
https://github.com/massive-com/client-python/blob/master/examples/rest/forex-tickers.py

"""

import asyncio
import aiohttp

import argparse
import getpass
import math
import requests
import signal
import socket
import sys
import time
import json
import commentjson
import os
import threading
from requests.exceptions import HTTPError
from urllib.parse import urlencode
import contextlib
from collections import defaultdict


sys.path.append("{}/..".format(os.path.dirname(os.path.abspath(__file__))))
sys.path.append("{}/../util".format(os.path.dirname(os.path.abspath(__file__))))


from util import email_utils

# This needs to be just copied over.
#from strat_main.util import symbolizer
import symbolizer

#import gateway_pb2
import datetime
import hashlib

import websockets
import websockets.exceptions
from tenacity import retry, wait_exponential, wait_fixed, stop_after_attempt
import traceback

from typing import Callable, List, Optional
from concurrent.futures import CancelledError

import zmq
import zmq.asyncio
import time
import uvloop
import mdmsg_pb2

import logging
import queue
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

import sys

import chron
import pytz

import databento as db
from databento import (
    SymbolMappingMsg,
    CMBP1Msg,
    MBP1Msg,
    TradeMsg,
    StatusMsg,
    ErrorMsg,
    SystemMsg,
    ErrorCode
) # Add other types as needed (e.g., OHLCVMsg)
from databento.common.enums import RecordFlags as RF

from polygon import WebSocketClient
from polygon.websocket.models import (
    WebSocketMessage,
    EquityAgg,
    EquityQuote,
    EquityTrade,
    ForexQuote,
    Feed,
    Market,
)



class HeartbeatException(Exception):
    """Exception raised from heartbeat to trigger retry"""
class HeartbeatTimeout(HeartbeatException):
    """Raised when a market data feed stops delivering data."""
class HeartbeatNotSubscribed(HeartbeatException):
    """Raised when a we have no confirmed subscription to data."""

class ForceRestartFeed(Exception):
    """Exception raised to trigger feed's retry decorator"""

def make_fast_logger(name: str, file_name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)   # start here; DEBUG only when needed
    log.propagate = False

    fmt = logging.Formatter("%(asctime)s - %(message)s")

    q = queue.Queue(maxsize=10000)
    qh = QueueHandler(q)
    log.addHandler(qh)

    # Real sinks live on the listener thread:
    fh = RotatingFileHandler(file_name, maxBytes=5*1024*1024, backupCount=7)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout) # optionally keep stdout
    sh.setFormatter(fmt)

    listener = QueueListener(q, fh, sh, respect_handler_level=True)
    listener.start()

    log._listener = listener  # stash so you can stop it on shutdown if you want

    # Nest email_utils under us, so its records go through the queue too (SMTP diagnostics
    # never block the feed).
    email_utils.use_parent_logger(log)
    return log


# Init databento secrets
DB_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.DataBento.creds.json")
try:
    with open(DB_SECRET_FILE_PATH, "r") as f:
        db_secrets = json.load(f)
        db_API_KEY = db_secrets["api_key"]
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


# Init polygon secrets.
POLYGON_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.Polygon.creds.json")
try:
    with open(POLYGON_SECRET_FILE_PATH, "r") as f:
        polygon_secrets = json.load(f)
        polygon_API_KEY = polygon_secrets["api_key"]
    #logger.info("Databento API key loaded successfully.")
except FileNotFoundError:
    print(f"Credentials file not found at {POLYGON_SECRET_FILE_PATH} . Please create it.")
except KeyError:
    print(f"API key not found within {POLYGON_SECRET_FILE_PATH} . Ensure it contains 'api_key'.")
except Exception as e:
    print(f"Error loading credentials: {e}")


# PolygonFX is a pretty niche use, so don't fail if it doesn't exist. 
# Instead, just warn the user. 
POLYGONFX_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.PolygonFX.creds.json")
try:
    with open(POLYGONFX_SECRET_FILE_PATH, "r") as f:
        polygonfx_secrets = json.load(f)
        polygonfx_API_KEY = polygonfx_secrets["api_key"]
    #logger.info("Databento API key loaded successfully.")
except FileNotFoundError:
    print(f"Credentials file not found at {POLYGONFX_SECRET_FILE_PATH} . Please create it.")
except KeyError:
    print(f"API key not found within {POLYGONFX_SECRET_FILE_PATH} . Ensure it contains 'api_key'.")
except Exception as e:
    print(f"Error loading polygonfx credentials: {e}")


# Polygon FX topics require the event prefix (C.) plus the pair name (e.g. C:XAUUSD)
polygonfx_names_to_symbols = {
    "GOLD": "C.XAU/USD",
    "SILVER": "C.XAG/USD",
    "EUR": "C.EUR/USD",
    "JPY": "C.USD/JPY",
    "GBP": "C.GBP/USD"
}


# Reverse lookup so we can translate Polygon pairs back to internal names.
polygonfx_symbols_to_names = {
    "XAU/USD": "GOLD",
    "XAG/USD": "SILVER",
    "EUR/USD": "EUR",
    "USD/JPY": "JPY",
    "GBP/USD": "GBP"
}

# Pyth symbols->addresses
# https://insights.pyth.network/price-feeds
# For mapping pyth symbols->addresses
pyth_names_to_addresses = {
    "GOLD": "0x765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",
    "SILVER": "0xf2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e",
    "PLATINUM": "0x398e4bbc7cbf89d6648c21e08019d878967677753b3096799595c78f805a34e5",
    "PALLADIUM": "0x80367e9664197f37d89a07a804dffd2101c479c7c4e8490501bc9d9e1e7f9021",

    # I'm going to leave off the "USD" part for now.
    "EUR": "0xa995d00bb36a63cef7fd2c287dc105fc8f3d93779f062f09551b0af3e81ec30b",
    "JPY": "0xef2c98c804ba503c6a707e38be4dfbb16683775f195b091252bf24693042fd52",
    "GBP": "0x84c2dde9633d93d1bcad84e7dc41c9d56578b7ec52fabedc1f335d673df0a7c1"
}

# Makes the reverse map for when we get it back.
pyth_addresses_to_names = {}
for one_name, one_address in pyth_names_to_addresses.items():

    pyth_addresses_to_names[one_address] = one_name
    # Might also be named without the 0x.
    short_address = one_address[2:]
    pyth_addresses_to_names[short_address] = one_name

# Local-currency -> USD Pyth addresses for Refinitiv price conversion.
# IMPORTANT: these must be the "USD/XXX" feeds (local units per 1 USD, e.g.
# KRW ~1350, JPY ~150), because conversion divides: usd_px = local_px / rate.
# Do NOT use the "XXX/USD" majors from pyth_names_to_addresses (EUR/GBP), which
# are inverted (USD per unit) and would silently corrupt prices.
PYTH_FX_ADDRESSES = {
    "KRW": "0xe539120487c29b4defdf9a53d337316ea022a2688978a468f9efd847201be7e3",
    "HKD": "0x19d75fde7fee50fe67753fdc825e583594eb2f51ae84e114a5246c4ab23aff4c",
    "CNY": "0xeef52e09c878ad41f6a81803e3640fe04dceea727de894edd4ea117e2e332e66",
    # USD/JPY feed (~150 JPY per USD) — used for Tokyo (TSE/JPX) cash equities.
    "JPY": "0xef2c98c804ba503c6a707e38be4dfbb16683775f195b091252bf24693042fd52",
}
# Legacy alias
PYTH_KRW_USD_ADDRESS = PYTH_FX_ADDRESSES["KRW"]

# Refinitiv (LSEG) RTO credentials - non-fatal if missing since not all users need it.
REFINITIV_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.Refinitiv.creds.json")
REFINITIV_CLIENT_ID = None
REFINITIV_CLIENT_SECRET = None
try:
    with open(REFINITIV_SECRET_FILE_PATH, "r") as f:
        refinitiv_secrets = json.load(f)
        REFINITIV_CLIENT_ID = refinitiv_secrets["client_id"]
        REFINITIV_CLIENT_SECRET = refinitiv_secrets["client_secret"]
except FileNotFoundError:
    print(f"Credentials file not found at {REFINITIV_SECRET_FILE_PATH}. Refinitiv feed will not work.")
except KeyError:
    print(f"client_id/client_secret not found in {REFINITIV_SECRET_FILE_PATH}.")
except Exception as e:
    print(f"Error loading Refinitiv credentials: {e}")


class MultiFeed:
    def __init__(self, conf, creds_dir, date_str, end_secs, position=None, testing=False):
        self.creds_dir = creds_dir
        self.position = position
        self.testing = testing

        self.conf = conf
        self.date_str = date_str  # kept for the CME contract expansion on config reload

        self.logger = make_fast_logger("pymultifeed", "pymultifeed.{}.log".format(date_str))

        with open(self.conf) as f:
            subs_json = commentjson.loads(f.read())

        self.databento_heartbeat_timeout_secs = 60
        self.databento_heartbeat_check_secs = 60
        # Extend this so that we dont fool ourselves during pre- and post- market.
        self.polygon_heartbeat_timeout_secs = 300
        self.polygon_heartbeat_check_secs = max(
            1, min(10, self.polygon_heartbeat_timeout_secs / 2)
        )

        # Extend this so that we dont fool ourselves during pre- and post- market.
        self.polygonfx_heartbeat_timeout_secs =300
        self.polygonfx_heartbeat_check_secs =  max(
            1, min(10, self.polygonfx_heartbeat_timeout_secs / 2)
        )

        # We publish one-way to this socket. Setup and release will be done in run().
        self.pub_socket_uri = subs_json["pub_socket"]
        self.pub_ctx = None
        self.pub_socket = None

        # Filled once the asyncio loop is running; lets other threads schedule sends.
        self._loop = None

        self.end_secs = end_secs

        # OK, now go through and manage hyperliquids.

        self.hl_syms = []

        # HyperliquidNode (the n1 ZMQ feed) is configured as its own
        # top-level market section. When both Hyperliquid (WS) and
        # HyperliquidNode are present, init_hl_unified() runs them as
        # a single state machine with node primary + WS fallback. See
        # overmind/studies/hl_feed/pymultifeed_node_consumer_20260611.md.
        self.hl_node_syms = []
        self.hl_node_books_endpoint = None
        self.hl_node_fills_endpoint = None
        self.hl_node_fallback_silent_s = 5.0
        self.hl_node_fallback_lag_s = 5.0
        # Live state shared between the WS and ZMQ paths. Health-check
        # coroutine updates these; both source coroutines read.
        self.hl_node_last_msg_wall_ms = 0    # 0 == never received
        self.hl_node_last_event_time_ms = 0  # event_time from latest PbMessage
        self.hl_publishing_from_node = False  # set true once node is healthy
        # Sent at most once per run, the first time the node feed is
        # flagged unhealthy while we have a WS fallback available.
        self.hl_node_unhealthy_alert_sent = False

        # Monotonic-time gate per (sym, message-kind). Prevents either
        # source — typically WS during a fallback period — from publishing
        # a chain state older than the one local BookManagers have already
        # consumed. Checked AND updated on both paths.
        self.hl_last_book_event_time_by_sym = {}
        # Last Hyperliquid trade actually published per sym: (trade_time,
        # source) where source is "ws" or "node". Within a single
        # trade_time we trust one source — either both feeds see the same
        # on-chain set, or the primary already drained it. Cross-source
        # duplicates around a primary flip are skipped because the source
        # recorded on last_t doesn't match. Same-source distinct trades
        # at the same trade_time all publish because arrival order from a
        # single source is monotonic. Mirrors C++
        # HLNodeShared::last_published_trade_by_sym.
        self.hl_last_published_trade_by_sym = {}
        # Freshness markers: chain time of the latest trade seen on each
        # path. Diff (node - ws) measures which path is delivering trades
        # more current to chain time. Periodic logger emits the diff
        # every 3s from the hl_node_health watchdog loop.
        self.hl_last_node_trade_time_ms = 0
        self.hl_last_ws_trade_time_ms = 0
        # Book-snapshot freshness, same pattern. Node side is
        # already tracked above via self.hl_node_last_event_time_ms.
        self.hl_last_ws_book_event_time_ms = 0

        # If we want to subscribe to pyth.
        self.pyth_syms = []

        # Let's choose to just branch this
        self.polygon_eq_syms = []
        self.polygon_ws_client = None

        self.polygonfx_syms = []
        self.polygonfx_ws_client = None


        self.dbento_eq_syms = []
        self.dbento_cme_syms = []
        self.dbento_boats_syms = []

        # Refinitiv (LSEG) RTO feed for cash equities (KRX, KRX NXT, or US SIP)
        self.refinitiv_syms = {}           # name -> RIC (old format) or dict (new format)
        self.refinitiv_ric_to_pub_sym = {} # RIC -> base publish symbol (e.g. "005930.KS" -> "SAMSUNG")
        self.refinitiv_ric_calendar = {}   # RIC -> calendar_type string (e.g. "005930.KS" -> "krx_equities")
        self.refinitiv_service = "ELEKTRON_DD"
        self.refinitiv_session = "KRX"     # "KRX" or "US"
        self.refinitiv_last_activity = None
        self.refinitiv_ws = None
        self.refinitiv_auth_token = None
        self.refinitiv_token_expiry = 0
        self.refinitiv_heartbeat_timeout_secs = 60
        self.refinitiv_ric_convert_from = {}    # RIC -> currency string (e.g. "KRW"), or absent for no conversion
        self.refinitiv_fx_rates = {}            # currency -> latest rate (e.g. {"KRW": 0.00069})
        self.refinitiv_fx_last_update = {}      # currency -> timestamp of last FX rate update
        # Calendar -> timezone map for timestamp reconstruction
        self.refinitiv_calendar_tz = {
            "krx_equities": pytz.timezone('Asia/Seoul'),
            "krx_nxt_equities": pytz.timezone('Asia/Seoul'),
            "krx_futures": pytz.timezone('Asia/Seoul'),
            "jpx_equities": pytz.timezone('Asia/Tokyo'),        # .N225 index (continuous)
            "jpx_equities_stock": pytz.timezone('Asia/Tokyo'),  # TSE single stocks (am/pm lunch break)
            "us_equities": pytz.timezone('America/New_York'),
            # HKEX equities trade in HKT; used to reconstruct transact times.
            "hkex_equities": pytz.timezone('Asia/Hong_Kong'),
        }
        self.calendars_needed = set()
        self.currencies_needed = set()

        self.dbento_eq_last_activity = None
        self.dbento_cme_last_activity = None
        self.dbento_boats_last_activity = None
        self.polygon_last_activity = None
        self.polygonfx_last_activity = None

        # List of (start_ns, end_ns) tuples for valid session intervals (in ns)
        self.polygon_eq_valid_intervals = []
        self.polygonfx_valid_intervals = []
        self.db_eq_valid_intervals = []
        self.cme_valid_intervals = []
        # BOATS session: 20:00 ET Sun-Thu -> 04:00 ET next day. No session Fri/Sat nights.
        self.db_boats_valid_intervals = []
        self.refinitiv_intervals = {}      # {calendar_type: [(start_ns, end_ns), ...]}

        # Pointers to current "market open" index for fast lookups
        self.polygon_eq_interval_idx = 0
        self.polygonfx_interval_idx = 0
        self.db_eq_interval_idx = 0
        self.cme_interval_idx = 0
        self.db_boats_interval_idx = 0
        self.refinitiv_interval_idx = {}   # {calendar_type: int}

        # Queues for separating consumption from processing of websocket messages
        # Note: if queue hits maxsize, we restart the feed, so set carefully
        self.MAX_QUEUE_LEN = 10_000
        self.polygon_eq_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        self.polygonfx_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        self.dbento_eq_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        self.dbento_cme_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        self.dbento_boats_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        self.refinitiv_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)

        # If message delays exceed this, we restart the feed
        self.STALE_THRESHOLD_MS = 10 * 1000 # 10s
        self.PYTH_STALE_THRESHOLD_MS = 20 * 1000 # 20s bc Pyth is normally already close to 10s

        # Bad situation, but sometimes they can give us
        # a maybe_bad_book flag for extended periods of time.
        # In this case, we want to reenable
        # this after enough time has passed (1 minute),
        # otherwise we might just live too long without
        # having an accurate sense of the book.
        self.eq_bad_book_start = 0
        self.cme_bad_book_start = 0
        self.boats_bad_book_start = 0
        self.MAX_BAD_BOOK_T = 60

        # Throttling: only publish once every N milliseconds per symbol
        self.eq_last_publish_time = {}  # symbol -> timestamp in ms
        self.polygonfx_last_publish_time = {}  
        self.cme_last_publish_time = {}
        self.pyth_last_publish_time = {}
        self.refinitiv_last_publish_time = {}
        self.refinitiv_last_publish_cal = {}   # pub_sym -> calendar_type of last publish

        self.publish_interval_ms = 20  # milliseconds

        # Record last mkt time for each message - if we run redundant feeds and one goes down, 
        # we can use the other one to catch up. 
        self.sym_last_publish_transact_time = defaultdict(int)

        start_time = time.time()
        self.et_tz = pytz.timezone('America/New_York')

        # Event flag for graceful shutdown (thread-safe)
        self.eod_event = asyncio.Event()
        # Set once run() has finished tearing down. The shutdown watchdog thread
        # watches this to tell a clean (if slow) teardown from a hang.
        self._teardown_done = False

        self.current_date = datetime.datetime.fromtimestamp(start_time, tz=self.et_tz).date()
        self.end_date = datetime.datetime.fromtimestamp(end_secs, tz=self.et_tz).date()

        for one_market, market_dict in subs_json["subscriptions"].items():
            if one_market == "Hyperliquid":
                self.hl_syms = market_dict["symbols"]
                self.hl_ws_url = market_dict["wss_endpoint"]
            if one_market == "HyperliquidNode":
                self.hl_node_syms = market_dict["symbols"]
                self.hl_node_books_endpoint = market_dict["books_endpoint"]
                self.hl_node_fills_endpoint = market_dict["fills_endpoint"]
                self.hl_node_fallback_silent_s = float(
                    market_dict.get("fallback_silent_s", 5.0))
                self.hl_node_fallback_lag_s = float(
                    market_dict.get("fallback_lag_s", 5.0))
            if one_market == "Pyth":
                self.pyth_syms = market_dict["symbols"]
                self._calc_pyth_session()
            if one_market == "DataBentoEquities":
                self.dbento_eq_syms = market_dict["symbols"]
                self._calc_db_eq_session()
            if one_market == "DataBentoBoats":
                # Defaults to the same symbol list as DataBentoEquities if symbols absent;
                # otherwise allows the BOATS feed to subscribe to a different set (e.g.,
                # exclude symbols where BOATS overnight liquidity is too thin).
                # Relies on dict insertion order: DataBentoEquities must be parsed first
                # for the fallback to pick up the EQ symbol list.
                if "symbols" not in market_dict and not self.dbento_eq_syms:
                    self.logger.warning(
                        "DataBentoBoats omits 'symbols' and DataBentoEquities hasn't been "
                        "parsed yet; BOATS will subscribe to []. Either give DataBentoBoats "
                        "an explicit 'symbols' list or reorder the config so DataBentoEquities "
                        "comes first."
                    )
                self.dbento_boats_syms = market_dict.get("symbols", self.dbento_eq_syms)
                self._calc_db_boats_session()
            if one_market == "PolygonEquities":
                self.polygon_eq_syms = market_dict["symbols"]
                self._calc_polygon_eq_session()
            if one_market == "PolygonFX":
                self.polygonfx_syms = market_dict["symbols"]
                self._calc_polygonfx_session()
            if one_market == "DataBentoCME":
                self.dbento_cme_syms = []
                self.cme_blend_info = {}       # {base_sym: {"front": db_sym, "next": db_sym,
                                               #             "front_w": float, "next_w": float}}
                self.cme_contract_to_leg = {}  # {db_sym: "front"|"next"}
                self.cme_roll_latest = {}      # {base_sym: {"front": (bb_px, ba_px, bid_sz, ask_sz, ts),
                                               #             "next": (bb_px, ba_px, bid_sz, ask_sz, ts)}}

                for one_sym in market_dict["symbols"]:
                    self._add_cme_symbol(one_sym)

                self._calc_cme_session()

            if one_market == "Refinitiv":
                self.refinitiv_syms = market_dict.get("symbols", {})
                self.refinitiv_service = market_dict.get("service", "ELEKTRON_DD")
                self.refinitiv_session = market_dict.get("session", "KRX")
                # Top-level convert_from is fallback for old string format symbols.
                # Kept on self so the config-reload path can resolve added symbols.
                self.refinitiv_default_convert_from = market_dict.get("convert_from", None)

                for name, val in self.refinitiv_syms.items():
                    self._add_refinitiv_symbol(name, val, self.refinitiv_default_convert_from)

                self._calc_refinitiv_session()

        # --- Intraday config reload (add-only) ---
        # Re-read the conf every config_reload_interval_s; append any newly-listed
        # symbols to the relevant feed and signal it to reconnect (@retry), which
        # resubscribes with the updated symbol state. Uniform reconnect across
        # feeds for simplicity — pymultifeed is no longer our critical path.
        self.config_reload_interval_s = 600
        self.hl_reload_event = asyncio.Event()
        self.hl_node_reload_event = asyncio.Event()
        self.dbento_eq_reload_event = asyncio.Event()
        self.dbento_boats_reload_event = asyncio.Event()
        self.dbento_cme_reload_event = asyncio.Event()
        self.refinitiv_reload_event = asyncio.Event()
        # Conf-level symbol identifiers already handled, per market. Only markets
        # present (with symbols) at startup are reloaded — a feed not started
        # can't be subscribed to intraday, and a HyperliquidNode configured with
        # an empty (accept-all) filter is left alone rather than narrowed. For
        # Refinitiv the ids are the conf keys (name -> spec); for the others, the
        # symbol list itself.
        self._known_syms = {}
        for market, market_dict in subs_json["subscriptions"].items():
            if market in ("Hyperliquid", "HyperliquidNode", "DataBentoEquities",
                          "DataBentoBoats", "DataBentoCME", "Refinitiv"):
                ids = self._conf_sym_ids(market, market_dict.get("symbols"))
                if ids:
                    self._known_syms[market] = set(ids)

    @staticmethod
    def _conf_sym_ids(market, raw):
        """Conf-level identifiers for a market's 'symbols' entry: dict keys for
        Refinitiv (name -> spec), else the list itself. Empty if absent."""
        if raw is None:
            return []
        if isinstance(raw, dict):
            return list(raw.keys())
        return list(raw)

    def _add_cme_symbol(self, base_sym):
        """Expand a CME base symbol to databento contract(s) for self.date_str,
        register its roll-blend/leg state, and append to self.dbento_cme_syms.
        A symbol that can't be resolved (typo/unknown) is logged and skipped
        rather than raising, so an intraday add can't crash-loop the feed.
        Shared by __init__ and the config-reload path."""
        try:
            contracts = symbolizer.get_all_contracts_for_date(base_sym, self.date_str)
        except Exception as e:
            self.logger.error(f"CME: skipping symbol {base_sym} (contract resolution failed: {e})")
            return
        for db_sym, weight in contracts:
            self.dbento_cme_syms.append(db_sym)
            self.logger.info(f"Adding CME sym {db_sym} weight {weight}")
        if len(contracts) == 2:
            # Roll blend active for this symbol
            front_sym, front_w = contracts[0]
            next_sym, next_w = contracts[1]
            self.cme_blend_info[base_sym] = {
                "front": front_sym, "next": next_sym,
                "front_w": front_w, "next_w": next_w,
            }
            self.cme_contract_to_leg[front_sym] = "front"
            self.cme_contract_to_leg[next_sym] = "next"
            self.cme_roll_latest[base_sym] = {"front": None, "next": None}
            self.logger.info(f"Roll blend active for {base_sym}: {front_sym}({front_w}) + {next_sym}({next_w})")

    def _add_refinitiv_symbol(self, name, val, default_convert_from):
        """Register one Refinitiv symbol into the RIC/calendar/currency maps.
        Supports both the old string format ({"SAMSUNG": "005930.KS"}) and the
        new dict format ({"SMSN:krx": {"calendar_type": ..., "ric": ...,
        "convert_from": ...}}). Shared by __init__ and the config-reload path."""
        if isinstance(val, str):
            # Old string format — calendar inferred from session field
            ric = val
            pub_sym = name
            convert_from = default_convert_from
            if self.refinitiv_session == "KRX":
                cal = "krx_equities"
            elif self.refinitiv_session == "US":
                cal = "us_equities"
            else:
                cal = "krx_equities"
        else:
            # New dict format
            ric = val["ric"]
            cal = val["calendar_type"]
            pub_sym = name.split(":")[0]
            convert_from = val.get("convert_from", default_convert_from)

        self.refinitiv_ric_to_pub_sym[ric] = pub_sym
        self.refinitiv_ric_calendar[ric] = cal
        if convert_from:
            self.refinitiv_ric_convert_from[ric] = convert_from
            self.currencies_needed.add(convert_from)
        self.calendars_needed.add(cal)

    async def _wait_reload(self, event):
        """Wait for a config-reload signal for one feed, then raise
        ForceRestartFeed so the feed's @retry wrapper reconnects and resubscribes
        from the (already-updated) symbol state. Added to each feed's
        asyncio.wait set; a normal event.wait() would let the feed exit instead."""
        await event.wait()
        event.clear()
        raise ForceRestartFeed("config reload: new symbols added")

    async def config_reload_loop(self):
        """Periodically re-read the conf and add newly-listed symbols to the HL /
        DataBento / Refinitiv feeds (add-only), nudging each affected feed to
        reconnect. Tolerant of torn reads (parse failure -> skip, retry next
        cycle). Runs until EOD."""
        reloadable = ("Hyperliquid", "HyperliquidNode", "DataBentoEquities",
                      "DataBentoBoats", "DataBentoCME", "Refinitiv")
        try:
            last_mtime = os.path.getmtime(self.conf)
        except OSError:
            last_mtime = None
        while not self.eod_event.is_set():
            try:
                await asyncio.wait_for(self.eod_event.wait(),
                                       timeout=self.config_reload_interval_s)
                return  # EOD reached
            except asyncio.TimeoutError:
                pass
            try:
                mtime = os.path.getmtime(self.conf)
            except OSError as e:
                self.logger.error(f"Config reload: cannot stat {self.conf}: {e}")
                continue
            if last_mtime is not None and mtime == last_mtime:
                continue
            try:
                with open(self.conf) as f:
                    subs = commentjson.loads(f.read())["subscriptions"]
            except Exception as e:
                # Likely caught mid-write; retry next cycle without advancing mtime.
                self.logger.warning(f"Config reload: parse failed, skipping this cycle: {e}")
                continue
            self.logger.info(f"Config reload: {self.conf} changed, checking for new symbols")
            try:
                for market in reloadable:
                    if market not in subs or market not in self._known_syms:
                        continue
                    md = subs[market]
                    ids = self._conf_sym_ids(market, md.get("symbols"))
                    known = self._known_syms[market]
                    new_ids = [x for x in ids if x not in known]
                    if not new_ids:
                        continue
                    for x in new_ids:
                        known.add(x)
                    self.logger.info(f"Config reload: {len(new_ids)} new {market} "
                                     f"symbol(s): {new_ids}")
                    self._apply_reload_market(market, md, new_ids)
            except Exception as e:
                self.logger.error(f"Config reload: apply failed: {e}")
                self.logger.error(traceback.format_exc())
            last_mtime = mtime

    def _apply_reload_market(self, market, md, new_ids):
        """Apply newly-added symbols for one market and signal its feed to
        reconnect. Sync (no awaits) so it can't interleave with the feeds reading
        this state. Lists are rebound (not mutated in place) so an aliased list
        — e.g. DataBentoBoats defaulting to the EQ list — isn't affected."""
        if market == "Hyperliquid":
            self.hl_syms = self.hl_syms + new_ids
            self.hl_reload_event.set()
        elif market == "HyperliquidNode":
            # Only reached when the node was started with a non-empty filter (an
            # empty accept-all filter isn't seeded into _known_syms), so growing
            # the list can't accidentally narrow an accept-all node.
            self.hl_node_syms = self.hl_node_syms + new_ids
            self.hl_node_reload_event.set()
        elif market == "DataBentoEquities":
            self.dbento_eq_syms = self.dbento_eq_syms + new_ids
            self.dbento_eq_reload_event.set()
        elif market == "DataBentoBoats":
            self.dbento_boats_syms = self.dbento_boats_syms + new_ids
            self.dbento_boats_reload_event.set()
        elif market == "DataBentoCME":
            for base_sym in new_ids:
                self._add_cme_symbol(base_sym)
            self.dbento_cme_reload_event.set()
        elif market == "Refinitiv":
            syms = md.get("symbols", {})
            cals_before = set(self.refinitiv_intervals.keys())
            for name in new_ids:
                val = syms[name]
                self.refinitiv_syms[name] = val
                self._add_refinitiv_symbol(name, val, self.refinitiv_default_convert_from)
            # A newly-introduced calendar needs a full interval recompute — the
            # calc early-returns once refinitiv_intervals is populated.
            if any(cal not in cals_before for cal in self.calendars_needed):
                self.refinitiv_intervals = {}
                self.refinitiv_interval_idx = {}
                self._calc_refinitiv_session()
            self.refinitiv_reload_event.set()

    def _calc_pyth_session(self):
        # Calc PolygonFX and CME sessions bc Pyth reuses those
        self._calc_polygonfx_session()
        self._calc_cme_session()

    def _calc_polygon_eq_session(self):
        if self.polygon_eq_valid_intervals:
            # Already calculated
            return

        date = self.current_date
        while date <= self.end_date:
            # Skip weekends
            if date.weekday() < 5:  # Monday=0, Friday=4
                # Polygon gives us the pre- and post-
                # market data too, so I want us to
                # consume things like this.

                # Reverting to polygon giving us everything. 
                market_open = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(4, 00)))
                market_close = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(20, 00)))
                start_ns = int(market_open.timestamp() * 1_000_000_000)
                end_ns = int(market_close.timestamp() * 1_000_000_000)
                self.polygon_eq_valid_intervals.append((start_ns, end_ns))
                self.logger.info(f"Polygon US Equity session: {market_open} to {market_close}")


                #market_open_am = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(4, 00)))
                #market_close_am = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(9, 30)))
                #market_open_pm = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(16, 0)))
                #market_close_pm = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(20, 0)))
                #start_ns_am = int(market_open_am.timestamp() * 1_000_000_000)
                #end_ns_am = int(market_close_am.timestamp() * 1_000_000_000)
                #self.polygon_eq_valid_intervals.append((start_ns_am, end_ns_am))
                #self.logger.info(f"Polygon US Equity session (AM): {market_open_am} to {market_close_am}")
                #start_ns_pm = int(market_open_pm.timestamp() * 1_000_000_000)
                #end_ns_pm = int(market_close_pm.timestamp() * 1_000_000_000)
                #self.polygon_eq_valid_intervals.append((start_ns_pm, end_ns_pm))
                #self.logger.info(f"Polygon US Equity session (PM): {market_open_pm} to {market_close_pm}")
            date += datetime.timedelta(days=1)

    def _calc_polygonfx_session(self):
        if self.polygonfx_valid_intervals:
            # Already calculated
            return

        date = self.current_date
        while date <= self.end_date:

            # They might actually have a 17:00-18:00 data for FX, but let's check this later. 
            if date.weekday() == 6:  # Sunday
                session_start = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(17, 0)))
                session_end = self.et_tz.localize(datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(0, 0)))
                self.polygonfx_valid_intervals.append((int(session_start.timestamp() * 1_000_000_000),
                                                int(session_end.timestamp() * 1_000_000_000)))
            elif date.weekday() < 4:  # Monday=0, Friday=4
                # Data I think is probably similar to the CME hours, but no hour break. 
                # Should verify this. 
                market_open = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(0, 00)))
                market_close = self.et_tz.localize(datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(0, 0)))
                start_ns = int(market_open.timestamp() * 1_000_000_000)
                end_ns = int(market_close.timestamp() * 1_000_000_000)
                self.polygonfx_valid_intervals.append((start_ns, end_ns))
                self.logger.info(f"PolygonFX session : {market_open} to {market_close}")
            if date.weekday() == 4:  # Friday
                session_start = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(00, 0)))
                session_end = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(17, 0)))
                self.polygonfx_valid_intervals.append((int(session_start.timestamp() * 1_000_000_000),
                                                int(session_end.timestamp() * 1_000_000_000)))

            date += datetime.timedelta(days=1)

    def _calc_db_eq_session(self):
        if self.db_eq_valid_intervals:
            # Already calculated
            return

        date = self.current_date
        while date <= self.end_date:
            # Skip weekends
            if date.weekday() < 5:  # Monday=0, Friday=4
                market_open = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(4, 00)))
                market_close = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(20, 0)))
                start_ns = int(market_open.timestamp() * 1_000_000_000)
                end_ns = int(market_close.timestamp() * 1_000_000_000)
                self.db_eq_valid_intervals.append((start_ns, end_ns))
                self.logger.info(f"US Equity session: {market_open} to {market_close}")
            date += datetime.timedelta(days=1)

    def _calc_db_boats_session(self):
        """Blue Ocean ATS overnight session: 20:00 ET -> 04:00 ET next day, Sun-Thu nights.

        Emits one interval per session: starts Sun 20:00 ET (first), then each weekday
        Mon-Thu 20:00 ET. Fri 20:00 ET and Sat 20:00 ET are skipped (no session).
        """
        if self.db_boats_valid_intervals:
            return

        # We may need an interval that started the night before self.current_date if
        # we boot mid-session, so step back one day from current_date as the loop start.
        date = self.current_date - datetime.timedelta(days=1)
        # Each session crosses midnight, so close on (end_date + 1) to capture the
        # session that opens on end_date evening.
        while date <= self.end_date:
            # Sun=6, Mon=0, Tue=1, Wed=2, Thu=3 are session-opening evenings.
            # Fri=4 and Sat=5 -> no BOATS session that night.
            if date.weekday() in (6, 0, 1, 2, 3):
                session_open = self.et_tz.localize(
                    datetime.datetime.combine(date, datetime.time(20, 0)))
                session_close = self.et_tz.localize(
                    datetime.datetime.combine(date + datetime.timedelta(days=1),
                                              datetime.time(4, 0)))
                start_ns = int(session_open.timestamp() * 1_000_000_000)
                end_ns = int(session_close.timestamp() * 1_000_000_000)
                self.db_boats_valid_intervals.append((start_ns, end_ns))
                self.logger.info(f"BOATS session: {session_open} to {session_close}")
            date += datetime.timedelta(days=1)

    def _calc_cme_session(self):
        if self.cme_valid_intervals:
            # Already calculated
            return

        # Precompute CME open hours: 24/5 excluding 5-6 PM ET daily maintenance
        # and Friday 5 PM ET - Sunday 6 PM ET weekend.
        date = self.current_date
        while date <= self.end_date + datetime.timedelta(days=1):
            # CME opens Sunday 6 PM ET, closes Friday 5 PM ET
            # Each day has: midnight to 5 PM, then 6 PM to midnight
            # Except: Saturday 6 PM - Sunday 6 PM is closed

            if date.weekday() == 6:  # Sunday
                # Sunday: 6 PM onwards is open
                session_start = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(18, 0)))
                session_end = self.et_tz.localize(datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(0, 0)))
                self.cme_valid_intervals.append((int(session_start.timestamp() * 1_000_000_000),
                                                int(session_end.timestamp() * 1_000_000_000)))
            elif date.weekday() == 5:  # Saturday - closed all day
                pass
            elif date.weekday() == 4:
                # Fridays, Only do midnight to 5PM
                session_start = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(0, 0)))
                friday_end = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(17, 0)))
                self.cme_valid_intervals.append((int(session_start.timestamp() * 1_000_000_000),
                                                int(friday_end.timestamp() * 1_000_000_000)))
            else:  # Monday-Thursdays
                # Midnight to 5 PM
                session_start = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(0, 0)))
                maint_start = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(17, 0)))
                self.cme_valid_intervals.append((int(session_start.timestamp() * 1_000_000_000),
                                                int(maint_start.timestamp() * 1_000_000_000)))
                # 6 PM to midnight
                maint_end = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(18, 0)))
                day_end = self.et_tz.localize(datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(0, 0)))
                self.cme_valid_intervals.append((int(maint_end.timestamp() * 1_000_000_000),
                                                int(day_end.timestamp() * 1_000_000_000)))
            date += datetime.timedelta(days=1)

        self.logger.info(f"CME: Generated {len(self.cme_valid_intervals)} session intervals")

    def _calc_refinitiv_session(self):
        if self.refinitiv_intervals:
            # Already calculated
            return

        # Generate session intervals for each calendar type needed
        kst_tz = pytz.timezone('Asia/Seoul')

        if "krx_equities" in self.calendars_needed:
            # KRX session: 9:00 AM - 3:30 PM KST, weekdays only
            self.refinitiv_intervals["krx_equities"] = []
            date = self.current_date
            while date <= self.end_date:
                if date.weekday() < 5:
                    market_open = kst_tz.localize(datetime.datetime.combine(date, datetime.time(9, 0)))
                    market_close = kst_tz.localize(datetime.datetime.combine(date, datetime.time(15, 30)))
                    start_ns = int(market_open.timestamp() * 1_000_000_000)
                    end_ns = int(market_close.timestamp() * 1_000_000_000)
                    self.refinitiv_intervals["krx_equities"].append((start_ns, end_ns))
                    self.logger.info(f"KRX session: {market_open} to {market_close}")
                date += datetime.timedelta(days=1)
            self.refinitiv_interval_idx["krx_equities"] = 0

        if "krx_nxt_equities" in self.calendars_needed:
            # KRX NXT (extended hours): 8:00 AM - 8:00 PM KST, weekdays only
            self.refinitiv_intervals["krx_nxt_equities"] = []
            date = self.current_date
            while date <= self.end_date:
                if date.weekday() < 5:
                    market_open = kst_tz.localize(datetime.datetime.combine(date, datetime.time(8, 0)))
                    market_close = kst_tz.localize(datetime.datetime.combine(date, datetime.time(20, 0)))
                    start_ns = int(market_open.timestamp() * 1_000_000_000)
                    end_ns = int(market_close.timestamp() * 1_000_000_000)
                    self.refinitiv_intervals["krx_nxt_equities"].append((start_ns, end_ns))
                    self.logger.info(f"KRX NXT session: {market_open} to {market_close}")
                date += datetime.timedelta(days=1)
            self.refinitiv_interval_idx["krx_nxt_equities"] = 0

        if "krx_futures" in self.calendars_needed:
            # KRX futures: two sessions per day
            #   Regular: 9:00 - 15:45 KST
            #   Night:  18:00 - 05:00 KST (next day)
            self.refinitiv_intervals["krx_futures"] = []
            date = self.current_date
            while date <= self.end_date:
                if date.weekday() < 5:
                    # Regular session
                    reg_open = kst_tz.localize(datetime.datetime.combine(date, datetime.time(9, 0)))
                    reg_close = kst_tz.localize(datetime.datetime.combine(date, datetime.time(15, 45)))
                    self.refinitiv_intervals["krx_futures"].append(
                        (int(reg_open.timestamp() * 1_000_000_000), int(reg_close.timestamp() * 1_000_000_000)))
                    # Night session (18:00 today to 05:00 next day)
                    night_open = kst_tz.localize(datetime.datetime.combine(date, datetime.time(18, 0)))
                    night_close = kst_tz.localize(datetime.datetime.combine(
                        date + datetime.timedelta(days=1), datetime.time(5, 0)))
                    self.refinitiv_intervals["krx_futures"].append(
                        (int(night_open.timestamp() * 1_000_000_000), int(night_close.timestamp() * 1_000_000_000)))
                    self.logger.info(f"KRX futures session: {reg_open} to {reg_close}, {night_open} to {night_close}")
                date += datetime.timedelta(days=1)
            self.refinitiv_interval_idx["krx_futures"] = 0

        if "jpx_equities" in self.calendars_needed:
            # JPX *index* session (e.g. .N225): continuous 09:00-15:30 JST,
            # weekdays only. NO lunch break — the JP225 allday trader relies on
            # this reference straight through the 11:30-12:30 TSE lunch (the cash
            # index is frozen then, but we keep publishing the last value so the
            # signal stays alive). Single TSE *stocks* use jpx_equities_stock
            # below, which DOES split out the lunch break. Keep these separate so
            # adding stocks never changes the index/JP225 trader behavior.
            jst_tz = pytz.timezone('Asia/Tokyo')
            self.refinitiv_intervals["jpx_equities"] = []
            date = self.current_date
            while date <= self.end_date:
                if date.weekday() < 5:
                    market_open = jst_tz.localize(datetime.datetime.combine(date, datetime.time(9, 0)))
                    market_close = jst_tz.localize(datetime.datetime.combine(date, datetime.time(15, 30)))
                    start_ns = int(market_open.timestamp() * 1_000_000_000)
                    end_ns = int(market_close.timestamp() * 1_000_000_000)
                    self.refinitiv_intervals["jpx_equities"].append((start_ns, end_ns))
                    self.logger.info(f"JPX index session: {market_open} to {market_close}")
                date += datetime.timedelta(days=1)
            self.refinitiv_interval_idx["jpx_equities"] = 0

        if "jpx_equities_stock" in self.calendars_needed:
            # TSE single-stock cash equities (e.g. 9984.T, 285A.T) have a midday
            # break, so each weekday is TWO sessions (zenba / goba):
            #   Morning (前場):   09:00 - 11:30 JST
            #   Afternoon (後場): 12:30 - 15:30 JST  (close extended to 15:30 on 2024-11-05)
            #
            # DST NOTE: pymultifeed builds these feed-acceptance windows in
            # Asia/Tokyo, so this calendar itself is not affected by US DST.
            # The live trader configs still express start_t/end_t in
            # America/New_York because trade_main.cc has an 18:00 ET day-roll
            # assumption. Those NY times are DST-sensitive; update/regenerate
            # the live configs when US DST shifts, or move the trader side to a
            # JST-native session representation.
            # Two intervals/day (like hkex_equities) so the 11:30-12:30 lunch is
            # out-of-session and we don't publish stale/withdrawn stock quotes
            # across it. Kept SEPARATE from jpx_equities (the continuous .N225
            # index calendar) so this does not affect the JP225 allday trader.
            jst_tz = pytz.timezone('Asia/Tokyo')
            self.refinitiv_intervals["jpx_equities_stock"] = []
            date = self.current_date
            while date <= self.end_date:
                if date.weekday() < 5:
                    # Morning session
                    am_open = jst_tz.localize(datetime.datetime.combine(date, datetime.time(9, 0)))
                    am_close = jst_tz.localize(datetime.datetime.combine(date, datetime.time(11, 30)))
                    self.refinitiv_intervals["jpx_equities_stock"].append(
                        (int(am_open.timestamp() * 1_000_000_000), int(am_close.timestamp() * 1_000_000_000)))
                    # Afternoon session
                    pm_open = jst_tz.localize(datetime.datetime.combine(date, datetime.time(12, 30)))
                    pm_close = jst_tz.localize(datetime.datetime.combine(date, datetime.time(15, 30)))
                    self.refinitiv_intervals["jpx_equities_stock"].append(
                        (int(pm_open.timestamp() * 1_000_000_000), int(pm_close.timestamp() * 1_000_000_000)))
                    self.logger.info(f"JPX stock session: {am_open} to {am_close}, {pm_open} to {pm_close}")
                date += datetime.timedelta(days=1)
            self.refinitiv_interval_idx["jpx_equities_stock"] = 0

        if "us_equities" in self.calendars_needed:
            # US equity session: 4:00 AM - 8:00 PM ET, weekdays only
            self.refinitiv_intervals["us_equities"] = []
            date = self.current_date
            while date <= self.end_date:
                if date.weekday() < 5:
                    market_open = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(4, 0)))
                    market_close = self.et_tz.localize(datetime.datetime.combine(date, datetime.time(20, 0)))
                    start_ns = int(market_open.timestamp() * 1_000_000_000)
                    end_ns = int(market_close.timestamp() * 1_000_000_000)
                    self.refinitiv_intervals["us_equities"].append((start_ns, end_ns))
                    self.logger.info(f"US equity session: {market_open} to {market_close}")
                date += datetime.timedelta(days=1)
            self.refinitiv_interval_idx["us_equities"] = 0

        if "hkex_equities" in self.calendars_needed:
            # HKEX main board has a midday break, so each weekday is TWO sessions:
            #   Morning:   09:30 - 12:00 HKT
            #   Afternoon: 13:00 - 16:00 HKT
            # We append two intervals per day (like krx_futures above) so the
            # 12:00-13:00 lunch break is correctly treated as out-of-session.
            hkt_tz = pytz.timezone('Asia/Hong_Kong')
            self.refinitiv_intervals["hkex_equities"] = []
            date = self.current_date
            while date <= self.end_date:
                if date.weekday() < 5:
                    # Morning session
                    am_open = hkt_tz.localize(datetime.datetime.combine(date, datetime.time(9, 30)))
                    am_close = hkt_tz.localize(datetime.datetime.combine(date, datetime.time(12, 0)))
                    self.refinitiv_intervals["hkex_equities"].append(
                        (int(am_open.timestamp() * 1_000_000_000), int(am_close.timestamp() * 1_000_000_000)))
                    # Afternoon session
                    pm_open = hkt_tz.localize(datetime.datetime.combine(date, datetime.time(13, 0)))
                    pm_close = hkt_tz.localize(datetime.datetime.combine(date, datetime.time(16, 0)))
                    self.refinitiv_intervals["hkex_equities"].append(
                        (int(pm_open.timestamp() * 1_000_000_000), int(pm_close.timestamp() * 1_000_000_000)))
                    self.logger.info(f"HKEX session: {am_open} to {am_close}, {pm_open} to {pm_close}")
                date += datetime.timedelta(days=1)
            self.refinitiv_interval_idx["hkex_equities"] = 0

        if not self.calendars_needed:
            self.logger.error(f"No calendars resolved for Refinitiv symbols")

        total_intervals = sum(len(v) for v in self.refinitiv_intervals.values())
        self.logger.info(f"Refinitiv: {len(self.refinitiv_syms)} symbols, "
                         f"calendars={list(self.calendars_needed)}, "
                         f"{total_intervals} total session intervals, "
                         f"fx_currencies={list(self.currencies_needed) if self.currencies_needed else 'none'}")

    def _is_in_polygon_eq_session(self, ts_event_ns):
        # Check if timestamp is in US equity session. Uses pointer to current interval.

        if self.polygon_eq_interval_idx < len(self.polygon_eq_valid_intervals):
            start_ns, end_ns = self.polygon_eq_valid_intervals[self.polygon_eq_interval_idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            if ts_event_ns > end_ns:
                self.polygon_eq_interval_idx += 1
                return self._is_in_polygon_eq_session(ts_event_ns)
        return False


    def _is_in_db_eq_session(self, ts_event_ns):
        # Check if timestamp is in US equity session. Uses pointer to current interval.

        if self.db_eq_interval_idx < len(self.db_eq_valid_intervals):
            start_ns, end_ns = self.db_eq_valid_intervals[self.db_eq_interval_idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            if ts_event_ns > end_ns:
                self.db_eq_interval_idx += 1
                return self._is_in_db_eq_session(ts_event_ns)
        return False

    def _is_in_db_boats_session(self, ts_event_ns):
        # Check if timestamp is in a BOATS overnight session.
        if self.db_boats_interval_idx < len(self.db_boats_valid_intervals):
            start_ns, end_ns = self.db_boats_valid_intervals[self.db_boats_interval_idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            if ts_event_ns > end_ns:
                self.db_boats_interval_idx += 1
                return self._is_in_db_boats_session(ts_event_ns)
        return False

    def _is_in_polygonfx_session(self, ts_event_ns):
        # Check if timestamp is in fx session. Uses pointer to current interval.

        if self.polygonfx_interval_idx < len(self.polygonfx_valid_intervals):
            start_ns, end_ns = self.polygonfx_valid_intervals[self.polygonfx_interval_idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            if ts_event_ns > end_ns:
                self.polygonfx_interval_idx += 1
                return self._is_in_polygonfx_session(ts_event_ns)
        return False

    def _is_in_cme_session(self, ts_event_ns):
        # Check if timestamp is in CME session. Uses pointer to current interval.

        if self.cme_interval_idx < len(self.cme_valid_intervals):
            start_ns, end_ns = self.cme_valid_intervals[self.cme_interval_idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            if ts_event_ns > end_ns:
                self.cme_interval_idx += 1
                return self._is_in_cme_session(ts_event_ns)
        return False

    def _is_pyth_fx(self, sym):
        return sym in ["EUR", "JPY", "GBP"]

    def _is_pyth_metal(self, sym):
        return sym in ["GOLD", "SILVER", "PLATINUM", "PALLADIUM"]

    def _is_in_pyth_session(self, ts_event_ns, sym):
        if self._is_pyth_fx(sym):
            return self._is_in_polygonfx_session(ts_event_ns)
        elif self._is_pyth_metal(sym):
            return self._is_in_cme_session(ts_event_ns)
        else:
            raise RuntimeError(f"Unrecognized Pyth symbol: {sym}")

    def _is_in_refinitiv_session(self, calendar_type, ts_event_ns):
        # Check if timestamp is in a specific Refinitiv calendar session.
        intervals = self.refinitiv_intervals.get(calendar_type, [])
        idx = self.refinitiv_interval_idx.get(calendar_type, 0)
        if idx < len(intervals):
            start_ns, end_ns = intervals[idx]
            if start_ns <= ts_event_ns <= end_ns:
                return True
            if ts_event_ns > end_ns:
                self.refinitiv_interval_idx[calendar_type] += 1
                return self._is_in_refinitiv_session(calendar_type, ts_event_ns)
        return False

    def _is_any_refinitiv_session_active(self, ts_event_ns):
        # Returns True if ANY Refinitiv calendar is currently in session.
        return any(self._is_in_refinitiv_session(cal, ts_event_ns)
                   for cal in self.refinitiv_intervals)

    def _resolve_refinitiv_transact_time(self, calendar_type, fields, fallback_ms):
        """Rebuild an exchange timestamp from Refinitiv fields if possible."""
        trade_time = fields.get("TIMACT") or fields.get("TRDTIM_1")
        trade_date = fields.get("TDAY") or fields.get("LONGLASTTRADEDATE")
        if not trade_time:
            return fallback_ms

        tz = self.refinitiv_calendar_tz.get(calendar_type)
        if tz is None:
            return fallback_ms

        try:
            time_str = str(trade_time).strip()
            if not time_str:
                return fallback_ms

            if ":" in time_str:
                hh_str, mm_str, ss_str = (time_str.split(":") + ["0", "0"])[:3]
            else:
                if len(time_str) < 6:
                    return fallback_ms
                hh_str, mm_str, ss_str = time_str[:2], time_str[2:4], time_str[4:]

            hour = int(hh_str)
            minute = int(mm_str)
            if "." in ss_str:
                sec_part, frac_part = ss_str.split(".", 1)
            else:
                sec_part, frac_part = ss_str, ""
            second = int(sec_part) if sec_part else 0
            microsecond = int((frac_part + "000000")[:6]) if frac_part else 0

            if trade_date:
                date_str = str(trade_date).strip()
                if len(date_str) == 8 and date_str.isdigit():
                    year = int(date_str[0:4])
                    month = int(date_str[4:6])
                    day = int(date_str[6:8])
                else:
                    today = datetime.datetime.now(tz).date()
                    year, month, day = today.year, today.month, today.day
            else:
                today = datetime.datetime.now(tz).date()
                year, month, day = today.year, today.month, today.day

            dt = tz.localize(datetime.datetime(year, month, day, hour, minute, second, microsecond))
            return int(dt.timestamp() * 1000)
        except Exception as exc:
            self.logger.debug(f"Failed to parse Refinitiv timestamp: {exc}")
            return fallback_ms

    def _setup_pub_socket(self):
        if self.pub_ctx or self.pub_socket:
            raise RuntimeError("_setup_pub_socket should only ever be called once")
        # For an ipc:// endpoint, a prior process that didn't exit cleanly (crash,
        # kill -9, or the shutdown watchdog's os._exit firing before the socket was
        # released) leaves the socket file behind, and bind() then fails with
        # EADDRINUSE on the stale file. Remove it first, mirroring pkmultifeed.
        # (A clean shutdown unlinks it via close(), so this is only for the
        # unclean case.)
        if self.pub_socket_uri.startswith("ipc://"):
            sock_path = self.pub_socket_uri[len("ipc://"):]
            try:
                os.remove(sock_path)
                self.logger.warning(f"Removed stale PUB socket file {sock_path}")
            except FileNotFoundError:
                pass
        self.pub_ctx = zmq.asyncio.Context()
        self.pub_socket = self.pub_ctx.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 100)
        self.pub_socket.bind(self.pub_socket_uri)
        self.logger.info("Bound to {}".format(self.pub_socket_uri))

    def _shutdown_watchdog(self):
        """Hard backstop against a wedged shutdown. Runs in a daemon thread, so it
        keeps ticking even if the event loop is stuck. Mirrors pkmultifeed's
        shutdown watchdog: if teardown doesn't finish within the deadline after
        EOD/shutdown begins, force-exit rather than sit hung for days (as a
        pkmultifeed instance did on 20260717). run()'s cooperative timeouts don't
        cover an unbounded await in run() itself (e.g. asyncio.wait(pending) or
        pub_ctx.term())."""
        WATCHDOG_SECS = 10
        # eod_event.is_set() is a plain-bool read, safe to poll from this thread.
        while not self.eod_event.is_set():
            time.sleep(1)
        deadline = time.monotonic() + WATCHDOG_SECS
        while not self._teardown_done:
            if time.monotonic() > deadline:
                try:
                    msg = (f"Shutdown watchdog: teardown exceeded {WATCHDOG_SECS}s, "
                           f"forcing os._exit(0) to avoid a hung process")
                    self.logger.error(msg)
                    # Don't bother flushing the log, which could introduce a block.
                    # send_alerts sends blocking here (no event loop in this thread),
                    # so it completes before os._exit rather than being killed.
                    email_utils.send_alerts("PYMULTIFEED SHUTDOWN HANG", msg)
                finally:
                    os._exit(0)
            time.sleep(1)

    def _release_pub_socket(self):
        if self.pub_socket:
            self.logger.info("Closing PUB socket")
            self.pub_socket.close(linger=0)
            self.pub_socket = None
        if self.pub_ctx:
            self.pub_ctx.term()
            self.pub_ctx = None

    async def publish_async(self, payload: bytes):
        """Send over the PUB socket from the asyncio event loop."""
        if self._loop is None:
            raise RuntimeError("publish_async called before event loop captured")
        if self.pub_socket:
            await self.pub_socket.send(payload)

    def publish_threadsafe(self, payload: bytes):
        """Schedule a PUB send from threads that aren't on the asyncio loop."""
        if self._loop is None:
            self.logger.error("publish_threadsafe called before event loop captured")
            return

        future = asyncio.run_coroutine_threadsafe(self.publish_async(payload), self._loop)

        def _on_done(done_future):
            try:
                done_future.result()
            except CancelledError:
                self.logger.warning("Publish coroutine was cancelled")
            except Exception as exc:
                self.logger.error(f"Failed to publish message: {exc}")

        future.add_done_callback(_on_done)

    async def _cancel_pending(self, pending, feed_name):
        """Cancel all `pending` tasks and wait up to 3s for them to finish, then
        raise TimeoutError if any are still stuck so the feed's @retry restarts.
        Keeps one wedged task from blocking a reconnect, or the EOD shutdown
        (which only has a few seconds to release the ZMQ sockets for the next
        day's process).

        Why asyncio.wait and not wait_for(gather(...)): wait_for's timeout path
        must guarantee the inner future isn't still running when it returns, so
        it cancels the gather and *waits* for it. If a task swallows that cancel
        too, it blocks forever with no timeout left — the very hang we're
        guarding against. asyncio.wait never cancels on timeout, so it's a hard
        bound, and the still-pending tasks still have usable stacks to log.

        Background: on Python 3.10 asyncio.wait_for silently drops a
        CancelledError when the awaited future has already completed
        (`if fut.done(): return fut.result()`), which wedges a recv loop on a busy
        stream — see the 20260715 Refinitiv FX incident. Fixed in 3.12; until we
        upgrade, this is the backstop."""
        if not pending:
            return
        for task in pending:
            task.cancel()
        _, still_pending = await asyncio.wait(pending, timeout=3)
        if still_pending:
            err_msg = (f"{feed_name} task cancellations timed out"
                       f" (retrying loop but could have stuck tasks):")
            for t in still_pending:
                # Read the stack before re-cancelling; a finished task's is empty.
                err_msg += f"\n  task={t} stack={[f.f_code.co_name for f in t.get_stack()]}"
                # Best-effort second cancel — one often lands cleanly and unwedges
                # the task. Deliberately not awaited, so it can't hang us.
                t.cancel()
            self.logger.error(err_msg)
            email_utils.send_alerts(f"{feed_name.upper()} CANCELS TIMED OUT", err_msg)
            raise asyncio.TimeoutError(f"{feed_name} task cancellations timed out")

    async def _monitor_feed_heartbeat(
        self,
        feed_name: str,
        last_activity_attr: str,
        timeout: float,
        check_interval: float,
        session_guard: Optional[Callable[[], bool]] = None,
        subscription_check_interval = None,
        is_subscribed: Optional[Callable[[], bool]] = None,
        alert: bool = False
    ):
        """Watch for feeds that stop delivering data and raise if stalled."""

        self.logger.info(
            "%s heartbeat monitor active (timeout=%ss, check=%ss)",
            feed_name,
            timeout,
            check_interval,
        )

        try:
            last_subscription_check_t = time.time()
            while not self.eod_event.is_set():
                await asyncio.sleep(check_interval)

                if subscription_check_interval:
                    # If requested, check if we're subscribed, but not every iteration
                    now_t = time.time()
                    if now_t - last_subscription_check_t > subscription_check_interval:
                        last_subscription_check_t = now_t
                        if not is_subscribed():
                            raise HeartbeatNotSubscribed(f"{feed_name} not subscribed")
                        else:
                            # Assume once we're subscribed, we'll stay subscribed
                            subscription_check_interval = None

                if session_guard is not None:
                    try:
                        in_session = session_guard()
                    except Exception as exc:
                        self.logger.error(
                            "%s session guard failed: %s", feed_name, exc
                        )
                        in_session = True

                    if not in_session:
                        setattr(self, last_activity_attr, None)
                        continue

                last_ts = getattr(self, last_activity_attr, None)
                if last_ts is None:
                    setattr(self, last_activity_attr, time.time())
                    continue

                silence = time.time() - last_ts
                if silence > timeout:
                    self.logger.error(
                        "%s feed silent for %.1fs (threshold=%ss)",
                        feed_name,
                        silence,
                        timeout,
                    )
                    if alert:
                        email_utils.send_alerts(
                            f"{feed_name.upper()} FEED DOWN",
                            f"{feed_name} feed silent for {silence:.1f}s "
                            f"(threshold={timeout}s), forcing reconnect.",
                        )
                    raise HeartbeatTimeout(
                        f"{feed_name} heartbeat stalled after {silence:.1f}s"
                    )
        except asyncio.CancelledError:
            self.logger.info(f"{feed_name} heartbeat cancelled")
            raise

    async def _shutdown_databento_client(self, client, name):
        if client is None:
            return
        try:
            client.stop()
            self.logger.info(f"Called stop on {name} client during shutdown")
            # If the clean stop doesn't complete in 1s, hard terminate() is automatically called.
            await client.wait_for_close(timeout=1)
            self.logger.info(f"{name} client closed")
        except Exception as exc:
            self.logger.error(f"Failed to stop {name} client: {exc}")

    async def _shutdown_polygon_client(self, client):
        if client is None:
            return
        try:
            client.unsubscribe_all()
            await client.close()
            self.logger.info("Called close on Polygon client during shutdown")
        except Exception as exc:
            self.logger.error("Failed to close Polygon client: %s", exc)

    async def _shutdown_refinitiv_ws(self, ws):
        if ws is None:
            return
        try:
            await ws.close()
            self.logger.info("Called close on Refinitiv WebSocket during shutdown")
        except Exception as exc:
            self.logger.error("Failed to close Refinitiv WebSocket: %s", exc)

    # The code is the same, but
    # I want to just have repetition to check off every box.
    async def _shutdown_polygonfx_client(self, client):
        if client is None:
            return
        try:
            client.unsubscribe_all()
            await client.close()
            self.logger.info("Called close on Polygonfx client during shutdown")
        except Exception as exc:
            self.logger.error("Failed to close Polygonfx client: %s", exc)

    # @retry(wait=wait_exponential(multiplier=1, min=1, max=30))
    @retry(wait=wait_fixed(1.5))
    async def init_hl(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying HL feed")
            return
        if len(self.hl_syms) == 0:
            self.logger.info("Not subscribing to HL, returning")
            return

        # Get all the symbols we want, start websocket, start subscribing.
        async with websockets.connect(
            self.hl_ws_url, ping_interval=100000000
        ) as ws:
            self.logger.info("Hyperliquid websocket connected at {}".format(self.hl_ws_url))
            self.logger.info("Hyperliquid syms: {}".format(self.hl_syms))
            self.hl_count = 0
            # Per-connection book state for fast+slow l2Book merge.
            # fast: 5 levels @ 0.5s. slow: 20 levels @ 5s (post network upgrade).
            self.hl_fast_book = {}
            self.hl_slow_book = {}

            # HL will disconect after 60s of inactivity, so ping every 25s so we have 2 chances
            # plus some buffer to keep the connection alive, just to be safe
            ping_task = asyncio.create_task(self.hl_ping(ws, 25))
            receive_task = asyncio.create_task(self.hl_receive_loop(ws))
            eod_task = asyncio.create_task(self.eod_event.wait())
            reload_task = asyncio.create_task(self._wait_reload(self.hl_reload_event))

            try:
                done, pending = await asyncio.wait(
                    {ping_task, receive_task, eod_task, reload_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel all pending tasks and wait for cancellations to complete
                self.logger.info(f"HL feed stopped (done: {done}). Cancelling all tasks.")
                await self._cancel_pending(pending, "HL")

                # Reraise any exceptions in the child tasks to trigger the retry
                self.logger.info("HL feed tasks cancelled. Raising task exception.")
                for task in done:
                    if exc := task.exception():
                        raise exc
                self.logger.info("HL feed cancelled")
            except websockets.exceptions.ConnectionClosedOK as e:
                if e.rcvd.code == 1000 and e.rcvd.reason == "Expired":
                    # This is the new (since 20251206) behavior from HL where server closes
                    # connections after ~3h, regardless of activity. Just have to reconnect.
                    # https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
                    # "Important: all automated users should handle disconnects from the server side
                    #  and gracefully reconnect. Disconnection from API servers may happen
                    #  periodically and without announcement. Missed data during the reconnect will
                    #  be present in the snapshot ack on reconnect. Users can also manually query
                    #  any missed data using the corresponding info request." (added 20251208)
                    self.logger.warning("Regular HL ConnectionClosedOK. Reconnecting.")
                    raise # retry
                else:
                    self.logger.error(f"Unexpected HL ConnectionClosedOK: {e}")
                    raise # still retry
            except ForceRestartFeed as e:
                # any alerts should already be sent by raiser
                self.logger.error(f"HL ForceRestartFeed: {e}")
                raise # retry
            except Exception as e:
                self.logger.error(f"HL feed error: {e}")
                self.logger.error(traceback.format_exc())
                raise  # retry anyway
            finally:
                self.logger.info(f"HL feed got {self.hl_count} messages today")

    async def hl_ping(self, ws, interval):
        while not self.eod_event.is_set():
            try:
                await ws.send(json.dumps({"method": "ping"}))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                self.logger.info("HL ping cancelled")
                raise

    async def hl_sub(self, ws):
        try:
            for one_sym in self.hl_syms:

                dex_index = one_sym.find(":")

                # Default l2Book (20 levels @ 5s post-upgrade) and fast variant
                # (5 levels @ 0.5s). Merged downstream in hl_receive_loop.
                l2_slow_sub_dct = {
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": one_sym},
                    }

                l2_fast_sub_dct = {
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": one_sym, "fast": True},
                    }

                trade_sub_dct = {
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": one_sym},
                }

                if dex_index != -1:
                    dex_name = one_sym[:dex_index]

                    if dex_name not in ["xyz"]:
                        raise RuntimeError("Tried to subscribe to unsupported dex: {}".format(dex_name))

                    l2_slow_sub_dct["subscription"]["dex"] = dex_name
                    l2_fast_sub_dct["subscription"]["dex"] = dex_name
                    trade_sub_dct["subscription"]["dex"] = dex_name

                await ws.send(json.dumps(l2_slow_sub_dct))
                await ws.send(json.dumps(l2_fast_sub_dct))
                await ws.send(json.dumps(trade_sub_dct))
        except Exception as e:
            self.logger.error("HL sub got exception: {}".format(e))

    def hl_merge_book(self, sym):
        """Merge fast (top-5) and slow (20 lvls) snapshots for one symbol.

        Fast prices are preferred for the levels they cover (fresher). Slow
        contributes only levels strictly outside fast's covered price range,
        so the merged book is monotonic in price even if fast and slow disagree
        on what the top-5 are.
        """
        fast = self.hl_fast_book.get(sym)
        slow = self.hl_slow_book.get(sym)
        if fast is None:
            return (slow[0], slow[1]) if slow else ([], [])
        if slow is None:
            return fast[0], fast[1]

        fast_bids, fast_asks = fast
        slow_bids, slow_asks = slow

        if fast_bids:
            cutoff_bid = float(fast_bids[-1]["px"])
            merged_bids = list(fast_bids) + [lvl for lvl in slow_bids if float(lvl["px"]) < cutoff_bid]
        else:
            merged_bids = list(slow_bids)

        if fast_asks:
            cutoff_ask = float(fast_asks[-1]["px"])
            merged_asks = list(fast_asks) + [lvl for lvl in slow_asks if float(lvl["px"]) > cutoff_ask]
        else:
            merged_asks = list(slow_asks)

        return merged_bids, merged_asks

    def _hl_advance_published_trade_gate(self, sym, trade_time, source):
        """Returns True if this trade should be published, updating the
        shared per-sym (trade_time, source) marker in place. Mirrors C++
        advanceHLPublishedTradeGateLocked.

        Within a single trade_time we only accept trades from the source
        that first claimed that slot — same-source distinct trades at
        the same trade_time are still allowed (arrival order is
        monotonic within a single source). New trade_times reset the
        source ownership.
        """
        if trade_time <= 0:
            return True
        last_t, last_source = self.hl_last_published_trade_by_sym.get(sym, (0, None))
        if trade_time < last_t:
            return False
        if trade_time > last_t:
            self.hl_last_published_trade_by_sym[sym] = (trade_time, source)
            return True
        # Same trade_time: only the source that opened it may add more.
        return source == last_source

    async def hl_receive_loop(self, ws):
        await self.hl_sub(ws)
        while not self.eod_event.is_set():
            try:
                self.hl_count += 1
                # Make sure we yield every 10 messages even when not publishing
                if self.hl_count % 10 == 0:
                    await asyncio.sleep(0)
                # Make sure we yield
                # No need to time out to send pings, since we're doing that in a separate coroutine
                message = await ws.recv()
                try:
                    msg_dct = json.loads(message)
                    #print(msg_dct)
                except json.JSONDecodeError as e:
                    self.logger.error(f"Invalid JSON received: {e}, message: {message[:100]}")
                    continue  # Skip this message, don't crash

                if msg_dct["channel"] == "l2Book":
                    sym = msg_dct["data"]["coin"]
                    ws_event_time = int(msg_dct["data"].get("time", 0))
                    bids = msg_dct["data"]["levels"][0]
                    asks = msg_dct["data"]["levels"][1]

                    # Fast push is 5 levels, slow push is 20. Pre-upgrade both
                    # subscriptions return the same slow shape, which still
                    # works: fast bucket stays empty and we publish slow only.
                    is_fast = max(len(bids), len(asks)) <= 5
                    if is_fast:
                        self.hl_fast_book[sym] = (bids, asks)
                    else:
                        self.hl_slow_book[sym] = (bids, asks)

                    merged_bids, merged_asks = self.hl_merge_book(sym)

                    one_msg_pb = mdmsg_pb2.PbMessage()
                    one_msg_pb.symbol_id = sym
                    # The enum needs to match with mdmsg.proto
                    one_msg_pb.market = 4
                    msg_update = one_msg_pb.book_snapshot
                    # Stamp the chain time so consumers can measure latency
                    # and so our monotonic gate has something to compare.
                    if ws_event_time:
                        msg_update.event_time = ws_event_time
                        # Freshness marker (see self.hl_last_ws_book_event_time_ms).
                        self.hl_last_ws_book_event_time_ms = int(ws_event_time)

                    for one_bid_lvl in merged_bids:
                        one_bid_lvl_pb = msg_update.bids.add()
                        one_bid_lvl_pb.px = one_bid_lvl["px"]
                        one_bid_lvl_pb.qty = one_bid_lvl["sz"]
                        one_bid_lvl_pb.numords = one_bid_lvl["n"]

                    for one_ask_lvl in merged_asks:
                        one_ask_lvl_pb = msg_update.asks.add()
                        one_ask_lvl_pb.px = one_ask_lvl["px"]
                        one_ask_lvl_pb.qty = one_ask_lvl["sz"]
                        one_ask_lvl_pb.numords = one_ask_lvl["n"]

                    msg_str = one_msg_pb.SerializeToString()
                    # In unified mode (HyperliquidNode also configured), only
                    # publish when the node feed is unhealthy. WS stays warm
                    # so it can take over on the flip.
                    if not self.hl_publishing_from_node:
                        # Monotonic gate: don't publish a chain state older
                        # than the latest already-published for this sym.
                        last = self.hl_last_book_event_time_by_sym.get(sym, 0)
                        if ws_event_time and ws_event_time < last:
                            pass  # stale; skip
                        else:
                            if ws_event_time:
                                self.hl_last_book_event_time_by_sym[sym] = ws_event_time
                            await self.publish_async(msg_str)
                    if self.hl_count % 300 == 0:
                        self.logger.info("HL {}".format(sym))
                elif msg_dct["channel"] == "trades":

                    for one_trd_dct in msg_dct["data"]:

                        #print(one_trd_dct)

                        one_msg_pb = mdmsg_pb2.PbMessage()
                        one_msg_pb.symbol_id = one_trd_dct["coin"]
                        one_msg_pb.market = 4

                        msg_trd = one_msg_pb.book_trade

                        msg_trd.px = one_trd_dct["px"]
                        msg_trd.qty = one_trd_dct["sz"]
                        msg_trd.passive_was_buyer = one_trd_dct["side"] != "B"
                        msg_trd.trade_time = one_trd_dct["time"]
                        # WS trades carry a tid; populate it so the de-dupe
                        # gate can distinguish same-ms trades AND match the
                        # tid emitted by the node-side publisher (which sets
                        # trade_id from the on-chain fill tid).
                        ws_tid = int(one_trd_dct.get("tid", 0))
                        if ws_tid:
                            msg_trd.trade_id = ws_tid
                        # Freshness marker (see self.hl_last_ws_trade_time_ms).
                        self.hl_last_ws_trade_time_ms = int(one_trd_dct["time"])

                        # Publish trade data too. Same source-primary gate as
                        # the book path. De-dupe gate is per (sym, trade_time,
                        # source) so the same trade arriving via both WS and
                        # node (e.g. around a primary flip) doesn't publish
                        # twice.
                        msg_str = one_msg_pb.SerializeToString()
                        if not self.hl_publishing_from_node:
                            sym = one_trd_dct["coin"]
                            t = int(one_trd_dct.get("time", 0))
                            if self._hl_advance_published_trade_gate(sym, t, "ws"):
                                await self.publish_async(msg_str)

                    pass
                elif msg_dct["channel"] == "subscriptionResponse":
                    self.logger.info(msg_dct)
                else:
                    #print(msg_dct)
                    # Probably something else.
                    pass

                #print(message)
            except asyncio.CancelledError:
                self.logger.info("HL receive loop cancelled")
                raise
            except websockets.exceptions.ConnectionClosedOK as e:
                self.logger.warning("HL receive loop got ConnectionClosedOK")
                raise
            except Exception as e:
                self.logger.error(f"Error in HL receive loop: {e}")
                raise  # This will trigger the @retry decorator


    # ----- HyperliquidNode (n1 ZMQ feed) ------------------------------------
    # Three coroutines run together when HyperliquidNode is configured:
    #   - init_hl_node():    one task per ZMQ socket (books, fills). Each
    #                         parses incoming PbMessage blobs, applies the
    #                         hl_node_syms filter, forwards the original
    #                         bytes to the local downstream PUB.
    #   - init_hl_node_health(): 1 Hz state machine. Promotes node to
    #                             primary when fresh, demotes when silent
    #                             or lagging. Toggles the shared
    #                             hl_publishing_from_node flag.
    #   - init_hl() (existing): WS stays warm and processes msgs even when
    #                            the gate is closed; it only calls
    #                            publish_async when hl_publishing_from_node
    #                            is False.
    # Design: overmind/studies/hl_feed/pymultifeed_node_consumer_20260611.md

    @retry(wait=wait_fixed(1.5))
    async def init_hl_node(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not starting HL node feed")
            return
        if not self.hl_node_books_endpoint or not self.hl_node_fills_endpoint:
            self.logger.info("HL node feed not configured; skipping init_hl_node")
            return

        sym_filter = set(self.hl_node_syms)
        # Create the SUB sockets before the main try/finally. Clean up on any
        # setup failure so that — now that this coroutine is @retry-wrapped — a
        # bad endpoint can't leak a zmq context + sockets on every retry.
        ctx = zmq.asyncio.Context()
        books_sub = None
        fills_sub = None
        try:
            books_sub = ctx.socket(zmq.SUB)
            fills_sub = ctx.socket(zmq.SUB)
            books_sub.connect(self.hl_node_books_endpoint)
            fills_sub.connect(self.hl_node_fills_endpoint)
            books_sub.setsockopt(zmq.SUBSCRIBE, b"")
            fills_sub.setsockopt(zmq.SUBSCRIBE, b"")
        except Exception:
            if books_sub is not None:
                books_sub.close(linger=0)
            if fills_sub is not None:
                fills_sub.close(linger=0)
            ctx.term()
            raise
        self.logger.info(
            "HL node SUB books=%s fills=%s syms=%d",
            self.hl_node_books_endpoint, self.hl_node_fills_endpoint,
            len(sym_filter),
        )

        async def relay(sock, label):
            local_count = 0
            while not self.eod_event.is_set():
                try:
                    blob = await sock.recv()
                except Exception as e:
                    self.logger.error(f"HL node {label} sub error: {e}")
                    raise
                pb = mdmsg_pb2.PbMessage()
                try:
                    pb.ParseFromString(blob)
                except Exception:
                    # malformed; skip
                    continue
                if sym_filter and pb.symbol_id not in sym_filter:
                    continue
                # Update health-check telemetry. event_time is in ms.
                self.hl_node_last_msg_wall_ms = int(time.time() * 1000)
                if pb.HasField("book_snapshot") and pb.book_snapshot.event_time:
                    self.hl_node_last_event_time_ms = pb.book_snapshot.event_time
                # Forward to local PUB only if gate is open (node is primary).
                # Apply the monotonic per-sym time gate; defensive against
                # any out-of-order delivery from ZMQ.
                if self.hl_publishing_from_node:
                    sym = pb.symbol_id
                    publish = True
                    if pb.HasField("book_snapshot"):
                        t = pb.book_snapshot.event_time
                        last = self.hl_last_book_event_time_by_sym.get(sym, 0)
                        if t and t < last:
                            publish = False
                        elif t:
                            self.hl_last_book_event_time_by_sym[sym] = t
                    elif pb.HasField("book_trade"):
                        t = pb.book_trade.trade_time
                        # Mirror C++: trade de-dupe gate tracks ACTUAL
                        # published output. Only advance it when we're
                        # going to publish, so a stale or duplicate node
                        # trade can't shadow a fresh WS publish later.
                        publish = self._hl_advance_published_trade_gate(
                            sym, t, "node"
                        )
                        # Freshness marker (see self.hl_last_node_trade_time_ms).
                        if t:
                            self.hl_last_node_trade_time_ms = int(t)
                    if publish:
                        await self.publish_async(blob)
                local_count += 1
                if local_count % 1000 == 0:
                    self.logger.info(
                        "HL node %s relayed %d msgs (publishing_from_node=%s)",
                        label, local_count, self.hl_publishing_from_node,
                    )

        books_task = asyncio.create_task(relay(books_sub, "books"))
        fills_task = asyncio.create_task(relay(fills_sub, "fills"))
        health_task = asyncio.create_task(self.init_hl_node_health())
        eod_task = asyncio.create_task(self.eod_event.wait())
        reload_task = asyncio.create_task(self._wait_reload(self.hl_node_reload_event))
        try:
            done, pending = await asyncio.wait(
                {books_task, fills_task, health_task, eod_task, reload_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            await self._cancel_pending(pending, "HL node")
            self.logger.info("HL node feed tasks cancelled. Raising task exception.")
            for t in done:
                if exc := t.exception():
                    raise exc
        finally:
            books_sub.close(linger=0)
            fills_sub.close(linger=0)
            ctx.term()
            self.logger.info("HL node feed shutdown complete")

    async def init_hl_node_health(self):
        """Promote the node feed to primary when fresh, demote when silent
        or lagging. Eager flip-back when health is restored."""
        # On startup with a WS fallback, default to WS primary until the
        # first node msg lands. Without WS, stay node-primary unconditionally
        # — the flip-on-lag logic would suppress our only feed (mirrors the
        # C++ guard in pkmultifeed.cc subscribeHyperliquidNode).
        has_ws_fallback = bool(self.hl_syms)
        if has_ws_fallback:
            self.hl_publishing_from_node = False
        else:
            self.hl_publishing_from_node = True
        last_logged_state = self.hl_publishing_from_node
        # Rolling 5-minute summary of node-vs-WS trade-arrival freshness
        # (mirrors pkmultifeed). Sampled every 3s, accumulator-only, one
        # log line per window. diff = node_chain_time - ws_chain_time
        # (positive means node has seen a fresher trade than WS). The
        # "tied" bucket captures samples where no new trade arrived on
        # either side, so both markers still hold the same prior trade.
        FRESH_SAMPLE_SECS = 3
        FRESH_WINDOW_SECS = 300
        FRESH_SAMPLES_PER_WINDOW = FRESH_WINDOW_SECS // FRESH_SAMPLE_SECS

        def _new_window():
            return {"n": 0, "sum": 0, "min": None, "max": None,
                    "node_ahead": 0, "tied": 0, "ws_ahead": 0}

        def _accumulate(fw, node_t, ws_t, label):
            if not node_t or not ws_t:
                return
            d = node_t - ws_t
            fw["n"] += 1
            fw["sum"] += d
            fw["min"] = d if fw["min"] is None or d < fw["min"] else fw["min"]
            fw["max"] = d if fw["max"] is None or d > fw["max"] else fw["max"]
            if d > 0:
                fw["node_ahead"] += 1
            elif d < 0:
                fw["ws_ahead"] += 1
            else:
                fw["tied"] += 1
            if fw["n"] >= FRESH_SAMPLES_PER_WINDOW:
                mean = fw["sum"] // fw["n"]
                self.logger.info(
                    "HL %s freshness %ds window: n=%d mean=%+dms min=%+d max=%+d "
                    "ahead=node %d / tied %d / ws %d",
                    label, FRESH_WINDOW_SECS, fw["n"], mean, fw["min"], fw["max"],
                    fw["node_ahead"], fw["tied"], fw["ws_ahead"],
                )
                fw.update(_new_window())

        trade_fw = _new_window()
        book_fw = _new_window()
        freshness_log_counter = 0
        while not self.eod_event.is_set():
            await asyncio.sleep(1.0)
            freshness_log_counter += 1
            if (
                has_ws_fallback
                and freshness_log_counter % FRESH_SAMPLE_SECS == 0
            ):
                _accumulate(trade_fw,
                            self.hl_last_node_trade_time_ms,
                            self.hl_last_ws_trade_time_ms,
                            "trade")
                _accumulate(book_fw,
                            self.hl_node_last_event_time_ms,
                            self.hl_last_ws_book_event_time_ms,
                            "book")
            if not has_ws_fallback:
                # Node-only mode: never flip away. Just log liveness occasionally
                # so a silent node feed is still visible in the journal.
                if freshness_log_counter % 60 == 0:
                    last_msg_age = (
                        int(time.time() * 1000) - self.hl_node_last_msg_wall_ms
                        if self.hl_node_last_msg_wall_ms else -1
                    )
                    self.logger.info(
                        "HL node health (no-WS-fallback mode): last_msg_age_ms=%d",
                        last_msg_age,
                    )
                continue
            wall_ms = int(time.time() * 1000)
            silent = (
                self.hl_node_last_msg_wall_ms == 0
                or (wall_ms - self.hl_node_last_msg_wall_ms)
                > int(self.hl_node_fallback_silent_s * 1000)
            )
            lagging = (
                self.hl_node_last_event_time_ms
                and (wall_ms - self.hl_node_last_event_time_ms)
                > int(self.hl_node_fallback_lag_s * 1000)
            )
            healthy = not (silent or lagging)
            last_msg_age_ms = (
                wall_ms - self.hl_node_last_msg_wall_ms
                if self.hl_node_last_msg_wall_ms else -1
            )
            last_event_age_ms = (
                wall_ms - self.hl_node_last_event_time_ms
                if self.hl_node_last_event_time_ms else -1
            )
            if healthy != last_logged_state:
                self.logger.warning(
                    "HL node feed health flip: healthy=%s "
                    "silent=%s lagging=%s last_msg_age_ms=%d last_event_age_ms=%d",
                    healthy, silent, lagging, last_msg_age_ms, last_event_age_ms,
                )
                last_logged_state = healthy
            # First time we go unhealthy while we have a WS fallback
            # available, fire a one-shot alert so on-call knows the WS
            # is now carrying production. We don't re-alert on subsequent
            # flips during the same run — emails would just be noise.
            if (not healthy
                    and self.hl_syms
                    and not self.hl_node_unhealthy_alert_sent):
                try:
                    email_utils.send_alerts(
                        "HL NODE FEED UNHEALTHY (pymultifeed)",
                        f"HL node feed went unhealthy:\n"
                        f"  silent={silent} (last_msg_age_ms={last_msg_age_ms}, "
                        f"threshold={self.hl_node_fallback_silent_s}s)\n"
                        f"  lagging={lagging} (last_event_age_ms={last_event_age_ms}, "
                        f"threshold={self.hl_node_fallback_lag_s}s)\n"
                        f"  WS fallback now publishing for {len(self.hl_syms)} syms.\n"
                        f"  (One alert per run; check the journal for further flips.)"
                    )
                except Exception as e:
                    self.logger.error("Failed to send HL node alert: %s", e)
                self.hl_node_unhealthy_alert_sent = True
            self.hl_publishing_from_node = healthy


    # @retry(wait=wait_exponential(multiplier=1, min=1, max=30))
    @retry(wait=wait_fixed(1.5))
    async def init_pyth(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying Pyth feed")
            return
        if len(self.pyth_syms) == 0:
            self.logger.info("Not subscribing to Pyth, returning")
            return

        pyth_url = "wss://hermes.pyth.network/ws"
        async with websockets.connect(pyth_url) as ws:
            self.logger.info("Pyth syms: {}".format(self.pyth_syms))
            self.pyth_count = 0

            receive_task = asyncio.create_task(self.pyth_receive_loop(ws))
            eod_task = asyncio.create_task(self.eod_event.wait())

            try:
                done, pending = await asyncio.wait(
                    {receive_task, eod_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel all pending tasks and wait for cancellations to complete
                self.logger.info(f"Pyth feed stopped (done: {done}). Cancelling all tasks.")
                await self._cancel_pending(pending, "Pyth")

                # Reraise any exceptions in the child tasks to trigger the retry
                self.logger.info("Pyth feed tasks cancelled. Raising task exception.")
                for task in done:
                    if exc := task.exception():
                        raise exc
                self.logger.info("Pyth feed cancelled")
            except ForceRestartFeed as e:
                # any alerts should already be sent by raiser
                self.logger.error(f"Pyth ForceRestartFeed: {e}")
                raise # retry
            except websockets.exceptions.ConnectionClosedError:
                # Pyth randomly disconnects every few hours, just need to reconnect
                self.logger.error("Pyth connection error, reconnecting.")
                raise # retry
            except Exception as e:
                self.logger.error(f"Pyth feed error: {e}")
                self.logger.error(traceback.format_exc())
                raise  # This will trigger the @retry decorator
            finally:
                self.logger.info(f"Pyth feed got {self.pyth_count} messages today")

    async def pyth_receive_loop(self, ws):
        # Subscribe
        for one_sym in self.pyth_syms:
            if one_sym not in pyth_names_to_addresses:
                self.logger.error("Could not find pyth address for {}".format(one_sym))
                continue

            contract_addr = pyth_names_to_addresses[one_sym]
            await ws.send(json.dumps({
                "type": "subscribe",
                "ids": [contract_addr]
            }))

        # Read until eod
        while not self.eod_event.is_set():
            try:
                self.pyth_count += 1
                # Avoiding asyncio.wait_for bc of Python 3.10 cancel-swallowing bug.
                recv_task = asyncio.create_task(ws.recv())
                try:
                    done, _ = await asyncio.wait({recv_task}, timeout=600)
                    if not done:
                        err_msg = "No Pyth message for 10min, forcing reconnect."
                        self.logger.error(err_msg)
                        email_utils.send_alerts("PYTH FEED DOWN", err_msg)
                        raise ForceRestartFeed(f"Pyth feed down for 10min")
                    message = recv_task.result()
                finally:
                    if not recv_task.done():
                        self.logger.info("Cancelling Pyth recv_task")
                        recv_task.cancel()
                # message = await ws.recv()
                # Just gonna benchmark how long this is.

                try:
                    msg_dct = json.loads(message)
                    #print(msg_dct)
                except json.JSONDecodeError as e:
                    self.logger.error(f"Invalid JSON received: {e}, message: {message[:100]}")
                    continue  # Skip this message, don't crash


                if msg_dct["type"] == "price_update":

                    # Do the lookup for the symbol.

                    feed_json = msg_dct["price_feed"]
                    address = feed_json["id"]
                    if address not in pyth_addresses_to_names:
                        # Got an unknown address for some reason.
                        self.logger.error("Got unknown pyth address {}".format(address))
                        continue
                    symbol = pyth_addresses_to_names[address]
                    pub_t_ms = feed_json["price"]["publish_time"] * 1000

                    # In milliseconds.
                    now_ms = time.time() * 1000

                    # Don't publish too much.
                    if (now_ms - self.pyth_last_publish_time.get(symbol, 0)) < self.publish_interval_ms:
                        continue

                    # Skip if we're not in session.
                    # Note: out of session, Pyth repeatedly sends messages for each subscribed
                    # symbol with a timestamp 3s after session end, so this works to skip them.
                    pub_t_ns = pub_t_ms * 1_000_000
                    if not self._is_in_pyth_session(pub_t_ns, symbol):
                        continue

                    # Don't publish this if we have more up to date data
                    if pub_t_ms < self.sym_last_publish_transact_time[symbol]:
                        continue

                    px_basic = float(feed_json["price"]["price"])
                    px_adj = feed_json["price"]["expo"]
                    # I'm doing this on every time because I'm
                    # not sure if they will give different feeds with
                    # different exp's.
                    px = round(px_basic * math.pow(10, px_adj), -px_adj)
                    px_str = str(px)

                    # Construct the pb objects.

                    pbmsg = mdmsg_pb2.PbMessage()
                    pbmsg.market = 8  # TOPBOOK_EQUITY

                    pbmsg.symbol_id = symbol

                    # Don't give it real sizes because we don't have that.
                    pbquote = pbmsg.quote
                    pbquote.best_bid_px = px_str
                    pbquote.best_bid_qty = "1"
                    pbquote.best_ask_px = px_str
                    pbquote.best_ask_qty = "1"
                    pbquote.mid_px = px

                    # The time it was published.
                    pbquote.transact_time = pub_t_ms

                    #print(pbmsg)
                    pub_string = pbmsg.SerializeToString()

                    self.sym_last_publish_transact_time[symbol] = pub_t_ms
                    self.pyth_last_publish_time[symbol] = now_ms
                    await self.publish_async(pub_string)

                    recv_delay_ms = now_ms - pub_t_ms
                    if self.pyth_count < 10 or self.pyth_count % 300 == 0 or recv_delay_ms > 3000:
                        self.logger.info(
                            "{}: Pyth {} msgtime {} delay_ms {}".format(
                                now_ms, symbol, pub_t_ms, recv_delay_ms
                            )
                        )

                    # Kill feed if data is too stale (e.g., > 20s)
                    if recv_delay_ms > self.PYTH_STALE_THRESHOLD_MS:
                        err_msg = (f"Pyth {symbol} delay {recv_delay_ms}ms exceeds "
                                   f"{self.PYTH_STALE_THRESHOLD_MS}ms threshold, forcing reconnect.")
                        self.logger.error(err_msg)
                        # Pyth is sometimes just permanently delayed, so only alert every hour.
                        email_utils.send_alerts("PYTH FEED STALE", err_msg, 60)
                        raise ForceRestartFeed(f"Pyth feed stale: {symbol} delay={recv_delay_ms}ms")

                    # Make the quote.
                    # We can call it TopBookEquity too.
                else:
                    #print(msg_dct)
                    # Probably something else.
                    pass
            except asyncio.CancelledError:
                self.logger.info("Pyth receive loop cancelled")
                raise
            except ForceRestartFeed:
                raise # retry


    ########################################################################################
    # Start Refinitiv (LSEG) RTO feed for Korean equities

    async def refinitiv_get_auth_token(self):
        """Get OAuth2 access token from Refinitiv using client_credentials grant."""
        url = "https://api.refinitiv.com/auth/oauth2/v2/token"
        payload = {
            "grant_type": "client_credentials",
            "scope": "trapi",
            "client_id": REFINITIV_CLIENT_ID,
            "client_secret": REFINITIV_CLIENT_SECRET,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Refinitiv auth failed ({resp.status}): {body}")
                data = await resp.json()
                access_token = data["access_token"]
                expires_in = data.get("expires_in", 300)
                self.logger.info(f"Refinitiv auth token obtained, expires_in={expires_in}s")
                return access_token, expires_in

    def _build_refinitiv_login_request(self, token=None, refresh: bool = False):
        """Create a login (or refresh) request payload for the Refinitiv WebSocket."""
        if token is None:
            token = self.refinitiv_auth_token
        login_req = {
            "ID": 1,
            "Domain": "Login",
            "Key": {
                "NameType": "AuthnToken",
                "Elements": {
                    "ApplicationId": "256",
                    "Position": self.position,
                    "AuthenticationToken": token,
                },
            },
        }
        if refresh:
            login_req["Refresh"] = True
        return login_req

    async def refinitiv_discover_service(self, token):
        """Discover RTO streaming endpoint using the service discovery API."""
        url = "https://api.refinitiv.com/streaming/pricing/v1/?transport=websocket"
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Refinitiv discovery failed ({resp.status}): {body}")
                data = await resp.json()
                # Pick the first available endpoint
                services = data.get("services", [])
                if not services:
                    raise RuntimeError("Refinitiv discovery returned no services")
                # Extract endpoint host and port from the first service entry
                svc = services[0]
                ep = svc.get("endpoint", "")
                port = svc.get("port", 443)
                if isinstance(ep, str) and ep:
                    # Flat format: {"endpoint": "host.example.com", "port": 14002}
                    result = f"{ep}:{port}"
                elif isinstance(ep, list) and len(ep) > 0:
                    # List format: {"endpoint": [{"endpoint": "host", "port": 443}]}
                    entry = ep[0]
                    if isinstance(entry, dict):
                        host = entry.get("endpoint", entry.get("host", ""))
                        port = entry.get("port", 443)
                        result = f"{host}:{port}"
                    else:
                        result = f"{entry}:{port}"
                else:
                    raise RuntimeError(f"Refinitiv discovery: unexpected format: {data}")
                self.logger.info(f"Refinitiv discovered endpoint: {result}")
                return result

    @retry(wait=wait_fixed(1.5))
    async def init_refinitiv(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying Refinitiv feed")
            return
        if len(self.refinitiv_syms) == 0:
            self.logger.info("Not subscribing to Refinitiv, returning")
            return
        if REFINITIV_CLIENT_ID is None or REFINITIV_CLIENT_SECRET is None:
            self.logger.error("Refinitiv credentials not loaded, cannot start feed")
            return

        try:
            # 1. Authenticate
            token, expires_in = await self.refinitiv_get_auth_token()
            self.refinitiv_auth_token = token
            self.refinitiv_token_expiry = time.time() + expires_in - 30  # refresh early

            # 2. Discover streaming endpoint
            endpoint = await self.refinitiv_discover_service(token)

            # 3. Connect WebSocket
            ws_url = f"wss://{endpoint}/WebSocket"
            self.logger.info(f"Refinitiv connecting to {ws_url}")
            self.logger.info(f"Refinitiv syms: {self.refinitiv_syms}")

            # Reset the message queue
            self.refinitiv_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)

            async with websockets.connect(
                ws_url,
                subprotocols=["tr_json2"],
                ping_interval=None,  # we handle ping/pong ourselves
            ) as ws:
                self.refinitiv_ws = ws
                self.refinitiv_count = 0
                self.refinitiv_last_activity = time.time()

                consume_task = asyncio.create_task(self.consume_refinitiv_loop(ws))
                process_task = asyncio.create_task(self.process_refinitiv_queue())
                heartbeat_task = asyncio.create_task(
                    self._monitor_feed_heartbeat(
                        "Refinitiv",
                        "refinitiv_last_activity",
                        self.refinitiv_heartbeat_timeout_secs,
                        self.refinitiv_heartbeat_timeout_secs / 2,
                        session_guard=lambda: self._is_any_refinitiv_session_active(
                            int(time.time() * 1_000_000_000)
                        ),
                        alert=True,
                    )
                )
                eod_task = asyncio.create_task(self.eod_event.wait())
                refresh_task = asyncio.create_task(self.refinitiv_token_refresh_loop(ws))
                reload_task = asyncio.create_task(self._wait_reload(self.refinitiv_reload_event))

                tasks = {consume_task, process_task, heartbeat_task, eod_task, refresh_task,
                         reload_task}
                # FX task runs independently — if Pyth dies we keep Refinitiv alive
                # as long as we have cached rates.
                fx_task = None
                if self.refinitiv_ric_convert_from:
                    fx_task = asyncio.create_task(self.refinitiv_fx_loop())
                    self.logger.info(f"Refinitiv FX conversion task started for: "
                                     f"{set(self.refinitiv_ric_convert_from.values())}")

                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                self.logger.info(f"Refinitiv feed stopped (done: {done}). Cancelling all tasks.")
                if fx_task is not None:
                    pending.add(fx_task)
                await self._cancel_pending(pending, "Refinitiv")

                self.logger.info("Refinitiv feed tasks cancelled. Raising task exception.")
                for task in done:
                    if exc := task.exception():
                        raise exc
                self.logger.info("Refinitiv feed cancelled")
        except asyncio.CancelledError:
            self.logger.info("Refinitiv feed cancelled")
            raise
        except HeartbeatException as e:
            self.logger.error(f"Refinitiv heartbeat error: {e}")
            raise
        except ForceRestartFeed as e:
            self.logger.error(f"Refinitiv ForceRestartFeed: {e}")
            raise  # retry
        except Exception as e:
            self.logger.error(f"Refinitiv feed error: {e}")
            self.logger.error(traceback.format_exc())
            raise  # retry
        finally:
            await self._shutdown_refinitiv_ws(self.refinitiv_ws)
            self.refinitiv_ws = None
            self.refinitiv_last_activity = None
            self.logger.info(f"Refinitiv feed got {getattr(self, 'refinitiv_count', 0)} messages today")

    async def refinitiv_token_refresh_loop(self, ws):
        """Refresh the OAuth token before expiry and re-issue the login request."""
        while not self.eod_event.is_set():
            try:
                wait_time = self.refinitiv_token_expiry - time.time()
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                if self.eod_event.is_set():
                    break

                token, expires_in = await self.refinitiv_get_auth_token()
                self.refinitiv_auth_token = token
                self.refinitiv_token_expiry = time.time() + expires_in - 30

                login_req = self._build_refinitiv_login_request(token=token, refresh=True)
                await ws.send(json.dumps([login_req]))
                self.logger.info(
                    "Refinitiv login refresh sent; token expires_in=%ss",
                    expires_in,
                )
            except asyncio.CancelledError:
                self.logger.info("Refinitiv token refresh loop cancelled")
                raise
            except Exception as exc:
                self.logger.error(f"Refinitiv token refresh failed: {exc}")
                self.logger.error(traceback.format_exc())
                raise

    @retry(wait=wait_fixed(1.5))
    async def refinitiv_fx_loop(self):
        """Subscribe to Pyth FX feeds for all currencies needed and update self.refinitiv_fx_rates.

        Runs independently of the Refinitiv feed — errors here will not tear down
        the Refinitiv websocket. @retry handles reconnection.
        """
        if self.eod_event.is_set():
            return
        pyth_url = "wss://hermes.pyth.network/ws"
        log_interval = 500
        msg_count = 0

        # Build address -> currency reverse map for all currencies we need
        addr_to_currency = {}
        subscribe_ids = []
        for currency in self.currencies_needed:
            addr = PYTH_FX_ADDRESSES.get(currency)
            if addr is None:
                err_msg = f"Refinitiv FX: no Pyth address for currency {currency} — check PYTH_FX_ADDRESSES config"
                self.logger.error(err_msg)
                email_utils.send_alerts("REFINITIV FX CONFIG ERROR", err_msg)
                return
            # Pyth feed IDs are without the 0x prefix in responses
            addr_to_currency[addr.replace("0x", "")] = currency
            subscribe_ids.append(addr)

        if not subscribe_ids:
            self.logger.error("Refinitiv FX: no valid currency feeds to subscribe to")
            return

        try:
            async with websockets.connect(pyth_url) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "ids": subscribe_ids
                }))
                self.logger.info(f"Refinitiv FX: subscribed to Pyth feeds for {list(self.currencies_needed)}")

                while not self.eod_event.is_set():
                    # Avoiding asyncio.wait_for bc of Python 3.10 cancel-swallowing bug.
                    recv_task = asyncio.create_task(ws.recv())
                    try:
                        done, _ = await asyncio.wait({recv_task}, timeout=600)
                        if not done:
                            err_msg = "Refinitiv FX: no Pyth message for 10min, reconnecting"
                            self.logger.error(err_msg)
                            raise asyncio.TimeoutError(err_msg)
                        raw = recv_task.result()
                    finally:
                        if not recv_task.done():
                            self.logger.info("Cancelling Refinitiv FX recv_task")
                            recv_task.cancel()

                    try:
                        msg_dct = json.loads(raw)
                    except json.JSONDecodeError as e:
                        self.logger.error(f"Refinitiv FX: invalid JSON: {e}")
                        continue

                    if msg_dct.get("type") == "price_update":
                        feed_json = msg_dct["price_feed"]
                        feed_id = feed_json.get("id", "")
                        currency = addr_to_currency.get(feed_id)
                        if currency is None:
                            self.logger.warning(f"Refinitiv FX: unknown Pyth feed_id {feed_id}")
                            continue

                        px_basic = float(feed_json["price"]["price"])
                        px_expo = feed_json["price"]["expo"]
                        rate = px_basic * math.pow(10, px_expo)

                        self.refinitiv_fx_rates[currency] = rate
                        self.refinitiv_fx_last_update[currency] = time.time()
                        msg_count += 1

                        if msg_count % log_interval == 1:
                            self.logger.info(f"Refinitiv FX: {currency}/USD rate={rate:.10f}")
        except asyncio.CancelledError:
            self.logger.info("Refinitiv FX loop cancelled")
            raise
        except websockets.exceptions.ConnectionClosedError:
            # Pyth randomly disconnects every few hours, just need to reconnect
            self.logger.error("Pyth connection error, reconnecting.")
            raise # retry
        except Exception as exc:
            self.logger.error(f"Refinitiv PYTHFX loop error: {exc}")
            self.logger.error(traceback.format_exc())
            if not self.refinitiv_fx_rates:
                err_msg = "Refinitiv PYTHFX: no cached rates — FX quotes will be dropped. Reconnecting."
                self.logger.error(err_msg)
                email_utils.send_alerts("REFINITIV PYTHFX DOWN", err_msg)                
            raise

    async def consume_refinitiv_loop(self, ws):
        """Read from Refinitiv WebSocket, handle protocol messages, enqueue data for processing."""

        # --- Login request ---
        login_req = self._build_refinitiv_login_request()
        await ws.send(json.dumps([login_req]))
        self.logger.info("Refinitiv login request sent")

        # Subscription is sent after login is confirmed (see Login Refresh handler below)
        subscribed = False

        # --- Receive loop ---
        while not self.eod_event.is_set():
            try:
                # Deliberately NOT wrapped in asyncio.wait_for (see _cancel_pending).
                # Stall detection is covered by the heartbeat monitor (60s and
                # in-session only, vs 600s all the time).
                raw = await ws.recv()

                messages = json.loads(raw)
                if not isinstance(messages, list):
                    messages = [messages]

                for msg in messages:
                    self.refinitiv_count += 1
                    self.refinitiv_last_activity = time.time()
                    msg_type = msg.get("Type", "")

                    # --- Ping/Pong ---
                    if msg_type == "Ping":
                        await ws.send(json.dumps([{"Type": "Pong"}]))
                        continue

                    # --- Login Refresh ---
                    domain = msg.get("Domain", "")
                    if domain == "Login":
                        if msg_type == "Refresh":
                            state = msg.get("State", {})
                            self.logger.info(f"Refinitiv login OK: {state}")
                            # Now that login is confirmed, send subscription
                            if not subscribed:
                                rics = list(self.refinitiv_ric_to_pub_sym.keys())
                                subscribe_req = {
                                    "ID": 2,
                                    "Key": {
                                        "Name": rics,
                                        "Service": self.refinitiv_service,
                                    },
                                    "View": [22, 25, 30, 31, 20, 686,
                                             6, 11, 21],  # 6=TRDPRC_1, 11=NETCHNG_1, 21=HST_CLOSE (for indices)
                                }
                                await ws.send(json.dumps([subscribe_req]))
                                self.logger.info(f"Refinitiv subscribed to {len(rics)} RICs: {rics}")
                                subscribed = True
                        elif msg_type == "Status":
                            self.logger.warning(f"Refinitiv login status: {msg}")
                        continue

                    # --- Status messages ---
                    if msg_type == "Status":
                        state = msg.get("State", {})
                        self.logger.warning(f"Refinitiv status: {msg.get('Key', {}).get('Name', '?')} {state}")
                        continue

                    # --- Enqueue Refresh / Update for processing ---
                    if msg_type in ("Refresh", "Update"):
                        self.refinitiv_queue.put_nowait(msg)

            except asyncio.CancelledError:
                self.logger.info("Refinitiv consume loop cancelled")
                raise
            except asyncio.QueueFull:
                self.logger.error("Refinitiv queue full!")
                raise
            except ForceRestartFeed:
                raise
            except websockets.exceptions.ConnectionClosedOK:
                self.logger.warning("Refinitiv ConnectionClosedOK, reconnecting")
                raise
            except Exception as e:
                self.logger.error(f"Error in Refinitiv consume loop: {e}")
                self.logger.error(traceback.format_exc())
                raise  # retry

    async def process_refinitiv_queue(self):
        """Process messages from Refinitiv queue: resolve symbols, apply FX, publish."""
        while not self.eod_event.is_set():
            try:
                msg = await self.refinitiv_queue.get()
                self.refinitiv_queue.task_done()

                fields = msg.get("Fields")
                if fields is None:
                    continue

                # Resolve RIC to our publish symbol
                ric = msg.get("Key", {}).get("Name", "")
                pub_sym = self.refinitiv_ric_to_pub_sym.get(ric)
                if pub_sym is None:
                    self.logger.warning(f"Refinitiv unknown RIC: {ric}")
                    continue

                now_s = time.time()
                now_ms = int(now_s * 1_000)

                # Throttle on publish symbol (shared across :krx/:nxt)
                last_pub = self.refinitiv_last_publish_time.get(pub_sym, 0)
                if now_ms - last_pub < self.publish_interval_ms:
                    continue

                # Per-RIC session guard using its calendar
                ts_event_ns = int(now_s * 1_000_000_000)
                cal = self.refinitiv_ric_calendar.get(ric, "")
                if self.testing and ric in (".KS200", ".N225"):
                    in_session = self._is_in_refinitiv_session(cal, ts_event_ns)
                    self.logger.info(f"TESTING Refinitiv raw: RIC={ric} pub_sym={pub_sym} cal={cal} in_session={in_session} fields={dict(fields)}")
                if not self._is_in_refinitiv_session(cal, ts_event_ns):
                    continue

                # NXT defers to KRX: if KRX published this symbol within 1s, skip NXT
                if cal == "krx_nxt_equities":
                    last_cal = self.refinitiv_last_publish_cal.get(pub_sym)
                    if last_cal == "krx_equities":
                        last_pub_ms = self.refinitiv_last_publish_time.get(pub_sym, 0)
                        if now_ms - last_pub_ms < 1000:
                            continue

                bid_px = fields.get("BID")
                ask_px = fields.get("ASK")
                bid_sz = fields.get("BIDSIZE")
                ask_sz = fields.get("ASKSIZE")

                # For indices (no bid/ask), use TRDPRC_1 as both bid and ask
                if bid_px is None or ask_px is None:
                    idx_px = fields.get("TRDPRC_1")
                    if idx_px is not None:
                        bid_px = idx_px
                        ask_px = idx_px
                        bid_sz = 1
                        ask_sz = 1
                    else:
                        continue

                bid_px = float(bid_px)
                ask_px = float(ask_px)
                bid_sz = float(bid_sz) if bid_sz is not None else 1
                ask_sz = float(ask_sz) if ask_sz is not None else 1

                if bid_px <= 0 or ask_px <= 0:
                    continue

                # Apply per-RIC FX conversion if configured
                convert_from = self.refinitiv_ric_convert_from.get(ric)
                if convert_from:
                    fx_rate = self.refinitiv_fx_rates.get(convert_from)
                    if not fx_rate:
                        if self.refinitiv_count % 1000 == 1:
                            err_str = f"Refinitiv: {convert_from}/USD rate not available yet, skipping quote"
                            self.logger.warning(err_str)
                        if self.refinitiv_count > 1000 and self.refinitiv_count % 1000 == 1:
                            # means we've been up for a while without rates
                            email_utils.send_alerts("REFINITIV FX RATE EMPTY", err_str)
                        continue
                    elif self.refinitiv_fx_last_update[convert_from] < now_s - 3_600:
                        if self.refinitiv_count % 1000 == 1:
                            err_str = (f"Refinitiv: {convert_from}/USD rate ({fx_rate}) over 1h stale."
                                       " Still using but PythFX might be down.")
                            self.logger.error(err_str)
                            email_utils.send_alerts("REFINITIV FX RATE STALE", err_str)
                    bid_px /= fx_rate
                    ask_px /= fx_rate

                # Build PbQuote
                pbmsg = mdmsg_pb2.PbMessage()
                pbmsg.market = 8  # PBMARKET_TOPBOOK_EQUITY
                pbmsg.symbol_id = pub_sym

                pbquote = pbmsg.quote
                pbquote.best_bid_px = str(round(bid_px, 6))
                pbquote.best_bid_qty = str(bid_sz)
                pbquote.best_ask_px = str(round(ask_px, 6))
                pbquote.best_ask_qty = str(ask_sz)
                pbquote.mid_px = round((bid_px + ask_px) / 2, 6)
                quote_ts_ms = self._resolve_refinitiv_transact_time(cal, fields, now_ms)
                pbquote.transact_time = quote_ts_ms

                pub_string = pbmsg.SerializeToString()

                self.sym_last_publish_transact_time[pub_sym] = quote_ts_ms
                self.refinitiv_last_publish_time[pub_sym] = now_ms
                self.refinitiv_last_publish_cal[pub_sym] = cal
                if (self.refinitiv_count % 500 == 0):
                    print(pbmsg)
                await self.publish_async(pub_string)

                if self.testing:
                    self.logger.info(f"Refinitiv {pub_sym} (RIC={ric}, cal={cal}) bid={bid_px} ask={ask_px}")
                elif self.refinitiv_count % 100 == 0:
                    self.logger.info(f"Refinitiv {pub_sym} (RIC={ric}, cal={cal}) bid={bid_px} ask={ask_px}")

            except asyncio.CancelledError:
                self.logger.info("Refinitiv process queue cancelled")
                raise
            except Exception as e:
                self.logger.error(f"Error processing Refinitiv queue: {e}")
                self.logger.error(traceback.format_exc())
                raise  # retry — future PR: consider log-and-continue for all feeds

    ########################################################################################
    # End Refinitiv


    # @retry(wait=wait_exponential(multiplier=1, min=2, max=120), reraise=True)
    @retry(wait=wait_fixed(1.5))
    async def init_databento_eq(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying DataBento EQ feed")
            return
        if len(self.dbento_eq_syms) == 0:
            self.logger.info("Not subscribing to DataBento EQ, returning")
            return

        # Reset the message queue
        self.dbento_eq_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        try:
            self.logger.info("DatabentoEq - connecting to {}".format(self.dbento_eq_syms))

            self.db_client = db.Live(key=db_API_KEY, ts_out=True, slow_reader_behavior="skip")
            self.subscribed_db_eq = False # whether we got a subscription confirmation
            self.got_sym_map_db_eq = False # whether we got at least 1 symbol mappings
            # TODO: let this be customizeable.
            self.db_client.subscribe(
                dataset="XNAS.BASIC",
                schema="cmbp-1",
                stype_in="raw_symbol",
                symbols=self.dbento_eq_syms,
            )

            # self.db_client.add_callback(self.consume_db_eq_cb)

            self.sym_to_idx_eq = {}
            self.idx_to_sym_eq = {}

            # self.db_client.start()
            self.dbento_eq_last_activity = time.time()
            self.db_eq_count = 0
            self.db_eq_real_count = 0
            self.db_eq_consume_consec_count = 0
            self.db_eq_process_consec_count = 0
            self.db_eq_bad_count = 0

            heartbeat_task = asyncio.create_task(
                self._monitor_feed_heartbeat(
                    "DataBento EQ",
                    "dbento_eq_last_activity",
                    self.databento_heartbeat_timeout_secs,
                    self.databento_heartbeat_check_secs,
                    session_guard=lambda: self._is_in_db_eq_session(
                        int(time.time() * 1_000_000_000)
                    ),
                    subscription_check_interval=5*60,
                    is_subscribed=lambda: self.subscribed_db_eq and self.got_sym_map_db_eq
                )
            )
            consume_task = asyncio.create_task(self.consume_db_eq_loop())
            process_task = asyncio.create_task(self.process_db_eq_queue())
            eod_task = asyncio.create_task(self.eod_event.wait())
            reload_task = asyncio.create_task(self._wait_reload(self.dbento_eq_reload_event))

            done, pending = await asyncio.wait(
                {heartbeat_task, consume_task, process_task, eod_task, reload_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel all pending tasks and wait for cancellations to complete
            self.logger.info(f"DataBento EQ feed stopped (done: {done}). Cancelling all tasks.")
            await self._cancel_pending(pending, "DataBento EQ")

            self.logger.info("DataBento EQ feed tasks cancelled. Raising task exception.")
            for task in done:
                if exc := task.exception():
                    raise exc

            self.logger.info("DataBento EQ feed cancelled")
        except asyncio.CancelledError:
            self.logger.info("DataBento EQ feed cancelled")
            # CancelledError is a BaseException, not Exception, so it won't trigger the retry.
            # We get this on a Ctrl-C event, so that is what we want.
            raise
        except HeartbeatException as e:
            self.logger.error(f"DataBento EQ heartbeat error: {e}")
            raise
        except ForceRestartFeed as e:
            # any alerts should already be sent by raiser
            self.logger.error(f"DataBento EQ ForceRestartFeed: {e}")
            raise # retry
        except Exception as e:
            self.logger.error(f"DataBento EQ error: {e}")
            self.logger.error(traceback.format_exc())
            raise
        finally:
            await self._shutdown_databento_client(self.db_client, "DataBento EQ")
            self.db_client = None
            self.dbento_eq_last_activity = None
            self.logger.info(f"DataBento EQ client got {self.db_eq_count} messages today, "
                             f"{self.db_eq_real_count} of them real")

    async def consume_db_eq_loop(self):
        """Enqueues records from DataBento EQ in async loop for later processing"""
        try:
            # This is the way to get async "callbacks" from DataBento. Automatically starts the
            # client, so don't call start() elsewhere, and no need to wait_for_close() either.
            async for db_record in self.db_client:
                if self.eod_event.is_set():
                    return
                # Warning: do NOT skip just because we're not in session! We need the symbol
                # mappings, which are sent when we start up and could arrive out of hours.
                self.dbento_eq_queue.put_nowait(db_record)
                # asyncio.Queue is not thread-safe so we need to do this wrapper
                # self.dbento_eq_queue.put_nowait(db_record)
                # asyncio.run_coroutine_threadsafe(self.dbento_eq_queue.put(db_record), self._loop)
                now_t = time.time()
                # Log and reset the number of consecutive msgs we processed
                if self.db_eq_process_consec_count > 100:
                    self.logger.info(f"DBEquity processed {self.db_eq_process_consec_count} msgs "
                                     f"in a row (in {now_t - self.dbento_eq_last_activity} secs)")
                self.db_eq_process_consec_count = 0
                # Tally up how many msgs we consume before processing the next msg
                self.db_eq_consume_consec_count += 1
                self.dbento_eq_last_activity = now_t

            # Iteration terminated unexpectedly. Restart feed.
            email_utils.send_alerts("PYFEED DATABENTO EQ DOWN", "Restarting.")
            raise ForceRestartFeed("DataBento EQ feed ended unexpectedly.")

        except asyncio.QueueFull:
            self.logger.error("Error DataBento EQ queue full!")
            email_utils.send_alerts("DATABENTO EQ QUEUE FULL", "Restarting.")
            raise # retry
        except ForceRestartFeed:
            raise # retry
        except Exception as e:
            self.logger.error(f"Error consuming DataBento EQ record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_db_eq_queue(self):
        """Process records from DataBento EQ in local queue"""
        try:
            while not self.eod_event.is_set():
                self.db_eq_count += 1
                # Get the next record from queue
                db_record = await self.dbento_eq_queue.get()
                await self.process_db_eq_record(db_record)
                # Let the queue know that we're done with the get()
                self.dbento_eq_queue.task_done()
                # Log and reset the number of consecutive msgs we consumed
                if self.db_eq_consume_consec_count > 100:
                    self.logger.info(f"DBEquity consumed {self.db_eq_consume_consec_count} msgs in a row")
                self.db_eq_consume_consec_count = 0
                # Tally up how many msgs we process before consuming the next msg
                self.db_eq_process_consec_count += 1
                # Make sure we yield every 10 messages even when not publishing
                if self.db_eq_process_consec_count % 10 == 0:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.logger.info("DataBento EQ processing loop cancelled")
            raise # no retry
        except ForceRestartFeed:
            raise # retry
        except Exception as e:
            self.logger.error(f"Error processing DataBento EQ record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_db_eq_record(self, db_record):
        """Process a single record from DataBento EQ"""
        if isinstance(db_record, CMBP1Msg):
            # Count the number of "real" messages we got
            self.db_eq_real_count += 1

            sym = self.idx_to_sym_eq.get(db_record.instrument_id)
            if sym is None:
                # Mapping might not have arrived yet; shouldn't happen.
                self.logger.warning(
                    "EQMBP1 without mapping: instrument_id={}".format(
                        db_record.instrument_id
                    )
                )
                return

            # Throttle: check if we should skip this message before doing any processing
            now_ms = int(time.time() * 1000)
            last_pub = self.eq_last_publish_time.get(sym, 0)
            if now_ms - last_pub < self.publish_interval_ms:
                return  # Skip this message, too soon since last publish

            # Skip if we're not in session.
            # Warning: don't skip other types of messages, esp. symbol mappings!
            if not self._is_in_db_eq_session(db_record.ts_event):
                return



            # We actually want to pass a midmsg back.

            # nano -> milli.
            ts_event_ms = int(db_record.ts_event * 1e-6)

            # Don't publish this if we have more up to date data
            if ts_event_ms < self.sym_last_publish_transact_time[sym]:
                return


            # Do some extra checking on db messages.

            if self.db_eq_bad_count >= 100:
                # Restart the db_eq feed. 
                self.db_eq_bad_count = 0
                email_utils.send_alerts("PYFEED DATABENTO_EQ DOWN", "Too many badbook exceptions. Restarting.")
                raise ForceRestartFeed("Got too many db_eq badbook exceptions, restarting")

            # This gets very chatty on the equities.
            if (db_record.price == db.UNDEF_PRICE or db_record.ts_event == db.UNDEF_TIMESTAMP or db_record.ts_recv == db.UNDEF_TIMESTAMP):
                return

            # This happens often, don't ping.
            if (db_record.levels[0].ask_px == db.UNDEF_PRICE or db_record.levels[0].bid_px == db.UNDEF_PRICE):
                return

            # | RF.F_MAYBE_BAD_BOOK
            if (db_record.flags & (RF.F_SNAPSHOT )):
                self.logger.warning("Got an unusable msg: {}".format(db_record))
                return

            if (db_record.flags & (RF.F_MAYBE_BAD_BOOK )):

                if self.eq_bad_book_start == 0:
                    self.eq_bad_book_start = int(time.time())


                if time.time() - self.eq_bad_book_start > self.MAX_BAD_BOOK_T:
                    # Keep on using it I guess.
                    pass
                else:
                    self.logger.warning("Got an unusable msg: {}".format(db_record))
                    self.db_eq_bad_count += 1
                    return
            else:
                self.eq_bad_book_start = 0

            if (db_record.flags & (RF.F_PUBLISHER_SPECIFIC)):
                # Log but don't return, something might be wrong but not sure.
                self.logger.warning("DB EQ, got unusual message: {}".format(db_record))

            # DB gives prices in units of 1e-9:
            # https://databento.com/docs/api-reference-live/basics/schemas-and-conventions?historical=python&live=python&reference=python
            px_convert = 1e-9

            px = abs(db_record.price * px_convert) # not used except for error check
            bb_px = round(db_record.levels[0].bid_px * px_convert, 9)
            ba_px = round(db_record.levels[0].ask_px * px_convert, 9)

            bid_sz = db_record.levels[0].bid_sz
            ask_sz = db_record.levels[0].ask_sz

            # Basic sanitizing, in case we get a heartbeat over a weekend maybe.
            # https://databento.com/docs/schemas-and-data-formats/mbp-1#fields-mbp-1?historical=python&live=python&reference=python
            # I don't think we get anything like this
            if px > 1e6 or bb_px > 1e6 or ba_px > 1e6:
                self.logger.warning("Got an unusable msg: {}".format(db_record))
                return

            #out_str = "{}: {} {} {} {} {} {} {} {} {} {} {}".format(now_ms, sym, ts_event_ms, ts_recv_ms, px, bb_px, ba_px, bid_sz, ask_sz, seqno, t_after_event, t_after_recv)
            # OK, create the pbmessage now.
            pbmsg = mdmsg_pb2.PbMessage()
            # PBMARKET_TOPBOOK_EQUITY
            pbmsg.market = 8
            pbmsg.symbol_id = sym

            pbquote = pbmsg.quote
            #pbquote.update_id = db_record.sequence
            pbquote.best_bid_px = str(bb_px)
            pbquote.best_bid_qty = str(bid_sz)
            pbquote.best_ask_px = str(ba_px)
            pbquote.best_ask_qty = str(ask_sz)
            pbquote.mid_px = round((bb_px + ba_px) / 2, 6)
            pbquote.transact_time = ts_event_ms

            # Publish it to the book.
            pub_string = pbmsg.SerializeToString()
            tot_delay_ms = now_ms - ts_event_ms
            queue_len = self.dbento_eq_queue.qsize()
            if self.db_eq_count % 3_000 == 0 or tot_delay_ms > 1_000 or queue_len > 100:
                ts_recv_ms = int(db_record.ts_recv * 1e-6)
                ts_out_ms = int(db_record.ts_out * 1e-6)
                recv_delay_ms = now_ms - ts_recv_ms
                ts_out_delay_ms = now_ms - ts_out_ms
                self.logger.info(
                    "{}: DBEquity {} msgtime {} tot_delay_ms {} recv_delay_ms {} ts_out_delay_ms {} queue_len {}".format(
                        time.time(), sym, ts_event_ms, tot_delay_ms, recv_delay_ms, ts_out_delay_ms, queue_len
                    )
                )

            # Kill feed if data is too stale (e.g., > 10s)
            if tot_delay_ms > self.STALE_THRESHOLD_MS:
                err_msg = (f"DBEquity {sym} delay {tot_delay_ms}ms exceeds "
                           f"{self.STALE_THRESHOLD_MS}ms threshold, forcing reconnect.")
                self.logger.error(err_msg)
                email_utils.send_alerts("DBEQUITY FEED STALE", err_msg)
                raise ForceRestartFeed(f"DBEquity feed stale: {sym} delay={tot_delay_ms}ms")

            self.sym_last_publish_transact_time[sym] = ts_event_ms
            self.eq_last_publish_time[sym] = now_ms
            await self.publish_async(pub_string)

        elif isinstance(db_record, SymbolMappingMsg):
            self.sym_to_idx_eq[db_record.stype_in_symbol] = db_record.instrument_id
            self.idx_to_sym_eq[db_record.instrument_id] = db_record.stype_in_symbol
            self.got_sym_map_db_eq = True
            self.logger.info(
                "EQSymbolMapping: {} -> {}".format(
                    db_record.stype_in_symbol, db_record.instrument_id
                )
            )

        elif isinstance(db_record, StatusMsg):
            sym = None
            if hasattr(db_record, "instrument_id"):
                sym = self.idx_to_sym_eq.get(db_record.instrument_id)
            if sym:
                self.logger.info("EQStatus: {} {}".format(sym, db_record))
            else:
                self.logger.info("EQStatus: {}".format(db_record))

        elif isinstance(db_record, ErrorMsg):
            sym = None
            if hasattr(db_record, "instrument_id"):
                sym = self.idx_to_sym_eq.get(db_record.instrument_id)
            if hasattr(db_record, "code") and db_record.code == ErrorCode.SKIPPED_RECORDS_AFTER_SLOW_READING:
                self.logger.warning(f"EQError: slow_reader_behavior=skip triggered, records were skipped: {db_record}")
            elif sym:
                self.logger.error("EQError: {} {}".format(sym, db_record))
            else:
                self.logger.error("EQError: {}".format(db_record))

        elif isinstance(db_record, SystemMsg):
            try:
                # Check for subscription confirmation
                if db_record.msg.startswith("Subscription") and db_record.msg.endswith("succeeded"):
                    self.subscribed_db_eq = True
                    self.logger.info(f"EQSystem subscription confirmed: {db_record}")
                else:
                    sym = self.idx_to_sym_eq.get(db_record.instrument_id)
                    self.logger.info("EQSystem: {} {}".format(sym, db_record))
            except AttributeError:
                self.logger.info("EQSystem: {}".format(db_record))

        else:
            self.logger.info(f"Unrecognized EQ db_record: {db_record}")

    ########################################################################################
    # Start DataBento BOATS (Blue Ocean ATS overnight US equities)
    #
    # Mirrors the DataBento EQ structure. Subscribes to OCEA.MEMOIR mbp-1 and publishes
    # to the same TopBookEquity protobuf market type as the EQ feed (sessions don't overlap:
    # EQ is 04:00-20:00 ET, BOATS is 20:00-04:00 ET Sun-Thu).

    @retry(wait=wait_fixed(1.5))
    async def init_databento_boats(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying DataBento BOATS feed")
            return
        if len(self.dbento_boats_syms) == 0:
            self.logger.info("Not subscribing to DataBento BOATS, returning")
            return

        # Reset the message queue
        self.dbento_boats_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        try:
            self.logger.info("DatabentoBOATS - connecting to {}".format(self.dbento_boats_syms))

            self.db_boats_client = db.Live(key=db_API_KEY, ts_out=True, slow_reader_behavior="skip")
            self.subscribed_db_boats = False
            self.got_sym_map_db_boats = False
            self.db_boats_client.subscribe(
                dataset="OCEA.MEMOIR",
                schema="mbp-1",
                stype_in="raw_symbol",
                symbols=self.dbento_boats_syms,
            )

            self.sym_to_idx_boats = {}
            self.idx_to_sym_boats = {}

            self.dbento_boats_last_activity = time.time()
            self.db_boats_count = 0
            self.db_boats_real_count = 0
            self.db_boats_consume_consec_count = 0
            self.db_boats_process_consec_count = 0
            self.db_boats_bad_count = 0

            heartbeat_task = asyncio.create_task(
                self._monitor_feed_heartbeat(
                    "DataBento BOATS",
                    "dbento_boats_last_activity",
                    self.databento_heartbeat_timeout_secs,
                    self.databento_heartbeat_check_secs,
                    session_guard=lambda: self._is_in_db_boats_session(
                        int(time.time() * 1_000_000_000)
                    ),
                    subscription_check_interval=5*60,
                    is_subscribed=lambda: self.subscribed_db_boats and self.got_sym_map_db_boats
                )
            )
            consume_task = asyncio.create_task(self.consume_db_boats_loop())
            process_task = asyncio.create_task(self.process_db_boats_queue())
            eod_task = asyncio.create_task(self.eod_event.wait())
            reload_task = asyncio.create_task(self._wait_reload(self.dbento_boats_reload_event))

            done, pending = await asyncio.wait(
                {heartbeat_task, consume_task, process_task, eod_task, reload_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            self.logger.info(f"DataBento BOATS feed stopped (done: {done}). Cancelling all tasks.")
            await self._cancel_pending(pending, "DataBento BOATS")

            self.logger.info("DataBento BOATS feed tasks cancelled. Raising task exception.")
            for task in done:
                if exc := task.exception():
                    raise exc

            self.logger.info("DataBento BOATS feed cancelled")
        except asyncio.CancelledError:
            self.logger.info("DataBento BOATS feed cancelled")
            raise
        except HeartbeatException as e:
            self.logger.error(f"DataBento BOATS heartbeat error: {e}")
            raise
        except ForceRestartFeed as e:
            self.logger.error(f"DataBento BOATS ForceRestartFeed: {e}")
            raise
        except Exception as e:
            self.logger.error(f"DataBento BOATS error: {e}")
            self.logger.error(traceback.format_exc())
            raise
        finally:
            await self._shutdown_databento_client(self.db_boats_client, "DataBento BOATS")
            self.db_boats_client = None
            self.dbento_boats_last_activity = None
            self.logger.info(f"DataBento BOATS client got {self.db_boats_count} messages today, "
                             f"{self.db_boats_real_count} of them real")

    async def consume_db_boats_loop(self):
        """Enqueues records from DataBento BOATS in async loop for later processing"""
        try:
            async for db_record in self.db_boats_client:
                if self.eod_event.is_set():
                    return
                # Don't skip out-of-session msgs here — symbol mappings can arrive out of hours.
                self.dbento_boats_queue.put_nowait(db_record)
                now_t = time.time()
                if self.db_boats_process_consec_count > 100:
                    self.logger.info(f"DBBoats processed {self.db_boats_process_consec_count} msgs "
                                     f"in a row (in {now_t - self.dbento_boats_last_activity} secs)")
                self.db_boats_process_consec_count = 0
                self.db_boats_consume_consec_count += 1
                self.dbento_boats_last_activity = now_t

            # Iteration terminated unexpectedly. Restart feed.
            email_utils.send_alerts("PYFEED DATABENTO BOATS DOWN", "Restarting.")
            raise ForceRestartFeed("DataBento BOATS feed ended unexpectedly.")

        except asyncio.QueueFull:
            self.logger.error("Error DataBento BOATS queue full!")
            email_utils.send_alerts("DATABENTO BOATS QUEUE FULL", "Restarting.")
            raise # retry
        except asyncio.CancelledError:
            self.logger.info("DataBento BOATS consume loop cancelled")
            raise
        except ForceRestartFeed:
            raise
        except Exception as e:
            self.logger.error(f"Error consuming DataBento BOATS record: {e}")
            self.logger.error(traceback.format_exc())
            raise

    async def process_db_boats_queue(self):
        """Process records from DataBento BOATS in local queue"""
        try:
            while not self.eod_event.is_set():
                self.db_boats_count += 1
                db_record = await self.dbento_boats_queue.get()
                await self.process_db_boats_record(db_record)
                self.dbento_boats_queue.task_done()
                if self.db_boats_consume_consec_count > 100:
                    self.logger.info(f"DBBoats consumed {self.db_boats_consume_consec_count} msgs in a row")
                self.db_boats_consume_consec_count = 0
                self.db_boats_process_consec_count += 1
                if self.db_boats_process_consec_count % 10 == 0:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.logger.info("DataBento BOATS processing loop cancelled")
            raise
        except ForceRestartFeed:
            raise
        except Exception as e:
            self.logger.error(f"Error processing DataBento BOATS record: {e}")
            self.logger.error(traceback.format_exc())
            raise

    async def process_db_boats_record(self, db_record):
        """Process a single record from DataBento BOATS (OCEA.MEMOIR mbp-1 → MBP1Msg)."""
        if isinstance(db_record, MBP1Msg):
            self.db_boats_real_count += 1

            sym = self.idx_to_sym_boats.get(db_record.instrument_id)
            if sym is None:
                self.logger.warning(
                    "BOATSMBP1 without mapping: instrument_id={}".format(
                        db_record.instrument_id
                    )
                )
                return

            now_ms = int(time.time() * 1000)
            last_pub = self.eq_last_publish_time.get(sym, 0)
            if now_ms - last_pub < self.publish_interval_ms:
                return

            # Skip if we're not in a BOATS overnight session window.
            if not self._is_in_db_boats_session(db_record.ts_event):
                return

            ts_event_ms = int(db_record.ts_event * 1e-6)

            # Don't publish this if we have more up-to-date data on this symbol already.
            if ts_event_ms < self.sym_last_publish_transact_time[sym]:
                return

            if self.db_boats_bad_count >= 100:
                self.db_boats_bad_count = 0
                email_utils.send_alerts("PYFEED DATABENTO_BOATS DOWN", "Too many badbook exceptions. Restarting.")
                raise ForceRestartFeed("Got too many db_boats badbook exceptions, restarting")

            if (db_record.price == db.UNDEF_PRICE or db_record.ts_event == db.UNDEF_TIMESTAMP or db_record.ts_recv == db.UNDEF_TIMESTAMP):
                return

            if (db_record.levels[0].ask_px == db.UNDEF_PRICE or db_record.levels[0].bid_px == db.UNDEF_PRICE):
                return

            if (db_record.flags & (RF.F_SNAPSHOT)):
                self.logger.warning("BOATS got an unusable msg: {}".format(db_record))
                return

            if (db_record.flags & (RF.F_MAYBE_BAD_BOOK)):
                if self.boats_bad_book_start == 0:
                    self.boats_bad_book_start = int(time.time())
                if time.time() - self.boats_bad_book_start > self.MAX_BAD_BOOK_T:
                    pass
                else:
                    self.logger.warning("BOATS got an unusable msg: {}".format(db_record))
                    self.db_boats_bad_count += 1
                    return
            else:
                self.boats_bad_book_start = 0

            if (db_record.flags & (RF.F_PUBLISHER_SPECIFIC)):
                self.logger.warning("DB BOATS, got unusual message: {}".format(db_record))

            px_convert = 1e-9
            px = abs(db_record.price * px_convert)
            bb_px = round(db_record.levels[0].bid_px * px_convert, 9)
            ba_px = round(db_record.levels[0].ask_px * px_convert, 9)
            bid_sz = db_record.levels[0].bid_sz
            ask_sz = db_record.levels[0].ask_sz

            if px > 1e6 or bb_px > 1e6 or ba_px > 1e6:
                self.logger.warning("BOATS got an unusable msg: {}".format(db_record))
                return

            pbmsg = mdmsg_pb2.PbMessage()
            # PBMARKET_TOPBOOK_EQUITY (same market type as XNAS path — strategy sees a unified feed)
            pbmsg.market = 8
            pbmsg.symbol_id = sym

            pbquote = pbmsg.quote
            pbquote.best_bid_px = str(bb_px)
            pbquote.best_bid_qty = str(bid_sz)
            pbquote.best_ask_px = str(ba_px)
            pbquote.best_ask_qty = str(ask_sz)
            pbquote.mid_px = round((bb_px + ba_px) / 2, 6)
            pbquote.transact_time = ts_event_ms

            pub_string = pbmsg.SerializeToString()
            tot_delay_ms = now_ms - ts_event_ms
            queue_len = self.dbento_boats_queue.qsize()
            if self.db_boats_count % 3_000 == 0 or tot_delay_ms > 1_000 or queue_len > 100:
                ts_recv_ms = int(db_record.ts_recv * 1e-6)
                ts_out_ms = int(db_record.ts_out * 1e-6)
                recv_delay_ms = now_ms - ts_recv_ms
                ts_out_delay_ms = now_ms - ts_out_ms
                self.logger.info(
                    "{}: DBBoats {} msgtime {} tot_delay_ms {} recv_delay_ms {} ts_out_delay_ms {} queue_len {}".format(
                        time.time(), sym, ts_event_ms, tot_delay_ms, recv_delay_ms, ts_out_delay_ms, queue_len
                    )
                )

            # No per-message stale-data check: BOATS overnight has long quiet stretches.
            # The heartbeat monitor covers genuine feed-down.

            self.sym_last_publish_transact_time[sym] = ts_event_ms
            self.eq_last_publish_time[sym] = now_ms
            await self.publish_async(pub_string)

        elif isinstance(db_record, SymbolMappingMsg):
            self.sym_to_idx_boats[db_record.stype_in_symbol] = db_record.instrument_id
            self.idx_to_sym_boats[db_record.instrument_id] = db_record.stype_in_symbol
            self.got_sym_map_db_boats = True
            self.logger.info(
                "BOATSSymbolMapping: {} -> {}".format(
                    db_record.stype_in_symbol, db_record.instrument_id
                )
            )

        elif isinstance(db_record, StatusMsg):
            sym = None
            if hasattr(db_record, "instrument_id"):
                sym = self.idx_to_sym_boats.get(db_record.instrument_id)
            if sym:
                self.logger.info("BOATSStatus: {} {}".format(sym, db_record))
            else:
                self.logger.info("BOATSStatus: {}".format(db_record))

        elif isinstance(db_record, ErrorMsg):
            sym = None
            if hasattr(db_record, "instrument_id"):
                sym = self.idx_to_sym_boats.get(db_record.instrument_id)
            if hasattr(db_record, "code") and db_record.code == ErrorCode.SKIPPED_RECORDS_AFTER_SLOW_READING:
                self.logger.warning(f"BOATSError: slow_reader_behavior=skip triggered, records were skipped: {db_record}")
            elif sym:
                self.logger.error("BOATSError: {} {}".format(sym, db_record))
            else:
                self.logger.error("BOATSError: {}".format(db_record))

        elif isinstance(db_record, SystemMsg):
            try:
                if db_record.msg.startswith("Subscription") and db_record.msg.endswith("succeeded"):
                    self.subscribed_db_boats = True
                    self.logger.info(f"BOATSSystem subscription confirmed: {db_record}")
                else:
                    sym = self.idx_to_sym_boats.get(db_record.instrument_id)
                    self.logger.info("BOATSSystem: {} {}".format(sym, db_record))
            except AttributeError:
                self.logger.info("BOATSSystem: {}".format(db_record))

        else:
            self.logger.info(f"Unrecognized BOATS db_record: {db_record}")

    ########################################################################################
    # Start PolygonEq

    @retry(wait=wait_fixed(1.5))
    async def init_polygon_eq(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying Polygon EQ feed")
            return
        if len(self.polygon_eq_syms) == 0:
            self.logger.info("Not subscribing to Polygon EQ, returning")
            return

        heartbeat_task = None
        consume_task = None
        process_task = None
        # Reset the message queue
        self.polygon_eq_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        # Flag to tell callback to raise exception when trying to shutdown polygon because the
        # client ignores everything and keeps delivering messages even when cancelled and closed
        self.polygon_eq_shutdown = False
        try:
            self.logger.info("PolygonEq - connecting to {}".format(self.polygon_eq_syms))

            # Create WebSocket client
            self.polygon_ws_client = WebSocketClient(
                api_key=polygon_API_KEY,
                feed=Feed.RealTime,
                # feed=Feed.Delayed, # for testing with lower tier
                market=Market.Stocks
            )

            # Subscribe to different message types based on configuration
            subscriptions = []
            for sym in self.polygon_eq_syms:
                subscriptions.append(f"Q.{sym}")  # Quotes
                # Don't deal with trades since we're not using the off hour vwap right now.
                # Hopefully speeds us up.
                #subscriptions.append(f"T.{sym}")  # Trades
                #subscriptions.append(f"A.{sym}") # Aggs, for testing with lower tier

            self.logger.info(f"Subscribing to: {subscriptions}")
            self.polygon_ws_client.subscribe(*subscriptions)

            self.polygon_last_activity = time.time()
            self.polygon_count = 0
            self.polygon_real_count = 0
            self.polygon_consec_count = 0

            heartbeat_task = asyncio.create_task(
                self._monitor_feed_heartbeat(
                    "Polygon EQ",
                    "polygon_last_activity",
                    self.polygon_heartbeat_timeout_secs,
                    self.polygon_heartbeat_check_secs,
                    session_guard=lambda: self._is_in_polygon_eq_session(
                        int(time.time() * 1_000_000_000)
                    )
                )
            )

            consume_task = asyncio.create_task(self.polygon_ws_client.connect(self.consume_polygon_eq_cb))
            process_task = asyncio.create_task(self.process_polygon_eq_queue())
            eod_task = asyncio.create_task(self.eod_event.wait())

            done, pending = await asyncio.wait(
                {heartbeat_task, consume_task, process_task, eod_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            self.logger.info(f"Polygon EQ feed stopped (done: {done}). Cancelling all tasks.")
            # We need to explicitly shut down the client here because canceling the consume task
            # gets ignored by the client. Doesn't hurt to shutdown twice.
            await self._shutdown_polygon_client(self.polygon_ws_client)
            self.polygon_eq_shutdown = True
            self.polygon_ws_client = None
            await self._cancel_pending(pending, "Polygon EQ")

            self.logger.info("Polygon EQ feed tasks cancelled. Raising task exception.")
            for task in done:
                if exc := task.exception():
                    raise exc

            self.logger.info("Polygon EQ feed cancelled")
        except asyncio.CancelledError:
            self.logger.info("Polygon EQ feed cancelled")
            # CancelledError is a BaseException, not Exception, so it won't trigger the retry.
            # We get this on a Ctrl-C event, so that is what we want.
            raise
        except websockets.exceptions.ConnectionClosedError as e:
            error_msg = f"Polygon WebSocket connection closed: {e}"
            stack_trace = traceback.format_exc()
            self.logger.error(error_msg)
            self.logger.error(stack_trace)
            email_utils.send_alerts("PYFEED POLYGON DOWN", stack_trace)
            raise
        except HeartbeatException as e:
            self.logger.error(f"Polygon EQ heartbeat error: {e}")
            raise
        except ForceRestartFeed as e:
            # any alerts should already be sent by raiser
            self.logger.error(f"Polygon EQ ForceRestartFeed: {e}")
            raise # retry
        except Exception as e:
            error_msg = f"Polygon EQ error: {e}"
            stack_trace = traceback.format_exc()
            self.logger.error(error_msg)
            self.logger.error(stack_trace)
            email_utils.send_alerts("PYFEED POLYGON DOWN", stack_trace)
            raise
        finally:
            await self._shutdown_polygon_client(self.polygon_ws_client)
            self.polygon_last_activity = None
            self.polygon_ws_client = None
            self.logger.info(f"Polygon client got {self.polygon_count} messages today, "
                             f"{self.polygon_real_count} of them real")

    # Must be async bc Polygon (when using async connect instead of sync run) awaits callbacks
    async def consume_polygon_eq_cb(self, msgs: List[WebSocketMessage]):
        """Enqueues messages from Polygon for later processing"""
        try:
            # Check for shutdown signal
            if self.eod_event.is_set():
                return
            if self.polygon_eq_shutdown:
                # This is the only way to kill the consume task when another task wants a restart.
                self.logger.error("Killing Polygon EQ consume task.")
                raise ForceRestartFeed("Killing Polygon EQ consume task.")
            self.polygon_last_activity = time.time()
            # If we're getting a message from outside market hours,
            # just dont handle the entire batch. This should
            # reduce our average checks.
            ts_event_ns = int(time.time() * 1_000_000_000)
            # Note: safe to skip here because Polygon doesn't have any pre-processing messages like
            # DataBento's symbol mapping messages that we need to process out-of-session.
            if not self._is_in_polygon_eq_session(ts_event_ns):
                return
            # Enqueue the whole list of messages without splitting them up into separate awaits.
            # This avoids interleaved messages from different callbacks.
            for msg in msgs:
                self.polygon_eq_queue.put_nowait(msg)
            # Tally up how many msgs we consume before processing the next msg
            self.polygon_consec_count += len(msgs)
        except asyncio.QueueFull:
            self.logger.error("Error Polygon EQ queue full!")
            email_utils.send_alerts("POLYGON EQ QUEUE FULL", "Restarting.")
            raise # retry
        except Exception as e:
            self.logger.error(f"Error consuming Polygon record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_polygon_eq_queue(self):
        """Process messages from Polygon in local queue"""
        try:
            while not self.eod_event.is_set():
                self.polygon_count += 1
                # Get the next message from queue
                msg = await self.polygon_eq_queue.get()
                await self.process_polygon_eq_msg(msg)
                # Let the queue know that we're done with the get()
                self.polygon_eq_queue.task_done()
                # Log and reset the number of consecutive msgs we consumed
                if self.polygon_consec_count > 100:
                    self.logger.info(f"Polygon got {self.polygon_consec_count} msgs in a row")
                self.polygon_consec_count = 0
        except asyncio.CancelledError:
            self.logger.info("Polygon EQ processing loop cancelled")
            raise # no retry
        except ForceRestartFeed:
            raise # retry
        except Exception as e:
            self.logger.error(f"Error processing Polygon record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_polygon_eq_msg(self, msg):
        now_ms = int(time.time() * 1_000)
        # Handle different message types
        # Example/docs here:
        # https://massive.com/docs/websocket/stocks/quotes
        if isinstance(msg, EquityQuote):
            # Count the number of "real" messages we got
            self.polygon_real_count += 1

            sym = msg.symbol

            # Convert timestamp to nanoseconds for session check
            # I'm just gonna use current time.

            last_pub = self.eq_last_publish_time.get(sym, 0)
            if now_ms - last_pub < self.publish_interval_ms:
                return  # Skip this message, too soon since last publish

            # Stamp it with the time they gave us...
            ts_event_ms = msg.timestamp

            # Don't publish this if we have more up to date data
            if ts_event_ms < self.sym_last_publish_transact_time[sym]:
                return


            # Create protobuf message
            pbmsg = mdmsg_pb2.PbMessage()
            pbmsg.market = 8  # TOPBOOK_EQUITY

            pbmsg.symbol_id = sym

            pbquote = pbmsg.quote
            # Start of day can be one-sided
            if not all((msg.bid_price, msg.ask_price)):
                return
            pbquote.best_bid_px = str(msg.bid_price)
            pbquote.best_bid_qty = "1"
            pbquote.best_ask_px = str(msg.ask_price)
            pbquote.best_ask_qty = "1"
            pbquote.mid_px = round((msg.bid_price + msg.ask_price) / 2, 6)

            pbquote.transact_time = ts_event_ms

            pub_string = pbmsg.SerializeToString()

            self.sym_last_publish_transact_time[sym] = ts_event_ms
            self.eq_last_publish_time[sym] = now_ms
            await self.publish_async(pub_string)

            recv_delay_ms = int(now_ms) - int(ts_event_ms)
            queue_len = self.polygon_eq_queue.qsize()
            if self.polygon_count % 300 == 0 or recv_delay_ms > 1000 or queue_len > 100:
                self.logger.info(
                    "{}: Polygon {} msgtime {} delay_ms {} queue_len {}".format(
                        time.time(), sym, ts_event_ms, recv_delay_ms, queue_len
                    )
                )

            # Kill feed if data is too stale (e.g., > 10s)
            if recv_delay_ms > self.STALE_THRESHOLD_MS:
                err_msg = (f"Polygon EQ {sym} delay {recv_delay_ms}ms exceeds "
                           f"{self.STALE_THRESHOLD_MS}ms threshold, forcing reconnect.")
                self.logger.error(err_msg)
                email_utils.send_alerts("POLYGON EQ FEED STALE", err_msg)
                raise ForceRestartFeed(f"Polygon EQ feed stale: {sym} delay={recv_delay_ms}ms")

        elif isinstance(msg, EquityAgg):
            sym = msg.symbol if hasattr(msg, 'symbol') else 'UNKNOWN'


            # Aggregates contain OHLCV data - convert to trade using close price
            # This is given in ms btw.

            # For our internal timing
            ts_event_ms = msg.start_timestamp
            # Just for time check
            ts_event_ns = msg.start_timestamp * 1_000_000

            # Create trade message from aggregate close price
            pbmsg = mdmsg_pb2.PbMessage()
            pbmsg.market = 8  # TOPBOOK_EQUITY
            pbmsg.symbol_id = sym

            msg_trd = pbmsg.book_trade
            msg_trd.px = str(msg.vwap)
            msg_trd.qty = str(msg.volume)
            msg_trd.trade_time = ts_event_ms

            pub_string = pbmsg.SerializeToString()
            await self.publish_async(pub_string)

            if self.polygon_count % 300 == 0:
                self.logger.info(f"Polygon Agg: {sym} volume={msg.volume} px={msg.vwap}")

        elif isinstance(msg, EquityTrade):
            # Since we're not doing off hour vwap now,
            # let's not propagate trades either.
            sym = msg.symbol

            ts_event_ns = msg.timestamp * 1_000_000 if hasattr(msg, 'timestamp') else int(time.time() * 1_000_000_000)

            last_pub = self.eq_last_publish_time.get(sym, 0)
            if now_ms - last_pub < self.publish_interval_ms:
                return  # Skip this message, too soon since last publish

            # Create trade message
            pbmsg = mdmsg_pb2.PbMessage()
            pbmsg.market = 8  # TOPBOOK_EQUITY
            pbmsg.symbol_id = sym

            msg_trd = pbmsg.book_trade
            msg_trd.px = str(msg.price if hasattr(msg, 'price') else 0)
            msg_trd.qty = str(msg.size if hasattr(msg, 'size') else 0)
            msg_trd.trade_time = int(ts_event_ns / 1_000_000)  # Convert ns to ms

            pub_string = pbmsg.SerializeToString()
            self.eq_last_publish_time[sym] = now_ms

            await self.publish_async(pub_string)

        else:
            self.logger.debug(
                "Polygon message: %s", getattr(msg, "__dict__", msg)
            )


    ########################################################################################
    # End PolygonEq


    ########################################################################################
    # Start PolygonFX

    @retry(wait=wait_fixed(1.5))
    async def init_polygonfx(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying Polygon FX feed")
            return
        if len(self.polygonfx_syms) == 0:
            self.logger.info("Not subscribing to PolygonFX, returning")
            return

        heartbeat_task = None
        consume_task = None
        process_task = None
        # Reset the message queue
        self.polygonfx_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        # Flag to tell callback to raise exception when trying to shutdown polygon because the
        # client ignores everything and keeps delivering messages even when cancelled and closed
        self.polygonfx_shutdown = False
        try:
            self.logger.info("PolygonFx - connecting to {}".format(self.polygonfx_syms))

            # Create WebSocket client
            self.polygonfx_ws_client = WebSocketClient(
                api_key=polygonfx_API_KEY,
                feed=Feed.RealTime,
                # feed=Feed.Delayed, # for testing with lower tier
                market=Market.Forex
            )

            # Subscribe to different message types based on configuration
            subscriptions = []
            for sym in self.polygonfx_syms:
                sym_name = polygonfx_names_to_symbols.get(sym)
                if not sym_name:
                    self.logger.error(f"No PolygonFX mapping for symbol {sym}, skipping")
                    continue
                subscriptions.append(sym_name)

            if not subscriptions:
                self.logger.warning("No valid PolygonFX symbols configured, skipping connect")
                return

            self.logger.info(f"Subscribing to: {subscriptions}")
            self.polygonfx_ws_client.subscribe(*subscriptions)

            self.polygonfx_last_activity = time.time()
            self.polygonfx_count = 0
            self.polygonfx_real_count = 0
            self.polygonfx_consec_count = 0

            heartbeat_task = asyncio.create_task(
                self._monitor_feed_heartbeat(
                    "Polygon FX",
                    "polygonfx_last_activity",
                    self.polygonfx_heartbeat_timeout_secs,
                    self.polygonfx_heartbeat_check_secs,
                    session_guard=lambda: self._is_in_polygonfx_session(
                        int(time.time() * 1_000_000_000)
                    )
                )
            )

            consume_task = asyncio.create_task(self.polygonfx_ws_client.connect(self.consume_polygonfx_cb))
            process_task = asyncio.create_task(self.process_polygonfx_queue())
            eod_task = asyncio.create_task(self.eod_event.wait())

            done, pending = await asyncio.wait(
                {heartbeat_task, consume_task, process_task, eod_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            self.logger.info(f"Polygonfx feed stopped (done: {done}). Cancelling all tasks.")
            # We need to explicitly shut down the client here because canceling the consume task
            # gets ignored by the client. Doesn't hurt to shutdown twice.
            self.polygonfx_shutdown = True
            await self._shutdown_polygonfx_client(self.polygonfx_ws_client)
            self.polygonfx_ws_client = None
            await self._cancel_pending(pending, "Polygonfx")

            self.logger.info("Polygonfx feed tasks cancelled. Raising task exception.")
            for task in done:
                if exc := task.exception():
                    raise exc

            self.logger.info("Polygonfx feed cancelled")
        except asyncio.CancelledError:
            self.logger.info("Polygonfx feed cancelled")
            # CancelledError is a BaseException, not Exception, so it won't trigger the retry.
            # We get this on a Ctrl-C event, so that is what we want.
            raise
        except websockets.exceptions.ConnectionClosedError as e:
            error_msg = f"Polygonfx WebSocket connection closed: {e}"
            stack_trace = traceback.format_exc()
            self.logger.error(error_msg)
            self.logger.error(stack_trace)
            email_utils.send_alerts("PYFEED POLYGONfx DOWN", stack_trace)
            raise
        except HeartbeatException as e:
            self.logger.error(f"PolygonFX heartbeat error: {e}")
            raise
        except ForceRestartFeed as e:
            # any alerts should already be sent by raiser
            self.logger.error(f"Polygonfx ForceRestartFeed: {e}")
            raise # retry
        except Exception as e:
            error_msg = f"Polygonfx error: {e}"
            stack_trace = traceback.format_exc()
            self.logger.error(error_msg)
            self.logger.error(stack_trace)
            email_utils.send_alerts("PYFEED POLYGONfx DOWN", stack_trace)
            raise
        finally:
            await self._shutdown_polygonfx_client(self.polygonfx_ws_client)
            self.polygonfx_last_activity = None
            self.polygonfx_ws_client = None
            self.logger.info(f"Polygonfx client got {self.polygonfx_count} messages today, "
                             f"{self.polygonfx_real_count} of them real")

    # Must be async bc Polygon (when using async connect instead of sync run) awaits callbacks
    async def consume_polygonfx_cb(self, msgs: List[WebSocketMessage]):
        """Enqueues messages from Polygonfx for later processing"""
        try:
            # Check for shutdown signal
            if self.eod_event.is_set():
                return
            if self.polygonfx_shutdown:
                # This is the only way to kill the consume task when another task wants a restart.
                self.logger.error("Killing Polygonfx consume task.")
                raise ForceRestartFeed("Killing Polygonfx consume task.")
            self.polygonfx_last_activity = time.time()          
            # If we're getting a message from outside market hours,
            # just dont handle the entire batch. This should
            # reduce our average checks.
            ts_event_ns = int(time.time() * 1_000_000_000)
            
            # Note: safe to skip here because Polygon doesn't have any pre-processing messages like
            # DataBento's symbol mapping messages that we need to process out-of-session.
            if not self._is_in_polygonfx_session(ts_event_ns):
                return

            # Enqueue the whole list of messages without splitting them up into separate awaits.
            # This avoids interleaved messages from different callbacks.
            for msg in msgs:
                self.polygonfx_queue.put_nowait(msg)

            # Tally up how many msgs we consume before processing the next msg
            self.polygonfx_consec_count += len(msgs)
        except asyncio.QueueFull:
            self.logger.error("Error Polygon FX queue full!")
            email_utils.send_alerts("POLYGON FX QUEUE FULL", "Restarting.")
            raise # retry
        except Exception as e:
            self.logger.error(f"Error consuming Polygonfx record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_polygonfx_queue(self):
        """Process messages from Polygonfx in local queue"""
        try:
            while not self.eod_event.is_set():
                self.polygonfx_count += 1
                # Get the next message from queue
                msg = await self.polygonfx_queue.get()
                await self.process_polygonfx_msg(msg)
                # Let the queue know that we're done with the get()
                self.polygonfx_queue.task_done()
                # Log and reset the number of consecutive msgs we consumed
                if self.polygonfx_consec_count > 100:
                    self.logger.info(f"Polygonfx got {self.polygonfx_consec_count} msgs in a row")
                self.polygonfx_consec_count = 0
        except asyncio.CancelledError:
            self.logger.info("Polygonfx processing loop cancelled")
            raise # no retry
        except ForceRestartFeed:
            raise # retry
        except Exception as e:
            self.logger.error(f"Error processing Polygonfx record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_polygonfx_msg(self, msg):
        now_ms = int(time.time() * 1_000)
        # Handle different message types
        # Example/docs here:
        if isinstance(msg, ForexQuote):
            # Count the number of "real" messages we got
            self.polygonfx_real_count += 1

            # Translate Polygon pair (e.g. C.XAUUSD) back to feed symbol (e.g. GOLD)
            pair_name = msg.pair
            sym = polygonfx_symbols_to_names.get(pair_name)
            if not sym:
                self.logger.warning(f"Polygonfx symbol {pair_name} not in config, skipping")
                return

            last_pub = self.polygonfx_last_publish_time.get(sym, 0)
            if now_ms - last_pub < self.publish_interval_ms:
                return  # Skip this message, too soon since last publish

            # Stamp it with the time they gave us...
            ts_event_ms = msg.timestamp
            if ts_event_ms < self.sym_last_publish_transact_time[sym]:
                return

            # Create protobuf message
            pbmsg = mdmsg_pb2.PbMessage()
            pbmsg.market = 8  # TOPBOOK_EQUITY

            pbmsg.symbol_id = sym

            pbquote = pbmsg.quote
            # Start of day can be one-sided

            pbquote.best_bid_px = str(msg.bid_price)
            pbquote.best_bid_qty = "1"
            pbquote.best_ask_px = str(msg.ask_price)
            pbquote.best_ask_qty =  "1"
            pbquote.mid_px = round((msg.bid_price + msg.ask_price) / 2, 6)



            pbquote.transact_time = ts_event_ms

            pub_string = pbmsg.SerializeToString()

            self.sym_last_publish_transact_time[sym] = ts_event_ms
            self.polygonfx_last_publish_time[sym] = now_ms
            await self.publish_async(pub_string)

            recv_delay_ms = int(now_ms) - int(ts_event_ms)
            queue_len = self.polygonfx_queue.qsize()
            if self.polygonfx_count % 300 == 0 or recv_delay_ms > 3000 or queue_len > 100:
                self.logger.info(
                    "{}: Polygonfx {} msgtime {} delay_ms {} queue_len {}".format(
                        time.time(), sym, ts_event_ms, recv_delay_ms, queue_len
                    )
                )

            # Kill feed if data is too stale (e.g., > 10s)
            if recv_delay_ms > self.STALE_THRESHOLD_MS:
                err_msg = (f"PolygonFX {sym} delay {recv_delay_ms}ms exceeds "
                           f"{self.STALE_THRESHOLD_MS}ms threshold, forcing reconnect.")
                self.logger.error(err_msg)
                email_utils.send_alerts("POLYGONFX FEED STALE", err_msg)
                raise ForceRestartFeed(f"PolygonFX feed stale: {sym} delay={recv_delay_ms}ms")

        else:
            self.logger.debug(
                "Polygonfx message: %s", getattr(msg, "__dict__", msg)
            )


    ########################################################################################
    # End FX



    # Not sure yet if I can do two different clients, but feels like I might need to.
    # @retry(wait=wait_exponential(multiplier=1, min=2, max=120), reraise=True)
    @retry(wait=wait_fixed(1.5))
    async def init_databento_cme(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying DataBento CME feed")
            return
        if len(self.dbento_cme_syms) == 0:
            self.logger.info("Not subscribing to Databento CME, returning")
            return

        # Reset the message queue
        self.dbento_cme_queue = asyncio.LifoQueue(maxsize=self.MAX_QUEUE_LEN)
        try:
            self.logger.info("DatabentoCME - connecting to {}".format(self.dbento_cme_syms))
            self.db_client_cme = db.Live(key=db_API_KEY, ts_out=True, slow_reader_behavior="skip")
            self.subscribed_db_cme = False # whether we got a subscription confirmation
            self.got_sym_map_db_cme = False # whether we got at least 1 symbol mappings
            # TODO: let this be customizeable.
            self.db_client_cme.subscribe(
                dataset="GLBX.MDP3",
                schema="mbp-1",
                stype_in="raw_symbol",
                #stype_in="continuous",
                symbols=self.dbento_cme_syms,
            )

            # self.db_client_cme.add_callback(self.consume_db_cme_cb)

            self.sym_to_idx_cme = {}
            self.idx_to_sym_cme = {}

            # self.db_client_cme.start()
            self.dbento_cme_last_activity = time.time()
            self.db_cme_count = 0
            self.db_cme_real_count = 0
            self.db_cme_consume_consec_count = 0
            self.db_cme_process_consec_count = 0
            # Count how many bad messages we get. 
            # If we get a lot of them, restart. 
            self.db_cme_bad_count = 0

            heartbeat_task = asyncio.create_task(
                self._monitor_feed_heartbeat(
                    "DataBento CME",
                    "dbento_cme_last_activity",
                    self.databento_heartbeat_timeout_secs,
                    self.databento_heartbeat_check_secs,
                    session_guard=lambda: self._is_in_cme_session(
                        int(time.time() * 1_000_000_000)
                    ),
                    subscription_check_interval=5*60,
                    is_subscribed=lambda: self.subscribed_db_cme and self.got_sym_map_db_cme
                )
            )
            consume_task = asyncio.create_task(self.consume_db_cme_loop())
            process_task = asyncio.create_task(self.process_db_cme_queue())
            eod_task = asyncio.create_task(self.eod_event.wait())
            reload_task = asyncio.create_task(self._wait_reload(self.dbento_cme_reload_event))

            done, pending = await asyncio.wait(
                {heartbeat_task, consume_task, process_task, eod_task, reload_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel all pending tasks and wait for cancellations to complete
            self.logger.info(f"DataBento CME feed stopped (done: {done}). Cancelling all tasks.")
            await self._cancel_pending(pending, "DataBento CME")

            self.logger.info("DataBento CME feed tasks cancelled. Raising task exception.")
            for task in done:
                if exc := task.exception():
                    raise exc

            self.logger.info("DataBento CME feed cancelled")
        except asyncio.CancelledError:
            self.logger.info("DataBento CME feed cancelled")
            # CancelledError is a BaseException, not Exception, so it won't trigger the retry.
            # We get this on a Ctrl-C event, so that is what we want.
            raise
        except HeartbeatException as e:
            self.logger.error(f"DataBento CME heartbeat error: {e}")
            raise
        except ForceRestartFeed as e:
            # any alerts should already be sent by raiser
            self.logger.error(f"DataBento CME ForceRestartFeed: {e}")
            raise # retry
        except Exception as e:
            self.logger.error(f"DataBento CME error: {e}")
            self.logger.error(traceback.format_exc())
            raise  # This will trigger the @retry decorator
        finally:
            await self._shutdown_databento_client(self.db_client_cme, "DataBento CME")
            self.db_client_cme = None
            self.dbento_cme_last_activity = None
            self.logger.info(f"DataBento CME client got {self.db_cme_count} messages today, "
                             f"{self.db_cme_real_count} of them real")

    async def consume_db_cme_loop(self):
        """Enqueues records from DataBento CME in async loop for later processing"""
        try:
            # This is the way to get async "callbacks" from DataBento. Automatically starts the
            # client, so don't call start() elsewhere, and no need to wait_for_close() either.
            async for db_record in self.db_client_cme:
                if self.eod_event.is_set():
                    return
                # Warning: do NOT skip just because we're not in session! We need the symbol
                # mappings, which are sent when we start up and could arrive out of hours.
                self.dbento_cme_queue.put_nowait(db_record)
                # asyncio.Queue is not thread-safe so we need to do this wrapper
                # self.dbento_cme_queue.put_nowait(db_record)
                # asyncio.run_coroutine_threadsafe(self.dbento_cme_queue.put(db_record), self._loop)
                now_t = time.time()
                # Log and reset the number of consecutive msgs we processed
                if self.db_cme_process_consec_count > 100:
                    self.logger.info(f"CME processed {self.db_cme_process_consec_count} msgs "
                                     f"in a row (in {now_t - self.dbento_cme_last_activity} secs)")
                self.db_cme_process_consec_count = 0
                # Tally up how many msgs we consume before processing the next msg
                self.db_cme_consume_consec_count += 1
                self.dbento_cme_last_activity = now_t

            # Iteration terminated unexpectedly. Restart feed.
            email_utils.send_alerts("PYFEED DATABENTO CME DOWN", "Restarting.")
            raise ForceRestartFeed("DataBento CME feed ended unexpectedly.")

        except asyncio.QueueFull:
            self.logger.error("Error DataBento CME queue full!")
            email_utils.send_alerts("DATABENTO CME QUEUE FULL", "Restarting.")
            raise # retry
        except ForceRestartFeed:
            raise # retry
        except Exception as e:
            self.logger.error(f"Error consuming DataBento CME record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_db_cme_queue(self):
        """Process records from DataBento CME in local queue"""
        try:
            while not self.eod_event.is_set():
                self.db_cme_count += 1
                # Get the next record from queue
                db_record = await self.dbento_cme_queue.get()
                await self.process_db_cme_record(db_record)
                # Let the queue know that we're done with the get()
                self.dbento_cme_queue.task_done()
                # Log and reset the number of consecutive msgs we consumed
                if self.db_cme_consume_consec_count > 100:
                    self.logger.info(f"CME consumed {self.db_cme_consume_consec_count} msgs in a row")
                self.db_cme_consume_consec_count = 0
                # Tally up how many msgs we process before consuming the next msg
                self.db_cme_process_consec_count += 1
                # Make sure we yield every 10 messages even when not publishing
                if self.db_cme_process_consec_count % 10 == 0:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.logger.info("DataBento CME processing loop cancelled")
            raise # no retry
        except ForceRestartFeed:
            raise # retry
        except Exception as e:
            self.logger.error(f"Error processing DataBento CME record: {e}")
            self.logger.error(traceback.format_exc())
            raise # retry

    async def process_db_cme_record(self, db_record):
        if isinstance(db_record, MBP1Msg):
            # Count the number of "real" messages we got
            self.db_cme_real_count += 1

            # This is the databento sym, something like "ES.v.0"
            sym = self.idx_to_sym_cme.get(db_record.instrument_id)
            if sym is None:
                # Mapping might not have arrived yet; shouldn't happen.
                self.logger.warning(
                    "CMEMBP1 without mapping: instrument_id={}".format(
                        db_record.instrument_id
                    )
                )
                return

            # Resolve to our internal symbol.
            our_sym = symbolizer.databento_to_ours(sym)

            # If this is a blend symbol, check which leg it is.
            is_blend_leg = sym in self.cme_contract_to_leg
            if is_blend_leg:
                leg = self.cme_contract_to_leg[sym]

            now_ms = int(time.time() * 1000)

            # For non-blend symbols, throttle early to skip unnecessary work.
            # For blend symbols, we always validate + update leg state first so
            # the blend uses fresh data, then throttle only the publish.
            if not is_blend_leg:
                last_pub = self.cme_last_publish_time.get(our_sym, 0)
                if now_ms - last_pub < self.publish_interval_ms:
                    return

            # Skip if we're not in session.
            # Warning: Don't skip other types of messages, esp. symbol mappings!
            if not self._is_in_cme_session(db_record.ts_event):
                return

            # nano -> milli.
            ts_event_ms = int(db_record.ts_event * 1e-6)

            if self.db_cme_bad_count >= 100:
                # Restart the cme feed.
                self.db_cme_bad_count = 0
                email_utils.send_alerts("PYFEED DATABENTO_CME DOWN", "Too many badbook exceptions. Restarting.")
                raise ForceRestartFeed("Got too many db_cme badbook exceptions, restarting")

            # Do some extra checking on db messages.
            if (db_record.price == db.UNDEF_PRICE or db_record.ts_event == db.UNDEF_TIMESTAMP or db_record.ts_recv == db.UNDEF_TIMESTAMP or \
                (db_record.flags & (RF.F_SNAPSHOT |  RF.F_MAYBE_BAD_BOOK))
                ):
                self.logger.warning("Got an unusable msg: {}".format(db_record))
                self.db_cme_bad_count += 1
                return

            if (db_record.flags & (RF.F_PUBLISHER_SPECIFIC)):
                # Log but don't return, something might be wrong but not sure.
                self.logger.warning("DB CME, got unusual message: {}".format(db_record))

            # DB gives prices in units of 1e-9:
            # https://databento.com/docs/api-reference-live/basics/schemas-and-conventions?historical=python&live=python&reference=python
            px_convert = 1e-9

            px = abs(db_record.price * px_convert) # not used except for error check
            bb_px = round(db_record.levels[0].bid_px * px_convert, 9)
            ba_px = round(db_record.levels[0].ask_px * px_convert, 9)

            bid_sz = db_record.levels[0].bid_sz
            ask_sz = db_record.levels[0].ask_sz

            # Basic sanitizing, in case we get a heartbeat over a weekend maybe.
            # https://databento.com/docs/schemas-and-data-formats/mbp-1#fields-mbp-1?historical=python&live=python&reference=python
            if px > 1e6 or bb_px > 1e6 or ba_px > 1e6:
                self.logger.warning("Got an unusable msg: {}".format(db_record))
                return

            # Roll blend handling: always update leg state with validated data,
            # then throttle the publish. This ensures the blend always uses the
            # freshest data from both legs even when individual messages are throttled.
            if is_blend_leg:
                self.cme_roll_latest[our_sym][leg] = (bb_px, ba_px, bid_sz, ask_sz, ts_event_ms)

                # Now throttle — state is already updated above
                last_pub = self.cme_last_publish_time.get(our_sym, 0)
                if now_ms - last_pub < self.publish_interval_ms:
                    return

                front_data = self.cme_roll_latest[our_sym]["front"]
                next_data = self.cme_roll_latest[our_sym]["next"]
                if front_data is None or next_data is None:
                    return  # Wait until we have data from both legs

                blend_info = self.cme_blend_info[our_sym]
                front_w = blend_info["front_w"]
                next_w = blend_info["next_w"]

                # TODO: The "blend" is really just of the mid_px. We also blend the bb/ba (px/sz),
                # which is weird if the query does something mechanical or non-linear with that.
                # For now the query only uses the mid_px anyway, so it's fine.
                bb_px = front_w * front_data[0] + next_w * next_data[0]
                ba_px = front_w * front_data[1] + next_w * next_data[1]
                bid_sz = front_data[2] + next_data[2]
                ask_sz = front_data[3] + next_data[3]
                # Use the most recent event time from either leg
                ts_event_ms = max(front_data[4], next_data[4])

                # Just some extra logging to check the blending
                if self.db_cme_count % 3_000 == 0 or self.db_cme_count < 100:
                    front_mid = round((front_data[0] + front_data[1]) / 2, 6)
                    next_mid = round((next_data[0] + next_data[1]) / 2, 6)
                    blend_mid = front_w * front_mid + next_w * next_mid
                    self.logger.info(
                        f"Blending {our_sym} mids: "
                        f"{front_w} x {front_mid} + {next_w} x {next_mid} = {blend_mid}")

                # self.logger.debug(f"ROLL BLEND {our_sym}: front=({front_data[0]:.4f},{front_data[1]:.4f}) next=({next_data[0]:.4f},{next_data[1]:.4f}) w={front_w} -> blended=({bb_px:.4f},{ba_px:.4f})")

            # OK, create the pbmessage now.
            pbmsg = mdmsg_pb2.PbMessage()
            # PBMARKET_TOPBOOK_CME
            pbmsg.market = 9
            pbmsg.symbol_id = our_sym

            pbquote = pbmsg.quote
            #pbquote.update_id = db_record.sequence
            pbquote.best_bid_px = str(bb_px)
            pbquote.best_bid_qty = str(bid_sz)
            pbquote.best_ask_px = str(ba_px)
            pbquote.best_ask_qty = str(ask_sz)
            pbquote.mid_px = round((bb_px + ba_px) / 2, 6)
            pbquote.transact_time = ts_event_ms

            # Publish it to the book.
            pub_string = pbmsg.SerializeToString()

            # Getting stuff.
            # Also log how behind we might be.
            tot_delay_ms = now_ms - ts_event_ms
            queue_len = self.dbento_cme_queue.qsize()
            if self.db_cme_count % 3_000 == 0 or tot_delay_ms > 1_000 or queue_len > 100:
                ts_recv_ms = int(db_record.ts_recv * 1e-6)
                ts_out_ms = int(db_record.ts_out * 1e-6)
                recv_delay_ms = now_ms - ts_recv_ms
                ts_out_delay_ms = now_ms - ts_out_ms
                self.logger.info(
                    "{}: CME {} msgtime {} tot_delay_ms {} recv_delay_ms {} ts_out_delay_ms {} queue_len {}".format(
                        time.time(), sym, ts_event_ms, tot_delay_ms, recv_delay_ms, ts_out_delay_ms, queue_len
                    )
                )

            # Kill feed if data is too stale (e.g., > 10s)
            if tot_delay_ms > self.STALE_THRESHOLD_MS and not self.testing:
                err_msg = (f"CME {sym} delay {tot_delay_ms}ms exceeds "
                           f"{self.STALE_THRESHOLD_MS}ms threshold, forcing reconnect.")
                self.logger.error(err_msg)
                email_utils.send_alerts("CME FEED STALE", err_msg)
                raise ForceRestartFeed(f"CME feed stale: {sym} delay={tot_delay_ms}ms")

            self.cme_last_publish_time[our_sym] = now_ms

            if self.testing:
                self.logger.info(f"CME {our_sym} ({sym}) bid={bb_px} ask={ba_px}")

            await self.publish_async(pub_string)

        elif isinstance(db_record, SymbolMappingMsg):
            self.sym_to_idx_cme[db_record.stype_in_symbol] = db_record.instrument_id
            self.idx_to_sym_cme[db_record.instrument_id] = db_record.stype_in_symbol
            self.got_sym_map_db_cme = True
            self.logger.info(
                "CME SymbolMapping: {} -> {}".format(
                    db_record.stype_in_symbol, db_record.instrument_id
                )
            )

        elif isinstance(db_record, StatusMsg):
            sym = None
            if hasattr(db_record, "instrument_id"):
                sym = self.idx_to_sym_cme.get(db_record.instrument_id)
            if sym:
                self.logger.info("CME Status: {} {}".format(sym, db_record))
            else:
                self.logger.info("CME Status: {}".format(db_record))

        elif isinstance(db_record, ErrorMsg):
            sym = None
            if hasattr(db_record, "instrument_id"):
                sym = self.idx_to_sym_cme.get(db_record.instrument_id)
            if hasattr(db_record, "code") and db_record.code == ErrorCode.SKIPPED_RECORDS_AFTER_SLOW_READING:
                self.logger.warning(f"CME Error: slow_reader_behavior=skip triggered, records were skipped: {db_record}")
            elif sym:
                self.logger.error("CME Error: {} {}".format(sym, db_record))
            else:
                self.logger.error("CME Error: {}".format(db_record))

        elif isinstance(db_record, SystemMsg):
            try:
                # Check for subscription confirmation
                if db_record.msg.startswith("Subscription") and db_record.msg.endswith("succeeded"):
                    self.subscribed_db_cme = True
                    self.logger.info(f"CME System subscription confirmed: {db_record}")
                else:
                    sym = self.idx_to_sym_cme.get(db_record.instrument_id)
                    self.logger.info("CME System: {} {}".format(sym, db_record))
            except AttributeError:
                self.logger.info("CME System: {}".format(db_record))

        else:
            self.logger.info(f"Unrecognized CME db_record: {db_record}")

    # Start up
    async def run(self, end_secs):
        self._loop = asyncio.get_running_loop()
        # Daemon thread so it survives a wedged event loop and doesn't hold up a
        # clean exit. See _shutdown_watchdog.
        threading.Thread(target=self._shutdown_watchdog, daemon=True,
                         name="shutdown-watchdog").start()
        tasks = set()
        # HL: WS (legacy) runs whenever Hyperliquid syms are configured.
        # The new node feed (HyperliquidNode) runs as its own task and
        # toggles hl_publishing_from_node. When BOTH are configured the
        # WS path still receives msgs but only publishes when the node
        # feed is unhealthy (the gate is in hl_receive_loop).
        if self.hl_syms:
            tasks.add(asyncio.create_task(self.init_hl()))
        if self.hl_node_books_endpoint and self.hl_node_fills_endpoint:
            tasks.add(asyncio.create_task(self.init_hl_node()))
            # If only HyperliquidNode is configured, node is always primary.
            if not self.hl_syms:
                self.hl_publishing_from_node = True
        tasks |= {
            asyncio.create_task(self.init_databento_eq()),
            asyncio.create_task(self.init_databento_cme()),
            asyncio.create_task(self.init_databento_boats()),
            asyncio.create_task(self.init_polygon_eq()),
            asyncio.create_task(self.init_polygonfx()),
            asyncio.create_task(self.init_pyth()),
            asyncio.create_task(self.init_refinitiv()),
            asyncio.create_task(self.config_reload_loop()),
        }
        time_to_eod = end_secs - time.time()
        try:
            self._setup_pub_socket()
            done, pending = await asyncio.wait(tasks, timeout=time_to_eod)
            if pending: # timed out
                self.logger.info("EOD time reached, initiating shutdown")
                self.eod_event.set()
                # Release the PUB socket now, before the (unbounded) drain below,
                # so its close()/unlink happens at EOD start rather than at some
                # later time that could slip past 18:00 and unlink the *next*
                # process's freshly-bound socket file (libzmq unlinks by path; see
                # _setup_pub_socket). The shutdown watchdog normally bounds that
                # drain to well before 18:00, so this is defense-in-depth for if
                # the watchdog is ever removed/broken. Safe to close early only
                # because publish_async/publish_threadsafe no-op when pub_socket
                # is None -- keep that guard if you add new publishers.
                self._release_pub_socket()
                # Let each task handle the eod_event and clean up
                await asyncio.wait(pending)
        except asyncio.CancelledError:
            self.logger.info("Main loop cancelled")
        finally:
            self._release_pub_socket()
            self.logger._listener.stop()
            # Teardown finished cleanly — defuse the shutdown watchdog.
            self._teardown_done = True



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
    parser.add_argument("--position", required=False,
                        default=socket.gethostbyname(socket.gethostname()),
                        help="IP address for Refinitiv login Position field (default: auto-detect via gethostbyname)")
    parser.add_argument("--testing", default=False, action="store_true",
                        help="Testing mode: log every price update, skip stale-data restarts")
    args = parser.parse_args()

    if args.how:
        print("How: ")
        print("./pymultifeed.py --conf feed.json  --creds /home/ubuntu/.creds/--date TOMORROW --end PKDayEnd")
        sys.exit()

    # End like this.
    if "2" in args.date:
        # We can give it a date label or a date string like 20250501
        date_str = args.date
    else:
        date_str = chron.get_date_easy(args.date)

    end_str = chron.get_end_time(args.end_label, date_str)

    # Start shutting down 15s before the session end so the old process is fully
    # gone before the next one starts (mirrors pkmultifeed). This must exceed the
    # shutdown-watchdog deadline (10s) so a hung teardown gets force-exited within
    # the buffer, not after the next process has already tried to bind the socket.
    end_secs = chron.timestr_to_secs(end_str) - 15

    pyfeed = MultiFeed(args.conf, args.creds, date_str, end_secs, position=args.position, testing=args.testing)

    # Set up signal handler for graceful shutdown
    def signal_handler(sig, frame):
        pyfeed.logger.info("Received interrupt signal, shutting down gracefully...")
        pyfeed.eod_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    await pyfeed.run(end_secs)


if __name__ == "__main__":
    uvloop.run(main())
    # I did a bunch of bs to make polygon work here.
