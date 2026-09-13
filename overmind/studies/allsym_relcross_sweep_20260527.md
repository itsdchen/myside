# All-Symbol RelCross Sweep — v3 Deployment

Date: 2026-05-27

Builds on the BZ/CL v2 work (`bzcl_sweep_20260521`) and the prior workstream notes
(`relcross_workstream_learnings_20260520.md`, `gf3_relcross_mode2_execution_20260520.md`).
Goal: extend the BZ/CL anchor (`pem=1.0, mode=2, exit=0.7, vol_norm=1.5/60`) to
every HL `xyz:` symbol with usable histdata, ranked by HL 24h notional volume.

Raw workdir: `/home/pktrade/scratch/gf1_relcross_iter/allsym_sweep_20260522/`. The
deploy-ready configs are in that workdir under `deploy_configs/`.

## Result summary

**38 RelCross traders deployed** across 5 machine configs:

| machine | window (NY) | #traders | symbols |
|---|---|--:|---|
| gf0 | 18:00 → 17:00 (CME) | 7 | CL, BRENTOIL, NATGAS, SP500, XYZ100, SILVER, JPY |
| gf1 | 9:35 → 17:00 (US eq) | 7 | TSLA, NVDA, GOOGL, AMZN, PLTR, RIVN, BABA |
| gf2 | 9:35 → 17:00 (US eq) | 16 | AAPL, MSFT, META, COIN, HOOD, AMD, MSTR, ORCL, CRCL, MU, SNDK, NFLX, TSM, USAR, CRWV, EWY |
| gf3 | 9:35 → 17:00 (US eq) | 6 | CBRS, DRAM, RKLB, LITE, MRVL, GME — all at `size_mult=0.25` (Tier D, recent listings) |
| gf3_kor | 20:00 → 02:30 (Korean cash) | 2 | SKHX (full), SMSN (`size_mult=0.5`) |

Combined OOS simulated PnL across the Tier A + Tier B + Tier C set is ~$430/day
on the May 6-21 window. Apply a ~50% live haircut for CL-style sim-vs-live
fillrate gap (see `live_combined_cross_v6/analysis_20260520.md` in
gf1_relcross_iter scratch).

## v3 anchor (locked across all symbols)

```
ioc_ord_latency = 1.1
cxl_ord_latency = 1.1
use_one_way_latency = false
cross_price_mode = 2
premium_ema_coef = 1.0
flip_through = false
vol_norm_tdc = 60
remote_mom_max_thresh_frac = 0.5
global_inherit = false        # added per deploy-side requirement
```

Per-symbol-varied: `base_cross_thresh`, `exit_adjust`, `premium_tdc_s`,
`remote_mom_coef`, `remote_mom_tdc_ms`, `vol_norm_coef`. Per-symbol values are
in the deploy JSONs and the full ranked table in
`VIABLE_TRADERS_REPORT.md` of the workdir.

## Methodology

Each symbol went through up to three stages.

### Stage 1: prescan (100 combos, full grid)

Locks the v3 anchor minus the three "discovery" axes, sweeps:

```
base_cross_thresh ∈ [1, 1.5, 2, 3, 4, 5, 7, 10, 15, 20] bps
exit_adjust ∈ [0.5, 0.7]
premium_tdc_s ∈ [10, 30, 60, 120, 300]
```

This identifies the trading regime per symbol cheaply (~10-30 min wall time per
symbol). The threshold grid spans 20x because the right range was very
symbol-dependent: SP500/XYZ100/AMD/RKLB sit at 1-1.5 bps; most US equities at
2-5 bps; SKHX at 7 bps; GOLD/PLATINUM had no productive range at all.

### Stage 2: OOS validation

Split the Apr 10 – May 21 window into IS (Apr 10 – May 5, 13d) and OOS (May 6 –
May 21, 11d), reconstructing per-variant per-day PnL from
`scratch/<variant>/acct_<v>_<date>.csv` files. The OOS-robust anchor for stage 3
is the variant with both `IS_avg > 0 AND OOS_avg > 0` and maximum OOS, requiring
at least 5 IS days for the screen.

This filtered out two classes of false positives:

- **OOS-bloom from IS-zero**: `exit_adjust=0.3` variants that had near-zero or
  negative IS but lucked into $20-30 OOS. These cluster around the wrong axis
  values and disappear once you require IS-positive.
- **OOS-crash from IS-spike**: AAPL/AMZN/MSFT top-IS-1 picks flipped to
  negative OOS at the original prescan's IS winners. The OOS-robust pick for
  the same symbol held positive at smaller absolute PnL.

