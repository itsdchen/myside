"""Latency sweep for sim calibration.

Two sweep modes are supported:

- **Legacy fixed-latency**: grid over ``(alo_ord_latency, cxl_ord_latency)``
  via ``--alo``/``--cxl``. Settings are labeled ``a{alo}c{cxl}``.
- **Lag-aware** (June 2026 model): grid over per-TIF
  ``(base_ms, coef)`` axes via ``--alo-base/--alo-coef``,
  ``--cxl-base/--cxl-coef``, ``--ioc-base/--ioc-coef``. Settings are labeled
  by their axis-pair components joined with ``_``, e.g.
  ``ab100_ac0p8_cb60_cc0p5`` for adder or ``ib700_ic1p5`` for crosser.

Both modes compare sim vs live performance across historical dates and print
a composite fit score for calibration.
"""

import copy
import io
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util.pathing import bindir
from util.sim import sim_one_day


def run_sim_sweep(args, config):
    """Run the full latency sweep pipeline."""
    from sim_eval import (_parse_dates, _parse_aliases, _find_dated_config,
                          _fetch_all, _download_market_data)
    from sim_eval_compare import compute_cancel_fills, compute_order_rtt, load_acct_file
    from sim_eval_logs import (parse_log_events, has_event,
                               generate_usermsg_schedule, generate_inherit_schedule)
    from trade_manager import load_acct_data

    # 1. Parse dates
    dates = _parse_dates(args)
    date_strs = [d.strftime("%Y%m%d") for d in dates]

    local_base = getattr(args, "local_base", None) or config["local_base"]
    sim_base = getattr(args, "sim_dir", None) or os.path.expanduser("~/scratch/simeval")
    aliases = _parse_aliases(args, config)
    remote_base = config["remote_base"]
    pnl_weight = float(getattr(args, "pnl_weight", 0.5) or 0.5)
    shs_weight = float(getattr(args, "shs_weight", 1.0) or 1.0)
    fr_weight  = float(getattr(args, "fr_weight",  1.0) or 1.0)
    cfr_weight = float(getattr(args, "cfr_weight", 1.0) or 1.0)
    weights = {"w_pnl": pnl_weight, "w_shs": shs_weight, "w_fr": fr_weight, "w_cfr": cfr_weight}

    # 2. Parse sweep grid — detect mode (legacy vs lag-aware) and build combos
    mode, sweep_combos, sweep_settings, alo_values, cxl_values = _parse_sweep_grid(args)
    n_combos = len(sweep_combos)
    if mode == "legacy":
        print(f"Sweep grid (legacy): alo={alo_values}ms x cxl={cxl_values}ms "
              f"({n_combos} combos)")
    else:
        axis_desc = ", ".join(f"{k}={v}" for k, v in _lag_axes_from_args(args).items())
        print(f"Sweep grid (lag-aware): {axis_desc} ({n_combos} combos)")
        if getattr(args, "force_lag_aware", False):
            print("[force-lag-aware] Will set lag_aware_latency=true and add HYPE "
                  "extra_sub to each pktrader in variant configs.")

    # 3. Fetch
    if not args.skip_fetch:
        print("\n" + "=" * 80)
        print("Fetching files...")
        print("=" * 80)
        _fetch_all(aliases, dates, local_base, remote_base)
    else:
        print("[skip-fetch] Using local files only.")

    # 4. Discover strat — expect exactly one (alias, group, strat)
    print("\nDiscovering strat...")
    acct_data = load_acct_data(local_base, aliases=aliases,
                               date_from=date_strs[0], date_to=date_strs[-1])
    if acct_data.empty:
        print("No acct data found.")
        return

    strat_filter = getattr(args, "strat", None)
    group_filter = getattr(args, "group", None)
    if strat_filter:
        acct_data = acct_data[acct_data["strat"].str.contains(strat_filter, case=False)]
    if group_filter:
        acct_data = acct_data[acct_data["group"].str.contains(group_filter, case=False)]
    if acct_data.empty:
        print("No data after filtering.")
        return

    strat_keys = acct_data[["alias", "group", "strat"]].drop_duplicates()
    if len(strat_keys) > 1:
        print("Error: sweep expects a single strat, found multiple:")
        for _, r in strat_keys.iterrows():
            print(f"  {r['alias']}/{r['group']}/{r['strat']}")
        print("Use --strat/--group/--aliases to narrow.")
        return

    alias = strat_keys.iloc[0]["alias"]
    group = strat_keys.iloc[0]["group"]
    strat = strat_keys.iloc[0]["strat"]
    syms = sorted(acct_data["sym"].unique())
    print(f"Strat: {alias}/{group}/{strat}  ({len(syms)} symbols)")

    # 5. Find dated configs
    configs = {}
    for ds in date_strs:
        p = _find_dated_config(local_base, alias, group, strat, ds)
        if p:
            configs[ds] = p
        else:
            print(f"  [warn] No config for {ds}, skipping")
    if not configs:
        print("No configs found.")
        return
    active_dates = sorted(configs.keys())

    # 6. Download market data
    if not getattr(args, "skip_md", False):
        print("\nDownloading market data...")
        selected = [{"alias": alias, "group": group, "strat": strat, "sym": s}
                    for s in syms]
        sim_dates = [d for d in dates if d.strftime("%Y%m%d") in configs]
        _download_market_data(selected, sim_dates, 0, local_base)

    # 7. Parse log events (DIRTY/CLEAN + schedule generation)
    log_events = parse_log_events(local_base, [alias], active_dates,
                                  event_types={"usermsg", "inherit"})
    day_status = {}
    dirty_reasons = {}
    for ds in active_dates:
        reasons = []
        if has_event(log_events, alias, group, strat, ds, "usermsg"):
            reasons.append("usermsg")
        if has_event(log_events, alias, group, strat, ds, "inherit"):
            reasons.append("inherit")
        day_status[ds] = "DIRTY" if reasons else "CLEAN"
        dirty_reasons[ds] = reasons

    # 8. Compute live RTT (first date with data)
    live_rtt_info = None
    for ds in active_dates:
        live_ord = os.path.join(local_base, alias, group, strat,
                                f"orders_{ds}.csv")
        rtt = compute_order_rtt(live_ord)
        if rtt:
            all_ord = [v for vals in rtt["ord_rtt"].values() for v in vals]
            all_cxl = [v for vals in rtt["cxl_rtt"].values() for v in vals]
            if all_ord or all_cxl:
                live_rtt_info = {"date": ds,
                                 "ord": sorted(all_ord),
                                 "cxl": sorted(all_cxl)}
                break

    # 9. Detect original latency + market key
    first_cfg_path = configs[active_dates[0]]
    with open(first_cfg_path) as f:
        first_cfg = json.load(f)
    sim_section = first_cfg.get("simulation", {})
    market_key = next(iter(sim_section), None) if sim_section else None
    if not market_key:
        print("Error: config has no simulation.<market> section.")
        return

    market_sim = sim_section.get(market_key, {})
    orig_alo_ms = (int(round(market_sim["alo_ord_latency"] * 1000))
                   if "alo_ord_latency" in market_sim else None)
    orig_cxl_ms = (int(round(market_sim["cxl_ord_latency"] * 1000))
                   if "cxl_ord_latency" in market_sim else None)

    # 10. Set up sweep dir and run sims
    sweep_dir = os.path.join(sim_base, "latency_sweep", alias, group, strat)
    os.makedirs(sweep_dir, exist_ok=True)
    pk_bin = os.path.join(bindir(), "pktrade")

    all_settings = ["LIVE", "orig"] + sweep_settings
    n_sims = (1 + len(sweep_settings)) * len(active_dates)

    # day_results[ds][setting] = {"pnl": {sym: float}, "trades": {sym: int}, "cfr": {...}}
    day_results = defaultdict(dict)

    print(f"\nRunning {1 + len(sweep_settings)} settings x "
          f"{len(active_dates)} dates = {n_sims} sims...")

    for ds in active_dates:
        cfg_path = configs[ds]

        # Generate schedule files once per date
        schedule_args = ""
        if log_events:
            strat_events = [e for e in log_events
                           if e["alias"] == alias and e["group"] == group
                           and e["strat"] == strat and e["date"] == ds]
            if strat_events:
                um_path = os.path.join(sweep_dir,
                                       f"usermsg_schedule_{ds}.tsv")
                if generate_usermsg_schedule(strat_events, ds, um_path) > 0:
                    schedule_args += f" --global-usermsg-path {um_path}"
                inh_path = os.path.join(sweep_dir,
                                        f"inherit_schedule_{ds}.csv")
                if generate_inherit_schedule(strat_events, ds, inh_path) > 0:
                    schedule_args += f" --global-inherit-path {inh_path}"

        # Original sim (config as-is)
        print(f"  [{ds}] orig")
        ord_path = os.path.join(sweep_dir, f"orders_sweep_orig_{ds}.csv")
        extra = f"--ord {ord_path}" + schedule_args
        acct_path = sim_one_day(pk_bin, sweep_dir, cfg_path, ds,
                                acct_suffix="sweep_orig",
                                extra_args=extra)
        day_results[ds]["orig"] = _collect_results(acct_path, ord_path)

        # Sweep sims
        with open(cfg_path) as f:
            base_cfg = json.load(f)

        for setting, combo in zip(sweep_settings, sweep_combos):
            print(f"  [{ds}] {setting}")

            sweep_cfg = copy.deepcopy(base_cfg)
            sim_section = sweep_cfg.setdefault("simulation", {})
            market = sim_section.setdefault(market_key, {})

            if mode == "legacy":
                # Legacy fixed-latency: values in combo are already in seconds.
                market["alo_ord_latency"] = combo["alo_ord_latency"]
                market["cxl_ord_latency"] = combo["cxl_ord_latency"]
            else:
                # Lag-aware: combo keys are the JSON field names as-is.
                if getattr(args, "force_lag_aware", False):
                    market["lag_aware_latency"] = True
                    # Add HYPE to each pktrader's extra_subs (idempotent)
                    for p in sweep_cfg.get("pktraders", []) or []:
                        subs = p.setdefault("extra_subs", [])
                        if not any(s.get("market") == "Hyperliquid"
                                   and s.get("symbol") == "HYPE"
                                   for s in subs):
                            subs.append({"market": "Hyperliquid",
                                         "symbol": "HYPE"})
                market.update(combo)

            cfg_out = os.path.join(sweep_dir, f"pk_{setting}_{ds}.json")
            with open(cfg_out, "w") as f:
                json.dump(sweep_cfg, f, indent=2)

            ord_path = os.path.join(sweep_dir,
                                    f"orders_sweep_{setting}_{ds}.csv")
            extra = f"--ord {ord_path}" + schedule_args
            acct_path = sim_one_day(pk_bin, sweep_dir, cfg_out, ds,
                                    acct_suffix=f"sweep_{setting}",
                                    extra_args=extra)
            day_results[ds][setting] = _collect_results(acct_path, ord_path)

    # 11. Collect live data
    for ds in active_dates:
        live_acct = os.path.join(local_base, alias, group, strat,
                                 f"acct_{ds}.csv")
        live_ord = os.path.join(local_base, alias, group, strat,
                                f"orders_{ds}.csv")
        day_results[ds]["LIVE"] = _collect_results(live_acct, live_ord)

    # 12. Generate report
    report = _build_report(alias, group, strat, active_dates,
                           alo_values, cxl_values,
                           orig_alo_ms, orig_cxl_ms, live_rtt_info,
                           day_results, day_status, dirty_reasons,
                           all_settings, mode=mode, weights=weights)
    print(report, end="")

    # 13. Save + email
    output_path = getattr(args, "output", None)
    if output_path:
        output_path = os.path.expanduser(output_path)
        parent = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(parent, exist_ok=True)
        with open(output_path, "w") as f:
            f.write(report)
        print(f"\nReport saved to: {output_path}")

    if getattr(args, "email", False):
        from util import email_utils
        subject = (f"Latency Sweep — {alias}/{strat} — "
                   f"{datetime.today().strftime('%Y-%m-%d')}")
        email_utils.send_mail(subject=subject, body=report, monospace=True)
        print("Report emailed.")


