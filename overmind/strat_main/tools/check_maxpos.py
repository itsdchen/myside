#!/usr/bin/env python3
"""
Check and fix mismatches between risk and ordex max-position/notional values
in pktrader config files.

For each pktrader entry:
  - If ordex.max_pos exists AND ordex.tgt_maxpos_notional does NOT exist,
    risk.max_position should match ordex.max_pos
  - If ordex.tgt_maxpos_notional exists, risk.max_notional should match it.
    Additionally, risk.max_position must be >= the implied max position
    (tgt_maxpos_notional / local_price), since the query will complain
    otherwise. If risk.max_position is too small, it is bumped to ceil(1.2x
    implied) to leave buffer for price drift (and rounded up to a whole number
    so the conf stays clean). The local price is fetched from Hyperliquid
    mainnet allMids for the traded symbol's dex.

Source of truth: ordex values. The fixed file updates risk to match ordex.

Usage:
    python check_maxpos.py [--inplace] <conf.json> [conf2.json ...]
"""

import argparse
import json
import math
import re
import sys

import commentjson
import requests


HL_MAINNET_INFO = "https://api.hyperliquid.xyz/info"

# Cache so we hit the API at most once per dex per script run.
_mids_cache = {}


def fetch_mid_price(traded_symbol):
    """Return the mainnet mid price (float) for a HL symbol like 'xyz:SKHX'.

    The dex is parsed from the traded_symbol prefix (everything before ':').
    Symbols without a ':' use no dex (the L1 perps universe)."""
    if ":" in traded_symbol:
        dex = traded_symbol.split(":", 1)[0]
    else:
        dex = ""

    if dex not in _mids_cache:
        payload = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
        r = requests.post(HL_MAINNET_INFO, json=payload, timeout=10,
                          headers={"Content-Type": "application/json"})
        r.raise_for_status()
        _mids_cache[dex] = {sym: float(px) for sym, px in r.json().items()}

    mids = _mids_cache[dex]
    if traded_symbol in mids:
        return mids[traded_symbol]
    # Fall back to suffix-only key in case the dex returns unprefixed names.
    if ":" in traded_symbol:
        suffix = traded_symbol.split(":", 1)[1]
        if suffix in mids:
            return mids[suffix]
    raise Exception(f"Could not find mid price for {traded_symbol} in allMids")



def replace_field_in_pktrader(text, traded_symbol, field, new_value_literal):
    """Replace `field`'s value within the first pktrader matching traded_symbol.

    Edits the raw text in-place so comments and unrelated formatting are
    preserved. `new_value_literal` must be a JSON literal string (e.g., '6000',
    '"6000"', '1.2'). Raises if the field can't be located."""
    sym_pat = re.escape(traded_symbol)
    field_pat = re.escape(field)
    # Match: "traded_symbol": "<sym>"  ...  "<field>": <value>
    # The value is either a JSON string literal or a scalar (number/bool/null).
    # `.*?` with DOTALL spans nested objects until the first occurrence of the
    # field within the pktrader block.
    pattern = (rf'("traded_symbol"\s*:\s*"{sym_pat}"'
               rf'.*?"{field_pat}"\s*:\s*)'
               r'("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|true|false|null)')
    new_text, count = re.subn(pattern, lambda m: m.group(1) + new_value_literal,
                              text, count=1, flags=re.DOTALL)
    if count != 1:
        raise Exception(
            f"Could not locate {field} for traded_symbol {traded_symbol!r} "
            f"to update in raw text")
    return new_text


def preserve_type(new_val, old_val):
    """Return new_val in the same type (str vs numeric) as old_val."""
    if isinstance(old_val, str):
        # Preserve string representation: use int-style if it's a whole number
        f = float(new_val)
        if f == int(f):
            return str(int(f))
        return str(f)
    else:
        # Preserve int vs float from the source
        if isinstance(new_val, str):
            new_val = float(new_val)
        if isinstance(new_val, float) and new_val == int(new_val) and isinstance(old_val, int):
            return int(new_val)
        return new_val


