#! /usr/bin/env python

"""

Given a conffile (eg. a trader),
Grab the model and run a new pnlclimb with it.

It skips all the other regression stuff and just gets on with the pnlclimb.

Command:
~/tradefi/pkt_0/overmind/strat_main/stratbuilder/TradeRefinery.py --conf modelbuild.json --pk pk_file.json

~/tradefi/pkt_0/overmind/strat_main/stratbuilder/TradeRefinery.py --super inherit.json --conf modelbuild.json --pk pk_file.json

Note that in the case of something like relativemm, we may define a relative_symbol and create premade signals based on that.

"""

import os
import json
import sys
import argparse
import glob

##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from templatizers import TempoFormer

from templatizers import SigFormer

from stratbuilder import (
    DailyStats,
    MagicLib,
    PnlClimb,
    SimOOS,
)

from util.chron import dates_list, dates_avail, blacklist
from util.pathing import bindir, datadir


sys.exit("Not supported right now, maybe later. Just putting it in to save state.")

status_dict = {
    # For sizing reasons
    "dailystats": {
        "done": False,
        "ins": {},
        "oos": {},
    },
    # Making this differently because then I can just run sed on a bunch of files
    # when rerunning pnlclimbs.
    "pnlclimb": {
        "pnlclimb_done": False,
        "golden_pk_conf": "",
        "ins_stats": {},
        "oos_stats": {},
    },
    # Can always run a simoos.
    # With every change in simoos, I can append a different row.
    # That way, I can keep track of experiments.
    "simoos": {"done": False, "oos_stats": []},
}


def read_status(cache_path):
    global status_dict
    with open(cache_path, "r") as f:
        status_dict = json.loads(f.read())


def write_status(cache_path, status_dict):
    print("Writing cache to {}".format(cache_path))
    with open(cache_path, "w") as f:
        f.write(json.dumps(status_dict, indent=2))


