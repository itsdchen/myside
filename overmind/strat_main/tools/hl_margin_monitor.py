#! /usr/bin/env python3

"""
Monitors Hyperliquid wallet positions for liquidation risk via WebSocket.

Subscribes to clearinghouseState for real-time position/margin updates and
userFills for liquidation detection. Sends ntfy alerts when margin distance
drops below threshold or a position is liquidated.

Usage:
    python hl_margin_monitor.py --address <wallet> --margin-threshold 0.5
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
import datetime as dt
import websocket
from zoneinfo import ZoneInfo

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.utils.constants import MAINNET_API_URL

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils

WS_ENDPOINT = "wss://api.hyperliquid.xyz/ws"
NY_TZ = ZoneInfo("America/New_York")
SHUTDOWN_HOUR = 18
RECONNECT_DELAY_S = 5
PING_INTERVAL_S = 50
MAX_TOP_UP_SINGLE = 2000
MAX_TOP_UP_TOTAL = 100000
HEARTBEAT_TOP_UP_INTERVAL_S = 3600  # how often to run the pipeline-verification top-up
HEARTBEAT_TOP_UP_AMOUNT = 0.01      # tiny amount added to an isolated position each heartbeat
HEARTBEAT_TOP_UP_MAX_STRIKES = 3    # consecutive heartbeat failures before we alert (HL blips)

logger = logging.getLogger("hl_margin_monitor")


def trade_day(ny_dt):
    """Return the trade date string (YYYYMMDD) for a given NY datetime.
    Day boundary is 18:00 ET."""
    if ny_dt.hour >= 18:
        return (ny_dt + dt.timedelta(days=1)).strftime("%Y%m%d")
    return ny_dt.strftime("%Y%m%d")


def setup_logging(log_dir):
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # Nest email_utils under us, so its records land in our handlers below.
    email_utils.use_parent_logger(logger)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)

    os.makedirs(log_dir, exist_ok=True)
    date_str = trade_day(dt.datetime.now(NY_TZ))
    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"hl_margin_monitor_{date_str}.log"))
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def compute_margin_dist(position):
    """Compute margin distance for an isolated position.

    Returns (margin_dist, details_dict) or (None, None) if not applicable.
    margin_dist starts at 1.0 at entry and reaches 0.0 at liquidation.

    Uses the same formula as live_alerter.py check_isolated_margin.
    """
    if position["leverage"]["type"] != "isolated":
        return None, None

    liq_px = position["liquidationPx"]
    if liq_px is None:
        return None, None
    liq_px = float(liq_px)

    entry_px = float(position["entryPx"])
    szi = float(position["szi"])
    if szi == 0:
        return None, None
    pos_val = float(position["positionValue"])
    margin_used = float(position["marginUsed"])
    leverage = position["leverage"]["value"]
    max_leverage = float(position["maxLeverage"])

    mark_px = abs(pos_val / szi)
    side = 1 if szi > 0 else -1

    # Initial margin and maintenance at entry
    initial_margin = entry_px * abs(szi) / leverage
    maintenance = 0.5 * entry_px * abs(szi) / max_leverage

    # Initial liquidation price at time of entry
    initial_avail_margin = initial_margin - maintenance
    initial_liq_px = entry_px - initial_avail_margin / szi / (1 - side / (2 * max_leverage))

    # Margin distance: 1.0 at entry, 0.0 at liquidation
    denom = entry_px - initial_liq_px
    if denom == 0:
        return None, None
    margin_dist = (mark_px - liq_px) / denom

    # Amount to add to bring margin_dist back to 1.0:
    # Adding $X increases avail_margin by X, so new margin_dist = 1 when
    # avail_margin + X = initial_avail_margin, i.e. X = initial_avail_margin - avail_margin
    avail_margin = margin_used - maintenance
    top_up_amount = max(0.0, initial_avail_margin - avail_margin)
    top_up_amount = round(top_up_amount, 2)

    # Cumulative funding paid since this position was opened (positive = paid,
    # which reduces account value and pushes liq_px toward entry).
    funding_paid = float(position.get("cumFunding", {}).get("sinceOpen", 0.0))

    details = {
        "sym": position["coin"],
        "side": "Long" if szi > 0 else "Short",
        "mark_px": mark_px,
        "entry_px": entry_px,
        "liq_px": liq_px,
        "margin_used": margin_used,
        "pos_val": pos_val,
        "szi": szi,
        "top_up_amount": top_up_amount,
        "funding_paid": funding_paid,
    }
    return margin_dist, details


class DayEnd(Exception):
    pass


class HLMarginMonitor:
    """Monitors Hyperliquid wallet positions for liquidation risk."""

    eps = 0.000001 # for float comparisons

    def __init__(self, address, agent_creds_path, margin_threshold, position_threshold=None,
                 top_up_at=None, max_top_up_single=MAX_TOP_UP_SINGLE,
                 max_top_up_total=MAX_TOP_UP_TOTAL, heartbeat_top_up=False, ntfy="all"):
        if address is not None:
            self.address = address
        else:
            creds_path = os.path.expanduser("~/.creds/.Hyperliquid.creds.json")
            with open(creds_path) as f:
                hyper_creds = json.load(f)
            self.address = hyper_creds["address"]
        self.margin_threshold = margin_threshold
        self.tier_step = 0.1
        self.position_threshold = position_threshold
        self.top_up_at = top_up_at
        self.max_top_up_single = max_top_up_single
        self.max_top_up_total = max_top_up_total
        self.heartbeat_top_up = heartbeat_top_up
        self.ntfy = ntfy
        self.exchange = None
        if top_up_at is not None or heartbeat_top_up:
            with open(agent_creds_path) as f:
                agent_creds = json.load(f)
            agent_wallet = eth_account.Account.from_key(agent_creds["secret_key"])
            master_address = agent_creds.get("account_address", self.address)
            # Subaccounts have no key of their own: the master's agent wallet signs and
            # the target is named in the L1 action's vaultAddress field (HL overloads that
            # field for both vaults and subaccounts; here it's a subaccount, not a vault).
            # When acting on the master itself, vault_address stays None.
            vault_address = None
            if master_address != self.address:
                if address:
                    logger.warning(f"Monitored address {self.address} differs from agent master "
                                   f"{master_address}; treating it as a subaccount and routing "
                                   f"top-ups to it via the vaultAddress field (not a vault).")
                    vault_address = self.address
                else:
                    logger.warning(f"Address mismatch: overriding HL creds address {self.address} "
                                   f"with agent master address {master_address}.")
                    self.address = master_address
            self.exchange = Exchange(agent_wallet, MAINNET_API_URL,
                                     vault_address=vault_address,
                                     perp_dexs=["", "xyz"])
            logger.info(f"Agent wallet {agent_wallet.address} enabled for "
                        f"account/subaccount {self.address}")
            if top_up_at is not None:
                logger.info(f"Auto top-up enabled at margin_dist={top_up_at:.2f}")
                logger.info(f"Top-up budget: {self.max_top_up_total} ({self.max_top_up_single} each)")
            if heartbeat_top_up:
                logger.info(f"Heartbeat top-up enabled: +${HEARTBEAT_TOP_UP_AMOUNT:.2f} to an "
                            f"isolated position every {HEARTBEAT_TOP_UP_INTERVAL_S}s")
        # Track which symbols we've already topped up (reset on recovery)
        self.topped_up = set()
        self.total_topped_up = 0.0
        self.num_topped_up = 0
        # Isolated positions currently open (updated from clearinghouseState), and the
        # last time the heartbeat top-up ran (0 = fire at the first opportunity on startup).
        self.active_isolated_syms = set()
        self.last_heartbeat_top_up = 0.0
        # Consecutive heartbeat top-up failures; we only alert after MAX_STRIKES in a row
        # so a transient HL outage / reconnect doesn't wake everyone up.
        self.heartbeat_strikes = 0
        # Track the lowest tier each symbol has alerted at: sym -> lowest tier value
        self.alerted_margin_tier = {}
        # Shutdown deadline
        now_ny = dt.datetime.now(NY_TZ)
        shutdown = now_ny.replace(hour=SHUTDOWN_HOUR, minute=0, second=0, microsecond=0)
        if now_ny >= shutdown:
            shutdown += dt.timedelta(days=1)
        self.shutdown_time = shutdown

    def _top_up_margin(self, sym, amount):
        """Add margin to an isolated position via the agent wallet."""
        if self.exchange is None:
            return
        remaining_budget = self.max_top_up_total - self.total_topped_up
        if remaining_budget <= 0:
            logger.warning("Daily top-up budget exhausted ($%.2f), skipping %s",
                           self.total_topped_up, sym)
            email_utils.send_ntfy_alert(
                msg=f"Daily top-up budget exhausted (${self.total_topped_up:.2f}), skipping {sym}",
                title="Top-Up Budget Exhausted",
                priority="urgent", to=self.ntfy, rate_limit=60)
            return
        amount = min(amount, self.max_top_up_single, remaining_budget)
        try:
            resp = self.exchange.update_isolated_margin(amount, sym)
            if not isinstance(resp, dict) or resp.get("status") != "ok":
                raise RuntimeError(f"non-ok response: {resp}")
            self.total_topped_up += amount
            self.num_topped_up += 1
            logger.info("Top-up %s +$%.2f response: %s (daily total: $%.2f)",
                        sym, amount, resp, self.total_topped_up)
            msg = (f"{sym} auto topped up +${amount:.2f}\n"
                   f"response: {resp}")
            email_utils.send_ntfy_alert(
                msg=msg, title=f"Margin Top-Up: {sym}",
                priority="default", to=self.ntfy, rate_limit=0)
        except Exception as e:
            logger.error("Failed to top up %s: %s", sym, e)
            email_utils.send_ntfy_alert(
                msg=f"Failed to top up {sym}: {e}",
                title=f"Top-Up FAILED: {sym}",
                priority="high", to=self.ntfy)

    def _heartbeat_top_up(self):
        """Periodically add a tiny amount of margin to an arbitrary isolated position
        to verify the margin top-up pipeline is still working end-to-end.

        On success, only logs and resets the strike counter. On failure (exception or
        non-ok response), bumps the strike counter and only sends an urgent ntfy alert
        once we've hit HEARTBEAT_TOP_UP_MAX_STRIKES failures in a row -- this filters out
        transient HL outages / reconnects that resolve on their own.

        Returns True if the heartbeat is considered settled for this interval (success,
        or nothing to test), and False on failure so the caller retries next ping cycle."""
        if self.exchange is None:
            return True
        syms = sorted(self.active_isolated_syms)
        if not syms:
            logger.info("Heartbeat top-up: no isolated positions to test, skipping")
            return True
        sym = syms[0]
        amount = HEARTBEAT_TOP_UP_AMOUNT
        try:
            resp = self.exchange.update_isolated_margin(amount, sym)
            if not isinstance(resp, dict) or resp.get("status") != "ok":
                raise RuntimeError(f"non-ok response: {resp}")
            logger.info("Heartbeat top-up %s +$%.2f OK: %s", sym, amount, resp)
            self.heartbeat_strikes = 0
            return True
        except Exception as e:
            self.heartbeat_strikes += 1
            logger.error("Heartbeat top-up FAILED for %s (strike %d/%d): %s",
                         sym, self.heartbeat_strikes, HEARTBEAT_TOP_UP_MAX_STRIKES, e)
            if self.heartbeat_strikes >= HEARTBEAT_TOP_UP_MAX_STRIKES:
                # Send to "kdb" regardless of self.ntfy so this noisy-but-low-stakes alert
                # only wakes me, not the whole team.
                email_utils.send_ntfy_alert(
                    msg=f"Heartbeat margin top-up failed {self.heartbeat_strikes}x in a row "
                        f"for {sym}: {e}",
                    title="Heartbeat Top-Up FAILED",
                    priority="urgent", to="kdb", rate_limit=60)
            return False

    def _priority(self, base_priority, pos_value):
        """Downgrade to 'low' if position is below position_threshold."""
        if self.position_threshold is not None and abs(pos_value) < self.position_threshold:
            return "low"
        return base_priority

    def _on_clearinghouse_state(self, data):
        """Process a clearinghouseState update."""
        positions = data["clearinghouseState"]["assetPositions"]
        active_syms = set()
        now_t = time.time()
        log = False
        if now_t - self.last_clearinghouse >= 300: # 5min
            log = True
            self.last_clearinghouse = now_t
        for asset_pos in positions:
            position = asset_pos["position"]
            margin_dist, details = compute_margin_dist(position)
            if margin_dist is None:
                continue

            sym = details["sym"]
            active_syms.add(sym)

            # Determine which tier this margin_dist falls into.
            # Tiers: threshold + step, threshold, threshold - step, threshold - 2*step, ...
            # E.g. threshold=0.5, step=0.1: tiers at 0.6, 0.5, 0.4, 0.3, 0.2, 0.1
            # current_tier: the tier that margin_dist has breached (dropped below).
            # Invariant: current_tier - step <= margin_dist < current_tier.
            # margin_dist=0.65 -> current_tier=None (above all tiers)
            # margin_dist=0.55 -> current_tier=0.6 (hasn't breached 0.5)
            # margin_dist=0.48 -> current_tier=0.5 (breached 0.5)
            # margin_dist=0.35 -> current_tier=0.4 (breached 0.4)
            current_tier = None
            tier = self.margin_threshold + self.tier_step
            while tier > 0 and margin_dist < tier:
                current_tier = tier
                tier -= self.tier_step

            # Alert tier tracks asymmetrically:
            #   Going down: alert tier eagerly follows current_tier
            #   Going up: alert tier lazily follows, staying 1 tier behind current_tier
            # This prevents flicker when margin_dist oscillates around a boundary.
            alert_tier = self.alerted_margin_tier.get(sym)

            if log:
                logger.debug("%s margin_dist=%.3f mark=%.2f entry=%.2f liq=%.2f "
                             "funding_paid=%.2f current_tier=%.2f alert_tier=%.2f",
                             sym, margin_dist, details["mark_px"],
                             details["entry_px"], details["liq_px"],
                             details["funding_paid"],
                             current_tier or 0, alert_tier or 0)

            if (alert_tier is not None
                    and (current_tier is None or current_tier > alert_tier + self.eps)):
                # Going up — update alert tier to 1 tier behind current_tier
                if current_tier is None:
                    new_alert_tier = self.margin_threshold + self.tier_step
                else:
                    new_alert_tier = current_tier - self.tier_step
                if new_alert_tier > alert_tier:
                    self.alerted_margin_tier[sym] = new_alert_tier
                    if new_alert_tier > self.margin_threshold:
                        # Fully recovered
                        del self.alerted_margin_tier[sym]
                        self.topped_up.discard(sym)
                        msg = (f"{sym} {details['side']}\n"
                               f"margin_dist={margin_dist:.3f} "
                               f"(recovered above {self.margin_threshold + self.tier_step:.2f})\n"
                               f"mark={details['mark_px']:.2f} entry={details['entry_px']:.2f} "
                               f"liq={details['liq_px']:.2f}")
                        logger.info("MARGIN RECOVERED: %s", msg.replace('\n', ' '))
                        email_utils.send_ntfy_alert(
                            msg=msg, title=f"Margin Recovered: {sym}",
                            priority="low", to=self.ntfy)

            if (current_tier is not None
                    and current_tier <= self.margin_threshold + self.eps
                    and (alert_tier is None or current_tier < alert_tier - self.eps)):
                # Going down past a new tier — alert
                self.alerted_margin_tier[sym] = current_tier
                msg = (f"{sym} {details['side']} ${details['pos_val']:,.0f}\n"
                       f"margin_dist={margin_dist:.2f} (breached {current_tier:.2f})\n"
                       f"mark={details['mark_px']:.2f} entry={details['entry_px']:.2f} "
                       f"liq={details['liq_px']:.2f}\n"
                       f"pos={details['szi']:.2f} margin_used={details['margin_used']:.2f} "
                       f"funding_paid={details['funding_paid']:.2f}")
                priority = self._priority("high", details["pos_val"])
                logger.warning("MARGIN ALERT [%s]: %s", priority, msg.replace('\n', ' '))
                email_utils.send_ntfy_alert(
                    msg=msg, title=f"Margin Warning: {sym}",
                    priority=priority, to=self.ntfy)

            # Auto top-up if margin_dist drops below top_up_at
            if (self.top_up_at is not None
                    and margin_dist < self.top_up_at
                    and sym not in self.topped_up
                    and details["top_up_amount"] > 0):
                amount = details["top_up_amount"]
                logger.warning("Auto top-up triggered for %s: margin_dist=%.3f, adding $%.2f",
                               sym, margin_dist, amount)
                self.topped_up.add(sym)
                self._top_up_margin(sym, amount)

        # Clear alerts for positions that no longer exist
        for sym in set(self.alerted_margin_tier) - active_syms:
            del self.alerted_margin_tier[sym]
            logger.info("Position %s closed, clearing margin alert", sym)
        self.topped_up &= active_syms
        # Snapshot of isolated positions for the heartbeat top-up (read from ping loop)
        self.active_isolated_syms = active_syms

    def _on_user_fills(self, data):
        """Process a userFills update, checking for liquidations."""
        fills = data.get("fills", [])
        for fill in fills:
            liq = fill.get("liquidation")
            if liq is None:
                continue
            sym = fill["coin"]
            side = fill["side"]
            px = fill["px"]
            sz = fill["sz"]
            mark_px = liq.get("markPx", "?")
            method = liq.get("method", "?")
            liquidated_user = liq.get("liquidatedUser", "")

            # Only alert if we are the one being liquidated
            if liquidated_user.lower() != self.address.lower():
                logger.debug("Ignoring liquidation of other user %s on %s", liquidated_user, sym)
                continue

            pos_notional = float(px) * float(sz)
            msg = (f"{sym} LIQUIDATED\n"
                   f"side={side} sz={sz} px={px}\n"
                   f"mark={mark_px} method={method}")
            priority = self._priority("urgent", pos_notional)
            logger.warning("LIQUIDATION [%s]: %s", priority, msg.replace('\n', ' '))

            email_utils.send_ntfy_alert(
                msg=msg, title=f"LIQUIDATION: {sym}",
                priority=priority, to=self.ntfy)

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            logger.debug("Non-JSON message: %s", message[:200])
            return

        channel = msg.get("channel")
        if channel == "clearinghouseState":
            self._on_clearinghouse_state(msg["data"])
        elif channel == "userFills":
            data = msg["data"]
            if not data.get("isSnapshot", False):
                self._on_user_fills(data)
        elif channel == "pong":
            logger.debug("Received pong")
        elif channel == "subscriptionResponse":
            logger.debug("Subscription confirmed: %s", msg)
        else:
            logger.debug("Ignoring channel: %s", channel)

    def _on_open(self, ws):
        logger.info("WebSocket connected, subscribing for %s", self.address)
        ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "clearinghouseState", "user": self.address, "dex": "xyz"},
        }))
        ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "userFills", "user": self.address},
        }))

    def _on_error(self, ws, error):
        logger.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning("WebSocket closed: code=%s reason=%s", close_status_code, close_msg)

    def _run_ws(self):
        """Run a single WebSocket session. Returns when the connection drops."""
        ws = websocket.WebSocketApp(
            WS_ENDPOINT,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        # Keep track of the last clearinghouseState callback for logging
        self.last_clearinghouse = 0.0

        # Run WS in a thread so we can check shutdown time
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()

        # Send periodic pings (HL requires a message every 60s)
        try:
            while ws_thread.is_alive():
                remaining = (self.shutdown_time - dt.datetime.now(NY_TZ)).total_seconds()
                if remaining <= 0:
                    logger.info("Reached shutdown time %s ET, exiting",
                                self.shutdown_time.strftime("%Y-%m-%d %H:%M"))
                    ws.close()
                    ws_thread.join()
                    raise DayEnd()
                # Sleep for ping interval, but not past shutdown
                time.sleep(min(PING_INTERVAL_S, max(remaining, 0)))
                if ws_thread.is_alive():
                    try:
                        ws.send(json.dumps({"method": "ping"}))
                    except Exception:
                        break
                if (self.heartbeat_top_up
                        and time.time() - self.last_heartbeat_top_up >= HEARTBEAT_TOP_UP_INTERVAL_S):
                    # Only advance the clock on success; a failure leaves last_heartbeat_top_up
                    # unchanged so we retry next ping cycle (and count strikes before alerting).
                    if self._heartbeat_top_up():
                        self.last_heartbeat_top_up = time.time()
        except KeyboardInterrupt:
            logger.info("Interrupted, shutting down")
            ws.close()
            raise

    def run(self):
        """Main loop: subscribe with auto-reconnect."""
        logger.info("Monitoring margin for %s (threshold=%.2f)",
                    self.address, self.margin_threshold)

        while True:
            try:
                self._run_ws()
            except (KeyboardInterrupt, DayEnd):
                break
            logger.warning("Reconnecting in %ds...", RECONNECT_DELAY_S)
            time.sleep(RECONNECT_DELAY_S)

        # Log some stats before exiting
        logger.info(f"Total top-up: {self.total_topped_up} ({self.num_topped_up} times)")


def main():
    parser = argparse.ArgumentParser(description="Monitor Hyperliquid wallet margin and liquidations")
    parser.add_argument("--address", help="Wallet address to monitor")
    # A single API/agent wallet shares one nonce set across the master and all its
    # subaccounts, so parallel monitors (e.g. one for the master and one for a
    # subaccount) must each use a distinct agent creds file to avoid nonce collisions.
    parser.add_argument("--agent-creds",
                        default=os.path.expanduser("~/.creds/.Hyperliquid.agent.creds.json"),
                        help="Path to agent creds file for topping up margin")
    parser.add_argument("--margin-threshold", type=float, default=0.5,
                        help="Margin distance threshold for alerts (default: 0.5)")
    parser.add_argument("--position-threshold", type=float, default=None,
                        help="Position value below which alerts are downgraded to low priority")
    parser.add_argument("--top-up-at", type=float, default=None,
                        help="Margin distance at which to auto top-up margin back to 1.0")
    parser.add_argument("--max-top-up-single", type=float, default=MAX_TOP_UP_SINGLE,
                        help="Max amount allowed for a single top-up")
    parser.add_argument("--max-top-up-total", type=float, default=MAX_TOP_UP_TOTAL,
                        help="Max total amount allowed for all top-ups during run")
    parser.add_argument("--heartbeat-top-up", action="store_true",
                        help=(f"Every {HEARTBEAT_TOP_UP_INTERVAL_S}s, add "
                              f"${HEARTBEAT_TOP_UP_AMOUNT:.2f} to an arbitrary isolated position "
                              "to verify the top-up pipeline (needs agent creds)"))
    parser.add_argument("--ntfy", default="all",
                        help="ntfy recipient for alerts (default: all)")
    parser.add_argument("--log-dir", default=".",
                        help="Directory for log files (default: current dir)")
    args = parser.parse_args()

    if args.top_up_at is not None and args.top_up_at > args.margin_threshold:
        parser.error("--top-up-at must be <= --margin-threshold")

    setup_logging(args.log_dir)

    monitor = HLMarginMonitor(args.address, args.agent_creds,
                              args.margin_threshold, args.position_threshold,
                              top_up_at=args.top_up_at,
                              max_top_up_single=args.max_top_up_single,
                              max_top_up_total=args.max_top_up_total,
                              heartbeat_top_up=args.heartbeat_top_up,
                              ntfy=args.ntfy)
    monitor.run()


if __name__ == "__main__":
    main()
