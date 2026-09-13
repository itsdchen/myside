# GF3 RelCross Mode-2 Execution Sweep

Date: 2026-05-20

This note captures the first GF3 `rel_cross` search after settling on `cross_price_mode=2` as the default crossing mode to test. The main lesson is that future sweeps should keep the mode fixed and spend search budget on execution parameters, especially `base_cross_thresh`.

Scratch artifacts:
- Run directory: `/home/pktrade/scratch/gf3_relcross_iter/mode2_exec_sample_v1`
- Vars: `/home/pktrade/scratch/gf3_relcross_iter/vars_gf3_mode2_exec_sample_v1.json`
- Run summary: `/home/pktrade/scratch/gf3_relcross_iter/mode2_exec_sample_v1/summary.md`
- Aggregate results: `/home/pktrade/scratch/gf3_relcross_iter/mode2_exec_sample_v1/aggregate_results.txt`
- Go-live candidate config: `/home/pktrade/scratch/gf3_relcross_iter/pk_gf3_top6_mode2_relcross_v1.json`
- Go-live manifest: `/home/pktrade/scratch/gf3_relcross_iter/pk_gf3_top6_mode2_relcross_v1_manifest.md`

## Sweep Shape

Dates passed to `SimVariations`: `20260512` through `20260515`. The tool ran local dates `20260512`, `20260513`, and `20260514`.

Universe with usable local May 12 coverage:

`bird,bx,crcl,crwv,dram,ebay,ewy,ewz,gme,lite,mrvl,rklb,xle,zm`

Skipped symbols:
- `CBRS` and `PURRDAT`: remote TopBookEquity files were missing locally for the window.
- `JP225` and `KR200`: present in saved strategy configs, but the JSON contains comments and the configs use special refs (`NIY`, `KS`, `NIKKEI225`, `KOSPI200`). They were not included in this plain GF3 equity pass.

Fixed parameters:
- `cross_price_mode=2`
- `premium_ema_coef=1.0`
- `min_edge_over_spread_bps=0`
- `ioc_ord_latency=0.9`
- `cxl_ord_latency=0.9`

Sampled parameters:
- `base_cross_thresh=[0.00025,0.00035,0.0005,0.00075,0.001,0.0015]`
- `exit_adjust=[0.2,0.3,0.5]`
- `ms_between_cross=[500,1000,1500]`
- `remote_mom_tdc_ms=[500,1000]`
- `remote_mom_coef=[0,0.2,0.3]`
- `remote_mom_max_thresh_frac=0.5`

This was a sampled run: 60 random variants per symbol from a 324-combination grid. It is a discovery pass, not an exhaustive validation.

## Best Sampled Row Per Symbol

| rank | sym | traded | thresh | avg pnl | closed pnl | sharpe | pos days | trades | score |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | LITE | xyz:LITE | 0.00025 | 184.72 | 201.27 | 2.68 | 1.00 | 647 | 552.21 |
| 2 | CRWV | xyz:CRWV | 0.00025 | 172.10 | 200.17 | 3.23 | 1.00 | 680 | 492.11 |
| 3 | RKLB | xyz:RKLB | 0.00050 | 160.31 | 185.57 | 1.39 | 1.00 | 974 | 450.11 |
| 4 | MRVL | xyz:MRVL | 0.00075 | 122.82 | 134.95 | 1.55 | 1.00 | 348 | 448.18 |
| 5 | CRCL | xyz:CRCL | 0.00150 | 113.90 | 120.87 | 2.31 | 1.00 | 155 | 387.56 |
| 6 | DRAM | xyz:DRAM | 0.00075 | 98.25 | 109.50 | 1.80 | 1.00 | 406 | 267.71 |
| 7 | BIRD | xyz:BIRD | 0.00150 | 26.73 | 28.77 | 0.80 | 0.67 | 93 | 54.06 |
| 8 | EWY | xyz:EWY | 0.00050 | 7.00 | 13.31 | 1.41 | 1.00 | 171 | 20.39 |
| 9 | XLE | xyz:XLE | 0.00035 | 6.07 | 6.47 | 1.43 | 1.00 | 25 | 7.87 |
| 10 | EWZ | xyz:EWZ | 0.00075 | 28.60 | 28.84 | 0.65 | 0.50 | 10 | 6.01 |
| 11 | ZM | xyz:ZM | 0.00100 | 4.83 | 5.89 | 0.28 | 0.67 | 35 | 4.03 |
| 12 | EBAY | xyz:EBAY | 0.00025 | -6.50 | -5.67 | -0.18 | 0.67 | 26 | 3.53 |
| 13 | GME | xyz:GME | 0.00035 | 2.32 | 3.57 | 0.11 | 0.67 | 41 | 1.90 |
| 14 | BX | xyz:BX | 0.00150 | 4.13 | 4.33 | 0.28 | 0.33 | 9 | -1.70 |

