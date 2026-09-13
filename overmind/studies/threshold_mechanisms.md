# Threshold-widening mechanisms reference

Reference for the threshold-affecting mechanisms in `AlphaRelWideMM` and
the shared `VolMechanisms` class (also composed by `RelWideMM2`,
`RelCross`, `WideMM`).

This is a working reference — formulas, configs, knob meanings, and
empirical-validation status. Original investigation under
`~/scratch/heavy_spikes_0506/cl_alpha/` (CL spike-defense study).

## Source files

- `src/pktrade/ordex/vol_mechanisms.h` — shared class composed by
  multiple ordex types. Mechanisms 1–6.
- `src/pktrade/ordex/alpha_rel_wide_mm.cc` — `AlphaRelWideMM` specific
  branches (`pred_momentum`, `dynamic_mid_sigmoid`, `remote_return_widen`,
  `curv_impulse`).

## Background — translating an EMS to widening

Mechanisms compute a signal (EMS of returns, EMS of trade-depth, etc.)
and translate it to a place-threshold adjustment in dollars. A few
observations:

- **Sum-form EMS** (`ems = x + decay·ems`) has steady-state magnitude
  that scales with `τ`. Coefficient tuning is τ-dependent — change τ,
  retune coef.
- **Mean-form EMA** (`ema = α·x + (1-α)·ema`) is bounded by typical-input
  magnitude, τ-invariant. Coef has interpretable units.
- **σ-per-√s framing**: for return-based signals, `σ_per_√s = √(E[ret²]/Δt)`
  is invariant to sampling/τ choice. Implied move over horizon `T` is
  `σ_per_√s · √T`. Lets coef = "stdevs of safety margin over T".
- **Trade-depth is event-based, not return-based** — depth grows linearly
  with rate × magnitude, not √T. So horizon framing doesn't apply.

## Mechanisms in `VolMechanisms`

### Mechanism 1 — `vol_widen` (Remote vol EMS)

Sum-form EMS of `|remote_return|`, applied as either additive or
multiplicative widening on both sides.

```json
"vol_widen_coef": 0,
"vol_widen_tdc": 4,
"vol_widen_scale": 0.001,
"vol_widen_additive": false
```

Update (on remote signal change):
```
abs_ret = |new_remote/old_remote − 1|
remote_vol_ems = abs_ret + exp(−Δt/τ)·remote_vol_ems
```

Apply:
- Additive: `widen = coef · ems · pred_px`
- Multiplicative: `widen = coef · ems/(ems+scale) · cur_thresh_px`

Notes: same shape as `vol_ems_remote_*` from DiagnosticsMM (just at single
τ). Disabled in cl_alpha; cl_alpha uses `dynamic_mid_sigmoid` instead.

### Mechanism 2 — `vol_norm` (Normalized vol)

Sum-form EMS of `ret²`, then `√(var_ems / τ)` ≈ σ-per-√s.

```json
"vol_norm_coef": 0,
"vol_norm_tdc": 15
```

Apply: `widen = coef · √(var_ems / τ_s) · pred_px`

Notes: σ-per-√s structure but no explicit horizon. Mechanism 5 is the
explicit-horizon refinement.

### Mechanism 3 — `vol_ratio` (Short/long vol ratio)

Captures regime change rather than absolute level.

```json
"vol_ratio_coef": 0,
"vol_ratio_short_tdc": 5,
"vol_ratio_long_tdc": 60
```

Apply: `widen = coef · (short_ems / long_ems) · pred_px`

### Mechanism 4 — `liq_depth` (Sided liquidity depth)

Sided — buy and sell can widen by different amounts.

```json
"liq_depth_coef": 0,
"liq_depth_notional": 200000,
"liq_depth_tdc": 30
```

Update on book changes: walks `BuySide` / `SellSide` levels until
accumulated notional reaches target, returns relative-distance EMS.

Apply: `buy_widen = coef · liq_depth_buy_ems · pred_px` (and sell).

### Mechanism 5 — `vol_implied` (Implied-horizon vol)

σ-per-√s with explicit horizon; coef has clean physical meaning. Adds
shape × reference modes (orthogonal config).

