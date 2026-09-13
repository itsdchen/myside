# HYPE Signal & Execution Study

Combined empirical record (what's been tried, what's been ruled out) and
forward-looking signal architecture (what to build next) for the HL/HYPE
research arc running April–May 2026.

**Scope.** Two parallel threads:
- *Execution-quality* — fill-level diagnosis on `pnlclimb_wide_v3`,
  regression-driven gating experiments, parameter grids.
- *Signal-ceiling / SigDer architecture* — single-feature R² climbs per
  signal family, SigDer wrapper iteration, candidate-signal review.

Working dir for raw artifacts: `~/scratch/hype_20260407/claude_iter/`.
Chronological logs in that dir: `NOTES.md`, `SUMMARY.md`, `SIGNAL_WORK.md`.
Baseline strategy: `~/scratch/hype_20260407/pnlclimb_wide_v3/final_pk.json`
(HYPE, WideMM, max_pos=30, order_size=3, place_thresh=0.00107). OOS
R²≈0.034 on the 58-feature SigLinear pred model.

---

## I. Empirical findings

### Signal-side R² is already strong; the leak is execution.
OOS R²≈0.034 is strong for HFT. Realized PnL on 16 OOS days (1967 fills,
20260313–20260403): **+$77.15**. Total size-weighted markout at 120s:
**−$51.06**. Oracle ceiling (skip every negative-markout fill): **+$134**.
**>$57/44-days of separable PnL sits in execution quality**, not in
chasing more R².

### Sell side is 90% of the regret.
Adverse markout breakdown:
- Sell-side: −$45.97 (89.9% of total)
- Buy-side:  −$5.08

Single strongest empirical fact in the study. **Symmetric threshold/gating
rules cannot fix this by construction** — fixes must be direction-aware
(curvicity machinery) or inventory-aware (per-side state gates). Static
side-bumps are sample-selecting.

### The leak is adverse selection, not stale quotes.
Markout horizon is monotone-negative from 5s out to 120s. Pure toxicity.
Faster-cancel / tighter-update mechanisms do not target this problem.

### v3 sits at the parameter optimum.
- `place_thresh = 0.00107` peaks across every `max_pos` ratio in the 42-cell
  ratio×threshold grid.
- `max_back_levels = 4, per_backlevel_mult = 1.5` peaks the 30-cell
  rung-spacing grid (Sharpe 8.22).
- `max_pos = 30` is within ~$50/44-days of optimal; ratio 15 (max_pos=45)
  gives +12% with a plateau above.

No easy wins remain from re-climbing v3's existing knobs. Improvements
need new mechanisms or new features.

### TrdImp is the only signal-family ceiling above mondo's.
Single-feature R² ceilings (CMA-ES, 9 days HYPE Feb 2026, 60s markout):

| Family               | Ceiling R² | In current pred model? |
|----------------------|-----------:|:---:|
| **trdimp**           | **0.01619**| **no**  |
| mondo_deepbookder    |   0.01583  | yes |
| deepbook (post-iter) |   0.01535  | yes |
| deepbookder          |   0.01502  | yes |
| avgtrdder            |   0.01197  | yes |
| bookimp              |   0.01093  | yes |

TrdImp is the highest standalone family measured and is **not in the
current 58-feature pred model**. Trade-event-based, structurally
orthogonal to the book-state families. **Ensemble contribution not yet
measured.**

### Within-family wrapper modifications consistently regressed.
SigDer-machinery edits (mondo F/G tags) and wrappers around the new
TrdImp ceiling all reduced R². Highlights vs the relevant baseline:

| Tag                | R²       | Δ                  |
|--------------------|---------:|-------------------:|
| mondo G1 (RMS)     | 0.01378  | −12.6% vs 0.01577  |
| mondo G2 (tgt_sync)| 0.01367  | −13.3% vs 0.01577  |
| trdimpder          | 0.01334  | −17.6% vs 0.01619  |
| trdimplibra        | 0.00015  | broken (scale mismatch) |

Code from these experiments has been **reverted to HEAD**. The session
produced one strong positive (TrdImp), one neutral architectural cleanup
direction (the Tier-0 mechanics on SigDer — reverted), and several clean
negatives.

---

## II. Concrete uncommitted wins

1. **Bump `max_pos` 30 → 45.** Expected +$50/44-days (+12% PnL),
   Sharpe 5.74 → 5.89. Low-risk; benefits from re-climb at the new ratio.
