#!/usr/bin/env python
"""Per-day timeline around an inflection date.

For a symbol (and optionally a specific strat), pulls N weekdays before and
after an inflection date, prints a table showing:

  HL micro-structure (via symstats binary):
    num_trades, notional/day, spread_bps, med_inside_liq, n_midchanges,
    med_trdsz, avg_mid

  Strat-side (if --strat given, via local acct files):
    net_pnl, times_traded, times_flipped, shs_traded,
    fill_rate, ioc_fill_rate, min_pnl_seen, open_pos

  Cancel-fill rate (optional, requires orders file):
    cancels_placed, cancels_that_filled, cancel_fill_pct

  Markouts (optional, requires bin/markout):
    avg mo at each --mo-offsets (defaults 1,10,30,60,120 seconds)

Weekends are skipped (both HL and strat data). This is intentional — strats
run weekdays only, and mixing weekend HL flow biases per-day averages (see
`reference_pm_hl_stats_weekdays.md`).

Usage
-----
Sym-only timeline (HL micro-structure only):
  python pm_daily_timeline.py --sym xyz:NVDA --inflection 20260625

With strat-side metrics:
  python pm_daily_timeline.py --sym xyz:NVDA --inflection 20260625 \\
      --alias gf1 --group combined_equities_bfx1 --strat usday

With cancel-fill rate and markouts:
  python pm_daily_timeline.py --sym xyz:NVDA --inflection 20260625 \\
      --alias gf1 --group combined_equities_bfx1 --strat usday \\
      --cancel-fills --markouts

Follow-up ideas
---------------
- `depth_for_$notional`: add a per-day median of the SigLiqBalance-style
  average liquidation price for a fixed notional (e.g. $50k, $500k, $5M)
  as a bps-from-mid cost. Needs adding to the symstats C++ binary.
- Fold this into the fleet_review playbook as the standard per-symbol
  view for kill / postmortem decisions.
"""

import argparse
import csv
import io
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TRADEMAN_DIR = os.path.dirname(_THIS_DIR)
if _TRADEMAN_DIR not in sys.path:
    sys.path.insert(0, _TRADEMAN_DIR)

REPO = Path(os.environ.get("PKTRADE_REPO",
                           str(Path(__file__).resolve().parents[5])))
SYMSTATS_BIN = REPO / "bin" / "symstats"
MARKOUT_BIN = REPO / "bin" / "markout"

DEFAULT_LOCAL_BASE = os.path.expanduser("~/scratch/tradeperf")
DEFAULT_MO_OFFSETS = [1, 10, 30, 60, 120]


# ── date helpers ──────────────────────────────────────────────────────────────

def weekdays_before(date_str: str, n: int):
    d = datetime.strptime(date_str, "%Y%m%d")
    out = []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return list(reversed(out))


def weekdays_from(date_str: str, n: int):
    d = datetime.strptime(date_str, "%Y%m%d")
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


# ── HL micro-structure (symstats) ─────────────────────────────────────────────

