#!/bin/bash
# Generic strategy launcher.
# Usage: ./run_strat.sh [--dry-run] BIN CONF_PATH today|tomorrow
# Example: ./run_strat.sh pktrade combined_equities_bfx1/usday/pk_usday.json today
# Example: ./run_strat.sh pktrade_1 combined_equities_bfx1/rel_nq_allday/pk_rel_nq_allday.json tomorrow

set -euo pipefail

SCRIPT_NAME=$(basename "$0")

usage() {
    cat <<USAGE
Usage: $SCRIPT_NAME [--dry-run] BIN CONF_PATH today|tomorrow

  BIN         Executable name, e.g. pktrade, pktrade_1
  CONF_PATH   Full config path, e.g. combined_equities_bfx1/usday/pk_usday.json
  today       Starts on its L1Date (e.g. usday at 09:00): --date TODAY
  tomorrow    Starts the evening before its L1Date (an 18:00 allday, or a 19:00/20:00
              Asia session, incl. ones that end the same evening like Japan AM): --date TOMORROW.
              pktrade maps --date (the L1Date) to the real start/end window internally.
USAGE
}

DRY_RUN=0
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

if [[ ${#POSITIONAL[@]} -lt 3 ]]; then
    echo "Missing arguments" >&2
    usage >&2
    exit 1
fi

BIN_NAME=${POSITIONAL[0]}
CONF_PATH=${POSITIONAL[1]}
DAY_ARG=${POSITIONAL[2]}

TODAY=$(date +%Y%m%d)
TOMORROW=$(date -d "tomorrow" +%Y%m%d)

BASE_DIR="/home/ubuntu/estrader"
LOG_DIR="$BASE_DIR/logs"
DAILY_LOG="$LOG_DIR/daily_trading_${TODAY}.log"
PK_EXEC="$BASE_DIR/$BIN_NAME"

if [[ ! -x "$PK_EXEC" ]]; then
    echo "Warning: $PK_EXEC is not executable" >&2
fi

mkdir -p "$LOG_DIR"
cd "$BASE_DIR"

# Derive strat ID from hostname, directory portion of conf path (/ -> _), and date.
HOSTNAME=$(hostname)
STRAT_PREFIX=$(dirname "$CONF_PATH" | tr '/' '_')
STRAT_ID="${HOSTNAME}_${STRAT_PREFIX}_${TODAY}"

case "$DAY_ARG" in
    today)
        # Process starts on its L1Date (e.g. usday at 09:00).
        DATE_FLAGS=(--date "$TODAY")
        ;;
    tomorrow)
        # Process cron-starts the evening before its L1Date (an 18:00 allday, a 19:00/20:00
        # Asia session, or a same-evening session like Japan AM). --date is the L1Date; pktrade
        # rolls both start_t and end_t back a day internally for times >= 18:00 ET (strict > for
        # end_t), so this is all we need to pass.
        DATE_FLAGS=(--date "$TOMORROW")
        ;;
    *)
        echo "Invalid day argument '$DAY_ARG': expected 'today' or 'tomorrow'" >&2
        exit 1
        ;;
esac

CMD=("$PK_EXEC" --conf "$CONF_PATH" "${DATE_FLAGS[@]}" --mainnet \
     --full-output --live --init-sleep-secs 15 -i "$STRAT_ID" --beacon-always-deliver)
STDOUT_LOG="$LOG_DIR/pk_stdout_${STRAT_ID}.txt"

join_cmd() {
    local IFS=' '
    printf '%q ' "$@"
}

CMD_STRING=$(join_cmd "${CMD[@]}")
CMD_STRING=${CMD_STRING%% }

{
    echo "$(date): Running $CONF_PATH ($DAY_ARG) as $STRAT_ID"
    echo "$(date): About to run: $CMD_STRING"
} >> "$DAILY_LOG"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "Dry-run: would execute $CMD_STRING"
    exit 0
fi

set +e
"${CMD[@]}" >> "$STDOUT_LOG" 2>&1
CMD_EXIT=$?
set -e

if [[ $CMD_EXIT -eq 0 ]]; then
    echo "$(date): Command completed successfully" >> "$DAILY_LOG"
else
    echo "$(date): Command exited with status $CMD_EXIT" | tee -a "$DAILY_LOG"
fi

exit $CMD_EXIT
