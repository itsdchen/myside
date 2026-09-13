from optimizers.VaryingParam import VaryingParam

from optimizers.BaseClimb import BaseClimber

import copy
import numpy as np
import json

"""
Somewhat parabolic hill climbing algorithm.


"""

# These are the various params-and-score values that we get.
# I haven't incorporated this into the rest of the climb yet.
class ParamVariant:
    def __init__(
        self, ordered_param_instances, varying_param_name, score=-1e4, stats={}
    ):
        self.ordered_param_instances = ordered_param_instances
        self.varying_param_name = varying_param_name
        self.score = score
        self.stats = stats


# Given a specific climbed param, stores all its actions between the
# differnt epochs. Helps us track and perform more globa-level changes for
# the param.
class ParamClimbStats:
    def __init__(self, name):
        self.name = name

        self.epoch_to_stats = {}

        pass

    # So far, it looks like this.
    #        {
    #            "center_progression": , # values: INC, DEC, MID
    #            "best_value": , # float
    #            "best_value_type": , #
    #            "best_value_score": , # float
    #            "step_change": , # SHRINK, EXPAND
    #        }
    def updateStatsEpoch(self, epoch_num, stats_dct):
        if not epoch_num in self.epoch_to_stats:
            self.epoch_to_stats[epoch_num] = stats_dct
        else:
            self.epoch_to_stats[epoch_num].update(stats_dct)

    # Various getters and stuff.
    def getLastValues(self, stat_name):
        epochs = list(self.epoch_to_stats.keys())
        epochs.sort()
        to_ret = []
        for i in epochs:
            to_ret.append(self.epoch_to_stats[i][stat_name])
        return to_ret


