#! /usr/bin/env python

"""
In lieu of just adding to the hatchery, going to make a coefLair instead.


"""


import os
import subprocess
import sys
import json

import numpy as np
import pandas as pd
from sklearn.linear_model import (
    LinearRegression,
    Lasso,
    ElasticNet,
    SGDRegressor,
    ElasticNetCV,
    LarsCV,
    Lars,
    LassoCV,
    LassoLars,
    LassoLarsIC,
    OrthogonalMatchingPursuit,
    BayesianRidge,
    HuberRegressor,
    QuantileRegressor,
    LassoCV,
    ARDRegression,
    MultiTaskElasticNet,
    TheilSenRegressor,
)
from sklearn.metrics import r2_score

# Trying to implement custom loss fxn
from scipy.optimize import minimize


from scipy.stats.stats import pearsonr

# Potentially use this instead
import statsmodels.api as sm

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)
from templatizers.SigFormer import *

from util.chron import dates_list, dates_avail, blacklist

from util.pathing import bindir
from util.runner import run_batches

from stratbuilder.RegressLib import sig_passes_imba_filter

scanner_bin = os.path.join(bindir(), "signalscanner")

MAX_MISMATCHED_DAYS = 5

def load_aligned_rets_and_sigs(ret_paths, sig_val_paths, ret_names, sig_names):
    """Load rets and sigs per-day, dropping days where row counts don't match.

    Raises if more than MAX_MISMATCHED_DAYS days are mismatched.
    Returns (rets, sigs) as numpy arrays with aligned rows.
    """
    ret_lst = []
    sig_lst = []
    mismatched_days = []

    for idx, (ret_path, sig_path) in enumerate(zip(ret_paths, sig_val_paths)):
        ret = pd.read_csv(ret_path, names=ret_names).values
        sig = pd.read_csv(sig_path, header=None, names=sig_names).values
        if ret.shape[0] != sig.shape[0]:
            day_tag = os.path.basename(ret_path)
            mismatched_days.append((day_tag, ret.shape[0], sig.shape[0]))
        else:
            ret_lst.append(ret)
            sig_lst.append(sig)

    if mismatched_days:
        if len(mismatched_days) > MAX_MISMATCHED_DAYS:
            raise ValueError(
                "Too many mismatched days ({}) — something is likely wrong with the data. "
                "Mismatches: {}".format(len(mismatched_days), mismatched_days)
            )
        for day_tag, n_ret, n_sig in mismatched_days:
            print("WARNING: dropping {} due to rets/sigs row mismatch (rets={}, sigs={})".format(
                day_tag, n_ret, n_sig))

    rets = np.concatenate(ret_lst)
    sigs = np.concatenate(sig_lst)
    return rets, sigs


# This will run the scribe commands for everything too.
# Let's figure out what the tickrate_dict has and maybe unpack some of it to below things.