# ── Helpers ──────────────────────────────────────────────────────────────────

# Ordered pairs of (base-flag-attr, coef-flag-attr, json-base-key, json-coef-key,
# short-base-tag, short-coef-tag) used for lag-aware grid generation and labels.
_LAG_AXIS_PAIRS = [
    ("alo_base", "alo_coef", "alo_lag_base_ms", "alo_lag_coef", "ab", "ac"),
    ("cxl_base", "cxl_coef", "cxl_lag_base_ms", "cxl_lag_coef", "cb", "cc"),
    ("ioc_base", "ioc_coef", "ioc_lag_base_ms", "ioc_lag_coef", "ib", "ic"),
]


def _lag_axes_from_args(args):
    """Return an ordered dict-like of {json_key: [values]} for provided lag axes.

    Empty (unset) axes are omitted. Errors if only one of (base, coef) is
    provided for a given axis-pair.
    """
    out = {}
    for base_attr, coef_attr, base_key, coef_key, _, _ in _LAG_AXIS_PAIRS:
        b = getattr(args, base_attr, None)
        c = getattr(args, coef_attr, None)
        if b is None and c is None:
            continue
        if (b is None) != (c is None):
            raise ValueError(
                f"Lag axis '{base_attr[:3]}' requires both --{base_attr.replace('_', '-')} "
                f"and --{coef_attr.replace('_', '-')} to be provided together.")
        out[base_key] = [int(x.strip()) for x in b.split(",") if x.strip()]
        out[coef_key] = [float(x.strip()) for x in c.split(",") if x.strip()]
    return out


