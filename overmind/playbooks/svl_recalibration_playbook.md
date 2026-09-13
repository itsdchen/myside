# SVL Recalibration Playbook

Date: 2026-07-21

Living playbook for recalibrating the sim's `lag_aware_latency` knobs
(`alo_lag_base_ms`/`alo_lag_coef`, `cxl_lag_base_ms`/`cxl_lag_coef`,
`ioc_lag_base_ms`/`ioc_lag_coef`) against fresh live RTT data. Tool:
`overmind/strat_main/tools/trademan/trade_manager.py sim-sweep`.

Companion reference for the model itself:
`overmind/studies/lag_aware_sim_svl_settings_20260609.md` (June 2026 model
introduction). Latest calibration iteration writeup:
`overmind/studies/svl_lag_aware_iteration_20260609.md`.

This document captures:
1. When to run a recalibration (trigger conditions).
2. Prerequisites — the two things that will silently break the sweep if skipped.
3. The step-by-step methodology.
4. Gotchas learned the hard way in the 2026-07 iteration.
5. Decision guide for adopting new defaults vs per-strat overrides.

---

## 1. When to recalibrate

Any of these should trigger a fresh SVL sweep before you trust sim numbers:

- **Sim-side changes to fill/cancel/queue mechanics** in `sim_lvl_inst.cc` or
  its callers. Not the lag knobs themselves — those get retuned to the new
  mechanics.
- **Live-side changes that affect RTT distribution** — e.g., the 2026-06-30
  fast-cancel wsgateway fix (`e18e4552`) tightened cancel tails, invalidating
  the pre-fix (100, 0.8) cxl calibration.
- **HL exchange-side changes** — block time, matching engine, or feed pipeline.
- **Aux-sub switches** — the heartbeat symbol changed from BTC → HYPE in
  `sim: replace BTC with HYPE as HL feed-lag heartbeat` (b94218c6). Live
  configs may or may not have caught up.
- **Sim quality regression signal** — if the fleet-review sim/live gap
  suddenly widens across many strats simultaneously, it usually means the
  environment shifted under the calibration.

Don't recalibrate for a single-strat mismatch. Look at the residual
categories in §5 first — most strat-level residuals aren't lag-fixable.

---

## 2. Prerequisites (skip these and the sweep produces garbage silently)

### 2.1 Rebuild pktrade

The sim reads the lag knobs at runtime, but *the compiled defaults for those
knobs live in `sim_lvl_inst.h`* and only take effect after a rebuild. A stale
binary will silently ignore parameter overrides that aren't wired up in the
older binary version. **Rebuild first, always.**

```bash
cd /home/pktrade/tradefi/retraded_cron
./compileit.sh pktrade
```

Verify with a two-config sanity check — one with lag_aware knobs stripped
(uses compiled defaults), one with explicit new values. Their acct outputs
should be identical if the defaults were updated to match. See §6 for the
snippet.

### 2.2 Ensure HYPE aux subscription is loaded in the sim

`eff_lat = base + coef × feed_lag`. `feed_lag` is `now - last_transact_t` of
the most recent HL trade observation. HYPE is the recommended heartbeat
symbol because it trades frequently enough to keep `feed_lag` fresh even
during quiet windows for the traded symbol.

Two paths:

- **Preferred**: dated configs on the live machines already have
  `{"market":"Hyperliquid","symbol":"HYPE"}` in each pktrader's `extra_subs`.
  As of 2026-07 this is patchy — many configs still have BTC (pre-switch) or
  nothing at all.
- **Fallback for sweeps**: use `--force-lag-aware` in `sim-sweep`. The tool
  will (a) set `lag_aware_latency: true`, and (b) append HYPE to each
  pktrader's `extra_subs` in the generated variant configs. Idempotent, safe
  to always pass.

Confirm HYPE actually loads: `pktrade ... --dry-run` should include
`(Hyperliquid, HYPE)` in `Dryrun_subscriptions`. If not, the model's `coef`
term collapses to zero and the calibration will be nonsense.

---

## 3. Methodology

### 3.1 Measure current live RTTs

Reads directly from live orders files (`~/scratch/tradeperf/`, populated by
`trade_manager fetch`). Cancel and ALO RTT use `NewOrd → NewOrdAck` and
`Cancel → CancelAck`; IOC uses `NewOrd → earliest (Exec|Elimination|
NewOrderReject)` because IOCs never get an ack.

`sim_eval_compare.compute_order_rtt` handles ALO+cancel out of the box. For
IOC, see the snippet in §6.3.

