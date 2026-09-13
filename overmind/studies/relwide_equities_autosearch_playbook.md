# RelWide Equities Autosearch — Iterative Playbook

Date: 2026-05-21

Living playbook for tuning **RelWideMM2** across the **US equities** universe via an iterative, phased autosearch. Companion tool:
`overmind/strat_main/tools/trademan/relwide_equities_autosearch.py`.

Working dir for this round: `~/scratch/relwide_retrains/20260521_equities/`.

This document captures:
1. The phased methodology and why each phase exists.
2. Decisions made for this round (fillrate target, sim window, donor, etc.).
3. The relevant conclusions baked in from prior studies (excerpts below).
4. Per-iteration logbook (filled in as work progresses).

---

## 1. Goal

Run RelWideMM2 against every symbol in the equities universe (`_US_EQUITIES`
in `symbolizer.py:1121`), iteratively narrowing parameter dimensions to
produce a per-symbol "best-known" config. Unlike one-shot grid searches,
the iteration is explicit: each pass either widens or narrows around
evidence from the prior pass.

The tool is a **wrapper around `coverage_autosearch`**, reusing its
`scan_thresh` and `run_spec` primitives. We don't fork the grid-search
engine; we wrap it with equities-specific dim defaults, donor cloning,
and a narrowing loop.

---

## 2. Phased methodology

### Phase 0 — `scan-thresh`
Single-dim `place_thresh` scan over a wide log-scale set:
`[0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02]` (1–200 bps).
All other dims pinned to base config values.

**Purpose:** size a proper place_thresh range for Phase 1. Without this,
the auto-ranger collapses to a single thresh when the base value is far
from the symbol's regime (see Excerpt A below).

### Phase 1 — broad sweep
Multi-dim sweep with **wide intentional walks** (not ±20%
perturbations). Default dims for equities:

| Param | Values | Source of choice |
|---|---|---|
| `place_thresh` | (3 values centered on Phase 0 winner, ±0.5×) | Phase 0 result |
| `cancel_buffer_frac` | `[0.25, 0.5, 1.0, 2.0, 4.0]` | dominant fillrate lever (Excerpt B) |
| `ladder_one_sided` | `[True, False]` | strongest cross-symbol signal (Excerpt B) |
| `pred_momentum_coef` | `[0, 0.5, 2.0, 3.0]` | bimodal (Excerpt B) |
| `pred_momentum_tdc` | `[5, 30]` | tdc=30 clearly wins (Excerpt B) |
| `premium_ema_coef` | `[0.9, 1.0]` | always 0.9–1.0 (Excerpt C) |
| `premium_tdc_s` | `[10, 60, 120]` | symbol-specific (Excerpt C) |
| `place_thresh_mode` | `[0, 1]` | symbol-specific (Excerpt C) |
| `curv_impulse_coef` | `[0, 1.25]` | bimodal across Korean A/B (Excerpt C) |
| `vol_norm_coef` | `[0.1, 1.0]` | 0.1 usually wins but suspicious (Excerpt C) |

Variants capped at `max_variants=500`; if Cartesian exceeds, downsample randomly.

### Phase 2 — narrowed sweep
Reads Phase 1 `results.txt`, filters by quality bar (see Selection below),
and produces a narrowed spec for re-run. Only lock a dim to a single value
when **≥70% of healthy variants concentrate there** (Excerpt D).

### Phase 3+ — judgement calls / handoff
Inspect the Phase 2 winner for sparseness, regime fragility, and
fillrate/PnL inversions. Promote to a candidate live config or queue for
a deeper drilldown.

---

## 3. Decisions for this round (2026-05-21)

| Decision | Value | Reason |
|---|---|---|
| Symbol universe | `_US_EQUITIES` (`symbolizer.py:1121`) | full equities coverage |
| Start with | `xyz:SNDK`, `xyz:MU` | exercise on two symbols first |
| Ordex | `RelWideMM2` | per user direction |
| Donor (for clone) | `xyz:TSLA` (fixed) | doesn't matter; thorough search regardless |
| `fillrate_target` | **0.01** (1%) | higher than `coverage_autosearch`'s default 0.005; equities tend to over-cancel |
| Sim window | 25 days (within 20–30 range) | Excerpt D ("≥20d, prefer 30+") |
| `md_exists` check | mandatory, both local + remote feeds | Excerpt A footnote, Excerpt D point 9 |
| Workdir | `~/scratch/relwide_retrains/20260521_equities/` | per user |
| Iteration mode | **manual** between phases | inspect Phase 0 before launching Phase 1 |
| Param walk style | **iterated walk along dimensions**, not ±20% perturb | per user — search thoroughly |

---

## 4. Quality bar (selection rule)

For ranking and promotion, **don't trust `sim_score` alone**. Re-rank from
raw fields:

```
filter:
    fillrate     >= 0.01     (1% — overrides default)
    pct_positive >= 0.67
    avg_pnl      >  0
    num_trds     >= 50       (relax to 20 for sparse names; warn)
sort:
    sharpe descending
```

Always cross-check `avg_pnl`, `sharpe`, `pct_positive`, `num_trds`,
`fillrate` together for the top candidate. Multiplicative score
inversions (high score, negative PnL) are real (Excerpt B point 3).

---

## 5. Workflow / commands

```bash
# Phase 0 — set up spec & scan place_thresh
relwide_equities_autosearch.py init-spec \
    --symbols xyz:SNDK,xyz:MU \
    --donor xyz:TSLA \
    --sim-days 25 \
    --workdir ~/scratch/relwide_retrains/20260521_equities

relwide_equities_autosearch.py scan-thresh \
    --spec ~/scratch/relwide_retrains/20260521_equities/spec.py

# Inspect ~/scratch/relwide_retrains/20260521_equities/thresh_scan/summary.txt,
# then edit spec to set place_thresh dims around the winner.

# Phase 1 — broad sweep
relwide_equities_autosearch.py run-spec \
    --spec ~/scratch/relwide_retrains/20260521_equities/spec.py

# Phase 2 — narrow & re-run
relwide_equities_autosearch.py narrow \
    --from-results ~/scratch/relwide_retrains/20260521_equities/results \
    --out ~/scratch/relwide_retrains/20260521_equities/spec_v2.py

relwide_equities_autosearch.py run-spec \
    --spec ~/scratch/relwide_retrains/20260521_equities/spec_v2.py
```

---

## 6. Excerpts from prior studies (the conclusions we're baking in)

### Excerpt A — `coverage_autosearch_lessons_20260513.md` (RelWideMM2 traps)

> **place_thresh auto-range clamps to single value when base > bounds max.**
> The auto-ranger does ±20% around base, then clamps to `(0.0001, 0.003)`.
> If base is above 30 bps, the clamp collapses the range. CRCL US Day:
> base 0.004 → auto-range `[0.0032, 0.0048]` → clamped → `[0.004]` only.
> Sweep ran 500 variants but with 1 thresh value, 375/500 didn't trade.
> Re-run with manual `[0.0012–0.003]` produced 476/500 traded, top score 6.55.
> **Mitigation: scan-thresh first.**

> **RelWideMM2 requires `beta_uncertainty_coef`, `relative_sig`, `relative_beta`,
> `snapshot_interval_s`, `hardcoded_min_tick`, snapshot fields.** Loading a
> config without these triggers a RapidJSON assertion in
> `RelWideMM2::RelWideMM2` and `Aborted (core dumped)` at sim startup.
> **Detection:** `num_trds == 0` in *all* variants in `results.txt`.

> **TopBookEquity remote feed is the silent-fail trap.** RelWideMM2 needs
> both local (Hyperliquid) and remote (TopBookEquity) data. If the remote
> feed has no historical data, the sim runs cleanly, the trader is enabled,
> but `our_cross 0 their_cross 0 their_add 0` — zero orders.
> `coverage_autosearch.run_spec` only checks local md. **Our tool checks
> both.**

> **`enabled=False` trap.** If the base pktrader has `enabled: False`, the
> sim runs but the trader is inert. `scan_thresh` already force-sets
> `enabled=True`; `run_spec` does not. **Our tool always forces
> `enabled=True`.**

### Excerpt B — `equities_autosearch_lessons_202605.md` (equity-specific findings)

> **Fillrate-aware scoring is essential.** Pre-2026-04-30, autosearch had no
> fillrate term. Top variants routinely had fillrates of 0.05–1% — strategy
> got "cheap" sim PnL by canceling >99% of orders. Live exchanges fill
> differently; winners over-fit to a regime that won't reproduce live.
> Fix: `fillrate_target` multiplier in `SimVariations.py:252-261`. Variants
> with fillrate ≥ target get full credit; below scales linearly. Default
> 0.005 (50 bps); **we use 0.01 (1%) for this round.**

