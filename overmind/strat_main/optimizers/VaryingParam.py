"""

Basis for a climbable parameter.
"""

#! /usr/bin/env python

import copy
import math
import numpy as np


# pre --> proj
class VaryingParam:
    def __init__(self):
        self.name = None
        self.proj_type = None
        self.proj_mult = 1.0
        self.proj_offset = 0.0

        # Bounds.
        self.max_proj = 1e13
        self.min_proj = -1e13

        # Limits how small a parameter can actually climb.
        # Sometimes decay terms can climb to very very small.
        self.min_abs_proj = 1e-13

        self.pre_val = None
        self.proj_val = None

        self.stepsize = 1

        self.max_expands = 3
        self.max_shrinks = 4

        self.max_steps = 12
        self.chill_n_steps = 0

        # if so, then cast it to int.
        self.is_int = False


        # TODO: quantize the projection/image.
        # Don't think it's necessary rn.

    def from_json(self, spec_dict):
        self.name = spec_dict["name"]
        self.proj_type = spec_dict["proj_type"]
        self.max_proj = spec_dict.get("max_proj", 1e13)
        self.min_proj = spec_dict.get("min_proj", -1e13)

        self.min_abs_proj = abs(spec_dict.get("min_abs_proj", 1e-13))

        self.proj_mult = spec_dict.get("proj_mult", 1)
        self.proj_offset = spec_dict.get("proj_offset", 0)

        self.proj_val = float(spec_dict["proj_value"])
        # Figure out the pre-value from this.
        self.pre_val = self.pre_from_proj(self.proj_val)

        self.stepsize = float(spec_dict["stepsize"])
        self.max_expand_steps = spec_dict.get("max_expand_steps", 3)
        self.max_shrink_steps = spec_dict.get("max_shrink_steps", 4)

        self.chill_n_steps = int(spec_dict.get("chill_n_steps", 0))
        self.is_int = spec_dict.get("is_int", False)

        if self.is_int:
            self.proj_val = int(self.proj_val)

        # valid, msg = self.init_validate()
        # if not valid:
        #    raise Exception("VaryingParam error: {}".format(msg))

    def pre_from_proj(self, proj_val):
        # Invert scaling and offset.
        tmp_proj_val = proj_val - self.proj_offset
        tmp_proj_val = tmp_proj_val / self.proj_mult

        pre_val = 0
        if self.proj_type == "EXP":
            # Depending on the library we might just get exp(large negative) == 0.
            # exp(-40) is around e-18, which is small enough for our purposes.
            if tmp_proj_val <= 0:
                pre_val = -40
            else:
                pre_val = np.log(tmp_proj_val)
        elif self.proj_type == "LINEAR":
            pre_val = tmp_proj_val
        elif self.proj_type == "LOGISTIC":
            # pre_val = -math.log(1 / (tmp_proj_val - 1))

            # We might have had something ruond to 1. In which case,
            # let's just bound it.
            if tmp_proj_val >= 1:
                pre_val = 40
            elif tmp_proj_val <= 0:
                pre_val = -40
            else:
                pre_val = -np.log((1 - tmp_proj_val) / tmp_proj_val)
        else:
            raise Exception("Cannot support proj_type {}".format(self.proj_type))

        return pre_val

    def proj_from_pre(self, pre_val):
        proj_val = 0
        if self.proj_type == "EXP":
            proj_val = np.exp(pre_val)
        elif self.proj_type == "LINEAR":
            proj_val = pre_val
        elif self.proj_type == "LOGISTIC":
            proj_val = 1 / (1 + np.exp(-pre_val))
        else:
            raise Exception("Cannot support proj_type {}".format(self.proj_type))

        # Apply offsets.
        proj_val *= self.proj_mult
        proj_val += self.proj_offset

        # Apply bounds.
        proj_val = min(proj_val, self.max_proj)
        proj_val = max(proj_val, self.min_proj)

        # Apply magnitude bounding.
        if abs(proj_val) < self.min_abs_proj:
            proj_val = self.min_abs_proj * np.sign(proj_val)

        if self.is_int:
            proj_val = int(proj_val)

        return proj_val


# Just to help myself. If I start out with a projected_val, what's
# the best way to... do it?
def try_varying(projected_val, proj_type, step_size):
    param = VaryingParam()
    param.proj_type = proj_type
    pre_val = param.pre_from_proj(projected_val)

    # Step up and down.
    up_pre = pre_val + step_size
    down_pre = pre_val - step_size

    up_post = param.proj_from_pre(up_pre)
    down_post = param.proj_from_pre(down_pre)

    return [down_post, up_post]


# print(try_varying(1, "EXP", 0.25))
