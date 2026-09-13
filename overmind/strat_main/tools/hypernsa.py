#! /usr/bin/env python

"""

I want to write some stuff that monitors hyperliquid and tries to get me better info about counterparties and
the current market.

This here will be a small collection of tools while I figure out what I can figure out.

There will be no signing of messages. I'm just gonna do bunches of subscriptions.

I'm also going to check on hydromancer:
https://docs.hydromancer.xyz/hydromancer-better-hyperliquid-apis/builder-guides/trading-frontend

I'm guessing they're indexing the chain via a node and making it available.
  - Maybe there's stuff we can do too

I also wonder if I can just parse the transaction stuff in realtime.
tbh.

Some questions
 - Are they submitting via a builder code (ui)
 - We can ID whether the account does manual orders or via API. That part should be easy
 - We can probably ID what builder codes they use. Maybe one flow is better than another.

They're building copytrading tools




"""

import argparse
import re
from typing import Any, Dict, Optional

import example_utils
import getpass

from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.constants import TESTNET_API_URL

# I'm guessing.. ok let's just read this. I think I might just need to append
# "dex" to each request.
from eth_abi import encode
import eth_account
from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex
import json
import requests


import websockets
from tenacity import wait_fixed, retry
import time

import asyncio
import sys
import traceback


mkt_to_endpoint = {
    "Hyperliquid": "https://api.hyperliquid.xyz/",
    "HyperliquidTest": "https://api.hyperliquid-testnet.xyz/",
}


def _validate_user_address(address: str) -> str:
    if not isinstance(address, str):
        raise TypeError("Expected user address to be a string.")
    if not address.startswith("0x"):
        raise ValueError("User address must be 0x-prefixed hex.")

    hex_part = address[2:]
    if not re.fullmatch(r"[0-9a-fA-F]+", hex_part):
        raise ValueError(f"User address has non-hex characters: {address}")
    if len(hex_part) > 40:
        raise ValueError(
            "User address must be 20 bytes (<= 40 hex chars after 0x prefix). Received: {}".format(
                address
            )
        )

    # Hyperliquid trade payloads sometimes elide leading zeros; pad back out to 20 bytes.
    normalized = hex_part.rjust(40, "0")
    return "0x" + normalized.lower()


INFO_TIMEOUT_SECONDS = 10


def _resolve_endpoint_base(market: str) -> str:
    base = mkt_to_endpoint.get(market, market)
    if not isinstance(base, str):
        raise ValueError(f"Unable to resolve endpoint for market '{market}'.")
    return base


def _post_info(session: requests.Session, url: str, payload: Dict[str, Any]) -> Any:
    response = session.post(url, json=payload, timeout=INFO_TIMEOUT_SECONDS)
    response.raise_for_status()
    if response.headers.get("Content-Type", "").startswith("application/json"):
        return response.json()
    # Fall back to json.loads to cover cases where the header is missing.
    return json.loads(response.text)


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _leverage_value(leverage_field: Any) -> Optional[float]:
    if isinstance(leverage_field, dict):
        return _to_float(leverage_field.get("value"))
    return _to_float(leverage_field)


def _compute_liquidation_fallback(
    *,
    size: Optional[float],
    mark_px: Optional[float],
    margin_used: Optional[float],
    max_leverage: Optional[float],
) -> Optional[float]:
    if (
        size is None
        or size == 0
        or mark_px is None
        or margin_used is None
        or max_leverage in (None, 0)
    ):
        return None

    side = 1 if size > 0 else -1
    maintenance = abs(size) * mark_px / (2 * max_leverage)
    avail_margin = margin_used - maintenance
    denom = size * (1 - side / (2 * max_leverage))
    if denom == 0:
        return None
    return mark_px - avail_margin / denom


def _aggregate_recent_activity(
    fills: Optional[list], *, lookback_seconds: int
) -> Dict[str, Dict[str, float]]:
    if not fills:
        return {}

    now_ms = int(time.time() * 1000)
    threshold_ms = now_ms - int(max(0, lookback_seconds) * 1000)
    summary: Dict[str, Dict[str, float]] = {}

    for fill in fills:
        if not isinstance(fill, dict):
            continue

        fill_time = fill.get("time")
        if not isinstance(fill_time, (int, float)) or fill_time < threshold_ms:
            continue

        coin = fill.get("coin")
        side = fill.get("side")
        size = _to_float(fill.get("sz"))
        price = _to_float(fill.get("px"))

        if not coin or side not in {"B", "S"} or size is None or price is None:
            continue

        notional = size * price
        bucket = summary.setdefault(
            coin,
            {
                "trade_count": 0,
                "buy_size": 0.0,
                "sell_size": 0.0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
            },
        )

        bucket["trade_count"] += 1
        if side == "B":
            bucket["buy_size"] += size
            bucket["buy_notional"] += notional
        else:
            bucket["sell_size"] += size
            bucket["sell_notional"] += notional

    for bucket in summary.values():
        bucket["net_size"] = bucket["buy_size"] - bucket["sell_size"]
        bucket["net_notional"] = bucket["buy_notional"] - bucket["sell_notional"]

    return summary