def _fmt_coef(v):
    """Render a float coefficient with '.' replaced by 'p' (e.g. 0.8 -> 0p8)."""
    # Trim trailing zeros for compactness (0.80 -> 0p8, 1.0 -> 1p0).
    s = f"{v:g}"
    return s.replace(".", "p").replace("-", "n")


def _parse_sweep_grid(args):
    """Detect legacy vs lag-aware mode and produce (mode, combos, settings, alo, cxl).

    Returns:
        mode: "legacy" or "lag_aware"
        combos: list of dicts (per-setting config-mutation payload)
        settings: parallel list of setting-name strings (deterministic order)
        alo_values, cxl_values: legacy grid axes (empty lists for lag-aware)

    For legacy, each combo has {"alo_ord_latency": seconds, "cxl_ord_latency": seconds}.
    For lag-aware, each combo has the JSON field names as keys (e.g.
    "alo_lag_base_ms", "alo_lag_coef", ...) — unmentioned axes are omitted so
    compiled defaults apply.
    """
    import itertools

    has_legacy = bool(getattr(args, "alo", None) or getattr(args, "cxl", None))
    lag_axes = _lag_axes_from_args(args)
    has_lag = bool(lag_axes)

    if has_legacy and has_lag:
        raise ValueError(
            "sim-sweep: cannot mix legacy (--alo/--cxl) with lag-aware "
            "(--alo-base/--alo-coef/...) axes in the same run.")
    if not has_legacy and not has_lag:
        raise ValueError(
            "sim-sweep: no sweep axes provided. Pass either --alo AND --cxl "
            "(legacy) or at least one lag-aware axis-pair "
            "(--alo-base/--alo-coef, --cxl-base/--cxl-coef, --ioc-base/--ioc-coef).")

    if has_legacy:
        if not (getattr(args, "alo", None) and getattr(args, "cxl", None)):
            raise ValueError("sim-sweep legacy mode requires both --alo and --cxl.")
        alo_values = [int(x.strip()) for x in args.alo.split(",")]
        cxl_values = [int(x.strip()) for x in args.cxl.split(",")]
        combos = []
        settings = []
        for alo in alo_values:
            for cxl in cxl_values:
                combos.append({
                    "alo_ord_latency": alo / 1000.0,
                    "cxl_ord_latency": cxl / 1000.0,
                })
                settings.append(f"a{alo}c{cxl}")
        return "legacy", combos, settings, alo_values, cxl_values

    # Lag-aware — build Cartesian product over provided axis-pairs.
    # We iterate _LAG_AXIS_PAIRS in fixed order so labels are stable.
    axis_specs = []  # list of (base_key, coef_key, base_tag, coef_tag, base_vals, coef_vals)
    for _, _, base_key, coef_key, base_tag, coef_tag in _LAG_AXIS_PAIRS:
        if base_key not in lag_axes:
            continue
        axis_specs.append((base_key, coef_key, base_tag, coef_tag,
                           lag_axes[base_key], lag_axes[coef_key]))

    combos = []
    settings = []
    # Product over (base, coef) per axis, then across axes.
    per_axis_iters = [list(itertools.product(a[4], a[5])) for a in axis_specs]
    for combo_tuple in itertools.product(*per_axis_iters):
        combo = {}
        label_parts = []
        for (base_key, coef_key, base_tag, coef_tag, _, _), (b, c) in zip(axis_specs, combo_tuple):
            combo[base_key] = int(b)
            combo[coef_key] = float(c)
            label_parts.append(f"{base_tag}{int(b)}")
            label_parts.append(f"{coef_tag}{_fmt_coef(c)}")
        combos.append(combo)
        settings.append("_".join(label_parts))

    # Deterministic sort: settings + parallel combos.
    order = sorted(range(len(settings)), key=lambda i: settings[i])
    settings = [settings[i] for i in order]
    combos = [combos[i] for i in order]

    return "lag_aware", combos, settings, [], []


