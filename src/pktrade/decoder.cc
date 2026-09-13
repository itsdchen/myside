#include "pktrade/decoder.h"

#include "pktrade/util/basiclib.h"

#include <algorithm>

namespace pktrade {

// *******************************************************************************
// Binance (common)
mdmsg::PbMessage decodeBinanceBookSnapshotCommon(const std::string& data) {
  rapidjson::Document doc;

  try {
    doc.Parse(data.c_str());
  } catch (std::exception e) {
    std::cout << " decodeBinanceBookSnapshotCommon receceived ill-parsed message: " << data
              << std::endl;
    std::cout << "Exception: " << e.what() << std::endl;
    return pktrade::mdmsg::PbMessage();
  }

  // Sometimes we will get a completely empty dict. So we should just check if
  // we have that so we can ignore it.
  if (!doc.HasMember("lastUpdateId")) {
    std::cout << " Received empty snapshot, returning" << std::endl;
    return pktrade::mdmsg::PbMessage();
  }

  pktrade::mdmsg::PbMessage msg;
  if (doc.HasParseError()) {
    std::cout << " decodeBinanceBookSnapshotCommon receceived ill-parsed message: " << data
              << std::endl;
    return msg;
  }

  auto* snapshot = msg.mutable_book_snapshot();
  snapshot->set_last_update_id(doc["lastUpdateId"].GetInt64());

  for (auto& e : doc["bids"].GetArray()) {
    auto* lvl = snapshot->add_bids();
    lvl->set_px(e[0].GetString());
    lvl->set_qty(e[1].GetString());
  }

  for (auto& e : doc["asks"].GetArray()) {
    auto* lvl = snapshot->add_asks();
    lvl->set_px(e[0].GetString());
    lvl->set_qty(e[1].GetString());
  }

  return msg;
}

// *******************************************************************************
// BinanceSPOT

mdmsg::PbMessage decodeBinanceDepthUpdate(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);
  auto update_type = d["e"].GetString();
  if (strcmp(update_type, "depthUpdate") == 0) {
    auto* book_update = msg.mutable_book_update();

    book_update->set_event_time(d["E"].GetInt64());
    book_update->set_first_update_id(d["U"].GetInt64());
    book_update->set_last_update_id(d["u"].GetInt64());
    book_update->set_depth(1000);
    for (auto& e : d["b"].GetArray()) {
      auto* level_update = book_update->add_bids();
      level_update->set_px(e[0].GetString());
      level_update->set_qty(e[1].GetString());
    }

    for (auto& e : d["a"].GetArray()) {
      auto* level_update = book_update->add_asks();
      level_update->set_px(e[0].GetString());
      level_update->set_qty(e[1].GetString());
    }
  }

  return msg;
}

mdmsg::PbMessage decodeBinanceTrade(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto* book_trade = msg.mutable_book_trade();

  book_trade->set_px(d["p"].GetString());
  book_trade->set_qty(d["q"].GetString());
  book_trade->set_trade_id(d["t"].GetInt64());
  // It loks like they started removing these from spot?
  // Example:
  // {"e":"trade","E":1718713969545,"s":"SOLFDUSD","t":94442617,"p":"136.20000000","q":"0.06200000","T":1718713969545,"m":false,"M":true}
  if (d.HasMember("b")) {
    book_trade->set_buy_ord_id(d["b"].GetInt64());
    book_trade->set_sell_ord_id(d["a"].GetInt64());
  }
  book_trade->set_trade_time(d["T"].GetInt64());
  book_trade->set_passive_was_buyer(d["m"].GetBool());

  return msg;
}

mdmsg::PbMessage decodeBinanceBookTicker(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto ticker_update = msg.mutable_ticker_update();
  ticker_update->set_update_id(d["u"].GetInt64());
  ticker_update->set_best_bid_px(d["b"].GetString());
  ticker_update->set_best_bid_qty(d["B"].GetString());
  ticker_update->set_best_ask_px(d["a"].GetString());
  ticker_update->set_best_ask_qty(d["A"].GetString());

  return msg;
}

