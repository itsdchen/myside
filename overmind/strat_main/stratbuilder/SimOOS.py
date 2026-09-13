#! /usr/bin/env python


import os
import subprocess
import sys
import json
import argparse

import numpy as np

import pandas as pd



##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from util.chron import dates_list

from util.sim import sim_date_range
from util.pathing import bindir
import subprocess

pk_bin = os.path.join(bindir(), "pktrade")


# No real object.
def simoos(work_dir, src_conf, oos_dates, json_overrides, extra_args=""):

    with open(src_conf, "r") as f:
        src_dict = json.loads(f.read())

    # Apply overrides.

    if 'risk_settings' in json_overrides:
        # Don't override everything.

        risk_overrides = json_overrides['risk_settings']

        for k, v in risk_overrides.items():
            src_dict["pktraders"][0]['risk'][k] = v
    # Overwrote.
    # Write the new config file.
    pk_dest = os.path.join(work_dir, "pktrade_oos.json")

    with open(pk_dest, "w") as f:
        f.write(json.dumps(src_dict, indent=2))

    # OK. Now we can run the sims...

    stats = sim_date_range(pk_bin, work_dir, pk_dest, oos_dates, trd_suffix="0", full_output=True, extra_args=extra_args)

    return stats



def main():
    parser = argparse.ArgumentParser()

    parser.add_argument('--dir')
    parser.add_argument("-f",'--conf', required=True)
    parser.add_argument("-s","--start")
    parser.add_argument("-e","--end")
    parser.add_argument("--dates_method", type=str, default="WEEKDAYS")
    parser.add_argument("--extra-args", dest="extra_args", type=str, default="",
                        help="Extra args appended verbatim to each pktrade invocation "
                             "(e.g. '--slowdown-market TopBookEquity --slowdown-market-offset=5')")

    # If given, use this bin instead.
    args = parser.parse_args()

    workdir = os.path.join(os.path.dirname(args.conf), "simoos")
    if args.dir:
        workdir = args.dir

    if not os.path.exists(workdir):
        os.mkdir(workdir)


    dates = dates_list(args.start, args.end, dates_method=args.dates_method)

    sim_stats = simoos(workdir, args.conf, dates, {}, extra_args=args.extra_args)
    print("Done")
    print("Stats:")
    print(json.dumps(sim_stats, indent=2))


if __name__ == "__main__":
    main()
