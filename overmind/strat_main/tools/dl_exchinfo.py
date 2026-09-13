#! /usr/bin/env python

# For downloading exchangeinfo that would be used for
# populating secmaster files. Run this on the aws machine.
# Also, fill in the api key
import json
import requests
import time

# These are the filters that we prob want. Defined here:
# https://binance-docs.github.io/apidocs/spot/en/#filters

filters = [
    "PRICE_FILTER",
    # Don't really want.
    #    "PERCENT_PRICE",

    "LOT_SIZE",
    "NOTIONAL",
]


# Let's first get the spot one.
session = requests.Session()
session.headers.update(
    {"Content-Type": "application/json;charset=utf-8", "X-MBX-APIKEY": ""}
)

params = {}
#full_endpt = endpt + "/depth?symbol=SUIUSDT&limit=10"

def fetch_binancespot_info():
    endpt = "https://api.binance.com/api/v3/exchangeInfo"
    x = session.get(**{"url": endpt, "params": params})

    exchInfo_dict = json.loads(x.text)
    out_file = "BinanceSPOT_exchinfo.json"
    with open(out_file, "w") as f:
        f.write(json.dumps(exchInfo_dict, indent=2))
    print("Wrote it.")

def fetch_binancefutures_info():
    endpt = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    x = session.get(**{"url": endpt, "params": params})

    exchInfo_dict = json.loads(x.text)
    out_file = "BinanceFutures_exchinfo.json"
    with open(out_file, "w") as f:
        f.write(json.dumps(exchInfo_dict, indent=2))
    print("Wrote it.")

def fetch_binancecoinfutures_info():
    endpt = "https://dapi.binance.com/dapi/v1/exchangeInfo"
    x = session.get(**{"url": endpt, "params": params})

    exchInfo_dict = json.loads(x.text)
    out_file = "BinanceCOINFutures_exchinfo.json"
    with open(out_file, "w") as f:
        f.write(json.dumps(exchInfo_dict, indent=2))
    print("Wrote it.")


print("Fetching spot info")
fetch_binancespot_info()
time.sleep(5)

print("Fetching futures info")
fetch_binancefutures_info()
time.sleep(5)

fetch_binancecoinfutures_info()

print("Done!")