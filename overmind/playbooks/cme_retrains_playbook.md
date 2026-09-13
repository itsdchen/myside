# CME Retrains — Canonical Runbook

Last updated: 2026-07-27

This is the operational source of truth for RelWideMM2 retrains of
Hyperliquid HIP3 products whose leading/reference book is CME or another
remote market. Historical studies under `overmind/studies/` explain how the
process evolved, but their dated commands and fixed simulation windows are
not launch templates.

The runbook covers SP500, XYZ100, CL, BRENTOIL, GOLD, SILVER, COPPER,
PLATINUM, PALLADIUM, EUR, JPY, GBP, and later symbols using the same design.
The general farm reference remains `overmind/swarmhost/PLAYBOOK.md`.

---

## 1. Non-negotiable rules

1. **Use recent data. Never copy a fixed historical date range.**
2. **State the exact dates before launch.** A range such as "the last month"
   is not sufficient.
3. **Build and verify the binary that will actually run remotely.** On a
   non-Jammy workstation, remote sweeps ship `jammy.bin/pktrade`, not
   `bin/pktrade`.
4. **Use a new workdir when the binary, dates, configs, grid, sample, scoring,
   or symbol changes.** Existing per-variant caches are results, not a generic
   acceleration cache.
5. **Never hide missing market data.** Report missing dates, subscriptions,
   examples, and likely holidays versus capture gaps; the user makes the
   explicit yes/no decision about excluding or accepting them.
6. **Audit remote signal and tradecall wiring before trusting a metric.**
7. **Run a dry run and record a manifest before dispatch.**
8. **Run one symbol at a time on the 48-core pool.** Parallel symbol sweeps
   oversubscribe the same hosts and historically created cache-contamination
   risk.
9. **Keep the approved queue continuous.** While one sweep runs, preflight
   and arm its successor. As soon as all jobs for the current symbol reach a
   clean `state: done`, launch the next approved symbol; do not leave the farm
   idle waiting for a manual status check. A failed gate or an unresolved
   user decision still stops that handoff.
10. **Do not promote the highest in-sample row.** Require daily-path,
    neighborhood, corrected-clock, and declared OOS evidence. If OOS is too
    short or unavailable, only an explicit user-approved provisional IS
    judgment may substitute; record it as such.
11. **Keep generated configs and run artifacts in scratch, not the repo.**
    Per-symbol sources, combined PK files, manifests, stats, and rollback
    snapshots belong under `~/scratch/cme_retrains/`. Repo-tracked paths contain
    tools and playbooks, never generated deployment configs.

If any gate fails, stop. Do not turn a warning into an implicit methodology
decision.

---

## 2. Recent-window policy

### 2.1 Choose the end date

Define `as_of` as an **inclusive, fully completed simulation session**.

- For a normal run, use the latest fully captured session, usually yesterday
  in `America/New_York`.
- If the user supplies an end date, that date is inclusive and must remain the
  requested end. If required data for it cannot be obtained, report the exact
  gap and ask whether to exclude the date, proceed with the limitation, or
  move the end. Never make that choice silently.
- Never include the current incomplete session.
- `SimVariations.py --end` is exclusive. An inclusive `as_of=20260721`
  therefore requires `--end 20260722`.

### 2.2 Choose the window

Default to the **30 most recent complete eligible sessions ending on `as_of`**.
Twenty sessions is the minimum for an exploratory search; prefer 30 or more.
The window must roll forward on future trials. Fixed May/June 2026 ranges in
the historical studies are examples of old experiments only.

For comparisons such as SP500 versus XYZ100, use the exact same session list.
Start from recent weekdays, then apply exchange holidays, the permanent date
blacklist, and full dependency checks. One or two unavailable sessions may be
acceptable. Show them to the user and record whether the approved result is a
shorter window, a start-date extension, or an explicitly partial-data trial.
Do not silently manufacture a different window.

Before launch, print and store:

- requested inclusive `as_of`;
- computed exclusive CLI end;
- target and actual session count;
- the complete ordered session list;
- excluded dates and reasons.

