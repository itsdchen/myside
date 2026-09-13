# n1 Navigation Guide

Practical reference for working with the HL non-validating node box. Last
updated 2026-06-10.

## What n1 is

- **Instance**: `i-0fade2737acb92dd2` (`r6i.4xlarge`, 16 vCPU / 128 GB RAM)
- **Region/AZ**: `ap-northeast-1a` (Tokyo)
- **OS**: Ubuntu 24.04 LTS
- **Public DNS**: `ec2-43-207-110-196.ap-northeast-1.compute.amazonaws.com`
- **Private IP**: `172.31.37.166` (this is what trading boxes will use)
- **Purpose**: runs an HL non-validating node (`hl-visor` → `hl-node`),
  emitting L3 book diffs + trades + EVM data to `~/hl/data/`.

## Getting in

From the dev workstation:

```bash
sn1                # ssh
mn1                # mosh (better for unstable networks)
ssh n1             # same — works via ~/.ssh/config Host entry
```

All three use `/home/pktrade/aws_pairs/l1_dchen.pem` and connect as
`ubuntu@`. The aliases live in `~/.bashrc` on the dev workstation.

If the public IP changes (auto-assigned, not an EIP), update the
HostName in `~/.ssh/config` and the alias in `~/.bashrc`.

## Quick health check (paste this)

```bash
sudo systemctl is-active hl-visor.service        # should print 'active'
sudo systemctl status hl-visor.service --no-pager -l | head -10
sudo journalctl -u hl-visor.service --since "5 min ago" -o cat \
  | grep -E "applied block|ERROR|panic" | tail -5
df -h /
du -sh ~/hl/data/*/  | sort -h | tail -5
```

What "healthy" looks like:
- `is-active` returns `active` (exit 0)
- recent journal shows `applied block N` lines incrementing
- `df -h /` shows <70% usage
- the largest dirs in `~/hl/data/` are
  `node_raw_book_diffs_streaming` (tens of GB), `replica_cmds` (~40-50
  GB at steady state)

## Sync lag — are we real-time?

The "best" measure: compare `local_time` vs `block_time` on the latest
streamed event packet:

```bash
DATE=$(date -u +%Y%m%d); HOUR=$(date -u +%-H)
tail -1 ~/hl/data/node_raw_book_diffs_streaming/hourly/${DATE}/${HOUR} \
  | jq -r '"local: \(.local_time)\nblock: \(.block_time)\n#:     \(.block_number)"'
```

Real-time = `local_time` is within ~0.3s of `block_time`. During
bootstrap (e.g. after a restart) the gap will be minutes; it shrinks as
the node catches up.

## Service control

```bash
sudo systemctl status hl-visor.service           # state + tail of recent logs
sudo systemctl restart hl-visor.service          # full restart (re-syncs ~1 min)
sudo systemctl stop hl-visor.service             # stop (data files freeze)
sudo systemctl start hl-visor.service            # start
sudo journalctl -u hl-visor.service -f           # follow live logs
sudo journalctl -u hl-visor.service --since "1 hour ago"
```

Notable: the unit file is at `/etc/systemd/system/hl-visor.service` (also
in repo at `studies/hl_node_mainnet_samples/hl-visor.service`).

**Singleton-check gotcha**: both `hl-visor` and the child `hl-node`
panic if they detect another process with their binary name in
cmdline. So **don't** do `pkill hl-visor` or run shell commands that
literally embed those strings. Use `killall -q "$BASENAME"` with
variable expansion, or just let systemd manage the process.

## Data dirs — what's where

All live under `~/hl/data/`. The interesting ones:

| Path | Format | What it is |
|---|---|---|
| `node_raw_book_diffs_streaming/hourly/{YYYYMMDD}/{H}` | JSONL | L3 order events (the main book stream) |
| `node_fills_streaming/hourly/{YYYYMMDD}/{H}` | JSONL | Per-counterparty fill rows with fee/closedPnl/crossed/twapId |
| `node_order_statuses_streaming/hourly/{YYYYMMDD}/{H}` | JSONL | Every L1 order lifecycle event (place/cancel/fill/reject) |
| `hip3_oracle_updates_streaming/hourly/{YYYYMMDD}/{H}` | JSONL | HIP-3 deployer mark price inputs (cash:*, vntl:*) |
| `node_twap_statuses_streaming/hourly/{YYYYMMDD}/{H}` | JSONL | TWAP order lifecycle (auto-enabled by --write-fills) |
| `node_trades_streaming/hourly/{YYYYMMDD}/{H}` | JSONL | (legacy — not written anymore since we switched to fills) |
| `node_fast_block_times/{YYYYMMDD}` | JSONL | Per-block apply timing (`fast` pipeline) |
| `node_slow_block_times/{YYYYMMDD}` | JSONL | Same, `slow` variant |
| `evm_block_and_receipts/hourly/{YYYYMMDD}/{H}/*` | binary | EVM block + receipt data |
| `node_logs/gossip_*/hourly/{YYYYMMDD}/{H}` | text | Peer gossip/RPC traces |
| `replica_cmds/{sessiontime}/{date}/{block_floor}` | binary | Replay log (huge; aggressively rotated) |
| `periodic_abci_states/{YYYYMMDD}/*.rmp` | binary | Periodic state snapshots (huge; rotated) |
| `latency_summaries/`, `latency_buckets/` | JSONL | Tokio task latency distributions |
| `tcp_lz4_stats/`, `tcp_traffic/` | JSONL | Per-peer gossip traffic |

The hour `H` is the **integer UTC hour without leading zero** (e.g. hour
8 is `.../{date}/8` not `.../{date}/08`).

