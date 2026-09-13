# HIP3 Degradation Working Notes

## Summary

This note captures the current findings from the HIP3 strategy degradation
debugging work. The main actionable result is a configurable spread-aware
placement threshold:

```text
place_thresh_mode = 2
effective_place_thresh = place_thresh + place_thresh_spread_coef * local_spread_ema
```

This should be treated as a configurable strategy mechanism, not a universal
config migration. The sim evidence says it can help in multiple contexts, but
the best coefficient and even whether to opt in varies by route.

## Code/Config Direction

- Add or keep `place_thresh_spread_coef` as an explicit config field for
  `AlphaRelWideMM`, `RelWideMM2`, and `WideMM`.
- Preserve existing behavior by default:
  - `place_thresh_mode` defaults to `0`.
  - Existing hardcoded mode-1 behavior is preserved by defaulting
    `place_thresh_spread_coef` to `0.5`.
- `RelWideMM2` needs a dedicated local spread EMA for mode 2 because it did not
  already have `AlphaRelWideMM`'s `lspread_ema_`.
- `RelWideMM2` default `place_thresh_spread_ema_tdc_s` is `120`, matching the
  alpha spread EMA convention.
- `alpha_relwide_autosearch.py` should include `place_thresh_mode=2`.

## Main AlphaRelWide Result

Route:

```text
/home/pktrade/scratch/tradeperf/gf2/combined_equities_bfx2/usday_alphacov_0415/pk_alpharel_gf2_20260415.json
```

Replay window:

```text
20260415, 20260416, 20260417, 20260420, 20260421, 20260422
```

Baseline versus refined active-symbol mode-2 config:

| Variant | PnL | Delta PnL | New order delta | Trades |
|---|---:|---:|---:|---:|
| Baseline | `2195.74` | `0` | `0` | `4233` |
| Mode 2 active refined | `5590.98` | `+3395.24` | `-52478` | `3306` |

The refined mode-2 active config improved all six replay days and cut new
orders every day. This is the best current "one mode across symbols" candidate
for live/shadow testing.

Current active refined table:

| Symbol | `place_thresh_mode` | `place_thresh` |
|---|---:|---:|
| `AAPL` | 2 | `0.00024` |
| `AMD` | 2 | `0.00024` |
| `CRWV` | 2 | `0.00014` |
| `HOOD` | 2 | `0.00012` |
| `INTC` | 2 | `0.00030` |
| `LLY` | 2 | `0.00020` |
| `META` | 2 | `0.00016` |
| `MU` | 2 | `0.00018` |
| `NFLX` | 2 | `0.00028` |
| `ORCL` | 2 | `0.00028` |
| `SNDK` | 2 | `0.00026` |
| `TSM` | 2 | `0.00012` |

## Coefficient Search

Using the refined mode-2 threshold table above, an explicit coefficient sweep
found:

| Coef | PnL | Delta PnL | New order delta | Trades |
|---:|---:|---:|---:|---:|
| `0.375` | `5656.96` | `+3461.22` | `-91408` | `4154` |
| `0.500` | `5590.98` | `+3395.24` | `-52478` | `3306` |
| `0.250` | `5378.78` | `+3183.04` | `-135818` | `5323` |
| `0.625` | `4169.46` | `+1973.72` | `-31911` | `2706` |
| `1.000` | `3854.28` | `+1658.54` | `-9391` | `1771` |

Read:

- `0.375` was the best tested coefficient for this specific gf2 alpha candidate.
- The peak is sharp enough that this should not become a global default.
- Keep the code/template default at `0.5`; override per config only when sims
  justify it.

## Broader Route Check

Purpose: verify the mechanism can be useful in more than one context, while
preserving each route's existing `place_thresh`.

Grid:

```text
baseline
mode2 coef = 0.25, 0.375, 0.5, 0.625
```

Complete-window results:

| Route | Best tested variant | Baseline PnL | Best PnL | Delta PnL | New order delta | Read |
|---|---|---:|---:|---:|---:|---|
| `gf2_usday_cov0313` | baseline | `1638.86` | `1503.97` | `-134.89` | `-17621` | Do not opt in from this test. |
| `gf2_usday_cov0330` | `mode2_coef_0375` | `673.03` | `726.54` | `+53.51` | `-4474` | Small positive. |
| `gf2_usday` | baseline | `1283.23` | `920.13` | `-363.10` | `+4549` | Do not opt in from this test. |
| `gf1_usday` | `mode2_coef_0375` | `277.76` | `1368.09` | `+1090.33` | `-4993` | Strong positive. |
| `gf0_alpharel_many_v3` | `mode2_coef_0250` | `4755.54` | `4947.34` | `+191.80` | `-5998` | Small positive. |
| `gf0_alpharel_many_v6` | `mode2_coef_0500` | `232.57` | `3521.58` | `+3289.01` | `-14715` | Strong but noisy. |

Read:

- This is enough evidence to proceed with the mechanism.
- It is not enough evidence to apply one coefficient everywhere.
- Some routes prefer baseline.
- Positive results appeared in both `AlphaRelWideMM` and `RelWideMM2` contexts.

Output locations:

```text
/tmp/hip3_selected_route_coef_sweep_complete_v1
/tmp/hip3_selected_route_coef_sweep_v1/gf2_usday
```

## What We Did Not Prove

- We did not find a strong markout-driven live decision rule.
- The current useful result is mostly static thresholding: mode choice,
  coefficient choice, and per-symbol absolute `place_thresh`.
- Any dynamic markout rule should be causal and based only on sampled historical
  markouts available before the decision point.

## Rollout Guidance

- Ship the code/config mechanism with conservative defaults.
- Do not globally flip all strategies to mode 2.
- Use mode 2 selectively where route-level sims support it.
- For the primary gf2 alpha candidate, the refined active-symbol mode-2 table is
  the strongest current live/shadow candidate.
- For other routes, rerun a short local grid before opting in.
