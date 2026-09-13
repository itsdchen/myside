#pragma once

#include <atomic>
#include <fmt/format.h>
#include <ixwebsocket/IXWebSocket.h>
#include <rapidjson/document.h>
#include <regex>
#include <string>

#include "pktrade/clock.h"
#include "pktrade/types.h"
#include "pktrade/util/sbcaller.h"

namespace pktrade {

class TradeCarrier;

struct UserMsg {
  std::string timestamp;
  std::string user;
  std::string strat_id_regex_str;
  std::regex strat_id_regex;
  SymbolId sym;
  int um_id;
  std::string args;

  std::string toString() const {
    return fmt::format("timestamp:{} user:{} strat_id_regex:{} sym:{} um_id:{} args:{}", timestamp,
                       user, strat_id_regex_str, sym.get(), um_id, args);
  }

  // Checks if the UserMsg targets a single strat/sym
  bool isSingular() const {
    return (strat_id_regex_str.find_first_of(".[]{}()*+?|") == std::string::npos &&
            sym.get() != "");
  }
};

class UMHandler : public pktrade::util::SBCaller {
 public:
  UMHandler(TradeCarrier* tc);
  ~UMHandler();

 private:
  ix::WebSocket webSocket_;
  std::string table_name_;
  TradeCarrier* tc_ = nullptr;
  int pending_subscribe_acks_ = 0;
  int pending_unsubscribe_acks_ = 0;
  int pending_heartbeat_acks_ = 0;
  int subscribe_retries_ = 0;
  int64_t last_processed_id_ = 0;

  // Subscription watchdog state (see checkSubscription()). awaiting_subscription_ is true while the
  // watchdog is armed -- from connection start or a sent subscribe until we're either confirmed
  // subscribed or we've handed recovery off to a (forced or automatic) reconnect. It is NOT a plain
  // "we're not subscribed" flag: it stands down on disconnect because ixwebsocket's auto-reconnect
  // owns that recovery, and re-arms on the next subscribe(). Written on the websocket thread, read
  // on the event-loop thread, hence atomic. subscribe_sent_at_ is timestamped off system_clock via
  // clock_cast, same as heartbeat() schedules, so the timeout is real wall-time in live mode.
  std::atomic<bool> awaiting_subscription_ = false;
  std::atomic<TimePoint> subscribe_sent_at_{};

  bool safeSend(const std::string& msg);
  void subscribe();
  void unsubscribe();
  bool processRec(const rapidjson::Value& rec);
  void handleSupabaseCB(const std::string& msg);
  void heartbeat();
  // Periodic watchdog (parallels heartbeat()): forces a reconnect if a subscription was sent but
  // never confirmed, catching silent subscription losses the ping/pong watchdog can't.
  void checkSubscription();
  void checkMissedUMs();

  // SBCaller interface
  void onGetUsermsgs(const cpr::Response& res) override;
};

} // namespace pktrade