> **`cancel_buffer_frac` is the dominant fillrate lever** (note:
> AlphaRelWideMM-specific; for RelWideMM2 the analogous knob is
> `cancel_buffer` absolute). Equity-side numbers from the May 2026 sweep:
> AMZN 0.35% → 8.05% (23×), BABA 0.74% → 11.5% (15×), NVDA 0.28% →
> 24.6% (88×, blew up PnL), TSLA 0.36% → 14.3% (40×). At cbf=4.0 most
> symbols over-traded into negative PnL. cbf=2.0 was the sweet spot.

> **Cross-symbol dim winners (top-100 pool, n=1300):**
> - `ladder_one_sided=True` — strongest boolean signal (mean score 30.4 vs 22.7).
> - `pred_momentum_tdc=30` — clear winner over 1/5/15.
> - `pred_momentum_coef` — bimodal: 0 or 2-3 win; mid values (0.5, 1.0) underperform.
> - `vol_widen_coef` — low values (0.07-0.18) win.
> Weak/no signal: `lmr_time_const_s`, `curv_impulse_tdc`, `lmr_local_only`,
> `curv_impulse_coef`.

> **Score-PnL inversion is real; cross-check raw stats.** Multiplicative
> scoring can produce inversions where higher-score variant has worse PnL.
> RIVN cbf=4.0 winner had $27 PnL / 28.67% fillrate; outscored cbf=0.25's
> $221 PnL because vol_mod and fillrate_mult compounded.

> **Sparse-trading symbols (LLY, COST):** wide auto-generated `place_thresh`
> ranges → low trade rate. LLY peaked at 23 trades over 10 days. Treat
> sample as fragile; operational overhead may exceed gain.

### Excerpt C — `coverage_autosearch_lessons_20260513.md` (per-symbol patterns from Korean/thin equities round)

> Cross-symbol pattern in winners (round 2, 2026-05-11):
> - `vol_norm_coef=0.1` dominates — the lowest value wins for ~all symbols.
> - `ladder_one_sided=True` forced.
> - `place_thresh_mode`: thin/sparse names prefer mode 0; higher-flow names prefer mode 1.
> - `premium_ema_coef`: 0.9 or 1.0, never lower.
> - Top sharpes line up with 100% positive days consistently.

> DKNG: best `place_thresh=0.0002` (2 bps), `pred_momentum_coef=0`, ptdc=10
> (fast premium). HIMS: tight thresh like DKNG, but ptdc=120 (slow premium)
> — "high-frequency low-edge" profile. Same template doesn't fit both.

### Excerpt D — `alpha_relwide_param_study_20260510/recommendations.md` (process rules)

> **11-day windows are too short.** Variance over 11 days is large enough
> that the sweep partly picks variants on noise. Full-range (23-day)
> re-sweeps produced clearly better picks. **Rule: 20–30 day minimum sweep
> window, prefer 30+.**

> **Aggressive locking can kill working regions.** Specific mistakes
> included locking `lmr_local_only=[True]` for GOLD when sweep was
> basically 50/50. **Rule: only lock a dim to a single value when ≥70%
> of healthy variants concentrate there. Otherwise keep top 2–3 values.**

> **`sim_score` ≠ profitable pnl.** Multiple times we almost promoted
> variants that scored high but lost money (sweep_1's CL "winner": sim_score
> 658, **−$40/d avg_pnl**). Minimum selection criterion:
> `pct_positive >= 0.67 AND avg_pnl > 0 AND num_trds >= 50`, then sort
> by sharpe descending.

> **Always run `md_exists` before launching an OOS sim.** We spent
> significant time debugging "0 trades for 6 symbols" before realizing the
> market data simply wasn't downloaded locally. The sim ran cleanly on
> empty data with no error.

> **Concentration over exploration.** Going from 800/~1M variants (sweep_1)
> to 800/~7k (sweep_a) didn't add new dim values — it just reweighted
> random sampling toward already-good regions. That alone produced 80%
> of the sharpe lift.

> **Trade-rate explosion across regimes is the over-fit smoking gun.**
> Variants that traded ~5/day in-sample and ~200/day OOS were the same
> ones whose sharpe collapsed. Diagnostic: any variant whose IS/OOS
> `avg_trades_per_day` ratio is >5× is likely over-fit.

### Excerpt E — `coverage_autosearch_lessons_20260513.md` (random seeding)

> **Random sampling is seeded — re-runs don't explore new variants.**
> `sim_grid` does `random.seed(42)` before sampling. Re-running the same
> spec with the same dim product picks the same indices. To explore new
> regions, change a dim or change the seed. Implication for narrowing:
> the narrowed spec should change at least one dim, otherwise it's the
> same sample.

---

## 7. Iteration logbook

Filled in as work progresses. One entry per phase.

### 2026-05-21 — Phase 0: scan-thresh on SNDK, MU

Donor: `xyz:DKNG` (patched config from
`~/scratch/coverage_autosearch/20260511/pk_dkng_patched_v2.json`).
Cloned for SNDK and MU, US Day session, 18 trading days
(2026-04-23 → 2026-05-20).

Results (`thresh_scan/summary.txt`):

```
xyz:MU|US Day
      thresh   trades    avg_pnl   sharpe  sim_score
     0.00010      442     -69.20    -0.23     -56.32
     0.00020      322     -40.95    -0.15     -20.03
     0.00050      152     -20.68    -0.13     -33.33
     0.00100       57       1.70     0.03      -4.83
     0.00200       13      -0.13    -0.00      -0.22
     0.00500        1      -2.84    -0.31      -1.52
     0.01000        0       1.26     0.27       0.00
     0.02000        0       1.46     0.27       0.00

xyz:SNDK|US Day
      thresh   trades    avg_pnl   sharpe  sim_score
     0.00010      762     -70.09    -0.16     -48.54
     0.00020      549     -52.48    -0.14     -53.23
     0.00050      237     -37.59    -0.13     -66.80
     0.00100       71      -9.86    -0.06     -32.47
     0.00200       12      -0.49    -0.01      -9.64
     0.00500        1       4.35     0.10      -2.46
     0.01000        1       8.40     0.22       0.00
     0.02000        0       9.44     0.24      -0.00
```

**Observations:**

1. **No clearly profitable thresh** at this point — every value with
   meaningful trade count loses money. This is the *donor wiring problem*,
   not a thresh problem: the DKNG-cloned config has `relative_beta=0`
   (patched safe default), so the rel signal contributes nothing to fair
   value; the strategy degenerates to a remote-mid market maker.
2. **MU best PnL** at 10 bps (`+1.70 avg_pnl`, only 57 trades over 18 days,
   sharpe 0.03 — basically noise).
3. **SNDK best PnL** at 100 bps or 200 bps with 1 trade — useless sample.
4. **Sub-50bps regime is actively bad** for both names: many trades at
   negative PnL/sharpe — strategy is getting adverse-selected.

**Implication for Phase 1**: clone-from-DKNG is not a good starting point
for SNDK/MU. Three next steps to consider before launching the broad
sweep:

- (a) Fit per-symbol `relative_beta` against the cloned remote_sig before
  sweeping — currently it's 0 by patch default.
- (b) Add `relative_beta` as a sweep dim (e.g., `[0, 0.5, 1.0, 1.5]`).
- (c) Pick a donor whose remote-anchored regime is closer to SNDK/MU's
  (semiconductor / large-cap tech) — perhaps a donor without rel
  signal at all, just a pure market maker config.

For *exercising* the tool, this round successfully:
- generated a spec for 2 named equities
- pre-fetched TopBookEquity remote feed
- ran md_exists pre-check on the Hyperliquid local feed
- swept place_thresh across 1–200 bps log scale
- wrote per-symbol results.txt + cross-symbol summary

The tooling is working end-to-end. Next iteration needs a better donor
or a `relative_beta` fit step.

> **Donor change** (resolved during the 2026-05-21 work): switched
> default donor from `xyz:TSLA` (old February config, missing rel-
> uncertainty fields) to **`xyz:DKNG`** with the May 2026 patched config
> at `~/scratch/coverage_autosearch/20260511/pk_dkng_patched_v2.json` —
> modern RelWideMM2 with all rel-uncertainty fields present. All later
> rounds use this donor.

### 2026-05-21 — Round A (placement): place_thresh × cancel_buffer × ladder_one_sided

40 variants per symbol (5 × 4 × 2). Per-symbol place_thresh dims chosen
to cover both MU regimes from Phase 0b.

