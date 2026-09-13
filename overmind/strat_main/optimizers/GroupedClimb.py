from optimizers.VaryingParam import VaryingParam
from optimizers.BaseClimb import BaseClimber

import copy
import numpy as np
import random

"""
Grouped Coordinate Descent (Staged Optimization) climber.

Optimizes params in stages: high-impact params first, then secondary
ones, with an optional final joint refinement pass. Each stage uses
a lightweight separable CMA-ES as the inner optimizer.

Stages are defined via "climb_groups" in the config. Each stage
specifies which params to optimize and how many evals to spend.
Fixed params use their current best values.

Example config (pnlclimb_specs):

    "num_iters": 40,
    "climb_type": "GROUPED",
    "qc_specs": {
        "min_epochs": 1,
        "max_epochs": 40,
        "n_particles": 6,
        "climb_groups": [
            {"stage": 1, "params": ["place_thresh", "cancel_buffer_frac",
                                    "vol_norm_coef"], "max_evals": 42},
            {"stage": 2, "params": ["pcurvicity", "pcurvicity_coef"],
             "max_evals": 30},
            {"stage": 3, "params": ["narrow_thresh_time_const_s",
                                    "narrow_fewtrade_minmultiple",
                                    "narrow_fewtrade_denom"], "max_evals": 30},
            {"stage": 4, "params": "ALL", "max_evals": 48}
        ],
        "params": []
    }

Each stage advances when max_evals is reached or the inner CMA-ES
converges. params: "ALL" means optimize every param jointly.
Grouping is ordex-specific — encode domain knowledge here.
"""

RND_SEED = 271828
random.seed(RND_SEED)
np.random.seed(RND_SEED)


def comma_separated_params(param_names, names_to_vals):
    to_ret = ""
    for pn in param_names:
        to_ret += "{:.6f},".format(names_to_vals[pn])
    if len(to_ret) > 0:
        to_ret = to_ret[:-1]
    return to_ret


class SepCmaEs:
    """Separable CMA-ES (diagonal covariance only) for inner optimization."""

    def __init__(self, n_dim, popsize, mean, sigma, diag_var):
        self.n_dim = n_dim
        self.popsize = popsize
        self.mu = popsize // 2
        self.mean = mean.copy()
        self.sigma = sigma

        # Diagonal variances (per-param)
        self.diag_var = diag_var.copy()

        # Recombination weights
        raw_w = np.array([np.log(self.mu + 0.5) - np.log(i + 1) for i in range(self.mu)])
        self.weights = raw_w / raw_w.sum()
        self.mu_eff = 1.0 / np.sum(self.weights ** 2)

        # Adaptation rates
        self.c_sigma = (self.mu_eff + 2.0) / (n_dim + self.mu_eff + 5.0)
        self.d_sigma = 1.0 + 2.0 * max(0, np.sqrt((self.mu_eff - 1.0) / (n_dim + 1.0)) - 1.0) + self.c_sigma
        self.c_c = (4.0 + self.mu_eff / n_dim) / (n_dim + 4.0 + 2.0 * self.mu_eff / n_dim) if n_dim > 0 else 0.5
        self.c_1 = 2.0 / ((n_dim + 1.3) ** 2 + self.mu_eff) if n_dim > 0 else 0.2
        self.c_mu_val = min(1.0 - self.c_1, 2.0 * (self.mu_eff - 2.0 + 1.0 / self.mu_eff) / ((n_dim + 2.0) ** 2 + self.mu_eff)) if n_dim > 0 else 0.2

        self.p_sigma = np.zeros(n_dim)
        self.p_c = np.zeros(n_dim)
        self.chi_n = np.sqrt(n_dim) * (1.0 - 1.0 / (4.0 * max(n_dim, 1)) + 1.0 / (21.0 * max(n_dim, 1) ** 2))

        self.epoch = 0
        self.candidates = None

    def sample(self):
        """Sample popsize candidates."""
        self.candidates = []
        std = np.sqrt(self.diag_var)
        for _ in range(self.popsize):
            z = np.random.randn(self.n_dim)
            x = self.mean + self.sigma * std * z
            self.candidates.append(x)
        return self.candidates

    def update(self, scores):
        """Update mean, sigma, diagonal variances from scores."""
        ranking = np.argsort(-np.array(scores))
        selected = ranking[:self.mu]

        old_mean = self.mean.copy()

        # Update mean
        self.mean = np.zeros(self.n_dim)
        for k, idx in enumerate(selected):
            self.mean += self.weights[k] * self.candidates[idx]

        mean_diff = (self.mean - old_mean) / self.sigma
        std = np.sqrt(np.maximum(self.diag_var, 1e-20))
        inv_std = 1.0 / std

        # Update p_sigma
        self.p_sigma = (1.0 - self.c_sigma) * self.p_sigma + \
            np.sqrt(self.c_sigma * (2.0 - self.c_sigma) * self.mu_eff) * inv_std * mean_diff

        # h_sigma
        h_thresh = (1.4 + 2.0 / (self.n_dim + 1.0)) * self.chi_n * np.sqrt(1.0 - (1.0 - self.c_sigma) ** (2.0 * (self.epoch + 1)))
        h_sigma = 1.0 if np.linalg.norm(self.p_sigma) < h_thresh else 0.0

        # Update p_c
        self.p_c = (1.0 - self.c_c) * self.p_c + \
            h_sigma * np.sqrt(self.c_c * (2.0 - self.c_c) * self.mu_eff) * mean_diff

        # Update diagonal variances
        delta_h = (1.0 - h_sigma) * self.c_c * (2.0 - self.c_c)
        rank_mu_diag = np.zeros(self.n_dim)
        for k, idx in enumerate(selected):
            diff = (self.candidates[idx] - old_mean) / self.sigma
            rank_mu_diag += self.weights[k] * diff ** 2

        self.diag_var = (1.0 - self.c_1 - self.c_mu_val + delta_h * self.c_1) * self.diag_var + \
            self.c_1 * self.p_c ** 2 + self.c_mu_val * rank_mu_diag

        # Update sigma
        self.sigma *= np.exp((self.c_sigma / self.d_sigma) * (np.linalg.norm(self.p_sigma) / self.chi_n - 1.0))

        self.epoch += 1

    def converged(self):
        return self.sigma * np.sqrt(np.max(self.diag_var)) < 1e-6


