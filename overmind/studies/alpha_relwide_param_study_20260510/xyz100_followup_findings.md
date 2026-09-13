## xyz:XYZ100 follow-up sweeps (A, B, C) — anchored on variant 17219

Follow-up to [`xyz100_drilldown_findings.md`](xyz100_drilldown_findings.md), run
2026-05-15. Three small targeted sweeps to test whether variant 17219 is robust,
which mechanisms in its stack are actually load-bearing, and whether the dims
locked in the drilldown were locked at sensible values.

All three phases anchored on `17219`'s `pk_full.json` and the same 23-weekday
window (Apr 1 – May 8 2026). Outputs at
`/home/david/scratch/alpha_relwide_autosearch/xyz100_followup_20260515/`.

### Headline

The anchor (variant 17219) is the top variant in every phase, with consistent
sharpe **0.61**, pnl/d **+77**, ~581 trades/day. Trades/day is much higher than
the drilldown summary reported (it said 26/d) — recomputing from the same CSVs
gives 581/d, so the drilldown's per-variant `total_trades` aggregation was
buggy. Headline sharpe and pnl/d figures from the drilldown are unaffected.

### Phase A — robustness around the anchor

3×3×3 = 27 variants. Perturbing `place_thresh ∈ {6,7,8}e-5`, `alpha_mult ∈
{0.4, 0.5, 0.6}`, `pcurvicity ∈ {0.5, 1, 2.5}`.

| Rank | place_thresh | alpha_mult | pcurvicity | sharpe | pnl/d | trd/d | pct+ |
|---|---|---|---|---|---|---|---|
| 1  | **7e-5** | **0.5** | **1**   | 0.61 | +77 | 581 | 0.74 |  ← anchor
| 2  | 7e-5     | 0.5     | 2.5     | 0.59 | +70 | 581 | 0.74 |
| 3  | 8e-5     | 0.6     | 1       | 0.56 | +54 | 506 | 0.65 |
| 4  | 8e-5     | 0.6     | 2.5     | 0.50 | +52 | 505 | 0.65 |
| 5  | 7e-5     | 0.5     | 0.5     | 0.49 | +59 | 589 | 0.70 |

**Reading:** the anchor's exact triple wins. Sharpe degrades smoothly with
perturbations (no cliff). `place_thresh=7e-5` and `alpha_mult=0.5` are
clearly the winning combination — any deviation drops sharpe by ≥0.05.
`pcurvicity=1` is best, with 2.5 a close runner-up, 0.5 noticeably worse.

### Phase B — mechanism ablation

3×3×3 = 27 variants. Sweeping `vol_implied_coef ∈ {0, 1, 2}`,
`trade_depth_coef ∈ {0, 0.01, 0.025}`, `narrow_fewtrade_minmultiple ∈
{0.5, 0.8, 1}`.

Top 8:

| Rank | vol_impl | trade_depth | narrow_min | sharpe | pnl/d | trd/d |
|---|---|---|---|---|---|---|
| 1  | 2 | 0.01 | 0.5 | 0.61 | +77 | 581 |
| 2  | 0 | 0.01 | 0.5 | 0.61 | +77 | 581 |
| 3  | 1 | 0.01 | 0.5 | 0.61 | +77 | 581 |
| 4  | 0 | 0    | 0.8 | 0.27 | +40 | 451 |
| 5  | 1 | 0    | 0.8 | 0.27 | +40 | 451 |
| 6  | 2 | 0    | 0.8 | 0.27 | +40 | 451 |
| 7  | 1 | 0    | 0.5 | 0.23 | +34 | 790 |
| 8  | 0 | 0    | 0.5 | 0.23 | +34 | 790 |

This is the most interesting phase. Three findings:

1. **`vol_implied` does nothing for XYZ100.** Top three variants differ only in
   `vol_implied_coef ∈ {0, 1, 2}` and produce *identical* sharpe, pnl/d, and
   trade count. Same flat behavior in the second cluster (vars 4-6). The
   mechanism appears to never trigger on XYZ100. This was carried over from
   CL where it helped — for XYZ100 it can be removed without loss.
2. **`trade_depth_coef = 0.01` is the dominant defense.** Turning it off drops
   sharpe from 0.61 → 0.27 (a halving). It is *the* mechanism unlocking the
   regime.
3. **`narrow_fewtrade_minmultiple` interacts with `trade_depth`.** With
   trade_depth on, 0.5 (strongest narrowing) wins. With trade_depth off, 0.8
   (gentler narrowing) wins — the more aggressive narrow=0.5 over-widens when
   trade_depth isn't there to gate it.

