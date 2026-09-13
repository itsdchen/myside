#!/usr/bin/env python3
"""relcross_autosearch — phased RelCross autosearch across the HIP3 universe.

Wraps coverage_autosearch's `run_spec` primitive with RelCross-tuned dim
defaults, per-symbol session mapping, in-sweep OOS reservation, and a
concentration-based narrowing loop. See:
  overmind/studies/relcross_autosearch_playbook.md

Subcommands:
  init-spec    : clone donor to targets, write initial spec.py
  anchor-scan  : Phase 0 coarse multi-dim scan of the four primary anchors
  run-spec     : Phase 1+ full sweep (delegates to coverage_autosearch.run_spec)
  narrow       : read results.txt + oos_results.txt, apply concentration narrowing
  show-picks   : read oos_results.txt, rank by oos_median_pnl, present top-N
  revalidate   : re-sim top-N on a forward window (stub — see TODO)
  promote      : copy winner pks to promoted/pk_<sym>_v<vid>.json

OOS discipline: every sim window reserves the last `oos_days_tail` (default 5)
trading dates for held-out scoring. Ranking uses oos_median_pnl with quality-
bar signals shown per variant. NO auto-selection — the human picks.
"""

import argparse
import csv
import importlib.util
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
for _p in (_SCRIPT_DIR, _STRAT_MAIN):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import commentjson  # noqa: E402


# ── Session windows ──────────────────────────────────────────────────────────
# CLI-name -> (spec-display-name, (start_t, end_t)). These mirror the machine
# windows in golive/build_per_machine.py. Copy verbatim from playbook §5.
SESSION_WINDOWS = {
    "usday":  ("US Day",  ("09:35:00 America/New_York", "17:00:00 America/New_York")),
    "24h":    ("24h",     ("18:00:00 America/New_York", "17:00:00 America/New_York")),
    # KRX has a 12:00-13:00 KST lunch break; sweep AM and PM as separate
    # sessions so the sim window matches actual trading.
    "krxam":  ("KRX AM",  ("20:00:00 America/New_York", "23:00:00 America/New_York")),
    "krxpm":  ("KRX PM",  ("00:00:00 America/New_York", "02:30:00 America/New_York")),
    # JPX (Tokyo Stock Exchange) has a 11:30-12:30 JST lunch break.
    # Regular session ends 15:30 JST = 02:30 NY. jpxpm end updated from
    # 02:00 to 02:30 to match TSE regular close (saved_strats confirmed).
    "jpxam":  ("JPX AM",  ("20:00:00 America/New_York", "22:30:00 America/New_York")),
    "jpxpm":  ("JPX PM",  ("23:30:00 America/New_York", "02:30:00 America/New_York")),
    # jpxday: single continuous session (JP225 index — no AM/PM split).
    "jpxday": ("JPX Day", ("20:00:00 America/New_York", "02:30:00 America/New_York")),
    # HKG (Hong Kong Stock Exchange) has a 12:00-13:00 HKT lunch break.
    # Used for Chinese firms listed on HKG (e.g., ZHIPU, MINIMAX).
    "hkgam":  ("HKG AM",  ("21:30:00 America/New_York", "00:00:00 America/New_York")),
    "hkgpm":  ("HKG PM",  ("01:00:00 America/New_York", "04:00:00 America/New_York")),
}

# Reverse map: display-name -> CLI-name (for spec parsing).
_DISPLAY_TO_CLI = {display: cli for cli, (display, _) in SESSION_WINDOWS.items()}


# ── RelCross-tuned defaults (playbook §5) ────────────────────────────────────
RELCROSS_DEFAULT_DIMS = [
    (["pktraders", "ordex", "base_cross_thresh"],  [0.0001, 0.0002, 0.0005, 0.001, 0.002]),
    (["pktraders", "ordex", "premium_tdc_s"],      [15, 30, 240, 300]),
    (["pktraders", "ordex", "vol_norm_tdc"],       [15, 60, 120, 240]),
    (["pktraders", "ordex", "vol_norm_coef"],      [1.0, 2.0, 3.0, 5.0, 8.0]),
    (["pktraders", "ordex", "exit_adjust"],        [0.3, 0.5, 0.7]),
    (["pktraders", "ordex", "premium_ema_coef"],   [0.6, 0.8, 1.0]),
    (["pktraders", "ordex", "remote_mom_coef"],    [0, 0.15, 0.3]),
    (["pktraders", "ordex", "feed_lag_widen_coef"], [0, 1.5]),
    (["pktraders", "ordex", "precog_miss_max_adjust"], [0.85, 1.0]),
]

RELCROSS_DEFAULT_FIXED = [
    (["pktraders", "ordex", "flip_through"], False),
    (["simulation", "Hyperliquid", "lag_aware_latency"], True),
    # IOC/ALO/CXL lag defaults intentionally NOT pinned here — the sim source
    # (src/pktrade/sim/sim_lvl_inst.h) is the calibration authority. Pinning
    # here would mask future SVL recalibrations (e.g., ce9d18d4 on 2026-07-21
    # updated IOC 900→750, coef 2.0→1.5 after the fast-cancel fix).
]

# ── Phase 0 anchor scan grid (playbook §2 Phase 0) ───────────────────────────
ANCHOR_SCAN_DIMS = [
    (["pktraders", "ordex", "base_cross_thresh"],
     [0.00005, 0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01]),
    (["pktraders", "ordex", "premium_tdc_s"],  [15, 60, 240, 1200]),
    (["pktraders", "ordex", "vol_norm_coef"],  [1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 16.0]),
    (["pktraders", "ordex", "vol_norm_tdc"],   [30, 60, 120, 240]),
]

# ── Config knobs ─────────────────────────────────────────────────────────────
DEFAULT_SIM_DAYS = 25
DEFAULT_OOS_DAYS_TAIL = 5
DEFAULT_MIN_TRAIN_DAYS = 10
DEFAULT_MAX_VARIANTS = 500
DEFAULT_NEIGHBORHOOD_PCT = 0.3

# Quality-bar signals (playbook §4). These are informational, not filters.
QUALITY_PCT_POS_MIN = 0.60
QUALITY_NUM_TRDS_MIN = 20
QUALITY_NUM_TRDS_SPARSE = 10
QUALITY_FILLRATE_MIN = 0.005
QUALITY_IS_OOS_RATIO_MAX = 3.0


# ── Session override / patch ─────────────────────────────────────────────────

def _apply_session_override(session_cli_name):
    """Register a session window in coverage_autosearch.SESSION_TIMES.

    Returns the spec-display-name (e.g. "KRX AM").
    """
    import coverage_autosearch
    if session_cli_name not in SESSION_WINDOWS:
        raise ValueError(
            f"Unknown session '{session_cli_name}'. "
            f"Choose from: {sorted(SESSION_WINDOWS.keys())}")
    display_name, window = SESSION_WINDOWS[session_cli_name]
    coverage_autosearch.SESSION_TIMES[display_name] = window
    return display_name


def _apply_all_sessions():
    """Register every SESSION_WINDOWS entry in coverage_autosearch.SESSION_TIMES.

    Needed for run-spec / anchor-scan / narrow, since a single spec can carry
    many syms with different sessions.
    """
    import coverage_autosearch
    for _cli, (display_name, window) in SESSION_WINDOWS.items():
        coverage_autosearch.SESSION_TIMES[display_name] = window


# ── Session validation guardrail (playbook §5, §8 warts #8) ──────────────────