def _collect_results(acct_path, orders_path):
    """Load acct + orders for a single sim/date.

    Returns dict with keys:
        pnl: {sym: float}
        trades: {sym: int}
        cfr: {sym: {"cancels": int, "cancel_fills": int}}
        shs_traded: {sym: float}
        shs_sent: {sym: float}
        fill_rate: {sym: float}
        reachable_fill_rate: {sym: float}
        ioc_fill_rate: {sym: float}
    """
    from sim_eval_compare import load_acct_file, compute_cancel_fills

    result = _empty()
    df = load_acct_file(acct_path)
    if df is not None and "sym" in df.columns:
        for _, row in df.iterrows():
            sym = row["sym"]
            result["pnl"][sym] = float(row.get("net_pnl", 0))
            result["trades"][sym] = int(row.get("times_traded", 0))
            result["shs_traded"][sym] = float(row.get("shs_traded", 0))
            result["shs_sent"][sym] = float(row.get("shs_sent", 0))
            result["fill_rate"][sym] = float(row.get("fill_rate", 0))
            result["reachable_fill_rate"][sym] = float(row.get("reachable_fill_rate", 0))
            result["ioc_fill_rate"][sym] = float(row.get("ioc_fill_rate", 0))
    cfr = compute_cancel_fills(orders_path)
    if cfr:
        result["cfr"] = cfr
    return result


def _empty():
    return {"pnl": {}, "trades": {}, "cfr": {},
            "shs_traded": {}, "shs_sent": {},
            "fill_rate": {}, "reachable_fill_rate": {}, "ioc_fill_rate": {}}


def _composite_score(sim_r, live_r, w_pnl=0.5, w_shs=1.0, w_fr=1.0, w_cfr=1.0):
    """Composite fit score aggregated over syms. Lower is better.

    Weighted sum of |1 - sim/live| deviations for total shs_traded, aggregate
    fill rate, aggregate cancel_fill_rate, and pnl. When a live total is zero
    we fall back to abs(sim) as a penalty for divergence.
    """
    def dev(s, l):
        if not l:
            return abs(s)
        return abs(1 - s / l)

    live_shs = sum(live_r.get("shs_traded", {}).values())
    sim_shs = sum(sim_r.get("shs_traded", {}).values())
    live_pnl = sum(live_r.get("pnl", {}).values())
    sim_pnl = sum(sim_r.get("pnl", {}).values())
    live_sent = sum(live_r.get("shs_sent", {}).values())
    sim_sent = sum(sim_r.get("shs_sent", {}).values())
    live_fr = live_shs / live_sent if live_sent else 0.0
    sim_fr = sim_shs / sim_sent if sim_sent else 0.0
    live_cfr = _total_cfr_pct(live_r.get("cfr", {})) / 100.0
    sim_cfr = _total_cfr_pct(sim_r.get("cfr", {})) / 100.0
    return (w_shs * dev(sim_shs, live_shs)
            + w_fr * dev(sim_fr, live_fr)
            + w_cfr * dev(sim_cfr, live_cfr)
            + w_pnl * dev(sim_pnl, live_pnl))


def _total_cfr_pct(cfr):
    """Total cancel fill rate % across all symbols."""
    total_cxl = sum(s.get("cancels", 0) for s in cfr.values())
    total_cf = sum(s.get("cancel_fills", 0) for s in cfr.values())
    return (total_cf / total_cxl * 100) if total_cxl > 0 else 0.0


def _sym_cfr_pct(cfr, sym):
    """Cancel fill rate % for a single symbol."""
    s = cfr.get(sym, {})
    cxl = s.get("cancels", 0)
    cf = s.get("cancel_fills", 0)
    return (cf / cxl * 100) if cxl > 0 else 0.0


def _setting_label(setting, orig_alo_ms):
    """Display label for a setting.

    Legacy labels ``a{alo}c{cxl}`` and the "orig" placeholder get special
    treatment; lag-aware labels (starting with ``ab``, ``cb``, ``ib``)
    are already human-readable so they pass through unchanged.
    """
    if setting == "LIVE":
        return "LIVE"
    if setting == "orig":
        return f"orig({orig_alo_ms})" if orig_alo_ms is not None else "orig"
    return setting


# ── Report generation ────────────────────────────────────────────────────────