**xyz:MU**: winner v32 (`place=9bp, cb=0.3, ladder=False`) — pnl $27.34,
sharpe 1.09, pct_pos 0.89, 88 trades, fillrate 1.75%. `ladder=False`
dominates every top row.

**xyz:SNDK**: winner v22/v37 cluster (`place=13bp, cb=0.5 or 0.1,
ladder=False`) — pnl $11.20, sharpe 0.34, pct_pos 0.78, 33-46 trades.
Lower ceiling than MU.

User feedback baked in after this round:
- **`ladder_one_sided=False`** locked globally.
- **Don't pre-filter negative-PnL variants** in narrowing — a later round
  may turn them positive; floor is "trades meaningfully", not "profitable
  already". (Memory: `feedback_dont_overprune_negative_variants.md`.)

### 2026-05-21 — Round B (signal): pred_momentum × curv × premium_tdc_s

64 variants per symbol (4 × 2 × 2 × 4). Held `place_thresh` /
`cancel_buffer` at Round A winners; swept the four signal dims.

**xyz:MU**: best by sharpe v8/v12 (`pmc=0, curv_coef=1.25, premium_tdc_s=10`)
sharpe 1.20, pnl $22.53, pct_pos 0.94, 73 trades. Round A baseline
(`pmc=0, curv=0, premium_tdc_s=30`) still wins on PnL ($27.34 vs $22.53).

**xyz:SNDK**: user picked v4/v0 cluster (`pmc=0, curv=0,
premium_tdc_s=10`): pnl $11.58, sharpe 0.32, **51 trades, fillrate 2.56%**
— high-volume baseline without flashy signals.

User feedback memory after this round:
`feedback_rank_by_more_than_sharpe.md` — don't rank by sharpe alone;
weigh trade count + fillrate; surface all regimes.

Also added to fixed overrides per user direction:
- `front_rung_spacing_mult=1.0` (donor had 0.5)
- `place_thresh_mode=1` (locked)

### 2026-05-21 — Round C (geometry + signal + vol_norm): 9-dim sweep, 1000 random-sampled variants

Full dim list:
- `place_thresh`: ±10% around per-symbol anchor (3 values)
- `per_order_widen_frac`: `[0.3, 0.5, 0.7]`
- `rung_spacing_mult`: `[1.0, 1.5, 2.0]`
- `curv_impulse_coef`: `[0.0, 1.25]`
- `curv_impulse_tdc`: `[30, 240]`
- `pred_momentum_coef`: `[0, 3, 5]`
- `pred_momentum_tdc`: `[10, 30]`
- `vol_norm_coef`: `[0.0, 0.1, 0.5, 1.0]`
- `vol_norm_tdc`: `[10, 60]`

5,184 Cartesian → 1000 random-sampled per symbol → ~36k sims, ~3h wall.

**xyz:MU**: 453/1000 healthy. Best v3274 (`place=9bp, widen=0.7, rsm=1.0,
curv=1.25, curv_tdc=30, pmc=0, ptdc=10, vol_norm=0.1, vol_norm_tdc=60`)
— sharpe 1.50, pnl $27.42, pct_pos 0.94, 101 trades.

**xyz:SNDK**: 95/1000 healthy. User-pref candidate v657 (`place=11.7bp,
widen=0.3, rsm=1.5, curv=0, pmc=0, vol_norm_coef=0.1, vol_norm_tdc=10`)
— sharpe 0.60, pnl $25.53, pct_pos 0.67, 51 trades, fillrate 2.25%.

### 2026-05-22 — 09:40 start override (data-driven)

User flagged that "the first 10 minutes are a little deceptive" and asked
to start US equities at 09:40 instead of 09:30. Added permanent override
in the tool (`relwide_equities_autosearch._apply_equity_us_day_override`)
that mutates `coverage_autosearch.SESSION_TIMES["US Day"]` for any sim
this tool launches. Slowest signal time constant in our space is
`curv_impulse_tdc=240` (4 min warmup) so brief signals are not materially
affected by the cold start.

**Round D1 — re-sim Round C winners at 09:40**: confirmed user's
hypothesis.

| symbol | sharpe (09:30) | sharpe (09:40) | pnl (09:30) | pnl (09:40) | pct_pos (09:30 → 09:40) |
|---|---|---|---|---|---|
| MU v3274 | 1.50 | 0.90 | $27.42 | $17.76 | 0.94 → 0.78 |
| SNDK v657 | 0.60 | 0.35 | $25.53 | $15.32 | 0.67 → 0.61 |

**Revalidate top-30 at 09:40**: many Round C winners collapsed; a few held.

- MU: v665 actually **improved** (sharpe 1.14 → 1.37, pct_pos 0.89 → 0.95).
  Multiple SNDK variants turned NEGATIVE sharpe (e.g. v3297: 0.41 → -0.03;
  v3892: 0.64 → 0.06).
- SNDK lost ~60% of healthy variants in the revalidate — many "wins" were
  open-microstructure artifacts.

### 2026-05-22 → 23 — Round C re-run at 09:40 start

Same `spec_round_c.py`, fresh sweep with 09:40 start. ~3h wall time.

**xyz:MU**: 388/1000 healthy (vs 453 at 09:30). New winners:

| var | place | widen | rsm | curv | pmc | vol_norm | pnl | sharpe | pct | trd | fr |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **v1624 ★** | 0.0009 | 0.5 | 1.0 | 0 | 0 | 0.5/10s | 22.60 | **1.35** | 0.89 | 49 | 0.89% |
| v4274 | 0.00099 | 0.7 | 1.0 | 0 | 0 | 0.5/60s | 15.77 | 1.34 | 0.89 | 46 | 0.95% |
| v665 | 0.00099 | 0.7 | 1.5 | 0 | 0 | 0.1/10s | 19.29 | 1.32 | 0.95 | 54 | 1.43% |

Locks (≥70% in top-30): `curv_impulse_coef=0`, `pred_momentum_coef=0`.
"No fancy signals" is now the cross-symbol pattern post-09:40.

**xyz:SNDK**: only **36/1000 healthy** (was 95 at 09:30 — confirms
structural fragility). User-preferred candidate:

| var | place | widen | rsm | curv | pmc | vol_norm | pnl | sharpe | pct | trd | fr |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **v385/v2599 ★** | 0.0013 | 0.7 | 1.0 | 0 | 0 | 0 | 9.25 | 0.48 | 0.74 | **49** | **2.05%** |

User picked the high-volume / high-fillrate variant over the sparse
high-sharpe options (v53 had sharpe 0.67 but only 23 trades).

### 2026-05-23 — Promoted configs

Saved at `~/scratch/relwide_retrains/20260521_equities/promoted/`:
- **`pk_xyz_MU_v1624.json`** — MU candidate; sharpe 1.35, $22.60/d, 49 trd, pct_pos 0.89.
- **`pk_xyz_SNDK_v385.json`** — SNDK candidate; sharpe 0.48, $9.25/d, 49 trd, pct_pos 0.74.

Both have `settings.start_t = 09:40:00 America/New_York`, all fixed
overrides applied, and are filtered to a single pktrader.

**Headline takeaways**:
- Always run the revalidate phase at 09:40 (or other delayed open) — open
  microstructure inflated 09:30 winners by ~40-60% on sharpe for both
  these symbols.
- For thin equities (SNDK class), pure market-maker configs (no curv, no
  pred_momentum, no vol_norm) beat signal-rich configs once the open
  artifact is removed.
- Healthy-variant count is a regime-fragility proxy. SNDK going 95 → 36
  (62% loss) under the 09:40 change was a stronger signal than any
  individual variant comparison.

### 2026-05-23 → 26 — NVDA (semis batch)

Workdir: `~/scratch/relwide_retrains/20260523_NVDA/`. Started with NVDA
alone per user direction ("one symbol at a time"). 09:40 start applied
from the very first phase (procedure now formalized in section 8).

**Phase 0b (fine scan-thresh)**: NVDA is a fragile / tight-spread symbol.
At sub-3 bp spreads, the strategy over-trades and gets adverse-selected
(1 bp: 512 trades, -$10.72 pnl). The "healthy" scan zone was sparse:
4 bp barely positive (37 trd, $2.36 pnl), 6 bp sharpe 0.51 (12 trd).
User chose to lean into the tight-spread regime trusting later signal
dims to flip it.

**Round A (placement, 16 variants)**: Best v11 (`place=4bp, cb=0.3`) —
pnl $1.04, sharpe 0.06, 82 trades, fillrate 1.44%. Marginally positive.
1-3 bp regimes all bleed without signal help.