def _validate_sessions(symbols_dct, spec_path_for_msg=None):
    """Every symbol entry must have a `session` in SESSION_WINDOWS.

    Fails LOUD on missing session. This is the guardrail against the 20260629
    Asian-sym silent-fail (US-day window quietly applied to KRX syms).
    """
    valid_displays = set(_DISPLAY_TO_CLI.keys())
    errors = []
    for key, entry in symbols_dct.items():
        # Session may come from "sym|Session" key or from the entry dict.
        parts = key.split("|")
        sess_from_key = parts[1] if len(parts) >= 2 else None
        sess_from_entry = entry.get("session") if isinstance(entry, dict) else None
        session = sess_from_key or sess_from_entry
        if not session:
            errors.append(
                f"ERROR: {key} has no `session` key. Add "
                f"`\"session\": \"KRX AM\"` or `\"session\": \"KRX PM\"` — halting."
            )
            continue
        if session not in valid_displays:
            errors.append(
                f"ERROR: {key} has session={session!r} not in SESSION_WINDOWS "
                f"(valid: {sorted(valid_displays)}) — halting."
            )
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        if spec_path_for_msg:
            print(f"  (spec: {spec_path_for_msg})", file=sys.stderr)
        sys.exit(1)


# ── Spec loader ──────────────────────────────────────────────────────────────

