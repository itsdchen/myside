#! /usr/bin/env python

"""
hip3takers.py - Hyperliquid TWAP Tracker

Monitors Hyperliquid perps for active TWAP orders, tracks the largest ones,
and publishes aggregated stats to a GitHub Pages site for visualization.

USAGE
=====

Main mode (run continuously):
    python hip3takers.py

    - Subscribes to trade feeds for all xyz symbols
    - Detects TWAP trades (identified by zero hash)
    - Tracks up to 10 largest TWAPs by remaining notional
    - Publishes stats to ~/tradefi/itsdchen.github.io/state.json every 10 mins
    - Logs status dump every 60 seconds

Bootstrap known addresses at startup:
    python hip3takers.py --watch 0xabc123... 0xdef456...
    python hip3takers.py --watch-file addresses.txt

    - Queries API for active TWAPs on these addresses before starting
    - Useful for catching TWAPs that started before the script

READ-ONLY DEBUG COMMANDS (won't interfere with running instance):

    python hip3takers.py --check 0xabc123...
        Query API for active TWAPs on an address right now

    python hip3takers.py --audit 0xabc123...
        Show audit log for an address (what decisions were made)
        Supports partial address match

    python hip3takers.py --audit-search SILVER
        Search audit log for addresses/entries matching a string

HOW IT WORKS
============

1. Detection: Subscribes to trade feeds. TWAP trades have hash=0x000...
2. Tracking: When a TWAP trade is seen, subscribes to twapStates for that user
3. Priority: Tracks top 10 users by remaining notional. Smaller TWAPs get evicted.
4. Validation: Before publishing, verifies all TWAPs still active via API
5. Publishing: Writes aggregated stats to state.json, pushes to GitHub Pages

DEBUGGING
=========

The script maintains an audit log (saved to audit_log.json every 30s) that
records all decisions made about each address:
    - TWAP_TRADE_SEEN: A TWAP trade was detected
    - SKIPPED_CAPACITY: Skipped because tracking 10/10 and this was smaller
    - STARTED_TRACKING: Added to tracking
    - STATE_UPDATE: Received twapStates update
    - REMOVED_PRE_PUBLISH: Removed by pre-publish sanity check
    - STOP_TRACKING: Stopped tracking (with reason)

Use --audit <address> to see the log for a specific address.

OUTPUT FILES
============

~/tradefi/itsdchen.github.io/state.json - Published TWAP stats (auto-pushed to GitHub)
~/tradefi/itsdchen.github.io/audit_log.json - Audit log for debugging
"""


import subprocess, pathlib


import argparse
import asyncio
import getpass
import json
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

import eth_account
import requests
import websockets
import uvloop
import os 
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(script_dir, "../util"))

import chron


mkt_to_endpoint = {
    "Hyperliquid": "https://api.hyperliquid.xyz/",
}


def dexname():
    return "xyz"


