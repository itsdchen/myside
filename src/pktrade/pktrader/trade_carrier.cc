#include "trade_carrier.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/sim/sim_verse.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include "pktrade/util/urls.h"

#include <chrono>
#include <cpr/cpr.h>
#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>
#include <regex>

namespace pktrade {

TradeCarrier::TradeCarrier(rapidjson::Document& trade_doc, std::string strat_label,
                           std::string output_dir) {
  // Actually, each pkt is its own thing. Let's iterate through each of them.
  for (rapidjson::Value& pkt_conf : trade_doc["pktraders"].GetArray()) {
    // A pkt_conf specifies the trader.

    if (pkt_conf.HasMember("enabled") && !pkt_conf["enabled"].GetBool()) {
      // If it's not enabled, skip it.
      std::cout << "PKTrader for " << pkt_conf["traded_symbol"].GetString()
                << " not enabled, skipping." << std::endl;
      continue;
    }

    // Create the PKTrader.
    SymbolId sym{pkt_conf["traded_symbol"].GetString()};

    traded_symbols_.push_back(sym);
    PKTrader* pkt = new PKTrader(pkt_conf, trade_doc["settings"]["commissions"], strat_label);
    pktraders_.push_back(pkt);
    sym_to_pktrader_[sym] = pkt;

    // Tell the trader to create its sim_verse if necessary too.
    // This might need to be elsewhere, I don't have a strong opinion yet.
    if (!pktrade::GlobalVar::live_) {
      rapidjson::Value& sim_conf = trade_doc["simulation"];
      pkt->createSimulators(sim_conf);
      pktrade::GlobalVar::sim_verse_->setRiskMan(sym, pkt->getRiskMan());

    } else {
      if (pktrade::GlobalVar::paper_trading_mode_) {
        // Do the same thing.
        rapidjson::Value& sim_conf = trade_doc["simulation"];
        pkt->createSimulators(sim_conf);
        pktrade::GlobalVar::sim_verse_->setRiskMan(sym, pkt->getRiskMan());
      } else {
        // Connect the trading contexts with the riskmen.
        if (pktrade::GlobalVar::use_local_context_) {
          // If we are trading hyperliquid,
          // we will be using live_context, EVEN if we are running in
          // local mode. So here, I'm just going to check the traded_market
          // and set context accordingly.
          // Then, later, we'll just be working through the traderiskman
          // so the ambiguity will be easier.
          Market traded_market = pkt->getTradedMarket();
          if (traded_market == Market::Hyperliquid) {
            pkt->getRiskMan()->setLiveContext(pktrade::GlobalVar::live_context_);
            pktrade::GlobalVar::live_context_->setRiskMan(sym, pkt->getRiskMan());
          } else {
            pkt->getRiskMan()->setLocalContext(pktrade::GlobalVar::local_context_);
            pktrade::GlobalVar::local_context_->setRiskMan(sym, pkt->getRiskMan());
          }
        } else {
          // Connect the livecontext with the riskmen. In this pathway, we
          // communicate over zmq to the gateway.
          pkt->getRiskMan()->setLiveContext(pktrade::GlobalVar::live_context_);
          pktrade::GlobalVar::live_context_->setRiskMan(sym, pkt->getRiskMan());
        }
      }
    }
  }

  // After 50s, check that all our books are in a good state...
  if (pktrade::GlobalVar::live_ &&
      clock_cast<Clock>(std::chrono::system_clock::now()) > pktrade::GlobalVar::start_t_) {
    pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                   std::chrono::seconds(50),
                                               [&] { this->heartbeat(); });

  } else {
    pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_,
                                               [&] { this->heartbeat(); });
  }
  // Create all small traders.

  // Call its own daystart stuff.
  if (pktrade::GlobalVar::live_ &&
      clock_cast<Clock>(std::chrono::system_clock::now()) > pktrade::GlobalVar::start_t_) {
    pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                   std::chrono::seconds(30),
                                               [&] { this->globalDayStart(); });

  } else {
    pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_,
                                               [&] { this->globalDayStart(); });
  }

  // Create the UMHandler if in live mode.
  if (!pktrade::GlobalVar::ignore_usermsgs_ && pktrade::GlobalVar::live_) {
    umh = std::make_unique<UMHandler>(this);
  }

  // For calling checkEventLoop: only check if >1min after start_t and also >1min after last check
  last_event_check_t_ = pktrade::GlobalVar::start_t_;
}

