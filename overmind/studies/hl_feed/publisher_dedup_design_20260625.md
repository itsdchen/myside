# Publisher restart resilience — design proposal (2026-06-25, v5)

Status: **draft for user review, before any code changes.** v5 after
feedback. Earlier versions in git history:
- v1: per-tid sliding-window set for fills.
- v2: unified `highest_block_seen` integer for both modes.
- v3: v2 + cron-orchestrated clean restart for scheduled maintenance.
- v4: v3 + forward-gap detection + always-subscribed WS so
  recovery is a flag-flip rather than a resubscribe cycle.
- v5 (this): v4 + publisher-side bootstrap freshness gate using WS
  l2Book time as the reference clock.

## Background

When `hl-node` restarts, `hl-visor` loads the last local snapshot
(`abci_state.rmp`, taken every 10,000 blocks ≈ ~80 min on mainnet)
and catches up from peers to the current chain head. Catch-up takes
**~9 min** in the case observed on 06-25.

During catch-up, `hl-node` **re-emits raw diff events to the streaming
files** for every block between snapshot+1 and chain head. Confirmed:

- Books (06-25 11h): block `1048908000` at line 5,603,373 (pre-restart)
  and line 9,874,826 (post-restart catch-up).
- Fills (same hour): block `1048905000` at line 65,341 and line
  126,206.

A publisher tailing across that window applies every event twice,
silently corrupting L3 state (books) and emitting duplicate trades
(fills).

## Failure modes and protection mechanisms

| Failure mode | Frequency | Mechanism |
|---|---|---|
| **Backward replay**: visor restarts, catch-up re-streams blocks ≤ highest_block_seen | Per-restart | **L1a: dedup** — drop on `block_no <= highest_block_seen` |
| **Forward gap**: streaming-file byte loss or hl-node skipping blocks | Rare but silent today | **L1b: gap detection** — flag and trigger WS re-catch-up on `block_no > current_block + 1` |
| **Concurrent publisher + visor restart**: fresh publisher starts while visor is still replaying historical blocks | Rare but possible | **L1c: bootstrap freshness gate** — `--bootstrap` waits until stream `block_time` is within 3s of latest WS l2Book time before seeking/tailing |
| **Scheduled maintenance window** | Daily 4 AM EDT | **L2: cron** gates publisher startup on catch-up completion |

L1a, L1b, L1c, and L2 are complementary. L1 is the always-on
publisher-side correctness layer; L2 is the orchestration that
sidesteps L1 entirely for scheduled events.

## Source-of-truth observations from `hl_node_publisher.cc`

Both streaming files carry `block_number` and `block_time` at the
packet level. Verified on n1:

```
fills packet keys: ['local_time', 'block_time', 'block_number', 'events']
```

The book-diff handler already parses `block_number`
(`hl_node_publisher.cc:1591-1594`). The fills handler currently does
not.

A single block produces **thousands of packets**, not one. Sample
on n1: 5000 lines covered 3 distinct blocks. Dedup design must not
drop live packets of the in-progress block.

---

# Layer 1a: backward-replay dedup

## Design

Track `highest_block_seen` as a single `int64_t` per process. Drop any
packet whose `block_number <= highest_block_seen`. Update
`highest_block_seen` **lazily** — only at block transition — so during
live block N (multi-packet), all N-packets see `highest_block_seen =
N-1` and pass the check.

### Books mode

Add to `BookSet`:

```cpp
int64_t highest_block_seen = -1;
int64_t duplicates_dropped = 0;
int64_t gaps_observed = 0;
```

In `BookSet::advance_block`, ratchet `highest_block_seen` from the
outgoing `current_block`:

```cpp
void advance_block(int64_t new_block_no, int64_t new_block_time_ms) {
    std::lock_guard<std::mutex> g(mu);
    if (new_block_time_ms > 0) latest_block_time_ms = new_block_time_ms;
    if (current_block > 0 && current_block > highest_block_seen) {
        highest_block_seen = current_block;
    }
    current_block = new_block_no;
}
```

