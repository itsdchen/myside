#! /usr/bin/env python

import argparse
import getpass
import json
import os
import re
import requests
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, ".."))
if os.path.basename(script_dir) == "pybin":
    sys.path.append(os.path.join(script_dir, "..", "overmind", "strat_main"))
from util import sbcreds

DEBUG = False

# This must match UserMsgId in basic_enums.h
UM_ID_MAP = {
    "SET_PLACE_THRESH"   : 301,
    "SET_THRESH_MULT"    : 302,
    "SET_ORDER_SIZE"     : 303,
    "SET_SIZE_MULT"      : 304,
    "SET_RELATIVE_BETA"  : 305,
    "SET_EXTRA_THRESHES" : 306,
    "SET_CONST_PRED_PX"  : 307,
    "FORCE_INHERIT"      : 402,
    "SET_TL"             : 403,
    "FORCE_INHERIT_LOOP" : 404,
    "SET_TL_REASON"      : 405
}
# TODO: Add check that each UM is given the appropriate args

def toNumberIfPossible(val):
    try:
        return int(val)
    except ValueError: # not an int, maybe a float
        try:
            return float(val)
        except ValueError: # not a float either, just a string
            return val

def sendUM(testnet, user, strat, sym, um, um_args, credsfile):
    # Validate the strat regex before sending. pktrade compiles this field into
    # a std::regex on receipt; an invalid pattern (e.g. an unbalanced paren)
    # would make pktrade throw std::regex_error and core dump. Catch it here so
    # the bad usermsg never leaves the sender. Python's re is not identical to
    # std::regex (ECMAScript), but it catches the common structural errors
    # (unbalanced (), [], etc.) that break both.
    try:
        re.compile(strat)
    except re.error as e:
        sys.exit(f"Error: invalid strat regex '{strat}': {e}")

    # Construct json row to write to supabase
    um_dict = {
        "user"           : user,
        "strat_id_regex" : strat,
        "um_id"          : UM_ID_MAP[um]
    }
    if sym:
        um_dict["sym"] = sym

    # If args are numbers (ints or floats), remove the quotes
    if um_args:
        for key, val in um_args.items():
            um_args[key] = toNumberIfPossible(val)
        um_dict["args"] = um_args

    # Get creds for Supabase using proper authorization for writes
    if DEBUG:
        sbcreds.DEBUG = True
    creds = sbcreds.SupabaseCredentials(testnet, credsfile, True)

    if DEBUG:
        sys.exit(f"(DEBUG) would write usermsg row: {um_dict}")

    # Write row to Supabase
    r = creds.post("usermsg", um_dict)
    if r.status_code == 201:
        print("Success: sent usermsg", um_dict)
    else:
        print("Error writing row to Supabase: {}".format(um_dict))
        print("Response ({}): {}".format(r.status_code, r.text))
        sys.exit(1)

def main():
    class ArgFormat(argparse.RawDescriptionHelpFormatter,
                    argparse.ArgumentDefaultsHelpFormatter):
        pass
    parser = argparse.ArgumentParser(
        description="Script to send a usermsg via Supabase.",
        formatter_class=ArgFormat,
        epilog="""
example usage:
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_PLACE_THRESH --args thresh:0.001
    usermsg.py --strat ".*" --um SET_MULT_THRESH --args mult:1.5
    usermsg.py --strat strat[123] --um SOME_OTHER_UM --args arg1:0.5,foo:bar

per-um:
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_PLACE_THRESH --args thresh:0.001
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_THRESH_MULT --args mult:1
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_ORDER_SIZE --args size:2
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_SIZE_MULT --args mult:1.5
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_RELATIVE_BETA --args beta:0.14
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_EXTRA_THRESHES --args buy:0,sell:0.0001
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_CONST_PRED_PX --args px:200
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um FORCE_INHERIT --args pos:-20.1
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_TL --args tl:1
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um FORCE_INHERIT_LOOP
    usermsg.py --strat strat1 --sym xyz:XYZ100 --um SET_TL_REASON --args tl:0,reason:Usermsg
    """
    )
    parser.add_argument("--testnet", action="store_true",
                        help="Send to testnet instead of mainnet table in Supabase")
    parser.add_argument("--user", default=getpass.getuser(), help="User sending um")
    parser.add_argument("--strat", required=True, help="Regex for strat_id")
    parser.add_argument("--sym", help="Symbol, or unspecified for all symbols")
    parser.add_argument("--um", required=True, help="um_id name, e.g., SET_PLACE_THRESH")
    parser.add_argument("--args", help="Args for usermsg, specified as key:val,key:val")
    sbcreds.add_sb_creds_arg(parser)
    args = parser.parse_args()

    creds_path = args.sb_creds
    if args.vault and "vault" not in creds_path:
        creds_path = creds_path.replace(".Supabase", ".Supabase.vault")

    um_args = None
    if args.args:
        um_args = {key: val for key, val in [pair.split(':') for pair in args.args.split(',')]}

    sendUM(args.testnet, args.user, args.strat, args.sym, args.um, um_args, creds_path)

if __name__ == "__main__":
    main()
