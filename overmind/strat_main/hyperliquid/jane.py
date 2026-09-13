#! /usr/bin/env python


"""
Test cmds for making hyperliquid calls.

Doing this to replicate/figure out what to do with the spot market.



"""



import argparse
import getpass
import hashlib
import hmac
import requests
import sys
import time
import json
from urllib.parse import urlencode
import os
from requests.exceptions import HTTPError
from urllib.parse import urlencode

from eth_abi import encode
import eth_account
from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex



mkt_to_endpoint = {
    "Hyperliquid": "https://api.hyperliquid.xyz/",
    # Testnet
    "HyperliquidTest": "https://api.hyperliquid-testnet.xyz/",

}

# Various creds.

# Same thing for hyperliquid_test.
creds_path =  "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())

with open(creds_path) as f:
    hyper_creds = json.load(f)

HYPERLIQUID_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# Step 1, let's just try to get the symbol info.



def get_perp_info(is_testnet, dex):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    session = requests.Session()

    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "meta",
    }
    if dex:
        params["dex"] = dex

    universe_data = session.post(
        endpt,
        json=params,
    )
    print(universe_data)

    # Write it out.
    all_data_json =  json.loads(universe_data.text)
    print(json.dumps(all_data_json, indent=2))



def get_spot_info(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    session = requests.Session()

    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "spotMeta",
    }

    universe_data = session.post(
        endpt,
        json=params,
    )


    # Gotta correlate the names to the actual token ids.
    # These effing people.

    json_data =  json.loads(universe_data.text)
    #print(json.dumps(json_data, indent=2))

    # To make it nicer, use the subsequent lines.
    #return

    # The key is gonna be the token's id#.
    # It'll map to other stats, like
    # the pair name
    # the pair index
    # the tokenid (eg. the contract address)

    launched_tokens_idx_to_stats = {}
    token_idx_to_stats = {}
    for one_token_info in json_data["tokens"]:
        token_idx_to_stats[one_token_info["index"]] = {
            "token_name": one_token_info["name"],
            "token_id": one_token_info["tokenId"],
            "evm_contract": one_token_info["evmContract"],
            "sz_decimals": one_token_info["szDecimals"],
        }

        one_token_info

    names_and_szdecimals = []

    # ok, token_dct looks something like;
    # {'name': 'USDC', 'szDecimals': 8, 'weiDecimals': 8, 'index': 0, 'tokenId': '0x6d1e7cde53ba9467b783cb7c530ce054', 'isCanonical': True}

    for i, token_dct in enumerate(json_data["tokens"]):
        if token_dct["name"] == "USDC":
            continue
        # Otherwise.
        names_and_szdecimals.append({
            "name": token_dct["name"],
            "szDecimals": token_dct["szDecimals"],
            "tokenId": token_dct["tokenId"],
        })

    for i, pair_info in enumerate(json_data["universe"]):
        # The pair name is the first token id.
        pair_anchor_idx = pair_info["tokens"][0]
        pair_idx = pair_info["index"]
        pair_name = pair_info["name"]
        if pair_anchor_idx in token_idx_to_stats:
            token_idx_to_stats[pair_anchor_idx]["pair_name"] = pair_name
            token_idx_to_stats[pair_anchor_idx]["pair_id"] = pair_idx
            launched_tokens_idx_to_stats[pair_anchor_idx] = token_idx_to_stats[pair_anchor_idx]
        else:
            print("Cant find {} in {}".format(pair_anchor_idx, token_idx_to_stats.keys()))


    print(json.dumps(launched_tokens_idx_to_stats, indent=2))


# Creates the mappings, like
# HFUN/USDC <> @1
# so we can do mids lookups and ish.
def get_spot_translate():

    # Honestly, this is pretty freaking annoying, but I guess we
    # should just do it properly. OK.
    endpoint_base = mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    session = requests.Session()

    session.headers.update({"Content-Type": "application/json"})
    spot_params = {
        "type": "spotMeta",
    }

    spot_call = session.post(endpt, json=spot_params)

    spot_dct = json.loads(spot_call.text)

    name_to_ref = {}
    ref_to_name = {}

    # We construct a reference->name and a name->reference map


    token_to_idx = {}
    idx_to_token = {}
    for one_token_info in spot_dct["tokens"]:
        # You can grab the tokenId and stuff but all we want is the index.
        token_to_idx[one_token_info["name"]] = one_token_info["index"]
        idx_to_token[one_token_info["index"]] = one_token_info["name"]

    # Now, go through the universe and make the other double map.
    for one_pair_info in spot_dct["universe"]:
        real_token = idx_to_token[one_pair_info["tokens"][0]]
        readable_pair_name = "{}/USDC".format(real_token)

        name_to_ref[readable_pair_name] = one_pair_info["name"]
        ref_to_name[one_pair_info["name"]] = readable_pair_name


    return {
        "name_to_ref": name_to_ref,
        "ref_to_name": ref_to_name
    }




def get_perp_mids(is_perps, dex):
    endpoint_base = mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    session = requests.Session()

    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "allMids",
    }
    if dex:
        params["dex"] = dex

    print(endpt)
    universe_data = session.post(
        endpt,
        json=params,
    )

    # Write it out.
    mids_json_data =  json.loads(universe_data.text)

    print(json.dumps(mids_json_data, indent=2))
    return


