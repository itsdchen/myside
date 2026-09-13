#!/usr/bin/env python3
"""Coverage report — show which HIP3 XYZ symbols are traded, when, and how many instances."""

import commentjson
import json
import os
import requests
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOCAL_BASE = os.path.expanduser("~/scratch/tradeperf")

# Default creds file lookup order (used when --creds is not passed).
# Lakeside is preferred on this machine; fall back to the generic file.
DEFAULT_CREDS_CANDIDATES = [
    os.path.expanduser("~/.creds/.Hyperliquid.Lakeside.creds.json"),
    os.path.expanduser("~/.creds/.Hyperliquid_Lakeside.creds.json"),
    os.path.expanduser("~/.creds/.Hyperliquid.creds.json"),
]

# ── Trading sessions (all times ET) ──────────────────────────────────────────
# These define contiguous blocks spanning a full 24h trading day (18:00→18:00).

SESSIONS = [
    ("Overnight",  (18, 0),  (4, 0)),   # 18:00 – 04:00
    ("Pre-US",     (4, 0),   (9, 30)),   # 04:00 – 09:30
    ("US Day",     (9, 30),  (16, 0)),   # 09:30 – 16:00
    ("Post-US",    (16, 0),  (18, 0)),   # 16:00 – 18:00
]


def _to_minutes(h, m):
    return h * 60 + m


def _parse_time(t_str):
    """Parse '09:30:00 America/New_York' → minutes since midnight."""
    hms = t_str.strip().split()[0]
    parts = hms.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _session_overlaps(start_m, end_m, sess_start, sess_end):
    """Check if a strat's [start_m, end_m) overlaps a session [sess_start, sess_end).

    Both the strat window and session window can wrap around midnight.
    """
    # Normalize all to sets of minutes within a 24h circle (0..1439).
    def _interval_set(a, b):
        if a < b:
            return set(range(a, b))
        else:  # wraps midnight
            return set(range(a, 1440)) | set(range(0, b))

    strat_set = _interval_set(start_m, end_m)
    sess_set = _interval_set(sess_start, sess_end)
    return bool(strat_set & sess_set)


def fetch_all_xyz_symbols():
    """Fetch all HIP3 XYZ symbols from the Hyperliquid API."""
    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "allMids", "dex": "xyz"},
            timeout=5,
        )
        return sorted(resp.json().keys())
    except Exception as e:
        print(f"[warn] Could not fetch XYZ symbols from API: {e}", file=sys.stderr)
        return []