def _load_spec_module(spec_path):
    """Import a Python spec file and return the module object."""
    spec = importlib.util.spec_from_file_location("relcross_spec", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Symbol utilities ─────────────────────────────────────────────────────────

def _normalize_symbol(sym):
    """Accept 'TSLA' or 'xyz:TSLA', return canonical 'xyz:TSLA'."""
    if ":" in sym:
        return sym
    return f"xyz:{sym}"


def _session_slug(session_display):
    """'KRX AM' -> 'krxam'; falls back to lowercased no-space form."""
    return _DISPLAY_TO_CLI.get(
        session_display,
        session_display.lower().replace(" ", "").replace("-", "")
    )


def _sym_session_dir_name(sym, session_display):
    """Directory naming: 'xyz:LLY' + 'US Day' -> 'xyz_LLY_usday'.

    Includes session so KRX AM and KRX PM don't collide (playbook §6, §10).
    """
    base = sym.replace(":", "_")
    return f"{base}_{_session_slug(session_display)}"


# ── Sim window construction (playbook §3, requirement #8) ────────────────────

def _resolve_sim_dates_for_sym(sym, market, sim_days, oos_days_tail,
                                min_train_days, sweep_end_str):
    """Return (sim_dates_list, error_msg_or_None). Never raises for sym-level.

    - Total window = sim_days ending at sweep_end_str (via WEEKDAYS calendar).
    - If sym has fewer available dates (listing gap), shorten to what's there.
    - Require ≥ (oos_days_tail + min_train_days) trading days; else error.
    """
    from util.chron import dates_avail, dates_list

    end_dt = date(int(sweep_end_str[:4]), int(sweep_end_str[4:6]),
                  int(sweep_end_str[6:]))
    start_dt = end_dt - timedelta(days=sim_days + 15)  # pad for wknd/holidays
    start_str = start_dt.strftime("%Y%m%d")
    end_str = end_dt.strftime("%Y%m%d")

    try:
        avail = dates_avail(start_str, end_str, sym, market,
                            dates_method="WEEKDAYS")
        sim_dates = avail.get("good_dates", [])
    except Exception:
        sim_dates = dates_list(start_str, end_str, dates_method="WEEKDAYS")

    if len(sim_dates) > sim_days:
        sim_dates = sim_dates[-sim_days:]

    min_needed = oos_days_tail + min_train_days
    if len(sim_dates) < min_needed:
        return (sim_dates,
                f"only {len(sim_dates)} trading days available "
                f"(need ≥ {min_needed} = oos {oos_days_tail} + train "
                f"{min_train_days}); skipping.")
    return sim_dates, None


# ── md_exists pre-flight (playbook §8 warts #2) ──────────────────────────────

def _md_exists_check(sym, market, sim_dates):
    """Verify md_exists for every date in sim_dates. Fail loud if missing.

    Uses overmind/strat_main/tools/md_exists.py (per requirement #10;
    playbook references overmind/scripts/md_exists.py but the actual
    tool lives at strat_main/tools/md_exists.py — same script).
    """
    if not sim_dates:
        return True
    start_str = sim_dates[0]
    end_str = sim_dates[-1]
    md_script = os.path.join(_STRAT_MAIN, "tools", "md_exists.py")
    try:
        proc = subprocess.run(
            [sys.executable, md_script,
             "--sym", sym, "--market", market,
             "--start", start_str, "--end", end_str],
            input="y\n", text=True, timeout=600, capture_output=True,
        )
        # md_exists prints missing dates to stdout; if the return code is
        # non-zero, or "missing" appears in output, warn and let the caller decide.
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            print(f"  WARN: md_exists returned {proc.returncode} for "
                  f"{sym}/{market} {start_str}-{end_str}")
            return False
        if "missing" in out.lower() or "not found" in out.lower():
            print(f"  WARN: md_exists reported gaps for {sym}/{market}: "
                  f"{out.strip().splitlines()[-3:]}")
        return True
    except Exception as e:
        print(f"  WARN: md_exists failed for {sym}/{market}: {e}")
        return False


# ── init-spec ────────────────────────────────────────────────────────────────

def _find_donor_config_path(donor_symbol):
    """Locate the freshest live config that has a RelCross ordex for donor.

    Reuses coverage_report.load_latest_configs + _build_config_index like
    the RelWide tool does. Falls back to erroring.
    """
    from coverage_autosearch import LOCAL_BASE, _build_config_index
    from coverage_report import load_latest_configs

    configs = load_latest_configs(LOCAL_BASE, max_stale_days=None)
    idx = _build_config_index(configs, LOCAL_BASE)
    entries = idx.get(donor_symbol)
    if not entries:
        sys.exit(
            f"No live config found for donor {donor_symbol}. "
            f"Pass --donor-path to specify one explicitly.")
    for entry in sorted(entries, key=lambda e: e["date"], reverse=True):
        path = entry["path"]
        try:
            with open(path) as f:
                cfg = commentjson.load(f)
        except Exception:
            continue
        for t in cfg.get("pktraders", []):
            if t.get("traded_symbol") == donor_symbol and \
               t.get("ordex", {}).get("type") == "RelCross":
                return path
    sys.exit(
        f"Donor {donor_symbol} has live configs but none use RelCross. "
        f"Pass --donor-path to specify one explicitly.")


def cmd_init_spec(args):
    """Write an initial spec.py that clones a donor to the target symbols."""
    workdir = Path(os.path.expanduser(args.workdir))
    workdir.mkdir(parents=True, exist_ok=True)

    donor = args.donor
    if args.donor_path:
        donor_path = os.path.expanduser(args.donor_path)
        if not os.path.exists(donor_path):
            sys.exit(f"Donor path does not exist: {donor_path}")
    else:
        donor_path = _find_donor_config_path(donor)
    print(f"Donor: {donor}  ->  {donor_path}")

    if not args.symbols:
        sys.exit("Pass --symbols SYM1,SYM2,...")
    symbols = [_normalize_symbol(s.strip())
               for s in args.symbols.split(",") if s.strip()]

    session_cli = args.session
    if session_cli not in SESSION_WINDOWS:
        sys.exit(f"Unknown --session {session_cli!r}. "
                 f"Choose from: {sorted(SESSION_WINDOWS.keys())}")
    session_display, session_window = SESSION_WINDOWS[session_cli]
    print(f"Session: {session_cli} -> {session_display} {session_window}")
    print(f"Symbols: {symbols}")

    if args.dry_run:
        print("[dry-run] would write spec.py — halting.")
        return

    lines = []
    lines.append(f"# RelCross Autosearch Spec — {date.today():%Y-%m-%d}")
    lines.append(f"# Donor (cloned): {donor}")
    lines.append(f"# Donor path: {donor_path}")
    lines.append("# Edit dims / add per-sym overrides, then run anchor-scan or run-spec.")
    lines.append("")
    lines.append(f"sim_days = {args.sim_days}")
    lines.append(f"oos_days_tail = {args.oos_days_tail}")
    lines.append(f"min_train_days = {DEFAULT_MIN_TRAIN_DAYS}")
    lines.append(f"max_variants = {args.max_variants}")
    lines.append("random_sample = True")
    lines.append(f"neighborhood_pct = {DEFAULT_NEIGHBORHOOD_PCT}")
    if args.sweep_end:
        lines.append(f"sweep_end = {args.sweep_end!r}   # YYYYMMDD; sim window ends here")
    else:
        lines.append("# sweep_end = 'YYYYMMDD'   # default: today")
    lines.append('extra_args = ""')
    lines.append("")
    lines.append("default_dims = [")
    for path, vals in RELCROSS_DEFAULT_DIMS:
        lines.append(f"    ({path}, {vals}),")
    lines.append("]")
    lines.append("")
    lines.append("default_fixed = [")
    for path, val in RELCROSS_DEFAULT_FIXED:
        lines.append(f"    ({path}, {val!r}),")
    lines.append("]")
    lines.append("")
    lines.append("default_range_dims = []")
    lines.append("")
    lines.append("symbols = {")
    for sym in symbols:
        key = f"{sym}|{session_display}"
        lines.append(f'    "{key}": {{')
        lines.append(f'        "base_config": "{donor_path}",')
        lines.append(f'        "session": "{session_display}",')
        lines.append(f'        "clone_from": "{donor}",')
        lines.append("    },")
    lines.append("}")
    lines.append("")

    spec_path = workdir / "spec.py"
    spec_path.write_text("\n".join(lines))
    print(f"Wrote spec: {spec_path}")
    print(f"\nNext: anchor-scan --spec {spec_path}")


# ── OOS scoring helpers ──────────────────────────────────────────────────────

def _per_variant_per_day_pnl(scratch_dir, variant_id):
    """Read scratch/<vid>/acct_<vid>_<date>.csv files, return {date: net_pnl_last_row}.

    Returns {} if the variant directory or acct files are missing.
    """
    vdir = Path(scratch_dir) / str(variant_id)
    if not vdir.is_dir():
        return {}
    out = {}
    for f in sorted(vdir.iterdir()):
        # Match acct_<vid>_<YYYYMMDD>.csv
        name = f.name
        if not name.startswith("acct_"):
            continue
        if not name.endswith(".csv"):
            continue
        stem = name[:-len(".csv")]
        parts = stem.split("_")
        if len(parts) < 3:
            continue
        dstr = parts[-1]
        if not (len(dstr) == 8 and dstr.isdigit()):
            continue
        try:
            with f.open() as fh:
                reader = csv.DictReader(fh)
                last_row = None
                for row in reader:
                    last_row = row
                if last_row is None:
                    continue
                net_pnl_raw = last_row.get("net_pnl")
                if net_pnl_raw is None or net_pnl_raw == "":
                    continue
                out[dstr] = float(net_pnl_raw)
        except Exception:
            continue
    return out


def _per_variant_ntrds_by_day(scratch_dir, variant_id):
    """Best-effort per-day num_trds from acct files (last row's num_trds col).

    Falls back to counting rows in trades_<vid>_<date>.csv (schema per memory
    reference_trades_csv.md: 22 cols, no header). Returns {} if nothing found.
    """
    vdir = Path(scratch_dir) / str(variant_id)
    if not vdir.is_dir():
        return {}
    out = {}
    for f in sorted(vdir.iterdir()):
        name = f.name
        if not name.startswith("acct_") or not name.endswith(".csv"):
            continue
        stem = name[:-len(".csv")]
        parts = stem.split("_")
        if len(parts) < 3:
            continue
        dstr = parts[-1]
        if not (len(dstr) == 8 and dstr.isdigit()):
            continue
        try:
            with f.open() as fh:
                reader = csv.DictReader(fh)
                last_row = None
                for row in reader:
                    last_row = row
                if last_row is None:
                    continue
                nt_raw = (last_row.get("times_traded")
                          or last_row.get("num_trds")
                          or last_row.get("num_trades"))
                if nt_raw is None or nt_raw == "":
                    continue
                out[dstr] = float(nt_raw)
        except Exception:
            continue
    # Fallback: count trades files.
    if not out:
        for f in sorted(vdir.iterdir()):
            name = f.name
            if not name.startswith("trades_") or not name.endswith(".csv"):
                continue
            stem = name[:-len(".csv")]
            parts = stem.split("_")
            if len(parts) < 3:
                continue
            dstr = parts[-1]
            if not (len(dstr) == 8 and dstr.isdigit()):
                continue
            try:
                with f.open() as fh:
                    count = sum(1 for _ in fh)
                out[dstr] = float(count)
            except Exception:
                continue
    return out


def _compute_oos_stats(results_txt_path, sim_dates, oos_days_tail):
    """Read results.txt, compute OOS-tail stats per variant. Return rows list.

    Rows contain:
      variant, <dim cols>, oos_avg_pnl, oos_median_pnl, oos_sharpe,
      oos_pct_positive, oos_num_trds, oos_fillrate,
      is_oos_ntrd_ratio, n_oos_dates_ok, sim_score
    """
    results_txt_path = Path(results_txt_path)
    if not results_txt_path.exists():
        return []
    scratch_dir = results_txt_path.parent / "scratch"

    with results_txt_path.open() as f:
        rows_in = list(csv.DictReader(f))

    if not sim_dates:
        return []
    oos_dates = set(sim_dates[-oos_days_tail:])
    is_dates = set(sim_dates[:-oos_days_tail]) if len(sim_dates) > oos_days_tail else set()

    # Identify dim columns dynamically — everything before stats block.
    # Preserve original column order for output.
    known_stats = {
        "avg_pnl", "median_pnl", "sharpe", "pct_positive", "num_trds",
        "fillrate", "sim_score", "variant",
    }
    known_ident = {"variant", "variant_id"}

    # Any non-stats/non-ident column is a dim column.
    dim_cols = []
    if rows_in:
        for col in rows_in[0].keys():
            if col in known_stats or col in known_ident:
                continue
            dim_cols.append(col)

    out_rows = []
    for r in rows_in:
        vid = r.get("variant") or r.get("variant_id")
        if vid is None:
            continue
        per_day = _per_variant_per_day_pnl(scratch_dir, vid)
        per_day_nt = _per_variant_ntrds_by_day(scratch_dir, vid)

        oos_pnls = [p for d, p in per_day.items() if d in oos_dates]
        is_pnls = [p for d, p in per_day.items() if d in is_dates]
        oos_nt = [n for d, n in per_day_nt.items() if d in oos_dates]
        is_nt = [n for d, n in per_day_nt.items() if d in is_dates]

        n_oos_ok = len(oos_pnls)
        if n_oos_ok == 0:
            # No acct data for OOS — skip this variant from OOS output.
            continue

        oos_avg = statistics.mean(oos_pnls)
        oos_med = statistics.median(oos_pnls)
        if n_oos_ok > 1 and statistics.pstdev(oos_pnls) > 0:
            oos_sharpe = oos_avg / statistics.pstdev(oos_pnls)
        else:
            oos_sharpe = 0.0
        oos_pos = sum(1 for p in oos_pnls if p > 0) / n_oos_ok

        # num_trds/fillrate: use per-day sum if available, else best-effort
        # from results.txt (which is IS+OOS aggregated).
        oos_num_trds = sum(oos_nt) if oos_nt else 0
        # fillrate: approximate as OOS num_trds / OOS session-length proxy;
        # we don't have per-day place counts, so fall back to results.txt col.
        try:
            oos_fillrate = float(r.get("fillrate", 0) or 0)
        except (ValueError, TypeError):
            oos_fillrate = 0.0

        # IS/OOS trade-rate ratio (playbook §4, overfit tell).
        is_days = max(1, len(is_nt)) if is_nt else max(1, len(is_dates))
        oos_days = max(1, n_oos_ok)
        is_rate = (sum(is_nt) / is_days) if is_nt else 0.0
        oos_rate = (oos_num_trds / oos_days) if oos_num_trds else 0.0
        if oos_rate > 0:
            is_oos_ratio = is_rate / oos_rate
        else:
            is_oos_ratio = float("inf") if is_rate > 0 else 0.0

        try:
            sim_score = float(r.get("sim_score", 0) or 0)
        except (ValueError, TypeError):
            sim_score = 0.0

        # IS-side robustness stats (playbook §4 regime-aware, added 2026-07-06
        # after silver 6/17-6/25 basis-blowup investigation).
        if is_pnls:
            is_avg = statistics.mean(is_pnls)
            is_worst_day = min(is_pnls)
            # Rolling 5-day windows over IS
            if len(is_pnls) >= 5:
                windows = [sum(is_pnls[i:i+5])
                           for i in range(len(is_pnls) - 4)]
                is_worst_5d = min(windows)
            else:
                is_worst_5d = sum(is_pnls)
        else:
            is_avg = 0.0
            is_worst_day = 0.0
            is_worst_5d = 0.0

        out = {"variant": vid}
        for c in dim_cols:
            out[c] = r.get(c, "")
        out.update({
            "oos_avg_pnl": f"{oos_avg:.4f}",
            "oos_median_pnl": f"{oos_med:.4f}",
            "oos_sharpe": f"{oos_sharpe:.4f}",
            "oos_pct_positive": f"{oos_pos:.4f}",
            "oos_num_trds": f"{oos_num_trds:.0f}",
            "oos_fillrate": f"{oos_fillrate:.6f}",
            "is_oos_ntrd_ratio": (f"{is_oos_ratio:.3f}"
                                   if is_oos_ratio != float("inf") else "inf"),
            "n_oos_dates_ok": str(n_oos_ok),
            "sim_score": f"{sim_score:.4f}",
            "is_avg_pnl": f"{is_avg:.4f}",
            "is_worst_day": f"{is_worst_day:.4f}",
            "is_worst_5d": f"{is_worst_5d:.4f}",
        })
        out_rows.append(out)
    return out_rows


def _write_oos_results(oos_rows, out_path):
    """Write oos_results.txt as CSV. Sorted by oos_median_pnl desc."""
    if not oos_rows:
        Path(out_path).write_text("")
        return
    try:
        oos_rows.sort(key=lambda r: float(r["oos_median_pnl"]), reverse=True)
    except Exception:
        pass
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(oos_rows[0].keys()))
        writer.writeheader()
        for r in oos_rows:
            writer.writerow(r)