Target percentiles:

- **p50** — anchors the median calibration target.
- **p99** — anchors the tail (drives the `coef` value; higher coef means
  faster growth during spikes).

Post-fix numbers observed 2026-07-08 → 07-14 (equity usday/allday adders,
consistent across gf1/gf2/gf3):

| TIF | p50 | p99 |
|---|--:|--:|
| ALO / cancel | ~450ms / ~460ms | ~720ms |
| IOC | ~790ms | ~1300ms |

Note the cancel-fix tightened tails significantly — pre-fix (2026-06)
IOC p99 was ~2500ms.

### 3.2 Derive a point estimate for the grid center

Assume the linear model holds and solve two equations for `(base, coef)`:

```
base + coef × feed_lag_p50 ≈ live_RTT_p50
base + coef × feed_lag_p99 ≈ live_RTT_p99
```

Feed lag distribution is roughly `p50 ≈ 485ms, p99 ≈ 1170ms` per the June
study — verify this hasn't shifted if HYPE trade frequency changed. For
July 2026:

- **Cancel**: `(720-460)/(1170-485) = 0.38`, `base = 460 - 485×0.38 ≈ 276ms`
  → point estimate `(276, 0.38)`.
- **ALO**: nearly symmetric to cancel by construction on HL →
  point estimate `(259, 0.39)`.
- **IOC**: `(1300-790)/(1170-485) = 0.74`, `base = 790 - 485×0.74 ≈ 431ms`
  → point estimate `(431, 0.74)`.

### 3.3 Design the grid — center on the point estimate, span ~±50%

Rule of thumb: **grid axes should not pin winners to their extremes**. If
your best combo lands on an edge, extend that axis and re-sweep.

For 2026-07:

- **Adder cxl**: `base ∈ {150, 250, 350, 450}, coef ∈ {0.15, 0.35, 0.55, 0.75}`
- **Adder alo** (small, since ALO/cxl are symmetric on HL):
  `base ∈ {200, 300}, coef ∈ {0.3, 0.5}`
- **Crosser ioc** (initial): `base ∈ {300, 450, 600, 750}, coef ∈ {0.4, 0.7, 1.0, 1.3}`
- **Crosser ioc** (extended after seeing edge-pinned winners):
  `base ∈ {500, 750, 1000, 1250}, coef ∈ {0.5, 1.0, 1.5, 2.0, 2.5, 3.0}`

Physical constraint worth applying: `base >= 500` for IOC (network + HL
match-engine floor). Bottom-corner IOC winners at `base=300` are usually
unphysical fits that game the loss surface.

### 3.4 Pick strat groups and run the sweeps

Adders and crossers are separate universes — sweep them independently. The
sim-sweep tool is single-strat, so drive it from a shell loop.

**Kitchen sink for adders** (drop `_boats_` and single-family
outliers to keep the consensus clean; commodities have a known
CME-remote-feed-staleness residual and won't fit, but include them anyway
to prove the residual pattern):

```bash
CMD="python overmind/strat_main/tools/trademan/trade_manager.py sim-sweep"
COMMON="--date-range 20260708:20260714 --skip-fetch --skip-md --force-lag-aware"
GRID="--alo-base 200,300 --alo-coef 0.3,0.5 \
      --cxl-base 150,250,350,450 --cxl-coef 0.15,0.35,0.55,0.75"
WEIGHTS="--shs-weight 0.5 --fr-weight 1.0 --cfr-weight 1.0 --pnl-weight 0.25"

for spec in gf3:combined_equities_bfx3:usday_cov_0616 \
            gf2:combined_equities_bfx2:usday_cov_0616 \
            gf1:combined_equities_bfx1:usday \
            gf0:combined_exotic_gf0:allday_cov_0313 \
            ...; do
  IFS=":" read -r alias group strat <<< "$spec"
  $CMD $COMMON $GRID $WEIGHTS \
    --aliases "$alias" --group "$group" --strat "$strat" \
    --output ~/scratch/svl_sweep_$(date +%Y%m%d)/adders/${alias}_${strat}/report.txt
done
```

**Crossers** are IOC-only; ALO/cxl don't apply. Use `--ioc-base` / `--ioc-coef`.

### 3.5 Composite score weights

The default composite is `1.0×shs + 1.0×fr + 1.0×cfr + 0.5×pnl`. For
crossers, `cfr` is essentially zero (IOC orders don't do resting-order
cancels), so it drops out and PnL dominates the score. That's usually too
noisy — PnL is day-to-day variable.