mdmsg::PbMessage decodeBinanceStreamData(const std::string& data, bool our_capture, bool store_ns) {
  rapidjson::Document doc;
  doc.Parse(data.c_str());
  auto& d = doc["data"];

  mdmsg::PbMessage msg;

  if (doc.HasParseError()) {
    std::cout << " DecodeBinanceStreamData receceived ill-parsed message: " << data << std::endl;
    return msg;
  }

  std::string stream = doc["stream"].GetString();
  auto pos = stream.find("@");
  auto stream_type = stream.substr(pos + 1);
  auto symbol = stream.substr(0, pos);

  // Symbols cointained in the stream name from Binance /stream endpoint are
  // lower-case, but upper-case in other situations. Make upper-case so that
  // string identifies compare equal.
  std::transform(symbol.begin(), symbol.end(), symbol.begin(),
                 [](auto c) { return std::toupper(c); });

  if (stream_type == "depthSnapshot") {
    auto* snapshot = msg.mutable_book_snapshot();
    snapshot->set_last_update_id(d["lastUpdateId"].GetInt64());

    for (auto& e : d["bids"].GetArray()) {
      auto* lvl = snapshot->add_bids();
      lvl->set_px(e[0].GetString());
      lvl->set_qty(e[1].GetString());
    }

    for (auto& e : d["asks"].GetArray()) {
      auto* lvl = snapshot->add_asks();
      lvl->set_px(e[0].GetString());
      lvl->set_qty(e[1].GetString());
    }

    // Tardis does not store the symbol ID in the nested "data" document, so we
    // grab it from the "stream" field.
    msg.set_symbol_id(symbol);
  } else if (stream_type == "depth@100ms") {
    msg = decodeBinanceDepthUpdate(d);
  } else if (stream_type == "bookTicker") {
    msg = decodeBinanceBookTicker(d);
  } else if (stream_type == "trade") {
    msg = decodeBinanceTrade(d);
  } else {
    // Unhandled stream. It's going to cause an error.
  }

  if (doc.HasMember("rx")) {
    int64_t recorded_rx = doc["rx"].GetInt64();
    int64_t final_rx = recorded_rx;
    if (our_capture && (!store_ns)) {
      // Written down in ns. Convert that to ms.
      final_rx = int64_t(recorded_rx / 1000000);
    }

    msg.set_rx_timestamp(final_rx);
  }
  // I guess in live, we're not actually writing the rx time down.

  msg.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
  return msg;
}

// *******************************************************************************
// BinanceFutures

mdmsg::PbMessage decodeBinanceFuturesDepthUpdate(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto update_type = d["e"].GetString();
  if (strcmp(update_type, "depthUpdate") == 0) {
    auto* book_update = msg.mutable_book_update();

    book_update->set_event_time(d["E"].GetInt64());
    book_update->set_first_update_id(d["U"].GetInt64());
    book_update->set_last_update_id(d["u"].GetInt64());

    for (auto& e : d["b"].GetArray()) {
      auto* level_update = book_update->add_bids();
      level_update->set_px(e[0].GetString());
      level_update->set_qty(e[1].GetString());
    }

    for (auto& e : d["a"].GetArray()) {
      auto* level_update = book_update->add_asks();
      level_update->set_px(e[0].GetString());
      level_update->set_qty(e[1].GetString());
    }
  }

  return msg;
}

// E: event time?
// T: next funding time
// t: trade id?
mdmsg::PbMessage decodeBinanceFuturesTrade(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto* book_trade = msg.mutable_book_trade();

  book_trade->set_px(d["p"].GetString());
  book_trade->set_qty(d["q"].GetString());
  book_trade->set_trade_id(d["t"].GetInt64());
  book_trade->set_trade_time(d["T"].GetInt64());
  book_trade->set_passive_was_buyer(d["m"].GetBool());

  // Maybe don't do insurance fund?

  return msg;
}

mdmsg::PbMessage decodeBinanceFuturesBookTicker(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto ticker_update = msg.mutable_ticker_update();
  ticker_update->set_update_id(d["u"].GetInt64());
  ticker_update->set_best_bid_px(d["b"].GetString());
  ticker_update->set_best_bid_qty(d["B"].GetString());
  ticker_update->set_best_ask_px(d["a"].GetString());
  ticker_update->set_best_ask_qty(d["A"].GetString());
  ticker_update->set_transact_time(d["T"].GetInt64());

  return msg;
}

