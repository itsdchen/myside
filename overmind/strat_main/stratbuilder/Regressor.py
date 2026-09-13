#! /usr/bin/env python

"""
Redundant and I'll need to fix up roles here. Just want a place to scribe a signal and
return back an R2.

Sorry.

"""

import os
import subprocess
import sys
import json
import copy

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from scipy.stats.stats import pearsonr
from sklearn.metrics import r2_score

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from util.pathing import bindir
from util.runner import run_batches

from util.log import getLogger

scanner_bin = os.path.join(bindir(), "signalscanner")


def scribe_and_regress(
    pred_sig_path,
    pred_sig_name,
    work_dir,
    oos_sampling_confs,
    dates,
    day_start_regress="06:00:00 America/New_York",
    day_end_regress="16:00:00 America/New_York",
    overwrite_rets=False,
    overwrite_feats=False,
    decompose_linsig=False,
    scribe_densely= False,
):
    if not os.path.exists(work_dir):
        os.makedirs(work_dir)

    rets_dir = os.path.join(work_dir, "rets")
    times_dir = os.path.join(work_dir, "times")
    sig_dir = os.path.join(work_dir, "sigs")

    if not os.path.exists(rets_dir):
        os.mkdir(rets_dir)

    if not os.path.exists(times_dir):
        os.mkdir(times_dir)

    if not os.path.exists(sig_dir):
        os.mkdir(sig_dir)

    # If we decompose the linsig, then scribe in all the sig names too.
    sig_lin_dct = {}
    sig_linear_names = []
    sig_linear_coefs = []
    if decompose_linsig:
        # Read in the pred sig.
        pred_sig_dct = json.load(open(pred_sig_path, "r"))
        for one_sig_dct in pred_sig_dct["signals"]:
            # Check for siglinear
            if one_sig_dct["type"] == "SigLinear":
                sig_lin_dct = one_sig_dct
                break
        # Go through and populate.
        for one_sig_name in sig_lin_dct["sig_names"]:
            sig_linear_names.append(one_sig_name)
            coef_name = "{}_coef".format(one_sig_name)
            if coef_name in sig_lin_dct:
                sig_linear_coefs.append(sig_lin_dct[coef_name])
            else:
                # shouldn't happen but o well.
                sig_linear_coefs.append(0.0)

    times_paths = []
    rets_paths = []
    all_val_outs = []
    procs = []

    if scribe_densely:
        for one_day in dates:
            returns_path = os.path.join(rets_dir, "returns_{}.csv".format(one_day))
            rets_paths.append(returns_path)

            vals_tgt = "vals_{}.csv".format(one_day)
            scan_out_path = os.path.join(sig_dir, vals_tgt)
            all_val_outs.append(scan_out_path)

            csnames = pred_sig_name
            if decompose_linsig:
                csnames = "{},{}".format(pred_sig_name, ",".join(sig_linear_names))

            scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage DENSE  --feature-conf {SIG_CONF_PATH} --signals-path {SIGNALS_PATH}  --returns-path {RETURNS_PATH}  --sigs {SIG_NAMES}'
            scanner_cmd = scanner_cmd.format(
                SCANNER_BIN=scanner_bin,
                DATE=one_day,
                START=day_start_regress,
                END=day_end_regress,
                SAMPLING_CONF=oos_sampling_confs[one_day],
                SIG_CONF_PATH=pred_sig_path,
                SIGNALS_PATH=scan_out_path,
                RETURNS_PATH=returns_path,
                SIG_NAMES=csnames,
            )

            proc = subprocess.Popen(scanner_cmd, shell=True)
            procs.append(proc)

        for one_proc in procs:
            one_proc.wait()

        pass
    else:
        for one_day in dates:
            time_path = os.path.join(times_dir, "times_{}.csv".format(one_day))
            returns_path = os.path.join(rets_dir, "returns_{}.csv".format(one_day))

            scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage RETURNS --times-path {TIME_PATH} --returns-path {RETURN_PATH}'
            scanner_cmd = scanner_cmd.format(
                SCANNER_BIN=scanner_bin,
                DATE=one_day,
                START=day_start_regress,
                END=day_end_regress,
                SAMPLING_CONF=oos_sampling_confs[one_day],
                TIME_PATH=time_path,
                RETURN_PATH=returns_path,
            )

            times_paths.append(time_path)
            rets_paths.append(returns_path)

            if (not overwrite_rets) and (
                os.path.exists(time_path) and os.path.exists(returns_path)
            ):
                # print(scanner_cmd)
                continue

            proc = subprocess.Popen(scanner_cmd, shell=True)
            procs.append(proc)

        for one_proc in procs:
            one_proc.wait()

        # Now scribe the target sig.
        procs = []

        csnames = pred_sig_name
        if decompose_linsig:
            csnames = "{},{}".format(pred_sig_name, ",".join(sig_linear_names))

        # Batched regressor scribing.
        batch_size = 8

        scribe_cmds = []
        for one_day in dates:
            time_path = os.path.join(times_dir, "times_{}.csv".format(one_day))
            returns_path = os.path.join(
                rets_dir, "returns_{}.csv".format(one_day)
            )
            vals_tgt = "vals_{}.csv".format(one_day)
            scan_out_path = os.path.join(sig_dir, vals_tgt)
            all_val_outs.append(scan_out_path)
            scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage FEATURES --times-path {TIME_PATH} --feature-conf {SIG_CONF_PATH} --signals-path {VALS_PATH} --sigs {SIG_NAMES}'
            scanner_cmd = scanner_cmd.format(
                SCANNER_BIN=scanner_bin,
                DATE=one_day,
                START=day_start_regress,
                END=day_end_regress,
                SAMPLING_CONF=oos_sampling_confs[one_day],
                TIME_PATH=time_path,
                VALS_PATH=scan_out_path,
                SIG_CONF_PATH=pred_sig_path,
                SIG_NAMES=csnames,
            )

            if (not overwrite_feats) and (os.path.exists(scan_out_path)):
                continue

            scribe_cmds.append(scanner_cmd)
        run_batches(scribe_cmds, batch_size)


    rets_list = []
    vals_list = []

    # Aggregate
    for one_day in dates:
        ret_path = os.path.join(rets_dir, "returns_{}.csv".format(one_day))

        vals_tgt = "vals_{}.csv".format(one_day)
        vals_path = os.path.join(sig_dir, vals_tgt)

        ret = pd.read_csv(ret_path, names=["ret"]).values
        val = pd.read_csv(vals_path, names=["sig"] + sig_linear_names).values

        rets_list.append(ret)
        vals_list.append(val)

    # Concat all of them.
    all_rets = np.concatenate(rets_list)
    all_vals = np.concatenate(vals_list)

    pred_vals = all_vals[:, 0]
    sig_vals = all_vals[:, 1:]
    sig_coefs = np.array(sig_vals)

    pred_vals = pred_vals.reshape(len(pred_vals), 1)

    cur_fit = LinearRegression(fit_intercept=False).fit(pred_vals, all_rets)
    # OK, I guess I should figure out the coef too.
    inv_beta = cur_fit.coef_.tolist()[0]
    old_r2 = cur_fit.score(pred_vals, all_rets)
    # This shouldn't be a fit score.
    r2 = r2_score(all_rets, pred_vals)
    print("Just to check. Regress(r2 inv_beta) ({} {}) straight r2_score {}".format(old_r2, inv_beta, r2))

    return {
        "score": r2,
        "val_paths": all_val_outs,
        "rets": all_rets,
        "pred_vals": pred_vals,
        "pred_inv_beta": inv_beta,
        "sig_vals": sig_vals,
        "sig_names": sig_linear_names,
        "sig_coefs": sig_linear_coefs,
    }


