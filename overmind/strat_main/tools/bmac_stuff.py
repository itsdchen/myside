#! /usr/bin/env python

"""


I finally installed the hyperliquid sdk.
pip install hyperliquid-python-sdk

To onboard:

1 - Must transfer USD from "main" spot balance to "unit" perp balance.

# To do this in python.


"""
import argparse

import example_utils
import getpass

from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.constants import TESTNET_API_URL

# I'm guessing.. ok let's just read this. I think I might just need to append
# "dex" to each request.
from eth_abi import encode
import eth_account
from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex
import json
import requests

mkt_to_endpoint = {
    "Hyperliquid": "https://api.hyperliquid.xyz/",
    # Testnet
    "HyperliquidTest": "http://api.hyperliquid-testnet.xyz/",
}

# Various creds.

# Same thing for hyperliquid_test.
# OK, so I guess I just gotta change everything to use the unit dex. That's fine.
creds_path =  "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())
with open(creds_path) as f:
    hyper_creds = json.load(f)
eth_wallet = eth_account.Account.from_key(hyper_creds["secret_key"])
my_address = eth_wallet.address


# OK, I guess I gotta make example_utils too.

def check_unit_universe():
    unit_dex = "unit"
    info = Info(TESTNET_API_URL, skip_ws=True, perp_dexs=[unit_dex])
    # Cool. I can get the unit universe now.


    unit_meta = info.meta(unit_dex)
    print(unit_meta)


# OK. I was able to transfer to the unit perp.
def transfer_usd_to_unit():
    DUMMY_DEX = "unit"
    COLLATERAL_TOKEN = "USDC"  # nosec

    address, info, exchange = example_utils.setup(constants.TESTNET_API_URL, skip_ws=True)

    # if needed, transfer perp (main) to spot (main)
    #main_perp_to_spot = exchange.usd_class_transfer(100, False)
    #print(main_perp_to_spot)

    # Transfer from spot balance to unit perp
    spot_to_dex_perp = exchange.perp_dex_class_transfer(DUMMY_DEX, COLLATERAL_TOKEN, 200000, True)
    print(spot_to_dex_perp)


# Now. Can I do the basic jane things?
# How do I make the same requests, eg. balance, etc?

# Shared for quickness.

def check_perp_balance(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "clearinghouseState",
        "user": my_address,
        "dex": "unit"
    }

    user_data = session.post(
        endpt,
        json=params,
    )
    print(user_data.text)
    user_data_dct = json.loads(user_data.text)
    positions = user_data_dct["assetPositions"]
    accountValue = user_data_dct["crossMarginSummary"]["accountValue"]

    print("Account value: {}".format(accountValue))

    pass

def check_perp_limits(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "userRateLimit",
        "user": my_address,
    }
    user_data = session.post(
        endpt,
        json=params,
    )
    user_data_dct = json.loads(user_data.text)

    print("Limits value: {} , remaining {}".format(user_data_dct, user_data_dct["nRequestsCap"] - user_data_dct["nRequestsUsed"]))




def get_all_perp_dexes(is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "perpDexs",
    }

    response = json.loads(requests.post(endpt, json=payload).text)
    #print(json.dumps(response, indent=2))
    return response


def get_perp_idx(is_testnet=True, perp_dex_name="unit"):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "perpDexs",
    }

    response = json.loads(requests.post(endpt, json=payload).text)


    perp_dexes = get_all_perp_dexes(is_testnet)
    for idx, dex in enumerate(perp_dexes):
        if dex is None:
            continue
        if dex["name"] == perp_dex_name:
            return idx
    return None

# OK, so the unit one is probably(?) going to be 2.
# Not sure yet what the index will be in real life, but let's not just hardcode it
# in for the real thing.
#get_all_perp_dexes(is_testnet=True)

def get_all_syms(is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "meta",
        "dex": "unit"
    }

    response = json.loads(requests.post(endpt, json=payload).text)
    print(json.dumps(response, indent=2))
    return response


def get_sym_idx(is_testnet=True, sym="unit:ES"):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "meta",
        "dex": "unit"
    }

    response_json = json.loads(requests.post(endpt, json=payload).text)

    for idx, one_sym_dict in enumerate(response_json["universe"]):
        if one_sym_dict["name"] == sym:
            return idx
    return None





#get_all_syms(is_testnet=True)



