from optimizers.VaryingParam import VaryingParam
from optimizers.BaseClimb import BaseClimber

import copy
import numpy as np
import random

"""
CMA-ES (Covariance Matrix Adaptation Evolution Strategy) climber.

Maintains a multivariate Gaussian over param space. Each epoch, samples
lambda candidates, evaluates them, then updates the mean and covariance
based on the best mu candidates. The covariance learns correlations
between params automatically.

Reference: Hansen, "The CMA Evolution Strategy: A Tutorial" (2016).

Example config (pnlclimb_specs):

    "num_iters": 14,
    "climb_type": "CMAES",
    "qc_specs": {
        "min_epochs": 1,
        "max_epochs": 14,
        "n_particles": 10,
        "initial_sigma": 0.3,
        "params": []
    }

n_particles rule of thumb: 4 + floor(3 * ln(n_dim)). For 7 params, ~10.
initial_sigma is optional (defaults to mean(stepsizes) * 2).
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


class CmaEsClimber:
    def __init__(self):
        self.epoch = 0
        self.min_epochs = 1
        self.max_epochs = 200

        self.n_particles = 10  # lambda (population size)
        self.best_score_so_far = -1e3
        self.best_pre_values = None
        self.best_projected_values = None

        self.param_names = []
        self.pt_objs = {}

        self.log_string = ""
        self.finished = False

        # CMA-ES state
        self.mean = None        # mean vector (n,)
        self.sigma = None       # overall step-size
        self.C = None           # covariance matrix (n, n)
        self.p_sigma = None     # evolution path for sigma
        self.p_c = None         # evolution path for C
        self.n_dim = 0
        self.mu = 0             # number of parents
        self.weights = None     # recombination weights
        self.mu_eff = 0         # variance-effective selection mass

        # Strategy parameters (derived from n_dim in init_from_json)
        self.c_sigma = 0
        self.d_sigma = 0
        self.c_c = 0
        self.c_1 = 0
        self.c_mu = 0

        # Store current epoch's candidates in pre-image space
        self._current_candidates = None

    def init_from_json(self, spec_dict):
        self.log_string += "Initializing CmaEsClimber\n"

        self.epoch = 0
        self.min_epochs = spec_dict.get("min_epochs", 1)
        self.max_epochs = spec_dict["max_epochs"]
        self.n_particles = spec_dict["n_particles"]

        # Initialize VaryingParam objects
        self.param_names = []
        self.pt_objs = {}
        for one_param_json in spec_dict["params"]:
            vp = VaryingParam()
            vp.from_json(one_param_json)
            self.param_names.append(vp.name)
            self.pt_objs[vp.name] = vp

        self.n_dim = len(self.param_names)
        lam = self.n_particles
        self.mu = lam // 2

        # Recombination weights (log-linear)
        raw_weights = np.array([np.log(self.mu + 0.5) - np.log(i + 1) for i in range(self.mu)])
        self.weights = raw_weights / raw_weights.sum()
        self.mu_eff = 1.0 / np.sum(self.weights ** 2)

        # Strategy parameters per Hansen's tutorial
        n = self.n_dim
        self.c_sigma = (self.mu_eff + 2.0) / (n + self.mu_eff + 5.0)
        self.d_sigma = 1.0 + 2.0 * max(0, np.sqrt((self.mu_eff - 1.0) / (n + 1.0)) - 1.0) + self.c_sigma
        self.c_c = (4.0 + self.mu_eff / n) / (n + 4.0 + 2.0 * self.mu_eff / n)
        self.c_1 = 2.0 / ((n + 1.3) ** 2 + self.mu_eff)
        self.c_mu = min(1.0 - self.c_1, 2.0 * (self.mu_eff - 2.0 + 1.0 / self.mu_eff) / ((n + 2.0) ** 2 + self.mu_eff))

        # Initialize mean from starting pre-values
        self.mean = np.array([self.pt_objs[name].pre_val for name in self.param_names])

        # Initial sigma
        stepsizes = np.array([self.pt_objs[name].stepsize for name in self.param_names])
        self.sigma = spec_dict.get("initial_sigma", float(np.mean(stepsizes * 2)))

        # Initial covariance: diagonal, scaled by relative stepsizes
        diag = (stepsizes / np.mean(stepsizes)) ** 2
        self.C = np.diag(diag)

        # Evolution paths
        self.p_sigma = np.zeros(n)
        self.p_c = np.zeros(n)

        # Expected length of N(0, I) vector
        self._chi_n = np.sqrt(n) * (1.0 - 1.0 / (4.0 * n) + 1.0 / (21.0 * n ** 2))

        self.log_string += "n_dim={} lambda={} mu={} sigma={:.4f}\n".format(
            n, lam, self.mu, self.sigma
        )

    def get_params_next_epoch(self):
        # Eigendecompose C for sampling
        C = self.C
        # Ensure symmetry
        C = (C + C.T) / 2.0
        try:
            eigenvalues, B = np.linalg.eigh(C)
        except np.linalg.LinAlgError:
            eigenvalues = np.ones(self.n_dim)
            B = np.eye(self.n_dim)

        # Clamp eigenvalues to be positive
        eigenvalues = np.maximum(eigenvalues, 1e-20)
        D = np.sqrt(eigenvalues)

        # Sample lambda candidates: x_i = mean + sigma * B * D * z_i
        candidates = []
        for _ in range(self.n_particles):
            z = np.random.randn(self.n_dim)
            x = self.mean + self.sigma * B.dot(D * z)
            candidates.append(x)

        self._current_candidates = candidates

        # Build the return structure
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

        # Collect scores and update best
        scores = []
        for i, instance_stat in enumerate(stats_for_epoch):
            score = instance_stat["score"]
            scores.append(score)

            proj_vals = {}
            for j, name in enumerate(self.param_names):
                proj_vals[name] = self.pt_objs[name].proj_from_pre(self._current_candidates[i][j])
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
                self.best_pre_values = self._current_candidates[i].copy()
                self.best_projected_values = dict(proj_vals)

        cur_log += "Best score: {}\n".format(self.best_score_so_far)
        if self.best_projected_values:
            cur_log += "Best params: {}\n".format(
                comma_separated_params(self.param_names, self.best_projected_values)
            )

        # CMA-ES update: rank candidates by score (descending)
        scores = np.array(scores)
        ranking = np.argsort(-scores)  # best first
        selected = ranking[:self.mu]

        # Old mean for later
        old_mean = self.mean.copy()

        # Update mean
        self.mean = np.zeros(self.n_dim)
        for k, idx in enumerate(selected):
            self.mean += self.weights[k] * self._current_candidates[idx]

        # Mean displacement
        mean_diff = (self.mean - old_mean) / self.sigma

        # Eigendecompose C for inv sqrt
        C = (self.C + self.C.T) / 2.0
        try:
            eigenvalues, B = np.linalg.eigh(C)
        except np.linalg.LinAlgError:
            eigenvalues = np.ones(self.n_dim)
            B = np.eye(self.n_dim)
        eigenvalues = np.maximum(eigenvalues, 1e-20)

        # C^(-1/2) = B * diag(1/sqrt(eigenvalues)) * B^T
        inv_sqrt_D = 1.0 / np.sqrt(eigenvalues)
        C_inv_sqrt = B.dot(np.diag(inv_sqrt_D)).dot(B.T)

        # Update evolution path for sigma (p_sigma)
        self.p_sigma = (1.0 - self.c_sigma) * self.p_sigma + \
            np.sqrt(self.c_sigma * (2.0 - self.c_sigma) * self.mu_eff) * C_inv_sqrt.dot(mean_diff)

        # Heaviside function for h_sigma
        h_sigma_threshold = (1.4 + 2.0 / (self.n_dim + 1.0)) * self._chi_n * np.sqrt(1.0 - (1.0 - self.c_sigma) ** (2.0 * (self.epoch + 1)))
        h_sigma = 1.0 if np.linalg.norm(self.p_sigma) < h_sigma_threshold else 0.0

        # Update evolution path for C (p_c)
        self.p_c = (1.0 - self.c_c) * self.p_c + \
            h_sigma * np.sqrt(self.c_c * (2.0 - self.c_c) * self.mu_eff) * mean_diff

        # Rank-one update term
        rank_one = np.outer(self.p_c, self.p_c)

        # Rank-mu update term
        rank_mu = np.zeros((self.n_dim, self.n_dim))
        for k, idx in enumerate(selected):
            diff = (self._current_candidates[idx] - old_mean) / self.sigma
            rank_mu += self.weights[k] * np.outer(diff, diff)

        # Correction for h_sigma
        delta_h = (1.0 - h_sigma) * self.c_c * (2.0 - self.c_c)

        # Update covariance matrix
        self.C = (1.0 - self.c_1 - self.c_mu + delta_h * self.c_1) * self.C + \
            self.c_1 * rank_one + self.c_mu * rank_mu

        # Update sigma (step-size control via CSA)
        self.sigma *= np.exp((self.c_sigma / self.d_sigma) * (np.linalg.norm(self.p_sigma) / self._chi_n - 1.0))

        cur_log += "sigma: {:.6f}\n\n".format(self.sigma)

        self.epoch += 1

        # Check convergence
        try:
            eig_vals = np.linalg.eigvalsh((self.C + self.C.T) / 2.0)
            max_eig = np.max(np.abs(eig_vals))
        except np.linalg.LinAlgError:
            max_eig = 1.0

        if self.sigma * np.sqrt(max_eig) < 1e-8:
            cur_log += "Converged: sigma * sqrt(max_eigenvalue) < 1e-8\n"
            self.finished = True

        if self.epoch >= self.max_epochs:
            self.finished = True

        self.log_string += cur_log

    def get_best_climbed_params(self):
        if self.best_projected_values is not None:
            return dict(self.best_projected_values)
        # Fallback to initial
        return {name: self.pt_objs[name].proj_from_pre(self.pt_objs[name].pre_val)
                for name in self.param_names}

    def get_best_score(self):
        return self.best_score_so_far
