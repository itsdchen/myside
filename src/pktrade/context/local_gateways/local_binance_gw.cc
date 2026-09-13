#include "local_binance_gw.h"

#include "pktrade/util/time_utils.h"

#include "pktrade/util/basiclib.h"

#include "pktrade/context/local_context.h"

#include "pktrade/context/local_crypto.h"
#include <fmt/format.h>

namespace pktrade::localgateway {

LocalBinanceGateway::LocalBinanceGateway(rapidjson::Document& risk_doc, LocalContext* lcl_ctx)
    : LocalGateway(Market::BinanceFutures, lcl_ctx), http_{true}

{
  if (risk_doc.HasMember("http_api_endpoint")) {
    http_api_endpoint_ = risk_doc["http_api_endpoint"].GetString();
  } else {
    throw std::runtime_error("Required field http_api_endpoint missing in risk doc");
  }

  if (risk_doc.HasMember("wss_listen_endpoint")) {
    wss_listen_endpoint_ = risk_doc["wss_listen_endpoint"].GetString();
  } else {
    throw std::runtime_error("Required field wss_listen_endpoint missing in risk doc");
  }

  if (risk_doc.HasMember("api_key")) {
    api_key_ = risk_doc["api_key"].GetString();
  } else {
    throw std::runtime_error("Required field api_key missing in risk doc");
  }

  if (risk_doc.HasMember("api_secret")) {
    api_secret_ = risk_doc["api_secret"].GetString();
  } else {
    throw std::runtime_error("Required field api_secret missing in risk doc");
  }

  // Set some options for our connection (requests, websockets) members.
  ix::SocketTLSOptions tls_options;
  tls_options.caFile = "NONE";
  ws_.setTLSOptions(tls_options);
  http_.setTLSOptions(tls_options);

  if (risk_doc.HasMember("num_sessions") && risk_doc["num_sessions"].IsInt()) {
    num_sessions_ = risk_doc["num_sessions"].GetInt();
  } else {
    throw std::runtime_error("Required field num_sessions missing in risk doc");
  }

  LOG(INFO) << " We have " << num_sessions_ << " sessions";

  initCprSession();
  connect();
};

LocalBinanceGateway::~LocalBinanceGateway() { disconnect(); }

void LocalBinanceGateway::onNewOrd(const NewOrder& neword, std::string pk_coid) {
  // Not sure why I need this decoded.

  // auto neword = NewOrder::decode(new_ord_msg);

  std::string side_str = neword.side == Side::Buy ? "BUY" : "SELL";

  // limit, market.
  std::string order_type_str;
  if (neword.order_type == OrderType::Limit) {
    order_type_str = "LIMIT";
  } else if (neword.order_type == OrderType::Market) {
    order_type_str = "MARKET";
  }

  // TODO: auto change this into a GTX.
  std::string time_in_force_str;
  if (neword.time_in_force == TimeInForce::GTC) {
    // Not quite proper, but I'll do it anyway as I think this is the better
    // thing to be done. We will just force ALO on binance-facing adders, the
    // commissions help is too much.
    time_in_force_str = "GTX";
    // time_in_force_str = "GTC";
  } else if (neword.time_in_force == TimeInForce::IOC) {
    time_in_force_str = "IOC";
  } else if (neword.time_in_force == TimeInForce::ALO) {
    // This is the flag for binancefutures.
    time_in_force_str = "GTX";
  }
  // TODO: GTX.

  std::string qty_str = std::to_string(neword.qty.toDouble());
  std::string px_str = std::to_string(neword.px.toDouble());

  // I'm using the very messy method of putting things together.
  // Including constructing this querystr almost twice. Later on I should
  // go back and test which one is actually better and clean this up.

  // Do something complteely different.  Construct and sign these params
  // the cpr way.
  int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                       std::chrono::system_clock::now().time_since_epoch())
                       .count();
  std::string queryString("");
  queryString.append("symbol=");
  queryString.append(neword.symbol.get());
  queryString.append("&side=");
  queryString.append(side_str);
  queryString.append("&type=");
  queryString.append(order_type_str);
  queryString.append("&quantity=");
  queryString.append(qty_str);
  queryString.append("&price=");
  queryString.append(px_str);
  queryString.append("&newOrderRespType=RESULT");
  queryString.append("&timeInForce=");
  queryString.append(time_in_force_str);

  queryString.append("&newClientOrderId=");
  queryString.append(pk_coid);

  if (neword.reduce_only) {
    // Don't do this for now, for some reason the reduceonly signature is
    // broken. Should look into this later.
    // queryString.append("&reduceOnly=true");
  }

