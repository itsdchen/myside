#! /usr/bin/env python

import os
import json
import copy
import sys

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from util.pathing import templatedir

"""

Constructs a PKTrader config from signals and ordexes.
This is not going to be as fancy as some of those other things, you guys.


"""

pkt_template = os.path.join(templatedir(), "trader")


# Note.
# Each one of these inputs should be the "value" in the key-val
# pair. So for signal_settings, just give it the list of signals, don't
# give it a dict like {"signals": [list_of_signals]}.
# It's not necessary, but it's helpful so we can give the mapping,
# ordex : ordex_setting in the below section.
class PKSpec:
    def __init__(
        self,
        traded_symbol,
        risk_setting,
        ordex_setting,
        signal_setting,
        tempo_setting,
        enabled=True,
        size_mult=1,
        ins_dates_str="",
        oos_dates_str="",
        ins_stats={},
        oos_stats={},
        global_settings={},
        sim_settings={},
        extra_subs=[]
    ):
        self.template_name = "pktrade.json"
        self.template_path = os.path.join(pkt_template, self.template_name)

        with open(self.template_path, "r") as f:
            self.base_setting = json.loads(f.read())

        # Just recording
        self.base_setting["ins_dates"] = ins_dates_str
        self.base_setting["oos_dates"] = oos_dates_str
        self.base_setting["ins_stats"] = ins_stats
        self.base_setting["oos_stats"] = oos_stats

        # TODO: impelment the rest of this shit, yeah?
        # If the settings are not-empty, update the base_setting.
        if len(global_settings) > 0:
            self.base_setting["settings"] = global_settings

        if len(sim_settings) > 0:
            self.base_setting["simulation"] = sim_settings

        self.base_setting["pktraders"] = [
            {
                "traded_symbol": traded_symbol,
                "enabled": enabled,
                "size_mult": size_mult,
                "risk": risk_setting,
                "ordex": ordex_setting,
                "signals": signal_setting,
                "tempos": tempo_setting,
                "extra_subs": extra_subs
            }
        ]

    def set_externals(self, ordex_setting={}, signal_setting={}):
        new_setting = copy.deepcopy(self.base_setting)

        if len(ordex_setting) > 0:
            new_setting["pktraders"][0]["ordex"] = ordex_setting
        if len(signal_setting) > 0:
            new_setting["pktraders"][0]["signals"] = signal_setting
        return new_setting

    # Keep the settings and simulation sections.
    # Given a list of different pktrader confs, combine the first two sections and
    def combine_pktraders(list_of_pktraders):
        pass
