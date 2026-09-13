#!/usr/bin/env python3
"""
Oracle Closed-Loop: Bridge oracle DP results to SimVariations parameter sweeps.

Reads oracle_dp_results.json, identifies which features predict profitable fills,
maps them to ordex mechanism parameters, generates a SimVariations sweep,
runs it, and compares results.

Supports both maker (RelWideMM2) and taker (RelCross) ordex types.

Usage:
    python pybin/oracle_closed_loop.py \
        --oracle-results ~/scratch/.../oracle_dp_results.json \
        --base-config ~/scratch/.../pk_trade_recorder.json \
        --symbol HOOD --start 20260224 --end 20260225 \
        --workdir ~/scratch/.../closed_loop_test

    # Taker sweep with RelCross config:
    python pybin/oracle_closed_loop.py \
        --oracle-results ~/scratch/.../oracle_dp_results.json \
        --base-config ~/scratch/.../pk_relcross.json \
        --symbol HOOD --start 20260224 --end 20260313 \
        --workdir ~/scratch/.../closed_loop_taker \
        --ordex-type RelCross
"""

import argparse
import json
import os
import subprocess
import sys

import pandas as pd

# Feature -> mechanism mapping
FEATURE_MECHANISM_MAP = {
    # vol features -> vol_widen
    "vol_30s": "vol_widen",
    "vol_60s": "vol_widen",
    "vol_300s": "vol_widen",
    "vol_10s": "vol_widen",
    "vol_4s": "vol_widen",
    "vol_1s": "vol_widen",
    "vol_100ms": "vol_widen",
    # momentum features -> momentum_pred
    "mom_4s": "momentum_pred",
    "mom_10s": "momentum_pred",
    "mom_30s": "momentum_pred",
    "mom_1s": "momentum_pred",
    "mom_100ms": "momentum_pred",
    # book depth features -> liq_depth
    "book_depth_bid": "liq_depth",
    "book_depth_ask": "liq_depth",
    # trade depth features -> sweep_depth
    "trade_depth": "sweep_depth",
    "buy_trade_depth": "sweep_depth",
    "sell_trade_depth": "sweep_depth",
    "large_trade_rate": "sweep_depth",
    # remote-local spread -> premium
    "remote_mid": "premium",
    "local_mid": "premium",
}

# Mechanism -> sweep parameter definitions (shared across ordex types)
MECHANISM_SWEEPS = {
    "vol_widen": [
        {"path": ["pktraders", "ordex", "vol_widen_coef"], "val": [0, 0.1, 0.2, 0.3]},
        {"path": ["pktraders", "ordex", "vol_widen_tdc"], "val": 15},
        {"path": ["pktraders", "ordex", "vol_widen_scale"], "val": 0.003},
    ],
    "momentum_pred": [
        {"path": ["pktraders", "ordex", "momentum_pred_coef"], "val": [0, 0.05, 0.1, 0.2]},
        {"path": ["pktraders", "ordex", "momentum_pred_max_bps"], "val": 15},
    ],
    "liq_depth": [
        {"path": ["pktraders", "ordex", "liq_depth_coef"], "val": [0, 0.1, 0.2, 0.4]},
        {"path": ["pktraders", "ordex", "liq_depth_notional"], "val": 5000},
    ],
    "sweep_depth": [
        {"path": ["pktraders", "ordex", "sweep_depth_coef"], "val": [0, 0.1, 0.2, 0.3]},
        {"path": ["pktraders", "ordex", "sweep_depth_tdc"], "val": 10},
    ],
    "premium": [
        {"path": ["pktraders", "ordex", "premium_ema_coef"], "val": [0, 0.3, 0.5, 0.7]},
        {"path": ["pktraders", "ordex", "premium_tdc_s"], "val": 30},
    ],
}

# Additional sweeps specific to RelCross taker ordex
RELCROSS_EXTRA_SWEEPS = {
    "base_cross_thresh": [
        {"path": ["pktraders", "ordex", "base_cross_thresh"], "val": [0.0002, 0.0004, 0.0006, 0.001]},
    ],
    "ms_between_cross": [
        {"path": ["pktraders", "ordex", "ms_between_cross"], "val": [500, 1000, 2000]},
    ],
    "exit_adjust": [
        {"path": ["pktraders", "ordex", "exit_adjust"], "val": [0.3, 0.5, 0.7, 1.0]},
    ],
    "cross_da": [
        {"path": ["pktraders", "ordex", "cross_da_coef"], "val": [0, 0.2, 0.5, 1.0]},
        {"path": ["pktraders", "ordex", "cross_da_tdc"], "val": 5},
    ],
}