# Trade pattern analyzer for TWAP detection
class SymTwapWatcher:
    def __init__(self):
        """
        Args:
            time_window: Time window in seconds to analyze trades (default 5 min)
            size_variance_threshold: Max coefficient of variation for TWAP (default 15%)
        """
        # Store recent trades per address for analysis
        self.endpoint_base = mkt_to_endpoint["Hyperliquid"]
        self.ws_url = "wss://api.hyperliquid.xyz/ws"


        # Way to track both trade and wallet behavior 
        # key: address
        # value: dict with twap stat dicts. 
        self.per_user_stats = {}

        # Address to twap stats (set for O(1) lookup)
        self.ongoing_twap_addresses = set() 

        # Key: symbol
        # Value: 
        #      : net_remaining_sz
        #      : num_active_twaps
        # 
        self.per_sym_twap_stats = {}


        self.t_last_publish = time.time()  # Avoid immediate publish on startup
        self.publish_task = None

        # Queue for throttled twapStates subscriptions
        self.pending_twap_users = deque()
        self.pending_twap_users_set = set()
        self.max_twap_subs_per_flush = 3
        self.twap_flush_interval = 0.2  # seconds
        self.t_last_twap_flush = 0.0

        # Queue for unsubscriptions (must unsubscribe before subscribing to new users)
        self.pending_unsub_users = deque()
        self.pending_unsub_users_set = set()
        self.subscribed_users = set()  # Track who we're actually subscribed to

        # Track at most 10 users (Hyperliquid limit)
        self.max_tracked_users = 10
        self.user_remaining_ntl = {}

        # 10 mins.
        self.publish_s = 60 * 10

        # Debug: periodic status dump
        self.t_last_status_dump = 0
        self.status_dump_interval = 60  # Every 60 seconds

        # Audit log: track all decisions about addresses for debugging
        # Key: address, Value: list of (timestamp, action, details)
        self.audit_log = defaultdict(list)
        self.max_audit_entries_per_address = 50
        self.audit_log_day = datetime.now(ZoneInfo("America/New_York")).date()
        self.audit_log_file = os.path.expanduser("~/tradefi/itsdchen.github.io/audit_log.json")
        self.t_last_audit_save = 0
        self.audit_save_interval = 30  # Save every 30 seconds

        # Weekend premium tracking state
        self.friday_close_prices = {}  # symbol -> price at Friday 18:00 ET
        self.friday_close_time = None  # ISO timestamp when Friday close was captured
        self.current_mids = {}         # Latest fetched mid prices
        self.t_last_mids_fetch = 0     # Time of last allMids fetch

        # Figure out what syms to subscribe to. Start with live discovery, fall back to static list.
        self.syms = []
        if not self.populate_syms_from_api():
            self.load_syms_from_file()

        # Load any existing Friday close data on startup
        self.load_friday_close_from_state()

    def populate_syms_from_api(self):
        if self.fetch_all_mids():
            if self.current_mids:
                self.syms = sorted(self.current_mids.keys())
                print(f"Loaded {len(self.syms)} xyz syms from Hyperliquid allMids")
                return True
        return False

    def load_syms_from_file(self):
        syms_path = os.path.join(script_dir, "known_syms.txt")
        try:
            with open(syms_path, "r") as f:
                self.syms = [line.strip() for line in f if line.strip()]
            if self.syms:
                print(f"Loaded {len(self.syms)} xyz syms from known_syms.txt fallback")
            else:
                self.syms = ["xyz:XYZ100"]
        except FileNotFoundError:
            print("known_syms.txt is missing, defaulting to xyz:XYZ100")
            self.syms = ["xyz:XYZ100"]

    def _post_info(self, payload, timeout=10):
        """POST to the info endpoint with 429 retry (60s backoff)."""
        response = requests.post(
            self.endpoint_base + "info",
            json=payload,
            timeout=timeout,
        )
        if response.status_code == 429:
            print(f"Rate limited (429) on {payload.get('type', '?')}, backing off 60s...")
            time.sleep(60)
            response = requests.post(
                self.endpoint_base + "info",
                json=payload,
                timeout=timeout,
            )
        return response

    def fetch_all_mids(self):
        """Fetch allMids from Hyperliquid API for dex xyz."""
        try:
            response = self._post_info({"type": "allMids", "dex": "xyz"})
            if response.status_code == 200:
                mids_data = response.json()
                # mids_data is a dict like {"xyz:XYZ100": "21500.5", "xyz:NVDA": "140.25", ...}
                self.current_mids = {}
                for sym, price_str in mids_data.items():
                    try:
                        self.current_mids[sym] = float(price_str)
                    except (ValueError, TypeError):
                        pass
                self.t_last_mids_fetch = time.time()
                return True
        except Exception as e:
            print(f"Error fetching allMids: {e}")
        return False

    def fetch_l2_book(self, sym):
        """Fetch L2 orderbook for a symbol."""
        try:
            response = self._post_info({"type": "l2Book", "coin": sym, "dex": "xyz"})
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            print(f"Error fetching L2 book for {sym}: {e}")
            return None

    def fetch_twap_states_for_user(self, user):
        """Fetch current TWAP states for a user via REST API.

        Returns list of active TWAP states, or None on error.

        Note: Uses twapHistory endpoint since twapStates is websocket-only.
        Filters for activated (non-terminated) TWAPs with remaining size.
        """
        try:
            response = self._post_info({"type": "twapHistory", "user": user})
            if response.status_code == 200:
                data = response.json()
                # Response format: list of {time, state, status, twapId} dicts
                # Filter to only active TWAPs (activated status and remaining size)
                active_states = []
                seen_twap_ids = set()
                for entry in data:
                    # twapHistory returns all state changes, most recent first
                    # Only take the latest state for each twapId
                    twap_id = entry.get("twapId")
                    if twap_id in seen_twap_ids:
                        continue
                    seen_twap_ids.add(twap_id)

                    status = entry.get("status", {}).get("status", "")
                    if status != "activated":
                        continue

                    state = entry.get("state", {})
                    coin = state.get("coin", "")
                    # Only include xyz perps (filter out mainnet like PAXG)
                    if not coin.startswith("xyz:"):
                        continue
                    sz = float(state.get("sz", 0))
                    executed_sz = float(state.get("executedSz", 0))
                    if sz > executed_sz:
                        active_states.append(state)
                return active_states
            print(f"twapHistory API returned status {response.status_code} for {user}")
            return None
        except Exception as e:
            print(f"Error fetching twapHistory for {user}: {e}")
            return None

    def calculate_liquidity_within_bps(self, l2_data, side, bps=50):
        """Calculate notional liquidity within X bps of best price on given side.

        Args:
            l2_data: L2 orderbook data from fetch_l2_book()
            side: "buy" (check asks) or "sell" (check bids)
            bps: Basis points from best price (default 50)

        Returns:
            Total notional liquidity within bps threshold, or None if no data
        """
        if not l2_data or "levels" not in l2_data:
            return None

        # For TWAP buys, we care about ask-side liquidity (what they're buying into)
        # For TWAP sells, we care about bid-side liquidity
        # levels[0] = bids, levels[1] = asks
        levels = l2_data["levels"][1] if side == "buy" else l2_data["levels"][0]

        if not levels:
            return None

        best_price = float(levels[0]["px"])
        if side == "buy":
            # For buys (checking asks), include levels up to threshold above best ask
            threshold = best_price * (1 + bps / 10000)
        else:
            # For sells (checking bids), include levels down to threshold below best bid
            threshold = best_price * (1 - bps / 10000)

        total_notional = 0.0
        for level in levels:
            px = float(level["px"])
            sz = float(level["sz"])
            # For buys (asks), include levels <= threshold
            # For sells (bids), include levels >= threshold
            if side == "buy" and px <= threshold:
                total_notional += px * sz
            elif side == "sell" and px >= threshold:
                total_notional += px * sz

        return total_notional

    def is_weekend_hours(self):
        """Check if currently in weekend period (Fri 18:00 ET - Sun 18:00 ET)."""
        et_now = datetime.now(ZoneInfo("America/New_York"))
        weekday = et_now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
        hour = et_now.hour

        # Friday after 18:00 ET
        if weekday == 4 and hour >= 18:
            return True
        # All of Saturday
        if weekday == 5:
            return True
        # Sunday before 18:00 ET
        if weekday == 6 and hour < 18:
            return True
        return False

    def should_capture_friday_close(self):
        """Check if we should capture Friday close prices (17:00-18:00 ET window)."""
        et_now = datetime.now(ZoneInfo("America/New_York"))
        weekday = et_now.weekday()
        hour = et_now.hour

        # Must be Friday between 17:00 and 18:00 ET (CME close window)
        # Captures continuously during this window; last capture before 18:00 is kept
        if weekday != 4 or hour < 17 or hour >= 18:
            return False

        return True

    def capture_friday_close(self):
        """Snapshot current mids as Friday close prices."""
        if not self.current_mids:
            return
        self.friday_close_prices = dict(self.current_mids)
        et_now = datetime.now(ZoneInfo("America/New_York"))
        self.friday_close_time = et_now.isoformat()
        print(f"Captured Friday close prices at {self.friday_close_time}")

    def load_friday_close_from_state(self):
        """Load Friday close data from existing state.json on startup."""
        tgt_file = os.path.expanduser("~/tradefi/itsdchen.github.io/state.json")
        try:
            with open(tgt_file, "r") as f:
                data = json.load(f)
            if "_fridayCloseData" in data:
                friday_data = data["_fridayCloseData"]
                self.friday_close_prices = friday_data.get("prices", {})
                self.friday_close_time = friday_data.get("time")
                if self.friday_close_prices:
                    print(f"Loaded Friday close data from {self.friday_close_time}")
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            print(f"No existing Friday close data to load: {e}")

    def get_weekend_premiums(self):
        """Calculate weekend premium percentages vs Friday close."""
        if not self.friday_close_prices or not self.current_mids:
            return None

        if not self.is_weekend_hours():
            return None

        symbols_data = {}
        for sym in self.friday_close_prices:
            if sym not in self.current_mids:
                continue
            friday_close = self.friday_close_prices[sym]
            current_mid = self.current_mids[sym]
            if friday_close == 0:
                continue
            premium_pct = (current_mid - friday_close) / friday_close * 100
            symbols_data[sym] = {
                "fridayClose": friday_close,
                "currentMid": current_mid,
                "premiumPct": round(premium_pct, 3),
            }

        if not symbols_data:
            return None

        return {
            "isWeekend": True,
            "fridayCloseTime": self.friday_close_time,
            "symbols": symbols_data,
        }

    async def run(self, bootstrap_addresses=None):
        # Bootstrap any addresses passed in
        if bootstrap_addresses:
            await self.bootstrap_addresses(bootstrap_addresses)

        # Run both things in the loop.
        await asyncio.gather(
            self.subscribe_trades(),
        )

    async def bootstrap_addresses(self, addresses):
        """Check given addresses for active TWAPs at startup."""
        print(f"Bootstrapping {len(addresses)} addresses...")
        for addr in addresses:
            addr = addr.strip()
            if not addr:
                continue
            print(f"  Checking {addr[:10]}...{addr[-6:]}...")
            states = await asyncio.to_thread(self.fetch_twap_states_for_user, addr)
            if states is None:
                print(f"    API error, skipping")
                continue
            if len(states) == 0:
                print(f"    No active TWAPs")
                continue

            # Found active TWAPs, add to tracking
            total_ntl = 0
            for state in states:
                coin = state.get("coin", "")
                sz = float(state.get("sz", 0))
                executed_sz = float(state.get("executedSz", 0))
                executed_ntl = float(state.get("executedNtl", 0))
                side = state.get("side", "?")

                if executed_sz > 0 and sz > 0:
                    remaining_sz = sz - executed_sz
                    total_ntl_est = executed_ntl * (sz / executed_sz)
                    remaining_ntl = total_ntl_est - executed_ntl
                    total_ntl += remaining_ntl
                    minutes = float(state.get("minutes", 0))
                    remaining_mins = minutes * (remaining_sz / sz) if sz > 0 else 0

                    side_str = "SELL" if side == "A" else "BUY"
                    print(f"    Found: {coin} {side_str} ${remaining_ntl:,.0f} remaining, ~{remaining_mins:.0f}m left")

                    # Add to tracking
                    if addr not in self.per_user_stats:
                        self.per_user_stats[addr] = {}
                    self.per_user_stats[addr][coin] = {
                        "coin": coin,
                        "side": side,
                        "remainingNtl": remaining_ntl,
                        "remainingMinutes": remaining_mins,
                        "totalNtl": total_ntl_est,
                        "remainingSz": remaining_sz,
                        "user": addr,
                        "lastUpdate": time.time(),
                    }

            if total_ntl > 0:
                self.ongoing_twap_addresses.add(addr)
                self.user_remaining_ntl[addr] = total_ntl
                self.enqueue_twap_subscription(addr)
                print(f"    Added to tracking: ${total_ntl:,.0f} total")

        print(f"Bootstrap complete. Tracking {len(self.ongoing_twap_addresses)} users.")

        
    # Goes through all symbols and publishes stats.
    async def try_publish(self):

        if time.time() - self.t_last_publish < self.publish_s:
            return

        if self.publish_task and not self.publish_task.done():
            return

        self.publish_task = asyncio.create_task(self._publish())

    async def _publish(self):
        try:
            # Fetch allMids for weekend premium tracking
            await asyncio.to_thread(self.fetch_all_mids)

            # Check if we should capture Friday close
            if self.should_capture_friday_close():
                self.capture_friday_close()

            # Get weekend premium data (None if not weekend or no data)
            weekend_premiums = self.get_weekend_premiums()

            # Don't publish before we have anything yet (TWAPs or weekend premiums)
            if len(self.per_user_stats) == 0 and weekend_premiums is None:
                return

            # Clean up stale entries (no update in 30 minutes = probably finished)
            # But first verify via API that the TWAP is actually gone
            stale_threshold = 30 * 60
            now = time.time()
            users_to_check = []
            for user in list(self.per_user_stats):
                is_stale = False
                for coin in self.per_user_stats[user]:
                    twap_state = self.per_user_stats[user][coin]
                    if now - twap_state.get("lastUpdate", now) > stale_threshold:
                        is_stale = True
                        break
                if is_stale:
                    users_to_check.append(user)

            # Verify each stale user via API before removing
            for user in users_to_check:
                stale_age_mins = (now - min(
                    s.get("lastUpdate", now) for s in self.per_user_stats[user].values()
                )) / 60
                print(f"User {user} appears stale ({stale_age_mins:.0f} mins), checking API...")

                active_states = await asyncio.to_thread(self.fetch_twap_states_for_user, user)

                if active_states is None:
                    # API error - keep tracking to be safe
                    print(f"  API error for {user}, keeping tracked")
                    continue

                if len(active_states) == 0:
                    # Confirmed: no active TWAPs, safe to remove
                    print(f"  Confirmed no active TWAPs for {user}, removing")
                    self.stop_tracking_user(user, reason="stale TWAP (verified via API)")
                else:
                    # TWAP still active! Reconcile tracked coins against API,
                    # then re-queue subscription.
                    active_api_coins = set()
                    total_remaining = 0
                    for state in active_states:
                        coin = state.get("coin", "")
                        if coin:
                            active_api_coins.add(coin)
                        sz = float(state.get("sz", 0))
                        executed_sz = float(state.get("executedSz", 0))
                        executed_ntl = float(state.get("executedNtl", 0))
                        if executed_sz > 0 and sz > 0:
                            remaining_sz = sz - executed_sz
                            total_ntl = executed_ntl * (sz / executed_sz)
                            remaining_ntl = total_ntl - executed_ntl
                            total_remaining += remaining_ntl
                    # Remove tracked coins not confirmed active by API
                    for coin in list(self.per_user_stats.get(user, {}).keys()):
                        if coin not in active_api_coins:
                            self.audit(user, "STALE_COIN_REMOVED", f"{coin} not in API during stale check")
                            del self.per_user_stats[user][coin]
                    if user in self.per_user_stats and len(self.per_user_stats[user]) == 0:
                        self.stop_tracking_user(user, reason="all TWAPs gone (stale check)")
                    else:
                        print(f"  TWAP still active for {user}! ~${total_remaining:,.0f} remaining. Re-subscribing.")
                        # Refresh lastUpdate only for confirmed-active coins
                        for coin in self.per_user_stats.get(user, {}):
                            self.per_user_stats[user][coin]["lastUpdate"] = now
                        # Re-queue websocket subscription
                        self.enqueue_twap_subscription(user)

            # Sanity check: verify ALL tracked users before publishing
            # This catches cancelled TWAPs that we haven't received updates about
            print(f"Pre-publish sanity check: verifying {len(self.per_user_stats)} tracked users...")
            users_to_verify = list(self.per_user_stats.keys())
            for user in users_to_verify:
                active_states = await asyncio.to_thread(self.fetch_twap_states_for_user, user)

                if active_states is None:
                    # API error - keep tracking to be safe, but log it
                    print(f"  API error verifying {user}, keeping in publish")
                    continue

                if len(active_states) == 0:
                    # No active TWAPs - user cancelled or completed, remove before publish
                    print(f"  User {user} has no active TWAPs (cancelled?), removing")
                    self.audit(user, "REMOVED_PRE_PUBLISH", "API confirmed no active TWAPs")
                    self.stop_tracking_user(user, reason="no active TWAPs on pre-publish check")
                    continue

                # Build set of coins with active TWAPs from API
                active_coins = set()
                for state in active_states:
                    coin = state.get("coin", "")
                    if coin:
                        active_coins.add(coin)

                # Remove any coins we're tracking that are no longer active
                tracked_coins = list(self.per_user_stats.get(user, {}).keys())
                for coin in tracked_coins:
                    if coin not in active_coins:
                        print(f"  User {user} coin {coin} no longer active, removing")
                        self.audit(user, "COIN_REMOVED_PRE_PUBLISH", f"{coin} not in API response (cancelled?)")
                        del self.per_user_stats[user][coin]

                # If user has no coins left, stop tracking
                if user in self.per_user_stats and len(self.per_user_stats[user]) == 0:
                    self.stop_tracking_user(user, reason="all TWAPs cancelled/completed")

            print(f"Sanity check complete: {len(self.per_user_stats)} users remaining")

            new_per_sym_stats = {}

            for user in self.per_user_stats:
                for coin in self.per_user_stats[user]:
                    twap_state = self.per_user_stats[user][coin]

                    # Add or subtract it to the per_sym_stats.

                    if coin not in new_per_sym_stats:
                        new_per_sym_stats[coin] = {
                            "netRemainingNtl": 0,
                            "numActiveTwaps": 0,
                            "users": [],  # List of {user, side, remainingNtl}
                        }

                    side_str = "sell" if twap_state["side"] == "A" else "buy"
                    new_per_sym_stats[coin]["users"].append({
                        "user": user,
                        "side": side_str,
                        "remainingNtl": twap_state["remainingNtl"],
                        "remainingMinutes": twap_state.get("remainingMinutes", 0),
                    })

                    if twap_state["side"] == "A":
                        # Hyperliquid marks "A" as the seller, so their remaining
                        # notional should pull the symbol's net lower
                        new_per_sym_stats[coin]["netRemainingNtl"] -= twap_state["remainingNtl"]
                        new_per_sym_stats[coin]["numActiveTwaps"] += 1
                    else:
                        new_per_sym_stats[coin]["netRemainingNtl"] += twap_state["remainingNtl"]
                        new_per_sym_stats[coin]["numActiveTwaps"] += 1

            # Add liquidity data for each symbol with active TWAPs
            for coin in new_per_sym_stats:
                net_ntl = new_per_sym_stats[coin]["netRemainingNtl"]
                if net_ntl == 0:
                    continue

                side = "buy" if net_ntl > 0 else "sell"
                l2_data = await asyncio.to_thread(self.fetch_l2_book, coin)
                liquidity = self.calculate_liquidity_within_bps(l2_data, side, bps=50)

                if liquidity and liquidity > 0:
                    new_per_sym_stats[coin]["liquidity50bps"] = round(liquidity, 2)
                    new_per_sym_stats[coin]["twapPctOfLiquidity"] = round(abs(net_ntl) / liquidity * 100, 1)

            #OK, now we should have stats for this, right?

            # Update timestamp before publishing
            self.t_last_publish = time.time()

            print("*"* 100)
            print("Publishing!")
            print(new_per_sym_stats)

            # Build full state payload
            et_now = datetime.now(ZoneInfo("America/New_York"))
            state_payload = {
                "twaps": new_per_sym_stats,
                "lastUpdated": et_now.isoformat(),
            }

            # Add weekend premiums if available
            if weekend_premiums:
                state_payload["weekendPremiums"] = weekend_premiums

            # Persist Friday close data for recovery after restart
            if self.friday_close_prices:
                state_payload["_fridayCloseData"] = {
                    "prices": self.friday_close_prices,
                    "time": self.friday_close_time,
                }

            await asyncio.to_thread(self.persist_state_payload, state_payload)

            print("*"* 100)
        finally:
            self.publish_task = None

    def persist_state_payload(self, state_payload):
        # Write it to GitHub Pages directory
        tgt_file = os.path.expanduser("~/tradefi/itsdchen.github.io/state.json")
        with open(tgt_file, "w") as f:
            f.write(json.dumps(state_payload, indent=2))

        # Auto-commit and push to GitHub Pages
        git_dir = os.path.expanduser("~/tradefi/itsdchen.github.io")
        try:
            subprocess.run(["git", "-C", git_dir, "add", "state.json"], check=True)
            subprocess.run(["git", "-C", git_dir, "commit", "-m", "Update TWAP stats"], check=False)
            subprocess.run(["git", "-C", git_dir, "push"], check=True)
            print("Successfully pushed to GitHub Pages")
        except subprocess.CalledProcessError as e:
            print(f"Git operation failed: {e}")

    def enqueue_twap_subscription(self, user):
        if user in self.pending_twap_users_set:
            return
        # Don't subscribe if we're pending unsubscribe
        if user in self.pending_unsub_users_set:
            return

        self.pending_twap_users.append(user)
        self.pending_twap_users_set.add(user)

    def remove_pending_twap_subscription(self, user):
        if user in self.pending_twap_users_set:
            try:
                self.pending_twap_users.remove(user)
            except ValueError:
                pass
            self.pending_twap_users_set.discard(user)

    def enqueue_twap_unsubscription(self, user):
        """Queue an unsubscription for a user we're no longer tracking."""
        # Remove from pending subscribe queue if there
        self.remove_pending_twap_subscription(user)

        # Only need to unsub if we're actually subscribed
        if user not in self.subscribed_users:
            return

        if user in self.pending_unsub_users_set:
            return

        self.pending_unsub_users.append(user)
        self.pending_unsub_users_set.add(user)

    async def flush_twap_subscription_queue(self, ws):
        if not self.pending_twap_users and not self.pending_unsub_users:
            return

        if (time.time() - self.t_last_twap_flush) < self.twap_flush_interval:
            return

        send_count = 0

        # Process unsubscriptions FIRST to free up slots
        while self.pending_unsub_users and send_count < self.max_twap_subs_per_flush:
            user = self.pending_unsub_users.popleft()
            self.pending_unsub_users_set.discard(user)
            unsub_json = json.dumps({
                "method": "unsubscribe",
                "subscription": {"type": "twapStates", "user": user, "dex": "xyz"},
            })
            try:
                await ws.send(unsub_json)
                self.subscribed_users.discard(user)
                print(f"Unsubscribed from twapStates for {user[:10]}...{user[-6:]}")
            except Exception as e:
                print(f"Failed to unsubscribe {user}: {e}")
                self.pending_unsub_users.appendleft(user)
                self.pending_unsub_users_set.add(user)
                break
            send_count += 1

        # Then process new subscriptions
        while self.pending_twap_users and send_count < self.max_twap_subs_per_flush:
            user = self.pending_twap_users.popleft()
            self.pending_twap_users_set.discard(user)

            # Skip if already subscribed
            if user in self.subscribed_users:
                continue

            twap_sub_json = json.dumps({
                "method": "subscribe",
                "subscription": {"type": "twapStates", "user": user, "dex": "xyz"},
            })
            try:
                await ws.send(twap_sub_json)
                self.subscribed_users.add(user)
            except Exception as e:
                print(f"Failed to send twap subscription for {user}: {e}")
                # Re-queue and bail so we retry on the next loop / connection
                self.pending_twap_users.appendleft(user)
                self.pending_twap_users_set.add(user)
                break
            send_count += 1

        if send_count > 0:
            self.t_last_twap_flush = time.time()

    def audit(self, user, action, details=""):
        """Record an action taken on a user for debugging."""
        et_now = datetime.now(ZoneInfo("America/New_York"))
        today = et_now.date()

        # Clear audit log at start of new day
        if today != self.audit_log_day:
            print(f"New day ({today}), clearing audit log")
            self.audit_log.clear()
            self.audit_log_day = today

        timestamp_str = et_now.strftime("%H:%M:%S")
        entry = (timestamp_str, action, details)
        self.audit_log[user].append(entry)
        # Keep only recent entries per address
        if len(self.audit_log[user]) > self.max_audit_entries_per_address:
            self.audit_log[user] = self.audit_log[user][-self.max_audit_entries_per_address:]

        # Periodically save to file
        now = time.time()
        if now - self.t_last_audit_save > self.audit_save_interval:
            self.save_audit_log()
            self.t_last_audit_save = now

    def save_audit_log(self):
        """Save audit log to file for external reading."""
        try:
            data = {
                "date": str(self.audit_log_day),
                "saved_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                "entries": {k: list(v) for k, v in self.audit_log.items()},
            }
            with open(self.audit_log_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error saving audit log: {e}")

    @staticmethod
    def load_audit_log_from_file(filepath=None):
        """Load audit log from file (static method for read-only access)."""
        if filepath is None:
            filepath = os.path.expanduser("~/tradefi/itsdchen.github.io/audit_log.json")
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            print(f"Error loading audit log: {e}")
            return None

    def dump_audit(self, user):
        """Print audit log for a specific user."""
        if user not in self.audit_log:
            print(f"No audit log for {user}")
            return
        print(f"\n{'='*80}")
        print(f"AUDIT LOG for {user}")
        print(f"{'='*80}")
        for timestamp, action, details in self.audit_log[user]:
            print(f"  [{timestamp}] {action}: {details}")
        print(f"{'='*80}\n")

    def check_address_now(self, user):
        """Query API for current TWAP state of an address and print it."""
        print(f"\nChecking {user} via API...")
        states = self.fetch_twap_states_for_user(user)
        if states is None:
            print("  API error")
            return
        if len(states) == 0:
            print("  No active TWAPs")
            return
        print(f"  Found {len(states)} active TWAP(s):")
        for state in states:
            coin = state.get("coin", "?")
            side = "SELL" if state.get("side") == "A" else "BUY"
            sz = float(state.get("sz", 0))
            executed_sz = float(state.get("executedSz", 0))
            executed_ntl = float(state.get("executedNtl", 0))
            if executed_sz > 0 and sz > 0:
                remaining_ntl = executed_ntl * (sz / executed_sz) - executed_ntl
                print(f"    {coin} {side} ${remaining_ntl:,.0f} remaining")

    def stop_tracking_user(self, user, reason=None):
        if reason:
            print(f"Stopping TWAP tracking for {user}: {reason}")
            self.audit(user, "STOP_TRACKING", reason)
        if user in self.per_user_stats:
            del self.per_user_stats[user]
        self.ongoing_twap_addresses.discard(user)
        self.remove_pending_twap_subscription(user)
        self.user_remaining_ntl.pop(user, None)
        # Queue unsubscription to free up the slot
        self.enqueue_twap_unsubscription(user)

    def dump_status(self):
        """Print current tracking status for debugging."""
        now = time.time()
        if now - self.t_last_status_dump < self.status_dump_interval:
            return
        self.t_last_status_dump = now

        print("\n" + "=" * 80)
        print(f"STATUS DUMP @ {datetime.now(ZoneInfo('America/New_York')).strftime('%H:%M:%S ET')}")
        print(f"Tracking {len(self.ongoing_twap_addresses)} users, {len(self.per_user_stats)} with stats")
        print(f"WS subscriptions: {len(self.subscribed_users)}/10 | Pending sub: {len(self.pending_twap_users)} | Pending unsub: {len(self.pending_unsub_users)}")

        if self.per_user_stats:
            print("\nActive TWAPs:")
            for user in self.per_user_stats:
                short_user = user[:10] + "..." + user[-6:]
                for coin, state in self.per_user_stats[user].items():
                    side = "BUY" if state["side"] == "B" else "SELL"
                    age_mins = (now - state.get("lastUpdate", now)) / 60
                    print(f"  {short_user} | {coin:20} | {side:4} | ${state['remainingNtl']:>12,.0f} | {state.get('remainingMinutes', 0):>5.0f}m left | updated {age_mins:.1f}m ago")

        # Show what the smallest tracked TWAP is
        smallest_user, smallest_val = self.get_smallest_tracked_user()
        if smallest_user:
            print(f"\nSmallest tracked: ${smallest_val:,.0f} (new TWAPs must exceed this)")

        # Show recently skipped addresses (not currently tracked)
        skipped = []
        for user, entries in self.audit_log.items():
            if user not in self.ongoing_twap_addresses:
                # Find last SKIPPED entry
                for ts, action, details in reversed(entries):
                    if action == "SKIPPED_CAPACITY":
                        short_user = user[:10] + "..." + user[-6:]
                        skipped.append((ts, short_user, details))
                        break
        if skipped:
            print(f"\nRecently skipped (not tracking, use --check <addr> to verify):")
            for ts, user, details in skipped[-5:]:  # Show last 5
                print(f"  [{ts}] {user}: {details}")

        print("=" * 80 + "\n")

    def get_smallest_tracked_user(self):
        if not self.user_remaining_ntl:
            return None, None
        smallest_user = min(
            self.user_remaining_ntl,
            key=lambda u: self.user_remaining_ntl.get(u, 0),
        )
        return smallest_user, self.user_remaining_ntl.get(smallest_user, 0)

    def should_track_new_user(self, user, approx_ntl):
        if user in self.ongoing_twap_addresses:
            prev = self.user_remaining_ntl.get(user, 0)
            if approx_ntl > prev:
                self.user_remaining_ntl[user] = approx_ntl
            return True

        if len(self.ongoing_twap_addresses) < self.max_tracked_users:
            self.user_remaining_ntl[user] = approx_ntl
            return True

        smallest_user, smallest_val = self.get_smallest_tracked_user()
        if smallest_user is None:
            return False

        if approx_ntl <= smallest_val:
            return False

        self.stop_tracking_user(
            smallest_user,
            reason=f"evicted for larger TWAP approx {approx_ntl:,.0f} > {smallest_val:,.0f}",
        )
        self.user_remaining_ntl[user] = approx_ntl
        return True

    def prune_to_capacity(self):
        while len(self.ongoing_twap_addresses) > self.max_tracked_users:
            smallest_user, _ = self.get_smallest_tracked_user()
            if smallest_user is None:
                break
            self.stop_tracking_user(smallest_user, reason="prune to capacity")

    def update_user_priority(self, user):
        if user not in self.per_user_stats or len(self.per_user_stats[user]) == 0:
            self.user_remaining_ntl.pop(user, None)
            return 0.0
        total_ntl = 0.0
        for coin in self.per_user_stats[user]:
            total_ntl += self.per_user_stats[user][coin]["remainingNtl"]
        self.user_remaining_ntl[user] = total_ntl
        return total_ntl

    def handle_twap_state(self, twap_dct):
        dex = twap_dct["dex"]
        user = twap_dct["user"]
        states = twap_dct["states"]

        # Let's ignore the hyperliquid for now.
        if len(dex) == 0:
            return

        if user not in self.per_user_stats:
            self.per_user_stats[user] = {}

        #print("TWAP STATE: {}".format(twap_dct))

        # Lets us figure out size, remaining size of the twap. 
        # TWAP STATE: {'dex': 'xyz', 'user': '0x9d097697e5b82a52f6b82c16c148a753bbf5c8ae', 'states': [[1482890, {'coin': 'xyz:XYZ100', 'user': '0x9d097697e5b82a52f6b82c16c148a753bbf5c8ae', 'side': 'A', 'sz': '8.4868', 'executedSz': '1.5416', 'executedNtl': '39158.0961', 'minutes': 1395, 'reduceOnly': False, 'randomize': False, 'timestamp': 1767183763535}]]}

        # side = A/B 
        # A = seller (I think)


        for one_state_lst in twap_dct["states"]:
            # For some reason this is a list of 2 things, and the second thing
            # is the thing we want. 
            one_state = one_state_lst[1] 

            coin = one_state["coin"]
            side = one_state["side"]
            side_str = "SELL" if side == "A" else "BUY" 
            sz = float(one_state["sz"])
            executedSz = float(one_state["executedSz"])
            executedNtl = float(one_state["executedNtl"])

            # Just in case, I don't want to get into /0 errors at the start
            if executedNtl < 10 or executedSz == 0 or sz == 0:
                continue

            totalNtl = executedNtl * (sz / executedSz)
            remainingSz = sz - executedSz
            remainingNtl = totalNtl - executedNtl
            # Not sure yet if this is total minutes for the twap, or 
            # remaining minutes 
            # OK, after running for a while, this looks like total 
            # minutes for the twap. 
            minutes = float(one_state["minutes"])
            remainingMinutes = round( minutes * (remainingSz / sz), 0)

            twap_state = {
                "coin": coin,
                "side": side,
                "remainingNtl": remainingNtl,
                "remainingMinutes": remainingMinutes,
                "totalNtl": totalNtl,
                "remainingSz": remainingSz,
                "user": user,
                "lastUpdate": time.time(),
            }
            if remainingNtl > 1000:
                self.per_user_stats[user][coin] = twap_state
                self.audit(user, "STATE_UPDATE", f"{coin} {side_str} ${remainingNtl:,.0f} remaining")
            else:
                # Probably it's about to go away, so remove it.
                self.audit(user, "TWAP_NEARLY_DONE", f"{coin} ${remainingNtl:,.0f} < $1000 threshold")
                if user in self.per_user_stats and coin in self.per_user_stats[user]:
                    del self.per_user_stats[user][coin]
                    # Clean up empty user dict to avoid accumulation
                    if len(self.per_user_stats[user]) == 0:
                        self.stop_tracking_user(user, reason="completed TWAP")

        # Reconcile: remove tracked coins for this dex that are no longer
        # in the update (i.e. the TWAP was cancelled or fully completed).
        # Each twapStates message contains ALL active states for a user+dex,
        # so any tracked coin absent from the update is gone.
        if user in self.per_user_stats:
            coins_in_update = set()
            for one_state_lst in states:
                coins_in_update.add(one_state_lst[1]["coin"])
            dex_prefix = dex + ":"
            for coin in list(self.per_user_stats[user].keys()):
                if coin.startswith(dex_prefix) and coin not in coins_in_update:
                    self.audit(user, "TWAP_GONE", f"{coin} absent from state update (cancelled/completed?)")
                    del self.per_user_stats[user][coin]

        total_remaining = self.update_user_priority(user)
        if total_remaining <= 0:
            self.stop_tracking_user(user, reason="no remaining TWAP")
        else:
            self.prune_to_capacity()

    async def subscribe_trades(self, debug=False):
        """
        Subscribe to trade feed for a symbol and record all trades with counterparties
        """
        while True:
            try:
                await self._subscribe_trades_inner(debug)
            except websockets.exceptions.ConnectionClosed as e:
                print(f"WebSocket connection closed ({e.code}: {e.reason}), reconnecting in 1s...")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"Unexpected error in subscribe_trades: {repr(e)}, reconnecting in 5s...")
                traceback.print_exc()
                await asyncio.sleep(5)

    async def _subscribe_trades_inner(self, debug=False):
        """Inner loop that handles a single WebSocket connection"""
        last_t_ping = time.time()

        async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
            try:
                # Clear subscription tracking on new connection (old subs are gone)
                self.subscribed_users.clear()
                self.pending_unsub_users.clear()
                self.pending_unsub_users_set.clear()

                for one_sym in self.syms:
                    # Subscribe to trades
                    trade_sub_json = json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": one_sym, "dex": "xyz"},
                    })
                    #print("Subscribing to {}".format(one_sym))
                    await ws.send(trade_sub_json)

                # Re-enqueue any previously tracked addresses so we resubscribe after reconnects
                for user in list(self.ongoing_twap_addresses):
                    self.enqueue_twap_subscription(user)

            except Exception as e:
                traceback.print_exc()
                sys.exit(1)

            while True:
                # Send ping to keep connection alive
                if (time.time() - last_t_ping) > 40:
                    await ws.send(json.dumps({"method": "ping"}))
                    last_t_ping = time.time()

                await self.try_publish()
                await self.flush_twap_subscription_queue(ws)
                self.dump_status()

                try:
                    message = await asyncio.wait_for(ws.recv(), 0.01)
                    msg_dct = json.loads(message)

                    # Handle different message types
                    if 'channel' in msg_dct and msg_dct['channel'] == 'trades':
                        # Process trade data
                        if 'data' in msg_dct:
                            for trade in msg_dct['data']:
                                # Debug mode: print raw trade data
                                if debug:
                                    print(f"\n{'='*80}")
                                    print("RAW TRADE DATA:")
                                    print(json.dumps(trade, indent=2))
                                    print(f"{'='*80}")

                                # Check for hash field to detect TWAP
                                trade_hash = trade.get('hash', 'no_hash_field')
                                zero_hash = "0x0000000000000000000000000000000000000000000000000000000000000000"
                                is_twap_by_hash = (trade_hash == zero_hash)

                                # This only cares about twaps. 
                                if not is_twap_by_hash:
                                    continue

                                # Check which address it's in. 
                                #print("Woo")



                                # Validate users array before accessing
                                users = trade.get("users", [])
                                if len(users) < 2:
                                    continue

                                taker_shs = float(trade["sz"])
                                px = float(trade["px"])

                                # I think this means which side the taker was?
                                if trade["side"] == "B":
                                    # Taker was a buyer
                                    taker = users[0]
                                    maker = users[1]
                                    #print("BUYING")
                                else:
                                    #print("SELLING")
                                    taker = users[1]
                                    maker = users[0]
                                    taker_shs *= -1
                                
                                # The taker is the one that's twapping.
                                coin = trade.get("coin", "unknown")
                                approx_ntl = abs(taker_shs * px)
                                self.audit(taker, "TWAP_TRADE_SEEN", f"{coin} ~${approx_ntl:,.0f}")

                                if taker not in self.ongoing_twap_addresses:
                                    smallest_user, smallest_val = self.get_smallest_tracked_user()
                                    if not self.should_track_new_user(taker, approx_ntl):
                                        self.audit(taker, "SKIPPED_CAPACITY",
                                            f"{coin} ${approx_ntl:,.0f} < min ${smallest_val or 0:,.0f}, "
                                            f"tracking {len(self.ongoing_twap_addresses)}/{self.max_tracked_users}")
                                        print(
                                            f"Skipping {taker[:10]}...{taker[-6:]} on {coin}, "
                                            f"approx ${approx_ntl:,.0f} < min ${smallest_val or 0:,.0f} "
                                            f"(tracking {len(self.ongoing_twap_addresses)}/{self.max_tracked_users})"
                                        )
                                        continue
                                    self.audit(taker, "STARTED_TRACKING", f"{coin} ~${approx_ntl:,.0f}")
                                    print(f"New TWAP detected: {taker[:10]}...{taker[-6:]} on {coin}, ~${approx_ntl:,.0f}")
                                    self.ongoing_twap_addresses.add(taker)
                                    # Queue a throttled subscription to avoid flooding the server
                                    self.enqueue_twap_subscription(taker)


                                #self.register_trade(taker, taker_shs, px, is_twap_by_hash, True)
                                # The maker is not a part of the twap, by definition. 
                                #self.register_trade(maker, -taker_shs, px, False, False)


                                # Great, we actually have a bunch of twaps! Nice!
                                #print(trade)

                                # OK, now we can add some details to our stuff. 
                    elif 'method' in msg_dct and msg_dct['method'] == 'pong':
                        # Pong response, keep going
                        pass
                    elif 'channel' in msg_dct and msg_dct['channel'] == 'subscriptionResponse':
                        if isinstance(msg_dct.get('data'), dict) and not msg_dct['data'].get('success', True):
                            print(f"Subscription error: {msg_dct}")

                    elif 'channel' in msg_dct and msg_dct["channel"] == "twapStates":
                        # Handle twap states. 
                        self.handle_twap_state(msg_dct["data"])
