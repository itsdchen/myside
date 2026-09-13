#!/usr/bin/env python3
"""Autosearch helper for alpha_rel_wide_mm SimVariations sweeps.

Usage mirrors coverage_autosearch:
  - --generate-spec writes a preset-based spec file, handling one or many
    symbols and auto-building configs when no base file is supplied.
  - --run-spec executes the edited spec via SimVariations, supporting
    max-variant caps and deterministic downsampling.

Example
───────
    python3 alpha_relwide_autosearch.py generate-spec \\
        --symbols xyz:COPPER,xyz:CL --sim-days 5 --max-variants 20 \\
        --sample-size 10 --downsample-mode even --neighborhood-pct 0.2 \\
        --output-dir ~/scratch/alpha_relwide_autosearch/test_run

    python3 alpha_relwide_autosearch.py run-spec \\
        --run-spec ~/scratch/alpha_relwide_autosearch/test_run/alpha_relwide_spec.py

Key sweep knobs
───────────────
max_variants   Hard cap on how many variant combinations we will generate
               per symbol.  This is the classic "SimVariations --max" cap.
sample_size    Optional tighter cap that down-samples the grid *before*
               the run.  When specified (default 400) we inspect the
               Cartesian product of all dims/range_dims and trim it down to
               at most `sample_size` combinations.  This prevents massive
               grids from exploding in size.
downsample_mode  Strategy used by the sampler when the Cartesian product is
                 larger than `sample_size`/`max_variants`:
                 - "even": deterministic, shrinks each dimension evenly
                   (via coverage_autosearch._downsample_dims).
                 - "random": randomly drops values until the product fits.

Both caps interact: if both are set, the sampler uses `min(max_variants,
sample_size)` as the effective limit and trims the dims accordingly.

Lessons from iterative drilldowns (2026-04)
───────────────────────────────────────────

Cross-symbol patterns for US equities (Nasdaq Basic remote):
  - lmr_ema_mult=1.0 for all equities. Don't amplify the noisy Nasdaq Basic
    signal. Higher mult works for commodities (CME feed) but hurts equities.
  - vol_widen_coef=0.2-0.4 (additive, tdc=2) consistently helps. Dynamically
    widening during volatile periods protects against the noisy remote.
  - sanitize_lspread=true always.
  - Tradecaller should fire on the remote symbol/market (e.g. TSLA/TopBookEquity),
    not BTC/Hyperliquid.
  - US equities auto-detect to US Day session (09:30-16:00 ET).

Per-symbol tuning insights:
  - Symbols with noisy remotes (MSFT, META) need high curv_impulse_coef (8-12)
    to avoid adverse selection from sequential fills. The impulse braking makes
    the strategy back off hard after each fill.
  - pcurvicity=2+ with steep position curve helps symbols prone to accumulating
    position and getting stuck at maxpos (MSFT especially).
  - Tighter tgt_maxpos_notional (16k-24k vs 40k) often helps equities by
    limiting exposure to wrong-way fills.
  - Crypto-native stocks (CRCL) are poor candidates — the equity feed lags
    the local Hyperliquid book, reversing the information advantage.

Iterative drilldown methodology:
  1. Run broad sweep (core,momentum presets, 200 samples).
  2. Diagnose failure mode from actstats (position accumulation? adverse
     selection? wrong direction every day?).
  3. Fix params that clearly work, extend params that show promise.
  4. Run focused sweep (400 samples) with narrowed grid.
  5. Repeat 2-4 at least 3-4 times. Don't conclude a symbol is untradeable
     after one sweep — MSFT went from "199/201 variants lose" to "$43/day,
     0.39 sharpe" in 4 rounds.

Example results (2026-04, US Day session):
  TSLA:  $160/day, 0.43 sharpe, 60% positive  (worked out of the box)
  INTC:  $175/day, 0.13 sharpe, 50% positive  (worked out of the box)
  MSFT:  $43/day, 0.39 sharpe, 56% positive   (4 rounds of drilldown)
  META:  $136/day, 0.61 sharpe, 67% positive  (1 focused round)
  CRCL:  $62/day, 0.60 sharpe, 78% positive   (but only 4 trades/10 days)
"""

from __future__ import annotations

import argparse
import copy
import csv
import importlib.util
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from pprint import pformat
from typing import Any

try:
    import commentjson  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    import json as _fallback_commentjson
    sys.modules["commentjson"] = _fallback_commentjson
    commentjson = _fallback_commentjson  # type: ignore

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR.parent))
REPO_ROOT = SCRIPT_DIR.parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from coverage_autosearch import (
    _resolve_range_dims,
    _dims_to_overrides,
    _downsample_dims,
    _get_param_from_config,
)  # type: ignore
from coverage_autosearch import SESSION_TIMES  # type: ignore
from util.chron import dates_list
from stratbuilder.SimVariations import sim_grid
from util.symbolizer import get_relwide_remote_meta, is_us_equity
from overmind.strat_main.tools.trademan import relwide_model_bank
from overmind.strat_main.tools.trademan.symbol_preferences import (
    ALPHA_RELWIDE_PREFERRED_DIMS,
)
from overmind.strat_main.stratbuilder import DailyStats

try:
    from pybin import gen_relcross
except ImportError:  # pragma: no cover - optional dependency during tests
    gen_relcross = None


DEFAULT_OUTPUT_ROOT = Path.home() / "scratch" / "alpha_relwide_autosearch"

# Optional override for "today". Set via --end-date YYYY-MM-DD on run-spec/generate-spec
# to make sim_days land on a backshifted window when recent histdata is gappy.
_TODAY_OVERRIDE: date | None = None

def _today() -> date:
    return _TODAY_OVERRIDE if _TODAY_OVERRIDE is not None else date.today()
DEFAULT_SPEC_PATH = DEFAULT_OUTPUT_ROOT / "test_run" / "alpha_relwide_spec.py"
AUTO_VARIANT_MULT = 2
AUTO_VARIANT_CAP = 2000
AUTO_SAMPLE_CAP = 2000
AUTO_NEIGHBORHOOD_MIN = 0.65
AUTO_EXTRA_STEPS = 2

DEFAULT_RANGE_PRESETS = {
    "core": [
        # place_thresh stays runtime-resolved so the per-symbol seed value can
        # recenter the sweep window (low-vol vs high-vol symbols differ a lot).
        ("place_thresh", {
            "path": ["pktraders", "ordex", "place_thresh"],
            "min": 0.0001,
            "max": 0.0015,
            "steps": 7,
            "neighborhood_pct": 0.25,
        }),
        # Modes 1 and 2 add a spread-driven term to place_thresh; mode 0
        # (constant) is excluded from this sweep.
        ("place_thresh_mode", {
            "path": ["pktraders", "ordex", "place_thresh_mode"],
            "values": [1, 2],
        }),
        # Multiplier on the spread term used when place_thresh_mode is 1 or 2;
        # only meaningful in those modes.
        ("place_thresh_spread_coef", {
            "path": ["pktraders", "ordex", "place_thresh_spread_coef"],
            "values": [0.25, 0.4, 0.5, 0.75, 1.0, 2.0],
        }),
        # Cancel hysteresis as a fraction of base place_thresh. The absolute
        # `cancel_buffer` key in the seed pk overrides this in C++, so the
        # spec generator emits a fixed_delete to strip it (see
        # _build_symbol_spec_entry).
        ("cancel_buffer_frac", {
            "path": ["pktraders", "ordex", "cancel_buffer_frac"],
            "values": [0.1, 0.25, 0.5, 0.75],
        }),
        ("lmr_time_const_s", {
            "path": ["pktraders", "ordex", "lmr_time_const_s"],
            "values": [4, 60],
        }),
        ("lmr_ema_mult", {
            "path": ["pktraders", "ordex", "lmr_ema_mult"],
            "values": [1.0],
        }),
        ("lmr_local_only", {
            "path": ["pktraders", "ordex", "lmr_local_only"],
            "values": [True, False],
        }),
        ("pcurvicity", {
            "path": ["pktraders", "ordex", "pcurvicity"],
            "values": [0.5, 1, 2.5],
        }),
        ("pcurvicity_coef", {
            "path": ["pktraders", "ordex", "pcurvicity_coef"],
            "values": [0.25, 0.5, 0.75, 2],
        }),
        # Locked on; unlikely to want one-sided=False for these symbols.
        ("ladder_one_sided", {
            "path": ["pktraders", "ordex", "ladder_one_sided"],
            "values": [True],
        }),
        # Multiplier on the regressed alpha signal applied to the predicted
        # price. Default 1.0 trusts the regression scaling; sweep tests
        # mild dampening/amplification, plus a 2.0 stretch case (the
        # script header notes higher mult tends to help commodities).
        ("alpha_mult", {
            "path": ["pktraders", "ordex", "alpha_mult"],
            "values": [0.5, 0.9, 1.0, 1.1, 2.0],
        }),
    ],
    "momentum": [
        ("pred_momentum_coef", {
            "path": ["pktraders", "ordex", "pred_momentum_coef"],
            "values": [0.0, 0.5, 1.0, 2.0, 3.0],
        }),
        ("pred_momentum_tdc", {
            "path": ["pktraders", "ordex", "pred_momentum_tdc"],
            "values": [1, 5, 15, 30],
        }),
        ("curv_impulse_coef", {
            "path": ["pktraders", "ordex", "curv_impulse_coef"],
            "values": [0.0, 1.0, 2.0, 4.0],
        }),
        ("curv_impulse_tdc", {
            "path": ["pktraders", "ordex", "curv_impulse_tdc"],
            "values": [5, 40],
        }),
    ],
    "spacing": [
        ("max_back_levels", {
            "path": ["pktraders", "ordex", "max_back_levels"],
            "min": 1,
            "max": 8,
            "steps": 5,
            "neighborhood_pct": 0.5,
        }),
        ("rung_spacing_mult", {
            "path": ["pktraders", "ordex", "rung_spacing_mult"],
            "min": 0.5,
            "max": 4.0,
            "steps": 5,
            "neighborhood_pct": 0.4,
        }),
        ("per_backlevel_rung_spacing_mult", {
            "path": ["pktraders", "ordex", "per_backlevel_rung_spacing_mult"],
            "min": 1.0,
            "max": 5.0,
            "steps": 5,
            "neighborhood_pct": 0.4,
        }),
        ("front_rung_spacing_mult", {
            "path": ["pktraders", "ordex", "front_rung_spacing_mult"],
            "min": 0.3,
            "max": 1.5,
            "steps": 4,
            "neighborhood_pct": 0.4,
        }),
    ],
    "cross": [
        ("want_cross", {
            "path": ["pktraders", "ordex", "want_cross"],
            "values": [True, False],
        }),
        ("cross_v2_lspread_thresh", {
            "path": ["pktraders", "ordex", "cross_v2_lspread_thresh"],
            "min": 5.0,
            "max": 20.0,
            "steps": 5,
            "neighborhood_pct": 0.4,
        }),
        ("cross_v2_exit_adjust", {
            "path": ["pktraders", "ordex", "cross_v2_exit_adjust"],
            "min": 0.5,
            "max": 1.1,
            "steps": 5,
            "neighborhood_pct": 0.2,
        }),
        ("cross_v2_pred_mode", {
            "path": ["pktraders", "ordex", "cross_v2_pred_mode"],
            "min": 0,
            "max": 3,
            "steps": 4,
            "neighborhood_pct": 0.6,
        }),
        ("TL2_aggr_exit_thresh", {
            "path": ["pktraders", "ordex", "TL2_aggr_exit_thresh"],
            "min": 0.0001,
            "max": 0.002,
            "steps": 5,
            "neighborhood_pct": 0.3,
        }),
        ("getflat_pct_away", {
            "path": ["pktraders", "ordex", "getflat_pct_away"],
            "min": 0.0005,
            "max": 0.02,
            "steps": 5,
            "neighborhood_pct": 0.3,
        }),
    ],
    "relcross_core": [
        ("base_cross_thresh", {
            "path": ["pktraders", "ordex", "base_cross_thresh"],
            "values": [0.0003, 0.0005, 0.0008, 0.001, 0.0015, 0.002, 0.003],
        }),
        ("premium_ema_coef", {
            "path": ["pktraders", "ordex", "premium_ema_coef"],
            "values": [0.0, 0.3, 0.5, 0.7, 1.0],
        }),
        ("exit_adjust", {
            "path": ["pktraders", "ordex", "exit_adjust"],
            "values": [0.6, 0.8, 1.0, 1.2],
        }),
        ("spread_thresh_coef", {
            "path": ["pktraders", "ordex", "spread_thresh_coef"],
            "values": [0.0, 0.25, 0.5, 1.0],
        }),
        ("cross_price_mode", {
            "path": ["pktraders", "ordex", "cross_price_mode"],
            "values": [0, 1, 2],
        }),
        ("tgt_maxpos_notional", {
            "path": ["pktraders", "ordex", "tgt_maxpos_notional"],
            "values": [1000, 3000, 5000, 8000],
        }),
        ("per_order_widen_frac", {
            "path": ["pktraders", "ordex", "per_order_widen_frac"],
            "values": [0.05, 0.1, 0.2, 0.3],
        }),
    ],
    "vol_widen": [
        ("vol_widen_coef", {
            "path": ["pktraders", "ordex", "vol_widen_coef"],
            "min": 0.0,
            "max": 0.4,
            "steps": 6,
            "neighborhood_pct": 0.3,
        }),
        ("vol_widen_tdc", {
            "path": ["pktraders", "ordex", "vol_widen_tdc"],
            "min": 1,
            "max": 20,
            "steps": 5,
            "neighborhood_pct": 0.3,
        }),
        ("vol_widen_additive", {
            "path": ["pktraders", "ordex", "vol_widen_additive"],
            "values": [True],
        }),
    ],
    "cancel": [
        ("cancel_buffer_frac", {
            "path": ["pktraders", "ordex", "cancel_buffer_frac"],
            "values": [0.1, 0.25, 0.4, 0.6],
        }),
    ],
    "vol_norm": [
        ("vol_norm_coef", {
            "path": ["pktraders", "ordex", "vol_norm_coef"],
            "values": [0.5, 1, 2, 4],
        }),
        ("vol_norm_tdc", {
            "path": ["pktraders", "ordex", "vol_norm_tdc"],
            "values": [4, 10, 40],
        }),
    ],
    "relcross_passive": [
        ("passive_exit_enabled", {
            "path": ["pktraders", "ordex", "passive_exit_enabled"],
            "values": [False, True],
        }),
        ("passive_exit_pct", {
            "path": ["pktraders", "ordex", "passive_exit_pct"],
            "values": [0.0005, 0.001, 0.0015, 0.002],
        }),
        ("passive_exit_style", {
            "path": ["pktraders", "ordex", "passive_exit_style"],
            "values": [0, 1],
        }),
        ("passive_exit_decay_per_min", {
            "path": ["pktraders", "ordex", "passive_exit_decay_per_min"],
            "values": [0.0002, 0.0005, 0.001],
        }),
        ("passive_exit_min_pct", {
            "path": ["pktraders", "ordex", "passive_exit_min_pct"],
            "values": [0.0001, 0.0002, 0.0003],
        }),
        ("size_signal_max_mult", {
            "path": ["pktraders", "ordex", "size_signal_max_mult"],
            "values": [0.0, 0.5, 1.0, 1.5, 2.0],
        }),
    ],
}

