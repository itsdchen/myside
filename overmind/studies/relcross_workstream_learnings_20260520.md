# RelCross Workstream Learnings

Date: 2026-05-20

This summarizes the RelCross iteration workstream across TSLA, GF1 equities, the first live GF1 run, CME commodities, BZ data quality, and GF3. The raw run directories live mostly under `~/scratch/tsla_relcross_iter`, `~/scratch/gf1_relcross_iter`, and `~/scratch/gf3_relcross_iter`.

## Main Conclusions

`cross_price_mode=2` is the right default for now. Mode 1 consistently increases fillrate and trade count, but those extra fills have generally been lower quality after fees/adverse selection. The next useful search is not "mode 1 vs mode 2" on every symbol; it is mode 2 with better thresholds, timing, and guarded momentum.

`base_cross_thresh` is the main execution knob. Across TSLA, GF1, CME, and GF3, useful thresholds varied materially by symbol and regime. Narrow threshold grids are risky because they can miss both low-threshold liquid names and wider-threshold sparse names.

Remote-signal momentum has real power, but it needs a cap. The useful version is a capped adjustment relative to the threshold, not an unbounded second signal. `remote_mom_max_thresh_frac=0.5` was a reasonable first production-safe cap; some 1.1s fallback searches showed `0.75` can help, but that is more aggressive.

Latency matters, but the live issue was not just RTT. Live order response timing was around the 0.9-1.1s sim assumptions, yet realized fill/trade count was much lower than sim. That points to fill modeling, queue/availability, remote/local alignment, or live exchange behavior as the bigger gap.

## TSLA Iteration

Scratch note: `/home/pktrade/scratch/tsla_relcross_iter/tsla_relcross_sweep_notes.md`

Initial TSLA runs used the existing generator from `overmind/for_live/saved_strats/tsla/pk_tsla_usday.json` over 2026-04-15 through 2026-04-22, with complete local sim dates `20260415`, `20260416`, `20260417`, `20260420`, `20260421`, and `20260422`.

The first sweep varied IOC latency, threshold, premium EMA, and exit adjustment. The strongest finding was latency sensitivity: average PnL dropped sharply as simulated IOC latency moved from `0.5s` to `0.9s` and `1.1s`. Low thresholds were best, `premium_ema_coef=1.0` was slightly better than `0.8`, and easy exits were better than sticky exits.

The early practical candidate was:

```json
{
  "base_cross_thresh": 0.00035,
  "premium_ema_coef": 1.0,
  "exit_adjust": 0.3,
  "cross_price_mode": 2,
  "min_edge_over_spread_bps": 0
}
```

Remote momentum then improved the TSLA search. Uncapped high momentum coefficients caused too much churn, but capped momentum kept the useful signal while limiting threshold distortion. The capped region that looked best at faster RTT was around:

```json
{
  "base_cross_thresh": 0.0002,
  "premium_ema_coef": 1.0,
  "exit_adjust": 0.3,
  "cross_price_mode": 2,
  "remote_mom_tdc_ms": 500,
  "remote_mom_coef": 0.25,
  "remote_mom_max_thresh_frac": 0.5
}
```

For the slower `0.9s`/`1.1s` question, the finer TSLA search found that `0.9s` still had usable power. Top `0.9s` rows clustered around low thresholds (`0.00025` to `0.00035`), `exit_adjust=0.3`, short momentum decay (`250-1000ms`), and `remote_mom_coef=0.20-0.30`. The best `1.1s` rows were positive but much weaker; they looked like fallback configs rather than the right optimization target.

## GF1 Equities

Scratch note: `/home/pktrade/scratch/gf1_relcross_iter/gf1_relcross_sweep_notes.md`

For GF1, the non-TSLA names tested were `NVDA`, `PLTR`, `GOOGL`, `AMZN`, `RIVN`, and `BABA`, using the same broad slow-IOC/momentum grid over 2026-04-15 through 2026-04-22. TSLA came from the full fine run.

Best sampled non-TSLA rows:

| symbol | ioc | thresh | exit | tdc | coef | avg pnl | sharpe | trades | score |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| PLTR | 0.9 | 0.00025 | 0.3 | 500 | 0.20 | 34.56 | 2.58 | 263 | 96.19 |
| RIVN | 0.9 | 0.00045 | 0.3 | 250 | 0.00 | 32.64 | 3.03 | 110 | 93.40 |
| NVDA | 0.9 | 0.00030 | 0.3 | 500 | 0.30 | 15.83 | 2.27 | 259 | 40.49 |
| GOOGL | 0.9 | 0.00025 | 0.3 | 500 | 0.20 | 12.38 | 2.09 | 179 | 33.25 |
| AMZN | 0.9 | 0.00030 | 0.3 | 1000 | 0.30 | 11.51 | 0.83 | 157 | 22.02 |
| BABA | 1.1 | 0.00035 | 0.2 | 250 | 0.20 | 6.43 | 0.77 | 44 | 8.49 |