### Phase C — uncertain dims with new stack

3×3×3×2 = 54 variants. Re-opening `pcurvicity`, `pred_momentum_coef`,
`vol_implied_shape_thr`, `trade_depth_ramp_thr` — dims either locked in the
drilldown or whose interaction with the new mechanism stack was unclear.

Top 12:

| Rank | pcurv | pm_coef | vi_thr | td_thr | sharpe | pnl/d | trd/d |
|---|---|---|---|---|---|---|---|
| 1-3 | **1**   | **2** | {0.0005, 0.001, 0.002} | 0     | 0.61 | +77 | 581 |
| 4-6 | 2.5     | 2     | {0.0005, 0.001, 0.002} | 0     | 0.59 | +70 | 581 |
| 7-9 | 1       | 1     | {0.0005, 0.001, 0.002} | 0     | 0.57 | +77 | 631 |
| 10-12| 2.5    | 1     | {0.0005, 0.001, 0.002} | 0     | 0.53 | +70 | 628 |

Findings:

- **`vol_implied_shape_thr` does nothing**, consistent with Phase B's finding
  that `vol_implied_coef` does nothing. Three thresholds → three identical
  rows in every cluster.
- **`trade_depth_ramp_thr = 0` (always-on) clearly wins over `0.025`
  (ramped).** None of the 0.025 variants made the top 12. For XYZ100, ramping
  trade_depth hurts.
- **`pred_momentum_coef = 2` wins over 1.** Modest gap (~0.04 sharpe). The
  drilldown's pm_coef=2 lock-in was right.
- **`pcurvicity = 1` wins over 2.5** (and likely over 0.5). The drilldown's
  pcurvicity=1 lock-in was right.

### Cross-phase conclusions

1. **Anchor 17219 is validated.** It is the top variant in all three phases,
   stable to perturbations, and at the centre of clean monotonic gradients on
   the dims that matter.
2. **The mechanism stack reduces cleanly.** The drilldown stacked three new
   mechanisms (vol_implied, trade_depth, narrow_when_few_trades). Phase B
   showed that **vol_implied contributes nothing** — only trade_depth and
   narrow are doing work. The stack should be simplified to those two.
3. **Recommended live config for XYZ100** — start from 17219, then turn off
   `vol_implied_coef = 0` to reduce noise/maintenance surface area:

   ```python
   "place_thresh": 7e-05,
   "place_thresh_spread_coef": 0.3,
   "cancel_buffer_frac": 0.5,
   "pcurvicity": 1,
   "pcurvicity_coef": 0.4,
   "ladder_one_sided": False,
   "alpha_mult": 0.5,
   "vol_norm_coef": 1.0,
   "pred_momentum_coef": 2,
   "trade_depth_coef": 0.01,
   "trade_depth_ramp_thr": 0,
   "vol_implied_coef": 0,                 # was 1 in drilldown — turns out to be inert
   "max_back_levels": 6,
   "rung_spacing_mult": 1,
   "narrow_when_few_trades": True,
   "narrow_fewtrade_minmultiple": 0.5,
   "tgt_maxpos_notional": 28000,
   ```

### Loose ends

- The drilldown's `total_trades` reporting was off by ~22× for some variants.
  The per-day CSVs are correct; the aggregator likely double-counted or under-
  counted. Worth checking the `xyz100_drilldown_v2_runner.py` aggregation
  loop before relying on the trade-count column from `results.txt`.
- `pred_momentum_coef = 0.5` was not in the top 12 of Phase C, so likely worse
  than 1 and 2 — but we didn't test it head-to-head with `vol_implied_coef = 0`
  to confirm.
- Two questions from the drilldown remain open: (1) does the same mechanism
  stack help SILVER and GOLD (per `fragile_symbols_drilldown_plan.md`), and
  (2) why does the sweep's `sharpe` metric diverge from daily-pnl sharpe by
  2-14×.

### Paths

- Runner: `/home/david/scratch/alpha_relwide_autosearch/xyz100_followup_runner.py`
- Outputs: `/home/david/scratch/alpha_relwide_autosearch/xyz100_followup_20260515/{phase_a_robustness,phase_b_ablation,phase_c_uncertain}/`
- Anchor pk: `/home/david/scratch/alpha_relwide_autosearch/xyz100_drilldown_v2_20260514/scratch/17219/pk_full.json`
