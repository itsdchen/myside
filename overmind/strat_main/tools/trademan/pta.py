#!/usr/bin/env python3
"""PTA — Post Trade Analysis.

Two subcommands:

  flag   — scan local acct files, z-score net_pnl per (alias, group, strat, sym)
           using a trailing window, list days where a symbol lost an unusually
           large amount.
  drill  — for a flagged (date, sym, [strat]), pull the trades file and produce
           a per-day forensic view: trade summary, TOD buckets, ADD/REMOVE,
           top losing trades, position trajectory, log events.

Usage:
  python pta.py flag [--days N] [--window 30] [--min-days 10]
                     [--z-threshold -2.0] [--abs-floor -25]
                     [--aliases gf0,gf1] [--strat S] [--sym xyz:GOLD]
  python pta.py drill --date YYYYMMDD --sym xyz:GOLD
                      [--strat S] [--group G] [--alias gf0]
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from trade_manager import CONFIG, load_acct_data
from sim_eval_drill import load_trade_file
from sim_eval_logs import parse_log_events


# ── flag ──────────────────────────────────────────────────────────────────────

def compute_zscores(acct, window, min_days):
    """Add baseline_mean / baseline_std / n_baseline / z_net_pnl columns.

    Baseline for row i in each (alias, group, strat, sym) series is the
    trailing `window` rows strictly before i — never includes the current day.
    "Window" counts trading days for that sym, not calendar days.
    """
    if acct.empty:
        return acct.copy()

    acct = acct.copy()
    acct["date"] = acct["date"].astype(str)
    acct["net_pnl"] = pd.to_numeric(acct["net_pnl"], errors="coerce")
    acct = acct.sort_values(["alias", "group", "strat", "sym", "date"]).reset_index(drop=True)

    def per_group(g):
        means, stds, counts = [], [], []
        for i in range(len(g)):
            past = g["net_pnl"].iloc[max(0, i - window):i].dropna()
            counts.append(len(past))
            if len(past) >= min_days:
                means.append(past.mean())
                stds.append(past.std(ddof=1) if len(past) > 1 else float("nan"))
            else:
                means.append(float("nan"))
                stds.append(float("nan"))
        g = g.copy()
        g["baseline_mean"] = means
        g["baseline_std"] = stds
        g["n_baseline"] = counts
        return g

    acct = acct.groupby(["alias", "group", "strat", "sym"], group_keys=False).apply(per_group)
    acct["z_net_pnl"] = (acct["net_pnl"] - acct["baseline_mean"]) / acct["baseline_std"]
    return acct


def compute_flags(args):
    """Run the flagger and return (flagged_df, header_str)."""
    aliases = args.aliases.split(",") if args.aliases else None
    strats = [args.strat] if args.strat else None
    symbols = [args.sym] if args.sym else None

    acct = load_acct_data(
        CONFIG["local_base"],
        aliases=aliases,
        strats=strats,
        symbols=symbols,
    )
    header = (f"(z < {args.z_threshold}, net_pnl < {args.abs_floor}, "
              f"window={args.window}d trailing, min_days={args.min_days})")
    if acct.empty:
        return pd.DataFrame(), header

    acct = compute_zscores(acct, window=args.window, min_days=args.min_days)
    flagged = acct[
        (acct["z_net_pnl"] < args.z_threshold)
        & (acct["net_pnl"] < args.abs_floor)
    ].copy()
    if args.days:
        cutoff = (datetime.today() - timedelta(days=args.days)).strftime("%Y%m%d")
        flagged = flagged[flagged["date"] >= cutoff]
    return flagged.sort_values("z_net_pnl"), header


def cmd_flag(args):
    flagged, header = compute_flags(args)
    if flagged.empty:
        print(f"No flagged days {header}.")
        return

    cols = ["date", "alias", "group", "strat", "sym", "net_pnl",
            "z_net_pnl", "baseline_mean", "baseline_std", "n_baseline"]
    for c in ["net_pnl", "z_net_pnl", "baseline_mean", "baseline_std"]:
        flagged[c] = flagged[c].round(2)
    print(f"Flagged days {header}:\n")
    print(flagged[cols].to_string(index=False))
    print(f"\n{len(flagged)} flagged.")


# ── drill ─────────────────────────────────────────────────────────────────────

def _diagnose_day(df, acct_row, context=None):
    """Return a list of factor dicts explaining the day's loss.

    Each factor: {label, metric, hypothesis, check}.
    Heuristics — not exact attribution. Multiple factors can fire on one day.

    `context` is an optional dict carrying external data:
        history_acct : DataFrame of prior acct rows for this (alias, group,
                       strat, sym) — used for volume anomaly.
        markouts     : DataFrame of fillstats rows for this sym/day with mo_Ns
                       columns — used for adverse-selection detection.
        mo_offset    : int seconds of the markout offset to use (e.g. 30).
    """
    context = context or {}
    df = df.copy()
    if "trade_pnl" not in df.columns:
        df["trade_pnl"] = df["closed_pnl"].diff().fillna(df["closed_pnl"].iloc[0])

    net_pnl = float(acct_row.get("net_pnl", df["trade_pnl"].sum()))
    if net_pnl >= 0:
        return []

    factors = []

    first_price = float(df["price"].iloc[0])
    last_price = float(df["price"].iloc[-1])
    price_move = last_price - first_price
    price_move_pct = (price_move / first_price) if first_price else 0.0

    end_pos = float(df["end_pos"].iloc[-1])
    avg_pos = float(df["end_pos"].mean())
    max_abs_pos = float(df["end_pos"].abs().max())

    # ── Directional inventory exposure ──
    inventory_pnl_est = avg_pos * price_move
    if inventory_pnl_est < 0 and abs(inventory_pnl_est) >= 0.4 * abs(net_pnl):
        share = abs(inventory_pnl_est) / abs(net_pnl)
        share_str = (f"~{share:.0%} of loss" if share <= 1.0
                     else f">{int(share*100)}% of loss; trading partially offset")
        side = "long" if avg_pos > 0 else "short"
        factors.append({
            "label": "DIRECTIONAL",
            "metric": (f"avg pos {avg_pos:+.1f} ({side}) while price moved "
                       f"{price_move:+.4f} ({price_move_pct:+.2%}) → "
                       f"inventory_pnl ≈ {inventory_pnl_est:+.2f} ({share_str})"),
            "hypothesis": ("MM held persistent directional position into an "
                           "adverse move — signal mispredict, inventory skew "
                           "too weak, or hit max-pos and couldn't unwind."),
            "check": ("did max-pos cap fire? is the inventory-penalty kicking "
                      "in at the right size? signal_fair vs realized price; "
                      "check for one-sided fill stream that pushed position there."),
        })

    # ── Whipsaw / round-trip leak ──
    buys = df[df["side"] == "Buy"]
    sells = df[df["side"] == "Sell"]
    total_size = float(df["size"].sum())
    if not buys.empty and not sells.empty and total_size > 0:
        buy_vwap = (buys["price"] * buys["size"]).sum() / buys["size"].sum()
        sell_vwap = (sells["price"] * sells["size"]).sum() / sells["size"].sum()
        edge_per_share = sell_vwap - buy_vwap
        times_flipped = acct_row.get("times_flipped")
        round_trip_ratio = total_size / (max_abs_pos + 1e-9)
        if (
            edge_per_share < 0
            and round_trip_ratio >= 4
            and (times_flipped is None or times_flipped >= 4)
        ):
            est_leak = edge_per_share * (min(buys["size"].sum(), sells["size"].sum()))
            tf_str = f", {int(times_flipped)} flips" if times_flipped else ""
            edge_bps = (edge_per_share / first_price * 1e4) if first_price else 0
            factors.append({
                "label": "WHIPSAW",
                "metric": (f"sell_vwap {sell_vwap:.4f} < buy_vwap {buy_vwap:.4f} "
                           f"(Δ={edge_per_share:+.4f}/sh = {edge_bps:+.1f} bps)"
                           f"{tf_str}; round-trip leak est. {est_leak:.2f} on "
                           f"{total_size:.0f} sh"),
                "hypothesis": ("realized adverse selection per round-trip "
                               "exceeded quoted spread — either quotes too tight "
                               "for the realized vol, or you're slow to cancel "
                               "after one side fills (the other side stays "
                               "exposed)."),
                "check": ("compare quoted half-spread to per-share leak; pull "
                          "`fillstats` to see markouts; consider vol-aware "
                          "spread or faster cancel-on-flip."),
            })

    # ── Concentrated loss (few bad trades) ──
    losers = df[df["trade_pnl"] < 0].sort_values("trade_pnl")
    total_loss = float(losers["trade_pnl"].sum()) if not losers.empty else 0.0
    if total_loss < 0 and len(losers) >= 5:
        top3 = float(losers.head(3)["trade_pnl"].sum())
        if top3 / total_loss >= 0.5:
            without_top3 = net_pnl - top3
            wl = losers.iloc[0]
            factors.append({
                "label": "CONCENTRATED",
                "metric": (f"top 3 trades = {top3:.2f}; without them the day "
                           f"would have been {without_top3:+.2f}. Worst: "
                           f"{wl['time_str']} {wl['side']} "
                           f"{wl['size']:.2f}@{wl['price']:.2f} → "
                           f"{wl['trade_pnl']:.2f}"),
                "hypothesis": ("a handful of trades crystallized most of the "
                               "loss — usually closing a built-up position at a "
                               "bad price, or a single adverse fill that broke "
                               "through risk levels."),
                "check": ("look at the worst trades' end_pos — were they closing "
                          "a large position? was a TL transition or cancel-pause "
                          "in flight? check log events around those timestamps."),
            })

    # ── Time-bucketed spike + one-sided flow in that bucket ──
    df["_bucket"] = df["time_str"].str[:2] + ":" + \
                    (df["time_str"].str[3:5].astype(int) // 30 * 30).astype(str).str.zfill(2)
    bucket_pnl = df.groupby("_bucket")["trade_pnl"].sum()
    worst_bucket = bucket_pnl.idxmin()
    worst_bucket_loss = float(bucket_pnl.min())
    if worst_bucket_loss < 0 and worst_bucket_loss / net_pnl >= 0.4:
        factors.append({
            "label": "TIME-SPIKE",
            "metric": (f"{worst_bucket} bucket lost {worst_bucket_loss:.2f} "
                       f"(~{worst_bucket_loss / net_pnl:.0%} of net_pnl)"),
            "hypothesis": ("loss is event-shaped: scheduled news, open/close, "
                           "or a single liquidity-removing trade — not a "
                           "spread-out adverse drift."),
            "check": ("what happens at this time daily? if scheduled (data "
                      "release, session boundary), implement event-aware "
                      "widen/pause; if ad-hoc, check feed_latency and reject "
                      "log events in this window."),
        })

        # One-sided flow within the worst bucket — strong pickoff signal
        bkt = df[df["_bucket"] == worst_bucket]
        bkt_buy_sz = float(bkt[bkt["side"] == "Buy"]["size"].sum())
        bkt_sell_sz = float(bkt[bkt["side"] == "Sell"]["size"].sum())
        bkt_total = bkt_buy_sz + bkt_sell_sz
        if bkt_total > 0:
            buy_frac = bkt_buy_sz / bkt_total
            if buy_frac >= 0.8 or buy_frac <= 0.2:
                dominant = "buyer" if buy_frac >= 0.8 else "seller"
                pct = buy_frac if buy_frac >= 0.8 else 1 - buy_frac
                factors.append({
                    "label": "ONE-SIDED FLOW",
                    "metric": (f"in {worst_bucket} bucket you were "
                               f"{pct:.0%} {dominant} ({bkt_buy_sz:.0f}B / "
                               f"{bkt_sell_sz:.0f}S of {bkt_total:.0f} sh)"),
                    "hypothesis": ("one side of your book repeatedly filled "
                                   "while the other side didn't — classic "
                                   "stale-quote pickoff, or you couldn't cancel "
                                   "fast enough as price ran."),
                    "check": ("RTT during this bucket (`order-analysis`), "
                              "cancel-reject events in logs, and quote update "
                              "cadence on the side that didn't fill."),
                })

    # ── Drawdown shape ──
    min_pnl_seen = acct_row.get("min_pnl_seen")
    closed_pnl = acct_row.get("closed_pnl", net_pnl)
    if min_pnl_seen is not None and not pd.isna(min_pnl_seen):
        min_pnl_seen = float(min_pnl_seen)
        closed_pnl = float(closed_pnl)
        recovery = closed_pnl - min_pnl_seen
        if min_pnl_seen < -1 and recovery >= 0.3 * abs(min_pnl_seen):
            factors.append({
                "label": "PARTIAL RECOVERY",
                "metric": (f"min_pnl_seen {min_pnl_seen:.2f}, ended "
                           f"{closed_pnl:.2f} (climbed back {recovery:+.2f}, "
                           f"~{recovery / abs(min_pnl_seen):.0%})"),
                "hypothesis": "dug a hole then mean-reverted partway out — common after a transient adverse move.",
                "check": "ok unless drawdown was near risk-cap; if so, sizing is too aggressive.",
            })
        elif min_pnl_seen < -1 and abs(closed_pnl - min_pnl_seen) < 0.1 * abs(min_pnl_seen):
            factors.append({
                "label": "BLED OUT",
                "metric": (f"min_pnl_seen {min_pnl_seen:.2f} ≈ closed_pnl "
                           f"{closed_pnl:.2f}; no recovery"),
                "hypothesis": "loss persisted to end of session — sym likely trended through the day.",
                "check": "did the strat keep adding to the losing position, or did it stop trading? trend-detection or harder pos-cap might cut the tail.",
            })

    # ── Ended carrying directional risk ──
    if max_abs_pos > 0 and abs(end_pos) >= 0.5 * max_abs_pos and abs(end_pos) >= 5:
        factors.append({
            "label": "OPEN POSITION",
            "metric": (f"ended {end_pos:+.1f} (max-abs during day was "
                       f"{max_abs_pos:.1f})"),
            "hypothesis": "session ended without flattening — overnight directional risk.",
            "check": "if intentional (overnight strat), fine. If MM that should end flat, check eod-flatten logic.",
        })

    # ── Volume anomaly ──
    # If today's shs_traded is way out of distribution for this (strat, sym),
    # something unusual happened (regime shift, gap, max-pos churn).
    history = context.get("history_acct")
    today_shs = float(acct_row.get("shs_traded", 0))
    if history is not None and not history.empty and today_shs > 0:
        past = pd.to_numeric(history["shs_traded"], errors="coerce").dropna()
        if len(past) >= 10:
            mean = float(past.mean())
            std = float(past.std(ddof=1)) if len(past) > 1 else float("nan")
            if std and std > 0:
                z = (today_shs - mean) / std
                ratio = today_shs / mean if mean > 0 else float("inf")
                if z >= 3.0 and ratio >= 2.0:
                    factors.append({
                        "label": "VOLUME-SPIKE",
                        "metric": (f"shs_traded {today_shs:.0f} vs baseline "
                                   f"mean {mean:.0f} (z={z:.1f}, "
                                   f"{ratio:.1f}× typical; n={len(past)})"),
                        "hypothesis": ("traded far more than usual — often a "
                                       "regime shift, news event triggering "
                                       "repeated quoting/cancel cycles, or "
                                       "max-pos thrashing."),
                        "check": ("did size-mult get bumped via usermsg? did "
                                  "a TL transition force unwinding? compare "
                                  "fill cadence to historical TOD pattern."),
                    })

    # ── Adverse selection (markouts) ──
    # Per fillstats convention mo_Ns is side-adjusted; negative = adverse.
    # Maker (ADD) fills are where pickoff lives, so weight by those.
    markouts = context.get("markouts")
    mo_offset = context.get("mo_offset", 30)
    mo_col = f"mo_{mo_offset}s"
    if markouts is not None and not markouts.empty and mo_col in markouts.columns:
        mo = pd.to_numeric(markouts[mo_col], errors="coerce")
        add_mask = markouts["add_remove"] == "ADD"
        rem_mask = markouts["add_remove"] == "REMOVE"
        n_add = int(add_mask.sum())
        n_rem = int(rem_mask.sum())
        avg_add = float(mo[add_mask].mean()) if n_add else float("nan")
        avg_rem = float(mo[rem_mask].mean()) if n_rem else float("nan")

        first_price = float(df["price"].iloc[0])
        add_bps = (avg_add / first_price * 1e4) if (first_price and not pd.isna(avg_add)) else None
        rem_bps = (avg_rem / first_price * 1e4) if (first_price and not pd.isna(avg_rem)) else None

        # Estimated $ leak from ADD adverse selection across the day's ADD shares
        add_shs = float(markouts.loc[add_mask, "size"].sum()) if n_add else 0.0
        est_add_leak = avg_add * add_shs if n_add and not pd.isna(avg_add) else 0.0

        # Flag when add markouts are clearly adverse (≤ -0.5 bps avg, ≥20 fills)
        if n_add >= 20 and add_bps is not None and add_bps <= -0.5:
            rem_str = (f"; REMOVE avg {avg_rem:+.4f} ({rem_bps:+.1f} bps, n={n_rem})"
                       if n_rem else "")
            factors.append({
                "label": "ADVERSE SELECTION",
                "metric": (f"ADD avg markout {avg_add:+.4f} at {mo_offset}s "
                           f"({add_bps:+.1f} bps, n={n_add}); "
                           f"est leak ≈ {est_add_leak:+.2f} on {add_shs:.0f} sh"
                           f"{rem_str}"),
                "hypothesis": ("maker fills systematically printed before "
                               "adverse moves — quotes were stale or too tight "
                               "for the realized vol; classic pickoff."),
                "check": ("look at `fillstats` quartile drill (run "
                          "`trade_manager.py fillstats ...`) to see whether the "
                          "leak concentrates in high-vol or wide-spread regimes; "
                          "consider widening on vol, faster cancel-on-tick, or "
                          "raising place_thresh."),
            })

    return factors


def _print_diagnosis(df, acct_row, context=None):
    factors = _diagnose_day(df, acct_row, context=context)
    if not factors:
        print("\n  Likely factors: (none fired — diffuse loss with no dominant pattern)")
        return []
    print("\n  Likely factors:")
    for f in factors:
        print(f"    [{f['label']}]  {f['metric']}")
        print(f"      ↳ likely: {f['hypothesis']}")
        print(f"      ↳ check:  {f['check']}")
    return factors


def _load_history_acct(alias, group, strat, sym, date, window_days=45):
    """Trailing window of acct rows for this (alias, group, strat, sym) strictly
    before `date`. Window is in calendar days; acct files are per trading day."""
    date_obj = datetime.strptime(date, "%Y%m%d")
    start = (date_obj - timedelta(days=window_days)).strftime("%Y%m%d")
    end = (date_obj - timedelta(days=1)).strftime("%Y%m%d")
    return load_acct_data(
        CONFIG["local_base"],
        aliases=[alias], groups=[group], strats=[strat],
        symbols=[sym], date_from=start, date_to=end,
    )


def _load_or_run_markouts(alias, group, strat, sym, date, offset=30):
    """Return per-fill markouts DataFrame filtered to sym, or None on failure.

    Uses cached `trades_YYYYMMDD_fillstats.csv` if present; otherwise invokes
    the fillstats binary lazily. Failures (missing market data, binary not
    found, etc.) are reported on one line and return None so the drill
    continues without the markouts factor.
    """
    import contextlib
    import io as _io
    try:
        from fillstats_analysis import run_fillstats
    except Exception as e:
        print(f"  [markouts unavailable: import failed: {e}]")
        return None

    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            results = run_fillstats(
                CONFIG["local_base"], aliases=[alias], date_strs=[date],
                offsets=[offset], strat_filter=strat, group_filter=group,
            )
    except Exception as e:
        print(f"  [markouts unavailable: {e}]")
        return None

    key = (alias, group, strat, date)
    df = results.get(key)
    if df is None or df.empty:
        msg = buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else "no output"
        print(f"  [markouts unavailable: {msg}]")
        return None
    df = df[df["sym"] == sym].copy()
    if df.empty:
        print(f"  [markouts: no fills for {sym} in fillstats output]")
        return None
    return df


def _print_markouts_summary(markouts, offset):
    if markouts is None or markouts.empty:
        return
    mo_col = f"mo_{offset}s"
    if mo_col not in markouts.columns:
        return
    mo = pd.to_numeric(markouts[mo_col], errors="coerce")
    add = markouts[markouts["add_remove"] == "ADD"]
    rem = markouts[markouts["add_remove"] == "REMOVE"]
    add_mo = pd.to_numeric(add[mo_col], errors="coerce").mean() if not add.empty else float("nan")
    rem_mo = pd.to_numeric(rem[mo_col], errors="coerce").mean() if not rem.empty else float("nan")
    all_mo = mo.mean()
    print(f"\n  Markouts ({offset}s, side-adjusted, +ve = favorable):")
    print(f"    overall : {all_mo:+.4f}  (n={int(mo.notna().sum())})")
    if not pd.isna(add_mo):
        print(f"    ADD     : {add_mo:+.4f}  (n={len(add)})")
    if not pd.isna(rem_mo):
        print(f"    REMOVE  : {rem_mo:+.4f}  (n={len(rem)})")


def _print_trade_drill(df, acct_closed_pnl=None):
    df = df.sort_values("time_ms").reset_index(drop=True)

    # closed_pnl in the trade file is cumulative within the day; per-trade pnl
    # is the diff. Usually last row's closed_pnl matches acct.closed_pnl, but
    # not always (carryover, missing trades, etc.) — print the gap when it
    # exists so we don't silently mislead.
    df["trade_pnl"] = df["closed_pnl"].diff().fillna(df["closed_pnl"].iloc[0])

    n = len(df)
    buys = df[df["side"] == "Buy"]
    sells = df[df["side"] == "Sell"]
    add = df[df["add_remove"] == "ADD"]
    rem = df[df["add_remove"] == "REMOVE"]
    total_size = df["size"].sum()
    total_notional = (df["price"] * df["size"]).sum()
    vwap = total_notional / total_size if total_size > 0 else 0

    first = df.iloc[0]
    start_pos = first["end_pos"] - (first["size"] if first["side"] == "Buy" else -first["size"])
    end_pos = df["end_pos"].iloc[-1]
    final_cpnl = df["closed_pnl"].iloc[-1]

    cpnl_note = ""
    if acct_closed_pnl is not None and not pd.isna(acct_closed_pnl):
        gap = final_cpnl - acct_closed_pnl
        if abs(gap) < 0.01:
            cpnl_note = "  (matches acct)"
        else:
            cpnl_note = f"  (acct={acct_closed_pnl:.2f}, gap={gap:+.2f})"

    print(f"\n  Trades       : {n}  ({len(buys)} buys / {len(sells)} sells)")
    print(f"  ADD / REMOVE : {len(add)} / {len(rem)}")
    print(f"  Shares       : buy={buys['size'].sum():.2f}  sell={sells['size'].sum():.2f}  total={total_size:.2f}")
    print(f"  VWAP         : {vwap:.4f}")
    print(f"  Final c.pnl  : {final_cpnl:.2f}{cpnl_note}")
    print(f"  Commission   : {df['commission'].sum():.2f}")
    print(f"  Position     : start={start_pos:.2f}  end={end_pos:.2f}  "
          f"min={df['end_pos'].min():.2f}  max={df['end_pos'].max():.2f}")

    df["bucket"] = df["time_str"].str[:2] + ":" + \
                   (df["time_str"].str[3:5].astype(int) // 30 * 30).astype(str).str.zfill(2)
    tod = df.groupby("bucket").agg(
        fills=("size", "count"),
        shs=("size", "sum"),
        pnl=("trade_pnl", "sum"),
        comm=("commission", "sum"),
    ).round(2)
    print("\n  Time-of-day buckets (30m):")
    print(tod.to_string())

    losing = df.nsmallest(10, "trade_pnl")
    losing = losing[losing["trade_pnl"] < 0]
    if not losing.empty:
        print(f"\n  Top {len(losing)} losing trades (per-trade pnl):")
        cols = ["time_str", "side", "price", "size", "end_pos",
                "trade_pnl", "add_remove", "front_flag"]
        present = [c for c in cols if c in losing.columns]
        out = losing[present].copy()
        for c in ["price", "size", "end_pos", "trade_pnl"]:
            if c in out.columns:
                out[c] = out[c].round(2)
        print(out.to_string(index=False))


def _print_log_events(local_base, alias, group, strat, sym, date):
    try:
        events = parse_log_events(local_base, aliases=[alias], date_strs=[date])
    except Exception as e:
        print(f"\n  [log event parse failed: {e}]")
        return

    relevant = [
        e for e in events
        if e.get("group") == group and e.get("strat") == strat
        and (e.get("sym") in ("", sym))
    ]
    if not relevant:
        print("\n  Log events: (none for this sym/strat)")
        return

    print(f"\n  Log events ({len(relevant)}):")
    for e in relevant[:50]:
        t = e.get("time", "")
        et = e.get("event_type", "")
        detail = e.get("detail") or e.get("line", "")
        if isinstance(detail, dict):
            detail = ", ".join(f"{k}={v}" for k, v in detail.items())
        detail = str(detail)[:100]
        print(f"    {t}  {et:<22}  {detail}")
    if len(relevant) > 50:
        print(f"    ... ({len(relevant) - 50} more)")


def _drill_one(alias, group, strat, sym, date, extra_header="",
               skip_markouts=False, mo_offset=30):
    print("\n" + "=" * 80)
    title = f"PTA Drill — {sym} on {date}  [{alias}/{group}/{strat}]"
    if extra_header:
        title += f"  {extra_header}"
    print(title)
    print("=" * 80)

    acct = load_acct_data(
        CONFIG["local_base"],
        aliases=[alias], groups=[group], strats=[strat],
        symbols=[sym], date_from=date, date_to=date,
    )
    if acct.empty:
        print(f"  [no acct row for {alias}/{group}/{strat} {sym} {date}]")
        return []

    row = acct.iloc[0]
    print(f"  net_pnl      : {row.get('net_pnl', float('nan')):>12.2f}")
    print(f"  closed_pnl   : {row.get('closed_pnl', float('nan')):>12.2f}")
    print(f"  commission   : {row.get('commission', float('nan')):>12.2f}")
    if "funding" in row:
        print(f"  funding      : {row.get('funding', float('nan')):>12.2f}")
    if "min_pnl_seen" in row:
        print(f"  min_pnl_seen : {row.get('min_pnl_seen', float('nan')):>12.2f}")
    print(f"  shs_traded   : {row.get('shs_traded', 0):>12.0f}")
    print(f"  times_traded : {row.get('times_traded', 0):>12.0f}")
    print(f"  open_pos     : {row.get('open_pos', 0):>12.0f}")

    strat_dir = Path(CONFIG["local_base"]) / alias / group / strat
    trades_path = strat_dir / f"trades_{date}.csv"
    if not trades_path.exists():
        print(f"  [no trades file at {trades_path}]")
        return []
    df = load_trade_file(str(trades_path))
    if df is None or df.empty:
        print(f"  [trades file empty: {trades_path}]")
        return []
    df = df[df["sym"] == sym].copy()
    if df.empty:
        print(f"  [no trades for {sym} in {trades_path.name}]")
        return []

    _print_trade_drill(df, acct_closed_pnl=row.get("closed_pnl"))

    history = _load_history_acct(alias, group, strat, sym, date)
    markouts = None
    if not skip_markouts:
        markouts = _load_or_run_markouts(alias, group, strat, sym, date,
                                         offset=mo_offset)
        _print_markouts_summary(markouts, mo_offset)

    context = {"history_acct": history, "markouts": markouts,
               "mo_offset": mo_offset}
    factors = _print_diagnosis(df, row, context=context) or []
    _print_log_events(CONFIG["local_base"], alias, group, strat, sym, date)
    return factors


def cmd_drill(args):
    aliases = [args.alias] if args.alias else None
    groups = [args.group] if args.group else None
    strats = [args.strat] if args.strat else None

    acct = load_acct_data(
        CONFIG["local_base"],
        aliases=aliases,
        groups=groups,
        strats=strats,
        symbols=[args.sym],
        date_from=args.date,
        date_to=args.date,
    )
    if acct.empty:
        print(f"No acct data for {args.sym} on {args.date} "
              f"(aliases={aliases}, groups={groups}, strats={strats}).")
        return

    for _, row in acct.iterrows():
        _drill_one(row["alias"], row["group"], row["strat"],
                   args.sym, args.date,
                   skip_markouts=args.skip_markouts,
                   mo_offset=args.mo_offset)


# ── scan (flag + drill) ───────────────────────────────────────────────────────

def cmd_scan(args):
    if getattr(args, "fetch", False):
        _fetch_before_scan(args)

    if not getattr(args, "email", False):
        _run_scan(args)
        return

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        n_flagged = _run_scan(args)
    sys.stdout.write(buf.getvalue())
    if n_flagged:
        _send_scan_email(buf.getvalue(), n_flagged, args)


def _fetch_before_scan(args):
    """Pull acct/trade files for the scan window before running the scan.

    fetch_all's --days N covers `[today, today-1, …, today-(N-1)]`, while
    pta's --days N filters to `date >= today - N`, so fetch needs N+1 to
    cover the same window.
    """
    from trade_manager import fetch_all
    aliases = (args.aliases.split(",") if args.aliases
               else CONFIG["ssh_aliases"])
    pta_days = args.days if args.days else 1
    fetch_days = pta_days + 1
    print(f"Fetching {fetch_days} day(s) for aliases {','.join(aliases)}...")
    fetch_all(aliases, fetch_days, CONFIG["local_base"],
              CONFIG["remote_base"], force=False)
    print()


def _run_scan(args):
    """Run flag + drill, print the report, return number of flagged rows."""
    flagged, header = compute_flags(args)
    if flagged.empty:
        print(f"No flagged days {header}.")
        return 0

    # Re-rank by combined importance so high-$ losses with modest z aren't
    # cut off, and high-z losses with modest $ aren't either.
    flagged = flagged.copy()
    z_rank = flagged["z_net_pnl"].rank(method="min")
    pnl_rank = flagged["net_pnl"].rank(method="min")
    flagged = flagged.assign(_combined_rank=pd.concat([z_rank, pnl_rank], axis=1).min(axis=1))
    flagged = flagged.sort_values(["_combined_rank", "z_net_pnl"]).drop(columns="_combined_rank")

    cols = ["date", "alias", "group", "strat", "sym", "net_pnl",
            "z_net_pnl", "baseline_mean", "baseline_std", "n_baseline"]
    summary = flagged.copy()
    for c in ["net_pnl", "z_net_pnl", "baseline_mean", "baseline_std"]:
        summary[c] = summary[c].round(2)
    print(f"Flagged days {header}:\n")
    print(summary[cols].to_string(index=False))
    print(f"\n{len(flagged)} flagged.  Drilling top {min(args.top, len(flagged))}.")

    from collections import Counter
    label_counts = Counter()
    drilled_days = []
    for i, (_, row) in enumerate(flagged.head(args.top).iterrows(), 1):
        extra = (f"({i}/{min(args.top, len(flagged))}  "
                 f"z={row['z_net_pnl']:.2f}  net_pnl={row['net_pnl']:.2f})")
        factors = _drill_one(row["alias"], row["group"], row["strat"],
                             row["sym"], row["date"], extra_header=extra,
                             skip_markouts=args.skip_markouts,
                             mo_offset=args.mo_offset)
        labels = [f["label"] for f in factors] if factors else []
        drilled_days.append((row["date"], row["sym"], labels))
        for lbl in labels:
            label_counts[lbl] += 1

    print("\n" + "=" * 80)
    print(f"Factor frequency across {len(drilled_days)} drilled days:")
    print("=" * 80)
    if not label_counts:
        print("  (no factors fired on any day)")
    else:
        n = len(drilled_days)
        for lbl, cnt in label_counts.most_common():
            print(f"  {lbl:<18} {cnt:>3} / {n}  ({cnt/n:.0%})")
    print()
    no_factors = [(d, s) for d, s, ls in drilled_days if not ls]
    if no_factors:
        print(f"Days with no factors (diffuse loss): {len(no_factors)}")
        for d, s in no_factors[:5]:
            print(f"  {d}  {s}")

    return len(flagged)


def _send_scan_email(body, n_flagged, args):
    _strat_main = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if _strat_main not in sys.path:
        sys.path.insert(0, _strat_main)
    from util import email_utils
    subject = (f"PTA Scan — {datetime.today().strftime('%Y-%m-%d')} "
               f"({n_flagged} flagged)")
    kwargs = {"subject": subject, "body": body, "monospace": True}
    if getattr(args, "to", None):
        kwargs["receiver"] = args.to
    email_utils.send_mail(**kwargs)
    print("Report emailed.")


# ── patterns (cross-day) ──────────────────────────────────────────────────────

def _analyze_silently(alias, group, strat, sym, date, include_markouts=False,
                      mo_offset=30):
    """Run diagnosis on one (alias, group, strat, sym, date) silently.

    Returns a dict with factors + per-day metadata, or None if data missing.
    """
    acct_today = load_acct_data(
        CONFIG["local_base"],
        aliases=[alias], groups=[group], strats=[strat],
        symbols=[sym], date_from=date, date_to=date,
    )
    if acct_today.empty:
        return None
    row = acct_today.iloc[0]

    strat_dir = Path(CONFIG["local_base"]) / alias / group / strat
    trades_path = strat_dir / f"trades_{date}.csv"
    if not trades_path.exists():
        return None
    df = load_trade_file(str(trades_path))
    if df is None or df.empty:
        return None
    df = df[df["sym"] == sym].copy()
    if df.empty:
        return None
    df = df.sort_values("time_ms").reset_index(drop=True)
    df["trade_pnl"] = df["closed_pnl"].diff().fillna(df["closed_pnl"].iloc[0])

    history = _load_history_acct(alias, group, strat, sym, date)
    markouts = None
    if include_markouts:
        import contextlib
        import io as _io
        try:
            from fillstats_analysis import run_fillstats
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                results = run_fillstats(
                    CONFIG["local_base"], aliases=[alias], date_strs=[date],
                    offsets=[mo_offset], strat_filter=strat, group_filter=group,
                )
            mdf = results.get((alias, group, strat, date))
            if mdf is not None and not mdf.empty:
                mdf = mdf[mdf["sym"] == sym].copy()
                if not mdf.empty:
                    markouts = mdf
        except Exception:
            pass

    context = {"history_acct": history, "markouts": markouts,
               "mo_offset": mo_offset}
    factors = _diagnose_day(df, row, context=context)

    first = df.iloc[0]
    start_pos = float(first["end_pos"] - (first["size"] if first["side"] == "Buy" else -first["size"]))

    df["_bucket"] = df["time_str"].str[:2] + ":" + \
                    (df["time_str"].str[3:5].astype(int) // 30 * 30).astype(str).str.zfill(2)
    bucket_pnl = df.groupby("_bucket")["trade_pnl"].sum()
    worst_bucket = bucket_pnl.idxmin() if not bucket_pnl.empty else None

    return {
        "factors": factors,
        "factor_labels": [f["label"] for f in factors],
        "start_pos": start_pos,
        "end_pos": float(df["end_pos"].iloc[-1]),
        "max_abs_pos": float(df["end_pos"].abs().max()),
        "worst_bucket": worst_bucket,
        "n_fills": len(df),
    }


def _classify_start_side(start_pos, threshold=5):
    if start_pos > threshold:
        return "long"
    if start_pos < -threshold:
        return "short"
    return "flat"


def _print_pattern_block(days):
    """Print factor freq, recurring TOD, side bias, day list for a list of analyses."""
    from collections import Counter, defaultdict
    n = len(days)

    labels = Counter()
    for a in days:
        for lbl in a["factor_labels"]:
            labels[lbl] += 1
    if labels:
        print("\n  Factor frequency:")
        for lbl, cnt in labels.most_common():
            print(f"    {lbl:<18}  {cnt:>2} / {n}  ({cnt/n:.0%})")

    buckets = Counter(a["worst_bucket"] for a in days if a["worst_bucket"])
    if buckets:
        print("\n  Recurring worst-bucket (30m, of TOD where the bleed landed):")
        for bucket, cnt in buckets.most_common(6):
            print(f"    {bucket}   {cnt:>2} / {n}  ({cnt/n:.0%})")

    side_pnls = defaultdict(list)
    for a in days:
        side_pnls[_classify_start_side(a["start_pos"])].append(a["net_pnl"])
    print("\n  Side going into the day:")
    for s in ("long", "short", "flat"):
        pnls = side_pnls.get(s, [])
        if pnls:
            avg = sum(pnls) / len(pnls)
            print(f"    {s:<6}  {len(pnls):>2} / {n}   avg loss {avg:>9.2f}   "
                  f"total {sum(pnls):>10.2f}")

    print("\n  Worst days:")
    for a in sorted(days, key=lambda x: x["net_pnl"])[:10]:
        labels_str = ", ".join(a["factor_labels"]) if a["factor_labels"] else "—"
        bkt = a["worst_bucket"] or "?"
        side = _classify_start_side(a["start_pos"])
        print(f"    {a['date']}  {a['net_pnl']:>9.2f}  z={a['z_net_pnl']:>6.2f}  "
              f"start={a['start_pos']:>+7.1f} ({side})  "
              f"worst-bkt={bkt:<5}  [{labels_str}]")


def _print_pair_summary(alias, group, strat, sym, days):
    from collections import Counter
    n = len(days)
    total = sum(a["net_pnl"] for a in days)
    avg = total / n if n else 0
    labels = Counter()
    for a in days:
        for lbl in a["factor_labels"]:
            labels[lbl] += 1
    top_labels = [f"{lbl}({cnt/n:.0%})" for lbl, cnt in labels.most_common(3)]
    buckets = Counter(a["worst_bucket"] for a in days if a["worst_bucket"])
    top_bkt = buckets.most_common(1)[0] if buckets else None

    print(f"  [{alias}/{group}/{strat}] {sym}  —  {n} flagged days  "
          f"total {total:>9.2f}  avg {avg:>8.2f}")
    print(f"      factors: {', '.join(top_labels) if top_labels else '(none)'}")
    if top_bkt:
        print(f"      top bucket: {top_bkt[0]} ({top_bkt[1]}/{n})")


def cmd_patterns(args):
    aliases = args.aliases.split(",") if args.aliases else None
    strats = [args.strat] if args.strat else None
    symbols = [args.sym] if args.sym else None

    acct = load_acct_data(
        CONFIG["local_base"],
        aliases=aliases, strats=strats, symbols=symbols,
    )
    if acct.empty:
        print("No local acct data.")
        return

    acct = compute_zscores(acct, window=args.window, min_days=args.min_days)
    flagged = acct[
        (acct["z_net_pnl"] < args.z_threshold)
        & (acct["net_pnl"] < args.abs_floor)
    ].copy()
    if args.days:
        cutoff = (datetime.today() - timedelta(days=args.days)).strftime("%Y%m%d")
        flagged = flagged[flagged["date"] >= cutoff]
    if flagged.empty:
        print(f"No flagged days for filters "
              f"(z<{args.z_threshold}, net_pnl<{args.abs_floor}, "
              f"window={args.window}d).")
        return

    print(f"Analyzing {len(flagged)} flagged days "
          f"(z<{args.z_threshold}, net_pnl<{args.abs_floor}, "
          f"window={args.window}d)"
          + (" — including markouts (slower)" if args.include_markouts else "")
          + " ...", flush=True)

    analyses = []
    missing = 0
    for _, row in flagged.iterrows():
        result = _analyze_silently(
            row["alias"], row["group"], row["strat"], row["sym"], row["date"],
            include_markouts=args.include_markouts, mo_offset=args.mo_offset,
        )
        if result is None:
            missing += 1
            continue
        result.update({
            "date": row["date"], "alias": row["alias"], "group": row["group"],
            "strat": row["strat"], "sym": row["sym"],
            "net_pnl": float(row["net_pnl"]),
            "z_net_pnl": float(row["z_net_pnl"]),
        })
        analyses.append(result)

    if missing:
        print(f"  ({missing} day(s) skipped — no trades file)")
    if not analyses:
        print("No analyzable days.")
        return

    # Single-sym deep dive
    if args.sym:
        from collections import defaultdict
        by_strat = defaultdict(list)
        for a in analyses:
            by_strat[(a["alias"], a["group"], a["strat"])].append(a)
        print(f"\n{args.sym} — pattern across {len(analyses)} flagged days "
              f"({len(by_strat)} strat(s))")
        print("=" * 80)
        for (alias, group, strat), days in sorted(by_strat.items(),
                                                  key=lambda kv: -len(kv[1])):
            if len(by_strat) > 1:
                print(f"\n[{alias}/{group}/{strat}]  {len(days)} flagged days")
            _print_pattern_block(days)
        return

    # Multi-pair view: group by (alias, group, strat, sym), filter by min count
    from collections import defaultdict
    pairs = defaultdict(list)
    for a in analyses:
        pairs[(a["alias"], a["group"], a["strat"], a["sym"])].append(a)
    shown = [(k, v) for k, v in pairs.items() if len(v) >= args.min_pattern_days]
    shown.sort(key=lambda kv: (-len(kv[1]), sum(a["net_pnl"] for a in kv[1])))

    if not shown:
        print(f"\nNo (strat, sym) pair had ≥{args.min_pattern_days} flagged days "
              f"in the window.")
        print(f"({len(pairs)} pair(s) had ≥1 flagged day; pass "
              f"--min-pattern-days 1 to see them all.)")
        return

    print(f"\nMost-affected (strat, sym) pairs "
          f"(≥{args.min_pattern_days} flagged days, top {args.top_pairs}):")
    print("=" * 80)
    for (alias, group, strat, sym), days in shown[:args.top_pairs]:
        _print_pair_summary(alias, group, strat, sym, days)
    print(f"\nDeep-dive any pair with:  "
          f"pta.py patterns --sym SYM [--strat STRAT]")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="pta", description="Post Trade Analysis")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_flag_args(p):
        p.add_argument("--days", type=int, default=None,
                       help="Only show flags within last N calendar days")
        p.add_argument("--window", type=int, default=30,
                       help="Trailing window length (trading days, default 30)")
        p.add_argument("--min-days", type=int, default=10,
                       help="Min baseline rows before z is computed (default 10)")
        p.add_argument("--z-threshold", type=float, default=-2.0,
                       help="Flag when z_net_pnl < threshold (default -2.0)")
        p.add_argument("--abs-floor", type=float, default=-25.0,
                       help="Also require net_pnl < this (default -25)")
        p.add_argument("--aliases", help="Comma-separated SSH aliases (default: all)")
        p.add_argument("--strat", help="Filter to one strat")
        p.add_argument("--sym", help="Filter to one symbol")

    f = sub.add_parser("flag", help="Flag unusually-bad days per (strat, sym)")
    _add_flag_args(f)
    f.set_defaults(func=cmd_flag)

    def _add_drill_args(p):
        p.add_argument("--skip-markouts", action="store_true",
                       help="Don't compute/load fillstats markouts (faster)")
        p.add_argument("--mo-offset", type=int, default=30,
                       help="Markout offset in seconds (default 30)")

    d = sub.add_parser("drill", help="Forensic view for (date, sym)")
    d.add_argument("--date", required=True, help="YYYYMMDD")
    d.add_argument("--sym", required=True, help="Symbol, e.g. xyz:GOLD")
    d.add_argument("--alias", help="Filter to one ssh alias")
    d.add_argument("--group", help="Filter to one group")
    d.add_argument("--strat", help="Filter to one strat")
    _add_drill_args(d)
    d.set_defaults(func=cmd_drill)

    s = sub.add_parser("scan", help="Flag, then drill the top N flagged rows")
    _add_flag_args(s)
    s.add_argument("--top", type=int, default=20,
                   help="Drill into the top N flagged rows by min(z_rank, "
                        "net_pnl_rank) (default 20)")
    _add_drill_args(s)
    s.add_argument("--fetch", action="store_true",
                   help="Run trade_manager fetch_all before scanning "
                        "(pulls --days+1 of acct/trade files)")
    s.add_argument("--email", action="store_true",
                   help="Email the report (skipped when nothing is flagged)")
    s.add_argument("--to", type=str, default=None,
                   help="Override email recipient (default: pktrade group)")
    s.set_defaults(func=cmd_scan)

    pat = sub.add_parser("patterns",
                         help="Cross-day patterns across flagged losses")
    _add_flag_args(pat)
    pat.add_argument("--min-pattern-days", type=int, default=2,
                     help="Multi-pair view: min flagged days per (strat,sym) "
                          "to include (default 2)")
    pat.add_argument("--top-pairs", type=int, default=20,
                     help="Multi-pair view: max pairs to show (default 20)")
    pat.add_argument("--include-markouts", action="store_true",
                     help="Run fillstats per day (slow but enables ADVERSE SELECTION)")
    pat.add_argument("--mo-offset", type=int, default=30,
                     help="Markout offset in seconds (default 30)")
    pat.set_defaults(func=cmd_patterns)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
