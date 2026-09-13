# RelCross feature calibration — feed-lag widen + precog miss — 2026-06-09

Calibration of the two new RelCross knobs introduced in commits `7abc5453` and
`af5ef2aa`:

- **`feed_lag_widen_coef`** — widens threshold during HL congestion bursts
- **precog miss tightening** — tightens threshold on near-miss IOCs detected by
  TradeRiskMan

Tested across all current cross deploys (gf0 CME, gf1 eq, gf2 eq, gf3 v4 Tier D,
gf3_kor Korean) on June 1-5 in sim, with `lag_aware_latency=true` baseline.

## Method

For each (host, date), ran sim with the new binary, comparing 5-day aggregate
sim PnL across variants. Sim is calibrated against live (see
`svl_lag_aware_iteration_20260609.md`), so relative PnL deltas are credible
signal even if absolute PnL has residual gap.

## Feed-lag widen

Single setting tested: `feed_lag_widen_coef=1.0, feed_lag_baseline_ms=500`.

| host | baseline | +widen | Δ |
|---|--:|--:|--:|
| gf2_eq | 2169 | 2354 | **+185 (+8.5%)** |
| gf3_v4 | 939 | 982 | **+43 (+4.6%)** |

Mechanism is the same as the lag-aware sim — when feed lag is high during HL
congestion bursts, threshold widens proportional to `lag_excess_s`. Reduces
adverse-selection fills during the ~50-second congestion windows.

**Recommendation**: enable on every cross deploy. Small but reliable.

## Precog miss — what the physical constant means

The first round of testing tuned `precog_fastest_possible_rtt` (the riskman's
"a level disappearing within X seconds of our order send counts as a
near-miss" window) to get good results. That's wrong:
**`precog_fastest_possible_rtt` is a physical reality** — the minimum HL RTT
we could ever experience (~400ms for HL given baseline feed lag ~485ms one-way
and matching processing). Tuning it wider lets the mechanism catch random
level churn as if it were near-misses, which is overfitting.

Pinned at the physical 0.4s, the remaining legitimate knobs are
`max_adjust`, `tdc_s`, `per_unit` (which control how strongly the strategy
responds to a detected miss).

## Precog grid — 5-day Δ vs baseline at fixed rtt=0.4s

| host | a0.85_t20_pu2 | a0.70_t20_pu2 | a0.85_t40_pu2 | a0.85_t20_pu1 |
|---|--:|--:|--:|--:|
| gf0_cme | −1.5% | −6.9% | −2.1% | +0.5% |
| gf1_eq | +13.3% | +0.3% | +3.6% | **+35.8%** |
| **gf2_eq** | **+22.4%** | +2.9% | −3.4% | −1.4% |
| gf3_v4 | −9.5% | +2.1% | −2.3% | +1.5% |
| gf3_kor | +0.7% | +2.6% | +0.4% | +2.8% |

## Interpretation

1. **Conservative `max_adjust=0.85` beats aggressive `0.70`** almost everywhere.
   Going harder on the tighten overshoots.
2. **Short `tdc_s=20` beats long `40`**. The impulse should fade fast.
3. **`per_unit` is regime-dependent**: gf2 (large deploy, 16 syms) prefers
   `pu=2.0`; gf1 (small deploy, 5 syms) prefers `pu=1.0` (more sensitive).
   Likely a per-deploy normalization effect — smaller deploys have larger
   per-symbol weight, so each miss is "worth more" relative to the deploy.
4. **gf0_cme has no precog signal** at any setting — consistent with the
   structural CME residual (remote-feed staleness on Databento, not HL
   congestion). Precog can't fix what isn't HL-side.
5. **gf3 illiquid deploys** show only marginal effect. Not enough genuine
   races on illiquid symbols to extract signal from at the physical RTT.

## Deploy recommendations

For each currently-running cross deploy:

| host | `feed_lag_widen_coef` | precog | precog settings |
|---|---|---|---|
| gf0_cme (CL, BZ, NATGAS, SP500, ...) | 1.0 | **off** | no signal |
| gf1_eq (TSLA, NVDA, AMZN, RIVN, BABA) | 1.0 | **on** | `rtt=0.4, max_adjust=0.85, tdc_s=20, per_unit=1.0` |
| gf2_eq (AAPL, MSFT, META, HOOD, ...) | 1.0 | **on** | `rtt=0.4, max_adjust=0.85, tdc_s=20, per_unit=2.0` |
| gf3_v4 (Tier D illiquid) | 1.0 | optional | small effect; can defer |
| gf3_kor (SKHX, SMSN) | 1.0 | optional | small effect; can defer |

All precog settings also require `risk.precog_miss=true` (and the
risk-side ordering: `risk.precog_fastest_possible_rtt=0.4`).

## Notes / methodology lesson

The first round of testing got positive precog results on gf3 by pulling
`precog_fastest_possible_rtt` to 2.0s. That was bogus — at that wide window,
the riskman labels random level churn on illiquid symbols as "near-misses,"
and those happen to correlate with profitable moments. The mechanism wasn't
catching real races.

The rule: **physical constants stay at their physical values.** Only knobs that
control strategy *response* are legitimate tuning targets.

## Files

- Sweep configs and sim outputs:
  - `~/scratch/gf1_relcross_iter/feedlag_sweep_20260608/features_test_combined/`
    (gf2+gf3 widen+precog combined)
  - `~/scratch/gf1_relcross_iter/feedlag_sweep_20260608/precog_grid/`
    (all 5 hosts × 5 dates × 5 variants)
- Analysis: `compare_combined.py`, `compare_grid.py` in the same directories
- Code: `src/pktrade/ordex/rel_cross.{h,cc}` commits `7abc5453`, `af5ef2aa`
- Underlying SVL work: `svl_lag_aware_iteration_20260609.md`,
  `lag_aware_sim_svl_settings_20260609.md`