**Round B (signal, 256 variants)**: Signals flipped NVDA from marginal
to clearly healthy.
- Best v13 (`place=3.3bp, pmc=3, pmc_tdc=5, curv=0, ptdc=10`): sharpe
  **0.77**, pnl $9.30, pct_pos 0.82, 46 trades.
- **Locked `pred_momentum_tdc=5`** — 22/30 = 73% in top-30 by sharpe.
- `pred_momentum_coef=2 or 3` strongly preferred (20/30) — fundamentally
  different from MU/SNDK where pmc=0 dominated.

**Round C (broad geometry + signal + vol_norm, 1000 random sampled)**:
NVDA improved further to **sharpe 1.08**.
- 358/1000 healthy (36% — best healthy rate of the symbols in this run).
- `pred_momentum_tdc=5` — **29/30 = 97% LOCK** (essentially mandatory).
- `pred_momentum_coef=3` preferred over 5 (20/30 vs 10/30).
- `per_order_widen_frac=0.3` (tighter ladder) — 20/30 = 67%.
- `curv_impulse_coef=1.25` lean (18/30 = 60%).
- `vol_norm_coef=0.5` lean.

**Promoted**: `pk_xyz_NVDA_v614.json` —
`place=3.6bp, cb=0.3, widen=0.3, rsm=1.0, curv=1.25/30s, pmc=3 / pmc_tdc=5,
vol_norm=0.1/10s, ptdc=30`. Sharpe **1.03**, pnl **$17.01/d**, pct_pos
0.88, 35 trades, fillrate 0.27%. User picked v614 over the higher-volume
v108 (53 trd, sharpe 0.81) because of the much higher pct_positive.

**NVDA cross-symbol learning**: signals matter more for tight-spread
symbols. SNDK/MU were "no fancy signals" winners; NVDA is the opposite
— it *requires* pred_momentum to be tradable. **Don't generalize either
template to the other**; the signal regime is per-symbol.

### 2026-05-26 — AMD (semis batch, second symbol)

Workdir: `~/scratch/relwide_retrains/20260523_AMD/`.

**Phase 0b (fine scan-thresh)**: AMD has the strongest baseline of any
symbol so far. Two viable regimes at 09:40 with no signals:
- High-volume dense (1-3 bp): 152-562 trades, sharpe 0.35-0.65, fillrate
  1.4-2.3%, all profitable
- Sharpe sweet spot (4-5 bp): 56-92 trades, sharpe 0.89-1.07, **pct_pos
  0.81-0.94** — strongest scan-thresh baseline observed.

**Round A (placement, 16 variants, 2-5 bp range)**: Winner v3
(`place=5bp, cb=0.1`): sharpe 1.23, pnl $18.64, pct_pos 0.88, 104
trades, fillrate 0.93%. **AMD prefers `cancel_buffer=0.1`** — different
from MU/SNDK/NVDA (which all used 0.3-0.5). Aggressive cancellation
suits AMD's faster price action.

**Round B (signal, 192 variants, place_thresh 4/4.5/5 bp)**: 159/192
healthy (83%). Outlier winner v149: sharpe **1.79**, pnl $26.86,
**pct_pos 1.00**, 61 trades, fillrate 0.59%. User-preferred v75
(matches later v93): sharpe 1.48, pct_pos 1.00, 80 trd. AMD signal
regime:
- ✅ `pred_momentum_tdc=5` LOCK (23/30)
- `pred_momentum_coef` bimodal: 0.5 (gentle) OR 3.0 (aggressive); 0 and 2 weaker
- `curv_impulse_coef=0` lean (different from NVDA)

**Round C (broad geometry + signal + vol_norm, 1000 random sampled)**:
**745/1000 healthy (74%)** — most robust symbol of the run.
- ✅ `per_order_widen_frac=0.7` LOCK (23/30 = 77%)
- ✅ `pred_momentum_tdc=5` LOCK (24/30 = 80%)
- `pred_momentum_coef=0.5` leans 19/30
- `place_thresh` distributed 4.5-5 bp; v93 at 4bp is outlier
- `vol_norm_coef`, `curv_impulse_*`, `rung_spacing_mult` distributed

**Promoted**: `pk_xyz_AMD_v93.json` —
`place=4bp, cb=0.1, widen=0.5, rsm=1.5, curv=1.25/240s, pmc=0.5/5,
vol_norm=0/10s, ptdc=30`. Sharpe **1.48**, pnl **$18.14/d**, **pct_pos
1.00 (every day positive)**, 80 trades, fillrate 0.62%. User chose v93
over higher-PnL v216 ($33.70, sharpe 0.94, pct_pos 0.81) — pct_pos 1.0
was the deciding factor.

**AMD cross-symbol learning**: liquid mid-cap semis can be a clean,
robust signal regime — AMD healthy rate of 74% is dramatically higher
than NVDA's 36% or SNDK's 4%. The `cancel_buffer=0.1` preference is
likely characteristic of liquid symbols where stale orders get
adverse-selected quickly.

### 2026-05-26 — INTC (semis batch, third symbol)

Workdir: `~/scratch/relwide_retrains/20260523_INTC/`.

**Phase 0b (fine scan-thresh)**: INTC has two viable regimes at 09:40
with donor's signals on:
- High-volume profitable (3-5 bp): 134-366 trades, sharpe 0.27-0.61,
  fillrate 1.4-2.3%
- Sharpe spike (9 bp): 28 trades, sharpe 0.65, pct_pos 0.81
- Catastrophic at 1 bp: -$26.11/day, sharpe -0.44 (worst tight-spread
  collapse of any symbol)

**Round A (placement, 20 variants, 3-5 bp every 0.5bp)**: Surprising
regression vs scan-thresh — Round A's "no signals" baseline (pinning
`pred_momentum_coef=0, premium_tdc_s=30`) neutered INTC. Best v4
(5bp, cb=0.1) was only +$5.03 vs scan's +$17.42 at the same thresh.
**Key lesson**: scan-thresh isn't actually signal-free — it inherits
the donor's `pred_momentum_coef=0.5` and `premium_tdc_s=120`. Round A
needs to be cautious about overriding signal dims to 0 before testing
them.

**Round B (signal, 144 variants, place_thresh 4/4.5/5 bp x signal dims,
cb=0.1, premium_tdc_s ∈ [10, 60, 120])**: 68/144 healthy (47%).
Strong recovery — top v54: sharpe 1.01, pnl $26.71, pct_pos 0.88,
**168 trades**. Highest trade volumes of any symbol tuned so far.
**`pred_momentum_coef=0` NEVER won** in top-30 — INTC strictly needs
pmc > 0.

**Round C (broad geometry + signal + vol_norm, 1000 random sampled,
premium_tdc_s=60 fixed)**: **450/1000 healthy (45%)** and produced
the strongest variant of any equity Round C so far.
- ✅ `pred_momentum_tdc=5` LOCK (23/30 = 77%)
- `place_thresh` distributed across 4/4.5/5 bp
- `pred_momentum_coef` distributed across 0.5/2/3 (all viable)
- `rung_spacing_mult=1.5` leans
- `vol_norm_coef=0` and `0.5` tied

**Promoted**: `pk_xyz_INTC_v394.json` —
`place=4.5bp, cb=0.1, widen=0.7, rsm=1.5, curv=0/240s, pmc=0.5/30s,
vol_norm=0/10s, ptdc=60`. Sharpe **1.13**, pnl **$31.73/d**, pct_pos
0.88, **178 trades** (highest volume of any promotion candidate),
fillrate 1.04%. Strictly dominates the other top candidates on every
metric.

**INTC cross-symbol learning**: tight-spread liquid symbols (NVDA, INTC)
both require `pred_momentum_coef > 0` AND fast `pred_momentum_tdc=5`,
but differ on other dims (curv, widen, vol_norm). Don't auto-clone
across them. Also: **the Round A "no signals" baseline can mislead** —
if scan-thresh shows promise but Round A regresses, suspect that
Round A pinned signal dims that the donor was using. Move directly to
Round B with signals on.

### 2026-05-26 — CRCL (volume-sorted batch, first symbol)

Workdir: `~/scratch/relwide_retrains/20260526_CRCL/`. Selected because
it had the highest 24h Hyperliquid volume among the remaining
`_US_EQUITIES` ($34M).

**Phase 0b (fine scan-thresh)**: CRCL bleeds catastrophically at tight
spreads (1 bp: -$300/d, sharpe -1.46). Narrow sweet spot at **15-16 bp**
(sharpe 0.52-0.64, ≤28 trd). User chose to focus Round A on 9-14 bp
range (higher trade count, lower sharpe ceiling) since the 15-16 bp
zone was too sparse.

