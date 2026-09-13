from optimizers.VaryingParam import VaryingParam

from optimizers.BaseClimb import BaseClimber

import copy
import numpy as np
import random
import json


"""
Slightly different climber

Hyperparameters:

w = inertia.
c_cog = cognitive coefficient. Comparing against the particles best.
c_soc = social coefficient. Comparing particles against the global best.

n_particles
n_epochs

*maybe stop_condition?

We do the update formula.

x_{i+1} = x_i + v_i
v_{i+1} = w*v_i + c_cog*r_cog*(p_i - x_i) + c_soc*r_soc*(g - x_i)

where p_i is the best position of particle i

Yeah. Let's just start coding it up.

From looking online, some suggestions for params are:

w in [0, 1]
c_cog and c_soc in [1, 3]
some people also recommend

I'm gonna have to do this progressively.

I'm going to use deepcopy a bit just to save myself from

Basic SignalOptimizer is looking ok with these settings:

climb_specs = {
    "min_epochs": 1,
    "max_epochs": 20,
    "n_particles": 20,
    "inertia_w": 0.5,
    "c_self": 0.5,
    "c_global": 2.0,
    "params": [],
}


ins_dates = dates_list("20230821", "20230826")
oos_dates = dates_list("20230901", "20230905")

my_optimizer = signaloptimizer.SigOptimizer(
    cur_work_dir,
    sigspec,
    ins_dates,
    climb_specs=climb_specs,
    climb_type="PARTICLE",
    markout_tgt=60,
    markout_cap=0.0035,
)


Another example, which decreases inertia over time. :

      "climb_type": "PARTICLE",
      "qc_specs": {
	  "min_epochs": 1,
	  "max_epochs": 12,
	  "n_particles": 20,
	  "inertia_w": 0.7298,
	  "low_inertia_w": 0.4,
	  "c_self": 1.49618,
	  "low_c_self": 0.5,

	  "c_global": 1.49618,

	  "inertia_w.0": 0.5,
	  "c_self.0": 0.5,
	  "c_global.0": 2.0,
	  "params": []
      },


References:
https://web2.qatar.cmu.edu/~gdicaro/15382-Spring18/hw/hw3-files/pso-book-extract.pdf

https://web.archive.org/web/20030503203304/http://antho.huntingdon.edu/publications/Off-The-Shelf_PSO.pdf
"Good Parameters for Particle Swarm Optimizatin"
https://web.archive.org/web/20200213130101/https://pdfs.semanticscholar.org/a5d2/8c26a2e2824170d67b69f14120cf67cabe26.pdf
https://bee22.com/resources/Bergh%202006.pdf

https://bee22.com/resources/Bergh%202006.pdf
 > Their results indicate that choosing w ∈ [0.8, 1.2] results in faster
   convergence, but that larger w values (> 1.2) result in more failures to converge
 > A w close to 1 is preferrable. Smaller w causes deceleration
 > Further empirical experiments have been performed with an inertia weight set to
   decrease linearly from 0.9 to 0.4 during the course of a simulation, this time using four
   different objective functions [121]

Comparing inertial weights and Constriction factor in particle swarm optimization
  w = 0.73, c_1 = c_2 = 1.5

https://www.semanticscholar.org/paper/A-study-of-particle-swarm-optimization-particle-Bergh-Engelbrecht/270d24da13010c775f3b09b9fc53b2e612fc97aa/figure/1

TODO:
  - Constrained?

  I'm going to have some things have 0 velocity until after they're allowed.

  - Going to iontroduce something called snapback.
    - If we are more than X amount worse (like, 90%), AND the cur round is > some minimal amount,
      then start jumping the particle to something between the global_best and local_best.
      Like, some fraction.
      So, there is some set of parmeters:
    - snapback_frac: 0.9
    - snapback_min_epoch: 5 (do not consider snapping back before then.)
    - snapback_global_frac: 0.9 (if this, snap to 90% of the way between current_pt and global_best.)


"""

# For randomizing.
RND_SEED = 271828
random.seed(RND_SEED)

# Can use random.uniform(a, b ) to get values.