def scribe_and_hatch_full_model(
    work_dir,
    dates_list,
    day_start,
    day_end,
    # A list of dicts in the format of a climb cache.
    all_sig_params,
    traded_symbol,
    traded_markets,
    # This has... a lot of stuff.
    # including the fit tgts.
    days_to_sampling_confs,
    reg_dct,
    ################
    # For forward selection
    num_wanted_signals=100,
    #reg_style="fwd",
    ################
    #reg_alpha=1e-6,
    # For doing a secondary OLS after a lasso.
    use_post_ols_coef=False,
    job_name="coef_lair",
    scribe_densely=False,
    reference_symbol={},
):
    # Couple of things now. This is maybe a mini version of the modelclimb.
    # But the nice thing is it doesn't use that many data cycles.

    lair_work_dir = os.path.join(work_dir, job_name)
    if not os.path.exists(lair_work_dir):
        os.mkdir(lair_work_dir)

    if use_post_ols_coef:
        use_post_ols_coef = False
        print("CoefLair: switching use_post_ols_coef from True->False for safety.")


    # First, do some sanity checks for versions with multiple targets.
    markout_mults_str = ""
    if reg_dct["reg_style"] == "multienet":
        markout_mults_str = reg_dct["markout_mults"]
        # I think this might be possible out of the box? Let's try it...
        #if reg_dct.get("do_post_final_regress", False):
        #    sys.exit("Cannot run post-final regress with multinet. I'm sorry, maybe next year. ")


    ########################################################################
    # Writing our own scribe conf.

    # Now, step 1, run the sample-and-returns.

    rets = None
    sigs = None

    # Step 2. Scribe out... ALL of these thingz.
    # I'm writing these things out.
    sig_names = []
    sig_dicts = []
    # theis is a list of cache-format dicts.
    # I need to tighten up this naming. Omg.
    for idx, one_sig_cache_params in enumerate(all_sig_params):
        class_suff = one_sig_cache_params["symbol"]
        sig_obj = load_sigspec_from_cache(
            one_sig_cache_params, class_suff, prefix="lair_{}".format(idx)
        )
        sig_dct = sig_obj.apply_varied_params(
            one_sig_cache_params["param_vals"], suffix="_{}".format(idx)
        )

        sig_dicts += sig_dct["signals_dict"]["signals"]
        sig_names.append(sig_dct["name"])

    # Write it out.
    sigs_dir = os.path.join(lair_work_dir, "pred_sigs")
    if not os.path.exists(sigs_dir):
        os.mkdir(sigs_dir)

    sigs_path = os.path.join(sigs_dir, "pred_sigs.json")
    with open(sigs_path, "w") as f:
        f.write(json.dumps({"signals": sig_dicts}, indent=2))

    if scribe_densely:
        # Do something else
        rets_dir = os.path.join(lair_work_dir, "rets")
        if not os.path.exists(rets_dir):
            os.mkdir(rets_dir)

        sigs_dir = os.path.join(lair_work_dir, "pred_sigs")
        if not os.path.exists(sigs_dir):
            os.mkdir(sigs_dir)

        rets_and_sigs = scribe_dense_sigs(
            rets_dir,
            sigs_dir,
            dates_list,
            day_start,
            day_end,
            days_to_sampling_confs,
            sigs_path,
            sig_names,
        )

        rets = rets_and_sigs["rets"]
        sigs = rets_and_sigs["sigs"]
    else:
        # dict with keys "rets" and "times"
        rets_and_times = sample_and_returns(
            lair_work_dir, dates_list, day_start, day_end, days_to_sampling_confs, markout_mults_str=markout_mults_str
        )
        rets = rets_and_times["rets"]

        # Cool. So now this exists. Now I can run the scribe process.
        sigs = scribe_pred_sigs(
            sigs_dir,
            dates_list,
            day_start,
            day_end,
            days_to_sampling_confs,
            rets_and_times["times"],
            sigs_path,
            sig_names,
        )

    reg_style = reg_dct.get("reg_style", "fwd")

    # Finally we can hatch them out.
    if reg_style == "fwd":
        lair_out = lair_fwd_spawn_signals(
            lair_work_dir,
            traded_symbol,
            traded_markets,
            all_sig_params,
            sigs,
            rets,
            reg_dct,
            num_wanted_signals,
        )
    else:
        lair_out = lair_lasso_spawn_signals(
            lair_work_dir,
            traded_symbol,
            traded_markets,
            all_sig_params,
            sigs,
            rets,
            reg_style,
            reg_dct,
            #num_wanted_signals,
            #reg_alpha=reg_alpha,
            use_post_ols_coef=use_post_ols_coef,
            reference_symbol=reference_symbol,
        )

    # This makes it easier to reproduce things in like
    # ipython, if I want to test out other things.
    return {
        "lair_out": lair_out,
        "work_dir": lair_work_dir,
        "traded_symbols": traded_symbol,
        "traded_markets": traded_markets,
        "all_sig_params": all_sig_params,
        "sigval_paths": sigs,
        "rets": rets,
    }

    # Returns: a fully-fledged final model, an R2, everything you wish for.


def scribe_dense_sigs(
    rets_dir,
    sigs_dir,
    days_lst,
    day_start,
    day_end,
    days_to_sampling_confs,
    sig_conf_path,
    sig_names,
):
    scribe_procs = []
    all_sig_paths = []
    ret_paths = []

    # OK then.
    for one_day in days_lst:
        vals_tgt = "vals_{}.csv".format(one_day)
        signals_path = os.path.join(sigs_dir, vals_tgt)
        all_sig_paths.append(signals_path)

        returns_path = os.path.join(rets_dir, "rets_{}.csv".format(one_day))
        ret_paths.append(returns_path)

        scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage DENSE  --feature-conf {SIG_CONF_PATH} --signals-path {SIGNALS_PATH}  --returns-path {RETURNS_PATH}  --sigs {SIG_NAMES}'
        scanner_cmd = scanner_cmd.format(
            SCANNER_BIN=scanner_bin,
            DATE=one_day,
            START=day_start,
            END=day_end,
            SAMPLING_CONF=days_to_sampling_confs[one_day],
            SIG_CONF_PATH=sig_conf_path,
            SIGNALS_PATH=signals_path,
            RETURNS_PATH=returns_path,
            SIG_NAMES=",".join(sig_names),
        )

        if not (os.path.exists(signals_path) and os.path.exists(returns_path)):
            proc = subprocess.Popen(scanner_cmd, shell=True)
            scribe_procs.append(proc)

    for one_proc in scribe_procs:
        one_proc.wait()

    return {"rets": ret_paths, "sigs": all_sig_paths}


# Returns the list of rets, times.
def sample_and_returns(lair_dir, days_lst, day_start, day_end, days_to_sampling_confs, markout_mults_str=""):
    # Make the rets, times paths.
    rets_dir = os.path.join(lair_dir, "rets")
    times_dir = os.path.join(lair_dir, "times")

    if not os.path.exists(rets_dir):
        os.mkdir(rets_dir)

    if not os.path.exists(times_dir):
        os.mkdir(times_dir)

    scribe_procs = []

    time_paths = []
    ret_paths = []

    for one_day in days_lst:
        time_path = os.path.join(times_dir, "times_{}.csv".format(one_day))
        returns_path = os.path.join(rets_dir, "rets_{}.csv".format(one_day))

        time_paths.append(time_path)
        ret_paths.append(returns_path)

        scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage RETURNS --times-path {TIME_PATH} --returns-path {RETURN_PATH}'
        scanner_cmd = scanner_cmd.format(
            SCANNER_BIN=scanner_bin,
            DATE=one_day,
            START=day_start,
            END=day_end,
            SAMPLING_CONF=days_to_sampling_confs[one_day],
            TIME_PATH=time_path,
            RETURN_PATH=returns_path,
        )

        if markout_mults_str != "":
            scanner_cmd += " --markout-mults \"{}\"".format(markout_mults_str)

        if not (os.path.exists(time_path) and os.path.exists(returns_path)):
            proc = subprocess.Popen(scanner_cmd, shell=True)
            scribe_procs.append(proc)
        else:
            # print("Skipping {}, it exists".format(returns_path))
            pass

    # Display the sampling commands.
    print("Scanner cmd for rets is : \n {}".format(scanner_cmd))
    for one_proc in scribe_procs:
        one_proc.wait()

    return {"rets": ret_paths, "times": time_paths}


