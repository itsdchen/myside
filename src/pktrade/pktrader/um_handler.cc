#include "um_handler.h"

#include <chrono>
#include <filesystem>
#include <glog/logging.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/supabase_credentials.h"
#include "trade_carrier.h"

namespace pktrade {

namespace {

std::string readyStateToString(ix::ReadyState state) {
  switch (state) {
  case ix::ReadyState::Connecting:
    return "connecting";
  case ix::ReadyState::Open:
    return "open";
  case ix::ReadyState::Closing:
    return "closing";
  case ix::ReadyState::Closed:
    return "closed";
  }
  return "unknown";
}

} // namespace

UMHandler::UMHandler(TradeCarrier* tc) : tc_(tc) {
  // Get Supabase credentials from singleton
  const auto& sb_creds = pktrade::util::SupabaseCredentials::getInstance();
  table_name_ = sb_creds.getUsermsgTable();

  // Set the wss URL
  sb_creds.setUsermsgUrl(webSocket_);

  // Set up callback handlers
  webSocket_.setOnMessageCallback([this](const ix::WebSocketMessagePtr& msg) {
    if (msg->type == ix::WebSocketMessageType::Message) {
      handleSupabaseCB(msg->str);
    } else if (msg->type == ix::WebSocketMessageType::Open) {
      LOG(INFO) << "Connected to Supabase realtime for usermsgs";
      // Subscribe to channels after connection opens
      subscribe();
    } else if (msg->type == ix::WebSocketMessageType::Close) {
      LOG(INFO) << "Disconnected from Supabase realtime:" << " code: " << msg->closeInfo.code << ","
                << " reason: " << msg->closeInfo.reason << ","
                << " remote: " << (msg->closeInfo.remote ? "yes" : "no");
      LOG(INFO) << "Last processed usermsg ID: " << last_processed_id_;
      // A transport-level disconnect tears down the Phoenix channel, so every in-flight
      // channel-level ack we were waiting on is now moot and will never arrive. Reset to a clean
      // slate; automatic reconnection re-subscribes (re-incrementing) on the next Open. This also
      // prevents pending_subscribe_acks_ from leaking when a disconnect races the subscription
      // confirmation.
      // Disarm the watchdog: ixwebsocket's auto-reconnect owns recovery from here, and it re-arms on
      // the next subscribe(). (Leaving it armed would fire close() on a stale subscribe_sent_at_ and
      // race the reconnect on every normal drop.)
      awaiting_subscription_ = false;
      pending_subscribe_acks_ = 0;
      pending_unsubscribe_acks_ = 0;
      pending_heartbeat_acks_ = 0;
      subscribe_retries_ = 0;
    } else if (msg->type == ix::WebSocketMessageType::Error) {
      LOG(ERROR) << "WebSocket error: " << msg->errorInfo.reason;
    }
  });

  // Explicitly set CA path for TLS because default can't seem to find it
  ix::SocketTLSOptions tlsOptions;
  tlsOptions.tls = true;
  tlsOptions.caFile = "/etc/ssl/certs/ca-certificates.crt"; // Ubuntu/Debian
  if (!std::filesystem::exists(tlsOptions.caFile)) {
    throw std::runtime_error(fmt::format("CA file {} not found", tlsOptions.caFile));
  }
  webSocket_.setTLSOptions(tlsOptions);

  // Supabase recommends an actual heartbeat message every 25s to keep the connection alive
  // (otherwise it closes the connection after ~66s of inactivity), but the built-in ping seems to
  // work for that. Also, this is how ixwebsocket knows to auto-reconnect for us if the connection
  // goes down.
  // We set ping time to a fairly short 10s bc the connection will randomly drop, and we won't
  // receive any messages until our ping detects silence and reconnects.
  // TODO: Have queries ack usermsgs so usermsg.py can confirm receipt.
  webSocket_.setPingInterval(10);
  webSocket_.enableAutomaticReconnection(); // enabled by default, but just in case

  // Set the more substantial heartbeat inverval, otherwise even with pings the AWS load balancer
  // (on Supabase's server side) will kill the connection after 2 hours of inactivity
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::minutes(30),
                                             [&] { this->heartbeat(); });

