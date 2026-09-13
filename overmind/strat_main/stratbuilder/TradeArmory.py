#! /usr/bin/env python

"""
Just for cleanliness, separating this from TradeAcademy. But it's just a 2-stage pipeline for
running CoefLair and then Pnlclimb


Command:
~/tradefi/pkt_0/overmind/strat_main/stratbuilder/TradeArmory.py --conf regress_one_sym.json

~/tradefi/pkt_0/overmind/strat_main/stratbuilder/TradeArmory.py  --super inherit.json --conf regress_one_sym.json



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

from tools import md_exists

from templatizers import SigFormer, TempoFormer

from stratbuilder import (
    DailyStats,
    TempoRate,
    CoefLair,
    MagicLib,
    PnlClimb,
    SimOOS,
    Regressor,
    TradeAcademy,
)

from util.chron import dates_list, dates_avail, blacklist
from util.pathing import bindir, datadir


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

    training_dict = {}
    if args.super and os.path.exists(args.super):
        with open(args.super, "r") as f:
            training_dict = json.loads(f.read())

    #####################################################
    # Step 1: read in potential cache, if it exists.
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

    sym_book_pairs = set()

    # Add in traded data.
    for traded_sym in training_dict["traded_symbols"]:
        for traded_mkt in training_dict["traded_markets"]:
            sym_book_pairs.add((traded_sym, traded_mkt))

    # Add in side products.
    side_prods = MagicLib.get_indeps(
        traded_sym, traded_mkt, training_dict["side_products"]
    )

    for sp_spec in side_prods:
        for one_mkt in sp_spec["markets"]:
            sym_book_pairs.add((sp_spec["symbol"], one_mkt))

    # Check whether data exists.
    syms_books_to_dates = TradeAcademy.get_wanted_datas(training_conf_path)
    missing_data_files = md_exists.missing_data_files(
        syms_books_to_dates, datadir()
    )

    if len(missing_data_files["missing"]) > 0:
        missing = missing_data_files["missing"]

        # Maybe for this thing, you want to actually remove these symbols
        # from the indeps.
        missing_sps = []
        print(side_prods)
        print("Missing these data files: {}".format(missing))
        print("But not removing sps")

        for one_missing_file in missing:
            missing_mkt = one_missing_file.split("/")[0]
            missing_sym = one_missing_file.split("/")[1].split("_")[0].upper()
            missing_sps.append((missing_sym, missing_mkt))
        missing_sps = list(set(missing_sps))

        new_sps = []
        passes = True
        for one_sp in side_prods:
            for one_missing_pair in missing_sps:
                if (
                    one_sp["symbol"] == one_missing_pair[0]
                    and one_sp["markets"][0] == one_missing_pair[1]
                ):
                    passes = False
            if passes:
                new_sps.append(one_sp)
            else:
                print(
                    "Would have {} from side_products because data is missing".format(
                        one_sp
                    )
                )
                new_sps.append(one_sp)

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

    # Run tickcounter.
    # Run it for ins- and oos-
    # Because we care about r2 in both cases.
    if not status_dict["tickrate"]["done"]:
        print("Status_dict looks like {}".format(status_dict["tickrate"]["done"]))
        tickrate_dir = os.path.join(work_dir, "tickrate")

        # All these objects are List of sampling tempos
        # TODO: include support for multi-tempo mixtures.
        # Right now, everythig is fixed at 1.
        sampling_tempo_specs = training_dict["timer_specs"]["sampling_tempos"][0]
        markout_tempo_specs = training_dict["timer_specs"]["markout_tempos"][0]

        # Samples.
        # ticks between sample should stay constant.

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

    buggy_sampling_replacement = training_dict["fit_specs"].get(
        "buggy_sampling_replacement", True
    )
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
        buggy_sampling_replacement=buggy_sampling_replacement,
        old_style_rets=training_dict["fit_specs"].get("old_style_rets", False),
        reference_symbol=training_dict.get("reference_symbol", {}),
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
        buggy_sampling_replacement=buggy_sampling_replacement,
        old_style_rets=training_dict["fit_specs"].get("old_style_rets", False),
        reference_symbol=training_dict.get("reference_symbol", {}),
    )

    # Run coeflair
    if not status_dict["final_fit"]["done"]:
        # Multiple sources of inheritance.

        inherit_specs = training_dict["final_fit"]["inherit"]
        inherited_sigs = MagicLib.get_inherited_sigs(
            sym, mkts, side_prods, inherit_specs
        )

        lair_all_results = CoefLair.scribe_and_hatch_full_model(
            work_dir,
            default_ins_dates,
            training_dict["day_start_regress"],
            training_dict["day_end_regress"],
            inherited_sigs,
            sym,
            mkts,
            ins_sampling_confs,
            reg_dct=training_dict["final_fit"],
            num_wanted_signals=training_dict["final_fit"].get("num_wanted_signals", 60),
            #reg_style=training_dict["final_fit"].get("reg_style", "fwd"),
            #reg_alpha=training_dict["final_fit"].get("reg_alpha", 1e-6),
            use_post_ols_coef=training_dict["final_fit"].get(
                "use_post_ols_coef", False
            ),
            scribe_densely=training_dict["final_fit"].get("scribe_densely", False),
            reference_symbol=training_dict.get("reference_symbol", {}),
        )

        final_lair_out = lair_all_results["lair_out"]

        # Potentially do a secondary set of regressions, after the first.
        if training_dict["final_fit"].get("do_post_final_regress", False):
            print("Running secondary post-final regression.")

            # Do an ols regression on the coefficients
            post_final_regress_specs = training_dict["final_fit"]["post_regress_specs"]
            # After the first filter
            post_sig_params = final_lair_out["chosen_sig_params"]

            post_lair_all_results = CoefLair.scribe_and_hatch_full_model(
                work_dir,
                default_ins_dates,
                training_dict["day_start_regress"],
                training_dict["day_end_regress"],
                post_sig_params,
                sym,
                mkts,
                ins_sampling_confs,
                reg_dct = post_final_regress_specs,
                num_wanted_signals=post_final_regress_specs.get(
                    "num_wanted_signals", 60
                ),
                #reg_style=post_final_regress_specs.get("reg_style", "enetcv"),
                #reg_alpha=post_final_regress_specs.get("reg_alpha", 2e-6),
                use_post_ols_coef=False,
                scribe_densely=training_dict["final_fit"].get("scribe_densely", False),
                job_name="post_final_regress",
                reference_symbol=training_dict.get("reference_symbol", {}),
            )

            final_lair_out = post_lair_all_results["lair_out"]
            print("Post-regression done")

        # Update status.
        status_dict["final_fit"]["pred_path"] = final_lair_out["pred_path"]
        status_dict["final_fit"]["pred_name"] = final_lair_out["pred_name"]
        status_dict["final_fit"]["ref_path"] = final_lair_out["ref_path"]
        status_dict["final_fit"]["ref_name"] = final_lair_out["ref_name"]
        status_dict["final_fit"]["stats"] = final_lair_out["stats"]
        status_dict["final_fit"]["done"] = True
        write_status(cache_path, status_dict)


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
            scribe_densely=training_dict["final_fit"].get("scribe_densely", False),
        )
        status_dict["oos_r2"]["done"] = True
        status_dict["oos_r2"]["oos_r2"] = regress_score["score"]
        write_status(cache_path, status_dict)

    if args.run_until and args.run_until == "final_fit":
        print("final_fit completed, exiting")
        return

    # Run pnlclimb
    if not status_dict["pnlclimb"]["pnlclimb_done"]:
        # This lets us do multiple pnlclimbs in the same dir.
        dir_name = training_dict["pnlclimb_specs"].get("dir_name", "pnlclimb")

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

            pnl_ins_dates = blacklist(pnl_ins_dates, blacklist_dates)
            pnl_oos_dates = blacklist(pnl_oos_dates, blacklist_dates)

        # Potentially truncate numdays
        if "numdays" in training_dict["pnlclimb_specs"]:
            truncate_numdays = training_dict["pnlclimb_specs"]["numdays"]
            pnl_ins_dates = pnl_ins_dates[-truncate_numdays:]

        pnlclimb_cache_dir = os.path.join(work_dir, "pnlclimb_cache")
        if not os.path.exists(pnlclimb_cache_dir):
            os.mkdir(pnlclimb_cache_dir)

        pnl_dir = os.path.join(work_dir, dir_name)
        if not os.path.exists(pnl_dir):
            os.mkdir(pnl_dir)

        # For things that have a local- and remote- signal
        # (eg. the relative priced ordexes):
        remote_sig_dct = {}
        remote_sig_name = ""

        # This is overrideable, if we start doing stuff w/ relative guys.
        # If this is the case, then this is the local_sig.
        # In normal usage, ref_path and ref_name correspond to the midmkt of the
        # symbol we are currently trading.
        # However, if we use a reference_symbol, we may switch it over.
        ref_path = status_dict["final_fit"]["ref_path"]
        ref_name = status_dict["final_fit"]["ref_name"]

        if "remote_product" in training_dict:

            remote_sym = training_dict["remote_product"]["symbol"]
            remote_markets = training_dict["remote_product"]["markets"]

            if "TopBookCme" in remote_markets or "TopBookEquity" in remote_markets:
                remote_sig = SigFormer.SigQuoteMidSpec(
                    "ref_{}".format(remote_sym),
                    symbol=remote_sym,
                    markets=remote_markets,
                    fixed_val_overrides={},
                    climbables={},
                )
            else:
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

            # Honestly: if we're doing something like a relative thing, with a reference_symbol,
            # then I'm going to need to a real switcharoo.

            # I don't know if this is the best place to insert it, but I guess we might as... well?
            ref_symbol = training_dict.get("reference_symbol", {})

            if len(ref_symbol) > 0:
                # Then, we're doing a switcharoo.
                remote_sym = training_dict["reference_symbol"]["symbol"]
                remote_markets = training_dict["reference_symbol"]["markets"]
                # This is the basic version.
                remote_sig = SigFormer.SigMidSpec(
                    "remote_ref_{}".format(remote_sym),
                    symbol=remote_sym,
                    markets=remote_markets,
                    fixed_val_overrides={},
                    climbables={},
                )

                remote_sig_stuff = remote_sig.apply_varied_params({})
                remote_sig_name = remote_sig_stuff["name"]
                remote_sig_dct = remote_sig_stuff["signals_dict"]

                # Also, for localsig, allows us to make a cool cool liqbalsig instead
                local_use_liqbal = training_dict["pnlclimb_specs"].get(
                    "local_use_liqbal", False
                )
                if local_use_liqbal:

                    liqbal_sz_decay = training_dict["pnlclimb_specs"].get("liqbal_sz_decay", 5000)
                    liqbal_levels_deep = training_dict["pnlclimb_specs"].get("liqbal_levels_deep", 5)
                    #Consider changing this to "Size"
                    liqbal_decay_type = training_dict["pnlclimb_specs"].get("liqbal_decay_type", "EXP_NOTIONAL")

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
                        symbol=traded_sym,
                        markets=[traded_mkt],
                        fixed_val_overrides=liq_bal_overrides,
                        climbables={"top_level_signal": {}},
                    )
                else:
                    # Also set up the local sig.
                    local_sig = SigFormer.SigMidSpec(
                        "local_ref_{}".format(remote_sym),
                        symbol=traded_sym,
                        markets=[traded_mkt],
                        fixed_val_overrides={},
                        climbables={},
                    )

                local_sig_stuff = local_sig.apply_varied_params({})

                ref_name = local_sig_stuff["name"]

                # Write out the local_sym to an appropriate spot and then override it.
                ref_path = os.path.join(pnl_dir, "local_sig.json")
                with open(ref_path, "w") as f:
                    f.write(json.dumps(local_sig_stuff["signals_dict"], indent=2))


        # Note that we do some potentially funky stuff to support relative things (eg. relsimplecross, relwidemm.)
        pclimber = PnlClimb.PnlClimber(
            pnl_dir,
            training_dict,
            status_dict["final_fit"]["pred_path"],
            status_dict["final_fit"]["pred_name"],
            ref_path,
            # ref_dct, really used by the refinery but not here.
            {},
            ref_name,
            pnl_ins_dates,
            pnl_oos_dates,
            pnlclimb_cache_dir,
            daily_stats_dct=status_dict["dailystats"],
            reference_symbol=training_dict.get("reference_symbol", {}),
            remote_symbol=training_dict.get("remote_symbol", {}),
            remote_sig_dct=remote_sig_dct,
            remote_sig_name=remote_sig_name
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

            # Write the ins, oos stats into the parent dir too. 
            parent_dir = os.path.dirname(work_dir)
            ins_path = os.path.join(parent_dir, "ins_stats.txt")
            oos_path = os.path.join(parent_dir, "oos_stats.txt")
            sym_label  = "{}_{}".format(traded_sym, training_dict["pnlclimb_specs"].get("dir_name", "pnlclimb"))

            files_exist = os.path.exists(ins_path) and os.path.exists(oos_path)

            label_line = "label,avg_pnl,avg_closed_pnl,avg_comm,sharpe,avg_numtrds,avg_flipped,pct_positive,avg_fillrate"
            with open(ins_path, "a") as f:
                kept_line = "{},{:.2f},{:.2f},{:.2f},{:.2f},{:.0f},{:.0f},{:.2f},{:.2f},".format(sym_label, ins_stats["avg_pnl"], ins_stats["avg_closed_pnl"], ins_stats["avg_comm"], ins_stats["sharpe"], ins_stats["avg_numtrds"], ins_stats["avg_flipped"], ins_stats["pct_positive"], ins_stats["avg_fillrate"])
                if not files_exist:
                    f.write(label_line + "\n")
                f.write(kept_line + "\n")

            with open(oos_path, "a") as f:
                kept_line = "{},{:.2f},{:.2f},{:.2f},{:.2f},{:.0f},{:.0f},{:.2f},{:.2f},".format(sym_label, oos_stats["avg_pnl"], oos_stats["avg_closed_pnl"], oos_stats["avg_comm"], oos_stats["sharpe"], oos_stats["avg_numtrds"], oos_stats["avg_flipped"], oos_stats["pct_positive"], oos_stats["avg_fillrate"])
                if not files_exist:
                    f.write(label_line + "\n")
                f.write(kept_line + "\n")


        status_dict["pnlclimb"]["golden_pk_conf"] = pkt_file
        write_status(cache_path, status_dict)

    if args.run_until and args.run_until == "pnlclimb":
        print("pnlclimb completed, exiting")
        return


def main():
    parser = argparse.ArgumentParser()

    # If we have a superclass json that stuff inherits from. Makes doing experiments across multiple symbols, easier.
    parser.add_argument("--super", type=str)
    parser.add_argument("--conf", type=str, required=True)
    # I think newer python can deal with this properly. But still.
    parser.add_argument("--force_restart", action="store_true")

    # Values:
    # coeflair
    # pnlclimb

    parser.add_argument("--run_until", type=str)

    args = parser.parse_args()

    training_pipeline(args)

    pass


if __name__ == "__main__":
    main()