def get_all_mids(is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    payload = {
        "type": "allMids",
        "dex": "unit"
    }

    response = requests.post(endpt, json=payload)
    print(response.text)
    return response.json()


def get_l2_snapshot(sym="unit:ES", is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    payload = {
        "type": "l2Book",
        "coin": sym,
        "dex": "unit"
    }

    response = requests.post(endpt, json=payload)
    print(response)
    print(response.text)


#get_l2_snapshot(is_testnet=True)

def get_my_orders(is_testnet=True, should_print=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "openOrders",
        "user": my_address,
        "dex": "unit"
    }

    response = requests.post(endpt, json=payload)
    response_json = json.loads(response.text)
    if should_print:
        print(json.dumps(response_json, indent=2))
    return response_json


# OK, now I guess I can start sending orders.
# OK, so what is the index for unit? I guess it's just
# 2 ?
# 'a': 120000,
"""
Builder-deployed perps expect 100000 + perp_dex_index * 10000 + index_in_meta .
For example, test:ABC on testnet has perp_dex_index = 1 ,index_in_meta = 0 ,
asset = 110000 . Note that builder-deployed perps always have name in
the format {dex}:{coin} .



"""

# 2 things.
# first, we gotta get the dex.
# Second, we gotta get the contract number.
def get_asset_num(sym="unit:ES", is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    unit_perp_idx = get_perp_idx(is_testnet, "unit")

    sym_idx = get_sym_idx(is_testnet, sym)

    if unit_perp_idx is None or sym_idx is None:
        print("Error: unit_perp_idx or sym_idx is None")
        return None

    # OK, cool.
    full_asset_num = 100000 + unit_perp_idx * 10000 + sym_idx

    print("Asset num for {} : {}".format(sym, full_asset_num))
    return full_asset_num

def get_all_asset_nums(is_testnet=True):

    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    unit_perp_idx = get_perp_idx(is_testnet, "unit")

    if unit_perp_idx is None:
        print("unit_perp_idx or sym_idx is None")
        return {}


    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "meta",
        "dex": "unit"
    }

    response_json = json.loads(requests.post(endpt, json=payload).text)

    names_to_idxs = {}
    for idx, one_sym_dict in enumerate(response_json["universe"]):
        full_asset_num = 100000 + unit_perp_idx * 10000 + idx
        sym_name = one_sym_dict["name"]
        names_to_idxs[sym_name] = full_asset_num

        #if one_sym_dict["name"] == sym:
        #    return idx

    #print(json.dumps(names_to_idxs, indent=2))
    return names_to_idxs



# I'm going to do this first, and then cancel it.
def send_order(price=5000,size=1, sym="unit:ES", side="BUY", is_testnet=True):
    DUMMY_DEX = "unit"
    COIN = sym

    side_bool = True
    if side == "SELL":
        side_bool = False

    address, info, exchange = example_utils.setup(
        base_url=constants.TESTNET_API_URL, skip_ws=True, perp_dexs=[DUMMY_DEX]
    )

    # OK, so to be clear it seems like the only thing this gets us is the
    # index of the thing.
    # So let's just grab the indices and start getting them.

    # Get the user state and print out position information
    user_state = info.user_state(address)
    positions = []
    for position in user_state["assetPositions"]:
        positions.append(position["position"])
    if len(positions) > 0:
        print("positions:")
        for position in positions:
            print(json.dumps(position, indent=2))
    else:
        print("no open positions")

    # Print the meta for DUMMY_DEX
    #print("dummy dex meta:", info.meta(dex=DUMMY_DEX))

    # Place an order that should rest by setting the price very low

    print("Placing Order")
    order_result = exchange.order(COIN, side_bool, size, price, {"limit": {"tif": "Alo"}})
    print("order result: {}".format(order_result))

    # Query the order status by oid
    if order_result["status"] == "ok":
        status = order_result["response"]["data"]["statuses"][0]
        if "resting" in status:
            order_status = info.query_order_by_oid(address, status["resting"]["oid"])
            print("Order status by oid:", order_status)

#send_order(price=5000, size=1, sym="unit:ES", is_testnet=True)

# 30760137365
def cancel_order(COIN, oid, is_tesetnet=True):
    DUMMY_DEX = "unit"

    tgt_url = constants.TESTNET_API_URL if is_tesetnet else constants.API_URL

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=[DUMMY_DEX]
    )

    cancel_result = exchange.cancel(COIN, oid)
    print(cancel_result)
#cancel_order("unit:ES", 30760137365)

def cancel_all_orders(COIN, oid, is_testnet=True):
    DUMMY_DEX = "unit"
    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.API_URL

    # Check my orders.
    my_ords_dct = get_my_orders(is_testnet, should_print=False)

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=[DUMMY_DEX]
    )

    # Look for the oids first.
    for one_ord in my_ords_dct:
        if one_ord["coin"] != COIN:
            continue

        # Cancel this.
        oid = one_ord["oid"]
        cancel_result = exchange.cancel(COIN, oid)
        print("Cancelling {} - {} - {}".format(COIN, oid, cancel_result))


def get_mids(sym="unit:ES", is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]



import websockets
from tenacity import wait_fixed, retry
import time