def _build_report(alias, group, strat, active_dates, alo_values, cxl_values,
                  orig_alo_ms, orig_cxl_ms, live_rtt_info,
                  day_results, day_status, dirty_reasons, all_settings,
                  mode="legacy", weights=None):
    if weights is None:
        weights = {"w_pnl": 0.5, "w_shs": 1.0, "w_fr": 1.0, "w_cfr": 1.0}
    pnl_weight = weights.get("w_pnl", 0.5)
    """Build the full sweep report text."""
    out = io.StringIO()
    SEP = "=" * 120

    # Sweep-setting-only list (excludes LIVE and orig) — used to render the
    # composite-fit table and derive the sweep-axis description.
    sweep_settings = [s for s in all_settings if s not in ("LIVE", "orig")]

    # ── Header ───────────────────────────────────────────────────────────
    out.write(SEP + "\n")
    out.write("LATENCY SWEEP REPORT\n")
    out.write(f"Strat: {alias} / {group} / {strat}\n")
    out.write(f"Dates: {', '.join(active_dates)}\n")
    if mode == "legacy":
        out.write(f"Sweep: alo in {{{', '.join(str(v) for v in alo_values)}}}ms"
                  f"  x  cxl in {{{', '.join(str(v) for v in cxl_values)}}}ms\n")
    else:
        out.write(f"Sweep (lag-aware): {len(sweep_settings)} combos "
                  f"[pnl_weight={pnl_weight:g}]\n")

    if orig_alo_ms is not None and orig_cxl_ms is not None:
        out.write(f"Original sim: alo={orig_alo_ms}ms cxl={orig_cxl_ms}ms\n")
    elif orig_alo_ms is not None:
        out.write(f"Original sim: alo={orig_alo_ms}ms cxl=default\n")
    else:
        out.write("Original sim: using config defaults\n")

    if live_rtt_info:
        o = live_rtt_info["ord"]
        c = live_rtt_info["cxl"]
        if o and c:
            n_o, n_c = len(o), len(c)
            out.write(
                f"Live RTT ({live_rtt_info['date']}): "
                f"NewOrd\u2192Ack p50={o[n_o // 2]}ms "
                f"p90={o[min(int(n_o * 0.9), n_o - 1)]}ms  "
                f"Cancel\u2192Ack p50={c[n_c // 2]}ms "
                f"p90={c[min(int(n_c * 0.9), n_c - 1)]}ms\n")
    out.write(SEP + "\n")

    # ── Per-date sections ────────────────────────────────────────────────
    for ds in active_dates:
        results = day_results.get(ds, {})
        live = results.get("LIVE", _empty())
        orig = results.get("orig", _empty())

        status = day_status.get(ds, "CLEAN")
        reasons = dirty_reasons.get(ds, [])
        if status == "DIRTY":
            status_str = f"DIRTY - {' + '.join(reasons)} events"
        else:
            status_str = "CLEAN - no live events"

        live_pnl = sum(live["pnl"].values())
        live_trades = sum(live["trades"].values())
        live_cfr = _total_cfr_pct(live["cfr"])
        orig_pnl = sum(orig["pnl"].values())
        orig_trades = sum(orig["trades"].values())
        orig_cfr = _total_cfr_pct(orig["cfr"])

        out.write(f"\n\n{SEP}\n")
        out.write(f"  DATE: {ds}  ({status_str})\n")
        out.write(f"  Live:     net_pnl={live_pnl:>10.2f}   "
                  f"trades={live_trades:>5}   "
                  f"cancel_fill_rate={live_cfr:.2f}%\n")
        out.write(f"  Original: net_pnl={orig_pnl:>10.2f}   "
                  f"trades={orig_trades:>5}   "
                  f"cancel_fill_rate={orig_cfr:.2f}%   "
                  f"pnl_diff={orig_pnl - live_pnl:+.2f}\n")
        out.write(SEP + "\n")

        # SUMMARY table
        out.write(f"\n  SUMMARY\n")
        out.write(f"  {'setting':<15} {'cfr%':>8} {'pnl':>10} "
                  f"{'pnl_diff':>10} {'trades':>7} {'trd_diff':>9}\n")
        out.write("  " + "-" * 60 + "\n")

        for setting in all_settings:
            if setting not in results:
                continue
            r = results[setting]
            s_pnl = sum(r["pnl"].values())
            s_trades = sum(r["trades"].values())
            s_cfr = _total_cfr_pct(r["cfr"])
            lbl = _setting_label(setting, orig_alo_ms)

            if setting == "LIVE":
                out.write(f"  {lbl:<15} {s_cfr:>7.2f}% {s_pnl:>10.2f} "
                          f"{'---':>10} {s_trades:>7} {'---':>9}\n")
            else:
                diff = s_pnl - live_pnl
                trd_diff = s_trades - live_trades
                out.write(f"  {lbl:<15} {s_cfr:>7.2f}% {s_pnl:>10.2f} "
                          f"{diff:>+10.2f} {s_trades:>7} {trd_diff:>+9d}\n")

        # Gather all symbols for this date
        all_syms = set()
        for setting in all_settings:
            if setting in results:
                all_syms.update(results[setting]["pnl"].keys())
        all_syms = sorted(all_syms)
        display_syms = [s.replace("xyz:", "") for s in all_syms]

        present = [(s, _setting_label(s, orig_alo_ms))
                    for s in all_settings if s in results]

        _write_cfr_table(out, present, results, all_syms, display_syms)
        _write_pnl_table(out, present, results, all_syms, display_syms)
        _write_trades_table(out, present, results, all_syms, display_syms)

        # COMPOSITE FIT table (per-date)
        _write_composite_fit(out, results, sweep_settings, orig_alo_ms,
                             weights)

    # ── Aggregates ───────────────────────────────────────────────────────
    dirty_dates = [ds for ds in active_dates if day_status.get(ds) == "DIRTY"]
    clean_dates = [ds for ds in active_dates if day_status.get(ds) == "CLEAN"]

    all_syms_g = set()
    for ds in active_dates:
        for s in all_settings:
            if s in day_results.get(ds, {}):
                all_syms_g.update(day_results[ds][s]["pnl"].keys())
    all_syms_g = sorted(all_syms_g)
    display_syms_g = [s.replace("xyz:", "") for s in all_syms_g]

    labels_g = [(s, _setting_label(s, orig_alo_ms)) for s in all_settings]

    if len(active_dates) > 1:
        _write_aggregate(out, f"ALL {len(active_dates)} DAYS",
                         active_dates, day_results, all_settings,
                         all_syms_g, display_syms_g, labels_g,
                         orig_alo_ms=orig_alo_ms, weights=weights)

    if dirty_dates:
        _write_aggregate(out,
                         f"DIRTY DAYS ONLY ({', '.join(dirty_dates)})",
                         dirty_dates, day_results, all_settings,
                         all_syms_g, display_syms_g, labels_g,
                         orig_alo_ms=orig_alo_ms, weights=weights)

    if clean_dates and dirty_dates:
        suffix = "S" if len(clean_dates) > 1 else ""
        _write_aggregate(out,
                         f"CLEAN DAY{suffix} ONLY ({', '.join(clean_dates)})",
                         clean_dates, day_results, all_settings,
                         all_syms_g, display_syms_g, labels_g,
                         orig_alo_ms=orig_alo_ms, weights=weights)

    return out.getvalue()


