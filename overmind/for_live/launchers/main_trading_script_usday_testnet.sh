#!/bin/bash

set -o pipefail

# Daily trading script with date variables and command types
# Usage: ./daily_trading_script.sh [--TODAY|--TOMORROW|--DAY_AFTER] [--BREAK]

# I want to clarify, these crontabs will be started BEFORE the day's trading should happen. So "today" is basically not used.

# Get date variables
TODAY=$(date +%Y%m%d)
TOMORROW=$(date -d "tomorrow" +%Y%m%d)
HOUTIAN=$(date -d "+2 days" +%Y%m%d)

# Default values

# Log the execution
echo "$(date): Running main_process DATE=$TODAY" >> /home/ubuntu/estrader/logs/daily_trading_$TODAY.log

cd /home/ubuntu/estrader/

# At some point, this should be a list of commands.

STRAT_ID="nqusday"

BASE_CMD="./pktrade --testnet --conf nq/usday/pk_nq_usday.json --date $TODAY --out-dir nq/usday --full-output --live --init-sleep-secs 15 -i $STRAT_ID 2>&1 | tee -a /home/ubuntu/estrader/logs/pk_stdout_${STRAT_ID}_$TODAY.txt"

echo "About to run $BASE_CMD" >> /home/ubuntu/estrader/logs/daily_trading_$TODAY.log
# Execute the command
eval $BASE_CMD
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "$(date): Main process command completed successfully" >> /home/ubuntu/estrader/logs/daily_trading_$TODAY.log
else
    echo "$(date): Main process command exited with status $EXIT_CODE" | tee -a /home/ubuntu/estrader/logs/daily_trading_$TODAY.log
fi

exit $EXIT_CODE
