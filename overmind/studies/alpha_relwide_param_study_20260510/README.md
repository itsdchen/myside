# Alpha RelWide MM Parameter Study

**Date range:** 2026-05-01 → 2026-05-10
**Author:** itsdchen
**Strategy:** `alpha_rel_wide_mm` ordex (HIP3 vs CME pairs, mostly commodities + 2 equity indices)
**Tool:** `overmind/strat_main/tools/trademan/alpha_relwide_autosearch.py`

This is a detailed log of a multi-day parameter search across 7 HIP3 symbols
trading against CME futures. The goal was to find production-quality parameter
sets per symbol, document the search methodology, and capture lessons that
should shape how we run future sweeps for this ordex (and possibly others).

The study includes:
- The final per-symbol parameter profiles (promoted into the consolidated pk).
- Per-symbol parameter cliffs and sweet spots, with concrete data tables.
- Cross-symbol patterns (where symbols agree, where they diverge).
- The mistakes we made narrowing the search and how we recovered.
- A short recipe for running the next sweep more efficiently.

**Companion files:**
- `recommendations.md` — meta-recommendations from the whole experience: what
  worked, what hurt us, process / deployment / tooling rules to keep going
  forward.
- `best_picks_summary.csv` — final picks (v1) with full params per symbol.
- `sweep_summary.csv` — per-sweep / per-symbol stats across all the sweeps
  in this series.

**Later artifacts (live outside the repo for size reasons):**
- v3 picks (from the regime-mixed full-range sweep, currently best):
  `~/scratch/alpha_relwide_autosearch/pk_consolidated_v3.json`
- v3 manifest: `~/scratch/alpha_relwide_autosearch/best_picks_v3.json`
- v3 sweep dir with SUMMARY.md: `~/scratch/alpha_relwide_autosearch/full_range_sweep_20260511/`
- OOS sim dir with SUMMARY.md: `~/scratch/alpha_relwide_autosearch/oos_v2_20260510/`
- Full-sim dir with SUMMARY.md: `~/scratch/alpha_relwide_autosearch/full_sim_v2_20260511/`

## TL;DR

We ran six sweeps across 7 symbols, ending with sharpe ≥ 1.29 for every symbol.

| Symbol | Best sharpe | Best avg_pnl | Trades | Source sweep |
|---|---|---|---|---|
| xyz:COPPER | 3.34 | +66 | 66 | sweep_b |
| xyz:NATGAS | 2.45 | +78 | 120 | sweep_b |
| xyz:GOLD | 1.99 | +92 | 58 | gold_v4 |
| xyz:SILVER | 1.57 | +162 | 129 | sweep_a |
| xyz:XYZ100 | 1.49 | +58 | 160 | sweep_b |
| xyz:SP500 | 1.45 | +88 | 179 | sweep_b |
| xyz:CL | 1.29 | +556 | 64 | sweep_b |

Compared to the initial broad sweep (sweep_1):

| Symbol | sweep_1 best sharpe | final best sharpe | Improvement |
|---|---|---|---|
| xyz:CL | 0.28 (top healthy) | 1.29 | **+0.81** |
| xyz:COPPER | 0.88 | 3.34 | **+2.46** |
| xyz:GOLD | 0.35 | 1.99 | **+1.64** |
| xyz:NATGAS | 0.85 | 2.45 | **+1.60** |
| xyz:SILVER | 1.07 | 1.57 | **+0.50** |
| xyz:SP500 | 0.51 | 1.45 | **+0.94** |
| xyz:XYZ100 | 0.73 | 1.49 | **+0.76** |

The biggest single source of gain across all symbols was **concentrating the
sample budget into the working region of the parameter space** rather than
discovering new parameter values. The first sweep sampled 800 of ~1M variants
per symbol (≈ 0.08%); the focused sweeps sampled 800 of ~50k–200k (≈ 0.5%–4%).
The denser sampling alone, with no new dim ranges, accounted for most of the
sharpe lift.

GOLD was the exception — its best variant was at `place_thresh = 1.25 bps`,
a value that wasn't even in the original sweep universe. We had to extend
the place_thresh range twice (down to 1 bp) before finding it.

## Artifacts

- `pk_consolidated.json` (in `~/scratch/alpha_relwide_autosearch/`) — 7-trader
  pk with the best variant per symbol, all `enabled=False` for safety.
- `best_picks_so_far.json` — manifest with provenance per symbol (variant id,
  sweep label, results path, full param set).
- `gold_v3_healthy.csv`, `pct_pos_67.csv` — exported subsets of variants for
  external analysis.
