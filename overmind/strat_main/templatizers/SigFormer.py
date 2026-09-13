#! /usr/bin/env python

import os
import json
import copy

# Stupid hack
# At some point do this properly, turn stuff into packages.
import sys

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from optimizers.VaryingParam import VaryingParam
from util.pathing import templatedir


"""
Given a source for templates, make some classes that let you translate between
the template and the useable json.

TODO: have this carry history too.
TODO: have it include... dependencies too.

"""

# Figure out where the templates are located.

sig_templates = os.path.join(templatedir(), "signals")


"""
  What's the operating pattern again. Each signalspec should have a set of fixed vals that it overrides.
  And a set of climbables that it keeps.
  The climbables are ClimbableParams.

  No longer sure what I should be doing but let's just keep writing and fix things as we go along.
"""

# Very rough thing rn. By default, we're not going to use flagging properties
# in stuff like sigmid and sigvomkt.
# Eventually, we will.
SIGS_USE_FLAGGING = True


# Returns one mondo dict.
def merge_extra_climbables(climbables, extra_climbables):
    if len(extra_climbables) == 0:
        return climbables

    # Otherwise, iterate through its set of dicts and push them into the climbables. Yes?
    for sigformer_tag, climbable_params_superset in extra_climbables.items():
        if sigformer_tag not in climbables:
            raise Exception(
                "Tried to add params for {} inside {} - you probably misconfigured".format(
                    sigformer_tag, climbables.keys()
                )
            )
        for climb_param_name, climb_param_dict in climbable_params_superset.items():
            climbables[sigformer_tag][climb_param_name] = climb_param_dict
    return climbables


class SignalSpec:
    # Climbables is a collection of VaryingParam
    # One thing to note: A SignalSpec has basically no knowledge
    # of any of its child values.
    def __init__(
        self,
        template_name,
        out_signame,
        child_signames,
        fixed_vals,
        climbables,
        classname,
        class_suffix,
        symbol="",
        markets=[],
        traded_symbol="",
        traded_markets=[],
    ):
        # Example: "SigBookImpulse.json"
        self.template_name = template_name
        # Example: {name_prefix}_bookimpulse
        # where name_prefix is something like "round0"
        self.name = out_signame
        # Used for... eh labels and ish.
        # Something like "BookImp"
        self.classname = classname
        # Something like "_SELF" or "_Side{SYMBOL}"
        self.class_suffix = class_suffix

        self.symbol = symbol
        self.markets = markets
        self.traded_symbol = traded_symbol
        self.traded_markets = traded_markets

        # Get the actual path.
        self.template_path = os.path.join(sig_templates, self.template_name)

        # Read in the template.
        with open(self.template_path, "r") as f:
            self.base_setting = json.loads(f.read())

        # print(self.base_setting)
        # Apply fixed overrides.
        self.fixed_vals = fixed_vals
        self.climbables = climbables

        # Why does this exist?
        # Well, a single SignalClass can have multiple signals. Each individual one is labelled by their sigformer_tab.
        # However, multiple signals may have parameters named the same thing: like, time_decay.
        # In which case, the Climbables are given names (which show up during the climb logs), but we need a way to map those back
        # to the map says, ok, time_decay_a goes to signformer_tag "xyz_bbb" and param_name "time_decay".
        self.climbable_inverted_map = {}

        for sigformer_tag, fixed_val_dict in fixed_vals.items():
            for param_name, param_val in fixed_val_dict.items():
                for one_sig in self.base_setting["signals"]:
                    if one_sig["sigformer_tag"] == sigformer_tag:
                        one_sig[param_name] = param_val


        for sigformer_tag, varying_param in climbables.items():
            # Here, param_name is what shows up in the signal json.
            # param_val is the climb dict, which also has a name. That is what shows up
            # when we print out the climb.
            for param_name, param_climb_setting in varying_param.items():
                self.climbable_inverted_map[param_climb_setting["name"]] = {
                    "sigformer_tag": sigformer_tag,
                    "param_name": param_name,
                }

                # Walk through the base setting too...
                for one_sig in self.base_setting["signals"]:
                    if one_sig["sigformer_tag"] == sigformer_tag:
                        one_sig[param_name] = param_climb_setting["proj_value"]


        self.child_signames = child_signames

    def override_fixed_vals(fixed_vals, fixed_val_overrides):
        for override_name, override_val in fixed_val_overrides.items():
            # Has to be the sigformer_tag.
            if override_name in fixed_vals:
                for param_name, param_val in override_val.items():
                    fixed_vals[override_name][param_name] = param_val
            else:
                print(
                    "Sigformer warning: could not find override name {} from {}".format(
                        override_name, fixed_vals.keys()
                    )
                )

        return fixed_vals

    # Something to set the initvalues of the param itself.
    # Basically, reinit.

    def set_varied_param_centers(self, names_to_vals):
        for name, val in names_to_vals.items():
            if name in self.climbable_inverted_map:
                inv_map_setting = self.climbable_inverted_map[name]
                # I should really just put this in a try/catch.
                # Anyway, just set this value in-place.
                self.climbables[inv_map_setting["sigformer_tag"]][
                    inv_map_setting["param_name"]
                ]["proj_value"] = val
            else:
                # TODO: don't do this long term.
                # This is only because I inserted something in climbables
                # that just should not happen.
                # print("Could not find {} in {}".format(name, self.climbable_inverted_map.keys()))
                for sf_tag, sf_dct in self.fixed_vals.items():
                    if name in sf_dct:
                        sf_dct[name] = val
                        # print("Found {} in {}".format(name, sf_tag))
                # print(self.fixed_vals)

    # Applying settings, getting back a dict.
    # The settings tend to be climbables
    # Add a suffix to all names too...
    # This... doesn't do anything if names_to_vals is empty.
    def apply_varied_params(self, names_to_vals, suffix=""):
        if len(names_to_vals) == 0:
            # If we aren't changing any params, then we would have never
            # modified via the suffix. So return the right thing then.

            new_name = self.name
        else:
            # new_name is something like
            # "round0_mid_{suffix}"
            new_name = self.name + suffix

        new_setting = copy.deepcopy(self.base_setting)
        for name, val in names_to_vals.items():
            # Find the appropriate signal dict from the inverted map.

            # Again, a quick hack that's the same as the hack
            # from set_varied_param_centers
            if name in self.climbable_inverted_map:
                inv_map_setting = self.climbable_inverted_map[name]

                for one_sig in new_setting["signals"]:
                    if one_sig["sigformer_tag"] == inv_map_setting["sigformer_tag"]:
                        one_sig[inv_map_setting["param_name"]] = val

            else:
                for sf_tag, sf_dct in self.fixed_vals.items():
                    if name in sf_dct:
                        # Then we look through the one_sig signals.
                        for one_sig in new_setting["signals"]:
                            if one_sig["sigformer_tag"] == sf_tag:
                                one_sig[name] = val

        # Also rename the signals with respect to the suffix.
        for one_sig in new_setting["signals"]:
            # Check if we need its name modified too. Basically, modify all the
            # stored names into suffixed names.
            for one_sig_key, one_sig_val in one_sig.items():
                if one_sig_val == self.name or one_sig_val in self.child_signames:
                    one_sig[one_sig_key] = one_sig_val + suffix

        return {"name": new_name, "signals_dict": new_setting}

    def get_varying_params(self):
        list_of_params = []
        for one_sig_tag, sig_climbables_dict in self.climbables.items():
            for one_sig_param_name, varying_param_dict in sig_climbables_dict.items():
                list_of_params.append(varying_param_dict)

        return list_of_params

    def class_suffix_name(self):
        return "{}_{}".format(self.classname, self.class_suffix)

    # A full json, not
    def get_sig_dict(self):
        return self.apply_varied_params({})

    def to_json_path(self, out_path):
        pass

    # Given a preexisting signalspec, add some climbable settings to it.

    # At some point: I want to serialize and deserialize these so I can
    def spec_to_json(self):
        pass

    def spec_from_json(self):
        pass

    # Extras can be things like score, etc.
    # Quick way to cache a sigparam.
    def export_with_param_settings(self, names_to_param_vals, extras={}):
        export_dct = {
            "classname": self.classname,
            "symbol": self.symbol,
            "markets": self.markets,
            "traded_symbol": self.traded_symbol,
            "traded_markets": self.traded_markets,
            "param_vals": names_to_param_vals,
            "fixed_vals": self.fixed_vals,
        }

        for extra_name, extra_val in extras.items():
            export_dct[extra_name] = extra_val

        return export_dct


class SigFileSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigFile.json"
        classname = "File"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )

        child_signames = []

        self.tmpl_tags = ["top_level_signal"]

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame

        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class SigMidSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        # All features will have this, just in case if they want
        # ders or midpx libras.
        # As we move on to more complex things we'll probably
        # have to break this interface more too.
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigMid.json"
        classname = "Mid"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )

        child_signames = []

        self.tmpl_tags = ["top_level_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                }
            }

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides

class SigQuoteMidSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        # All features will have this, just in case if they want
        # ders or midpx libras.
        # As we move on to more complex things we'll probably
        # have to break this interface more too.
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigQuoteMid.json"
        classname = "QuoteMid"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )

        child_signames = []

        self.tmpl_tags = ["top_level_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                }
            }

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides



class SigBidSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        # All features will have this, just in case if they want
        # ders or midpx libras.
        # As we move on to more complex things we'll probably
        # have to break this interface more too.
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigBid.json"
        classname = "Bid"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )

        child_signames = []

        self.tmpl_tags = ["top_level_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {"top_level_signal": {"symbol": symbol, "books": markets}}

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class SigAskSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        # All features will have this, just in case if they want
        # ders or midpx libras.
        # As we move on to more complex things we'll probably
        # have to break this interface more too.
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigAsk.json"
        classname = "Ask"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )

        child_signames = []

        self.tmpl_tags = ["top_level_signal"]

        if len(fixed_vals) == 0:
            fixed_vals = {"top_level_signal": {"symbol": symbol, "books": markets}}

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame

        climbables = merge_extra_climbables(climbables, extra_climbables)
        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class SigTrdSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        # All features will have this, just in case if they want
        # ders or midpx libras.
        # As we move on to more complex things we'll probably
        # have to break this interface more too.
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigTrd.json"
        classname = "Trd"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )

        child_signames = []

        self.tmpl_tags = ["top_level_signal"]

        if len(fixed_vals) == 0:
            fixed_vals = {"top_level_signal": {"symbol": symbol, "books": markets}}

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# x = SigMidSpec("BTCUSDT",  ["BinanceSPOT"], "test", fixed_val_overrides={}, climbables={} )
# print(json.dumps(x.get_sig_dict(), indent=2))


class SigWMidSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigWMid.json"
        classname = "WMid"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        child_signames = []

        self.tmpl_tags = ["top_level_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "return_price": False,
                }
            }

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# x = SigWMidSpec("BTCUSDT",  ["BinanceSPOT"], "test", fixed_val_overrides={}, climbables={} )
# print(json.dumps(x.get_sig_dict(), indent=2))


