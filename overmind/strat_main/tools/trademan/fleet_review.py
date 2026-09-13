#!/usr/bin/env python3
"""Fleet review — inheritance-attributed PnL rollup + actionable classification.

Three subcommands, each producing a CSV under --outdir (default ~/scratch/tradeperf/):

  attribute — per (alias, group, strat, sym, date) row: net_pnl split into
              inherit_mtm (mark-to-market on the SOD position over the strat's
              trading window) and strat_own (residual, i.e. earned edge).
              Output: _attribution_<since>.csv

  classify  — reads the attribution CSV, aggregates to (alias, group, strat, sym),
              tags each pair as FAKE_WINNER, REAL_BLEEDER, CUSHIONED_BLEEDER,
              MARGINAL_BLEEDER, OVERACHIEVER, GENUINE_WINNER, or NEUTRAL.
              Output: _actionable_<since>.csv

  history   — reads actionable pairs, pulls each pair's full lifetime history
              from local acct files, adds monthly averages, peak date, days
              since peak, drawdown, and a verdict (ALWAYS_BAD, OLD_DECAY,
              RECENT_DECAY, CYCLICAL, STEADY_EARNER, NEW/THIN).
              Output: _history_review_<since>.csv

  run       — attribute → classify → history in one shot.

Attribution method — READ THIS BEFORE INTERPRETING THE OUTPUT
-------------------------------------------------------------
For each (strat, sym, date) with trades:
  SOD_pos      = first trade's end_pos − signed size of first trade
  first_mid    = first trade's mid (fall back to fill price)
  last_mid     = last trade's mid  (fall back to fill price)
  inherit_mtm  = SOD_pos × (last_mid − first_mid)
  own          = net_pnl (from acct) − inherit_mtm

What `inherit_mtm` means, precisely:
  The change in mark-to-market value of the inherited SOD position IF IT HAD
  BEEN HELD CONSTANT across the strat's session, marked at first_mid at open
  and last_mid at close. It is a **fair-value benchmark**, not a strategy
  attribution — it doesn't depend on what the strat actually did.

What `own` (strat_own) means, precisely:
  The difference between what the strat actually realized and what a pure
  buy-and-mark-to-market on the SOD position would have shown. Captures three
  effects mixed:
    (1) Spread capture from making markets  (positive)
    (2) Adverse selection on fills          (negative)
    (3) Positioning changes via signal      (either sign, depends on quality)

Sign interpretation:
  own > 0  — strat's active trading ADDED value beyond the buy-and-hold mark.
             Spread capture net of adverse selection was positive.
  own < 0  — strat's trading SUBTRACTED value vs the buy-and-hold mark.
             **This does NOT mean the strat lost money**: net_pnl can still
             be strongly positive. When the SOD position drifted favorably
             through the session, a well-functioning MM will often show
             own < 0 because pure buy-and-hold ALSO earned that drift and
             the MM's flatten-by-EOD activity gave some of it back.

What `own` does NOT measure:
  * The strat's edge. `own < 0` on any single window is not a disable trigger.
  * A verdict on any pair. That is the job of the kill decision framework
    (overmind/studies/kill_decision_framework.md — forward monthly hit rate
    + PnL trajectory).

Every FAKE_WINNER / REAL_BLEEDER / etc classification below is a DIAGNOSTIC
LABEL for the sign pattern of (net, inh_mtm, own) — never an edge verdict.
Always cross-check with the framework before recommending action.

Caveats:
  * Mid-day inherits (a second GPCalc event during the session) are absorbed
    into own. For strats with material intraday inherits, inh_mtm understates
    the true buy-and-hold benchmark.
  * Cross-family strats (RelCross et al) end flat by design, so SOD_pos = 0
    for their entries -> inh_mtm = 0 always.

Usage
-----
Global args come before the subcommand:
  python fleet_review.py --since 20260701 run
  python fleet_review.py --since 20260701 attribute
  python fleet_review.py --since 20260701 classify
  python fleet_review.py --since 20260701 history

Filter to one family:
  python fleet_review.py --since 20260701 --strat usday run
  python fleet_review.py --since 20260701 --aliases gf3 run
"""

import argparse
import glob
import os
import re
import sys
from datetime import date, datetime, timedelta

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

DEFAULT_LOCAL_BASE = os.path.expanduser("~/scratch/tradeperf")

