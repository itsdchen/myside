#!/usr/bin/env python3
"""relwide_equities_autosearch — iterative RelWideMM2 autosearch for equities.

Wraps coverage_autosearch's `scan_thresh` / `run_spec` primitives with
equity-tuned dim defaults, fixed-donor config cloning, and a narrowing
loop. See:
  overmind/studies/relwide_equities_autosearch_playbook.md

Subcommands:
  init-spec    : generate a fresh spec (clones from a fixed donor)
  scan-thresh  : place_thresh-only scan (delegates to coverage_autosearch)
  run-spec     : full grid sweep    (delegates to coverage_autosearch)
  narrow       : read results, generate a narrowed spec for the next pass
"""

import argparse
import csv
import os
import sys
from datetime import date
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
for p in (_SCRIPT_DIR, _STRAT_MAIN):
    if p not in sys.path:
        sys.path.insert(0, p)

import commentjson  # noqa: E402

from coverage_autosearch import (  # noqa: E402
    LOCAL_BASE,
    _build_config_index,
    run_spec as _run_spec_impl,
)
from coverage_report import load_latest_configs  # noqa: E402
from util.symbolizer import _US_EQUITIES  # noqa: E402


# ── Equity-tuned defaults ────────────────────────────────────────────────────
#
# Wide intentional walks per dim — not ±20% perturbations. Sized from the
# May 2026 study findings; see playbook excerpts B and C.

EQUITY_DEFAULT_DIMS = [
    # place_thresh is overridden per-symbol after scan-thresh; this list is
    # a fallback if no scan-thresh has been run.
    (["pktraders", "ordex", "place_thresh"],
     [0.0002, 0.0005, 0.001, 0.002]),
    (["pktraders", "ordex", "cancel_buffer"],
     [0.1, 0.2, 0.3, 0.5]),  # RelWideMM2 uses absolute cancel_buffer
    (["pktraders", "ordex", "pred_momentum_coef"],
     [0.0, 0.5, 2.0, 3.0]),
    (["pktraders", "ordex", "pred_momentum_tdc"],
     [5, 30]),
    (["pktraders", "ordex", "premium_tdc_s"],
     [10, 60, 120]),
    (["pktraders", "ordex", "curv_impulse_coef"],
     [0.0, 1.25]),
    (["pktraders", "ordex", "per_order_widen_frac"],
     [0.5, 0.7, 0.9]),
]

EQUITY_DEFAULT_FIXED = [
    (["pktraders", "ordex", "max_back_levels"], 4),
    (["pktraders", "ordex", "restrict_near_book"], True),
    (["pktraders", "ordex", "can_cross"], False),
    (["pktraders", "ordex", "premium_ema_coef"], 1.0),
    (["pktraders", "ordex", "front_rung_spacing_mult"], 1.0),
    (["pktraders", "ordex", "place_thresh_mode"], 1),
    (["pktraders", "ordex", "ladder_one_sided"], False),
    # Zero out vol mechanisms by default; can be enabled via vol_mode later.
    (["pktraders", "ordex", "vol_widen_coef"], 0),
    (["pktraders", "ordex", "vol_norm_coef"], 0),
    (["pktraders", "ordex", "vol_ratio_coef"], 0),
]

# Fine-grained place_thresh scan: 1 bp per step from 1 to 50 bps.
# Override of coverage_autosearch.SCAN_THRESHES.
FINE_SCAN_THRESHES = [round(i * 0.0001, 8) for i in range(1, 51)]

# Session windows mapped by CLI flag name -> (spec-display-name, (start_t, end_t)).
# usday delays 10 min to skip noisy open-auction microstructure.
# krxday/jpxday match actual cash hours (DST-sensitive: these are correct during
# US EDT; will need to shift 1hr earlier when US falls back to EST in November).
SESSION_WINDOWS = {
    "usday":       ("US Day",       ("09:40:00 America/New_York",
                                     "16:00:00 America/New_York")),
    "usovernight": ("US Overnight", ("18:00:00 America/New_York",
                                     "09:30:00 America/New_York")),
    # KRX equity: 09:00-15:30 KST continuous.
    "krxday":      ("KRX Day",      ("20:00:00 America/New_York",
                                     "02:30:00 America/New_York")),
    # KOSPI 200 futures: 09:00-15:45 KST (15 min longer than spot).
    "krxday_futures": ("KRX Day Futures", ("20:00:00 America/New_York",
                                          "02:45:00 America/New_York")),
    # JPX (TSE) continuous — 09:00-15:30 JST (matches production JP225 config).
    "jpxday":      ("JPX Day",      ("20:00:00 America/New_York",
                                     "02:30:00 America/New_York")),
    # JPX AM session: 09:00-11:30 JST.
    "jpxday_am":   ("JPX Day AM",   ("20:00:00 America/New_York",
                                     "22:30:00 America/New_York")),
    # JPX PM session: 12:30-15:30 JST (includes closing auction).
    "jpxday_pm":   ("JPX Day PM",   ("23:30:00 America/New_York",
                                     "02:30:00 America/New_York")),
    # HKEX AM: 09:30-12:00 HKT.
    "hkxday_am":   ("HKX Day AM",   ("21:30:00 America/New_York",
                                     "00:00:00 America/New_York")),
    # HKEX PM: 13:00-16:00 HKT.
    "hkxday_pm":   ("HKX Day PM",   ("01:00:00 America/New_York",
                                     "04:00:00 America/New_York")),
    # Rest-of-day slots for Asian syms — asian local EOD → US EOD (18:00 ET).
    # Covers the ~15h window when local Asian markets are closed but the HL
    # contract is still active. Relative signal picked per playbook §10b from
    # OLS 1-min-return correlation in the restofday window.
    "krxday_restofday":         ("KRX Day RestOfDay",
                                 ("02:30:00 America/New_York",
                                  "18:00:00 America/New_York")),
    "krxday_futures_restofday": ("KRX Day Futures RestOfDay",
                                 ("02:45:00 America/New_York",
                                  "18:00:00 America/New_York")),
    "jpxday_restofday":         ("JPX Day RestOfDay",
                                 ("02:30:00 America/New_York",
                                  "18:00:00 America/New_York")),
    "hkxday_restofday":         ("HKX Day RestOfDay",
                                 ("04:00:00 America/New_York",
                                  "18:00:00 America/New_York")),
}