# Like a SigLinear
class SigLinearSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigLinear.json"
        classname = "Linear"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        child_signames = []
        self.tmpl_tags = ["top_level_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {"top_level_signal": {}}

        # Here, we're futzing around with coefficients, so we turn on the append functionality.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# Let's try the interesting override.
# ovr = {
#    "top_level_signal": {
#        "sig_names": ["sig_a", "sig_b", "sig_c"],
#        "sig_a_coef": 100,
#        "sig_b_coef": 120,
#        "sig_c_coef": 140,
#        "offset": 0.2
#    }
# }
# x = SigLinearSpec("test", fixed_val_overrides=ovr, climbables={} )
# print(json.dumps(x.get_sig_dict(), indent=2))


# Like a DA
class SigTrdImpSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigTrdImpulse.json"
        classname = "TrdImp"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        ref_signame = out_signame + "_refpx"

        child_signames = [ref_signame]
        self.impl_tags = ["top_level_signal", "ref_px"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "use_killed_lvl_boost": True,
                    "killed_lvl_boost": 2,
                    "min_tgt_diff_bps": 0.00001,
                    "batch": False,
                    "normalize_size_draped_inside": True,
                     # If we normalize against the draped_inside, then
                     # size_decay_const should much smaller, because now we're comparing
                     # against measurements against the inside.
                    "sz_decay": 0.5,
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["tgt_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame

        # Set the climbables.
        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 0.5,
                        "stepsize": 0.5,
                    },
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },

                    "killed_lvl_boost": {
                        "name": "killed_lvl_boost",
                        "proj_type": "EXP",
                        "proj_value": 1.5,
                        "stepsize": 0.005,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                    "tgt_sig_decay": {
                        "name": "tgt_sig_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.90,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_disagree": {
                        "name": "tgt_decay_disagree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.5,
                        "max_shrink_steps": 4,
                        "max_expand_steps": 4,
                    },
                    "bungee_mult_frac": {
                        "name": "bungee_mult_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.5,
                        "max_shrink_steps": 4,
                        "max_expand_steps": 4,
                    },
                    "bungee_denom": {
                        "name": "bungee_denom",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.02,
                        "stepsize": 0.5,
                        "max_shrink_steps": 4,
                        "max_expand_steps": 4,
                    },
                    "draped_tdc_s": {
                        "name": "draped_tdc_s",
                        "proj_type": "EXP",
                        "proj_value": 100,
                        "stepsize": 0.5,
                        "max_shrink_steps": 4,
                        "max_expand_steps": 4,
                    },

                    "px_decay": {
                        "name": "px_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.0001,
                        "stepsize": 0.25,
                        "min_proj": 1e-8,
                        "max_shrink_steps": 4,
                        "max_expand_steps": 4,
                    },

                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# x = SigTrdImpSpec("BTCUSDT", ["BinanceSPOT"],  "test", {}, {})
# print(json.dumps(x.get_sig_dict(), indent=2))


# CAT-y
class SigAvgTrdSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigAvgTrd.json"
        classname = "AvgTrd"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        ref_signame = out_signame + "_refpx"

        child_signames = [ref_signame]
        self.impl_tags = ["top_level_signal", "ref_px"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "sz_decay_type": "EXP_SIZE",
                    "calc_style": "AVG_REL_PRICE",
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }
        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["ref_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 2000,
                        "stepsize": 0.5,
                    },
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# x = SigAvgTrdSpec("BTCUSDT", ["BinanceSPOT"],  "test", {}, {})
# print(json.dumps(x.get_sig_dict(), indent=2))


# I was here.
class SigAvgTrdDerSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "AvgTrdDer.json"
        classname = "AvgTrdDer"

        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        avgt_signame = out_signame + "_avgtrd"
        ref_signame = out_signame + "_refpx"
        der_decay_signame = out_signame + "_der_decay"

        child_signames = [avgt_signame, ref_signame, der_decay_signame]
        self.impl_tags = ["top_level_signal", "avg_trd", "ref_px", "der_decay_signal"]

        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "take_pred_returns": False,
                    "defer_decay": False,
                    "max_impulse": 0.02,
                    "max_bound_mult": 30,
                    "min_tick_decay": 0.999,
                    "tgt_decay_denom": -1,
                    "output_sigmoid": True,
                    "output_sigmoid_denom": 0.01,
                },
                "avg_trd": {
                    "symbol": symbol,
                    "books": markets,
                    "sz_decay_type": "EXP_SIZE",
                    "return_price": False,
                    "calc_style": "AVG_REL_PRICE",
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
                "der_decay_signal": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }
        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["pred_signal"] = avgt_signame
        # The midpx of the traded symbol is the der input.
        fixed_vals["top_level_signal"]["tgt_signal"] = der_decay_signame

        fixed_vals["avg_trd"]["name"] = avgt_signame
        fixed_vals["avg_trd"]["ref_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame

        fixed_vals["der_decay_signal"]["name"] = der_decay_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_agree": {
                        "name": "tgt_decay_agree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_disagree": {
                        "name": "tgt_decay_disagree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_denom": {
                        "name": "tgt_decay_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "stepsize": 0.25,
                    },
                    "output_sigmoid_denom": {
                        "name": "output_sigmoid_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.03,
                        "stepsize": 0.5,
                        "chill_n_steps": 0,
                    },
                },
                "avg_trd": {
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 2000,
                        "stepsize": 0.5,
                    },
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.98,
                        "stepsize": 0.5,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# Don't even define the pxders, because the topology is
# the exact same - we just need to create the overrides set.


class SigAvgTrdMidLibraSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "AvgTrdMidLibra.json"
        classname = "AvgTrdMidLibra"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        avgt_signame = out_signame + "_avgtrd"
        ref_signame = out_signame + "_refpx"
        libra_decay_signame = out_signame + "_libra_decay"

        child_signames = [avgt_signame, ref_signame, libra_decay_signame]
        self.impl_tags = ["top_level_signal", "avg_trd", "ref_px", "libra_decay_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "max_impulse": 0.01,
                    "apply_basic_decay": True,
                    "sig_a_scale": 1,
                    "sig_b_scale": 0,
                    "sig_a_selfscale": 1,
                    "max_bound_mult": 30,
                    "output_sigmoid": True,
                    "output_sigmoid_denom": 0.001,
                },
                "avg_trd": {
                    "symbol": symbol,
                    "books": markets,
                    "sz_decay_type": "EXP_SIZE",
                    "return_price": True,
                    "calc_style": "AVG_REL_PRICE",
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
                "libra_decay_signal": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }

        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["sig_a"] = avgt_signame
        # Again, the libra b side is the traded symbol.
        fixed_vals["top_level_signal"]["sig_b"] = libra_decay_signame

        fixed_vals["avg_trd"]["name"] = avgt_signame
        fixed_vals["avg_trd"]["ref_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame

        fixed_vals["libra_decay_signal"]["name"] = libra_decay_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "sig_a_selfscale": {
                        "name": "a_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "sig_b_selfscale": {
                        "name": "b_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "ab_elim_scale": {
                        "name": "ab_elim_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "ab_elim_diff_scale": {
                        "name": "ab_elim_diff_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.1,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.999,
                        "stepsize": 0.1,
                    },
                    "output_sigmoid_denom": {
                        "name": "output_sigmoid_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.001,
                        "stepsize": 0.5,
                        "chill_n_steps": 0,
                    },
                },
                "avg_trd": {
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 2000,
                        "stepsize": 0.5,
                    },
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class SigBookImpSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigBookImpulse.json"
        classname = "BookImp"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        ref_signame = out_signame + "_refpx"

        child_signames = [ref_signame]
        self.impl_tags = ["top_level_signal", "ref_px"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "sz_decay_type": "EXP_SIZE",
                    "min_tick_decay": 1,
                    "pred_decay": 0.98,
                    # Possibly include option to climb these too.
                    "bungee_mult_frac": 0,
                    "bungee_denom": 5,
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }
        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["ref_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 2000,
                        "stepsize": 0.5,
                    },
                    "px_decay": {
                        "name": "px_decay",
                        "proj_type": "EXP",
                        "proj_value": 0.001,
                        "min_proj": 1e-8,
                        "stepsize": 0.5,
                    },
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                    "time_behind_const": {
                        "name": "time_behind_const",
                        "proj_type": "EXP",
                        "proj_value": 0.5,
                        "stepsize": 0.5,
                    },
                    "pred_decay": {
                        "name": "pred_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.98,
                        "stepsize": 0.25,
                    },
                    "pred_decay_agree": {
                        "name": "pred_decay_agree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.90,
                        "stepsize": 0.25,
                    },
                    "pred_decay_denom": {
                        "name": "pred_decay_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "stepsize": 0.25,
                    },
                    "pred_boost_timeconst_s": {
                        "name": "pred_boost_timeconst_s",
                        "proj_type": "EXP",
                        "proj_value": 50,
                        "stepsize": 0.5,
                    },
                    "pred_boost_offset": {
                        "name": "pred_boost_offset",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.5,
                        "stepsize": 0.15,
                    },
                    "update_weight": {
                        "name": "update_weight",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.5,
                        "stepsize": 0.15,
                    },
                    "trade_weight": {
                        "name": "trade_weight",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.5,
                        "stepsize": 0.15,
                    },
                    "bungee_mult_frac": {
                        "name": "bungee_mult_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.5,
                        "max_shrink_steps": 4,
                        "max_expand_steps": 4,
                    },
                    "bungee_denom": {
                        "name": "bungee_denom",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.02,
                        "stepsize": 0.5,
                        "max_shrink_steps": 4,
                        "max_expand_steps": 4,
                    },

                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class SigDeepBkSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigDeepBook.json"
        classname = "DeepBook"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        child_signames = []

        # The tags are
        self.tmpl_tags = ["top_level_signal", "ref_px"]

        # Then, default fixed_vals
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "min_px_weight": 1e-10,
                    "max_pct_away": 0.02,
                    "insides_before_recalc": 30,
                    "ignore_recalc_thresh_mult": 0.7,
                    "sz_decay_type": "EXP_NOTIONAL",
                    # Only used for some types
                    "sz_decay_2": 10,
                    "bounding_val": 2,
                    "return_price": False,
                    "calc_style": "LOG_RATIO",
                    "use_flagged": SIGS_USE_FLAGGING,
                    "use_shadow": True,
                    "totstren_ema_bound": 8,
                }
            }

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)

        # Now, add in the names and markets.
        fixed_vals["top_level_signal"]["name"] = out_signame

        self.fixed_vals = fixed_vals

        # The climbed item's name -> {"sigformer_tag": "blah", "param_name": "blahblah"}
        self.climbable_inverted_map = {}

        # Add in the climbables.
        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "px_decay": {
                        "name": "px_decay",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "min_proj": 1e-8,
                        "stepsize": 0.5,
                    },
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "shadow_frac": {
                        "name": "shadow_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.25,
                    },
                    "shadow_decay_frac": {
                        "name": "shadow_decay_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.92,
                        "stepsize": 0.25,
                    },
                    "totstren_tdc_s": {
                        "name": "totstren_tdc_s",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.25,
                        "chill_n_steps": 2,
                    },
                    "totstren_ema_power": {
                        "name": "totstren_ema_power",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.25,
                    },
                },
            }
        # Create the inverted map.
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )

        self.fixed_val_overrides = fixed_val_overrides

