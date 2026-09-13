#! /usr/bin/env python

"""
End-to-end trainer for a trading strategy.

I'll test this one at a time but... yeah.


Command:
~/tradefi/pkt_0/overmind/strat_main/stratbuilder/TradeAcademy.py --conf build_one_sym.json --force_restart

"""

import os
import json
import glob
import sys
import argparse
import shutil


##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from templatizers import SigFormer, TempoFormer
from stratbuilder import (
    DailyStats,
    TempoRate,
    ModelClimb,
    CoefHatchery,
    CoefLair,
    MagicLib,
    PnlClimb,
    SimOOS,
    Regressor,
)

from tools import md_exists

from util.chron import dates_list, dates_avail, blacklist
from util.pathing import bindir, inheritdir, datadir


# For a single tradeacademy, we want to keep a cache for intermediate results.
# Stuff to tell us our training status and where we'll get.
status_dict = {
    "dailystats": {
        "done": False,
        "ins": {},
        "oos": {},
    },
    "tickrate": {
        "done": False,
        "ins_sampling_ticks_dict": {},
        "ins_markout_ticks_dict": {},
        "ins_inside_ticks_dict": {},
        "ins_fit_tgt_dict": {},
        "oos_markout_ticks_dict": {},
        "oos_inside_ticks_dict": {},
        "oos_fit_tgt_dict": {},
        "ticks_between_sample": 1,
        "mkout_thresh_dct": 1,
    },
    "multiclimb": {
        "done": False,
        "sample_and_returns_done": False,
        "inherited_scribe_done": False,
        "scribe_path": "",
        "inherited_sigval_paths": [],
        "inherited_sig_names": [],
        "inherited_sigs_path": "",
        "inherited_sig_params_path": "",
        "last_completed_round": -1,
        "last_reg_score": 0,
        "golden_sig_names": [],
        "golden_sigs_to_vals": {},
        "golden_sigs_scores": [],
        "golden_sig_paths": [],
        "golden_sig_params": [],
    },
    "final_fit": {
        "done": False,
        "pred_path": "",
        "pred_name": "",
        "ref_path": "",
        "ref_name": "",
    },
    "oos_r2": {
        "done": False,
        "oos_r2": 0,
    },
    "pnlclimb": {"pnlclimb_done": False, "golden_pk_conf": "", "ins_stats": {}, "oos_stats": {}},
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


# Returns:
# {(sym, market)} : [list of dates]
def get_wanted_datas(conf_path):
    conf_dct = {}
    with open(conf_path, "r") as f:
        try:
            conf_dct = json.loads(f.read())
        except Exception as e:
            print("Had a problem parsing {}".format(conf_path))

    # Given a TA file, read in what symbols we want and the superset of all dates we want.
    # Returns a dict:
    # (symbol, book) -> [dates]

    sym_book_pairs = set()

    
    # Add in traded data.
    for traded_sym in conf_dct["traded_symbols"]:
        for traded_mkt in conf_dct["traded_markets"]:
            # Substitute the symbol appropriately
            sym_str = traded_sym.replace("/", "_")
            sym_book_pairs.add((sym_str, traded_mkt))

    # Add in side products.
    side_prods = MagicLib.get_indeps(traded_sym, traded_mkt, conf_dct.get("side_products", []))

    # Perhaps also add in remote_refs.
    if "remote_product" in conf_dct:
        sym_book_pairs.add((conf_dct["remote_product"]["symbol"], conf_dct["remote_product"]["markets"][0]))

    for sp_spec in side_prods:
        for one_mkt in sp_spec["markets"]:

            sym_str = sp_spec["symbol"].replace("/", "_")
            sym_book_pairs.add((sym_str, one_mkt))

    dates_method = conf_dct.get("dates_method", "ALLDAYS")

    # OK. Now let's go through all the dates.

    all_dates = []

    # default start, end.
    default_dr = dates_list(
        conf_dct["start_date"], conf_dct["end_date"], dates_method=dates_method
    )
    all_dates += default_dr

    # oos
    oos_dr = dates_list(conf_dct["start_date_oos"], conf_dct["end_date_oos"])
    all_dates += oos_dr

    # Also consider modelclimb and hatch, if they want
    # a custom daterange.
    if "modelclimb_specs" in conf_dct and "start_date" in conf_dct["modelclimb_specs"]:
        mc_custom_dr = dates_list(
            conf_dct["modelclimb_specs"]["start_date"],
            conf_dct["modelclimb_specs"]["end_date"],
            dates_method=dates_method,
        )
        all_dates += mc_custom_dr

    # Also blacklist.
    blacklist_dates = conf_dct.get("blacklist_dates", [])
    # If it's a string, interpret it as a path.
    if isinstance(blacklist_dates, str):
        blacklist_path = blacklist_dates
        blacklist_dates = []
        with open(blacklist_path, "r") as f:
            blacklist_dates = f.read().split("\n")
    blacklist_dates = [a_date for a_date in blacklist_dates if a_date]

    all_dates = blacklist(all_dates, blacklist_dates)


    all_dates = list(set(all_dates))

    all_dates.sort()

    to_ret = {}

    # OK, now let's make the full thing.
    for one_sb_pair in sym_book_pairs:
        to_ret[one_sb_pair] = all_dates

    return to_ret


# force_redos: a list of pipeline segments to force-redo.
def training_pipeline(args):
    training_conf_path = args.conf

    #####################################################
    # Step 1: read in potential cache, if it exists.
    with open(training_conf_path, "r") as f:
        training_dict = json.loads(f.read())

    # Check if workdir exists.
    work_dir = os.path.dirname(os.path.abspath(training_conf_path))

    # This will need to be passed to every binary.
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


    sym_book_pairs = set()

    # Add in traded data.
    for traded_sym in training_dict["traded_symbols"]:
        for traded_mkt in training_dict["traded_markets"]:
            sym_book_pairs.add((traded_sym, traded_mkt))

    # Add in side products.
    side_prods = MagicLib.get_indeps(traded_sym, traded_mkt, training_dict["side_products"])

    for sp_spec in side_prods:
        for one_mkt in sp_spec["markets"]:
            sym_book_pairs.add((sp_spec["symbol"], one_mkt))

    # Check whether data exists.
    syms_books_to_dates = get_wanted_datas(training_conf_path)
    missing_data_files = md_exists.missing_data_files(syms_books_to_dates, datadir())
    if len(missing_data_files["missing"]) > 0:
        missing = missing_data_files["missing"]
        print("Missing files: {}".format(missing))

        # Maybe for this thing, you want to actually remove these symbols
        # from the indeps.
        missing_sps = []
        print(side_prods)

        for one_missing_file in missing:

            missing_mkt = one_missing_file.split("/")[0]
            missing_sym = one_missing_file.split("/")[1].split('_')[0].upper()
            missing_sps.append((missing_sym, missing_mkt))
        missing_sps = list(set(missing_sps))

        new_sps = []
        passes = True
        for one_sp in side_prods:
            for one_missing_pair in missing_sps:
                if one_sp["symbol"] == one_missing_pair[0] and one_sp["markets"][0] == one_missing_pair[1]:
                    passes = False
            if passes:
                new_sps.append(one_sp)
            else:
                print("Removing {} from side_products because data is missing".format(one_sp))

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

    ####################################################################
    # Step 2.5. Run dailystats.
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
            training_dict["day_start_regress"],
            training_dict["day_end_regress"],
        )

        daily_stats_oos = DailyStats.run_symstats(
            sym,
            mkts[0],
            default_oos_dates,
            training_dict["day_start_regress"],
            training_dict["day_end_regress"],
        )

        # Save to cache.
        status_dict["dailystats"]["done"] = True
        status_dict["dailystats"]["ins"] = daily_stats_ins
        status_dict["dailystats"]["oos"] = daily_stats_oos
        write_status(cache_path, status_dict)

    ####################################################################
    # Step 3: Run temporate.

    # Tickrate is definitely run on the default_dates.
    if not status_dict["tickrate"]["done"]:
        print("Running TickRate")
        tickrate_dir = os.path.join(work_dir, "tickrate")

        # All these objects are List of sampling tempos
        # TODO: include support for multi-tempo mixtures.
        # Right now, everythig is fixed at 1.
        sampling_tempo_specs = training_dict["timer_specs"]["sampling_tempos"][0]
        markout_tempo_specs = training_dict["timer_specs"]["markout_tempos"][0]

        ins_tempo_stats = MagicLib.gen_sampling_specs(
            sym,
            mkts,
            default_ins_dates,
            training_dict["day_start_regress"],
            training_dict["day_end_regress"],
            sampling_tempo_specs,
            markout_tempo_specs,
            tickrate_dir,
            training_dict["fit_specs"],
        )

        ticks_between_sample = (
            ins_tempo_stats["sampling_ticks_dict"]["tot_ticks"]
            / training_dict["final_fit"]["num_samples"]
        )

        # TODO: encode this somehow.
        use_daily_markout_rate = training_dict["fit_specs"].get(
            "use_daily_markout_rate", False
        )

        ins_mkout_thresh_dct = MagicLib.markout_thresh(
            ins_tempo_stats["fit_tgt_dict"],
            ins_tempo_stats["markout_ticks_dict"],
            use_daily_markout_rate,
        )

        oos_tempo_stats = MagicLib.gen_sampling_specs(
            sym,
            mkts,
            default_oos_dates,
            training_dict["day_start_regress"],
            training_dict["day_end_regress"],
            sampling_tempo_specs,
            markout_tempo_specs,
            tickrate_dir,
            training_dict["fit_specs"],
        )

        oos_mkout_thresh_dct = MagicLib.markout_thresh(
            oos_tempo_stats["fit_tgt_dict"],
            oos_tempo_stats["markout_ticks_dict"],
            use_daily_markout_rate,
        )

        # Save these values to cache.
        status_dict["tickrate"]["done"] = True
        status_dict["tickrate"]["ins_sampling_ticks_dict"] = ins_tempo_stats[
            "sampling_ticks_dict"
        ]
        status_dict["tickrate"]["ins_markout_ticks_dict"] = ins_tempo_stats[
            "markout_ticks_dict"
        ]
        status_dict["tickrate"]["ins_inside_ticks_dict"] = ins_tempo_stats[
            "inside_ticks_dict"
        ]
        status_dict["tickrate"]["ins_fit_tgt_dict"] = ins_tempo_stats["fit_tgt_dict"]

        status_dict["tickrate"]["oos_markout_ticks_dict"] = oos_tempo_stats[
            "markout_ticks_dict"
        ]
        status_dict["tickrate"]["oos_inside_ticks_dict"] = oos_tempo_stats[
            "inside_ticks_dict"
        ]
        status_dict["tickrate"]["oos_fit_tgt_dict"] = oos_tempo_stats["fit_tgt_dict"]

        # This is saved
        status_dict["tickrate"]["ticks_between_sample"] = ticks_between_sample

        # This might need to be different.
        status_dict["tickrate"]["ins_mkout_thresh_dct"] = ins_mkout_thresh_dct
        status_dict["tickrate"]["oos_mkout_thresh_dct"] = oos_mkout_thresh_dct

        write_status(cache_path, status_dict)
    else:
        print("Skipping tickrate because it completed. Reading from cache.")

    markout_cap = training_dict["final_fit"].get("markout_cap", 0.0035)

    sampling_dir = os.path.join(work_dir, "sampling_confs")
    ins_sampling_confs = MagicLib.gen_sampling_confs(
        sym,
        mkts,
        default_ins_dates,
        status_dict["tickrate"]["ins_sampling_ticks_dict"],
        status_dict["tickrate"]["ins_markout_ticks_dict"],
        status_dict["tickrate"]["ticks_between_sample"],
        status_dict["tickrate"]["ins_mkout_thresh_dct"],
        sampling_dir,
        markout_cap,
        overwrite=False,
        old_style_rets=training_dict["fit_specs"].get("old_style_rets", False)
    )

    oos_sampling_confs = MagicLib.gen_sampling_confs(
        sym,
        mkts,
        default_oos_dates,
        status_dict["tickrate"]["ins_sampling_ticks_dict"],
        status_dict["tickrate"]["oos_markout_ticks_dict"],
        status_dict["tickrate"]["ticks_between_sample"],
        status_dict["tickrate"]["oos_mkout_thresh_dct"],
        sampling_dir,
        markout_cap,
        overwrite=False,
        old_style_rets=training_dict["fit_specs"].get("old_style_rets", False)
    )

    if args.run_until and args.run_until == "tickrate":
        print("tickrate completed, exiting")
        return

    ####################################################################
    # Step 4. Start multiclimb returns running.
    # OK, now let's create the dirs.

    if not status_dict["multiclimb"]["done"]:
        multiclimb_work_dir = os.path.join(work_dir, "multiclimb")
        if not os.path.exists(multiclimb_work_dir):
            os.mkdir(multiclimb_work_dir)

        # Yes, note that this is a list of dicts.
        inherit_specs = training_dict["modelclimb_specs"]["inherit"]
        inherited_sigs = MagicLib.get_inherited_sigs(
            sym, mkts, side_prods, inherit_specs
        )

        # Otherwise, potentially run a lair 0 to fetch inherited signals too.
        if "lair_0" in training_dict and len(inherited_sigs) > 0:
            lair_all_results = CoefLair.scribe_and_hatch_full_model(
                work_dir,
                default_ins_dates,
                training_dict["day_start_regress"],
                training_dict["day_end_regress"],
                inherited_sigs,
                sym,
                mkts,
                ins_sampling_confs,
                num_wanted_signals=training_dict["lair_0"].get(
                    "num_wanted_signals", 60
                ),
                reg_style=training_dict["lair_0"].get("reg_style", "fwd"),
                reg_alpha=training_dict["lair_0"].get("reg_alpha", 1e-6),
                use_post_ols_coef=training_dict["lair_0"].get(
                    "use_post_ols_coef", False
                ),
                job_name="lair_0",
            )
            lair_out = lair_all_results["lair_out"]
            # Add the params to the inherited sigs list.
            inherited_sigs = lair_out["chosen_sig_params"]

        mclimber = ModelClimb.MultiClimber(
            multiclimb_work_dir,
            training_dict,
            side_prods,
            ins_sampling_confs,
            default_ins_dates,
            blacklist_dates=blacklist_dates,
        )

        # This sets the baseline for golden values.
        if len(inherited_sigs) > 0:
            mclimber.set_inherited_params(inherited_sigs)

        if status_dict["multiclimb"]["sample_and_returns_done"]:
            print("Multiclimb sampling/returns done, skipping!")
        else:
            mclimber.sample_and_returns()
            status_dict["multiclimb"]["sample_and_returns_done"] = True
            # Save
            write_status(cache_path, status_dict)

        # Potentially run the inherited values.
        if status_dict["multiclimb"]["inherited_scribe_done"]:
            print("Multiclimb inherited scribe done, skipping!")
            # Pick this up from cache.
            mclimber.inherited_sigval_paths = status_dict["multiclimb"][
                "inherited_sigval_paths"
            ]
            mclimber.inherited_sig_names = status_dict["multiclimb"][
                "inherited_sig_names"
            ]
            mclimber.inherited_sigs_path = status_dict["multiclimb"][
                "inherited_sigs_path"
            ]
        else:
            mclimber.scribe_inherited_values()
            status_dict["multiclimb"]["inherited_scribe_done"] = True
            # Also save the paths.
            status_dict["multiclimb"][
                "inherited_sigval_paths"
            ] = mclimber.inherited_sigval_paths
            status_dict["multiclimb"][
                "inherited_sig_names"
            ] = mclimber.inherited_sig_names
            status_dict["multiclimb"][
                "inherited_sigs_path"
            ] = mclimber.inherited_sigs_path
            status_dict["multiclimb"][
                "inherited_sig_params_path"
            ] = mclimber.inherited_sig_params_path

            write_status(cache_path, status_dict)

        print("Done!")

        # Next, let's run multiclimb....

        last_completed_round = -1
        if status_dict["multiclimb"]["last_completed_round"] is not None:
            last_completed_round = status_dict["multiclimb"]["last_completed_round"]

            # Cache the stuff we chose I guess.
            # In the future, can cache some of the other stuff too.
            # Like, it would be helpful to write the climb log in each
            # signal's climb dir. You know?
            golden_sig_names = status_dict["multiclimb"]["golden_sig_names"]
            golden_sigs_to_vals = status_dict["multiclimb"]["golden_sigs_to_vals"]
            # Go through these and sort them.
            for one_sig_name in golden_sigs_to_vals:
                golden_sigs_to_vals[one_sig_name].sort()

            golden_sigs_scores = status_dict["multiclimb"]["golden_sigs_scores"]
            golden_sig_paths = status_dict["multiclimb"]["golden_sig_paths"]

            # This needs to be a list. Because the same thing can be chosen multiple times.
            if "golden_sig_params" in status_dict["multiclimb"]:
                golden_sig_params = status_dict["multiclimb"]["golden_sig_params"]
            else:
                golden_sig_params = []

            # Also store the multiclimb's chosen signals.
            # Potentially do some

            mclimber.set_last_completed_round(last_completed_round)
            mclimber.set_golden(
                golden_sig_names,
                golden_sigs_to_vals,
                golden_sigs_scores,
                golden_sig_paths,
                golden_sig_params,
            )

            # Read in the previously climbed bestParams for each signal type.
            # So we can reinit if needed.
            mclimber.load_cached_sig_params()

        # Safe bailout.
        while (
            not mclimber.done
            and mclimber.get_last_completed_round() < mclimber.num_rounds - 1
        ):
            print(
                "Multiclimb: Running round {}".format(mclimber.last_completed_round + 1)
            )
            mclimber.one_round_multiclimb()
            print("Finished round {}".format(mclimber.get_last_completed_round))

            # Update cache.
            status_dict["multiclimb"][
                "last_completed_round"
            ] = mclimber.get_last_completed_round()

            status_dict["multiclimb"][
                "last_reg_score"
            ] = mclimber.get_best_score_so_far()

            status_dict["multiclimb"]["golden_sig_names"] = mclimber.get_golden()[
                "sig_names"
            ]
            status_dict["multiclimb"]["golden_sigs_to_vals"] = mclimber.get_golden()[
                "sigs_to_vals"
            ]
            status_dict["multiclimb"]["golden_sigs_scores"] = mclimber.get_golden()[
                "sigs_scores"
            ]
            status_dict["multiclimb"]["golden_sig_paths"] = mclimber.get_golden()[
                "sig_paths"
            ]
            status_dict["multiclimb"]["golden_sig_params"] = mclimber.get_golden()[
                "sig_params"
            ]

            status_dict["multiclimb"][
                "last_completed_round"
            ] = mclimber.get_last_completed_round()

            write_status(cache_path, status_dict)

        # Save the climbed golden params to a file too.
        golden_export_path = os.path.join(work_dir, "golden_sig_params.json")
        # Augment the golden params with some info re:
        golden_climbs_dct = status_dict["multiclimb"]["golden_sig_params"]
        # This messes up parsing, sorry. I'll fix this later TODO
        #golden_climbs_dct["stats"] = {
        #    "train_dates": mclimber.dates_list,
        #    "score": mclimber.best_score_so_far,
        #}
        with open(golden_export_path, "w") as f:
            f.write(
                json.dumps(golden_climbs_dct, indent=2)
            )

        # Save golden param locally too.
        repo_save_dir = os.path.join(inheritdir(), mkts[0], sym)
        if not os.path.exists(repo_save_dir):
            os.makedirs(repo_save_dir)
        repo_tgt = os.path.join(
            repo_save_dir, "inherit_{}_{}.json".format(start_d, end_d)
        )
        shutil.copyfile(golden_export_path, repo_tgt)

        # If I'm done, we're done.
        # Don't do this while I'm testing, fool.
        # Update cache
        status_dict["multiclimb"]["done"] = True
        write_status(cache_path, status_dict)

    if args.run_until and args.run_until == "multiclimb":
        print("multiclimb completed, exiting")
        return

    # Finally, reregress and write the final linear model.
    # let's call it, idk, coefHatchery.
    # Potentially run the hatchery.
    if not status_dict["final_fit"]["done"]:
        # Potentially do hatch or lair. Just depends.
        if training_dict["final_fit"]["type"] == "hatch":
            # Do the hatch step.

            rets_dir = os.path.join(work_dir, "multiclimb", "rets")
            hatch_results = CoefHatchery.regress_final_model(
                work_dir,
                rets_dir,
                default_ins_dates,
                status_dict["multiclimb"]["golden_sig_names"],
                status_dict["multiclimb"]["golden_sig_paths"],
                status_dict["multiclimb"]["golden_sigs_to_vals"],
                sym,
                mkts,
                training_dict["final_fit"].get("fit_intercept"),
                training_dict["final_fit"].get("keep_intercept"),
                training_dict["final_fit"].get("sd_bound", 80),
                status_dict["multiclimb"]["inherited_sigval_paths"],
                status_dict["multiclimb"]["inherited_sig_names"],
                status_dict["multiclimb"]["inherited_sigs_path"],
            )
            # Update status.
            status_dict["final_fit"]["pred_path"] = hatch_results["pred_path"]
            status_dict["final_fit"]["pred_name"] = hatch_results["pred_name"]
            status_dict["final_fit"]["ref_path"] = hatch_results["ref_path"]
            status_dict["final_fit"]["ref_name"] = hatch_results["ref_name"]
            status_dict["final_fit"]["stats"] = hatch_results["stats"]
            status_dict["final_fit"]["done"] = True
            write_status(cache_path, status_dict)
        elif training_dict["final_fit"]["type"] == "lair":
            # Do the same thing as a normal coef lair.

            # Stuff we inherited
            inherited_params_lst = []
            if os.path.exists(status_dict["multiclimb"]["inherited_sig_params_path"]):
                with open(
                    status_dict["multiclimb"]["inherited_sig_params_path"], "r"
                ) as f:
                    inherited_params_lst = json.loads(f.read())
            # stuff we climbed
            # print(inherited_params_lst)

            mc_sigs = status_dict["multiclimb"]["golden_sig_params"]
            all_sig_params = inherited_params_lst + mc_sigs

            # Potentially have their own dates.
            fit_dates_list = default_ins_dates
            if "start_date" in training_dict["final_fit"]:
                start_d = training_dict["final_fit"]["start_date"]
                end_d = training_dict["final_fit"]["end_date"]
                fit_dates_list = dates_avail(
                    start_d, end_d, sym, mkts[0], dates_method=dates_method
                )["good_dates"]
                fit_dates_list = blacklist(fit_dates_list, blacklist_dates)

            # Instantiate the lair.
            lair_all_results = CoefLair.scribe_and_hatch_full_model(
                work_dir,
                fit_dates_list,
                training_dict["day_start_regress"],
                training_dict["day_end_regress"],
                all_sig_params,
                sym,
                mkts,
                ins_sampling_confs,
                training_dict["final_fit"]["num_wanted_signals"],
                reg_style=training_dict["final_fit"].get("reg_style", "fwd"),
                reg_alpha=training_dict["final_fit"].get("reg_alpha", 1e-6),
                use_post_ols_coef=training_dict["final_fit"].get(
                    "use_post_ols_coef", False
                ),
            )
            lair_out = lair_all_results["lair_out"]

            # Update status.
            status_dict["final_fit"]["pred_path"] = lair_out["pred_path"]
            status_dict["final_fit"]["pred_name"] = lair_out["pred_name"]
            status_dict["final_fit"]["ref_path"] = lair_out["ref_path"]
            status_dict["final_fit"]["ref_name"] = lair_out["ref_name"]
            status_dict["final_fit"]["stats"] = lair_out["stats"]
            status_dict["final_fit"]["done"] = True
            write_status(cache_path, status_dict)

    if args.run_until and args.run_until == "final_fit":
        print("final fit completed, exiting")
        return

    # Regress oos.
    if not status_dict["oos_r2"]["done"]:
        # Potentially do hatch or lair. Just depends.

        # Do an oos fit.
        regress_dir = os.path.join(work_dir, "oos_r2")

        regress_score = Regressor.scribe_and_regress(
            status_dict["final_fit"]["pred_path"],
            status_dict["final_fit"]["pred_name"],
            regress_dir,
            oos_sampling_confs,
            default_oos_dates,
            day_start_regress=training_dict["day_start_regress"],
            day_end_regress=training_dict["day_end_regress"],
        )
        status_dict["oos_r2"]["done"] = True
        status_dict["oos_r2"]["oos_r2"] = regress_score["score"]
        write_status(cache_path, status_dict)

    # Step 5: Run pnlclimb.
    if not status_dict["pnlclimb"]["pnlclimb_done"]:

        pnlclimb_cache_dir = os.path.join(work_dir, "pnlclimb_cache")
        if not os.path.exists(pnlclimb_cache_dir):
            os.mkdir(pnlclimb_cache_dir)

        pnl_dir = os.path.join(work_dir, "pnlclimb")
        if not os.path.exists(pnl_dir):
            os.mkdir(pnl_dir)

        pnl_ins_dates = default_ins_dates
        pnl_oos_dates = default_oos_dates


        # Potentially set our own oos dates.
        if (
            "start_date" in training_dict["pnlclimb_specs"]
            and "end_date" in training_dict["pnlclimb_specs"]
        ):
            pnl_ins_dates = dates_avail(
                training_dict["pnlclimb_specs"]["start_date"],
                training_dict["pnlclimb_specs"]["end_date"],
                sym,
                mkts[0],
                dates_method=dates_method,
            )["good_dates"]
            pnl_oos_dates = dates_avail(
                start_d_oos, end_d_oos, sym, mkts[0], dates_method=dates_method
            )["good_dates"]

            pnl_ins_dates = blacklist(
                pnl_ins_dates, blacklist_dates
            )
            pnl_oos_dates = blacklist(
                pnl_oos_dates, blacklist_dates
            )
        # Potentially truncate numdays
        if "numdays" in training_dict["pnlclimb_specs"]:
            truncate_numdays = training_dict["pnlclimb_specs"]["numdays"]
            pnl_ins_dates = pnl_ins_dates[-truncate_numdays:]

        pclimber = PnlClimb.PnlClimber(
            pnl_dir,
            training_dict,
            status_dict["final_fit"]["pred_path"],
            status_dict["final_fit"]["pred_name"],
            status_dict["final_fit"]["ref_path"],
            # ref_dct, really used by the refinery but not here.
            {},
            status_dict["final_fit"]["ref_name"],
            pnl_ins_dates,
            pnl_oos_dates,
            pnlclimb_cache_dir,
            daily_stats_dct=status_dict["dailystats"]
        )

        pkt_file = pclimber.pnl_climb()

        status_dict["pnlclimb"]["pnlclimb_done"] = True
        ins_stats = pclimber.sim_ins(pnl_dir, pkt_file, "ins")
        oos_stats = pclimber.sim_oos(pnl_dir, pkt_file, "oos")
        status_dict["pnlclimb"]["golden_pk_conf"] = pkt_file
        status_dict["pnlclimb"]["ins_stats"] = ins_stats
        status_dict["pnlclimb"]["oos_stats"] = oos_stats
        write_status(cache_path, status_dict)

    if args.run_until and args.run_until == "pnlclimb":
        print("pnlclimb completed, exiting")
        return

    # Do simoos stuff.
    if not status_dict["simoos"]["done"] and "simoos_specs" in training_dict:
        oos_dir = os.path.join(work_dir, "simoos")
        if not os.path.exists(oos_dir):
            os.mkdir(oos_dir)

        oos_stats = SimOOS.simoos(
            oos_dir,
            status_dict["pnlclimb"]["golden_pk_conf"],
            default_oos_dates,
            training_dict["simoos_specs"],
        )

        # Write these stats.
        status_dict["simoos"]["oos_stats"].append(
            {"overrides": training_dict["simoos_specs"], "stats": oos_stats}
        )
        status_dict["simoos"]["done"] = True
        write_status(cache_path, status_dict)
        # Run some stuff oos.

    print("Done!")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--conf", type=str, required=True)
    # I think newer python can deal with this properly. But still.
    parser.add_argument("--force_restart", action="store_true")

    # Values:
    # tickrate
    # multiclimb
    # coefhatch
    # pnlclimb

    parser.add_argument("--run_until", type=str)

    args = parser.parse_args()

    training_pipeline(args)

    pass


if __name__ == "__main__":
    main()
