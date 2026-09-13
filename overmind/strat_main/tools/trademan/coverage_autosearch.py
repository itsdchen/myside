#!/usr/bin/env python3
"""
Coverage gap autosearch — find under-covered HIP3 XYZ symbols and run param sweeps.

The coverage report (coverage_report.py) tells us which symbols are traded and
during which sessions, but it doesn't act on the gaps.  When a symbol has only
0 or 1 instances in a session it's a single point of failure.  This tool
automates the process of:

  1. Identifying those gaps (symbols × sessions with count <= 1).
  2. Pulling the existing base config that already covers each symbol.
  3. Generating candidate parameter variations around the current values.
  4. Running sims via SimVariations.sim_grid() and scoring the results.

Usage examples
──────────────
  # Generate a spec from current coverage gaps (default 14 sim days):
  coverage_autosearch.py --generate-spec

  # Generate with more sim days and force-include specific symbols:
  coverage_autosearch.py --generate-spec --sim-days 21 --symbols BRENTOIL,PLATINUM

  # Generate with a specific vol mechanism mode (sweeps its params for all symbols):
  coverage_autosearch.py --generate-spec --vol-mode vol_norm

  # Generate to a custom output directory:
  coverage_autosearch.py --generate-spec --output-dir ~/scratch/my_search

  # Run a (possibly hand-edited) spec:
  coverage_autosearch.py --run-spec ~/scratch/coverage_autosearch/YYYYMMDD/coverage_search_spec.py

Two-step workflow
─────────────────
Step 1 — generate a spec:

    coverage_autosearch.py --generate-spec [--sim-days 14] [--output-dir DIR]
                           [--symbols SYM1,SYM2] [--max-stale-days 4]

  Runs the coverage analysis, finds every (symbol, session) pair with <= 1
  instance, locates the best existing config for each, reads its ordex params,
  and writes an editable Python spec file to:

      ~/scratch/coverage_autosearch/YYYYMMDD/coverage_search_spec.py

  The spec contains per-(symbol, session, vol_mode) entries with:
    - base_config path (the existing pk_*.json it derived from)
    - sessions that are under-covered
    - dims: parameter sweep dimensions auto-generated from the base config's
      current values (±20% / 5 steps for floats, ±1 for ints)
    - vol_mode: which vol mechanism to use (each mode is a separate sweep)

  Vol modes (mutually exclusive — one per sweep entry):
    - no_vol:          baseline, no vol widening
    - vol_widen_add:   mechanism 1 additive (sweeps vol_widen_coef, vol_widen_tdc)
    - vol_widen_mult:  mechanism 1 multiplicative (sweeps vol_widen_coef, tdc, scale)
    - vol_norm:        mechanism 2 normalized vol (sweeps vol_norm_coef, vol_norm_tdc)

  You can (and should) hand-edit the spec before running it — add or remove
  symbols, adjust the sweep ranges, swap in custom dimensions, change sim_days,
  delete vol modes you don't want to test, etc.  The file is plain Python so
  anything goes.

Dimension formats
─────────────────
Two formats are supported in the spec, and can be mixed freely:

  "dims" — explicit value lists (classic SimVariations style):
      (["pktraders", "ordex", "place_thresh"], [0.0003, 0.0004, 0.0005])

  "range_dims" — range-based, resolved against the base config's current value:
      (["pktraders", "ordex", "per_order_widen_frac"], {"min": 0.0, "max": 3.0, "steps": 7})
      (["pktraders", "ordex", "ladder_one_sided"],     {"values": [True, False]})

  For range_dims with min/max/steps:
    - Reads the current value from the base config
    - Generates `steps` values centered on current, clamped to [min, max]
    - neighborhood_pct (default 0.3 = ±30%) controls how far from current to explore
    - Override per-dim: {"min": 0, "max": 1, "steps": 11, "neighborhood_pct": 0.5}
  For range_dims with "values": uses those values directly (good for booleans).

  Both "dims" and "range_dims" can coexist in the same symbol spec — they
  get merged into a single overrides list at run time.

Step 2 — run the spec:

    coverage_autosearch.py --run-spec ~/scratch/coverage_autosearch/YYYYMMDD/coverage_search_spec.py

  Imports the (possibly edited) spec, then for each entry:
    - Loads the base config and filters to that symbol's pktrader section.
    - Applies vol mode dims and fixed overrides (zeroing out other mechanisms).
    - Resolves available sim dates via dates_avail (falls back to weekday list).
    - Converts the spec's dims into SimVariations overrides_dct format.
    - If variants exceed max_variants, randomly samples (default) or
      deterministically downsamples.
    - Calls sim_grid() with resume=True so interrupted runs can continue.
    - Parses the results.txt and ranks variants by sim_score.

  Outputs land in:
      ~/scratch/coverage_autosearch/YYYYMMDD/
        results/<symbol>_<session>_<vol_mode>/<spec_basename>/
                                                 per-spec sim scratch + results.txt.
                                                 Per-spec subdir isolates the
                                                 sim_results.json cache so two
                                                 different specs sharing the
                                                 same workdir can't return
                                                 stale results from each other.
        summary.txt                              cross-entry top-5 ranking

Dependencies
────────────
  coverage_report.py   — fetch_all_xyz_symbols, load_latest_configs,
                         build_coverage, build_disabled_set, session helpers
  SimVariations.py     — sim_grid() for running parameter sweeps
  util/chron.py        — dates_list, dates_avail for sim date ranges
  util/sim.py          — sim_date_range (called inside sim_grid)
"""

import argparse
import commentjson
import importlib.util
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from coverage_report import (
    SESSIONS, LOCAL_BASE, _to_minutes, _parse_time, _session_overlaps,
    fetch_all_xyz_symbols, load_latest_configs, build_coverage, build_disabled_set,
)
from util.chron import dates_list, dates_avail


# Sessions to skip when generating specs. Gaps in these sessions are ignored.
SKIP_SESSIONS = ["Pre-US", "Post-US"]

# ── Default tuning dimensions ────────────────────────────────────────────────
# These ordex params get auto-ranged (swept) when generating specs.
DEFAULT_TUNE_PARAMS = [
    "place_thresh",
    "enter_thresh",
    "ladder_one_sided",
    "per_order_widen_frac",
    "premium_ema_coef",
    "curv_impulse_coef",
    "cancel_buffer",
    "pred_momentum_coef",
    "premium_tdc_s",
    "place_thresh_mode",
]

# Valid ranges for ordex params: (min, max).
# Auto-generated sweeps are clamped to these bounds.
# Omitted params have no bounds (±20% around current value).
PARAM_BOUNDS = {
    "premium_ema_coef":       (0.5, 1.0),
    "curv_impulse_coef":      (0.0, 5.0),
    "place_thresh":           (0.0001, 0.003),
    "enter_thresh":           (0.0, 0.01),
    "per_order_widen_frac":   (0.5, 5.0),
    "cancel_buffer":          (0.05, 0.5),
    "pred_momentum_coef":     (0.0, 3.0),
    "premium_tdc_s":          (5.0, 300.0),
    "place_thresh_mode":      (0, 2),
}