// queues the factories on what they subscribe to.
std::vector<BookId> TradeCarrier::getAllSubs() {
  std::set<pktrade::BookId> ids;

  // tempo
  std::vector<BookId> tempo_bids = pktrade::GlobalVar::tf_->getBookIds();
  ids.insert(tempo_bids.begin(), tempo_bids.end());

  // print tempos:

  // signals
  std::vector<BookId> signal_bids = pktrade::GlobalVar::sf_->getBookIds();
  ids.insert(signal_bids.begin(), signal_bids.end());

  // print signals:

  // ordex. Luckily, we implement this in the tradecarrier itself.
  std::vector<BookId> ordex_bids = getTradingSubs();
  ids.insert(ordex_bids.begin(), ordex_bids.end());

  // print ordexes:

  // Get extra subscriptions (for example, if we needed it for fastsims)
  for (PKTrader* pt : pktraders_) {
    std::vector<BookId> trader_extra_bookids = pt->getExtraSubs();
    ids.insert(trader_extra_bookids.begin(), trader_extra_bookids.end());
  }

  if (pktrade::GlobalVar::sim_verse_ != nullptr) {
    std::vector<BookId> sim_extra_bookids =
        pktrade::GlobalVar::sim_verse_->getRequiredDataSubs();
    ids.insert(sim_extra_bookids.begin(), sim_extra_bookids.end());
  }

  std::vector<pktrade::BookId> ids_vec(ids.begin(), ids.end());
  return ids_vec;
}

// queues the pktraders and their ordexes re: what they subscribe to.
std::vector<BookId> TradeCarrier::getTradingSubs() {
  std::set<pktrade::BookId> ids;
  // Add from all signals.
  for (PKTrader* pt : pktraders_) {
    std::vector<pktrade::BookId> trader_bookids = pt->getTradingSubs();
    ids.insert(trader_bookids.begin(), trader_bookids.end());
  }
  std::vector<pktrade::BookId> ids_vec(ids.begin(), ids.end());
  return ids_vec;
}

void TradeCarrier::postSecmaster() {
  for (PKTrader* pt : pktraders_) {
    pt->postSecmaster();
  }
}

// This subscribes things to mktdata, not to each other.
void TradeCarrier::subscribeAndFinalize() {
  // Subscribe tempos
  pktrade::GlobalVar::tf_->subscribeAll();

  // Subscribe signals
  pktrade::GlobalVar::sf_->subscribeAll();

  // Subscribe the sim_verse if necessary.
  if (!pktrade::GlobalVar::live_) {
    pktrade::GlobalVar::sim_verse_->subscribeData(pktrade::GlobalVar::md_beacon_);
  } else if (pktrade::GlobalVar::live_ && pktrade::GlobalVar::paper_trading_mode_) {
    pktrade::GlobalVar::sim_verse_->subscribeData(pktrade::GlobalVar::md_beacon_);
  }

  // Subscribe ordexes.
  pktrade::GlobalVar::of_->subscribeAll();

  // Subscribe pktraders if needed.
  for (PKTrader* pt : pktraders_) {
    pt->subscribe();
  }

  // Subscribe HL for oracle prices.
  subscribeHL();

  // Fin
}

void TradeCarrier::globalDayStart() {
  LOG(INFO) << " TradeCarrier::globalDayStart. Starting global processes";

  // Request for funding in live mode.
  if (pktrade::GlobalVar::live_) {
    pktrade::GlobalVar::funding_master_->start_funding_requests();
    checkNetwork();
  }

  // Also print its acct and position lines.
  printAcctLines();
}

