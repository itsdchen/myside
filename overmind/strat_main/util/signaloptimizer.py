#! /usr/bin/env python
"""
Set of tools to inspect and optimize individual signals.

Ported from pkt_1/overmind/strat_main/util/signaloptimizer.py.
Runs single-feature climbs: one signal class, its hyperparams climbed by
QuadClimber/ParticleClimber against single-feature R² on forward returns.
Used for "how high can this class of signal go" benchmarks.
"""

import os
import subprocess
import sys
import json
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from util.pathing import bindir

from templatizers import SigFormer
from stratbuilder import TempoRate

from optimizers.QuadClimb import QuadClimber
from optimizers.ParticleClimb import ParticleClimber
from optimizers.BayesClimb import BayesClimber
from optimizers.CmaEsClimb import CmaEsClimber


scanner_bin = os.path.join(bindir(), "signalscanner")


default_qc_specs = {
    "min_epochs": 1,
    "max_epochs": 5,
    "can_snapback": True,
    "snapback_frac": 0.92,
    "prefer_best": True,
    "expand_quad_up": True,
    "required_delta_params": 0,
    "stop_condition": {"center_pct_increase": 0.001, "pct_change_variants": 0.05},
    "params": [],
}


class SigOptimizer:
    def __init__(
        self,
        work_dir,
        sigspec_obj,
        dates,
        sampling_conf="",
        mc_dir="",
        day_start="08:00:00 America/New_York",
        day_end="18:00:00 America/New_York",
        bound_sd_mult=0,
        climb_specs={},
        climb_type="QUAD",
        markout_tgt=60,
        markout_cap=0.0035,
        tempo_type="final",
    ):
        self.mc_dir = mc_dir

        self.sigspec_obj = sigspec_obj
        self.work_dir = work_dir
        if not os.path.exists(work_dir):
            os.makedirs(work_dir)

        if sampling_conf != "":
            self.sampling_conf = sampling_conf
        else:
            self.sampling_conf = os.path.join(work_dir, "sampling.conf")

        if not os.path.exists(self.sampling_conf):
            print("Running temporate.")
            num_wanted_samples = len(dates) * 8000
            TempoRate.gen_scan_conf(
                self.sampling_conf,
                num_wanted_samples,
                markout_tgt,
                sigspec_obj.traded_symbol,
                sigspec_obj.traded_markets,
                dates,
                day_start,
                day_end,
                tempo_type,
                work_dir,
                name_prefix="",
                markout_cap=markout_cap,
            )

        self.rets_dir = os.path.join(self.mc_dir, "rets")
        self.times_dir = os.path.join(self.mc_dir, "times")
        self.dates = dates

        self.day_start = day_start
        self.day_end = day_end

        self.bound_sd_mult = bound_sd_mult

        if len(climb_specs) == 0:
            self.climb_specs = default_qc_specs
            self.climb_type = "QUAD"
        else:
            self.climb_specs = climb_specs
            self.climb_type = climb_type

        self.times_paths = []
        self.rets_paths = []

        all_times_exist = True
        all_rets_exist = True

        pot_times_dir = os.path.join(mc_dir, "times")
        for one_date in dates:
            pot_time_path = os.path.join(pot_times_dir, "times_{}.csv".format(one_date))
            if not os.path.exists(pot_time_path):
                all_times_exist = False
                break
            else:
                self.times_paths.append(pot_time_path)

        pot_rets_dir = os.path.join(mc_dir, "rets")
        for one_date in dates:
            pot_ret_path = os.path.join(pot_rets_dir, "returns_{}.csv".format(one_date))
            if not os.path.exists(pot_ret_path):
                all_rets_exist = False
                break
            else:
                self.rets_paths.append(pot_ret_path)

        if not (all_times_exist and all_rets_exist):
            all_times_exist = False
            all_rets_exist = False
            self.times_paths = []
            self.rets_paths = []

            self.rets_dir = os.path.join(self.work_dir, "rets")
            self.times_dir = os.path.join(self.work_dir, "times")

            if not os.path.exists(self.rets_dir):
                os.mkdir(self.rets_dir)
            if not os.path.exists(self.times_dir):
                os.mkdir(self.times_dir)
        self.all_times_exist = all_times_exist
        self.all_rets_exist = all_rets_exist

    def sample_and_returns(self, dates=[]):
        if len(dates) == 0:
            dates = self.dates

        procs = []
        for one_date in dates:
            time_path = os.path.join(self.times_dir, "times_{}.csv".format(one_date))
            returns_path = os.path.join(self.rets_dir, "returns_{}.csv".format(one_date))

            if os.path.exists(time_path) and os.path.exists(returns_path):
                continue
            scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage RETURNS --times-path {TIME_PATH} --returns-path {RETURN_PATH}'
            scanner_cmd = scanner_cmd.format(
                SCANNER_BIN=scanner_bin,
                DATE=one_date,
                START=self.day_start,
                END=self.day_end,
                SAMPLING_CONF=self.sampling_conf,
                TIME_PATH=time_path,
                RETURN_PATH=returns_path,
            )
            self.times_paths.append(time_path)
            self.rets_paths.append(returns_path)

            scanner_proc = subprocess.Popen(scanner_cmd, shell=True)
            procs.append(scanner_proc)

        for one_proc in procs:
            one_proc.wait()

        self.all_times_exist = True
        self.all_rets_exist = True

    def climb_sigspec(self):
        self.sample_and_returns()

        climb_workdir = os.path.join(self.work_dir, "climb")
        if not os.path.exists(climb_workdir):
            os.mkdir(climb_workdir)

        climber = None
        if self.climb_type == "QUAD":
            climber = QuadClimber()
        elif self.climb_type == "PARTICLE":
            climber = ParticleClimber()
        elif self.climb_type == "BAYES":
            climber = BayesClimber()
        elif self.climb_type == "CMAES":
            climber = CmaEsClimber()
        else:
            raise Exception("Cannot support climb type {}".format(self.climb_type))

        climb_specs = self.climb_specs
        climb_specs["params"] = self.sigspec_obj.get_varying_params()
        climber.init_from_json(climb_specs)

        for i in range(climber.max_epochs):
            next_params = climber.get_params_next_epoch()["params_and_scores"]

            print("Running iter {} of climb, max_epochs {}.".format(i, climber.max_epochs))
            sigs_to_scan = []
            signames_to_scan = []

            for suff_counter, one_sig_version in enumerate(next_params):
                suff = "_{}".format(suff_counter)
                sig_varying_params = {}
                for one_varying_param_dict in one_sig_version[
                    "ordered_param_instances"
                ]:
                    sig_varying_params[
                        one_varying_param_dict["name"]
                    ] = one_varying_param_dict["projection_value"]

                sig_dict = self.sigspec_obj.apply_varied_params(
                    sig_varying_params, suffix=suff
                )
                sigs_to_scan += sig_dict["signals_dict"]["signals"]
                signames_to_scan.append(sig_dict["name"])

            sigs_dict = {"signals": sigs_to_scan}
            sigs_path = os.path.join(climb_workdir, "pred_sigs.json")
            with open(sigs_path, "w") as f:
                f.write(json.dumps(sigs_dict, indent=2))

            scores = scribe_and_score(
                self, climb_workdir, sigs_path, signames_to_scan
            )["scores"]
            print("~" * 30)
            print("Cur scores:")
            print(scores)

            for param_idx in range(len(next_params)):
                next_params[param_idx]["score"] = scores[param_idx]

            climber.get_results_evaluate(next_params)
            round_log = getattr(climber, "cur_round_log", None)
            if round_log:
                print(round_log)
            else:
                print("Best so far: {}".format(climber.best_score_so_far))

            if climber.finished:
                print("Breaking because finished")
                break

        print("Done with climb after {} iterations".format(i))
        print("Best score {}".format(climber.best_score_so_far))

        best_params = climber.get_best_climbed_params()
        final_signal = self.sigspec_obj.apply_varied_params(
            best_params, suffix="_{}".format(self.sigspec_obj.class_suffix)
        )

        final_sig_name = final_signal["name"]

        log_path = os.path.join(climb_workdir, "climb_log.txt")
        with open(log_path, "w") as f:
            f.write(climber.log_string)

        final_dir = os.path.join(self.work_dir, "final")
        if not os.path.exists(final_dir):
            os.mkdir(final_dir)

        final_sig_dest = os.path.join(final_dir, "final_sig.json")
        with open(final_sig_dest, "w") as f:
            f.write(json.dumps(final_signal["signals_dict"], indent=2))

        scores = scribe_and_score(self, final_dir, final_sig_dest, [final_sig_name])[
            "scores"
        ]

        print("Done with climb")
        return {
            "r2": climber.best_score_so_far,
            "params": best_params,
            "sig_path": final_sig_dest,
            "sig_name": final_sig_name,
        }


