#! /usr/bin/env python

"""
A collection of useful things for modelbuilding scripts.
Libraries, lookups, etc.



"""
import os
import sys
import glob
import json
import numpy as np

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from util.pathing import inheritdir, tickerdir


from stratbuilder import (
    TempoRate,
)
from templatizers.SigFormer import *

# Try to decrease spurious correlations.

# Users can use "GLOBAL" to specify this list of included symbols.
# They tend to correlate with a lot of things.
GLOBAL_INDEPS = [
    "ETHUSDT",
    #    "APTUSDT",
    "AVAXUSDT",
    "SOLUSDT",
    #    "XRPUSDT",
    "MATICUSDT",
    "BTCUSDT",
    #    "ARBUSDT",
]


# Just lets you specify a bunch of indep symbols at once. If you need it.
# Eventually spec the traded symbol for lookups too. Not now tho.
def get_indeps(traded_sym, traded_mkt, sp_lst):

    orig_indeps_lst = []
    for one_sp in sp_lst:
        if one_sp["symbol"] == "GLOBAL":
            # Iterate through the global
            for one_indep_sym in GLOBAL_INDEPS:
                orig_indeps_lst.append(
                    {"symbol": one_indep_sym, "markets": one_sp["markets"]}
                )
        else:
            orig_indeps_lst.append(one_sp)

    # Remove duplicates and the traded_symbol

    filter_indeps_list = [
        sp_dct
        for sp_dct in orig_indeps_lst
        if (sp_dct["symbol"] != traded_sym or sp_dct["markets"][0] != traded_mkt)
    ]

    # Dedup the entire list.
    handled_strings = set()
    filtered_again_lst = []
    for one_dct in filter_indeps_list:
        cur_dct_str = str(one_dct)
        if cur_dct_str in handled_strings:
            continue
        handled_strings.add(cur_dct_str)
        filtered_again_lst.append(one_dct)

    return filtered_again_lst


# Returns: a list of sym_dicts that we can use for inheriting. This performs
# the switcharoo automatically..
# Note: this does not do the symbol/
def get_related_inherits(cur_sym, cur_mkt, traded_sym, traded_mkt, n_files=5):

    # These are dicts that we'll want loaded by the sigformer.
    wanted_sig_params = []

    # Given a symbol, "translate" it to the expected right symbol in BinanceFutures,
    # where we have mmost of our saved stuff.
    binance_sym_name = cur_sym

    # Note, this really just tries to translate the symbol down to its base token and
    # tries to find a similar symbol in the BinanceFutures bookticker.
    # It kind of collapses a bunch of exchange symbology into one thing. But like,
    # it works for now. Later on I should probably do a per-exchange conversion thing.
    if "FDUSD" in cur_sym:
        suffix_loc = cur_sym.find("FDUSD")
        binance_sym_name = cur_sym[:suffix_loc] + "USDT"
    elif "PERP" in cur_sym and "USD" in cur_sym:
        suffix_loc = cur_sym.find("PERP")
        binance_sym_name = cur_sym[:suffix_loc] + "USDT"

    elif "-" in cur_sym:
        suffix_loc = cur_sym.find("-")
        binance_sym_name = cur_sym[:suffix_loc] + "USDT"
    elif "USD" in cur_sym:
        # A lot of symbols are named stuff like BTCUSD or BTCUSDT
        suffix_loc = cur_sym.find("USD")
        binance_sym_name = cur_sym[:suffix_loc] + "USDT"

    # Let's try to find it now.
    #similar_syms = get_similar_syms(binance_sym_name)
    # Gonna turn off the similar syms thing for now. Would rather we just be more explicit.
    similar_syms = []

    # OK, great. Now let's go through these and find the files.
    inherit_files = []
    for one_sim_sym in similar_syms:
        inherit_files += glob.glob(
            os.path.join(inheritdir(), "*", one_sim_sym, "*.json")
        )
        if len(inherit_files) > n_files:
            break

    # Now, go through inherit_files and and grab them.
    for one_sig_param_path in inherit_files:
        with open(one_sig_param_path, "r") as f:
            one_sig_params_list = json.loads(f.read())

        for one_inherited_param_setting in one_sig_params_list:
            if "LiqBal" in one_inherited_param_setting["classname"]:
                continue

            # Check if this signal was even a signal using one of the "similar" symbols we decided on earlier.
            # Also, there's a situation because we don't include the cur_symbol inside
            # the similar_syms ranked list. But the cur_sym *may* exist in the the saved
            # signal params of the other symbol - it may have just been an indepsig.
            if one_inherited_param_setting["symbol"] not in similar_syms and one_inherited_param_setting["symbol"] != binance_sym_name:
                #print("Skipping sig({}) bc we're trying to find {}".format(one_inherited_param_setting["symbol"], cur_sym))
                continue

            for sig_name, sig_dict in one_inherited_param_setting["fixed_vals"].items():
                if "symbol" in sig_dict:
                    del sig_dict["symbol"]
                if "markets" in sig_dict:
                    del sig_dict["markets"]
                if "books" in sig_dict:
                    del sig_dict["books"]

            #print(
            #    "Swapping {} for {}".format(
            #        one_inherited_param_setting["symbol"], cur_sym
            #    )
            #)
            one_inherited_param_setting["symbol"] = cur_sym
            one_inherited_param_setting["markets"] = [cur_mkt]
            one_inherited_param_setting["traded_symbol"] = traded_sym
            one_inherited_param_setting["traded_markets"] = [traded_mkt]

            wanted_sig_params.append(one_inherited_param_setting)

    return wanted_sig_params


