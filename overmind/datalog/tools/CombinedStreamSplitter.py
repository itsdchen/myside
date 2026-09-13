#! /usr/bin/env python3

"""

Splits combined capture- and snapshot- files into per-symbol, per-stream instances.

Example usage:
./CombinedStreamSplitter.py --market BinanceSPOT --capture-path capture.txt --snapshot-path snapshot.txt --date 20231214 --outdir BinancecSPOT

"""

import argparse
import json
import os
from pathlib import Path
from typing import Dict
import sys


def parse_binance_combined(capture_paths, snapshot_path, date, outdir):

    # Parse the stream/diff-capture first.
    sym_stream_to_out_f = {}
    print("Binance splitting, capture_paths is {}".format(capture_paths))

    # There are a few files, sorted re: time, and we go through them to split and write.
    for one_capture_path in capture_paths:
        print("Reading {}".format(one_capture_path))
        total_bytes = os.path.getsize(one_capture_path)
        read_bytes = 0

        with open(one_capture_path) as in_f:
            for i, line in enumerate(in_f):
                read_bytes += len(line)
                if i % 1_000_000 == 0:
                    completion = read_bytes / total_bytes * 100
                    print(f"Split completion: {completion:.2f}%")

                # format is "ts,json", but json may contain commas
                splitted = line.split(",")
                try:
                    rx_ns = int(splitted[0])
                    rawMsg = ",".join(splitted[1:])
                    msg = json.loads(rawMsg)
                except Exception as e:
                    print("Messed up parsing line (skipping): {}".format(line))
                    continue

                sym = msg["data"]["s"]
                # This is basically true if
                stream_name = msg["stream"].split("@")[1]

                # sym, stream = parse_fn(msg)

                # Let's just rename all of these to be capitalized names. Srsly.
                if (sym, stream_name) not in sym_stream_to_out_f:
                    # Modification for hyperliquid SPOT: replace slashes with underscores.
                    replacement_sym = sym.replace("/", "_")
                    out_fname = "{SYM}_{STREAM_NAME}_{DATE}.jsonl".format(
                        SYM=replacement_sym, STREAM_NAME=stream_name, DATE=date
                    )
                    out_path = os.path.join(outdir, out_fname)
                    sym_stream_to_out_f[(sym, stream_name)] = open(out_path, "w")

                out_f = sym_stream_to_out_f[(sym, stream_name)]
                # add rx (receive timestamp)
                msg["rx"] = rx_ns
                out_f.write(json.dumps(msg))
                out_f.write("\n")

    # close files after done.
    for out_f in sym_stream_to_out_f.values():
        out_f.close()

    # Then handle the snapshots.
    # Don't necessarily die out if it doesn't exist.
    if not os.path.exists(snapshot_path):
        print("Snapshot path {} does not exist -- skipping".format(snapshot_path))
        return
    sym_to_out_f = {}
    with open(snapshot_path) as in_f:
        for line in in_f:
            # format is "ts,sym,json", but json may contain commas
            splitted = line.split(",")
            try:
                rx = int(splitted[0])
                sym = splitted[1]
                msg = {
                    "stream": f"{sym.lower()}@depthSnapshot",
                    "rx": rx,
                    "data": json.loads(",".join(splitted[2:])),
                }
            except Exception as e:
                print("Error parsing {} -- continuing".format(line))
                continue
            if sym not in sym_to_out_f:
                out_fname = f"{sym}_depthSnapshot_{date}.jsonl"
                out_path = os.path.join(outdir, out_fname)
                sym_to_out_f[sym] = open(out_path, "w")
            out_f = sym_to_out_f[sym]
            out_f.write(json.dumps(msg))
            out_f.write("\n")

    # close files
    for out_f in sym_to_out_f.values():
        out_f.close()



