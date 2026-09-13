from optimizers.VaryingParam import VaryingParam
from optimizers.BaseClimb import BaseClimber

import copy
import numpy as np
import random
from scipy.optimize import minimize

"""
TuRBO (Trust Region Bayesian Optimization) climber.

Runs GP-based BO inside an adaptive trust region. If evals keep improving,
the region expands. If they keep failing, it shrinks. If it shrinks below
a minimum, restarts from the best known point.

State of the art for noisy, moderate-dim expensive optimization.

Example config (pnlclimb_specs):

    "num_iters": 20,
    "climb_type": "TURBO",
    "qc_specs": {
        "min_epochs": 1,
        "max_epochs": 20,
        "n_particles": 4,
        "trust_region_length": 0.8,
        "trust_region_min": 0.01,
        "trust_region_max": 2.0,
        "success_tol": 3,
        "fail_tol": 5,
        "max_restarts": 3,
        "params": []
    }

Expands trust region after success_tol consecutive improving epochs,
shrinks after fail_tol consecutive failures, restarts from best point
when the region collapses below trust_region_min.
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


class LocalGP:
    """Minimal GP for trust-region local modeling."""

    def __init__(self, n_dim):
        self.n_dim = n_dim
        self.log_lengthscales = np.zeros(n_dim)
        self.log_variance = 0.0
        self.log_noise = -1.0
        self.X_train = None
        self.y_train = None
        self.y_mean = 0.0
        self.y_std = 1.0
        self.L = None
        self.alpha = None

    def _rbf_kernel(self, X1, X2):
        ls = np.exp(self.log_lengthscales)
        var = np.exp(self.log_variance)
        X1s = X1 / ls
        X2s = X2 / ls
        sq_dist = np.sum(X1s ** 2, axis=1, keepdims=True) + \
                  np.sum(X2s ** 2, axis=1) - 2.0 * X1s.dot(X2s.T)
        return var * np.exp(-0.5 * np.maximum(sq_dist, 0))

    def fit(self, X, y):
        self.X_train = X.copy()
        self.y_mean = np.mean(y)
        self.y_std = max(np.std(y), 1e-6)
        self.y_train = (y - self.y_mean) / self.y_std

        def neg_lml(theta):
            self.log_lengthscales = theta[:self.n_dim]
            self.log_variance = theta[self.n_dim]
            self.log_noise = theta[self.n_dim + 1]
            K = self._rbf_kernel(X, X)
            K += np.exp(self.log_noise) * np.eye(len(X)) + 1e-6 * np.eye(len(X))
            try:
                L = np.linalg.cholesky(K)
                alpha = np.linalg.solve(L.T, np.linalg.solve(L, self.y_train))
                nlml = 0.5 * self.y_train.dot(alpha) + np.sum(np.log(np.diag(L)))
                return nlml
            except np.linalg.LinAlgError:
                return 1e10

        theta0 = np.concatenate([self.log_lengthscales, [self.log_variance, self.log_noise]])
        bounds = [(-3, 3)] * self.n_dim + [(-5, 5), (-5, 2)]
        try:
            result = minimize(neg_lml, theta0, method='L-BFGS-B', bounds=bounds)
            if result.success:
                self.log_lengthscales = result.x[:self.n_dim]
                self.log_variance = result.x[self.n_dim]
                self.log_noise = result.x[self.n_dim + 1]
        except Exception:
            pass

        K = self._rbf_kernel(X, X)
        K += np.exp(self.log_noise) * np.eye(len(X)) + 1e-6 * np.eye(len(X))
        try:
            self.L = np.linalg.cholesky(K)
            self.alpha = np.linalg.solve(self.L.T, np.linalg.solve(self.L, self.y_train))
        except np.linalg.LinAlgError:
            self.L = np.eye(len(X))
            self.alpha = self.y_train * 1e-6

    def predict(self, X_new):
        if self.X_train is None:
            return np.zeros(len(X_new)), np.ones(len(X_new))
        k_star = self._rbf_kernel(X_new, self.X_train)
        mu = k_star.dot(self.alpha) * self.y_std + self.y_mean

        v = np.linalg.solve(self.L, k_star.T)
        k_ss = np.exp(self.log_variance)
        var = k_ss - np.sum(v ** 2, axis=0)
        var = np.maximum(var, 1e-10)
        sigma = np.sqrt(var) * self.y_std
        return mu, sigma

    def sample_posterior(self, X_new, n_samples=1):
        """Draw samples from the GP posterior."""
        mu, sigma = self.predict(X_new)
        samples = np.random.randn(n_samples, len(X_new)) * sigma[None, :] + mu[None, :]
        return samples


class TurboClimber:
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

        # Trust region state
        self.trust_length = 0.8
        self.trust_min = 0.01
        self.trust_max = 2.0
        self.success_tol = 3
        self.fail_tol = 4
        self.max_restarts = 3
        self.restarts_left = 3

        self.success_count = 0
        self.fail_count = 0

        # Center of trust region (in normalized [0,1] space)
        self.center = None
        # Mapping from normalized to pre-image space
        self.bounds = None  # (n_dim, 2)

        # All observations within current trust region restart
        self.tr_X = []  # normalized space
        self.tr_y = []
        # All observations ever (for best tracking)
        self.all_X_pre = []
        self.all_y = []

        self.gp = None
        self._current_candidates_pre = None
        self._current_candidates_norm = None

    def init_from_json(self, spec_dict):
        self.log_string += "Initializing TurboClimber\n"

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

        self.trust_length = spec_dict.get("trust_region_length", 0.8)
        self.trust_min = spec_dict.get("trust_region_min", 0.01)
        self.trust_max = spec_dict.get("trust_region_max", 2.0)
        self.success_tol = spec_dict.get("success_tol", 3)
        self.fail_tol = spec_dict.get("fail_tol", max(4, self.n_dim))
        self.max_restarts = spec_dict.get("max_restarts", 3)
        self.restarts_left = self.max_restarts

        # Bounds in pre-image space: center +/- 3 * stepsize
        self.bounds = np.array([
            [self.pt_objs[name].pre_val - 3 * self.pt_objs[name].stepsize,
             self.pt_objs[name].pre_val + 3 * self.pt_objs[name].stepsize]
            for name in self.param_names
        ])

        # Initialize center at starting point (in normalized space = 0.5)
        self.center = np.full(self.n_dim, 0.5)

        self.gp = LocalGP(self.n_dim)

        self.log_string += "n_dim={} batch={} trust_length={} fail_tol={} restarts={}\n".format(
            self.n_dim, self.n_particles, self.trust_length, self.fail_tol, self.max_restarts
        )

    def _pre_to_norm(self, x_pre):
        """Convert pre-image space to [0, 1] normalized space."""
        return (x_pre - self.bounds[:, 0]) / (self.bounds[:, 1] - self.bounds[:, 0])

    def _norm_to_pre(self, x_norm):
        """Convert [0, 1] normalized space to pre-image space."""
        return x_norm * (self.bounds[:, 1] - self.bounds[:, 0]) + self.bounds[:, 0]

    def _get_trust_bounds(self):
        """Get trust region bounds in normalized space, clipped to [0, 1]."""
        # Per-dimension length scaled by stepsizes
        stepsizes = np.array([self.pt_objs[name].stepsize for name in self.param_names])
        rel_stepsizes = stepsizes / np.mean(stepsizes)
        half_lengths = self.trust_length * rel_stepsizes / 2.0

        lo = np.maximum(self.center - half_lengths, 0.0)
        hi = np.minimum(self.center + half_lengths, 1.0)
        return lo, hi

    def _lhs_in_trust(self, n_samples):
        """Latin hypercube sampling within current trust region."""
        lo, hi = self._get_trust_bounds()
        samples = np.zeros((n_samples, self.n_dim))
        for d in range(self.n_dim):
            perm = np.random.permutation(n_samples)
            for i in range(n_samples):
                samples[i, d] = lo[d] + (perm[i] + np.random.random()) / n_samples * (hi[d] - lo[d])
        return samples

    def _thompson_sample_candidates(self):
        """Use Thompson sampling to suggest candidates within trust region."""
        lo, hi = self._get_trust_bounds()

        # Generate a dense set of random points in the trust region
        n_random = max(200, 20 * self.n_dim)
        X_random = np.random.uniform(lo, hi, (n_random, self.n_dim))

        # Draw a posterior sample and find the best points
        X_all = np.array(self.tr_X) if self.tr_X else np.empty((0, self.n_dim))
        y_all = np.array(self.tr_y) if self.tr_y else np.empty(0)

        if len(X_all) >= 2:
            self.gp.fit(X_all, y_all)
            posterior_samples = self.gp.sample_posterior(X_random, n_samples=self.n_particles)
            # Each row is a draw; pick the argmax of each draw
            candidates = []
            for s in range(self.n_particles):
                best_idx = np.argmax(posterior_samples[s])
                candidates.append(X_random[best_idx].copy())
            return np.array(candidates)
        else:
            return self._lhs_in_trust(self.n_particles)

    def _restart(self):
        """Restart trust region from best known point."""
        self.log_string += "Trust region restart (restarts left: {})\n".format(self.restarts_left)
        self.trust_length = 0.8  # reset to initial
        self.success_count = 0
        self.fail_count = 0
        self.tr_X = []
        self.tr_y = []

        if self.best_pre_values is not None:
            self.center = self._pre_to_norm(self.best_pre_values)
        else:
            self.center = np.full(self.n_dim, 0.5)

    def get_params_next_epoch(self):
        if self.epoch < 2 or len(self.tr_X) < self.n_particles:
            candidates_norm = self._lhs_in_trust(self.n_particles)
        else:
            candidates_norm = self._thompson_sample_candidates()

        # Convert to pre-image space
        candidates_pre = np.array([self._norm_to_pre(c) for c in candidates_norm])

        self._current_candidates_pre = candidates_pre
        self._current_candidates_norm = candidates_norm

        to_ret = {"params_and_scores": []}
        for candidate in candidates_pre:
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

        best_this_epoch = -1e10
        for i, instance_stat in enumerate(stats_for_epoch):
            score = instance_stat["score"]
            x_pre = self._current_candidates_pre[i]
            x_norm = self._current_candidates_norm[i]

            self.tr_X.append(x_norm.copy())
            self.tr_y.append(score)
            self.all_X_pre.append(x_pre.copy())
            self.all_y.append(score)

            proj_vals = {}
            for j, name in enumerate(self.param_names):
                proj_vals[name] = self.pt_objs[name].proj_from_pre(x_pre[j])
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

            best_this_epoch = max(best_this_epoch, score)

            if score > self.best_score_so_far:
                self.best_score_so_far = score
                self.best_pre_values = x_pre.copy()
                self.best_projected_values = dict(proj_vals)

        # Trust region adaptation
        if best_this_epoch > self.best_score_so_far - 1e-10:
            # This epoch found a new best (or tied)
            self.success_count += 1
            self.fail_count = 0
        else:
            self.fail_count += 1
            self.success_count = 0

        old_length = self.trust_length
        if self.success_count >= self.success_tol:
            self.trust_length = min(self.trust_length * 2.0, self.trust_max)
            self.success_count = 0
            cur_log += "Trust region EXPANDED: {:.4f} -> {:.4f}\n".format(old_length, self.trust_length)
            # Re-center on best
            if self.best_pre_values is not None:
                self.center = self._pre_to_norm(self.best_pre_values)
        elif self.fail_count >= self.fail_tol:
            self.trust_length /= 2.0
            self.fail_count = 0
            cur_log += "Trust region SHRUNK: {:.4f} -> {:.4f}\n".format(old_length, self.trust_length)

            if self.trust_length < self.trust_min:
                self.restarts_left -= 1
                if self.restarts_left > 0:
                    self._restart()
                    cur_log += "Trust region below minimum, restarting.\n"
                else:
                    cur_log += "No restarts left, finishing.\n"
                    self.finished = True

        cur_log += "Best score: {} | trust_length: {:.4f} | succ/fail: {}/{}\n".format(
            self.best_score_so_far, self.trust_length, self.success_count, self.fail_count
        )
        if self.best_projected_values:
            cur_log += "Best params: {}\n".format(
                comma_separated_params(self.param_names, self.best_projected_values)
            )
        cur_log += "\n"

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
