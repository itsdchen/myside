"""
Cross-symbol diagnostic report generator.

Loads diag_analysis.json files produced by SymDiag.py and generates
a consolidated text report comparing fill quality, signal strength,
and market microstructure across symbols.

================================================================================
FULL DIAGNOSTIC WORKFLOW
================================================================================

The diagnostic pipeline has 3 layers:

  C++ (DiagnosticsMM ordex)
    -> per-fill CSV with ~40 features + markout horizons
  Python analysis (fill_diag.py)
    -> per-symbol summary JSON with binned markouts, cross-tabs, term structure
  Python report (this file)
    -> cross-symbol comparison report

--- Step 1: Run diagnostic sims ---

  Batch (recommended — runs all symbols, auto-generates cross-symbol report):

    python pybin/SymDiag.py run+analyze SILVER,GOLD,NVDA,TSLA \
        --start 20260120 --end 20260215 \
        --workdir /tmp/diag_batch

    Creates /tmp/diag_batch/{silver,gold,nvda,tsla}/ subdirectories,
    runs sims + analysis for each symbol, then auto-generates a
    cross-symbol report in /tmp/diag_batch/report/.

  Single symbol:

    python pybin/SymDiag.py run+analyze SILVER \
        --start 20260120 --end 20260215 \
        --workdir /tmp/diag_silver

  Key options:
    --horizon 30s     Markout horizon for binned analysis (default: 30s)
    --n-bins 10       Number of bins / deciles (default: 10)

--- Step 2: (Optional) Re-analyze without re-running sims ---

  Single:
    python pybin/SymDiag.py analyze --workdir /tmp/diag_silver

  Batch (auto-detects subdirectories with fill_diag CSVs):
    python pybin/SymDiag.py analyze --workdir /tmp/diag_batch --horizon 60s

--- Step 3: (Optional) Standalone cross-symbol report ---

  Only needed if you want to regenerate the report separately.
  For batch runs, the report is auto-generated in {workdir}/report/.

    python pybin/SymDiag.py report --diag-dir /tmp/diag_batch/report [-o report.txt]

  The report includes:
    1. Overview (fills, days, median markouts per symbol)
    2. Markout summary + term structure shape (GROWING/FADING/STABLE/etc.)
    3. Market microstructure (spread, vol, trade rate distributions)
    4. Signal strength matrix (which features predict markouts per symbol)
    5. Best/worst feature bins per symbol
    6. Cross-tab highlights
    7. Per-day summary
    8. Side analysis (buy vs sell skew)
    9. Premium analysis (side x premium, time-of-day, EMA magnitude)
   10. Key takeaways

--- Architecture ---

  pybin/SymDiag.py              CLI entry point (run, analyze, run+analyze, report)
  src/pktrade/ordex/
    diagnostics_mm.{h,cc}       C++ ordex that records per-fill CSVs
  overmind/strat_main/util/
    fill_diag.py                Analysis library (load CSVs, bin, cross-tab, etc.)
    diag_report.py              This file (cross-symbol report from JSONs)

--- Per-fill features recorded by DiagnosticsMM ---

  Vol EMS (|return|):     vol_ems_remote_{100ms,1s,4s,10s,30s,60s,300s}
  Momentum (side-adj):    adj_momentum_{100ms,1s,4s,10s,30s}
  Spread:                 local_spread, avg_local_spread (60s EMA)
  Premium:                adj_premium (-side * premium), premium_ema
  Book (top-of-book):     local_bb_size, local_ba_size
  Book (deep, 50bps):     deep_bid_notional, deep_ask_notional
  Book (5-tick EMS):      book_depth_bid, book_depth_ask (30s TDC, updated on trades)
  Trade micro (30s TDC):  trade_rate_ems, trade_size_ems, large_trade_rate_ems,
                          trade_depth_ems, buy_trade_depth_ems, sell_trade_depth_ems,
                          trade_notional_signed_ems, trade_notional_abs_ems
  Trailing returns (adj): adj_trailing_ret_{30s,60s,300s} (side-adjusted, exact lookback)
  Local volume:           local_vol_ems (60s TDC)
  Markout horizons:       local_mid + remote_mid at {1s,5s,30s,60s,300s} after fill

================================================================================
"""

import json
import os
from datetime import date


FEATURES = [
    "vol_ems_remote_100ms", "vol_ems_remote_1s", "vol_ems_remote_4s",
    "vol_ems_remote_30s",
    "adj_momentum_100ms", "adj_momentum_1s", "adj_momentum_4s", "adj_momentum_10s", "adj_momentum_30s",
    "rel_spread_ema", "adj_premium",
    "adj_trailing_ret_30s", "adj_trailing_ret_60s", "adj_trailing_ret_300s",
    "trade_rate_ems", "trade_notional_abs_ems", "adj_trade_notional_ems",
    "large_trade_frac", "trade_depth_ems",
    ("buy_trade_depth_ems", 1), ("sell_trade_depth_ems", -1),
    ("liq_depth_buy_100k", 1), ("liq_depth_buy_250k", 1),
    ("liq_depth_sell_100k", -1), ("liq_depth_sell_250k", -1),
    ("liq_depth_buy_100k_ems", 1), ("liq_depth_buy_250k_ems", 1),
    ("liq_depth_sell_100k_ems", -1), ("liq_depth_sell_250k_ems", -1),
    "adj_book_depth_ratio", "adj_deep_book_imbalance", "book_imbalance",
    "fill_edge_bps", "time_since_fill_ms", "vol_accel",
    "hour_et", "side",
]

