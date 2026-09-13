# Postmortem: EWY on combined_equities_bfx2/usday_cov_0313

- **Case ID:** `ewy_usday_cov_0313_20260618`
- **Author date:** 2026-06-18
- **Strat path:** `gf2/combined_equities_bfx2/usday_cov_0313`
- **Symbol:** `xyz:EWY`
- **Verdict:** mixed (HL-specific micro-structure change — competition + thinner inside book — with no underlying regime event); pending sim-eval

> **CORRECTION 2026-07-15**: HL micro-structure ratios below used the
> original `pm_hl_stats.py` which aggregated across calendar days including
> weekends. Re-run with `--weekdays-only`:
>
> - HL notional/day **0.81× LOWER** (this doc said 3.6× HIGHER — flipped)
> - HL num_trades **0.39× (61% FEWER)** (this doc said 2.08× more — flipped)
> - HL n_midchanges/day **1.02× (flat)** (this doc said 4× — very different)
> - HL med_inside_liq **0.70× thinner** (this doc said 0.30× / 3.4× thinner — direction confirmed but magnitude less extreme)
> - HL spread 0.87× (this doc said flat — small directional shift)
>
> The corrected story is: **HL activity dropped ~60% with inside thinning
> ~30%**, not the "new aggressive participant with 3.6× notional" story.
> The verdict of "HL-specific micro-structure change (topbook was flat)"
> **stands** — but the character of the change is closer to "HL participants
> withdrew" than "new aggressive participant arrived." Do not cite the raw
> ratios from the tables below.

## Summary

EWY traded well on `usday_cov_0313` during 05-28 to 06-10 (+$293/day avg, cum +$3,937 over the leg).
From 06-11 to 06-15 it lost $871/day on average (–$2,984 in 4 trading days). On the topbook,
EWY was *unchanged*: volume, spread, and 5-min realized vol all flat. On HL, the inside book
thinned ~3.4×, mid-changes rose 4× per day, num_trades doubled, and notional/day rose 3.6×.
This looks like a new aggressive participant arriving on HL EWY (or a change in their style)
rather than any real-world news. Same Jun-11 turn shows up on `usday_cov_0330`, confirming
this is an EWY-level (not strat-specific) issue.

## Windows

- **Baseline (good):** 20260528 – 20260610 — 10 trading days, +$2,929 cum, +$293/day
- **Bad:** 20260611 – 20260616 — 4 trading days, –$3,228 cum, –$807/day
- **Inflection:** 20260611 (first –$1.9k day; cumulative drops from peak $8,190 to $5,206 by 06-15)

## Baseline vs Bad — account metrics

Per-day averages.

| metric | baseline | bad | ratio |
|---|---:|---:|---:|
| net_pnl/day            |    +293 |    –807 | — |
| times_traded/day       |     105 |     108 | 1.03× |
| times_flipped/day      |       5 |       0 | 0.0× (no flips at all) |
| shs_traded/day         |   2,540 |   2,489 | 0.98× |
| min_pnl_seen (worst)   |    –696 |  –1,909 | 2.74× |

Notably: trade count, share volume, and flips per day on the *account side* are largely
unchanged. The strat is doing what it did before; what changed is the market response.

Cross-check on `usday_cov_0330` shows the same Jun-11 → Jun-16 drawdown (–$847, –$557,
+$22, –$244 → cum –$1,626), confirming the issue is EWY-level rather than tied to this
particular cov variant.

## HL market stats (symstats + returner, 5-min returns)

| metric | baseline | bad | ratio |
|---|---:|---:|---:|
| HL spread_bps_avg          |     3.70 |       3.55 | 0.96× |
| HL avg_mid                 |   203.68 |     194.69 | 0.96× |
| HL num_trades              |   23,103 |     48,037 | **2.08×** |
| HL vol_notional / day ($)  | 2.07 M  |     7.41 M | **3.57×** |
| HL med_inside_liq          |    33.94 |      10.02 | **0.295×** |
| HL n_midchanges / day      |    4,766 |     19,088 | **4.00×** |
| HL 5-min log-ret stdev (bps) |   25.07 |       21.19 | 0.85× |
| HL annualized realized vol |     0.81 |       0.69 | 0.85× |

HL-level changes: more trades, much more frequent mid changes, dramatically thinner inside
book (3.4× thinner). Spread and avg mid basically unchanged.