In `handle_line`, BEFORE the existing `block_changed` check
(`hl_node_publisher.cc:1600`):

```cpp
if (block_no > 0 && block_no <= bookset.highest_block_seen) {
    ++bookset.duplicates_dropped;
    return;
}
// Gap check — see Layer 1b below
```

### Fills mode

`run_tail_fills` doesn't currently maintain block state. Add the same
int64_ts as locals (or to `FillsStats`), parse `block_number` from
each packet, then apply the same dedup + lazy-transition logic:

```cpp
int64_t block_no = -1;
if (auto it = d.FindMember("block_number");
    it != d.MemberEnd() && it->value.IsInt64()) {
    block_no = it->value.GetInt64();
}
// Dedup
if (block_no > 0 && block_no <= highest_block_seen) {
    ++duplicates_dropped;
    return;
}
// Gap check (forward jump)
if (block_no > 0 && current_block > 0 &&
    block_no > current_block + 1) {
    ++gaps_observed;
    fmt::print(stderr,
        "[GAP] fills forward jump {} -> {} (gap={})\n",
        current_block, block_no, block_no - current_block - 1);
    // Fills has no WS reseed; log only. See "Fills caveat" in L1b.
}
// Lazy ratchet on block transition
if (block_no > 0 && block_no != current_block) {
    if (current_block > 0 && current_block > highest_block_seen) {
        highest_block_seen = current_block;
    }
    current_block = block_no;
}
```

Then the existing tid-pairing logic runs unchanged.

### Why `<=` works given lazy update

The dedup check runs **before** `advance_block`. So at check time,
`highest_block_seen` reflects the previous transition's outcome, not
the current packet's effect. Tracing one full cycle:

- Live block N-1 mid-stream: `current=N-1`, `highest=N-2` (set
  during the transition into N-1).
- Block N first packet arrives. Check sees `highest=N-2`,
  `block_no=N`. `N <= N-2`? No → pass. Then `advance_block` fires:
  ratchet `highest = current = N-1`; set `current = N`.
- Block N second packet. Check sees `highest=N-1`. `N <= N-1`? No
  → pass. No transition (block_changed false).
- Block N+1 first packet. Check sees `highest=N-1`. `N+1 <= N-1`?
  No → pass. `advance_block`: ratchet `highest = N`, set `current = N+1`.
- Replay during catch-up covers S+1..H. At dedup time `highest=N`
  (set on the transition into N+1). Replay blocks S+1..N all
  satisfy `<= N` → dropped. Block N+1 onward is `> N` → processed
  (mid-block re-apply, see below).

### One-block re-apply window (accepted)

Lazy ratcheting "graduates" block N into `highest_block_seen` only
when block N+1's first packet arrives. So whenever the visor restarts
while the publisher is "in" block N — whether mid-block (some packets
applied, others not) or fully done with N but no N+1 packets yet —
the replay re-applies block N. State at the moment of crash:
`current=N, highest=N-1`. Replay block N: `N <= N-1`? No → processed.

(The eager-ratchet alternative would set `highest=N` on N's first
packet, but then subsequent live N-packets would dedup against
themselves and get dropped. Lazy is the correct tradeoff; this
one-block re-apply is the cost.)

Effects of one-block re-apply:

- Books: `apply_new` / `apply_update` on known oids are idempotent.
  Only `apply_remove` on already-removed oids corrupts synth via the
  unknown-remove path. Bounded to one block. Recovery is automatic
  and doesn't involve L1b: for syms already in `caught_up_`, synth is
  empty so `synth_estimate_remove` is a no-op (zero corruption). For
  syms still in bootstrap, the WS reseed loop is already running and
  overwrites any spurious synth state on its next message.
- Fills: any tid whose both counterparties were already seen
  pre-restart gets emitted twice (~one block of duplicate trades).
  Tids with one counterparty pending pre-restart hit a self-pairing
  anomaly and get dropped — the trade is lost.