  // Watchdog for silently-lost subscriptions (see checkSubscription()).
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::seconds(60),
                                             [&] { this->checkSubscription(); });

  // Arm the watchdog before connecting so it fires even if we never get an Open (and so never call
  // subscribe()). subscribe_sent_at_ is still epoch-0 here, so the first tick sees a huge elapsed
  // and flags it.
  awaiting_subscription_ = true;

  // Start the connection
  webSocket_.start();
}

UMHandler::~UMHandler() {
  // Send unsubscribe message
  unsubscribe();

  // Give it a moment to send
  std::this_thread::sleep_for(std::chrono::milliseconds(100));

  // Close the WebSocket
  webSocket_.stop();
}

bool UMHandler::safeSend(const std::string& msg) {
  const auto state = webSocket_.getReadyState();
  if (state != ix::ReadyState::Open) {
    LOG(WARNING) << "Not sending websocket message because connection is "
                 << readyStateToString(state);
    return false;
  }
  try {
    auto r = webSocket_.send(msg);
    if (!r.success) {
      LOG(ERROR) << "Error sending message to websocket: " << msg;
      return false;
    }
  } catch (const std::exception& e) {
    LOG(ERROR) << "EXCEPTION sending message to websocket: " << e.what();
    return false;
  } catch (...) {
    LOG(ERROR) << "UNKNOWN EXCEPTION sending message to websocket!";
    return false;
  }
  return true;
}

void UMHandler::subscribe() {
  if (subscribe_retries_ > 10) {
    LOG(ERROR) << "Subscription retried " << subscribe_retries_ << " times; forcing reconnect";
    awaiting_subscription_ = false;
    webSocket_.close();
    return;
  }

  LOG(INFO) << "Subscribing to usermsgs.";
  // Put together the message to subscribe to INSERT messages
  const std::string message = fmt::format(R"(
{{
  "topic": "realtime:public:{table_name}",
  "event": "phx_join",
  "payload": {{
    "config": {{
      "postgres_changes": [{{"event":"INSERT",
                            "schema":"public",
                            "table":"{table_name}"}}]
    }}
  }},
  "ref": "usermsg_sub"
}}
)",
                                          fmt::arg("table_name", table_name_));

  // Send the subscription request
  if (safeSend(message)) {
    ++pending_subscribe_acks_;
    // Start the confirmation watchdog: expect a "Subscribed to PostgreSQL" system event soon.
    subscribe_sent_at_ = clock_cast<Clock>(std::chrono::system_clock::now());
    awaiting_subscription_ = true;
  } else {
    // Something is wrong. Restart the websocket.
    LOG(ERROR) << "Failed to send subscription request; forcing reconnect";
    awaiting_subscription_ = false;
    webSocket_.close();
  }
}

void UMHandler::unsubscribe() {
  LOG(INFO) << "Unsubscribing from usermsgs";
  // Put together the JSON string for unsubscribing
  const std::string message = fmt::format(R"(
{{
  "topic": "realtime:public:{table_name}",
  "event": "phx_leave",
  "payload": {{}},
  "ref": "usermsg_unsub"
}})",
                                          fmt::arg("table_name", table_name_));

  // Send the unsubscription request
  if (safeSend(message)) {
    ++pending_unsubscribe_acks_;
  }
}