import asyncio
import sys
import traceback
# Do the feed subscription process.
async def subscribe_l2(sym="unit:ES", is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    ws_url = "wss://api.hyperliquid-testnet.xyz/ws"
    endpt = endpoint_base + "info"
    payload = {
        "type": "l2Book",
        "coin": sym,
        "dex": "unit"
    }

    syms = [sym]
    last_t_ping = time.time()

    async with websockets.connect(
        ws_url, ping_interval=100000000
    ) as ws:

        try:
            # Subscribe to own fills.
            for one_sym in syms:
                sub_json = json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": one_sym, "dex": "unit"},
#                        "subscription": {"type": "l2Book", "coin": "BTC"},
                     }
                )
                #print(sub_json)
                await ws.send(sub_json)

                # Subscribe to trades too, I guess.
                trade_sub_json = json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": one_sym, "dex": "unit"},
                    }
                )
                await ws.send(trade_sub_json)
        except Exception as e:
            print("-----------------------------------")
            print("Got exception!")
            print(f"Type: {type(e)}")         # Prints the class of the exception
            print(f"Args: {e.args}")          # Prints the arguments passed to the exception constructor
            print(f"Message (str(e)): '{e}'") # This is what you were doing
            print(f"Repr (repr(e)): {repr(e)}") # Often more detailed for debugging
            print("-----------------------------------")
            print("Full Traceback:")
            traceback.print_exc() # This is VERY useful - prints the full stack trace
            print("-----------------------------------")
            sys.exit()

        while True:
            if (time.time() - last_t_ping) > 40:
                # Do my own ping. I guess this is the "right" way that they
                # do ping/pongs.
                await ws.send(json.dumps({"method": "ping"}))
                last_t_ping = time.time()

            try:
                # message = await websocket.recv()
                message = await asyncio.wait_for(ws.recv(), 0.01)
                msg_dct = json.loads(message)
                print(msg_dct)
            except asyncio.exceptions.TimeoutError: # <-- CATCH THE SPECIFIC TIMEOUT ERROR
                #print("Caught asyncio.TimeoutError: The operation took too long. Continuing execution...")
                pass
            except Exception as e:
                print("-----------------------------------")
                print("Got exception!")
                print(f"Type: {type(e)}")         # Prints the class of the exception
                print(f"Args: {e.args}")          # Prints the arguments passed to the exception constructor
                print(f"Message (str(e)): '{e}'") # This is what you were doing
                print(f"Repr (repr(e)): {repr(e)}") # Often more detailed for debugging
                print("-----------------------------------")
                print("Full Traceback:")
                traceback.print_exc() # This is VERY useful - prints the full stack trace
                print("-----------------------------------")
                print("Got exception: {}".format(e))
                sys.exit()




def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--how", default=False, action="store_true")

    parser.add_argument("--mode", type=str, required=True)
    parser.add_argument("--testnet", default=False, action="store_true")
    parser.add_argument("--sym", type=str, default="unit:ES")

    parser.add_argument("--price", type=int, default=5000)
    parser.add_argument("--size", type=float, default=1)
    # BUY OR SELL
    parser.add_argument("--side", type=str, default="BUY")

    parser.add_argument("--oid", type=int)

    args = parser.parse_args()

    if args.how:
        print("How: ")
        print("./hl_utils.py --testnet --mode balance")
        print("./hl_utils.py --testnet --mode limits")
        print("./hl_utils.py --testnet --mode cancel_all")
        print("./hl_utils.py --testnet --mode orders")

        sys.exit()


    is_testnet = args.testnet
    # For now let's set it ot true.
    is_testnet = True

    # Let's make sure everything works now.
    if args.mode == "balance":
        check_perp_balance(is_testnet)
    elif args.mode == "limits":
        check_perp_limits(is_testnet)

    elif args.mode == "transfer":
        transfer_usd_to_unit()
    elif args.mode == "dexes":
        get_all_perp_dexes(is_testnet)
    elif args.mode == "syms":
        get_all_syms(is_testnet)
    elif args.mode == "mids":
        get_all_mids(is_testnet)
    elif args.mode == "l2":
        get_l2_snapshot(args.sym, is_testnet)
    elif args.mode == "orders":
        get_my_orders(is_testnet)
    elif args.mode == "asset_num":
        # Not implemented yet, but this isn't that important rn.
        # Because we just have the one.
        get_asset_num(args.sym, is_testnet)
    elif args.mode == "all_asset_nums":
        # Not implemented yet, but this isn't that important rn.
        # Because we just have the one.
        get_all_asset_nums(is_testnet)

    elif args.mode == "send":
        send_order(args.price, args.size, args.sym, args.side, is_testnet)
    elif args.mode == "cancel":
        cancel_order(args.sym, args.oid, is_testnet)
    elif args.mode == "cancel_all":
        cancel_all_orders(args.sym, args.oid, is_testnet)
    elif args.mode == "subscribe_l2":
        asyncio.run(subscribe_l2(args.sym, is_testnet))



if __name__ == "__main__":
    main()
