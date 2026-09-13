#! /usr/bin/env python

"""
Earnings de-risk actuator.

Reads the upcoming-earnings CSV (produced by nasdaqearnings.py and scp'd over to the
trading host) plus a sector map, works out which symbols to de-risk *today*, and sends
SET_TL_REASON usermsgs with reason=Derisk to ramp their trade level down ahead of an
after-market-close (AMC) earnings release.

This replaces the old manual, per-earnings crontab editing: a fixed set of cron lines
calls this script at each ramp step (15:00 TL1, 15:30 TL2, 15:50 TL3, 16:00 TL5 ET), and
the script itself decides which symbols are affected. It no-ops on days with no AMC
earnings.

Sector contagion: when a symbol that belongs to a sector reports, every member of that
sector is de-risked too -- memory/semis peers tend to move violently on each other's
prints. Sectors are independent (a memory print de-risks only the memory list). Sector
members include US-hours ETFs and foreign names that don't report earnings of their own
but catch sympathy moves (see sector_map.json).

Re-enable is deliberately manual (the operator watches the move and decides when it has
stabilised). Use --reenable to clear the Derisk level for today's affected set, or
--reenable --sym xyz:MU to clear a single symbol's sector set as each one settles.

Examples:
    # ramp steps (driven by cron):
    earnings_derisk.py --tl 1
    earnings_derisk.py --tl 5

    # see what would be sent, send nothing:
    earnings_derisk.py --tl 1 --dry-run

    # manual re-enable once the move has stabilised:
    earnings_derisk.py --reenable                 # all of today's affected set
    earnings_derisk.py --reenable --sym xyz:MU    # just MU's sector set
"""

import argparse
import datetime
import getpass
import json
import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, ".."))

# Reuse the usermsg sender (same dir) and the shared Supabase creds helper. usermsg also
# sets up sys.path for `from util import ...` on both dev and the deployed host.
import usermsg
from util import sbcreds

# Default locations on the trading host. ace scp's next_earnings.csv here; the operator
# scp's sector_map.json here. Both overridable for testing.
DEFAULT_EARNINGS_DIR = "/home/ubuntu/estrader/earnings"

AMC = "time-after-hours"      # after market close -- the only timing we act on
BMO = "time-pre-market"       # before market open -- heads-up only, never de-risked here
UNKNOWN = "time-not-supplied"

REASON = "Derisk"


# ---------------------------------------------------------------------------
# Symbol mapping. CSV tickers are bare (e.g. "MU"); the sector map and usermsg --sym use
# the xyz: HIP3 form (e.g. "xyz:MU"). Usually it's just "xyz:" + ticker, but not always
# (e.g. PURR -> xyz:PURRDAT), so consult symbolizer when it's importable. symbolizer pulls
# in chron/pytz and builds large tables, which may not be available on the trading host;
# fall back to the simple prefix rule in that case.
# ---------------------------------------------------------------------------
_remote_to_xyz = {}
try:
    from util import symbolizer
    for _xyz, _meta in symbolizer.relwide_remote_wiring.items():
        _rs = _meta.get("remote_symbol")
        if _rs:
            _remote_to_xyz.setdefault(_rs.upper(), _xyz)
except Exception as e:  # pragma: no cover - environment dependent
    print(f"WARN: could not import symbolizer ({e}); "
          f"falling back to 'xyz:'+ticker mapping")


def ticker_to_xyz(ticker):
    """Map a bare CSV ticker (e.g. 'MU') to its xyz: trading symbol (e.g. 'xyz:MU')."""
    t = ticker.strip().upper()
    return _remote_to_xyz.get(t, "xyz:" + t)


def normalize_xyz(sym):
    """Accept 'MU' or 'xyz:MU' and return the canonical 'xyz:MU' form."""
    s = sym.strip()
    if s.lower().startswith("xyz:"):
        return "xyz:" + s.split(":", 1)[1].upper()
    return ticker_to_xyz(s)


# ---------------------------------------------------------------------------
# Loading inputs
# ---------------------------------------------------------------------------
def load_sector_map(path):
    """Load sector -> set(xyz syms). Keys starting with '_' (e.g. _comment) are ignored."""
    with open(path) as f:
        raw = json.load(f)
    sector_map = {}
    for sector, members in raw.items():
        if sector.startswith("_"):
            continue
        sector_map[sector] = {normalize_xyz(m) for m in members}
    return sector_map


def parse_earnings_csv(path):
    """Parse 'date,ticker,tod' rows. Returns list of (date_str, ticker, tod)."""
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) != 3:
                print(f"WARN: skipping malformed earnings row: {line!r}")
                continue
            date_str, ticker, tod = (p.strip() for p in parts)
            rows.append((date_str, ticker.upper(), tod))
    return rows


