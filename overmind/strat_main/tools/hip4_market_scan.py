#!/usr/bin/env python
"""Scan live HIP-4 outcomes on a Hyperliquid deployer venue and, for sports outcomes, match
them to the corresponding Kalshi (and best-effort Polymarket) market, then compare books.

For each matched market it reports, per venue, the top-of-book spread and the NOTIONAL
(price * size) resting within the top N levels near the mid -- NOT raw contract counts, which
are dominated by deep "stink" bids that mirror across the YES/NO duality (a huge bid at ~0
shows up as huge size at ~1). It also cross-checks orientation: HL YES == the reference's YES,
confirmed by the two mids agreeing.

This is read-only market data -- no auth, no orders.

Example usage:
    # Scan all txyz outcomes, match sports to Kalshi, compare books (top 10 levels):
    python hip4_market_scan.py

    # Only NFL, only outcomes whose HL book has >= $500 notional near the mid, 5 levels:
    python hip4_market_scan.py --competition NFL --min-hl-notional 500 --levels 5

    # Also try Polymarket (best-effort matching):
    python hip4_market_scan.py --polymarket

Arguments:
    --venue-name STR      HIP-4 deployer venue to scan (default: txyz).
    --competition STR     Only outcomes whose description competition == this (e.g. NFL, NCAAF).
    --levels N            Book levels to sum notional over, near the mid (default: 10).
    --min-hl-notional F   Skip outcomes whose HL near-mid bid+ask notional is below this ($).
    --polymarket          Additionally attempt a (best-effort) Polymarket match.
    --mainnet/--testnet   Which Hyperliquid to hit (default: mainnet).
    --json                Emit machine-readable JSON instead of the table.
"""

import argparse
import json
import sys

import requests

HL_MAINNET = "https://api.hyperliquid.xyz/info"
HL_TESTNET = "https://api.hyperliquid-testnet.xyz/info"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"

# HL competition -> Kalshi series ticker. Extend as more sports are supported. Outcomes whose
# competition isn't here are still listed, but reported as "no reference matcher".
KALSHI_SERIES = {
    "NFL": "KXNFLGAME",
    "NCAAF": "KXNCAAFGAME",
}


# --------------------------------------------------------------------------------------------
# Hyperliquid
# --------------------------------------------------------------------------------------------

def hl_outcome_meta(base):
    return requests.post(base, json={"type": "outcomeMeta"}, timeout=20).json()


def parse_desc(desc):
    """A HIP-4 description is pipe-delimited key:value pairs -> dict."""
    out = {}
    for part in (desc or "").split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k] = v
    return out


def outcome_coin(outcome_id, side):
    """Order/book coin symbol for a HIP-4 outcome side (0=YES, 1=NO)."""
    return "#{}".format(10 * int(outcome_id) + side)


def hl_book(base, coin):
    """(bids, asks) as lists of (price, size) in canonical YES terms, best-first."""
    lv = requests.post(base, json={"type": "l2Book", "coin": coin}, timeout=10).json()["levels"]
    bids = [(float(l["px"]), float(l["sz"])) for l in lv[0]]  # already desc by price
    asks = [(float(l["px"]), float(l["sz"])) for l in lv[1]]  # already asc by price
    return bids, asks


# --------------------------------------------------------------------------------------------
# Kalshi
# --------------------------------------------------------------------------------------------

def kalshi_open_markets(series):
    """All open markets for a Kalshi series, as {ticker: yes_sub_title}."""
    out = {}
    cursor = None
    for _ in range(20):  # page defensively
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI}/markets", params=params, timeout=20)
        if r.status_code != 200:
            break
        j = r.json()
        for m in j.get("markets", []):
            out[m["ticker"]] = m.get("yes_sub_title", "")
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def kalshi_book(ticker):
    """Canonical YES (bids, asks) for a Kalshi market from orderbook_fp.

    orderbook_fp.yes_dollars = YES bids [price, size]; no_dollars = NO bids. A NO bid at price p
    is a YES offer at (1 - p), so the YES asks are {(1-p, size)}. Sizes are contract counts.
    """
    ob = requests.get(f"{KALSHI}/markets/{ticker}/orderbook", timeout=10).json().get(
        "orderbook_fp", {})
    yd = [(float(p), float(s)) for p, s in (ob.get("yes_dollars") or [])]
    nd = [(float(p), float(s)) for p, s in (ob.get("no_dollars") or [])]
    bids = sorted(yd, reverse=True)             # best YES bid first
    asks = sorted((1.0 - p, s) for p, s in nd)  # best (lowest) YES ask first
    return bids, asks


def _norm(s):
    return " ".join((s or "").lower().replace(".", "").split())


