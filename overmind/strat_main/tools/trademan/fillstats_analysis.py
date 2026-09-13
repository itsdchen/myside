"""Fill-stats analysis — run fillstats binary, parse results, drill-down reports.

The fillstats binary is a superset of markout: it replays both local and remote
order books, computes per-trade feature columns (spread, LMR, volatility, etc.)
at fill time, plus the usual markout columns.  The Python side segments trades
by these features to surface patterns (e.g., "markouts are worst when spread
is wide and remote has moved away").
"""

import io
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

# ── Path setup (same pattern as markout_analysis.py) ─────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util.pathing import bindir, datadir

# Base trade CSV has 22 columns (no header)
_N_BASE_COLS = 22

# Feature columns appended by fillstats binary (between base cols and markout cols)
FEATURE_COLS = [
    "lmr", "lmr_ema", "local_ret_ema", "remote_ret_ema",
    "local_spread", "local_spread_ema", "local_notional",
]

_N_FEATURE_COLS = len(FEATURE_COLS)

_BASE_NAMES = [
    "timestamp", "time_ms", "order_num", "sym", "market", "side",
    "price", "size", "end_pos", "closed_pnl", "commission",
    "col11", "bid", "ask", "col14", "mid", "col16",
    "col17", "col18", "col19", "add_remove", "front_flag",
]


# ── Config parsing ───────────────────────────────────────────────────────────

def extract_remote_map(config_path):
    """Parse pk_*.json config, return remote map string for fillstats binary.

    Returns string like "xyz:AAPL=AAPL@TopBookEquity,xyz:CL=CL@TopBookCme"
    by following: pktraders[i].ordex.remote_sig -> signals[j] -> symbol, books
    """
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] Cannot read config {config_path}: {e}", file=sys.stderr)
        return ""

    mappings = []
    for trader in cfg.get("pktraders", []):
        traded_sym = trader.get("traded_symbol", "")
        if not traded_sym:
            continue

        ordex = trader.get("ordex", {})
        remote_sig_name = ordex.get("remote_sig", "")
        if not remote_sig_name:
            continue

        # Find the signal with this name
        signals = trader.get("signals", [])
        for sig in signals:
            if sig.get("name") == remote_sig_name:
                remote_sym = sig.get("symbol", "")
                books = sig.get("books", [])
                if remote_sym and books:
                    remote_market = books[0]
                    mappings.append(f"{traded_sym}={remote_sym}@{remote_market}")
                break

    return ",".join(mappings)


def _find_config(strat_dir, date_str):
    """Find pk_*.json.YYYYMMDD or fall back to pk_*.json in strat dir."""
    strat_path = Path(strat_dir)

    # Try exact date match first
    configs = list(strat_path.glob(f"pk_*.json.{date_str}"))
    if configs:
        return str(configs[0])

    # Fall back to most recent dated backup before this date
    all_dated = []
    for f in strat_path.glob("pk_*.json.*"):
        suffix = f.name.rsplit(".", 1)[-1]
        if len(suffix) == 8 and suffix.isdigit() and suffix <= date_str:
            all_dated.append((suffix, f))
    if all_dated:
        all_dated.sort(key=lambda x: x[0], reverse=True)
        return str(all_dated[0][1])

    # Fall back to undated pk_*.json
    undated = list(strat_path.glob("pk_*.json"))
    if undated:
        return str(undated[0])

    return None


# ── Binary execution ─────────────────────────────────────────────────────────

