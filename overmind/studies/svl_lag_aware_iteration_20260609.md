# Sim-vs-live (SVL) iteration — lag-aware sim — June 2026

Story of the SVL investigation that produced the `lag_aware_latency` sim model.
Companion to `lag_aware_sim_svl_settings_20260609.md` (the reference for
defaults).

## Symptom

Across the cross strategies that went live in early June 2026, sim was
producing roughly **2× live fillrate**. The fillrate gap held across most
symbols on most days, which made the live PnL persistently below sim
predictions and made it hard to size or kill symbols on sim evidence alone.

## Diagnosis

### Median is fine — tails aren't

Sim's `ioc_ord_latency = 1.1s` was already close to live IOC RTT median
(~970ms). The gap was driven by the **tail**: live RTT had p99 ~2.5s with
occasional 4-5 second spikes, and during those spikes our IOCs arrived stale
and got rejected. Sim modeled all orders with the constant — no adverse
selection during bursts → over-fills.

The tail events cluster heavily in time, not symbol. On 2026-06-01 alone, gf2
had **424 events with RTT > 3000ms**, of which 85-92% were `NewOrderReject`
("order arrived, but no match"). Most live spikes co-occurred at the same UTC
moment across hosts (gf2 + gf3 simultaneously), and recurring UTC times across
days (15:30 UTC, 17:30 UTC, etc.).

### The bottleneck is HL, not us

We had two suspects: HL is slow, or our outbound queue is slow.

Decisive evidence: at each identified spike window, HL's **book feed lag**
(`now − transact_t`) spiked synchronously across **every symbol observed** —
US equities (AAPL/META/HOOD), crypto (BTC), and Korean equities (SKHX/SMSN,
whose market was closed and live host had no orders going out). The numbers
matched within 1-3 ms across all of them.

That's three independent observation points (tardis collector, gf2 orders,
gf3 orders) seeing slowness at the same instant. Our outbound queue couldn't
explain Korean symbols slowing on a host with no traffic. It's HL.

Quiet days at the same wall-clock had no spike — these are event-driven, not
recurring schedule artifacts. Macro release days (ISM, JOLTS) were the worst.

### A separate bug — 60-second rejects after disconnect

Spike investigation surfaced a different pattern: **extreme** 60-second RTT
events that were *not* HL congestion. They tracked exactly with HL's periodic
`ConnectionClosedOK ("Expired", code 1000)` ~3-4h cycle. Our reconnect path
cleared the order/cancel queues but left entries in `pkcoids_to_timings` —
orders sent-but-unacked at disconnect time waited until the next periodic
`check_expired_orders` (up to 60s away) before being marked expired. Fixed
in commit `a63cad67` by sweeping expired orders immediately on reconnect.

## Model

### Iteration 1 — multiplier-only with floor

First model: `eff_ioc_delay = max(ioc_ord_latency_floor, mult × feed_lag)`.

Sweep on gf2 6/01 (worst spike day) over multiplier values 1.5-3.0 closed
70-75% of the SVL gap with one knob. Best fit at `mult = 3.0`: ratio 1.11×.

Issues:
- Floor was load-bearing in quiet regimes (mult × tiny_lag → tiny number).
- Single multiplier conflated network and congestion.
- Coupled with existing `ioc_ord_latency` field — hard to reason about
  independently.

### Iteration 2 — linear decomposition

Switched to `eff_<tif>_delay = base + coef × feed_lag`:
- `base_ms` — fixed network + minimum HL processing (always paid).
- `coef × feed_lag` — HL congestion that scales with observed feed lag.

Sweep on gf2 6/01 over `(base, coef)` grid converged on `nc=700, m=1.5` →
ratio **1.06×** on the worst spike day. Marginally better fit than the
floor model, but the *interpretability* win is the real reason to ship:
parameters are individually meaningful and can be retuned independently.

### Iteration 3 — naming

Renamed `ioc_lag_from_feed` → `lag_aware_latency` (the toggle isn't
IOC-specific), `ioc_lag_const_ms` → `ioc_lag_base_ms` (clearer intent), and
`ioc_lag_mult` → `ioc_lag_coef` (standard linear-model term, less
confusable with `size_mult`). Pattern `<tif>_lag_base_ms` / `<tif>_lag_coef`
sets up cleanly for ALO/cancel/GTC extension.

### Iteration 4 — extension to ALO + cancel

For MM strategies, IOC lag doesn't help directly. The relevant timings are:
- **ALO post**: time from "decide to post" to "order on book" — slow post
  means we miss the cross opportunity we were chasing.
- **Cancel**: time from "decide to cancel" to "order removed" — slow cancel
  means more time exposed to adverse selection.

Live RTT analysis across 4 MM strategies × 6 days:

| TIF | p50 | p99 |
|---|--:|--:|
| NewOrd→Ack (ALO) | ~530ms | ~1300ms |
| Cancel→Ack | ~530ms | ~1300ms |
| (vs. IOC) | ~970ms | ~2500ms |

ALO and cancel are essentially symmetric (HL treats add/remove identically),
and ~half of IOC because no matching is involved.

Sweep on `(alo=cxl base, coef)` across 4 strategies × 4 days × 7 variants:
best fit at `(base=100, coef=0.8)`.

