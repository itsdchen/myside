# SVL recalibration — post-fast-cancel-fix — July 2026

Writeup of the SVL sweep that produced the current compiled defaults
`(alo=300/0.5, cxl=450/0.55, ioc=750/1.5)`. Iteration follows
`lag_aware_sim_svl_settings_20260609.md` (June introduction) and
`svl_lag_aware_iteration_20260609.md`. Methodology docs:
`overmind/playbooks/svl_recalibration_playbook.md`.

## Symptom

Fleet-wide sim-vs-live comparison over 2026-07-08 → 07-14 showed sim
underperforming live by ~$26k on a $30k live PnL base — 88% miss on
like-vs-like rows. Pattern was systematic across most strats, not one
family: **sim over-trades adders 3-6× (usday/allday/rel-family) and
under-trades crossers 5-10× (RelCross variants)**. Both bled PnL relative
to live.

The direction was inconsistent with the June study's diagnosis: pre-lag-
aware sim over-filled RelCross by ~2×; June's calibration brought that to
~1× on gf3. Something had shifted.

## Root cause (took a while)

Two sequential discoveries.

### First: the fetch pipeline was silently dropping events

`sim_eval.py`'s fetch step short-circuited via `_check_local_files`
whenever acct files were local, even when INFO logs (needed for
`usermsg`/`inherit` schedule replay) weren't. Separately, the remote
find glob was `pktrade.*.log.INFO` — literal dot after `pktrade` — which
missed the newer `pktrade_N.HOST.log.INFO` filenames. Effect: every
`sim-eval` since the filename change was running with **zero replayed
schedules**, and no one noticed because the sim-eval log doesn't
prominently surface a "0 events found" message.

Fixed in `sim_eval: fix fetch step so INFO logs actually get pulled` on
this branch. Removing the `_check_local_files` shortcut is safe because
`scp_files` already dedupes per-file.

### Second: `bin/pktrade` was 4 months old

**The compiled binary at `bin/pktrade` predated the entire `lag_aware`
code path**. Rebuilt 2026-04-22, before commits `c72ec9c0` / `89a40ee5`
that introduced `lag_aware_latency`. Setting `cxl_lag_base_ms=100000`
(100 seconds, which would guarantee every cancel fails catastrophically)
produced byte-identical output to `cxl_lag_base_ms=0` because the code
that reads that field didn't exist yet.

This blew up half a day of investigation before we noticed. It's now the
loudest gotcha in the playbook.

## Post-fix live RTT distribution

Measured across the 11 usday/allday adder strats and 4 RelCross strats
over 2026-07-08 → 07-14 (all post-fast-cancel-fix). ALO+CXL RTT from
`sim_eval_compare.compute_order_rtt`, IOC RTT from `NewOrd → earliest
(Exec | Elimination | NewOrderReject)`.

| TIF | p1 | p50 | p90 | p99 |
|---|--:|--:|--:|--:|
| ALO (NewOrd→Ack) | ~340 | ~450 | ~530 | ~720 |
| Cancel (Cancel→Ack) | ~350 | ~460 | ~530 | ~720 |
| IOC (NewOrd→terminal) | ~650 | ~790 | ~910 | ~1300 |

Compare to June:
- June ALO/cxl p50 was ~530ms → post-fix ~460ms (~13% faster median)
- June IOC p50 was ~970ms → post-fix ~790ms (~19% faster median)
- **June IOC p99 was ~2500ms → post-fix ~1300ms (~50% faster tail)**

The fast-cancel wsgateway fix (`e18e4552`, 2026-06-30) tightened tails
much more than medians. That's the key change that invalidated the June
calibration.

## Deriving the grid center from data

The linear model `eff_lat = base + coef × feed_lag` has two unknowns.
Using p50 and p99 as two anchors (feed_lag distribution assumed
unchanged from June: p50 ≈ 485ms, p99 ≈ 1170ms):

- **Cancel**: `(720-460)/(1170-485) = 0.38`, `base = 460 - 485×0.38 = 276`
- **ALO**: essentially symmetric to cxl → `(259, 0.39)`
- **IOC**: `(1300-790)/(1170-485) = 0.74`, `base = 790 - 485×0.74 = 431`

These are the point-estimate grid centers. Actual sweeps span ~±50%
around them.

## Sweeps

### Adder pass — 11 strats × 64 combos