  queryString.append("&timestamp=");
  queryString.append(std::to_string(now_ms));

  std::string signature = hexEncode(computeHmacSha256(api_secret_, queryString));

  cpr::Parameters new_parameters = cpr::Parameters{{"symbol", neword.symbol.get()},
                                                   {"side", side_str},
                                                   {"type", order_type_str},
                                                   {"quantity", qty_str},
                                                   {"price", px_str},
                                                   {"newOrderRespType", "RESULT"},
                                                   {"timeInForce", time_in_force_str},
                                                   {"newClientOrderId", pk_coid},
                                                   {"timestamp", std::to_string(now_ms)},
                                                   {"signature", signature}};
  if (neword.reduce_only) {
    // Again, don't do this for now, as stated above.
    // new_parameters.Add({"reduceOnly", "true"});
  }
  cpr_sessions_[cpr_session_idx_]->SetParameters(new_parameters);

  // I guess for now, let's just log that we got it.
  // LOG(INFO) << " Got NewOrder: " << data.ShortDebugString();
  // LOG(INFO) << "Placing once: " << queryString;
  cpr_sessions_[cpr_session_idx_]->PostCallback([&, pk_coid = pk_coid](cpr::Response r) {
    // std::cout << " Response for"  << pk_coid << " The text was:" <<
    // r.text
    // << " status:" << r.status_code << " reason:" << r.reason
    //           << std::endl;
    if (r.status_code == 200) {
      auto st_iter = send_times_.find(pk_coid);
      if (st_iter != send_times_.end()) {
        // rapidjson::Document doc;
        // doc.Parse(r.text.c_str());
        // st_iter->second.mkt_update_t = doc["updateTime"].GetInt64();
        st_iter->second.hear_back_t = std::chrono::duration_cast<std::chrono::milliseconds>(
                                          std::chrono::system_clock::now().time_since_epoch())
                                          .count();
        // Let's write it out.
        LOG(INFO) << "Order response. coid " << pk_coid << " send " << st_iter->second.send_t
                  << " mkt_update "
                  //        << st_iter->second.mkt_update_t << " hear_back "
                  << st_iter->second.hear_back_t << " rtt "
                  << st_iter->second.hear_back_t - st_iter->second.send_t << std::endl;
        LOG(INFO) << "response text: " << r.text << std::endl;
      }

      // For now, let's just deal with NewOrderAcks inside the execution report.
    } else {
      // parse and respond to the response code.
      // In some cases, this failure might not be a json at all, so check
      // isObject.

      LOG(INFO) << " POST call failed call! code " << r.status_code << " status_line "
                << r.status_line << " text " << r.text << std::endl;

      std::string reason = r.reason;
      rapidjson::Document doc;
      doc.Parse(r.text.c_str());
      if (doc.IsObject() && doc.HasMember("msg")) {
        reason = doc["msg"].GetString();
      } else {
        LOG(ERROR) << "POST Failure did not receive a json with an errormsg " << r.text;
      }

      // Exit immediately.
      lcl_ctx_->handleNewOrdReject(pk_coid, reason);
    }
  });

  cpr_session_idx_ = (cpr_session_idx_ + 1) % num_sessions_;

  // Write the sendtime.
  send_times_.emplace(pk_coid, std::chrono::duration_cast<std::chrono::milliseconds>(
                                   std::chrono::system_clock::now().time_since_epoch())
                                   .count());

  // LOG(INFO) << " Gateway: handling new order info" << std::endl;
}

