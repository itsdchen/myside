#pragma once

#include <string>

#include <rapidjson/document.h>

#include "mdapi.h"
#include "mdmsg.pb.h"

namespace pktrade {

// Decode Binance (common)
mdmsg::PbMessage decodeBinanceBookSnapshotCommon(const std::string& data);

// Decode BinanceSPOT
mdmsg::PbMessage decodeBinanceDepthUpdate(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceTrade(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceBookTicker(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceStreamData(const std::string& data, bool our_capture, bool store_ns);

// Decode BinanceFutures
mdmsg::PbMessage decodeBinanceFuturesDepthUpdate(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceFuturesTrade(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceFuturesBookTicker(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceFuturesStreamData(const std::string& data, bool our_capture,
                                                bool store_ns);

// Decode BinanceCOINFutures
mdmsg::PbMessage decodeBinanceCOINFuturesDepthUpdate(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceCOINFuturesTrade(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceCOINFuturesBookTicker(const rapidjson::Value& d);
mdmsg::PbMessage decodeBinanceCOINFuturesStreamData(const std::string& data, bool our_capture,
                                                    bool store_ns);

// Decode Bybit (common)
mdmsg::PbMessage decodeBybitOrderbook(const rapidjson::Value& d, bool our_capture, bool store_ns);
std::vector<mdmsg::PbMessage> decodeBybitPublicTrade(const rapidjson::Value& d, Market mkt,
                                                     bool our_capture, bool store_ns);
std::vector<mdmsg::PbMessage> decodeBybitStreamData(const std::string& data, Market mkt,
                                                    bool our_capture, bool store_ns);

// TODO: rm this when I implement the feed.
std::vector<mdmsg::PbMessage> decodeBybitDerivativesStreamData(const std::string& data,
                                                               bool our_capture, bool store_ns);
std::vector<mdmsg::PbMessage> decodeBybitSPOTStreamData(const std::string& data, bool our_capture,
                                                        bool store_ns);

// Decode Hyperliquid
mdmsg::PbMessage decodeHyperliquidL2BookSnapshot(const std::string& data);

std::vector<mdmsg::PbMessage> decodeHyperliquidTrades(const rapidjson::Value& d,
                                                      int64_t recorded_rx, bool our_capture,
                                                      bool store_ns);
mdmsg::PbMessage decodeHyperliquidL2Book(const rapidjson::Value& d, int64_t recorded_rx,
                                         bool our_capture, bool store_ns);
std::vector<mdmsg::PbMessage> decodeHyperliquidStreamData(const rapidjson::Document& doc,
                                                          int64_t rx_t, bool our_capture,
                                                          bool store_ns);

} // namespace pktrade
