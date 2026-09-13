# HL Feed — Go-Live Playbook

How to flip from "HL public WS is the canonical data path" to "n1 node
feed is canonical, WS is the hot fallback." Read `README.md` in this
directory first for the architectural overview.

This playbook is intentionally conservative: phased rollout, explicit
checkpoints, well-defined rollback. The system has been built so the
risk of going live is low — node and WS run in parallel on every
consumer, and the per-process health FSM auto-flips back to WS in
seconds if node degrades. So in the worst case "go-live failed" the
trading boxes still trade off WS data exactly as they did before.

---

## 0. Concept

A trading box's `pkmultifeed` / `pymultifeed` config now has TWO blocks
for Hyperliquid:

```json
"subscriptions": {
  "Hyperliquid": {                          // HL public WS (the legacy path)
    "wss_endpoint": "wss://api.hyperliquid.xyz/ws",
    "symbols": ["BTC", "ETH", "..."]
  },
  "HyperliquidNode": {                      // The n1 node feed
    "books_endpoint": "tcp://172.31.37.166:5555",
    "fills_endpoint": "tcp://172.31.37.166:5556",
    "symbols": ["BTC", "ETH", "..."],       // mirror of the WS sym list
    "fallback_silent_s": 5.0,
    "fallback_lag_s": 5.0
  }
}
```

When both are present, the 1 Hz health FSM picks one as the canonical
publisher per sym. Default at startup is node-primary; if node goes
quiet (`silent_s`) or its chain time falls behind (`lag_s`) the FSM
flips to WS within seconds. Strategies don't see a discontinuity
because both feeds are mid-restart-resilient and chain-time-monotonic.

"Go live" = adding the `HyperliquidNode` block to a trading box's
config. No code change required; the binary already handles both.

---

## 1. Pre-flight (do once, before flipping any trading box)

Each of these is a yes/no checkpoint. Don't proceed until all are yes.

### 1.1 n1 publisher is healthy
```bash
ssh n1 'sudo systemctl is-active hl-visor hl-publisher-books hl-publisher-fills'
# Expect: active / active / active

ssh n1 'ps -o pid,etime,pcpu,rss -p $(pgrep -f hl_node_publisher_cpp)'
# Expect: both processes alive, RSS < 800 MB (well below the 2 GB cap if we set one)
```

### 1.2 n1 publisher is emitting
```bash
ssh n1 'sudo journalctl -u hl-publisher-books -o cat --since "5 min ago" | grep -E "rotation|events=" | tail'
# Expect: events count incrementing across rotations; anomalies=0 mostly;
# l3_trimmed=N nonzero after the first hour or two of steady-state operation.
```

### 1.3 Disk on n1 is healthy
```bash
ssh n1 'df -h /home | tail -1'
# Expect: <50% used. If >70%, hl_disk_alert.sh will already have fired email.
```

### 1.4 Sunday-2am restart timer is enabled
```bash
ssh n1 'sudo systemctl list-timers hl-publisher-restart.timer'
# Expect: NEXT shows the upcoming Sunday 02:00 EDT.
```

### 1.5 Datalog box is capturing
```bash
# (run on the datalog box that has the HL cron entries)
ls -la raw/HyperliquidNodeV1/$(date +%Y%m%d).1.books.pb.gz 2>/dev/null
ls -la raw/HyperliquidNodeV1/$(date +%Y%m%d).1.fills.pb.gz 2>/dev/null
# Expect: both files exist and are growing; check timestamps.
```
(Datalog is independent of trading-box rollout, but you want this
running before trading-box flip so any post-flip data is captured.)

### 1.6 One trading box has run pkmultifeed against n1 already
This is the validation step that proves the consumer side wires up.
gv0 has been used for this in the past. The smoke is in
`ws2_open_items.md` → "Live smoke of pkmultifeed WS fast+slow merge"
and "E2E test with C++ BookManager on n1 — live" (both done
2026-06-12).

If it's been a while, re-run on gv0:
```bash
ssh gv0 'pgrep -f pkmultifeed_wsmerge && echo already running, kill first'
ssh gv0 'cd ~/test_nodefeed && nohup ./pkmultifeed_wsmerge --conf test_feed_cpp.json --date TODAY > smoke.log 2>&1 &'
sleep 30
ssh gv0 'grep -cE "HL node books relay|HL node fills relay" ~/test_nodefeed/smoke.log'
# Expect: nonzero, growing. If zero, pkmultifeed isn't seeing n1 data.
ssh gv0 'pkill -INT -f pkmultifeed_wsmerge'
```

---

## 2. Phased rollout

The fleet is small enough that phasing by "one box first, then watch,
then the rest" is sufficient. Don't roll out all boxes simultaneously.

### Phase A — gv0 only (the dev/test box)
gv0 is the box we already use for smoke tests, so this is mostly a
formality, but it's the canonical "first prod box on the new feed."

