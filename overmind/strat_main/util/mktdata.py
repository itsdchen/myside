
"""
Cosntants and stuff surrounding market data.
Putting this here to replace the channel mapping in dl_tardis_lib.

"""


books_to_channels = {
    "BinanceSPOT": ["depth", "depthSnapshot", "bookTicker", "trade"],
    "BinanceFutures":  ["depth", "depthSnapshot", "bookTicker", "trade"],
    "BinanceCOINFutures":  ["depth", "depthSnapshot", "bookTicker", "trade"],

    "Hyperliquid":  ["trades", "l2Book"],

    "TopBookCme": ["quotes"],
    "TopBookEquity": ["quotes"],
    "TopBookEquity_Boats": ["quotes"],

    # A quick look seems to say trades- is better than trades-all in terms of
    # information.
    # tbt data is tick-by-tick, and requires vip5 or above

    # OK, so the tbt stuff is pretty bad. Pretty big disadvantage.
    # Let's start w/ "books" first, and then deal with the tbts later.
    # "books-l2-tbt", "bbo-tbt",
    "OKXPerp": ["trades", "books"],
    "OKXSPOT": ["trades", "books"],

    "BybitSPOT":  ["publicTrade", "orderbook.1", "orderbook.50", "orderbook.500"],
    "BybitDeriv": ["publicTrade", "orderbook.1", "orderbook.50", "orderbook.500"],
    "BybitInverseDeriv": ["publicTrade", "orderbook.1", "orderbook.50", "orderbook.500"]
}


