"""Sim vs live drill-down on trade-level data.

Operates on already-produced trade files from sim-eval runs.
Supports time-window filtering and per-symbol comparison.
"""

import io
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

# Trade file columns (no header in either sim or live files)
TRADE_COLS = [
    "timestamp", "time_ms", "order_num", "sym", "market", "side",
    "price", "size", "end_pos", "closed_pnl", "commission",
    "col11", "bid", "ask", "col14", "mid", "col16",
    "col17", "col18", "col19", "add_remove", "front_flag",
]


def load_trade_file(path):
    """Load a trade CSV (no header) into a DataFrame."""
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, header=None, names=TRADE_COLS[:22])
    except Exception as e:
        print(f"  [warn] Could not load {path}: {e}", file=sys.stderr)
        return None
    if df.empty:
        return None

    # Parse timestamp to extract time-of-day for filtering
    # Format: "20260212 09:45:03.111655535 EST"
    df["time_str"] = df["timestamp"].str.extract(r"(\d{2}:\d{2}:\d{2})")
    for col in ["price", "size", "end_pos", "closed_pnl", "commission", "bid", "ask", "mid"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def filter_time_window(df, time_start, time_end):
    """Filter trades to a time window (HH:MM-HH:MM or HH:MM:SS-HH:MM:SS)."""
    if df is None or df.empty:
        return df
    return df[(df["time_str"] >= time_start) & (df["time_str"] <= time_end)].copy()


def filter_symbol(df, sym):
    """Filter trades to a specific symbol."""
    if df is None or df.empty or sym is None:
        return df
    return df[df["sym"] == sym].copy()


def compute_stats(df, label):
    """Compute summary stats for a set of trades."""
    if df is None or df.empty:
        return {"label": label, "n_fills": 0, "shs_traded": 0, "net_pnl": 0,
                "commission": 0, "n_add": 0, "n_remove": 0, "n_buy": 0,
                "n_sell": 0, "avg_price": 0, "end_pos": 0, "vwap": 0}

    buy_df = df[df["side"] == "Buy"]
    sell_df = df[df["side"] == "Sell"]
    add_df = df[df["add_remove"] == "ADD"]
    remove_df = df[df["add_remove"] == "REMOVE"]

    total_notional = (df["price"] * df["size"]).sum()
    total_size = df["size"].sum()
    vwap = total_notional / total_size if total_size > 0 else 0

    return {
        "label": label,
        "n_fills": len(df),
        "shs_traded": total_size,
        "net_pnl": df["closed_pnl"].sum(),
        "commission": df["commission"].sum(),
        "n_buy": len(buy_df),
        "n_sell": len(sell_df),
        "buy_shs": buy_df["size"].sum(),
        "sell_shs": sell_df["size"].sum(),
        "n_add": len(add_df),
        "n_remove": len(remove_df),
        "vwap": vwap,
        "end_pos": df["end_pos"].iloc[-1] if len(df) > 0 else 0,
    }


def compute_per_sym_stats(df, label):
    """Compute stats grouped by symbol."""
    if df is None or df.empty:
        return []
    results = []
    for sym, grp in df.groupby("sym"):
        stats = compute_stats(grp, label)
        stats["sym"] = sym
        results.append(stats)
    return results


def run_drill(args, config):
    """Main entry point for sim-drill command."""
    local_base = getattr(args, "local_base", None) or config["local_base"]
    sim_base = getattr(args, "sim_dir", None) or os.path.expanduser("~/scratch/simeval")
    date_str = args.date
    sym_filter = getattr(args, "sym", None)
    time_window = getattr(args, "time", None)

    # Parse time window
    time_start, time_end = None, None
    if time_window:
        parts = time_window.split("-")
        if len(parts) == 2:
            time_start = parts[0].strip()
            time_end = parts[1].strip()
            # Pad to HH:MM:SS if needed
            if len(time_start) == 5:
                time_start += ":00"
            if len(time_end) == 5:
                time_end += ":59"

    # Find matching strat directories
    strat_filter = getattr(args, "strat", None)
    group_filter = getattr(args, "group", None)

    pairs = _find_trade_file_pairs(local_base, sim_base, date_str,
                                   strat_filter, group_filter)

    if not pairs:
        print("No matching trade file pairs found.")
        return

    out = io.StringIO()
    out.write("=" * 80 + "\n")
    out.write("SIM vs LIVE DRILL-DOWN\n")
    if time_window:
        out.write(f"  Time window: {time_start} — {time_end}\n")
    if sym_filter:
        out.write(f"  Symbol: {sym_filter}\n")
    out.write(f"  Date: {date_str}\n")
    out.write("=" * 80 + "\n")

    for alias, group, strat, live_path, sim_path in pairs:
        live_df = load_trade_file(live_path)
        sim_df = load_trade_file(sim_path)

        # Apply filters
        if sym_filter:
            live_df = filter_symbol(live_df, sym_filter)
            sim_df = filter_symbol(sim_df, sym_filter)
        if time_start and time_end:
            live_df = filter_time_window(live_df, time_start, time_end)
            sim_df = filter_time_window(sim_df, time_start, time_end)

        out.write(f"\n  [{alias}] {group}/{strat}\n")
        out.write(f"  {'─' * 76}\n")

        if sym_filter:
            # Single symbol — show detailed comparison
            _write_single_sym(live_df, sim_df, out)
        else:
            # Multi symbol — show per-symbol summary table
            _write_multi_sym(live_df, sim_df, out)

    report = out.getvalue()
    print(report, end="")


def _write_single_sym(live_df, sim_df, out):
    """Detailed comparison for a single symbol."""
    live_stats = compute_stats(live_df, "LIVE")
    sim_stats = compute_stats(sim_df, "SIM")

    out.write(f"\n    {'':>18} {'LIVE':>12} {'SIM':>12} {'DIFF':>12}\n")
    out.write(f"    {'─' * 54}\n")

    rows = [
        ("fills", "n_fills", "d"),
        ("shs traded", "shs_traded", ".1f"),
        ("buy shs", "buy_shs", ".1f"),
        ("sell shs", "sell_shs", ".1f"),
        ("net PnL", "net_pnl", ".2f"),
        ("commission", "commission", ".4f"),
        ("VWAP", "vwap", ".4f"),
        ("end pos", "end_pos", ".1f"),
        ("ADD fills", "n_add", "d"),
        ("REMOVE fills", "n_remove", "d"),
    ]

    for label, key, fmt in rows:
        lv = live_stats.get(key, 0)
        sv = sim_stats.get(key, 0)
        diff = lv - sv
        out.write(f"    {label:>18} {lv:>12{fmt}} {sv:>12{fmt}} {diff:>+12{fmt}}\n")

    # Show recent trades if manageable
    for tag, df in [("LIVE", live_df), ("SIM", sim_df)]:
        if df is not None and len(df) > 0 and len(df) <= 30:
            out.write(f"\n    {tag} trades ({len(df)}):\n")
            out.write(f"    {'time':>15} {'side':>4} {'price':>10} {'size':>8} {'pos':>8} {'cpnl':>8} {'type':>6}\n")
            out.write(f"    {'─' * 60}\n")
            for _, t in df.iterrows():
                sym_short = str(t["sym"])
                if sym_short.startswith("xyz:"):
                    sym_short = sym_short[4:]
                out.write(f"    {t['time_str']:>15} {t['side']:>4} "
                          f"{t['price']:>10.2f} {t['size']:>8.3f} "
                          f"{t['end_pos']:>8.3f} {t['closed_pnl']:>8.4f} "
                          f"{t['add_remove']:>6}\n")
        elif df is not None and len(df) > 30:
            out.write(f"\n    {tag}: {len(df)} trades (too many to list, showing stats only)\n")

    out.write("\n")


def _write_multi_sym(live_df, sim_df, out):
    """Per-symbol summary table."""
    live_by_sym = compute_per_sym_stats(live_df, "LIVE")
    sim_by_sym = compute_per_sym_stats(sim_df, "SIM")

    live_map = {s["sym"]: s for s in live_by_sym}
    sim_map = {s["sym"]: s for s in sim_by_sym}
    all_syms = sorted(set(list(live_map.keys()) + list(sim_map.keys())))

    if not all_syms:
        out.write("    No trades found.\n")
        return

    out.write(f"\n    {'sym':<12} {'L_fills':>7} {'S_fills':>7} {'L_shs':>8} {'S_shs':>8} {'L_pnl':>9} {'S_pnl':>9} {'diff':>9}\n")
    out.write(f"    {'─' * 70}\n")

    total_live_pnl = 0
    total_sim_pnl = 0
    for sym in all_syms:
        ls = live_map.get(sym, {})
        ss = sim_map.get(sym, {})
        l_pnl = ls.get("net_pnl", 0)
        s_pnl = ss.get("net_pnl", 0)
        total_live_pnl += l_pnl
        total_sim_pnl += s_pnl
        sym_short = sym[4:] if sym.startswith("xyz:") else sym

        out.write(f"    {sym_short:<12} "
                  f"{ls.get('n_fills', 0):>7} {ss.get('n_fills', 0):>7} "
                  f"{ls.get('shs_traded', 0):>8.1f} {ss.get('shs_traded', 0):>8.1f} "
                  f"{l_pnl:>9.2f} {s_pnl:>9.2f} "
                  f"{l_pnl - s_pnl:>+9.2f}\n")

    diff_total = total_live_pnl - total_sim_pnl
    out.write(f"    {'TOTAL':<12} {'':<7} {'':<7} {'':<8} {'':<8} "
              f"{total_live_pnl:>9.2f} {total_sim_pnl:>9.2f} {diff_total:>+9.2f}\n")
    out.write("\n")


def _find_trade_file_pairs(local_base, sim_base, date_str, strat_filter, group_filter):
    """Find matching pairs of live trade files and sim trade files."""
    pairs = []
    base = Path(local_base)
    if not base.exists():
        return pairs

    for alias_dir in sorted(base.iterdir()):
        if not alias_dir.is_dir():
            continue
        alias = alias_dir.name
        for group_dir in sorted(alias_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            if group_filter and group_filter.lower() not in group.lower():
                continue
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name
                if strat_filter and strat_filter.lower() not in strat.lower():
                    continue

                from sim_eval import _find_live_file
                live_path = _find_live_file(
                    local_base, alias, group, strat, "trades", date_str
                )
                sim_path = _find_sim_trade_path(sim_base, alias, group, strat, date_str)

                if (live_path and Path(live_path).exists()) or (sim_path and Path(sim_path).exists()):
                    pairs.append((alias, group, strat, str(live_path) if live_path else "", str(sim_path) if sim_path else ""))

    return pairs


def _find_sim_trade_path(sim_base, alias, group, strat, live_date):
    """Locate the sim trade file, accounting for live→sim date offsets.

    Files are written as trd_simeval_<live_date>_<sim_date>.csv.  When sim and
    live dates match there will be an exact *_{live_date}.csv file; otherwise we
    need to glob for any matching prefix.
    """
    base = Path(sim_base) / alias / group / strat
    if not base.exists():
        return None

    exact = base / f"trd_simeval_{live_date}_{live_date}.csv"
    if exact.exists():
        return str(exact)

    matches = sorted(base.glob(f"trd_simeval_{live_date}_*.csv"))
    if not matches:
        return None

    if len(matches) > 1:
        print(f"  [warn] Multiple sim files for {alias}/{group}/{strat} {live_date}; using {matches[-1].name}")
    return str(matches[-1])
