// Datalog application
//
// This app listens to an exchange's websocket market streams and writes them to
// file.
//
// Usage:
// $ ./simple_datalog --date 20240114 --market BinanceFutures --outpath
// raw/BinanceFutures/202040114.capture --batch-size 1 --ping 1 --syms
// BTCUSDT,ETHUSDT
//
// Current project: support both hyperliquid and bybit.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <endian.h>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <string_view>
#include <utility>

#include <mutex>

#include "pktrade/util/cli.h"
#include <csignal>
#include <fmt/format.h>
#include <glog/logging.h>
#include <unistd.h>

#include <nlohmann/json.hpp>

#include <ixwebsocket/IXHttp.h>
#include <ixwebsocket/IXHttpClient.h>

#include <ixwebsocket/IXConnectionState.h>
#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXUserAgent.h>
#include <ixwebsocket/IXWebSocket.h>

#include <zlib.h>
#include <zmq.hpp>

#include "mdmsg.pb.h"

#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

#include <magic_enum.hpp>

std::atomic<bool> interrupted(false);  // this flag is on SIGINT for graceful
                                       // Ctl-C-ing

///////////////////////////////////////////////////////////////
// BINANCE

int recv_messages_binance(std::vector<std::string> syms,
                          int64_t end_ts,
                          int ping_interval,
                          bool deflate,
                          pktrade::Market market,
                          std::string outpath) {
  bool should_reconnect = true;

  // Create the ws
  ix::WebSocket* ws = new ix::WebSocket();

  if (market == pktrade::Market::BinanceFutures) {
    std::string binance_endpoint = "fstream.binance.com";
    std::vector<std::string> stream_types = {"depth@0ms",   "depth@100ms",
                                             "depth@250ms", "depth@500ms",
                                             "bookTicker",  "trade"};
    std::vector<std::string> streams;
    for (auto sym : syms) {
      for (auto stream_type : stream_types) {
        std::transform(sym.begin(), sym.end(), sym.begin(),
                       [](unsigned char c) { return std::tolower(c); });
        streams.push_back(sym + "@" + stream_type);
        // url += sym + "@" + stream + "/";
      }
    }
    std::string url = fmt::format("wss://{}/stream?streams={}",
                                  binance_endpoint, fmt::join(streams, "/"));
    ws->setUrl(url);
  } else if (market == pktrade::Market::BinanceSPOT) {
    std::string binance_endpoint = "stream.binance.com";
    std::vector<std::string> stream_types = {"depth@100ms", "depth",
                                             "bookTicker", "trade"};
    std::vector<std::string> streams;
    for (auto sym : syms) {
      for (auto stream_type : stream_types) {
        std::transform(sym.begin(), sym.end(), sym.begin(),
                       [](unsigned char c) { return std::tolower(c); });
        streams.push_back(sym + "@" + stream_type);
        // url += sym + "@" + stream + "/";
      }
    }
    std::string url = fmt::format("wss://{}/stream?streams={}",
                                  binance_endpoint, fmt::join(streams, "/"));
    ws->setUrl(url);
  } else if (market == pktrade::Market::BinanceCOINFutures) {
    std::string binance_endpoint = "dstream.binance.com";
    std::vector<std::string> stream_types = {"depth@0ms",   "depth@100ms",
                                             "depth@250ms", "depth@500ms",
                                             "bookTicker",  "trade"};
    std::vector<std::string> streams;
    for (auto sym : syms) {
      for (auto stream_type : stream_types) {
        std::transform(sym.begin(), sym.end(), sym.begin(),
                       [](unsigned char c) { return std::tolower(c); });
        streams.push_back(sym + "@" + stream_type);
        // url += sym + "@" + stream + "/";
      }
    }
    std::string url = fmt::format("wss://{}/stream?streams={}",
                                  binance_endpoint, fmt::join(streams, "/"));
    ws->setUrl(url);

    //} else if (market == "BybitDeriv") {
    // ws->setUrl("wss://stream.bybit.com/v5/public/linear");
  } else {
    throw std::runtime_error("Unknown market provided");
  }

  if (deflate) {
    ws->enablePerMessageDeflate();
  } else {
    ws->disablePerMessageDeflate();
  }

  ix::SocketTLSOptions tlsOptions;
  tlsOptions.caFile = "NONE";
  ws->setTLSOptions(tlsOptions);
  ws->setPingInterval(ping_interval);
  ws->enableAutomaticReconnection();  // this is enabled by default but let's
                                      // make it explicit
  ws->setMinWaitBetweenReconnectionRetries(1);  // Set min wait for reconnect to
                                                // 1ms for faster reconnect

  bool stop_ws = false;
  // Write tgt.
  std::ofstream out_file(outpath, std::ios_base::app);
  if (!out_file.is_open()) {
    std::cerr << "Failed to open outfile: " << std::strerror(errno)
              << std::endl;
    return 1;
  }

  // std::vector<TimestampedMessage_t> msgs;
  // msgs.reserve(batch_size);
  int64_t count = 0;
  ws->setOnMessageCallback([&](const ix::WebSocketMessagePtr& msg) {
    if (msg->type == ix::WebSocketMessageType::Message) {
      if (stop_ws || interrupted) {
        return;
      }
      count++;
      auto now = std::chrono::system_clock::now();

      // Convert time_point to a duration since the epoch
      auto duration_since_epoch = now.time_since_epoch();

      // Convert the duration to nanoseconds
      auto recv_ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         duration_since_epoch)
                         .count();
      out_file << recv_ts << "," << msg->str << std::endl;

      if (recv_ts > end_ts) {
        LOG(INFO) << "Stopping because past end ts";
        stop_ws = true;
        return;
      }

      // Print every so often
      if (count % 100000 == 0) {
        std::cout << "Received " << count << " messages" << std::endl;
      }
    } else if (msg->type == ix::WebSocketMessageType::Open) {
      LOG(INFO) << "Connection established to " << msg->openInfo.uri;
      /*
      if (market == "BybitDeriv") {
        // Need to send ws message to subscribe to streams for Bybit

        std::vector<std::string> streams;
        std::vector<std::string> stream_types = {
            "publicTrade", "orderbook.1", "orderbook.50", "orderbook.500"};
        for (auto sym : syms) {
          for (auto stream_type : stream_types) {
            streams.push_back(stream_type + "." + sym);
          }
        }

        nlohmann::json payload = {{"op", "subscribe"}, {"args", streams}};
        ws->send(payload.dump());
      }
      */
    } else if (msg->type == ix::WebSocketMessageType::Error) {
      LOG(ERROR) << "Connection error: " << msg->errorInfo.reason;
    } else if (msg->type == ix::WebSocketMessageType::Close) {
      LOG(INFO) << "Connection closed (Code: " << msg->closeInfo.code
                << " Reason: " << msg->closeInfo.reason << ")";
    } else if (msg->type == ix::WebSocketMessageType::Ping) {
      LOG(INFO) << "Got ping";
    } else if (msg->type == ix::WebSocketMessageType::Pong) {
      LOG_EVERY_N(INFO, 100) << "Got pong";
    } else if (msg->type == ix::WebSocketMessageType::Fragment) {
      LOG(INFO) << "Got fragment";
    } else {
      LOG(ERROR) << "Unknown message type";
    }
  });
  ws->start();

  while (!stop_ws && !interrupted) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }
  ws->stop();
  LOG(INFO) << "Stopped ws receiver";
  out_file.close();

  return 0;
}

