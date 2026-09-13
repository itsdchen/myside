#!/usr/bin/env python3

import argparse
import json
import os
import sys

from collections import defaultdict

CAT_TO_SYM = {
    "useq": [
        "xyz:TSLA",
        "xyz:NVDA",
        "xyz:HOOD",
        "xyz:INTC",
        "xyz:PLTR",
        "xyz:COIN",
        "xyz:META",
        "xyz:AAPL",
        "xyz:MSFT",
        "xyz:ORCL",
        "xyz:GOOGL",
        "xyz:AMZN",
        "xyz:AMD",
        "xyz:MU",
        "xyz:SNDK",
        "xyz:MSTR",
        "xyz:CRCL",
        "xyz:NFLX",
        "xyz:COST",
        "xyz:LLY",
        "xyz:TSM",
        "xyz:RIVN",
        "xyz:BABA",
        "xyz:USAR",
        "xyz:CRWV",
        "xyz:URNM",
        "xyz:DXY",
        "xyz:HIMS",
        "xyz:GME",
        "xyz:DKNG",
        "xyz:BIRD",
        "xyz:LITE",
        "xyz:BX",
        "xyz:MRVL",
        "xyz:RKLB",
        "xyz:ZM",
        "xyz:EBAY",
        "xyz:CBRS",
        "xyz:PURRDAT",
        "xyz:ARM",
        "xyz:BB",
        "xyz:IBM",
        "xyz:DELL",
        "xyz:NOK",
        "xyz:SMH",
        "xyz:SPCX",
        "xyz:AVGO",
        "xyz:NOW",
        "xyz:NBIS",
        "xyz:WDC",
        "xyz:ASML",
        "xyz:QNT",
        "xyz:BE",
        "xyz:QCOM",
        "xyz:STRC",
        "xyz:BOT",
        "xyz:AMAT",
        "xyz:SHAZ",
        "xyz:SKHY",
        "xyz:GEV",
    ],

    "etf": [
        "xyz:XYZ100",
        "xyz:SP500",
        "xyz:EWY",
        "xyz:EWJ",
        "xyz:EWZ",
        "xyz:EWT",
        "xyz:XLE",
        "xyz:DRAM",
        "xyz:KSTR"
    ],

    "comm": [
        "xyz:GOLD",
        "xyz:SILVER",
        "xyz:CL",
        "xyz:BRENTOIL",
        "xyz:COPPER",
        "xyz:NATGAS",
        "xyz:PLATINUM",
        "xyz:URANIUM",
        "xyz:ALUMINIUM",
        "xyz:PALLADIUM",
        "xyz:BRENTOIL",
    ],

    "asia": [
        "xyz:SKHX",
        "xyz:SMSN",
        "xyz:HYUNDAI",
        "xyz:KR200",
        "xyz:JP225",
        "xyz:KIOXIA",
        "xyz:SOFTBANK",
        "xyz:MINIMAX",
        "xyz:ZHIPU",
        "xyz:CXMT",
        "xyz:GIGADEV",
    ],

    "other": [
        "xyz:JPY",
        "xyz:EUR",
        "xyz:GBP",
        "xyz:VIX",
        "HYPE",
        "BTC",
    ],

    "new": [
        "xyz:IBOV",
        "xyz:IBIDEN",
        "xyz:KSTR",
        "xyz:CXMT",
        "xyz:GIGADEV",
        "xyz:GEV",
    ]
}

def main():
    parser = argparse.ArgumentParser(description="Split trades by symbol category")
    parser.add_argument("-c", "--categs", action="store_true",
                        help="Print out the categories, one per line")
    parser.add_argument("-o", "--outdir", default="~/webpnl",
                        help="Directory to write files (default: ~/webpnl/)")
    args = parser.parse_args()

    if args.categs:
        print("\n".join(cat for cat in CAT_TO_SYM))
        return

    sym_to_cats = defaultdict(list)
    for cat, syms in CAT_TO_SYM.items():
        for sym in syms:
            sym_to_cats[sym].append(cat)
    writers = {cat: open(os.path.expanduser(f"{args.outdir}/trades.{cat}.csv"), "w")
               for cat in CAT_TO_SYM}
    # Also split the overall trades (all categories) by ADD vs REM tag (field 20).
    # Synthetic MARK re-mark rows go to both so their time-series graphs still work.
    tag_writers = {tag: open(os.path.expanduser(f"{args.outdir}/trades.{tag}.csv"), "w")
                   for tag in ("add", "rem")}

    for line in sys.stdin:
        fields = line.split(",")
        sym = fields[3]
        for cat in (sym_to_cats[sym] or ["other"]):
            writers[cat].write(line)
        tag = fields[20].strip() if len(fields) > 20 else ""
        if tag == "ADD":
            tag_writers["add"].write(line)
        elif tag == "REM":
            tag_writers["rem"].write(line)
        elif tag == "MARK":
            tag_writers["add"].write(line)
            tag_writers["rem"].write(line)

    for w in (*writers.values(), *tag_writers.values()):
        w.close()

if __name__ == "__main__":
    main()
