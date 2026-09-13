import argparse
import json
from typing import Dict
import re
from pathlib import Path

"""
Script for combining two jsonl files of the same market/stream into a single jsonl

Usage:
./combine_captures.py feed1/btcusdt_bookTicker_20240104.jsonl feed2/btcusdt_bookTicker_20240104.jsonl --out combined/btcusdt_bookTicker_20240104.jsonl --market BinanceFutures --stream bookTicker

"""

filename_matcher = re.compile(r'(?P<symbol>\w+)_(?P<stream>(\w|\.|\@)+)_(?P<date>\d{8}).*.jsonl?(\.\d+)')

def get_uid(msg: Dict, market: str, stream: str):
    if market == "BinanceFutures":
        if stream == "bookTicker":
            return msg['data']['u']
        elif stream == "trade":
            return msg['data']['t']
        elif stream == "depth":
            # return last update id
            return msg['data']['u']
        else:
            raise ValueError("unsupported stream {stream} for market {market}")
    else:
        raise ValueError(f"unsupported market {market}")

def combine_captures(capture1, capture2, outpath, market, stream):
    seen = set()
    m1, m2 = None, None
    done1, done2 = False, False
    cnt = 0
    with open(outpath, "w") as out_f:
        with open(capture1) as cap_f1:
            with open(capture2) as cap_f2:
                while (not done1 and not done2) or m1 or m2:
                    cnt += 1
                    if cnt % 100_000 == 0:
                        print(cnt, "messages processed")
                    if not m1 and not done1:
                        l = cap_f1.readline()
                        if not l:
                            done1 = True
                        else:
                            m1 = json.loads(l)
                    if not m2 and not done2:
                        l = cap_f2.readline()
                        if not l:
                            done2 = True
                        else:
                            m2 = json.loads(l)
                    if m1 and m2:
                        r1 = int(m1["rx"])
                        r2 = int(m2["rx"])
                        if r1 < r2:
                            m = m1
                            m1 = None
                        else:
                            m = m2
                            m2 = None
                        uid = get_uid(m, market, stream)
                        if uid not in seen:
                            seen.add(uid)
                            out_f.write(json.dumps(m) + "\n")
                    elif m1:
                        uid = get_uid(m1, market, stream)
                        m1 = None
                        if uid not in seen:
                            out_f.write(json.dumps(m1) + "\n")
                            seen.add(uid)
                    elif m2:
                        uid = get_uid(m1, market, stream)
                        m2 = None
                        if uid not in seen:
                            out_f.write(json.dumps(m2) + "\n")
                            seen.add(uid)

def get_stream_from_path(path: str):
    p = Path(path)
    m = filename_matcher.match(p.name)
    if not m:
        raise ValueError(f'could not infer market and stream from path: {path}')
    groupdict = m.groupdict()
    return groupdict['stream']

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture1", type=str)
    parser.add_argument("capture2", type=str)
    parser.add_argument("--outpath", type=str)
    parser.add_argument("--market", type=str, required=True)
    parser.add_argument("--stream", type=str, help="override for stream")
    args = parser.parse_args()

    market = args.market
    if args.stream:
        stream = args.stream
    else:
        # infer stream from filenames
        stream1 = get_stream_from_path(args.capture1)
        stream2 = get_stream_from_path(args.capture2)
        if stream1 != stream2:
            raise ValueError(f"inferred stream mismatch: {stream1} {stream2}")
        stream = stream1
        
    combine_captures(args.capture1, args.capture2, args.outpath, market, stream)    

if __name__ == '__main__':
    main()