def get_spot_mids(is_perps):
    endpoint_base = mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    session = requests.Session()

    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "spotMetaAndAssetCtxs",
    }

    print(endpt)
    universe_data = session.post(
        endpt,
        json=params,
    )

    # Write it out.
    mids_json_data =  json.loads(universe_data.text)

    print(json.dumps(mids_json_data, indent=2))
    return


def get_mids_old(is_perps):
    endpoint_base = mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    session = requests.Session()

    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "allMids",
    }

    print(endpt)
    universe_data = session.post(
        endpt,
        json=params,
    )

    # Write it out.
    mids_json_data =  json.loads(universe_data.text)

    print(json.dumps(mids_json_data, indent=2))
    return

    spot_translations = get_spot_translate()

    final_mids = {}
    for sym_name, midpx in mids_json_data.items():
        if sym_name in spot_translations["ref_to_name"]:
            final_mids[spot_translations["ref_to_name"][sym_name]] = midpx
        else:
            final_mids[sym_name] = midpx

    # OK, let's check it out.

    print(json.dumps(final_mids, indent=2))


    # So, almost good, but this is annoying. They are using their
    # "@" terminology for their


    #print(json.dumps(json_data, indent=2))


def check_perp_balance(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    eth_wallet = eth_account.Account.from_key(hyper_creds["secret_key"])
    my_address = eth_wallet.address

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "clearinghouseState",
        "user": my_address,
    }

    user_data = session.post(
        endpt,
        json=params,
    )
    user_data_dct = json.loads(user_data.text)
    positions = user_data_dct["assetPositions"]
    accountValue = user_data_dct["crossMarginSummary"]["accountValue"]

    print("Account value: {}".format(accountValue))

    pass

def check_spot_balance(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    eth_wallet = eth_account.Account.from_key(hyper_creds["secret_key"])
    my_address = eth_wallet.address

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "spotClearinghouseState",
        "user": my_address,
    }

    user_data = session.post(
        endpt,
        json=params,
    )
    user_data_dct = json.loads(user_data.text)
    positions = user_data_dct["balances"]
    for one_coin in positions:
        print("{}: {}".format(one_coin["coin"], one_coin["total"]))

    pass


def get_positions_futures(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    eth_wallet = eth_account.Account.from_key(hyper_creds["secret_key"])
    my_address = eth_wallet.address

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "clearinghouseState",
        "user": my_address,
    }

    user_data = session.post(
        endpt,
        json=params,
    )

    user_data_dct = json.loads(user_data.text)
    positions = user_data_dct["assetPositions"]


    print("Positions:")
    for one_sym_dct in positions:
        print("{}: {} (breakeven {} )".format(one_sym_dct["position"]["coin"], one_sym_dct["position"]["szi"], one_sym_dct["position"]["entryPx"]))
  #  print(json.dumps(positions, indent=2))

    pass

def get_positions_spot(is_testnet):
    endpoint_base = mkt_to_endpoint["HyperliquidTest"] if is_testnet else mkt_to_endpoint["Hyperliquid"]

    endpt = endpoint_base + "info"
    eth_wallet = eth_account.Account.from_key(hyper_creds["secret_key"])
    my_address = eth_wallet.address

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    params = {
        # Rough, but it's ok
        "type": "spotClearinghouseState",
        "user": my_address,
    }

    user_data = session.post(
        endpt,
        json=params,
    )

    user_data_dct = json.loads(user_data.text)

    if "balances" not in user_data_dct:
        print("No positions.")
        return
    # Otherwise, display.
    positions = user_data_dct["balances"]


    print("Positions:")
    for one_sym_dct in positions:
        print("{}: {} ".format(one_sym_dct["coin"], one_sym_dct["total"]))
    pass




def main():
    # What calls.
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, required=True)
    # By default, point to devnet.
    # If not, point to testnet.
    parser.add_argument("--dex", default="", type=str)
    parser.add_argument("--testnet", default=False, action="store_true")
    parser.add_argument("--perps", default=False,  action="store_true")


    parser.add_argument("--amt", default=0)

    # Instead of having
    parser.add_argument("--spottoperp", default=False, action="store_true")
    parser.add_argument("--peprtospot", default=False, action="store_true")

    # I think newer python can deal with this properly. But still.
    args = parser.parse_args()

    if args.mode == "info":
        if args.perps:
            get_perp_info(args.testnet, args.dex)
        else:
            get_spot_info(args.testnet)
    elif args.mode == "balance":
        if args.perps:
            check_perp_balance(args.testnet)
        else:
            check_spot_balance(args.testnet)
    elif args.mode == "positions":
        if args.perps:
            get_positions_futures(args.testnet)
        else:
            get_positions_spot(args.testnet)

    elif args.mode == "transfer":
        # Check whether we go one direction or the other.
        # Signature needs to be updated to their new thing. How bizarre.
        sys.exit("Not implemented yet")
    elif args.mode == "mids":
        if args.perps:
            get_perp_mids(args.testnet, args.dex)
        else:
            get_spot_mids(args.testnet)






if __name__ == "__main__":
    main()