# I really have 2 questions I want to solve.

# 1 - if I reregress a set of signals on this period, what are the
# new coefficients

# 2 - If I take the current signals and iteratively remove them,
# what is the best R2 I can get?


# Given a set of signals, remove 1 signal at a time and see how the R2 changes.
# Later on, we can remove multiple of them (greedily) and see
# how that affects OOS R2 Subtractive stuff is not helping.
# It might be too hard. I might need to go additive.
# OK. This doesn't seem to help. Let's go additive instead.
def greedily_remove_sigs(sigs_arr, sig_names, sig_coefs, rets):
    coefs_arr = np.array(sig_coefs)

    best_r2_so_far = -10
    best_r2_this_round = -10
    sigs_removed = []
    sigs_removed_this_round = []

    best_sig_idx_this_round = -1

    # I gues this does the multiplying by each column property.
    transformed_sigs = sigs_arr * coefs_arr

    full_columns = range(len(sig_names))

    # What if I remove none?
    print("At the start")
    preds = transformed_sigs.dot(np.ones(len(sig_names)))
    preds = preds.reshape(len(preds), 1)
    cur_fit = LinearRegression(fit_intercept=False).fit(preds, rets)
    r2 = cur_fit.score(preds, rets)
    print("DO-NOTHING R2: {}".format(r2))

    # To start,
    for i, sig_name in enumerate(sig_names):
        # Generate the subsetted array.

        if i in sigs_removed:
            continue

        sigs_to_keep = range(len(sig_names))
        sigs_removed_this_round = sigs_removed
        sigs_removed_this_round.append(i)
        sigs_to_keep = [a for a in sigs_to_keep if a not in sigs_removed_this_round]

        sub_sigs_arr = transformed_sigs[:, sigs_to_keep]
        # OK, I guess we can
        preds = sub_sigs_arr.dot(np.ones(len(sigs_to_keep)))
        preds = preds.reshape(len(preds), 1)

        cur_fit = LinearRegression(fit_intercept=False).fit(preds, rets)
        r2 = cur_fit.score(preds, rets)
        if r2 > best_r2_this_round:
            best_r2_this_round = r2
            best_sig_idx_this_round = i

    print("{}: {}".format(sig_names[best_sig_idx_this_round], best_r2_this_round))

    pass