def _run_fillstats_binary(trades_path, date_str, offsets, remote_map="",
                          data_dir=None):
    """Run bin/fillstats on a single trades file.

    Returns (success, output_path, message).
    """
    binary = os.path.join(bindir(), "fillstats")
    if not os.path.isfile(binary):
        return False, None, f"fillstats binary not found at {binary}"

    offsets_str = ",".join(str(o) for o in offsets)
    cmd = [binary, "--trades", str(trades_path), "--date", date_str,
           "-m", offsets_str]
    if data_dir:
        cmd += ["--datadir", data_dir]
    if remote_map:
        cmd += ["--remote-map", remote_map]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, None, "fillstats binary timed out"

    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        return False, None, f"fillstats failed: {msg}"

    # Binary writes to trades_YYYYMMDD_fillstats.csv by default
    base = str(trades_path).replace(".csv", "_fillstats.csv")
    if os.path.isfile(base):
        return True, base, result.stdout.strip()
    return False, None, f"expected output not found at {base}"


# ── File parsing ─────────────────────────────────────────────────────────────

def _has_header(path):
    """Check if a fillstats CSV has a header line."""
    try:
        with open(path, "r") as f:
            first = f.readline().strip()
        return first.split(",", 1)[0] == "timestamp"
    except OSError:
        return False


def _infer_offsets_from_file(path):
    """Read the header of a fillstats CSV to extract markout offsets."""
    try:
        with open(path, "r") as f:
            line = f.readline().strip()
    except OSError:
        return []

    cols = line.split(",")
    if not cols or cols[0] != "timestamp":
        return []

    offsets = []
    for col in cols:
        m = re.match(r"^mo_(\d+)s$", col)
        if m:
            offsets.append(int(m.group(1)))
    return offsets


def parse_fillstats_file(path, offsets):
    """Read a _fillstats.csv file and return a DataFrame."""
    mo_names = [f"mo_{o}s" for o in offsets]

    try:
        if _has_header(path):
            df = pd.read_csv(path)
        else:
            n_total = _N_BASE_COLS + _N_FEATURE_COLS + len(offsets)
            col_names = _BASE_NAMES + FEATURE_COLS + mo_names
            df = pd.read_csv(path, header=None, names=col_names[:n_total])
    except Exception as e:
        print(f"  [warn] Could not read {path}: {e}", file=sys.stderr)
        return None

    if df.empty:
        return None

    # Ensure numeric columns
    numeric_cols = ["price", "size", "mid"] + FEATURE_COLS + mo_names
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


# ── Run fillstats across files ───────────────────────────────────────────────

