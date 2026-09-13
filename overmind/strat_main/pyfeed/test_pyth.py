#! /usr/bin/env python

import asyncio
import math
import websockets
import json
import time 

#0x765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2
XAU_FEED_ID = "0x765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"

async def main():
    url = "wss://hermes.pyth.network/ws"
    async with websockets.connect(url) as ws:
        # Subscribe
        await ws.send(json.dumps({
            "type": "subscribe",
            "ids": [XAU_FEED_ID]
        }))

        # Read indefinitely
        while True:
            msg = await ws.recv()

            msg_json = json.loads(msg)
            if msg_json["type"] == "price_update":
                feed_json = msg_json["price_feed"]
                #print(feed_json)
                px_basic = float(feed_json["price"]["price"])
                px_adj = feed_json["price"]["expo"]
                px = px_basic * math.pow(10, px_adj)

                pub_t = feed_json["price"]["publish_time"]
                now_t = time.time()
                t_diff = now_t - pub_t
                print("{}: t_diff {} px {}".format(now_t, t_diff, px))


asyncio.run(main())