DEFAULT_FIXED = [
    (["pktraders", "ordex", "max_back_levels"], 3),
    (["pktraders", "ordex", "sanitize_lspread"], True),
    (["pktraders", "ordex", "lmr_ema_mult"], 1.0),
    (["pktraders", "ordex", "adding_no_cutin"], True),
]

DEFAULT_SETTINGS_TEMPLATE = {
    "commissions": {
        "tiers": {"Hyperliquid": "HIP3_3"},
        "promo_tier": "Silver",
        "use_promo": True,
    },
    "start_t": "18:00:00 America/New_York",
    "end_t": "17:59:55 America/New_York",
}

# Short aliases for SESSION_TIMES keys.
SESSION_ALIASES = {
    "usday": "US Day",
    "preus": "Pre-US",
    "postus": "Post-US",
    "overnight": "Overnight",
    "allday": None,  # Use the 18:00-17:59 default (no override).
}

def _default_session_for_symbol(symbol: str) -> str | None:
    """Return a session alias if the symbol should default to restricted hours."""
    if is_us_equity(symbol):
        return "usday"
    return None


def _apply_session_to_settings(settings: dict, session: str | None) -> None:
    """Override start_t/end_t in settings dict based on session label."""
    if not session:
        return
    resolved = SESSION_ALIASES.get(session, session)
    if resolved is None:
        return  # "allday" — keep defaults
    times = SESSION_TIMES.get(resolved)
    if not times:
        print(f"  Warning: unknown session '{session}', keeping default times")
        return
    settings["start_t"] = times[0]
    settings["end_t"] = times[1]

WAREHOUSE_FILENAME = "warehouse.sqlite3"
BEST_VARIANTS_FILENAME = "best_variants.json"

DEFAULT_SIM_TEMPLATE = {
    "Hyperliquid": {
        "cxl_frac_ahead": 0.3,
        "use_one_way_latency": False,
        "sim_latency_secs": 1.0,
    }
}

DEFAULT_RISK_TEMPLATE = {
    "max_notional": 40000,
    "max_position": "100000",
    "max_orders": 20,
    "base_currency": "USDT",
    "global_inherit": True,
    "load_overnight_pos": False,
    "min_pnl": -2000,
    "fv_limit": 2,
    "timeout_minfv": 900,
}

SUPPORTED_ORDEX_TYPES = {"AlphaRelWideMM", "RelWideMM2", "RelCross"}

DEFAULT_SAVED_STRATS_ROOT = REPO_ROOT / "overmind" / "for_live" / "saved_strats"
DEFAULT_MD_EXISTS_SCRIPT = REPO_ROOT / "pybin" / "md_exists.py"
DEFAULT_MD_CHECK_BUFFER_DAYS = 7

ORDEX_TEMPLATE_DIR = SCRIPT_DIR.parents[2] / "TEMPLATES" / "ordexes"
_ORDEX_TEMPLATE_CACHE: dict[str, dict] = {}


def _load_ordex_template(name: str) -> dict:
    if name in _ORDEX_TEMPLATE_CACHE:
        return _ORDEX_TEMPLATE_CACHE[name]
    path = ORDEX_TEMPLATE_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Ordex template not found: {path}")
    with path.open() as fh:
        data = commentjson.load(fh)
    payload = data.get("ordex", data)
    _ORDEX_TEMPLATE_CACHE[name] = payload
    return payload


def _ordex_template_alpha_relwide() -> dict:
    return copy.deepcopy(_load_ordex_template("AlphaRelWideMM"))


def _ordex_template_relwide_mm2() -> dict:
    return copy.deepcopy(_load_ordex_template("RelWideMM2"))


def _ordex_template_relcross() -> dict:
    return copy.deepcopy(_load_ordex_template("RelCross"))


DEFAULT_ORDEX_TEMPLATES = {
    "AlphaRelWideMM": _ordex_template_alpha_relwide,
    "RelWideMM2": _ordex_template_relwide_mm2,
    "RelCross": _ordex_template_relcross,
}


def _symbol_slug(symbol: str) -> str:
    return symbol.replace(":", "_")


def _load_config(path: Path) -> dict:
    with path.open() as fh:
        return commentjson.load(fh)


def _del_param_from_config(pk_dct: dict, path: list) -> None:
    # Walk a param path in a loaded pk and delete the leaf key if present.
    # Mirrors _get_param_from_config's pktraders-as-list convention (idx 0).
    cur = pk_dct
    for key in path[:-1]:
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict) or key not in cur:
            return
        cur = cur[key]
    if isinstance(cur, list):
        cur = cur[0] if cur else None
    if isinstance(cur, dict):
        cur.pop(path[-1], None)


def _apply_symbol_preferences(range_dims: list, preferences: dict) -> list:
    # Override values for any leaf already present, then append leaves that
    # only live in preferences. The rooted path for injected leaves is
    # ["pktraders", "ordex", <leaf>] — same convention as the presets.
    if not preferences:
        return range_dims
    seen: set[str] = set()
    out: list = []
    for path, spec in range_dims:
        leaf = path[-1] if path else ""
        if leaf in preferences:
            spec = {"values": list(preferences[leaf])}
        seen.add(leaf)
        out.append((list(path), spec))
    for leaf, vals in preferences.items():
        if leaf in seen:
            continue
        out.append((["pktraders", "ordex", leaf], {"values": list(vals)}))
    return out


def _write_spec_file(spec_path: Path, content: str) -> None:
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(content)


def _write_run_script(spec_path: Path) -> None:
    run_path = spec_path.parent / "run.sh"
    script = f"""#!/usr/bin/env bash
set -euo pipefail
/home/pktrade/tradefi/retraded_8/pybin/alpha_relwide_autosearch.py run-spec --run-spec "$(dirname "$0")/alpha_relwide_spec.py"
"""
    run_path.write_text(script)
    run_path.chmod(0o755)


_SAVED_STRATS_CACHE: dict[str, dict[str, list[tuple[int, Path]]]] = {}
_SYMBOL_STATS_CACHE: dict[tuple[str, str, int], dict[str, float]] = {}


def _saved_strats_root(args) -> Path:
    root = getattr(args, "saved_strats_root", None) or DEFAULT_SAVED_STRATS_ROOT
    return Path(root).expanduser()


def _saved_strat_priority(name: str) -> int:
    lowered = name.lower()
    if "usday" in lowered and "2usday" not in lowered:
        return 4
    if "2usday" in lowered:
        return 3
    if "allday" in lowered:
        return 2
    if "day" in lowered:
        return 1
    return 0


def _build_saved_strat_index(root: Path) -> dict[str, list[tuple[int, Path]]]:
    key = str(root)
    if key in _SAVED_STRATS_CACHE:
        return _SAVED_STRATS_CACHE[key]
    index: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    if not root.exists():
        _SAVED_STRATS_CACHE[key] = {}
        return {}
    for path in root.rglob("pk_*.json"):
        try:
            with path.open() as fh:
                payload = json.load(fh)
            trader = payload.get("pktraders", [{}])[0]
            traded_symbol = trader.get("traded_symbol")
            if not traded_symbol:
                continue
            priority = _saved_strat_priority(path.name)
            index[traded_symbol.upper()].append((priority, path))
        except (OSError, json.JSONDecodeError, IndexError, AttributeError):
            continue
    for symbol, entries in index.items():
        entries.sort(key=lambda item: item[0], reverse=True)
    _SAVED_STRATS_CACHE[key] = index
    return index


def _find_saved_strat_config(symbol: str, root: Path) -> Path | None:
    index = _build_saved_strat_index(root)
    entries = index.get(symbol.upper())
    if not entries:
        return None
    return entries[0][1]


def _recent_trade_dates(sim_days: int) -> list[str]:
    end_date = _today()
    start_date = end_date - timedelta(days=sim_days + 7)
    dates = dates_list(start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"),
                       dates_method="WEEKDAYS")
    if len(dates) > sim_days:
        dates = dates[-sim_days:]
    return dates


def _fetch_symbol_stats(symbol: str, market: str, sim_days: int,
                        start_t: str, end_t: str) -> dict[str, float] | None:
    key = (symbol, market, sim_days)
    if key in _SYMBOL_STATS_CACHE:
        return _SYMBOL_STATS_CACHE[key]
    dates = _recent_trade_dates(sim_days)
    if not dates:
        return None
    try:
        stats = DailyStats.run_symstats(symbol, market, dates, start_t, end_t)
    except Exception as exc:  # pragma: no cover - external binary failures
        print(f"  Warning: symstats failed for {symbol} ({market}): {exc}")
        _SYMBOL_STATS_CACHE[key] = None
        return None
    avg_stats = (stats or {}).get("avg") if isinstance(stats, dict) else None
    if not avg_stats:
        _SYMBOL_STATS_CACHE[key] = None
        return None
    _SYMBOL_STATS_CACHE[key] = avg_stats
    return avg_stats