# ── Table writers ────────────────────────────────────────────────────────────

def _write_cfr_table(out, labels, results, all_syms, display_syms):
    """Write CANCEL FILL RATE BY SYMBOL table."""
    col_w = max(8, max((len(l) for _, l in labels), default=8))

    out.write("\n  CANCEL FILL RATE BY SYMBOL\n")
    sym_w = max(8, max((len(s) for s in display_syms), default=4) + 2)
    hdr = f"  {'sym':<{sym_w}}"
    for _, label in labels:
        hdr += f" {label:>{col_w}}"
    out.write(hdr + "\n")
    out.write("  " + "-" * (len(hdr) - 2) + "\n")

    for sym, dsym in zip(all_syms, display_syms):
        row = f"  {dsym:<{sym_w}}"
        for setting, _ in labels:
            r = results.get(setting, _empty())
            pct = _sym_cfr_pct(r["cfr"], sym)
            row += f" {pct:>{col_w - 1}.2f}%"
        out.write(row + "\n")

    row = f"  {'TOTAL':<{sym_w}}"
    for setting, _ in labels:
        r = results.get(setting, _empty())
        pct = _total_cfr_pct(r["cfr"])
        row += f" {pct:>{col_w - 1}.2f}%"
    out.write(row + "\n")


def _write_pnl_table(out, labels, results, all_syms, display_syms):
    """Write NET PNL BY SYMBOL table."""
    col_w = max(9, max((len(l) for _, l in labels), default=9))

    out.write("\n  NET PNL BY SYMBOL\n")
    sym_w = max(8, max((len(s) for s in display_syms), default=4) + 2)
    hdr = f"  {'sym':<{sym_w}}"
    for _, label in labels:
        hdr += f" {label:>{col_w}}"
    out.write(hdr + "\n")
    out.write("  " + "-" * (len(hdr) - 2) + "\n")

    for sym, dsym in zip(all_syms, display_syms):
        row = f"  {dsym:<{sym_w}}"
        for setting, _ in labels:
            r = results.get(setting, _empty())
            pnl = r["pnl"].get(sym, 0)
            row += f" {pnl:>{col_w}.2f}"
        out.write(row + "\n")

    row = f"  {'TOTAL':<{sym_w}}"
    for setting, _ in labels:
        r = results.get(setting, _empty())
        total = sum(r["pnl"].values())
        row += f" {total:>{col_w}.2f}"
    out.write(row + "\n")


def _write_trades_table(out, labels, results, all_syms, display_syms):
    """Write TIMES TRADED BY SYMBOL table."""
    col_w = max(6, max((len(l) for _, l in labels), default=6))

    out.write("\n  TIMES TRADED BY SYMBOL\n")
    sym_w = max(8, max((len(s) for s in display_syms), default=4) + 2)
    hdr = f"  {'sym':<{sym_w}}"
    for _, label in labels:
        hdr += f" {label:>{col_w}}"
    out.write(hdr + "\n")
    out.write("  " + "-" * (len(hdr) - 2) + "\n")

    for sym, dsym in zip(all_syms, display_syms):
        row = f"  {dsym:<{sym_w}}"
        for setting, _ in labels:
            r = results.get(setting, _empty())
            trades = r["trades"].get(sym, 0)
            row += f" {trades:>{col_w}d}"
        out.write(row + "\n")

    row = f"  {'TOTAL':<{sym_w}}"
    for setting, _ in labels:
        r = results.get(setting, _empty())
        total = sum(r["trades"].values())
        row += f" {total:>{col_w}d}"
    out.write(row + "\n")