# Snapshots are INSIDE the capture.
def parse_hyperliquid_combined(capture_paths, date, outdir):

    # Parse the stream/diff-capture first.
    sym_stream_to_out_f = {}
    print("Hyperliquid splitting, capture_paths is {}".format(capture_paths))
    for one_capture_path in capture_paths:
        total_bytes = os.path.getsize(one_capture_path)
        read_bytes = 0

        with open(one_capture_path) as in_f:
            for i, line in enumerate(in_f):
                read_bytes += len(line)
                if i % 1_000_000 == 0:
                    completion = read_bytes / total_bytes * 100
                    print(f"Split completion: {completion:.2f}%")

                # format is "ts,json", but json may contain commas
                splitted = line.split(",")
                try:
                    rx_ns = int(splitted[0])
                    rawMsg = ",".join(splitted[1:])
                    msg = json.loads(rawMsg)
                except Exception as e:
                    print("Issue loading msg {}".format(rawMsg))
                    continue
                if msg["channel"] == "subscriptionResponse":
                    continue

                sym = ""
                try:
                    if msg["channel"] == "l2Book":
                        sym = msg["data"]["coin"]
                    elif msg["channel"] == "trades":
                        sym = msg["data"][0]["coin"]
                except Exception as e:
                    print("Issue: misparsed {}".format(msg))
                    continue

                # This is basically true if
                stream_name = msg["channel"]
                # print("Sym is ", sym)
                # sym, stream = parse_fn(msg)

                # Let's just rename all of these to be capitalized names. Srsly.
                if (sym, stream_name) not in sym_stream_to_out_f:
                    replacement_sym = sym.replace("/", "_")
                    out_fname = "{SYM}_{STREAM_NAME}_{DATE}.jsonl".format(
                        SYM=replacement_sym, STREAM_NAME=stream_name, DATE=date
                    )
                    out_path = os.path.join(outdir, out_fname)
                    # print("Hi! For Hyperliquid, {} => {} | {}".format(sym, out_fname, out_path))
                    sym_stream_to_out_f[(sym, stream_name)] = open(out_path, "w")

                out_f = sym_stream_to_out_f[(sym, stream_name)]
                # add rx (receive timestamp)
                msg["rx"] = rx_ns
                out_f.write(json.dumps(msg))
                out_f.write("\n")

    # close files
    for out_f in sym_stream_to_out_f.values():
        out_f.close()


def parse_bybit_combined(capture_paths, date, outdir):

    # Parse the stream/diff-capture first.
    sym_stream_to_out_f = {}

    # There are a few files, sorted re: time, and we go through them to split and write.
    for one_capture_path in capture_paths:

        total_bytes = os.path.getsize(one_capture_path)
        read_bytes = 0

        # Parse the stream/diff-capture first.
        with open(one_capture_path) as in_f:
            for i, line in enumerate(in_f):
                read_bytes += len(line)
                if i % 1_000_000 == 0:
                    completion = read_bytes / total_bytes * 100
                    print(f"Split completion: {completion:.2f}%")

                # format is "ts,json", but json may contain commas
                splitted = line.split(",")
                try:
                    rx_ns = int(splitted[0])
                    rawMsg = ",".join(splitted[1:])
                    msg = json.loads(rawMsg)
                except Exception as e:
                    print("Error parsing json: {}".format(rawMsg))
                    continue

                # There's stuff like "Success" lines in here that we want to ignore.
                if "topic" not in msg:
                    continue

                split_topic = msg["topic"].split(".")
                sym = split_topic[-1]
                stream_name = ".".join(split_topic[:-1])

                # Let's just rename all of these to be capitalized names. Srsly.
                if (sym, stream_name) not in sym_stream_to_out_f:
                    out_fname = f"{sym}_{stream_name}_{date}.jsonl"
                    out_path = os.path.join(outdir, out_fname)
                    sym_stream_to_out_f[(sym, stream_name)] = open(out_path, "w")

                out_f = sym_stream_to_out_f[(sym, stream_name)]
                # add rx (receive timestamp)
                msg["rx"] = rx_ns
                out_f.write(json.dumps(msg))
                out_f.write("\n")

        # close files
    for out_f in sym_stream_to_out_f.values():
        out_f.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", type=str, required=True)
    parser.add_argument("--date", type=str, required=True)
    parser.add_argument("--capture_prefix", type=str, required=True)
    parser.add_argument("--snapshot", type=str, required=True)
    parser.add_argument("--outdir", type=str, default=os.getcwd())
    args = parser.parse_args()

    # create outdir if needed.
    Path(args.outdir).mkdir(exist_ok=True, parents=True)

    capture_files = [args.capture_prefix]
    for i in range(10):
        pot_capture_file = "{}.{}".format(args.capture_prefix, i)
        if os.path.exists(pot_capture_file):
            capture_files.append(pot_capture_file)

    # With all of these, the format is
    if (
        args.market == "BinanceSPOT"
        or args.market == "BinanceFutures"
        or args.market == "BinanceCOINFutures"
    ):
        parse_binance_combined(capture_files, args.snapshot, args.date, args.outdir)
    elif args.market == "Hyperliquid":
        parse_hyperliquid_combined(capture_files, args.date, args.outdir)
    elif (
        args.market == "BybitSPOT"
        or args.market == "BybitDeriv"
        or args.market == "BybitInverseDeriv"
    ):
        parse_bybit_combined(capture_files, args.date, args.outdir)

    # split_captures(args.capture_path, args.market, args.date, args.outdir)
    # split_snapshots(args.snapshot_path, args.date, args.outdir)


if __name__ == "__main__":
    main()
