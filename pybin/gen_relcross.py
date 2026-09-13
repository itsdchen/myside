#!/usr/bin/env python3
"""
Generate RelCross configs for xyz: symbols from saved_strats, then run
SimVariations parameter sweep for each.

Scans saved_strats for all xyz: symbols with a remote signal (TopBookEquity
or TopBookCme), generates a RelCross taker config per symbol, and runs a
SimVariations sweep across the specified date range.

The tempo is automatically set to fire on the remote signal's book (e.g.
BZ/TopBookCme for BRENTOIL, PLTR/TopBookEquity for PLTR). Gold and silver
use CME futures (GC, SI) instead of spot feeds for reliability.

Usage:
    PYTHON=/home/pktrade/.venvs/v1/bin/python

    # List available symbols (no sims):
    $PYTHON pybin/gen_relcross.py --outdir /tmp/test --dry-run

    # Sweep with custom vars and random sampling (recommended for large grids):
    $PYTHON pybin/gen_relcross.py --outdir ~/scratch/relcross_sweep \
        --vars vars.json --max-variants 500 --random-sample \
        --symbols bz,cl,natgas,copper,pltr,tsla \
        --start 20260310 --end 20260402

    # Sweep with deterministic downsampling (old behavior):
    $PYTHON pybin/gen_relcross.py --outdir ~/scratch/relcross_sweep \
        --vars vars.json --max-variants 300 \
        --symbols cl,pltr --start 20260310 --end 20260402

Sweep vars format (same as SimVariations, cartesian product of all val lists):
    [
      {"path": ["pktraders", "ordex", "base_cross_thresh"], "val": [0.0003, 0.0005, 0.001]},
      {"path": ["pktraders", "ordex", "premium_ema_coef"], "val": [0, 0.5, 1.0]},
      {"path": ["pktraders", "ordex", "exit_adjust"], "val": [0.5]}
    ]

Output:
    <outdir>/<symbol>/pk_relcross.json   - generated config
    <outdir>/<symbol>/sweep/results.txt  - per-symbol sweep results
    <outdir>/aggregate_results.txt       - best variant per symbol, sorted by sim_score
"""
import json
import math
import os
import subprocess
import sys
import glob
import argparse

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SAVED_STRATS = os.path.join(REPO_ROOT, "overmind", "for_live", "saved_strats")
SIMVAR = os.path.join(SCRIPT_DIR, "SimVariations.py")
PYTHON = sys.executable

