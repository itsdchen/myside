#include "sig_reference.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"

#include <fmt/format.h>
#include <glog/logging.h>

#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <chrono>
#include <cmath>
#include <filesystem>

namespace pktrade::signals {

namespace {
// Kalshi order-book WS path — must match exactly, it is part of the signed message.
constexpr const char* KALSHI_WS_PATH = "/trade-api/ws/v2";
constexpr const char* DEFAULT_KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2";
constexpr const char* DEFAULT_KALSHI_REST_URL = "https://external-api.kalshi.com/trade-api/v2";
constexpr const char* DEFAULT_POLY_CLOB_URL = "https://clob.polymarket.com";

// Parse an array of [price, size] pairs (Kalshi *_dollars_fp) into a price->size map.
void parseLevels(const rapidjson::Value& arr, std::map<double, double>& out) {
  out.clear();
  if (!arr.IsArray()) {
    return;
  }
  for (const auto& lvl : arr.GetArray()) {
    if (!lvl.IsArray() || lvl.Size() < 2) {
      continue;
    }
    double px = lvl[0].GetDouble();
    double sz = lvl[1].GetDouble();
    if (px < 0.0 || px > 1.0) {
      continue;
    }
    out[px] = sz;
  }
}
} // namespace

SigReference::SigReference(int signal_id, const rapidjson::Value& conf, SignalFactory* sf)
    : Signal(signal_id, conf, sf) {
  testnet_ = pktrade::GlobalVar::hl_testnet_;
  if (conf.HasMember("testnet")) {
    testnet_ = conf["testnet"].GetBool();
  }
  if (conf.HasMember("publish_interval_ms")) {
    publish_interval_ms_ = conf["publish_interval_ms"].GetInt();
  }
  if (conf.HasMember("reference_stale_after_ms")) {
    reference_stale_after_ms_ = conf["reference_stale_after_ms"].GetInt64();
  }

  // -------- Kalshi --------
  if (conf.HasMember("kalshi")) {
    const rapidjson::Value& k = conf["kalshi"];
    kalshi_enabled_ = true;
    kalshi_ticker_ = k["market_ticker"].GetString();
    kalshi_ws_url_ = k.HasMember("ws_url") ? k["ws_url"].GetString() : DEFAULT_KALSHI_WS_URL;
    kalshi_rest_url_ =
        k.HasMember("rest_url") ? k["rest_url"].GetString() : DEFAULT_KALSHI_REST_URL;
    kalshi_weight_ = k.HasMember("weight") ? k["weight"].GetDouble() : 1.0;
    kalshi_creds_file_ = k["creds_file"].GetString();
    if (k.HasMember("rest_poll_s")) {
      kalshi_rest_poll_s_ = k["rest_poll_s"].GetInt();
    }

    // Load Kalshi credentials: api_key_id (or key_id) + an RSA private key PEM path.
    rapidjson::Document creds = pktrade::util::read_json_file(kalshi_creds_file_);
    if (creds.HasMember("api_key_id")) {
      kalshi_api_key_id_ = creds["api_key_id"].GetString();
    } else if (creds.HasMember("key_id")) {
      kalshi_api_key_id_ = creds["key_id"].GetString();
    } else {
      throw std::runtime_error("Kalshi creds missing api_key_id/key_id: " + kalshi_creds_file_);
    }
    std::string pem_path;
    if (creds.HasMember("private_key_path")) {
      pem_path = creds["private_key_path"].GetString();
    } else if (creds.HasMember("private_key_file")) {
      pem_path = creds["private_key_file"].GetString();
    } else {
      throw std::runtime_error("Kalshi creds missing private_key_path: " + kalshi_creds_file_);
    }
    // Resolve a relative key path against the creds file's directory (matches hip4maker).
    std::filesystem::path kp(pem_path);
    if (!kp.is_absolute()) {
      kp = std::filesystem::path(kalshi_creds_file_).parent_path() / kp;
    }
    FILE* fp = std::fopen(kp.c_str(), "r");
    if (fp == nullptr) {
      throw std::runtime_error("Cannot open Kalshi private key PEM: " + kp.string());
    }
    kalshi_pkey_ = PEM_read_PrivateKey(fp, nullptr, nullptr, nullptr);
    std::fclose(fp);
    if (kalshi_pkey_ == nullptr) {
      throw std::runtime_error("Cannot parse Kalshi RSA private key: " + kp.string());
    }
  }

  // -------- Polymarket (REST only) --------
  if (conf.HasMember("polymarket")) {
    const rapidjson::Value& p = conf["polymarket"];
    poly_enabled_ = true;
    poly_token_id_ = p["token_id"].GetString();
    poly_clob_url_ = p.HasMember("clob_url") ? p["clob_url"].GetString() : DEFAULT_POLY_CLOB_URL;
    poly_weight_ = p.HasMember("weight") ? p["weight"].GetDouble() : 1.0;
    if (p.HasMember("poll_interval_s")) {
      poly_poll_s_ = p["poll_interval_s"].GetInt();
    }
  }

  // -------- Optional tie adjustment --------
  if (conf.HasMember("tie")) {
    const rapidjson::Value& t = conf["tie"];
    tie_enabled_ = true;
    tie_ticker_ = t["market_ticker"].GetString();
    tie_fraction_ = t.HasMember("settlement_fraction") ? t["settlement_fraction"].GetDouble() : 0.5;
  }
}

SigReference::~SigReference() {
  kalshi_ws_.stop();
  if (kalshi_pkey_ != nullptr) {
    EVP_PKEY_free(kalshi_pkey_);
    kalshi_pkey_ = nullptr;
  }
}

bool SigReference::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                          const rapidjson::Value& all_signals_array) const {
  // Each external subscription is its own live resource; only treat two configs as the same
  // signal if they share the name (dedup by name), never merge distinct Kalshi tickers.
  return std::string(other_conf["name"].GetString()) == name_;
}

