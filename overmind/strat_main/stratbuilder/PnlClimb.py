#! /usr/bin/env python

"""
Pretty basic process. Does an iterative pnlclimb.

Sims things in-sample and oos as well.

"""

import os
import subprocess
import sys
import json
import itertools
import math
import numpy as np

import pandas as pd

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from util.pathing import bindir
from util.chron import dates_list, dates_avail

from util.log import getLogger
import logging

from templatizers import PKFormer, OrdexFormer, TempoFormer
import subprocess
import socket

from optimizers.VaryingParam import VaryingParam
from optimizers.ParticleClimb import ParticleClimber
from optimizers.QuadClimb import QuadClimber
from optimizers.CmaEsClimb import CmaEsClimber
from optimizers.BayesClimb import BayesClimber
from optimizers.TpeClimb import TpeClimber
from optimizers.TurboClimb import TurboClimber
from optimizers.GroupedClimb import GroupedClimber
from util.sim import sim_date_range

pkdex_bin = os.path.join(bindir(), "pkdex")
pk_bin = os.path.join(bindir(), "pktrade")
scan_bin = os.path.join(bindir(), "signalscanner")

# Let's make sure all of them exist.

if not os.path.exists(pk_bin):
    sys.exit("Cannot run pnlclimb without pktrade: {}".format(pk_bin))

def round_sz(sym, market, qty):
    dex_cmd = "{PKDEX_BIN} --action ROUNDQTY --book {MARKET} --symbol {SYM} --tgt {QTY}".format(
        PKDEX_BIN=pkdex_bin, MARKET=market, SYM=sym, QTY=qty
    )
    dex_proc = subprocess.Popen(dex_cmd, shell=True, stdout=subprocess.PIPE)
    dex_proc.wait()

    dex_std, _ = dex_proc.communicate()
    dex_std = dex_std.decode("utf8").split("\n")

    for one_line in dex_std:
        if "QTY" in one_line:
            # The rounded_size is...
            rounded_qty = one_line.split(" ")[-1]
            break
    print(rounded_qty)
    return float(rounded_qty)


