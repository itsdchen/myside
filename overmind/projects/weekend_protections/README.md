# Weekend MM Protections

Five opt-in mechanisms that defend WideMM-family ordexes against the kinds of
book manipulation seen on Hyperliquid weekend sessions. Originally motivated by
the HYUNDAI (2026-04-10) and USAR (2026-04-11) attacks, where adversaries
flashed phantom levels to bait quote moves and then swept across them.

All knobs default to off so existing strategies are unaffected. Enable via the
trader's `ordex` and `risk` blocks in the strategy config.

## Background incidents

- **HYUNDAI**, sim date 20260411: ~$200 loss over 977 trades. Position jumped
  to +3.84 mid-session via `global_inherit`, then forced exit into a falling
  market while alpha said buy.
- **USAR**, sim date 20260412: ~$600 loss over 4119 trades, position swung
  +39 / -16. Post-fill mid move alone was -$1193.

In both, the immediate loss driver was forced exit into adverse direction; book
manipulation amplified rather than caused the loss. The detector and structural
protections below address both the amplifier and the displayed-book trust
problem.

## Protection stack

### 1. Bleed detector (`risk.bleed_detector`)

`src/pktrade/risk/trade_risk_man.{h,cc}`. Maintains an EMS of win/loss
indicators on position closes, normalized to `[-1, 1]`. When score crosses
`bleed_score_widen`, scale `thresh_mult` by `bleed_widen_thresh_mult`. When it
crosses `bleed_score_stop`, set `TradingLevel::TL5` (hard block). Recovery via
`bleedRecovery()` after `bleed_stop_timeout` seconds (default 1800).

On recovery, both thresholds halve — escalating: 1st stop at `-0.8`, 2nd at
`-0.4`, 3rd at `-0.2`, 4th at `-0.1`. If escalation drives the stop threshold
low enough, a single bad trade can end the day.

```json
"risk": {
  "bleed_detector": {
    "tdc": 10,
    "deadzone_thresh": 0.0001,
    "bleed_score_widen": -0.6,
    "bleed_score_stop": -0.8,
    "bleed_widen_thresh_mult": 2.0,
    "bleed_stop_timeout": 1800
  }
}
```

Live-validated 2026-04-17: fired on HYUNDAI 3 times and USAR once at
`size_mult=0.125`. Final losses $47 / $41 vs prior week's $200 / $600 at
similar exposure without it.

### 2. `adding_no_cutin` (default true)

In `WideMM`, `AlphaRelWideMM`, `RelWideMM2`. Don't place below an existing
top-of-book bid (or above a top-of-book ask) including our own resting orders.
Partial protection against the self-chase spiral where each placement makes us
the new most-aggressive level for the next sweep.

### 3. Backlevel sparsity (`min_foreign_inside_mult`)

In `manageBacklevels` for all three ordexes. Before placing a new backlevel,
walk the book between the previous same-side order price and the proposed
backlevel price. Sum foreign liquidity (`effectiveQty - own resting`) at each
level. Require cumulative foreign size ≥ `min_foreign_inside_mult *
inside_size_<side>_ems`. Skip placement if the book can't supply it.

Self-scales across thick and thin books because the threshold is a multiple of
the symbol's own typical inside depth. A 1-share phantom on a thin book and a
1000-share phantom on a thick book both fail to satisfy a `mult=2` threshold
relative to that book's characteristic depth.

Helper: `protection::sparse{Buy,Sell}BacklevelPx` in
`src/pktrade/ordex/protection_helpers.h`.

### 4. Local-liquidity size cap (`local_size_cap_mult`, `local_size_cap_tdc_ms`)

In `maybeJoin` / `maybePlaceFront` only — backlevels keep their natural size
since sparsity is their protection. Cap the front order qty at
`local_size_cap_mult * inside_size_<side>_ems`. When the inside thins out, our
front order shrinks proportionally and we're not the disproportionately large
target for a sweep.

`protection::capBySize` floors at the symbol's minimum tradeable notional, so
the cap shrinks the order but never reduces it to zero (which would cause us
to skip placing entirely).