2. **Add `SigTrdImp` to the 58-feature pred model.** Standalone R² = 0.01619,
   structurally orthogonal to the current families. Pinned best params
   (from `bench_family.py trdimp cmaes`):
   ```
   sz_decay              5.68e-06     tgt_decay_disagree    0.1694
   time_decay            12191.19     bungee_mult_frac      0.9557
   killed_lvl_boost      1.518        bungee_denom          0.8227
   tick_decay            0.9916       draped_tdc_s          4.742
   tgt_sig_decay         0.9997       px_decay              1.41e-04
   ```
   Caveat: standalone ≠ ensemble contribution; marginal R² unmeasured.

---

## III. Ruled-out methodologies

### Per-event regression on market tape → place_thresh gate.
Trained direction-aligned regression on 1.3M trade events × 14 days.
Walking-window OOS R² = +0.0032, 5/6 windows positive. Wired top features
(`thresh_imbalance_coef`, `thresh_mom_4s_coef`) into wide_mm.
**Failed in 44-day full sweep for both features in both sign directions.**
The 16-day window initially showed +$67 (+87%, t=1.24); 44-day extension
flipped to −$152.

**Root cause**: selection-conditional correlation. Regression trained on
hypothetical passive fills at `agg_px` for every market trade. Real fills
happen only where our quotes sit — a different conditional distribution.
The correlation does not survive the intervention.

Rules out TradeRecorder+oracle_dp+regression as a source of features for
ordex-level place_thresh gating on HYPE. Oracle DP infrastructure remains
useful for ceiling references.

### Walk-cost-gated place_thresh rule.
On HYPE, walk-cost at X=$5k–80k is 0.81-correlated with raw `local_spread`
because HL's TOB has ≥$80k. A walk-cost rule reduces to
`place_thresh_mode_1` (`wide_mm.cc:642-646`). **No incremental value for
HYPE.**

### Depth-aware gating beyond TOB on HYPE.
All three `sig_liq_balance` walks (X=20k/40k/80k) return identical values
on HYPE because of TOB depth. Multi-level depth gating is not a HYPE lever.

### SigDer wrappers don't lift single-feature ceilings.
- DeepBook → DeepBookDer: −2.2%
- TrdImp → TrdImpDer: −17.6%
- Three SigDer-internal modifications (Tier 1 RMS normalization, Tier 2
  tgt-synced impulse, mondo F-tags): all regressed 5–13%.

The Tier 0 cleanups (time-decay on tgt, `iv_` for direction) were
neutral on the ceiling. All Tier 0/1/2 SigDer code reverted.

### SigLibra(SigTrdImpulse, SigMid) is structurally broken.
SigLibra adds `sig_a_scale * sig_a + sig_b_scale * sig_b` on raw values.
SigTrdImpulse outputs a sigmoid-bounded flow (~±0.5); SigMid outputs raw
mid (~$25–50). Scale mismatch; sig_b dominates. Existing Libra specs
work because their pred children use `return_price=true`. SigTrdImpulse
has no equivalent flag.

---

## IV. Infrastructure left in tree

All no-op at default settings unless explicitly toggled. (Reverted items
no longer listed.)

- **`src/pktrade/toolbins/fillstats.cc`** — `walk_bb_px`, `walk_ba_px`,
  `walk_bb_notl`, `walk_ba_notl` columns + `--walk-notional` flag.
- **`src/pktrade/ordex/trade_recorder.{cc,h}`** — 11 new feature columns
  (tob_imbalance, microprice_dev, signed flow at 100ms/500ms, instant 5-tick
  depth, book_update_rate, three sig_liq_balance variants).
- **`pybin/TradeRecorder.py`** — HYPE support (`_data_symbol`, REMOTE_REF
  self-mapping, SigMid fallback).
- **`overmind/strat_main/optimizers/`** — five new climbers (CmaEsClimb,
  BayesClimb, TpeClimb, TurboClimb, GroupedClimb) + `len(stats) > 0` guard
  bug fix on six climber files.
- **`overmind/strat_main/util/signaloptimizer.py`** — single-feature
  ceiling-climb tool (drives `bench_family.py`, `iter_*.py`).

---

## V. Forward architecture: signal class menu

