#! /usr/bin/env python3

import argparse
import csv
import datetime as dt
import json
import os
import requests
import sys
from collections import defaultdict
from dataclasses import dataclass

@dataclass
class Account:
    # Tier 3 (>100M)
    base_fee = {"ADD": 0.00004,
                "REM": 0.00030}
    # Silver (staking >1K HYPE)
    promo_mult = 0.85
    # HIP3 doubles fees
    hip3_mult = 2

    sym = None
    growth = False
    total_fees: float = 0.0
    volume_shares: float = 0.0
    volume_notional: float = 0.0
    volume_scaled: float = 0.0
    tot_entry_notional: float = 0.0
    avg_entry_px: float = 0.0
    position: float = 0.0
    position_notional: float = 0.0
    last_pos: float = 0.0
    open_pnl: float = 0.0
    closed_pnl: float = 0.0
    total_pnl: float = 0.0

    def add_execution(self, sym: str, side: str, price: float, size: float, contra: str, fee=None):
        if self.sym is None:
            self.sym = sym
            self.growth = is_growth_mode(sym)
        elif self.sym != sym:
            raise ValueError(f"Adding execution for {sym} to account for {self.sym}")
        notional = price * size
        if contra == "INT":
            # Internal "trade" so no fees and don't count toward volume
            pass
        else:
            if fee is not None:
                # Use fees directly from trades files
                self.total_fees += fee
            else:
                # Or figure out fees based on contra
                fee = self.base_fee[contra] * self.promo_mult * self.hip3_mult * notional
                self.total_fees += fee
            self.volume_shares += size
            self.volume_notional += notional
            self.volume_scaled += notional * (0.1 if self.growth else 1)

        side_sign = 1 if side == "Buy" else -1
        self.last_pos = self.position
        self.position += size * side_sign
        self.position_notional = self.position * price

        # Update tot_entry_notional
        exec_notional = notional * side_sign
        if 0 < self.last_pos < self.position or 0 > self.last_pos > self.position:
            self.tot_entry_notional += exec_notional
        elif 0 < self.position < self.last_pos or 0 > self.position > self.last_pos:
            self.tot_entry_notional *= (self.position / self.last_pos)
            self.closed_pnl += size * (self.avg_entry_px - price) * side_sign
        else:
            self.tot_entry_notional = self.position * price
            self.closed_pnl += self.last_pos * (price - self.avg_entry_px)

        # Update avg_entry_px
        if self.position:
            self.avg_entry_px = self.tot_entry_notional / self.position

        # Update open pnl (marked to exec price) and total pnl
        self.mark_to_price(price)

    def mark_to_price(self, price: float):
        self.open_pnl = self.position * price - self.tot_entry_notional
        self.total_pnl = self.open_pnl + self.closed_pnl
        self.position_notional = self.position * price

    def __add__(self, other):
        if not isinstance(other, Account):
            raise TypeError(f"Unsupported types for +: 'Account' and '{type(other).__name__}'")
        sum = Account()
        sum.total_pnl = self.total_pnl + other.total_pnl
        sum.closed_pnl = self.closed_pnl + other.closed_pnl
        sum.open_pnl = self.open_pnl + other.open_pnl
        sum.total_fees = self.total_fees + other.total_fees
        sum.volume_notional = self.volume_notional + other.volume_notional
        sum.volume_scaled = self.volume_scaled + other.volume_scaled
        sum.position_notional = self.position_notional + other.position_notional
        return sum

    def __str__(self):
        return (f"{self.total_pnl:>10.2f} "
                f"{self.closed_pnl:>10.2f} "
                f"{self.open_pnl:>10.2f} "
                f"{self.total_fees:>8.2f} "
                f"{self.position:>12.2f} "
                f"{self.position_notional:>12.2f} "
                f"{self.volume_shares:>12.2f} "
                f"{self.volume_notional:>12.2f}{'' if self.growth else '*'}")

def is_growth_mode(sym):
    return sym.startswith("xyz:") and sym not in ["xyz:GOLD", "xyz:MSTR", "xyz:PURRDAT"]

