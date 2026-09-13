#! /usr/bin/env python

"""
Given multiple pktrade files, combine them into a single pktrader.

Example:

~/tradefi/pkt_0/pybin/PKatamari.py --confs pk_*.json --standardize

"""

import argparse
import json
import os
import re
from collections import defaultdict
import glob
import commentjson


def _suffix_from_symbol(traded_symbol):
    # Build a name-safe suffix from the traded symbol, e.g. "xyz:CL" -> "xyz_CL".
    # Non-alphanumeric chars (like the ":" namespace separator) become underscores.
    return re.sub(r"[^0-9A-Za-z]+", "_", traded_symbol).strip("_")


def _rewrite_refs(obj, name_map):
    """Recursively rewrite every string that references a renamed name.

    Walks dicts/lists and replaces any string value that exactly matches an old
    name with its new name. The "name" key is skipped because those are the
    definitions themselves (already renamed) -- rewriting them again could
    double-suffix if one signal's new name collides with another's old name.
    """
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key == "name":
                continue
            if isinstance(val, str):
                if val in name_map:
                    obj[key] = name_map[val]
            else:
                _rewrite_refs(val, name_map)
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            if isinstance(val, str):
                if val in name_map:
                    obj[i] = name_map[val]
            else:
                _rewrite_refs(val, name_map)


def disambiguate_names(pktrader):
    """Suffix every signal/tempo name in a pktrader with its traded_symbol.

    This is done uniformly (whether or not names actually collide) so that when
    multiple pktraders are combined into one file, their signals/tempos never
    clash. Both the *definitions* (signals[].name, tempos[].name) and every
    *reference* to them are rewritten via the same old->new mapping. References
    live in the ordex block (local_sig/remote_sig/relative_sig/trade_caller) and
    also inside other signal/tempo definitions (e.g. SigLiqBalance.ref_signal),
    possibly pointing at a name defined later -- so we build the full map first,
    then rewrite all references in a second pass.
    """

    suffix = _suffix_from_symbol(pktrader["traded_symbol"])

    # Pass 1: build the rename map and rename every signal/tempo definition.
    name_map = {}
    for section in ("signals", "tempos"):
        for entry in pktrader.get(section, []):
            old_name = entry.get("name")
            if old_name is None:
                continue
            name_map[old_name] = "{}_{}".format(old_name, suffix)
    for section in ("signals", "tempos"):
        for entry in pktrader.get(section, []):
            if entry.get("name") in name_map:
                entry["name"] = name_map[entry["name"]]

    # Pass 2: rewrite every reference to a renamed name, anywhere in the
    # pktrader (ordex params and cross-references between signals/tempos).
    _rewrite_refs(pktrader, name_map)

    return pktrader


def combine(pktrades_lst, out_path, standardize):

    # Go through each of them and
    settings_sec = None
    sim_sec = None

    # Defaults to [], collection of pktraders
    idx_to_sections = defaultdict(list)

    # Defaults to set(), collection of handled symbols in this idx.
    idx_to_handled_sym = defaultdict(set)

    for one_pk in pktrades_lst:

        if not os.path.exists(one_pk):
            print("Specified {} to combine but doesnt exist, skipping".format(one_pk))
            continue

        pk_dct = commentjson.loads(open(one_pk, "r").read())

        if settings_sec is None:
            settings_sec = pk_dct["settings"]

            if standardize:
                settings_sec["start_t"] = "09:00:00 America/New_York"
                settings_sec["end_t"] = "22:00:00 America/New_York"
        if sim_sec is None:
            sim_sec = pk_dct["simulation"]

        # Suffix each pktrader's signal/tempo names with its traded symbol so
        # combined files never have colliding signal names.
        for one_pktrader in pk_dct["pktraders"]:
            disambiguate_names(one_pktrader)

        traded_sym = pk_dct["pktraders"][0]["traded_symbol"]

        # Iterate through and figure out which bucket it should go into.
        cur_idx = 0
        handled = False

        for cur_idx in idx_to_handled_sym:
            if traded_sym not in idx_to_handled_sym[cur_idx]:
                handled = True
                break

        if not handled:
            cur_idx += 1

        # Now we have a target bucket.
        idx_to_sections[cur_idx] += pk_dct["pktraders"]
        idx_to_handled_sym[cur_idx].add(traded_sym)

    for idx in idx_to_sections:

        final_dct = {
            "settings": settings_sec,
            "simulation": sim_sec,
            "pktraders": idx_to_sections[idx],
        }

        if idx == 1:
            cur_out_path = out_path
        else:
            cur_out_path = "{}.{}".format(out_path, idx)

        # Write it out now.
        with open(cur_out_path, "w") as f:
            f.write(json.dumps(final_dct, indent=2))

        print("Done, wrote to {}".format(cur_out_path))


def main():

    parser = argparse.ArgumentParser()
    # glob for pktrade files.
    # It's literally that easy.
    # We grab the sim, start/stop times for
    parser.add_argument("--confs", nargs="+")
    # If given, assumes we're in a climb dir and looks through all the pnlclimbs for the glob label,
    # and then gathers the final_pk.json files.
    parser.add_argument("--glob", default=None)
    parser.add_argument("--out", default="combined_pk.json")
    # If we do startend, we will also grab the start and end times.
    parser.add_argument("--standardize", default=False, action="store_true")

    args = parser.parse_args()

    confs = args.confs
    if args.glob is not None:
        confs = glob.glob("*/pnlclimb*{}*/final_pk.json".format(args.glob))

    combine(confs, args.out, args.standardize)


if __name__ == "__main__":
    main()
