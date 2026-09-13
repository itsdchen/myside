#! /usr/bin/env python


"""

Periodically runs on one machine
Should check marign usage per symbol, and if we're close, it'll send an email

Run this in a tmux session. 

"""

import argparse
import commentjson
import datetime as dt
import getpass
import json
import os
import requests
import sys
import time

import logging
import logging.handlers

from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils

def getLogger(log_name, file_name):
    log = logging.getLogger(log_name)
    log.setLevel(logging.DEBUG)

    format = logging.Formatter("%(asctime)s - %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(format)
    log.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(file_name, maxBytes=(1048576*5), backupCount=7)
    fh.setFormatter(format)
    log.addHandler(fh)

    # Nest email_utils under us, so its records land in our handlers.
    email_utils.use_parent_logger(log)
    return log

def diff_pct(base, alt):
    diff = alt - base
    return diff / base * 100 if base else float("nan")

class LiveAlerts:
    """Periodically queries Hyperliquid and check against alert thresholds."""

    def __init__(self, conf, is_testnet, label, address=None, known_syms_fn=None, log_only=False):
        self.conf_fn = conf
        self._load_conf()
        self.is_testnet = is_testnet
        self.label = label
        if address is not None:
            self.address = address
        else:
            if label is None:
                creds_path = "/home/{}/.creds/.Hyperliquid.creds.json".format(getpass.getuser())
            elif label == "OLD":
                creds_path = "/home/{}/.creds/.Hyperliquid.old.creds.json".format(getpass.getuser())
            elif label == "L1G":
                creds_path = "/home/{}/.creds/.Hyperliquid.l1g.creds.json".format(getpass.getuser())
            else:
                raise ValueError(f"Unsuppported label {label}")
            with open(creds_path) as f:
                hyper_creds = json.load(f)
            self.address = hyper_creds.get("subaccount_address", hyper_creds["address"])

        self.known_syms_fn = (
            known_syms_fn or
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_syms.txt")
        )
        with open(self.known_syms_fn) as f:
            # status is either "listed" or "delisted"
            self.known_syms = {sym: status for sym, status in
                               (line.strip().split(',') for line in f)}
        self.active_syms = set()

        endpoint_base = TESTNET_API_URL if self.is_testnet else MAINNET_API_URL
        self.endpt = endpoint_base + "/info"
        self.unit_dex = "xyz"

        self.all_clearing_data_dct = None
        self.dex_clearing_data_dct = None
        self.asset_ctx_data_dct = None
        self.orders_data_dct = None
        self.leverage = {} # per sym

        date_string = dt.date.today().strftime("%Y%m%d")
        self.logger = getLogger("live_alerter", "alerts.{}.log".format(date_string))
        self.log_only = log_only

    def _load_conf(self):
        with open(self.conf_fn) as f:
            self.config = commentjson.load(f)

    def _post_and_parse(self, payload):
        try:
            r = requests.post(self.endpt, json=payload)
            return json.loads(r.text)
        except Exception as e:
            self.logger.error(f"Failed post request with payload {payload}: {e}")
            return None

    def get_info(self):
        # Make overall perps clearinghouseState query and store result
        # Note: this has correct overall account values, but no symbol-specific data
        payload = {
            "type": "clearinghouseState",
            "user": self.address
        }
        self.all_clearing_data_dct = self._post_and_parse(payload)

        # Make clearinghouseState query and store result
        # Note: this has symbol-specific data, but incomprehensible overall account values
        payload = {
            "type": "clearinghouseState",
            "user": self.address,
            "dex": self.unit_dex
        }
        self.dex_clearing_data_dct = self._post_and_parse(payload)

        # Make spotClearinghouseState query and store result
        payload = {
            "type": "spotClearinghouseState",
            "user": self.address
        }
        self.spot_clearing_data_dct = self._post_and_parse(payload)

        # Make metaAndAssetCtxs query and store result
        payload = {
            "type": "metaAndAssetCtxs",
            "dex": self.unit_dex
        }
        self.asset_ctx_data_dct = self._post_and_parse(payload)

        # Make openOrders query and store result
        payload = {
            "type": "openOrders",
            "user": self.address,
            "dex": self.unit_dex
        }
        self.orders_data_dct = self._post_and_parse(payload)

        # Make portfolio query and store result
        payload = {
            "type": "portfolio",
            "user": self.address
        }
        self.portfolio_data_dct = self._post_and_parse(payload)

    def _send_alerts(self, highest_alert_level, subject, text, rate_limit=None):
        # Add label to subject, for both email and ntfy
        if self.label:
            subject += f" ({self.label})"

        # Check if we're suppressing all alerts
        if self.log_only:
            self.logger.info(f"{subject} (suppressing alerts)")
            return

        # Log and get alert config referenced in highest_alert_level
        notifications = {}
        for key in highest_alert_level:
            # Get the thresh of the highest alert level triggered
            thresh = highest_alert_level[key]
            if thresh == "SPECIAL":
                if len(self.config[key]) != 1:
                    self.logger.error(
                        f"Invalid use of SPECIAL thresh for {key}: must have exactly 1 check")
                    return
                notifications[key] = self.config[key][0]
            elif thresh is not None:
                # Get the check corresponding to that thresh, which contains notification specs
                for check in self.config[key]:
                    if check["thresh"] == thresh:
                        notifications[key] = check

        # Send email
        email_receivers = set()
        for key, check in notifications.items():
            if "email" in check:
                email_receivers.add(check["email"])
        if email_receivers:
            email_utils.send_mail(
                subject=subject,
                body=text,
                receiver=','.join(email_receivers),
                rate_limit=rate_limit or 15,
                add_hostname=False
            )
            self.logger.info(f"Sending email alert for {subject}")

        # Send ntfy
        ntfy_receivers = {}
        for key, check in notifications.items():
            if "ntfy" in check:
                receiver = check["ntfy"]
                pri = check.get("pri", None)
                if receiver not in ntfy_receivers:
                    ntfy_receivers[receiver] = pri
                elif ntfy_receivers[receiver] == pri:
                    pass
                else:
                    self.logger.error(f"Conflicting ntfy priorities for {key}, {receiver}")
        for receiver, pri in ntfy_receivers.items():
            email_utils.send_ntfy_alert(
                msg=text,
                title=subject,
                priority=pri,
                to=receiver,
                rate_limit=rate_limit or 15
            )
            self.logger.info(f"Sending ntfy alert for {subject} to {receiver} at pri {pri}")

    def _is_sym_included(self, check, sym):
        if "include_syms" in check:
            if "exclude_syms" in check:
                self.logger.error(f"Check contains both include and exclude syms: {check}")
                return
            return sym in check["include_syms"]
        elif "exclude_syms" in check:
            return sym not in check["exclude_syms"]
        return True

    # Check cross margin available in overall perp account. This is what we can use for topping up
    # margin for isolated-margin positions.
    def check_cross_margin(self):
        if not self.all_clearing_data_dct:
            return

        alert = False
        highest_alert_level = {"cross_margin": None}
        alert_lines = []
        log_lines = []

        withdrawable = float(self.all_clearing_data_dct["withdrawable"])
        margin_summary = self.all_clearing_data_dct["marginSummary"]
        cross_margin_summary = self.all_clearing_data_dct["crossMarginSummary"]
        log_lines.append(f"  withdrawable: {withdrawable}")
        log_lines.append(f"  marginSummary: {margin_summary}")
        log_lines.append(f"  crossMarginSummary: {cross_margin_summary}")

        # Querying clearinghouseState with NO dex specified (what we're looking at here) seems to
        # give us the same value for marginSummary.accountValue, crossMarginSummary.accountValue,
        # and withdrawable, which matches the UI's "Margin available to add." This is presumably
        # also what determines whether we have enough margin available to place new orders.
        # Querying clearinghouseState with dex=xyz gives us weird values for all of the above.
        cross_margin_val = float(cross_margin_summary["accountValue"])

        line = f"  Cross margin account value remaining: {cross_margin_val:.2f}"
        log_lines.append(line)

        for check in self.config["cross_margin"]:
            thresh = check["thresh"]
            if cross_margin_val < thresh:
                alert = True
                if (highest_alert_level["cross_margin"] is None or
                    highest_alert_level["cross_margin"] > thresh): # smaller is more urgent
                    highest_alert_level["cross_margin"] = thresh
                if "loop" in check:
                    self.loop_secs = min(self.loop_secs, check["loop"])
        if alert:
            alert_lines.append(line.strip())

        alert_text = '\n'.join(alert_lines)
        if alert:
            self._send_alerts(highest_alert_level, "CROSS MARGIN ACCOUNT VALUE ALERT", alert_text)
        else:
            self.logger.info("No alert for cross margin account value")
        if log_lines:
            for line in log_lines:
                self.logger.info(line)

        return alert

    # Run margin check
    def check_isolated_margin(self):
        if not self.dex_clearing_data_dct:
            return
        alert = False
        highest_alert_level = {"isolated_margin": None}
        alert_lines = []
        log_lines = []

        for one_coin_position in self.dex_clearing_data_dct["assetPositions"]:
            one_position = one_coin_position["position"]
            # Only act if it's isolated.
            if one_position["leverage"]["type"] != "isolated":
                continue
            # Otherwise, let's calculate.

            # Get relevant values from API
            sym = one_position["coin"]
            entry_px = float(one_position["entryPx"])
            szi = float(one_position["szi"])
            pos_val = float(one_position["positionValue"])
            upnl = float(one_position["unrealizedPnl"])
            liq_px = one_position["liquidationPx"]
            if liq_px is None:
                continue
            liq_px = float(liq_px)
            margin_used = float(one_position["marginUsed"])
            leverage = one_position["leverage"]["value"]
            max_leverage = float(one_position["maxLeverage"])

            # Compute basic derived values
            initial_margin = entry_px * abs(szi) / leverage
            maintenance = 0.5 * entry_px * abs(szi) / max_leverage
            mark_px = abs(pos_val / szi)
            side = 1 if szi > 0 else -1

            # Calculate liquidation price ourselves to sanity check against API version
            # Using formula here: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
            avail_margin = margin_used - maintenance
            liq_px_v2 = mark_px - avail_margin / szi / (1 - side/(2*max_leverage))

            # Now make sure they're close enough
            diff = diff_pct(liq_px, liq_px_v2)
            if abs(diff) > 0.1: # only print if diff of more than 0.1%
                self.logger.debug(f"    ({sym}) Drift in liq_px vs v2: {liq_px} {liq_px_v2}, diff: {diff:.3f}%")

            # Use same formula to calculate initial liquidation price at time of entry
            initial_avail_margin = initial_margin - maintenance
            initial_liq_px = entry_px - initial_avail_margin / szi / (1 - side/(2*max_leverage))

            # Calculate our main margin distance, using price:
            # How far is the current price from liquidation, compared to when we entered?
            margin_dist = (mark_px - liq_px) / (entry_px - initial_liq_px)

            # Calculate a v2 margin distance, using margin:
            # How far is the current margin from liquidation, compared to when we entered?
            margin_dist_v2 = (margin_used - maintenance) / (initial_margin - maintenance)

            # Note: both these distance measures have 2 key properties:
            # 1. They start at 1, and when they reach 0, liquidation is triggered.
            # 2. Adding (removing) margin increases (decreases) the distance, but only in the numerator.

            # Now make sure they're close enough
            diff = diff_pct(margin_dist, margin_dist_v2)
            if abs(diff) > 1: # only print if diff of more than 1%
                self.logger.debug(f"    ({sym}) mark_px {mark_px} entry_px {entry_px} liq_px {liq_px}")
                self.logger.debug(f"    margin_used {margin_used} initial_margin {initial_margin} maintenance {maintenance}")
                self.logger.debug(f"    Drift in margin_dist vs v2: {margin_dist} {margin_dist_v2}, diff: {diff:.3f}%")

            # Add sym to active syms for orders count check, and record leverage
            self.active_syms.add(sym)
            self.leverage[sym] = leverage

            line = (f"  {sym}: MARGIN_DIST {margin_dist:.2f} pos {side*pos_val:.2f} upnl {upnl:.2f} "
                    f"  mark_px {mark_px:.2f} liq_px {liq_px:.2f} "
                    f"  margin_used {margin_used:.2f} maintenance {maintenance:.2f}")
            log_lines.append(line)

            # Determine the highest alert level triggered
            sym_alert = False
            for check in self.config["isolated_margin"]:
                if not self._is_sym_included(check, sym):
                    continue
                if "min_pos" in check and pos_val < check["min_pos"]:
                    continue
                thresh = check["thresh"]
                if margin_dist < thresh:
                    sym_alert = True
                    if (highest_alert_level["isolated_margin"] is None or
                        highest_alert_level["isolated_margin"] > thresh): # smaller is more urgent
                        highest_alert_level["isolated_margin"] = thresh
                    # Uses shortest loop_secs of all triggered alerts that specifies it
                    if "loop" in check:
                        self.loop_secs = min(self.loop_secs, check["loop"])
            if sym_alert:
                alert = True
                alert_lines.append(line.strip())

        # TODO: Automate topping up margin.
        alert_text = '\n'.join(alert_lines)
        if alert:
            self._send_alerts(highest_alert_level, "MARGIN ALERT", alert_text)
        else:
            self.logger.info("No alert for isolated margin")
        if log_lines:
            for line in log_lines:
                self.logger.info(line)

        return alert

    # Run position and upnl check
    def check_isolated_misc(self):
        if not self.dex_clearing_data_dct:
            return
        alert = False
        highest_alert_level = {"position": None, "upnl": None}
        alert_lines = []
        log_lines = []
        rate_limit = {"position": 0, "upnl": 0}

        for one_coin_position in self.dex_clearing_data_dct["assetPositions"]:
            one_position = one_coin_position["position"]
            # Only act if it's isolated.
            if one_position["leverage"]["type"] != "isolated":
                continue
            # Otherwise, let's calculate.

            # Get relevant values from API
            sym = one_position["coin"]
            szi = float(one_position["szi"])
            pos_val = float(one_position["positionValue"])
            upnl = float(one_position["unrealizedPnl"])
            leverage = one_position["leverage"]["value"]

            # Derived values
            side = 1 if szi > 0 else -1
            notional = side * pos_val

            # Add sym to active syms for orders count check
            self.active_syms.add(sym)
            self.leverage[sym] = leverage

            # Prep line to log and send alerts
            line = f"  {sym}: position {notional:.2f} upnl {upnl:.2f}"
            log_lines.append(line)

            # Determine the highest alert levels triggered
            sym_alert = False
            for check in self.config["position"]:
                if not self._is_sym_included(check, sym):
                    continue
                thresh = check["thresh"]
                if pos_val > thresh:
                    sym_alert = True
                    if (highest_alert_level["position"] is None or
                        highest_alert_level["position"] < thresh): # bigger is more urgent
                        highest_alert_level["position"] = thresh
                        if "rate_limit" in check:
                            rate_limit["position"] = check["rate_limit"]
                    # Uses shortest loop_secs of all triggered alerts that specifies it
                    if "loop" in check:
                        self.loop_secs = min(self.loop_secs, check["loop"])
            for check in self.config["upnl"]:
                if not self._is_sym_included(check, sym):
                    continue
                thresh = check["thresh"]
                if upnl < thresh:
                    sym_alert = True
                    if (highest_alert_level["upnl"] is None or
                        highest_alert_level["upnl"] > thresh): # smaller is more urgent
                        highest_alert_level["upnl"] = thresh
                        if "rate_limit" in check:
                            rate_limit["upnl"] = check["rate_limit"]
                    # Uses shortest loop_secs of all triggered alerts that specifies it
                    if "loop" in check:
                        self.loop_secs = min(self.loop_secs, check["loop"])
            if sym_alert:
                alert = True
                alert_lines.append(line.strip())

        alert_text = '\n'.join(alert_lines)
        if alert:
            try:
                # Take the lowest among specified non-zero rate limits
                combined_rate_limit = min(r for r in rate_limit.values() if r)
            except ValueError:
                # No one specified a rate limit
                combined_rate_limit = None
            self._send_alerts(highest_alert_level, "POSITION/UPNL ALERT", alert_text,
                              combined_rate_limit)
        else:
            self.logger.info("No alert for position/upnl")
        if log_lines:
            for line in log_lines:
                self.logger.info(line)

        return alert

    # Run price check
    def check_mark_oracle(self):
        if not self.asset_ctx_data_dct:
            return
        # For symbols in the universe, check if mark and oracle are very off and send an email if so. Note that this can be
        # noisy on the weekends, while we're in hyperp mode, but let's err on the side of caution.
        alert = False
        highest_alert_level = {"mark_oracle": None, "new_syms": None}
        alert_lines = []
        log_lines = []
        alert_new_sym = False
        rate_limit = None

        sym_universe = self.asset_ctx_data_dct[0]["universe"]
        per_sym_state = self.asset_ctx_data_dct[1]

        for idx, one_sym_state in enumerate(per_sym_state):
            sym = sym_universe[idx]["name"]
            is_delisted = sym_universe[idx].get("isDelisted", False)
            listed_str = "delisted" if is_delisted else "listed"
            # In case we sent orders but have no positions yet, self.leverage might not be populated
            # yet, so just assume max leverage for orders margin usage calcs later.
            if sym not in self.leverage:
                self.leverage[sym] = sym_universe[idx]["maxLeverage"]
            oracle = float(one_sym_state["oraclePx"])
            mark = float(one_sym_state["markPx"])
            pct_delta = diff_pct(mark, oracle)

            # Prep line to log and send alerts
            line = f"  {sym}: oracle {oracle:.2f} mark {mark:.2f} pct_delta {pct_delta:.3f}%"
            log_lines.append(line)

            # Other fields we want to just log to track
            day_vlm = one_sym_state["dayNtlVlm"]
            funding = one_sym_state["funding"]
            liq_pxs = one_sym_state["impactPxs"]
            if liq_pxs:
                liq_bid, liq_ask = liq_pxs
            else:
                liq_bid, liq_ask = None, None
            mid_px = one_sym_state["midPx"]
            oi = one_sym_state["openInterest"]
            premium = one_sym_state["premium"]
            prev_day_px = one_sym_state["prevDayPx"]
            extra_line = (f"  {sym}: day_vlm {day_vlm} funding {funding} liq_pxs {liq_bid}x{liq_ask}"
                          f" mid_px {mid_px} oi {oi} premium {premium} prev_day_px {prev_day_px}")
            log_lines.append(extra_line)

            # Determine the highest alert levels triggered
            sym_alert = False
            for check in self.config["new_syms"]:
                # should have just one check, so use the SPECIAL placeholder thresh
                thresh = "SPECIAL"
                if sym not in self.known_syms:
                    sym_alert = True
                    alert_new_sym = True
                    highest_alert_level["new_syms"] = thresh
                    paren = "" if is_delisted else " (ALREADY LISTED!)" # should never happen
                    alert_lines.append(f"NEW SYMBOL FOUND: {sym}{paren}")
                    # add new sym to known syms and write to file
                    self.known_syms[sym] = listed_str
                    with open(self.known_syms_fn, 'a') as f:
                        f.write(f"{sym},{listed_str}\n")
                elif self.known_syms[sym] != listed_str:
                    sym_alert = True
                    alert_new_sym = True
                    highest_alert_level["new_syms"] = thresh
                    if listed_str == "listed":
                        alert_line = f"SYMBOL STARTED TRADING: {sym}"
                    else:
                        alert_line = f"SYMBOL DELISTED: {sym}"
                    alert_lines.append(alert_line)
                    # update known syms status and write to file
                    self.known_syms[sym] = listed_str
                    with open(self.known_syms_fn, 'w') as f:
                        for sym, status in self.known_syms.items():
                            f.write(f"{sym},{status}\n")
            # If not an active symbol, don't check mark/oracle
            if sym in self.active_syms:
                for check in self.config["mark_oracle"]:
                    if not self._is_sym_included(check, sym):
                        continue
                    thresh = check["thresh"]
                    if abs(pct_delta) > thresh:
                        sym_alert = True
                        if (highest_alert_level["mark_oracle"] is None or
                            highest_alert_level["mark_oracle"] < thresh): # bigger is more urgent
                            highest_alert_level["mark_oracle"] = thresh
                            if "rate_limit" in check:
                                rate_limit = check["rate_limit"]
                        # Uses shortest loop_secs of all triggered alerts that specifies it
                        if "loop" in check:
                            self.loop_secs = min(self.loop_secs, check["loop"])
            if sym_alert:
                alert = True
                alert_lines.append(line.strip())

        alert_text = '\n'.join(alert_lines)
        if alert:
            subject = "NEW SYMBOL FOUND" if alert_new_sym else "MARK/ORACLE MISMATCH"
            self._send_alerts(highest_alert_level, subject, alert_text, rate_limit)
        else:
            self.logger.info("No alert for new symbol or mark/oracle")
        if log_lines:
            for line in log_lines:
                self.logger.info(line)

        return alert

    def check_orders_count(self):
        if not self.orders_data_dct:
            return
        # Check that we have outstanding orders on the book, if not we're probably doing something wrong. Process, gateway might be down.
        alert = False
        highest_alert_level = {"min_orders": None, "max_orders": None}
        alert_lines = []
        log_lines = []

        count_per_sym = {sym: 0 for sym in self.active_syms}
        open_margin_per_sym = {sym: 0 for sym in self.active_syms}
        for one_order in self.orders_data_dct:
            coin_name = one_order["coin"]
            order_notional = float(one_order["limitPx"]) * float(one_order["sz"])
            try:
                count_per_sym[coin_name] += 1
                open_margin_per_sym[coin_name] += order_notional / self.leverage[coin_name]
            except KeyError as e:
                # Probably a symbol we have orders out for but no position in yet
                self.active_syms.add(coin_name)
                count_per_sym[coin_name] = 1
                open_margin_per_sym[coin_name] = order_notional / self.leverage[coin_name]

        #print(json.dumps(self.orders_data_dct, indent=2))

        tot_open_margin = 0.

        for one_sym, one_count in count_per_sym.items():
            # Prep line to log and send alerts
            open_margin = open_margin_per_sym[one_sym]
            tot_open_margin += open_margin
            line = f"  {one_sym}: {one_count} orders using {open_margin:.2f} margin"
            log_lines.append(line)

            # Determine the highest alert levels triggered
            sym_alert = False
            for check in self.config["min_orders"]:
                if not self._is_sym_included(check, one_sym):
                    continue
                thresh = check["thresh"]
                if one_count < thresh:
                    sym_alert = True
                    if (highest_alert_level["min_orders"] is None or
                        highest_alert_level["min_orders"] > thresh): # smaller is more urgent
                        highest_alert_level["min_orders"] = thresh
                    # Uses shortest loop_secs of all triggered alerts that specifies it
                    if "loop" in check:
                        self.loop_secs = min(self.loop_secs, check["loop"])
            for check in self.config["max_orders"]:
                if not self._is_sym_included(check, one_sym):
                    continue
                thresh = check["thresh"]
                if one_count > thresh:
                    sym_alert = True
                    if (highest_alert_level["max_orders"] is None or
                        highest_alert_level["max_orders"] < thresh): # bigger is more urgent
                        highest_alert_level["max_orders"] = thresh
                    # Uses shortest loop_secs of all triggered alerts that specifies it
                    if "loop" in check:
                        self.loop_secs = min(self.loop_secs, check["loop"])
            if sym_alert:
                alert = True
                alert_lines.append(line.strip())

        # Log total margin used by open orders
        tot_open_margin_line = f"  Total margin used by open orders: {tot_open_margin:.2f}"
        log_lines.append(tot_open_margin_line)
        if alert_lines:
            alert_lines.append(tot_open_margin_line)

        alert_text = '\n'.join(alert_lines)
        if alert:
            self._send_alerts(highest_alert_level, "LOW/HIGH ORDER COUNT", alert_text)
        else:
            self.logger.info("No alert for orders")
        if log_lines:
            for line in log_lines:
                self.logger.info(line)

        return alert

    def check_portfolio(self):
        if not self.portfolio_data_dct:
            return
        if not self.spot_clearing_data_dct:
            return
        # print(self.portfolio_data_dct)
        day_list = self.portfolio_data_dct[0]
        if day_list[0] != "day":
            self.logger.error(f"Unrecognized format for portfolio day data: {day_list}")
            return
        _, total_portfolio = day_list[1]["accountValueHistory"][-1]
        perp_day_list = self.portfolio_data_dct[4]
        if perp_day_list[0] != "perpDay":
            self.logger.error(f"Unrecognized format for portfolio perp day data: {perp_day_list}")
            return
        _, perp_portfolio = perp_day_list[1]["accountValueHistory"][-1]
        for balance in self.spot_clearing_data_dct["balances"]:
            if balance["coin"] == "USDC":
                spot_portfolio = balance["total"]
                break
        staking_account = float(total_portfolio) - float(perp_portfolio) - float(spot_portfolio)
        self.logger.info(f"PORTFOLIO VALUE {total_portfolio}")
        self.logger.info(f"PERP PORTFOLIO VALUE {perp_portfolio}")
        self.logger.info(f"SPOT PORTFOLIO VALUE {spot_portfolio}")
        self.logger.info(f"STAKING PORTFOLIO VALUE {staking_account}")

    # Run all checks in a loop
    def run_checks(self, loop_secs):
        base_loop_secs = loop_secs
        while True:
            # Reload the conf in case it changed
            self._load_conf()
            self.logger.info("=" * 100)
            self.logger.info(f"Running at {dt.datetime.now()} using config {self.conf_fn}:")
            self.logger.info(self.config)

            # Make all REST queries to get info from HL
            self.get_info()

            # Reset self.loop_secs bc checks may want a shorter one
            self.loop_secs = base_loop_secs

            # Run all checks
            self.check_cross_margin()
            self.check_isolated_margin()
            self.check_isolated_misc()
            self.check_mark_oracle() # should go after isolated margin/misc checks
            self.check_orders_count() # should go after isolated margin/misc checks
            self.check_portfolio()

            # If base_loop_secs is 0, just run once.
            if not base_loop_secs:
                break

            # Otherwise, loop using either base_loop_secs or whatever the checks wanted
            self.logger.info(f"Checking again in {self.loop_secs}s")
            time.sleep(self.loop_secs)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--conf", default="alerts.json")
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--label", choices=["OLD", "L1G"])
    parser.add_argument("--address")
    parser.add_argument("--known-syms-fn")
    parser.add_argument("--loop-secs", default=60*15, type=int)
    parser.add_argument("--log-only", action="store_true")
    args = parser.parse_args()

    live_alerts = LiveAlerts(args.conf, args.testnet, args.label, args.address, args.known_syms_fn,
                             args.log_only)
    live_alerts.run_checks(args.loop_secs)

if __name__ == "__main__":
    main()
