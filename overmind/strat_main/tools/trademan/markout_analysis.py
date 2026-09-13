"""Markout analysis — run markout binary, parse results, format reports.

The markout binary replays the order book and computes side-adjusted future
mid-price movement at configurable offsets after each trade.  Output is the
original trades CSV with markout columns appended.
"""

import io
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

# ── Path setup (same pattern as sim_eval.py) ─────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util.pathing import bindir, datadir

# Base trade CSV has 22 columns (no header)
_N_BASE_COLS = 22

# Column names that matter for markout parsing
_BASE_NAMES = [
    "timestamp", "time_ms", "order_num", "sym", "market", "side",
    "price", "size", "end_pos", "closed_pnl", "commission",
    "col11", "bid", "ask", "col14", "mid", "col16",
    "col17", "col18", "col19", "add_remove", "front_flag",
]


# ── Binary execution ─────────────────────────────────────────────────────────

def _run_markout_binary(trades_path, date_str, offsets, data_dir=None):
    """Run bin/markout on a single trades file.

    Returns (success, output_path, message).
    """
    binary = os.path.join(bindir(), "markout")
    if not os.path.isfile(binary):
        return False, None, f"markout binary not found at {binary}"

    offsets_str = ",".join(str(o) for o in offsets)
    cmd = [binary, "--trades", str(trades_path), "--date", date_str, "-m", offsets_str]
    if data_dir:
        cmd += ["--datadir", data_dir]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, None, "markout binary timed out"

    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        return False, None, f"markout failed: {msg}"

    # Binary writes to trades_YYYYMMDD_markout.csv by default
    base = str(trades_path).replace(".csv", "_markout.csv")
    if os.path.isfile(base):
        return True, base, result.stdout.strip()
    return False, None, f"expected output not found at {base}"


# ── File parsing ─────────────────────────────────────────────────────────────

def _has_header(path):
    """Check if a markout CSV has a header line (first field is 'timestamp')."""
    try:
        with open(path, "r") as f:
            first = f.readline().strip()
        return first.split(",", 1)[0] == "timestamp"
    except OSError:
        return False


def parse_markout_file(path, offsets):
    """Read a _markout.csv file and return a DataFrame with trade + markout columns.

    Supports both header (new binary) and headerless (legacy) formats.
    """
    mo_names = [f"mo_{o}s" for o in offsets]

    try:
        if _has_header(path):
            df = pd.read_csv(path)
        else:
            n_total = _N_BASE_COLS + len(offsets)
            col_names = _BASE_NAMES + mo_names
            df = pd.read_csv(path, header=None, names=col_names[:n_total])
    except Exception as e:
        print(f"  [warn] Could not read {path}: {e}", file=sys.stderr)
        return None

    if df.empty:
        return None

    for col in ["price", "size", "mid"] + mo_names:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    return df


def _infer_offsets_from_file(path):
    """Read the header of a markout CSV to extract the actual offsets.

    Header columns after the base 22 are named mo_Ns (e.g. mo_1s, mo_10s).
    Returns list of integer offsets, e.g. [1, 10, 30, 60, 120].
    Falls back to column-count guessing for legacy headerless files.
    """
    try:
        with open(path, "r") as f:
            line = f.readline().strip()
    except OSError:
        return []

    cols = line.split(",")
    if not cols:
        return []

    # Detect header: first column of a header is "timestamp" (text),
    # first column of a data line is a datetime like "20260212 ..."
    if cols[0] == "timestamp":
        # Parse mo_Ns columns
        offsets = []
        for col in cols[_N_BASE_COLS:]:
            m = re.match(r"^mo_(\d+)s$", col)
            if m:
                offsets.append(int(m.group(1)))
        return offsets

    # Legacy headerless file — fall back to column count
    n_mo = len(cols) - _N_BASE_COLS
    if n_mo <= 0:
        return []
    return list(range(n_mo))


# ── Run markouts across files ────────────────────────────────────────────────

