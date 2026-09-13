#! /usr/bin/env python

"""
hip4_utils.py -- HIP-4 (outcome / prediction market) plumbing for Hyperliquid.

This is the HIP-4 sibling of hl_utils.py. It is deliberately a separate file
because outcomes are NOT perps: there is no dex, no leverage, no funding, no
liquidation, and the asset-id encoding is a fourth scheme distinct from core
perps, spot, and HIP-3 builder perps.

STATUS: exploration tooling, not a trading path. Every read path has been
exercised against mainnet. Every write path -- orders, cancels, and all four
collateral primitives -- has been verified end-to-end on TESTNET with balance
reconciliation. NOTHING has been sent on mainnet.

Not fit for live trading as written: _setup_outcome_exchange() rebuilds the
Exchange and refetches spotMeta + perp metas + outcomeMeta on EVERY call, which
is hundreds of ms per order. Fine for CLI use, useless for a strategy loop.

Read overmind/studies/hip4_guide.md before trading this. The deployer side
(market creation, settlement authority, void handling) is in
overmind/studies/hip4_deployer_analysis.md.

--------------------------------------------------------------------------
The one thing you must internalise: the encoding
--------------------------------------------------------------------------

An outcome market is identified by an integer `outcome` id plus a binary
`side` (0 = first entry in sideSpecs, usually "Yes"; 1 = second, usually "No").
Those two collapse into a single integer:

    encoding = 10 * outcome + side

and that same encoding then appears in three different textual/numeric skins
depending on which part of the API you are talking to:

    "#<encoding>"           order books, trades, orders, candles, allMids
    "+<encoding>"           spot balance rows (spotClearinghouseState)
    100_000_000 + encoding  the integer `asset` inside a signed order action

So outcome 961 side 0 is "#9610" on the book, "+9610" in your balances, and
asset id 100009610 in the wire format. Mixing these up is the #1 way to get a
confusing error, so everything below goes through the helpers.

--------------------------------------------------------------------------
The second thing: the book is merged
--------------------------------------------------------------------------

Yes and No for the same outcome share one book. Buying Yes at p IS selling No
at 1-p. So "#9610" and "#9611" are two views of the same liquidity, mirrored
about 1.0. Do not treat them as two independent markets and do not try to
arb them against each other -- that trade does not exist.

--------------------------------------------------------------------------
Example usage
--------------------------------------------------------------------------

  PY=/home/david/.venvs/v1/bin/python

  # What outcome markets exist right now, with the spec string parsed out.
  #   --mode meta      : hit the outcomeMeta info endpoint
  #   (no --testnet)   : mainnet. Add --testnet for the testnet universe.
  $PY hip4_utils.py --mode meta

  # Same, but include the multi-outcome "questions" (price buckets).
  $PY hip4_utils.py --mode meta --show_questions

  # Top of book for the BTC daily binary, Yes side.
  #   --outcome 961    : the outcome id from --mode meta
  #   --side 0         : 0 = Yes (first sideSpec), 1 = No
  $PY hip4_utils.py --mode l2 --outcome 961 --side 0

  # Equivalent, addressing the market by its book symbol directly.
  $PY hip4_utils.py --mode l2 --sym '#9610'

  # All outcome mids in one shot (filtered out of the global allMids).
  $PY hip4_utils.py --mode mids

  # My outcome share balances (these live in the SPOT clearinghouse, not perp).
  $PY hip4_utils.py --mode balances

  # How did outcome 951 actually settle, and to what price.
  #   --outcome 951    : must already be settled or this 500s
  $PY hip4_utils.py --mode settled --outcome 951

  # Rest a bid for 50 Yes shares at 0.81 on the BTC daily binary.
  #   --side_str BUY   : BUY = long the side named by --side
  #   --price 0.81     : probability, in [0, 1]
  #   --size 50        : number of shares; 1 share pays 1 quote token if Yes
  #   --tif Alo        : post-only. Gtc / Ioc also accepted.
  $PY hip4_utils.py --mode send --outcome 961 --side 0 \
      --price 0.81 --size 50 --side_str BUY --tif Alo

  # Mint 100 Yes + 100 No out of 100 USDC (the risk-free decomposition).
  $PY hip4_utils.py --mode split --outcome 961 --amt 100

  # Burn 100 Yes + 100 No back into 100 USDC. --amt omitted means "max".
  $PY hip4_utils.py --mode merge --outcome 961

  # Cancel everything I have resting on outcome books.
  $PY hip4_utils.py --mode cancel_every

  # Stream the merged book + prints for one outcome.
  $PY hip4_utils.py --mode subscribe_l2 --outcome 961 --side 0

  # Stream outcome creation/settlement events (this is how you learn that the
  # daily rolled over without polling).
  $PY hip4_utils.py --mode subscribe_meta
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import eth_account
import requests

# Same endpoint table as hl_utils.py. Outcomes are on the same hosts as
# everything else -- there is no separate outcome API.
mkt_to_endpoint = {
    "Hyperliquid": "https://api.hyperliquid.xyz/",
    "HyperliquidTest": "https://api.hyperliquid-testnet.xyz/",
}

ws_endpoint = {
    "Hyperliquid": "wss://api.hyperliquid.xyz/ws",
    "HyperliquidTest": "wss://api.hyperliquid-testnet.xyz/ws",
}

# Asset-id base for outcomes. Core perps start at 0, spot at 10_000, builder
# perps at 100_000, and outcomes at 100_000_000.
OUTCOME_ASSET_BASE = 100_000_000

# Whole shares only. MEASURED, not assumed: a fractional size is rejected with
# "Order has invalid size." on every market tested -- four distinct testnet
# outcomes including a deployer-created one and a USDH-quoted one.
#
# Caveat that keeps this a constant rather than a lookup: there is NO szDecimals
# field published for outcomes anywhere in the info API (not in spotMeta, not in
# outcomeMeta), so we cannot check it per market. If a market ever appears with
# a fractional size, this global is wrong for it and both the SDK asset seeding
# and any size validation break for that market.
OUTCOME_SZ_DECIMALS = 0

# Price tick is a fixed 0.00001 (5 decimal places). Measured, not assumed --
# see round_outcome_px for the evidence and for why it must be a uniform grid
# rather than the significant-figure rule spot/perps use.
OUTCOME_PX_DECIMALS = 5

CREDS_PATH = "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())
eth_wallet = None
my_address = None


def _load_creds(path):
    global eth_wallet, my_address, CREDS_PATH
    path = os.path.expanduser(path)
    with open(path) as f:
        hyper_creds = json.load(f)
    eth_wallet = eth_account.Account.from_key(hyper_creds["secret_key"])
    my_address = eth_wallet.address
    CREDS_PATH = path


_load_creds(CREDS_PATH)


def _base(is_testnet):
    # Endpoint root, with the trailing slash kept so we can append "info".
    return mkt_to_endpoint["HyperliquidTest" if is_testnet else "Hyperliquid"]


def _info(payload, is_testnet):
    # Every info call is an unauthenticated POST of a small JSON blob. Kept as
    # a one-liner so the individual modes below stay readable.
    endpt = _base(is_testnet) + "info"
    resp = requests.post(endpt, json=payload)
    return json.loads(resp.text)


# --------------------------------------------------------------------------
# Encoding helpers. Use these; never hand-build a "#9610" string.
# --------------------------------------------------------------------------

def encode_outcome(outcome, side):
    """(outcome id, side 0/1) -> the shared integer encoding."""
    if side not in (0, 1):
        raise ValueError("side must be 0 or 1, got {}".format(side))
    return 10 * int(outcome) + int(side)


def decode_outcome(encoding):
    """Inverse of encode_outcome: encoding -> (outcome id, side)."""
    return divmod(int(encoding), 10)


def outcome_coin(outcome, side):
    """Book/trade/order symbol, e.g. '#9610'."""
    return "#{}".format(encode_outcome(outcome, side))


def outcome_token(outcome, side):
    """Spot-balance token name, e.g. '+9610'."""
    return "+{}".format(encode_outcome(outcome, side))


def outcome_asset_id(outcome, side):
    """Integer `asset` for a signed order/cancel action, e.g. 100009610."""
    return OUTCOME_ASSET_BASE + encode_outcome(outcome, side)


def parse_outcome_sym(sym):
    """Accept '#9610', '+9610', '9610', or '961:0' and return (outcome, side).

    Lets the CLI take either --sym or --outcome/--side without caring which.
    """
    s = str(sym).strip()
    if ":" in s:
        o, sd = s.split(":", 1)
        return int(o), int(sd)
    s = s.lstrip("#+")
    return decode_outcome(int(s))


def dual_coin(sym):
    """Given one side's book symbol, return the other side's.

    Handy because the two are the same book: if you want to know what your
    Yes bid looks like to a No trader, flip it through here.
    """
    outcome, side = parse_outcome_sym(sym)
    return outcome_coin(outcome, 1 - side)


def round_outcome_px(px):
    """Round a probability onto the exchange's price grid.

    The tick is a FIXED 0.00001 -- i.e. at most 5 DECIMAL PLACES. It is NOT the
    5-significant-figure rule that spot and perps use, and getting this wrong
    is silent until you quote below 0.1. Measured on testnet:

        0.11234  (5dp, 5sf)  accepted
        0.01234  (5dp, 4sf)  accepted
        0.00123  (5dp, 3sf)  accepted      <- a sig-fig rule would allow more
        0.012345 (6dp, 5sf)  REJECTED      <- a sig-fig rule would allow this
        0.001234 (6dp, 4sf)  REJECTED

    The reason it has to be a uniform grid is the merged book (see module
    docstring): an order to buy Yes at p is the same order as a sell of No at
    1-p, so BOTH prices must be representable. A fixed decimal grid is closed
    under p -> 1-p; a significant-figure rule is not (0.012345 is 5sf, but
    0.987655 is 6sf). The mirror would break at the extremes.

    This matters most exactly where daily digitals end up near expiry -- deep
    OTM, below 0.1 -- which is the region the sig-fig rule silently corrupts.
    """
    return round(float(px), OUTCOME_PX_DECIMALS)


def parse_spec(description):
    """Parse a recurring-outcome `description` into a dict.

    The protocol packs the whole contract spec into a pipe-delimited string:
        class:priceBinary|underlying:BTC|expiry:20260731-0600|targetPrice:64009|period:1d

    Non-recurring (builder-listed) markets have prose descriptions instead, in
    which case there are no colons to split on and we return {} so callers can
    just check for emptiness.
    """
    out = {}
    if not description or "|" not in description:
        return out
    for part in description.split("|"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def spec_expiry_ts(spec):
    """Convert an expiry field like '20260731-0600' to epoch ms (UTC).

    Recurring outcomes settle at the stated UTC wall-clock time, so this is
    what you clock a time-to-expiry off of.
    """
    exp = spec.get("expiry")
    if not exp:
        return None
    dt = datetime.strptime(exp, "%Y%m%d-%H%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------
# Info / read-only modes
# --------------------------------------------------------------------------

def get_outcome_meta(is_testnet=True, show_questions=False, should_print=True):
    """The outcome universe: every live outcome, plus multi-outcome questions.

    This is the HIP-4 analogue of `meta` for perps. Note it takes no dex
    argument -- outcomes are not namespaced by dex.
    """
    response = _info({"type": "outcomeMeta"}, is_testnet)

    if should_print:
        for one in response.get("outcomes", []):
            spec = parse_spec(one.get("description", ""))
            # For recurring markets show the parsed spec; for prose-described
            # builder markets just truncate the sentence.
            if spec:
                detail = "{} {} exp {} tgt {} per {}".format(
                    spec.get("class", "?"),
                    spec.get("underlying", "?"),
                    spec.get("expiry", "?"),
                    spec.get("targetPrice", spec.get("priceThresholds", "?")),
                    spec.get("period", "?"),
                )
            else:
                detail = (one.get("description") or "")[:70]

            sides = [s["name"] for s in one.get("sideSpecs", [])]
            print("outcome {:>6}  {:<24} quote {:<5} sides {} -> {} / {}   {}".format(
                one["outcome"],
                one.get("name", "")[:24],
                one.get("quoteToken", "?"),
                sides,
                outcome_coin(one["outcome"], 0),
                outcome_coin(one["outcome"], 1),
                detail,
            ))

        if show_questions:
            print()
            for q in response.get("questions", []):
                print("question {:>6}  {:<20} fallback {} named {} settled {}".format(
                    q["question"],
                    q.get("name", "")[:20],
                    q.get("fallbackOutcome"),
                    q.get("namedOutcomes"),
                    q.get("settledNamedOutcomes"),
                ))
                print("    {}".format(q.get("description", "")[:160]))

    return response


def get_outcome_deployer(outcome, is_testnet=True, meta=None):
    """Return the deployer address for an outcome, or None if protocol-deployed.

    This is the single most important risk field on an outcome. A null deployer
    means the PROTOCOL settles it, deterministically, against a mark price --
    no human discretion anywhere. A non-null deployer means some third party
    chooses the settleFraction by hand, and as of the HIP-4 deployer docs there
    is no documented dispute mechanism, challenge window, or appeal if they get
    it wrong (or settle adversarially).

    Mainnet currently returns no `deployer` key at all -- every mainnet outcome
    is protocol-deployed. Testnet has both kinds. See
    overmind/studies/hip4_deployer_analysis.md.
    """
    if meta is None:
        meta = get_outcome_meta(is_testnet, should_print=False)
    for one in meta.get("outcomes", []):
        if one["outcome"] == int(outcome):
            return one.get("deployer")
    return None


def warn_if_deployer_settled(outcome, is_testnet=True, meta=None):
    """Print a warning if this outcome is settled by a third party, not the protocol.

    Called before sending orders. Deliberately a warning rather than a block --
    third-party markets are legitimately tradeable, they just carry settlement
    risk that protocol markets do not, and that should be a conscious decision
    rather than something noticed after settlement.
    """
    deployer = get_outcome_deployer(outcome, is_testnet, meta)
    if deployer:
        print("*** WARNING: outcome {} is DEPLOYER-SETTLED by {}".format(
            outcome, deployer))
        print("***          Settlement is discretionary; no documented dispute path.")
    return deployer


def get_settled_outcome(outcome, is_testnet=True):
    """How a settled outcome resolved.

    `settleFraction` is what one Yes share converted into: "1.0" means Yes
    paid 1 quote token and No paid 0; "0.0" is the reverse. `details` carries
    the observed value that drove it (e.g. "price:64040"), which is what you
    reconcile a mark against.
    """
    response = _info({"type": "settledOutcome", "outcome": int(outcome)}, is_testnet)
    print(json.dumps(response, indent=2))
    return response


def get_outcome_book(outcome, side, is_testnet=True, n_levels=10):
    """L2 snapshot for one side of an outcome.

    Remember this is a merged book -- the asks you see here are the mirror of
    the other side's bids. We print the dual price alongside each level so the
    equivalence is visible rather than something you have to remember.
    """
    coin = outcome_coin(outcome, side)
    response = _info({"type": "l2Book", "coin": coin}, is_testnet)

    bids, asks = response["levels"]
    print("{} (dual {})  t={}".format(coin, dual_coin(coin), response.get("time")))
    print("{:>12} {:>10} {:>4}   |   {:>12} {:>10} {:>4}".format(
        "bid", "sz", "n", "ask", "sz", "n"))
    for i in range(min(n_levels, max(len(bids), len(asks)))):
        b = bids[i] if i < len(bids) else {"px": "", "sz": "", "n": ""}
        a = asks[i] if i < len(asks) else {"px": "", "sz": "", "n": ""}
        print("{:>12} {:>10} {:>4}   |   {:>12} {:>10} {:>4}".format(
            b["px"], b["sz"], b["n"], a["px"], a["sz"], a["n"]))

    # The merged-book invariant, stated numerically: best bid here should be
    # 1 - (best ask on the dual). Worth asserting in any live strategy.
    if bids and asks:
        print("best_bid {} best_ask {}  -> dual best_ask {:.5f} dual best_bid {:.5f}".format(
            bids[0]["px"], asks[0]["px"],
            1.0 - float(bids[0]["px"]), 1.0 - float(asks[0]["px"])))

    return response


def get_outcome_mids(is_testnet=True):
    """Every outcome mid, pulled out of the global allMids response.

    allMids is not filterable server-side, so outcomes arrive mixed in with
    all the perp and spot mids and we sieve on the '#' prefix. Yes and No mids
    sum to exactly 1.0, which is a cheap sanity check on the feed.
    """
    response = _info({"type": "allMids"}, is_testnet)
    mids = {k: v for k, v in response.items() if k.startswith("#")}

    # Group the two sides back together so it reads as one market per line.
    by_outcome = {}
    for coin, px in mids.items():
        outcome, side = parse_outcome_sym(coin)
        by_outcome.setdefault(outcome, {})[side] = float(px)

    for outcome in sorted(by_outcome):
        sides = by_outcome[outcome]
        yes, no = sides.get(0), sides.get(1)
        print("outcome {:>6}  yes {:>9}  no {:>9}  sum {}".format(
            outcome,
            "{:.5f}".format(yes) if yes is not None else "-",
            "{:.5f}".format(no) if no is not None else "-",
            "{:.5f}".format(yes + no) if yes is not None and no is not None else "-",
        ))
    return mids


def get_outcome_trades(outcome, side, is_testnet=True):
    """Recent prints on one outcome book."""
    coin = outcome_coin(outcome, side)
    response = _info({"type": "recentTrades", "coin": coin}, is_testnet)
    for t in response:
        print("{}  {}  px {:>9}  sz {:>9}  tid {}".format(
            t["time"], t["side"], t["px"], t["sz"], t["tid"]))
    return response


def get_outcome_candles(outcome, side, interval="1m", lookback_ms=3600_000,
                        is_testnet=True):
    """Candles for one outcome book.

    Same candleSnapshot endpoint as everything else; the only difference is
    that `coin` is the '#<encoding>' form.
    """
    coin = outcome_coin(outcome, side)
    end = int(time.time() * 1000)
    req = {
        "coin": coin,
        "interval": interval,
        "startTime": end - lookback_ms,
        "endTime": end,
    }
    response = _info({"type": "candleSnapshot", "req": req}, is_testnet)
    for c in response:
        print("{}  o {:>9} h {:>9} l {:>9} c {:>9}  v {:>10} n {}".format(
            c["t"], c["o"], c["h"], c["l"], c["c"], c["v"], c["n"]))
    return response


def get_my_outcome_balances(is_testnet=True):
    """My outcome share balances.

    These are SPOT balances, not perp positions -- outcomes are fully
    collateralised tokens, so `check_perp_balance` in hl_utils.py will never
    show them. `hold` is the amount locked in resting sell orders. `entryNtl`
    is the notional cost basis, so avg entry = entryNtl / total.
    """
    response = _info(
        {"type": "spotClearinghouseState", "user": my_address}, is_testnet)

    balances = response.get("balances", [])
    quote = [b for b in balances if b["coin"] in ("USDC", "USDH")]
    shares = [b for b in balances if b["coin"].startswith("+")]

    for b in quote:
        print("{:<8} total {:>16} hold {:>14}".format(
            b["coin"], b["total"], b["hold"]))

    print()
    for b in shares:
        outcome, side = parse_outcome_sym(b["coin"])
        total = float(b["total"])
        entry_ntl = float(b.get("entryNtl", 0.0))
        avg_px = (entry_ntl / total) if total else 0.0
        print("outcome {:>6} side {}  total {:>12} hold {:>10}  avgpx {:.5f}".format(
            outcome, side, b["total"], b["hold"], avg_px))

    return response


def get_my_outcome_orders(is_testnet=True, should_print=True):
    """My resting orders on outcome books only.

    openOrders returns everything across perp/spot/outcome in one list, so we
    filter to the '#' coins. Note the dual-order wrinkle: an order that both
    matched and rested can come back split across the primary and dual coin,
    so do not assume one submitted order == one row here.
    """
    response = _info({"type": "openOrders", "user": my_address}, is_testnet)
    orders = [o for o in response if o["coin"].startswith("#")]

    if should_print:
        for o in orders:
            outcome, side = parse_outcome_sym(o["coin"])
            print("{:<8} outcome {:>6} side {}  {}  px {:>9} sz {:>9}  oid {}".format(
                o["coin"], outcome, side, o["side"], o["limitPx"], o["sz"], o["oid"]))
    return orders


# --------------------------------------------------------------------------
# Exchange / write modes
#
# The installed SDK (hyperliquid-python-sdk 0.23.0) has NO outcome support:
#   - Info.__init__ builds coin_to_asset from spotMeta + perp metas only, and
#     outcome tokens/pairs do NOT appear in spotMeta at all, so every outcome
#     symbol is a KeyError in name_to_coin.
#   - There is no split/merge/negate action anywhere in exchange.py.
# So we seed the SDK's lookup tables ourselves and hand-roll the userOutcome
# actions. Everything else (nonce handling, action hashing, signing, posting)
# is reused from the SDK so we are not reimplementing the signature scheme.
# --------------------------------------------------------------------------

def _seed_outcome_assets(info, meta):
    """Register every live outcome (both sides) in one Info's lookup tables.

    The SDK builds coin_to_asset from spotMeta + perp metas, and outcome
    tokens/pairs appear in NEITHER, so without this every outcome symbol is a
    KeyError in name_to_asset.
    """
    n = 0
    for one in meta.get("outcomes", []):
        for side in (0, 1):
            coin = outcome_coin(one["outcome"], side)
            asset = outcome_asset_id(one["outcome"], side)
            info.coin_to_asset[coin] = asset
            info.name_to_coin[coin] = coin
            # Used by the SDK's price rounding; see OUTCOME_SZ_DECIMALS above.
            info.asset_to_sz_decimals[asset] = OUTCOME_SZ_DECIMALS
            n += 1
    return n


def _setup_outcome_exchange(is_testnet=True):
    """Build an SDK Exchange that knows about outcome assets.

    Returns (address, info, exchange). The seeding step is the whole point:
    after it, exchange.order("#9610", ...) resolves to asset 100009610 and the
    normal SDK order path works unmodified.

    GOTCHA (cost us a live failure): Exchange.__init__ constructs its OWN Info
    internally, so the `info` returned by example_utils.setup is a DIFFERENT
    object from `exchange.info`. Orders and cancels resolve assets through
    exchange.info, so both instances must be seeded. Seeding only the returned
    one looks fine -- split/merge/negate still work, because those are raw
    actions keyed by outcome id and never touch the asset map -- and then
    every order dies with a KeyError.
    """
    import example_utils
    from hyperliquid.utils import constants

    tgt_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL
    address, info, exchange = example_utils.setup(
        base_url=tgt_url, skip_ws=True, creds_path=CREDS_PATH)

    # Pull the live outcome universe once and seed both Info instances.
    meta = get_outcome_meta(is_testnet, should_print=False)
    n = _seed_outcome_assets(info, meta)
    _seed_outcome_assets(exchange.info, meta)
    print("Seeded {} outcome assets into both SDK asset maps.".format(n))

    return address, info, exchange


def _signed_action(exchange, action, is_testnet):
    """Sign and post a raw L1 action the SDK has no helper for.

    Used for the userOutcome family (split/merge/negate). The action dict is
    msgpack-hashed by the SDK exactly as-is, so the field names below must
    match the docs character for character.
    """
    from hyperliquid.utils.signing import sign_l1_action

    nonce = int(time.time() * 1000)
    signature = sign_l1_action(
        exchange.wallet,
        action,
        exchange.vault_address,
        nonce,
        exchange.expires_after,
        not is_testnet,   # is_mainnet
    )
    return exchange._post_action(action, signature, nonce)


def send_order(outcome, side, price, size, side_str, tif="Alo", is_testnet=True):
    """Rest (or cross) an order on an outcome book.

    price is a probability in [0, 1]. size is a share count -- one Yes share
    pays out 1 quote token if the outcome resolves Yes, 0 otherwise, so your
    max loss on a buy is price * size and your max gain is (1 - price) * size.
    There is no leverage and nothing to liquidate.

    VERIFIED on testnet: rests, crosses, and fills correctly. Never sent on
    mainnet. Warns first if the market is deployer-settled (discretionary).
    """
    coin = outcome_coin(outcome, side)

    if side_str == "BUY":
        is_buy = True
    elif side_str == "SELL":
        is_buy = False
    else:
        raise ValueError("Invalid side_str {}".format(side_str))

    if not (0.0 < price < 1.0):
        # Outcome prices are bounded by construction. A price outside (0, 1)
        # is always a bug on our side, not something the venue should reject.
        raise ValueError("Outcome price must be in (0, 1), got {}".format(price))

    px = round_outcome_px(price)
    if px != price:
        print("Rounded price {} -> {} (5 sig figs).".format(price, px))

    address, info, exchange = _setup_outcome_exchange(is_testnet)

    # Surface discretionary-settlement risk before we take a position, not after.
    warn_if_deployer_settled(outcome, is_testnet)

    print("Placing {} {} @ {} on {} (outcome {} side {})".format(
        side_str, size, px, coin, outcome, side))
    order_result = exchange.order(coin, is_buy, size, px, {"limit": {"tif": tif}})
    print("order result: {}".format(order_result))
    return order_result


def cancel_order(outcome, side, oid, is_testnet=True):
    """Cancel one resting outcome order by oid."""
    coin = outcome_coin(outcome, side)
    address, info, exchange = _setup_outcome_exchange(is_testnet)
    result = exchange.cancel(coin, oid)
    print(result)
    return result


def cancel_every_order(is_testnet=True):
    """Cancel every resting order on every outcome book.

    Deliberately scoped to outcomes only -- unlike hl_utils.cancel_every_order,
    this will not touch your perp or spot orders.
    """
    orders = get_my_outcome_orders(is_testnet, should_print=False)
    if not orders:
        print("No open outcome orders.")
        return

    address, info, exchange = _setup_outcome_exchange(is_testnet)
    cancel_requests = [
        {"coin": o["coin"], "oid": int(o["oid"])} for o in orders
    ]
    print("Cancelling {} outcome orders across {} books.".format(
        len(cancel_requests), len({r["coin"] for r in cancel_requests})))
    result = exchange.bulk_cancel(cancel_requests)
    print(result)
    return result


def split_outcome(outcome, amt, is_testnet=True):
    """Mint `amt` Yes + `amt` No shares by locking `amt` quote tokens.

    This is the primitive that makes outcomes fully collateralised: 1 Yes +
    1 No is worth exactly 1 quote token at settlement no matter what happens,
    so the protocol will always hand you the pair for the quote token. Use it
    to manufacture inventory to sell into a one-sided book rather than lifting
    the offer.

    VERIFIED on testnet with exact balance reconciliation. Never sent on mainnet.
    """
    address, info, exchange = _setup_outcome_exchange(is_testnet)
    action = {
        "type": "userOutcome",
        "splitOutcome": {"outcome": int(outcome), "amount": str(float(amt))},
    }
    print("Splitting {} quote tokens into {}/{} shares.".format(
        amt, outcome_coin(outcome, 0), outcome_coin(outcome, 1)))
    result = _signed_action(exchange, action, is_testnet)
    print(result)
    return result


def merge_outcome(outcome, amt=None, is_testnet=True):
    """Burn `amt` Yes + `amt` No shares back into `amt` quote tokens.

    The inverse of split. amt=None means "as much as possible", which is the
    normal way to sweep a paired residual back into cash before expiry rather
    than waiting for settlement.

    VERIFIED on testnet with exact balance reconciliation. Never sent on mainnet.
    """
    address, info, exchange = _setup_outcome_exchange(is_testnet)
    action = {
        "type": "userOutcome",
        "mergeOutcome": {
            "outcome": int(outcome),
            "amount": None if amt is None else str(float(amt)),
        },
    }
    print("Merging {} paired shares on outcome {} back to quote.".format(
        "max" if amt is None else amt, outcome))
    result = _signed_action(exchange, action, is_testnet)
    print(result)
    return result


def merge_question(question, amt=None, is_testnet=True):
    """Burn one Yes share from EVERY outcome of a question into a quote token.

    Only applies to multi-outcome questions (the price-bucket markets), where
    exactly one bucket resolves Yes. Holding one Yes of each bucket is
    therefore worth exactly 1 quote token, and this redeems it early.

    VERIFIED on testnet with exact balance reconciliation. Never sent on mainnet.
    """
    address, info, exchange = _setup_outcome_exchange(is_testnet)
    action = {
        "type": "userOutcome",
        "mergeQuestion": {
            "question": int(question),
            "amount": None if amt is None else str(float(amt)),
        },
    }
    print("Merging {} across question {}.".format(
        "max" if amt is None else amt, question))
    result = _signed_action(exchange, action, is_testnet)
    print(result)
    return result


def negate_outcome(question, outcome, amt, is_testnet=True):
    """Convert `amt` No shares of one outcome into `amt` Yes of every other.

    "Not bucket B" is the same claim as "bucket A or bucket C", so this is a
    pure relabelling within a question. Useful to move an existing No position
    onto the books where the liquidity actually is.

    VERIFIED on testnet with exact balance reconciliation. Never sent on mainnet.
    """
    address, info, exchange = _setup_outcome_exchange(is_testnet)
    action = {
        "type": "userOutcome",
        "negateOutcome": {
            "question": int(question),
            "outcome": int(outcome),
            "amount": str(float(amt)),
        },
    }
    print("Negating {} No shares of outcome {} within question {}.".format(
        amt, outcome, question))
    result = _signed_action(exchange, action, is_testnet)
    print(result)
    return result


# --------------------------------------------------------------------------
# Websocket modes
# --------------------------------------------------------------------------

async def subscribe_l2(outcome, side, is_testnet=True):
    """Stream the merged book and prints for one outcome.

    Subscriptions take the '#<encoding>' coin and, unlike HIP-3 perps, take no
    `dex` field -- outcomes are not dex-namespaced.
    """
    import websockets

    coin = outcome_coin(outcome, side)
    ws_url = ws_endpoint["HyperliquidTest" if is_testnet else "Hyperliquid"]

    last_t_ping = time.time()
    async with websockets.connect(ws_url, ping_interval=100000000) as ws:
        for sub_type in ("l2Book", "trades"):
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": sub_type, "coin": coin},
            }))

        while True:
            # HL wants an application-level ping; the library-level one is
            # disabled above so we own the cadence.
            if (time.time() - last_t_ping) > 40:
                await ws.send(json.dumps({"method": "ping"}))
                last_t_ping = time.time()

            try:
                message = await asyncio.wait_for(ws.recv(), 0.01)
                print(json.loads(message))
            except asyncio.exceptions.TimeoutError:
                pass
            except Exception:
                traceback.print_exc()
                sys.exit()


async def subscribe_meta(is_testnet=True):
    """Stream outcome lifecycle events.

    This is the important one for recurring markets: `outcomeCreated` /
    `outcomeSettled` tell you the daily rolled over, so you learn the new
    outcome id and target price without polling outcomeMeta on a timer.
    """
    import websockets

    ws_url = ws_endpoint["HyperliquidTest" if is_testnet else "Hyperliquid"]

    last_t_ping = time.time()
    async with websockets.connect(ws_url, ping_interval=100000000) as ws:
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "outcomeMetaUpdates"},
        }))

        while True:
            if (time.time() - last_t_ping) > 40:
                await ws.send(json.dumps({"method": "ping"}))
                last_t_ping = time.time()

            try:
                message = await asyncio.wait_for(ws.recv(), 0.01)
                msg = json.loads(message)
                # Annotate the settlement/creation events with the derived
                # book symbols, since the raw event only carries the id.
                for update in (msg.get("data") or []):
                    if isinstance(update, dict) and "outcomeCreated" in update:
                        spec = update["outcomeCreated"]
                        print("CREATED outcome {} -> {} / {}  {}".format(
                            spec["outcome"],
                            outcome_coin(spec["outcome"], 0),
                            outcome_coin(spec["outcome"], 1),
                            spec.get("description", "")))
                    elif isinstance(update, dict) and "outcomeSettled" in update:
                        print("SETTLED outcome {}".format(update["outcomeSettled"]))
                print(msg)
            except asyncio.exceptions.TimeoutError:
                pass
            except Exception:
                traceback.print_exc()
                sys.exit()


def main():
    parser = argparse.ArgumentParser(
        description="HIP-4 outcome market utilities for Hyperliquid.")

    # Which operation to run; see the module docstring for worked examples.
    parser.add_argument("--mode", type=str, required=True)
    # Default is MAINNET (opposite of hl_utils, because HIP-4 recurring
    # markets are live on mainnet and the testnet set is a different universe).
    parser.add_argument("--testnet", default=False, action="store_true")

    # Market selection: either --outcome/--side, or --sym as '#9610'/'961:0'.
    parser.add_argument("--outcome", type=int, default=None,
                        help="Outcome id from --mode meta")
    parser.add_argument("--side", type=int, default=0,
                        help="0 = first sideSpec (usually Yes), 1 = second (No)")
    parser.add_argument("--sym", type=str, default=None,
                        help="Alternative to --outcome/--side: '#9610' or '961:0'")
    parser.add_argument("--question", type=int, default=None,
                        help="Question id, for merge_question / negate")

    # Order parameters.
    parser.add_argument("--price", type=float, default=0.5,
                        help="Probability in (0, 1)")
    parser.add_argument("--size", type=float, default=1,
                        help="Share count (integer sizes on all live markets)")
    parser.add_argument("--side_str", type=str, default="BUY",
                        help="BUY or SELL, on the side named by --side")
    parser.add_argument("--tif", type=str, default="Alo",
                        help="Alo (post-only) / Gtc / Ioc")
    parser.add_argument("--oid", type=int, default=None)

    # Amount for split/merge/negate, in quote tokens or shares.
    parser.add_argument("--amt", type=float, default=None)

    # Candle parameters.
    parser.add_argument("--interval", type=str, default="1m")
    parser.add_argument("--lookback_min", type=int, default=60)

    parser.add_argument("--show_questions", default=False, action="store_true")
    parser.add_argument("--creds", type=str, default=None,
                        help="Path to Hyperliquid creds JSON "
                             "(default: ~/.creds/.Hyperliquid.creds.json)")

    args = parser.parse_args()

    if args.creds is not None:
        _load_creds(args.creds)

    is_testnet = args.testnet

    # Resolve market selection once, so every mode below sees the same pair.
    outcome, side = args.outcome, args.side
    if args.sym is not None:
        outcome, side = parse_outcome_sym(args.sym)

    def need_outcome():
        if outcome is None:
            sys.exit("This mode needs --outcome (or --sym).")
        return outcome

    if args.mode == "meta":
        get_outcome_meta(is_testnet, args.show_questions)
    elif args.mode == "settled":
        get_settled_outcome(need_outcome(), is_testnet)
    elif args.mode == "l2":
        get_outcome_book(need_outcome(), side, is_testnet)
    elif args.mode == "mids":
        get_outcome_mids(is_testnet)
    elif args.mode == "trades":
        get_outcome_trades(need_outcome(), side, is_testnet)
    elif args.mode == "candles":
        get_outcome_candles(need_outcome(), side, args.interval,
                            args.lookback_min * 60_000, is_testnet)
    elif args.mode == "balances":
        get_my_outcome_balances(is_testnet)
    elif args.mode == "orders":
        get_my_outcome_orders(is_testnet)

    elif args.mode == "send":
        send_order(need_outcome(), side, args.price, args.size,
                   args.side_str, args.tif, is_testnet)
    elif args.mode == "cancel":
        if args.oid is None:
            sys.exit("Need --oid for --mode cancel")
        cancel_order(need_outcome(), side, args.oid, is_testnet)
    elif args.mode == "cancel_every":
        cancel_every_order(is_testnet)

    elif args.mode == "split":
        if args.amt is None:
            sys.exit("Need --amt for --mode split")
        split_outcome(need_outcome(), args.amt, is_testnet)
    elif args.mode == "merge":
        merge_outcome(need_outcome(), args.amt, is_testnet)
    elif args.mode == "merge_question":
        if args.question is None:
            sys.exit("Need --question for --mode merge_question")
        merge_question(args.question, args.amt, is_testnet)
    elif args.mode == "negate":
        if args.question is None or args.amt is None:
            sys.exit("Need --question and --amt for --mode negate")
        negate_outcome(args.question, need_outcome(), args.amt, is_testnet)

    elif args.mode == "subscribe_l2":
        asyncio.run(subscribe_l2(need_outcome(), side, is_testnet))
    elif args.mode == "subscribe_meta":
        asyncio.run(subscribe_meta(is_testnet))
    else:
        sys.exit("Unknown --mode {}".format(args.mode))


if __name__ == "__main__":
    main()