FEAT_LABELS = {
    "vol_ems_remote_100ms": "vol_100ms",
    "vol_ems_remote_1s": "vol_1s",
    "vol_ems_remote_4s": "vol_4s",
    "vol_ems_remote_30s": "vol_30s",
    "adj_momentum_100ms": "mom_100ms",
    "adj_momentum_1s": "mom_1s",
    "adj_momentum_4s": "mom_4s",
    "adj_momentum_10s": "mom_10s",
    "adj_momentum_30s": "mom_30s",
    "rel_spread_ema": "ba_spread",
    "adj_premium": "adj_prem",
    "adj_trailing_ret_30s": "trail_30s",
    "adj_trailing_ret_60s": "trail_60s",
    "adj_trailing_ret_300s": "trail_300s",
    "trade_rate_ems": "trade_rate",
    "trade_notional_abs_ems": "notl_abs",
    "adj_trade_notional_ems": "adj_notl",
    "large_trade_frac": "lg_trade",
    "trade_depth_ems": "trade_depth",
    "buy_trade_depth_ems": "buy_depth",
    "sell_trade_depth_ems": "sell_depth",
    "liq_depth_buy_100k": "liq_b_100k",
    "liq_depth_buy_250k": "liq_b_250k",
    "liq_depth_sell_100k": "liq_s_100k",
    "liq_depth_sell_250k": "liq_s_250k",
    "liq_depth_buy_100k_ems": "liq_b_100k_e",
    "liq_depth_buy_250k_ems": "liq_b_250k_e",
    "liq_depth_sell_100k_ems": "liq_s_100k_e",
    "liq_depth_sell_250k_ems": "liq_s_250k_e",
    "adj_book_depth_ratio": "adj_bk_dep",
    "adj_deep_book_imbalance": "adj_deep_im",
    "book_imbalance": "book_imbal",
    "fill_edge_bps": "fill_edge",
    "time_since_fill_ms": "time_gap",
    "vol_accel": "vol_accel",
    "hour_et": "hour_et",
    "side": "side",
}


def _parse_feat_entry(entry):
    """Parse a FEATURES entry into (feat_name, result_key, label).

    Entries can be a plain string or a tuple (feature, filter_side).
    result_key is how the feature is stored in the analysis JSON's "binned" dict.
    """
    if isinstance(entry, tuple):
        feat, fside = entry
        side_label = "buys" if fside == 1 else "sells"
        result_key = f"{feat}|{side_label}"
        label = FEAT_LABELS.get(feat, feat)
    else:
        feat = entry
        result_key = feat
        label = FEAT_LABELS.get(feat, feat)
    return feat, result_key, label


def load_diag_jsons(diag_dir):
    """Load all *.json files from diag_dir, keyed by uppercase symbol name."""
    data = {}
    for f in sorted(os.listdir(diag_dir)):
        if not f.endswith(".json"):
            continue
        sym = os.path.splitext(f)[0].upper()
        with open(os.path.join(diag_dir, f)) as fh:
            data[sym] = json.load(fh)
    return data


def signal_spread(bins, key="median"):
    """Compute max-min markout across bins (signal spread in bps)."""
    vals = [b[key] for b in bins]
    return max(vals) - min(vals)


