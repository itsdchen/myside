#!/usr/bin/env python3
"""

Simple CLI to surface upcoming US equity events for market-making workflows.

Done with financialmodellingprep.com's free api. 

Example: 

(v1) david@GoingMerry: ~/tradefi/retraded_1/overmind/strat_main/tools (invigilation) $ ./event_watch.py --macro| g country=US | g -v Low | g -v Medium | h
2026-01-08 13:30 | -      | macro | Continuing Jobless Claims (Dec/27)                   | actual=1914, country=US, estimate=1900, impact=High, previous=1858, unit=K     
2026-01-08 13:30 | -      | macro | Initial Jobless Claims (Jan/03)                      | actual=208, country=US, estimate=210, impact=High, previous=200, unit=K        
2026-01-08 13:30 | -      | macro | Jobless Claims 4-Week Average (Jan/03)               | actual=211.75, country=US, estimate=210.75, impact=High, previous=219, unit=K  
2026-01-09 13:30 | -      | macro | Nonfarm Payrolls Private (Dec)                       | country=US, estimate=64, impact=High, previous=69, unit=K                      
2026-01-09 13:30 | -      | macro | Housing Starts (Sep)                                 | country=US, estimate=1.33, impact=High, previous=1.307, unit=M                 
2026-01-09 13:30 | -      | macro | U-6 Unemployment Rate (Dec)                          | country=US, estimate=8.8, impact=High, previous=8.7, unit=%                    
2026-01-09 13:30 | -      | macro | Unemployment Rate (Dec)                              | country=US, estimate=4.5, impact=High, previous=4.6, unit=%                    
2026-01-09 13:30 | -      | macro | Non Farm Payrolls (Dec)                              | country=US, estimate=60, impact=High, previous=64, unit=K                      
2026-01-09 13:30 | -      | macro | Housing Starts (Oct)                                 | country=US, estimate=1.33, impact=High, previous=1.307                         
2026-01-09 15:00 | -      | macro | Michigan Consumer Sentiment (Jan)                    | country=US, estimate=53.5, impact=High, previous=52.9                          


"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional

import getpass

import requests

BASE_URL = "https://financialmodelingprep.com/api/v3"
ALT_BASE_URL = "https://financialmodelingprep.com/api"
DEFAULT_WINDOW_DAYS = 30
EARNINGS_TIME_HINT = {
    "bmo": dt.time(8, 0),  # before market open
    "amc": dt.time(16, 30),  # after market close
    "dmt": dt.time(12, 0),  # during market
}


class EventFetchError(RuntimeError):
    """Raised when an API request fails."""


@dataclass
class UpcomingEvent:
    when: dt.datetime
    event_type: str
    title: str
    symbol: Optional[str] = None
    source: str = "fmp"
    metadata: Optional[Dict[str, Any]] = None

    def as_row(self) -> List[str]:
        meta = self.metadata or {}
        details = ", ".join(f"{k}={meta[k]}" for k in sorted(meta)) if meta else ""
        timestamp = self.when.strftime("%Y-%m-%d %H:%M")
        return [timestamp, self.symbol or "-", self.event_type, self.title, details]


@dataclass
class FetchContext:
    api_key: str
    session: requests.Session

    def get(self, endpoint: str, **params: Any) -> List[Dict[str, Any]]:
        cleaned = {k: v for k, v in params.items() if v is not None}
        cleaned["apikey"] = self.api_key
        url = self._build_url(endpoint)
        resp = self.session.get(url, params=cleaned, timeout=20)
        if resp.status_code != 200:
            raise EventFetchError(
                f"{endpoint} request failed with {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        if isinstance(payload, dict):
            if payload.get("error") or payload.get("message"):
                raise EventFetchError(
                    f"{endpoint} response error: {payload.get('error') or payload.get('message')}"
                )
            if "items" in payload and isinstance(payload["items"], list):
                return payload["items"]
            if "data" in payload and isinstance(payload["data"], list):
                return payload["data"]
            return [payload]
        return payload

    def _build_url(self, endpoint: str) -> str:
        if endpoint.startswith("v4/"):
            return f"{ALT_BASE_URL}/{endpoint}"
        return f"{BASE_URL}/{endpoint}"


def iso_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def combine_date_time(date_str: str, time_hint: Optional[str], default_hour: int = 9) -> dt.datetime:
    d = iso_date(date_str)
    time_hint = (time_hint or "").lower()
    if time_hint in EARNINGS_TIME_HINT:
        t = EARNINGS_TIME_HINT[time_hint]
    else:
        t = dt.time(default_hour, 0)
    return dt.datetime.combine(d, t)


def extract_numeric_metadata(record: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    payload = {}
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            payload[key] = value
    return payload


def parse_datetime_hint(value: str, default_hour: int = 9) -> dt.datetime:
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
        if parsed.tzinfo:
            return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    date_part = value.split(" ")[0]
    return combine_date_time(date_part, None, default_hour)


def fetch_with_fallback(ctx: FetchContext, endpoints: Iterable[str], **params: Any) -> List[Dict[str, Any]]:
    errors = []
    for endpoint in endpoints:
        try:
            return ctx.get(endpoint, **params)
        except EventFetchError as exc:
            errors.append(str(exc))
    raise EventFetchError("; ".join(errors))


def fetch_earnings(ctx: FetchContext, symbol: str, start: str, end: str) -> List[UpcomingEvent]:
    records = fetch_with_fallback(
        ctx,
        ("calendar/earnings", "earning_calendar"),
        **{"symbol": symbol, "from": start, "to": end},
    )
    events: List[UpcomingEvent] = []
    for rec in records:
        date_field = rec.get("date") or rec.get("earningsDate")
        if not date_field:
            continue
        events.append(
            UpcomingEvent(
                when=combine_date_time(date_field, rec.get("time")),
                event_type="earnings",
                title=f"Earnings ({rec.get('time', '').upper() or 'TBD'})",
                symbol=rec.get("symbol"),
                metadata=extract_numeric_metadata(
                    rec,
                    (
                        "epsEstimate",
                        "epsActual",
                        "eps",
                        "epsEstimated",
                        "revenueEstimate",
                        "revenueActual",
                        "revenue",
                        "revenueEstimated",
                        "year",
                        "quarter",
                    ),
                ),
            )
        )
    return events


def fetch_dividends(ctx: FetchContext, symbol: str, start: str, end: str) -> List[UpcomingEvent]:
    records = fetch_with_fallback(
        ctx,
        ("calendar/dividends", "dividend_calendar"),
        **{"symbol": symbol, "from": start, "to": end},
    )
    events: List[UpcomingEvent] = []
    for rec in records:
        date_field = rec.get("paymentDate") or rec.get("date")
        if not date_field:
            continue
        title = f"Dividend ${rec.get('dividend', 0)}"
        metadata = extract_numeric_metadata(
            rec,
            (
                "announcementDate",
                "recordDate",
                "dividend",
                "adjustedDividend",
                "frequency",
            ),
        )
        events.append(
            UpcomingEvent(
                when=combine_date_time(date_field, None, default_hour=8),
                event_type="dividend",
                title=title,
                symbol=rec.get("symbol"),
                metadata=metadata,
            )
        )
    return events


def fetch_splits(ctx: FetchContext, symbol: str, start: str, end: str) -> List[UpcomingEvent]:
    records = fetch_with_fallback(
        ctx,
        ("calendar/splits", "stock_split_calendar"),
        **{"symbol": symbol, "from": start, "to": end},
    )
    events: List[UpcomingEvent] = []
    for rec in records:
        date_field = rec.get("date") or rec.get("executionDate")
        if not date_field:
            continue
        events.append(
            UpcomingEvent(
                when=combine_date_time(date_field, None, default_hour=9),
                event_type="split",
                title=f"Split {rec.get('ratio')}",
                symbol=rec.get("symbol"),
                metadata=extract_numeric_metadata(
                    rec,
                    (
                        "numerator",
                        "denominator",
                        "exchange",
                    ),
                ),
            )
        )
    return events


def fetch_macro(ctx: FetchContext, start: str, end: str, importance: Optional[str]) -> List[UpcomingEvent]:
    records = ctx.get(
        "economic_calendar",
        **{"from": start, "to": end},
    )
    events: List[UpcomingEvent] = []
    for rec in records:
        if importance and rec.get("importance", "").lower() != importance.lower():
            continue
        date_field = rec.get("date") or rec.get("dateTime")
        if not date_field:
            continue
        when = parse_datetime_hint(date_field, default_hour=8)
        metadata = {
            k: rec.get(k)
            for k in (
                "country",
                "actual",
                "previous",
                "estimate",
                "unit",
                "impact",
            )
            if rec.get(k) not in (None, "")
        }
        events.append(
            UpcomingEvent(
                when=when,
                event_type="macro",
                title=rec.get("event" , "Macro Event"),
                symbol=None,
                metadata=metadata,
            )
        )
    return events


def collect_events(
    ctx: FetchContext,
    symbols: Iterable[str],
    start: dt.date,
    end: dt.date,
    include_macro: bool,
    macro_importance: Optional[str],
) -> List[UpcomingEvent]:
    start_str = start.isoformat()
    end_str = end.isoformat()
    events: List[UpcomingEvent] = []
    for sym in symbols:
        sym = sym.strip().upper()
        if not sym:
            continue
        events.extend(fetch_earnings(ctx, sym, start_str, end_str))
        events.extend(fetch_dividends(ctx, sym, start_str, end_str))
        events.extend(fetch_splits(ctx, sym, start_str, end_str))
    if include_macro:
        events.extend(fetch_macro(ctx, start_str, end_str, macro_importance))
    events.sort(key=lambda ev: ev.when)
    return events


def format_events_table(events: List[UpcomingEvent]) -> str:
    headers = ["Date (ET)", "Symbol", "Type", "Title", "Details"]
    rows = [ev.as_row() for ev in events] or [["(none)", "", "", "", ""]]
    widths = [max(len(row[i]) for row in [headers] + rows) for i in range(len(headers))]
    def fmt_row(row: List[str]) -> str:
        return " | ".join(value.ljust(widths[idx]) for idx, value in enumerate(row))
    lines = [fmt_row(headers), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt_row(r) for r in rows)
    return "\n".join(lines)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="US equity tickers (space separated)")
    parser.add_argument(
        "--symbols-file",
        help="Path to a file with one ticker per line (merged with CLI symbols)",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Look-ahead window (default {DEFAULT_WINDOW_DAYS} days)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Override start date (YYYY-MM-DD). Default is today.",
    )
    parser.add_argument(
        "--macro",
        action="store_true",
        default=False,
        help="Include high-level economic calendar events",
    )
    parser.add_argument(
        "--macro-importance",
        choices=["low", "medium", "high"],
        help="Filter macro events by reported importance",
    )
    parser.add_argument(
        "--api-key",
        help="Override FMP API key (CLI has precedence over creds/env)",
    )
    parser.add_argument(
        "--creds",
        help="Json containing FMP API key.",
        default="/home/{}/.creds/.fmp.creds.json".format(getpass.getuser())
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of a table",
    )
    return parser.parse_args(argv)


def load_symbols(args: argparse.Namespace) -> List[str]:
    symbols = list(args.symbols)
    if args.symbols_file:
        with open(args.symbols_file) as handle:
            symbols.extend(line.strip() for line in handle)
    deduped = []
    seen = set()
    for sym in symbols:
        sym = sym.strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        deduped.append(sym)
    return deduped


def resolve_api_key(args: argparse.Namespace) -> str:
    if getattr(args, "api_key", None):
        return args.api_key
    creds_path = getattr(args, "creds", None)
    if creds_path:
        try:
            with open(creds_path) as handle:
                payload = json.load(handle)
            if "api_key" in payload and payload["api_key"]:
                return payload["api_key"]
        except FileNotFoundError:
            pass
    env_key = os.environ.get("FMP_API_KEY")
    if env_key:
        return env_key
    raise SystemExit("Missing API key. Use --api-key, --creds, or set FMP_API_KEY")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    symbols = load_symbols(args)
    if not symbols and not args.macro:
        raise SystemExit("Provide at least one symbol or use --macro for macro-only mode")
    api_key = resolve_api_key(args)

    start = iso_date(args.start_date) if args.start_date else dt.date.today()
    end = start + dt.timedelta(days=args.window_days)
    ctx = FetchContext(api_key=api_key, session=requests.Session())
    events = collect_events(
        ctx,
        symbols,
        start,
        end,
        include_macro=args.macro,
        macro_importance=args.macro_importance,
    )
    if args.json:
        payload = [asdict(ev) for ev in events]
        print(json.dumps(payload, default=str, indent=2))
    else:
        print(format_events_table(events))


if __name__ == "__main__":
    main()
