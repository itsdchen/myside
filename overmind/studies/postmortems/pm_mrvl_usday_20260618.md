# Postmortem: MRVL on combined_equities_bfx3/usday

- **Case ID:** `mrvl_usday_20260618`
- **Author date:** 2026-06-18
- **Strat path:** `gf3/combined_equities_bfx3/usday`
- **Symbol:** `xyz:MRVL`
- **Verdict:** mixed (HL micro-structure regime change driven by underlying news event; pending sim-eval to confirm)

> **CORRECTION 2026-07-15**: HL micro-structure ratios below used the
> original `pm_hl_stats.py` which aggregated across calendar days including
> weekends. HL trades 24/7 so weekend flow biased per-day averages. Re-run
> with `--weekdays-only`: HL notional/day **26×** (not 1,045×), num_trades
> 4.2× (not 74×), n_midchanges/day 2.7× (not 34.5×), spread 0.27× (not
> 0.13×), **med_inside_liq 1.95× DEEPER** (not 0.69× thinner — direction
> flipped), avg_mid +52%. The overall verdict ("MRVL got discovered on
> HL, edge vanished") **stands** — MRVL clearly went from a thin market
> to a much busier one — but the specific magnitudes in this document
> are inflated by weekend contamination and inside-liq direction is
> reversed. Do not cite the raw ratios from the tables below.

## Summary

MRVL traded well on usday from 05-11 to 05-28 (+$3,985 cum, $293/day avg over 12 days).
Starting 05-29 the strat turned volatile and net-negative through 06-11; on 06-15 daily
activity collapsed (likely manual size cut). The dominant change is in the HL micro-structure
of MRVL itself: HL daily notional went from ~$50k/day to ~$52M/day (~1,000×), spread
compressed 8× (31 bps → 4 bps), and trades went up 74×. This happened together with a
~80% rally and ~1.65× spike in topbook realized vol — consistent with a news/regime event
on MRVL. Strategy was edge-mining a thin/quiet HL market; once liquidity arrived, the edge
went with it.

## Windows

- **Baseline (good):** 20260511 – 20260528 — 12 trading days, +$3,985 cum, $293/day avg
- **Bad (volatile leg):** 20260603 – 20260611 — 7 trading days, –$1,602 cum, –$229/day avg
- **Collapsed activity:** 20260615 – 20260617 — 3 days, ~$20/day, ttr ≈ 41 (looks like a manual size cut)
- **Inflection:** 20260529 (first negative day post-streak); 20260602 (massive trade-count jump from <100/day to 693)

## Baseline vs Bad — account metrics

Per-day averages over each window.

| metric | baseline | bad | ratio |
|---|---:|---:|---:|
| net_pnl/day            |   +293.07 |  –228.84 | — |
| closed_pnl/day         |  see history.csv | see history.csv | — |
| commission/day         |  see history.csv | see history.csv | — |
| times_traded/day       |       192 |      562 | 2.93× |
| times_flipped/day      |        16 |      137 | **8.50×** |
| shs_traded/day         |     1,460 |   10,194 | 6.98× |
| open_pos (eod, typical)|     small |    small | — |
| min_pnl_seen (worst)   |     –275  |  –1,574 | 5.72× |

Flips per day going 16 → 137 is the strongest acct-level tell: the strat is being whipsawed
in/out of position. Trade count also tripled. Shares traded up 7×.

## HL market stats (symstats + returner, 5-min returns)

| metric | baseline | bad | ratio |
|---|---:|---:|---:|
| HL spread_bps_avg          |     30.91 |        3.86 | **0.125×** |
| HL avg_mid                 |    165.76 |      302.35 | 1.82× |
| HL num_trades              |     2,822 |     207,645 | **73.6×** |
| HL vol_notional ($)        |   896,322 | 468,528,958 | **522.7×** |
| HL vol_notional / day ($)  |    49,796 |  52,058,773 | **1,045×** |
| HL med_inside_liq          |     30.71 |       21.30 | 0.69× |
| HL n_midchanges / day      |       728 |      25,067 | **34.5×** |
| HL 5-min log-ret stdev (bps) |  36.74 |       56.64 | 1.54× |
| HL annualized realized vol |      1.19 |        1.84 | 1.54× |

HL contract underwent a regime change. Volume scaled 1,000×, spread compressed 8×,
mid-changes 34× more frequent. Inside size got slightly thinner. Mid is up 82% — price doubled.

## Topbook (real-world MRVL) stats (Databento XNAS.BASIC, 5-min returns)

| metric | baseline | bad | ratio |
|---|---:|---:|---:|
| spread_bps_avg              |     6.92 |     7.38 | 1.07× |
| shs / day                   | 18.1 M  | 50.5 M  | 2.79× |
| notional / day ($)          |   3.4 B |  14.4 B | 4.24× |
| 5-min log-ret stdev (bps)   |    48.67 |    80.36 | 1.65× |
| annualized realized vol     |     0.68 |     1.13 | 1.65× |

Underlying MRVL had a clear regime event: ~80% rally, 4× notional, 1.65× realized vol.
Spread on the real-world book essentially unchanged because MRVL is liquid in both periods.

## What changed (side-by-side)

- **Underlying (topbook)**: big move + vol expansion + 4× volume. Normal "MRVL has news" pattern.
- **HL contract**: discovered. Went from ~0% to a meaningful % of topbook notional
  (rough: baseline 50k/day vs topbook 3.4B/day = 0.0015%; bad 52M/day vs 14.4B/day = 0.36%; ~240× share gain).
  Spread collapsed; competition arrived; mid-change frequency multiplied.
- **Strategy**: was mining edge in a thin HL market; the conditions that produced that edge
  no longer hold (no spread to capture, intense competition, faster mid moves driving flips).
- **Manual intervention**: 06-15 onwards activity collapses 14× to ~41 trades/day — looks
  like sizing was cut hard (size_mult or risk_level change). Confirm via `log-events`.

## Verdict & evidence

**Verdict:** mixed — primarily HL micro-structure regime change driven by an underlying news event.

Evidence:
1. HL spread compression 8× and HL volume 1,000× (the conditions that produced edge are gone).
2. Topbook side also shifted (notional 4×, vol 1.65×) — this is a real underlying event, not just HL noise.
3. Account-level flips 8.5× per day — the strat is being whipsawed by the new HL micro-structure.

Ruled out:
- `data` — no symbolizer/contract mismatch (HL spread/vol patterns match topbook directionally).
- `signal_decay` (pure) — needs sim-eval; if sim PnL also degraded in lockstep, would shift verdict toward signal_decay. Currently HL micro-structure looks like the larger story.
- `execution` (pure) — fill quality not yet inspected (markouts/fillstats), but the change in HL
  volume scale alone is sufficient to break a thin-market edge strat.
- `config_change` — no evidence of a sizing/param change driving entry into the bad period;
  the 06-15 cut is downstream of (not cause of) the bad regime.

## Pending follow-ups (to lock the verdict)

- `sim-eval` over `20260511:20260528` vs `20260603:20260611` to see if sim mirrors the live
  degradation. Sim same → signal/regime. Sim still good → execution / HL feed quality.
- `markout`, `tod`, `fillstats` on the bad window to look at fill quality.
- `log-events --types size_mult,risk_level` to confirm the 06-15 manual cut and date its application.

## Actions

- Immediate: looks already cut; confirm.
- Recalibrate the strat's expected spread / inside-liq for MRVL — the new regime is fundamentally
  different. The strat may simply not have edge in the new regime; alternatively, retune to size
  for thinner edge per fill.

## Commands used

```bash
# Per-symbol PnL history
python ~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/pm_history.py \
    --strat-path gf3/combined_equities_bfx3/usday --symbol xyz:MRVL \
    --out ~/scratch/postmortems/mrvl_usday_20260618/history.csv \
    --print-curve --summary

# HL stats (symstats + returner-derived 5-min vol)
python ~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/pm_hl_stats.py \
    --symbol xyz:MRVL --baseline 20260511:20260528 --bad 20260603:20260611

# Topbook stats (Databento XNAS.BASIC ohlcv-1m + cbbo-1s)
python ~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/pm_topbook_stats.py \
    --symbol MRVL --baseline 20260511:20260528 --bad 20260603:20260611
```

## Artifacts

- `~/scratch/postmortems/mrvl_usday_20260618/history.csv`
- `~/scratch/postmortems/_returner_cache/xyz_MRVL_*.csv` (per-day 1-min HL mids)
- `~/scratch/postmortems/_topbook_cache/MRVL_XNAS.BASIC_*.json` (per-window topbook stats)