mdmsg::PbMessage decodeBinanceFuturesStreamData(const std::string& data, bool our_capture,
                                                bool store_ns) {
  rapidjson::Document doc;
  doc.Parse(data.c_str());
  auto& d = doc["data"];

  mdmsg::PbMessage msg;

  if (doc.HasParseError()) {
    std::cout << " Hyperliquid receceived ill-parsed message: " << data << std::endl;
    return msg;
  }

  std::string stream = doc["stream"].GetString();
  auto pos = stream.find("@");
  auto stream_type = stream.substr(pos + 1);
  auto symbol = stream.substr(0, pos);

  // Symbols cointained in the stream name from Binance /stream endpoint are
  // lower-case, but upper-case in other situations. Make upper-case so that
  // string identifies compare equal.
  std::transform(symbol.begin(), symbol.end(), symbol.begin(),
                 [](auto c) { return std::toupper(c); });

  if (stream_type == "depthSnapshot") {
    if (!d.HasMember("lastUpdateId") || !d.HasMember("bids") || !d.HasMember("asks")) {
      // Probably improperly formatted msg.
      return msg;
    }
    auto* snapshot = msg.mutable_book_snapshot();
    snapshot->set_last_update_id(d["lastUpdateId"].GetInt64());

    // We got an empty snapshot once.
    for (auto& e : d["bids"].GetArray()) {
      auto* lvl = snapshot->add_bids();
      lvl->set_px(e[0].GetString());
      lvl->set_qty(e[1].GetString());
    }

    for (auto& e : d["asks"].GetArray()) {
      auto* lvl = snapshot->add_asks();
      lvl->set_px(e[0].GetString());
      lvl->set_qty(e[1].GetString());
    }

    // Tardis does not store the symbol ID in the nested "data" document, so we
    // grab it from the "stream" field.
    msg.set_symbol_id(symbol);
  } else if (stream_type == "depth@0ms") {
    msg = decodeBinanceFuturesDepthUpdate(d);
  } else if (stream_type == "bookTicker") {
    msg = decodeBinanceFuturesBookTicker(d);
  } else if (stream_type == "trade") {
    msg = decodeBinanceFuturesTrade(d);
  } else {
    // Unhandled stream. It's going to cause an error.
  }

  if (doc.HasMember("rx")) {
    int64_t recorded_rx = doc["rx"].GetInt64();
    int64_t final_rx = recorded_rx;
    if (our_capture && (!store_ns)) {
      // Written down in ns. Convert that to ms.
      final_rx = int64_t(recorded_rx / 1000000);
    }

    msg.set_rx_timestamp(final_rx);
  }

  msg.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_FUTURES);
  return msg;
}

// *******************************************************************************
// BinanceCOINFutures

mdmsg::PbMessage decodeBinanceCOINFuturesDepthUpdate(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto update_type = d["e"].GetString();
  if (strcmp(update_type, "depthUpdate") == 0) {
    auto* book_update = msg.mutable_book_update();

    book_update->set_event_time(d["E"].GetInt64());
    book_update->set_first_update_id(d["U"].GetInt64());
    book_update->set_last_update_id(d["u"].GetInt64());

    for (auto& e : d["b"].GetArray()) {
      auto* level_update = book_update->add_bids();
      level_update->set_px(e[0].GetString());
      level_update->set_qty(e[1].GetString());
    }

    for (auto& e : d["a"].GetArray()) {
      auto* level_update = book_update->add_asks();
      level_update->set_px(e[0].GetString());
      level_update->set_qty(e[1].GetString());
    }
  }

  return msg;
}

// E: event time?
// T: next funding time
// t: trade id?
mdmsg::PbMessage decodeBinanceCOINFuturesTrade(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto* book_trade = msg.mutable_book_trade();

  book_trade->set_px(d["p"].GetString());
  book_trade->set_qty(d["q"].GetString());
  book_trade->set_trade_id(d["t"].GetInt64());
  book_trade->set_trade_time(d["T"].GetInt64());
  book_trade->set_passive_was_buyer(d["m"].GetBool());

  // Maybe don't do insurance fund?

  return msg;
}

