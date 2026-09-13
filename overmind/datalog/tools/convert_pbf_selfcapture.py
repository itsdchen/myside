#! /usr/bin/env python3

"""
For batch-processing my jsonl files into pbf files.

Example usage:
./batch_json_pbf.py --src /home/dchen/tardis_datasets/ --dest /media/dchen/T7/tardis_datasets/ --start 20230501 --end 20230502 --mkt BinanceSPOT --symbol ETHUSDT

"""

import os
import subprocess
import sys
import argparse
import datetime

mkt_to_stream_types = {
    "BinanceFutures": ["bookTicker", "depthSnapshot", "depth", "trade"],
    "BinanceCOINFutures": ["bookTicker", "depthSnapshot", "depth", "trade"],
    "BinanceSPOT": ["bookTicker", "depthSnapshot", "depth", "trade"],

    # IRL, there are orderbook.200 feeds too.
    "BybitDeriv": ["publicTrade", "orderbook.1", "orderbook.50", "orderbook.500"],
    "BybitInverseDeriv": ["publicTrade", "orderbook.1", "orderbook.50", "orderbook.500"],
    "BybitSPOT": ["publicTrade", "orderbook.1", "orderbook.50"],

    "Hyperliquid": ["l2Book", "trades"],
}

def translate_one(symbol, market, date, overwrite, src, dest, translate_bin):
    stream_types = mkt_to_stream_types[market]
    for one_stream in stream_types:

        src_sym = symbol.replace("/", "_")
        dest_sym = symbol.replace("/", "_")
        src_path = os.path.join(
            src, market, "{}_{}_{}.jsonl".format(src_sym, one_stream, date)
        )
        if not os.path.exists(src_path):
            print("No file found at {}".format(src_path))
            continue

            # If it's empty, also skip.
        if os.path.getsize(src_path) == 0:
            print("{} empty, skipping".format(src_path))
            continue

            # Otherwise, try to translate it.
        dest_path = os.path.join(
            dest, market, "{}_{}_{}.gzpbf".format(dest_sym, one_stream, date)
        )
        if os.path.exists(dest_path) and not overwrite:
            print(
                "Skipping {} because exists and not choosing to overwrite".format(
                    dest_path
                )
            )
            continue

            # Compress to lvl 5 by default.
        translate_cmd = '{BIN} --src {SRC_PATH} --dest "{DEST_PATH}" --mkt {MARKET} --compress --compress_level 5 --our-capture'.format(
            BIN=translate_bin, SRC_PATH=src_path, DEST_PATH=dest_path, MARKET=market
        )
        print("Running: {}".format(translate_cmd))
        subprocess.run(translate_cmd, shell=True)

def translate(symbol, market, start_day, end_day, overwrite, src, dest, translate_bin):
    dates = [
        start_day + datetime.timedelta(days=x) for x in range((end_day - start_day).days)
    ]


    dates = dates_list(start_day, end_day)
    for one_day in dates:
        translate_one(symbol, market, one_day, overwrite, src, dest, translate_bin)

def main():
    parser = argparse.ArgumentParser()
    # Which symbol.
    parser.add_argument("--symbol", type=str, required=True)
    # eg. binancefutures, binancespot
    parser.add_argument("--mkt", type=str, required=True)
    parser.add_argument("--start", type=str)
    parser.add_argument("--end", type=str)
    parser.add_argument("--date", type=str)
    parser.add_argument("--overwrite", action="store_true")

    parser.add_argument(
        "--src", type=str, default=os.path.expanduser('~/tardis_datasets')
    )
    # After we convert to protobuf, we can further compress things with gzip. The default dir here is
    # where we put gzip-compressed protobuf files.
    parser.add_argument(
        "--dest",
        type=str,
        default=os.path.expanduser('~/tardis_datasets/gzpbf')
    )
    parser.add_argument(
        "--translate-bin",
        dest="translate_bin",
        type=str,
        required=True
    )

    args = parser.parse_args()

    sym = args.symbol
    print(args.date)

    if args.date is not None:
        translate_one(sym, args.mkt, args.date, args.overwrite, args.src, args.dest, args.translate_bin)
    else:
        translate(sym, args.mkt, args.start, args.end, args.overwrite, args.src, args.dest, args.translate_bin)



if __name__ == "__main__":
    main()
