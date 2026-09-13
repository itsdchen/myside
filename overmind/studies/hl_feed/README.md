# HL Feed System — Entry Point

Single overview of how Hyperliquid market data flows from the chain into
our strategies. Read this first; deep-dive docs in this directory have
the operational detail.

The HL feed exists because Hyperliquid's public WS, the only way to
consume HL data directly, has two problems we couldn't engineer around
from a trading box: it's the slowest path off the exchange, and it
truncates / batches book and trade data in ways that lose information
we need (per-order oids, full L3, per-block boundary semantics). The
fix is to run a non-validating HL node ("n1") inside HL's AWS Tokyo
VPC, tail its raw event streams, and republish them to our trading
boxes over a faster path. Bytes from chain land in our strategies
~170-210 ms earlier on average than via the public WS, plus we get the
richer L3 view of the book that the public WS doesn't expose.

---

## Architecture

```
                                            +------------------+
                                            |  HL chain        |
                                            +--------+---------+
                                                     | gossip
                                                     v
   +----------------------------------------------------------------+
   |  n1  (r6i.4xlarge, ap-northeast-1, l1 acct, 172.31.37.166)     |
   |                                                                |
   |   +------------+      writes hourly JSONL files                |
   |   |  hl-visor  +----> ~/hl/data/node_raw_book_diffs_streaming  |
   |   |  (systemd) |      ~/hl/data/node_fills_streaming           |
   |   +------------+      ~/hl/data/node_*_streaming               |
   |                                |                               |
   |                tail -F         |                               |
   |   +------------------------------+    PbBookSnapshot           |
   |   | hl_node_publisher_cpp (books)+--> ZMQ PUB tcp://*:5555 ====|====+
   |   | (systemd, --bootstrap)       |                             |    |
   |   +------------------------------+                             |    |
   |   +------------------------------+    PbTrade                  |    |
   |   | hl_node_publisher_cpp (fills)+--> ZMQ PUB tcp://*:5556 ====|====+
   |   | (systemd)                    |                             |    |
   |   +------------------------------+                             |    |
   |                                                                |    |
   |   hl_data_retain.sh (cron) -- gzip>2h, delete>48h              |    |
   |   hl_disk_alert.sh   (cron) -- 70%/85% email + retention bump  |    |
   +----------------------------------------------------------------+    |
                                                                         |
   over VPC                                                              |
                              +--------------------------+               |
                              |  datalog box             |               |
                              |                          |               |
                              | simple_datalog --hlnode <----------------+
                              |   :5555 -> books.pb.gz   |               |
                              |   :5556 -> fills.pb.gz   |               |
                              | + hourly upload to s3    |               |
                              | hlnode_gzsplitter        |               |
                              |   per-(sym,stream).gzpbf |               |
                              +--------------------------+               |
                                                                         |
   over VPC                                                              |
                              +--------------------------+               |
                              |  trading box (gv0/gf*/…) |               |
                              |                          |               |
                              | pkmultifeed (C++) ----<-+|               |
                              |   |  HyperliquidNode sub-+---------------+
                              |   |  (books :5555 + fills :5556)         |
                              |   |  + HL WS (fallback)                  |
                              |   |  + per-process health FSM            |
                              |   |    flips publish-from-node           |
                              |   v                                      |
                              | ZMQ PUB ipc:///tmp/feed-sock-cpp         |
                              |   -> strategies via pkfeedclient         |
                              +--------------------------+               |
                                                                         |
                              +--------------------------+               |
                              | pymultifeed (Python) -<--+               |
                              |   same shape, same fan-in, same FSM      |
                              +--------------------------+               |
```

---

## Components

**hl-visor + hl-node on n1.** Runs the official Hyperliquid non-validator
binary. We start it under systemd with `--write-trades --write-raw-book-diffs
--write-fills --write-order-statuses --write-hip3-oracle-updates
--stream-with-block-info --disable-output-file-buffering`. The trailing
flag (which visor forwards to hl-node) writes streaming files unbuffered
so our publisher's tail sees fills within milliseconds instead of
hundreds — without it median end-to-end node latency degrades by
~200 ms. The visor downloads and
verifies the inner `hl-node` binary (GPG-verified against HL's public
key, which must be imported into `~/.gnupg` first), syncs the chain, and
writes one JSONL-per-line file per stream per hour under
`~/hl/data/<stream>_streaming/hourly/YYYYMMDD/HH`.
Mainnet requires `~/override_gossip_config.json` listing Tokyo-resident
seed peers; testnet auto-loads its own. Disk burn rate on mainnet at
the current 48h retention settles around ~58 GB for the book-diffs dir.
Provisioning recipe: `n1_provision_runbook.md`. Day-to-day operation:
`n1_navigation_guide.md`.

