#!/bin/bash
# Example:
# ./daily-capture.sh Hyperliquid 20240221
mkt=$1

# Default date to tomorrow
if [ -n "$2" ]; then
    DATE=$2
else
    DATE=$(date +"%Y%m%d" --date="tomorrow")
fi

capturenum=$3

# Declare symbol universe
declare -A mkt_to_syms

# Function to read symbols from file and convert to comma-separated string.
# Returns empty string when the file is missing so we don't spew errors for
# markets we aren't capturing on this host (each capture machine only needs
# the sym list for its active market).
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

source .venvs/v1/bin/activate

# Load syms
syms="${mkt_to_syms[$mkt]}"

# Infinite loop to keep retrying the capture
while true; do

    # In case of issues.
    counter=1
    outname="raw/$mkt/$DATE.$capturenum.capture"
    if [ -e "$outname" ]; then
        # Keep checking for the next available filename
        while [ -e "$outname.$counter" ]; do
            counter=$((counter + 1))
        done
        outname="$outname.$counter"
    fi

    # Run capture for the day
    case $mkt in
        BinanceFutures|BinanceCOINFutures|BinanceSPOT)
            ./tools/simple_datalog --date $DATE --market $mkt --outpath $outname --batch-size 1 --ping 60 --syms "$syms";
            ;;
        BybitSPOT|BybitDeriv|BybitInverseDeriv)
            ./tools/simple_datalog --date $DATE --market $mkt --outpath $outname --batch-size 1 --ping 60 --syms "$syms";
            ;;

        Hyperliquid)
            ./tools/simple_datalog --date $DATE --market $mkt --outpath $outname --batch-size 1 --ping 60 --syms "$syms";
            ;;
        *)
            echo "Invalid market"
            exit 1
    esac
    exitcode=$?


    if [ $exitcode -ne 0 ]; then
        echo  "exited with code:" $exitcode
       	echo  "restarting..."
    else
        # Capture exited normally so break
        echo "exiting normally"
        break
    fi
done

# Exit this script on first failure for these
# Don't exit quickly here.
# set -e

# Upload to s3
echo "Uploading initial capture to s3"
aws s3 mv raw/$mkt/$DATE.$capturenum.capture s3://l1-pktrade-capture/raw/$mkt/$DATE.$capturenum.capture --quiet

# Perhaps upload the other versions too.
counter=1
outname="raw/$mkt/$DATE.$capturenum.capture"
while [ -e "$outname.$counter" ]; do
    # upload the current file too.
    current_file="$outname.$counter"
    echo "Also uploading $current_file"
    aws s3 mv "$current_file" "s3://l1-pktrade-capture/$current_file" --quiet
    counter=$((counter + 1))
done


# Mark job as done
touch raw/$mkt/$DATE.$capturenum.done
aws s3 mv raw/$mkt/$DATE.$capturenum.done s3://l1-pktrade-capture/raw/$mkt/$DATE.$capturenum.done --quiet

# Delete after uploading
# rm -f raw/$mkt/$DATE*
