#! /usr/bin/env python

"""
    Given a symbol and a market, run symstats on it and record the daily and aggregate
    stats.

    Sample cmd:
    ~/tradefi/pkt_0/overmind/strat_main/stratbuilder/DailyStats.py --symbol DYDXUSDT --books BinanceSPOT --dates "20230103,20230104" --start "03:00:00 America/New_York" --end "04:00:00 America/New_York"

"""


import os
import shlex, subprocess
import argparse
import json

import sys
import math

##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from templatizers import TempoFormer, SigFormer

from util.pathing import bindir

# Get the bin.
symstats_bin = os.path.join(bindir(), "symstats")


def run_symstats(symbol, book, dates, start_t, end_t):
    procs = []
    for one_date in dates:
        symstats_cmd = '{BIN} --date {DATE} --start-time "{START}" --end-time "{END}" --book {BOOK} --symbol {SYMBOL} --csv'.format(
            BIN=symstats_bin,
            DATE=one_date,
            START=start_t,
            END=end_t,
            BOOK=book,
            SYMBOL=symbol,
        )

        print(symstats_cmd)
        split_cmd = shlex.split(symstats_cmd)

        symstats_proc = subprocess.Popen(
            split_cmd, shell=False, stdout=subprocess.PIPE
        )
        procs.append(symstats_proc)

    for symstats_proc in procs:
        symstats_proc.wait()

    wanted_cols = [
        "num_trades",
        "vol",
        "med_inside_liq",
        "avg_mid",
        "spread_pct",
        "n_midchanges",
    ]

    daily_stats = {}

    tot_stats = {}
    for col in wanted_cols:
        tot_stats[col] = 0

    counted_dates = 0

    for i, symstats_proc in enumerate(procs):
        symstats_std, symstats_errs = symstats_proc.communicate()
        symstats_std = symstats_std.decode("utf8").split("\n")

        # OK, so if we get baddays this messes up. OK.
        kept_symstats = []
        for one_line in symstats_std:
            if "exist" in one_line:
                continue
            if "Popping" in one_line:
                continue
            # l/w, add it.
            kept_symstats.append(one_line)


        labels = kept_symstats[0].split(",")
        vals = kept_symstats[1].split(",")

        date = dates[i]
        day_stats = {}
        for i, label in enumerate(labels):
            if label in wanted_cols:
                day_stats[label] = float(vals[i])
        print(day_stats)
        skip_date = False
        try:
            for col in wanted_cols:
                # Filter out nanning.
                if math.isnan(day_stats[col]):
                    print("Skipping {} because of a nan for {}: {}".format(
                        i, col, day_stats[col]
                    ))
                    skip_date = True
        except Exception as e:
            print("Skipping day {} because of an exception: {} | stats were {}".format(date, e, day_stats))
            skip_date = True
        if skip_date:
            continue

        # Look for nans.
        daily_stats[date] = day_stats
        for col in wanted_cols:
            tot_stats[col] += day_stats[col]
        counted_dates += 1
    # Average out the daily stats
    avg_stats = {}
    for col in wanted_cols:
        avg_stats["avg_{}".format(col)] = tot_stats[col] / counted_dates
    return {"avg": avg_stats, "daily": daily_stats}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--book", type=str, required=True)
    parser.add_argument("--dates", type=str, required=True)
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    args = parser.parse_args()

    dates_list = args.dates.split(",")

    # Run tickrate accordingly.
    symstats_dict = run_symstats(
        args.symbol, args.book, dates_list, args.start, args.end
    )
    print("symstats_dict: {}".format(symstats_dict))


if __name__ == "__main__":
    # Do this main stuff.
    main()