def trade_date(timestamp):
    # Trade day runs [18:00, next 18:00), labeled by the closing calendar date.
    # timestamp looks like "20260630 18:04:03.042000000 EDT".
    d = dt.datetime.strptime(timestamp[:8], "%Y%m%d").date()
    if int(timestamp[9:11]) >= 18:
        d += dt.timedelta(days=1)
    return d.strftime("%Y%m%d")

def parse_tradesfiles(tradesfiles, start_pos, start_px, cur_px, syms, excl_syms, fee_from_csv=False, dated=False):
    accounts = defaultdict(Account)
    rows = []
    time_series = defaultdict(list)

    # If tradesfiles given, read from them
    if tradesfiles:
        for fn in tradesfiles:
            with open(fn) as f:
                reader = csv.reader(f)
                rows += [row for row in reader]
    # Otherwise, read from stdin
    else:
        reader = csv.reader(sys.stdin)
        rows = [row for row in reader]

    # Keep rows sorted by timestamp
    rows.sort(key=lambda row: int(row[1]))

    # If start_px given, use that to mark starting positions
    for symbol, price in start_px.items():
        if syms and symbol not in syms:
            continue
        if excl_syms and symbol in excl_syms:
            continue
        pos = start_pos.pop(symbol) / price
        accounts[symbol].add_execution(symbol, "Buy" if pos > 0 else "Sell", price, abs(pos), "INT")

    for row in rows:
        try:
            timestamp = row[0]
            symbol = row[3]
            side = row[5]
            price = float(row[6])
            size = float(row[7])
            contra = row[20]
        except (ValueError, IndexError):
            continue
        if syms and symbol not in syms:
            continue
        if excl_syms and symbol in excl_syms:
            continue

        # MARK rows are synthetic re-marks (no trade): re-price an existing open
        # position to the given mid and record a time-series point, without touching
        # any execution accounting (volume, fees, avg entry, closed/open split).
        # Symbols we don't currently hold are skipped (membership test avoids
        # autovivifying the defaultdict).
        if contra == "MARK":
            if symbol not in accounts:
                continue
            acct = accounts[symbol]
            acct.mark_to_price(price)
            time_series[symbol].append((timestamp,
                                        acct.position_notional,
                                        acct.volume_notional,
                                        acct.closed_pnl,
                                        acct.total_pnl))
            tot_position_notional = sum(acct.position_notional for _, acct in accounts.items())
            tot_volume_notional = sum(acct.volume_notional for _, acct in accounts.items())
            tot_closed_pnl = sum(acct.closed_pnl for _, acct in accounts.items())
            tot_total_pnl = sum(acct.total_pnl for _, acct in accounts.items())
            time_series["TOTAL"].append((timestamp, tot_position_notional, tot_volume_notional, tot_closed_pnl, tot_total_pnl))
            continue

        # In dated mode, each (symbol, trade-day) pair gets its own account, so a
        # symbol's trades are split across days exactly as if each day were its own file.
        key = (symbol, trade_date(timestamp)) if dated else symbol
        acct = accounts[key]

        # If first time seeing symbol, and it's specified in start_pos, insert internal "exec" if it
        # wasn't already inserted above, just using the exec price
        if symbol in start_pos:
            pos = start_pos.pop(symbol)
            acct.add_execution(symbol, "Buy" if pos > 0 else "Sell", price, abs(pos), "INT")

        fee = float(row[10]) if fee_from_csv else None
        acct.add_execution(symbol, side, price, size, contra, fee=fee)
        time_series[symbol].append((timestamp,
                                    acct.position_notional,
                                    acct.volume_notional,
                                    acct.closed_pnl,
                                    acct.total_pnl))
        tot_position_notional = sum(acct.position_notional for _, acct in accounts.items())
        tot_volume_notional = sum(acct.volume_notional for _, acct in accounts.items())
        tot_closed_pnl = sum(acct.closed_pnl for _, acct in accounts.items())
        tot_total_pnl = sum(acct.total_pnl for _, acct in accounts.items())
        time_series["TOTAL"].append((timestamp, tot_position_notional, tot_volume_notional, tot_closed_pnl, tot_total_pnl))

    # If cur_px given, use that to mark ending positions
    if cur_px:
        timestamp = dt.datetime.now().strftime("%Y%m%d %H:%M:%S.000000000 EST")
        for symbol, price in cur_px.items():
            if syms and symbol not in syms:
                continue
            if excl_syms and symbol in excl_syms:
                continue
            if symbol in accounts:
                acct = accounts[symbol]
                acct.mark_to_price(price)
                # Add a final point to time series for symbol
                time_series[symbol].append((timestamp,
                                            acct.position_notional,
                                            acct.volume_notional,
                                            acct.closed_pnl,
                                            acct.total_pnl))
        # Add a final point to time series for TOTAL
        tot_position_notional = sum(acct.position_notional for _, acct in accounts.items())
        tot_volume_notional = sum(acct.volume_notional for _, acct in accounts.items())
        tot_closed_pnl = sum(acct.closed_pnl for _, acct in accounts.items())
        tot_total_pnl = sum(acct.total_pnl for _, acct in accounts.items())
        time_series["TOTAL"].append((timestamp, tot_position_notional, tot_volume_notional, tot_closed_pnl, tot_total_pnl))

    return (accounts, time_series)

