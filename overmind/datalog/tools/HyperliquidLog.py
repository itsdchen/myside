#! /usr/bin/env python

"""
  Sample command:
  ./HyperliquidLog.py --syms BTC,ETH --capture_path capture.out --snapshot_path snapshot.out --date 20231207
"""

import asyncio
import aiohttp
import aiofiles
import json
from typing import List
import time
import argparse
import websockets
from tenacity import retry, wait_fixed

from datetime import datetime
import time

from hyperliquid.utils.constants import (
    TESTNET_API_URL,
    MAINNET_API_URL
)

def printd(*args):
    print(datetime.now(), *args)

async def connect_and_run(date_str, syms, capture_path, snapshot_path, snapshot_delay, mainnet):
    cur_date = datetime.strptime(date_str, "%Y%m%d")
    end_dt = datetime.combine(cur_date, datetime.max.time())

    base_url = MAINNET_API_URL if mainnet else TESTNET_API_URL
    ws_url = "ws" + base_url[len("http") :] + "/ws"
    
    async def take_snapshots():
        # Wait for websocket to connect first
        await asyncio.sleep(snapshot_delay)
        async with aiofiles.open(snapshot_path, 'a') as snapshot_f:
            headers = { "Content-Type": "application/json;charset=utf-8" }
            async with aiohttp.ClientSession(headers=headers) as session:
                printd("Getting snapshots")
                for sym in syms:
                    resp = await session.post(f'{base_url}/info', json={ "type": "l2Book", "coin": sym })
                    text = await resp.text()
                    rx = int(time.time_ns()/1e3)
                    await snapshot_f.write("{},{},{}\n".format(rx, sym, text))
                printd("Done getting snapshots")


    @retry(wait=wait_fixed(1))
    async def receive_captures():
        async with aiofiles.open(capture_path, 'a') as capture_f:
            async with websockets.connect(ws_url) as websocket:

                printd("Websocket connected")

                # Subscribe to symbols
                for sym in syms:
                    await websocket.send(json.dumps({ "method" : "subscribe", "subscription": { "type": "trades", "coin": sym } }))
                    await websocket.send(json.dumps({ "method" : "subscribe", "subscription": { "type": "l2Book", "coin": sym } }))

                while True:
                    message = await websocket.recv()
                    now = datetime.now()
                    
                    if now > end_dt:
                        printd('Stopping bc too late')
                        return

                    rx = int(time.time_ns()/1e3)
                    await capture_f.write("{},{}\n".format(rx, message))

    await asyncio.gather(
        receive_captures(),
        take_snapshots()
    )
    printd("receive_captures retry statistics:", receive_captures.retry.statistics)

async def main():
    # Don't have anything to really parse.
    parser = argparse.ArgumentParser()
    # in YYYYMMDD format
    parser.add_argument("--date", type=str, required=True)
    parser.add_argument("--syms", type=str, required=True)
    parser.add_argument("--capture-path", type=str, required=True)
    parser.add_argument("--snapshot-path", type=str, required=True)
    parser.add_argument('--snapshot-delay', type=int, default=5)
    parser.add_argument('--mainnet', action='store_true')
    args = parser.parse_args()

    syms = args.syms.split(',')
    await connect_and_run(args.date, syms, args.capture_path, args.snapshot_path, 
                          args.snapshot_delay, args.mainnet)


if __name__ == "__main__":
    asyncio.run(main())