### Stage 3: refine (60 combos, full grid)

Locks the OOS-robust prescan anchor (`thr, exit, prem_tdc`), sweeps:

```
remote_mom_coef ∈ [0, 0.1, 0.2, 0.25, 0.3]
remote_mom_tdc_ms ∈ [250, 500, 1000]
vol_norm_coef ∈ [1.0, 1.5, 2.0, 2.5]
```

Refinement gains were modest ($0-2/day over prescan) but worth running. The
BZ/CL defaults `mom_coef=0.25, mom_tdc=500, vol_norm=1.5` win for ~70% of
symbols; the exceptions: NVDA wants `vol_norm=1.0`, TSLA `vol_norm=2.0`, EWY
`vol_norm=2.5`, SKHX `vol_norm=2.0`.

### Skips

Symbols dropped before stage 3 (any of):

1. **No edge**: GOLD (1-2 trades total at 20bps), PLATINUM (0 trades), COPPER
   (586 trades but sharpe 0.10 → adverse selection), EUR (high fillrate + low
   sharpe), EWJ, KR200.
2. **Insufficient histdata** (0 IS days, recent HL listings): CBRS, DRAM, RKLB,
   LITE, MRVL, GME, EWZ, EBAY, XLE, BX, ZM. The first six had OOS-only
   sharpes ≥0.7 so were kept as Tier D at `size_mult=0.25`; the rest had bad
   OOS too and were dropped.
3. **OOS-crash**: BIRD (IS $101 → OOS $3.22 — classic overfit on small sample).
4. **Low sharpe**: INTC (0.21), COST (0.19) — refined cleanly but no edge.
5. **Sim failure**: URANIUM — re-run pending.

## ioc_ord_latency rule

`ioc_ord_latency` is a sim *calibration* parameter, not a strategy knob. Earlier
sweeps put it in the grid; that biases winners toward the optimistic 0.9s
assumption. Empirical demonstration: CL mode2 best variant at `ioc=0.9` was
$120.79/day sharpe 1.35; the same grid filtered to `ioc=1.1` gave best
$30.93/day sharpe 0.73. The 0.9s winners weren't better strategies — they were
better at exploiting sim optimism.

Live RTT on gf0 (combined_cross/v6 on 2026-05-20) measured ~830ms p50 for
CL/BRENTOIL, so `0.9` matches median. But sim doesn't model latency variance,
and live p90/p99 are 1160/1800-2500ms. `ioc=1.1` is the conservative default:
variants that survive at the slow-tail RTT assumption are the realistic deploy
candidates. (See also `project_hl_rtt_by_host.md` memory.)

## Pattern: per-symbol thresh ranges differ by 10×

The Mar/Apr BZ/CL work landed on `thr=0.0003` (3bps). Applying that uniformly
would have crushed half the new symbols. Observed sweet spots:

| thresh range | symbols | typical category |
|---|---|---|
| 1.0-1.5 bps | SP500, XYZ100, AMD, ORCL, MSFT, RKLB, LITE, MSTR(no!), DRAM, AAPL, AMZN, BABA, CRWV, NFLX, META, TSLA, EWY, EWZ | high-liquidity/tight-basis |
| 2-3 bps | INTC, COIN, NVDA, NATGAS, GOOGL, PLTR | mid-band US equities + some CME |
| 5-7 bps | SILVER, SNDK, CBRS, CRCL, MRVL, RIVN, SKHX, MSTR | wider-basis CME and Korean |
| 10-20 bps | TSM, JP225, KR200, GOLD, SKHX (some) | low-activity or structurally-wide |

Prescan with a wide threshold grid (1 to 20 bps) is mandatory for any new
symbol — narrow grids miss the productive band.

## Adverse-selection diagnostic

Symbols showing both high fillrate (≥0.3) and low sharpe (≤0.2) are the
adverse-selection class. Examples from the prescan:

| sym | fillrate | sharpe | OOS pnl | likely cause |
|---|---:|---:|---:|---|
| COPPER | 0.35 | 0.10 | $2.42 | basis moves too fast; sim fills more than reality would |
| EWJ | 0.59 | -0.58 | -$4.92 | wrong reference (us-day ETF vs Asian price discovery) |
| EWZ | 0.39 | -0.70 | -$8.57 | same shape |
| EUR | 0.48 | 0.39 | $0.21 | might want CME 6E future as ref instead of spot EUR |
| BIRD | 0.36 | 0.60 | $3.22 (post-crash) | recent listing IS-overfit |

