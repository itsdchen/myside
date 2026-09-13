# HL node performance consolidated notes (2026-06-23)

This is the current start-here note for the HL node performance work.
It consolidates the older snapshot-stall, n2, n3 tmpfs, gossip-peer, and
external-review notes into one decision-oriented handoff.

## Bottom line

We found two distinct failure modes:

1. **Snapshot-related contention**: the slow pipeline periodically writes
   a large ABCI state snapshot. This used to correlate with many
   >=5 s fast-feed gaps.
2. **Peer-network outliers**: the active upstream peer dies or stalls,
   then our node cycles through bad candidates, `Peer full` responses,
   private/unroutable peers, or slow reconnect paths.

The snapshot problem has materially improved. The peer-network problem
is separate and not fixed by disk, THP, tmpfs, or snapshot tuning.

Best historical test-node configuration observed before the temporary
test boxes were torn down:

```text
instance: r7i.4xlarge, 500 GB gp3 at 1000 MB/s / 4000 IOPS
snapshot tmpfs: /mnt/hl-snap-tmpfs, 16G
stream tmpfs:   /mnt/hl-stream-tmpfs, 48G
gossip:         n_gossip_peers=8, try_new_peers=true,
                split_client_blocks=false, curated root list
node args:      --serve-info
                --write-fills
                --write-raw-book-diffs
                --stream-with-block-info
                --replica-cmds-style recent-actions
                --disable-output-file-buffering
```

Notably, **`--write-hip3-oracle-updates` was removed** in that test
configuration because the current required outputs are raw book diffs and
fills, and repo grep did not find code consumers for
`hip3_oracle_updates_streaming`.

Last recorded check from that test configuration, 2026-06-23 22:19 UTC:

```text
service: active
raw book latest lag: 0.190 s
fills latest lag:    0.190 s
wall-clock lag:      0.211 s
snapshot tmpfs:      5.0G / 16G
stream tmpfs:        5.2G / 48G
```

Do **not** promote this to n1 yet. It is the best-looking test variant,
but it still needs more snapshot samples and a planned maintenance
decision.

Temporary test infrastructure note: n2 and n3 were short-lived EC2 test
machines. They have been terminated and their root volumes removed. Do
not use old SSH details or IP addresses; keep only the measurements below.

## Production decision: keep n1 hardware

We are **not planning an n1 machine migration**. The production instance
should stay on the current r6i.4xlarge/savings-plan setup unless future
evidence changes materially.

Rationale:

- The largest proven win, gp3 throughput 250 -> 1000 MB/s, is already
  applied on n1.
- r7i/n3-class hardware appears incremental after the disk fix, not a
  large enough delta to justify abandoning the committed n1 economics.
- The remaining peer-network outliers are not fixed by faster local
  hardware.
- n1 is production and known-good; the experimental boxes should absorb
  restart/config risk.

The only current n1-local production candidates are:

1. **THP=always**: modest positive test result; reversible but
   system-wide.
2. **Flags-only change**: `--replica-cmds-style recent-actions` and
   removing unused `--write-hip3-oracle-updates`.
3. **Snapshot tmpfs**: put `periodic_abci_states` on tmpfs with aggressive
   cleanup/monitoring.

All three still need an explicit n1 maintenance decision before
promotion.

## Safety boundary

- **n1 is production/read-only** unless explicitly approved.
- **n2/n3 are gone**; they were temporary test machines, not standing
  comparators.
- Any future machine experiment requires provisioning a new temporary
  box and recording fresh identifiers in the operational ticket/session,
  not in this long-lived note.

## Production state and historical test boxes

### n1 production

- Instance: `i-0fade2737acb92dd2`
- Type: `r6i.4xlarge`
- Public/private: `43.207.110.196` / `172.31.37.166`
- Root EBS: 500 GB gp3, **1000 MB/s**, 4000 IOPS
- Status: production feed source; read-only for this test set
- Important applied win: EBS gp3 throughput bump, 250 -> 1000 MB/s

### n2

- Temporary test box used for i4i/r7i/THP/flag tests.
- Terminated after the test batch; keep only the measurement rows below.

### n3

- Temporary test box used for tmpfs, gossip/root, and final flag tests.
- Terminated after the test batch; keep only the measurement rows below.
- Historical tmpfs setup: 16G for `periodic_abci_states`; 48G for raw
  book/fill streams; cleanup timers kept snapshots trimmed and stream
  files to current plus previous UTC hour.

## Snapshot performance

The slow-pipeline snapshot apply duration improved materially:

