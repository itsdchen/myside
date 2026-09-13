# Consumer-side freshness improvements (2026-06-26)

Driven by the n2 variant study (see `hl_node_timeline.md` 2026-06-26 15:55
entry): non-validator nodes have a structural every-2000-block slow-apply
event (~7-9 s in the slow pipeline, translating to ~1-9 s of fast-pipeline
silence). HL's public WS — sourced from validators, not from non-validator
state — does **not** experience these stalls, so during every slow-apply
window the public WS is more current than our node feed.

Current pkmultifeed behavior wastes that. Threshold for switching to WS is
5 s of silence/lag, so the typical 1-5 s slow-apply window leaves consumers
emitting stale node data while WS is sitting there with current data.

## Two-phase plan

### Phase 1 (immediate): lower fallback thresholds in pkmultifeed

**File**: `src/pktrade/feed/pkmultifeed.cc`

**Change**: default `fallback_silent_s` and `fallback_lag_s` from `5.0` to
`1.5` (lines 146-147).

**Why 1.5 not 1.0**: ZMQ jitter, GC pauses, and brief consumer-side scheduling
hiccups can spuriously trip a 1.0 threshold. 1.5 s comfortably exceeds the
worst-case observed ZMQ-jitter floor while still catching ~90% of the
[1,5) s daily silence events (we measured ~210 daily gaps in [1,2) s,
~17 in [2,3) s, ~27 in [3,5) s).

**Expected effect**: switch events increase from ~31/day (only ≥5 s events)
to ~285/day (~12/hour). One-shot alert email still only fires once per
process run, so no email spam.

**Risks / caveats**:
- Frequent flips between node and WS may stress the per-sym monotonic gate's
  `gate_mtx` more (more book-snapshot map lookups). Negligible in practice
  but worth watching with a CPU sample post-deploy.
- Consumers reading the freshness-window log will see different "ahead"
  ratios — node-ahead count will fall because more silence windows now
  trigger WS-ahead.

**Rollback**: revert the constant or set `fallback_silent_s` / `fallback_lag_s`
explicitly in the consumer's config.

**Test plan**: deploy to one consumer (gf0?) first, watch for one stall
cycle (~2:20 of node silence), confirm WS picks up cleanly, no consumer-side
errors.

### Phase 2 (real fix): freshest-source arbitration

**Goal**: stop thinking of it as "primary + fallback with threshold". Both
feeds always running, every emit per-sym compares incoming `event_time`
against the most-recent already-emitted for that sym; emit only if newer.

#### What's already in place (don't have to build)

- Per-sym monotonic gate (`HLNodeShared::last_book_event_time_by_sym` and
  the `gate_mtx`) — already used as a one-way valve to prevent WS regression.
- `last_node_trade_time_ms`, `last_ws_trade_time_ms`, `last_event_time_ms`,
  `last_ws_book_event_time_ms` — atomics tracking freshness per path.
- Sample logger (every 5 min) recording node-vs-WS chain-time-ahead ratios.
- **Trades already do freshest-source arbitration** via
  `advanceHLPublishedTradeGateLocked` (line 212): per-(sym, trade_time),
  first source to claim a chain_time slot wins; subsequent same-time
  messages from the other source are dropped. The `publishing_from_node`
  check on trades is *additional* and currently masks this behavior.
- **Trades on gf2 telemetry (06-24 10:05 window)**: WS was ahead 4.2s on
  average, worst case 34s. Same window node was ahead in some samples.
  The existing arbitration logic *would have* taken the freshest from
  each source if not for the `publishing_from_node` gate.

#### Reality check: minimal viable change

Phase 2 is mostly about **removing the `publishing_from_node` gate**
from the publish decisions. Both paths already track shared per-sym
gates; both already advance them. The flag just artificially binds
publishing to one side.

#### As-shipped scope (2026-06-26)

- `publishing_from_node` atomic renamed to `node_healthy`. Now purely
  informational — drives the one-shot unhealthy email + the periodic
  freshness-window log, no longer gates publishing.
- All four publish sites (WS books, WS trades, node books, node trades)
  removed the health-flag check. Per-(sym, chain_time) gate is the only
  decision.
- Health-thread per-flip log rate-limited (`LOG_EVERY_N(INFO, 50)`) to
  avoid spam under the new 1.5 s threshold.