```json
"vol_implied_coef": 0,
"vol_implied_tdc_s": 30,            // smoothing window for σ estimate
"vol_implied_horizon_s": 2,         // exposure window we protect against

// shape (default linear, which is ramp with thr=0)
"vol_implied_shape": "linear",      // | "ramp" | "sigmoid"
"vol_implied_shape_thr": 0,         // ramp/sigmoid threshold
"vol_implied_shape_scale": 1.0,     // sigmoid steepness
"vol_implied_shape_max": 1.0,       // sigmoid asymptote

// reference (default absolute)
"vol_implied_reference": "absolute",   // | "zscore" | "spread_normalized"
"vol_implied_baseline_tdc_s": 600      // for zscore: long window
```

Pipeline:
```
σ_per_√s = √(var_ems / τ_s)

x =
  σ_per_√s                                          (absolute)
  (σ_per_√s − rolling_mean) / rolling_std           (zscore — unitless)
  (σ_per_√s · √horizon · pred_px) / spread_ema      (spread_normalized — unitless)

y =
  x                                                  (linear)
  max(0, x − thr)                                    (ramp)
  shape_max / (1 + exp(−(x − thr)/scale))            (sigmoid)

dollar_unit =
  √horizon · pred_px                                 (absolute)
  rolling_std · √horizon · pred_px                   (zscore)
  spread_ema                                         (spread_normalized)

widen = coef · y · dollar_unit
```

**Coef interpretation by mode:**
- absolute: "implied-horizon stdevs of safety margin"
- zscore: "z-units above baseline, scaled to baseline-stdev of horizon-move"
- spread_normalized: "spreads of widening per unit of (depth-in-spreads)"

**Why the three knobs are independent:**
- `tdc_s` controls *only* the σ-estimate smoothness/responsiveness — does
  NOT affect widening magnitude (since `var_ems / tdc_s` is approximately
  tdc-invariant in steady state).
- `horizon_s` controls *only* the exposure window. Bigger horizon →
  bigger widening (vol scales as `√T`).
- `coef` controls *only* the safety-margin multiplier (in stdevs of the
  implied horizon-move).
- Each knob does one thing. Change `tdc` and you don't have to retune
  `coef`.

**`horizon_s` default rationale**: should approximate the time an
existing quote is at risk of being filled. On Hyperliquid: `place_rtt
(~500ms) + min_ord_lifetime (~500ms) + cancel_rtt (~500ms) ≈ 1.5–2s`.
Default 2s.

**Spread_normalized** requires the caller to pass `spread_ema` to
`getVolWidenPx`. AlphaRelWideMM passes `lspread_ema_`; other ordex types
default to 0 → spread_normalized disabled there.

**Empirical validation status (CL, 2026-05-10):**
- **Absolute reference + ramp shape**: small lift over baseline +
  variant-44 stack. Best `coef=2, shape_thr=0.001` (= the σ_per_√s
  spike boundary). +1% pnl, +3% sharpe.
- **Z-score reference**: does NOT add value on top of variant 44 on CL.
  Top variants in z-score sweep all have `coef=0`. Code retained for
  potential cross-symbol value (other symbols may have less stationary
  vol regimes).
- **Spread_normalized reference**: untested.
- **Sigmoid shape**: untested.

### Mechanism 6 — `trade_depth` (Trade-depth widening)

Sum-form EMS of `depth_rel = max(0, |opposite_best − trade_px|) / mid`
per market-trade event. The signal that scored 5–60 cross-symbol in the
heavy_spikes_0506 DiagnosticsMM validation.

```json
"trade_depth_coef": 0,
"trade_depth_tdc_s": 30,            // matches DiagnosticsMM-validated signal
"trade_depth_ramp_thr": 0,          // 0 = linear, > 0 = ramp
"trade_depth_reference": "absolute" // | "spread_normalized"
```

Update (per market-trade event):
```
depth_rel = (passive_side==Buy)
            ? max(0, best_bid − trade_px) / mid
            : max(0, trade_px − best_ask) / mid
trade_depth_ems = depth_rel + exp(−Δt/τ)·trade_depth_ems
```

Apply:
```
x =
  ems                                                  (absolute)
  (ems · pred_px) / spread_ema                         (spread_normalized)

y = max(0, x − thr)                                    (ramp; thr=0 → linear)

dollar_unit =
  pred_px                                              (absolute)
  spread_ema                                           (spread_normalized)

widen = coef · y · dollar_unit                         (symmetric)
```