# Works better(I think) with sigmoid, but can leave that to the mondo version.
class SigDeepBkDerSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "DeepBookDer.json"
        classname = "DeepBookDer"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        deepbk_signame = out_signame + "_deepbook"
        der_decay_signame = out_signame + "der_decay"

        child_signames = [deepbk_signame, der_decay_signame]
        self.impl_tags = ["top_level_signal", "deep_book", "ref_px", "der_decay_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "take_pred_returns": False,
                    "defer_decay": False,
                    "max_impulse": 0.03,
                    "max_bound_mult": 50,
                    "min_tick_decay": 0.9999,
                    "tgt_decay_denom": -1,
                    "output_sigmoid": False,
                    "output_sigmoid_denom": 0.01,
                },
                "deep_book": {
                    "symbol": symbol,
                    "books": markets,
                    "min_px_weight": 1e-10,
                    "max_pct_away": 0.02,
                    "insides_before_recalc": 30,
                    "ignore_recalc_thresh_mult": 0.7,
                    "sz_decay_type": "EXP_NOTIONAL",
                    "sz_decay_2": 10,
                    "bounding_val": 100.0,
                    "return_price": False,
                    "calc_style": "LOG_RATIO",
                    "use_flagged": SIGS_USE_FLAGGING,
                    "use_shadow": True,
                    "totstren_ema_bound": 8,
                },
                "der_decay_signal": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }
        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["pred_signal"] = deepbk_signame
        fixed_vals["top_level_signal"]["tgt_signal"] = der_decay_signame

        fixed_vals["deep_book"]["name"] = deepbk_signame

        fixed_vals["der_decay_signal"]["name"] = der_decay_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_agree": {
                        "name": "tgt_decay_agree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_disagree": {
                        "name": "tgt_decay_disagree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_denom": {
                        "name": "tgt_decay_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "stepsize": 0.25,
                    },
                },
                "deep_book": {
                    "px_decay": {
                        "name": "a_pxdecay",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "min_proj": 1e-8,
                        "stepsize": 0.5,
                    },
                    "sz_decay": {
                        "name": "a_szdecay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "shadow_frac": {
                        "name": "shadow_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.25,
                    },
                    "shadow_decay_frac": {
                        "name": "shadow_decay_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.92,
                        "stepsize": 0.25,
                    },
                    "totstren_tdc_s": {
                        "name": "totstren_tdc_s",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.25,
                        "chill_n_steps": 2,
                    },
                    "totstren_ema_power": {
                        "name": "totstren_ema_power",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.25,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class MondoSigDeepBkDerSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "DeepBookDer.json"
        classname = "MondoDeepBookDer"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        deepbk_signame = out_signame + "_deepbook"
        der_decay_signame = out_signame + "der_decay"

        child_signames = [deepbk_signame, der_decay_signame]
        self.impl_tags = ["top_level_signal", "deep_book", "ref_px", "der_decay_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "take_pred_returns": False,
                    "defer_decay": False,
                    "max_impulse": 0.03,
                    "max_bound_mult": 50,
                    "min_tick_decay": 0.9999,
                    "tgt_decay_denom": -1,
                    "output_sigmoid": True,
                    "output_sigmoid_denom": 0.01,
                },
                "deep_book": {
                    "symbol": symbol,
                    "books": markets,
                    "min_px_weight": 1e-10,
                    "max_pct_away": 0.02,
                    "insides_before_recalc": 30,
                    "ignore_recalc_thresh_mult": 0.7,
                    "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                    "sz_decay_2": 10,
                    "bounding_val": 100.0,
                    "return_price": False,
                    "calc_style": "LOG_RATIO",
                    "use_flagged": SIGS_USE_FLAGGING,
                    "use_shadow": True,
                    "totstren_ema_bound": 8,
                },
                "der_decay_signal": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }
        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["pred_signal"] = deepbk_signame
        fixed_vals["top_level_signal"]["tgt_signal"] = der_decay_signame

        fixed_vals["deep_book"]["name"] = deepbk_signame

        fixed_vals["der_decay_signal"]["name"] = der_decay_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_agree": {
                        "name": "tgt_decay_agree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_disagree": {
                        "name": "tgt_decay_disagree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_denom": {
                        "name": "tgt_decay_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "stepsize": 0.25,
                    },
                    "output_sigmoid_denom": {
                        "name": "output_sigmoid_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.03,
                        "stepsize": 0.5,
                        "chill_n_steps": 0,
                    },
                },
                "deep_book": {
                    "px_decay": {
                        "name": "a_pxdecay",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "min_proj": 1e-8,
                        "stepsize": 0.5,
                    },
                    "sz_decay": {
                        "name": "a_szdecay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "sz_decay_2": {
                        "name": "sz_decay_2",
                        "proj_type": "EXP",
                        "proj_value": 10,
                        "stepsize": 1,
                    },
                    "shadow_frac": {
                        "name": "shadow_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.25,
                    },
                    "shadow_decay_frac": {
                        "name": "shadow_decay_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.92,
                        "stepsize": 0.25,
                    },
                    "totstren_tdc_s": {
                        "name": "totstren_tdc_s",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.25,
                        "chill_n_steps": 2,
                    },
                    "totstren_ema_power": {
                        "name": "totstren_ema_power",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.25,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class SigDeepRetMidLibraSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "DeepBookRetMidLibra.json"
        classname = "DeepRetMidLibra"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        pred_signame = out_signame + "_preddeep"
        libra_decay_signame = out_signame + "_libra_decay"

        child_signames = [pred_signame, libra_decay_signame]

        # The tags are
        self.tmpl_tags = [
            "top_level_signal",
            "deepbook_predsym",
            "ref_px",
            "libra_decay_signal",
        ]

        # Then, default fixed_vals
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "max_impulse": 0.01,
                    "apply_basic_decay": False,
                    "sig_a_scale": 1,
                    "sig_b_scale": 0,
                    "ab_elim_diff_scale": 0,
                    "max_bound_mult": 30,
                    "output_sigmoid": True,
                    "output_sigmoid_denom": 0.001,
                },
                "deepbook_predsym": {
                    "symbol": symbol,
                    "books": markets,
                    "min_px_weight": 1e-10,
                    "max_pct_away": 0.02,
                    "insides_before_recalc": 30,
                    "ignore_recalc_thresh_mult": 0.7,
                    "sz_decay_type": "EXP_SIZE",
                    "sz_decay_2": 10,
                    "bounding_val": 100.0,
                    "return_price": True,
                    "calc_style": "LOG_RATIO",
                    "use_flagged": SIGS_USE_FLAGGING,
                    "use_shadow": True,
                    "totstren_ema_bound": 8,
                },
                "libra_decay_signal": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }

        for override_name, override_val in fixed_val_overrides.items():
            # Has to be the sigformer_tag.
            if override_name in fixed_vals:
                for param_name, param_val in override_val.items():
                    fixed_vals[override_name][param_name] = param_val
            else:
                print("Warning: could not find override name {}".format(override_name))

        # Now, add in the names and markets.
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["sig_a"] = pred_signame
        fixed_vals["top_level_signal"]["sig_b"] = libra_decay_signame

        fixed_vals["deepbook_predsym"]["name"] = pred_signame

        fixed_vals["libra_decay_signal"]["name"] = libra_decay_signame

        self.fixed_vals = fixed_vals

        # The climbed item's name -> {"sigformer_tag": "blah", "param_name": "blahblah"}
        self.climbable_inverted_map = {}

        # Add in the climbables.
        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "sig_a_selfscale": {
                        "name": "a_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                        "max_proj": 5,
                        "min_proj": 1.1,
                    },
                    "sig_b_selfscale": {
                        "name": "b_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                        "max_proj": 5,
                        "min_proj": 1.1,
                    },
                    "ab_elim_scale": {
                        "name": "ab_elim_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                        "max_proj": 10,
                        "min_proj": 0.1,
                    },
                    #"time_decay": {
                    #    "name": "time_decay",
                    #    "proj_type": "EXP",
                    #    "proj_value": 1000,
                    #    "stepsize": 0.1,
                    #},
                    #"tick_decay": {
                    #    "name": "tick_decay",
                    #    "proj_type": "LOGISTIC",
                    #    "proj_value": 0.999,
                    #    "stepsize": 0.1,
                    #},
                    "output_sigmoid_denom": {
                        "name": "output_sigmoid_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.001,
                        "stepsize": 0.5,
                        "chill_n_steps": 0,
                    },
                },
                "deepbook_predsym": {
                    "px_decay": {
                        "name": "a_pxdecay",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "min_proj": 1e-8,
                        "stepsize": 0.5,
                    },
                    "sz_decay": {
                        "name": "a_szdecay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "shadow_frac": {
                        "name": "shadow_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.25,
                    },
                    "shadow_decay_frac": {
                        "name": "shadow_decay_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.92,
                        "stepsize": 0.25,
                    },
                    "totstren_tdc_s": {
                        "name": "totstren_tdc_s",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.25,
                        "chill_n_steps": 2,
                    },
                    "totstren_ema_power": {
                        "name": "totstren_ema_power",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.25,
                    },
                },
            }
        # Create the inverted map.
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides




