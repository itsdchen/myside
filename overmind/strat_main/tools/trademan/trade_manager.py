#!/usr/bin/env python3
"""Trade Manager v2 — fetch, report, anomaly flagging, config tracking, email."""

import argparse
import io
import commentjson
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util import email_utils

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG = {
    "ssh_aliases": ["gf0", "gf1", "gf2", "gf3"],
    "remote_base": "/home/ubuntu/estrader",
    "local_base": os.path.expanduser("~/scratch/tradeperf"),
    "alert_rules": [
        {"name": "3 consecutive losing days", "type": "consecutive_losing", "threshold": 3},
        {"name": "<=2 positive in last 6 days", "type": "min_positive_days", "window": 6, "threshold": 2},
        {"name": "norm_pnl/day < -20 over window", "type": "neg_norm_pnl", "window": 7, "threshold": -20},
        {"name": "win pct < 40% after 10+ days", "type": "low_winpct_after_min_days", "min_days": 10, "max_winpct": 0.40},
        {"name": "avg norm_pnl/day < $5 (10+ days)", "type": "low_avg_pnl", "min_days": 10, "threshold": 5.0},
    ],
    "sizeup_rule": {"window": 7, "min_positive": 5, "min_profit_factor": 2.0, "cooldown_days": 4},
    "turnoff_rule": {
        "window": 7,
        "max_fill_rate": 0.005,
        "max_avg_times_traded": 4,
        "strat_rollup_min_fraction": 0.5,
        "strat_rollup_min_count": 3,
    },
}


# ── Config parsing ────────────────────────────────────────────────────────────

def _load_pk_config(path):
    """Load a pk_*-style config. Most configs are plain JSON, but some contain
    JS-style comments — stdlib json is ~1000x faster than commentjson, so try
    it first and only fall back when comments make it fail.

    Raises ValueError if the parsed JSON doesn't look like a pk config (we
    fetch any *.json in a strat dir, so this is the shape gate).
    """
    with open(path) as f:
        text = f.read()
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError:
        cfg = commentjson.loads(text)
    if not (isinstance(cfg, dict) and "pktraders" in cfg):
        raise ValueError(f"{path}: missing 'pktraders' — not a pk config")
    return cfg


# ── SSH / SCP helpers ─────────────────────────────────────────────────────────

def _ssh_opts(alias):
    """SSH options that share one connection per host via ControlMaster.

    First ssh/scp to a given alias does the full handshake (~3s); subsequent
    calls reuse the connection (~0.3s). ControlPersist keeps the master alive
    briefly after the last client so close-spaced calls don't strand it.
    """
    ctl = f"/tmp/trademan-ssh-ctl-{alias}.sock"
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={ctl}",
        "-o", "ControlPersist=60s",
    ]