# Force param types — overrides whatever type is in the config JSON.
# "float", "int", "bool"
PARAM_TYPES = {
    "premium_ema_coef":       "float",
    "curv_impulse_coef":      "float",
    "place_thresh":           "float",
    "enter_thresh":           "float",
    "per_order_widen_frac":   "float",
    "ladder_one_sided":       "bool",
    "max_back_levels":        "int",
    "cancel_buffer":          "float",
    "pred_momentum_coef":     "float",
    "premium_tdc_s":          "float",
    "place_thresh_mode":      "int",
}

# Max variants per symbol. If the product of all dim sizes exceeds this,
# dims are uniformly downsampled to fit. Configurable in spec via max_variants.
DEFAULT_MAX_VARIANTS = 500

# Session name -> (start_t, end_t) for config settings.
# These match the SESSIONS windows in coverage_report.py.
SESSION_TIMES = {
    "Overnight":  ("18:00:00 America/New_York", "04:00:00 America/New_York"),
    "Pre-US":     ("04:00:00 America/New_York", "09:30:00 America/New_York"),
    "US Day":     ("09:30:00 America/New_York", "16:00:00 America/New_York"),
    "Post-US":    ("16:00:00 America/New_York", "18:00:00 America/New_York"),
}

# These ordex params get set to a fixed value (not swept).
DEFAULT_FIXED_OVERRIDES = {
    "max_back_levels": 3,
    "restrict_near_book": True,
}

# ── Vol mechanism modes ──────────────────────────────────────────────────────
# Each mode is a separate sweep. Only one vol mechanism is active per mode;
# the others are zeroed out via fixed overrides.
# "dims": params to sweep for this mode
# "fixed": params to fix (including zeroing out other mechanisms)
VOL_MODES = {
    "no_vol": {
        "description": "Baseline — no vol widening",
        "dims": [],
        "fixed": {
            "vol_widen_coef": 0,
            "vol_norm_coef": 0,
            "vol_ratio_coef": 0,
        },
    },
    "vol_widen_add": {
        "description": "Mechanism 1 additive: vol_widen with vol_widen_additive=True",
        "dims": [
            ("vol_widen_coef", (0.1, 3.0), "float", 7),
            ("vol_widen_tdc",  (2, 15), "float", 2),
        ],
        "fixed": {
            "vol_widen_additive": True,
            "vol_norm_coef": 0,
            "vol_ratio_coef": 0,
        },
    },
    "vol_widen_mult": {
        "description": "Mechanism 1 multiplicative: vol_widen with vol_widen_additive=False",
        "dims": [
            ("vol_widen_coef", (0.1, 3.0), "float", 7),
            ("vol_widen_tdc",  (2, 15), "float", 2),
            ("vol_widen_scale", (0.0003, 0.003), "float", 7),
        ],
        "fixed": {
            "vol_widen_additive": False,
            "vol_norm_coef": 0,
            "vol_ratio_coef": 0,
        },
    },
    "vol_norm": {
        "description": "Mechanism 2: normalized vol (sigma-per-sqrt-s) widening",
        "dims": [
            ("vol_norm_coef", (0.1, 5.0), "float", 7),
            ("vol_norm_tdc",  (5, 60), "float", 2),
        ],
        "fixed": {
            "vol_widen_coef": 0,
            "vol_ratio_coef": 0,
        },
    },
}


def _generate_vol_mode_dims(mode_name):
    """Generate (path, values) dims for a vol mode's sweep params."""
    mode = VOL_MODES[mode_name]
    dims = []
    for entry in mode["dims"]:
        param_name, bounds, ptype, steps = entry
        path = ["pktraders", "ordex", param_name]
        lo, hi = bounds
        step_size = (hi - lo) / (steps - 1) if steps > 1 else 0
        if ptype == "float":
            values = [round(lo + i * step_size, 8) for i in range(steps)]
        elif ptype == "int":
            values = sorted(set(int(round(lo + i * step_size)) for i in range(steps)))
        else:
            values = [lo + i * step_size for i in range(steps)]
        dims.append((path, values))
    return dims


def _generate_vol_mode_fixed(mode_name):
    """Generate (path, value) fixed overrides for a vol mode."""
    mode = VOL_MODES[mode_name]
    fixed = []
    for param_name, val in mode["fixed"].items():
        path = ["pktraders", "ordex", param_name]
        fixed.append((path, val))
    return fixed


def _config_path(local_base, machine, group, strat, config, date_str):
    """Reconstruct the full path to a dated config file."""
    return os.path.join(local_base, machine, group, strat, f"{config}.{date_str}")


def _build_config_index(configs, local_base):
    """Build index: symbol -> list of config entries with file paths.

    Each entry includes the file path and time window, for matching to sessions.
    Only includes enabled, non-weekend-only traders.
    """
    index = defaultdict(list)
    for cfg in configs:
        if not cfg["start_t"] or not cfg["end_t"]:
            continue
        # Skip weekend-only strats
        if cfg["machine"] != "gf0" and "rel_btc" in cfg["strat"]:
            continue
        try:
            start_m = _parse_time(cfg["start_t"])
            end_m = _parse_time(cfg["end_t"])
        except (ValueError, IndexError):
            continue

        path = _config_path(local_base, cfg["machine"], cfg["group"],
                            cfg["strat"], cfg["config"], cfg["date"])

        for trader in cfg["traders"]:
            if not trader["enabled"]:
                continue
            index[trader["symbol"]].append({
                "machine": cfg["machine"],
                "group": cfg["group"],
                "strat": cfg["strat"],
                "config": cfg["config"],
                "date": cfg["date"],
                "path": path,
                "start_m": start_m,
                "end_m": end_m,
            })
    return index


# Groups of similar symbols for donor matching.
# When a symbol has zero coverage, we clone from another symbol in the same group.
SYMBOL_GROUPS = [
    ["xyz:GOLD", "xyz:SILVER", "xyz:PLATINUM", "xyz:COPPER", "xyz:PALLADIUM"],
    ["xyz:CL", "xyz:BRENTOIL", "xyz:NATGAS"],
    ["xyz:EUR", "xyz:JPY", "xyz:GBP", "xyz:DXY"],
]


def _find_donor_config(symbol, config_index):
    """Find a donor (symbol, config_path) to clone for a symbol with no coverage.

    Looks in SYMBOL_GROUPS for a sibling symbol that has a config.
    Returns (donor_symbol, config_path) or None.
    """
    # Find which group this symbol belongs to
    for group in SYMBOL_GROUPS:
        if symbol in group:
            for sibling in group:
                if sibling != symbol and sibling in config_index:
                    return (sibling, config_index[sibling][0]["path"])
            break

    # Fallback: pick any symbol from the config index (not ideal but something)
    for other_sym, entries in config_index.items():
        if other_sym != symbol:
            return (other_sym, entries[0]["path"])
    return None


