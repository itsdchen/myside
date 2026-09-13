#! /usr/bin/env python3

"""
pos_recon.py — reconcile account-held positions vs what strategies think they hold.

Pulls:
  - Account side: Hyperliquid clearinghouseState (szi + positionValue per coin).
    This is a read-only HTTP call, same as overmind/strat_main/tools/position_alerter.py.
  - Strategy side: Supabase `position` table (latest my_pos per (strat_id, symbol),
    summed across strats for each symbol). Rows are published every ~5min by the
    GlobalPositioner in src/pktrade/risk/global_positioner.cc.

Notional is computed with the HL mark price (positionValue / |szi|), so both sides
are valued at the same current mark. Prints a table sorted by |diff notional| desc.

Usage:
    pos_recon.py                                  # mainnet, default creds
    pos_recon.py --testnet
    pos_recon.py --hl-creds ~/.creds/.Hyperliquid.l1g.creds.json
    pos_recon.py --by-strat                       # also show per-strat breakdown
"""

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)
sys.path.insert(0, os.path.join(script_dir, "..", "overmind", "strat_main"))
from util import sbcreds  # noqa: E402

MAINNET_INFO = "https://api.hyperliquid.xyz/info"
TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"

DEFAULT_LOOKBACK_MIN = 15   # max age of strat position rows we'll consider "live"
DEFAULT_BREAK_USD = 100.0   # flag reconciliation breaks >= this USD
DEFAULT_ROW_LIMIT = 5000    # cap on rows pulled from supabase (newest first)


def _parse_pg_ts(ts):
    """Parse a Postgres/Supabase timestamp. Fractional seconds can have any number
    of digits; datetime.fromisoformat only accepts 0/3/6. Normalise by padding or
    truncating the fractional part."""
    ts = ts.replace("Z", "+00:00")
    if "." in ts:
        head, tail = ts.split(".", 1)
        tz_idx = max(tail.find("+"), tail.find("-"))
        if tz_idx == -1:
            frac, tz = tail, ""
        else:
            frac, tz = tail[:tz_idx], tail[tz_idx:]
        frac = (frac + "000000")[:6]
        ts = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(ts)


def fetch_account_positions(creds_path, is_testnet, dex):
    """Returns (sym -> {szi, pos_val_signed, mark_px}, address, marginSummary)."""
    with open(os.path.expanduser(creds_path)) as f:
        hl_creds = json.load(f)
    # Preferred in order: explicit listen_address, subaccount_address, address,
    # then derive from secret_key (same precedence as toucannon.py).
    address = (hl_creds.get("listen_address")
               or hl_creds.get("subaccount_address")
               or hl_creds.get("address"))
    if not address:
        secret = hl_creds.get("secret_key")
        if not secret:
            sys.exit(f"Creds file {creds_path} has no address/subaccount_address/"
                     f"listen_address and no secret_key to derive one from.")
        from eth_account import Account
        address = Account.from_key(secret).address
    address = address.lower()

    endpoint = TESTNET_INFO if is_testnet else MAINNET_INFO
    payload = {"type": "clearinghouseState", "user": address, "dex": dex}
    r = requests.post(endpoint, json=payload, timeout=10,
                      headers={"Content-Type": "application/json"})
    r.raise_for_status()
    data = r.json()

    out = {}
    for ap in data.get("assetPositions", []):
        p = ap["position"]
        coin = p["coin"]
        szi = float(p["szi"])
        # HL returns positionValue as magnitude; sign it by szi.
        pos_val = abs(float(p["positionValue"]))
        pos_val_signed = pos_val if szi >= 0 else -pos_val
        mark_px = (pos_val / abs(szi)) if szi != 0 else None
        out[coin] = {
            "szi": szi,
            "pos_val": pos_val_signed,
            "mark_px": mark_px,
        }
    return out, address, data.get("marginSummary", {})