def hl_stats_day(sym: str, date: str, book: str = "Hyperliquid"):
    """Returns dict with HL stats for one day, or None if unavailable."""
    if not SYMSTATS_BIN.exists():
        raise RuntimeError(f"symstats binary not found at {SYMSTATS_BIN}")
    try:
        out = subprocess.check_output(
            [str(SYMSTATS_BIN), "--book", book, "--symbol", sym,
             "--date", date, "--csv"], text=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    rows = list(csv.DictReader(out.splitlines()))
    if not rows:
        return None
    r = rows[0]
    try:
        vol_shs = float(r["vol"])
        avg_mid = float(r["avg_mid"])
        return {
            "num_trades": int(r["num_trades"]),
            "vol_shs": vol_shs,
            "notional": vol_shs * avg_mid,
            "med_trdsz": float(r["med_trdsz"]),
            "med_inside_liq": float(r["med_inside_liq"]),
            "avg_mid": avg_mid,
            "spread_bps": float(r["spread_pct"]) * 1e4,
            "n_midchanges": int(r["n_midchanges"]),
        }
    except (KeyError, ValueError):
        return None


# ── Strat-side (acct file) ────────────────────────────────────────────────────

def strat_stats_day(local_base, alias, group, strat, date, sym):
    p = Path(local_base) / alias / group / strat / f"acct_{date}.csv"
    if not p.exists():
        return None
    import pandas as pd
    try:
        df = pd.read_csv(p)
    except Exception:
        return None
    df = df[df["sym"] == sym]
    if df.empty:
        return None
    r = df.iloc[0]
    def n(c):
        try: return float(r[c])
        except (KeyError, ValueError): return 0.0
    # NB: acct file's fill_rate is written at 2dp precision (values < 0.5% all show
    # 0.00). Compute directly from shs_traded / shs_sent for accurate ratio.
    shs_traded = n("shs_traded")
    shs_sent = n("shs_sent")
    fill_rate = (shs_traded / shs_sent) if shs_sent > 0 else 0.0
    return {
        "net_pnl": n("net_pnl"),
        "times_traded": int(n("times_traded")),
        "times_flipped": int(n("times_flipped")),
        "shs_traded": shs_traded,
        "shs_sent": shs_sent,
        "fill_rate": fill_rate,
        "ioc_fill_rate": n("ioc_fill_rate"),  # still from acct — 2dp lossy but same limitation applies
        "min_pnl_seen": n("min_pnl_seen"),
        "open_pos": n("open_pos"),
    }


# ── Cancel-fill rate (from orders file) ───────────────────────────────────────

def cancel_fill_stats_day(local_base, alias, group, strat, date, sym):
    """Return {cancels_placed, cancels_that_filled, cancel_fill_pct} or None.

    Definition: a "cancel-fill" is a Cancel event on a resting order whose
    matching Exec landed AFTER the Cancel was sent but before/instead of a
    CancelAck. This is the "we tried to cancel but got hit anyway" signature.
    """
    p = Path(local_base) / alias / group / strat / f"orders_{date}.csv"
    if not p.exists():
        return None
    # oid -> list of events for that oid in chronological order
    events = {}  # oid -> list of (epoch_ms, event_type, sym)
    try:
        with p.open("r", errors="replace") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 4:
                    continue
                try:
                    epoch_ms = int(parts[1])
                except ValueError:
                    continue
                etype = parts[2]
                # NewOrd/Cancel/Exec/CancelAck all have oid in parts[3]
                if etype in ("NewOrd", "NewOrdAck", "Cancel", "CancelAck", "Exec"):
                    if len(parts) < 5:
                        continue
                    oid = parts[3]
                    esym = parts[4] if etype in ("NewOrd", "NewOrdAck", "Cancel", "CancelAck") else parts[4] if len(parts) > 4 else ""
                    if esym != sym:
                        continue
                    events.setdefault(oid, []).append((epoch_ms, etype))
    except OSError:
        return None
    n_cancels = 0
    n_cancel_fills = 0
    for oid, evs in events.items():
        evs.sort()
        # Find each Cancel and check if an Exec follows before CancelAck
        for i, (t, e) in enumerate(evs):
            if e != "Cancel":
                continue
            n_cancels += 1
            # Look ahead until CancelAck or Exec
            for j in range(i + 1, len(evs)):
                tj, ej = evs[j]
                if ej == "Exec":
                    n_cancel_fills += 1
                    break
                if ej == "CancelAck":
                    break
    pct = (n_cancel_fills / n_cancels * 100) if n_cancels else 0.0
    return {
        "cancels_placed": n_cancels,
        "cancels_that_filled": n_cancel_fills,
        "cancel_fill_pct": pct,
    }


# ── Markouts ──────────────────────────────────────────────────────────────────

def markouts_day(local_base, alias, group, strat, date, sym, offsets, data_dir=None):
    """Run bin/markout on the trades file (or read cached _markout.csv) and
    return {mo_1s: avg_bps, mo_10s: avg_bps, ...} averaged over ADD fills for
    the specified symbol. Returns None if markouts couldn't be produced."""
    if not MARKOUT_BIN.exists():
        return None
    trades_path = Path(local_base) / alias / group / strat / f"trades_{date}.csv"
    if not trades_path.exists():
        return None
    mo_path = Path(str(trades_path).replace(".csv", "_markout.csv"))
    if not mo_path.exists():
        cmd = [str(MARKOUT_BIN), "--trades", str(trades_path), "--date", date,
               "-m", ",".join(str(o) for o in offsets)]
        if data_dir:
            cmd += ["--datadir", data_dir]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return None
        except subprocess.TimeoutExpired:
            return None
        if not mo_path.exists():
            return None
    import pandas as pd
    try:
        try:
            df = pd.read_csv(mo_path)
            headered = "timestamp" in df.columns and "sym" in df.columns
        except Exception:
            headered = False
        if not headered:
            base_cols = ["timestamp", "time_ms", "order_num", "sym", "market", "side",
                         "price", "size", "end_pos", "closed_pnl", "commission",
                         "col11", "bid", "ask", "col14", "mid", "col16",
                         "col17", "col18", "col19", "add_remove", "front_flag"]
            mo_cols = [f"mo_{o}s" for o in offsets]
            df = pd.read_csv(mo_path, header=None, names=base_cols + mo_cols)
    except Exception:
        return None
    df = df[df["sym"] == sym]
    if df.empty:
        return None
    # Split by ADD / REM and pick the side that dominates (or emit both if
    # each has meaningful presence). Cross strats (RelCross et al) are
    # IOC-only → all REM; MM strats (RelWideMM2, WideMM) are mostly ADD.
    # We used to filter ADD-only which zeroed out cross-strat markouts.
    add = df[df["add_remove"] == "ADD"]
    rem = df[df["add_remove"] == "REM"] if "REM" in set(df["add_remove"].unique()) else df.iloc[0:0]
    # Fall back for legacy files where the column is REMOVE
    if rem.empty and "REMOVE" in set(df["add_remove"].unique()):
        rem = df[df["add_remove"] == "REMOVE"]

    n_add, n_rem = len(add), len(rem)
    if n_add == 0 and n_rem == 0:
        return None
    # Only annotate "side" if there's a clear dominant side; otherwise mark mixed.
    if n_rem == 0:
        side = "ADD"; use = add
    elif n_add == 0:
        side = "REM"; use = rem
    elif n_rem / max(n_add + n_rem, 1) >= 0.8:
        side = "REM"; use = rem
    elif n_add / max(n_add + n_rem, 1) >= 0.8:
        side = "ADD"; use = add
    else:
        side = "MIX"; use = df  # keep all

    out = {"_side": side, "_n_add": n_add, "_n_rem": n_rem}
    for o in offsets:
        c = f"mo_{o}s"
        if c not in use.columns:
            continue
        v = pd.to_numeric(use[c], errors="coerce").dropna()
        if v.empty:
            continue
        # Markouts are in bps (matches convention in markout_analysis)
        out[f"mo_{o}s"] = float(v.mean())
    # If no numeric markout columns available, return None
    if not any(k.startswith("mo_") for k in out):
        return None
    return out


# ── Print ─────────────────────────────────────────────────────────────────────

def _fmt(v, w, dp=0, plus=False):
    if v is None:
        return "n/a".rjust(w)
    if plus:
        return f"{v:+.{dp}f}".rjust(w)
    if isinstance(v, int) or dp == 0:
        return f"{int(round(v)):,}".rjust(w)
    return f"{v:,.{dp}f}".rjust(w)


def build_timeline(args):
    before = weekdays_before(args.inflection, args.days_before)
    on_and_after = weekdays_from(args.inflection, args.days_after)
    dates = before + on_and_after

    print(f"\n=== {args.sym}  (inflection {args.inflection[:4]}-{args.inflection[4:6]}-{args.inflection[6:]}) ===")
    if args.strat:
        print(f"  Strat: {args.alias}/{args.group}/{args.strat}")

    # HL columns always shown
    hl_hdr = f"{'date':>10} {'dow':>3}  {'trades':>7}  {'notional':>12}  {'sprd_bp':>8}  {'ins_liq':>8}  {'midchg':>7}  {'trdsz':>6}  {'avg_mid':>8}"
    strat_hdr = ""
    cf_hdr = ""
    mo_hdr = ""

    if args.strat:
        strat_hdr = f"    {'net_pnl':>8}  {'trd':>4} {'flp':>3} {'shs':>7}  {'fill_bp':>7} {'ioc%':>5} {'min_pnl':>8}"
    if args.cancel_fills:
        cf_hdr = f"    {'cxls':>5} {'cxl_fills':>9} {'cxl_fill%':>9}"
    if args.markouts:
        mo_hdr = "    " + f"{'side':>4} " + " ".join(f"mo_{o}s".rjust(7) for o in args.mo_offsets)

    print(f"  {hl_hdr}{strat_hdr}{cf_hdr}{mo_hdr}")
    print(f"  {'-' * (len(hl_hdr) + len(strat_hdr) + len(cf_hdr) + len(mo_hdr))}")

    for i, dt in enumerate(dates):
        dow = datetime.strptime(dt, "%Y%m%d").strftime("%a")
        pretty = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
        hl = hl_stats_day(args.sym, dt)
        row_hl = f"{pretty:>10} {dow:>3}  "
        if hl is None:
            row_hl += "NO HL DATA".ljust(70)
        else:
            row_hl += (
                f"{_fmt(hl['num_trades'], 7)}  "
                f"{_fmt(hl['notional'], 12)}  "
                f"{_fmt(hl['spread_bps'], 8, 2)}  "
                f"{_fmt(hl['med_inside_liq'], 8, 1)}  "
                f"{_fmt(hl['n_midchanges'], 7)}  "
                f"{_fmt(hl['med_trdsz'], 6, 2)}  "
                f"{_fmt(hl['avg_mid'], 8, 2)}"
            )

        row_strat = ""
        if args.strat:
            s = strat_stats_day(args.local_base, args.alias, args.group, args.strat, dt, args.sym)
            if s is None:
                row_strat = "    " + "no acct row".ljust(50)
            else:
                row_strat = (
                    f"    {_fmt(s['net_pnl'], 8, 0, plus=True)}  "
                    f"{_fmt(s['times_traded'], 4)} "
                    f"{_fmt(s['times_flipped'], 3)} "
                    f"{_fmt(s['shs_traded'], 7)}  "
                    f"{_fmt(s['fill_rate'] * 1e4, 7, 1)} "
                    f"{_fmt(s['ioc_fill_rate'] * 100, 5, 2)} "
                    f"{_fmt(s['min_pnl_seen'], 8, 0, plus=True)}"
                )

        row_cf = ""
        if args.cancel_fills:
            if not args.strat:
                row_cf = "    " + "(--cancel-fills needs --strat)".ljust(30)
            else:
                c = cancel_fill_stats_day(args.local_base, args.alias, args.group, args.strat, dt, args.sym)
                if c is None:
                    row_cf = "    " + "no orders".ljust(30)
                else:
                    row_cf = (
                        f"    {_fmt(c['cancels_placed'], 5)} "
                        f"{_fmt(c['cancels_that_filled'], 9)} "
                        f"{_fmt(c['cancel_fill_pct'], 9, 2)}"
                    )

        row_mo = ""
        if args.markouts:
            if not args.strat:
                row_mo = "    " + "(--markouts needs --strat)".ljust(30)
            else:
                m = markouts_day(args.local_base, args.alias, args.group, args.strat,
                                 dt, args.sym, args.mo_offsets)
                if m is None:
                    row_mo = "    " + "no markouts".ljust(30)
                else:
                    parts = [f"{m.get('_side','?'):>4}"]
                    for o in args.mo_offsets:
                        k = f"mo_{o}s"
                        parts.append(_fmt(m.get(k), 7, 2, plus=True) if m.get(k) is not None
                                     else "n/a".rjust(7))
                    row_mo = "    " + " ".join(parts)

        marker = "  ← inflection" if dt == args.inflection else ""
        print(f"  {row_hl}{row_strat}{row_cf}{row_mo}{marker}")

        # Separator between "before" and "on-and-after"
        if i == args.days_before - 1:
            total_w = len(hl_hdr) + len(strat_hdr) + len(cf_hdr) + len(mo_hdr)
            print(f"  {'-' * total_w}")


def main():
    ap = argparse.ArgumentParser(description="Per-day timeline around an inflection date.")
    ap.add_argument("--sym", required=True, help="e.g. xyz:NVDA")
    ap.add_argument("--inflection", required=True, help="YYYYMMDD — inflection or first bad day")
    ap.add_argument("--days-before", type=int, default=5)
    ap.add_argument("--days-after", type=int, default=5,
                    help="Includes inflection day (default 5)")
    ap.add_argument("--alias", default=None)
    ap.add_argument("--group", default=None)
    ap.add_argument("--strat", default=None,
                    help="If given (with --alias/--group), also pull strat-side metrics from local acct files.")
    ap.add_argument("--local-base", default=DEFAULT_LOCAL_BASE)
    ap.add_argument("--cancel-fills", action="store_true",
                    help="Also compute cancel-fill rate from orders_YYYYMMDD.csv (needs --strat).")
    ap.add_argument("--markouts", action="store_true",
                    help="Run bin/markout on trades files (needs --strat).")
    ap.add_argument("--mo-offsets", default="1,10,30,60,120",
                    help="Comma-separated markout offsets in seconds.")
    args = ap.parse_args()
    args.mo_offsets = [int(o) for o in args.mo_offsets.split(",")]
    if any([args.alias, args.group, args.strat]) and not all([args.alias, args.group, args.strat]):
        ap.error("--alias, --group, --strat must be given together (or none of them)")
    build_timeline(args)


if __name__ == "__main__":
    main()