Working state (the node's brain — never touch):
`~/hl/hyperliquid_data/` (~9 GB of RocksDB).

## Looking at the streaming data

```bash
# Live tail (1 line = 1 event packet)
DATE=$(date -u +%Y%m%d); HOUR=$(date -u +%-H)
tail -f ~/hl/data/node_raw_book_diffs_streaming/hourly/${DATE}/${HOUR}

# Pretty-print one packet
head -1 ~/hl/data/node_trades_streaming/hourly/${DATE}/${HOUR} | jq .

# Filter for one coin (jq selecting on .events array)
head -10000 ~/hl/data/node_trades_streaming/hourly/${DATE}/${HOUR} \
  | jq -c 'select(.events[].coin == "BTC")'

# All unique coins seen in one hour of book diffs
head -100000 ~/hl/data/node_raw_book_diffs_streaming/hourly/${DATE}/${HOUR} \
  | jq -r '.events[].coin' | sort -u
```

For reconstructed L2 books (uses our publisher):

```bash
# Print mode — just look at books per block, no ZMQ
~/.venvs/v1/bin/python ~/hl_node_publish.py \
  --tail-dir ~/hl/data/node_raw_book_diffs_streaming/hourly \
  --print-only --coin BTC -v

# ZMQ publish mode (for a downstream consumer)
~/.venvs/v1/bin/python ~/hl_node_publish.py \
  --tail-dir ~/hl/data/node_raw_book_diffs_streaming/hourly \
  --endpoint ipc:///tmp/hl_node_sock

# With BOOTSTRAP — seeds initial book state from public WS l2Book at
# startup. Without this, the book is incomplete for ~1 hour after
# startup. Caveat: brief (~10s) crossed books while phantoms resolve.
~/.venvs/v1/bin/python ~/hl_node_publish.py \
  --tail-dir ~/hl/data/node_raw_book_diffs_streaming/hourly \
  --bootstrap \
  --endpoint ipc:///tmp/hl_node_sock

# Fills publisher (separate process, separate endpoint)
~/.venvs/v1/bin/python ~/hl_node_publish.py \
  --tail-fills-dir ~/hl/data/node_fills_streaming/hourly \
  --endpoint ipc:///tmp/hl_node_fills_sock

# With blacklist (any mode)
~/.venvs/v1/bin/python ~/hl_node_publish.py \
  --tail-dir ~/hl/data/node_raw_book_diffs_streaming/hourly \
  --coins-blacklist-file /etc/hl/blacklist.txt \
  --print-only -v
```

## /info HTTP endpoint

Localhost only (not exposed publicly). Useful for one-shot state queries:

```bash
curl -s -X POST http://localhost:3001/info \
  -H "Content-Type: application/json" \
  -d '{"type":"meta"}' | jq .universe[0:5]

curl -s -X POST http://localhost:3001/info \
  -H "Content-Type: application/json" \
  -d '{"type":"clearinghouseState","user":"0x..."}' | jq .
```

Caveat: **does NOT support `l2Book`** by design — HL says use the
public WS for book queries.

## Logs — where things go

| What | Where |
|---|---|
| Visor + node main logs (live) | `journalctl -u hl-visor.service -f` |
| Visor + node main logs (historical) | `journalctl -u hl-visor.service --since "...".` |
| Disk retention activity | `journalctl -t hl_data_retain -S "1 hour ago"` |
| Disk usage heartbeat / alerts | `journalctl -t hl_disk_alert -S today` |
| Child stderr (per node spawn) | `~/hl/data/visor_child_stderr/<date>/<binary_idx>/<timestamp>` |
| Old tmux-era log (pre-systemd) | `/tmp/nlog` (may still exist; not active anymore) |

To see only alerts:

```bash
sudo journalctl -t hl_disk_alert -p warning..crit -S today
```

## Disk & retention

- Total: 484 GB root EBS (gp3, throughput bumped to 250 MB/s)
- Steady state usage: ~100-180 GB depending on which `--write-*` flags
- Retention script: `/usr/local/bin/hl_data_retain.sh` (cron every 30 min)
  - Streaming dirs: gzip files >2h, delete `.gz` >72h
  - `replica_cmds` + `periodic_abci_states`: aggressive drop >1h (no gzip)
  - Env vars `GZIP_AFTER_HOURS`, `DELETE_AFTER_HOURS`,
    `AGGRESSIVE_DELETE_HOURS` override defaults — used by disk alert in
    panic mode
  - `flock`-protected so concurrent runs don't stack
- Disk alert: `/usr/local/bin/hl_disk_alert.sh` (cron every 5 min)
  - <70% info heartbeat, no action
  - 70-84% warning: email (1/hr rate-limited) + **force normal
    retention pass**
  - ≥85% critical: email (1/hr) + **force AGGRESSIVE retention pass**
    (`GZIP_AFTER_HOURS=0.5 DELETE_AFTER_HOURS=24 AGGRESSIVE_DELETE_HOURS=0.25`)
  - Email goes to `itsdchen@gmail.com` (active); `pktrade@googlegroups.com`
    subscription is pending and likely blocked by group's external-sender
    settings.

Manual cleanup if needed:

```bash
# Force a retention pass now
sudo /usr/local/bin/hl_data_retain.sh

# Drop the testnet archive (saved during phase 0; ~12 GB)
sudo rm -rf ~/hl_testnet_state_20260609
```

## External monitoring

CloudWatch alarms on n1 (in AWS account `l1`, region `ap-northeast-1`):

- `n1-StatusCheckFailed-System` — AWS-side hardware/network failure
- `n1-StatusCheckFailed-Instance` — OS-side (kernel, OOM, etc.)

Both route through SNS topic `n1-alerts` →
`pktrade@googlegroups.com`. Currently catches "instance dead." Does NOT
catch "instance up but visor process crashed" — that's pending (option B
from the earlier monitoring discussion).

To list / inspect from your workstation:

```bash
aws cloudwatch describe-alarms --region ap-northeast-1 --alarm-name-prefix n1-
aws sns list-subscriptions-by-topic --region ap-northeast-1 \
  --topic-arn arn:aws:sns:ap-northeast-1:228948462720:n1-alerts
```

## Common ops

### Change visor flags (enable more `--write-*`)

1. Edit `/etc/systemd/system/hl-visor.service` (the `ExecStart=` line)
2. `sudo systemctl daemon-reload`
3. `sudo systemctl restart hl-visor.service`
4. New `~/hl/data/<thing>/` dirs will appear if the flag adds a stream

Currently enabled in the unit (as of 2026-06-10):
- `--serve-info` (HTTP info server on localhost:3001)
- `--write-fills` (per-counterparty fill rows; richer than trades; auto-enables twap_statuses)
- `--write-raw-book-diffs` (L3 book events — the main stream)
- `--write-order-statuses` (every L1 order lifecycle event)
- `--write-hip3-oracle-updates` (HIP-3 deployer mark inputs)
- `--stream-with-block-info` (per-event streaming with block context; appends `_streaming` to dir names)

Available but NOT enabled:
- `--write-misc-events` (catch-all)
- `--write-system-and-core-writer-actions` (transfers, system actions)
- `--serve-eth-rpc` (EVM JSON-RPC on port 3001 `/evm`)

