"""
Reusable fill diagnostic analysis library.

Load fill_diag CSVs from sim output, compute derived features,
and produce summary stats, binned markouts, cross-tabs, and per-day views.
"""

import os
import glob
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Markout horizon labels matching C++ kHorizonMs = {1000, 5000, 30000, 60000, 300000}
HORIZONS = ["1s", "5s", "30s", "60s", "300s"]
LOCAL_HORIZON_COLS = [f"local_mid_{h}" for h in HORIZONS]
REMOTE_HORIZON_COLS = [f"remote_mid_{h}" for h in HORIZONS]
# Primary markout uses local (Hyperliquid) mid — the actual market where fills happen.
HORIZON_COLS = LOCAL_HORIZON_COLS

# Default features to bin by in univariate analysis.
# Entries can be a string (no filter) or a tuple (feature, filter_side)
# where filter_side=1 for buy fills only, -1 for sell fills only.
DEFAULT_BIN_FEATURES = [
    "vol_ems_remote_100ms", "vol_ems_remote_1s", "vol_ems_remote_4s",
    "vol_ems_remote_30s",
    "adj_momentum_100ms", "adj_momentum_1s", "adj_momentum_4s", "adj_momentum_10s", "adj_momentum_30s",
    "rel_spread_ema", "adj_premium",
    "adj_trailing_ret_30s", "adj_trailing_ret_60s", "adj_trailing_ret_300s",
    "trade_rate_ems", "trade_notional_abs_ems", "adj_trade_notional_ems",
    "large_trade_frac", "trade_depth_ems",
    ("buy_trade_depth_ems", 1), ("sell_trade_depth_ems", -1),
    ("liq_depth_buy_100k", 1), ("liq_depth_buy_250k", 1),
    ("liq_depth_sell_100k", -1), ("liq_depth_sell_250k", -1),
    ("liq_depth_buy_100k_ems", 1), ("liq_depth_buy_250k_ems", 1),
    ("liq_depth_sell_100k_ems", -1), ("liq_depth_sell_250k_ems", -1),
    "adj_book_depth_ratio", "adj_deep_book_imbalance",
    "book_imbalance",
    "fill_edge_bps", "time_since_fill_ms", "vol_accel",
    "hour_et", "side", "add_liq",
]

# Default feature pairs for cross-tab analysis.
# Each pair produces a 2D grid (terciles x terciles) of markout medians.
DEFAULT_CROSS_PAIRS = [
    # Vol x directional
    ("vol_ems_remote_30s", "adj_trailing_ret_30s"),
    ("vol_ems_remote_4s", "adj_momentum_4s"),
    ("vol_ems_remote_30s", "adj_momentum_30s"),
    # Vol x microstructure
    ("vol_ems_remote_30s", "trade_rate_ems"),
    ("vol_ems_remote_30s", "adj_deep_book_imbalance"),
    # Momentum x spread/imbalance
    ("adj_momentum_4s", "rel_spread_ema"),
    ("adj_momentum_4s", "adj_deep_book_imbalance"),
    # Trade activity
    ("trade_rate_ems", "large_trade_frac"),
    ("trade_rate_ems", "trade_depth_ems"),
    # Imbalance interactions
    ("adj_deep_book_imbalance", "book_imbalance"),
]

ET = ZoneInfo("America/New_York")