### 2.3 Freeze IS at queue start; accumulate OOS while the queue runs

The launch manifest must contain `search_dates` and a `holdout_policy`.
Append the actual `oos_dates` only after those future sessions exist. Dates
used to rank or narrow variants are not OOS.

For a long sequential CME retrain, the default policy is:

1. Immediately before launching the first symbol, freeze one common recent IS
   list ending on the latest completed approved session.
2. Use that identical frozen IS list for every symbol, even though later
   symbols start days later. Do not roll each symbol's IS end independently.
3. Record `is_as_of`, `queue_started_at`, and the ordered IS dates in the
   manifest.
4. Do not inspect or tune against performance after `is_as_of` while the
   symbol queue is running.
5. After all CME symbol sweeps finish, define the common OOS window as the
   approved complete sessions strictly after `is_as_of` through the latest
   completed session at evaluation time.
6. Preflight and report OOS data gaps separately, then run the frozen
   finalists on that common OOS list.

Because the queue takes many days, this naturally creates fresh OOS data
without making the IS window stale at launch. Record the number of accrued
OOS sessions and treat a short OOS read as provisional; the user decides
whether enough sessions have accumulated for a promotion decision.

Do not inspect a holdout, change the grid, and continue calling it holdout.
Once it influences tuning, it is training evidence.

---

## 3. Run identity and fresh-versus-resume policy

### 3.1 Scratch workspace layout

Use one stable scratch hierarchy for all future cycles:

```text
~/scratch/cme_retrains/
  run_<batch_date>/
    <YYYYMMDD>_<slug>/                 # one sweep/run identity
  deploy/
    run_<decision_date>/               # immutable staged snapshot
      sources/pk_<symbol>.json         # normalized per-symbol picks
      by_machine/pk_cme_<machine>_<instance>.json
      STATS.md                          # metrics, provenance, hashes
    current -> run_<decision_date>      # created/changed only at cutover
```

Start new sweeps inside this hierarchy. Do not put generated PK files under
`overmind/for_live/`, `overmind/playbooks/`, or any other repo-tracked path.

Run manifests and evaluation records commonly contain absolute workdir paths.
Do not move a completed legacy run merely to make its path look canonical; a
symlink from the canonical hierarchy is acceptable. New runs must use the
canonical path from the start.

`deploy/run_<decision_date>/` is a self-contained candidate snapshot.
Packaging it does not make it live. Do not create or repoint `deploy/current`
until the user authorizes cutover and overlapping live processes have been
reconciled. Preserve older dated snapshots for rollback.

A run identity is the tuple of:

- pktrade binary SHA-256;
- repository commit and relevant dirty source state;
- traded symbol and remote/tradecall wiring;
- source PK and vars file hashes;
- exact ordered search-date list and blacklist;
- grid, random-sample count/seed, chunk size, and scoring policy;
- host inventory.

Write these fields to `<runroot>/manifest.json` before launch. Use a workdir
name containing at least the `as_of` date, symbol, and first eight characters
of the shipped binary hash.

### Start fresh

Use a new, nonexistent workdir if any run-identity field changed. This is
mandatory after rebuilding pktrade or changing the date window.

Remote `SimVariations` loads `scratch/<variant>/sim_results.json` from an
existing workdir even without `--resume`; omitting the flag is not enough to
guarantee a clean run. A new workdir is the guarantee.

Preserve an invalid/stale run for audit by labeling or moving it out of the
active runroot. Do not delete material results unless explicitly requested.

### Resume

Resume only when every run-identity field is identical and the interruption
belongs to that same logical run. Before resuming:

1. Check `tmux ls` and `pgrep -af SimVariations` to prevent double dispatch.
2. Read `remote_state.json`, `remote_chunks.jsonl`, `progress.json`, and the
   launcher tail.
3. Confirm the manifest binary hash, dates, config hashes, and sample count.
4. Use the same workdir and `--resume`.

