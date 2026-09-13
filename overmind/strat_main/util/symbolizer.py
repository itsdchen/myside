#! /usr/bin/env python

"""
Translates internal symbology to provider symbology (primarily Databento).
Makes it so we can change internal symbology going into the feed, without
dealing with providers.

Futures month codes:
    F=Jan  G=Feb  H=Mar  J=Apr  K=May  M=Jun
    N=Jul  Q=Aug  U=Sep  V=Oct  X=Nov  Z=Dec

Roll blend schedule generation:
    This file also has a helper to generate roll_blend_schedule entries for
    commodity futures. Run it directly to produce dicts you can paste in:

        python symbolizer.py HG 2026                     # uses default holidays
        python symbolizer.py CL 2026 --holidays          # uses no holidays
        python symbolizer.py NG 2026 --holidays 20260302 # uses given holidays

    IMPORTANT: Always check the CME holiday calendar before generating!
    https://www.cmegroup.com/tools-information/holiday-calendar.html
    A holiday before BD9 shifts all subsequent business days forward by one.
    Pass holidays via --holidays:

        python symbolizer.py CL 2026 --holidays 20260119 # MLK Day

    Common CME closures to watch for:
        - New Year's Day (Jan 1)
        - MLK Day (3rd Mon in Jan)
        - Presidents' Day (3rd Mon in Feb)
        - Good Friday (varies, Mar/Apr)
        - Memorial Day (last Mon in May)
        - Juneteenth (Jun 19)
        - Independence Day (Jul 4)
        - Labor Day (1st Mon in Sep)
        - Thanksgiving (4th Thu in Nov)
        - Christmas (Dec 25)

    Roll months per commodity:
        CL / NG: every month (monthly contracts)
        HG:      Feb, Apr, Jun, Aug, Nov (bimonthly, 5 delivery months)
"""

import sys
import os

# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory and parent directory to sys.path
sys.path.insert(0, script_dir)
parent_dir = os.path.join(script_dir, "..")
sys.path.insert(0, parent_dir)

import datetime as dt
import pytz
import chron

# Right now, use the volume-ranked method to get traded contract. It's not going to be as good as doing the proper roll, but I don't want to implement and test that yte.
# Note: databento takes the highest ranked volume from the PREVIOUS day and uses that for the current day.
# So we can miss rolls by one day.
ours_db_map = {
    # Equities
    "ES": "ES.v.0",
    "NQ": "NQ.v.0",
    "YM": "YM.v.0",
    "RTY": "RTY.v.0",

    "CL": "CL.v.0",

    "HG": "HG.v.0",
    "NG": "NG.v.0",

    "SI": "SI.v.0",
    "GC": "GC.v.0",

    "PL": "PL.v.0",
    "PA": "PA.v.0",


    # Index futures
    "NIY": "NIY.v.0",

    # FX
    "6E": "6E.v.0",
    "6J": "6J.v.0",
    "6B": "6B.v.0",
    "6S": "6S.v.0",
    "6A": "6A.v.0",
    "6C": "6C.v.0",

    # Do others later.
}

reverse_db_map = {v: k for k, v in ours_db_map.items()}


def ours_to_databento(symbol: str) -> str:
    """
    Translate our symbology to databento symbology.
    """
    return ours_db_map[symbol]


def databento_to_ours(databento_sym: str) -> str:
    """
    Translate databento symbology to our symbology.
    """
    if databento_sym not in reverse_db_map:
        print("Tried to translate {} but this family isn't in our reverse_db_map!".format(databento_sym))
        sys.exit()

    return reverse_db_map[databento_sym]


# When we start doing real stuff, we'll need to use
# Create the reverse dated map too.

#####################################################
# OK, I'll just make a dated map for these.
# Let's populate this for every combination of things.
# Aligned with roll blend completion date.
dated_db_map = {
    "ES": {
        # Equity index futures roll on the Monday before the third Friday expiry
        # (i.e., the start of expiry week) into the next quarterly H/M/U/Z contract.
        # https://www.cmegroup.com/trading/equity-index/rolldates.html
        # Cross reference with this.
        20250616: "ESU5",
        20250915: "ESZ5",
        20251215: "ESH6",
        20260316: "ESM6",
        20260615: "ESU6",
        20260914: "ESZ6",
        20261214: "ESH7",
        20270315: "ESM7",
        20270614: "ESU7",
        20270913: "ESZ7",
        20271213: "ESH8"
    },

    "NQ": {
        # Same quarterly rule as ES: switch on the Monday leading into the third
        # Friday (expiry) of the delivery month.
        20250616: "NQU5",
        20250915: "NQZ5",
        20251215: "NQH6",
        20260316: "NQM6",
        20260615: "NQU6",
        20260914: "NQZ6",
        20261214: "NQH7",
        20270315: "NQM7",
        20270614: "NQU7",
        20270913: "NQZ7",
        20271213: "NQH8",
    },

    # CME Nikkei 225 Yen futures — quarterly H/M/U/Z, same roll timing as ES/NQ
    # (Monday before the third Friday of the delivery month).
    # https://www.cmegroup.com/markets/equities/nikkei/nikkei-225-yen.contractSpecs.html
    "NIY": {
        20250616: "NIYU5",
        20250915: "NIYZ5",
        20251215: "NIYH6",
        20260316: "NIYM6",
        20260615: "NIYU6",
        20260914: "NIYZ6",
        20261214: "NIYH7",
        20270315: "NIYM7",
        20270614: "NIYU7",
        20270913: "NIYZ7",
        20271213: "NIYH8",
    },

    # CME FX futures roll into the next quarterly contract on the Monday
    # preceding the third Wednesday of the contract month.
    "6E": {
        20251215: "6EH6",
        20260316: "6EM6",
        20260615: "6EU6",
        20260914: "6EZ6",
        20261214: "6EH7",
        20270315: "6EM7",
        20270614: "6EU7",
        20270913: "6EZ7",
        20271213: "6EH8",
    },
    "6J": {
        20251215: "6JH6",
        20260316: "6JM6",
        20260615: "6JU6",
        20260914: "6JZ6",
        20261214: "6JH7",
        20270315: "6JM7",
        20270614: "6JU7",
        20270913: "6JZ7",
        20271213: "6JH8",
    },
    "6B": {
        20251215: "6BH6",
        20260316: "6BM6",
        20260615: "6BU6",
        20260914: "6BZ6",
        20261214: "6BH7",
        20270315: "6BM7",
        20270614: "6BU7",
        20270913: "6BZ7",
        20271213: "6BH8",
    },

    "CL": {
        # Monthly WTI roll: blend starts on BD6 with switch finalized after BD9
        # of the roll month (dates produced by generate_roll_blend_entries).
        20251215: "CLG6",
        20260115: 'CLH6',
        20260213: 'CLJ6',
        20260313: 'CLK6',
        20260415: 'CLM6',
        20260514: 'CLN6',
        20260612: 'CLQ6',
        20260715: 'CLU6',
        20260814: 'CLV6',
        20260915: 'CLX6',
        20261014: 'CLZ6',
        20261113: 'CLF7',
        20261214: 'CLG7',
        20270115: 'CLH7',
        20270212: 'CLJ7',
        20270312: 'CLK7',
        20270414: 'CLM7',
        20270514: 'CLN7',
        20270614: 'CLQ7',
        20270715: 'CLU7',
        20270813: 'CLV7',
        20270915: 'CLX7',
        20271014: 'CLZ7',
        20271112: 'CLF8',
        20271214: 'CLG8'
    },
# https://www.cmegroup.com/markets/metals/base/copper.contractSpecs.html
# Trading terminates at 12:00 Noon CT on the third last business day of the
# contract month, so our roll completes just before that deadline.
    "HG": {
        20260111: "HGH6",
        20260213: 'HGK6',
        20260415: 'HGN6',
        20260612: 'HGU6',
        20260814: 'HGZ6',
        20261113: 'HGH7',
        20270212: 'HGK7',
        20270414: 'HGN7',
        20270614: 'HGU7',
        20270813: 'HGZ7',
        20271112: 'HGH8'
    },

    # TODO: go and review this.
    # https://www.cmegroup.com/markets/metals/precious/gold.calendar.html
    "GC" : {
        # Gold rolls 5 business days before the 3rd-last business day of the
        # contract month (per CME rulebook, taking holidays into account).
        20251118: "GCG6", # Roll from Dec (Z) to Feb (G)
        20260121: "GCJ6", # Roll from Feb (G) to Apr (J)
        20260320: "GCM6", # Roll from Apr (J) to Jun (M)
        20260520: "GCQ6", # Roll from Jun (M) to Aug (Q)
        20260722: "GCZ6", # Roll from Aug (Q) to Dec (Z) - Skips Oct (V)
        20261119: "GCG7", # Roll from Dec (Z) to Feb (G) 2027
        20270120: "GCJ7",
        20270319: "GCM7",
        20270519: "GCQ7",
        20270721: "GCZ7",
        20271118: "GCG8",
    },

    # COMEX Silver (SI) rollover dates
    # https://www.cmegroup.com/markets/metals/precious/silver.calendar.html
    # March, May, July, September, December. There are other monthlies but these are the liquid ones.
    # Don't ask me why.
    # We don't need this to be exact, because the oracle isn't actually using the contract for pricing. And we plan to
    # always just use 1 as the premium_ema_coef.
    "SI": {
        # Silver rolls 6 business days before the 3rd-last business day of the
        # delivery month (again adjusted for CME holidays).
        20251114: "SIH6", # Roll from Dec (Z) to Mar (H)
        20260217: "SIK6", # Roll from Mar (H) to May (K)
        20260420: "SIN6",  # Roll from May (K) to Jul (N)
        20260617: "SIU6", # Roll from Jul (N) to Sep (U)
        20260819: "SIZ6", #  Roll from Sep (U) to Dec (Z)
        20261118: "SIH7", #  Roll from Dec (Z) to Mar (H) 2027
        20270216: "SIK7",
        20270420: "SIN7",
        20270617: "SIU7",
        20270819: "SIZ7",
        20271117: "SIH8",
    },

    # NYMEX Platinum: quarterly J/N/V/F (Apr/Jul/Oct/Jan). Like GC, traders
    # roll well before First Notice Day (FND = first business day of contract
    # month) to avoid delivery risk. Use a roll ~1 week before FND.
    "PL": {
        20250923: "PLF6", # Jan 2026 active from late-Sep roll (PLV5 FND = 2025-10-01)
        20251223: "PLJ6", # Apr 2026 active from late-Dec roll (PLF6 FND = 2026-01-01)
        20260326: "PLN6", # Roll Apr -> Jul (PLJ6 FND = 2026-04-01)
        20260624: "PLV6", # Roll Jul -> Oct (PLN6 FND = 2026-07-01)
        20260924: "PLF7", # Roll Oct -> Jan-2027 (PLV6 FND = 2026-10-01)
        20261222: "PLJ7", # Roll Jan -> Apr-2027
    },

    # NYMEX Palladium: quarterly H/M/U/Z (Mar/Jun/Sep/Dec). Same FND-driven
    # roll convention.
    "PA": {
        20251124: "PAH6", # Mar 2026 active from late-Nov roll (PAZ5 FND = 2025-12-01)
        20260224: "PAM6", # Roll Mar -> Jun (PAH6 FND = 2026-03-02)
        20260525: "PAU6", # Roll Jun -> Sep (PAM6 FND = 2026-06-01)
        20260824: "PAZ6", # Roll Sep -> Dec (PAU6 FND = 2026-09-01)
        20261123: "PAH7", # Roll Dec -> Mar-2027
    },

    # https://www.cmegroup.com/markets/energy/crude-oil/brent-crude-oil-last-day.calendar.html
    # I guess this is this one.
    # TODO: figure out the roll times.
    "BZ": {
        # Brent trades one contract month AHEAD of CL/NG. In January, CL
        # tracks the Feb contract (G) while BZ tracks the March contract (H).
        # This matches the HL BRENTOIL convention per docs.trade.xyz.
        # Same BD6-BD9 blend timing, but shifted one month forward.
        20260115: 'BZJ6',
        20260213: 'BZK6',
        20260313: 'BZM6',
        20260415: 'BZN6',
        20260514: 'BZQ6',
        20260612: 'BZU6',
        20260715: 'BZV6',
        20260814: 'BZX6',
        20260915: 'BZZ6',
        20261014: 'BZF7',
        20261113: 'BZG7',
        20261214: 'BZH7',
        20270115: 'BZJ7',
        20270212: 'BZK7',
        20270312: 'BZM7',
        20270414: 'BZN7',
        20270514: 'BZQ7',
        20270614: 'BZU7',
        20270715: 'BZV7',
        20270813: 'BZX7',
        20270915: 'BZZ7',
        20271014: 'BZF8',
        20271112: 'BZG8',
        20271214: 'BZH8'
    },


    # I guess we're just gonna go with only this one until we need to deal with the next rolls.
    "NG": {
        # Monthly NatGas roll following the same BD6-BD9 blend window as CL.
        20260115: 'NGH26',
        20260213: 'NGJ26',
        20260313: 'NGK26',
        20260415: 'NGM26',
        20260514: 'NGN26',
        20260612: 'NGQ26',
        20260715: 'NGU26',
        20260814: 'NGV26',
        20260915: 'NGX26',
        20261014: 'NGZ26',
        20261113: 'NGF27',
        20261214: 'NGG27',
        20270115: 'NGH27',
        20270212: 'NGJ27',
        20270312: 'NGK27',
        20270414: 'NGM27',
        20270514: 'NGN27',
        20270614: 'NGQ27',
        20270715: 'NGU27',
        20270813: 'NGV27',
        20270915: 'NGX27',
        20271014: 'NGZ27',
        20271112: 'NGF28',
        20271214: 'NGG28'
    }
}

