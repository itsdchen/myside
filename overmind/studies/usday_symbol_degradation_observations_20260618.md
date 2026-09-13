# usday Symbol Degradation — Cross-Case Observations (2026-06-18)

> **CORRECTION 2026-07-15**: The specific HL micro-structure magnitudes
> cited below (MRVL 1,045× notional, 34× mid_changes; EWY 3.6× notional,
> 4× mid_changes) came from the original `pm_hl_stats.py` which included
> weekend HL activity in calendar-day averages. Re-running with the new
> `--weekdays-only` flag shows the true magnitudes are much smaller —
> and in the EWY case the direction of "notional/day" flips (weekday-only
> shows notional/day *decreased* 0.81×, not increased 3.6×). The
> qualitative "MRVL got discovered / EWY had HL-only participant change"
> readings remain, but the paired postmortems have been marked with
> per-file correction notices. See:
> - `postmortems/pm_mrvl_usday_20260618.md`
> - `postmortems/pm_ewy_usday_cov_0313_20260618.md`
> - `symbol_regime_log.md` (for updated per-symbol reads)



Initial findings from two postmortems written today on usday strats where a single symbol
went from consistently positive to consistently negative PnL:

- `pm_mrvl_usday_20260618.md` — MRVL on `gf3/combined_equities_bfx3/usday`
- `pm_ewy_usday_cov_0313_20260618.md` — EWY on `gf2/combined_equities_bfx2/usday_cov_0313`

Both cases are about a thin/edge-mining HL contract that stopped working. The headline
result is that the **two cases broke for opposite reasons**, even though the strat-side
account metrics tell a similar "consistently good → consistently bad" story.

## TL;DR

| dimension | MRVL (good→bad) | EWY (good→bad) |
|---|---|---|
| Topbook spread          | unchanged (~7 bps) | unchanged (~4 bps) |
| Topbook notional/day    | 4.2× (3.4B → 14.4B) | 0.83× (1.63B → 1.36B) |
| Topbook 5-min vol       | 1.65× (49 → 80 bps) | unchanged (~33 bps) |
| HL spread               | **0.13× (31 → 4 bps)** | unchanged (~3.6 bps) |
| HL notional/day         | **1,045× (50k → 52M)** | 3.6× (2.1M → 7.4M) |
| HL inside-liq           | 0.69× | **0.30× (34 → 10)** |
| HL mid-changes/day      | 34.5× | 4× |
| HL 5-min vol            | 1.54× | 0.85× |
| Strat times_traded/day  | 2.9× | 1.03× (unchanged) |
| Strat times_flipped/day | **8.5×** | 0× (was already small) |

MRVL = underlying news/regime event reshaped the HL contract end-to-end. EWY = HL-only
micro-structure change with the underlying entirely quiet.

## Methodology used

For each case, three datasets per window (baseline vs bad):

1. **Strat-side per-day metrics** from local `acct_*.csv` (net_pnl, fills, flips, shares, etc.)
   — extracted with `tools/trademan/postmortems/pm_history.py`.
2. **HL market stats** — spread / volume / inside liq / mid-changes / 5-min realized vol —
   from the `symstats` binary (range mode) + the `returner` binary (1-min HL mids, resampled
   to 5-min log returns). Wrapped in `tools/trademan/postmortems/pm_hl_stats.py`.
3. **Topbook stats** — spread / volume / 5-min realized vol — from Databento
   (`ohlcv-1m` for vol+returns, `cbbo-1s` for spread, dataset `XNAS.BASIC`). Wrapped in
   `tools/trademan/postmortems/pm_topbook_stats.py`.

5-minute log-return stdev is the common intraday vol metric on both sides — coarse enough
that the HL-side 1-min `returner` data resamples cleanly, fine enough to detect intraday
regime changes.

This three-source pattern (strat-side, HL micro-structure, topbook micro-structure) cleanly
separates three kinds of causes:

| pattern | likely cause |
|---|---|
| Topbook moves + HL moves                  | underlying news/regime event |
| Topbook flat + HL moves                   | HL participant / liquidity change |
| Topbook moves + HL flat                   | HL feed / connectivity issue |
| All three flat, strat-side metrics change | config / sizing / signal-side change |

(MRVL = row 1. EWY = row 2.)

## Pattern A — Underlying-driven HL regime change (MRVL)

A liquid US equity has a news/regime event (here: ~80% rally, 1.65× 5-min vol, 4× topbook
notional). The HL contract on that name, previously thin and edge-rich, gets *discovered*:
volume jumps 1,000×, spread compresses 8×, mid-changes 34×.