# Trade file columns (matches sim_eval_drill.load_trade_file)
TRADE_COLS = [
    "timestamp", "time_ms", "order_num", "sym", "market", "side",
    "price", "size", "end_pos", "closed_pnl", "commission",
    "col11", "bid", "ask", "col14", "mid", "col16",
    "col17", "col18", "col19", "add_remove", "front_flag",
]


# ── data loading ──────────────────────────────────────────────────────────────

def _load_trades(path):
    try:
        df = pd.read_csv(path, header=None, names=TRADE_COLS)
    except Exception:
        return None
    for c in ["price", "size", "end_pos", "closed_pnl", "commission", "bid", "ask", "mid"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _load_acct_history(local_base, since=None, aliases=None, strat_filter=None, group_filter=None):
    """Load every acct_YYYYMMDD.csv under local_base matching filters."""
    rows = []
    for f in glob.glob(os.path.join(local_base, "gf*/*/*/acct_2*.csv")):
        m = re.match(rf".*/{re.escape(os.path.basename(local_base))}/([^/]+)/([^/]+)/([^/]+)/acct_(\d{{8}})\.csv$", f)
        if not m:
            continue
        alias, group, strat, dt = m.groups()
        if since and int(dt) < int(since):
            continue
        if aliases and alias not in aliases:
            continue
        if strat_filter and strat_filter not in strat:
            continue
        if group_filter and group_filter not in group:
            continue
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        if not len(df) or "sym" not in df.columns:
            continue
        df = df.copy()
        df["alias"], df["group"], df["strat"], df["date"] = alias, group, strat, dt
        rows.append(df)
    if not rows:
        return pd.DataFrame()
    d = pd.concat(rows, ignore_index=True)
    for c in ["net_pnl", "times_traded", "shs_traded", "min_pnl_seen", "open_pos"]:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    return d


# ── attribute ─────────────────────────────────────────────────────────────────

def _analyze_day(local_base, alias, group, strat, dt, sym, net_pnl):
    """Compute mark-to-market inheritance attribution for one (strat, sym, day).

    ── What `inherit_mtm` measures — exactly ────────────────────────────────
    Given SOD_pos (position at start of session, inherited from a sibling
    handoff), first_mid (market mid at the strat's first trade), and last_mid
    (market mid at the strat's last trade):

        inherit_mtm = SOD_pos × (last_mid − first_mid)

    This is the change in mark-to-market value of the inherited position IF
    IT HAD BEEN HELD CONSTANT across the strat's session, marked at first_mid
    at open and last_mid at close.

    It is a **fair-value benchmark**, not a strategy attribution. It does not
    depend on what the strat actually did; it only depends on the SOD position
    and the two market mids.

    ── What `own` (strat_own) measures — exactly ────────────────────────────
        own = net_pnl − inherit_mtm

    This is the difference between what the strat actually realized and what
    a pure buy-and-mark-to-market on the SOD position would have shown. It
    captures three effects mixed together:

      1) Spread capture from making markets (MM captures ~half-spread on each
         round trip → positive contribution)
      2) Adverse selection on fills (someone hits our bid and the market
         moves away → negative contribution)
      3) Positioning changes via signal beyond the inherited SOD (positive or
         negative — depends on signal quality that day)

    ── Sign interpretation ─────────────────────────────────────────────────
      own > 0 : strat's active trading added value BEYOND the buy-and-hold
                mark. Spread capture net of adverse selection was positive.
      own < 0 : strat's trading subtracted value vs the buy-and-hold mark.
                This does NOT mean the strat lost money — net_pnl can still
                be strongly positive. On days when the SOD position drifted
                favorably, a well-functioning MM can naturally show own < 0
                because the pure buy-and-hold benchmark also earned that
                drift, and real MM trading had to work (flatten by EOD)
                that lost some of it.

    ── What `own` does NOT measure ─────────────────────────────────────────
      * The strat's edge. `own < 0` is not a disable trigger.
      * A verdict on any pair. That is the job of the kill decision framework
        in `overmind/studies/kill_decision_framework.md` (recent forward hit
        rate + monthly PnL trajectory).

    The classification categories emitted by `cmd_classify` (FAKE_WINNER,
    REAL_BLEEDER, etc.) are diagnostic labels for the SIGN pattern of
    net/inh/own; they carry no edge verdict on their own. Always cross-check
    with the framework before acting.
    """
    tpath = os.path.join(local_base, alias, group, strat, f"trades_{dt}.csv")
    if not os.path.exists(tpath):
        return None
    df = _load_trades(tpath)
    if df is None or df.empty:
        return None
    df = df[df["sym"] == sym].sort_values("timestamp")
    if len(df) == 0:
        return None
    first, last = df.iloc[0], df.iloc[-1]
    signed = first["size"] if first["side"] == "Buy" else -first["size"]
    sod_pos = first["end_pos"] - signed
    first_mid = first["mid"] if first["mid"] > 0 else first["price"]
    last_mid = last["mid"] if last["mid"] > 0 else last["price"]
    inherit_mtm = sod_pos * (last_mid - first_mid)
    return {
        "sod_pos": sod_pos, "first_mid": first_mid, "last_mid": last_mid,
        "inherit_mtm": inherit_mtm, "net": net_pnl,
        "own": net_pnl - inherit_mtm, "had_inh": sod_pos != 0,
    }


def cmd_attribute(args):
    d = _load_acct_history(args.local_base, since=args.since,
                           aliases=args.aliases.split(",") if args.aliases else None,
                           strat_filter=args.strat, group_filter=args.group)
    if d.empty:
        print("No acct data matched filters.")
        return
    d = d[d["times_traded"] > 0]
    out = []
    for _, r in d.iterrows():
        a = _analyze_day(args.local_base, r["alias"], r["group"], r["strat"], r["date"], r["sym"], r["net_pnl"])
        if a is None:
            continue
        out.append({
            "alias": r["alias"], "group": r["group"], "strat": r["strat"],
            "date": r["date"], "sym": r["sym"],
            "net": a["net"], "inh": a["inherit_mtm"], "own": a["own"],
            "had_inh": a["had_inh"], "sod_pos": a["sod_pos"],
        })
    A = pd.DataFrame(out)
    path = os.path.join(args.outdir, f"_attribution_{args.since}.csv")
    A.to_csv(path, index=False)
    print(f"Wrote {len(A)} rows to {path}")
    tot_net, tot_inh, tot_own = A["net"].sum(), A["inh"].sum(), A["own"].sum()
    share = tot_inh / tot_net * 100 if tot_net else 0
    print(f"  totals: net={tot_net:+.0f}  inh_mtm={tot_inh:+.0f} ({share:+.0f}%)  own={tot_own:+.0f}")
    print(f"  NOTE: inh_mtm is a fair-value benchmark (buy-and-hold-mark), NOT a strategy attribution.")
    print(f"        own = net − inh_mtm; own<0 does NOT mean the strat is losing money.")
    print(f"        For disable/hold decisions, use overmind/studies/kill_decision_framework.md.")


# ── classify ──────────────────────────────────────────────────────────────────

def _classify_pair(r):
    """Tag a (strat, sym) aggregate row by the sign pattern of net / inh_mtm / own.

    IMPORTANT — these are DIAGNOSTIC labels for the shape of the numbers, NOT
    edge verdicts. See `_analyze_day` docstring for the precise meaning of
    `inh_mtm` and `own`. Key non-obvious cases:

    - FAKE_WINNER (net>0, own<0 large, inh>|own|): the strat is UP but its
      trading contributed less than the buy-and-hold MTM benchmark on the
      SOD position. This can happen for a well-functioning MM when the SOD
      position drifted favorably — the pure buy-and-hold benchmark captured
      the drift, and the MM's flatten-by-EOD activity gave some of it back.
      NOT a disable trigger on its own.
    - OVERACHIEVER (net>0, own>0, inh<0): the strat's trading captured value
      even while the SOD position was drifting AGAINST it. Good sign for
      real edge but still requires the framework to confirm.
    - REAL_BLEEDER (net<0, own<-200): visible loss AND the strat's trading
      was on the wrong side of the MTM benchmark. The clearest "actual loss"
      signal but the classification alone still isn't sufficient — walk the
      framework to check if it's noise or genuine decline.

    Always cross-check any label with the kill decision framework
    (overmind/studies/kill_decision_framework.md) before recommending action.
    """
    if r["own"] < -100 and r["net"] > 0 and r["inh"] > abs(r["own"]):
        return "FAKE_WINNER"       # net positive; own underperformed the buy-and-hold MTM
    if r["own"] < -200 and r["net"] < 0:
        return "REAL_BLEEDER"      # visible loss AND own on wrong side of MTM
    if r["own"] < -100 and r["net"] < -50 and r["inh"] > 50:
        return "CUSHIONED_BLEEDER" # visible loss; MTM was favourable but own worse still
    if r["own"] > 200 and r["net"] > 0 and r["inh"] < -100:
        return "OVERACHIEVER"      # net positive AND own captured value despite adverse MTM
    if r["own"] > 100 and r["net"] > 0:
        return "GENUINE_WINNER"
    if r["own"] < -50 and abs(r["net"]) < 100:
        return "MARGINAL_BLEEDER"  # small net; own on wrong side of MTM
    return "NEUTRAL"


def cmd_classify(args):
    src = os.path.join(args.outdir, f"_attribution_{args.since}.csv")
    if not os.path.exists(src):
        print(f"Missing {src} — run `attribute` first.")
        sys.exit(1)
    A = pd.read_csv(src)
    G = A.groupby(["alias", "group", "strat", "sym"]).agg(
        d=("date", "nunique"), n_inh=("had_inh", "sum"),
        net=("net", "sum"), inh=("inh", "sum"), own=("own", "sum"),
    ).reset_index()
    G = G[G["d"] >= args.min_days]
    G["cat"] = G.apply(_classify_pair, axis=1)
    path = os.path.join(args.outdir, f"_actionable_{args.since}.csv")
    G.to_csv(path, index=False)
    counts = G["cat"].value_counts().to_dict()
    print(f"Wrote {len(G)} pairs to {path}")
    for c in ["FAKE_WINNER", "REAL_BLEEDER", "CUSHIONED_BLEEDER", "MARGINAL_BLEEDER",
              "OVERACHIEVER", "GENUINE_WINNER", "NEUTRAL"]:
        n = counts.get(c, 0)
        if n:
            sub = G[G["cat"] == c]
            print(f"  {c:>18}: n={n:>3}  net={sub['net'].sum():+.0f}  own={sub['own'].sum():+.0f}")


# ── history ───────────────────────────────────────────────────────────────────

def _monthly_avg(hist, lo, hi):
    s = hist[(hist["date"] >= lo) & (hist["date"] < hi)]
    return len(s), s["net_pnl"].sum(), (s["net_pnl"].mean() if len(s) else 0)


def _verdict(hist, a_may, a_jun, a_jul, days_since_peak, dd_from_peak, peak_cum, total_days):
    life_pnl = hist["net_pnl"].sum()
    monthly_avgs = [a for n, s, a in [(1, 1, a_may), (1, 1, a_jun), (1, 1, a_jul)] if a != 0]
    if total_days < 15:
        return "NEW/THIN"
    if all(a < 0 for a in monthly_avgs) or (life_pnl < 0 and life_pnl / total_days < -30):
        return "ALWAYS_BAD"
    if days_since_peak > 20 and (dd_from_peak) / max(abs(peak_cum), 1) < -0.1:
        return "OLD_DECAY"
    if days_since_peak <= 20 and dd_from_peak < -100:
        return "RECENT_DECAY"
    if a_jul < 0 and a_jun > 0 and a_may > 0:
        return "RECENT_BREAK"
    if a_jul > 0 and a_jun > 0 and life_pnl > 500:
        return "STEADY_EARNER"
    return "CYCLICAL"


def _summarize_history(all_hist, alias, group, strat, sym):
    hist = all_hist[(all_hist["alias"] == alias) & (all_hist["group"] == group)
                    & (all_hist["strat"] == strat) & (all_hist["sym"] == sym)].sort_values("date").reset_index(drop=True)
    if len(hist) == 0:
        return None
    hist["cum"] = hist["net_pnl"].cumsum()
    peak_idx = hist["cum"].idxmax()
    peak_cum = hist["cum"].iloc[peak_idx]
    n_feb, _, a_feb = _monthly_avg(hist, "20260101", "20260301")
    n_mar, _, a_mar = _monthly_avg(hist, "20260301", "20260501")
    n_may, _, a_may = _monthly_avg(hist, "20260501", "20260601")
    n_jun, _, a_jun = _monthly_avg(hist, "20260601", "20260701")
    n_jul, _, a_jul = _monthly_avg(hist, "20260701", "20261231")
    days_since_peak = len(hist) - peak_idx - 1
    dd_from_peak = hist["cum"].iloc[-1] - peak_cum
    return {
        "total_d": len(hist), "life": hist["net_pnl"].sum(),
        "peak_date": hist["date"].iloc[peak_idx], "peak_cum": peak_cum,
        "dd_from_peak": dd_from_peak, "d_since_peak": days_since_peak,
        "feb": a_feb, "mar_apr": a_mar, "may": a_may, "jun": a_jun, "jul": a_jul,
        "d_feb": n_feb, "d_mar_apr": n_mar, "d_may": n_may, "d_jun": n_jun, "d_jul": n_jul,
        "verdict": _verdict(hist, a_may, a_jun, a_jul, days_since_peak, dd_from_peak, peak_cum, len(hist)),
    }


def cmd_history(args):
    src = os.path.join(args.outdir, f"_actionable_{args.since}.csv")
    if not os.path.exists(src):
        print(f"Missing {src} — run `classify` first.")
        sys.exit(1)
    G = pd.read_csv(src)
    actionable = G[G["cat"].isin(["FAKE_WINNER", "REAL_BLEEDER", "CUSHIONED_BLEEDER",
                                   "MARGINAL_BLEEDER", "OVERACHIEVER"])].copy()
    all_hist = _load_acct_history(args.local_base)  # full history, no filter
    if all_hist.empty:
        print("No acct history found.")
        return
    rows = []
    for _, r in actionable.iterrows():
        s = _summarize_history(all_hist, r["alias"], r["group"], r["strat"], r["sym"])
        if s is None:
            continue
        rows.append({**r.to_dict(), **s})
    R = pd.DataFrame(rows)
    path = os.path.join(args.outdir, f"_history_review_{args.since}.csv")
    R.to_csv(path, index=False)
    print(f"Wrote {len(R)} rows to {path}")
    print("\nCategory x Verdict counts:")
    print(pd.crosstab(R["cat"], R["verdict"], margins=True).to_string())


def cmd_run(args):
    import io
    body_capture = io.StringIO()
    from contextlib import redirect_stdout

    class _Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, s):
            for st in self.streams: st.write(s)
        def flush(self):
            for st in self.streams: st.flush()

    tee = _Tee(sys.stdout, body_capture)
    with redirect_stdout(tee):
        cmd_attribute(args)
        cmd_classify(args)
        cmd_history(args)

    if getattr(args, "email", False):
        _strat_main = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if _strat_main not in sys.path:
            sys.path.insert(0, _strat_main)
        try:
            from util import email_utils
        except ImportError as e:
            print(f"# email skipped: cannot import email_utils: {e}", file=sys.stderr)
        else:
            from datetime import datetime as _dt
            subject = f"Fleet review — since {args.since} — run {_dt.today().strftime('%Y-%m-%d')}"
            kwargs = {"subject": subject, "body": body_capture.getvalue(), "monospace": True}
            if getattr(args, "to", None):
                kwargs["receiver"] = args.to
            email_utils.send_mail(**kwargs)
            print("# Report emailed.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fleet inheritance attribution + actionable classification.")
    ap.add_argument("--outdir", default=DEFAULT_LOCAL_BASE, help="Directory to write CSVs (default ~/scratch/tradeperf)")
    ap.add_argument("--local-base", default=DEFAULT_LOCAL_BASE, help="tradeperf root (default ~/scratch/tradeperf)")
    ap.add_argument("--since", required=True, help="Analysis window start, YYYYMMDD")
    ap.add_argument("--aliases", default=None, help="Comma-separated alias filter (default all)")
    ap.add_argument("--strat", default=None, help="Substring filter on strat name")
    ap.add_argument("--group", default=None, help="Substring filter on group name")
    ap.add_argument("--min-days", type=int, default=3, help="Minimum days a pair must have to be classified")
    ap.add_argument("--email", action="store_true", help="Email the report (only respected by `run`)")
    ap.add_argument("--to", type=str, default=None, help="Email recipient (default: group list)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("attribute", cmd_attribute), ("classify", cmd_classify),
                     ("history", cmd_history), ("run", cmd_run)]:
        sp = sub.add_parser(name)
        sp.set_defaults(func=fn)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
