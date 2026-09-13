#! /usr/bin/env python

"""
Trade Checker for Hyperliquid

Subscribes to Hyperliquid's trade feed for a given symbol and records:
- Both counterparties (maker and taker) for each trade
- Classification of trades as TWAP vs market trades

TWAP trades are identified by:
- Consistent small sizes
- Regular time intervals
- Same address pattern

Market trades are identified by:
- Large irregular sizes
- Irregular timing
- Mixed addresses
"""

import argparse
import asyncio
import getpass
import json
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime

import eth_account
import requests
import websockets
import uvloop

mkt_to_endpoint = {
    "Hyperliquid": "https://api.hyperliquid.xyz/",
}


def dexname():
    return "xyz"


# Trade pattern analyzer for TWAP detection
class TradeAnalyzer:
    def __init__(self):
        """
        Args:
            time_window: Time window in seconds to analyze trades (default 5 min)
            size_variance_threshold: Max coefficient of variation for TWAP (default 15%)
        """
        # Store recent trades per address for analysis
        self.address_trades = defaultdict(lambda: deque(maxlen=100))
        self.endpoint_base = mkt_to_endpoint["Hyperliquid"]

        self.ws_url = "wss://api.hyperliquid.xyz/ws"


        # Way to track both trade and wallet behavior 
        # key: address
        # value: num_trds, net_trade_volume, gross_trade_volume, 
        # is_twapping, gross_twap_amt, num_make, num_take
        self.per_user_stats = {}

        # Address to twap stats. 
        self.ongoing_twaps = {}



    def reset_address(self, address):
        self.per_user_stats[address] = {
            "num_trds": 0,
            "net_trade_volume": 0,
            "gross_trade_volume": 0,
            "gross_trade_notional": 0,
            "is_twapping": False, 
            "gross_twap_amt": 0,
            "num_make": 0,
            "num_take": 0
        }


    # Checks if it's 
    def register_trade(self, address, net_shs, px, is_twap, is_take):
        if address not in self.per_user_stats:
            self.reset_address(address)

        self.per_user_stats[address]["num_trds"] += 1
        self.per_user_stats[address]["net_trade_volume"] += net_shs
        self.per_user_stats[address]["gross_trade_volume"] += abs(net_shs)
        self.per_user_stats[address]["gross_trade_notional"] += abs(net_shs) * px

        self.per_user_stats[address]["is_twapping"] = self.per_user_stats[address]["is_twapping"] or is_twap
        if is_twap:
            self.per_user_stats[address]["gross_twap_amt"] += abs(net_shs)
        if is_take:
            self.per_user_stats[address]["num_take"] += 1
        else:
            self.per_user_stats[address]["num_make"] += 1


    async def rank_accts(self):
        # Periodically go through the stored addresses and try to track addresses
        # with a large amount of volume, twapping, or
        # large # of take orders.

        while True:
            await asyncio.sleep(300)  # Run every 10 minutes

            if not self.per_user_stats:
                print("\nNo account data yet...")
                continue

            print(f"\n{'='*100}")
            print(f"ACCOUNT RANKINGS - {datetime.now().isoformat()}")
            print(f"Total accounts tracked: {len(self.per_user_stats)}")
            print(f"{'='*100}\n")

            # Get top 10 by gross trade volume
            gross_vol_sorted = sorted(
                self.per_user_stats.items(),
                key=lambda x: x[1]["gross_trade_volume"],
                reverse=True
            )[:10]

            # Helper function to print consistent account data
            def print_account_stats(i, addr, stats):
                total_trades = stats['num_take'] + stats['num_make']
                take_pct = (stats['num_take'] / total_trades * 100) if total_trades > 0 else 0
                twap_pct = (stats['gross_twap_amt'] / stats['gross_trade_volume'] * 100) if stats['gross_trade_volume'] > 0 else 0
                direction = "LONG" if stats['net_trade_volume'] > 0 else "SHORT"
                print(f"{i:2d}. {addr}")
                print(f"    Net: {stats['net_trade_volume']:+,.2f} ({direction}) | Gross: {stats['gross_trade_volume']:,.2f} | Notional: ${stats['gross_trade_notional']:,.2f}")
                print(f"    Trades: {stats['num_trds']} | Takes: {stats['num_take']} ({take_pct:.1f}%) | Makes: {stats['num_make']}")
                print(f"    TWAP Vol: {stats['gross_twap_amt']:,.2f} ({twap_pct:.1f}%)")

            print("TOP 10 BY GROSS TRADE VOLUME:")
            for i, (addr, stats) in enumerate(gross_vol_sorted, 1):
                print_account_stats(i, addr, stats)
            print()

            # Get top 10 by net trade volume (absolute value - largest position changes)
            net_vol_sorted = sorted(
                self.per_user_stats.items(),
                key=lambda x: abs(x[1]["net_trade_volume"]),
                reverse=True
            )[:10]

            print("TOP 10 BY NET POSITION CHANGE (ABS):")
            for i, (addr, stats) in enumerate(net_vol_sorted, 1):
                print_account_stats(i, addr, stats)
            print()

            # Get top 10 TWAP users
            twap_sorted = sorted(
                self.per_user_stats.items(),
                key=lambda x: x[1]["gross_twap_amt"],
                reverse=True
            )[:10]

            print("TOP 10 TWAP USERS:")
            for i, (addr, stats) in enumerate(twap_sorted, 1):
                print_account_stats(i, addr, stats)
            print()

            # Get top 10 takers (aggressive traders)
            taker_sorted = sorted(
                self.per_user_stats.items(),
                key=lambda x: x[1]["num_take"],
                reverse=True
            )[:10]

            print("TOP 10 AGGRESSIVE TRADERS (TAKERS):")
            for i, (addr, stats) in enumerate(taker_sorted, 1):
                print_account_stats(i, addr, stats)
            print()

            # Get top 10 makers (passive traders)
            maker_sorted = sorted(
                self.per_user_stats.items(),
                key=lambda x: x[1]["num_make"],
                reverse=True
            )[:10]

            print("TOP 10 PASSIVE TRADERS (MAKERS):")
            for i, (addr, stats) in enumerate(maker_sorted, 1):
                print_account_stats(i, addr, stats)
            print()

            print(f"{'='*100}\n")


    async def run(self):
        # Run both things in the loop.
        await asyncio.gather(
            self.subscribe_trades(),
            self.rank_accts()
        )


    async def subscribe_trades(self, sym="xyz:XYZ100",  output_file=None, debug=False):
        """
        Subscribe to trade feed for a symbol and record all trades with counterparties
        """
        # used for grabbing trades. 

        last_t_ping = time.time()

        # Prepare output file if specified
        output_fh = None
        if output_file:
            output_fh = open(output_file, 'a')
            output_fh.write(f"\n{'='*100}\n")
            output_fh.write(f"Trade monitoring started at {datetime.now().isoformat()}\n")
            output_fh.write(f"Symbol: {sym}\n")
            output_fh.write(f"{'='*100}\n")
            output_fh.flush()

        print(f"Connecting to Hyperliquid trade feed for {sym}")

        async with websockets.connect(self.ws_url, ping_interval=100000000) as ws:
            try:
                # Subscribe to trades
                trade_sub_json = json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": sym, "dex": "xyz"},
                })
                await ws.send(trade_sub_json)

            except Exception as e:
                traceback.print_exc()
                if output_fh:
                    output_fh.close()
                sys.exit(1)

            while True:
                # Send ping to keep connection alive
                if (time.time() - last_t_ping) > 40:
                    await ws.send(json.dumps({"method": "ping"}))
                    last_t_ping = time.time()

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

                                taker = ""
                                maker = ""

                                taker_shs = float(trade["sz"])
                                px = float(trade["px"])

                                # I think this means which side the taker was? 
                                if trade["side"] == "B":
                                    # Taker was a buyer
                                    taker = trade["users"][0]
                                    maker = trade["users"][1]
                                    #print("BUYING")
                                else:
                                    #print("SELLING")
                                    taker = trade["users"][1]
                                    maker = trade["users"][0]
                                    taker_shs *= -1
                                #if is_twap_by_hash:
                                #    print("WAS_TWAP")
                                
                                self.register_trade(taker, taker_shs, px, is_twap_by_hash, True)
                                # The maker is not a part of the twap, by definition. 
                                self.register_trade(maker, -taker_shs, px, False, False)


                                # Great, we actually have a bunch of twaps! Nice!
                                #print(trade)

                                # OK, now we can add some details to our stuff. 


                    elif 'method' in msg_dct and msg_dct['method'] == 'pong':
                        # Pong response, keep going
                        pass
                    elif 'channel' in msg_dct and msg_dct['channel'] == 'subscriptionResponse':
                        #print(f"Subscription response: {msg_dct}")
                        pass
                    else:
                        # Unknown message type
                        print(f"Received message: {msg_dct}")

                except asyncio.exceptions.TimeoutError:
                    # No message received, continue
                    pass
                except Exception as e:
                    print("-----------------------------------")
                    print("Got exception in receive loop!")
                    print(f"Repr (repr(e)): {repr(e)}")
                    print("-----------------------------------")
                    print("Full Traceback:")
                    traceback.print_exc()
                    print("-----------------------------------")
                    if output_fh:
                        output_fh.close()
                    sys.exit(1)



def main():
    parser = argparse.ArgumentParser(
        description="Monitor Hyperliquid trades and identify TWAP vs market trades"
    )
    parser.add_argument(
        "--sym",
        type=str,
        default="BTC",
        help="Symbol to monitor (default: BTC)"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output file to save trade data"
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Hours of historical data to analyze (default: 24)"
    )

    args = parser.parse_args()

    # Remove prefix if present
    sym = args.sym
    if ":" in sym:
        colon_idx = sym.find(":")
        sym = sym[colon_idx + 1:]

    try:
        ta = TradeAnalyzer()
        uvloop.run(ta.run())

    except KeyboardInterrupt:
        print("\nShutting down trade checker...")
        sys.exit(0)


if __name__ == "__main__":
    main()