Strategy effect:
- The conditions that produced edge no longer hold. Spread to capture went from ~31 bps
  to ~4 bps. Competition arrived.
- Account-side: `times_flipped/day` 8.5× and `times_traded/day` 3× — strat is being
  whipsawed by the new mid-change rate.
- Final state: manual size cut (06-15+ activity collapses ~14×).

The HL contract's share of the underlying went from ~0.0015% of topbook notional to ~0.36%
— roughly 240× share gain. Strategy was implicitly betting that share stayed tiny.

## Pattern B — HL-only micro-structure shift (EWY)

The underlying is quiet — spread, volume, and 5-min vol all unchanged on Databento. On HL:
inside book thins 3.4×, mid-changes 4× more frequent, num_trades 2×, notional/day 3.6×.

Strategy effect:
- Account-side activity is essentially unchanged (same number of trades, same shares, even
  fewer flips). The strat is doing exactly what it did before.
- What changed is the *response*: with thinner inside, the same posting style gets adversely
  selected faster.
- Same drawdown profile shows up on `usday_cov_0330` for EWY — confirms this is symbol-level
  (HL participant or behavior change), not specific to the cov variant.

This pattern is consistent with a new aggressive HL participant arriving (or an existing one
changing style) on EWY. We don't have a confirmed way to detect this currently.

## Diagnostic dimensions that matter

After running two cases, the metrics that pulled the most weight in distinguishing causes:

- **HL spread (bps)** — the most direct edge metric. 8× compression on MRVL was the
  loudest signal.
- **HL notional/day** — 1,000× tells you the contract was discovered.
- **HL inside_liq** — 3.4× thinning on EWY was the loudest signal when spread didn't move.
- **HL n_midchanges/day** — high multiples (4–34×) are a strong tell for "harder market"
  even when realized vol looks normal.
- **Topbook 5-min vol + notional** — disambiguates "underlying caused it" vs "only HL changed".
- **Strat-side times_flipped/day** — whipsaw indicator; MRVL went 8.5×, EWY went to zero
  (no flips because we never reversed; we just bled).
- **Strat-side times_traded/day** — moves with the underlying activity; doesn't move much
  in the EWY/Pattern-B case.

Less useful in these two cases (still worth tracking):
- avg_mid trend — directionally useful but only when symbol moved (MRVL).
- HL 5-min realized vol — moved with the underlying (MRVL) but didn't move on EWY.

## Takeaways for usday monitoring

1. **A symbol going from good to bad with unchanged topbook is the EWY pattern** — likely
   HL participant change. The strat's own trade count won't move much; the inside book
   thinning will. Watch `med_inside_liq` and `n_midchanges_per_day` as leading indicators.
2. **A symbol going from good to bad with a big topbook event is the MRVL pattern** —
   underlying news/regime event reshapes the HL contract. Watch for HL volume scaling up
   alongside topbook volume; the spread will compress and edge will disappear.
3. **`times_flipped/day` is a useful whipsaw indicator**, but it only tells you about the
   MRVL pattern. The EWY pattern shows up in the response (PnL) without showing up in our
   activity.
4. **Thin-market edge strats are inherently fragile to liquidity arrival.** Both cases are
   variants of "the conditions that produced edge changed." That's worth a follow-on study:
   what's the conditional PnL of usday on these symbols when sized as a function of HL spread
   or HL notional/day?

## Open questions

- Are there other usday (or `usday_cov_*`) symbols currently in Pattern A or Pattern B that
  we haven't noticed? An automated daily sweep of the diagnostic dimensions against PnL
  inflection points could pre-flag them.
- Can we detect HL participant arrivals/changes directly (rather than inferring from inside
  book thinning)?
- For Pattern A: at what point in the HL-volume scaling does the edge disappear? Is there
  a tradable signal "HL share of topbook notional > X% → cut size"?

## Pending work to lock the two case verdicts

Both postmortems currently mark verdict as **mixed** pending:

- `sim-eval` over both windows for each case (would distinguish execution vs signal/regime).
- `markout` / `fillstats` to check fill quality directly on the bad windows.
- `log-events` to confirm/date the MRVL 06-15 size cut.

## Pointers

- Postmortems index: `studies/postmortems/README.md`
- Workflow: `studies/postmortems/_workflow.md`
- Template: `studies/postmortems/_template.md`
- Scripts: `tools/trademan/postmortems/{pm_history,pm_hl_stats,pm_topbook_stats}.py`
- Binaries used: `bin/symstats`, `bin/returner` (built today)