def build_dossier(
    address: str,
    *,
    market: str = "Hyperliquid",
    dex: Optional[str] = "xyz",
    session: Optional[requests.Session] = None,
    trade_limit: int = 500,
    lookback_hours: int = 24,
) -> Dict[str, Any]:
    """Collect user state from Hyperliquid HTTP endpoints, with and without a dex override."""

    normalized_address = _validate_user_address(address)
    endpoint_base = _resolve_endpoint_base(market)
    info_url = endpoint_base.rstrip("/") + "/info"

    local_session = session or requests.Session()
    created_session = session is None
    local_session.headers.setdefault("Content-Type", "application/json")

    dossier: Dict[str, Any] = {
        "address": normalized_address,
        "market": market,
        "dex": dex,
        "generated_at": int(time.time() * 1000),
        "account": {},
        "perp_positions": [],
        "activity_summary": {},
        "errors": [],
    }

    def _fetch(context_key: str, name: str, payload: Dict[str, Any]) -> Any:
        try:
            data = _post_info(local_session, info_url, payload)
            return data
        except Exception as exc:  # noqa: BLE001 -- propagate error details to caller
            dossier["errors"].append(
                {
                    "context": context_key,
                    "step": name,
                    "error": str(exc),
                    "payload": payload,
                }
            )
            return None

    lookback_seconds = max(1, lookback_hours) * 3600

    def _collect_context(context_key: str, dex_override: Optional[str]) -> Dict[str, Any]:
        context_summary = {
            "account": {},
            "perp_positions": [],
            "activity_summary": {},
        }

        def _with_dex(payload: Dict[str, Any]) -> Dict[str, Any]:
            updated = dict(payload)
            if dex_override is not None:
                updated["dex"] = dex_override
            return updated

        clearing_payload = _with_dex({"type": "clearinghouseState", "user": normalized_address})
        clearing_data = _fetch(context_key, "clearinghouse_state", clearing_payload)
        if isinstance(clearing_data, dict):
            margin_summary = clearing_data.get("marginSummary")
            account_snapshot: Dict[str, Optional[float]] = {}
            if isinstance(margin_summary, dict):
                account_snapshot["account_value"] = _to_float(margin_summary.get("accountValue"))
                account_snapshot["total_margin"] = _to_float(margin_summary.get("totalMargin"))
                account_snapshot["maintenance_margin"] = _to_float(margin_summary.get("maintenanceMargin"))
            account_snapshot["withdrawable"] = _to_float(clearing_data.get("withdrawable"))
            account_snapshot["updated_at"] = clearing_data.get("time")
            context_summary["account"] = account_snapshot

            positions = clearing_data.get("assetPositions")
            if isinstance(positions, list):
                simplified_positions = []
                for pos in positions:
                    if not isinstance(pos, dict):
                        continue
                    position_detail = pos.get("position") if isinstance(pos.get("position"), dict) else pos
                    coin = position_detail.get("coin") if isinstance(position_detail, dict) else None
                    if not isinstance(position_detail, dict) or not coin:
                        continue
                    size_val = _to_float(position_detail.get("szi"))
                    entry_px = _to_float(position_detail.get("entryPx"))
                    position_value = _to_float(position_detail.get("positionValue"))
                    unrealized = _to_float(position_detail.get("unrealizedPnl"))
                    margin_used = _to_float(position_detail.get("marginUsed"))
                    max_leverage = _to_float(position_detail.get("maxLeverage"))
                    mark_px = None
                    if position_value is not None and size_val not in (None, 0):
                        mark_px = abs(position_value / size_val)

                    leverage_field = position_detail.get("leverage")
                    liquidation_px = _to_float(position_detail.get("liquidationPx"))
                    if liquidation_px is None:
                        liquidation_px = _compute_liquidation_fallback(
                            size=size_val,
                            mark_px=mark_px,
                            margin_used=margin_used,
                            max_leverage=max_leverage,
                        )

                    simplified_positions.append(
                        {
                            "coin": coin,
                            "size": size_val,
                            "entry_px": entry_px,
                            "position_value": position_value,
                            "unrealized_pnl": unrealized,
                            "leverage": _leverage_value(leverage_field),
                            "leverage_type": leverage_field.get("type")
                            if isinstance(leverage_field, dict)
                            else None,
                            "max_leverage": max_leverage,
                            "liquidation_px": liquidation_px,
                            "margin_used": margin_used,
                        }
                    )
                context_summary["perp_positions"] = simplified_positions

        fills_payload = _with_dex(
            {
                "type": "userFills",
                "user": normalized_address,
                "limit": trade_limit,
            }
        )
        fills_data = _fetch(context_key, "user_fills", fills_payload)
        fills_list = None
        if isinstance(fills_data, dict) and "fills" in fills_data:
            fills_list = fills_data.get("fills")
        elif isinstance(fills_data, list):
            fills_list = fills_data

        if isinstance(fills_list, list):
            activity_summary = _aggregate_recent_activity(
                fills_list, lookback_seconds=lookback_seconds
            )
            context_summary["activity_summary"] = activity_summary

        return context_summary

    try:
        context_key = "dex:{}".format(dex) if dex else "default"
        context = _collect_context(context_key, dex)
        dossier["account"] = context.get("account", {})
        dossier["perp_positions"] = context.get("perp_positions", [])
        dossier["activity_summary"] = context.get("activity_summary", {})

    finally:
        if created_session:
            local_session.close()

    return dossier