The practical top group was TSLA, PLTR, RIVN, and GOOGL, with NVDA/AMZN weaker and BABA not compelling. The combined GF1 candidate config was:

`/home/pktrade/scratch/gf1_relcross_iter/pk_gf1_top4_ioc11_relcross.json`

The 1.1s mode comparison on the top four made mode 2 the clear default:

| symbol | mode 1 avg pnl | mode 2 avg pnl | mode 1 score | mode 2 score | read |
|---|---:|---:|---:|---:|---|
| RIVN | 8.52 | 22.62 | 10.75 | 55.16 | mode 2 clearly better |
| TSLA | -104.45 | 13.65 | -92.45 | 39.45 | mode 1 overtraded badly |
| PLTR | 9.60 | 8.72 | 16.07 | 24.27 | mode 1 raw PnL slightly higher, but worse quality |
| GOOGL | -2.00 | 4.98 | 0.12 | 9.83 | mode 2 better |

Live-day replay on 2026-05-15 told the same story: mode 2 was better for TSLA and GOOGL and less bad for RIVN. Mode 1 mainly bought more fills.

## Live GF1 Run

Live files: `/home/pktrade/scratch/gf1_relcross_iter/live_files/v1`

The live run on 2026-05-15 covered about 339.6 minutes from 10:02:23 to 15:42:00 ET, roughly 87% of a regular session.

End-to-end IOC response timing was in the same neighborhood as the sim assumptions:

| symbol | sent orders | filled orders | first response med/p90 ms | fill med/p90 ms |
|---|---:|---:|---:|---:|
| RIVN | 132 | 8 | 861/1193 | 871/914 |
| TSLA | 936 | 41 | 901/1246 | 938/1213 |
| PLTR | 63 | 0 | 941/1217 | n/a |
| GOOGL | 426 | 37 | 921/1239 | 933/1226 |

The live-vs-sim gap was fillrate/trade count:

| symbol | actual closed | actual trades | expected trades scaled | actual / expected trades | live fill rate |
|---|---:|---:|---:|---:|---:|
| RIVN | -1.76 | 8 | 92.3 | 9% | 0.05 |
| TSLA | 7.01 | 41 | 186.4 | 22% | 0.03 |
| PLTR | 0.00 | 0 | 43.5 | 0% | 0.00 |
| GOOGL | 1.61 | 37 | 92.3 | 40% | 0.04 |

This shifted the debugging priority. RTT was not wildly wrong; the strategy simply filled far less than expected. We identified logging needs around attempted crosses, remote/local state at send time, response type and timing, markouts, and sim-vs-live opportunity reconstruction. That logging was the right next step before overfitting thresholds to one live day.

## CME Commodities

Mode sample summary: `/home/pktrade/scratch/gf1_relcross_iter/cme_mode_sample_v1/cme_mode_sample_summary.md`

Capacity summary: `/home/pktrade/scratch/gf1_relcross_iter/cme_capacity_v1/capacity_summary.md`

Go-live manifest: `/home/pktrade/scratch/gf1_relcross_iter/pk_cme_commodities_go_live_relcross_v1_manifest.md`

The CME-based pass tested `BZ`, `CL`, `COPPER`, `GOLD`, `NATGAS`, `NQ`, and `SILVER` over 2026-04-15 through 2026-04-22. This used `TopBookCme` as the remote book and fixed `premium_ema_coef=1.0`.

Mode 2 won best sampled score for every CME symbol:

| symbol | mode 1 score | mode 2 score | mode 1 pnl | mode 2 pnl | read |
|---|---:|---:|---:|---:|---|
| BZ | 56.54 | 71.93 | 7.10 | 24.53 | usable but data-quality-sensitive |
| CL | 161.61 | 237.40 | 94.22 | 92.35 | strongest live candidate |
| COPPER | -248.21 | -150.26 | -301.84 | -183.18 | not live-ready |
| GOLD | -457.81 | -129.19 | -458.08 | -170.03 | not live-ready after fees |
| NATGAS | -5.78 | 23.46 | -10.27 | 12.00 | optional, lower capacity |
| NQ | 9.58 | 21.76 | 2.93 | 7.86 | positive but not focus |
| SILVER | -52.29 | 18.03 | -152.49 | -146.78 | score positive but PnL bad |

The go-live commodity config selected CL and BZ enabled at small size, with NATGAS present but disabled:

`/home/pktrade/scratch/gf1_relcross_iter/pk_cme_commodities_go_live_relcross_v1.json`

| symbol | traded symbol | enabled | order notional | maxpos notional | thresh | exit | mom tdc | mom coef |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| CL | xyz:CL | true | 500 | 1000 | 0.00035 | 0.3 | 500 | 0.30 |
| BZ | xyz:BRENTOIL | true | 500 | 1000 | 0.00035 | 0.3 | 500 | 0.30 |
| NATGAS | xyz:NATGAS | false | 500 | 1000 | 0.00040 | 0.3 | 250 | 0.00 |