### Enable mempool streaming

```bash
echo '{"enabled": true}' > ~/hl/file_mod_time_tracker/node_gossip_priority_config.json
sudo systemctl restart hl-visor.service
```

Per HL docs this makes the node "respect onchain gossip auction priority
ordering," i.e. start receiving pre-block transaction intents.

### Switch chain (Testnet ↔ Mainnet)

1. Stop: `sudo systemctl stop hl-visor.service`
2. Optionally archive state: `mv ~/hl ~/hl_<oldchain>_state_$(date +%Y%m%d)`
3. Edit `~/visor.json` to `{"chain": "Mainnet"}` or `{"chain": "Testnet"}`
4. If switching to Mainnet, ensure `~/override_gossip_config.json` has
   valid Mainnet seed peers (Testnet auto-loads its config; Mainnet needs
   explicit). See `hl_node_mainnet_samples/override_gossip_config.json`.
5. Start: `sudo systemctl start hl-visor.service`

### Update the publisher binary (C++)

When `src/pktrade/toolbins/hl_node_publisher.cc` changes, this is the
flow. The service restart causes a fresh bootstrap; expect ~5-10 min
before all syms are caught back up. Books and fills share one binary
(`hl_node_publisher_cpp`) but are different units.

```bash
# 1. Cross-build for noble (n1 is 24.04, dev box typically isn't).
./noblebuild.sh hl_node_publisher          # ~30 s warm cache; ~5 min cold

# 2. Ship as ".new" so we can verify before swapping.
scp noble.bin/hl_node_publisher n1:/home/ubuntu/hl_node_publisher_cpp.new
ssh n1 '/home/ubuntu/hl_node_publisher_cpp.new --help | head'   # smoke

# 3. (Optional) If the systemd unit's ExecStart changed too:
scp overmind/studies/hl_node_mainnet_samples/hl-publisher-books.service \
    n1:/tmp/hl-publisher-books.service.new
ssh n1 'sudo diff /etc/systemd/system/hl-publisher-books.service \
                  /tmp/hl-publisher-books.service.new'   # eyeball
ssh n1 'sudo install -m644 /tmp/hl-publisher-books.service.new \
                            /etc/systemd/system/hl-publisher-books.service \
        && sudo systemctl daemon-reload'
# repeat for hl-publisher-fills.service if it changed.

# 4. Atomic swap: back up old, move new into place, restart.
ssh n1 'sudo cp /home/ubuntu/hl_node_publisher_cpp /home/ubuntu/hl_node_publisher_cpp.bak \
        && sudo mv /home/ubuntu/hl_node_publisher_cpp.new /home/ubuntu/hl_node_publisher_cpp \
        && sudo systemctl restart hl-publisher-books \
        && sudo systemctl restart hl-publisher-fills'

# 5. Verify.
ssh n1 'systemctl is-active hl-publisher-books hl-publisher-fills'
ssh n1 'sudo journalctl -u hl-publisher-books --since "1 min ago" --no-pager | tail -20'
# Expect "[ws] CAUGHT UP and unsubscribed: <SYM> (total caught_up: N/252)" lines.
```

**Rollback** if something's wrong:

```bash
ssh n1 'sudo mv /home/ubuntu/hl_node_publisher_cpp.bak \
                /home/ubuntu/hl_node_publisher_cpp \
        && sudo systemctl restart hl-publisher-books \
        && sudo systemctl restart hl-publisher-fills'
```

**End-to-end sanity** (from inside the VPC, e.g. gv0): subscribe to the
PUB and watch for ~10 s of msgs. See the `Publisher` section above for
the SUB snippet — `tcp://172.31.37.166:5555` is books, `:5556` is fills.

### Rebuild from scratch

If the node ever gets into a bad state:

```bash
sudo systemctl stop hl-visor.service
mv ~/hl ~/hl_bad_$(date +%Y%m%d_%H%M)   # don't delete in case we need forensics
sudo systemctl start hl-visor.service   # visor will re-download binary + re-sync
```

Expect ~10-30 min to re-sync to current block on Mainnet.

## Disk wider context

