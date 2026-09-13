"""Sim vs live comparison metrics and reporting."""

import io
import os
from collections import defaultdict

import pandas as pd


# Columns from acct files used for comparison
METRIC_COLS = ["net_pnl", "closed_pnl", "commission", "shs_traded", "shs_sent",
               "times_traded", "times_flipped", "fill_rate", "reachable_fill_rate",
               "ioc_fill_rate"]


def load_acct_file(path):
    """Load an acct CSV, coercing metric columns to numeric."""
    if not path or not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if df.empty:
        return None
    for c in METRIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def compare_day(sim_acct_path, live_acct_path, alias, group, strat,
                live_date, log_events=None):
    """Compare a single sim acct file against a live acct file.

    Returns list of per-symbol comparison row dicts.
    """
    from sim_eval_logs import has_event

    sim_df = load_acct_file(sim_acct_path)
    live_df = load_acct_file(live_acct_path)

    if live_df is None:
        return []

    rows = []
    all_syms = set()
    if live_df is not None and "sym" in live_df.columns:
        all_syms.update(live_df["sym"].unique())
    if sim_df is not None and "sym" in sim_df.columns:
        all_syms.update(sim_df["sym"].unique())

    for sym in sorted(all_syms):
        live_row = live_df[live_df["sym"] == sym].iloc[0] if (live_df is not None and sym in live_df["sym"].values) else None
        sim_row = sim_df[sim_df["sym"] == sym].iloc[0] if (sim_df is not None and sym in sim_df["sym"].values) else None

        row = {
            "date": live_date,
            "alias": alias,
            "group": group,
            "strat": strat,
            "sym": sym,
        }

        if live_row is None or sim_row is None:
            row["status"] = "MISSING_SIM" if sim_row is None else "MISSING_LIVE"
            row["live_net_pnl"] = _get(live_row, "net_pnl")
            row["sim_net_pnl"] = _get(sim_row, "net_pnl")
            row["pnl_diff"] = None
            rows.append(row)
            continue

        # Core metrics
        for col in ["net_pnl", "closed_pnl", "commission", "shs_traded",
                     "times_traded", "times_flipped"]:
            row[f"live_{col}"] = _get(live_row, col)
            row[f"sim_{col}"] = _get(sim_row, col)
            row[f"diff_{col}"] = _safe_diff(_get(live_row, col), _get(sim_row, col))

        # Fill rates
        for col in ["fill_rate", "reachable_fill_rate", "ioc_fill_rate"]:
            row[f"live_{col}"] = _get(live_row, col)
            row[f"sim_{col}"] = _get(sim_row, col)

        # PnL diff percentage
        sim_pnl = _get(sim_row, "net_pnl")
        live_pnl = _get(live_row, "net_pnl")
        row["pnl_diff"] = _safe_diff(live_pnl, sim_pnl)
        row["pnl_diff_pct"] = _safe_pct_diff(live_pnl, sim_pnl)

        # Log event flags
        has_um = False
        has_inh = False
        if log_events:
            has_um = has_event(log_events, alias, group, strat, live_date, "usermsg")
            has_inh = has_event(log_events, alias, group, strat, live_date, "inherit")
        row["has_usermsg"] = has_um
        row["has_inherit"] = has_inh

        if has_um or has_inh:
            row["status"] = "FLAGGED"
        else:
            row["status"] = "OK"

        rows.append(row)

    return rows


def build_comparison_report(comparison_rows, dates, cancel_fill_data=None,
                            rtt_data=None):
    """Build full comparison report from comparison row dicts.

    Returns (report_text, comparison_df).
    """
    if not comparison_rows:
        return "No comparison data.\n", pd.DataFrame()

    df = pd.DataFrame(comparison_rows)
    out = io.StringIO()

    out.write("=" * 80 + "\n")
    out.write("SIM vs LIVE EVALUATION REPORT\n")
    out.write("=" * 80 + "\n\n")

    # Day-by-day
    _write_daily_report(df, dates, out, cancel_fill_data=cancel_fill_data)

    # Aggregate
    if len(dates) > 1:
        _write_aggregate_report(df, out)

    # Flagged events
    _write_flagged(df, out)

    # Cancel fill rate
    if cancel_fill_data:
        _write_cancel_fill_section(cancel_fill_data, dates, out)

    # Live RTT percentiles
    if rtt_data:
        _write_rtt_section(rtt_data, dates, out)

    return out.getvalue(), df