# Backward-compat: existing references to EQUITY_US_DAY_WINDOW resolve to usday.
EQUITY_US_DAY_WINDOW = SESSION_WINDOWS["usday"][1]


def _apply_session_override(session_cli_name):
    """Register the requested session window in coverage_autosearch.SESSION_TIMES.

    Returns the spec-display-name (e.g. 'US Day', 'KRX Day') for the caller
    to embed in the spec.
    """
    import coverage_autosearch
    if session_cli_name not in SESSION_WINDOWS:
        raise ValueError(
            f"Unknown session '{session_cli_name}'. "
            f"Choose from: {sorted(SESSION_WINDOWS.keys())}")
    display_name, window = SESSION_WINDOWS[session_cli_name]
    coverage_autosearch.SESSION_TIMES[display_name] = window
    return display_name


def _apply_equity_us_day_override():
    """Backward-compat shim; equivalent to _apply_session_override('usday')."""
    return _apply_session_override("usday")


def _install_remote_ticker_clone_patch():
    """Monkey-patch coverage_autosearch._clone_config_for_symbol so that
    after the donor->target rename, remote-side references get further
    rewritten to use the canonical remote ticker per symbolizer.relwide_remote_wiring.

    Without this, xyz:KR200 clones look for TopBookEquity ticker "KR200"
    even though the actual data is under "KOSPI200". Same for JP225 -> NIY/NIKKEI225.
    """
    import coverage_autosearch
    if getattr(coverage_autosearch, "_remote_ticker_patch_installed", False):
        return
    original_clone = coverage_autosearch._clone_config_for_symbol

    def patched_clone(pk_dct, donor_symbol, target_symbol):
        cloned = original_clone(pk_dct, donor_symbol, target_symbol)
        try:
            from util import symbolizer
            wiring = symbolizer.relwide_remote_wiring.get(target_symbol)
        except Exception:
            wiring = None
        if not wiring:
            return cloned
        remote_ticker = wiring.get("remote_symbol")
        remote_market = wiring.get("remote_market", "TopBookEquity")
        remote_quote_invert = bool(wiring.get("remote_quote_invert", False))
        target_short = target_symbol.split(":")[-1]
        if not remote_ticker:
            return cloned
        ticker_diff = remote_ticker != target_short
        # Rewrite remote-side references in the cloned config.
        for trader in cloned.get("pktraders", []):
            ordex = trader.get("ordex", {})
            if ticker_diff:
                for k in ("remote_sig", "trade_caller"):
                    if k in ordex and isinstance(ordex[k], str):
                        ordex[k] = ordex[k].replace(target_short, remote_ticker)
            for sig in trader.get("signals", []):
                name = sig.get("name", "")
                if name.startswith("remote_"):
                    if ticker_diff:
                        sig["name"] = name.replace(target_short, remote_ticker)
                        if sig.get("symbol") == target_short:
                            sig["symbol"] = remote_ticker
                    # Override the market list to point at the right venue.
                    if "books" in sig:
                        sig["books"] = [remote_market]
                    if sig.get("type") == "SigQuoteMid":
                        if remote_quote_invert:
                            sig["invert"] = True
                        else:
                            sig.pop("invert", None)
            for tempo in trader.get("tempos", []):
                name = tempo.get("name", "")
                if name.startswith("remote_"):
                    if ticker_diff:
                        tempo["name"] = name.replace(target_short, remote_ticker)
                        if tempo.get("symbol") == target_short:
                            tempo["symbol"] = remote_ticker
                    if "markets" in tempo:
                        tempo["markets"] = [remote_market]
                elif ticker_diff and target_short in name:
                    # Non-"remote_" tempo (e.g. rel_tradecall_tempo_<sym>) must
                    # also be renamed to stay in sync with ordex.trade_caller,
                    # which gets target_short→remote_ticker rewritten above.
                    # Don't rewrite symbol/markets here — those are independent
                    # (e.g. NQ on TopBookCme for a relative-anchor tempo).
                    tempo["name"] = name.replace(target_short, remote_ticker)
        return cloned

    coverage_autosearch._clone_config_for_symbol = patched_clone
    coverage_autosearch._remote_ticker_patch_installed = True


DEFAULT_DONOR = "xyz:DKNG"
# Default donor config path (modern RelWideMM2 with rel-uncertainty fields).
# DKNG was tuned in the May 2026 Korean/thin coverage round and has all
# the post-refactor RelWideMM2 fields (relative_sig, beta_uncertainty_coef,
# snapshot_interval_s, etc.). See coverage_autosearch_lessons §3.
# 2026-06-09: switched default to the lag-aware variant (PR #927). The lag-aware
# settings (ioc/alo/cxl base+coef) + BTC aux subscription are baked in, so any
# sym cloned from this donor gets calibrated sim-vs-live behavior by default.
# See overmind/studies/lag_aware_sim_svl_settings_20260609.md for the model.
DEFAULT_DONOR_PATH = "/home/pktrade/scratch/coverage_autosearch/20260511/pk_dkng_patched_v2_lagaware.json"
DEFAULT_SIM_DAYS = 25
DEFAULT_MAX_VARIANTS = 500