def run_fillstats(local_base, aliases, date_strs, offsets,
                  strat_filter=None, group_filter=None, force=False):
    """Discover trades files, extract remote config, run fillstats binary.

    Returns dict keyed by (alias, group, strat, date) with DataFrames.
    """
    results = {}
    base = Path(local_base)
    if not base.exists():
        return results

    data_dir = datadir()

    for alias_dir in sorted(base.iterdir()):
        if not alias_dir.is_dir() or alias_dir.name not in aliases:
            continue
        alias = alias_dir.name
        for group_dir in sorted(alias_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            if group_filter and group_filter not in group:
                continue
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name
                if strat_filter and strat_filter not in strat:
                    continue

                for ds in date_strs:
                    trades_file = strat_dir / f"trades_{ds}.csv"
                    fillstats_file = strat_dir / f"trades_{ds}_fillstats.csv"
                    if not trades_file.exists():
                        continue

                    if fillstats_file.exists() and not force:
                        # Reuse cached results only when the stored offsets
                        # exactly match the requested offsets. Otherwise
                        # regenerate so downstream reports stay consistent.
                        existing_offsets = _infer_offsets_from_file(fillstats_file)
                        need_recompute = (not existing_offsets) or (existing_offsets != offsets)
                        if need_recompute:
                            print(
                                f"  [{alias}] {group}/{strat} {ds} "
                                "offsets changed, recomputing",
                                flush=True,
                            )
                        else:
                            df = parse_fillstats_file(fillstats_file, offsets)
                            if df is not None:
                                results[(alias, group, strat, ds)] = df
                            continue

                    # Find config and extract remote map
                    config_path = _find_config(strat_dir, ds)
                    remote_map = ""
                    if config_path:
                        remote_map = extract_remote_map(config_path)

                    # Run binary
                    print(f"  [{alias}] {group}/{strat} {ds} ... ", end="",
                          flush=True)
                    ok, out_path, msg = _run_fillstats_binary(
                        trades_file, ds, offsets, remote_map, data_dir)
                    if ok:
                        print(msg)
                        df = parse_fillstats_file(out_path, offsets)
                        if df is not None:
                            results[(alias, group, strat, ds)] = df
                    else:
                        print(msg)

    return results


# ── Drill-down report (standalone fillstats command) ─────────────────────────

def build_fillstats_report(results, offsets):
    """Build a human-readable fill context report with quartile drill-down.

    For each (alias, group, strat), for each feature column, bucket trades
    into quartiles, show avg markout per bucket.
    """
    out = io.StringIO()
    mo_names = [f"mo_{o}s" for o in offsets]

    out.write("=" * 90 + "\n")
    out.write("FILL CONTEXT ANALYSIS\n")
    out.write("=" * 90 + "\n")

    # Group by (alias, group, strat)
    by_strat = defaultdict(list)
    for (alias, group, strat, ds), df in results.items():
        by_strat[(alias, group, strat)].append(df)

    for key in sorted(by_strat):
        alias, group, strat = key
        all_df = pd.concat(by_strat[key], ignore_index=True)
        n_days = len(by_strat[key])

        out.write(f"\n  [{alias}] {group}/{strat}  "
                  f"({n_days} day(s), {len(all_df)} fills)\n")

        for feat in FEATURE_COLS:
            if feat not in all_df.columns:
                continue

            valid = all_df[feat].dropna()
            if len(valid) < 8:
                continue

            out.write(f"\n  -- By {feat} --\n")

            # Compute quartile boundaries
            q1, q2, q3 = valid.quantile([0.25, 0.5, 0.75])

            # Bucket trades
            def bucket_label(v):
                if pd.isna(v):
                    return None
                if v < q1:
                    return f"Q1 (< {q1:.4g})"
                elif v < q2:
                    return f"Q2 ({q1:.4g}-{q2:.4g})"
                elif v < q3:
                    return f"Q3 ({q2:.4g}-{q3:.4g})"
                else:
                    return f"Q4 (> {q3:.4g})"

            all_df["_bucket"] = all_df[feat].apply(bucket_label)
            bucketed = all_df[all_df["_bucket"].notna()]

            # Header
            hdr = f"    {'bucket':<28} {'fills':>5}"
            for mn in mo_names:
                hdr += f"  {mn:>8}"
            out.write(hdr + "\n")
            out.write("    " + "-" * (len(hdr) - 4) + "\n")

            # Order: Q1, Q2, Q3, Q4
            for bname in sorted(bucketed["_bucket"].unique()):
                bdf = bucketed[bucketed["_bucket"] == bname]
                line = f"    {bname:<28} {len(bdf):>5}"
                for mn in mo_names:
                    if mn in bdf.columns:
                        val = bdf[mn].mean()
                        line += f"  {val:>8.4f}" if not pd.isna(val) else f"  {'':>8}"
                    else:
                        line += f"  {'':>8}"
                out.write(line + "\n")

            # Clean up temp column
            all_df.drop(columns=["_bucket"], inplace=True, errors="ignore")

    out.write("\n")
    return out.getvalue()


# ── Embedded report section (for main report) ────────────────────────────────

def build_fillstats_report_section(out, local_base, data, dates_present):
    """Compact fillstats section for main report. Reads pre-computed files.

    Shows top 1-2 most discriminative features per strat.
    """
    aliases_in_data = sorted(data["alias"].unique())
    date_strs = sorted(dates_present)

    strats_in_data = set()
    for _, r in data.iterrows():
        strats_in_data.add((r["alias"], r["group"], r["strat"]))

    base = Path(local_base)
    if not base.exists():
        return

    # Discover existing fillstats files
    found = {}
    for alias_dir in sorted(base.iterdir()):
        if not alias_dir.is_dir() or alias_dir.name not in aliases_in_data:
            continue
        alias = alias_dir.name
        for group_dir in sorted(alias_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name
                if (alias, group, strat) not in strats_in_data:
                    continue
                for ds in date_strs:
                    fs_file = strat_dir / f"trades_{ds}_fillstats.csv"
                    if fs_file.exists():
                        found[(alias, group, strat, ds)] = fs_file

    if not found:
        return

    first_path = next(iter(found.values()))
    offsets = _infer_offsets_from_file(first_path)
    if not offsets:
        return
    mo_names = [f"mo_{o}s" for o in offsets]

    # Parse all found files
    parsed = {}
    for key, path in found.items():
        df = parse_fillstats_file(path, offsets)
        if df is not None:
            parsed[key] = df

    if not parsed:
        return

    out.write("=" * 90 + "\n")
    out.write("FILL CONTEXT ANALYSIS\n")
    out.write("=" * 90 + "\n")

    # Group by (alias, group, strat)
    by_strat = defaultdict(list)
    for (alias, group, strat, ds), df in parsed.items():
        by_strat[(alias, group, strat)].append(df)

    # Pick the longest markout column for ranking features
    rank_col = mo_names[-1] if mo_names else None

    for key in sorted(by_strat):
        alias, group, strat = key
        all_df = pd.concat(by_strat[key], ignore_index=True)

        out.write(f"\n  [{alias}] {group}/{strat}\n")

        if rank_col is None or rank_col not in all_df.columns:
            continue

        # Find most discriminative features: largest Q4-Q1 spread on rank_col
        feat_spreads = []
        for feat in FEATURE_COLS:
            if feat not in all_df.columns:
                continue
            valid = all_df[[feat, rank_col]].dropna()
            if len(valid) < 8:
                continue
            q1_val = valid[feat].quantile(0.25)
            q3_val = valid[feat].quantile(0.75)
            q1_mo = valid[valid[feat] < q1_val][rank_col].mean()
            q4_mo = valid[valid[feat] >= q3_val][rank_col].mean()
            if not pd.isna(q1_mo) and not pd.isna(q4_mo):
                feat_spreads.append((feat, abs(q4_mo - q1_mo), q1_mo, q4_mo))

        feat_spreads.sort(key=lambda x: -x[1])

        # Show top 2 features
        for feat, spread, q1_mo, q4_mo in feat_spreads[:2]:
            valid = all_df[[feat] + mo_names].dropna(subset=[feat])
            q1, q2, q3 = valid[feat].quantile([0.25, 0.5, 0.75])

            out.write(f"\n    -- By {feat} --\n")
            hdr = f"      {'bucket':<28} {'fills':>5}"
            for mn in mo_names:
                hdr += f"  {mn:>8}"
            out.write(hdr + "\n")
            out.write("      " + "-" * (len(hdr) - 6) + "\n")

            def bucket(v):
                if pd.isna(v):
                    return None
                if v < q1:
                    return f"Q1 (< {q1:.4g})"
                elif v < q2:
                    return f"Q2"
                elif v < q3:
                    return f"Q3"
                else:
                    return f"Q4 (> {q3:.4g})"

            valid = valid.copy()
            valid["_b"] = valid[feat].apply(bucket)
            valid = valid[valid["_b"].notna()]

            for bname in sorted(valid["_b"].unique()):
                bdf = valid[valid["_b"] == bname]
                line = f"      {bname:<28} {len(bdf):>5}"
                for mn in mo_names:
                    if mn in bdf.columns:
                        val = bdf[mn].mean()
                        line += f"  {val:>8.4f}" if not pd.isna(val) else f"  {'':>8}"
                    else:
                        line += f"  {'':>8}"
                out.write(line + "\n")

    out.write("\n")