def kalshi_team_matches(yes_sub_title, participant):
    """True if a Kalshi yes_sub_title (city + optional team initial, e.g. 'Los Angeles R')
    refers to the same team as an HL participant full name (e.g. 'Los Angeles Rams').

    Match rule: the participant's city (all but the last word) must be a prefix of the Kalshi
    title, and whatever remains of the Kalshi title after the city must be a prefix of the
    participant's team word (disambiguates Rams vs Chargers, Jets vs Giants).
    """
    kt = _norm(yes_sub_title)
    parts = _norm(participant).split()
    if not kt or len(parts) < 2:
        return kt and kt in _norm(participant)
    city, team = " ".join(parts[:-1]), parts[-1]
    if not kt.startswith(city):
        return False
    remainder = kt[len(city):].strip()
    return remainder == "" or team.startswith(remainder)


def match_kalshi(series, participant_a, participant_b):
    """Find the Kalshi market whose YES == participant_a for the a-vs-b matchup.

    Returns (ticker, orientation) where orientation is 'aligned' (Kalshi YES == HL YES),
    'flipped' (only the participant_b market matched -> Kalshi YES is the other team), or
    (None, reason) if nothing matched.
    """
    markets = kalshi_open_markets(series)
    a_hit = [tk for tk, yt in markets.items() if kalshi_team_matches(yt, participant_a)]
    b_hit = [tk for tk, yt in markets.items() if kalshi_team_matches(yt, participant_b)]
    # Prefer a market whose sibling (same matchup code) is the other team, to avoid coincidences.
    def matchup_code(tk):
        # KXNFLGAME-26SEP20SEAARI-SEA -> "26SEP20SEAARI"
        segs = tk.split("-")
        return segs[1] if len(segs) >= 3 else tk
    for tk in a_hit:
        code = matchup_code(tk)
        if any(matchup_code(bt) == code for bt in b_hit):
            return tk, "aligned"
    if a_hit:
        return a_hit[0], "aligned"           # matched YES team but couldn't confirm sibling
    if b_hit:
        return b_hit[0], "flipped"           # only the other side matched
    return None, "no kalshi market"


# --------------------------------------------------------------------------------------------
# Polymarket (best-effort)
# --------------------------------------------------------------------------------------------

def match_polymarket(participant_a, participant_b):
    """Best-effort: search Polymarket for an event mentioning both teams, return a CLOB token id
    for the participant_a ("YES") side, or None. Polymarket's API is loosely structured, so this
    is intentionally conservative and may return None even when a market exists."""
    try:
        r = requests.get(f"{POLY_GAMMA}/events",
                         params={"search": f"{participant_a} {participant_b}", "closed": "false",
                                 "limit": 10}, timeout=15)
        for ev in (r.json() if r.status_code == 200 else []):
            title = _norm(ev.get("title", ""))
            if _norm(participant_a).split()[-1] in title and _norm(participant_b).split()[-1] in title:
                for mk in ev.get("markets", []):
                    outcomes = mk.get("outcomes")
                    tokens = mk.get("clobTokenIds")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(tokens, str):
                        tokens = json.loads(tokens)
                    if outcomes and tokens:
                        for oc, tok in zip(outcomes, tokens):
                            if _norm(participant_a).split()[-1] in _norm(oc):
                                return tok
    except Exception:
        pass
    return None


def poly_book(token_id):
    try:
        d = requests.get(f"{POLY_CLOB}/book", params={"token_id": token_id}, timeout=10).json()
        bids = sorted(((float(b["price"]), float(b["size"])) for b in d.get("bids", [])),
                      reverse=True)
        asks = sorted((float(a["price"]), float(a["size"])) for a in d.get("asks", []))
        return bids, asks
    except Exception:
        return [], []


# --------------------------------------------------------------------------------------------
# Book stats + reporting
# --------------------------------------------------------------------------------------------

def book_stats(bids, asks, levels, band_frac):
    """(best_bid, best_ask, spread, bid_notional, ask_notional).

    Notional is summed only over levels that are BOTH within the first `levels` AND within
    `band_frac` of the mid (price band = band_frac * mid). This bounds how far into the book we
    reach, so a far-out "stink" level -- e.g. a huge NO-side bid at ~0 that mirrors, through the
    YES/NO duality, into a huge YES ask at ~1 -- can't leak into the near-mid notional.
    """
    bb = bids[0][0] if bids else 0.0
    ba = asks[0][0] if asks else 1.0
    mid = (bb + ba) / 2.0
    band = band_frac * mid
    bid_ntl = sum(p * s for i, (p, s) in enumerate(bids) if i < levels and (mid - p) <= band)
    ask_ntl = sum(p * s for i, (p, s) in enumerate(asks) if i < levels and (p - mid) <= band)
    return bb, ba, (ba - bb), bid_ntl, ask_ntl


