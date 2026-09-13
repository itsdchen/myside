#! /usr/bin/env python

"""
Used to check oracle and


"""

import asyncio
import argparse
import getpass
import math
import os
import requests
import sys
import time
import json
import zmq
import zmq.asyncio
import uvloop

sys.path.append("{}/..".format(os.path.dirname(os.path.abspath(__file__))))

sys.path.append("{}/../util".format(os.path.dirname(os.path.abspath(__file__))))

# If running from tools/ directory, add pyfeed to path for mdmsg_pb2
if "tools/" in os.path.abspath(__file__):
    sys.path.append("{}/../pyfeed".format(os.path.dirname(os.path.abspath(__file__))))

# This needs to be just copied over.
# from strat_main.util import symbolizer
import symbolizer

# import gateway_pb2
import datetime

import websockets
from tenacity import wait_fixed, retry
import traceback

from concurrent.futures import CancelledError
from util import email_utils

import mdmsg_pb2

import pytz


"""
A set of checks that the published oracle is behaving correctly. 

Specifically, I want to check that: 

1 - If it's during CME market hours (eg. we got a recent quote), 
    the published oracle matches up (is within a small # of bps )
2 - The oracle doesn't appear to be stuck. I will have it complain if it's publishing
    the same value for the last 10 minutes. 
3 - If there is a large recent jump in oracle (>1%) AND we didn't just get a 
    CME value that justified it. 

For now: going to just write this for one symbol, and then build more stuff in. 

This is just pretty annoying, is the thing. 
    

"""


SUB_ADDRESS = "ipc:///tmp/feed-sock"