# Returns both the daily and overall fit targets.
# fit_spec_dct is formatted like:
"""
    fit_specs: {
        "fit_tgt": 60,
        "use_daily_markout_rate": true
    }

    or

    fit_specs: {
      "fit_tgt_min": 10,
      "fit_tgt_max": 120,
      "fit_tgt_num": 30,
      "calc_fit_tgt_daily": true,
      "use_daily_markout_rate": false
    }

    btw:
    calc_fit_daily_tgt will take those fit params and give a different markout tgt for each day.
    use_daily_markout_rate uses the markout timer's density and can modify the threshold for each day.
    They are not the same.

"""


# Note that this does not calculate the actual markout threshes.
# We either return:
# {"fit_tgt": _number_}
# or
# {"daily_fit_tgt": {day: fit_tgt}}
def fit_tgt(fit_spec_dct, inside_tempo_stats):
    # For each day, set both the target and the markout thresh.

    # First, figure out the aggregate tgt.
    # If we need to calc daily, then we calc daily.

    avg_fit_tgt = 1

    to_ret = {}
    if "fit_tgt" in fit_spec_dct:
        to_ret["fit_tgt"] = fit_spec_dct["fit_tgt"]
        to_ret["med_tgt"] = fit_spec_dct["fit_tgt"]
    else:
        # We either calc it daily or not.
        if not fit_spec_dct["calc_fit_tgt_daily"]:
            markout_tgt = (
                fit_spec_dct["fit_tgt_num"] / inside_tempo_stats["ticks_per_sec"]
            )
            # By default, bound at 10 and 120
            markout_tgt = max(markout_tgt, fit_spec_dct["fit_tgt_min"])
            markout_tgt = min(markout_tgt, fit_spec_dct["fit_tgt_max"])
            to_ret["fit_tgt"] = markout_tgt

            to_ret["med_tgt"] = markout_tgt
        else:
            # Calculate the daily rate.
            daily_fit_tgts = {}
            all_tgts = []

            for date, inside_changes in inside_tempo_stats["daily_ticks"].items():
                # Gotta get the insides per sec
                insides_per_sec = inside_changes["n_ticks"] / (
                    inside_tempo_stats["tot_secs"]
                    / len(inside_tempo_stats["daily_ticks"])
                )

                markout_tgt = fit_spec_dct["fit_tgt_num"] / insides_per_sec

                # By default, bound at 10 and 120
                markout_tgt = max(markout_tgt, fit_spec_dct["fit_tgt_min"])
                markout_tgt = min(markout_tgt, fit_spec_dct["fit_tgt_max"])
                daily_fit_tgts[date] = markout_tgt
                all_tgts.append(markout_tgt)

            # Let's use the median instead? Don't want the
            to_ret["med_tgt"] = np.median(all_tgts)
            to_ret["daily_fit_tgt"] = daily_fit_tgts

    return to_ret