void TradeCarrier::heartbeat() {
  LOG(INFO) << " ********************************** ";
  LOG(INFO) << " TradeCarrier: checking book states.";

  const std::unordered_map<BookId, BookData>& all_books =
      pktrade::GlobalVar::book_manager_->getBooks();
  for (auto book_it = all_books.begin(); book_it != all_books.end(); book_it++) {
    LOG(INFO) << pktrade::util::print_bookid(book_it->first) << " : depthupdateid "
              << book_it->second.book_update_last_update_id << " ticker_update_id "
              << book_it->second.ticker_update_last_update_id << " bb ba "
              << book_it->second.last_ticker_bb.toDouble() << " "
              << book_it->second.last_ticker_ba.toDouble() << " seen_snapshot "
              << book_it->second.seen_first_snapshot << std::endl;
  }
  LOG(INFO) << " ********************************** ";

  // Insert another heartbeat later, in 5 minutes.
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::seconds(300),
                                             [&] { this->heartbeat(); });
}

void TradeCarrier::printAcctLines() {
  if (pktrade::GlobalVar::acct_path_ != "") {
    // Write the accounting file.
    std::ofstream acct_out(pktrade::GlobalVar::acct_path_);
    std::string acct_lines = "date,sym,net_pnl,closed_pnl,commission,funding,min_pnl_seen,shs_"
                             "traded,shs_sent,"
                             "fill_rate,reachable_fill_rate,ioc_fill_rate,"
                             "times_traded,times_flipped,open_"
                             "pos";
    for (PKTrader* pt : pktraders_) {
      acct_lines += "\n";
      acct_lines += pt->getAcctLine();
    }

    // Write to accounting file.
    acct_out << acct_lines;
    acct_out.close();
  }
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(30 * 60),
                                             [&] { this->printAcctLines(); });
}



void TradeCarrier::emergencyEOD() {
  // Tell each pktrader to cancel its orders.
  for (const auto& pkt : pktraders_) {
    pkt->getRiskMan()->emergencyCancelOrders();
  }
}

// Welp. For me, I want to write out my accounting.
void TradeCarrier::eod() {
  std::cout << " Tradecarrier doing EOD accounting." << std::endl;
  printAcctLines();
}

// Loads inherit schedule CSV. Format: one event per line, no header.
// <epoch_ms>,<sym>,<size>
// e.g. 1770908123456,xyz:TSLA,50.0
void TradeCarrier::loadInheritSchedule(const std::string& path) {
  std::ifstream file(path);
  if (!file.is_open()) {
    throw std::runtime_error("Could not open inherit schedule file: " + path);
  }

  // Build sym -> riskman lookup
  std::unordered_map<std::string, pktrade::risk::TradeRiskMan*> sym_to_riskman;
  for (PKTrader* pt : pktraders_) {
    std::string sym = pt->getRiskMan()->getSymbol().get();
    sym_to_riskman[sym] = pt->getRiskMan();
  }

  int count = 0;
  std::string line;
  while (std::getline(file, line)) {
    if (line.empty()) continue;

    std::vector<std::string> parts = util::split_str(line, ',');
    if (parts.size() < 3) {
      LOG(WARNING) << "Skipping malformed inherit schedule line: " << line;
      continue;
    }

    int64_t epoch_ms = std::stoll(parts[0]);
    std::string sym = parts[1];
    double size = std::stod(parts[2]);

    auto it = sym_to_riskman.find(sym);
    if (it == sym_to_riskman.end()) {
      LOG(WARNING) << "Inherit schedule sym " << sym << " not found in pktraders, skipping";
      continue;
    }

    pktrade::risk::TradeRiskMan* riskman = it->second;
    Clock::time_point tp = time_utils::msToTp(epoch_ms);

    pktrade::GlobalVar::event_loop_->onTimeout(tp, [riskman, size, sym]() {
      LOG(INFO) << "Replaying inherit for " << sym << " size=" << size;
      std::cout << time_utils::nowToStr()  << "  Replaying inherit for " << sym << " size=" << size;
      riskman->addInheritPosition(size);
    });
    count++;
  }

  LOG(INFO) << "Loaded " << count << " inherit schedule events from " << path;
}