### Current stance
Prioritize features studyable from existing historical `l2Book` and
`trades` data. HL context fields (oracle, mark, premium, funding, OI)
should not become core HFT alpha inputs until backfilled or recorded
forward. They are still useful as live risk controls / gates
(e.g. oracle dislocation → throttle/widen).

### Near-term plumbing priorities
1. **Propagate HL order count (`n`)** from `l2Book` through `BookManager`
   into `LevelAdd/Modify/Delete`. Unlocks order-count features.
2. **Build trade-flow base signals** from existing `trades` stream.
3. **Add reusable operator signals** so normalization, decay, clipping,
   gating, and derivative logic stop being re-embedded inside base signals.
4. **Revisit BBO** only after measuring live cadence vs `l2Book`. Faster
   for TOB changes but not in current historical data — needs a live
   logger first.

### Trade-flow base signals (highest-priority candidates)

| Class                | One-liner |
|----------------------|-----------|
| `SigCVD`             | Signed aggressive volume/notional accumulator; time-, event-, or notional-normalized; same-side burst boost variant. |
| `SigTradeBurst`      | Trade count / notional / signed acceleration over short windows. Use as gate or regime detector. |
| `SigSweep`           | Per-batch sweep depth/notional/levels-crossed. Cleaner primitive than `SigTrdImpulse`'s mixed responsibilities. |
| `SigAbsorption`      | Aggressive flow without commensurate mid move; passive-side strength indicator. Often more useful than raw CVD. |
| `SigFlowBookDivergence` | Signed trade flow vs book imbalance. Natural SigLibra input pair. |
| `SigTradePxPressure` | Signed recent trade VWAP vs mid/BBO/liquidation. Cleaner than `SigTrdPx`. |

### Book/structure signals (assume order count plumbed)

| Class                       | One-liner |
|-----------------------------|-----------|
| `SigOrderCountImbalance`    | Top-N order-count imbalance; size-per-order; concentration scores. **Highest-value HL addition** — `n` is already in the wire payload. |
| `SigBookChurn`              | Add/del/mod notional rates. Gate for book-based signals; replenishment-after-sweep feature. |
| `SigReplenishment`          | Time-to-recover and replenished-fraction after a sweep on the touched side. |
| `SigBookAge / SigStaleDepth`| Age-weighted depth imbalance. Standalone gate or alpha. |
| `SigSpreadState`            | Width, z-score, widen/tighten impulses. Useful gate. |
| `SigMicroprice`             | Classic + multi-level + queue-adjusted variants. Order-count-adjusted version is new. |
| `SigQueueFragility`         | Probability-like score that touch breaks soon, from size/count/recent dels/recent trades. |
| `SigBookSlope / Convexity`  | Cumulative-depth slope and convexity. Sweep-continuation predictor. |
| `SigLiquidityWall`          | Outsized resting levels vs local depth distribution. Order count distinguishes real vs spoof walls. |
| `SigTradeToBookRatio`       | Aggressive notional / visible liquidity. Compact toxicity signal. |

### Risk / regime signals

| Class                 | One-liner |
|-----------------------|-----------|
| `SigLatency / FeedHealth` | event→rx delay, snapshot/trade gaps. Gate, not alpha. |
| `SigMarketMode`       | Composite classifier over spread/churn/sweep/burst/replenishment. Higher-level operator. |
| `SigCrossAssetLeadLag`| BTC/ETH leading alt/HIP-3, ETF/index leading single names. Requires synchronized data. |

### Context signals (defer until historical recording)
`SigOraclePremium`, `SigFundingPressure`, `SigOIMomentum`,
`SigImpactPxBasis`. First-use as risk controls / regime gates, not
model-fit features.

### Generic operator signals
Pull repeated logic out of base signals. Composable graph: base →
`SigLagDiff` (or `SigDer`) → `SigEma` → `SigZScore` → `SigGate` → `SigShape`.

| Operator                | Purpose |
|-------------------------|---------|
| `SigResidual / SigReconcile` | Tracks unresolved diff between two comparable signals. **Libra-family** (the "consumption model" idea). |
| `SigEma`                | Generic time/event/hybrid EMA. Replaces ad-hoc decay logic. |
| `SigZScore / SigRmsNorm`| Rolling normalization (RMS, z-score, EW, robust). |
| `SigClip / SigShape`    | Output shaping (clip / tanh / sigmoid / signed-power / deadband). |
| `SigGate`               | One child scales/zeros another by gate predicate. |
| `SigLagDiff`            | Derivative over configurable lag (updates / ms / last-before-target). |
| `SigAgreement`          | Sign/magnitude agreement between two children. Simpler explicit form of partial SigLibra logic. |

