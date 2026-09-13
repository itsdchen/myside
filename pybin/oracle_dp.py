#!/usr/bin/env python3
"""
Oracle DP: Compute oracle-optimal trade sequences via dynamic programming.

Loads trade_record CSVs from TradeRecorder, computes oracle-optimal decisions
for maker (passive fills) and taker (aggressive crossing) strategies, then
regresses features against markouts and oracle decisions.

Includes:
  - Markout-proxy DPs (original: value each fill by markout)
  - Full DPs (mid-to-mid carry PnL with position tracking)
  - Linear regressions (Ridge / Logistic)
  - Tree regressions (GradientBoosting)

Usage:
    python pybin/oracle_dp.py --workdir ~/scratch/trade_recorder_test --max-pos 5
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────
# 1. Data loading
# ─────────────────────────────────────────────────────────────────────

def load_trade_records(workdir):
    """Load and concatenate all trade_record_*.csv files from workdir."""
    pattern = os.path.join(workdir, "**", "trade_record_*.csv")
    files = sorted(glob.glob(pattern, recursive=True))
    if not files:
        # Also try direct path.
        pattern = os.path.join(workdir, "trade_record_*.csv")
        files = sorted(glob.glob(pattern))
    if not files:
        print(f"ERROR: No trade_record_*.csv files found in {workdir}")
        return None

    print(f"  Loading {len(files)} trade record files...")
    dfs = []
    for f in files:
        df = pd.read_csv(f)
        df["source_file"] = os.path.basename(f)
        dfs.append(df)

    df = pd.concat(dfs, ignore_index=True)
    df.sort_values("time_ms", inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(f"  Loaded {len(df)} trade events from {len(files)} files")
    return df


def get_feature_cols(prefix):
    """Return the list of feature column names for a given prefix (mk_ or tk_)."""
    return [
        f"{prefix}remote_mid", f"{prefix}local_mid", f"{prefix}spread",
        f"{prefix}bid_size", f"{prefix}ask_size",
        f"{prefix}vol_100ms", f"{prefix}vol_1s", f"{prefix}vol_4s",
        f"{prefix}vol_10s", f"{prefix}vol_30s", f"{prefix}vol_60s", f"{prefix}vol_300s",
        f"{prefix}mom_100ms", f"{prefix}mom_1s", f"{prefix}mom_4s",
        f"{prefix}mom_10s", f"{prefix}mom_30s",
        f"{prefix}trade_rate", f"{prefix}trade_size",
        f"{prefix}trade_notional_signed", f"{prefix}trade_notional_abs",
        f"{prefix}book_depth_bid", f"{prefix}book_depth_ask",
        f"{prefix}buy_trade_depth", f"{prefix}sell_trade_depth",
        f"{prefix}large_trade_rate", f"{prefix}trade_depth",
    ]


# ─────────────────────────────────────────────────────────────────────
# 2. Maker oracle DP
# ─────────────────────────────────────────────────────────────────────

def maker_oracle_dp(df, max_pos, horizon_col="markout_30s"):
    """Backward-induction DP for passive (maker) oracle.

    At each trade event, the oracle can:
      - Fill on the passive side (matching the trade's aggressor) or skip.
      - Fill value = markout-based PnL using the specified horizon.

    State: (event_index, position) where position in [-max_pos, max_pos].

    Returns:
        decisions: np.array of shape (N,), values in {-1, 0, +1}
            -1 = sell fill, +1 = buy fill, 0 = skip
        optimal_pnl: float, total PnL of optimal sequence
    """
    N = len(df)
    pos_range = 2 * max_pos + 1  # positions from -max_pos to +max_pos
    pos_offset = max_pos  # index offset: position p -> index p + max_pos

    # Precompute per-event fill value.
    # net_signed_qty > 0 means aggressor was buying, so passive side = sell.
    # net_signed_qty < 0 means aggressor was selling, so passive side = buy.
    remote_mid = df["remote_mid_at_trade"].values
    markout_mid = df[horizon_col].values
    agg_px = df["agg_px"].values
    net_signed = df["net_signed_qty"].values

    # fill_side: +1 if we could buy passively (aggressor selling, net<0),
    #            -1 if we could sell passively (aggressor buying, net>0)
    fill_side = np.where(net_signed < 0, 1, np.where(net_signed > 0, -1, 0))

    # Fill value: (markout_mid - trade_px) * fill_side
    # If fill_side=+1 (buy): profit when price goes up -> markout - px
    # If fill_side=-1 (sell): profit when price goes down -> px - markout
    fill_value = np.where(
        np.isnan(markout_mid) | (fill_side == 0),
        0,
        (markout_mid - agg_px) * fill_side,
    )

    # DP: V[i, p] = optimal future PnL from event i onward at position p.
    V = np.zeros((N + 1, pos_range))
    decision = np.zeros((N, pos_range), dtype=np.int8)

    for i in range(N - 1, -1, -1):
        for p_idx in range(pos_range):
            pos = p_idx - pos_offset

            # Option 1: skip.
            val_skip = V[i + 1, p_idx]
            best_val = val_skip
            best_dec = 0

            # Option 2: fill (if position allows).
            fs = fill_side[i]
            if fs != 0:
                new_pos = pos + fs
                if -max_pos <= new_pos <= max_pos:
                    new_p_idx = new_pos + pos_offset
                    val_fill = fill_value[i] + V[i + 1, new_p_idx]
                    if val_fill > best_val:
                        best_val = val_fill
                        best_dec = fs

            V[i, p_idx] = best_val
            decision[i, p_idx] = best_dec

    # Forward pass: extract optimal decisions.
    decisions = np.zeros(N, dtype=np.int8)
    pos = 0
    for i in range(N):
        p_idx = pos + pos_offset
        dec = decision[i, p_idx]
        decisions[i] = dec
        pos += dec

    optimal_pnl = V[0, pos_offset]
    return decisions, optimal_pnl


# ─────────────────────────────────────────────────────────────────────
# 3. Taker oracle DP
# ─────────────────────────────────────────────────────────────────────

def taker_oracle_dp(df, max_pos, spread_col="mk_spread", horizon_col="markout_30s"):
    """Backward-induction DP for aggressive (taker) oracle.

    At each trade event (used as time samples), the oracle can:
      - Buy (cross the ask), sell (cross the bid), or hold.
      - Crossing costs half the spread.

    State: (time_index, position) where position in [-max_pos, max_pos].

    Returns:
        decisions: np.array of shape (N,), values in {-1, 0, +1}
        optimal_pnl: float
    """
    N = len(df)
    pos_range = 2 * max_pos + 1
    pos_offset = max_pos

    remote_mid = df["remote_mid_at_trade"].values
    markout_mid = df[horizon_col].values
    spread = df[spread_col].values

    # Half-spread cost for crossing.
    half_spread = np.abs(spread) / 2.0
    half_spread = np.where(np.isnan(half_spread), 0, half_spread)

    # Expected PnL of a trade at event i:
    # Buy: markout_mid - (remote_mid + half_spread) = (markout_mid - remote_mid) - half_spread
    # Sell: (remote_mid - half_spread) - markout_mid = (remote_mid - markout_mid) - half_spread
    mid_move = np.where(np.isnan(markout_mid), 0, markout_mid - remote_mid)

    V = np.zeros((N + 1, pos_range))
    decision = np.zeros((N, pos_range), dtype=np.int8)

    for i in range(N - 1, -1, -1):
        for p_idx in range(pos_range):
            pos = p_idx - pos_offset

            # Hold.
            best_val = V[i + 1, p_idx]
            best_dec = 0

            # Buy (+1).
            if pos + 1 <= max_pos:
                val_buy = (mid_move[i] - half_spread[i]) + V[i + 1, p_idx + 1]
                if val_buy > best_val:
                    best_val = val_buy
                    best_dec = 1

            # Sell (-1).
            if pos - 1 >= -max_pos:
                val_sell = (-mid_move[i] - half_spread[i]) + V[i + 1, p_idx - 1]
                if val_sell > best_val:
                    best_val = val_sell
                    best_dec = -1

            V[i, p_idx] = best_val
            decision[i, p_idx] = best_dec

    # Forward pass.
    decisions = np.zeros(N, dtype=np.int8)
    pos = 0
    for i in range(N):
        p_idx = pos + pos_offset
        dec = decision[i, p_idx]
        decisions[i] = dec
        pos += dec

    optimal_pnl = V[0, pos_offset]
    return decisions, optimal_pnl


# ─────────────────────────────────────────────────────────────────────
# 3a. Full maker DP (mid-to-mid carry PnL)
# ─────────────────────────────────────────────────────────────────────

def full_maker_dp(df, max_pos):
    """Full DP for maker oracle using actual mid-to-mid carry PnL.

    Unlike the markout DP which values each fill independently, this tracks
    cumulative position carry: holding position p from event i to i+1 earns
    (mid[i+1] - mid[i]) * p.

    V[i, p] = max over d in {fill_side, 0}:
        new_p = p + d
        step_pnl = (mid[i+1] - mid[i]) * new_p          # carry on new position
                 + (mid[i] - agg_px[i]) * d               # fill edge
        V[i, p] = step_pnl + V[i+1, new_p]
    Terminal: V[N, p] = 0

    Returns: (decisions, optimal_pnl)
    """
    N = len(df)
    pos_range = 2 * max_pos + 1
    pos_offset = max_pos

    remote_mid = df["remote_mid_at_trade"].values
    agg_px = df["agg_px"].values
    net_signed = df["net_signed_qty"].values

    # fill_side: +1 buy passively (aggressor selling), -1 sell passively
    fill_side = np.where(net_signed < 0, 1, np.where(net_signed > 0, -1, 0))

    # Mid prices for carry computation (use remote_mid as the mid reference)
    mid = remote_mid.copy()

    V = np.zeros((N + 1, pos_range))
    decision = np.zeros((N, pos_range), dtype=np.int8)

    for i in range(N - 1, -1, -1):
        # Carry: mid change from this event to next (0 for last event)
        mid_change = mid[i + 1] - mid[i] if i + 1 < N else 0.0

        for p_idx in range(pos_range):
            pos = p_idx - pos_offset

            # Option 1: skip (d=0). Carry on existing position.
            val_skip = mid_change * pos + V[i + 1, p_idx]
            best_val = val_skip
            best_dec = 0

            # Option 2: fill
            fs = fill_side[i]
            if fs != 0:
                new_pos = pos + fs
                if -max_pos <= new_pos <= max_pos:
                    new_p_idx = new_pos + pos_offset
                    fill_edge = (mid[i] - agg_px[i]) * fs
                    val_fill = mid_change * new_pos + fill_edge + V[i + 1, new_p_idx]
                    if val_fill > best_val:
                        best_val = val_fill
                        best_dec = fs

            V[i, p_idx] = best_val
            decision[i, p_idx] = best_dec

    # Forward pass
    decisions = np.zeros(N, dtype=np.int8)
    pos = 0
    for i in range(N):
        p_idx = pos + pos_offset
        dec = decision[i, p_idx]
        decisions[i] = dec
        pos += dec

    return decisions, V[0, pos_offset]


# ─────────────────────────────────────────────────────────────────────
# 3b. Full taker DP (mid-to-mid carry PnL)
# ─────────────────────────────────────────────────────────────────────

def full_taker_dp(df, max_pos, spread_col="mk_spread"):
    """Full DP for taker oracle using actual mid-to-mid carry PnL.

    At each event, oracle can buy (+1), sell (-1), or hold (0).
    Crossing costs half the spread. Position carry uses actual mid changes.

    V[i, p] = max over d in {-1, 0, +1}:
        new_p = p + d
        step_pnl = (mid[i+1] - mid[i]) * new_p          # carry on new position
                 + (mid[i] - agg_px_effective) * d        # fill edge (with spread cost)
        where agg_px_effective accounts for half-spread crossing cost
    Terminal: V[N, p] = 0

    Returns: (decisions, optimal_pnl)
    """
    N = len(df)
    pos_range = 2 * max_pos + 1
    pos_offset = max_pos

    mid = df["remote_mid_at_trade"].values.copy()
    spread = df[spread_col].values
    half_spread = np.abs(spread) / 2.0
    half_spread = np.where(np.isnan(half_spread), 0, half_spread)

    V = np.zeros((N + 1, pos_range))
    decision = np.zeros((N, pos_range), dtype=np.int8)

    for i in range(N - 1, -1, -1):
        mid_change = mid[i + 1] - mid[i] if i + 1 < N else 0.0

        for p_idx in range(pos_range):
            pos = p_idx - pos_offset

            # Hold (d=0)
            val_hold = mid_change * pos + V[i + 1, p_idx]
            best_val = val_hold
            best_dec = 0

            # Buy (+1)
            if pos + 1 <= max_pos:
                new_p_idx = p_idx + 1
                # Buy at mid + half_spread, edge = -half_spread
                val_buy = mid_change * (pos + 1) - half_spread[i] + V[i + 1, new_p_idx]
                if val_buy > best_val:
                    best_val = val_buy
                    best_dec = 1

            # Sell (-1)
            if pos - 1 >= -max_pos:
                new_p_idx = p_idx - 1
                # Sell at mid - half_spread, edge = -half_spread
                val_sell = mid_change * (pos - 1) - half_spread[i] + V[i + 1, new_p_idx]
                if val_sell > best_val:
                    best_val = val_sell
                    best_dec = -1

            V[i, p_idx] = best_val
            decision[i, p_idx] = best_dec

    # Forward pass
    decisions = np.zeros(N, dtype=np.int8)
    pos = 0
    for i in range(N):
        p_idx = pos + pos_offset
        dec = decision[i, p_idx]
        decisions[i] = dec
        pos += dec

    return decisions, V[0, pos_offset]


# ─────────────────────────────────────────────────────────────────────
# 4. Markout regression
# ─────────────────────────────────────────────────────────────────────

def markout_regression(df, prefix="mk_", horizon_col="markout_30s"):
    """Ridge regression of features -> signed markout return.

    Returns dict with R^2, top feature coefficients.
    """
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler

    feature_cols = get_feature_cols(prefix)
    # Filter to available columns.
    feature_cols = [c for c in feature_cols if c in df.columns]

    # Target: signed markout return relative to remote_mid at trade.
    remote_mid = df["remote_mid_at_trade"].values
    markout = df[horizon_col].values
    valid = (~np.isnan(markout)) & (remote_mid > 0)

    X = df.loc[valid, feature_cols].values
    y = (markout[valid] - remote_mid[valid]) / remote_mid[valid]

    # Drop rows with NaN or inf features.
    feat_valid = ~np.any(np.isnan(X) | np.isinf(X), axis=1)
    X = X[feat_valid]
    y = y[feat_valid]

    if len(X) < 10:
        print(f"  Markout regression ({prefix}): too few valid samples ({len(X)})")
        return None

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = Ridge(alpha=1.0)
    model.fit(X_scaled, y)
    r2 = model.score(X_scaled, y)

    # Top coefficients.
    coef_abs = np.abs(model.coef_)
    top_idx = np.argsort(coef_abs)[::-1][:10]

    print(f"\n  Markout regression ({prefix}{horizon_col}):")
    print(f"    R^2 = {r2:.4f}  (N = {len(X)})")
    print(f"    Top features:")
    for idx in top_idx:
        print(f"      {feature_cols[idx]:40s}  coef={model.coef_[idx]:+.6f}")

    return {"r2": r2, "n_samples": len(X), "top_features": {
        feature_cols[i]: float(model.coef_[i]) for i in top_idx
    }}


# ─────────────────────────────────────────────────────────────────────
# 5. Oracle regression
# ─────────────────────────────────────────────────────────────────────

def oracle_regression(df, oracle_decisions, prefix="mk_", label="maker"):
    """Logistic regression of features -> oracle fill/skip decision.

    Returns dict with accuracy, top feature coefficients.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    feature_cols = get_feature_cols(prefix)
    feature_cols = [c for c in feature_cols if c in df.columns]

    # Binary target: 1 if oracle decided to fill (decision != 0), 0 if skip.
    y = (oracle_decisions != 0).astype(int)

    X = df[feature_cols].values
    valid = ~np.any(np.isnan(X) | np.isinf(X), axis=1)
    X = X[valid]
    y = y[valid]

    if len(X) < 10:
        print(f"  Oracle regression ({label}): too few valid samples ({len(X)})")
        return None

    n_pos = y.sum()
    n_neg = len(y) - n_pos
    if n_pos < 5 or n_neg < 5:
        print(f"  Oracle regression ({label}): too few positive/negative samples ({n_pos}/{n_neg})")
        return None

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = LogisticRegression(max_iter=1000, C=1.0)
    model.fit(X_scaled, y)
    accuracy = model.score(X_scaled, y)

    coef_abs = np.abs(model.coef_[0])
    top_idx = np.argsort(coef_abs)[::-1][:10]

    print(f"\n  Oracle regression ({label}):")
    print(f"    Accuracy = {accuracy:.4f}  (N = {len(X)}, fills = {n_pos}, skips = {n_neg})")
    print(f"    Top features:")
    for idx in top_idx:
        print(f"      {feature_cols[idx]:40s}  coef={model.coef_[0][idx]:+.6f}")

    return {"accuracy": accuracy, "n_samples": len(X), "n_fills": int(n_pos),
            "top_features": {feature_cols[i]: float(model.coef_[0][i]) for i in top_idx}}


# ─────────────────────────────────────────────────────────────────────
# 6. ElasticNet markout regression
# ─────────────────────────────────────────────────────────────────────

def elasticnet_markout_regression(df, prefix="mk_", horizon_col="markout_30s"):
    """ElasticNet regression of features -> markout return.

    L1+L2 regularization gives feature selection (sparse coefs) without
    overfitting risk of tree models. Uses temporal 80/20 train/test split.
    """
    import warnings
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import ElasticNetCV
    from sklearn.preprocessing import StandardScaler
    warnings.filterwarnings("ignore", category=ConvergenceWarning)

    feature_cols = get_feature_cols(prefix)
    feature_cols = [c for c in feature_cols if c in df.columns]

    remote_mid = df["remote_mid_at_trade"].values
    markout = df[horizon_col].values
    valid = (~np.isnan(markout)) & (remote_mid > 0)

    X = df.loc[valid, feature_cols].values
    y = (markout[valid] - remote_mid[valid]) / remote_mid[valid]

    feat_valid = ~np.any(np.isnan(X) | np.isinf(X), axis=1)
    X = X[feat_valid]
    y = y[feat_valid]

    if len(X) < 20:
        print(f"  ElasticNet markout regression ({prefix}): too few samples ({len(X)})")
        return None

    # Temporal split
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    # Standardize target to avoid alpha miscalibration on small-scale returns.
    y_mean, y_std = y_train.mean(), y_train.std()
    y_train_s = (y_train - y_mean) / y_std
    y_test_s = (y_test - y_mean) / y_std

    model = ElasticNetCV(
        l1_ratio=[0.1, 0.5, 0.7, 0.9, 0.95],
        n_alphas=100, cv=5, max_iter=10000, random_state=42,
    )
    model.fit(X_train_s, y_train_s)
    train_r2 = model.score(X_train_s, y_train_s)
    test_r2 = model.score(X_test_s, y_test_s)

    coef_abs = np.abs(model.coef_)
    top_idx = np.argsort(coef_abs)[::-1][:10]
    n_nonzero = np.sum(coef_abs > 1e-8)

    print(f"\n  ElasticNet markout regression ({prefix}{horizon_col}):")
    print(f"    Train R^2 = {train_r2:.4f}, Test R^2 = {test_r2:.4f}  (N = {len(X)}, split = {split})")
    print(f"    alpha = {model.alpha_:.6f}, l1_ratio = {model.l1_ratio_:.2f}, nonzero = {n_nonzero}/{len(feature_cols)}")
    print(f"    Top features:")
    for idx in top_idx:
        if coef_abs[idx] < 1e-8:
            continue
        print(f"      {feature_cols[idx]:40s}  coef={model.coef_[idx]:+.6f}")

    return {"train_r2": train_r2, "test_r2": test_r2, "n_samples": len(X),
            "alpha": float(model.alpha_), "l1_ratio": float(model.l1_ratio_),
            "n_nonzero": int(n_nonzero),
            "top_features": {feature_cols[i]: float(model.coef_[i]) for i in top_idx
                             if coef_abs[i] > 1e-8}}


# ─────────────────────────────────────────────────────────────────────
# 7. ElasticNet oracle regression (via SGDClassifier with elasticnet)
# ─────────────────────────────────────────────────────────────────────

def elasticnet_oracle_regression(df, oracle_decisions, prefix="mk_", label="maker"):
    """Logistic regression with ElasticNet penalty for oracle fill/skip.

    Uses SGDClassifier(loss='log_loss', penalty='elasticnet') for L1+L2
    regularized classification. Temporal 80/20 train/test split.
    """
    from sklearn.linear_model import SGDClassifier
    from sklearn.preprocessing import StandardScaler

    feature_cols = get_feature_cols(prefix)
    feature_cols = [c for c in feature_cols if c in df.columns]

    y = (oracle_decisions != 0).astype(int)
    X = df[feature_cols].values
    valid = ~np.any(np.isnan(X) | np.isinf(X), axis=1)
    X = X[valid]
    y = y[valid]

    if len(X) < 20:
        print(f"  ElasticNet oracle regression ({label}): too few samples ({len(X)})")
        return None

    n_pos = y.sum()
    n_neg = len(y) - n_pos
    if n_pos < 5 or n_neg < 5:
        print(f"  ElasticNet oracle regression ({label}): too few pos/neg ({n_pos}/{n_neg})")
        return None

    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    model = SGDClassifier(
        loss="log_loss", penalty="elasticnet",
        l1_ratio=0.5, alpha=1e-4,
        max_iter=2000, random_state=42,
    )
    model.fit(X_train_s, y_train)
    train_acc = model.score(X_train_s, y_train)
    test_acc = model.score(X_test_s, y_test)

    coef_abs = np.abs(model.coef_[0])
    top_idx = np.argsort(coef_abs)[::-1][:10]
    n_nonzero = np.sum(coef_abs > 1e-8)

    print(f"\n  ElasticNet oracle regression ({label}):")
    print(f"    Train acc = {train_acc:.4f}, Test acc = {test_acc:.4f}  (N = {len(X)}, fills = {n_pos})")
    print(f"    nonzero = {n_nonzero}/{len(feature_cols)}")
    print(f"    Top features:")
    for idx in top_idx:
        if coef_abs[idx] < 1e-8:
            continue
        print(f"      {feature_cols[idx]:40s}  coef={model.coef_[0][idx]:+.6f}")

    return {"train_accuracy": train_acc, "test_accuracy": test_acc,
            "n_samples": len(X), "n_fills": int(n_pos),
            "n_nonzero": int(n_nonzero),
            "top_features": {feature_cols[i]: float(model.coef_[0][i]) for i in top_idx
                             if coef_abs[i] > 1e-8}}


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Oracle DP analysis pipeline")
    parser.add_argument("--workdir", required=True, help="Directory with trade_record CSVs")
    parser.add_argument("--max-pos", type=int, default=5, help="Max position for DP (default: 5)")
    parser.add_argument("--horizon", default="markout_30s",
                        help="Markout horizon column (default: markout_30s)")
    args = parser.parse_args()

    df = load_trade_records(args.workdir)
    if df is None:
        sys.exit(1)

    print(f"\n  DataFrame shape: {df.shape}")
    print(f"  Columns: {list(df.columns)}")
    print(f"  Time range: {df['time_ms'].min()} - {df['time_ms'].max()}")

    horizon_col = args.horizon

    if horizon_col not in df.columns:
        print(f"  ERROR: Horizon column '{horizon_col}' not found in data")
        print(f"  Available markout columns: {[c for c in df.columns if 'markout' in c]}")
        sys.exit(1)

    # ── Markout-proxy DPs (original) ──
    print(f"\n{'='*60}")
    print(f"  MARKOUT-PROXY MAKER DP (max_pos={args.max_pos})")
    print(f"{'='*60}")

    maker_dec, maker_pnl = maker_oracle_dp(df, args.max_pos, horizon_col=horizon_col)
    n_fills = np.sum(maker_dec != 0)
    n_buys = np.sum(maker_dec == 1)
    n_sells = np.sum(maker_dec == -1)
    print(f"  Optimal PnL: {maker_pnl:.4f}")
    print(f"  Fills: {n_fills} ({n_buys} buys, {n_sells} sells) out of {len(df)} events")

    print(f"\n{'='*60}")
    print(f"  MARKOUT-PROXY TAKER DP (max_pos={args.max_pos})")
    print(f"{'='*60}")

    taker_dec, taker_pnl = taker_oracle_dp(df, args.max_pos, horizon_col=horizon_col)
    n_trades = np.sum(taker_dec != 0)
    n_buys = np.sum(taker_dec == 1)
    n_sells = np.sum(taker_dec == -1)
    print(f"  Optimal PnL: {taker_pnl:.4f}")
    print(f"  Trades: {n_trades} ({n_buys} buys, {n_sells} sells) out of {len(df)} events")

    # ── Full DPs (mid-to-mid carry) ──
    print(f"\n{'='*60}")
    print(f"  FULL MAKER DP (mid-to-mid carry, max_pos={args.max_pos})")
    print(f"{'='*60}")

    full_maker_dec, full_maker_pnl = full_maker_dp(df, args.max_pos)
    n_fills_f = np.sum(full_maker_dec != 0)
    n_buys_f = np.sum(full_maker_dec == 1)
    n_sells_f = np.sum(full_maker_dec == -1)
    print(f"  Optimal PnL: {full_maker_pnl:.4f}")
    print(f"  Fills: {n_fills_f} ({n_buys_f} buys, {n_sells_f} sells) out of {len(df)} events")

    print(f"\n{'='*60}")
    print(f"  FULL TAKER DP (mid-to-mid carry, max_pos={args.max_pos})")
    print(f"{'='*60}")

    full_taker_dec, full_taker_pnl = full_taker_dp(df, args.max_pos)
    n_trades_f = np.sum(full_taker_dec != 0)
    n_buys_f = np.sum(full_taker_dec == 1)
    n_sells_f = np.sum(full_taker_dec == -1)
    print(f"  Optimal PnL: {full_taker_pnl:.4f}")
    print(f"  Trades: {n_trades_f} ({n_buys_f} buys, {n_sells_f} sells) out of {len(df)} events")

    # ── DP Comparison ──
    print(f"\n{'='*60}")
    print(f"  DP COMPARISON")
    print(f"{'='*60}")
    print(f"  {'Method':<30s} {'Maker PnL':>12s} {'Taker PnL':>12s}")
    print(f"  {'-'*54}")
    print(f"  {'Markout-proxy':<30s} {maker_pnl:>12.4f} {taker_pnl:>12.4f}")
    print(f"  {'Full (mid-to-mid)':<30s} {full_maker_pnl:>12.4f} {full_taker_pnl:>12.4f}")

    # ── Markout regressions (linear) ──
    print(f"\n{'='*60}")
    print(f"  MARKOUT REGRESSIONS (Linear)")
    print(f"{'='*60}")

    mk_reg = markout_regression(df, prefix="mk_", horizon_col=horizon_col)
    tk_reg = markout_regression(df, prefix="tk_", horizon_col=horizon_col)

    # ── Markout regressions (ElasticNet) ──
    print(f"\n{'='*60}")
    print(f"  MARKOUT REGRESSIONS (ElasticNet)")
    print(f"{'='*60}")

    mk_enet_reg = elasticnet_markout_regression(df, prefix="mk_", horizon_col=horizon_col)
    tk_enet_reg = elasticnet_markout_regression(df, prefix="tk_", horizon_col=horizon_col)

    # ── Oracle regressions (linear) ──
    print(f"\n{'='*60}")
    print(f"  ORACLE REGRESSIONS (Logistic)")
    print(f"{'='*60}")

    mk_oracle_reg = oracle_regression(df, maker_dec, prefix="mk_", label="maker")
    tk_oracle_reg = oracle_regression(df, taker_dec, prefix="tk_", label="taker")

    # ── Oracle regressions (ElasticNet) ──
    print(f"\n{'='*60}")
    print(f"  ORACLE REGRESSIONS (ElasticNet)")
    print(f"{'='*60}")

    mk_enet_oracle = elasticnet_oracle_regression(df, maker_dec, prefix="mk_", label="maker")
    tk_enet_oracle = elasticnet_oracle_regression(df, taker_dec, prefix="tk_", label="taker")

    # ── Regression Comparison ──
    print(f"\n{'='*60}")
    print(f"  REGRESSION COMPARISON")
    print(f"{'='*60}")
    print(f"  {'Model':<40s} {'Maker':>12s} {'Taker':>12s}")
    print(f"  {'-'*64}")

    mk_r2 = mk_reg["r2"] if mk_reg else float("nan")
    tk_r2 = tk_reg["r2"] if tk_reg else float("nan")
    mk_enet_test = mk_enet_reg["test_r2"] if mk_enet_reg else float("nan")
    tk_enet_test = tk_enet_reg["test_r2"] if tk_enet_reg else float("nan")
    print(f"  {'Markout R² (Ridge, in-sample)':<40s} {mk_r2:>12.4f} {tk_r2:>12.4f}")
    print(f"  {'Markout R² (ElasticNet, test)':<40s} {mk_enet_test:>12.4f} {tk_enet_test:>12.4f}")

    mk_acc = mk_oracle_reg["accuracy"] if mk_oracle_reg else float("nan")
    tk_acc = tk_oracle_reg["accuracy"] if tk_oracle_reg else float("nan")
    mk_enet_acc = mk_enet_oracle["test_accuracy"] if mk_enet_oracle else float("nan")
    tk_enet_acc = tk_enet_oracle["test_accuracy"] if tk_enet_oracle else float("nan")
    print(f"  {'Oracle acc (Logistic, in-sample)':<40s} {mk_acc:>12.4f} {tk_acc:>12.4f}")
    print(f"  {'Oracle acc (ElasticNet, test)':<40s} {mk_enet_acc:>12.4f} {tk_enet_acc:>12.4f}")

    if mk_enet_reg:
        print(f"\n  ElasticNet sparsity: mk markout {mk_enet_reg['n_nonzero']} nonzero, "
              f"tk markout {tk_enet_reg['n_nonzero'] if tk_enet_reg else '?'} nonzero")
    if mk_enet_oracle:
        print(f"  ElasticNet sparsity: mk oracle {mk_enet_oracle['n_nonzero']} nonzero, "
              f"tk oracle {tk_enet_oracle['n_nonzero'] if tk_enet_oracle else '?'} nonzero")

    # ── Save results ──
    results = {
        "maker_oracle_markout": {
            "optimal_pnl": float(maker_pnl),
            "n_fills": int(np.sum(maker_dec != 0)),
            "n_events": len(df),
        },
        "taker_oracle_markout": {
            "optimal_pnl": float(taker_pnl),
            "n_trades": int(np.sum(taker_dec != 0)),
            "n_events": len(df),
        },
        "maker_oracle_full": {
            "optimal_pnl": float(full_maker_pnl),
            "n_fills": int(np.sum(full_maker_dec != 0)),
            "n_events": len(df),
        },
        "taker_oracle_full": {
            "optimal_pnl": float(full_taker_pnl),
            "n_trades": int(np.sum(full_taker_dec != 0)),
            "n_events": len(df),
        },
        "markout_regression_maker": mk_reg,
        "markout_regression_taker": tk_reg,
        "elasticnet_markout_regression_maker": mk_enet_reg,
        "elasticnet_markout_regression_taker": tk_enet_reg,
        "oracle_regression_maker": mk_oracle_reg,
        "oracle_regression_taker": tk_oracle_reg,
        "elasticnet_oracle_regression_maker": mk_enet_oracle,
        "elasticnet_oracle_regression_taker": tk_enet_oracle,
    }

    out_path = os.path.join(args.workdir, "oracle_dp_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results written to {out_path}")


if __name__ == "__main__":
    main()