# Defaults below reflect the Apr 2026 RelCross multi-symbol postmortem sweep
# (~15k sims across COPPER/CL/BZ/NATGAS/SILVER). Key findings baked in:
#   - exit_adjust=0.3 best for 4/5 symbols (BZ prefers 1.0 — see ORDEX_OVERRIDES)
#   - premium_ema_coef >= 0.8 always preferred; values < 0.7 were strictly dominated
#   - vol_norm_coef=1.0, vol_norm_tdc=60 is the cross-symbol vol-adaptive sweet spot
#   - vol_widen / snr_thresh_coef / trade_rate_widen_coef all stay off (strictly worse or inert)
#   - cross_price_mode=2 was the best value from earlier March sweep
# Working doc: ~/scratch/cross_postmortem_v3/latency_sweep/working_doc.md
RELCROSS_ORDEX_TEMPLATE = {
    "type": "RelCross",
    "markets": ["Hyperliquid"],
    "max_pos": "40",
    "order_size": "20",
    "tgt_order_notional": 1000,
    "tgt_maxpos_notional": 2000,  # keep max_pos at 2 * order_size
    "base_cross_thresh": 0.0006,
    "exit_adjust": 0.3,  # tightened from 1.0 post-sweep; BZ overridden to 1.0
    "per_order_widen_frac": 0.1,
    "ms_between_cross": 1000,  # avoid burst-firing 5 IOCs on one dislocation
    "premium_ema_coef": 0.8,  # raised from 0.5; <0.7 always lost in sweep
    "premium_tdc_s": 30,
    "cross_price_mode": 2,  # best from March sweep
    # Spread-aware entry filter: only fire entries if dislocation exceeds
    # spread by this many bps. 0 = disabled. On COPPER, vol_norm already
    # subsumes this; may help equities where vol_norm is set low.
    "min_edge_over_spread_bps": 0,
    # Remote momentum extrapolation on pred_px.
    "remote_mom_coef": 0,
    "remote_mom_tdc_ms": 1000,
    # Vol mechanisms. vol_norm dominates vol_widen ~7x at peak (COPPER sweep).
    "vol_widen_coef": 0,
    "vol_widen_tdc": 4,
    "vol_widen_scale": 0.001,
    "vol_norm_coef": 1.0,  # enabled — the cross-symbol winner
    "vol_norm_tdc": 60,    # longer tdc better than the old 15s default
    "vol_ratio_coef": 0,
    "vol_ratio_short_tdc": 5,
    "vol_ratio_long_tdc": 60,
    # Passive exit.
    "passive_exit_enabled": False,
    "passive_exit_pct": 0.002,
    "passive_exit_style": 0,
    "passive_exit_decay_per_min": 0.0005,
    "passive_exit_min_pct": 0.0002,
    # Hold time exit pressure. TESTED on BZ (Apr 2026 sweep) and found to be
    # strictly HARMFUL — every top-20 variant had hd=0, and mean net at hd=60/180
    # was ~9× worse than hd=0. Hold decay forced premature exits that locked in
    # losses; waiting for real reversion was better. Keep disabled by default.
    "hold_decay_tdc_s": 0,
    "hold_decay_floor": 0.1,
    "forced_exit_after_s": 0,
    # Impulse curvicity.
    "curv_impulse_tdc": 1,
    "curv_impulse_coef": 0,
    # Pred momentum sided widening.
    "pred_momentum_tdc": 30,
    "pred_momentum_coef": 0,
    # Signal-proportional sizing.
    "size_signal_max_mult": 0,
    # SNR-based threshold scaling.
    "snr_thresh_coef": 0,
    "snr_vol_tdc": 5,
    # Flip-through: size to flip position when signal exceeds full threshold.
    "flip_through": False,
    # Trade rate conditioning: widen when trade rate is elevated.
    "trade_rate_widen_coef": 0,
    "trade_rate_tdc": 10,
    "trade_rate_baseline_tdc": 120,
}