#{'channel': 'twapStates', 'data': {'dex': '', 'user': '0x9d097697e5b82a52f6b82c16c148a753bbf5c8ae', 'states': [[1482877, {'coin': 'APEX', 'user': '0x9d097697e5b82a52f6b82c16c148a753bbf5c8ae', 'side': 'A', 'sz': '142980.0', 'executedSz': '25747.0', 'executedNtl': '12938.40495', 'minutes': 1410, 'reduceOnly': False, 'randomize': False, 'timestamp': 1767183189545}], [1482878, {'coin': 'MNT', 'user': '0x9d097697e5b82a52f6b82c16c148a753bbf5c8ae', 'side': 'B', 'sz': '93835.3', 'executedSz': '16797.8', 'executedNtl': '16283.878555', 'minutes': 1410, 'reduceOnly': False, 'randomize': False, 'timestamp': 1767183282698}], [1482885, {'coin': 'ETH', 'user': '0x9d097697e5b82a52f6b82c16c148a753bbf5c8ae', 'side': 'A', 'sz': '141.3281', 'executedSz': '24.8488', 'executedNtl': '74324.9686', 'minutes': 1410, 'reduceOnly': False, 'randomize': False, 'timestamp': 1767183565068}]]}}

                    elif 'channel' in msg_dct and msg_dct["channel"] == "pong":
                        continue

                    else:
                        # Unknown message type
                        print(f"Received message: {msg_dct}")
                        pass

                except asyncio.exceptions.TimeoutError:
                    # No message received, continue
                    pass
                except websockets.exceptions.ConnectionClosed:
                    # Let this bubble up to the reconnection handler
                    raise
                except Exception as e:
                    print("-----------------------------------")
                    print("Got exception in receive loop!")
                    print(f"Repr (repr(e)): {repr(e)}")
                    print("-----------------------------------")
                    print("Full Traceback:")
                    traceback.print_exc()
                    print("-----------------------------------")
                    raise  # Bubble up for reconnection