// Loads usermsg schedule TSV. Format: one event per line, no header.
// <epoch_ms>\t<sym>\t<um_id>\t<args_json>
void TradeCarrier::loadUsermsgSchedule(const std::string& path) {
  std::ifstream file(path);
  if (!file.is_open()) {
    throw std::runtime_error("Could not open usermsg schedule file: " + path);
  }

  int count = 0;
  std::string line;
  while (std::getline(file, line)) {
    if (line.empty()) continue;

    std::vector<std::string> parts = util::split_str(line, '\t');
    if (parts.size() < 4) {
      LOG(WARNING) << "Skipping malformed usermsg schedule line: " << line;
      continue;
    }

    int64_t epoch_ms = std::stoll(parts[0]);
    std::string sym = parts[1];
    int um_id = std::stoi(parts[2]);
    std::string args = parts[3];

    UserMsg um = {
        .timestamp = parts[0],
        .user = "sim_replay",
        .strat_id_regex_str = ".*",
        .strat_id_regex = std::regex(".*"),
        .sym = SymbolId{sym},
        .um_id = um_id,
        .args = args,
    };

    Clock::time_point tp = time_utils::msToTp(epoch_ms);

    pktrade::GlobalVar::event_loop_->onTimeout(tp, [this, um]() {
      LOG(INFO) << "Replaying usermsg: " << um.toString();
      this->handleUM(um);
    });
    count++;
  }

  LOG(INFO) << "Loaded " << count << " usermsg schedule events from " << path;
}

// Temporary(?) check to make sure event_loop isn't stuck/never started because of heap corruption
void TradeCarrier::checkEventLoop() {
  uint64_t event_count = pktrade::GlobalVar::event_loop_->getEventCount();
  if (event_count <= last_event_count_ && !alert_sent_) {
    LOG(ERROR) << fmt::format("Event loop stuck: last event count {} current event count {}",
                              last_event_count_, event_count);
    pktrade::util::Mailer::instance().send_mail(
        "Event loop stuck!", fmt::format("Last event count: {}\nCurrent event count: {}",
                                         last_event_count_, event_count));
    alert_sent_ = true;
    // Don't kill the process bc we might want it live to debug
  } else {
    LOG(INFO) << fmt::format("Event loop check: event count {}", event_count);
  }
  last_event_count_ = event_count;
}

// Forward usermsg to the appropriate pktraders
void TradeCarrier::handleUM(const UserMsg& um) {
  // Check strat_id against the targeted strat_id_regex
  if (!std::regex_match(pktrade::GlobalVar::strat_id_, um.strat_id_regex)) {
    LOG(INFO) << "Ignoring UM not targeted at strat_id " << pktrade::GlobalVar::strat_id_ << ": "
              << um.toString();
  } else {
    // Pass usermsg to pktraders and let them match sym
    LOG(INFO) << "Received UM targeting strat_id " << pktrade::GlobalVar::strat_id_ << ": "
              << um.toString();
    for (const auto& pkt : pktraders_) {
      pkt->handleUM(um);
    }
  }
  // Temporary(?) check here where it triggers regularly even if event loop is stuck
  // Only check if it's been at least 1min since start_t_, since sometimes we schedule usermsgs
  // right at dayStart and event loop hasn't started yet, triggering a false alarm.
  // Also only check if it's been at least 1min since the last check, since a UM can trigger
  // multiple checks at the same time, which will look like a stuck loop.
  Clock::time_point now_t = clock_cast<Clock>(std::chrono::system_clock::now());
  if (now_t > last_event_check_t_ + std::chrono::seconds(60)) {
    last_event_check_t_ = now_t;
    checkEventLoop();
  }
}