def _emit_oos_for_workdir(results_root, sim_dates_by_key, oos_days_tail):
    """For every results.txt under <results_root>/<symdir>/<spec>/results.txt,
    compute and write oos_results.txt alongside. `sim_dates_by_key` maps
    the sym-dir name (e.g. 'xyz_LLY_usday') to the sim_dates list used.
    """
    root = Path(results_root)
    if not root.is_dir():
        return
    for sym_dir in sorted(root.iterdir()):
        if not sym_dir.is_dir():
            continue
        # Two layouts: legacy (results.txt directly), current (spec_basename/results.txt).
        direct = sym_dir / "results.txt"
        candidates = []
        if direct.exists():
            candidates.append(sym_dir)
        else:
            for sub in sorted(sym_dir.iterdir()):
                if sub.is_dir() and (sub / "results.txt").exists():
                    candidates.append(sub)
        for cand_dir in candidates:
            key = sym_dir.name
            sim_dates = sim_dates_by_key.get(key, [])
            if not sim_dates:
                # Fall back to inferring dates from scratch/<any>/acct_*.csv
                sim_dates = _infer_dates_from_scratch(cand_dir / "scratch")
            oos_rows = _compute_oos_stats(cand_dir / "results.txt",
                                          sim_dates, oos_days_tail)
            _write_oos_results(oos_rows, cand_dir / "oos_results.txt")
            print(f"  wrote oos_results.txt: {cand_dir}/oos_results.txt "
                  f"({len(oos_rows)} rows)")


def _infer_dates_from_scratch(scratch_dir):
    """Fallback: scan any variant scratch dir for acct_*.csv date suffixes."""
    scratch_dir = Path(scratch_dir)
    if not scratch_dir.is_dir():
        return []
    dates = set()
    for vdir in scratch_dir.iterdir():
        if not vdir.is_dir():
            continue
        for f in vdir.iterdir():
            name = f.name
            if not name.startswith("acct_") or not name.endswith(".csv"):
                continue
            stem = name[:-len(".csv")]
            parts = stem.split("_")
            if len(parts) < 3:
                continue
            dstr = parts[-1]
            if len(dstr) == 8 and dstr.isdigit():
                dates.add(dstr)
    return sorted(dates)


# ── anchor-scan (Phase 0) ────────────────────────────────────────────────────