def interpret_oracle(results_path):
    """Load oracle results and map top features to mechanism recommendations.

    Returns dict: {mechanism_name: {"score": float, "features": [list]}}
    """
    with open(results_path) as f:
        results = json.load(f)

    # Aggregate feature importance across all regression results
    mechanism_scores = {}  # mechanism -> cumulative importance score
    mechanism_features = {}  # mechanism -> list of contributing features

    for key in results:
        entry = results[key]
        if entry is None or "top_features" not in entry:
            continue
        top_feats = entry["top_features"]

        for feat_name, feat_val in top_feats.items():
            # Strip prefix (mk_ or tk_)
            bare_name = feat_name
            for pfx in ("mk_", "tk_"):
                if bare_name.startswith(pfx):
                    bare_name = bare_name[len(pfx):]
                    break

            mechanism = FEATURE_MECHANISM_MAP.get(bare_name)
            if mechanism is None:
                continue

            score = abs(feat_val)
            if mechanism not in mechanism_scores:
                mechanism_scores[mechanism] = 0.0
                mechanism_features[mechanism] = []
            mechanism_scores[mechanism] += score
            if feat_name not in mechanism_features[mechanism]:
                mechanism_features[mechanism].append(feat_name)

    # Sort by total score
    ranked = sorted(mechanism_scores.items(), key=lambda x: -x[1])

    print(f"\n  Oracle Interpretation:")
    print(f"  {'Mechanism':<20s} {'Score':>10s}  Features")
    print(f"  {'-'*60}")
    recommendations = {}
    for mech, score in ranked:
        feats = mechanism_features[mech]
        print(f"  {mech:<20s} {score:>10.4f}  {', '.join(feats[:4])}")
        recommendations[mech] = {"score": score, "features": feats}

    return recommendations


def generate_sweep(recommendations, base_config_path, workdir, top_n=3,
                   ordex_type="RelWideMM2"):
    """Generate SimVariations vars.json from oracle recommendations.

    Selects the top N mechanisms by score and combines their sweep params.
    For RelCross, also includes taker-specific sweep params (base_cross_thresh,
    ms_between_cross, exit_adjust, cross_da).
    """
    os.makedirs(workdir, exist_ok=True)

    # Select top mechanisms
    top_mechs = sorted(recommendations.keys(),
                       key=lambda m: -recommendations[m]["score"])[:top_n]

    print(f"\n  Generating sweep for mechanisms: {top_mechs}")
    print(f"  Ordex type: {ordex_type}")

    vars_list = []
    for mech in top_mechs:
        sweep_params = MECHANISM_SWEEPS.get(mech, [])
        vars_list.extend(sweep_params)

    # For RelCross, add taker-specific sweeps
    if ordex_type == "RelCross":
        print(f"  Adding RelCross-specific sweeps...")
        for name, params in RELCROSS_EXTRA_SWEEPS.items():
            vars_list.extend(params)

    # Count grid size
    grid_size = 1
    for v in vars_list:
        if isinstance(v["val"], list):
            grid_size *= len(v["val"])

    print(f"  Grid size: {grid_size} variants")
    print(f"  Parameters:")
    for v in vars_list:
        name = v["path"][-1]
        val = v["val"]
        print(f"    {name}: {val}")

    # Write vars.json
    vars_path = os.path.join(workdir, "vars.json")
    with open(vars_path, "w") as f:
        json.dump(vars_list, f, indent=2)

    # Copy base config
    import shutil
    pk_dest = os.path.join(workdir, "pk.json")
    shutil.copy2(base_config_path, pk_dest)

    print(f"\n  Written: {vars_path}")
    print(f"  Copied:  {pk_dest}")

    return vars_path, pk_dest, grid_size