def comma_separated_params(param_names, names_to_vals):
    to_ret = ""
    for pn in param_names:
        to_ret += "{:.6f},".format(names_to_vals[pn])
    if len(to_ret) > 0:
        to_ret = to_ret[:-1]
    # Chop off the last comma.
    return to_ret


# Owned by both the particles and particleclimb.
# Maps from param_name to cur_value and full_value, and also
class ParamInstance:
    def __init__(self, param_names, pt_objs, mapped_pre_values):
        self.param_names = param_names
        self.pt_objs = pt_objs
        self.mapped_pre_values = mapped_pre_values
        self.score = -1e3

    # When this gets evaluated.
    def set_score(self, score):
        self.score = score

    def get_score(self):
        return self.score

    def get_projected_values(self):
        to_ret = {}
        for one_name in self.param_names:
            pre_val = self.mapped_pre_values[one_name]
            post_val = self.pt_objs[one_name].proj_from_pre(pre_val)
            to_ret[one_name] = post_val
        return to_ret


# Each particle only knows


class Particle:
    def __init__(
        self, position, velocity, param_names, pt_objs, inertia_w, c_self, c_global
    ):
        # Map: param_name -> pre_value
        # it is NOT a ParamInstance.
        self.position = position
        # Map: param_name -> double
        self.velocity = velocity

        self.param_names = param_names
        self.pt_objs = pt_objs

        self.last_score = -1e3
        self.best_score = -1e3
        self.best_instance = None

        # List of ParamInstances. Sorted by epoch.
        self.param_instances = []

        # Create the initial ParamInstance.
        init_param_instance = ParamInstance(param_names, pt_objs, position)
        self.param_instances.append(init_param_instance)

    def __str__(self):
        return (
            "Particle: "
            + str(self.position)
            + " "
            + str(self.velocity)
            + " "
            + str(self.fitness)
        )

    def setScore(self, epoch, score):
        if epoch > len(self.param_instances):
            raise Exception(
                "Particle.setScore issue: epoch {} > {}".format(
                    epoch, len(self.param_instances)
                )
            )
        self.param_instances[epoch].set_score(score)
        self.last_score = score
        # Check if this is the best score.
        if score > self.best_score:
            self.best_score = score
            self.best_instance = self.param_instances[epoch]

    def setXV(self, epoch, new_position, new_velocity):
        self.position = copy.deepcopy(new_position)
        self.velocity = copy.deepcopy(new_velocity)
        new_param_instance = ParamInstance(self.param_names, self.pt_objs, new_position)
        self.param_instances.append(new_param_instance)

    def getCurProjection(self):
        return self.param_instances[-1].get_projected_values()

    def getCurInstance(self):
        return self.param_instances[-1]


# Maybe something about