///////////////////////////////////////////////////////////////
// Hyperliquid

// Filters `syms` to those known to be in Hyperliquid's published universe.
// Issues HTTP POSTs to /info for:
//   - {"type":"meta"}                     -> default perp universe
//   - {"type":"meta","dex":"<prefix>"}    -> each "<prefix>:..." dex perp universe
//   - {"type":"spotMeta"}                 -> spot universe (PURR/USDC, @N)
// The endpoints we hit are determined by what's in `syms`, so we don't pay
// for fetches we don't need.
//
// Each call has bounded timeouts and is retried a few times on failure (the
// /info endpoint can be flaky). Critically, if all retries for a given
// endpoint fail, syms that depended on it are KEPT (not dropped) -- we'd
// rather risk a runtime cascade than silently throw away good syms because
// we couldn't talk to /info at startup.
std::vector<std::string> filter_syms_by_hyperliquid_universe(
    const std::vector<std::string>& syms) {
  // Classify each sym by which endpoint covers it.
  // - "" key: default-dex perp ({"type":"meta"})
  // - other key: dex-prefixed perp ({"type":"meta","dex":<key>})
  std::set<std::string> dex_prefixes;
  bool need_spot = false;
  for (const auto& sym : syms) {
    if (sym.empty()) continue;
    size_t colon_idx = sym.find(":");
    if (colon_idx != std::string::npos) {
      dex_prefixes.insert(sym.substr(0, colon_idx));
    } else if (sym[0] == '@' || sym.find('/') != std::string::npos) {
      need_spot = true;
    } else {
      dex_prefixes.insert("");
    }
  }

  const std::string info_url = "https://api.hyperliquid.xyz/info";
  // 3 attempts × ~10s worst case = 30s upper bound per endpoint. With current
  // typical latency (<1s), happy path is unaffected.
  const int max_attempts = 3;
  const int connect_timeout_s = 5;
  const int transfer_timeout_s = 10;
  const auto retry_backoff = std::chrono::seconds(1);

  // Single-endpoint fetch: POST `body` to /info, parse `universe[].name` into
  // `out`. Retries on transport/HTTP/parse failure. Returns true iff one
  // attempt fully succeeded.
  auto fetch_endpoint = [&](const std::string& body,
                            std::set<std::string>& out) -> bool {
    for (int attempt = 1; attempt <= max_attempts; ++attempt) {
      // New client per attempt — cheap, and avoids any sticky state from a
      // prior failed connect.
      ix::HttpClient http;
      // HttpClient defaults won't reach https://api.hyperliquid.xyz without
      // a TLS config; mirror the WS path which uses caFile="NONE" (system
      // CAs / no extra verification).
      ix::SocketTLSOptions tlsOptions;
      tlsOptions.caFile = "NONE";
      http.setTLSOptions(tlsOptions);

      auto args = http.createRequest();
      args->extraHeaders["Content-Type"] = "application/json";
      args->connectTimeout = connect_timeout_s;
      args->transferTimeout = transfer_timeout_s;

      auto resp = http.post(info_url, body, args);
      if (resp && resp->statusCode == 200) {
        try {
          auto j = nlohmann::json::parse(resp->body);
          if (j.contains("universe") && j["universe"].is_array()) {
            for (const auto& item : j["universe"]) {
              std::string name = item.value("name", "");
              if (!name.empty()) {
                out.insert(name);
              }
            }
            return true;
          }
          LOG(WARNING) << "Universe payload missing 'universe' array (body="
                       << body << "), attempt=" << attempt << "/"
                       << max_attempts;
        } catch (const std::exception& e) {
          LOG(WARNING) << "Universe parse failed (body=" << body
                       << "), attempt=" << attempt << "/" << max_attempts
                       << ": " << e.what();
        }
      } else {
        LOG(WARNING) << "Universe fetch failed (body=" << body
                     << "), attempt=" << attempt << "/" << max_attempts
                     << ", status=" << (resp ? resp->statusCode : -1);
      }
      if (attempt < max_attempts) {
        std::this_thread::sleep_for(retry_backoff);
      }
    }
    return false;
  };

  // Run each needed fetch and remember which ones succeeded so we can decide
  // per-sym whether membership-checking is meaningful.
  std::map<std::string, std::set<std::string>> dex_names;  // dex -> names
  std::set<std::string> ok_dexes;
  for (const auto& dex : dex_prefixes) {
    nlohmann::json body = {{"type", "meta"}};
    if (!dex.empty()) body["dex"] = dex;
    if (fetch_endpoint(body.dump(), dex_names[dex])) {
      ok_dexes.insert(dex);
    }
  }
  std::set<std::string> spot_names;
  bool spot_ok = false;
  if (need_spot) {
    spot_ok = fetch_endpoint("{\"type\":\"spotMeta\"}", spot_names);
  }

  // Apply filter. For each sym, identify its covering endpoint:
  //   - if that endpoint succeeded: drop the sym iff it's not in the names
  //     we got back.
  //   - if that endpoint failed: keep the sym, since we have no basis to
  //     drop it. We log a WARNING so the situation is visible.
  std::vector<std::string> filtered;
  filtered.reserve(syms.size());
  int kept_unverified = 0;
  for (const auto& sym : syms) {
    if (sym.empty()) continue;
    size_t colon_idx = sym.find(":");
    if (colon_idx != std::string::npos) {
      // Dex perp.
      std::string dex = sym.substr(0, colon_idx);
      if (ok_dexes.count(dex)) {
        if (dex_names[dex].count(sym)) {
          filtered.push_back(sym);
        } else {
          LOG(WARNING) << "Pre-flight: sym not in dex='" << dex
                       << "' universe, skipping: " << sym;
        }
      } else {
        LOG(WARNING) << "Pre-flight: dex='" << dex
                     << "' meta fetch failed; keeping unverified: " << sym;
        filtered.push_back(sym);
        kept_unverified++;
      }
    } else if (sym[0] == '@' || sym.find('/') != std::string::npos) {
      // Spot pair.
      if (spot_ok) {
        if (spot_names.count(sym)) {
          filtered.push_back(sym);
        } else {
          LOG(WARNING) << "Pre-flight: sym not in spot universe, skipping: "
                       << sym;
        }
      } else {
        LOG(WARNING) << "Pre-flight: spotMeta fetch failed; keeping "
                        "unverified: "
                     << sym;
        filtered.push_back(sym);
        kept_unverified++;
      }
    } else {
      // Default-dex perp.
      if (ok_dexes.count("")) {
        if (dex_names[""].count(sym)) {
          filtered.push_back(sym);
        } else {
          LOG(WARNING) << "Pre-flight: sym not in default perp universe, "
                          "skipping: "
                       << sym;
        }
      } else {
        LOG(WARNING) << "Pre-flight: default perp meta fetch failed; "
                        "keeping unverified: "
                     << sym;
        filtered.push_back(sym);
        kept_unverified++;
      }
    }
  }
  LOG(INFO) << "Pre-flight: subscribing to " << filtered.size() << " of "
            << syms.size() << " syms (" << (syms.size() - filtered.size())
            << " dropped, " << kept_unverified << " kept unverified).";
  return filtered;
}