def load_fill_diags(workdir):
    """Load all fill_diag_*.csv from workdir, concatenate, add derived columns.

    Returns (df, info) where info is a dict with metadata like N per horizon.
    """
    pattern = os.path.join(workdir, "fill_diag_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No fill_diag CSVs found in {workdir}")

    dfs = []
    skipped = 0
    for f in files:
        if os.path.getsize(f) <= 1:
            skipped += 1
            continue
        try:
            d = pd.read_csv(f)
        except pd.errors.EmptyDataError:
            skipped += 1
            continue
        if len(d) == 0:
            skipped += 1
            continue
        date_str = os.path.basename(f).replace("fill_diag_", "").replace(".csv", "")
        d["date"] = date_str
        dfs.append(d)

    if not dfs:
        raise ValueError(f"All {len(files)} CSVs were empty")

    df = pd.concat(dfs, ignore_index=True)

    # --- Derived columns ---

    # Primary markout uses local (Hyperliquid) mid at horizon.
    for h, col in zip(HORIZONS, LOCAL_HORIZON_COLS):
        mid_h = pd.to_numeric(df[col], errors="coerce")
        valid = (mid_h > 0) & (df["fill_px"] > 0)
        df[f"markout_{h}_bps"] = np.nan
        df.loc[valid, f"markout_{h}_bps"] = (
            df.loc[valid, "side"] * (mid_h[valid] - df.loc[valid, "fill_px"])
            / df.loc[valid, "fill_px"] * 10000
        )
        # Mid-move: local mid at horizon vs local mid at fill time.
        valid_mid = (mid_h > 0) & (df["local_mid"] > 0)
        df[f"midmove_{h}_bps"] = np.nan
        df.loc[valid_mid, f"midmove_{h}_bps"] = (
            df.loc[valid_mid, "side"]
            * (mid_h[valid_mid] - df.loc[valid_mid, "local_mid"])
            / df.loc[valid_mid, "local_mid"] * 10000
        )

    # Secondary markout using remote (oracle) mid for comparison.
    for h, col in zip(HORIZONS, REMOTE_HORIZON_COLS):
        mid_h = pd.to_numeric(df[col], errors="coerce")
        valid = (mid_h > 0) & (df["fill_px"] > 0)
        df[f"remote_markout_{h}_bps"] = np.nan
        df.loc[valid, f"remote_markout_{h}_bps"] = (
            df.loc[valid, "side"] * (mid_h[valid] - df.loc[valid, "fill_px"])
            / df.loc[valid, "fill_px"] * 10000
        )

    # Relative spread features
    safe_mid = df["local_mid"].replace(0, np.nan)
    df["rel_spread"] = df["local_spread"] / safe_mid
    df["rel_spread_ema"] = df["avg_local_spread"] / safe_mid

    # Premium
    safe_remote = df["remote_mid"].replace(0, np.nan)
    df["premium"] = (df["local_mid"] - df["remote_mid"]) / safe_remote

    # Side-adjusted premium: -side * premium. Positive = trading with the premium (favorable).
    # e.g. selling when local > remote, or buying when local < remote.
    df["adj_premium"] = -df["side"] * df["premium"]

    # Absolute position
    df["abs_position"] = df["position_after"].abs()

    # Hour in ET
    df["hour_et"] = np.nan
    valid_t = df["time_ms"] > 0
    if valid_t.any():
        ts = pd.to_datetime(df.loc[valid_t, "time_ms"], unit="ms", utc=True)
        df.loc[valid_t, "hour_et"] = ts.dt.tz_convert(ET).dt.hour

    # Book imbalance: side * (bb - ba) / (bb + ba)
    bb = df.get("local_bb_size", pd.Series(dtype=float))
    ba = df.get("local_ba_size", pd.Series(dtype=float))
    denom = bb + ba
    denom = denom.replace(0, np.nan)
    df["book_imbalance"] = df["side"] * (bb - ba) / denom

    # Trade microstructure derived features.
    if "trade_rate_ems" in df.columns and "large_trade_rate_ems" in df.columns:
        safe_rate = df["trade_rate_ems"].replace(0, np.nan)
        df["large_trade_frac"] = df["large_trade_rate_ems"] / safe_rate
    if "book_depth_bid" in df.columns and "book_depth_ask" in df.columns:
        depth_sum = df["book_depth_bid"] + df["book_depth_ask"]
        depth_sum = depth_sum.replace(0, np.nan)
        df["book_depth_ratio"] = (df["book_depth_bid"] - df["book_depth_ask"]) / depth_sum

    # Side-adjusted book depth ratio: side * book_depth_ratio.
    # Positive = more near-touch liquidity (5 ticks) on our fill side (favorable).
    if "book_depth_ratio" in df.columns:
        df["adj_book_depth_ratio"] = df["side"] * df["book_depth_ratio"]

    # Deep book imbalance: (bid_notional - ask_notional) / (bid + ask) within 50bps of mid.
    if "deep_bid_notional" in df.columns and "deep_ask_notional" in df.columns:
        deep_sum = df["deep_bid_notional"] + df["deep_ask_notional"]
        deep_sum = deep_sum.replace(0, np.nan)
        df["deep_book_imbalance"] = (df["deep_bid_notional"] - df["deep_ask_notional"]) / deep_sum

    # Side-adjusted deep book imbalance: side * deep_book_imbalance.
    # Positive = more deep liquidity on our fill side (favorable: support/resistance behind us).
    if "deep_book_imbalance" in df.columns:
        df["adj_deep_book_imbalance"] = df["side"] * df["deep_book_imbalance"]

    # Side-adjusted momentum: side * momentum. Positive = momentum in fill direction.
    for tdc in ["100ms", "1s", "4s", "10s", "30s"]:
        raw = f"momentum_{tdc}"
        if raw in df.columns:
            df[f"adj_{raw}"] = df["side"] * df[raw]

    # Side-adjusted trailing returns: side * trailing_ret. Positive = recent move in fill direction.
    for lb in ["30s", "60s", "300s"]:
        raw = f"trailing_ret_{lb}"
        if raw in df.columns:
            df[f"adj_{raw}"] = df["side"] * df[raw]

    # Side-adjusted trade notional: side * trade_notional_signed_ems.
    # Positive = net notional flow in our fill direction (favorable if flow is informed/persistent).
    if "trade_notional_signed_ems" in df.columns:
        df["adj_trade_notional_ems"] = df["side"] * df["trade_notional_signed_ems"]

    # Fill edge at fill time: how much theoretical edge vs oracle at moment of fill (bps).
    # Positive = bought below oracle or sold above oracle.
    safe_fill = df["fill_px"].replace(0, np.nan)
    df["fill_edge_bps"] = df["side"] * (df["remote_mid"] - df["fill_px"]) / safe_fill * 10000

    # Time since last fill (ms), computed per day to avoid cross-day artifacts.
    df["time_since_fill_ms"] = np.nan
    for _, day_idx in df.groupby("date").groups.items():
        day_times = df.loc[day_idx, "time_ms"].sort_values()
        df.loc[day_times.index, "time_since_fill_ms"] = day_times.diff()

    # Vol acceleration: short-term vol relative to longer-term vol.
    # > 1 means vol is spiking (recent burst), < 1 means vol is subsiding.
    if "vol_ems_remote_4s" in df.columns and "vol_ems_remote_30s" in df.columns:
        safe_vol30 = df["vol_ems_remote_30s"].replace(0, np.nan)
        df["vol_accel"] = df["vol_ems_remote_4s"] / safe_vol30

    # Track valid N per horizon
    horizon_n = {}
    for h in HORIZONS:
        horizon_n[h] = int(df[f"markout_{h}_bps"].notna().sum())

    info = {
        "n_fills": len(df),
        "n_days": df["date"].nunique(),
        "n_files": len(files),
        "n_skipped": skipped,
        "horizon_n": horizon_n,
    }

    return df, info


