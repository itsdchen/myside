#! /usr/bin/env python

import os
import json
import copy
import sys

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from optimizers.VaryingParam import VaryingParam
from util.pathing import templatedir

"""
Given a source for templates, this is a class that lets you translate between the template and useable
json.

"""


ordex_tempaltes = os.path.join(templatedir(), "ordexes")

"""
  What's the operating pattern again. Each signalspec should have a set of fixed vals that it overrides.
  And a set of climbables that it keeps.
  The climbables are ClimbableParams.

  No longer sure what I should be doing but let's just keep writing and fix things as we go along.
"""


class OrdexSpec:
    def __init__(self, template_name, fixed_vals, climbables):
        self.template_name = template_name
        self.template_path = os.path.join(ordex_tempaltes, self.template_name)

        with open(self.template_path, "r") as f:
            self.base_setting = json.loads(f.read())

        self.fixed_vals = fixed_vals
        self.climbables = climbables

        # Map of the param name inside the climb to the location fo the param in the ordex.
        self.climbable_inverted_map = {}

        # This assumes all the settings are in the one ordex section.
        for param_name, param_val in fixed_vals.items():
            # Go and modify them in the base settings.
            self.base_setting["ordex"][param_name] = param_val

        # Also initialize the param val "properly" for the climbed values.
        for param_name, param_setting in climbables.items():
            self.base_setting["ordex"][param_name] = param_setting["proj_value"]

            self.climbable_inverted_map[param_setting["name"]] = param_name

        # Nothing else...? OK.

    def override_fixed_values(fixed_vals, fixed_val_overrides):
        for param_name, param_val in fixed_val_overrides.items():
            fixed_vals[param_name] = param_val
            print("{}:{}".format(param_name, fixed_vals[param_name]))
        return fixed_vals

    # Like apply_varied_params but works for fixed_vals too
    def override_my_dict_settings(self, settings_dct):
        for param_name, param_val in settings_dct.items():
            self.base_setting["ordex"][param_name] = param_val

    # Similar situation as with sigformers.
    # But since there's just one ordex the climbables are not nested... like... no.
    def set_varied_param_centers(self, names_to_vals):
        for name, val in names_to_vals.items():
            if name in self.climbable_inverted_map:
                inv_map_setting = self.climbable_inverted_map[name]
                self.climbables[inv_map_setting]["proj_value"] = val

    def apply_varied_params(self, names_to_vals):
        new_setting = copy.deepcopy(self.base_setting)

        for name, val in names_to_vals.items():
            if name in self.climbable_inverted_map:
                new_setting["ordex"][self.climbable_inverted_map[name]] = val

        return new_setting

    def update_varying_param_stepsizes(self, param_name_to_stepsize):
        for param_name, step_size in param_name_to_stepsize.items():
            if param_name in self.climbable_inverted_map:
                inv_map_setting = self.climbable_inverted_map[param_name]
                self.climbables[inv_map_setting]["stepsize"] = step_size

    def get_varying_param_named(self, name):
        if name in self.climbable_inverted_map:
            return self.climbables[self.climbable_inverted_map[name]]
        return None

    def get_varying_params(self):
        list_of_params = []
        for param_name, param_climbable_dict in self.climbables.items():
            list_of_params.append(param_climbable_dict)

        return list_of_params


# Cool. And now a quick implementation of simplecross.


class SimpleCrossSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        # Note, this is a string.
        max_pos,
        # This is a string too.
        min_order_sz,
        ref_sig_name,
        pred_sig_name,
        trade_caller_name,
        t_between_retries_s=0,
        t_between_execs_s=1,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "SimpleCross.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "min_order_sz": min_order_sz,
            "exit_adjust": 0.9,
            "exit_time_const": 60,
            "overfill_exit_adjust": 0.7,
            "premiss_increase_aggression": False,
            "minfv_thresh_mult": 1,
            "pred_cap": 5e-3,
            "ref_sig": ref_sig_name,
            "pred_sig": pred_sig_name,
            "trade_caller": trade_caller_name,
            "t_between_retries_s": t_between_retries_s,
            "t_between_execs_s": t_between_execs_s,
            "ticks_inside_px": 2,
            "market_all": False,
            # Updating ourselves to use it now.
            # One day. Maybe this can be climbed. Who knows.
            "use_miss_da": False,
            "miss_max_adjust": 0.90,
            "miss_da_time_const": 20,
            "use_cross_da": True,
            "cross_da_max_adjust": 0.2,
            "cross_da_const": 2,
            "cross_da_time_const_s": 30,
            "use_mkt_da": False,
            "mkt_da_max_adjust": 0.1,
            "mkt_da_notional_denom": 50000000,
            "mkt_da_time_const_s": 1800,
            "take_passive_profit": True,
            "passive_profit_t": 1,
            "passive_profit_pct": 0.00715,
            "passive_subtract_per_min": 0.0005,
            "min_passive_profit_pct": 0.0015,
            "midchange_highthresh": True,
            "midchange_maxincrease": 0.4,
            "midchange_time_const_s": 1,
            "effective_spread_time_const_s": 1,
            "fast_mid_decay_const_s": 10,
            "slow_mid_decay_const_s": 120,
            "mid_ema_boost": 0.1,
            "do_mid_ema_boost": False,
            "cross_roundup": False,
            "cross_roundup_sub_thresh": False,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "enter_thresh": {
                    "name": "enter_thresh",
                    "proj_type": "EXP",
                    "proj_value": 3e-4,
                    "stepsize": 0.05,
                },
                "alpha_mult": {
                    "name": "alpha_mult",
                    "proj_type": "EXP",
                    "proj_value": 1.0,
                    "stepsize": 0.02,
                    "chill_n_steps": 0,
                },
                "exit_adjust": {
                    "name": "exit_adjust",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.70,
                    "stepsize": 0.4,
                    "chill_n_steps": 2,
                },
                # "miss_max_adjust": {
                #    "name": "miss_max_adjust",
                #    "proj_type": "LOGISTIC",
                #    "proj_value": 0.8,
                #    "stepsize": 0.25,
                #    "chill_n_steps": 4,
                # },
                "midchange_maxincrease": {
                    "name": "midchange_maxincrease",
                    "proj_type": "EXP",
                    "proj_value": 0.15,
                    "stepsize": 0.15,
                    "chill_n_steps": 2,
                },
                #               "cross_da_max_adjust": {
                #                   "name": "cross_da_max_adjust",
                #                   "proj_type": "EXP",
                #                   "proj_value": 0.5,
                #                   "stepsize": 0.10,
                #                  "chill_n_steps": 2,
                #               },
                # "cross_da_const": {
                #    "name": "cross_da_const",
                #    "proj_type": "EXP",
                #    "proj_value": 1.0,
                #    "stepsize": 0.2,
                #    "chill_n_steps": 2,
                # },
                #               "mkt_da_max_adjust": {
                #                    "name": "mkt_da_max_adjust",
                #                    "proj_type": "LOGISTIC",
                #                    "proj_value": 0.1,
                #                    "stepsize": 0.05,
                #                    "chill_n_steps": 2,
                #                },
                #               "mkt_da_notional_denom": {
                #                    "name": "mkt_da_notional_denom",
                #                    "proj_type": "EXP",
                #                    "proj_value": 20000000,
                #                    "stepsize": 0.05,
                #                    "chill_n_steps": 2,
                #                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


class RelSimpleCrossSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        # Note, this is a string.
        max_pos,
        # This is a string too.
        min_order_sz,
        local_sig_name,
        remote_sig_name,
        pred_sig_name,
        trade_caller_name,
        t_between_retries_s=0,
        t_between_execs_s=1,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "RelSimpleCross.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "min_order_sz": min_order_sz,
            "exit_adjust": 0.9,
            "exit_time_const": 60,
            "overfill_exit_adjust": 0.7,
            "premiss_increase_aggression": False,
            "minfv_thresh_mult": 1,
            "pred_cap": 5e-3,

            # It goes local, remote, pred.
            "local_sig": local_sig_name,
            "remote_sig": remote_sig_name,
            "pred_sig": pred_sig_name,

            "trade_caller": trade_caller_name,
            "ema_time_const_s": 30,

            "t_between_retries_s": t_between_retries_s,
            "t_between_execs_s": t_between_execs_s,
            "ticks_inside_px": 2,
            "market_all": False,
            # Updating ourselves to use it now.
            # One day. Maybe this can be climbed. Who knows.
            "use_miss_da": False,
            "miss_max_adjust": 0.90,
            "miss_da_time_const": 20,
            "use_cross_da": True,
            "cross_da_max_adjust": 0.2,
            "cross_da_const": 2,
            "cross_da_time_const_s": 30,
            "use_mkt_da": False,
            "mkt_da_max_adjust": 0.1,
            "mkt_da_notional_denom": 50000000,
            "mkt_da_time_const_s": 1800,
            "take_passive_profit": True,
            "passive_profit_t": 1,
            "passive_profit_pct": 0.00715,
            "passive_subtract_per_min": 0.0005,
            "min_passive_profit_pct": 0.0015,
            "midchange_highthresh": True,
            "midchange_maxincrease": 0.4,
            "midchange_time_const_s": 1,
            "effective_spread_time_const_s": 1,
            "fast_mid_decay_const_s": 10,
            "slow_mid_decay_const_s": 120,
            "mid_ema_boost": 0.1,
            "do_mid_ema_boost": False,
            "cross_roundup": False,
            "cross_roundup_sub_thresh": False,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "enter_thresh": {
                    "name": "enter_thresh",
                    "proj_type": "EXP",
                    "proj_value": 3e-4,
                    "stepsize": 0.05,
                },
                "alpha_mult": {
                    "name": "alpha_mult",
                    "proj_type": "EXP",
                    "proj_value": 1.0,
                    "stepsize": 0.02,
                    "chill_n_steps": 0,
                },
                "exit_adjust": {
                    "name": "exit_adjust",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.70,
                    "stepsize": 0.4,
                    "chill_n_steps": 2,
                },
                # "miss_max_adjust": {
                #    "name": "miss_max_adjust",
                #    "proj_type": "LOGISTIC",
                #    "proj_value": 0.8,
                #    "stepsize": 0.25,
                #    "chill_n_steps": 4,
                # },
                "midchange_maxincrease": {
                    "name": "midchange_maxincrease",
                    "proj_type": "EXP",
                    "proj_value": 0.15,
                    "stepsize": 0.15,
                    "chill_n_steps": 2,
                },
                "cross_da_max_adjust": {
                    "name": "cross_da_max_adjust",
                    "proj_type": "EXP",
                    "proj_value": 0.5,
                    "stepsize": 0.10,
                    "chill_n_steps": 2,
                },
                "cross_da_const": {
                   "name": "cross_da_const",
                   "proj_type": "EXP",
                   "proj_value": 1.0,
                   "stepsize": 0.2,
                   "chill_n_steps": 2,
                },
                #               "mkt_da_max_adjust": {
                #                    "name": "mkt_da_max_adjust",
                #                    "proj_type": "LOGISTIC",
                #                    "proj_value": 0.1,
                #                    "stepsize": 0.05,
                #                    "chill_n_steps": 2,
                #                },
                #               "mkt_da_notional_denom": {
                #                    "name": "mkt_da_notional_denom",
                #                    "proj_type": "EXP",
                #                    "proj_value": 20000000,
                #                    "stepsize": 0.05,
                #                    "chill_n_steps": 2,
                #                },

                "lmr_time_const_s": {
                    "name": "lmr_time_const_s",
                    "proj_type": "EXP",
                    "proj_value": 10.0,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                }
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


class SimpleMMSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        ref_sig_name,
        pred_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "SimpleMM.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "min_back_levels": 1,
            "max_back_levels": 5,
            "rung_spacing_mult": 0.5,
            "pxdiff_before_safe_mult": 2,
            "ms_between_place": 30,
            "ms_between_cxl": 30,
            "min_ord_lifetime_s": 0.5,
            "getflat_pct_away": 0.01,
            "place_new_backlevels": True,
            "inside_consider_rung_spacing": True,
            "consider_cross": False,
            "print_ords": False,
            "pcurvicity_one_sided": False,
            "disable_tradeout": False,
            "ref_sig": ref_sig_name,
            "pred_sig": pred_sig_name,
            "trade_caller": trade_caller_name,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "alpha_mult": {
                    "name": "alpha_mult",
                    "proj_type": "EXP",
                    "proj_value": 1.0,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "place_thresh": {
                    "name": "place_thresh",
                    "proj_type": "EXP",
                    "proj_value": 1e-4,
                    "stepsize": 0.15,
                },
                "cancel_buffer_frac": {
                    "name": "cancel_buffer_frac",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.2,
                    "stepsize": 0.2,
                    "chill_n_steps": 2,
                },
                "rung_spacing_mult": {
                    "name": "rung_spacing_mult",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.5,
                    "stepsize": 0.2,
                    "proj_mult": 2,
                    "chill_n_steps": 2,
                },
                "pcurvicity": {
                    "name": "pcurvicity",
                    "proj_type": "LOGISTIC",
                    "proj_value": 1.0,
                    "proj_mult": 2,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "pcurvicity_coef": {
                    "name": "pcurvicity_coef",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.5,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


class WideMMSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        ref_sig_name,
        pred_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "WideMM.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "cross_order_size_mult": 2,

            "max_back_levels": 4,
            "rung_spacing_mult": 2,
            "min_ord_lifetime": 0.5,
            "ms_between_place": 50,
            "ms_between_cxl": 50,
            "ms_between_cross": 100,
            "getflat_pct_away": 0.01,

            "want_cross": False,
            "place_thresh_mode": 0,
            "place_thresh_spread_coef": 0.5,
            "print_ords": False,

            "pcurv_denom_ord_mult": -1,
            "pcurvicity_one_sided": False,
            "oneside_pcurv_increases_thresh": False,

            "disable_tradeout": False,

            "mm_gtc": True,

            "use_ladder_ticks": True,
            "ladder_one_sided": True,
            "per_order_n_ticks": 0.2, 

            "rung_smoothly": True,
            "rung_smoothly_n_ticks": 5,


            # We change threshold based on stats on midmkt moves.
            "mid_ems_tdc_s": 10,

            "scale_thresh_midmove_spread": False,
            "scale_thresh_mids_const": 200,
            "scale_midmove_lower_bound": 0.3,

            # Trading more while there are fewer trades.
            "narrow_when_few_trades": False,
            "narrow_fewtrade_minmultiple": 0.8,
            "narrow_thresh_time_const_s": 500,
            "narrow_fewtrade_denom": 2,

            # Using the draped spread.
            "scale_thresh_draped_spread": False,
            "draped_spread_thresh_mult":  2,
            "draped_spread_thresh_min": 0.00015,
            "draped_thresh_mode": 0,
            "drape_tdc_s": 120,

            # Readjust prices to be more profit-taking, if we're close.
            "while_close_bias_takeprofit_B": False,
            "bias_takeprofit_thresh_mult": 1,
            "bias_takeprofit_rel_offset": 0.5,

            "spreadscale_v2": False,
            "spreadscale_v2_bias": False,
            "spreadscale_v2_bias_coef": 1,
            "spreadscale_v2_momentum": False,
            "spreadscale_v2_shape": 0,
            "spreadscale_v2_mult": 1, 
            "spreadscale_v2_denom_const": 20,

            "ref_sig": ref_sig_name,
            "pred_sig": pred_sig_name,
            "trade_caller": trade_caller_name,
        }

        # Lot of thesee things will not work well without the particle climber.

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "place_thresh": {
                    "name": "place_thresh",
                    "proj_type": "EXP",
                    "proj_value": 1e-4,
                    "stepsize": 0.15,
                },
                "cancel_buffer_frac": {
                    "name": "cancel_buffer_frac",
                    "proj_type": "EXP",
                    "proj_value": 0.2,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "cross_thresh": {
                    "name": "cross_thresh",
                    "proj_type": "EXP",
                    "proj_value": 4e-4,
                    "stepsize": 0.05,
                },
                "per_order_n_ticks": {
                    "name": "per_order_n_ticks",
                    "proj_type": "EXP",
                    "proj_value": 0.2,
                    "stepsize": 0.1,
                    "chill_n_steps": 3,
                },
                "mid_ems_tdc_s": {
                    "name": "mid_ems_tdc_s",
                    "proj_type": "EXP",
                    "proj_value": 10,
                    "stepsize": 0.10,
                    "chill_n_steps": 0
                },
                "narrow_fewtrade_minmultiple": {
                    "name": "narrow_fewtrade_minmultiple",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.5,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "narrow_thresh_time_const_s": {
                    "name": "narrow_thresh_time_const_s",
                    "proj_type": "EXP",
                    "proj_value": 500,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "narrow_fewtrade_denom": {
                    "name": "narrow_fewtrade_denom",
                    "proj_type": "EXP",
                    "proj_value": 2,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },

            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)



class AlphaRelWideMMSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        local_sig_name,
        remote_sig_name,
        pred_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "AlphaRelWideMM.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "cross_order_size_mult": 2,

            "ema_time_const_s": 30,

            "max_back_levels": 4,
            "rung_spacing_mult": 2,
            "min_ord_lifetime": 0.5,
            "ms_between_place": 50,
            "ms_between_cxl": 50,
            "ms_between_cross": 100,
            "getflat_pct_away": 0.01,

            "want_cross": False,
            "place_thresh_mode": 0,
            "place_thresh_spread_coef": 0.5,
            "print_ords": False,

            "cross_v2": False,
            "cross_v2_lspread_thresh": 13,
            "cross_v2_exit_adjust": 0.99,
            "cross_v2_sub_thresh": False,
            "cross_v2_resize_w_thresh": False,
            "cross_v2_pred_mode": 1,
            # Keep between 0 and 1 prob. 
            "cross_v2_lmr_dampener": 1,
            "cross_v2_rel_return_filter": 0,
            # Used for filtering. 
            "return_ems_time_const_s": 1,

            # For tracking the localspread which is used for crossv2. 
            "lspread_time_const_s": 120,

            "pcurv_denom_ord_mult": 4,
            "pcurvicity_one_sided": False,
            "oneside_pcurv_increases_thresh": False,

            "disable_tradeout": False,

            "want_add": True,
            "mm_gtc": True,

            # We change threshold based on stats on midmkt moves.
            "thresh_mid_sigmoid_scale": False,
            "mid_ems_tdc_s": 10,
            "dynamic_mid_sigmoid_style": 2,
            "dynamic_mid_sigmoid_base": 0.01,
            "dynamic_mid_sigmoid_offset": 0.5,
            "dynamic_mid_sigmoid_mult": 1,

            "scale_thresh_midmove_spread": False,
            "scale_thresh_mids_const": 200,
            "scale_midmove_lower_bound": 0.3,

            #
            "narrow_when_few_trades": False,
            "narrow_fewtrade_minmultiple": 0.8,
            "narrow_thresh_time_const_s": 500,
            "narrow_fewtrade_denom": 2,

            "local_sig": local_sig_name,
            "remote_sig": remote_sig_name,
            "pred_sig": pred_sig_name,
            "trade_caller": trade_caller_name,
        }

        # Lot of thesee things will not work well without the particle climber.

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "alpha_mult": {
                    "name": "alpha_mult",
                    "proj_type": "EXP",
                    "proj_value": 1.0,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "place_thresh": {
                    "name": "place_thresh",
                    "proj_type": "EXP",
                    "proj_value": 1e-4,
                    "stepsize": 0.15,
                },
                "cancel_buffer_frac": {
                    "name": "cancel_buffer_frac",
                    "proj_type": "EXP",
                    "proj_value": 0.2,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "cross_thresh": {
                    "name": "cross_thresh",
                    "proj_type": "EXP",
                    "proj_value": 4e-4,
                    "stepsize": 0.05,
                },
                "pcurvicity": {
                    "name": "pcurvicity",
                    "proj_type": "LOGISTIC",
                    "proj_value": 1.0,
                    "proj_mult": 4,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "pcurvicity_coef": {
                    "name": "pcurvicity_coef",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.5,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "mid_ems_tdc_s": {
                    "name": "mid_ems_tdc_s",
                    "proj_type": "EXP",
                    "proj_value": 10,
                    "stepsize": 0.10,
                    "chill_n_steps": 0
                },
                "dynamic_place_thresh_exp": {
                    "name": "dynamic_place_thresh_exp",
                    "proj_type": "EXP",
                    "proj_value": 1,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "dynamic_place_thresh_denom": {
                    "name": "dynamic_place_thresh_denom",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.0005,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "dynamic_base": {
                    "name": "dynamic_base",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.01,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "dynamic_mult": {
                    "name": "dynamic_mult",
                    "proj_type": "EXP",
                    "proj_value": 1,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },

                "scale_thresh_mids_const": {
                    "name": "scale_thresh_mids_const",
                    "proj_type": "EXP",
                    "proj_value": 2000,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "narrow_fewtrade_minmultiple": {
                    "name": "narrow_fewtrade_minmultiple",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.5,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "narrow_thresh_time_const_s": {
                    "name": "narrow_thresh_time_const_s",
                    "proj_type": "EXP",
                    "proj_value": 500,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "narrow_fewtrade_denom": {
                    "name": "narrow_fewtrade_denom",
                    "proj_type": "EXP",
                    "proj_value": 2,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "lmr_time_const_s": {
                    "name": "lmr_time_const_s",
                    "proj_type": "EXP",
                    "proj_value": 10.0,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                }

            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)

class RelWideMM2Spec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        local_sig_name,
        remote_sig_name,
        rel_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "RelWideMM2.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,

            "max_back_levels": 5,
            "front_rung_spacing_mult": 0.5,
            "rung_spacing_mult": 1.5,
            "per_backlevel_rung_spacing_mult": 1.5,
            "front_order_random_upper_limit": 1.3,
            "front_order_random_lower_limit": 0.7,
            "order_random_upper_limit": 1.2,
            "order_random_lower_limit": 0.8,
            "can_cross": False,
            "cross_thresh": 0.0015,
            "ms_between_cross": 500,
            "cross_limit_maxpos_frac": 0.20,

            "ms_between_place": 250,
            "ms_between_cxl": 50,

            "min_ord_lifetime": 0.5,

            "place_thresh_mode": 0,
            "place_thresh_spread_coef": 0.5,
            "place_thresh_spread_ema_tdc_s": 120,
            "place_thresh": 0.0005,
            "cancel_buffer": 0.35,

            "extra_buy_thresh": 0.0000,
            "extra_sell_thresh": 0.0000,

            "premium_tdc_s": 120,
            "premium_ema_coef": 0.8,
            "curv_impulse_tdc": 240,
            "curv_impulse_coef": 0,
            "pred_momentum_tdc": 30,
            "pred_momentum_coef": 0.0,
            "enable_queue_jump": False,
            "queue_jump_size_mult": 3,
            "equity_off_hour_vwap": False,
            "mm_gtc": False,
            "ladder_one_sided": False,
            "per_order_widen_frac": 0.8,

            "local_sig": local_sig_name,
            "remote_sig": remote_sig_name,
            "rel_sig": rel_sig_name,
            "trade_caller": trade_caller_name,
        }

        # Lot of thesee things will not work well without the particle climber.

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "place_thresh": {
                    "name": "place_thresh",
                    "proj_type": "EXP",
                    "proj_value": 1e-4,
                    "stepsize": 0.15,
                },
                "cancel_buffer": {
                    "name": "cancel_buffer",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.2,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "per_order_widen_frac": {
                    "name": "per_order_widen_frac",
                    "proj_type": "EXP",
                    "proj_value": 0.8,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "pred_momentum_coef": {
                    "name": "pred_momentum_coef",
                    "proj_type": "EXP",
                    "proj_value": 0.8,
                    "stepsize": 0.10,
                    "chill_n_steps": 0
                },
                "pred_momentum_tdc": {
                    "name": "pred_momentum_tdc",
                    "proj_type": "EXP",
                    "proj_value": 20,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "premium_ema_coef": {
                    "name": "premium_ema_coef",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.8,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                }
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


class RelCrossSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        local_sig_name,
        remote_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "RelCross.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "base_cross_thresh": 0.0006,
            "exit_adjust": 1.0,
            "per_order_widen_frac": 0.1,
            "ms_between_cross": 0,
            "cross_limit_maxpos_frac": 0.5,
            "premium_tdc_s": 30,
            "premium_ema_coef": 0.5,
            "curv_impulse_tdc": 1,
            "curv_impulse_coef": 0,
            "pred_momentum_tdc": 30,
            "pred_momentum_coef": 0,
            "cross_price_mode": 0,
            "spread_thresh_coef": 0,
            "size_signal_max_mult": 0,
            "hold_decay_tdc_s": 0,
            "hold_decay_floor": 0.1,
            "forced_exit_after_s": 0,
            "remote_mom_tdc_ms": 1000,
            "remote_mom_coef": 0,
            "passive_exit_enabled": False,
            "passive_exit_pct": 0.002,
            "passive_exit_style": 0,
            "passive_exit_decay_per_min": 0.0005,
            "passive_exit_min_pct": 0.0002,
            "local_sig": local_sig_name,
            "remote_sig": remote_sig_name,
            "trade_caller": trade_caller_name,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        if len(climbables) == 0:
            climbables = {
                "base_cross_thresh": {
                    "name": "base_cross_thresh",
                    "proj_type": "EXP",
                    "proj_value": 6e-4,
                    "stepsize": 0.15,
                },
                "exit_adjust": {
                    "name": "exit_adjust",
                    "proj_type": "LOGISTIC",
                    "proj_value": 1.0,
                    "stepsize": 0.25,
                    "chill_n_steps": 2,
                },
                "per_order_widen_frac": {
                    "name": "per_order_widen_frac",
                    "proj_type": "EXP",
                    "proj_value": 0.1,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "premium_ema_coef": {
                    "name": "premium_ema_coef",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.5,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


# Basically copies the SimpleMM.
class SimpleHedgedMMSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        ref_sig_name,
        pred_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "SimpleHedgedMM.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "min_back_levels": 1,
            "max_back_levels": 4,
            "rung_spacing": 4,
            "pxdiff_before_safe_mult": 2,
            "ms_between_place": 40,
            "ms_between_cxl": 50,
            "min_ord_lifetime": 0.5,
            "getflat_pct_away": 0.01,
            "place_new_backlevels": True,
            "print_ords": False,
            "pcurvicity_one_sided": False,
            "disable_tradeout": True,
            "ref_sig": ref_sig_name,
            "pred_sig": pred_sig_name,
            "trade_caller": trade_caller_name,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "alpha_mult": {
                    "name": "alpha_mult",
                    "proj_type": "EXP",
                    "proj_value": 1.0,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "place_thresh": {
                    "name": "place_thresh",
                    "proj_type": "EXP",
                    "proj_value": 1e-4,
                    "stepsize": 0.15,
                },
                "cancel_buffer_frac": {
                    "name": "cancel_buffer_frac",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.2,
                    "stepsize": 0.2,
                    "chill_n_steps": 2,
                },
                "pcurvicity": {
                    "name": "pcurvicity",
                    "proj_type": "LOGISTIC",
                    "proj_value": 1.0,
                    "proj_mult": 4,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "pcurvicity_coef": {
                    "name": "pcurvicity_coef",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.5,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


# Basically copies the SimpleMM.
class RelativeMMSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        ref_sig_name,
        remote_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "RelativeMM.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "max_pos_mult": 5,
            "local_sig": ref_sig_name,
            "remote_sig": remote_sig_name,
            "trade_caller": trade_caller_name,
            "ema_time_const_s": 30,
            "place_thresh_scale_spread": True,
            "place_thresh": 10,
            "consider_cross_from_thresh": False,
            "cross_thresh": 0.001,
            "cross_order_mult": 3,
            "cross_uses_curv": False,
            "cross_roundup": False,
            "cross_roundup_sub_thresh": False,
            "cancel_buffer_frac": 0.5,
            "cutin_round_to_book": True,
            # gtc vs alo for order placement.
            "mm_gtc": True,
            # ezmode things.
            "enable_ez_mode": True,
            "ez_sided_follow_pred": True,
            "ez_mode_cutoff": 1,
            "min_back_levels": 1,
            "max_back_levels": 5,
            "rung_spacing_mult": 1,
            "pcurvicity": 1,
            "pcurvicity_coef": 0.5,
            "pcurvicity_one_sided": False,
            "getflat_pct_away": 0.01,
            "min_ord_lifetime_s": 2,
            "ms_between_place": 1000,
            "ms_between_cxl": 1000,
            "take_passive_profit": False,
            "passive_profit_t": 180,
            "passive_profit_pct": 0.015,
            "pause_after_fill_side_s": 2,
            "mkt_data_tdc_s": 60,
            "do_v1cross": True,
            "v1cross_maxpos_frac": 0.5,
            "v1cross_trd_ratio_lthresh": 0.5,
            "v1cross_trd_not_thresh": 10000,
            "v1cross__mkt_move_thresh": 0.01,
            "v1cross_t_between_fires_s": 10,
            "bias_thresh_trd_ratio": True,
            "trd_bias_coef": 1,
            "scale_thresh_mid_ratio": True,
            "scale_thresh_mids_const": 20,
            "scale_thresh_midmove_spread": True,
            "scale_midmove_lower_bound": 0.3,
            "place_new_backlevels": False,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "place_thresh": {
                    "name": "place_thresh",
                    "proj_type": "EXP",
                    # I'm using the spread-version of the place_thresh.
                    #                    "proj_value": 0.0005,
                    "proj_value": 5,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "cancel_buffer_frac": {
                    "name": "cancel_buffer_frac",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.2,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "cross_thresh": {
                    "name": "cross_thresh",
                    "proj_type": "EXP",
                    "proj_value": 0.0008,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "pcurvicity": {
                    "name": "pcurvicity",
                    "proj_type": "LOGISTIC",
                    "proj_value": 1.0,
                    "proj_mult": 4,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "pcurvicity_coef": {
                    "name": "pcurvicity_coef",
                    "proj_type": "LOGISTIC",
                    "proj_value": 2.5,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
                "v1cross_trd_ratio_lthresh": {
                    "name": "v1cross_trd_ratio_lthresh",
                    "proj_type": "EXP",
                    "proj_value": 0.5,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "v1cross__mkt_move_thresh": {
                    "name": "v1cross__mkt_move_thresh",
                    "proj_type": "EXP",
                    "proj_value": 0.01,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "trd_bias_coef": {
                    "name": "trd_bias_coef",
                    "proj_type": "EXP",
                    "proj_value": 1,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "scale_thresh_mids_const": {
                    "name": "scale_thresh_mids_const",
                    "proj_type": "EXP",
                    "proj_value": 20,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "ema_time_const_s": {
                    "name": "ema_time_const_s",
                    "proj_type": "EXP",
                    "proj_value": 600,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


# Basically copies the SimpleMM.
class ManderSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        ref_sig_name,
        remote_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "Mander.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "local_sig": ref_sig_name,
            "remote_sig": remote_sig_name,
            "trade_caller": trade_caller_name,
            "revert_order_size_mult": 2,
            "no_adding": False,
            "no_crossing": True,
            "curv_coef": 1,
            "add_place_thresh_mult": 0.5,
            "add_cxl_buf_mult": 0.2,
            "rung_spacing_tick_mult": 3,
            "favor_place_inside": False,
            "add_rr_buy_limit": 0.1,
            "add_frrnot_limit": 50000,
            "add_buy_rr_joint_limit": 0.5,
            "add_buy_frr_joint_limit": 0.25,
            "add_buy_favor_rr_limit": 0.75,
            "add_buy_favor_frr_limit": 2.5,
            "t_between_cross_s": 30,
            "cross_size_mult": 6,
            "cross_buy_rbs_frac": 4,
            "cross_buy_frbs_frac": 8,
            "cross_sell_rbs_frac": 0.25,
            "cross_sell_frbs_frac": 0.125,
            "cross_recent_not_limit": 250000,
            "frbs_lmr_spread_mult": 8,
            "min_ord_lifetime_s": 2,
            "ms_between_place": 1000,
            "ms_between_cxl": 1000,
            "pause_after_fill_side_s": 2,
            "ema_time_const_s": 10,
            "fast_ema_time_const_s": 2,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                # Units of spread
                # Note that before, I had this at
                # "add_place_thresh_mult": 30,
                "add_place_thresh_mult": {
                    "name": "add_place_thresh_mult",
                    "proj_type": "EXP",
                    "proj_value": 8,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                # Units of spread.
                "add_cxl_buf_mult": {
                    "name": "add_cxl_buf_mult",
                    "proj_type": "EXP",
                    "proj_value": 2.5,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "curv_coef": {
                    "name": "curv_coef",
                    "proj_type": "EXP",
                    "proj_value": 1,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "rung_spacing_tick_mult": {
                    "name": "rung_spacing_tick_mult",
                    "proj_type": "EXP",
                    "proj_value": 4,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "add_rr_buy_limit": {
                    "name": "add_rr_buy_limit",
                    "proj_type": "EXP",
                    "proj_value": 0.1,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "add_buy_rr_joint_limit": {
                    "name": "add_buy_rr_joint_limit",
                    "proj_type": "EXP",
                    "proj_value": 0.5,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "add_buy_frr_joint_limit": {
                    "name": "add_buy_frr_joint_limit",
                    "proj_type": "EXP",
                    "proj_value": 0.25,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "add_buy_favor_frr_limit": {
                    "name": "add_buy_favor_frr_limit",
                    "proj_type": "EXP",
                    "proj_value": 2.5,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "cross_buy_frbs_frac": {
                    "name": "cross_buy_frbs_frac",
                    "proj_type": "EXP",
                    "proj_value": 8,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "frbs_lmr_spread_mult": {
                    "name": "frbs_lmr_spread_mult",
                    "proj_type": "EXP",
                    "proj_value": 8,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "ema_time_const_s": {
                    "name": "ema_time_const_s",
                    "proj_type": "EXP",
                    "proj_value": 10.0,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)


# Basically copies the SimpleMM.
class InsideMMSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        ref_sig_name,
        remote_sig_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "InsideMM.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "local_sig": ref_sig_name,
            "remote_impulse_sig": remote_sig_name,
            "trade_caller": trade_caller_name,
            "remote_sig_timedecay_s": 120,
            "use_fast_remote_sig_ema": False,
            "fast_remote_sig_timedecay_s": 2,
            "follow_remote_signal": True,
            "remote_signal_follow_frac": 0.3,
            "follow_frac_cxl_mult": 1.5,
            "cutin_if_multispread": False,
            "per_order_curvicity_mult": 0,
            "ms_between_place": 20,
            "ms_between_cxl": 20,
            "min_ord_lifetime_s": 0.5,
            "pause_after_fill_side_s": 2,
            "consider_cross": False,
            "actually_cross_fire": False,
            "cross_thresh_ratio": 2.5,
            "t_between_cross_s": 3,
            "cross_maxpos_mult": 1,
            "cancel_buf_frac": 0.45,
        }

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                # Units of spread
                # Note that before, I had this at
                # "add_place_thresh_mult": 30,
                "remote_sig_timedecay_s": {
                    "name": "remote_sig_timedecay_s",
                    "proj_type": "EXP",
                    "proj_value": 120,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "remote_signal_follow_frac": {
                    "name": "remote_signal_follow_frac",
                    "proj_type": "LOGISTIC",
                    "proj_value": 0.3,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "follow_frac_cxl_mult": {
                    "name": "follow_frac_cxl_mult",
                    "proj_type": "EXP",
                    "proj_value": 1.5,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "per_order_curvicity_mult": {
                    "name": "per_order_curvicity_mult",
                    "proj_type": "EXP",
                    "proj_value": 1,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "add_place_thresh_mult": {
                    "name": "add_place_thresh_mult",
                    "proj_type": "EXP",
                    "proj_value": 4,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)



class HLNUMMSpec(OrdexSpec):
    def __init__(
        self,
        markets,
        max_pos,
        order_sz,
        ref_sig_name,
        pred_sig_name,
        numm_liq_name,
        trade_caller_name,
        fixed_val_overrides={},
        climbables={},
    ):
        template_name = "HLNUMM.json"
        fixed_vals = {
            "markets": markets,
            "max_pos": max_pos,
            "order_size": order_sz,
            "cross_order_size_mult": 2,

            "max_back_levels": 4,
            "rung_spacing_mult": 2,
            "min_ord_lifetime": 0.5,
            "ms_between_place": 50,
            "ms_between_cxl": 50,
            "ms_between_cross": 100,
            "getflat_pct_away": 0.01,

            "want_add": True,
            "want_cross": False,
            "print_ords": False,

            "per_fill_skew_ticks": 0.01,
            "skew_one_sided": True,

            "do_emergency_cancel": True,
            "mm_gtc": True,

            "large_order_mult": 1,
            "large_order_thresh": 75,

            "mid_ems_tdc_s": 10,


            "disable_tradeout": False,

            "mm_gtc": True,


            "spreadscale_v2": False,
            "spreadscale_v2_momentum": False,
            "spreadscale_v2_shape": 0,
            "spreadscale_v2_mult": 1, 
            "spreadscale_v2_denom_const": 20,

            "ref_sig": ref_sig_name,
            "pred_sig": pred_sig_name,
            "liq_sig": numm_liq_name,

            "trade_caller": trade_caller_name,
        }

        # Lot of thesee things will not work well without the particle climber.

        fixed_vals = OrdexSpec.override_fixed_values(fixed_vals, fixed_val_overrides)

        # THen we give it... some climbables.

        # TODO: create scaleExitFromEnter.
        if len(climbables) == 0:
            # Setting it somewhat close to the commissions.
            climbables = {
                "alpha_mult": {
                    "name": "alpha_mult",
                    "proj_type": "EXP",
                    "proj_value": 1.0,
                    "stepsize": 0.10,
                    "chill_n_steps": 0,
                },
                "inside_liq_spread_thresh": {
                    "name": "inside_liq_spread_thresh",
                    "proj_type": "EXP",
                    "proj_value": 1,
                    "stepsize": 0.15,
                },
                "cancel_buffer_frac": {
                    "name": "cancel_buffer_frac",
                    "proj_type": "EXP",
                    "proj_value": 0.2,
                    "stepsize": 0.1,
                    "chill_n_steps": 2,
                },
                "cross_thresh": {
                    "name": "cross_thresh",
                    "proj_type": "EXP",
                    "proj_value": 4e-4,
                    "stepsize": 0.05,
                },
                "per_fill_skew_ticks": {
                    "name": "per_fill_skew_ticks",
                    "proj_type": "EXP",
                    "proj_value": 0.01,
                    "stepsize": 0.1,
                    "chill_n_steps": 3,
                },
                "return_ems_time_const_s": {
                    "name": "return_ems_time_const_s",
                    "proj_type": "EXP",
                    "proj_value": 10,
                    "stepsize": 0.10,
                    "chill_n_steps": 0
                },
                "spreadscale_v2_mult": {
                    "name": "spreadscale_v2_mult",
                    "proj_type": "EXP",
                    "proj_value": 1,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },

                "spreadscale_v2_denom_const": {
                    "name": "spreadscale_v2_denom_const",
                    "proj_type": "EXP",
                    "proj_value": 15,
                    "stepsize": 0.25,
                    "chill_n_steps": 3,
                },
            }

        OrdexSpec.__init__(self, template_name, fixed_vals, climbables)
