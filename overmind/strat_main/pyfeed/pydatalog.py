#! /usr/bin/env python

"""
Connects to pymultifeed's ZMQ PUB socket, filters by a hardcoded symbol list,
and writes each symbol's messages to a per-symbol gzip'd length-delimited
protobuf file.

Output format: {SYMBOL}_quotes_{DATE}.pb.gz
  Each record: [varint-encoded length][serialized PbMessage bytes]
  Compatible with protobuf's SerializeDelimitedToZeroCopyStream / ParseDelimitedFromZeroCopyStream.

Usage:
    /home/pktrade/.venvs/v1/bin/python pydatalog.py --date TODAY --outdir /path/to/output
    /home/pktrade/.venvs/v1/bin/python pydatalog.py --date TOMORROW
    /home/pktrade/.venvs/v1/bin/python pydatalog.py --date 20260225 --end-label PKDayEnd

"""

import argparse
import gzip
import os
import signal
import sys
import time
import traceback
from datetime import datetime

from google.protobuf.internal.encoder import _EncodeVarint

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))

import zmq
import mdmsg_pb2
import chron

# ---- Hardcoded symbol list ----
# Add/remove symbols you care about here.
SYMBOLS = {
    "SKHX",
    "SMSN",
    "HYUNDAI",
    "KOSPI200",
    "KS",
    "NIKKEI225",
    "MINIMAX",
    "ZHIPU",
    "SOFTBANK",  # TSE 9984.T via Refinitiv (JPY->USD); reference capture pre-listing
    "KIOXIA",  # TSE 285A.T via Refinitiv (JPY->USD); reference capture pre-listing
    "NIY",
}

ZMQ_ENDPOINT = "ipc:///tmp/feed-sock"

LOG_INTERVAL = 10_000  # print stats every N messages received
FLUSH_INTERVAL_SECS = 30


def make_output_path(outdir, symbol, date_str):
    safe_sym = symbol.replace("/", "_")
    return os.path.join(outdir, f"{safe_sym}_quotes_{date_str}.gzpbf")


def flush_all(sym_files):
    for f in sym_files.values():
        f.flush()


def close_all(sym_files, sym_counts):
    for sym, f in sym_files.items():
        try:
            f.close()
        except Exception as e:
            print(f"Error closing file for {sym}: {e}")
        print(f"Closed file for {sym} ({sym_counts.get(sym, 0)} messages)")


def run_loop(args, end_secs):
    """Main recv/write loop. Returns normally on shutdown signal or EOD, raises on error."""

    ctx = zmq.Context()
    sub_socket = ctx.socket(zmq.SUB)
    sub_socket.connect(args.endpoint)
    sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")
    print(f"Connected to {args.endpoint}, filtering for {sorted(SYMBOLS)}")
    print(f"Will shut down at end_secs={end_secs} ({datetime.utcfromtimestamp(end_secs)} UTC)")

    sym_files = {}
    total_recv = 0
    total_written = 0
    sym_counts = {}
    last_flush = time.monotonic()

    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        print(f"\nCaught signal {signum}, flushing and shutting down...")
        shutdown = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("Listening...")

    try:
        while not shutdown:
            # Check EOD.
            if time.time() >= end_secs:
                print("EOD time reached, shutting down.")
                break

            # Use a poller so we can check shutdown flag periodically.
            if sub_socket.poll(timeout=1000):  # 1s timeout
                raw = sub_socket.recv(zmq.NOBLOCK)
            else:
                # No message -- check if it's time to flush.
                now = time.monotonic()
                if sym_files and now - last_flush >= FLUSH_INTERVAL_SECS:
                    flush_all(sym_files)
                    last_flush = now
                continue

            total_recv += 1

            # Deserialize to check symbol_id and stamp rx_timestamp.
            msg = mdmsg_pb2.PbMessage()
            msg.ParseFromString(raw)
            sym = msg.symbol_id

            if sym not in SYMBOLS:
                if total_recv % LOG_INTERVAL == 0:
                    print(f"[stats] recv={total_recv}  written={total_written}  syms={sym_counts}")
                continue

            # Stamp rx_timestamp (ms) so HistoricalFeed can order messages.
            msg.rx_timestamp = int(time.time() * 1000)
            serialized = msg.SerializeToString()

            # Lazily open the gzip file for this symbol.
            if sym not in sym_files:
                path = make_output_path(args.outdir, sym, args.date)
                sym_files[sym] = gzip.open(path, "ab", compresslevel=5)
                sym_counts[sym] = 0
                print(f"Opened {path}")

            # Write varint-delimited (matches protobuf's SerializeDelimitedTo).
            _EncodeVarint(sym_files[sym].write, len(serialized))
            sym_files[sym].write(serialized)

            total_written += 1
            sym_counts[sym] = sym_counts.get(sym, 0) + 1

            # Periodic flush and stats.
            now = time.monotonic()
            if now - last_flush >= FLUSH_INTERVAL_SECS:
                flush_all(sym_files)
                last_flush = now

            if total_recv % LOG_INTERVAL == 0:
                print(f"[stats] recv={total_recv}  written={total_written}  syms={sym_counts}")

    finally:
        close_all(sym_files, sym_counts)
        sub_socket.close()
        ctx.term()
        print(f"Done. recv={total_recv}  written={total_written}")


def main():
    parser = argparse.ArgumentParser(description="Log filtered protobuf messages from pymultifeed")
    parser.add_argument("--outdir", type=str, default=os.getcwd(),
                        help="Directory for output .gzpbf files")
    parser.add_argument("--date", type=str, default="TODAY",
                        help="TODAY, TOMORROW, or YYYYMMDD date string")
    parser.add_argument("--end-label", type=str, default="PKDayEnd",
                        help="End-of-day label from chron (default: PKDayEnd = 18:00 ET)")
    parser.add_argument("--endpoint", type=str, default=ZMQ_ENDPOINT,
                        help="ZMQ PUB endpoint to connect to")
    args = parser.parse_args()

    # Resolve date: TODAY/TOMORROW labels or raw YYYYMMDD string.
    if "2" in args.date:
        date_str = args.date
    else:
        date_str = chron.get_date_easy(args.date)
    args.date = date_str

    # End time: 18:00 ET (or whatever end-label maps to) on that date.
    end_str = chron.get_end_time(args.end_label, date_str)
    end_secs = chron.timestr_to_secs(end_str)

    print(f"Date: {date_str}, end: {end_str} ({end_secs})")

    os.makedirs(args.outdir, exist_ok=True)

    while True:
        if time.time() >= end_secs:
            print("Past EOD, not starting.")
            break
        try:
            run_loop(args, end_secs)
            break  # clean shutdown via signal or EOD
        except Exception:
            traceback.print_exc()
            print("Error in run_loop, retrying in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    main()