- `symbol_preferences.py` (in `overmind/strat_main/tools/trademan/`) — the
  per-symbol learned dim overrides; consumed by the autosearch generator
  via `_apply_symbol_preferences`. Used by default; opt out with `--no-preferences`.

## Symbol scope

We searched 7 symbols. The wiring (in `overmind/strat_main/util/symbolizer.py`,
`relwide_remote_wiring`) routes each xyz: HIP3 symbol to a CME counterpart:

| HIP3 symbol | Remote (CME) | Class |
|---|---|---|
| xyz:CL | CL | crude oil |
| xyz:NATGAS | NG | natural gas |
| xyz:COPPER | HG | copper |
| xyz:GOLD | GC | gold |
| xyz:SILVER | SI | silver |
| xyz:SP500 | ES | equity index |
| xyz:XYZ100 | NQ | equity index |

We initially asked about including PLATINUM and PALLADIUM, but the system
doesn't ingest CME PL/PA market data (they are pyth-only routed via
`TopBookEquity`), so they were dropped from this study.

We initially considered BRENTOIL (BZ) too but dropped it on the user's
request. Future sweeps could revisit.

## Sweep methodology

Each sweep produces a per-symbol `range_dims` list, a base config (a real pk
with normalized model bundle pred/ref signals), and runs `sim_grid` over a
random sample. Healthy variants are filtered with:

```
pct_positive >= 0.67 AND avg_pnl > 0 AND num_trds >= 50  (production tradeable)
pct_positive >= 0.67 AND avg_pnl > 0 AND num_trds >= 20  (looser exploration)
```

Variants are ranked by `sharpe` (not by `sim_score`, which can crown bleeders —
see process learnings below).

