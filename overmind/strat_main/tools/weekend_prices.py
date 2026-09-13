#! /usr/bin/env python

"""
Compute Friday 17:00 ET and Sunday 18:00 ET (or Monday 9:30 ET) prices for CME, US equities, and crypto symbols.

Usage example:
  python weekend_prices.py \
      --start-date 20250101 --end-date 20250201 \
      --symbol CME:ES --symbol USEQ:AAPL --symbol CRYPTO:BTC-USD

  # For equities, use Monday market open instead of Sunday:
  python weekend_prices.py \
      --start-date 20250101 --end-date 20250201 \
      --symbol USEQ:AAPL --end-time-mode monday

The script loads DataBento credentials from ~/.creds/.DataBento.creds.json.
Crypto prices are fetched from Coinbase spot candles (1 minute granularity).

Example with default Sunday mode:

(v1) pktrade@pktrade: ~/tradefi/retraded/overmind/strat_main/tools (regressors) $ python weekend_prices.py  --symbol CME:NQ --symbol CRYPTO:BTC-USD --start 20250101 --end 20250131

FRI 20250103 | CME:NQ : 21489.25 CRYPTO:BTC-USD : 98234.45
SUN 20250103 | CME:NQ : 21529.50 CRYPTO:BTC-USD : 98702.99
FRI 20250110 | CME:NQ : 20994.00 CRYPTO:BTC-USD : 94609.45
SUN 20250110 | CME:NQ : 20992.62 CRYPTO:BTC-USD : 94055.79
FRI 20250117 | CME:NQ : 21587.50 CRYPTO:BTC-USD : 104651.04
SUN 20250117 | CME:NQ : 21606.38 CRYPTO:BTC-USD : 102440.41
FRI 20250124 | CME:NQ : 21903.50 CRYPTO:BTC-USD : 105143.92
SUN 20250124 | CME:NQ : 21693.50 CRYPTO:BTC-USD : 104172.64
FRI 20250131 | CME:NQ : 21557.38 CRYPTO:BTC-USD : 102111.73
SUN 20250131 | CME:NQ : 20987.50 CRYPTO:BTC-USD : 97446.45

TODO: add support for polygon here.


"""

import argparse
import datetime as dt
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

import databento as db

from zoneinfo import ZoneInfo
import sys
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)

from util import symbolizer

DB_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.DataBento.creds.json")
NY_TZ = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


@dataclass(frozen=True)
class SymbolSpec:
    market: str
    symbol: str


class WeekendPriceCalculator:
    def __init__(self, api_key: Optional[str]):
        if api_key is None:
            raise RuntimeError("DataBento API key is required for CME and USEQ symbols.")
        self.db_client = db.Historical(api_key)

    def fetch_databento_prices(
        self,
        spec: SymbolSpec,
        start_dt: dt.datetime,
        end_dt: dt.datetime,
    ) -> pd.DataFrame:
        dataset, schema, stype_in, db_symbol = self._databento_params(spec)
        start_iso = start_dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
        end_iso = end_dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
        raw = self.db_client.timeseries.get_range(
            dataset=dataset,
            symbols=[db_symbol],
            stype_in=stype_in,
            schema=schema,
            start=start_iso,
            end=end_iso,
        )
        frame = raw.to_df()
        if frame.empty:
            return frame
        frame["ts_event"] = pd.to_datetime(frame["ts_event"], utc=True)
        if {"bid_px_00", "ask_px_00"}.issubset(frame.columns):
            frame["mid_px"] = (frame["bid_px_00"].astype(float) + frame["ask_px_00"].astype(float)) / 2
        elif "close" in frame.columns:
            frame["mid_px"] = frame["close"].astype(float)
        else:
            raise RuntimeError(f"Unsupported schema output for {spec}")
        frame = frame.dropna(subset=["mid_px"]).sort_values("ts_event").reset_index(drop=True)
        return frame

    def _databento_params(self, spec: SymbolSpec) -> Tuple[str, str, str, str]:
        market = spec.market.upper()
        if market == "CME":
            dataset = "GLBX.MDP3"
            schema = "bbo-1m"
            stype_in = "continuous"
            db_symbol = symbolizer.ours_db_map.get(spec.symbol, spec.symbol)
        elif market == "USEQ":
            dataset = "EQUS.MINI"
            schema = "bbo-1m"
            stype_in = "raw_symbol"
            db_symbol = spec.symbol
        else:
            raise ValueError(f"Unsupported DataBento market {spec.market}")
        return dataset, schema, stype_in, db_symbol

    @staticmethod
    def select_price(
        frame: pd.DataFrame,
        target_dt: dt.datetime,
        mode: str,
    ) -> Tuple[Optional[float], Optional[dt.datetime]]:
        if frame.empty:
            return None, None
        target_utc = target_dt.astimezone(UTC)
        if mode == "before":
            subset = frame[frame["ts_event"] <= target_utc]
            if subset.empty:
                subset = frame[frame["ts_event"] > target_utc]
                if subset.empty:
                    return None, None
                row = subset.iloc[0]
            else:
                row = subset.iloc[-1]
        elif mode == "after":
            subset = frame[frame["ts_event"] >= target_utc]
            if subset.empty:
                subset = frame[frame["ts_event"] < target_utc]
                if subset.empty:
                    return None, None
                row = subset.iloc[-1]
            else:
                row = subset.iloc[0]
        else:
            raise ValueError(f"Invalid mode {mode}")
        return float(row["mid_px"]), row["ts_event"].to_pydatetime()