def run_markouts(local_base, aliases, date_strs, offsets, strat_filter=None,
                 group_filter=None, force=False):
    """Discover trades files and run the markout binary on each.

    Returns dict keyed by (alias, group, strat, date) with parsed DataFrames.
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
                    markout_file = strat_dir / f"trades_{ds}_markout.csv"
                    if not trades_file.exists():
                        continue

                if markout_file.exists() and not force:
                    # Only reuse cached output if the stored offsets match the
                    # offsets requested for this run. Otherwise recompute so
                    # callers are guaranteed consistent columns.
                    existing_offsets = _infer_offsets_from_file(markout_file)
                    need_recompute = (not existing_offsets) or (existing_offsets != offsets)
                    if need_recompute:
                        print(
                            f"  [{alias}] {group}/{strat} {ds} "
                            "offsets changed, recomputing",
                            flush=True,
                        )
                    else:
                        df = parse_markout_file(markout_file, offsets)
                        if df is not None:
                            results[(alias, group, strat, ds)] = df
                        continue

                # Run binary
                print(f"  [{alias}] {group}/{strat} {ds} ... ", end="", flush=True)
                ok, out_path, msg = _run_markout_binary(
                    trades_file, ds, offsets, data_dir)
                if ok:
                    print(msg)
                    df = parse_markout_file(out_path, offsets)
                    if df is not None:
                        results[(alias, group, strat, ds)] = df
                else:
                    print(msg)

    return results


# ── Standalone report (markout command) ──────────────────────────────────────

def build_markout_report(results, offsets):
    """Build a human-readable markout report from run_markouts results."""
    out = io.StringIO()
    mo_names = [f"mo_{o}s" for o in offsets]
    offsets_label = ",".join(f"{o}s" for o in offsets)

    out.write("=" * 90 + "\n")
    out.write("MARKOUT ANALYSIS\n")
    out.write("=" * 90 + "\n")

    # Group by (alias, group, strat)
    by_strat = defaultdict(dict)
    for (alias, group, strat, ds), df in results.items():
        by_strat[(alias, group, strat)][ds] = df

    for key in sorted(by_strat):
        alias, group, strat = key
        dates_data = by_strat[key]

        for ds in sorted(dates_data):
            df = dates_data[ds]
            out.write(f"\n  [{alias}] {group}/{strat}  date={ds}  offsets={offsets_label}\n")

            # ── Per-symbol breakdown ─────────────────────────────────
            out.write(f"\n    per symbol (avg markout in price units, positive = favorable):\n")
            hdr = f"    {'sym':<20} {'fills':>5}"
            for mn in mo_names:
                hdr += f"  {mn:>8}"
            out.write(hdr + "\n")
            out.write("    " + "-" * (len(hdr) - 4) + "\n")

            sym_grp = df.groupby("sym")
            sym_stats = []
            for sym, sdf in sym_grp:
                row = {"sym": sym, "fills": len(sdf)}
                for mn in mo_names:
                    row[mn] = sdf[mn].mean()
                sym_stats.append(row)
            sym_stats.sort(key=lambda r: -r["fills"])

            for r in sym_stats:
                line = f"    {r['sym']:<20} {r['fills']:>5}"
                for mn in mo_names:
                    line += f"  {r[mn]:>8.3f}"
                out.write(line + "\n")

            # Total
            line = f"    {'TOTAL':<20} {len(df):>5}"
            for mn in mo_names:
                line += f"  {df[mn].mean():>8.3f}"
            out.write(line + "\n")

            # ── ADD vs REM breakdown ─────────────────────────────────
            out.write(f"\n    by liquidity (ADD = maker, REM = taker):\n")
            hdr2 = f"    {'type':<8} {'fills':>5}"
            for mn in mo_names:
                hdr2 += f"  {mn:>8}"
            out.write(hdr2 + "\n")
            out.write("    " + "-" * (len(hdr2) - 4) + "\n")

            for liq_type in ["ADD", "REM"]:
                ldf = df[df["add_remove"] == liq_type]
                if ldf.empty:
                    continue
                line = f"    {liq_type:<8} {len(ldf):>5}"
                for mn in mo_names:
                    line += f"  {ldf[mn].mean():>8.3f}"
                out.write(line + "\n")

    out.write("\n")
    return out.getvalue()


# ── Time-of-day helpers ──────────────────────────────────────────────────────

def _bucket_time(timestamp_str):
    """Extract 30-minute bucket label from a trade timestamp.

    Input format: "20260212 18:00:46.596090679 EST" (or similar with HH:MM).
    Returns e.g. "18:00", "18:30".
    """
    parts = str(timestamp_str).split()
    if len(parts) < 2:
        return "??:??"
    hhmm = parts[1][:5]  # "HH:MM"
    try:
        hh, mm = hhmm.split(":")
        mm_bucketed = "00" if int(mm) < 30 else "30"
        return f"{hh}:{mm_bucketed}"
    except (ValueError, IndexError):
        return "??:??"


def build_tod_report(results, offsets):
    """Build a standalone time-of-day performance report from markout results."""
    out = io.StringIO()
    mo_names = [f"mo_{o}s" for o in offsets]

    out.write("=" * 90 + "\n")
    out.write("TIME-OF-DAY ANALYSIS\n")
    out.write("=" * 90 + "\n")

    # Group by (alias, group, strat)
    by_strat = defaultdict(list)
    for (alias, group, strat, ds), df in results.items():
        df = df.copy()
        df["bucket"] = df["timestamp"].apply(_bucket_time)
        by_strat[(alias, group, strat)].append(df)

    for key in sorted(by_strat):
        alias, group, strat = key
        all_df = pd.concat(by_strat[key], ignore_index=True)
        n_days = len(by_strat[key])

        out.write(f"\n  [{alias}] {group}/{strat}  ({n_days} day(s))\n\n")

        # Build header
        hdr = f"    {'bucket':<8} {'fills':>5} {'net_pnl':>10} {'pnl_bps':>8}"
        for mn in mo_names:
            hdr += f"  {mn:>8}"
        out.write(hdr + "\n")
        out.write("    " + "-" * (len(hdr) - 4) + "\n")

        # Ensure numeric columns
        for col in ["closed_pnl", "mid", "size"] + mo_names:
            if col in all_df.columns:
                all_df[col] = pd.to_numeric(all_df[col], errors="coerce").fillna(0)

        # Notional per trade for weighting
        all_df["notional"] = (all_df["mid"] * all_df["size"]).abs()

        grouped = all_df.groupby("bucket")
        rows = []
        for bucket, bdf in grouped:
            if bucket == "??:??":
                continue
            row = {"bucket": bucket, "fills": len(bdf), "net_pnl": bdf["closed_pnl"].sum()}
            total_notional = bdf["notional"].sum()
            if total_notional > 0:
                row["pnl_bps"] = (bdf["closed_pnl"].sum() / total_notional) * 10000
            else:
                row["pnl_bps"] = 0.0
            for mn in mo_names:
                if mn in bdf.columns and total_notional > 0:
                    row[mn] = (bdf[mn] * bdf["notional"]).sum() / total_notional
                else:
                    row[mn] = 0.0
            rows.append(row)

        rows.sort(key=lambda r: r["bucket"])

        for r in rows:
            line = f"    {r['bucket']:<8} {r['fills']:>5} {r['net_pnl']:>10.2f} {r['pnl_bps']:>8.2f}"
            for mn in mo_names:
                line += f"  {r[mn]:>8.3f}"
            out.write(line + "\n")

        # TOTAL row
        total_notional = all_df["notional"].sum()
        total_pnl = all_df["closed_pnl"].sum()
        total_bps = (total_pnl / total_notional * 10000) if total_notional > 0 else 0.0
        line = f"    {'TOTAL':<8} {len(all_df):>5} {total_pnl:>10.2f} {total_bps:>8.2f}"
        for mn in mo_names:
            if mn in all_df.columns and total_notional > 0:
                val = (all_df[mn] * all_df["notional"]).sum() / total_notional
            else:
                val = 0.0
            line += f"  {val:>8.3f}"
        out.write(line + "\n")

    out.write("\n")
    return out.getvalue()


def build_tod_report_section(out, local_base, data, dates_present):
    """Add compact time-of-day summary to the main report output.

    Reads pre-existing _markout.csv files only — does NOT run the binary.
    """
    aliases_in_data = sorted(data["alias"].unique())
    date_strs = sorted(dates_present)

    strats_in_data = set()
    for _, r in data.iterrows():
        strats_in_data.add((r["alias"], r["group"], r["strat"]))

    base = Path(local_base)
    if not base.exists():
        return

    # Discover existing markout files
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
                    mo_file = strat_dir / f"trades_{ds}_markout.csv"
                    if mo_file.exists():
                        found[(alias, group, strat, ds)] = mo_file

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
        df = parse_markout_file(path, offsets)
        if df is not None:
            parsed[key] = df

    if not parsed:
        return

    out.write("=" * 90 + "\n")
    out.write("TIME-OF-DAY ANALYSIS\n")
    out.write("=" * 90 + "\n")

    # Group by (alias, group, strat), concatenate all dates
    by_strat = defaultdict(list)
    for (alias, group, strat, ds), df in parsed.items():
        df = df.copy()
        df["bucket"] = df["timestamp"].apply(_bucket_time)
        by_strat[(alias, group, strat)].append(df)

    for key in sorted(by_strat):
        alias, group, strat = key
        all_df = pd.concat(by_strat[key], ignore_index=True)

        out.write(f"\n  [{alias}] {group}/{strat}\n")

        hdr = f"    {'bucket':<8} {'fills':>5} {'net_pnl':>10} {'pnl_bps':>8}"
        for mn in mo_names:
            hdr += f"  {mn:>8}"
        out.write(hdr + "\n")
        out.write("    " + "-" * (len(hdr) - 4) + "\n")

        for col in ["closed_pnl", "mid", "size"] + mo_names:
            if col in all_df.columns:
                all_df[col] = pd.to_numeric(all_df[col], errors="coerce").fillna(0)

        all_df["notional"] = (all_df["mid"] * all_df["size"]).abs()

        grouped = all_df.groupby("bucket")
        rows = []
        for bucket, bdf in grouped:
            if bucket == "??:??":
                continue
            row = {"bucket": bucket, "fills": len(bdf)}
            row["net_pnl"] = bdf["closed_pnl"].sum()
            total_notional = bdf["notional"].sum()
            if total_notional > 0:
                row["pnl_bps"] = (bdf["closed_pnl"].sum() / total_notional) * 10000
            else:
                row["pnl_bps"] = 0.0
            for mn in mo_names:
                if mn in bdf.columns and total_notional > 0:
                    row[mn] = (bdf[mn] * bdf["notional"]).sum() / total_notional
                else:
                    row[mn] = 0.0
            rows.append(row)

        rows.sort(key=lambda r: r["bucket"])

        for r in rows:
            line = f"    {r['bucket']:<8} {r['fills']:>5} {r['net_pnl']:>10.2f} {r['pnl_bps']:>8.2f}"
            for mn in mo_names:
                line += f"  {r[mn]:>8.3f}"
            out.write(line + "\n")

    out.write("\n")


# ── Report section (embedded in main report) ─────────────────────────────────

def build_report_section(out, local_base, data, dates_present):
    """Add compact markout summary to the main report output.

    Only reads pre-existing _markout.csv files — does NOT run the binary.
    Silently skipped if no markout files exist.
    """
    aliases_in_data = sorted(data["alias"].unique())
    date_strs = sorted(dates_present)

    strats_in_data = set()
    for _, r in data.iterrows():
        strats_in_data.add((r["alias"], r["group"], r["strat"]))

    base = Path(local_base)
    if not base.exists():
        return

    # Discover existing markout files
    found = {}  # (alias, group, strat, date) -> path
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
                    mo_file = strat_dir / f"trades_{ds}_markout.csv"
                    if mo_file.exists():
                        found[(alias, group, strat, ds)] = mo_file

    if not found:
        return

    # Infer offsets from the first file's header (or column count for legacy files)
    first_path = next(iter(found.values()))
    offsets = _infer_offsets_from_file(first_path)
    if not offsets:
        return
    mo_names = [f"mo_{o}s" for o in offsets]

    # Parse all found files
    parsed = {}
    for key, path in found.items():
        df = parse_markout_file(path, offsets)
        if df is not None:
            parsed[key] = df

    if not parsed:
        return

    out.write("=" * 90 + "\n")
    out.write("MARKOUT ANALYSIS\n")
    out.write("=" * 90 + "\n")

    # Group by (alias, group, strat)
    by_strat = defaultdict(dict)
    for (alias, group, strat, ds), df in parsed.items():
        by_strat[(alias, group, strat)][ds] = df

    for key in sorted(by_strat):
        alias, group, strat = key
        dates_data = by_strat[key]
        out.write(f"\n  [{alias}] {group}/{strat}\n")
        hdr = f"    {'date':<10} {'fills':>5}"
        for mn in mo_names:
            hdr += f"  {mn:>8}"
        out.write(hdr + "\n")
        out.write("    " + "-" * (len(hdr) - 4) + "\n")

        for ds in sorted(dates_data):
            df = dates_data[ds]
            line = f"    {ds:<10} {len(df):>5}"
            for mn in mo_names:
                line += f"  {df[mn].mean():>8.3f}"
            out.write(line + "\n")

    out.write("\n")