def scribe_one_sig(sigopt_obj, one_sig_dict, sig_name):
    one_off_dir = os.path.join(sigopt_obj.work_dir, "one_off")
    if not os.path.exists(one_off_dir):
        os.mkdir(one_off_dir)
    sigs_path = os.path.join(one_off_dir, "pred_sigs.json")
    with open(sigs_path, "w") as f:
        f.write(json.dumps(one_sig_dict, indent=2))

    x = scribe_and_score(sigopt_obj, one_off_dir, sigs_path, [sig_name])
    x["sig_path"] = sigs_path
    x["sig_name"] = sig_name

    return x


scribed = False


def scribe_and_score(
    sigopt_obj,
    sig_work_dir,
    sig_conf_path,
    signal_names,
    dates=[],
    vals_id="",
):
    csnames = ",".join(signal_names)

    all_val_outs = []
    procs = []

    if len(dates) == 0:
        dates = sigopt_obj.dates

    for one_day in dates:
        time_path = os.path.join(sigopt_obj.times_dir, "times_{}.csv".format(one_day))
        vals_tgt = "vals_{}.csv".format(one_day)
        if vals_id != "":
            vals_tgt = "vals_{}_{}".format(vals_id, one_day)
        scan_out_path = os.path.join(sig_work_dir, vals_tgt)
        all_val_outs.append(scan_out_path)

        scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage FEATURES --times-path {TIME_PATH} --feature-conf {SIG_CONF_PATH} --signals-path {VALS_PATH} --sigs {SIG_NAMES}'
        scanner_cmd = scanner_cmd.format(
            SCANNER_BIN=scanner_bin,
            DATE=one_day,
            START=sigopt_obj.day_start,
            END=sigopt_obj.day_end,
            SAMPLING_CONF=sigopt_obj.sampling_conf,
            TIME_PATH=time_path,
            VALS_PATH=scan_out_path,
            SIG_CONF_PATH=sig_conf_path,
            SIG_NAMES=csnames,
        )
        global scribed
        if not scribed:
            print(scanner_cmd)
            scribed = True
        proc = subprocess.Popen(scanner_cmd, shell=True)
        procs.append(proc)

    for one_proc in procs:
        one_proc.wait()

    rets_list = []
    vals_list = []

    for one_day in dates:
        ret_path = os.path.join(sigopt_obj.rets_dir, "returns_{}.csv".format(one_day))

        vals_tgt = "vals_{}.csv".format(one_day)
        if vals_id != "":
            vals_tgt = "vals_{}_{}".format(vals_id, one_day)

        vals_path = os.path.join(sig_work_dir, vals_tgt)
        ret = pd.read_csv(ret_path, names=["ret"]).values

        if len(signal_names) == 1:
            val = pd.read_csv(vals_path, names=["sig"]).values
        else:
            val = pd.read_csv(vals_path, names=signal_names).values

        rets_list.append(ret)
        vals_list.append(val)

    all_rets = np.concatenate(rets_list)
    all_vals = np.concatenate(vals_list)

    r2_scores = []
    for i in range(len(signal_names)):
        cur_vals = all_vals[:, i]
        cur_vals = cur_vals.reshape(len(cur_vals), 1)

        cur_fit = LinearRegression(fit_intercept=False).fit(cur_vals, all_rets)

        r2 = cur_fit.score(cur_vals, all_rets)
        r2_scores.append(r2)

    return {
        "scores": r2_scores,
        "val_paths": all_val_outs,
        "rets": all_rets,
        "vals": all_vals,
    }