A submitted-but-unscored chunk is recoverable from its ledger. A cancelled
chunk whose remote processes were killed must be rerun. Heartbeats describe
progress and must not replace the chunk's lifecycle status.

---

## 4. Preflight gates

### 4.1 Confirm scope, priority, and pool state

Record the symbol order requested by the user. Check for active local queues
and farm work before starting:

```bash
tmux ls
pgrep -af 'SimVariations|run_.*queue'
./overmind/swarmhost/status.py
```

Use a sequential, state-gated queue. Preflight and arm the next approved
symbol before the current sweep finishes. A successor starts only after its
predecessor exits zero and writes `state: done`; once that condition is met,
the handoff starts immediately without waiting for a manual status check.

### 4.2 Build both local and remote pktrade

When pktrade C++ or embedded secmaster data changed, rebuild both binaries:

```bash
./compileit.sh -c pktrade
./jammybuild.sh -c pktrade
```

Then:

1. Record SHA-256 and timestamps for `bin/pktrade` and
   `jammy.bin/pktrade`.
2. Run `check_binary_freshness()` on the selected remote binary. Any warning
   is a hard failure.
3. Run a harmless `--help` smoke test.
4. Run a representative one-day config smoke when wiring, secmaster, signals,
   or sim mechanics changed.
5. Hard-code the expected remote binary hash in the launcher and refuse a
   mismatch.

The experiment ID suffix must match the first eight characters of the
recorded shipped-binary hash.

### 4.3 Audit the current config, not a historical routing table

For every pktrader, verify and save a parsed audit containing:

1. `traded_symbol` is the intended HIP3 product.
2. `ordex.remote_sig` names the intended `remote_mid_*` signal.
3. That signal subscribes to the intended leading symbol and market.
4. `ordex.trade_caller` names a `FinalTempo` whose symbol and market match
   that same leading book.
5. The relative signal is separate. BTC may be a relative leg, but it must
   not accidentally be the tradecall clock.
6. No donor symbol names survive cloning.

Examples: SP500 normally clocks from ES/TopBookCme and XYZ100 from
NQ/TopBookCme. Treat these as checks, not configuration generation rules.
FX routing changed during earlier work from proxy books to corrected CME
futures configs (`6E`, `6J`, `6B`); therefore FX must always be resolved from
the current intended config and audited afresh.

The previous BTC-clock error changed combined corrected OOS from negative to
strongly positive. Results without this audit are invalid.

### 4.4 Compare CME and proxy anchors without mixing trials

Prefer the CME leading symbol when a validated CME route exists. A proxy-book
trial is allowed as a separate comparison branch, not as an unrecorded config
substitution.

For FX, the default primary branch is:

- EUR: `6E / TopBookCme`;
- JPY: `6J / TopBookCme`, with quote-mid inversion enabled because 6J is the
  reciprocal of the HIP3 USDJPY-style price;
- GBP: `6B / TopBookCme`.

The optional comparison branch may use the validated `EUR`, `JPY`, or `GBP`
proxy on `TopBookEquity`. For other products, add a proxy branch only when an
intended alternative config exists and passes the same wiring and quote-scale
smokes.

Make an anchor comparison controlled:

1. Use the same recent date list, pktrade hash, vars grid, deterministic
   sampled variant IDs, scoring policy, and holdout policy.
2. Preflight both branches independently, then use their common complete date
   intersection. Do not compare different date windows.
3. Give every branch a unique manifest identity, config hash, workdir, and
   experiment ID. Never share or pool per-variant caches.
4. Smoke-test price level, direction, trade count, and transformations such
   as JPY inversion before the full sweep.
5. Compare paired daily results as well as aggregates. Label the winner as a
   CME-anchor or proxy-anchor result; do not silently replace the live source.

Record `anchor_family`, `remote_symbol`, `remote_market`, and any transform in
the manifest and kickoff report.

### 4.5 Check sizing and healthy quoting

Run one representative day before a large sweep. Confirm:

- the intended symbols load;
- the config quotes and trades at a plausible rate;
- no secmaster/assert/config errors occur;
- notional and position sizing are coherent.