**Round A (placement, 24 variants, 9-14 bp)**: Only 2 positive (v14:
11bp/cb=0.3 sharpe 0.11; v17: 14bp/cb=0.3 sharpe 0.08). Same
"signals-off" trap as INTC — pinning `pmc=0, premium_tdc_s=30`
neutered the symbol.

**Round B (signal, 192 variants, place_thresh 11-14 bp + signal sweep,
cb=0.3)**: Only **12/192 healthy (6%)** — structurally fragile,
SNDK-class. Best v87: sharpe 0.64, pnl $13.17, pct_pos 0.75, 34 trd.

**Round C (broad 8-dim, 1000 random sampled, cb=0.3, premium_tdc_s=60)**:
**56/1000 healthy (6%)** — confirms fragility. No dims locked at ≥70%.

**Promoted**: `pk_xyz_CRCL_v822.json` —
`place=13bp, cb=0.3, widen=0.5, rsm=2.0, curv=0/240s, pmc=0.5/30s,
vol_norm=0/10s, ptdc=60`. Sharpe **0.69**, pnl **$10.57/d**, pct_pos
0.81, 33 trades, fillrate 0.98%. User picked v822 over v2844 (sharpe
0.67, pnl $25.62 but fillrate only 0.25% — sketchy quality of the few
fills).

**CRCL cross-symbol learning**: CRCL is fragile-class (SNDK, CRCL both
have ~6% healthy rates) but differs in signal regime — CRCL prefers
`pred_momentum_tdc=30` while SNDK had pmc off entirely. Recently-listed
symbols (CRCL was added recently) may share fragility profile.

### 2026-05-26 → 27 — CBRS skipped (too little data)

Workdir: `~/scratch/relwide_retrains/20260526_CBRS/`. User flagged that
CBRS IPO'd 2026-05-14 — only ~9 trading days of data, far too little
to tune robustly.

Added `start_date_override` support to the tool (in `cmd_scan_thresh`)
to honor explicit post-IPO start dates. Scan-thresh ran on the 7
trading days from 2026-05-15 to 2026-05-25, but **every meaningful-trade
row was negative**. Only 4 sparse positive rows at very wide thresh
(32/35/45/50 bp with 3-12 trades) — statistically meaningless.

**Decision**: skip CBRS for now; revisit when ≥3 weeks of post-IPO data
have accumulated.

**Process lesson**: for any symbol with <~15 trading days of data, the
sweep is too noisy to trust. Either wait for more data, or accept that
the picks are speculative.

### 2026-05-27 — TSLA (volume-sorted batch)

Workdir: `~/scratch/relwide_retrains/20260527_TSLA/`.

**Phase 0b (fine scan-thresh)**: Classic tight-spread liquid profile.
1 bp bleeds (-$12.50). Sweet spot at **4 bp** (52 trd, sharpe 0.87,
pct_pos 0.87) with donor signals on.

**Round A (placement, 20 variants, 2-4 bp every 0.5 bp)**: Same
no-signals trap as INTC/CRCL — best v3 (3.5 bp, cb=0.1) only got
sharpe 0.35 vs scan's 0.87. Tightest thresh (2 bp) catastrophic
(-$10 to -$17/d, -0.22 to -0.38 sharpe).

**Round B (signal, 144 variants, 3-4 bp x signal sweep, cb=0.1)**:
70/144 healthy (49%) — solid recovery. Best v28: sharpe 0.80, pnl
$9.61, 55 trd. `pred_momentum_tdc=5` slight lean (63%). All other
signal dims distributed in winners.

**Round C (broad 9-dim, premium_tdc_s swept, 1000 random sampled)**:
**502/1000 healthy (50%)**.
- ✅ `per_order_widen_frac=0.7` LOCK (24/30 = 80%)
- `curv_impulse_coef=0` lean (19/30 = 63%)
- `pred_momentum_tdc=5` lean only (16/30 = 53%) — *unusually distributed*
  vs NVDA/AMD/INTC where this dim locked
- Everything else distributed (place_thresh, pmc, vol_norm, premium_tdc_s)

**Promoted**: `pk_xyz_TSLA_v17.json` —
`place=4bp, cb=0.1, widen=0.7, rsm=1.5, curv=0/30s, pmc=0.5/5,
vol_norm=0/10s, ptdc=10`. Sharpe **1.01**, pnl **$12.15/d**, pct_pos
0.93, 56 trades, fillrate 0.59%. Strictly dominated the other top
candidates.

**TSLA cross-symbol learning**: TSLA fits the "tight-spread liquid"
class (NVDA/AMD/INTC/TSLA) — all need cb=0.1, prefer `pred_momentum`
on, tight thresh (3-5 bp), and `per_order_widen_frac=0.7`. But the
signal recipe differs per symbol; TSLA's premium_tdc_s and curv values
were unusually distributed in winners — suggests TSLA's microstructure
has more equally-good operating points than the others.

---

## 8. Procedure for the next symbols (validated 2026-05-23)

Sequence of phases that worked end-to-end on SNDK / MU. Use this as the
default playbook for any new US equities pair.

> **For US equities, use 09:40 start from the FIRST step.** The tool
> applies this override automatically (`_apply_equity_us_day_override`
> is called from both `scan-thresh` and `run-spec`). Do not run any
> phase at 09:30 — open microstructure inflated 09:30 winners by ~40-60%
> on sharpe in the 2026-05-21 work.

> **For non-US sessions: pass `--session krxday|jpxday|usovernight|usday`.**
> Added 2026-05-30. CLI flag selects the window; the tool registers it
> in `coverage_autosearch.SESSION_TIMES` so the cloned spec's
> `"session": "KRX Day"` (etc.) resolves correctly. Hours (during EDT):
> - `usday`: 09:40–16:00 ET
> - `usovernight`: 18:00–09:30 ET (per rel-nq convention)
> - `krxday`: 20:00–02:30 ET (KRX 09:00–15:30 KST)
> - `jpxday`: 20:00–02:00 ET (JPX 09:00–15:00 JST)
>
> **Cross-day date semantics** (KR/JP): the sim labels a session by the
> ET *closing* day, which matches the Korean/Japanese calendar date.
> A `trd_*_20260528.csv` for KRX covers ET 5/27 20:00 → ET 5/28 02:30
> (= Mon 5/28 KST trading session). Verify by sampling timestamps in
> a trd CSV before promoting.

> **Don't run on partial data — hard rule (per memory).** Before any
> sweep (`scan-thresh`, Round A/B/C), explicitly enumerate the dates
> the tool will pick and confirm md_exists for BOTH feeds (local
> Hyperliquid + remote TopBookEquity/TopBookCme) on every date. The
> tool's auto-fetch silently fails on missing days, leaving you
> averaging over a partial window. Re-verify between rounds — files
> may land between Round A and Round B. See
> `feedback_communicate_dates_verify_md.md`. If anything missing:
> fetch, shrink the window, or shift the window backward to a fully-
> covered range. All symbols in a comparison must share the same
> window for cross-symbol comparability.

### Step-by-step

1. **Init spec** (1 min):
   ```bash
   relwide_equities_autosearch.py init-spec \
       --symbols SYM1,SYM2 --donor xyz:DKNG \
       --sim-days 25 \
       --workdir ~/scratch/relwide_retrains/YYYYMMDD_equities
   ```

2. **Fine scan-thresh** (~10 min for 2 symbols, 50 thresh values 1-50 bp):
   ```bash
   relwide_equities_autosearch.py scan-thresh --spec WORKDIR/spec.py
   ```
   Read `thresh_scan/summary.txt`. Identify **all** sweet-spot regimes
   per symbol (do not collapse to one row). Cover them in Round A's
   per-symbol place_thresh dim.

3. **Round A — placement** (~10 min): hand-write `spec_round_a.py`.
   Sweep: per-symbol `place_thresh` × `cancel_buffer [0.1, 0.2, 0.3, 0.5]`.
   ≈ 16-25 variants/sym.

   **Anchor the `place_thresh` dim on the VOLUME zone, not the top-sharpe
   peak.** scan-thresh's top-sharpe rows are often at 40-50bp+ with
   only 2-5 trades (statistical noise). Filter mentally for `num_trds ≥ 20`
   (~1+ trade/day over 25-day window) before reading sharpe. Pick a range
   that brackets the volume zone — signals (Round B) lift volume regimes
   much more reliably than they lift thin ones. See
   `feedback_anchor_round_a_on_volume.md`.

   - Report: top variants by sharpe AND by pnl AND by fillrate per symbol.
   - Lock dims with ≥70% concentration in top-30 healthy.