# Holidays used for calculating roll_blend_schedule below.
# Source: https://www.cmegroup.com/trading-hours.html
# CME holidays are identical for Energy and Metals so we don't need to split this by symbol.
# This list goes by holidays with no CME trade date. XYZ might be doing something different, so
# check regularly to make sure these are what XYZ is using.
default_holidays = [
    20260101, # New Year's Day
    20260119, # MLK Day
    20260217, # President's Day
    20260403, # Good Friday (XYZ roll schedule currently doesn't account for this)
    20260525, # Memorial Day
    20260619, # Juneteenth (closes early)
    20260703, # Independence Day (closes early)
    20260907, # Labor Day
    20261126, # Thanksgiving
    # 20261127, # Day after Thanksgiving (closes early, but has a trade date)
    # 20261224, # Christmas Eve (closes early, but has a trade date)
    20261225, # Christmas Day
    20270101, # New Year's Day
    20270118, # MLK Day
    20270215, # President's Day
    20270326, # Good Friday
    20270531, # Memorial Day
    20270618, # Juneteenth (observed)
    20270705, # Independence Day (observed)
    20270906, # Labor Day
    20271125, # Thanksgiving
    20271224, # Christmas (observed)
]

# Roll blending schedule for commodity futures (tradexyz model).
# During roll period, price = front_weight * front_px + (1 - front_weight) * next_px
# Weight transitions happen at 17:30 ET each business day.
# Structured so CL/NG can be added later with the same format.
roll_blend_schedule = {
    "HG": {
        20260209: ('HGH6', 'HGK6', 0.8),
        20260210: ('HGH6', 'HGK6', 0.6),
        20260211: ('HGH6', 'HGK6', 0.4),
        20260212: ('HGH6', 'HGK6', 0.2),
        20260409: ('HGK6', 'HGN6', 0.8),
        20260410: ('HGK6', 'HGN6', 0.6),
        20260411: ('HGK6', 'HGN6', 0.6),
        20260412: ('HGK6', 'HGN6', 0.6),
        20260413: ('HGK6', 'HGN6', 0.4),
        20260414: ('HGK6', 'HGN6', 0.2),
        20260608: ('HGN6', 'HGU6', 0.8),
        20260609: ('HGN6', 'HGU6', 0.6),
        20260610: ('HGN6', 'HGU6', 0.4),
        20260611: ('HGN6', 'HGU6', 0.2),
        20260810: ('HGU6', 'HGZ6', 0.8),
        20260811: ('HGU6', 'HGZ6', 0.6),
        20260812: ('HGU6', 'HGZ6', 0.4),
        20260813: ('HGU6', 'HGZ6', 0.2),
        20261109: ('HGZ6', 'HGH7', 0.8),
        20261110: ('HGZ6', 'HGH7', 0.6),
        20261111: ('HGZ6', 'HGH7', 0.4),
        20261112: ('HGZ6', 'HGH7', 0.2),
        20270208: ('HGH7', 'HGK7', 0.8),
        20270209: ('HGH7', 'HGK7', 0.6),
        20270210: ('HGH7', 'HGK7', 0.4),
        20270211: ('HGH7', 'HGK7', 0.2),
        20270408: ('HGK7', 'HGN7', 0.8),
        20270409: ('HGK7', 'HGN7', 0.6),
        20270410: ('HGK7', 'HGN7', 0.6),
        20270411: ('HGK7', 'HGN7', 0.6),
        20270412: ('HGK7', 'HGN7', 0.4),
        20270413: ('HGK7', 'HGN7', 0.2),
        20270608: ('HGN7', 'HGU7', 0.8),
        20270609: ('HGN7', 'HGU7', 0.6),
        20270610: ('HGN7', 'HGU7', 0.4),
        20270611: ('HGN7', 'HGU7', 0.2),
        20270612: ('HGN7', 'HGU7', 0.2),
        20270613: ('HGN7', 'HGU7', 0.2),
        20270809: ('HGU7', 'HGZ7', 0.8),
        20270810: ('HGU7', 'HGZ7', 0.6),
        20270811: ('HGU7', 'HGZ7', 0.4),
        20270812: ('HGU7', 'HGZ7', 0.2),
        20271108: ('HGZ7', 'HGH8', 0.8),
        20271109: ('HGZ7', 'HGH8', 0.6),
        20271110: ('HGZ7', 'HGH8', 0.4),
        20271111: ('HGZ7', 'HGH8', 0.2)
    },

    "CL": {
        20260109: ('CLG6', 'CLH6', 0.8),
        20260110: ('CLG6', 'CLH6', 0.8),
        20260111: ('CLG6', 'CLH6', 0.8),
        20260112: ('CLG6', 'CLH6', 0.6),
        20260113: ('CLG6', 'CLH6', 0.4),
        20260114: ('CLG6', 'CLH6', 0.2),
        20260209: ('CLH6', 'CLJ6', 0.8),
        20260210: ('CLH6', 'CLJ6', 0.6),
        20260211: ('CLH6', 'CLJ6', 0.4),
        20260212: ('CLH6', 'CLJ6', 0.2),
        20260309: ('CLJ6', 'CLK6', 0.8),
        20260310: ('CLJ6', 'CLK6', 0.6),
        20260311: ('CLJ6', 'CLK6', 0.4),
        20260312: ('CLJ6', 'CLK6', 0.2),
        20260409: ('CLK6', 'CLM6', 0.8),
        20260410: ('CLK6', 'CLM6', 0.6),
        20260411: ('CLK6', 'CLM6', 0.6),
        20260412: ('CLK6', 'CLM6', 0.6),
        20260413: ('CLK6', 'CLM6', 0.4),
        20260414: ('CLK6', 'CLM6', 0.2),
        20260508: ('CLM6', 'CLN6', 0.8),
        20260509: ('CLM6', 'CLN6', 0.8),
        20260510: ('CLM6', 'CLN6', 0.8),
        20260511: ('CLM6', 'CLN6', 0.6),
        20260512: ('CLM6', 'CLN6', 0.4),
        20260513: ('CLM6', 'CLN6', 0.2),
        20260608: ('CLN6', 'CLQ6', 0.8),
        20260609: ('CLN6', 'CLQ6', 0.6),
        20260610: ('CLN6', 'CLQ6', 0.4),
        20260611: ('CLN6', 'CLQ6', 0.2),
        20260709: ('CLQ6', 'CLU6', 0.8),
        20260710: ('CLQ6', 'CLU6', 0.6),
        20260711: ('CLQ6', 'CLU6', 0.6),
        20260712: ('CLQ6', 'CLU6', 0.6),
        20260713: ('CLQ6', 'CLU6', 0.4),
        20260714: ('CLQ6', 'CLU6', 0.2),
        20260810: ('CLU6', 'CLV6', 0.8),
        20260811: ('CLU6', 'CLV6', 0.6),
        20260812: ('CLU6', 'CLV6', 0.4),
        20260813: ('CLU6', 'CLV6', 0.2),
        20260909: ('CLV6', 'CLX6', 0.8),
        20260910: ('CLV6', 'CLX6', 0.6),
        20260911: ('CLV6', 'CLX6', 0.4),
        20260912: ('CLV6', 'CLX6', 0.4),
        20260913: ('CLV6', 'CLX6', 0.4),
        20260914: ('CLV6', 'CLX6', 0.2),
        20261008: ('CLX6', 'CLZ6', 0.8),
        20261009: ('CLX6', 'CLZ6', 0.6),
        20261010: ('CLX6', 'CLZ6', 0.6),
        20261011: ('CLX6', 'CLZ6', 0.6),
        20261012: ('CLX6', 'CLZ6', 0.4),
        20261013: ('CLX6', 'CLZ6', 0.2),
        20261109: ('CLZ6', 'CLF7', 0.8),
        20261110: ('CLZ6', 'CLF7', 0.6),
        20261111: ('CLZ6', 'CLF7', 0.4),
        20261112: ('CLZ6', 'CLF7', 0.2),
        20261208: ('CLF7', 'CLG7', 0.8),
        20261209: ('CLF7', 'CLG7', 0.6),
        20261210: ('CLF7', 'CLG7', 0.4),
        20261211: ('CLF7', 'CLG7', 0.2),
        20261212: ('CLF7', 'CLG7', 0.2),
        20261213: ('CLF7', 'CLG7', 0.2),
        20270111: ('CLG7', 'CLH7', 0.8),
        20270112: ('CLG7', 'CLH7', 0.6),
        20270113: ('CLG7', 'CLH7', 0.4),
        20270114: ('CLG7', 'CLH7', 0.2),
        20270208: ('CLH7', 'CLJ7', 0.8),
        20270209: ('CLH7', 'CLJ7', 0.6),
        20270210: ('CLH7', 'CLJ7', 0.4),
        20270211: ('CLH7', 'CLJ7', 0.2),
        20270308: ('CLJ7', 'CLK7', 0.8),
        20270309: ('CLJ7', 'CLK7', 0.6),
        20270310: ('CLJ7', 'CLK7', 0.4),
        20270311: ('CLJ7', 'CLK7', 0.2),
        20270408: ('CLK7', 'CLM7', 0.8),
        20270409: ('CLK7', 'CLM7', 0.6),
        20270410: ('CLK7', 'CLM7', 0.6),
        20270411: ('CLK7', 'CLM7', 0.6),
        20270412: ('CLK7', 'CLM7', 0.4),
        20270413: ('CLK7', 'CLM7', 0.2),
        20270510: ('CLM7', 'CLN7', 0.8),
        20270511: ('CLM7', 'CLN7', 0.6),
        20270512: ('CLM7', 'CLN7', 0.4),
        20270513: ('CLM7', 'CLN7', 0.2),
        20270608: ('CLN7', 'CLQ7', 0.8),
        20270609: ('CLN7', 'CLQ7', 0.6),
        20270610: ('CLN7', 'CLQ7', 0.4),
        20270611: ('CLN7', 'CLQ7', 0.2),
        20270612: ('CLN7', 'CLQ7', 0.2),
        20270613: ('CLN7', 'CLQ7', 0.2),
        20270709: ('CLQ7', 'CLU7', 0.8),
        20270710: ('CLQ7', 'CLU7', 0.8),
        20270711: ('CLQ7', 'CLU7', 0.8),
        20270712: ('CLQ7', 'CLU7', 0.6),
        20270713: ('CLQ7', 'CLU7', 0.4),
        20270714: ('CLQ7', 'CLU7', 0.2),
        20270809: ('CLU7', 'CLV7', 0.8),
        20270810: ('CLU7', 'CLV7', 0.6),
        20270811: ('CLU7', 'CLV7', 0.4),
        20270812: ('CLU7', 'CLV7', 0.2),
        20270909: ('CLV7', 'CLX7', 0.8),
        20270910: ('CLV7', 'CLX7', 0.6),
        20270911: ('CLV7', 'CLX7', 0.6),
        20270912: ('CLV7', 'CLX7', 0.6),
        20270913: ('CLV7', 'CLX7', 0.4),
        20270914: ('CLV7', 'CLX7', 0.2),
        20271008: ('CLX7', 'CLZ7', 0.8),
        20271009: ('CLX7', 'CLZ7', 0.8),
        20271010: ('CLX7', 'CLZ7', 0.8),
        20271011: ('CLX7', 'CLZ7', 0.6),
        20271012: ('CLX7', 'CLZ7', 0.4),
        20271013: ('CLX7', 'CLZ7', 0.2),
        20271108: ('CLZ7', 'CLF8', 0.8),
        20271109: ('CLZ7', 'CLF8', 0.6),
        20271110: ('CLZ7', 'CLF8', 0.4),
        20271111: ('CLZ7', 'CLF8', 0.2),
        20271208: ('CLF8', 'CLG8', 0.8),
        20271209: ('CLF8', 'CLG8', 0.6),
        20271210: ('CLF8', 'CLG8', 0.4),
        20271211: ('CLF8', 'CLG8', 0.4),
        20271212: ('CLF8', 'CLG8', 0.4),
        20271213: ('CLF8', 'CLG8', 0.2)
    },

    "NG": {
        20260109: ('NGG26', 'NGH26', 0.8),
        20260110: ('NGG26', 'NGH26', 0.8),
        20260111: ('NGG26', 'NGH26', 0.8),
        20260112: ('NGG26', 'NGH26', 0.6),
        20260113: ('NGG26', 'NGH26', 0.4),
        20260114: ('NGG26', 'NGH26', 0.2),
        20260209: ('NGH26', 'NGJ26', 0.8),
        20260210: ('NGH26', 'NGJ26', 0.6),
        20260211: ('NGH26', 'NGJ26', 0.4),
        20260212: ('NGH26', 'NGJ26', 0.2),
        20260309: ('NGJ26', 'NGK26', 0.8),
        20260310: ('NGJ26', 'NGK26', 0.6),
        20260311: ('NGJ26', 'NGK26', 0.4),
        20260312: ('NGJ26', 'NGK26', 0.2),
        20260409: ('NGK26', 'NGM26', 0.8),
        20260410: ('NGK26', 'NGM26', 0.6),
        20260411: ('NGK26', 'NGM26', 0.6),
        20260412: ('NGK26', 'NGM26', 0.6),
        20260413: ('NGK26', 'NGM26', 0.4),
        20260414: ('NGK26', 'NGM26', 0.2),
        20260508: ('NGM26', 'NGN26', 0.8),
        20260509: ('NGM26', 'NGN26', 0.8),
        20260510: ('NGM26', 'NGN26', 0.8),
        20260511: ('NGM26', 'NGN26', 0.6),
        20260512: ('NGM26', 'NGN26', 0.4),
        20260513: ('NGM26', 'NGN26', 0.2),
        20260608: ('NGN26', 'NGQ26', 0.8),
        20260609: ('NGN26', 'NGQ26', 0.6),
        20260610: ('NGN26', 'NGQ26', 0.4),
        20260611: ('NGN26', 'NGQ26', 0.2),
        20260709: ('NGQ26', 'NGU26', 0.8),
        20260710: ('NGQ26', 'NGU26', 0.6),
        20260711: ('NGQ26', 'NGU26', 0.6),
        20260712: ('NGQ26', 'NGU26', 0.6),
        20260713: ('NGQ26', 'NGU26', 0.4),
        20260714: ('NGQ26', 'NGU26', 0.2),
        20260810: ('NGU26', 'NGV26', 0.8),
        20260811: ('NGU26', 'NGV26', 0.6),
        20260812: ('NGU26', 'NGV26', 0.4),
        20260813: ('NGU26', 'NGV26', 0.2),
        20260909: ('NGV26', 'NGX26', 0.8),
        20260910: ('NGV26', 'NGX26', 0.6),
        20260911: ('NGV26', 'NGX26', 0.4),
        20260912: ('NGV26', 'NGX26', 0.4),
        20260913: ('NGV26', 'NGX26', 0.4),
        20260914: ('NGV26', 'NGX26', 0.2),
        20261008: ('NGX26', 'NGZ26', 0.8),
        20261009: ('NGX26', 'NGZ26', 0.6),
        20261010: ('NGX26', 'NGZ26', 0.6),
        20261011: ('NGX26', 'NGZ26', 0.6),
        20261012: ('NGX26', 'NGZ26', 0.4),
        20261013: ('NGX26', 'NGZ26', 0.2),
        20261109: ('NGZ26', 'NGF27', 0.8),
        20261110: ('NGZ26', 'NGF27', 0.6),
        20261111: ('NGZ26', 'NGF27', 0.4),
        20261112: ('NGZ26', 'NGF27', 0.2),
        20261208: ('NGF27', 'NGG27', 0.8),
        20261209: ('NGF27', 'NGG27', 0.6),
        20261210: ('NGF27', 'NGG27', 0.4),
        20261211: ('NGF27', 'NGG27', 0.2),
        20261212: ('NGF27', 'NGG27', 0.2),
        20261213: ('NGF27', 'NGG27', 0.2),
        20270111: ('NGG27', 'NGH27', 0.8),
        20270112: ('NGG27', 'NGH27', 0.6),
        20270113: ('NGG27', 'NGH27', 0.4),
        20270114: ('NGG27', 'NGH27', 0.2),
        20270208: ('NGH27', 'NGJ27', 0.8),
        20270209: ('NGH27', 'NGJ27', 0.6),
        20270210: ('NGH27', 'NGJ27', 0.4),
        20270211: ('NGH27', 'NGJ27', 0.2),
        20270308: ('NGJ27', 'NGK27', 0.8),
        20270309: ('NGJ27', 'NGK27', 0.6),
        20270310: ('NGJ27', 'NGK27', 0.4),
        20270311: ('NGJ27', 'NGK27', 0.2),
        20270408: ('NGK27', 'NGM27', 0.8),
        20270409: ('NGK27', 'NGM27', 0.6),
        20270410: ('NGK27', 'NGM27', 0.6),
        20270411: ('NGK27', 'NGM27', 0.6),
        20270412: ('NGK27', 'NGM27', 0.4),
        20270413: ('NGK27', 'NGM27', 0.2),
        20270510: ('NGM27', 'NGN27', 0.8),
        20270511: ('NGM27', 'NGN27', 0.6),
        20270512: ('NGM27', 'NGN27', 0.4),
        20270513: ('NGM27', 'NGN27', 0.2),
        20270608: ('NGN27', 'NGQ27', 0.8),
        20270609: ('NGN27', 'NGQ27', 0.6),
        20270610: ('NGN27', 'NGQ27', 0.4),
        20270611: ('NGN27', 'NGQ27', 0.2),
        20270612: ('NGN27', 'NGQ27', 0.2),
        20270613: ('NGN27', 'NGQ27', 0.2),
        20270709: ('NGQ27', 'NGU27', 0.8),
        20270710: ('NGQ27', 'NGU27', 0.8),
        20270711: ('NGQ27', 'NGU27', 0.8),
        20270712: ('NGQ27', 'NGU27', 0.6),
        20270713: ('NGQ27', 'NGU27', 0.4),
        20270714: ('NGQ27', 'NGU27', 0.2),
        20270809: ('NGU27', 'NGV27', 0.8),
        20270810: ('NGU27', 'NGV27', 0.6),
        20270811: ('NGU27', 'NGV27', 0.4),
        20270812: ('NGU27', 'NGV27', 0.2),
        20270909: ('NGV27', 'NGX27', 0.8),
        20270910: ('NGV27', 'NGX27', 0.6),
        20270911: ('NGV27', 'NGX27', 0.6),
        20270912: ('NGV27', 'NGX27', 0.6),
        20270913: ('NGV27', 'NGX27', 0.4),
        20270914: ('NGV27', 'NGX27', 0.2),
        20271008: ('NGX27', 'NGZ27', 0.8),
        20271009: ('NGX27', 'NGZ27', 0.8),
        20271010: ('NGX27', 'NGZ27', 0.8),
        20271011: ('NGX27', 'NGZ27', 0.6),
        20271012: ('NGX27', 'NGZ27', 0.4),
        20271013: ('NGX27', 'NGZ27', 0.2),
        20271108: ('NGZ27', 'NGF28', 0.8),
        20271109: ('NGZ27', 'NGF28', 0.6),
        20271110: ('NGZ27', 'NGF28', 0.4),
        20271111: ('NGZ27', 'NGF28', 0.2),
        20271208: ('NGF28', 'NGG28', 0.8),
        20271209: ('NGF28', 'NGG28', 0.6),
        20271210: ('NGF28', 'NGG28', 0.4),
        20271211: ('NGF28', 'NGG28', 0.4),
        20271212: ('NGF28', 'NGG28', 0.4),
        20271213: ('NGF28', 'NGG28', 0.2)
    },

    # Brent trades one contract month AHEAD of CL/NG. Each blend pair
    # is shifted forward one month vs what CL would use on the same dates.
    "BZ": {
        20260109: ('BZH6', 'BZJ6', 0.8),
        20260110: ('BZH6', 'BZJ6', 0.8),
        20260111: ('BZH6', 'BZJ6', 0.8),
        20260112: ('BZH6', 'BZJ6', 0.6),
        20260113: ('BZH6', 'BZJ6', 0.4),
        20260114: ('BZH6', 'BZJ6', 0.2),
        20260209: ('BZJ6', 'BZK6', 0.8),
        20260210: ('BZJ6', 'BZK6', 0.6),
        20260211: ('BZJ6', 'BZK6', 0.4),
        20260212: ('BZJ6', 'BZK6', 0.2),
        20260309: ('BZK6', 'BZM6', 0.8),
        20260310: ('BZK6', 'BZM6', 0.6),
        20260311: ('BZK6', 'BZM6', 0.4),
        20260312: ('BZK6', 'BZM6', 0.2),
        20260409: ('BZM6', 'BZN6', 0.8),
        20260410: ('BZM6', 'BZN6', 0.6),
        20260411: ('BZM6', 'BZN6', 0.6),
        20260412: ('BZM6', 'BZN6', 0.6),
        20260413: ('BZM6', 'BZN6', 0.4),
        20260414: ('BZM6', 'BZN6', 0.2),
        20260508: ('BZN6', 'BZQ6', 0.8),
        20260509: ('BZN6', 'BZQ6', 0.8),
        20260510: ('BZN6', 'BZQ6', 0.8),
        20260511: ('BZN6', 'BZQ6', 0.6),
        20260512: ('BZN6', 'BZQ6', 0.4),
        20260513: ('BZN6', 'BZQ6', 0.2),
        20260608: ('BZQ6', 'BZU6', 0.8),
        20260609: ('BZQ6', 'BZU6', 0.6),
        20260610: ('BZQ6', 'BZU6', 0.4),
        20260611: ('BZQ6', 'BZU6', 0.2),
        20260709: ('BZU6', 'BZV6', 0.8),
        20260710: ('BZU6', 'BZV6', 0.6),
        20260711: ('BZU6', 'BZV6', 0.6),
        20260712: ('BZU6', 'BZV6', 0.6),
        20260713: ('BZU6', 'BZV6', 0.4),
        20260714: ('BZU6', 'BZV6', 0.2),
        20260810: ('BZV6', 'BZX6', 0.8),
        20260811: ('BZV6', 'BZX6', 0.6),
        20260812: ('BZV6', 'BZX6', 0.4),
        20260813: ('BZV6', 'BZX6', 0.2),
        20260909: ('BZX6', 'BZZ6', 0.8),
        20260910: ('BZX6', 'BZZ6', 0.6),
        20260911: ('BZX6', 'BZZ6', 0.4),
        20260912: ('BZX6', 'BZZ6', 0.4),
        20260913: ('BZX6', 'BZZ6', 0.4),
        20260914: ('BZX6', 'BZZ6', 0.2),
        20261008: ('BZZ6', 'BZF7', 0.8),
        20261009: ('BZZ6', 'BZF7', 0.6),
        20261010: ('BZZ6', 'BZF7', 0.6),
        20261011: ('BZZ6', 'BZF7', 0.6),
        20261012: ('BZZ6', 'BZF7', 0.4),
        20261013: ('BZZ6', 'BZF7', 0.2),
        20261109: ('BZF7', 'BZG7', 0.8),
        20261110: ('BZF7', 'BZG7', 0.6),
        20261111: ('BZF7', 'BZG7', 0.4),
        20261112: ('BZF7', 'BZG7', 0.2),
        20261208: ('BZG7', 'BZH7', 0.8),
        20261209: ('BZG7', 'BZH7', 0.6),
        20261210: ('BZG7', 'BZH7', 0.4),
        20261211: ('BZG7', 'BZH7', 0.2),
        20261212: ('BZG7', 'BZH7', 0.2),
        20261213: ('BZG7', 'BZH7', 0.2),
        20270111: ('BZH7', 'BZJ7', 0.8),
        20270112: ('BZH7', 'BZJ7', 0.6),
        20270113: ('BZH7', 'BZJ7', 0.4),
        20270114: ('BZH7', 'BZJ7', 0.2),
        20270208: ('BZJ7', 'BZK7', 0.8),
        20270209: ('BZJ7', 'BZK7', 0.6),
        20270210: ('BZJ7', 'BZK7', 0.4),
        20270211: ('BZJ7', 'BZK7', 0.2),
        20270308: ('BZK7', 'BZM7', 0.8),
        20270309: ('BZK7', 'BZM7', 0.6),
        20270310: ('BZK7', 'BZM7', 0.4),
        20270311: ('BZK7', 'BZM7', 0.2),
        20270408: ('BZM7', 'BZN7', 0.8),
        20270409: ('BZM7', 'BZN7', 0.6),
        20270410: ('BZM7', 'BZN7', 0.6),
        20270411: ('BZM7', 'BZN7', 0.6),
        20270412: ('BZM7', 'BZN7', 0.4),
        20270413: ('BZM7', 'BZN7', 0.2),
        20270510: ('BZN7', 'BZQ7', 0.8),
        20270511: ('BZN7', 'BZQ7', 0.6),
        20270512: ('BZN7', 'BZQ7', 0.4),
        20270513: ('BZN7', 'BZQ7', 0.2),
        20270608: ('BZQ7', 'BZU7', 0.8),
        20270609: ('BZQ7', 'BZU7', 0.6),
        20270610: ('BZQ7', 'BZU7', 0.4),
        20270611: ('BZQ7', 'BZU7', 0.2),
        20270612: ('BZQ7', 'BZU7', 0.2),
        20270613: ('BZQ7', 'BZU7', 0.2),
        20270709: ('BZU7', 'BZV7', 0.8),
        20270710: ('BZU7', 'BZV7', 0.8),
        20270711: ('BZU7', 'BZV7', 0.8),
        20270712: ('BZU7', 'BZV7', 0.6),
        20270713: ('BZU7', 'BZV7', 0.4),
        20270714: ('BZU7', 'BZV7', 0.2),
        20270809: ('BZV7', 'BZX7', 0.8),
        20270810: ('BZV7', 'BZX7', 0.6),
        20270811: ('BZV7', 'BZX7', 0.4),
        20270812: ('BZV7', 'BZX7', 0.2),
        20270909: ('BZX7', 'BZZ7', 0.8),
        20270910: ('BZX7', 'BZZ7', 0.6),
        20270911: ('BZX7', 'BZZ7', 0.6),
        20270912: ('BZX7', 'BZZ7', 0.6),
        20270913: ('BZX7', 'BZZ7', 0.4),
        20270914: ('BZX7', 'BZZ7', 0.2),
        20271008: ('BZZ7', 'BZF8', 0.8),
        20271009: ('BZZ7', 'BZF8', 0.8),
        20271010: ('BZZ7', 'BZF8', 0.8),
        20271011: ('BZZ7', 'BZF8', 0.6),
        20271012: ('BZZ7', 'BZF8', 0.4),
        20271013: ('BZZ7', 'BZF8', 0.2),
        20271108: ('BZF8', 'BZG8', 0.8),
        20271109: ('BZF8', 'BZG8', 0.6),
        20271110: ('BZF8', 'BZG8', 0.4),
        20271111: ('BZF8', 'BZG8', 0.2),
        20271208: ('BZG8', 'BZH8', 0.8),
        20271209: ('BZG8', 'BZH8', 0.6),
        20271210: ('BZG8', 'BZH8', 0.4),
        20271211: ('BZG8', 'BZH8', 0.4),
        20271212: ('BZG8', 'BZH8', 0.4),
        20271213: ('BZG8', 'BZH8', 0.2)
    }
}

