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
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

mkt_to_endpoint = {
    "Hyperliquid": "https://api.hyperliquid.xyz/",
    # Testnet
    "HyperliquidTest": "http://api.hyperliquid-testnet.xyz/",
}

# Various creds.

# Same thing for hyperliquid_test.
# OK, so I guess I just gotta change everything to use the z dex. That's fine.
CREDS_PATH = "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())
eth_wallet = None
my_address = None

def _load_creds(path):
    global eth_wallet, my_address, CREDS_PATH
    path = os.path.expanduser(path)
    with open(path) as f:
        hyper_creds = json.load(f)
    eth_wallet = eth_account.Account.from_key(hyper_creds["secret_key"])
    my_address = eth_wallet.address
    CREDS_PATH = path

_load_creds(CREDS_PATH)


# OK, I guess I gotta make example_utils too.

def dexname(is_testnet):
    if is_testnet:
        return "xyz"
    return "xyz"

def check_unit_universe(is_testnet):
    unit_dex = dexname(is_testnet)
    info = Info(TESTNET_API_URL, skip_ws=True, perp_dexs=[unit_dex])

    unit_meta = info.meta(unit_dex)
    print(unit_meta)


# OK. I was able to transfer to the unit perp.
def transfer_usd_to_unit(is_testnet, amt):
    #DUMMY_DEX = "z"
    DUMMY_DEX = dexname(is_testnet)
    COLLATERAL_TOKEN = "USDC"  # nosec
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    # Remove the trailing slash
    endpoint_base = endpoint_base[:-1]

    address, info, exchange = example_utils.setup(endpoint_base, skip_ws=True, creds_path=CREDS_PATH)

    # Transfer from spot balance to unit perp
    # They changed this with the versions.
    spot_to_dex_perp = exchange.send_asset(address,  "spot", DUMMY_DEX, COLLATERAL_TOKEN, amt)
    print(spot_to_dex_perp)


# Now. Can I do the basic jane things?
# How do I make the same requests, eg. balance, etc?

# Shared for quickness.

def check_perp_balance(is_testnet):
    unit_dex = dexname(is_testnet)

    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "clearinghouseState",
        "user": my_address,
        "dex": unit_dex
    }

    user_data = session.post(
        endpt,
        json=params,
    )
    user_data_dct = json.loads(user_data.text)
    print(json.dumps(user_data_dct, indent=2))
    positions = user_data_dct["assetPositions"]
    accountValue = user_data_dct["crossMarginSummary"]["accountValue"]
    print("Account value: {}".format(accountValue))

    pass


# Response looks something like:
# {
#  "marginSummary": {
#    "accountValue": "260002.055391",
#    "totalNtlPos": "2463.0",
#    "totalRawUsd": "262465.055391",
#    "totalMarginUsed": "154.195591"
#  },
#  "crossMarginSummary": {
#    "accountValue": "259847.8598",
#    "totalNtlPos": "0.0",
#    "totalRawUsd": "259847.8598",
#    "totalMarginUsed": "0.0"
#  },
#  "crossMaintenanceMarginUsed": "0.0",
#  "withdrawable": "154466.9648",
#  "assetPositions": [
#    {
#      "type": "oneWay",
#      "position": {
#        "coin": "z:XYZ100",
#        "szi": "-0.1",
#        "leverage": {
#          "type": "isolated",
#          "value": 20,
#          "rawUsd": "2617.195591"
#        },
#        "entryPx": "24647.0",
#        "positionValue": "2463.0",
#        "unrealizedPnl": "1.7",
#        "returnOnEquity": "0.0137947823",
#        "liquidationPx": "25533.6155219512",
#        "marginUsed": "154.195591",
#        "maxLeverage": 20,
#        "cumFunding": {
#          "allTime": "-0.87819",
#          "sinceOpen": "0.0",
#          "sinceChange": "0.0"
#        }
#      }
#    }
#  ],
#  "time": 1759160493222
#}

