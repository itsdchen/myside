#!/bin/bash
# hl_data_retain: gzip + delete rotation for HL node files.
# Tunable via env vars (used by hl_disk_alert.sh in panic mode):
#   GZIP_AFTER_HOURS        default 2    (streaming dirs: gzip after N hours)
#   DELETE_AFTER_HOURS      default 72   (streaming dirs: delete gz after N hours)
#   AGGRESSIVE_DELETE_HOURS default 1    (replica_cmds + abci_states: delete after N hours)
#
# Run via cron with defaults, or from hl_disk_alert.sh with tighter values
# when disk usage is high.
set -e

GZIP_AFTER_HOURS=${GZIP_AFTER_HOURS:-2}
DELETE_AFTER_HOURS=${DELETE_AFTER_HOURS:-72}
AGGRESSIVE_DELETE_HOURS=${AGGRESSIVE_DELETE_HOURS:-1}

# Prevent concurrent runs. flock blocks if held; we want to be patient
# (a normal pass finishing is fine; we just don't want pile-ups).
LOCKFILE=/var/lib/hl_data_retain/hl_data_retain.lock
exec 9<>"$LOCKFILE" 2>/dev/null || { logger -t hl_data_retain "can't open lock"; exit 1; }
if ! flock -n 9; then
  logger -t hl_data_retain "another instance is running; exiting"
  exit 0
fi

# Convert hours to minutes (find -mmin uses minutes). Use awk to handle decimals.
GZIP_MINUTES=$(awk -v h="$GZIP_AFTER_HOURS" 'BEGIN{printf "%d", h*60}')
DELETE_MINUTES=$(awk -v h="$DELETE_AFTER_HOURS" 'BEGIN{printf "%d", h*60}')
AGG_MINUTES=$(awk -v h="$AGGRESSIVE_DELETE_HOURS" 'BEGIN{printf "%d", h*60}')

DIRS_GZ=(
  /home/ubuntu/hl/data/node_raw_book_diffs_streaming/hourly
  /home/ubuntu/hl/data/node_trades_streaming/hourly
  /home/ubuntu/hl/data/node_fills_streaming/hourly
  /home/ubuntu/hl/data/node_order_statuses_streaming/hourly
  /home/ubuntu/hl/data/node_twap_statuses_streaming/hourly
  /home/ubuntu/hl/data/hip3_oracle_updates_streaming/hourly
  /home/ubuntu/hl/data/evm_block_and_receipts/hourly
  /home/ubuntu/hl/data/visor_child_stderr
)

DIRS_DROP=(
  /home/ubuntu/hl/data/replica_cmds
  /home/ubuntu/hl/data/periodic_abci_states
)

logger -t hl_data_retain "starting (gzip>${GZIP_AFTER_HOURS}h, delete>${DELETE_AFTER_HOURS}h, aggressive>${AGGRESSIVE_DELETE_HOURS}h)"

for D in "${DIRS_GZ[@]}"; do
  [ -d "$D" ] || continue
  find "$D" -type f ! -name "*.gz" -mmin +${GZIP_MINUTES} -print0 2>/dev/null \
    | while IFS= read -r -d '' f; do
        if [ -s "$f" ]; then gzip -q "$f" 2>/dev/null && logger -t hl_data_retain "gz $f"; fi
      done
  find "$D" -type f -name "*.gz" -mmin +${DELETE_MINUTES} -delete 2>/dev/null || true
done

for D in "${DIRS_DROP[@]}"; do
  [ -d "$D" ] || continue
  BEFORE=$(du -sb "$D" 2>/dev/null | awk '{print $1}')
  find "$D" -type f -mmin +${AGG_MINUTES} -delete 2>/dev/null || true
  find "$D" -mindepth 1 -type d -empty -delete 2>/dev/null || true
  AFTER=$(du -sb "$D" 2>/dev/null | awk '{print $1}')
  logger -t hl_data_retain "$D before=$BEFORE after=$AFTER"
done

logger -t hl_data_retain "done"