class QuadClimber(BaseClimber):
    def __init__(self):
        self.epoch = 0
        self.min_epochs = 5
        self.max_epochs = 10
        self.can_snapback = False
        self.snapback_frac = 0.9
        self.prefer_best = False

        # If this is set, we expand while recentering on the best so far
        # when evaluating.
        self.expand_all_negative = False

        # Ordered
        self.climbed_param_names = []

        # param_name : varyingparam
        self.pt_objs = {}

        self.num_snapbacks = 0
        # I can't imagine us snapping back that much.
        # First one is  unused.
        self.snapback_div_factors = [1.5, 1.5, 2.0, 1.5, 1.25, 1.25, 1.25, 1.25, 1.25, 1.5, 1.25, 1.25, 1.25, 1.25, 1.25]

        self.shrink_aggressively = False
        """

        Formats look like:
        0: {

           params_and_scores: [
                 # I'm going to call it a parameterization. Bad name.
                 {'ordered_param_instances':
                   [{'name': XXX, 'projection_value': XXX, 'current_value': XXX, 'stepsize': XXX, 'num_shrinks': XXX, 'num_expands': XXX,
                      'finished': XXX,  'has_run': XXX}, ...],
                  'varying_param': XXX,
                  'score': XXX,
                  'stats': XXX }],
           'best_score_stats': XXX,
           'best_score': XXX,
           'best_param': [{'name': XXX, 'projection_value': XXX, ...}]
        ],
        1: {}...
        }

        """
        self.epochs_to_params = {}

        # I'll start replacing params_to_ts with param_stats.
        self.param_stats = {}

        self.cur_center = []

        self.required_delta_params = 0
        self.stop_conditions = {
            "max_epochs": 10,
            "center_pct_increase": 0.05,
            "pct_change_variants": 0.05,
        }

        self.best_param_so_far = []
        self.best_stats_so_far = {}
        self.best_score_so_far = -3000

        self.finished = False
        self.log_string = ""
        self.cur_round_log = ""

        self.epsilon = 1e-5

    def init_from_json(self, spec_dict):
        self.epoch = 0
        self.min_epochs = spec_dict["min_epochs"]
        self.max_epochs = spec_dict["max_epochs"]

        self.can_snapback = spec_dict.get("can_snapback", False)
        self.snapback_frac = spec_dict.get("snapback_frac", 0.9)

        self.prefer_best = spec_dict.get("prefer_best", True)
        self.expand_all_negative = spec_dict.get("expand_all_negative", False)

        self.shrink_aggressively = spec_dict.get("shrink_aggressively", False)

        self.required_delta_params = float(spec_dict.get("required_delta_params", 0))

        if "stop_conditions" in spec_dict:
            self.stop_conditions = spec_dict["stop_conditions"]

        zero_epoch_settings = []
        for one_param_json in spec_dict["params"]:
            one_param_transform = VaryingParam()
            one_param_transform.from_json(one_param_json)

            self.climbed_param_names.append(one_param_transform.name)
            self.pt_objs[one_param_transform.name] = one_param_transform

            cur_param_dict = {
                "name": one_param_transform.name,
                "current_value": one_param_transform.pre_val,
                "projection_value": one_param_transform.proj_val,
                "num_shrinks": 0,
                "num_expands": 0,
                "finished": False,
                "stepsize": one_param_transform.stepsize,
            }

            if one_param_transform.proj_type == "CONSTANT":
                cur_param_dict["finished"] = True

            # Add it to the list too.
            zero_epoch_settings.append(cur_param_dict)

        # print(zero_epoch_settings)
        self.cur_center = zero_epoch_settings
        self.get_params_next_epoch()

        for one_param in self.climbed_param_names:
            self.param_stats[one_param] = ParamClimbStats(one_param)

    def pretty_print_epochs_to_params(
        self, epoch_num, print_projected_vals=True, do_print=True
    ):
        param_str = ""
        epoch_setting = self.epochs_to_params[epoch_num]

        if do_print:
            print("*" * 100)

        param_str += "\nRound {}\n".format(epoch_num)

        for pn in self.climbed_param_names:
            param_str += "{},".format(pn)

        is_pnlscores = False
        if (
            "stats" in epoch_setting["params_and_scores"][0]
            and len(epoch_setting["params_and_scores"][0]["stats"]) > 0
        ):
            is_pnlscores = True

        # Potentially add scores.
        if (
            len(epoch_setting["params_and_scores"]) > 0
            and epoch_setting["params_and_scores"][0]["has_run"]
        ):
            # Can also check if pnl is in here but not rn.
            if is_pnlscores:
                param_str += "net_pnl,closed_pnl,comm,sharpe,num_trds,"
            param_str += "score"

        else:
            # rm trailing comma.
            param_str = param_str[:-1]

        param_str += "\n"

        params_and_scores = epoch_setting["params_and_scores"]
        for one_param_group_setting in params_and_scores:
            for single_param_setting in one_param_group_setting[
                "ordered_param_instances"
            ]:
                # Reduce the amount of sig figs. 
                if print_projected_vals:
                    param_str += "{:.6f},".format(single_param_setting["projection_value"])
                else:
                    param_str += "{:.6f},".format(single_param_setting["current_value"])

            # DOn't print if hasnt run
            if one_param_group_setting["has_run"]:
                if is_pnlscores:
                    param_str += "{:.4f},{:.4f},{:.4f},{:.4f},{:.4f},".format(
                        one_param_group_setting["stats"]["avg_pnl"],
                        one_param_group_setting["stats"]["avg_closed_pnl"],
                        one_param_group_setting["stats"]["avg_comm"],
                        one_param_group_setting["stats"]["sharpe"],
                        one_param_group_setting["stats"]["avg_numtrds"],
                    )

                param_str += "{:.6f}".format(one_param_group_setting["score"])

            else:
                # Remove trailing comma
                param_str = param_str[:-1]
            param_str += "\n"
        return param_str

    def get_best_climbed_params(self):
        toret = {}
        if self.prefer_best:
            for one_param_val in self.best_param_so_far:
                toret[one_param_val["name"]] = one_param_val["projection_value"]
        else:
            best_epoch = self.epochs_to_params[self.epoch]
            if not best_epoch["best_param"] and self.epoch > 0:
                best_epoch = self.epochs_to_params[self.epoch - 1]
            for one_param_val in best_epoch["best_param"]:
                toret[one_param_val["name"]] = one_param_val["projection_value"]
        return toret

    def get_best_score(self):
        return self.best_score_so_far

    # Returns a list of param settings
    # First entry is the "center" that was calculated from the previous round, or from init.
    # The subsequent entries are settings where we let specific parameter values vary, one
    # at a time. Right now, this is coupled with the climb type. In the future, we should
    # add some frickin metadata and ish.
    def get_params_next_epoch(self):
        # Starting from the center, vary params and generate the search grid.

        params_and_scores = [
            {
                "ordered_param_instances": self.cur_center,
                "varying_param": "",
                "has_run": False,
                "stats": {},
                "score": -5000,
            }
        ]

        for i, _ in enumerate(self.cur_center):
            # If this param is "done", don't let it vary no more.
            if self.cur_center[i]["finished"]:
                continue

            # Check against the chillout period.
            the_pt_obj = self.pt_objs[self.cur_center[i]["name"]]
            if self.epoch < the_pt_obj.chill_n_steps:
                #self.log_string += (
                #    "\nSkipping iteration for {} because epoch {}<{}".format(
                #        the_pt_obj.name, self.epoch, the_pt_obj.chill_n_steps
                #    )
                #)
                continue

            # Prefer deep copies just in case.
            progressive_params_low = copy.deepcopy(self.cur_center)
            progressive_params_high = copy.deepcopy(self.cur_center)

            associated_param = progressive_params_low[i]
            associated_param_obj = self.pt_objs[associated_param["name"]]

            # Vary the param by the current stepsize
            low_current_value = (
                self.cur_center[i]["current_value"] - self.cur_center[i]["stepsize"]
            )

            low_projected_value = associated_param_obj.proj_from_pre(low_current_value)

            # Set this in the low object.
            progressive_params_low[i]["current_value"] = low_current_value
            progressive_params_low[i]["projection_value"] = low_projected_value

            # Same thing for high value.

            # Vary the param by the current stepsize
            high_current_value = (
                self.cur_center[i]["current_value"] + self.cur_center[i]["stepsize"]
            )

            high_projected_value = associated_param_obj.proj_from_pre(
                high_current_value
            )

            # Set this in the low object.
            progressive_params_high[i]["current_value"] = high_current_value
            progressive_params_high[i]["projection_value"] = high_projected_value

            params_and_scores.append(
                {
                    "ordered_param_instances": progressive_params_low,
                    "has_run": False,
                    "varying_param": associated_param["name"],
                    "stats": {},
                    "score": -5000,
                }
            )

            params_and_scores.append(
                {
                    "ordered_param_instances": progressive_params_high,
                    "has_run": False,
                    "varying_param": associated_param["name"],
                    "stats": {},
                    "score": -5000,
                }
            )
            # print("Appended +2 for idx {}".format(i))

        # So what is it then.
        # End generation loop. Store the current set of climb params.
        self.epochs_to_params[self.epoch] = {
            "params_and_scores": params_and_scores,
            "best_score_stats": {},
            "best_score": -5000,
            "best_param": [],
        }

        # 2 - return to a consumer
        return self.epochs_to_params[self.epoch]

    def get_results_evaluate(self, stats_for_epoch):
        # Check data correctness...
        self.cur_round_log = ""

        if len(stats_for_epoch) != len(
            self.epochs_to_params[self.epoch]["params_and_scores"]
        ):
            raise Exception(
                "Error: the stats_for_epoch did not match my saved stats_for_epoch"
            )

        for i in range(len(stats_for_epoch)):
            # Each of these is a combo of param settings that make up one full setting.
            # It's a list of dicts.
            cur_stat_setting_external = stats_for_epoch[i]["ordered_param_instances"]
            cur_stat_setting_internal = self.epochs_to_params[self.epoch][
                "params_and_scores"
            ][i]["ordered_param_instances"]

            # Go through their params, check that they're the same.
            if len(cur_stat_setting_external) != len(cur_stat_setting_internal):
                print(
                    "Error: the length of the param setting for instance {} of cur_stat_setting(in, ex)ternal do not match".format(
                        i
                    )
                )

            for j in range(len(cur_stat_setting_external)):
                one_param_ex = cur_stat_setting_external[j]
                one_param_in = cur_stat_setting_internal[j]

                for dict_key in one_param_ex:
                    if one_param_ex[dict_key] != one_param_in[dict_key]:
                        raise Exception(
                            "Error: internal/external mismatch for {}: '{}' vs '{}'".format(
                                dict_key,
                                cur_stat_setting_external[j][dict_key],
                                cur_stat_setting_internal[j][dict_key],
                            )
                        )

        # END CORRECTNESS CHECKING
        # Using the stats_per_epoch, generate a score for each, do the fit.

        cur_epoch_stats = []
        self.epochs_to_params[self.epoch]["params_and_scores"] = stats_for_epoch

        # This gets turned into the array for minimizing error

        progressive_values_for_fit = []
        scores_vector = []

        any_positive_scores = False

        for one_param_stat_combo in stats_for_epoch:
            score = one_param_stat_combo["score"]
            one_param_stat_combo["has_run"] = True
            cur_epoch_stats.append(one_param_stat_combo)

            if score > 0:
                any_positive_scores = True

            # First value is 1, for including an intercept/offset.
            values_for_fit_row = [1]

            counter = 0
            for _ in self.climbed_param_names:
                param_dict = one_param_stat_combo["ordered_param_instances"][counter]
                counter += 1
                if param_dict["finished"]:
                    continue

                # Add the preimage values for params that get fit.
                values_for_fit_row.append(param_dict["current_value"])
                values_for_fit_row.append(param_dict["current_value"] ** 2)

            progressive_values_for_fit.append(values_for_fit_row)
            scores_vector.append(score)

        force_expand = False
        if self.expand_all_negative and (not any_positive_scores):
            force_expand = True

        # Branch point
        # It's possible we got worse, and we should identify snapback locations
        should_snapback = False

        best_stats_round = {}
        best_score_round = -5000
        best_params_round = []

        # Iterate through params-to-scores and compare
        for one_param_stat_combo in cur_epoch_stats:
            cur_score = one_param_stat_combo["score"]
            if cur_score > best_score_round:
                best_stats_round = one_param_stat_combo["stats"]
                best_score_round = cur_score

                best_params_round = one_param_stat_combo["ordered_param_instances"]

        new_center = []

        # Check if should snapback.
        snapback_bar = 0
        if self.epoch > 0:
            snapback_bar = self.epochs_to_params[self.epoch - 1]["best_score"]
            if self.prefer_best:
                snapback_bar = self.best_score_so_far

        should_snapback = (
            self.can_snapback
            and self.epoch > 0
            # snapback is not working if the values are negative.
            and (
                (0 < best_score_round < self.snapback_frac * snapback_bar)
                or (best_score_round < 0 < snapback_bar)
                or (best_score_round < snapback_bar / self.snapback_frac < 0)
            )
        )


        round_est_strs = ""

        cur_index = 0
        # Print the param progression.
        for cur_param_index, cur_param_name in enumerate(self.climbed_param_names):
            if stats_for_epoch[0]["ordered_param_instances"][cur_param_index][
                "finished"
            ]:
                continue

            if self.pt_objs[cur_param_name].chill_n_steps > self.epoch:
                continue

            self.cur_round_log += "{} (stepsize {:.4f}) params [{:.6f} {:.6f} {:.6f}]  -> scores [{:.6f} {:.6f} {:.6f}]\n".format(
                cur_param_name,
                stats_for_epoch[0]["ordered_param_instances"][cur_param_index][
                    "stepsize"
                ],
                stats_for_epoch[cur_index * 2 + 1]["ordered_param_instances"][
                    cur_param_index
                ]["projection_value"],
                stats_for_epoch[0]["ordered_param_instances"][cur_param_index][
                    "projection_value"
                ],
                stats_for_epoch[cur_index * 2 + 2]["ordered_param_instances"][
                    cur_param_index
                ]["projection_value"],
                stats_for_epoch[cur_index * 2 + 1]["score"],
                stats_for_epoch[0]["score"],
                stats_for_epoch[cur_index * 2 + 2]["score"],
            )
            cur_index += 1

        # Before we reestimate parameters, we allow for hard decisions:
        # snapping back, expand_all, or reestimate.
        if should_snapback:
            # self.log_string += "\n{} vs {}: snapping back\n".format(
            #    best_score_round, self.epochs_to_params[self.epoch - 1]["best_score"]
            # )

            self.num_snapbacks += 1
            # Copy the previous round's stuff into the new center.
            for cur_param_index, cur_param_name in enumerate(self.climbed_param_names):
                inherited_param_next_round = copy.deepcopy(
                    self.epochs_to_params[self.epoch - 1]["best_param"][cur_param_index]
                )

                # If we snapback to abs best, still want to retain the step, shrink values of the
                # current epoch.
                if self.prefer_best:
                    inherited_param_next_round[
                        "projection_value"
                    ] = self.best_param_so_far[cur_param_index]["projection_value"]
                    inherited_param_next_round[
                        "current_value"
                    ] = self.best_param_so_far[cur_param_index]["current_value"]

                # Otherwise, snapback creates a slight shrink.
                # 1 - if no min, we can shrink
                # 2 - If If there is a min, but limit is smaller, we allow it
                # OK, creating the shrink instead.
                if True or False:
                    # Snapback-based shrinkage.
                    inherited_param_next_round["stepsize"] /= self.snapback_div_factors[self.num_snapbacks]
                    # inherited_param_next_round["num_shrinks"] += 1

                self.cur_round_log += "Snapback to params: {}\n".format(
                    inherited_param_next_round
                )
                new_center.append(inherited_param_next_round)

                param_epoch_actions = {
                    "center_progression": "snapback",
                    "best_value": 0,
                    "best_value_type": "previous",
                    "best_value_score": 0,
                    "step_change": "SHRINK",
                }
                self.param_stats[cur_param_name].updateStatsEpoch(
                    self.epoch, param_epoch_actions
                )
        elif force_expand:
            # Copy over to the best in the round. And force everything to
            # expand

            for cur_param_index, cur_param_name in enumerate(self.climbed_param_names):
                if self.epoch > 1 and self.param_stats[cur_param_name].getLastValues("center_progression") == "force_expand":
                    inherited_param_next_round = copy.deepcopy(
                        best_params_round[cur_param_index]
                    )
                else:
                    inherited_param_next_round = copy.deepcopy(
                        stats_for_epoch[0]["ordered_param_instances"][cur_param_index]
                    )

                # Expand but don't let it count against you.
                inherited_param_next_round["stepsize"] *= 2

                new_center.append(inherited_param_next_round)

                param_epoch_actions = {
                    "center_progression": "force_expand",
                    "best_value": 0,
                    "best_value_type": "previous",
                    "best_value_score": 0,
                    "step_change": "EXPAND",
                }
                self.param_stats[cur_param_name].updateStatsEpoch(
                    self.epoch, param_epoch_actions
                )

            self.cur_round_log += (
                "Triggered force_expand because all scores negative. Expanding radii\n"
            )

            pass

        # vs should_snapback...
        else:
            # Create the fitting matrix.
            params_matrix = np.array(progressive_values_for_fit)
            target_matrix = np.array(scores_vector)
            
            #self.cur_round_log += (
            #    "About to fit: params \n{} \n target {}\n".format(
            #        progressive_values_for_fit, target_matrix
            #    )
            #)

            try:
                fit_results = np.linalg.lstsq(params_matrix, target_matrix, rcond=-1)
                fit_coefs = fit_results[0]
            except Exception as e:
                print("Failed while trying to fit: ")
                print(self.climbed_param_names)
                print(params_matrix)
                print(target_matrix)
                self.finished = True
                return
            self.cur_round_log += ("After fit: coefs(incl. offset) {}\n".format(fit_coefs))

            # cur_index is an index into the fitting scheme. It is NOT cur_param_index.
            # This is because some params could have stopped climbing.
            # TODO: make this more accessible, yeah?

            cur_index = 0

            for cur_param_index, cur_param_name in enumerate(self.climbed_param_names):
                # If we halted search in this, don't do the new center estimate

                if stats_for_epoch[0]["ordered_param_instances"][cur_param_index][
                    "finished"
                ]:
                    # Then just skip it. Don't do any fitting.

                    inherited_param_next_round = copy.deepcopy(
                        stats_for_epoch[0]["ordered_param_instances"][cur_param_index]
                    )
                    new_center.append(inherited_param_next_round)
                    continue

                # Also, if we aren't climbing this guy...
                if self.pt_objs[cur_param_name].chill_n_steps > self.epoch:
                    inherited_param_next_round = copy.deepcopy(
                        stats_for_epoch[0]["ordered_param_instances"][cur_param_index]
                    )
                    new_center.append(inherited_param_next_round)
                    continue

                ##########################################################

                # Used by the history per param
                center_progression = ""
                best_value = 0
                best_value_type = ""
                best_value_score = 0
                #################################################

                # Gather the param scores from current epoch, do the
                # naive estimate. Note: we're not doing any interaction terms so it's pretty easy.
                # const_term = fit_coefs[0]

                lin_term = fit_coefs[1 + cur_index * 2]
                quad_term = fit_coefs[2 + cur_index * 2]

                # Optimal, assuming correct sign.
                # Removing epsilons.
                supposed_optimal_point = -lin_term / (2 * quad_term)
                # Let's display the actual values too.

                # TODO: check for correctness.
                center_value = stats_for_epoch[0]["ordered_param_instances"][
                    cur_param_index
                ]["current_value"]
                lower_value = stats_for_epoch[cur_index * 2 + 1][
                    "ordered_param_instances"
                ][cur_param_index]["current_value"]
                higher_value = stats_for_epoch[cur_index * 2 + 2][
                    "ordered_param_instances"
                ][cur_param_index]["current_value"]

                center_score = stats_for_epoch[0]["score"]
                lower_score = stats_for_epoch[cur_index * 2 + 1]["score"]
                higher_score = stats_for_epoch[cur_index * 2 + 2]["score"]

                round_est_strs += "Initial estimate for {}: opt {:.8f} ({:.8f}) prev vals [ {:.8f} {:.8f} {:.8f} ], quad_terms ({:.6f} {:.6f})\n".format(
                    cur_param_name,
                    supposed_optimal_point,
                    self.pt_objs[cur_param_name].proj_from_pre(supposed_optimal_point),
                    lower_value,
                    center_value,
                    higher_value,
                    quad_term,
                    lin_term,
                )

                special_print = cur_param_name == "BLANK"
                if special_print:
                    self.cur_round_log += "b_scale: opt {:.6f} low center high  {:.6f}  {:.6f}  {:.6f}\n".format(
                        supposed_optimal_point,
                        lower_value,
                        center_value,
                        higher_value,
                    )

                # Find the best of the three points.
                # Center
                if (
                    stats_for_epoch[0]["score"]
                    >= stats_for_epoch[cur_index * 2 + 1]["score"]
                    and stats_for_epoch[0]["score"]
                    >= stats_for_epoch[cur_index * 2 + 2]["score"]
                ):
                    best_value = center_value
                    best_value_type = "center"
                    best_value_score = stats_for_epoch[0]["score"]
                elif (
                    stats_for_epoch[cur_index * 2 + 1]["score"]
                    >= stats_for_epoch[0]["score"]
                    and stats_for_epoch[cur_index * 2 + 1]["score"]
                    >= stats_for_epoch[cur_index * 2 + 2]["score"]
                ):
                    # low
                    best_value = lower_value
                    best_value_type = "lower"
                    best_value_score = stats_for_epoch[cur_index * 2 + 2]["score"]
                else:
                    best_value = higher_value
                    best_value_type = "higher"
                    best_value_score = stats_for_epoch[cur_index * 2 + 2]["score"]

                # Now. Figure out which direction it went.
                # If went beyond span, bound and perhaps expand.
                maybe_should_expand = False
                maybe_should_shrink = False
                step_change = "SAME"

                new_climb_center_dict = copy.deepcopy(
                    stats_for_epoch[0]["ordered_param_instances"][cur_param_index]
                )

                # Firstly, figure out if quadratic is pointing up or down.
                # It's pointed up.
                if quad_term > 0:
                    # If monotonic going left, then we move a little to the left.
                    if lower_score > center_score > higher_score:
                        supposed_optimal_point = (
                            lower_value - new_climb_center_dict["stepsize"] * 0.75
                        )
                    elif higher_score > center_score > lower_score:
                        # DO the opposite and increase.
                        supposed_optimal_point = (
                            higher_value + new_climb_center_dict["stepsize"] * 0.75
                        )
                    else:
                        # Center is one of the two. sides, unless if this is messt
                        # up. Choose the higher of the two.
                        if higher_score > lower_score:
                            # With a bit of noise
                            supposed_optimal_point = (
                                higher_value + new_climb_center_dict["stepsize"] * 0.1
                            )
                        else:
                            supposed_optimal_point = (
                                lower_value - new_climb_center_dict["stepsize"] * 0.1
                            )

                else:
                    # The quadratic is going down.
                    # versus the quad_term > 0 comparison

                    # Let's use the center_value as a source of truth...
                    # TODO: check this for correctness.
                    if (
                        supposed_optimal_point
                        > higher_value + new_climb_center_dict["stepsize"] * 1.5
                    ):
                        supposed_optimal_point = (
                            higher_value + new_climb_center_dict["stepsize"] * 0.75
                        )
                        maybe_should_expand = True
                    elif (
                        supposed_optimal_point
                        < lower_value - new_climb_center_dict["stepsize"] * 1.5
                    ):
                        supposed_optimal_point = (
                            lower_value - new_climb_center_dict["stepsize"] * 0.75
                        )
                        maybe_should_expand = True

                    else:
                        if center_value > supposed_optimal_point > lower_value:
                            maybe_should_shrink = True
                        elif center_value < supposed_optimal_point < higher_value:
                            maybe_should_shrink = True

                # Outside of the "else" comparing quad_term.
                pt_obj = self.pt_objs[new_climb_center_dict["name"]]

                # Decide Increase or decrease
                if supposed_optimal_point > higher_value:
                    center_progression = "INC"
                elif supposed_optimal_point < lower_value:
                    center_progression = "DEC"
                else:
                    center_progression = "MID"
                    if (self.shrink_aggressively):
                        maybe_should_shrink = True


                # Check if we should stop. Just want to calculate pct change, but one of thsee could>
                max_score_from_param = max(center_score, lower_score, higher_score)
                min_score_from_param = min(center_score, lower_score, higher_score)

                param_didnt_change_enough = False
                if max_score_from_param == 0 and min_score_from_param == 0:
                    param_didnt_change_enough = True
                else:
                    # Middle not used...
                    pct_change = (max_score_from_param - min_score_from_param) / (
                        0.5 * (abs(max_score_from_param) + abs(min_score_from_param))
                    )

                    if abs(pct_change) < self.required_delta_params:
                        param_didnt_change_enough = True

                # Check if we would increase or decrease.
                # First, check if we went many times in the same direction.
                if param_didnt_change_enough:
                    if new_climb_center_dict["num_shrinks"] == 0 and self.epoch < 2:
                        step_change = "EXPAND"
                    elif self.epoch > 4:
                        # We should probably end this param. It's not contributing.
                        new_climb_center_dict["finished"] = True
                        print("Stopping {} because of low pct".format(cur_param_name))

                else:
                    # Check if we should shrink.
                    sorted_step_changes = self.param_stats[
                        cur_param_name
                    ].getLastValues("step_change")
                    sorted_center_progressions = self.param_stats[
                        cur_param_name
                    ].getLastValues("center_progression")

                    LOOK_BACK_LEN = 2
                    if (
                        "SHRINK" in sorted_step_changes
                        or "EXPAND" in sorted_step_changes
                    ):
                        LOOK_BACK_LEN = 3

                    can_alter_steps = True
                    if len(sorted_step_changes) < LOOK_BACK_LEN:
                        can_alter_steps = False

                    for prev_step_change in sorted_step_changes[-LOOK_BACK_LEN:]:
                        if prev_step_change != "SAME":
                            can_alter_steps = False

                    if can_alter_steps:
                        # Check if we are hitting the limits.
                        center_move_count = 0
                        mid_count = 0
                        for center_move in sorted_center_progressions[-LOOK_BACK_LEN:]:
                            if center_move == "INC":
                                center_move_count += 1
                            elif center_move == "DEC":
                                center_move_count -= 1
                            elif center_move == "MID":
                                mid_count += 1
                        if abs(center_move_count) == LOOK_BACK_LEN:
                            can_alter_steps = True
                            if LOOK_BACK_LEN > 2:
                                maybe_should_expand = True
                        elif mid_count == LOOK_BACK_LEN:
                            can_alter_steps = True
                            maybe_should_shrink = True

                        if maybe_should_expand:
                            step_change = "EXPAND"
                        if maybe_should_shrink:
                            step_change = "SHRINK"

                        self.cur_round_log += "{} (stepping): last_step_changes {} center_move_count {} mid_count {} conc {} \n".format(
                            cur_param_name,
                            sorted_step_changes[-LOOK_BACK_LEN:],
                            center_move_count,
                            mid_count,
                            step_change,
                        )

                # Save stats from this climb.
                this_round_param_stats = {}
                this_round_param_stats["center_progression"] = center_progression
                this_round_param_stats["best_value"] = best_value
                this_round_param_stats["best_value_type"] = best_value_type
                this_round_param_stats["best_value_score"] = best_value_score
                this_round_param_stats["step_change"] = step_change

                self.param_stats[cur_param_name].updateStatsEpoch(
                    self.epoch, this_round_param_stats
                )

                if (
                    step_change == "EXPAND"
                    and new_climb_center_dict["num_expands"] < pt_obj.max_expand_steps
                ):
                    self.cur_round_log += "\nShould_expand on {} ({} vs {}), custom stepsize:{} -> {}\n".format(
                        new_climb_center_dict["name"],
                        new_climb_center_dict["num_expands"],
                        pt_obj.max_expand_steps,
                        new_climb_center_dict["stepsize"],
                        new_climb_center_dict["stepsize"] * 2,
                    )

                    new_climb_center_dict["stepsize"] *= 2
                    new_climb_center_dict["num_expands"] += 1
                elif (
                    step_change == "SHRINK"
                    and new_climb_center_dict["num_shrinks"] < pt_obj.max_shrink_steps
                ):
                    # Slightly off from 2x so we don't just walk the same values.
                    self.cur_round_log += (
                        "\nShould_shrink on {}, shrink stepsize:{} -> {}\n".format(
                            new_climb_center_dict["name"],
                            new_climb_center_dict["stepsize"],
                            new_climb_center_dict["stepsize"] / 2,
                        )
                    )

                    new_climb_center_dict["stepsize"] /= 2.0
                    new_climb_center_dict["num_shrinks"] += 1

                new_climb_center_dict["current_value"] = supposed_optimal_point
                projection_value = pt_obj.proj_from_pre(supposed_optimal_point)
                new_climb_center_dict["projection_value"] = projection_value

                # self.cur_round_log += "{}: supposed_opt {} -> {} ({})\n".format(
                #    cur_param_name,
                #    center_value,
                #    supposed_optimal_point,
                #    projection_value,
                # )

                if new_climb_center_dict["num_shrinks"] > pt_obj.max_shrink_steps:
                    new_climb_center_dict["finished"] = True

                # Add this guy to the center.
                new_center.append(new_climb_center_dict)
                cur_index += 1

        # Block level: the if should_snapback
        all_are_same = True
        scores_vary_enough = False
        for one_param_stat_combo in cur_epoch_stats:
            # Check that aren't all same score
            if one_param_stat_combo["score"] != best_score_round:
                all_are_same = False

            if (
                one_param_stat_combo["score"] != 0
                and abs(
                    (best_score_round - one_param_stat_combo["score"])
                    / (self.epsilon + one_param_stat_combo["score"])
                )
                > self.stop_conditions["pct_change_variants"]
            ):
                scores_vary_enough = True

        # TODO: undo this at some point.
        # But for now, just going to undo this scores_vary_enough change.
        scores_vary_enough = True

        self.epochs_to_params[self.epoch]["best_score"] = best_score_round
        self.epochs_to_params[self.epoch]["best_score_stats"] = best_stats_round
        self.epochs_to_params[self.epoch]["best_param"] = best_params_round

        # After iteration, append to log...
        params_and_scores = self.pretty_print_epochs_to_params(
            self.epoch, print_projected_vals=True, do_print=False
        )

        if should_snapback:
            # Put snapback string in the right place.
            self.cur_round_log += "\n{} vs {}: snapping back\n".format(
                best_score_round, snapback_bar
            )

        self.log_string += "\n" + "*" * 100 + params_and_scores
        self.log_string += "\n\n" + self.cur_round_log
        self.log_string += "\n" + round_est_strs

        ######################################################
        # Check finish criteria

        # Don't iterate more than this.
        if self.epoch >= self.max_epochs:
            self.log_string += "Finished due to max_epochs\n"
            self.finished = True

        if all_are_same:
            self.log_string += "Finished due to all_are_same\n"
            self.finished = True

        all_finished = True
        for one_param_dict in new_center:
            if not one_param_dict["finished"]:
                all_finished = False

        if all_finished:
            self.log_string += "Finished due to all_finished\n"
            self.finished = True

        # if self.epoch + 1 > self.min_epochs and self.epoch > 0:
        # prev_center_score = self.epochs_to_params[self.epoch - 1][
        #    "params_and_scores"
        # ][0]["score"]

        # TODO: check this
        # if (not should_snapback and
        #    (best_score_round - prev_center_score) / (max(abs(prev_center_score), abs(best_score_round)) + self.epsilon) < self.stop_conditions["center_pct_increase"]):
        #    self.log_string += "Finished due to center_pct_increase\n"
        #    self.finished = True

        # End finish criteria

        if best_score_round > self.best_score_so_far:
            self.best_stats_so_far = best_stats_round
            self.best_score_so_far = best_score_round
            self.best_param_so_far = best_params_round

        self.epoch += 1
        self.cur_center = new_center

        self.get_params_next_epoch()