4. **Round B — signal** (~15 min): hand-write `spec_round_b.py`. Hold
   place_thresh/cancel_buffer at Round A per-symbol winners. Sweep:
   `pred_momentum_coef [0, 0.5, 2, 3]` × `pred_momentum_tdc [5, 30]` ×
   `curv_impulse_coef [0, 1.25]` × `premium_tdc_s [10, 30, 60, 120]`.
   64 variants/sym.

5. **Round C — broad grid** (~3h for 2 symbols, 1000 random samples):
   hand-write `spec_round_c.py`. 9-dim sweep:
   - `place_thresh`: per-symbol ±10% around anchor (3 values)
   - `per_order_widen_frac`: `[0.3, 0.5, 0.7]`
   - `rung_spacing_mult`: `[1.0, 1.5, 2.0]`
   - `curv_impulse_coef`: `[0.0, 1.25]`
   - `curv_impulse_tdc`: `[30, 240]`
   - `pred_momentum_coef`: `[0, 3, 5]`
   - `pred_momentum_tdc`: `[10, 30]`
   - `vol_norm_coef`: `[0, 0.1, 0.5, 1.0]`
   - `vol_norm_tdc`: `[10, 60]`

   5,184 Cartesian, cap at `max_variants=1000`, `random_sample=True`.
   Per-symbol `fixed` carries the held knobs from Rounds A/B.

6. **Rank + concentration analysis** (1 min): use the helper script
   pattern at the end of this playbook to print top-8 by sharpe, top-5
   by pnl, and ≥70% lock dims per symbol.

7. **Promote**: pick per-symbol winners using:
   - sharpe + pct_positive + trade count + fillrate together (not sharpe alone)
   - prefer high-volume / high-fillrate variants over sparse high-sharpe ones
   - read the per-day breakdown of the candidate (acct_*.csv) to confirm
     no single-day artifact is driving the win.

8. **Save promoted configs**: copy each winner's `pk_full.json` from
   `results/<sym>/scratch/<vid>/pk_full.json` to
   `WORKDIR/promoted/pk_<sym>_v<vid>.json`. Verify `settings.start_t`
   is 09:40 and `enabled=True`.

9. **Update the logbook section above** with the round results and
   the promoted configs.

### When to skip steps

- If 2 symbols are similar enough to share most dims (Round A and B
  produce the same locks), you can combine them into a single
  spec_round_c.py without Rounds A/B.
- If a symbol has < 50 healthy variants out of 1000 in Round C, treat it
  as fragile. Either deploy small (0.1-0.5×) or skip.
- If the sharpe leader has <30 trades over 18 days, prefer the
  high-volume runner-up.

### Quality bar (current)

```
filter:
    pct_positive >= 0.67
    avg_pnl      >  0          (for promotion only — keep for narrowing)
    num_trds     >= 20         (loosened from playbook's 50 for sparse names)
    fillrate     >= 0.005      (0.5% — equities tend to be sparse)
sort:
    sharpe descending
cross-check:
    pct_positive, num_trds, fillrate, per-day breakdown
```

### Analysis helper (paste into Python)

```python
import csv
from collections import Counter
path = "results/<sym>/results.txt"
with open(path) as f:
    rows = list(csv.DictReader(f))
for r in rows:
    for k in ("avg_pnl","sharpe","pct_positive","num_trds","fillrate"):
        try: r[k] = float(r[k])
        except: pass
healthy = [r for r in rows
           if r["pct_positive"]>=0.67 and r["avg_pnl"]>0
           and r["num_trds"]>=20]
by_sharpe = sorted(healthy, key=lambda r: r["sharpe"], reverse=True)
top30 = by_sharpe[:30]
for c in ["place_thresh","per_order_widen_frac","rung_spacing_mult",
          "curv_impulse_coef","pred_momentum_coef","vol_norm_coef"]:
    cnt = Counter(str(r[c]) for r in top30)
    top, share = cnt.most_common(1)[0]
    print(f"  {c}: best {top} ({share}/30) — "
          f"{'LOCK' if share>=21 else 'lean'}")
```

---

## 8a. Re-tuning an already-promoted symbol (iterative refinement from seed)

Added 2026-06-09. When re-running this procedure on a symbol that
already has a previous-batch promotion (`pk_xyz_<SYM>_v<NNN>.json` in
the promoted dir), do NOT start from scratch — seed from the old
config.

### Workflow

1. **Re-sim the old promotion** as a 1-variant spec on the current
   sim window. Compare to the original-batch stats. This validates
   whether the regime has shifted.

2. **Compare** to the top variant from a fresh wide search (if you've
   done one). Three outcomes:
   - **Old still strong + no fresh search winner clearly better →**
     keep the old promotion. No work needed.
   - **Fresh search top is clearly better →** promote it.
   - **Both are reasonable but old anchor wasn't covered by the fresh
     search range →** do a tight sweep around the old anchor (this step).

3. **Tight Round A** = vary placement dims ±1 step from old:
   - `place_thresh`: `[old - 0.5bp, old, old + 0.5bp]`
   - `cancel_buffer`: `[old, old±1 standard value]` (e.g. old=0.3 → [0.2, 0.3, 0.5])
   - `premium_ema_coef`: `[0.9, 1.0]` (fine grid — see Section 4d)

   Pin everything else to the old values.

4. **Tight Round B** = vary signal dims ±1 step from old:
   - `pred_momentum_coef`: ±1 standard value
   - `pred_momentum_tdc`: `[5, 30]`
   - `curv_impulse_coef`: `[0, 1.25]`
   - `premium_tdc_s`: ±1 standard value
   - `vol_norm_coef`: `[old, old±1]`

5. **Tight Round C** = vary geometry dims ±1 step from old:
   - `per_order_widen_frac`: ±1 standard value (use the name
     `widen_frac` in surfaced results, NOT `pow`)
   - `rung_spacing_mult`: ±1 (use `rung_mult` in tables, NOT `rsm`)
   - `curv_impulse_tdc`: `[30, 240]`

### Why

Saw 2026-06-09 across gf1 syms: a fresh-procedure search anchored on
slightly wrong cb / widen_frac for RIVN and ended up with 0/384
healthy in Round B. The old promotion (v4533) still ran at $11.74/d
/ pct+0.80 when re-simmed on the current window. We never explored
its region because Round B was anchored on a different cb.

Iterative refinement is cheaper too (~50-150 variants vs ~1000 for a
fresh Round B/C), and it preserves the prior tuning's signal.

## 4c. Sim model: lag-aware by default (2026-06-09)

The default donor (`pk_dkng_patched_v2_lagaware.json`) bakes in the
lag-aware sim settings from PR #927:

- `simulation.Hyperliquid.lag_aware_latency = true`
- `ioc_lag_base_ms=700`, `ioc_lag_coef=1.5`
- `alo_lag_base_ms=100`, `alo_lag_coef=0.8`
- `cxl_lag_base_ms=100`, `cxl_lag_coef=0.8`
- BTC added as an `extra_subs` on each pktrader (required for lag-aware
  to read HL feed lag)

Anything cloned from this donor inherits all of the above. Sim PnL /
fillrate / sharpe are now calibrated against the June 2026 live SVL
study — they're a believable proxy for live behavior, not the optimistic
old-sim numbers.

