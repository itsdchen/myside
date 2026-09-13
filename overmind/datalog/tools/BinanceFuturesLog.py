#! /usr/bin/env python


"""
  Sample command:
  ./BinanceFuturesLog.py --capture_path capture.out --snapshot_path snapshot.out --date 20231207


  V1 of a dataloger for binance perps data.
  Let's just check whether this works, and then let's start making
  new versions for other books.

  Not sure what to do just yet. But I think would be helpful to:

  1 - At midnight eastern, start up the stream.

  2 - At midnight eastern, end the stream.

  3 - Periodically place requests for snapshots.

  For ease of use, I'm going to write this all to one file and
  probably split things up later.
"""

import time
import argparse

from datetime import datetime
#import schedule
import time

import aiohttp
import asyncio
import aiofiles
import websockets
import time

import logging

logger = logging.getLogger('BinanceFuturesLog')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

BINANCE_ENDPOINT = "fstream.binance.com"

stream_names = [
    "depth@0ms",
    "bookTicker",
    "trade"
]

snapshot_tgt = "https://fapi.binance.com/fapi/v1/depth?symbol={SYM}&limit=1000"

API_KEY = "pO4ZxwNKRAuMerVbOM532BwRCkQacoYURc9aJIxtNFmR5N0JqIkfppIuv8iCjjr8"


# End time is tonight's midnight.
#>>> cur_date = datetime.strptime("20231207", "%Y%m%d")
#>>> datetime.now() < datetime.combine(cur_date, datetime.max.time())

async def connect_and_run(date_str, syms, capture_path, snapshot_path, snapshot_delay):
    cur_date = datetime.strptime(date_str, "%Y%m%d")
    end_dt = datetime.combine(cur_date, datetime.max.time())
    # This will localize to system timezone which should be US/Eastern
    end_dt = end_dt.astimezone() 
    end_ts = end_dt.timestamp()

    sym_stream_lst = []
    for one_sym in syms:
        for one_stream in stream_names:
            sym_stream = "{}@{}".format(one_sym, one_stream)
            sym_stream_lst.append(sym_stream)

    stream_url = "wss://{}/stream?streams={}".format(BINANCE_ENDPOINT, "/".join(sym_stream_lst))

    async def take_snapshots():
        # Wait for websocket to connect first
        await asyncio.sleep(snapshot_delay)
        async with aiofiles.open(snapshot_path, 'a') as snapshot_f:
            headers = {"Content-Type": "application/json;charset=utf-8", "X-MBX-APIKEY": API_KEY}
            async with aiohttp.ClientSession(headers=headers) as session:
                logger.info("Getting snapshots")
                for one_sym in syms:
                    upper_sym = one_sym.upper()
                    resp = await session.get(snapshot_tgt.format(SYM=one_sym))
                    text = await resp.text()
                    rx = int(time.time_ns()/1e3)
                    await snapshot_f.write("{},{},{}\n".format(rx, upper_sym, text))
                logger.info("Done getting snapshots")

    async def receive_captures():
        # Increasing this buffer improves tail latencies (the default is determined by system, usually 8192)
        buffering = 1024 * 1024
        # We observed crashes that happen when this queue is consistently full, so increase the size to
        # give more of a buffer against that. (The default is 32)
        max_queue = 256
        with open(capture_path, 'a', buffering=buffering) as capture_f:
            async with websockets.connect(stream_url, max_queue=max_queue, compression=None, ping_timeout=None) as websocket:
                logger.info("Websocket connected")
                count = 0
                while True:
                    message = await websocket.recv()
                    now = time.time()
                    if now > end_ts:
                        logger.info("Stopping bc too late")
                        return

                    rx = time.time_ns()
                    if count % 100_000 == 0:
                        logger.info(f"Got {count} messages")

                    count += 1
                    capture_f.write("{},{}\n".format(rx, message))


    await asyncio.gather(
        receive_captures(),
        take_snapshots()
    )

async def main():
    # Don't have anything to really parse.
    parser = argparse.ArgumentParser()
    # in YYYYMMDD format
    parser.add_argument("--date", type=str, required=True)
    parser.add_argument("--syms", type=str, required=True)
    parser.add_argument("--capture-path", type=str, required=True)
    parser.add_argument("--snapshot-path", type=str, required=True)
    parser.add_argument('--snapshot-delay', type=int, default=5)
    args = parser.parse_args()

    syms = [x.lower() for x in args.syms.split(",")]

    await connect_and_run(args.date, syms, args.capture_path, args.snapshot_path,
                          args.snapshot_delay)



if __name__ == "__main__":
    asyncio.run(main())