| stage | config | snapshot apply median | read |
|---|---|---:|---|
| baseline | n1 r6i + gp3 250 MB/s | 32.43 s | starting point |
| E12 | n1 r6i + gp3 1000 MB/s | 26.40 s | proven and kept |
| E15 | n2 i4i + NVMe | 27.87 s | rejected; NVMe did not help |
| E13 | n2 r7i + gp3 1000 | 24.80 s | DDR5/r7i helped modestly |
| E17 | n2 r7i + THP=always | 24.00 s | modest additional win |
| E18/E19 | n3 r7i + snapshot tmpfs | about 22.12 s | promising, small sample |
| E25 | n3 flags, no HIP3 | 22.249 s | first valid sample only |

The **big proven production win** is E12: n1 gp3 throughput at
1000 MB/s. This removed the observed snapshot-correlated >=5 s fast
stall population in the post-change window.

The **best-looking historical test-node stack** was tmpfs plus the
flags-only change. It needs more samples before promotion.

## What snapshots do and do not mean

Snapshot apply duration does **not** mean the entire node freezes for
22-25 seconds. It is a slow-pipeline measurement.

The live/fast pipeline can keep moving during snapshot work. The trading
problem is not the snapshot duration itself; it is whether snapshot work
creates shared contention that produces fast-pipeline gaps large enough
to trip consumers.

External review correctly sharpened the metric:

```text
primary metric = max fast-pipeline gap during each snapshot window
secondary proxy = snapshot apply_duration
```

Action item: rederive this `max_fast_gap_during_snapshot_window` metric
for the existing experiment windows so every hardware/flag result is
reported against the trading-relevant measure, not only
`apply_duration`.

## Peer-network story

Peer-network outliers are separate from snapshots. Example from n1 on
2026-06-23:

```text
35.77.73.152  -> early eof
35.79.2.229   -> Peer full
35.78.199.36  -> accepted and became active source
```

This pattern matches historical peer outliers: active peer dies, then
the node cycles through bad or saturated candidates before recovering.

Useful model:

```text
The metric is time-to-next-good-serving-peer after active peer failure.
```

`reserved_peer_ips` does **not** solve this. It is an inbound/VIP
allowlist, not an outbound preferred-peer mechanism.

## Gossip/root experiments on temporary test box

Historical best-looking gossip/root test config:

```text
n_gossip_peers = 8
split_client_blocks = false
try_new_peers = true
root_node_ips = curated list
```

Curated roots:

```text
202.182.115.118
35.75.146.229
35.77.73.152
47.243.101.107
5.104.86.44
54.178.38.245
57.181.193.102
64.34.94.159
```

Gossip/root results:

| test | result | decision |
|---|---|---|
| `n_gossip_peers=8` | compatible; best clean settled window: raw/fill p99 0.248 s | test-only positive |
| `n_gossip_peers=16` | accepted, but restart noisier and settled p99 worse | reject |
| default / `None` | worst reconnect path; private `172.31.39.48` no-route loop | reject |
| `split_client_blocks=true` | compatible, but p99 ~1.1-1.2 s | reject |
| curated roots + split true | cleaner acquisition but still bad p99 | reject split true |
| curated roots + split false | healthy; p99 ~0.51-0.53 s in one window | observe |

Read: curated roots are a reconnect-hygiene candidate, not a proven
steady-state speed win. The best original-root `n_gossip_peers=8`
settled sample had lower p99, but curated roots avoided several bad
classes in reconnect tests.

## Output/flag experiments

Flags that matter:

- Keep `--stream-with-block-info`. The publisher depends on block
  metadata.
- Keep `--disable-output-file-buffering`. Previous work showed file
  buffering added feed latency.
- Do not use `--batch-by-block` for the current publisher path.
- Best historical flag variant used `--replica-cmds-style
  recent-actions` and removes unused HIP3 output.

Results:

| test | result | decision |
|---|---|---|
| `--batch-by-block` | required removing `--stream-with-block-info`; raw/fill files stopped updating after catch-up | reject for current path |
| `--replica-cmds-style recent-actions` on n2 | 14 snapshots, median 22.72 s; partial positive | promising, incomplete |
| `recent-actions` on n3 with HIP3 | compatible; first snapshot 24.114 s; raw p99 0.284 s, fills p99 0.363 s | OK, but not best |
| `recent-actions` on n3 without HIP3 | compatible; first valid snapshot 22.249 s; raw/fill p99 0.248 s | current best test variant |

Test process used for the best historical flag variant:

```text
hl-visor run-non-validator --serve-info --write-fills --write-raw-book-diffs \
  --stream-with-block-info --replica-cmds-style recent-actions \
  --disable-output-file-buffering
```

## What is proven

- n1 gp3 throughput bump to 1000 MB/s helped a lot and is already kept.
- NVMe/i4i did not beat gp3 1000. Do not pursue NVMe/io2 for this
  snapshot bottleneck.