def _write_composite_fit(out, results, sweep_settings, orig_alo_ms, weights):
    """Write COMPOSITE FIT table for a set of results (per-date or aggregate).

    ``results`` is the same shape used elsewhere: ``{setting: _collect_results-like}``.
    Prints LIVE first (score = 0 by definition), then sweep settings sorted by
    ascending composite score (best first). "orig" — if present — is included
    among the ranked rows so users can see how it compares.
    """
    live = results.get("LIVE")
    if live is None:
        # Can't produce ratios without a LIVE baseline; skip.
        return

    live_shs = sum(live.get("shs_traded", {}).values())
    live_sent = sum(live.get("shs_sent", {}).values())
    live_pnl = sum(live.get("pnl", {}).values())
    live_trades = sum(live.get("trades", {}).values())
    live_fr = (live_shs / live_sent) if live_sent else 0.0
    live_cfr = _total_cfr_pct(live.get("cfr", {}))

    # Collect ranked rows: (score, label, row-values-tuple)
    ranked = []
    # Include "orig" for comparison, plus sweep settings.
    candidates = [s for s in ["orig"] + list(sweep_settings) if s in results]
    for setting in candidates:
        r = results[setting]
        score = _composite_score(r, live, **weights)
        s_shs = sum(r.get("shs_traded", {}).values())
        s_sent = sum(r.get("shs_sent", {}).values())
        s_pnl = sum(r.get("pnl", {}).values())
        s_trades = sum(r.get("trades", {}).values())
        s_fr = (s_shs / s_sent) if s_sent else 0.0
        s_cfr = _total_cfr_pct(r.get("cfr", {}))

        def ratio(s, l):
            if not l:
                return float("nan")
            return s / l

        ranked.append({
            "score": score,
            "label": _setting_label(setting, orig_alo_ms),
            "shs": s_shs,
            "shs_r": ratio(s_shs, live_shs),
            "fr": s_fr,
            "fr_r": ratio(s_fr, live_fr),
            "cfr": s_cfr,
            "cfr_r": ratio(s_cfr, live_cfr),
            "pnl": s_pnl,
            "pnl_r": ratio(s_pnl, live_pnl),
            "trades": s_trades,
        })
    ranked.sort(key=lambda x: x["score"])

    # Column widths — pick a label width wide enough for the longest label.
    all_labels = ["LIVE"] + [row["label"] for row in ranked]
    lbl_w = max(12, max(len(l) for l in all_labels))

    w_str = ", ".join(f"{k[2:]}={v:g}" for k, v in weights.items())
    out.write(f"\n  COMPOSITE FIT (sorted by score, best first; weights: {w_str})\n")
    hdr = (f"  {'setting':<{lbl_w}} {'score':>7} "
           f"{'shs':>10} {'shs_r':>7} "
           f"{'fill%':>7} {'fr_r':>7} "
           f"{'cfr%':>7} {'cfr_r':>7} "
           f"{'pnl':>10} {'pnl_r':>7} "
           f"{'trades':>8}")
    out.write(hdr + "\n")
    out.write("  " + "-" * (len(hdr) - 2) + "\n")

    def fmt_ratio(v):
        if v != v:  # NaN
            return "--"
        return f"{v:.2f}"

    # LIVE row
    out.write(f"  {'LIVE':<{lbl_w}} {'---':>7} "
              f"{live_shs:>10.0f} {'---':>7} "
              f"{live_fr * 100:>6.2f}% {'---':>7} "
              f"{live_cfr:>6.2f}% {'---':>7} "
              f"{live_pnl:>10.2f} {'---':>7} "
              f"{live_trades:>8}\n")

    # Sweep rows (sorted). Mark the best row with a trailing " <- best".
    for i, row in enumerate(ranked):
        marker = "  <- best" if i == 0 else ""
        out.write(f"  {row['label']:<{lbl_w}} {row['score']:>7.3f} "
                  f"{row['shs']:>10.0f} {fmt_ratio(row['shs_r']):>7} "
                  f"{row['fr'] * 100:>6.2f}% {fmt_ratio(row['fr_r']):>7} "
                  f"{row['cfr']:>6.2f}% {fmt_ratio(row['cfr_r']):>7} "
                  f"{row['pnl']:>10.2f} {fmt_ratio(row['pnl_r']):>7} "
                  f"{row['trades']:>8}{marker}\n")