def scribe_pred_sigs(
    sigs_dir,
    days_lst,
    day_start,
    day_end,
    days_to_sampling_confs,
    times_paths,
    sig_conf_path,
    sig_names,
):
    # Make the workdir if needed.

    all_sig_paths = []
    scribe_procs = []

    print("About to scribe the predsigs")

    batch_size = 12

    # Let's make some batching now.
    scribe_cmds = []
    for d_idx, one_day in enumerate(days_lst):
        time_path = times_paths[d_idx]
        vals_tgt = "vals_{}.csv".format(one_day)
        scan_out_path = os.path.join(sigs_dir, vals_tgt)
        all_sig_paths.append(scan_out_path)

        scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage FEATURES --times-path {TIME_PATH} --feature-conf {SIG_CONF_PATH} --signals-path {VALS_PATH} --sigs {SIG_NAMES}'
        scanner_cmd = scanner_cmd.format(
            SCANNER_BIN=scanner_bin,
            DATE=one_day,
            START=day_start,
            END=day_end,
            SAMPLING_CONF=days_to_sampling_confs[one_day],
            TIME_PATH=time_path,
            VALS_PATH=scan_out_path,
            SIG_CONF_PATH=sig_conf_path,
            SIG_NAMES=",".join(sig_names),
        )
        if not os.path.exists(scan_out_path):
            scribe_cmds.append(scanner_cmd)
    # Show the first one.
    if len(scribe_cmds) > 0:
        print(scribe_cmds[0])
    run_batches(scribe_cmds, batch_size)

    return all_sig_paths