int recv_messages_hyperliquid(std::vector<std::string> syms,
                              int64_t end_ts,
                              int ping_interval,
                              bool deflate,
                              std::string outpath) {
  bool should_reconnect = true;

  // ----- Pre-flight sym validation -----
  // Background: a single bad coin in `syms` (e.g. a misspelled ticker) makes
  // the Hyperliquid server abnormally close the WS shortly after the bad
  // subscribe is processed. Auto-reconnect immediately re-runs the same
  // subscribe loop, the conn dies again at the same point, and the entire
  // tail of the sym list never produces data.
  //
  // Fix: query the /info meta endpoints up-front (with retries + bounded
  // timeouts) and drop syms that aren't in the published universe. Syms whose
  // covering endpoint fails to fetch are kept (logged as "unverified") rather
  // than dropped, so a transient /info outage doesn't cost us a day of data.
  syms = filter_syms_by_hyperliquid_universe(syms);

  // Create the ws
  ix::WebSocket* ws = new ix::WebSocket();

  std::string hyperliquid_wss_tgt = "wss://api.hyperliquid.xyz/ws";
  ws->setUrl(hyperliquid_wss_tgt);

  if (deflate) {
    ws->enablePerMessageDeflate();
  } else {
    ws->disablePerMessageDeflate();
  }

  ix::SocketTLSOptions tlsOptions;
  tlsOptions.caFile = "NONE";
  ws->setTLSOptions(tlsOptions);

  ws->setPingInterval(ping_interval);
  ws->enableAutomaticReconnection();  // this is enabled by default but let's
                                      // make it explicit
  ws->setMinWaitBetweenReconnectionRetries(1);  // Set min wait for reconnect to
                                                // 1ms for faster reconnect

  bool stop_ws = false;
  // Write tgt.
  std::ofstream out_file(outpath, std::ios_base::app);

  if (!out_file.is_open()) {
    std::cerr << "Failed to open outfile: " << std::strerror(errno)
              << std::endl;
    return 1;
  }

  // std::vector<TimestampedMessage_t> msgs;
  // msgs.reserve(batch_size);
  int64_t count = 0;
  ws->setOnMessageCallback([&](const ix::WebSocketMessagePtr& msg) {
    if (msg->type == ix::WebSocketMessageType::Message) {
      if (stop_ws || interrupted) {
        return;
      }
      count++;
      auto now = std::chrono::system_clock::now();

      // Convert time_point to a duration since the epoch
      auto duration_since_epoch = now.time_since_epoch();

      // Convert the duration to nanoseconds
      auto recv_ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         duration_since_epoch)
                         .count();
      out_file << recv_ts << "," << msg->str << std::endl;

      if (recv_ts > end_ts) {
        LOG(INFO) << "Stopping because past end ts";
        stop_ws = true;
        return;
      }

      // Print every so often
      if (count % 100000 == 0) {
        std::cout << "Received " << count << " messages" << std::endl;
      }
    } else if (msg->type == ix::WebSocketMessageType::Open) {
      LOG(INFO) << "Connection established to " << msg->openInfo.uri;

      std::cout << " Connection established, sending request" << std::endl;
      // After we start, we need to send it subscriptions. Pre-flight already
      // filtered `syms` to ones in the published universe, so we don't expect
      // the server to reject any of these.

      for (std::string& sym : syms) {
        // Potentially do something different if the sym is a dex sym.
        size_t colon_idx = sym.find(":");

        // sub to trades
        nlohmann::json trd_sub_details;
        trd_sub_details["type"] = "trades";
        trd_sub_details["coin"] = sym;
        if (colon_idx != std::string::npos) {
          trd_sub_details["dex"] = sym.substr(0, colon_idx);
        }

        nlohmann::json trd_payload = {{"method", "subscribe"}};
        trd_payload["subscription"] = trd_sub_details;
        ws->send(trd_payload.dump());

        // sub to l2 book (slow: top-20 levels, ~5s cadence).
        nlohmann::json sub_details;
        sub_details["type"] = "l2Book";
        sub_details["coin"] = sym;
        if (colon_idx != std::string::npos) {
          // Was previously assigning to trd_sub_details by mistake (typo); the
          // l2Book subscribe never carried a dex field. Server tolerated it
          // because `coin` already includes the "<dex>:" prefix, but fix anyway.
          sub_details["dex"] = sym.substr(0, colon_idx);
        }

        nlohmann::json payload = {{"method", "subscribe"}};
        payload["subscription"] = sub_details;
        ws->send(payload.dump());

        // sub to l2 book (fast: top-5 levels, ~0.5s cadence). Capture
        // both streams to disk so downstream tools have the same data
        // pymultifeed/pkmultifeed merge from. Pre-upgrade this returns
        // the same slow shape; either way it's just more lines in the
        // capture file.
        nlohmann::json fast_sub_details = sub_details;
        fast_sub_details["fast"] = true;
        nlohmann::json fast_payload = {{"method", "subscribe"}};
        fast_payload["subscription"] = fast_sub_details;
        ws->send(fast_payload.dump());
      }
    } else if (msg->type == ix::WebSocketMessageType::Error) {
      LOG(ERROR) << "Connection error: " << msg->errorInfo.reason;
    } else if (msg->type == ix::WebSocketMessageType::Close) {
      LOG(INFO) << "Connection closed (Code: " << msg->closeInfo.code
                << " Reason: " << msg->closeInfo.reason << ")";
    } else if (msg->type == ix::WebSocketMessageType::Ping) {
      LOG(INFO) << "Got ping";
    } else if (msg->type == ix::WebSocketMessageType::Pong) {
      LOG_EVERY_N(INFO, 100) << "Got pong";
    } else if (msg->type == ix::WebSocketMessageType::Fragment) {
      LOG(INFO) << "Got fragment";
    } else {
      LOG(ERROR) << "Unknown message type";
    }
  });
  ws->start();

  while (!stop_ws && !interrupted) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }
  ws->stop();
  LOG(INFO) << "Stopped ws receiver";
  out_file.close();
  return 0;
}