def main():
    parser = argparse.ArgumentParser(
        description="Monitor Hyperliquid trades and identify TWAP vs market trades"
    )

    # Eventaully, if not given, have it do all hip3 symbols.
    # There's a way of pushing this to the netlify file.
    parser.add_argument(
        "--sym",
        type=str,
    )
    parser.add_argument(
        "--watch",
        type=str,
        nargs="+",
        help="Addresses to check for active TWAPs at startup",
    )
    parser.add_argument(
        "--watch-file",
        type=str,
        help="File containing addresses to watch (one per line)",
    )
    parser.add_argument(
        "--check",
        type=str,
        help="Check a specific address for active TWAPs via API (read-only, won't interfere with running instance)",
    )
    parser.add_argument(
        "--audit",
        type=str,
        help="Show audit log for a specific address (reads from saved file, won't interfere)",
    )
    parser.add_argument(
        "--audit-search",
        type=str,
        help="Search audit log for addresses containing this string",
    )
    args = parser.parse_args()

    # Read-only modes - don't interfere with running instance
    if args.check:
        # Just query API, no websockets
        print(f"Checking {args.check} via API (read-only)...")
        stw = SymTwapWatcher()
        stw.check_address_now(args.check)
        sys.exit(0)

    if args.audit:
        # Read from saved audit log file
        data = SymTwapWatcher.load_audit_log_from_file()
        if data is None:
            print("No audit log file found. Is the main script running?")
            sys.exit(1)
        addr = args.audit.lower()
        found = False
        for user, entries in data.get("entries", {}).items():
            if addr in user.lower():
                found = True
                print(f"\n{'='*80}")
                print(f"AUDIT LOG for {user}")
                print(f"Log date: {data.get('date')}, saved at: {data.get('saved_at')}")
                print(f"{'='*80}")
                for ts, action, details in entries:
                    print(f"  [{ts}] {action}: {details}")
        if not found:
            print(f"No audit entries found for address containing '{addr}'")
        sys.exit(0)

    if args.audit_search:
        # Search all addresses in audit log
        data = SymTwapWatcher.load_audit_log_from_file()
        if data is None:
            print("No audit log file found. Is the main script running?")
            sys.exit(1)
        search = args.audit_search.lower()
        matches = []
        for user, entries in data.get("entries", {}).items():
            if search in user.lower():
                matches.append((user, len(entries)))
            else:
                # Also search in entry details
                for ts, action, details in entries:
                    if search in details.lower():
                        matches.append((user, len(entries)))
                        break
        if matches:
            print(f"Found {len(matches)} address(es) matching '{search}':")
            for user, count in matches:
                print(f"  {user} ({count} entries)")
            print(f"\nUse --audit <address> to see full log")
        else:
            print(f"No addresses found matching '{search}'")
        sys.exit(0)

    # Collect addresses to bootstrap
    bootstrap_addresses = []
    if args.watch:
        bootstrap_addresses.extend(args.watch)
    if args.watch_file:
        try:
            with open(args.watch_file, "r") as f:
                for line in f:
                    addr = line.strip()
                    if addr and not addr.startswith("#"):
                        bootstrap_addresses.append(addr)
        except FileNotFoundError:
            print(f"Warning: watch file {args.watch_file} not found")

    try:
        stw = SymTwapWatcher()
        uvloop.run(stw.run(bootstrap_addresses=bootstrap_addresses if bootstrap_addresses else None))

    except KeyboardInterrupt:
        print("\nShutting down trade checker...")
        sys.exit(0)


if __name__ == "__main__":
    main()
