#!/bin/bash

# Sample command:
# ./cron/postprocess-captures.sh BinanceFutures 20240221 1

# Exit script on first failure
set -e

mkt=$1

# Default date to yesterday
if [ -n "$2" ]; then
    date=$2
else
    #date=$(date +"%Y%m%d" --date="yesterday")
    date=$(date +"%Y%m%d" --date="2 days ago")
fi


# Make directories
#mkdir -p split/$mkt/combined
mkdir -p gzpbf/$mkt

source ~/.venvs/v1/bin/activate

echo Processing market $mkt for $date

# Wait for raw capture upload to finish
while [ true ]; do
    sleep 5
    # Disable exiting on first failure since we expect this to fail until capture upload is done
    set +e
    raws_done=1
    aws s3 ls s3://l1-pktrade-capture/raw/$mkt/$date.1.done
    exitcode=$?
    raws_done=$(( raws_done && exitcode == 0 ))
    set -e
    if [ $raws_done ]; then
        break
    fi
done

echo "Downloading raw captures"

# Download raw captures
# Note thatt for a market like hyperliquid, there's no sepsarate snapshot file.
dl_capture_cmd="aws s3 cp s3://l1-pktrade-capture/raw/$mkt/$date.1.capture raw/$mkt/ --quiet"
dl_snap_cmd="aws s3 cp s3://l1-pktrade-capture/raw/$mkt/$date.snapshot raw/$mkt/ --quiet"

echo "Running > $dl_capture_cmd"
eval "$dl_capture_cmd"

# Also check if subsequent files exist too, and deal with those.
# This is for the dudes may have had restarts happen to them.
for counter in {1..10}; do
    # Try to aws cp the target.
    dl_capture_cmd_inner="aws s3 cp s3://l1-pktrade-capture/raw/$mkt/$date.1.capture.$counter raw/$mkt/ --quiet"
    echo "Running > $dl_capture_cmd_inner"
    if ! eval "$dl_capture_cmd_inner"; then
        echo "File does not exit, exiting"
        break
    fi
done

# Some markets don't have a snapshot
# I added binance spot because it's a pain to deal with.
if [ "$mkt" = "Hyperliquid" ] || [ "$mkt" = "BybitSPOT" ] || [ "$mkt" = "BybitDeriv" ] || [ "$mkt" = "BybitInverseDeriv" ] || [ "$mkt" = "BinanceSPOT" ]; then
    # We do not need to capture snapshots
    echo "Skipping snapshot dl"
else
    echo "Running > $dl_snap_cmd"
    eval "$dl_snap_cmd"
fi



# Split captures for each feed
combine_snap_cmd="./tools/CombinedStreamSplitter.py --date $date --market $mkt --outdir split/$mkt --capture raw/$mkt/$date.1.capture --snapshot raw/$mkt/$date.snapshot"

echo "Running > $combine_snap_cmd"
eval "$combine_snap_cmd"


# Delete raw captures now that we're done with them
echo 'Removing raw combined capture'
rm -f raw/$mkt/$date.*


# Declare symbol universe
declare -A mkt_to_syms

# Function to read symbols from file and convert to comma-separated string.
# Returns empty string when the file is missing so we don't spew errors for
# markets we aren't postprocessing on this host.
read_symbols() {
    local file=$1
    if [ ! -f "$file" ]; then
        return 0
    fi
    # Read file, remove any empty lines, and join with commas
    tr '\n' ',' < "$file" | sed 's/,$//'
}

# Read symbols from files
script_dir="$(dirname "$0")"
sym_dir="${script_dir}/sym_lists"

mkt_to_syms["BinanceFutures"]=$(read_symbols "${sym_dir}/binance_futures.txt")
mkt_to_syms["BinanceSPOT"]=$(read_symbols "${sym_dir}/binance_spot.txt")
mkt_to_syms["BinanceCOINFutures"]=$(read_symbols "${sym_dir}/binance_coinm.txt")
mkt_to_syms["Hyperliquid"]=$(read_symbols "${sym_dir}/hyperliquid.txt")
mkt_to_syms["BybitSPOT"]=$(read_symbols "${sym_dir}/bybit_spot.txt")
mkt_to_syms["BybitDeriv"]=$(read_symbols "${sym_dir}/bybit_futures.txt")
mkt_to_syms["BybitInverseDeriv"]="BTCUSD,ETHUSD"  # Keep this hardcoded since it's just two symbols

# Parse symbols to array
OLDIFS=$IFS
IFS=','
read -ra syms <<< "${mkt_to_syms[$mkt]}"
IFS=$OLDIFS

# Convert each symbol
translate_bin=./tools/jsontoproto
srcdir=split/
destdir=gzpbf
for sym in "${syms[@]}"; do
    zip_cmd="./tools/convert_pbf_selfcapture.py --symbol $sym --mkt $mkt --date $date --src $srcdir --dest $destdir --translate-bin $translate_bin "
    echo "Running > $zip_cmd"
    eval "$zip_cmd"
done

# Upload converted captures to s3 with mv
aws s3 mv gzpbf/$mkt s3://l1-pktrade-capture/gzpbf/$mkt/ --recursive --quiet

# Delete split captures now that we're done with them
rm split/$mkt/*_$date.jsonl