def check_file(filepath, inplace=False):
    with open(filepath, "r") as f:
        raw = f.read()

    data = commentjson.loads(raw)
    diffs = []
    edits = []  # list of (traded_symbol, field, new_typed_value) for raw-text edits

    pktraders = data.get("pktraders", [])

    for i, trader in enumerate(pktraders):
        risk = trader["risk"]
        ordex = trader["ordex"]
        symbol = trader["traded_symbol"]

        # Check max_pos -> max_position (only when tgt_maxpos_notional is absent)
        if "tgt_maxpos_notional" not in ordex:
            ordex_val = ordex["max_pos"]
            risk_val = risk.get("max_position")

            if risk_val is None:
                raise Exception("Did not find risk max_position in pktrade conf")
            else:
                try:
                    if float(ordex_val) != float(risk_val):
                        diffs.append({
                            "pktrader": symbol,
                            "field": "max_position",
                            "risk_val": risk_val,
                            "ordex_val": ordex_val,
                        })
                        edits.append((symbol, "max_position",
                                      preserve_type(ordex_val, risk_val)))
                except (ValueError, TypeError):
                    diffs.append({
                        "pktrader": symbol,
                        "field": "max_position",
                        "risk_val": risk_val,
                        "ordex_val": ordex_val,
                        "error": "could not compare",
                    })

        # Check tgt_maxpos_notional -> max_notional
        if "tgt_maxpos_notional" in ordex:
            ordex_val = ordex["tgt_maxpos_notional"]
            risk_val = risk.get("max_notional")

            if risk_val is None:
                raise Exception("Did not find risk max_notional in pktrade conf")
            else:
                try:
                    if float(ordex_val) != float(risk_val):
                        diffs.append({
                            "pktrader": symbol,
                            "field": "max_notional",
                            "risk_val": risk_val,
                            "ordex_val": ordex_val,
                        })
                        edits.append((symbol, "max_notional",
                                      preserve_type(ordex_val, risk_val)))
                except (ValueError, TypeError):
                    diffs.append({
                        "pktrader": symbol,
                        "field": "max_notional",
                        "risk_val": risk_val,
                        "ordex_val": ordex_val,
                        "error": "could not compare",
                    })

            # When tgt_maxpos_notional is set, the query also requires
            # risk.max_position >= tgt_maxpos_notional / local_price. Bump it
            # to 1.2x the implied value if it's too small, leaving headroom
            # for price drift.
            risk_pos = risk.get("max_position")
            if risk_pos is None:
                raise Exception("Did not find risk max_position in pktrade conf")
            try:
                local_px = fetch_mid_price(symbol)
                implied_pos = float(ordex_val) / local_px
                if float(risk_pos) < implied_pos:
                    new_max_pos = math.ceil(implied_pos * 1.2)
                    diffs.append({
                        "pktrader": symbol,
                        "field": "max_position",
                        "risk_val": risk_pos,
                        "ordex_val": (f"implied {implied_pos:.4f} "
                                      f"(={ordex_val}/{local_px:g})"),
                        "fixed_to": f"{new_max_pos} (ceil of 1.2x implied)",
                    })
                    edits.append((symbol, "max_position",
                                  preserve_type(new_max_pos, risk_pos)))
            except (ValueError, TypeError):
                diffs.append({
                    "pktrader": symbol,
                    "field": "max_position",
                    "risk_val": risk_pos,
                    "ordex_val": f"implied from {ordex_val}",
                    "error": "could not compare",
                })

    # Print results
    print(f"\n{'='*60}")
    print(f"File: {filepath}")
    print(f"{'='*60}")

    if not diffs:
        print("  OK - all risk values match ordex")
        return False

    for d in diffs:
        err = f"  [{d.get('error')}]" if "error" in d else ""
        fixed_to = d.get("fixed_to", d["ordex_val"])
        print(f"  MISMATCH in {d['pktrader']}:")
        print(f"    {d['field']}: risk={d['risk_val']}  ordex={d['ordex_val']}{err}")
        print(f"    -> fixing risk.{d['field']} to {fixed_to}")

    # Apply each edit as a targeted substitution on the raw text so comments
    # and formatting outside the touched values are preserved verbatim.
    new_text = raw
    for sym, field, new_val in edits:
        new_text = replace_field_in_pktrader(
            new_text, sym, field, json.dumps(new_val))

    if inplace:
        out_path = filepath
    else:
        out_path = filepath + ".fixed"
    with open(out_path, "w") as f:
        f.write(new_text)
    if inplace:
        print(f"\n  Fixed in-place: {out_path}")
    else:
        print(f"\n  Fixed file written to: {out_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Check/fix risk vs ordex maxpos mismatches")
    parser.add_argument("files", nargs="+", metavar="conf.json")
    parser.add_argument("--inplace", action="store_true",
                        help="Overwrite the original file instead of creating a .fixed copy")
    args = parser.parse_args()

    any_diffs = False
    for filepath in args.files:
        try:
            any_diffs |= check_file(filepath, inplace=args.inplace)
        except Exception as e:
            print(f"\nERROR processing {filepath}: {e}")
            any_diffs = True

    if any_diffs:
        sys.exit(1)
    else:
        print("\nAll files OK.")
        sys.exit(0)


if __name__ == "__main__":
    main()