def cmd_anchor_scan(args):
    """Phase 0: coarse multi-dim scan of the four RelCross primary anchors.

    Sweeps ANCHOR_SCAN_DIMS (8×4×4×3 = 384 variants per sym) using the sim
    window from spec's sim_days/sweep_end. Writes to
      <workdir>/anchor_scan/<symdir>/results.txt
    and                oos_results.txt.
    """
    from stratbuilder.SimVariations import sim_grid

    spec_path = os.path.expanduser(args.spec)
    spec_mod = _load_spec_module(spec_path)
    symbols = getattr(spec_mod, "symbols", {})
    if not symbols:
        sys.exit("No symbols in spec.")

    _validate_sessions(symbols, spec_path)
    _apply_all_sessions()
    from coverage_autosearch import _clone_config_for_symbol, SESSION_TIMES

    sim_days = getattr(spec_mod, "sim_days", DEFAULT_SIM_DAYS)
    oos_days_tail = getattr(spec_mod, "oos_days_tail", DEFAULT_OOS_DAYS_TAIL)
    min_train_days = getattr(spec_mod, "min_train_days", DEFAULT_MIN_TRAIN_DAYS)
    sweep_end = getattr(spec_mod, "sweep_end", None) or \
        date.today().strftime("%Y%m%d")
    default_fixed = getattr(spec_mod, "default_fixed", RELCROSS_DEFAULT_FIXED)
    extra_args = getattr(spec_mod, "extra_args", "")

    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    scan_root = os.path.join(spec_dir, "anchor_scan")
    os.makedirs(scan_root, exist_ok=True)

    sim_dates_by_key = {}

    for entry_key, sym_spec in sorted(symbols.items()):
        parts = entry_key.split("|")
        sym_name = parts[0]
        session = parts[1] if len(parts) >= 2 else sym_spec.get("session")

        print(f"\n{'='*60}\nanchor-scan: {entry_key}\n{'='*60}")

        base_config = sym_spec["base_config"]
        if not os.path.exists(base_config):
            print(f"  skip: base config missing: {base_config}")
            continue

        with open(base_config) as f:
            pk_dct = commentjson.load(f)

        clone_from = sym_spec.get("clone_from")
        if clone_from:
            pk_dct = _clone_config_for_symbol(pk_dct, clone_from, sym_name)

        pk_dct["pktraders"] = [
            t for t in pk_dct["pktraders"]
            if t.get("traded_symbol") == sym_name
        ]
        if not pk_dct["pktraders"]:
            print(f"  skip: symbol not in config after clone")
            continue
        pk_dct["pktraders"][0]["size_mult"] = 1
        pk_dct["pktraders"][0]["enabled"] = True

        if session in SESSION_TIMES:
            start_t, end_t = SESSION_TIMES[session]
            pk_dct.setdefault("settings", {})["start_t"] = start_t
            pk_dct["settings"]["end_t"] = end_t

        market = pk_dct["pktraders"][0]["ordex"]["markets"][0]
        sim_dates, err = _resolve_sim_dates_for_sym(
            sym_name, market, sim_days, oos_days_tail, min_train_days, sweep_end
        )
        if err:
            print(f"  skip {sym_name}: {err}")
            continue

        # md_exists check (playbook §8 warts #2)
        _md_exists_check(sym_name, market, sim_dates)

        sym_dir_name = _sym_session_dir_name(sym_name, session)
        # Unique workdir slug per invocation (playbook §6, requirement #10).
        sym_scan_dir = os.path.join(
            scan_root, sym_dir_name,
            f"sweep_{sym_dir_name}_{args.spec_name or 'anchor'}"
        )
        os.makedirs(sym_scan_dir, exist_ok=True)

        overrides = []
        for path, vals in ANCHOR_SCAN_DIMS:
            overrides.append({"path": list(path), "val": list(vals)})
        for path, val in default_fixed:
            overrides.append({"path": list(path), "val": val})

        print(f"  dates: {sim_dates[0]}..{sim_dates[-1]} ({len(sim_dates)} days; "
              f"OOS tail = {sim_dates[-oos_days_tail:]})")
        print(f"  variants: {math.prod(len(v) for _, v in ANCHOR_SCAN_DIMS)}")

        if args.dry_run:
            print("  [dry-run] would call sim_grid — skipping.")
            continue

        try:
            sim_grid(overrides, pk_dct, sym_scan_dir, sim_dates,
                     fast_sim=False, resume=True,
                     remote=getattr(args, "remote", False),
                     hosts_yaml=getattr(args, "hosts", None),
                     chunk_size=getattr(args, "chunk_size", 50),
                     extra_args=extra_args)
        except Exception as e:
            import traceback
            print(f"  anchor-scan failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

        sim_dates_by_key[sym_dir_name] = sim_dates

        # Emit oos_results.txt for this sweep dir.
        oos_rows = _compute_oos_stats(
            os.path.join(sym_scan_dir, "results.txt"),
            sim_dates, oos_days_tail
        )
        _write_oos_results(oos_rows, os.path.join(sym_scan_dir, "oos_results.txt"))
        print(f"  wrote {sym_scan_dir}/oos_results.txt ({len(oos_rows)} rows)")


# ── run-spec (Phase 1+) ──────────────────────────────────────────────────────

def cmd_run_spec(args):
    """Run a spec via SimVariations. Wraps coverage_autosearch.run_spec but:

      - Validates sessions up front.
      - Registers all SESSION_WINDOWS entries before delegating.
      - Enforces sim window construction with oos_days_tail reservation.
      - After the sweep, walks results/ and writes oos_results.txt per sym.
    """
    spec_path = os.path.expanduser(args.spec)
    spec_mod = _load_spec_module(spec_path)
    symbols = getattr(spec_mod, "symbols", {})
    if not symbols:
        sys.exit("No symbols in spec.")

    _validate_sessions(symbols, spec_path)
    _apply_all_sessions()

    sim_days = getattr(spec_mod, "sim_days", DEFAULT_SIM_DAYS)
    oos_days_tail = getattr(spec_mod, "oos_days_tail", DEFAULT_OOS_DAYS_TAIL)
    min_train_days = getattr(spec_mod, "min_train_days", DEFAULT_MIN_TRAIN_DAYS)
    sweep_end = getattr(spec_mod, "sweep_end", None) or \
        date.today().strftime("%Y%m%d")

    # md_exists pre-flight per sym (playbook §8 warts #2). We do it here
    # because coverage_autosearch.run_spec only checks the local feed.
    from coverage_autosearch import _clone_config_for_symbol
    sim_dates_by_key = {}
    for entry_key, sym_spec in sorted(symbols.items()):
        parts = entry_key.split("|")
        sym_name = parts[0]
        session = parts[1] if len(parts) >= 2 else sym_spec.get("session")
        base_config = sym_spec["base_config"]
        if not os.path.exists(base_config):
            print(f"  skip pre-flight {entry_key}: base config missing")
            continue
        with open(base_config) as f:
            pk_dct = commentjson.load(f)
        clone_from = sym_spec.get("clone_from")
        if clone_from:
            pk_dct = _clone_config_for_symbol(pk_dct, clone_from, sym_name)
        traders = [t for t in pk_dct.get("pktraders", [])
                   if t.get("traded_symbol") == sym_name]
        if not traders:
            print(f"  skip pre-flight {entry_key}: no matching trader")
            continue
        market = traders[0]["ordex"]["markets"][0]
        sim_dates, err = _resolve_sim_dates_for_sym(
            sym_name, market, sim_days, oos_days_tail, min_train_days, sweep_end
        )
        if err:
            print(f"  ERROR pre-flight {sym_name}: {err} — remove from spec or shorten window.")
            continue
        _md_exists_check(sym_name, market, sim_dates)
        sim_dates_by_key[_sym_session_dir_name(sym_name, session)] = sim_dates

    if args.dry_run:
        print("[dry-run] pre-flight complete; skipping sim_grid dispatch.")
        return

    # Delegate the actual sweep.
    from coverage_autosearch import run_spec as _cov_run_spec
    _cov_run_spec(
        spec_path,
        remote=getattr(args, "remote", False),
        hosts_yaml=getattr(args, "hosts", None),
        chunk_size=getattr(args, "chunk_size", 50),
    )

    # After: emit oos_results.txt alongside each results.txt.
    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    results_root = os.path.join(spec_dir, "results")
    print(f"\nEmitting OOS results under {results_root} "
          f"(oos_days_tail={oos_days_tail})...")
    _emit_oos_for_workdir(results_root, sim_dates_by_key, oos_days_tail)


# ── narrow (Phase 2) ─────────────────────────────────────────────────────────

def _rank_by_oos_median(oos_rows):
    """Sort variants by oos_median_pnl desc (tiebreak sharpe, then pct_pos)."""
    def key(r):
        try:
            m = float(r.get("oos_median_pnl", 0) or 0)
            s = float(r.get("oos_sharpe", 0) or 0)
            p = float(r.get("oos_pct_positive", 0) or 0)
        except (ValueError, TypeError):
            m, s, p = 0.0, 0.0, 0.0
        return (m, s, p)
    return sorted(oos_rows, key=key, reverse=True)


def _concentration_narrow(pool_rows, dims, lock_threshold=0.7):
    """For each dim, count top-value share among `pool_rows`.

    Locks the dim to a single value when share ≥ lock_threshold; otherwise
    keeps the top 2-3 values by count.
    """
    narrowed = []
    n = len(pool_rows)
    for path, values in dims:
        param = path[-1]
        counts = Counter()
        for r in pool_rows:
            raw = r.get(param)
            if raw is None or raw == "":
                continue
            try:
                v = float(raw)
                best = min(values, key=lambda x:
                           abs(float(x) - v) if not isinstance(x, bool) else 1e9)
                counts[repr(best)] += 1
            except (ValueError, TypeError):
                if raw in ("True", "False"):
                    counts[repr(raw == "True")] += 1
        if n == 0:
            narrowed.append((path, values))
            continue
        ranked = counts.most_common()
        if not ranked:
            narrowed.append((path, values))
            continue
        top_key, top_count = ranked[0]
        share = top_count / n
        # Regime-defensive dims want the higher value as a tiebreak — hitting a
        # higher-vol period OOS wants defensive params (silver 20260617-20260625
        # lesson).
        REGIME_DEFENSIVE_DIMS = {"vol_norm_coef", "feed_lag_widen_coef"}
        # Reordered: on regime-defensive dims, if top-2 counts are within 15%,
        # prefer higher value.
        if param in REGIME_DEFENSIVE_DIMS and len(ranked) >= 2:
            top_c = ranked[0][1]
            second_c = ranked[1][1]
            if second_c >= top_c * 0.85:
                try:
                    top_v = float(eval(ranked[0][0]))
                    sec_v = float(eval(ranked[1][0]))
                    if sec_v > top_v:
                        ranked[0], ranked[1] = ranked[1], ranked[0]
                        top_key, top_count = ranked[0]
                        share = top_count / n
                        print(f"    (prefer-higher-defensive: {param} tiebreak "
                              f"{top_v}→{sec_v})")
                except (ValueError, TypeError):
                    pass

        if share >= lock_threshold:
            narrowed.append((path, [eval(top_key)]))  # noqa: S307
            print(f"    lock {param} -> {top_key} ({share:.0%} of pool)")
        else:
            keep = [eval(k) for k, c in ranked[:3] if c > 0]  # noqa: S307
            if not keep:
                keep = list(values)
            # Preserve-extremes: for regime-sensitive dims (vol_norm_*,
            # feed_lag_widen_coef, base_cross_thresh), also carry the extreme
            # values along even if top-N by count didn't include them. Silver
            # 20260617-20260625 showed that vnc=3.0 was dropped by the naive
            # top-3 narrow, blinding Phase 1 to the regime-defense zone.
            REGIME_SENSITIVE_DIMS = {
                "vol_norm_coef", "vol_norm_tdc",
                "feed_lag_widen_coef", "base_cross_thresh",
            }
            if param in REGIME_SENSITIVE_DIMS and len(values) > len(keep):
                try:
                    numeric_vals = [float(v) for v in values
                                    if not isinstance(v, bool)]
                    hi = max(numeric_vals)
                    numeric_kept = [float(k) for k in keep
                                    if not isinstance(k, bool)]
                    if numeric_kept and hi > max(numeric_kept):
                        keep.append(hi)
                        print(f"    (preserve-extreme: also carry {hi})")
                except (TypeError, ValueError):
                    pass
            narrowed.append((path, keep))
            print(f"    keep top {len(keep)} for {param}: {keep}")
    return narrowed


def _find_oos_results_for_key(results_root, sym_dir_name):
    """Locate the newest oos_results.txt for a given sym dir name."""
    root = Path(results_root)
    sym_dir = root / sym_dir_name
    if not sym_dir.is_dir():
        return None
    direct = sym_dir / "oos_results.txt"
    if direct.exists():
        return direct
    candidates = []
    for sub in sym_dir.iterdir():
        if sub.is_dir() and (sub / "oos_results.txt").exists():
            candidates.append(sub / "oos_results.txt")
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1]


