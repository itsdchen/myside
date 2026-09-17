#! /usr/bin/env python


"""

Python version of our order entry gateway. Only supports a few markets:

Hyperliquid
HyperliquidTest

I'm going to make it pretty simple for now. It's only going to support
Hyperliquid and HyperliquidTest

Sample command:
./pygateway.py --market HyperliquidTest


Note: a lot of this is just copypasted from live_cockpit. This is not
ideal. I should take all of this out into its own set of indep
libraries at some point.
  - But not today.

I'm now testing out order entry on hyperliquid testnet.


OK, did a first pass on this.
Next step is to look at parsing responses from the ws side.

If running with multiple IPs, do it like: 
./wsgateway.py --market Hyperliquid --id 0 --local-ips 172.31.43.103,172.31.37.88

where --local-ips are the PRIVATE ips of the instance. You can find them by looking at the instance 
at
EC2 > Network Interfaces > eni-0be136935381270ea > Manage

Make sure the private ips are attached to public ips. 


To check my private ips: 

> ip -brief addr show
> ip addr show enp39s0


"""
import os
import sys
import threading
sys.path.append("{}/..".format(os.path.dirname(os.path.abspath(__file__))))
sys.path.append("{}/../util".format(os.path.dirname(os.path.abspath(__file__))))

# We need to handle
import asyncio
import aiohttp

import chron

import argparse
import bisect
import getpass
import math
import operator
import requests
import signal
import time
import json
from requests.exceptions import HTTPError
from urllib.parse import urlencode
from eth_abi import encode
import eth_account
from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex
import gateway_pb2
import datetime
import hashlib
from dataclasses import dataclass

from util import email_utils

import websockets
import websockets.exceptions
from tenacity import wait_fixed, retry
import traceback

import zmq
import zmq.asyncio
import time
import uvloop
import re
import socket

# For signing orders.
import hl_signing


# RTT monitoring: we accumulate order/cancel round-trip times into a fixed
# histogram (mergeable across machines, O(1) per update, fixed memory) and dump
# a stats line per category at shutdown for gateway_log_analyzer.py to parse.
RTT_BIN_MS = 5      # bucket width in ms
RTT_NBINS = 1000    # covers 0..5000 ms; larger RTTs go to an overflow counter
RTT_CATS = ("Alo", "Ioc", "Cxl")