void SigReference::reset() {
  std::lock_guard<std::mutex> lk(mu_);
  kalshi_yes_levels_.clear();
  kalshi_no_levels_.clear();
  kalshi_last_seq_ = -1;
  kalshi_ready_ = false;
  comp_valid_ = false;
  setValidity(false);
}

int64_t SigReference::nowMs() const {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

void SigReference::subscribeData(MDBeacon* /*beacon*/) {
  // We don't use the beacon. In sim / non-live runs there is no external reference feed, so
  // the signal simply stays invalid. In live mode, schedule the connections to start at the
  // trading start time (mirrors HedgePositioner).
  if (!pktrade::GlobalVar::live_) {
    return;
  }
  auto now = clock_cast<Clock>(std::chrono::system_clock::now());
  if (now > pktrade::GlobalVar::start_t_) {
    pktrade::GlobalVar::event_loop_->onTimeout(now + std::chrono::seconds(1),
                                               [this] { this->startConnections(); });
  } else {
    pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_,
                                               [this] { this->startConnections(); });
  }
}

void SigReference::startConnections() {
  if (started_) {
    return;
  }
  started_ = true;
  LOG(INFO) << fmt::format("SigReference[{}]: starting (kalshi={} poly={} tie={})", name_,
                           kalshi_enabled_, poly_enabled_, tie_enabled_);
  if (kalshi_enabled_) {
    connectKalshiWs();
    // REST fallback poll (also used until the WS book is ready).
    pollKalshiRest();
  }
  if (poly_enabled_) {
    pollPolymarket();
  }
  if (tie_enabled_) {
    pollTie();
  }
  schedulePublish();
}

void SigReference::schedulePublish() {
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::milliseconds(publish_interval_ms_),
                                             [this] { this->publishTick(); });
}

void SigReference::publishTick() {
  double mid;
  bool valid;
  {
    std::lock_guard<std::mutex> lk(mu_);
    // Staleness: invalidate if we haven't had a composite update recently.
    if (comp_valid_ && (nowMs() - comp_last_update_ms_) > reference_stale_after_ms_) {
      comp_valid_ = false;
    }
    mid = 0.5 * (comp_bid_ + comp_ask_);
    valid = comp_valid_;
  }
  // setVV runs listener callbacks here on the event-loop thread.
  setVV(mid, valid);
  schedulePublish();
}

// ---------------------------------------------------------------------------
// Kalshi WebSocket
// ---------------------------------------------------------------------------

