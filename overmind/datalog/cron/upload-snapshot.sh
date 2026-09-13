#!/bin/bash

mkt=$1

# Default date to tomorrow
if [ -n "$2" ]; then
    DATE=$2
else
    DATE=$(date +"%Y%m%d" --date="tomorrow")
fi


# Upload to s3
# Do this in a separate job, after all the snapshots of the day are done.
aws s3 mv raw/$mkt/$DATE.snapshot s3://l1-pktrade-capture/raw/$mkt/$DATE.snapshot --quiet