def load_latest_configs(local_base, max_stale_days=4):
    """Walk tradeperf dirs, load the latest pk_*.json.YYYYMMDD per strat.

    Staleness check: for each machine, find the most recent config date among
    all configs on that machine. Any config whose latest date is more than
    max_stale_days behind that machine's max is considered stale (likely turned
    off by cron) and skipped. Set max_stale_days=None to disable.

    Returns list of dicts with keys: machine, group, strat, config, start_t, end_t,
    traders (list of {symbol, enabled}).
    """
    base = Path(local_base)

    # Pass 1: collect all (machine, group, strat, config_name, latest_date, path)
    raw_entries = []
    for machine_dir in sorted(base.iterdir()):
        if not machine_dir.is_dir():
            continue
        machine = machine_dir.name
        for group_dir in sorted(machine_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name

                config_files = defaultdict(list)
                for f in strat_dir.glob("pk_*.json.*"):
                    date_suffix = f.name.rsplit(".", 1)[-1]
                    if len(date_suffix) == 8 and date_suffix.isdigit():
                        config_name = f.name.rsplit(".", 1)[0]
                        config_files[config_name].append((date_suffix, f))

                for config_name, dated in config_files.items():
                    dated.sort(key=lambda x: x[0])
                    latest_date, latest_path = dated[-1]
                    raw_entries.append({
                        "machine": machine,
                        "group": group,
                        "strat": strat,
                        "config_name": config_name,
                        "latest_date": latest_date,
                        "latest_path": latest_path,
                    })

    # Pass 2: find the max date per machine
    machine_max_date = defaultdict(str)
    for entry in raw_entries:
        if entry["latest_date"] > machine_max_date[entry["machine"]]:
            machine_max_date[entry["machine"]] = entry["latest_date"]

    # Pass 3: load configs, skipping stale ones
    results = []
    for entry in raw_entries:
        latest_date = entry["latest_date"]

        if max_stale_days is not None:
            max_date = machine_max_date[entry["machine"]]
            try:
                max_dt = datetime.strptime(max_date, "%Y%m%d")
                entry_dt = datetime.strptime(latest_date, "%Y%m%d")
                if (max_dt - entry_dt).days > max_stale_days:
                    continue
            except ValueError:
                pass

        try:
            with open(entry["latest_path"]) as fh:
                cfg = commentjson.load(fh)
        except Exception as e:
            print(f"  [skip] {entry['latest_path']}: {e}", file=sys.stderr)
            continue

        start_t = cfg.get("settings", {}).get("start_t", "")
        end_t = cfg.get("settings", {}).get("end_t", "")

        # Check the undated (live) config for true enabled state
        live_enabled = {}
        live_path = str(entry["latest_path"]).rsplit(".", 1)[0]  # strip .YYYYMMDD
        if os.path.exists(live_path):
            try:
                with open(live_path) as lf:
                    live_cfg = commentjson.load(lf)
                for t in live_cfg.get("pktraders", []):
                    live_enabled[t.get("traded_symbol", "?")] = t.get("enabled", True)
            except Exception:
                pass

        traders = []
        for trader in cfg.get("pktraders", []):
            sym = trader.get("traded_symbol", "?")
            # Prefer live config's enabled state if available
            enabled = live_enabled.get(sym, trader.get("enabled", True))
            traders.append({
                "symbol": sym,
                "enabled": enabled,
            })

        results.append({
            "machine": entry["machine"],
            "group": entry["group"],
            "strat": entry["strat"],
            "config": entry["config_name"],
            "date": latest_date,
            "start_t": start_t,
            "end_t": end_t,
            "traders": traders,
        })

    return results


def _is_weekend_only(cfg):
    """rel_btc strats on non-gf0 machines only run on weekends."""
    return cfg["machine"] != "gf0" and "rel_btc" in cfg["strat"]


def build_coverage(configs):
    """Build coverage map: symbol → list of strat entries (only enabled traders).

    Each entry: {machine, group, strat, config, start_m, end_m}.
    Excludes weekend-only strats (rel_btc on non-gf0 machines).
    """
    coverage = defaultdict(list)
    weekend_only = defaultdict(list)
    for cfg in configs:
        if not cfg["start_t"] or not cfg["end_t"]:
            continue
        try:
            start_m = _parse_time(cfg["start_t"])
            end_m = _parse_time(cfg["end_t"])
        except (ValueError, IndexError):
            continue

        is_wkend = _is_weekend_only(cfg)
        for trader in cfg["traders"]:
            if not trader["enabled"]:
                continue
            entry = {
                "machine": cfg["machine"],
                "strat": cfg["strat"],
                "config": cfg["config"],
                "start_m": start_m,
                "end_m": end_m,
            }
            if is_wkend:
                weekend_only[trader["symbol"]].append(entry)
            else:
                coverage[trader["symbol"]].append(entry)

    return coverage, weekend_only


def build_disabled_set(configs):
    """Return set of symbols that appear only as disabled."""
    enabled_syms = set()
    all_syms = set()
    for cfg in configs:
        for trader in cfg["traders"]:
            all_syms.add(trader["symbol"])
            if trader["enabled"]:
                enabled_syms.add(trader["symbol"])
    return all_syms - enabled_syms


def build_report_text(all_xyz, coverage, disabled_syms, verbose=False, weekend_only=None):
    """Build the coverage report as a string."""
    if weekend_only is None:
        weekend_only = {}
    lines = []
    sess_names = [s[0] for s in SESSIONS]
    sess_header = "".join(f"{s:>12}" for s in sess_names)

    lines.append("")
    lines.append("=" * 100)
    lines.append("SYMBOL COVERAGE REPORT — instances per session")
    lines.append("=" * 100)
    lines.append("")
    lines.append(f"{'Symbol':<20} {sess_header}  {'Total':>7}")
    lines.append("-" * 100)

    covered_enabled = set()
    for sym in sorted(all_xyz):
        entries = coverage.get(sym, [])
        if entries:
            covered_enabled.add(sym)
            counts = []
            sess_entries = []
            for sess_name, sess_start, sess_end in SESSIONS:
                s_start = _to_minutes(*sess_start)
                s_end = _to_minutes(*sess_end)
                matching = [
                    e for e in entries
                    if _session_overlaps(e["start_m"], e["end_m"], s_start, s_end)
                ]
                counts.append(len(matching))
                sess_entries.append((sess_name, matching))
            total = len(entries)
            count_str = "".join(
                f"{(str(c) if c > 0 else '·'):>12}" for c in counts
            )
            lines.append(f"{sym:<20} {count_str}  {total:>7}")
            if verbose:
                for sess_name, matching in sess_entries:
                    if matching:
                        strat_names = ", ".join(
                            f"{e['machine']}/{e['strat']}" for e in matching
                        )
                        lines.append(f"    {sess_name:<12} {strat_names}")
        elif sym in disabled_syms:
            lines.append(f"{sym:<20} {'— disabled —':^55}")
        else:
            lines.append(f"{sym:<20} {'·':>12}{'·':>12}{'·':>12}{'·':>12}  {'0':>7}")

    # Summary
    no_coverage = sorted(set(all_xyz) - covered_enabled - disabled_syms)
    disabled_only = sorted(disabled_syms & set(all_xyz))

    lines.append("")
    lines.append("=" * 100)
    lines.append(f"Total HIP3 XYZ symbols:    {len(all_xyz)}")
    lines.append(f"Actively traded (enabled): {len(covered_enabled)}")
    lines.append(f"Configured but disabled:   {len(disabled_only)}")
    lines.append(f"No coverage at all:        {len(no_coverage)}")

    if no_coverage:
        lines.append(f"\nUncovered: {', '.join(no_coverage)}")
    if disabled_only:
        lines.append(f"Disabled:  {', '.join(disabled_only)}")

    if weekend_only:
        lines.append("")
        lines.append("=" * 100)
        lines.append("WEEKEND-ONLY COVERAGE (excluded from weekday counts above)")
        lines.append("=" * 100)
        lines.append("")
        lines.append(f"{'Symbol':<20} {sess_header}  {'Total':>7}")
        lines.append("-" * 100)

        for sym in sorted(weekend_only):
            wk_entries = weekend_only[sym]
            counts = []
            wk_sess_entries = []
            for sess_name, sess_start, sess_end in SESSIONS:
                s_start = _to_minutes(*sess_start)
                s_end = _to_minutes(*sess_end)
                matching = [
                    e for e in wk_entries
                    if _session_overlaps(e["start_m"], e["end_m"], s_start, s_end)
                ]
                counts.append(len(matching))
                wk_sess_entries.append((sess_name, matching))
            total = len(wk_entries)
            count_str = "".join(
                f"{(str(c) if c > 0 else '·'):>12}" for c in counts
            )
            lines.append(f"{sym:<20} {count_str}  {total:>7}")
            if verbose:
                for sess_name, matching in wk_sess_entries:
                    if matching:
                        strat_names = ", ".join(
                            f"{e['machine']}/{e['strat']}" for e in matching
                        )
                        lines.append(f"    {sess_name:<12} {strat_names}")

        lines.append("")
        lines.append(f"Weekend-only symbols: {len(weekend_only)}")
    lines.append("")

    return "\n".join(lines)


def build_schedule_text(all_xyz, coverage, disabled_syms, verbose=False):
    """Build the schedule report: per symbol, per session, which configs are active.

    Quick mode (verbose=False): count of configs per session.
    Detailed mode (verbose=True): list the config files per session.
    """
    lines = []
    sess_names = [s[0] for s in SESSIONS]
    sess_header = "".join(f"{s:>12}" for s in sess_names)

    lines.append("")
    lines.append("=" * 100)
    lines.append("SCHEDULE — active configs per symbol per session")
    lines.append("=" * 100)
    lines.append("")
    lines.append(f"{'Symbol':<20} {sess_header}  {'Total':>7}")
    lines.append("-" * 100)

    for sym in sorted(all_xyz):
        entries = coverage.get(sym, [])
        if not entries:
            if sym in disabled_syms:
                lines.append(f"{sym:<20} {'— disabled —':^55}")
            else:
                lines.append(f"{sym:<20} {'·':>12}{'·':>12}{'·':>12}{'·':>12}  {'0':>7}")
            continue

        # Group entries by session
        sess_matches = []
        for sess_name, sess_start, sess_end in SESSIONS:
            s_start = _to_minutes(*sess_start)
            s_end = _to_minutes(*sess_end)
            matching = [
                e for e in entries
                if _session_overlaps(e["start_m"], e["end_m"], s_start, s_end)
            ]
            sess_matches.append((sess_name, matching))

        counts = [len(m) for _, m in sess_matches]
        total = len(entries)
        count_str = "".join(
            f"{(str(c) if c > 0 else '·'):>12}" for c in counts
        )
        lines.append(f"{sym:<20} {count_str}  {total:>7}")

        if verbose:
            for sess_name, matching in sess_matches:
                if matching:
                    configs_list = sorted(set(
                        f"{e['machine']}/{e['strat']}/{e['config']}" for e in matching
                    ))
                    lines.append(f"  {sess_name + ':':<14} {', '.join(configs_list)}")

    # Summary
    covered = set(s for s in all_xyz if coverage.get(s))
    no_cov = sorted(set(all_xyz) - covered - disabled_syms)
    dis_only = sorted(disabled_syms & set(all_xyz))

    lines.append("")
    lines.append("=" * 100)
    lines.append(f"Total symbols:   {len(all_xyz)}")
    lines.append(f"Active:          {len(covered)}")
    lines.append(f"Disabled:        {len(dis_only)}")
    lines.append(f"No coverage:     {len(no_cov)}")
    if no_cov:
        lines.append(f"\nUncovered: {', '.join(no_cov)}")
    lines.append("")

    return "\n".join(lines)


def find_turnoffs(local_base, max_days=60):
    """Walk dated configs and find symbols that were turned off or removed.

    Returns list of dicts with keys: symbol, config (display string),
    config_path (absolute path to live config), machine, group, strat,
    config_name, date_off, days_ago.
    """
    base = Path(local_base)
    today = datetime.today()
    cutoff = today - timedelta(days=max_days)
    cutoff_str = cutoff.strftime("%Y%m%d")
    results = []

    for machine_dir in sorted(base.iterdir()):
        if not machine_dir.is_dir():
            continue
        machine = machine_dir.name
        for group_dir in sorted(machine_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name

                # Collect dated config files grouped by config name
                config_files = defaultdict(list)
                for f in strat_dir.glob("pk_*.json.*"):
                    date_suffix = f.name.rsplit(".", 1)[-1]
                    if len(date_suffix) == 8 and date_suffix.isdigit():
                        config_name = f.name.rsplit(".", 1)[0]
                        config_files[config_name].append((date_suffix, f))

                for config_name, dated in config_files.items():
                    dated.sort(key=lambda x: x[0])
                    live_path = strat_dir / config_name

                    # Walk chronologically, track enabled symbols
                    prev_enabled = None
                    for date_str, path in dated:
                        try:
                            with open(path) as fh:
                                cfg = commentjson.load(fh)
                        except Exception:
                            continue
                        cur_enabled = set()
                        for t in cfg.get("pktraders", []):
                            if t.get("enabled", True):
                                cur_enabled.add(t.get("traded_symbol", "?"))

                        if prev_enabled is not None and date_str >= cutoff_str:
                            # Symbols that were enabled before but now aren't
                            turned_off = prev_enabled - cur_enabled
                            for sym in turned_off:
                                date_off = datetime.strptime(date_str, "%Y%m%d")
                                days_ago = (today - date_off).days
                                results.append({
                                    "symbol": sym,
                                    "config": f"{machine}/{strat}/{config_name}",
                                    "config_path": str(live_path),
                                    "machine": machine,
                                    "group": group,
                                    "strat": strat,
                                    "config_name": config_name,
                                    "date_off": date_str,
                                    "days_ago": days_ago,
                                })

                        prev_enabled = cur_enabled

    # Deduplicate: keep the most recent turnoff per (symbol, config_path)
    seen = {}
    for r in results:
        key = (r["symbol"], r["config_path"])
        if key not in seen or r["date_off"] > seen[key]["date_off"]:
            seen[key] = r

    # Check if symbol is still off (not re-enabled in the live config)
    final = []
    for r in seen.values():
        live_path = Path(r["config_path"])
        still_off = True
        if live_path.exists():
            try:
                with open(live_path) as fh:
                    live_cfg = commentjson.load(fh)
                live_enabled = set()
                for t in live_cfg.get("pktraders", []):
                    if t.get("enabled", True):
                        live_enabled.add(t.get("traded_symbol", "?"))
                if r["symbol"] in live_enabled:
                    still_off = False
            except Exception:
                pass
        if still_off:
            final.append(r)

    final.sort(key=lambda r: (r["symbol"], r["date_off"]))
    return final


def build_resim_text(turnoffs, max_days=60):
    """Build resim candidates report."""
    lines = []
    lines.append("")
    lines.append("=" * 105)
    lines.append(f"RESIM CANDIDATES — symbols turned off in the last {max_days} days (still off)")
    lines.append("=" * 105)
    lines.append("")

    if not turnoffs:
        lines.append("No turnoffs found.")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"{'Symbol':<20} {'Turned off':<14} {'Days ago':>8}  {'Config'}")
    lines.append("-" * 105)

    # Group by symbol for readability
    by_sym = defaultdict(list)
    for r in turnoffs:
        by_sym[r["symbol"]].append(r)

    for sym in sorted(by_sym):
        entries = sorted(by_sym[sym], key=lambda r: r["date_off"])
        for i, r in enumerate(entries):
            date_fmt = f"{r['date_off'][:4]}-{r['date_off'][4:6]}-{r['date_off'][6:]}"
            sym_col = sym if i == 0 else ""
            lines.append(f"{sym_col:<20} {date_fmt:<14} {r['days_ago']:>8}  {r['config']}")

    # Summary
    unique_syms = sorted(by_sym.keys())
    lines.append("")
    lines.append("=" * 105)
    lines.append(f"Total turnoffs:    {len(turnoffs)}")
    lines.append(f"Unique symbols:    {len(unique_syms)}")
    lines.append(f"\nSymbols: {', '.join(unique_syms)}")
    lines.append("")

    return "\n".join(lines)


def resolve_creds_path(explicit=None):
    """Resolve which Hyperliquid creds file to use.

    Order: explicit --creds path > Lakeside > generic Hyperliquid file.
    Returns None if nothing exists.
    """
    if explicit:
        # Honour the user's explicit choice even if the file is missing
        # (we want a clear error from the caller in that case).
        return os.path.expanduser(explicit)
    for candidate in DEFAULT_CREDS_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def fetch_open_positions(creds_path):
    """Fetch open HIP3 XYZ positions for the wallet derived from `creds_path`.

    Returns dict: symbol -> {"szi": signed size, "notional": abs USD notional}.
    Only symbols with non-zero size are returned.
    """
    # Lazy import — eth_account is only needed when the user asks for positions.
    try:
        import eth_account
    except ImportError as e:
        print(f"[warn] eth_account not installed; cannot fetch positions: {e}",
              file=sys.stderr)
        return {}

    try:
        with open(creds_path) as f:
            creds = json.load(f)
        wallet = eth_account.Account.from_key(creds["secret_key"]).address
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": wallet, "dex": "xyz"},
            timeout=5,
        )
        data = resp.json()
    except Exception as e:
        print(f"[warn] Could not fetch positions from {creds_path}: {e}",
              file=sys.stderr)
        return {}

    positions = {}
    for ap in data.get("assetPositions", []):
        p = ap.get("position", {})
        sym = p.get("coin", "?")
        try:
            szi = float(p.get("szi", 0))
            notional = float(p.get("positionValue", 0))
        except (TypeError, ValueError):
            continue
        if szi != 0:
            positions[sym] = {"szi": szi, "notional": notional}
    return positions