def fetch_all_mids(is_testnet, dex):
    """Returns sym -> mid price (float). Used as a fallback mark when the account
    has no position in a symbol that strats are carrying."""
    endpoint = TESTNET_INFO if is_testnet else MAINNET_INFO
    payload = {"type": "allMids", "dex": dex}
    try:
        r = requests.post(endpoint, json=payload, timeout=10,
                          headers={"Content-Type": "application/json"})
        r.raise_for_status()
        return {sym: float(px) for sym, px in r.json().items()}
    except Exception as e:
        print(f"(warning) allMids fetch failed: {e}", file=sys.stderr)
        return {}


def fetch_strategy_positions(sb_creds_path, is_testnet, lookback_min, row_limit):
    """Returns sym -> list of dicts with strat_id, my_pos, global_pos, created_at.
    Keeps only the most recent row per (strat_id, symbol).

    Supabase times out on wide created_at filters; instead we pull the newest
    `row_limit` rows by id and filter in-process by the lookback window."""
    creds = sbcreds.SupabaseCredentials(is_testnet, sb_creds_path)
    params = {
        "select": "strat_id,symbol,my_pos,global_pos,created_at",
        "order": "id.desc",
        "limit": str(row_limit),
    }
    r = creds.get("position", params)
    if r.status_code != 200:
        sys.exit(f"Supabase position query failed ({r.status_code}): {r.text}")
    rows = r.json()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
    rows = [row for row in rows if _parse_pg_ts(row["created_at"]) >= cutoff]

    # rows are ordered id.desc → first occurrence of (strat_id, sym) is the newest.
    latest = {}
    for row in rows:
        key = (row["strat_id"], row["symbol"])
        if key not in latest:
            latest[key] = row

    by_sym = {}
    for (strat_id, sym), row in latest.items():
        by_sym.setdefault(sym, []).append({
            "strat_id": strat_id,
            "my_pos": float(row["my_pos"]),
            "global_pos": float(row["global_pos"]),
            "created_at": row["created_at"],
        })
    return by_sym


def build_rows(account, strats, all_mids):
    all_syms = set(account) | set(strats)
    rows = []
    for sym in all_syms:
        acct = account.get(sym, {})
        acct_szi = acct.get("szi", 0.0)
        acct_val = acct.get("pos_val", 0.0)

        # Prefer positionValue-derived mark (authoritative); fall back to allMids.
        mark_px = acct.get("mark_px")
        mark_src = "pos" if mark_px is not None else None
        if mark_px is None and sym in all_mids:
            mark_px = all_mids[sym]
            mark_src = "mid"

        strat_list = strats.get(sym, [])
        strat_qty = sum(r["my_pos"] for r in strat_list)

        if mark_px is not None:
            strat_val = strat_qty * mark_px
            diff_val = (acct_szi - strat_qty) * mark_px
        else:
            strat_val = 0.0
            diff_val = 0.0

        rows.append({
            "sym": sym,
            "acct_qty": acct_szi,
            "acct_val": acct_val,
            "strat_qty": strat_qty,
            "strat_val": strat_val,
            "diff_qty": acct_szi - strat_qty,
            "diff_val": diff_val,
            "mark_px": mark_px,
            "n_strats": len(strat_list),
            "mark_src": mark_src,
        })
    rows.sort(key=lambda r: -abs(r["diff_val"]))
    return rows


def print_table(rows, break_thresh_usd, show_zero):
    hdr = (f"{'symbol':<22} {'acct_qty':>12} {'acct_$':>12} "
           f"{'strat_qty':>12} {'strat_$':>12} "
           f"{'diff_qty':>12} {'diff_$':>14} {'mark':>10} {'src':>4} {'#s':>3}")
    print(hdr)
    print("-" * len(hdr))

    tot_acct = tot_strat = tot_diff = 0.0
    for r in rows:
        if not show_zero and r["acct_qty"] == 0 and r["strat_qty"] == 0:
            continue
        flag = " !!" if abs(r["diff_val"]) >= break_thresh_usd else "   "
        mark_str = f"{r['mark_px']:.4f}" if r["mark_px"] is not None else "-"
        src = r["mark_src"] or "-"
        print(f"{r['sym']:<22} {r['acct_qty']:>12.4f} {r['acct_val']:>12.2f} "
              f"{r['strat_qty']:>12.4f} {r['strat_val']:>12.2f} "
              f"{r['diff_qty']:>12.4f} {r['diff_val']:>+14.2f}{flag} "
              f"{mark_str:>10} {src:>4} {r['n_strats']:>3}")
        tot_acct += r["acct_val"]
        tot_strat += r["strat_val"]
        tot_diff += r["diff_val"]

    print("-" * len(hdr))
    print(f"{'TOTAL':<22} {'':>12} {tot_acct:>12.2f} "
          f"{'':>12} {tot_strat:>12.2f} "
          f"{'':>12} {tot_diff:>+14.2f}")
    if any(r["mark_src"] is None for r in rows):
        print("\nsrc legend: pos=from positionValue (authoritative),  "
              "mid=from allMids (fallback),  -=no mark available (notional=0)")