mdmsg::PbMessage decodeBinanceCOINFuturesBookTicker(const rapidjson::Value& d) {
  mdmsg::PbMessage msg;

  auto symbol_id = d["s"].GetString();
  msg.set_symbol_id(symbol_id);

  auto ticker_update = msg.mutable_ticker_update();
  ticker_update->set_update_id(d["u"].GetInt64());
  ticker_update->set_best_bid_px(d["b"].GetString());
  ticker_update->set_best_bid_qty(d["B"].GetString());
  ticker_update->set_best_ask_px(d["a"].GetString());
  ticker_update->set_best_ask_qty(d["A"].GetString());
  ticker_update->set_transact_time(d["T"].GetInt64());

  return msg;
}

mdmsg::PbMessage decodeBinanceCOINFuturesStreamData(const std::string& data, bool our_capture,
                                                    bool store_ns) {
  rapidjson::Document doc;
  doc.Parse(data.c_str());
  auto& d = doc["data"];

  mdmsg::PbMessage msg;

  if (doc.HasParseError()) {
    std::cout << " Hyperliquid receceived ill-parsed message: " << data << std::endl;
    return msg;
  }

  std::string stream = doc["stream"].GetString();
  auto pos = stream.find("@");
  auto stream_type = stream.substr(pos + 1);
  auto symbol = stream.substr(0, pos);

  // Symbols cointained in the stream name from Binance /stream endpoint are
  // lower-case, but upper-case in other situations. Make upper-case so that
  // string identifies compare equal.
  std::transform(symbol.begin(), symbol.end(), symbol.begin(),
                 [](auto c) { return std::toupper(c); });

  if (stream_type == "depthSnapshot") {
    auto* snapshot = msg.mutable_book_snapshot();
    snapshot->set_last_update_id(d["lastUpdateId"].GetInt64());

    for (auto& e : d["bids"].GetArray()) {
      auto* lvl = snapshot->add_bids();
      lvl->set_px(e[0].GetString());
      lvl->set_qty(e[1].GetString());
    }

    for (auto& e : d["asks"].GetArray()) {
      auto* lvl = snapshot->add_asks();
      lvl->set_px(e[0].GetString());
      lvl->set_qty(e[1].GetString());
    }

    // Tardis does not store the symbol ID in the nested "data" document, so we
    // grab it from the "stream" field.
    msg.set_symbol_id(symbol);
  } else if (stream_type == "depth@0ms") {
    msg = decodeBinanceCOINFuturesDepthUpdate(d);
  } else if (stream_type == "bookTicker") {
    msg = decodeBinanceCOINFuturesBookTicker(d);
  } else if (stream_type == "trade") {
    msg = decodeBinanceCOINFuturesTrade(d);
  } else {
    // Unhandled stream. It's going to cause an error.
  }

  if (doc.HasMember("rx")) {
    int64_t recorded_rx = doc["rx"].GetInt64();
    int64_t final_rx = recorded_rx;
    if (our_capture && (!store_ns)) {
      // Written down in ns. Convert that to ms.
      final_rx = int64_t(recorded_rx / 1000000);
    }

    msg.set_rx_timestamp(final_rx);
  }

  msg.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_COINFUTURES);
  return msg;
}

// *******************************************************************************
// Hyperliquid