def _write_daily_report(df, dates, out, cancel_fill_data=None):
    """Per-date comparison table, grouped by strat."""
    for ds in dates:
        day_df = df[df["date"] == ds]
        if day_df.empty:
            continue

        out.write(f"\n{'─' * 80}\n")
        out.write(f"  {ds}\n")
        out.write(f"{'─' * 80}\n")

        # Group by strat
        for (alias, group, strat), strat_df in day_df.groupby(["alias", "group", "strat"]):
            out.write(f"\n  [{alias}] {group}/{strat}\n")
            hdr = f"    {'sym':<15} {'live_pnl':>9} {'sim_pnl':>9} {'diff':>9} {'diff%':>7} {'L_fr':>5} {'S_fr':>5} {'L_trd':>6} {'S_trd':>6} {'st':>7}"
            out.write(hdr + "\n")
            out.write("    " + "-" * 74 + "\n")

            strat_live_total = 0
            strat_sim_total = 0

            for _, row in strat_df.iterrows():
                sym = str(row["sym"])
                if sym.startswith("xyz:"):
                    sym = sym[4:]

                live_pnl = row.get("live_net_pnl")
                sim_pnl = row.get("sim_net_pnl")
                if not _is_missing(live_pnl):
                    strat_live_total += live_pnl
                if not _is_missing(sim_pnl):
                    strat_sim_total += sim_pnl

                out.write(f"    {sym:<15} "
                          f"{_fmt_num(live_pnl):>9} "
                          f"{_fmt_num(sim_pnl):>9} "
                          f"{_fmt_num(row.get('pnl_diff')):>9} "
                          f"{_fmt_pct(row.get('pnl_diff_pct')):>7} "
                          f"{_fmt_rate(row.get('live_fill_rate')):>5} "
                          f"{_fmt_rate(row.get('sim_fill_rate')):>5} "
                          f"{_fmt_int(row.get('live_times_traded')):>6} "
                          f"{_fmt_int(row.get('sim_times_traded')):>6} "
                          f"{row.get('status', ''):>7}\n")

            # Aggregate cancel fill rate for this strat/date
            cfr_suffix = ""
            if cancel_fill_data:
                key = (alias, group, strat, ds)
                cfd = cancel_fill_data.get(key)
                if cfd:
                    live_cfr = cfd.get("live") or {}
                    sim_cfr = cfd.get("sim") or {}
                    l_cxl = sum(s.get("cancels", 0) for s in live_cfr.values())
                    l_cf = sum(s.get("cancel_fills", 0) for s in live_cfr.values())
                    s_cxl = sum(s.get("cancels", 0) for s in sim_cfr.values())
                    s_cf = sum(s.get("cancel_fills", 0) for s in sim_cfr.values())
                    l_pct = l_cf / l_cxl * 100 if l_cxl > 0 else 0
                    s_pct = s_cf / s_cxl * 100 if s_cxl > 0 else 0
                    cfr_suffix = f"  L_cfr={l_pct:.2f}%  S_cfr={s_pct:.2f}%"

            diff_total = strat_live_total - strat_sim_total
            out.write(f"    {'TOTAL':<15} {strat_live_total:>9,.2f} {strat_sim_total:>9,.2f} {diff_total:>9,.2f}{cfr_suffix}\n")

    out.write("\n")


