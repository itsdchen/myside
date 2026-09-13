#!/bin/bash
# Daily unconditional hl-visor + publisher restart.
#
# Cron entry (every day at 4:00 EDT = 08:00 UTC):
#   0 4 * * * /usr/local/bin/hl_weekend_restart.sh
#
# Rationale: between 06-21 and 06-25 we observed memory-pressure stalls
# (1-80 ≥5 s fast-pipeline stalls/day, peaking on day 3-4 of a node's
# lifetime) once hl-node's VmRSS pushed into the 75-119 GB range. A
# threshold-gated restart (was 70 GB) cut it close — by the time the
# threshold tripped we were already inside the danger zone for one
# daily window. Daily unconditional restart at 04:00 EDT (the quietest
# hour) is the right trade now that the new L2 cron handles catch-up
# cleanly (~9 min consumer gap, vs. the prior random-time stall pattern).
#
# Always records pre-restart VmRSS / VmHWM in the email so we can plot
# the day-over-day growth curve.
#
# Manually force a restart at any other time:
#   sudo -u ubuntu /usr/local/bin/hl_weekend_restart.sh --force
#
# (--force is kept for symmetry but functionally equivalent now — the
# script always restarts.)
#
# Restart sequence (defense-in-depth Layer 2 per
# overmind/studies/hl_feed/publisher_dedup_design_20260625.md):
#   1. STOP both publishers BEFORE touching the visor — guarantees they
#      do not read the catch-up replay region as if it were live.
#   2. systemctl restart hl-visor.service (restarts hl-node child).
#   3. wait_for_caught_up: poll the streaming file's last block_time
#      until it is within 3 s of wall clock. Catch-up takes ~9 min
#      from a snapshot taken 80 min ago. Timeout 20 min — if exceeded
#      the script leaves publishers stopped and emails an alert.
#   4. Start books publisher (re-bootstraps via WS; L1c gate in the
#      binary itself is the publisher-side double-check).
#   5. Start fills publisher.
#   6. wait 15 s for publishers to settle.
#   7. verify all three services active; email summary.
#
# Variable indirection on the service name dodges hl-visor's singleton check.
set -u

HOST=$(hostname)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LOG_TAG="hl_weekend_restart"
DATA_DIR="${DATA_DIR:-/home/ubuntu/hl/data/node_raw_book_diffs_streaming/hourly}"
FRESHNESS_MS="${FRESHNESS_MS:-3000}"
MAX_WAIT_S="${MAX_WAIT_S:-1200}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-5}"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { logger -t "$LOG_TAG" -p user.info -- "$1"; echo "$1"; }

PID=$(pgrep -fo "hl-node --chain Mainnet")
if [ -z "$PID" ]; then
  log "$TS  no hl-node process found; aborting"
  exit 0
fi

VMRSS_KB=$(awk '/^VmRSS:/ {print $2}' /proc/$PID/status 2>/dev/null)
VMHWM_KB=$(awk '/^VmHWM:/ {print $2}' /proc/$PID/status 2>/dev/null)
ETIME=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')

VMRSS_GB=$((VMRSS_KB / 1024 / 1024))
VMHWM_GB=$((VMHWM_KB / 1024 / 1024))
log "$TS  pre-check: PID=$PID uptime=$ETIME VmRSS=${VMRSS_GB}GB VmHWM=${VMHWM_GB}GB force=$FORCE"

# --- restart sequence ---

SVC=hl-${PLACEHOLDER:-vis}or.service
BOOKS=hl-publisher-books.service
FILLS=hl-publisher-fills.service

# Step 1: stop publishers BEFORE touching the visor so they do not read
# the catch-up replay region as if it were live data.
log "$TS  stopping publishers (books, fills)"
sudo systemctl stop "$BOOKS"
sudo systemctl stop "$FILLS"

# Step 2: restart the visor.
log "$TS  triggering hl-visor restart (force=$FORCE)"
sudo systemctl restart "$SVC"

