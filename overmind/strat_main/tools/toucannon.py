#!/usr/bin/env python

"""Interactive Hyperliquid trade management shell.

Credentials file format (JSON):
{
    "secret_key": "0x...",              # Required: private key for signing
    "subaccount_address": "0x...",      # Optional: subaccount to trade (uses main wallet if omitted)
    "listen_address": "0x..."           # Optional: override address to watch/query
}

Usage examples:
    # Normal mode (trade own account or subaccount)
    python toucannon.py --creds ~/.creds/.Hyperliquid.creds.json

    # Agent mode (creds file contains "listen_address")
    python toucannon.py --creds ~/.creds/.Hyperliquid.agent.json
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import getpass
import json
import os
import shlex
import sys
import textwrap
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import requests
from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import CancelRequest, get_timestamp_ms

try:  # pragma: no cover - optional
    import readline  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - optional
    readline = None

# We base the HTTP endpoints on tools.hl_utils defaults so behaviour stays consistent.
INFO_BASES = {
    "main": "https://api.hyperliquid.xyz/",
    "test": "https://api.hyperliquid-testnet.xyz/",
}

DEFAULT_CREDS_PATH = "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())
DEFAULT_DEX = "xyz"
INFO_TIMEOUT = 10
MARGIN_WARN_THRESHOLD = 0.15  # warn if <= 15% above maintenance
MARGIN_ALERT_THRESHOLD = 0.05  # scream if <= 5% above maintenance
LARGE_ORDER_NOTIONAL_THRESHOLD = 500_000  # confirm IOC/market orders above this USD notional
HISTORY_PATH = os.path.join(os.path.expanduser("~"), ".toucannon_history")


# ---- basic helpers ---------------------------------------------------------------------------

def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_signed(value: Optional[float], precision: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:+.{precision}f}"


def _format_plain(value: Optional[float], precision: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{precision}f}"


def _format_percent(value: Optional[float], precision: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{precision}f}%"


def _strip_prefix(sym: str) -> str:
    if not sym:
        return sym
    if ":" in sym:
        return sym.split(":", 1)[1]
    return sym


def _margin_warning(distance: Optional[float]) -> str:
    if distance is None:
        return ""
    if distance <= MARGIN_ALERT_THRESHOLD:
        return "!!"
    if distance <= MARGIN_WARN_THRESHOLD:
        return "!"
    return ""


# ---- snapshot containers --------------------------------------------------------------------

@dataclass
class PositionSnapshot:
    coin: str
    side: str
    size: float
    notional: float
    entry_px: Optional[float]
    mark_px: Optional[float]
    liq_px: Optional[float]
    upnl: Optional[float]
    margin_used: Optional[float]
    leverage: Optional[float]
    margin_distance: Optional[float]
    leverage_type: Optional[str] = None


@dataclass
class OrderSnapshot:
    oid: Optional[int]
    coin: str
    side: str
    size: Optional[float]
    px: Optional[float]
    tif: str
    reduce_only: Optional[bool]


# ---- Hyperliquid API context ----------------------------------------------------------------

class HyperliquidContext:
    """Wrap Info/Exchange instances with credential handling."""

    def __init__(
        self,
        *,
        testnet: bool,
        dex: Optional[str],
        creds_path: str,
        skip_ws: bool = True,
    ) -> None:
        self.testnet = testnet
        self.dex = dex or DEFAULT_DEX
        self.creds_path = creds_path
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.info_endpoint = (INFO_BASES["test" if testnet else "main"] + "info")
        self.base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        creds = self._load_creds()
        self.wallet: LocalAccount = Account.from_key(creds["secret_key"])
        self.wallet_address = self.wallet.address.lower()

        # Load subaccount address directly from creds (like wsgateway)
        sub = creds.get("subaccount_address")
        self.subaccount_address: Optional[str] = sub.lower() if sub else None

        # Check for explicit listen_address override (agent mode)
        listen_override = creds.get("listen_address")
        if listen_override:
            self.listen_address = listen_override.lower()
            self.vault_address = None  # Don't pass vault in agent mode
            self.is_agent_mode = True
        else:
            # Use subaccount if present, otherwise wallet (like wsgateway)
            self.listen_address = self.subaccount_address or self.wallet_address
            self.vault_address = self.subaccount_address  # Can be None for main account
            self.is_agent_mode = False
        self.skip_ws = skip_ws
        self._info = Info(
            base_url=self.base_url,
            skip_ws=skip_ws,
            perp_dexs=[self.dex],
        )
        # In agent mode, don't pass vault_address - we sign as authorized agent
        self._exchange = Exchange(
            self.wallet,
            self.base_url,
            vault_address=self.vault_address,
            perp_dexs=[self.dex],
        )
        # Set a short timeout to avoid indefinite hangs on stale connections
        self._exchange.timeout = 10

    # exposed API helpers ----------------------------------------------------------------------
    @property
    def exchange(self) -> Exchange:
        return self._exchange

    @property
    def info(self) -> Info:
        return self._info

    def post_info(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(self.info_endpoint, json=payload, timeout=INFO_TIMEOUT)
        response.raise_for_status()
        if response.headers.get("Content-Type", "").startswith("application/json"):
            return response.json()
        return json.loads(response.text)

    def fetch_clearing_state(self) -> Dict[str, Any]:
        return self.post_info({"type": "clearinghouseState", "user": self.listen_address, "dex": self.dex})

    def fetch_open_orders(self) -> List[Dict[str, Any]]:
        response = self.post_info({"type": "openOrders", "user": self.listen_address, "dex": self.dex})
        if isinstance(response, list):
            return response
        return []

    def fetch_all_mids(self) -> Dict[str, float]:
        response = self.post_info({"type": "allMids", "dex": self.dex})
        mids: Dict[str, float] = {}
        if isinstance(response, dict):
            for key, value in response.items():
                fp = _to_float(value)
                if fp is not None:
                    mids[key] = fp
        return mids

    def normalize_coin(self, sym: str) -> str:
        sym = sym.strip()
        if not sym:
            raise ValueError("Symbol must be non-empty")
        if ":" in sym:
            return sym
        return f"{self.dex}:{sym}"

    def update_isolated_margin(self, coin: str, amount: float) -> Any:
        """Update isolated margin using SDK's method."""
        return self._exchange.update_isolated_margin(amount, coin)

    # internal helpers ------------------------------------------------------------------------
    def _load_creds(self) -> Dict[str, Any]:
        if not os.path.exists(self.creds_path):
            raise FileNotFoundError(f"Credentials file not found: {self.creds_path}")
        with open(self.creds_path) as fh:
            return json.load(fh)