Grid: `alo_base ∈ {200, 300}, alo_coef ∈ {0.3, 0.5}, cxl_base ∈ {150,
250, 350, 450}, cxl_coef ∈ {0.15, 0.35, 0.55, 0.75}`. ~10 hours of
compute across gf0/gf1/gf2/gf3 usday/allday variants.

Weights: `shs=1.0, fr=1.0, cfr=1.0, pnl=0.5` (default at time of run).

Best per strat (top 6, ordered by fit quality):

| strat | best (alo_b, alo_c, cxl_b, cxl_c) | score | shs_r | fr_r | cfr_r | pnl_r |
|---|---|--:|--:|--:|--:|--:|
| gf3/usday_cov_0616 | (300, 0.3, 450, 0.55) | 0.10 | 1.00 | 1.01 | 1.00 | 1.19 |
| gf3/usday_cov_0516 | (200, 0.5, 450, 0.55) | 0.21 | 1.01 | 1.01 | 1.11 | 0.83 |
| gf2/usday_cov_0616 | (300, 0.3, 150, 0.55) | 1.29 | 1.56 | 1.53 | 1.18 | 0.95 |
| gf2/usday_cov_0521 | (300, 0.3, 350, 0.75) | 1.41 | 1.50 | 1.50 | 1.00 | 0.20 |
| gf1/allday_0706 | (200, 0.5, 250, 0.55) | 1.79 | 1.49 | 0.73 | 0.68 | -0.43 |
| gf0/allday_cov_0423 | (300, 0.5, 450, 0.75) | 2.28 | 2.52 | 1.32 | 1.33 | 0.77 |

**Cross-strat consensus (mean rank across 6 clean-parse strats)**:
`(300, 0.5, 450, 0.55)` — mean rank 12/64. Wins over `orig` on every
strat in the sweep; matches strat-best on gf3, is a modest compromise on
gf2 variants. Effective latencies:

- ALO baseline: 300 + 0.5×485 = 543ms vs live_p50 450ms (1.21×)
- CXL baseline: 450 + 0.55×485 = 717ms vs live_p50 460ms (**1.56×**)

The cxl coefficient is meaningfully above live median — sim needs cancels
modeled slower than reality to counterbalance otherwise-optimistic
queue-position modeling. Empirically closes shs_r to ~1.0 on the equity
usday/allday family. Not a bug in the calibration, a fill-model artifact.

### Crosser pass — 2 rounds

**Round 1** (16-combo initial grid): winners edge-pinned to the top-of-
grid `ib=750` for gf1 and bottom-of-grid `ib=300`/`ib=500` for gf3.
Extension needed.

**Round 2** (24-combo extended grid with `ib ≥ 500` physical constraint,
reweighted composite with `shs=0.5, fr=1.0, cfr=1.0, pnl=0.25`):

- Grid: `ib ∈ {500, 750, 1000, 1250}, ic ∈ {0.5, 1.0, 1.5, 2.0, 2.5, 3.0}`
- gf1 winners moved to interior (`ib=750`), confirming the sweet spot.
- gf3 winners at `ib=500` (bottom of constrained grid) — physically OK
  since IOC floor is ~640-700ms and reality has staleness that pulls
  eff_ioc above base.

Best per strat:

| strat | best (ib, ic) | score | shs_r | fr_r | pnl_r |
|---|---|--:|--:|--:|--:|
| gf3/cross_0616 | (500, 1.0) | 0.04 | 1.02 | 1.02 | 1.02 |
| gf3/cross_0527 | (500, 0.5) | 0.09 | 1.03 | 1.03 | 0.82 |
| gf1/eq_gf1_cross/v3 | (750, 1.0) | 0.24 | 1.06 | 0.93 | 0.43 |
| gf1/eq_gf1_cross/v2 | (750, 1.5) | 0.30 | 0.88 | 0.79 | 0.90 |

**Cross-strat consensus**: `ib=750, ic=1.5`, mean score 0.32.

- Effective latency baseline: 750 + 1.5×485 = **1478ms** vs live_p50 790ms (1.87×).
- Effective latency spike (feed_lag=1500ms): 750 + 1.5×1500 = 3000ms.
- Previous default (`ib=900, ic=2.0`) gave 1870ms baseline (2.37×) — that
  was over-slowing IOCs post-fix and driving the crosser under-trade.

## Composite score reweighting rationale

Default composite is `1.0×shs + 1.0×fr + 1.0×cfr + 0.5×pnl`. For
crossers, `cfr` is essentially zero (IOC orders don't do resting
cancels), so the term drops out and PnL dominates the remaining
weighting. PnL is noisy per-day; adopting it as primary drove weird
edge-pinned winners.

