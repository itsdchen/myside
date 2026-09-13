#! /usr/bin/env python

import os
import json
import copy

import sys

x = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.append(x)

from util.pathing import templatedir


"""
Make some classes for easy creation, updating of tempo files. Used eventually
by TempoRate.

I guess, this is just much easier than the SigFormer.

"""

tempos_dir = os.path.join(templatedir(), "tempos")


types_to_files = {
    "exec": "ExecTempo.json",
    "file": "FileTempo.json",
    "final": "FinalTempo.json",
    "finalsupresstrades": "FinalSuppressTradesTempo.json",
    "impt": "ImptTempo.json",
    "inside": "InsideTempo.json",
    "msg": "MsgTempo.json",
    "multi": "MultiTempo.json",
    "time": "TimeTempo.json",
}

# Returns a string.
def make_tempo(tempo_type, replace_dict):
    tempo_path = os.path.join(tempos_dir, types_to_files[tempo_type])
    with open(tempo_path, "r") as f:
        tempo_dict = json.loads(f.read())

    # The way the current tempo files are set up,
    our_tempo_dict = tempo_dict["tempos"][0]

    for key, val in replace_dict.items():
        if key in our_tempo_dict:
            our_tempo_dict[key] = val

    # I think this should be ok, it should replace stuff by reference?
    return tempo_dict


def test_tempo(tempo_type, replace_dict):
    sturf = make_tempo(tempo_type, replace_dict)
    print(json.dumps(sturf, indent=2))