mdmsg::PbMessage decodeHyperliquidL2BookSnapshot(const std::string& data) {
  rapidjson::Document doc;
  doc.Parse(data.c_str());

  mdmsg::PbMessage msg;

  // Check if it parsed correctly, die if not.
  if (doc.HasParseError()) {
    std::cout << " Hyperliquid receceived ill-parsed message: " << data << std::endl;
    return {};
  }
  // 3. Check if it's an object
  if (!doc.IsObject()) {
    std::cout << "Hyperliquid receceived  invalid non object" << data << std::endl;
    return {};
  }

  auto* snapshot = msg.mutable_book_snapshot();

  if (!doc.HasMember("levels")) {
    // srsly, wtf.
    std::cout << " Hyperliquid receceived ill-parsed message(no_levels): " << data << std::endl;
    return {};
  }

  // Just noting that they do sort it from the inside of the book to the
  // outside. And the bids do arrive first. Example is here:
  // https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

  // Bid levels
  // The structure is:
  // Outside is an array, 2 elements
  // First element is bids. Second is asks
  // The bids is an array of dicts sorted with the most aggressive
  // level first.
  for (auto& bid_lvl : doc["levels"].GetArray()[0].GetArray()) {
    auto* lvl = snapshot->add_bids();
    lvl->set_px(bid_lvl["px"].GetString());
    lvl->set_qty(bid_lvl["sz"].GetString());
    lvl->set_numords(bid_lvl["n"].GetInt());
  }

  // Ask levels
  for (auto& ask_lvl : doc["levels"].GetArray()[1].GetArray()) {
    auto* lvl = snapshot->add_asks();
    lvl->set_px(ask_lvl["px"].GetString());
    lvl->set_qty(ask_lvl["sz"].GetString());
    lvl->set_numords(ask_lvl["n"].GetInt());
  }

  msg.set_market(mdmsg::PbMarket::PBMARKET_HYPERLIQUID);

  return msg;
}

std::vector<mdmsg::PbMessage> decodeHyperliquidTrades(const rapidjson::Value& d,
                                                      int64_t recorded_rx, bool our_capture,
                                                      bool store_ns) {
  std::vector<mdmsg::PbMessage> msgs;

  for (auto& e : d.GetArray()) {
    mdmsg::PbMessage msg;

    msg.set_symbol_id(e["coin"].GetString());
    msg.set_market(pktrade::mdmsg::PbMarket::PBMARKET_HYPERLIQUID);

    auto* book_trade = msg.mutable_book_trade();

    book_trade->set_px(e["px"].GetString());
    book_trade->set_qty(e["sz"].GetString());
    if (e.HasMember("tid") && e["tid"].IsInt64()) {
      book_trade->set_trade_id(e["tid"].GetInt64());
    }
    book_trade->set_trade_time(e["time"].GetInt64());
    book_trade->set_passive_was_buyer(e["side"] != "B");

    int64_t final_rx = recorded_rx;
    if (our_capture && (!store_ns)) {
      // Written down in ns. Convert that to ms.
      final_rx = int64_t(recorded_rx / 1000000);
    }
    msg.set_rx_timestamp(final_rx);

    msgs.push_back(msg);
  }
  return msgs;
}

// This is the same as the snapshot handling, actually.
// So we are using this to return a snapshot instead.
mdmsg::PbMessage decodeHyperliquidL2Book(const rapidjson::Value& d, int64_t recorded_rx,
                                         bool our_capture, bool store_ns) {
  mdmsg::PbMessage msg;
  msg.set_symbol_id(d["coin"].GetString());
  msg.set_market(pktrade::mdmsg::PbMarket::PBMARKET_HYPERLIQUID);

  auto* book_update = msg.mutable_book_snapshot();

  book_update->set_event_time(d["time"].GetInt64());

  // Bid levels
  for (auto& bid_lvl : d["levels"].GetArray()[0].GetArray()) {
    auto* lvl = book_update->add_bids();
    lvl->set_px(bid_lvl["px"].GetString());
    lvl->set_qty(bid_lvl["sz"].GetString());
    lvl->set_numords(bid_lvl["n"].GetInt());
  }

  // Ask levels
  for (auto& ask_lvl : d["levels"].GetArray()[1].GetArray()) {
    auto* lvl = book_update->add_asks();
    lvl->set_px(ask_lvl["px"].GetString());
    lvl->set_qty(ask_lvl["sz"].GetString());
    lvl->set_numords(ask_lvl["n"].GetInt());
  }

  int64_t final_rx = recorded_rx;
  if (our_capture && (!store_ns)) {
    // Written down in ns. Convert that to ms.
    final_rx = int64_t(recorded_rx / 1000000);
  }

  msg.set_rx_timestamp(final_rx);

  return msg;
}