Chosen for the crosser Round 2: `shs=0.5, fr=1.0, cfr=1.0, pnl=0.25`.
Rationale:
- Fill rate is the *direct* output of IOC latency — up-weight it.
- Shares tracks fill rate closely — halve to avoid double-counting.
- PnL is a tie-breaker, not a primary signal — quarter it.
- CFR stays 1.0 for adders where it's a real signal.

The tool now takes `--shs-weight`, `--fr-weight`, `--cfr-weight`
alongside the existing `--pnl-weight`.

## Adopted compiled defaults

Header changes in `src/pktrade/sim/sim_lvl_inst.h`:

| knob | old (2026-06-29) | new (2026-07-21) |
|---|--:|--:|
| `alo_lag_base_ms_` | 150 | **300** |
| `alo_lag_coef_` | 1.25 | **0.5** |
| `cxl_lag_base_ms_` | 350 | **450** |
| `cxl_lag_coef_` | 1.25 | **0.55** |
| `ioc_lag_base_ms_` | 900 | **750** |
| `ioc_lag_coef_` | 2.0 | **1.5** |

Verified via the two-config sanity check (config with fields stripped ≡
config with explicit new values, both differ from old). Rebuild is
required for the new defaults to take effect.

## Residuals — not lag-fixable

The June study called these out; this iteration confirms them:

- **gf1 multi-host self-collision** (`gf1/eq_gf1_cross/v2` and `v3`):
  NVDA, PLTR, TSLA, AMZN, RIVN, BABA are deployed on cross strategies
  across gf1+gf2+gf3. Each sim sees full HL counterparty depth; live
  splits it across our own deploys. Consensus score for gf1 crossers is
  ~0.30-0.50 vs ~0.05-0.10 for gf3 crossers — the delta is exactly this
  residual. No latency model fixes it.

- **gf0 CME remote-feed staleness** (`gf0/combined_exotic_gf0/allday_*`):
  sim assumes the CME quote at `sim_now` is current; Databento → us has
  its own one-way delay. `gf0/allday_cov_0313` scored 6.11 at its own
  best — 20× worse than gf3 usday_cov_0616 — and no combo in the sweep
  brought it below shs_r=1.57.

- **`gf2/usday_cov_0616` symbol-mix anomaly**: prefers `cxl_base=150`
  vs the consensus 450. Its symbol mix behaves differently enough that
  the consensus is 4.6× worse than the strat-best. Worth investigating
  separately — probably tied to specific symbols with unusual queue
  dynamics.

## What ships in this branch (`svl-recalibration-0721`)

- `src/pktrade/sim/sim_lvl_inst.h` — new compiled defaults (see table
  above).
- `overmind/strat_main/tools/trademan/sim_sweep.py` — supports lag-aware
  axes (`--alo-base`, `--alo-coef`, `--cxl-base`, `--cxl-coef`,
  `--ioc-base`, `--ioc-coef`), a `--force-lag-aware` mutation that
  writes `lag_aware_latency: true` and adds HYPE to `extra_subs`, and
  weighted composite scoring.
- `overmind/strat_main/tools/trademan/trade_manager.py` — CLI plumbing.
- `overmind/strat_main/tools/trademan/sim_eval.py` — fetch step fixes.
- `overmind/playbooks/svl_recalibration_playbook.md` — methodology &
  gotchas doc.

## Follow-ups

- **`gf2/usday_cov_0616` symbol-level dive** — figure out why it wants
  `cxl_base=150` while everyone else wants 450. Possibly one symbol in
  the mix has fundamentally different queue behavior.
- **Roll HYPE into per-pktrader `extra_subs`** across deployed
  `pk_*.json` configs. The `--force-lag-aware` runtime path patches
  this in sweeps, but deployed live configs still mostly lack HYPE
  (some have stale BTC). Configs with no HYPE won't get feed_lag
  updates during quiet windows for their traded symbol.
- **Update the settings-reference doc** —
  `lag_aware_sim_svl_settings_20260609.md` still says BTC and lists
  outdated numbers. Either supersede it with a
  `lag_aware_sim_svl_settings_20260721.md` or update in place with a
  clear "current settings" section.
- **Fleet-review verification** — re-run the July sim-eval on the new
  binary and confirm the like-vs-like PnL gap materially tightens vs
  the old binary. That's the closing "did we improve sim" check.
