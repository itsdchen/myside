#!/bin/bash

mkt=$1

# Default date to tomorrow
if [ -n "$2" ]; then
    DATE=$2
else
    DATE=$(date +"%Y%m%d" --date="tomorrow")
fi


# Ugh. Be careful. If the input string is too long, I guess, this will get truncated.
# So maybe snapshot separately or something.
# Declare symbol universe
declare -A mkt_to_syms

# Function to read symbols from file and convert to comma-separated string
read_symbols() {
    local file=$1
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

# Load syms
syms="${mkt_to_syms[${mkt}]}"

# Infinite loop to keep retrying the capture
    # Run capture for the day
case $mkt in
    BinanceFutures|BinanceCOINFutures|BinanceSPOT)
        pp_cmd="./tools/simple_snapshotter --market $mkt --outpath raw/$mkt/$DATE.snapshot  --syms '$syms';"
        echo "Running > $pp_cmd"
        eval "$pp_cmd"
        ;;
    *)
        echo "Invalid market"
        exit 1
esac

# Exit this script on first failure for these
# Don't exit quickly here.
# set -e

# Upload to s3
# Do this in a separate job, after all the snapshots of the day are done.
#aws s3 mv raw/$mkt/$DATE.snapshot s3://pktrade-capture/raw/$mkt/$DATE.snapshot --quiet