Recommended for post-fix crossers:
`--shs-weight 0.5 --fr-weight 1.0 --cfr-weight 1.0 --pnl-weight 0.25`.

Rationale:
- **Fill rate matters most** for crossers because it's the direct output of
  IOC latency (slower IOC → lower fillrate).
- **Shares** are correlated with fill rate; halving avoids double-counting.
- **PnL** is noisy and multi-modal per day; quartering keeps it as a
  tie-breaker rather than the primary signal.
- **cfr** stays at 1.0 for adders where it's a real signal.

### 3.6 Aggregate cross-strat

Parse each report's `AGGREGATE: ALL N DAYS` → `COMPOSITE FIT` block. The
rows are already sorted by score. Look at:

- **Per-strat winner** — is the winner at an interior grid point or edge-
  pinned? If edge-pinned, extend that direction and re-sweep.
- **Mean rank across strats** — a low-rank consensus setting is a good
  candidate for the global default.
- **Mean score across strats** — the consensus setting shouldn't score
  more than ~3-5× the per-strat best on any single strat, otherwise the
  compromise is too painful.

### 3.7 Verify and adopt

Update the compiled defaults in `sim_lvl_inst.h`:

```cpp
int64_t alo_lag_base_ms_ = <new>;
double alo_lag_coef_ = <new>;
int64_t cxl_lag_base_ms_ = <new>;
double cxl_lag_coef_ = <new>;
int64_t ioc_lag_base_ms_ = <new>;
double ioc_lag_coef_ = <new>;
```

Rebuild pktrade and re-run the two-config sanity check from §2.1 to confirm.
Also run a fresh `sim-eval` and check the fleet-level like-vs-like PnL gap
tightens vs prior binary — that's the closing "did we improve the sim"
check.

---

## 4. Gotchas from the 2026-07 iteration

### 4.1 Stale pktrade binary silently ignoring lag knobs (single worst gotcha)

`bin/pktrade` was from 2026-04. The lag-aware code shipped in June. The
2026-04 binary silently ignored `cxl_lag_base_ms=100000` (100 seconds!)
because the code path that reads that field didn't exist. **Every sim-eval
in the session was garbage until the rebuild**, and this took about 6 hours
of investigation to notice.

Two things saved us the second time: (a) checking `bin/pktrade` mtime as
soon as we saw parameter changes having no effect, and (b) the two-config
sanity check at §2.1.

**Rule**: mtime-check the binary before every sim-sweep session. If
`bin/pktrade` predates the current commit that touches `src/pktrade/sim/`,
rebuild.

### 4.2 HYPE aux sub missing from most deployed configs

The BTC → HYPE switch (`b94218c6`) was a source-side change; the
per-pktrader `extra_subs` updates never got a coordinated rollout. As of
2026-07, only ~4% of dated configs had HYPE and ~24% had BTC (pre-switch).

Without HYPE loaded, `feed_lag` stays 0 → `coef × feed_lag = 0` → sim's
effective latency collapses to just `base_ms`. Model is broken.

**Rule**: always pass `--force-lag-aware` to `sim-sweep` unless you have
verified the base configs already have HYPE. Verify with `--dry-run` output
looking for `(Hyperliquid, HYPE)` in `Dryrun_subscriptions`.

### 4.3 sim_eval's `_check_local_files` short-circuit + INFO log glob

Two separate bugs in the fetch path caused `parse_log_events` to silently
return empty:

- `_check_local_files` exited the fetch early if acct files were local, even
  when INFO logs (which live at different paths and different naming) were
  missing.
- The remote find glob was `pktrade.*.log.INFO` — literal dot after
  `pktrade` — which missed the newer `pktrade_N.HOST.log.INFO` naming.

Both fixed on this branch (see the `sim_eval: fix fetch step` commit). If
you see zero `usermsg`/`inherit` schedule files generated on a re-run,
suspect these first.

### 4.4 Multi-host self-collision and CME remote-feed staleness

`gf1` crossers with same-symbols-across-gf1+gf2+gf3 will always show a
residual because sim sees the full HL counterparty while live splits it
across our own deploys. `gf0` CME-paired adders will always show a
residual because sim assumes CME's quote at `sim_now` is current;
Databento → us has its own one-way delay.

These are *not lag-fixable*. Exclude them from the consensus, note the
residual, move on. Trying to make them fit will drag the calibration off
for the strats that would otherwise converge cleanly.

### 4.5 Committing to `main` is not the default

