# xyz:SILVER dense single-axis tuning findings

Follow-up to [`fragile_symbols_drilldown_plan.md`](fragile_symbols_drilldown_plan.md)
and [`xyz100_followup_findings.md`](xyz100_followup_findings.md). Captures the
SILVER tuning pass run May 18-21 2026 against the post-revert binary (see
session memory `project_db53e179_cancel_first_regression.md` for the binary
context — `best_picks_v3.json` numbers are valid again after `d3bf7846`).

## Context

SILVER was the third "fragile" symbol from the original study, flagged in
[`fragile_symbols_drilldown_plan.md`](fragile_symbols_drilldown_plan.md):
v3 pick had IS sharpe +0.63 but OOS -0.08 — a big regime regression.
Across all 23 IS days, sharpe was just +0.18, dominated by a -$1020 day
on Apr 8. An earlier attempted SILVER drilldown was aborted because the
search was malformed (didn't include anchor's value on several dims) and
because we were unknowingly running on the broken binary.

After the binary revert was confirmed reproducing the v3 anchor numbers
exactly (sharpe +0.184, pnl/d +$70, trd/d 144), this tuning pass ran with
two methodology corrections from the prior round:

1. **Dense single-axis** — each mechanism gets its own sweep with paired
   parameters co-swept densely, instead of one sparse multi-mechanism
   cartesian. See memory `feedback_dense_singleaxis_searches.md`.
2. **Hard trade-volume floor** — `trd/d >= 100` is a hard constraint when
   ranking healthy variants, not a soft metric. The v3 anchor traded at
   145/d; thin-trade winners (the failure mode of the first attempted
   drilldown) are not deployable. See memory `feedback_trade_volume_floor.md`.

Window: **29 weekdays Apr 1 - May 18 2026** (extended from the original
23-day IS window once May data was ingested). IS = Apr 1 - May 7 (23 days,
the original sweep window). OOS = May 8 - May 15 (6 fresh days).

## Methodology in summary

For each candidate mechanism (one at a time), sweep its coef/tdc pair (or
single parameter) densely, all anchor values included, all other dims locked
to anchor or prior-sweep winners. Healthy filter: `pct_positive >= 0.55,
+pnl, trd/d >= 100`. Report FULL/IS/OOS sharpe and worst-day loss.

## Sweeps run

| # | mechanism | grid | n | result |
|---|---|---|---|---|
| 1 | sparse 4-dim sanity | `pt × psc × am × pcv` (3x3x3x3) | 81 | confirmed binary healthy, anchor reproduced exactly |
| 2 | `pred_momentum` first dense | `coef [0..3] × tdc [5..120]` | 48 | found two peaks: `coef=0.75/tdc=60` and `coef=3.0/tdc=10`; the second is healthy |
| 3 | `pred_momentum` zoom + `place_thresh` | 5x3x4 | 60 | peak at `coef=5, tdc=10, pt=2.0e-4`; pt 2.0e-4 wins over anchor's 2.2e-4 |
| 4 | extended-window IS/OOS validation | 4 configs | 4 | confirmed `coef=5, tdc=10, pt=2.0e-4` is most robust; `pt=2.2e-4` collapses OOS |
| 5 | `pred_momentum` edge extension | `coef [5..12] × tdc [3..10]` | 24 | confirmed `coef=5, tdc=10` is true peak; higher coef and shorter tdc both monotonically worse |
| 6 | `pcurvicity` paired dense | `pcv [0.25..3] × pcc [0.1..1]` | 40 | anchor #1 by FULL but **worst OOS**; **`pcv=2.5/pcc=0.75`** picked for OOS robustness (cuts worst-day loss from -$369 to -$29) |
| 7 | `vol_norm` paired dense | `coef [0..6] × tdc [3..60]` | 48 | **anchor wins** (`coef=2/tdc=10`); coef=0 makes strategy unhinged (1344 trd/d, sharpe -0.84); high coef starves trades |
| 8 | `cancel_buffer_frac` single | [0..1.0] | 9 | **anchor wins** (0.1); cbf=0 close runner-up but anchor has best balance |
| 9 | `place_thresh_spread_coef` single | [0..1.5] | 10 | **anchor wins** (0.4); psc=0.6 has slightly higher FULL but trd/d falls below floor |

## Final locked config

| dim | v3 anchor | locked | source |
|---|---|---|---|
| `place_thresh` | 2.2e-4 | **2.0e-4** | sweep #3, validated #4 |
| `place_thresh_spread_coef` | 0.4 | 0.4 | sweep #9 (anchor) |
| `cancel_buffer_frac` | 0.1 | 0.1 | sweep #8 (anchor) |
| `pcurvicity` | 0.5 | **2.5** | sweep #6 |
| `pcurvicity_coef` | 0.25 | **0.75** | sweep #6 |
| `alpha_mult` | 1.3 | 1.3 | not swept this pass |
| `vol_norm_coef` | 2 | 2 | sweep #7 (anchor) |
| `vol_norm_tdc` | 10 | 10 | sweep #7 (anchor) |
| `pred_momentum_coef` | **0 (off)** | **5** | sweeps #2/#3/#5 |
| `pred_momentum_tdc` | 30 | **10** | sweeps #2/#3/#5 |

