# Fleet Review Playbook

How to run the fleet-wide inheritance-attributed review, root-cause the
actionable pairs, and apply kill decisions safely. This is the durable
"how do we do this every time" guide; snapshot findings from each pass
live in dated companion studies (e.g. `fleet_review_20260715.md`).

Companion tooling: `overmind/strat_main/tools/trademan/fleet_review.py`.

---

## When to run

- Ad-hoc: any time you want to re-baseline "who's earning edge vs riding
  inheritance."
- After a major infrastructure change (e.g. cancel-path fix): once you have
  ~10 post-change trading days.
- On a rolling basis: consider adding to cron once the method is trusted
  (see final section).

## Step 0 — Pick the analysis window

Pick a `--since YYYYMMDD` that starts *after* any recent infrastructure
change so pre- and post-change trading aren't lumped together. Two hazards
this avoids:

- **Cancel-path / feed / gateway fixes** materially change fill quality —
  see `reference_fast_cancel_fix.md` and the study
  `us_equities_cancel_rtt_degradation_202606.md` for the June 2026 case.
- **Sim-vs-live calibration** work assumes the current infra; anything from
  before the fix is not a valid target.

Rule of thumb: use the fix-merge-date or a couple of days after, whichever
lines up with a clean session boundary. Note the date in the resulting
study.

## Step 1 — Attribute PnL

```bash
python fleet_review.py --since YYYYMMDD run
```

`run` chains attribute → classify → history. Output CSVs land under
`~/scratch/tradeperf/`:

- `_attribution_<since>.csv` — one row per (strat, sym, date) with
  `net`, `inh`, `own`, `had_inh`, `sod_pos`.
- `_actionable_<since>.csv` — one row per (strat, sym) aggregate with
  a `cat` in {FAKE_WINNER, REAL_BLEEDER, CUSHIONED_BLEEDER,
  MARGINAL_BLEEDER, OVERACHIEVER, GENUINE_WINNER, NEUTRAL}.
- `_history_review_<since>.csv` — actionable pairs enriched with
  lifetime PnL, monthly averages, peak date, days-since-peak, drawdown,
  and a `verdict` in {ALWAYS_BAD, OLD_DECAY, RECENT_DECAY, CYCLICAL,
  STEADY_EARNER, NEW/THIN}.

