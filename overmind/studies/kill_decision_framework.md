# Kill / Size-Cut / Hold Decision Framework

Drafted 2026-07-15 after two near-misses (META and GOOGL) where
attribution and micro-structure flags said "disable" but recent hit
rate said "adapting successfully." This is the durable playbook to use
before recommending any disable / size cut on a currently-earning strat.

## The mistake this framework prevents

Fleet-review attribution and HL divergence flags are **backward-looking**
signals of stress. They can flash red while the *forward* PnL is
adapting successfully. Two 2026-07-15 examples:

- **META** on `usday_cov_0521`: fill_bp z=+4.7 (same magnitude as
  MSFT/AAPL killers) → but monthly hit rate 48% → 73% and July was
  the strat's best month. Killing META would have been wrong.
- **GOOGL** on `gf1/usday`: FAKE_WINNER attribution (own −$1,090
  via MTM proxy), Group B "quieter + thinner" HL fingerprint → but
  July hit rate 80%, monthly net **+$1,754** (best month in 105
  sessions). Was disabled Round 4; re-enabled after hit-rate check.

Fill_bp spike, HL divergence flags, and MTM attribution all worked as
*warnings*. But they aren't *verdicts* on their own.

## The framework

Before recommending any disable, run through these signals in order.
A "hold" answer at any layer stops the escalation.

### Layer 1: Recent forward-looking PnL (the loudest signal)

**Look at monthly hit rate + monthly PnL for the last 3 months.**

- **Best month is the current month** → HOLD. The strat is adapting or
  finding new edge. Never kill during a best month based on backward
  attribution alone. (This is the META / GOOGL rule.)
- **Hit rate trending up month-over-month** → HOLD. Signal of
  adaptation.
- **Hit rate declining month-over-month for 2+ months AND recent
  monthly PnL declining** → continue to Layer 2.
- **Recent month hit rate < 40% AND recent month PnL negative** →
  continue to Layer 2.

### Layer 2: Lifetime trajectory + drawdown

- **Lifetime PnL still positive AND drawdown from peak < 30%** → HOLD.
  Normal drawdown; don't overreact.
- **Lifetime PnL positive but drawdown 30-60%** → SIZE CUT candidate.
  Continue to Layer 3.
- **Lifetime PnL positive but drawdown > 60% AND no recovery in last
  10 sessions** → DISABLE candidate (like QNT: 77% drawdown).
- **Lifetime PnL near-zero or negative** → DISABLE candidate.

### Layer 3: Tail-loss frequency

For pairs at Layer 2, examine the *shape* of losses.

- **2+ single-day losses each ≥ 30% of peak in the last 10 sessions**
  → DISABLE / SIZE CUT. Tail risk is elevated (QNT −$593 + −$615
  pattern).
- **Losses are broad-distribution (many small losses, no huge days)**
  → SIZE CUT or HOLD. Not tail risk; probably signal decay.
- **Losses are all in one direction (always short-side, always
  long-side)** → DIAGNOSE first (signal or feed issue, not
  necessarily kill).

### Layer 4: Combined regime signals

Only *combined* signals justify disable at this point:

- HL micro-structure divergence (from `hl_divergence_monitor.py`)
- Fill_bp z-spike (from the same tool's fill-rate annotation)
- Attribution FAKE_WINNER / REAL_BLEEDER (from `fleet_review.py`)

Rule: at least **two** of these signals **AND** Layer 1-3 flags. Any
one signal alone is not sufficient to override a positive Layer 1.

## What backward signals *are* good for

Not disable decisions — but:

- **Watchlist**: pair flagged by 2+ tools moves to "monitor closely" list
- **Postmortem trigger**: run the daily timeline, investigate the "why"
- **Regime log entry**: record the fingerprint change even if we don't act

## What forward signals *are* good for

- **Disable**: only when Layers 1-3 all say the strat is genuinely losing
- **Size cut**: when Layer 1 is uncertain but Layer 2-3 show elevated
  tail risk

## Quick reference table

| Layer 1 (recent) | Layer 2 (lifetime) | Layer 3 (tail) | Action |
|---|---|---|---|
| Best month is current | any | any | **HOLD** (META/GOOGL rule) |
| Hit rate improving | any | any | **HOLD** |
| Steady hit rate | positive, <30% drawdown | none | **HOLD** |
| Declining | positive, 30-60% drawdown | occasional | **SIZE CUT** |
| Declining | positive, 30-60% drawdown | 2+ tail days | **SIZE CUT** or DISABLE |
| Declining | positive, >60% drawdown | 2+ tail days | **DISABLE** (like QNT) |
| Declining | near-zero or negative lifetime | any | **DISABLE** |
| Any | never earned (ALWAYS_BAD) | any | **DISABLE** — clean kill |

## Framework refinement note (2026-07-15)

Original Layer 2 said: `positive lifetime + <30% drawdown → HOLD`.
Retroactive check on NVDA (Feb-Jul curve; July -$454/d avg despite
only 19% drawdown) shows this rule is too permissive when Layer 1 has
clearly declined. Refinement: **Layer 2 defers to Layer 1 when Layer 1
has fired.** If recent monthly hit rate is worst month AND declining
across 2+ months, drawdown alone doesn't rescue.

NVDA disable was still defensible under the refined framework because:
- Multi-month hit rate decline (Feb 69% → Jul 36%)
- July worst month by any measure
- 2 consecutive big loss days (07-14, 07-15) at −$1,192, −$851

Contrast with GOOGL/AAPL where **current month was best month** — clear
Layer 1 hold.

## Meta-rule

**Attribution proxies (MTM inheritance) can systematically mislead in
some regimes.** Do not treat FAKE_WINNER as a disable trigger without
Layer 1-3 confirmation. Similarly, HL divergence flags and fill_bp
spikes are warnings — they justify a look, not a disable.

**Recent forward PnL wins.** If the strat is making money right now
in an adapted regime, that's the reality; the backward attribution is
noise about how we got here, not signal about where we're going.