///////////////////////////////////////////////////////////////
// Hyperliquid (via n1 node publisher SUB instead of public WS)
//
// One ZMQ SUB socket to a single tcp endpoint of the n1 node publisher
// (default ports 5555 books / 5556 fills). Incoming bytes are already
// serialized PbMessages; we write a gzipped binary stream where each
// record is:
//
//     [8 bytes  recv_ts_ns      little-endian]
//     [4 bytes  pb_payload_len  little-endian]
//     [pb_payload_len bytes     serialized PbMessage]
//
// This mode is selected by `--hlnode` alongside `--market Hyperliquid`.
// Books and fills are run as two separate processes (one --node-endpoint
// each, distinct --outpath each) — matches the one-file-per-stream
// pattern of the existing markets.
int recv_messages_hyperliquid_node(std::string endpoint,
                                   int64_t end_ts,
                                   std::string outpath,
                                   std::set<std::string> syms_filter) {
  zmq::context_t ctx{1};
  zmq::socket_t sub(ctx, zmq::socket_type::sub);
  sub.set(zmq::sockopt::subscribe, "");
  try {
    sub.connect(endpoint);
  } catch (const std::exception& e) {
    LOG(ERROR) << "Failed to connect SUB to " << endpoint << ": " << e.what();
    return 1;
  }
  LOG(INFO) << "SUB connected to " << endpoint;

  // Append mode so a restart in the same day extends the same capture
  // file (matches the existing markets' std::ios_base::app).
  gzFile gz = gzopen(outpath.c_str(), "ab");
  if (gz == nullptr) {
    LOG(ERROR) << "Failed to gzopen " << outpath << ": " << std::strerror(errno);
    return 1;
  }
  // Bigger internal buffer than zlib's 8 KB default — fewer syscalls on
  // high-rate streams.
  gzbuffer(gz, 256 * 1024);
  LOG(INFO) << "Writing gzipped pbf stream to " << outpath;
  if (!syms_filter.empty()) {
    LOG(INFO) << "syms filter active (" << syms_filter.size() << " syms): "
              << fmt::format("{}", fmt::join(syms_filter, ","));
  }

  int64_t count = 0;
  int64_t skipped = 0;
  int64_t last_flush_count = 0;
  // Periodic Z_SYNC_FLUSH so an in-progress capture is partially readable
  // by downstream tooling even mid-day. Every ~10k messages.
  constexpr int64_t kFlushEvery = 10'000;

  while (!interrupted) {
    zmq::message_t msg;
    zmq::pollitem_t items[] = {{static_cast<void*>(sub), 0, ZMQ_POLLIN, 0}};
    zmq::poll(items, 1, std::chrono::milliseconds(500));

    int64_t now_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         std::chrono::system_clock::now().time_since_epoch())
                         .count();
    if (now_ns > end_ts) {
      LOG(INFO) << "Stopping because past end ts";
      break;
    }
    if (!(items[0].revents & ZMQ_POLLIN)) continue;

    auto rc = sub.recv(msg, zmq::recv_flags::none);
    if (!rc) continue;

    // Sym filter (if active): parse the PbMessage just to read symbol_id
    // and skip if it's not in the watch list. Parse cost is ~µs per msg;
    // the savings in disk write are massive when filtering down from
    // ~444 syms to a focused subset.
    if (!syms_filter.empty()) {
      pktrade::mdmsg::PbMessage pb;
      if (!pb.ParseFromArray(msg.data(), msg.size())) {
        // Malformed; skip without counting as written.
        continue;
      }
      if (!syms_filter.count(pb.symbol_id())) {
        ++skipped;
        continue;
      }
    }

    // Refresh recv_ts now that we have the message; the poll could have
    // returned earlier and we want recv_ts to reflect when the bytes
    // actually arrived to us.
    now_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                 std::chrono::system_clock::now().time_since_epoch())
                 .count();

    uint64_t ts_le = htole64(static_cast<uint64_t>(now_ns));
    uint32_t len_le = htole32(static_cast<uint32_t>(msg.size()));

    if (gzwrite(gz, &ts_le, sizeof(ts_le)) != sizeof(ts_le) ||
        gzwrite(gz, &len_le, sizeof(len_le)) != sizeof(len_le) ||
        gzwrite(gz, msg.data(), msg.size()) != static_cast<int>(msg.size())) {
      LOG(ERROR) << "gzwrite failed; aborting capture";
      break;
    }

    ++count;
    if (count - last_flush_count >= kFlushEvery) {
      gzflush(gz, Z_SYNC_FLUSH);
      last_flush_count = count;
      LOG(INFO) << "Captured " << count << " messages (skipped " << skipped
                << " by sym filter)";
    }
  }

  gzflush(gz, Z_FINISH);
  gzclose(gz);
  LOG(INFO) << "Finished. " << count << " messages written to " << outpath
            << " (skipped " << skipped << " by sym filter)";
  return 0;
}