def _clone_config_for_symbol(pk_dct, donor_symbol, target_symbol):
    """Clone a config by swapping all references from donor_symbol to target_symbol.

    Replaces traded_symbol, signal names/symbols, tempo names, and ordex sig refs.
    """
    import copy, json

    # Extract the short names (e.g. "GOLD" from "xyz:GOLD")
    donor_short = donor_symbol.split(":")[-1] if ":" in donor_symbol else donor_symbol
    target_short = target_symbol.split(":")[-1] if ":" in target_symbol else target_symbol

    # Deep-copy and do string replacement throughout
    cfg_str = json.dumps(pk_dct)
    cfg_str = cfg_str.replace(donor_symbol, target_symbol)
    cfg_str = cfg_str.replace(donor_short, target_short)
    return json.loads(cfg_str)


def _retarget_remote(pk_dct, target_remote):
    """Auto-detect the donor's remote-contract code and substitute target_remote.

    The remote contract code (e.g. "HG" for COPPER's copper futures) shows up
    as the value of the SigQuoteMid signal's `symbol` field, and may also show
    up as an underscore-prefixed suffix on signal/tempo names (`remote_mid_hg`,
    `remote_tradecall_tempo_hg`, etc.). After a clone_from, those still point
    at the donor's remote — call this helper to retarget them.

    Substitution is deliberately narrow: only the quoted symbol value
    ("HG" -> "GC") and the underscore-prefixed name suffix (_hg -> _gc). Naming
    suffixes are inconsistent across configs (_hg, _bz, _natgas, or none), so a
    blunt substring replace would corrupt unrelated JSON. Call this AFTER the
    pktraders list has been filtered to the single target trader, so the
    SigQuoteMid we read belongs to the target and not some other trader.
    """
    import json
    source = None
    for trader in pk_dct.get("pktraders", []):
        for sig in trader.get("signals", []):
            if sig.get("type") == "SigQuoteMid":
                source = sig.get("symbol")
                break
        if source:
            break
    if not source or source == target_remote:
        return pk_dct
    cfg_str = json.dumps(pk_dct)
    # Quoted symbol value: "HG" -> "GC"
    cfg_str = cfg_str.replace('"%s"' % source, '"%s"' % target_remote)
    # Underscore-prefixed name suffix: _hg -> _gc (no-op if names are bare)
    cfg_str = cfg_str.replace('_%s' % source.lower(), '_%s' % target_remote.lower())
    return json.loads(cfg_str)


def find_gaps(coverage, config_index, all_config_index=None, all_xyz=None):
    """Find symbols/sessions with count <= 1.

    config_index: index built from active (non-stale) configs.
    all_config_index: index built from ALL configs (including stale). Used to
        determine sessions and find base configs for symbols with no active coverage.
    all_xyz: full list of XYZ symbols to check (ensures symbols with zero coverage
        are still considered).

    Returns: list of {symbol, session, count, base_config, clone_from (optional)}
    """
    if all_config_index is None:
        all_config_index = config_index

    # Build the full set of symbols to check
    symbols_to_check = set(coverage.keys())
    if all_xyz:
        symbols_to_check |= set(all_xyz)

    gaps = []
    for symbol in sorted(symbols_to_check):
        entries = coverage.get(symbol, [])
        # Determine which sessions are relevant for this symbol.
        # Check each config — if any single config covers 3+ sessions,
        # this is an "all day" symbol; use one entry with the config's
        # original time window instead of splitting into sessions.

        # Use all_config_index for session detection (covers stale configs too)
        lookup_index = config_index if symbol in config_index else all_config_index
        if symbol not in lookup_index:
            # No config anywhere — need a donor
            donor = _find_donor_config(symbol, all_config_index)
            if donor:
                donor_sym, donor_path = donor
                gaps.append({
                    "symbol": symbol,
                    "session": "All Day",
                    "count": 0,
                    "base_config": donor_path,
                    "clone_from": donor_sym,
                })
            continue

        is_allday = False
        for ci in lookup_index[symbol]:
            sessions_covered = sum(
                1 for _, ss, se in SESSIONS
                if _session_overlaps(ci["start_m"], ci["end_m"],
                                     _to_minutes(*ss), _to_minutes(*se))
            )
            if sessions_covered >= 3:
                is_allday = True
                break

        if is_allday:
            # Treat as a single "All Day" entry — count total instances
            all_count = len(entries)
            if all_count <= 1:
                base_path = lookup_index[symbol][0]["path"]
                gaps.append({
                    "symbol": symbol,
                    "session": "All Day",
                    "count": all_count,
                    "base_config": base_path,
                })
            continue

        # Per-session gaps for symbols with session-specific configs
        relevant_sessions = []
        for sess_name, sess_start, sess_end in SESSIONS:
            if sess_name in SKIP_SESSIONS:
                continue
            s_start = _to_minutes(*sess_start)
            s_end = _to_minutes(*sess_end)
            has_config = any(
                _session_overlaps(ci["start_m"], ci["end_m"], s_start, s_end)
                for ci in lookup_index[symbol]
            )
            if has_config:
                relevant_sessions.append((sess_name, sess_start, sess_end))

        for sess_name, sess_start, sess_end in relevant_sessions:
            s_start = _to_minutes(*sess_start)
            s_end = _to_minutes(*sess_end)
            matching = [
                e for e in entries
                if _session_overlaps(e["start_m"], e["end_m"], s_start, s_end)
            ]
            if len(matching) <= 1:
                # Find best base config for this symbol/session
                base_path = None
                for ci in lookup_index[symbol]:
                    if _session_overlaps(ci["start_m"], ci["end_m"], s_start, s_end):
                        base_path = ci["path"]
                        break
                if base_path is None:
                    base_path = lookup_index[symbol][0]["path"]

                gaps.append({
                    "symbol": symbol,
                    "session": sess_name,
                    "count": len(matching),
                    "base_config": base_path,
                })
    return gaps


# ── Variant cap helpers ───────────────────────────────────────────────────────

def _downsample_dims(dims, max_variants):
    """Reduce dim sizes so total product <= max_variants.

    Repeatedly trims the largest dim by 1 until within budget.
    Dims with 2 values (e.g. bool True/False) are left alone.
    Always keeps at least 2 values per dim.
    """
    import math
    sizes = [len(vals) for _, vals in dims]
    total = math.prod(sizes)

    if total <= max_variants:
        return dims

    # Iteratively shrink the largest dim
    while math.prod(sizes) > max_variants:
        # Find largest dim that's still > 2
        max_idx = -1
        max_size = 2
        for i, s in enumerate(sizes):
            if s > max_size:
                max_size = s
                max_idx = i
        if max_idx == -1:
            break  # All dims at minimum, can't reduce further
        sizes[max_idx] -= 1

    # Subsample each dim evenly to the target size
    result = []
    for (path, vals), target_size in zip(dims, sizes):
        if target_size >= len(vals):
            result.append((path, vals))
        else:
            # Pick evenly spaced indices including first and last
            indices = [round(i * (len(vals) - 1) / (target_size - 1)) for i in range(target_size)]
            result.append((path, [vals[i] for i in indices]))
    return result