Capacity read:
- CL scaled best. It stayed close to linear through roughly `$5k` order notional and remained positive through `$10k`, though the live test should start much smaller.
- BZ was usable but less clean. `$2k-$3k` looked reasonable, `$5k` still positive but lower efficiency, and `$7.5k-$10k` looked capacity/slippage-risky.
- NATGAS saturated quickly. `$1k` captured most of the dollars; larger sizes mainly lowered efficiency.
- COPPER/GOLD/SILVER should not get capacity until the base search is fixed.

## BZ Data Quality and Roll Issue

Quality outputs:
- `/home/pktrade/scratch/gf1_relcross_iter/bz_cme_quality_v1/bz_viz_quality_summary.csv`
- `/home/pktrade/scratch/gf1_relcross_iter/bz_cl_quality_through_20260518/viz_tmp.csv`

The mid-March BZ issue was real. Around 2026-03-14 through 2026-03-23, the CME BZ proxy became weak or stale relative to the local Brent reference. Several days had very low remote-change counts, large gaps, and abnormal premium distributions. Examples from the BZ quality summary:
- 2026-03-15 had only 5 remote changes over the covered window.
- 2026-03-22 had only 8 remote changes.
- 2026-03-14 and 2026-03-21 had max gaps over 100 seconds.
- Premiums were badly distorted on several of those days.

The practical lesson is that BZ should not be treated like CL unless the proxy quality passes a freshness/change-rate check. For future BZ sims, add or enforce a roll/proxy-quality guard: skip days or intervals where the CME proxy is stale, has large gaps, or has a structurally distorted premium. CL looked cleaner and is the safer first commodity live test.

## GF3 Search

Detailed GF3 note: `/home/pktrade/tradefi/retraded_1/overmind/studies/gf3_relcross_mode2_execution_20260520.md`

Run directory: `/home/pktrade/scratch/gf3_relcross_iter/mode2_exec_sample_v1`

Go-live candidate:

`/home/pktrade/scratch/gf3_relcross_iter/pk_gf3_top6_mode2_relcross_v1.json`

GF3 fixed mode 2 and swept execution parameters over local dates 2026-05-12 through 2026-05-14. Eligible symbols were `bird,bx,crcl,crwv,dram,ebay,ewy,ewz,gme,lite,mrvl,rklb,xle,zm`; `CBRS` and `PURRDAT` were missing local remote files, and `JP225/KR200` were deferred because their saved configs need special handling.

Best sampled rows:

| rank | sym | traded | thresh | avg pnl | closed pnl | sharpe | pos days | trades | score |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | LITE | xyz:LITE | 0.00025 | 184.72 | 201.27 | 2.68 | 1.00 | 647 | 552.21 |
| 2 | CRWV | xyz:CRWV | 0.00025 | 172.10 | 200.17 | 3.23 | 1.00 | 680 | 492.11 |
| 3 | RKLB | xyz:RKLB | 0.00050 | 160.31 | 185.57 | 1.39 | 1.00 | 974 | 450.11 |
| 4 | MRVL | xyz:MRVL | 0.00075 | 122.82 | 134.95 | 1.55 | 1.00 | 348 | 448.18 |
| 5 | CRCL | xyz:CRCL | 0.00150 | 113.90 | 120.87 | 2.31 | 1.00 | 155 | 387.56 |
| 6 | DRAM | xyz:DRAM | 0.00075 | 98.25 | 109.50 | 1.80 | 1.00 | 406 | 267.71 |

The first GF3 go-live candidate uses the top six with conservative `tgt_order_notional=500` and `tgt_maxpos_notional=1000`. The most important GF3-specific lesson is again threshold breadth: winners spanned `0.00025`, `0.00050`, `0.00075`, and `0.00150`.

## Future Search Template

Use this as the default RelCross sweep shape unless there is a specific reason to deviate:

- Fix `cross_price_mode=2`.
- Fix `premium_ema_coef=1.0`.
- Set `min_edge_over_spread_bps=0` unless we add a clearly separate edge filter.
- Sweep `base_cross_thresh` broadly.
- Sweep `exit_adjust` and `ms_between_cross`.
- Include capped remote momentum: short `remote_mom_tdc_ms`, moderate `remote_mom_coef`, and `remote_mom_max_thresh_frac` around `0.5`.
- Search both `0.9s` and `1.1s` IOC/cancel assumptions when live RTT is uncertain, but do not let latency tuning distract from live fillrate validation.
- Promote symbols on raw PnL, closed PnL, positive-day rate, trade count, fillrate, and live diagnosability, not score alone.

## Open Questions

The biggest unresolved question is why live fillrate was so much lower than sim on the GF1 test. Before scaling, we need enough logging to reconstruct every missed/filled opportunity with the local book, remote book, quote age, premium, threshold, momentum adjustment, order response, and markout.

For BZ, we need explicit roll/proxy-quality rules before trusting longer backtests. The mid-March behavior can otherwise pollute both search and capacity estimates.

For GF3, the top six are only supported by a short three-day sample. The right next step is live discovery at small size or a longer/finer sim around the selected thresholds if fresh local data is available.