def scribe_and_rets(sigopt_obj, sig_conf_path, sig_names, cap_ret=0.0025, dates=[]):
    sigopt_obj.sample_and_returns(dates)

    scratch_dir = os.path.join(sigopt_obj.work_dir, "scratch_scribe")
    if not os.path.exists(scratch_dir):
        os.mkdir(scratch_dir)

    to_ret = scribe_and_score(sigopt_obj, scratch_dir, sig_conf_path, sig_names, dates=dates)
    sig_df = pd.DataFrame(to_ret["vals"], columns=["sig"])
    ret_df = pd.DataFrame(to_ret["rets"], columns=["ret"])

    ret_df = ret_df.clip(upper=cap_ret, lower=-cap_ret)

    sigs_ret_df = pd.concat([sig_df, ret_df], axis=1)
    to_ret["sig_reg_df"] = sigs_ret_df

    return to_ret


def scribe_and_rets_sigparams(sigopt_obj, sigformer_obj, sig_params, cap_ret=0.0025, dates=[]):
    scratch_dir = os.path.join(sigopt_obj.work_dir, "scratch_scribe")
    if not os.path.exists(scratch_dir):
        os.mkdir(scratch_dir)

    our_sigobj_path = os.path.join(sigopt_obj.work_dir, "custom_sig.json")
    sig_dct = sigformer_obj.apply_varied_params(sig_params)

    sig_name = sig_dct["name"]
    onlysig_dct = sig_dct["signals_dict"]

    with open(our_sigobj_path, "w") as f:
        f.write(json.dumps(onlysig_dct, indent=2))

    to_ret = scribe_and_rets(sigopt_obj, our_sigobj_path, [sig_name])

    return to_ret


def reg_and_score(src_df, dep_label, indep_label, fit_intercept=True):
    curX = src_df[indep_label].values
    curX = curX.reshape(len(curX), 1)

    curY = src_df[dep_label].values
    curY = curY.reshape(len(curY), 1)

    full_fit = LinearRegression(fit_intercept=fit_intercept).fit(curX, curY)
    r2 = full_fit.score(curX, curY)

    return {"r2": r2, "coefs": full_fit.coef_, "intercept": full_fit.intercept_}