Detailed pseudocode sketches for these classes were drafted in
`signal_review_notes.md`; recoverable from git if needed for
implementation. Dropped here for brevity.

---

## VI. SigDer review + roadmap

### Empirical results from this session

| Step                         | Result                |
|------------------------------|-----------------------|
| Tier 0: time-decay on tgt + `iv_`-direction-test | Neutral on R² (reverted, but mechanically correct cleanup) |
| Tier 1: `normalization` enum (raw / returns / rms) | RMS variant lost −12.6% on mondo (reverted) |
| Tier 2: `impulse_mode = tgt_synced` | Lost −13.3% on mondo (reverted) |
| Wrap TrdImp in SigDer (`SigTrdImpDerSpec`) | Lost −17.6% vs standalone TrdImp (reverted) |

For book-state and trade-event preds at 60s horizon on HYPE, **per-tick
raw-diff impulse is near-optimal**; SigDer machinery modifications
explored so far consistently subtract.

### Implementation-correctness items still untested

These are correctness/safety improvements drafted in
`signal_review_notes.md` and **independent of the alpha-machinery
modifications that failed**:

1. **Reset/validity safety.** `reset()` doesn't clear `iv_` / `value_` /
   validity. `onSignalValidity(false)` doesn't call `setValidity(false)`.
   Downstream signals may treat stale SigDer as valid.
2. **`isSame()` asymmetric optionals.** Optional-field handling can dedupe
   configs incorrectly. Clamped `tgt_decay_*` are compared raw.
3. **Zero guards.** `take_pred_returns` divides by `|last_pred|` with no
   floor; scaled tgt decay divides by `last_tgt_val_` (not `abs`, not
   guarded).
4. **Initial-state pollution.** `last_tgt_val_` starts at 0; first tgt
   update decays against zero baseline.
5. **`defer_decay_` is non-functional** — `defer_decay_t_` is always 0ms,
   no `defer_decay_ms` config exists. Either implement or remove.
6. **Clocked/end-of-event mode.** Currently reacts immediately to each
   child callback. When pred and tgt share an underlying event, output is
   path/order dependent. A `FINAL`-tick-coalesced mode is likely the
   biggest correctness lift available.

### Empirically-tested-and-dead items
Originally proposed in `signal_review_notes.md`'s SigDer roadmap; tested
this session for HYPE DeepBook/TrdImp preds; **dead ends for these pred
types** (may still apply to noisier/non-price preds elsewhere):

- "Replace `take_pred_returns` with `pred_delta_mode`" — RMS variant
  regressed; raw is correct for book/trade preds.
- "Add lagged derivative support" — tgt-synced variant regressed; per-tick
  is correct here.

### Recommended order for any future SigDer work
1. Items 1–4 from "untested" above — pure correctness, low risk.
2. Item 6 (clocked/coalesce same-timestamp child updates) — likely the
   biggest correctness lift; addresses path-dependence in current code.
3. Item 5 (defer_decay decision: implement or remove).
4. **Component telemetry + SigDer scanner.** Bucket events by impulse size,
   age, target movement, spread, flow regime; report forward markouts.
   Probably higher ROI than expanding the parameter space further.
5. **New mechanics** (deadband, normalization modes, lagged variants) are
   not justified on the current data. Revisit only if a specific pred
   type / horizon shows symptoms that one of these would target.

### Architectural note
Output shaping (sigmoid, clip, normalization) belongs in generic
operator signals (`SigShape`, `SigClip`, `SigZScore`), not inside SigDer.
The cleaner long-term graph is: `base → SigLagDiff → SigEma → SigZScore
→ SigGate → SigShape`.

---

## VII. Methodological lessons

1. **Small-sample realized-PnL improvements are dangerous.** First 16-day
   window of the imbalance rule showed +$67 (+87%, t=1.24). 44-day
   extension: −$152. The feature was dead. Don't ship before extension.
2. **Correlation at event level ≠ causal gating at fill level.** A
   feature with real OOS R² in a regression doesn't translate to PnL when
   used as an ordex gate. The intervention changes which events you fill.
3. **Train on your own fills, not on the market tape.** Fillstats-style
   analysis on actual fills is methodologically sound. Oracle DP /
   market-tape regressions are for ceilings, not gating rules.