def check_perp_margin(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    unit_dex = dexname(is_testnet)

    endpt = endpoint_base + "info"

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "clearinghouseState",
        "user": my_address,
        "dex": unit_dex
    }

    user_data = session.post(
        endpt,
        json=params,
    )

    user_data_dct = json.loads(user_data.text)

    # OK, from my interpretation, maintenance margin is how much you need in account equity.
    # So like, let's just go through and calculate the maintenance margin and
    # how much we might need. Also how far we are from the actual liquidation point.

    for one_coin_position in user_data_dct["assetPositions"]:
        one_position = one_coin_position["position"]
        # Only act if it's isolated.
        if one_position["leverage"]["type"] != "isolated":
            continue
        # Otherwise, let's calculate.
        equity = float(one_position["marginUsed"]) + float(one_position["unrealizedPnl"])
        maintenance_margin = float(one_position["entryPx"]) * abs(float(one_position["szi"])) / one_position["leverage"]["value"] / 2

        margin_distance = (equity - maintenance_margin) / maintenance_margin
        should_top_up = margin_distance < 0.5
        print("{}: size {:.2f} upnl {:.2f} equity {:.2f} margin_distance {:.2f} maint_margin {:.2f} {}".format(
                one_position["coin"],
                float(one_position["positionValue"]),
                float(one_position["unrealizedPnl"]),
                equity,
                margin_distance,
                maintenance_margin,
               "TOP_UP" if should_top_up else ""
        ))

    #print(json.dumps(user_data_dct, indent=2))


    pass

def check_margin_avail(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})

    params = {
        "type": "clearinghouseState",
        "user": my_address
    }

    user_data = session.post(
        endpt,
        json=params,
    )

    user_data_dct = json.loads(user_data.text)

    marginTotal = float(user_data_dct["marginSummary"]["accountValue"])
    crossMarginTotal = float(user_data_dct["crossMarginSummary"]["accountValue"])
    withdrawable = float(user_data_dct["withdrawable"])
    print(f"marginSummary.accountValue: {marginTotal}, "
          f"crossMarginSummary.accountValue: {crossMarginTotal}, "
          f"withdrawable: {withdrawable}")

# This is just for checking my L1 limits, doesn't matter if it's hip3 or not.
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


def get_perp_oicaps(is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "perpDexLimits",
        "dex": "xyz"
    }

    response = json.loads(requests.post(endpt, json=payload).text)
    print(json.dumps(response, indent=2))
    return response



def get_all_perp_dexes(is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "perpDexs",
    }

    response = json.loads(requests.post(endpt, json=payload).text)
    print(json.dumps(response, indent=2))
    return response


def get_unit_perp_idx(is_testnet=True):

    unit_dex = dexname(is_testnet)

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
        if dex["name"] == unit_dex:
            return idx
    return None

# OK, so the unit one is probably(?) going to be 2.
# Not sure yet what the index will be in real life, but let's not just hardcode it
# in for the real thing.
#get_all_perp_dexes(is_testnet=True)

def get_all_syms(is_testnet=True):
    unit_dex = dexname(is_testnet)

    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "meta",
        "dex": unit_dex
    }

    response = json.loads(requests.post(endpt, json=payload).text)
    print(json.dumps(response, indent=2))
    return response


def get_asset_ctxs(is_testnet=True):
    unit_dex = dexname(is_testnet)

    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "metaAndAssetCtxs",
        "dex": unit_dex
    }

    response = json.loads(requests.post(endpt, json=payload).text)
    print(json.dumps(response, indent=2))
    return response


def get_sym_idx(is_testnet=True, sym="XYZ100"):
    unit_dex = dexname(is_testnet)

    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "meta",
        "dex": unit_dex
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
    unit_dex = dexname(is_testnet)

    payload = {
        "type": "allMids",
        "dex": unit_dex
    }

    response = requests.post(endpt, json=payload)
    print(response.text)
    return response.json()


