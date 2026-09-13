#!/usr/bin/env python3
"""Compute average spread and traded volume from Databento historical data."""

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple, TYPE_CHECKING

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd
    import databento as db

pd = None
db = None

# Allow reusing internal symbol translation helpers when available.
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
UTIL_DIR = os.path.abspath(os.path.join(TOOLS_DIR, "..", "util"))
if UTIL_DIR not in sys.path:
    sys.path.append(UTIL_DIR)
try:
    import symbolizer  # type: ignore
except Exception:  # pragma: no cover - fallback when util module is absent
    symbolizer = None

DB_SECRET_FILE_PATH = os.path.expanduser("~/.creds/.DataBento.creds.json")

DEFAULT_DATASETS = {
    "cme": "GLBX.MDP3",
    #"equity": "EQUS.MINI",
    # TODO: look this up later, but I might need to 
    "equity": "XNAS.BASIC",
}

SPREAD_COLUMN_CANDIDATES: Tuple[Tuple[str, str], ...] = (
    ("ask_px", "bid_px"),
    ("ask_px_00", "bid_px_00"),
    ("best_ask_px", "best_bid_px"),
    ("ask_price", "bid_price"),
)

VOLUME_COLUMNS = ("size", "sz", "quantity", "qty", "size_00")


def ensure_pandas():
    global pd
    if pd is None:
        try:
            import pandas as pandas_mod  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("pandas is required for this script") from exc
        pd = pandas_mod
    return pd


def ensure_databento():
    global db
    if db is None:
        try:
            import databento as databento_mod  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "databento package not found. Install it with `pip install databento`."
            ) from exc
        db = databento_mod
    return db


@dataclass
class DayResult:
    trading_date: dt.date
    databento_symbol: str
    stype_in_used: str
    average_spread: Optional[float]
    average_mid: Optional[float]
    average_spread_bps: Optional[float]
    spread_column_pair: Optional[Tuple[str, str]]
    total_volume: Optional[float]
    trade_volume_column: Optional[str]
    bbo_rows: int
    trade_rows: int


def load_api_key() -> str:
    if not os.path.exists(DB_SECRET_FILE_PATH):
        raise SystemExit(
            f"Credentials file not found at {DB_SECRET_FILE_PATH}. Please create it."
        )
    try:
        with open(DB_SECRET_FILE_PATH, "r", encoding="utf-8") as cred_file:
            creds = json.load(cred_file)
            return creds["api_key"]
    except KeyError as exc:
        raise SystemExit(
            f"API key not found within {DB_SECRET_FILE_PATH}. Ensure it contains 'api_key'."
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Failed to parse credentials file: {exc}") from exc


def parse_date(date_str: str) -> dt.date:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse date '{date_str}'. Use YYYY-MM-DD or YYYYMMDD."
    )


def parse_time(time_str: str) -> dt.time:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return dt.datetime.strptime(time_str, fmt).time()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse time '{time_str}'. Use HH:MM or HH:MM:SS."
    )


def daterange(start_date: dt.date, end_date: dt.date) -> List[dt.date]:
    if end_date < start_date:
        raise argparse.ArgumentTypeError("end-date must be on or after start-date")
    delta = (end_date - start_date).days
    return [start_date + dt.timedelta(days=offset) for offset in range(delta + 1)]


def to_iso_utc(date_val: dt.date, time_val: dt.time, tz_name: str) -> str:
    local_tz = ZoneInfo(tz_name)
    combined = dt.datetime.combine(date_val, time_val, tzinfo=local_tz)
    return combined.astimezone(ZoneInfo("UTC")).isoformat()


def build_symbol_trials(
    market: str,
    symbol: str,
    trading_date: dt.date,
    stype_in: str,
    override_symbol: Optional[str],
    use_dated_rollovers: bool,
) -> List[Tuple[str, str]]:
    trials: List[Tuple[str, str]] = []

    def add_trial(sym: Optional[str], stype: str) -> None:
        if not sym:
            return
        candidate = (sym, stype)
        if candidate not in trials:
            trials.append(candidate)

    if override_symbol:
        add_trial(override_symbol, stype_in)
        return trials

    if market == "cme":
        date_str = trading_date.strftime("%Y%m%d")
        if symbolizer is not None and (use_dated_rollovers or stype_in == "raw_symbol"):
            try:
                mapped = symbolizer.ours_to_databento_dated(symbol, date_str)
                add_trial(mapped, "raw_symbol")
            except Exception as exc:
                print(
                    f"{trading_date:%Y-%m-%d}: failed dated symbol translation for {symbol}: {exc}",
                    file=sys.stderr,
                )

        if symbolizer is not None:
            try:
                ranked = symbolizer.ours_to_databento(symbol)
                add_trial(ranked, "continuous")
            except Exception:
                pass

        add_trial(f"{symbol}.c.0", "continuous")
        add_trial(symbol, stype_in)
    else:
        add_trial(symbol, stype_in)

    return trials