# ── Auto-range helpers ────────────────────────────────────────────────────────

def _auto_range_float(val, steps=5, bounds=None):
    """Generate ±20% range around a float value, clamped to bounds if given.

    bounds: (min_val, max_val) or None
    """
    if val == 0:
        if bounds:
            # Spread from min to some small value within bounds
            low, high = bounds[0], min(bounds[1], 0.5)
            step = (high - low) / (steps - 1)
            return [round(low + i * step, 8) for i in range(steps)]
        return [0.0]
    low = val * 0.8
    high = val * 1.2
    if bounds:
        low = max(low, bounds[0])
        high = min(high, bounds[1])
    if low >= high:
        return [round(val, 8)]
    step = (high - low) / (steps - 1)
    return [round(low + i * step, 8) for i in range(steps)]


def _auto_range_int(val):
    """Generate small range around an int value."""
    candidates = [max(0, val - 1), val, val + 1]
    # Deduplicate while preserving order
    seen = set()
    result = []
    for v in candidates:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def _read_ordex_type(config_path, symbol):
    """Read the ordex type (e.g. 'RelWideMM2', 'AlphaRelWideMM') for a symbol."""
    try:
        with open(config_path) as f:
            cfg = commentjson.load(f)
    except Exception:
        return None

    for trader in cfg.get("pktraders", []):
        if trader.get("traded_symbol") == symbol:
            return trader.get("ordex", {}).get("type")
    return None


# Ordex types whose params are compatible with our sweep dims.
COMPATIBLE_ORDEX_TYPES = {"RelWideMM2"}


def _read_ordex_params(config_path, symbol):
    """Read common tuning params from the ordex section for a given symbol."""
    try:
        with open(config_path) as f:
            cfg = commentjson.load(f)
    except Exception:
        return {}

    for trader in cfg.get("pktraders", []):
        if trader.get("traded_symbol") == symbol:
            ordex = trader.get("ordex", {})
            params = {}
            for key in DEFAULT_TUNE_PARAMS:
                if key in ordex:
                    params[key] = ordex[key]
            return params
    return {}


def _generate_dims_for_params(params):
    """Generate dimension specs from current param values."""
    dims = []
    for key, val in params.items():
        path = ["pktraders", "ordex", key]
        bounds = PARAM_BOUNDS.get(key)
        ptype = PARAM_TYPES.get(key)  # forced type, or None to infer

        if ptype == "bool" or (ptype is None and isinstance(val, bool)):
            dims.append((path, [True, False]))
        elif ptype == "float" or (ptype is None and isinstance(val, float)):
            dims.append((path, _auto_range_float(float(val), bounds=bounds)))
        elif ptype == "int" or (ptype is None and isinstance(val, int)):
            dims.append((path, _auto_range_int(int(val))))
        elif isinstance(val, str):
            try:
                fval = float(val)
                dims.append((path, [str(round(v, 8)) for v in _auto_range_float(fval, bounds=bounds)]))
            except ValueError:
                pass
    return dims


# ── Step 1: Generate spec ─────────────────────────────────────────────────────