class CoinbaseCandleFetcher:
    BASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
    MAX_POINTS = 300  # exchange limitation per request

    def __init__(self, symbol: str):
        self.product_id = self._normalize_symbol(symbol)
        self.cache: Optional[pd.DataFrame] = None

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        sym = symbol.upper().replace("/", "-")
        if "-" not in sym:
            if sym.endswith("USD"):
                base = sym[:-3]
                sym = f"{base}-USD"
            else:
                raise ValueError(f"Crypto symbol {symbol} must include quote currency (e.g. BTC-USD)")
        return sym

    def ensure_data(self, start_dt: dt.datetime, end_dt: dt.datetime) -> None:
        start = start_dt.astimezone(UTC)
        end = end_dt.astimezone(UTC)
        candles: List[Dict[str, object]] = []
        chunk_start = start
        url = self.BASE_URL.format(product=self.product_id)

        while chunk_start < end:
            chunk_end = min(chunk_start + dt.timedelta(minutes=self.MAX_POINTS), end)
            params = {
                "granularity": 60,
                "start": chunk_start.isoformat().replace("+00:00", "Z"),
                "end": chunk_end.isoformat().replace("+00:00", "Z"),
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                chunk_start = chunk_end
                continue
            for entry in batch:
                ts = dt.datetime.fromtimestamp(entry[0], tz=UTC)
                candles.append({
                    "open_time": ts,
                    "close_time": ts + dt.timedelta(minutes=1),
                    "close": float(entry[4]),
                })
            chunk_start = chunk_end

        frame = pd.DataFrame(candles)
        if frame.empty:
            self.cache = frame
        else:
            self.cache = frame.sort_values("open_time").reset_index(drop=True)

    def select_price(
        self,
        target_dt: dt.datetime,
        mode: str,
    ) -> Tuple[Optional[float], Optional[dt.datetime]]:
        if self.cache is None or self.cache.empty:
            return None, None
        target_utc = target_dt.astimezone(UTC)
        frame = self.cache
        if mode == "before":
            subset = frame[frame["close_time"] <= target_utc]
            if subset.empty:
                subset = frame[frame["open_time"] > target_utc]
                if subset.empty:
                    return None, None
                row = subset.iloc[0]
                price_time = row["open_time"]
            else:
                row = subset.iloc[-1]
                price_time = row["close_time"]
        elif mode == "after":
            subset = frame[frame["open_time"] >= target_utc]
            if subset.empty:
                subset = frame[frame["close_time"] < target_utc]
                if subset.empty:
                    return None, None
                row = subset.iloc[-1]
                price_time = row["close_time"]
            else:
                row = subset.iloc[0]
                price_time = row["open_time"]
        else:
            raise ValueError(f"Invalid mode {mode}")
        return float(row["close"]), price_time


def load_databento_key() -> Optional[str]:
    if not os.path.exists(DB_SECRET_FILE_PATH):
        return None
    with open(DB_SECRET_FILE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("api_key")


def parse_symbol_arg(arg: str) -> SymbolSpec:
    if ":" not in arg:
        raise argparse.ArgumentTypeError("Symbols must be formatted as MARKET:SYMBOL")
    market, symbol = arg.split(":", 1)
    market = market.strip().upper()
    symbol = symbol.strip()
    if market not in {"CME", "USEQ", "CRYPTO"}:
        raise argparse.ArgumentTypeError(f"Unsupported market {market}")
    return SymbolSpec(market=market, symbol=symbol)


def iter_fridays(start_date: str, end_date: str) -> Iterable[dt.date]:
    start = dt.datetime.strptime(start_date, "%Y%m%d").date()
    end = dt.datetime.strptime(end_date, "%Y%m%d").date()
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    days_ahead = (4 - start.weekday()) % 7
    first_friday = start + dt.timedelta(days=days_ahead)
    current = first_friday
    while current <= end:
        yield current
        current += dt.timedelta(days=7)


def target_datetimes(friday: dt.date, end_mode: str = "sunday") -> Tuple[dt.datetime, dt.datetime]:
    friday_dt = dt.datetime.combine(friday, dt.time(hour=17, minute=0), tzinfo=NY_TZ)
    if end_mode == "monday":
        # Monday 9:30 ET (market open for equities)
        end_dt = friday_dt + dt.timedelta(days=3, hours=-7, minutes=-30)  # move to Monday 09:30 ET
    else:
        # Sunday 18:00 ET (default)
        end_dt = friday_dt + dt.timedelta(days=2, hours=1)  # move to Sunday 18:00 ET
    return friday_dt, end_dt


def build_time_bounds(fridays: List[dt.date], end_mode: str = "sunday") -> Tuple[dt.datetime, dt.datetime]:
    friday_targets, end_targets = zip(*[target_datetimes(day, end_mode) for day in fridays])    

    global_start = min(friday_targets) - dt.timedelta(hours=12)
    global_end = max(end_targets) + dt.timedelta(hours=24)
    return global_start, global_end


def compute_weekend_prices(
    specs: List[SymbolSpec],
    start_date: str,
    end_date: str,
    end_mode: str = "sunday",
) -> Dict[str, Dict[str, Dict[str, object]]]:
    fridays = list(iter_fridays(start_date, end_date))
    if not fridays:
        raise ValueError("No Fridays found in the provided date range")
    db_key = load_databento_key()
    calculator = WeekendPriceCalculator(db_key) if any(spec.market != "CRYPTO" for spec in specs) else None
    global_start, global_end = build_time_bounds(fridays, end_mode)

    databento_frames: Dict[SymbolSpec, pd.DataFrame] = {}
    crypto_fetchers: Dict[str, CoinbaseCandleFetcher] = {}

    for spec in specs:
        if spec.market == "CRYPTO":
            fetcher = CoinbaseCandleFetcher(spec.symbol)
            fetcher.ensure_data(global_start, global_end)
            crypto_fetchers[spec.symbol] = fetcher
        else:
            if calculator is None:
                raise RuntimeError("DataBento access required for tradfi symbols")
            databento_frames[spec] = calculator.fetch_databento_prices(spec, global_start, global_end)

    friday_prices: Dict[str, Dict[str, Dict[str, object]]] = defaultdict(dict)
    sunday_prices: Dict[str, Dict[str, Dict[str, object]]] = defaultdict(dict)

    for friday in fridays:
        friday_dt, end_dt = target_datetimes(friday, end_mode)
        week_label = friday.strftime("%Y%m%d")
        for spec in specs:
            key = f"{spec.market}:{spec.symbol}"
            if spec.market == "CRYPTO":
                fetcher = crypto_fetchers[spec.symbol]
                f_price, f_obs = fetcher.select_price(friday_dt, mode="before")
                s_price, s_obs = fetcher.select_price(end_dt, mode="after")
                source = "coinbase"
            else:
                frame = databento_frames[spec]
                f_price, f_obs = WeekendPriceCalculator.select_price(frame, friday_dt, mode="before")
                s_price, s_obs = WeekendPriceCalculator.select_price(frame, end_dt, mode="after")
                source = "databento"
            friday_prices[week_label][key] = {
                "target": friday_dt.isoformat(),
                "observed": f_obs.astimezone(UTC).isoformat() if f_obs else None,
                "price": f_price,
                "source": source,
            }
            sunday_prices[week_label][key] = {
                "target": end_dt.isoformat(),
                "observed": s_obs.astimezone(UTC).isoformat() if s_obs else None,
                "price": s_price,
                "source": source,
            }

    # Iterate through both and give us prices per day for symbols.
    end_label = "MON" if end_mode == "monday" else "SUN"
    friday_i = 0
    end_i = 0

    friday_keys = list(friday_prices.keys())
    end_keys = list(sunday_prices.keys())
    while end_i < len(end_keys) and friday_i < len(friday_keys):
        cur_friday = int(friday_keys[friday_i])
        cur_end = int(end_keys[end_i])

        if (cur_friday < cur_end):
            friday_i += 1
            continue
        elif cur_end < cur_friday:
            end_i += 1
            continue

        if (cur_friday == cur_end):
            # Print the friday
            friday_dct = friday_prices[friday_keys[friday_i]]

            friday_line = "FRI {} | ".format(cur_friday)
            for sym in friday_dct:
                friday_line += "{} : {:.2f} ".format(sym, friday_dct[sym]["price"])
            print(friday_line)
            friday_i += 1

            end_dct = sunday_prices[end_keys[end_i]]
            end_line = "{} {} | ".format(end_label, cur_end)
            for sym in end_dct:
                end_line += "{} : {:.2f} ".format(sym, end_dct[sym]["price"])
            print(end_line)
            end_i += 1
        

        #friday_key = list(friday_prices.keys())[friday_i]
        #sunday_key = list(sunday_prices.keys())[sunday_i]
        #if friday_key != sunday_key:
        #    raise ValueError("Weekend prices not aligned")

    return {"friday_prices": friday_prices, "sunday_prices": sunday_prices}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch weekend prices for selected symbols")
    parser.add_argument("--start-date", required=True, help="Inclusive start date YYYYMMDD")
    parser.add_argument("--end-date", required=True, help="Inclusive end date YYYYMMDD")
    parser.add_argument("--symbol", action="append", required=True, type=parse_symbol_arg,
                        help="Symbol formatted as MARKET:SYMBOL (CME, USEQ, CRYPTO)")
    parser.add_argument("--end-time-mode", choices=["sunday", "monday"], default="sunday",
                        help="End time mode: 'sunday' for Sunday 18:00 ET (default), 'monday' for Monday 9:30 ET (equity market open)")
    args = parser.parse_args()

    result = compute_weekend_prices(args.symbol, args.start_date, args.end_date, args.end_time_mode)
    #print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
