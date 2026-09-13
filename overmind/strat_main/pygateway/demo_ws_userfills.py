#! /usr/bin/env python

"""

Little test script to demo subscribing to my hyperliquid fills and checking what I actually
get out of them.

"""


import websocket
from websocket import create_connection
import json
import time
import argparse

from datetime import date, datetime

# import schedule


BINANCE_ENDPOINT = "fstream.binance.com"


# End time is tonight's midnight.
# >>> cur_date = datetime.strptime("20231207", "%Y%m%d")
# >>> datetime.now() < datetime.combine(cur_date, datetime.max.time())


stream_url = "wss://api.hyperliquid-testnet.xyz/ws"


my_wallet = "0x2AD7672c2990107b8Fd8130D896D5Bdd2aA4a6b9"


"""
    OK, so I ended up getting messages that look like this:

# note that the isSnapshot
{"channel":"userFills","data":{"isSnapshot":true,"user":"0x2ad7672c2990107b8fd8130d896d5bdd2aa4a6b9","fills":[{"coin":"SUSHI","px":"1.2852","sz":"10.0","side":"B","time":1702057057201,"startPosition":"0.0","dir":"Open Long","closedPnl":"0.0","hash":"0xa18f88b2f800ffcb99af040873d6c1011300badb1155a16360e7a09bb1d078e7","oid":2745613867,"crossed":true,"fee":"0.003213","liquidationMarkPx":null,"tid":1081959953107430},{"coin":"SUSHI","px":"1.2856","sz":"10.0","side":"A","time":1702057138484,"startPosition":"10.0","dir":"Close Long","closedPnl":"0.004","hash":"0x232f42cee11ea686abd6040873d8700108009dfac33d5d638a6b5288d251646f","oid":2745625556,"crossed":false,"fee":"-0.000257","liquidationMarkPx":null,"tid":890906720001408},{"coin":"ETH","px":"2215.4","sz":"0.4152","side":"B","time":1702490155043,"startPosition":"0.0","dir":"Open Long","closedPnl":"0.0","hash":"0xc223b934707cbb5b2d14040896e48b01130033abbc342f540a1bf94bf19bf536","oid":2995237886,"crossed":true,"fee":"0.229958","liquidationMarkPx":null,"tid":286525721792647},{"coin":"ETH","px":"2221.2","sz":"0.005","side":"A","time":1702493418251,"startPosition":"0.4152","dir":"Close Long","closedPnl":"0.029","hash":"0x471c038698db88a99d91040897285a010a00f4920fc0dad7092f2b0f2225de44","oid":2996742733,"crossed":true,"fee":"0.002776","liquidationMarkPx":null,"tid":708651666530428},{"coin":"ETH","px":"2221.1","sz":"0.4102","side":"A","time":1702493418251,"startPosition":"0.4102","dir":"Close Long","closedPnl":"2.33814","hash":"0x471c038698db88a99d91040897285a010a00f4920fc0dad7092f2b0f2225de44","oid":2996742733,"crossed":true,"fee":"0.227773","liquidationMarkPx":null,"tid":1019798320199369}]}}
websocket Got message:
{"channel":"userFills","data":{"user":"0x2ad7672c2990107b8fd8130d896d5bdd2aa4a6b9","fills":[{"coin":"SUSHI","px":"1.1803","sz":"10.0","side":"B","time":1702786328663,"startPosition":"0.0","dir":"Open Long","closedPnl":"0.0","hash":"0xa8d094ea02608a93f8e10408aeb273011000f6dac1c342ca641c4c2ab10ee431","oid":3139191514,"crossed":true,"fee":"0.00295","liquidationMarkPx":null,"tid":163860968303999}]}}
websocket Got message:
{"channel":"userFills","data":{"user":"0x2ad7672c2990107b8fd8130d896d5bdd2aa4a6b9","fills":[{"coin":"SUSHI","px":"1.18","sz":"10.0","side":"A","time":1702786334099,"startPosition":"10.0","dir":"Close Long","closedPnl":"-0.003","hash":"0xf0b24a60430f02a94b450408aeb290011000ad8d9731cbbfa09f4a28cd686894","oid":3139193727,"crossed":true,"fee":"0.00295","liquidationMarkPx":null,"tid":925391004589683}]}}

"""


def on_message(ws, message):
    print("websocket Got message: ")

    print(message)
    msg_dct = json.loads(message)

    if msg_dct["channel"] != "userFills":
        continue

    if msg_dct["data"]["isSnapshot"]:
        continue

    # OK, now we're talking.
    for one_fill in msg_dct["data"]["fills"]:
        px = one_fill["px"]
        sz = one_fill["sz"]
        oid = one_fill["oid"]
    pass

# OK, so then if we check out the channel (userFills)
#


# I see, you have to send after it's open, got it.
def on_open(
    ws,
):
    print("Got it really")
    ws.send(
        json.dumps(
            {
                "method": "subscribe",
                "subscription": {"type": "userFills", "user": my_wallet},
            }
        )
    )


ws = websocket.WebSocketApp(stream_url, on_message=on_message, on_open=on_open)

print("Starting")


while True:
    ws.run_forever()