def downsample_vars(vars_data, max_variants):
    """Reduce sweep grid so total product <= max_variants.

    Same algorithm as coverage_autosearch._downsample_dims: repeatedly trims
    the largest dimension by 1, then evenly subsamples each dimension.
    """
    sizes = [len(v["val"]) if isinstance(v["val"], list) else 1 for v in vars_data]
    total = math.prod(sizes)

    if total <= max_variants:
        return vars_data

    # Iteratively shrink the largest dim
    while math.prod(sizes) > max_variants:
        max_idx = -1
        max_size = 1
        for i, s in enumerate(sizes):
            if s > max_size:
                max_size = s
                max_idx = i
        if max_idx == -1:
            break
        sizes[max_idx] -= 1

    # Subsample each dim evenly to the target size
    result = []
    for v, target_size in zip(vars_data, sizes):
        vals = v["val"]
        if not isinstance(vals, list) or target_size >= len(vals):
            result.append(v)
        else:
            if target_size <= 1:
                # Pick the middle value when only one slot remains
                indices = [len(vals) // 2]
            else:
                indices = [
                    round(i * (len(vals) - 1) / (target_size - 1))
                    for i in range(target_size)
                ]
            result.append({"path": v["path"], "val": [vals[i] for i in indices]})
    return result


def find_all_symbols():
    """Find all xyz: symbols with a remote signal in saved_strats."""
    symbols = []
    seen = set()

    for sym_dir in sorted(glob.glob(os.path.join(SAVED_STRATS, "*"))):
        sym = os.path.basename(sym_dir)
        if sym in seen:
            continue

        # Search all JSON configs (including subdirs like testnet/)
        candidates = []
        for root, dirs, files in os.walk(sym_dir):
            for f in files:
                if f.endswith(".json") and f.startswith("pk_"):
                    candidates.append(os.path.join(root, f))

        best_conf = None
        best_priority = -1

        for conf_path in candidates:
            try:
                with open(conf_path) as f:
                    d = json.load(f)
                trader = d["pktraders"][0]
                traded_symbol = trader["traded_symbol"]

                # Only xyz: symbols (skip unit: like ES)
                if not traded_symbol.startswith("xyz:"):
                    continue

                # Find remote signal
                has_remote = False
                for s in trader.get("signals", []):
                    for b in s.get("books", []):
                        if b in ("TopBookEquity", "TopBookCme"):
                            has_remote = True
                            break

                if not has_remote:
                    continue

                # Prioritize: usday > 2usday > allday > day > anything else
                fname = os.path.basename(conf_path)
                if "usday" in fname and "2usday" not in fname:
                    priority = 4
                elif "2usday" in fname:
                    priority = 3
                elif "allday" in fname:
                    priority = 2
                elif "day" in fname:
                    priority = 1
                else:
                    priority = 0

                if priority > best_priority:
                    best_priority = priority
                    best_conf = conf_path

            except (json.JSONDecodeError, KeyError, IndexError):
                continue

        if best_conf:
            seen.add(sym)
            symbols.append((sym, best_conf))

    return symbols


# Override remote signal to use CME futures instead of spot equity feed.
# Our spot gold/silver feeds have reliability issues (detected historically),
# while CME GC/SI feeds through our vendor are more reliable. The local HL
# price tracks spot, so there will be a futures/spot basis — use
# premium_ema_coef=1.0 for these symbols to account for it.
CME_REMOTE_OVERRIDE = {
    "xyz:SILVER": ("SI", "TopBookCme"),
    "xyz:GOLD":   ("GC", "TopBookCme"),
}

# Per-symbol ordex overrides applied after the template. Derived from the
# Apr 2026 sweep — each entry is a parameter that wants to differ from the
# common default for a specific reason.
ORDEX_OVERRIDES = {
    # BZ best variant from Apr 2026 bz_logic_sweep2 (post premium-EMA logic change).
    # Best total over 20 days: +$454 at these settings. Still marginal ($23/day,
    # 7/20 loss days, worst -$71) — BZ is not a great RelCross symbol.
    "xyz:BRENTOIL": {
        "base_cross_thresh": 0.0017,
        "exit_adjust": 0.7,
        "premium_ema_coef": 0.9,
        "vol_norm_coef": 3.0,
        "vol_norm_tdc": 120,
    },
    # Futures-vs-spot basis is large for SI/GC, so we want full premium
    # tracking (no discount). The sweep forced 1.0 for SILVER and it won.
    "xyz:SILVER":   {"premium_ema_coef": 1.0},
    "xyz:GOLD":     {"premium_ema_coef": 1.0},
}


def generate_relcross_config(src_conf_path, ioc_ord_latency=0.7):
    """Generate a RelCross config from an existing saved_strats config."""
    with open(src_conf_path) as f:
        d = json.load(f)

    trader = d["pktraders"][0]
    traded_symbol = trader["traded_symbol"]

    # Find signals and tempo
    local_sig = None
    remote_sig = None
    tempo = None

    for s in trader["signals"]:
        books = s.get("books", [])
        if any(b in ("TopBookEquity", "TopBookCme") for b in books):
            if "remote" in s.get("name", "").lower() or s["type"] == "SigQuoteMid":
                remote_sig = s
        elif s["type"] == "SigMid" and "local" in s.get("name", "").lower():
            local_sig = s
        elif s["type"] == "SigMid" and local_sig is None:
            local_sig = s

    # Override remote signal to CME if configured.
    if traded_symbol in CME_REMOTE_OVERRIDE:
        cme_sym, cme_book = CME_REMOTE_OVERRIDE[traded_symbol]
        name = traded_symbol.split(":")[1].lower()
        remote_sig = {
            "name": f"remote_mid_{name}",
            "type": "SigQuoteMid",
            "symbol": cme_sym,
            "books": [cme_book],
        }

    # Build tempo tied to the remote signal's book so we fire when the
    # remote price updates (not on unrelated BTC trades).
    remote_book = remote_sig["books"][0]
    remote_sym = remote_sig.get("symbol", remote_sig.get("name", "").split("_")[-1])
    name = traded_symbol.split(":")[1].lower()
    tempo = {
        "name": f"remote_tempo_{name}",
        "type": "FinalTempo",
        "symbol": remote_sym,
        "markets": [remote_book],
    }

    if not local_sig or not remote_sig:
        return None, None

    # Determine time window from source config
    settings = d.get("settings", {})
    start_t = settings.get("start_t", "09:45:00 America/New_York")
    end_t = settings.get("end_t", "16:00:00 America/New_York")

    # Build RelCross config
    ordex = dict(RELCROSS_ORDEX_TEMPLATE)
    ordex["trade_caller"] = tempo["name"]
    ordex["local_sig"] = local_sig["name"]
    ordex["remote_sig"] = remote_sig["name"]

    # Apply per-symbol ordex overrides from the sweep findings.
    if traded_symbol in ORDEX_OVERRIDES:
        ordex.update(ORDEX_OVERRIDES[traded_symbol])

    # Determine commission tier
    comm_tiers = {"Hyperliquid": "HIP3_3"}

    config = {
        "settings": {
            "commissions": {
                "tiers": comm_tiers,
                "promo_tier": "Silver",
                "use_promo": True,
            },
            "start_t": start_t,
            "end_t": end_t,
        },
        "simulation": {
            "Hyperliquid": {
                "cxl_frac_ahead": 0.3,
                "use_one_way_latency": False,
                "sim_latency_secs": 1.0,
                # Latency is per-deployment, not per-symbol. 0.7 matched COPPER
                # on a quiet host. During usday on a contended gateway (e.g.
                # gf2 running equities cross alongside many other strats) live
                # p50 RTT is 850-1100ms. Override with --ioc-ord-latency.
                "ioc_ord_latency": ioc_ord_latency,
                "cxl_ord_latency": ioc_ord_latency,
            }
        },
        "pktraders": [
            {
                "traded_symbol": traded_symbol,
                "enabled": True,
                "size_mult": 1,
                "risk": {
                    "max_notional": 10000000,
                    "max_position": "300",
                    "max_orders": 30,
                    "base_currency": "USDT",
                    "global_inherit": True,
                    "min_pnl": -1000,
                    "fv_limit": 2,
                    "timeout_minfv": 900,
                },
                "ordex": ordex,
                "signals": [local_sig, remote_sig],
                "tempos": [tempo],
                "extra_subs": [],
            }
        ],
    }

    return config, traded_symbol


def main():
    parser = argparse.ArgumentParser(
        description="Generate RelCross configs and run SimVariations sweeps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --dry-run                                    # list symbols, no sims
  %(prog)s --start 20260224 --end 20260313              # sweep all symbols
  %(prog)s --symbols hood,tsla --start 20260224 --end 20260313
  %(prog)s --vars my_vars.json --start 20260224 --end 20260313
""")
    parser.add_argument("--outdir", required=True,
                        help="Output directory for configs and results")
    parser.add_argument("--start", default="20260224",
                        help="Start date YYYYMMDD (default: 20260224)")
    parser.add_argument("--end", default="20260313",
                        help="End date YYYYMMDD (default: 20260313)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Just generate configs, don't run sweeps")
    parser.add_argument("--symbols", type=lambda s: s.split(","),
                        help="Comma-separated symbols to run (default: all)")
    parser.add_argument("--vars", default=None,
                        help="Path to sweep vars JSON (default: built-in thresh x premium grid)")
    parser.add_argument("--timeout", type=int, default=0,
                        help="Per-symbol timeout in seconds (0 = no timeout)")
    parser.add_argument("--max-variants", type=int, default=0,
                        help="Subsample to at most N variants (0 = no limit)")
    parser.add_argument("--random-sample", action="store_true",
                        help="Use random sampling instead of deterministic downsampling")
    parser.add_argument("--ioc-ord-latency", type=float, default=0.7,
                        help="Sim ioc_ord_latency (and cxl_ord_latency) in seconds. "
                             "Default 0.7 matches quiet-host COPPER calibration; use "
                             "~0.9 for usday on contended gateways (gf2-style).")
    # Swarmhost dispatch — opt in. Default off keeps existing local behavior.
    parser.add_argument("--remote", action="store_true",
                        help="Dispatch each per-symbol sweep via swarmhost (forwarded to "
                             "SimVariations --remote).")
    parser.add_argument("--hosts", default=None,
                        help="Path to swarmhost hosts.yaml (forwarded to SimVariations).")
    parser.add_argument("--chunk-size", type=int, default=50,
                        help="Variants per dispatch chunk when --remote.")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    symbols = find_all_symbols()
    print(f"Found {len(symbols)} xyz: symbols with remote signal")
    for sym, conf in symbols:
        print(f"  {sym}: {conf}")

    if args.symbols:
        symbols = [(s, p) for s, p in symbols if s in args.symbols]
        print(f"Filtered to {len(symbols)} symbols: {[s for s,_ in symbols]}")

    # Default sweep vars
    if args.vars:
        vars_path = args.vars
    else:
        vars_path = os.path.join(args.outdir, "vars_default.json")
        # Default sweep grid, informed by the Apr 2026 postmortem + forward-walk.
        # Key findings baked in:
        #   - vol_norm is the primary vol mechanism (dominates vol_widen on most symbols)
        #   - vol_widen_additive included as secondary axis — helpful on some symbols,
        #     harmful on COPPER. Worth testing per-symbol.
        #   - min_edge_over_spread_bps: spread-aware entry filter. No effect when
        #     vol_norm is aggressive, but may help symbols with low vol_norm (equities).
        #   - ms_between_cross locked to 1000 in template; not swept (0 was always bad)
        #   - premium_ema_coef <0.7 always lost; 0.9-1.0 is the sweet spot
        # Raw grid is large (~18k variants); use --max-variants --random-sample.
        vars_data = [
            {"path": ["pktraders", "ordex", "base_cross_thresh"],
             "val": [0.0003, 0.0005, 0.0008, 0.0012, 0.002]},
            {"path": ["pktraders", "ordex", "premium_ema_coef"],
             "val": [0.8, 0.9, 1.0]},
            {"path": ["pktraders", "ordex", "exit_adjust"],
             "val": [0.3, 0.5, 0.7, 1.0]},
            {"path": ["pktraders", "ordex", "vol_norm_coef"],
             "val": [1.0, 1.5, 2.5, 3.5, 5.0]},
            {"path": ["pktraders", "ordex", "vol_norm_tdc"],
             "val": [30, 60, 120, 180]},
            {"path": ["pktraders", "ordex", "vol_widen_coef"],
             "val": [0, 0.5, 1.0]},
            {"path": ["pktraders", "ordex", "min_edge_over_spread_bps"],
             "val": [0, 1.0, 2.0]},
            # Added Apr 24 2026 after equities cross reject-rate postmortem.
            # Mode 2 sends at pred_px - thresh (barely crosses the book); on
            # contended-gateway RTT this lost ~95% of orders to reject. Mode 1
            # sends at pred_px (full-thresh aggression past the touch).
            {"path": ["pktraders", "ordex", "cross_price_mode"],
             "val": [1, 2]},
        ]
        with open(vars_path, "w") as f:
            json.dump(vars_data, f, indent=2)

    # Downsample if --max-variants is set (skip if --random-sample, SimVariations handles it)
    if args.max_variants > 0 and not args.random_sample:
        with open(vars_path) as f:
            vars_data = json.load(f)
        total_before = math.prod(len(v["val"]) if isinstance(v["val"], list) else 1
                                 for v in vars_data)
        if total_before > args.max_variants:
            vars_data = downsample_vars(vars_data, args.max_variants)
            total_after = math.prod(len(v["val"]) if isinstance(v["val"], list) else 1
                                    for v in vars_data)
            print(f"Downsampled: {total_before} -> {total_after} variants (max {args.max_variants})")
            vars_path = os.path.join(args.outdir, "vars_downsampled.json")
            with open(vars_path, "w") as f:
                json.dump(vars_data, f, indent=2)
    elif args.random_sample and args.max_variants > 0:
        with open(vars_path) as f:
            vars_data = json.load(f)
        total = math.prod(len(v["val"]) if isinstance(v["val"], list) else 1
                          for v in vars_data)
        print(f"Random sample: will run {args.max_variants} of {total} variants per symbol")

    # Generate configs and run sweeps
    results = {}
    failed = []
    for sym, src_path in symbols:
        sym_dir = os.path.join(args.outdir, sym)
        os.makedirs(sym_dir, exist_ok=True)

        config, traded_sym = generate_relcross_config(src_path, args.ioc_ord_latency)
        if config is None:
            print(f"  SKIP {sym}: could not extract signals/tempo from {src_path}")
            failed.append((sym, "no signals/tempo"))
            continue

        pk_path = os.path.join(sym_dir, "pk_relcross.json")
        with open(pk_path, "w") as f:
            json.dump(config, f, indent=2)

        print(f"  Generated {pk_path} ({traded_sym})")

        if args.dry_run:
            continue

        # Ensure market data exists for all signals and the traded symbol.
        md_script = os.path.join(REPO_ROOT, "overmind", "strat_main", "tools", "md_exists.py")
        md_checks = set()
        # Traded symbol on its market.
        mkt = config["pktraders"][0]["ordex"]["markets"][0]
        md_checks.add((traded_sym, mkt))
        # All signals (local + remote) on their respective books.
        for sig in config["pktraders"][0]["signals"]:
            sig_sym = sig.get("symbol", traded_sym.split(":")[-1])
            for book in sig.get("books", []):
                md_checks.add((sig_sym, book))
        # Also check the tempo's market.
        for tempo in config["pktraders"][0]["tempos"]:
            tempo_sym = tempo.get("symbol", "")
            for tmkt in tempo.get("markets", []):
                if tempo_sym:
                    md_checks.add((tempo_sym, tmkt))

        for md_sym, md_mkt in md_checks:
            try:
                subprocess.run(
                    [PYTHON, md_script,
                     "--sym", md_sym, "--market", md_mkt,
                     "--start", args.start, "--end", args.end],
                    input="y\n", text=True, timeout=300,
                )
            except Exception as e:
                print(f"  Warning: md_exists failed for {md_sym}/{md_mkt}: {e}")

        # Run sweep
        sweep_dir = os.path.join(sym_dir, "sweep")
        cmd = [
            PYTHON, SIMVAR,
            "--conf", vars_path,
            "--pk", pk_path,
            "--start", args.start,
            "--end", args.end,
            "--workdir", sweep_dir,
        ]
        if args.random_sample and args.max_variants > 0:
            cmd += ["--random-sample", str(args.max_variants)]
        if args.remote:
            cmd += ["--remote", "--chunk-size", str(args.chunk_size)]
            if args.hosts:
                cmd += ["--hosts", args.hosts]

        print(f"  Running sweep for {sym}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=args.timeout if args.timeout > 0 else None,
                                    cwd=sym_dir)
            results_path = os.path.join(sweep_dir, "results.txt")
            if os.path.exists(results_path):
                with open(results_path) as f:
                    lines = f.readlines()
                print(f"  {sym}: {len(lines)-1} variants completed")
                results[sym] = results_path
            else:
                print(f"  {sym}: no results.txt generated")
                if result.stderr:
                    err_lines = result.stderr.strip().split('\n')
                    for line in err_lines[-3:]:
                        print(f"    ERR: {line}")
                failed.append((sym, "no results"))
        except subprocess.TimeoutExpired:
            print(f"  {sym}: TIMEOUT ({args.timeout}s)")
            # Still check for partial results
            results_path = os.path.join(sweep_dir, "results.txt")
            if os.path.exists(results_path):
                with open(results_path) as f:
                    lines = f.readlines()
                if len(lines) > 1:
                    print(f"  {sym}: {len(lines)-1} partial variants before timeout")
                    results[sym] = results_path
            failed.append((sym, "timeout"))
        except Exception as e:
            print(f"  {sym}: ERROR {e}")
            failed.append((sym, str(e)))

    # Aggregate results
    if not args.dry_run:
        print("\n\n=== SUMMARY ===")
        print(f"{'sym':10s} {'traded':16s} {'best_var':>8s} {'thresh':>8s} {'prem_ema':>8s} {'avg_pnl':>8s} {'sharpe':>8s} {'trades':>6s} {'sim_score':>10s}")
        print("-" * 95)

        all_best = []
        for sym in sorted(results.keys()):
            rpath = results[sym]
            try:
                with open(rpath) as f:
                    lines = f.readlines()
                if len(lines) < 2:
                    continue
                header = lines[0].strip().split(",")
                best_score = -999
                best_row = None
                for line in lines[1:]:
                    vals = line.strip().split(",")
                    row = dict(zip(header, vals))
                    score = float(row.get("sim_score", -999))
                    if score > best_score:
                        best_score = score
                        best_row = row
                if best_row:
                    # Get traded symbol
                    pk_path = os.path.join(args.outdir, sym, "pk_relcross.json")
                    traded = ""
                    try:
                        with open(pk_path) as f:
                            traded = json.load(f)["pktraders"][0]["traded_symbol"]
                    except Exception:
                        pass
                    print(f"{sym:10s} {traded:16s} {best_row['variant']:>8s} {best_row['base_cross_thresh']:>8s} "
                          f"{best_row.get('premium_ema_coef','?'):>8s} {best_row['avg_pnl']:>8s} "
                          f"{best_row['sharpe']:>8s} {best_row['num_trds']:>6s} {best_row['sim_score']:>10s}")
                    all_best.append((sym, traded, best_row))
            except Exception as e:
                print(f"{sym:10s} ERROR: {e}")

        if failed:
            print(f"\nFailed ({len(failed)}):")
            for sym, reason in failed:
                print(f"  {sym}: {reason}")

        # Write aggregate results
        agg_path = os.path.join(args.outdir, "aggregate_results.txt")
        with open(agg_path, "w") as f:
            f.write("sym,traded_symbol,best_variant,base_cross_thresh,premium_ema_coef,avg_pnl,avg_closed_pnl,sharpe,pct_positive,num_trds,sim_score\n")
            for sym, traded, row in all_best:
                f.write(f"{sym},{traded},{row['variant']},{row['base_cross_thresh']},"
                        f"{row.get('premium_ema_coef','')},{row['avg_pnl']},{row['avg_closed_pnl']},"
                        f"{row['sharpe']},{row['pct_positive']},{row['num_trds']},{row['sim_score']}\n")
        print(f"\nAggregate results saved to {agg_path}")


if __name__ == "__main__":
    main()