def scan(args):
    base = HL_TESTNET if args.testnet else HL_MAINNET
    meta = hl_outcome_meta(base)
    outcomes = [o for o in meta.get("outcomes", []) if o.get("venue") == args.venue_name]

    rows = []
    for o in outcomes:
        d = parse_desc(o.get("description", ""))
        comp = d.get("competition", "")
        if args.competition and comp != args.competition:
            continue
        oid = o["outcome"]
        coin = outcome_coin(oid, 0)  # canonical YES coin
        pa, pb = d.get("participantA"), d.get("participantB")

        # HL book stats.
        try:
            hb, ha = hl_book(base, coin)
        except Exception:
            hb, ha = [], []
        hbb, hba, hsp, hbn, han = book_stats(hb, ha, args.levels, args.band_frac)
        if (hbn + han) < args.min_hl_notional:
            continue

        row = {
            "outcome": oid, "coin": coin, "type": o.get("name", "").split(":")[-1],
            "competition": comp, "yes": pa, "no": pb,
            "start": d.get("scheduledStart", d.get("time", "")),
            "hl": {"bid": hbb, "ask": hba, "spread": hsp, "bid_ntl": hbn, "ask_ntl": han},
            "kalshi": None, "polymarket": None,
        }

        # Kalshi match (sports only, where we have a series + participants).
        series = KALSHI_SERIES.get(comp)
        if series and pa and pb:
            tk, orient = match_kalshi(series, pa, pb)
            if tk:
                kb, ka = kalshi_book(tk)
                kbb, kba, ksp, kbn, kan = book_stats(kb, ka, args.levels, args.band_frac)
                kmid = (kbb + kba) / 2
                hmid = (hbb + hba) / 2
                # Orientation sanity: if the mids disagree wildly, flag it regardless of the
                # name match (this is the cheap side-mapping guard).
                px_ok = abs(hmid - kmid) < 0.15 if (kb and ka) else None
                row["kalshi"] = {
                    "ticker": tk, "orientation": orient, "bid": kbb, "ask": kba, "spread": ksp,
                    "bid_ntl": kbn, "ask_ntl": kan, "mid_agrees": px_ok,
                }

        # Polymarket (best-effort).
        if args.polymarket and pa and pb:
            tok = match_polymarket(pa, pb)
            if tok:
                pbk, pak = poly_book(tok)
                pbb, pba, psp, pbn, pan = book_stats(pbk, pak, args.levels, args.band_frac)
                row["polymarket"] = {"token_id": tok, "bid": pbb, "ask": pba, "spread": psp,
                                     "bid_ntl": pbn, "ask_ntl": pan}
        rows.append(row)

    return rows


def print_table(rows, levels, band_frac):
    print(f"# notional ($ = price*size) within min({levels} levels, {band_frac:.0%} of mid); "
          f"spread in probability points\n")
    hdr = (f"{'coin':>8} {'start':<13} {'competition':<8} {'matchup (YES / NO)':<32} "
           f"{'HL spr':>7} {'HL bid$':>9} {'HL ask$':>9}  "
           f"{'ref':<7} {'ref spr':>7} {'ref bid$':>10} {'ref ask$':>10}  orient")
    print(hdr)
    print("-" * len(hdr))
    # Soonest first; outcomes without a parseable start (e.g. crypto binaries) sort last.
    for r in sorted(rows, key=lambda x: (x.get("start") or "z", -( x["hl"]["bid_ntl"]
                                                                     + x["hl"]["ask_ntl"]))):
        hl = r["hl"]
        matchup = f"{(r['yes'] or '?')[:14]} / {(r['no'] or '?')[:13]}"
        start = (r.get("start") or "")[:13]
        ref = r["kalshi"] or r["polymarket"]
        refname = "kalshi" if r["kalshi"] else ("poly" if r["polymarket"] else "-")
        if ref:
            orient = ref.get("orientation", "")
            if ref.get("mid_agrees") is False:
                orient = "MID-DISAGREE!"
            print(f"{r['coin']:>8} {start:<13} {r['competition']:<8} {matchup:<32} "
                  f"{hl['spread']:7.3f} {hl['bid_ntl']:9,.0f} {hl['ask_ntl']:9,.0f}  "
                  f"{refname:<7} {ref['spread']:7.3f} {ref['bid_ntl']:10,.0f} {ref['ask_ntl']:10,.0f}"
                  f"  {orient}")
        else:
            print(f"{r['coin']:>8} {start:<13} {r['competition']:<8} {matchup:<32} "
                  f"{hl['spread']:7.3f} {hl['bid_ntl']:9,.0f} {hl['ask_ntl']:9,.0f}  "
                  f"{'(no reference matched)':<38}")


def main():
    ap = argparse.ArgumentParser(description="Scan HIP-4 txyz outcomes and compare to references.")
    ap.add_argument("--venue-name", default="txyz")
    ap.add_argument("--competition", default=None)
    ap.add_argument("--levels", type=int, default=10)
    ap.add_argument("--band-frac", type=float, default=0.10,
                    help="Only count levels within this fraction of the mid (price band = "
                         "band_frac*mid), on top of the --levels cap. Keeps far-out stink "
                         "levels out of the notional. Default 0.10 (10%%).")
    ap.add_argument("--min-hl-notional", type=float, default=0.0)
    ap.add_argument("--polymarket", action="store_true")
    ap.add_argument("--testnet", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = scan(args)
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
    else:
        print_table(rows, args.levels, args.band_frac)


if __name__ == "__main__":
    main()