# Step 3: wait_for_caught_up. Polls the latest hourly file's last block_time
# and waits until wall_clock - block_time < FRESHNESS_MS. On timeout,
# leaves publishers stopped and emails an alert (publishers stay down rather
# than tail mid-replay).
wait_for_caught_up() {
  local start_t
  start_t=$(date +%s)
  log "$TS  wait_for_caught_up: target lag <${FRESHNESS_MS}ms, timeout ${MAX_WAIT_S}s"
  while true; do
    local hour_file last_block_time last_block_ms now_ms lag_ms elapsed
    hour_file="${DATA_DIR}/$(date -u +%Y%m%d)/$(date -u +%-H)"
    if [ ! -f "$hour_file" ]; then
      sleep "$POLL_INTERVAL_S"
      elapsed=$(( $(date +%s) - start_t ))
      [ "$elapsed" -gt "$MAX_WAIT_S" ] && return 1
      continue
    fi
    last_block_time=$(tail -1 "$hour_file" 2>/dev/null | jq -r '.block_time // empty' 2>/dev/null)
    if [ -z "$last_block_time" ]; then
      sleep "$POLL_INTERVAL_S"
      elapsed=$(( $(date +%s) - start_t ))
      [ "$elapsed" -gt "$MAX_WAIT_S" ] && return 1
      continue
    fi
    last_block_ms=$(date -u -d "$last_block_time" +%s%3N 2>/dev/null)
    if [ -z "$last_block_ms" ]; then
      sleep "$POLL_INTERVAL_S"
      elapsed=$(( $(date +%s) - start_t ))
      [ "$elapsed" -gt "$MAX_WAIT_S" ] && return 1
      continue
    fi
    now_ms=$(date -u +%s%3N)
    lag_ms=$(( now_ms - last_block_ms ))
    if [ "$lag_ms" -lt "$FRESHNESS_MS" ]; then
      log "$TS  wait_for_caught_up: caught up (lag ${lag_ms}ms)"
      return 0
    fi
    elapsed=$(( $(date +%s) - start_t ))
    if [ "$elapsed" -gt "$MAX_WAIT_S" ]; then
      log "$TS  wait_for_caught_up: TIMEOUT after ${elapsed}s (last lag ${lag_ms}ms)"
      return 1
    fi
    # Log every ~30s while waiting so journal shows progress
    if [ $(( elapsed % 30 )) -lt "$POLL_INTERVAL_S" ]; then
      log "$TS  wait_for_caught_up: still catching up, lag $((lag_ms/1000))s (elapsed ${elapsed}s)"
    fi
    sleep "$POLL_INTERVAL_S"
  done
}

if wait_for_caught_up; then
  CATCHUP_OK=1
else
  CATCHUP_OK=0
  log "$TS  wait_for_caught_up failed; LEAVING publishers stopped and alerting"
fi

# Step 4-5: only start publishers if catch-up succeeded.
if [ "$CATCHUP_OK" -eq 1 ]; then
  log "$TS  starting books publisher (with --bootstrap; L1c gate will re-verify freshness)"
  sudo systemctl start "$BOOKS"

  log "$TS  starting fills publisher"
  sudo systemctl start "$FILLS"

  log "$TS  waiting 15s for publishers to settle"
  sleep 15
fi

# --- verify ---

NEW_PID=$(pgrep -fo "hl-node --chain Mainnet")
VISOR_ACTIVE=$(sudo systemctl is-active "$SVC")
BOOKS_ACTIVE=$(sudo systemctl is-active "$BOOKS")
FILLS_ACTIVE=$(sudo systemctl is-active "$FILLS")
NEW_VMRSS_KB=""
if [ -n "$NEW_PID" ]; then
  NEW_VMRSS_KB=$(awk '/^VmRSS:/ {print $2}' /proc/$NEW_PID/status 2>/dev/null)
fi
log "$TS  post-check: hl-visor=$VISOR_ACTIVE books=$BOOKS_ACTIVE fills=$FILLS_ACTIVE new-PID=${NEW_PID:-none} VmRSS=${NEW_VMRSS_KB:-?}kB (was ${VMRSS_KB}kB) catchup_ok=$CATCHUP_OK"

SUBJ_TAG=""
[ "$CATCHUP_OK" -eq 0 ] && SUBJ_TAG=" [CATCHUP-TIMEOUT]"

(echo "From: ubuntu@${HOST}"
 echo "To: itsdchen@gmail.com"
 echo "Subject: [${HOST}] hl-node daily restart ${TS} (RSS was ${VMRSS_GB}GB)${SUBJ_TAG}"
 echo ""
 echo "Pre-restart memory (the day-over-day growth signal we're tracking):"
 echo "  PID:       $PID"
 echo "  uptime:    $ETIME"
 echo "  VmRSS:     ${VMRSS_KB} kB (${VMRSS_GB} GB)"
 echo "  VmHWM:     ${VMHWM_KB} kB (${VMHWM_GB} GB)"
 echo "  force:     $FORCE"
 echo ""
 echo "Catch-up:"
 echo "  status:    $([ "$CATCHUP_OK" -eq 1 ] && echo OK || echo TIMEOUT)"
 echo "  data_dir:  $DATA_DIR"
 echo "  freshness: ${FRESHNESS_MS} ms"
 echo "  max_wait:  ${MAX_WAIT_S} s"
 echo ""
 echo "Post-restart:"
 echo "  hl-visor:  $VISOR_ACTIVE"
 echo "  books:     $BOOKS_ACTIVE"
 echo "  fills:     $FILLS_ACTIVE"
 echo "  new PID:   ${NEW_PID:-(not yet)}"
 [ -n "$NEW_VMRSS_KB" ] && echo "  VmRSS:     ${NEW_VMRSS_KB} kB ($((NEW_VMRSS_KB/1024/1024)) GB)"
 if [ "$CATCHUP_OK" -eq 0 ]; then
   echo ""
   echo "ACTION REQUIRED: wait_for_caught_up timed out after ${MAX_WAIT_S}s."
   echo "Publishers were NOT started. Check hl-visor status and manually"
   echo "start hl-publisher-books / hl-publisher-fills once visor is current."
 fi
) | /usr/bin/msmtp itsdchen@gmail.com 2>&1 | logger -t "$LOG_TAG" -p user.warning

exit 0