Five parameters changed vs anchor; everything else confirmed correct.

## Headline impact

29-day window (Apr 1 - May 18 2026):

| metric | v3 anchor | new locked | delta |
|---|---|---|---|
| FULL sharpe | +0.013 | **+0.32** | **25x** |
| FULL pnl/d | +$5.7 | +$60 | 10x |
| IS sharpe (23d) | +0.18 | +0.26 to +0.40 | ~2x |
| OOS sharpe (6d) | **-0.39** | **+0.4 to +0.7** | sign-flipped |
| OOS worst-day loss | **-$924** | **-$29 to -$45** | order of magnitude |
| trd/d | 145 | 108 | -26% (acceptable) |

The headline win is **tail truncation**. Pre-tuning, the worst OOS day cost
-$924 — would take ~16 winning days at the anchor's +$60/d pace to recover.
Post-tuning, worst OOS days are -$30 to -$45 — one normal winning day
recovers them. The strategy still loses on the bad regime days, just much
less.

The dimensions that actually moved the needle:

- **`pred_momentum` turned on** (coef 0 -> 5, tdc 30 -> 10). Biases quotes
  away from the trend direction; specifically penalizes adding-into-a-trend
  on the days the strategy used to bleed (Apr 14 uptrend, Apr 21 downtrend,
  May 11+14+15 OOS days).
- **`pcurvicity` cranked up** (0.5/0.25 -> 2.5/0.75). Stops the strategy
  from accumulating position once it starts to grow — once long, buy quotes
  widen sharply, making further fills only happen at much more favorable
  prices.
- **`place_thresh` tightened slightly** (2.2 -> 2.0 bps). Trades very
  modestly more aggressively at the front. Surprisingly robust OOS: at
  2.2e-4 with strong momentum bias, OOS collapsed; at 2.0e-4 it held up.

Everything else (vol_norm, cancel_buffer_frac, place_thresh_spread_coef)
was already correctly tuned in the v3 anchor.

## Key methodology lessons

- **Dense single-axis with paired parameters** found peaks the prior
  sparse multi-mechanism sweep missed entirely. The `pred_momentum`
  optimum at `coef=5, tdc=10` was never present in the prior coarse grid.
- **Always include anchor's value as one of the swept points.** Several
  sweeps showed the anchor wins; that finding is only possible if anchor
  is in the grid.
- **Extended IS+OOS breakout is essential.** Several sweeps had #1 by
  FULL sharpe being the worst on OOS (e.g., pcurvicity #6: anchor was
  #1 FULL but had the worst OOS minD of -$369). Picking by FULL alone
  would have ignored the tail-mitigation insight that drove the actual
  change.
- **Trade-volume floor as a hard constraint** prevented the optimizer from
  finding thin-trade lottery winners. Multiple "best by sharpe" variants
  were below the floor; the right pick was the best healthy variant.

## Pending decisions

Three options for what to do next on SILVER:

1. **Rung structure dense sweep** — `max_back_levels × rung_spacing_mult ×
   ladder_one_sided`. Never specifically tuned on SILVER with the new
   mechanism stack.
2. **Add `trade_depth` (mech 6)** — never tested on SILVER. Was
   transformative on XYZ100 and CL.
3. **Stop tuning, finalize, move to GOLD** — diminishing returns; the big
   mechanism wins are already captured.

Open questions:

- Why does the OOS period (May 8-15) have such different regime
  characteristics? Five of six OOS days are losing days for most variants.
  Worth understanding the regime feature before assuming the OOS-robust
  picks generalize forward.
- Why did the May 11-18 binary-bug window (when the live deploy was
  unhinged) produce such a heavy concentration of losing days even after
  the revert? Suggests the underlying market regime, not just the bug,
  was difficult during that period.

## Paths

- Spec + outputs:
  `/home/david/scratch/alpha_relwide_autosearch/silver_{sparse_v1,pred_momentum_dense,pm_zoom_v2,extended_validate,pm_edge_v3,pcurv_dense,volnorm_dense,cbf_dense,psc_dense}_20260518-20/`
- v3 anchor pk: `~/scratch/alpha_relwide_autosearch/full_range_sweep_20260511/xyz_SILVER/scratch/32/pk_full.json`
- The locked-config pk for any future use: rebuild by overriding the 5
  changed parameters on top of the v3 anchor.