### 5. Exec-anchored mid (`exec_anchored_mid_tdc_ms`, WideMM only)

In `WideMM::tryFire`. Maintain two EMAs over public trade prints: one of
`px*qty` and one of `qty`, both with the same time constant. Their ratio is a
size-weighted exponential VWAP of recent prints (`exec_vwap`).

At fire time, blend per side, picking the less-favorable-to-us reference:

```
blend_mid_buy  = min(exec_vwap, mid)
blend_mid_sell = max(exec_vwap, mid)
pred_px_buy    = blend_mid_buy  * (1 + alpha)
pred_px_sell   = blend_mid_sell * (1 + alpha)
```

A phantom can move the displayed mid but cannot fake a trade print, so this
blocks displayed-book moves from dragging `pred_px` until a real trade
confirms them. The asymmetric blend means protection is one-sided per attack
direction (when the attack drops mid, only sell-side is protected; when it
lifts mid, only buy-side is). That's correct for the forced-exit failure mode.

Not mirrored into AlphaRelWideMM or RelWideMM2 — those classes have their own
relative-pricing logic that would interact non-trivially with the blend.

## Sided inside-size EMAs

`updateInsideSizeEms` in `protection_helpers.h` maintains separate EMAs for
the buy and sell sides of the book, each fed only by its own side. Member vars
on each ordex: `inside_size_buy_ems_` / `inside_size_buy_last_t_` and
`inside_size_sell_ems_` / `inside_size_sell_last_t_`. `getEffectiveOrderSize`
takes a `Side` argument and consults the matching EMA.

This handles asymmetric attacks (e.g., adversary dumps the bid side only)
where a combined EMA would be polluted by the unaffected side.

Each side is also re-checked after a front order is placed: if the inside
thins meaningfully on that side after placement, the front order is canceled.

## Shared helper

`src/pktrade/ordex/protection_helpers.h` (header-only, in
`pktrade::ordex::protection` namespace):

- `ownQtyAt` — sum own non-canceled resting qty at a given price.
- `sparseBuyBacklevelPx` / `sparseSellBacklevelPx` — backlevel walk + check.
- `updateInsideSizeEms` — sided foreign-depth EMAs.
- `capBySize` — `min(input, cap_mult * ems)` floored at min tradeable.
- `updateExecVwapEms` — discounted-sum EMAs for trade VWAP.

## Recommended starting defaults

Tune per-symbol from there.

```json
"ordex": {
  "min_foreign_inside_mult":  2.0,
  "local_size_cap_mult":      3.0,
  "local_size_cap_tdc_ms":    60000,
  "exec_anchored_mid_tdc_ms": 180000
}
```

`exec_anchored_mid_tdc_ms` of 180s was the PnL-best in a `[30s, 60s, 180s,
600s, 1800s]` sweep and not very tdc-sensitive past that.

## Sim observations

(From sweep at the merge commit, before the sided-EMA refactor — directional
takeaways still apply but exact numbers will have shifted.)

- Sparse and cap engage strongly on thin books (HYUNDAI: ~50% reduction in
  order count) and barely affect thick books (USAR, AAPL).
- Exec-anchored mid is a net PnL win in sim across both attack-prone and
  benign symbols, but doubles order count on symbols where exec_vwap sits
  persistently on one side of mid (USAR-like). Watch rate-limit headroom on
  those.
- Across the universe, protection is a **net cost** on benign weekends —
  treat it as insurance whose payoff shows up only on attack days, which the
  baseline sim doesn't faithfully reproduce (own orders contaminate the
  replayed book; sim has no self-chase spiral).

## Open considerations

- `global_inherit` dumping oversized positions into a session is still
  unaddressed — it was the underlying loss driver in both validated incidents.
- Sim-vs-live fidelity gap (replay book contains our own historical orders;
  sim doesn't reproduce adversarial response) means the bleed detector is the
  only mechanism with a credible measured payoff so far. The structural
  protections look correct in design but their attack-day value is inferred,
  not measured.
- Adversary economics at larger sizes are unknown — if we scale up, the
  escalating bleed thresholds may not be enough deterrent.
