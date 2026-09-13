# sim_lvl_inst lag defaults — 2026-06-28 recalibration

**TL;DR.** The lag-aware sim defaults were retuned in light of a BTC mktdata
outage that silently zeroed `last_feed_lag_ms` from 2026-06-11 onward and
bucketed-RTT measurements that showed cancels run materially slower than
ALOs on live HL. New defaults are in `sim_lvl_inst.h`:

| param | old | new |
|---|---|---|
| `alo_lag_base_ms_` | 100 | **150** |
| `alo_lag_coef_`    | 0.8 | **1.25** |
| `cxl_lag_base_ms_` | 100 | **350** |
| `cxl_lag_coef_`    | 0.8 | **1.25** |
| heartbeat sym (hardcoded in `sim_lvl_inst.cc`) | BTC | **HYPE** |
| IOC params         | unchanged | (separate study) |

## What triggered the recalibration

Live performance for the 2026-06-12 `run_20260612_gf3` lag-aware retrains
was dramatically worse than promotion sim suggested. Over 4 trading days
(2026-06-18 → 06-24) under tag `usday_cov_0616`:

| sym | promotion sim pnl/d | live pnl/d |
|---|---|---|
| CBRS | +$40 | **−$510** |
| MRVL | +$150 | **−$283** |
| ARM  | +$102 | **−$194** |
| BB   | +$289 | **−$190** |
| HOOD | +$9  | **−$135** |

Trade counts were 5–80x sim counts on the losers, fillrates 2–5x sim
fillrates — meaning real-world execution was filling our quotes much
faster than sim's lag model predicted.

## Two issues found

### 1. BTC mktdata outage silently broke the heartbeat

`HyperliquidSim::onSentOrder` computed effective lag as

```cpp
eff_alo_lat = alo_lag_base_ms_ + alo_lag_coef_ * last_feed_lag_ms_;
```

`last_feed_lag_ms_` was meant to refresh from the HL trade feed — including
a BTC subscription as a always-on heartbeat for sparse-symbol windows. The
BTC mktdata pipeline stopped archiving on 2026-06-11 (file sizes dropped
from ~38MB/day to 40-byte stubs). For sparse syms whose own trade rate was
low, this meant `last_feed_lag_ms_` never updated → `coef × 0 = 0` →
effective lag collapsed to the base term only.

**Fix:** switched heartbeat sym from `BTC` to `HYPE` in
`src/pktrade/sim/sim_lvl_inst.cc` (was hardcoded as `SymbolId{"BTC"}`).
HYPE mktdata has been healthy throughout the window — ~13-37 MB/day. Code
change is in this branch (`coverage-equities`).

### 2. Live ALO/cancel RTT measurements

Bucketed live RTT analysis from
`compute_live_rtt.py` (NewOrd → NewOrdAck and Cancel → CancelAck deltas,
parsed from `orders_<date>.csv` on `gf3/usday_cov_0616`):

| stage | median ms | p95 ms |
|---|---|---|
| ALO place  | ~500 | ~750 |
| Cancel     | ~790 | ~1000 |

Cancel runs ~290 ms slower than place across all 19 sampled syms — sym
independent, so it's a venue/network property. The previous defaults had
`cxl_base = alo_base = 100` with identical coefs, i.e. the model assumed
symmetric place/cancel RTT.

## How the new defaults were chosen

After fixing the heartbeat, a 36-cell refined sweep ran across 18 gf3
retrain syms (`sweep_gf3_refined/`):

```
alo_base ∈ {100, 200, 300}     (or paired with cxl_base; see below)
cxl_base ∈ {100, 300, 500}
alo_coef ∈ {1.0, 1.25, 1.5}
cxl_coef ∈ {1.0, 1.25, 1.5}
```

The grid actually used paired `(alo_base, cxl_base) ∈ {(100,300), (150,350),
(200,400), (300,500)}` to enforce `cxl > alo` (since cancel was empirically
slower) — 4 base pairs × 3 alo_coef × 3 cxl_coef = 36 cells per sym.

Each cell was ranked by `e_fr + e_trd` (composite normalized fillrate +
trade-count error vs live; PnL tracked but not in ranking — see
`feedback_sim_fit_metric_priority.md` in memory). The top global cells
across all 18 syms:

| rank | (alo_b, cxl_b, alo_c, cxl_c) | sum(e_fr+e_trd) |
|---|---|---|
| 1 | (300, 500, 1.50, 1.25) | 17.36 |
| 2 | **(150, 350, 1.25, 1.25)** | **18.29** |
| 3 | (200, 400, 1.50, 1.50) | 18.39 |
| 4 | (300, 500, 1.25, 1.00) | 18.40 |

Top 4 within 6% of each other — landscape near optimum is flat.

## Validation: top 4 settings × every live usday config × 10 days

`sweep_live_configs/` ran the top 4 settings against all 8 live usday-non-cross
pk configs over the most recent 10 trading days. Aggregate sim PnL/d vs the
fixed live target (sum across all 72 (config, sym) rows = **+$673/d live**):

| setting | sim total $/d | sim/live | matches live sign |
|---|---|---|---|
| S1 (300,500,1.50,1.25) | +$110 | 0.16x | 6/8 configs |
| **S2 (150,350,1.25,1.25)** | **+$2,315** | 3.44x | **7/8 configs** |
| S3 (200,400,1.50,1.50) | +$1,408 | 2.09x | 7/8 configs |
| S4 (300,500,1.25,1.00) | +$2,314 | 3.44x | 6/8 configs |

S2 (rank-2 by gf3-only sum-err, but with broader sign-coverage across all
8 live configs and the lowest fillrate compression relative to live) was
chosen as the new default. S1 is more conservative in aggregate PnL but
flips sign on two winning configs (gf1/usday and gf2/usday_cov_0521).

## Caveats

- The fit was done against gf3 lag-aware retrain configs over 3-5 days of
  live data per sym. Live PnL has dramatic week-over-week variation
  (USAR's live PnL ranged +$151 to −$552 across 4 weeks under the May
  configs), so confidence in "these are correct" is bounded by the small
  window.
- `gf2/usday_cov_0616` is wildly mis-predicted by all 4 settings (sim
  $845–$1040/d vs live $36/d). Whatever's going on there isn't a venue-lag
  story — it's something else (the retrain configs picked place_thresh
  values that look fine in sim but barely break even live).
- `gf3/usday_cov_0516` lives at sim ~$0/d but live is losing $322/d — sim
  can't reproduce the live loss at any lag setting. Sym-specific
  fill-quality / adverse-selection modeling is probably missing.
- IOC params (`ioc_lag_base_ms = 700`, `ioc_lag_coef = 1.5`) were not
  recalibrated in this pass; they apply to the cross/IOC strategies, which
  are a separate study.

## Files

- `live_vs_sim.md` — initial 4-day live vs promotion-sim comparison
- `live_rtt.md` — bucketed live RTT measurement (pre-HYPE-fix framing)
- `bucket_rtt_MRVL.csv` — 15-min RTT buckets on MRVL
- `sweep_0521_hype/` — 20-day 0521 May-config lag sweep (HYPE fixed, 6 syms)
- `sweep_gf3_hype/` — 17 gf3 retrains, original 4D grid
- `sweep_mrvl_hype/` — MRVL alone, original 4D grid, post-HYPE-fix
- `sweep_gf3_refined/` — 18 gf3 retrains, refined paired-base grid (the
  sweep that picked the new defaults)
- `sweep_live_configs/` — top 4 settings × every live usday config × 10 days
  (the validation that S2 wins on sign-coverage)
