#! /usr/bin/env python3

"""

Reads nasdaq earnings calendar and writes the next earnings dates to 
a file. Note that nasdaq can be wrong, so what we'll do is 
make a csv of the next known earnings for the symbol, and tell you if 
there was a diff. We look out for the next 2 weeks. 

API endpoint: api.nasdaq.com


"""

import argparse
import requests
import json
import datetime as dt
from typing import List, Dict, Optional
import subprocess
import time
import pytz
import re

import os
import sys
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils
from util import symbolizer
import earnings_derisk

# Nasdaq's market-timing markers in the "time" field.
AMC = "time-after-hours"     # after market close (the timing earnings_derisk.py acts on)
BMO = "time-pre-market"      # before market open
UNKNOWN = "time-not-supplied"


def next_trading_day(d):
    """Next weekday after d (ignores holidays -- good enough for a heads-up)."""
    nd = d + dt.timedelta(days=1)
    while nd.weekday() >= 5:
        nd += dt.timedelta(days=1)
    return nd


class NasdaqEarnings:
    """
    Fetches earnings data from NASDAQ API
    """

    def __init__(self, debug=False):
        self.debug = debug
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Origin': 'https://www.nasdaq.com',
            'Referer': 'https://www.nasdaq.com/',
        })
        self.base_url = "https://api.nasdaq.com/api"


    def fetch_earnings_calendar(self, date) -> List[Dict]:
        """
        Fetch earnings calendar for a specific date
        """
        earnings_syms = []

        url = f"{self.base_url}/calendar/earnings"
        params = {
            'date': date
        }

        try:
            if self.debug:
                print(f"Fetching calendar URL: {url}")
                print(f"Params: {params}")

            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()

            if self.debug:
                print(f"Response status: {response.status_code}")

            data = response.json()

            if self.debug:
                print(f"Response keys: {list(data.keys())}")

            # Parse calendar data
            if data.get('status', {}).get('rCode') == 200:
                calendar_data = data.get('data', {})

                # Extract rows/events
                rows = calendar_data.get('rows', [])
                rows = rows or [] # sometimes it's None

                for row in rows:
                    row_dct = self._parse_calendar_row(row)
                    if row_dct:
                        earnings_syms.append(row_dct)

        except Exception as e:
            print(f"Error fetching calendar: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()

        return earnings_syms

    def _parse_calendar_row(self, row: Dict) -> Optional[Dict]:
        """
        Parse a calendar row from NASDAQ API
        """
        try:
            sym = row["symbol"]
            tod = row["time"]
            return {"sym": sym, "tod": tod}

        except Exception as e:
            if self.debug:
                print(f"Error parsing calendar row: {e}")
            return None

def main():
    parser = argparse.ArgumentParser(description="Fetch earnings from NASDAQ")
    parser.add_argument("--n-days-ahead", type=int, default=14, help="Number of days to look ahead")
    parser.add_argument("--email", action="store_true", help="Send email on changes or near-term earnings")
    parser.add_argument("--scp-dest", action="append", default=[],
                        help="rsync next_earnings.csv and sector_map.json to this destination "
                             "after running (e.g. ubuntu@gf1:/home/ubuntu/estrader/earnings/). "
                             "Only changed files are copied. Repeatable for multiple hosts.")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    args = parser.parse_args()

    nasdaq = NasdaqEarnings(debug=args.debug)
    all_events = []

    all_dates = []

    for i in range(args.n_days_ahead):
        date = dt.datetime.now() + dt.timedelta(days=i)
        # Only count weekdays. 
        if date.weekday() < 5:
            all_dates.append(date.strftime("%Y-%m-%d"))

    dates_to_earnings_syms = {}
    syms_to_earnings = {}

    for one_date in all_dates:
        print("Fetching {}".format(one_date))
        earnings_syms = nasdaq.fetch_earnings_calendar(date=one_date)
        # Filter for symbols we actually care about. 

        our_eq_universe = set(sym for sym, mkt in symbolizer.syms_to_market.items()
                              if mkt == "TopBookEquity")

        wanted_earnings_syms = [row_dct for row_dct in earnings_syms
                                if row_dct["sym"] in our_eq_universe]

        for row_dct in wanted_earnings_syms:
            sym = row_dct["sym"]
            tod = row_dct["tod"]
            syms_to_earnings[sym] = (one_date, tod)
        # Sleep a little to not get rate limited. 
        time.sleep(1)
    

    print("Found earnings dates: {}".format(syms_to_earnings))


    # Write it to file. 
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, "../../for_live/next_earnings.csv")
    sector_map_path = os.path.join(script_dir, "../../for_live/sector_map.json")
    
    # Read in the previously written files. 
    prev_syms_to_earnings = {}
    if os.path.exists(csv_path):
        with open(csv_path, 'r') as f:
            for line in f:
                date, sym, tod = line.strip().split(",")
                prev_syms_to_earnings[sym] = (date, tod)

    new_earnings_found = {}
    updated_earnings_found = {}
    for sym, date in syms_to_earnings.items():
        if sym not in prev_syms_to_earnings:
            new_earnings_found[sym] = date
        elif prev_syms_to_earnings[sym] != syms_to_earnings[sym]:
            updated_earnings_found[sym] = date
        # Anyway, write it out. 
        prev_syms_to_earnings[sym] = syms_to_earnings[sym]

    changed = bool(new_earnings_found or updated_earnings_found)
    if changed:
        print(f"Writing to {csv_path}")
        out_rows = []
        for sym, (date, tod) in prev_syms_to_earnings.items():
            out_rows.append(f"{date},{sym},{tod}\n")
        out_rows.sort() # keep the file sorted by date, then alphabetical by symbol
        with open(csv_path, 'w') as f:
            f.write(''.join(out_rows))
    else:
        print("No change")

    # Copy the CSV and the sector map (both source-of-truth in repo) to the trading host(s)
    # so earnings_derisk.py there has the latest copies. Done every run so a host that lost
    # one recovers. rsync -a (not scp) preserves mtimes and skips unchanged files (its quick
    # check compares size + mtime), so the dest mtime reflects when the content last changed.
    if args.scp_dest:
        for path in (csv_path, sector_map_path):
            if not os.path.exists(path):
                continue
            for dest in args.scp_dest:
                try:
                    subprocess.run(["rsync", "-a", path, dest], check=True)
                    print(f"rsync'd {path} -> {dest}")
                except Exception as e:
                    print(f"WARN: rsync of {path} to {dest} failed: {e}")

    # Heads-up: today's AMC/unknown earnings (de-risked automatically if AMC) and the next
    # trading morning's BMO/unknown earnings (not de-risked -- handle manually).
    eastern = pytz.timezone("US/Eastern")
    today_str = dt.datetime.now(eastern).date().isoformat()
    next_str = next_trading_day(dt.datetime.now(eastern).date()).isoformat()
    # Sector map (best-effort) so each TODAY/NEXT MORNING row can name the sector(s) it belongs to.
    sector_map = {}
    if os.path.exists(sector_map_path):
        try:
            sector_map = earnings_derisk.load_sector_map(sector_map_path)
        except Exception as e:
            print(f"WARN: could not load sector map {sector_map_path}: {e}")

    def annotate(date, sym, tod):
        # AMC is de-risked (and, via sector contagion, so are its listed sector peers);
        # UNKNOWN timing is never auto-de-risked -- flag it for a manual check.
        label = "auto-de-risked" if tod == AMC else "MANUAL"
        xyz = earnings_derisk.ticker_to_xyz(sym)
        sectors = earnings_derisk.sectors_containing(xyz, sector_map)
        row = f"{date},{sym},{tod} -- {label}"
        if sectors:
            row += ", " + ", ".join(sorted(sectors))
        return row

    today_rows = sorted(annotate(date, sym, tod)
                        for sym, (date, tod) in prev_syms_to_earnings.items()
                        if date == today_str and tod in (AMC, UNKNOWN))
    nextam_rows = sorted(annotate(date, sym, tod)
                         for sym, (date, tod) in prev_syms_to_earnings.items()
                         if date == next_str and tod in (BMO, UNKNOWN))

    body = []
    if new_earnings_found:
        body.append("New earnings found:")
        body += [f"{date},{sym},{tod}" for sym, (date, tod) in new_earnings_found.items()]
    if updated_earnings_found:
        body.append("Updated earnings found:")
        body += [f"{date},{sym},{tod}" for sym, (date, tod) in updated_earnings_found.items()]
    if today_rows:
        if body:
            body.append("")
        body.append(f"Earnings TODAY ({today_str}):")
        body += today_rows
    if nextam_rows:
        if body:
            body.append("")
        body.append(f"Earnings NEXT MORNING ({next_str}):")
        body += nextam_rows
    body = '\n'.join(body)
    if body:
        print(body)
        if args.email:
            subject = ("Earnings Heads-Up" if (today_rows or nextam_rows)
                       else "Earnings Calendar Changed")
            print(f"Sending email: {subject}")
            email_utils.send_mail(subject=subject, body=body, add_hostname=True)


if __name__ == "__main__":
    main()