Sample sizes were typically 800–3200 per symbol over an 11-day window
(`sim_days = 14` with date trimming). The maximum cardinality we searched in
a single symbol was ~967k (sweep_1's full grid for some symbols); the smallest
was ~7k (sweep_a refine for CL).

## Sweep timeline

### sweep_1 — initial broad sweep
- **2026-05-01 / 2026-05-02**
- 7 symbols at `core,vol_norm` presets, sample_size 800, sim_days 14.
- Cartesian per symbol: ~967k. Coverage: ~0.08%.
- Result: most symbols got a "best" variant at sim_score top, but several had
  bad pnl/sharpe (CL: −40 pnl, sharpe -0.04). NATGAS crashed entirely
  ("Symbol (Hyperliquid, xyz:NATGAS) not found in secmaster") because the
  pktrade binary was built before NATGAS was added to `HyperliquidSec.h`.
  Recovered by a fresh build.
- After binary rebuild: NATGAS got its own rerun and produced a clean sharpe
  0.85 / +118 pnl best.

### sweep_a — focused refine for CL and SILVER
- **2026-05-03**
- After analyzing sweep_1's healthy distributions for CL and SILVER, we
  narrowed dim ranges to top-leaning values from the data.
- 2 symbols at custom narrowed `range_dims`, sample_size 800, sim_days 14.
- Cartesian per symbol: 7,776 (CL) / 19,440 (SILVER).
- Result: CL improved from sim_score-best with -40 pnl → sharpe 0.79 with
  +364 pnl. SILVER improved from 1.07 → 1.57 sharpe.
- Key insight from the analysis: the CL Sweep 1 winner was a high-trade
  bleeder; tightening place_thresh and locking lmr_local_only=True produced
  a 65-trade variant with +291 pnl.

### sweep_b — full 7-symbol sweep with momentum
- **2026-05-03 / 2026-05-06**
- Per-symbol narrowed ranges via a new `symbol_preferences.py` file (the
  preferences plumbing was added during this sweep).
- All 7 symbols simultaneously, with `core,vol_norm,momentum` presets.
- Added `pred_momentum_coef`, `pred_momentum_tdc`, `curv_impulse_coef`,
  `curv_impulse_tdc` for the first time.
- Sample size 1500 (auto-bumped to 1600 effective per `min(max_variants,
  sample_size)`), sim_days 14.
- Cartesian per symbol: 35k–166k.
- Result: 5 of 6 (excluding GOLD) showed major sharpe improvements. SILVER
  regressed from 1.57 → 1.03 because vol_norm_tdc was paired [4,40] instead
  of [4,10] (10 was secretly the strong value at SILVER). GOLD was killed
  early because its first 42% looked terrible (only 2 healthy).
- COPPER produced the best result of the entire study: sharpe 3.34.

### natgas_rerun — quick fix after binary rebuild
- **2026-05-02**
- Single-symbol sweep launched right after `pktrade` was rebuilt to include
  the new HyperliquidSec.h NATGAS entry.
- Used the same dim universe as sweep_1.
- 800 samples, sim_days 14.

### silver_rerun — single-value vol_norm_tdc=[10] retry
- **2026-05-06**
- After sweep_b's SILVER regression, narrowed `vol_norm_tdc` to `[10]`
  (single value).
- Result: 1.26 sharpe (vs sweep_a's 1.57). Improved on sweep_b's 1.03 but
  didn't recover sweep_a's peak. Likely because SILVER's best was found by
  random luck in sweep_a; we couldn't find that exact corner of the space
  with limited samples.
- Decision: keep sweep_a's variant 18810 as SILVER's pick.

### gold_v2 / gold_v3 / gold_v4 — GOLD-specific reruns
- **2026-05-04 → 2026-05-10**
- GOLD turned out to be the hardest symbol. Three sequential reruns were
  needed to find a good config.
- v2: Used `symbol_preferences.py` defaults — sharpe 0.13, only 2 healthy
  variants. Worse than sweep_1.
- v3: Restored `lmr_local_only=[True]`, narrowed `place_thresh` to
  [0.0002, 0.00025, 0.0004] with extension target 1.75 bps planned. Sharpe
  1.43 / 36 trades (improvement, but at low frequency).
- v4: Extended `place_thresh` down to 1 bp ([0.0001, 0.000125, 0.00015,
  0.000175, 0.0002, 0.00025]), restored `cancel_buffer_frac=[0.1, 0.25, 0.5]`,
  restored `vol_norm_tdc=[4, 10]`, bumped sample_size to 3200.
- Result: sharpe 1.99 at 58 trades. Broke the sharpe/frequency tradeoff
  that the earlier reruns hit (where high-sharpe was always low-freq).

## Per-symbol parameter profiles

The picked variant for each symbol is in `pk_consolidated.json`. Below is
each profile with our reading of *why* the dims look the way they do.

### xyz:CL — "tight quotes, dampened alpha, slow vol_norm"

| Dim | Value | Notes |
|---|---|---|
| place_thresh | 0.0003 (3 bps) | Extended down from sweep_1's 4-15 bps range |
| place_thresh_mode | 1 (with-spread additive) | |
| place_thresh_spread_coef | 0.75 | |
| cancel_buffer_frac | 0.1 (responsive) | Sweep_1 dist favored 0.1 |
| lmr_time_const_s | 60 (slow LMR) | Universal across symbols except GOLD |
| lmr_local_only | True | |
| pcurvicity | 0.5 (low) | |
| pcurvicity_coef | 0.5 (low) | |
| alpha_mult | 0.7 (dampened) | |
| vol_norm_coef | 2 | |
| vol_norm_tdc | 40 (slow) | |
| pred_momentum_coef | 1 | Pred_momentum helps strongly for CL |
| pred_momentum_tdc | 30 | |
| curv_impulse_coef | 0 (off) | Curv_impulse hurts CL |
| curv_impulse_tdc | 60 | |

**Story:** Sweep_1 sampled CL across `place_thresh ∈ [1, 6] bps` and the
random sampling picked a high-trade-count bleeder. Tightening the search
to 4 bps and below (sweep_a, then sweep_b) and including `place_thresh=3 bps`
produced the +556 pnl / 1.29 sharpe winner. Single biggest leverage: dropping
place_thresh values ≥ 4 bps once we saw the top-30 distribution clustered
at the lower end.

### xyz:COPPER — "tight quotes, sticky orders, high pcurvicity"

| Dim | Value | Notes |
|---|---|---|
| place_thresh | 0.00021516 (~2.15 bps) | Symstats-derived |
| place_thresh_mode | 1 | |
| place_thresh_spread_coef | 0.5 | |
| cancel_buffer_frac | 0.75 (very sticky) | Lowest-churn |
| lmr_time_const_s | 60 | |
| lmr_local_only | True | |
| pcurvicity | 2.5 (high — opposite of CL) | |
| pcurvicity_coef | 2 (high) | |
| alpha_mult | 0.9 | |
| vol_norm_coef | 1 | |
| vol_norm_tdc | 10 | |
| pred_momentum_coef | 1 | |
| pred_momentum_tdc | 30 | |
| curv_impulse_coef | 0 (off) | |
| curv_impulse_tdc | 60 | |

**Story:** COPPER produced the highest sharpe in the study (3.34) — but only
at 66 trades. The high `pcurvicity` (2.5) gives a steep edge curve, and the
sticky `cancel_buffer_frac=0.75` avoids churn. Note: COPPER's overall
sweep_b distribution showed `curv_impulse_coef=2` winning across 22/30 of
healthy variants, but the sharpe-best variant has `curv_impulse_coef=0`. The
high-sharpe regime is in the low-trade subset; curv_impulse helps in the
mid-trade regime where it isn't winning sharpe-wise.

### xyz:GOLD — "very tight quotes, fast LMR, both momentum dims on"

| Dim | Value | Notes |
|---|---|---|
| place_thresh | 0.000125 (1.25 bps) | Tightest of all 7 picks |
| place_thresh_mode | 1 | |
| place_thresh_spread_coef | 1.0 | |
| cancel_buffer_frac | 0.25 | |
| lmr_time_const_s | **4 (FAST — unique to GOLD)** | All other symbols want 60 |
| lmr_local_only | True | |
| pcurvicity | 1 | |
| pcurvicity_coef | 0.75 | |
| alpha_mult | 1.0 | |
| vol_norm_coef | 0.5 (low) | |
| vol_norm_tdc | 10 | |
| pred_momentum_coef | 1 | |
| pred_momentum_tdc | 30 | |
| curv_impulse_coef | 2 (on) | |
| curv_impulse_tdc | 60 | |

**Story:** GOLD took 4 sweep iterations (the most of any symbol). The defining
features:
- It's the only symbol that prefers fast `lmr_time_const_s=4` instead of 60.
  We discovered this in sweep_1's healthy distribution (26/30 had value 4).
- Its peak `place_thresh` is well below all other symbols at 1.25 bps. This
  was *not* in the original sweep universe; we only found it after extending
  the range down twice.
- The earlier reruns (gold_v2, gold_v3) failed because the param narrowing
  inherited from a misreading of the distribution: locked
  `lmr_local_only=[True]` (sweep_1 had 16 True / 14 False, basically tied),
  dropped `cancel_buffer_frac=0.1` (the strongest value at 19.7% healthy),
  locked `vol_norm_tdc=[10]` (eliminated the high-trade-rate `4` option).
  Restoring all three in v4 plus extending to 1 bp produced the breakthrough.

### xyz:NATGAS — "tightest quotes, alpha damped, no pred_momentum"

| Dim | Value | Notes |
|---|---|---|
| place_thresh | 0.00011636 (~1.16 bps) | Symstats-derived; tightest of all |
| place_thresh_mode | 1 | |
| place_thresh_spread_coef | 1.0 | |
| cancel_buffer_frac | 0.75 (very sticky) | |
| lmr_time_const_s | 60 | |
| lmr_local_only | True | |
| pcurvicity | 1 | |
| pcurvicity_coef | 0.75 | |
| alpha_mult | **0.5 (heavily dampened)** | Most dampened of all 7 |
| vol_norm_coef | 1 | |
| vol_norm_tdc | 40 (slow) | |
| pred_momentum_coef | **0 (OFF)** | Only symbol where pred_momentum is off |
| pred_momentum_tdc | 30 | |
| curv_impulse_coef | 2 (on) | |
| curv_impulse_tdc | 60 | |

**Story:** The only symbol whose best variant has pred_momentum off. NATGAS
quotes very tight (1.16 bps) and wants the alpha signal heavily dampened.
The model bundle's raw alpha would push it around too much; the strategy
works best as a near-passive MM. Curv_impulse helps (the mid-bar curvicity
impulse fires often in nat gas).

### xyz:SILVER — "tight quotes, low curvicity, mode 2 (only one)"

| Dim | Value | Notes |
|---|---|---|
| place_thresh | 0.0002 (2 bps) | |
| place_thresh_mode | **2 (with-spread-EMA)** | Only symbol using mode 2 |
| place_thresh_spread_coef | 0.4 | |
| cancel_buffer_frac | 0.1 (responsive) | |
| lmr_time_const_s | 60 | |
| lmr_local_only | True | |
| pcurvicity | 0.5 (low) | |
| pcurvicity_coef | 0.25 (low) | |
| alpha_mult | **1.3 (slightly amplified)** | Only symbol > 1.0 |
| vol_norm_coef | 2 | |
| vol_norm_tdc | 10 | |
| **(no momentum dims)** | — | Pick from sweep_a (pre-momentum) |

**Story:** SILVER's best was found in sweep_a, before we added momentum dims.
Subsequent reruns with momentum included didn't beat it — partly because
sample randomness, partly because we mis-narrowed `vol_norm_tdc` to `[4, 40]`
in sweep_b (10 was the secretly-strong value). It's the only symbol using
`place_thresh_mode=2` (spread EMA) and the only one with `alpha_mult > 1`.
The combination reads as: dampen everything except alpha, keep cancels
responsive, and let the spread-ema set the placement floor.

### xyz:SP500 — "very tight quotes, strong pred_momentum, slow vol_norm"

| Dim | Value | Notes |
|---|---|---|
| place_thresh | 5e-05 (0.5 bps) | Equity-index pattern |
| place_thresh_mode | 1 | |
| place_thresh_spread_coef | 0.4 | |
| cancel_buffer_frac | 0.75 (sticky) | |
| lmr_time_const_s | 60 | |
| lmr_local_only | True | |
| pcurvicity | 0.5 | |
| pcurvicity_coef | 2 | |
| alpha_mult | 1.0 | |
| vol_norm_coef | 1 | |
| vol_norm_tdc | 40 (slow) | |
| pred_momentum_coef | **2 (strong)** | |
| pred_momentum_tdc | 30 | |
| curv_impulse_coef | 0 (off) | |
| curv_impulse_tdc | 60 | |

**Story:** equity-index pattern, deep CME ES book → tight quotes (0.5 bps)
required to be competitive. Pred_momentum at coef=2 is the biggest single
alpha contributor. Slow vol_norm because intra-day vol fluctuates a lot
and chasing it produces churn.

### xyz:XYZ100 — "very tight quotes, strong pred_momentum, dynamic spread coef"

| Dim | Value | Notes |
|---|---|---|
| place_thresh | 5e-05 (0.5 bps) | Same as SP500 |
| place_thresh_mode | 1 | |
| place_thresh_spread_coef | **2.0 (high)** | Unique among picks |
| cancel_buffer_frac | 0.5 | |
| lmr_time_const_s | 60 | |
| lmr_local_only | True | |
| pcurvicity | 1 | |
| pcurvicity_coef | 0.25 (low) | |
| alpha_mult | 1.0 | |
| vol_norm_coef | 0.5 (low) | |
| vol_norm_tdc | 10 | |
| pred_momentum_coef | **2 (strong)** | |
| pred_momentum_tdc | 30 | |
| curv_impulse_coef | 0 (off) | |
| curv_impulse_tdc | 60 | |

**Story:** like SP500 but with much higher `place_thresh_spread_coef=2.0`
(vs SP500's 0.4). XYZ100 wants its placement threshold to widen
proportionally with current spread; SP500 wants a more constant offset.
Most active variant in the picks (160 trades, 11d).

## Cross-symbol patterns

### Universal across all 7 picks

| Dim | Value | Reading |
|---|---|---|
| `lmr_local_only` | True | Local-only LMR is consistently better for HIP3-vs-CME pairs. |
| `ladder_one_sided` | True | One-sided ladder dominates for everyone. |

### Strong consensus (6/7)

| Dim | Common value | Outlier |
|---|---|---|
| `lmr_time_const_s` | 60 (slow LMR) | GOLD: 4 (fast) |
| `place_thresh_mode` | 1 (instantaneous spread) | SILVER: 2 (spread EMA) |

### Bimodal: two camps on momentum mechanism

| Camp A: pred_momentum on, curv_impulse off | Camp B: curv_impulse on, mild/no pred_momentum |
|---|---|
| CL (pmcf=1, cicf=0) | GOLD (pmcf=1, cicf=2) |
| COPPER (pmcf=1, cicf=0) | NATGAS (pmcf=0, cicf=2) |
| SP500 (pmcf=2, cicf=0) | |
| XYZ100 (pmcf=2, cicf=0) | |
| (SILVER unswept on these dims) | |

The camps don't fall on commodity-vs-equity lines. CL/COPPER are commodities
in Camp A; GOLD/NATGAS are commodities in Camp B; SP500/XYZ100 are equity
indices in Camp A. The split is more about microstructure: GOLD and NATGAS
get value from the curvicity impulse fire, while the others get value from
the predicted-momentum signal.

### Per-symbol unique characteristics

| Symbol | Unique trait | Most likely reason |
|---|---|---|
| GOLD | only `lmr_time_const_s=4` | Faster LMR fits GOLD's mid quote dynamics; mid bar moves correlate over short windows but de-correlate over longer ones for GC. |
| SILVER | only `place_thresh_mode=2` | SI's spread is more bursty than smooth — spread EMA gives a more stable placement reference than instantaneous spread. |
| SILVER | only `alpha_mult > 1.0` | SI's regressed alpha may be under-scaled given the relative-pair signal strength here. |
| NATGAS | most dampened `alpha_mult=0.5` | NG has very high realized volatility per signal unit; dampening is required to avoid being whipsawed. |
| NATGAS | only pred_momentum off | Pred momentum likely double-counts NG's already-strong directional regime, hurting fills. |
| XYZ100 | high `place_thresh_spread_coef=2.0` | NQ's spread varies more session-to-session than ES's, so a proportional-to-spread threshold pays off. |

## Key findings

### Place_thresh has hard cliffs, not gentle slopes

GOLD's healthy% by place_thresh from sweep_1 (with the `pct_pos≥0.67 AND
+pnl AND num_trds≥20` filter):

| place_thresh | n | %healthy | %no-trade | %losing | %mid |
|---|---|---|---|---|---|
| 2.00 bp | 99 | 43.4% | 0% | **25.3%** | 31.3% |
| 2.50 bp | 84 | 47.6% | 0% | 11.9% | 40.5% |
| **3.00 bp** | 95 | **53.7%** | 0% | 0% | 46.3% |
| 4.00 bp | 106 | 0.9% | 0% | 0% | 99.1% |
| 5.00 bp | 94 | 0% | 0% | 1.1% | 98.9% |
| 6.50 bp | 123 | 0% | 62.6% | 1.6% | 35.8% |
| 8.00 bp | 92 | 0% | **91.3%** | 0% | 8.7% |
| 10.00 bp | 107 | 0% | **100%** | 0% | 0% |

Reading:
- Below 3 bps: trades, sometimes loses (adverse selection).
- 3 bps: sweet spot — trades enough, doesn't lose.
- 4–5 bps: trades occasionally but rarely (`%mid` 99% means
  `5 ≤ num_trds < 20`).
- 6.5+ bps: quotes too wide to fill — `no-trade` dominates.

The jump from 3 → 4 bps drops healthy% from 53.7% to 0.9%. This isn't a
gradual decay; it's a phase transition. The strategy stops trading enough
to qualify as healthy.

This pattern recurred across symbols. SP500 and XYZ100 both peak at 0.5 bps
(the lowest value in their range). Their data also shows similar cliffs,
just at lower thresholds. NATGAS and COPPER peak just above their symstats-
derived `place_thresh` (~1.2 and ~2.2 bps respectively).

### High-sharpe / high-frequency tradeoff (and how GOLD broke it)

In most sweeps, the sharpe-best variant for a symbol was at a particular
trade count, and pushing trade count higher dropped sharpe. Example: GOLD v3:

| ≥ trades | best variant sharpe |
|---|---|
| 20 | 1.43 |
| 30 | 1.43 |
| 50 | 0.38 |
| 75 | none |
| 100 | none |

The high-sharpe regime was *strictly* low-frequency (≤38 trades).

GOLD v4 broke this:

| ≥ trades | best variant sharpe |
|---|---|
| 20 | 1.99 |
| 30 | 1.99 |
| **50** | **1.99** |
| 75 | 0.43 |
| 100 | 0.27 |

The same variant (184249) hits 50+ trades while keeping sharpe 1.99. The
key was the 1.25 bps `place_thresh` plus the right combination of other
dims (alpha_mult=1.0, vol_norm_tdc=10, pred_momentum_coef=1).

This suggests symbols may have a "true peak" that isn't at a given trade-count
floor; you might just need to extend the search universe correctly to find
it. For symbols still showing the tradeoff (e.g., COPPER's sharpe drops past
~70 trades), there's plausibly a similar untested corner.

### `sim_score` is NOT the same as profitable

Sweep_1's CL "winner" by `sim_score`:
- variant 1020623, sim_score 658.55, **avg_pnl −40.27**, sharpe -0.04, 126 trades.

Sweep_a's CL winner with same data quality:
- variant 3024, sim_score 274.22, **avg_pnl +291.43**, sharpe 0.54, 65 trades.

The first variant scored 2.4× higher on `sim_score` despite *losing money*.
`sim_score` weights things like markout and trade count; high trade count with
mediocre per-trade pnl can score higher than low trade count with great
per-trade pnl. Always cross-check `avg_pnl` and `sharpe` before promoting.

The criterion we settled on for picking:
```
filter: pct_positive >= 0.67 AND avg_pnl > 0 AND num_trds >= 50
sort:   sharpe descending
```

The 50-trade threshold filters out single-day flukes. The pct_positive≥0.67
filter requires being net-positive on at least 2/3 of sim days.

### Cardinality concentration matters more than param universe size

Going from sweep_1 (~967k cartesian per symbol, 800 samples = 0.08% coverage)
to the focused sweeps (7k–200k cartesian, 800–3200 samples = 0.5–10% coverage)
was the single biggest source of sharpe lift. The narrowed sweeps largely
**didn't add new dim values** — they just concentrated random sampling in
regions that sweep_1's sparse sampling had under-explored.

The exception is GOLD, where we genuinely needed to extend `place_thresh`
down past the original lower bound to find the optimum at 1.25 bps.

## Process learnings (mistakes & recoveries)

### 1. Narrowing aggressively can kill working regions

When a dim's healthy distribution shows a leaning preference but is not
overwhelming, locking to the leading value can eliminate too much of the
working region. Examples:

- GOLD `lmr_local_only`: sweep_1 healthy was 16 True / 14 False (essentially
  tied within statistical noise). Locking to `[True]` for v2/v3 lost ~50%
  of the working region.
- GOLD `cancel_buffer_frac`: sweep_1 healthy distribution had 0.1 at 19.7%
  healthy (highest) but we dropped it in v2 because the user decided to drop
  cancel_buffer_frac=0.75 (a different value). The strongest value (0.1) was
  removed by mistake during the same simplification pass.
- GOLD `vol_norm_tdc`: locked to `[10]` despite v1 showing comparable healthy
  rates at `4` (17.4%), `10` (18.3%), and `40` (14.7%). Locking eliminated
  the higher-trade-frequency regime.
- SILVER `vol_norm_tdc`: paired `[4, 40]` instead of `[4, 10]` in sweep_b.
  The data showed 4 (15) and 10 (10) as the leading values; 40 had only 8.
  We picked 40 thinking it gave max contrast with 4, but actually 10 was
  the second-strongest value.

**Rule of thumb:** Only lock a dim to a single value when the data shows
> 70% of healthy variants prefer that value, or when you're explicitly
optimizing for cardinality reduction and accept the working-region loss.
Otherwise, keep at least the top 2 values.

### 2. The narrowed sweeps need feedback

Each focused refine should observe the dim distribution among healthy
variants and check the cliffs. The mistakes in (1) were caught by
re-examining the healthy distribution per dim per bucket — a kind of
post-hoc sensitivity analysis. Doing this *before* committing to a narrowed
spec would catch more.

### 3. `pktrade` binary has compile-time secmaster snapshot

The `pktrade` simulator binary embeds the HIP3/Hyperliquid snapshot from
`src/pktrade/util/secmaster_lib/HyperliquidSec.h` at compile time. When new
HIP3 symbols are added (e.g., NATGAS in 2026-02), the binary needs to be
rebuilt before sims will recognize them — or the run will fail with
"Symbol (Hyperliquid, xyz:FOO) not found in secmaster".

Workflow for adding a new symbol:
1. Update `relwide_remote_wiring` in `symbolizer.py` (or whatever wiring is
   relevant for the strategy).
2. Confirm `HyperliquidSec.h` includes the symbol (if it's new on
   Hyperliquid).
3. Rebuild via `./jammybuild.sh -c pktrade`.
4. Then run sweeps.

We hit this for NATGAS during sweep_1 (binary was older than the header).

### 4. The harness kills long-running bash tasks

The Claude Code harness terminates bash background tasks at ~24-48h
intervals (exit code 144). Effects we hit:
- Chained `wait-for-X-then-launch-Y` waiters died silently in their sleep
  loops, even when the command was working correctly.
- Active `run-spec` python processes got killed mid-sweep.

Mitigations that worked:
- `sim_grid` writes results.txt incrementally and supports `resume=True`.
  Relaunching the same spec after a kill picks up where it left off.
- For chained launches, just monitor for the "Top 5 variants for X" event
  and manually launch the next sweep when it fires.

### 5. The `cancel_buffer` / `cancel_buffer_frac` interaction

The seed pk template sets BOTH `cancel_buffer_frac` (default 0.25) and
absolute `cancel_buffer` (default 0.00005). The C++ ordex prefers
`cancel_buffer` if both are set. So sweeping `cancel_buffer_frac` on a seed
that has `cancel_buffer` set is a no-op — every variant behaves the same
because the absolute key wins.

We added a `fixed_delete` mechanism to the spec generator and run-spec to
strip `cancel_buffer` from `pk_dct` before `sim_grid` runs whenever
`cancel_buffer_frac` is in the swept dims. This is auto-emitted by
`_build_symbol_spec_entry` in `alpha_relwide_autosearch.py`. See the
`_apply_symbol_preferences` and `_del_param_from_config` helpers in the
same file.

### 6. Generate-spec auto-bumps caps

When `--base-config` is *not* passed (i.e., the spec generator builds the
base config from a model bundle), `_build_symbol_spec_entry` auto-bumps
`max_variants` and `sample_size` by 2× (capped at `AUTO_VARIANT_CAP=2000`,
`AUTO_SAMPLE_CAP=2000`). When `--base-config` IS passed, the auto-bump is
skipped.

Practical effect: passing `--base-config` keeps the per-symbol caps at
exactly what you specify on the CLI; otherwise the caps double. We hit a
case where we passed `--max-variants=800` and `--sample-size=1500` thinking
we'd get 1500 sims, but with `--base-config` not passed, the auto-bump
turned them into 1600 and 2000 — and the effective limit became `min(1600,
2000) = 1600`.

For the GOLD reruns we used `--base-config` to skip rebuilding the base,
which means we had to bump `--max-variants` AND `--sample-size` explicitly
to get the desired effective count.

## Recommendations for future sweeps

### Methodology

1. **Always start with a broad sweep on the symbols you care about**, even
   if you have prior preferences. Sample 800–1500 variants per symbol.
   This gives you the per-dim healthy distribution to work from.

2. **Run a per-symbol sensitivity analysis** before narrowing. For each
   dim, look at:
   - %healthy by value
   - %losing by value (adverse selection check)
   - %no-trade by value (sparse trading check)
   - median sharpe and median pnl by value

   The cliffs and bimodal patterns show up here. Decisions to lock a dim
   should be data-driven (>70% concentration), not opinion-driven.

3. **For dims at the edge of their range**, extend the range outward if
   data shows the edge winning. GOLD's place_thresh was at the lower bound
   in three different sweeps before we extended to 1 bp.

4. **For dims in their middle**, narrow only to the top 2-3 values, not to
   1. Locking to a single value should require strong evidence.

5. **Pick the best variant by sharpe** with a `num_trds >= 50` filter for
   tradeable picks. Don't trust sim_score.

6. **Cross-check the chosen variant**: does its trade count, pct_positive,
   and avg_pnl all look reasonable? A sharpe-best with 22 trades over 11d
   is barely-statistically-meaningful and likely a fluke.

### Tooling

- Prefer using `symbol_preferences.py` to encode learned per-symbol dim
  ranges; the spec generator applies these via `_apply_symbol_preferences`.
- Use `--no-preferences` for a fresh from-scratch sweep that ignores prior
  preferences.
- Use `sim_grid`'s `resume=True` (already default) — kills are recoverable
  by relaunching.
- Don't rely on chained bash waiters for long-running flows; they die.

### Followups worth doing

- **PLATINUM and PALLADIUM**: currently routed via Pyth + TopBookEquity
  (see `relwide_remote_wiring` in `symbolizer.py`). Adding CME PL/PA
  ingestion would let us run the full alpha_rel_wide_mm pipeline for them.
- **SILVER's sweep_a peak (sharpe 1.57)** wasn't recovered in two reruns.
  Worth running a wider SILVER sweep with `vol_norm_tdc=[4, 10, 40]` and
  more samples to see if a higher peak exists.
- **COPPER's high-trade-count regime**. Sharpe 3.34 at 66 trades is great,
  but the second-best COPPER variant has higher pnl with similar trades.
  Worth a focused refine like GOLD v4 did (extend `place_thresh` lower).
- **More days of sim**. We used 11 trading days; 20–30 days would smooth
  out regime variability and make sharpe more reliable.
- **Live-validation**. Promote a few variants with `enabled=True`, scale
  size down (`size_mult=0.1`), and run for a week to compare live vs
  sim metrics.

## Pointers

| Artifact | Path |
|---|---|
| Consolidated pk | `~/scratch/alpha_relwide_autosearch/pk_consolidated.json` |
| Picks manifest | `~/scratch/alpha_relwide_autosearch/best_picks_so_far.json` |
| Per-variant CSV (GOLD v3) | `~/scratch/alpha_relwide_autosearch/gold_v3_healthy.csv` |
| 67%-pos CSV (sweep_b) | `~/scratch/alpha_relwide_autosearch/pct_pos_67.csv` |
| Sweep 1 results | `~/scratch/alpha_relwide_autosearch/20260501/multi/results/` |
| NATGAS rerun | `~/scratch/alpha_relwide_autosearch/20260502/xyz_NATGAS_only/results/` |
| Sweep A | `~/scratch/alpha_relwide_autosearch/20260503/cl_silver_focused/results/` |
| Sweep B | `~/scratch/alpha_relwide_autosearch/20260503/sweep_b/results/` |
| GOLD v2 / v3 / v4 | `~/scratch/alpha_relwide_autosearch/{20260504/gold_v2,20260507/gold_v3,20260508/gold_v4}/results/` |
| Symbol preferences | `overmind/strat_main/tools/trademan/symbol_preferences.py` |
| Autosearch tool | `overmind/strat_main/tools/trademan/alpha_relwide_autosearch.py` |
| Autosearch design doc | `overmind/strat_main/tools/trademan/alpha_relwide_autosearch.md` |
| C++ ordex | `src/pktrade/ordex/alpha_rel_wide_mm.{h,cc}` |
| Wiring | `overmind/strat_main/util/symbolizer.py:relwide_remote_wiring` |
