# pkprobe: AWS backbone path-cluster experiment

Living plan for the Ohio → Tokyo UDP path-characterization experiment. Update
this doc when the design changes; don't let it drift behind the code.

## Goal

Characterize ECMP path clusters between us-east-2 (Ohio) and ap-northeast-1
(Tokyo) over the AWS backbone, by sending UDP probes on multiple destination
ports and logging per-packet one-way times. Use the resulting data to answer:

- How many distinct latency clusters exist intraday between Ohio and Tokyo?
- How stable is the lane → cluster assignment across days?
- Does pkrelay's observed 65–75 ms one-way drift correspond to lane reassignment,
  or to whole-cluster shifts (the entire underlay moving)?
- Should pkrelay fan out to multiple dst ports and let pkmultifeed pick the
  fastest at startup? If so, how many ports are enough?

## Architecture

Two standalone binaries, `pkprobe_send` (Ohio) and `pkprobe_recv` (Tokyo),
under `src/pktrade/probe/`. No databento dep, no symbols, no fusion gate. The
binaries' only job is to send timestamped probes on N lanes and log per-packet
one-way times.

Standalone rather than instrumenting pkrelay/pkmultifeed because:

- No risk of perturbing the live feed path.
- Independent lifecycle — the experiment runs for days, pkrelay restarts daily
  by design; not coupling them keeps both simple.
- No need to drag the databento SDK / GLBX subscription into a measurement that
  has nothing to do with market data.

Still runs on production hosts (same NIC, same kernel, same VPC peering), so
the measurement is representative.

## Design choices

### Probe lanes: vary only the destination port

- One socket on the sender. `sendto()` to N different `(tokyo_ip, base_port+i)`
  pairs.
- Do **not** `bind()` a src port. The kernel picks an ephemeral on first send,
  mirroring pkrelay's behaviour.
- The same src port is shared across all N lanes within a run (because they
  share one socket). Only the dst port differs across lanes — clean mapping to
  "what production would experience if it fanned out across ports".

### Daily lifecycle: cron + self-EOD at 18:00 ET (mirrors pkrelay)

- Cron starts each binary at 18:00 ET.
- Each binary computes its own EOD epoch (next 18:00 ET that is ≥ 23 h after
  startup, minus 15 s) and exits cleanly when reached. The 15 s buffer lets the
  next cron-launched instance bind its port without race.
- Each daily run gets a fresh kernel-assigned ephemeral src port. This is
  intentional and is the experimental analogue of pkrelay's daily src-port
  refresh.

No `--date` flag needed; EOD is purely a function of startup time. No CME
calendar gating — the experiment runs every day including weekends.

### Number of lanes: 16 (configurable)

Enough to resolve 3–4 latency clusters with several lanes per cluster
(distinguishes "this lane is genuinely fast" from "single-packet noise").
Small enough that one-thread-per-lane on the receiver is trivially correct.

### Send rate: 10 packets/sec per lane (configurable)

160 pps aggregate, ~14 M packets/day, ~10 KB/s bandwidth. Low enough to not
measure our own queueing; dense enough to get hundreds of samples in any
10-second bin.

### Packet size: 88 bytes

Matches `DatabentoCmeMbpPacket` so we don't accidentally measure a
size-dependent path effect. The probe struct itself is smaller; remainder is
zero-padded.

### Wire format

`pkprobe_packet.h`, mirroring pkrelay_packet.h conventions:

```cpp
inline constexpr uint32_t kPkProbeMagic   = 0x48544150;  // 'P','A','T','H' LE
inline constexpr uint16_t kPkProbeVersion = 1;
inline constexpr size_t   kPkProbeBytes   = 88;  // matches DatabentoCmeMbpPacket

struct PkProbePacket {
  uint32_t magic;        // kPkProbeMagic
  uint16_t version;      // kPkProbeVersion
  uint16_t lane_id;      // 0 .. num_lanes - 1
  uint64_t seq;          // monotonic per-lane on the sender
  int64_t  send_ts_ns;   // CLOCK_REALTIME at sendto()
  uint8_t  pad[88 - 24]; // zero-fill to 88 bytes
};
static_assert(sizeof(PkProbePacket) == kPkProbeBytes);
```

Trivially copyable, standard layout. Receiver rejects packets with wrong magic
or version.

### Timing: userspace `clock_gettime(CLOCK_REALTIME)` both ends

~10 µs scheduling jitter is negligible against 65–75 ms paths. Clock skew
between Ohio and Tokyo is a constant offset that doesn't affect intraday
cluster shape, which is what we care about. No `SO_TIMESTAMPING`, no PTP.

### Logging: CSV, per-lane files, per side

Filenames include startup timestamp so a same-day restart produces a fresh
set of files (matches the production convention of run-keyed output files).

- Sender → one file per lane: `send_<startup>_lane<NN>.csv` with header
  `seq,send_ts_ns`. One row per `sendto()`. The sender is single-threaded so
  this is just for symmetry with the receiver layout.