For FX, use notional sizing consistently. Never sweep `order_size` against a
fixed `max_pos` without checking the ladder ratio, and never compare raw unit
sizes across instruments with very different price scales.

### 4.6 Enumerate and validate market data

First derive the exact recent date list from Section 2. Then run
`discover_for_conf(..., include_prev_day=True)` using the current local
pktrade and intended config.

Require every dry-run-discovered subscription, including:

- the Hyperliquid traded leg and required channels;
- the remote CME/reference book;
- configured relative/auxiliary subscriptions;
- the prior-session files needed for warmup;
- the correct CME contract mapping across rolls.

Fetch missing local files through the normal S3 fallback, then repeat
discovery. If anything remains missing, produce a decision report containing:

- total missing files and affected simulation dates;
- subscription/market and representative paths;
- whether each gap looks like an exchange holiday, expected no-session day,
  unavailable warmup, or unexpected capture loss;
- which symbols are affected and whether comparison windows would diverge;
- the proposed action and resulting exact date list.

The user makes the yes/no decision. Typical choices are:

1. blacklist the whole affected session for every compared symbol;
2. accept a shorter window without replacing the date;
3. extend the start backward to preserve the target session count; or
4. explicitly approve a partial-data run with `--allow-missing-data`.

Prefer excluding the whole affected session over scoring a partially observed
day. One or two documented missing or holiday sessions are acceptable when
approved. Store the report and decision in the run manifest. Never pass
`--allow-missing-data` merely to make a detached launcher continue.

The primary-symbol `dates_avail()` check is not sufficient because it does
not prove that the remote leading book and warmup files exist.

### 4.7 Dry run

Run `SimVariations.py --dry-run` with the exact production inputs, dates,
blacklist, sample count, and chunk size. Use a separate preflight workdir so
the production workdir remains nonexistent.

Confirm:

- printed dates exactly equal the manifest;
- symbol and config are correct;
- total variants and jobs are expected;
- host inventory is intended;
- no freshness or wiring warning remains;
- every missing-data warning exactly matches the manifest's approved decision.

Only then create the production workdir and launch.

---

## 5. Launch pattern

Substitute values from the manifest; do not paste dates from this or another
study:

```bash
~/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
  --workdir "$FRESH_WORKDIR" \
  --conf "$VARS" \
  --pk "$PK" \
  --sym "$SYMBOL" \
  --start "$START_INCLUSIVE" \
  --end "$END_EXCLUSIVE" \
  --blacklist "$BLACKLIST" \
  --random-sample "$N_VARIANTS" \
  --remote \
  --chunk-size 50
```

Fresh launches do not use `--resume`. Do not use `--allow-missing-data` unless
the kickoff manifest records the reported gaps and the user's explicit
approval. Run the queue inside a named tmux session and append to durable
launcher logs. Keep the queue's priority order in the manifest.

Experiment IDs now include symbol/config content, but still use unique
per-symbol workdir basenames. Sequential execution remains preferred because
all symbols share the same 48 cores.

---

## 6. Monitor, stop, and recover

Monitor all four sources of truth:

```bash
tmux ls
pgrep -af SimVariations
jq . "$WORKDIR/remote_state.json"
tail -n 30 "$WORKDIR/launcher.log"
tail -n 10 "$WORKDIR/remote_chunks.jsonl"
```

Track cached/scored variants, current chunk jobs, failures, timestamps, and
log growth. A stale state file alone does not prove a dead dispatcher; inspect
tmux/processes and the ledger before launching another copy.

To stop, send SIGINT to the foreground SimVariations process. Its handler
stops dispatch and kills remote pktrade processes. Wait for `state: cancelled`
and confirm the reported remote kill count. Use the scoped swarmhost kill tool
only if the normal handler could not run.

Do not start a second dispatcher until the first is gone or its recovery path
has been reconciled.

---

## 7. Completion and integrity gates

Before scoring candidates or advancing the queue, require:

1. process exit code zero and `remote_state.json` state `done`;
2. metadata binary hash equals the manifest hash;
3. metadata dates exactly equal the manifest dates;
4. expected variant count is present exactly once;
5. failures are zero, or every exception is investigated and approved;
6. every row has `n_dates_ok == expected included-date count`, unless the
   manifest explicitly approved partial dates; incomplete rows must remain
   flagged and cannot be ranked as comparable to complete rows;
7. sampled account rows contain the intended traded symbol;
8. source configs, vars, blacklist, manifest, logs, and audits are retained.

The account-symbol audit is a defense against the historical cross-symbol
remote-cache contamination bug. Never rank partial-window rows against
complete rows.

---

## 8. Candidate evaluation and promotion

Use this order:

1. Reject wiring-invalid and unapproved incomplete-date results. Keep any
   explicitly approved partial-data result visibly labeled and compare it
   only on a like-for-like observed-date basis.
2. Rank primarily on `avg_pnl`/`net_pnl` after commission, not closed PnL or
   `sim_score` alone.
3. Check positive-day fraction, trade count, fill rate, daily path, worst day,
   and known stress days.
4. Split the retained path into first and second halves. Report both average
   PnL and positive-day counts so a decline in winning-day magnitude is not
   confused with a collapsing hit rate.
5. Report median daily PnL, average winning/losing day, and the share of total
   PnL supplied by the best one and best three sessions. A candidate whose
   recent improvement disappears after removing one day is outlier-driven.
6. Prefer broad positive parameter neighborhoods over isolated lucky rows.
   Record both the local median and the number of sampled neighbors; a median
   computed from only one or two adjacent rows is not broad evidence.
7. Preserve several finalists, not only the top in-sample row.
8. Run finalists on declared corrected-clock holdout/OOS dates.
9. Treat a short or unavailable OOS window as provisional. Report its exact
   dates and gaps, then obtain the user's explicit decision to wait, decline,
   or judge from IS. If IS is accepted, label the selection `IS-approved` and
   retain short OOS results as diagnostic only.
10. If building a package, run combined-book OOS; summed individual OOS is
    not the final portfolio test.
11. Record source and generated configs, daily rows, audits, results,
    rejection reasons, user approvals, and promotion rationale.

Symbol-specific priors in the historical studies are hypotheses to retest on
the current window, not permanent truths.

---

## 9. Deployment packaging and cutover

Build deployments as dated, scratch-only snapshots:

1. Create a new `~/scratch/cme_retrains/deploy/run_<decision_date>/` with
   `sources/`, `by_machine/`, and `STATS.md`. Never mutate an older snapshot.
2. Copy each approved variant's generated `pk_full.json` into
   `sources/pk_<symbol>.json`. Record the variant, original path, source hash,
   IS/OOS evidence, and user decision.
3. Force every newly promoted pktrader to `size_mult=1`, even if its donor or
   current live config uses a larger multiplier. Ramp up only after several
   days of acceptable live behavior.
4. Run `overmind/strat_main/tools/check_maxpos.py --inplace` on every source.
   The tuned `ordex.tgt_maxpos_notional` is the source of truth for
   `risk.max_notional`; also satisfy the current-price-implied unit
   `risk.max_position` check.
5. Combine normalized sources with
   `overmind/strat_main/stratbuilder/PKatamari.py`. Its deterministic
   signal/tempo namespacing prevents cross-symbol definition collisions.
6. Validate the combined file before deployment:
   - JSON parses;
   - traded symbols are unique and expected;
   - tuned ordex blocks and CME wiring match the selected sources;
   - all signal/tempo references resolve;
   - every risk cap matches its ordex target;
   - every new pick remains at `size_mult=1`;
   - the exact intended pktrade binary loads it with `--dry-run`;
   - dry-run subscriptions contain every intended traded leg and CME anchor;
   - binary, source, and combined-config hashes are recorded.
7. Run a combined-book smoke/OOS check when the package will share one
   process or capital pool. Individual PnL sums do not prove portfolio
   behavior.
