#! /usr/bin/env python

"""
    Given a candidate tempo and some specs, run the metronome on them and figure out the rate.

    Metronome should output the elapsed time anyway.

    Sample cmd:
    ~/tradefi/pkt_0/overmind/strat_main/stratbuilder/TempoRate.py --workdir /home/david/modeltrain/modelv1/tickrate --symbol DYDXUSDT --books BinanceSPOT --dates "20230103,20230104" --start "03:00:00 America/New_York" --end "04:00:00 America/New_York" --tempotype "msg"

"""

import os
import shlex, subprocess
import argparse
import json

import sys

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
metro_bin = os.path.join(bindir(), "metronome")

# Returns:
# ticks_per_sec
# tot_ticks
# bad_dates
def run_tickrate(symbol, books, dates, start_t, end_t, tempo_type, work_dir, name_prefix=""):
    # Make the workdir, write the tempo.

    if not os.path.exists(work_dir):
        os.mkdir(work_dir)

    tempo_path = os.path.join(work_dir, "{}_tickrate_tempo.json".format(name_prefix))

    # Write out the tempo with tempoformer, yeah?

    tempo_name = "{}_{}_{}".format(name_prefix, tempo_type, symbol)
    tempo_dict = TempoFormer.make_tempo(tempo_type,
        {
            "name": tempo_name,
            "symbol": symbol,
            "markets": books
        })
    with open(tempo_path, "w") as f:
        f.write(json.dumps(tempo_dict, indent=2))

    tot_ticks = 0
    tot_secs = 0
    bad_dates = []

    # TODO: parallelize
    daily_ticks = {}

    procs = []
    for one_date in dates:

        metro_cmd = "{BIN} --date {DATE} --start \"{START}\" --end \"{END}\" --conf {TEMPO_PATH}".format(
            BIN=metro_bin,
            DATE=one_date,
            START=start_t,
            END=end_t,
            TEMPO_PATH=tempo_path)

        #print(metro_cmd)
        split_cmd = shlex.split(metro_cmd)

        metro_proc = subprocess.Popen(split_cmd, shell=False, stdout=subprocess.PIPE)
        procs.append(metro_proc)

    # Print the first one.
    print("Metro cmd: \n{}".format(metro_cmd))

    for metro_proc in procs:
        metro_proc.wait()

    for idx, metro_proc in enumerate(procs):
        metro_std, metro_errs = metro_proc.communicate()
        metro_std = metro_std.decode('utf8').split('\n')

        metr_output = {"num_ticks": 0, "tot_secs": 0}
        for one_line in metro_std:
            if "num_tempo_fires" in one_line:
                metr_output["num_ticks"] = int(one_line.split(" ")[-1])
            elif "tot_secs" in one_line:
                metr_output["tot_secs"] = float(one_line.split(" ")[-1])


        # Parse and add, man.
        if metr_output["num_ticks"] == 0:
            # Do not pass go.
            bad_dates.append(one_date)
        else:
            tot_ticks += metr_output["num_ticks"]
            tot_secs += metr_output["tot_secs"]

        daily_ticks[dates[idx]] = {"n_ticks": metr_output['num_ticks'], "n_secs": metr_output['tot_secs']}

        #print(metr_output)
        # TODO: to account for large tail days, perhaps use
        # median counts too.

    tick_rate = tot_ticks / tot_secs
    to_ret = {
        "daily_ticks": daily_ticks,
        "tot_ticks": tot_ticks,
        "ticks_per_sec": tick_rate,
        "tot_secs": tot_secs,
        "bad_dates": bad_dates,
        "tempo_dict": tempo_dict,
        "tempo_name": tempo_name
    }
    return to_ret

def gen_scan_conf(conf_path, num_samples, markout_tgt, symbol, books, dates, start_t, end_t, tempo_type, work_dir, name_prefix="", markout_cap=0.0035, flag_mid=False):
    tempo_rate = run_tickrate(symbol, books, dates, start_t, end_t, tempo_type, work_dir, name_prefix=name_prefix)

    ticks_between_sample = tempo_rate["tot_ticks"] / num_samples
    markout_thresh = tempo_rate["ticks_per_sec"] * markout_tgt

    all_tempos = []
    all_tempos += tempo_rate["tempo_dict"]["tempos"]

    flag_mid_dct = {}
    if flag_mid:
        flag_mid_dct = {"top_level_signal": {"use_flagged": flag_mid}}


    # Let's get the sigformer.
    sig_mid = SigFormer.SigMidSpec(
        "ref",
        symbol=symbol,
        markets=books,
        fixed_val_overrides=flag_mid_dct,
        climbables={},
    )

    sig_mid_name = sig_mid.name
    sig_mid_dict = sig_mid.get_sig_dict()["signals_dict"]["signals"][0]

    # TODO: get the signal conf dict from the sigformer. yeah.

    # Now, write the sampling config file.
    sampling_conf = {
        "signalscanner": {
        "markout_tempo": tempo_rate["tempo_name"],
        "markout_thresh": markout_thresh,
        "sampling_tempo": tempo_rate["tempo_name"],
        "sampling_thresh": ticks_between_sample,
        "tgt_sig": sig_mid_name,
        "markout_cap": markout_cap,
        },
        "tempos": all_tempos,
        "signals": [sig_mid_dict],
        }

    # Write it in the multiclimb regress conf.
    with open(conf_path, "w") as f:
        f.write(json.dumps(sampling_conf, indent=2))


# What do we want.
# workdir
# symbol
# books
# date
# start
# end
# tempotype
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=str, required=True)
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--books", type=str, required=True)
    parser.add_argument("--dates", type=str, required=True)
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    parser.add_argument("--tempotype", type=str, required=True)
    args = parser.parse_args()

    books_list = args.books.split(",")
    dates_list = args.dates.split(",")

    # Run tickrate accordingly.
    ticks_dict = run_tickrate(args.symbol, books_list, dates_list, args.start, args.end, args.tempotype, args.workdir)
    print("ticks_dict: {}".format(ticks_dict))


if __name__ == '__main__':
    # Do this main stuff.
    main()
