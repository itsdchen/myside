# Lag-aware sim — recommended SVL settings — 2026-06-09

Reference for any HL strategy sim you want to be calibrated against the live
sim-vs-live gap we measured during the June 2026 cross / MM investigation.

## When to use

Turn this on whenever you want sim PnL / fillrate to be a believable proxy for
live behavior. Especially valuable for:

- IOC-heavy strategies (RelCross and similar) — the **largest** sim-vs-live gap
  was here.
- MM strategies (`RelWideMM2`) with frequent post + cancel — moderate but
  consistent improvement.
- Any backtest where the conclusion depends on fillrate (e.g., capacity sweeps,
  size_mult selection, sym-on/off decisions).

Leave off if you specifically want the legacy fixed-latency behavior or you're
testing pure execution code paths.

## Recommended defaults

Add to `simulation.Hyperliquid`:

```json
"lag_aware_latency": true,
"ioc_lag_base_ms": 700,
"ioc_lag_coef": 1.5,
"alo_lag_base_ms": 100,
"alo_lag_coef": 0.8,
"cxl_lag_base_ms": 100,
"cxl_lag_coef": 0.8
```

Add BTC as an aux subscription on every pktrader (or any single one — the set
dedupes):

```json
"extra_subs": [{"market": "Hyperliquid", "symbol": "BTC"}]
```

`md_exists.py --pk <conf>` picks up BTC from the `pktrade --dry-run` output,
so no separate data-staging is needed.

## Why these numbers

### Live RTT calibration (June 1-8, 2026)

| order type | p50 | p99 |
|---|--:|--:|
| IOC (cross) | ~970ms | ~2500ms |
| ALO (post) | ~530ms | ~1300ms |
| Cancel | ~530ms | ~1300ms |

HL feed lag p50 baseline: ~485ms (p99 ~1170ms).

### Decomposition

The model says `eff_<tif>_delay = base + coef × feed_lag`:

- **`ioc_lag_base_ms = 700`** — fixed network round-trip + HL match-engine
  floor (HL is a blockchain, ~200-400ms block time, plus our network).
- **`ioc_lag_coef = 1.5`** — HL's order-side congestion scales 1.5× the
  observed feed-side lag, so at baseline `700 + 1.5 × 485 ≈ 1430ms` (slightly
  conservative vs live median ~970ms — captures the tail-driven fill
  suppression).
- **`alo_lag_base_ms = 100`, `alo_lag_coef = 0.8`** — ALO ack is fast (no
  matching work), and during bursts HL's order acks scale a bit *less* than
  feed delivery does. At baseline `100 + 0.8 × 485 ≈ 488ms` (matches live
  ~530ms within ~10%).
- **`cxl_lag_base_ms = 100`, `cxl_lag_coef = 0.8`** — symmetric with ALO, since
  HL treats add/remove identically in the data.

### Burst behavior

During the worst spike windows we observed (HL feed lag jumping to ~3800ms p50
for ~50 sec, e.g., 2026-06-01 15:30:30 UTC):

| TIF | effective delay during burst |
|---|--:|
| IOC | 700 + 1.5 × 3800 = **6400ms** |
| ALO | 100 + 0.8 × 3800 = **3140ms** |
| Cancel | 100 + 0.8 × 3800 = **3140ms** |

These match observed live behavior — order RTT spikes 3-5s, cancel acks slow
proportionally — and produce the adverse-selection / reject patterns we see
live.

## SVL fit quality

Calibration on 6/01-6/05, 4 hosts × 2 strategy classes:

### RelCross (IOC)

| host | baseline gap | lag-aware gap | residual cause |
|---|--:|--:|---|
| gf3 | 1.31× | **1.01×** | — clean fit |
| gf2 | 1.56× | **1.17×** | mostly closed |
| gf1 | 2.29× | 1.60× | multi-host self-collision |
| gf0 | 2.31× | 1.80× | CME remote-feed staleness, illiquid HL spreads |

### MM (ALO + cancel)

| host | baseline gap (shs ratio) | lag-aware gap |
|---|--:|--:|
| gf3_usday | 1.05× | **1.02×** |
| gf2_usday | 1.16× | **1.10×** |
| gf0_davidsearch | 1.92× | **1.49×** |
| gf1_usday | 3.04× | 2.94× (structural — same multi-host issue) |

Excluding gf1, mean sim-vs-live miss drops ~48%. Direction is always right;
no regressions on any host-date pair.

## When *not* to retune

These knobs were calibrated against actual observed HL behavior and shouldn't
be casually changed per-strategy. If sim still differs meaningfully from live
after applying these, the cause is probably **not** lag-aware modeling — look
at:

1. **Multi-host self-collision** (gf1-style): same symbol deployed in multiple
   places we don't track in any one sim. Sim sees full HL counterparty; live
   has it split across our deploys.
2. **Remote-feed staleness** (gf0 CME-paired): sim assumes CME quote at
   `sim_now` is current. Databento → us has its own one-way delay. Not modeled
   in sim today.
3. **Symbol-specific microstructure** (HOOD has the gf2 cross residual):
   queue priority loss, wide HL spreads, counterparty churn. Not lag-fixable.
4. **6/05-style anomalies**: live fillrate dropped abnormally on gf1/gf2 with
   NewOrd volume up 50%. Strategy-side dynamics, not HL-side lag.

For (1) and (2), separate modeling work is needed — adding remote-feed lag
sampling to sim is a reasonable next step but out of scope for this study.

## Mechanism details

`HyperliquidSim::onTrade` refreshes `last_feed_lag_ms_` from every HL trade's
`transact_t`. The BTC heartbeat subscription makes this work even when the
traded symbol is sparse (BTC trades ~10s of ms apart).

The effective delay is computed in `onSentOrder` (IOC, ALO) and `onCancelOrder`
(cancel) at order-send time. Both the `use_one_way_latency=true` (queue dispatch)
and `false` (event-loop dispatch) code paths respect `lag_aware_latency` — they
compose cleanly.

If `lag_aware_latency=false` (the default), the existing fixed constants
(`ioc_ord_latency`, `alo_ord_latency`, `cxl_ord_latency`) are used unchanged.

## Files

- C++: `src/pktrade/sim/sim_lvl_inst.{h,cc}` (commits `c72ec9c0`, `89a40ee5`)
- Sweep configs/outputs: `~/scratch/gf1_relcross_iter/feedlag_sweep_20260608/`
- Underlying findings:
  - `~/scratch/gf1_relcross_iter/feedlag_sweep_20260608/RESULTS.md`
  - `~/scratch/gf1_relcross_iter/feedlag_sweep_20260608/mmsweep/RESULTS.md`
  - `~/scratch/gf1_relcross_iter/allsym_sweep_20260522/HL_FEED_LAG_AT_SPIKES_20260608.md`
  - `~/scratch/gf1_relcross_iter/allsym_sweep_20260522/TAIL_RTT_EXPLORATION_20260608.md`