def get_l2_snapshot(sym="XYZ100", is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    unit_dex = dexname(is_testnet)

    payload = {
        "type": "l2Book",
        "coin": sym,
        "dex": unit_dex
    }

    response = requests.post(endpt, json=payload)
    #print(response)

    response_dct = json.loads(response.text)
    print(json.dumps(response_dct, indent=2))
    #print(response.text)


#get_l2_snapshot(is_testnet=True)

def get_my_orders(is_testnet=True, should_print=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    unit_dex = dexname(is_testnet)

    endpt = endpoint_base + "info"
    payload = {
        "type": "openOrders",
        "user": my_address,
        "dex": unit_dex
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
def get_asset_num(sym="XYZ100", is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    unit_perp_idx = get_unit_perp_idx(is_testnet)

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
    unit_dex = dexname(is_testnet)
    unit_perp_idx = get_unit_perp_idx(is_testnet)

    if unit_perp_idx is None:
        print("unit_perp_idx or sym_idx is None")
        return {}


    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "meta",
        "dex": unit_dex
    }

    response_json = json.loads(requests.post(endpt, json=payload).text)

    names_to_idxs = {}
    for idx, one_sym_dict in enumerate(response_json["universe"]):
        full_asset_num = 100000 + unit_perp_idx * 10000 + idx
        sym_name = one_sym_dict["name"]
        names_to_idxs[sym_name] = full_asset_num

        #if one_sym_dict["name"] == sym:
        #    return idx

    print(json.dumps(names_to_idxs, indent=2))
    return names_to_idxs

def get_growth_mode_status(is_testnet=True):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]
    endpt = endpoint_base + "info"
    payload = {
        "type": "allPerpMetas"
    }
    response = json.loads(requests.post(endpt, json=payload).text)

    growth_mode_syms = []
    non_growth_mode_syms = []
    for dex in response:
        universe = dex["universe"]
        if universe[0]["name"].startswith("xyz"):
            # found the xyz dex
            for asset in universe:
                sym = asset["name"]
                if asset.get("growthMode") == "enabled":
                    growth_mode_syms.append(sym)
                else:
                    non_growth_mode_syms.append(sym)
            break

    print(f"Growth-mode syms ({len(growth_mode_syms)}): {growth_mode_syms}")
    print(f"Non-growth-mode syms ({len(non_growth_mode_syms)}): {non_growth_mode_syms}")
    return growth_mode_syms

# I'm going to do this first, and then cancel it.
def send_order(price, size, sym, side, is_testnet=True):
    unit_dex = dexname(is_testnet)
    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL
    COIN = sym

    if side == "BUY":
        side_bool = True
    elif side == "SELL":
        side_bool = False
    else:
        raise ValueError(f"Invalid side {side}")

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=["", unit_dex], creds_path=CREDS_PATH
    )

    # OK, so to be clear it seems like the only thing this gets us is the
    # index of the thing.
    # So let's just grab the indices and start getting them.

    # Get the user state and print out position information
    # order_dex = sym.split(":")[0] if ":" in sym else ""
    # user_state = info.user_state(address, dex=order_dex)
    # positions = []
    # for position in user_state["assetPositions"]:
    #     positions.append(position["position"])
    # if len(positions) > 0:
    #     print("positions:")
    #     for position in positions:
    #         print(json.dumps(position, indent=2))
    # else:
    #     print("no open positions")

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
def cancel_order(COIN, oid, is_testnet=True):
    unit_dex = dexname(is_testnet)

    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=["", unit_dex], creds_path=CREDS_PATH
    )

    cancel_result = exchange.cancel(COIN, oid)
    print(cancel_result)
#cancel_order("unit:ES", 30760137365)

# Modify an existing resting order in place. The SDK fully replaces the order,
# so every field is up for grabs: side, size, price, tif, and reduce_only. The
# coin must match the original order's symbol. `tif` is passed through verbatim
# (e.g. Alo / Gtc / Ioc) so anything the API accepts works.
def modify_order(oid, price, size, sym, side, tif="Alo", reduce_only=False, is_testnet=True):
    unit_dex = dexname(is_testnet)
    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL
    COIN = sym

    if side == "BUY":
        side_bool = True
    elif side == "SELL":
        side_bool = False
    else:
        raise ValueError(f"Invalid side {side}")

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=["", unit_dex], creds_path=CREDS_PATH
    )

    order_type = {"limit": {"tif": tif}}

    print("Modifying order {}".format(oid))
    modify_result = exchange.modify_order(
        oid, COIN, side_bool, size, price, order_type, reduce_only=reduce_only
    )
    print("modify result: {}".format(modify_result))

    # Query the (possibly new) order status by oid.
    if modify_result["status"] == "ok":
        status = modify_result["response"]["data"]["statuses"][0]
        if "resting" in status:
            order_status = info.query_order_by_oid(address, status["resting"]["oid"])
            print("Order status by oid:", order_status)
