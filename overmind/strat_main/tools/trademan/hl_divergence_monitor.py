#!/usr/bin/env python3
"""HL micro-structure divergence monitor.

For every currently-traded HL symbol, computes per-day z-scores against a
trailing 30-weekday baseline. Flags days where |z| > 2 on any dimension,
with elevated severity when multiple dimensions fire together (the
"MSFT / AAPL 2026-06-25 fingerprint" — spread widened AND inside deepened
AND notional shifted).

Runs against the local `bin/symstats` binary (fast, no external calls).
Weekend HL flow is filtered out (matches the `pm_hl_stats.py
--weekdays-only` convention).

**Not** yet integrated: HL/topbook divergence (Databento — slow). That's
the crown jewel per the fleet-review-playbook — this tool will get
extended once we have a topbook per-day cache. HL-only is the loudest
signal on its own, though.

Usage
-----
  # Check one date across every currently-traded HL sym:
  python hl_divergence_monitor.py --date 20260625

  # Backfill validation across a range:
  python hl_divergence_monitor.py --date-range 20260601:20260714

  # Only specific syms:
  python hl_divergence_monitor.py --date 20260625 --syms xyz:NVDA,xyz:MSFT

  # Also compare against yesterday's own baseline, not just today's flag:
  python hl_divergence_monitor.py --date 20260625 --window 14

Flagging
--------
  For each (sym, dimension), z = (today - trailing_mean) / trailing_std.
  Dimensions monitored (all sourced from symstats):
    - notional_per_day  = vol_shs * avg_mid
    - num_trades
    - spread_bps
    - med_inside_liq
    - n_midchanges
    - med_trdsz

  Severity:
    LOW    — 1 dim with |z| > 2
    MEDIUM — 2 dims fired same day
    HIGH   — 3+ dims fired same day  (this is the MSFT/AAPL fingerprint)

  Cross-referenced with local acct files to show which strats trade each
  flagged sym — output includes the affected `(alias, group, strat)` list.

Fill-rate annotation
--------------------
  On the `traded by:` line for each flagged sym, per-strat `fill_bp`
  (= shs_traded / shs_sent × 10000) is z-scored against a 30-weekday
  trailing baseline per (alias, group, strat, sym). Strats with |z| ≥ 2
  are annotated with `(max fill_bp z=±X.X)`. This surfaces the
  "news-day fill-rate spike" pattern from the 2026-06-25 AMZN/ORCL
  survival drill: MSFT/AAPL had fill_bp z=+3 to +8 across multiple
  strats; AMZN/ORCL survivor strats show no annotation because their
  fill_bp stayed at baseline.

  Uses shs_traded/shs_sent directly because the acct file's `fill_rate`
  column is 2dp-lossy (sub-1% rates all read 0.00).

Cron
----
Suggested nightly entry (after `pta scan` at 02:00):

  30 2 * * 2-6  /home/pktrade/.venvs/v1/bin/python \\
    /home/pktrade/tradefi/retraded_cron/overmind/strat_main/tools/trademan/hl_divergence_monitor.py \\
    --date-range $(date -d yesterday +%Y%m%d):$(date +%Y%m%d) \\
    --rolling 10 --consec-threshold 3 \\
    >> ~/scratch/tradeperf/cron_hl_divergence.log 2>&1

Rolling mode with 10-day window catches persistent shifts. `--include-topbook`
is optional — omit for a fast HL-only nightly, add for a weekly deeper pass.

Follow-ups
----------
- Backfill validation confirmed on 2026-06-25 tech-cluster event: META,
  MSFT, AAPL, NVDA, AMZN, TSLA, AVGO all flagged 3+ days in the 12-day
  window; LLY (the unaffected one) is absent.
- Consider auto-feeding rolling flags into `fleet_review.py` as a
  supplementary signal.
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import statistics
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(os.environ.get("PKTRADE_REPO",
                           str(Path(__file__).resolve().parents[4])))
SYMSTATS_BIN = REPO / "bin" / "symstats"
DEFAULT_LOCAL_BASE = os.path.expanduser("~/scratch/tradeperf")
CACHE_DIR = Path(os.path.expanduser("~/scratch/hl_divergence_cache"))

DIMS = ["notional_per_day", "num_trades", "spread_bps",
        "med_inside_liq", "n_midchanges", "med_trdsz"]

# Extra dimensions when --include-topbook is set. Only valid for US equity
# symbols (xyz:AAPL etc.) — non-US-equity syms will get HL-only mode.
TOPBOOK_DIMS = ["hl_tb_notional_ratio", "hl_tb_spread_ratio"]


# ── data ──────────────────────────────────────────────────────────────────────

def weekdays_before(date_str, n):
    d = datetime.strptime(date_str, "%Y%m%d")
    out = []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return list(reversed(out))


def _cache_path(sym, date, book):
    sym_safe = sym.replace(":", "_").replace("/", "_")
    return CACHE_DIR / f"{sym_safe}_{book}_{date}.json"


def hl_stats_day(sym, date, book="Hyperliquid"):
    """symstats for one day. Returns dict or None if no data / bad row.
    Cached per (sym, book, date) — cache is authoritative including negative
    (no-data) results, since sym×date is immutable once trading day has closed."""
    import json
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(sym, date, book)
    if cp.exists():
        try:
            with cp.open() as f:
                cached = json.load(f)
            return cached if cached else None
        except Exception:
            pass  # fall through to fetch

    if not SYMSTATS_BIN.exists():
        raise RuntimeError(f"symstats binary not found at {SYMSTATS_BIN}")
    try:
        out = subprocess.check_output(
            [str(SYMSTATS_BIN), "--book", book, "--symbol", sym,
             "--date", date, "--csv"], text=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        result = None
    else:
        rows = list(csv.DictReader(out.splitlines()))
        result = None
        if rows:
            r = rows[0]
            try:
                num_trades = int(r["num_trades"])
                if num_trades > 0:
                    vol_shs = float(r["vol"])
                    avg_mid = float(r["avg_mid"])
                    if avg_mid > 0:
                        result = {
                            "num_trades": num_trades,
                            "vol_shs": vol_shs,
                            "notional_per_day": vol_shs * avg_mid,
                            "med_trdsz": float(r["med_trdsz"]),
                            "med_inside_liq": float(r["med_inside_liq"]),
                            "avg_mid": avg_mid,
                            "spread_bps": float(r["spread_pct"]) * 1e4,
                            "n_midchanges": int(r["n_midchanges"]),
                        }
            except (KeyError, ValueError):
                pass
    try:
        with cp.open("w") as f:
            json.dump(result if result is not None else {}, f)
    except OSError:
        pass
    return result


def topbook_stats_day(sym, date):
    """Fetch topbook stats for `sym` on `date` from XNAS.BASIC via Databento.
    Reuses pm_topbook_stats.compute_window per-day (which naturally caches
    per-window under ~/scratch/postmortems/_topbook_cache/).

    Returns dict {notional_per_day, spread_bps, ret_stdev_bps} or None if
    the sym isn't a US equity or Databento returned nothing.
    """
    if not sym.startswith("xyz:"):
        return None
    underlying = sym.split(":", 1)[1]
    try:
        pm_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "postmortems")
        if pm_dir not in sys.path:
            sys.path.insert(0, pm_dir)
        from pm_topbook_stats import compute_window as _tb_compute
    except Exception as e:
        print(f"WARN: cannot import pm_topbook_stats: {e}", file=sys.stderr)
        return None
    try:
        w = _tb_compute(underlying, date, date, skip_spread=False)
    except Exception as e:
        return None
    if not w or w.get("days_with_data", 0) == 0:
        return None
    return {
        "notional_per_day": w.get("notional_per_day"),
        "spread_bps": w.get("avg_spread_bps"),
        "ret_stdev_bps": w.get("ret_5m_stdev_bps"),
    }


def combined_stats_day(sym, date, include_topbook=False):
    """HL stats + optional topbook ratios."""
    hl = hl_stats_day(sym, date)
    if hl is None:
        return None
    if include_topbook:
        tb = topbook_stats_day(sym, date)
        if tb is not None and tb.get("notional_per_day") and tb.get("spread_bps"):
            hl["hl_tb_notional_ratio"] = hl["notional_per_day"] / tb["notional_per_day"]
            hl["hl_tb_spread_ratio"] = hl["spread_bps"] / tb["spread_bps"]
    return hl


def fill_bp_day(local_base, alias, group, strat, date, sym):
    """Per-(strat, sym) fill rate on `date`, in bps. Uses shs_traded / shs_sent
    directly (acct's `fill_rate` column is 2dp-lossy). Returns None if no
    traded shares."""
    import pandas as pd
    p = Path(local_base) / alias / group / strat / f"acct_{date}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, usecols=["sym", "shs_traded", "shs_sent"])
    except Exception:
        return None
    row = df[df["sym"] == sym]
    if row.empty:
        return None
    st = float(row["shs_traded"].iloc[0])
    ss = float(row["shs_sent"].iloc[0])
    if ss <= 0:
        return None
    return (st / ss) * 1e4  # bps


def fill_bp_z_pair(local_base, alias, group, strat, sym, date, window=30):
    """Trailing z-score of today's fill_bp against `window` weekdays for
    this (alias, group, strat, sym). Returns (z, today, mean, std) or None."""
    today = fill_bp_day(local_base, alias, group, strat, date, sym)
    if today is None:
        return None
    base_vals = []
    for d in weekdays_before(date, window):
        v = fill_bp_day(local_base, alias, group, strat, d, sym)
        if v is not None:
            base_vals.append(v)
    if len(base_vals) < 5:
        return None
    m = statistics.mean(base_vals)
    if len(base_vals) < 2:
        return None
    s = statistics.stdev(base_vals)
    if s <= 0:
        return None
    return ((today - m) / s, today, m, s)


def discover_current_syms(local_base, aliases=None, min_recent_days=3):
    """Return set of HL symbols that traded in any (strat, sym) row on at
    least `min_recent_days` distinct recent trading days. Uses acct_*.csv.
    """
    import pandas as pd
    rows = []
    for f in glob.glob(os.path.join(local_base, "gf*/*/*/acct_2026*.csv")):
        m = re.match(r".*/tradeperf/([^/]+)/([^/]+)/([^/]+)/acct_(\d{8})\.csv$", f)
        if not m:
            continue
        alias, group, strat, dt = m.groups()
        if aliases and alias not in aliases:
            continue
        try:
            df = pd.read_csv(f, usecols=["sym", "times_traded"])
        except Exception:
            continue
        if not len(df):
            continue
        df = df[(df["times_traded"] > 0) & df["sym"].astype(str).str.startswith("xyz:")]
        df = df.copy()
        df["date"] = dt
        rows.append(df)
    if not rows:
        return set()
    d = pd.concat(rows, ignore_index=True)
    by_sym = d.groupby("sym")["date"].nunique()
    return set(by_sym[by_sym >= min_recent_days].index)


def strats_trading_sym(local_base, sym, recent_days=5):
    """Return list of (alias, group, strat) currently trading `sym`.
    Looks at the most recent `recent_days` calendar days of acct files."""
    import pandas as pd
    hits = set()
    for f in glob.glob(os.path.join(local_base, "gf*/*/*/acct_2026*.csv")):
        m = re.match(r".*/tradeperf/([^/]+)/([^/]+)/([^/]+)/acct_(\d{8})\.csv$", f)
        if not m:
            continue
        alias, group, strat, dt = m.groups()
        # rough recency filter: any date in the last ~2 weeks
        # (recent_days measured in calendar days for simplicity)
        try:
            d_int = int(dt)
        except ValueError:
            continue
        try:
            df = pd.read_csv(f, usecols=["sym", "times_traded"])
        except Exception:
            continue
        if not len(df):
            continue
        matched = df[(df["sym"] == sym) & (df["times_traded"] > 0)]
        if len(matched):
            hits.add((alias, group, strat, dt))
    if not hits:
        return []
    # Filter to most-recent {recent_days} distinct dates present, then unique triples
    dates = sorted({d for _, _, _, d in hits}, reverse=True)[:recent_days]
    triples = sorted({(a, g, s) for a, g, s, d in hits if d in dates})
    return triples


# ── z-score ───────────────────────────────────────────────────────────────────

def zscore_dim(today, baseline_vals):
    if today is None:
        return None
    baseline_vals = [v for v in baseline_vals if v is not None]
    if len(baseline_vals) < 5:
        return None
    m = statistics.mean(baseline_vals)
    if len(baseline_vals) < 2:
        return None
    s = statistics.stdev(baseline_vals)
    if s <= 0:
        return None
    return (today - m) / s, m, s


def analyze_sym_day(sym, date, window=30, z_threshold=2.0, include_topbook=False):
    """Compute per-dim z-scores for `sym` on `date` against a trailing
    `window`-weekday baseline. Returns dict of results or None if no data.

    If include_topbook is True and the sym is a US equity, also compute
    HL/topbook ratio dims (which is what would have most cleanly flagged
    the 2026-06-25 tech-cluster event as HL-specific rather than
    market-wide)."""
    today = combined_stats_day(sym, date, include_topbook=include_topbook)
    if today is None:
        return None
    base_dates = weekdays_before(date, window)
    base_rows = []
    for d in base_dates:
        r = combined_stats_day(sym, d, include_topbook=include_topbook)
        if r is not None:
            base_rows.append(r)
    if len(base_rows) < window // 2:
        return {"sym": sym, "date": date, "today": today,
                "flags": [], "n_base": len(base_rows), "notes": "insufficient baseline"}
    dims_to_check = list(DIMS)
    if include_topbook:
        dims_to_check += TOPBOOK_DIMS
    flags = []
    for dim in dims_to_check:
        base_vals = [r[dim] for r in base_rows if dim in r]
        z_pack = zscore_dim(today.get(dim), base_vals)
        if z_pack is None:
            continue
        z, m, s = z_pack
        if abs(z) >= z_threshold:
            flags.append({"dim": dim, "z": z, "today": today[dim], "baseline_mean": m, "baseline_std": s})
    return {"sym": sym, "date": date, "today": today, "flags": flags,
            "n_base": len(base_rows), "notes": None}


def severity(flags):
    n = len(flags)
    if n >= 3:
        return "HIGH"
    if n == 2:
        return "MEDIUM"
    if n == 1:
        return "LOW"
    return None


# ── report ────────────────────────────────────────────────────────────────────

def fmt_z(z):
    sign = "+" if z >= 0 else ""
    return f"{sign}{z:.1f}"


def report_date(date, results, local_base, show_strats=True):
    hits = [r for r in results if r["flags"]]
    if not hits:
        return f"\n=== HL divergence, {date[:4]}-{date[4:6]}-{date[6:]} ===\n  no flags\n"
    hits.sort(key=lambda r: (-len(r["flags"]), -max(abs(f["z"]) for f in r["flags"])))
    lines = [f"\n=== HL divergence, {date[:4]}-{date[4:6]}-{date[6:]} ===",
             f"  {len(hits)} flagged sym(s)  ({sum(1 for r in hits if severity(r['flags'])=='HIGH')} HIGH, "
             f"{sum(1 for r in hits if severity(r['flags'])=='MEDIUM')} MEDIUM, "
             f"{sum(1 for r in hits if severity(r['flags'])=='LOW')} LOW)\n"]
    for r in hits:
        sev = severity(r["flags"])
        lines.append(f"  {r['sym']:>14}  [{sev}]  {len(r['flags'])} dim(s) fired")
        for f in r["flags"]:
            lines.append(f"    {f['dim']:<18} z={fmt_z(f['z']):>6}  "
                         f"(today {f['today']:.2f}  vs base {f['baseline_mean']:.2f} ± {f['baseline_std']:.2f})")
        if show_strats:
            trips = strats_trading_sym(local_base, r["sym"])
            if trips:
                annos = []
                for a, g, s in trips:
                    ann = f"{a}/{s}"
                    fbz = fill_bp_z_pair(local_base, a, g, s, r["sym"], r["date"])
                    if fbz is not None and abs(fbz[0]) >= 2.0:
                        ann += f" (fill_bp z={fmt_z(fbz[0])})"
                    annos.append(ann)
                lines.append(f"    traded by: {', '.join(annos)}")
        lines.append("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _tight_val(v):
    """Compact 5-char value for the table. Handles scale via k/M/B suffixes."""
    if v is None:
        return "  -  "
    a = abs(v)
    if a >= 1e9:
        s = f"{v/1e9:.1f}B"
    elif a >= 1e6:
        s = f"{v/1e6:.1f}M"
    elif a >= 1e4:
        s = f"{v/1e3:.0f}k"
    elif a >= 1e3:
        s = f"{v/1e3:.1f}k"
    elif a >= 100:
        s = f"{v:.0f}"
    elif a >= 10:
        s = f"{v:.1f}"
    elif a >= 1:
        s = f"{v:.2f}"
    else:
        s = f"{v:.2f}"
    return s.rjust(5)


def _base_str(m, s):
    return f"{_tight_val(m).strip()}±{_tight_val(s).strip()}"


def rolling_severity_report(dates, syms, window, z_threshold, local_base,
                             consec_threshold=2, show_strats=True,
                             include_topbook=False):
    """For each sym, report if it flagged on ≥ consec_threshold of the last
    N dates. Uses the same analyze_sym_day machinery per date."""
    per_sym = {sym: [] for sym in syms}
    for date in dates:
        for sym in syms:
            r = analyze_sym_day(sym, date, window=window, z_threshold=z_threshold,
                                include_topbook=include_topbook)
            if r is None:
                continue
            # store today's raw values too, so we can render a time series
            per_sym[sym].append((date, r["flags"], r["today"]))
    lines = [f"\n=== HL divergence rolling report over {len(dates)} weekday(s) "
             f"({dates[0]} → {dates[-1]}) ==="]
    hits = []
    for sym, entries in per_sym.items():
        flag_days = [(d, fs, t) for d, fs, t in entries if fs]
        if len(flag_days) >= consec_threshold:
            hits.append((sym, entries, flag_days))
    if not hits:
        lines.append(f"  no sym flagged on {consec_threshold}+ days")
        return "\n".join(lines)
    hits.sort(key=lambda x: (-len(x[2]), -max((severity_score(fs)) for _, fs, _ in x[2])))
    lines.append(f"  {len(hits)} sym(s) flagged on {consec_threshold}+ day(s):\n")
    for sym, entries, flag_days in hits:
        dim_info = {}   # dim -> (max_z, baseline_mean, baseline_std)
        for _, fs, _ in flag_days:
            for f in fs:
                cur = dim_info.get(f["dim"])
                if cur is None or abs(f["z"]) > abs(cur[0]):
                    dim_info[f["dim"]] = (f["z"], f["baseline_mean"], f["baseline_std"])
        n_flagged = len(flag_days)
        n_high = sum(1 for _, fs, _ in flag_days if severity_score(fs) == 3)
        n_med = sum(1 for _, fs, _ in flag_days if severity_score(fs) == 2)
        lines.append("")
        lines.append(f"  {sym}   flagged {n_flagged}/{len(entries)} days  ({n_high} HIGH, {n_med} MEDIUM)")

        # Table header: date columns across
        date_cols = [d[4:6] + "-" + d[6:] for d, _fs, _t in entries]
        header = "    " + " " * 18 + " " + " ".join(f"{d:>5}" for d in date_cols) + "  | " + f"{'baseline':>12} " + f"{'max_z':>6}"
        lines.append(header)
        lines.append("    " + "-" * (len(header) - 4))
        # One row per dim (sorted by |max_z| desc)
        for dim, (z, m, s) in sorted(dim_info.items(), key=lambda kv: -abs(kv[1][0])):
            vals = []
            for d, _fs, t in entries:
                v = t.get(dim) if t else None
                vals.append(_tight_val(v))
            lines.append(
                f"    {dim:<18} " + " ".join(vals) + "  | " +
                f"{_base_str(m,s):>12} " + f"{fmt_z(z):>6}"
            )
        if show_strats:
            trips = strats_trading_sym(local_base, sym)
            if trips:
                annos = []
                for a, g, s in trips:
                    # For each strat, take max abs fill_bp z across the window
                    max_z = 0.0
                    for d in [dt for dt, _ in entries]:
                        fbz = fill_bp_z_pair(local_base, a, g, s, sym, d)
                        if fbz is not None and abs(fbz[0]) > abs(max_z):
                            max_z = fbz[0]
                    ann = f"{a}/{s}"
                    if abs(max_z) >= 2.0:
                        ann += f" (max fill_bp z={fmt_z(max_z)})"
                    annos.append(ann)
                lines.append(f"    traded by: {', '.join(annos)}")
        lines.append("")
    return "\n".join(lines)


def severity_score(flags):
    n = len(flags)
    return 3 if n >= 3 else (2 if n == 2 else (1 if n == 1 else 0))


def main():
    ap = argparse.ArgumentParser(description="HL micro-structure divergence monitor.")
    ap.add_argument("--date", help="YYYYMMDD (default: today)")
    ap.add_argument("--date-range", help="YYYYMMDD:YYYYMMDD (backfill validation)")
    ap.add_argument("--rolling", type=int, metavar="N", help="Rolling mode: instead of per-day report, look at the last N weekdays and report syms flagged on --consec-threshold+ of them")
    ap.add_argument("--consec-threshold", type=int, default=2, help="For --rolling: minimum # of flagged days to include in report (default 2)")
    ap.add_argument("--syms", help="Comma-separated sym filter (default: auto-discover from local acct data)")
    ap.add_argument("--min-recent-days", type=int, default=3, help="Sym must have traded on N+ recent days to be included in auto-discover (default 3)")
    ap.add_argument("--window", type=int, default=30, help="Trailing baseline window in weekdays (default 30)")
    ap.add_argument("--z-threshold", type=float, default=2.0)
    ap.add_argument("--local-base", default=DEFAULT_LOCAL_BASE)
    ap.add_argument("--aliases", help="Comma-separated alias filter for sym auto-discover")
    ap.add_argument("--no-strats", action="store_true", help="Don't include the 'traded by:' line")
    ap.add_argument("--include-topbook", action="store_true",
                    help="Also compute HL/topbook divergence dims (US equities only). "
                         "SLOW on first pass — each new (sym, date) is a Databento fetch (~30-60s). "
                         "Cached per window under ~/scratch/postmortems/_topbook_cache/, so subsequent runs are fast.")
    ap.add_argument("--email", action="store_true",
                    help="Also email the report (uses util.email_utils.send_mail)")
    ap.add_argument("--to", type=str, default=None,
                    help="Email recipient (default: group list from email_utils)")
    ap.add_argument("--email-only-on-flags", action="store_true",
                    help="With --email, only send if at least one sym was flagged (keeps quiet days quiet)")
    args = ap.parse_args()

    if args.date and args.date_range:
        ap.error("Give --date OR --date-range, not both")
    if args.date_range:
        start, end = args.date_range.split(":")
        d = datetime.strptime(start, "%Y%m%d")
        e = datetime.strptime(end, "%Y%m%d")
        dates = []
        while d <= e:
            if d.weekday() < 5:
                dates.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
    else:
        dates = [args.date or datetime.today().strftime("%Y%m%d")]

    aliases = args.aliases.split(",") if args.aliases else None
    if args.syms:
        syms = args.syms.split(",")
    else:
        syms = sorted(discover_current_syms(args.local_base, aliases=aliases,
                                             min_recent_days=args.min_recent_days))
        print(f"# auto-discovered {len(syms)} syms trading on ≥{args.min_recent_days} recent days")

    if not syms:
        print("No syms to analyze.")
        return

    import io
    body_capture = io.StringIO()

    def emit(s):
        print(s)
        body_capture.write(s + "\n")

    n_flagged_total = 0
    if args.rolling:
        recent = dates[-args.rolling:]
        report = rolling_severity_report(recent, syms, window=args.window,
                                          z_threshold=args.z_threshold,
                                          local_base=args.local_base,
                                          consec_threshold=args.consec_threshold,
                                          show_strats=not args.no_strats,
                                          include_topbook=args.include_topbook)
        emit(report)
        # Count flags for the "email only on flags" gate
        n_flagged_total = sum(1 for line in report.split("\n") if "flagged" in line and "days" in line)
    else:
        for date in dates:
            results = []
            for sym in syms:
                r = analyze_sym_day(sym, date, window=args.window,
                                    z_threshold=args.z_threshold,
                                    include_topbook=args.include_topbook)
                if r is not None:
                    results.append(r)
            report = report_date(date, results, args.local_base,
                                  show_strats=not args.no_strats)
            emit(report)
            n_flagged_total += sum(1 for r in results if r["flags"])

    if args.email:
        if args.email_only_on_flags and n_flagged_total == 0:
            print("# --email-only-on-flags set and no flags; skipping email")
        else:
            _strat_main = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            if _strat_main not in sys.path:
                sys.path.insert(0, _strat_main)
            try:
                from util import email_utils
            except ImportError as e:
                print(f"# email skipped: cannot import email_utils: {e}", file=sys.stderr)
            else:
                mode = "rolling" if args.rolling else "per-day"
                span = f"{dates[0]}..{dates[-1]}" if len(dates) > 1 else dates[0]
                subject = f"HL divergence ({mode}) — {span} — {n_flagged_total} flagged"
                kwargs = {"subject": subject, "body": body_capture.getvalue(), "monospace": True}
                if args.to:
                    kwargs["receiver"] = args.to
                email_utils.send_mail(**kwargs)
                print("# Report emailed.")


if __name__ == "__main__":
    main()