void LocalBinanceGateway::onCancelOrd(const CancelOrder& new_cxl, ExchOID exch_oid, std::string sym,
                                      std::string pk_coid) {
  // auto msg = CancelOrder::decode(new_cxl);

  // Check if we already cancelled it. In which case, send the response and
  // exit.

  // todo.
  // This should happen inside the local_context, not here.
  /*
  if (order_manager_.isClosed(msg.gateway_order_id)) {
    // Send an update saying that it was cancelled.
    LOG(INFO) << " Tried to cancel already-gone " << new_cxl.strategy_id() << "
  "
              << new_cxl.executor_order_id() << " - sending a cancelAck
  instead.";

    PbMessage cxl_msg;
    auto* cxl_ack = cxl_msg.mutable_cancel_ack();
    cxl_ack->set_strategy_id(new_cxl.strategy_id());
    cxl_ack->set_executor_order_id(new_cxl.executor_order_id());
    cxl_ack->set_exch_transact_time(time_utils::nowToMs());
    publishGWResponse(cxl_msg);
    return;
  }
*/
  // LOG(INFO) << "Cancelling " << pk_coid << " exch_oid " << exch_oid;
  //  PbMessage ack_msg;
  //  auto* ack = ack_msg.mutable_gateway_ack();
  //  ack->set_strategy_id(msg.gateway_order_id.strategy_id);
  //  ack->set_executor_order_id(msg.gateway_order_id.order_id);
  //   ack->set_strategy_id(msg.strategy_id);

  //  auto& order = order_manager_.cancelOrder(msg);

  if (n_cancels_.contains(pk_coid) && n_cancels_[pk_coid] > n_cancels_before_ack_) {
    LOG(INFO) << " Tried to cancel " << pk_coid << " too many times. Sending CancelAck.";

    // Send back the cancelack.

    lcl_ctx_->handleCancelAck(pk_coid, time_utils::nowToMs());

    return;
  }

  if (n_cancels_.contains(pk_coid)) {
    n_cancels_[pk_coid]++;
  } else {
    n_cancels_[pk_coid] = 0;
  }

  int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                       std::chrono::system_clock::now().time_since_epoch())
                       .count();

  std::string queryString("");

  queryString.append("symbol=");
  queryString.append(sym);

  queryString.append("&orderId=");
  queryString.append(exch_oid);
  queryString.append("&timestamp=");
  queryString.append(std::to_string(now_ms));

  std::string signature = hexEncode(computeHmacSha256(api_secret_, queryString));

  cpr::Parameters new_parameters = cpr::Parameters{{"symbol", sym},
                                                   {"orderId", exch_oid},
                                                   {"timestamp", std::to_string(now_ms)},
                                                   {"signature", signature}};

  cpr_sessions_[cpr_session_idx_]->SetParameters(new_parameters);

  cpr_sessions_[cpr_session_idx_]->DeleteAsync();
  cpr_session_idx_ = (cpr_session_idx_ + 1) % num_sessions_;
  // END
}

void LocalBinanceGateway::connect() {
  // Open a Binance user data listen key for the duration of this process:
  // https://binance-docs.github.io/apidocs/spot/en/#listen-key-spot

  std::string user_report_url = perp_endpoint("listenKey");

  // std::cout << " LocalGateway connecting to user fills at " <<
  // user_report_url << std::endl;
  ix::HttpParameters params;
  auto request = createRequest(user_report_url, ix::HttpClient::kPost, &params, false);

  std::string body;
  auto response = http_.post(user_report_url, body, request);

  LOG(INFO) << "LocalGateway: Listen key request to " << user_report_url << std::endl;
  if (response->statusCode == 200) {
    LOG(INFO) << "LocalGateway: obtained listen key";

    // We expect an application/json response body like {"listenKey": "...."}
    rapidjson::Document doc;
    doc.Parse(response->body.c_str());
    listen_key_ = doc["listenKey"].GetString();

    connected_ = true;

    LOG(INFO) << "Starting threads for listenkey and cprconn";

    // Start listen key refresh thread
    listen_key_refresh_ = std::thread([&] { refreshListenKey(); });

    // Start cpr  refresh thread
    cpr_refresh_ = std::thread([&] { refreshCprConn(); });

    LOG(INFO) << "Got listen key" << std::endl;
    subscribeToUserData();
  } else {
    LOG(FATAL) << fmt::format("Failed to obtain listen key: {} [HTTP {}]", response->body,
                              response->statusCode);
  }
}

void LocalBinanceGateway::disconnect() {
  // Stop user data stream
  ws_.stop();
  LOG(INFO) << "LocalGateway: Stopped user data stream";

  // Stop and join listen key refresh thread
  connected_ = false;
  if (listen_key_refresh_.joinable()) {
    listen_key_refresh_.join();
  }
  LOG(INFO) << "LocalGateway: Stopped and joined listen key refresh thread";
}

