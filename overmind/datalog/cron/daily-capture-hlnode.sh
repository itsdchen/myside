#!/bin/bash
# Daily capture for the Hyperliquid Node feed published by the n1
# hl_node_publisher_cpp services. Two ZMQ PUB endpoints (books + fills)
# → two cron entries, one per stream, sharing this script.
#
# Example:
#   ./daily-capture-hlnode.sh 20260612 books 1
#   ./daily-capture-hlnode.sh 20260612 fills 1
#
# Output (per stream): raw/HyperliquidNodeV1/$DATE.$capturenum.$STREAM.pb.gz
# Restart files (if simple_datalog crashes & is relaunched within this
# script's `while` loop) append to the SAME file via gzip's stream
# concatenation property, so we don't need a `.$counter` suffix per
# restart the way the WS captures do.
#
# Uploads to s3://l1-pktrade-capture/raw/HyperliquidNodeV1/

DATE=${1}
STREAM=${2}
capturenum=${3:-1}

if [ -z "$DATE" ] || [ -z "$STREAM" ]; then
    echo "Usage: $0 <date YYYYMMDD> <books|fills> [capturenum]"
    exit 1
fi

# n1's private-VPC IP. Trading boxes inside the same VPC reach it
# directly; this script is intended to run on those boxes (or any box
# that has IP-level access to n1).
NODE_HOST="172.31.37.166"

case $STREAM in
    books)  PORT=5555 ;;
    fills)  PORT=5556 ;;
    *)      echo "Invalid stream (expected books|fills): $STREAM"; exit 1 ;;
esac
ENDPOINT="tcp://${NODE_HOST}:${PORT}"

MKT=HyperliquidNodeV1
outname="raw/$MKT/$DATE.$capturenum.$STREAM.pb.gz"
mkdir -p "raw/$MKT"

source ~/.venvs/v1/bin/activate

# Infinite retry loop — same pattern as daily-capture.sh. simple_datalog
# --hlnode exits cleanly when it crosses --date's EOD, so a clean exit
# means "end of day reached, time to upload."
while true; do
    ./tools/simple_datalog \
        --market Hyperliquid --hlnode \
        --node-endpoint "$ENDPOINT" \
        --date "$DATE" \
        --outpath "$outname"
    exitcode=$?

    if [ $exitcode -ne 0 ]; then
        echo "$(date) [$STREAM] exited with code: $exitcode; restarting..."
        # Tiny pause so a fast crash loop doesn't hammer us
        sleep 2
    else
        echo "$(date) [$STREAM] exiting normally"
        break
    fi
done

# Upload to s3
echo "Uploading $outname to s3"
aws s3 mv "$outname" "s3://l1-pktrade-capture/$outname" --quiet

# Mark this stream done (postproc waits for BOTH books.done and fills.done)
touch "raw/$MKT/$DATE.$capturenum.$STREAM.done"
aws s3 mv "raw/$MKT/$DATE.$capturenum.$STREAM.done" \
          "s3://l1-pktrade-capture/raw/$MKT/$DATE.$capturenum.$STREAM.done" --quiet