bool UMHandler::processRec(const rapidjson::Value& rec) {
  // Make sure rec is well-formed. We don't want to core dump
  // because of malformed usermsg.
  if (!rec.HasMember("id") || !rec["id"].IsInt() || !rec.HasMember("created_at") ||
      !rec["created_at"].IsString() || !rec.HasMember("user") || !rec["user"].IsString() ||
      !rec.HasMember("strat_id_regex") || !rec["strat_id_regex"].IsString() ||
      !rec.HasMember("sym") || !rec["sym"].IsString() || !rec.HasMember("um_id") ||
      !rec["um_id"].IsInt() || !rec.HasMember("args") ||
      !(rec["args"].IsNull() || rec["args"].IsObject())) {
    return false;
  }
  // Make sure we only process usermsgs in order, and remember the last one
  int64_t id = rec["id"].GetInt();
  if (id <= last_processed_id_) {
    // Supabase should never resend usermsgs, even on reconnects, and the only duplicate we might
    // get is from our own missed-usermsgs query, which we now dedup separately, so re-promoting
    // this to ERROR log. Note: last_processed_id_ reading/writing isn't locked, so race condition
    // could theoretically trigger this, but not going to add locking just for that.
    LOG(ERROR) << "Ignoring already-processed usermsg id " << id
               << " (last handled id: " << last_processed_id_ << ")";
    return true;
  }
  if (last_processed_id_ == 0) {
    // Probably a new usermsg arriving before the response to the initialization query.
    LOG(WARNING) << "Got usermsg id " << id << " before last_processed_id init";
  }
  last_processed_id_ = id;
  LOG(INFO) << "Processing usermsg id " << id;
  // Parse the usermsg
  const std::string strat_id_regex_str = rec["strat_id_regex"].GetString();
  // Compile the strat_id regex defensively: an invalid pattern (e.g. an
  // unbalanced paren in the user's strat target) makes std::regex throw
  // std::regex_error, which would otherwise propagate to terminate() and core
  // dump. Skip the malformed usermsg instead of crashing.
  std::regex strat_id_regex;
  try {
    strat_id_regex = std::regex(strat_id_regex_str);
  } catch (const std::regex_error& e) {
    LOG(ERROR) << "Ignoring usermsg id " << id << ": invalid strat_id_regex ("
               << strat_id_regex_str << "): " << e.what();
    return false;
  }
  const SymbolId sym{rec["sym"].GetString()};
  const std::string timestamp = rec["created_at"].GetString();
  const std::string user = rec["user"].GetString();
  const int um_id = rec["um_id"].GetInt();
  const std::string args = rec["args"].IsNull() ? "" : pktrade::util::rjson_to_str(rec["args"]);
  const UserMsg um = {.timestamp = timestamp,
                      .user = user,
                      .strat_id_regex_str = strat_id_regex_str,
                      .strat_id_regex = strat_id_regex,
                      .sym = sym,
                      .um_id = um_id,
                      .args = args};
  // Give the usermsg to the TradeCarrier to handle
  tc_->handleUM(um);
  return true;
}

