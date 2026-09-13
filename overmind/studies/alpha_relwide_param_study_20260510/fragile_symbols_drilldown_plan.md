# Drill-down plan for the three fragile symbols (xyz:XYZ100, xyz:GOLD, xyz:SILVER)

Followup to the v3 full-range sweep (Apr 1 – May 8, 23 weekdays). Across the
seven symbols we searched, four (CL, SP500, COPPER, NATGAS) are deployable
at conservative size, and three are *fragile* in different ways that need
further investigation before we can confidently size them up.

This document captures the observations about each fragile symbol and the
proposed drill-down sweep for each one.

## Fragility profile comparison

| Symbol | v3 sharpe | #healthy / 135 | IS sharpe | OOS sharpe | Fragility character |
|---|---|---|---|---|---|
| xyz:XYZ100 | 0.45 | **1** | 0.45 | 0.47 | Thin sweet spot only — picked variant *is* regime-stable, but only 1 of 135 variants passes the healthy filter |
| xyz:GOLD | 0.27 | **0** | 0.27 | 0.14 | Structurally fragile — no variants pass healthy filter in 23-day window |
| xyz:SILVER | 0.18 | **2** | 0.63 | **-0.08** | Worst of both — thin AND regime-fragile; the v2 picked SILVER actively lost money OOS |

Key reading:
- **XYZ100 is the easiest case.** The picked variant works across regimes;
  we just don't have many neighbors. Suggests the dim space we swept is too
  narrow, not wrong. Likely a quick win with a wider search.
- **GOLD is the hardest.** We've already iterated four times on it
  (sweep_1 → v2 → v3 → v4) and still no variants pass the strict healthy
  filter over 23 days. Possible causes: wrong dim space, locked-too-aggressively
  on key dims, or GOLD genuinely doesn't MM well in current Hyperliquid
  conditions. Worth one more careful pass, then deprioritize if no progress.
- **SILVER is medium-hard.** The picked variant has a real IS→OOS regression
  (+0.63 → -0.08). Suggests SILVER needs regime-conditioning, not just a
  wider search. Adding `vol_widen_*` dims (which we haven't swept at all)
  is the most promising lever.

## Proposed drill-down sweeps

For each symbol, sweep widens the dim space around the v3 picked variant
plus adds dims we haven't tested. All three should run over the full
Apr 1 – May 8 window (23 weekdays) for regime stability.

### xyz:XYZ100 — widen search around picked

**Hypothesis:** the dim space is too narrow; broader sweep around the v3 pick
will surface more healthy variants without changing the basic shape.

| Dim | v3 range | Proposed |
|---|---|---|
| `place_thresh` | [4.5e-05, 5e-05, 5.5e-05] | **[4e-05, 5e-05, 6e-05, 8e-05, 1e-04]** (extend up to 10 bps) |
| `alpha_mult` | [0.9, 1.0, 1.1] | **[0.7, 0.9, 1.0, 1.1, 1.3]** |
| `pcurvicity` | [0.5, 1, 2.5] | **[0.5, 1, 1.5, 2, 2.5]** (denser) |
| `place_thresh_mode` | locked at 1 (in v3 prefs) | **[1, 2]** (test mode 2 too) |
| `tgt_maxpos_notional` | 5 values | same |
| Everything else | locked to v3 pick | locked |

Cartesian: 5 × 5 × 5 × 2 × 5 = **1,250**. Sample size 400 → 32% coverage.

### xyz:GOLD — unlock structural dims, add vol_widen

**Hypothesis:** we've been over-locking critical dims (especially the LMR
parameters), and the strategy may need adaptive width via `vol_widen_*`.