def cmd_narrow(args):
    """Read oos_results.txt for each sym, apply concentration narrowing, emit next spec."""
    src_spec_path = os.path.expanduser(args.spec)
    src_spec = _load_spec_module(src_spec_path)
    symbols = getattr(src_spec, "symbols", {})
    _validate_sessions(symbols, src_spec_path)

    src_default_dims = getattr(src_spec, "default_dims", RELCROSS_DEFAULT_DIMS)
    src_default_fixed = getattr(src_spec, "default_fixed", RELCROSS_DEFAULT_FIXED)
    sim_days = getattr(src_spec, "sim_days", DEFAULT_SIM_DAYS)
    oos_days_tail = getattr(src_spec, "oos_days_tail", DEFAULT_OOS_DAYS_TAIL)
    min_train_days = getattr(src_spec, "min_train_days", DEFAULT_MIN_TRAIN_DAYS)
    max_variants = getattr(src_spec, "max_variants", DEFAULT_MAX_VARIANTS)

    src_workdir = Path(src_spec_path).parent
    if getattr(args, "results_root", None):
        results_root = Path(os.path.expanduser(args.results_root))
    else:
        results_root = src_workdir / "results"
    if not results_root.is_dir():
        print(f"WARN: no results dir at {results_root} — nothing to narrow.")

    pool_top_n = args.pool_top_n

    per_symbol_narrowed = {}
    per_symbol_summary = {}
    for entry_key in symbols.keys():
        parts = entry_key.split("|")
        sym_name = parts[0]
        session = parts[1] if len(parts) >= 2 else symbols[entry_key].get("session")
        sym_dir_name = _sym_session_dir_name(sym_name, session)

        oos_path = _find_oos_results_for_key(results_root, sym_dir_name)
        if not oos_path:
            print(f"\n[{entry_key}] no oos_results.txt found — leaving dims wide")
            per_symbol_narrowed[entry_key] = src_default_dims
            per_symbol_summary[entry_key] = "no oos_results"
            continue

        with oos_path.open() as f:
            rows = list(csv.DictReader(f))
        if not rows:
            print(f"\n[{entry_key}] empty oos_results — leaving dims wide")
            per_symbol_narrowed[entry_key] = src_default_dims
            per_symbol_summary[entry_key] = "empty oos_results"
            continue

        ranked = _rank_by_oos_median(rows)
        pool = ranked[:pool_top_n]
        print(f"\n[{entry_key}] {len(rows)} oos rows; narrowing on top {len(pool)}")
        narrowed = _concentration_narrow(pool, src_default_dims,
                                         lock_threshold=args.lock_threshold)
        per_symbol_narrowed[entry_key] = narrowed
        per_symbol_summary[entry_key] = f"pool={len(pool)}"

    if args.dry_run:
        print("[dry-run] would write narrowed spec — halting.")
        return

    out_spec_path = Path(os.path.expanduser(args.out))
    out_spec_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append(f"# RelCross Autosearch — Narrowed spec — {date.today():%Y-%m-%d}")
    lines.append(f"# Narrowed from: {src_spec_path}")
    lines.append(f"# Pool: top {pool_top_n} by oos_median_pnl; lock@{args.lock_threshold}")
    lines.append("")
    lines.append(f"sim_days = {sim_days}")
    lines.append(f"oos_days_tail = {oos_days_tail}")
    lines.append(f"min_train_days = {min_train_days}")
    lines.append(f"max_variants = {max_variants}")
    lines.append("random_sample = True")
    lines.append(f"neighborhood_pct = {DEFAULT_NEIGHBORHOOD_PCT}")
    sweep_end = getattr(src_spec, "sweep_end", None)
    if sweep_end:
        lines.append(f"sweep_end = {sweep_end!r}")
    lines.append(f'extra_args = {getattr(src_spec, "extra_args", "")!r}')
    lines.append("")
    lines.append("default_dims = [")
    for path, vals in src_default_dims:
        lines.append(f"    ({path}, {vals}),")
    lines.append("]")
    lines.append("")
    lines.append("default_fixed = [")
    for entry in src_default_fixed:
        lines.append(f"    {tuple(entry)!r},")
    lines.append("]")
    lines.append("")
    lines.append("default_range_dims = []")
    lines.append("")
    lines.append("symbols = {")
    for entry_key, src_entry in symbols.items():
        narrowed = per_symbol_narrowed.get(entry_key, src_default_dims)
        summary = per_symbol_summary.get(entry_key, "")
        lines.append(f'    "{entry_key}": {{   # {summary}')
        for k, v in src_entry.items():
            if k in ("dims", "range_dims", "fixed"):
                continue
            lines.append(f'        "{k}": {v!r},')
        lines.append('        "dims": [')
        for path, vals in narrowed:
            lines.append(f"            ({path}, {vals}),")
        lines.append("        ],")
        lines.append("    },")
    lines.append("}")
    lines.append("")

    out_spec_path.write_text("\n".join(lines))
    print(f"\nWrote narrowed spec: {out_spec_path}")
    print(f"\nNext: run-spec --spec {out_spec_path}")


# ── show-picks presenter (§4, requirement #7) ────────────────────────────────

QUALITY_IS_WORST_5D_MIN = -30.0  # regime-fragility threshold (playbook §4)