@dataclass
class WSConnection:
    index: int              # 0 = primary (subscribes to fills/orderUpdates)
    local_ip: object        # str or None; None = don't bind
    ws: object = None       # set after connect

    # Per-connection rate limit state
    minute_cutoff_s: float = 0.0
    num_batches_1min: int = 0
    num_msgs_1min: int = 0
    num_weighted_1min: int = 0
    # Priority IOC lane (TIF=2): carved out of the per-min budget so cross
    # strats can't starve MM strats. Past the cap, pending priority IOCs are
    # rejected back to the strategy.
    num_priority_batches_1min: int = 0
    paused_429: bool = False
    # Previous minute's totals, snapshotted at rollover. A 429 can arrive just after the
    # minute boundary for traffic sent before it, and by then the live counters have already
    # been zeroed, so the 429 handler reports these instead.
    prev_num_batches_1min: int = 0
    prev_num_msgs_1min: int = 0
    prev_num_weighted_1min: int = 0

    def reset_minute_if_needed(self, now_t):
        if now_t >= self.minute_cutoff_s:
            self.prev_num_batches_1min = self.num_batches_1min
            self.prev_num_msgs_1min = self.num_msgs_1min
            self.prev_num_weighted_1min = self.num_weighted_1min
            self.num_batches_1min = 0
            self.num_msgs_1min = 0
            self.num_weighted_1min = 0
            self.num_priority_batches_1min = 0
            self.minute_cutoff_s = (now_t // 60 + 1) * 60
            self.paused_429 = False

    def headroom(self, now_t):
        self.reset_minute_if_needed(now_t)
        if self.paused_429:
            return None
        # Assume the 1200 limit on the weighted count that's supposed to be for REST queries since
        # that's what we observed they use, even though the docs imply it should be a 2000 limit on
        # the message count. This could be negative. We'll just try to send to the connection with
        # the highest headroom (even if negative), and rely on the other backoff and the actual
        # 429 errors, so we don't stop sending orders/cancels prematurely.
        return 1200 - self.num_weighted_1min


# 20% of the 1200 weighted-msgs/min headroom budget per connection. Past this
# many priority batches in a minute, pending priority IOCs are rejected back
# to the strategy instead of sent.
PRIORITY_BATCHES_CAP_PER_MIN = 240


###################################################################
# This stuff is just copied from log.py, but I don't want to deal with copying over the dir
# structure to livetrading. So, sorry.
import logging
from logging.handlers import RotatingFileHandler
from logging import handlers
import sys


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

    # Nest email_utils under us, so its records land in our handlers.
    email_utils.use_parent_logger(log)
    return log


###################################################################
# Test creating a protobuf

# The sub_socket is the one where traders send outbound orders,
# where we
# Remember that zmq pubs need to BIND first before subs can CONNECT to them.
# That's why we have this specific ordering of starting up processes.
# BINDING happens immediately, SUB needs to happen eventually.

mkt_to_config = {
    "Hyperliquid": {
        "http_api_endpoint": "https://api.hyperliquid.xyz/",
        "wss_listen_endpoint": "wss://api.hyperliquid.xyz/ws",
        # TODO: do this later
        "md_sub_socket": "ipc:///tmp/feed-sock",
        # Important.
        "oe_sub_socket": "ipc:///tmp/gateway-oe-sub-sock",
        "oe_pub_socket": "ipc:///tmp/gateway-oe-pub-sock",
        # No risk checks rn?
        # TODO: create simple risk checks later.
        "risk_config": "risk.json",
        "recv_window_ms": 60000,
        # HIP-4 deployer venue(s) to trade outcomes on (e.g. ["txyz"]). Empty = OFF: no
        # outcome discovery, no outcome gate (unchanged behavior). When set, the gateway
        # periodically refreshes the live outcome set on these venues and refuses orders /
        # capital-reqs for outcome coins not currently live on them. Overridable per-instance
        # with --outcome-venues.
        "outcome_venues": [],
        # How often (minutes) to refresh sym_to_idx (perp/spot).
        "symbol_refresh_mins": 10,
        # How often (minutes) to refresh the live outcome set. Faster than the perp/spot
        # refresh because it also drives resolution: a settled outcome drops out of outcomeMeta,
        # leaves live_outcome_coins, and the gate then rejects it (which the strat winds down on).
        "outcome_refresh_mins": 3,
    },
    "HyperliquidTest": {
        "http_api_endpoint": "https://api.hyperliquid-testnet.xyz/",
        "wss_listen_endpoint": "wss://api.hyperliquid-testnet.xyz/ws",
        # TODO: do this later
        "md_sub_socket": "ipc:///tmp/feed-sock",
        # Important.
        "oe_sub_socket": "ipc:///tmp/gateway-oe-sub-sock",
        "oe_pub_socket": "ipc:///tmp/gateway-oe-pub-sock",
        # No risk checks rn?
        # TODO: create simple risk checks later.
        "risk_config": "risk.json",
        "recv_window_ms": 60000,
        "outcome_venues": [],
        "symbol_refresh_mins": 10,
        "outcome_refresh_mins": 3,
    },

}

# The gateway rejects an order for an outcome coin that is not live on the configured deployer
# venue(s) with a reason containing this exact marker. RelWideHip4::ordReject matches on it to
# wind down (a settled outcome drops out of outcomeMeta -> off the live set -> this reject).
# Keep this string in sync with the ordex (src/pktrade/ordex/rel_wide_hip4.cc).
OUTCOME_NOT_LIVE_MARKER = "outcome not live"


HYPERLIQUID_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


async def hyp_fetch(session, endpt, payload):
    async with session.post(endpt, json=payload) as response:
        return await response.text()



def hash_to_128bit_hex(s: str) -> str:
    h = hashlib.md5(s.encode()).hexdigest()
    return "0x" + h

class HyperliquidGateway:
    def __init__(self, market, creds, id, local_ips=None, fast_cancels=True,
                 outcome_venues=None):
        # Make a logger.
        # Log name should have date.
        current_date = datetime.date.today()
        date_string = current_date.strftime("%Y%m%d")
        self.logger = getLogger("pygateway", "pygateway.{}.log".format(date_string))

        # Whether we're testnet or real
        self.market = market
        self.unit_market = {"Hyperliquid": "xyz", "HyperliquidTest": "xyz"}[self.market]

        self.gateway_config = mkt_to_config[market]
        self.gateway_id = id
        self.fast_cancels = fast_cancels

        # HIP-4 outcome discovery + safety gate. `outcome_venues` (CLI override, else the market
        # config default) are the deployer venues we trade outcomes on. When non-empty, the
        # gateway periodically rebuilds `live_outcome_coins` (all "#" coins live on those venues)
        # and refuses orders / capital-reqs for outcome coins not in it. Empty = feature OFF.
        self.outcome_venues = (list(outcome_venues) if outcome_venues is not None
                               else list(self.gateway_config.get("outcome_venues", [])))
        self.symbol_refresh_mins = self.gateway_config.get("symbol_refresh_mins", 10)
        self.outcome_refresh_mins = self.gateway_config.get("outcome_refresh_mins", 3)
        self.live_outcome_coins = set()
        # False until the first successful outcome discovery. While the gate is enabled but not
        # ready, "#" orders are blocked fail-closed (we don't trade an unverified outcome).
        self.outcome_gate_ready = False
        # One-shot alert per coin whose capital-req was refused for being off-venue.
        self.outcome_gate_alerted = set()

        # 5 ms sleep — was 20ms; reduced to pick up new orders sooner.
        # Hits gateway IOC RTT directly. CPU cost from 4x polls is negligible.
        self.zmq_sleep_t = 0.005
        # Rate limit safeguard: 
        # HL Limit is 2000 msgs/min.
        # We have two loops (orders, cancels). 
        # 0.07s gap => ~14.3 req/s/loop => ~28.6 req/s total => ~1714 req/min.
        # This keeps us safely under 2000 even if both loops are saturated.
        # Note (the actual limit might actually be 1200, but we're also sending with two ws'...)
        # someone on discord: "going by the logic, if you write to ws, then the rest limit apply as well"
        self.gap_between_send_s = 0.07

        # On Hyperliquid, the 100 highest nonces are stored per address
        # https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets

        # Allocate nonces among the different order types 
        # Instead of doing different buckets among order types, I'm just going to allocate nonce
        # usage among the current gateway. 
        # Strat: 
        # nonce_now = t_ms - (t_ms % 10) + index
        # oldest_poss_nonce = nonce_now - 90 (make sure it is a multiple of max_num_gateways) 
        # Compare oldest_poss_nonce to self.last_nonce_used. 
        # if it's greater, then use oldest_possible_nonce
        # if it's less, then use last_nonce_used + self.max_num_gateways

        self.last_nonce_used = id
        self.max_num_gateways = 10


        # Build per-IP websocket connection objects (rate limiting is per-connection)
        if local_ips:
            self.ws_connections = [
                WSConnection(index=i, local_ip=ip)
                for i, ip in enumerate(local_ips)
            ]
        else:
            self.ws_connections = [WSConnection(index=0, local_ip=None)]

        self.is_mainnet = self.market == "Hyperliquid"

        self.market_endpoint = self.gateway_config["http_api_endpoint"]

        # Giving back market responses to the strategy. One shared context for both
        # gateway sockets, so teardown can close them and term() a single context.
        self.zmq_ctx = zmq.asyncio.Context()
        self.acks_pub_socket = self.zmq_ctx.socket(zmq.PUB)
        self._remove_stale_ipc(self.gateway_config["oe_pub_socket"])
        self.acks_pub_socket.bind(self.gateway_config["oe_pub_socket"])
        self.logger.info("Gateway bound to pub socket. Waiting and then subscribing to sub socket")

        time.sleep(2)

        self.oe_sub_socket = self.zmq_ctx.socket(zmq.SUB)

        # Also, set up the order entry session.
        self.oe_session = requests.Session()
        self.oe_session.headers.update({"Content-Type": "application/json"})

        with open(creds) as f:
            mkt_creds_dct = json.load(f)

        self.secret_key = mkt_creds_dct["secret_key"]
        self.eth_wallet = eth_account.Account.from_key(self.secret_key)
        self.wallet_address = self.eth_wallet.address

        # Load subaccount address if provided (for trading on behalf of a subaccount)
        self.subaccount_address = mkt_creds_dct.get("subaccount_address", None)

        # The account whose fills/orderUpdates we subscribe to: an explicit listen_address
        # if given, else the subaccount, else the main wallet. Also used to tell apart a
        # liquidation of OUR position from one where we're just the contra.
        self.listen_address = (mkt_creds_dct.get("listen_address")
                               or self.subaccount_address or self.wallet_address)

        # Symbols blocked from new orders after we saw one of our positions liquidated
        # (cancels are never blocked). Mirrored to a block file kept next to this script
        # so it's easy to find/edit on the box: we append on auto-block, and every 30s
        # watch_block_file reconciles with the file authoritative for removals — delete a
        # symbol's line there to unblock it without restarting. Both the set and the file
        # start empty on startup, so blocks do not persist across restarts.
        self.liq_blocked_syms = set()
        self.block_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f"wsgateway_block.{self.market}.{self.gateway_id}.txt")
        self._write_block_file()

        # Grab the symbol universe.
        hl_asset_maps = self.load_hyperliquid_assets()

        self.sym_to_idx = hl_asset_maps["sym_to_idx"]
        self.idx_to_sym = hl_asset_maps["idx_to_sym"]

        # Load assets.

        # I know this is bad naming, and we should redo it.
        # note that hl_hexoids is DIFFERENT from hloids.
        # hl_hexoid is a string rep of a 128 big hex (e.g., '0xa367919e0f2814c68b2615ca4f6ed73e')
        # hloid is an int (e.g., 39784561743)
        self.pk_cloids_to_hl_hexoids = {}
        # Reverse map for if we get the exec.
        self.hl_hexoids_to_pk_cloids = {}

        # Mapping of our oids to their oids.
        self.pkcoids_to_hloids = {}
        self.hloids_to_pkcoids = {}

        self.pkcoid_cxl_count = {}

        # We need to save these when we flush zmq.
        # ALO (TIF=4) goes here.
        self.queued_fast_ord_pbs = []

        # IOC priority lane (TIF=2). Drained every send-loop tick (5ms cadence)
        # so IOC pickup latency is bounded by the loop period, not by the
        # gap-between-ALO-sends. Subject to PRIORITY_BATCHES_CAP_PER_MIN per
        # connection.
        self.queued_priority_ord_pbs = []

        # ALO sends are paced to one batch per gap_between_send_s. The loop
        # itself runs every 5ms so it can drain IOCs quickly, but it only
        # actually fires an ALO batch when enough time has elapsed since the
        # previous ALO send.
        self.last_alo_send_t = 0.0

        # GTC orders are no longer allowed — strategies should use IOC or ALO.
        # We reject any non-IOC/ALO TIF at the routing step and fire a one-shot
        # email so a misconfigured strat surfaces fast without spamming alerts.
        self._gtc_alert_sent = False

        # List of rejected cancel_oids that need to be retried.
        self.queued_cancel_pbs = []

        # HIP-4 capitalization. The strategy declares, per ticker, how many complete sets of an
        # outcome it needs (PbCapitalReq); the gateway owns making it so: read on-chain balances,
        # mint the shortfall via a userOutcome/splitOutcome L1 action, and BLOCK order placement
        # for the ticker until it is capitalized. If capitalization fails, the block stays and we
        # alert loudly. This is one-directional (strat -> gateway); nothing is sent back.
        #   cap_reqs: symbol -> {outcome, target, strategy_id, symbol, state}
        #     state in {"needed", "in_flight", "done", "failed"}
        self.cap_reqs = {}
        # Symbols currently refused for placement (pending or failed capitalization). Checked in
        # the send loop alongside liq_blocked_syms.
        self.cap_blocked_syms = set()
        # One-shot failure alert per symbol, so a stuck capitalization doesn't spam email.
        self.cap_alerted = set()
        # ws_msg_idx -> symbol for an in-flight capitalization split, to match the WS response.
        self.ws_id_to_cap_sym = {}

        # We need to ignore cancels for these oids bc they were already cancelled.
        # Maps pkcoid -> time.time() when first marked dead. The timestamp lets
        # the orphan backstop tell a just-cancelled order (still settling on HL)
        # from one that should have left the book long ago.
        self.pkcoids_to_dead_t = {}
        # pkcoids the orphan backstop wants force-cancelled: these are already in
        # pkcoids_to_dead_t, so the normal send path would short-circuit them
        # (lines guarding pkcoid_cxl_count and pkcoids_to_dead_t). Membership
        # here lets such a cancel through to HL exactly once.
        self.force_cancel_pkcoids = set()
        # Orders where we got an exec.
        # If something is here, then we should not requeue it if we get a 
        # "never placed" error. 
        self.recent_exec_pkcoids = set()

        # Think this isn't needed rn.
        #self.pk_to_hl_hexoids = {}
        # ws_idx -> list(pk_cloids) that was sent in the batch.
        self.ws_id_to_pk_cloids_batch = {}

        # When we send orders over ws, we'll need to track the idx.
        # This also lets us know how to map the responses payloads to our sent payloads.
        self.ws_msg_idx = 0

        # Set of tids that we've already delivered. For example,
        # if we hear the same exec back, let's skip the second time.
        # note that tids is what we want - txhashes can be
        # the same for multiple trades that happen within, I'm guessing,
        # the same block?
        # Map tid -> int(seen_at_unix_time). The periodic_cleanup task
        # purges entries older than handled_tid_lifetime_s so this dict
        # doesn't grow without bound across days of uptime.
        self.handled_tids_to_t = {}
        self.handled_tid_lifetime_s = 120

        # We can have timing issues where we get an exec before we get the actual
        # ack back (since these are kind on diff streams). In which case,
        # we keep the previous iter's fills just in case.
        self.queued_fills = []

        # Ugh. All the other crap that needs to be filled in for
        # our craps.
        # strategy_id
        # executor_id
        # qty
        # tif
        # keys are pk_coids (eg. t1-1)
        # {pk_coid : {strategy_id, executor_id, symbol}}
        self.pkcoids_to_orderdeets = {}

        # To keep track of our rtts and when an order has gone un-acked too long.
        # pk_coid -> {t_placed, t_resp}
        self.pkcoids_to_timings = {}

        # RTT monitoring accumulators (see RTT_* constants). One fixed histogram
        # per category plus exact count/min/max; updated O(1) on each response
        # and dumped at shutdown by _log_rtt_stats().
        self.rtt_hist = {c: [0] * RTT_NBINS for c in RTT_CATS}
        self.rtt_overflow = {c: 0 for c in RTT_CATS}
        self.rtt_count = {c: 0 for c in RTT_CATS}
        self.rtt_min = {c: None for c in RTT_CATS}
        self.rtt_max = {c: None for c in RTT_CATS}
        # ws_msg_idx -> send time, so cancel responses can compute their RTT.
        self.ws_id_to_cancel_send_t = {}

        # Listen to our fills via websocket.
        self.ws_url = self.gateway_config["wss_listen_endpoint"]

        # Event flag for graceful shutdown (thread-safe)
        self.eod_event = asyncio.Event()
        # Set once teardown finishes; the shutdown watchdog watches this.
        self._teardown_done = False

        # If a symbol is added intraday, we'll likely get a KeyError accessing sym_to_idx, so
        # remember that so on retry we can re-populate.
        self.error_on_key = False

        # If we get an "L1 is congested" error, we pause all orders and cancels for 1min.
        self.l1_congested_paused_until = None
        self.l1_congested_backoff_s = 10 # let's do 10s for now, see if we need exp backoff later

        # Orders sent and un-acked for this many seconds will be considered expired.
        self.expired_order_thresh_s = 3

        self.logger.info("HL fast cancels enabled: {}".format(self.fast_cancels))

    def _record_rtt(self, cat, rtt_s):
        """Accumulate one round-trip time (seconds) into the category histogram.

        Hot path: one int increment plus a couple comparisons. cat is one of
        RTT_CATS; anything else (e.g. Gtc) is ignored.
        """
        hist = self.rtt_hist.get(cat)
        if hist is None:
            return
        idx = int(rtt_s * 1000.0 / RTT_BIN_MS)
        if idx >= RTT_NBINS:
            self.rtt_overflow[cat] += 1
        else:
            hist[idx] += 1
        self.rtt_count[cat] += 1
        if self.rtt_min[cat] is None or rtt_s < self.rtt_min[cat]:
            self.rtt_min[cat] = rtt_s
        if self.rtt_max[cat] is None or rtt_s > self.rtt_max[cat]:
            self.rtt_max[cat] = rtt_s

    def _remove_stale_ipc(self, uri):
        """Remove a leftover ipc:// socket file before binding. A prior process
        that exited uncleanly (crash / kill -9 / the shutdown watchdog's os._exit)
        leaves the file behind, and bind() then fails with EADDRINUSE. Mirrors
        pkmultifeed / pymultifeed."""
        if uri.startswith("ipc://"):
            sock_path = uri[len("ipc://"):]
            try:
                os.remove(sock_path)
                self.logger.warning(f"Removed stale ipc socket file {sock_path}")
            except FileNotFoundError:
                pass

    def release_zmq(self):
        if self.acks_pub_socket:
            self.logger.info("Closing ZMQ sockets")
            self.acks_pub_socket.close(linger=0)
            self.acks_pub_socket = None
        if self.oe_sub_socket:
            self.oe_sub_socket.close(linger=0)
            self.oe_sub_socket = None
        if self.zmq_ctx:
            self.zmq_ctx.term()
            self.zmq_ctx = None

    def _shutdown_watchdog(self):
        """Hard backstop against a wedged shutdown, in a daemon thread so it keeps
        ticking even if the event loop is stuck. If teardown doesn't finish within
        the deadline after EOD begins, force-exit rather than hang for days (as a
        pkmultifeed instance did on 20260717). 3s to fit inside the next gateway's
        5s start-of-day buffer; polled finely since that budget is tight."""
        WATCHDOG_SECS = 3
        # eod_event.is_set() is a plain-bool read, safe to poll from this thread.
        while not self.eod_event.is_set():
            time.sleep(0.5)
        deadline = time.monotonic() + WATCHDOG_SECS
        while not self._teardown_done:
            if time.monotonic() > deadline:
                try:
                    msg = (f"Shutdown watchdog: teardown exceeded {WATCHDOG_SECS}s, "
                           f"forcing os._exit(0) to avoid a hung process")
                    self.logger.error(msg)
                    email_utils.send_alerts("WSGATEWAY SHUTDOWN HANG", msg)
                finally:
                    os._exit(0)
            time.sleep(0.5)

    async def _cancel_pending(self, pending, name):
        """Cancel `pending` and wait up to 2s, then raise TimeoutError if any are
        still stuck so the caller's @retry restarts. Mirrors pymultifeed's
        _cancel_pending. 2s stays under the 3s EOD watchdog; this also runs on
        mid-day reconnects (watchdog not armed then), so a wedged subtask can't
        hang teardown forever."""
        if not pending:
            return
        for task in pending:
            task.cancel()
        _, still_pending = await asyncio.wait(pending, timeout=2)
        if still_pending:
            err_msg = (f"{name} task cancellations timed out "
                       f"(retrying but could have stuck tasks):")
            for t in still_pending:
                err_msg += f"\n  task={t} stack={[f.f_code.co_name for f in t.get_stack()]}"
            self.logger.error(err_msg)
            email_utils.send_alerts(f"{name.upper()} CANCELS TIMED OUT", err_msg)
            raise asyncio.TimeoutError(f"{name} task cancellations timed out")

    def _log_rtt_stats(self):
        """Dump one RTT_STATS line per non-empty category, then clear its counters.

        Called from the run_ws teardown (each reconnect plus final EOD). Clearing
        makes every block a non-overlapping delta, so the analyzer just sums all
        blocks — no matter how many reconnects or mid-day process restarts there
        were. An unclean exit only loses the RTTs recorded since the last dump.
        """
        for cat in RTT_CATS:
            n = self.rtt_count[cat]
            if n == 0:
                continue
            hist = self.rtt_hist[cat]
            last = max((i for i, v in enumerate(hist) if v), default=-1)
            hist_str = ",".join(str(v) for v in hist[:last + 1])
            self.logger.info(
                "RTT_STATS cat={} count={} min_ms={:.2f} max_ms={:.2f} "
                "bin_ms={} overflow={} hist={}".format(
                    cat, n, self.rtt_min[cat] * 1000.0, self.rtt_max[cat] * 1000.0,
                    RTT_BIN_MS, self.rtt_overflow[cat], hist_str))
            # Reset this category so the next dump is a fresh delta (cheap:
            # teardown-only).
            self.rtt_hist[cat] = [0] * RTT_NBINS
            self.rtt_overflow[cat] = 0
            self.rtt_count[cat] = 0
            self.rtt_min[cat] = None
            self.rtt_max[cat] = None

    def pick_best_connection(self, now_t, pri=False):
        """Return the WSConnection with the most headroom, or None if all exhausted.
           If pri, return the connection with the most priority headroom among those with any
           overall headroom."""
        best = None
        best_hr = -10_000 # higher is better
        best_pri = 10_000 # lower is better
        for conn in self.ws_connections:
            hr = conn.headroom(now_t)
            if hr is None:
                continue
            if pri and conn.num_priority_batches_1min < best_pri:
                best_pri = conn.num_priority_batches_1min
                best = conn
            elif not pri and hr > best_hr:
                best_hr = hr
                best = conn
        return best

    def clear_all_orders(self, reason):
        # Clear all queued orders and send order rejects to let queries know
        num_ords = (len(self.queued_priority_ord_pbs)
                    + len(self.queued_fast_ord_pbs))
        self.logger.warning(f"Clearing {num_ords} queued orders bc of {reason}.")
        for ord_pb in (self.queued_priority_ord_pbs
                       + self.queued_fast_ord_pbs):
            # Create an order reject
            pbresponse = gateway_pb2.PbMessage()
            new_rej = pbresponse.new_reject
            new_rej.strategy_id = ord_pb.strategy_id
            new_rej.executor_order_id = ord_pb.executor_order_id
            new_rej.reason = reason
            # Serialize and publish
            pub_string = pbresponse.SerializeToString()
            self.acks_pub_socket.send(pub_string)
        self.queued_priority_ord_pbs.clear()
        self.queued_fast_ord_pbs.clear()

    def clear_all_cancels(self, reason):
        # Clear all queued cancels and send cancel rejects to let queries know
        num_cxls = len(self.queued_cancel_pbs)
        self.logger.warning(f"Clearing {num_cxls} queued cancels bc of {reason}.")
        for cancel_pb in self.queued_cancel_pbs:
            # Create a cancel reject
            pbresponse = gateway_pb2.PbMessage()
            cancel_rej = pbresponse.cancel_reject
            cancel_rej.strategy_id = cancel_pb.strategy_id
            cancel_rej.executor_order_id = cancel_pb.executor_order_id
            cancel_rej.reason = reason
            # Serialize and publish
            pub_string = pbresponse.SerializeToString()
            self.acks_pub_socket.send(pub_string)
        self.queued_cancel_pbs.clear()

    @retry(wait=wait_fixed(0.5))
    async def run_ws(self):
        if self.eod_event.is_set():
            self.logger.error("EOD event set, not retrying HL websocket")
            return

        if self.error_on_key:
            self.error_on_key = False
            # Grab the symbol universe.
            # Note: do it here so if a new symbol gets added intraday it gets picked up on retry
            self.logger.info("Got KeyError, probably new sym: re-requesting HL assets on retry")
            hl_asset_maps = self.load_hyperliquid_assets()
            self.sym_to_idx = hl_asset_maps["sym_to_idx"]
            self.idx_to_sym = hl_asset_maps["idx_to_sym"]

        # Make sure order and cancel queues are empty on a retry for a clean-ish restart
        self.clear_all_orders("restarting")
        self.clear_all_cancels("restarting")
        # Disconnects often leave in-flight orders sent-but-unacked. Without
        # this, those orders sit in pkcoids_to_timings until the next periodic
        # check (up to 60s away) before the strategy is told they expired.
        # Catches orders already older than expired_order_thresh_s (3s) at
        # reconnect time; younger orders still rely on the periodic loop.
        self.check_expired_orders()

        open_websockets = []
        try:
            # Open one websocket per connection (each on its own local IP if configured)
            for conn in self.ws_connections:
                connect_kwargs = {"ping_interval": 100000000}
                if conn.local_ip is not None:
                    connect_kwargs["local_addr"] = (conn.local_ip, 0)
                ws = await websockets.connect(self.ws_url, **connect_kwargs)
                conn.ws = ws
                open_websockets.append(ws)
                self.logger.info(f"WS conn {conn.index} connected at {self.ws_url} "
                                 f"(local_ip={conn.local_ip})")

            self.logger.info(f"All {len(self.ws_connections)} WS connection(s) open")

            # Per-connection tasks: ping + receive loop
            # Shared tasks: one send-orders loop + one send-cancels loop
            tasks = set()
            for conn in self.ws_connections:
                tasks.add(asyncio.create_task(self.hl_ping(conn, 25)))
                tasks.add(asyncio.create_task(self.hl_receive_loop(conn)))
            tasks.add(asyncio.create_task(self.hl_send_orders_loop()))
            tasks.add(asyncio.create_task(self.hl_send_cancels_loop()))
            tasks.add(asyncio.create_task(self.capitalize_loop()))
            tasks.add(asyncio.create_task(self.refresh_symbols_loop()))
            tasks.add(asyncio.create_task(self.outcome_discovery_loop()))
            tasks.add(asyncio.create_task(self.periodic_cleanup()))
            tasks.add(asyncio.create_task(self.watch_block_file()))
            tasks.add(asyncio.create_task(self.eod_event.wait()))

            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel all pending tasks with a bounded wait (see _cancel_pending).
            await self._cancel_pending(pending, "WSGateway")

            # Reraise any exceptions in the child tasks to trigger the retry
            for task in done:
                if exc := task.exception():
                    raise exc
            self.logger.info("HL websocket cancelled")
        except websockets.exceptions.ConnectionClosedOK as e:
            # TODO: this reconnects both ws' when one gets closed. In future, probably want to 
            # separate this. Maybe even start them slightly offset so the 3 hour thing doesn't exactly coincide.  
            if e.rcvd.code == 1000 and e.rcvd.reason == "Expired":
                # This is the new (since 20251206) behavior from HL where server closes
                # connections after ~3h, regardless of activity. Just have to reconnect.
                self.logger.warning("Regular HL ConnectionClosedOK. Reconnecting.")
                raise # retry
            else:
                self.logger.error(f"Unexpected HL ConnectionClosedOK: {e}")
                raise # still retry
        except websockets.exceptions.ConnectionClosedError as e:
            self.logger.warning(f"Random but expected HL ConnectionClosedError: {e}")
            raise # retry
        except KeyError:
            self.error_on_key = True
            raise # retry
        except Exception as e:
            self.logger.error(f"HL websocket error: {e}")
            self.logger.error(traceback.format_exc())
            raise  # retry anyway
        finally:
            for ws in open_websockets:
                try:
                    await ws.close()
                except Exception:
                    pass
            for conn in self.ws_connections:
                conn.ws = None
            # Just want to see how big this grows to see if we need to garbage-collect.
            self.logger.info(f"Ending len(pkcoids_to_timings) = {len(self.pkcoids_to_timings)}")
            # Dump cumulative RTT stats for the daily report. Runs on every
            # teardown (reconnects included); the analyzer keeps the max-count
            # block per category, so an unclean final exit still leaves the day's
            # data from the last reconnect.
            self._log_rtt_stats()

    async def periodic_cleanup(self):
        # Periodically prunes stale bookkeeping that would otherwise grow
        # unbounded across days of gateway uptime. Cancelled via the ws-loop's
        # asyncio.wait(FIRST_COMPLETED) teardown like the other ws tasks.
        # NOTE: future cleanups (dead-pkcoid purge, per-strategy live order
        # tracking) will be added here when their backing data structures land.
        try:
            while not self.eod_event.is_set():
                # Drop tids we last saw more than handled_tid_lifetime_s ago.
                # The set exists to dedupe fills across the few seconds where
                # an exec can race with the ack on a different stream; we don't
                # need to remember tids forever.
                cutoff = time.time() - self.handled_tid_lifetime_s
                stale = [tid for tid, seen_at in self.handled_tids_to_t.items() if seen_at < cutoff]
                for tid in stale:
                    del self.handled_tids_to_t[tid]

                # Backstop: catch orders HL still has resting that we believe
                # are dead (e.g. an order/cancel double-lap that left a cancel
                # silently dropped). Wrapped so a transient query failure logs
                # but doesn't tear the gateway down via the outer handler.
                try:
                    await self._check_orphaned_orders()
                except Exception as e:
                    self.logger.error(f"orphan backstop failed: {e}")
                    self.logger.error(traceback.format_exc())

                await asyncio.sleep(300)  # 5 minutes
        except asyncio.CancelledError:
            self.logger.info("periodic_cleanup cancelled")
            raise
        except Exception as e:
            # Don't let a cleanup bug kill the gateway — log loudly and let
            # the FIRST_COMPLETED teardown bring everything down for a reconnect.
            self.logger.error(f"periodic_cleanup error: {e}")
            self.logger.error(traceback.format_exc())
            raise

    def _write_block_file(self):
        """Write the block file: instructional header followed by the currently blocked
        symbols. Used to initialize it on startup (empty block list, so header only) and
        to recreate it consistently if it goes missing."""
        header = (
            f"# wsgateway block file — {self.market} gateway {self.gateway_id}\n"
            "#\n"
            "# Symbols listed here (one HL coin per line, exactly as it appears in\n"
            "# fills/orders, e.g. xyz:XYZ100) are BLOCKED from new orders; cancels are\n"
            "# still allowed. The gateway appends a symbol here automatically when it sees\n"
            "# one of our positions liquidated, and alerts.\n"
            "#\n"
            "# Every 30s the gateway reconciles its in-memory block list with this file,\n"
            "# with the FILE AUTHORITATIVE for removals: to UNBLOCK a symbol without a\n"
            "# restart, delete its line here. (Reconciliation only runs while something is\n"
            "# blocked, so adding a line here is NOT a way to create a block from scratch.)\n"
            "# Lines starting with '#' and blank lines are ignored.\n"
            "#\n"
            "# This file is rewritten on restart — blocks do not persist across restarts.\n"
            "#\n"
            "# --- blocked symbols below this line ---\n"
        )
        try:
            with open(self.block_file, "w") as f:
                f.write(header)
                for sym in sorted(self.liq_blocked_syms):
                    f.write(f"{sym}\n")
        except Exception as e:
            self.logger.error(f"Could not write block file {self.block_file}: {e}")

    def _append_block_file(self, sym):
        """Append a newly auto-blocked symbol to the block file, keeping it in sync with
        the in-memory block list."""
        try:
            with open(self.block_file, "a") as f:
                f.write(f"{sym}\n")
        except Exception as e:
            self.logger.error(f"Could not append {sym} to block file {self.block_file}: {e}")

    def _read_block_file(self):
        """Return the set of symbols listed in the block file (comments/blanks ignored)."""
        with open(self.block_file) as f:
            lines = f.read().splitlines()
        return {s for line in lines if (s := line.strip()) and not s.startswith("#")}

    async def watch_block_file(self):
        """Every 30s, reconcile the in-memory block list with the block file (file
        authoritative for removals) so we can unblock a symbol — by deleting its line —
        without restarting. Skips all work while nothing is blocked, which is almost
        always the case."""
        self.logger.info(f"Block-file watcher starting ({self.block_file})")
        try:
            while not self.eod_event.is_set():
                await asyncio.sleep(30)
                # Fast path: nothing blocked (the usual case). The file is only used to
                # remove blocks, so there's nothing to reconcile while the set is empty.
                if not self.liq_blocked_syms:
                    continue
                try:
                    external = self._read_block_file()
                except FileNotFoundError:
                    self._write_block_file()  # recreate consistently if it went missing
                    continue
                except Exception as e:
                    self.logger.error(f"Block-file watcher read error: {e}")
                    continue
                # Anything dropped from the file gets unblocked. Symbols in the file but
                # not blocked internally are ignored (the file doesn't add manual blocks).
                removed = self.liq_blocked_syms - external
                if removed:
                    self.liq_blocked_syms -= removed
                    self.logger.warning(f"Block file: unblocked (new orders re-enabled) "
                                        f"for {sorted(removed)}")
        except asyncio.CancelledError:
            self.logger.info("Block-file watcher cancelled")
            raise
        self.logger.info("Block-file watcher exiting")

    async def hl_ping(self, conn, interval):
        self.logger.info(f"HL ping loop starting (conn {conn.index})")
        while not self.eod_event.is_set():
            try:
                await conn.ws.send(json.dumps({"method": "ping"}))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                self.logger.info(f"HL ping cancelled (conn {conn.index})")
                raise
            except websockets.exceptions.ConnectionClosedOK:
                self.logger.warning(f"HL ping got ConnectionClosedOK (conn {conn.index})")
                raise # retry
            except websockets.exceptions.ConnectionClosedError:
                self.logger.warning(f"HL ping got ConnectionClosedError (conn {conn.index})")
                raise # retry
            except Exception as e:
                error_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                self.logger.error(f"Error in HL ping (conn {conn.index}): {error_str}")
                email_utils.send_alerts("WSGATEWAY PING ERROR, RECONNECTING", error_str)
                raise # retry

        self.logger.info(f"HL ping loop exiting (conn {conn.index})")

    def check_expired_orders(self):
        now_t = time.time()
        for pkcoid, timing_dct in self.pkcoids_to_timings.items():
            if now_t - timing_dct["t_placed"] > self.expired_order_thresh_s and not timing_dct["t_resp"]:
                # If the order was placed >3s ago, and never acked or rejected, manufacture a
                # new_reject to the query and send a cancel to HL just in case.
                if pkcoid in self.pkcoids_to_dead_t: # unless it was already canceled
                    # Stamp t_resp so we don't check it again
                    timing_dct["t_resp"] = now_t
                    continue
                order_deets = self.pkcoids_to_orderdeets[pkcoid]
                # Create a reject.
                pbresponse = gateway_pb2.PbMessage()
                new_rej = pbresponse.new_reject
                new_rej.strategy_id = order_deets["strategy_id"]
                new_rej.executor_order_id = order_deets["executor_order_id"]
                new_rej.reason = f"Expired after {self.expired_order_thresh_s}s without ack"
                # Serialize and publish.
                pub_string = pbresponse.SerializeToString()
                self.acks_pub_socket.send(pub_string)
                # Send a cancel in case HL somehow processed or will process this order anyway
                ord_action_pb = gateway_pb2.PbMessage()
                cancel_pb = ord_action_pb.cancel_order
                cancel_pb.strategy_id = order_deets["strategy_id"]
                cancel_pb.executor_order_id = order_deets["executor_order_id"]
                self.queued_cancel_pbs.append(cancel_pb)
                # Consider this a "response" so we don't re-expire the same order
                timing_dct["t_resp"] = now_t

    async def hl_send_orders_loop(self):
        self.logger.info("HL send orders loop starting")
        send_orders_count = 0
        check_expired_orders_last_t = time.time()
        while not self.eod_event.is_set():
            try:
                # If we have no orders to send, just sleep a bit
                if (not self.queued_priority_ord_pbs
                        and not self.queued_fast_ord_pbs):
                    await asyncio.sleep(0.005)
                    continue

                now_t = time.time()
                now_t_ms = int(now_t * 1_000)

                # Check if we're paused bc of L1 congestion
                if self.l1_congested_paused_until is not None:
                    if now_t >= self.l1_congested_paused_until:
                        self.l1_congested_paused_until = None
                    else:
                        # We're paused, so dump stale orders and sleep
                        self.clear_all_orders("L1 congested")
                        await asyncio.sleep(0.1)
                        continue

                # Pick the connection with the most headroom
                conn = self.pick_best_connection(now_t)
                if conn is None:
                    # All connections exhausted / 429-paused — dump stale orders

                    # Don't just sleep until the end of the minute, bc if we get more orders from
                    # queries while paused we need to discard them so they don't go out later
                    self.clear_all_orders("429 pause - all connections exhausted")
                    await asyncio.sleep(0.1)
                    continue

                # If we're getting close to the rate limit (apparently 1200 like the REST limit, not
                # 2000 like HL docs says for websockets), back off a bit after this round.
                sleep_s = 0.005 # default sleep time 5ms for fast IOC sending
                if conn.num_weighted_1min > 1_100:
                    self.logger.warning(
                        f"Waiting to send orders on conn {conn.index}, "
                        f"1min {conn.num_batches_1min} batches, "
                        f"{conn.num_msgs_1min} msgs, {conn.num_weighted_1min} weighted.")
                    sleep_s = 0.3

                if send_orders_count % 200 == 0:
                    self.logger.info(
                        f"Sending orders on conn {conn.index}, "
                        f"1min {conn.num_batches_1min} batches, "
                        f"{conn.num_msgs_1min} msgs, {conn.num_weighted_1min} weighted, "
                        f"{conn.num_priority_batches_1min}/{PRIORITY_BATCHES_CAP_PER_MIN} priority.")

                # Filter out new orders for blocked symbols before sending (cancels go through a
                # different loop and stay allowed). Two block sets: liquidation blocks, and HIP-4
                # capitalization blocks (a ticker is refused until it holds enough complete sets,
                # and stays refused if capitalization fails). Almost always a no-op: both sets are
                # empty, so this is one check for the whole batch. When non-empty, two C-level
                # comprehensions per queue collect the rejects and keep the rest; the rejects are
                # sent after the live orders go out.
                # Third gate: the HIP-4 outcome safety gate refuses "#" coins that aren't live on
                # the configured deployer venue(s) (fail-closed before first discovery).
                rej_queue = []
                blocked_syms = self.liq_blocked_syms | self.cap_blocked_syms

                def _order_blocked(o):
                    return o.symbol in blocked_syms or self._outcome_gate_blocks(o.symbol)

                if blocked_syms or self.outcome_venues:
                    for q in (self.queued_priority_ord_pbs, self.queued_fast_ord_pbs):
                        rej_queue += [o for o in q if _order_blocked(o)]
                        q[:] = [o for o in q if not _order_blocked(o)]

                # Priority IOC lane. Drained every tick (subject to per-min cap)
                # so IOC pickup latency is loop-period-bounded (~5ms).
                if self.queued_priority_ord_pbs:
                    pri_conn = self.pick_best_connection(now_t, pri=True)
                    if pri_conn.num_priority_batches_1min >= PRIORITY_BATCHES_CAP_PER_MIN:
                        for one_order_pb in self.queued_priority_ord_pbs:
                            pbresponse = gateway_pb2.PbMessage()
                            new_rej = pbresponse.new_reject
                            new_rej.strategy_id = one_order_pb.strategy_id
                            new_rej.executor_order_id = one_order_pb.executor_order_id
                            new_rej.reason = (
                                f"Priority IOC budget exhausted "
                                f"({PRIORITY_BATCHES_CAP_PER_MIN}/min on conn {pri_conn.index})")
                            self.acks_pub_socket.send(pbresponse.SerializeToString())
                        if pri_conn.num_priority_batches_1min == PRIORITY_BATCHES_CAP_PER_MIN:
                            self.logger.warning(
                                f"Priority IOC cap hit on conn {pri_conn.index} "
                                f"({PRIORITY_BATCHES_CAP_PER_MIN}/min); rejected "
                                f"{len(self.queued_priority_ord_pbs)} pending IOCs.")
                            pri_conn.num_priority_batches_1min += 1  # bump past cap to suppress re-log
                        self.queued_priority_ord_pbs.clear()
                    else:
                        send_orders_count += 1
                        await self.send_hyperliquid_orders(pri_conn, now_t_ms, "priority")

                # ALO lane — paced to one batch per gap_between_send_s by
                # checking time-since-last-ALO-send instead of post-send sleep.
                if (self.queued_fast_ord_pbs
                        and (now_t - self.last_alo_send_t) >= self.gap_between_send_s):
                    send_orders_count += 1
                    await self.send_hyperliquid_orders(conn, now_t_ms, "fast")
                    self.last_alo_send_t = now_t

                # Send rejects for any orders dropped above due to a block (liquidation or
                # HIP-4 capitalization), after the live orders so we don't delay them (empty in
                # the common case).
                for one_order_pb in rej_queue:
                    pbresponse = gateway_pb2.PbMessage()
                    new_rej = pbresponse.new_reject
                    new_rej.strategy_id = one_order_pb.strategy_id
                    new_rej.executor_order_id = one_order_pb.executor_order_id
                    if one_order_pb.symbol in self.cap_blocked_syms:
                        reason = "not yet capitalized"
                    elif self._outcome_gate_blocks(one_order_pb.symbol):
                        # Terminal marker (OUTCOME_NOT_LIVE_MARKER) only once the gate is ready:
                        # the outcome is genuinely off-venue/settled, so the strat should wind
                        # down. Before first discovery it's transient ("gate not ready").
                        reason = (f"{OUTCOME_NOT_LIVE_MARKER} on deployer venue(s) "
                                  f"{self.outcome_venues}"
                                  if self.outcome_gate_ready else "outcome gate not ready")
                    else:
                        reason = "blocked after liquidation"
                    new_rej.reason = (
                        f"Symbol {one_order_pb.symbol} {reason}; "
                        f"new orders rejected until unblocked")
                    self.acks_pub_socket.send(pbresponse.SerializeToString())

                # Check if we have sent orders that have expired without an ack
                if now_t > check_expired_orders_last_t + 60: # only check once a minute
                    self.check_expired_orders()
                    check_expired_orders_last_t = now_t

                # Tight loop: IOCs get picked up within ~5ms; ALOs still pace
                # themselves via the time-gate above.
                await asyncio.sleep(sleep_s)

            except asyncio.CancelledError:
                self.logger.info("HL send orders loop cancelled")
                raise
            except websockets.exceptions.ConnectionClosedOK:
                self.logger.warning("HL send orders loop got ConnectionClosedOK")
                raise # retry
            except websockets.exceptions.ConnectionClosedError:
                self.logger.warning("HL send orders loop got ConnectionClosedError")
                raise # retry
            except Exception as e:
                error_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                self.logger.error("Error in HL send orders loop: {}".format(error_str))
                email_utils.send_alerts("WSGATEWAY SEND ORDERS ERROR, RECONNECTING", error_str)
                raise # retry
        self.logger.info("HL send orders loop exiting")

    async def hl_send_cancels_loop(self):
        self.logger.info("HL send cancels loop starting")
        send_cancels_count = 0
        while not self.eod_event.is_set():
            try:
                # If we have no cancels to send, just sleep a bit
                if not self.queued_cancel_pbs:
                    await asyncio.sleep(0.01)
                    continue

                now_t = time.time()
                now_t_ms = int(now_t * 1_000)

                # Check if we're paused bc of L1 congestion
                if self.l1_congested_paused_until is not None:
                    if now_t >= self.l1_congested_paused_until:
                        self.l1_congested_paused_until = None
                    else:
                        # We're paused, so sleep until end of pause
                        self.logger.warning(
                            f"Paused because of L1 congestion, holding "
                            f"{len(self.queued_cancel_pbs)} cancels.")
                        secs_to_end_of_pause = self.l1_congested_paused_until - now_t
                        await asyncio.sleep(secs_to_end_of_pause)
                        continue

                # Pick the connection with the most headroom
                conn = self.pick_best_connection(now_t)
                if conn is None:
                    # All connections exhausted / 429-paused — keep cancels, sleep till end of min
                    self.logger.warning(
                        f"All WS connections exhausted, holding "
                        f"{len(self.queued_cancel_pbs)} cancels.")
                    secs_to_end_of_min = 60 - (now_t % 60)
                    await asyncio.sleep(secs_to_end_of_min)
                    continue

                # Send cancels
                send_cancels_count += 1
                if send_cancels_count % 200 == 0:
                    self.logger.info(
                        f"Sending cancels on conn {conn.index}, "
                        f"1min {conn.num_batches_1min} batches, "
                        f"{conn.num_msgs_1min} msgs, {conn.num_weighted_1min} weighted.")
                await self.send_hyperliquid_cancel(conn, now_t_ms)

                # Sleep until we're allowed to send cancels again
                # If we're getting close to the rate limit (apparently 1200 like the REST limit, not
                # 2000 like HL docs says for websockets), sleep a bit longer
                if conn.num_weighted_1min > 1_100:
                    self.logger.warning(
                        f"Waiting to send cancels on conn {conn.index}, "
                        f"1min {conn.num_batches_1min} batches, "
                        f"{conn.num_msgs_1min} msgs, {conn.num_weighted_1min} weighted.")
                    await asyncio.sleep(0.3)
                else:
                    await asyncio.sleep(self.gap_between_send_s)

            except asyncio.CancelledError:
                self.logger.info("HL send cancels loop cancelled")
                raise
            except websockets.exceptions.ConnectionClosedOK:
                self.logger.warning("HL send cancels loop got ConnectionClosedOK")
                raise # retry
            except websockets.exceptions.ConnectionClosedError:
                self.logger.warning("HL send cancels loop got ConnectionClosedError")
                raise # retry
            except Exception as e:
                error_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                self.logger.error("Error in HL send cancels loop: {}".format(error_str))
                email_utils.send_alerts("WSGATEWAY SEND CANCELS ERROR, RECONNECTING", error_str)
                raise # retry
        self.logger.info("HL send cancels loop exiting")

    async def hl_sub(self, ws):
        try:
            # Subscribe to own fills.
            sub_userfill_json = json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "userFills", "user": self.listen_address},
                }
            )
            await ws.send(sub_userfill_json)

            # We don't actually need to subscribe to userEvents to get the STP callbacks,
            # but leaving this in case we want it for something else in the future. 
            # Subscribe to userevents (most importantly, nonusercancels. )
            #userevent_sub_json = json.dumps(
            #    {
            #        "method": "subscribe",
            #        "subscription": {"type": "userEvents", "user": self.wallet_address},
            #    }
            #)          
            #await ws.send(userevent_sub_json)


            # Subscribe to orderUpdates
            orderupdate_sub_json = json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "orderUpdates", "user": self.listen_address},
                }
            )
            await ws.send(orderupdate_sub_json)
        except Exception as e:
            self.logger.error(f"HL sub got exception: {e}")
            raise # retry (maybe shouldn't retry here?)

    def _mark_ignore_cancel(self, pkcoid):
        # setdefault keeps the *first* time we marked it dead, so the orphan
        # backstop's age check measures how long it's truly been considered dead.
        self.pkcoids_to_dead_t.setdefault(pkcoid, time.time())

    def _get_terminal_type(self, status, pkcoid):
        # Returns a string for the type of the terminal cancel error if it's terminal, otherwise
        # returns None for not terminal.
        if "never placed" not in status.get("error", ""):
            return None
        if pkcoid not in self.pkcoids_to_timings:
            return None
        # These are always initialized when the pkcoid is first inserted into pkcoids_to_timings:
        t_placed = self.pkcoids_to_timings[pkcoid]["t_placed"]
        t_resp = self.pkcoids_to_timings[pkcoid]["t_resp"]
        now_t = time.time()
        if pkcoid in self.pkcoids_to_hloids:
            # We got a "resting" order ack..
            if now_t - t_resp > 3:
                # ..at least 3s ago. Should avoid double-lapping case where our cancel to HL laps
                # our order, then their cancel reject back laps their order ack, but not a total
                # guarantee (saw cases where 1s was not enough), so in the early-terminal branch we
                # also fire an insurance cancel (covers even the "expired" case below).
                return "got resting ack"
            # But if it was very recent, might be that double-lapping case, so return None.
            self.logger.warning(f"Got a cancel reject for recently acked pkcoid {pkcoid}; "
                                f"possible double-lap, not treating as terminal")
            return None
        if now_t - t_placed > self.expired_order_thresh_s:
            # We sent the order out >3s ago and haven't heard back, so consider it expired.
            return "expired"
        return None

    async def _fetch_open_orders(self, session, listen_address, dex):
        # dex=None queries the default perps; dex="xyz" queries the unit perps.
        # We trade on both, so the backstop has to ask about both.
        payload = {"type": "openOrders", "user": listen_address}
        if dex:
            payload["dex"] = dex
        text = await hyp_fetch(session, self.market_endpoint + "info", payload)
        data = json.loads(text)
        return data if isinstance(data, list) else []

    async def _check_orphaned_orders(self):
        # Ask HL which of our orders are still resting and compare against the
        # set we believe is dead (pkcoids_to_dead_t). Anything HL still shows
        # open that we marked dead >60s ago is an orphan: a cancel that was
        # silently dropped (the order/cancel double-lap is the known cause). We
        # force-cancel it past the normal send-path short-circuits and alert,
        # because at >60s it can't be a cancel that's merely still in flight.
        #
        # Scope note: matching uses in-memory maps (hl_hexoids_to_pk_cloids,
        # populated at send; hloids_to_pkcoids, at the resting ack) and
        # pkcoids_to_dead_t, all of which reset on restart — so this only
        # covers orders from the current process, which is exactly the scope of
        # the bookkeeping it's protecting.
        orphan_grace_s = 60
        listen_address = (self.listen_address or self.subaccount_address
                          or self.wallet_address)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                self._fetch_open_orders(session, listen_address, self.unit_market),
                self._fetch_open_orders(session, listen_address, None),
            )

        now_t = time.time()
        orphans = {}  # pkcoid -> HL oid, for logging/alerting
        for one_order in (o for sub in results for o in sub):
            # openOrders returns both cloid (our hl_hexoid) and oid (our hloid);
            # the hl_hexoid is the more robust match since it's mapped at send time.
            hl_hexoid = one_order.get("cloid")
            pkcoid = self.hl_hexoids_to_pk_cloids.get(hl_hexoid) if hl_hexoid else None
            if pkcoid is None:
                pkcoid = self.hloids_to_pkcoids.get(one_order.get("oid"))
            if pkcoid is None:
                continue  # not ours (or from a previous process)
            marked_dead_t = self.pkcoids_to_dead_t.get(pkcoid)
            if marked_dead_t is not None and now_t - marked_dead_t > orphan_grace_s:
                orphans[pkcoid] = one_order.get("oid")

        if not orphans:
            return

        for pkcoid in orphans:
            deets = self.pkcoids_to_orderdeets[pkcoid]
            cancel_holder = gateway_pb2.PbMessage()
            cancel_pb = cancel_holder.cancel_order
            cancel_pb.strategy_id = deets["strategy_id"]
            cancel_pb.executor_order_id = deets["executor_order_id"]
            self.queued_cancel_pbs.append(cancel_pb)
            self.force_cancel_pkcoids.add(pkcoid)

        msg = (f"HL still has these orders resting though we marked them dead "
               f">{orphan_grace_s}s ago; force-cancelling. pkcoid->oid: {orphans}")
        self.logger.error(f"Orphaned orders: {msg}")
        email_utils.send_alerts("WSGATEWAY ORPHANED ORDERS", msg)

    async def hl_receive_loop(self, conn):
        self.logger.info(f"HL receive loop starting (conn {conn.index})")
        # Only the primary connection subscribes to userFills/orderUpdates
        # (avoids duplicates and saves message budget on secondary connections)
        if conn.index == 0:
            await self.hl_sub(conn.ws)
        # When we first connect, we get a dump of the last few
        # fills. We don't want to process those. So just tracking
        # when the first one is.
        # TODO: what about initial dump of orderUpdates?
        handled_first_userfills = False
        rtt_log_count = 0
        cxl_ack_log_count = 0
        while not self.eod_event.is_set():
            try:
                # No need to time out to send orders or pings, since we're doing those in separate
                # coroutines
                message = await conn.ws.recv()
                msg_dct = json.loads(message)

                # Otherwise, parse websocket response.
                #self.logger.info("ws msg: {}".format(msg_dct))

                if msg_dct["channel"] == "pong":
                    # Gonna track this so we can check that they got this. 
                    #self.logger.info("pong got (conn {})".format(conn.index))
                    continue
                elif msg_dct["channel"] == "userFills":
                    if not handled_first_userfills:
                        handled_first_userfills = True
                        continue
                    else:
                        # Handle the fills.
                        fills_to_handle = self.queued_fills + msg_dct["data"]["fills"]
                        self.queued_fills = []

                    # print("Length is {}".format(len(msg_dct["data"]["fills"])))
                    for one_fill in fills_to_handle:
                        px = one_fill["px"]
                        sz = one_fill["sz"]
                        hloid = one_fill["oid"]
                        # Our own filled version of this.
                        hl_hexoid = None
                        if "cloid" in one_fill:
                            hl_hexoid = one_fill["cloid"]
                        tid = one_fill["tid"]
                        # print("Handling exec oid {}".format(oid))

                        # If one of OUR positions was liquidated (not just us being the
                        # contra to someone else's liquidation), block new orders in that
                        # symbol and alert. A contra fill also has "liquidation" set but a
                        # different liquidatedUser, so it falls through to normal exec
                        # handling below.
                        liq = one_fill.get("liquidation")
                        if (liq is not None and liq.get("liquidatedUser", "").lower()
                                == self.listen_address.lower()):
                            coin = one_fill["coin"]
                            if coin not in self.liq_blocked_syms:
                                self.liq_blocked_syms.add(coin)
                                self._append_block_file(coin)
                                body = (f"OUR POSITION LIQUIDATED on {coin}: "
                                        f"side={one_fill['side']} sz={sz} px={px} (tid={tid}). "
                                        f"Blocking new orders in {coin} (cancels still "
                                        f"allowed). To unblock, delete its line from "
                                        f"{self.block_file}.")
                                self.logger.error(body)
                                email_utils.send_alerts(
                                    f"WSGATEWAY LIQUIDATION — {coin} BLOCKED", body)
                            continue

                        # Probably an IOC in this case. Or maybe a stale oid.
                        # TODO: make this more robust.
                        should_skip = False
                        if hloid not in self.hloids_to_pkcoids and (not (hl_hexoid and hl_hexoid in self.hl_hexoids_to_pk_cloids)):
                            # Don't handle backlog for old results.
                            should_skip = True

                        # Duplicate
                        if tid in self.handled_tids_to_t:
                            continue

                        if should_skip:
                            # Don't log because fills from other gateways are too common.
                            #self.logger.info(
                            #    "Skipped fill for order {} hlcloid {} because could not find, dets are {}".#format(
                            #        hloid, hl_hexoid, one_fill
                            #    )
                            #)

                            if time.time() - one_fill["time"] / 1000 < 5:
                                self.queued_fills.append(one_fill)
                            continue
                        #else:
                        #    self.logger.info("Got fill for order {} hlcloid {}".format(hloid, hl_hexoid))


                        # Otherwise, deal with it.
                        self.handled_tids_to_t[tid] = int(time.time())

                        # Look up the pkcoid and stuff.
                        if hloid in self.hloids_to_pkcoids:
                            pk_coid = self.hloids_to_pkcoids[hloid]
                        elif hl_hexoid and hl_hexoid in self.hl_hexoids_to_pk_cloids:
                            # I guess this can happen if we immediately get filled.
                            pk_coid = self.hl_hexoids_to_pk_cloids[hl_hexoid]
                        else:
                            continue

                        strat_id = self.pkcoids_to_orderdeets[pk_coid]["strategy_id"]
                        exec_id = self.pkcoids_to_orderdeets[pk_coid][
                            "executor_order_id"
                        ]

                        add_liq = not one_fill["crossed"]

                        # Create an exec and publish it.
                        pbresponse = gateway_pb2.PbMessage()
                        exec = pbresponse.execution
                        exec.strategy_id = strat_id
                        exec.executor_order_id = exec_id
                        exec.trade_qty = sz
                        exec.trade_px = px
                        exec.add_liq = add_liq

                        # print("Gateway sending exec: ")
                        # print(exec)

                        # Serialize and publish.
                        pub_string = pbresponse.SerializeToString()
                        self.acks_pub_socket.send(pub_string)

                        # Record that we got an exec for this, so we don't possibly
                        # requeue cancels if we get the "never placed..." error. 
                        self.recent_exec_pkcoids.add(pk_coid)


                elif msg_dct["channel"] == "orderUpdates":
                    #self.logger.info("SPECIAL UPDATE, orderUPDATES {}".format(msg_dct))

                    # for some reason, when it's a market order causing the stp, 
                    # we only hear about the cancel back inside the orderupdates
                    # channel and not the "user" channel. Oh well. 
                    # Let's potentially do this too. 
                    # Example
                    # {'channel': 'orderUpdates', 'data': [{'order': {'coin': 'z:XYZ100', 'side': 'B', 'limitPx': '26464.0', 'sz': '1.0', 'oid': 39784561743, 'timestamp': 1759026385365, 'origSz': '1.0'}, 'status': 'iocCancelRejected', 'statusTimestamp': 1759026385365}, {'order': {'coin': 'z:XYZ100', 'side': 'A', 'limitPx': '24515.0', 'sz': '1.35', 'oid': 39784512877, 'timestamp': 1759026289734, 'origSz': '1.35', 'cloid': '0xa367919e0f2814c68b2615ca4f6ed73e'}, 'status': 'selfTradeCanceled', 'statusTimestamp': 1759026385365}, {'order': {'coin': 'z:XYZ100', 'side': 'A', 'limitPx': '24526.0', 'sz': '7.72', 'oid': 39784512879, 'timestamp': 1759026289734, 'origSz': '7.72', 'cloid': '0x626cc8f64c268e037a980797a6432db9'}, 'status': 'selfTradeCanceled', 'statusTimestamp': 1759026385365}, {'order': {'coin': 'z:XYZ100', 'side': 'A', 'limitPx': '24540.0', 'sz': '6.9', 'oid': 39784513092, 'timestamp': 1759026290337, 'origSz': '6.9', 'cloid': '0xb2fe89cff0f0295621a54e462390ffca'}, 'status': 'selfTradeCanceled', 'statusTimestamp': 1759026385365}, {'order': {'coin': 'z:XYZ100', 'side': 'A', 'limitPx': '24559.0', 'sz': '7.87', 'oid': 39784513326, 'timestamp': 1759026290946, 'origSz': '7.87', 'cloid': '0x8087af6cc5484acc4b5b8de90512a3ff'}, 'status': 'selfTradeCanceled', 'statusTimestamp': 1759026385365}, {'order': {'coin': 'z:XYZ100', 'side': 'A', 'limitPx': '24583.0', 'sz': '6.57', 'oid': 39784513539, 'timestamp': 1759026291547, 'origSz': '6.57', 'cloid': '0xd13d07cd19b334a6bdc95c23591a0beb'}, 'status': 'selfTradeCanceled', 'statusTimestamp': 1759026385365}, {'order': {'coin': 'z:XYZ100', 'side': 'A', 'limitPx': '24614.0', 'sz': '6.55', 'oid': 39784513644, 'timestamp': 1759026292150, 'origSz': '6.55', 'cloid': '0xdfdcd67e70c15b0bf2e0129f57683078'}, 'status': 'selfTradeCanceled', 'statusTimestamp': 1759026385365}]}

                    # Handle status "canceled" here too even though it'll double the "post"
                    # channel handling for all our normal cancels, because manual cancels from
                    # the UI or any external source would only come through here.
                    handle_statuses = {"selfTradeCanceled", "canceled"}
                    ignore_statuses = {"iocCanceled", "iocCancelRejected"}
                    for one_order_update in msg_dct["data"]:
                        status = one_order_update["status"]
                        if status not in handle_statuses:
                            if "cancel" in status.lower() and status not in ignore_statuses:
                                # For any other unexpected cancel types, log it
                                self.logger.info(f"Got unexpected cancel type: {one_order_update}")
                            continue
                        # Otherwise, check if it's one of ours.
                        hl_hexoid = one_order_update["order"].get("cloid")
                        if not hl_hexoid:
                            # Order wasn't sent with a "cloid" field, so not from any gateway.
                            # Possibly a manual order that was canceled? Weird, let's log.
                            self.logger.info(f"Got cancel without cloid: {one_order_update}")
                            continue
                        if hl_hexoid not in self.hl_hexoids_to_pk_cloids:
                            # Probably an order from a different gateway. Safe to ignore.
                            continue
                        # Order was sent from this gateway, and we have the pk_coid.
                        pk_coid = self.hl_hexoids_to_pk_cloids[hl_hexoid]
                        # Potentially skip this if we've already registered the cancel.
                        if pk_coid in self.pkcoids_to_dead_t:
                            continue
                        # Otherwise, send the cancelack. 
                        pbresponse = gateway_pb2.PbMessage()
                        new_cxl = pbresponse.cancel_ack

                        strat_id = self.pkcoids_to_orderdeets[pk_coid]["strategy_id"]
                        executor_id = self.pkcoids_to_orderdeets[pk_coid][
                            "executor_order_id"
                        ]

                        new_cxl.strategy_id = strat_id
                        new_cxl.executor_order_id = executor_id

                        pub_string = pbresponse.SerializeToString()
                        self.acks_pub_socket.send(pub_string)

                        # Don't cancel this in the future because it got cancelled.
                        self._mark_ignore_cancel(pk_coid)

                elif msg_dct["channel"] == "user":
                    #self.logger.info("Got user channel message {}".format(msg_dct))
                # Example message: 
                #Received message: {'channel': 'user', 'data': {'nonUserCancel': [{'coin': 't:XYZ500', 'oid': 39772058445}, {'coin': 't:XYZ500', 'oid': 39772056730}, {'coin': 't:XYZ500', 'oid': 39772051624}]}}
                    msg_data = msg_dct["data"]
                    if "nonUserCancel" in msg_data:
                        # Iterate through them. 
                        for one_non_user_cancel in msg_data["nonUserCancel"]:
                            hloid = one_non_user_cancel["oid"]
                            if hloid not in self.hloids_to_pkcoids:
                                self.logger.info("Got non-user cancel for unknown hloid {}".format(hloid))
                                # Hit some logging. 
                                continue
                            # Otherwise, we have a pkcoid we can match to. 

                            pk_coid = self.hloids_to_pkcoids[hloid]
                            # Potentially skip this if we've already registered the cancel.
                            if pk_coid in self.pkcoids_to_dead_t:
                                continue
                            # Create the cancel                            
                            pbresponse = gateway_pb2.PbMessage()
                            new_cxl = pbresponse.cancel_ack

                            strat_id = self.pkcoids_to_orderdeets[pk_coid]["strategy_id"]
                            executor_id = self.pkcoids_to_orderdeets[pk_coid][
                                "executor_order_id"
                            ]

                            new_cxl.strategy_id = strat_id
                            new_cxl.executor_order_id = executor_id

                            pub_string = pbresponse.SerializeToString()
                            self.acks_pub_socket.send(pub_string)

                            # Don't cancel this in the future because it got cancelled.
                            self._mark_ignore_cancel(pk_coid)

                elif msg_dct["channel"] == "post":
                    # This is a response to either a neword or a cancel.
                    msg_data = msg_dct["data"]
                    msg_id = msg_data["id"]
                    msg_response = msg_data["response"]

                    # HIP-4 capitalization split responses are on their own track (keyed by
                    # ws_msg_idx in ws_id_to_cap_sym), so intercept before the order/cancel
                    # matching (which would KeyError on ws_id_to_pk_cloids_batch[msg_id]).
                    if msg_id in self.ws_id_to_cap_sym:
                        self._handle_cap_response(msg_id, msg_response)
                        continue

                    # This is so annoying tbh.
                    response_type = msg_response["type"]

                    # Do timing stuff later.

                    # Go deeper in the msg_response, but guard against bad payloads.
                    payload = msg_response.get("payload")
                    if not isinstance(payload, dict):
                        if payload == "429 Too Many Requests":
                            body = f"Conn {conn.index} received 429"
                            # In case the 429 triggered right at the end of the minute on HL's clock
                            # and we received it at the start of the next minute on our clock, give
                            # it some wiggle room
                            secs_into_min = time.time() % 60
                            if secs_into_min < 5: # give it a 5s buffer
                                body += f": {secs_into_min:.1f}s < 5s into minute, ignoring."
                                # The offending traffic was in the minute that just ended, and
                                # the live counters were zeroed at the rollover, so report the
                                # snapshot of that minute instead.
                                num_batches = conn.prev_num_batches_1min
                                num_msgs = conn.prev_num_msgs_1min
                                num_weighted = conn.prev_num_weighted_1min
                            else:
                                conn.paused_429 = True
                                body += ": pausing until end of minute."
                                num_batches = conn.num_batches_1min
                                num_msgs = conn.num_msgs_1min
                                num_weighted = conn.num_weighted_1min
                            body += (f" 1min counts: {num_batches} batches, "
                                     f"{num_msgs} msgs, {num_weighted} weighted.")
                            self.logger.error(body)
                            email_utils.send_alerts("WSGATEWAY GOT 429", body)
                        else:
                            self.logger.error(
                                "Unexpected payload type %s in msg_response %s",
                                type(payload).__name__, msg_response,
                            )
                        continue

                    response_inner_response = payload.get("response")
                    if response_inner_response is None:
                        self.logger.error(
                            "Missing inner response in msg_response %s", msg_response
                        )
                        continue

                    # 2025-06-02 11:27:41,230 - Got error 'string indices must be integers' when parsing {'type': 'action', 'payload': {'status': 'err', 'response': 'Invalid nonce: duplicate nonce 1748863660821'}}
                    if "duplicate nonce" in response_inner_response:
                        # Then we probably got a duplicate nonce.
                        self.logger.info("Prob got a duplicate nonce: {}".format(msg_response))
                        continue

                    # 2026-04-07 20:00:19,380 - Got error 'string indices must be integers' when parsing {'type': 'action', 'payload': {'status': 'err', 'response': 'L1 is congested. Action not sent to L1.'}}
                    # 2026-04-27 11:32:23,963 - Got error 'string indices must be integers' when parsing {'type': 'action', 'payload': {'status': 'err', 'response': 'L1 is congested. Rate limited based on master account maker share.'}}
                    if "L1 is congested" in response_inner_response:
                        # L1 is congested and they're throttling everyone
                        err_msg = f"{response_inner_response} Backing off."
                        self.logger.error(err_msg)
                        email_utils.send_alerts("L1 CONGESTED", err_msg)
                        self.clear_all_orders("L1 congested")
                        self.l1_congested_paused_until = time.time() + self.l1_congested_backoff_s
                        continue

                    try:
                        inner_response_type = response_inner_response["type"]
                    except Exception as e:
                        self.logger.error("Got error '{}' when parsing {}".format(e, msg_response))

                        # If it's bad, like, we have a quota issue, then don't do it.
                        # 2025-05-08 12:48:18,376 - Got error string indices must be integers when parsing {'type': 'action', 'payload': {'status': 'err', 'response': 'Too many cumulative requests sent (2887581 > 2807233) for cumu
                        # lative volume traded $2797234.88019. Place taker orders to free up 1 request per USDC traded.'}}
                        str_msg_response = str(msg_response)
                        if "err" in str_msg_response and "cumulative" in str_msg_response:
                            self.logger.error("I think we need to take a break for L1 reasons")
                            email_utils.send_alerts("WSGATEWAY QUOTA ERROR, EXITING", str_msg_response)
                            self.eod_event.set() # initiate exit, don't retry
                            return
                        continue


                    #  {'channel': 'post', 'data': {'id': 0, 'response': {'type': 'action', 'payload': {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [{'error': 'Post only order would have immediately matched, bbo was 84350@84361. asset=3'}]}}}}}}
                    #self.logger.info("inner_response_type {} ==s are  {} {}".format(inner_response_type, inner_response_type=="order", inner_response_type=="cancel"))

                    # The pkcloids are here:
                    batch_cloids = self.ws_id_to_pk_cloids_batch[msg_id]

                    if inner_response_type == "order":
                        # Go through and map the orders.
                        response_statuses = response_inner_response["data"]["statuses"]
                        if len(batch_cloids) != len(response_statuses):
                            # Mismatched lengths means we can't trust the index-based
                            # pairing below — skip rather than risk associating a status
                            # with the wrong cloid. Log full contents so we can
                            # investigate offline.
                            self.logger.error(
                                "len(batch_cloids) {} != len(response_statuses) {}; "
                                "batch_cloids={} response_statuses={}".format(
                                    len(batch_cloids), len(response_statuses),
                                    batch_cloids, response_statuses))
                            continue
                        for idx, one_status in enumerate(response_statuses):

                            og_pk_cloid = batch_cloids[idx]
                            ## Don't double count things.
                            if og_pk_cloid in self.pkcoids_to_timings and "rtt" not in self.pkcoids_to_timings[og_pk_cloid]:
                                timing_dct = self.pkcoids_to_timings[og_pk_cloid]
                                timing_dct["t_resp"] = time.time()
                                timing_dct["rtt"] = timing_dct["t_resp"] - timing_dct["t_placed"]
                                self._record_rtt(timing_dct["tif"], timing_dct["rtt"])
                                if rtt_log_count % 200 == 0:
                                    self.logger.info("RTT for {}: {}".format(og_pk_cloid, timing_dct))
                                rtt_log_count += 1

                            if "resting" in one_status:
                                #self.logger.info("resting status is like {}".format(one_status))
                                hloid = one_status["resting"]["oid"]
                                # Store this but also send an ack.

                                self.pkcoids_to_hloids[og_pk_cloid] = hloid
                                self.hloids_to_pkcoids[hloid] = og_pk_cloid
                                #self.logger.info("Inner rest hloid {} pk_coid {}".format(hloid, og_pk_cloid))

                                # Update our oid responses. For example
                                # we should create the ack.
                                #self.cloids_to_oids[cur_orderpb.executor_order_id] = resting_oid
                                #self.logger.info("Resting oid {} for {}".format(hl_hexoid, og_pk_cloid))

                                pbresponse = gateway_pb2.PbMessage()
                                new_ack = pbresponse.new_ack
                                new_ack.strategy_id = self.pkcoids_to_orderdeets[og_pk_cloid]["strategy_id"]
                                new_ack.executor_order_id = self.pkcoids_to_orderdeets[og_pk_cloid]["executor_order_id"]
                                new_ack.pk_coid = og_pk_cloid
                                new_ack.exch_oid = str(hloid)

                                pub_string = pbresponse.SerializeToString()
                                self.acks_pub_socket.send(pub_string)


                            elif "error" in one_status:
                                # Grab the orderdeets then.
                                order_deets = self.pkcoids_to_orderdeets[og_pk_cloid]
                                err_str = "Error with {}: {}".format(
                                    order_deets,
                                    one_status["error"],
                                )
                                # Too many of these, due to ioc misses. Don't print.
                                if "immediately" not in err_str:
                                    self.logger.info(err_str)
                                # If it's an insufficient margin reject, send alerts.
                                if "Insufficient margin" in err_str:
                                    email_utils.send_alerts(
                                        "WSGATEWAY GOT INSUFFICIENT MARGIN REJECT", err_str)
                                # Create a reject.
                                pbresponse = gateway_pb2.PbMessage()
                                new_rej = pbresponse.new_reject
                                new_rej.strategy_id = order_deets["strategy_id"]
                                new_rej.executor_order_id = order_deets["executor_order_id"]
                                new_rej.reason = err_str
                                # Serialize and publish.
                                pub_string = pbresponse.SerializeToString()
                                self.acks_pub_socket.send(pub_string)

                                # If this order was inside the cancel queue,
                                # remove it (because it is implicitly cancelled)

                                to_remove_cancel = None
                                for one_cancel_pb in self.queued_cancel_pbs:
                                    if one_cancel_pb.strategy_id == order_deets["strategy_id"] and one_cancel_pb.executor_order_id == order_deets["executor_order_id"]:
                                        to_remove_cancel = one_cancel_pb
                                        break

                                if to_remove_cancel is not None:
                                    self.queued_cancel_pbs.remove(to_remove_cancel)
                                    # Preemptively remove a cancel that shouldnt have to be.
                                    self.logger.info("Removing cancel for {} because never went live".format(og_pk_cloid))
                                else:
                                    # Dont retry a cancel for this because it never actually posted.
                                    # But we didn't send a cancel for these yet.
                                    self._mark_ignore_cancel(og_pk_cloid)

                                # Maybe log this.
                                #self.logger.info("Error for {}: {}".format(og_pk_cloid, one_status))

                            elif "filled" in one_status:
                                # We got filled off an aggressive move.
                                # Docs are here:
                                #https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
                                order_deets = self.pkcoids_to_orderdeets[og_pk_cloid]

                                hloid = one_status["filled"]["oid"]
                                sz_filled = one_status["filled"]["totalSz"]
                                avg_px = one_status["filled"]["avgPx"]

                                # Don't publish this to the strat because we can double-count
                                # fills. Instead, let's listen to the userFills stream
                                # for the actual execs. This is also because
                                # there is no tid associated with this fill, so
                                # we won't necessarily know how to dedu.

                                #pbresponse = gateway_pb2.PbMessage()
                                #new_exec = pbresponse.execution

                                #new_exec.strategy_id = order_deets["strategy_id"]
                                #new_exec.executor_order_id = order_deets["executor_order_id"]
                                #new_exec.trade_qty = sz_filled
                                #new_exec.trade_px = avg_px
                                #new_exec.feed_trade_id = 0
                                #new_exec.exch_transact_time = 0
                                #new_exec.add_liq = False

                                # Push it back to the strat.
                                #pub_string = pbresponse.SerializeToString()
                                #self.acks_pub_socket.send(pub_string)

                                # Store the trade id.


                                # Check if it was fully filled. If it was less than
                                # the full ordersize, send an elim.
                                if float(sz_filled) < float(order_deets["qty"]) and order_deets["tif"] == "Ioc":
                                    elimresponse = gateway_pb2.PbMessage()
                                    new_elim = elimresponse.elim
                                    new_elim.strategy_id = order_deets["strategy_id"]
                                    new_elim.executor_order_id = order_deets["executor_order_id"]

                                    # Send the elim back.
                                    elim_pub_string = elimresponse.SerializeToString()
                                    self.acks_pub_socket.send(elim_pub_string)
                                    # Send an elim.

                                    # Don't cancel this because it was fully executed.
                                    self._mark_ignore_cancel(og_pk_cloid)



                    elif inner_response_type == "cancel":
                        #self.logger.info("Got cancel response: {}".format(msg_response))

                        # One RTT per cancel batch round-trip. pop() regardless of
                        # which path below we take, so the send-time dict can't leak.
                        cxl_send_t = self.ws_id_to_cancel_send_t.pop(msg_id, None)
                        if cxl_send_t is not None:
                            self._record_rtt("Cxl", time.time() - cxl_send_t)

                        inner_str = str(response_inner_response)
                        if "err" in inner_str and "nonce" in inner_str:
                            # Then we probably got a duplicate nonce.
                            self.logger.info("Prob got a duplicate nonce: {}".format(msg_response))
                            continue

                        try:
                            response_statuses = response_inner_response["data"]["statuses"]
                        except Exception as e:
                            self.logger.info("Bad cancel response: {}".format(msg_response))
                            continue
                        if len(batch_cloids) != len(response_statuses):
                            # Same hazard as the order-response branch: index-based pairing
                            # below would misalign cloids with statuses. Skip and log the
                            # full contents for offline investigation.
                            self.logger.error(
                                "len(batch_cloids) {} != len(response_statuses) {} (cancel); "
                                "batch_cloids={} response_statuses={}".format(
                                    len(batch_cloids), len(response_statuses),
                                    batch_cloids, response_statuses))
                            continue
                        for idx, one_status in enumerate(response_statuses):
                            og_pk_cloid = batch_cloids[idx]
                            order_deets = self.pkcoids_to_orderdeets[og_pk_cloid]

                            if "success" in one_status:
                                # Sometimes the "orderUpdates" channel beats the "post" channel, so
                                # we might have already sent a cancel ack for this.
                                if og_pk_cloid in self.pkcoids_to_dead_t:
                                    # Happens a lot so only log every 100.
                                    if cxl_ack_log_count % 100 == 0:
                                        self.logger.info(
                                            f"Got cancel ack from HL for {og_pk_cloid} "
                                            f"but already acked query")
                                    cxl_ack_log_count += 1
                                    continue

                                # Otherwise this is the main cancel ack case, so send the ack
                                pbresponse = gateway_pb2.PbMessage()
                                new_cxl = pbresponse.cancel_ack

                                strat_id = self.pkcoids_to_orderdeets[og_pk_cloid]["strategy_id"]
                                executor_id = self.pkcoids_to_orderdeets[og_pk_cloid][
                                    "executor_order_id"
                                ]

                                new_cxl.strategy_id = strat_id
                                new_cxl.executor_order_id = executor_id

                                pub_string = pbresponse.SerializeToString()
                                self.acks_pub_socket.send(pub_string)

                                # Don't cancel this in the future because it got cancelled.
                                self._mark_ignore_cancel(og_pk_cloid)

                            elif "error" in one_status:
                                self.logger.info("Error from cancel for {}: {}".format(og_pk_cloid, one_status))

                                # Potentially retry the cancel. We might have had a race condition w/ the original placement.
                                if og_pk_cloid in self.pkcoids_to_dead_t or og_pk_cloid in self.recent_exec_pkcoids:
                                    self.logger.info("Ignoring cancel error for {} because it should be ignored".format(og_pk_cloid))

                                elif terminal_type := self._get_terminal_type(one_status, og_pk_cloid):
                                    # HL's "Order was never placed, already canceled, or filled" error
                                    # looks terminal (order acked >3s ago, or sent > 3s ago and never
                                    # acked), so we ack the query immediately and don't keep retrying
                                    # the cancel. But to be sure we also send one final insurance
                                    # cancel.
                                    pbresponse = gateway_pb2.PbMessage()
                                    new_cxl = pbresponse.cancel_ack

                                    strat_id = self.pkcoids_to_orderdeets[og_pk_cloid]["strategy_id"]
                                    executor_id = self.pkcoids_to_orderdeets[og_pk_cloid][
                                        "executor_order_id"
                                    ]

                                    new_cxl.strategy_id = strat_id
                                    new_cxl.executor_order_id = executor_id

                                    pub_string = pbresponse.SerializeToString()
                                    self.acks_pub_socket.send(pub_string)
                                    self.logger.info(f"Sending cxlack early for {og_pk_cloid} "
                                                     f"(terminal state on HL: {terminal_type})")

                                    # Don't cancel this in the future because it won't work anyway.
                                    self._mark_ignore_cancel(og_pk_cloid)

                                    # Insurance cancel (see above): one-shot via force_cancel_pkcoids,
                                    # so it bypasses the send-path short-circuits exactly once. Its
                                    # response is swallowed by the pkcoids_to_dead_t guard at the top of
                                    # this handler (success -> "already acked query"; error ->
                                    # "ignoring"), so it cannot re-enter this branch or loop.
                                    insurance_holder = gateway_pb2.PbMessage()
                                    insurance_cancel = insurance_holder.cancel_order
                                    insurance_cancel.strategy_id = strat_id
                                    insurance_cancel.executor_order_id = executor_id
                                    self.queued_cancel_pbs.append(insurance_cancel)
                                    self.force_cancel_pkcoids.add(og_pk_cloid)

                                elif self.pkcoid_cxl_count.get(og_pk_cloid, 0) > 5:
                                    # Then just send the cancelack anyway.
                                    pbresponse = gateway_pb2.PbMessage()
                                    new_cxl = pbresponse.cancel_ack

                                    strat_id = self.pkcoids_to_orderdeets[og_pk_cloid]["strategy_id"]
                                    executor_id = self.pkcoids_to_orderdeets[og_pk_cloid][
                                        "executor_order_id"
                                    ]

                                    new_cxl.strategy_id = strat_id
                                    new_cxl.executor_order_id = executor_id

                                    pub_string = pbresponse.SerializeToString()
                                    self.acks_pub_socket.send(pub_string)
                                    self.logger.info("Sending cxlack anyway for {}".format(og_pk_cloid))

                                    # Don't cancel this in the future because it won't work anyway.
                                    self._mark_ignore_cancel(og_pk_cloid)

                                else:
                                    # Add it to the cancel queue again.
                                    re_act_pb = gateway_pb2.PbMessage()
                                    re_cancel_pb = re_act_pb.cancel_order
                                    re_cancel_pb.strategy_id = self.pkcoids_to_orderdeets[og_pk_cloid]["strategy_id"]
                                    re_cancel_pb.executor_order_id = self.pkcoids_to_orderdeets[og_pk_cloid]["executor_order_id"]
                                    self.queued_cancel_pbs.append(re_cancel_pb)

                    else:
                        self.logger.info("Did not understand response: {}".format(response_inner_response))

            except asyncio.CancelledError:
                self.logger.info(f"HL receive loop cancelled (conn {conn.index})")
                raise # this will not retry
            except websockets.exceptions.ConnectionClosedOK:
                self.logger.warning(f"HL receive loop got ConnectionClosedOK (conn {conn.index})")
                raise # retry
            except websockets.exceptions.ConnectionClosedError:
                self.logger.warning(f"HL receive loop got ConnectionClosedError (conn {conn.index})")
                raise # retry
            except Exception as e:
                # Sometimes the websocket can be flaky, raise so we
                # reconnect.
                error_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                self.logger.error(f"Error from websocket (conn {conn.index}): {error_str}")
                email_utils.send_alerts("WSGATEWAY RECEIVE ERROR, RECONNECTING", error_str)
                raise # retry
        self.logger.info(f"HL receive loop exiting (conn {conn.index})")


    def get_next_nonce(self, cur_t_ms):
       
        # Translate cur_ms into nonce_ms associated to my gateway_id 
        # This is the largest nonce_ms we can use 
        cur_nonce_ms = cur_t_ms - (cur_t_ms % self.max_num_gateways) + self.gateway_id

        # Look backwards to check if we can use an older one. 
        nonce_contender = cur_nonce_ms - 90 
        if nonce_contender <= self.last_nonce_used:
            nonce_contender = self.last_nonce_used + self.max_num_gateways

        # If it will go into the future, reject. 
        if nonce_contender > cur_nonce_ms:
            return 0
        return nonce_contender


    # Does batch sending
    def _asset_num(self, symbol):
        """Resolve the Hyperliquid asset id for an order/cancel symbol.

        HIP-4 outcome coins ("#<encoding>") are NOT in sym_to_idx -- that map is built from the
        perp `meta` and `spotMeta`, never `outcomeMeta`. Their asset id is the fixed offset
        OUTCOME_ASSET_OFFSET + encoding, where encoding = 10*outcome + side (the same integer in
        the coin name after the '#'). Everything else is a normal perp/spot symbol.
        """
        if symbol.startswith("#"):
            return 100_000_000 + int(symbol[1:])
        return self.sym_to_idx[symbol]

    async def send_hyperliquid_orders(self, conn, now_ms, queue_name):
        """queue_name in {"priority", "fast"}."""
        #endpt = self.market_endpoint + "exchange"

        if queue_name == "priority":
            ord_pbs = self.queued_priority_ord_pbs
        elif queue_name == "fast":
            ord_pbs = self.queued_fast_ord_pbs
        else:
            raise ValueError(f"Unknown queue_name: {queue_name!r}")

        if len(ord_pbs) == 0:
            return

        # Do a quick nonce check. If we can't process it, we can't process it. 
        next_nonce = self.get_next_nonce(now_ms)
        if next_nonce == 0:
            # No space for nonces. 
            self.logger.info("Skipping newords because no nonce space")
            return
        # Otherwise, store it. 
        self.last_nonce_used = next_nonce

        # Detach this batch before any await. New ZMQ arrivals can append to
        # the live queue while conn.ws.send() is pending and must survive for
        # the next send loop.
        batch_ord_pbs = list(ord_pbs)
        ord_pbs.clear()

        all_order_wires = []
        send_t = time.time()

        pk_coids_this_batch = []

        for one_order_pb in batch_ord_pbs:
            tif_str = "Gtc"
            if one_order_pb.time_in_force == 2:
                tif_str = "Ioc"
            elif one_order_pb.time_in_force == 4:
                tif_str = "Alo"


            # Record that we got it.
            pk_coid = "{}-{}".format(
                one_order_pb.strategy_id, one_order_pb.executor_order_id
            )
            self.pkcoids_to_timings[pk_coid] = {"t_placed": send_t, "t_resp": 0, "tif": tif_str}
            pk_coids_this_batch.append(pk_coid)

            self.pkcoids_to_orderdeets[pk_coid] = {
                "strategy_id": one_order_pb.strategy_id,
                "executor_order_id": one_order_pb.executor_order_id,
                "symbol": one_order_pb.symbol,
                "tif": tif_str,
                "qty": one_order_pb.qty,
            }
            self.pkcoid_cxl_count[pk_coid] = 0


            is_buy = True
            if one_order_pb.side == 2:
                is_buy = False

            order_dct = {
                "coin": one_order_pb.symbol,
                "is_buy": is_buy,
                "sz": float(one_order_pb.qty),
                "limit_px": float(one_order_pb.px),
                "order_type": {"limit": {"tif": tif_str}},
                "reduce_only": False,
            }

            asset_num = self._asset_num(one_order_pb.symbol)
            order_wire = hl_signing.order_request_to_order_wire(
                order_dct, asset_num
            )

            hl_hexoid = hash_to_128bit_hex(pk_coid)
            order_wire["c"]= hl_hexoid

            # Make the reverse maps.
            self.pk_cloids_to_hl_hexoids[pk_coid] = hl_hexoid
            self.hl_hexoids_to_pk_cloids[hl_hexoid] = pk_coid
            #self.logger.info("Did update the pk_cloids_to_hl_hexoids: {}".format(self.pk_cloids_to_hl_hexoids))
            all_order_wires.append(order_wire)


        # Then, send it the whole batch.
        order_actions = hl_signing.order_wires_to_order_action(all_order_wires)
        signature = hl_signing.sign_l1_action(
            self.eth_wallet,
            order_actions,
            self.subaccount_address,  # Use subaccount if configured, None for main account
            next_nonce,
            self.is_mainnet,
        )

        # OK, then we just need this:

        payload = {
            "action": order_actions,
            "nonce": next_nonce,
            "signature": signature,
            "vaultAddress": self.subaccount_address,  # Use subaccount if configured, None for main account
        }

        ws_payload = {
            "method": "post",
            "id": self.ws_msg_idx,
            "request": {
                "type": "action",
                "payload": payload,
            }
        }

        # Update per-connection 1min counts
        conn.num_batches_1min += 1
        conn.num_msgs_1min += len(all_order_wires)
        conn.num_weighted_1min += 1 + len(all_order_wires) // 40
        if queue_name == "priority":
            conn.num_priority_batches_1min += 1

        self.ws_id_to_pk_cloids_batch[self.ws_msg_idx] = pk_coids_this_batch

        self.ws_msg_idx += 1
        await conn.ws.send(json.dumps(ws_payload))
        #self.logger.info("Sent neword, the pk_cloids_to_hl_hexoids is like {}".format(self.pk_cloids_to_hl_hexoids))



    # ------------------------------------------------------------------
    # HIP-4 capitalization
    #
    # The strategy declares (PbCapitalReq) how many complete sets of an outcome it needs to
    # quote a ticker. The gateway owns satisfying that: read on-chain balances, mint the
    # shortfall via a splitOutcome L1 action, and keep the ticker order-blocked until it holds
    # enough (and permanently, loudly, if minting fails).
    #
    # Per-req state machine (cap_reqs[sym]["state"]):
    #   needed     -> query balances; enough? -> done (unblock); else send split -> in_flight
    #   in_flight  -> waiting on the split's WS response (resolved in _handle_cap_response):
    #                 ok -> confirming ; err -> failed
    #   confirming -> query balances (never re-mint here, to avoid double-minting on settlement
    #                 lag); enough? -> done ; past deadline? -> needed (retry, up to MAX) / failed
    #   done       -> unblocked, skip
    #   failed     -> blocked, one-shot alert, skip
    # Only the `needed` state ever sends a split, so a split is never issued twice concurrently.
    # ------------------------------------------------------------------

    CAP_CONFIRM_TIMEOUT_S = 15
    CAP_MAX_ATTEMPTS = 3

    def _cap_user_address(self):
        return self.listen_address or self.subaccount_address or self.wallet_address

    async def _query_complete_sets(self, session, outcome):
        """complete_sets = min(YES total, NO total) for the outcome, from spotClearinghouseState.

        YES/NO balances are the spot tokens "+{10*outcome+0}" / "+{10*outcome+1}".
        """
        payload = {"type": "spotClearinghouseState", "user": self._cap_user_address()}
        text = await hyp_fetch(session, self.market_endpoint + "info", payload)
        data = json.loads(text)
        balances = data.get("balances", []) if isinstance(data, dict) else []
        yes_tok = "+{}".format(10 * outcome + 0)
        no_tok = "+{}".format(10 * outcome + 1)
        yes_total = 0.0
        no_total = 0.0
        for b in balances:
            coin = b.get("coin")
            if coin == yes_tok:
                yes_total = float(b.get("total", "0"))
            elif coin == no_tok:
                no_total = float(b.get("total", "0"))
        return min(yes_total, no_total)

    async def _send_cap_split(self, conn, sym, req, need_shares):
        """Mint `need_shares` complete sets of the outcome (splitOutcome L1 action).

        Whole shares only. Signed/posted exactly like an order batch. Records the WS msg id so
        _handle_cap_response can resolve success/failure. Returns True if the split was sent.
        Wire format ported from hip4_utils.split_outcome.
        """
        amount = int(math.floor(need_shares))
        if amount <= 0:
            return False
        next_nonce = self.get_next_nonce(int(time.time() * 1000))
        if next_nonce == 0:
            self.logger.info(f"Capital split for {sym}: no nonce space, will retry")
            return False
        self.last_nonce_used = next_nonce

        action = {
            "type": "userOutcome",
            "splitOutcome": {"outcome": int(req["outcome"]), "amount": str(float(amount))},
        }
        signature = hl_signing.sign_l1_action(
            self.eth_wallet, action, self.subaccount_address, next_nonce, self.is_mainnet)
        ws_payload = {
            "method": "post",
            "id": self.ws_msg_idx,
            "request": {"type": "action", "payload": {
                "action": action,
                "nonce": next_nonce,
                "signature": signature,
                "vaultAddress": self.subaccount_address,
            }},
        }
        self.ws_id_to_cap_sym[self.ws_msg_idx] = sym
        self.ws_msg_idx += 1
        conn.num_batches_1min += 1
        conn.num_msgs_1min += 1
        conn.num_weighted_1min += 1
        self.logger.info(
            f"Capital split: minting {amount} complete sets of outcome {req['outcome']} "
            f"for {sym}")
        await conn.ws.send(json.dumps(ws_payload))
        return True

    async def capitalize_loop(self):
        """Drive every ticker's capitalization to `done` (or `failed`). Runs every ~2s.

        Only touches `needed` and `confirming` reqs (balance queries; `needed` may also send a
        split). `in_flight` reqs are resolved by the WS response handler.
        """
        self.logger.info("Capitalization loop starting")
        async with aiohttp.ClientSession() as session:
            while not self.eod_event.is_set():
                for sym, req in list(self.cap_reqs.items()):
                    state = req.get("state")
                    if state not in ("needed", "confirming"):
                        continue
                    try:
                        current = await self._query_complete_sets(session, req["outcome"])
                    except Exception as e:
                        self.logger.warning(
                            f"Capital: balance query failed for {sym}: {e}; will retry")
                        continue

                    if current >= req["target"]:
                        if state != "done":
                            req["state"] = "done"
                            self.cap_blocked_syms.discard(sym)
                            self.logger.info(
                                f"Capital: {sym} capitalized "
                                f"(have {current} >= target {req['target']}); unblocked")
                        continue

                    if state == "confirming":
                        # Awaiting settlement of a split that came back ok. Never re-mint here.
                        if time.time() >= req.get("confirm_deadline", 0):
                            if req.get("attempts", 0) < self.CAP_MAX_ATTEMPTS:
                                req["state"] = "needed"  # retry a fresh split
                            else:
                                self._cap_fail(
                                    sym, req,
                                    f"still short after {self.CAP_MAX_ATTEMPTS} attempts "
                                    f"(have {current}, target {req['target']})")
                        continue

                    # state == "needed": mint the shortfall.
                    conn = self.pick_best_connection(time.time())
                    if conn is None:
                        continue  # no connection headroom; retry next loop
                    req["attempts"] = req.get("attempts", 0) + 1
                    sent = await self._send_cap_split(conn, sym, req, req["target"] - current)
                    if sent:
                        req["state"] = "in_flight"
                await asyncio.sleep(2.0)
        self.logger.info("Capitalization loop exiting")

    def _cap_fail(self, sym, req, reason):
        """Mark a ticker's capitalization failed: stays order-blocked, alert once, log loudly."""
        req["state"] = "failed"
        self.cap_blocked_syms.add(sym)
        msg = (f"HIP-4 capitalization FAILED for {sym} (outcome {req.get('outcome')}): {reason}. "
               f"Orders for this ticker are blocked until it is capitalized.")
        self.logger.error(msg)
        if sym not in self.cap_alerted:
            self.cap_alerted.add(sym)
            email_utils.send_alerts("WSGATEWAY HIP-4 CAPITALIZATION FAILED", msg)

    def _handle_cap_response(self, msg_id, msg_response):
        """Resolve a capitalization split's WS "post" response.

        ok -> confirming (balances re-checked by capitalize_loop before we unblock, so we never
        trade on an unconfirmed mint); anything else -> failed (blocked + one-shot alert).
        """
        sym = self.ws_id_to_cap_sym.pop(msg_id, None)
        if sym is None:
            return
        req = self.cap_reqs.get(sym)
        if req is None:
            return
        payload = msg_response.get("payload") if isinstance(msg_response, dict) else None
        status = payload.get("status") if isinstance(payload, dict) else None
        if status == "ok":
            req["state"] = "confirming"
            req["confirm_deadline"] = time.time() + self.CAP_CONFIRM_TIMEOUT_S
            self.logger.info(f"Capital split ok for {sym}; confirming via balances")
        else:
            reason = payload.get("response") if isinstance(payload, dict) else msg_response
            self._cap_fail(sym, req, str(reason)[:500])

    # ------------------------------------------------------------------
    # Periodic symbol refresh + HIP-4 outcome discovery / safety gate
    # ------------------------------------------------------------------

    async def refresh_symbols_loop(self):
        """Every symbol_refresh_mins: refresh perp/spot sym_to_idx AND, if outcome_venues is
        configured, rebuild the live outcome allowlist. Runs one pass immediately at startup so
        the gate is populated before much order flow arrives.

        load_hyperliquid_assets is synchronous with blocking retry sleeps, so it is offloaded to
        a thread to avoid stalling the order/cancel loops.
        """
        self.logger.info(f"Symbol refresh loop starting (perp/spot every "
                         f"{self.symbol_refresh_mins} min)")
        while not self.eod_event.is_set():
            try:
                maps = await asyncio.to_thread(self.load_hyperliquid_assets)
                # Atomic rebind (readers see the old or new dict, never a torn one).
                self.sym_to_idx = maps["sym_to_idx"]
                self.idx_to_sym = maps["idx_to_sym"]
            except Exception as e:
                self.logger.warning(f"Symbol refresh: load_hyperliquid_assets failed: {e}")

            # Sleep in 1s steps so we exit promptly on eod.
            for _ in range(max(1, int(self.symbol_refresh_mins * 60))):
                if self.eod_event.is_set():
                    break
                await asyncio.sleep(1)
        self.logger.info("Symbol refresh loop exiting")

    async def outcome_discovery_loop(self):
        """Rebuild the live outcome allowlist from outcomeMeta every outcome_refresh_mins.

        Runs on its own (faster) cadence than the perp/spot refresh because it also drives
        resolution: a settled outcome drops out of outcomeMeta -> off live_outcome_coins -> the
        gate rejects it -> the strat winds down on that reject. First pass runs immediately so the
        gate is populated before much order flow arrives. No-op unless outcome_venues is set.
        """
        if not self.outcome_venues:
            return
        self.logger.info(f"Outcome discovery loop starting (every {self.outcome_refresh_mins} "
                         f"min; venues={self.outcome_venues})")
        async with aiohttp.ClientSession() as session:
            while not self.eod_event.is_set():
                try:
                    await self._refresh_outcomes(session)
                except Exception as e:
                    # Keep the last good set on a transient outage rather than opening the gate
                    # (fail-safe), but make the staleness visible.
                    self.logger.warning(
                        f"Outcome discovery failed (keeping last "
                        f"{len(self.live_outcome_coins)} coins): {e}")
                for _ in range(max(1, int(self.outcome_refresh_mins * 60))):
                    if self.eod_event.is_set():
                        break
                    await asyncio.sleep(1)
        self.logger.info("Outcome discovery loop exiting")

    async def _refresh_outcomes(self, session):
        """Rebuild self.live_outcome_coins from outcomeMeta, filtered to self.outcome_venues.

        outcomeMeta has no server-side venue filter, so fetch all and filter on the per-outcome
        `venue` field. Coins are "#{10*outcome+side}" for both sides.
        """
        text = await hyp_fetch(session, self.market_endpoint + "info", {"type": "outcomeMeta"})
        data = json.loads(text)
        outs = data.get("outcomes", []) if isinstance(data, dict) else []
        venues = set(self.outcome_venues)
        coins = set()
        for o in outs:
            if o.get("venue") in venues:
                oid = o.get("outcome")
                if oid is None:
                    continue
                coins.add("#{}".format(10 * int(oid) + 0))
                coins.add("#{}".format(10 * int(oid) + 1))
        # A non-empty venue list that matches nothing is almost always a venue typo -- and it
        # would silently block ALL outcome trading (empty allowlist, fail-closed). Surface it.
        if not coins:
            msg = (f"outcome_venues={self.outcome_venues} configured but 0 live outcomes matched "
                   f"in outcomeMeta -- likely a venue typo. ALL outcome orders will be blocked.")
            self.logger.error(msg)
            if "outcome_venues_empty" not in self.outcome_gate_alerted:
                self.outcome_gate_alerted.add("outcome_venues_empty")
                email_utils.send_alerts("WSGATEWAY HIP-4 OUTCOME_VENUES MATCHED NOTHING", msg)
        else:
            self.outcome_gate_alerted.discard("outcome_venues_empty")
        self.live_outcome_coins = coins
        self.outcome_gate_ready = True
        self.logger.info(
            f"Outcome discovery: {len(coins)} live outcome coins on venues {sorted(venues)}")

    def _outcome_gate_blocks(self, sym):
        """True iff the outcome safety gate should refuse this symbol.

        Only applies when outcome_venues is configured and sym is an outcome ("#") coin.
        Fail-closed: before the first successful discovery, all "#" coins are blocked (we won't
        trade an unverified outcome).
        """
        if not self.outcome_venues or not sym.startswith("#"):
            return False
        if not self.outcome_gate_ready:
            return True
        return sym not in self.live_outcome_coins

    async def send_hyperliquid_cancel(self, conn, now_ms):
        #endpt = self.market_endpoint + "exchange"

        all_cancel_dicts = []
        pk_coids_this_batch = []

        next_nonce = self.get_next_nonce(now_ms)
        if next_nonce == 0:
            # No space for nonces. 
            self.logger.info("Skipping cancels because no nonce space")
            return
        # Otherwise, store it. 
        self.last_nonce_used = next_nonce


        # There are some cancelpbs that we might want to keep for the next time,
        # in case they didn't get a chance yet.
        next_round_cancel_pbs = []

        #self.logger.info("Starting cancel loop, length is {}".format(len(self.queued_cancel_pbs)))
        for one_cancel_pb in self.queued_cancel_pbs:

            pkcoid = "{}-{}".format(
                one_cancel_pb.strategy_id, one_cancel_pb.executor_order_id
            )

            if pkcoid in self.pkcoid_cxl_count:
                self.pkcoid_cxl_count[pkcoid] += 1
            else:
                self.pkcoid_cxl_count[pkcoid] = 1

            if pkcoid not in self.force_cancel_pkcoids and self.pkcoid_cxl_count[pkcoid] > 10:
                # Just tell the strategy that it's been cancelled or something.
                self.logger.info("Cancelback {} because of too many cancelAttempts".format(pkcoid))
                pbresponse = gateway_pb2.PbMessage()
                new_cxl = pbresponse.cancel_ack

                new_cxl.strategy_id = one_cancel_pb.strategy_id
                new_cxl.executor_order_id = one_cancel_pb.executor_order_id

                pub_string = pbresponse.SerializeToString()
                self.acks_pub_socket.send(pub_string)

                # Don't cancel this in the future because it won't work anyway.
                self._mark_ignore_cancel(pkcoid)
                continue

            if pkcoid not in self.force_cancel_pkcoids and pkcoid in self.pkcoids_to_dead_t:
                # Then we don't need to cancel it. But still send a cancelack back.
                self.logger.info("Ignoring cancel for {} because it never posted".format(pkcoid))
                pbresponse = gateway_pb2.PbMessage()
                new_cxl = pbresponse.cancel_ack

                new_cxl.strategy_id = one_cancel_pb.strategy_id
                new_cxl.executor_order_id = one_cancel_pb.executor_order_id

                pub_string = pbresponse.SerializeToString()
                self.acks_pub_socket.send(pub_string)
                continue


            if pkcoid not in self.pk_cloids_to_hl_hexoids:
                # Suppress the log on the very first miss: the cancel may simply
                # have arrived at the cancel-send loop a hair before the order-send
                # loop got around to actually sending the placement (which is when
                # pk_cloids_to_hl_hexoids gets populated). On the next sweep the
                # entry is usually there. Persistent misses (count > 1) still print
                # so the original signal — restart-loss / 429-clear — is preserved.
                if self.pkcoid_cxl_count[pkcoid] > 1:
                    self.logger.info(f"Couldnt find pk_coid {pkcoid} in self.pk_cloids_to_hl_hexoids")

                # This can happen in 3 cases: cancel arrived ahead of placement
                # (resolves on the next sweep), order from query was lost bc
                # gateway was in the middle of a restart (e.g., when HL does its
                # periodic disconnects), or we cleared the order without sending
                # it during a 429 pause. None of these cases benefit from retries,
                # but they're also mostly harmless so just reattempt 5 times before
                # giving up.
                if self.pkcoid_cxl_count[pkcoid] > 5:
                    pbresponse = gateway_pb2.PbMessage()
                    cancel_rej = pbresponse.cancel_reject
                    cancel_rej.strategy_id = one_cancel_pb.strategy_id
                    cancel_rej.executor_order_id = one_cancel_pb.executor_order_id
                    cancel_rej.reason = (
                        "Cancel failed, couldnt find in gateway records(maybe http response hasnt arrived yet)".format(
                            pkcoid
                        )
                    )
                    # Send this back.
                    pub_string = pbresponse.SerializeToString()
                    self.acks_pub_socket.send(pub_string)
                    # Log it for ourselves.
                    log_str = "Couldnt find pk_coid {} in self.pk_cloids_to_hl_hexoids, sending cancelrej".format(
                        pkcoid)
                    #log_str += "\nKeys were {} \n".format(self.pk_cloids_to_hl_hexoids.keys())
                    self.logger.info(log_str)
                else:
                    next_round_cancel_pbs.append(one_cancel_pb)
                    #self.logger.info("Queued cancel for {}".format(pkcoid))

                # Still, if the strategy is trying to cancel it, we should
                # try stop it from spamming us.
                continue

            pk_coids_this_batch.append(pkcoid)
            # One-shot: the force flag exists only to get this cancel past the
            # guards above and onto the wire. Drop it now so a still-resting
            # order gets re-flagged by the next backstop sweep rather than
            # force-cancelling forever.
            self.force_cancel_pkcoids.discard(pkcoid)

            sym = self.pkcoids_to_orderdeets[pkcoid]["symbol"]
            asset_num = self._asset_num(sym)

            cancel_dct = {
                "asset": asset_num,
                "cloid": self.pk_cloids_to_hl_hexoids[pkcoid],
            }
            all_cancel_dicts.append(cancel_dct)

        self.queued_cancel_pbs.clear()
        self.queued_cancel_pbs += next_round_cancel_pbs

        if len(all_cancel_dicts) == 0:
            return  # Nothing to cancel, don't send empty message

        cancel_action = {
            "type": "cancelByCloid",
            "cancels": all_cancel_dicts,
        }
        # HL requires f to be omitted when false; actions hashed with f:false
        # are rejected. Only include the field on the enabled fast path.
        if self.fast_cancels:
            cancel_action["f"] = True

        cancel_signature = hl_signing.sign_l1_action(
            self.eth_wallet,
            cancel_action,
            self.subaccount_address,  # Use subaccount if configured, None for main account
            next_nonce,
            self.is_mainnet,
        )

        cancel_payload = {
            "action": cancel_action,
            "nonce": next_nonce,
            "signature": cancel_signature,
            "vaultAddress": self.subaccount_address,  # Use subaccount if configured, None for main account
        }

        ws_payload = {
            "method": "post",
            "id": self.ws_msg_idx,
            "request": {
                "type": "action",
                "payload": cancel_payload,
            }
        }

        # Update per-connection 1min counts
        conn.num_batches_1min += 1
        conn.num_msgs_1min += len(all_cancel_dicts)
        conn.num_weighted_1min += 1 + len(all_cancel_dicts) // 40

        self.ws_id_to_pk_cloids_batch[self.ws_msg_idx] = pk_coids_this_batch
        # Stamp send time so the cancel response can compute this batch's RTT.
        self.ws_id_to_cancel_send_t[self.ws_msg_idx] = time.time()

        self.ws_msg_idx += 1
        await conn.ws.send(json.dumps(ws_payload))


    def get_all_unit_asset_nums(self):
        try:
            endpoint_base = self.market_endpoint

            perp_endpt = endpoint_base + "info"
            perp_payload = {
                "type": "perpDexs",
            }
            perp_response = json.loads(requests.post(perp_endpt, json=perp_payload).text)

            unit_perp_idx = None
            for idx, dex in enumerate(perp_response):
                if dex is None:
                    continue
                if dex["name"] == self.unit_market:
                    unit_perp_idx = idx
                    break

            if unit_perp_idx is None:
                self.logger.warning("get_all_unit_asset_nums: unit_perp_idx is None")
                return {}

            sym_endpt = endpoint_base + "info"
            sym_payload = {
                "type": "meta",
                "dex": self.unit_market
            }

            sym_response_json = json.loads(requests.post(sym_endpt, json=sym_payload).text)

            names_to_idxs = {}
            for idx, one_sym_dict in enumerate(sym_response_json["universe"]):
                full_asset_num = 100000 + unit_perp_idx * 10000 + idx
                sym_name = one_sym_dict["name"]
                names_to_idxs[sym_name] = full_asset_num

            if len(names_to_idxs) == 0:
                self.logger.warning("get_all_unit_asset_nums: returned empty dict")
            else:
                self.logger.info("get_all_unit_asset_nums: loaded {} unit assets".format(len(names_to_idxs)))

        except Exception as e:
            self.logger.error("get_all_unit_asset_nums: Exception: {}".format(e))
            return {}
        return names_to_idxs

    # Given a symbol, gives back the index of that asset.
    # Fucking annoying.
    def load_hyperliquid_assets(self):
        endpt = self.market_endpoint + "info"
        session = requests.Session()
        # Not sure if this is the only thing.
        session.headers.update({"Content-Type": "application/json"})

        params = {
            # Rough, but it's ok
            "type": "meta",
        }

        universe_data = session.post(
            endpt,
            json=params,
            )

        if universe_data.status_code != 200:
            # Sleep and try again.
            for i in range(10):
                self.logger.info("Got error from hyperliquid: err_code {}  {} | sleeping and retrying".format(universe_data.status_code, universe_data.text))
                time.sleep(10)

                universe_data = session.post(
                    endpt,
                    json=params,
                )
                if universe_data.status_code == 200:
                    break

        universe_data_dct = json.loads(universe_data.text)

        universe_lst = universe_data_dct["universe"]
        sym_to_idx = {}
        idx_to_sym = {}
        for idx, one_sym_dct in enumerate(universe_lst):
            sym_to_idx[one_sym_dct["name"]] = idx
            idx_to_sym[idx] = one_sym_dct["name"]


        # Add in stuff for spot.
        # sleep a little for spot stuff.
        # time.sleep(30)
        # 2 or 3 info calls in a row isn't going to 429 us.
        # Not as hard as it could be for spot.
        spot_params = {
            # Rough, but it's ok
            "type": "spotMeta",
        }

        spot_universe_data = session.post(
            endpt,
            json=spot_params,
        )

        if spot_universe_data.status_code != 200:
            self.logger.info("Got error from hyperliquid: err_code {}  {} | sleeping and retrying".format(spot_universe_data.status_code, spot_universe_data.text))
            for i in range(10):
                time.sleep(10)

                spot_universe_data = session.post(
                    endpt,
                    json=spot_params,
                )

        spot_universe_data_dct = json.loads(spot_universe_data.text)

        # Test is @50
        # I'm using BTC symbol now.
        #print("Universe data: {}".format(json.dumps(spot_universe_data_dct, indent=4)))

        # Name and index is all I want here.
        spot_pairs_lst = spot_universe_data_dct["universe"]
        for one_spot_pair in spot_pairs_lst:
            spot_name = one_spot_pair["name"]
            spot_idx = one_spot_pair["index"] + 10000

            # OK, now add them to the mappings.
            sym_to_idx[spot_name] = spot_idx
            idx_to_sym[spot_idx] = spot_name


        # Also add in stuff for unit dex.
        # Retry logic in case the request fails and returns empty
        max_retries = 5
        retry_wait_secs = 3
        unit_names_to_idxs = {}

        for attempt in range(max_retries):
            unit_names_to_idxs = self.get_all_unit_asset_nums()
            if len(unit_names_to_idxs) > 0:
                break
            if attempt < max_retries - 1:
                self.logger.warning("load_hyperliquid_assets: get_all_unit_asset_nums returned empty on attempt {}/{}, retrying after {} secs".format(
                    attempt + 1, max_retries, retry_wait_secs))
                time.sleep(retry_wait_secs)
            else:
                self.logger.error("load_hyperliquid_assets: get_all_unit_asset_nums returned empty after {} attempts".format(max_retries))

        for name, idx in unit_names_to_idxs.items():
            sym_to_idx[name] = idx
            idx_to_sym[idx] = name


        return {"sym_to_idx": sym_to_idx, "idx_to_sym": idx_to_sym}


    async def poll_zmq_buffer(self):
        self.logger.info("ZMQ poll loop starting")
        # Create the pub socket first.
        # Wait 10s, wait to sub.

        # This is where we get incoming
        # self.oe_sub_socket.connect(self.gateway_config["oe_sub_socket"])

        # The pygateway is the one who is creating this socket - it needs to bind it.
        self._remove_stale_ipc(self.gateway_config["oe_sub_socket"])
        self.oe_sub_socket.bind(self.gateway_config["oe_sub_socket"])
        self.oe_sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

        # TODO: wrap this in some try/except blocks too.
        while not self.eod_event.is_set():
            # Listens to sub socket and sends out orders.
            all_action_strs = []

            # Get the first one.
            try:
                ord_action_str = await self.oe_sub_socket.recv(flags=zmq.NOBLOCK)
                all_action_strs.append(ord_action_str)
            except zmq.Again:
                pass
            # print("Got first order action")

            # Drain the zmq of all outstanding messages.
            while True:
                try:
                    msg = self.oe_sub_socket.recv(
                        flags=zmq.NOBLOCK
                    ).result()  # Receive message
                    all_action_strs.append(msg)
                    # Process the received message here
                except zmq.Again:
                    # Socket was empty.
                    # print("Socket now empty, break")
                    break

            for one_action in all_action_strs:

                # Parse ordedr actions.
                ord_action_pb = gateway_pb2.PbMessage()
                ord_action_pb.ParseFromString(one_action)
                # Parse actions.
                #self.logger.info("zmq action {}".format(one_action))
                if ord_action_pb.HasField("new_order"):
                    # Check type. 

                    # ALO -> fast queue (paced one batch per gap_between_send_s).
                    # IOC -> priority queue (drained every 5ms tick, subject to
                    # per-min cap). GTC and any other TIF are rejected — strats
                    # must use IOC or ALO. First offender triggers a one-shot email.
                    tif = ord_action_pb.new_order.time_in_force
                    if tif == 4:
                        self.queued_fast_ord_pbs.append(ord_action_pb.new_order)
                    elif tif == 2:
                        self.queued_priority_ord_pbs.append(ord_action_pb.new_order)
                    else:
                        # Reject the order back to the strategy.
                        new_order = ord_action_pb.new_order
                        pbresponse = gateway_pb2.PbMessage()
                        new_rej = pbresponse.new_reject
                        new_rej.strategy_id = new_order.strategy_id
                        new_rej.executor_order_id = new_order.executor_order_id
                        new_rej.reason = (
                            f"GTC orders not allowed at gateway (TIF={tif}); "
                            f"strategy must use IOC or ALO.")
                        self.acks_pub_socket.send(pbresponse.SerializeToString())
                        # One-shot email so a misconfigured strat surfaces fast.
                        if not self._gtc_alert_sent:
                            self._gtc_alert_sent = True
                            body = (
                                f"Gateway received a non-IOC/non-ALO order (TIF={tif}) "
                                f"from strategy_id='{new_order.strategy_id}' "
                                f"symbol='{new_order.symbol}' "
                                f"executor_order_id={new_order.executor_order_id}. "
                                f"The order was rejected. This alert fires once per gateway "
                                f"lifetime; check rejects in logs for subsequent occurrences.")
                            self.logger.error(body)
                            email_utils.send_alerts(
                                "WSGATEWAY GOT GTC ORDER — STRAT MISCONFIGURED", body)
                elif ord_action_pb.HasField("cancel_order"):
                    # Don't queue the cancel if it already was cancelled.
                    # I think this was happening quite a bit.
                    cur_pk_coid =  "{}-{}".format(
                        ord_action_pb.cancel_order.strategy_id, ord_action_pb.cancel_order.executor_order_id
                    )
                    if cur_pk_coid in self.pkcoids_to_dead_t:
                        self.logger.info("Not cancelling {} bc in ignore_list, sending ack anyway".format(cur_pk_coid))
                        # Send a cancelack back so the strategy can move on. 
                        pbresponse = gateway_pb2.PbMessage()
                        new_cxl = pbresponse.cancel_ack

                        new_cxl.strategy_id = ord_action_pb.cancel_order.strategy_id
                        new_cxl.executor_order_id = ord_action_pb.cancel_order.executor_order_id

                        pub_string = pbresponse.SerializeToString()
                        self.acks_pub_socket.send(pub_string)

                        continue

                    self.queued_cancel_pbs.append(ord_action_pb.cancel_order)
                elif ord_action_pb.HasField("capital_req"):
                    # HIP-4 capitalization requirement. Record it and block the ticker until the
                    # capitalize_loop confirms we hold enough complete sets (fail-safe: never
                    # place before we're capitalized). The loop unblocks immediately if we
                    # already hold enough.
                    cr = ord_action_pb.capital_req
                    sym = cr.symbol
                    # Outcome safety gate: never mint for an outcome that isn't live on the
                    # configured deployer venue (settled / unknown / wrong-venue). Block the
                    # ticker and alert once, rather than splitting collateral into dead tokens.
                    if self._outcome_gate_blocks(sym):
                        self.cap_blocked_syms.add(sym)
                        why = (f"{OUTCOME_NOT_LIVE_MARKER} on deployer venue(s) "
                               f"{self.outcome_venues}"
                               if self.outcome_gate_ready else "outcome gate not ready")
                        self.logger.error(
                            f"Capital req REFUSED for {sym} (outcome {cr.outcome}): {why}. "
                            f"Ticker blocked.")
                        if sym not in self.outcome_gate_alerted:
                            self.outcome_gate_alerted.add(sym)
                            email_utils.send_alerts(
                                "WSGATEWAY HIP-4 CAPITAL REQ OFF-VENUE",
                                f"Refused capital req for {sym} (outcome {cr.outcome}): {why}")
                    else:
                        self.cap_reqs[sym] = {
                            "outcome": int(cr.outcome),
                            "target": float(cr.target_complete_sets),
                            "strategy_id": cr.strategy_id,
                            "symbol": sym,
                            "state": "needed",
                        }
                        self.cap_blocked_syms.add(sym)
                        self.cap_alerted.discard(sym)
                        self.outcome_gate_alerted.discard(sym)
                        self.logger.info(
                            f"Capital req: {sym} outcome={cr.outcome} "
                            f"target_complete_sets={cr.target_complete_sets}")

            # Making this happen more often.
            await asyncio.sleep(self.zmq_sleep_t)
        self.logger.info("ZMQ poll loop exiting")


