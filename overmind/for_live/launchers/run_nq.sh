#!/bin/bash
# Unified launcher for trading strategies.
# Usage: ./run_strategy.sh [--dry-run] [--list] STRATEGY_NAME

set -euo pipefail

SCRIPT_NAME=$(basename "$0")
AVAILABLE_STRATEGIES=(
    main_usday
    main_preus
    allday_superwide
    allday_narrow
    allday_narrow_centered
    small_break
    long_break
)

usage() {
    cat <<USAGE
Usage: $SCRIPT_NAME [--dry-run] [--list] STRATEGY

Options:
  --dry-run   Print the command without executing it.
  --list      Show known strategies and exit.
  --help      Show this message and exit.

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
    printf '%s\n' "${AVAILABLE_STRATEGIES[@]}"
    exit 0
fi

if [[ ${#POSITIONAL[@]} -lt 1 ]]; then
    echo "Missing strategy name" >&2
    usage
    exit 1
fi

STRATEGY=${POSITIONAL[0]}

TODAY=$(date +%Y%m%d)
TOMORROW=$(date -d "tomorrow" +%Y%m%d)
DAY_AFTER=$(date -d "+2 days" +%Y%m%d)

BASE_DIR="/home/ubuntu/estrader"
LOG_DIR="$BASE_DIR/logs"
DAILY_LOG="$LOG_DIR/daily_trading_${TODAY}.log"
PK_EXEC="$BASE_DIR/pktrade"

if [[ ! -x "$PK_EXEC" ]]; then
    echo "Warning: $PK_EXEC is not executable" >&2
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
    main_usday)
        DESCRIPTION="Running main_process DATE=$TODAY"
        STRAT_ID="nqusday"
        CMD=("$PK_EXEC" --conf nq/usday/pk_nq_usday.json --date "$TODAY" --mainnet --out-dir nq/usday --full-output --live --init-sleep-secs 15 -i "$STRAT_ID")
        STDOUT_LOG="$LOG_DIR/pk_stdout_${STRAT_ID}_${TODAY}.txt"
        ;;
    main_preus)
        DESCRIPTION="Running main_process DATE=$TOMORROW"
        STRAT_ID="nqpreus"
        CMD=("$PK_EXEC" --conf nq/preus/pk_nq_preus.json --date "$TOMORROW" --mainnet --out-dir nq/preus --full-output --live --init-sleep-secs 15 -i "$STRAT_ID")
        STDOUT_LOG="$LOG_DIR/pk_stdout_mainnet_${STRAT_ID}_${TODAY}.txt"
        ;;
    allday_superwide)
        DESCRIPTION="Running allday_superwide DATE=$TOMORROW"
        INSTANCE_ID="superwide$TODAY"
        CMD=("$PK_EXEC" --conf nq/allday_superwide/pk_nq_rel_btc_allday_superwide.json --date "$TODAY" --end-date "$TOMORROW" --mainnet --out-dir nq/allday_superwide --full-output --live --init-sleep-secs 15 -i "$INSTANCE_ID")
        STDOUT_LOG="$LOG_DIR/pk_stdout_${TODAY}.txt"
        ;;
    allday_narrow)
        DESCRIPTION="Running allday_narrow DATE=$TOMORROW"
        INSTANCE_ID="narrow$TODAY"
        CMD=("$PK_EXEC" --conf nq/allday_narrow/pk_nq_rel_btc_allday_narrow.json --date "$TODAY" --end-date "$TOMORROW" --mainnet --out-dir nq/allday_narrow --full-output --live --init-sleep-secs 15 -i "$INSTANCE_ID")
        STDOUT_LOG="$LOG_DIR/pk_stdout_${TODAY}.txt"
        ;;
    allday_narrow_centered)
        DESCRIPTION="Running allday_narrow_centered DATE=$TOMORROW"
        INSTANCE_ID="narrowcentered$TODAY"
        CMD=("$PK_EXEC" --conf nq/allday_narrow_centered/pk_nq_rel_btc_allday_narrow.json --date "$TODAY" --end-date "$TOMORROW" --mainnet --out-dir nq/allday_narrow_centered --full-output --live --init-sleep-secs 15 -i "$INSTANCE_ID")
        STDOUT_LOG="$LOG_DIR/pk_stdout_${TODAY}.txt"
        ;;
    small_break)
        DESCRIPTION="Running small_break for $TODAY"
        STRAT_ID="nqsmallbreak"
        CMD=("$PK_EXEC" --conf nq/smallbreak_daily/pk_nq_rel_btc_breakpd.json --date "$TODAY" --mainnet --out-dir nq/smallbreak_daily --full-output --live --init-sleep-secs 15 -i "$STRAT_ID" --beacon-always-deliver)
        STDOUT_LOG="$LOG_DIR/pk_stdout_${STRAT_ID}_${TODAY}.txt"
        ;;
    long_break)
        DESCRIPTION="Running long_break from $TODAY to $TOMORROW"
        STRAT_ID="nqbigbreak"
        CMD=("$PK_EXEC" --conf nq/longbreak_btc/pk_nq_rel_btc_allday.json --date "$TODAY" --end-date "$TOMORROW" --mainnet --out-dir nq/longbreak_btc/ --full-output --live --init-sleep-secs 15 -i "$STRAT_ID" --beacon-always-deliver)
        STDOUT_LOG="$LOG_DIR/pk_stdout_${STRAT_ID}_${TODAY}.txt"
        ;;
    *)
        echo "Unknown strategy: $STRATEGY" >&2
        echo "Known strategies: ${AVAILABLE_STRATEGIES[*]}" >&2
        exit 1
        ;;
esac

CMD_STRING=$(join_cmd "${CMD[@]}")
CMD_STRING=${CMD_STRING%% } # trim trailing space

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
CMD_EXIT=${PIPESTATUS[0]}
set -e

if [[ $CMD_EXIT -eq 0 ]]; then
    echo "$(date): Command completed successfully" >> "$DAILY_LOG"
else
    echo "$(date): Command exited with status $CMD_EXIT" | tee -a "$DAILY_LOG"
fi

exit $CMD_EXIT
