#!/bin/bash
# Unified launcher for equity trading strategies.
# Usage: ./run_eq.sh [--dry-run] [--list] EQ STRATEGY
# Example: ./run_eq.sh tsla usday
# Example: ./run_eq.sh --dry-run tsla breakpd

set -euo pipefail

SCRIPT_NAME=$(basename "$0")

# NOTE THAT THESE NAMES SHOULD BE UNIQUE ACROSS MACHINES.
# two machines running combined_equities strategies would collide a TON.
AVAILABLE_EQUITIES=(
    combined_exotic_gf0
)


AVAILABLE_STRATEGIES=(
    allday_v1
)

contains() {
    local needle=$1
    shift
    for item in "$@"; do
        if [[ $item == "$needle" ]]; then
            return 0
        fi
    done
    return 1
}

usage() {
    cat <<USAGE
Usage: $SCRIPT_NAME [--dry-run] [--list] EQUITY STRATEGY

Options:
  --dry-run   Print the command without executing it.
  --list      Show known equities and strategies, then exit.
  --help      Show this message and exit.

Known equities:
$(printf '  %s\n' "${AVAILABLE_EQUITIES[@]}")

Known strategies:
$(printf '  %s\n' "${AVAILABLE_STRATEGIES[@]}")
USAGE
}

DRY_RUN=0
SHOW_LIST=0
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --list)
            SHOW_LIST=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

if [[ $SHOW_LIST -eq 1 ]]; then
    printf 'Known equities:\n'
    printf '  %s\n' "${AVAILABLE_EQUITIES[@]}"
    printf '\nKnown strategies:\n'
    printf '  %s\n' "${AVAILABLE_STRATEGIES[@]}"
    exit 0
fi

if [[ ${#POSITIONAL[@]} -lt 2 ]]; then
    echo "Missing equity or strategy" >&2
    usage
    exit 1
fi

EQUITY_RAW=${POSITIONAL[0]}
EQUITY=$(printf '%s' "$EQUITY_RAW" | tr '[:upper:]' '[:lower:]')
STRATEGY=${POSITIONAL[1]}

if ! contains "$EQUITY" "${AVAILABLE_EQUITIES[@]}"; then
    echo "Unknown equity: $EQUITY_RAW" >&2
    echo "Known equities: ${AVAILABLE_EQUITIES[*]}" >&2
    exit 1
fi

if ! contains "$STRATEGY" "${AVAILABLE_STRATEGIES[@]}"; then
    echo "Unknown strategy: $STRATEGY" >&2
    echo "Known strategies: ${AVAILABLE_STRATEGIES[*]}" >&2
    exit 1
fi

TODAY=$(date +%Y%m%d)
TOMORROW=$(date -d "tomorrow" +%Y%m%d)

BASE_DIR="/home/ubuntu/estrader"
LOG_DIR="$BASE_DIR/logs"
DAILY_LOG="$LOG_DIR/daily_trading_${TODAY}.log"
PK_EXEC="$BASE_DIR/pktrade"
EQUITY_DIR="$BASE_DIR/$EQUITY"

if [[ ! -x "$PK_EXEC" ]]; then
    echo "Warning: $PK_EXEC is not executable" >&2
fi

if [[ ! -d "$EQUITY_DIR" ]]; then
    echo "Warning: equity directory $EQUITY_DIR does not exist" >&2
fi

mkdir -p "$LOG_DIR"
cd "$BASE_DIR"

DESCRIPTION=""
STDOUT_LOG=""
CMD=()

join_cmd() {
    local IFS=' '
    printf '%q ' "$@"
}

case "$STRATEGY" in
    allday_v1)
        DESCRIPTION="Running $EQUITY allday_v1 for $TODAY"
        STRAT_ID="${EQUITY}allday_v1"
        CMD=("$PK_EXEC" --conf "$EQUITY/allday_v1/pk_allday.json" --date "$TODAY"  --end-date "$TOMORROW" --mainnet \
             --out-dir "$EQUITY/allday_v1" --full-output --live --init-sleep-secs 15 -i "$STRAT_ID")
        STDOUT_LOG="$LOG_DIR/pk_stdout_${STRAT_ID}_${TODAY}.txt"
        ;;


    rel_btc_narrow)
        DESCRIPTION="Running $EQUITY rel_btc_narrow for $TODAY"
        STRAT_ID="${EQUITY}_btc_narrow"
        CMD=("$PK_EXEC" --conf "$EQUITY/rel_btc_narrow/pk_rel_btc_narrow.json" --date "$TODAY"  --end-date "$TOMORROW" --mainnet \
             --out-dir "$EQUITY/rel_btc_narrow" --full-output --live --init-sleep-secs 15 -i "$STRAT_ID" --beacon-always-deliver)
        STDOUT_LOG="$LOG_DIR/pk_stdout_${STRAT_ID}_${TODAY}.txt"
        ;;
    *)
        echo "Unknown strategy: $STRATEGY" >&2
        exit 1
        ;;
esac

CMD_STRING=$(join_cmd "${CMD[@]}")
CMD_STRING=${CMD_STRING%% }

{
    echo "$(date): $DESCRIPTION"
    echo "$(date): About to run: $CMD_STRING"
} >> "$DAILY_LOG"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "Dry-run: would execute $CMD_STRING"
    exit 0
fi

set +e
"${CMD[@]}" >> "$STDOUT_LOG"
CMD_EXIT=$?
set -e

if [[ $CMD_EXIT -eq 0 ]]; then
    echo "$(date): Command completed successfully" >> "$DAILY_LOG"
else
    echo "$(date): Command exited with status $CMD_EXIT" >> "$DAILY_LOG"
fi

exit $CMD_EXIT