- Unhealthy alert email body updated to describe per-message
  arbitration semantics.
- No config flag — freshest-source is the only mode. Rollback is by
  binary revert.

#### What needs building

1. **Generalize the monotonic gate**: today it gates WS based on the node's
   `event_time`. Generalize to "max(event_time seen across paths) is the
   gate; any path's message with `event_time > gate_value` is emitted and
   becomes the new gate."

2. **Per-sym gate keyed by (sym, msg_type)**: today it's per-sym for books.
   Trades need their own gate (`(sym, trade_id)` arguably, but `(sym,
   event_time)` may be enough if HL doesn't produce two trades with the
   same chain-time per sym).

3. **Remove or repurpose `publishing_from_node`**: today it's a global
   on/off switch. Under freshest-source, every message stands on its own —
   no global flag. Health thread reduces to:
   - Track per-sym staleness for alerting / metric only.
   - Detect "complete silence on both paths" (something broken in the
     consumer process itself) and alert separately.

4. **Handle the case where node and WS chain-times tie**: prefer node (we
   built it; WS may have slightly different framing). Or alphabetical / a
   tiebreaker pinned in code. Doesn't matter much, but should be deterministic.

5. **Test surface**: add a `--print-only` / dry-run mode that logs both
   paths' decisions per emit ("kept" vs "dropped, stale") so we can verify
   on a known-mixed workload.

#### Failure modes to design for

- **WS publishes stale `event_time`**: if HL's WS occasionally emits an old
  snapshot (we've seen this in observability data — the freshness window
  sometimes logs `mn` deeply negative), it would never overtake node
  freshness, just silently be discarded. Fine.
- **Node publishes ahead-of-WS data we've never seen on WS**: same gate
  permits it. Fine.
- **Both feeds silent**: alert. Same as today.
- **`event_time` clock skew**: chain-time is the chain's view, so all paths
  should agree to within message-framing precision. Sub-millisecond skew
  is fine for our ordering.

#### Where this lives in code

- `src/pktrade/feed/pkmultifeed.cc` — same file as Phase 1
- Specifically: the SUB relay (around L1450) and the WS handler (search for
  `last_ws_book_event_time_ms` assignment) need a shared "should I emit?"
  predicate replacing today's `publishing_from_node` check.

#### Estimated scope

- ~150 lines of C++ changes, mostly factoring existing gate logic into a
  unified predicate
- 1 day of work, 1 day of validation
- Validation = side-by-side run with current pkmultifeed on the same
  consumer, compare emit timing and message identity per sym

## Out of scope (intentionally)

- **Cross-process consumer changes** (e.g., updating downstream strategies to
  handle slightly different message cadence). The wire format and message
  contents stay identical; only the *timing* of emission changes. Existing
  consumers shouldn't need updates.
- **Removing the WS fallback as a separate concept in consumer configs**.
  We can ship Phase 2 with WS still a "fallback" semantically (in config
  it's still called `fallback`) — the runtime logic is what changes.
- **Cross-feed dedup**. If node and WS both deliver "the same" event with
  identical chain-time, the freshest-source predicate naturally emits
  whichever arrived first then drops the duplicate. No explicit dedup
  needed.

## Open questions

1. **Does HL's public WS actually deliver fresher data than our node during
   slow-apply windows?** Inferred from architecture (WS is validator-sourced,
   node has the 2000-block stall). Worth confirming by looking at the
   existing freshness-window log on gf0/gf2 for the last few days — what
   does the `ws_ahead / tied / node_ahead` ratio actually look like during
   different time-of-day windows?
2. **Phase 2 should ideally land before Phase 1 is dialed lower** (e.g., to
   1.0 s) — at lower thresholds the cost of the global flag flipping
   becomes more pronounced. 1.5 s is conservative enough that Phase 1 buys
   most of the win without forcing Phase 2's timeline.

## Related files

- `src/pktrade/feed/pkmultifeed.cc` — consumer logic (Phase 1 + 2)
- `overmind/strat_main/pyfeed/pymultifeed.py` — Python sibling (parity
  follow-up; same logic, same defaults)
- `overmind/studies/hl_feed/hl_node_timeline.md` — V1 deploy + variant
  study summary (06-26 15:55)
- `overmind/studies/hl_feed/ws2_open_items.md` — "freshest-source
  arbitration" is already on the Strategic backlog; this doc concretizes
  what that means