Layer 2 sidesteps this entirely for scheduled events (publishers
stopped during the catch-up window). For unscheduled visor
restarts, it's a bounded ~one-block cost that's far better than
today's ~9-min duplication.

---

# Layer 1b: forward-gap detection + WS re-catch-up

## The problem L1a alone doesn't fix

If `block_no > current_block + N_THRESHOLD` (i.e., a forward jump big
enough to be meaningful), the publisher is skipping ahead. Note we
compare against `current_block`, not `highest_block_seen`: with the
lazy ratcheting in L1a, `highest_block_seen` lags by one block at all
times, so using it as the floor would false-fire on every normal
transition. `current_block` is the actual most-recent block we
processed.

**Threshold required**: HL Mainnet's streaming file **omits packets
for blocks that have no book-diff events for any coin** — measured
~5-6% of blocks during a 60s live observation on n2 (3 of 62 unique
blocks gapped by 1). Without a threshold, the publisher would treat
every empty-block skip as a forward gap and call `force_recatch_up`
several times per minute, keeping every sym in perpetual bootstrap.
A real catch-up replay spans thousands of blocks (~5,300 observed in
the n2 systemctl restart test), so a threshold of **100 blocks**
cleanly separates the two cases without missing genuine problems.

Small skips below threshold are still counted (in `small_gaps_observed`)
for visibility but do not fire recovery.

Possible causes of a real forward jump:

- Publisher fell hours behind chain (so visor's snapshot is *ahead*
  of publisher's position; catch-up replay starts at S+1 > publisher's
  highest+1).
- Streaming file lost bytes due to disk / kernel weirdness.
- hl-node skipped blocks (shouldn't happen but cheap to defend
  against).

L1a doesn't catch this — forward jumps aren't dedup'd. The publisher
silently misses events in the gap window: resting orders created in
the gap are invisible to L3; trades in the gap are lost from fills
emit. The state diverges from chain truth with no recovery
mechanism.

## Always-subscribed WS

Change `ContinuousWS` so it **never unsubscribes** per-sym after
catch-up. The WS thread receives l2Book updates for every sym
continuously. The per-sym `caught_up_` flag now governs *behavior*
(reseed vs. ignore), **not subscription state**.

Code changes:

```cpp
// In ContinuousWS::handle_l2book, the existing early-return on
// caught_up_ stays — but the meaning shifts from "straggler
// post-unsub" to "we're in steady state, ignore this WS msg".

// REMOVE the ws_.send(make_l2book_unsub(coin)) line in the streak ==
// kMatchStreakRequired branch. Keep the caught_up_.insert(coin) and
// bookset_->clear_synth_for(coin) calls.
```

**Also change the on-Open handler** — today it skips already-caught-up
syms, which under the always-subscribed model would mean a WS
reconnect (network blip, HL drop, ixwebsocket auto-reconnect) returns
to a silent socket. We need every reconnect to re-subscribe to the
full universe so the steady-state flow is restored:

```cpp
// In on_message, WebSocketMessageType::Open branch:
// REPLACE the "subscribe only to !caught_up_" loop with an
// unconditional subscribe over all syms.
for (const auto& sym : syms_) ws_.send(make_l2book_sub(sym));
```

`caught_up_` then governs only what we *do* with incoming messages —
not whether we're listening for them.

New method on ContinuousWS:

```cpp
void force_recatch_up() {
    std::lock_guard<std::mutex> g(state_mu_);
    caught_up_.clear();
    match_streak_.clear();
    // No resubscribe needed — we never unsubscribed.
}
```

## Triggering from gap detection

`BookSet` doesn't currently know about `ContinuousWS`. Two options:

1. **Back-reference**: store a `ContinuousWS*` (or `std::function<void()>`
   callback) on `BookSet`, set from `run_tail` after `cws->start()`.
   `BookSet` calls it on gap.