################################################################################
# Hyperliquid helper functions.
# These are basically copied from live_cockpit though.


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--how", default=False, action="store_true")
    parser.add_argument("--market", required=True)
    parser.add_argument("--creds", default="/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser()))
    # For ID-ing the gateway for nonces.
    # We limit ourselves to 20 gateawys at once, I think that's an ok number.
    parser.add_argument("--id", required=True, type=int)
    parser.add_argument("--force-id", action="store_true")
    # Sleep this many secs before starting, to give time for the previous proceses to finish.
    parser.add_argument("--wait_secs", type=int, default=5)
    parser.add_argument("--date", required=False, default="TOMORROW")
    parser.add_argument("--end-label", required=False, default="PKDayEnd")
    parser.add_argument("--local-ips", required=False, default=None,
                        help="Comma-separated local IPs for multi-WS connections, "
                             "e.g. 10.0.1.10,10.0.1.11,10.0.1.12")
    parser.add_argument("--outcome-venues", required=False, default=None,
                        help="Comma-separated HIP-4 deployer venues to trade outcomes on "
                             "(e.g. 'txyz'). Overrides the market config default. When set, the "
                             "gateway discovers the live outcome set on these venues every "
                             "symbol_refresh_mins and refuses orders/capital-reqs for outcome "
                             "coins not live on them.")
    parser.add_argument("--disable-fast-cancels", action="store_true",
                        help="Omit Hyperliquid's top-level f:true fast-cancel flag")
    args = parser.parse_args()

    if args.market not in ["Hyperliquid", "HyperliquidTest"]:
        sys.exit("Running with unsupported market.")

    if args.how:
        print("How: ")
        print("./wsgateway.py --market HyperliquidTest --creds /home/ubuntu/.creds/.HyperliquidTest.creds.json")
        sys.exit()

    if "2" in args.date:
        # We can give it a date label or a date string like 20250501
        date_str = args.date
    else:
        date_str = chron.get_date_easy(args.date)

    end_str = chron.get_end_time(args.end_label, date_str)
    end_secs = chron.timestr_to_secs(end_str)

    if not os.path.exists(args.creds):
        sys.exit("Creds file does not exist: {}".format(args.creds))

    if args.id >= 20 or args.id < 0:
        sys.exit(f"Invalid id {args.id}, must be within [0,19]")
    if not args.force_id:
        hostname = socket.gethostname()
        m = re.search(r'\d+$', hostname)
        if not m or int(m.group()) != args.id:
            sys.exit(f"Invalid id {args.id}, must match hostname {hostname}")

    # Sleep for a bit.
    print("Sleeping for {} secs before starting".format(args.wait_secs))
    await asyncio.sleep(args.wait_secs)

    local_ips = args.local_ips.split(",") if args.local_ips else None
    outcome_venues = (args.outcome_venues.split(",") if args.outcome_venues else None)
    hl_gw = HyperliquidGateway(
        args.market,
        args.creds,
        args.id,
        local_ips=local_ips,
        fast_cancels=not args.disable_fast_cancels,
        outcome_venues=outcome_venues,
    )
    await asyncio.sleep(2)

    # Set up signal handler for graceful shutdown
    def signal_handler(sig, frame):
        hl_gw.logger.info("Received interrupt signal, shutting down gracefully...")
        hl_gw.eod_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("About to start.")
    tasks = {
        asyncio.create_task(hl_gw.poll_zmq_buffer()),
        asyncio.create_task(hl_gw.run_ws())
    }
    # Daemon thread so it survives a wedged event loop; see _shutdown_watchdog.
    threading.Thread(target=hl_gw._shutdown_watchdog, daemon=True,
                     name="shutdown-watchdog").start()
    time_to_eod = end_secs - time.time()
    try:
        done, pending = await asyncio.wait(tasks, timeout=time_to_eod)
        if pending: # timed out (EOD)
            hl_gw.logger.info("EOD time reached, initiating shutdown")
            hl_gw.eod_event.set()
            # Let each task handle the eod_event and clean up
            await asyncio.wait(pending)
    except asyncio.CancelledError:
        hl_gw.logger.error("Main loop cancelled")
    finally:
        hl_gw.release_zmq()
        # Teardown finished cleanly — defuse the shutdown watchdog.
        hl_gw._teardown_done = True

if __name__ == "__main__":
    uvloop.run(main())
