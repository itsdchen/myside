#!/bin/bash

source .venvs/v1/bin/activate
pip install -r requirements.txt

# make dirs
stages=("raw" "split" "gzpbf")
markets=("BinanceFutures" "BinanceCOINFutures" "BinanceSPOT")

for stage in "${stages[@]}"; do
    mkdir -p logs/$stage
    for market in "${markets[@]}"; do
        mkdir -p $stage/$market
        mkdir -p logs/raw/$market
    done
done