def generate_spec(output_dir, sim_days=14, max_stale_days=4, force_symbols=None, vol_mode=None):
    """Find coverage gaps and write a Python spec file for user editing.

    force_symbols: list of symbol names (e.g. ["xyz:BRENTOIL"]) to always include,
    even if coverage doesn't flag them as gaps.
    vol_mode: if set, generate entries for this vol mode only (e.g. "vol_norm").
    If None, generate plain entries with no vol mode.
    """
    os.makedirs(output_dir, exist_ok=True)

    print("Fetching XYZ symbols...")
    all_xyz = fetch_all_xyz_symbols()
    if not all_xyz:
        sys.exit("Could not fetch XYZ symbols.")

    print(f"Loading configs (max_stale_days={max_stale_days})...")
    configs = load_latest_configs(LOCAL_BASE, max_stale_days=max_stale_days)
    all_configs = load_latest_configs(LOCAL_BASE, max_stale_days=None)
    coverage, _weekend = build_coverage(configs)
    config_index = _build_config_index(configs, LOCAL_BASE)
    all_config_index = _build_config_index(all_configs, LOCAL_BASE)

    print("Finding coverage gaps...")
    gaps = find_gaps(coverage, config_index, all_config_index, all_xyz)

    # One entry per (symbol, session) — each gets its own sim
    symbol_session_gaps = []
    for gap in gaps:
        symbol_session_gaps.append({
            "symbol": gap["symbol"],
            "session": gap["session"],
            "count": gap["count"],
            "base_config": gap["base_config"],
        })

    # Force-include symbols even if not flagged as gaps
    if force_symbols:
        existing = set(g["symbol"] for g in symbol_session_gaps)
        for sym in force_symbols:
            if sym in existing:
                continue
            if sym in all_config_index:
                base_path = all_config_index[sym][0]["path"]
                # Check if allday
                is_allday = any(
                    sum(1 for _, ss, se in SESSIONS
                        if _session_overlaps(ci["start_m"], ci["end_m"],
                                             _to_minutes(*ss), _to_minutes(*se))) >= 3
                    for ci in all_config_index[sym]
                )
                if is_allday:
                    symbol_session_gaps.append({
                        "symbol": sym, "session": "All Day",
                        "count": 0, "base_config": base_path,
                    })
                else:
                    # Add for each relevant session (not in SKIP_SESSIONS)
                    for sess_name, sess_start, sess_end in SESSIONS:
                        if sess_name in SKIP_SESSIONS:
                            continue
                        s_start = _to_minutes(*sess_start)
                        s_end = _to_minutes(*sess_end)
                        if any(_session_overlaps(ci["start_m"], ci["end_m"], s_start, s_end)
                               for ci in all_config_index[sym]):
                            symbol_session_gaps.append({
                                "symbol": sym, "session": sess_name,
                                "count": 0, "base_config": base_path,
                            })
                print(f"  Force-included: {sym}")
            else:
                # No config at all — find a donor symbol from the same market
                donor = _find_donor_config(sym, all_config_index)
                if donor:
                    donor_sym, donor_path = donor
                    print(f"  Force-included: {sym} (cloned from {donor_sym})")
                    symbol_session_gaps.append({
                        "symbol": sym, "session": "All Day",
                        "count": 0, "base_config": donor_path,
                        "clone_from": donor_sym,
                    })
                else:
                    print(f"  Warning: {sym} — no config or donor found, skipping")

    if not symbol_session_gaps:
        print("No coverage gaps found (all symbols have >= 2 instances per session).")
        return

    # Build spec content
    today_str = date.today().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"# Coverage Autosearch Spec — {today_str}")
    lines.append("# Edit dimensions/values per symbol, then run with --run-spec")
    lines.append("")
    lines.append("from datetime import date")
    lines.append("")
    lines.append("# Global defaults — applied to any symbol not overridden below")
    lines.append("# Each entry: (param_path_list, values_to_sweep)")
    lines.append("default_dims = [")
    lines.append('    (["pktraders", "ordex", "place_thresh"], [0.00036, 0.00040, 0.00045, 0.00050, 0.00054]),')
    lines.append("]")
    lines.append("")
    lines.append("# Fixed overrides — always applied, not swept (single-value dims)")
    lines.append("# To change a fixed param to a sweep, move it to dims/range_dims with a list of values")
    lines.append("default_fixed = [")
    for k, v in DEFAULT_FIXED_OVERRIDES.items():
        lines.append(f'    (["pktraders", "ordex", "{k}"], {v!r}),')
    lines.append("]")
    lines.append("")
    lines.append("# Number of trailing days to simulate")
    lines.append(f"sim_days = {sim_days}")
    lines.append("")
    lines.append("# Max variants per symbol (excess variants are randomly sampled)")
    lines.append(f"max_variants = {DEFAULT_MAX_VARIANTS}")
    lines.append("")
    lines.append("# Use random sampling when variants exceed max_variants (vs deterministic downsampling)")
    lines.append("random_sample = True")
    lines.append("")
    if vol_mode:
        lines.append(f"# Vol mechanism mode: {vol_mode} — {VOL_MODES[vol_mode]['description']}")
        lines.append(f"# Available modes: {', '.join(VOL_MODES.keys())}")
        lines.append("")
    lines.append("# Per-(symbol, session) specs — each entry runs a separate sim")
    lines.append("# Key format: 'symbol|session' e.g. 'xyz:AAPL|US Day'")
    lines.append("# base_config: path to the existing pk_*.json config")
    lines.append("# session: which session to sim (config time window is set automatically)")
    lines.append("# dims: override default_dims (optional, omit to use defaults)")
    lines.append("symbols = {")

    skipped_types = []
    for gap in sorted(symbol_session_gaps, key=lambda g: (g["symbol"], g["session"])):
        sym = gap["symbol"]
        session = gap["session"]
        base_config = gap["base_config"]
        clone_from = gap.get("clone_from")
        read_sym = clone_from if clone_from else sym

        # Check ordex type compatibility
        ordex_type = _read_ordex_type(base_config, read_sym)
        if ordex_type and ordex_type not in COMPATIBLE_ORDEX_TYPES:
            skipped_types.append((sym, session, base_config, ordex_type))
            continue

        params = _read_ordex_params(base_config, read_sym)
        auto_dims = _generate_dims_for_params(params)

        key = f"{sym}|{session}"

        if vol_mode:
            vol_dims = _generate_vol_mode_dims(vol_mode)
            vol_fixed = _generate_vol_mode_fixed(vol_mode)
            all_entry_dims = list(auto_dims) + vol_dims if auto_dims else vol_dims
        else:
            vol_fixed = []
            all_entry_dims = auto_dims

        lines.append(f'    "{key}": {{')
        lines.append(f'        "base_config": "{base_config}",')
        lines.append(f'        "session": "{session}",  # count={gap["count"]}')
        if vol_mode:
            lines.append(f'        "vol_mode": "{vol_mode}",  # {VOL_MODES[vol_mode]["description"]}')
        if clone_from:
            lines.append(f'        "clone_from": "{clone_from}",  # config cloned from this symbol')

        if all_entry_dims:
            lines.append("        # Auto-generated from current config values:")
            lines.append('        "dims": [')
            for path, values in all_entry_dims:
                lines.append(f"            ({path}, {values}),")
            lines.append("        ],")
        else:
            lines.append("        # dims: use default_dims (could not read current values)")

        if vol_fixed:
            lines.append("        # Vol mode fixed overrides (zero out other mechanisms):")
            lines.append('        "fixed": [')
            for path, val in vol_fixed:
                lines.append(f"            ({path}, {val!r}),")
            lines.append("        ],")

        lines.append("    },")

    lines.append("}")
    lines.append("")

    spec_path = os.path.join(output_dir, "coverage_search_spec.py")
    with open(spec_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\nGenerated spec: {spec_path}")
    unique_syms = set(g["symbol"] for g in symbol_session_gaps)
    print(f"Symbols with gaps: {len(unique_syms)}")
    print(f"Total entries: {len(symbol_session_gaps) - len(skipped_types)}")
    if vol_mode:
        print(f"Vol mode: {vol_mode}")
    if skipped_types:
        print(f"\nSkipped {len(skipped_types)} entries (incompatible ordex type, not {'/'.join(COMPATIBLE_ORDEX_TYPES)}):")
        for sym, session, path, otype in skipped_types:
            print(f"  {sym} [{session}]: {otype} ({os.path.basename(path)})")
    print(f"\nEdit the spec file, then run:")
    print(f"  coverage_autosearch.py --run-spec {spec_path}")


# ── Step 2: Run spec ──────────────────────────────────────────────────────────

def _load_spec(spec_path):
    """Import a Python spec file and return its module."""
    spec = importlib.util.spec_from_file_location("coverage_search_spec", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dims_to_overrides(dims):
    """Convert spec dims to SimVariations overrides_dct format.

    Input:  [(path_list, values_list), ...]
    Output: [{"path": path_list, "val": values_list}, ...]
    """
    return [{"path": list(path), "val": list(values)} for path, values in dims]


def _get_param_from_config(pk_dct, path):
    """Walk a param path in a loaded config dict, return the current value.

    Handles the pktraders-is-a-list convention (takes index 0).
    """
    cur = pk_dct
    for key in path:
        if isinstance(cur, list):
            cur = cur[0]
        cur = cur[key]
    return cur


def _resolve_range_dims(range_dims, pk_dct, default_neighborhood_pct=0.3):
    """Resolve range_dims into explicit (path, values) dims using the config's current values.

    Each range_dim entry is either:
      (path, {"min": lo, "max": hi, "steps": N, [, "neighborhood_pct": P]})
      (path, {"values": [v1, v2, ...]})
    """
    resolved = []
    for path, spec in range_dims:
        if "values" in spec:
            # Explicit value list (e.g. booleans)
            resolved.append((list(path), list(spec["values"])))
            continue

        lo = spec["min"]
        hi = spec["max"]
        steps = spec["steps"]
        nbr_pct = spec.get("neighborhood_pct", default_neighborhood_pct)

        # Read the current value from the config
        try:
            current = _get_param_from_config(pk_dct, path)
        except (KeyError, IndexError, TypeError):
            print(f"    [warn] Could not read {path} from config, using midpoint of [{lo}, {hi}]")
            current = (lo + hi) / 2.0

        # Compute neighborhood around current, clamped to [lo, hi]
        if current == 0:
            # Can't do percentage of zero; use absolute range
            sweep_lo = lo
            sweep_hi = hi
        else:
            spread = abs(current) * nbr_pct
            sweep_lo = max(lo, current - spread)
            sweep_hi = min(hi, current + spread)

        # Generate evenly spaced values
        if steps <= 1:
            values = [current]
        else:
            step_size = (sweep_hi - sweep_lo) / (steps - 1)
            values = [sweep_lo + i * step_size for i in range(steps)]

        # Match the type of the current value.
        # Use float if the range spec has fractional min/max/step, even if current is int.
        range_is_fractional = (lo != int(lo) or hi != int(hi) or
                               (steps > 1 and (sweep_hi - sweep_lo) / (steps - 1) != int((sweep_hi - sweep_lo) / (steps - 1))))
        if isinstance(current, int) and not range_is_fractional:
            values = sorted(set(int(round(v)) for v in values))
        else:
            values = [round(v, 8) for v in values]

        print(f"    range_dim {path[-1]}: current={current}, sweep [{sweep_lo:.6g}, {sweep_hi:.6g}], {len(values)} values: {values}")
        resolved.append((list(path), values))

    return resolved


def run_spec(spec_path, remote=False, hosts_yaml=None, chunk_size=50):
    """Load the user-edited spec, run sim_grid per symbol, output ranked results.

    Args:
        spec_path: edited spec file.
        remote: if True, dispatch each per-symbol sim_grid via swarmhost.
        hosts_yaml: optional path to a hosts.yaml; defaults to the swarmhost default.
        chunk_size: variants per dispatch chunk when remote=True.
    """
    from stratbuilder.SimVariations import sim_grid

    print(f"Loading spec: {spec_path}")
    spec_mod = _load_spec(spec_path)

    default_dims = getattr(spec_mod, "default_dims", [])
    default_range_dims = getattr(spec_mod, "default_range_dims", [])
    default_fixed = getattr(spec_mod, "default_fixed", [])
    neighborhood_pct = getattr(spec_mod, "neighborhood_pct", 0.3)
    max_variants = getattr(spec_mod, "max_variants", DEFAULT_MAX_VARIANTS)
    random_sample = getattr(spec_mod, "random_sample", True)
    sim_days = getattr(spec_mod, "sim_days", 14)
    symbols = getattr(spec_mod, "symbols", {})
    extra_args = getattr(spec_mod, "extra_args", "")

    if not symbols:
        sys.exit("No symbols in spec.")

    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    results_dir = os.path.join(spec_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    # Compute date range (pad for weekends/holidays).
    # Honor spec's sweep_end when set (avoids Phase 1 chasing today's data
    # that may not be staged yet — relcross autosearch adds this field).
    spec_sweep_end = getattr(spec_mod, "sweep_end", None)
    if spec_sweep_end:
        end_date = datetime.strptime(str(spec_sweep_end), "%Y%m%d").date()
    else:
        end_date = date.today()
    start_date = end_date - timedelta(days=sim_days + 7)
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    summary_lines = []
    summary_lines.append(f"Coverage Autosearch Results — {date.today().strftime('%Y-%m-%d')}")
    summary_lines.append(f"Sim days target: {sim_days}")
    summary_lines.append("=" * 80)

    for entry_key, sym_spec in sorted(symbols.items()):
        # Parse symbol, session, and optional vol_mode from key
        # Formats: "xyz:AAPL|US Day|vol_norm", "xyz:AAPL|US Day", "xyz:AAPL"
        parts = entry_key.split("|")
        if len(parts) >= 3:
            sym_name, session, vol_mode = parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            sym_name, session = parts
            vol_mode = sym_spec.get("vol_mode", None)
        else:
            sym_name = entry_key
            session = sym_spec.get("session", None)
            vol_mode = sym_spec.get("vol_mode", None)

        print(f"\n{'=' * 60}")
        vol_label = f" [{vol_mode}]" if vol_mode else ""
        print(f"Processing: {sym_name} — {session or 'all sessions'}{vol_label}")
        print(f"{'=' * 60}")

        base_config = sym_spec["base_config"]
        dims = sym_spec.get("dims", default_dims)
        range_dims = sym_spec.get("range_dims", default_range_dims)
        fixed = list(sym_spec.get("fixed", default_fixed))

        # Merge vol mode fixed overrides if vol_mode is set but fixed wasn't
        # explicitly provided in the spec entry (i.e. it fell back to default_fixed)
        if vol_mode and vol_mode in VOL_MODES and "fixed" not in sym_spec:
            fixed = fixed + _generate_vol_mode_fixed(vol_mode)

        if not dims and not range_dims:
            print(f"  Skipping {sym_name}: no dimensions specified")
            continue

        if not os.path.exists(base_config):
            print(f"  Skipping {sym_name}: base config not found: {base_config}")
            continue

        # Load the base config
        try:
            with open(base_config) as f:
                pk_dct = commentjson.load(f)
        except Exception as e:
            print(f"  Skipping {sym_name}: error loading config: {e}")
            continue

        # If cloning from a donor symbol, swap all symbol references
        clone_from = sym_spec.get("clone_from")
        if clone_from:
            pk_dct = _clone_config_for_symbol(pk_dct, clone_from, sym_name)
            print(f"  Cloned config from {clone_from}")

        # Filter to just this symbol's pktrader (sim_grid uses pktraders[0])
        pk_dct["pktraders"] = [
            t for t in pk_dct["pktraders"]
            if t.get("traded_symbol") == sym_name
        ]
        if not pk_dct["pktraders"]:
            print(f"  Skipping {sym_name}: symbol not found in config")
            continue

        # Retarget remote AFTER filtering, so _retarget_remote reads the target
        # trader's SigQuoteMid (not some other trader's that happens to be first).
        target_remote = sym_spec.get("remote_sym")
        if target_remote:
            pk_dct = _retarget_remote(pk_dct, target_remote)
            print(f"  Retargeted remote: -> {target_remote}")

        # Reset size_mult to 1 and force-enable for consistent sim scoring
        pk_dct["pktraders"][0]["size_mult"] = 1
        pk_dct["pktraders"][0]["enabled"] = True

        # Set the config time window to match the target session
        if session and session in SESSION_TIMES:
            start_t, end_t = SESSION_TIMES[session]
            pk_dct.setdefault("settings", {})["start_t"] = start_t
            pk_dct["settings"]["end_t"] = end_t

        # Ensure market data exists for this symbol
        trader = pk_dct["pktraders"][0]
        mkt = trader["ordex"]["markets"][0]
        print(f"  Checking/downloading market data for {sym_name} on {mkt}...")
        try:
            md_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "md_exists.py")
            subprocess.run(
                [sys.executable, md_script,
                 "--sym", sym_name, "--market", mkt,
                 "--start", start_str, "--end", end_str],
                input="y\n", text=True,
                timeout=600,
            )
        except Exception as e:
            print(f"  Warning: md_exists failed: {e}")

        # Get available sim dates
        try:
            avail = dates_avail(start_str, end_str, sym_name, mkt,
                                dates_method="WEEKDAYS")
            sim_dates = avail["good_dates"]
        except Exception as e:
            print(f"  Falling back to weekday dates: {e}")
            sim_dates = dates_list(start_str, end_str, dates_method="WEEKDAYS")

        # Trim to requested sim_days
        if len(sim_dates) > sim_days:
            sim_dates = sim_dates[-sim_days:]

        if not sim_dates:
            print(f"  Skipping {sym_name}: no dates available")
            continue

        # Resolve range_dims against the loaded config
        resolved_range = []
        if range_dims:
            resolved_range = _resolve_range_dims(range_dims, pk_dct, neighborhood_pct)

        all_dims = list(dims) + resolved_range

        # Cap variant count per symbol
        import math
        total_before = math.prod(len(vals) for _, vals in all_dims) if all_dims else 0
        sym_max = sym_spec.get("max_variants", max_variants)
        use_random = sym_spec.get("random_sample", random_sample)
        random_sample_n = 0
        if total_before > sym_max:
            if use_random:
                random_sample_n = sym_max
                print(f"  Variants: {total_before} total, random sampling {sym_max}")
            else:
                all_dims = _downsample_dims(all_dims, sym_max)
                total_after = math.prod(len(vals) for _, vals in all_dims)
                print(f"  Variants: {total_before} -> {total_after} (capped to max_variants={sym_max})")
        else:
            total_after = total_before

        print(f"  Base config: {base_config}")
        print(f"  Session: {session or 'all'}")
        print(f"  Dims: {len(dims)} explicit + {len(resolved_range)} range = {len(all_dims)} total")
        if fixed:
            print(f"  Fixed overrides: {len(fixed)} ({', '.join(p[-1] for p, _ in fixed)})")
        print(f"  Dates: {sim_dates[0]} to {sim_dates[-1]} ({len(sim_dates)} days)")

        label = f"{sym_name} [{session}]" if session else sym_name
        if vol_mode:
            label += f" ({vol_mode})"

        # Build overrides in SimVariations format
        overrides = _dims_to_overrides(all_dims)

        # Add fixed overrides (single values, not swept)
        for path, val in fixed:
            overrides.append({"path": list(path), "val": val})

        # Working directory per (symbol, session, vol_mode, spec).
        # The per-spec subdir is required: sim_grid caches per-variant results
        # in scratch/<i>/sim_results.json keyed by integer index. Without the
        # spec subdir, a second spec sharing this workdir would inherit cached
        # results from the previous spec's variant <i>, even though the
        # configs differ — silently returning the previous round's pnl.
        dir_name = sym_name.replace(":", "_")
        if session:
            dir_name += "_" + session.lower().replace(" ", "").replace("-", "")
        if vol_mode:
            dir_name += "_" + vol_mode
        spec_basename = os.path.splitext(os.path.basename(spec_path))[0]
        sym_dir = os.path.join(results_dir, dir_name, spec_basename)
        os.makedirs(sym_dir, exist_ok=True)

        # Run the sim grid
        try:
            sim_grid(overrides, pk_dct, sym_dir, sim_dates,
                     fast_sim=False, resume=True, random_sample=random_sample_n,
                     remote=remote, hosts_yaml=hosts_yaml, chunk_size=chunk_size,
                     extra_args=extra_args)
        except Exception as e:
            print(f"  Error simming {sym_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

        # Collect results
        results_file = os.path.join(sym_dir, "results.txt")
        if os.path.exists(results_file):
            with open(results_file) as f:
                results_text = f.read()
            print(f"\n  Results for {label}:")
            print(results_text)

            summary_lines.append(f"\n{label}")
            summary_lines.append(f"  Base: {base_config}")

            # Parse and rank by sim_score (last column), show top 5
            result_lines = results_text.strip().split("\n")
            if len(result_lines) > 1:
                header = result_lines[0]
                data_lines = result_lines[1:]
                try:
                    scored = []
                    for line in data_lines:
                        parts = line.strip().split(",")
                        if parts and parts[-1]:
                            score = float(parts[-1])
                            scored.append((score, line))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    summary_lines.append(f"  {header}")
                    for score, line in scored[:5]:
                        summary_lines.append(f"  {line}")
                except (ValueError, IndexError):
                    summary_lines.append("  (could not parse results)")
        else:
            summary_lines.append(f"\n{label}: no results file generated")

    # Write cross-symbol summary
    summary_path = os.path.join(spec_dir, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(summary_lines) + "\n")
    print(f"\nSummary written to: {summary_path}")


# ── Pre-flight place_thresh scan ──────────────────────────────────────────────

# Wide log-scale thresh values to probe per entry. ~50bp/100bp/200bp captures
# wide-spread thin names; ~1bp/2bp/5bp captures tight liquid names.
SCAN_THRESHES = [0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02]


def scan_thresh(spec_path):
    """Place_thresh-only scan over each entry in the spec.

    Helps size a proper place_thresh sweep range before the full multi-dim
    run. Holds all other dims at the base config defaults; only sweeps
    place_thresh across SCAN_THRESHES. Results land in <spec_dir>/thresh_scan/
    with one results.txt per (symbol, session) and an aggregated summary.
    """
    from stratbuilder.SimVariations import sim_grid

    print(f"Loading spec: {spec_path}")
    spec_mod = _load_spec(spec_path)
    symbols = getattr(spec_mod, "symbols", {})
    sim_days = getattr(spec_mod, "sim_days", 14)
    # The spec may override the threshold list (e.g. for a finer/wider scan).
    scan_threshes = getattr(spec_mod, "scan_threshes", SCAN_THRESHES)

    if not symbols:
        sys.exit("No symbols in spec.")

    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    scan_dir = os.path.join(spec_dir, "thresh_scan")
    os.makedirs(scan_dir, exist_ok=True)

    # Honor spec's sweep_end when set (relcross autosearch adds this).
    spec_sweep_end = getattr(spec_mod, "sweep_end", None)
    if spec_sweep_end:
        end_date = datetime.strptime(str(spec_sweep_end), "%Y%m%d").date()
    else:
        end_date = date.today()
    start_date = end_date - timedelta(days=sim_days + 7)
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    summary_lines = [
        f"Place_thresh scan — {date.today().strftime('%Y-%m-%d')}",
        f"Threshes: {scan_threshes}",
        "=" * 80,
    ]

    for entry_key, sym_spec in sorted(symbols.items()):
        parts = entry_key.split("|")
        sym_name = parts[0]
        session = parts[1] if len(parts) >= 2 else sym_spec.get("session")

        print(f"\n{'='*60}\nScanning: {entry_key}\n{'='*60}")

        base_config = sym_spec["base_config"]
        if not os.path.exists(base_config):
            print(f"  skipping: base config missing")
            continue

        try:
            with open(base_config) as f:
                pk_dct = commentjson.load(f)
        except Exception as e:
            print(f"  error loading config: {e}")
            continue

        clone_from = sym_spec.get("clone_from")
        if clone_from:
            pk_dct = _clone_config_for_symbol(pk_dct, clone_from, sym_name)

        pk_dct["pktraders"] = [
            t for t in pk_dct["pktraders"]
            if t.get("traded_symbol") == sym_name
        ]
        if not pk_dct["pktraders"]:
            print(f"  symbol not in config")
            continue

        # Retarget remote AFTER filtering so we read the target trader's SigQuoteMid.
        target_remote = sym_spec.get("remote_sym")
        if target_remote:
            pk_dct = _retarget_remote(pk_dct, target_remote)

        pk_dct["pktraders"][0]["size_mult"] = 1
        pk_dct["pktraders"][0]["enabled"] = True

        if session and session in SESSION_TIMES:
            start_t, end_t = SESSION_TIMES[session]
            pk_dct.setdefault("settings", {})["start_t"] = start_t
            pk_dct["settings"]["end_t"] = end_t

        trader = pk_dct["pktraders"][0]
        mkt = trader["ordex"]["markets"][0]
        try:
            md_script = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "md_exists.py",
            )
            subprocess.run(
                [sys.executable, md_script,
                 "--sym", sym_name, "--market", mkt,
                 "--start", start_str, "--end", end_str],
                input="y\n", text=True, timeout=600,
                capture_output=True,
            )
        except Exception:
            pass

        try:
            avail = dates_avail(start_str, end_str, sym_name, mkt,
                                dates_method="WEEKDAYS")
            sim_dates = avail["good_dates"]
        except Exception:
            sim_dates = dates_list(start_str, end_str, dates_method="WEEKDAYS")
        if len(sim_dates) > sim_days:
            sim_dates = sim_dates[-sim_days:]
        if not sim_dates:
            print(f"  no dates available")
            continue

        scan_overrides = [
            {"path": ["pktraders", "ordex", "place_thresh"], "val": scan_threshes},
            {"path": ["pktraders", "ordex", "restrict_near_book"], "val": True},
        ]
        # Apply the spec entry's fixed overrides too, so e.g. premium_ema_coef=1
        # (or any zeroing-out of unused vol mechanisms) actually takes effect.
        # Also fold in the vol_mode's fixed block when set.
        fixed = list(sym_spec.get("fixed", []))
        vol_mode = sym_spec.get("vol_mode")
        if vol_mode and vol_mode in VOL_MODES and "fixed" not in sym_spec:
            fixed = fixed + _generate_vol_mode_fixed(vol_mode)
        for path, val in fixed:
            scan_overrides.append({"path": list(path), "val": val})

        # Per-spec subdir for the same isolation reasons as run_spec —
        # see the long comment in run_spec() near `sym_dir`.
        dir_name = sym_name.replace(":", "_")
        if session:
            dir_name += "_" + session.lower().replace(" ", "").replace("-", "")
        spec_basename = os.path.splitext(os.path.basename(spec_path))[0]
        sym_scan_dir = os.path.join(scan_dir, dir_name, spec_basename)
        os.makedirs(sym_scan_dir, exist_ok=True)

        print(f"  threshes: {scan_threshes}")
        print(f"  dates: {sim_dates[0]} to {sim_dates[-1]} ({len(sim_dates)} days)")

        try:
            sim_grid(scan_overrides, pk_dct, sym_scan_dir, sim_dates,
                     fast_sim=False, resume=True)
        except Exception as e:
            print(f"  scan failed: {e}")
            continue

        rfile = os.path.join(sym_scan_dir, "results.txt")
        if not os.path.exists(rfile):
            continue
        import csv
        with open(rfile) as f:
            rows = list(csv.DictReader(f))
        rows.sort(key=lambda r: float(r["place_thresh"]))
        print(f"\n  {'thresh':>10} {'trades':>8} {'avg_pnl':>10} {'sharpe':>8} {'sim_score':>10}")
        summary_lines.append(f"\n{entry_key}")
        summary_lines.append(f"  {'thresh':>10} {'trades':>8} {'avg_pnl':>10} {'sharpe':>8} {'sim_score':>10}")
        for r in rows:
            line = f"  {float(r['place_thresh']):>10.5f} {int(float(r['num_trds'])):>8} {float(r['avg_pnl']):>10.2f} {float(r['sharpe']):>8.2f} {float(r['sim_score']):>10.2f}"
            print(line)
            summary_lines.append(line)

    summary_path = os.path.join(scan_dir, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(summary_lines) + "\n")
    print(f"\nScan summary: {summary_path}")
    print(f"Inspect results, then update place_thresh dims in the spec and run --run-spec.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Coverage gap autosearch — find gaps and run sim variations"
    )
    parser.add_argument("--generate-spec", action="store_true",
                        help="Generate a spec file from current coverage gaps")
    parser.add_argument("--run-spec", type=str, metavar="PATH",
                        help="Run sims from an edited spec file")
    parser.add_argument("--scan-thresh", type=str, metavar="PATH",
                        help="Place_thresh-only scan over an existing spec — "
                             "run before --run-spec to size proper ranges")
    parser.add_argument("--sim-days", type=int, default=14,
                        help="Number of trailing days to simulate (default: 14)")
    parser.add_argument("--max-stale-days", type=int, default=4,
                        help="Configs older than this many days behind the machine max are stale (default: 4)")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols to force-include (e.g. 'BRENTOIL,EWJ,PLATINUM'). "
                             "These are added even if coverage says they're fine.")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Override output directory")
    parser.add_argument("--vol-mode", type=str, default=None,
                        choices=list(VOL_MODES.keys()),
                        help="Only generate entries for this vol mode (default: all modes)")
    # Swarmhost dispatch — opt in. Default off keeps existing local behavior.
    parser.add_argument("--remote", action="store_true",
                        help="Dispatch sims to the swarmhost pool instead of running locally.")
    parser.add_argument("--hosts", default=None,
                        help="Path to swarmhost hosts.yaml (default: overmind/swarmhost/hosts.yaml).")
    parser.add_argument("--chunk-size", type=int, default=50,
                        help="Variants per dispatch chunk when --remote. Default 50.")
    args = parser.parse_args()

    if not args.generate_spec and not args.run_spec and not args.scan_thresh:
        parser.print_help()
        sys.exit(1)

    if args.generate_spec:
        if args.output_dir:
            output_dir = args.output_dir
        else:
            today_str = date.today().strftime("%Y%m%d")
            output_dir = os.path.expanduser(
                f"~/scratch/coverage_autosearch/{today_str}"
            )
        force_symbols = None
        if args.symbols:
            force_symbols = [s.strip() for s in args.symbols.split(",")]
            # Add xyz: prefix if missing
            force_symbols = [s if ":" in s else f"xyz:{s}" for s in force_symbols]
        generate_spec(output_dir, sim_days=args.sim_days,
                      max_stale_days=args.max_stale_days,
                      force_symbols=force_symbols,
                      vol_mode=args.vol_mode)

    if args.scan_thresh:
        scan_thresh(args.scan_thresh)

    if args.run_spec:
        run_spec(args.run_spec, remote=args.remote, hosts_yaml=args.hosts,
                 chunk_size=args.chunk_size)


if __name__ == "__main__":
    main()