def _quality_bar_flags(row):
    """Return short strings summarizing quality-bar checks for a variant.

    Flag legend (printed by show-picks header):
      PP  pct_positive (OOS) ≥ 0.60
      NT  num trades (OOS) ≥ 20 (~ 10-20 borderline; - < 10)
      FR  fillrate (OOS) ≥ 0.005
      AV  OOS avg PnL > 0
      RR  IS/OOS trade-rate ratio ≤ 3× (overfit tell fires if >)
      IA  IS avg PnL > 0 (profitable overall, not just OOS-tail)
      DD  worst 5-day IS window ≥ -$30 (regime-fragility check)
    """
    flags = []
    try:
        pp = float(row.get("oos_pct_positive", 0) or 0)
        nt = float(row.get("oos_num_trds", 0) or 0)
        fr = float(row.get("oos_fillrate", 0) or 0)
        av = float(row.get("oos_avg_pnl", 0) or 0)
        rr_raw = row.get("is_oos_ntrd_ratio", "")
        rr = float(rr_raw) if rr_raw not in ("", "inf") else float("inf")
        ia = float(row.get("is_avg_pnl", 0) or 0)
        dd = float(row.get("is_worst_5d", 0) or 0)
    except (ValueError, TypeError):
        return "?"
    flags.append(("PP+" if pp >= QUALITY_PCT_POS_MIN else "PP-"))
    if nt >= QUALITY_NUM_TRDS_MIN:
        flags.append("NT+")
    elif nt >= QUALITY_NUM_TRDS_SPARSE:
        flags.append("NT~")
    else:
        flags.append("NT-")
    flags.append(("FR+" if fr >= QUALITY_FILLRATE_MIN else "FR-"))
    flags.append(("AV+" if av > 0 else "AV-"))
    flags.append(("RR+" if rr <= QUALITY_IS_OOS_RATIO_MAX else "RR-"))
    flags.append(("IA+" if ia > 0 else "IA-"))
    flags.append(("DD+" if dd >= QUALITY_IS_WORST_5D_MIN else "DD-"))
    return " ".join(flags)


FLAGS_LEGEND = """  Flags legend:
    PP  pct_positive (OOS)      + ≥ 0.60,  - < 0.60
    NT  num trades (OOS)        + ≥ 20,    ~ 10-20,  - < 10
    FR  fillrate (OOS)          + ≥ 0.005, - < 0.005
    AV  OOS avg PnL             + > 0,     - ≤ 0
    RR  IS/OOS trade-rate ratio + ≤ 3×,    - > 3× (overfit tell fires)
    IA  IS avg PnL              + > 0 (profitable overall)
    DD  worst 5-day IS window   + ≥ -$30,  - < -$30 (regime-fragility)"""


def _rank_by_robust(rows):
    """Rank by is_avg + oos_avg + regime-survival bonus. Filters out variants
    with is_avg ≤ 0, oos_avg ≤ 0, or worst-5d IS window < -$50 (regime-fragile).
    Playbook §4 regime-aware rank.
    """
    passing = []
    for r in rows:
        try:
            ia = float(r.get("is_avg_pnl", 0) or 0)
            oa = float(r.get("oos_avg_pnl", 0) or 0)
            dd = float(r.get("is_worst_5d", 0) or 0)
        except (ValueError, TypeError):
            continue
        if ia <= 0 or oa <= 0 or dd < -50.0:
            continue
        # Score: IS_avg + OOS_avg + max(0, worst_5d + 50) × 0.1
        # (bonus for less-bad worst 5-day window, capped at 50 headroom)
        headroom = max(0.0, dd + 50.0)
        r["_robust_score"] = ia + oa + headroom * 0.1
        passing.append(r)
    passing.sort(key=lambda r: -r["_robust_score"])
    return passing


def cmd_show_picks(args):
    """Rank by oos_median_pnl (default) or robust (regime-aware), print top-N.
    NO auto-pick.
    """
    spec_path = os.path.expanduser(args.spec)
    spec_mod = _load_spec_module(spec_path)
    symbols = getattr(spec_mod, "symbols", {})
    _validate_sessions(symbols, spec_path)
    default_dims = getattr(spec_mod, "default_dims", RELCROSS_DEFAULT_DIMS)

    src_workdir = Path(spec_path).parent
    results_root = args.results_root and Path(os.path.expanduser(args.results_root)) or \
        (src_workdir / "results")

    rank_mode = getattr(args, "rank", "oos_median") or "oos_median"
    rank_label = {
        "oos_median": "oos_median_pnl",
        "robust": "regime-robust (IS_avg + OOS_avg + survival)",
    }.get(rank_mode, rank_mode)

    print(FLAGS_LEGEND)

    for entry_key in sorted(symbols.keys()):
        parts = entry_key.split("|")
        sym_name = parts[0]
        session = parts[1] if len(parts) >= 2 else symbols[entry_key].get("session")
        sym_dir_name = _sym_session_dir_name(sym_name, session)
        oos_path = _find_oos_results_for_key(results_root, sym_dir_name)
        if not oos_path:
            print(f"\n=== {entry_key}: no oos_results.txt ===")
            continue
        with oos_path.open() as f:
            rows = list(csv.DictReader(f))
        if not rows:
            print(f"\n=== {entry_key}: empty oos_results ===")
            continue

        if rank_mode == "robust":
            ranked = _rank_by_robust(rows)
            n_ranked = len(ranked)
        else:
            ranked = _rank_by_oos_median(rows)
            n_ranked = len(rows)
        top = ranked[:args.top_n]
        pool_n = min(len(ranked), 100)
        pool = ranked[:pool_n]

        header_note = ""
        if rank_mode == "robust":
            header_note = f" ({n_ranked} pass robust filter of {len(rows)})"
        print(f"\n=== {entry_key}: {len(rows)} variants; top {len(top)} "
              f"by {rank_label}{header_note} ===")
        print(f"  {'var':>6} {'med':>7} {'oos_avg':>8} {'is_avg':>7} "
              f"{'worst5d':>8} {'sharpe':>6} {'pct_pos':>7} "
              f"{'ntrd':>6} {'fillr':>8} {'is/oos':>7}  flags")
        for r in top:
            try:
                med = float(r.get("oos_median_pnl", 0) or 0)
                oav = float(r.get("oos_avg_pnl", 0) or 0)
                iav = float(r.get("is_avg_pnl", 0) or 0)
                w5d = float(r.get("is_worst_5d", 0) or 0)
                sh = float(r.get("oos_sharpe", 0) or 0)
                pp = float(r.get("oos_pct_positive", 0) or 0)
                nt = float(r.get("oos_num_trds", 0) or 0)
                fr = float(r.get("oos_fillrate", 0) or 0)
                rr = r.get("is_oos_ntrd_ratio", "")
            except (ValueError, TypeError):
                continue
            print(f"  {r.get('variant', '?'):>6} "
                  f"{med:>7.2f} {oav:>8.2f} {iav:>7.2f} {w5d:>8.2f} "
                  f"{sh:>6.2f} {pp:>7.2f} "
                  f"{nt:>6.0f} {fr:>8.4f} {rr:>7}  {_quality_bar_flags(r)}")

        # Concentration report on pool (top 100)
        print(f"\n  --- would-be-locked concentration on top {pool_n} ---")
        for path, values in default_dims:
            param = path[-1]
            counts = Counter()
            for r in pool:
                raw = r.get(param)
                if raw is None or raw == "":
                    continue
                try:
                    v = float(raw)
                    best = min(values, key=lambda x:
                               abs(float(x) - v) if not isinstance(x, bool) else 1e9)
                    counts[repr(best)] += 1
                except (ValueError, TypeError):
                    if raw in ("True", "False"):
                        counts[repr(raw == "True")] += 1
            if not counts:
                print(f"    {param:>28}: (no data)")
                continue
            top_key, top_count = counts.most_common(1)[0]
            share = top_count / pool_n if pool_n else 0
            lock_marker = " <-LOCK" if share >= 0.7 else ""
            print(f"    {param:>28}: {top_key} -> {share:.0%}{lock_marker}")


# ── revalidate (stub) ────────────────────────────────────────────────────────

def cmd_revalidate(args):
    """Re-sim top-N variants on a forward window (playbook §3 optional).

    Not implemented — OOS-tail is the primary check per playbook §3. Add
    only when there's a meaningful gap between sweep-end and deploy-time,
    or when the OOS-tail landed on unusually low-vol days.
    """
    raise NotImplementedError(
        "revalidate is a stub. OOS-tail from the sweep is the primary check; "
        "playbook §3 marks post-hoc revalidation as a fallback. Wire this up "
        "with (a) --oos-start/--oos-end date args, (b) a re-sim harness that "
        "reads scratch/<vid>/pk_full.json per variant and calls sim_grid on "
        "the forward window, (c) a summary comparing sweep-OOS vs forward-OOS "
        "stats per variant."
    )


# ── promote ──────────────────────────────────────────────────────────────────