def build_uncovered_positions_text(positions, covered_enabled, creds_path):
    """Build a section listing open positions in symbols with no active coverage.

    `covered_enabled` is the set of symbols currently traded by an enabled strat
    (weekday coverage); anything not in that set counts as "no coverage" here.
    """
    lines = []
    lines.append("")
    lines.append("=" * 100)
    lines.append("OPEN POSITIONS IN UNCOVERED SYMBOLS")
    lines.append(f"  (creds: {creds_path})")
    lines.append("=" * 100)
    lines.append("")

    # Filter to positions whose symbol has no active enabled coverage.
    uncovered = {
        sym: info for sym, info in positions.items()
        if sym not in covered_enabled
    }

    if not uncovered:
        lines.append("No open positions in uncovered symbols.")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"{'Symbol':<20} {'Size':>16} {'Notional (USD)':>18}")
    lines.append("-" * 100)
    total_notional = 0.0
    # Sort by absolute notional descending so the biggest exposures stick out.
    for sym in sorted(uncovered, key=lambda s: -uncovered[s]["notional"]):
        info = uncovered[sym]
        total_notional += info["notional"]
        lines.append(f"{sym:<20} {info['szi']:>16.4f} {info['notional']:>18,.2f}")

    lines.append("-" * 100)
    lines.append(f"{'TOTAL':<20} {'':>16} {total_notional:>18,.2f}")
    lines.append("")
    return "\n".join(lines)