4. **Symmetric rules can't address sample-dependent asymmetries.** Any
   permanent side bump is sample-selecting. Only direction-aware /
   inventory-aware mechanisms are robust.
5. **When a parameter is already climbed, incremental wins are small.**
   v3's climb landed near-optimal on the key knobs. Easy fruit is picked.
6. **Single-feature R² ≠ ensemble contribution.** Modest standalone
   ceiling can still add meaningful marginal R² if it captures
   orthogonal information. Open test for TrdImp.
7. **CMA-ES noise floor ≈ ±0.00006 R² on these climbs.** Two same-config
   runs landed 0.01577 / 0.01583. Calibration for "real vs seed noise."
8. **Single-feature ceiling ≠ wrapper-friendly.** Standalone-optimal
   params for a pred (e.g. TrdImp) need not be optimal when fed into
   SigDer/SigLibra. Wrapper experiments should climb child + wrapper
   jointly to give the wrapper a fair shot.

---

## VIII. Open directions ranked by ROI

### Highest value / lowest cost
1. **Front-rung vs back-rung markout split.** 10 min of pandas on
   existing fillstats data. Strong prior: front-rung adversely selected,
   back-rung benign. "Back-rung-only in stressed regimes" mode is
   orthogonal to place_thresh.
2. **Post-fill cooldown on adverse side.** Monotone-negative markout
   horizon implies: after a bad fill, drift continues — don't re-place
   side S for N seconds. New code in `wide_mm`.
3. **Inventory-vs-state diagnostic for sell-side regret.** Correlate
   adverse sell markout with `end_pos` at fill. Disambiguates
   inventory-driven (curvicity fix) vs state-driven (per-side state-signal
   fix). ~10 min on existing data.
4. **TrdImp into the 58-feature pred model.** Re-fit SigLinear with
   TrdImp added; measure ΔR² OOS. Confirms or rules out new alpha source.

### Modest parameter-level wins
5. **Re-climb at `max_pos = 45`.** +$50/44-days from the change alone;
   re-climb may surface more.

### New signal classes (when ready for C++ work)
6. **HL order count plumbing** through BookManager → `SigOrderCountImbalance`.
   "Highest-value HL addition" per the candidate review — data is already
   in the wire payload.
7. **Trade-flow base signals**: `SigCVD`, `SigSweep`, `SigAbsorption`.
   New primitives, not extensions of `SigTrdImpulse`.
8. **Operator signals**: `SigEma`, `SigZScore/SigRmsNorm`, `SigGate`,
   `SigClip/SigShape`, `SigLagDiff`. Pull repeated logic out of base
   signals; enable composable signal graphs.
9. **Cheap remaining ceiling climbs** on unused families: `SigRetEms`,
   `SigLiqBal*`, `SigTrdPx`. Existing `bench_family.py` infrastructure.

### SigDer correctness pass (if revisited)
10. Reset/validity safety, `isSame()` symmetry, zero guards, clocked /
    same-timestamp coalescing, telemetry + scanner. See §VI.

### Ruled out (don't redo)
- Regression-on-market-tape → ordex place_thresh rule.
- Walk-cost-gated place_thresh on HYPE (= `place_thresh_mode_1`).
- Multi-level depth gating beyond TOB on HYPE.
- Tier 1 RMS / Tier 2 tgt-synced SigDer modes for book/trade preds.

---

## IX. Cross-references

| Source                                                                 | Contents |
|------------------------------------------------------------------------|----------|
| `~/scratch/hype_20260407/claude_iter/NOTES.md` (34 KB)                 | Chronological log of execution work |
| `~/scratch/hype_20260407/claude_iter/SUMMARY.md`                       | Pre-consolidation execution summary |
| `~/scratch/hype_20260407/claude_iter/SIGNAL_WORK.md`                   | Per-family ceiling plan |
| `~/scratch/hype_20260407/claude_iter/sig_bench/cmaes/summary.json`     | Per-family CMA-ES ceiling table |
| `~/scratch/hype_20260407/claude_iter/sig_bench/iter_mondo/log.json`    | Mondo F/G tag iteration log |
| `~/scratch/hype_20260407/claude_iter/sig_bench/iter/log.json`          | DeepBook D-tag iteration log |
| `~/scratch/hype_20260407/pso_tuning_notes.txt`                         | PnlClimb optimizer bake-off |