def print_summary(accounts, dated=False):
    # In dated mode keys are (symbol, date) pairs; otherwise plain symbols.
    syms = [key[0] for key in accounts] if dated else list(accounts)
    sym_width = max(10, max((len(sym) for sym in syms), default=0))
    date_width = 8
    date_hdr = f"{'Date':<{date_width}} " if dated else ""
    tot_width = 93 + sym_width + (date_width + 1 if dated else 0)
    print(f"{'Symbol':<{sym_width}} "
          f"{date_hdr}"
          f"{'Total PnL':>10} "
          f"{'Closed PnL':>10} "
          f"{'Open PnL':>10} "
          f"{'Fees':>8} "
          f"{'Pos (shs)':>12} "
          f"{'Pos (not)':>12} "
          f"{'Volume (shs)':>12} "
          f"{'Volume (not)':>12}")
    print("-" * tot_width)

    total = Account()
    # sorted() on (symbol, date) keys groups each symbol's rows together, ordered by date.
    for key, acct in sorted(accounts.items()):
        if dated:
            symbol, date = key
            print(f"{symbol:<{sym_width}} {date:>{date_width}} {acct}")
        else:
            print(f"{key:<{sym_width}} {acct}")
        total += acct
    total.growth = True
    print("-" * tot_width)
    total_date = f"{'':>{date_width}} " if dated else ""
    print(f"{'TOTAL':<{sym_width}} {total_date}{total}")
    print(f"{total.volume_scaled:>{tot_width+1}.2f}*")

def parse_pos_file(f):
    start_pos = {}
    start_px = {}
    for line in f.readlines():
        if line.startswith("***"):
            break
        words = line.split()
        sym = words[0].rstrip(':')
        pos_shs = float(words[2])
        pos_not = float(words[4])
        pos = abs(pos_not) if pos_shs > 0 else -abs(pos_not)
        start_pos[sym] = pos
        if len(words) > 10:
            entry_px = float(words[8])
            mark_px = float(words[10])
            start_px[sym] = mark_px
    # print(start_pos)
    return (start_pos, start_px)

def get_remote_pos(date):
    import paramiko
    client = None
    try:
        config = paramiko.SSHConfig()
        with open(os.path.expanduser("~/.ssh/config")) as f:
            config.parse(f)
        host_config = config.lookup("bfx0")
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.connect(hostname=host_config["hostname"], username=host_config["user"])
        _, stdout, _ = client.exec_command(f"cat /home/ubuntu/estrader/logs/pos_{date}.log")
    except Exception as e:
        print(f"Error retrieving positions from bfx0: {e}")
        return {}
    finally:
        if client:
            client.close()
    # print(start_pos)
    return parse_pos_file(stdout)