`~/hl/data/replica_cmds/<sessiontime>/...` is the biggest dir at
steady state because the node writes ~28 GB/hour of replay data. The
retention's 1-hour drop policy keeps it bounded. If you see this dir
growing past ~80 GB, check whether the cron is running (`systemctl status
cron`, `crontab -l`).

The "sessiontime" subdir under `replica_cmds` changes every time the node
restarts — useful to spot multiple restarts (each gets its own subdir).

## Process tree (when healthy)

```
systemd
  └── hl-visor.service
       └── hl-visor (parent watchdog, ~20 MB RSS)
            └── hl-node (the actual node, 12-70 GB RSS depending on warmup)
```

`hl-visor` is the supervisor — it downloads & verifies new `hl-node`
binaries (via GPG signature using
`gpg --import https://raw.githubusercontent.com/hyperliquid-dex/node/main/pub_key.asc`),
spawns the node, restarts it on crash.

## Publisher

Two implementations live side by side, byte-equivalent on the wire (last
verified 2026-06-11 with a live n1 sxs run: 100/100 match against WS
chain-truth, time-aligned).

- **C++** — primary going forward. Source: `src/pktrade/toolbins/hl_node_publisher.cc`.
  Built via `./noblebuild.sh hl_node_publisher` from the dev box (uses
  the noble Docker image to produce a binary that matches n1's
  glibc/libstdc++ ABI). Output: `noble.bin/hl_node_publisher`. `scp` to
  `~/hl_node_publisher` on n1.
- **Python** — `overmind/strat_main/tools/hl_node_publish.py`. Runs out
  of `~/.venvs/v1/bin/python`. Still canonical for `--tail-fills-dir`
  (fills mode not yet ported to C++) and for the debug instrumentation
  (`--trace-pxs`, `--watch-pxs`, `--log-rule1`).

Both honor the same CLI: `--tail-dir`, `--bootstrap`, `--coin`,
`--endpoint`, `--coins-blacklist-file`, `--poll-interval`, `-v`.

Suspected publisher misbehavior? See bug history in
`ws2_open_items.md`; live debug bundle in
`~/scratch/hl_publisher_debug_20260610/` (op tracer, watch-emit log,
time-aligned compare script).

## Troubleshooting playbook

**Symptom: `is-active` returns `failed` or `activating (auto-restart)`**

```bash
sudo journalctl -u hl-visor.service --since "30 min ago" -o cat | tail -50
ls -t ~/hl/data/visor_child_stderr/*/96/ | head -3   # latest crash logs
cat $(ls -t ~/hl/data/visor_child_stderr/*/96/* | head -1)
```

Common causes: gossip config missing or invalid, GPG verify failed, OOM
(check `dmesg`), disk full (`df -h /`).

**Symptom: sync lag growing instead of shrinking after restart**

- Check peer connections: `journalctl -u hl-visor.service | grep "received abci"`
- Check CPU and disk pressure: `top -b -n1 | head -20; iostat -x 1 3`
- If iowait is high, the gp3 throughput might be saturated; bump
  throughput on the volume.

**Symptom: data dirs not growing**

- Verify the right flags are enabled in the unit's ExecStart
- Check the journal for "writing replica_cmds data to" — appears on
  successful node startup; absence means the node hasn't progressed past
  bootstrap.

**Symptom: disk filling up**

```bash
sudo /usr/local/bin/hl_data_retain.sh                 # force a retention pass
sudo journalctl -t hl_data_retain -S "1 hour ago"     # see what it did
du -sh ~/hl/data/*/ | sort -h                         # find the culprit
crontab -l                                             # is the cron still installed?
systemctl status cron                                 # is cron itself running?
```

## Quick reference card

```bash
# Status
sudo systemctl status hl-visor.service
sudo journalctl -u hl-visor.service -f
df -h /

# Data
tail -f ~/hl/data/node_raw_book_diffs_streaming/hourly/$(date -u +%Y%m%d)/$(date -u +%-H)
tail -f ~/hl/data/node_trades_streaming/hourly/$(date -u +%Y%m%d)/$(date -u +%-H)
curl -s -X POST http://localhost:3001/info -H "Content-Type: application/json" -d '{"type":"meta"}' | jq .

# Restart
sudo systemctl restart hl-visor.service

# Disk
sudo /usr/local/bin/hl_data_retain.sh
sudo journalctl -t hl_disk_alert -p warning..crit -S today
```