# ---- tab completion -------------------------------------------------------------------------

COMMANDS = [
    "cancel", "cancelall", "cxl", "cxlall", "exit", "h", "help",
    "leverage", "lev", "margin", "o", "order", "orders", "ords",
    "place", "pos", "positions", "q", "quit", "r", "refresh",
    "status", "summary", "topup", "watch",
]


class ShellCompleter:
    """Tab completer for command names and symbols."""

    def __init__(self, shell: "TradeShell") -> None:
        self.shell = shell
        self._matches: List[str] = []

    def complete(self, text: str, state: int) -> Optional[str]:
        if state == 0:
            line = readline.get_line_buffer() if readline else ""
            begin = readline.get_begidx() if readline else 0
            self._matches = self._get_matches(text, line, begin)
        if state < len(self._matches):
            return self._matches[state]
        return None

    def _get_matches(self, text: str, line: str, begin: int) -> List[str]:
        # If at start of line, complete commands
        if begin == 0:
            return [cmd for cmd in COMMANDS if cmd.startswith(text.lower())]
        # Otherwise complete symbols (from positions and mids)
        symbols = self._get_known_symbols()
        text_lower = text.lower()
        return [sym for sym in symbols if sym.lower().startswith(text_lower)]

    def _get_known_symbols(self) -> List[str]:
        symbols: List[str] = []
        seen: set[str] = set()
        # Add position symbols (stripped)
        for pos in self.shell.positions:
            stripped = _strip_prefix(pos.coin)
            if stripped.lower() not in seen:
                symbols.append(stripped)
                seen.add(stripped.lower())
        # Add symbols from mids
        for sym in self.shell.last_mids:
            if sym.lower() not in seen:
                symbols.append(sym)
                seen.add(sym.lower())
        return sorted(symbols)