#modify_order(30760137365, price=5000, size=1, sym="xyz:XYZ100", side="BUY", tif="Gtc", is_testnet=True)

def cancel_all_orders(COIN, oid, is_testnet=True):
    unit_dex = dexname(is_testnet)

    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL

    # Check my orders.
    my_ords_dct = get_my_orders(is_testnet, should_print=False)

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=["", unit_dex], creds_path=CREDS_PATH
    )

    # Look for the oids first.
    for one_ord in my_ords_dct:
        if one_ord["coin"] != COIN:
            continue

        # Cancel this.
        oid = one_ord["oid"]
        cancel_result = exchange.cancel(COIN, oid)
        print("Cancelling {} - {} - {}".format(COIN, oid, cancel_result))


def cancel_every_order(is_testnet=True):
    unit_dex = dexname(is_testnet)
    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL

    my_ords_dct = get_my_orders(is_testnet, should_print=False)
    if not my_ords_dct:
        print("No open orders.")
        return

    cancel_requests = [
        {"coin": one_ord["coin"], "oid": int(one_ord["oid"])}
        for one_ord in my_ords_dct
    ]

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=["", unit_dex], creds_path=CREDS_PATH
    )

    print("Cancelling {} orders across {} symbols.".format(
        len(cancel_requests),
        len({r["coin"] for r in cancel_requests}),
    ))
    cancel_result = exchange.bulk_cancel(cancel_requests)
    print(cancel_result)


def schedule_cancel_at_et(et_hhmm="18:00", is_testnet=True):
    # HL evaluates the timestamp on its own server clock, so our local clock
    # only affects what wall-clock day we're targeting -- not when the cancel
    # actually fires.
    hh, mm = (int(x) for x in et_hhmm.split(":"))

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    target_et = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    ts_ms = int(target_et.timestamp() * 1000)

    print("Scheduling cancel at {} ({} UTC ms). Local now (ET): {}".format(
        target_et.isoformat(), ts_ms, now_et.isoformat(),
    ))

    unit_dex = dexname(is_testnet)
    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL
    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=["", unit_dex], creds_path=CREDS_PATH
    )
    result = exchange.schedule_cancel(ts_ms)
    print(result)


def get_mids(sym="XYZ100", is_testnet=True):
    unit_dex = dexname(is_testnet)

    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]



import websockets
from tenacity import wait_fixed, retry
import time

import asyncio
import sys
import traceback
# Do the feed subscription process.
async def subscribe_l2(sym="XYZ100", is_testnet=True):
    unit_dex = dexname(is_testnet)

    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    ws_url = "wss://api.hyperliquid-testnet.xyz/ws"
    endpt = endpoint_base + "info"
    payload = {
        "type": "l2Book",
        "coin": sym,
        "dex": unit_dex
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
                        "subscription": {"type": "l2Book", "coin": one_sym, "dex": unit_dex},
#                        "subscription": {"type": "l2Book", "coin": "BTC"},
                     }
                )
                #print(sub_json)
                await ws.send(sub_json)

                # Subscribe to trades too, I guess.
                trade_sub_json = json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": one_sym, "dex": unit_dex},
                    }
                )
                await ws.send(trade_sub_json)
        except Exception as e:
            print("-----------------------------------")
            print("Got exception!")
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
                print(f"Repr (repr(e)): {repr(e)}") # Often more detailed for debugging
                print("-----------------------------------")
                print("Full Traceback:")
                traceback.print_exc() # This is VERY useful - prints the full stack trace
                sys.exit()


