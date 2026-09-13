# Equities Autosearch Lessons — May 2026

Date: 2026-05-10

This is a working reference for the May 2026 round of `alpha_relwide_autosearch` on US equities. Captures the scoring/dim/workflow lessons from the sweeps that produced `equities_golive_gf{1,2}_20260503.json`. Most of these have been baked into code defaults; this doc explains the "why" and the gotchas.

Sweep workdirs (under `~/scratch/alpha_relwide_autosearch/`):
- `equities_full_0501/` — Group A (NVDA/TSLA/GOOGL/AMZN/PLTR/RIVN/BABA), 800 variants/sym, full dim sweep with `cancel_buffer_frac` added.
- `equities_cancel_0501/` — focused cancel-knob sweep on Group A (10 variants/sym, conditioned on prior winner).
- `equities_others_0502/` — 19 non-Group-A symbols, 800 variants/sym, sim window pinned to 4/13–4/24 via `--end-date 2026-04-25` (recent HL captures gappy on S3).

Final golive configs:
- `~/scratch/alpha_relwide_autosearch/equities_golive_gf1_20260503.json` — 7 pktraders for `gf1`
- `~/scratch/alpha_relwide_autosearch/equities_golive_gf2_20260503.json` — 19 pktraders for `gf2`

## 1. Fillrate-aware scoring

**Problem:** Pre-2026-04-30 the autosearch's sim_score (`wins_pnl_charminvol_pct`) had no fillrate term. Top variants routinely had fillrates of 0.05–1% — i.e. the strategy was getting "cheap" sim PnL by canceling >99% of orders. Live exchanges fill differently than sim assumes for cancels-in-flight, so winners were over-fit to a regime that won't reproduce live.

**Fix:** Added `fillrate_target` to `scoring_spec` (default `0.005` = 50bps) in `SimVariations.py`. When set and base score > 0, score is multiplied by `min(avg_fillrate / fillrate_target, 1.0)`. Variants with fillrate at-or-above target get full credit; below scales linearly.

Code: `overmind/strat_main/stratbuilder/PnlClimb.py:506-516` (the wrapping `score_stats` that calls `_score_stats_base`).

The `fillrate` column (4 decimals) is now in `results.txt`, `grid_results.csv`, and the per-iter climb log so it can be reviewed without re-deriving it.

## 2. vol_limit lowered 100 → 50

The same scoring formula uses a volume modifier `vol_mod = min(stats[vol_style] / vol_limit, 1.0)`. With `vol_limit=100` and `vol_style="avg_med_numtrds"`, a variant with 20 trades/day got `vol_mod=0.20` regardless of PnL/sharpe — an 80% haircut just from being below an arbitrary trade-count target.

This was over-aggressive for symbols with sparser-but-high-quality trading. ORCL was the cleanest example: a $96 PnL / sharpe 0.97 / 20-trade variant was scoring 14.6, while a $-4 PnL / sharpe -0.01 / 66-trade variant scored 137 — purely because the 66-trade variant got `vol_mod=0.66` vs `0.20`.

**Fix:** Default `vol_limit` lowered to **50** in `SimVariations.py:254`. After re-ranking the May sweeps under this, 5/16 "others" symbols and 4/6 Group A symbols had their top variant flip — most flips were clean wins (better PnL+sharpe at slightly lower trade count), confirming the volume bonus had been over-weighted.

## 3. Score-PnL inversion is real; cross-check raw stats

Multiplicative scoring (`wins_pnl × vol_mod × pct_adj × fillrate_mult`) can produce inversions where a higher-score variant has *worse* underlying PnL. Each multiplicative term can dominate in isolation. Examples seen:

- **RIVN** (cancel-knob sweep): cbf=4.0 winner had $27 PnL with 28.67% fillrate; outscored cbf=0.25's $221 PnL because both `vol_mod` (91 trades vs 34) and `fillrate_mult` (1.0 vs 0.6) compounded.
- **ORCL** (cancel sweep): –$4 PnL variant outscored +$144 PnL variant.
- **PLTR** earlier: pct_positive=0.67 kept negative-PnL variants positive on score.