void LocalBinanceGateway::initCprSession() {
  LOG(INFO) << "LocalGateway: Initializing CPR sessions";
  // Create the sessions
  for (uint i = 0; i < num_sessions_; i++) {
    cpr_sessions_.push_back(std::make_shared<cpr::Session>());

    // Do the very basics.
    cpr_sessions_[i]->SetHeader(
        cpr::Header{{"Content-Type", "application/json"}, {"X-MBX-APIKEY", api_key_}});

    cpr::SslOptions sslOpts = cpr::Ssl(cpr::ssl::MaxTLSVersion{}, cpr::ssl::ALPN{true},
                                       //
                                       cpr::ssl::NPN{false}, cpr::ssl::VerifyPeer{false},
                                       cpr::ssl::VerifyHost{false}, cpr::ssl::VerifyStatus{false});
    cpr_sessions_[i]->SetSslOptions(sslOpts);
    cpr_sessions_[i]->SetVerifySsl(cpr::VerifySsl{false});
    cpr_sessions_[i]->SetAcceptEncoding({{"deflate"}});

    // Initialize with the proper urls.
    std::string order_url = perp_endpoint("order");
    cpr_sessions_[i]->SetUrl(cpr::Url{order_url});
  }
}

void LocalBinanceGateway::refreshListenKey() {
  LOG(INFO) << "LocalGateway: Started listen key refresh thread";

  std::string user_report_url = perp_endpoint("listenKey");

  auto last_refresh_time = std::chrono::system_clock::now();

  // Token expires every 60 minutes without ping/pong. Refresh every 30 minutes
  while (connected_) {
    std::this_thread::sleep_for(std::chrono::seconds{60});
    auto now = std::chrono::system_clock::now();
    if (now - last_refresh_time >= std::chrono::minutes{30}) {
      LOG(INFO) << "Refreshing listen key";

      ix::HttpParameters params;
      params["listenKey"] = listen_key_;

      auto request = createRequest(user_report_url, ix::HttpClient::kPut, &params, false);
      auto response = http_.put(user_report_url, request->body, request);
      if (response->statusCode == 200) {
        last_refresh_time = now;
      } else {
        LOG(WARNING) << "Failed to refresh listen key: " << response->body;
      }
    }
  }
}

// TODO: figure out if I actually want to do this or not.
// Let this be a separate thing altogether.
void LocalBinanceGateway::refreshCprConn() {
  LOG(INFO) << "Started CPR Conns";
  int64_t last_refresh_time = time_utils::nowToMs();

  while (connected_) {
    std::this_thread::sleep_for(std::chrono::seconds{5});
    int64_t now = time_utils::nowToMs();
    if (now - last_refresh_time >= 60 * 1000) {
      LOG(INFO) << " CPPRCONN INTERNAL, REFRESHING CONNS";
      // hit em with the refresh.
      std::string time_url = perp_endpoint("time");
      std::string order_url = perp_endpoint("order");

      for (uint i = 0; i < num_sessions_; i++) {
        cpr_sessions_[i]->SetUrl(cpr::Url{time_url});
        cpr_sessions_[i]->Get();
        cpr_sessions_[i]->SetUrl(cpr::Url{order_url});
      }
      last_refresh_time = now;
    }
  }
}

ix::HttpRequestArgsPtr LocalBinanceGateway::createRequest(const std::string& url,
                                                          const std::string& verb,
                                                          ix::HttpParameters* params,
                                                          bool sign_request) {
  auto request = http_.createRequest(url, verb);

  // Enable HTTP logging
  request->verbose = true;
  request->logger = [](auto& msg) { VLOG(1) << msg; };

  // Append API key to request headers
  request->extraHeaders["X-MBX-APIKEY"] = api_key_;

  std::stringstream body;

  if (!params->empty()) {
    body << http_.serializeHttpParameters(*params) << "&";
  }

  if (sign_request) {
    // Append production timestamp to query string. This argument must
    // immediately preceed the signature, if it exists.
    body << "timestamp="
         << std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch())
                .count();

    // Append the HMAC SHA256 hex digest of the query string if this request is
    // signed. This must be the last query argument in the request.
    auto signature = hexEncode(computeHmacSha256(api_secret_, body.str()));
    body << "&signature=" << signature;
  }

  if (verb == ix::HttpClient::kPost || verb == ix::HttpClient::kPut) {
    request->body = body.str();
  } else {
    request->url += fmt::format("?{}", body.str());
  }

  return request;
}