def summary_stats(df):
    """Compute overall symbol behavior summary.

    Returns dict with sections: markouts, spreads, vol, activity.
    """
    result = {}

    # --- Markout table ---
    markout_rows = []
    for h in HORIZONS:
        col = f"markout_{h}_bps"
        v = df[col].dropna()
        if len(v) == 0:
            continue
        markout_rows.append({
            "horizon": h,
            "mean": float(v.mean()),
            "median": float(v.median()),
            "std": float(v.std()),
            "pct_pos": float((v > 0).mean() * 100),
            "N": int(len(v)),
        })
    result["markouts"] = markout_rows

    # --- Spread distribution ---
    spread_stats = {}
    for feat in ["rel_spread", "rel_spread_ema"]:
        v = df[feat].dropna()
        if len(v) == 0:
            continue
        spread_stats[feat] = {
            "p10": float(v.quantile(0.10)),
            "p25": float(v.quantile(0.25)),
            "p50": float(v.quantile(0.50)),
            "p75": float(v.quantile(0.75)),
            "p90": float(v.quantile(0.90)),
            "N": int(len(v)),
        }
    result["spreads"] = spread_stats

    # --- Vol distribution ---
    vol_stats = {}
    vol_feats = ["vol_ems_remote", "vol_ems_remote_10s", "vol_ems_remote_30s",
                 "vol_ems_remote_60s", "vol_ems_remote_300s"]
    for feat in vol_feats:
        if feat not in df.columns:
            continue
        v = df[feat].dropna()
        v = v[v > 0]
        if len(v) == 0:
            continue
        vol_stats[feat] = {
            "p10": float(v.quantile(0.10)),
            "p25": float(v.quantile(0.25)),
            "p50": float(v.quantile(0.50)),
            "p75": float(v.quantile(0.75)),
            "p90": float(v.quantile(0.90)),
            "N": int(len(v)),
        }
    result["vol"] = vol_stats

    # --- Activity ---
    n_days = df["date"].nunique()
    fills_per_day = len(df) / max(n_days, 1)

    hour_counts = df["hour_et"].dropna().value_counts().sort_index()
    fills_per_hour = {int(k): int(v) for k, v in hour_counts.items()}

    n_buy = int((df["side"] == 1).sum())
    n_sell = int((df["side"] == -1).sum())
    n_passive = int((df["add_liq"] == 1).sum()) if "add_liq" in df.columns else 0
    n_aggressive = int((df["add_liq"] == 0).sum()) if "add_liq" in df.columns else 0

    result["activity"] = {
        "total_fills": int(len(df)),
        "n_days": int(n_days),
        "fills_per_day": float(fills_per_day),
        "fills_per_hour": fills_per_hour,
        "n_buy": n_buy,
        "n_sell": n_sell,
        "side_ratio": float(n_buy / max(n_buy + n_sell, 1)),
        "n_passive": n_passive,
        "n_aggressive": n_aggressive,
        "passive_frac": float(n_passive / max(n_passive + n_aggressive, 1)),
    }

    return result


def bin_markouts(df, feature, horizon="30s", n_bins=4, method="quantile", filter_side=None):
    """Univariate binning of markouts by a single feature.

    Args:
        filter_side: If set to 1 or -1, only include fills on that side.
            Used for features that only make sense for one side (e.g. buy_trade_depth_ems
            should only be analyzed against buy fills).

    Returns list of dicts, one per bin, with: bin_label, lo, hi, count, mean, median, std, pct_pos.
    Returns None if feature is unsuitable (missing, <2 distinct values).
    """
    markout_col = f"markout_{horizon}_bps"
    if markout_col not in df.columns or feature not in df.columns:
        return None

    valid = df[feature].notna() & df[markout_col].notna()
    if filter_side is not None:
        valid = valid & (df["side"] == filter_side)
    sub = df.loc[valid, [feature, markout_col]].copy()

    if len(sub) < n_bins * 2:
        return None
    if sub[feature].nunique() < 2:
        return None

    # For binary features (side, add_liq), use raw values as bins
    if sub[feature].nunique() <= 3:
        sub["bin"] = sub[feature]
        groups = sub.groupby("bin", observed=True)
    elif method == "quantile":
        try:
            sub["bin"] = pd.qcut(sub[feature], n_bins, duplicates="drop")
        except ValueError:
            return None
        groups = sub.groupby("bin", observed=True)
    else:
        try:
            sub["bin"] = pd.cut(sub[feature], n_bins)
        except ValueError:
            return None
        groups = sub.groupby("bin", observed=True)

    rows = []
    for label, g in groups:
        v = g[markout_col]
        if isinstance(label, pd.Interval):
            lo, hi = float(label.left), float(label.right)
            # Round to 6 significant figures to avoid floating point artifacts.
            lo = float(f"{lo:.6g}")
            hi = float(f"{hi:.6g}")
            bin_label = f"[{lo:.4g}, {hi:.4g})"
        else:
            lo = hi = float(label)
            bin_label = str(label)

        rows.append({
            "bin_label": bin_label,
            "lo": lo,
            "hi": hi,
            "count": int(len(v)),
            "mean": float(v.mean()),
            "median": float(v.median()),
            "std": float(v.std()) if len(v) > 1 else 0.0,
            "pct_pos": float((v > 0).mean() * 100),
        })

    return rows


def _fmt_interval(label):
    """Format a pandas Interval or scalar as a clean bin label, avoiding float artifacts."""
    if isinstance(label, pd.Interval):
        lo = float(f"{float(label.left):.6g}")
        hi = float(f"{float(label.right):.6g}")
        return f"({lo:.4g}, {hi:.4g}]"
    return str(label)


def cross_tab_markouts(df, f1, f2, horizon="30s", n_bins=3):
    """2D binning: tercile grid of (f1, f2) → markout stats.

    Returns dict with keys: f1, f2, horizon, cells (list of dicts).
    Returns None if insufficient data.
    """
    markout_col = f"markout_{horizon}_bps"
    needed = [f1, f2, markout_col]
    for c in needed:
        if c not in df.columns:
            return None

    valid = df[f1].notna() & df[f2].notna() & df[markout_col].notna()
    sub = df.loc[valid, needed].copy()
    if len(sub) < n_bins * n_bins * 5:
        return None

    for feat, name in [(f1, "b1"), (f2, "b2")]:
        if sub[feat].nunique() < 2:
            return None
        try:
            sub[name] = pd.qcut(sub[feat], n_bins, duplicates="drop")
        except ValueError:
            return None

    groups = sub.groupby(["b1", "b2"], observed=True)
    cells = []
    for (l1, l2), g in groups:
        v = g[markout_col]
        cells.append({
            "f1_bin": _fmt_interval(l1),
            "f2_bin": _fmt_interval(l2),
            "count": int(len(v)),
            "mean": float(v.mean()),
            "median": float(v.median()),
            "pct_pos": float((v > 0).mean() * 100),
        })

    return {"f1": f1, "f2": f2, "horizon": horizon, "cells": cells}