**Attribution method** (see `fleet_review.py` docstring and
`fleet_review_20260715.md` for the full write-up). MTM approach:
`inherit_mtm = SOD_pos * (last_mid − first_mid)` measures the drag/lift on
the handoff position across the strat's own session. `strat_own = net_pnl
− inherit_mtm` is the residual — the earned edge.

Known limitations to remember when reading output:
- Mid-day inherits (second GPCalc event) get absorbed into `strat_own`.
- Cross strats end flat by design → `inherit_mtm = 0`. Cross PnL is
  always genuine.
- The proxy assumes zero cost-basis change; it doesn't reconstruct the
  sibling's original entry price. A future refinement is FIFO or
  cost-basis attribution parsed from GPCalc log lines.

## Step 2 — Build the kill / retune / size-up shortlists

From the `_history_review_<since>.csv`, split by verdict × category:

- **Clean kills** — `verdict = ALWAYS_BAD`. Never profitable, no history to
  save. Cheap to remove.
- **Postmortem candidates** — `verdict = RECENT_DECAY` or `OLD_DECAY` for
  pairs with meaningful lifetime PnL. Something worked and broke.
- **Fake winners** — `cat = FAKE_WINNER`. Positive net is inheritance-driven;
  strat's own edge is negative. Dangerous because raw PnL says "keep running."
- **Size-up candidates** — `cat = OVERACHIEVER` + `verdict = STEADY_EARNER`.
  Consider running the capacity sweep from
  `allsym_relcross_capacity_20260531.md`.

**Cluster the postmortem candidates by peak date.** If a batch shares an
inflection date, one postmortem may explain the whole cluster (the
2026-07-15 pass surfaced 5 tech mega-caps all peaking on 2026-06-25).

## Step 3 — Root-cause each kill (mandatory before disabling)

For every symbol you plan to disable, run the three-source diagnostic
before touching config — you want the "why" recorded, not just the "what."
Framework and script inventory in
`postmortems/README.md` + `postmortems/_workflow.md`.

Per-symbol checklist:

1. **Strat-side rollup** — compare baseline vs bad window from
   `acct_*.csv`. Key metrics: `net_pnl`, `times_traded`, `times_flipped`,
   `shs_traded`, `shs_sent`, `fill_rate`, `min_pnl_seen`. Sharp changes
   in `times_flipped` or `fill_rate` are the loudest tells.

2. **HL micro-structure** — `pm_hl_stats.py`:
   ```bash
   python postmortems/pm_hl_stats.py --symbol xyz:SYM \
       --baseline YYYYMMDD:YYYYMMDD --bad YYYYMMDD:YYYYMMDD
   ```
   Key ratios: `num_trades`, `vol_notional_per_day`, `spread_bps_avg`,
   `med_inside_liq`, `n_midchanges_per_day`, `ret_stdev_bps`. Fast (~seconds).

3. **Per-day timeline around the inflection** — `pm_daily_timeline.py`.
   Standard view for any kill / retune / postmortem decision. Shows N
   weekdays before + after an inflection date, with HL micro-structure
   (spread, inside_liq, midchanges, notional, med_trdsz, avg_mid), strat-side
   metrics (net_pnl, fill_rate as bps, min_pnl, times_traded/flipped),
   cancel-fill rate (from orders file), and markouts (via bin/markout).
   ```bash
   python postmortems/pm_daily_timeline.py --sym xyz:NVDA --inflection 20260625 \
       --alias gf1 --group combined_equities_bfx1 --strat usday \
       --cancel-fills --markouts
   ```
   Weekends skipped automatically. Fill rate is computed from
   shs_traded/shs_sent because the acct file's own `fill_rate` column is
   2dp-lossy (sub-1% rates show as 0.00). Use this **for every kill /
   postmortem decision** — the day-by-day step function makes inflections
   obvious in a way the aggregate baseline-vs-bad ratios don't.

4. **Topbook micro-structure** — `pm_topbook_stats.py` (uses Databento).
   ```bash
   python postmortems/pm_topbook_stats.py --symbol SYM \
       --baseline YYYYMMDD:YYYYMMDD --bad YYYYMMDD:YYYYMMDD
   ```
   Slower — plan minutes not seconds. Confirms whether the driver was an
   underlying-market event or HL-only. When time-constrained, skip topbook
   for symbols where the HL story is unambiguous (dead volume, huge inside
   change, etc.) and only run it for the ambiguous cases.

Pattern lookup table (from
`usday_symbol_degradation_observations_20260618.md`):

| topbook | HL | reading |
|---|---|---|
| moves | moves | underlying news/regime event |
| flat | moves | HL participant / liquidity change |
| moves | flat | HL feed / connectivity issue |
| all flat, strat-side changed | | config / sizing / signal-side change |

Record the "why" per symbol in the dated study.

## Step 3.5 — Apply the kill decision framework

**Do not disable a currently-earning strat based on backward attribution
alone.** Run `overmind/studies/kill_decision_framework.md` before any
disable / size-cut recommendation.

### What the classifier is actually measuring

`fleet_review.py classify` labels each pair by the sign pattern of
`(net, inh_mtm, own)` where:

- `inh_mtm = SOD_pos × (last_mid − first_mid)` is the **mark-to-market
  change of the inherited SOD position if held constant** across the
  strat's session. A fair-value benchmark — it doesn't depend on what
  the strat did.
- `own = net_pnl − inh_mtm` is the difference between actual realized
  and buy-and-hold-mark. Captures spread capture minus adverse selection
  minus positioning changes. **`own < 0` does NOT mean the strat lost
  money** — it means the strat's active trading underperformed the
  buy-and-hold benchmark on the SOD position that window.

For a well-functioning MM on a day when the SOD position drifted
favorably: the pure buy-and-hold benchmark captures the drift, and the
MM's flatten-by-EOD activity naturally gives some of it back → `own`
can be negative even while the strat is capturing real spread and
earning positive net.

So `FAKE_WINNER` / `REAL_BLEEDER` / etc are **DIAGNOSTIC labels**, not
edge verdicts. Never disable on them alone.

### Quick framework version

1. Check recent monthly hit rate + PnL trajectory (last 3 months).
2. If recent month is best month or hit rate trending up → **HOLD**, even
   if classification / HL flags say otherwise.
3. Fleet-review categories, HL divergence flags, and fill_bp z-spikes
   are **warnings**, not verdicts. Combine them with forward PnL
   trajectory (Step 1) before recommending action.

Lesson from 2026-07-15: META and GOOGL both had loud backward-attribution
red flags but were in the middle of their best month by hit rate. Killing
either would have been wrong.

## Step 4 — Apply the kills to live configs

**Rule**: config on remote lives at `{host}:/home/ubuntu/estrader/{group}/{strat}/pk_*.json`.
Snapshot copies in `~/scratch/tradeperf/{alias}/{group}/{strat}/pk_*.json.YYYYMMDD`
lag by ~1 day — always edit the un-dated live file, not a snapshot.

The safe procedure per (host, filepath, syms):

1. **Locate the live file** — one un-dated `pk_*.json` per strat dir. Names
   vary (`pk_gf2.json`, `pk_cx_gf1_1.json`, `pk_rel_nq_allday_boats.json`).
2. **Back up** — `cp file file.pre_kills.YYYYMMDD` on the remote before
   editing.
3. **Edit line-based, not JSON-round-tripped.** Line-based edits preserve
   formatting/indent exactly and produce a one-line diff per disable that's
   easy to audit. JSON round-trip re-flows the whole file and hides intent
   in a big diff.
4. **Diff after** — confirm exactly `N` lines changed for `N` disables and
   nothing else moved.
5. **Verify** — read the file back through the JSON parser and confirm the
   target symbol has `enabled: false`.

The mechanism for the disable is `"enabled": true` → `"enabled": false` on
the pktrader entry matching the `traded_symbol`. Line-based edit script
used on 2026-07-15 is inlined below (copy-paste template):

```python
FP = '/home/ubuntu/estrader/{group}/{strat}/{fname}.json'
KILLS = ['xyz:SYM1', 'xyz:SYM2']
with open(FP) as f: lines = f.readlines()
changes = []
for i, line in enumerate(lines):
    for sym in KILLS:
        if f'"traded_symbol": "{sym}"' in line:
            for j in range(i+1, min(i+6, len(lines))):
                if '"enabled":' in lines[j]:
                    if 'true' in lines[j]:
                        lines[j] = lines[j].replace('true', 'false')
                        changes.append((sym, j+1, lines[j].strip()))
                    else:
                        changes.append((sym, j+1, 'already-false-or-missing'))
                    break
