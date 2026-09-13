#! /usr/bin/env python
"""

    Silly little thing to test sending/receiving on zmq.

    Just verifying that I can receive pb messages over zmq.

    This isn't used for real stuff but it is being used to demo
    how to use the zmq and pyproto stuff.

"""



import argparse
import getpass
import requests
import sys
import time
import json
import os
from requests.exceptions import HTTPError
from urllib.parse import urlencode
from eth_abi import encode
import eth_account
from eth_account.messages import encode_structured_data
from eth_utils import keccak, to_hex
import gateway_pb2

import zmq
import time



sub_sock = "ipc:///tmp/gateway-oe-pub-sock"

sub_ctx = zmq.Context()
sub_socket = sub_ctx.socket(zmq.SUB)

sub_socket.connect(sub_sock)

sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")


print("Starting sub")
while True:
    print("Waiting for a message")
    rec_str = sub_socket.recv()
    print("Received string")
    print(rec_str)
    # OK, I guess now I need to deserialize it and ID what it is.
    received_msg = gateway_pb2.PbMessage()
    received_msg.ParseFromString(rec_str)

    if received_msg.HasField("gateway_ack"):
        print("Received gateway ack")
        print(received_msg.gateway_ack)



