#!/bin/bash


# Testing the read_file fxn.

declare -A mkt_to_syms

# Function to read symbols from file and convert to comma-separated string
read_symbols() {
    local file=$1
    # Read file, remove any empty lines, and join with commas
    tr '\n' ',' < "$file" | sed 's/,$//'
}

# Read symbols from files
sym_dir="sym_lists"
mkt_to_syms["BinanceFutures"]=$(read_symbols "${sym_dir}/binance_futures.txt")
mkt_to_syms["BinanceSPOT"]=$(read_symbols "${sym_dir}/binance_spot.txt")
mkt_to_syms["BinanceCOINFutures"]=$(read_symbols "${sym_dir}/binance_coinm.txt")
mkt_to_syms["Hyperliquid"]=$(read_symbols "${sym_dir}/hyperliquid.txt")
mkt_to_syms["BybitSPOT"]=$(read_symbols "${sym_dir}/bybit_spot.txt")
mkt_to_syms["BybitDeriv"]=$(read_symbols "${sym_dir}/bybit_futures.txt")
mkt_to_syms["BybitInverseDeriv"]="BTCUSD,ETHUSD"  # Keep this hardcoded since it's just two symbols

# Print values for testing - here are a few ways to test:

# Print all market symbols:
echo -e "\nAll market symbols:"
for market in "${!mkt_to_syms[@]}"; do
    echo "$market: ${mkt_to_syms[$market]}"
done