2. **Flag-and-poll**: `BookSet` sets a `bool gap_detected` atomic.
   `ContinuousWS` checks it on each message and calls
   `force_recatch_up()` itself.

Option 1 is more direct; option 2 avoids the back-reference at the
cost of a single bool check per WS msg. Lean toward **option 1**
since it's one pointer and one nullptr-check, and we already have
`bookset` referenced from `ContinuousWS`.

In `handle_line`, after the dedup check:

```cpp
constexpr int64_t kGapTriggerThreshold = 100;
if (block_no > 0 && bookset.current_block > 0 &&
    block_no > bookset.current_block + 1) {
    int64_t gap = block_no - bookset.current_block - 1;
    if (gap >= kGapTriggerThreshold) {
        ++bookset.gaps_observed;
        fmt::print(stderr,
            "[GAP] forward jump {} -> {} (gap={}); triggering WS re-catch-up\n",
            bookset.current_block, block_no, gap);
        if (bookset.gap_callback) bookset.gap_callback();
    } else {
        ++bookset.small_gaps_observed;  // empty-block skip, normal HL behavior
    }
}
// continue with existing block_changed / advance_block flow
```

The publisher then keeps processing events normally — the WS thread
will reseed synth for every sym over the next ~seconds-to-minutes
(same path as initial bootstrap).

## Fills caveat

Fills mode has no WS reseed mechanism (PbTrade is per-event, not a
snapshot). Gap detection still applies — we log the gap and
increment a counter — but recovery is impossible. The missed trades
are permanently absent from our emit stream.

If the gap is large (say > 100 blocks ≈ ~50 s of fills), the cleanest
recovery is `std::exit(1)` and let systemd respawn. Fresh start, no
pretend-state. Configurable threshold; default off — log only.

## Trade-off: continuous WS bandwidth

Always-subscribed means we keep receiving l2Book updates for every
sym in steady state. Rough estimate: ~200 syms × variable update
rate → ~1000-3000 msgs/sec, ~2-15 MB/s. The CPU per msg is small
(JSON parse + `caught_up_.count(coin)` → early return), but it's
non-zero load forever, vs. today's near-quiet steady state.

n1 is a 4xlarge with headroom. Acceptable cost for the
simplification (no resubscribe cycle, atomic flag-flip recovery).

---

# Layer 1c: bootstrap freshness gate

## The problem L1a doesn't fix

If `hl-visor` and the publisher both restart, the publisher is a
fresh process: `highest_block_seen = -1`, `current_block = -1`, and
there is no in-process memory that the visor is replaying historical
blocks. With today's `--bootstrap` flow, books start WS bootstrap and
then seek to the end of the current streaming file. If that seek
happens while visor is mid-catch-up, the publisher can still read the
remaining historical replay as if it were live.

That is rare, but it is the one case where L1a/L1b don't help:
there is no backward block from the publisher's point of view, and no
forward gap if the replayed file is internally contiguous.

## Design

Use the public WS l2Book feed as the freshness clock for publisher
startup. In `--bootstrap` mode, after `ContinuousWS` starts but before
the publisher opens/seeks/tails the node streaming file, wait until
the current file's latest packet `block_time` is within 3s of the
freshest WS l2Book `data.time` observed by the WS thread.

One-sided lag check:

```cpp
stream_is_fresh = latest_ws_l2book_time_ms > 0 &&
                  latest_stream_block_time_ms > 0 &&
                  latest_ws_l2book_time_ms - latest_stream_block_time_ms <= 3000;
```

This intentionally does **not** require absolute difference. If the
node stream is slightly ahead of a delayed public WS message, that is
fine; the unsafe case is the node stream lagging WS by minutes during
visor catch-up.

### ContinuousWS addition

Track the freshest l2Book timestamp seen across all symbols:

```cpp
std::atomic<int64_t> latest_l2book_time_ms{0};

int64_t latest_l2book_time() const {
    return latest_l2book_time_ms.load(std::memory_order_relaxed);
}
```

