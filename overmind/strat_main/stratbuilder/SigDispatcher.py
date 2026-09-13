#! /usr/bin/env python

import os
import sys
import json
import copy

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from templatizers.SigFormer import *

import random
import math

"""
    A very basic arrangement. Given a traded symbol and side products,
    Give a basic pool of what *could* be considered at round n of the climb.

    Things like chill_out_rules should be decided by the person writing the multiclimb,
    not the dispatcher.
"""


random.seed(math.e)


class SigDispatcher:
    def __init__(
        self,
        traded_sym,
        traded_markets,
        side_products_lst,
        num_rounds,
        sigs_per_round=15,
    ):
        self.traded_sym = traded_sym
        self.traded_markets = traded_markets
        self.side_products_lst = side_products_lst
        self.num_rounds = num_rounds

        # Maybe randomly sample
        self.sigs_per_round = sigs_per_round

    # class SigTrdImpSpec(SignalSpec):

    # class SigBookImpSpec(SignalSpec):
    # class SigDeepBkSpec(SignalSpec):
    # class SigDeepBkDerSpec(SignalSpec):
    # class SigDeepRetMidLibraSpec(SignalSpec):
    # class SigDeepDeepRetLibraSpec(SignalSpec):
    # class SigLiqBalSpec(SignalSpec):
    # class SigTrdLiqBalSpec(SignalSpec):
    # class SigLiqBalDerSpec(SignalSpec):
    # class SigLiqBalBalLibraSpec(SignalSpec):

    def dispatch(self, dispatch_style):
        sigs_per_round_v1 = {}

        if dispatch_style == "basic":
            sigs_per_round_v1 = self.basic_dispatch()
        elif dispatch_style == "slow_rampup":
            sigs_per_round_v1 = self.slow_rampup()
        elif dispatch_style == "small":
            sigs_per_round_v1 = self.small_dispatch()
        elif dispatch_style == "dense_rampup_rr":
            sigs_per_round_v1 = self.dense_roundrobin_dispatch()
        elif dispatch_style == "rampup_rr":
            sigs_per_round_v1 = self.roundrobin_dispatch()
        elif dispatch_style == "smallrampup_rr":
            sigs_per_round_v1 = self.smallroundrobin_dispatch()
        elif dispatch_style == "smallsmallrampup_rr":
            sigs_per_round_v1 = self.smallsmallroundrobin_dispatch()
        elif dispatch_style == "farming_rr":
            self.sigs_per_round = 12
            # A smaller set of dispatch requirements for farming sigs.
            sigs_per_round_v1 = self.farming_rr()

        sigs_per_round_final = {}
        # Go through and resample/shuffle all of these
        for round, sigs_lst in sigs_per_round_v1.items():
            # Use shuffle instead of sample because
            random.shuffle(sigs_lst)
            sigs_per_round_final[round] = sigs_lst[: self.sigs_per_round]
        return sigs_per_round_final

    def small_dispatch(self):
        to_ret = {}

        for x in range(0, self.num_rounds):
            cur_round_feats = []

            if True:
                # Add in the rest of the gang.
                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(x),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )
                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(x),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # Append side product datas.
                for one_side_prod_dct in self.side_products_lst:
                    cur_round_feats.append(
                        SigTrdImpSpec(
                            "round{}".format(x),
                            symbol=one_side_prod_dct["symbol"],
                            markets=one_side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                one_side_prod_dct["symbol"],
                                "_".join(one_side_prod_dct["markets"]),
                            ),
                        )
                    )
                    cur_round_feats.append(
                        SigBookImpSpec(
                            "round{}".format(x),
                            symbol=one_side_prod_dct["symbol"],
                            markets=one_side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                one_side_prod_dct["symbol"],
                                "_".join(one_side_prod_dct["markets"]),
                            ),
                        )
                    )
            to_ret[x] = cur_round_feats
        return to_ret

    # Pretty simple dispatcher. But basically on every round after the first, it
    # just goes through a different
    def roundrobin_dispatch(self):
        to_ret = {}

        # Let's make all of these available.
        # TO start, climb very basic things.
        rampup_cutoff = 2

        for round in range(0, self.num_rounds):
            cur_round_feats = []
            if round < rampup_cutoff:
                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )
                # CAT
                cur_round_feats.append(
                    SigAvgTrdSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )
                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                # rm noshadow variants for efficiencies.
                # cur_round_feats.append(
                #    SigDeepBkSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_noshadow_Self",
                #        fixed_val_overrides={
                #            "top_level_signal": {
                #                "use_shadow": False,
                #            },
                #        },
                #    )
                # )

                # Poor man's liqbal
                cur_round_feats.append(
                    SigLiqBalSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigLiqBalDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "deep_book": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "deep_book": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepRetMidLibraSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

            else:
                # Add in the rest of the gang, but round-robinned.

                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )

                # CAT
                cur_round_feats.append(
                    SigAvgTrdSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # cur_round_feats.append(
                #    SigDeepBkSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_noshadow_Self",
                #        fixed_val_overrides={
                #            "top_level_signal": {
                #                "use_shadow": False,
                #            },
                #        },
                #    )
                # )

                cur_round_feats.append(
                    SigLiqBalDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepRetMidLibraSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # Append side product datas.
                # Let's try to do two side_products at a time.
                # I guess I'll just start w/ this and look at offsets.
                density = 3
                if len(self.side_products_lst) == 1:
                    density = 1
                # Don't do this foolishness if no side products.
                if len(self.side_products_lst) == 0:
                    continue
                # Not useful if no side products
                for i in range(density):
                    offset = i * len(self.side_products_lst) // density
                    side_prod_dct = self.side_products_lst[
                        (round + offset) % len(self.side_products_lst)
                    ]

                    cur_round_feats.append(
                        SigTrdImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_v2feats_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # )

                    cur_round_feats.append(
                        SigAvgTrdDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_px_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "take_pred_returns": True,
                                },
                                "avg_trd": {
                                    "return_price": True,
                                },
                            },
                        )
                    )

                    cur_round_feats.append(
                        SigBookImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    cur_round_feats.append(
                        SigLiqBalDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    cur_round_feats.append(
                        SigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # vomktpxder
                    cur_round_feats.append(
                        SigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_px_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "max_impulse": 0.01,
                                    "take_pred_returns": True,
                                },
                                "deep_book": {
                                    "sz_decay_type": "EXP_SIZE",
                                    "calc_style": "LOG_RATIO",
                                    "return_price": True,
                                },
                            },
                        )
                    )

                    cur_round_feats.append(
                        MondoSigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

            to_ret[round] = cur_round_feats
        return to_ret

    # Like roundrobin, but tries to be a bit smaller. Maybe opting for speed versus
    # coverage.
    def smallroundrobin_dispatch(self):
        to_ret = {}

        prelim_round_to_sig = {}
        sigs_per_round = 15

        # Let's make all of these available.
        # TO start, climb very basic things.
        rampup_cutoff = 2

        for round in range(0, self.num_rounds):
            cur_round_feats = []
            if round < rampup_cutoff:
                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )
                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

            else:
                # Add in the rest of the gang, but round-robinned.
                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )

                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # Append side product datas.
                # Let's try to do two side_products at a time.
                # I guess I'll just start w/ this and look at offsets.
                density = 3
                if len(self.side_products_lst) == 1:
                    density = 1
                # Don't do this foolishness if no side products.
                if len(self.side_products_lst) == 0:
                    # Still add in signals if no side products! 
                    to_ret[round] = cur_round_feats
                    continue
                # Not useful if no side products
                for i in range(density):
                    offset = i * len(self.side_products_lst) // density
                    side_prod_dct = self.side_products_lst[
                        (round + offset) % len(self.side_products_lst)
                    ]

                    cur_round_feats.append(
                        SigTrdImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_v2feats_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # )

                    cur_round_feats.append(
                        SigAvgTrdDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_px_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "take_pred_returns": True,
                                },
                                "avg_trd": {
                                    "return_price": True,
                                },
                            },
                        )
                    )

                    cur_round_feats.append(
                        SigBookImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # vomktpxder
                    cur_round_feats.append(
                        SigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_px_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "max_impulse": 0.01,
                                    "take_pred_returns": True,
                                },
                                "deep_book": {
                                    "sz_decay_type": "EXP_SIZE",
                                    "calc_style": "LOG_RATIO",
                                    "return_price": True,
                                },
                            },
                        )
                    )

            prelim_round_to_sig[round] = cur_round_feats

        # Finally, subsample prelim_round_to_sig to form the to_ret dict
        for one_round, list_of_sigs in prelim_round_to_sig.items():
            if len(list_of_sigs) < sigs_per_round:
                subset_sigs = list_of_sigs
            else:
                subset_sigs = random.sample(list_of_sigs, sigs_per_round)
            to_ret[one_round] = subset_sigs

        return to_ret

    def smallsmallroundrobin_dispatch(self):
        to_ret = {}

        prelim_round_to_sig = {}
        sigs_per_round = 15

        # Let's make all of these available.
        # TO start, climb very basic things.
        rampup_cutoff = 2

        for round in range(0, self.num_rounds):
            cur_round_feats = []
            if round < rampup_cutoff:
                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )
                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

            else:
                # Add in the rest of the gang, but round-robinned.

                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )

                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )
                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                # Append side product datas.
                # Let's try to do two side_products at a time.
                # I guess I'll just start w/ this and look at offsets.
                density = 3
                if len(self.side_products_lst) == 1:
                    density = 1
                # Don't do this foolishness if no side products.
                if len(self.side_products_lst) == 0:
                    continue
                # Not useful if no side products
                for i in range(density):
                    offset = i * len(self.side_products_lst) // density
                    side_prod_dct = self.side_products_lst[
                        (round + offset) % len(self.side_products_lst)
                    ]

                    cur_round_feats.append(
                        SigTrdImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_v2feats_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # )

                    # catpxder
                    cur_round_feats.append(
                        SigBookImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # vomktpxder
                    cur_round_feats.append(
                        SigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_px_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "max_impulse": 0.01,
                                    "take_pred_returns": True,
                                },
                                "deep_book": {
                                    "sz_decay_type": "EXP_SIZE",
                                    "calc_style": "LOG_RATIO",
                                    "return_price": True,
                                },
                            },
                        )
                    )

            prelim_round_to_sig[round] = cur_round_feats

        # Finally, subsample prelim_round_to_sig to form the to_ret dict
        for one_round, list_of_sigs in prelim_round_to_sig.items():
            subset_sigs = random.sample(list_of_sigs, sigs_per_round)
            to_ret[one_round] = subset_sigs
        return to_ret

    # Pretty simple dispatcher. But basically on every round after the first, it
    # just goes through a different
    def dense_roundrobin_dispatch(self):
        to_ret = {}

        prelim_round_to_sig = {}
        sigs_per_round = 15
        # Let's make all of these available.
        # TO start, climb very basic things.
        rampup_cutoff = 2

        for round in range(0, self.num_rounds):
            cur_round_feats = []
            if round < rampup_cutoff:
                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )
                # CAT
                cur_round_feats.append(
                    SigAvgTrdSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )
                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                # rm noshadow variants for efficiencies.
                # cur_round_feats.append(
                #    SigDeepBkSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_noshadow_Self",
                #        fixed_val_overrides={
                #            "top_level_signal": {
                #                "use_shadow": False,
                #            },
                #        },
                #    )
                # )

                # Poor man's liqbal
                cur_round_feats.append(
                    SigLiqBalSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigLiqBalDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "deep_book": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "deep_book": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepRetMidLibraSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

            else:
                # Add in the rest of the gang, but round-robinned.

                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )

                # CAT
                cur_round_feats.append(
                    SigAvgTrdSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # cur_round_feats.append(
                #    SigDeepBkSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_noshadow_Self",
                #        fixed_val_overrides={
                #            "top_level_signal": {
                #                "use_shadow": False,
                #            },
                #        },
                #    )
                # )

                cur_round_feats.append(
                    SigLiqBalDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepRetMidLibraSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # Not useful if no side products
                for side_prod_dct in self.side_products_lst:

                    cur_round_feats.append(
                        SigTrdImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_v2feats_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # )

                    cur_round_feats.append(
                        SigAvgTrdDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_px_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "take_pred_returns": True,
                                },
                                "avg_trd": {
                                    "return_price": True,
                                },
                            },
                        )
                    )

                    cur_round_feats.append(
                        SigBookImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    cur_round_feats.append(
                        SigLiqBalDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    cur_round_feats.append(
                        SigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # vomktpxder
                    cur_round_feats.append(
                        SigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_px_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "max_impulse": 0.01,
                                    "take_pred_returns": True,
                                },
                                "deep_book": {
                                    "sz_decay_type": "EXP_SIZE",
                                    "calc_style": "LOG_RATIO",
                                    "return_price": True,
                                },
                            },
                        )
                    )

                    cur_round_feats.append(
                        MondoSigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

            prelim_round_to_sig[round] = cur_round_feats

        # Finally, subsample prelim_round_to_sig to form the to_ret dict
        for one_round, list_of_sigs in prelim_round_to_sig.items():
            subset_sigs = random.sample(list_of_sigs, sigs_per_round)
            to_ret[one_round] = subset_sigs
        return to_ret

    # Gonna restrict it to a small # of sigs per round. Something like 8
    def farming_rr(self):
        to_ret = {}

        # Let's make all of these available.
        # TO start, climb very basic things.
        rampup_cutoff = 2

        for round in range(0, self.num_rounds):
            cur_round_feats = []
            if round < rampup_cutoff:
                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )
                # CAT
                cur_round_feats.append(
                    SigAvgTrdSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )
                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )
        
                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_notandsize_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                            },
                        },
                        extra_climbables={
                            "top_level_signal": {
                                "sz_decay_2": {
                                    "name": "sz_decay_2",
                                    "proj_type": "EXP",
                                    "proj_value": 10,
                                    "stepsize": 1,
                                },
                            }
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                #cur_round_feats.append(
                #    SigDeepBkDerSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_notandsize_Self",
                #        fixed_val_overrides={
                #            "deep_book": {
                #                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                #            },
                #        },
                #        extra_climbables={
                #            "deep_book": {
                #                "sz_decay_2": {
                #                    "name": "sz_decay_2",
                #                    "proj_type": "EXP",
                #                    "proj_value": 10,
                #                    "stepsize": 1,
                #                },
                #            }
                #        },
                #    )
                #)

                # vomktpxder
                #cur_round_feats.append(
                #    SigDeepBkDerSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_px_Self",
                #        fixed_val_overrides={
                #            "top_level_signal": {
                #                "max_impulse": 0.01,
                #                "take_pred_returns": True,
                #            },
                #            "deep_book": {
                #                "sz_decay_type": "EXP_SIZE",
                #                "calc_style": "LOG_RATIO",
                #                "return_price": True,
                #            },
                #        },
                #    )
                #)

                cur_round_feats.append(
                    SigDeepRetMidLibraSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    MondoSigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

            else:
                # Add in the rest of the gang, but round-robinned.

                cur_round_feats.append(
                    SigTrdImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_v2feats_Self",
                    )
                )

                # CAT
                cur_round_feats.append(
                    SigAvgTrdSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )
                cur_round_feats.append(
                    SigAvgTrdDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "take_pred_returns": True,
                            },
                            "avg_trd": {
                                "return_price": True,
                            },
                        },
                    )
                )
                cur_round_feats.append(
                    SigAvgTrdMidLibraSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                    )
                )

                cur_round_feats.append(
                    SigBookImpSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigDeepBkSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                cur_round_feats.append(
                    SigLiqBalDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                #cur_round_feats.append(
                #    SigDeepBkSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_notandsize_Self",
                #        fixed_val_overrides={
                #            "top_level_signal": {
                #                "sz_decay_type": "EXP_NOTIONAL_AND_SIZE",
                #            },
                #        },
                #        extra_climbables={
                #            "top_level_signal": {
                #                "sz_decay_2": {
                #                    "name": "sz_decay_2",
                #                    "proj_type": "EXP",
                #                    "proj_value": 10,
                #                    "stepsize": 1,
                #                },
                #            }
                #        },
                #    )
                #)

                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                # vomktpxder
                cur_round_feats.append(
                    SigDeepBkDerSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_px_Self",
                        fixed_val_overrides={
                            "top_level_signal": {
                                "max_impulse": 0.01,
                                "take_pred_returns": True,
                            },
                            "deep_book": {
                                "sz_decay_type": "EXP_SIZE",
                                "calc_style": "LOG_RATIO",
                                "return_price": True,
                            },
                        },
                    )
                )

                cur_round_feats.append(
                    SigDeepRetMidLibraSpec(
                        "round{}".format(round),
                        symbol=self.traded_sym,
                        markets=self.traded_markets,
                        traded_symbol=self.traded_sym,
                        traded_markets=self.traded_markets,
                        class_suffix="_Self",
                    )
                )

                #cur_round_feats.append(
                #    MondoSigDeepBkDerSpec(
                #        "round{}".format(round),
                #        symbol=self.traded_sym,
                #        markets=self.traded_markets,
                #        traded_symbol=self.traded_sym,
                #        traded_markets=self.traded_markets,
                #        class_suffix="_Self",
                #    )
                #)

                # Append side product datas.
                # Not useful if no side products
                for i in range(len(self.side_products_lst)):
                    side_prod_dct = self.side_products_lst[
                        i
                    ]

                    cur_round_feats.append(
                        SigTrdImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_v2feats_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    # )

                    cur_round_feats.append(
                        SigAvgTrdDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_px_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                            fixed_val_overrides={
                                "top_level_signal": {
                                    "take_pred_returns": True,
                                },
                                "avg_trd": {
                                    "return_price": True,
                                },
                            },
                        )
                    )

                    cur_round_feats.append(
                        SigAvgTrdMidLibraSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_px_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    cur_round_feats.append(
                        SigBookImpSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )
                    #cur_round_feats.append(
                    #    SigDeepBkDerSpec(
                    #        "round{}".format(round),
                    #        symbol=side_prod_dct["symbol"],
                    #        markets=side_prod_dct["markets"],
                    #        traded_symbol=self.traded_sym,
                    #        traded_markets=self.traded_markets,
                    #        class_suffix="_Side{}_Mkts{}".format(
                    #            side_prod_dct["symbol"],
                    #            "_".join(side_prod_dct["markets"]),
                    #        ),
                    #    )
                    #)

                    # vomktpxder
                    cur_round_feats.append(
                        SigDeepRetMidLibraSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

                    cur_round_feats.append(
                        MondoSigDeepBkDerSpec(
                            "round{}".format(round),
                            symbol=side_prod_dct["symbol"],
                            markets=side_prod_dct["markets"],
                            traded_symbol=self.traded_sym,
                            traded_markets=self.traded_markets,
                            class_suffix="_Side{}_Mkts{}".format(
                                side_prod_dct["symbol"],
                                "_".join(side_prod_dct["markets"]),
                            ),
                        )
                    )

            to_ret[round] = cur_round_feats

        return to_ret
