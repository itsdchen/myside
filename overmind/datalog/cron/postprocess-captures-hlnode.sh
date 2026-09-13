#!/bin/bash
# Postproc for HyperliquidNodeV1: download the raw .pb.gz files written
# by daily-capture-hlnode.sh, split per (sym, stream) via
# hlnode_gzsplitter, upload the resulting .gzpbf files to s3.
#
# Usage: ./postprocess-captures-hlnode.sh [YYYYMMDD]
# (no date arg → defaults to two days ago, matching the WS postproc.)

set -e

MKT=HyperliquidNodeV1

if [ -n "$1" ]; then
    DATE=$1
else
    DATE=$(date +"%Y%m%d" --date="2 days ago")
fi

CAPTURENUM=1   # All current cron entries use capturenum=1; if we ever
               # parallelize captures across boxes we'll generalize.

mkdir -p "raw/$MKT" "gzpbf/$MKT"
source ~/.venvs/v1/bin/activate

echo "Postproc $MKT $DATE — waiting for capture done markers..."

# Wait for BOTH stream done markers. Mirrors postprocess-captures.sh's
# poll loop. Captures wrap up at end-of-day; this runs after midnight
# and may legitimately need to wait a bit.
while true; do
    sleep 5
    set +e
    aws s3 ls "s3://l1-pktrade-capture/raw/$MKT/$DATE.$CAPTURENUM.books.done" > /dev/null
    books_done=$?
    aws s3 ls "s3://l1-pktrade-capture/raw/$MKT/$DATE.$CAPTURENUM.fills.done" > /dev/null
    fills_done=$?
    set -e
    if [ $books_done -eq 0 ] && [ $fills_done -eq 0 ]; then
        break
    fi
done

echo "Both done markers present. Downloading raw captures."

# Pull both streams. There's no .$counter restart-file mechanism on the
# hlnode capture side — simple_datalog --hlnode opens in append mode and
# multiple gzip streams concatenate cleanly, so a single file per stream
# is all we expect.
for stream in books fills; do
    raw="raw/$MKT/$DATE.$CAPTURENUM.$stream.pb.gz"
    aws s3 cp "s3://l1-pktrade-capture/$raw" "$raw" --quiet
done

echo "Splitting per (sym, stream) via hlnode_gzsplitter."
./tools/hlnode_gzsplitter \
    --src "raw/$MKT/$DATE.$CAPTURENUM.books.pb.gz" \
    --outdir "gzpbf/$MKT" --date "$DATE"
./tools/hlnode_gzsplitter \
    --src "raw/$MKT/$DATE.$CAPTURENUM.fills.pb.gz" \
    --outdir "gzpbf/$MKT" --date "$DATE"

echo "Uploading per-(sym, stream) .gzpbf files to s3."
aws s3 mv "gzpbf/$MKT" "s3://l1-pktrade-capture/gzpbf/$MKT/" --recursive --quiet

echo "Cleanup."
rm -f "raw/$MKT/$DATE."*

echo "Done."