**Coef interpretation:**
- absolute: "how many recent crossing-depths to widen by"; coef=1 = "match
  the depth in dollars"; coef=2 = "double-margin"
- spread_normalized: "spreads of widening per unit of depth-in-spreads"

**Wiring**: callers must call `vol_mechs_.onTrade(bk, trd)` from their
own `onTrade`. The `subscribeData` Trd-callback condition is
`vol_mechs_.needsTradeCallbacks() = hasLiqDepth() || hasTradeDepth()`.
Wired in AlphaRelWideMM only at present.

**Empirical validation (CL, 2026-05-09):** at `coef=0.025, ramp_thr=0.075`
combined with `pred_momentum` and tightened `place_thresh`, eliminates
spike+runover episodes from 13 → 0 in the heavy_spikes_0506 testbed.
This is the structural defensive win.

## Mechanisms in `AlphaRelWideMM` proper

### `pred_momentum`

Signed sum-form EMS of `pred_px` returns. One-sided widening on the side
the prediction says is in danger.

```json
"pred_momentum_coef": 0,
"pred_momentum_tdc": 30
```

Apply:
- if `ems > 0`: `place_thresh_sell += ems · coef · pred_px`
- if `ems < 0`: `place_thresh_buy += |ems| · coef · pred_px`

**Best CL setting**: `coef=0.5, tdc=4`. Reduces spike+runover episodes
from 13 → 9 alone; eliminates them entirely when combined with
`trade_depth`.

