from optimizers.VaryingParam import VaryingParam
from optimizers.BaseClimb import BaseClimber

import copy
import numpy as np
import random
from scipy.optimize import minimize
from scipy.stats import norm

"""
Bayesian Optimization climber using a Gaussian Process surrogate.

Fits a GP to all (params, score) pairs observed so far, then uses an
acquisition function (Expected Improvement or UCB) to suggest the next
points to evaluate. Most sample-efficient optimizer but fundamentally
more sequential than population-based methods.

Uses Kriging Believer for batch suggestions.

Example config (pnlclimb_specs):

    "num_iters": 15,
    "climb_type": "BAYES",
    "qc_specs": {
        "min_epochs": 1,
        "max_epochs": 15,
        "n_particles": 4,
        "acq_type": "EI",
        "gp_noise": 1.0,
        "n_restarts": 10,
        "params": []
    }

acq_type: "EI" (exploit, safer default) or "UCB" (explore).
For UCB, add "ucb_kappa": 2.5 — higher = more exploration.
First 2 epochs use Latin Hypercube sampling to build the initial GP.
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


class GP:
    """Minimal Gaussian Process with RBF kernel."""

    def __init__(self, n_dim, noise=1.0):
        self.n_dim = n_dim
        self.log_lengthscales = np.zeros(n_dim)
        self.log_variance = 0.0
        self.log_noise = np.log(max(noise, 1e-6))
        self.X_train = None
        self.y_train = None
        self.K_inv = None
        self.alpha = None

    def _rbf_kernel(self, X1, X2, log_ls, log_var):
        ls = np.exp(log_ls)
        var = np.exp(log_var)
        # Scaled distances
        X1s = X1 / ls
        X2s = X2 / ls
        sq_dist = np.sum(X1s ** 2, axis=1, keepdims=True) + \
                  np.sum(X2s ** 2, axis=1) - 2.0 * X1s.dot(X2s.T)
        return var * np.exp(-0.5 * sq_dist)

    def fit(self, X, y):
        self.X_train = X.copy()
        # Normalize y for numerical stability
        self.y_mean = np.mean(y)
        self.y_std = max(np.std(y), 1e-6)
        self.y_train = (y - self.y_mean) / self.y_std

        # Optimize hyperparameters
        def neg_log_marginal_likelihood(theta):
            log_ls = theta[:self.n_dim]
            log_var = theta[self.n_dim]
            log_noise = theta[self.n_dim + 1]
            K = self._rbf_kernel(X, X, log_ls, log_var)
            K += np.exp(log_noise) * np.eye(len(X))
            K += 1e-6 * np.eye(len(X))
            try:
                L = np.linalg.cholesky(K)
                alpha = np.linalg.solve(L.T, np.linalg.solve(L, self.y_train))
                nlml = 0.5 * self.y_train.dot(alpha) + np.sum(np.log(np.diag(L))) + 0.5 * len(X) * np.log(2 * np.pi)
                return nlml
            except np.linalg.LinAlgError:
                return 1e10

        theta0 = np.concatenate([self.log_lengthscales, [self.log_variance, self.log_noise]])
        bounds = [(-3, 3)] * self.n_dim + [(-5, 5), (-5, 3)]
        try:
            result = minimize(neg_log_marginal_likelihood, theta0, method='L-BFGS-B', bounds=bounds)
            if result.success:
                self.log_lengthscales = result.x[:self.n_dim]
                self.log_variance = result.x[self.n_dim]
                self.log_noise = result.x[self.n_dim + 1]
        except Exception:
            pass

        K = self._rbf_kernel(X, X, self.log_lengthscales, self.log_variance)
        K += np.exp(self.log_noise) * np.eye(len(X)) + 1e-6 * np.eye(len(X))
        try:
            self.K_inv = np.linalg.inv(K)
        except np.linalg.LinAlgError:
            self.K_inv = np.eye(len(X)) * 1e-6
        self.alpha = self.K_inv.dot(self.y_train)

    def predict(self, X_new):
        if self.X_train is None:
            return np.zeros(len(X_new)), np.ones(len(X_new))
        k_star = self._rbf_kernel(X_new, self.X_train, self.log_lengthscales, self.log_variance)
        k_ss = np.exp(self.log_variance) * np.ones(len(X_new))

        mu = k_star.dot(self.alpha) * self.y_std + self.y_mean
        v = k_ss - np.sum(k_star.dot(self.K_inv) * k_star, axis=1)
        v = np.maximum(v, 1e-10)
        sigma = np.sqrt(v) * self.y_std
        return mu, sigma


class BayesClimber:
    def __init__(self):
        self.epoch = 0
        self.min_epochs = 1
        self.max_epochs = 200
        self.n_particles = 4

        self.best_score_so_far = -1e3
        self.best_pre_values = None
        self.best_projected_values = None

        self.param_names = []
        self.pt_objs = {}
        self.n_dim = 0

        self.log_string = ""
        self.finished = False

        # BO state
        self.all_X = []  # list of pre-value arrays
        self.all_y = []  # list of scores
        self.gp = None
        self.acq_type = "EI"
        self.ucb_kappa = 2.0
        self.n_restarts = 10
        self.bounds = None  # (n_dim, 2) array of [lo, hi] in pre-image space

        self._current_candidates = None

    def init_from_json(self, spec_dict):
        self.log_string += "Initializing BayesClimber\n"

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

        gp_noise = spec_dict.get("gp_noise", 1.0)
        self.acq_type = spec_dict.get("acq_type", "EI")
        self.ucb_kappa = spec_dict.get("ucb_kappa", 2.0)
        self.n_restarts = spec_dict.get("n_restarts", 10)

        self.gp = GP(self.n_dim, noise=gp_noise)

        # Bounds: center +/- 3 * stepsize in pre-image space
        self.bounds = np.array([
            [self.pt_objs[name].pre_val - 3 * self.pt_objs[name].stepsize,
             self.pt_objs[name].pre_val + 3 * self.pt_objs[name].stepsize]
            for name in self.param_names
        ])

        self.log_string += "n_dim={} batch_size={} acq={}\n".format(
            self.n_dim, self.n_particles, self.acq_type
        )

    def _latin_hypercube_sample(self, n_samples):
        """Generate LHS samples within bounds."""
        samples = np.zeros((n_samples, self.n_dim))
        for d in range(self.n_dim):
            lo, hi = self.bounds[d]
            perm = np.random.permutation(n_samples)
            for i in range(n_samples):
                samples[i, d] = lo + (perm[i] + np.random.random()) / n_samples * (hi - lo)
        return samples

    def _acquisition(self, X, f_best):
        mu, sigma = self.gp.predict(X)
        if self.acq_type == "UCB":
            return mu + self.ucb_kappa * sigma
        else:  # EI
            with np.errstate(divide='ignore', invalid='ignore'):
                z = (mu - f_best) / sigma
                ei = (mu - f_best) * norm.cdf(z) + sigma * norm.pdf(z)
                ei = np.where(sigma < 1e-10, 0.0, ei)
            return ei

    def _optimize_acquisition(self, f_best):
        """Find the point that maximizes the acquisition function."""
        best_x = None
        best_acq = -np.inf

        for _ in range(self.n_restarts):
            x0 = np.array([np.random.uniform(lo, hi) for lo, hi in self.bounds])

            def neg_acq(x):
                val = self._acquisition(x.reshape(1, -1), f_best)
                return -val[0]

            try:
                result = minimize(neg_acq, x0, method='L-BFGS-B',
                                  bounds=[(lo, hi) for lo, hi in self.bounds])
                if -result.fun > best_acq:
                    best_acq = -result.fun
                    best_x = result.x.copy()
            except Exception:
                pass

        if best_x is None:
            best_x = np.array([np.random.uniform(lo, hi) for lo, hi in self.bounds])

        return best_x

    def get_params_next_epoch(self):
        if self.epoch < 2 or len(self.all_X) < 2 * self.n_particles:
            # Initial exploration via LHS
            candidates = self._latin_hypercube_sample(self.n_particles)
        else:
            # Fit GP and use acquisition function
            X = np.array(self.all_X)
            y = np.array(self.all_y)
            self.gp.fit(X, y)
            f_best = np.max(y)

            # Kriging Believer for batch suggestions
            candidates = []
            temp_X = list(self.all_X)
            temp_y = list(self.all_y)

            for _ in range(self.n_particles):
                x_next = self._optimize_acquisition(f_best)
                candidates.append(x_next)

                # Kriging Believer: add with GP mean prediction
                mu_pred, _ = self.gp.predict(x_next.reshape(1, -1))
                temp_X.append(x_next)
                temp_y.append(mu_pred[0])
                self.gp.fit(np.array(temp_X), np.array(temp_y))

            candidates = np.array(candidates)

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
        cur_log += "Total observations: {}\n\n".format(len(self.all_y))

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