**Mitigation:** As of 2026-05-05 the autosearch summary.txt automatically prints **Top N by sim_score** AND **Top N by avg_pnl** plus a sparse-trading warning. The cross-check is now part of the default output. Code: `alpha_relwide_autosearch.py:_top_variants` and the summary block in `run_spec`. Still the user's job to eyeball PnL/sharpe/pct_pos for the score winner.

## 4. cancel_buffer absolute overrides cancel_buffer_frac

In `src/pktrade/ordex/alpha_rel_wide_mm.cc`:

```cpp
cancel_buffer_ = ordex_conf["cancel_buffer_frac"].GetDouble() * base_place_thresh_conf_;
if (ordex_conf.HasMember("cancel_buffer")) {
    cancel_buffer_ = ordex_conf["cancel_buffer"].GetDouble();
}
```

If both fields exist, the absolute `cancel_buffer` wins. The `AlphaRelWideMM.json` template historically set both (`cancel_buffer_frac=0.25, cancel_buffer=5e-05`), so any sweep over `cancel_buffer_frac` produced *identical* sim outputs across all values.

Caught us on the first cancel-knob sweep — five different cbf values gave identical PnL/sharpe/fillrate. Fix: removed the absolute line from the template (`AlphaRelWideMM.json:31` area). Older base configs still in scratch dirs need the line stripped before re-use:

```python
ordex.pop('cancel_buffer', None)
```

This same gotcha will apply to any future "fractional" + "absolute" pair.

## 5. cancel_buffer_frac is a major fillrate lever

Once we got cbf actually exercising, it dominated the fillrate dimension:

| Symbol | cbf 0.25 → 4.0 fillrate movement |
|---|---|
| AMZN | 0.35% → 8.05% (23×) |
| BABA | 0.74% → 11.5% (15×) |
| NVDA | 0.28% → 24.6% (88×, blew up PnL) |
| TSLA | 0.36% → 14.3% (40×) |

At cbf=4.0 most symbols overtraded into negative PnL. cbf=2.0 was the sweet spot for many: meaningful fillrate gain with stable-or-better PnL. Final picks across the 26 symbols: cbf=2.0 won 7/26, cbf=4.0 won 8/26, cbf=1.0 won 2/26, cbf=0.5 won 1/26, cbf=0.25 won 1/26. Combined high-cbf (≥2): 15/19 of the others; 10/13 in the analyzed Group A subset.

**`will_cancel_isolated`** had smaller and inconsistent effect. `will_cancel_isolated=False` is now baked as `DEFAULT_FIXED` based on this round but was not added as a sweep dim in the canonical `cancel` preset (only `cancel_buffer_frac`).

## 6. Cross-symbol dim winners (top-100 per symbol pool, n=1300)

Strong positive signal:
- **`ladder_one_sided=True`** — mean score 30.4 (T) vs 22.7 (F) — strongest boolean signal.
- **`per_backlevel_rung_spacing_mult=2.0`** — modestly beats 1.5 (29.1 vs 24.7).
- **`pred_momentum_tdc=30`** — clear winner over 1/5/15 (mean score 34 vs 23-27).
- **`pred_momentum_coef`** — bimodal: 0 or 2-3 win; mid values (0.5, 1.0) underperform.
- **`vol_widen_coef`** — low values (0.07-0.18) win; 0.33 worst.

Weak / no signal:
- `lmr_time_const_s [4 vs 60]` — essentially tied.
- `curv_impulse_tdc [5 vs 40]` — tied.
- `lmr_local_only [T vs F]` — barely matters.
- `curv_impulse_coef` — all 4 values comparable.

Acted on these by:
- Trimming `lmr_time_const_s` to `[4, 60]` in the `core` preset.
- Trimming `curv_impulse_tdc` to `[5, 40]` in `momentum`.
- Adding `lmr_ema_mult=1.0` and `adding_no_cutin=True` to `DEFAULT_FIXED` (per existing memory; these are stable findings).