# Do forward selection on these here signals.
def lair_fwd_spawn_signals(
    lair_work_dir,
    traded_symbol,
    traded_markets,
    wanted_sig_params,
    sig_val_paths,
    ret_paths,
    reg_dct,
    num_sigs_wanted,
):
    num_sigs_wanted = min(num_sigs_wanted, len(wanted_sig_params))

    stats_dct = {
        "final_r2": 0,
        "num_sigs": 0,
        "ret_percentiles": [],
        "scores_per_round": [],
        "sigs_to_scores": [],
    }

    chosen_signal_idxs = []

    golden_sig_vals = None

    sig_names = list(range(len(wanted_sig_params)))

    rets, cand_sig_vals = load_aligned_rets_and_sigs(
        ret_paths, sig_val_paths, ["ret"], sig_names
    )

    # Grab the return percentiles.
    perc_dct = {}
    for one_percentile in [1, 10, 25, 50, 75, 99]:
        perc_dct[one_percentile] = np.percentile(rets, one_percentile)
    stats_dct["ret_percentiles"] = perc_dct

    best_r2 = 0

    sig_bounds = []

    # What if I did a combined regression?
    # This is the max R2 we could get.
    reg = LinearRegression(fit_intercept=False).fit(cand_sig_vals, rets)
    score = reg.score(cand_sig_vals, rets)

    print("At the start of CoefLair, max r2 is: {}".format(score))

    # Now, do forward selection.
    for num_chosen in range(num_sigs_wanted):
        best_r2_this_round = best_r2
        best_idx_this_round = -1
        bound_this_round = 1e10

        # print("Starting round {} of selection, best r2 baseline is {}".format(num_chosen, best_r2_this_round))

        for sig_idx in range(len(wanted_sig_params)):
            # Check if we can
            if sig_idx in chosen_signal_idxs:
                continue

            # I think I might need to copy() this.
            candidate_reg = cand_sig_vals[:, sig_idx].copy()
            candidate_reg = candidate_reg.reshape(len(candidate_reg), 1)

            # Some of these are very large bounds. But they might affect things.
            cand_sig_bound = candidate_reg.std() * 180
            # sig_bounds.append(cand_sig_bound)
            candidate_reg[candidate_reg > cand_sig_bound] = 0
            candidate_reg[candidate_reg < -cand_sig_bound] = 0

            if golden_sig_vals is not None:
                candidate_reg = np.concatenate([golden_sig_vals, candidate_reg], axis=1)

            # Regress them and check the score.
            reg = LinearRegression(fit_intercept=False).fit(candidate_reg, rets)
            score = reg.score(candidate_reg, rets)

            # print("Checking combined reg with signal {} ({}), score is {}".format(sig_idx, wanted_sig_params[sig_idx]["classname"],  score))
            if score > best_r2_this_round:
                best_r2_this_round = score
                best_idx_this_round = sig_idx
                bound_this_round = cand_sig_bound

        # OK.
        if best_idx_this_round == -1:
            # Then we gotta stop
            print(
                "Stopped at {}/{} chosen sigs, cannot increase r2 no more".format(
                    num_chosen, num_sigs_wanted
                )
            )
            break

        # Otherwise, add it. Update the r2, chosen_candidates, and the golden vals.
        best_r2 = best_r2_this_round
        chosen_signal_idxs.append(best_idx_this_round)

        this_round_sigvals = cand_sig_vals[:, best_idx_this_round].copy()
        this_round_sigvals = this_round_sigvals.reshape(len(this_round_sigvals), 1)

        sig_bounds.append(bound_this_round)

        # Apply bounding and append to the bounds.
        # Note: this is not the best thing to do.
        # The ideal thing is to apply this bounding in the earlier part, so
        # the scoring takes it into account too. But I'm ok... with
        # being a little lazy here.
        # TODO: go back and do the right thing later.
        # Currently giving it a huge bound.

        # Be careful to append this to the right side. Because we are also
        # appending the best_signal_idx to the right side too.
        # They need to correspond to each other.
        if golden_sig_vals is None:
            golden_sig_vals = this_round_sigvals
        else:
            golden_sig_vals = np.concatenate(
                [golden_sig_vals, this_round_sigvals], axis=1
            )

        print("Chose sig {}, r2 is {}".format(best_idx_this_round, best_r2))

        # Update stats dict.
        stats_dct["num_sigs"] += 1
        stats_dct["final_r2"] = best_r2
        stats_dct["scores_per_round"].append(best_r2)
    # OK, so now we should have a set of signals and their idxs.
    # Let's calculate their sds.

    # Finally, we have the last regression.
    final_reg = LinearRegression(fit_intercept=False).fit(golden_sig_vals, rets)

    final_coefs = final_reg.coef_.tolist()[0]

    # print("Coefs for the final regress is like ")
    # print(final_coefs)

    # Form the set of signals.
    chosen_signal_dicts = []
    chosen_signal_names = []
    chosen_sig_params = []

    for counter, chosen_signal_idx in enumerate(chosen_signal_idxs):
        # Get the sigparam
        sig_param = wanted_sig_params[chosen_signal_idx]
        chosen_sig_params.append(sig_param)

        # Create the signal object.
        one_sig_label = sig_param["symbol"]
        sig_obj = load_sigspec_from_cache(
            sig_param, one_sig_label, prefix="lair_{}".format(counter)
        )

        sig_dct = sig_obj.apply_varied_params(
            sig_param["param_vals"], suffix="_{}".format(counter)
        )
        chosen_signal_dicts += sig_dct["signals_dict"]["signals"]
        chosen_signal_names.append(sig_dct["name"])
        stats_dct["sigs_to_scores"].append(
            "{} : {}".format(sig_dct["name"], stats_dct["scores_per_round"][counter])
        )

    # Form the linear signal.
    lin_overrides = {
        "top_level_signal": {
            "sig_names": chosen_signal_names,
            "offset": 0,
        }
    }

    # Maybe at some point we want to keep these intercepts.
    if False:
        lin_overrides["top_level_signal"]["intercept"] = final_reg.intercept_[0]

    for idx, one_sig_name in enumerate(chosen_signal_names):
        lin_overrides["top_level_signal"]["{}_coef".format(one_sig_name)] = final_coefs[
            idx
        ]
        lin_overrides["top_level_signal"]["{}_bound".format(one_sig_name)] = sig_bounds[
            idx
        ]

    my_siglinear = SigLinearSpec(
        "pred_model_{}".format(traded_symbol),
        symbol="",
        markets=[],
        traded_symbol="",
        traded_markets="",
        fixed_val_overrides=lin_overrides,
    ).apply_varied_params({})

    pred_sig_lst = my_siglinear["signals_dict"]["signals"]

    # Now, go through old signals and add them.
    pred_sig_lst += chosen_signal_dicts

    pred_sig_dct = {"signals": pred_sig_lst}

    # Write the pred sig.
    pred_sig_path = os.path.join(lair_work_dir, "pred_sig.json")
    with open(pred_sig_path, "w") as f:
        f.write(json.dumps(pred_sig_dct, indent=2))

    # Write the ref sig too.

    ref_sig = SigMidSpec(
        "ref_{}".format(traded_symbol),
        symbol=traded_symbol,
        markets=traded_markets,
        fixed_val_overrides={},
        climbables={},
    )
    ref_sig_stuff = ref_sig.apply_varied_params({})
    ref_sig_path = os.path.join(lair_work_dir, "ref_sig.json")

    with open(ref_sig_path, "w") as f:
        f.write(json.dumps(ref_sig_stuff["signals_dict"], indent=2))

    return {
        "pred_path": pred_sig_path,
        "pred_name": my_siglinear["name"],
        "ref_path": ref_sig_path,
        "ref_name": ref_sig_stuff["name"],
        "stats": stats_dct,
        "chosen_sig_params": chosen_sig_params,
    }


