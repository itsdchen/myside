"""Sum HL trade notional during 20:00-04:00 ET across a date range, for each
xyz:SYMBOL that's in _US_EQUITIES. Used to pick candidate symbols for the
boats-variant playbook study.
"""
import sys, gzip, glob, os, datetime, concurrent.futures as cf
sys.path.insert(0, "/home/pktrade/tradefi/retraded/overmind/strat_main")
from pyfeed import mdmsg_pb2 as M
from google.protobuf.internal.decoder import _DecodeVarint32
from util import symbolizer

ROOT = "/home/pktrade/tardis_datasets/gzpbf/Hyperliquid"
# UTC dates spanning Mon 5/18 evening ET through Sat 5/23 morning ET
DATES = ["20260518", "20260519", "20260520", "20260521", "20260522", "20260523"]
ET = datetime.timezone(datetime.timedelta(hours=-4))  # EDT

def is_overnight(ts_ms):
    h = datetime.datetime.fromtimestamp(ts_ms/1000, tz=ET).hour
    return h >= 20 or h < 4

def file_notional(path):
    if not os.path.exists(path):
        return 0.0, 0
    notional = 0.0
    n = 0
    try:
        with gzip.open(path, "rb") as f:
            data = f.read()
    except Exception:
        return 0.0, 0
    pos = 0
    while pos < len(data):
        try:
            size, new_pos = _DecodeVarint32(data, pos)
        except Exception:
            break
        pos = new_pos
        pb = M.PbMessage()
        try:
            pb.ParseFromString(data[pos:pos+size])
        except Exception:
            pos += size; continue
        pos += size
        if not pb.HasField("book_trade"):
            continue
        t = pb.book_trade.trade_time
        if not is_overnight(t):
            continue
        try:
            px = float(pb.book_trade.px); qty = float(pb.book_trade.qty)
        except ValueError:
            continue
        notional += px * qty
        n += 1
    return notional, n

def sym_total(sym):
    total = 0.0; total_n = 0
    for d in DATES:
        path = f"{ROOT}/xyz:{sym}_trades_{d}.gzpbf"
        n, c = file_notional(path)
        total += n; total_n += c
    return sym, total, total_n

us = sorted(symbolizer._US_EQUITIES)
results = []
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    for r in ex.map(sym_total, us):
        results.append(r)

results.sort(key=lambda r: -r[1])
print(f"{'symbol':10s} {'5d_notional':>16s} {'trades':>10s}")
for sym, nt, nc in results:
    if nt > 0:
        print(f"{sym:10s} ${nt:>14,.0f}  {nc:>9d}")
print("---")
print(f"{'TOTAL':10s} ${sum(r[1] for r in results):>14,.0f}  {sum(r[2] for r in results):>9d}")