///////////////////////////////////////////////////////////////
// Bybit
int recv_messages_bybit(std::vector<std::string> syms,
                        int64_t end_ts,
                        int ping_interval,
                        bool deflate,
                        pktrade::Market market,
                        std::string outpath) {
  bool should_reconnect = true;

  // Create the ws
  ix::WebSocket* ws = new ix::WebSocket();

  // Used downstairs in the request part.
  std::vector<std::string> wanted_streams;

  // https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
  std::string wss_tgt;
  if (market == pktrade::Market::BybitSPOT) {
    wss_tgt = "wss://stream.bybit.com/v5/public/spot";
    wanted_streams = {"publicTrade", "orderbook.1", "orderbook.50",
                      "orderbook.200"};
  } else if (market == pktrade::Market::BybitDeriv) {
    wss_tgt = "wss://stream.bybit.com/v5/public/linear";
    wanted_streams = {"publicTrade", "orderbook.1", "orderbook.50",
                      "orderbook.200", "orderbook.500"};
  } else if (market == pktrade::Market::BybitInverseDeriv) {
    wss_tgt = "wss://stream.bybit.com/v5/public/inverse";
    wanted_streams = {"publicTrade", "orderbook.1", "orderbook.50",
                      "orderbook.200", "orderbook.500"};
  }

  // As advised by bybit, send a heartbeat every 20s.
  // https://bybit-exchange.github.io/docs/v5/ws/connect#how-to-send-the-heartbeat-packet
  int64_t heartbeat_interval_t = 20 * 1000;
  int64_t last_heartbeat_t = 0;

  ws->setUrl(wss_tgt);

  if (deflate) {
    ws->enablePerMessageDeflate();
  } else {
    ws->disablePerMessageDeflate();
  }

  ix::SocketTLSOptions tlsOptions;
  tlsOptions.caFile = "NONE";
  ws->setTLSOptions(tlsOptions);

  ws->setPingInterval(ping_interval);
  ws->enableAutomaticReconnection();  // this is enabled by default but let's
                                      // make it explicit
  ws->setMinWaitBetweenReconnectionRetries(1);  // Set min wait for reconnect to
                                                // 1ms for faster reconnect

  bool stop_ws = false;
  // Write tgt.
  std::ofstream out_file(outpath, std::ios_base::app);
  if (!out_file.is_open()) {
    std::cerr << "Failed to open outfile: " << std::strerror(errno)
              << std::endl;
    return 1;
  }

  // std::vector<TimestampedMessage_t> msgs;
  // msgs.reserve(batch_size);
  int64_t count = 0;

  // I need to send a periodicping too.

  ws->setOnMessageCallback([&](const ix::WebSocketMessagePtr& msg) {
    if (msg->type == ix::WebSocketMessageType::Message) {
      if (stop_ws || interrupted) {
        return;
      }
      count++;
      auto now = std::chrono::system_clock::now();

      // Convert time_point to a duration since the epoch
      auto duration_since_epoch = now.time_since_epoch();

      // Convert the duration to nanoseconds
      auto recv_ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         duration_since_epoch)
                         .count();
      out_file << recv_ts << "," << msg->str << std::endl;

      if (recv_ts > end_ts) {
        LOG(INFO) << "Stopping because past end ts";
        stop_ws = true;
        return;
      }

      // Print every so often
      if (count % 100000 == 0) {
        std::cout << "Received " << count << " messages" << std::endl;
        // maybe flush too?
      }

      // Not using the other stuff bc it's going to use Clock which isn't
      // necessarily instantiated.
      int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                           duration_since_epoch)
                           .count();
      if (now_ms - last_heartbeat_t > heartbeat_interval_t) {
        // Perform heartbeat, update last_heartbeat_t.
        nlohmann::json hb_json = {{"op", "ping"}};
        ws->send(hb_json.dump());
        last_heartbeat_t = now_ms;
      }

    } else if (msg->type == ix::WebSocketMessageType::Open) {
      LOG(INFO) << "Connection established to " << msg->openInfo.uri;

      std::cout << " Connection established, sending request" << std::endl;
      // After we start, we need to send it subscriptions.

      // From docs:
      // https://bybit-exchange.github.io/docs/v5/ws/connect
      // Spot can input up to 10 args for each subscription request sent to one
      // connection

      // So, let's

      // Do this for each symbol actually.
      for (std::string& sym : syms) {
        nlohmann::json sub_json;
        std::vector<std::string> sub_streams;

        // Things we listen to:
        // Trades
        // depth-1 book
        // depth-50 book
        // depth-200 book
        // depth-500, if a deriv book.
        for (auto one_stream : wanted_streams) {
          sub_streams.push_back(one_stream + "." + sym);
        }
        sub_json = {{"op", "subscribe"}, {"args", sub_streams}};
        // std::cout << " Subscribing to " << std::endl;
        // std::cout << sub_json.dump() << std::endl;

        ws->send(sub_json.dump());
        sleep(0.5);
      }

    } else if (msg->type == ix::WebSocketMessageType::Error) {
      LOG(ERROR) << "Connection error: " << msg->errorInfo.reason;
    } else if (msg->type == ix::WebSocketMessageType::Close) {
      LOG(INFO) << "Connection closed (Code: " << msg->closeInfo.code
                << " Reason: " << msg->closeInfo.reason << ")";
    } else if (msg->type == ix::WebSocketMessageType::Ping) {
      LOG(INFO) << "Got ping";
    } else if (msg->type == ix::WebSocketMessageType::Pong) {
      LOG_EVERY_N(INFO, 100) << "Got pong";
    } else if (msg->type == ix::WebSocketMessageType::Fragment) {
      LOG(INFO) << "Got fragment";
    } else {
      LOG(ERROR) << "Unknown message type";
    }
  });
  ws->start();

  while (!stop_ws && !interrupted) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }
  ws->stop();
  LOG(INFO) << "Stopped ws receiver";
  out_file.close();

  return 0;
}