def get_current_mids():
    # Only import if needed
    from hyperliquid.utils.constants import MAINNET_API_URL
    endpt = MAINNET_API_URL + "/info"
    cur_mid = {}

    # Get all top-level perps
    payload_all = {"type": "allMids"}
    r_all = requests.post(endpt, json=payload_all, timeout=10,
                          headers={"Content-Type": "application/json"})
    r_all.raise_for_status()
    for sym, px in r_all.json().items():
        cur_mid[sym] = float(px)

    # Get all xyz perps
    payload_xyz = {"type": "allMids", "dex": "xyz"}
    r_xyz = requests.post(endpt, json=payload_xyz, timeout=10,
                          headers={"Content-Type": "application/json"})
    r_xyz.raise_for_status()
    for sym, px in r_xyz.json().items():
        cur_mid[sym] = float(px)

    return cur_mid

def get_current_prices():
    # Only import if needed
    from hyperliquid.utils.constants import MAINNET_API_URL

    # Make the info endpoint query
    endpt = MAINNET_API_URL + "/info"
    payload = {
        "type": "metaAndAssetCtxs",
        "dex": "xyz"
    }
    try:
        r = requests.post(endpt, json=payload)
        asset_ctx_data_dct = json.loads(r.text)
    except Exception as e:
        print(f"Failed post request with payload {payload}: {e}")
        return None
    # Parse the result
    cur_px = {}
    sym_universe = asset_ctx_data_dct[0]["universe"]
    per_sym_state = asset_ctx_data_dct[1]
    for idx, one_sym_state in enumerate(per_sym_state):
        sym = sym_universe[idx]["name"]
        mark_px = float(one_sym_state["markPx"])
        cur_px[sym] = mark_px
    return cur_px

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tradesfiles", nargs='*')
    parser.add_argument("--time-series", action="store_true")
    parser.add_argument("--start-pos", type=str)
    parser.add_argument("--mark-to-current", action="store_true")
    parser.add_argument("--categ")
    parser.add_argument("--fee-from-csv", action="store_true")
    parser.add_argument("--dated", action="store_true",
                        help="Break out a row per (symbol, trade-day), trade-day being [18:00, next 18:00).")
    args = parser.parse_args()

    if args.dated and (args.time_series or args.start_pos or args.mark_to_current):
        sys.exit("--dated cannot be combined with --time-series, --start-pos, or --mark-to-current")

    start_pos = {}
    start_px = {}
    if args.start_pos:
        if os.path.isfile(args.start_pos):
            with open(args.start_pos) as f:
                start_pos, start_px = parse_pos_file(f)
        elif len(args.start_pos) == 8 and args.start_pos.startswith("202"):
            start_pos, start_px = get_remote_pos(args.start_pos)
        else:
            sys.exit("Invalid --start_pos")
    cur_px = {}
    if args.mark_to_current:
        cur_px = get_current_mids()
    syms = None
    excl_syms = None
    if args.categ:
        from split_trades import CAT_TO_SYM
        if args.categ != "other":
            syms = CAT_TO_SYM[args.categ]
        else:
            excl_syms = []
            for categ in CAT_TO_SYM:
                if categ != "other":
                    excl_syms += CAT_TO_SYM[categ]
    accounts, time_series = parse_tradesfiles(args.tradesfiles, start_pos, start_px, cur_px, syms, excl_syms, fee_from_csv=args.fee_from_csv, dated=args.dated)
    if args.time_series:
        symbols = sorted(time_series)
        if "TOTAL" in symbols and len(symbols) == 2:
            symbols.remove("TOTAL")
        for symbol in symbols:
            for timestamp, position_notional, volume_notional, closed_pnl, total_pnl in time_series[symbol]:
                print(f"{symbol},{timestamp},{position_notional:.2f},{volume_notional:.2f},{closed_pnl:.2f},{total_pnl:.2f}")
    else:
        print_summary(accounts, dated=args.dated)

if __name__ == "__main__":
    main()