class PnlClimber:
    def __init__(
        self,
        work_dir,
        training_dict,
        pred_path,
        pred_name,
        ref_path,
        ref_dct,
        ref_name,
        provided_ins_dates,
        provided_oos_dates,
        pnlclimb_cache_dir,
        daily_stats_dct={},
        remote_sig_dct={},
        remote_sig_name="",
        reference_symbol={},
        remote_symbol={},

        existing_ordex_overrides={}
    ):
        self.pnlclimb_specs = training_dict["pnlclimb_specs"]
        self.tgt_symbol = training_dict["traded_symbols"][0]
        self.traded_markets = training_dict["traded_markets"]

        self.daily_stats_dct = daily_stats_dct

        self.ins_dates = provided_ins_dates
        self.oos_dates = provided_oos_dates

        self.work_dir = work_dir
        if not os.path.exists(work_dir):
            os.mkdir(work_dir)

        self.pred_path = pred_path
        self.pred_name = pred_name

        self.ref_path = ref_path
        self.ref_dct = ref_dct
        self.ref_name = ref_name

        self.remote_sig_dct = remote_sig_dct
        self.remote_sig_name = remote_sig_name


        # Path of the best conffile.
        self.golden_conf_path = ""
        self.total_sims = 0

        self.climb_type = training_dict["pnlclimb_specs"].get("climb_type", "QUAD")
        self.qc_specs = training_dict["pnlclimb_specs"]["qc_specs"]

        # TODO: one day push this inside something else. But for now
        # let's just specify it on the outisde layer.
        # Layer 0 grid search
        # param names : grid values.
        self.grid_params = training_dict["pnlclimb_specs"].get("grid_params", {})

        # If true, then we may update the grid scale of the
        # parameter w.r.t. the local spacing of the param from the grid climb.
        self.update_grid_scaling = training_dict["pnlclimb_specs"].get(
            "update_grid_scaling", False
        )

        self.num_iters = training_dict["pnlclimb_specs"]["num_iters"]

        self.ordex_specs = training_dict["pnlclimb_specs"].get("ordex_specs", {})
        self.custom_climb_params = training_dict["pnlclimb_specs"].get("climb_params", {})

        # Add existing overrides to the ordex_specs too.
        for key, val in existing_ordex_overrides.items():
            if key not in self.ordex_specs:
                self.ordex_specs[key] = val
                print("Adding {} ({}) to ordex_specs".format(key, val))

                # Also re-set the center of the custom_climb_param, if it is in the
                # existing override.
                if key in self.custom_climb_params:
                    #print("Did override for {}: {}->{}".format(key, self.custom_climb_params[key]["proj_value"], val))
                    self.custom_climb_params[key]["proj_value"] = val


        # post-climb finalsim option.
        # Basically, there is *some* noise we introduce when we do fastpnlclimbs.
        # There might be differences in callbacks, and definitely a difference in timestamps. So we can store the best n
        # params and run full sims on each, before picking the best option overall.
        self.finalsim_best_n = training_dict["pnlclimb_specs"].get("finalsim_best_n", 1)

        # list of dicts, {score:float, stats: dict, ordex_params: dict} for determining what to finalsim.
        self.best_n_confs = []

        self.sim_settings = training_dict["pnlclimb_specs"]["sim_settings"]
        self.global_settings = training_dict["pnlclimb_specs"]["global_settings"]
        self.risk_settings = training_dict["pnlclimb_specs"]["risk_settings"]

        self.scoring_spec = training_dict["pnlclimb_specs"]["scoring_spec"]

        self.tradecaller = training_dict["pnlclimb_specs"].get("tradecaller", "final")

        self.tempo_sym = self.tgt_symbol
        self.tempo_mkts =  self.traded_markets

        if training_dict["pnlclimb_specs"].get("tradecall_reference_sym", False) and len(reference_symbol) > 0:
            self.tempo_sym = reference_symbol["symbol"]
            self.tempo_mkts = reference_symbol["markets"]

        if training_dict["pnlclimb_specs"].get("tradecall_remote_sym", False) and len(remote_symbol) > 0:
            self.tempo_sym = remote_symbol["symbol"]
            self.tempo_mkts = remote_symbol["markets"]

        if self.tradecaller == "final":
            self.tempo_dicts = TempoFormer.make_tempo(
                "final",
                {
                    "name": "{}_tradecall_tempo".format(self.tgt_symbol),
                    "symbol": self.tempo_sym,
                    "markets": self.tempo_mkts,
                },
            )["tempos"]

            # For fastcache/climbing, use the original symbol/market
            # (eg. hyperliquid) so we can hopefully have faster
            # iterations.
            self.fast_tempo_dicts =  TempoFormer.make_tempo(
                "final",
                {
                    "name": "{}_tradecall_tempo".format(self.tgt_symbol),
                    "symbol": self.tgt_symbol,
                    "markets": self.traded_markets,
                },
            )["tempos"]
        elif self.tradecaller == "impt":
            self.tempo_dicts = TempoFormer.make_tempo(
                "impt",
                {
                    "name": "{}_tradecall_tempo".format(self.tgt_symbol),
                    "symbol": self.tempo_sym,
                    "markets": self.tempo_mkts,
                    "enable_time_aspect": False,
                    "do_bunch_check": False,
                    "trigger_inside_widen": True,
                },
            )["tempos"]


            self.fast_tempo_dicts = TempoFormer.make_tempo(
                "impt",
                {
                    "name": "{}_tradecall_tempo".format(self.tgt_symbol),
                    "symbol": self.tgt_symbol,
                    "markets": self.traded_markets,
                    "enable_time_aspect": False,
                    "do_bunch_check": False,
                    "trigger_inside_widen": True,
                },
            )["tempos"]

        # Don't do fast cache right now. Don't even try. 
        self.fast_pnl_mode = self.pnlclimb_specs.get("fast_pnl_mode", False)
        self.fast_cache_dir = pnlclimb_cache_dir

        # list of side products that made it past regression. We use this to get the fastpktrader
        # to subscribe to this data too, so we can have approximate matching of callbacks.
        self.side_prods = []
        side_prods_set = set()

        # I read through all the signals and add add the symbol/market pairs.
        if os.path.exists(self.pred_path):
            with open(self.pred_path, "r") as f:
                pred_sigs = json.loads(f.read())

                # Go through the pred sigs, add in all the sym/market groups.
                for one_pred_sig in pred_sigs.get("signals", []):
                    if "symbol" in one_pred_sig and "books" in one_pred_sig:
                        bid_pair = {"symbol": one_pred_sig["symbol"], "market": one_pred_sig["books"][0]}
                        # Sym, Market
                        # After this, make the tuple a dict.
                        bid_pair = (bid_pair["symbol"], bid_pair["market"])
                        side_prods_set.add(bid_pair)

        # OK, go through and add these into the side_prods.
        for one_sid_prod_dct in side_prods_set:
            self.side_prods.append(
                {
                    "symbol": one_sid_prod_dct[0],
                    "market": one_sid_prod_dct[1],
                }
            )

        # The files where we write to for fastsim.
        self.fast_ref_sig_paths = []
        self.fast_pred_sig_paths = []

        # The pattern for filenames for fastsim scanned signals.
        self.ref_vals_tmpl = ""
        self.pred_vals_tmpl = ""

        # The SigFiles...
        self.fast_sigs_dicts = []

        self.logger = getLogger("pnlclimb", "pnlclimb.log")
        self.logger.info("-- Starting Pnlclimb for {} -- ".format(self.tgt_symbol))

        self.cmd_logger = getLogger("pnlclimb_cmds", "pnlclimb_cmds.txt")
        
        # Create a copy of the log in the overmind/pnlclimb_logs directory
        self._setup_local_log_copy()

    def _setup_local_log_copy(self):
        """
        Set up a copy of the pnlclimb log in the overmind/pnlclimb_logs directory.
        This works whether the script is run from REPO/overmind/strat_main/stratbuilder
        or from REPO/pybin.
        """
        # Get the directory of the current script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Navigate to find the overmind directory
        # Start from script_dir and go up until we find overmind
        current_dir = script_dir
        overmind_dir = None
        
        # Look for overmind directory by going up the directory tree
        while current_dir != os.path.dirname(current_dir):  # Stop at root
            if os.path.basename(current_dir) == "overmind":
                overmind_dir = current_dir
                break
            current_dir = os.path.dirname(current_dir)
        
        if overmind_dir is None:
            # Fallback: try to construct path relative to script location
            # Script is in overmind/strat_main/stratbuilder, so go up 3 levels
            overmind_dir = os.path.join(script_dir, "..", "..", "..")
            overmind_dir = os.path.abspath(overmind_dir)
        
        # Create the pnlclimb_logs directory
        pnlclimb_logs_dir = os.path.join(overmind_dir, "pnlclimb_logs")
        os.makedirs(pnlclimb_logs_dir, exist_ok=True)
        
        # Create a unique log filename based on target symbol and timestamp

        # The name should be 
        # each machine should have a machine name 
        file_name = "{MACHINE_NAME}_{SYM}_{STARTEND}_{PNLCLIMB_LABEL}".format(
            MACHINE_NAME= socket.gethostname(), 
            SYM=self.tgt_symbol,
            STARTEND=self.ins_dates[0] + "_" + self.ins_dates[-1],
            PNLCLIMB_LABEL=self.pnlclimb_specs.get("dir_name", "UNKNOWN"),
        )
        local_log_filename = f"{file_name}.log"
        self.local_log_path = os.path.join(pnlclimb_logs_dir, local_log_filename)
        

    # If fast_mode, pre-generate the pred and ref values.
    def gen_cache_fastmode(self):
        # Get the pred, ref paths, and write the

        print("GENERATING CACHE")
        print(self.ins_dates + self.oos_dates)

        pred_scan_path = os.path.join(self.fast_cache_dir, "pred_scan.json")
        ref_scan_path = os.path.join(self.fast_cache_dir, "ref_scan.json")

        pred_sig_dicts = {}
        ref_sig_dicts = {}

        with open(self.pred_path, "r") as f:
            pred_sig_dicts = json.loads(f.read())

        with open(self.ref_path, "r") as f:
            ref_sig_dicts = json.loads(f.read())

        # Construct the pred sample files.
        pred_scan_dict = {
            "signalscanner": {
                "tradecall_tempo": "{}_tradecall_tempo".format(self.tgt_symbol)
            },
            "tempos": self.tempo_dicts,
            "signals": pred_sig_dicts["signals"],
        }

        # Write it down.
        with open(pred_scan_path, "w") as f:
            f.write(json.dumps(pred_scan_dict, indent=2))

        # Same with the ref.
        ref_scan_dict = {
            "signalscanner": {
                "tradecall_tempo": "{}_tradecall_tempo".format(self.tgt_symbol)
            },
            "tempos": self.tempo_dicts,
            "signals": ref_sig_dicts["signals"],
        }
        with open(ref_scan_path, "w") as f:
            f.write(json.dumps(ref_scan_dict, indent=2))

        # Cool, these are written. Now let's go and scan the paths, shall we?

        scan_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode FASTSIM --signals-path {DEST_PATH} --sigs {SIGNAL_NAME}  --times-path {TIME_PATH}'
        self.ref_vals_tmpl = os.path.join(self.fast_cache_dir, "ref_vals_{}.csv")
        self.pred_vals_tmpl = os.path.join(self.fast_cache_dir, "pred_vals_{}.csv")
        time_vals_tmpl = os.path.join(self.fast_cache_dir, "times_{}.csv")

        # Do this for both the ref and the pred.

        # Let's batch this actually. 
        batch_size=24

        scan_cmds = []

        procs = []
        for one_date in list(set(self.ins_dates + self.oos_dates)):
            pred_dest = self.pred_vals_tmpl.format(one_date)
            ref_dest = self.ref_vals_tmpl.format(one_date)

            self.fast_pred_sig_paths.append(pred_dest)
            self.fast_ref_sig_paths.append(ref_dest)

            one_pred_scan_cmd = scan_cmd.format(
                SCANNER_BIN=scan_bin,
                DATE=one_date,
                START=self.global_settings["start_t"],
                END=self.global_settings["end_t"],
                SAMPLING_CONF=pred_scan_path,
                DEST_PATH=pred_dest,
                SIGNAL_NAME=self.pred_name,
                TIME_PATH="/dev/null",
            )

            one_ref_scan_cmd = scan_cmd.format(
                SCANNER_BIN=scan_bin,
                DATE=one_date,
                START=self.global_settings["start_t"],
                END=self.global_settings["end_t"],
                SAMPLING_CONF=ref_scan_path,
                DEST_PATH=ref_dest,
                SIGNAL_NAME=self.ref_name,
                TIME_PATH=time_vals_tmpl.format(one_date),
            )


            if not os.path.exists(pred_dest):
                scan_cmds.append(one_pred_scan_cmd)
                #pred_proc = subprocess.Popen(one_pred_scan_cmd, shell=True)
                #procs.append(pred_proc)

            if not os.path.exists(ref_dest):
                scan_cmds.append(one_ref_scan_cmd)
                #ref_proc = subprocess.Popen(one_ref_scan_cmd, shell=True)
                #procs.append(ref_proc)

        # Batch execute them. 
        if (len(scan_cmds) < batch_size) and len(scan_cmds) > 0:
            batch_size = len(scan_cmds)
    
        n_batches = (len(scan_cmds) // (batch_size)) + 1

        print("N_batches is {}".format(n_batches))
        for i in range(n_batches):
            print("Handling batch {}".format(i))
            cur_batch_procs = []
            cur_batch_cmds = scan_cmds[i*batch_size:(i+1)*batch_size]
            for one_cmd in cur_batch_cmds:
                p = subprocess.Popen(one_cmd, shell=True, text=True)
                cur_batch_procs.append(p)

            for one_proc in cur_batch_procs:
                print("Waiting ")
                one_proc.wait()


        #for one_proc in procs:
        #    one_proc.wait()

        # Should be done now.

        pred_sigfile_dct = {
            "name": self.pred_name,
            "type": "SigFile",
            "file_path_tmpl": self.pred_vals_tmpl,
        }

        ref_sigfile_dct = {
            "name": self.ref_name,
            "type": "SigFile",
            "file_path_tmpl": self.ref_vals_tmpl,
        }

        # self.fast_sigs_dicts = [pred_sigfile_dct, ref_sigfile_dct]

        # Switch to use the real midsig for fastclimb. And include multiple signals if we need it, if we're
        # using liqpx.
        # This way we can get access to bb/ba, which we might use for
        # a few things. This is a hack for sure.
        self.fast_sigs_dicts = [pred_sigfile_dct] +  ref_sig_dicts["signals"]

        self.file_tempo_dct = {
            "name": "{}_tradecall_tempo".format(self.tgt_symbol),
            "type": "FileTempo",
            "file_path": time_vals_tmpl,
        }


    def write_pk_from_params(self, ordex_spec, ordex_params, sigs_list, dest_path, minfv=-1000):
        cur_ordex = ordex_spec.apply_varied_params(ordex_params)["ordex"]
        cur_pkspec = PKFormer.PKSpec(
            self.tgt_symbol,
            self.risk_settings,
            ordex_spec.base_setting["ordex"],
            sigs_list,
            self.tempo_dicts,
            global_settings=self.global_settings,
            sim_settings=self.sim_settings,
            ins_dates_str=",".join(self.ins_dates),
            oos_dates_str=",".join(self.oos_dates),
        )
        pk_dct = cur_pkspec.set_externals(ordex_setting=cur_ordex)
        pk_dct["pktraders"][0]["risk"]["min_pnl"] = minfv

        with open(dest_path, "w") as f:
            f.write(json.dumps(pk_dct, indent=2))


    # Depending on choice. Here we go.
    # pnl: straight up pnl
    # pnl_softvol: use pnl, but do a smoothed scoring (lose money!)
    #                as we get closer to the lowvol limit.
    # pnl_hardvol: use pnl, but below a certain numtrades volume,
    #              vol_limit = 40 # number of roundtrips
    #              penalty = -1000
    # simscore: Use sqrt(pnl) * sharpe
    # sharpe: Use straight up sharpe
    # Example:
    # ss = {"style": "pnl_softvol",  "vol_limit": 30, "penalty_mult": 3}
    # ss = {"style": "pnl_hardvol",  "vol_limit": 30,  "penalty": -200}
    def score_stats(pnlclimber, stats_dict, all_stats_dicts):
        base = PnlClimber._score_stats_base(pnlclimber, stats_dict, all_stats_dicts)
        fillrate_target = pnlclimber.scoring_spec.get("fillrate_target", 0)
        if fillrate_target > 0 and base > 0:
            fr = stats_dict.get("avg_fillrate", 0.0)
            base = base * min(fr / fillrate_target, 1.0)
        return base

    def _score_stats_base(pnlclimber, stats_dict, all_stats_dicts):
        if pnlclimber.scoring_spec["style"] == "pnl":
            return stats_dict["avg_pnl"]
        elif pnlclimber.scoring_spec["style"] == "pnl_softvol":
            # Then, get the max abs pnl.
            abs_pnls = []
            for one_stat_dict in all_stats_dicts:
                abs_pnls.append(abs(one_stat_dict["avg_pnl"]))
            max_abs_pnl = max(abs_pnls)

            vol_style = pnlclimber.scoring_spec.get("vol_style", "avg_numtrds")
            penalty_exp = max(
                (stats_dict[vol_style] - pnlclimber.scoring_spec["vol_limit"])
                / pnlclimber.scoring_spec["vol_limit"],
                -1,
            )
            penalty = (
                np.exp(-penalty_exp) * max_abs_pnl * pnlclimber.scoring_spec["penalty_mult"]
            )
            return stats_dict["avg_pnl"] - penalty
        elif pnlclimber.scoring_spec["style"] == "pnl_hardvol":
            vol_style = pnlclimber.scoring_spec.get("vol_style", "avg_numtrds")

            if stats_dict[vol_style] < pnlclimber.scoring_spec["vol_limit"]:
                return pnlclimber.scoring_spec["penalty"]
            return stats_dict["avg_pnl"]
        elif pnlclimber.scoring_spec["style"] == "wins_pnl":
            return stats_dict["avg_wins_pnl"]
        elif pnlclimber.scoring_spec["style"] == "wins_pnl_softvol":
            # Then, get the max abs pnl.
            abs_pnls = []
            for one_stat_dict in all_stats_dicts:
                abs_pnls.append(abs(one_stat_dict["avg_wins_pnl"]))

            vol_style = pnlclimber.scoring_spec.get("vol_style", "avg_numtrds")
            vol_scale = pnlclimber.scoring_spec.get("vol_scale", "linear_frac_bounded")

            # Figuring out modifier.
            # Maybe only have it matter when negative.
            if vol_scale == "linear_frac_bounded":
                vol_mod = min(
                    stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"], 1.0
                )
            elif vol_scale == "linear_frac_unbounded":
                vol_mod = stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"]
            elif vol_scale == "log":
                # Something that incentivizes volume. But doesn't go super negative.
                vol_mod = math.log(
                     max(stats_dict[vol_style] - pnlclimber.scoring_spec["vol_limit"], 1.5)
                )

            score = stats_dict["avg_wins_pnl"]
            if score > 0:
                score *= vol_mod

            return score
        elif pnlclimber.scoring_spec["style"] == "wins_pnl_charminvol_sharpe":
            # An even softer winsorized climb.

            # 1 - Penalize by volume.
            # 2 - Multiply by sharpe. To promote consistency.
            base_score = stats_dict["avg_wins_pnl"]
            vol_style = pnlclimber.scoring_spec.get("vol_style", "avg_numtrds")
            vol_scale = pnlclimber.scoring_spec.get("vol_scale", "linear_frac_bounded")

            vol_penalty = stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"]
            vol_penalty = min(vol_penalty, 1.0)

            if vol_scale == "linear_frac_bounded":
                vol_mod = min(
                    stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"], 1.0
                )
            elif vol_scale == "linear_frac_unbounded":
                vol_mod = stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"]
            elif vol_scale == "log":
                # Something that incentivizes volume. But doesn't go super negative.
                vol_mod = math.log(
                     max(stats_dict[vol_style] - pnlclimber.scoring_spec["vol_limit"], 1.5)
                )
            if base_score > 0:
                base_score = base_score * vol_mod

            # Maybe easier to interpret if I normalize.
            base_score = base_score * abs(stats_dict["sharpe"])
            return base_score
        elif pnlclimber.scoring_spec["style"] == "wins_pnl_charminvol_pct":
            # An even softer winsorized climb.

            # 1 - Penalize by volume.
            # 2 - Multiply by pct_positive. To promote consistency.
            base_score = stats_dict["avg_wins_pnl"]
            vol_style = pnlclimber.scoring_spec.get("vol_style", "avg_numtrds")
            vol_scale = pnlclimber.scoring_spec.get("vol_scale", "linear_frac_bounded")
            pct_adjust_powr = pnlclimber.scoring_spec.get("pct_adjust_powr", 2)

            if vol_scale == "linear_frac_bounded":
                vol_mod = min(
                    stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"], 1.0
                )
            elif vol_scale == "linear_frac_unbounded":
                vol_mod = stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"]
            elif vol_scale == "log":
                # Something that incentivizes volume. But doesn't go super negative.
                vol_mod = math.log(
                     max(stats_dict[vol_style] - pnlclimber.scoring_spec["vol_limit"], 1.5)
                )
            elif vol_scale == "log_scaled":
                # Something that incentivizes volume. But doesn't go super negative.
                vol_mod = math.log(
                     max(stats_dict[vol_style] /pnlclimber.scoring_spec["vol_limit"], 1.5))
            elif vol_scale == "softmax_scaled":
                # Softmax-like scaling that saturates gracefully
                ratio = stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"]
                vol_mod = 1.0 - math.exp(-ratio)
            elif vol_scale == "sigmoid_scaled":
                # Sigmoid-like scaling that provides smooth transition
                ratio = stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"]
                vol_mod = 1.0 / (1.0 + math.exp(-2 * (ratio - 1.0)))


            if base_score > 0:
                base_score = base_score * vol_mod

            pct_adjust = (0.5 + stats_dict["pct_positive"])
            # Square it for a more extreme effect(?)
            pct_adjust = math.pow(pct_adjust, pct_adjust_powr)
            base_score = base_score * pct_adjust

            # Maybe just do a very simple change. If it's negative, that's bad. So
            # force this as bad and move towards positivity.
            # It's a deceptive thing because thes adjusts might penalize positive trading. It's bad.
            # TODO: intro this to other styles too. But just doing this for now.
            if stats_dict["avg_wins_pnl"] < 0:
                base_score = stats_dict["avg_wins_pnl"]

            return base_score
        elif pnlclimber.scoring_spec["style"] == "trd_pnl_charminvol_pct":
            # An even softer winsorized climb.

            # 1 - Penalize by volume.
            # 2 - Multiply by pct_positive. To promote consistency.
            base_score = stats_dict["avg_trd_scaled_pnl"]
            vol_style = pnlclimber.scoring_spec.get("vol_style", "avg_numtrds")
            vol_scale = pnlclimber.scoring_spec.get("vol_scale", "linear_frac_bounded")

            if vol_scale == "linear_frac_bounded":
                vol_mod = min(
                    stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"], 1.0
                )
            elif vol_scale == "linear_frac_unbounded":
                vol_mod = stats_dict[vol_style] / pnlclimber.scoring_spec["vol_limit"]
            elif vol_scale == "log":
                # Something that incentivizes volume. But doesn't go super negative.
                vol_mod = math.log(
                     max(stats_dict[vol_style] - pnlclimber.scoring_spec["vol_limit"], 1.5)
                )

            if base_score > 0:
                base_score = base_score * vol_mod
            base_score = base_score * (0.5 + stats_dict["pct_positive"])
            return base_score

    # Stop outputting so much. 
    # I removed the full_output option and the trd_suffix, so these climbs write less to disk. 
    def sim_ins(self, sim_dir, conf_path, variant_idx):
        stats = sim_date_range(
            pk_bin,
            sim_dir,
            conf_path,
            self.ins_dates,
            acct_suffix=variant_idx,
#            trd_suffix=variant_idx,
            logger=self.cmd_logger,
            full_output=False,
        )

        # Replace avg pnl

        self.cmd_logger.info("------")
        return stats

    def sim_oos(self, sim_dir, conf_path, variant_idx):
        stats = sim_date_range(
            pk_bin,
            sim_dir,
            conf_path,
            self.oos_dates,
            acct_suffix=variant_idx,
#            trd_suffix=variant_idx,
            logger=self.cmd_logger,
            full_output=False,
        )
        return stats

    def pnl_climb(self):
        # Well, we have the workdir already, no? Great.

        ordex_setting = {}
        signal_setting = {}

        # Let's read the signals.
        pred_sig_dict = {"signals": []}
        ref_sig_dict = {"signals": []}

        if os.path.exists(self.pred_path):
            with open(self.pred_path, "r") as f:
                pred_sig_dict = json.loads(f.read())

        if os.path.exists(self.ref_path):
            with open(self.ref_path, "r") as f:
                ref_sig_dict = json.loads(f.read())

        print(ref_sig_dict)

        # These are both lists.
        sigs_list = pred_sig_dict["signals"] + ref_sig_dict["signals"]
        print(self.remote_sig_dct)
        if self.remote_sig_dct and len(self.remote_sig_dct) > 0:
            print("We did it")
            sigs_list += self.remote_sig_dct["signals"]


        # If we want tow be fast, then we
        # generate the cache files and replace signals with our
        # referencing signals.
        if self.fast_pnl_mode:
            self.gen_cache_fastmode()
            sigs_list = self.fast_sigs_dicts

            # Push the remote sigs in again if we're in certain ordexes.
            if self.remote_sig_dct and len(self.remote_sig_dct) > 0:
                sigs_list += self.remote_sig_dct["signals"]


        # TODO: make this nicer.
        # OK, let's create the ordexformer too.

        # Let's create some ordex_specs if we want.


        ordex_specs = self.pnlclimb_specs.get("ordex_specs", {})
        if self.pnlclimb_specs["ordex_type"] == "simple_cross":
            if "maxpos_usd" in ordex_specs:
                # Back it out from the dailystats.
                desired_maxpos = (
                    ordex_specs["maxpos_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_maxpos < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_maxpos, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_maxpos = round(desired_maxpos, num_digits_in + 2)
                else:
                    desired_maxpos = round(desired_maxpos, 0)

                # Round w.r.t. secmaster.
                desired_maxpos = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_maxpos
                )

                maxpos_str = str(desired_maxpos)
                # Because of passive_out, double the risk maxpos.
                self.risk_settings["max_position"] = str(desired_maxpos * 4)
            else:
                maxpos_str = self.risk_settings["max_position"] / 2

            oxspec = OrdexFormer.SimpleCrossSpec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                # min order size, from somewhere.
                "1",
                self.ref_name,
                self.pred_name,
                # TODO: do I need to create a tradecalling tempo?
                "{}_tradecall_tempo".format(self.tgt_symbol),
                t_between_retries_s=0,
                t_between_execs_s=0,
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )
        elif self.pnlclimb_specs["ordex_type"] == "rel_simple_cross":
            if "maxpos_usd" in ordex_specs:
                # Back it out from the dailystats.
                desired_maxpos = (
                    ordex_specs["maxpos_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_maxpos < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_maxpos, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_maxpos = round(desired_maxpos, num_digits_in + 2)
                else:
                    desired_maxpos = round(desired_maxpos, 0)

                # Round w.r.t. secmaster.
                desired_maxpos = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_maxpos
                )

                maxpos_str = str(desired_maxpos)
                # Because of passive_out, double the risk maxpos.
                self.risk_settings["max_position"] = str(desired_maxpos * 4)
            else:
                maxpos_str = self.risk_settings["max_position"] / 2

            oxspec = OrdexFormer.RelSimpleCrossSpec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                # min order size, from somewhere.
                "1",
                # As with the ordexformer ordering, it goes local_sig, remote_sig,
                # pred_sig.
                self.ref_name,
                self.remote_sig_name,
                self.pred_name,
                # TODO: do I need to create a tradecalling tempo?
                "{}_tradecall_tempo".format(self.tgt_symbol),
                t_between_retries_s=0,
                t_between_execs_s=0,
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )



        elif self.pnlclimb_specs["ordex_type"] == "simple_mm":
            if "ordersize_usd" in ordex_specs:
                # ordersize_usd and maxpos_mult

                desired_ordersize = (
                    ordex_specs["ordersize_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_ordersize < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_ordersize, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_ordersize = round(desired_ordersize, num_digits_in)
                else:
                    desired_ordersize = round(desired_ordersize, 0)

                desired_ordersize = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_ordersize
                )

                desired_maxpos = ordex_specs["maxpos_mult"] * desired_ordersize
                desired_maxpos = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_maxpos
                )

                ordersize_str = str(desired_ordersize)
                maxpos_str = str(desired_maxpos)
                ordex_specs["ordersize_usd"] = ordersize_str
                ordex_specs["max_pos"] = maxpos_str
                # Give it some buffer.
                self.risk_settings["max_position"] = str(desired_maxpos * 1.5)
            else:
                # We just read stuff in to ordex_specs
                ordersize_str = ordex_specs["ordersize_usd"]
                maxpos_str = ordex_specs["max_pos"]

            oxspec = OrdexFormer.SimpleMMSpec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                ordersize_str,
                self.ref_name,
                self.pred_name,
                "{}_tradecall_tempo".format(self.tgt_symbol),
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )
        elif self.pnlclimb_specs["ordex_type"] == "wide_mm":
            if "ordersize_usd" in ordex_specs:
                # ordersize_usd and maxpos_mult

                desired_ordersize = (
                    ordex_specs["ordersize_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_ordersize < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_ordersize, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_ordersize = round(desired_ordersize, num_digits_in + 2)
                else:
                    desired_ordersize = round(desired_ordersize, 0)

                desired_ordersize = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_ordersize
                )
                desired_maxpos = ordex_specs["maxpos_mult"] * desired_ordersize

                ordersize_str = str(desired_ordersize)
                maxpos_str = str(desired_maxpos)
                ordex_specs["ordersize_usd"] = ordersize_str
                ordex_specs["max_pos"] = maxpos_str
                self.risk_settings["max_position"] = maxpos_str
            else:
                # We just read stuff in to ordex_specs
                ordersize_str = ordex_specs["ordersize_usd"]
                maxpos_str = ordex_specs["max_pos"]

            oxspec = OrdexFormer.WideMMSpec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                ordersize_str,
                self.ref_name,
                self.pred_name,
                "{}_tradecall_tempo".format(self.tgt_symbol),
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )
        elif self.pnlclimb_specs["ordex_type"] == "rel_wide_mm2":
            if "ordersize_usd" in ordex_specs:
                # ordersize_usd and maxpos_mult

                desired_ordersize = (
                    ordex_specs["ordersize_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_ordersize < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_ordersize, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_ordersize = round(desired_ordersize, num_digits_in + 2)
                else:
                    desired_ordersize = round(desired_ordersize, 0)

                desired_ordersize = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_ordersize
                )
                desired_maxpos = ordex_specs["maxpos_mult"] * desired_ordersize

                ordersize_str = str(desired_ordersize)
                maxpos_str = str(desired_maxpos)
                ordex_specs["ordersize_usd"] = ordersize_str
                ordex_specs["max_pos"] = maxpos_str
                self.risk_settings["max_position"] = maxpos_str
            else:
                # We just read stuff in to ordex_specs
                ordersize_str = ordex_specs["ordersize_usd"]
                maxpos_str = ordex_specs["max_pos"]

            oxspec = OrdexFormer.RelWideMM2Spec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                ordersize_str,
                self.ref_name,
                self.remote_sig_name,
                self.pred_name,
                "{}_tradecall_tempo".format(self.tgt_symbol),
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )
        elif self.pnlclimb_specs["ordex_type"] == "rel_cross":
            if "ordersize_usd" in ordex_specs:
                desired_ordersize = (
                    ordex_specs["ordersize_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_ordersize < 1:
                    num_digits_in = -math.floor(math.log(desired_ordersize, 10))
                    desired_ordersize = round(desired_ordersize, num_digits_in + 2)
                else:
                    desired_ordersize = round(desired_ordersize, 0)

                desired_ordersize = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_ordersize
                )
                desired_maxpos = ordex_specs["maxpos_mult"] * desired_ordersize

                ordersize_str = str(desired_ordersize)
                maxpos_str = str(desired_maxpos)
                ordex_specs["ordersize_usd"] = ordersize_str
                ordex_specs["max_pos"] = maxpos_str
                self.risk_settings["max_position"] = maxpos_str
            else:
                ordersize_str = ordex_specs["ordersize_usd"]
                maxpos_str = ordex_specs["max_pos"]

            oxspec = OrdexFormer.RelCrossSpec(
                self.traded_markets,
                maxpos_str,
                ordersize_str,
                self.ref_name,
                self.remote_sig_name,
                "{}_tradecall_tempo".format(self.tgt_symbol),
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )
        elif self.pnlclimb_specs["ordex_type"] == "alpha_rel_wide_mm":
            if "ordersize_usd" in ordex_specs:
                # ordersize_usd and maxpos_mult

                desired_ordersize = (
                    ordex_specs["ordersize_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_ordersize < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_ordersize, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_ordersize = round(desired_ordersize, num_digits_in + 2)
                else:
                    desired_ordersize = round(desired_ordersize, 0)

                desired_ordersize = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_ordersize
                )
                desired_maxpos = ordex_specs["maxpos_mult"] * desired_ordersize

                ordersize_str = str(desired_ordersize)
                maxpos_str = str(desired_maxpos)
                ordex_specs["ordersize_usd"] = ordersize_str
                ordex_specs["max_pos"] = maxpos_str
                self.risk_settings["max_position"] = maxpos_str
            else:
                # We just read stuff in to ordex_specs
                ordersize_str = ordex_specs["ordersize_usd"]
                maxpos_str = ordex_specs["max_pos"]

            oxspec = OrdexFormer.AlphaRelWideMMSpec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                ordersize_str,
                self.ref_name,
                self.remote_sig_name,
                self.pred_name,
                "{}_tradecall_tempo".format(self.tgt_symbol),
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )
        elif self.pnlclimb_specs["ordex_type"] == "simple_hedged_mm":
            if "ordersize_usd" in ordex_specs:
                # ordersize_usd and maxpos_mult

                desired_ordersize = (
                    ordex_specs["ordersize_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_ordersize < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_ordersize, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_ordersize = round(desired_ordersize, num_digits_in + 2)
                else:
                    desired_ordersize = round(desired_ordersize, 0)

                desired_ordersize = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_ordersize
                )
                desired_maxpos = ordex_specs["maxpos_mult"] * desired_ordersize

                ordersize_str = str(desired_ordersize)
                maxpos_str = str(desired_maxpos)
                ordex_specs["ordersize_usd"] = ordersize_str
                ordex_specs["max_pos"] = maxpos_str
                self.risk_settings["max_position"] = maxpos_str
            else:
                # We just read stuff in to ordex_specs
                ordersize_str = ordex_specs["ordersize_usd"]
                maxpos_str = ordex_specs["max_pos"]

            oxspec = OrdexFormer.SimpleHedgedMMSpec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                ordersize_str,
                self.ref_name,
                self.pred_name,
                "{}_tradecall_tempo".format(self.tgt_symbol),
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )

        elif self.pnlclimb_specs["ordex_type"] == "relative_mm":
            if "ordersize_usd" in ordex_specs:
                # ordersize_usd and maxpos_mult

                desired_ordersize = (
                    ordex_specs["ordersize_usd"]
                    / self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
                )

                if desired_ordersize < 1:
                    # Then we might want to round it.
                    # Found it to some value. Let's use the med_inside_liq to spec how big a lot size is.
                    # Let's just round it to something nice?
                    num_digits_in = -math.floor(math.log(desired_ordersize, 10))
                    # Increase the number of digits by 2. Just so we don't go crazy.
                    desired_ordersize = round(desired_ordersize, num_digits_in + 2)
                else:
                    desired_ordersize = round(desired_ordersize, 0)

                desired_ordersize = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_ordersize
                )

                desired_maxpos = ordex_specs["maxpos_mult"] * desired_ordersize
                desired_maxpos = round_sz(
                    self.tgt_symbol, self.traded_markets[0], desired_maxpos
                )

                ordersize_str = str(desired_ordersize)
                maxpos_str = str(desired_maxpos)
                ordex_specs["ordersize_usd"] = ordersize_str
                ordex_specs["max_pos"] = maxpos_str
                self.risk_settings["max_position"] = maxpos_str
            else:
                # We just read stuff in to ordex_specs
                ordersize_str = ordex_specs["ordersize_usd"]
                maxpos_str = ordex_specs["max_pos"]

            oxspec = OrdexFormer.RelativeMMSpec(
                self.traded_markets,
                # maxposition, from somewhere. Hardcoding it for now.
                maxpos_str,
                ordersize_str,
                self.ref_name,
                self.remote_sig_name,
                "{}_tradecall_tempo".format(self.tgt_symbol),
                fixed_val_overrides=ordex_specs,
                climbables=self.custom_climb_params,
            )
        # The maybe more correct thing is to use the final callback. It's ok.
        # Let's do the more incorrect thing for now, until I go to to the
        # full on regression.

        climb_tempo = []

        if self.fast_pnl_mode:
            climb_tempo = [self.file_tempo_dct]
        else:
            climb_tempo = self.tempo_dicts

        # Create the pkformer and stuff.
        pkguy = PKFormer.PKSpec(
            self.tgt_symbol,
            self.risk_settings,
            oxspec.base_setting["ordex"],
            sigs_list,
            climb_tempo,
            global_settings=self.global_settings,
            sim_settings=self.sim_settings,
            extra_subs=self.side_prods
        )

        if self.climb_type == "QUAD":
            climber = QuadClimber()
        elif self.climb_type == "PARTICLE":
            climber = ParticleClimber()
        elif self.climb_type == "CMAES":
            climber = CmaEsClimber()
        elif self.climb_type == "BAYES":
            climber = BayesClimber()
        elif self.climb_type == "TPE":
            climber = TpeClimber()
        elif self.climb_type == "TURBO":
            climber = TurboClimber()
        elif self.climb_type == "GROUPED":
            climber = GroupedClimber()

        qc_specs = self.qc_specs
        climb_log = os.path.join(self.work_dir, "pnlclimb_log.txt")

        # Potentially run a first grid search.
        if len(self.grid_params) > 0:
            # Then let's do a grid search.
            # Great. I can use itertools.product.
            var_names = []
            products_lst = []

            # Do a stpe of preprocessing where we might grab percentiles of everything.

            all_pred_vals =[]
            # Go through and find actual grid params.
            # If we need to operate on the pred values, so be it.
            derived_grid_params = {}
            for var_name, var_values in self.grid_params.items():
                if isinstance(var_values, list):
                    derived_grid_params[var_name] = var_values
                elif isinstance(var_values, dict):

                    # If we are in fastmode, we can run this. If not, we can't.
                    if not self.fast_pnl_mode:
                        sys.exit("Trying to gridsearch a derived_value {}, but not in fastpnlmode. This is not allowed".format(var_name))

                    # Check if we loaded pred_values.
                    pred_vals = []
                    if len(all_pred_vals) == 0:
                        pred_vals = []
                        for one_pred_path in self.fast_pred_sig_paths:
                            cur =  np.genfromtxt(one_pred_path, delimiter=",", skip_header=True)
                            if len(cur) == 0:
                                continue
                            pred_vals.append(
                            cur
                            )
                        # Make sure this works correctly btw.
                        all_pred_vals = np.concatenate(pred_vals, axis=0)[:, 1]


                    # Then we need directives.
                    # "enter_thresh": { "mode": "pred_percentile", "low": 99, "high": 99.5, "func": "logspace", "n": 5},
                    # Example.

                    derived_values = []

                    if var_values["mode"] == "pred_percentile":
                        low_perc = np.percentile(all_pred_vals, var_values["low"])
                        high_perc = np.percentile(all_pred_vals, var_values["high"])
                        n = var_values["n"]
                        if var_values["func"] == "geomspace":
                            derived_values = np.geomspace(low_perc, high_perc, n)
                        elif var_values["func"] == "linspace":
                            derived_values = np.linspace(low_perc, high_perc, n)
                        else:
                            sys.exit(
                                "Unknown func for pred_percentile: {} ".format(
                                    var_values["func"]
                                )
                            )
                        self.logger.info(
                            "Grid took percentiles [{}, {}] of {}, got grid values of {}".format(
                                low_perc, high_perc, var_name, derived_values
                            )
                        )
                    else:
                        sys.exit(
                            "Pnlclimb encountered unknown grid mode type: {}".format(
                                var_values["mode"]
                            )
                        )
                    # Save these calculated values.
                    derived_grid_params[var_name] = derived_values

            # Delete pred_vals to free up memory
            del all_pred_vals

            for var_name, var_values in derived_grid_params.items():
                products_lst.append(var_values)
                var_names.append(var_name)

            self.logger.info("Running Layer 0 grid for climb.")

            print(self.grid_params)
            # Run the sims iteratively.
            ordered_product_settings = []
            grid_scores = []
            all_grid_stats = []
            idx = 0
            for one_setting_combo in itertools.product(*products_lst):
                # This specs out a an ordex setting.
                params_to_vals = {}
                for i in range(len(var_names)):
                    params_to_vals[var_names[i]] = one_setting_combo[i]
                oxspec.override_my_dict_settings(params_to_vals)
                ox_dict = oxspec.apply_varied_params(params_to_vals)["ordex"]

                ordered_product_settings.append(params_to_vals)

                # Apply the ordex changes to the whole pk
                pk_dict = pkguy.set_externals(ordex_setting=ox_dict)

                # Write the pk conf.
                pk_path = os.path.join(self.work_dir, "pk_grid_{}.json".format(idx))
                with open(pk_path, "w") as f:
                    f.write(json.dumps(pk_dict, indent=2))
                # THen run the sims.
                # Let's just do the avg pnl?
                sim_results = self.sim_ins(self.work_dir, pk_path, str(idx))
                self.total_sims += 1

                # Just use basic pnl, nothing fancy.
                # I should really score them based on the scoring function.
                # No?
                all_grid_stats.append(sim_results)
                idx += 1

            for idx, one_grid_stat in enumerate(all_grid_stats):
                cur_grid_score = PnlClimber.score_stats(self, one_grid_stat, all_grid_stats)
                grid_scores.append(cur_grid_score)

                self.logger.info(
                    "Grid: {} | pnl {:.2f} sharpe {:.2f} 20pct_trds {:.1f} fillrate {:.4f} score {}".format(
                        idx,
                        one_grid_stat["avg_pnl"],
                        one_grid_stat["sharpe"],
                        one_grid_stat["pct_20_numtrds"],
                        one_grid_stat.get("avg_fillrate", 0.0),
                        cur_grid_score,
                    )
                )

            # Write results file. 
            grid_results_path = os.path.join(self.work_dir, "grid_results.csv")
            # Write the headers. 
            all_lines = ""
            header_line = "variant,"
            for one_var_name in var_names:
                header_line += "{},".format(one_var_name)
            header_line += "avg_pnl,avg_closed_pnl,avg_comm,sharpe,num_trds,fillrate,sim_score\n"
            all_lines += header_line
            # Now, do this for each variant. 
            for idx, cur_sim_results in enumerate(all_grid_stats):
                # variant,values,avg_pnl,sharpe,avg_numtrds
                cur_line = "{},".format(idx)
                var_overrides_dct = ordered_product_settings[idx]
                for one_var_name in var_names:
                    cur_line += "{},".format(var_overrides_dct[one_var_name])
                # Add the results from the sim summary too.
                cur_line += "{:.2f},{:.2f},{:.2f},{:.2f},{:.0f},{:.4f},{:.2f}\n".format(
                    cur_sim_results["avg_pnl"],
                    cur_sim_results["avg_closed_pnl"],
                    cur_sim_results["avg_comm"],
                    cur_sim_results["sharpe"],
                    cur_sim_results["avg_numtrds"],
                    cur_sim_results.get("avg_fillrate", 0.0),
                    grid_scores[idx]
                )
                all_lines += cur_line


            with open(grid_results_path, "w") as f: 
                f.write(all_lines)


            best_score = max(grid_scores)
            best_score_idx = grid_scores.index(best_score)
            best_product_setting = ordered_product_settings[best_score_idx]

            # Get the associated values.
            self.logger.info(
                "Grid done. Best idx {} score is {}.".format(best_score_idx, best_score)
            )
            self.logger.info("Best params are {}".format(best_product_setting))

            # Let's get the associated values then.
            oxspec.fixed_vals = OrdexFormer.OrdexSpec.override_fixed_values(
                oxspec.fixed_vals, best_product_setting
            )
            oxspec.set_varied_param_centers(best_product_setting)

            # Potentially change search sizes of the param too.
            if self.update_grid_scaling:
                # For each param, get the new scaling.
                params_to_scales = {}

                # It is a map of the name to the value.
                best_params = ordered_product_settings[best_score_idx]
                for param_name, best_param_value in best_params.items():
                    # First, check if there is actual variance in here.
                    if len(derived_grid_params[param_name]) > 1:
                        # Then we can check.
                        # This is not the most straightforward but should be good enough
                        # to get us the closest val.
                        closest_val = min(
                            derived_grid_params[param_name],
                            key=lambda one_val: (
                                1e20
                                if one_val == best_param_value
                                else abs(one_val - best_param_value)
                            ),
                        )
                        # Now let's project this diff into the preimage space and then halve it.

                        # Grab the param spec
                        v_p_dct = oxspec.get_varying_param_named(param_name)

                        # Create a VaryingParam and operate on it.
                        v_p = VaryingParam()
                        v_p.from_json(v_p_dct)

                        if v_p is not None:
                            best_val_preimage = v_p.pre_from_proj(best_param_value)
                            closest_val_preimage = v_p.pre_from_proj(closest_val)
                            half_step = (
                                abs(best_val_preimage - closest_val_preimage) / 2
                            )
                            # Save this
                            params_to_scales[param_name] = half_step
                            self.logger.info(
                                "Setting stepsize for {} from {} to {} (due to update_grid_scaling {}--{})".format(
                                    v_p_dct["stepsize"],
                                    param_name,
                                    half_step,
                                    best_val_preimage,
                                    closest_val_preimage,
                                )
                            )
                        else:
                            print(
                                "Could not find varying param named {}".format(
                                    param_name
                                )
                            )

                # Update the relevant params.
                oxspec.update_varying_param_stepsizes(params_to_scales)

        # Start from 0. Create the first conffile and run.
        climbed_param_specs = oxspec.get_varying_params()


        qc_specs["params"] = climbed_param_specs
        climber.init_from_json(qc_specs)
        epoch = 0
        for epoch in range(self.num_iters):
            next_params = climber.get_params_next_epoch()["params_and_scores"]

            self.logger.info("Running iteration {} of climb.".format(epoch))

            pk_scores = []

            # This is a little redundant, just because I don't want to disentangle the web of what is what right now.
            # Sorry. This is just an ordered list of the paramname->paramvals we have for a single climb instance.
            param_sets_this_epoch = []
            for idx, one_param_version in enumerate(next_params):
                param_set = {}
                for one_varying_param in one_param_version["ordered_param_instances"]:
                    param_set[one_varying_param["name"]] = one_varying_param[
                        "projection_value"
                    ]

                param_sets_this_epoch.append(param_set)
                # Use this to modify the ordex.
                ox_dict = oxspec.apply_varied_params(param_set)["ordex"]

                # Apply the ordex changes to the whole pk
                pk_dict = pkguy.set_externals(ordex_setting=ox_dict)

                # Write the pk conf.
                pk_path = os.path.join(self.work_dir, "pk_{}.json".format(idx))
                with open(pk_path, "w") as f:
                    f.write(json.dumps(pk_dict, indent=2))
                # THen run the sims.
                # Let's just do the avg pnl?
                sim_results = self.sim_ins(self.work_dir, pk_path, str(idx))
                self.total_sims += 1
                pk_scores.append(sim_results)


            # Evaluate and rerun.
            for param_idx in range(len(next_params)):
                # Update the score.
                the_score = PnlClimber.score_stats(self, pk_scores[param_idx], pk_scores)
                # Let's add these in.
                instance_param_and_score = {"score": the_score, "stats": pk_scores[param_idx], "ordex_params": param_sets_this_epoch[param_idx]}
                self.best_n_confs.append(instance_param_and_score)

                next_params[param_idx]["score"] = the_score
                next_params[param_idx]["stats"] = pk_scores[param_idx]
            climber.get_results_evaluate(next_params)
            # Write log updates every round.
            with open(climb_log, "w") as f:
                f.write(climber.log_string)
            with open(self.local_log_path, "w") as f:
                f.write(climber.log_string)

            # Cache the best pnlclimbed so far.
            best_so_far_path = os.path.join(self.work_dir, "pk_best_rd{}.json".format(epoch))
            best_so_far_params = climber.get_best_climbed_params()

            sigs_list = pred_sig_dict["signals"] + ref_sig_dict["signals"]
            if len(self.remote_sig_dct) > 0:
                sigs_list += self.remote_sig_dct["signals"]
            if self.remote_sig_dct and len(self.remote_sig_dct) > 0:
                sigs_list += self.remote_sig_dct["signals"]

            self.write_pk_from_params(oxspec, best_so_far_params, sigs_list, best_so_far_path)

            if climber.finished:
                break

        with open(climb_log, "w") as f:
            f.write(climber.log_string)
            f.write("\nTotal sims: {}\n".format(self.total_sims))
        with open(self.local_log_path, "w") as f:
            f.write(climber.log_string)
            f.write("\nTotal sims: {}\n".format(self.total_sims))


        # Done with pnlclimb.
        self.logger.info("Done with climb after {} iterations".format(epoch))
        self.logger.info("Best score was {}".format(climber.best_score_so_far))
        self.logger.info("Total sims: {}".format(self.total_sims))
        # OK, so we have the best params.
        if epoch > 0:
            best_params = climber.get_best_climbed_params()
        else:
            best_params = {}

        # Regardless of whenter we did fastpnlclimb or not,
        # we make the final guy include all the signals.
        final_sigs_list = pred_sig_dict["signals"] + ref_sig_dict["signals"]
        if self.remote_sig_dct and len(self.remote_sig_dct) > 0:
            final_sigs_list += self.remote_sig_dct["signals"]
        # Don't write stats yet, the instrumenter should write
        # sim-stats.


        if self.finalsim_best_n > 1:
            # Then, we look through the best n scores and do full sims of all of them.
            self.logger.info("best_n_confs is like: {}".format(self.best_n_confs))
            sorted_best_confs = sorted(self.best_n_confs, key=lambda x: x["score"], reverse=True)
            sorted_best_confs = sorted_best_confs[:self.finalsim_best_n]

            self.logger.info("after sorting, it looks like  is like: {}".format(sorted_best_confs))

            self.logger.info("Running full sims of the best {} confs.".format(self.finalsim_best_n))

            finalsim_dir = os.path.join(self.work_dir, "finalsim")
            os.makedirs(finalsim_dir, exist_ok=True)

            finalsim_minpnl = -1000

            stats_and_scores = []
            for idx, one_top_setting in enumerate(sorted_best_confs):

                one_top_ordex_params = one_top_setting["ordex_params"]
                # Write the pk conf.
                pk_path = os.path.join(finalsim_dir, "pk_{}.json".format(idx))
                self.write_pk_from_params(oxspec, one_top_ordex_params, final_sigs_list, pk_path, finalsim_minpnl)

                sim_results = self.sim_ins(finalsim_dir, pk_path, str(idx))
                self.total_sims += 1
                stats_and_scores.append(sim_results)

            for idx, one_stats in enumerate(stats_and_scores):
                the_score = PnlClimber.score_stats(self, one_stats, stats_and_scores)
                one_stats["score"] = the_score


            # Find the idx of the best one:
            self.logger.info("Finalsim Scores")
            for idx, one_stats in enumerate(stats_and_scores):
                self.logger.info("For idx {}, score is {} fillrate {:.4f}".format(idx, one_stats["score"], one_stats.get("avg_fillrate", 0.0)))
            self.logger.info("*"*60)
            highest_score_index = max(range(len(stats_and_scores)), key=lambda i: stats_and_scores[i]["score"])
            self.logger.info("Finished running sims, best score is {} with stats {} - finishing".format(
                stats_and_scores[highest_score_index]["score"],
                stats_and_scores[highest_score_index]
            ))

            # Use that to substitute the best_params
            best_params = sorted_best_confs[highest_score_index]["ordex_params"]


        # Update its minfv.
        final_minfv = -1000
        if "ins" in self.daily_stats_dct:
            final_minfv = (
                -self.pnlclimb_specs.get("golive_minpnl_maxpos_mult", 0.05)
                * (float(self.risk_settings["max_position"]))
                * self.daily_stats_dct["ins"]["avg"]["avg_avg_mid"]
            )
            # Round to nearest mult of 5. Just makes it easier.
            final_minfv = round(final_minfv / 5) * 5

        final_dest = os.path.join(self.work_dir, "final_pk.json")
        self.write_pk_from_params(oxspec, best_params, final_sigs_list, final_dest, final_minfv)

        self.golden_conf_path = final_dest
        return final_dest
