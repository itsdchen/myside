#!/bin/bash
# Generic feed launcher with auto-restart.
# Runs the feed in a loop, restarting on unexpected death if before 17:00.
# Does NOT restart if the process was killed by a user (SIGTERM/SIGKILL).
#
# Usage: ./run_feed.sh BIN CONF_PATH today|tomorrow
# Example: ./run_feed.sh cppfeed/pkmultifeed_2 feed.gf0.cpp.json tomorrow
#
# Designed to replace the raw cron line:
#   0 18 * * * cd /home/ubuntu/estrader/cppfeed && ./pkmultifeed_2 --conf feed.gf0.cpp.json --date TOMORROW >> ...

set -euo pipefail

SCRIPT_NAME=$(basename "$0")

usage() {
    cat <<USAGE
Usage: $SCRIPT_NAME BIN CONF_PATH today|tomorrow

  BIN         Relative path to executable, e.g. cppfeed/pkmultifeed_2
  CONF_PATH   Config file name, e.g. feed.gf0.cpp.json
  today       Use --date TODAY
  tomorrow    Use --date TOMORROW
USAGE
}

if [[ $# -lt 3 ]]; then
    echo "Missing arguments" >&2
    usage >&2
    exit 1
fi

BIN_PATH=$1
CONF_PATH=$2
DAY_ARG=$3

BASE_DIR="/home/ubuntu/estrader"
LOG_DIR="$BASE_DIR/logs"
RUNNER_LOG="$LOG_DIR/feed_runner_$(date +%Y%m%d).log"
PK_EXEC="$BASE_DIR/$BIN_PATH"
WORK_DIR=$(dirname "$PK_EXEC")
BIN_NAME=$(basename "$PK_EXEC")

# Exit codes from signals: 128 + signal number.
EXIT_SIGTERM=143  # kill / kill -15
EXIT_SIGKILL=137  # kill -9

# Don't restart if within this many seconds of 18:00.
CUTOFF_HOUR=17
RESTART_DELAY=5

mkdir -p "$LOG_DIR"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [run_feed] $*" >> "$RUNNER_LOG"
}

resolve_date_flag() {
    case "$DAY_ARG" in
        today)    date +%Y%m%d ;;
        tomorrow) date -d "tomorrow" +%Y%m%d ;;
        *)
            echo "Invalid day argument '$DAY_ARG': expected 'today' or 'tomorrow'" >&2
            exit 1
            ;;
    esac
}

should_restart() {
    local now cutoff_readable current_readable
    now=$(date +%s)
    if (( now >= CUTOFF_TS )); then
        cutoff_readable=$(date -d "@$CUTOFF_TS" '+%Y-%m-%d %H:%M')
        current_readable=$(date -d "@$now" '+%Y-%m-%d %H:%M')
        log "Current time ($current_readable) is past cutoff ($cutoff_readable), not restarting."
        return 1
    fi
    return 0
}

was_user_killed() {
    local exit_code=$1
    if [[ $exit_code -eq $EXIT_SIGTERM || $exit_code -eq $EXIT_SIGKILL ]]; then
        return 0
    fi
    return 1
}

DATE_VAL=$(resolve_date_flag)
CUTOFF_TS=$(date -d "${DATE_VAL} ${CUTOFF_HOUR}:00" +%s)
FEED_LOG="$LOG_DIR/feed_logs_$(date +%Y%m%d).log"

log "Starting feed loop: $BIN_NAME --conf $CONF_PATH --date $DATE_VAL"
log "Log file: $FEED_LOG"

while true; do
    cd "$WORK_DIR"
    log "Launching: ./$BIN_NAME --conf $CONF_PATH --date $DATE_VAL"

    set +e
    ./"$BIN_NAME" --conf "$CONF_PATH" --date "$DATE_VAL" >> "$FEED_LOG" 2>&1
    EXIT_CODE=$?
    set -e

    log "Feed exited with code $EXIT_CODE."

    if [[ $EXIT_CODE -eq 0 ]]; then
        log "Clean exit, stopping."
        break
    fi

    if was_user_killed $EXIT_CODE; then
        log "Process was killed (exit $EXIT_CODE), not restarting."
        break
    fi

    if ! should_restart; then
        break
    fi

    # If we crossed midnight, the session date is now "today" not "tomorrow".
    if [[ "$DAY_ARG" == "tomorrow" ]]; then
        NEW_DATE=$(date +%Y%m%d)
        if [[ "$NEW_DATE" == "$DATE_VAL" ]]; then
            log "Past midnight, switching --date from TOMORROW to TODAY ($DATE_VAL)."
            DAY_ARG="today"
        fi
    fi

    log "Unexpected death, restarting in ${RESTART_DELAY}s..."
    sleep $RESTART_DELAY
done

log "Feed loop finished."