**hl_node_publisher (C++).** Tails the JSONL files in `node_raw_book_diffs_streaming`
and `node_fills_streaming`, reconstructs L3 books per chain block,
emits one `PbBookSnapshot` per block over ZMQ PUB (top-N capped at
`--max-levels 40`), and pairs the two-sided fill records into single
`PbTrade` events with `passive_was_buyer` semantics. Bootstraps on
startup via HL's `/info` endpoint to seed initial state. Two systemd
units (`hl-publisher-books.service`, `hl-publisher-fills.service`)
on n1, both currently running the noblebuild-produced
`/home/ubuntu/hl_node_publisher_cpp`. The C++ port dropped publisher
CPU from ~95% to ~20% on n1 and brought wire delay back to the
hundreds-of-ms range.
A Python sibling (`overmind/strat_main/tools/hl_node_publish.py`)
exists for ad-hoc debugging — not wired into any systemd unit.

**Why snapshots and not incremental updates.** The publisher emits full
top-N snapshots per block rather than `PbBookUpdate` incrementals. That
keeps downstream consumers simple and made the node feed an ordinary
snapshot source for `BookManager`.

**ZMQ fan-out.** `tcp://0.0.0.0:5555` for books, `tcp://0.0.0.0:5556`
for fills. Subscribers connect over the same AWS Tokyo VPC. There is
no authentication on the socket — anyone inside the VPC can SUB. This
is fine for now (the VPC is small and we control it), but is one of
the things to revisit when n1 isn't a sole-tenant box anymore.

**Datalog capture.** `simple_datalog --hlnode --node-endpoint tcp://172.31.37.166:5555`
runs on the capture box, writing a custom-framed `.pb.gz` (8B
recv_ts_ns LE + 4B pb_len LE + protobuf bytes, gzip envelope around
the whole stream). The postproc cron runs `hlnode_gzsplitter` to
explode that into per-(sym, stream) `.gzpbf` files matching the
existing `<sym>_<stream>_<date>.gzpbf` naming the rest of the pipeline
expects. Cron entries in `overmind/datalog/capture.crontab` and
`overmind/datalog/postprocessing.crontab`; deploy instructions in
`overmind/datalog/README.md`.

**Consumers (pkmultifeed + pymultifeed).** Both subscribe to the node
feed AND the HL public WS simultaneously per a `HyperliquidNode` +
`Hyperliquid` config block. A per-process 1 Hz health FSM
(`silent_s` / `lag_s` thresholds, default 5 s each) flips a
`publish-from-node` flag; the WS path stays warm and takes over
within seconds on degradation. A per-sym monotonic time gate
prevents WS-fallback from regressing chain time below the node's
last-published event. First unhealthy transition fires a one-shot
email per process run (skipped when no WS fallback is configured —
nothing to fall back to).

**Where the public WS fast+slow merge lives.** Both pkmultifeed and
pymultifeed subscribe to `l2Book` with `fast: true` AND with
`fast: false` per sym and merge them in-process. As of 2026-06-13 HL
returns identical payloads on both (20 levels at ~538 ms cadence) and
each `time` value appears once across the combined stream — i.e. they
unified the fast/slow streams server-side and the in-process merge is
currently a no-op (`fast_bids[-1]` cutoff keeps everything). We keep
both subscribes wired up against a future divergence; cost is one
extra subscribe message per sym at startup.

---

## Operating the system day-to-day

For specific commands: `n1_navigation_guide.md`.