In `handle_l2book`, update this immediately after parsing
`data["time"]`, before the `caught_up_` early return. Use max-update
semantics so a delayed per-symbol WS message cannot move the reference
clock backward:

```cpp
int64_t prev = latest_l2book_time_ms.load(std::memory_order_relaxed);
while (ws_t > prev &&
       !latest_l2book_time_ms.compare_exchange_weak(
           prev, ws_t, std::memory_order_relaxed)) {}
```

### Startup wait

Add a helper in books tail mode:

```cpp
bool wait_for_bootstrap_stream_fresh(const std::string& data_dir,
                                     const ContinuousWS& cws,
                                     int64_t freshness_ms = 3000,
                                     int64_t max_wait_s = 1200);
```

Behavior:

- Poll the current hourly raw book-diff file.
- Parse the last complete line's `block_time`.
- Read `cws.latest_l2book_time()`.
- Return once `ws_ms - stream_ms <= freshness_ms`.
- Keep waiting if no WS l2Book has arrived yet, the file is missing,
  the file is empty, or the last line is malformed.
- On timeout, fail startup rather than tailing replay as live data.

Important implementation detail: this gate must run **before** the
initial `seekg(0, std::ios::end)`, or it must explicitly re-seek to
EOF after the wait. Otherwise the publisher would still read the
historical replay bytes appended while it was waiting.

## Scope

Apply this gate to books `--bootstrap`. Fills has no WS bootstrap
thread, so the cron L2 sequence remains the main scheduled protection
for fills. If books and fills both crash with visor, fills can still
emit duplicate historical trades during the remaining catch-up window;
that is transient and does not create lasting publisher state.

---

# Layer 2: cron-orchestrated clean restart

## Current sequence (broken)

`/usr/local/bin/hl_weekend_restart.sh`:

1. Stop hl-visor
2. Start hl-visor (catch-up ~9 min)
3. Sleep 60 s ← blind, far less than catch-up
4. Restart books publisher (reads mid-replay)
5. Restart fills publisher (reads mid-replay)
6. Sleep 15 s, verify

Step 3 is the bug.

## Proposed sequence

1. Stop publishers (clear their state)
2. Stop hl-visor
3. Start hl-visor
4. **`wait_for_caught_up`** — poll streaming file until
   `wall_clock - block_time < 3 s`
5. Start publishers (fresh, post-catch-up; books reseeds via WS)
6. Verify

### `wait_for_caught_up`

```bash
wait_for_caught_up() {
    local data_dir="${HOME}/hl/data/node_raw_book_diffs_streaming/hourly"
    local max_wait_s=1200
    local freshness_ms=3000
    local poll_interval_s=5
    local start_t=$(date +%s)

    while true; do
        local hour_file="${data_dir}/$(date -u +%Y%m%d)/$(date -u +%-H)"
        if [ ! -f "$hour_file" ]; then
            sleep "$poll_interval_s"; continue
        fi
        local last_block_time
        last_block_time=$(tail -1 "$hour_file" | jq -r '.block_time // empty' 2>/dev/null)
        if [ -z "$last_block_time" ]; then
            sleep "$poll_interval_s"; continue
        fi
        local last_block_ms now_ms lag_ms
        last_block_ms=$(date -u -d "$last_block_time" +%s%3N 2>/dev/null) || {
            sleep "$poll_interval_s"; continue
        }
        now_ms=$(date -u +%s%3N)
        lag_ms=$((now_ms - last_block_ms))

        if [ "$lag_ms" -lt "$freshness_ms" ]; then
            echo "Caught up (lag ${lag_ms}ms)"
            return 0
        fi

        local elapsed=$(($(date +%s) - start_t))
        if [ "$elapsed" -gt "$max_wait_s" ]; then
            echo "ERROR: catch-up exceeded ${max_wait_s}s, aborting"
            return 1
        fi
        echo "  ... still catching up (lag $((lag_ms/1000))s, elapsed ${elapsed}s)"
        sleep "$poll_interval_s"
    done
}
```