| Dim | v3 range | Proposed |
|---|---|---|
| `place_thresh` | [0.0001, 0.000125, 0.00015, ...0.00025] | keep similar, ~5 values |
| `alpha_mult` | [0.9, 1.0, 1.1] | **[0.5, 0.9, 1.0, 1.1, 2.0]** (re-test bimodal hypothesis) |
| `pcurvicity` | [1] | **[0.5, 1, 2.5]** (unlock — we've been forcing 1) |
| `lmr_time_const_s` | [4] | **[4, 10, 60]** (unlock — let it pick) |
| `lmr_local_only` | [True] | **[True, False]** (unlock — sweep_1 was 50/50) |
| `vol_widen_coef` | not swept | **[0, 0.1, 0.2]** (NEW dim) |
| `vol_widen_tdc` | not swept | **[5, 20]** (NEW dim) |
| `pred_momentum_*` | [0,1,2] × [4,30] | keep |
| `curv_impulse_*` | [0, 2] × [60] | **[0, 1, 2]** × **[20, 60]** (slightly wider) |
| `tgt_maxpos_notional` | 5 values | same |

Cartesian (rough): 5 × 5 × 3 × 3 × 2 × 3 × 2 × 3 × 2 × 3 × 2 × 5 ≈ **160k**.
That's too big for full enumeration; **sample 1500–2000** with random
sampling. Coverage ~1%, but spread across many promising regions.

### xyz:SILVER — add vol_widen, half-and-half cross-validation

**Hypothesis:** SILVER needs regime conditioning to avoid OOS bleed. The
`vol_widen_*` dims let the strategy quote wider in high-vol regimes (which
is what hurt SILVER in v2's OOS).

| Dim | v3 range | Proposed |
|---|---|---|
| `place_thresh` | [0.00018, 0.0002, 0.00022] | **[0.00015, 0.00018, 0.0002, 0.00022, 0.00025]** |
| `alpha_mult` | [1.17, 1.3, 1.43] | **[0.5, 0.9, 1.0, 1.3]** (also test lower, since v3 saw +0.63 IS / -0.08 OOS) |
| `pcurvicity` | [0.5, 1, 2.5] | same |
| `place_thresh_mode` | locked at 2 | **[1, 2]** (test mode 1 too) |
| `pcurvicity_coef` | locked at 0.25 | **[0.25, 0.5]** |
| `vol_widen_coef` | not swept | **[0, 0.1, 0.2, 0.4]** (NEW) |
| `vol_widen_tdc` | not swept | **[5, 20]** (NEW) |
| `tgt_maxpos_notional` | 5 values | same (SILVER is sharply ratio-sensitive at 10:1) |

Plus methodology: also evaluate each variant separately on the first half
(Apr 1–17) and second half (Apr 20–May 8) of the window. Variants whose
sharpe matches between halves are the regime-robust picks; pick from those
rather than from the full-window-best.

Cartesian (rough): 5 × 4 × 3 × 2 × 2 × 4 × 2 × 5 = **9,600**. Sample 1000.

## Suggested order

1. **xyz:XYZ100 first** — easiest, fastest, likely productive (1-2 day wall
   time at current pace). Confirms the methodology works for "just need more
   coverage" cases.
2. **xyz:SILVER second** — the vol_widen exploration is the most novel
   thing we'll test. Either it helps a lot or it doesn't; finding out is
   high information value.
3. **xyz:GOLD last** — most uncertain. If steps 1–2 produce strong
   improvements, do GOLD with the same playbook. If they don't, maybe GOLD
   genuinely needs different infrastructure (regime detection, time-of-day
   gates) before more parameter search will help.

Total wall time estimate: ~3 sweeps × 8–16 hrs each = ~1–2 days of compute,
spread across a few calendar days for results review and any mid-flight
adjustments.

## What to track during each drill-down

- `#healthy variants` count — primary regime-fragility metric
- IS-half vs OOS-half sharpe per variant — regime-robustness diagnostic
- Trade-rate IS vs OOS — over-fit smoking gun (if OOS trades >5× IS, over-fit)
- pct_positive over the full window
- The dim distributions in top-30 healthy — used to inform any further
  narrowing or extension

## Open questions to keep in mind

1. Is GOLD's fragility a methodology problem (too narrow / wrong dims) or
   a fundamental "this symbol doesn't work well" problem? Be ready to
   conclude the latter if drill-down #3 doesn't help.
2. For all three: how much do these symbols' results depend on the model
   bundle freshness? The bundles were built ~April 7–8. Re-building them
   before the next sweep could meaningfully change results.
3. Once we've improved picks for the three, should we re-OOS test the *whole
   portfolio* (refined v4) on a holdout window? Probably yes once we have
   data after May 11 accumulating.

## Paths

- v3 picks (current baseline): `~/scratch/alpha_relwide_autosearch/best_picks_v3.json`
- v3 consolidated pk: `~/scratch/alpha_relwide_autosearch/pk_consolidated_v3.json`
- v3 sweep dir: `~/scratch/alpha_relwide_autosearch/full_range_sweep_20260511/`
- Live deploy pk (current): `~/scratch/alpha_relwide_autosearch/pk_live_20260512.json`
- Sweep tool: `overmind/strat_main/tools/trademan/alpha_relwide_autosearch.py`
- Symbol preferences: `overmind/strat_main/tools/trademan/symbol_preferences.py`