## Go-Live Candidate

Built config:

`/home/pktrade/scratch/gf3_relcross_iter/pk_gf3_top6_mode2_relcross_v1.json`

Included symbols: `LITE`, `CRWV`, `RKLB`, `MRVL`, `CRCL`, `DRAM`.

Sizing was intentionally conservative:
- `tgt_order_notional=500`
- `tgt_maxpos_notional=1000`

The config validates with `jq` and `pktrade --dry-run` on `20260512`. The dry run subscribed to Hyperliquid `xyz:CRCL`, `xyz:CRWV`, `xyz:DRAM`, `xyz:LITE`, `xyz:MRVL`, `xyz:RKLB` and the corresponding TopBookEquity feeds.

Per-symbol selected rows:

| sym | thresh | exit | ms_between_cross | mom_tdc_ms | mom_coef | fillrate | score |
|---|---:|---:|---:|---:|---:|---:|---:|
| LITE | 0.00025 | 0.5 | 500 | 500 | 0 | 0.2475 | 552.21 |
| CRWV | 0.00025 | 0.3 | 500 | 500 | 0.2 | 0.2221 | 492.11 |
| RKLB | 0.00050 | 0.3 | 500 | 500 | 0.2 | 0.2236 | 450.11 |
| MRVL | 0.00075 | 0.5 | 1500 | 1000 | 0.3 | 0.4310 | 448.18 |
| CRCL | 0.00150 | 0.5 | 500 | 500 | 0.2 | 0.1463 | 387.56 |
| DRAM | 0.00075 | 0.5 | 1500 | 500 | 0 | 0.3844 | 267.71 |

## Learnings

`cross_price_mode=2` should be the default mode for these searches for now. The useful question is no longer mode 1 vs mode 2 on every pass; it is how each symbol wants to cross once mode 2 is fixed.

`base_cross_thresh` is the most important axis to keep wide. The best rows landed across the full range: `LITE/CRWV` liked `0.00025`, `RKLB/EWY` liked `0.00050`, `MRVL/DRAM` liked `0.00075`, and `CRCL/BIRD/BX` liked `0.00150`. A narrow GF1-style threshold range would have missed several viable candidates.

The top six were strong enough for small-size live discovery. `LITE`, `CRWV`, `RKLB`, `MRVL`, `CRCL`, and `DRAM` all had positive PnL, positive closed PnL, positive days on all three sim days, and enough trades to be worth looking at live.

Second-tier and sparse names should not be promoted from this pass alone. `BIRD` was positive but weaker. `EWY`, `XLE`, `EWZ`, `ZM`, `GME`, `EBAY`, and `BX` either had low PnL, sparse trading, poor positive-day rate, or negative average PnL.

Remote momentum still looks worth keeping in the grid, but it should be capped relative to threshold. In this run `remote_mom_max_thresh_frac=0.5` kept the momentum adjustment from overwhelming the base crossing threshold. Winners did not all require momentum: `LITE` and `DRAM` selected `remote_mom_coef=0`, while `CRWV/RKLB/CRCL` selected `0.2` and `MRVL` selected `0.3`.

## Caveats and Next Work

This was only a three-day sample. Treat the output as live-discovery sizing, not a final production allocation.

The run sampled 60 of 324 combinations per symbol. For any symbol that looks good live, rerun a finer or exhaustive local search around the selected threshold and timing region.

The current latency assumption is `0.9s` for IOC and cancel. Earlier work suggested checking both `0.9s` and `1.1s`; this GF3 pass used `0.9s` for the first candidate config. If live observed RTT is closer to `1.1s`, repeat the same threshold-focused search at `1.1s`.

Future GF3 search template:
- Fix `cross_price_mode=2`.
- Keep `premium_ema_coef=1.0` unless there is a specific reason to revisit it.
- Keep `min_edge_over_spread_bps=0` unless adding a separate edge filter with a clear non-overlap vs threshold.
- Sweep `base_cross_thresh` broadly.
- Sweep `ms_between_cross`, `exit_adjust`, and capped remote-momentum parameters.
- Promote symbols based on raw PnL, closed PnL, positive-day rate, trade count, and fillrate, not score alone.
