#! /usr/bin/env python

"""
Kind of like a coefficientHatchery.
Operates on the outputs of the modelclimb, creates a linear model file.

"""

import os
import subprocess
import sys
import json

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

from templatizers.SigFormer import *

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)


# work_dir is where we write out the final models.
def regress_final_model(
    work_dir,
    rets_dir,
    dates,
    golden_sig_names,
    golden_sig_paths,
    golden_sigs_to_vals,
    traded_symbol,
    traded_markets,
    fit_intercept,
    keep_intercept,
    sd_bound,
    inherited_sigval_paths,
    inherited_sig_names,
    inherited_sigs_path,
):
    # Make the work_dir if needed.

    hatch_work_dir = os.path.join(work_dir, "final_model_hatch")

    if not os.path.exists(hatch_work_dir):
        os.mkdir(hatch_work_dir)

    # Also need the returns
    rets_list = []
    for one_day in dates:
        ret_path = os.path.join(rets_dir, "returns_{}.csv".format(one_day))
        ret = pd.read_csv(ret_path, names=["ret"]).values
        rets_list.append(ret)
    all_rets = np.concatenate(rets_list)

    golden_vals_arr = None
    golden_vals_lst = []

    sigs_to_bounds = []
    wanted_sig_names = []

    stats_dct = {
        "final_r2": 0,
        "num_sigs": 0,
    }

    # Read in all the values.
    for one_sig_name in golden_sig_names:
        # Read in their values.
        one_gvs_list = []
        sig_val_paths = golden_sigs_to_vals[one_sig_name]
        for one_vals_path in sig_val_paths:
            # I should really change all these to be header=None.
            sig_vals = pd.read_csv(one_vals_path, names=["sig"]).values
            one_gvs_list.append(sig_vals)
        gvs_alldays = np.concatenate(one_gvs_list)
        # Zero out extreme days.

        # TODO: I think this might be a bug.
        # Come back and fix this.
        # Oh, no, nevermind. It's just for the specific signal.
        gv_bound = gvs_alldays.std() * sd_bound
        sigs_to_bounds.append(gv_bound)
        gvs_alldays[gvs_alldays > gv_bound] = 0
        gvs_alldays[gvs_alldays < -gv_bound] = 0

        gvs_alldays = gvs_alldays.reshape(len(gvs_alldays), 1)
        golden_vals_lst.append(gvs_alldays)

    # Concatenate them in the horizontal direction
    if len(golden_vals_lst) > 0:
        golden_vals_arr = np.concatenate(golden_vals_lst, axis=1)
    wanted_sig_names += golden_sig_names

    # Potentially include the inherited signals too.
    if len(inherited_sigval_paths) > 0:
        inherited_vals_arr = None
        inherited_vals_lst = []

        inherited_bounds = []
        for one_inherit_file in inherited_sigval_paths:
            sig_vals = pd.read_csv(one_inherit_file, names=inherited_sig_names).values
            if len(sig_vals.shape) == 1:
                sig_vals = sig_vals.reshape(len(sig_vals), 1)
            inherited_vals_lst.append(sig_vals)
        # Concat them all.
        inherited_vals_arr = np.concatenate(inherited_vals_lst)

        inherited_sd_bounds = (inherited_vals_arr.std(axis=0) * sd_bound).tolist()

        # And then add these to the golden vals arr too.
        print("Adding inherited signals to the golden signals.")
        if golden_vals_arr is None:
            golden_vals_arr = inherited_vals_arr
        else:
            golden_vals_arr = np.concatenate([golden_vals_arr, inherited_vals_arr], axis=1)

        print("Now shape is {}".format(golden_vals_arr.shape))
    wanted_sig_names += inherited_sig_names

    reg = LinearRegression(fit_intercept=fit_intercept).fit(golden_vals_arr, all_rets)

    print("Fit our signals. Coefs: {}".format(reg.coef_.tolist()[0]))
    print("Intercept: {}".format(reg.intercept_))
    # Also, just for safety, what's the score?
    print("R2: {}".format(reg.score(golden_vals_arr, all_rets)))

    stats_dct["num_sigs"] = golden_vals_arr.shape[1]
    stats_dct["final_r2"] = reg.score(golden_vals_arr, all_rets)

    # TODO: get the percentiles of the reg.predict() values.

    # Let's always ignore the intercept for now?
    # TODO: check out if we want to or not.
    lin_overrides = {
        "top_level_signal": {
            "sig_names": wanted_sig_names,
            "offset": 0,
        }
    }

    if keep_intercept:
        lin_overrides["top_level_signal"]["intercept"] = reg.intercept_[0]

    coefs = reg.coef_.tolist()[0]

    for idx, one_sig_name in enumerate(golden_sig_names):
        lin_overrides["top_level_signal"]["{}_coef".format(one_sig_name)] = coefs[idx]
        lin_overrides["top_level_signal"][
            "{}_bound".format(one_sig_name)
        ] = sigs_to_bounds[idx]

    # Add in SigLinears for inherited guys too.
    for idx2, one_sig_name in enumerate(inherited_sig_names):
        lin_overrides["top_level_signal"]["{}_coef".format(one_sig_name)] = coefs[
            len(golden_sig_names) + idx2
        ]
        lin_overrides["top_level_signal"][
            "{}_bound".format(one_sig_name)
        ] = inherited_sd_bounds[idx2]

    my_siglinear = SigLinearSpec(
        "pred_model_{}".format(traded_symbol),
        symbol="",
        markets=[],
        traded_symbol="",
        traded_markets="",
        fixed_val_overrides=lin_overrides,
    ).apply_varied_params({})

    pred_sig_lst = my_siglinear["signals_dict"]["signals"]

    # Now, go through old signals and add them too.
    for idx, one_sig_path in enumerate(golden_sig_paths):
        with open(one_sig_path, "r") as f:
            choice_sigs = json.loads(f.read())

        # Add them all to the list of signals.
        choice_sigs = choice_sigs["signals"]
        pred_sig_lst += choice_sigs

    # Also go through the inherited list and add those.
    if inherited_sigs_path != "":
        with open(inherited_sigs_path, "r") as f:
            inherit_sig_dicts = json.loads(f.read())

        # Add them all to the list of signals.
        sigs_section = inherit_sig_dicts["signals"]
        pred_sig_lst += sigs_section

    # print(my_siglinear["name"])
    # print(json.dumps(my_siglinear["signals_dict"], indent=2))

    # Also create the SigMid.

    sig_mid = SigMidSpec(
        "ref_{}".format(traded_symbol),
        symbol=traded_symbol,
        markets=traded_markets,
        fixed_val_overrides={},
        climbables={},
    )
    sig_mid_stuff = sig_mid.apply_varied_params({})

    pred_sig_dct = {"signals": pred_sig_lst}

    # OK, cool. Write them down and then return this to the parent.
    pred_sig_path = os.path.join(hatch_work_dir, "pred_sig.json")
    with open(pred_sig_path, "w") as f:
        f.write(json.dumps(pred_sig_dct, indent=2))

    ref_sig_path = os.path.join(hatch_work_dir, "ref_sig.json")
    with open(ref_sig_path, "w") as f:
        f.write(json.dumps(sig_mid_stuff["signals_dict"], indent=2))

    return {
        "pred_path": pred_sig_path,
        "pred_name": my_siglinear["name"],
        "ref_path": ref_sig_path,
        "ref_name": sig_mid_stuff["name"],
        "stats": stats_dct,
    }
