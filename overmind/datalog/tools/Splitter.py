#! /usr/bin/env python3

"""
Splits combined capture and snapshot files into per symbol and per stream with naming like {sym}_{stream}_{date}.jsonl

Example usage:
./Splitter.py --market BinanceSPOT --capture-path capture.out --snapshot-path snapshot.out --date 20231214 --outdir BinanceSPOT
"""

import argparse
import json
import os
from pathlib import Path
from typing import Dict

def parse_binance_msg(msg: Dict):
    data = msg['data']
    stream = data['e']
    # rename for consistency with tardis
    if stream == 'depthUpdate':
        stream = 'depth'
    return data['s'], stream

def split_captures(capture_path: str, market: str, date: str, outdir: str):

    if market in ('BinanceFutures', 'BinanceSPOT', 'BinanceCOINFutures'):
        parse_fn = parse_binance_msg

    total_bytes = os.path.getsize(capture_path)
    read_bytes = 0

    sym_stream_to_out_f = {}
    with open(capture_path) as in_f:
        for i, line in enumerate(in_f):
            read_bytes += len(line)
            if i % 1_000_000 == 0:
                completion = read_bytes / total_bytes * 100
                print(f"Split completion: {completion:.2f}%")

            # format is "ts,json", but json may contain commas
            splitted = line.split(",")
            rx = int(splitted[0])
            rawMsg = ",".join(splitted[1:])
            msg = json.loads(rawMsg)
            sym, stream = parse_fn(msg)

            if (sym, stream) not in sym_stream_to_out_f:
                out_fname = f'{sym.lower()}_{stream}_{date}.jsonl'
                out_path = os.path.join(outdir, out_fname)
                sym_stream_to_out_f[(sym, stream)] = open(out_path, "w")

            out_f = sym_stream_to_out_f[(sym, stream)]
            # add rx (receive timestamp)
            msg['rx'] = rx
            out_f.write(json.dumps(msg))
            out_f.write('\n')

    # close files
    for out_f in sym_stream_to_out_f.values():
        out_f.close()

def split_snapshots(snapshot_path: str, date: str, outdir: str):
    sym_to_out_f = {}
    with open(snapshot_path) as in_f:
        for line in in_f:
            # format is "ts,sym,json", but json may contain commas
            splitted = line.split(",")
            rx = int(splitted[0])
            sym = splitted[1]
            msg = {
                "stream": f"{sym.lower()}@depthSnapshot",
                "rx": rx,
                "data": json.loads(",".join(splitted[2:]))
            }
            if sym not in sym_to_out_f:
                out_fname = f"{sym.lower()}_depthSnapshot_{date}.jsonl"
                out_path = os.path.join(outdir, out_fname)
                sym_to_out_f[sym] = open(out_path, "w")
            out_f = sym_to_out_f[sym]
            out_f.write(json.dumps(msg))
            out_f.write('\n')

    # close files
    for out_f in sym_to_out_f.values():
        out_f.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=str, required=True)
    parser.add_argument('--date', type=str, required=True)
    parser.add_argument('--capture-path', type=str, required=True)
    parser.add_argument('--snapshot-path', type=str, required=True)
    parser.add_argument('--outdir', type=str, default=os.getcwd())
    args = parser.parse_args()

    # create outdir
    Path(args.outdir).mkdir(exist_ok=True, parents=True)

    split_captures(args.capture_path, args.market, args.date, args.outdir)
    split_snapshots(args.snapshot_path, args.date, args.outdir)

if __name__ == '__main__':
    main()