std::vector<std::string> SigReference::kalshiAuthHeaders() const {
  // message = "{timestamp_ms}GET{path}", signed RSA-PSS(MGF1(SHA256), salt=digest len), base64.
  int64_t ts = nowMs();
  std::string ts_str = std::to_string(ts);
  std::string message = ts_str + "GET" + KALSHI_WS_PATH;

  std::string sig_b64;
  EVP_MD_CTX* ctx = EVP_MD_CTX_new();
  EVP_PKEY_CTX* pctx = nullptr;
  if (ctx != nullptr &&
      EVP_DigestSignInit(ctx, &pctx, EVP_sha256(), nullptr, kalshi_pkey_) == 1 &&
      EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) == 1 &&
      EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) == 1) {
    size_t siglen = 0;
    const auto* msg = reinterpret_cast<const unsigned char*>(message.data());
    if (EVP_DigestSign(ctx, nullptr, &siglen, msg, message.size()) == 1) {
      std::vector<unsigned char> sig(siglen);
      if (EVP_DigestSign(ctx, sig.data(), &siglen, msg, message.size()) == 1) {
        sig_b64 = base64Encode(sig.data(), siglen);
      }
    }
  }
  if (ctx != nullptr) {
    EVP_MD_CTX_free(ctx);
  }
  if (sig_b64.empty()) {
    LOG(ERROR) << fmt::format("SigReference[{}]: failed to sign Kalshi auth headers", name_);
  }

  return {
      "KALSHI-ACCESS-KEY: " + kalshi_api_key_id_,
      "KALSHI-ACCESS-SIGNATURE: " + sig_b64,
      "KALSHI-ACCESS-TIMESTAMP: " + ts_str,
  };
}

void SigReference::connectKalshiWs() {
  // We manage reconnection ourselves so every connect re-signs a fresh timestamp (Kalshi
  // rejects a stale signature). ixwebsocket auto-reconnect would reuse the old headers.
  kalshi_ws_.stop();
  kalshi_ws_.disableAutomaticReconnection();
  kalshi_ws_.setUrl(kalshi_ws_url_);

  ix::WebSocketHttpHeaders headers;
  for (const std::string& h : kalshiAuthHeaders()) {
    auto colon = h.find(": ");
    if (colon != std::string::npos) {
      headers[h.substr(0, colon)] = h.substr(colon + 2);
    }
  }
  kalshi_ws_.setExtraHeaders(headers);

  kalshi_ws_.setOnMessageCallback([this](const ix::WebSocketMessagePtr& msg) {
    if (msg->type == ix::WebSocketMessageType::Open) {
      // Subscribe to the orderbook_delta channel for our ticker (YES-priced).
      std::string sub = std::string("{\"id\":1,\"cmd\":\"subscribe\",\"params\":{") +
                        "\"channels\":[\"orderbook_delta\"],\"market_ticker\":\"" +
                        kalshi_ticker_ + "\",\"use_yes_price\":true}}";
      kalshi_ws_.send(sub);
      LOG(INFO) << fmt::format("SigReference[{}]: Kalshi WS open, subscribed {}", name_,
                               kalshi_ticker_);
    } else if (msg->type == ix::WebSocketMessageType::Message) {
      onKalshiMessage(msg->str);
    } else if (msg->type == ix::WebSocketMessageType::Error ||
               msg->type == ix::WebSocketMessageType::Close) {
      invalidateKalshi("ws closed/errored");
      // Reconnect with backoff, re-signing headers, on the event-loop thread.
      pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(1),
                                                 [this] { this->connectKalshiWs(); });
    }
  });
  kalshi_ws_.start();
}

void SigReference::invalidateKalshi(const std::string& reason) {
  std::lock_guard<std::mutex> lk(mu_);
  kalshi_yes_levels_.clear();
  kalshi_no_levels_.clear();
  kalshi_last_seq_ = -1;
  kalshi_ready_ = false;
  kalshi_book_.valid = false;
  recomputeComposite();
  VLOG(1) << fmt::format("SigReference[{}]: Kalshi invalidated: {}", name_, reason);
}

void SigReference::onKalshiMessage(const std::string& raw) {
  rapidjson::Document msg;
  if (msg.Parse(raw.c_str()).HasParseError() || !msg.IsObject()) {
    return;
  }
  if (!msg.HasMember("type")) {
    return;
  }
  std::string type = msg["type"].GetString();
  if (type == "subscribed") {
    return;
  }
  if (type == "error") {
    invalidateKalshi("subscription error");
    return;
  }
  int64_t seq = msg.HasMember("seq") ? msg["seq"].GetInt64() : -1;
  if (type == "orderbook_snapshot" && msg.HasMember("msg")) {
    applyKalshiSnapshot(msg["msg"], seq);
  } else if (type == "orderbook_delta" && msg.HasMember("msg")) {
    applyKalshiDelta(msg["msg"], seq);
  }
}