def main():
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(description="HIP3 XYZ symbol coverage report")
    parser.add_argument("--email", action="store_true", help="Send report via email")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show which strategies cover each symbol per session")
    parser.add_argument("--schedule", "-s", action="store_true",
                        help="Schedule view: configs per symbol per session")
    parser.add_argument("--resim", action="store_true",
                        help="Show symbols turned off recently as resim candidates")
    parser.add_argument("--days", type=int, default=60,
                        help="Lookback window for --resim (default: 60)")
    parser.add_argument("--creds", default=None,
                        help="Hyperliquid creds JSON path (default: "
                             "~/.creds/.Hyperliquid.Lakeside.creds.json, "
                             "fallback ~/.creds/.Hyperliquid_Lakeside.creds.json, "
                             "then ~/.creds/.Hyperliquid.creds.json)")
    args = parser.parse_args()

    if args.resim:
        turnoffs = find_turnoffs(LOCAL_BASE, max_days=args.days)
        text = build_resim_text(turnoffs, max_days=args.days)
        print(text)
    else:
        all_xyz = fetch_all_xyz_symbols()
        if not all_xyz:
            print("Could not fetch HIP3 XYZ symbol list. Aborting.")
            sys.exit(1)

        configs = load_latest_configs(LOCAL_BASE)
        coverage, weekend_only = build_coverage(configs)
        disabled_syms = build_disabled_set(configs)

        if args.schedule:
            text = build_schedule_text(all_xyz, coverage, disabled_syms,
                                       verbose=args.verbose)
        else:
            text = build_report_text(all_xyz, coverage, disabled_syms,
                                     verbose=args.verbose, weekend_only=weekend_only)
        print(text)

        # Append a section showing outstanding notional in symbols with no
        # active enabled coverage. Skipped if no creds file can be located.
        creds_path = resolve_creds_path(args.creds)
        if creds_path:
            positions = fetch_open_positions(creds_path)
            covered_enabled = set(s for s in all_xyz if coverage.get(s))
            print(build_uncovered_positions_text(
                positions, covered_enabled, creds_path))
        else:
            print(f"\n[info] No Hyperliquid creds file found in "
                  f"{DEFAULT_CREDS_CANDIDATES}; skipping open-position section.",
                  file=sys.stderr)

    if args.email:
        _STRAT_MAIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if _STRAT_MAIN not in sys.path:
            sys.path.insert(0, _STRAT_MAIN)
        from util import email_utils
        # Email always includes both: quick schedule on top, detailed below
        quick = build_schedule_text(all_xyz, coverage, disabled_syms, verbose=False)
        detailed = build_schedule_text(all_xyz, coverage, disabled_syms, verbose=True)
        email_body = quick + "\n\n" + detailed
        subject = f"Coverage Report — {datetime.today().strftime('%Y-%m-%d')}"
        email_utils.send_mail(subject=subject, body=email_body, monospace=True)
        print("Report emailed.")


if __name__ == "__main__":
    main()