async def subscribe_me(is_testnet=True):
    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL
    unit_dex = dexname(is_testnet)

    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, perp_dexs=["", unit_dex], creds_path=CREDS_PATH
    )

    ws_url = "wss://api.hyperliquid-testnet.xyz/ws"

    last_t_ping = time.time()

    async with websockets.connect(
        ws_url, ping_interval=100000000
    ) as ws:

        try:
            sub_json = json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "userFills", "user": address},
                }
            )
            await ws.send(sub_json)

            userevent_sub_json = json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "userEvents", "user": address},
                }
            )
            await ws.send(userevent_sub_json)



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
                print("Received message: {}".format(msg_dct))
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


    pass



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--how", default=False, action="store_true")

    parser.add_argument("--mode", type=str, required=True)
    parser.add_argument("--testnet", default=False, action="store_true")
    # Pass the fully-qualified symbol: HIP-3 perps as {dex}:{coin} (e.g.
    # xyz:XYZ100), core L1 perps as the bare coin (e.g. BTC).
    parser.add_argument("--sym", type=str, default="xyz:XYZ100")

    parser.add_argument("--price", type=float, default=5000)
    parser.add_argument("--size", type=float, default=1)
    # BUY OR SELL
    parser.add_argument("--side", type=str, default="BUY")
    # Time-in-force for modify (Alo / Gtc / Ioc), passed through verbatim.
    parser.add_argument("--tif", type=str, default="Alo")
    parser.add_argument("--reduce_only", default=False, action="store_true")

    parser.add_argument("--oid", type=int)

    parser.add_argument("--amt", type=int)

    parser.add_argument("--et", type=str, default="18:00",
                        help="HH:MM Eastern Time for schedule_cancel mode")

    parser.add_argument("--creds", type=str, default=None,
                        help="Path to Hyperliquid creds JSON (default: ~/.creds/.Hyperliquid.creds.json)")

    args = parser.parse_args()

    if args.how:
        print("How: ")
        sys.exit()

    if args.creds is not None:
        _load_creds(args.creds)


    is_testnet = args.testnet

    # Symbols are passed through verbatim. HL keys both its REST endpoints and
    # the SDK by the fully-qualified name, so HIP-3 perps must be given as
    # {dex}:{coin} (e.g. xyz:XYZ100) and core L1 perps as the bare coin (e.g.
    # BTC). We neither add nor strip a prefix.
    sym = args.sym

    # Let's make sure everything works now.
    if args.mode == "balance":
        check_perp_balance(is_testnet)
    elif args.mode == "margin":
        check_perp_margin(is_testnet)
    elif args.mode == "available":
        check_margin_avail(is_testnet)
    elif args.mode == "limits":
        check_perp_limits(is_testnet)
    elif args.mode == "oicaps":
        get_perp_oicaps(is_testnet)

    elif args.mode == "transfer":
        if not args.amt:
            sys.exit("Need a --amt for --mode transfer")
        transfer_usd_to_unit(is_testnet, args.amt)
    elif args.mode == "dexes":
        get_all_perp_dexes(is_testnet)
    elif args.mode == "syms":
        get_all_syms(is_testnet)
    elif args.mode == "ctxs":
        get_asset_ctxs(is_testnet)
    elif args.mode == "mids":
        get_all_mids(is_testnet)
    elif args.mode == "l2":
        get_l2_snapshot(sym, is_testnet)
    elif args.mode == "orders":
        get_my_orders(is_testnet)
    elif args.mode == "asset_num":
        # Not implemented yet, but this isn't that important rn.
        # Because we just have the one.
        get_asset_num(sym, is_testnet)
    elif args.mode == "all_asset_nums":
        # Not implemented yet, but this isn't that important rn.
        # Because we just have the one.
        get_all_asset_nums(is_testnet)
    elif args.mode == "growth_mode":
        get_growth_mode_status(is_testnet)

    elif args.mode == "send":
        send_order(args.price, args.size, sym, args.side, is_testnet)
    elif args.mode == "cancel":
        cancel_order(sym, args.oid, is_testnet)
    elif args.mode == "modify":
        modify_order(args.oid, args.price, args.size, sym, args.side,
                     args.tif, args.reduce_only, is_testnet)
    elif args.mode == "cancel_all":
        cancel_all_orders(sym, args.oid, is_testnet)
    elif args.mode == "cancel_every":
        cancel_every_order(is_testnet)
    elif args.mode == "schedule_cancel":
        schedule_cancel_at_et(args.et, is_testnet)
    elif args.mode == "subscribe_l2":
        asyncio.run(subscribe_l2(sym, is_testnet))
    elif args.mode == "myfeed":
        asyncio.run(subscribe_me(is_testnet))



if __name__ == "__main__":
    main()