def _write_aggregate_report(df, out):
    """Aggregate stats across all dates."""
    out.write("=" * 80 + "\n")
    out.write("AGGREGATE (all dates)\n")
    out.write("=" * 80 + "\n\n")

    valid = df[df["status"].isin(["OK", "FLAGGED"])].copy()
    if valid.empty:
        out.write("  No valid comparison rows.\n\n")
        return

    agg = valid.groupby(["alias", "group", "strat"]).agg(
        live_pnl=("live_net_pnl", "sum"),
        sim_pnl=("sim_net_pnl", "sum"),
        diff=("pnl_diff", "sum"),
        live_trades=("live_times_traded", "sum"),
        sim_trades=("sim_times_traded", "sum"),
        n_days=("date", "nunique"),
    ).reset_index()

    for _, r in agg.iterrows():
        label = f"[{r['alias']}] {r['group']}/{r['strat']}"
        out.write(f"  {label}  ({int(r['n_days'])}d)\n")
        out.write(f"    live={r['live_pnl']:>10,.2f}  sim={r['sim_pnl']:>10,.2f}  "
                  f"diff={r['diff']:>10,.2f}  "
                  f"trades: {int(r['live_trades'])}L / {int(r['sim_trades'])}S\n")

    out.write(f"\n  TOTAL: live={agg['live_pnl'].sum():,.2f}  sim={agg['sim_pnl'].sum():,.2f}  diff={agg['diff'].sum():,.2f}\n\n")


def _write_flagged(df, out):
    """Report days/strats with usermsg or position inheritance events."""
    flagged = df[df["status"] == "FLAGGED"]
    if flagged.empty:
        return

    out.write("=" * 80 + "\n")
    out.write("FLAGGED (unmodeled events detected in logs)\n")
    out.write("=" * 80 + "\n\n")

    # Group by strat to avoid repetitive output
    for (alias, group, strat), grp in flagged.groupby(["alias", "group", "strat"]):
        dates_flagged = sorted(grp["date"].unique())
        flags = set()
        if grp["has_usermsg"].any():
            flags.add("usermsg")
        if grp["has_inherit"].any():
            flags.add("inherit")
        out.write(f"  [{alias}] {group}/{strat}  dates={','.join(dates_flagged)}  {', '.join(sorted(flags))}\n")

    out.write("\n")


# ── Live RTT percentiles ─────────────────────────────────────────────────────

def compute_order_rtt(path):
    """Parse orders file and compute NewOrd→Ack and Cancel→Ack RTT per symbol.

    Returns {"ord_rtt": {sym: [ms, ...]}, "cxl_rtt": {sym: [ms, ...]}} or None.
    """
    if not path or not os.path.exists(path):
        return None

    new_ords = {}   # oid -> (epoch_ms, sym)
    cancels = {}    # oid -> (epoch_ms, sym)
    ord_rtt = defaultdict(list)
    cxl_rtt = defaultdict(list)

    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    epoch_ms = int(parts[1])
                except ValueError:
                    continue
                etype = parts[2]

                if etype == "NewOrd" and len(parts) >= 5:
                    new_ords[parts[3]] = (epoch_ms, parts[4])
                elif etype == "NewOrdAck" and len(parts) >= 5:
                    oid, sym = parts[3], parts[4]
                    if oid in new_ords:
                        rtt = epoch_ms - new_ords[oid][0]
                        if 0 <= rtt < 60000:
                            ord_rtt[sym].append(rtt)
                elif etype == "Cancel" and len(parts) >= 5:
                    cancels[parts[3]] = (epoch_ms, parts[4])
                elif etype == "CancelAck" and len(parts) >= 5:
                    oid, sym = parts[3], parts[4]
                    if oid in cancels:
                        rtt = epoch_ms - cancels[oid][0]
                        if 0 <= rtt < 60000:
                            cxl_rtt[sym].append(rtt)
    except OSError:
        return None

    return {"ord_rtt": dict(ord_rtt), "cxl_rtt": dict(cxl_rtt)}