`coef < 1` says HL's order-side ack scales slightly less than feed delivery
during bursts. Could be HL prioritizing order acks over feed publishes, or
sim's matching logic is already mildly aggressive and doesn't need the
symmetric scaling.

## Multi-day, multi-host validation

Cross strategies, `(ioc_base=700, ioc_coef=1.5)`, June 1-5 (5 days × 4 hosts):

| host | baseline gap | lag-aware gap |
|---|--:|--:|
| gf3 (Tier D) | 1.31× | **1.01×** |
| gf2 (16 US equities) | 1.56× | **1.17×** |
| gf1 (NVDA/TSLA/…) | 2.29× | 1.60× |
| gf0 (CL/BZ/CME-paired) | 2.31× | 1.80× |

MM strategies, `(alo/cxl base=100, coef=0.8)`, June 1-4 (3-4 days × 4 strats):

| host | baseline shs ratio | lag-aware shs ratio |
|---|--:|--:|
| gf3_usday | 1.05× | **1.02×** |
| gf2_usday | 1.16× | **1.10×** |
| gf0_davidsearch | 1.92× | **1.49×** |
| gf1_usday | 3.04× | 2.94× |

Direction is right on every host-date pair — no regressions.

## What lag-aware doesn't fix

Some residual gap remains on a few hosts. These are **not** lag-modeling
problems and need separate work:

- **Multi-host self-collision** (gf1): NVDA/PLTR/TSLA/AMZN/RIVN/BABA are
  co-deployed on cross strategies across gf1 + gf2 + gf3. Each sim sees full
  HL counterparty depth; live splits it across our own deploys. No latency
  model fixes this — only consolidation does.
- **Remote-feed staleness** (gf0 CME-paired): sim assumes the CME quote at
  `sim_now` is current. Databento → us has its own one-way delay. The
  CME-vs-HL speed differential means our orders are stale on arrival even
  with no HL congestion. Modeling remote-feed lag is a reasonable follow-up.
- **Symbol-specific microstructure** (HOOD): live fillrate is anomalously
  low (5%) and stuck at 2.3× even after lag-aware. Queue-priority loss or
  microstructure that no timing model captures.
- **6/05-style anomalies**: live fillrate dropped on gf1/gf2 with NewOrd
  volume up 50%. Strategy fired on smaller opportunities for some reason.
  Orthogonal to lag.

## What ships in this branch

- C++ — `src/pktrade/sim/sim_lvl_inst.{h,cc}`: `lag_aware_latency` flag,
  per-TIF `<tif>_lag_base_ms` / `<tif>_lag_coef` knobs (IOC + ALO + cancel),
  `last_feed_lag_ms_` heartbeat from any HL trade callback, optional BTC
  trade-only subscription so the lag estimate stays fresh in sparse-symbol
  windows. (Commits `c72ec9c0`, `89a40ee5`.)
- C++ — defaults set to the calibrated values, so a sim author can just turn
  on `lag_aware_latency` to get a reasonable out-of-box fit.
- Python — `wsgateway.py` fixes the 60-second-after-reconnect rejects.
  (Commit `a63cad67`.)
- Python — `sim_eval.py` glob fix for `gf?_*relcross*` dated configs.
  (Commit `96733163`.)

Reference for SVL settings to use in any new sim:
`lag_aware_sim_svl_settings_20260609.md`.

## Operational notes for the next iteration

When using lag-aware sim:

- Always include BTC in `extra_subs` of at least one pktrader so
  `last_feed_lag_ms` stays fresh in sparse-symbol windows. `md_exists.py`
  picks it up automatically from the dry-run subscription list.
- `lag_aware_latency` composes cleanly with `use_one_way_latency`. They're
  orthogonal: `use_one_way_latency` chooses dispatch mechanism (queue vs
  event-loop), `lag_aware_latency` chooses delay value (constant vs
  feed-derived).
- If sim diverges meaningfully from live after applying lag-aware, the cause
  is probably not lag — look at the four residual categories above.

When designing the next sim improvement:
- Modeling **remote-feed lag** (CME, equity TopBook) would close most of the
  remaining gf0 gap and probably improve gf1 too. Same architectural pattern
  as lag-aware HL: track per-source `now − transact_t`, plug into a per-TIF
  `eff_delay`.
- Modeling **cross-deploy self-collision** is structurally harder — needs
  awareness of other deploys' live orders, which sim doesn't have today.
- The `<tif>_lag_base_ms` / `<tif>_lag_coef` pattern extends to GTC trivially
  if/when needed.

## Source files

- C++: `src/pktrade/sim/sim_lvl_inst.{h,cc}`
- Sweep configs, sim outputs, analysis scripts: `~/scratch/gf1_relcross_iter/feedlag_sweep_20260608/`
- Underlying root-cause docs: `~/scratch/gf1_relcross_iter/allsym_sweep_20260522/`
  - `TAIL_RTT_EXPLORATION_20260608.md`
  - `HL_FEED_LAG_AT_SPIKES_20260608.md`
- Recommended SVL settings reference: `overmind/studies/lag_aware_sim_svl_settings_20260609.md`