# Given a fit_tgt_dict and a set of tempo stats, give the markout threshes - whether
# on a per-day basis or over the whole period.
# mkout_tempo_stats is the dict that looks like ["tickrate"]["ins_sampling_ticks_dict"] in
# the cache.json.
# OK. So this makes us "more right" on days when we just don't have enough data. We don't mess up the regression.
def markout_thresh(fit_tgt_dct, mkout_tempo_stats, use_daily_markout_rate):
    to_ret = {}
    if "fit_tgt" in fit_tgt_dct:
        if not use_daily_markout_rate:
            # The classic thing. Fit tgt divided by mkout tempo ticks-per-second.
            markout_thresh = mkout_tempo_stats["ticks_per_sec"] * fit_tgt_dct["fit_tgt"]
            to_ret["markout_thresh"] = markout_thresh
        else:
            # Go through day by day and calculate thresh.
            for day, day_tick_count in mkout_tempo_stats["daily_ticks"].items():
                # Changed it to using the exact current day's n_ticks.
                # This way, we should be able to adjust for if a day has
                # partial data (in some cases, I saw just 10% data. )
                daily_ticks_per_sec = day_tick_count["n_ticks"] / (
                    day_tick_count["n_secs"]
                )
                markout_thresh = daily_ticks_per_sec * fit_tgt_dct["fit_tgt"]
                to_ret[day] = markout_thresh
    elif "daily_fit_tgt" in fit_tgt_dct:
        # The per_day thing is a little silly but can still make sense.
        # We either use the session's ticks_per_sec or the daily ticks_per_sec.
        for day, day_tick_count in mkout_tempo_stats["daily_ticks"].items():
            if use_daily_markout_rate:
                ticks_per_sec = day_tick_count["n_ticks"] / (
                    day_tick_count["n_secs"]
                )
            else:
                ticks_per_sec = mkout_tempo_stats["ticks_per_sec"]

            markout_thresh = ticks_per_sec * fit_tgt_dct["daily_fit_tgt"][day]
            to_ret[day] = markout_thresh
    return to_ret


# Maybe this is too much work right tnow.
def form_multi_tempo():
    pass


# Just take out
# Do the same thing as we did.
# Just all the things.
def gen_sampling_specs(
    sym,
    mkts,
    dates_list,
    day_start_regress,
    day_end_regress,
    sampling_tempo_specs,
    markout_tempo_specs,
    tickrate_dir,
    fit_specs,
):
    sampling_ticks_dict = TempoRate.run_tickrate(
        sym,
        mkts,
        dates_list,
        day_start_regress,
        day_end_regress,
        sampling_tempo_specs["type"],
        tickrate_dir,
        "sampling",
    )

    if markout_tempo_specs["type"] == sampling_tempo_specs["type"]:
        markout_ticks_dict = sampling_ticks_dict.copy()
    else:
        markout_ticks_dict = TempoRate.run_tickrate(
            sym,
            mkts,
            dates_list,
            day_start_regress,
            day_end_regress,
            markout_tempo_specs["type"],
            tickrate_dir,
            "markout",
        )

    # Set insidechanges.
    inside_ticks_dict = TempoRate.run_tickrate(
        sym,
        mkts,
        dates_list,
        day_start_regress,
        day_end_regress,
        "inside",
        tickrate_dir,
        "markout",
    )

    fit_tgt_dict = fit_tgt(fit_specs, inside_ticks_dict)

    to_ret = {
        "sampling_ticks_dict": sampling_ticks_dict,
        "markout_ticks_dict": markout_ticks_dict,
        "inside_ticks_dict": inside_ticks_dict,
        "fit_tgt_dict": fit_tgt_dict,
    }
    return to_ret