## Topbook (real-world EWY) stats (Databento XNAS.BASIC, 5-min returns)

| metric | baseline | bad | ratio |
|---|---:|---:|---:|
| spread_bps_avg              |     4.22 |     4.08 | 0.97× |
| shs / day                   |  8.39 M  |  6.79 M  | 0.81× |
| notional / day ($)          |   1.63 B |   1.36 B | 0.83× |
| 5-min log-ret stdev (bps)   |    32.82 |    32.74 | 1.00× |
| annualized realized vol     |     0.46 |     0.46 | 1.00× |

Topbook EWY is *flat*. No underlying news, no vol expansion. This isolates the cause to HL.

## What changed (side-by-side)

- **Underlying (topbook)**: unchanged. EWY is quiet, low-vol, normal volume.
- **HL contract**: more frequent mid moves (4×), more trades (2×), and a much thinner inside
  book (one-third of baseline depth). Notional/day jumped 3.6×. Spread didn't change because
  the inside got thinner but kept the same width.
- **Strategy**: account-level activity ≈ unchanged; PnL collapsed. With thinner inside, the
  same posting style gets adversely selected faster — fills happen, but the post-fill move
  is worse because the next mid update is more likely. Net result: more pain per trade
  without more volume on our side.

## Verdict & evidence

**Verdict:** mixed — primarily an HL-side micro-structure change (likely new aggressive
participant in HL EWY) with no real-world catalyst.

Evidence:
1. Topbook EWY is essentially identical baseline vs bad (vol, spread, daily volume all flat).
2. HL inside book 3.4× thinner; mid changes 4× more frequent; HL notional 3.6× higher.
3. Same drawdown profile on `usday_cov_0330` rules out cov-variant-specific bug.
4. Our acct-side activity (trades, shs, flips) is unchanged — the strat hasn't shifted behavior;
   the *response* to its behavior has shifted.

Ruled out:
- `data` — topbook and HL are directionally consistent; no symbolizer issue likely.
- `regime` in the traditional macro sense — topbook is flat, so no real-world regime change.
- `config_change` — account-side activity profile didn't change.
- `execution` (alone) — possible but the inside-liq change is the obvious mechanism; confirm
  via markouts/fillstats.

## Pending follow-ups (to lock the verdict)

- `sim-eval` over `20260528:20260610` vs `20260611:20260616` — if sim still profitable,
  it's pure execution (HL fill quality); if sim degraded, the signal/regime change shows
  up there too.
- `markout` and `fillstats` on the bad window — look for degraded markouts at +30/+60s
  (adverse selection signature) and any shift in TOD profile.
- Run on `usday_cov_0330` for cross-confirmation.
- If we have a way to detect HL participant changes (new market makers / new takers),
  check whether a notable participant appeared on EWY around 06-10/06-11.

## Actions

- Immediate: consider sizing down EWY across both `usday_cov_0313` and `usday_cov_0330` until
  the regime stabilizes (the issue is EWY-level, not cov-specific).
- Longer term: if a new HL participant is the cause, retune to handle thinner inside / faster
  mid updates — or accept reduced edge per fill in this name.

## Commands used

```bash
python ~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/pm_history.py \
    --strat-path gf2/combined_equities_bfx2/usday_cov_0313 --symbol xyz:EWY \
    --out ~/scratch/postmortems/ewy_usday_cov_0313_20260618/history.csv \
    --print-curve --summary

python ~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/pm_hl_stats.py \
    --symbol xyz:EWY --baseline 20260528:20260610 --bad 20260611:20260616

python ~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/pm_topbook_stats.py \
    --symbol EWY --baseline 20260528:20260610 --bad 20260611:20260616

# Cross-check on usday_cov_0330
python ~/tradefi/retraded_4/overmind/strat_main/tools/trademan/postmortems/pm_history.py \
    --strat-path gf2/combined_equities_bfx2/usday_cov_0330 --symbol xyz:EWY --print-curve
```

## Artifacts

- `~/scratch/postmortems/ewy_usday_cov_0313_20260618/history.csv`
- `~/scratch/postmortems/_returner_cache/xyz_EWY_*.csv`
- `~/scratch/postmortems/_topbook_cache/EWY_XNAS.BASIC_*.json`