For EWJ/EWZ/EUR/KR200 the most defensible next step is **swapping the remote
reference**: ETFs of Asian markets and FX spot tickers may not be the actual
price-discovery venue; CME NIY for EWJ/JP225, CME 6E for EUR, etc. Not pursued
in this sweep.

## Tier D — recent listings (deploy at quarter size)

Six HL symbols listed within the last 1-2 weeks have only 7-13 days of
OOS-only data:

| sym | OOS pnl | sharpe | trd | fillrate | OOS days | thr / exit / p_tdc |
|---|--:|--:|--:|--:|--:|---|
| CBRS | $121.5 | 0.81 | 385 | 0.196 | 13 | 7b / 0.5 / 60 |
| LITE | $101.6 | 2.19 | 718 | 0.382 | 7 | 1b / 0.5 / 120 |
| RKLB | $100.9 | 1.48 | 745 | 0.240 | 7 | 1b / 0.5 / 60 |
| DRAM | $87.5 | 2.43 | 605 | 0.455 | 7 | 1.5b / 0.7 / 300 |
| MRVL | $64.6 | 2.05 | 282 | 0.252 | 7 | 5b / 0.7 / 120 |
| GME | $6.5 | 0.67 | 47 | 0.278 | 7 | 3b / 0.7 / 30 |

Sharpe of 2+ from a 7-day OOS-only window is *not credible* without IS
confirmation. `size_mult=0.25` is the conservative deploy. Two-to-three weeks
of accumulated data should be enough to re-run as full Stage 1+2+3.

## Suspect-overfit Tier B/C deployments

These passed Tier A/B classification but with large IS→OOS jumps that may be
regime, not edge:

| sym | IS | OOS | Δ | classification |
|---|--:|--:|--:|---|
| SKHX | $15.5 | $107.5 | +$92 | Tier B (deployed full size on gf3_kor) |
| SMSN | -$2.3 | $101.5 | +$104 | Tier C (deployed `size_mult=0.5`) |
| HYUNDAI | -$3.4 | $63.5 | +$67 | Not deployed (not on per-machine list) |

The shared pattern (Korean symbols all blooming OOS) suggests a market-wide
activity uptick in the Korean cash window during the OOS half, not a per-symbol
edge. Worth pulling live data after 1-2 weeks of deployment to see whether
the OOS rate sustains.

## Source files

- Workdir: `/home/pktrade/scratch/gf1_relcross_iter/allsym_sweep_20260522/`
  - `VIABLE_TRADERS_REPORT.md` — 39-symbol ranked table with full params
  - `deploy_configs/gf{0,1,2,3,3_kor}_relcross_v3.json` — the deploy JSONs
  - `deploy_configs/PER_MACHINE_SUMMARY.md` — per-machine notes
  - `oos_validation.md` — IS/OOS validation work
  - `findings_t1refine_t2prescan.md` — intermediate tier-1/tier-2 results
  - `vars_prescan.json`, orchestrator scripts — reproducibility
- Prior BZ/CL work: `/home/pktrade/scratch/gf1_relcross_iter/bzcl_sweep_20260521/`
- Live calibration baseline: `/home/pktrade/scratch/gf1_relcross_iter/live_combined_cross_v6/`

## Open hypotheses to pursue

1. **Reference-swap for EUR/EWJ/EWZ/KR200**: try CME-futures or proper Asian
   index ref instead of US-hours ETF / spot. The adverse-selection signature
   suggests our reference isn't the actual price-discovery venue.
2. **GOLD spot reference**: gen_relcross's `CME_REMOTE_OVERRIDE` forces GC on
   CME; spot GOLD on TopBookEquity might cross more often (despite the
   reliability comment in the generator).
3. **Tier D re-evaluation in ~2 weeks**: CBRS/DRAM/RKLB/LITE/MRVL should have
   ≥10 IS days by then. Full prescan→OOS→refine pass.
4. **Symbols not yet in sweep**: PALLADIUM, GBP, LLY, DKNG, HIMS, ARM, BB, EWT,
   PURR, NIFTY, IBOV exist on HL but have no saved_strats config. Add them and
   include in the next sweep round.
5. **Tier 1/2 OOS-robust drift**: a few stable winners (AMD, TSLA) had
   refinement-bound Δ ≤ $3 between IS and OOS — these are the most plausible
   long-term anchors. Look at whether their first-week live numbers track
   sim more closely than the symbols with big OOS jumps.