std::vector<std::string> split(const std::string& s, char delimiter) {
  std::vector<std::string> tokens;
  std::string token;
  std::istringstream tokenStream(s);

  while (std::getline(tokenStream, token, delimiter)) {
    tokens.push_back(token);
  }

  return tokens;
}

void signalHandler(int signum) { interrupted = true; }

int main(int argc, char* argv[]) {
  FLAGS_logtostderr = 1;
  google::InitGoogleLogging(argv[0]);

  std::string date_str;
  std::string syms_str;
  std::string outpath;
  int ping_interval;
  std::string market_str;
  bool deflate = false;
  std::string batch_size_str;  // Unused, take out later.

  CLI::App cmd_flags("datalog");
  cmd_flags.add_option("--date", date_str);
  cmd_flags.add_option("--syms", syms_str);
  cmd_flags.add_option("--outpath", outpath);
  cmd_flags.add_option("--ping", ping_interval);
  cmd_flags.add_option("--batch-size", batch_size_str);

  cmd_flags.add_flag("--deflate", deflate, "Enable per message deflate");
  cmd_flags.add_option("--market", market_str);

  // --hlnode: alternate path for Hyperliquid. Instead of subscribing to
  // HL's public WS, subscribe to our n1 node publisher (via ZMQ SUB) and
  // log the pre-parsed PbMessages as a gzipped binary stream. Requires
  // --market Hyperliquid and --node-endpoint. Books and fills are run as
  // separate processes (one --node-endpoint each).
  bool hlnode = false;
  std::string node_endpoint;
  cmd_flags.add_flag(
      "--hlnode", hlnode,
      "For --market Hyperliquid, use the n1 node publisher SUB path "
      "instead of HL's public WS. Output is gzipped pbf.");
  cmd_flags.add_option(
      "--node-endpoint", node_endpoint,
      "ZMQ SUB endpoint for the n1 node publisher, e.g. "
      "tcp://172.31.37.166:5555 (books) or :5556 (fills). Required with "
      "--hlnode.");

  // For --hlnode: limit captured PbMessages to a watch list of symbol_ids.
  // The publisher emits all ~444 syms HL tracks; for focused comparison
  // captures (e.g. side-by-side vs HL WS for a single coin) we usually
  // only need one or a handful, and filtering at capture time keeps disk
  // usage reasonable.
  std::string hlnode_syms_str;
  cmd_flags.add_option(
      "--hlnode-syms", hlnode_syms_str,
      "Comma-separated sym list to capture (--hlnode only). Default: all.");

  PARSE(cmd_flags, argc, argv);

  pktrade::Market market = magic_enum::enum_cast<pktrade::Market>(market_str)
                               .value_or(pktrade::Market::Unknown);

  std::istringstream iss(date_str);
  std::tm tm = {};
  iss >> std::get_time(&tm, "%Y%m%d");
  if (iss.fail()) {
    std::cerr << "Failed to parse date" << std::endl;
    return 1;
  }

  // Convert to time_point (assumes local time)
  std::chrono::system_clock::time_point tp =
      std::chrono::system_clock::from_time_t(std::mktime(&tm));

  // Convert to epochtime
  int64_t end_ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                       tp.time_since_epoch())
                       .count();

  // Add nanos in day
  end_ts += 24 * 60 * 60 * 1e9;

  std::vector<std::string> syms = split(syms_str, ',');

  // Print end ts for debugging
  LOG(INFO) << "End Ts: " << end_ts;

  signal(SIGINT, signalHandler);
  // Ignore SIGPIPE so a stale TLS write during a remote-side restart
  // surfaces as EPIPE for the WS library to handle instead of killing
  // the process silently. Without this the daily-capture wrapper sees
  // a non-zero exit and restarts us — usually fine, except for ad-hoc
  // runs (e.g. the 2026-06-13 5am HL outage) where there is no wrapper.
  signal(SIGPIPE, SIG_IGN);
  if (market == pktrade::Market::BinanceSPOT ||
      market == pktrade::Market::BinanceFutures ||
      market == pktrade::Market::BinanceCOINFutures) {
    std::thread recv_msgs_t(recv_messages_binance, syms, end_ts, ping_interval,
                            deflate, market, outpath);
    recv_msgs_t.join();
  } else if (market == pktrade::Market::Hyperliquid) {
    if (hlnode) {
      if (node_endpoint.empty()) {
        std::cerr << "--hlnode requires --node-endpoint" << std::endl;
        return 1;
      }
      // Capture via n1 node publisher SUB instead of HL public WS.
      std::set<std::string> hlnode_syms;
      if (!hlnode_syms_str.empty()) {
        for (const auto& s : split(hlnode_syms_str, ',')) {
          if (!s.empty()) hlnode_syms.insert(s);
        }
      }
      std::thread recv_msgs_t(recv_messages_hyperliquid_node, node_endpoint,
                              end_ts, outpath, hlnode_syms);
      recv_msgs_t.join();
    } else {
      // Capture via HL public WS (original path).
      std::thread recv_msgs_t(recv_messages_hyperliquid, syms, end_ts,
                              ping_interval, deflate, outpath);
      recv_msgs_t.join();
    }

  } else if (market == pktrade::Market::BybitSPOT ||
             market == pktrade::Market::BybitDeriv ||
             market == pktrade::Market::BybitInverseDeriv) {
    // Capture bybit.
    std::cout << " Starting bybit-like market" << std::endl;
    std::thread recv_msgs_t(recv_messages_bybit, syms, end_ts, ping_interval,
                            deflate, market, outpath);
    recv_msgs_t.join();
  } else {
    throw std::runtime_error("Unsupported market " + market_str);
  }

  // std::thread write_msgs_t(write_messages, std::ref(queue), outpath);

  // write_msgs_t.join();
  return 0;
}