class ParticleClimber:
    def __init__(self):
        self.epoch = 0
        self.min_epochs = 10
        self.max_epochs = 200

        # Params for the particle climber.
        self.n_particles = 10
        self.inertia_w = 0.8
        self.low_inertia_w = 0.8
        self.inertia_w_series = [self.inertia_w]

        self.c_self = 1
        self.low_c_self = 1
        self.c_self_series = [self.c_self]

        self.c_global = 2

        self.best_score_so_far = -1e3

        # a ParamInstance.
        self.best_instance_so_far = None

        self.normalize_v = False

        # Just index them.
        self.particles = []
        self.log_string = ""
        self.cur_round_log = ""

        self.param_names = []
        self.pt_objs = {}

        self.do_snapback = False
        self.snapback_frac = 0.9
        self.snapback_min_epoch = 4
        self.snapback_global_frac = 0.9


        # Give a 2 round gap between snapbacks.
        self.rounds_between_snapbacks = 2
        self.last_snapback_epoch = -1

        # If we fit the number of snapback triggering conditions, we let ourselves snap back.
        self.num_snapback_triggers = 0
        self.snapback_triggers_before_fire = 2

        # If we do snapback, decrease velocities...
        self.snapback_v_damp_frac = 0.5

        self.finished = False

    # Gets the
    def init_from_json(self, spec_dict):
        self.log_string += "Initiailizing ParticleClimber\n"

        self.epoch = 0
        self.min_epochs = spec_dict["min_epochs"]
        self.max_epochs = spec_dict["max_epochs"]

        if "stop_conditions" in spec_dict:
            self.stop_conditions = spec_dict["stop_conditions"]

        self.n_particles = spec_dict["n_particles"]
        self.inertia_w = spec_dict["inertia_w"]
        self.low_inertia_w = spec_dict.get("low_inertia_w", self.inertia_w)
        self.inertia_w_series = np.linspace(self.inertia_w, self.low_inertia_w, self.max_epochs).tolist()

        self.c_self = spec_dict["c_self"]
        self.low_c_self = spec_dict.get("low_c_self", self.c_self)
        self.c_self_series = np.linspace(self.c_self, self.low_c_self, self.max_epochs).tolist()

        self.c_global = spec_dict["c_global"]

        self.normalize_v = spec_dict.get("normalize_v", False)

        self.do_snapback = spec_dict.get("do_snapback", False)
        self.snapback_frac = spec_dict.get("snapback_frac", 0.9)
        self.snapback_min_epoch = spec_dict.get("snapback_min_epoch", 4)
        self.snapback_global_frac = spec_dict.get("snapback_global_frac", 0.9)
        self.snapback_v_damp_frac = spec_dict.get("snapback_v_damp_frac", 0.5)

        # Initialize the pt_objs and the param_names.
        for one_param_json in spec_dict["params"]:
            one_param_transform = VaryingParam()
            one_param_transform.from_json(one_param_json)

            self.param_names.append(one_param_transform.name)
            self.pt_objs[one_param_transform.name] = one_param_transform

        # Initialize the particles.
        for i in range(self.n_particles):
            # Initialize the

            x_dct = {}
            v_dct = {}
            #self.log_string += "Initializing particle {}: ".format(i)
            for param_name in self.param_names:
                start_pre = self.pt_objs[param_name].pre_val
                # Note: these stepsizes will need to be larger.
                # So maybe let's just multiply it by 3 a priori.
                stepsize = self.pt_objs[param_name].stepsize * 2
                x_dct[param_name] = random.uniform(
                    start_pre - stepsize, start_pre + stepsize
                )
                # For initialization, actually, include the starting point as the first one.
                if i == 0:
                    x_dct[param_name] = start_pre

                # Smaller initial velocity.
                v_dct[param_name] = random.uniform(0, 0.5 * stepsize)
                if self.pt_objs[param_name].chill_n_steps > 0:
                    # Do not give a real velocity while we're letting stuff chill.
                    # Enable that later.
                    v_dct[param_name] = 0
                self.log_string += "{}: x={} v={} ".format(param_name, x_dct[param_name], v_dct[param_name])
            self.log_string += "\n"


            # This defines the particle
            cur_pt = Particle(
                x_dct,
                v_dct,
                self.param_names,
                self.pt_objs,
                self.inertia_w,
                self.c_self,
                self.c_global,
            )
            self.particles.append(cur_pt)

    # Returns the actual dict.
    def get_best_climbed_params(self):
        return self.best_instance_so_far.get_projected_values()

    def get_best_score(self):
        return self.best_score_so_far


    # Returns an array of dicts of param -> value.
    def get_params_next_epoch(self):
        to_ret = {
            "params_and_scores": [],
            "best_score_stats": {},
            "best_score": -5000,
            "best_param": [],
        }

        params_and_scores = []
        for one_pt in self.particles:
            param_dct = {
                "ordered_param_instances": [],
                "has_run": False,
                "varying_param": "",
                "stats": {},
                "score": -5000,
            }
            # All I need is the name and projection value
            ordered_projection_vals = []
            for name, projection_val in one_pt.getCurProjection().items():
                ordered_projection_vals.append(
                    {"name": name, "projection_value": projection_val}
                )

            param_dct["ordered_param_instances"] = ordered_projection_vals
            params_and_scores.append(param_dct)
        to_ret["params_and_scores"] = params_and_scores

        return to_ret

    def get_results_evaluate(self, stats_for_epoch):
        self.cur_round_log = "*" * 100

        # Pass 1: update all the scores
        self.cur_round_log += "Epoch {}: updating scores\n".format(self.epoch)
        self.cur_round_log += ",".join(self.param_names)
        # Perhaps add in pnl-specific stats too

        is_pnlscores = False
        if "stats" in stats_for_epoch[0] and len(stats_for_epoch[0]["stats"]) > 0:
            is_pnlscores = True
            self.cur_round_log += (
                ",,net_pnl,closed_pnl,sharpe,num_trds,pct_positive,score"
            )

        self.cur_round_log += "\n"

        best_score_this_round = -1e10

        for i, instance_stat in enumerate(stats_for_epoch):
            self.particles[i].setScore(self.epoch, instance_stat["score"])
            proj_vals = comma_separated_params(
                self.param_names, self.particles[i].getCurProjection()
            )

            score_line = ""
            if is_pnlscores:
                score_line = "{:.2f},{:.2f},{:.2f},{:.1f},{:.2f},{:.4f}".format(
                    instance_stat["stats"]["avg_pnl"],
                    instance_stat["stats"]["avg_closed_pnl"],
                    instance_stat["stats"]["sharpe"],
                    instance_stat["stats"]["avg_numtrds"],
                    instance_stat["stats"]["pct_positive"],
                    instance_stat["score"],
                )
            else:
                score_line = "{:.4f}".format(instance_stat["score"])

            self.cur_round_log += "{} --> {}\n".format(proj_vals, score_line)

            best_score_this_round = max(best_score_this_round, instance_stat["score"])

            if instance_stat["score"] > self.best_score_so_far:
                self.best_score_so_far = instance_stat["score"]
                self.best_instance_so_far = self.particles[i].getCurInstance()
        self.cur_round_log += "Best score: {}\n".format(self.best_score_so_far)
        if self.best_score_so_far > 0:
            best_instance_vals = comma_separated_params(
                self.param_names, self.best_instance_so_far.get_projected_values()
            )
            self.cur_round_log += "Best params: {}\n".format(best_instance_vals)

        self.cur_round_log += "\n\n"
        # Increment epoch and generate the next positions and velocities.
        self.epoch += 1

        self.cur_round_log += "Starting epoch {}\n".format(self.epoch)

        # Next:
        # check if we need to snapback or not. If we meet the conditions, do that instead
        # of a proper particle.
        should_snapback = False
        if self.do_snapback:
            if best_score_this_round > 0 and best_score_this_round < self.snapback_frac * self.best_score_so_far and self.epoch > self.snapback_min_epoch and self.epoch > self.last_snapback_epoch  + self.rounds_between_snapbacks:
                self.num_snapback_triggers += 1
                self.cur_round_log += "{} < {}*{}, incrementing snapback_triggers count {}\n".format(best_score_this_round, self.best_score_so_far, self.snapback_frac, self.num_snapback_triggers)
            elif self.num_snapback_triggers > 0:
                self.num_snapback_triggers = 0
                self.cur_round_log += "No needed snapback, resetting snapback_triggers count {}\n".format(self.num_snapback_triggers)

            if self.num_snapback_triggers >= self.snapback_triggers_before_fire:
                self.num_snapback_triggers = 0
                should_snapback = True
                self.cur_round_log += "snapback_trigger count {} >= {},  snapping back\n".format(self.num_snapback_triggers, self.snapback_triggers_before_fire-1)

        #self.cur_round_log += "snapback status: num_triggers {}, best_score {} vs {}\n".format(self.num_snapback_triggers, best_score_this_round, self.best_score_so_far)


        for i in range(self.n_particles):
            x_dct = {}
            v_dct = {}

            # Generate the positions:
            last_pos = copy.deepcopy(self.particles[i].position)
            last_v = copy.deepcopy(self.particles[i].velocity)

            last_pos_projection = self.particles[i].getCurProjection()

            # Generate the velocities
            best_particle_x = self.particles[i].best_instance.mapped_pre_values
            best_global_x = self.best_instance_so_far.mapped_pre_values

            # Update velocity first.
            for param_name in self.param_names:
                r_cog = random.uniform(0.0, 1)
                r_soc = random.uniform(0.0, 1)
                # I guess, still set the v...
                v_dct[param_name] = (
                    # Potentially change inertia_w to the ranged one
                    # And keep it to inside the list pls.
                    self.inertia_w_series[ min(len(self.inertia_w_series)-1, self.epoch-1) ] * last_v[param_name]
                    #self.inertia_w * last_v[param_name]

                    # + self.c_self
                    + self.c_self_series[ min(len(self.c_self_series)-1, self.epoch-1) ]
                    * r_cog
                    * (best_particle_x[param_name] - last_pos[param_name])
                    + self.c_global
                    * r_soc
                    * (best_global_x[param_name] - last_pos[param_name])
                )
                if should_snapback:
                    v_dct[param_name] *= self.snapback_v_damp_frac

                # In the instance that we had stopped velocities for chill_n_steps reasons, restart them up.
                if self.pt_objs[param_name].chill_n_steps == self.epoch:
                    cur_stepsize = self.pt_objs[param_name].stepsize * 2
                    next_v = random.uniform(0, cur_stepsize)
                    #self.log_string += "Particle {}: Chill step for {} at epoch {}:  velocity at {}\n".format(i, param_name, self.epoch, next_v)
                    v_dct[param_name] = next_v

                # Maybe just normalize with inertia and the c's?
                if self.normalize_v:
                    v_dct[param_name] = v_dct[param_name] / (self.inertia_w_series[ min(len(self.inertia_w_series)-1, self.epoch-1) ] + r_cog + r_soc)

                # self.cur_round_log += "{} v: last_v {} r_cog {} best_particle_diff ({}-{}={}) r_soc {} best_global_diff ({}-{}={}) final {}\n".format(
                #    param_name,
                #    last_v[param_name],
                #    r_cog,
                #    best_particle_x[param_name],
                #    last_pos[param_name],
                #    best_particle_x[param_name] - last_pos[param_name],
                #    r_soc,
                #    best_global_x[param_name],
                #    last_pos[param_name],
                #    best_global_x[param_name] - last_pos[param_name],
                #    v_dct[param_name]
                # )

            # Use the just-computed velocity to update position for the
            # next round.
            #self.cur_round_log += "*"*20 + "\n"
            #self.cur_round_log += "Particle {}\n".format(i)
            # self.cur_round_log += "Updating positions\n"
            for param_name in self.param_names:
                if should_snapback:
                    # Snapping to approximation.
                    # A sliding scale between the best current local location and the
                    # best global location.
                    x_dct[param_name] = (1 - self.snapback_global_frac) * best_particle_x[param_name] + (self.snapback_global_frac) * best_global_x[param_name]
                    #self.cur_round_log += "{}: (lclbest {}, globbest {})={} (snapback!)\n".format(param_name, best_particle_x[param_name], best_global_x[param_name], x_dct[param_name])
                else:
                    x_dct[param_name] = last_pos[param_name] + v_dct[param_name]
                    #self.cur_round_log += "{}: ({})+({})={}\n".format(param_name, last_pos[param_name], v_dct[param_name], x_dct[param_name])

            # self.cur_round_log += "\n"

            self.particles[i].setXV(self.epoch, x_dct, v_dct)
            new_pos_projection = self.particles[i].getCurProjection()

            # Great. We have new values for this particle.
            # self.cur_round_log += "~" * 20
            # self.cur_round_log += "\nParticle {}\n".format(i)
            # old_x->new_x
            # self.cur_round_log += "{}\n".format(
            #    comma_separated_params(self.param_names, last_pos_projection)
            # )
            # self.cur_round_log += "v\n"
            # self.cur_round_log += "{}\n".format(
            #    comma_separated_params(self.param_names, new_pos_projection)
            # )
            #

        self.cur_round_log += "-" * 100 + "\n"

        self.log_string += self.cur_round_log
        # All done.
