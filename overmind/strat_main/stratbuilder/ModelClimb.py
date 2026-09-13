#! /usr/bin/env python

"""
    Basic outline to do iterative modelclimb.

    Note: eventually, we can probably do different forms of fitting.
    Right now, I'm going to define the most basic one.

"""
import os
import subprocess
import sys
import json
import copy
import shutil
import logging

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from util.pathing import bindir

from util.log import getLogger
from util.runner import run_batches

from templatizers import SigFormer
from stratbuilder.SigDispatcher import SigDispatcher
from stratbuilder.RegressLib import sig_passes_imba_filter
from util.chron import dates_list, dates_avail, blacklist


from optimizers.QuadClimb import QuadClimber

# from modelfit.SigDispatcher import SigDispatcher

scanner_bin = os.path.join(bindir(), "signalscanner")


# Configure logger.


class MultiClimber:
    # work_dir is the local multiclimb work_dir.
    def __init__(
        self,
        work_dir,
        training_dict,
        side_prods,
        days_to_sampling_confs,
        provided_dates_list,
        blacklist_dates=[],
    ):
        # Copied over from the training_dict.
        self.tgt_symbol = training_dict["traded_symbols"][0]
        self.traded_markets = training_dict["traded_markets"]
        self.side_products = side_prods

        # Figure out the dates we want. Have the option of having a different set of
        # dates than the rest of the process. This is just so we can do funky things like
        # climb on a subset of days (for speed) and fit on a larger set of days.
        if (
            "start_date" in training_dict["modelclimb_specs"]
            and "end_date" in training_dict["modelclimb_specs"]
        ):
            dates_list = dates_avail(
                training_dict["modelclimb_specs"]["start_date"],
                training_dict["modelclimb_specs"]["end_date"],
                self.tgt_symbol,
                self.traded_markets[0],
            )["good_dates"]

        else:
            dates_list = provided_dates_list

        # apply blacklist
        dates_list = blacklist(dates_list, blacklist_dates)
        self.dates_list = dates_list

        # These are time-of-days.
        self.day_start_regress = training_dict["day_start_regress"]
        self.day_end_regress = training_dict["day_end_regress"]

        self.training_dict = training_dict
        self.qc_specs = self.training_dict["modelclimb_specs"]["qc_specs"]

        # How many to run in parallel at a time.
        self.batch_size = self.training_dict["modelclimb_specs"].get("batch_size", 5)

        self.work_dir = work_dir

        self.reinit_prev = False
        if "reinit_prev" in self.training_dict["modelclimb_specs"]:
            self.reinit_prev = self.training_dict["modelclimb_specs"]["reinit_prev"]

        self.run_combined_signalclimb = False
        if "run_combined_signalclimb" in training_dict["modelclimb_specs"]:
            self.run_combined_signalclimb = training_dict["modelclimb_specs"][
                "run_combined_signalclimb"
            ]

        self.early_signal_shutoff = self.training_dict["modelclimb_specs"].get("early_signal_shutoff", False)

        # On each iteration, we write all the signals
        self.golden_dir_sigs = os.path.join(self.work_dir, "golden")
        if not os.path.exists(self.golden_dir_sigs):
            os.mkdir(self.golden_dir_sigs)

        self.golden_sig_names = []

        # With each of these signals, I want to scribe out their
        # values, of course.
        self.golden_dir_sigvals = os.path.join(self.work_dir, "golden_sigvals")
        if not os.path.exists(self.golden_dir_sigvals):
            os.mkdir(self.golden_dir_sigvals)

        # List of ordered_pairs;
        # (signal name, r2)
        self.golden_sigs_scores = []

        # For each signal with signal_name above, a list
        # of the ordered scribed files.
        self.golden_sigs_to_vals = {}

        # Paths of all the files inside golden_dir_sigs.
        # Just storing it to be easy.
        self.golden_sig_paths = []

        self.golden_sig_params = []

        # Basically same thing as the climbed_cache files. But
        # remembers just the last values for each signal classification. So we can
        # reinit on it for the next round.
        # Example:
        useless_ex = """  "TrdImp__Self": {
        "classname": "TrdImp",
        "symbol": "APEUSDT",
        "markets": [
          "BinanceSPOT"
        ],
        "traded_symbol": "APEUSDT",
        "traded_markets": [
          "BinanceSPOT"
        ],
        "param_vals": {
          "sz_decay": 2000,
          "time_decay": 300,
          "tick_decay": 0.9939107340081471,
          "tgt_sig_decay": 0.9
        }
        },

        """
        self.sig_type_to_reinit = {}

        # And. I'm going to do this inefficiently. But basically want to keep

        self.rets_dir = os.path.join(self.work_dir, "rets")
        if not os.path.exists(self.rets_dir):
            os.mkdir(self.rets_dir)

        self.times_dir = os.path.join(self.work_dir, "times")
        if not os.path.exists(self.times_dir):
            os.mkdir(self.times_dir)

        self.days_to_sampling_confs = days_to_sampling_confs

        self.multi_regress_conf = os.path.join(self.work_dir, "sampling_conf.json")

        self.num_rounds = training_dict["modelclimb_specs"]["num_rounds"]
        self.sigs_per_round = training_dict["modelclimb_specs"]["dispatch_sigs_per_round"]
        # We should take over this from the tradeacademy side.
        # Anyway, it doesn't really matter right now.
        self.markout_cap = training_dict["modelclimb_specs"].get("markout_cap", 0.002)

        self.signals_per_round = training_dict["modelclimb_specs"]["signals_per_round"]
        self.fit_intercept = training_dict["modelclimb_specs"]["fit_intercept"]
        self.keep_intercept = training_dict["modelclimb_specs"]["keep_intercept"]

        self.best_score_so_far = 0
        self.last_completed_round = -1
        self.done = False

        sd = SigDispatcher(
            self.tgt_symbol, self.traded_markets, self.side_products, self.num_rounds,
            self.sigs_per_round
        )

        # Use this for the multiclimb.
        # Testing the combined dispatch.
        # if "run_combined_signalclimb" in training_dict["modelclimb_specs"] and training_dict["modelclimb_specs"]["run_combined_signalclimb"]:
        # self.rounds_to_signals = sd.test_dispatch()
        # else:
        # Always use basic_dispatch.

        self.rounds_to_signals = sd.dispatch(
            training_dict["modelclimb_specs"]["dispatch_style"]
        )
        # print(self.rounds_to_signals)
        # Grab the basic rounds_to_signals and save them.

        # Dict of chosen params
        self.inherited_params_cache_format = {}

        self.inherited_params_objs = []
        self.inherited_sig_names = []
        self.inherited_sigs_path = ""
        self.inherited_sig_params_path = ""

        # Scribe and break these apart.
        self.inherited_sigval_paths = []

        self.inherited_dir = ""

        # Start logging.
        self.logger = getLogger("multiclimb", "multiclimb.log")
        self.logger.info("-- Starting Multiclimb -- ")

        self.cmd_logger = getLogger("multiclimb_cmds", "multiclimb_cmds.txt")

    def load_cached_sig_params(self):
        # Iteratively go through my rounds, and populate my sig_type_to_reinit
        for one_round in range(self.last_completed_round):
            round_dir = os.path.join(self.work_dir, "round_{}".format(one_round))
            cache_file = os.path.join(round_dir, "climbed_cache.json")
            if not os.path.exists(cache_file):
                # Don't read a ghost.
                continue
            # Read the cache.
            with open(cache_file, "r") as f:
                climbed_cache_dct = json.loads(f.read())

            # Read it.
            # On subsequent rounds, overwrite them. That's still fine.
            for sig_label, sig_props in climbed_cache_dct.items():
                self.sig_type_to_reinit[sig_label] = sig_props["param_vals"]

    # At the end of each round of multiclimb. We save this round's climbed params.
    # This will be used if we want to use reinit.
    def save_round_params(self, cached_sig_dicts, chosen_sigs_keyname):
        # print("I'm saving the round params")
        for signame_key, sig_settings in cached_sig_dicts.items():
            self.sig_type_to_reinit[signame_key] = sig_settings["param_vals"]

        # Un-cache the signals that actually got chosen
        # Give it a chance to climb again.
        for signame_key in chosen_sigs_keyname:
            if signame_key in self.sig_type_to_reinit:
                del self.sig_type_to_reinit[signame_key]

        # print("Afterwards, the round_params are ")
        # print(self.sig_type_to_reinit)

    # Separate dict.
    # 1: we write the inherited params and include them into our
    # thing by default
    # 2: We store the sigparams into our set of final_chosen_params
    # 3: We write this out into a inherited_file.
    def set_inherited_params(self, inherited_params):
        self.inherited_params_cache_format = inherited_params

        self.logger.info("Trying to inherit params")
        # self.logger.info(inherited_params)

        inherited_sig_names = []
        all_sigs_lst = []

        for idx, one_sig_cache_setting in enumerate(inherited_params):
            # one_sig_label = one_sig_cache_setting["signame_key"]
            class_suff = one_sig_cache_setting["symbol"]
            sig_obj = SigFormer.load_sigspec_from_cache(
                one_sig_cache_setting, class_suff, prefix="inh_{}".format(idx)
            )

            self.inherited_params_objs.append(sig_obj)

            sig_dct = sig_obj.apply_varied_params(
                one_sig_cache_setting["param_vals"],
                suffix="_{}".format(sig_obj.class_suffix),
            )
            all_sigs_lst += sig_dct["signals_dict"]["signals"]
            inherited_sig_names.append(sig_dct["name"])

            # self.logger.info("_" * 100)
            # self.logger.info(sig_dct["signals_dict"]["signals"][0])
            # self.logger.info(sig_dct["name"])
            # self.logger.info("_" * 100)

        # Cache the names
        self.inherited_sig_names = inherited_sig_names

        # Write the inherited file.
        self.inherited_dir = os.path.join(self.work_dir, "inherited")
        if not os.path.exists(self.inherited_dir):
            os.mkdir(self.inherited_dir)

        self.inherited_sigs_path = os.path.join(
            self.inherited_dir, "inherited_sigs.json"
        )
        with open(self.inherited_sigs_path, "w") as f:
            f.write(json.dumps({"signals": all_sigs_lst}, indent=2))

        # Write the params too I guess.
        self.inherited_sig_params_path = os.path.join(
            self.inherited_dir, "inherited_sig_params.json"
        )
        with open(self.inherited_sig_params_path, "w") as f:
            f.write(json.dumps(inherited_params, indent=2))

        # This stuff will get written out when we run the rets...
        self.logger.info("Done inheriting")

    def scribe_inherited_values(self):
        # Basically do a scribe_and_score here.
        if self.inherited_sigs_path == "":
            return

        # Otherwise, scribe_and_score.
        # Let's just make it skip. Just in case.
        inherited_scribes = self.scribe_and_score(
            self.inherited_dir,
            self.inherited_sigs_path,
            self.inherited_sig_names,
            skip_if_val_exists=True,
            include_inherited=False,
        )
        # Save these.
        self.inherited_sigval_paths = inherited_scribes["val_out_paths"]

    # After multiclimb, write the param configs of the
    # chosen signals to its own json file.
    # This can be picked up by the coefLair.
    def write_chosen_signal_dicts(self):
        pass

    # I think this should do it. I'll run it at the start of each
    # multiclimb round.
    def update_sigformer_centers(self, this_round_sigs):
        for one_sig in this_round_sigs:
            # Get the label.
            signame_key = "{}_{}".format(one_sig.classname, one_sig.class_suffix)
            if signame_key in self.sig_type_to_reinit:
                # Then update the centers.
                one_sig.set_varied_param_centers(self.sig_type_to_reinit[signame_key])
                self.logger.info(
                    "I updated the center of {} with {}".format(
                        signame_key, self.sig_type_to_reinit[signame_key]
                    )
                )

    def one_day_returns(self, date, should_print=False, always_overwrite=False):
        time_path = os.path.join(self.times_dir, "times_{}.csv".format(date))
        returns_path = os.path.join(self.rets_dir, "returns_{}.csv".format(date))

        if (not always_overwrite) and (
            os.path.exists(time_path) and os.path.exists(returns_path)
        ):
            return True

        scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage RETURNS --times-path {TIME_PATH} --returns-path {RETURN_PATH}'
        scanner_cmd = scanner_cmd.format(
            SCANNER_BIN=scanner_bin,
            DATE=date,
            START=self.day_start_regress,
            END=self.day_end_regress,
            SAMPLING_CONF=self.days_to_sampling_confs[date],
            TIME_PATH=time_path,
            RETURN_PATH=returns_path,
        )

        self.cmd_logger.info(scanner_cmd)
        scanner_lines = subprocess.run(
            scanner_cmd, shell=True, capture_output=True, text=True
        )
        scanner_std = scanner_lines.stdout.split("\n")
        scanner_errs = scanner_lines.stderr
        # Let's run it then.

        # I guess let's display an error if it fails?
        if len(scanner_errs) > 0:
            # print(
            #    "Scanning times and returns on {}, saw an error: \n{}\n".format(
            #        date, scanner_errs
            #    )
            # )
            return False
        return True

    # Run a sigscanner to generate both print times and returns.
    def sample_and_returns(self):
        all_completed = True
        days_good = []
        days_bad = []

        first_day = True

        scan_cmds = []
        for one_day in self.dates_list:
            time_path = os.path.join(self.times_dir, "times_{}.csv".format(one_day))
            returns_path = os.path.join(
                self.rets_dir, "returns_{}.csv".format(one_day)
            )

            scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage RETURNS --times-path {TIME_PATH} --returns-path {RETURN_PATH}'
            scanner_cmd = scanner_cmd.format(
                SCANNER_BIN=scanner_bin,
                DATE=one_day,
                START=self.day_start_regress,
                END=self.day_end_regress,
                SAMPLING_CONF=self.days_to_sampling_confs[one_day],
                TIME_PATH=time_path,
                RETURN_PATH=returns_path,
            )
            scan_cmds.append(scanner_cmd)

            self.cmd_logger.info(scanner_cmd)

        run_batches(scan_cmds, self.batch_size)

        self.logger.info("Done with times and returns")
        self.cmd_logger.info(" ----- ")

        # Prob store them.

    # This needs to taken into account the
    # Scribes out signal values
    # Returns an ordered set of these things.
    # TODO: implement include_golden_vals here.
    def scribe_and_score(
        self,
        sig_work_dir,
        signal_path,
        signal_names,
        vals_id="",
        include_golden_vals=True,
        include_inherited=True,
        skip_if_val_exists=False,
    ):
        csnames = ",".join(signal_names)

        all_val_out_paths = []

        first_print = True

        scribe_cmds = []
        for one_day in self.dates_list:
            time_path = os.path.join(self.times_dir, "times_{}.csv".format(one_day))

            vals_tgt = "vals_{}.csv".format(one_day)
            if vals_id != "":
                vals_tgt = "vals_{}_{}.csv".format(vals_id, one_day)
            scan_out_path = os.path.join(sig_work_dir, vals_tgt)
            all_val_out_paths.append(scan_out_path)

            if skip_if_val_exists and os.path.exists(scan_out_path):
                # Then we don't need to run this command.
                continue

            scanner_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode REGRESS --regress-stage FEATURES --times-path {TIME_PATH} --signals-path {DEST_PATH} --feature-conf {SIGNAL_CONF} --sigs {SIGNAL_NAMES}'

            scanner_cmd = scanner_cmd.format(
                SCANNER_BIN=scanner_bin,
                DATE=one_day,
                START=self.day_start_regress,
                END=self.day_end_regress,
                SAMPLING_CONF=self.days_to_sampling_confs[one_day],
                TIME_PATH=time_path,
                DEST_PATH=scan_out_path,
                SIGNAL_CONF=signal_path,
                SIGNAL_NAMES=csnames,
            )

            self.cmd_logger.info(scanner_cmd)
            scribe_cmds.append(scanner_cmd)
        print("scribe_and_scoring, cmds is len {}".format(len(scribe_cmds)))
        run_batches(scribe_cmds, self.batch_size)

        # R2 values.
        ordered_scores = []

        rets_list = []
        vals_list = []

        # Aggregate them all.
        for one_day in self.dates_list:
            ret_path = os.path.join(self.rets_dir, "returns_{}.csv".format(one_day))
            vals_tgt = "vals_{}.csv".format(one_day)
            if vals_id != "":
                vals_tgt = "vals_{}_{}.csv".format(vals_id, one_day)
            val_path = os.path.join(sig_work_dir, vals_tgt)

            # Read in the csvs.
            ret = pd.read_csv(ret_path, names=["ret"]).values
            val = pd.read_csv(val_path, names=signal_names).values

            rets_list.append(ret)
            vals_list.append(val)

        # Concat them all.
        all_rets = np.concatenate(rets_list)
        all_vals = np.concatenate(vals_list)

        # OK, great. Now we have all our signal vals.

        # Also include the inherited params.
        inherited_vals_arr = None
        inherited_vals_lst = []
        if include_inherited:
            # pass
            for one_inherit_file in self.inherited_sigval_paths:
                sig_vals = pd.read_csv(
                    one_inherit_file, names=self.inherited_sig_names
                ).values
                # 1d to 2d.
                if len(sig_vals.shape) == 1:
                    # Then needs to be reshaped.
                    sig_vals = sig_vals.reshape(len(sig_vals), 1)

                inherited_vals_lst.append(sig_vals)
            if len(inherited_vals_lst) > 0:
                # axis=0 because we're concatenating them day by day
                inherited_vals_arr = np.concatenate(inherited_vals_lst, axis=0)

        # I need to also include the golden values, if this is going to be a thing.
        golden_vals_arr = None
        golden_vals_lst = []
        if include_golden_vals:
            # For each signal_name, grab its golden_vals and concat them
            # normally.

            for golden_sig_name, gs_files_list in self.golden_sigs_to_vals.items():
                # Ok, now we start accumulating scribed values.
                one_gvs_list = []
                for one_gs_file in gs_files_list:
                    sig_vals = pd.read_csv(one_gs_file, names=[golden_sig_name]).values
                    one_gvs_list.append(sig_vals)

                # Accumulate these into one thing.
                gv_alldays = np.concatenate(one_gvs_list)
                # Have to preemptively reshape this so I can concat them across
                # a diff axis.
                gv_alldays = gv_alldays.reshape(len(gv_alldays), 1)
                golden_vals_lst.append(gv_alldays)
            # Then we can concat them all
            if len(golden_vals_lst) > 0:
                golden_vals_arr = np.concatenate(golden_vals_lst, axis=1)

            # At the end, concat all the chosen_sigs' golden_vals with axis=1.

        r2_scores = []
        passes_filter = []
        for i in range(len(signal_names)):
            cur_vals = all_vals[:, i]
            cur_vals = cur_vals.reshape(len(cur_vals), 1)

            # Introduce a penalization factor if we fail the imba test.
            #badsig_penalization_factor = 0.9
            med_std_limit = .05
            mean_std_limit = 0.05
            corr_limit=0.005
            signed_corr_limit=-1
            # eg. if we fail the imba test, then we multiply the r2 by 0.9. Too bad.
            passed_imba_filter = sig_passes_imba_filter(cur_vals, all_rets, med_std_limit, mean_std_limit, corr_limit, signed_corr_limit)
            passes_filter.append(passed_imba_filter)

            # Do the mixed regression if we want have them.
            if golden_vals_arr is not None:
                cur_vals = np.concatenate([golden_vals_arr, cur_vals], axis=1)

            # Take in the inherited sigs too.
            if inherited_vals_arr is not None:
                cur_vals = np.concatenate([inherited_vals_arr, cur_vals], axis=1)

            # TODO: maybe play around with fit_intercept choice.
            cur_fit = LinearRegression(fit_intercept=self.fit_intercept).fit(
                cur_vals, all_rets
            )
            r2 = cur_fit.score(cur_vals, all_rets)

            # TODO: probably log that this happened. womp womp.
            #if not passed_imba_filter:
            #    r2 *= badsig_penalization_factor

            r2_scores.append(r2)
        return {"scores": r2_scores, "val_out_paths": all_val_out_paths, "passes_filter": passes_filter}

    def run_signalclimb(self, cur_workdir, sigformer_obj, round_num):
        sig_workdir = os.path.join(
            cur_workdir,
            "{}_{}".format(sigformer_obj.classname, sigformer_obj.class_suffix),
        )

        if not os.path.exists(sig_workdir):
            os.mkdir(sig_workdir)

        qc = QuadClimber()

        qc_specs = self.qc_specs
        climbed_param_specs = sigformer_obj.get_varying_params()

        qc_specs["params"] = climbed_param_specs

        qc.init_from_json(qc_specs)

        # Start the multiclimb.

        for i in range(qc.max_epochs):
            next_params = qc.get_params_next_epoch()["params_and_scores"]

            self.logger.info("Running iteration {} of climb".format(i))
            sigs_to_scan = []
            signames_to_scan = []
            for suff_counter, one_sig_version in enumerate(next_params):
                suff = "_{}".format(suff_counter)

                # This naming sucks. I'm sorry.
                sig_varying_param = {}

                for one_varying_param_dict in one_sig_version[
                    "ordered_param_instances"
                ]:
                    sig_varying_param[
                        one_varying_param_dict["name"]
                    ] = one_varying_param_dict["projection_value"]
                # Apply the transformation.
                sig_dict = sigformer_obj.apply_varied_params(
                    sig_varying_param, suffix=suff
                )
                # Add this to the sig_list.
                sigs_to_scan += sig_dict["signals_dict"]["signals"]
                signames_to_scan.append(sig_dict["name"])
            # Do a single loop of scribe and score.
            sigs_dict = {"signals": sigs_to_scan}
            sigs_path = os.path.join(sig_workdir, "pred_sigs.json")
            with open(sigs_path, "w") as f:
                f.write(json.dumps(sigs_dict, indent=2))

            scores = self.scribe_and_score(sig_workdir, sigs_path, signames_to_scan)[
                "scores"
            ]

            # self.logger.info("~" * 100)
            # self.logger.info(signames_to_scan)
            # self.logger.info(scores)
            self.logger.info("~" * 100)
            # Feed this back into the quadclimber. And make the circle of life continue.
            for param_idx in range(len(next_params)):
                next_params[param_idx]["score"] = scores[param_idx]

            # So then, feed this back to the qc.
            qc.get_results_evaluate(next_params)
            # Do iterative climbing.
            if qc.finished:
                break

        # So, at this point we've finished the climb.
        # We should grab the best climbed results and save them, as
        # well as the scores.
        self.logger.info("Done with climb after {} iterations".format(i))
        self.logger.info("Best score was {}".format(qc.best_score_so_far))

        # TODO: write the climb log to the local dir

        best_params = qc.get_best_climbed_params()
        # Apply it to the signal.
        # TODO: I should have changed this.
        final_signal = sigformer_obj.apply_varied_params(
            best_params, suffix="_{}".format(sigformer_obj.class_suffix)
        )

        climb_log = os.path.join(sig_workdir, "climb_log.txt")
        # Write climb log results in each local dir.
        with open(climb_log, "w") as f:
            f.write(qc.log_string)

        final_signal_dir = os.path.join(sig_workdir, "final")
        if not os.path.exists(final_signal_dir):
            os.mkdir(final_signal_dir)

        final_signal_dest = os.path.join(final_signal_dir, "best_sig.json")

        with open(final_signal_dest, "w") as f:
            f.write(json.dumps(final_signal["signals_dict"], indent=2))

        self.scribe_and_score(
            final_signal_dir,
            final_signal_dest,
            [final_signal["name"]],
            skip_if_val_exists=True,
        )

        self.logger.info("Done with climb")
        # Return the score I guess.
        return {
            "r2": qc.best_score_so_far,
            "params": best_params,
            "sig_path": final_signal_dest,
            "name": final_signal["name"],
            "sig_spec": sigformer_obj,
        }

    # Keeps track of signalclimb objects for each, but consolidates the
    # scribing portions.
    # That way we can run through data more efficiently in...
    # Note. We may also want to switch this up to be broken down by per-symbol tool, I'm not sure.
    def combined_signalclimb_round(
        self, cur_workdir, sigformer_objs, round_num, combined_workdir_title
    ):
        # Ordered list of the sigformers. NOT the
        list_sigformer_names = []
        # Each corresponds approx to a worker in a single signalclimb process.
        sfnames_to_sigformers = {}
        sfnames_to_qcs = {}
        sfnames_to_dirs = {}

        max_epochs = 0

        for one_sfobj in sigformer_objs:
            sfname = "{}_{}".format(one_sfobj.classname, one_sfobj.class_suffix)
            list_sigformer_names.append(sfname)

            sfnames_to_sigformers[sfname] = one_sfobj

            # Make the directory for this guy. It's where we'll write out the successive files so we can
            # check them later.
            sf_dir = os.path.join(cur_workdir, sfname)
            if not os.path.exists(sf_dir):
                os.mkdir(sf_dir)
            sfnames_to_dirs[sfname] = sf_dir

            sf_qc = QuadClimber()
            sf_qcspecs = copy.deepcopy(self.qc_specs)

            sf_param_specs = one_sfobj.get_varying_params()
            sf_qcspecs["params"] = sf_param_specs

            sf_qc.init_from_json(sf_qcspecs)
            sfnames_to_qcs[sfname] = sf_qc
            max_epochs = sf_qc.max_epochs

        # Make my own scratch dir too.
        my_scratchdir = os.path.join(cur_workdir, combined_workdir_title)

        if not os.path.exists(my_scratchdir):
            os.mkdir(my_scratchdir)

        # OK, we're all set up now. Let's run the mc round.

        for i in range(max_epochs):
            self.logger.info("Running iter {} of climb".format(i))

            all_sigs_to_scan = []
            all_sig_names_to_scan = []

            # Use this to update the score
            sfnames_to_sfparams = {}

            for one_sfname in list_sigformer_names:
                # i is the current epoch too.
                # print(sfnames_to_qcs[one_sfname].get_params_next_epoch())
                # Don't try to further climb stuff that can't be climbed.
                if sfnames_to_qcs[one_sfname].finished:
                    print("Continuing due to {} finished".format(one_sfname))
                    continue

                # We might still have unclimbable stuff? Not sure.
                # if len(sfnames_to_qcs[one_sfname].get_params_next_epoch()) <= 1:
                #    print("Somehow, still have bad values for {}".format(one_sfname))
                #    print(sfnames_to_qcs[one_sfname].get_params_next_epoch())

                sf_params = sfnames_to_qcs[one_sfname].get_params_next_epoch()[
                    "params_and_scores"
                ]
                sfnames_to_sfparams[one_sfname] = sf_params

                for suff_counter, one_sig_version in enumerate(sf_params):
                    suff = "_{}".format(suff_counter)
                    sig_varying_param = {}
                    for one_varying_param_dict in one_sig_version[
                        "ordered_param_instances"
                    ]:
                        sig_varying_param[
                            one_varying_param_dict["name"]
                        ] = one_varying_param_dict["projection_value"]

                    sig_dict = sfnames_to_sigformers[one_sfname].apply_varied_params(
                        sig_varying_param, suffix=suff
                    )
                    all_sigs_to_scan += sig_dict["signals_dict"]["signals"]
                    all_sig_names_to_scan.append(sig_dict["name"])


            if len(all_sig_names_to_scan) == 0:
                print("No more signals to climb in this round of multiclimb, breaking")
                break

            # OK, now we have all the things for all the signals.
            sigs_dict = {"signals": all_sigs_to_scan}
            sigs_path = os.path.join(my_scratchdir, "pred_sigs.json")

            with open(sigs_path, "w") as f:
                f.write(json.dumps(sigs_dict, indent=2))


            scores = self.scribe_and_score(
                my_scratchdir, sigs_path, all_sig_names_to_scan
            )["scores"]

            self.logger.info("~" * 100)
            # self.logger.info(all_sig_names_to_scan)
            # self.logger.info(scores)

            counter = 0
            # Feed these back to all quadclimbers.
            for one_sfname in list_sigformer_names:
                if sfnames_to_qcs[one_sfname].finished:
                    continue

                for param_idx in range(len(sfnames_to_sfparams[one_sfname])):
                    sfnames_to_sfparams[one_sfname][param_idx]["score"] = scores[
                        counter
                    ]
                    counter += 1
                # Update the qc now.
                sfnames_to_qcs[one_sfname].get_results_evaluate(
                    sfnames_to_sfparams[one_sfname]
                )

            # Insert extra process.
            if self.early_signal_shutoff:
                # Then, look at the scores, and only continue climbing the top 6 signals.
                # There are multiple ways of doing this but I guess I could make this work.
                # Probably is "good enough" and should speed us up a ton.
                # Give it 2 iterations to climb a little bit.
                if i == 1:
                    # Then go through and mark them.
                    all_scores = []
                    all_ranked_names = []

                    for one_sfname in list_sigformer_names:
                        if sfnames_to_qcs[one_sfname].finished:
                            continue

                        all_scores.append(sfnames_to_qcs[one_sfname].get_best_score())
                        all_ranked_names.append(one_sfname)
                    # OK, now get the top 5.
                    ranked_scores = copy.deepcopy(all_scores)
                    ranked_scores.sort(reverse=True)
                    #
                    score_cutoff = ranked_scores[min(4, len(ranked_scores))]
                    #Then, we can go through the names and mark which one is kept and which one isnt
                    self.logger.info("About to start early shutoff checks: cutoff is {}".format(score_cutoff))
                    for i, one_score in enumerate(all_scores):
                        if one_score < score_cutoff:
                            sfnames_to_qcs[all_ranked_names[i]].finished = True
                            early_ending_str = "Marking {} as finished early, because of low score {}<{}\n".format(all_ranked_names[i], one_score, score_cutoff)
                            sfnames_to_qcs[all_ranked_names[i]].log_string += early_ending_str
                            self.logger.info(early_ending_str)

        sf_scores = {}

        # I need to construct 2 versions of the finalsig:
        # 1: A consolidated signal conf for all of them, for combined scribing
        # 2: A custom version for each signal, for the posterity folks.

        final_combined_sigs_to_scan = []
        final_combined_sig_names_to_scan = []
        final_combined_sig_path = os.path.join(my_scratchdir, "final_sigs.json")

        for one_sfname in list_sigformer_names:
            self.logger.info(
                "Done climbing {}, best score weas {}".format(
                    one_sfname, sfnames_to_qcs[one_sfname].best_score_so_far
                )
            )
            # Write it.
            params = sfnames_to_qcs[one_sfname].get_best_climbed_params()
            final_sig_dict = sfnames_to_sigformers[one_sfname].apply_varied_params(
                params,
                suffix="_{}".format(sfnames_to_sigformers[one_sfname].class_suffix),
            )
            # print("Final signal like {}".format(final_sig_dict["name"]))

            final_combined_sigs_to_scan += final_sig_dict["signals_dict"]["signals"]
            final_combined_sig_names_to_scan.append(final_sig_dict["name"])

            climb_log = os.path.join(sfnames_to_dirs[one_sfname], "climb_log.txt")

            with open(climb_log, "w") as f:
                f.write(sfnames_to_qcs[one_sfname].log_string)

            final_sig_dir = os.path.join(sfnames_to_dirs[one_sfname], "final")
            if not os.path.exists(final_sig_dir):
                os.mkdir(final_sig_dir)

            final_sig_path = os.path.join(final_sig_dir, "best_sig.json")
            with open(final_sig_path, "w") as f:
                f.write(json.dumps(final_sig_dict["signals_dict"], indent=2))

            # Let's not od an early exit here. Oh well.
            sf_scores[one_sfname] = {
                "r2": sfnames_to_qcs[one_sfname].best_score_so_far,
                "params": params,
                "sig_name": final_sig_dict["name"],
            }

        # Write the final sig in its destination.
        final_combined_sigs_dict = {"signals": final_combined_sigs_to_scan}
        with open(final_combined_sig_path, "w") as f:
            f.write(json.dumps(final_combined_sigs_dict, indent=2))

        return {
            "scores": sf_scores,
            "dir": my_scratchdir,
            "sig_path": final_combined_sig_path,
            "names": final_combined_sig_names_to_scan,
        }

    # This is specifically, one multiclimb round.
    # This needs to call out to a single
    # signalclimb round too.
    # I think I gotta be clear with what these things all are.
    def one_round_multiclimb(self):
        cur_round = self.last_completed_round + 1
        if cur_round >= self.num_rounds:
            self.logger.info("Already completed all rounds!")
            self.done = True
            return
        # Otherwise, get the list of signals for this round.

        # Firstly, make the workdir.
        round_workdir = os.path.join(self.work_dir, "round_{}".format(cur_round))
        if not os.path.exists(round_workdir):
            os.mkdir(round_workdir)

        this_round_signals = self.rounds_to_signals[cur_round]

        # Well. Only do this if reinit.
        if self.reinit_prev:
            self.logger.info("Reinit enabled. updating centers of inherited sigs.")
            self.update_sigformer_centers(this_round_signals)

        # saves the best params.
        sig_to_params = {}

        # The best signals and the best params.
        cache_path = os.path.join(round_workdir, "climbed_cache.json")
        # Read the existing cache if it exist. And then if
        # we already have the climbed value, then we don't need
        # to run the climb.
        cached_sig_dicts = {}
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                cached_sig_dicts = json.loads(f.read())

        if self.run_combined_signalclimb:
            self.logger.info("Running combined signalclimb")
            combined_workdir_title = "combined_scratch"
            # Definitely run this thing, we wouldn't be here if we had
            combined_climb_results = self.combined_signalclimb_round(
                round_workdir, this_round_signals, cur_round, combined_workdir_title
            )
            climb_scores = combined_climb_results["scores"]
            for one_sfname, one_sig_result in climb_scores.items():
                sig_to_params[one_sfname] = one_sig_result["params"]

            for one_sig in this_round_signals:
                signame_key = "{}_{}".format(one_sig.classname, one_sig.class_suffix)
                sig_cache_dict = one_sig.export_with_param_settings(
                    sig_to_params[signame_key]
                )
                sig_cache_dict["signame_key"] = signame_key
                cached_sig_dicts[signame_key] = sig_cache_dict
            # Write the cache to disk.
            with open(cache_path, "w") as f:
                f.write(json.dumps(cached_sig_dicts))

            # Before the choosing ceremony, I should do a combined scribe-and-score so I can
            # guard against running through same data redundantly too.
            # Good thing is, I know the names and stuff.
            scratchdir = combined_climb_results["dir"]
            final_path = combined_climb_results["sig_path"]
            final_names = combined_climb_results["names"]
            self.logger.info("*" * 30)
            self.logger.info("Running final scribe_and_score on {}".format(final_path))
            scribes_and_vals = self.scribe_and_score(
                scratchdir, final_path, final_names
            )

            # self.logger.info("Observed scribe_and_vals is like ")
            # self.logger.info(scribes_and_vals)

            # OK, so at this point I should have the values for each name.
            # That's good. So then I can go through each name and figure out
            # where to put them.
            # This should be a list of dated values, sorted by the dates
            # in self.dates_list
            scribed_out_vals = scribes_and_vals["val_out_paths"]
            for idx, val_path in enumerate(scribed_out_vals):
                the_date = self.dates_list[idx]

                # Reads in the csv. Breaks it up and writes them out to the
                # appropriate dest.
                vals_df = pd.read_csv(val_path, names=final_names)

                # OK, this is perfect then.
                for sig_idx, one_sig_name in enumerate(final_names):
                    # The dest of this vals file should be...
                    # Where should this be?
                    cur_sigformer = this_round_signals[sig_idx]
                    cur_finaldir = os.path.join(
                        round_workdir, cur_sigformer.class_suffix_name(), "final"
                    )
                    if not os.path.exists(cur_finaldir):
                        os.makedirs(cur_finaldir)

                    vals_dest = os.path.join(
                        cur_finaldir, "vals_{}.csv".format(the_date)
                    )

                    # Write the val to the vals_dest.
                    our_sig_df = vals_df[one_sig_name]
                    # Why do they make it so hard to write these things out. F.

                    our_sig_df.to_csv(vals_dest, header=False, index=False)
                    # print("Wrote out vals to {}".format(vals_dest))

        else:
            for one_sig in this_round_signals:
                signame_key = "{}_{}".format(one_sig.classname, one_sig.class_suffix)
                if signame_key in cached_sig_dicts:
                    sig_to_params[signame_key] = cached_sig_dicts[signame_key]
                    continue
                else:
                    # Run climb.
                    climb_result = self.run_signalclimb(
                        round_workdir, one_sig, cur_round
                    )
                    # Save value
                    sig_to_params[signame_key] = climb_result["params"]

                    # Cache and save cache.
                    sig_cache_dict = one_sig.export_with_param_settings(
                        climb_result["params"]
                    )

                    sig_cache_dict["signame_key"] = signame_key
                    cached_sig_dicts[signame_key] = sig_cache_dict
                    with open(cache_path, "w") as f:
                        f.write(json.dumps(cached_sig_dicts))


        self.logger.info("Done with round {} of multiclimb".format(cur_round))

        # Go through all signals, pick out the ones we want to keep.
        # Add those and scribe them to the golden copy.

        max_chosen_sigs = min(self.signals_per_round, len(this_round_signals))

        # We'll be doing a bunch of scribing for this for the
        # final round evaluation.
        round_scratchdir = os.path.join(round_workdir, "scratch")
        if not os.path.exists(round_scratchdir):
            os.mkdir(round_scratchdir)

        chosen_sigs = set()
        chosen_sigs_keyname = set()
        for i in range(max_chosen_sigs):
            best_score_so_far = 0
            best_sig_obj = None
            best_sig_name = ""
            best_sig_val_paths = []

            # one_sig should be of type SignalSpec
            for one_sig in this_round_signals:
                # Do not add things that are already good.
                if one_sig.class_suffix_name() in chosen_sigs:
                    continue

                # Scribe and score the golden guy, yeah?

                sig_finaldir = os.path.join(
                    round_workdir, one_sig.class_suffix_name(), "final"
                )

                if not os.path.exists(sig_finaldir):
                    os.makedirs(sig_finaldir)

                # Get the signal name.
                final_sig_path = os.path.join(
                    sig_finaldir,
                    "best_sig.json",
                )

                # This is operating on the climbed results from
                # individual signal. So it doesn't have a specific
                # name yet. Rewrite this and the name after we make the choice.
                final_sig_name = one_sig.apply_varied_params(
                    {}, suffix="_{}".format(one_sig.class_suffix)
                )["name"]

                ss_output = self.scribe_and_score(
                    sig_finaldir,
                    final_sig_path,
                    [final_sig_name],
                    skip_if_val_exists=True,
                )
                score = ss_output["scores"][0]
                val_paths = ss_output["val_out_paths"]
                if not ss_output["passes_filter"][0]:
                    # Do not include signals if they fail the imba test.
                    # Feel like this has fewer problems if so.
                    continue

                if score > best_score_so_far:
                    best_score_so_far = score
                    best_sig_obj = one_sig
                    best_sig_name = final_sig_name
                    best_sig_val_paths = val_paths

            # OK, I guess we have it.
            if best_sig_name == "":
                print("Could not find a best sig this round. Breaking.")
                # Nothing is worth picking. That's fine.
                break

            # I'm really sorry, I think this is getting a little
            # messy and annoying.
            # TODO: Come back and fix all this.
            # Probably the best way is to have the sigformer
            # be able to read from file.

            self.logger.info("Best sig is like {}".format(best_sig_name))
            # Add the name
            chosen_sigs.add(best_sig_obj.class_suffix_name())
            signame_key = "{}_{}".format(best_sig_obj.classname, best_sig_obj.class_suffix)
            chosen_sigs_keyname.add(signame_key)

            self.best_score_so_far = best_score_so_far
            # Write the signal

            golden_sig_path = os.path.join(
                self.golden_dir_sigs,
                # Maybe this can be simplified? UNDO if needed.
                "{}.json".format(best_sig_name),
                #    "{}_{}.json".format(best_sig_name, best_sig_obj.class_suffix_name()),
            )
            # Create the final sig dict.
            golden_sig_name_and_sig = best_sig_obj.apply_varied_params(
                sig_to_params[best_sig_obj.class_suffix_name()],
                suffix="_choice{}".format(i),
            )

            golden_sig_name = golden_sig_name_and_sig["name"]
            golden_sig_dict = golden_sig_name_and_sig["signals_dict"]

            self.golden_sig_names.append(golden_sig_name)
            with open(golden_sig_path, "w") as f:
                f.write(json.dumps(golden_sig_dict, indent=2))

            # skip scribe_and_score and just cp it over. Saves us from running through mktdata.
            for idx, src_val_path in enumerate(best_sig_val_paths):
                the_date = self.dates_list[idx]
                golden_dest = os.path.join(
                    self.golden_dir_sigvals,
                    "vals_{}_{}.csv".format(golden_sig_name, the_date),
                )
                # cp one to the other.
                shutil.copy(src_val_path, golden_dest)

            # Don't rescribe them. But still run the score.
            golden_scribed_out = self.scribe_and_score(
                self.golden_dir_sigvals,
                golden_sig_path,
                [golden_sig_name],
                vals_id=golden_sig_name,
                skip_if_val_exists=True,
            )

            # Get the list and set it here.
            self.golden_sig_paths.append(golden_sig_path)
            self.golden_sigs_to_vals[golden_sig_name] = golden_scribed_out[
                "val_out_paths"
            ]
            self.golden_sigs_scores.append(
                (golden_sig_name, golden_scribed_out["scores"][0])
            )
            # Clean up the scratchdir for the potential next choice.
            # Save the golden_sig_params too.
            golden_signame_key = "{}_{}".format(
                best_sig_obj.classname, best_sig_obj.class_suffix
            )
            best_sig_export_params = best_sig_obj.export_with_param_settings(
                sig_to_params[best_sig_obj.class_suffix_name()]
            )
            best_sig_export_params["signame_key"] = golden_signame_key
            self.golden_sig_params.append(best_sig_export_params)

        # Save as well.
        self.save_round_params(cached_sig_dicts, chosen_sigs_keyname)

        self.last_completed_round += 1

        pass

    # Actually, do nothing w/ this for now.
    # Orchestrate it from tradeacademy rn.
    def run_climb(self):
        # Run multiple versions of signalclimb.

        for cur_round in range(self.num_rounds):
            # Potentially write stats to disk.

            # For now,
            self.run_one_round()

            pass

        pass

    def set_last_completed_round(self, round_no):
        self.last_completed_round = round_no

    def get_last_completed_round(self):
        return self.last_completed_round

    def get_best_score_so_far(self):
        return self.best_score_so_far

    def set_golden(
        self,
        golden_sig_names,
        golden_sigs_to_vals,
        golden_sigs_scores,
        golden_sig_paths,
        golden_sig_params,
    ):
        self.golden_sig_names = golden_sig_names
        self.golden_sigs_to_vals = golden_sigs_to_vals
        self.golden_sigs_scores = golden_sigs_scores
        self.golden_sig_paths = golden_sig_paths
        self.golden_sig_params = golden_sig_params

    def get_golden(self):
        return {
            "sig_names": self.golden_sig_names,
            "sigs_to_vals": self.golden_sigs_to_vals,
            "sigs_scores": self.golden_sigs_scores,
            "sig_paths": self.golden_sig_paths,
            "sig_params": self.golden_sig_params,
        }