class GroupedClimber:
    def __init__(self):
        self.epoch = 0
        self.min_epochs = 1
        self.max_epochs = 200
        self.n_particles = 8

        self.best_score_so_far = -1e3
        self.best_pre_values = None
        self.best_projected_values = None

        self.param_names = []  # ALL param names
        self.pt_objs = {}
        self.n_dim = 0

        self.log_string = ""
        self.finished = False

        # Staged optimization state
        self.stages = []  # list of stage dicts
        self.current_stage_idx = 0
        self.stage_evals = 0  # evals used in current stage

        # Current best pre-values for ALL params (used for fixed params)
        self.current_best_pre = {}

        # Inner optimizer for current stage
        self.inner_optimizer = None
        self.inner_active_names = []  # param names being optimized this stage

        self._current_candidates_pre = None  # full pre-values for all params

    def init_from_json(self, spec_dict):
        self.log_string += "Initializing GroupedClimber\n"

        self.epoch = 0
        self.min_epochs = spec_dict.get("min_epochs", 1)
        self.max_epochs = spec_dict["max_epochs"]
        self.n_particles = spec_dict["n_particles"]

        self.param_names = []
        self.pt_objs = {}
        for one_param_json in spec_dict["params"]:
            vp = VaryingParam()
            vp.from_json(one_param_json)
            self.param_names.append(vp.name)
            self.pt_objs[vp.name] = vp

        self.n_dim = len(self.param_names)

        # Initialize current best from starting values
        for name in self.param_names:
            self.current_best_pre[name] = self.pt_objs[name].pre_val

        # Parse stages
        climb_groups = spec_dict.get("climb_groups", None)
        if climb_groups is None:
            # Default: single stage with all params
            self.stages = [{"stage": 1, "params": "ALL", "max_evals": self.max_epochs * self.n_particles}]
        else:
            self.stages = sorted(climb_groups, key=lambda s: s["stage"])

        self._init_stage(0)

        self.log_string += "n_dim={} popsize={} stages={}\n".format(
            self.n_dim, self.n_particles, len(self.stages)
        )
        for s in self.stages:
            self.log_string += "  Stage {}: params={} max_evals={}\n".format(
                s["stage"], s["params"], s.get("max_evals", "inf")
            )

    def _init_stage(self, stage_idx):
        """Initialize the inner optimizer for a given stage."""
        if stage_idx >= len(self.stages):
            self.finished = True
            return

        self.current_stage_idx = stage_idx
        self.stage_evals = 0
        stage = self.stages[stage_idx]

        # Determine active params
        if stage["params"] == "ALL":
            self.inner_active_names = list(self.param_names)
        else:
            self.inner_active_names = [p for p in stage["params"] if p in self.param_names]

        if len(self.inner_active_names) == 0:
            self.log_string += "Stage {} has no valid params, skipping.\n".format(stage["stage"])
            self._init_stage(stage_idx + 1)
            return

        n_active = len(self.inner_active_names)

        # Initialize separable CMA-ES for active params
        mean = np.array([self.current_best_pre[name] for name in self.inner_active_names])
        stepsizes = np.array([self.pt_objs[name].stepsize for name in self.inner_active_names])
        sigma = float(np.mean(stepsizes * 2))
        diag_var = (stepsizes / np.mean(stepsizes)) ** 2

        self.inner_optimizer = SepCmaEs(n_active, self.n_particles, mean, sigma, diag_var)

        self.log_string += "\n{}\nStarting Stage {} — optimizing: [{}]\n".format(
            "=" * 80, stage["stage"], ", ".join(self.inner_active_names)
        )
        self.log_string += "Fixed params: {}\n".format(
            {name: self.pt_objs[name].proj_from_pre(self.current_best_pre[name])
             for name in self.param_names if name not in self.inner_active_names}
        )

    def get_params_next_epoch(self):
        # Sample from inner optimizer (only active params)
        active_candidates = self.inner_optimizer.sample()

        # Build full candidates (active + fixed)
        full_candidates = []
        for active_vals in active_candidates:
            full_pre = {}
            active_idx = 0
            for name in self.param_names:
                if name in self.inner_active_names:
                    idx = self.inner_active_names.index(name)
                    full_pre[name] = active_vals[idx]
                else:
                    full_pre[name] = self.current_best_pre[name]
            full_candidates.append(full_pre)

        self._current_candidates_pre = full_candidates

        to_ret = {"params_and_scores": []}
        for cand in full_candidates:
            param_dct = {
                "ordered_param_instances": [],
                "has_run": False,
                "varying_param": "",
                "stats": {},
                "score": -5000,
            }
            for name in self.param_names:
                proj_val = self.pt_objs[name].proj_from_pre(cand[name])
                param_dct["ordered_param_instances"].append(
                    {"name": name, "projection_value": proj_val}
                )
            to_ret["params_and_scores"].append(param_dct)

        return to_ret

    def get_results_evaluate(self, stats_for_epoch):
        stage = self.stages[self.current_stage_idx]

        cur_log = "*" * 100
        cur_log += "Stage {} (params: [{}]) - Epoch {}: updating scores\n".format(
            stage["stage"], ", ".join(self.inner_active_names), self.epoch
        )
        cur_log += ",".join(self.param_names)

        is_pnlscores = (
            "stats" in stats_for_epoch[0]
            and len(stats_for_epoch[0]["stats"]) > 0
        )
        if is_pnlscores:
            cur_log += ",,net_pnl,closed_pnl,sharpe,num_trds,pct_positive,score"
        cur_log += "\n"

        scores = []
        for i, instance_stat in enumerate(stats_for_epoch):
            score = instance_stat["score"]
            scores.append(score)

            cand = self._current_candidates_pre[i]
            proj_vals = {name: self.pt_objs[name].proj_from_pre(cand[name]) for name in self.param_names}
            proj_str = comma_separated_params(self.param_names, proj_vals)

            if is_pnlscores:
                score_line = "{:.2f},{:.2f},{:.2f},{:.1f},{:.2f},{:.4f}".format(
                    instance_stat["stats"]["avg_pnl"],
                    instance_stat["stats"]["avg_closed_pnl"],
                    instance_stat["stats"]["sharpe"],
                    instance_stat["stats"]["avg_numtrds"],
                    instance_stat["stats"]["pct_positive"],
                    score,
                )
            else:
                score_line = "{:.4f}".format(score)

            cur_log += "{} --> {}\n".format(proj_str, score_line)

            if score > self.best_score_so_far:
                self.best_score_so_far = score
                self.best_pre_values = dict(cand)
                self.best_projected_values = dict(proj_vals)
                # Update current best pre-values
                self.current_best_pre.update(cand)

        # Feed scores to inner optimizer
        self.inner_optimizer.update(scores)
        self.stage_evals += len(scores)

        cur_log += "Best score: {} | stage_evals: {}/{} | sigma: {:.6f}\n".format(
            self.best_score_so_far, self.stage_evals,
            stage.get("max_evals", "inf"), self.inner_optimizer.sigma
        )
        if self.best_projected_values:
            cur_log += "Best params: {}\n".format(
                comma_separated_params(self.param_names, self.best_projected_values)
            )
        cur_log += "\n"

        self.epoch += 1

        # Check if stage is done
        max_evals = stage.get("max_evals", float("inf"))
        stage_done = self.stage_evals >= max_evals or self.inner_optimizer.converged()

        if stage_done:
            cur_log += "Stage {} complete after {} evals.\n".format(
                stage["stage"], self.stage_evals
            )
            # Update current_best_pre from the inner optimizer's mean (projected back)
            for idx, name in enumerate(self.inner_active_names):
                self.current_best_pre[name] = self.inner_optimizer.mean[idx]

            self._init_stage(self.current_stage_idx + 1)

        if self.epoch >= self.max_epochs:
            self.finished = True

        self.log_string += cur_log

    def get_best_climbed_params(self):
        if self.best_projected_values is not None:
            return dict(self.best_projected_values)
        return {name: self.pt_objs[name].proj_from_pre(self.pt_objs[name].pre_val)
                for name in self.param_names}

    def get_best_score(self):
        return self.best_score_so_far
