#! /usr/bin/env bash

# Builds the python protobuf files and puts them in the pygateway dir.
# Make sure protoc protobuf compiler is installed.

# Add other protobuf files as needed, I guess.
protoc -I=./src/pktrade --python_out=.//overmind/strat_main/pygateway ./src/pktrade/gateway.proto
protoc -I=./src/pktrade --python_out=.//overmind/strat_main/pyfeed ./src/pktrade/mdmsg.proto

# One day: maybe we'll need to have mdmsg in there too. Until then, nope!!