# This is the most involved version, so let me implement this first.
# This is basically a SigSymmetricMove...
# OK, and also. If we provide fixed_vals and climbables, then
# we better be sure we're set on EVERYTHING except for the signal names.
class SigDeepDeepRetLibraSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "DeepBookDeepRetLibra.json"
        classname = "DeepDeepRetLibra"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        pred_signame = out_signame + "_preddeep"
        tgt_signame = out_signame + "_tgtdeep"

        child_signames = [pred_signame, tgt_signame]

        # The tags are
        self.tmpl_tags = [
            "top_level_signal",
            "deepbook_predsym",
            "deepbook_tradesym",
        ]

        # Then, default fixed_vals
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "max_impulse": 0.01,
                    "apply_basic_decay": True,
                    #                "sig_a_scale": 1,
                    "sig_b_scale": 1,
                    "max_bound_mult": 30,
                },
                "deepbook_predsym": {
                    "symbol": symbol,
                    "books": markets,
                    "min_px_weight": 1e-10,
                    "max_pct_away": 0.02,
                    "insides_before_recalc": 30,
                    "ignore_recalc_thresh_mult": 0.7,
                    "sz_decay_type": "EXP_SIZE",
                    "sz_decay_2": 10,
                    "bounding_val": 10.0,
                    "return_price": True,
                    "calc_style": "LOG_RATIO",
                    "use_flagged": SIGS_USE_FLAGGING,
                    "use_shadow": True,
                },
                "deepbook_tgtsym": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "min_px_weight": 1e-10,
                    "max_pct_away": 0.02,
                    "insides_before_recalc": 30,
                    "ignore_recalc_thresh_mult": 0.7,
                    "sz_decay_type": "EXP_SIZE",
                    "sz_decay_2": 10,
                    "bounding_val": 10.0,
                    "return_price": True,
                    "calc_style": "LOG_RATIO",
                    "use_flagged": SIGS_USE_FLAGGING,
                    "use_shadow": True,
                },
            }

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)

        # Now, add in the names and markets.
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["sig_a"] = pred_signame
        fixed_vals["top_level_signal"]["sig_b"] = tgt_signame

        fixed_vals["deepbook_predsym"]["name"] = pred_signame
        fixed_vals["deepbook_tgtsym"]["name"] = tgt_signame

        # Add in the climbables.
        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "sig_a_scale": {
                        "name": "a_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "sig_a_selfscale": {
                        "name": "a_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "sig_b_selfscale": {
                        "name": "b_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "ab_elim_scale": {
                        "name": "ab_elim_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "ab_elim_diff_scale": {
                        "name": "ab_elim_diff_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.1,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.999,
                        "stepsize": 0.1,
                    },
                },
                "deepbook_predsym": {
                    "px_decay": {
                        "name": "a_pxdecay",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "min_proj": 1e-8,
                        "stepsize": 0.5,
                    },
                    "sz_decay": {
                        "name": "a_szdecay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "shadow_frac": {
                        "name": "a_shadow_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.25,
                    },
                    "shadow_decay_frac": {
                        "name": "a_shadow_decay_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.92,
                        "stepsize": 0.25,
                    },
                    "totstren_tdc_s": {
                        "name": "totstren_tdc_s",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.25,
                        "chill_n_steps": 2,
                    },
                    "totstren_ema_power": {
                        "name": "totstren_ema_power",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.25,
                    },
                },
                "deepbook_tgtsym": {
                    "px_decay": {
                        "name": "b_pxdecay",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "min_proj": 1e-8,
                        "stepsize": 0.5,
                    },
                    "sz_decay": {
                        "name": "b_szdecay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "shadow_frac": {
                        "name": "b_shadow_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.8,
                        "stepsize": 0.25,
                    },
                    "shadow_decay_frac": {
                        "name": "b_shadow_decay_frac",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.92,
                        "stepsize": 0.25,
                    },
                    "totstren_tdc_s": {
                        "name": "totstren_tdc_s",
                        "proj_type": "EXP",
                        "proj_value": 1000,
                        "stepsize": 0.25,
                        "chill_n_steps": 2,
                    },
                    "totstren_ema_power": {
                        "name": "totstren_ema_power",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.25,
                    },
                },
            }
        # Create the inverted map.
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# Classic LiqBal
class SigLiqBalSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "SigLiqBalance.json"
        classname = "LiqBal"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        ref_signame = out_signame + "_refpx"
        child_signames = [ref_signame]
        self.impl_tags = ["top_level_signal", "ref_px"]

        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "liq_type": "FIXED",
                    "calc_style": "DEEPWP1",
                    "sz_decay_type": "EXP_SIZE",
                    "min_sz_wt": 1e-12,
                    # This will need to just be searched over I guess.
                    # Maybe I should create an int searcher.
                    "return_price": False,
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }

        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["ref_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame


        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "per_level_decay": {
                        "name": "per_level_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.05,
                    },
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "levels_deep": {
                        "name": "levels_deep",
                        "proj_type": "EXP",
                        "proj_value": 5,
                        "stepsize": 1,
                        "max_proj": 12,
                        "min_proj": 2,
                        "max_shrink_steps": 1,
                        "is_int": True,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# x = SigLiqBalSpec("BTCUSDT", ["BinanceSPOT"],  "test", {}, {})
# print(json.dumps(x.get_sig_dict(), indent=2))


class SigTrdLiqBalSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "TrdLiqBal.json"
        classname = "TrdLiqBal"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        ref_signame = out_signame + "_refpx"
        child_signames = [ref_signame]
        self.impl_tags = ["top_level_signal", "ref_px"]

        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "symbol": symbol,
                    "books": markets,
                    "liq_type": "EMA_TRDS",
                    "return_price": False,
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }

        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["ref_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "ema_halflife": {
                        "name": "ema_halflife",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "ema_mult": {
                        "name": "ema_mult",
                        "proj_type": "EXP",
                        "proj_value": 50,
                        "stepsize": 0.5,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


class SigLiqBalDerSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "LiqBalDer.json"
        classname = "LiqBalDer"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        liqbal_signame = out_signame + "_liqbal"
        ref_signame = out_signame + "_refpx"
        der_decay_signame = out_signame + "_der_decay"

        child_signames = [liqbal_signame, ref_signame, der_decay_signame]
        self.impl_tags = ["top_level_signal", "liq_bal", "ref_px", "der_decay_signal"]
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "take_pred_returns": False,
                    "defer_decay": False,
                    "max_impulse": 0.03,
                    "max_bound_mult": 30,
                    "min_tick_decay": 0.9999,
                    "tgt_decay_denom": -1,
                    "output_sigmoid": True,
                    "output_sigmoid_denom": 0.01,
                },
                "liq_bal": {
                    "symbol": symbol,
                    "books": markets,
                    "liq_type": "FIXED",
                    "calc_style": "DEEPWP1",
                    "sz_decay_type": "EXP_SIZE",
                    "min_sz_wt": 1e-12,
                    "return_price": False,
                },
                "ref_px": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
                "der_decay_signal": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }
        # Do the climbables now.
        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["pred_signal"] = liqbal_signame
        fixed_vals["top_level_signal"]["tgt_signal"] = der_decay_signame

        fixed_vals["liq_bal"]["name"] = liqbal_signame
        fixed_vals["liq_bal"]["ref_signal"] = ref_signame

        fixed_vals["ref_px"]["name"] = ref_signame

        fixed_vals["der_decay_signal"]["name"] = der_decay_signame

        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "time_decay": {
                        "name": "time_decay",
                        "proj_type": "EXP",
                        "proj_value": 300,
                        "stepsize": 0.5,
                    },
                    "tick_decay": {
                        "name": "tick_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_agree": {
                        "name": "tgt_decay_agree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_disagree": {
                        "name": "tgt_decay_disagree",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.9,
                        "stepsize": 0.5,
                    },
                    "tgt_decay_denom": {
                        "name": "tgt_decay_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.0001,
                        "stepsize": 0.25,
                    },
                    "output_sigmoid_denom": {
                        "name": "output_sigmoid_denom",
                        "proj_type": "EXP",
                        "proj_value": 0.03,
                        "stepsize": 0.5,
                        "chill_n_steps": 0,
                    },
                },
                "liq_bal": {
                    "per_level_decay": {
                        "name": "per_level_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.05,
                    },
                    "sz_decay": {
                        "name": "sz_decay",
                        "proj_type": "EXP",
                        "proj_value": 20000,
                        "stepsize": 0.5,
                    },
                    "levels_deep": {
                        "name": "levels_deep",
                        "proj_type": "EXP",
                        "proj_value": 5,
                        "stepsize": 1,
                        "max_proj": 12,
                        "min_proj": 2,
                        "max_shrink_steps": 1,
                        "is_int": True,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# x = SigLiqBalDerSpec("BTCUSDT", ["BinanceSPOT"],  "test", {}, {})
# print(json.dumps(x.get_sig_dict(), indent=2))

# There's an opportunity to do LiqBalMidLibra too. But not now.


class SigLiqBalBalLibraSpec(SignalSpec):
    def __init__(
        self,
        name_prefix,
        symbol="",
        markets=[],
        fixed_val_overrides={},
        fixed_vals={},
        climbables={},
        extra_climbables={},
        traded_symbol="",
        traded_markets=[],
        class_suffix="",
    ):
        template_name = "LiqBalBalLibra.json"
        classname = "LiqBalBalLibra"
        out_signame = (
            name_prefix + "_{}_{}".format(traded_symbol, classname) + class_suffix
        )
        pred_signame = out_signame + "_predliq"
        tgt_signame = out_signame + "_tgtliq"

        pred_ref_signame = out_signame + "_pred_ref"
        tgt_ref_signame = out_signame + "_tgt_ref"

        child_signames = [pred_signame, tgt_signame, pred_ref_signame, tgt_ref_signame]

        # The tags are
        self.tmpl_tags = [
            "top_level_signal",
            "liqbal_predsym",
            "liqbal_tradesym",
            "a_refpx",
            "b_refpx",
        ]

        # Then, default fixed_vals
        if len(fixed_vals) == 0:
            fixed_vals = {
                "top_level_signal": {
                    "max_impulse": 0.01,
                    "apply_basic_decay": False,
                    "time_decay": 1e20,
                    "tick_decay": 1,
                    "max_bound_mult": 30,
                },
                "liqbal_predsym": {
                    "symbol": symbol,
                    "books": markets,
                    "liq_type": "FIXED",
                    "calc_style": "AVG_LIQ_PX",
                    "return_price": True,
                },
                "liqbal_tgtsym": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "liq_type": "FIXED",
                    "calc_style": "AVG_LIQ_PX",
                    "return_price": True,
                },
                "a_refpx": {
                    "symbol": symbol,
                    "books": markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
                "b_refpx": {
                    "symbol": traded_symbol,
                    "books": traded_markets,
                    "use_flagged": SIGS_USE_FLAGGING,
                },
            }

        fixed_vals = SignalSpec.override_fixed_vals(fixed_vals, fixed_val_overrides)

        # Now, add in the names and markets.
        fixed_vals["top_level_signal"]["name"] = out_signame
        fixed_vals["top_level_signal"]["sig_a"] = pred_signame
        fixed_vals["top_level_signal"]["sig_b"] = tgt_signame

        fixed_vals["liqbal_predsym"]["name"] = pred_signame
        fixed_vals["liqbal_tgtsym"]["name"] = tgt_signame

        fixed_vals["liqbal_predsym"]["ref_signal"] = pred_ref_signame
        fixed_vals["liqbal_tgtsym"]["ref_signal"] = tgt_ref_signame

        fixed_vals["a_refpx"]["name"] = pred_ref_signame
        fixed_vals["b_refpx"]["name"] = tgt_ref_signame

        # Add in the climbables.
        if len(climbables) == 0:
            climbables = {
                "top_level_signal": {
                    "sig_a_scale": {
                        "name": "a_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "sig_a_selfscale": {
                        "name": "a_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "sig_b_selfscale": {
                        "name": "b_selfscale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                    "ab_elim_scale": {
                        "name": "ab_elim_scale",
                        "proj_type": "EXP",
                        "proj_value": 1,
                        "stepsize": 0.1,
                    },
                },
                "liqbal_predsym": {
                    "liq_size": {
                        "name": "a_liq_size",
                        "proj_type": "EXP",
                        "proj_value": 2000,
                        "stepsize": 0.5,
                    },
                    "per_level_decay": {
                        "name": "per_level_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.05,
                    },
                },
                "liqbal_tgtsym": {
                    "liq_size": {
                        "name": "b_liq_size",
                        "proj_type": "EXP",
                        "proj_value": 2000,
                        "stepsize": 0.5,
                    },
                    "per_level_decay": {
                        "name": "per_level_decay",
                        "proj_type": "LOGISTIC",
                        "proj_value": 0.99,
                        "stepsize": 0.05,
                    },
                },
            }
        climbables = merge_extra_climbables(climbables, extra_climbables)

        SignalSpec.__init__(
            self,
            template_name,
            out_signame,
            child_signames,
            fixed_vals,
            climbables,
            classname,
            class_suffix,
            symbol=symbol,
            markets=markets,
            traded_symbol=traded_symbol,
            traded_markets=traded_markets,
        )
        self.fixed_val_overrides = fixed_val_overrides


# uses the same dict scheme as the multiclimb's caching scheme.
example_cache_dct = """
  "TrdImp__Self": {
    "classname": "TrdImp",
    "symbol": "APEUSDT",
    "markets": [
      "BinanceSPOT"
    ],
    "traded_symbol": "APEUSDT",
    "traded_markets": [
      "BinanceSPOT"
    ],
    "param_vals": {
      "sz_decay": 2000,
      "time_decay": 300,
      "tick_decay": 0.9939107340081471,
      "tgt_sig_decay": 0.9
    }
  },
"""


# Note: if I change this from setting fixed_val_overrides to fixed_vals,
# then I'll need to make sure the specified symbol, traded_symbol, market
# parts get updated properly too. Otherwise a bunch of stuff won't work.
def load_sigspec_from_cache(
    ss_dct,
    class_suffix,
    prefix="",
):
    ss_obj = None

    if ss_dct["classname"] == "Mid":
        ss_obj = SigMidSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "WMid":
        ss_obj = SigWMidSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "Linear":
        ss_obj = SigLinearSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "TrdImp":
        ss_obj = SigTrdImpSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "AvgTrd":
        ss_obj = SigAvgTrdSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "AvgTrdDer":
        ss_obj = SigAvgTrdDerSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "AvgTrdMidLibra":
        ss_obj = SigAvgTrdMidLibraSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "BookImp":
        ss_obj = SigBookImpSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "DeepBook":
        ss_obj = SigDeepBkSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "DeepBookDer":
        ss_obj = SigDeepBkDerSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "DeepRetMidLibra":
        ss_obj = SigDeepRetMidLibraSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "DeepRetBigMidLibra":
        # Sorry. This used to be a bigmidlibra but it can't.
        # It's the same now.
        ss_obj = SigDeepDeepRetLibraSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "DeepDeepRetLibra":
        ss_obj = SigDeepDeepRetLibraSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "MondoDeepBookDer":
        ss_obj = MondoSigDeepBkDerSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "LiqBal":
        ss_obj = SigLiqBalSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "LiqBalDer":
        ss_obj = SigLiqBalDerSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    elif ss_dct["classname"] == "LiqBalBalLibra":
        ss_obj = SigLiqBalBalLibraSpec(
            prefix,
            symbol=ss_dct["symbol"],
            markets=ss_dct["markets"],
            fixed_val_overrides=ss_dct.get("fixed_vals", {}),
            traded_symbol=ss_dct["traded_symbol"],
            traded_markets=ss_dct["traded_markets"],
            class_suffix=class_suffix,
        )
    # Probably throw an error here.

    ss_obj.set_varied_param_centers(ss_dct["param_vals"])
    return ss_obj


# OK, now let's test this out?
# ex_dct = {
#    "classname": "TrdImp",
#    "symbol": "APEUSDT",
#    "markets": [
#      "BinanceSPOT"
#    ],
#   "traded_symbol": "APEUSDT",
#   "traded_markets": [
#     "BinanceSPOT"
#   ],
#   "param_vals": {
#     "sz_decay": 2000,
#     "time_decay": 300,
#      "tick_decay": 0.9939107340081471,
#      "tgt_sig_decay": 0.9
#    }
#  }

# my_sigformer = load_sigspec_from_cache(ex_dct, "TrdImp__Self", prefix="yoo")

# Let's try to get the dict?
# print(my_sigformer.apply_varied_params({})["signals_dict"])


# x = SigLiqBalBalLibraSpec("BTCUSDT", "BTCUSDT", ["BinanceSPOT"], ["BinanceSPOT"],  "test", {}, {})
# print(json.dumps(x.get_sig_dict(), indent=2))


# x = SigMidSpec("DYDXUSDT",  ["BinanceSPOT"], "test", fixed_val_overrides={}, climbables={} )
# print(json.dumps(x.get_sig_dict(), indent=2))
# print(x.name)