def _write_rtt_section(rtt_data, dates, out):
    """Write live RTT percentiles section."""
    if not rtt_data:
        return

    out.write("=" * 80 + "\n")
    out.write("LIVE ORDER RTT (ms)\n")
    out.write("=" * 80 + "\n")

    by_strat = defaultdict(dict)
    for (alias, group, strat, ds), data in rtt_data.items():
        by_strat[(alias, group, strat)][ds] = data

    for key in sorted(by_strat):
        alias, group, strat = key
        dates_data = by_strat[key]

        # Aggregate all RTTs across dates for this strat
        all_ord = []
        all_cxl = []
        for ds in sorted(dates_data):
            data = dates_data[ds]
            if data:
                for vals in data["ord_rtt"].values():
                    all_ord.extend(vals)
                for vals in data["cxl_rtt"].values():
                    all_cxl.extend(vals)

        if not all_ord and not all_cxl:
            continue

        out.write(f"\n  [{alias}] {group}/{strat}  ({len(dates_data)}d)\n")
        hdr = f"    {'type':<12} {'count':>6} {'p50':>5} {'p75':>5} {'p90':>5} {'p95':>5} {'p99':>5} {'max':>5}"
        out.write(hdr + "\n")
        out.write("    " + "-" * 55 + "\n")

        if all_ord:
            p = _rtt_pctiles(all_ord)
            out.write(f"    {'NewOrd→Ack':<12} {p['n']:6d} {p['p50']:5d} {p['p75']:5d} "
                      f"{p['p90']:5d} {p['p95']:5d} {p['p99']:5d} {p['max']:5d}\n")
        if all_cxl:
            p = _rtt_pctiles(all_cxl)
            out.write(f"    {'Cancel→Ack':<12} {p['n']:6d} {p['p50']:5d} {p['p75']:5d} "
                      f"{p['p90']:5d} {p['p95']:5d} {p['p99']:5d} {p['max']:5d}\n")

    out.write("\n")


def _rtt_pctiles(vals):
    """Compute RTT percentiles from a list of ms values."""
    sv = sorted(vals)
    n = len(sv)
    return {
        "n": n,
        "p50": sv[int(n * 0.50)],
        "p75": sv[int(n * 0.75)],
        "p90": sv[int(n * 0.90)],
        "p95": sv[min(int(n * 0.95), n - 1)],
        "p99": sv[min(int(n * 0.99), n - 1)],
        "max": sv[-1],
    }


# ── Cancel fill rate ─────────────────────────────────────────────────────────

def compute_cancel_fills(path):
    """Parse orders file and compute cancel fill stats per symbol.

    A 'cancel fill' is when Cancel was sent for an order but Exec arrived
    after the cancel (filled while cancel in flight).

    Returns {sym: {"cancels": int, "cancel_fills": int, "cancel_fill_shs": float}}
    or None if path doesn't exist.
    """
    if not path or not os.path.exists(path):
        return None

    cancel_events = {}   # oid -> (epoch_ms, sym)
    exec_events = {}     # oid -> (epoch_ms, sym, size)

    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    epoch_ms = int(parts[1])
                except ValueError:
                    continue
                etype = parts[2]

                if etype == "Cancel" and len(parts) >= 5:
                    oid = parts[3]
                    sym = parts[4]
                    cancel_events[oid] = (epoch_ms, sym)

                elif etype == "Exec" and len(parts) >= 9:
                    oid = parts[3]
                    sym = parts[4]
                    try:
                        sz = float(parts[8])
                    except (ValueError, IndexError):
                        sz = 0.0
                    exec_events[oid] = (epoch_ms, sym, sz)
    except OSError:
        return None

    sym_stats = defaultdict(lambda: {"cancels": 0, "cancel_fills": 0, "cancel_fill_shs": 0.0})

    for oid, (cxl_ms, sym) in cancel_events.items():
        sym_stats[sym]["cancels"] += 1
        if oid in exec_events:
            exec_ms, _, sz = exec_events[oid]
            if exec_ms >= cxl_ms:
                sym_stats[sym]["cancel_fills"] += 1
                sym_stats[sym]["cancel_fill_shs"] += sz

    return dict(sym_stats)


