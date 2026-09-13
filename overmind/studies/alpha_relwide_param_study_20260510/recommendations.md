# Recommendations from the May 2026 alpha_relwide parameter study

This is a meta-reflection on the May 2026 alpha_relwide sweep series — what
actually moved results vs what was noise, what to do differently next time,
and the most important takeaway. The detailed per-symbol study is in
`README.md` alongside this file; the v3 picks story is in
`~/scratch/alpha_relwide_autosearch/full_range_sweep_20260511/SUMMARY.md`.

## What worked

1. **One-decision-at-a-time iteration.** Each narrowing was traceable to
   specific data ("we dropped 0.0008 bp because top-30 healthy showed only
   1 at that value"). When we tried a big black-box improvement pass and
   got a regression on GOLD, debugging was easy because we could roll back
   to the specific knob that broke it.

2. **Concentration over exploration.** Going from 800 / ~1M variants
   (sweep_1) to 800 / ~7k (sweep_a) didn't add new dim values — it just
   reweighted random sampling toward already-good regions. That alone
   produced 80% of the sharpe lift. New dim values only mattered for one
   symbol (GOLD's lower place_thresh).

3. **`symbol_preferences.py` plumbing.** Per-symbol learned ranges live in
   one file, the spec generator consults them automatically with an
   `--no-preferences` opt-out. Future sweeps will pick up where this one
   left off instead of starting from scratch.

4. **Cross-symbol pattern hunting.** Tracking dim choices across all
   symbols revealed real structure (the pred_momentum vs curv_impulse
   camps, GOLD's unique fast-LMR preference, SILVER's mode-2 placement).
   These wouldn't have been visible from per-symbol drilldown alone.

## What hurt us / process changes

5. **`sim_score` ≠ profitable pnl.** Multiple times we almost promoted
   variants that scored high but actually lost money (sweep_1's CL
   "winner" had sim_score 658 with **−$40/d avg_pnl**). The minimum
   selection criterion going forward is:

   ```
   filter: pct_positive >= 0.67 AND avg_pnl > 0 AND num_trds >= 50
   sort:   sharpe descending
   ```

   And always read avg_pnl + sharpe + num_trds together before promoting.
   Sim_score alone is unreliable as a ranking metric.

6. **The sweep's `sharpe` field doesn't match daily-pnl sharpe.** We
   discovered this very late. Sweep-reported sharpes were systematically
   2–14× higher than full-sim daily-pnl sharpe. (Example: COPPER sweep
   reported 5.14; full-sim measured 0.42.) **Action: trace what the sweep
   actually computes** before the next sweep so we know what we're tuning
   against. Likely candidates: per-trade markout, intra-day mark-to-market,
   or a filtered subset of trades. Without knowing, the sweep summary
   tables are dangerous to act on.

7. **11-day windows are too short.** Variance over 11 days is large
   enough that the sweep was partly picking variants on noise. The full-
   range (23-day) re-sweep with identical dim spaces produced clearly
   better picks for every symbol. **Rule: 20–30 day minimum sweep window,
   prefer 30+.** If only 11 days of data exist for a symbol, accept that
   selection is noisy and don't deploy at full size.

8. **Aggressive locking can kill working regions.** GOLD took four sweep
   iterations because we kept locking dims to single values where the
   data was 50/50 or weakly leaning. Specific mistakes:

   - `lmr_local_only=[True]` for GOLD (sweep_1 had 16 True / 14 False,
     basically tied) → lost ~50% of healthy region.
   - `cancel_buffer_frac=[0.25, 0.5, 0.75]` for GOLD when sweep_1's
     strongest value was actually `0.1` (19.7% healthy).
   - `vol_norm_tdc=[10]` for GOLD when `4` was the higher-trade-rate
     option (and SILVER's pairing of `[4, 40]` lost `10` which was its
     strongest value).

   **Rule: only lock a dim to a single value when ≥70% of healthy variants
   concentrate there.** Otherwise keep top 2–3 values.

9. **Always run `md_exists` before launching an OOS sim.** We spent
   significant time debugging "0 trades for 6 symbols" before realizing
   the market data simply wasn't downloaded locally. The sim ran cleanly
   on empty data with no error, producing the wrong conclusion. **Action
   item: add a pre-sim data-availability assertion to all custom sim
   runners** so missing data fails loudly. (`sim_grid` does an
   `md_exists` check via the autosearch flow but custom scripts that call
   `sim_grid` directly skip it.)

10. **The `pktrade` binary has compile-time HyperliquidSec.** When new
    HIP3 symbols are added (e.g., NATGAS) the binary needs to be rebuilt
    or sims fail with "Symbol not found in secmaster". The header at
    `src/pktrade/util/secmaster_lib/HyperliquidSec.h` is the source; rebuild
    via `./jammybuild.sh -c pktrade`. (Already noted in study README, but
    worth repeating since it cost us a full sweep run.)

## Strategy / deployment lessons

11. **Regime fragility is a real per-symbol dimension** we weren't
    explicitly measuring. The proxy that worked: **`#healthy variants` out
    of total swept** with `pct_positive >= 0.67` filter. SP500 had 73/135,
    GOLD had 0/135 in the full-range sweep — same dim space, dramatically
    different fragility. Track this metric routinely.

12. **Trade-rate explosion across regimes is the over-fit smoking gun.**
    Variants that traded ~5/day in-sample and ~200/day OOS were the same
    ones whose sharpe collapsed. **Diagnostic: compare per-variant
    in-sample vs OOS `avg_trades_per_day`. Any variant whose ratio is >5×
    is likely over-fit.** Add this to standard sweep-result tables.

13. **Don't deploy at full size on freshly-discovered picks.** Even with
    the v3 mixed-regime picks, OOS / IS sharpe gaps remain non-trivial
    for some symbols. Start at 0.1–0.5× size; ramp up only after observing
    live data for at least 1–2 weeks.

14. **Some symbols don't have enough trading history yet.** GOLD, SILVER,
    XYZ100 produce 0–2 healthy variants out of 135 over a 23-day window.
    Likely contributing factors: high intra-regime variance, recent HIP3
    listing, model-bundle freshness. Either:

    - Wait for more data and re-sweep,
    - Try a wider dim search (these may be the wrong regime entirely), or
    - Develop regime-conditioning logic (vol gates, time-of-day gates).

    Until then, deploy these at very small size or skip.

15. **`tgt_maxpos_notional` (position-cap ratio) varies dramatically by
    symbol.** SILVER is sharply peaked at 10:1 (anything else loses money).
    SP500 likes 15–20:1. XYZ100 wants 7:1. COPPER doesn't care above 7:1.
    **The ratio is a first-class dim worth sweeping per symbol**, not a
    universal default. Going forward, include it routinely.

## Tooling / process recommendations

16. **Persist sweep results in dated dirs with manifests + summaries.**
    Going back to compare v1/v2/v3 was easy because we kept everything
    versioned and dated. Don't overwrite; write to new dirs and write a
    SUMMARY.md per dir. The summary documents are the most useful
    artifacts when revisiting.

17. **Background bash tasks die at 24–48h boundaries.** Chained-waiter
    bash scripts that sleep-loop got killed multiple times mid-run.
    Workarounds:

    - `sim_grid`'s `resume=True` (already the default) lets you re-launch
      and pick up where you left off.
    - For chained launches, just monitor for the "Top 5 variants for X"
      event and manually launch the next when it fires.
    - Or write the chain as a single bash script that exits when each
      stage completes — but realistically, the harness will still kill it.

18. **Routinely re-sweep against the latest data.** As HIP3 symbols
    accumulate history, picks should improve. Plan a roughly monthly
    re-sweep cadence and a longer window (40+ days) once available.

19. **Per-sweep diagnostics to add:**

    - `#healthy variants` (with the standard filter) per symbol per sweep.
    - Trade-rate distribution per symbol per regime (IS vs OOS portion).
    - Median sharpe within healthy variants (vs just the top).
    - Whether each dim value's per-bucket healthy% trends monotonically
      (cliff detection).

    The first two would have caught the over-fit issues in v2 before we
    deployed anything.

## Single most important takeaway

**Trust longer-window / OOS-aware sharpe over in-sample / short-window
sharpe, always.** The v3 picks are uniformly better than v2 not because we
tested new dim values, but because we judged variants over more data. The
temptation to optimize against a small recent window is strong (recent data
feels most "live"), but it's exactly the wrong instinct. Pick variants that
work across regimes, not variants that happened to work last week.

If you read this study a year from now and only remember one thing, let it
be that.