# ---------------------------------------------------------------------------
# Affected-set computation
# ---------------------------------------------------------------------------
def sectors_containing(xyz, sector_map):
    return [s for s, members in sector_map.items() if xyz in members]


def affected_for_date(rows, on_date, sector_map):
    """Compute the set of xyz: symbols to de-risk for an AMC earnings date.

    Returns {xyz_sym: set(reason_notes)}. A reporting symbol is always included
    ("reporting"); if it belongs to a sector, every member of that sector is included with
    a "<reporter>:<sector>" note.
    """
    affected = {}

    def add(sym, note):
        affected.setdefault(sym, set()).add(note)

    for date_str, ticker, tod in rows:
        if date_str != on_date or tod != AMC:
            continue
        xyz = ticker_to_xyz(ticker)
        add(xyz, "reporting")
        for sector in sectors_containing(xyz, sector_map):
            for member in sector_map[sector]:
                if member != xyz:
                    add(member, f"{xyz}:{sector}")
    return affected


def sector_set_for_symbol(xyz, sector_map):
    """The de-risk set for a single symbol: its sector peers, or just itself."""
    members = {xyz}
    for sector in sectors_containing(xyz, sector_map):
        members |= sector_map[sector]
    return members


# ---------------------------------------------------------------------------
# Actuation
# ---------------------------------------------------------------------------
def send_tl(sym, tl, *, testnet, user, strat, creds_path, dry_run):
    """Send a single SET_TL_REASON reason=Derisk usermsg (or print it under --dry-run)."""
    um_args = {"tl": str(tl), "reason": REASON}
    if dry_run:
        print(f"(dry-run) usermsg.py --strat {strat!r} --sym {sym} "
              f"--um SET_TL_REASON --args tl:{tl},reason:{REASON}"
              + (" --testnet" if testnet else ""))
        return
    usermsg.sendUM(testnet, user, strat, sym, "SET_TL_REASON", dict(um_args), creds_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tl", type=int,
                      help="Trade level to set for today's affected set (ramp step)")
    mode.add_argument("--reenable", action="store_true",
                      help="Clear the Derisk level (TL0) for today's affected set, "
                           "or for --sym's sector set")
    parser.add_argument("--sym",
                        help="With --reenable, restrict to this symbol's sector set "
                             "(accepts 'MU' or 'xyz:MU')")
    parser.add_argument("--date",
                        help="Override 'today' as YYYY-MM-DD (for testing). "
                             "Defaults to the local date (host runs on ET).")
    parser.add_argument("--earnings-dir", default=DEFAULT_EARNINGS_DIR,
                        help="Directory holding next_earnings.csv and sector_map.json")
    parser.add_argument("--earnings-csv",
                        help="Override path to next_earnings.csv")
    parser.add_argument("--sector-map",
                        help="Override path to sector_map.json")
    parser.add_argument("--strat", default=".*",
                        help="strat_id regex to target (default: all)")
    parser.add_argument("--user", default=getpass.getuser(), help="User sending um")
    parser.add_argument("--testnet", action="store_true",
                        help="Send to testnet instead of mainnet")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without sending")
    sbcreds.add_sb_creds_arg(parser)
    args = parser.parse_args()

    if args.tl is not None and args.sym:
        parser.error("--sym is only valid with --reenable")

    creds_path = args.sb_creds
    if args.vault and "vault" not in creds_path:
        creds_path = creds_path.replace(".Supabase", ".Supabase.vault")

    csv_path = args.earnings_csv or os.path.join(args.earnings_dir, "next_earnings.csv")
    map_path = args.sector_map or os.path.join(args.earnings_dir, "sector_map.json")
    today = args.date or datetime.date.today().isoformat()

    sector_map = load_sector_map(map_path)

    # Determine the de-risk set and target level.
    if args.reenable and args.sym:
        target_tl = 0
        syms = sector_set_for_symbol(normalize_xyz(args.sym), sector_map)
        notes = {s: {"reenable"} for s in syms}
    else:
        target_tl = 0 if args.reenable else args.tl
        rows = parse_earnings_csv(csv_path)
        notes = affected_for_date(rows, today, sector_map)
        syms = set(notes)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    action = "re-enable (TL0)" if args.reenable else f"TL{target_tl}"
    if not syms:
        print(f"[{stamp}] earnings_derisk: no AMC earnings for {today}; nothing to {action}.")
        return

    ordered = sorted(syms)
    summary = ", ".join(f"{s} ({','.join(sorted(notes[s]))})" for s in ordered)
    print(f"[{stamp}] earnings_derisk {action} for {today}: {summary}")

    for sym in ordered:
        send_tl(sym, target_tl, testnet=args.testnet, user=args.user,
                strat=args.strat, creds_path=creds_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