void LocalBinanceGateway::subscribeToUserData() {
  auto url = fmt::format("{}/ws/{}", wss_listen_endpoint_, listen_key_);
  LOG(INFO) << "Subscribing to user data(order updates) stream " << url;
  ws_.setUrl(url);

  // I'm not sure if this is going to fix the disconnection issue. But copying
  // this over from pkmultifeed.
  // Set a ping interval so we can restart the connection if it dies.
  ws_.setPingInterval(60);
  ws_.enableAutomaticReconnection();           // this is enabled by default but let's
                                               // make it explicit
  ws_.setMinWaitBetweenReconnectionRetries(1); // Set min wait for reconnect
                                               // to 1ms for faster reconnect

  ws_.setOnMessageCallback([&](const ix::WebSocketMessagePtr& msg) {
    if (msg->type == ix::WebSocketMessageType::Message) {
      VLOG(1) << msg->str;
      // LOG(INFO) << "LBGW got " << msg->str;

      // Fail safely if malformatted.
      rapidjson::Document doc;
      try {
        doc.Parse(msg->str.c_str());
        if (doc.HasParseError()) {
          LOG(ERROR) << " Gateway receceived ill-parsed message: " << msg->str << std::endl;
          return;
        }

      } catch (std::exception e) {
        LOG(ERROR) << "Gateway received ill-parsed message: " << msg->str;
        return;
      }
      // TODO: make a conffile-enabled way to specify what we're choosing to do,
      // instead of inferring it from the feed response.
      if (std::strcmp(doc["e"].GetString(), "ORDER_TRADE_UPDATE") == 0) {
        // This is specific to BinanceFutures and BinanceCOINFutures.
        // Check the formats here:
        // https://binance-docs.github.io/apidocs/futures/en/#event-order-update
        // https://binance-docs.github.io/apidocs/delivery/en/#event-order-update
        // TODO: check this when
        handleBFExecutionReport(doc);
      } else {
        // Just catch unhandled stuff.
        LOG(INFO) << "Gateway websocket: unkown type " << msg->str;
      }
    } else if (msg->type == ix::WebSocketMessageType::Open) {
      LOG(INFO) << "Subscribed to userorder data stream";
    } else if (msg->type == ix::WebSocketMessageType::Error) {
      LOG(FATAL) << "Failed to subscribe to userorder data stream";
    } else if (msg->type == ix::WebSocketMessageType::Close) {
      LOG(INFO) << "Connection to userorder closed (Code: " << msg->closeInfo.code
                << " Reason: " << msg->closeInfo.reason << ")";
    }
  });

  // Start connection asynchronously
  ws_.start();
}

std::string LocalBinanceGateway::perp_endpoint(std::string_view name) const {
  return fmt::format("{}/fapi/v1/{}", http_api_endpoint_, name);
}

void LocalBinanceGateway::handleOpenListenKeyResponse(const ix::HttpResponse& response) {
  if (response.errorCode == ix::HttpErrorCode::Ok) {
    listen_key_ = response.body;
  }
}

// BinanceFutures
void LocalBinanceGateway::handleBFExecutionReport(const rapidjson::Document& doc) {
  auto* execution_type = doc["o"]["x"].GetString();
  if (std::strcmp(execution_type, "NEW") == 0) {
    PKCOID pk_coid = doc["o"]["c"].GetString();
    // LOG(INFO) << "LBGW: got NewOrderAck " << pk_coid << std::endl;

    lcl_ctx_->handleNewOrdAck(pk_coid, std::to_string(doc["o"]["i"].GetInt64()),
                              doc["T"].GetInt64());

    // auto* order = order_manager_.find(pk_coid);

  } else if (std::strcmp(execution_type, "CANCELED") == 0) {
    // OK. Be careful here, because this might not quite correct.
    // But in the SPOT, if we have a cancel, we have the original clientorderid
    // of the order. This is not true in perps.
    // Just make sure that "c" is what we think it is.

    PKCOID pk_coid = doc["o"]["c"].GetString();
    lcl_ctx_->handleCancelAck(pk_coid, doc["T"].GetInt64());

    // TODO: finish this.
    // auto* order = order_manager_.find(pk_coid);

  } else if (std::strcmp(execution_type, "TRADE") == 0) {
    PKCOID pk_coid = doc["o"]["c"].GetString();
    // Verifying:
    // https://binance-docs.github.io/apidocs/futures/en/#event-order-update
    // Big "L" is price
    // little "l" is qty
    // The
    lcl_ctx_->handleExec(pk_coid, Price{doc["o"]["L"].GetString()},
                         Quantity{doc["o"]["l"].GetString()}, doc["o"]["t"].GetInt64(),
                         doc["T"].GetInt64(), doc["o"]["m"].GetBool());
  } else if (std::strcmp(execution_type, "EXPIRED") == 0) {
    PKCOID pk_coid = doc["o"]["c"].GetString();

    lcl_ctx_->handleElim(pk_coid);

  } else {
    // Possible that there was something else?
    LOG(INFO) << " Gateway got unknown execution message" << doc["o"]["x"].GetString();
  }
}

} // namespace pktrade::localgateway