- Receiver → one file per lane: `recv_<startup>_lane<NN>.csv` with header
  `seq,send_ts_ns,recv_ts_ns,one_way_ns`. One row per validated packet.

Per-lane files mean each receiver thread owns its `FILE*` outright — no
mutex, no shared-file atomicity assumptions, no risk of interleaved rows.
Post-analysis reads a glob (`duckdb.read_csv('recv_*_lane*.csv')` or
`pd.concat([pd.read_csv(f) for f in glob(...)])`) and groups by lane from the
filename or by lane_id parsed from the path.

All files cover one ~24 h run. Post-analysis joins send/recv per lane on
`seq` to detect losses (in sender log, missing from recv log) and reordering.

## Sender (Ohio) responsibilities

1. Parse JSON config.
2. Resolve `tokyo_ip` via `getaddrinfo`; precompute N `sockaddr_in` for
   `(tokyo_ip, base_port + i)`.
3. Open one `SOCK_DGRAM` socket. **Do not bind.**
4. Compute EOD epoch (next 18:00 ET ≥ 23 h after startup, minus 15 s).
5. Open N `send_<startup>_lane<NN>.csv` files, write header to each.
6. Run send loop at configured rate. Each tick: round-robin through lanes;
   fill `send_ts_ns` immediately before `sendto()`; append a row to that
   lane's CSV.
7. Exit cleanly on EOD reached or SIGINT/SIGTERM. Flush, fsync, close.

## Receiver (Tokyo) responsibilities

1. Parse JSON config.
2. Open N `SOCK_DGRAM` sockets; bind each to `INADDR_ANY:base_port + i`.
   Set `SO_RCVBUF = 8 MB`, `SO_RCVTIMEO = 2 s` (copying pkmultifeed values).
3. Compute EOD epoch.
4. Open N `recv_<startup>_lane<NN>.csv` files, one per lane, each with header.
5. Spawn N threads, one per lane socket. Each thread owns its own lane CSV
   `FILE*` and loops on blocking `recv()`. On each valid packet: take
   `recv_ts_ns`, validate magic+version, append a row to its own CSV. No
   cross-thread sharing of file handles — no locks.
6. Exit cleanly on EOD reached or SIGINT/SIGTERM. Flush, fsync, close, join.

## Config

JSON config, sharable across hosts (each side ignores fields it doesn't use):

```json
{
  "tokyo_ip": "172.31.43.108",
  "base_port": 5000,
  "num_lanes": 16,
  "rate_per_lane_hz": 10,
  "packet_bytes": 88,
  "output_dir": "/home/ubuntu/logs/pkprobe"
}
```

Sender uses all fields. Receiver uses `base_port`, `num_lanes`, `output_dir`.

## Deployment

- Pick one Tokyo box for the receiver (configurable; can re-run against others
  later).
- Add UDP `base_port .. base_port + num_lanes - 1` (5000–5015 default) to that
  box's security group, from the us-east-2 VPC CIDR (`10.100.0.0/16`).
- Cron entries on each host. Use `CRON_TZ=America/New_York` so 18:00 ET stays
  correct through DST without manual edits twice a year:

  ```
  CRON_TZ=America/New_York
  0 18 * * * /home/ubuntu/bin/pkprobe_send --conf /home/ubuntu/etc/pkprobe.json
  ```

  (Receiver entry on the Tokyo box is identical with `pkprobe_recv`.)

## Post-analysis (out of scope for the binaries)

Separate Python/pandas/duckdb scripts that:

- Join sender and receiver CSVs on `(lane_id, seq)` to compute loss and
  reordering rates per lane.
- For each lane, plot `one_way_ns` over time.
- Histogram `one_way_ns` per lane per hour-of-day, per lane per day.
- Cluster the per-lane medians (or KDE peaks); identify which lanes share a
  cluster.
- Track lane → cluster assignment across days to answer "do the clusters drift,
  or do lanes hop between fixed clusters?"

Lives outside this directory (e.g. `analysis/pkprobe/`); the experiment's
output is the pair of CSVs.

## Out of scope for v1

- Heartbeats: not needed at a steady 10 pps; gaps in seq are an unambiguous
  silence signal.
- Email alerts / paging: research tool, not production.
- `SO_TIMESTAMPING` or PTP: userspace clock is enough for cluster ID.
- Multiple Tokyo destinations in one run: configurable to one, re-run for
  others if needed.
- Reverse direction (Tokyo → Ohio): doubles the work; defer.

## Change log

- 2026-05-15 — Initial plan. 16 lanes, 10 pps/lane, 88-byte packets,
  cron-driven daily restart at 18:00 ET, one Tokyo destination (configurable),
  CSV logging on both sides, post-analysis out of scope.
- 2026-05-15 — Switched receiver logging from one shared CSV with a mutex to
  one CSV per lane. Each thread owns its `FILE*` outright; no locking, no
  shared-file atomicity assumptions. Sender mirrors the layout for symmetry.