async def subscribe_user(address):
    address = _validate_user_address(address)
    ws_url = "wss://api.hyperliquid.xyz/ws"
    last_t_ping = time.time()

    async with websockets.connect(ws_url, ping_interval=100000000) as ws:
        try:
            # Subscribe to own fills.
            # Subscriptions I want. 
            # userEvents
            # userFills (maybe) 
            # userNonFundingLedgerUpdates
            events_dct = {
                "method": "subscribe",
                "subscription": {"type": "userEvents", "user": address},
            }
            events_sub_json = json.dumps(events_dct)
            await ws.send(events_sub_json)

            userfills_dct = {
                "method": "subscribe",
                "subscription": {"type": "userFills", "user": address},
            }
            userfills_sub_json = json.dumps(userfills_dct)
            #await ws.send(userfills_sub_json)


            userledger_dct = {
                "method": "subscribe",
                "subscription": {"type": "userNonFundingLedgerUpdates", "user": address},
            }
            userledger_sub_json = json.dumps(userledger_dct)
            await ws.send(userledger_sub_json)


            userledger_dct = {
                "method": "subscribe",
                "subscription": {"type": "userNonFundingLedgerUpdates", "user": address},
            }
            userledger_sub_json = json.dumps(userledger_dct)
            await ws.send(userledger_sub_json)

            usertwapslice_dct = {
                "method": "subscribe",
                "subscription": {"type": "userTwapSliceFills", "user": address},
            }
            usertwapslice_sub_json = json.dumps(usertwapslice_dct)
            await ws.send(usertwapslice_sub_json)            



            usertwaphistory_dct = {
                "method": "subscribe",
                "subscription": {"type": "userTwapHistory", "user": address},
            }
            usertwaphistory_sub_json = json.dumps(usertwaphistory_dct)
            await ws.send(usertwaphistory_sub_json)            


            # OK, so we do get the users. 
            # This probably means we can aggregate recent activity per-user too. 
            # And also gets us a sense of who has what 
        except Exception as e:
            print("-----------------------------------")
            traceback.print_exc()  # This is VERY useful - prints the full stack trace
            print("-----------------------------------")
            sys.exit()

        print("Hi")

        while True:
            if (time.time() - last_t_ping) > 40:
                # Do my own ping. I guess this is the "right" way that they
                # do ping/pongs.
                await ws.send(json.dumps({"method": "ping"}))
                last_t_ping = time.time()

            try:
                # message = await websocket.recv()
                message = await asyncio.wait_for(ws.recv(), 1.01)
                msg_dct = json.loads(message)
                print(msg_dct)
            except (
                asyncio.exceptions.TimeoutError
            ):  # <-- CATCH THE SPECIFIC TIMEOUT ERROR
                # print("Caught asyncio.TimeoutError: The operation took too long. Continuing execution...")
                pass
            except Exception as e:
                print("-----------------------------------")
                print("Got exception!")
                print(f"Repr (repr(e)): {repr(e)}")  # Often more detailed for debugging
                print("-----------------------------------")
                print("Full Traceback:")
                traceback.print_exc()  # This is VERY useful - prints the full stack trace
                sys.exit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str)
    parser.add_argument("--address", type=str)
    parser.add_argument("--market", type=str, default="Hyperliquid")
    parser.add_argument("--dex", type=str, default="xyz")
    parser.add_argument("--trade-limit", type=int, default=500)
    parser.add_argument("--lookback-hours", type=int, default=24)
    args = parser.parse_args()

    normalized_address: Optional[str] = None
    if args.address:
        normalized_address = _validate_user_address(args.address)
        if normalized_address != args.address:
            print(
                "Normalized user address from {} to {}".format(
                    args.address, normalized_address
                )
            )

    if args.mode == "user":
        if args.address is None:
            raise ValueError("--address is required when mode is 'user'.")

        # Subscribe to all the user stuff that might be interesting.
        asyncio.run(subscribe_user(normalized_address))
    elif args.mode == "dossier":
        if args.address is None:
            raise ValueError("--address is required when mode is 'dossier'.")

        dossier = build_dossier(
            normalized_address,
            market=args.market,
            dex=args.dex,
            trade_limit=args.trade_limit,
            lookback_hours=args.lookback_hours,
        )
        print(json.dumps(dossier, indent=2, sort_keys=True))


    # Let's just start w/ the basics.


if __name__ == "__main__":
    main()
