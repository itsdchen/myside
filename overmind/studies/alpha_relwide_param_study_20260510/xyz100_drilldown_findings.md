# xyz:XYZ100 drilldown findings

Followup to the main study and the [fragile symbols drilldown plan](fragile_symbols_drilldown_plan.md).
This is XYZ100-specific — captures what we learned from running a heavily
iterated drilldown sweep against XYZ100 over the full Apr 1 – May 8 window.

## Context

XYZ100 was one of three "fragile" symbols flagged at the end of the main study:
- Only 1 of 135 variants passed the strict healthy filter in the v3 full-range
  sweep — narrow sweet spot.
- Picked variant (v3 #31, copied from regime_ratio) used a unique
  `place_thresh_spread_coef = 2.0` (no other symbol picked that high).
- Sharpe was 0.45, +54/d pnl over 23 days at 241 trades/day.

Fragility character: *narrow but regime-stable*. Picked variant works across
IS/OOS, but few neighbors do. Worth a wider search.

A first drilldown (May 12) widened the dim space and added `max_back_levels`,
`rung_spacing_mult`, and `trade_depth_coef` (mech 6). It found 12 healthy
variants out of 2000 — better than 1/135 but still thin, and the top variant
(52757, sharpe 0.40) actually traded less than the v3 baseline (10/day vs
241/day).

This second drilldown (May 14, cancelled at ~80% / 1606 of 2000 sims)
**widened the new mechanisms** (`vol_implied_coef`, `narrow_when_few_trades`)
and explicitly tested `ladder_one_sided=False`. Results below are based on the
partial data — full 2000 wasn't necessary to draw conclusions.

## Sweep design (drilldown v2)

20 dims, 62,208 cartesian, sample target 2000 (stopped at 1606).
Window: 23 weekdays Apr 1 – May 8, 2026.
Spec: `/home/david/scratch/alpha_relwide_autosearch/xyz100_drilldown_v2_20260514/`

The dim space refined what previous sweeps left under-explored:

| Dim | Values | Notes |
|---|---|---|
| `place_thresh` | [3e-05, 5e-05, 7e-05] | narrowed to 0.3-0.7 bps |
| `place_thresh_spread_coef` | **[0, 0.3]** | much lower than v3's 2.0 |
| `pcurvicity` | [1] | locked at picked |
| `pcurvicity_coef` | [0.4, 0.8, 2] | widened |
| `alpha_mult` | [0.5, 1.1] | bookends of prior data |
| `vol_norm_coef` | [0.5, 1.0] | |
| `pred_momentum_coef` | [0.5, 1, 2] | |
| `pred_momentum_tdc` | [30] | locked |
| `cancel_buffer_frac` | [0.25, 0.5] | |
| **`trade_depth_coef`** (mech 6, NEW) | [0, 0.01] | XYZ100-scaled (1/3 of CL) |
| `trade_depth_ramp_thr` | locked at 0 | |
| **`vol_implied_coef`** (mech 5, NEW) | [0, 1, 2] | CL-validated shape (ramp, thr=0.001) |
| `max_back_levels` | [6] locked | up from picked v3 = 3 |
| `rung_spacing_mult` | [1, 2] | |
| **`ladder_one_sided`** | **[True, False]** | first time False tested |
| **`narrow_when_few_trades`** (NEW) | True | always on |
| `narrow_fewtrade_minmultiple` | [0.5, 0.8, 1] | 1 = mechanism off in practice |
| `narrow_thresh_time_const_s` | [60, 180] | |
| `tgt_maxpos_notional` | [28000] locked at 7:1 | |

## Headline results

| Metric | Prev drilldown (v1) | **This drilldown** | v3 baseline |
|---|---|---|---|
| Variants run | 2000 | 1606 (cancelled at 80%) | 135 |
| Healthy (`pct≥0.67, +pnl, trades≥50`) | 12 | **131** | 1 |
| Healthy hit rate | 0.6% | **8.2%** | 0.7% |
| +pnl with ≥50 trades | small | **585** | n/a |
| Top sharpe | 0.40 | **0.63** | 0.45 |
| Top variant pnl/d | +71 | **+80** | +54 |
| Top variant trades (23d) | 227 | **608** | 5,543 |

**Sharpe 0.63 with +80/d pnl over 608 trades** is the strongest XYZ100
result we have. **8.2% healthy hit rate** is a 13× improvement over the
previous drilldown's regime-fragility — the picks are now genuinely robust
across the 23-day window.

## Top variant — the new "best XYZ100" config

Variant 17219 (top by sharpe):

```python
# Placement
"place_thresh": 7e-05,                    # 0.7 bps (up from picked v3 = 0.5)
"place_thresh_mode": 1,
"place_thresh_spread_coef": 0.3,          # WAY down from v3 = 2.0
"cancel_buffer_frac": 0.5,
# Curvicity
"pcurvicity": 1,
"pcurvicity_coef": 0.4,
"ladder_one_sided": False,                # NEW — two-sided wins
"alpha_mult": 0.5,                        # WAY down from v3 = 1.0 (or 1.1 prior)
# Volatility / momentum
"vol_norm_coef": 1.0,
"vol_norm_tdc": 10,
"pred_momentum_coef": 2,
"pred_momentum_tdc": 30,
"curv_impulse_coef": 0,                   # off
# Trade-depth (mech 6) — XYZ100-tuned
"trade_depth_coef": 0.01,                 # NEW
"trade_depth_ramp_thr": 0,
"trade_depth_tdc_s": 30,
"trade_depth_reference": "absolute",
# Vol-implied (mech 5) — CL-validated shape
"vol_implied_coef": 1,                    # NEW
"vol_implied_shape": "ramp",
"vol_implied_shape_thr": 0.001,
"vol_implied_horizon_s": 2,
"vol_implied_tdc_s": 30,
"vol_implied_reference": "absolute",
# Ladder
"max_back_levels": 6,                     # up from picked v3 = 3
"rung_spacing_mult": 1,
"front_rung_spacing_mult": 1.0,
"per_backlevel_rung_spacing_mult": 1.5,
# Narrow when few trades (NEW)
"narrow_when_few_trades": True,
"narrow_fewtrade_minmultiple": 0.5,       # NEW — strong narrowing
"narrow_thresh_time_const_s": 180,
"narrow_fewtrade_denom": 2,
# Sizing
"tgt_maxpos_notional": 28000,             # 7:1 ratio
"tgt_order_notional": 4000,
```

Saved at: `/home/david/scratch/alpha_relwide_autosearch/xyz100_drilldown_v2_20260514/scratch/17219/pk_full.json`

## Big shifts vs the v3 picked variant

Several dims moved meaningfully, *all consistent across the top 30 healthy
variants* (not just the top one):

| Dim | v3 picked | Top 30 modal | What changed |
|---|---|---|---|
| `alpha_mult` | 1.0 | **0.5 (28/30)** | Dampened alpha is now strongly preferred. Earlier sweeps preferred 1.1; this regime is different. |
| `place_thresh_spread_coef` | 2.0 | **0 (19/30)** | Spread term essentially disabled. The picked v3's 2.0 was an outlier all along. |
| `ladder_one_sided` | True | **False (21/30)** | Two-sided ladder beats one-sided. New finding — was locked at True universally before. |
| `pred_momentum_coef` | 2 | **0.5 / 1 (27/30)** | Weaker momentum coefficient preferred when paired with vol_implied + trade_depth + narrowing. |
| `place_thresh` | 5e-05 (0.5 bps) | **7e-05 (25/30)** | Quotes are slightly wider — 0.7 bps. |
| `max_back_levels` | 3 | **6 (locked)** | Deeper ladder. Confirmed from v1 drilldown. |
| `cancel_buffer_frac` | 0.5 | **0.5 (26/30)** | Already correct (sticky cancels). |
| `vol_norm_coef` | 0.5 | **1.0 (20/30)** | Stronger vol_norm widening. |

And the **three new mechanisms** all paid off:

| Mechanism | Off | On |
|---|---|---|
| `trade_depth_coef` | 0 (11/30) | **0.01 (19/30 — wins)** |
| `vol_implied_coef` | 0 (10/30) | **1 (15/30 — modal), 2 (5/30)** |
| `narrow_when_few_trades` (via minmultiple) | minmultiple=1 effectively off (2/30) | **0.5 or 0.8 (28/30 — wins)** |

All three on-modes win in the top 30. The combination is what unlocks the
regime.

## Interpretation

The previous picked v3 variant was a *high-frequency, no-defense* configuration:
quote tight (0.5 bps), trade aggressively (241/d), amplify alpha (mult=1.0/1.1),
one-sided ladder. It worked but with sharpe 0.45.

The new top variant is a *moderate-frequency, multi-defense* configuration:
quote slightly wider (0.7 bps), trade less (26/d), dampen alpha (mult=0.5),
two-sided ladder, AND three different widening defenses active
(vol_implied + trade_depth + narrow_when_few_trades). Sharpe 0.63 with
+80/d pnl.

The reading: **for XYZ100, layered passive defenses beat aggressive quoting.**
The strategy gives up some fill volume by quoting wider and narrowing only
when few trades fire, but the defenses keep it from getting adversely selected,
and the cleaner pnl-per-trade more than compensates.

This is a similar story arc to what `threshold_mechanisms.md` documents for
CL: the trade_depth + pred_momentum + tightened place_thresh stack
eliminates spike+runover episodes 13 → 0. For XYZ100, the relevant stack is
trade_depth + vol_implied + narrow_when_few_trades.

## Recommended next steps

1. **Promote variant 17219** as the new XYZ100 deployment config. Update
   `pk_consolidated_v3.json` → `v4.json` substituting XYZ100's trader for
   17219. Initial size_mult = 0.2 since the dim space is meaningfully
   different from anything we've put live.
2. **Validate against a holdout window** once more weekdays accrue past
   May 8. Comparing 17219 to the previous v3 pick on fresh data would tell
   us whether the new config holds up.
3. **Update `symbol_preferences.py` for `xyz:XYZ100`** with the new picked
   values + extended dim ranges so future sweeps default to the right region.
4. **Replicate the mechanism stack on SILVER and GOLD.** Both are also
   fragile (only 2 and 0 healthy in v3) and might benefit from the same
   layered-defense approach. SILVER is next on the drilldown list per
   `fragile_symbols_drilldown_plan.md`.
5. **Re-examine the sweep `sharpe` metric.** XYZ100's sweep-reported numbers
   in regime_ratio (2.64) vs full-sim daily-pnl sharpe (0.45) diverged by
   ~6×. We should understand what the sweep is actually measuring before
   trusting its absolute scale for live decisions.

## Open questions

- The previous drilldown (v1) had `pred_momentum_coef ∈ [0.5, 1, 2]` and
  top healthy preferred 1.1 — but v1 didn't include vol_implied or
  narrow_when_few_trades. Now that those are on, lower pred_momentum_coef
  wins. **Is the optimal pred_momentum_coef a function of which other
  defenses are active?** Worth a focused test once the SILVER/GOLD drilldowns
  are done.
- We locked `pcurvicity=1` for this drilldown. Top variant has pcurvicity=1
  and seems to work, but we don't know if 0.5 or 2.5 with the new mechanism
  stack would be better. Could re-open in a focused follow-up.
- The healthy hit rate of 8.2% is good but not great (compare SP500's 73/135
  ≈ 54% in v3). Could the rate go higher with even tighter dim selection? Or
  is this near the natural rate for XYZ100's microstructure?

## Paths

- Drilldown spec + results: `/home/david/scratch/alpha_relwide_autosearch/xyz100_drilldown_v2_20260514/`
- Top variant pk: `/home/david/scratch/alpha_relwide_autosearch/xyz100_drilldown_v2_20260514/scratch/17219/pk_full.json`
- Previous drilldown (v1): `/home/david/scratch/alpha_relwide_autosearch/xyz100_drilldown_20260512/`
- v3 baseline for comparison: `/home/david/scratch/alpha_relwide_autosearch/full_range_sweep_20260511/`