1. **Look at publisher state.** SSH to n1, `sudo systemctl status hl-publisher-books hl-publisher-fills`. Check `journalctl -u <service>` for recent rotation log lines (`=== rotation: …/HH -> …/HH+1 === events=N emitted=N anomalies=0 (drained N late lines)`).
2. **Look at visor state.** `sudo systemctl status hl-visor`. `du -sh ~/hl/data/*_streaming/hourly` to spot-check ingestion is alive across all streams.
3. **Update the publisher binary.** Build via `noblebuild.sh` on the dev box (matches n1's 24.04 glibc / libstdc++), `scp` to n1 as `hl_node_publisher_cpp.new`, `sudo systemctl stop hl-publisher-{books,fills}`, `mv`, `sudo systemctl start`. ~5 s of feed silence; consumers fall back to HL WS and recover.
4. **Add a sym to consumer coverage.** Edit the consumer's config (gv0's `~/test_nodefeed/test_feed_cpp.json` is the canonical example), add the sym to the `HyperliquidNode.symbols` array (or the `Hyperliquid.symbols` array for WS-only), restart the consumer. Publisher already publishes all syms — no n1-side change needed.
5. **Adjust disk retention on n1.** `/usr/local/bin/hl_data_retain.sh` has `GZIP_AFTER_HOURS=2`, `DELETE_AFTER_HOURS=48`, `AGGRESSIVE_DELETE_HOURS=1`. Edit in place; the next cron run picks up the new values. Currently steady-state at ~58 GB for the books dir, 27% disk used.

---

## Failure modes & how we detect them

| Failure | Detected by | Alert path | What to do |
|---|---|---|---|
| n1 instance dead / unreachable | CloudWatch StatusCheckFailed alarm | SNS → email | restart from AWS console, then re-verify systemd units |
| hl-visor.service down (silent) | None directly; downstream: publisher goes silent → consumer health FSM trips | one-shot HL-NODE-UNHEALTHY email from pkmultifeed/pymultifeed | `sudo systemctl restart hl-visor.service` on n1; check journal for panic |
| hl-publisher dies | Same downstream chain as above | one-shot HL-NODE-UNHEALTHY email | `sudo systemctl restart hl-publisher-{books,fills}.service` |
| Node feed silent or lagging | Per-process 1 Hz health FSM (`silent_s` / `lag_s`) | flips publish-from-node off + one-shot email | Investigate publisher / network; WS fallback is already serving consumers while you do |
| Disk fills up | `hl_disk_alert.sh` cron on n1 | email at 70% (warning + retention tightened) and 85% (critical + thresholds halved) | investigate which `_streaming` dir is growing; usually a retention threshold is wrong |
| HL public WS idle disconnect (60s) | WS reconnect logic recovers automatically | none normally | only matters during catch-up; C++ ixwebsocket pings + app-level pings keep it warm |

CloudWatch only catches "box is dead." The consumer-side silence emails
are how we catch every higher-level failure mode end-to-end. We
explicitly chose NOT to build a separate process-level heartbeat
because the consumer signal already covers everything that operationally
matters (see ws2_open_items.md → "Process-level heartbeat" Dropped).

---

## Top-level docs

Keep this directory small. Historical working notes were consolidated
into the files below; start here rather than chasing dated notes.

- **`README.md`** — system architecture and operational context.
- **`hl_node_perf_consolidated_20260623.md`** — current performance
  handoff: historical test results, what is proven, what is promising,
  what not to redo, and next actions.
- **`ws2_open_items.md`** — live open/deferred/done tracker.
- **`go_live_playbook.md`** — trading-fleet cutover and rollback
  playbook.
- **`n1_navigation_guide.md`** — commands, paths, and troubleshooting
  for operating n1.
- **`n1_provision_runbook.md`** — recipe to build a new n1 from scratch.
- **`hl_node_mainnet_samples/`** — setup script, gossip / visor configs,
  systemd unit snapshots, and retention scripts that ship to n1.

For AWS-wide context (cost review, instance inventory across the
whole Tokyo fleet, not just n1): `../aws_setup_cost_review.md`.

---

## When to update this README vs other docs

This README is the orientation doc. Update it when:
- The architecture diagram changes (a new component, a stream is added
  or removed, a consumer pattern changes).
- A failure mode is added or its detection path changes.
- A lasting operational doc joins this directory and needs a one-line
  pointer in the top-level-docs list.

Don't update it for:
- Per-fix detail that is already captured by the current consolidated
  handoff or open-items tracker.
- Current open items (goes in `ws2_open_items.md`).
- One-off experiments (append the durable result to
  `hl_node_perf_consolidated_20260623.md` or `ws2_open_items.md`; avoid
  adding another dated top-level note unless it needs to live long term).

Rule of thumb: if a future operator who has only read this README would
fail to understand or operate the system, fix it here. Otherwise, fix
it in the relevant focused doc.