# Generates the conffiles and then returns a
# dated map of dates->
def gen_sampling_confs(
    traded_symbol,
    traded_markets,
    dates_list,
    sampling_ticks_dict,
    markout_ticks_dict,
    ticks_between_sample,
    markout_thresh_dict,
    sampling_conf_dir,
    markout_cap,
    overwrite=False,
    buggy_sampling_replacement=True,
    old_style_rets=False,
    reference_symbol={}
):
    if not os.path.exists(sampling_conf_dir):
        os.mkdir(sampling_conf_dir)

    days_to_sampling_confs = {}

    daily_sampling_tempos = False
    if len(markout_thresh_dict) > 1:
        daily_sampling_tempos = True

    all_tempos = []
    all_tempos += sampling_ticks_dict["tempo_dict"]["tempos"]
    all_tempos += markout_ticks_dict["tempo_dict"]["tempos"]

    mid_sym = traded_symbol
    mid_mkt = traded_markets
    # Get rid of this for the regressing part.
    if len(reference_symbol) > 0:
        mid_sym = reference_symbol["symbol"]
        mid_mkt = reference_symbol["markets"]

    sig_mid = SigMidSpec(
        "ref",
        symbol=mid_sym,
        markets=mid_mkt,
        fixed_val_overrides={},
        climbables={},
    )
    sig_mid_name = sig_mid.name
    sig_mid_dict = sig_mid.get_sig_dict()["signals_dict"]["signals"][0]

    # This is a little annoying. I'm sorry.
    if daily_sampling_tempos == False:
        # FYI, can have an issue if the ins- or oos- is just one day.
        lair_sampling_config = {
            "signalscanner": {
                "markout_tempo": markout_ticks_dict["tempo_name"],
                "markout_thresh": markout_thresh_dict["markout_thresh"],
                "sampling_tempo": sampling_ticks_dict["tempo_name"],
                "sampling_thresh": ticks_between_sample,
                "tgt_sig": sig_mid_name,
                "markout_cap": markout_cap,
                "old_style_rets": old_style_rets,
            },
            "tempos": all_tempos,
            "signals": [sig_mid_dict],
        }

        lair_sampling_path = os.path.join(sampling_conf_dir, "sampling_conf.json")

        if overwrite or (not os.path.exists(lair_sampling_path)):
            with open(lair_sampling_path, "w") as f:
                f.write(json.dumps(lair_sampling_config, indent=2))

        for date in dates_list:
            days_to_sampling_confs[date] = lair_sampling_path
    else:
        # Make a different sampling conf for every single day.
        for one_day, daily_thresh in markout_thresh_dict.items():
            lair_sampling_config = {
                "signalscanner": {
                    "markout_tempo": markout_ticks_dict["tempo_name"],
                    "markout_thresh": daily_thresh,
                    "sampling_tempo": sampling_ticks_dict["tempo_name"],
                    "sampling_thresh": ticks_between_sample,
                    "tgt_sig": sig_mid_name,
                    "markout_cap": markout_cap,
                    "old_style_rets": old_style_rets,
                },
                "tempos": all_tempos,
                "signals": [sig_mid_dict],
            }
            if buggy_sampling_replacement:
                # This is a shame but when I implemented magiclib, I made this happen by accident.
                # So have the buggy version be the default unless if I want to do research
                # with this on.
                lair_sampling_config["sampling_tempo"] = markout_ticks_dict[
                    "tempo_name"
                ]

            lair_sampling_path = os.path.join(
                sampling_conf_dir, "sampling_conf.{}.json".format(one_day)
            )

            if overwrite or (not os.path.exists(lair_sampling_path)):
                with open(lair_sampling_path, "w") as f:
                    f.write(json.dumps(lair_sampling_config, indent=2))
            days_to_sampling_confs[one_day] = lair_sampling_path

    return days_to_sampling_confs