def per_day_stats(df, horizon="30s"):
    """Per-day breakdown of fills and markouts.

    Returns list of dicts sorted by date.
    """
    markout_col = f"markout_{horizon}_bps"
    rows = []
    for date in sorted(df["date"].unique()):
        day = df[df["date"] == date]
        v = day[markout_col].dropna()
        row = {
            "date": date,
            "n_fills": int(len(day)),
        }
        if len(v) > 0:
            row["markout_mean"] = float(v.mean())
            row["markout_median"] = float(v.median())
            row["pct_pos"] = float((v > 0).mean() * 100)
            row["worst"] = float(v.min())
            # Approximate PnL: sum of markouts is directionally useful
            row["markout_sum"] = float(v.sum())
        else:
            row["markout_mean"] = None
            row["markout_median"] = None
            row["pct_pos"] = None
            row["worst"] = None
            row["markout_sum"] = None

        if "vol_ems_remote" in day.columns:
            vol = day["vol_ems_remote"]
            vol = vol[(vol > 0) & (vol < 1)]  # Filter startup garbage (raw EMS > 1 is unrealistic)
            row["avg_vol_ems"] = float(vol.mean()) if len(vol) > 0 else None
        rows.append(row)
    return rows


def premium_analysis(df, horizon="30s"):
    """Analyze premium impact on fill quality.

    Returns dict with:
      - premium_stats: overall premium distribution
      - side_x_premium: 2x2 table of side × premium_sign → markout
      - by_timeblock: premium stats by 3-hour ET block
      - ema_magnitude: markout when |EMA premium| is high vs low, per side
    """
    markout_col = f"markout_{horizon}_bps"
    result = {"horizon": horizon}

    # Premium distribution
    prem = df["premium"].dropna() * 10000  # to bps
    if len(prem) == 0:
        return None
    result["premium_stats"] = {
        "mean_bps": float(prem.mean()),
        "median_bps": float(prem.median()),
        "std_bps": float(prem.std()),
        "pct_positive": float((prem > 0).mean() * 100),
    }

    # Side × premium sign → markout
    valid = df[markout_col].notna() & df["premium"].notna()
    sub = df.loc[valid].copy()
    prem_bps = sub["premium"] * 10000

    cells = []
    for side_val, side_label in [(1, "buy"), (-1, "sell")]:
        for prem_positive in [False, True]:
            prem_label = "prem>0" if prem_positive else "prem<0"
            if prem_positive:
                mask = (sub["side"] == side_val) & (prem_bps > 0)
            else:
                mask = (sub["side"] == side_val) & (prem_bps < 0)

            bucket = sub.loc[mask, markout_col]
            if len(bucket) > 0:
                cells.append({
                    "side": side_label,
                    "premium_sign": prem_label,
                    "count": int(len(bucket)),
                    "mean_markout": float(bucket.mean()),
                    "median_markout": float(bucket.median()),
                    "pct_pos": float((bucket > 0).mean() * 100),
                })
    result["side_x_premium"] = cells

    # Premium by 3-hour ET blocks
    blocks = []
    block_labels = {
        18: "18-21 (open)", 21: "21-00", 0: "00-03 (Asia)", 3: "03-06",
        6: "06-09 (EU)", 9: "09-12 (US open)", 12: "12-15", 15: "15-18 (close)",
    }
    block_order = [18, 21, 0, 3, 6, 9, 12, 15]

    valid_h = df["hour_et"].notna() & df["premium"].notna()
    hsub = df.loc[valid_h].copy()
    hsub["block"] = (hsub["hour_et"].astype(int) // 3) * 3

    for b in block_order:
        bdata = hsub[hsub["block"] == b]
        if len(bdata) == 0:
            continue
        p = bdata["premium"] * 10000
        blocks.append({
            "block": int(b),
            "label": block_labels.get(b, f"{b:02d}-{b+3:02d}"),
            "count": int(len(bdata)),
            "mean_prem_bps": float(p.mean()),
            "std_prem_bps": float(p.std()),
            "pct_positive": float((p > 0).mean() * 100),
        })
    result["by_timeblock"] = blocks

    # EMA magnitude impact: per side, split by signed EMA into halves
    if "premium_ema" in df.columns:
        valid_ema = (df[markout_col].notna() & df["premium_ema"].notna()
                     & (df["remote_mid"] > 0))
        esub = df.loc[valid_ema].copy()
        esub["ema_prem_bps"] = esub["premium_ema"] / esub["remote_mid"] * 10000
        # Filter startup garbage (EMA not yet initialized)
        esub = esub[esub["ema_prem_bps"].abs() < 1000]

        ema_result = []
        for side_val, side_label in [(1, "buy"), (-1, "sell")]:
            side_data = esub[esub["side"] == side_val].copy()
            if len(side_data) < 8:
                continue
            median_ema = side_data["ema_prem_bps"].median()
            lo = side_data[side_data["ema_prem_bps"] <= median_ema]
            hi = side_data[side_data["ema_prem_bps"] > median_ema]

            if len(lo) > 0 and len(hi) > 0:
                ema_result.append({
                    "side": side_label,
                    "lo_avg_ema_bps": float(lo["ema_prem_bps"].mean()),
                    "lo_count": int(len(lo)),
                    "lo_median_markout": float(lo[markout_col].median()),
                    "lo_pct_pos": float((lo[markout_col] > 0).mean() * 100),
                    "hi_avg_ema_bps": float(hi["ema_prem_bps"].mean()),
                    "hi_count": int(len(hi)),
                    "hi_median_markout": float(hi[markout_col].median()),
                    "hi_pct_pos": float((hi[markout_col] > 0).mean() * 100),
                })
        result["ema_magnitude"] = ema_result

    return result


def markout_term_structure(markout_rows):
    """Classify markout shape across horizons.

    Takes markout_rows from summary_stats and returns dict with:
      - shape: GROWING / FADING / STABLE / NEGATIVE / REVERTING
      - peak_horizon: horizon with highest median markout
      - details: per-horizon median and delta from previous
    """
    if not markout_rows or len(markout_rows) < 2:
        return None

    horizon_order = ["1s", "5s", "30s", "60s", "300s"]
    by_h = {m["horizon"]: m["median"] for m in markout_rows}
    available = [h for h in horizon_order if h in by_h]
    if len(available) < 2:
        return None

    vals = [by_h[h] for h in available]
    peak_idx = vals.index(max(vals))
    trough_idx = vals.index(min(vals))
    peak_horizon = available[peak_idx]

    first, last, peak = vals[0], vals[-1], vals[peak_idx]

    # Classify
    if all(v < 0 for v in vals):
        shape = "NEGATIVE"
    elif abs(last - first) < 1.0:
        shape = "STABLE"
    elif 0 < peak_idx < len(vals) - 1 and peak > first + 1 and peak > last + 1:
        shape = "REVERTING"
    elif last > first + 1:
        shape = "GROWING"
    elif first > last + 1:
        shape = "FADING"
    else:
        shape = "STABLE"

    details = []
    for i, h in enumerate(available):
        d = {"horizon": h, "median": by_h[h]}
        if i > 0:
            d["delta"] = by_h[h] - by_h[available[i - 1]]
        details.append(d)

    return {
        "shape": shape,
        "peak_horizon": peak_horizon,
        "peak_median": float(peak),
        "details": details,
    }


# ---- Formatting helpers (terminal output) ----

def fmt_table(headers, rows, col_widths=None):
    """Format a simple aligned table. Returns list of strings."""
    if col_widths is None:
        col_widths = []
        for i, h in enumerate(headers):
            w = len(h)
            for r in rows:
                val = r[i] if i < len(r) else ""
                w = max(w, len(str(val)))
            col_widths.append(w + 2)

    lines = []
    header_line = "".join(str(h).rjust(w) for h, w in zip(headers, col_widths))
    lines.append(header_line)
    for r in rows:
        line = "".join(str(v).rjust(w) for v, w in zip(r, col_widths))
        lines.append(line)
    return lines


def print_summary(stats):
    """Print formatted summary_stats output to stdout."""
    # Markouts
    print("\n=== MARKOUT SUMMARY (vs fill price, bps) ===")
    headers = ["horizon", "mean", "median", "std", "pct+", "N"]
    rows = []
    for m in stats["markouts"]:
        rows.append([
            m["horizon"],
            f"{m['mean']:+.1f}",
            f"{m['median']:+.1f}",
            f"{m['std']:.1f}",
            f"{m['pct_pos']:.1f}%",
            str(m["N"]),
        ])
    for line in fmt_table(headers, rows):
        print("  " + line)

    # Spreads
    print("\n=== SPREAD DISTRIBUTION ===")
    for feat, s in stats.get("spreads", {}).items():
        vals = f"p10={s['p10']*1e4:.1f}  p25={s['p25']*1e4:.1f}  p50={s['p50']*1e4:.1f}  p75={s['p75']*1e4:.1f}  p90={s['p90']*1e4:.1f} bps  (N={s['N']})"
        print(f"  {feat}: {vals}")

    # Vol
    print("\n=== VOL EMS DISTRIBUTION ===")
    for feat, s in stats.get("vol", {}).items():
        vals = f"p10={s['p10']:.5f}  p25={s['p25']:.5f}  p50={s['p50']:.5f}  p75={s['p75']:.5f}  p90={s['p90']:.5f}  (N={s['N']})"
        print(f"  {feat}: {vals}")

    # Activity
    act = stats.get("activity", {})
    print("\n=== ACTIVITY ===")
    print(f"  Total fills: {act.get('total_fills', 0)}  over {act.get('n_days', 0)} days  ({act.get('fills_per_day', 0):.0f}/day)")
    print(f"  Buy/Sell: {act.get('n_buy', 0)}/{act.get('n_sell', 0)}  (buy frac: {act.get('side_ratio', 0):.1%})")
    print(f"  Passive/Aggressive: {act.get('n_passive', 0)}/{act.get('n_aggressive', 0)}  (passive frac: {act.get('passive_frac', 0):.1%})")


def print_term_structure(ts):
    """Print markout term structure analysis."""
    if not ts:
        return
    print(f"\n=== MARKOUT TERM STRUCTURE ===")
    print(f"  Shape: {ts['shape']}  (peak at {ts['peak_horizon']}: {ts['peak_median']:+.1f} bps)")
    for d in ts["details"]:
        delta = f"  (Δ {d['delta']:+.1f})" if "delta" in d else ""
        print(f"  {d['horizon']:>5}: {d['median']:>+6.1f} bps{delta}")


def bin_takeaway(feature, bins):
    """Generate a one-line takeaway from binned markout data."""
    if not bins or len(bins) < 2:
        return None

    medians = [b["median"] for b in bins]
    best_i = max(range(len(medians)), key=lambda i: medians[i])
    worst_i = min(range(len(medians)), key=lambda i: medians[i])
    spread = medians[best_i] - medians[worst_i]

    if abs(spread) < 3:
        return f"  -> Weak signal: only {spread:.1f} bps spread between best/worst bin."

    # Special case: binary/ternary feature (side, add_liq).
    if len(bins) <= 3:
        labels = [b["bin_label"] for b in bins]
        return (f"  -> {labels[best_i]} fills are {spread:.0f} bps better than "
                f"{labels[worst_i]} fills.")

    n = len(bins)
    best_label = f"Bin {best_i+1}/{n}"
    worst_label = f"Bin {worst_i+1}/{n}"

    # Check trend: compare top-third avg vs bottom-third avg.
    third = max(n // 3, 1)
    top_avg = sum(medians[-third:]) / third
    bot_avg = sum(medians[:third]) / third

    if top_avg > bot_avg + 1:
        return (f"  -> Higher {feature} = better fills: {best_label} ({medians[best_i]:+.1f}) "
                f"vs {worst_label} ({medians[worst_i]:+.1f}). Spread: {spread:.0f} bps.")
    elif bot_avg > top_avg + 1:
        return (f"  -> Lower {feature} = better fills: {best_label} ({medians[best_i]:+.1f}) "
                f"vs {worst_label} ({medians[worst_i]:+.1f}). Spread: {spread:.0f} bps.")
    else:
        return (f"  -> {spread:.0f} bps spread: {best_label} ({medians[best_i]:+.1f}) vs "
                f"{worst_label} ({medians[worst_i]:+.1f}). Non-monotonic.")


def cross_tab_takeaway(ct):
    """Generate a takeaway from 2D cross-tab data."""
    if not ct or not ct.get("cells"):
        return None
    cells = ct["cells"]
    best = max(cells, key=lambda c: c["median"])
    worst = min(cells, key=lambda c: c["median"])
    spread = best["median"] - worst["median"]
    if abs(spread) < 5:
        return f"  -> Weak interaction: only {spread:.1f} bps spread."
    return (f"  -> Best: {ct['f1']}={best['f1_bin']}, {ct['f2']}={best['f2_bin']} "
            f"({best['median']:+.1f} bps). "
            f"Worst: {ct['f1']}={worst['f1_bin']}, {ct['f2']}={worst['f2_bin']} "
            f"({worst['median']:+.1f} bps). "
            f"Spread: {spread:.0f} bps.")


# Human-readable descriptions of each bin feature.
FEATURE_DESCRIPTIONS = {
    "vol_ems_remote_100ms": "EMS of |remote return|, 100ms TDC. Ultra-short-term volatility (sub-second).",
    "vol_ems_remote_1s": "EMS of |remote return|, 1s TDC. Very short-term volatility.",
    "vol_ems_remote_4s": "EMS of |remote return|, 4s TDC. Short-term volatility.",
    "vol_ems_remote_30s": "EMS of |remote return|, 30s TDC. Measures recent oracle price volatility.",
    "adj_momentum_100ms": "side * momentum EMS, 100ms TDC. Positive = oracle trending in fill direction (favorable if trend continues).",
    "adj_momentum_1s": "side * momentum EMS, 1s TDC. Positive = oracle trending in fill direction (favorable).",
    "adj_momentum_4s": "side * momentum EMS, 4s TDC. Positive = oracle trending in fill direction (favorable).",
    "adj_momentum_10s": "side * momentum EMS, 10s TDC. Positive = oracle trending in fill direction (favorable).",
    "adj_momentum_30s": "side * momentum EMS, 30s TDC. Positive = oracle trending in fill direction (favorable).",
    "rel_spread_ema": "EMA of local bid-ask spread / mid, 60s TDC. Wide = illiquid.",
    "premium": "(local_mid - remote_mid) / remote_mid at fill time. Not smoothed. Positive = local > oracle.",
    "adj_premium": "-side * premium. Positive = trading with the premium (selling when local > oracle, buying when local < oracle). Favorable for passive MM.",
    "adj_trailing_ret_30s": "side * trailing remote return, 30s lookback. Positive = oracle moved in fill direction (favorable if trend continues).",
    "adj_trailing_ret_60s": "side * trailing remote return, 60s lookback. Positive = oracle moved in fill direction (favorable).",
    "adj_trailing_ret_300s": "side * trailing remote return, 5min lookback. Positive = oracle moved in fill direction (favorable).",
    "trade_rate_ems": "EMS count of local trades, 30s TDC. High = busy market.",
    "trade_notional_abs_ems": "EMS of |trade_px * trade_qty|, 30s TDC. Total dollar volume regardless of direction.",
    "adj_trade_notional_ems": "side * EMS(signed trade notional). Positive = net dollar flow in fill direction (favorable if flow is informed).",
    "large_trade_frac": "large_trade_rate_ems / trade_rate_ems. Fraction of trades >2x avg size. 30s TDC.",
    "trade_depth_ems": "EMS of (trade_px - best) / mid on local trades, 30s TDC. High = aggressive sweeping.",
    "buy_trade_depth_ems": "EMS of sweep depth for buy-side trades only (aggressor buying, lifting asks). Analyzed against buy fills only. Higher = buyers sweeping deeper through the ask side. If this predicts better buy markouts, it suggests aggressive buying flow is informed and continues.",
    "sell_trade_depth_ems": "EMS of sweep depth for sell-side trades only (aggressor selling, hitting bids). Analyzed against sell fills only. Higher = sellers sweeping deeper through the bid side. If this predicts better sell markouts, it suggests aggressive selling flow is informed and continues.",
    "liq_depth_buy_100k": "Price depth (relative to mid) to sell $100k into bids. Instantaneous snapshot at fill time. Analyzed against buy fills only. Higher = thinner bid side = less support below our buy = unfavorable.",
    "liq_depth_buy_250k": "Same as liq_depth_buy_100k but for $250k. Captures deeper liquidity.",
    "liq_depth_sell_100k": "Price depth (relative to mid) to buy $100k from asks. Instantaneous snapshot at fill time. Analyzed against sell fills only. Higher = thinner ask side = less resistance above our sell = unfavorable.",
    "liq_depth_sell_250k": "Same as liq_depth_sell_100k but for $250k.",
    "liq_depth_buy_100k_ems": "EMS of liq_depth_buy_100k, 30s TDC, updated on each trade. Smoothed version. Analyzed against buy fills only.",
    "liq_depth_buy_250k_ems": "EMS of liq_depth_buy_250k, 30s TDC. Analyzed against buy fills only.",
    "liq_depth_sell_100k_ems": "EMS of liq_depth_sell_100k, 30s TDC. Analyzed against sell fills only.",
    "liq_depth_sell_250k_ems": "EMS of liq_depth_sell_250k, 30s TDC. Analyzed against sell fills only.",
    "adj_book_depth_ratio": "side * (bid_depth - ask_depth) / total within 5 ticks. 30s TDC EMS updated on trades. Positive = more near-touch liquidity on fill side (favorable: support/resistance behind us).",
    "adj_deep_book_imbalance": "side * (bid_notional - ask_notional) / (bid+ask) within 50bps of mid. Snapshot at fill time. Positive = more deep liquidity on our fill side (favorable: support/resistance behind us).",
    "book_imbalance": "side * (bb_size - ba_size) / (bb+ba) at top of book. Snapshot at fill time, no smoothing.",
    "fill_edge_bps": "side * (remote_mid - fill_px) / fill_px * 10000. Theoretical edge at fill time vs oracle. Positive = bought below oracle or sold above.",
    "time_since_fill_ms": "Time since previous fill in ms (per day). Short gaps = burst of activity; long gaps = quiet period before fill.",
    "vol_accel": "vol_ems_remote_4s / vol_ems_remote_30s. >1 = vol spiking (recent burst), <1 = vol subsiding. Measures vol regime change speed.",
    "hour_et": "Hour of the day in US Eastern time.",
    "side": "Fill side: -1 = sell, +1 = buy.",
    "add_liq": "Whether the fill added liquidity (passive, 1) or took it (aggressive, 0).",
}


def print_binned(feature, bins, horizon="30s"):
    """Print binned markout table."""
    if bins is None:
        return
    print(f"\n=== BINNED: {feature} -> {horizon} markout (bps) ===")
    desc = FEATURE_DESCRIPTIONS.get(feature)
    if desc:
        print(f"  ({desc})")
    headers = ["bin", "range", "N", "mean", "median", "pct+"]
    rows = []
    for i, b in enumerate(bins):
        rows.append([
            f"Q{i+1}",
            b["bin_label"],
            str(b["count"]),
            f"{b['mean']:+.1f}",
            f"{b['median']:+.1f}",
            f"{b['pct_pos']:.1f}%",
        ])
    for line in fmt_table(headers, rows):
        print("  " + line)
    takeaway = bin_takeaway(feature, bins)
    if takeaway:
        print(takeaway)


def print_cross_tab(ct):
    """Print 2D cross-tab markout table."""
    if ct is None:
        return
    print(f"\n=== CROSS-TAB: {ct['f1']} x {ct['f2']} -> {ct['horizon']} markout (bps) ===")
    headers = [f"{ct['f1']} bin", f"{ct['f2']} bin", "N", "mean", "median", "pct+"]
    rows = []
    for c in ct["cells"]:
        rows.append([
            c["f1_bin"],
            c["f2_bin"],
            str(c["count"]),
            f"{c['mean']:+.1f}",
            f"{c['median']:+.1f}",
            f"{c['pct_pos']:.1f}%",
        ])
    for line in fmt_table(headers, rows):
        print("  " + line)
    takeaway = cross_tab_takeaway(ct)
    if takeaway:
        print(takeaway)


def print_per_day(day_stats, horizon="30s"):
    """Print per-day stats table."""
    print(f"\n=== PER-DAY ({horizon} markout, bps) ===")
    headers = ["date", "N", "mean", "median", "pct+", "worst", "avg_vol_ems"]
    rows = []
    for d in day_stats:
        mean_s = f"{d['markout_mean']:+.1f}" if d["markout_mean"] is not None else "n/a"
        med_s = f"{d['markout_median']:+.1f}" if d["markout_median"] is not None else "n/a"
        pct_s = f"{d['pct_pos']:.1f}%" if d["pct_pos"] is not None else "n/a"
        worst_s = f"{d['worst']:+.1f}" if d["worst"] is not None else "n/a"
        vol_s = f"{d['avg_vol_ems']:.5f}" if d.get("avg_vol_ems") is not None else "n/a"
        rows.append([d["date"], str(d["n_fills"]), mean_s, med_s, pct_s, worst_s, vol_s])
    for line in fmt_table(headers, rows):
        print("  " + line)


def print_premium_analysis(pa):
    """Print premium analysis results."""
    if not pa:
        return

    horizon = pa["horizon"]

    ps = pa.get("premium_stats")
    if ps:
        print(f"\n=== PREMIUM ANALYSIS ({horizon} markout) ===")
        bias = "positive" if ps["mean_bps"] > 2 else "negative" if ps["mean_bps"] < -2 else "neutral"
        print(f"  Premium bias: {bias} (mean {ps['mean_bps']:+.1f} bps, "
              f"median {ps['median_bps']:+.1f} bps, {ps['pct_positive']:.0f}% positive)")

    cells = pa.get("side_x_premium", [])
    if cells:
        print(f"\n  Side x Premium Sign -> {horizon} markout:")
        print(f"  {'':>15}  {'N':>6}  {'median':>8}  {'mean':>8}  {'pct+':>6}")
        for c in cells:
            label = f"{c['side']:>4} {c['premium_sign']}"
            print(f"  {label:>15}  {c['count']:>6}  {c['median_markout']:>+8.1f}  "
                  f"{c['mean_markout']:>+8.1f}  {c['pct_pos']:>5.1f}%")

        # Compute buy/sell diffs for verdict
        by_key = {(c["side"], c["premium_sign"]): c for c in cells}
        for side in ["buy", "sell"]:
            neg = by_key.get((side, "prem<0"))
            pos = by_key.get((side, "prem>0"))
            if neg and pos:
                diff = pos["median_markout"] - neg["median_markout"]
                if abs(diff) > 3:
                    effect = "HELPS" if diff > 0 else "HURTS"
                    print(f"  -> Premium {effect} {side}s ({diff:+.1f} bps)")

    blocks = pa.get("by_timeblock", [])
    if blocks:
        print(f"\n  Premium by time of day:")
        print(f"  {'Block':>20}  {'N':>6}  {'mean':>8}  {'std':>8}  {'pct+':>6}")
        for b in blocks:
            print(f"  {b['label']:>20}  {b['count']:>6}  {b['mean_prem_bps']:>+8.1f}  "
                  f"{b['std_prem_bps']:>8.1f}  {b['pct_positive']:>5.1f}%")

    ema = pa.get("ema_magnitude", [])
    if ema:
        print(f"\n  EMA magnitude impact ({horizon} markout):")
        for e in ema:
            diff = e["hi_median_markout"] - e["lo_median_markout"]
            direction = "WORSE" if diff < -3 else "BETTER" if diff > 3 else "similar"
            print(f"    {e['side'].upper()} ({e['lo_count'] + e['hi_count']} fills):")
            print(f"      Low EMA  (avg {e['lo_avg_ema_bps']:>+7.1f} bps): "
                  f"median {e['lo_median_markout']:>+6.1f} bps, {e['lo_pct_pos']:.0f}% profitable")
            print(f"      High EMA (avg {e['hi_avg_ema_bps']:>+7.1f} bps): "
                  f"median {e['hi_median_markout']:>+6.1f} bps, {e['hi_pct_pos']:.0f}% profitable")
            print(f"      -> High-EMA fills are {direction} ({diff:+.1f} bps)")


def run_full_analysis(workdir, horizon="30s", n_bins=4, print_output=True):
    """Run all analysis steps and optionally print to stdout.

    Returns (df, results_dict).
    """
    df, info = load_fill_diags(workdir)
    if print_output:
        print(f"Loaded {info['n_fills']} fills from {info['n_days']} days ({info['n_files']} files, {info['n_skipped']} skipped)")
        for h, n in info["horizon_n"].items():
            if n < info["n_fills"]:
                print(f"  {h}: {n}/{info['n_fills']} valid markouts")

    results = {"info": info}

    # Summary
    stats = summary_stats(df)
    results["summary"] = stats
    if print_output:
        print_summary(stats)

    # Term structure
    ts = markout_term_structure(stats.get("markouts", []))
    results["term_structure"] = ts
    if print_output:
        print_term_structure(ts)

    # Binned markouts
    # all_takeaways: list of (feature_name, spread_bps, takeaway_text)
    results["binned"] = {}
    all_takeaways = []
    for entry in DEFAULT_BIN_FEATURES:
        if isinstance(entry, tuple):
            feat, fside = entry
            side_label = "buys" if fside == 1 else "sells"
            result_key = f"{feat}|{side_label}"
        else:
            feat, fside = entry, None
            side_label = None
            result_key = feat
        if feat not in df.columns:
            continue
        bins = bin_markouts(df, feat, horizon=horizon, n_bins=n_bins, filter_side=fside)
        if bins is not None:
            results["binned"][result_key] = bins
            if print_output:
                label = f"{feat} ({side_label} only)" if side_label else feat
                print_binned(label, bins, horizon)
            ta = bin_takeaway(feat, bins)
            if ta:
                ta_text = ta.strip()
                if ta_text.startswith("-> "):
                    ta_text = ta_text[3:]
                medians = [b["median"] for b in bins]
                spread = max(medians) - min(medians)
                display_name = f"{feat} ({side_label})" if side_label else feat
                all_takeaways.append((display_name, spread, ta_text))

    # Cross-tabs
    results["cross_tabs"] = []
    for f1, f2 in DEFAULT_CROSS_PAIRS:
        if f1 not in df.columns or f2 not in df.columns:
            continue
        ct = cross_tab_markouts(df, f1, f2, horizon=horizon)
        if ct is not None:
            results["cross_tabs"].append(ct)
            if print_output:
                print_cross_tab(ct)
            cells = ct.get("cells", [])
            if cells:
                ct_medians = [c["median"] for c in cells]
                spread = max(ct_medians) - min(ct_medians)
                ta = cross_tab_takeaway(ct)
                if ta:
                    ta_text = ta.strip()
                    if ta_text.startswith("-> "):
                        ta_text = ta_text[3:]
                    all_takeaways.append((f"{f1} x {f2}", spread, ta_text))

    # Per-day
    day_stats = per_day_stats(df, horizon=horizon)
    results["per_day"] = day_stats
    if print_output:
        print_per_day(day_stats, horizon)

    # Premium analysis
    pa = premium_analysis(df, horizon=horizon)
    results["premium"] = pa
    if print_output:
        print_premium_analysis(pa)

    # Summary takeaways at the bottom, ranked by signal strength.
    if print_output and all_takeaways:
        sorted_ta = sorted(all_takeaways, key=lambda x: x[1], reverse=True)

        print(f"\n{'='*68}")
        print(f"  KEY TAKEAWAYS (ranked by signal strength, {horizon} markout)")
        print(f"{'='*68}")
        for i, (feat, spread, text) in enumerate(sorted_ta, 1):
            label = "STRONG" if spread >= 5 else "MODERATE" if spread >= 3 else "WEAK"
            print(f"  {i}. [{label:>8}] {feat}: {text}")

    return df, results