# ---- interactive shell ----------------------------------------------------------------------

class TradeShell:
    def __init__(self, ctx: HyperliquidContext, refresh_interval: float, verbose: bool = False) -> None:
        self.ctx = ctx
        self.refresh_interval = max(0.0, refresh_interval)
        self.clearing_state: Dict[str, Any] = {}
        self.orders: List[Dict[str, Any]] = []
        self.order_counts: Dict[str, Dict[str, Any]] = {}
        self.positions: List[PositionSnapshot] = []
        self.last_refresh: Optional[float] = None
        self.last_mids: Dict[str, float] = {}
        self.verbose = verbose
        self._history_enabled = False
        self._completer: Optional[ShellCompleter] = None
        if readline is not None:
            try:
                readline.read_history_file(HISTORY_PATH)
            except FileNotFoundError:
                pass
            self._history_enabled = True
            atexit.register(self._save_history)
            # Set up tab completion
            self._completer = ShellCompleter(self)
            readline.set_completer(self._completer.complete)
            readline.set_completer_delims(" \t\n")
            readline.parse_and_bind("tab: complete")

    # ---- refresh & formatting ---------------------------------------------------------------
    def refresh(self, *, quiet: bool = False) -> bool:
        try:
            self.clearing_state = self.ctx.fetch_clearing_state()
            self.orders = self.ctx.fetch_open_orders()
            self.order_counts = self._aggregate_order_counts()
            self.last_mids = self.ctx.fetch_all_mids()
            self.positions = self._parse_positions(self.clearing_state)
            self.last_refresh = time.time()
            if not quiet:
                self._print_summary()
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[error] refresh failed: {exc}")
            return False

    def _save_history(self) -> None:
        if readline is None or not self._history_enabled:
            return
        try:
            readline.write_history_file(HISTORY_PATH)
        except Exception:  # pragma: no cover - filesystem issues
            pass

    def _parse_positions(self, clearing: Dict[str, Any]) -> List[PositionSnapshot]:
        results: List[PositionSnapshot] = []
        for entry in clearing.get("assetPositions", []) or []:
            pos = entry.get("position") if isinstance(entry, dict) else entry
            if not isinstance(pos, dict):
                continue
            size = _to_float(pos.get("szi"))
            if not size:
                continue
            coin = pos.get("coin", "")
            notional = _to_float(pos.get("positionValue")) or abs(size)
            entry_px = _to_float(pos.get("entryPx"))
            mark_px = None
            if size:
                mark_px = (notional / abs(size)) if notional is not None else None
            liq_px = _to_float(pos.get("liquidationPx"))
            upnl = _to_float(pos.get("unrealizedPnl"))
            margin_used = _to_float(pos.get("marginUsed"))
            leverage_val = None
            leverage_type = None
            lev_field = pos.get("leverage")
            if isinstance(lev_field, dict):
                leverage_val = _to_float(lev_field.get("value"))
                leverage_type = str(lev_field.get("type")) if lev_field.get("type") else None
                leverage_raw_usd = _to_float(lev_field.get("rawUsd"))
            else:
                leverage_val = _to_float(lev_field)
                leverage_raw_usd = None
            margin_distance = None
            if (
                leverage_type == "isolated"
                and margin_used is not None
            ):
                abs_size = abs(size)
                initial_margin = None
                if entry_px not in (None, 0) and leverage_val not in (None, 0):
                    initial_margin = entry_px * abs_size / leverage_val
                maint_candidates: List[float] = []
                if leverage_raw_usd not in (None, 0):
                    maint_candidates.append(float(leverage_raw_usd))
                max_lev = _to_float(pos.get("maxLeverage"))
                if entry_px not in (None, 0) and max_lev not in (None, 0):
                    maint_candidates.append(0.5 * entry_px * abs_size / max_lev)
                maint = min(maint_candidates) if maint_candidates else None
                if (
                    maint not in (None, 0)
                    and initial_margin not in (None, 0)
                    and initial_margin > maint
                ):
                    margin_distance = (margin_used - maint) / (initial_margin - maint)
                elif maint not in (None, 0):
                    # fallback: compare margin buffer to maintenance directly
                    margin_distance = (margin_used - maint) / maint
            side = "LONG" if size > 0 else "SHORT"
            results.append(
                PositionSnapshot(
                    coin=coin,
                    side=side,
                    size=size,
                    notional=notional,
                    entry_px=entry_px,
                    mark_px=mark_px,
                    liq_px=liq_px,
                    upnl=upnl,
                    margin_used=margin_used,
                    leverage=leverage_val,
                    margin_distance=margin_distance,
                    leverage_type=leverage_type,
                )
            )
        return sorted(results, key=lambda pos: pos.coin)

    def _print_summary(self) -> None:
        timestamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        acct_value = _to_float(self.clearing_state.get("marginSummary", {}).get("accountValue"))
        cross_value = _to_float(self.clearing_state.get("crossMarginSummary", {}).get("accountValue"))
        withdrawable = _to_float(self.clearing_state.get("withdrawable"))
        total_ntl = _to_float(self.clearing_state.get("marginSummary", {}).get("totalNtlPos"))
        print("=" * 96)
        mode_str = ""
        if self.ctx.is_agent_mode:
            mode_str = f" (agent: {self.ctx.wallet_address[:10]}...)"
        print(
            f"[{timestamp}] account {self.ctx.listen_address}{mode_str} | dex {self.ctx.dex} | "
            f"margin { _format_plain(acct_value) } | cross { _format_plain(cross_value) } | "
            f" total_ntl { _format_plain(total_ntl) }"
        )
        print("-- positions --")
        if not self.positions:
            print("  (flat)")
        else:
            header = (
                f"{'warn':<4} {'coin':<14} {'side':<6} {'sz':>9} {'notional':>11} {'entry':>9} "
                f"{'mark':>9} {'liq':>9} {'upnl':>10} {'margin':>9} {'dist%':>8} {'ords':>5}"
            )
            print("  " + header)
            for pos in self.positions:
                order_key = _strip_prefix(pos.coin).lower()
                ords = self.order_counts.get(order_key, {}).get("count", 0)
                warning = _margin_warning(pos.margin_distance)
                print(
                    "  "
                    + f"{warning:<4}"
                    + f"{pos.coin:<14} {pos.side:<6} {pos.size:>9.3f} {pos.notional:>11.1f} "
                    + f"{_format_plain(pos.entry_px):>9} { _format_plain(pos.mark_px):>9} "
                    + f"{ _format_plain(pos.liq_px):>9} { _format_signed(pos.upnl):>10} "
                    + f"{ _format_plain(pos.margin_used):>9} { _format_percent(pos.margin_distance):>8} {ords:>5}"
                )
        print("-- commands --")
        print("  order <sym> <buy|sell> <size> <px> [--tif ...] [--reduce]")
        print(
            "  orders [sym] | cancel <oid> | cancelall <sym> | margin <sym> <amt> | leverage <sym> <val> [mode]"
        )
        print("  watch <seconds> [interval] | refresh | help | quit")

    def _format_orders(self) -> List[OrderSnapshot]:
        formatted: List[OrderSnapshot] = []
        for raw in self.orders:
            if not isinstance(raw, dict):
                continue
            coin = raw.get("coin", "")
            side = "B" if raw.get("isBuy", raw.get("side", "")) in {True, "B", "buy", "BUY"} else "S"
            size = _to_float(raw.get("sz") or raw.get("size"))
            px = _to_float(raw.get("limitPx") or raw.get("px"))
            tif = raw.get("tif") or raw.get("tifType") or raw.get("type", "?")
            reduce_only = raw.get("reduceOnly")
            oid = raw.get("oid")
            snapshot = OrderSnapshot(
                oid=int(oid) if isinstance(oid, (int, float)) else None,
                coin=coin,
                side="BUY" if side == "B" else "SELL",
                size=size,
                px=px,
                tif=str(tif).upper() if isinstance(tif, str) else "?",
                reduce_only=reduce_only,
            )
            formatted.append(snapshot)
        return formatted

    def _aggregate_order_counts(self) -> Dict[str, Dict[str, Any]]:
        counts: Dict[str, Dict[str, Any]] = {}
        for raw in self.orders:
            if not isinstance(raw, dict):
                continue
            coin = raw.get("coin")
            if not coin:
                continue
            key = _strip_prefix(coin).lower()
            entry = counts.setdefault(key, {"count": 0, "label": coin})
            entry["count"] += 1
        return counts

    # ---- REPL --------------------------------------------------------------------------------
    def repl(self) -> None:
        if not self.refresh():
            print("Unable to load initial account state; exiting.")
            return
        while True:
            try:
                raw = input("toucannon> ")
            except (KeyboardInterrupt, EOFError):
                print("\nbye")
                return
            command = raw.strip()
            if not command:
                continue
            if command.lower() in {"quit", "exit", "q"}:
                return
            self._dispatch(command)

    def _dispatch(self, command: str) -> None:
        tokens = shlex.split(command)
        if not tokens:
            return
        cmd, *args = tokens
        cmd = cmd.lower()
        if cmd in {"h", "help", "?"}:
            self._print_help()
        elif cmd in {"r", "refresh"}:
            self.refresh()
        elif cmd in {"status", "summary"}:
            self._print_summary()
        elif cmd in {"pos", "positions"}:
            self._print_summary()
        elif cmd in {"orders", "ords"}:
            sym = args[0] if args else None
            self._print_orders_only(sym)
        elif cmd in {"order", "o", "place"}:
            self._handle_order(args)
        elif cmd in {"cancel", "cxl"}:
            self._handle_cancel(args)
        elif cmd in {"cancelall", "cxlall"}:
            self._handle_cancel_all(args)
        elif cmd in {"margin", "topup"}:
            self._handle_margin(args)
        elif cmd in {"leverage", "lev"}:
            self._handle_leverage(args)
        elif cmd == "watch":
            self._handle_watch(args)
        else:
            print(f"Unknown command '{cmd}'. Type 'help' for options.")

    def _print_help(self) -> None:
        help_text = """
        Commands:
          refresh                     reload account data immediately
          status|positions            print current pnl/positions snapshot
          orders [sym]                list open orders (optional symbol filter)
          order <sym> <buy|sell> <size> <price> [--tif alo|ioc|market] [--reduce]
          cancel <oid>                cancel a single order id
          cancelall <sym>             cancel every order in a symbol
          margin <sym> <amount>       add (>0) or remove (<0) isolated margin in USD
          leverage <sym> <value> [cross|isolated]
          watch <seconds> [interval]  keep refreshing summary for a bit (Ctrl-C to stop)
          help                        show this message
          quit                        exit toucannon
        """
        print(textwrap.dedent(help_text).strip())

    def _print_orders_only(self, sym: Optional[str] = None) -> None:
        target = None
        if sym:
            try:
                target = self.ctx.normalize_coin(sym)
            except Exception as exc:  # noqa: BLE001
                print(f"Invalid symbol: {exc}")
                return
        print("-- open orders --")
        count = 0
        for order in self._format_orders():
            if target and not self._matches_coin({"coin": order.coin}, target):
                continue
            print(self._format_order_line(order))
            count += 1
        if count == 0:
            if target:
                print(f"(none for {target})")
            else:
                print("(none)")

    def _format_order_line(self, order: OrderSnapshot) -> str:
        side = (order.side or "?").upper()
        side_label = side[0] if side else "?"
        size = _format_plain(order.size)
        px = _format_plain(order.px)
        tif = order.tif
        flags: List[str] = []
        if order.reduce_only:
            flags.append("R")
        flag_str = f" ({''.join(flags)})" if flags else ""
        return (
            f"{order.oid or '-':>12} {order.coin:<14} {side_label:<1} "
            f"{size:>10} @ {px:>10} {tif}{flag_str}"
        )

    # ---- command handlers -------------------------------------------------------------------
    def _handle_order(self, args: Sequence[str]) -> None:
        if len(args) < 4:
            print("Usage: order <sym> <buy|sell> <size> <price> [--tif alo|ioc|market] [--reduce]")
            return
        sym_raw, side_raw, size_raw, price_raw, *flags = args
        tif = "alo"
        reduce_only = False
        i = 0
        while i < len(flags):
            flag = flags[i]
            if flag == "--reduce":
                reduce_only = True
            elif flag in ("--tif", "--type"):
                # Handle space-separated: --tif alo
                if i + 1 < len(flags) and not flags[i + 1].startswith("--"):
                    i += 1
                    tif = flags[i].lower()
            elif flag.startswith("--tif=") or flag.startswith("--type="):
                # Handle equals-separated: --tif=alo
                _, _, value = flag.partition("=")
                if value:
                    tif = value.lower()
            i += 1
        try:
            coin = self.ctx.normalize_coin(sym_raw)
            side = side_raw.lower()
            if side not in {"buy", "sell"}:
                raise ValueError("side must be buy or sell")
            size = float(size_raw)
            price = float(price_raw)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid order params: {exc}")
            return
        if size <= 0:
            print("Size must be positive")
            return
        # Sanity check for IOC and market orders with large notional
        if tif in ("market", "ioc"):
            # Use mid price for notional estimate (or limit price if mid unavailable)
            stripped = _strip_prefix(coin)
            mid_px = self.last_mids.get(stripped) or self.last_mids.get(coin) or price
            notional = size * mid_px
            if notional > LARGE_ORDER_NOTIONAL_THRESHOLD:
                if not self._confirm_large_order(tif, side, coin, size, notional):
                    print(f"cancelled {tif.upper()} order")
                    return
        if tif == "market":
            if not self._confirm_market(side, coin, size):
                print("cancelled market order")
                return
        try:
            is_buy = side == "buy"
            tif_payload = self._build_tif_payload(tif, reduce_only)
            if tif == "market":
                order_method = getattr(self.ctx.exchange, "market_order", None)
                if order_method:
                    resp = order_method(coin, is_buy, size)
                else:
                    resp = self.ctx.exchange.order(coin, is_buy, size, price, tif_payload)
            else:
                resp = self.ctx.exchange.order(coin, is_buy, size, price, tif_payload)
            print(f"order response: {resp}")
            self.refresh(quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Order failed: {exc}")

    def _confirm_market(self, side: str, coin: str, size: float) -> bool:
        prompt = f"Confirm MARKET {side.upper()} {coin} size {size}? [y/N]: "
        try:
            ans = input(prompt)
        except (KeyboardInterrupt, EOFError):  # pragma: no cover - interactive only
            print()
            return False
        return ans.strip().lower() in {"y", "yes"}

    def _confirm_large_order(
        self, tif: str, side: str, coin: str, size: float, notional: float
    ) -> bool:
        notional_str = f"${notional:,.0f}"
        prompt = (
            f"WARNING: Large {tif.upper()} order! {side.upper()} {coin} size {size} "
            f"(~{notional_str} notional). Confirm? [y/N]: "
        )
        try:
            ans = input(prompt)
        except (KeyboardInterrupt, EOFError):  # pragma: no cover - interactive only
            print()
            return False
        return ans.strip().lower() in {"y", "yes"}

    def _build_tif_payload(self, tif: str, reduce_only: bool) -> Dict[str, Any]:
        tif = tif.lower()
        tif_key = tif
        if tif in {"alo", "ioc", "gtc"}:
            # HL expects title-case strings ("Alo", "Ioc", "Gtc") for limit tif values
            payload: Dict[str, Any] = {"limit": {"tif": tif.title()}}
        elif tif == "market":
            payload = {"market": {}}
        else:
            raise ValueError(f"Unsupported tif '{tif_key}'")
        if reduce_only:
            payload["reduceOnly"] = True
        return payload

    def _handle_cancel(self, args: Sequence[str]) -> None:
        if not args:
            print("Usage: cancel <oid>")
            return
        try:
            oid = int(args[0])
        except ValueError:
            print("Order id must be an integer")
            return
        try:
            resp = self.ctx.exchange.cancel(None, oid)
            print(f"cancel response: {resp}")
            self.refresh(quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Cancel failed: {exc}")

    def _handle_cancel_all(self, args: Sequence[str]) -> None:
        if not args:
            print("Usage: cancelall <sym>")
            return
        sym = args[0]
        try:
            coin = self.ctx.normalize_coin(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid symbol: {exc}")
            return
        pending = [order for order in self.orders if self._matches_coin(order, coin)]
        if not pending:
            print(f"No outstanding orders found for {coin}")
            return
        cancel_requests: List[CancelRequest] = []
        for order in pending:
            oid = order.get("oid")
            order_coin = order.get("coin", coin)
            if oid is not None:
                cancel_requests.append({"coin": order_coin, "oid": int(oid)})
        if not cancel_requests:
            print(f"No valid order IDs found for {coin}")
            return
        try:
            resp = self.ctx.exchange.bulk_cancel(cancel_requests)
            print(f"bulk cancel {len(cancel_requests)} orders for {coin}: {resp}")
        except Exception as exc:  # noqa: BLE001
            print(f"Bulk cancel failed: {exc}")
        self.refresh(quiet=True)

    def _matches_coin(self, order: Dict[str, Any], target: str) -> bool:
        coin = order.get("coin", "")
        if coin == target:
            return True
        return _strip_prefix(coin).lower() == _strip_prefix(target).lower()

    def _find_position(self, coin: str) -> Optional[PositionSnapshot]:
        target = _strip_prefix(coin).lower()
        for pos in self.positions:
            candidate = _strip_prefix(pos.coin).lower()
            if candidate == target:
                return pos
        return None

    def _handle_margin(self, args: Sequence[str]) -> None:
        if len(args) < 2:
            print("Usage: margin <sym> <amount>")
            return
        sym, amt_raw = args[:2]
        try:
            coin = self.ctx.normalize_coin(sym)
            amount = float(amt_raw)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid margin args: {exc}")
            return
        if amount == 0:
            print("Amount must be non-zero")
            return
        pos = self._find_position(coin)
        if pos is None:
            print(f"No open position found for {coin}")
            return
        if (pos.leverage_type or "").lower() != "isolated":
            print(f"{coin} is cross margin; switch to isolated before adjusting margin")
            return
        try:
            resp = self.ctx.update_isolated_margin(pos.coin, amount)
            print(f"margin response: {resp}")
            self.refresh(quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Margin update failed: {exc}")

    def _handle_leverage(self, args: Sequence[str]) -> None:
        if len(args) < 2:
            print("Usage: leverage <sym> <value> [cross|isolated]")
            return
        sym, value_raw, *rest = args
        try:
            target = int(float(value_raw))
        except ValueError:
            print("Leverage must be a number")
            return
        if target <= 0:
            print("Leverage must be positive")
            return
        mode = "isolated"
        if rest:
            candidate = rest[0].lower()
            if candidate in {"cross", "isolated"}:
                mode = candidate
        try:
            coin = self.ctx.normalize_coin(sym)
            resp = self.ctx.exchange.update_leverage(target, coin, is_cross=(mode == "cross"))
            print(f"leverage response: {resp}")
            self.refresh(quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Leverage update failed: {exc}")

    def _handle_watch(self, args: Sequence[str]) -> None:
        duration = 60.0
        interval = max(5.0, self.refresh_interval or 5.0)
        if args:
            try:
                duration = float(args[0])
            except ValueError:
                pass
        if len(args) > 1:
            try:
                interval = float(args[1])
            except ValueError:
                pass
        end_time = time.time() + max(5.0, duration)
        try:
            while time.time() < end_time:
                self.refresh()
                time.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover - user controlled
            print("\nwatch cancelled")


# ---- argument parsing -----------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hyperliquid trade management shell")
    parser.add_argument("--testnet", action="store_true", help="use testnet endpoints")
    parser.add_argument("--dex", help="perp dex name (default xyz)")
    parser.add_argument("--creds", default=DEFAULT_CREDS_PATH, help="path to .Hyperliquid creds JSON")
    parser.add_argument(
        "--auto-refresh",
        type=float,
        default=10,
        help="seconds between automatic refreshes while using watch (default 10)",
    )
    parser.add_argument("--verbose", action="store_true", help="print raw API payloads/responses")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    try:
        ctx = HyperliquidContext(
            testnet=args.testnet,
            dex=args.dex,
            creds_path=args.creds,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to init Hyperliquid context: {exc}")
        sys.exit(1)
    shell = TradeShell(ctx, refresh_interval=args.auto_refresh, verbose=args.verbose)
    shell.repl()


if __name__ == "__main__":
    main()
