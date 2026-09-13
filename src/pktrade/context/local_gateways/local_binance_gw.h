#pragma once

#include "local_gw.h"
#include <rapidjson/document.h>

#include <cpr/cpr.h>
#include <ixwebsocket/IXHttp.h>
#include <ixwebsocket/IXHttpClient.h>
#include <ixwebsocket/IXWebSocket.h>

#include "pktrade/oeapi.h"

/*

Initialized with a risk conf that looks like:
      "num_sessions": 15,
      "http_api_endpoint": "https://fapi.binance.com",
      "api_key":"",
      "api_secret":""

*/

using namespace pktrade;

namespace pktrade::localgateway {

// copied over from gateway.h
struct SendTimes {
  // Mark this right when we send the order out.
  int64_t send_t;
  // Written by the market.
  int64_t mkt_update_t;
  // Write this right when we receive.
  int64_t hear_back_t;

  // Initialize at send_t, and then write the update_t and hear_back_t when we
  // hear back the ack. There should be no way that we get the callback before
  // adding sendtimes inside the map.
  SendTimes(int64_t send_t) : send_t(send_t), mkt_update_t(0), hear_back_t(0) {}
};

class LocalBinanceGateway : public LocalGateway {
 public:
  LocalBinanceGateway(rapidjson::Document& risk_doc, LocalContext* lcl_ctx);
  ~LocalBinanceGateway();

  void onNewOrd(const NewOrder& msg, std::string pk_coid) override;

  void onCancelOrd(const CancelOrder& msg, ExchOID exch_oid, std::string sym,
                   std::string pk_coid) override;

 private:
  void initCprSession();
  void refreshListenKey();

  void connect();
  void disconnect();

  // Subscribes to the
  void subscribeToUserData();

  // Refreshing our sessions.
  void refreshCprConn();

  void handleOpenListenKeyResponse(const ix::HttpResponse& response);

  // BinanceFutures
  void handleBFExecutionReport(const rapidjson::Document& doc);

  // Is this being used anywhere?
  ix::HttpRequestArgsPtr createRequest(const std::string& endpoint, const std::string& verb,
                                       ix::HttpParameters* params, bool sign_request);

  std::string perp_endpoint(std::string_view name) const;

  std::map<PKCOID, SendTimes> send_times_;

  ix::HttpClient http_;
  ix::WebSocket ws_;

  int num_sessions_ = 0;
  std::vector<std::shared_ptr<cpr::Session>> cpr_sessions_;
  // Which cpr session are we currently at, for our round robin order sending.
  size_t cpr_session_idx_ = 0;

  // Prefix of where we send orders to.
  std::string http_api_endpoint_;
  // Prefix of where we subscribe listen to our own order updates.
  std::string wss_listen_endpoint_;

  // Secret keys for order placement.
  std::string api_key_;
  std::string api_secret_;

  std::string listen_key_;

  // We have some processes for keeping some gateway monitoring things
  // alive/"fast"
  std::thread listen_key_refresh_;
  std::atomic_bool connected_ = false;

  std::thread cpr_refresh_;

  // After this many cancels, just send back a cancelack. Something probably went wrong
  // and the order probably never saw the light of day.
  int n_cancels_before_ack_ = 5;
  // key: {executor_id}-{executor_order_id}
  std::map<std::string, int> n_cancels_;
};

} // namespace pktrade::localgateway