std::vector<mdmsg::PbMessage> decodeHyperliquidStreamData(const rapidjson::Document& doc,
                                                          int64_t rx_t, bool our_capture,
                                                          bool store_ns) {
  std::vector<mdmsg::PbMessage> msgs;

  if (!doc.IsObject()) {
    return msgs;
  }

  auto channel_it = doc.FindMember("channel");
  if (channel_it == doc.MemberEnd() || !channel_it->value.IsString()) {
    return msgs;
  }

  auto data_it = doc.FindMember("data");
  if (data_it == doc.MemberEnd()) {
    return msgs;
  }

  const std::string channel = channel_it->value.GetString();
  const auto& data = data_it->value;

  if (channel == "l2Book") {
    msgs.push_back(decodeHyperliquidL2Book(data, rx_t, our_capture, store_ns));
  } else if (channel == "trades") {
    msgs = decodeHyperliquidTrades(data, rx_t, our_capture, store_ns);
  } else {
    // Unhandled stream. It's going to cause an error.
  }
  return msgs;
}

// *******************************************************************************
// Bybit

std::vector<mdmsg::PbMessage> decodeBybitPublicTrade(const rapidjson::Value& doc, Market mkt,
                                                     bool our_capture, bool store_ns) {
  std::vector<mdmsg::PbMessage> msgs;
  // Data contains list of trades in ascending order of match time
  for (auto& trade : doc["data"].GetArray()) {
    mdmsg::PbMessage msg;

    if (doc.HasMember("rx")) {
      int64_t recorded_rx = doc["rx"].GetInt64();
      int64_t final_rx = recorded_rx;
      if (our_capture && (!store_ns)) {
        // Written down in ns. Convert that to ms.
        final_rx = int64_t(recorded_rx / 1000000);
      }

      msg.set_rx_timestamp(final_rx);
    }

    msg.set_symbol_id(trade["s"].GetString());

    if (mkt == Market::BybitSPOT) {
      msg.set_market(mdmsg::PbMarket::PBMARKET_BYBITSPOT);
    } else if (mkt == Market::BybitDeriv) {
      msg.set_market(mdmsg::PbMarket::PBMARKET_BYBITDERIV);
    } else if (mkt == Market::BybitInverseDeriv) {
      msg.set_market(mdmsg::PbMarket::PBMARKET_BYBITINVERSEDERIV);
    }

    auto* book_trade = msg.mutable_book_trade();

    book_trade->set_px(trade["p"].GetString());
    book_trade->set_qty(trade["v"].GetString());
    // Note that we use T instead of ts. I *think* T is more indicative
    // of the matching engine time.
    book_trade->set_trade_time(trade["T"].GetInt64());
    book_trade->set_passive_was_buyer(std::strcmp(trade["S"].GetString(), "Sell") == 0);
    // Bybit has uuid4 trade ids, so not setting these for now
    msgs.push_back(msg);
  }
  return msgs;
}