def print_by_strat(rows_by_sym, strats):
    print("\n== By strategy ==")
    for sym in sorted(strats):
        strat_list = strats[sym]
        if not strat_list:
            continue
        total = sum(r["my_pos"] for r in strat_list)
        print(f"  {sym}  (sum my_pos = {total:+.4f})")
        for r in sorted(strat_list, key=lambda x: x["strat_id"]):
            print(f"    {r['strat_id']:<45} my_pos={r['my_pos']:>+12.4f}  "
                  f"global_pos={r['global_pos']:>+12.4f}  "
                  f"(as of {r['created_at']})")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    default_hl = f"/home/{getpass.getuser()}/.creds/.Hyperliquid.creds.json"
    parser.add_argument("--hl-creds", default=default_hl,
                        help=f"Hyperliquid creds file (default: {default_hl})")
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--dex", default="xyz", help="HL dex (default: xyz)")
    parser.add_argument("--lookback-min", type=int, default=DEFAULT_LOOKBACK_MIN,
                        help=f"Only consider strat rows newer than this "
                             f"(default: {DEFAULT_LOOKBACK_MIN} min)")
    parser.add_argument("--break-usd", type=float, default=DEFAULT_BREAK_USD,
                        help=f"Flag breaks >= this USD notional "
                             f"(default: ${DEFAULT_BREAK_USD:.0f})")
    parser.add_argument("--show-zero", action="store_true",
                        help="Include rows where both sides are zero")
    parser.add_argument("--row-limit", type=int, default=DEFAULT_ROW_LIMIT,
                        help=f"Max rows to pull from supabase, newest first "
                             f"(default: {DEFAULT_ROW_LIMIT})")
    parser.add_argument("--by-strat", action="store_true",
                        help="Also print per-strategy breakdown of strat positions")
    sbcreds.add_sb_creds_arg(parser)
    args = parser.parse_args()

    sb_creds_path = args.sb_creds
    if args.vault and "vault" not in sb_creds_path:
        sb_creds_path = sb_creds_path.replace(".Supabase", ".Supabase.vault")

    account, address, margin = fetch_account_positions(
        args.hl_creds, args.testnet, args.dex)
    strats = fetch_strategy_positions(sb_creds_path, args.testnet, args.lookback_min,
                                       args.row_limit)
    # Only fetch allMids if there are strat-side symbols not covered by account.
    need_mids = set(strats) - set(account)
    all_mids = fetch_all_mids(args.testnet, args.dex) if need_mids else {}

    env = "TESTNET" if args.testnet else "MAINNET"
    print(f"\n== Position reconciliation ==")
    print(f"addr {address}   env {env}   dex {args.dex}   "
          f"strat lookback {args.lookback_min}m   "
          f"break>=${args.break_usd:.0f}")
    if margin:
        av = margin.get("accountValue", "-")
        tnp = margin.get("totalNtlPos", "-")
        print(f"account_value={av}  total_notional_pos={tnp}")
    print()

    rows = build_rows(account, strats, all_mids)
    print_table(rows, args.break_usd, args.show_zero)

    if args.by_strat:
        print_by_strat(rows, strats)


if __name__ == "__main__":
    main()