# Quality bar (from playbook section 4)
FILLRATE_TARGET = 0.01      # 1% — equity round override of SimVariations default
PCT_POS_MIN = 0.67
AVG_PNL_MIN = 0.0
NUM_TRDS_MIN = 50


def _normalize_symbol(sym):
    """Accept 'TSLA' or 'xyz:TSLA', return canonical 'xyz:TSLA'."""
    if ":" in sym:
        return sym
    return f"xyz:{sym}"


def _find_donor_config_path(donor_symbol):
    """Locate the most recent live config containing donor_symbol's RelWideMM2 pktrader.

    Scans ~/scratch/tradeperf/<machine>/<group>/<strat>/pk_*.json.YYYYMMDD
    via coverage_report.load_latest_configs.
    """
    configs = load_latest_configs(LOCAL_BASE, max_stale_days=None)
    config_index = _build_config_index(configs, LOCAL_BASE)
    entries = config_index.get(donor_symbol)
    if not entries:
        sys.exit(
            f"No live config found for donor {donor_symbol}. "
            f"Pass --donor-path to specify one explicitly.")

    # Prefer the freshest config that has a RelWideMM2 ordex for the donor.
    for entry in sorted(entries, key=lambda e: e["date"], reverse=True):
        path = entry["path"]
        try:
            with open(path) as f:
                cfg = commentjson.load(f)
        except Exception:
            continue
        for t in cfg.get("pktraders", []):
            if t.get("traded_symbol") == donor_symbol and \
               t.get("ordex", {}).get("type") == "RelWideMM2":
                return path
    sys.exit(
        f"Donor {donor_symbol} has live configs but none use RelWideMM2. "
        f"Pass --donor-path to specify one explicitly.")


def _patch_relwide_rel_fields(pk_dct, target_symbol):
    """Add required RelWideMM2 fields if missing (per coverage_autosearch_lessons §3).

    Older configs predate the rel-uncertainty refactor. Loading them
    triggers a RapidJSON assertion at sim startup. This sets safe defaults
    (relative_beta=0 → no rel signal effect on prediction).
    """
    short = target_symbol.split(":")[-1]
    for trader in pk_dct.get("pktraders", []):
        ordex = trader.get("ordex", {})
        if ordex.get("type") != "RelWideMM2":
            continue
        ordex.setdefault("relative_sig", f"local_mid_{short}")
        ordex.setdefault("relative_beta", 0.0)
        ordex.setdefault("beta_uncertainty_coef", 0.0)
        ordex.setdefault("snapshot_interval_s", 60)
        ordex.setdefault("hardcoded_min_tick", 0.01)
        ordex.setdefault("last_perm_snapshot_rel_px.old", 0)
        ordex.setdefault("last_perm_snapshot_remote_px.old", 0)


def cmd_init_spec(args):
    """Generate a fresh spec for the given equity symbols, cloning from a donor."""
    workdir = Path(os.path.expanduser(args.workdir))
    workdir.mkdir(parents=True, exist_ok=True)

    session_display = _apply_session_override(args.session)
    print(f"Session: {args.session} -> {session_display} {SESSION_WINDOWS[args.session][1]}")

    donor = args.donor
    if args.donor_path:
        donor_path = args.donor_path
    elif donor == DEFAULT_DONOR and os.path.exists(DEFAULT_DONOR_PATH):
        donor_path = DEFAULT_DONOR_PATH
    else:
        donor_path = _find_donor_config_path(donor)
    print(f"Donor: {donor}  ->  {donor_path}")

    # Resolve symbols.
    if args.symbols:
        symbols = [_normalize_symbol(s.strip())
                   for s in args.symbols.split(",") if s.strip()]
    elif args.all_equities:
        symbols = sorted(_normalize_symbol(s) for s in _US_EQUITIES)
    else:
        sys.exit("Pass --symbols or --all-equities")

    print(f"Symbols: {symbols}")

    equity_variant = getattr(args, "equity_variant", "") or ""

    # Build & write spec.
    lines = []
    lines.append(f"# RelWide Equities Autosearch Spec — {date.today():%Y-%m-%d}")
    lines.append(f"# Donor (cloned): {donor}")
    lines.append("# Edit dims after scan-thresh, then run --run-spec.")
    lines.append("")
    lines.append(f"sim_days = {args.sim_days}")
    lines.append(f"max_variants = {args.max_variants}")
    lines.append("random_sample = True")
    lines.append("neighborhood_pct = 0.3")
    lines.append(f"equity_variant = {equity_variant!r}")
    if equity_variant:
        lines.append(f'extra_args = "--equity-variant {equity_variant}"')
    else:
        lines.append('extra_args = ""')
    lines.append("")
    lines.append("# Equity-tuned default dims (intentional wide walks).")
    lines.append("default_dims = [")
    for path, vals in EQUITY_DEFAULT_DIMS:
        lines.append(f"    ({path}, {vals}),")
    lines.append("]")
    lines.append("")
    lines.append("default_fixed = [")
    for path, val in EQUITY_DEFAULT_FIXED:
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
    print(f"\nNext: scan-thresh --spec {spec_path}")


