from optimizers.VaryingParam import VaryingParam
from optimizers.BaseClimb import BaseClimber

import copy
import numpy as np
import random

"""
TPE (Tree-structured Parzen Estimator) climber.

Models the distributions of "good" params and "bad" params separately
(split by a percentile threshold). Samples from where P(good)/P(bad)
is highest. More robust than GP-BO and handles parallelism well.

Example config (pnlclimb_specs):

    "num_iters": 14,
    "climb_type": "TPE",
    "qc_specs": {
        "min_epochs": 1,
        "max_epochs": 14,
        "n_particles": 8,
        "gamma": 0.25,
        "n_ei_candidates": 24,
        "prior_weight": 1.0,
        "bandwidth_mult": 1.0,
        "params": []
    }

gamma: fraction of trials in the "good" set (0.25 = top quartile).
prior_weight: weight of the uniform prior (higher = more exploration).
bandwidth_mult: multiplier on KDE bandwidth (higher = smoother).
First 2 epochs are random exploration before TPE kicks in.
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


class KDE1D:
    """Simple 1D Gaussian kernel density estimator with uniform prior."""

    def __init__(self, observations, lo, hi, prior_weight=1.0, bandwidth_mult=1.0):
        self.observations = np.array(observations)
        self.lo = lo
        self.hi = hi
        self.prior_weight = prior_weight
        self.n = len(observations)

        # Scott's rule bandwidth
        if self.n > 1:
            std = max(np.std(self.observations), (hi - lo) * 0.01)
            self.bandwidth = std * (self.n ** (-0.2)) * bandwidth_mult
        else:
            self.bandwidth = (hi - lo) * 0.3 * bandwidth_mult

    def pdf(self, x):
        """Evaluate density at points x."""
        x = np.atleast_1d(x)
        # Gaussian kernel contributions
        if self.n > 0:
            diffs = (x[:, None] - self.observations[None, :]) / self.bandwidth
            kernel_vals = np.exp(-0.5 * diffs ** 2) / (self.bandwidth * np.sqrt(2 * np.pi))
            kde = np.mean(kernel_vals, axis=1)
        else:
            kde = np.zeros_like(x)

        # Uniform prior
        uniform = 1.0 / (self.hi - self.lo)

        # Weighted combination
        total_weight = self.n + self.prior_weight
        density = (self.n * kde + self.prior_weight * uniform) / total_weight
        return density

    def sample(self, n_samples):
        """Sample from the mixture of kernels + uniform prior."""
        samples = []
        for _ in range(n_samples):
            if np.random.random() < self.prior_weight / (self.n + self.prior_weight):
                # Sample from uniform prior
                s = np.random.uniform(self.lo, self.hi)
            else:
                # Sample from a random kernel
                idx = np.random.randint(self.n)
                s = self.observations[idx] + self.bandwidth * np.random.randn()
            # Clip to bounds
            s = np.clip(s, self.lo, self.hi)
            samples.append(s)
        return np.array(samples)


class TpeClimber:
    def __init__(self):
        self.epoch = 0
        self.min_epochs = 1
        self.max_epochs = 200
        self.n_particles = 10

        self.best_score_so_far = -1e3
        self.best_pre_values = None
        self.best_projected_values = None

        self.param_names = []
        self.pt_objs = {}
        self.n_dim = 0

        self.log_string = ""
        self.finished = False

        # TPE state
        self.all_X = []  # list of pre-value arrays
        self.all_y = []  # list of scores
        self.gamma = 0.25  # quantile split
        self.n_ei_candidates = 24
        self.prior_weight = 1.0
        self.bandwidth_mult = 1.0
        self.bounds = None

        self._current_candidates = None

    def init_from_json(self, spec_dict):
        self.log_string += "Initializing TpeClimber\n"

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

        self.gamma = spec_dict.get("gamma", 0.25)
        self.n_ei_candidates = spec_dict.get("n_ei_candidates", 24)
        self.prior_weight = spec_dict.get("prior_weight", 1.0)
        self.bandwidth_mult = spec_dict.get("bandwidth_mult", 1.0)

        # Bounds: center +/- 3 * stepsize
        self.bounds = np.array([
            [self.pt_objs[name].pre_val - 3 * self.pt_objs[name].stepsize,
             self.pt_objs[name].pre_val + 3 * self.pt_objs[name].stepsize]
            for name in self.param_names
        ])

        self.log_string += "n_dim={} batch={} gamma={}\n".format(
            self.n_dim, self.n_particles, self.gamma
        )

    def _random_sample(self, n_samples):
        """Random samples within bounds."""
        samples = np.zeros((n_samples, self.n_dim))
        for d in range(self.n_dim):
            lo, hi = self.bounds[d]
            center = self.pt_objs[self.param_names[d]].pre_val
            step = self.pt_objs[self.param_names[d]].stepsize
            samples[:, d] = np.random.uniform(center - 2 * step, center + 2 * step, n_samples)
            samples[:, d] = np.clip(samples[:, d], lo, hi)
        return samples

    def _tpe_suggest(self):
        """Suggest a single candidate using TPE."""
        X = np.array(self.all_X)
        y = np.array(self.all_y)

        # Split into good and bad
        n_good = max(1, int(np.ceil(self.gamma * len(y))))
        sorted_indices = np.argsort(-y)  # best first
        good_indices = sorted_indices[:n_good]
        bad_indices = sorted_indices[n_good:]

        candidate = np.zeros(self.n_dim)
        for d in range(self.n_dim):
            lo, hi = self.bounds[d]
            good_vals = X[good_indices, d]
            bad_vals = X[bad_indices, d] if len(bad_indices) > 0 else X[:, d]

            l_kde = KDE1D(good_vals, lo, hi, self.prior_weight, self.bandwidth_mult)
            g_kde = KDE1D(bad_vals, lo, hi, self.prior_weight, self.bandwidth_mult)

            # Sample candidates from l(x) and pick the one with highest l(x)/g(x)
            samples = l_kde.sample(self.n_ei_candidates)
            l_vals = l_kde.pdf(samples)
            g_vals = g_kde.pdf(samples)
            g_vals = np.maximum(g_vals, 1e-20)
            ratios = l_vals / g_vals

            best_idx = np.argmax(ratios)
            candidate[d] = samples[best_idx]

        return candidate

    def get_params_next_epoch(self):
        if self.epoch < 2 or len(self.all_X) < 2 * self.n_particles:
            candidates = self._random_sample(self.n_particles)
        else:
            candidates = np.array([self._tpe_suggest() for _ in range(self.n_particles)])

        self._current_candidates = candidates

        to_ret = {"params_and_scores": []}
        for candidate in candidates:
            param_dct = {
                "ordered_param_instances": [],
                "has_run": False,
                "varying_param": "",
                "stats": {},
                "score": -5000,
            }
            for i, name in enumerate(self.param_names):
                proj_val = self.pt_objs[name].proj_from_pre(candidate[i])
                param_dct["ordered_param_instances"].append(
                    {"name": name, "projection_value": proj_val}
                )
            to_ret["params_and_scores"].append(param_dct)

        return to_ret

    def get_results_evaluate(self, stats_for_epoch):
        cur_log = "*" * 100
        cur_log += "Epoch {}: updating scores\n".format(self.epoch)
        cur_log += ",".join(self.param_names)

        is_pnlscores = (
            "stats" in stats_for_epoch[0]
            and len(stats_for_epoch[0]["stats"]) > 0
        )
        if is_pnlscores:
            cur_log += ",,net_pnl,closed_pnl,sharpe,num_trds,pct_positive,score"
        cur_log += "\n"

        for i, instance_stat in enumerate(stats_for_epoch):
            score = instance_stat["score"]
            x = self._current_candidates[i]

            self.all_X.append(x.copy())
            self.all_y.append(score)

            proj_vals = {}
            for j, name in enumerate(self.param_names):
                proj_vals[name] = self.pt_objs[name].proj_from_pre(x[j])
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
                self.best_pre_values = x.copy()
                self.best_projected_values = dict(proj_vals)

        cur_log += "Best score: {}\n".format(self.best_score_so_far)
        if self.best_projected_values:
            cur_log += "Best params: {}\n".format(
                comma_separated_params(self.param_names, self.best_projected_values)
            )

        n_good = max(1, int(np.ceil(self.gamma * len(self.all_y))))
        cur_log += "Observations: {} (good threshold: top {})\n\n".format(
            len(self.all_y), n_good
        )

        self.epoch += 1
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
