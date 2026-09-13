#! /usr/bin/env python

import sys
import os

# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)

import email_utils

email_utils.send_ntfy_alert("Hihi")