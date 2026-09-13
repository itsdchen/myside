#! /usr/bin/env python


"""

Periodically runs on one machine 
Tells us by EOD, what our positions are. 

Run this on cron, once, every day at 18:00 ET. 


"""

import argparse

import example_utils
import getpass

from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

import json
import os
import requests
import sys

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils

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
def check_positions(is_testnet, label, addresses):
    if not addresses:
        creds_path =  "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())
        if label is None:
            creds_path = "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())
        elif label == "OLD":
            creds_path =  "/home/{}/.creds/.Hyperliquid.old.creds.json".format(getpass.getuser())
        elif label == "L1G":
            creds_path =  "/home/{}/.creds/.Hyperliquid.l1g.creds.json".format(getpass.getuser())
        else:
            raise ValueError(f"Unsuppported label {label}")
        with open(creds_path) as f:
            hyper_creds = json.load(f)
        addresses = [hyper_creds.get("subaccount_address", hyper_creds["address"])]

    endpoint_base = TESTNET_API_URL if is_testnet else MAINNET_API_URL
    endpt = endpoint_base + "/info"

    session = requests.Session()
    # Not sure if this is the only thing.
    session.headers.update({"Content-Type": "application/json"})

    all_lines = ""

    for my_address in addresses:
        params = {
            "type": "clearinghouseState",
            "user": my_address
        }
        user_data = session.post(
            endpt,
            json=params,
        )
        user_data_dct = json.loads(user_data.text)

        params = {
            "type": "clearinghouseState",
            "user": my_address,
            "dex": "xyz"
        }
        user_data_xyz = session.post(
            endpt,
            json=params,
        )
        user_data_xyz_dct = json.loads(user_data_xyz.text)

        # OK, from my interpretation, maintenance margin is how much you need in account equity. 
        # So like, let's just go through and calculate the maintenance margin and
        # how much we might need. Also how far we are from the actual liquidation point. 

        for one_coin_position in user_data_dct["assetPositions"] + user_data_xyz_dct["assetPositions"]:
            one_position = one_coin_position["position"]
            # Only act if it's isolated. 

            pos = float(one_position["szi"])
            pos_val = float(one_position["positionValue"])
            upnl = float(one_position["unrealizedPnl"])

            entry_px = float(one_position["entryPx"])
            mark_px = abs(pos_val / pos)

            pos_line = "{}: pos {:.4f} val {:.2f} upnl {:.2f} entryPx {:.4f} markPx {:.4f}\n".format(
                one_position["coin"],
                pos, 
                pos_val, 
                upnl,
                entry_px,
                mark_px
            )
            all_lines += pos_line
            # I guess we can top it off in this case. 
    all_lines += "*"*100 + "\n"

    # Add acct value. 
    all_lines += "act value: {}\n".format(json.dumps(user_data_dct["marginSummary"], indent=2))

    # For checking L1 rate limits
    params_l1rate = {
        "type": "userRateLimit",
        "user": addresses[0],
    }
    user_data_l1rate = session.post(
        endpt,
        json=params_l1rate,
    )
    user_data_l1rate_dct = json.loads(user_data_l1rate.text)
    l1rate_cap = user_data_l1rate_dct["nRequestsCap"]
    l1rate_used = user_data_l1rate_dct["nRequestsUsed"]
    l1rate_rem = l1rate_cap - l1rate_used

    all_lines += "*"*100 + "\n"
    all_lines += "L1 rate limit: {}\n".format(json.dumps(user_data_l1rate_dct, indent=2))
    all_lines += f"Remaining: {l1rate_rem} of {l1rate_cap} ({l1rate_rem/l1rate_cap:.2%})"

    print(all_lines)

    #print(json.dumps(user_data_dct, indent=2))

    subject="POSITION UPDATE"
    if label is not None:
        subject += f" ({label})"
    email_utils.send_mail(
        subject=subject,
        body=all_lines,
        add_hostname=False
    )





def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--how", default=False, action="store_true")
    parser.add_argument("--testnet", default=False, action="store_true")
    parser.add_argument("--label", choices=["OLD", "L1G", "VAULT"])
    parser.add_argument("--addresses", nargs='*')
    args = parser.parse_args()

    # Check every day. 
    check_positions(args.testnet, args.label, args.addresses)

if __name__ == "__main__":
    main()