Edge cases: hour rotation, empty/malformed file, unbounded waits all
handled. 20-min timeout protects against bad catch-ups; if exceeded,
cron leaves publishers stopped and emails an alert.

---

# Combined behavior

| Event | Publisher running? | What handles it |
|---|---|---|
| Scheduled cron restart | Stopped → restarted post-catch-up | L2 |
| Unscheduled visor crash | Keeps running | L1a drops the replay |
| Streaming-file byte loss or hl-node skipped blocks | Keeps running | L1b detects the forward jump and triggers WS re-catch-up |
| Visor crash AND publisher crash together | systemd restarts both | Books `--bootstrap` waits for L1c stream-vs-WS freshness before tailing; fills may emit duplicate historical trades during remaining catch-up, accepted as transient |

---

# Observability

Add to process-exit summary:

```
=== exit === events=... emitted=... duplicates_dropped=N gaps_observed=N
```

First occurrence of each → log once. Rate-limit after.

Morning stall report should surface non-zero `duplicates_dropped` and
`gaps_observed` from the prior day.

---

# Testing strategy

1. **`--file` replay test (L1a)**: use the 06-25 11h hour file
   (known double-write of block 1048908000). Run with `--print-only -v`,
   confirm `duplicates_dropped > 0` and `events=` matches a clean
   reference hour.
2. **Gap-injection test (L1b)**: craft a small JSONL file with a
   forward block-number jump, run with `--bootstrap --print-only`,
   verify the gap log fires and `force_recatch_up` triggers (check
   WS reseed via stderr).
3. **Bootstrap freshness-gate test (L1c)**: start books publisher
   with `--bootstrap` while the raw book-diff file's last `block_time`
   is intentionally stale vs. WS l2Book time; verify it waits and does
   not seek/tail until `ws_ms - stream_ms <= 3000`.
4. **L2 dry-run**: run the new `hl_weekend_restart.sh` manually on n1
   once. Watch logs: should pause at `wait_for_caught_up` for ~9 min,
   then proceed. Verify the publisher layers behave (`duplicates_dropped` and
   `gaps_observed` should be 0 in this path since L2 prevented the
   exposure).

## Production rollout

1. `./noblebuild.sh hl_node_publisher`
2. Stage as `/usr/local/bin/hl_node_publisher.new` and
   `hl_weekend_restart.sh.new` on n1
3. Swap during next planned restart window
4. Verify in next morning's report

Rollback: keep `.bak` of both for one week.

---

# Open questions

1. **Freshness threshold for `wait_for_caught_up`** — 3 s OK?
2. **Freshness threshold for L1c WS-vs-stream startup gate** — also
   3 s OK?
3. **Max-wait timeout** — 20 min?
4. **Fills gap-exit threshold** — log only, or exit on gaps > N
   blocks? Default off.
5. **Always-subscribed WS bandwidth** — accept ~2-15 MB/s steady
   state? Or sample-and-check on n1 before locking in?
6. **Python sibling parity** — port L1a/L1b/L1c to `hl_node_publish.py`
   as a follow-up?

# Out of scope

- Real-time duplicate-detection alerting.
- Backfill of historical corruption from prior restarts.
- Hot-standby second node (the architectural fix for the 9-min
  consumer gap during L2 restarts).

# Related files

- `src/pktrade/toolbins/hl_node_publisher.cc` — L1a + L1b + L1c source
- `/usr/local/bin/hl_weekend_restart.sh` (n1) + repo copy at
  `overmind/studies/hl_feed/hl_node_mainnet_samples/hl_weekend_restart.sh`
  — L2 cron
- `overmind/strat_main/tools/hl_node_publish.py` — Python sibling
- `overmind/studies/hl_feed/hl_node_timeline.md` — 06-25 11:55 row