Same sum-form τ-magnitude entanglement as Mech 1 — coef is jointly
tuned with τ. Refactor candidate (mean-form + drift-horizon framing,
linear in T not √T because it's signed/directional).

### `dynamic_mid_sigmoid`

Saturating widening on absolute remote-mid moves. Toggle:
`thresh_mid_sigmoid_scale`.

```json
"thresh_mid_sigmoid_scale": false,    // master toggle
"dynamic_mid_sigmoid_style": 2,       // log shape
"dynamic_mid_sigmoid_base": 0.01,
"dynamic_mid_sigmoid_offset": 0.5,
"dynamic_mid_sigmoid_mult": 1
```

Apply: widening multiplier on `place_thresh` based on
`abs_remote_mid_move_ems_` shape.

**Important**: counterintuitive name — actually TIGHTENS thresholds in
volatile regimes (pulls `modifier` toward `lower_bound < 1` when vol is
high). Disabled in production cl_alpha. Confirmed empirically to hurt
pnl/sharpe when enabled.

### `remote_return_widen` (Branch 7)

Symmetric, signed widening based on `remote_return_ems_` direction.

```json
"remote_return_widen_coef": 0
```

Disabled by default; not in cl_alpha.

### `curv_impulse` (Branch 6)

Self-braking on recent fills (signed by side). Adds `qty/order_size` per
exec, decays over `curv_impulse_tdc`.

```json
"curv_impulse_coef": 0,
"curv_impulse_tdc": 1
```

**Important (CL)**: empirically *hurts* CL specifically. Confirmed
2026-05-06 from prior sweep results. Disabled in cl_alpha.

## Currently-active mechanisms in cl_alpha pk.json (production-ish)

- `place_thresh = 0.0004` (4 bps base) + `place_thresh_mode=1` (= base +
  0.5·spread)
- `cancel_buffer_frac = 0.1`, `per_order_widen_frac = 0.2`
- `pcurvicity = 1, pcurvicity_coef = 0.5` (position-driven widening; in
  alpha_rel_wide_mm.cc, separate from VolMechanisms)
- `alpha_mult = 0.9` (mid shift on pred, not widen)
- All else off (`pred_momentum=0`, `curv_impulse=0`, `vol_widen=0`,
  `vol_norm=0`, `vol_ratio=0`, `liq_depth=0`, `vol_implied=0`,
  `trade_depth=0`, `dynamic_mid_sigmoid_scale=false`,
  `narrow_when_few_trades=false`, etc.)

## Best-known CL config (heavy_spikes_0506 contour-sweep variant 44 / abs-sweep variant 341)

Adds these on top of the cl_alpha base:

```json
"place_thresh": 0.000275,             // tightened from 4.0 → 2.75 bps

"pred_momentum_coef": 0.5,
"pred_momentum_tdc": 4,

"trade_depth_coef": 0.025,
"trade_depth_tdc_s": 30,
"trade_depth_ramp_thr": 0.075,
"trade_depth_reference": "absolute",

// Optional polish (variant 341, +1% pnl / +3% sharpe over variant 44):
"vol_implied_coef": 2.0,
"vol_implied_tdc_s": 30,
"vol_implied_horizon_s": 2,
"vol_implied_shape": "ramp",
"vol_implied_shape_thr": 0.001,
"vol_implied_reference": "absolute"
```

vs cl_alpha baseline (in-sample, 18 sim days):
- pnl 159 → **231–236** (+45–48%)
- sharpe 0.94 → **1.39–1.43** (+48–52%)
- spike+runover episodes 13 → **0**
- num_trds 110 → 142

## Lessons learned

1. **Composition matters more than any single mechanism.** The +45% pnl
   improvement is mostly from composing `pred_momentum` + `trade_depth`
   + tighter `place_thresh`. No individual mechanism alone gets close.
2. **trade_depth is the structural win.** It eliminates spike+runover
   episodes (13 → 0). Other mechanisms add marginal lift on top but
   don't replace this structural property.
3. **place_thresh is jointly tuned with the defenses.** In isolation,
   4.0 bps was sharpe-optimal. With `pm + td` defenses, 2.75 bps is
   better — fire ~2× more trades while the defenses keep them clean.
4. **Sum-form EMS magnitude scales with τ.** Same coef gives different
   widening at different τ — keep this in mind when tuning or porting.
5. **vol_implied's reference choice matters.** Absolute reference adds
   marginal value on CL; z-score reference adds none. Other symbols may
   differ.
6. **`dynamic_mid_sigmoid` is a misnomer** — TIGHTENS thresholds in
   volatile regimes (counterintuitive). Confirmed off in production.
7. **Calibration sweeps before big sweeps.** A 10-variant magnitude
   check correctly identified that `z_thr=5` was too high for ramp
   gating in z-score mode (z>5 events very rare). Saved a wasted full
   sweep at the wrong threshold range.

## Cross-symbol findings (DiagnosticsMM validation, sub-bar onset)

For 1-min tail-bar spike onsets, top-0.1% per-symbol, score = `|onset_med
− base_med| / base_IQR`:

| signal                  | CL    | COPPER | GOLD  | NATGAS | SILVER |
|-------------------------|-------|--------|-------|--------|--------|
| `trade_depth_ems`       | 60.6  | 5.1    | 56.0  | 12.1   | 13.7   |
| `trade_notional_abs_ems`| 19.3  | 10.3   | 16.4  | 3.3    | 6.9    |
| `vol_ems_remote_4s`     | 16.3  | 3.5    | 0.0   | 2.0    | 0.0    |
| `momentum_4s`           | 4.2   | 3.6    | n/a   | 0.0    | n/a    |

`trade_depth_ems` is the universal winner across all five symbols.
Vol-on-remote signals are blind on GOLD/SILVER (their spikes are
disconnected from CME futures). `momentum_*` is the weakest across the
board.

## References

- Investigation working dir: `~/scratch/heavy_spikes_0506/cl_alpha/`
  - `SWEEP_RESULTS.md` — full per-sweep results
  - `MECHANISMS.md` — same content as this file (working version)
  - `diagnostics_smoke/FINDINGS.md` — DiagnosticsMM signal-discrimination
  - `diagnostics_multi/` — multi-symbol DiagnosticsMM data
  - `<sweep>/` subdirs — per-sweep specs and results
- Code:
  - `src/pktrade/ordex/vol_mechanisms.h`
  - `src/pktrade/ordex/alpha_rel_wide_mm.{h,cc}`
  - `src/pktrade/ordex/diagnostics_mm.{h,cc}` (research-instrumented for this study)
- Tools:
  - `pybin/SymDiag.py` (with `periodic_dump_ms` instrumentation added 2026-05-07)
  - `pybin/alpha_relwide_autosearch.py` (the sweep runner)