- Disk throughput beyond gp3 1000 is not the main remaining bottleneck.
- Snapshot path is dominated by userspace serialization/copy/page-fault
  work, not RocksDB or raw disk I/O.
- `--batch-by-block` is incompatible with the current raw/fill
  publisher path.
- `reserved_peer_ips` does not help outbound peer selection.
- `split_client_blocks=true`, `n_gossip_peers=16`, and
  `n_gossip_peers=None` should not be promoted.

## What is promising but not proven

- n3 snapshot tmpfs: looks like roughly 2 s saved versus the r7i/THP
  baseline, but sample size is still small and the ops surface is larger
  than a flag change.
- THP=always: modest positive result on the temporary r7i test box,
  24.80 s -> 24.00 s median snapshot apply. This is smaller than the
  disk win and was not tested directly on n1.
- `--replica-cmds-style recent-actions`: partial positive on n2 and
  clean first n3 samples, but not enough snapshots.
- Removing HIP3 output: repo grep found no code consumer; raw/fill
  output stayed healthy; first snapshot sample was good.
- Curated roots: better reconnect hygiene in n3 tests, but not proven
  to improve steady-state speed.

Evidence currently written down for the active n1-local candidates:

| candidate | evidence so far | missing before n1 |
|---|---|---|
| THP=always | temporary r7i test: 30 samples, median 24.00 s vs 24.80 s pre-THP | decide whether a system-wide kernel setting change is acceptable on n1; measure fast-gap impact if enabled |
| Flags-only: `recent-actions`, no HIP3 output | n2 `recent-actions`: 14 snapshots, median 22.72 s vs r7i+THP 24.00 s; n3 `recent-actions` compatible; no-HIP3 sample had first valid snapshot 22.249 s and raw/fill p99 0.248 s | more samples on a recreated test box or a tightly monitored n1 maintenance window; fast-gap-during-snapshot metric |
| Snapshot tmpfs | tmpfs stack around 22.12 s vs r7i/THP 24.00 s baseline; tmpfs occupancy observed at 5.0G/16G during final check | same fast-gap metric, cleanup/alert runbook, and explicit maintenance plan |

## What not to redo

- Do not retest `--batch-by-block` on the live publisher path unless
  building a separate compatibility harness.
- Do not promote `n_gossip_peers=16`.
- Do not assume default/None is safer than explicit `n_gossip_peers=8`.
- Do not promote `split_client_blocks=true`.
- Do not populate `reserved_peer_ips` expecting outbound peer pinning.
- Do not spend time on NVMe/io2/fsync-latency variants for this
  snapshot problem.
- Do not refer to n2/n3 SSH details or IPs; those machines were
  terminated after the test batch.

## Recommended next actions

1. **Keep n1 hardware as-is.** The savings-plan decision stands, and the
   largest proven win, gp3 1000 MB/s, is already applied.
2. **Compute fast-gap-during-snapshot-window** for all existing
   experiment windows. This is the trading-relevant metric.
3. **If we touch n1, choose one low-blast-radius change first.** Best
   order: THP=always if we want the smallest reversible test; flags-only
   if we want the most plausible software improvement; tmpfs only if we
   accept added cleanup/monitoring surface.
4. **Do not recreate n2/n3 by default.** Recreate a temporary box only if
   we decide the extra pre-production evidence is worth the cost.
5. **Ask HL the refined questions**:
   non-validator-mode snapshot relief, valid `replica-cmds-style`
   semantics, `batch-by-block` semantics, and outbound peer/allowlist
   coordination.
6. **Design freshest-source arbitration** for consumers. Keep node and
   WS hot and publish whichever has the freshest block per symbol. This
   is the local mitigation for peer-network outliers.

## Promotion candidates for n1

These require a planned maintenance window and more evidence:

1. **THP=always**: modest measured benefit; reversible, but system-wide.
2. **Flags-only**: `--replica-cmds-style recent-actions` and removing
   `--write-hip3-oracle-updates`.
3. **Snapshot tmpfs** for `periodic_abci_states`.

Suggested promotion order, if evidence holds:

1. **THP=always** if we want the smallest reversible production probe.
2. **Flags only**: `recent-actions` and no HIP3 output. More specific to
   the node behavior, lower blast radius than filesystem changes.
3. **Snapshot tmpfs**: likely helps snapshot duration, but has more ops
   surface area.

Gossip/root config is not an active n1 promotion candidate. The test
results are useful context, but do not treat gossip/root changes as a
near-term production decision.

## Source notes

This file is now the canonical performance note. The older dated
working notes were folded into this summary and removed from the
top-level study directory.

- `ws2_open_items.md`: broader WS2/n1 open-items tracker.
- `n1_navigation_guide.md` and `n1_provision_runbook.md`: ops/runbook
  material, not experiment conclusions.
