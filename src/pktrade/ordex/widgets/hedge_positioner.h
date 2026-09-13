#pragma once

#include "pktrade/mdapi.h"
#include "pktrade/types.h"

#include <cpr/cpr.h>
#include <rapidjson/document.h>
#include <vector>

// For websocket subscription.
#include <ixwebsocket/IXHttp.h>
#include <ixwebsocket/IXHttpClient.h>
#include <ixwebsocket/IXWebSocket.h>

#include <openssl/hmac.h>
#include <openssl/sha.h>

/*
Leader and a lagger. This provides subscription and updates
for a hedge leader.

In the base case, a hedgeleader will be ignorant of hedged
positions, whereas a hedgelagger will take that into
account when they're making personal decisions.

The way this is implemented, I *want* the ordex to handle and manage what it
gets from the hedgepositioner.

There was a choice to have this responsibility be in the riskman or the ordex,
but I think we need to keep things clean.


*/

namespace pktrade::ordex {

class HedgePositioner {
 public:
  HedgePositioner();

  void config(SymbolId sym_, const rapidjson::Value& conf);

  // This should be for a specific symbol.
  double getLeaderPosition();

  // Potentially use notional if we want to hedge betta when
  // it's not 1-1.
  double getLeaderNotional();

  void startProcess();

  // position update subscriptions.

  // Note that this handle/update stuff is going to be
  // semi-shared with the gateway process. I think we want to do this
  // versus being a cleaner programmer.

  // Binance spot subscription

  // Binance Futures subscription

  void connectBF();

  // Bybit subscription

  // Hyperliquid subscription
  void connectHyperliquid();

  // Other subscription

  // useful utils.
  std::string computeHmacSha256(std::string_view key, std::string_view msg);
  std::string hexEncode(const std::string& in);

 protected:
  SymbolId leader_symbol_;
  Market leader_market_;

  // Not sure, but let's just make the sesh anyway.
  std::shared_ptr<cpr::Session> cpr_session_;

  ix::HttpClient ix_http_;
  ix::WebSocket ix_ws_;

  double leader_pos_;
  double leader_not_;

  // Refresh your websocket key this often.
  int refresh_key_mins_;

  // binance spot

  // binance futures

  std::string bf_api_key_;
  std::string bf_secret_key_;

  std::string hl_address_;

  // To start, I'm just going to make api calls to get the position, versus
  // subscribing to the websocket.
  int bf_update_fequency_s_;

  // Similarly for hyperliquid, let's keep it simple and not do the
  int hl_update_frequency_s_;
};

} // namespace pktrade::ordex