# david_signed_penalty_lasso
# Can use this with scipy's minimize.
def custom_loss_with_lasso(beta, X, y, alpha, penalty_mult):
    predictions = X @ beta
    residuals = predictions - y

    # Define a penalty for sign mismatch between predicted and true values
    sign_mismatch_penalty = np.where(np.sign(predictions) != np.sign(y), penalty_mult, 1.0)

    # Penalize the residuals more when signs don't match
    weighted_errors = sign_mismatch_penalty * residuals**2

    # L1 (Lasso) regularization term
    lasso_term = alpha * np.sum(np.abs(beta))  # L1 norm of the coefficients

    # Total loss is the weighted errors plus the Lasso penalty
    return np.sum(weighted_errors) + lasso_term


def lair_lasso_spawn_signals(
    lair_work_dir,
    traded_symbol,
    traded_markets,
    wanted_sig_params,
    sig_val_paths,
    ret_paths,
    reg_style,
    # The dict that this all comes from.
    reg_dct,
    #num_sigs_wanted,
    #reg_alpha=2e-6,
    use_post_ols_coef=False,
    # Revert these later I guess.
    med_std_limit=0.05,
    mean_std_limit=0.05,
    corr_limit=0.005,
    signed_corr_limit=-1,
    reference_symbol={},
    #################
):
    stats_dct = {
        "final_r2": 0,
        "num_sigs": 0,
        "ret_percentiles": {},
        # Don't care for either.
        "scores_per_round": [],
        "sigs_to_scores": [],
    }
    print("Running lasso, appending stuff")

    markout_mult_str = reg_dct.get("markout_mults", "")
    ret_names = ["ret"]
    if markout_mult_str != "":
        ret_names = markout_mult_str.split(",")

    sig_names = list(range(len(wanted_sig_params)))

    rets, cand_sig_vals = load_aligned_rets_and_sigs(
        ret_paths, sig_val_paths, ret_names, sig_names
    )

    # Grab the return percentiles.
    perc_dct = {}
    for one_percentile in [1, 10, 25, 50, 75, 99]:
        perc_dct[one_percentile] = np.percentile(rets, one_percentile)
    stats_dct["ret_percentiles"] = perc_dct

    if cand_sig_vals.shape[1] != len(wanted_sig_params):
        raise ValueError(
            "Mismatch in sig vals and wanted sig params: Scribed out {} sigs but expected {}".format(
                cand_sig_vals.shape[1], len(wanted_sig_params)
            )
        )

    #################################################################
    # Blacklist symbols due to bias and othe rish.
    # V1 is to just have this done through aggregate stats
    # V2 is to apply daily filters too.

    passed_filter_sigs = range(len(wanted_sig_params))
    blacklisted_sigs = []

    # This is the thing we end up seeing actually.
    # OK. I'm going to add a few more filters into this.

    # Percentile filter: compare 25 and 75 percentile, 10 and 90, 1 and 99, and check that they are within a factor of 5 from each other.
    # Bias filter: if 25th percentile positive or 75 percentile negative, bad.
    passed_count = 0
    # Go through all of them and apply basic filters.

    for i in range(len(wanted_sig_params)):
        this_sig_vals = cand_sig_vals[:, i]

        cn = wanted_sig_params[i]["classname"]

        try:
            blacklisted = not sig_passes_imba_filter(
                this_sig_vals,
                rets,
                med_std_limit,
                mean_std_limit,
                corr_limit,
                signed_corr_limit,
            )
        except Exception as e:
            blacklisted = False
            print("Got some exception w/ blacklisting, error is {}".format(e))

        if blacklisted:
            blacklisted_sigs.append(i)
        else:
            #    print("Passed signal {} (idx {} {}): stats were {} and {} | med {} std {} percentiles {}\n".format(passed_count, i, cn, med/std, mean/std, med, std, json.dumps(pctile_to_val, indent=2)))
            #    #print(json.dumps(wanted_sig_params[i], indent=2))
            passed_count += 1

    # Go through and find the kept ones.
    filtered_sig_idxs = [
        i for i in range(len(wanted_sig_params)) if i not in blacklisted_sigs
    ]

    # Do it with the sig_params and sig_vals also.
    filtered_sig_params = [wanted_sig_params[idx] for idx in filtered_sig_idxs]
    filtered_sig_vals = cand_sig_vals[:, filtered_sig_idxs]

    filtered_sig_classes = []
    blacklisted_sig_classes = []
    for idx, sig_param in enumerate(wanted_sig_params):
        if idx in filtered_sig_idxs:
            filtered_sig_classes.append(
                sig_param.get("signame_key", "couldnt_find_name")
            )
        else:
            blacklisted_sig_classes.append(
                sig_param.get("signame_key", "couldnt_find_name")
            )

    # print("Hatchery: filtered out the following sigs: {}".format(blacklisted_sig_classes))
    print("Hatchery: filtered out {} sigs".format(len(blacklisted_sig_classes)))

    # Both bound things in the regression, as well as inside the linear signal.

    #################################################################

    print("Done appending")

    # Could be SKLEARN or SCIKIT_MINIMIZE
    reg_lib = "SKLEARN"

    normalized_cands = filtered_sig_vals.copy()

    n_cols = normalized_cands.shape[1]
    sig_bounds = []
    stds = []
    for i in range(n_cols):
        this_sig_vals = filtered_sig_vals[:, i]
        pctiles = [0.005, 99.995]
        sig_pctiles = np.percentile(this_sig_vals, pctiles)

        max_bound = max(abs(sig_pctiles[0]), abs(sig_pctiles[1]))

        this_sig_vals[this_sig_vals < -max_bound] = -max_bound
        this_sig_vals[this_sig_vals > max_bound] = max_bound

        sig_bounds.append(max_bound)

        cur_std = normalized_cands[:, i].std()
        stds.append(cur_std)
        if cur_std != 0:
            normalized_cands[:, i] = normalized_cands[:, i] / cur_std

    # TODO: let people select and parameterize these, from elsewhere.
    # I'll do this in another changeset now.
    # For later: this is an ok way for us to switch between different
    # filtering schemes.
    # In the future, I should pull out the filtering and regularization stuff
    # into their own, perhaps-composable, modules.
    # style = "lasso"
    # OK, let's just move ourselves to this. It's probably just better.
    if reg_style == "enetcv":
        reg_alpha = reg_dct.get("reg_alpha", 1e-6)

        alphas = np.geomspace(reg_alpha, 1e-5, 5)
        ratios = np.linspace(0.1, 0.5, 6)
        lasso_reg = ElasticNetCV(
            fit_intercept=False, alphas=alphas, l1_ratio=ratios, cv=10, max_iter=1000
        ).fit(normalized_cands, rets)
    elif reg_style == "multienet":
        # TODO:
        # Figure out the roight way of signalscribing out a
        # multitaskenet. Yes? Also, what then does it mean to have them be normalized?
        # I assume this means each column needs to be iteratively normalized.

        # defaults: alpha = 1, l1_ratio = 0.5
        alpha = reg_dct["reg_alpha"]
        l1_ratio = reg_dct["l1_ratio"]
        lasso_reg = MultiTaskElasticNet(alpha=alpha, l1_ratio=l1_ratio).fit(
            normalized_cands, rets
        )
        #score = lasso_reg.score(normalized_cands, rets)
        # Sometimes this is a list of lists and sometimes this is just the list of
        # coefs. Not sure when. But for now this is working.
        # But we might need to [0] here in the future.
        #final_coefs = lasso_reg.coef_.tolist()
    elif reg_style == "lasso":
        # default, 1e-6
        reg_alpha = reg_dct["reg_alpha"]
        lasso_reg = Lasso(fit_intercept=False, alpha=reg_alpha).fit(
            normalized_cands, rets
        )
    elif reg_style == "lars":
        num_sigs_wanted = reg_dct.get("num_sigs_wanted", 60)
        lasso_reg = Lars(fit_intercept=False, n_nonzero_coefs=num_sigs_wanted).fit(
            normalized_cands, rets
        )
    elif reg_style == "ard":

        # alpha_1: shape parameter for the Gamma distribution prior over the alpha parameter
        # alpha_2: inverse scale parameter (rate parameter) for the Gamma distribution prior over the alpha parameter.
        # lambda_1: shape parameter for the Gamma distribution prior over the lambda parameter
        # lambda_2: inverse scale parameter (rate parameter) for the Gamma distribution prior over the lambda parameter
        # How to choose?
        # ARD is also known in the literature as Sparse Bayesian Learning and Relevance Vector Machine
        # https://scikit-learn.org/stable/modules/linear_model.html#bayesian-regression
        # Look here to check out what alpha and lambda corresponds to.
        # ARD regression tends to set coefficients to 0 if needed.
        # defaults
        #    max_iter=300,
        # Precision param for prior distribution of weights. Larger alpha_1 means sparser results
        #    alpha_1=1e-06,
        #    alpha_2=1e-06,
        #    lambda_1=1e-06,
        #    lambda_2=1e-06,
        # If data is noisy, you should decrease lambdas.

        # If want stronger sparsity, increase the two alphas.

        alpha_1 = reg_dct["alpha_1"]
        alpha_2 = reg_dct["alpha_2"]

        # These are actually the l1 and l2 penalization values.
        lambda_1 = reg_dct["lambda_1"]
        lambda_2 = reg_dct["lambda_2"]

        lasso_reg = ARDRegression(
            alpha_1=alpha_1,
            alpha_2=alpha_2,
            lambda_1=lambda_1,
            lambda_2=lambda_2,
            fit_intercept=False,
        ).fit(normalized_cands, rets)
        sys.exit("Unsupported right now, sorry. ")
    elif reg_style == "bayesianridge":
        # But somehow, bayesianridge works while ard doesn't? OK.

        # I'm not sure how this is different from ARD I guess. Since this seems to have two alphas
        # and two lambdas either.
        # Shapes for the gamma distributions.
        # https://scikit-learn.org/stable/modules/linear_model.html#bayesian-regression
        # Look here when you're actually implementing stuff.

        # defaults
        #    alpha_1=1e-06,
        #    alpha_2=1e-06,
        #    lambda_1=1e-06,
        #    lambda_2=1e-06,
        alpha_1 = reg_dct["alpha_1"]
        alpha_2 = reg_dct["alpha_2"]
        lambda_1 = reg_dct["lambda_1"]
        lambda_2 = reg_dct["lambda_2"]

        lasso_reg = BayesianRidge(
            alpha_1=alpha_1,
            alpha_2=alpha_2,
            lambda_1=lambda_1,
            lambda_2=lambda_2,

        ).fit(normalized_cands, rets)

    elif reg_style == "thielsen":

        # Supposedly a technique for robust regression.
        # max_subpopulation:  consider only a stochastic subpopulation of a given maximal size if ‘n choose k’ is larger than max_subpopulation
        # n_subsamples Number of samples to calculate the parameters. This is at least the number of features (plus 1 if fit_intercept=True) and the number of samples as a maximum. A lower number leads to a higher breakdown point and a low efficiency while a high number leads to a low breakdown point and a high efficiency

        max_subpopulation = reg_dct["max_subpopulation"]
        n_subsamples = reg_dct["n_subsamples"]
        # defaults
        #    max_subpopulation=10000.0,
        #    n_subsamples=None,

        lasso_reg = TheilSenRegressor(
            max_subpopulation=max_subpopulation,
            n_subsamples=n_subsamples,
        ).fit(normalized_cands, rets)

    elif reg_style == "omp":
        omp_limit = reg_dct.get("num_wanted_signals", 60)
        omp_limit = min(omp_limit, max(10, len(filtered_sig_params) - 10))
        lasso_reg = OrthogonalMatchingPursuit(
            n_nonzero_coefs=omp_limit, fit_intercept=False
        ).fit(normalized_cands, rets)

    elif reg_style == "huber":
        # It feels like the alpha here needs to be on the order of magnitude of error. So like, probably
        # on the level of 10s of bps.
        reg_alpha = reg_dct["reg_alpha"]

        lasso_reg = HuberRegressor(
            epsilon=1.01, alpha=reg_alpha, fit_intercept=False
        ).fit(normalized_cands, rets)
    elif reg_style == "sgd":
        sys.exit("You probably didn't mean to do sgd because this is not done correctly. Try again later. ")

        # loss: squared_error, huber, epsilon_insensitive, squared_epsilon_insensitive

        # penalty: l1, l2, elasticnet
        # alpha: multiplies the reg. term. default = 0.0001
        # l1_ratio: enet mixing ratio
        # shuffle: reshuffle every iteration
        # Not including the others.

        # defaults
        #    alpha=0.5,
        #    l1_ratio=0.15,
        loss=reg_dct["loss"]
        reg_alpha = reg_dct["reg_alpha"]
        epsilon = reg_dct.get("epsilon", 5e-5)

        # Let's always just do elasticnet.
        penalty="elasticnet"
        l1_ratio = 0.5

        lasso_reg = SGDRegressor(
            loss=loss,
            penalty=penalty,
            alpha=reg_alpha,
            l1_ratio=l1_ratio,
            epsilon=epsilon,
            learning_rate="optimal",
            fit_intercept=False,
            shuffle=True,
        ).fit(normalized_cands, rets)

        print(lasso_reg)
        score = lasso_reg.score(normalized_cands, rets)
        print(score)
        sys.exit()
    elif reg_style == "david_signed_penalty_lasso":
        beta_init = np.zeros(normalized_cands.shape[1])
        reg_alpha = reg_dct["reg_alpha"]
        minimize_result = minimize(custom_loss_with_lasso, beta_init, args=(normalized_cands, rets, reg_alpha), method='BFGS')
        reg_lib = "SCIKIT_MINIMIZE"
    else:
        sys.exit("Could not support reg_style {}".format(reg_style))

    if reg_lib == "SKLEARN":
        score = lasso_reg.score(normalized_cands, rets)
        # Sometimes this is a list of lists and sometimes this is just the list of
        # coefs. Not sure when. But for now this is working.
        # But we might need to [0] here in the future.
        final_coefs = lasso_reg.coef_.tolist()

        if reg_style == "multienet":
            # This is a list of lists.
            final_coefs = final_coefs[0]
    elif reg_lib == "SCIKIT_MINIMIZE":
        # Then we have a different way of fetching these.

        # We are using the value of the optimizer then, not the actual r2.
        score = minimize_result.fun
        final_coefs = minimize_result.x.tolist()


    #score = lasso_reg.score(normalized_cands, rets)
    stats_dct["final_r2"] = score
    print("CoefLair Lasso, r2 is: {}".format(score))

    # Right. OK. We take all of these guys. We divide them by stds to run lasso.

    # Can I just make sure I get all these too?
    post_lasso_sig_vals = []
    post_lasso_coefs = []

    # Form the set of signals.
    chosen_signal_dicts = []
    chosen_signal_names = []
    chosen_signal_coefs = []
    chosen_sig_bounds = []

    # A little annoying because this is a subset of
    # wanted_sig_params, which is a bit similarly named. Nonetheless.
    chosen_sig_params = []

    chosen_sig_idxs = []
    chosen_sig_orig_coefs = []
    chosen_sig_std = []
    for chosen_signal_idx, coef in enumerate(final_coefs):
        # Ignore sigs that lasso selects against.
        if abs(coef) < 1e-9:
            continue

        chosen_sig_idxs.append(chosen_signal_idx)
        chosen_sig_orig_coefs.append(coef)
        chosen_sig_std.append(stds[chosen_signal_idx])

        # Undo the column standardization.
        # Since the normalized column is col / std,
        rescaled_coef = coef
        if stds[chosen_signal_idx] != 0:
            rescaled_coef = coef / stds[chosen_signal_idx]
        # Get the sigparam
        sig_param = filtered_sig_params[chosen_signal_idx]
        chosen_sig_params.append(sig_param)

        one_sig_vals = filtered_sig_vals[:, chosen_signal_idx]
        one_sig_vals = one_sig_vals.reshape(len(one_sig_vals), 1)
        post_lasso_sig_vals.append(one_sig_vals)
        post_lasso_coefs.append(rescaled_coef)

        # Create the signal object.
        one_sig_label = sig_param["symbol"]
        sig_obj = load_sigspec_from_cache(
            sig_param, one_sig_label, prefix="lair_{}".format(chosen_signal_idx)
        )

        sig_dct = sig_obj.apply_varied_params(
            sig_param["param_vals"], suffix="_{}".format(chosen_signal_idx)
        )
        chosen_signal_dicts += sig_dct["signals_dict"]["signals"]
        chosen_signal_names.append(sig_dct["name"])
        chosen_signal_coefs.append(rescaled_coef)
        chosen_sig_bounds.append(sig_bounds[chosen_signal_idx])

    # Form the linear signal.
    lin_overrides = {
        "top_level_signal": {
            "sig_names": chosen_signal_names,
            "offset": 0,
        }
    }

    print("Num chosen: {}".format(len(post_lasso_coefs)))
    # Sidebar. Let's do a regression here then.
    prediction = None
    for idx, sig_val_vec in enumerate(post_lasso_sig_vals):
        if prediction is None:
            prediction = sig_val_vec * post_lasso_coefs[idx]
        else:
            prediction += sig_val_vec * post_lasso_coefs[idx]

    # Can I show prediction stats?
    pred_df = pd.DataFrame(prediction)
    # print("Lasso prediction stats")
    # print(pred_df.describe(percentiles=[.005, .01, .1, .25, .5, .75, .9, .99, .995]))

    # Don't need this anymore, we get the score from elsewhere.
    #score = r2_score(rets, prediction)
    print("The regression's R2 is {}".format(score))

    prediction = prediction.reshape(len(prediction), 1)
    checker_reg = LinearRegression(fit_intercept=False).fit(prediction, rets)

    # What about the coef?
    coef = checker_reg.coef_.tolist()[0]
    print("The inv beta is {}".format(coef))

    # What if I did a secondary regression on top of those?
    post_lasso_sig_vals_nparr = np.concatenate(post_lasso_sig_vals, axis=1)

    second_ols = sm.OLS(rets, post_lasso_sig_vals_nparr)
    second_ols_fit = second_ols.fit()
    # second_ols = LinearRegression(fit_intercept=False).fit(
    #    post_lasso_sig_vals_nparr, rets
    # )

    # print("Coefs")
    # print(second_ols.coef_.tolist()[0])
    if use_post_ols_coef:
        print("Using second_ols coefficients instead of lasso:")
        # print("Old version:")
        # print(chosen_signal_coefs)
        chosen_signal_coefs = second_ols_fit.params
        # print("New version:")
        # print(chosen_signal_coefs)

    # Maybe at some point we want to keep these intercepts.
    if False:
        lin_overrides["top_level_signal"]["intercept"] = final_reg.intercept_[0]

    for idx, one_sig_name in enumerate(chosen_signal_names):
        lin_overrides["top_level_signal"]["{}_coef".format(one_sig_name)] = (
            chosen_signal_coefs[idx]
        )
        lin_overrides["top_level_signal"]["{}_bound".format(one_sig_name)] = (
            chosen_sig_bounds[idx]
        )

    my_siglinear = SigLinearSpec(
        "pred_model_{}".format(traded_symbol),
        symbol="",
        markets=[],
        traded_symbol="",
        traded_markets="",
        fixed_val_overrides=lin_overrides,
    ).apply_varied_params({})

    pred_sig_lst = my_siglinear["signals_dict"]["signals"]

    # Now, go through old signals and add them.
    pred_sig_lst += chosen_signal_dicts

    pred_sig_dct = {"signals": pred_sig_lst}

    # Write the pred sig.
    pred_sig_path = os.path.join(lair_work_dir, "pred_sig.json")
    with open(pred_sig_path, "w") as f:
        f.write(json.dumps(pred_sig_dct, indent=2))

    # Write the ref sig too.

    mid_sym = traded_symbol
    mid_mkt = traded_markets
    if len(reference_symbol) > 0:
        mid_sym = reference_symbol["symbol"]
        mid_mkt = reference_symbol["markets"]

    ref_sig = SigMidSpec(
        "ref_{}".format(traded_symbol),
        symbol=mid_sym,
        markets=mid_mkt,
        fixed_val_overrides={},
        climbables={},
    )
    ref_sig_stuff = ref_sig.apply_varied_params({})
    ref_sig_path = os.path.join(lair_work_dir, "ref_sig.json")

    with open(ref_sig_path, "w") as f:
        f.write(json.dumps(ref_sig_stuff["signals_dict"], indent=2))

    return {
        "pred_path": pred_sig_path,
        "pred_name": my_siglinear["name"],
        "ref_path": ref_sig_path,
        "ref_name": ref_sig_stuff["name"],
        "stats": stats_dct,
        "chosen_sig_params": chosen_sig_params,
        "sig_bounds": sig_bounds,
        "chosen_idxs": chosen_sig_idxs,
        "orig_coefs": chosen_sig_orig_coefs,
        "chosen_stds": chosen_sig_std,
        "chosen_coefs": chosen_signal_coefs,
        "rets": rets,
    }