8. Inventory active launchers, crons, tmux sessions, hosts, and configs for
   every included traded symbol. Do not start the package beside an existing
   process quoting the same symbol.
9. Write `STATS.md` with selections, daily-path metrics, evidence dates,
   exclusions, risk totals, validation results, and unresolved cautions.
10. Cut over only with explicit authorization. Create or repoint
    `deploy/current` only after the actual live launcher is confirmed to read
    that snapshot (or after the snapshot is safely synchronized to its
    declared live destination).
11. Keep prior `run_<date>/` snapshots intact. Roll back by restoring the
    previous declared snapshot and verifying the launcher/process state.

Never call a staged snapshot deployed merely because its configs exist.

---

## 10. Failure modes learned in prior runs

| Failure | Prevention |
|---|---|
| Old May/June dates copied into a July run | Derive a rolling window ending on the declared `as_of`; store and announce every date. |
| Local pktrade rebuilt but stale Jammy binary shipped | Build both; freshness gate and hash-lock the launcher. |
| New binary run restored old variant caches | Use a new nonexistent workdir whenever run identity changes. |
| Remote leading signal correct but tradecall clocked by BTC | Parsed remote-signal/FinalTempo audit before sim or OOS. |
| Primary book present but remote/warmup dependency missing | Full `discover_for_conf` preflight, missing-data examples, and an explicit user decision. |
| CME and proxy trials used different dates or shared caches | Controlled branches with a common date list and separate manifests/workdirs. |
| Partial jobs produced attractive but incomparable rows | Require full `n_dates_ok` and investigate all failures. |
| Concurrent symbols cross-served cached results | Unique experiment/workdir identity, sequential queue, sampled account-symbol audit. |
| Cancelled dispatcher left remote work burning cores | Stop with SIGINT and verify `state: cancelled` plus remote cleanup. |
| FX unit sizing produced a one-clip ladder and inconsistent risk | Use coherent notional order/max-position sizing and smoke-test quoting. |
| Best in-sample row failed the next regime | Preserve finalists and collect fresh corrected-clock OOS; label any explicit IS-only decision provisional. |
| Generated promotion configs were added under `overmind/for_live/` | Keep all source, combined, stats, and rollback artifacts under `~/scratch/cme_retrains/deploy/`. |
| Sim-template `size_mult` leaked into a new deployment | Normalize every new pick to `size_mult=1` in the dated source snapshot. |
| Risk-layer limits remained looser than tuned ordex targets | Run `check_maxpos.py` on every source and the combined config. |
| A combined config double-quoted symbols already live elsewhere | Inventory all active configs/processes and remove overlap before cutover. |
| Mean PnL was driven by one exceptional session | Report median, top-one/top-three concentration, and first/second-half behavior. |

---

## 11. Required kickoff report

Before starting remote work, report:

- symbols and priority order;
- inclusive `as_of` and exclusive CLI end;
- exact search dates and the holdout/OOS policy; append exact OOS dates when
  they become available after the queue;
- excluded dates with reasons;
- binary path and SHA-256;
- source PK/vars hashes;
- wiring audit result;
- required/existing/missing market-data counts, representative missing paths,
  affected dates, and the explicit yes/no decision;
- variant and job counts from dry run;
- fresh workdir paths;
- explicit statement that old results will or will not be resumed.

This report is the final human-readable guardrail. If it disagrees with the
manifest or command, do not launch.

---

## References

- `overmind/swarmhost/PLAYBOOK.md` — farm operation and recovery.
- `overmind/studies/relwide_cme_gf0_search_20260616.md` — full-symbol setup,
  binary/data/cache failures, FX sizing, and June OOS history.
- `overmind/studies/relwide_cme_continuity_20260608.md` — wiring guardrail,
  value system, result reading, and restart discipline.
- `overmind/studies/relwide_cme_search_learnings_20260609.md` — search path,
  symbol-level lessons, and promotion checklist.
- `overmind/studies/relwide_equities_autosearch_playbook.md` — rolling-window
  length and full-data validation lessons.
