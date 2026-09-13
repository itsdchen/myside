#include <stdio.h>

#include <filesystem>
#include <fmt/format.h>
#include <glog/logging.h>
#include <iostream>
#include <rapidjson/document.h>
#include <rapidjson/prettywriter.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>
#include <string>

#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"

#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>

#include <csignal>
#include <ixwebsocket/IXWebSocket.h>
#include <thread>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/supabase_credentials.h"

/*
  This will test subscribing to Supabase realtime to listen for Postgres changes.
  We will be using this interface to receive usermsgs. See supabase_realtime.py
  for table setup on Supabase.
*/

using namespace pktrade;

std::sig_atomic_t stop_flag = false;

class SupabaseRealtimeClient {
 private:
  ix::WebSocket webSocket_;
  std::string table_name_;
  std::thread heartbeat_thread_;

 public:
  SupabaseRealtimeClient() {
    // Get Supabase credentials from singleton
    pktrade::GlobalVar::hl_testnet_ = true; // override to use testnet
    const auto& sb_creds = pktrade::util::SupabaseCredentials::getInstance();
    table_name_ = sb_creds.getUsermsgTable();

    // Set the wss URL
    sb_creds.setUsermsgUrl(webSocket_);

    // Set up callback handlers
    webSocket_.setOnMessageCallback([this](const ix::WebSocketMessagePtr& msg) {
      if (msg->type == ix::WebSocketMessageType::Message) {
        handleSupabaseCB(msg->str);
      } else if (msg->type == ix::WebSocketMessageType::Open) {
        LOG(INFO) << "Connected to Supabase realtime";
        // Subscribe to channels after connection opens
        subscribe();
      } else if (msg->type == ix::WebSocketMessageType::Close) {
        LOG(INFO) << "Disconnected from Supabase realtime:" << " code: " << msg->closeInfo.code
                  << "," << " reason: " << msg->closeInfo.reason << ","
                  << " remote: " << (msg->closeInfo.remote ? "yes" : "no");
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

    // Set the built-in ping to every 25s. Supabase recommends an actual heartbeat message every
    // 25s to keep the connection alive (otherwise it closes the connection after ~66s of
    // inactivity), but the built-in ping seems to work for that. Also, this is how ixwebsocket
    // knows to auto-reconnect for us if the connection goes down.
    webSocket_.setPingInterval(25);

    // Make sure automatic reconnection is enabled (should be default, but just make sure)
    if (!webSocket_.isAutomaticReconnectionEnabled()) {
      LOG(INFO) << "Automatic reconnection was disabled. Enabling.";
      webSocket_.enableAutomaticReconnection();
    }

    // Start the connection
    LOG(INFO) << "Starting connection...";
    webSocket_.start();
    LOG(INFO) << "Connection started";
  }

  ~SupabaseRealtimeClient() {
    // Detach the heartbeat thread so we can interrupt and kill the main thread cleanly
    if (heartbeat_thread_.joinable()) {
      heartbeat_thread_.detach();
    }

    // Send unsubscribe message
    unsubscribe();

    // Give it a moment to send
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    // Close the WebSocket
    LOG(INFO) << "Stopping connection...";
    webSocket_.stop();
    LOG(INFO) << "Connection stopped";
  }

  void startHeartbeat() {
    heartbeat_thread_ = std::thread([this]() {
      LOG(INFO) << "Heartbeat thread started, tid=" << std::this_thread::get_id();
      try {
        while (!stop_flag) {
          LOG(INFO) << "Heartbeat sleeping for 30m";
          std::this_thread::sleep_for(std::chrono::minutes(30));
          if (stop_flag)
            break;
          heartbeat();
        }
      } catch (const std::exception& e) {
        LOG(ERROR) << "EXCEPTION in heartbeat thread: " << e.what();
      } catch (...) {
        LOG(ERROR) << "UNKNOWN EXCEPTION in heartbeat thread!";
      }
      LOG(ERROR) << "Heartbeat thread exiting!";
    });
  }

 private:
  void heartbeat() {
    // Send Phoenix heartbeat - this is actual data through the LB
    const std::string heartbeat = R"(
{
  "topic": "phoenix",
  "event": "heartbeat",
  "payload": {},
  "ref": "heartbeat"
}
)";
    LOG(INFO) << "Sending heartbeat";
    try {
      auto r = webSocket_.send(heartbeat);
      if (!r.success) {
        LOG(ERROR) << "Error sending heartbeat!";
      }
    } catch (const std::exception& e) {
      LOG(ERROR) << "EXCEPTION sending heartbeat: " << e.what();
    } catch (...) {
      LOG(ERROR) << "UNKNOWN EXCEPTION sending heartbeat!";
    }
  }

  void subscribe() {
    // Put together the JSON string for subscribing
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
}})",
                                            fmt::arg("table_name", table_name_));

    LOG(INFO) << "Subscribing with message: " << message;
    auto r = webSocket_.send(message);
    if (!r.success) {
      LOG(ERROR) << "Error sending subsribe message!";
    }
  }

  void unsubscribe() {
    // Put together the JSON string for unsubscribing
    // Technically, this isn't necessary, since closing the socket
    // will automatically clean up, but better to be thorough.
    const std::string message = fmt::format(R"(
{{
  "topic": "realtime:public:{}",
  "event": "phx_leave",
  "payload": {{}},
  "ref": "usermsg_unsub"
}})",
                                            table_name_);

    LOG(INFO) << "Unsubscribing with message: " << message;
    auto r = webSocket_.send(message);
    if (!r.success) {
      LOG(ERROR) << "Error sending unsubscribe message!";
    }
  }

  void handleSupabaseCB(const std::string& msg) {
    rapidjson::Document json_msg;
    json_msg.Parse(msg.c_str());

    if (json_msg.HasParseError() || !json_msg.HasMember("event") ||
        !json_msg.HasMember("payload")) {
      LOG(ERROR) << "Failed to parse Supabase realtime message: " << msg;
      return;
    }

    const std::string& event = json_msg["event"].GetString();
    const rapidjson::Value& payload = json_msg["payload"];
    LOG(INFO) << "Received event: " << event;

    if (event == "postgres_changes") {
      if (!payload.HasMember("data") || !payload["data"].HasMember("type") ||
          payload["data"]["type"] != "INSERT" || !payload["data"].HasMember("record")) {
        LOG(ERROR) << "Unrecognized postgres_changes message: " << msg;
        return;
      }

      // Parse and process the usermsg
      const rapidjson::Value& rec = payload["data"]["record"];
      const std::string user = rec["user"].GetString();
      const std::string strat_id_regex = rec["strat_id_regex"].GetString();
      const std::string sym = rec.HasMember("sym") ? rec["sym"].GetString() : "";
      const int um_id = rec["um_id"].GetInt();
      const std::string args = rec["args"].IsNull() ? "" : pktrade::util::rjson_to_str(rec["args"]);
      processUsermsg(user, strat_id_regex, sym, um_id, args);

    } else if (event == "phx_reply") {
      if (!payload.HasMember("status") || !payload.HasMember("response") ||
          !payload["response"].IsObject()) {
        LOG(ERROR) << "Unrecognized phx_reply message: " << msg;
        return;
      }
      rapidjson::StringBuffer buffer;
      rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
      payload.Accept(writer);
      const std::string& reply = buffer.GetString();
      LOG(INFO) << "Reply: " << reply;
      const std::string& status = payload["status"].GetString();
      if (status == "ok") {
        // This is either a heartbeat ack or a subscription ack
        LOG(INFO) << "Heartbeat or subscription ack received";
      } else if (status == "error") {
        LOG(ERROR) << "Retrying subscription";
        subscribe();
      } else {
        LOG(ERROR) << "Unrecognized reply status";
        return;
      }
    }
  }

  void processUsermsg(const std::string& user, const std::string& strat_id_regex,
                      const std::string& sym, const int um_id, const std::string& args) {
    LOG(INFO) << "Usermsg received: " << "user:" << user << ", "
              << "strat_id_regex:" << strat_id_regex << ", " << "sym:" << sym << ", "
              << "um_id:" << um_id << ", " << "args:" << args;
  }
};

void signalHandler(int signum) {
  LOG(ERROR) << "Interrupt signal (" << signum << ") received";
  stop_flag = true;
}

int main(int argc, char** argv) {
  std::string sym;

  CLI::App cmd_flags("pkdex");
  // cmd_flags.add_option("--symbol", sym, "Symbol");
  PARSE(cmd_flags, argc, argv);

  // Add realtime client, running in background
  SupabaseRealtimeClient realtime;

  signal(SIGINT, signalHandler);
  signal(SIGTERM, signalHandler);
  signal(SIGHUP, signalHandler);
  signal(SIGPIPE, SIG_IGN);

  realtime.startHeartbeat();
  while (!stop_flag) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }
  LOG(ERROR) << "Main loop exiting! (stop_flag " << stop_flag << ")";
}