def detect_spread_columns(df: "pd.DataFrame") -> Optional[Tuple[str, str]]:
    for ask_col, bid_col in SPREAD_COLUMN_CANDIDATES:
        if ask_col in df.columns and bid_col in df.columns:
            return ask_col, bid_col
    return None


def compute_spread_stats(
    df: "pd.DataFrame",
) -> Tuple[Optional[float], Optional[Tuple[str, str]], Optional[float], Optional[float]]:
    ensure_pandas()
    if df.empty:
        return None, None, None, None
    column_pair = detect_spread_columns(df)
    if column_pair is None:
        return None, None, None, None
    ask_col, bid_col = column_pair
    subset = df[[ask_col, bid_col]].replace([pd.NA, pd.NaT], pd.NA).dropna()
    if subset.empty:
        return None, column_pair, None, None
    spread = subset[ask_col] - subset[bid_col]
    mid = (subset[ask_col] + subset[bid_col]) / 2
    valid_mask = mid > 0
    spread = spread[valid_mask]
    mid = mid[valid_mask]
    if spread.empty or mid.empty:
        return None, column_pair, None, None

    avg_spread = float(spread.mean())
    avg_mid = float(mid.mean()) if not mid.empty else None

    avg_bps = None
    if avg_mid is not None and avg_mid != 0:
        spread_over_mid = (spread / mid) * 10000
        spread_over_mid = spread_over_mid.replace([pd.NA, pd.NaT], pd.NA).dropna()
        if not spread_over_mid.empty:
            avg_bps = float(spread_over_mid.mean())

    return avg_spread, column_pair, avg_mid, avg_bps


def compute_total_volume(df: "pd.DataFrame") -> Tuple[Optional[float], Optional[str]]:
    ensure_pandas()
    if df.empty:
        return None, None
    for column in VOLUME_COLUMNS:
        if column in df.columns:
            series = df[column].replace([pd.NA, pd.NaT], pd.NA).dropna()
            if series.empty:
                continue
            return float(series.sum()), column
    return None, None


def fetch_dataframe(
    client: "db.Historical",
    dataset: str,
    schema: str,
    stype_in: str,
    symbol: str,
    start_iso: str,
    end_iso: str,
) -> "pd.DataFrame":
    try:
        data = client.timeseries.get_range(
            dataset=dataset,
            schema=schema,
            stype_in=stype_in,
            symbols=symbol,
            start=start_iso,
            end=end_iso,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Databento request failed for schema '{schema}', symbol '{symbol}': {exc}"
        ) from exc

    try:
        return data.to_df()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to convert Databento data to DataFrame for schema '{schema}': {exc}"
        ) from exc


def calculate_for_day(
    client: "db.Historical",
    market: str,
    symbol: str,
    trading_date: dt.date,
    dataset: str,
    stype_in: str,
    time_window: Tuple[dt.time, dt.time],
    tz_name: str,
    schema_bbo: str,
    schema_trades: str,
    override_symbol: Optional[str],
    use_dated_rollovers: bool,
) -> DayResult:
    bbo_start_iso = to_iso_utc(trading_date, time_window[0], tz_name)
    bbo_end_iso = to_iso_utc(trading_date, time_window[1], tz_name)

    trials = build_symbol_trials(
        market=market,
        symbol=symbol,
        trading_date=trading_date,
        stype_in=stype_in,
        override_symbol=override_symbol,
        use_dated_rollovers=use_dated_rollovers,
    )

    errors = []
    for trial_symbol, trial_stype in trials:
        try:
            bbo_df = fetch_dataframe(
                client, dataset, schema_bbo, trial_stype, trial_symbol, bbo_start_iso, bbo_end_iso
            )
            trades_df = fetch_dataframe(
                client, dataset, schema_trades, trial_stype, trial_symbol, bbo_start_iso, bbo_end_iso
            )
        except RuntimeError as exc:
            errors.append(f"{trial_symbol} ({trial_stype}) -> {exc}")
            continue

        avg_spread, spread_cols, avg_mid, avg_bps = compute_spread_stats(bbo_df)
        total_volume, volume_col = compute_total_volume(trades_df)

        if trials and (trial_symbol, trial_stype) != trials[0]:
            print(
                f"{trading_date:%Y-%m-%d}: resolved to symbol '{trial_symbol}' with stype_in='{trial_stype}'",
                file=sys.stderr,
            )

        return DayResult(
            trading_date=trading_date,
            databento_symbol=trial_symbol,
            stype_in_used=trial_stype,
            average_spread=avg_spread,
            average_mid=avg_mid,
            average_spread_bps=avg_bps,
            spread_column_pair=spread_cols,
            total_volume=total_volume,
            trade_volume_column=volume_col,
            bbo_rows=len(bbo_df),
            trade_rows=len(trades_df),
        )

    joined_errors = "; ".join(errors) if errors else "no symbol attempts"
    raise RuntimeError(
        "All symbol attempts failed. "
        "Try specifying --databento-symbol and --stype-in explicitly. Details: "
        + joined_errors
    )