def _write_spec_payload(spec_path: Path, spec: dict) -> None:
    formatted = "# Auto-generated alpha_relwide spec\n"
    formatted += f"sim_days = {spec['sim_days']}\n"
    formatted += f"max_variants = {spec['max_variants']}\n"
    formatted += f"sample_size = {spec['sample_size']}\n"
    formatted += f"downsample_mode = '{spec['downsample_mode']}'\n"
    formatted += f"neighborhood_pct = {spec['neighborhood_pct']}\n"
    formatted += "symbols = {}\n".format(pformat(spec["symbols"], indent=2, width=120))
    _write_spec_file(spec_path, formatted)


def _parse_symbol_inputs(args: argparse.Namespace) -> list[str]:
    symbols: list[str] = []
    if getattr(args, "symbol", None):
        symbols.append(args.symbol)
    if getattr(args, "symbols", None):
        symbols.extend([
            s.strip() for s in args.symbols.split(",") if s.strip()
        ])
    symbols_file = getattr(args, "symbols_file", None)
    if symbols_file:
        file_path = Path(symbols_file).expanduser()
        if not file_path.exists():
            raise FileNotFoundError(f"Symbols file not found: {file_path}")
        with file_path.open() as fh:
            for line in fh:
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                symbols.append(entry)
    # Deduplicate but keep order.
    seen = set()
    result = []
    for sym in symbols:
        if sym in seen:
            continue
        seen.add(sym)
        result.append(sym)
    return result


def _parse_symbol_ordex_overrides(args: argparse.Namespace) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in getattr(args, "symbol_ordex", []) or []:
        if "=" not in item:
            raise ValueError(f"Invalid --symbol-ordex entry '{item}', expected symbol=TYPE")
        sym, ordex_type = [part.strip() for part in item.split("=", 1)]
        if not sym or not ordex_type:
            raise ValueError(f"Invalid --symbol-ordex entry '{item}', expected symbol=TYPE")
        if ordex_type not in SUPPORTED_ORDEX_TYPES:
            raise ValueError(
                f"Unsupported ordex type '{ordex_type}' for {sym}; choose from {sorted(SUPPORTED_ORDEX_TYPES)}"
            )
        overrides[sym] = ordex_type
    return overrides


def _range_dims_from_presets(presets_arg: str) -> list[tuple[list[str], dict]]:
    presets = [p.strip() for p in (presets_arg or "").split(",") if p.strip()]
    if not presets:
        raise ValueError("Choose at least one preset via --presets")
    range_dims = []
    for preset in presets:
        if preset not in DEFAULT_RANGE_PRESETS:
            raise ValueError(f"Unknown preset: {preset}")
        for _, block in DEFAULT_RANGE_PRESETS[preset]:
            entry = (block["path"], {k: v for k, v in block.items() if k != "path"})
            range_dims.append(entry)
    return range_dims