with open(FP, 'w') as f: f.writelines(lines)
for c in changes: print(f'  line {c[1]}: {c[0]} -> {c[2]}')
```

### When the change takes effect

**Config changes are picked up at the next daily process startup** — pktrade
processes are not restarted mid-day. Do NOT force a restart to accelerate a
disable unless there's an active-loss risk. For clean kills there isn't:
the disable applies tomorrow, which is fine.

Practical implication: bundle kill batches so a single day's edit covers
everything you want live tomorrow. Don't drip individual changes over the
week if you can help it — restart cadence is a shared resource with
whatever else is going on that day.

### Rollback

Backups are per-host in `/tmp/{fname}.pre_kills.YYYYMMDD`. To reverse:

```bash
ssh {host} 'cp /tmp/{fname}.pre_kills.YYYYMMDD /home/ubuntu/estrader/{group}/{strat}/{fname}'
```

Takes effect on next daily startup like any other config change.

## Step 5 — Record the pass

Every fleet review pass produces a dated study `fleet_review_YYYYMMDD.md`.
Minimum contents:

- Analysis window and rationale (which infra change is the cutoff)
- Fleet-total table (net / inh / own / inh_share)
- Per-family rollup
- Category counts (FAKE_WINNER etc.)
- Verdict × category crosstab
- Per-symbol root-cause reads for anything you disabled
- What got applied to live configs (host, file, sym, line, direction)
- Follow-ups + open decisions

The 2026-07-15 study is the pattern to copy.

## Optional: cron integration

Once the flow is trusted, consider adding a weekly cron entry:

```
0 3 * * 2 /home/pktrade/.venvs/v1/bin/python \
  /home/pktrade/tradefi/retraded_cron/overmind/strat_main/tools/trademan/fleet_review.py \
  --since {latest-infra-fix-date} run \
  >> ~/scratch/tradeperf/cron_fleet_review.log 2>&1
```

Weekly cadence is right — daily is too noisy given inheritance rolls over
day-to-day. `--since` should track the most recent material infra change
(cancel path, sim model change, etc.). The dated CSVs accumulate and
can be diffed run-over-run to catch new bleeders early.

## Companion: HL divergence monitor

The fleet review is *retrospective* (looks at PnL attribution). The
**HL divergence monitor** (`hl_divergence_monitor.py`) is *prospective* —
it z-scores each HL sym's per-day micro-structure against a trailing
30-weekday baseline and flags shifts *before* they show up as PnL loss.

- **HL-only mode** (fast): daily cron, `--rolling 10 --consec-threshold 3`.
  Catches most regime shifts; backfill on 2026-06-25 tech-cluster event
  cleanly reproduced the manually-identified fingerprint.
- **HL+topbook mode** (slow, Databento): `--include-topbook`. Adds
  HL/topbook ratio dimensions — the crown jewel that distinguishes
  "HL-specific participant change" from "market-wide event." Run weekly
  or ad-hoc for US-equity syms; cached per-day under `~/scratch/postmortems/_topbook_cache/`.

Suggested cadence:
- **Nightly**: `hl_divergence_monitor.py --rolling 10 --consec-threshold 3`
  (HL-only, ~seconds after cache warm).
- **Weekly**: same + `--include-topbook` for US equities (adds Databento
  divergence dims; slow first pass, then cached).
- **On any fleet review pass**: cross-check the HL-divergence flags with
  the fleet review's PnL-attribution flags. Symbols flagged by *both*
  are the highest-conviction disable / retune candidates.