def summarize(results: List[DayResult]) -> None:
    if not results:
        print("No results to summarize.")
        return

    header = (
        f"{'Date':<12} {'Symbol':<14} {'Stype':<10} {'AvgSpread':>12} {'AvgMid':>12}"
        f" {'SpreadBps':>12} {'TotalVol':>12} {'BBORows':>8} {'TradeRows':>10}"
    )
    print(header)
    print("-" * len(header))

    agg_spreads = []
    agg_mid = []
    agg_bps = []
    agg_volumes = []
    for res in results:
        avg_spread_display = f"{res.average_spread:.6f}" if res.average_spread is not None else "-"
        avg_mid_display = f"{res.average_mid:.6f}" if res.average_mid is not None else "-"
        avg_bps_display = f"{res.average_spread_bps:.2f}" if res.average_spread_bps is not None else "-"
        total_vol_display = f"{res.total_volume:.2f}" if res.total_volume is not None else "-"
        print(
            f"{res.trading_date:%Y-%m-%d} {res.databento_symbol:<14} {res.stype_in_used:<10}"
            f" {avg_spread_display:>12} {avg_mid_display:>12} {avg_bps_display:>12}"
            f" {total_vol_display:>12} {res.bbo_rows:>8} {res.trade_rows:>10}"
        )

        if res.average_spread is not None:
            agg_spreads.append(res.average_spread)
        if res.average_mid is not None:
            agg_mid.append(res.average_mid)
        if res.average_spread_bps is not None:
            agg_bps.append(res.average_spread_bps)
        if res.total_volume is not None:
            agg_volumes.append(res.total_volume)

    print("-")
    if agg_spreads:
        print(f"Overall average spread: {sum(agg_spreads) / len(agg_spreads):.6f}")
    else:
        print("Overall average spread: n/a")

    if agg_mid:
        print(f"Overall average mid: {sum(agg_mid) / len(agg_mid):.6f}")
    else:
        print("Overall average mid: n/a")

    if agg_bps:
        print(f"Overall average spread (bps): {sum(agg_bps) / len(agg_bps):.2f}")
    else:
        print("Overall average spread (bps): n/a")

    if agg_volumes:
        print(f"Overall total volume: {sum(agg_volumes):.2f}")
    else:
        print("Overall total volume: n/a")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute average spread and volume over a date/time window using Databento data."
    )
    parser.add_argument("--symbol", required=True, help="Base symbol, e.g. NQ or QQQ")
    parser.add_argument(
        "--market",
        choices=("cme", "equity"),
        required=True,
        help="Market family to determine default dataset and symbol handling.",
    )
    parser.add_argument("--start-date", required=True, type=parse_date, help="Start date inclusive")
    parser.add_argument("--end-date", required=True, type=parse_date, help="End date inclusive")
    parser.add_argument(
        "--start-time", default="00:00", type=parse_time, help="Start time (HH:MM) in local TZ"
    )
    parser.add_argument(
        "--end-time", default="23:59:59", type=parse_time, help="End time (HH:MM[:SS]) in local TZ"
    )
    parser.add_argument(
        "--timezone",
        default="America/New_York",
        help="Timezone for provided times (IANA identifier, default America/New_York)",
    )
    parser.add_argument(
        "--dataset",
        help="Override Databento dataset (default GLBX.MDP3 for cme, EQUS.MBO for equity)",
    )
    parser.add_argument(
        "--stype-in",
        default="raw_symbol",
        help="Databento stype_in value (default raw_symbol)",
    )
    parser.add_argument(
        "--schema-bbo",
        #default="bbo-1s",
        default="cbbo-1s",
        help="Schema to use for spread calculation (default bbo-1s)",
    )
    parser.add_argument(
        "--schema-trades",
        default="trades",
        help="Schema to use for volume calculation (default trades)",
    )
    parser.add_argument(
        "--databento-symbol",
        help="Explicit Databento symbol to use (overrides automatic mapping)",
    )
    parser.add_argument(
        "--use-dated-rollovers",
        action="store_true",
        help="For CME symbols, use symbolizer.ours_to_databento_dated to map by date",
    )
    return parser


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    dates = daterange(args.start_date, args.end_date)
    api_key = load_api_key()

    dataset = args.dataset or DEFAULT_DATASETS[args.market]

    ensure_pandas()
    databento_mod = ensure_databento()

    client = databento_mod.Historical(key=api_key)

    results: List[DayResult] = []
    for date_val in dates:
        try:
            day_result = calculate_for_day(
                client=client,
                market=args.market,
                symbol=args.symbol,
                trading_date=date_val,
                dataset=dataset,
                stype_in=args.stype_in,
                time_window=(args.start_time, args.end_time),
                tz_name=args.timezone,
                schema_bbo=args.schema_bbo,
                schema_trades=args.schema_trades,
                override_symbol=args.databento_symbol,
                use_dated_rollovers=args.use_dated_rollovers,
            )
        except Exception as exc:
            print(f"{date_val:%Y-%m-%d}: failed to compute ({exc})")
            continue
        results.append(day_result)

    summarize(results)


if __name__ == "__main__":
    main()