###################################################################
# For getting inherited signals. Let's have specs for
# how to get the traded symbol signals. How to get the indep signals.
# Whether to include the indeps at all.
# Or not.

import subprocess


def get_file_added_date(filename):
    old_dir = os.getcwd()
    os.chdir(os.path.dirname(filename))
    try:
        # Use git log to get the commit history of the file
        log_output = subprocess.check_output(
            ["git", "log", "--format=%ad", "--date=iso", "--", filename]
        )

        # Split the output by newline and take the first line (the earliest date)
        earliest_date = log_output.decode("utf-8").strip().split("\n")[0]
        os.chdir(old_dir)
        return earliest_date
    except subprocess.CalledProcessError:
        os.chdir(old_dir)
        return None


# Example dict:
#        "inherit": {
#            "use_inherit": true,
#            "traded": {
#                "include": true,
#                "use_default_dir": true,
#                "inherit_globs": [
#                ],
#                "traded_sym_override": false,
#                "src_sym_switcharoo": false,
#                "latest_n_files": 2,
#                "exclude_pattern": "10d"
#            },
#            "side": {
#                "include": true,
#                "use_native_and_override": false,
#                "use_traded_indeps": true,
#                "inherit_globs": [],
#                "latest_n_files": 2,
#                "exclude_pattern": "10d"
#            }
#            },