void TradeCarrier::getGlobalPositions() {
  // check the global hyperliquid position for my symbol.
  std::string api_url = pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_);

  rapidjson::Document doc;
  doc.SetObject();
  auto& allocator = doc.GetAllocator();

  doc.AddMember("type", rapidjson::Value("clearinghouseState", allocator), allocator);
  doc.AddMember("user", rapidjson::Value(pktrade::GlobalVar::hl_address_.c_str(), allocator),
                allocator);
  // Potentially set dex name
  if (dex_name_ != "") {
    doc.AddMember("dex", rapidjson::Value(dex_name_.c_str(), allocator), allocator);
  }

  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  doc.Accept(writer);

  cpr::Response res = pktrade::urls::CprRetry::Post(
      cpr::Url{api_url}, cpr::Header{{"Content-Type", "application/json"}},
      cpr::Body{buffer.GetString()}, cpr::Timeout{8000}); // 8 second timeout

  if (res.status_code != 200) {
    LOG(ERROR) << "Error fetching hyperliquid position - status: " << res.status_code
               << ", error: " << res.error.message << ", text: " << res.text;
    return;
  }

  if (res.error.code != cpr::ErrorCode::OK) {
    LOG(ERROR) << "CPR error fetching hyperliquid position: " << res.error.message;
    return;
  }

  // Parse the response.
  rapidjson::Document response_doc;
  response_doc.Parse(res.text.c_str());

  if (!response_doc.HasMember("assetPositions")) {
    LOG(ERROR) << "GetGlobalPosition: No assetPositions field in response";
    return;
  }

  // Otherwise, look through.
  for (const auto& entry : response_doc["assetPositions"].GetArray()) {
    const auto& pos = entry["position"];
    const std::string& sym = pos["coin"].GetString();
    double szi = std::atof(pos["szi"].GetString());
    global_positions_[SymbolId(sym)] = szi;
    LOG(INFO) << "getGlobalPosition: " << sym << " " << szi;
  }

  // Update time global positions last retrieved.
  global_positions_t_ = time_utils::nowToMs();
}

double TradeCarrier::getGlobalPosition(SymbolId sym) {
  // Check the dex name and extract it if necessary.
  size_t colon_pos = sym.get().find(':');
  if (colon_pos != std::string::npos) {
    std::string dex_name = sym.get().substr(0, colon_pos);
    if (dex_name_ != "" && dex_name_ != dex_name) {
      LOG(ERROR) << "Inconsistent dex names in traded symbols: " << dex_name_ << " " << dex_name;
    }
    dex_name_ = dex_name;
  }

  // If global positions was retrieved more than 10s ago, refresh them.
  if (global_positions_t_ == 0 || time_utils::nowToMs() - global_positions_t_ > 10'000) {
    getGlobalPositions();
  }
  // Return the retrieved value for this sym.
  return global_positions_[sym];
}

void TradeCarrier::checkNetwork() {
  std::string api_url = pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_);

  rapidjson::Document doc;
  doc.SetObject();
  auto& allocator = doc.GetAllocator();
  doc.AddMember("type", "perpDexs", allocator);

  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  doc.Accept(writer);

  cpr::Response res = pktrade::urls::CprRetry::Post(
      cpr::Url{api_url}, cpr::Header{{"Content-Type", "application/json"}},
      cpr::Body{buffer.GetString()}, cpr::Timeout{8000}); // 8 second timeout

  // Consider network down if we got a non-success response status code
  network_down_ = (res.status_code < 200 || res.status_code >= 300);

  // Recheck every 30s if network is down, 5m otherwise
  pktrade::GlobalVar::event_loop_->onTimeout(network_down_ ? std::chrono::seconds(30)
                                                           : std::chrono::seconds(60 * 5),
                                             [&] { this->checkNetwork(); });
}