def _resolve_pk_path(scratch_dir, variant_id):
    """Look up scratch/<vid>/pk_full.json or fall back to pk.json."""
    v = Path(scratch_dir) / str(variant_id)
    for name in ("pk_full.json", "pk.json"):
        cand = v / name
        if cand.exists():
            return cand
    return None


def cmd_promote(args):
    """Copy chosen variants to <workdir>/promoted/pk_<sym>_v<vid>.json.

    Picks are read from --picks CSV: two columns `sym_key` and `variant`.
      sym_key,variant
      xyz:LLY|US Day,18711
      xyz:MSFT|US Day,18726
    """
    spec_path = os.path.expanduser(args.spec)
    spec_mod = _load_spec_module(spec_path)
    symbols = getattr(spec_mod, "symbols", {})
    _validate_sessions(symbols, spec_path)

    src_workdir = Path(spec_path).parent
    results_root = src_workdir / "results"
    promoted_dir = src_workdir / "promoted"
    promoted_dir.mkdir(parents=True, exist_ok=True)

    picks_path = Path(os.path.expanduser(args.picks))
    if not picks_path.exists():
        sys.exit(f"picks file not found: {picks_path}")

    with picks_path.open() as f:
        picks = list(csv.DictReader(f))
    if not picks:
        sys.exit(f"empty picks file: {picks_path}")

    for pick in picks:
        sym_key = pick.get("sym_key") or pick.get("symbol_key") or ""
        vid = pick.get("variant") or pick.get("variant_id")
        if not sym_key or not vid:
            print(f"  skip malformed pick: {pick}")
            continue
        if sym_key not in symbols:
            print(f"  skip {sym_key}: not in spec.symbols")
            continue

        parts = sym_key.split("|")
        sym_name = parts[0]
        session = parts[1] if len(parts) >= 2 else symbols[sym_key].get("session")
        sym_dir_name = _sym_session_dir_name(sym_name, session)

        # Find the newest spec subdir under results/<symdir>/
        sym_dir = results_root / sym_dir_name
        if not sym_dir.is_dir():
            print(f"  skip {sym_key}: no results dir {sym_dir}")
            continue
        scratch_dir = None
        direct_scratch = sym_dir / "scratch"
        if direct_scratch.is_dir():
            scratch_dir = direct_scratch
        else:
            candidates = [d / "scratch" for d in sym_dir.iterdir()
                          if d.is_dir() and (d / "scratch").is_dir()]
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime)
                scratch_dir = candidates[-1]
        if scratch_dir is None:
            print(f"  skip {sym_key}: no scratch dir under {sym_dir}")
            continue

        pk_path = _resolve_pk_path(scratch_dir, vid)
        if not pk_path:
            print(f"  skip {sym_key} v{vid}: no pk_full.json/pk.json in {scratch_dir}/{vid}")
            continue

        short = sym_name.split(":")[-1].lower()
        dest = promoted_dir / f"pk_{short}_v{vid}.json"
        if args.dry_run:
            print(f"  [dry-run] would copy {pk_path} -> {dest}")
        else:
            shutil.copy2(pk_path, dest)
            print(f"  promoted {sym_key} v{vid}: {dest}")

    print(f"\nPromoted files under: {promoted_dir}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def _add_common_dispatch_flags(p):
    p.add_argument("--remote", action="store_true",
                   help="Dispatch sims to swarmhost workers.")
    p.add_argument("--hosts", default=None,
                   help="Path to hosts.yaml (default: swarmhost default).")
    p.add_argument("--chunk-size", type=int, default=50,
                   help="Variants per dispatch chunk in --remote mode.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print plan but do not dispatch sims / write files.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # init-spec
    p_init = sub.add_parser("init-spec", help="Write an initial spec.py")
    p_init.add_argument("--workdir", required=True)
    p_init.add_argument("--symbols", required=True,
                        help="Comma-separated symbols (e.g. LLY,MSFT)")
    p_init.add_argument("--donor", required=True,
                        help="Donor symbol to clone from (e.g. xyz:SKHX)")
    p_init.add_argument("--donor-path", default=None,
                        help="Explicit donor config path (skips auto-discovery)")
    p_init.add_argument("--session", default="usday",
                        choices=sorted(SESSION_WINDOWS.keys()))
    p_init.add_argument("--sim-days", type=int, default=DEFAULT_SIM_DAYS)
    p_init.add_argument("--oos-days-tail", type=int, default=DEFAULT_OOS_DAYS_TAIL)
    p_init.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS)
    p_init.add_argument("--sweep-end", default=None,
                        help="Sweep window end date YYYYMMDD (default: today).")
    p_init.add_argument("--dry-run", action="store_true")
    p_init.set_defaults(func=cmd_init_spec)

    # anchor-scan
    p_anc = sub.add_parser("anchor-scan",
        help="Phase 0 coarse scan of the four RelCross anchors")
    p_anc.add_argument("--spec", required=True)
    p_anc.add_argument("--spec-name", default=None,
                       help="Sweep subdir name (default: anchor)")
    _add_common_dispatch_flags(p_anc)
    p_anc.set_defaults(func=cmd_anchor_scan)

    # run-spec
    p_run = sub.add_parser("run-spec",
        help="Run a Phase 1+ multi-dim sweep from a spec")
    p_run.add_argument("--spec", required=True)
    _add_common_dispatch_flags(p_run)
    p_run.set_defaults(func=cmd_run_spec)

    # narrow
    p_narrow = sub.add_parser("narrow",
        help="Read oos_results.txt, apply concentration narrowing, emit next spec")
    p_narrow.add_argument("--spec", required=True,
                          help="Source spec (Phase N)")
    p_narrow.add_argument("--out", required=True,
                          help="Output narrowed spec path (Phase N+1)")
    p_narrow.add_argument("--results-root",
                          help="Path to results/ dir with oos_results.txt "
                          "(default: <workdir>/results). Set to anchor_scan/ "
                          "to narrow from Phase 0 output.")
    p_narrow.add_argument("--pool-top-n", type=int, default=100,
                          help="Pool size (top-N by oos_median_pnl) for concentration analysis")
    p_narrow.add_argument("--lock-threshold", type=float, default=0.7,
                          help="Lock a dim to a single value when top share ≥ this")
    p_narrow.add_argument("--dry-run", action="store_true")
    p_narrow.set_defaults(func=cmd_narrow)

    # show-picks
    p_show = sub.add_parser("show-picks",
        help="Rank variants by oos_median_pnl; print top-N with quality flags")
    p_show.add_argument("--spec", required=True)
    p_show.add_argument("--results-root", default=None,
                        help="Path to results/ dir (default: <workdir>/results)")
    p_show.add_argument("--top-n", type=int, default=20)
    p_show.add_argument("--rank", choices=["oos_median", "robust"],
                        default="oos_median",
                        help="oos_median (default) ranks by OOS median PnL; "
                             "robust applies IS>0/OOS>0/worst_5d≥-50 filter "
                             "and ranks by IS_avg+OOS_avg+regime_bonus")
    p_show.set_defaults(func=cmd_show_picks)

    # revalidate (stub)
    p_rev = sub.add_parser("revalidate",
        help="Re-sim top-N on a forward window (STUB — see playbook §3)")
    p_rev.add_argument("--spec", required=True)
    p_rev.add_argument("--oos-start", required=True, help="YYYYMMDD")
    p_rev.add_argument("--oos-end", required=True, help="YYYYMMDD")
    p_rev.add_argument("--top-n", type=int, default=30)
    p_rev.set_defaults(func=cmd_revalidate)

    # promote
    p_prom = sub.add_parser("promote",
        help="Copy chosen variants to promoted/pk_<sym>_v<vid>.json")
    p_prom.add_argument("--spec", required=True)
    p_prom.add_argument("--picks", required=True,
                        help="CSV file with columns: sym_key,variant")
    p_prom.add_argument("--dry-run", action="store_true")
    p_prom.set_defaults(func=cmd_promote)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