def _write_cancel_fill_section(cancel_fill_data, dates, out):
    """Write cancel fill rate comparison section (sim vs live)."""
    if not cancel_fill_data:
        return

    out.write("=" * 80 + "\n")
    out.write("CANCEL FILL RATE (sim vs live)\n")
    out.write("=" * 80 + "\n")

    # Group by (alias, group, strat)
    by_strat = defaultdict(dict)
    for (alias, group, strat, ds), data in cancel_fill_data.items():
        by_strat[(alias, group, strat)][ds] = data

    for key in sorted(by_strat):
        alias, group, strat = key
        dates_data = by_strat[key]

        for ds in sorted(dates_data):
            data = dates_data[ds]
            live_cfr = data.get("live")
            sim_cfr = data.get("sim")

            if not live_cfr and not sim_cfr:
                continue

            out.write(f"\n  [{alias}] {group}/{strat}  {ds}\n")
            hdr = (f"    {'sym':<12} {'L_cxl':>6} {'L_cfill':>7} {'L_cfr%':>7}"
                   f"   {'S_cxl':>6} {'S_cfill':>7} {'S_cfr%':>7}")
            out.write(hdr + "\n")
            out.write("    " + "-" * 68 + "\n")

            all_syms = set()
            if live_cfr:
                all_syms.update(live_cfr.keys())
            if sim_cfr:
                all_syms.update(sim_cfr.keys())

            totals = {"live_cxl": 0, "live_cf": 0, "sim_cxl": 0, "sim_cf": 0}

            for sym in sorted(all_syms):
                display_sym = sym.replace("xyz:", "")

                lc = live_cfr.get(sym, {}) if live_cfr else {}
                sc = sim_cfr.get(sym, {}) if sim_cfr else {}

                l_cxl = lc.get("cancels", 0)
                l_cf = lc.get("cancel_fills", 0)
                l_pct = l_cf / l_cxl * 100 if l_cxl > 0 else 0

                s_cxl = sc.get("cancels", 0)
                s_cf = sc.get("cancel_fills", 0)
                s_pct = s_cf / s_cxl * 100 if s_cxl > 0 else 0

                totals["live_cxl"] += l_cxl
                totals["live_cf"] += l_cf
                totals["sim_cxl"] += s_cxl
                totals["sim_cf"] += s_cf

                out.write(f"    {display_sym:<12} {l_cxl:6d} {l_cf:7d} {l_pct:6.2f}%"
                          f"   {s_cxl:6d} {s_cf:7d} {s_pct:6.2f}%\n")

            # Totals
            lt = totals["live_cxl"]
            lf = totals["live_cf"]
            lpct = lf / lt * 100 if lt > 0 else 0
            st = totals["sim_cxl"]
            sf = totals["sim_cf"]
            spct = sf / st * 100 if st > 0 else 0

            out.write(f"    {'TOTAL':<12} {lt:6d} {lf:7d} {lpct:6.2f}%"
                      f"   {st:6d} {sf:7d} {spct:6.2f}%\n")

    out.write("\n")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get(row, col):
    if row is None:
        return None
    try:
        v = row[col]
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (KeyError, TypeError, ValueError):
        return None


def _safe_diff(a, b):
    if a is not None and b is not None:
        return a - b
    return None


def _safe_pct_diff(a, b):
    if a is not None and b is not None and b != 0:
        return (a - b) / abs(b) * 100
    return None


def _is_missing(v):
    if v is None:
        return True
    try:
        return pd.isna(v)
    except (TypeError, ValueError):
        return False


def _fmt_num(v):
    if _is_missing(v):
        return "N/A"
    return f"{v:,.2f}"


def _fmt_pct(v):
    if _is_missing(v):
        return "N/A"
    return f"{v:+.1f}%"


def _fmt_rate(v):
    if _is_missing(v):
        return "—"
    return f"{v:.2f}"


def _fmt_int(v):
    if _is_missing(v):
        return "N/A"
    return str(int(v))