# There's a lot of logic in here, so I'm going to do it very
# slowly and painstakingly. Sorry that there is redundancy.
def get_inherited_sigs(traded_sym, traded_mkts, side_products, inherit_specs):
    # :(
    if not inherit_specs["use_inherit"]:
        return []

    all_inherited_params = []
    traded_sym_params = []
    side_prod_params = []

    ########################################################################
    # First, deal with the traded symbol.
    # This will ONLY do stuff for the traded symbol, even if the indep
    # symbols would reference the same files.
    traded_specs = inherit_specs["traded"]
    if traded_specs["include"]:
        traded_sym_paths = []
        traded_exclude_pattern = traded_specs.get("exclude_pattern", "")
        if inherit_specs["traded"]["use_default_dir"]:
            # Use the default dir glob
            sig_inherit_dir = os.path.join(inheritdir(), traded_mkts[0], traded_sym)
            if os.path.exists(sig_inherit_dir):
                traded_sym_paths = glob.glob(os.path.join(sig_inherit_dir, "*.json"))

            # Special treat. If there isn't stuff in these paths, then we can
            # add some globs
            if len(traded_sym_paths) < 5:
                supplements = []
                supplement_glob = glob.glob(
                    os.path.join(os.path.join(inheritdir(), traded_mkts[0], "*/*.json"))
                )
                # Turn on the switcharoo.
                traded_specs["traded_sym_override"] = True
                traded_specs["src_sym_switcharoo"] = True
                traded_sym_paths += supplement_glob

        else:
            # Goes through the globs.
            for one_sig_glob in traded_specs["inherit_globs"]:
                globbed_paths = glob.glob(one_sig_glob)
                traded_sym_paths += globbed_paths

        # Now, filter out the exclude_pattern.
        pre_len = len(traded_sym_paths)
        traded_sym_paths = [
            p_path
            for p_path in traded_sym_paths
            if (
                len(traded_exclude_pattern) == 0 or traded_exclude_pattern not in p_path
            )
        ]

        post_len = len(traded_sym_paths)

        paths_and_dates = []

        for one_traded_sym_path in traded_sym_paths:
            add_date = get_file_added_date(one_traded_sym_path)
            paths_and_dates.append(
                {"path": one_traded_sym_path, "added_date": add_date}
            )
        # Sort.
        paths_and_dates.sort(key=lambda x: x["added_date"], reverse=True)

        kept_traded_sym_paths = [
            path_and_date["path"]
            for path_and_date in paths_and_dates[: traded_specs["latest_n_files"]]
        ]

        # print("Grabbing inherited traded params from: {}".format(kept_traded_sym_paths))

        # TODO: maybe limit it to only like, 20 sigs from each inherit file or something.
        for one_sig_param_path in kept_traded_sym_paths:
            with open(one_sig_param_path, "r") as f:
                one_sig_params_list = json.loads(f.read())
            #print("Main: Reading sigs from {}".format(one_sig_param_path))

            for one_inherited_param_setting in one_sig_params_list:

                # Let's not entertain these, ok?
                if "LiqBal" in one_inherited_param_setting["classname"]:
                    continue
                # Undecided whether this is good or not.
                # if 'Libra' in one_inherited_param_setting["classname"]:
                #    continue

                # Remove all notions of symbol and book from the inherited.
                # It creates problemos.
                for sig_name, sig_dict in one_inherited_param_setting[
                    "fixed_vals"
                ].items():
                    if "symbol" in sig_dict:
                        del sig_dict["symbol"]
                    if "markets" in sig_dict:
                        del sig_dict["markets"]
                    if "books" in sig_dict:
                        del sig_dict["books"]

                sbp = (
                    one_inherited_param_setting["symbol"],
                    one_inherited_param_setting["markets"][0],
                )

                # If we want to move other symbols' signals over to ours.
                if traded_specs["src_sym_switcharoo"] and (
                    one_inherited_param_setting["symbol"]
                    == one_inherited_param_setting["traded_symbol"]
                    and one_inherited_param_setting["markets"][0]
                    == one_inherited_param_setting["traded_markets"][0]
                ):
                    # Switch it.
                    one_inherited_param_setting["symbol"] = traded_sym
                    one_inherited_param_setting["markets"] = traded_mkts
                    one_inherited_param_setting["traded_symbol"] = traded_sym
                    one_inherited_param_setting["traded_markets"] = traded_mkts

                if traded_specs["traded_sym_override"]:
                    one_inherited_param_setting["traded_symbol"] = traded_sym
                    one_inherited_param_setting["traded_markets"] = traded_mkts

                if (
                    one_inherited_param_setting["symbol"] == traded_sym
                    and one_inherited_param_setting["markets"][0] == traded_mkts[0]
                ):
                    traded_sym_params.append(one_inherited_param_setting)


    # Potentially supplement traded_sym_params
    if (
        traded_specs.get("use_supplement", False)
        and len(traded_sym_params) < traded_specs["supplement_to_n"]
    ):
        n_wanted = traded_specs["supplement_to_n"] - len(traded_sym_params)
        # Get supplemental params.
        supp_params = get_related_inherits(
            traded_sym, traded_mkts[0], traded_sym, traded_mkts[0]
        )
        supp_params = supp_params[:n_wanted]

        # Add supplemental params.
        traded_sym_params += supp_params
        print(
            "MagicLib::get_inherited_sigs: Added {} params as supplements to traded_symbol {}".format(
                len(supp_params), traded_sym
            )
        )

    ########################################################################
    # Secondly, deal with the side products.


    side_specs = inherit_specs["side"]
    if side_specs["include"]:
        side_exclude_pattern = side_specs.get("exclude_pattern", "XXXXX")
        print("Side products is {}".format(side_products))
        for one_side_product in side_products:

            cur_side_prod_params = []

            side_prod_paths = []

            if side_specs["use_native_and_override"]:
                # Go inside the side product's own directory.
                # and grab those files.
                sig_inherit_dir = os.path.join(
                    inheritdir(),
                    one_side_product["markets"][0],
                    one_side_product["symbol"],
                )
                if os.path.exists(sig_inherit_dir):
                    side_prod_paths = glob.glob(os.path.join(sig_inherit_dir, "*.json"))

            elif side_specs["use_traded_indeps"]:
                sig_inherit_dir = os.path.join(inheritdir(), traded_mkts[0], traded_sym)
                if os.path.exists(sig_inherit_dir):
                    side_prod_paths = glob.glob(os.path.join(sig_inherit_dir, "*.json"))
            else:
                for one_sig_glob in side_specs["inherit_globs"]:
                    globbed_paths = glob.glob(one_sig_glob)
                    side_prod_paths += globbed_paths

            pre_len = len(side_prod_paths)
            side_prod_paths = [
                p_path
                for p_path in side_prod_paths
                if (
                    len(side_exclude_pattern) == 0 or side_exclude_pattern not in p_path
                )
            ]

            post_len = len(side_prod_paths)

            paths_and_dates = []

            for one_side_sym_path in side_prod_paths:
                add_date = get_file_added_date(one_side_sym_path)
                paths_and_dates.append(
                    {"path": one_side_sym_path, "added_date": add_date}
                )
            # Sort.
            paths_and_dates.sort(key=lambda x: x["added_date"], reverse=True)

            kept_side_sym_paths = [
                path_and_date["path"]
                for path_and_date in paths_and_dates[: side_specs["latest_n_files"]]
            ]

            # print(
            #    "Grabbing inherited side params from: {}".format(kept_side_sym_paths)
            # )
            for one_sig_param_path in kept_side_sym_paths:
                print("Side: reading from {}".format(one_sig_param_path))
                with open(one_sig_param_path, "r") as f:
                    one_sig_params_list = json.loads(f.read())

                for one_inherited_param_setting in one_sig_params_list:

                    # Let's not entertain these, ok?
                    if "LiqBal" in one_inherited_param_setting["classname"]:
                        continue

                    # Check that we got the right side product.
                    if (
                        one_inherited_param_setting["symbol"]
                        != one_side_product["symbol"]
                    ):
                        print("Skipping bc of {}".format(one_inherited_param_setting["symbol"]))
                        continue
                    if (
                        one_inherited_param_setting["markets"][0]
                        != one_side_product["markets"][0]
                    ):
                        print("Skipping bc of {}".format(one_inherited_param_setting["markets"]))
                        continue

                    if side_specs["use_native_and_override"]:
                        # Override, of course.
                        if not side_specs.get("actually_no_override", False):
                            one_inherited_param_setting["traded_symbol"] = traded_sym
                            one_inherited_param_setting["traded_markets"] = traded_mkts

                    # Remove all notions of symbol and book from the inherited.
                    # It creates problemos.
                    for sig_name, sig_dict in one_inherited_param_setting[
                        "fixed_vals"
                    ].items():
                        if "symbol" in sig_dict:
                            del sig_dict["symbol"]
                        if "markets" in sig_dict:
                            del sig_dict["markets"]
                        if "books" in sig_dict:
                            del sig_dict["books"]

                    if (
                        one_inherited_param_setting["traded_symbol"] == traded_sym
                        and one_inherited_param_setting["traded_markets"][0]
                        == traded_mkts[0]
                    ) or (side_specs.get("actually_no_override", False)):
                        # Add this to our collection.
                        cur_side_prod_params.append(one_inherited_param_setting)
                        #print("Got to the additional part")

            # Potentiall supplement too.
            if (
                side_specs.get("use_supplement", False)
                and len(cur_side_prod_params) < side_specs["supplement_to_n"]
            ):
                n_wanted = side_specs["supplement_to_n"] - len(cur_side_prod_params)
                # Get supplemental params.
                supp_params = get_related_inherits(
                    one_side_product["symbol"],
                    one_side_product["markets"][0],
                    traded_sym,
                    traded_mkts[0],
                )
                supp_params = supp_params[:n_wanted]

                # Add supplemental params.
                cur_side_prod_params += supp_params
                print(
                    "MagicLib::get_inherited_sigs: Added {} params as supplements to side_prod ({},{})".format(
                        len(supp_params),
                        one_side_product["symbol"],
                        one_side_product["markets"][0],
                    )
                )

            # Append the current sideproducts' params to the full list.
            side_prod_params += cur_side_prod_params

    # Now go through and gather the the actual signals from the side proinherit_specsducts.
    all_inherited_params = traded_sym_params + side_prod_params

    print("{} traded_sym_sigs {} side_prod_sigs {} total ".format(len(traded_sym_params), len(side_prod_params), len(all_inherited_params)))
    return all_inherited_params