class OracleChecker:
    def __init__(self, emails):
        self.emails = emails
        self.wanted_dex_syms = ["xyz:XYZ100"]

        # In preparation for external symbols we dont need to discount
        self.wanted_real_syms = []

        # Things we need to do rfr-based discounting. 
        self.wanted_cme_syms = []
        for one_dex_sym in self.wanted_dex_syms:
            if one_dex_sym in symbolizer.dex_to_real:
                real_sym = symbolizer.dex_to_real[one_dex_sym]
                # Check if it's a CMESym. 
                # TODO: update this w/ more structure when 
                if real_sym in symbolizer.sym_to_expiry:
                    self.wanted_cme_syms.append(real_sym)
                self.wanted_real_syms.append(real_sym)


        # Dictionary to store last mid price for each symbol
        # Format: {symbol_id: {'mid': float, 'timestamp': int, 'implied_oracle': float}}
        self.last_mid_calced_oracles = {}

        # Sample the mid this many seconds in between.
        self.sample_mid_t = 1
        self.record_hl_oracle_t = 3

        # Recording the last hyperliquid-reported oracle prices. 
        # Symbol -> list of oracle prices.
        # Sample this every 3 seconds.
        # Keep up to 5 minutes worth.
        self.last_oracle_pxs = {}
        # We probably don't expect oracle to jump 1%
        # unless if the implied price allowed for it.
        self.oracle_jump_thresh = 0.01

        # Only check for stale oracles after 5 updates from hyperliquid
        self.n_before_stale_check = 5

        # Tolerance between ours and theirs 
        self.theirs_vs_ours_oracle_thresh = 0.0003

        # Store this many of their oracles for staleness checks.
        self.num_oracles_stored = self.check_t_secs / self.record_hl_oracle_t

        # Initialize the stuff with our wanted_syms I guess.
        for one_sym in self.wanted_cme_syms:
            self.last_oracle_pxs[one_sym] = []

        # Run the big check this often. 
        self.check_t_secs = 5 * 60 
        # ZMQ setup
        self.context = zmq.asyncio.Context()

        self.sub_socket = self.context.socket(zmq.SUB)
        # Connect to the PUB socket's address
        self.sub_socket.connect(SUB_ADDRESS)
        # Subscribe to all messages (empty string subscribes to everything)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")
        print(f"Connected to ZMQ socket: {SUB_ADDRESS}")

    def translate_cme_to_oracle(self, cme_sym, cme_px):
        """
        Translate CME futures price to spot-equivalent oracle price.
        Discounts the futures price based on risk-free rate and time to expiry.
        """

        now_t = int(time.time())
        sym_rfr = symbolizer.sym_to_rfr[cme_sym]
        sym_expiry_s = symbolizer.get_expiry_t(cme_sym, now_t)


        # Get current time in the same timezone

        time_to_expiry = sym_expiry_s - now_t
        # Calculate time to expiry in seconds

        # Handle expired futures
        if time_to_expiry <= 0:
            print(f"Warning: {cme_sym} has expired or expires at {sym_expiry_s}")
            return cme_px

        # Discount the futures price to spot
        # Using continuous compounding: spot = futures / exp(rfr * time_in_years)
        # Or simple interest: spot = futures / (1 + rfr * time_in_years)
        seconds_per_year = 365 * 24 * 3600
        time_in_years = time_to_expiry / seconds_per_year

        # Using continuous interest
        discount_factor = math.exp(time_in_years * sym_rfr)

        oracle_px = cme_px / discount_factor
        # print("Calculated {}".format(oracle_px))
        return oracle_px

    # We only care about the CME symbols right now, so we only expect quotes.
    def process_message(self, raw_message: bytes):
        """Process incoming ZMQ message and update last_mid."""
        try:
            # Parse protobuf message
            pb_msg = mdmsg_pb2.PbMessage()
            pb_msg.ParseFromString(raw_message)

            symbol = pb_msg.symbol_id
            mid = None
            timestamp = int(time.time())  # milliseconds
            # Only update if enough time has passed.

            if symbol not in self.wanted_cme_syms:
                return

            # Extract mid price based on message type
            if pb_msg.HasField("quote"):
                # For equity/CME markets with PbQuote
                mid = pb_msg.quote.mid_px
                our_oracle = self.translate_cme_to_oracle(symbol, mid)
                # print(f"[{symbol}] mid={mid:.2f} our_oracle={our_oracle:.2f}")
                # Check if we should update our mids.

                if symbol not in self.last_mid_calced_oracles or (
                   timestamp - self.last_mid_calced_oracles[symbol]["timestamp"]
                   > self.sample_mid_t
                ):
                    # Update it.
                    self.last_mid_calced_oracles[symbol] = {
                        "mid": mid,
                        "timestamp": timestamp,
                        "implied_oracle": our_oracle,
                    }
                    #print("{}: We updated {:.2f}".format(timestamp, our_oracle))
        except Exception as e:
            print(f"Error processing message: {e}")
            traceback.print_exc()

    @retry(wait=wait_fixed(0.5))
    async def sub_hl(self):
        hl_url = "wss://api.hyperliquid.xyz/ws"

        # Get all the symbols we want, start websocket, start subscribing.
        async with websockets.connect(hl_url, ping_interval=100000000) as ws:
            print("Own ordersWebsocket connected at {}".format(hl_url))

            try:
                for one_sym in self.wanted_dex_syms:
                    dex_index = one_sym.find(":")
                    ctx_sub_dct = {
                        "method": "subscribe",
                        "subscription": {"type": "activeAssetCtx", "coin": one_sym},
                    }

                    if dex_index != -1:
                        dex_name = one_sym[:dex_index]
                        ctx_sub_dct["subscription"]["dex"] = dex_name

                    sub_json = json.dumps(ctx_sub_dct)
                    await ws.send(sub_json)
                    # print("Subscribing like {}".format(l2_sub_dct))

            except Exception as e:
                print("Got exception: {}".format(e))

            last_t_ping = time.time()
            while True:
                try:

                    # Consider sending ping.
                    if (time.time() - last_t_ping) > 40:
                        # Do my own ping. I guess this is the "right" way that they
                        # do ping/pongs.
                        await ws.send(json.dumps({"method": "ping"}))
                        last_t_ping = time.time()

                    # message = await websocket.recv()
                    message = await asyncio.wait_for(ws.recv(), 3)
                    try:
                        msg_dct = json.loads(message)
                        # print(msg_dct)
                    except json.JSONDecodeError as e:
                        print(f"Invalid JSON received: {e}, message: {message[:100]}")
                        continue  # Skip this message, don't crash

                    if msg_dct["channel"] == "activeAssetCtx":

                        # print(msg_dct)
                        dex_name = msg_dct["data"]["coin"]
                        real_name = symbolizer.dex_to_real[dex_name]

                        oracle_px = float(msg_dct["data"]["ctx"]["oraclePx"])
                        rcv_t = int(time.time())

                        if len(self.last_oracle_pxs[real_name]) == 0:
                            self.last_oracle_pxs[real_name].append(
                                {"oraclePx": oracle_px, "t": rcv_t}
                            )
                        else:
                            # Check if we should
                            last_recorded_px = self.last_oracle_pxs[real_name][-1]
                            if rcv_t - last_recorded_px["t"] < self.record_hl_oracle_t:
                                continue
                            # Otherwise, push it back.
                            update_dct = {"oraclePx": oracle_px, "t": rcv_t}
                            #print(
                            #    "{}: HL updated oracle px  {}".format(rcv_t, oracle_px)
                            #)
                            self.last_oracle_pxs[real_name].append(update_dct)
                        pass
                    elif msg_dct["channel"] == "subscriptionResponse":
                        # print(msg_dct)
                        pass
                    else:
                        # Probably something else.
                        pass

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"Error in websocket loop: {e}")
                    raise  # This will trigger the @retry decorator


    async def check_oracle(self):
        # For each symbol we have oracle data for,
        # (aka NQ), we go through some sanity checks.

        # Check 1, do the oracles seem stale?

        alert_lines = []

        while True:
            print("{}: Performing oracle checks.".format(time.time()))

            # Go through all our syms to perform  
            for one_real_sym in self.wanted_real_syms:

                # Clean up the oracles, actually. If there are too many,
                self.last_oracle_pxs[one_real_sym] = self.last_oracle_pxs[one_real_sym][
                    -self.num_oracles_stored :
                ]

                if len(self.last_oracle_pxs[one_real_sym]) == 0:
                    await asyncio.sleep(self.check_t_secs)
                    continue

                # First, check if they're stale.
                first_oracle_px = self.last_oracle_pxs[one_real_sym][0]["oraclePx"]
                oracle_changed = any(px != first_oracle_px for px in self.last_oracle_pxs[one_real_sym])              

                # If we just started sampling, don't worry. 
                if len(self.last_oracle_pxs[one_real_sym]) <= self.n_before_stale_check:
                    oracle_changed = True
                if not oracle_changed:
                    #print("A tragedy, these oracles are all the same!")
                    alert_lines.append("{} {}: oracles have been the same - oldest record is {} | oracles  {}".format(time.time(), one_real_sym,self.last_oracle_pxs[one_real_sym][0]["t"],  self.last_oracle_pxs[one_real_sym]))

                #print(
                #    "last_oracle_pxs = {}".format(
                #        json.dumps(self.last_oracle_pxs[one_real_sym], indent=2)
                #    )
                #)

                # 2, does the last one match us?
                last_oracle_px = self.last_oracle_pxs[one_real_sym][-1]["oraclePx"]
                last_oracle_t = self.last_oracle_pxs[one_real_sym][-1]["t"]
                our_last_oracle = self.last_mid_calced_oracles[one_real_sym]["implied_oracle"]
                our_last_oracle_t = self.last_mid_calced_oracles[one_real_sym]["timestamp"]

                if last_oracle_px == 0: 
                    alert_lines.append(
                        "{} {} Got HL oracle of 0, ours {}".format(
                            time.time(), one_real_sym, 
                            our_last_oracle
                        )
                    )
                elif abs(last_oracle_t - our_last_oracle_t) < 3:
                    # Only check if they are comparable.
                    # Check.
                    if (
                        abs(last_oracle_px - our_last_oracle)
                    ) / last_oracle_px > self.theirs_vs_ours_oracle_thresh:
                        alert_lines.append(
                            "{} {} Got some mismatched oracles: ours {} theirs {}".format(
                                time.time(), one_real_sym, 
                                our_last_oracle, last_oracle_px
                            )
                        )

                # Go through oracles and look for hops.
                last_px = 1
                for idx, one_oracle_px in enumerate(self.last_oracle_pxs[one_real_sym]):
                    if idx == 0:
                        last_px = one_oracle_px["oraclePx"]
                        continue
                    # Otherwise, check.
                    cur_px = one_oracle_px["oraclePx"]
                    delta_pct = abs(cur_px - last_px) / last_px
                    if delta_pct > self.oracle_jump_thresh:

                        alert_lines.append("{} {}: Experienced a jumping oracle value, {}".format(
                            time.time(), one_real_sym, 
                            delta_pct))
                    last_px = cur_px

            print("Oracle check done")

            # Maybe send email.
            alert_text = '\n'.join(alert_lines)
            if len(alert_lines) > 0:
                print("ALERT FROM ORACLE:", alert_text)
                if self.emails:
                    email_utils.send_mail(
                        receiver=self.emails,
                        subject="ORACLE ISSUE",
                        body=alert_text,
                    )


            await asyncio.sleep(self.check_t_secs)
        # Check 1, do we have recent data? If so,
        # does our calculation seem to agree with it?

        # Check 2, is the price stale? Can we check the last few updates seem
        # to be correct?

    async def sub_feed(self):
        """Subscribing to pymultifeed."""
        message_count = 0
        while True:
            try:
                # Poll with timeout
                if await self.sub_socket.poll(timeout=100):  # 100ms timeout
                    raw_message = await self.sub_socket.recv()
                    self.process_message(raw_message)
                    message_count += 1
            except asyncio.CancelledError:
                print("Feed cancelled")
                break
            except Exception as e:
                print(f"Error in main loop: {e}")
                err_line = "Oracle_check received error in sub_feed loop: {}".format(traceback.format_exc())

                email_utils.send_mail(
                    receiver=self.emails,
                    subject="Oracle Alerter Broke",
                    body=err_line,
                )

                # Send this back. 
                traceback.print_exc()
                await asyncio.sleep(1)


async def main():
    parser = argparse.ArgumentParser()
    # File that says what to subscribe to
    parser.add_argument("--date", default="TOMORROW")
    parser.add_argument("--emails", default="pktrade@googlegroups.com")
    args = parser.parse_args()

    checker = OracleChecker(args.emails)
    await asyncio.gather(
        checker.sub_feed(),
        checker.sub_hl(),
        checker.check_oracle(),
    )
    # Run the main loop
    # await checker.run()


if __name__ == "__main__":
    uvloop.run(main())
    # I did a bunch of bs to make polygon work here.