#
dex_to_real = {
    "xyz:XYZ100": "NQ",
    "xyz:SP500": "ES",
    "xyz:CL": "CL",
    "xyz:COPPER": "HG",
    "xyz:NATGAS": "NG",
    "xyz:BRENTOIL": "BZ",
    "xyz:GOLD": "GC",
    "xyz:SILVER": "SI",
    "xyz:KR200": "KOSPI200",
    "xyz:JP225": "NIY",

}

real_to_dex = {}
# Invert the mapping.
for k, v in dex_to_real.items():
    real_to_dex[v] = k

# Default RelWide wiring metadata for HIP3 symbols.  This lets tooling build
# reasonable pk configs without scraping saved_strats.
relwide_remote_wiring = {
    "xyz:XYZ100": {
        "remote_symbol": "NQ",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:SP500": {
        "remote_symbol": "ES",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:CL": {
        "remote_symbol": "CL",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:COPPER": {
        "remote_symbol": "HG",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:NATGAS": {
        "remote_symbol": "NG",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:BRENTOIL": {
        "remote_symbol": "BZ",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:GOLD": {
        "remote_symbol": "GC",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:SILVER": {
        "remote_symbol": "SI",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:TSLA": {
        "remote_symbol": "TSLA",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:NVDA": {
        "remote_symbol": "NVDA",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:HOOD": {
        "remote_symbol": "HOOD",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:INTC": {
        "remote_symbol": "INTC",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:PLTR": {
        "remote_symbol": "PLTR",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:COIN": {
        "remote_symbol": "COIN",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:META": {
        "remote_symbol": "META",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:AAPL": {
        "remote_symbol": "AAPL",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:MSFT": {
        "remote_symbol": "MSFT",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:ORCL": {
        "remote_symbol": "ORCL",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:GOOGL": {
        "remote_symbol": "GOOGL",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:AMZN": {
        "remote_symbol": "AMZN",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:AMD": {
        "remote_symbol": "AMD",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:COST": {
        "remote_symbol": "COST",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:NFLX": {
        "remote_symbol": "NFLX",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:CRCL": {
        "remote_symbol": "CRCL",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:MSTR": {
        "remote_symbol": "MSTR",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:SNDK": {
        "remote_symbol": "SNDK",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:MU": {
        "remote_symbol": "MU",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:LLY": {
        "remote_symbol": "LLY",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:BABA": {
        "remote_symbol": "BABA",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:RIVN": {
        "remote_symbol": "RIVN",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:TSM": {
        "remote_symbol": "TSM",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:CRWV": {
        "remote_symbol": "CRWV",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:EWY": {
        "remote_symbol": "EWY",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:EWJ": {
        "remote_symbol": "EWJ",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:PLATINUM": {
        "remote_symbol": "PL",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:PALLADIUM": {
        "remote_symbol": "PA",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    # CME FX futures are the leading books. 6J is USD/JPY's reciprocal, so
    # its quote-mid signal must be inverted to align with xyz:JPY (USDJPY).
    "xyz:EUR": {
        "remote_symbol": "6E",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:JPY": {
        "remote_symbol": "6J",
        "remote_market": "TopBookCme",
        "remote_quote_invert": True,
        "relative_symbol": "BTC",
    },
    "xyz:GBP": {
        "remote_symbol": "6B",
        "remote_market": "TopBookCme",
        "relative_symbol": "BTC",
    },
    "xyz:URNM": {
        "remote_symbol": "URNM",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:USAR": {
        "remote_symbol": "USAR",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:JP225": {
        "remote_symbol": "NIKKEI225",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:KR200": {
        "remote_symbol": "KOSPI200",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:HYUNDAI": {
        "remote_symbol": "HYUNDAI",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:SKHX": {
        "remote_symbol": "SKHX",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:SMSN": {
        "remote_symbol": "SMSN",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:ZHIPU": {
        "remote_symbol": "ZHIPU",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:MINIMAX": {
        "remote_symbol": "MINIMAX",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:GME": {
        "remote_symbol": "GME",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:LITE": {
        "remote_symbol": "LITE",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:XLE": {
        "remote_symbol": "XLE",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:BX": {
        "remote_symbol": "BX",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:MRVL": {
        "remote_symbol": "MRVL",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:RKLB": {
        "remote_symbol": "RKLB",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:DRAM": {
        "remote_symbol": "DRAM",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:EWZ": {
        "remote_symbol": "EWZ",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:ZM": {
        "remote_symbol": "ZM",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:EBAY": {
        "remote_symbol": "EBAY",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:CBRS": {
        "remote_symbol": "CBRS",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    "xyz:PURRDAT": {
        "remote_symbol": "PURR",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # ARM Holdings (single-stock) — added 2026-05-21.
    "xyz:ARM": {
        "remote_symbol": "ARM",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # iShares MSCI Taiwan ETF — added 2026-05-21.
    "xyz:EWT": {
        "remote_symbol": "EWT",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # BlackBerry (single-stock) — added 2026-05-27.
    "xyz:BB": {
        "remote_symbol": "BB",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # IBM (single-stock) — added 2026-06-01.
    "xyz:IBM": {
        "remote_symbol": "IBM",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # Dell Technologies (single-stock) — added 2026-06-01.
    "xyz:DELL": {
        "remote_symbol": "DELL",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # Broadcom (single-stock) — added 2026-06-03.
    "xyz:AVGO": {
        "remote_symbol": "AVGO",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # Quant / QNT (single-stock) — added 2026-06-04. Not yet on public
    # equity markets; remote TopBookEquity feed will be empty until listed.
    "xyz:QNT": {
        "remote_symbol": "QNT",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # ServiceNow / NOW (single-stock) — added 2026-06-04.
    "xyz:NOW": {
        "remote_symbol": "NOW",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # Nebius / NBIS (single-stock) — added 2026-06-08.
    "xyz:NBIS": {
        "remote_symbol": "NBIS",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # Western Digital / WDC (single-stock) — added 2026-06-08.
    "xyz:WDC": {
        "remote_symbol": "WDC",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # SPCX IPO'd 2026-06-12.
    "xyz:SPCX": {
        "remote_symbol": "SPCX",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # NOK (Nokia) HL listing 2026-06-10.
    "xyz:NOK": {
        "remote_symbol": "NOK",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # SMH (VanEck Semiconductor ETF) HL listing 2026-06-12.
    "xyz:SMH": {
        "remote_symbol": "SMH",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # BE (Bloom Energy) HL listing 2026-06-15.
    "xyz:BE": {
        "remote_symbol": "BE",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # MINIMAX (HKEX 0100.HK) — added 2026-06-16. NOT a US equity: the remote
    # leg is Refinitiv-fed (FX-converted HKD->USD) into the TopBookEquity book.
    # Trades HKEX AM/PM hours via its conffile start_t/end_t, not is_us_equity().
    "xyz:MINIMAX": {
        "remote_symbol": "MINIMAX",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # ZHIPU (HKEX 2513.HK) — added 2026-06-18. Same setup as MINIMAX: NOT a US
    # equity; the remote leg is Refinitiv-fed (FX-converted HKD->USD) into the
    # TopBookEquity book. Trades HKEX AM/PM hours via its conffile start_t/end_t.
    "xyz:ZHIPU": {
        "remote_symbol": "ZHIPU",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # QCOM (Qualcomm) HL listing 2026-06-21.
    "xyz:QCOM": {
        "remote_symbol": "QCOM",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # STRC (Strategy Inc preferred) HL listing 2026-06-22.
    "xyz:STRC": {
        "remote_symbol": "STRC",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # BOT (RoboStrategy Inc — robotics/physical-AI closed-end fund) HL listing
    # 2026-06-25. Thin/volatile with very wide spreads.
    "xyz:BOT": {
        "remote_symbol": "BOT",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # AMAT (Applied Materials — semiconductor equipment) HL listing 2026-06-26.
    "xyz:AMAT": {
        "remote_symbol": "AMAT",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # SHAZ (SharonAI Holdings — AI/GPU-cloud HPC) HL listing 2026-07-06.
    # Volatile mid-cap with moderately wide spreads.
    "xyz:SHAZ": {
        "remote_symbol": "SHAZ",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # SKHY (SK Hynix US ADR; distinct US-listed leg of the Korea name xyz:SKHX).
    # HL listing 2026-07-09 as a pre-IPO ticker; Nasdaq launch 2026-07-10. No
    # historical ADR data yet, so place_thresh was set manually rather than
    # derived from spreads.
    "xyz:SKHY": {
        "remote_symbol": "SKHY",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # KSTR (KraneShares SSE STAR Market 50 Index ETF) HL listing 2026-07-12.
    "xyz:KSTR": {
        "remote_symbol": "KSTR",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # GEV (GE Vernova — power/grid/energy) HL listing 2026-07-21.
    "xyz:GEV": {
        "remote_symbol": "GEV",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # KORU (Direxion Daily MSCI South Korea Bull 3X Shares — thin/volatile
    # 3x-leveraged South Korea equity ETF) HL listing 2026-08-01.
    "xyz:KORU": {
        "remote_symbol": "KORU",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # SOFTBANK (TSE 9984.T) — added 2026-06-24. NOT a US equity: the remote leg
    # is Refinitiv-fed (FX-converted JPY->USD) into the TopBookEquity book.
    # Trades TSE AM/PM hours (09:00-11:30 / 12:30-15:30 JST) via its conffile
    # start_t/end_t, not is_us_equity(). HL hip3 listing pending as of add date.
    "xyz:SOFTBANK": {
        "remote_symbol": "SOFTBANK",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
    # KIOXIA (TSE 285A.T) — added 2026-06-24. Same setup as SOFTBANK: NOT a US
    # equity; the remote leg is Refinitiv-fed (FX-converted JPY->USD) into the
    # TopBookEquity book. Trades TSE AM/PM hours via its conffile start_t/end_t.
    # HL hip3 listing pending as of add date.
    "xyz:KIOXIA": {
        "remote_symbol": "KIOXIA",
        "remote_market": "TopBookEquity",
        "relative_symbol": "BTC",
    },
}


def get_relwide_remote_meta(symbol: str):
    """Return default remote wiring info for a HIP3 symbol."""
    return relwide_remote_wiring.get(symbol)


_US_EQUITIES = {
    "TSLA", "NVDA", "HOOD", "INTC", "PLTR", "COIN", "META", "AAPL", "MSFT",
    "ORCL", "GOOGL", "AMZN", "AMD", "COST", "NFLX", "CRCL", "MSTR", "SNDK",
    "MU", "LLY", "BABA", "RIVN", "TSM", "CRWV", "EWY", "EWJ",
    "GME", "LITE", "XLE", "BX", "MRVL", "RKLB", "DRAM", "EWZ", "ZM",
    "EBAY",
    "CBRS", "PURR", "PURRDAT",
    "ARM", "EWT",  # added 2026-05-21
    "BB",  # added 2026-05-27
    "USAR", "URNM",  # added 2026-05-28
    "IBM", "DELL",  # added 2026-06-01
    "AVGO",  # added 2026-06-03
    "QNT", "NOW",  # added 2026-06-04
    "NBIS", "WDC",  # added 2026-06-08
    "SPCX",  # IPO 2026-06-12
    "NOK", "SMH",  # HL listings 2026-06-10 / 2026-06-12
    "BE",  # HL listing 2026-06-15
    "QCOM",  # HL listing 2026-06-21
    "STRC",  # HL listing 2026-06-22
    "BOT",  # HL listing 2026-06-25
    "AMAT",  # HL listing 2026-06-26
    "SHAZ",  # HL listing 2026-07-06
    "SKHY",  # HL listing 2026-07-09 (pre-IPO); SK Hynix US ADR, Nasdaq launch 2026-07-10
    "KSTR",  # HL listing 2026-07-12; KraneShares SSE STAR Market 50 ETF
    "GEV",  # HL listing 2026-07-21; GE Vernova
    "KORU",  # HL listing 2026-08-01; Direxion Daily MSCI South Korea Bull 3X
}


def is_us_equity(symbol: str) -> bool:
    """Return True if symbol is a US equity (trades US day hours only).

    Accepts both 'TSLA' and 'xyz:TSLA' forms.

    SCOPE — read before adding a non-US traded symbol here:
    This predicate is consumed ONLY by the sim/autosearch tooling
    (alpha_relwide_autosearch.py, relwide_equities_autosearch.py) to pick a
    DEFAULT backtest session window ("usday" vs 24h allday). It is NOT a
    live-trading gate. Live order placement hours come from a trader's
    start_t/end_t conffile fields, which flow into GlobalVar::start_t_/end_t_
    (C++); nothing in the live path calls is_us_equity().

    So a non-US traded symbol (e.g. the HKEX name xyz:MINIMAX, quoted off a
    Refinitiv-fed TopBookEquity book) must deliberately stay OUT of
    _US_EQUITIES — adding it would falsely impose US day hours on its
    backtests. Its real trading hours live entirely in its conffile's
    start_t/end_t (e.g. the HKEX AM 09:30-12:00 / PM 13:00-16:00 HKT windows).
    """
    bare = symbol.split(":")[-1].upper()
    return bare in _US_EQUITIES

sym_to_rfr = {
    "ES": 0.038,
    "NQ": 0.038,
    "CL": 0.0,
    "HG": 0.0,
    "NG": 0.0,
    "BZ": 0.0,
    "GC": 0.0,
    "SI": 0.0,
    "PL": 0.0,
    "PA": 0.0,
    "NIY": 0.0,
    "KOSPI200": 0.0,
    "KS": 0.0,
    "NIKKEI225": 0.0,
}

# Symbol to market mapping
syms_to_market = {
    "ES": "TopBookCme",
    "NQ": "TopBookCme",
    "CL": "TopBookCme",
    "HG": "TopBookCme",
    "NG": "TopBookCme",
    "GC": "TopBookCme",
    "SI": "TopBookCme",
    "PL": "TopBookCme",
    "PA": "TopBookCme",
    "BZ": "TopBookCme",
    "6E": "TopBookCme",
    "6J": "TopBookCme",
    "6B": "TopBookCme",
    "NIY": "TopBookCme",
    "KOSPI200": "TopBookEquity",
    "KS": "TopBookEquity",
    "NIKKEI225": "TopBookEquity",
    "BTC": "Hyperliquid",
    "TSLA": "TopBookEquity",
    "NVDA": "TopBookEquity",
    "HOOD": "TopBookEquity",
    "INTC": "TopBookEquity",
    "PLTR": "TopBookEquity",
    "COIN": "TopBookEquity",
    "META": "TopBookEquity",
    "AAPL": "TopBookEquity",
    "MSFT": "TopBookEquity",
    "ORCL": "TopBookEquity",
    "GOOGL": "TopBookEquity",
    "AMZN": "TopBookEquity",
    "AMD": "TopBookEquity",
    "COST": "TopBookEquity",
    "NFLX": "TopBookEquity",
    "CRCL": "TopBookEquity",
    "MSTR": "TopBookEquity",
    "SNDK": "TopBookEquity",
    "MU": "TopBookEquity",
    "LLY": "TopBookEquity",
    "BABA": "TopBookEquity",
    "RIVN": "TopBookEquity",
    "TSM": "TopBookEquity",
    "USAR": "TopBookEquity",
    "URNM": "TopBookEquity",
    "CRWV": "TopBookEquity",
    "EWY": "TopBookEquity",
    "EWJ": "TopBookEquity",
    "GME": "TopBookEquity",
    "LITE": "TopBookEquity",
    "XLE": "TopBookEquity",
    "BX": "TopBookEquity",
    "MRVL": "TopBookEquity",
    "RKLB": "TopBookEquity",
    "DRAM": "TopBookEquity",
    "EWZ": "TopBookEquity",
    "ZM": "TopBookEquity",
    "EBAY": "TopBookEquity",
    "ARM": "TopBookEquity",
    "EWT": "TopBookEquity",
    "BB": "TopBookEquity",
    "IBM": "TopBookEquity",
    "DELL": "TopBookEquity",
    "AVGO": "TopBookEquity",
    "QNT": "TopBookEquity",
    "NOW": "TopBookEquity",
    "NBIS": "TopBookEquity",
    "WDC": "TopBookEquity",
    "SPCX": "TopBookEquity",
    "NOK": "TopBookEquity",
    "SMH": "TopBookEquity",
    "BE": "TopBookEquity",
    "QCOM": "TopBookEquity",
    "STRC": "TopBookEquity",
    "BOT": "TopBookEquity",
    "AMAT": "TopBookEquity",
    "SHAZ": "TopBookEquity",
    "SKHY": "TopBookEquity",
    "KSTR": "TopBookEquity",
    "GEV": "TopBookEquity",
    "KORU": "TopBookEquity",
    "MINIMAX": "TopBookEquity",  # HKEX 0100.HK via Refinitiv (non-US equity)
    "ZHIPU": "TopBookEquity",  # HKEX 2513.HK via Refinitiv (non-US equity)
    "SOFTBANK": "TopBookEquity",  # TSE 9984.T via Refinitiv (non-US equity)
    "KIOXIA": "TopBookEquity",  # TSE 285A.T via Refinitiv (non-US equity)
    "CBRS": "TopBookEquity",
    "PURR": "TopBookEquity",
    "HIMS": "TopBookEquity",
    "BIRD": "TopBookEquity",
    "DKNG": "TopBookEquity",
    # Actually FX
    "GOLD": "TopBookEquity",
    "SILVER": "TopBookEquity",
    "JPY": "TopBookEquity",
    "EUR": "TopBookEquity",
    "GBP": "TopBookEquity",

    # Pyth-only
    "PLATINUM": "TopBookEquity",
    "PALLADIUM": "TopBookEquity",
}

# Symbol go-live dates (when data became available)
# Format: YYYYMMDD
syms_go_live_dates = {
    "ES": "20260315",
    "NQ": "20251115",
    "GC": "20251215",
    "SI": "20251215",
    "PL": "20251215",
    "PA": "20251215",
    "6E": "20251215",
    "6J": "20251215",
    "6B": "20251215",

    "CL": "20260105",
    "HG": "20260113",
    "NG": "20260120",
    "BZ": "20260303",
    "BTC": "20251115",
    "TSLA": "20251115",
    "NVDA": "20251115",
    "HOOD": "20251115",
    "INTC": "20251130",
    "PLTR": "20251115",
    "COIN": "20251115",
    "META": "20251115",
    "AAPL": "20251115",
    "MSFT": "20251115",
    "ORCL": "20251130",
    "GOOGL": "20251115",
    "AMZN": "20251115",
    "AMD": "20251130",
    "COST": "20251130",
    "NFLX": "20251130",
    "CRCL": "20251130",
    "MSTR": "20251130",
    "SNDK": "20251130",
    "MU": "20251130",
    "LLY": "20251130",
    "BABA": "20260112",
    "RIVN": "20260112",
    "TSM": "20260126",
    "USAR": "20260126",
    "URN": "20260126",
    "URNM": "20260126",
    "CRWV": "20260126",
    "NIY": "20260408",
    "KOSPI200": "20260408",
    "KS": "20260408",
    "NIKKEI225": "20260408",
    "EWY": "20260301",
    "EWJ": "20260301",
    "GME": "20260504",
    "LITE": "20260504",
    "XLE": "20260504",
    "BX": "20260504",
    "MRVL": "20260504",
    "RKLB": "20260504",
    "DRAM": "20260504",
    "EWZ": "20260504",
    "ZM": "20260504",
    "EBAY": "20260506",
    "ARM": "20260521",
    "EWT": "20260521",
    "BB": "20260526",
    "IBM": "20260601",
    "DELL": "20260601",
    "AVGO": "20260603",
    "QNT": "20260604",
    "NOW": "20260604",
    "NBIS": "20260608",
    "WDC": "20260608",
    "SPCX": "20260612",
    "NOK": "20260610",
    "SMH": "20260612",
    "BE": "20260615",
    "QCOM": "20260621",
    "STRC": "20260622",
    "BOT": "20260625",
    "AMAT": "20260626",
    "SHAZ": "20260706",
    "SKHY": "20260710",
    "KSTR": "20260712",
    "GEV": "20260721",
    "KORU": "20260801",
    # No historical data for MINIMAX (Refinitiv-fed, day-one); go-live = first
    # recorded date. Bump if recording actually starts later.
    "MINIMAX": "20260616",
    # No historical data for ZHIPU (Refinitiv-fed, day-one); go-live = first
    # recorded date. Bump if recording actually starts later.
    "ZHIPU": "20260618",
    # No historical data for SOFTBANK (Refinitiv-fed, day-one); go-live = first
    # recorded date (Refinitiv 9984.T capture start). Bump if recording actually
    # starts later. HL hip3 listing still pending as of this date.
    "SOFTBANK": "20260624",
    # No historical data for KIOXIA (Refinitiv-fed, day-one); go-live = first
    # recorded date (Refinitiv 285A.T capture start). Bump if recording actually
    # starts later. HL hip3 listing still pending as of this date.
    "KIOXIA": "20260624",
    "CBRS": "20260514",
    "PURR": "20260514",
    "BIRD": "20260417",
    "DKNG": "20260502",
    "HIMS": "20260502",

    "GOLD": "20251228",
    "SILVER": "20251229",
    "JPY": "20251229",
    "EUR": "20251229",
    "GBP": "20260519",

    "PLATINUM": "20260127",
    "PALLADIUM": "20260127",
}


# Shadowed expiry map for expiries. This is copied/synchronized w/
sym_to_expiry = {
    "ES": [
        # ESZ25
        ("20250916 00:00:00 America/New_York", "20251219 17:00:00 America/New_York"),
        # ESH26
         ("20251214 18:00:00 America/New_York", "20260320 09:30:00 America/New_York"),
        # ESM26
         ("20260313 18:00:00 America/New_York", "20260618 09:30:00 America/New_York"),
        # ESU26
         ("20260614 18:00:00 America/New_York", "20260918 09:30:00 America/New_York"),
        # ESZ26
         ("20260913 18:00:00 America/New_York", "20261218 09:30:00 America/New_York"),
        # ESH27
         ("20261213 18:00:00 America/New_York", "20270319 09:30:00 America/New_York"),
    ],

    "NQ": [
        # NQZ25
        ("20250916 00:00:00 America/New_York", "20251219 17:00:00 America/New_York"),
        # NQH26
         ("20251214 18:00:00 America/New_York", "20260320 09:30:00 America/New_York"),
        # NQM26
         ("20260313 18:00:00 America/New_York", "20260618 09:30:00 America/New_York"),
        # NQU26
         ("20260614 18:00:00 America/New_York", "20260918 09:30:00 America/New_York"),
        # NQZ26
         ("20260913 18:00:00 America/New_York", "20261218 09:30:00 America/New_York"),
        # NQH27
         ("20261213 18:00:00 America/New_York", "20270319 09:30:00 America/New_York"),
    ],

    # TODO: do this more properly.
    # At some point, they're going to switch a model where they provide a mixed price in the last week.
    # And we'll have to shadow it too.
    "CL": [
        # CLG6
        ("20251220 18:00:00 America/New_York", "20260121 17:00:00 America/New_York"),
        # CLH6
        ("20260111 17:00:00 America/New_York", "20260220 17:00:00 America/New_York"),
        # CLJ6
        ("20260213 17:00:00 America/New_York", "20260320 17:00:00 America/New_York"),
    ],
    "HG": [
        # HGH6
        # Third last business day of March, so 20260327, at noon CT, 13:00 ET.
        # Anyway, no discounting, so it doesn't matter.
        ("20201231 17:00:00 America/New_York", "20260327 13:00:00 America/New_York"),
        # HGK6
        # Third last biz day of May, so 20260527.
        ("20260320 17:00:00 America/New_York", "20260527 13:00:00 America/New_York"),
    ],
    "NG": [
        # NGH26
        # Termination date is from cme page
        # https://www.cmegroup.com/markets/energy/natural-gas/natural-gas.calendar.html
        ("20131127 17:00:00 America/New_York", "20260225 17:00:00 America/New_York"),
        # NGJ26
        ("20260218 17:00:00 America/New_York", "20260327 17:00:00 America/New_York"),
    ],

    # NYMEX Platinum (PL) — quarterly J/N/V/F. LTD = 3rd business day prior
    # to the last business day of the contract month, 13:05 ET.
    # https://www.cmegroup.com/markets/metals/precious/platinum.contractSpecs.html
    "PL": [
        # PLF6 (Jan 2026)
        ("20250923 18:00:00 America/New_York", "20260127 13:05:00 America/New_York"),
        # PLJ6 (Apr 2026)
        ("20251223 18:00:00 America/New_York", "20260427 13:05:00 America/New_York"),
        # PLN6 (Jul 2026)
        ("20260326 18:00:00 America/New_York", "20260728 13:05:00 America/New_York"),
        # PLV6 (Oct 2026)
        ("20260624 18:00:00 America/New_York", "20261027 13:05:00 America/New_York"),
        # PLF7 (Jan 2027)
        ("20260924 18:00:00 America/New_York", "20270126 13:05:00 America/New_York"),
        # PLJ7 (Apr 2027)
        ("20261222 18:00:00 America/New_York", "20270427 13:05:00 America/New_York"),
    ],

    # NYMEX Palladium (PA) — quarterly H/M/U/Z. Same LTD rule as PL.
    # https://www.cmegroup.com/markets/metals/precious/palladium.contractSpecs.html
    "PA": [
        # PAH6 (Mar 2026)
        ("20251124 18:00:00 America/New_York", "20260326 13:05:00 America/New_York"),
        # PAM6 (Jun 2026)
        ("20260224 18:00:00 America/New_York", "20260625 13:05:00 America/New_York"),
        # PAU6 (Sep 2026)
        ("20260525 18:00:00 America/New_York", "20260925 13:05:00 America/New_York"),
        # PAZ6 (Dec 2026) — LBD = 12/31, walk back 3 BD past Christmas (12/25 closed)
        ("20260824 18:00:00 America/New_York", "20261228 13:05:00 America/New_York"),
        # PAH7 (Mar 2027)
        ("20261123 18:00:00 America/New_York", "20270326 13:05:00 America/New_York"),
    ],

    # CME Nikkei 225 Yen futures — quarterly H/M/U/Z
    # https://www.cmegroup.com/markets/equities/nikkei/nikkei-225-yen.contractSpecs.html
    "NIY": [
        # NIYM6
        ("20260313 18:00:00 America/New_York", "20260612 09:30:00 America/New_York"),
        # NIYU6
        ("20260614 18:00:00 America/New_York", "20260911 09:30:00 America/New_York"),
        # NIYZ6
        ("20260913 18:00:00 America/New_York", "20261211 09:30:00 America/New_York"),
        # NIYH7
        ("20261213 18:00:00 America/New_York", "20270312 09:30:00 America/New_York"),
    ],

}

sym_to_expiry_epochsecs = {}
# Go through and convert the string datetimes for sym_to_expiry to epoch seconds.
for one_sym_class, contract_specs_list in sym_to_expiry.items():
    # Convert them.
    converted_ranges = []
    for one_expiry_tuple in contract_specs_list:
        # Converts the start, end times.
        start_secs = int(chron.timestr_to_secs(one_expiry_tuple[0]))
        end_secs = int(chron.timestr_to_secs(one_expiry_tuple[1]))

        converted_ranges.append((start_secs, end_secs))
    sym_to_expiry_epochsecs[one_sym_class] = converted_ranges


# For subscribing to polygon histdata
# Routing-only set used by hist_quotedata.py: when backfilling under these
# polygonfx-style symbol names, source from Pyth (since they're not in
# polygonfx_easyname_to_feed and aren't equity tickers). Not relevant to the
# CME-side flow, which uses PL/PA via TopBookCme.
pyth_only_symbols = {"PLATINUM", "PALLADIUM"}

polygonfx_easyname_to_feed = {
    "GOLD": "C:XAUUSD",
    "SILVER": "C:XAGUSD",
    "EUR": "C:EURUSD",
    "JPY": "C:USDJPY",
    "GBP": "C:GBPUSD"
}


# cur_t_secs is in seconds since epoch
def get_expiry_t(sym, cur_t_secs):
    # Look through the sym_to_expiry times, compare the cur_t with the value
    # of the first value of the tuple. The relevant contract is the first one where
    # the first index is larger that the cur_time.

    # Check if symbol exists in map
    if sym not in sym_to_expiry:
        sys.exit("ERROR: {} not found in sym_to_expiry map!".format(sym))

    expiry_ranges = sym_to_expiry_epochsecs[sym]

    right_expiry_t = 0
    for start_time_secs, expiry_time_secs in expiry_ranges:
        # Check if current time falls within this contract period

        if cur_t_secs >= start_time_secs and cur_t_secs < expiry_time_secs:
            #print("Returning {} which corresponds to {}".format(expiry_t_secs, expiry_time_str_debug))
            right_expiry_t = expiry_time_secs
    if right_expiry_t != 0:
        return right_expiry_t

    # If we get here, cur_t_secs is either before all ranges or after all ranges
    # Check if it's before the first range
    first_start_str, _ = expiry_ranges[0]
    first_start_dt_str, first_start_tz_str = first_start_str.rsplit(' ', 1)
    first_start_tz = pytz.timezone(first_start_tz_str)
    first_start_dt = dt.datetime.strptime(first_start_dt_str, "%Y%m%d %H:%M:%S")
    first_start_dt = first_start_tz.localize(first_start_dt)
    first_start_t_secs = int(first_start_dt.timestamp())

    if cur_t_secs < first_start_t_secs:
        sys.exit("ERROR: Current time ({}) is before the first contract start time ({}) for {}!".format(
            cur_t_secs, first_start_t_secs, sym
        ))
    else:
        sys.exit("ERROR: Current time ({}) is after all contract expiry times for {}!".format(
            cur_t_secs, sym
        ))


def ours_to_databento_dated(easy_symbol: str, date: str):

    if easy_symbol not in dated_db_map:
        print("Tried to translate {} but this family isn't in our dated_db_map!".format(easy_symbol))
        sys.exit()

    # Otherwise, walk through the map.
    symbol_dates = dated_db_map[easy_symbol]
    date_int = int(date)
    potential_sym = None
    for roll_date, symbol_raw_name in sorted(symbol_dates.items()):
        if date_int >= roll_date:
            potential_sym = symbol_raw_name
            # We got em!

    if potential_sym is not None:
        reverse_db_map[potential_sym] = easy_symbol

        return potential_sym

    # Otherwise, we didn't find it.
    print("Could not properly translate {} to anything on or after {}".format(easy_symbol, date))


def get_all_contracts_for_date(symbol: str, date_str: str):
    """
    Returns list of (databento_symbol, weight) tuples for a given date.
    During non-roll periods: [(contract, 1.0)]
    During roll periods: [(front_contract, front_w), (next_contract, next_w)]
    """
    date_int = int(date_str)
    if symbol in roll_blend_schedule and date_int in roll_blend_schedule[symbol]:
        front_contract, next_contract, front_weight = roll_blend_schedule[symbol][int(date_str)]
        next_weight = round(1.0 - front_weight, 2)
        reverse_db_map[front_contract] = symbol
        reverse_db_map[next_contract] = symbol
        return [(front_contract, front_weight), (next_contract, next_weight)]
    else:
        return [(ours_to_databento_dated(symbol, date_str), 1.0)]


def test_dated_mappings():
    # print(ours_to_databento_dated("NQ", "20250701"))
    # print(ours_to_databento_dated("NQ", "20250901"))
    # print(ours_to_databento_dated("NQ", "20251001"))
    # print(ours_to_databento_dated("NQ", "20251221"))

    # print()
    # print(databento_to_ours("NQU5"))
    print(get_all_contracts_for_date("CL", "20260205"))
    print(get_all_contracts_for_date("CL", "20260206"))
    print(get_all_contracts_for_date("CL", "20260209"))
    print(get_all_contracts_for_date("CL", "20260210"))
    print(get_all_contracts_for_date("CL", "20260211"))
    print(get_all_contracts_for_date("CL", "20260212"))
    print(get_all_contracts_for_date("CL", "20260213"))
    print(get_all_contracts_for_date("CL", "20260214"))

def test_blended_rolls(sym, year):
    date = dt.date(year, 1, 1)
    while date.year == year:
        print(f"{sym} on {date}: {get_all_contracts_for_date(sym, date.strftime('%Y%m%d'))}")
        date += dt.timedelta(days=1)


#####################################################
# Helper to generate roll_blend_schedule entries.
# Run: python symbolizer.py HG 2026 3
# Output: a dict you can paste into roll_blend_schedule after verifying.
#####################################################

MONTH_TO_CODE = {1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
                 7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'}

# For each commodity, which calendar months have a roll, and what
# delivery months are the front/next.
# Format: {calendar_month: (front_delivery_month, next_delivery_month)}
# CL and NG have monthly contracts. In month M, front = M+1, next = M+2.
# HG has contracts in Mar(H), May(K), Jul(N), Sep(U), Dec(Z) only.
_CL_NG_ROLL_MONTHS = {
    1: (2, 3), 2: (3, 4), 3: (4, 5), 4: (5, 6), 5: (6, 7), 6: (7, 8),
    7: (8, 9), 8: (9, 10), 9: (10, 11), 10: (11, 12), 11: (12, 1), 12: (1, 2)
}
COMMODITY_ROLL_MONTHS = {
    "CL": _CL_NG_ROLL_MONTHS,
    "NG": _CL_NG_ROLL_MONTHS,
    "BZ": _CL_NG_ROLL_MONTHS,
    "HG": {
        # Delivery months: H(3), K(5), N(7), U(9), Z(12)
        # Roll happens the month before the front contract changes.
        2: (3, 5),     # Feb: Mar(H) -> May(K)
        4: (5, 7),     # Apr: May(K) -> Jul(N)
        6: (7, 9),     # Jun: Jul(N) -> Sep(U)
        8: (9, 12),    # Aug: Sep(U) -> Dec(Z)
        11: (12, 3),   # Nov: Dec(Z) -> Mar(H) next year
    },
}

# Databento symbol year format: CL/HG use 1 digit, NG uses 2 digits.
_YEAR_DIGITS = {"CL": 1, "HG": 1, "NG": 2, "BZ": 1}


def _make_contract_sym(symbol, delivery_month, year):
    """Build a Databento raw symbol like 'CLH6', 'HGK6', 'NGJ26'."""
    code = MONTH_TO_CODE[delivery_month]
    yr_digits = _YEAR_DIGITS[symbol]
    yr_str = str(year % (10 ** yr_digits))
    return f"{symbol}{code}{yr_str}"


def generate_roll_blend_entries(symbol, year, holidays=None, print_updates=True):
    """
    Generate all roll_blend_schedule entries for a commodity symbol for the given year.
    Also generate all dated_db_map entries that need to be added.
    Print each dict for manual review before inserting into roll_blend_schedule.
    TODO: Once this works stably, skip the hardcoded dicts and just run this on-the-fly.

    *** IMPORTANT: CHECK FOR CME MARKET HOLIDAYS THIS YEAR! ***
    Holidays shift business day numbering and will produce wrong roll dates.
    Pass holidays=[YYYYMMDD, ...] to exclude them from business day counting.
    Common CME closures: New Year's, MLK Day, Presidents' Day, Good Friday,
    Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas.

    Args:
        symbol: "HG", "CL", or "NG"
        year: e.g. 2026
        holidays: optional list of YYYYMMDD ints for CME holidays this year

    Returns:
        tuple containing 2 dicts:
        a dict with dates as keys, each mapping to a 3-tuple (front, next, front_weight), e.g.,
        {
          20260209: ("CLH6", "CLJ6", 0.8),
          20260210: ("CLH6", "CLJ6", 0.6),
        }
        and a dict with dates as keys, each mapping to the next contract, e.g.,
        {
          20260213: "CLJ6",
        }
    """
    if symbol not in COMMODITY_ROLL_MONTHS:
        print(f"Unknown symbol: {symbol}")
        return ({}, {})

    # Parse holidays once for the whole year
    if holidays is None:
        # Only if None. If [] then keep that.
        holidays = default_holidays
    holiday_dates = set()
    if holidays:
        for h_int in holidays:
            h = str(h_int)
            holiday_dates.add(dt.date(int(h[:4]), int(h[4:6]), int(h[6:8])))

    roll_info = COMMODITY_ROLL_MONTHS[symbol]
    bd_to_fw = {6: 0.8,
                7: 0.6,
                8: 0.4,
                9: 0.2}
    last_blended_bd = max(bd_to_fw)

    roll_blend_schedule_sym = {}
    dated_db_map_sym = {}

    for month in sorted(roll_info.keys()):
        front_delivery, next_delivery = roll_info[month]

        # Determine contract years. If next delivery month < front, it's next year.
        front_year = year if front_delivery > month else year + 1
        next_year = year if next_delivery > month else year + 1

        front_sym = _make_contract_sym(symbol, front_delivery, front_year)
        next_sym = _make_contract_sym(symbol, next_delivery, next_year)

        # Populate the roll_blend_schedule and dated_db_map entries for the month.
        date = dt.date(year, month, 1)
        bd = 0
        while bd <= last_blended_bd:
            if date.weekday() < 5 and date not in holiday_dates: # is a business day
                bd += 1
            date_int = int(date.strftime("%Y%m%d"))
            if bd in bd_to_fw:
                roll_blend_schedule_sym[date_int] = (front_sym, next_sym, bd_to_fw[bd])
            elif bd > last_blended_bd:
                dated_db_map_sym[date_int] = next_sym
                break
            date += dt.timedelta(days=1)

    if print_updates:
        import pprint
        print(f'Update roll_blend_schedule["{symbol}"] with:')
        pprint.pprint(roll_blend_schedule_sym, indent=8)
        print(f'Update dated_db_map["{symbol}"] with:')
        pprint.pprint(dated_db_map_sym, indent=8)

    return (roll_blend_schedule_sym, dated_db_map_sym)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate roll blend schedule entries")
    parser.add_argument("symbol", help="HG, CL, or NG")
    parser.add_argument("year", type=int, help="e.g. 2026")
    parser.add_argument("--holidays", nargs="*", type=int,
                        help="CME holiday dates as YYYYMMDD (e.g. 20260119 for MLK Day)")
    parser.add_argument("--print-updates", action="store_true", help="Print updates for inspection.")
    parser.add_argument("--test", action="store_true", help="Test mapping for whole year.")
    args = parser.parse_args()

    # test_dated_mappings()

    roll_blend_schedule_new, dated_db_map_new = generate_roll_blend_entries(args.symbol, args.year, args.holidays, args.print_updates)
    if args.test:
        if roll_blend_schedule_new:
            roll_blend_schedule[args.symbol].update(roll_blend_schedule_new)
        if dated_db_map_new:
            dated_db_map[args.symbol].update(dated_db_map_new)
        test_blended_rolls(args.symbol, args.year)