## 7. Sparse-trading symbols (LLY, COST)

Some symbols are inherently low-trade-rate due to wide auto-generated `place_thresh` ranges (LLY: 16-82 bps, vs NVDA at 1-7 bps). LLY peaked at 23 trades over 10 days (median 4); COST at 15 trades (median 3). Best PnL ceilings: LLY ~$70/day, COST ~$22/day.

The summary.txt now prints a `WARN: sparse trading` line when best variant has <20 trades or median <5. For these symbols:
- Treat sample as fragile — 5-10 trades over 10 days is not a robust regime.
- Operational overhead may exceed expected gain. Consider whether to deploy.
- Potential follow-up: a separate sweep with much narrower place_thresh (1-5 bps) might find more-frequent regimes — not yet done.

## 8. Date-pinning (`--end-date`)

When recent histdata is gappy on S3 (24 of 26 May equities were missing 2-5 most-recent days from the capture pipeline), use `--end-date YYYY-MM-DD` on `run-spec` and `generate-spec` to pin `today()`. The autosearch's date math (`_recent_trade_dates`, `_maybe_check_market_data`, the run_spec date block) all consult `_today()` which respects the override.

Used in `equities_others_0502/` to pin to `2026-04-25` so `sim_days=10` landed on `4/13–4/24` where all 19 symbols had full HL coverage. Previously this required a wrapper script (`run_with_date.py`) that monkey-patched `date.today()`; the flag obsoletes that.

## 9. Default change: tgt_order/maxpos notional

`AlphaRelWideMM.json` defaults changed from `tgt_order_notional=4000, tgt_maxpos_notional=40000` to `1000/10000` (still 1:10 ratio). Live golive configs were updated separately on the machines.

## 10. Final picks (where to find them)

| group | variants | source workdir | golive path |
|---|---|---|---|
| gf1 (Group A, 7) | TSLA: 59271704, NVDA: 1821098953, PLTR: 1722786833, GOOGL: 1726812765, AMZN: cancel-knob var 8 (cbf=2.0/F on prior 0430 winner), RIVN: 1453201078, BABA: 1404403443 | `equities_full_0501/`, `equities_cancel_0501/` | `~/scratch/alpha_relwide_autosearch/equities_golive_gf1_20260503.json` |
| gf2 (others, 19) | per `build_combined_golive_0503.py` | `equities_others_0502/` (incl. hand-picks for ORCL/LLY/COST) | `~/scratch/alpha_relwide_autosearch/equities_golive_gf2_20260503.json` |

Hand-picks worth noting (didn't take the sim_score winner):
- **ORCL** → variant 507621088: $144 PnL, sharpe 0.44, pct_pos 0.90, 35 trades. The default winner had a –$4 PnL inversion artifact.
- **LLY** → variant 1644616922: $69 PnL, sharpe 1.17, pct_pos 0.90, 9 trades. Sparse but the strongest among LLY's options.
- **COST** → variant 1349993687: $19 PnL, 4 trades. Marginal — included for completeness but watch closely live.
- **RIVN** → variant 1453201078: $146 PnL, sharpe 0.71, fillrate 10.88%. The 28.67%-fillrate sim_score winner was an inversion artifact.

## Open follow-ups

- Re-sweep LLY/COST/AAPL with much tighter `place_thresh` ranges (1-5 bps) to test whether higher-trade-count regimes exist.
- Decide whether `vol_limit=50` is still too aggressive for the sparsest symbols (could go to 30).
- Consider a hard-floor variant of the fillrate score (penalty if below floor, no bonus above) instead of the current soft multiplier — would prevent the RIVN-style "high-fillrate-low-PnL" inversion.
- Validate live performance against sim PnL ranges over 2-4 weeks; revisit fillrate_target if live fillrates diverge meaningfully from sim.