void TradeCarrier::subscribeHL() {
  // For HYPERLIQUID Oracles
  // Only do this if we're live.
  if (!pktrade::GlobalVar::live_) {
    return;
  }

  auto url = "wss://api.hyperliquid.xyz/ws";
  if (pktrade::GlobalVar::hl_testnet_) {
    url = "wss://api.hyperliquid-testnet.xyz/ws";
  }
  LOG(INFO) << fmt::format("Subscribing to oracle data at {}", url);
  ws_.setUrl(url);

  // Explicitly set CA path for TLS because default can't seem to find it
  ix::SocketTLSOptions tlsOptions;
  tlsOptions.tls = true;
  tlsOptions.caFile = "/etc/ssl/certs/ca-certificates.crt"; // Ubuntu/Debian
  if (!std::filesystem::exists(tlsOptions.caFile)) {
    LOG(ERROR) << fmt::format("CA file {} not found, running without CA", tlsOptions.caFile);
    tlsOptions.caFile = "NONE";
  }
  ws_.setTLSOptions(tlsOptions);

  // HL needs a custom heartbeat, not the built-in ping frame from ixwebsocket.
  // https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats
  // They close connections inactive for 60s, so we send a heartbeat every 25s.
  // We'll start sending the heartbeats when the connection is open.
  // Still set the ping interval bc this detects silence and triggers auto-reconnects.
  ws_.setPingInterval(60);
  ws_.enableAutomaticReconnection(); // this is enabled by default but let's make it explicit
  ws_.setMinWaitBetweenReconnectionRetries(10'000); // 10s reconnect so we don't spin when down

  ws_.setOnMessageCallback([&, the_ws = &ws_](const ix::WebSocketMessagePtr& msg) {
    try {
      if (msg->type == ix::WebSocketMessageType::Message) {
        // If the message is a channelresponse, ignore.

        // Wrap this in a try/catch also.
        rapidjson::Document ws_doc;
        try {
          ws_doc.Parse(msg->str.c_str());
          if (ws_doc.HasParseError()) {
            LOG(ERROR) << fmt::format("Received ill-parsed HL message: {}", msg->str);
            return;
          }
        } catch (const std::exception& e) {
          LOG(ERROR) << fmt::format("Received ill-parsed HL message: {}", msg->str);
          return;
        }

        if (!ws_doc.HasMember("channel")) {
          LOG(ERROR) << fmt::format("Received ill-parsed HL message: {}", msg->str);
          return;
        }
        const std::string channel = ws_doc["channel"].GetString();

        if (channel == "activeAssetCtx") {

          // Then we can check the reported oracle price.
          //   {"channel":"activeAssetCtx","data":{"coin":"xyz:XYZ100","ctx":{"funding":"0.0000109878","openInterest":"2615.7722","prevDayPx":"26140.0","dayNtlVlm":"127875894.9337999821","premium":"0.0000192864","oraclePx":"25925.0","markPx":"25925.0","midPx":"25925.5","impactPxs":["25925.0","25926.0"],"dayBaseVlm":"4907.8022"}}}

          if (!ws_doc.HasMember("data") || !ws_doc["data"].HasMember("ctx") ||
              !ws_doc["data"]["ctx"].HasMember("oraclePx") ||
              !ws_doc["data"]["ctx"]["oraclePx"].IsString() || !ws_doc["data"].HasMember("coin") ||
              !ws_doc["data"]["coin"].IsString()) {
            LOG(ERROR) << fmt::format("Received ill-formed HL activeAssetCtx json: {}", msg->str);
            return;
          }
          const SymbolId sym{ws_doc["data"]["coin"].GetString()};
          const double oracle_px = std::atof(ws_doc["data"]["ctx"]["oraclePx"].GetString());
          if (!sym_to_pktrader_.contains(sym)) {
            LOG(ERROR) << fmt::format("Received oracle price for {} but not ours", sym.get());
            return;
          }
          sym_to_pktrader_[sym]->setOraclePx(oracle_px);
          LOG_EVERY_N(INFO, 101) << fmt::format("Received oracle price {} for {}", oracle_px,
                                                sym.get());

        } else if (channel == "pong") {
          LOG_EVERY_N(INFO, 11) << "Got heartbeat pong from HL";

        } else if (channel == "subscriptionResponse") {
          if (!ws_doc.HasMember("data") || !ws_doc["data"].HasMember("subscription") ||
              !ws_doc["data"]["subscription"].HasMember("coin") ||
              !ws_doc["data"]["subscription"]["coin"].IsString()) {
            LOG(ERROR) << fmt::format("Received ill-formed HL subscriptionResponse: {}", msg->str);
            return;
          }
          const std::string sym = ws_doc["data"]["subscription"]["coin"].GetString();
          LOG(INFO) << fmt::format("Subscription confirmed for {}", sym);

        } else if (channel == "error") {
          if (!ws_doc.HasMember("data") || !ws_doc["data"].IsString()) {
            LOG(ERROR) << fmt::format("Received ill-formed HL error: {}", msg->str);
            return;
          }
          LOG(ERROR) << fmt::format("Error from HL: {}", ws_doc["data"].GetString());

        } else {
          LOG(ERROR) << fmt::format("Unrecognized channel: {}", channel);
        }
        // Does nothing for other types of channels, we're not subscribing to those.

      } else if (msg->type == ix::WebSocketMessageType::Open) {
        LOG(INFO) << fmt::format("Connection established to {}", msg->openInfo.uri);
        // After we start, we need to send it subscriptions.
        // sub to activeassetcontext.
        for (SymbolId sym : traded_symbols_) {
          const std::string payload = fmt::format(
              R"(
{{
  "method": "subscribe",
  "subscription": {{
    "type": "activeAssetCtx",
    "coin": "{sym}"
  }}
}}
)",
              fmt::arg("sym", sym.get()));
          try {
            auto r = the_ws->send(payload);
            if (!r.success) {
              LOG(ERROR) << fmt::format("Failed to subscribe to {}", sym.get());
            }
          } catch (const std::exception& e) {
            LOG(ERROR) << fmt::format("Error subscribing to {}: {}", sym.get(), e.what());
          }
        }
        // Start heartbeat loop
        heartbeatHL();

      } else if (msg->type == ix::WebSocketMessageType::Pong) {
        LOG_EVERY_N(INFO, 11) << "Got websocket pong from HL";

      } else if (msg->type == ix::WebSocketMessageType::Error) {
        // HL has regular down times (usually in weekends) that trigger this, so just log warn
        LOG(WARNING) << fmt::format("Connection error: {}", msg->errorInfo.reason);

      } else {
        LOG(INFO) << fmt::format("Unhandled Hyperliquid websocket msg: {}", msg->str);
      }

    } catch (const std::exception& e) {
      LOG(ERROR) << fmt::format("Error in websocket callback: {}", e.what());
    }
  }); // end ws onCallback

  ws_.start();
}

void TradeCarrier::heartbeatHL() {
  const std::string heartbeat = R"({"method": "ping"})";
  if (ws_.getReadyState() != ix::ReadyState::Open) {
    LOG_EVERY_N(WARNING, 11) << "Skipping HL heartbeat because websocket is not open";
  } else {
    try {
      auto result = ws_.send(heartbeat);
      if (!result.success) {
        LOG(WARNING) << "Failed to send HL heartbeat";
      }
    } catch (const std::exception& e) {
      LOG(WARNING) << fmt::format("Exception sending HL heartbeat: {}", e.what());
    } catch (...) {
      LOG(WARNING) << "Unknown exception sending HL heartbeat";
    }
  }
  // Using fixed-time version instead of duration version of onTimeout because this could be
  // called before event loop starts, and duration version doesn't work then.
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::seconds(25),
                                             [&] { this->heartbeatHL(); });
}

} // namespace pktrade.