1. Edit `~/test_nodefeed/test_feed_cpp.json` (or whichever feed config
   gv0's prod consumer uses) to include both `Hyperliquid` and
   `HyperliquidNode` blocks per the section 0 template. Sym lists in
   the two blocks must match — the gates in pkmultifeed don't make any
   sense if the universes disagree.
2. Restart the consumer.
3. Watch for **30 minutes** (longer is fine). What you're checking for
   is in §3.

### Phase B — the rest of the fleet, one box at a time
Order: `gf0 → gf1 → gf2 → gf3 → qf1`. Wait 10-15 minutes between
boxes to confirm nothing degrades. If gv0 has been running clean for
24h+, you can compress this to "all at once" — but the conservative
default is one box at a time.

For each box:
1. Edit the trading box's `feed_config.json` to add the
   `HyperliquidNode` block alongside the existing `Hyperliquid`
   block.
2. Restart the consumer (pkmultifeed / pymultifeed, whichever the
   box runs).
3. Watch §3 metrics for ~10 minutes before moving to the next box.

The fills publisher on n1 doesn't gate on consumer count — adding
more SUBs doesn't impact n1. ZMQ PUB on the n1 side fans out to all
subscribers.

---

## 3. What to watch during rollout

### 3.1 The HL-NODE-UNHEALTHY email
The first time a consumer flags the node feed unhealthy AND has a WS
fallback configured, it fires a one-shot email per process run from
`pktrade::util::Mailer` (C++) or `email_utils.send_alerts` (Python).
**You should NOT see this email during normal rollout.** If you do:
- Check n1 first: `ssh n1 'sudo systemctl status hl-publisher-books hl-publisher-fills'`
- If n1 is fine, the trading box's network path to n1 may be the
  problem. Test: `ssh <box> 'nc -z 172.31.37.166 5555 && nc -z 172.31.37.166 5556'`
- The consumer auto-fell back to WS, so trading is uninterrupted;
  investigate at a calm pace.

### 3.2 The trading box's own publisher journal
```bash
ssh <box> 'sudo journalctl -u <consumer-service> -o cat --since "5 min ago" | grep -E "HL node|publishing.*node|HL.*delay_ms|fallback|silent"'
# Expect periodic "HL node books relay msgs=N publishing=yes" and
# "HL <sym> trade trade_time T delay_ms D" lines. The delay_ms should
# be in the low hundreds at most, not five-digit.
```

### 3.3 Strategy behavior
You shouldn't see anything different. Strategies see merged book +
trade messages from `pkfeedclient`; whether those came from the WS
or node path is invisible above the consumer. The smoke-test
comparison in `ws2_open_items.md` confirmed 100.00% BBO match on the
overlap window, so the data shape is identical.

If any strategy fires a "feed inconsistent" alert in the first 24h,
that's the actionable signal — pull the trigger on §4 rollback for
that box.

### 3.4 n1 disk + CPU
```bash
ssh n1 'df -h /home; ps -o pid,pcpu,rss -p $(pgrep -f hl_node_publisher_cpp)'
```
Disk shouldn't change much because consumers don't write to n1.
CPU on the books publisher should stay around 10-20% (single core);
RSS bounded by the L3 cap.

---

## 4. Rollback

Each step is independent — you can roll back one box without
touching others.

### 4.1 Per-box rollback
Edit the box's feed config to REMOVE the `HyperliquidNode` block,
leaving the `Hyperliquid` (WS) block alone. Restart the consumer. The
box is back on WS-only for HL data.

```diff
 "subscriptions": {
   "Hyperliquid": { ... },
-  "HyperliquidNode": { ... }
 }
```

### 4.2 Whole-fleet rollback
Do §4.1 across every trading box. n1 keeps running but no consumer
listens to it; that's fine — n1 doesn't care.

### 4.3 If you also want to take down n1
You almost certainly don't want to do this — leave n1 running so the
datalog box keeps capturing. But if for some reason you need to:
```bash
ssh n1 'sudo systemctl stop hl-publisher-books hl-publisher-fills'
# (Leave hl-visor up so chain data keeps being written; just the
# publisher fan-out goes silent.)
```

---

## 5. Post-go-live verification (24h after Phase B completes)

Once every box is on the new config and has run for ~24h:

1. **No silence emails in inbox.** Inbox grep for `HL NODE FEED UNHEALTHY`
   and `HYPERLIQUID FEED SILENT` should return zero hits.
2. **n1 weekend timer fired clean.** Find the next Sunday 02:00 EDT
   restart journal entry and confirm both services came back up
   within 30s and reached caught-up state for all syms within
   another minute.
3. **n1 RSS is bounded.** `ssh n1 'ps -o rss -p $(pgrep -f hl_node_publisher_cpp)'`
   should show the books publisher between 100 MB and 800 MB. If it's
   climbing past 800 MB at week-end, the L3 trim isn't keeping up;
   investigate.
4. **Trading PnL didn't regress.** This is the actual ground-truth
   check. If strategies are running consistent strategies day-over-day
   and PnL drops on the day we flip, that's the signal something
   feed-side is wrong. Roll back per §4.

---

## 6. After go-live: what changes operationally

- **Routine restart** of n1 publishers happens Sunday 02:00 EDT
  via the systemd timer. Nothing to do manually. The Sunday cadence
  is chosen to land safely after HL's own network restart window
  (roughly Saturday ~04:00 ET), so HL chain state has had ~22h to
  settle before our publishers re-bootstrap.
- **Routine binary upgrade**: build via `noblebuild.sh`, scp to n1 as
  `/tmp/hl_node_publisher_cpp.new`, rename `/home/ubuntu/hl_node_publisher_cpp`
  to `.prev`, mv new into place, `systemctl restart hl-publisher-books
  hl-publisher-fills`. ~15s of WS-fallback during the restart, then
  back. If something's wrong, mv `.prev` back and restart again.
- **Emergency rollback to WS-only**: §4 above.
- **Monitoring posture**: passive. The consumer-side silence emails are
  the only proactive alert. Disk alerts come from n1's own cron.
- **What stays on HL public WS**: nothing trading-critical; node is
  primary. WS is the hot fallback only.

---

## Related docs

- `README.md` — architectural overview
- `n1_navigation_guide.md` — operating n1
- `n1_provision_runbook.md` — building a new n1 from scratch
- `ws2_open_items.md` — current state of what's done and what's still
  open