def training_pipeline(args):
    training_conf_path = args.conf

    #####################################################

    training_dict = {}
    if args.super and os.path.exists(args.super):
        with open(args.super, "r") as f:
            training_dict = json.loads(f.read())

    with open(training_conf_path, "r") as f:
        sub_training_dict = json.loads(f.read())

    # Override things in the super.
    for key, val in sub_training_dict.items():
        training_dict[key] = val

    # Check if workdir exists.
    work_dir = os.path.dirname(os.path.abspath(training_conf_path))
    # This will need to be passed to every binary.
    # data_dir = training_dict["data_dir"]

    sym = training_dict["traded_symbols"][0]
    mkts = training_dict["traded_markets"]

    start_d = training_dict["start_date"]
    end_d = training_dict["end_date"]

    start_d_oos = training_dict["start_date_oos"]
    end_d_oos = training_dict["end_date_oos"]

    dates_method = training_dict.get("dates_method", "ALLDAYS")

    # If individual sections don't have their own dates, use these.
    default_ins_dates = dates_avail(
        start_d, end_d, sym, mkts[0], dates_method=dates_method
    )["good_dates"]
    default_oos_dates = dates_avail(
        start_d_oos, end_d_oos, sym, mkts[0], dates_method=dates_method
    )["good_dates"]

    blacklist_dates = training_dict.get("blacklist_dates", [])
    # If it's a string, interpret it as a path.
    if isinstance(blacklist_dates, str):
        blacklist_path = blacklist_dates
        blacklist_dates = []
        with open(blacklist_path, "r") as f:
            blacklist_dates = f.read().split("\n")
    blacklist_dates = [a_date for a_date in blacklist_dates if a_date]

    default_ins_dates = blacklist(default_ins_dates, blacklist_dates)
    default_oos_dates = blacklist(default_oos_dates, blacklist_dates)

    # Write dates files out in the workdir.
    ins_dates_file = os.path.join(work_dir, "ins_dates.txt")
    oos_dates_file = os.path.join(work_dir, "oos_dates.txt")
    with open(ins_dates_file, "w") as f:
        f.write("\n".join(default_ins_dates) + "\n")
    with open(oos_dates_file, "w") as f:
        f.write("\n".join(default_oos_dates) + "\n")

    #####################################################
    # Step 2: Create workdir, basic subdirs
    # Read cache.
    cache_path = os.path.join(work_dir, "cache.json")
    if os.path.exists(cache_path):
        read_status(cache_path)

    if not os.path.exists(work_dir):
        os.mkdir(work_dir)

    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            cache_dict = json.loads(f.read())

    ds_done = False
    try:
        ds_done = status_dict["dailystats"]["done"]
    except Exception as e:
        pass

    if not ds_done:
        print("Running dailystats")
        daily_stats_ins = DailyStats.run_symstats(
            sym,
            mkts[0],
            default_ins_dates,
            # Start, end day points for getting volumes.
            "08:00:00 America/New_York",
            "18:00:00 America/New_York",
        )

        daily_stats_oos = DailyStats.run_symstats(
            sym,
            mkts[0],
            default_oos_dates,
            # Start, end day points for getting volumes.
            "08:00:00 America/New_York",
            "18:00:00 America/New_York",
        )

        # Save to cache.
        status_dict["dailystats"]["done"] = True
        status_dict["dailystats"]["ins"] = daily_stats_ins
        status_dict["dailystats"]["oos"] = daily_stats_oos
        write_status(cache_path, status_dict)

    # Go into the pk.conf, extract out the signals, and
    # grab their names.

    pred_sig_out = ""
    pred_sig_name = ""

    ref_sig_out = ""
    ref_sig_dct = {}
    ref_sig_name = ""

    if args.pk:
        with open(args.pk) as pk_f:
            pk_dct = json.loads(pk_f.read())

        # Assume there is one pktrader here.
        signal_dcts = pk_dct["pktraders"][0]["signals"]

        # Not necessarily correct but it's fine.
        # Last signal is always just the ref signal
        pred_sig_dcts = {"signals": signal_dcts[:-1]}
        ref_sig_dct = {"signals": signal_dcts[-1:]}

        # Write these files out.
        pred_sig_out = os.path.join(work_dir, "pred_sig.json")
        ref_sig_out = os.path.join(work_dir, "ref_sig.json")

        with open(pred_sig_out, "w") as f:
            f.write(json.dumps(pred_sig_dcts, indent=2))

        with open(ref_sig_out, "w") as f:
            f.write(json.dumps(ref_sig_dct, indent=2))

        pred_sig_name = pk_dct["pktraders"][0]["ordex"]["pred_sig"]
        try:
            ref_sig_name = pk_dct["pktraders"][0]["ordex"]["ref_sig"]
        except Exception as e:
            # Might be a remote thing.
            print("Couldn't find ref_sig, probably a relative ordex in this case. ")
            pass

    # Otherwise, still construct the ref signal based on the traded (symbol, book)
    # ref_sig is the local sig.
    if ref_sig_out == "":
        ref_sig = SigFormer.SigMidSpec(
            "ref_{}".format(sym),
            symbol=sym,
            markets=mkts,
            fixed_val_overrides={},
            climbables={},
        )

        ref_sig_stuff = ref_sig.apply_varied_params({})
        ref_sig_name = ref_sig_stuff["name"]
        ref_sig_dct = ref_sig_stuff["signals_dict"]

        ref_sig_out = os.path.join(work_dir, "ref_sig.json")

        with open(ref_sig_out, "w") as f:
            f.write(json.dumps(ref_sig_dct, indent=2))

    # Also handle if we have a remote sig.
    # This is for stuff, I guess, that want sto do relative positioning.

    remote_sig_dct = {}
    remote_sig_name = ""
    if "remote_product" in training_dict:

        remote_sym = training_dict["remote_product"]["symbol"]
        remote_markets = training_dict["remote_product"]["markets"]

        local_use_liqbal = training_dict["pnlclimb_specs"].get(
            "local_use_liqbal", False
        )

        if local_use_liqbal:
            liqbal_sz_decay = training_dict["pnlclimb_specs"].get(
                "liqbal_sz_decay", 5000
            )
            liqbal_levels_deep = training_dict["pnlclimb_specs"].get(
                "liqbal_levels_deep", 5
            )
            # Consider changing this to "Size"
            liqbal_decay_type = training_dict["pnlclimb_specs"].get(
                "liqbal_decay_type", "EXP_NOTIONAL"
            )

            liq_bal_overrides = {
                "top_level_signal": {
                    "calc_style": "DEEPWP1",
                    "sz_decay_type": liqbal_decay_type,
                    "levels_deep": liqbal_levels_deep,
                    "sz_decay": liqbal_sz_decay,
                    "per_level_decay": 0.95,
                    "min_sz_wt": 1e-12,
                    "return_price": True,
                }
            }

            local_sig = SigFormer.SigLiqBalSpec(
                "local_ref_{}".format(remote_sym),
                symbol=sym,
                markets=mkts,
                fixed_val_overrides=liq_bal_overrides,
                climbables={"top_level_signal": {}},
            )
        else:
            # Also set up the local sig.
            local_sig = SigFormer.SigMidSpec(
                "local_ref_{}".format(remote_sym),
                symbol=sym,
                markets=mkts,
                fixed_val_overrides={},
                climbables={},
            )

        # Write local_sig out to the ref_sig places.

        ref_sig_stuff = local_sig.apply_varied_params({})
        ref_sig_name = ref_sig_stuff["name"]
        ref_sig_dct = ref_sig_stuff["signals_dict"]

        ref_sig_out = os.path.join(work_dir, "ref_sig.json")

        with open(ref_sig_out, "w") as f:
            f.write(json.dumps(ref_sig_dct, indent=2))

        # This is the basic version.
        remote_sig = SigFormer.SigMidSpec(
            "ref_{}".format(remote_sym),
            symbol=remote_sym,
            markets=remote_markets,
            fixed_val_overrides={},
            climbables={},
        )
        remote_sig_stuff = remote_sig.apply_varied_params({})
        remote_sig_name = remote_sig_stuff["name"]
        remote_sig_dct = remote_sig_stuff["signals_dict"]

    # Also, run the

    # Regress oos.
    # Run pnlclimb
    if not status_dict["pnlclimb"]["pnlclimb_done"]:
        # This lets us do multiple pnlclimbs in the same dir.
        dir_name = training_dict["pnlclimb_specs"].get("dir_name", "pnlclimb")

        pnl_ins_dates = default_ins_dates
        pnl_oos_dates = default_oos_dates

        pnlclimb_cache_dir = os.path.join(work_dir, "pnlclimb_cache")
        if not os.path.exists(pnlclimb_cache_dir):
            os.mkdir(pnlclimb_cache_dir)

        pnl_dir = os.path.join(work_dir, dir_name)
        if not os.path.exists(pnl_dir):
            os.mkdir(pnl_dir)

        # Give the option of grabbing the preexisting ordex specs for pnlclimb.
        existing_ordex_overrides = {}
        if training_dict["pnlclimb_specs"].get("reinit_from_pk", False):
            # Then go through the pk file and grab ordex specs.
            with open(args.pk) as pk_f:
                pk_dct = json.loads(pk_f.read())
            ordex_sec = pk_dct["pktraders"][0]["ordex"]
            for key, val in ordex_sec.items():
                if key not in ["type", "markets", "print_ords", "getflat_pct_away", "pred_sig", "ref_sig", "trade_caller", "max_pos", "order_size"]:
                    existing_ordex_overrides[key] = val


        pclimber = PnlClimb.PnlClimber(
            pnl_dir,
            training_dict,

            pred_sig_out,
            pred_sig_name,

            # It's the local sig fyi.
            ref_sig_out,
            ref_sig_dct,
            ref_sig_name,

            pnl_ins_dates,
            pnl_oos_dates,
            pnlclimb_cache_dir,
            daily_stats_dct=status_dict["dailystats"],

            remote_sig_dct=remote_sig_dct,
            remote_sig_name=remote_sig_name,
            existing_ordex_overrides=existing_ordex_overrides
        )
        pkt_file = os.path.join(pnl_dir, "final_pk.json")
        pkt_file = pclimber.pnl_climb()

        status_dict["pnlclimb"]["pnlclimb_done"] = True

        sim_final = training_dict["pnlclimb_specs"].get("sim_final", True)

        if sim_final:
            ins_stats = pclimber.sim_ins(pnl_dir, pkt_file, "ins")
            oos_stats = pclimber.sim_oos(pnl_dir, pkt_file, "oos")
            status_dict["pnlclimb"]["ins_stats"] = ins_stats
            status_dict["pnlclimb"]["oos_stats"] = oos_stats
            write_status(cache_path, status_dict)

            # Also write this into final_pk.
            with open(pkt_file, "r") as f:
                pkt_dict = json.loads(f.read())

                pkt_dict["ins_stats"] = ins_stats
                pkt_dict["oos_stats"] = oos_stats
            with open(pkt_file, "w") as f:
                f.write(json.dumps(pkt_dict, indent=2))

        status_dict["pnlclimb"]["golden_pk_conf"] = pkt_file
        write_status(cache_path, status_dict)


def main():
    parser = argparse.ArgumentParser()
    # If we have a superclass json that stuff inherits from. Makes doing experiments across multiple symbols, easier.
    parser.add_argument("--super", type=str)
    parser.add_argument("--conf", type=str, required=True)
    parser.add_argument("--pk", type=str)
    # I think newer python can deal with this properly. But still.

    args = parser.parse_args()

    training_pipeline(args)

    pass


if __name__ == "__main__":
    main()