void UMHandler::handleSupabaseCB(const std::string& msg) {
  rapidjson::Document json_msg;
  json_msg.Parse(msg.c_str());

  if (json_msg.HasParseError() || !json_msg.HasMember("event") || !json_msg.HasMember("payload")) {
    LOG(ERROR) << "Failed to parse Supabase realtime message: " << msg;
    return;
  }
  const std::string& event = json_msg["event"].GetString();
  const rapidjson::Value& payload = json_msg["payload"];

  if (event == "postgres_changes") {
    if (!payload.HasMember("data") || !payload["data"].HasMember("type") ||
        payload["data"]["type"] != "INSERT" || !payload["data"].HasMember("record")) {
      LOG(ERROR) << "Unrecognized postgres_changes message: " << msg;
      return;
    }
    const rapidjson::Value& rec = payload["data"]["record"];
    // Parse and handle the rec as a usermsg
    if (!processRec(rec)) {
      LOG(ERROR) << "Failed to process usermsg: " << msg;
      return;
    }

  } else if (event == "phx_reply") {
    if (!payload.HasMember("status")) {
      LOG(ERROR) << "Unrecognized phx_reply message: " << msg;
      return;
    }
    const std::string& status = payload["status"].GetString();
    if (status == "ok") {
      // Ack from either subscription, unsubscription, or heartbeat
      if (pending_subscribe_acks_ > 0) {
        LOG(INFO) << "Subscription reply status: ok";
        // Don't decrement pending_subscribe_acks_ until system event received
      } else if (pending_unsubscribe_acks_ > 0) {
        LOG(INFO) << "Unsubscription reply status: ok";
        // Don't decrement pending_unsubscribe_acks_ until phx_close event received
      } else if (pending_heartbeat_acks_ > 0) {
        LOG(INFO) << "Heartbeat reply status: ok";
        if (pending_heartbeat_acks_ > 0) --pending_heartbeat_acks_;
      } else {
        LOG(WARNING) << "Unexpected reply status: ok";
      }
    } else if (status == "error") {
      if (awaiting_subscription_) {
        LOG(WARNING) << "Reply status: error. Retrying subscription";
        if (pending_subscribe_acks_ > 0) --pending_subscribe_acks_;
        ++subscribe_retries_;
        subscribe();
      } else {
        LOG(ERROR) << "Reply status: error. Not from subscription. Payload: "
                   << pktrade::util::rjson_to_str(payload);
      }
    } else {
      LOG(ERROR) << "Unrecognized reply status: " << status;
    }

  } else if (event == "system") {
    if (payload.HasMember("message") &&
        std::string(payload["message"].GetString()) == "Subscribed to PostgreSQL") {
      LOG(INFO) << "Subscription complete with system event";
      if (pending_subscribe_acks_ > 0) --pending_subscribe_acks_;
      subscribe_retries_ = 0;
      awaiting_subscription_ = false; // confirmed; stand the watchdog down
      // Check for any missed usermsgs, or set last_processed_id_ as the most recent one
      LOG(INFO) << "Checking for missed usermsgs after ID " << last_processed_id_;
      checkMissedUMs();
    } else if (payload.HasMember("status") &&
               std::string(payload["status"].GetString()) == "error") {
      // Server-side subscription failure. Seen when Supabase Realtime is mid-migration and a node
      // returns a transient error (e.g. "cannot cast type record to realtime.user_defined_filter").
      // The connection is up but we're NOT actually subscribed, so no postgres_changes will flow
      // and checkMissedUMs() never runs -- we'd sit deaf until the next ping timeout happened to
      // cycle us. Force a reconnect so automatic reconnection re-establishes a fresh connection
      // (likely a different, healthy Realtime node) and re-subscribes. last_processed_id_ is
      // preserved, so the eventual successful reconnect's checkMissedUMs() backfills anything sent
      // in the gap.
      // NB: close(), not stop()/start(): stop() joins the websocket thread, and we ARE on that
      // thread in this callback, so stop() would self-deadlock. close() only closes the current
      // connection; with automatic reconnection enabled the run loop reconnects for us.
      LOG(ERROR) << "Subscription failed server-side, forcing reconnect. Payload: "
                 << pktrade::util::rjson_to_str(payload);
      // Disarm the watchdog now (it reads this on the event-loop thread); the pending-ack counters
      // are zeroed by the Close handler once close() disconnects us.
      awaiting_subscription_ = false;
      webSocket_.close();
    } else {
      LOG(ERROR) << "Unrecognized system event. Payload: " << pktrade::util::rjson_to_str(payload);
    }

  } else if (event == "presence_state") {
    // Just a message you get when subscribing or unsubscribing (should be empty payload)
    LOG(INFO) << "Event presence_state. Payload: " << pktrade::util::rjson_to_str(payload);

  } else if (event == "phx_close") {
    // Channel was closed from unsubscription, duplicate subscription, or unknown
    if (pending_unsubscribe_acks_ > 0) {
      LOG(INFO) << "Unsubscription complete with phx_close event";
      if (pending_unsubscribe_acks_ > 0) --pending_unsubscribe_acks_;
    } else if (pending_subscribe_acks_ > 0) {
      LOG(ERROR) << "Possible duplicate subscription";
      if (pending_subscribe_acks_ > 0) --pending_subscribe_acks_;
    } else {
      LOG(ERROR) << "Unexpected channel close. Payload: " << pktrade::util::rjson_to_str(payload);
      subscribe();
    }

  } else if (event == "phx_error") {
    // Channel might still be open but encountered error
    LOG(ERROR) << "Channel error. Payload: " << pktrade::util::rjson_to_str(payload);
    subscribe();

  } else {
    LOG(ERROR) << "Unrecognized event: " << event
               << ". Payload: " << pktrade::util::rjson_to_str(payload);
  }
}

