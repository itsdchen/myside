Feed Handler
------------

This directory contains the application code for the Binance feed handler. *When
expanding to new exchanges, it may make sense to create subdirectories for
different venues.*

### Binance

Example Configuration:

```
{
    // HTTP address on which application will connect to recieve market data
    // from Binance
    "wss_endpoint": "stream.binance.com:9443",

    // Stream names correspond to those listed on the Binance documentation
    "stream_names": [
         "btcusdt@depth@100ms",
         "btcusdt@bookTicker",
         "btcusdt@trade"
    ],

    // ZMQ socket on which the application will bind to publish market data.
    // Various unicast transport options are supported - see
    // https://zguide.zeromq.org/docs/chapter2/#Unicast-Transports for details
    // and trade-offs.
    "pub_socket": "ipc:///tmp/feed-sock",

    // HTTP address on which Prometheus will serve metrics. Metrics are exposed
    // by default on the /metrics endpoint.
    "prometheus_endpoint": "localhost:8080"
}
```

Usage:

```
$ pkfeed config.json
```

The application respects the following environment variables:

 - `GLOG_*`: See https://github.com/google/glog#setting-flags for more details.
   Enabling verbose logging level=1 (e.g. `GLOG_v=1`) will cause the application
   to emit raw websocket JSON messages.

Notes:

 - The feed handler parses the JSON-encoded data from the Binance websocket
   feed, timestamps it, and serializes it to byte streams using Protocol
   Buffers. The schema is defined in `binance.proto`
 - If no consumers are connected to the feed, messages are immediately dropped
 - ZMQ PUB/SUB pattern allows the publisher and subscribers to be started in any
   order
