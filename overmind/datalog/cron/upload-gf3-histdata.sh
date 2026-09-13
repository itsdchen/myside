#!/bin/bash

# Upload gzpbf files from ~/histdata to S3.
# Runs on gf3 directly.
# Usage: ./upload-gf3-histdata.sh [DATE]
# DATE defaults to today (YYYYMMDD).

set -e

MKT=TopBookEquity

if [ -n "$1" ]; then
    DATE=$1
else
    DATE=$(date +"%Y%m%d")
fi

S3_DEST="s3://l1-pktrade-capture/gzpbf/$MKT/"

echo "Uploading histdata for $DATE to $S3_DEST"

for f in ~/histdata/*_${DATE}.gzpbf; do
    [ -e "$f" ] || { echo "No files found for $DATE"; exit 1; }
    echo "Uploading $(basename "$f")"
    aws s3 cp "$f" "$S3_DEST" --quiet --profile l1
done

echo "Done"
