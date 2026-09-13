# BOATS vs NQ Overnight A/B Study — Plan

Date: 2026-06-01

A/B-tune RelWideMM2 on the **US Overnight** session twice — once with the
existing setup (NQ via TopBookCme as the rel signal, XNAS.BASIC remote
which goes stale 20:00–04:00 ET) and once with the BOATS variant
(`--equity-variant Boats` swaps the remote feed to a merged XNAS daytime
+ OCEA.MEMOIR overnight stream). Compare the promoted configs and the
Round-C concentration patterns to understand how having live overnight
remote prices changes the tuner's choices.

Companion playbook: `relwide_equities_autosearch_playbook.md`.

---

## 1. Question

When the remote NVDA-like signal is live during 20:00–04:00 ET (via BOATS)
instead of stale (XNAS frozen 8pm–4am ET, NQ × beta filling the gap),
does the autosearch's "best-known" config materially change, and do the
resulting promoted strategies trade better?

Specifically: are the per-symbol winners (place_thresh, cancel_buffer,
signal coefs, ladder shape) the same between arms, or does BOATS produce
a different regime?

## 2. Scope

- **Symbols (7)**: MU, NVDA, EWY, SNDK, CRCL, INTC, TSLA
  — chosen by HL overnight notional in `tools/boats_overnight_volume.py`
  (MU dominates at $109M / 5 nights; EWY is a thinner KR ETF kept for variety).
- **Session**: `usovernight` (18:00 → 09:30 ET).
- **Donor**: `xyz:DKNG` (playbook default). It's US-Day-tuned, but we're
  comparing arms, not searching for absolute optimum — donor bias cancels.
- **Sim window**: 20 trading days = the May 2026 BOATS backfill
  (~2026-05-01 → 2026-05-29). Both arms must use the same 20 days for
  cross-arm comparability.
- **Workdirs**:
  - `~/scratch/relwide_retrains/20260601_overnight_ab/nq/`
  - `~/scratch/relwide_retrains/20260601_overnight_ab/boats/`

## 3. Phases (execute one at a time, pause for review)

| Phase | What | Per arm (7 sym) | Stop-and-review on |
|---|---|---|---|
| 0 | `init-spec` (both arms) | < 1 min | spec files written; sim_days/extra_args correct |
| 1 | `scan-thresh` (both arms, parallel) | ~est. several h | per-symbol thresh/PnL/sharpe regimes; do arms differ already? |
| 2 | Round A — placement (`spec_round_a.py`, hand-written per playbook) | ~30 min/arm | per-symbol locks (cancel_buffer, ladder_one_sided); cross-arm diffs |
| 3 | Round B — signal sweep | ~1.5 h/arm | which signal dims lock per arm; do BOATS/NQ pick different recipes? |
| 4 | Round C — broad 1000-variant random sample | ~14 h/arm | healthy-variant count per arm per symbol; ≥70% locks |
| 5 | Promote + cross-arm diff | ~30 min | side-by-side promoted configs; per-symbol PnL/sharpe deltas; concentration deltas |

Wall time for the full pipeline is roughly **1–1.5 days** if Round C runs
unattended. We'll quote concrete estimates as each phase actually starts.

Decision rule at each pause: if the arms produce *identical* winners and
identical concentrations, the rest of the pipeline is unlikely to surface
a difference — we stop early and report "no material effect". If even
Phase 1 shows clear divergence (e.g. BOATS scan-thresh winners are 2–3×
different bp values than NQ), we continue.

## 4. Execution constraints (carry over from playbook)

- Both arms run on the **same set of dates**. `dates_avail` filters to
  what's locally present; we'll verify both arms get the same 20 dates
  before each phase. If one arm picks fewer dates than the other,
  hand-pin the date list.
- `md_exists` pre-fetch runs at scan-thresh + run-spec entry. For the
  boats arm the tool now points it at `TopBookEquity_Boats/`.
- Donor / session / spec are written once per arm at `init-spec`.
  Hand-edited `spec_round_*.py` files are the same shape per arm and
  get copied between workdirs (only the `extra_args` line differs).
- We deliberately do NOT change `relative_beta`. The strategy is the
  same; only the remote data feed differs. If results suggest beta is
  wrong under BOATS, that's a *finding*, not a parameter to tune in this
  study.

## 5. Output / what we want to know at the end

For each symbol, side-by-side:
- Promoted variant config: are key knobs the same?
  (place_thresh, cancel_buffer, per_order_widen_frac, pred_momentum_*,
  premium_tdc_s, ladder_one_sided, vol_norm_*)
- Promoted metrics: sharpe, avg_pnl, pct_positive, num_trds, fillrate.
- Round C concentration: which dims locked under NQ vs BOATS?
- Healthy-variant count (regime-fragility proxy). If BOATS halves the
  count for any symbol, that's a strong signal the regime shifted.

Plus one global question: **does BOATS net win, lose, or wash** across
the 7 symbols once both arms are properly tuned to their respective data
regime? (The fixed-config A/B we already ran said BOATS lost −43% — but
that was without re-tuning, so this study is the fair comparison.)

## 6. Logbook

Filled in per phase as we run.

### Phase 0 — init-spec (pending)
### Phase 1 — scan-thresh (pending)
### Phase 2 — Round A (pending)
### Phase 3 — Round B (pending)
### Phase 4 — Round C (pending)
### Phase 5 — Promote + diff (pending)