void SigReference::applyKalshiSnapshot(const rapidjson::Value& m, int64_t seq) {
  if (!m.IsObject() || !m.HasMember("market_ticker") ||
      kalshi_ticker_ != m["market_ticker"].GetString()) {
    invalidateKalshi("snapshot ticker mismatch");
    return;
  }
  std::lock_guard<std::mutex> lk(mu_);
  if (m.HasMember("yes_dollars_fp")) {
    parseLevels(m["yes_dollars_fp"], kalshi_yes_levels_);
  }
  if (m.HasMember("no_dollars_fp")) {
    parseLevels(m["no_dollars_fp"], kalshi_no_levels_);
  }
  kalshi_last_seq_ = seq;
  kalshi_ready_ = true;
  recomputeKalshiFromLevels(nowMs());
  recomputeComposite();
}

void SigReference::applyKalshiDelta(const rapidjson::Value& m, int64_t seq) {
  if (!m.IsObject() || !m.HasMember("market_ticker") ||
      kalshi_ticker_ != m["market_ticker"].GetString()) {
    invalidateKalshi("delta ticker mismatch");
    return;
  }
  std::lock_guard<std::mutex> lk(mu_);
  // Strict sequencing: any gap forces a resnapshot (invalidate).
  if (kalshi_last_seq_ < 0 || seq != kalshi_last_seq_ + 1) {
    int64_t had_seq = kalshi_last_seq_;
    // Release the lock before invalidate (which relocks) by clearing inline instead.
    kalshi_yes_levels_.clear();
    kalshi_no_levels_.clear();
    kalshi_last_seq_ = -1;
    kalshi_ready_ = false;
    kalshi_book_.valid = false;
    recomputeComposite();
    VLOG(1) << fmt::format("SigReference[{}]: Kalshi seq gap (have {}, got {})", name_, had_seq,
                           seq);
    return;
  }

  std::string side;
  if (m.HasMember("side")) {
    side = m["side"].GetString();
  } else if (m.HasMember("outcome_side")) {
    side = m["outcome_side"].GetString();
  }
  if (side != "yes" && side != "no") {
    return;
  }
  double price = m.HasMember("price_dollars") ? m["price_dollars"].GetDouble() : -1.0;
  double delta = m.HasMember("delta_fp") ? m["delta_fp"].GetDouble() : 0.0;
  if (price < 0.0 || price > 1.0) {
    return;
  }
  std::map<double, double>& levels = (side == "yes") ? kalshi_yes_levels_ : kalshi_no_levels_;
  double updated = levels[price] + delta;  // operator[] default-inserts 0
  if (updated < 0.0) {
    // Level went negative — book is corrupt, resnapshot.
    kalshi_yes_levels_.clear();
    kalshi_no_levels_.clear();
    kalshi_last_seq_ = -1;
    kalshi_ready_ = false;
    kalshi_book_.valid = false;
    recomputeComposite();
    return;
  }
  if (updated == 0.0) {
    levels.erase(price);
  } else {
    levels[price] = updated;
  }
  kalshi_last_seq_ = seq;
  int64_t src = m.HasMember("ts_ms") ? m["ts_ms"].GetInt64() : nowMs();
  recomputeKalshiFromLevels(src);
  recomputeComposite();
}

void SigReference::recomputeKalshiFromLevels(int64_t src_ts_ms) {
  // Caller holds mu_. best_bid = max(yes_levels), best_ask = min(no_levels).
  if (kalshi_yes_levels_.empty() && kalshi_no_levels_.empty()) {
    kalshi_book_.valid = false;
    return;
  }
  double bid = kalshi_yes_levels_.empty() ? 0.0 : kalshi_yes_levels_.rbegin()->first;
  double ask = kalshi_no_levels_.empty() ? 1.0 : kalshi_no_levels_.begin()->first;
  kalshi_book_.bid = bid;
  kalshi_book_.ask = ask;
  kalshi_book_.bid_size = kalshi_yes_levels_.empty() ? 0.0 : kalshi_yes_levels_.rbegin()->second;
  kalshi_book_.ask_size = kalshi_no_levels_.empty() ? 0.0 : kalshi_no_levels_.begin()->second;
  kalshi_book_.source_ts_ms = src_ts_ms;
  kalshi_book_.recv_ts_ms = nowMs();
  kalshi_book_.weight = kalshi_weight_;
  kalshi_book_.valid = (bid > 0.0 && ask < 1.0 && bid < ask);
}