**Comparing old-sim picks against lag-aware picks** (seen 2026-06-09 on
the gf1 re-validation): lag-aware sim typically produces:
- 20-70% **higher** avg_pnl (fewer adverse-selection fills are kept)
- 10-15% **lower** num_trds (latency means more orders miss)
- Materially **lower** fillrate (often 0.3-0.5% on US equities vs
  old-sim's 0.5-1.5%). The 0.5% fillrate quality bar may need to be
  lowered to 0.3-0.4% under lag-aware.

For details on the calibration:
`overmind/studies/lag_aware_sim_svl_settings_20260609.md`.

## 4d. Parameter conventions and ranges

Added 2026-06-09.

### Naming in surfaced tables

When printing markdown tables or summary rows, use these abbreviations:

| Full param | Use abbrev | Avoid |
|---|---|---|
| `per_order_widen_frac` | `widen_frac` | ~~`pow`~~ (looks like exponent) |
| `rung_spacing_mult` | `rung_mult` | ~~`rsm`~~ (looks like RMS) |
| `place_thresh` | `pt` | |
| `cancel_buffer` | `cb` | |
| `premium_ema_coef` | `ema` | |
| `premium_tdc_s` | `pre_t` | |
| `pred_momentum_coef`/`tdc` | `pm_c`/`pm_t` | |
| `curv_impulse_coef`/`tdc` | `cu_c`/`cu_t` | |
| `vol_norm_coef`/`tdc` | `vn_c`/`vn_t` | |

### `premium_ema_coef` sweep granularity

**Canonical sweep: `[0.9, 1.0]` (NOT `[0.5, 0.8, 1.0]`).** 0.5 vs 1.0
is a very different regime — 0.5 mostly disables the premium reaction,
1.0 fully uses it. A coarse sweep finds 0.5 "winning" by accident on
volatile symbols when the real optimum is near 0.9. If you suspect a
sub-0.9 optimum, sweep `[0.8, 0.85, 0.9, 0.95, 1.0]`. Don't jump to 0.5.

### Phased dim assignment (refined from §2)

- **scan-thresh:** `place_thresh` only.
- **Round A:** `pt × cb × premium_ema_coef`. (Refined 2026-06-09:
  premium_ema_coef belongs in Round A, not Round B. Standard sweep
  `[0.7, 0.8, 0.9, 1.0]` — see §4d granularity rule.)
- **Round B:** `pt±10% × pred_momentum × curv_impulse_coef × premium_tdc × vol_norm`.
  (Refined 2026-06-09: vol_norm_coef belongs in Round B, not Round C.
  Every Round C winner across the May-June batch had vol_norm>0.)
- **Round C:** `pt±10% × widen_frac × rung_mult × curv_impulse_tdc` (geometry).
- **Round D (final polish):** `pt±10%` plus ±10% on each other tuned
  dim around the Round C winner. See below.

**Rule: each dim lives in exactly one round, EXCEPT `pt` which gets
a ±10% drift in every round from B onward.** Don't re-sweep
`pred_momentum_coef`, `curv_impulse_coef`, or `premium_tdc_s` in
Round C — those are Round B's job. Doubling up wastes compute and
yields ambiguous attribution. The ±10% `pt` drift is the exception
because `pt` is the most sensitive dim and small drift catches local
optima the Round A grid couldn't resolve.

**Rule: every swept grid MUST include the anchor's value for that dim
(when seeding from an old promotion).** If the old anchor uses
`pre_t=30`, the new sweep must include 30 even if your standard grid
is `[10, 60, 120]` — extend it to `[10, 30, 60, 120]`. Otherwise the
new round can't revisit the old config and may "discover" a worse
winner that's just the closest grid point to the actual optimum.
Bit us 2026-06-09 on the first gf1 re-validation pass — Round B's
`pre_t ∈ [10, 60, 120]` missed NVDA's anchor value of 30 and several
syms' Round B winners were materially worse than re-simmed old. Apply
this rule to all rounds: scan-thresh (pt grid), A (pt × cb × ema), B
(signals), C (geometry), D (polish).

### Round D — final ±10% polish

Added 2026-06-09. After Round C picks a winner, run a tight sweep
that varies each tuned dim by ±10% around the winner's value. Catches
the case where the winner sits at a local optimum that's slightly
off-axis from the grid we swept in A/B/C.

For each dim, generate `[winner × 0.9, winner, winner × 1.1]`. For
bounded params (e.g. `premium_ema_coef ∈ [0, 1]`), clamp. For very
discrete dims with only 2 values (e.g. `curv_impulse_coef ∈ {0, 1.25}`),
just keep at winner's value (no meaningful ±10% step). For pm_tdc/cu_tdc
(time constants in ms or s), ±10% snaps to the nearest standard
value (5/10/30/60/120/240).

Total dims: ~5-7 (skipping the very discrete ones) × 3 vals each =
243-2187 Cartesian. Cap `max_variants=500` with `random_sample=True`.
sim_days=25 as default.

If Round D winner ties or improves Round C winner, promote Round D
winner. Otherwise keep Round C.

---

## 8c. Promotion knob overrides — always explicitly set

Added 2026-07-13. When promoting a variant into a live-shipping config
via clone-then-set pattern (`copy.deepcopy(donor_entry)` → target sym),
some knobs must be **explicitly overridden**, not inherited from the
donor. Silent inheritance has caused live incidents.

### Must-override list per promoted sym entry

| Field                    | Default to set  | Why                                                                                        |
|--------------------------|-----------------|--------------------------------------------------------------------------------------------|
| `size_mult`              | **1**           | Live order-size scalar. DKNG donor has 4 → inherits by default. Caught 2026-07-13 after 7 Asian syms shipped with size_mult=4. |
| `enabled`                | **true**        | Some donors have `enabled: false` from prior disable — inherit will silently skip the sym. |
| `risk.max_position`      | sym-specific    | Donor `max_position` (e.g. `"600"`) is calibrated for donor's price. Reset to reflect target sym's typical size. |
| `risk.max_notional`      | sym-specific    | Same reason as max_position.                                                              |
| `ordex.max_pos`, `order_size`, `tgt_maxpos_notional`, `tgt_order_notional` | sym-specific | Same rationale. Don't ship with donor values. |

Also verify (don't need to override, but check):
- `ordex.trade_caller` — should be `rel_tradecall_tempo_<sym>` after
  clone, not `remote_tradecall_tempo_<sym>` (dodge the PURRDAT clone-
  patch conflict). If donor uses the `remote_` prefix, rename to `rel_`
  before promoting.
- `size_mult` in `queue_jump_size_mult`: this one **should** stay at the
  donor's 3 (it's a queue-jump ratio, not a global scalar). Different
  knob, don't confuse.

### Snippet — promotion clone pattern

```python
def promote(donor_entry, target_sym, tuned_ordex_params):
    e = copy.deepcopy(donor_entry)
    e = _rename_donor_sym_to_target(e, donor_sym, target_sym)  # string replace
    # === MUST-OVERRIDE (do these unconditionally) ===
    e['size_mult'] = 1
    e['enabled']   = True
    # Then apply the tuned ordex params:
    e['ordex'].update(tuned_ordex_params)
    return e
```

Add these overrides to any promotion script. Missing them silently
ships risky configs.

---

## 8b. Remote ticker mapping (HIP3 ↔ remote feed)

Added 2026-05-30. Most HIP3 contracts map 1:1 to their remote ticker
(`xyz:AAPL` ↔ TopBookEquity `AAPL`), but some don't:
- `xyz:KR200` → TopBookEquity `KOSPI200`
- `xyz:JP225` → TopBookEquity `NIKKEI225` (**updated 2026-07-10**: was
  previously `TopBookCme/NIY`, but NIY is the *relative* signal in
  production, not the *remote* — the registry's "remote_*" field maps
  to the `remote_mid` sig which uses the equity index feed. The NIY
  futures signal is separately wired as `rel_mid_JP225` in the donor.)

The tool consults `symbolizer.relwide_remote_wiring` automatically:

1. **md_exists pre-fetch** uses the correct remote ticker + market via
   `_remote_ticker_and_market_for(hl_symbol)` in `relwide_equities_autosearch.py`.
2. **Clone post-processing** (`_install_remote_ticker_clone_patch`)
   rewrites the cloned config so remote-side signal/tempo names AND
   their `symbol` / `books` / `markets` fields reference the remote
   ticker. Without this, the sim looks for nonexistent files (e.g.
   `KR200_quotes_*.gzpbf` in TopBookEquity) and produces zero trades.

To add a new HIP3 contract whose remote name differs, add it to
`symbolizer.relwide_remote_wiring`. The tool picks it up.

---

## 10. Non-US-session retrains (Asian syms, added 2026-07-10)

Retraining KRX/JPX/HK-session syms follows the same round progression
(scan-thresh → A → B → C → promote) but needs infra prep beyond what
US-hours syms need.

### 10a. Session windows

Sessions live in `SESSION_WINDOWS` inside
`overmind/strat_main/tools/trademan/relwide_equities_autosearch.py`.
Current values:

| Session key      | Description                                        | ET window       |
|------------------|----------------------------------------------------|-----------------|
| `krxday`         | KRX equity 09:00-15:30 KST                         | 20:00-02:30 ET  |
| `krxday_futures` | KOSPI 200 futures 09:00-**15:45** KST              | 20:00-02:45 ET  |
| `jpxday`         | JPX continuous 09:00-15:30 JST (incl closing auc)  | 20:00-02:30 ET  |
| `jpxday_am`      | JPX morning 09:00-11:30 JST                        | 20:00-22:30 ET  |
| `jpxday_pm`      | JPX afternoon 12:30-15:30 JST                      | 23:30-02:30 ET  |
| `hkxday_am`      | HKEX morning 09:30-12:00 HKT                       | 21:30-00:00 ET  |
| `hkxday_pm`      | HKEX afternoon 13:00-16:00 HKT                     | 01:00-04:00 ET  |

**DST caveat:** ET values shift with US DST twice a year (JPX/KRX/HK
don't observe DST). Session strings hardcode ET times — revisit at
DST transitions (Nov 2026 and Mar 2027).

**Split-session syms** (SOFTBANK/KIOXIA on JPX, ZHIPU/MINIMAX on HK)
get separate workspaces per session (`<SYM>_am/`, `<SYM>_pm/`) and
separate scan-thresh / Round A/B/C runs. Each session is its own
"sim unit" for the swarmhost cap.

### 10b. Picking a relative signal (Asian syms don't default to BTC)

Use cross-symbol OLS correlation across the HIP3 universe:
1. Extract 1-min mid returns per HL sym via the `returner` bin
   (`bin/returner --symbol xyz:<SYM> --date <YYYYMMDD> --out <csv>`).
2. Aggregate to a wide DataFrame (rows = minute-timestamps, cols =
   syms), resample to **5-min** returns (sums; log returns are additive).
3. For each target sym × candidate sym, run OLS: `y = α + β·x + ε`
   filtered to the target's market-hours window. Rank by R².
4. Pick the top candidate with R² ≥ 0.10. Below that, no relative.

Wire the picked relative into the donor:
- `relative_sig`: the sym-specific rel signal name (e.g. `rel_mid_EWY`)
- Signal block: `SigMid` on `xyz:<REL>` in `Hyperliquid` (HL-native
  relatives are simplest). For JP225 the production choice is `NIY` on
  `TopBookCme` — a high-freq CME futures signal that meaningfully beats
  the HL alternatives.
- `trade_caller` tempo: point at the relative feed (dispatches quotes
  when the relative moves)

Translate OLS output into config parameters:
- `relative_beta` = OLS β directly (not 1.0 or 0.0)
- `beta_uncertainty_coef` = piecewise buckets by R²:

| R² bucket   | `beta_uncertainty_coef` |
|-------------|-------------------------|
| ≥ 0.40      | 0.5                     |
| 0.25 – 0.40 | 0.8                     |
| 0.10 – 0.25 | 1.5                     |
| < 0.10      | skip relative (β=0)     |

Empirically established Asian-sym mappings (as of 2026-07-10):

| target    | rel signal         | β    | R²   | unc |
|-----------|--------------------|------|------|-----|
| SMSN      | xyz:EWY (HL)       | 0.86 | 0.45 | 0.5 |
| SKHX      | xyz:EWY            | 0.95 | 0.41 | 0.5 |
| HYUNDAI   | xyz:EWY            | 0.36 | 0.09 | 2.5 (or skip) |
| KR200     | xyz:EWY            | 0.69 | 0.39 | 0.8 |
| JP225     | NIY (TopBookCme)   | 1.0  | prod default | 0.5 |
| KIOXIA    | xyz:JP225 (HL)     | 3.30 | 0.31 | 0.8 |
| SOFTBANK  | (none)             | —    | —    | —   |
| ZHIPU     | (none)             | —    | —    | —   |
| MINIMAX   | (none)             | —    | —    | —   |

### 10c. Donor building

**Donor configs** live in
`~/scratch/coverage_autosearch/<date>_asian/pk_<SYM>_<session>_rel_<REL>_lagaware.json`.

Build them by:
1. Load DKNG allday donor as base
2. Rename donor sym → target sym (recursive string replace)
3. Set `settings.start_t/end_t` to the session default (tool overrides
   at run time via `--session`, but donor default should be sensible)
4. Swap the relative signal block for the picked relative:
   - Update `ordex.relative_sig`, `relative_beta`, `beta_uncertainty_coef`
   - Replace the `rel_mid_<SYM>` signal entry with `rel_mid_<REL>`
     pointing at the correct symbol/books
   - Repoint the `rel_tradecall_tempo_*` tempo at the relative feed
5. Fix `remote_sig` symbol/books if remote equity ticker differs from
   target short (e.g. JP225 → NIKKEI225, KR200 → KOSPI200)
6. Rename any `remote_tradecall_tempo_*` → `rel_tradecall_tempo_*` to
   dodge the clone-patch's `remote_`-prefix rewriting (a bug caught in
   the PURRDAT $299B incident 2026-06-25)

**Registry** (`symbolizer.relwide_remote_wiring`): add one entry per
new HL sym mapping (remote_symbol, remote_market). The clone-patch and
pre-fetch consult this. Mismatch with the donor → scan-thresh looks in
the wrong dir. See §8b.

### 10d. Production configs as sim-donor references

Live Asian configs are JSON-with-comments. Load with `commentjson`
(not stdlib `json`, which errors on comments):

```python
import commentjson
d = commentjson.load(open(path))
```

Reference paths (as of 2026-07):
- KRX single-names: `~/scratch/tradeperf/gf3/combined_foreign_gf3/cov_0521/pk_gf3_kor.json`
- JP225: `~/scratch/tradeperf/gf3/jp225/allday/pk_jp225_rel_niy_allday.json`
- KR200: `~/scratch/tradeperf/gf3/kr200/allday/pk_kr200_rel_ks_allday.json`
- JPX AM/PM: `~/scratch/tradeperf/gf3/japan/{am,pm}/pk_japan_{am,pm}.json`
- HK AM/PM: `~/scratch/tradeperf/gf3/hkstocks/{am,pm}/pk_hkex_{am,pm}.json`
- Live perf trades: `~/scratch/tradeperf/gf3/combined_foreign_gf3/<variant>/trades_*.csv`
  (per-sym trades live inside COMBINED-strategy dirs, not per-sym dirs)

### 10e. Data staging quirks

- **Weekend sentinels** are needed for BOTH sides:
  - HL: `xyz:<SYM>_l2Book_*.gzpbf`, `xyz:<SYM>_trades_*.gzpbf`
  - Equity: `<REMOTE>_quotes_*.gzpbf`
  - CME (for NIY): `NIY_quotes_*.gzpbf` in `TopBookCme/`
- **BTC HL feed** is used as an aux `extra_sub` in donors; recent BTC
  dates (today, yesterday) may need sentinel staging too.
- **Fresh listings** (SOFTBANK/KIOXIA/ZHIPU/MINIMAX 2026-06-13+) have
  ~2-3 weeks of history. Set `sim_days=13-15` and pre-stage sentinels
  for pre-listing dates the tool scans.
- The tool's date list looks back from *today*: keep sentinels current.

### 10f. Ordex style: US vs Korean

Empirical finding (SMSN Track 3 A/B, 2026-07-08): Korean-style ordex
outperformed US-style for HL Korean equity perps by 2.4× sim_score
(baseline 35 → Korean-style 160 for SMSN).

|                        | US-style   | Korean-style |
|------------------------|------------|--------------|
| `ladder_one_sided`     | true       | **false**    |
| `ms_between_place`     | 400        | **250**      |
| `min_ord_lifetime`     | 1.5        | **0.5**      |
| `max_back_levels`      | 3          | **4**        |
| `per_order_widen_frac` | 0.525      | **0.5**      |

Try both styles as parallel tracks in Round A for Asian syms. Working
theory: `ladder_one_sided=false` (two-sided quoting) is the biggest
factor — Asian HL perps have genuine two-sided flow, unlike US perps
which tend to have persistent one-sided drift.

### 10g. Promotion file layout

Different sessions = different `settings.start_t/end_t`, so they can't
share a single pk file. Convention:

- `pk_gf3_asian_krxday.json` — SMSN, SKHX, HYUNDAI (krxday 20:00-02:30)
- `pk_gf3_asian_krxday_futures.json` — KR200 (krxday_futures 20:00-02:45)
- `pk_gf3_asian_jpxday.json` — JP225 (jpxday continuous 20:00-02:30)
- `pk_gf3_asian_jpxday_am.json`, `pk_gf3_asian_jpxday_pm.json` —
  SOFTBANK, KIOXIA (split; separate configs)
- `pk_gf3_asian_hkxday_am.json`, `pk_gf3_asian_hkxday_pm.json` —
  ZHIPU, MINIMAX (split)

Backup convention: `.bak`, `.bak2`, ... on each promotion as usual.

---

## 9. Open follow-ups (parking lot)

- Plumb `fillrate_target` override through `sim_grid` — currently it's
  hardcoded in `SimVariations.py:260` at 0.005. For now we re-rank from
  raw fields and accept that the sim's `sim_score` column uses 0.005
  internally.
- Auto-detect single-value place_thresh dims in spec generation and emit
  a warning (carry-over from coverage_autosearch follow-ups).
- Patch `coverage_autosearch.run_spec` to fetch both local AND remote
  feeds when the trader has a `remote_sig` on a non-local market.
- Decide whether to extend the equity universe to GF3 names (LITE, CRWV,
  RKLB, MRVL, CRCL, DRAM, BIRD, etc.) that overlap with the RelCross
  workstream.