def signal_trend(bins, key="median"):
    """Classify trend shape across bins.

    Returns one of:
      UP      Higher feature value -> better markout (monotonic-ish)
      DOWN    Higher feature value -> worse markout
      PEAK    Best markout in middle bins, worse at extremes (∩-shaped, sweet spot)
      VALLEY  Worst markout in middle, better at extremes (∪-shaped)
      FLAT    Signal range too small to be meaningful (<2 bps)
      NOISY   Large signal range but no clean shape
    """
    vals = [b[key] for b in bins]
    n = len(vals)
    spread = max(vals) - min(vals)

    # Too small to classify.
    if spread < 2:
        return "FLAT"

    if n <= 3:
        inc = all(vals[i] <= vals[i+1] for i in range(n-1))
        dec = all(vals[i] >= vals[i+1] for i in range(n-1))
        if inc:
            return "UP"
        if dec:
            return "DOWN"
        return "PEAK" if vals[1] > vals[0] and vals[1] > vals[2] else "VALLEY"

    # For many bins (deciles), compare edges vs middle.
    third = max(n // 3, 1)
    top_avg = sum(vals[-third:]) / third    # high-value bins
    bot_avg = sum(vals[:third]) / third     # low-value bins
    mid_avg = sum(vals[third:-third]) / max(n - 2 * third, 1)  # middle bins

    # Check monotonic trend first.
    edge_diff = top_avg - bot_avg
    if abs(edge_diff) > spread * 0.35:
        if edge_diff > 0:
            return "UP"
        return "DOWN"

    # Check for peak (∩) or valley (∪) shape.
    edge_avg = (top_avg + bot_avg) / 2
    mid_vs_edge = mid_avg - edge_avg

    if mid_vs_edge > spread * 0.12:
        return "PEAK"
    if mid_vs_edge < -spread * 0.12:
        return "VALLEY"

    # Weak directional.
    if edge_diff > 1:
        return "UP"
    if edge_diff < -1:
        return "DOWN"

    # Large spread but no clean shape.
    return "NOISY"


def gen_report(data):
    """Generate cross-symbol diagnostic report.

    Args:
        data: dict of {symbol_name: diag_analysis_json}

    Returns:
        Report text as a string.
    """
    syms = list(data.keys())
    lines = []
    L = lines.append

    # Infer date range from first symbol's per_day data.
    first_sym = data[syms[0]]
    days = first_sym.get("per_day", [])
    if days:
        date_range = f"{days[0]['date']} to {days[-1]['date']} ({len(days)} days)"
    else:
        date_range = "unknown"

    # Infer bin count from first symbol's first binned feature.
    n_bins = "?"
    for entry in FEATURES:
        feat, rkey, _ = _parse_feat_entry(entry)
        bins = first_sym.get("binned", {}).get(rkey, [])
        if bins and feat != "side":
            n_bins = len(bins)
            break

    L("=" * 100)
    L("HIP3 DIAGNOSTIC REPORT")
    L(f"DiagnosticsMM Fill Analysis — {date_range}")
    L(f"{n_bins} bins per feature | Generated {date.today()}")
    L("=" * 100)
    L("")

    # ─── Glossary ───
    L("━" * 100)
    L("GLOSSARY")
    L("━" * 100)
    L("")
    L("Report-level terms:")
    L("  Markout        side * (local_mid_at_horizon - fill_px) / fill_px * 10000, in bps.")
    L("                 Uses local (Hyperliquid) mid — the actual market where fills happen.")
    L("                 Positive = fill was profitable vs local mid at that horizon.")
    L("  Median         Median markout across fills. Primary metric (robust to outliers).")
    L("  Mean           Mean markout. Can be skewed by a few large adverse fills.")
    L("  PctPos         Percentage of fills with positive markout.")
    L("  Signal range   max(bin_median) - min(bin_median) across deciles for a feature.")
    L("                 Measures how predictive a feature is. Larger = more useful.")
    L("  Direction      Shape of markout across feature bins:")
    L("                   UP = higher feature -> better markout (monotonic-ish)")
    L("                   DOWN = higher feature -> worse markout")
    L("                   PEAK = best markout in middle bins, worse at extremes (sweet spot)")
    L("                   VALLEY = worst markout in middle, better at extremes")
    L("                   FLAT = signal range < 2 bps, not meaningful")
    L("                   NOISY = large range but no clean directional or curved shape")
    L("  Shape          Markout term structure across horizons (1s->5s->30s->60s->300s):")
    L("                   GROWING = markout increases over time (edge persists)")
    L("                   FADING = peaks then declines (temporary edge)")
    L("                   STABLE = flat across horizons")
    L("                   NEGATIVE = negative at all horizons (adverse selection)")
    L("                   REVERTING = peaks mid-horizon then reverts (mean-reversion signature)")
    L("  AvgVol         Mean of vol_ems_remote (120s TDC) across fills that day, shown in bps.")
    L("                 Higher = more volatile oracle price that day.")
    L("")
    L("Per-fill features (recorded by DiagnosticsMM at each fill):")
    L("")
    L("  VOLATILITY (unsigned, always >= 0)")
    L("  vol_*          EMS of |remote return| at various time-decay constants (TDCs).")
    L("                 Formula: vol_ems += |return| ; vol_ems *= exp(-dt/tdc)")
    L("                 Larger value = oracle price is moving more per unit time.")
    L("                   vol_100ms  Ultra-short-term: captures sub-second price spikes, single-tick moves.")
    L("                   vol_1s     Very short-term: captures fast bursts of activity over ~1 second.")
    L("                   vol_4s     Short-term: good proxy for 'current' volatility regime.")
    L("                   vol_10s    Medium-term: smooths over short bursts.")
    L("                   vol_30s    Recent regime: primary measure of current vol environment.")
    L("                   vol_60s    Longer-term background volatility.")
    L("                   vol_300s   5-minute background: captures sustained vol shifts.")
    L("  vol_accel      vol_4s / vol_30s. Ratio of short-term to medium-term volatility.")
    L("                 >1 = vol is spiking (recent burst exceeds background), <1 = vol subsiding.")
    L("                 Useful for detecting vol regime transitions.")
    L("")
    L("  MOMENTUM (side-adjusted, signed)")
    L("  mom_*          side * EMS(signed remote return). Side-adjusted directional momentum.")
    L("                 Formula: raw_momentum += signed_return ; raw_momentum *= exp(-dt/tdc)")
    L("                 Then: adj_momentum = side * raw_momentum")
    L("                 Positive = oracle was trending in our fill direction before the fill.")
    L("                   Example: we bought (side=+1) and oracle was trending up (momentum>0).")
    L("                   If momentum persists, the future mid moves further above our fill price,")
    L("                   producing a positive markout. FAVORABLE if trend continues.")
    L("                 Negative = oracle was trending against our fill direction.")
    L("                   Example: we bought but oracle was trending down. If it continues,")
    L("                   future mid drops below our fill price. UNFAVORABLE.")
    L("                 Should POSITIVELY correlate with markouts (positive mom -> better markout).")
    L("                   mom_100ms/1s  Sub-second to very short-term directional pressure.")
    L("                   mom_4s        Short-term momentum, good for detecting fast trends.")
    L("                   mom_10s       Medium-term directional bias.")
    L("                   mom_30s       Longer-term trend direction.")
    L("")
    L("  TRAILING RETURNS (side-adjusted, signed)")
    L("  trail_*        side * (remote_mid_now - remote_mid_{N_seconds_ago}) / remote_mid_{N_seconds_ago}.")
    L("                 Exact lookback from a ring buffer of remote mid history (not EMS).")
    L("                 Positive = oracle moved in our fill direction over past N seconds.")
    L("                   Example: we bought and oracle went up over the past 30s.")
    L("                   If this trend continues, our markout is positive. FAVORABLE.")
    L("                 Negative = oracle moved against our fill direction. UNFAVORABLE.")
    L("                 Should POSITIVELY correlate with markouts (positive trail -> better markout).")
    L("                 Unlike momentum EMS, this is a precise return over a fixed window.")
    L("                   trail_30s   Return over last 30 seconds.")
    L("                   trail_60s   Return over last 60 seconds.")
    L("                   trail_300s  Return over last 5 minutes.")
    L("")
    L("  SPREAD & PREMIUM (signed where noted)")
    L("  ba_spread      EMA of (local_ask - local_bid) / local_mid, with 60s TDC.")
    L("                 Measures typical bid-ask spread on the local (Hyperliquid) book.")
    L("                 Reported in bps. Wide = illiquid conditions, likely wider quoting.")
    L("                 Sampled at fill times (biased toward conditions where fills occur).")
    L("  adj_prem       -side * (local_mid - remote_mid) / remote_mid. Side-adjusted premium.")
    L("                 Positive = trading WITH the premium (favorable for passive MM):")
    L("                   Selling when local is above oracle, or buying when local is below oracle.")
    L("                   The premium implies the fill direction, so positive = capturing the premium.")
    L("                 Negative = trading AGAINST the premium (unfavorable):")
    L("                   Buying when local is above oracle, or selling when local is below oracle.")
    L("                   We're getting filled on the wrong side of the local-oracle dislocation.")
    L("")
    L("  TRADE MICROSTRUCTURE (unsigned unless noted)")
    L("  trade_rate     EMS count of local trades, 30s TDC. Number of trades per unit time on")
    L("                 the local book. High = busy, active market with frequent executions.")
    L("  notl_abs       EMS of |trade_px * trade_qty|, 30s TDC. Total dollar volume per unit time,")
    L("                 regardless of direction. High = heavy dollar flow through the book.")
    L("  adj_notl       side * EMS(signed trade notional), 30s TDC. SIDE-ADJUSTED.")
    L("                 Signed notional: buy trades contribute +notional, sell trades contribute -notional.")
    L("                 Then multiplied by fill side.")
    L("                 Positive = net dollar flow in our fill direction.")
    L("                   Example: we bought (side=+1) and net flow is buy-heavy (+notional).")
    L("                   Other participants are aggressively buying too. If this flow is informed")
    L("                   and persistent, price continues up and our buy markout is positive. FAVORABLE.")
    L("                 Negative = net dollar flow against our fill direction. UNFAVORABLE.")
    L("                 Should POSITIVELY correlate with markouts.")
    L("  lg_trade       large_trade_rate_ems / trade_rate_ems. Fraction of local trades that are")
    L("                 >2x average size, 30s TDC. High = large players are active, potential informed flow.")
    L("  trade_depth    EMS of |trade_px - best_px| / mid on ALL local trades, 30s TDC.")
    L("                 Measures how aggressively trades sweep past the best price.")
    L("                 High = aggressive sweeping through multiple levels (institutional flow).")
    L("                 Low = trades mostly at best price (normal retail/HFT flow).")
    L("  buy_depth      EMS of sweep depth for buy-side trades only (aggressor buying, lifting asks).")
    L("                 FILTERED: only analyzed against buy fill markouts.")
    L("                 Higher = aggressive buyers sweeping deeper through the ask side.")
    L("                 If higher buy_depth predicts better buy markouts, it means aggressive")
    L("                 buying flow is informed and continues (price keeps going up after our buy fill).")
    L("  sell_depth     EMS of sweep depth for sell-side trades only (aggressor selling, hitting bids).")
    L("                 FILTERED: only analyzed against sell fill markouts.")
    L("                 Higher = aggressive sellers sweeping deeper through the bid side.")
    L("                 If higher sell_depth predicts better sell markouts, it means aggressive")
    L("                 selling flow is informed and continues (price keeps going down after our sell fill).")
    L("")
    L("  BOOK SHAPE & LIQUIDITY DEPTH")
    L("  adj_bk_dep     side * (bid_depth - ask_depth) / (bid_depth + ask_depth) within 5 ticks of best.")
    L("                 30s TDC EMS updated on trade events. SIDE-ADJUSTED near-touch liquidity imbalance.")
    L("                 Positive = more resting depth on our fill side (favorable):")
    L("                   If we bought and bid_depth > ask_depth, there is more near-touch support")
    L("                   behind our buy. If we sold and ask_depth > bid_depth, more resistance above.")
    L("                 Negative = more near-touch depth on the opposite side (unfavorable).")
    L("                 Should POSITIVELY correlate with markouts.")
    L("  adj_deep_im    side * (bid_notional - ask_notional) / (bid_notional + ask_notional) within 50bps of mid.")
    L("                 Snapshot at fill time (not smoothed). SIDE-ADJUSTED deep liquidity skew.")
    L("                 Positive = more deep liquidity on our fill side (favorable):")
    L("                   If we bought (side=+1) and bid_notional > ask_notional, there is more")
    L("                   resting support behind our buy. If price dips, deep bids cushion the move.")
    L("                   If we sold (side=-1) and ask_notional > bid_notional (raw imbalance < 0),")
    L("                   side * negative = positive: more resistance above our sell is favorable.")
    L("                 Negative = more deep liquidity on the opposite side (unfavorable).")
    L("                 Should POSITIVELY correlate with markouts.")
    L("  book_imbal     side * (top_bid_size - top_ask_size) / (top_bid_size + top_ask_size).")
    L("                 Top-of-book only, snapshot at fill time. SIDE-ADJUSTED.")
    L("                 Positive = more resting size on our fill side (favorable: cushion behind us).")
    L("                 Negative = more resting size on the opposite side (less support).")
    L("  liq_b_100k     Price depth (as fraction of mid) to sell $100k into the bid side.")
    L("  liq_b_250k     Same but $250k. Both are instantaneous snapshots at fill time.")
    L("                 FILTERED: only analyzed against buy fill markouts.")
    L("                 Measures how thin the bid side is. If we bought and the bid side is thin")
    L("                 (high depth value), there's little support below — a reversal would hit us")
    L("                 hard. Higher = thinner book = should NEGATIVELY correlate with buy markouts.")
    L("  liq_s_100k     Price depth to buy $100k from the ask side. Instantaneous at fill time.")
    L("  liq_s_250k     Same but $250k.")
    L("                 FILTERED: only analyzed against sell fill markouts.")
    L("                 Higher = thinner ask side = less resistance above. If we sold and asks are")
    L("                 thin, a bounce would push price up easily. NEGATIVELY correlates with sell markouts.")
    L("  liq_b_*_e      EMS versions of liq_b_*, 30s TDC, updated on each trade. Smoothed over")
    L("                 recent trades rather than a single snapshot. Same side-filtering logic.")
    L("  liq_s_*_e      EMS versions of liq_s_*. Same interpretation, smoothed.")
    L("")
    L("  FILL QUALITY & TIMING")
    L("  fill_edge      side * (remote_mid - fill_px) / fill_px * 10000, in bps.")
    L("                 Theoretical edge at fill time vs oracle (remote mid).")
    L("                 Positive = bought below oracle or sold above oracle (captured edge).")
    L("                 Negative = bought above oracle or sold below oracle (gave up edge).")
    L("                 This is the instantaneous edge; markout shows if it persists.")
    L("  time_gap       Time since previous fill in ms, computed per day (resets each day).")
    L("                 Short gaps = burst of fill activity, long gaps = quiet period before fill.")
    L("                 Useful for detecting clustering vs isolated fills.")
    L("  hour_et        Hour of the day in US Eastern time (0-23). Captures intraday patterns:")
    L("                 18-21=CME open, 0-3=Asia, 6-9=EU, 9-12=US open, 15-18=close.")
    L("  side           +1 = buy fill, -1 = sell fill. Binary feature.")
    L("  add_liq        1 = passive fill (added liquidity, our resting order was hit),")
    L("                 0 = aggressive fill (took liquidity, we crossed the spread).")
    L("")

    # ─── Section 1: Overview ───
    L("━" * 100)
    L("1. OVERVIEW")
    L("━" * 100)
    L("")
    header = f"{'Symbol':<8} {'Fills':>10} {'Days':>5} {'Fills/Day':>10} {'Buy%':>6} {'Sell%':>6}"
    L(header)
    L("─" * len(header))
    for sym in syms:
        d = data[sym]
        info = d["info"]
        act = d["summary"]["activity"]
        buy_pct = act["side_ratio"] * 100
        L(f"{sym:<8} {info['n_fills']:>10,} {info['n_days']:>5} {act['fills_per_day']:>10,.0f} {buy_pct:>5.1f}% {100-buy_pct:>5.1f}%")
    L("")

    # ─── Section 2: Markout Summary + Term Structure ───
    L("━" * 100)
    L("2. MARKOUT SUMMARY (bps)")
    L("━" * 100)
    L("")
    horizons = ["1s", "5s", "30s", "60s", "300s"]
    header = (f"{'Symbol':<8} {'mean_30s':>9}" +
              "".join(f"{'med_'+h:>9}" for h in horizons) +
              f"{'pct_pos_30s':>12} {'Shape':>10} {'Peak':>6}")
    L(header)
    L("─" * len(header))
    for sym in syms:
        mkts = {m["horizon"]: m for m in data[sym]["summary"]["markouts"]}
        parts = f"{sym:<8} {mkts['30s']['mean']:>9.2f}"
        for h in horizons:
            parts += f"{mkts[h]['median']:>9.2f}"
        parts += f"{mkts['30s']['pct_pos']:>11.1f}%"
        # Term structure
        ts = data[sym].get("term_structure")
        if ts:
            parts += f" {ts['shape']:>9} {ts['peak_horizon']:>5}"
        L(parts)
    L("")
    L("Shape: GROWING = markout increases with horizon, FADING = peaks early then drops,")
    L("       STABLE = roughly flat, NEGATIVE = negative at all horizons, REVERTING = peaks mid-horizon.")
    L("")

    # ─── Section 3: Market Microstructure ───
    L("━" * 100)
    L("3. MARKET MICROSTRUCTURE")
    L("━" * 100)
    L("")
    L("EMA Local Spread (60s TDC, percentiles, bps):")
    header = f"{'Symbol':<8}" + "".join(f"{'p'+str(p):>10}" for p in [10, 25, 50, 75, 90])
    L(header)
    L("─" * len(header))
    for sym in syms:
        sp = data[sym]["summary"]["spreads"].get("rel_spread_ema")
        if not sp:
            continue
        parts = f"{sym:<8}"
        for p in ["p10", "p25", "p50", "p75", "p90"]:
            parts += f"{sp[p]*10000:>10.1f}"
        L(parts)
    L("")

    L("Remote Volatility EMS 30s (percentiles, bps):")
    header = f"{'Symbol':<8}" + "".join(f"{'p'+str(p):>10}" for p in [10, 25, 50, 75, 90])
    L(header)
    L("─" * len(header))
    for sym in syms:
        vol = data[sym]["summary"]["vol"].get("vol_ems_remote_30s")
        if not vol:
            continue
        parts = f"{sym:<8}"
        for p in ["p10", "p25", "p50", "p75", "p90"]:
            parts += f"{vol[p]*10000:>10.1f}"
        L(parts)
    L("")

    # ─── Section 4: Signal Strength Matrix ───
    L("━" * 100)
    L("4. SIGNAL STRENGTH MATRIX (30s median markout spread across bins, bps)")
    L("━" * 100)
    L("")
    L(f"For each feature, all fills are split into {n_bins} bins by the feature's value at fill time.")
    L("The 30s median markout is computed within each bin. 'Spread' = max(bin_median) - min(bin_median).")
    L("A larger spread means the feature is more predictive of fill quality.")
    L("")
    L("Direction: UP = higher -> better, DOWN = higher -> worse, PEAK = middle best (∩), VALLEY = middle worst (∪), FLAT = <2bps, NOISY = no clean shape.")
    L("'side' is buy(+1) vs sell(-1) — structural, not tunable. Shows buy/sell asymmetry (Section 8).")
    L("")

    col_w = 15
    header = f"{'Feature':<14}" + "".join(f"{s:>{col_w}}" for s in syms)
    L(header)
    L("─" * len(header))
    for entry in FEATURES:
        feat, rkey, label = _parse_feat_entry(entry)
        parts = f"{label:<14}"
        for sym in syms:
            bins = data[sym]["binned"].get(rkey, [])
            if bins:
                spread = signal_spread(bins)
                direction = signal_trend(bins)
                cell = f"{spread:.1f} {direction}"
                parts += f"{cell:>{col_w}}"
            else:
                parts += f"{'N/A':>{col_w}}"
        L(parts)
    L("")
    L("Signal strength (excluding side): STRONG (>=5 bps), MODERATE (>=3 bps), WEAK (<3 bps)")
    L("")

    # ─── Section 5: Best/Worst Bins per Feature ───
    L("━" * 100)
    L("5. BEST & WORST BINS BY FEATURE (30s median markout, bps)")
    L("━" * 100)
    L("")

    for sym in syms:
        L(f"  {sym}:")
        header = f"  {'Feature':<14} {'Best Bin':<30} {'Median':>8} {'Worst Bin':<30} {'Median':>8} {'Spread':>8}"
        L(header)
        L("  " + "─" * (len(header) - 2))
        for entry in FEATURES:
            feat, rkey, label = _parse_feat_entry(entry)
            bins = data[sym]["binned"].get(rkey, [])
            if not bins:
                continue
            best = max(bins, key=lambda b: b["median"])
            worst = min(bins, key=lambda b: b["median"])
            spread = best["median"] - worst["median"]
            L(f"  {label:<14} {best['bin_label']:<30} {best['median']:>8.1f} {worst['bin_label']:<30} {worst['median']:>8.1f} {spread:>8.1f}")
        L("")

    # ─── Section 6: Cross-Tab Highlights ───
    L("━" * 100)
    L("6. CROSS-TAB HIGHLIGHTS (vol_30s x adj_trailing_ret_30s)")
    L("━" * 100)
    L("")

    for sym in syms:
        xtabs = data[sym].get("cross_tabs", [])
        vol_trail = [x for x in xtabs if x["f1"] == "vol_ems_remote_30s" and x["f2"] == "adj_trailing_ret_30s"]
        if not vol_trail:
            continue
        cells = vol_trail[0]["cells"]
        best = max(cells, key=lambda c: c["median"])
        worst = min(cells, key=lambda c: c["median"])
        spread = best["median"] - worst["median"]
        L(f"  {sym}:")
        L(f"    Best:  vol={best['f1_bin']:<25} trail={best['f2_bin']:<25} median={best['median']:>8.1f} bps  N={best['count']}")
        L(f"    Worst: vol={worst['f1_bin']:<25} trail={worst['f2_bin']:<25} median={worst['median']:>8.1f} bps  N={worst['count']}")
        L(f"    Spread: {spread:.1f} bps")
        L("")

    # ─── Section 7: Per-Day Breakdown ───
    L("━" * 100)
    L("7. PER-DAY SUMMARY (30s markout, bps)")
    L("━" * 100)
    L("")

    for sym in syms:
        days = data[sym]["per_day"]
        pos_days = sum(1 for d in days if d["markout_median"] is not None and d["markout_median"] > 0)
        total_days = len(days)
        L(f"  {sym}: {pos_days}/{total_days} positive median days ({pos_days/total_days*100:.0f}%)")
        header = f"  {'Date':<12} {'Fills':>8} {'Median':>8} {'Mean':>8} {'PctPos':>7} {'AvgVol':>10}"
        L(header)
        L("  " + "─" * (len(header) - 2))
        for d in days:
            vol_str = f"{d['avg_vol_ems']*10000:>9.1f}" if d.get("avg_vol_ems") else "      N/A"
            L(f"  {d['date']:<12} {d['n_fills']:>8} {d['markout_median']:>8.1f} {d['markout_mean']:>8.1f} {d['pct_pos']:>6.1f}% {vol_str}")
        L("")

    # ─── Section 8: Side Analysis ───
    L("━" * 100)
    L("8. SIDE ANALYSIS (30s markout, bps)")
    L("━" * 100)
    L("")
    L("Buy-side markout = fill on our bid; Sell-side = fill on our offer.")
    L("Positive buy + negative sell = remote sig leading local (expected).")
    L("")
    header = f"{'Symbol':<8} {'Buy Med':>9} {'Buy PctPos':>11} {'Sell Med':>10} {'Sell PctPos':>12} {'Side Spread':>12}"
    L(header)
    L("─" * len(header))
    for sym in syms:
        side_bins = data[sym]["binned"].get("side", [])
        sell = next((b for b in side_bins if b["bin_label"] == "-1"), None)
        buy = next((b for b in side_bins if b["bin_label"] == "1"), None)
        if sell and buy:
            spread = sell["median"] - buy["median"]
            L(f"{sym:<8} {buy['median']:>9.1f} {buy['pct_pos']:>10.1f}% {sell['median']:>10.1f} {sell['pct_pos']:>11.1f}% {spread:>12.1f}")
    L("")

    # ─── Section 9: Premium Analysis ───
    L("━" * 100)
    L("9. PREMIUM ANALYSIS")
    L("━" * 100)
    L("")
    L("How premium (local_mid - remote_mid) affects fill quality by side.")
    L("Prem Bias = median premium in bps across all fills: median((local_mid - remote_mid) / remote_mid * 10000).")
    L("Positive bias = local typically trades above oracle; negative = local typically below oracle.")
    L("Premium HELPS sells = demanding higher sell prices when local > remote is correct.")
    L("Premium HURTS buys = overpaying when local is temporarily inflated.")
    L("")
    header = f"{'Symbol':<8} {'Prem Bias':>10} {'Buy prem<0':>11} {'Buy prem>0':>11} {'Sell prem<0':>12} {'Sell prem>0':>12}"
    L(header)
    L("─" * len(header))
    for sym in syms:
        pa = data[sym].get("premium")
        if not pa:
            continue
        ps = pa.get("premium_stats", {})
        cells = {(c["side"], c["premium_sign"]): c for c in pa.get("side_x_premium", [])}
        bias = f"{ps.get('median_bps', 0):>+.1f}"
        bn = cells.get(("buy", "prem<0"), {}).get("median_markout")
        bp = cells.get(("buy", "prem>0"), {}).get("median_markout")
        sn = cells.get(("sell", "prem<0"), {}).get("median_markout")
        sp_val = cells.get(("sell", "prem>0"), {}).get("median_markout")
        bn_s = f"{bn:>+.1f}" if bn is not None else "N/A"
        bp_s = f"{bp:>+.1f}" if bp is not None else "N/A"
        sn_s = f"{sn:>+.1f}" if sn is not None else "N/A"
        sp_s = f"{sp_val:>+.1f}" if sp_val is not None else "N/A"
        L(f"{sym:<8} {bias:>10} {bn_s:>11} {bp_s:>11} {sn_s:>12} {sp_s:>12}")
    L("  (values are 30s median markout in bps)")
    L("")

    # ─── Section 10: Key Takeaways ───
    L("━" * 100)
    L("10. KEY TAKEAWAYS")
    L("━" * 100)
    L("")

    # Rank symbols by median markout
    sym_mkts = []
    for sym in syms:
        m30 = next(m for m in data[sym]["summary"]["markouts"] if m["horizon"] == "30s")
        sym_mkts.append((sym, m30["median"], m30["mean"], data[sym]["info"]["n_fills"]))
    sym_mkts.sort(key=lambda x: x[1], reverse=True)

    L("  Symbols ranked by 30s median markout:")
    for rank, (sym, med, mean, fills) in enumerate(sym_mkts, 1):
        status = "POSITIVE" if med > 0 else "NEGATIVE"
        ts = data[sym].get("term_structure", {})
        shape = ts.get("shape", "?") if ts else "?"
        L(f"    {rank}. {sym:<8} med={med:>+6.2f}  mean={mean:>+8.2f} bps  ({fills:,} fills)  [{status}] [{shape}]")
    L("")

    # Find strongest signal per symbol (best feature for each symbol).
    # "Signal range" = max(bin_median) - min(bin_median) across deciles.
    # A larger range means the feature is more predictive of fill quality for that symbol.
    L("  Most predictive feature per symbol (best signal range across deciles):")
    L("  (Signal range = best_bin_median - worst_bin_median in bps)")
    L("")
    best_per_sym = {}
    for sym in syms:
        best_feat, best_spread = None, 0
        for entry in FEATURES:
            feat, rkey, label = _parse_feat_entry(entry)
            bins = data[sym]["binned"].get(rkey, [])
            if bins and feat != "side":
                spread = signal_spread(bins, key="median")
                if spread > best_spread:
                    best_spread = spread
                    best_feat = label
        if best_feat:
            best_per_sym[sym] = (best_feat, best_spread)
    for rank, (sym, (feat, spread)) in enumerate(
            sorted(best_per_sym.items(), key=lambda x: x[1][1], reverse=True), 1):
        L(f"    {rank}. {sym:<8} {feat:<14} {spread:>8.1f} bps range")
    L("")

    # Also show top 10 feature-symbol pairs across all symbols (deduplicated: max 2 per symbol).
    L("  Top feature-symbol pairs (max 2 per symbol):")
    all_signals = []
    for sym in syms:
        for entry in FEATURES:
            feat, rkey, label = _parse_feat_entry(entry)
            bins = data[sym]["binned"].get(rkey, [])
            if bins and feat != "side":
                spread = signal_spread(bins, key="median")
                direction = signal_trend(bins)
                all_signals.append((sym, label, spread, direction))
    all_signals.sort(key=lambda x: x[2], reverse=True)
    sym_count = {}
    shown = 0
    for sym, feat, spread, direction in all_signals:
        if sym_count.get(sym, 0) >= 2:
            continue
        sym_count[sym] = sym_count.get(sym, 0) + 1
        shown += 1
        L(f"    {shown:>2}. {sym:<8} {feat:<14} {spread:>8.1f} bps  {direction}")
        if shown >= 10:
            break
    L("")

    L("=" * 100)
    L("END OF REPORT")
    L("=" * 100)

    return "\n".join(lines)