// ---------------------------------------------------------------------------
// REST pollers (cpr async; re-armed on the event loop)
// ---------------------------------------------------------------------------

void SigReference::pollKalshiRest() {
  // Only needed until the WS book is ready; keep polling as a cheap fallback.
  bool need_rest;
  {
    std::lock_guard<std::mutex> lk(mu_);
    need_rest = !kalshi_ready_;
  }
  if (need_rest) {
    std::string url = kalshi_rest_url_ + "/markets/" + kalshi_ticker_ + "/orderbook";
    cpr::GetCallback(
        [this](cpr::Response r) {
          if (r.status_code != 200) {
            return;
          }
          rapidjson::Document doc;
          if (doc.Parse(r.text.c_str()).HasParseError() || !doc.IsObject()) {
            return;
          }
          if (!doc.HasMember("orderbook_fp")) {
            return;
          }
          const rapidjson::Value& ob = doc["orderbook_fp"];
          std::map<double, double> yes, no;
          if (ob.HasMember("yes_dollars")) {
            parseLevels(ob["yes_dollars"], yes);
          }
          if (ob.HasMember("no_dollars")) {
            parseLevels(ob["no_dollars"], no);
          }
          std::lock_guard<std::mutex> lk(mu_);
          if (kalshi_ready_) {
            return;  // WS took over; ignore REST.
          }
          double bid = yes.empty() ? 0.0 : yes.rbegin()->first;
          double ask = no.empty() ? 1.0 : no.begin()->first;
          kalshi_book_.bid = bid;
          kalshi_book_.ask = ask;
          kalshi_book_.bid_size = yes.empty() ? 0.0 : yes.rbegin()->second;
          kalshi_book_.ask_size = no.empty() ? 0.0 : no.begin()->second;
          kalshi_book_.source_ts_ms = nowMs();
          kalshi_book_.recv_ts_ms = nowMs();
          kalshi_book_.weight = kalshi_weight_;
          kalshi_book_.valid = (bid > 0.0 && ask < 1.0 && bid < ask);
          recomputeComposite();
        },
        cpr::Url{url});
  }
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(kalshi_rest_poll_s_),
                                             [this] { this->pollKalshiRest(); });
}

void SigReference::pollPolymarket() {
  std::string url = poly_clob_url_ + "/book?token_id=" + poly_token_id_;
  cpr::GetCallback(
      [this](cpr::Response r) {
        if (r.status_code != 200) {
          return;
        }
        rapidjson::Document doc;
        if (doc.Parse(r.text.c_str()).HasParseError() || !doc.IsObject()) {
          return;
        }
        // best bid = highest bid price; best ask = lowest ask price.
        double best_bid = 0.0, best_bid_sz = 0.0;
        double best_ask = 1.0, best_ask_sz = 0.0;
        if (doc.HasMember("bids") && doc["bids"].IsArray()) {
          for (const auto& b : doc["bids"].GetArray()) {
            double px = std::stod(b["price"].GetString());
            if (px > best_bid) {
              best_bid = px;
              best_bid_sz = std::stod(b["size"].GetString());
            }
          }
        }
        if (doc.HasMember("asks") && doc["asks"].IsArray()) {
          for (const auto& a : doc["asks"].GetArray()) {
            double px = std::stod(a["price"].GetString());
            if (px < best_ask) {
              best_ask = px;
              best_ask_sz = std::stod(a["size"].GetString());
            }
          }
        }
        std::lock_guard<std::mutex> lk(mu_);
        poly_book_.bid = best_bid;
        poly_book_.ask = best_ask;
        poly_book_.bid_size = best_bid_sz;
        poly_book_.ask_size = best_ask_sz;
        poly_book_.source_ts_ms = nowMs();
        poly_book_.recv_ts_ms = nowMs();
        poly_book_.weight = poly_weight_;
        poly_book_.valid = (best_bid > 0.0 && best_ask < 1.0 && best_bid < best_ask);
        recomputeComposite();
      },
      cpr::Url{url});
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(poly_poll_s_),
                                             [this] { this->pollPolymarket(); });
}