def _parse_yyyymmdd(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y%m%d").date()
    except ValueError:
        return None


def _latest_bundle_info(symbol: str, venue: str, roots: list[Path]) -> dict | None:
    slug = _symbol_slug(symbol).upper()
    candidates: list[tuple[str, Path, dict]] = []
    for root in roots:
        base = (root / venue / slug)
        if not base.exists():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            bundle_path = child / "model_bundle.json"
            if bundle_path.exists():
                try:
                    with bundle_path.open() as fh:
                        data = json.load(fh)
                    built_at = data.get("metadata", {}).get("built_at", child.name)
                    candidates.append((built_at, bundle_path, data))
                except (OSError, json.JSONDecodeError):
                    continue
    if not candidates:
        return None
    candidates.sort(key=lambda tup: tup[0], reverse=True)
    best_built, best_path, bundle = candidates[0]
    metadata = bundle.get("metadata", {}) or {}
    built_at = metadata.get("built_at", best_built)
    metadata.setdefault("built_at", built_at)
    metadata.setdefault("bundle_path", str(best_path))
    return {
        "bundle": bundle,
        "metadata": metadata,
        "path": best_path,
        "built_at": built_at,
    }


def _find_model_bundle(symbol: str, venue: str, roots: list[Path]) -> dict | None:
    info = _latest_bundle_info(symbol, venue, roots)
    if not info:
        return None
    return info["bundle"]


def _regression_window(ins_days: int, oos_days: int) -> tuple[str, str, str, str]:
    today = _today()
    ins_end = today - timedelta(days=1)
    ins_start = ins_end - timedelta(days=max(ins_days, 5))
    oos_end = ins_end
    oos_start = oos_end - timedelta(days=max(oos_days, 3))
    return (
        ins_start.strftime("%Y%m%d"),
        ins_end.strftime("%Y%m%d"),
        oos_start.strftime("%Y%m%d"),
        oos_end.strftime("%Y%m%d"),
    )


def _resolve_output_dir(explicit: str | None, symbols: list[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    today = _today().strftime("%Y%m%d")
    if len(symbols) == 1:
        return DEFAULT_OUTPUT_ROOT / today / _symbol_slug(symbols[0])
    return DEFAULT_OUTPUT_ROOT / today / "multi"


def _scan_autobuild_runs(symbol: str, workdir: Path) -> dict | None:
    slug = _symbol_slug(symbol)
    prefix = f"autobuild_{slug}_"
    if not workdir.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for child in workdir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith(prefix):
            continue
        stamp = name[len(prefix):]
        candidates.append((stamp, child))
    if not candidates:
        return None
    candidates.sort(key=lambda tup: tup[0], reverse=True)
    stamp, path = candidates[0]
    return {
        "stamp": stamp,
        "path": path,
        "date": _parse_yyyymmdd(stamp),
    }


def _autobuild_model_bundle(symbol: str, remote_meta: dict, args) -> dict:
    template_path = Path(args.bootstrap_template or DEFAULT_REGRESSION_TEMPLATE)
    if not template_path.exists():
        raise FileNotFoundError(f"Regression template not found: {template_path}")
    with template_path.open() as fh:
        template = json.load(fh)
    start_ins, end_ins, start_oos, end_oos = _regression_window(
        args.bootstrap_ins_days, args.bootstrap_oos_days
    )
    template["traded_symbols"] = [symbol]
    template["traded_markets"] = ["Hyperliquid"]
    template["remote_product"]["symbol"] = remote_meta["remote_symbol"]
    template["remote_product"]["markets"] = [remote_meta["remote_market"]]
    template["start_date"] = start_ins
    template["end_date"] = end_ins
    template["start_date_oos"] = start_oos
    template["end_date_oos"] = end_oos

    job_root = Path(args.bootstrap_workdir).expanduser()
    job_root.mkdir(parents=True, exist_ok=True)
    slug = _symbol_slug(symbol)
    job_dir = job_root / f"autobuild_{slug}_{_today():%Y%m%d}"
    if job_dir.exists():
        shutil.rmtree(job_dir)
    job_dir.mkdir(parents=True)
    conf_path = job_dir / "relwide_conf.json"
    conf_path.write_text(json.dumps(template, indent=2))
    cmd = [
        sys.executable,
        str(REPO_ROOT / "overmind" / "strat_main" / "stratbuilder" / "TradeArmory.py"),
        "--conf",
        str(conf_path),
        "--run_until",
        "final_fit",
    ]
    print(f"  bootstrapping regression for {symbol}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(job_dir))
    if proc.returncode != 0:
        raise RuntimeError(f"Regression command failed for {symbol} ({proc.returncode})")
    coef_dir = job_dir / "coef_lair"
    pred_file = coef_dir / "pred_sig.json"
    ref_file = coef_dir / "ref_sig.json"
    if not pred_file.exists() or not ref_file.exists():
        raise FileNotFoundError(f"Missing pred/ref sig outputs in {coef_dir}")
    import_args = argparse.Namespace(
        symbol=symbol,
        venue="Hyperliquid",
        source_dir=str(coef_dir),
        pred_file="pred_sig.json",
        ref_file="ref_sig.json",
        built_at=_today().strftime("%Y%m%d"),
        start_date=start_ins,
        end_date=end_ins,
        default_start=start_ins,
        default_end=end_ins,
        quality=args.bootstrap_quality,
        score=None,
        scratch_root=args.model_bank_root or str(DEFAULT_MODEL_BANK_ROOTS[0]),
        catalog=args.model_bank_catalog or str(DEFAULT_MODEL_BANK_CATALOG),
    )
    relwide_model_bank.cmd_import(import_args)
    print(f"  regression bundle imported for {symbol}")
    roots = []
    if args.model_bank_root:
        roots.append(Path(args.model_bank_root).expanduser())
    roots.extend(DEFAULT_MODEL_BANK_ROOTS)
    bundle = _find_model_bundle(symbol, "Hyperliquid", roots)
    if not bundle:
        raise FileNotFoundError(f"Bundle still missing for {symbol} after autobuild")
    return bundle


def _build_default_trader(symbol: str, remote_meta: dict, ordex_type: str,
                          model_bundle: dict | None) -> dict:
    slug = _symbol_slug(symbol).lower()
    remote_symbol = remote_meta["remote_symbol"]
    remote_slug = remote_symbol.lower()
    relative_symbol = remote_meta.get("relative_symbol", "BTC")
    tempo_symbol = remote_meta.get("tempo_symbol", remote_symbol)
    tempo_market = remote_meta.get("tempo_market", remote_meta["remote_market"])

    template_factory = DEFAULT_ORDEX_TEMPLATES[ordex_type]
    trader = {
        "traded_symbol": symbol,
        "enabled": True,
        "size_mult": 1,
        "risk": copy.deepcopy(DEFAULT_RISK_TEMPLATE),
        "ordex": template_factory(),
        "signals": [],
        "tempos": [],
        "extra_subs": [],
    }

    ordex = trader["ordex"]
    ordex["type"] = ordex_type
    ordex["markets"] = ["Hyperliquid"]
    ordex["trade_caller"] = f"remote_tradecall_tempo_{remote_slug}"
    ordex["local_sig"] = f"local_mid_{slug}"
    ordex["remote_sig"] = f"remote_mid_{remote_slug}"
    ordex["relative_sig"] = f"rel_mid_{slug}"
    ordex["riskfree_rate"] = remote_meta.get("riskfree_rate", 0)

    signals_block = [
        {
            "name": ordex["local_sig"],
            "type": "SigMid",
            "symbol": symbol,
            "books": ["Hyperliquid"],
            "use_flagged": True,
            "sigformer_tag": "top_level_signal",
        },
        {
            "name": ordex["relative_sig"],
            "type": "SigMid",
            "symbol": relative_symbol,
            "books": ["Hyperliquid"],
            "use_flagged": True,
            "sigformer_tag": "top_level_signal",
        },
        {
            "name": ordex["remote_sig"],
            "type": "SigQuoteMid",
            "symbol": remote_symbol,
            "books": [remote_meta["remote_market"]],
            "use_flagged": True,
            "sigformer_tag": "top_level_signal",
        },
    ]
    if ordex_type == "AlphaRelWideMM":
        if not model_bundle or "pred_signal" not in model_bundle:
            raise ValueError(
                f"Model bundle required for {symbol}; run relwide_model_bank.py first"
            )
        bundle_copy = json.loads(json.dumps(model_bundle["pred_signal"]))
        pred_signals = bundle_copy.get("signals", [])
        if not pred_signals:
            raise ValueError(f"Pred signal bundle for {symbol} is empty")
        ordex["pred_sig"] = pred_signals[0].get("name")
        signals_block.extend(pred_signals)

    trader["signals"] = signals_block
    trader["tempos"] = [
        {
            "name": ordex["trade_caller"],
            "type": "FinalTempo",
            "symbol": tempo_symbol,
            "markets": [tempo_market],
        }
    ]

    return trader


def _load_bundle_for_symbol(symbol: str, venue: str, custom_root: str | None) -> dict | None:
    roots: list[Path] = []
    if custom_root:
        roots.append(Path(custom_root).expanduser())
    roots.extend(DEFAULT_MODEL_BANK_ROOTS)
    return _find_model_bundle(symbol, venue, roots)


def _write_default_pk_config(symbol: str, dest_dir: Path, ordex_type: str,
                             model_bundle: dict | None,
                             session: str | None = None) -> Path:
    remote_meta = get_relwide_remote_meta(symbol)
    if not remote_meta:
        raise ValueError(
            f"No default RelWide wiring known for {symbol}; supply --base-config"
        )
    settings = copy.deepcopy(DEFAULT_SETTINGS_TEMPLATE)
    effective_session = session or _default_session_for_symbol(symbol)
    _apply_session_to_settings(settings, effective_session)
    if effective_session:
        print(f"  session: {effective_session} ({settings['start_t']} -> {settings['end_t']})")
    config = {
        "settings": settings,
        "simulation": copy.deepcopy(DEFAULT_SIM_TEMPLATE),
        "pktraders": [_build_default_trader(symbol, remote_meta, ordex_type, model_bundle)],
    }
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"base_auto_{_symbol_slug(symbol)}.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"  built default config for {symbol}: {path}")
    return path


def _prepare_base_config(symbol: str, base_config_arg: str | None, symbol_dir: Path,
                         ordex_type: str, model_bundle: dict | None,
                         session: str | None = None) -> Path:
    if base_config_arg:
        base_path = Path(base_config_arg).expanduser()
        if not base_path.exists():
            raise FileNotFoundError(f"Base config not found: {base_path}")
        dest = symbol_dir / f"base_{base_path.name}"
        shutil.copy2(base_path, dest)
        print(f"  copied base config for {symbol}: {dest}")
        return dest
    return _write_default_pk_config(symbol, symbol_dir, ordex_type, model_bundle,
                                    session=session)


def _maybe_check_market_data(config: dict, sim_days: int, buffer_days: int) -> None:
    if not DEFAULT_MD_EXISTS_SCRIPT.exists():
        return
    try:
        trader = config.get("pktraders", [{}])[0]
        traded_symbol = trader.get("traded_symbol")
        ordex = trader.get("ordex", {})
        markets = ordex.get("markets", [])
    except (IndexError, AttributeError):
        return
    if not traded_symbol or not markets:
        return
    end_date = _today()
    start_date = end_date - timedelta(days=max(sim_days + buffer_days, 5))
    cmd = [
        sys.executable,
        str(DEFAULT_MD_EXISTS_SCRIPT),
        "--sym",
        traded_symbol,
        "--market",
        markets[0],
        "--start",
        start_date.strftime("%Y%m%d"),
        "--end",
        end_date.strftime("%Y%m%d"),
    ]
    try:
        proc = subprocess.run(cmd, input="y\n", text=True, capture_output=True)
        if proc.returncode != 0:
            print(f"  Warning: md_exists returned {proc.returncode} for {traded_symbol}")
            if proc.stderr:
                print("    md_exists stderr:")
                for line in proc.stderr.strip().splitlines()[-5:]:
                    print(f"      {line}")
    except Exception as exc:  # pragma: no cover - md_exists is external
        print(f"  Warning: md_exists failed for {traded_symbol}: {exc}")


def _prepare_relcross_base_config(symbol: str, base_config_arg: str | None,
                                  symbol_dir: Path, args, sim_days: int | None) -> Path:
    if base_config_arg:
        return _prepare_base_config(symbol, base_config_arg, symbol_dir, "RelCross", None)
    if gen_relcross is None:
        raise RuntimeError("pybin/gen_relcross.py is unavailable; cannot auto-build RelCross configs")
    saved_root = _saved_strats_root(args)
    conf_path = _find_saved_strat_config(symbol, saved_root)
    if not conf_path:
        raise FileNotFoundError(
            f"No saved_strats config found for {symbol} under {saved_root}; supply --base-config"
        )
    config, traded_symbol = gen_relcross.generate_relcross_config(str(conf_path))
    if not config:
        raise ValueError(f"Failed to build RelCross config for {symbol} from {conf_path}")
    dest_dir = symbol_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"base_relcross_{_symbol_slug(symbol)}.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"  built RelCross config for {symbol} ({traded_symbol}): {path}")
    buffer_days = DEFAULT_MD_CHECK_BUFFER_DAYS
    eff_sim_days = sim_days or getattr(args, "sim_days", 14) or 14
    _maybe_check_market_data(config, eff_sim_days, buffer_days)
    return path


def _stat_based_param_values(path: list[str], spec: dict, stats: dict[str, float] | None,
                             pk_dct: dict) -> list[float] | None:
    if not stats:
        return None
    param = path[-1]
    current_val = None
    try:
        current_val = _get_param_from_config(pk_dct, path)
    except Exception:
        current_val = None
    if param in {"place_thresh", "base_cross_thresh"}:
        spread = stats.get("avg_spread_pct")
        if not spread or spread <= 0:
            return None
        base = max(spread, float(current_val or spread))
        min_bound = spec.get("min", base * 0.4)
        max_bound = max(spec.get("max", base * 3.0), base * 3.0)
        multipliers = [0.4, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0]
        values = [max(1e-6, min(max_bound, max(min_bound, base * mult))) for mult in multipliers]
        if current_val:
            values.append(max(1e-6, float(current_val)))
        return sorted({round(val, 8) for val in values})
    return None


def _tune_range_dims(range_dims: list[tuple[list[str], dict]], ordex_type: str,
                     pk_dct: dict, stats: dict[str, float] | None,
                     is_auto_generated: bool, base_neighborhood: float) -> list[tuple[list[str], dict]]:
    tuned: list[tuple[list[str], dict]] = []
    for path, spec in copy.deepcopy(range_dims):
        path_list = list(path)
        spec_copy = copy.deepcopy(spec)
        custom_values = _stat_based_param_values(path_list, spec_copy, stats, pk_dct)
        if custom_values:
            tuned.append((path_list, {"values": custom_values}))
            continue
        if is_auto_generated and isinstance(spec_copy, dict) and "min" in spec_copy:
            spec_copy["neighborhood_pct"] = max(
                spec_copy.get("neighborhood_pct", base_neighborhood),
                AUTO_NEIGHBORHOOD_MIN,
            )
            if "steps" in spec_copy:
                spec_copy["steps"] = min(spec_copy["steps"] + AUTO_EXTRA_STEPS, 15)
        tuned.append((path_list, spec_copy))
    return tuned


def _build_symbol_spec_entry(symbol: str, ordex_type: str, args: argparse.Namespace,
                             range_dims: list[tuple[list[str], dict]], output_dir: Path,
                             sym_max_variants: int, sym_sample_size: int,
                             downsample_mode: str, sim_days: int | None,
                             base_neighborhood: float) -> dict:
    symbol_dir = output_dir / _symbol_slug(symbol)
    symbol_dir.mkdir(parents=True, exist_ok=True)
    base_config_arg = getattr(args, "base_config", None)
    is_auto_generated = base_config_arg is None
    if ordex_type == "RelCross":
        base_copy = _prepare_relcross_base_config(symbol, base_config_arg, symbol_dir, args, sim_days)
    else:
        bundle = None
        if (not base_config_arg) and ordex_type == "AlphaRelWideMM":
            remote_meta = get_relwide_remote_meta(symbol)
            if not remote_meta:
                raise ValueError(f"No remote wiring defined for {symbol}")
            bundle = _load_bundle_for_symbol(
                symbol,
                "Hyperliquid",
                getattr(args, "model_bank_root", None),
            )
            if bundle is None and getattr(args, "bootstrap_models", False):
                bundle = _autobuild_model_bundle(symbol, remote_meta, args)
            if bundle is None:
                raise FileNotFoundError(
                    f"No model bundle found for {symbol}. Use --bootstrap-models or supply --base-config"
                )
        session = getattr(args, "session", None)
        base_copy = _prepare_base_config(symbol, base_config_arg, symbol_dir, ordex_type, bundle,
                                         session=session)
    pk_path = Path(base_copy)
    pk_dct = _load_config(pk_path)
    eff_sim_days = sim_days or getattr(args, "sim_days", 14) or 14
    _maybe_check_market_data(pk_dct, eff_sim_days, DEFAULT_MD_CHECK_BUFFER_DAYS)
    market = None
    trader_ordex = {}
    trader_cfg: dict[str, Any] = {}
    try:
        trader_cfg = pk_dct.get("pktraders", [{}])[0]
        trader_ordex = trader_cfg.get("ordex", {}) or {}
        markets = trader_ordex.get("markets", []) or []
        if markets:
            market = markets[0]
    except (IndexError, AttributeError):
        market = None
    stats = None
    if market:
        settings = pk_dct.get("settings", {})
        stats_start_t = settings.get("start_t", DEFAULT_SETTINGS_TEMPLATE["start_t"])
        stats_end_t = settings.get("end_t", DEFAULT_SETTINGS_TEMPLATE["end_t"])
        stats = _fetch_symbol_stats(
            trader_cfg.get("traded_symbol", symbol),
            market,
            sim_days or getattr(args, "sim_days", 14) or 14,
            stats_start_t,
            stats_end_t,
        )
    tuned_range_dims = _tune_range_dims(range_dims, ordex_type, pk_dct, stats,
                                        is_auto_generated, base_neighborhood)
    # Apply per-symbol preferences (overrides preset values, can inject new
    # dims). Disabled when --no-preferences is passed. Only meaningful for
    # AlphaRelWideMM ordex; other types use a different parameter family.
    if (getattr(args, "use_preferences", True)
            and ordex_type == "AlphaRelWideMM"):
        prefs = ALPHA_RELWIDE_PREFERRED_DIMS.get(symbol)
        if prefs:
            tuned_range_dims = _apply_symbol_preferences(tuned_range_dims, prefs)
    max_variants = sym_max_variants
    sample_size = sym_sample_size
    if is_auto_generated:
        max_variants = min(AUTO_VARIANT_CAP, sym_max_variants * AUTO_VARIANT_MULT)
        if sample_size:
            sample_size = min(AUTO_SAMPLE_CAP, sample_size * AUTO_VARIANT_MULT)
    # If we're sweeping cancel_buffer_frac, the seed's absolute cancel_buffer
    # would override it in the C++ ordex. Flag it for deletion at run time.
    fixed_delete: list[list[str]] = []
    swept_leaves = {path[-1] for path, _ in tuned_range_dims if path}
    if "cancel_buffer_frac" in swept_leaves:
        fixed_delete.append(["pktraders", "ordex", "cancel_buffer"])
    return {
        "base_config": str(base_copy),
        "range_dims": tuned_range_dims,
        "dims": [],
        "fixed": copy.deepcopy(DEFAULT_FIXED),
        "fixed_delete": fixed_delete,
        "max_variants": max_variants,
        "sample_size": sample_size,
        "downsample_mode": downsample_mode,
    }


def _find_coverage_gap_symbols(max_count: int = 1) -> list[tuple[str, list[int]]]:
    """Find symbols with coverage gaps (count <= max_count in any session).

    Returns list of (symbol, [overnight, preus, usday, postus]) tuples,
    only for symbols that have wiring in relwide_remote_wiring.
    """
    try:
        from coverage_report import (
            SESSIONS, LOCAL_BASE, _to_minutes, _session_overlaps,
            fetch_all_xyz_symbols, load_latest_configs, build_coverage,
        )
    except ImportError:
        raise RuntimeError("coverage_report.py not importable; check sys.path")

    all_xyz = fetch_all_xyz_symbols()
    configs = load_latest_configs(LOCAL_BASE, max_stale_days=4)
    coverage, _weekend = build_coverage(configs)

    gap_symbols = []
    for sym in sorted(set(all_xyz) | set(coverage.keys())):
        if not get_relwide_remote_meta(sym):
            continue
        entries = coverage.get(sym, [])
        counts = []
        has_gap = False
        for sess_name, sess_start, sess_end in SESSIONS:
            s_start = _to_minutes(*sess_start)
            s_end = _to_minutes(*sess_end)
            matching = [
                e for e in entries
                if _session_overlaps(e["start_m"], e["end_m"], s_start, s_end)
            ]
            counts.append(len(matching))
            if len(matching) <= max_count:
                has_gap = True
        if has_gap:
            gap_symbols.append((sym, counts))

    return gap_symbols


def from_coverage(args: argparse.Namespace) -> Path:
    """Generate spec from coverage report gaps."""
    max_count = getattr(args, "max_count", 1)
    gap_symbols = _find_coverage_gap_symbols(max_count=max_count)

    if not gap_symbols:
        print("No coverage gaps found for symbols with wiring.")
        return

    print(f"Found {len(gap_symbols)} symbols with coverage gaps (count <= {max_count}):")
    print(f"  {'Symbol':<20} {'Overnight':>10} {'Pre-US':>10} {'US Day':>10} {'Post-US':>10}")
    print(f"  {'-'*60}")
    for sym, counts in gap_symbols:
        cols = "".join(f"{c:>10}" for c in counts)
        print(f"  {sym:<20}{cols}")

    # Filter to only requested symbols if --symbols is given
    explicit = _parse_symbol_inputs(args)
    if explicit:
        explicit_set = set(explicit)
        gap_symbols = [(s, c) for s, c in gap_symbols if s in explicit_set]
        print(f"\nFiltered to {len(gap_symbols)} requested symbols.")

    # Exclude symbols if --exclude is given
    exclude = set()
    if getattr(args, "exclude", None):
        exclude = {s.strip() for s in args.exclude.split(",") if s.strip()}
        gap_symbols = [(s, c) for s, c in gap_symbols if s not in exclude]
        print(f"After exclusions: {len(gap_symbols)} symbols.")

    if not gap_symbols:
        print("No symbols remaining after filtering.")
        return

    # Delegate to generate_spec by setting symbols on args
    args.symbols = ",".join(s for s, _ in gap_symbols)
    args.symbol = None
    args.symbols_file = None
    return generate_spec(args)


def generate_spec(args: argparse.Namespace) -> Path:
    symbols = _parse_symbol_inputs(args)
    if not symbols:
        raise ValueError("Supply at least one symbol via --symbol/--symbols/--symbols-file")

    if getattr(args, "auto_ordex_type", None) and args.auto_ordex_type not in SUPPORTED_ORDEX_TYPES:
        raise ValueError(
            f"Unsupported --auto-ordex-type '{args.auto_ordex_type}'; choose from {sorted(SUPPORTED_ORDEX_TYPES)}"
        )
    ordex_overrides = _parse_symbol_ordex_overrides(args)

    presets_arg = args.presets
    if presets_arg == "core" and getattr(args, "auto_ordex_type", None) == "RelCross":
        presets_arg = "relcross_core"
    range_dims = _range_dims_from_presets(presets_arg)

    output_dir = _resolve_output_dir(args.output_dir, symbols)
    output_dir.mkdir(parents=True, exist_ok=True)

    spec_symbols: dict[str, dict] = {}
    for symbol in symbols:
        ordex_type = ordex_overrides.get(symbol, args.auto_ordex_type)
        spec_symbols[symbol] = _build_symbol_spec_entry(
            symbol,
            ordex_type,
            args,
            copy.deepcopy(range_dims),
            output_dir,
            args.max_variants,
            args.sample_size,
            args.downsample_mode,
            args.sim_days,
            args.neighborhood_pct,
        )

    spec = {
        "sim_days": args.sim_days,
        "max_variants": args.max_variants,
        "sample_size": args.sample_size,
        "downsample_mode": args.downsample_mode,
        "neighborhood_pct": args.neighborhood_pct,
        "symbols": spec_symbols,
    }

    spec_path = output_dir / "alpha_relwide_spec.py"
    _write_spec_payload(spec_path, spec)
    _write_run_script(spec_path)
    print(f"Spec written to {spec_path}")
    return spec_path


def onboard_symbol(args: argparse.Namespace) -> Path:
    symbol = args.symbol
    if not symbol:
        raise ValueError("Supply --symbol for onboarding")
    if args.ordex_type not in SUPPORTED_ORDEX_TYPES:
        raise ValueError(
            f"Unsupported --ordex-type '{args.ordex_type}'; choose from {sorted(SUPPORTED_ORDEX_TYPES)}"
        )
    spec_path = Path(args.spec_path).expanduser()
    spec_dir = spec_path.parent
    spec_dir.mkdir(parents=True, exist_ok=True)
    existing_symbols: dict[str, Any] = {}
    existing_sim_days = existing_max_variants = existing_sample_size = None
    existing_downsample = existing_neighborhood = None
    if spec_path.exists():
        spec_mod = _load_spec_module(spec_path)
        existing_symbols = copy.deepcopy(getattr(spec_mod, "symbols", {}))
        existing_sim_days = getattr(spec_mod, "sim_days", None)
        existing_max_variants = getattr(spec_mod, "max_variants", None)
        existing_sample_size = getattr(spec_mod, "sample_size", None)
        existing_downsample = getattr(spec_mod, "downsample_mode", None)
        existing_neighborhood = getattr(spec_mod, "neighborhood_pct", None)
    if symbol in existing_symbols and not args.force:
        raise ValueError(f"Symbol {symbol} already present; rerun with --force to replace")

    def _resolve(arg_val, existing_val, default_val):
        if arg_val is not None:
            return arg_val
        if existing_val is not None:
            return existing_val
        return default_val

    sim_days_val = _resolve(args.sim_days, existing_sim_days, 14)
    max_variants_val = _resolve(args.max_variants, existing_max_variants, 800)
    sample_size_val = _resolve(args.sample_size, existing_sample_size, 400)
    downsample_val = _resolve(args.downsample_mode, existing_downsample, "random")
    neighborhood_val = _resolve(args.neighborhood_pct, existing_neighborhood, 0.3)

    presets_arg = args.presets or "core"
    if presets_arg == "core" and args.ordex_type == "RelCross":
        presets_arg = "relcross_core"
    range_dims = _range_dims_from_presets(presets_arg)

    spec_symbols = copy.deepcopy(existing_symbols)
    spec_symbols[symbol] = _build_symbol_spec_entry(
        symbol,
        args.ordex_type,
        args,
        copy.deepcopy(range_dims),
        spec_dir,
        max_variants_val,
        sample_size_val,
        downsample_val,
        sim_days_val,
        neighborhood_val,
    )

    spec = {
        "sim_days": sim_days_val,
        "max_variants": max_variants_val,
        "sample_size": sample_size_val,
        "downsample_mode": downsample_val,
        "neighborhood_pct": neighborhood_val,
        "symbols": spec_symbols,
    }

    _write_spec_payload(spec_path, spec)
    _write_run_script(spec_path)
    print(f"Spec updated at {spec_path}")
    print(f"  Symbol {symbol} base config: {spec_symbols[symbol]['base_config']}")
    return spec_path


def check_bundles(args: argparse.Namespace) -> None:
    symbols = _parse_symbol_inputs(args)
    if not symbols:
        raise ValueError("Supply at least one symbol via --symbol/--symbols/--symbols-file")

    roots: list[Path] = []
    if args.model_bank_root:
        roots.append(Path(args.model_bank_root).expanduser())
    roots.extend(DEFAULT_MODEL_BANK_ROOTS)

    workdir = Path(args.bootstrap_workdir).expanduser()
    stale_days = max(0, args.stale_days)
    fresh = stale = missing = 0

    def describe_bundle(info: dict | None) -> tuple[str | None, int | None, bool, dict | None]:
        if not info:
            return None, None, False, None
        built_at = info.get("built_at") or info.get("metadata", {}).get("built_at")
        built_date = _parse_yyyymmdd(built_at)
        age = (date.today() - built_date).days if built_date else None
        is_stale = bool(age is not None and age > stale_days)
        return built_at, age, is_stale, info.get("metadata")

    for symbol in symbols:
        print(f"\n{symbol}")
        info = _latest_bundle_info(symbol, "Hyperliquid", roots)
        built_at, age, is_stale, metadata = describe_bundle(info)
        if info:
            status = "STALE" if is_stale else "FRESH"
            bundle_path = info.get("path")
            quality = (metadata or {}).get("quality", "unknown")
            score = (metadata or {}).get("score")
            age_desc = f"{age}d" if age is not None else "unknown"
            print(f"  Bundle: {status} — built {built_at or 'unknown'} ({age_desc}), quality={quality}")
            print(f"  Path: {bundle_path}")
            if score is not None:
                print(f"  Score: {score}")
        else:
            status = "MISSING"
            print("  Bundle: MISSING (no entries under relwide_modelbank)")

        last_job = _scan_autobuild_runs(symbol, workdir)
        if last_job:
            job_age = None
            if last_job.get("date"):
                job_age = (date.today() - last_job["date"]).days
            age_desc = f"{job_age}d" if job_age is not None else "unknown"
            print(f"  Last autobuild job: {last_job['stamp']} ({age_desc}) @ {last_job['path']}")
        else:
            if workdir.exists():
                print(f"  No autobuild jobs found under {workdir}")
            else:
                print(f"  Autobuild workdir missing: {workdir}")

        if args.bootstrap_models and status != "FRESH":
            remote_meta = get_relwide_remote_meta(symbol)
            if not remote_meta:
                print("  Cannot bootstrap: remote wiring unknown for symbol")
            else:
                try:
                    _autobuild_model_bundle(symbol, remote_meta, args)
                except Exception as exc:  # pragma: no cover - external process failure
                    print(f"  Bootstrap failed: {exc}")
                else:
                    info = _latest_bundle_info(symbol, "Hyperliquid", roots)
                    built_at, age, is_stale, metadata = describe_bundle(info)
                    if info:
                        bundle_path = info.get("path")
                        quality = (metadata or {}).get("quality", "unknown")
                        age_desc = f"{age}d" if age is not None else "unknown"
                        status = "STALE" if is_stale else "FRESH"
                        print(f"  Bootstrap complete: {status} bundle built {built_at or 'unknown'} ({age_desc}), quality={quality}")
                        print(f"  Path: {bundle_path}")
                    else:
                        status = "MISSING"
                        print("  Bootstrap completed but bundle still missing; inspect TradeArmory logs")

        if status == "FRESH":
            fresh += 1
        elif status == "STALE":
            stale += 1
        else:
            missing += 1

    print("\nBundle doctor summary:")
    print(f"  Fresh:   {fresh}")
    print(f"  Stale:   {stale}")
    print(f"  Missing: {missing}")


def _load_spec_module(spec_path: Path):
    spec = importlib.util.spec_from_file_location("alpha_relwide_spec", spec_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def _apply_sampling(dims, limit, mode, seed):
    """Down-sample Cartesian product until <= limit combinations remain."""
    if limit is None:
        return dims
    import math
    total = math.prod(len(vals) for _, vals in dims) if dims else 0
    if total <= limit:
        return dims
    if mode == "even":
        return _downsample_dims(dims, limit)
    # random mode
    rng = random.Random(seed)
    sizes = [len(vals) for _, vals in dims]
    while math.prod(sizes) > limit:
        candidates = [i for i, s in enumerate(sizes) if s > 2]
        if not candidates:
            break
        idx = rng.choice(candidates)
        sizes[idx] -= 1
    result = []
    for (path, vals), target_size in zip(dims, sizes):
        if target_size >= len(vals):
            result.append((path, vals))
        else:
            chosen = sorted(rng.sample(range(len(vals)), target_size))
            result.append((path, [vals[i] for i in chosen]))
    return result


def _top_variants(results_file: Path, top_k: int):
    """Return (headers, top_by_score, top_by_pnl, all_rows)."""
    try:
        with results_file.open() as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            rows = list(reader)
    except (OSError, csv.Error):
        return [], [], [], []
    if not rows or not headers:
        return [], [], [], []

    def _to_float(row, key):
        try:
            return float(row.get(key, "") or 0.0)
        except ValueError:
            return 0.0

    scored = sorted(
        rows,
        key=lambda row: (
            _to_float(row, "sim_score"),
            _to_float(row, "avg_pnl"),
            _to_float(row, "sharpe"),
            _to_float(row, "pct_positive"),
        ),
        reverse=True,
    )
    by_pnl = sorted(
        rows,
        key=lambda row: (
            _to_float(row, "avg_pnl"),
            _to_float(row, "sharpe"),
            _to_float(row, "pct_positive"),
            _to_float(row, "fillrate"),
        ),
        reverse=True,
    )
    return headers, scored[:top_k], by_pnl[:top_k], rows


def _new_run_id() -> str:
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{random.randint(0, 9999):04d}"


def _init_warehouse(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            spec_path TEXT NOT NULL,
            sim_days INTEGER,
            max_variants INTEGER,
            sample_size INTEGER,
            downsample_mode TEXT,
            neighborhood_pct REAL,
            sample_seed INTEGER,
            top_k INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_runs (
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            status TEXT NOT NULL,
            base_config TEXT,
            dims_count INTEGER,
            sim_days INTEGER,
            dates TEXT,
            max_variants INTEGER,
            sample_size INTEGER,
            downsample_mode TEXT,
            results_path TEXT,
            scratch_root TEXT,
            notes TEXT,
            PRIMARY KEY (run_id, symbol),
            FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS top_variants (
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            rank INTEGER NOT NULL,
            variant_id TEXT,
            metrics TEXT,
            scratch_dir TEXT,
            pk_path TEXT,
            results_path TEXT,
            PRIMARY KEY (run_id, symbol, rank),
            FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
        )
        """
    )


def _open_warehouse(results_dir: Path) -> tuple[sqlite3.Connection, Path]:
    path = results_dir / WAREHOUSE_FILENAME
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _init_warehouse(conn)
    return conn, path


def _record_run_metadata(conn: sqlite3.Connection, run_id: str, spec_path: Path,
                         sim_days: int, max_variants: int, sample_size: int,
                         downsample_mode: str, neighborhood_pct: float,
                         sample_seed: int, top_k: int) -> None:
    conn.execute(
        """
        INSERT INTO runs (id, created_at, spec_path, sim_days, max_variants,
                          sample_size, downsample_mode, neighborhood_pct,
                          sample_seed, top_k)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            datetime.utcnow().isoformat(timespec="seconds"),
            str(spec_path),
            sim_days,
            max_variants,
            sample_size,
            downsample_mode,
            neighborhood_pct,
            sample_seed,
            top_k,
        ),
    )


def _record_symbol_entry(conn: sqlite3.Connection, run_id: str, symbol: str,
                         *, status: str, base_config: str | None,
                         dims_count: int, sim_days: int,
                         dates: list[str], max_variants: int,
                         sample_size: int | None, downsample_mode: str,
                         results_path: Path, scratch_root: Path,
                         notes: str | None = None) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO symbol_runs
        (run_id, symbol, status, base_config, dims_count, sim_days, dates,
         max_variants, sample_size, downsample_mode, results_path,
         scratch_root, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            symbol,
            status,
            base_config,
            dims_count,
            sim_days,
            json.dumps(dates),
            max_variants,
            sample_size,
            downsample_mode,
            str(results_path),
            str(scratch_root),
            notes,
        ),
    )


def _variant_artifacts(sym_dir: Path, variant_id: str | None) -> tuple[Path | None, Path | None]:
    scratch_root = sym_dir / "scratch"
    if variant_id is None:
        return None, None
    variant_dir = scratch_root / str(variant_id)
    if not variant_dir.exists():
        return variant_dir, None
    pk_full = variant_dir / "pk_full.json"
    if pk_full.exists():
        return variant_dir, pk_full
    pk_json = variant_dir / "pk.json"
    if pk_json.exists():
        return variant_dir, pk_json
    return variant_dir, None


def _record_top_variants(conn: sqlite3.Connection, run_id: str, symbol: str,
                         top_rows: list[dict[str, Any]], sym_dir: Path,
                         results_file: Path) -> list[dict[str, Any]]:
    conn.execute(
        "DELETE FROM top_variants WHERE run_id = ? AND symbol = ?",
        (run_id, symbol),
    )
    snapshot_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(top_rows, start=1):
        variant_raw = row.get("variant")
        variant_id = str(variant_raw) if variant_raw is not None else None
        variant_dir, pk_path = _variant_artifacts(sym_dir, variant_id)
        metrics_json = json.dumps(row, sort_keys=True)
        conn.execute(
            """
            INSERT INTO top_variants
            (run_id, symbol, rank, variant_id, metrics, scratch_dir, pk_path, results_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                symbol,
                rank,
                variant_id,
                metrics_json,
                str(variant_dir) if variant_dir else None,
                str(pk_path) if pk_path else None,
                str(results_file),
            ),
        )
        snapshot_rows.append({
            "rank": rank,
            "variant": variant_id,
            "metrics": row,
            "scratch_dir": str(variant_dir) if variant_dir else None,
            "pk_path": str(pk_path) if pk_path else None,
            "results_file": str(results_file),
        })
    return snapshot_rows


def _write_aggregate_results(best_snapshot: dict[str, Any], aggregate_path: Path) -> None:
    headers = [
        "symbol",
        "traded_symbol",
        "variant",
        "place_thresh",
        "lmr_time_const_s",
        "avg_pnl",
        "avg_closed_pnl",
        "sharpe",
        "pct_positive",
        "num_trds",
        "sim_score",
        "base_config",
        "pk_path",
    ]
    rows = []

    def _float_or_zero(val: Any) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    for symbol, entry in best_snapshot.items():
        metrics = entry.get("metrics") or {}
        base_config = entry.get("base_config")
        traded_symbol = ""
        if base_config:
            try:
                cfg = _load_config(Path(base_config))
                traded_symbol = cfg.get("pktraders", [{}])[0].get("traded_symbol", "")
            except Exception:
                traded_symbol = ""
        row = {
            "symbol": symbol,
            "traded_symbol": traded_symbol,
            "variant": entry.get("variant", ""),
            "place_thresh": metrics.get("place_thresh", ""),
            "lmr_time_const_s": metrics.get("lmr_time_const_s", ""),
            "avg_pnl": metrics.get("avg_pnl", ""),
            "avg_closed_pnl": metrics.get("avg_closed_pnl", ""),
            "sharpe": metrics.get("sharpe", ""),
            "pct_positive": metrics.get("pct_positive", ""),
            "num_trds": metrics.get("num_trds", ""),
            "sim_score": metrics.get("sim_score", ""),
            "base_config": base_config or "",
            "pk_path": entry.get("pk_path", ""),
        }
        rows.append((symbol, row, _float_or_zero(row["sim_score"])))

    rows.sort(key=lambda tup: tup[2], reverse=True)
    aggregate_path.parent.mkdir(parents=True, exist_ok=True)
    with aggregate_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for _, row, _score in rows:
            writer.writerow([row[h] for h in headers])


def scout_thresh(args: argparse.Namespace) -> None:
    """Run a place_thresh-only scout sweep across symbols.

    Holds all other dims at DEFAULT_FIXED + base config defaults. Sweeps
    place_thresh across a log-spaced default range (or user-supplied values),
    one variant per value. Reports per-value trade stats and highlights
    productive values to use in the main multi-dim sweep.
    """
    import math
    import pprint as _pprint

    symbols = _parse_symbol_inputs(args)
    output_dir = _resolve_output_dir(args.output_dir, symbols) / "scout_thresh"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.values:
        values = [float(v) for v in args.values.split(",")]
    else:
        # Default: 12 log-spaced values from 1bp to 200bp.
        log_lo, log_hi = math.log10(0.0001), math.log10(0.02)
        values = [round(10 ** (log_lo + i * (log_hi - log_lo) / 11), 6) for i in range(12)]

    print(f"Scout-thresh: {len(symbols)} symbols × {len(values)} place_thresh values")
    print("Values: " + ", ".join(f"{v*10000:.1f}bp" for v in values))

    range_dims_scout = [(["pktraders", "ordex", "place_thresh"], {"values": values})]
    ordex_type = getattr(args, "ordex_type", None) or "AlphaRelWideMM"

    spec_symbols = {}
    for symbol in symbols:
        # Reuse the standard spec-entry builder; it handles bootstrap + base config + symstats.
        # We override sample_size to exactly len(values) so all are sampled.
        scout_inner_args = argparse.Namespace(**vars(args))
        scout_inner_args.use_preferences = False  # scout should not import per-symbol preferences
        entry = _build_symbol_spec_entry(
            symbol=symbol,
            ordex_type=ordex_type,
            args=scout_inner_args,
            range_dims=range_dims_scout,
            output_dir=output_dir,
            sym_max_variants=len(values),
            sym_sample_size=len(values),
            downsample_mode="even",
            sim_days=args.sim_days,
            base_neighborhood=0.3,
        )
        spec_symbols[symbol] = entry

    spec_path = output_dir / "scout_thresh_spec.py"
    with spec_path.open("w") as f:
        f.write(f"# Scout-thresh spec — generated {_today():%Y-%m-%d}\n")
        f.write(f"sim_days = {args.sim_days}\n")
        f.write(f"max_variants = {len(values)}\n")
        f.write(f"sample_size = {len(values)}\n")
        f.write("downsample_mode = 'even'\n")
        f.write("neighborhood_pct = 0.3\n")
        f.write("symbols = " + _pprint.pformat(spec_symbols, indent=2, width=120) + "\n")
    print(f"Spec: {spec_path}")

    run_args = argparse.Namespace(
        run_spec=str(spec_path),
        sim_days=args.sim_days,
        max_variants=len(values),
        sample_size=len(values),
        downsample_mode="even",
        neighborhood_pct=0.3,
        sample_seed=0,
        top_k=len(values),
        end_date=None,
    )
    run_spec(run_args)

    # ---- post-process: per-value table with highlights ----
    import csv as _csv
    print()
    print("=" * 110)
    print("SCOUT-THRESH RESULTS (full table; '***' marks recommended values for main sweep)")
    print("=" * 110)

    results_root = output_dir / "results"
    sim_days_used = args.sim_days
    trade_floor = max(int(0.5 * sim_days_used), 5)  # ≥0.5 trades/day or 5 over the window
    over_trade = sim_days_used * 50

    summary_lines = []
    for symbol in symbols:
        slug = _symbol_slug(symbol)
        rf = results_root / slug / "results.txt"
        if not rf.exists():
            print(f"\n{symbol}: no results file at {rf}")
            continue
        with rf.open() as f:
            rows = list(_csv.DictReader(f))
        try:
            rows.sort(key=lambda r: float(r.get("place_thresh", "0") or 0))
        except ValueError:
            pass

        print(f"\n=== {symbol} ===")
        print(f"  {'pt(raw)':>11} {'bp':>6} {'pnl':>9} {'sharpe':>7} {'pct_pos':>7} {'num_trds':>9} {'fillrate':>9} {'sim_score':>10}  flag")
        recs = []
        for r in rows:
            try:
                pt = float(r.get("place_thresh", "0") or 0)
                pnl = float(r.get("avg_pnl", "0") or 0)
                sh = float(r.get("sharpe", "0") or 0)
                pp = float(r.get("pct_positive", "0") or 0)
                nt = int(float(r.get("num_trds", "0") or 0))
                fr = float(r.get("fillrate", "0") or 0)
                sc = float(r.get("sim_score", "0") or 0)
            except (TypeError, ValueError):
                continue
            flag = ""
            if nt < max(trade_floor // 2, 2):
                flag = "(too wide / no trades)"
            elif nt > over_trade:
                flag = "(over-trading)"
            elif nt >= trade_floor and pp >= 0.67 and pnl > 0 and sh > 0:
                flag = "*** recommend ***"
                recs.append(pt)
            print(f"  {pt:>11.6f} {pt*10000:>5.1f}bp {pnl:>9.2f} {sh:>7.2f} {pp:>7.2f} {nt:>9} {fr:>9.4f} {sc:>10.2f}  {flag}")
        if recs:
            summary_lines.append(f"{symbol}: {len(recs)} recommended -> place_thresh values {recs}")
        else:
            summary_lines.append(f"{symbol}: no values met criteria (num_trds>={trade_floor}, pct_pos>=0.67, pnl>0, sharpe>0); inspect manually")

    print()
    print("=" * 110)
    print("RECOMMENDATION SUMMARY")
    print("=" * 110)
    for line in summary_lines:
        print("  " + line)
    print(f"\nResults dir: {results_root}")
    print(f"Use the recommended place_thresh values in your full sweep spec for each symbol.")


def run_spec(args: argparse.Namespace) -> None:
    spec_path = Path(args.run_spec)
    if not spec_path.exists():
        raise FileNotFoundError(spec_path)
    spec_mod = _load_spec_module(spec_path)
    symbols = getattr(spec_mod, "symbols", {})
    if not symbols:
        raise ValueError("Spec has no symbols")
    sim_days = getattr(spec_mod, "sim_days", args.sim_days)
    base_max_variants = getattr(spec_mod, "max_variants", args.max_variants)
    base_sample_size = getattr(spec_mod, "sample_size", args.sample_size)
    base_downsample_mode = getattr(spec_mod, "downsample_mode", args.downsample_mode)
    neighborhood_pct = getattr(spec_mod, "neighborhood_pct", args.neighborhood_pct)
    sample_seed = getattr(spec_mod, "sample_seed", args.sample_seed)
    top_k = getattr(spec_mod, "top_k", args.top_k)
    if top_k <= 0:
        top_k = 1

    spec_dir = spec_path.parent
    results_dir = spec_dir / "results"
    results_dir.mkdir(exist_ok=True)

    warehouse_path = results_dir / WAREHOUSE_FILENAME
    best_snapshot: dict[str, Any] = {}
    warehouse_conn = None
    run_id = _new_run_id()

    try:
        warehouse_conn, warehouse_path = _open_warehouse(results_dir)
        _record_run_metadata(
            warehouse_conn,
            run_id,
            spec_path,
            sim_days,
            base_max_variants,
            base_sample_size,
            base_downsample_mode,
            neighborhood_pct,
            sample_seed,
            top_k,
        )

        summary_lines = [
            f"Alpha RelWide Autosearch — {_today():%Y-%m-%d}",
            f"Run ID: {run_id}",
        ]

        for symbol, sym_spec in symbols.items():
            sym_dir = results_dir / _symbol_slug(symbol)
            sym_dir.mkdir(exist_ok=True)
            results_file = sym_dir / "results.txt"
            scratch_root = sym_dir / "scratch"
            sym_max_variants = sym_spec.get("max_variants", base_max_variants)
            sym_sample_size = sym_spec.get("sample_size", base_sample_size)
            downsample_mode = sym_spec.get("downsample_mode", base_downsample_mode)
            summary_lines.append(f"\n{symbol}")
            base_config = sym_spec.get("base_config")
            if base_config:
                summary_lines.append(f"  Base config: {base_config}")
            else:
                summary_lines.append("  Base config: (not provided)")

            def _record_skip(status: str, reason: str) -> None:
                summary_lines.append(f"  Skipped: {reason}")
                _record_symbol_entry(
                    warehouse_conn,
                    run_id,
                    symbol,
                    status=status,
                    base_config=base_config,
                    dims_count=0,
                    sim_days=sim_days,
                    dates=[],
                    max_variants=sym_max_variants,
                    sample_size=sym_sample_size,
                    downsample_mode=downsample_mode,
                    results_path=results_file,
                    scratch_root=scratch_root,
                    notes=reason,
                )
                _record_top_variants(warehouse_conn, run_id, symbol, [], sym_dir, results_file)

            if not base_config:
                reason = "no base_config"
                print(f"Skipping {symbol}: {reason}")
                _record_skip("SKIP_NO_CONFIG", reason)
                continue
            base_path = Path(base_config)
            if not base_path.exists():
                reason = f"missing base config {base_path}"
                print(f"Skipping {symbol}: {reason}")
                _record_skip("SKIP_MISSING_CONFIG", reason)
                continue
            pk_dct = _load_config(base_path)
            traders = [t for t in pk_dct.get("pktraders", []) if t.get("traded_symbol") == symbol]
            if not traders:
                reason = "symbol not in config"
                print(f"Skipping {symbol}: {reason}")
                _record_skip("SKIP_SYMBOL_NOT_FOUND", reason)
                continue
            pk_dct["pktraders"] = [traders[0]]
            # Strip any keys the spec marks as "delete before sim" (e.g.
            # cancel_buffer when cancel_buffer_frac is being swept — the
            # C++ ordex prefers cancel_buffer if both are set).
            for del_path in sym_spec.get("fixed_delete", []):
                _del_param_from_config(pk_dct, del_path)
            dims = sym_spec.get("dims", [])
            range_dims = sym_spec.get("range_dims", [])
            fixed = sym_spec.get("fixed", DEFAULT_FIXED)
            resolved = []
            if range_dims:
                resolved = _resolve_range_dims(range_dims, pk_dct, neighborhood_pct)
            all_dims = dims + resolved
            limit = sym_max_variants
            if sym_sample_size:
                limit = min(limit, sym_sample_size)
            overrides = _dims_to_overrides(all_dims)
            for path, val in fixed:
                overrides.append({"path": list(path), "val": val})
            total_variants = 1
            for ov in overrides:
                if isinstance(ov.get("val"), list):
                    total_variants *= len(ov["val"])
            random_sample = min(limit, total_variants) if limit and total_variants > limit else 0
            end_date = _today()
            start_date = end_date - timedelta(days=sim_days + 7)
            dates = dates_list(start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"),
                               dates_method="WEEKDAYS")
            if len(dates) > sim_days:
                dates = dates[-sim_days:]
            if random_sample > 0:
                print(f"Running {symbol}: {len(all_dims)} dims, random sample {random_sample} of {total_variants} variants, {len(dates)} days")
            else:
                print(f"Running {symbol}: {len(all_dims)} dims, {total_variants} variants, {len(dates)} days")
            sim_grid(overrides, pk_dct, str(sym_dir), dates, fast_sim=False, resume=True,
                     random_sample=random_sample,
                     remote=getattr(args, "remote", False),
                     hosts_yaml=getattr(args, "hosts", None),
                     chunk_size=getattr(args, "chunk_size", 50))
            has_results = results_file.exists()
            headers: list[str] = []
            top_rows: list[dict[str, Any]] = []
            if has_results:
                headers, top_rows, top_by_pnl, all_rows = _top_variants(results_file, top_k)
                if top_rows and headers:
                    def _emit(label: str, rows_to_emit: list[dict[str, Any]]):
                        summary_lines.append(f"  {label}:")
                        summary_lines.append("  " + ",".join(headers))
                        print(f"{label} for {symbol}:")
                        print(",".join(headers))
                        for row in rows_to_emit:
                            line = ",".join(str(row.get(h, "")) for h in headers)
                            summary_lines.append("  " + line)
                            print(line)
                    _emit(f"Top {len(top_rows)} by sim_score", top_rows)
                    _emit(f"Top {len(top_by_pnl)} by avg_pnl", top_by_pnl)

                    # Sparse-symbol warning: low trade count suggests fragile sample
                    def _to_float(row, key):
                        try:
                            return float(row.get(key, "") or 0.0)
                        except ValueError:
                            return 0.0
                    best_trds = _to_float(top_rows[0], "num_trds")
                    nt_sorted = sorted(_to_float(r, "num_trds") for r in all_rows)
                    median_trds = nt_sorted[len(nt_sorted)//2] if nt_sorted else 0.0
                    if best_trds < 20 or median_trds < 5:
                        warn = (f"  WARN: sparse trading — best variant has {int(best_trds)} trades, "
                                f"median {int(median_trds)}; treat sample as fragile.")
                        summary_lines.append(warn)
                        print(warn.strip())
                else:
                    summary_lines.append("  (no parsable results; see full table)")
                summary_lines.append(f"  Full results: {results_file}")
            else:
                summary_lines.append("  (no results file generated)")
                summary_lines.append(f"  Expected path: {results_file}")

            status = "COMPLETE" if has_results else "NO_RESULTS"
            _record_symbol_entry(
                warehouse_conn,
                run_id,
                symbol,
                status=status,
                base_config=str(base_path),
                dims_count=len(all_dims),
                sim_days=sim_days,
                dates=dates,
                max_variants=sym_max_variants,
                sample_size=sym_sample_size,
                downsample_mode=downsample_mode,
                results_path=results_file,
                scratch_root=scratch_root,
            )
            snapshot_rows = _record_top_variants(
                warehouse_conn,
                run_id,
                symbol,
                top_rows if has_results else [],
                sym_dir,
                results_file,
            )
            if snapshot_rows:
                best_row = snapshot_rows[0]
                best_snapshot[symbol] = {
                    "run_id": run_id,
                    "variant": best_row.get("variant"),
                    "metrics": best_row.get("metrics"),
                    "scratch_dir": best_row.get("scratch_dir"),
                    "pk_path": best_row.get("pk_path"),
                    "results_file": best_row.get("results_file"),
                    "base_config": base_config,
                }

        summary_path = results_dir / "summary.txt"
        summary_path.write_text("\n".join(summary_lines) + "\n")
        print(f"Summary written to {summary_path}")
    finally:
        if warehouse_conn:
            warehouse_conn.commit()
            warehouse_conn.close()

    aggregate_path = results_dir / "aggregate_results.csv"
    _write_aggregate_results(best_snapshot, aggregate_path)
    print(f"Aggregate results written to {aggregate_path}")
    best_snapshot_path = results_dir / BEST_VARIANTS_FILENAME
    best_snapshot_path.write_text(json.dumps(best_snapshot, indent=2, sort_keys=True) + "\n")
    print(f"Best variants snapshot written to {best_snapshot_path}")
    print(f"Result warehouse updated: {warehouse_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alpha relwide SimVariations helper")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-spec")
    gen.add_argument("--symbol", help="Single symbol to seed (optional if using --symbols*)")
    gen.add_argument("--symbols", help="Comma-separated list of symbols", default=None)
    gen.add_argument("--symbols-file", help="File containing one symbol per line", default=None)
    gen.add_argument("--base-config",
                     help="pk config path; optional when default wiring exists")
    gen.add_argument("--presets", default="core",
                     help="Comma-separated preset names (core,momentum,spacing,cross,vol_widen,vol_norm)")
    gen.add_argument("--session", default=None,
                     help="Session time window (usday,preus,postus,overnight,allday). "
                          "Auto-detected for equities (defaults to usday).")
    gen.add_argument("--sim-days", type=int, default=14)
    gen.add_argument("--max-variants", type=int, default=800)
    gen.add_argument("--sample-size", type=int, default=400)
    gen.add_argument("--downsample-mode", choices=["even", "random"], default="random")
    gen.add_argument("--neighborhood-pct", type=float, default=0.3)
    gen.add_argument("--auto-ordex-type", default="AlphaRelWideMM",
                     help="Ordex type for auto-built configs",
                     choices=sorted(SUPPORTED_ORDEX_TYPES))
    gen.add_argument("--symbol-ordex", action="append", default=[],
                     help="Override ordex type per symbol (SYMBOL=TYPE)")
    gen.add_argument("--model-bank-root", default=None,
                     help="Optional override for relwide model bank root")
    gen.add_argument("--model-bank-catalog", default=None,
                     help="Optional override for model bank catalog path")
    gen.add_argument("--bootstrap-models", action="store_true",
                     help="Auto-run regression if model bundle missing")
    gen.add_argument("--bootstrap-ins-days", type=int, default=30,
                     help="Number of in-sample days for regression window")
    gen.add_argument("--bootstrap-oos-days", type=int, default=10,
                     help="Number of OOS days for regression window")
    gen.add_argument("--bootstrap-workdir", default=str(Path.home() / "scratch" / "relwide_autobuild"),
                     help="Where to stage regression jobs")
    gen.add_argument("--bootstrap-template", default=None,
                     help="Override TradeArmory template for regression")
    gen.add_argument("--bootstrap-quality", default="provisional",
                     choices=["provisional", "validated"],
                     help="Quality tag for auto-built bundles")
    gen.add_argument("--output-dir", default=None)
    gen.add_argument("--saved-strats-root", default=str(DEFAULT_SAVED_STRATS_ROOT),
                     help="Where to discover saved_strats RelCross seeds")
    gen.add_argument("--no-preferences", dest="use_preferences",
                     action="store_false", default=True,
                     help="Skip per-symbol learned preference dim overrides")
    gen.add_argument("--end-date", default=None,
                     help="Pin 'today' to YYYY-MM-DD; use when recent histdata is gappy "
                          "and you want sim_days to land on a backshifted window.")
    gen.set_defaults(func=generate_spec)

    cov = sub.add_parser("from-coverage",
                         help="Generate spec from coverage report gaps")
    cov.add_argument("--max-count", type=int, default=1,
                     help="Include symbols with session count <= this (default: 1)")
    cov.add_argument("--symbols", default=None,
                     help="Comma-separated list to filter to (only include these from gaps)")
    cov.add_argument("--symbol", default=None)
    cov.add_argument("--symbols-file", default=None)
    cov.add_argument("--exclude", default=None,
                     help="Comma-separated symbols to exclude from gaps")
    cov.add_argument("--presets", default="core,momentum",
                     help="Comma-separated preset names (default: core,momentum)")
    cov.add_argument("--session", default=None,
                     help="Session time window override")
    cov.add_argument("--sim-days", type=int, default=10)
    cov.add_argument("--max-variants", type=int, default=200)
    cov.add_argument("--sample-size", type=int, default=200)
    cov.add_argument("--downsample-mode", choices=["even", "random"], default="random")
    cov.add_argument("--neighborhood-pct", type=float, default=0.3)
    cov.add_argument("--auto-ordex-type", default="AlphaRelWideMM",
                     choices=sorted(SUPPORTED_ORDEX_TYPES))
    cov.add_argument("--symbol-ordex", action="append", default=[])
    cov.add_argument("--base-config", default=None)
    cov.add_argument("--model-bank-root", default=None)
    cov.add_argument("--model-bank-catalog", default=None)
    cov.add_argument("--bootstrap-models", action="store_true",
                     help="Auto-run regression if model bundle missing")
    cov.add_argument("--bootstrap-ins-days", type=int, default=30)
    cov.add_argument("--bootstrap-oos-days", type=int, default=10)
    cov.add_argument("--bootstrap-workdir", default=str(Path.home() / "scratch" / "relwide_autobuild"))
    cov.add_argument("--bootstrap-template", default=None)
    cov.add_argument("--bootstrap-quality", default="provisional",
                     choices=["provisional", "validated"])
    cov.add_argument("--output-dir", default=None)
    cov.add_argument("--saved-strats-root", default=str(DEFAULT_SAVED_STRATS_ROOT))
    cov.add_argument("--no-preferences", dest="use_preferences",
                     action="store_false", default=True,
                     help="Skip per-symbol learned preference dim overrides")
    cov.set_defaults(func=from_coverage)

    onboard = sub.add_parser("onboard-symbol")
    onboard.add_argument("--symbol", required=True, help="Symbol to onboard")
    onboard.add_argument("--spec-path", default=str(DEFAULT_SPEC_PATH),
                         help="Existing spec path to update")
    onboard.add_argument("--ordex-type", default="AlphaRelWideMM",
                         choices=sorted(SUPPORTED_ORDEX_TYPES))
    onboard.add_argument("--presets", default="core",
                         help="Comma-separated preset names (core,momentum,spacing,cross,vol_widen,vol_norm)")
    onboard.add_argument("--session", default=None,
                         help="Session time window (usday,preus,postus,overnight,allday). "
                              "Auto-detected for equities (defaults to usday).")
    onboard.add_argument("--sim-days", type=int, default=None,
                         help="Override sim_days for the spec")
    onboard.add_argument("--max-variants", type=int, default=None,
                         help="Override max_variants for the spec")
    onboard.add_argument("--sample-size", type=int, default=None,
                         help="Override sample_size for the spec")
    onboard.add_argument("--downsample-mode", choices=["even", "random"], default=None)
    onboard.add_argument("--neighborhood-pct", type=float, default=None)
    onboard.add_argument("--base-config",
                         help="pk config path; optional when default wiring exists")
    onboard.add_argument("--model-bank-root", default=None,
                         help="Optional override for relwide model bank root")
    onboard.add_argument("--model-bank-catalog", default=None,
                         help="Optional override for model bank catalog path")
    onboard.add_argument("--bootstrap-models", action="store_true",
                         help="Auto-run regression if model bundle missing")
    onboard.add_argument("--bootstrap-ins-days", type=int, default=30,
                         help="Number of in-sample days for regression window")
    onboard.add_argument("--bootstrap-oos-days", type=int, default=10,
                         help="Number of OOS days for regression window")
    onboard.add_argument("--bootstrap-workdir", default=str(Path.home() / "scratch" / "relwide_autobuild"),
                         help="Where to stage regression jobs")
    onboard.add_argument("--bootstrap-template", default=None,
                         help="Override TradeArmory template for regression")
    onboard.add_argument("--bootstrap-quality", default="provisional",
                         choices=["provisional", "validated"],
                         help="Quality tag for auto-built bundles")
    onboard.add_argument("--force", action="store_true",
                         help="Replace existing symbol entry if it already exists")
    onboard.add_argument("--saved-strats-root", default=str(DEFAULT_SAVED_STRATS_ROOT),
                         help="Where to discover saved_strats RelCross seeds")
    onboard.add_argument("--no-preferences", dest="use_preferences",
                         action="store_false", default=True,
                         help="Skip per-symbol learned preference dim overrides")
    onboard.set_defaults(func=onboard_symbol)

    run = sub.add_parser("run-spec")
    run.add_argument("--run-spec", required=True)
    run.add_argument("--sim-days", type=int, default=14)
    run.add_argument("--max-variants", type=int, default=800)
    run.add_argument("--sample-size", type=int, default=400)
    run.add_argument("--downsample-mode", choices=["even", "random"], default="random")
    run.add_argument("--neighborhood-pct", type=float, default=0.3)
    run.add_argument("--sample-seed", type=int, default=0)
    run.add_argument("--top-k", type=int, default=5,
                     help="Number of top variants to highlight in the summary")
    run.add_argument("--end-date", default=None,
                     help="Pin 'today' to YYYY-MM-DD; use when recent histdata is gappy "
                          "and you want sim_days to land on a backshifted window.")
    # Swarmhost dispatch — opt in. Default off keeps existing local-loop behavior.
    run.add_argument("--remote", action="store_true",
                     help="Dispatch sims to the swarmhost pool instead of running locally.")
    run.add_argument("--hosts", default=None,
                     help="Path to swarmhost hosts.yaml (default: overmind/swarmhost/hosts.yaml).")
    run.add_argument("--chunk-size", type=int, default=50,
                     help="Variants per dispatch chunk when --remote. Default 50.")
    run.set_defaults(func=run_spec)

    scout = sub.add_parser("scout-thresh",
                           help="Run a place_thresh-only scout sweep; reports per-value stats "
                                "and recommends a productive band for the main sweep.")
    scout.add_argument("--symbol", help="Single symbol to scout")
    scout.add_argument("--symbols", help="Comma-separated list of symbols", default=None)
    scout.add_argument("--symbols-file", help="File containing one symbol per line", default=None)
    scout.add_argument("--values", default=None,
                       help="Comma-separated place_thresh values to scout. "
                            "Default: 12 log-spaced values from 1bp to 200bp (0.0001-0.02).")
    scout.add_argument("--ordex-type", default="AlphaRelWideMM",
                       choices=sorted(SUPPORTED_ORDEX_TYPES))
    scout.add_argument("--session", default=None,
                       help="Session window (usday,preus,postus,overnight,allday).")
    scout.add_argument("--sim-days", type=int, default=14)
    scout.add_argument("--base-config", default=None,
                       help="Optional pk config path; if not given, bootstraps a default.")
    scout.add_argument("--model-bank-root", default=None)
    scout.add_argument("--model-bank-catalog", default=None)
    scout.add_argument("--bootstrap-models", action="store_true",
                       help="Auto-run regression if model bundle missing")
    scout.add_argument("--bootstrap-ins-days", type=int, default=30)
    scout.add_argument("--bootstrap-oos-days", type=int, default=10)
    scout.add_argument("--bootstrap-workdir",
                       default=str(Path.home() / "scratch" / "relwide_autobuild"))
    scout.add_argument("--bootstrap-template", default=None)
    scout.add_argument("--bootstrap-quality", default="provisional",
                       choices=["provisional", "validated"])
    scout.add_argument("--output-dir", default=None)
    scout.add_argument("--saved-strats-root", default=str(DEFAULT_SAVED_STRATS_ROOT))
    scout.add_argument("--symbol-ordex", action="append", default=[])
    scout.add_argument("--end-date", default=None,
                       help="Pin 'today' to YYYY-MM-DD; used for sim window + bootstrap.")
    scout.set_defaults(func=scout_thresh, use_preferences=False)

    check = sub.add_parser("check-bundles")
    check.add_argument("--symbol", help="Single symbol to inspect", default=None)
    check.add_argument("--symbols", help="Comma-separated list of symbols", default=None)
    check.add_argument("--symbols-file", help="File containing one symbol per line", default=None)
    check.add_argument("--model-bank-root", default=None,
                       help="Optional override for relwide model bank root")
    check.add_argument("--model-bank-catalog", default=None,
                       help="Optional override for model bank catalog path")
    check.add_argument("--stale-days", type=int, default=30,
                       help="Consider bundles older than this as stale")
    check.add_argument("--bootstrap-models", action="store_true",
                       help="Auto-run regression if bundle missing or stale")
    check.add_argument("--bootstrap-ins-days", type=int, default=30,
                       help="Number of in-sample days for regression window")
    check.add_argument("--bootstrap-oos-days", type=int, default=10,
                       help="Number of OOS days for regression window")
    check.add_argument("--bootstrap-workdir", default=str(Path.home() / "scratch" / "relwide_autobuild"),
                       help="Where to stage regression jobs")
    check.add_argument("--bootstrap-template", default=None,
                       help="Override TradeArmory template for regression")
    check.add_argument("--bootstrap-quality", default="provisional",
                       choices=["provisional", "validated"],
                       help="Quality tag for auto-built bundles")
    check.set_defaults(func=check_bundles)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    end_date_arg = getattr(args, "end_date", None)
    if end_date_arg:
        global _TODAY_OVERRIDE
        _TODAY_OVERRIDE = datetime.strptime(end_date_arg, "%Y-%m-%d").date()
        print(f"  --end-date set: pinning today() to {_TODAY_OVERRIDE}")
    result = args.func(args)
    return result


DEFAULT_MODEL_BANK_ROOTS = [
    Path.home() / "scratch" / "relwide_modelbank",
    REPO_ROOT / "overmind" / "model_inheritance",
]
DEFAULT_MODEL_BANK_CATALOG = REPO_ROOT / "overmind" / "model_inheritance" / "catalog.json"
DEFAULT_REGRESSION_TEMPLATE = REPO_ROOT / "overmind" / "modelbuild_confs" / "xyz_relwide_armory_v1.json"

if __name__ == "__main__":
    main()