mdmsg::PbMessage decodeBybitOrderbook(const rapidjson::Value& doc, Market mkt, int depth,
                                      bool our_capture, bool store_ns) {
  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  doc.Accept(writer);

  // Output {"project":"rapidjson","stars":11}

  mdmsg::PbMessage msg;
  auto type = doc["type"].GetString();
  auto& data = doc["data"];
  // Set common fields
  if (mkt == Market::BybitSPOT) {
    msg.set_market(mdmsg::PbMarket::PBMARKET_BYBITSPOT);
  } else if (mkt == Market::BybitDeriv) {
    msg.set_market(mdmsg::PbMarket::PBMARKET_BYBITDERIV);
  } else if (mkt == Market::BybitInverseDeriv) {
    msg.set_market(mdmsg::PbMarket::PBMARKET_BYBITINVERSEDERIV);
  }

  if (doc.HasMember("rx")) {
    int64_t recorded_rx = doc["rx"].GetInt64();
    int64_t final_rx = recorded_rx;
    if (our_capture && (!store_ns)) {
      // Written down in ns. Convert that to ms.
      final_rx = int64_t(recorded_rx / 1000000);
    }

    msg.set_rx_timestamp(final_rx);
  }

  msg.set_symbol_id(data["s"].GetString());

  // The structure of the message is the same for both snapshot and delta types,
  // we have to do a "if else" here because of our pb types
  if (strcmp(type, "snapshot") == 0) {
    // snapshot

    // The structure for the levels (keys "a" and "b") is a list of lists. Each
    // inner list has 2 elements, where first elem is price, second elem is qty.
    // The levels go from most aggressive to least.
    auto* snapshot = msg.mutable_book_snapshot();

    // Bid levels
    for (auto& bid_lvl : data["b"].GetArray()) {
      auto* lvl = snapshot->add_bids();
      lvl->set_px(bid_lvl.GetArray()[0].GetString());
      lvl->set_qty(bid_lvl.GetArray()[1].GetString());
    }

    // Ask levels
    for (auto& ask_lvl : data["a"].GetArray()) {
      auto* lvl = snapshot->add_asks();
      lvl->set_px(ask_lvl.GetArray()[0].GetString());
      lvl->set_qty(ask_lvl.GetArray()[1].GetString());
    }

    // "seq" is used to compare across the different level feeds
    snapshot->set_last_update_id(data["seq"].GetInt64());

    // "cts" is the timestamp from the match engine and can be correlated with
    // the "T" field on public trade channel, so let's use that for event time
    // instead of "ts"
    snapshot->set_event_time(doc["cts"].GetInt64());
  } else {
    // delta

    // The structure for the levels (keys "a" and "b") is a list of lists. Each
    // inner list has 2 elements, where first elem is price, second elem is qty.
    // The levels go from most aggressive to least.
    auto* book_update = msg.mutable_book_update();

    // Bid levels
    for (auto& bid_lvl : data["b"].GetArray()) {
      auto* lvl = book_update->add_bids();
      lvl->set_px(bid_lvl.GetArray()[0].GetString());
      lvl->set_qty(bid_lvl.GetArray()[1].GetString());
    }

    // Ask levels
    for (auto& ask_lvl : data["a"].GetArray()) {
      auto* lvl = book_update->add_asks();
      lvl->set_px(ask_lvl.GetArray()[0].GetString());
      lvl->set_qty(ask_lvl.GetArray()[1].GetString());
    }

    // "seq" is used to compare across different levels of order book
    book_update->set_last_update_id(data["seq"].GetInt64());

    // "cts" is the timestamp from the match engine and can be correlated with
    // the "T" field on public trade channel, so let's use that for event time
    // instead of "ts"
    book_update->set_event_time(doc["cts"].GetInt64());
    book_update->set_depth(depth);
  }
  return msg;
}
std::vector<mdmsg::PbMessage> decodeBybitStreamData(const std::string& data, Market mkt,
                                                    bool our_capture, bool store_ns) {
  rapidjson::Document doc;
  doc.Parse(data.c_str());

  if (doc.HasParseError()) {
    std::cout << " Bybit receceived ill-parsed message: " << data << std::endl;
    return {};
  }
  // std::cout << " Bybit receceived some message: " << data << std::endl;
  if (!doc.HasMember("topic")) {
    // Probably the subscribe response:
    //  {"success":true,"ret_msg":"","conn_id":"6ebba28e-15d0-4681-a8b6-8ae0a07e858b","req_id":"","op":"subscribe"}
    // std::cout << " Bybit receiving unhandled message: " << data << std::endl;
    return {};
  }

  // Format is {stream_type}.{sym}
  // Eg. publicTrade.BTCUSDT and orderbook.500.BTCUSDT
  std::string topic = doc["topic"].GetString();
  auto pos = topic.find(".");
  auto stream_type = topic.substr(0, pos);

  if (stream_type == "orderbook") {
    auto topic_rest = topic.substr(pos + 1);
    auto pos = topic_rest.find(".");
    // The depth should be encoded, like, 1, 50, 200.
    int depth = std::stoi(topic_rest.substr(0, pos));
    auto msg = decodeBybitOrderbook(doc, mkt, depth, our_capture, store_ns);

    return {msg};
  } else if (stream_type == "publicTrade") {
    return decodeBybitPublicTrade(doc, mkt, our_capture, store_ns);
  } else {
    // Unhandled stream. It's going to cause an error.
  }
  return {};
}

std::vector<mdmsg::PbMessage> decodeBybitDerivativesStreamData(const std::string& data) {
  return {};
}
std::vector<mdmsg::PbMessage> decodeBybitSPOTStreamData(const std::string& data) { return {}; }

} // namespace pktrade