def ssh_run(alias, cmd, timeout=30):
    """Run a command on a remote host via SSH. Returns stdout lines."""
    try:
        result = subprocess.run(
            ["ssh"] + _ssh_opts(alias) + [alias, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  [warn] ssh {alias}: timed out after {timeout}s", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"  [warn] ssh {alias}: {result.stderr.strip()}", file=sys.stderr)
        return []
    return [l for l in result.stdout.strip().splitlines() if l]


def scp_files(alias, remote_paths, local_dir):
    """SCP multiple files from the same remote directory to a local directory.

    All remote_paths must be from the same directory. Returns number of files fetched.
    """
    os.makedirs(local_dir, exist_ok=True)
    sources = [f"{alias}:{p}" for p in remote_paths]
    try:
        result = subprocess.run(
            ["scp", "-q"] + _ssh_opts(alias) + sources + [local_dir],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"  [warn] scp from {alias}: timed out after 120s", file=sys.stderr)
        return 0
    if result.returncode != 0:
        print(f"  [warn] scp from {alias} failed: {result.stderr.strip()}", file=sys.stderr)
        return 0
    return len(remote_paths)


# ── Discovery & Fetch ─────────────────────────────────────────────────────────

import re as _re
import concurrent.futures as _cf

_RE_DATED_CONFIG = _re.compile(r'^.+\.json\.\d{8}$')
_RE_LIVE_CONFIG = _re.compile(r'^.+\.json$')


def discover_all_for_alias(alias, remote_base, dates):
    """Single SSH call that locates data files (acct/trades/orders/log),
    dated config backups (pk_*.json.YYYYMMDD), and live (undated) configs
    (pk_*.json).

    Returns (data_files, dated_configs, live_configs); each is a list of
    (remote_path, group, strat, fname) tuples.
    """
    date_strs = [d.strftime("%Y%m%d") for d in dates]
    name_patterns = []
    for ds in date_strs:
        name_patterns.append(f'-name "acct_{ds}.csv"')
        name_patterns.append(f'-name "trades_{ds}.csv"')
        name_patterns.append(f'-name "orders_{ds}.csv"')
        # Match both pktrade.foo.log.INFO.* and pktrade_0.foo.log.INFO.* —
        # multi-process strats get a _N suffix on the leading word.
        name_patterns.append(f'-name "pktrade*.log.INFO.{ds}-*"')
        name_patterns.append(f'-name "*.json.{ds}"')
    name_patterns.append('-name "*.json"')  # live (undated) configs
    name_expr = " -o ".join(name_patterns)
    cmd = f'find {remote_base} -type f \\( {name_expr} \\)'
    lines = ssh_run(alias, cmd, timeout=60)

    data_files, dated_configs, live_configs = [], [], []
    base_len = len(remote_base.rstrip("/")) + 1
    for line in lines:
        rel = line[base_len:]
        parts = rel.split("/")
        if len(parts) == 3:
            group, strat, fname = parts
        elif len(parts) == 2:
            group, fname = parts
            strat = "_root"
        else:
            continue
        entry = (line, group, strat, fname)
        if _RE_LIVE_CONFIG.match(fname):
            live_configs.append(entry)
        elif _RE_DATED_CONFIG.match(fname):
            dated_configs.append(entry)
        else:
            data_files.append(entry)
    return data_files, dated_configs, live_configs


def _fetch_one_alias(alias, dates, local_base, remote_base, force):
    """Discovery + scp's for a single alias. Returns a list of log lines for
    the orchestrator to print (we buffer log output to keep concurrent runs
    readable).
    """
    log = [f"[{alias}] Discovering files for last {len(dates)} day(s)..."]
    data_files, dated_configs, live_configs = discover_all_for_alias(
        alias, remote_base, dates)

    # ── Data files (acct/trades/orders/logs)
    if data_files:
        strat_set = sorted({(g, s) for _, g, s, _ in data_files})
        log.append(f"  [{alias}] Found {len(data_files)} data file(s) across "
                   f"{len(strat_set)} strat(s)")
        by_dir = defaultdict(list)
        skipped = 0
        for remote_path, group, strat, fname in data_files:
            local_path = os.path.join(local_base, alias, group, strat, fname)
            if not force and os.path.exists(local_path):
                skipped += 1
                continue
            by_dir[(group, strat)].append(remote_path)
        fetched = 0
        for (group, strat), paths in by_dir.items():
            local_dir = os.path.join(local_base, alias, group, strat)
            fetched += scp_files(alias, paths, local_dir)
        log.append(f"  [{alias}] Data: fetched {fetched}, skipped {skipped} (already local)")
    else:
        log.append(f"  [{alias}] No data files found")

    # ── Dated config backups
    if dated_configs:
        by_dir = defaultdict(list)
        skipped = 0
        for remote_path, group, strat, fname in dated_configs:
            local_path = os.path.join(local_base, alias, group, strat, fname)
            if os.path.exists(local_path):
                skipped += 1
                continue
            by_dir[(group, strat)].append(remote_path)
        fetched = 0
        for (group, strat), paths in by_dir.items():
            local_dir = os.path.join(local_base, alias, group, strat)
            fetched += scp_files(alias, paths, local_dir)
        if fetched or skipped:
            log.append(f"  [{alias}] Config backups: fetched {fetched}, skipped {skipped}")

    # ── Live (undated) configs — always re-fetch, scoped to local strats
    if live_configs:
        local_strats = set()
        alias_dir = Path(os.path.join(local_base, alias))
        if alias_dir.exists():
            for group_dir in alias_dir.iterdir():
                if not group_dir.is_dir():
                    continue
                for strat_dir in group_dir.iterdir():
                    if strat_dir.is_dir() and list(strat_dir.glob("*.json.[0-9]*")):
                        local_strats.add((group_dir.name, strat_dir.name))
        if local_strats:
            live_by_dir = defaultdict(list)
            for remote_path, group, strat, fname in live_configs:
                if (group, strat) in local_strats:
                    live_by_dir[(group, strat)].append(remote_path)
            live_fetched = 0
            for (group, strat), paths in live_by_dir.items():
                local_dir = os.path.join(local_base, alias, group, strat)
                live_fetched += scp_files(alias, paths, local_dir)
            if live_fetched:
                log.append(f"  [{alias}] Live configs: fetched {live_fetched}")
    return log


def fetch_all(aliases, days, local_base, remote_base, force=False):
    """Discover + fetch data files, dated configs, and live configs for all
    aliases in parallel. One ssh find per alias covers every pattern; aliases
    run concurrently.
    """
    dates = [datetime.today() - timedelta(days=i) for i in range(days)]

    with _cf.ThreadPoolExecutor(max_workers=max(1, len(aliases))) as pool:
        futures = {pool.submit(_fetch_one_alias, alias, dates, local_base,
                               remote_base, force): alias
                   for alias in aliases}
        for fut in _cf.as_completed(futures):
            alias = futures[fut]
            try:
                for line in fut.result():
                    print(line)
            except Exception as e:
                print(f"  [warn] {alias} fetch failed: {e}", file=sys.stderr)


# ── Parse ─────────────────────────────────────────────────────────────────────

def load_acct_data(local_base, aliases=None, groups=None, strats=None, symbols=None, date_from=None, date_to=None):
    """Load all local acct_*.csv files into a tagged DataFrame."""
    rows = []
    base = Path(local_base)
    if not base.exists():
        return pd.DataFrame()

    for alias_dir in sorted(base.iterdir()):
        if not alias_dir.is_dir():
            continue
        alias = alias_dir.name
        if aliases and alias not in aliases:
            continue
        for group_dir in sorted(alias_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            if groups and group not in groups:
                continue
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name
                if strats and strat not in strats:
                    continue
                for f in sorted(strat_dir.glob("acct_*.csv")):
                    try:
                        df = pd.read_csv(f)
                    except Exception as e:
                        print(f"  [warn] Failed to read {f}: {e}", file=sys.stderr)
                        continue
                    if df.empty:
                        continue
                    df["alias"] = alias
                    df["group"] = group
                    df["strat"] = strat
                    rows.append(df)

    if not rows:
        return pd.DataFrame()

    data = pd.concat(rows, ignore_index=True)

    # Filter by date range
    if "date" in data.columns:
        data["date"] = data["date"].astype(str)
        if date_from:
            data = data[data["date"] >= date_from]
        if date_to:
            data = data[data["date"] <= date_to]

    # Filter by symbol
    if symbols and "sym" in data.columns:
        data = data[data["sym"].isin(symbols)]

    return data


def _read_trade_symbols(path):
    """Return symbols with at least one row in a trades_YYYYMMDD.csv file."""
    import csv

    syms = set()
    try:
        with open(path, newline="", errors="replace") as fp:
            for row in csv.reader(fp):
                if len(row) > 3 and row[3]:
                    syms.add(str(row[3]))
    except Exception as e:
        print(f"  [warn] Failed to read {path}: {e}", file=sys.stderr)
        return None
    return syms


def _recent_traded_symbol_keys(local_base, data, cutoff_date):
    """Find (alias, group, strat, sym) keys that actually traded recently.

    Uses the union of trade-file rows and acct activity (times_traded/shs_traded)
    so stale open-position acct rows don't count, while minor acct/trade-file
    disagreements don't hide symbols with real activity.
    """
    needed = {"alias", "group", "strat", "sym", "date"}
    if data.empty or not needed.issubset(data.columns):
        return set()

    recent = data[data["date"].astype(str) >= cutoff_date]
    if recent.empty:
        return set()

    activity = pd.Series(False, index=recent.index)
    for c in ("times_traded", "shs_traded"):
        if c in recent.columns:
            activity |= pd.to_numeric(recent[c], errors="coerce").fillna(0) > 0

    recent_dates = sorted(recent["date"].astype(str).unique())
    acct_activity_by_strat_date = defaultdict(set)
    acct_cols = ["alias", "group", "strat", "date", "sym"]
    for row in recent.loc[activity, acct_cols].drop_duplicates().itertuples(index=False):
        acct_activity_by_strat_date[(row.alias, row.group, row.strat, str(row.date))].add(str(row.sym))

    allowed_by_strat = defaultdict(set)
    for row in recent[acct_cols].drop_duplicates().itertuples(index=False):
        strat_key = (row.alias, row.group, row.strat)
        allowed_by_strat[strat_key].add(str(row.sym))

    keys = set()
    for (alias, group, strat), allowed_syms in allowed_by_strat.items():
        strat_dir = os.path.join(local_base, alias, group, strat)
        for ds in recent_dates:
            traded_syms = set(acct_activity_by_strat_date.get((alias, group, strat, ds), set()))
            trade_path = os.path.join(strat_dir, f"trades_{ds}.csv")
            if os.path.exists(trade_path):
                file_syms = _read_trade_symbols(trade_path)
                if file_syms is not None:
                    traded_syms.update(file_syms)
            for sym in traded_syms & allowed_syms:
                keys.add((alias, group, strat, sym))

    return keys


# ── Anomaly Rules ─────────────────────────────────────────────────────────────

def _check_consecutive_losing(daily_pnl, threshold):
    """Return True if the last `threshold` days are all negative."""
    if len(daily_pnl) < threshold:
        return False
    return all(v < 0 for v in daily_pnl[-threshold:])


def _check_min_positive_days(daily_pnl, window, threshold):
    """Return True if positive days in last `window` days <= `threshold`."""
    recent = daily_pnl[-window:]
    if len(recent) < window:
        return False
    pos = sum(1 for v in recent if v > 0)
    return pos <= threshold


def _select_current_config_file(strat_dir):
    """Pick the live config whose name matches the newest dated snapshot."""
    dated_files = sorted(strat_dir.glob("*.json.[0-9]*"),
                         key=lambda p: (p.name.rsplit(".", 1)[-1], p.name))
    for dated_path in reversed(dated_files):
        live_path = dated_path.with_name(dated_path.name.rsplit(".", 1)[0])
        if live_path.exists():
            try:
                _load_pk_config(live_path)
                return live_path
            except Exception:
                pass
        try:
            _load_pk_config(dated_path)
            return dated_path
        except Exception:
            continue

    live_files = sorted(strat_dir.glob("*.json"),
                        key=lambda p: (p.stat().st_mtime, p.name))
    for path in reversed(live_files):
        try:
            _load_pk_config(path)
            return path
        except Exception:
            continue
    return None


# ── Active Symbol Detection ──────────────────────────────────────────────────

def get_active_symbols(local_base, strat_keys):
    """Read the current pk_*.json config for each strat to find enabled symbols.

    Args:
        local_base: local storage path (e.g. ~/scratch/tradeperf).
        strat_keys: set of (alias, group, strat) tuples to check.

    Returns a set of (alias, group, strat, sym) tuples where the symbol is enabled.
    """
    active = set()
    for alias, group, strat in strat_keys:
        strat_dir = Path(os.path.join(local_base, alias, group, strat))
        config_file = _select_current_config_file(strat_dir)
        if config_file is None:
            continue
        cfg = _load_pk_config(config_file)
        for trader in cfg.get("pktraders", []):
            sym = trader.get("traded_symbol", "")
            enabled = trader.get("enabled", False)
            if enabled and sym:
                active.add((alias, group, strat, sym))
    return active


# ── Size-Up Candidates ───────────────────────────────────────────────────────

def _load_strat_configs(local_base, strat_keys):
    """Load the current pk_*.json per strat once.

    Reads exactly one config file per strat. Returns dict:
    (alias, group, strat) → {"traders": {sym: trader_dict}, "settings": {...}}.
    Uses the live undated config whose name matches the newest dated snapshot,
    falling back to dated/live parseable files when needed. Strats whose config
    is missing/unparseable map to an entry with empty traders / settings.
    """
    cache = {}
    for alias, group, strat in strat_keys:
        strat_dir = Path(os.path.join(local_base, alias, group, strat))
        entry = {"traders": {}, "settings": {}}
        config_file = _select_current_config_file(strat_dir)
        if config_file is not None:
            cfg = _load_pk_config(config_file)
            for trader in cfg.get("pktraders", []):
                sym = trader.get("traded_symbol", "")
                if sym:
                    entry["traders"][sym] = trader
            entry["settings"] = cfg.get("settings", {})
        cache[(alias, group, strat)] = entry
    return cache


def _last_size_increase(local_base, alias, group, strat, sym, history_cache):
    """Find the most recent date when size_mult was increased for a symbol.

    Loads each strat's full dated config history at most once, memoized in
    `history_cache` (a dict provided by the caller).

    Returns (date_str, old_mult, new_mult) or None if no increase found.
    """
    key = (alias, group, strat)
    if key not in history_cache:
        import glob as globmod
        strat_dir = os.path.join(local_base, alias, group, strat)
        pattern = os.path.join(strat_dir, "*.json.[0-9]*")
        history = []
        for fpath in sorted(globmod.glob(pattern)):
            date_str = fpath.rsplit(".", 1)[-1]
            try:
                history.append((date_str, _load_pk_config(fpath)))
            except Exception:
                continue
        history_cache[key] = history
    history = history_cache[key]
    if len(history) < 2:
        return None

    prev_mult = None
    prev_date = None
    for date_str, cfg in reversed(history):
        mult = None
        for trader in cfg.get("pktraders", []):
            if trader.get("traded_symbol") == sym:
                mult = trader.get("size_mult", 1)
                break
        if mult is None:
            continue
        if prev_mult is not None and mult < prev_mult:
            # prev_mult (more recent) > mult (older) => increase happened on the prev date
            return (prev_date, mult, prev_mult)
        prev_mult = mult
        prev_date = date_str

    return None


def evaluate_sizeup_candidates(data, local_base, strat_configs, rule):
    """Find symbols that are candidates for size increase.

    Args:
        data: acct DataFrame with alias, group, strat, sym, date, net_pnl columns.
        local_base: local storage path (used to lazy-load full config history
            when the cooldown / last-increase walk is needed).
        strat_configs: dict from `_load_strat_configs` keyed by (alias, group, strat).
        rule: dict with 'window' and 'min_positive' keys.

    Returns list of dicts with: alias, group, strat, sym, positive_days, window,
        total_pnl, current_mult, last_increase.
    """
    import math
    window = rule["window"]
    min_positive = rule["min_positive"]
    min_pf = rule.get("min_profit_factor", 0.0)
    cooldown_days = rule.get("cooldown_days", 0)
    # Symbol must have traded within this many calendar days of today to be
    # eligible — keeps strats that went dark weeks ago from qualifying off an
    # old positive streak. 4 days covers a normal Fri→Mon gap plus one holiday.
    recency_days = rule.get("recency_days", 4)
    candidates = []
    history_cache = {}

    recency_cutoff = (datetime.today() - timedelta(days=recency_days)).strftime("%Y%m%d")

    for (alias, group, strat, sym), grp_df in data.groupby(["alias", "group", "strat", "sym"]):
        daily = grp_df.groupby("date")["net_pnl"].sum().sort_index()
        if daily.empty or daily.index[-1] < recency_cutoff:
            continue
        recent = daily.values.tolist()[-window:]
        if len(recent) < window:
            continue
        pos_days = sum(1 for v in recent if v > 0)
        if pos_days < min_positive:
            continue

        # Profit-factor gate: sum(wins) / |sum(losses)|. Default-pass when no losing days.
        wins = sum(v for v in recent if v > 0)
        losses = sum(-v for v in recent if v < 0)
        profit_factor = math.inf if losses == 0 else wins / losses
        if profit_factor < min_pf:
            continue

        trader = strat_configs.get((alias, group, strat), {}).get("traders", {}).get(sym)
        if not trader or not trader.get("enabled", False):
            continue
        current_mult = trader.get("size_mult", 1)

        last_inc = _last_size_increase(local_base, alias, group, strat, sym, history_cache)

        # Cooldown: suppress if the last size increase was recent.
        if last_inc and cooldown_days > 0:
            try:
                inc_date = datetime.strptime(last_inc[0], "%Y%m%d")
                if (datetime.today() - inc_date).days < cooldown_days:
                    continue
            except ValueError:
                pass

        candidates.append({
            "alias": alias,
            "group": group,
            "strat": strat,
            "sym": sym,
            "positive_days": pos_days,
            "window": window,
            "total_pnl": sum(recent),
            "profit_factor": profit_factor,
            "current_mult": current_mult,
            "last_increase": last_inc,
        })

    # Sort by alias, then strat (group/strat), then pnl descending within each
    candidates.sort(key=lambda c: (c["alias"], f"{c['group']}/{c['strat']}", -c["total_pnl"]))
    return candidates


def _fmt_ord_vs_ins(x):
    """Ratio formatter for ord_vs_ins with eat-the-book flag.

    Adds ! when our clip ≥ median inside (sweeps levels on a cross),
    !! when ≥ 2× median inside (sweeps multiple levels by design).
    """
    if x is None:
        return "—"
    s = f"{x:.2f}x"
    if x >= 2.0:
        s += "!!"
    elif x >= 1.0:
        s += "!"
    return s


def _fmt_pct(x):
    if x is None:
        return "—"
    return f"{x*100:.2f}%"


def _capacity_cells(c, capacity):
    """Return formatted capacity column strings for a candidate row.

    Returns a tuple (mkt_share, ord_vs_ins, fills) of strings, or None
    if no capacity dict was passed.
    """
    if not capacity:
        return None
    cap = capacity.get((c["alias"], c["group"], c["strat"], c["sym"]))
    if cap is None or cap.get("fills", 0) == 0:
        return ("—", "—", "0")
    return (
        _fmt_pct(cap.get("mkt_share")),
        _fmt_ord_vs_ins(cap.get("ord_vs_ins")),
        str(cap["fills"]),
    )


def format_sizeup_candidates(candidates, out, capacity=None):
    """Write size-up candidates section as a table to output buffer."""
    if not candidates:
        return
    window = candidates[0]["window"]
    min_pos = min(c["positive_days"] for c in candidates)
    out.write("=" * 140 + "\n")
    out.write("SIZE-UP CANDIDATES\n")
    out.write("=" * 140 + "\n")
    out.write(f"Symbols with >={min_pos}/{window} positive days, profit factor gate, "
              "and cooldown since last increase.\n\n")

    pnl_label = f"pnl({window}d)"
    cap_hdr = (f" {'mkt_shr':>8} {'ord/ins':>9} {'fills':>6} "
               if capacity is not None else "  ")
    hdr = (f"{'alias':<6} {'strat':<42} {'symbol':<14} {'days+':<6} {pnl_label:>10} "
           f"{'pf':>6} {'sz_mult':>7}{cap_hdr}{'last increase'}")
    out.write(hdr + "\n")
    out.write("-" * len(hdr) + "\n")

    prev_alias = None
    for c in candidates:
        # Blank line between machines
        if prev_alias is not None and c["alias"] != prev_alias:
            out.write("\n")
        prev_alias = c["alias"]

        strat_label = f"{c['group']}/{c['strat']}"
        if len(strat_label) > 42:
            strat_label = strat_label[:42]

        days_str = f"{c['positive_days']}/{c['window']}"
        pnl_str = f"{c['total_pnl']:+,.2f}"
        pf = c.get("profit_factor", float("inf"))
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        mult_str = str(c["current_mult"]) if c["current_mult"] is not None else "?"

        inc = c["last_increase"]
        if inc:
            inc_str = f"{inc[0]} ({inc[1]}->{inc[2]})"
        else:
            inc_str = "none"

        cap_cells = _capacity_cells(c, capacity)
        cap_str = (f" {cap_cells[0]:>8} {cap_cells[1]:>9} {cap_cells[2]:>6} "
                   if cap_cells is not None else "  ")

        out.write(f"{c['alias']:<6} {strat_label:<42} {c['sym']:<14} {days_str:<6} {pnl_str:>10} "
                  f"{pf_str:>6} {mult_str:>7}{cap_str}{inc_str}\n")

    out.write("\n")


# ── Turn-Off Candidates ──────────────────────────────────────────────────────

def evaluate_turnoff_unified(data, strat_configs, alert_rules, turnoff_rule):
    """Unified disable-relevant flagging.

    Combines pnl-trend signals (LOSING_STREAK, few_positive) with low-activity
    signals (low_fill, low_trades). Returns (strat_rows, sym_rows) where each
    row carries every reason that fired.

    Symbol-level rows are emitted only when at least one reason fires AND the
    symbol is present in the latest acct file for its strat AND the symbol is
    enabled in the latest strat config.

    Strat-level rows fire on pnl-trend signals (LOSING_STREAK / few_positive).
    Activity-only signals don't fire at strat level — that lens lives in
    rollup_strat_level.
    """
    if data.empty:
        return [], []

    window = turnoff_rule["window"]
    max_fr = turnoff_rule.get("max_fill_rate", 0.0)
    max_tt = turnoff_rule.get("max_avg_times_traded", 0.0)
    has_fr_inputs = "shs_traded" in data.columns and "shs_sent" in data.columns
    has_tt = "times_traded" in data.columns

    rule_consec = next((r for r in alert_rules or [] if r["type"] == "consecutive_losing"), None)
    rule_minpos = next((r for r in alert_rules or [] if r["type"] == "min_positive_days"), None)
    rule_negnorm = next((r for r in alert_rules or [] if r["type"] == "neg_norm_pnl"), None)
    rule_winpct = next((r for r in alert_rules or [] if r["type"] == "low_winpct_after_min_days"), None)
    rule_lowavg = next((r for r in alert_rules or [] if r["type"] == "low_avg_pnl"), None)
    has_norm = "norm_pnl" in data.columns

    # Previous trading day per the data itself — handles weekends/holidays
    # without a calendar. Strats whose latest acct file is on or after this
    # cutoff are still considered "active" for turn-off scoring.
    unique_dates = sorted(data["date"].astype(str).unique())
    cutoff_date = unique_dates[-2] if len(unique_dates) >= 2 else unique_dates[-1]
    latest_date_by_strat = (
        data.groupby(["alias", "group", "strat"])["date"]
        .agg(lambda x: max(x.astype(str)))
        .to_dict()
    )

    def _check_neg_norm(norm_series):
        if not (rule_negnorm and has_norm):
            return False
        win = rule_negnorm["window"]
        th = rule_negnorm["threshold"]
        recent = norm_series[-win:]
        if len(recent) < win:
            return False
        return (sum(recent) / len(recent)) < th

    def _check_low_winpct(pnl_series):
        if not rule_winpct:
            return False
        if len(pnl_series) < rule_winpct["min_days"]:
            return False
        pos = sum(1 for v in pnl_series if v > 0)
        return (pos / len(pnl_series)) < rule_winpct["max_winpct"]

    def _check_low_avg_pnl(norm_series):
        if not (rule_lowavg and has_norm):
            return False
        if len(norm_series) < rule_lowavg["min_days"]:
            return False
        return (sum(norm_series) / len(norm_series)) < rule_lowavg["threshold"]

    # ── Strat-level: pnl-trend reasons only
    strat_rows = []
    for (alias, group, strat), grp_df in data.groupby(["alias", "group", "strat"]):
        agg = {"net_pnl": ("net_pnl", "sum")}
        if has_norm:
            agg["norm_pnl"] = ("norm_pnl", "sum")
        daily = grp_df.groupby("date").agg(**agg).sort_index()
        dates = daily.index.astype(str).tolist()
        if not dates or dates[-1] < cutoff_date:
            continue  # strat went silent
        pnl_series = daily["net_pnl"].values.tolist()
        norm_series = (daily["norm_pnl"].values.tolist() if has_norm
                       else [None] * len(pnl_series))
        reasons = []
        if rule_consec and _check_consecutive_losing(pnl_series, rule_consec["threshold"]):
            reasons.append("LOSING_STREAK")
        if rule_minpos and _check_min_positive_days(pnl_series, rule_minpos["window"], rule_minpos["threshold"]):
            reasons.append("few_positive")
        if _check_neg_norm(norm_series):
            reasons.append("neg_norm_pnl")
        if _check_low_winpct(pnl_series):
            reasons.append("low_winpct")
        if _check_low_avg_pnl(norm_series):
            reasons.append("low_avg_pnl")
        if not reasons:
            continue
        recent = pnl_series[-window:] if len(pnl_series) >= window else pnl_series
        total = sum(recent)
        avg_per_day = total / len(recent) if recent else 0.0
        strat_rows.append({
            "alias": alias, "group": group, "strat": strat,
            "total_pnl": total,
            "avg_per_day": avg_per_day,
            "window": window,
            "reasons": reasons,
        })

    # ── Symbol-level: pnl + activity
    sym_rows = []
    for (alias, group, strat, sym), grp_df in data.groupby(["alias", "group", "strat", "sym"]):
        agg_kwargs = {"net_pnl": ("net_pnl", "sum")}
        if has_norm:
            agg_kwargs["norm_pnl"] = ("norm_pnl", "sum")
        if has_fr_inputs:
            agg_kwargs["shs_traded"] = ("shs_traded", "sum")
            agg_kwargs["shs_sent"] = ("shs_sent", "sum")
        if has_tt:
            agg_kwargs["times_traded"] = ("times_traded", "sum")
        daily = grp_df.groupby("date").agg(**agg_kwargs).sort_index()
        dates = daily.index.astype(str).tolist()
        strat_latest_date = latest_date_by_strat.get((alias, group, strat))
        if not dates or not strat_latest_date or strat_latest_date < cutoff_date:
            continue  # stale strat
        if dates[-1] != strat_latest_date:
            continue  # symbol missing from the latest acct file for this strat

        pnl_series = daily["net_pnl"].values.tolist()
        norm_series = (daily["norm_pnl"].values.tolist() if has_norm
                       else [None] * len(pnl_series))
        reasons = []
        if rule_consec and _check_consecutive_losing(pnl_series, rule_consec["threshold"]):
            reasons.append("LOSING_STREAK")
        if rule_minpos and _check_min_positive_days(pnl_series, rule_minpos["window"], rule_minpos["threshold"]):
            reasons.append("few_positive")
        if _check_neg_norm(norm_series):
            reasons.append("neg_norm_pnl")
        if _check_low_winpct(pnl_series):
            reasons.append("low_winpct")
        if _check_low_avg_pnl(norm_series):
            reasons.append("low_avg_pnl")

        recent = daily.tail(window)
        avg_fr = None
        avg_tt = None
        if len(recent) >= window:
            if has_fr_inputs:
                ts = float(recent["shs_sent"].sum())
                tt = float(recent["shs_traded"].sum())
                avg_fr = (tt / ts) if ts > 0 else 0.0
                if avg_fr < max_fr:
                    reasons.append("low_fill")
            if has_tt:
                avg_tt = float(recent["times_traded"].mean())
                if avg_tt < max_tt:
                    reasons.append("low_trades")

        if not reasons:
            continue

        trader = strat_configs.get((alias, group, strat), {}).get("traders", {}).get(sym)
        if not trader or not trader.get("enabled", False):
            continue
        current_mult = trader.get("size_mult", 1)

        total = float(recent["net_pnl"].sum()) if len(recent) else float(daily["net_pnl"].sum())
        avg_per_day = total / max(len(recent), 1)
        recent_pnl = recent["net_pnl"] if len(recent) else daily["net_pnl"]
        win_days = int((recent_pnl > 0).sum())
        total_days = int(len(recent_pnl))

        sym_rows.append({
            "alias": alias, "group": group, "strat": strat, "sym": sym,
            "current_mult": current_mult,
            "total_pnl": total,
            "avg_per_day": avg_per_day,
            "win_days": win_days,
            "total_days": total_days,
            "avg_fill_rate": avg_fr,
            "avg_times_traded": avg_tt,
            "window": window,
            "reasons": reasons,
        })

    strat_rows.sort(key=lambda r: (r["alias"], f"{r['group']}/{r['strat']}"))
    sym_rows.sort(key=lambda r: (r["alias"], f"{r['group']}/{r['strat']}", r["total_pnl"]))
    return strat_rows, sym_rows


def format_turnoff_unified(strat_rows, sym_rows, out, title="TURN-OFF CANDIDATES",
                           description=None, capacity=None):
    """Write merged disable-candidate section: optional strat-level subblock
    followed by symbol-level rows. Reasons render as comma-separated tags;
    LOSING_STREAK is uppercase to mark severity.
    """
    if not strat_rows and not sym_rows:
        return

    out.write("=" * 175 + "\n")
    out.write(title + "\n")
    out.write("=" * 175 + "\n")
    out.write((description or
        "Things flagged for disable. Multiple reasons = stronger signal. "
        "Reasons: LOSING_STREAK (3+ losing days), few_positive (<=2 positive in 6), "
        "neg_norm_pnl (avg norm_pnl/day below threshold), "
        "low_winpct (win% below threshold after enough days), "
        "low_avg_pnl (avg norm_pnl/day below threshold over all available days), "
        "low_fill, low_trades.") + "\n\n")

    if strat_rows:
        out.write("-- Strat-level --\n")
        hdr_s = (f"{'alias':<6} {'strat':<42} "
                 f"{'pnl':>10} {'avg/day':>10}  {'reasons'}")
        out.write(hdr_s + "\n")
        out.write("-" * len(hdr_s) + "\n")
        prev_alias = None
        for r in strat_rows:
            if prev_alias is not None and r["alias"] != prev_alias:
                out.write("\n")
            prev_alias = r["alias"]
            strat_label = f"{r['group']}/{r['strat']}"[:42]
            out.write(f"{r['alias']:<6} {strat_label:<42} "
                      f"{r['total_pnl']:>+10,.2f} {r['avg_per_day']:>+10,.2f}  "
                      f"{', '.join(r['reasons'])}\n")
        out.write("\n")

    if sym_rows:
        if strat_rows:
            out.write("-- Symbol-level --\n")
        cap_hdr = (f" {'mkt_shr':>8} {'ord/ins':>9} {'fills':>6} "
                   if capacity is not None else "  ")
        hdr = (f"{'alias':<6} {'strat':<42} {'symbol':<14} "
               f"{'sz_mult':>7} {'pnl':>10} {'avg/day':>10} {'win/days':>8} "
               f"{'avg_fr':>10} {'avg_nt':>7}{cap_hdr}{'reasons'}")
        out.write(hdr + "\n")
        out.write("-" * len(hdr) + "\n")
        prev_alias = None
        for r in sym_rows:
            if prev_alias is not None and r["alias"] != prev_alias:
                out.write("\n")
            prev_alias = r["alias"]
            strat_label = f"{r['group']}/{r['strat']}"[:42]
            mult_str = str(r["current_mult"]) if r["current_mult"] is not None else "?"
            fr_str = f"{r['avg_fill_rate']*100:.4f}%" if r['avg_fill_rate'] is not None else "—"
            tt_str = f"{r['avg_times_traded']:.1f}" if r['avg_times_traded'] is not None else "—"
            wd_str = f"{r['win_days']}/{r['total_days']}"
            cap_cells = _capacity_cells(r, capacity)
            cap_str = (f" {cap_cells[0]:>8} {cap_cells[1]:>9} {cap_cells[2]:>6} "
                       if cap_cells is not None else "  ")
            reason_str = ", ".join(r["reasons"])
            out.write(f"{r['alias']:<6} {strat_label:<42} {r['sym']:<14} "
                      f"{mult_str:>7} {r['total_pnl']:>+10,.2f} {r['avg_per_day']:>+10,.2f} "
                      f"{wd_str:>8} {fr_str:>10} {tt_str:>7}{cap_str}{reason_str}\n")
        out.write("\n")


def rollup_strat_level(candidates, local_base, rule):
    """Promote per-symbol flags to a strat-level entry when most of a strat's
    enabled symbols are flagged — likely a data/feed issue, not 8 independent
    strategy issues.

    Returns (strat_rollups, remaining_per_symbol).
    """
    min_frac = rule.get("strat_rollup_min_fraction", 0.5)
    min_count = rule.get("strat_rollup_min_count", 3)

    by_strat = defaultdict(list)
    for c in candidates:
        by_strat[(c["alias"], c["group"], c["strat"])].append(c)

    if not by_strat:
        return [], []

    strat_keys = set(by_strat.keys())
    active = get_active_symbols(local_base, strat_keys)
    enabled_by_strat = defaultdict(set)
    for (a, g, s, sym) in active:
        enabled_by_strat[(a, g, s)].add(sym)

    rollups = []
    remaining = []
    for key, flagged in by_strat.items():
        n_flagged = len(flagged)
        n_enabled = len(enabled_by_strat.get(key, set()))
        promote = (
            n_enabled >= min_count
            and n_flagged >= min_count
            and n_flagged / n_enabled >= min_frac
        )
        if promote:
            rollups.append({
                "alias": key[0],
                "group": key[1],
                "strat": key[2],
                "n_flagged": n_flagged,
                "n_enabled": n_enabled,
                "total_pnl": sum(c["total_pnl"] for c in flagged),
                "window": flagged[0]["window"],
                "syms": [c["sym"] for c in flagged],
            })
        else:
            remaining.extend(flagged)

    rollups.sort(key=lambda r: (r["alias"], f"{r['group']}/{r['strat']}"))
    return rollups, remaining


def format_strat_rollups(rollups, out):
    """Write strat-level investigate section to output buffer."""
    if not rollups:
        return
    window = rollups[0]["window"]
    pnl_label = f"pnl({window}d)"
    out.write("=" * 126 + "\n")
    out.write("STRAT-LEVEL INVESTIGATE\n")
    out.write("=" * 126 + "\n")
    out.write("Strats where most enabled symbols flagged for low activity — "
              "likely data/feed issue, not per-symbol decisions.\n\n")

    hdr = (f"{'alias':<6} {'strat':<42} {'flagged':>9} {pnl_label:>12}  {'symbols'}")
    out.write(hdr + "\n")
    out.write("-" * 126 + "\n")

    prev_alias = None
    for r in rollups:
        if prev_alias is not None and r["alias"] != prev_alias:
            out.write("\n")
        prev_alias = r["alias"]

        strat_label = f"{r['group']}/{r['strat']}"
        if len(strat_label) > 42:
            strat_label = strat_label[:42]

        flagged_str = f"{r['n_flagged']}/{r['n_enabled']}"
        pnl_str = f"{r['total_pnl']:+,.2f}"
        syms_str = ",".join(r["syms"])

        out.write(f"{r['alias']:<6} {strat_label:<42} {flagged_str:>9} {pnl_str:>12}  {syms_str}\n")

    out.write("\n")


# ── Capacity ─────────────────────────────────────────────────────────────────

def _hms_from_t(t_str):
    """Parse 'HH:MM:SS America/New_York' → 'HH:MM:SS' (or '' if missing)."""
    if not t_str:
        return ""
    return str(t_str).split()[0]


def compute_capacity(local_base, candidate_keys, strat_configs, window):
    """Compute notional-space activity stats per (alias, group, strat, sym).

    For each strat, walks the last `window` trades_*.csv and orders_*.csv
    files, filters rows to the symbols in candidate_keys and to the strat's
    [start_t, end_t] window (handles wrap-around for allday strats). Per
    (strat, sym):
      our_total_notl  = sum(|price * size|) over fills, for mkt_share denom
      fills           = fill row count
      our_ord_notl    = mean(|price * size|) over NewOrd rows — order size
                        per logical clip, not per fragmented fill

    Then for each date in the window calls symstats_cache.lookup to pull
    market totals over the same [start_t, end_t]:
      mkt_share  = our_total_notl / mkt_total_notl
      ord_vs_ins = our_ord_notl   / mkt_med_inside_notl

    Returns dict keyed by (alias, group, strat, sym).
    """
    import glob as globmod
    from symstats_cache import lookup as _ss_lookup, DEFAULT_BOOK as _SS_BOOK

    by_strat = defaultdict(set)
    for a, g, s, sym in candidate_keys:
        by_strat[(a, g, s)].add(sym)

    results = {}
    for (alias, group, strat), needed_syms in by_strat.items():
        settings = strat_configs.get((alias, group, strat), {}).get("settings", {})
        start_hms = _hms_from_t(settings.get("start_t", ""))
        end_hms = _hms_from_t(settings.get("end_t", ""))
        wraps = bool(start_hms and end_hms and end_hms < start_hms)

        def _in_window(hms):
            if not (start_hms and end_hms):
                return True
            if wraps:
                return hms >= start_hms or hms <= end_hms
            return start_hms <= hms <= end_hms

        strat_dir = os.path.join(local_base, alias, group, strat)
        trade_files = sorted(globmod.glob(os.path.join(strat_dir, "trades_*.csv")))
        recent = trade_files[-window:]

        # Extract dates from filenames (trades_YYYYMMDD.csv) for market lookups.
        recent_dates = []
        for tf in recent:
            base = os.path.basename(tf)
            if base.startswith("trades_") and base.endswith(".csv"):
                ds = base[len("trades_"):-len(".csv")]
                if len(ds) == 8 and ds.isdigit():
                    recent_dates.append(ds)

        # Fills pass: per-date totals and counts for mkt_share / fills column.
        per_date_notl = {sym: {} for sym in needed_syms}
        per_date_fills = {sym: {} for sym in needed_syms}
        for tf in recent:
            base = os.path.basename(tf)
            if not (base.startswith("trades_") and base.endswith(".csv")):
                continue
            ds = base[len("trades_"):-len(".csv")]
            if not (len(ds) == 8 and ds.isdigit()):
                continue
            try:
                df = pd.read_csv(tf, header=None, usecols=[0, 3, 6, 7])
            except Exception:
                continue
            if df.empty:
                continue
            df.columns = ["t", "sym", "price", "size"]
            df = df[df["sym"].isin(needed_syms)]
            if df.empty:
                continue
            # "YYYYMMDD HH:MM:SS.xxx EDT" → "HH:MM:SS"
            df = df.assign(hms=df["t"].str.split(" ").str[1].str[:8])
            if start_hms and end_hms:
                if wraps:
                    df = df[(df["hms"] >= start_hms) | (df["hms"] <= end_hms)]
                else:
                    df = df[(df["hms"] >= start_hms) & (df["hms"] <= end_hms)]
            if df.empty:
                continue
            notl = (df["price"] * df["size"]).abs()
            for sym, idx in df.groupby("sym").groups.items():
                per_date_notl[sym][ds] = per_date_notl[sym].get(ds, 0.0) + float(notl.loc[idx].sum())
                per_date_fills[sym][ds] = per_date_fills[sym].get(ds, 0) + len(idx)

        # Orders pass: per-symbol mean(|px*sz|) over NewOrd rows. One row =
        # one logical order, so the average is unaffected by fill fragmentation.
        # NewOrd columns: ts, epoch_ms, NewOrd, oid, sym, exch, side, px, sz, ...
        # Order size is governed by size_mult and is effectively constant for a
        # given strat-symbol, so we cap at SAMPLE_CAP samples per sym and bail
        # out — relwide strats can otherwise emit tens of thousands of NewOrds
        # per day and re-parsing them all in Python is wasted work.
        SAMPLE_CAP = 100
        ord_notl_total = {sym: 0.0 for sym in needed_syms}
        ord_count_total = {sym: 0 for sym in needed_syms}
        pending = set(needed_syms)
        # Newest-first so SAMPLE_CAP fires on the most recent file and the
        # outer `if not pending: break` skips older files entirely.
        for ds in reversed(recent_dates):
            if not pending:
                break
            of = os.path.join(strat_dir, f"orders_{ds}.csv")
            if not os.path.exists(of):
                continue
            try:
                fp = open(of, "r", errors="replace")
            except OSError:
                continue
            with fp:
                for line in fp:
                    parts = line.rstrip("\n").split(",")
                    if len(parts) < 9 or parts[2] != "NewOrd":
                        continue
                    sym = parts[4]
                    if sym not in pending:
                        continue
                    ts = parts[0]
                    sp = ts.split(" ")
                    if len(sp) < 2:
                        continue
                    hms = sp[1][:8]
                    if not _in_window(hms):
                        continue
                    try:
                        px = float(parts[7])
                        sz = float(parts[8])
                    except ValueError:
                        continue
                    ord_notl_total[sym] += abs(px * sz)
                    ord_count_total[sym] += 1
                    if ord_count_total[sym] >= SAMPLE_CAP:
                        pending.discard(sym)
                        if not pending:
                            break

        for sym in needed_syms:
            # Overall stats over the full window (all dates we traded)
            our_total = sum(per_date_notl[sym].values())
            our_fills = sum(per_date_fills[sym].values())
            our_ord_avg = (ord_notl_total[sym] / ord_count_total[sym]
                           if ord_count_total[sym] > 0 else 0.0)

            # Market-side aggregation, restricted to dates where BOTH a cached
            # market record exists AND we traded — keeps the mkt_share ratio
            # comparing the same time period on both sides.
            mkt_total = 0.0
            mkt_nt = 0
            wsum_inside = 0.0
            matched_dates = 0
            our_matched_notl = 0.0
            if start_hms and end_hms:
                for d in recent_dates:
                    if d not in per_date_notl[sym]:
                        continue
                    mk = _ss_lookup(sym, d, start_hms, end_hms, book=_SS_BOOK)
                    if mk is None:
                        continue
                    nt = mk.get("num_trades", 0) or 0
                    mkt_total += mk.get("vol_notl", 0.0) or 0.0
                    mkt_nt += nt
                    if nt > 0:
                        wsum_inside += (mk.get("med_inside_notl") or 0.0) * nt
                    matched_dates += 1
                    our_matched_notl += per_date_notl[sym][d]

            mkt_med_ins = wsum_inside / mkt_nt if mkt_nt > 0 else 0.0
            mkt_share = (our_matched_notl / mkt_total) if mkt_total > 0 else None
            ord_vs_ins = (our_ord_avg / mkt_med_ins) if mkt_med_ins > 0 else None
            results[(alias, group, strat, sym)] = {
                "our_ord_notl": our_ord_avg,
                "our_total_notl": our_total,
                "fills": our_fills,
                "mkt_total_notl": mkt_total,
                "mkt_med_ins_notl": mkt_med_ins,
                "mkt_share": mkt_share,
                "ord_vs_ins": ord_vs_ins,
                "mkt_n_dates": matched_dates,
            }
    return results


def format_capacity_warnings(capacity, out, mild_thresh=1.0, strong_thresh=2.0):
    """Emit a CAPACITY WARNINGS block listing rows whose ord_vs_ins is high
    enough that taking liquidity sweeps the inside.
    """
    if not capacity:
        return
    flagged = []
    for (alias, group, strat, sym), cap in capacity.items():
        r = cap.get("ord_vs_ins")
        if r is None or r < mild_thresh:
            continue
        flagged.append((alias, group, strat, sym, cap))
    if not flagged:
        return

    flagged.sort(key=lambda x: (-(x[4].get("ord_vs_ins") or 0), x[0], x[1], x[2], x[3]))

    out.write("=" * 110 + "\n")
    out.write("CAPACITY WARNINGS — clip too big for the book\n")
    out.write("=" * 110 + "\n")
    out.write(f"Symbols where our typical clip eats the median inside size "
              f"(! ≥ {mild_thresh:.1f}x, !! ≥ {strong_thresh:.1f}x).\n\n")

    hdr = (f"{'alias':<6} {'strat':<42} {'symbol':<14} {'our_ord':>10} "
           f"{'med_ins':>12} {'ord/ins':>9}")
    out.write(hdr + "\n")
    out.write("-" * len(hdr) + "\n")

    prev_alias = None
    for alias, group, strat, sym, cap in flagged:
        if prev_alias is not None and alias != prev_alias:
            out.write("\n")
        prev_alias = alias
        strat_label = f"{group}/{strat}"[:42]
        ord_ins = cap.get("ord_vs_ins")
        marker = "!!" if ord_ins >= strong_thresh else "!"
        out.write(f"{alias:<6} {strat_label:<42} {sym:<14} "
                  f"{cap.get('our_ord_notl', 0):>10,.0f} "
                  f"{cap.get('mkt_med_ins_notl', 0):>12,.0f} "
                  f"{ord_ins:>7.2f}x{marker}\n")
    out.write("\n")


# ── Config Change Tracking ────────────────────────────────────────────────────

def _diff_dicts(old, new, prefix=""):
    """Recursively diff two dicts. Returns list of (field, old_val, new_val)."""
    changes = []
    all_keys = set(list(old.keys()) + list(new.keys()))
    for key in sorted(all_keys):
        full_key = f"{prefix}.{key}" if prefix else key
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val == new_val:
            continue
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            changes.extend(_diff_dicts(old_val, new_val, full_key))
        else:
            changes.append((full_key, old_val, new_val))
    return changes


def _diff_pktraders(old_traders, new_traders):
    """Diff pktraders arrays, matching by traded_symbol. Returns list of (sym, field, old, new)."""
    old_by_sym = {t.get("traded_symbol", ""): t for t in old_traders} if old_traders else {}
    new_by_sym = {t.get("traded_symbol", ""): t for t in new_traders} if new_traders else {}
    changes = []
    all_syms = set(list(old_by_sym.keys()) + list(new_by_sym.keys()))
    for sym in sorted(all_syms):
        old_t = old_by_sym.get(sym, {})
        new_t = new_by_sym.get(sym, {})
        if not old_t and new_t:
            changes.append((sym, "(added)", None, "new symbol"))
            continue
        if old_t and not new_t:
            changes.append((sym, "(removed)", "existed", None))
            continue
        for field, old_v, new_v in _diff_dicts(old_t, new_t):
            changes.append((sym, field, old_v, new_v))
    return changes


def load_config_changes(local_base, aliases=None, groups=None, strats=None,
                        date_from=None, date_to=None):
    """Load and diff consecutive pk_*.json.YYYYMMDD config backups.

    Respects optional alias/group/strat filters plus date window.

    Returns list of dicts: {date, alias, group, strat, sym, field, old, new}.
    """
    base = Path(local_base)
    if not base.exists():
        return []

    changes = []

    for alias_dir in sorted(base.iterdir()):
        if not alias_dir.is_dir():
            continue
        alias = alias_dir.name
        if aliases and alias not in aliases:
            continue
        for group_dir in sorted(alias_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            if groups and group not in groups:
                continue
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name
                if strats and strat not in strats:
                    continue

                # Find all pk_*.json.YYYYMMDD files, group by config name
                config_files = defaultdict(list)
                for f in strat_dir.glob("*.json.*"):
                    name = f.name
                    # Extract date suffix — last 8 chars should be YYYYMMDD
                    date_suffix = name.rsplit(".", 1)[-1]
                    if len(date_suffix) != 8 or not date_suffix.isdigit():
                        continue
                    # config base name is everything before the date suffix
                    config_name = name.rsplit(".", 1)[0]
                    config_files[config_name].append((date_suffix, f))

                for config_name, dated_files in config_files.items():
                    dated_files.sort(key=lambda x: x[0])
                    for i in range(len(dated_files) - 1):
                        date_a, path_a = dated_files[i]
                        date_b, path_b = dated_files[i + 1]
                        try:
                            cfg_a = _load_pk_config(path_a)
                            cfg_b = _load_pk_config(path_b)
                        except (json.JSONDecodeError, OSError) as e:
                            print(f"  [warn] config diff error {path_a} -> {path_b}: {e}", file=sys.stderr)
                            continue

                        if date_from and date_b < date_from:
                            continue
                        if date_to and date_b > date_to:
                            continue

                        # Diff pktraders specially
                        old_pt = cfg_a.get("pktraders", [])
                        new_pt = cfg_b.get("pktraders", [])
                        for sym, field, old_v, new_v in _diff_pktraders(old_pt, new_pt):
                            changes.append({
                                "date": date_b,
                                "alias": alias,
                                "group": group,
                                "strat": strat,
                                "sym": sym,
                                "field": field,
                                "old": old_v,
                                "new": new_v,
                            })

                        # Diff top-level keys (excluding pktraders)
                        cfg_a_top = {k: v for k, v in cfg_a.items() if k != "pktraders"}
                        cfg_b_top = {k: v for k, v in cfg_b.items() if k != "pktraders"}
                        for field, old_v, new_v in _diff_dicts(cfg_a_top, cfg_b_top):
                            changes.append({
                                "date": date_b,
                                "alias": alias,
                                "group": group,
                                "strat": strat,
                                "sym": "",
                                "field": field,
                                "old": old_v,
                                "new": new_v,
                            })

    changes.sort(key=lambda c: (c["date"], c["alias"], c["group"], c["strat"], c["sym"]))
    return changes


def format_config_changes(changes, out):
    """Write config changes section to output buffer."""
    if not changes:
        return
    out.write("=" * 90 + "\n")
    out.write("CONFIG CHANGES\n")
    out.write("=" * 90 + "\n")

    fmt = "{:<10} {:<12} {:<30} {:<15} {:<20} {:>12} {:>12}"
    out.write(fmt.format("date", "alias", "group/strat", "symbol", "field", "old", "new") + "\n")
    out.write("-" * 111 + "\n")
    for c in changes:
        old_s = str(c["old"]) if c["old"] is not None else ""
        new_s = str(c["new"]) if c["new"] is not None else ""
        out.write(fmt.format(
            c["date"],
            c["alias"],
            f"{c['group']}/{c['strat']}"[:30],
            str(c["sym"])[:15],
            str(c["field"])[:20],
            old_s[:12],
            new_s[:12],
        ) + "\n")
    out.write("\n")


# ── Log event summary for report ──────────────────────────────────────────────

def _write_log_event_section(out, local_base, data, dates_present):
    """Add log event summary section to report output.

    Parses INFO logs for the same aliases/dates as the acct data and shows
    per-strat-per-date counts of key events alongside feed latency p99.
    """
    try:
        from sim_eval_logs import parse_log_events
    except ImportError:
        return  # sim_eval_logs not available, skip silently

    aliases_in_data = sorted(data["alias"].unique())
    date_strs = sorted(dates_present)

    # Only parse the event types we want to summarize
    events = parse_log_events(
        local_base, aliases_in_data, date_strs,
        event_types={"usermsg", "inherit", "cancel_pause", "reject", "feed_latency"},
    )
    if not events:
        return

    # Aggregate by (alias, group, strat, date)
    from collections import defaultdict
    agg = defaultdict(lambda: {
        "usermsg": 0, "inherit": 0, "cancel_pause": 0,
        "cancel_reject": 0, "oe_ack_not_found": 0, "oe_erase_nonexistent": 0,
        "latency_vals": [],
    })
    for e in events:
        key = (e["alias"], e["group"], e["strat"], e["date"])
        et = e["event_type"]
        if et == "usermsg":
            agg[key]["usermsg"] += 1
        elif et == "inherit":
            agg[key]["inherit"] += 1
        elif et == "cancel_pause":
            agg[key]["cancel_pause"] += 1
        elif et == "cancel_reject":
            agg[key]["cancel_reject"] += 1
        elif et == "oe_ack_not_found":
            agg[key]["oe_ack_not_found"] += 1
        elif et == "oe_erase_nonexistent":
            agg[key]["oe_erase_nonexistent"] += 1
        elif et == "feed_latency":
            agg[key]["latency_vals"].append(e["detail"]["ms_behind"])

    if not agg:
        return

    # Only show strats that appear in the acct data (respect --strat/--group filters)
    strats_in_data = set()
    for _, r in data.iterrows():
        strats_in_data.add((r["alias"], r["group"], r["strat"]))

    out.write("=" * 90 + "\n")
    out.write("LOG EVENTS\n")
    out.write("=" * 90 + "\n")

    # Group by (alias, group, strat), show one block per strat with per-date rows
    strat_keys = sorted(set((k[0], k[1], k[2]) for k in agg) & strats_in_data)

    for alias, group, strat in strat_keys:
        out.write(f"\n  {alias}/{group}/{strat}\n")
        header = (f"    {'date':<10}  {'UMs':>4}  {'inh':>4}  "
                  f"{'cxl_rej':>7}  {'cxl_pause':>9}  "
                  f"{'ack_nf':>6}  {'erase_ne':>8}  "
                  f"{'lat_p50':>7}  {'lat_p99':>7}  {'lat_max':>7}")
        out.write(header + "\n")
        out.write("    " + "-" * (len(header) - 4) + "\n")

        for ds in date_strs:
            key = (alias, group, strat, ds)
            if key not in agg:
                continue
            a = agg[key]
            lv = sorted(a["latency_vals"]) if a["latency_vals"] else []
            n = len(lv)
            if n > 0:
                p50 = lv[int(n * 0.50)]
                p99 = lv[min(int(n * 0.99), n - 1)]
                mx = lv[-1]
                lat_p50 = f"{p50:>5d}ms"
                lat_p99 = f"{p99:>5d}ms"
                lat_max = f"{mx:>5d}ms"
            else:
                lat_p50 = lat_p99 = lat_max = "     --"

            row = (f"    {ds:<10}  {a['usermsg']:>4}  {a['inherit']:>4}  "
                   f"{a['cancel_reject']:>7}  {a['cancel_pause']:>9}  "
                   f"{a['oe_ack_not_found']:>6}  {a['oe_erase_nonexistent']:>8}  "
                   f"{lat_p50:>7}  {lat_p99:>7}  {lat_max:>7}")
            out.write(row + "\n")

    out.write("\n")


def _write_order_analysis_section(out, local_base, data, dates_present):
    """Add order analysis section (RTT percentiles, rejects) to report output."""
    try:
        from order_analysis import analyze_orders, build_report_section
    except ImportError:
        return

    aliases_in_data = sorted(data["alias"].unique())
    date_strs = sorted(dates_present)

    # Build strats_in_data set for filtering
    strats_in_data = set()
    for _, r in data.iterrows():
        strats_in_data.add((r["alias"], r["group"], r["strat"]))

    results = analyze_orders(local_base, aliases_in_data, date_strs)
    if not results:
        return

    build_report_section(out, results, dates_present, strats_in_data)


def run_trends(local_base, aliases, date_strs, strat_filter=None, group_filter=None,
               symbols=None, strat_only=False):
    """Build a report of how order RTT and fill rates evolve over time, per strat
    and per symbol. Returns the report text (the caller prints and/or emails it).

    Two metrics, one row per date:
      - Order RTT (NewOrd→NewOrdAck), in ms, split by order type via the ADD/REM
        marker on each NewOrd line: a_p50/a_p99 = ADD (passive/maker/ALO),
        r_p50/r_p99 = REM (aggressive/taker/IOC). cxl50 = Cancel→CancelAck p50.
      - Fill rate from acct files: fill% recomputed as shs_traded/shs_sent (the
        file's own fill_rate is only 2dp), reach%/ioc% taken straight from the
        acct file's reachable_fill_rate/ioc_fill_rate (2dp).

    Layout: one block per (alias, group, strat) — a strat-level rollup table, then
    (unless strat_only) one sub-table per symbol, since per-symbol behaviour can
    vary wildly. Empty days are skipped. Use --strat/--group/--symbol to scope;
    with no filters this prints every strat/symbol over the window.
    """
    from collections import defaultdict
    from order_analysis import analyze_orders, _percentiles

    # ── RTT source: parse orders files, keyed (alias, group, strat, date) ──
    results = analyze_orders(local_base, aliases, date_strs,
                             strat_filter=strat_filter, group_filter=group_filter)

    # ── Fill source: acct rows over the same window (substring-filtered to match
    #    analyze_orders' substring semantics) ──
    acct = load_acct_data(local_base, aliases=aliases,
                          date_from=min(date_strs), date_to=max(date_strs))
    if not acct.empty:
        if group_filter:
            acct = acct[acct["group"].str.contains(group_filter, na=False)]
        if strat_filter:
            acct = acct[acct["strat"].str.contains(strat_filter, na=False)]
        if symbols:
            acct = acct[acct["sym"].isin(symbols)]
        for c in ["shs_traded", "shs_sent", "reachable_fill_rate", "ioc_fill_rate"]:
            if c in acct.columns:
                acct[c] = pd.to_numeric(acct[c], errors="coerce").fillna(0)

    # acct indexed: (alias, group, strat, date) -> {sym: (traded, sent, reach, ioc)}
    acct_idx = defaultdict(dict)
    if not acct.empty:
        for r in acct.itertuples(index=False):
            acct_idx[(r.alias, r.group, r.strat, str(r.date))][r.sym] = (
                float(getattr(r, "shs_traded", 0) or 0),
                float(getattr(r, "shs_sent", 0) or 0),
                float(getattr(r, "reachable_fill_rate", 0) or 0),
                float(getattr(r, "ioc_fill_rate", 0) or 0),
            )

    # Union of strat keys seen in either source.
    strat_keys = set(k[:3] for k in results)
    strat_keys |= set(acct_idx_key[:3] for acct_idx_key in acct_idx)
    if not strat_keys:
        return "No orders or acct data found for the given filters/window.\n"

    # ── Formatting helpers ──
    rowfmt = ("{:<10} {:>5} {:>6} {:>6} {:>6} {:>6} {:>6} {:>7} {:>7} {:>6}")
    # "rtts" = number of round-trip samples = ADD acks (NewOrd→NewOrdAck) + REM
    # terminals (NewOrd→earliest Exec/Elim/Reject). Both order types now contribute.
    header = rowfmt.format("date", "rtts", "a_p50", "a_p99", "r_p50", "r_p99",
                           "cxl50", "fill%", "reach%", "ioc%")

    def _pp(vals, idx):
        # _percentiles -> (p50, p75, p90, p99, max); idx 0 = p50, 3 = p99.
        p = _percentiles(vals)
        return str(p[idx]) if p else "-"

    def _fillpct(traded, sent):
        return f"{traded / sent * 100:.1f}%" if sent > 0 else "-"

    def _ratepct(v):
        return f"{v * 100:.0f}%" if v else "-"

    def _row(ds, add, rem, cxl, traded, sent, reach, ioc):
        return rowfmt.format(
            ds, len(add) + len(rem),
            _pp(add, 0), _pp(add, 3), _pp(rem, 0), _pp(rem, 3), _pp(cxl, 0),
            _fillpct(traded, sent), _ratepct(reach), _ratepct(ioc))

    def _keep(sym):
        return (not symbols) or (sym in symbols)

    out = io.StringIO()
    out.write("=" * 100 + "\n")
    out.write("TRENDS — order RTT (ALO=ADD / IOC=REM) + fill rate over time\n")
    out.write("=" * 100 + "\n")
    out.write(f"Window: {len(date_strs)} day(s), {min(date_strs)} to {max(date_strs)}. "
              "RTT ms; a_*=ADD (passive/ALO), r_*=REM (aggressive/IOC). "
              "fill%=shs_traded/shs_sent; reach%/ioc% from acct (2dp).\n")

    for (a, g, s) in sorted(strat_keys):
        out.write(f"\n[{a}] {g}/{s}\n")

        # ── Strat-level rollup (all syms combined per day) ──
        out.write("  -- strat rollup --\n")
        out.write("  " + header + "\n")
        out.write("  " + "-" * len(header) + "\n")
        for ds in date_strs:
            pr = results.get((a, g, s, ds))
            add, rem, cxl = [], [], []
            if pr:
                for sym, v in pr["rtt_add_by_sym"].items():
                    if _keep(sym):
                        add += v
                for sym, v in pr["rtt_rem_by_sym"].items():
                    if _keep(sym):
                        rem += v
                for sym, v in pr["cancel_rtt_by_sym"].items():
                    if _keep(sym):
                        cxl += v
            traded = sent = rw = iw = 0.0
            for sym, (t, se, re, iov) in acct_idx.get((a, g, s, ds), {}).items():
                if not _keep(sym):
                    continue
                traded += t
                sent += se
                rw += re * se   # share-weighted reach/ioc (only the ratio is stored)
                iw += iov * se
            reach = rw / sent if sent > 0 else 0
            ioc = iw / sent if sent > 0 else 0
            if not (add or rem or cxl or sent > 0):
                continue
            out.write("  " + _row(ds, add, rem, cxl, traded, sent, reach, ioc) + "\n")

        if strat_only:
            continue

        # ── Per-symbol detail ──
        syms = set()
        for ds in date_strs:
            pr = results.get((a, g, s, ds))
            if pr:
                syms |= set(pr["rtt_add_by_sym"]) | set(pr["rtt_rem_by_sym"]) \
                        | set(pr["cancel_rtt_by_sym"])
            syms |= set(acct_idx.get((a, g, s, ds), {}))
        syms = {x for x in syms if _keep(x)}
        if not syms:
            continue

        out.write("  -- by symbol --\n")
        for sym in sorted(syms):
            rows = []
            for ds in date_strs:
                pr = results.get((a, g, s, ds))
                add = pr["rtt_add_by_sym"].get(sym, []) if pr else []
                rem = pr["rtt_rem_by_sym"].get(sym, []) if pr else []
                cxl = pr["cancel_rtt_by_sym"].get(sym, []) if pr else []
                t, se, re, iov = acct_idx.get((a, g, s, ds), {}).get(sym, (0, 0, 0, 0))
                if not (add or rem or cxl or se > 0):
                    continue
                rows.append((ds, add, rem, cxl, t, se, re, iov))
            if not rows:
                continue
            out.write(f"\n    {sym}\n")
            out.write("    " + header + "\n")
            out.write("    " + "-" * len(header) + "\n")
            for (ds, add, rem, cxl, t, se, re, iov) in rows:
                out.write("    " + _row(ds, add, rem, cxl, t, se, re, iov) + "\n")

    out.write("\n")
    return out.getvalue()


def _write_markout_section(out, local_base, data, dates_present):
    """Add markout analysis section to report output (reads pre-computed files only)."""
    try:
        from markout_analysis import build_report_section
    except ImportError:
        return
    build_report_section(out, local_base, data, dates_present)


def _write_tod_section(out, local_base, data, dates_present):
    """Add time-of-day analysis section to report output (reads pre-computed markout files)."""
    try:
        from markout_analysis import build_tod_report_section
    except ImportError:
        return
    build_tod_report_section(out, local_base, data, dates_present)


def _write_fillstats_section(out, local_base, data, dates_present):
    """Add fill context analysis section to report output (reads pre-computed files only)."""
    try:
        from fillstats_analysis import build_fillstats_report_section
    except ImportError:
        return
    build_fillstats_report_section(out, local_base, data, dates_present)


# ── Report ────────────────────────────────────────────────────────────────────

def compute_flagged_pairs(local_base, aliases=None, groups=None, strats=None, symbols=None):
    """Re-run the turnoff evaluation and return flagged losing (strat, sym) rows.

    Returns a list of dicts: {alias, group, strat, sym, reasons, total_pnl, avg_per_day}.
    Mirrors the logic in build_report's turnoff section but returns the data rather
    than rendering it. Used by the `diagnose` / `diagnose-flagged` flows.
    """
    data = load_acct_data(local_base, aliases=aliases, groups=groups,
                          strats=strats, symbols=symbols)
    if data.empty:
        return []
    num_cols = ["net_pnl", "closed_pnl", "commission", "funding",
                "shs_traded", "shs_sent", "times_traded", "fill_rate"]
    for c in num_cols:
        if c in data.columns:
            data[c] = pd.to_numeric(data[c], errors="coerce").fillna(0)
    data_strat_keys = set(data.groupby(["alias", "group", "strat"]).groups.keys())
    strat_configs = _load_strat_configs(local_base, data_strat_keys)
    size_mults = {}
    for (a, g, s), entry in strat_configs.items():
        for sym, t in entry.get("traders", {}).items():
            try:
                m = float(t.get("size_mult", 1))
            except (TypeError, ValueError):
                m = 1.0
            size_mults[(a, g, s, sym)] = m if m > 0 else 1.0
    data["size_mult"] = [
        size_mults.get((a, g, s, sym), 1.0)
        for a, g, s, sym in zip(data["alias"], data["group"], data["strat"], data["sym"])
    ]
    data["norm_pnl"] = data["net_pnl"] / data["size_mult"]
    turnoff_rule = CONFIG.get("turnoff_rule")
    if not turnoff_rule:
        return []
    alert_rules = CONFIG.get("alert_rules", [])
    _strat_alerts, sym_rows = evaluate_turnoff_unified(
        data, strat_configs, alert_rules, turnoff_rule)
    losing = [r for r in sym_rows if r["total_pnl"] <= 0]
    return losing


def build_report(local_base, days=None, aliases=None, groups=None, strats=None, symbols=None,
                 show_alerts=True, show_config=True, symbol_perf_days=14):
    """Build the full report as a string. Returns (report_text, data) or (None, None) if no data."""
    date_from = None
    if days:
        date_from = (datetime.today() - timedelta(days=days - 1)).strftime("%Y%m%d")

    data = load_acct_data(
        local_base,
        aliases=aliases,
        groups=groups,
        strats=strats,
        symbols=symbols,
        date_from=date_from,
    )

    if data.empty:
        return None, None

    num_cols = ["net_pnl", "closed_pnl", "commission", "funding", "shs_traded", "shs_sent", "times_traded", "fill_rate"]
    for c in num_cols:
        if c in data.columns:
            data[c] = pd.to_numeric(data[c], errors="coerce").fillna(0)

    # Load strat configs once (latest pk_*.json per strat) — the same cache
    # serves norm_pnl computation, the sizeup/turnoff evaluators, and capacity.
    data_strat_keys = set(data.groupby(["alias", "group", "strat"]).groups.keys())
    strat_configs = _load_strat_configs(local_base, data_strat_keys)

    # ── Goodness: net_pnl normalized by size_mult ─────────────────────────
    size_mults = {}
    for (a, g, s), entry in strat_configs.items():
        for sym, t in entry.get("traders", {}).items():
            try:
                m = float(t.get("size_mult", 1))
            except (TypeError, ValueError):
                m = 1.0
            size_mults[(a, g, s, sym)] = m if m > 0 else 1.0
    data["size_mult"] = [
        size_mults.get((a, g, s, sym), 1.0)
        for a, g, s, sym in zip(data["alias"], data["group"], data["strat"], data["sym"])
    ]
    data["norm_pnl"] = data["net_pnl"] / data["size_mult"]

    out = io.StringIO()

    dates_present = sorted(data["date"].unique())
    n_days = len(dates_present)
    latest_date = dates_present[-1]
    cutoff_date = dates_present[-2] if len(dates_present) >= 2 else latest_date
    recent_traded_keys = _recent_traded_symbol_keys(local_base, data, cutoff_date)
    recent_traded_syms_by_strat = defaultdict(set)
    for a, g, s, sym in recent_traded_keys:
        recent_traded_syms_by_strat[(a, g, s)].add(sym)

    out.write(f"Data: {n_days} day(s), {dates_present[0]} to {dates_present[-1]}\n\n")

    # Always defined — populated inside the size-up branch when it runs.
    capacity = {}

    # ── Size-up + Turn-off candidates ─────────────────────────────────────
    if show_alerts and CONFIG.get("sizeup_rule"):
        # Load every cached day so long-window rules (e.g. low_avg_pnl) see
        # full history. Files are local + already concatenated cheaply; the
        # short report window passed in via --days only governs the display
        # tables, not the alert evaluators.
        sizeup_data = data if date_from is None else load_acct_data(
            local_base, aliases=aliases, groups=groups, strats=strats, symbols=symbols,
        )
        if not sizeup_data.empty:
            for c in ["net_pnl", "fill_rate", "times_traded", "shs_traded", "shs_sent"]:
                if c in sizeup_data.columns:
                    sizeup_data[c] = pd.to_numeric(sizeup_data[c], errors="coerce").fillna(0)
            sizeup_strat_keys = set(sizeup_data.groupby(["alias", "group", "strat"]).groups.keys())
            missing = sizeup_strat_keys - set(strat_configs.keys())
            if missing:
                strat_configs.update(_load_strat_configs(local_base, missing))
            # Recompute norm_pnl on sizeup_data — it's loaded fresh whenever the
            # broader window doesn't fit inside the report window (typical case)
            # so it doesn't inherit the column from `data`.
            if "norm_pnl" not in sizeup_data.columns:
                # Refresh size_mults to cover any newly-loaded strats.
                for (a, g, s), entry in strat_configs.items():
                    for sym, t in entry.get("traders", {}).items():
                        try:
                            m = float(t.get("size_mult", 1))
                        except (TypeError, ValueError):
                            m = 1.0
                        size_mults[(a, g, s, sym)] = m if m > 0 else 1.0
                sizeup_data["size_mult"] = [
                    size_mults.get((a, g, s, sym), 1.0)
                    for a, g, s, sym in zip(sizeup_data["alias"], sizeup_data["group"],
                                            sizeup_data["strat"], sizeup_data["sym"])
                ]
                sizeup_data["norm_pnl"] = sizeup_data["net_pnl"] / sizeup_data["size_mult"]
            sizeup = evaluate_sizeup_candidates(sizeup_data, local_base, strat_configs, CONFIG["sizeup_rule"])

            turnoff_rule = CONFIG.get("turnoff_rule")
            alert_rules = CONFIG.get("alert_rules", []) if show_alerts else []
            if turnoff_rule:
                strat_alerts, sym_rows = evaluate_turnoff_unified(
                    sizeup_data, strat_configs, alert_rules, turnoff_rule)
                # Activity-only subset feeds the rollup (data-feed-issue lens).
                activity_only = [r for r in sym_rows
                                 if {"low_fill", "low_trades"} & set(r["reasons"])]
                rollups, _ = (rollup_strat_level(activity_only, local_base, turnoff_rule)
                              if activity_only else ([], []))
                rolled_keys = {(r["alias"], r["group"], r["strat"]) for r in rollups}
                sym_to_show = [r for r in sym_rows
                               if (r["alias"], r["group"], r["strat"]) not in rolled_keys]
                losing = [r for r in sym_to_show if r["total_pnl"] <= 0]
                profitable = [r for r in sym_to_show if r["total_pnl"] > 0]
            else:
                strat_alerts, sym_rows, rollups = [], [], []
                losing, profitable = [], []

            # Capacity for every row that will appear in any table — sizeup /
            # turnoff / review candidates, plus recently traded symbols so
            # SYMBOL PERFORMANCE has full coverage.
            cap_window = CONFIG["sizeup_rule"]["window"]
            cap_keys = set()
            for c in sizeup + losing + profitable:
                cap_keys.add((c["alias"], c["group"], c["strat"], c["sym"]))
            cap_keys.update(recent_traded_keys)
            capacity = (compute_capacity(local_base, cap_keys, strat_configs, cap_window)
                        if cap_keys else {})

            format_sizeup_candidates(sizeup, out, capacity=capacity)

            if turnoff_rule:
                format_turnoff_unified(
                    strat_alerts, losing, out,
                    title="TURN-OFF CANDIDATES",
                    capacity=capacity,
                )
                format_strat_rollups(rollups, out)
                format_turnoff_unified(
                    [], profitable, out,
                    title="REVIEW: ILLIQUID BUT PROFITABLE",
                    description="Low-activity symbols still making money — review config "
                                "(spread, sizing, reach) rather than disable.",
                    capacity=capacity,
                )

            format_capacity_warnings(capacity, out)

    # ── Config changes ────────────────────────────────────────────────────
    if show_config:
        date_to = dates_present[-1] if dates_present else None
        config_changes = load_config_changes(
            local_base,
            aliases=aliases,
            groups=groups,
            strats=strats,
            date_from=date_from,
            date_to=date_to,
        )
        format_config_changes(config_changes, out)

    # ── Per-strat summary ─────────────────────────────────────────────────
    out.write("=" * 90 + "\n")
    out.write("SUMMARY BY STRAT\n")
    out.write("=" * 90 + "\n")

    strat_grp = data.groupby(["alias", "group", "strat"])
    summary = strat_grp.agg(
        net_pnl=("net_pnl", "sum"),
        norm_pnl=("norm_pnl", "sum"),
        closed_pnl=("closed_pnl", "sum"),
        commission=("commission", "sum"),
        funding=("funding", "sum"),
        n_symbols=("sym", "nunique"),
        total_trades=("times_traded", "sum"),
    ).reset_index()
    summary["daily_avg"] = summary["net_pnl"] / n_days
    summary["norm_daily_avg"] = summary["norm_pnl"] / n_days

    summary = summary.sort_values("net_pnl", ascending=False)

    fmt_row = "{:<12} {:<30} {:>12} {:>12} {:>12} {:>12} {:>12} {:>8} {:>8}"
    out.write(fmt_row.format(
        "alias", "group/strat", "daily_avg", "norm_d_avg",
        "net_pnl", "norm_pnl", "commission", "symbols", "trades") + "\n")
    out.write("-" * 116 + "\n")
    for _, r in summary.iterrows():
        out.write(fmt_row.format(
            r["alias"],
            f"{r['group']}/{r['strat']}"[:30],
            f"{r['daily_avg']:,.2f}",
            f"{r['norm_daily_avg']:,.2f}",
            f"{r['net_pnl']:,.2f}",
            f"{r['norm_pnl']:,.2f}",
            f"{r['commission']:,.2f}",
            str(int(r["n_symbols"])),
            str(int(r["total_trades"])),
        ) + "\n")
    out.write("\n")
    total_pnl = summary["net_pnl"].sum()
    total_norm = summary["norm_pnl"].sum()
    total_comm = summary["commission"].sum()
    out.write(f"TOTAL net_pnl: {total_pnl:,.2f}   norm_pnl: {total_norm:,.2f}   "
              f"commission: {total_comm:,.2f}   daily_avg: {total_pnl / n_days:,.2f}   "
              f"norm_d_avg: {total_norm / n_days:,.2f}\n\n")

    # ── Per-strat daily breakdown (norm_pnl per day, plus raw net_pnl total) ──
    daily = data.groupby(["alias", "group", "strat", "date"]).agg(
        net_pnl=("net_pnl", "sum"),
        norm_pnl=("norm_pnl", "sum"),
    ).reset_index()

    out.write("=" * 90 + "\n")
    out.write("DAILY NORM_PNL BY STRAT\n")
    out.write("=" * 90 + "\n")

    norm_pivot = daily.pivot_table(
        index=["alias", "group", "strat"],
        columns="date",
        values="norm_pnl",
        fill_value=0,
    )
    raw_pivot = daily.pivot_table(
        index=["alias", "group", "strat"],
        columns="date",
        values="net_pnl",
        fill_value=0,
    )

    date_cols = list(norm_pivot.columns)
    header = (f"{'strat':<45}" + "".join(f"{d:>12}" for d in date_cols)
              + f"{'norm_total':>12}{'pnl_total':>12}")
    out.write(header + "\n")
    out.write("-" * len(header) + "\n")
    for idx, row in norm_pivot.iterrows():
        alias, group, strat = idx
        label = f"{alias}/{group}/{strat}"[:44]
        vals = "".join(f"{row[d]:>12,.2f}" for d in date_cols)
        norm_total = row.sum()
        raw_total = raw_pivot.loc[idx].sum()
        out.write(f"{label:<45}{vals}{norm_total:>12,.2f}{raw_total:>12,.2f}\n")
    out.write("\n")

    # ── Log events summary ──────────────────────────────────────────────
    _write_log_event_section(out, local_base, data, dates_present)

    # ── Order analysis ────────────────────────────────────────────────
    _write_order_analysis_section(out, local_base, data, dates_present)

    # ── Markout analysis ────────────────────────────────────────────
    _write_markout_section(out, local_base, data, dates_present)

    # ── Time-of-day analysis ─────────────────────────────────────────
    _write_tod_section(out, local_base, data, dates_present)

    # ── Fill context analysis ────────────────────────────────────────
    _write_fillstats_section(out, local_base, data, dates_present)

    # ── Per-symbol performance + capacity by strat ───────────────────────
    # Load a wider window than the report's --days view so aggregations have
    # enough history. Mirrors the sizeup_data full-history reload pattern.
    perf_date_from = (datetime.today() - timedelta(days=symbol_perf_days - 1)).strftime("%Y%m%d")
    if date_from is not None and perf_date_from >= date_from:
        perf_data = data
    else:
        perf_data = load_acct_data(
            local_base, aliases=aliases, groups=groups, strats=strats, symbols=symbols,
            date_from=perf_date_from,
        )
        if not perf_data.empty:
            for c in num_cols:
                if c in perf_data.columns:
                    perf_data[c] = pd.to_numeric(perf_data[c], errors="coerce").fillna(0)
            perf_data["size_mult"] = [
                size_mults.get((a, g, s, sym), 1.0)
                for a, g, s, sym in zip(perf_data["alias"], perf_data["group"],
                                        perf_data["strat"], perf_data["sym"])
            ]
            perf_data["norm_pnl"] = perf_data["net_pnl"] / perf_data["size_mult"]
        else:
            perf_data = data

    perf_dates_present = sorted(perf_data["date"].unique()) if not perf_data.empty else dates_present
    out.write("=" * 90 + "\n")
    out.write("SYMBOL PERFORMANCE\n")
    out.write("=" * 90 + "\n")
    header_dates = f"{cutoff_date} or {latest_date}" if cutoff_date != latest_date else latest_date
    out.write(f"One row per (strat, sym) that traded on {header_dates}. "
              f"Aggregated over {len(perf_dates_present)} day(s) "
              f"({perf_dates_present[0]} to {perf_dates_present[-1]}). "
              "Grouped by alias→strat, alphabetical within. SHOWN TOTAL sums "
              "the displayed rows; NOT SHOWN/TOTAL ALL include other symbols "
              "with acct rows in the aggregation window.\n\n")

    for (alias, group, strat), strat_df in perf_data.groupby(["alias", "group", "strat"]):
        recent_syms = recent_traded_syms_by_strat.get((alias, group, strat), set())
        if not recent_syms:
            continue

        sym_daily = strat_df.groupby(["sym", "date"]).agg(
            net_pnl=("net_pnl", "sum"),
        ).reset_index()
        win_info = sym_daily.groupby("sym").agg(
            total_days=("date", "count"),
            win_days=("net_pnl", lambda x: (x > 0).sum()),
        ).reset_index()

        sym_agg = strat_df.groupby("sym").agg(
            net_pnl=("net_pnl", "sum"),
            norm_pnl=("norm_pnl", "sum"),
            total_trades=("times_traded", "sum"),
        ).reset_index()
        sym_agg = sym_agg.merge(win_info, on="sym", how="left")
        sym_agg = sym_agg[sym_agg["sym"].isin(recent_syms)]
        if sym_agg.empty:
            continue
        sym_agg["win_pct"] = sym_agg["win_days"] / sym_agg["total_days"] * 100
        sym_agg["pnl_per_trade"] = sym_agg["net_pnl"] / sym_agg["total_trades"].clip(lower=1)
        sym_agg = sym_agg.sort_values("sym")

        out.write(f"\n  [{alias}] {group}/{strat}\n")
        hdr = (f"    {'sym':<14} {'sz_mult':>7} {'pnl':>10} {'norm_pnl':>10} "
               f"{'win/days':>8} {'win%':>5} {'trades':>7} {'pnl/trd':>8} "
               f"{'mkt_shr':>8} {'ord/ins':>9} {'fills':>6}")
        out.write(hdr + "\n")
        out.write("    " + "-" * (len(hdr) - 4) + "\n")

        for _, r in sym_agg.iterrows():
            sym = str(r["sym"])
            cap = capacity.get((alias, group, strat, sym), {})
            if cap.get("fills", 0) > 0:
                mkt_shr_s = _fmt_pct(cap.get("mkt_share"))
                ord_ins_s = _fmt_ord_vs_ins(cap.get("ord_vs_ins"))
                fills_s = str(cap["fills"])
            else:
                mkt_shr_s = ord_ins_s = "—"
                fills_s = "0"
            trader = strat_configs.get((alias, group, strat), {}).get("traders", {}).get(sym, {})
            sz_mult_v = trader.get("size_mult")
            sz_s = str(sz_mult_v) if sz_mult_v is not None else "?"
            wd = f"{int(r['win_days'])}/{int(r['total_days'])}"
            out.write(f"    {sym:<14} {sz_s:>7} {r['net_pnl']:>+10,.2f} {r['norm_pnl']:>+10,.2f} "
                      f"{wd:>8} {r['win_pct']:>4.0f}% {int(r['total_trades']):>7} "
                      f"{r['pnl_per_trade']:>+8,.2f} "
                      f"{mkt_shr_s:>8} {ord_ins_s:>9} {fills_s:>6}\n")

        def _write_total_row(label, total_df, sz_s=""):
            total_pnl = total_df["net_pnl"].sum()
            total_norm = total_df["norm_pnl"].sum()
            total_trades = int(total_df["times_traded"].sum())
            total_days = len(total_df["date"].unique())
            total_win = int((total_df.groupby("date")["net_pnl"].sum() > 0).sum())
            total_wd = f"{total_win}/{total_days}"
            total_wp = total_win / total_days * 100 if total_days else 0
            total_ppt = total_pnl / max(total_trades, 1)
            out.write(f"    {label:<14} {sz_s:>7} {total_pnl:>+10,.2f} {total_norm:>+10,.2f} "
                      f"{total_wd:>8} {total_wp:>4.0f}% {total_trades:>7} "
                      f"{total_ppt:>+8,.2f}\n")

        shown_df = strat_df[strat_df["sym"].isin(recent_syms)]
        hidden_df = strat_df[~strat_df["sym"].isin(recent_syms)]
        hidden_syms = int(hidden_df["sym"].nunique()) if not hidden_df.empty else 0
        _write_total_row("SHOWN TOTAL", shown_df)
        if hidden_syms:
            _write_total_row("NOT SHOWN", hidden_df, f"{hidden_syms} syms")
            _write_total_row("TOTAL ALL", strat_df)

    out.write("\n")

    return out.getvalue(), data


def report(local_base, days=None, aliases=None, groups=None, strats=None, symbols=None,
           show_alerts=True, show_config=True, symbol_perf_days=14):
    """Print performance reports from local data."""
    text, _ = build_report(local_base, days=days, aliases=aliases, groups=groups,
                           strats=strats, symbols=symbols,
                           show_alerts=show_alerts, show_config=show_config,
                           symbol_perf_days=symbol_perf_days)
    if text is None:
        print("No data found. Run 'fetch' first or adjust filters.")
        return
    print(text, end="")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_list(s):
    """Parse a comma-separated string into a list."""
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _run_post_report_diagnose(args, local_base, aliases_list):
    """Diagnose hook for report/fetch-report/email-report when --diagnose is set."""
    from postmortems.pm_diagnose import diagnose_flagged_rows
    flagged = compute_flagged_pairs(
        local_base,
        aliases=aliases_list,
        groups=parse_list(args.group) if getattr(args, "group", None) else None,
        strats=parse_list(args.strat) if getattr(args, "strat", None) else None,
        symbols=parse_list(args.symbol) if getattr(args, "symbol", None) else None,
    )
    print()
    print("=" * 90)
    print("DIAGNOSE: flagged (strat, sym) pairs — trailing HL micro-structure")
    print("=" * 90)
    diagnose_flagged_rows(
        local_base, flagged,
        recent_n=args.diagnose_days_recent,
        prior_n=args.diagnose_days_prior,
        with_topbook=args.diagnose_with_topbook,
        alert_only=args.diagnose_alert_only,
        topbook_dataset=args.diagnose_topbook_dataset,
        min_shifts=args.diagnose_min_shifts,
    )


def _add_diagnose_flags(parser):
    """Diagnose options shared by 'diagnose' and the --diagnose hook on report."""
    parser.add_argument("--diagnose-days-recent", type=int, default=5,
                        help="Trailing days as 'recent' window for diagnose (default 5)")
    parser.add_argument("--diagnose-days-prior", type=int, default=10,
                        help="Days before recent for diagnose 'prior' window (default 10)")
    parser.add_argument("--diagnose-with-topbook", action="store_true",
                        help="Include Databento topbook stats in diagnose (costs credits)")
    parser.add_argument("--diagnose-topbook-dataset", default="XNAS.BASIC",
                        choices=["XNAS.BASIC", "EQUS.MINI"])
    parser.add_argument("--diagnose-alert-only", action="store_true",
                        help="Suppress diagnose detail when fewer than --diagnose-min-shifts metrics shifted")
    parser.add_argument("--diagnose-min-shifts", type=int, default=3,
                        help="Minimum shifted metrics to print under --diagnose-alert-only (default 3)")


def _add_report_flags(parser):
    """Add common report flags to a subparser."""
    parser.add_argument("--days", type=int, default=None, help="Number of recent days to include")
    parser.add_argument("--symbol-perf-days", type=int, default=14,
                        help="Window for SYMBOL PERFORMANCE aggregation (default: 14)")
    parser.add_argument("--aliases", type=str, default=None, help="Filter by alias (comma-separated)")
    parser.add_argument("--group", type=str, default=None, help="Filter by group (comma-separated)")
    parser.add_argument("--strat", type=str, default=None, help="Filter by strat (comma-separated)")
    parser.add_argument("--symbol", type=str, default=None, help="Filter by symbol (comma-separated)")
    parser.add_argument("--local-base", type=str, default=None, help="Local storage path")
    parser.add_argument("--no-alerts", action="store_true", help="Suppress anomaly alerts")
    parser.add_argument("--diagnose", action="store_true",
                        help="After the report, diagnose each flagged (strat, sym) "
                             "with trailing HL micro-structure stats")
    _add_diagnose_flags(parser)
    parser.add_argument("--config", action="store_true",
                        help="Include CONFIG CHANGES section (off by default — verbose)")


def _report_kwargs(args):
    """Extract common report kwargs from parsed args."""
    return dict(
        days=args.days,
        aliases=parse_list(args.aliases),
        groups=parse_list(getattr(args, "group", None)),
        strats=parse_list(getattr(args, "strat", None)),
        symbols=parse_list(getattr(args, "symbol", None)),
        show_alerts=not args.no_alerts,
        show_config=getattr(args, "config", False),
        symbol_perf_days=getattr(args, "symbol_perf_days", 14),
    )


def main():
    parser = argparse.ArgumentParser(description="Trade Manager v2")
    sub = parser.add_subparsers(dest="command")

    # fetch
    p_fetch = sub.add_parser("fetch", help="Discover and download acct/trade files")
    p_fetch.add_argument("--days", type=int, default=3, help="Number of days to fetch (default: 3)")
    p_fetch.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases (default: all)")
    p_fetch.add_argument("--local-base", type=str, default=None, help="Local storage path")
    p_fetch.add_argument("--force", action="store_true", help="Re-download files even if they exist locally")

    # report
    p_report = sub.add_parser("report", help="Print performance report from local data")
    _add_report_flags(p_report)

    # fetch-report
    p_fr = sub.add_parser("fetch-report", help="Fetch then report")
    _add_report_flags(p_fr)
    p_fr.set_defaults(days=3)
    p_fr.add_argument("--email", action="store_true", help="Also send report via email")
    p_fr.add_argument("--to", type=str, default=None, help="Override email recipient (default: group list)")
    p_fr.add_argument("--force", action="store_true", help="Re-download files even if they exist locally")

    # email-report
    p_email = sub.add_parser("email-report", help="Fetch, build report, and send via email")
    _add_report_flags(p_email)
    p_email.set_defaults(days=3)
    p_email.add_argument("--to", type=str, default=None, help="Override email recipient (default: group list)")
    p_email.add_argument("--force", action="store_true", help="Re-download files even if they exist locally")

    # sim-eval
    p_se = sub.add_parser("sim-eval", help="Run sim vs live evaluation")
    p_se.add_argument("--date", type=str, default=None, help="Live session date (YYYYMMDD)")
    p_se.add_argument("--date-range", type=str, default=None, help="Date range START:END (YYYYMMDD:YYYYMMDD)")
    p_se.add_argument("--date-offset", type=int, default=0, help="Sim date = live date + N (for overnight strats)")
    p_se.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases (default: all)")
    p_se.add_argument("--local-base", type=str, default=None, help="Local storage path")
    p_se.add_argument("--sim-dir", type=str, default=None, help="Sim output directory (default: ~/scratch/simeval)")
    p_se.add_argument("--strat", type=str, default=None, help="Filter strats by substring (e.g. 'usday')")
    p_se.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_se.add_argument("--sym", type=str, default=None, help="Filter symbols by substring (comma-separated, e.g. 'GOLD,CL,NVDA')")
    p_se.add_argument("--skip-fetch", action="store_true", help="Skip remote file fetch")
    p_se.add_argument("--skip-md", action="store_true", help="Skip market data download")
    p_se.add_argument("--auto-all", action="store_true", help="Auto-select all (strat, sym) pairs")
    p_se.add_argument("--days", type=int, default=None, help="Number of recent trading days")
    p_se.add_argument("--email", action="store_true", help="Email the report")

    # sim-drill
    p_sd = sub.add_parser("sim-drill", help="Drill down into sim vs live trade-level data")
    p_sd.add_argument("--date", type=str, required=True, help="Date (YYYYMMDD)")
    p_sd.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_sd.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_sd.add_argument("--sym", type=str, default=None, help="Single symbol to drill into (e.g. xyz:TSLA)")
    p_sd.add_argument("--time", type=str, default=None, help="Time window HH:MM-HH:MM (e.g. 09:30-10:00)")
    p_sd.add_argument("--local-base", type=str, default=None, help="Local storage path")
    p_sd.add_argument("--sim-dir", type=str, default=None, help="Sim output directory")

    # order-analysis
    p_oa = sub.add_parser("order-analysis", help="Analyze orders files for RTT and rejects")
    p_oa.add_argument("--days", type=int, default=3, help="Number of recent days (default: 3)")
    p_oa.add_argument("--date", type=str, default=None, help="Single date (YYYYMMDD)")
    p_oa.add_argument("--date-range", type=str, default=None, help="Date range START:END (YYYYMMDD:YYYYMMDD)")
    p_oa.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases")
    p_oa.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_oa.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_oa.add_argument("--local-base", type=str, default=None, help="Local storage path")

    # trends
    p_tr = sub.add_parser("trends",
                          help="RTT (ALO/IOC split) and fill-rate evolution over time, per strat/symbol")
    p_tr.add_argument("--days", type=int, default=14, help="Number of recent days (default: 14)")
    p_tr.add_argument("--date", type=str, default=None, help="Single date (YYYYMMDD)")
    p_tr.add_argument("--date-range", type=str, default=None, help="Date range START:END (YYYYMMDD:YYYYMMDD)")
    p_tr.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases")
    p_tr.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_tr.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_tr.add_argument("--symbol", type=str, default=None, help="Comma-separated symbols (exact, e.g. xyz:NVDA)")
    p_tr.add_argument("--strat-only", action="store_true", help="Show strat rollups only, skip per-symbol tables")
    p_tr.add_argument("--email", action="store_true", help="Email the report (in addition to printing)")
    p_tr.add_argument("--to", type=str, default=None, help="Override email recipient (default: group list)")
    p_tr.add_argument("--local-base", type=str, default=None, help="Local storage path")

    # markout
    p_mo = sub.add_parser("markout", help="Compute markouts on live trade files")
    p_mo.add_argument("--date", type=str, default=None, help="Single date (YYYYMMDD)")
    p_mo.add_argument("--date-range", type=str, default=None, help="Date range START:END")
    p_mo.add_argument("--days", type=int, default=1, help="Number of recent days (default: 1)")
    p_mo.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases")
    p_mo.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_mo.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_mo.add_argument("--offsets", type=str, default="1,10,30,60,120", help="Markout offsets in seconds")
    p_mo.add_argument("--force", action="store_true", help="Recompute even if markout file exists")
    p_mo.add_argument("--local-base", type=str, default=None, help="Local storage path")

    # tod (time-of-day)
    p_tod = sub.add_parser("tod", help="Time-of-day performance analysis from markout data")
    p_tod.add_argument("--date", type=str, default=None, help="Single date (YYYYMMDD)")
    p_tod.add_argument("--date-range", type=str, default=None, help="Date range START:END")
    p_tod.add_argument("--days", type=int, default=1, help="Number of recent days (default: 1)")
    p_tod.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases")
    p_tod.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_tod.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_tod.add_argument("--offsets", type=str, default="1,10,30,60,120", help="Markout offsets in seconds")
    p_tod.add_argument("--force", action="store_true", help="Recompute even if markout file exists")
    p_tod.add_argument("--local-base", type=str, default=None, help="Local storage path")

    # fillstats
    p_fs = sub.add_parser("fillstats", help="Fill context analysis with markout drill-down")
    p_fs.add_argument("--date", type=str, default=None, help="Single date (YYYYMMDD)")
    p_fs.add_argument("--date-range", type=str, default=None, help="Date range START:END")
    p_fs.add_argument("--days", type=int, default=1, help="Number of recent days (default: 1)")
    p_fs.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases")
    p_fs.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_fs.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_fs.add_argument("--offsets", type=str, default="1,10,30,60,120", help="Markout offsets in seconds")
    p_fs.add_argument("--force", action="store_true", help="Recompute even if fillstats file exists")
    p_fs.add_argument("--local-base", type=str, default=None, help="Local storage path")

    # email-sim-eval
    p_ese = sub.add_parser("email-sim-eval", help="Run sim-eval and email report (cron-friendly)")
    p_ese.add_argument("--days", type=int, default=5, help="Number of recent trading days (default: 5)")
    p_ese.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases (default: all)")
    p_ese.add_argument("--local-base", type=str, default=None, help="Local storage path")
    p_ese.add_argument("--sim-dir", type=str, default=None, help="Sim output directory")
    p_ese.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_ese.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_ese.add_argument("--date-offset", type=int, default=0, help="Sim date = live date + N")
    p_ese.add_argument("--skip-md", action="store_true", help="Skip market data download")

    # sim-sweep
    p_ss = sub.add_parser("sim-sweep", help="Sweep latency settings (legacy alo/cxl grid, or lag-aware base/coef grid)")
    p_ss.add_argument("--date", type=str, default=None, help="Date (YYYYMMDD)")
    p_ss.add_argument("--date-range", type=str, default=None, help="Date range START:END (YYYYMMDD:YYYYMMDD)")
    p_ss.add_argument("--days", type=int, default=None, help="Number of recent trading days")
    p_ss.add_argument("--date-offset", type=int, default=0, help="Sim date = live date + N")
    # Legacy fixed-latency axes (constants sweep) — pair both to enable
    p_ss.add_argument("--alo", type=str, default=None, help="Legacy ALO latencies in ms, comma-separated (e.g. 500,550,600)")
    p_ss.add_argument("--cxl", type=str, default=None, help="Legacy Cancel latencies in ms, comma-separated")
    # Lag-aware axes (per-TIF base + coef) — pair each base with its coef
    p_ss.add_argument("--alo-base", type=str, default=None, help="alo_lag_base_ms values, comma-separated (e.g. 80,100,120)")
    p_ss.add_argument("--alo-coef", type=str, default=None, help="alo_lag_coef values, comma-separated (e.g. 0.6,0.8,1.0)")
    p_ss.add_argument("--cxl-base", type=str, default=None, help="cxl_lag_base_ms values, comma-separated")
    p_ss.add_argument("--cxl-coef", type=str, default=None, help="cxl_lag_coef values, comma-separated")
    p_ss.add_argument("--ioc-base", type=str, default=None, help="ioc_lag_base_ms values, comma-separated")
    p_ss.add_argument("--ioc-coef", type=str, default=None, help="ioc_lag_coef values, comma-separated")
    p_ss.add_argument("--force-lag-aware", action="store_true",
                       help="Write lag_aware_latency=true and add HYPE extra_sub to each pktrader in variant configs")
    p_ss.add_argument("--pnl-weight", type=float, default=0.5,
                       help="Weight of |1 - pnl_ratio| in composite score (default 0.5)")
    p_ss.add_argument("--shs-weight", type=float, default=1.0,
                       help="Weight of |1 - shs_ratio| in composite score (default 1.0)")
    p_ss.add_argument("--fr-weight", type=float, default=1.0,
                       help="Weight of |1 - fillrate_ratio| in composite score (default 1.0)")
    p_ss.add_argument("--cfr-weight", type=float, default=1.0,
                       help="Weight of |1 - cfr_ratio| in composite score (default 1.0)")
    p_ss.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases")
    p_ss.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_ss.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_ss.add_argument("--local-base", type=str, default=None, help="Local storage path")
    p_ss.add_argument("--sim-dir", type=str, default=None, help="Sim output directory")
    p_ss.add_argument("--skip-fetch", action="store_true", help="Skip remote file fetch")
    p_ss.add_argument("--skip-md", action="store_true", help="Skip market data download")
    p_ss.add_argument("--email", action="store_true", help="Email the report")
    p_ss.add_argument("--output", type=str, default=None, help="Save report to file")

    # bin-regtest
    p_br = sub.add_parser("bin-regtest", help="Binary regression test: compare sims from two pktrade builds")
    p_br.add_argument("--base-repo", type=str, required=True, help="Path to clean repo checkout (reference build)")
    p_br.add_argument("--days", type=int, default=None, help="Number of recent trading days (default: 7)")
    p_br.add_argument("--date-range", type=str, default=None, help="Date range START:END (YYYYMMDD:YYYYMMDD)")
    p_br.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases (default: all)")
    p_br.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_br.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_br.add_argument("--auto-all", action="store_true", help="Auto-select all strats (skip interactive menu)")
    p_br.add_argument("--skip-fetch", action="store_true", help="Skip remote file fetch")
    p_br.add_argument("--skip-md", action="store_true", help="Skip market data download")
    p_br.add_argument("--local-base", type=str, default=None, help="Local storage path")
    p_br.add_argument("--tol-pnl-pct", type=float, default=10, help="PnL tolerance threshold in percent (default: 10)")
    p_br.add_argument("--tol-vol-pct", type=float, default=15, help="Volume tolerance threshold in percent (default: 15)")

    # diagnose-flagged
    p_dgf = sub.add_parser("diagnose-flagged",
                           help="Pull bad performers from the daily turnoff evaluation and "
                                "diagnose each with trailing HL micro-structure stats")
    p_dgf.add_argument("--aliases", type=str, default=None)
    p_dgf.add_argument("--group", type=str, default=None)
    p_dgf.add_argument("--strat", type=str, default=None)
    p_dgf.add_argument("--symbol", type=str, default=None)
    p_dgf.add_argument("--local-base", type=str, default=None)
    _add_diagnose_flags(p_dgf)

    # email-diagnose-flagged
    p_edgf = sub.add_parser("email-diagnose-flagged",
                            help="Run diagnose-flagged and email the report (cron-friendly; "
                                 "skips email if nothing's flagged)")
    p_edgf.add_argument("--aliases", type=str, default=None)
    p_edgf.add_argument("--group", type=str, default=None)
    p_edgf.add_argument("--strat", type=str, default=None)
    p_edgf.add_argument("--symbol", type=str, default=None)
    p_edgf.add_argument("--local-base", type=str, default=None)
    p_edgf.add_argument("--to", type=str, default=None,
                        help="Override email recipient (default: group list)")
    _add_diagnose_flags(p_edgf)

    # diagnose
    p_dg = sub.add_parser("diagnose",
                          help="Compare trailing-window HL (+optional topbook) stats for (strat,sym) pairs and flag shifts")
    p_dg.add_argument("--pairs", type=str, required=True,
                      help="Comma-separated 'alias/group/strat:xyz:SYMBOL' pairs")
    p_dg.add_argument("--days-recent", type=int, default=5,
                      help="Trailing days as the 'recent' window (default 5)")
    p_dg.add_argument("--days-prior", type=int, default=10,
                      help="Days before the recent window to use as 'prior' (default 10)")
    p_dg.add_argument("--with-topbook", action="store_true",
                      help="Also pull topbook stats from Databento (costs credits)")
    p_dg.add_argument("--topbook-dataset", type=str, default="XNAS.BASIC",
                      choices=["XNAS.BASIC", "EQUS.MINI"])
    p_dg.add_argument("--alert-only", action="store_true",
                      help="Suppress detail when no shifts flagged")
    p_dg.add_argument("--local-base", type=str, default=None, help="Local storage path")

    # log-events
    p_le = sub.add_parser("log-events", help="Parse INFO logs for actionable events")
    p_le.add_argument("--date", type=str, default=None, help="Date (YYYYMMDD)")
    p_le.add_argument("--date-range", type=str, default=None, help="Date range START:END (YYYYMMDD:YYYYMMDD)")
    p_le.add_argument("--aliases", type=str, default=None, help="Comma-separated SSH aliases")
    p_le.add_argument("--strat", type=str, default=None, help="Filter strats by substring")
    p_le.add_argument("--group", type=str, default=None, help="Filter groups by substring")
    p_le.add_argument("--types", type=str, default=None,
                       help="Comma-separated event types: usermsg,inherit,risk_level,cancel_pause,feed_latency,reject,overnight_pos,size_mult")
    p_le.add_argument("--local-base", type=str, default=None, help="Local storage path")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    local_base = getattr(args, "local_base", None) or CONFIG["local_base"]
    aliases_list = parse_list(args.aliases) if getattr(args, "aliases", None) else CONFIG["ssh_aliases"]

    if args.command == "fetch":
        fetch_all(aliases_list, args.days, local_base, CONFIG["remote_base"],
                  force=getattr(args, "force", False))
        from symstats_cache import populate_from_local_configs
        populate_from_local_configs(local_base, args.days)

    elif args.command == "report":
        report(local_base, **_report_kwargs(args))
        if getattr(args, "diagnose", False):
            _run_post_report_diagnose(args, local_base, aliases_list)

    elif args.command in ("fetch-report", "email-report"):
        force = getattr(args, "force", False)
        fetch_days = max(args.days, getattr(args, "symbol_perf_days", 14))
        fetch_all(aliases_list, fetch_days, local_base, CONFIG["remote_base"], force=force)
        print()
        print("─" * 90)
        print()

        if args.command == "email-report" or getattr(args, "email", False):
            kwargs = _report_kwargs(args)
            text, _ = build_report(local_base, **kwargs)
            if text is None:
                print("No data found. Run 'fetch' first or adjust filters.")
            else:
                print(text, end="")
                subject = f"Trade Report — {datetime.today().strftime('%Y-%m-%d')}"
                send_kwargs = {"subject": subject, "body": text, "monospace": True}
                to = getattr(args, "to", None)
                if to:
                    send_kwargs["receiver"] = to
                email_utils.send_mail(**send_kwargs)
        else:
            report(local_base, **_report_kwargs(args))

        if getattr(args, "diagnose", False):
            _run_post_report_diagnose(args, local_base, aliases_list)

        # Refresh symstats cache after the email goes out. Current report
        # uses whatever's already cached; the latest day's capacity columns
        # may be missing until the next run.
        from symstats_cache import populate_from_local_configs
        populate_from_local_configs(local_base, args.days)

    elif args.command == "sim-eval":
        from sim_eval import run_sim_eval
        run_sim_eval(args, CONFIG)

    elif args.command == "email-sim-eval":
        args.skip_fetch = False
        args.auto_all = True
        args.email = True
        args.date = None
        args.date_range = None
        from sim_eval import run_sim_eval
        run_sim_eval(args, CONFIG)

    elif args.command == "bin-regtest":
        from bin_regtest import run_bin_regtest
        run_bin_regtest(args, CONFIG)

    elif args.command == "sim-sweep":
        from sim_sweep import run_sim_sweep
        run_sim_sweep(args, CONFIG)

    elif args.command == "sim-drill":
        from sim_eval_drill import run_drill
        run_drill(args, CONFIG)

    elif args.command == "order-analysis":
        from order_analysis import analyze_orders, summarize_orders
        if args.date:
            date_strs = [args.date]
        elif args.date_range:
            start, end = args.date_range.split(":")
            d = datetime.strptime(start, "%Y%m%d")
            end_d = datetime.strptime(end, "%Y%m%d")
            date_strs = []
            while d <= end_d:
                date_strs.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            date_strs = [(datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
                         for i in range(args.days - 1, -1, -1)]
        results = analyze_orders(local_base, aliases_list, date_strs,
                                 strat_filter=args.strat, group_filter=args.group)
        if not results:
            print("No orders files found.")
        else:
            print(f"Parsed {len(results)} orders files.")
            summarize_orders(results)

    elif args.command == "trends":
        if args.date:
            date_strs = [args.date]
        elif args.date_range:
            start, end = args.date_range.split(":")
            d = datetime.strptime(start, "%Y%m%d")
            end_d = datetime.strptime(end, "%Y%m%d")
            date_strs = []
            while d <= end_d:
                date_strs.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            date_strs = [(datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
                         for i in range(args.days - 1, -1, -1)]
        text = run_trends(local_base, aliases_list, date_strs,
                          strat_filter=args.strat, group_filter=args.group,
                          symbols=parse_list(args.symbol) if args.symbol else None,
                          strat_only=args.strat_only)
        print(text)
        if args.email:
            subject = f"Trends (RTT/fill) — {datetime.today().strftime('%Y-%m-%d')}"
            send_kwargs = {"subject": subject, "body": text, "monospace": True}
            if args.to:
                send_kwargs["receiver"] = args.to
            email_utils.send_mail(**send_kwargs)
            print(f"Emailed trends report to {args.to or 'default group list'}.")

    elif args.command == "diagnose":
        from postmortems.pm_diagnose import run_diagnose
        run_diagnose(args, CONFIG)

    elif args.command == "diagnose-flagged":
        from postmortems.pm_diagnose import diagnose_flagged_rows
        flagged = compute_flagged_pairs(
            local_base,
            aliases=aliases_list,
            groups=parse_list(args.group) if args.group else None,
            strats=parse_list(args.strat) if args.strat else None,
            symbols=parse_list(args.symbol) if args.symbol else None,
        )
        diagnose_flagged_rows(
            local_base, flagged,
            recent_n=args.diagnose_days_recent,
            prior_n=args.diagnose_days_prior,
            with_topbook=args.diagnose_with_topbook,
            alert_only=args.diagnose_alert_only,
            topbook_dataset=args.diagnose_topbook_dataset,
            min_shifts=args.diagnose_min_shifts,
        )

    elif args.command == "email-diagnose-flagged":
        import contextlib
        from postmortems.pm_diagnose import diagnose_flagged_rows
        flagged = compute_flagged_pairs(
            local_base,
            aliases=aliases_list,
            groups=parse_list(args.group) if args.group else None,
            strats=parse_list(args.strat) if args.strat else None,
            symbols=parse_list(args.symbol) if args.symbol else None,
        )
        # Default to alert-only with min-shifts for cron mode unless caller overrides.
        if not args.diagnose_alert_only:
            args.diagnose_alert_only = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            diagnose_flagged_rows(
                local_base, flagged,
                recent_n=args.diagnose_days_recent,
                prior_n=args.diagnose_days_prior,
                with_topbook=args.diagnose_with_topbook,
                alert_only=args.diagnose_alert_only,
                topbook_dataset=args.diagnose_topbook_dataset,
                min_shifts=args.diagnose_min_shifts,
            )
        body = buf.getvalue()
        print(body, end="")
        n_flagged = body.count("\n  FLAGGED:")
        if n_flagged == 0:
            print(f"\n[email-diagnose-flagged] no pairs met min-shifts={args.diagnose_min_shifts}; skipping email.")
        else:
            subject = (f"Diagnose Flagged ({n_flagged}) — "
                       f"{datetime.today().strftime('%Y-%m-%d')}")
            send_kwargs = {"subject": subject, "body": body, "monospace": True}
            to = getattr(args, "to", None)
            if to:
                send_kwargs["receiver"] = to
            email_utils.send_mail(**send_kwargs)

    elif args.command == "log-events":
        from sim_eval_logs import parse_log_events, summarize_events
        if args.date:
            date_strs = [args.date]
        elif args.date_range:
            start, end = args.date_range.split(":")
            # generate date list
            d = datetime.strptime(start, "%Y%m%d")
            end_d = datetime.strptime(end, "%Y%m%d")
            date_strs = []
            while d <= end_d:
                date_strs.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            # default: today
            date_strs = [datetime.today().strftime("%Y%m%d")]
        event_types = set(args.types.split(",")) if args.types else None
        events = parse_log_events(local_base, aliases_list, date_strs, event_types)
        # apply strat/group filters
        if args.strat:
            events = [e for e in events if args.strat in e["strat"]]
        if args.group:
            events = [e for e in events if args.group in e["group"]]
        if not events:
            print("No events found.")
        else:
            print(f"Found {len(events)} events.")
            summarize_events(events)

    elif args.command == "markout":
        from markout_analysis import run_markouts, build_markout_report
        if args.date:
            date_strs = [args.date]
        elif args.date_range:
            start, end = args.date_range.split(":")
            d = datetime.strptime(start, "%Y%m%d")
            end_d = datetime.strptime(end, "%Y%m%d")
            date_strs = []
            while d <= end_d:
                date_strs.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            date_strs = [(datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
                         for i in range(args.days - 1, -1, -1)]
        offsets = [int(x) for x in args.offsets.split(",")]
        results = run_markouts(local_base, aliases_list, date_strs, offsets,
                               strat_filter=args.strat, group_filter=args.group,
                               force=args.force)
        if not results:
            print("No trade files found or markout binary unavailable.")
        else:
            print(build_markout_report(results, offsets))

    elif args.command == "tod":
        from markout_analysis import run_markouts, build_tod_report
        if args.date:
            date_strs = [args.date]
        elif args.date_range:
            start, end = args.date_range.split(":")
            d = datetime.strptime(start, "%Y%m%d")
            end_d = datetime.strptime(end, "%Y%m%d")
            date_strs = []
            while d <= end_d:
                date_strs.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            date_strs = [(datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
                         for i in range(args.days - 1, -1, -1)]
        offsets = [int(x) for x in args.offsets.split(",")]
        results = run_markouts(local_base, aliases_list, date_strs, offsets,
                               strat_filter=args.strat, group_filter=args.group,
                               force=args.force)
        if not results:
            print("No trade files found or markout binary unavailable.")
        else:
            print(build_tod_report(results, offsets))

    elif args.command == "fillstats":
        from fillstats_analysis import run_fillstats, build_fillstats_report
        if args.date:
            date_strs = [args.date]
        elif args.date_range:
            start, end = args.date_range.split(":")
            d = datetime.strptime(start, "%Y%m%d")
            end_d = datetime.strptime(end, "%Y%m%d")
            date_strs = []
            while d <= end_d:
                date_strs.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            date_strs = [(datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
                         for i in range(args.days - 1, -1, -1)]
        offsets = [int(x) for x in args.offsets.split(",")]
        results = run_fillstats(local_base, aliases_list, date_strs, offsets,
                                strat_filter=args.strat, group_filter=args.group,
                                force=args.force)
        if not results:
            print("No trade files found or fillstats binary unavailable.")
        else:
            print(build_fillstats_report(results, offsets))


if __name__ == "__main__":
    main()