def cmd_scan_thresh(args):
    """Place_thresh-only scan with 1 bp granularity, honoring the spec's
    default_fixed overrides (can_cross, max_back_levels, etc.).

    Loosely modeled on coverage_autosearch.scan_thresh but applies
    default_fixed so the scan reflects the regime we'll sweep in Phase 1.

    Pre-fetches both local (Hyperliquid) and remote (TopBookEquity)
    feeds via md_exists.
    """
    from stratbuilder.SimVariations import sim_grid
    from util.chron import dates_list, dates_avail
    from datetime import timedelta
    import importlib.util
    import subprocess

    spec_path = os.path.expanduser(args.spec)
    _apply_session_override(args.session)
    _install_remote_ticker_clone_patch()
    # Import AFTER patch is installed so we get the patched version.
    from coverage_autosearch import (
        _clone_config_for_symbol, SESSION_TIMES,
    )
    _ensure_remote_md_for_spec(spec_path)

    spec = importlib.util.spec_from_file_location("relwide_eq_spec", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    symbols = getattr(mod, "symbols", {})
    sim_days = getattr(mod, "sim_days", DEFAULT_SIM_DAYS)
    default_fixed = getattr(mod, "default_fixed", [])
    start_date_override = getattr(mod, "start_date_override", None)
    # Spec can override the default 1-50bp grid with a custom scan_threshes list.
    scan_threshes = getattr(mod, "scan_threshes", FINE_SCAN_THRESHES)
    extra_args = getattr(mod, "extra_args", "")
    if extra_args:
        print(f"Sim extra_args: {extra_args}")

    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    scan_dir = os.path.join(spec_dir, "thresh_scan")
    os.makedirs(scan_dir, exist_ok=True)

    end_date = date.today()
    if start_date_override:
        from datetime import datetime as _dt
        start_date = _dt.strptime(start_date_override, "%Y%m%d").date()
        print(f"Using start_date_override: {start_date_override}")
    else:
        start_date = end_date - timedelta(days=sim_days + 7)
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    print(f"Scan threshes ({len(scan_threshes)} values): "
          f"{scan_threshes[0]:.5f} .. {scan_threshes[-1]:.5f}")

    summary_lines = [
        f"Place_thresh scan - {date.today():%Y-%m-%d}",
        f"Threshes: {scan_threshes}",
        f"Fixed overrides applied: {[(p[-1], v) for p, v in default_fixed]}",
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

        with open(base_config) as f:
            pk_dct = commentjson.load(f)

        clone_from = sym_spec.get("clone_from")
        if clone_from:
            pk_dct = _clone_config_for_symbol(pk_dct, clone_from, sym_name)
        _patch_relwide_rel_fields(pk_dct, sym_name)

        pk_dct["pktraders"] = [
            t for t in pk_dct["pktraders"]
            if t.get("traded_symbol") == sym_name
        ]
        if not pk_dct["pktraders"]:
            print(f"  symbol not in config")
            continue
        pk_dct["pktraders"][0]["size_mult"] = 1
        pk_dct["pktraders"][0]["enabled"] = True

        if session and session in SESSION_TIMES:
            start_t, end_t = SESSION_TIMES[session]
            pk_dct.setdefault("settings", {})["start_t"] = start_t
            pk_dct["settings"]["end_t"] = end_t

        trader = pk_dct["pktraders"][0]
        mkt = trader["ordex"]["markets"][0]
        md_script = os.path.join(_STRAT_MAIN, "tools", "md_exists.py")
        try:
            subprocess.run(
                [sys.executable, md_script,
                 "--sym", sym_name, "--market", mkt,
                 "--start", start_str, "--end", end_str],
                input="y\n", text=True, timeout=600, capture_output=True,
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
            {"path": ["pktraders", "ordex", "place_thresh"],
             "val": scan_threshes},
        ]
        for path, val in default_fixed:
            scan_overrides.append({"path": list(path), "val": [val]})

        dir_name = sym_name.replace(":", "_")
        if session:
            dir_name += "_" + session.lower().replace(" ", "").replace("-", "")
        sym_scan_dir = os.path.join(scan_dir, dir_name)
        os.makedirs(sym_scan_dir, exist_ok=True)

        print(f"  dates: {sim_dates[0]} to {sim_dates[-1]} ({len(sim_dates)} days)")

        try:
            sim_grid(scan_overrides, pk_dct, sym_scan_dir, sim_dates,
                     fast_sim=False, resume=True,
                     remote=getattr(args, 'remote', False),
                     hosts_yaml=getattr(args, 'hosts', None),
                     chunk_size=getattr(args, 'chunk_size', 50),
                     extra_args=extra_args)
        except Exception as e:
            print(f"  scan failed: {e}")
            continue

        rfile = os.path.join(sym_scan_dir, "results.txt")
        if not os.path.exists(rfile):
            continue
        with open(rfile) as f:
            rows = list(csv.DictReader(f))
        rows.sort(key=lambda r: float(r["place_thresh"]))
        header = f"  {'thresh':>10} {'trades':>8} {'avg_pnl':>10} {'sharpe':>8} {'pct_pos':>8} {'fillrate':>10}"
        print(f"\n{header}")
        summary_lines.append(f"\n{entry_key}")
        summary_lines.append(header)
        for r in rows:
            line = (f"  {float(r['place_thresh']):>10.5f} "
                    f"{int(float(r['num_trds'])):>8} "
                    f"{float(r['avg_pnl']):>10.2f} "
                    f"{float(r['sharpe']):>8.2f} "
                    f"{float(r.get('pct_positive', 0)):>8.2f} "
                    f"{float(r.get('fillrate', 0)):>10.4f}")
            print(line)
            summary_lines.append(line)

    summary_path = os.path.join(scan_dir, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(summary_lines) + "\n")
    print(f"\nScan summary: {summary_path}")


def cmd_run_spec(args):
    """Delegate to coverage_autosearch.run_spec."""
    spec_path = os.path.expanduser(args.spec)
    _apply_session_override(args.session)
    _install_remote_ticker_clone_patch()
    _ensure_remote_md_for_spec(spec_path)
    _run_spec_impl(
        spec_path,
        remote=args.remote,
        hosts_yaml=args.hosts,
        chunk_size=args.chunk_size,
    )


def _remote_ticker_and_market_for(hl_symbol):
    """Map an HL symbol (e.g. 'xyz:KR200') to its (remote_ticker, remote_market).

    Defaults to (bare_symbol, 'TopBookEquity') for US equities. Symbolizer's
    relwide_remote_wiring overrides for HIP3 contracts whose remote feed is
    under a different ticker or market (KR200 -> KOSPI200/TopBookEquity,
    JP225 -> NIY/TopBookCme).
    """
    bare = hl_symbol.split(":")[-1]
    try:
        from util import symbolizer
        wiring = symbolizer.relwide_remote_wiring.get(hl_symbol)
        if wiring:
            return wiring["remote_symbol"], wiring["remote_market"]
    except Exception:
        pass
    return bare, "TopBookEquity"


def _ensure_remote_md_for_spec(spec_path):
    """Pre-fetch remote market data for each symbol in the spec.

    coverage_autosearch.scan_thresh / run_spec call md_exists on the local
    market only. RelWideMM2 silently produces 0 trades when the remote
    feed is missing. Uses symbolizer.relwide_remote_wiring to pick the
    right (ticker, market) per HL symbol.
    """
    import importlib.util
    import subprocess
    from datetime import timedelta

    spec = importlib.util.spec_from_file_location("relwide_eq_spec", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    symbols = list(getattr(mod, "symbols", {}).keys())
    sim_days = getattr(mod, "sim_days", DEFAULT_SIM_DAYS)
    equity_variant = getattr(mod, "equity_variant", "") or ""
    end_date = date.today()
    start_date = end_date - timedelta(days=sim_days + 7)
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    md_script = os.path.join(_STRAT_MAIN, "tools", "md_exists.py")
    for entry_key in symbols:
        sym = entry_key.split("|")[0]
        remote_ticker, remote_market = _remote_ticker_and_market_for(sym)
        # When using an equity variant, also check the variant dir for TopBookEquity.
        if equity_variant and remote_market == "TopBookEquity":
            remote_market = f"TopBookEquity_{equity_variant}"
        print(f"Pre-fetching {remote_market} for {remote_ticker} (HL {sym}, {start_str}–{end_str})...")
        try:
            subprocess.run(
                [sys.executable, md_script,
                 "--sym", remote_ticker, "--market", remote_market,
                 "--start", start_str, "--end", end_str],
                input="y\n", text=True, timeout=600,
                capture_output=True,
            )
        except Exception as e:
            print(f"  warn: md_exists {remote_market} failed for {remote_ticker}: {e}")


def _load_results(results_dir):
    """Walk results.txt files under <workdir>/results/.

    Layouts handled:
      <workdir>/results/<symdir>/results.txt              (legacy)
      <workdir>/results/<symdir>/<spec_basename>/results.txt  (current)

    Yields (label, rows). Label is the relative path from results_dir to
    the dir holding results.txt — e.g. "xyz_AAPL_usday" (legacy) or
    "xyz_AAPL_usday/spec_round_b" (current).
    """
    out = []
    root = Path(results_dir)
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # Legacy: results.txt directly under symdir.
        direct = child / "results.txt"
        if direct.exists():
            with direct.open() as f:
                rows = list(csv.DictReader(f))
            out.append((child.name, rows))
            continue
        # Current: results.txt one level deeper, under spec_basename.
        for sub in sorted(child.iterdir()):
            if not sub.is_dir():
                continue
            rfile = sub / "results.txt"
            if rfile.exists():
                with rfile.open() as f:
                    rows = list(csv.DictReader(f))
                label = f"{child.name}/{sub.name}"
                out.append((label, rows))
    return out


def _healthy_filter(rows):
    """Apply the playbook quality bar; return surviving rows (list of dicts)."""
    healthy = []
    for r in rows:
        try:
            fr = float(r.get("fillrate", 0) or 0)
            pp = float(r.get("pct_positive", 0) or 0)
            pnl = float(r.get("avg_pnl", 0) or 0)
            nt = float(r.get("num_trds", 0) or 0)
        except (ValueError, TypeError):
            continue
        if (fr >= FILLRATE_TARGET and pp >= PCT_POS_MIN and
                pnl > AVG_PNL_MIN and nt >= NUM_TRDS_MIN):
            healthy.append(r)
    return healthy


def _concentration_narrow(healthy_rows, current_dims, lock_threshold=0.7):
    """For each dim, count healthy occurrences of each value.

    If ≥ lock_threshold concentrate at one value, lock to that single value.
    Otherwise keep the top 2-3 values by healthy frequency.

    current_dims: list of (path_list, values_list)
    Returns: list of (path_list, narrowed_values_list)
    """
    narrowed = []
    n_healthy = len(healthy_rows)
    for path, values in current_dims:
        param_name = path[-1]
        counts = {repr(v): 0 for v in values}
        for r in healthy_rows:
            v = r.get(param_name)
            if v is None:
                continue
            # Try to coerce to numeric, then bool, then keep as-is
            try:
                vc = float(v)
                # Match to closest existing value
                best = min(values, key=lambda x: abs(float(x) - vc)
                           if not isinstance(x, bool) else 1e9)
                counts[repr(best)] = counts.get(repr(best), 0) + 1
            except (ValueError, TypeError):
                if v in ("True", "False"):
                    b = (v == "True")
                    counts[repr(b)] = counts.get(repr(b), 0) + 1
        if n_healthy == 0:
            narrowed.append((path, values))
            continue
        # Sort by count desc
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        top_key, top_count = ranked[0]
        share = top_count / n_healthy if n_healthy else 0
        if share >= lock_threshold:
            narrowed.append((path, [eval(top_key)]))  # noqa: S307 - controlled input
            print(f"    lock {param_name} -> {top_key} ({share:.0%} of healthy)")
        else:
            keep = [eval(k) for k, c in ranked[:3] if c > 0]  # noqa: S307
            if not keep:
                keep = list(values)
            narrowed.append((path, keep))
            print(f"    keep top {len(keep)} for {param_name}: {keep}")
    return narrowed


def cmd_narrow(args):
    """Read results dir, generate a narrowed spec for the next pass."""
    src_results = Path(os.path.expanduser(args.from_results))
    out_spec = Path(os.path.expanduser(args.out))
    src_spec = Path(os.path.expanduser(args.spec)) if args.spec else \
        src_results.parent / "spec.py"

    if not src_spec.exists():
        sys.exit(f"Source spec not found: {src_spec}")

    import importlib.util
    spec = importlib.util.spec_from_file_location("relwide_eq_spec_src", src_spec)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    src_symbols = getattr(mod, "symbols", {})
    src_default_dims = getattr(mod, "default_dims", [])
    src_default_fixed = getattr(mod, "default_fixed", [])
    sim_days = getattr(mod, "sim_days", DEFAULT_SIM_DAYS)
    max_variants = getattr(mod, "max_variants", DEFAULT_MAX_VARIANTS)

    print(f"Narrowing from results: {src_results}")
    per_symbol_narrowed = {}
    for sym_dir_name, rows in _load_results(src_results):
        # Strip the trailing /<spec_basename> when present so the lookup below
        # matches dir_name (which is the bare sym/session dir).
        bare_sym_dir = sym_dir_name.split("/", 1)[0]
        print(f"\n[{sym_dir_name}] {len(rows)} rows")
        healthy = _healthy_filter(rows)
        print(f"  healthy (fr>={FILLRATE_TARGET}, pp>={PCT_POS_MIN}, "
              f"pnl>0, nt>={NUM_TRDS_MIN}): {len(healthy)}")
        if not healthy:
            print("  no healthy variants — leaving dims wide for re-explore")
            per_symbol_narrowed[bare_sym_dir] = src_default_dims
            continue
        narrowed = _concentration_narrow(healthy, src_default_dims)
        per_symbol_narrowed[bare_sym_dir] = narrowed

    # Write the narrowed spec.
    lines = []
    lines.append(f"# RelWide Equities Autosearch — Narrowed spec — {date.today():%Y-%m-%d}")
    lines.append(f"# Narrowed from: {src_results}")
    lines.append("")
    lines.append(f"sim_days = {sim_days}")
    lines.append(f"max_variants = {max_variants}")
    lines.append("random_sample = True")
    lines.append("neighborhood_pct = 0.3")
    lines.append("")
    lines.append("default_dims = [")
    for path, vals in src_default_dims:
        lines.append(f"    ({path}, {vals}),")
    lines.append("]")
    lines.append("")
    lines.append("default_fixed = [")
    for entry in src_default_fixed:
        lines.append(f"    {entry!r},")
    lines.append("]")
    lines.append("")
    lines.append("default_range_dims = []")
    lines.append("")
    lines.append("symbols = {")
    for entry_key, src_entry in src_symbols.items():
        # Find the matching results subdir name (replace ":" -> "_", lowercase session)
        sym_name = entry_key.split("|")[0]
        session = entry_key.split("|")[1] if "|" in entry_key else "US Day"
        dir_name = sym_name.replace(":", "_") + "_" + \
            session.lower().replace(" ", "").replace("-", "")
        narrowed = per_symbol_narrowed.get(dir_name, src_default_dims)
        lines.append(f'    "{entry_key}": {{')
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

    out_spec.parent.mkdir(parents=True, exist_ok=True)
    out_spec.write_text("\n".join(lines))
    print(f"\nWrote narrowed spec: {out_spec}")
    print(f"\nNext: run-spec --spec {out_spec}")


def cmd_revalidate(args):
    """Re-sim top-N healthy variants from a prior results dir, with the
    current SESSION_TIMES override (US Day 09:40) applied. Reports
    old (from source results.txt) vs new (re-sim) stats including
    pct_positive, so we can see how much the open contributed.
    """
    from stratbuilder.SimVariations import sim_grid
    from util.chron import dates_avail, dates_list
    from datetime import timedelta
    import subprocess

    _apply_session_override(args.session)
    _install_remote_ticker_clone_patch()
    from coverage_autosearch import _clone_config_for_symbol, SESSION_TIMES

    src = Path(os.path.expanduser(args.from_results))
    out_dir = Path(os.path.expanduser(args.workdir))
    out_dir.mkdir(parents=True, exist_ok=True)
    top_n = args.top_n
    sim_days = args.sim_days

    end_date = date.today()
    start_date = end_date - timedelta(days=sim_days + 7)
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    md_script = os.path.join(_STRAT_MAIN, "tools", "md_exists.py")

    # Discover (sym_dir_name, results_dir_path) pairs. Handle both layouts:
    #   <src>/<symdir>/results.txt              (legacy)
    #   <src>/<symdir>/<spec_basename>/results.txt   (current — picks the
    #                                                 most recently modified
    #                                                 spec subdir per symdir)
    rev_targets = []
    for sym_subdir in sorted(src.iterdir()):
        if not sym_subdir.is_dir():
            continue
        if not sym_subdir.name.startswith("xyz_"):
            continue
        direct = sym_subdir / "results.txt"
        if direct.exists():
            rev_targets.append((sym_subdir.name, sym_subdir))
            continue
        # Look one level deeper.
        spec_dirs = [d for d in sym_subdir.iterdir()
                     if d.is_dir() and (d / "results.txt").exists()]
        if not spec_dirs:
            continue
        # Newest spec subdir wins.
        spec_dirs.sort(key=lambda d: (d / "results.txt").stat().st_mtime)
        chosen = spec_dirs[-1]
        rev_targets.append((sym_subdir.name, chosen))

    summary_rows = []
    for name, sym_subdir in rev_targets:
        rfile = sym_subdir / "results.txt"
        # Re-map name to canonical traded_symbol
        # "xyz_MU_usday" -> "xyz:MU", session "US Day"
        bare = name[len("xyz_"):]
        if bare.endswith("_usday"):
            bare = bare[:-len("_usday")]
            session = "US Day"
        else:
            print(f"  skip {name}: unknown session suffix")
            continue
        traded_symbol = f"xyz:{bare}"
        print(f"\n=== revalidating {traded_symbol} ===")

        # Pre-fetch remote feed.
        try:
            subprocess.run(
                [sys.executable, md_script,
                 "--sym", bare, "--market", "TopBookEquity",
                 "--start", start_str, "--end", end_str],
                input="y\n", text=True, timeout=600, capture_output=True,
            )
        except Exception:
            pass

        # Read top-N healthy from source results.
        with rfile.open() as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            for k in ("avg_pnl", "sharpe", "pct_positive", "num_trds", "fillrate"):
                try:
                    r[k] = float(r[k])
                except (ValueError, TypeError, KeyError):
                    r[k] = 0.0
        healthy = [r for r in rows
                   if r["pct_positive"] >= 0.67 and r["avg_pnl"] > 0
                   and r["num_trds"] >= 20]
        healthy.sort(key=lambda r: r["sharpe"], reverse=True)
        top = healthy[:top_n]
        if not top:
            print(f"  no healthy variants in {sym_subdir}")
            continue
        print(f"  picked top {len(top)} healthy by sharpe "
              f"(of {len(healthy)} healthy / {len(rows)} total)")

        # Resolve sim dates once per symbol.
        try:
            avail = dates_avail(start_str, end_str, traded_symbol, "Hyperliquid",
                                dates_method="WEEKDAYS")
            sim_dates = avail["good_dates"]
        except Exception:
            sim_dates = dates_list(start_str, end_str, dates_method="WEEKDAYS")
        if len(sim_dates) > sim_days:
            sim_dates = sim_dates[-sim_days:]
        if not sim_dates:
            print(f"  no dates available")
            continue

        for old in top:
            vid = old["variant"]
            pk_path = sym_subdir / "scratch" / vid / "pk_full.json"
            if not pk_path.exists():
                # Fallback to pk.json
                pk_path = sym_subdir / "scratch" / vid / "pk.json"
            if not pk_path.exists():
                print(f"  skip v{vid}: no pk file")
                continue
            with pk_path.open() as f:
                pk_dct = commentjson.load(f)
            # Force the new session start time.
            start_t, end_t = SESSION_TIMES[session]
            pk_dct.setdefault("settings", {})["start_t"] = start_t
            pk_dct["settings"]["end_t"] = end_t
            # Filter to the single pktrader (should already be filtered, but be safe).
            pk_dct["pktraders"] = [
                t for t in pk_dct["pktraders"]
                if t.get("traded_symbol") == traded_symbol
            ]
            if not pk_dct["pktraders"]:
                print(f"  skip v{vid}: no matching pktrader")
                continue
            pk_dct["pktraders"][0]["enabled"] = True
            pk_dct["pktraders"][0]["size_mult"] = 1

            # Trivial 1-value dim — sim_grid runs 1 variant for all dates.
            ord_pt = pk_dct["pktraders"][0]["ordex"]["place_thresh"]
            overrides = [{"path": ["pktraders", "ordex", "place_thresh"],
                          "val": [ord_pt]}]
            variant_dir = out_dir / sym_subdir.name / vid
            variant_dir.mkdir(parents=True, exist_ok=True)
            try:
                sim_grid(overrides, pk_dct, str(variant_dir), sim_dates,
                         fast_sim=False, resume=True)
            except Exception as e:
                print(f"  v{vid} sim failed: {e}")
                continue
            new_rfile = variant_dir / "results.txt"
            if not new_rfile.exists():
                continue
            with new_rfile.open() as f:
                new_rows = list(csv.DictReader(f))
            if not new_rows:
                continue
            n = new_rows[0]
            summary_rows.append({
                "symbol": traded_symbol,
                "variant": vid,
                "old_sharpe": old["sharpe"], "new_sharpe": float(n["sharpe"]),
                "old_pnl": old["avg_pnl"], "new_pnl": float(n["avg_pnl"]),
                "old_pct_pos": old["pct_positive"], "new_pct_pos": float(n["pct_positive"]),
                "old_trd": int(old["num_trds"]), "new_trd": int(float(n["num_trds"])),
                "old_fr": old["fillrate"], "new_fr": float(n["fillrate"]),
            })

    # Print and write summary table.
    print(f"\n{'='*100}")
    print(f"{'symbol':>10} {'var':>5}  "
          f"{'sharpe':>14}  {'pnl':>14}  {'pct_pos':>14}  {'trd':>12}  {'fr':>16}")
    print(f"{'':>10} {'':>5}  {'old →new':>14}  {'old →new':>14}  "
          f"{'old →new':>14}  {'old→new':>12}  {'old  →  new':>16}")
    print("-"*120)
    out_csv = out_dir / "revalidate_summary.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()) if summary_rows else [])
        if summary_rows:
            w.writeheader()
        for r in sorted(summary_rows,
                        key=lambda x: (x["symbol"], -x["new_sharpe"])):
            if summary_rows:
                w.writerow(r)
            print(f"{r['symbol']:>10} {r['variant']:>5}  "
                  f"{r['old_sharpe']:>5.2f}->{r['new_sharpe']:>5.2f}  "
                  f"{r['old_pnl']:>6.2f}->{r['new_pnl']:>6.2f}  "
                  f"{r['old_pct_pos']:>5.2f}->{r['new_pct_pos']:>5.2f}  "
                  f"{r['old_trd']:>4d}->{r['new_trd']:>4d}  "
                  f"{r['old_fr']:>6.4f}->{r['new_fr']:>6.4f}")
    print(f"\nWrote {out_csv}")


def cmd_rank(args):
    """Print per-symbol top-N variants using the playbook quality bar."""
    results_dir = Path(os.path.expanduser(args.from_results))
    n = args.top_n
    print(f"Ranking results in {results_dir} (top {n} per symbol)\n")
    print(f"Filter: fillrate>={FILLRATE_TARGET}, pct_positive>={PCT_POS_MIN}, "
          f"avg_pnl>0, num_trds>={NUM_TRDS_MIN}\n")
    for sym_dir_name, rows in _load_results(results_dir):
        healthy = _healthy_filter(rows)
        if not healthy:
            print(f"=== {sym_dir_name}: NO HEALTHY variants ({len(rows)} total) ===")
            continue
        healthy.sort(key=lambda r: float(r.get("sharpe", 0) or 0), reverse=True)
        print(f"=== {sym_dir_name}: {len(healthy)}/{len(rows)} healthy ===")
        print(f"  {'sharpe':>8} {'pnl':>10} {'pct_pos':>8} {'trades':>8} "
              f"{'fillrate':>10}  variant")
        for r in healthy[:n]:
            print(f"  {float(r.get('sharpe', 0)):>8.2f} "
                  f"{float(r.get('avg_pnl', 0)):>10.2f} "
                  f"{float(r.get('pct_positive', 0)):>8.2f} "
                  f"{int(float(r.get('num_trds', 0))):>8} "
                  f"{float(r.get('fillrate', 0)):>10.4f}  "
                  f"{r.get('variant_id', '?')}")
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-spec", help="Generate a fresh spec")
    p_init.add_argument("--symbols", help="Comma-separated symbols (e.g. SNDK,MU)")
    p_init.add_argument("--all-equities", action="store_true",
                        help="Use the full _US_EQUITIES universe")
    p_init.add_argument("--donor", default=DEFAULT_DONOR,
                        help=f"Donor symbol to clone from (default: {DEFAULT_DONOR})")
    p_init.add_argument("--donor-path", default=None,
                        help="Explicit donor config path (overrides auto-discovery)")
    p_init.add_argument("--sim-days", type=int, default=DEFAULT_SIM_DAYS,
                        help=f"Sim window in days (default: {DEFAULT_SIM_DAYS})")
    p_init.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS)
    p_init.add_argument("--workdir", required=True)
    p_init.add_argument("--session", default="usday",
                        choices=sorted(SESSION_WINDOWS.keys()),
                        help="Trading session window (default: usday)")
    p_init.add_argument("--equity-variant", default="",
                        help="TopBookEquity variant suffix (e.g. 'Boats' loads from "
                             "TopBookEquity_Boats/ and passes --equity-variant Boats to pktrade)")
    p_init.set_defaults(func=cmd_init_spec)

    p_scan = sub.add_parser("scan-thresh", help="Place_thresh-only scan")
    p_scan.add_argument("--spec", required=True)
    p_scan.add_argument("--session", default="usday",
                        choices=sorted(SESSION_WINDOWS.keys()),
                        help="Trading session window (must match spec; default: usday)")
    p_scan.add_argument("--remote", action="store_true",
                        help="Dispatch sims to swarmhost workers")
    p_scan.add_argument("--hosts", default=None,
                        help="Path to a hosts.yaml (default: swarmhost default)")
    p_scan.add_argument("--chunk-size", type=int, default=50,
                        help="Variants per dispatch chunk in --remote mode")
    p_scan.set_defaults(func=cmd_scan_thresh)

    p_run = sub.add_parser("run-spec", help="Full multi-dim sweep")
    p_run.add_argument("--spec", required=True)
    p_run.add_argument("--session", default="usday",
                       choices=sorted(SESSION_WINDOWS.keys()),
                       help="Trading session window (must match spec; default: usday)")
    p_run.add_argument("--remote", action="store_true",
                       help="Dispatch sims to swarmhost workers (Hetzner pool)")
    p_run.add_argument("--hosts", default=None,
                       help="Path to a hosts.yaml (default: overmind/swarmhost/hosts.yaml)")
    p_run.add_argument("--chunk-size", type=int, default=50,
                       help="Variants per dispatch chunk in --remote mode (default 50)")
    p_run.set_defaults(func=cmd_run_spec)

    p_narrow = sub.add_parser("narrow",
        help="Read results, generate narrowed spec for next pass")
    p_narrow.add_argument("--from-results", required=True,
        help="Path to <workdir>/results/")
    p_narrow.add_argument("--spec", default=None,
        help="Source spec (default: <results_dir>/../spec.py)")
    p_narrow.add_argument("--out", required=True)
    p_narrow.set_defaults(func=cmd_narrow)

    p_rank = sub.add_parser("rank",
        help="Print top-N healthy variants per symbol from a results dir")
    p_rank.add_argument("--from-results", required=True)
    p_rank.add_argument("--top-n", type=int, default=5)
    p_rank.set_defaults(func=cmd_rank)

    p_rev = sub.add_parser("revalidate",
        help="Re-sim top-N variants with current SESSION_TIMES override")
    p_rev.add_argument("--from-results", required=True,
                       help="Path to prior <workdir>/results/ to read variants from")
    p_rev.add_argument("--workdir", required=True,
                       help="Output dir for re-sim scratch + summary csv")
    p_rev.add_argument("--top-n", type=int, default=30)
    p_rev.add_argument("--sim-days", type=int, default=DEFAULT_SIM_DAYS)
    p_rev.add_argument("--session", default="usday",
                       choices=sorted(SESSION_WINDOWS.keys()),
                       help="Trading session window (default: usday)")
    p_rev.set_defaults(func=cmd_revalidate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