We use feature branches for everything, including calibration writeups.
`svl-recalibration-<yyyymmdd>` is a reasonable convention.

---

## 5. Decision guide — global default vs per-strat override

Prefer **global default** in `sim_lvl_inst.h`:

- Cross-strat consensus setting is within ~3× of every per-strat best.
- No cluster of strats disagrees systematically.
- The compromise cost is smaller than the drift risk from letting configs
  diverge.

Prefer **per-strat override** in each `pk_*.json`:

- The strat has a known non-lag-fixable residual (multi-host, CME
  staleness, symbol microstructure).
- The per-strat best is 5-10× better than any global consensus.
- Very high volume — the residual affects fleet-level PnL enough to justify
  the config drift.

Don't per-strat-override casually. Configs drift; every override is a
future recalibration burden.

---

## 6. Reference snippets

### 6.1 Two-config sanity check (verify binary picks up new defaults)

```python
import json, subprocess, hashlib, os
base = json.load(open('/some/pk_*.json.20260714'))

# Strip lag knobs → exercises compiled defaults
d1 = json.loads(json.dumps(base))
d1['simulation']['Hyperliquid']['lag_aware_latency'] = True
for k in ('alo_lag_base_ms','alo_lag_coef','cxl_lag_base_ms',
          'cxl_lag_coef','ioc_lag_base_ms','ioc_lag_coef'):
    d1['simulation']['Hyperliquid'].pop(k, None)
json.dump(d1, open('/tmp/conf_defaults.json','w'), indent=2)

# Explicit NEW values — should match d1
d2 = json.loads(json.dumps(base))
sim = d2['simulation']['Hyperliquid']
sim['lag_aware_latency'] = True
sim.update({'alo_lag_base_ms':300,'alo_lag_coef':0.5,
            'cxl_lag_base_ms':450,'cxl_lag_coef':0.55,
            'ioc_lag_base_ms':750,'ioc_lag_coef':1.5})
json.dump(d2, open('/tmp/conf_new.json','w'), indent=2)

for tag in ('defaults','new'):
    subprocess.run(f'bin/pktrade --date 20260714 --conf /tmp/conf_{tag}.json '
                   f'--acct /tmp/acct_{tag}.csv --trd /dev/null', shell=True)
    print(tag, hashlib.md5(open(f'/tmp/acct_{tag}.csv','rb').read()).hexdigest())
# Expect the two md5s to match. If they don't, your binary is stale.
```

### 6.2 Live cancel + ALO RTT per strat

```python
from sim_eval_compare import compute_order_rtt
r = compute_order_rtt(f'/home/pktrade/scratch/tradeperf/{alias}/{group}/{strat}/orders_{date}.csv')
add_vals = [v for xs in r['ord_rtt'].values() for v in xs]
cxl_vals = [v for xs in r['cxl_rtt'].values() for v in xs]
```

### 6.3 Live IOC RTT (crossers)

`compute_order_rtt` doesn't handle IOC because they don't get acks. Roll
your own:

```python
from collections import defaultdict
def ioc_rtt(path):
    new_ords, term = {}, {}
    for line in open(path, errors='replace'):
        parts = line.strip().split(',')
        if len(parts) < 5: continue
        try: t = int(parts[1])
        except ValueError: continue
        etype, oid, sym = parts[2], parts[3], parts[4]
        if etype == 'NewOrd':
            new_ords[oid] = (t, sym)
        elif etype in ('Exec','Elimination','NewOrderReject'):
            if oid not in term or t < term[oid][0]:
                term[oid] = (t, sym)
    rtts = defaultdict(list)
    for oid, (t0, sym) in new_ords.items():
        if oid in term:
            rtt = term[oid][0] - t0
            if 0 <= rtt < 60000: rtts[sym].append(rtt)
    return rtts
```

---

## 7. Files touched during a recalibration

- `src/pktrade/sim/sim_lvl_inst.h` — compiled defaults.
- `overmind/strat_main/tools/trademan/sim_sweep.py` — sweep tool; add new
  axes here if the model gains parameters.
- `overmind/strat_main/tools/trademan/trade_manager.py` — CLI for
  sim-sweep.

Study docs to update at the end:

- New iteration writeup at `overmind/studies/svl_recalibration_<yyyymmdd>.md`
  summarizing the sweep results, chosen defaults, and residual notes.
- Consider whether the settings-reference doc
  (`lag_aware_sim_svl_settings_20260609.md`) still reflects reality; if
  not, replace it or supersede.