def _write_aggregate(out, title, date_list, day_results, all_settings,
                     all_syms, display_syms, labels,
                     orig_alo_ms=None, weights=None):
    """Write an aggregate section (ALL / DIRTY / CLEAN)."""
    if weights is None:
        weights = {"w_pnl": 0.5, "w_shs": 1.0, "w_fr": 1.0, "w_cfr": 1.0}
    SEP = "=" * 120
    n = len(date_list)

    # Sum results across dates. In addition to pnl/trades/cfr we accumulate
    # shs_traded and shs_sent so the composite-fit table can compute aggregate
    # fill-rate ratios.
    agg = {}
    for setting in all_settings:
        agg_pnl = defaultdict(float)
        agg_trades = defaultdict(int)
        agg_cfr = defaultdict(lambda: {"cancels": 0, "cancel_fills": 0})
        agg_shs_traded = defaultdict(float)
        agg_shs_sent = defaultdict(float)
        for ds in date_list:
            r = day_results.get(ds, {}).get(setting)
            if not r:
                continue
            for sym, pnl in r["pnl"].items():
                agg_pnl[sym] += pnl
            for sym, trades in r["trades"].items():
                agg_trades[sym] += trades
            for sym, cfr_data in r["cfr"].items():
                agg_cfr[sym]["cancels"] += cfr_data.get("cancels", 0)
                agg_cfr[sym]["cancel_fills"] += cfr_data.get("cancel_fills", 0)
            for sym, v in r.get("shs_traded", {}).items():
                agg_shs_traded[sym] += v
            for sym, v in r.get("shs_sent", {}).items():
                agg_shs_sent[sym] += v
        agg[setting] = {"pnl": dict(agg_pnl),
                        "trades": dict(agg_trades),
                        "cfr": dict(agg_cfr),
                        "shs_traded": dict(agg_shs_traded),
                        "shs_sent": dict(agg_shs_sent),
                        "fill_rate": {},
                        "reachable_fill_rate": {},
                        "ioc_fill_rate": {}}

    live = agg.get("LIVE", _empty())
    orig = agg.get("orig", _empty())
    live_pnl = sum(live["pnl"].values())
    live_trades = sum(live["trades"].values())
    orig_pnl = sum(orig["pnl"].values())
    orig_trades = sum(orig["trades"].values())

    out.write(f"\n\n{SEP}\n")
    out.write(f"  AGGREGATE: {title}\n")
    out.write(SEP + "\n")

    out.write(f"\n  Live:     sum_pnl={live_pnl:>10.2f}   "
              f"sum_trades={live_trades:>5}\n")
    out.write(f"  Original: sum_pnl={orig_pnl:>10.2f}   "
              f"sum_trades={orig_trades:>5}   "
              f"pnl_diff={orig_pnl - live_pnl:+.2f}\n")

    # Summary table
    out.write(f"\n  {'setting':<12} {'avg_cfr%':>10} {'sum_pnl':>10} "
              f"{'pnl_diff':>10} {'sum_trd':>8} {'trd_diff':>9} "
              f"{'avg_pnl/d':>10}\n")
    out.write("  " + "-" * 70 + "\n")

    for setting in all_settings:
        if setting not in agg:
            continue
        r = agg[setting]
        s_pnl = sum(r["pnl"].values())
        s_trades = sum(r["trades"].values())
        s_cfr = _total_cfr_pct(r["cfr"])
        avg_d = s_pnl / n if n > 0 else 0

        if setting == "LIVE":
            lbl = "LIVE"
            out.write(f"  {lbl:<12} {s_cfr:>9.2f}% {s_pnl:>10.2f} "
                      f"{'---':>10} {s_trades:>8} {'---':>9} "
                      f"{avg_d:>10.2f}\n")
        else:
            lbl = next((l for s, l in labels if s == setting), setting)
            diff = s_pnl - live_pnl
            trd_diff = s_trades - live_trades
            out.write(f"  {lbl:<12} {s_cfr:>9.2f}% {s_pnl:>10.2f} "
                      f"{diff:>+10.2f} {s_trades:>8} {trd_diff:>+9d} "
                      f"{avg_d:>10.2f}\n")

    # Per-symbol aggregate PNL
    present = [(s, l) for s, l in labels if s in agg]
    col_w = max(9, max((len(l) for _, l in present), default=9))
    sym_w = max(8, max((len(s) for s in display_syms), default=4) + 2)

    out.write(f"\n  PER-SYMBOL AGGREGATE PNL\n")
    hdr = f"  {'sym':<{sym_w}}"
    for _, label in present:
        hdr += f" {label:>{col_w}}"
    out.write(hdr + "\n")
    out.write("  " + "-" * (len(hdr) - 2) + "\n")

    for sym, dsym in zip(all_syms, display_syms):
        row = f"  {dsym:<{sym_w}}"
        for setting, _ in present:
            r = agg.get(setting, _empty())
            pnl = r["pnl"].get(sym, 0)
            row += f" {pnl:>{col_w}.2f}"
        out.write(row + "\n")

    row = f"  {'TOTAL':<{sym_w}}"
    for setting, _ in present:
        r = agg.get(setting, _empty())
        total = sum(r["pnl"].values())
        row += f" {total:>{col_w}.2f}"
    out.write(row + "\n")

    # Per-symbol aggregate CFR
    out.write(f"\n  PER-SYMBOL AGGREGATE CANCEL FILL RATE\n")
    hdr = f"  {'sym':<{sym_w}}"
    for _, label in present:
        hdr += f" {label:>{col_w}}"
    out.write(hdr + "\n")
    out.write("  " + "-" * (len(hdr) - 2) + "\n")

    for sym, dsym in zip(all_syms, display_syms):
        row = f"  {dsym:<{sym_w}}"
        for setting, _ in present:
            r = agg.get(setting, _empty())
            pct = _sym_cfr_pct(r["cfr"], sym)
            row += f" {pct:>{col_w - 1}.2f}%"
        out.write(row + "\n")

    row = f"  {'TOTAL':<{sym_w}}"
    for setting, _ in present:
        r = agg.get(setting, _empty())
        pct = _total_cfr_pct(r["cfr"])
        row += f" {pct:>{col_w - 1}.2f}%"
    out.write(row + "\n")

    # Per-symbol aggregate trades
    out.write(f"\n  PER-SYMBOL AGGREGATE TRADES\n")
    hdr = f"  {'sym':<{sym_w}}"
    for _, label in present:
        hdr += f" {label:>{col_w}}"
    out.write(hdr + "\n")
    out.write("  " + "-" * (len(hdr) - 2) + "\n")

    for sym, dsym in zip(all_syms, display_syms):
        row = f"  {dsym:<{sym_w}}"
        for setting, _ in present:
            r = agg.get(setting, _empty())
            trades = r["trades"].get(sym, 0)
            row += f" {trades:>{col_w}}"
        out.write(row + "\n")

    row = f"  {'TOTAL':<{sym_w}}"
    for setting, _ in present:
        r = agg.get(setting, _empty())
        total = sum(r["trades"].values())
        row += f" {total:>{col_w}}"
    out.write(row + "\n")

    # COMPOSITE FIT table (aggregate)
    sweep_settings = [s for s in all_settings if s not in ("LIVE", "orig")]
    _write_composite_fit(out, agg, sweep_settings, orig_alo_ms, weights)