def run_and_compare(workdir, oracle_recommendations, symbol, start, end):
    """Run SimVariations and compare results with oracle predictions.

    Returns the best variant info.
    """
    vars_path = os.path.join(workdir, "vars.json")
    pk_path = os.path.join(workdir, "pk.json")

    script_dir = os.path.dirname(os.path.realpath(__file__))
    sv_script = os.path.join(script_dir, "SimVariations.py")
    python = sys.executable

    cmd = [
        python, sv_script,
        "--pk", pk_path,
        "--conf", vars_path,
        "--start", start,
        "--end", end,
        "--workdir", workdir,
        "--sym", symbol,
    ]

    print(f"\n  Running SimVariations...")
    print(f"  Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  SimVariations FAILED (rc={result.returncode})")
        print(f"  stderr: {result.stderr[:500]}")
        return None

    # Load results
    results_path = os.path.join(workdir, "results.txt")
    if not os.path.exists(results_path):
        print(f"  ERROR: results.txt not found at {results_path}")
        return None

    results_df = pd.read_csv(results_path)
    if "sim_score" not in results_df.columns:
        print(f"  ERROR: sim_score column not found in results")
        return None

    # Find best variant
    best_idx = results_df["sim_score"].idxmax()
    best_row = results_df.iloc[best_idx]

    print(f"\n  SimVariations Results:")
    print(f"  Best variant: {int(best_row['variant'])}")
    print(f"    sim_score = {best_row['sim_score']:.4f}")
    print(f"    avg_pnl   = {best_row['avg_pnl']:.2f}")
    print(f"    sharpe    = {best_row['sharpe']:.2f}")
    print(f"    num_trds  = {best_row['num_trds']:.0f}")

    # Show top 5 variants
    top5 = results_df.nlargest(5, "sim_score")
    print(f"\n  Top 5 variants:")
    print(top5.to_string(index=False))

    # Compare with oracle prediction
    print(f"\n  Oracle vs Actual:")
    oracle_ranked = sorted(oracle_recommendations.keys(),
                           key=lambda m: -oracle_recommendations[m]["score"])
    print(f"  Oracle-predicted best mechanisms: {oracle_ranked[:3]}")
    print(f"  Check: do the winning variants use nonzero values for these params?")

    # Check which params in best variant are nonzero
    for col in results_df.columns:
        if col in ("variant", "avg_pnl", "avg_closed_pnl", "sharpe",
                    "pct_positive", "num_trds", "sim_score"):
            continue
        val = best_row[col]
        if val != 0:
            print(f"    {col} = {val}")

    return {
        "best_variant": int(best_row["variant"]),
        "best_sim_score": float(best_row["sim_score"]),
        "best_avg_pnl": float(best_row["avg_pnl"]),
        "results_path": results_path,
    }


def main():
    parser = argparse.ArgumentParser(description="Oracle closed-loop optimization")
    parser.add_argument("--oracle-results", required=True,
                        help="Path to oracle_dp_results.json")
    parser.add_argument("--base-config", required=True,
                        help="Path to base pktrade config (pk.json)")
    parser.add_argument("--symbol", required=True, help="Trading symbol (e.g. HOOD)")
    parser.add_argument("--start", required=True, help="Start date YYYYMMDD")
    parser.add_argument("--end", required=True, help="End date YYYYMMDD")
    parser.add_argument("--workdir", required=True, help="Output directory")
    parser.add_argument("--top-n", type=int, default=3,
                        help="Number of top mechanisms to sweep (default: 3)")
    parser.add_argument("--sweep-only", action="store_true",
                        help="Only generate sweep, don't run SimVariations")
    parser.add_argument("--ordex-type", default="RelWideMM2",
                        choices=["RelWideMM2", "RelCross"],
                        help="Ordex type: RelWideMM2 (maker) or RelCross (taker)")
    args = parser.parse_args()

    # Step 1: Interpret oracle results
    print(f"{'='*60}")
    print(f"  ORACLE INTERPRETATION")
    print(f"{'='*60}")
    recommendations = interpret_oracle(args.oracle_results)

    if not recommendations:
        print("  No mechanism recommendations found. Exiting.")
        sys.exit(1)

    # Step 2: Generate sweep
    print(f"\n{'='*60}")
    print(f"  SWEEP GENERATION")
    print(f"{'='*60}")
    vars_path, pk_path, grid_size = generate_sweep(
        recommendations, args.base_config, args.workdir, top_n=args.top_n,
        ordex_type=args.ordex_type
    )

    if args.sweep_only:
        print(f"\n  Sweep generated. Run manually with:")
        print(f"  python pybin/SimVariations.py --pk {pk_path} --conf {vars_path} "
              f"--start {args.start} --end {args.end} --workdir {args.workdir} --sym {args.symbol}")
        return

    # Step 3: Run and compare
    print(f"\n{'='*60}")
    print(f"  SIMVARIATIONS RUN ({grid_size} variants)")
    print(f"{'='*60}")
    comparison = run_and_compare(
        args.workdir, recommendations, args.symbol, args.start, args.end
    )

    # Save closed-loop summary
    summary = {
        "oracle_recommendations": {k: v["score"] for k, v in recommendations.items()},
        "sweep_grid_size": grid_size,
        "comparison": comparison,
    }
    summary_path = os.path.join(args.workdir, "closed_loop_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Summary written to {summary_path}")


if __name__ == "__main__":
    main()