def greedily_add_sigs(sigs_arr, sig_names, sig_coefs, rets):
    coefs_arr = np.array(sig_coefs)

    best_r2_so_far = -10
    best_r2_this_round = -10
    sigs_added = []
    sigs_added_this_round = []

    best_sig_idx_this_round = -1

    # I gues this does the multiplying by each column property.
    transformed_sigs = sigs_arr * coefs_arr

    full_columns = range(len(sig_names))

    # What if I remove none?
    print("At the start")
    preds = transformed_sigs.dot(np.ones(len(sig_names)))
    preds = preds.reshape(len(preds), 1)
    cur_fit = LinearRegression(fit_intercept=False).fit(preds, rets)
    r2 = cur_fit.score(preds, rets)
    print("DO-NOTHING R2: {}".format(r2))

    # To start,
    for j in range(len(sig_names)):
        best_r2_this_round = best_r2_so_far
        for i, sig_name in enumerate(sig_names):
            # Generate the subsetted array.

            if i in sigs_added:
                continue

            sigs_added_this_round = sigs_added.copy()
            sigs_added_this_round.append(i)

            sub_sigs_arr = transformed_sigs[:, sigs_added_this_round]
            # OK, I guess we can
            preds = sub_sigs_arr.dot(np.ones(len(sigs_added_this_round)))
            preds = preds.reshape(len(preds), 1)

            cur_fit = LinearRegression(fit_intercept=False).fit(preds, rets)
            r2 = cur_fit.score(preds, rets)
            if r2 > best_r2_this_round:
                best_r2_this_round = r2
                best_sig_idx_this_round = i

        if best_r2_so_far == best_r2_this_round:
            print("Breaking on {}".format(j))
            break
        sigs_added.append(best_sig_idx_this_round)
        best_r2_so_far = best_r2_this_round
        print(
            "Round {}: {}: {}".format(
                j, sig_names[best_sig_idx_this_round], best_r2_this_round
            )
        )

    # What about all the signals that weren't chosen?

    pass


# OK, let's just check a few stats for the signals?
# For now,
def basic_stats(sigs_arr, sig_names, sig_coefs, rets, print_n=100):
    stats = []
    for i, sig_name in enumerate(sig_names):
        this_sig_vals = sigs_arr[:, i]
        med = np.median(this_sig_vals)
        mean = this_sig_vals.mean()
        std = this_sig_vals.std()
        corr = pearsonr(this_sig_vals, rets.ravel()).statistic
        stats.append(
            {
                "name": sig_name,
                "med": med,
                "mean": mean,
                "std": std,
                "corr": corr,
                "scaled_mean": mean / std,
            }
        )

    stats.sort(key=lambda x: x["scaled_mean"])

    # Print top and lowest.
    print("Negatives")
    for s in stats[:print_n]:
        print(
            "{:50}: mn {: .4f} md {: .4f} corr {: .4f}".format(
                s["name"],
                s["mean"] / s["std"],
                s["med"] / s["std"],
                s["corr"]
            )
        )

    print("")
    print("Positives")
    for s in stats[-print_n:]:
        print(
            "{:50}: mn {: .4f} md {: .4f} corr {: .4f}".format(
                s["name"],
                s["mean"] / s["std"],
                s["med"] / s["std"],
                s["corr"]
            )
        )

    return stats



# Gives you a bundle of stats re: the prediction.
def pred_ideal_stats(pred_vals, ret_vals, use_intercept=False):
    init_r2 = r2_score(ret_vals, pred_vals)

    cur_fit = LinearRegression(fit_intercept=use_intercept).fit(pred_vals, ret_vals)
    r2 = cur_fit.score(pred_vals, ret_vals)

    inv_beta = cur_fit.coef_.tolist()[0]
    offset = cur_fit.intercept_
    return {
        "init_r2": init_r2,
        "regressed_r2": r2,
        "inv_beta": inv_beta,
        "offset": offset
    }