void UMHandler::heartbeat() {
  LOG(INFO) << "Sending heartbeat";
  // Send Phoenix heartbeat - this is actual data through the AWS load balancer
  const std::string heartbeat = R"(
{
  "topic": "phoenix",
  "event": "heartbeat",
  "payload": {},
  "ref": "heartbeat"
}
)";
  if (safeSend(heartbeat)) {
    ++pending_heartbeat_acks_;
  }
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::minutes(30),
                                             [&] { this->heartbeat(); });
}

void UMHandler::checkSubscription() {
  // If we sent a subscription and never got the "Subscribed to PostgreSQL" confirmation within the
  // timeout, it was lost silently (no phx_reply error, no disconnect -- so the ping/pong watchdog
  // can't catch it because the connection itself is healthy). Force a reconnect so we re-subscribe
  // on a fresh connection. Rare, and checkMissedUMs() backfills on recovery, so the coarse poll is
  // fine. close(), not stop(): see the note in the "system"/error branch of handleSupabaseCB().
  if (awaiting_subscription_ &&
      clock_cast<Clock>(std::chrono::system_clock::now()) - subscribe_sent_at_.load() >
          std::chrono::seconds(10)) {
    LOG(ERROR) << "Subscription not confirmed within timeout; forcing reconnect";
    // Disarm the watchdog; the pending-ack counters are zeroed by the Close handler on disconnect.
    awaiting_subscription_ = false;
    webSocket_.close();
  }
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::seconds(60),
                                             [&] { this->checkSubscription(); });
}

void UMHandler::checkMissedUMs() {
  LOG(INFO) << "Querying for usermsgs with ID > " << last_processed_id_;
  cpr::Parameters params;
  params.Add({"select", "*"});
  if (last_processed_id_ == 0) {
    // Initialization query: just get the most recent UM and record its id
    params.Add({"order", "id.desc"});
    params.Add({"limit", "1"});
  } else {
    // Get everything since the last one we processed
    params.Add({"id", "gt." + std::to_string(last_processed_id_)});
    params.Add({"order", "id.asc"});
  }
  // At startup (and sometimes when SB bounces), a lot of queries can be calling this, so use a
  // long timeout so they hopefully succeed eventually. Doesn't hurt to take a bit longer anyway.
  const cpr::Timeout timeout{std::chrono::seconds(10)};
  auto& sb_creds = pktrade::util::SupabaseCredentials::getInstance();
  sb_creds.cprGetUsermsgsAS(params, timeout, this);
}

void UMHandler::onGetUsermsgs(const cpr::Response& r) {
  if (r.status_code != 200) {
    LOG(ERROR) << "Failed to query for missed usermsgs, status: " << r.status_code;
    return;
  }
  if (r.error.code != cpr::ErrorCode::OK) {
    LOG(ERROR) << "CPR error: " << r.error.message;
    return;
  }

  LOG(INFO) << "Response for missed usermsgs: " << r.text;
  rapidjson::Document response_doc;
  response_doc.Parse(r.text.c_str());
  // Safety checks.
  if (response_doc.HasParseError() || !response_doc.IsArray()) {
    LOG(ERROR) << "Failed to parse json response";
    return;
  }

  for (const rapidjson::Value& rec : response_doc.GetArray()) {
    if (!rec.HasMember("id") || !rec["id"].IsInt()) {
      LOG(ERROR) << "Failed to process usermsg";
      return;
    }
    const int id = rec["id"].GetInt();
    if (last_processed_id_ == 0) {
      last_processed_id_ = id;
      LOG(INFO) << "Initializing last_processed_id to " << last_processed_id_;
      // Only ever 1 record to process in this case, so could return or not.
    } else if (id <= last_processed_id_) {
      // last_processed_id_ already progressed past this "missed" usermsg, probably from a usermsg
      // received while query for missed usermsgs was en route, so ignore.
      LOG(INFO) << "Ignoring already-processed usermsg id " << id
                << " (last handled id: " << last_processed_id_ << ")";
    } else {
      // Probably a missed usermsg during disconnect/reconnect that needs to be processed.
      LOG(INFO) << "Checking missed usermsg id " << id;
      if (!processRec(rec)) {
        LOG(ERROR) << "Failed to process usermsgs";
        return;
      }
    }
  }
}

} // namespace pktrade