void SigReference::pollTie() {
  std::string url = kalshi_rest_url_ + "/markets/" + tie_ticker_ + "/orderbook";
  cpr::GetCallback(
      [this](cpr::Response r) {
        if (r.status_code != 200) {
          return;
        }
        rapidjson::Document doc;
        if (doc.Parse(r.text.c_str()).HasParseError() || !doc.IsObject() ||
            !doc.HasMember("orderbook_fp")) {
          return;
        }
        const rapidjson::Value& ob = doc["orderbook_fp"];
        std::map<double, double> yes, no;
        if (ob.HasMember("yes_dollars")) {
          parseLevels(ob["yes_dollars"], yes);
        }
        if (ob.HasMember("no_dollars")) {
          parseLevels(ob["no_dollars"], no);
        }
        double bid = yes.empty() ? 0.0 : yes.rbegin()->first;
        double ask = no.empty() ? 1.0 : no.begin()->first;
        std::lock_guard<std::mutex> lk(mu_);
        tie_mid_ = 0.5 * (bid + ask);
        tie_have_ = (bid > 0.0 && ask < 1.0 && bid < ask);
        recomputeComposite();
      },
      cpr::Url{url});
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(kalshi_rest_poll_s_),
                                             [this] { this->pollTie(); });
}

// ---------------------------------------------------------------------------
// Compose
// ---------------------------------------------------------------------------

void SigReference::recomputeComposite() {
  // Caller holds mu_. Weighted average of each valid venue's bid/ask (references.py combine),
  // then an optional tie shift.
  double wsum = 0.0, bid_acc = 0.0, ask_acc = 0.0, bsz_acc = 0.0, asz_acc = 0.0;
  auto add = [&](const RefVenueBook& b) {
    if (!b.valid || b.weight <= 0.0) {
      return;
    }
    wsum += b.weight;
    bid_acc += b.weight * b.bid;
    ask_acc += b.weight * b.ask;
    bsz_acc += b.weight * b.bid_size;
    asz_acc += b.weight * b.ask_size;
  };
  add(kalshi_book_);
  add(poly_book_);

  if (wsum <= 0.0) {
    comp_valid_ = false;
    return;
  }
  double bid = bid_acc / wsum;
  double ask = ask_acc / wsum;

  // Tie shift: move the whole book up by tie_fraction_ * tie_mid_ (draw settlement bucket).
  if (tie_enabled_ && tie_have_) {
    double shift = tie_fraction_ * tie_mid_;
    bid = std::min(1.0, bid + shift);
    ask = std::min(1.0, ask + shift);
  }

  comp_bid_ = bid;
  comp_ask_ = ask;
  comp_bid_size_ = bsz_acc / wsum;
  comp_ask_size_ = asz_acc / wsum;
  comp_last_update_ms_ = nowMs();
  comp_valid_ = (bid > 0.0 && ask < 1.0 && bid < ask);
}

// ---------------------------------------------------------------------------
// Book-like measures (thread-safe reads)
// ---------------------------------------------------------------------------

double SigReference::getBBMeasure() {
  std::lock_guard<std::mutex> lk(mu_);
  return comp_bid_;
}
double SigReference::getBAMeasure() {
  std::lock_guard<std::mutex> lk(mu_);
  return comp_ask_;
}
double SigReference::getBBSize() {
  std::lock_guard<std::mutex> lk(mu_);
  return comp_bid_size_;
}
double SigReference::getBASize() {
  std::lock_guard<std::mutex> lk(mu_);
  return comp_ask_size_;
}

// ---------------------------------------------------------------------------
// base64
// ---------------------------------------------------------------------------

std::string SigReference::base64Encode(const unsigned char* data, size_t len) {
  if (len == 0) {
    return "";
  }
  // 4 chars per 3 bytes, plus NUL from EVP_EncodeBlock.
  std::string out(4 * ((len + 2) / 3), '\0');
  int n = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(out.data()), data,
                          static_cast<int>(len));
  out.resize(static_cast<size_t>(n));
  return out;
}

} // namespace pktrade::signals
