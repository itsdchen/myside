/*

Rewrote the pkfeed to handle multiple markets and to use a different
config scheme.

Usage:
./pkmultifeed --conf feed_config.json

*/

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <csignal>
#include <exception>
#include <fstream>
#include <functional>
#include <list>
#include <array>
#include <limits>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include <mutex>

#include "pktrade/mdapi.h"
#include "pktrade/util/cli.h"

#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <glog/logging.h>
#include <google/protobuf/message.h>

// For asking for snapshots.
#include <ixwebsocket/IXHttp.h>
#include <ixwebsocket/IXHttpClient.h>

#include <ixwebsocket/IXConnectionState.h>
#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXUserAgent.h>
#include <ixwebsocket/IXWebSocket.h>

#include <magic_enum.hpp>

#include <nlohmann/json.hpp>
#include <rapidjson/document.h>
#include <rapidjson/istreamwrapper.h>

#include <zmq.hpp>

#include <cpr/cpr.h>

#include "pktrade/decoder.h"

#include "mdmsg.pb.h"

// For doing ip address lookup
#include <arpa/inet.h>
#include <cstring>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include "pktrade/feedrelay/pkrelay_packet.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/symbolizer.h"

// Databento SDK
#include <databento/constants.hpp>
#include <databento/enums.hpp>
#include <databento/flag_set.hpp>
#include <databento/live.hpp>
#include <databento/live_threaded.hpp>
#include <databento/record.hpp>
#include <databento/with_ts_out.hpp>

namespace {

template <typename Msg>
struct DbRecordView {
  const Msg* msg = nullptr;
  bool has_ts_out = false;
  int64_t ts_out_ms = 0;
};

template <typename Msg>
DbRecordView<Msg> extractRecordView(const databento::Record& record) {
  DbRecordView<Msg> view;
  if (const auto* with_ts = record.GetIf<databento::WithTsOut<Msg>>()) {
    view.msg = &with_ts->rec;
    const auto ts_out_raw = with_ts->ts_out.time_since_epoch().count();
    if (ts_out_raw != databento::kUndefTimestamp) {
      view.has_ts_out = true;
      view.ts_out_ms = static_cast<int64_t>(ts_out_raw / 1000 / 1000);
    }
    return view;
  }
  if (const auto* plain = record.GetIf<Msg>()) {
    view.msg = plain;
  }
  return view;
}

} // namespace

namespace pktrade {

static std::atomic<bool> g_shutdown{false};
static std::atomic<int64_t> g_eod_secs{0};
// Set once main() has finished tearing everything down. The shutdown watchdog
// watches this to distinguish a clean (if slightly slow) teardown from a hang.
static std::atomic<bool> g_teardown_done{false};

// Shared state between the HyperliquidNode SUB relay (which consumes the
// pre-parsed PB feed from the n1 publisher) and the Hyperliquid WS path.
// Both paths arbitrate publishing per-message via the per-(sym, chain_time)
// gate — first source to claim a chain_time slot wins, the other is
// dropped. The `node_healthy` flag is informational only (drives the
// one-shot unhealthy alert). Mirrors pymultifeed's hl_node_* state.
struct HLNodeShared {
  std::atomic<bool> configured{false};
  // Health-state indicator only. Phase 2 (2026-06-26) removed this as a
  // publish gate — every node/WS path now arbitrates per-message via the
  // per-(sym, chain_time) gate alone. Kept for the one-shot alert email
  // and freshness-window log so we can still track when the node feed
  // becomes silent/lagging vs healthy.
  std::atomic<bool> node_healthy{false};
  std::atomic<int64_t> last_msg_wall_ms{0};   // wall-clock ms of latest SUB msg
  std::atomic<int64_t> last_event_time_ms{0}; // chain time of latest node book snapshot
                                              // (also used as the node side of book freshness)
  // Freshness markers: chain time of the most recent event seen on each
  // path. Diff (node - ws) is a per-tick measure of which path is delivering
  // events more current to chain time. Positive = node ahead. Updated on
  // every arrival from each source; periodic logger reads them.
  std::atomic<int64_t> last_node_trade_time_ms{0};
  std::atomic<int64_t> last_ws_trade_time_ms{0};
  // Book event_time freshness — mirror pattern of trades. Node side is
  // already tracked above via last_event_time_ms; the WS side is the
  // event_time of the most recent l2Book snapshot received.
  std::atomic<int64_t> last_ws_book_event_time_ms{0};
  bool ws_fallback_present = false;           // is "Hyperliquid" (WS) also configured?
  // Lowered from 5.0 s on 2026-06-26 after the n2 variant study showed the
  // every-2000-block slow-apply event produces ~285 fast-pipeline-gap >1 s
  // events per day, only 31 of which crossed the old 5 s threshold. The
  // public WS is sourced from validators and doesn't experience these
  // stalls, so 1.5 s catches ~90% of daily silence events while staying
  // above ZMQ-jitter and GC-pause noise. The thresholds now drive only the
  // one-shot unhealthy alert email — they no longer gate publishing. See
  // overmind/studies/hl_feed/consumer_freshness_plan_20260626.md.
  double fallback_silent_s = 1.5;
  double fallback_lag_s = 1.5;
  // Set on the first unhealthy detection (with WS fallback present); the
  // health thread uses exchange(true) to send the alert exactly once per
  // process run. Subsequent flips just update the publish flag silently.
  std::atomic<bool> unhealthy_alert_sent{false};

  std::mutex gate_mtx;
  // Last Hyperliquid book actually published per sym, recording both the
  // chain time and which source emitted it. Same-source equal event_time is
  // allowed because the WS fast/slow merge can produce more than one useful
  // merged snapshot at a single chain time; cross-source equal event_time is
  // dropped as a duplicate.
  enum class HLBookSource : uint8_t { None = 0, WS = 1, Node = 2 };
  struct LastPublishedBook {
    int64_t t = 0;
    HLBookSource source = HLBookSource::None;
  };
  std::map<std::string, LastPublishedBook> last_published_book_by_sym;
  // Last Hyperliquid trade actually published per sym, recording both the
  // chain time and which source emitted it. Within a single trade_time, we
  // trust one source — either both feeds see the same on-chain set, or the
  // primary already drained it. Cross-source duplicates around a primary
  // flip are skipped because the source recorded on last_t doesn't match.
  // Same-source distinct trades at the same trade_time all publish because
  // arrival order from the same source is monotonic.
  enum class HLTradeSource : uint8_t { None = 0, WS = 1, Node = 2 };
  struct LastPublishedTrade {
    int64_t t = 0;
    HLTradeSource source = HLTradeSource::None;
  };
  std::map<std::string, LastPublishedTrade> last_published_trade_by_sym;
};

static HLNodeShared g_hl_node;

// ---------------------------------------------------------------------------
// Intraday config reload — add-only symbol subscriptions.
//
// pkmultifeed runs continuously for a full trade day. Occasionally a symbol is
// added to the feed conf intraday (typically DataBentoEquities, Hyperliquid, or
// HyperliquidNode) that we want to start publishing without restarting the
// process. The main loop re-reads the conf every kConfigReloadIntervalSec (when
// its mtime changes), records any genuinely-new symbols below, and nudges the
// affected feed to resubscribe. Add-only: symbols are never removed here — a
// removal still needs a restart.
//
// IMPORTANT: the reload parses a *throwaway* rapidjson::Document. main()'s live
// config_doc must never be re-parsed in place — the Hyperliquid WS Open handler
// (and other subscribe paths) hold references into it that would dangle. Extra
// symbols flow through the owned std::string structures below instead, so
// config_doc stays immutable for the life of the process.
constexpr int64_t kConfigReloadIntervalSec = 600;

// Extra symbols added intraday, keyed by conf market name ("Hyperliquid",
// "DataBentoEquities", "DataBentoBoats", "DataBentoCME", "RelayDBCME").
// Consulted by each feed's (re)subscribe path. Normally empty. HyperliquidNode
// is handled via the swappable combined filter below instead, since its relay
// filters every message rather than subscribing.
static std::mutex g_extra_syms_mtx;
static std::map<std::string, std::vector<std::string>> g_extra_syms;

// Per-feed handles/state used to apply intraday adds. The Hyperliquid WS handle
// lets the reload subscribe new symbols directly on the live socket (no
// reconnect). The DataBento feeds subscribe new symbols on the live session from
// their record callback (see DbExtraSubState / drainDbExtras); a runner-side
// timeout falls back to a reconnect if the callback hasn't applied them within
// ~10s (e.g. a silent feed whose callback never fires). RelayDBCME has no
// reconnect and uses the count below instead.
static std::atomic<ix::WebSocket*> g_hl_ws{nullptr};

// Per-feed bookkeeping for live-subscribing intraday-added databento symbols.
// `count` == g_extra_syms[market] size (bumped by the reload); `applied` is how
// many have been subscribed (by the callback drain, or by a reconnect's combined
// Subscribe). count > applied means there's pending work for that feed.
struct DbExtraSubState {
  std::atomic<uint64_t> count{0};
  std::atomic<uint64_t> applied{0};
};
static DbExtraSubState g_eq_extra;
static DbExtraSubState g_boats_extra;
static DbExtraSubState g_cme_extra;
// RelayDBCME has no reconnect — its single UDP receiver thread applies extras
// itself at a safe point. This monotonic count (== g_extra_syms["RelayDBCME"]
// size) lets the hot recv loop cheaply detect pending additions without locking
// per packet; it only takes the lock when the count exceeds what it has applied.
static std::atomic<uint64_t> g_relay_cme_extra_count{0};

// HyperliquidNode combined symbol filter (base ∪ intraday-added), swapped
// wholesale on add. An empty/null set means accept-all, matching the original
// per-thread sym_filter semantics. Seeded in subscribeHyperliquidNode.
//
// Each relay thread caches the filter locally and only re-reads it (under the
// mutex) when g_hl_node_filter_version moves — so the steady-state per-message
// cost is a single atomic load + compare, no lock. A monotonic counter (rather
// than a bool) is required because both relay threads consume version bumps
// independently. The version is bumped after the pointer is stored.
static std::mutex g_hl_node_filter_mtx;
static std::shared_ptr<const std::unordered_set<std::string>> g_hl_node_filter;
static std::atomic<uint64_t> g_hl_node_filter_version{0};

// HL node relay threads. These own SUB sockets created on main's zmq context, so
// they MUST be joined before ctx.close(): zmq_ctx_term blocks until every socket
// on the context is closed, with no timeout. They used to be detached, which left
// shutdown racing them — a straggler holding an open SUB socket hangs the process
// forever (suspected cause of the 20260717 gf3 stuck instance).
static std::mutex g_hl_node_threads_mtx;
static std::vector<std::thread> g_hl_node_threads;

// Hyperliquid WS helper threads (ping + watchdog). They capture the raw
// ix::WebSocket* and call send()/close() on it, so they MUST be joined before
// main() does `delete ws` — a detached thread that wakes after the delete
// touches freed memory. Joined ahead of the websocket teardown.
static std::mutex g_hl_ws_threads_mtx;
static std::vector<std::thread> g_hl_ws_threads;

// Sleep in short slices so a thread notices g_shutdown promptly. The HL ping and
// watchdog threads are joined at shutdown, and a plain sleep_for(25s) would stall
// teardown well past the 15s EOD buffer before the next day's process starts.
static void shutdownAwareSleep(std::chrono::milliseconds total) {
  constexpr auto kSlice = std::chrono::seconds(1);
  const auto deadline = std::chrono::steady_clock::now() + total;
  while (!g_shutdown.load()) {
    const auto now = std::chrono::steady_clock::now();
    if (now >= deadline) return;
    std::this_thread::sleep_for(
        std::min<std::chrono::milliseconds>(
            kSlice, std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now)));
  }
}

// Owned snapshot of the extra symbols recorded for a market (safe to use after
// the lock is released).
static std::vector<std::string> extraSymsFor(const std::string& market) {
  std::lock_guard<std::mutex> lk(g_extra_syms_mtx);
  auto it = g_extra_syms.find(market);
  if (it == g_extra_syms.end()) return {};
  return it->second;
}

// Markets whose symbols can be added intraday via config reload.
static bool isIntradayReloadMarket(const std::string& name) {
  return name == "Hyperliquid" || name == "DataBentoEquities" ||
         name == "DataBentoBoats" || name == "DataBentoCME" ||
         name == "RelayDBCME" || name == "HyperliquidNode";
}

// Subscribe any intraday-added symbols on a live databento session. MUST be
// called from the client's record callback — that runs on the client's internal
// thread, which is the only thread-safe place to call Subscribe (it has no
// internal locking) and to touch per-symbol feed state. `build` maps a config
// symbol to the databento symbol(s) to subscribe: identity for EQ/BOATS, or
// contract expansion + roll-state update for CME. Cheap no-op (one atomic load)
// unless the reload bumped `count`. On Subscribe failure `applied` is left
// unchanged so the runner's timeout fallback reconnects instead.
template <typename BuildFn>
void drainDbExtras(DbExtraSubState& st, databento::LiveThreaded* client,
                   const std::string& market, databento::Schema schema, BuildFn build) {
  const uint64_t count = st.count.load(std::memory_order_acquire);
  const uint64_t applied = st.applied.load(std::memory_order_relaxed);
  if (count <= applied) return;

  std::vector<std::string> extras = extraSymsFor(market);
  std::vector<std::string> to_sub;
  for (size_t i = applied; i < extras.size(); ++i) {
    for (auto& db_sym : build(extras[i])) to_sub.push_back(std::move(db_sym));
  }
  if (!to_sub.empty()) {
    try {
      client->Subscribe(to_sub, schema, databento::SType::RawSymbol);
      LOG(INFO) << market << ": live-subscribed " << to_sub.size() << " contract(s) for "
                << (extras.size() - applied) << " intraday-added symbol(s)";
    } catch (const std::exception& e) {
      LOG(ERROR) << market << ": live Subscribe failed (" << e.what()
                 << "); leaving for reconnect fallback";
      return;  // applied unchanged → runner reconnects
    }
  }
  st.applied.store(extras.size(), std::memory_order_release);
}

// Runner-side fallback for drainDbExtras: returns true once the reload's extras
// have been pending (un-live-subscribed) longer than `timeout_ms`, meaning the
// record callback isn't draining them (e.g. a silent feed) and the runner should
// reconnect to apply them via its combined Subscribe. `pending_since` is
// runner-local state carried across BlockForStop iterations (0 = not pending).
bool dbExtrasReconnectDue(const DbExtraSubState& st, int64_t& pending_since,
                          int64_t timeout_ms = 10'000) {
  if (st.count.load() <= st.applied.load()) {
    pending_since = 0;
    return false;
  }
  const int64_t now = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::system_clock::now().time_since_epoch()).count();
  if (pending_since == 0) {
    pending_since = now;
    return false;
  }
  return now - pending_since > timeout_ms;
}

static bool isPublishableMarket(mdmsg::PbMarket market) noexcept {
  switch (market) {
  case mdmsg::PBMARKET_BINANCE_SPOT:
  case mdmsg::PBMARKET_BINANCE_FUTURES:
  case mdmsg::PBMARKET_BINANCE_COINFUTURES:
  case mdmsg::PBMARKET_HYPERLIQUID:
  case mdmsg::PBMARKET_BYBITSPOT:
  case mdmsg::PBMARKET_BYBITDERIV:
  case mdmsg::PBMARKET_BYBITINVERSEDERIV:
  case mdmsg::PBMARKET_TOPBOOK_EQUITY:
  case mdmsg::PBMARKET_TOPBOOK_CME:
    return true;
  case mdmsg::PBMARKET_UNKNOWN:
    return false;
  default:
    return false;
  }
}

static int payloadFieldCount(const mdmsg::PbMessage& msg) noexcept {
  int count = 0;
  if (msg.has_ticker_update()) ++count;
  if (msg.has_book_update()) ++count;
  if (msg.has_book_snapshot()) ++count;
  if (msg.has_book_trade()) ++count;
  if (msg.has_quote()) ++count;
  return count;
}

// Caller must hold g_hl_node.gate_mtx. Returns true if `msg` should be
// published from `source`. Within a single event_time, one source owns the
// slot; same-source equal-time updates are allowed for WS fast/slow merge.
static bool advanceHLPublishedBookGateLocked(const mdmsg::PbMessage& msg,
                                             HLNodeShared::HLBookSource source) {
  int64_t t = msg.book_snapshot().event_time();
  if (t <= 0) return true;
  auto& last = g_hl_node.last_published_book_by_sym[msg.symbol_id()];
  if (t < last.t) return false;             // stale chain time
  if (t > last.t) {                          // new chain time, any source
    last.t = t;
    last.source = source;
    return true;
  }
  // t == last.t: only the source that opened this slot may update it.
  return source == last.source;
}

// Caller must hold g_hl_node.gate_mtx. Returns true if `msg` should be
// published from `source`. Within a single trade_time we only accept
// from the source that first claimed that slot. New trade_times reset
// the source ownership.
static bool advanceHLPublishedTradeGateLocked(const mdmsg::PbMessage& msg,
                                              HLNodeShared::HLTradeSource source) {
  int64_t t = msg.book_trade().trade_time();
  if (t <= 0) return true;
  auto& last = g_hl_node.last_published_trade_by_sym[msg.symbol_id()];
  if (t < last.t) return false;             // stale chain time
  if (t > last.t) {                          // new chain time, any source
    last.t = t;
    last.source = source;
    return true;
  }
  // t == last.t: only the source that opened this slot may add more.
  return source == last.source;
}

void handleShutdown(int) { g_shutdown.store(true); }

// Does dns lookups and returns them.
// We'll use this to map sessions to tgts I think. ANd we may
// need to restart them over time.
std::vector<std::string> get_ip_addresses(std::string tgt_domain) {
  std::set<std::string> to_ret_set;

  std::vector<std::string> to_ret;

  struct addrinfo* dns_lookup;
  int lookup_err;

  /* resolve the domain name into a list of addresses */
  std::cout << " Performing DNS lookup for " << tgt_domain << std::endl;
  lookup_err = getaddrinfo(tgt_domain.c_str(), NULL, NULL, &dns_lookup);

  if (lookup_err != 0) {
    if (lookup_err == EAI_SYSTEM) {
      throw std::runtime_error("Got an error doing getaddrinfo, EAI_SYSTEM");
    } else {
      fprintf(stderr, "error in getaddrinfo: %s\n", gai_strerror(lookup_err));
    }
    exit(EXIT_FAILURE);
  }

  char ipv4_chars[INET_ADDRSTRLEN];
  struct sockaddr_in* addr4;

  char ipv6_chars[INET6_ADDRSTRLEN];
  struct sockaddr_in6* addr6;

  for (struct addrinfo* res = dns_lookup; res != NULL; res = res->ai_next) {
    if (res->ai_addr->sa_family == AF_INET) {
      addr4 = (struct sockaddr_in*)res->ai_addr;
      inet_ntop(AF_INET, &addr4->sin_addr, ipv4_chars, INET_ADDRSTRLEN);

      // printf("IP: %s\n", ipv4);
      to_ret_set.emplace(ipv4_chars);
    } else if (res->ai_addr->sa_family == AF_INET6) {
      // FYI, there might be some issues w/ ipv6 subscriptions. Maybe
      // (looking at some stacoverclow) we need to wrap it in backets?
      // eg.
      //   , ws = new WebSocket('ws://[2600:3c00::f03c:91ff:fe73:2b08]:31333');

      addr6 = (struct sockaddr_in6*)res->ai_addr;
      inet_ntop(AF_INET6, &addr6->sin6_addr, ipv6_chars, INET6_ADDRSTRLEN);
      // printf("IP: %s\n", ipv6);
      to_ret_set.emplace(ipv6_chars);
    }
  }
  freeaddrinfo(dns_lookup);

  to_ret.assign(to_ret_set.begin(), to_ret_set.end());

  return to_ret;
}

std::mutex pub_mtx;

void publish(const mdmsg::PbMessage& msg, zmq::socket_t* sock) {
  try {
    if (!isPublishableMarket(msg.market())) {
      LOG_EVERY_N(WARNING, 1000)
          << "Dropping mdmsg with invalid market=" << static_cast<int>(msg.market())
          << " symbol=" << msg.symbol_id();
      return;
    }

    int payload_count = payloadFieldCount(msg);
    if (payload_count != 1) {
      LOG_EVERY_N(WARNING, 1000)
          << "Dropping mdmsg with payload_count=" << payload_count
          << " market=" << static_cast<int>(msg.market())
          << " symbol=" << msg.symbol_id();
      return;
    }

    size_t byte_size = msg.ByteSizeLong();
    if (byte_size == 0 || byte_size > static_cast<size_t>(std::numeric_limits<int>::max())) {
      LOG_EVERY_N(WARNING, 1000)
          << "Dropping mdmsg with invalid serialized size=" << byte_size
          << " market=" << static_cast<int>(msg.market())
          << " symbol=" << msg.symbol_id();
      return;
    }

    zmq::message_t m(byte_size);
    if (!msg.SerializeToArray(m.data(), static_cast<int>(byte_size))) {
      LOG_EVERY_N(WARNING, 1000)
          << "Dropping mdmsg after SerializeToArray failure"
          << " market=" << static_cast<int>(msg.market())
          << " symbol=" << msg.symbol_id()
          << " size=" << byte_size;
      return;
    }

    std::lock_guard<std::mutex> lck(pub_mtx);
    auto result = sock->send(m, zmq::send_flags::dontwait);
    if (!result.has_value()) {
      LOG(ERROR) << "Failed to send ZMQ message";
    }
    VLOG(1) << msg.ShortDebugString();
  } catch (const std::exception& e) {
    LOG(ERROR) << "Error publishing message: " << e.what();
  }
}

// This is a little painstaking, but I think it's ok.
void readSecrets(rapidjson::Value& subscription_config,
                 std::map<pktrade::Market, std::string>& mkt_to_apikeys) {
  std::cout << " Reading secrets" << std::endl;
  // Multiplex over all supported markets.
  rapidjson::Document secrets_json;

  // Not elseifs. Definite
  if (subscription_config.HasMember("BinanceSPOT")) {
    std::string creds_path = std::string(getenv("HOME")) + "/.creds/.BinanceSPOT.creds.json";

    if (!std::filesystem::exists(creds_path)) {
      throw std::runtime_error("Trying to read nonexistent secrets path " + creds_path);
    }
    if (subscription_config["BinanceSPOT"].HasMember("creds_path")) {
      creds_path = subscription_config["BinanceSPOT"]["creds_path"].GetString();
    }

    secrets_json = pktrade::util::read_json_file(creds_path);
    mkt_to_apikeys[pktrade::Market::BinanceSPOT] = secrets_json["api_key"].GetString();
  }

  if (subscription_config.HasMember("BinanceFutures")) {
    std::string creds_path = std::string(getenv("HOME")) + "/.creds/.BinanceFutures.creds.json";
    if (!std::filesystem::exists(creds_path)) {
      throw std::runtime_error("Trying to read nonexistent secrets path " + creds_path);
    }

    if (subscription_config["BinanceFutures"].HasMember("creds_path")) {
      creds_path = subscription_config["BinanceFutures"]["creds_path"].GetString();
    }
    secrets_json = pktrade::util::read_json_file(creds_path);
    mkt_to_apikeys[pktrade::Market::BinanceFutures] = secrets_json["api_key"].GetString();
  }

  if (subscription_config.HasMember("BinanceCOINFutures")) {
    std::string creds_path = std::string(getenv("HOME")) + "/.creds/.BinanceCOINFutures.creds.json";
    if (!std::filesystem::exists(creds_path)) {
      throw std::runtime_error("Trying to read nonexistent secrets path " + creds_path);
    }

    if (subscription_config["BinanceCOINFutures"].HasMember("creds_path")) {
      creds_path = subscription_config["BinanceCOINFutures"]["creds_path"].GetString();
    }
    secrets_json = pktrade::util::read_json_file(creds_path);
    mkt_to_apikeys[pktrade::Market::BinanceCOINFutures] = secrets_json["api_key"].GetString();
  }

  if (subscription_config.HasMember("Hyperliquid")) {
    // Don't need an api key for hyperliquid market data.
  }

  // Databento uses the same API key for both EQ and CME
  if (subscription_config.HasMember("DataBentoEquities") ||
      subscription_config.HasMember("DataBentoCME") ||
      subscription_config.HasMember("DataBentoBoats")) {
    std::string creds_path = std::string(getenv("HOME")) + "/.creds/.DataBento.creds.json";
    if (!std::filesystem::exists(creds_path)) {
      throw std::runtime_error("Trying to read nonexistent secrets path " + creds_path);
    }
    secrets_json = pktrade::util::read_json_file(creds_path);
    std::string db_key = secrets_json["api_key"].GetString();
    if (subscription_config.HasMember("DataBentoEquities")) {
      mkt_to_apikeys[pktrade::Market::TopBookEquity] = db_key;
    }
    if (subscription_config.HasMember("DataBentoCME")) {
      mkt_to_apikeys[pktrade::Market::TopBookCme] = db_key;
    }
    if (subscription_config.HasMember("DataBentoBoats")) {
      // BOATS publishes to the same TopBookEquity protobuf market as the EQ feed
      // (sessions don't overlap), so it shares the same API key slot.
      mkt_to_apikeys[pktrade::Market::TopBookEquity] = db_key;
    }
  }
}

constexpr int64_t kEqStaleThresholdMs = 10'000;
constexpr int64_t kEqSilentThresholdMs = 60'000;
constexpr int64_t kCmeStaleThresholdMs = 10'000;
constexpr int64_t kCmeSilentThresholdMs = 60'000;
// Silent watchdog only — no per-message stale check (overnight book is naturally quiet).
constexpr int64_t kBoatsSilentThresholdMs = 60'000;
constexpr int64_t kStaleAlertIntervalMs = 60'000;
constexpr int kMaxDatabentoBadMsgs = 100;
// Hyperliquid occasionally pauses for just over 30s without actually dropping
// the connection, so we give it a little more slack before paging.
constexpr int64_t kHyperliquidWatchdogMs = 45'000;
constexpr int64_t kHyperliquidAlertIntervalMs = 60'000;

namespace {

const date::time_zone* NyZone() {
  static const auto* zone = date::locate_zone("America/New_York");
  return zone;
}

date::sys_days currentEtDay() {
  auto now =
      std::chrono::time_point_cast<std::chrono::milliseconds>(std::chrono::system_clock::now());
  date::zoned_time<std::chrono::milliseconds> et_time{NyZone(), now};
  auto local_midnight = date::floor<date::days>(et_time.get_local_time());
  date::local_time<std::chrono::milliseconds> midnight_local{local_midnight};
  auto sys_midnight = NyZone()->to_sys(midnight_local);
  return date::floor<date::days>(sys_midnight);
}

using SessionInterval = std::pair<int64_t, int64_t>;

class SessionIntervalCache {
 public:
  using Generator = std::function<void(date::sys_days, int, std::vector<SessionInterval>&)>;

  SessionIntervalCache(Generator generator, date::sys_days start_day, int initial_days)
      : generator_(std::move(generator)), next_day_(start_day) {
    appendDays(initial_days);
  }

  bool contains(int64_t ts_ms) {
    std::lock_guard<std::mutex> lock(mtx_);
    if (intervals_.empty()) {
      appendDays(kExtendDays);
      if (intervals_.empty()) {
        return false;
      }
    }

    ensureCoverage(ts_ms);

    while (index_ < intervals_.size() && ts_ms >= intervals_[index_].second) {
      ++index_;
    }
    while (index_ > 0 && ts_ms < intervals_[index_].first) {
      --index_;
    }

    if (index_ >= intervals_.size()) {
      return false;
    }
    const auto& [start, end] = intervals_[index_];
    return ts_ms >= start && ts_ms < end;
  }

 private:
  void appendDays(int days) {
    generator_(next_day_, days, intervals_);
    next_day_ += date::days(days);
  }

  void ensureCoverage(int64_t ts_ms) {
    while (!intervals_.empty() && ts_ms >= intervals_.back().second) {
      auto prev_size = intervals_.size();
      appendDays(kExtendDays);
      if (intervals_.size() == prev_size) {
        break;
      }
    }
  }

  Generator generator_;
  date::sys_days next_day_;
  std::vector<SessionInterval> intervals_;
  size_t index_ = 0;
  static constexpr int kExtendDays = 7;
  std::mutex mtx_;
};

void generateUsEquityIntervals(date::sys_days start_day, int days,
                               std::vector<SessionInterval>& intervals) {
  auto* ny_zone = NyZone();
  for (int i = 0; i < days; ++i) {
    date::sys_days cur_day = start_day + date::days(i);
    date::weekday weekday{cur_day};
    // c_encoding: 0=Sun, 1=Mon, ..., 5=Fri, 6=Sat
    if (weekday.c_encoding() == 0 || weekday.c_encoding() == 6) {
      continue;
    }

    auto local_day = date::local_days{date::year_month_day{cur_day}};
    date::local_time<std::chrono::milliseconds> base_local{local_day};
    auto open_local = base_local + std::chrono::hours(4);
    auto close_local = base_local + std::chrono::hours(20);

    auto open_sys = ny_zone->to_sys(open_local);
    auto close_sys = ny_zone->to_sys(close_local);
    intervals.emplace_back(open_sys.time_since_epoch().count(),
                           close_sys.time_since_epoch().count());
  }
}

void generateCmeIntervals(date::sys_days start_day, int days,
                          std::vector<SessionInterval>& intervals) {
  auto* ny_zone = NyZone();
  for (int i = 0; i < days; ++i) {
    date::sys_days cur_day = start_day + date::days(i);
    date::weekday weekday{cur_day};
    auto local_day = date::local_days{date::year_month_day{cur_day}};
    date::local_time<std::chrono::milliseconds> base_local{local_day};

    auto append_interval = [&](int start_hour, int end_hour) {
      auto start_local = base_local + std::chrono::hours(start_hour);
      auto end_local = base_local + std::chrono::hours(end_hour);
      auto start_sys = ny_zone->to_sys(start_local);
      auto end_sys = ny_zone->to_sys(end_local);
      intervals.emplace_back(start_sys.time_since_epoch().count(),
                             end_sys.time_since_epoch().count());
    };

    switch (weekday.c_encoding()) {
      case 0: // Sunday
        append_interval(18, 24);
        break;
      case 6: // Saturday
        break;
      case 5: // Friday
        append_interval(0, 17);
        break;
      default: // Monday-Thursday
        append_interval(0, 17);
        append_interval(18, 24);
        break;
    }
  }
}

// BOATS (Blue Ocean ATS) overnight US equity session: 20:00 ET → next-day 04:00 ET,
// session opens Sun-Thu evenings. Fri evening and Sat evening = no session.
void generateBoatsIntervals(date::sys_days start_day, int days,
                            std::vector<SessionInterval>& intervals) {
  auto* ny_zone = NyZone();
  for (int i = 0; i < days; ++i) {
    date::sys_days cur_day = start_day + date::days(i);
    date::weekday weekday{cur_day};
    int wd = weekday.c_encoding(); // 0=Sun, 1=Mon, ..., 5=Fri, 6=Sat
    // Session opens on the evenings of Sun(0), Mon(1), Tue(2), Wed(3), Thu(4).
    if (wd >= 5) {
      continue;
    }

    auto local_day = date::local_days{date::year_month_day{cur_day}};
    date::local_time<std::chrono::milliseconds> base_local{local_day};
    auto open_local = base_local + std::chrono::hours(20);

    // Close is 04:00 ET on the next calendar day — anchor on next-day midnight to
    // let to_sys() handle any DST transition cleanly.
    auto next_local_day = date::local_days{date::year_month_day{cur_day + date::days(1)}};
    date::local_time<std::chrono::milliseconds> next_base_local{next_local_day};
    auto close_local = next_base_local + std::chrono::hours(4);

    auto open_sys = ny_zone->to_sys(open_local);
    auto close_sys = ny_zone->to_sys(close_local);
    intervals.emplace_back(open_sys.time_since_epoch().count(),
                           close_sys.time_since_epoch().count());
  }
}

SessionIntervalCache& usEquityCache() {
  static SessionIntervalCache cache(generateUsEquityIntervals, currentEtDay() - date::days(2), 30);
  return cache;
}

SessionIntervalCache& cmeCache() {
  static SessionIntervalCache cache(generateCmeIntervals, currentEtDay() - date::days(3), 30);
  return cache;
}

SessionIntervalCache& boatsCache() {
  // Start 2 days back to cover a session that opened the previous evening.
  static SessionIntervalCache cache(generateBoatsIntervals, currentEtDay() - date::days(2), 30);
  return cache;
}

} // namespace

bool isInCmeSession(int64_t ts_event_ms) {
  if (ts_event_ms <= 0) {
    return false;
  }
  return cmeCache().contains(ts_event_ms);
}

bool isInUsEquitySession(int64_t ts_event_ms) {
  if (ts_event_ms <= 0) {
    return false;
  }
  return usEquityCache().contains(ts_event_ms);
}

bool isInBoatsSession(int64_t ts_event_ms) {
  if (ts_event_ms <= 0) {
    return false;
  }
  return boatsCache().contains(ts_event_ms);
}

int resolveTradingDate(const std::string& date_arg) {
  auto digits_only = [](const std::string& s) {
    return !s.empty() &&
           std::all_of(s.begin(), s.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
  };

  if (digits_only(date_arg)) {
    if (date_arg.size() != 8) {
      throw std::runtime_error("Trading date must be YYYYMMDD when using digits.");
    }
    return std::stoi(date_arg);
  }

  std::string upper_arg = date_arg;
  std::transform(upper_arg.begin(), upper_arg.end(), upper_arg.begin(),
                 [](unsigned char c) { return static_cast<char>(std::toupper(c)); });

  const std::map<std::string, int> label_to_offset = {
      {"TODAY", 0},
      {"TOMORROW", 1},
      {"TOMORROW2", 2},
      {"TOMORROW3", 3},
  };

  auto label_it = label_to_offset.find(upper_arg);
  if (label_it == label_to_offset.end()) {
    throw std::runtime_error("Unsupported trading date label: " + date_arg);
  }

  const auto* ny_zone = date::locate_zone("America/New_York");
  date::zoned_time ny_time{ny_zone, std::chrono::system_clock::now()};
  auto local_days = date::floor<date::days>(ny_time.get_local_time());
  local_days += date::days(label_it->second);
  date::year_month_day ymd{local_days};

  int year = static_cast<int>(ymd.year());
  unsigned month = static_cast<unsigned>(ymd.month());
  unsigned day = static_cast<unsigned>(ymd.day());
  return year * 10000 + month * 100 + day;
}

////////////////////////////////////////////////////////////////////////////////////
// Websocket subscription handling

std::string getBinanceWebsocketURI(std::string ip_addr_or_domain,
                                   std::vector<std::string> stream_names) {
  return fmt::format("wss://{}/stream?streams={}", ip_addr_or_domain, fmt::join(stream_names, "/"));
}

// For non-binancec, we probably just want to provide the suffix (eg. "/spot")
std::string getWebsocketURI(std::string ip_addr_or_domain, std::string suffix) {
  return fmt::format("wss://{}/{}", ip_addr_or_domain, suffix);
}

void subscribeBinanceWebsockets(
    std::string market_name, rapidjson::Value& binance_sub_config,

    std::vector<ix::WebSocket*>& websockets,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_trd,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_ticker,
    std::mutex& t_mutex, zmq::socket_t& publisher) {
  pktrade::Market stream_mkt;

  bool deflate = false;
  if (binance_sub_config.HasMember("wssdeflate")) {
    deflate = binance_sub_config["wssdeflate"].GetBool();
  }

  std::vector<std::string> sub_stream_names;

  // TODO: expand these receives later on to capture some of the
  // other datafeeds we might get.
  if (market_name == "BinanceSPOT") {
    stream_mkt = Market::BinanceSPOT;
    for (rapidjson::Value& sym : binance_sub_config["symbols"].GetArray()) {
      std::string sym_lower = pktrade::util::lower_str(sym.GetString());
      sub_stream_names.push_back(fmt::format("{}@depth@100ms", sym_lower));
      sub_stream_names.push_back(fmt::format("{}@bookTicker", sym_lower));
      sub_stream_names.push_back(fmt::format("{}@trade", sym_lower));

      mkt_sym_latest_update[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_trd[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_ticker[stream_mkt][sym.GetString()] = 0;
    }
  } else if (market_name == "BinanceFutures") {
    stream_mkt = Market::BinanceFutures;
    for (rapidjson::Value& sym : binance_sub_config["symbols"].GetArray()) {
      std::string sym_lower = pktrade::util::lower_str(sym.GetString());
      sub_stream_names.push_back(fmt::format("{}@depth@0ms", sym_lower));
      sub_stream_names.push_back(fmt::format("{}@bookTicker", sym_lower));
      sub_stream_names.push_back(fmt::format("{}@trade", sym_lower));

      mkt_sym_latest_update[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_trd[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_ticker[stream_mkt][sym.GetString()] = 0;
    }
  } else if (market_name == "BinanceCOINFutures") {
    stream_mkt = Market::BinanceCOINFutures;
    for (rapidjson::Value& sym : binance_sub_config["symbols"].GetArray()) {
      std::string sym_lower = pktrade::util::lower_str(sym.GetString());
      sub_stream_names.push_back(fmt::format("{}@depth@0ms", sym_lower));
      sub_stream_names.push_back(fmt::format("{}@bookTicker", sym_lower));
      sub_stream_names.push_back(fmt::format("{}@trade", sym_lower));

      mkt_sym_latest_update[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_trd[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_ticker[stream_mkt][sym.GetString()] = 0;
    }
  }

  bool do_ip_lookup = false;
  if (binance_sub_config.HasMember("do_ip_lookup")) {
    do_ip_lookup = binance_sub_config["do_ip_lookup"].GetBool();
  }

  std::vector<std::string> wss_tgts =
      pktrade::get_ip_addresses(binance_sub_config["wss_endpoint"].GetString());

  if (!do_ip_lookup) {
    wss_tgts = {binance_sub_config["wss_endpoint"].GetString()};
  } else {
    int max_websocket_ips = 3;
    if (binance_sub_config.HasMember("max_websocket_ips")) {
      max_websocket_ips = binance_sub_config["max_websocket_ips"].GetInt();
    }
    // Restrict to #threads.
    if (wss_tgts.size() > max_websocket_ips) {
      wss_tgts.resize(max_websocket_ips);
    }
  }
  for (std::string wss_tgt : wss_tgts) {
    std::string sub_url = pktrade::getBinanceWebsocketURI(wss_tgt, sub_stream_names);
    // Subscribe, set the callback, run
    ix::WebSocket* ws = new ix::WebSocket();
    websockets.push_back(ws);
    ws->setUrl(sub_url);

    if (deflate) {
      ws->enablePerMessageDeflate();
    }
    ix::SocketTLSOptions tlsOptions;
    tlsOptions.caFile = "NONE";

    ws->setTLSOptions(tlsOptions);

    // TODO: Maybe make this a bit more graceful. Maybe config it from the
    // config?
    // Set a ping interval so we can restart the connection if it dies.
    ws->setPingInterval(60);
    ws->enableAutomaticReconnection();           // this is enabled by default but let's
                                                 // make it explicit
    ws->setMinWaitBetweenReconnectionRetries(1); // Set min wait for reconnect
                                                 // to 1ms for faster reconnect

    ws->setOnMessageCallback([&, mkt = stream_mkt](const ix::WebSocketMessagePtr& msg) {
      try {
        if (msg->type == ix::WebSocketMessageType::Message) {
          auto now = std::chrono::system_clock::now();

          // VLOG(2) << msg->str;
          std::lock_guard<std::mutex> lock(t_mutex);

          try {
            // TODO: figure out how to decode these gracefully for arbitrary
            // markets. This will be necessary when we start subbing to
            // perps data live.
            if (mkt == pktrade::Market::BinanceSPOT) {
              pktrade::mdmsg::PbMessage pb_msg =
                  pktrade::decodeBinanceStreamData(msg->str, false, false);

              if (pb_msg.has_book_update()) {
                if (pb_msg.book_update().last_update_id() >
                    mkt_sym_latest_update[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);
                  mkt_sym_latest_update[mkt][pb_msg.symbol_id()] =
                      pb_msg.book_update().last_update_id();
                }
              } else if (pb_msg.has_ticker_update()) {
                // std::cout << delta << std::endl;
                if (pb_msg.ticker_update().update_id() >
                    mkt_sym_latest_ticker[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);

                  mkt_sym_latest_ticker[mkt][pb_msg.symbol_id()] =
                      pb_msg.ticker_update().update_id();
                }

              } else if (pb_msg.has_book_trade()) {
                // std::cout << delta << std::endl;
                if (pb_msg.book_trade().trade_id() > mkt_sym_latest_trd[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);
                  mkt_sym_latest_trd[mkt][pb_msg.symbol_id()] = pb_msg.book_trade().trade_id();
                }
              }

            } else if (mkt == pktrade::Market::BinanceFutures) {
              // Horrible, horrible code dup. I'm sorry.
              pktrade::mdmsg::PbMessage pb_msg =
                  pktrade::decodeBinanceFuturesStreamData(msg->str, false, false);

              if (pb_msg.has_book_update()) {
                if (pb_msg.book_update().last_update_id() >
                    mkt_sym_latest_update[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);
                  mkt_sym_latest_update[mkt][pb_msg.symbol_id()] =
                      pb_msg.book_update().last_update_id();
                }
              } else if (pb_msg.has_ticker_update()) {
                // std::cout << delta << std::endl;
                if (pb_msg.ticker_update().update_id() >
                    mkt_sym_latest_ticker[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);

                  mkt_sym_latest_ticker[mkt][pb_msg.symbol_id()] =
                      pb_msg.ticker_update().update_id();
                }

              } else if (pb_msg.has_book_trade()) {
                // std::cout << delta << std::endl;
                if (pb_msg.book_trade().trade_id() > mkt_sym_latest_trd[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);

                  mkt_sym_latest_trd[mkt][pb_msg.symbol_id()] = pb_msg.book_trade().trade_id();
                }
              }
            } else if (mkt == pktrade::Market::BinanceCOINFutures) {
              // Horrible, horrible code dup. I'm sorry.
              pktrade::mdmsg::PbMessage pb_msg =
                  pktrade::decodeBinanceCOINFuturesStreamData(msg->str, false, false);

              if (pb_msg.has_book_update()) {
                if (pb_msg.book_update().last_update_id() >
                    mkt_sym_latest_update[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);
                  mkt_sym_latest_update[mkt][pb_msg.symbol_id()] =
                      pb_msg.book_update().last_update_id();
                }
              } else if (pb_msg.has_ticker_update()) {
                // std::cout << delta << std::endl;
                if (pb_msg.ticker_update().update_id() >
                    mkt_sym_latest_ticker[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);

                  mkt_sym_latest_ticker[mkt][pb_msg.symbol_id()] =
                      pb_msg.ticker_update().update_id();
                }

              } else if (pb_msg.has_book_trade()) {
                // std::cout << delta << std::endl;
                if (pb_msg.book_trade().trade_id() > mkt_sym_latest_trd[mkt][pb_msg.symbol_id()]) {
                  pktrade::publish(pb_msg, &publisher);
                  mkt_sym_latest_trd[mkt][pb_msg.symbol_id()] = pb_msg.book_trade().trade_id();
                }
              }
            }
          } catch (std::exception e) {
            LOG(ERROR) << "Received ill-parsed message: " << msg->str;
            std::cout << " Received ill-parsed message: " << msg->str << std::endl;
            return;
          }

        } else if (msg->type == ix::WebSocketMessageType::Pong) {
          // We get this every 60s (based on setPingInterval) so just log every 30min
          LOG_EVERY_N(INFO, 30) << "Got pong from Binance: " << msg->str;
        } else if (msg->type == ix::WebSocketMessageType::Open) {
          LOG(INFO) << "Connection established to " << msg->openInfo.uri;
        } else if (msg->type == ix::WebSocketMessageType::Error) {
          LOG(ERROR) << "Connection error: " << msg->errorInfo.reason;
        } else if (msg->type == ix::WebSocketMessageType::Close) {
          LOG(INFO) << "Connection closed (Code: " << msg->closeInfo.code
                    << " Reason: " << msg->closeInfo.reason << ")";
        }
      } catch (const std::exception& e) {
        LOG(ERROR) << "Error in Binance websocket callback: " << e.what();
      } catch (...) {
        LOG(ERROR) << "Unknown exception in Binance websocket callback";
      }
    }); // end ws onCallback

    ws->start();
  }
}

// Per-sym snapshot state for the HL WS fast+slow merge. Fast: top-5 @ ~0.5s,
// slow: top-20 @ ~5s. Both subscriptions land on the same WS connection;
// we classify each incoming l2Book by depth, keep the latest fast and slow
// snapshots per sym, and publish a merged book where fast levels take
// precedence inside their depth range. Mirrors pymultifeed.hl_merge_book.
struct HLWsLevel {
  std::string px;
  std::string sz;
  int32_t n;
};
struct HLWsBook {
  std::vector<HLWsLevel> bids;
  std::vector<HLWsLevel> asks;
  // Per-stream freshness. fast and slow l2Book streams advance their
  // own event_time independently, so the cache must gate per-stream —
  // otherwise a newer fast snapshot can block a still-fresh slow update
  // and freeze levels 6-20.
  int64_t last_event_time = 0;
};

// fast top-5 levels first; slow contributes only px strictly outside fast's
// price range, so the merged book stays monotonic by price even if fast
// and slow disagree on the boundary.
static void mergeHyperliquidFastSlow(const HLWsBook* fast, const HLWsBook* slow,
                                     std::vector<HLWsLevel>* out_bids,
                                     std::vector<HLWsLevel>* out_asks) {
  out_bids->clear();
  out_asks->clear();
  if (!fast && !slow) return;
  if (!fast) { *out_bids = slow->bids; *out_asks = slow->asks; return; }
  if (!slow) { *out_bids = fast->bids; *out_asks = fast->asks; return; }

  // Bids: sorted high → low. Fast's last px is its lowest covered bid.
  if (!fast->bids.empty()) {
    double cutoff = std::stod(fast->bids.back().px);
    *out_bids = fast->bids;
    for (const auto& s : slow->bids) {
      if (std::stod(s.px) < cutoff) out_bids->push_back(s);
    }
  } else {
    *out_bids = slow->bids;
  }
  // Asks: sorted low → high. Fast's last px is its highest covered ask.
  if (!fast->asks.empty()) {
    double cutoff = std::stod(fast->asks.back().px);
    *out_asks = fast->asks;
    for (const auto& s : slow->asks) {
      if (std::stod(s.px) > cutoff) out_asks->push_back(s);
    }
  } else {
    *out_asks = slow->asks;
  }
}

// Send the three Hyperliquid subscription frames (trades, slow l2Book, fast
// l2Book) for one symbol on an already-open websocket. Factored out so the same
// path is used both when (re)establishing a connection (the Open handler) and
// when adding a symbol intraday without a reconnect (the config reload). `ws`
// send() is thread-safe — the ping and watchdog threads already call it/close()
// cross-thread — so the reload thread may call this directly. Symbols use the
// "dex_name:COIN" form for dex subscriptions.
void hlSubscribeSymbol(ix::WebSocket* ws, const std::string& sym_str) {
  // Check if this is a dex subscription (format: "dex_name:COIN")
  std::string dex_name;
  auto dex_index = sym_str.find(':');
  if (dex_index != std::string::npos) {
    dex_name = sym_str.substr(0, dex_index);
    if (dex_name != "xyz") {
      LOG(ERROR) << "Tried to subscribe to unsupported dex: " << dex_name;
      return;
    }
  }

  // sub to trades
  nlohmann::json trd_sub_details;
  trd_sub_details["type"] = "trades";
  trd_sub_details["coin"] = sym_str;
  if (!dex_name.empty()) {
    trd_sub_details["dex"] = dex_name;
  }
  nlohmann::json trd_payload = {{"method", "subscribe"}};
  trd_payload["subscription"] = trd_sub_details;
  try {
    ws->send(trd_payload.dump());
  } catch (const std::exception& e) {
    LOG(ERROR) << "Failed to send subscription request: " << e.what();
  }

  // sub to l2 book (slow: top-20 levels, ~5s cadence)
  nlohmann::json sub_details;
  sub_details["type"] = "l2Book";
  sub_details["coin"] = sym_str;
  if (!dex_name.empty()) {
    sub_details["dex"] = dex_name;
  }
  nlohmann::json payload = {{"method", "subscribe"}};
  payload["subscription"] = sub_details;
  try {
    ws->send(payload.dump());
  } catch (const std::exception& e) {
    LOG(ERROR) << "Failed to send subscription request: " << e.what();
  }

  // sub to l2 book (fast: top-5 levels, ~0.5s cadence). Pre-upgrade this
  // returns the same slow shape and gets bucketed as slow by the depth
  // heuristic in the message handler; no regression.
  nlohmann::json fast_sub_details = sub_details;
  fast_sub_details["fast"] = true;
  nlohmann::json fast_payload = {{"method", "subscribe"}};
  fast_payload["subscription"] = fast_sub_details;
  try {
    ws->send(fast_payload.dump());
  } catch (const std::exception& e) {
    LOG(ERROR) << "Failed to send fast subscription request: " << e.what();
  }
}

void subscribeHyperliquidWebsockets(
    std::string market_name, rapidjson::Value& hyperliquid_sub_config,

    std::vector<ix::WebSocket*>& websockets,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_trd,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_ticker,
    std::mutex& t_mutex, zmq::socket_t& publisher) {
  pktrade::Market stream_mkt = pktrade::Market::Hyperliquid;

  std::vector<std::string> symbols;

  std::string wss_tgt = hyperliquid_sub_config["wss_endpoint"].GetString();

  ix::WebSocket* ws = new ix::WebSocket();
  websockets.push_back(ws);
  // Publish the handle so the intraday config reload can subscribe newly-added
  // symbols directly on this live socket (thread-safe send, no reconnect). Only
  // one Hyperliquid WS is ever created, so a single global handle suffices.
  g_hl_ws.store(ws);
  ws->setUrl(wss_tgt);

  auto last_activity_ms = std::make_shared<std::atomic<int64_t>>(0);
  auto last_alert_ms = std::make_shared<std::atomic<int64_t>>(0);
  auto hl_msg_count = std::make_shared<std::atomic<int64_t>>(0);
  // Per-sym fast/slow book state. shared_ptr so the WS callback's lifetime
  // is extended past the end of this function (the WS thread keeps running).
  // Access is guarded by t_mutex (taken at the top of every message).
  auto fast_books = std::make_shared<std::map<std::string, HLWsBook>>();
  auto slow_books = std::make_shared<std::map<std::string, HLWsBook>>();

  ix::SocketTLSOptions tlsOptions;
  tlsOptions.caFile = "NONE";
  ws->setTLSOptions(tlsOptions);

  // Not sure if this is necessary to do, but I noticed we aren't doing it
  // already? Note though that this isn't the same as the ping needed for the
  // gateway.
  ws->setPingInterval(60);

  // This is copied from the datlog.
  ws->enableAutomaticReconnection();           // this is enabled by default but let's
                                               // make it explicit
  ws->setMinWaitBetweenReconnectionRetries(1); // Set min wait for reconnect to
                                               // 1ms for faster reconnect

  // HL will disconnect after 60s of inactivity, so ping every 25s
  // (two chances + buffer), matching pymultifeed behavior.
  std::thread ping_thread([the_ws = ws]() {
    while (!g_shutdown.load()) {
      shutdownAwareSleep(std::chrono::seconds(25));
      if (g_shutdown.load()) break;
      try {
        the_ws->send(R"({"method":"ping"})");
      } catch (const std::exception& e) {
        LOG(ERROR) << "Hyperliquid ping failed: " << e.what();
      }
    }
  });
  {
    // Joinable, not detached — it dereferences `ws`, so main() joins it before
    // deleting the websocket. See g_hl_ws_threads.
    std::lock_guard<std::mutex> lock(g_hl_ws_threads_mtx);
    g_hl_ws_threads.emplace_back(std::move(ping_thread));
  }

  ws->setOnMessageCallback([the_ws = ws, mkt = stream_mkt, last_activity_ms, hl_msg_count,
                            fast_books, slow_books, &hyperliquid_sub_config,
                            &mkt_sym_latest_update, &mkt_sym_latest_trd,
                            &t_mutex, &publisher](const ix::WebSocketMessagePtr& msg) {
    try {
      if (msg->type == ix::WebSocketMessageType::Message) {
        // If the message is a channelresponse, ignore.

        // Wrap this in a try/catch also.
        rapidjson::Document ws_doc;
        try {
          ws_doc.Parse(msg->str.c_str());
          if (ws_doc.HasParseError()) {
            std::cout << " Hyperliquid receceived ill-parsed message: " << msg->str << std::endl;
            return;
          }
        } catch (std::exception e) {
          LOG(ERROR) << "Hyperliquid receceived ill-parsed message: " << msg->str;
          return;
        }

        if (!ws_doc.IsObject()) {
          LOG(WARNING) << "Hyperliquid received non-object message, skipping: " << msg->str;
          return;
        }

        auto channel_it = ws_doc.FindMember("channel");
        if (channel_it == ws_doc.MemberEnd() || !channel_it->value.IsString()) {
          // Always the heartbeat. 
          // LOG(WARNING) << "Hyperliquid message missing channel field, skipping: " << msg->str;
          return;
        }
        const char* channel = channel_it->value.GetString();

        if (!ws_doc.HasMember("data")) {
          // This happens all the time for hyperliquid, going to skip it so we don't clog things up. 
          // LOG(WARNING) << "Hyperliquid message missing data field, skipping: " << msg->str;
          return;
        }

        // Does some string matching here so we can skip unused
        // channels.
        if (strcmp(channel, "l2Book") != 0 && strcmp(channel, "trades") != 0) {
          std::cout << " Skipping unused response " << msg->str << std::endl;
          return;
        }

        auto now = std::chrono::system_clock::now();
        std::lock_guard<std::mutex> lock(t_mutex);

        int64_t now_ms =
            std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();
        last_activity_ms->store(now_ms);

        // TODO: figure out how to decode these gracefully for arbitrary
        // markets. This will be necessary when we start subbing to perps
        // data live.
        std::vector<pktrade::mdmsg::PbMessage> pb_msgs =
            pktrade::decodeHyperliquidStreamData(ws_doc, now_ms, false, false);

        if (pb_msgs.empty()) {
          LOG(WARNING) << "Hyperliquid decoder produced no messages for " << channel
                       << ", payload: " << msg->str;
          return;
        }

        // Note that hyperliquid deals in snapshots and not diff updates.
        if (pb_msgs[0].has_book_snapshot()) {
          // Decoder gives us a PbMessage with all the levels of THIS
          // incoming snapshot (fast OR slow). For the fast+slow merge
          // (mirrors pymultifeed.hl_merge_book), we:
          //   1. classify this snapshot by depth (≤5 → fast, else slow),
          //   2. cache the levels in the corresponding per-sym book,
          //   3. build a merged PbBookSnapshot,
          //   4. publish the merged form through the existing gates.
          //
          // Pre-upgrade fallthrough: if HL hasn't enabled fast yet, both
          // subscribes return 20-level slow shape; fast bucket stays
          // empty and merge() returns slow-only, so behavior is unchanged.
          const pktrade::mdmsg::PbMessage& pb_msg = pb_msgs[0];
          const auto& bs_in = pb_msg.book_snapshot();
          const std::string& sym = pb_msg.symbol_id();
          int64_t event_time = bs_in.event_time();

          // Freshness marker (see HLNodeShared::last_ws_book_event_time_ms).
          if (event_time) {
            g_hl_node.last_ws_book_event_time_ms.store(
                event_time, std::memory_order_relaxed);
          }

          // Classify by depth and gate against THIS stream's cache
          // freshness — not the shared per-sym timestamp. If we gated
          // both streams on a single shared timestamp, a fresh fast
          // snapshot at T1 would block a still-fresh slow snapshot at
          // T0 < T1 from updating, freezing levels 6-20.
          int32_t depth = std::max(bs_in.bids_size(), bs_in.asks_size());
          bool is_fast = depth <= 5;
          HLWsBook& dest = (is_fast ? *fast_books : *slow_books)[sym];
          if (event_time < dest.last_event_time) {
            // Old update on this stream; ignore (but stay quiet —
            // post-upgrade HL serves both subscribes from the same
            // stream so duplicates at identical event_time are normal).
            if (event_time != dest.last_event_time) {
              LOG(WARNING) << fmt::format(
                  "HL {} l2Book stale on {} stream: event_time={} < last={}",
                  sym, is_fast ? "fast" : "slow", event_time, dest.last_event_time);
            }
          } else {
            dest.bids.clear();
            dest.asks.clear();
            for (const auto& l : bs_in.bids()) {
              dest.bids.push_back({l.px(), l.qty(), l.numords()});
            }
            for (const auto& l : bs_in.asks()) {
              dest.asks.push_back({l.px(), l.qty(), l.numords()});
            }
            dest.last_event_time = event_time;

            // Build merged book.
            const HLWsBook* fast_p = nullptr;
            const HLWsBook* slow_p = nullptr;
            if (auto it = fast_books->find(sym); it != fast_books->end()) fast_p = &it->second;
            if (auto it = slow_books->find(sym); it != slow_books->end()) slow_p = &it->second;
            std::vector<HLWsLevel> merged_bids, merged_asks;
            mergeHyperliquidFastSlow(fast_p, slow_p, &merged_bids, &merged_asks);

            // Construct merged PbMessage.
            pktrade::mdmsg::PbMessage merged_msg;
            merged_msg.set_symbol_id(sym);
            merged_msg.set_market(pktrade::mdmsg::PBMARKET_HYPERLIQUID);
            // Preserve the decoder's rx_timestamp on the merged message so
            // downstream consumers can measure latency the same way as on
            // the trade path (which publishes the decoded pb_msg directly).
            if (pb_msg.rx_timestamp()) {
              merged_msg.set_rx_timestamp(pb_msg.rx_timestamp());
            }
            auto* mbs = merged_msg.mutable_book_snapshot();
            if (event_time) mbs->set_event_time(event_time);
            for (const auto& l : merged_bids) {
              auto* pl = mbs->add_bids();
              pl->set_px(l.px); pl->set_qty(l.sz); pl->set_numords(l.n);
            }
            for (const auto& l : merged_asks) {
              auto* pl = mbs->add_asks();
              pl->set_px(l.px); pl->set_qty(l.sz); pl->set_numords(l.n);
            }

            // Publish decision: per-(sym, event_time) gate alone — whichever
            // source has the newer event_time wins per sym. See
            // overmind/studies/hl_feed/consumer_freshness_plan_20260626.md.
            bool gate_publish = true;
            {
              std::lock_guard<std::mutex> lk(g_hl_node.gate_mtx);
              gate_publish = advanceHLPublishedBookGateLocked(
                  merged_msg, HLNodeShared::HLBookSource::WS);
            }
            if (gate_publish) {
              pktrade::publish(merged_msg, &publisher);
            }
            mkt_sym_latest_update[mkt][sym] = event_time;
            int64_t n = hl_msg_count->fetch_add(1) + 1;
            int64_t delay_ms = now_ms - event_time;
            // Same throttle as the trade-arrival log below: every 500th, or
            // every 5th when delay > 1s. Snapshots arrive at ~2/sec per sym
            // so without this the WS-lag case can flood the journal.
            if (n % 500 == 0 || (delay_ms > 1000 && n % 5 == 0)) {
              LOG(INFO) << fmt::format("HL ws {} l2Book event_time {} delay_ms {} (fast={}, merged={})",
                                       sym, event_time, delay_ms, is_fast,
                                       merged_bids.size() + merged_asks.size());
            }
          }
        } else if (pb_msgs[0].has_book_trade()) {
          // When you first subscribe to trades feed, you get a message
          // with a bunch of old trades which could be out of order by
          // time. I haven't seen a trade message with more than one
          // trade after the initial message, but I'm sorting by time if
          // there is more than one trade just to be safe.
          if (pb_msgs.size() > 1) {
            std::sort(
                pb_msgs.begin(), pb_msgs.end(),
                [](const pktrade::mdmsg::PbMessage& lhs, const pktrade::mdmsg::PbMessage& rhs) {
                  return lhs.book_trade().trade_time() < rhs.book_trade().trade_time();
                });
          }
          for (auto& pb_msg : pb_msgs) {
            if (pb_msg.book_trade().trade_time() >=
                mkt_sym_latest_trd[mkt][pb_msg.symbol_id()] - 1) {
              // Per-(sym, trade_time) first-source-wins arbitration.
              bool gate_publish;
              {
                std::lock_guard<std::mutex> lk(g_hl_node.gate_mtx);
                gate_publish = advanceHLPublishedTradeGateLocked(
                    pb_msg, HLNodeShared::HLTradeSource::WS);
              }
              if (gate_publish) {
                pktrade::publish(pb_msg, &publisher);
              }
              mkt_sym_latest_trd[mkt][pb_msg.symbol_id()] = pb_msg.book_trade().trade_time();
              // Freshness marker (see HLNodeShared::last_ws_trade_time_ms).
              g_hl_node.last_ws_trade_time_ms.store(
                  pb_msg.book_trade().trade_time(), std::memory_order_relaxed);
              int64_t n = hl_msg_count->fetch_add(1) + 1;
              int64_t delay_ms = now_ms - pb_msg.book_trade().trade_time();
              // Always log every 500th. For high-delay trades (>1s), also
              // log every 5th — enough to see a delay event in the journal
              // without spamming when the WS path lags consistently.
              if (n % 500 == 0 || (delay_ms > 1000 && n % 5 == 0)) {
                LOG(INFO) << fmt::format("HL ws {} trade trade_time {} delay_ms {}",
                                         pb_msg.symbol_id(), pb_msg.book_trade().trade_time(),
                                         delay_ms);
              }
            }
          }
        } else {
          // We shouldn't be in this case at all, but let's check just in case?
          LOG(WARNING) << "Hyperliquid received unhandled message: " << msg->str;
        }

      } else if (msg->type == ix::WebSocketMessageType::Pong) {
        // We get this every 60s (based on setPingInterval) so just log every 30min
        LOG_EVERY_N(INFO, 30) << "Got pong from Hyperliquid: " << msg->str;
      } else if (msg->type == ix::WebSocketMessageType::Open) {
        LOG(INFO) << "Connection established to " << msg->openInfo.uri;
        std::cout << " Connection established, sending request" << std::endl;
        // (Re)establish subscriptions for the base symbols (from the immutable
        // config) plus any intraday-added extras. Extras are empty unless the
        // conf was reloaded; re-sending them on every reconnect is what makes an
        // intraday add durable across HL's automatic reconnects (the reload
        // subscribes them live too, see reloadConfigSymbols).
        for (rapidjson::Value& sym : hyperliquid_sub_config["symbols"].GetArray()) {
          hlSubscribeSymbol(the_ws, sym.GetString());
        }
        for (const auto& extra_sym : extraSymsFor("Hyperliquid")) {
          hlSubscribeSymbol(the_ws, extra_sym);
        }
      } else if (msg->type == ix::WebSocketMessageType::Error) {
        LOG(ERROR) << "Connection error: " << msg->errorInfo.reason;
      } else {
        LOG(INFO) << " Unhandled Hyperliquid websocket msg " << msg->str;
      }

    } catch (const std::exception& e) {
      LOG(ERROR) << "Error in Hyperliquid websocket callback: " << e.what();
    } catch (...) {
      LOG(ERROR) << "Unknown exception in Hyperliquid websocket callback";
    }
  }); // end ws onCallback
  ws->start();

  std::thread watchdog_thread([last_activity_ms, last_alert_ms, the_ws = ws]() {
    while (!g_shutdown.load()) {
      try {
        int64_t last = last_activity_ms->load();
        int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::system_clock::now().time_since_epoch())
                            .count();
        if (last > 0 && now_ms - last > kHyperliquidWatchdogMs) {
          int64_t last_alert = last_alert_ms->load();
          if (now_ms - last_alert > kHyperliquidAlertIntervalMs) {
            last_alert_ms->store(now_ms);
            LOG(ERROR) << fmt::format(
                "Hyperliquid watchdog: no data for {}ms (threshold {}ms), forcing reconnect",
                now_ms - last, kHyperliquidWatchdogMs);
            try {
              pktrade::util::Mailer::instance().send_mail(
                  "HYPERLIQUID FEED SILENT",
                  fmt::format("No Hyperliquid data for {}ms, forcing reconnect", now_ms - last));
            } catch (const std::exception& e) {
              LOG(ERROR) << "Failed to send Hyperliquid watchdog alert: " << e.what();
            }
            // Push last_activity_ms 15s into the future so we don't re-fire
            // while the reconnect is still settling, and close the socket to
            // trigger ixwebsocket's automatic reconnection.
            last_activity_ms->store(now_ms + 15'000);
            try {
              the_ws->close(ix::WebSocketCloseConstants::kNormalClosureCode,
                            "watchdog forced reconnect");
            } catch (const std::exception& e) {
              LOG(ERROR) << "Hyperliquid watchdog close failed: " << e.what();
            }
          }
        }
      } catch (const std::exception& e) {
        LOG(ERROR) << "Hyperliquid watchdog exception: " << e.what();
      } catch (...) {
        LOG(ERROR) << "Hyperliquid watchdog unknown exception";
      }
      shutdownAwareSleep(std::chrono::seconds(5));
    }
  });
  {
    // Joinable, not detached — it dereferences `ws`, so main() joins it before
    // deleting the websocket. See g_hl_ws_threads.
    std::lock_guard<std::mutex> lock(g_hl_ws_threads_mtx);
    g_hl_ws_threads.emplace_back(std::move(watchdog_thread));
  }
}

// HyperliquidNode — consumes the pre-parsed PB feed published by
// hl_node_publisher_cpp on n1. Spawns two SUB threads (books + fills),
// each of which relays PbMessages straight to the local feed publisher.
// A 1Hz health thread maintains g_hl_node.node_healthy based on
// silent/lagging thresholds. Both the node SUB relay and the WS path
// (subscribeHyperliquidWebsockets) always publish via the per-(sym,
// chain_time) gate — first source to claim a slot wins, the other is
// dropped. The health flag is informational and drives the one-shot
// unhealthy alert. Mirrors pymultifeed's init_hl_node + init_hl_node_health.
void subscribeHyperliquidNode(rapidjson::Value& sub_config,
                              zmq::context_t& ctx,
                              zmq::socket_t& publisher) {
  std::string books_endpoint = sub_config["books_endpoint"].GetString();
  std::string fills_endpoint = sub_config["fills_endpoint"].GetString();
  std::unordered_set<std::string> sym_filter;
  if (sub_config.HasMember("symbols") && sub_config["symbols"].IsArray()) {
    for (const auto& sym : sub_config["symbols"].GetArray()) {
      if (sym.IsString()) sym_filter.insert(sym.GetString());
    }
  }
  // Publish the base filter so the relays read it and the intraday config
  // reload can swap in an extended set without a reconnect (the node publisher
  // already sends every symbol; the relay just filters). Empty = accept-all.
  {
    std::lock_guard<std::mutex> lk(g_hl_node_filter_mtx);
    g_hl_node_filter = std::make_shared<const std::unordered_set<std::string>>(sym_filter);
  }
  // Bump so each relay picks up the base filter on its first message.
  g_hl_node_filter_version.fetch_add(1, std::memory_order_release);
  if (sub_config.HasMember("fallback_silent_s")) {
    g_hl_node.fallback_silent_s = sub_config["fallback_silent_s"].GetDouble();
  }
  if (sub_config.HasMember("fallback_lag_s")) {
    g_hl_node.fallback_lag_s = sub_config["fallback_lag_s"].GetDouble();
  }
  g_hl_node.configured.store(true);

  // If no WS fallback is configured at all, declare the node always
  // healthy. With no second source the node's per-(sym, chain_time) gate
  // is uncontested, so this only affects the one-shot alert email.
  if (!g_hl_node.ws_fallback_present) {
    g_hl_node.node_healthy.store(true);
  }

  LOG(INFO) << fmt::format(
      "HyperliquidNode: books={} fills={} syms={} silent_s={:.1f} lag_s={:.1f} "
      "ws_fallback={} arbitration=freshest-source",
      books_endpoint, fills_endpoint, sym_filter.size(),
      g_hl_node.fallback_silent_s, g_hl_node.fallback_lag_s,
      g_hl_node.ws_fallback_present ? "yes" : "no");

  auto now_ms = []() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
  };

  // Relay thread factory: one per SUB endpoint. Reads PbMessages, applies
  // the shared per-sym monotonic gate, and republishes through the
  // process publisher. ctx and publisher are owned by main(); the
  // threads outlive nothing but the process.
  auto spawn_relay = [&publisher, &ctx, now_ms](std::string endpoint,
                                                std::string label) {
    std::thread relay_thread([endpoint, label, &ctx, &publisher, now_ms]() {
      zmq::socket_t sub(ctx, zmq::socket_type::sub);
      sub.set(zmq::sockopt::subscribe, "");
      // LINGER=0: drop anything queued on close instead of making ctx.close()
      // wait for it. Without this the default is infinite.
      sub.set(zmq::sockopt::linger, 0);
      // RCVHWM keeps us from blocking the publisher if we briefly fall
      // behind; CONFLATE is NOT set — we want every msg.
      try {
        sub.connect(endpoint);
      } catch (const std::exception& e) {
        LOG(ERROR) << fmt::format("HL node {} connect to {} failed: {}", label,
                                  endpoint, e.what());
        return;
      }
      LOG(INFO) << fmt::format("HL node {} SUB connected to {}", label, endpoint);

      int64_t local_count = 0;
      int64_t node_trade_log_count = 0;
      // Per-thread cached symbol filter, refreshed only when the reload bumps
      // the version (see g_hl_node_filter). seen_version starts at 0 so the
      // first message loads the base filter (seeded at version >= 1).
      uint64_t seen_filter_version = 0;
      std::shared_ptr<const std::unordered_set<std::string>> local_filter;
      while (!g_shutdown.load()) {
        zmq::message_t msg;
        zmq::pollitem_t items[] = {{static_cast<void*>(sub), 0, ZMQ_POLLIN, 0}};
        zmq::poll(items, 1, std::chrono::milliseconds(500));
        if (!(items[0].revents & ZMQ_POLLIN)) continue;
        auto rc = sub.recv(msg, zmq::recv_flags::none);
        if (!rc) continue;

        pktrade::mdmsg::PbMessage pb;
        if (!pb.ParseFromArray(msg.data(), msg.size())) {
          LOG(WARNING) << fmt::format("HL node {}: pb parse failed (len={})",
                                      label, msg.size());
          continue;
        }
        // Refresh the cached filter only when the reload bumps the version —
        // steady state is a single atomic load + compare, no lock. On a change
        // we take the lock once to copy the shared_ptr, then do the O(1) lookup
        // outside it. Empty/null = accept-all.
        uint64_t cur_version = g_hl_node_filter_version.load(std::memory_order_acquire);
        if (cur_version != seen_filter_version) {
          std::lock_guard<std::mutex> lk(g_hl_node_filter_mtx);
          local_filter = g_hl_node_filter;
          seen_filter_version = cur_version;
        }
        if (local_filter && !local_filter->empty() && !local_filter->count(pb.symbol_id())) {
          continue;
        }

        g_hl_node.last_msg_wall_ms.store(now_ms());
        if (pb.has_book_snapshot() && pb.book_snapshot().event_time()) {
          g_hl_node.last_event_time_ms.store(pb.book_snapshot().event_time());
        }

        // Per-(sym, chain_time) gate — first source to claim wins. See
        // overmind/studies/hl_feed/consumer_freshness_plan_20260626.md.
        bool republish = true;
        {
          std::lock_guard<std::mutex> lk(g_hl_node.gate_mtx);
          if (pb.has_book_snapshot()) {
            republish = advanceHLPublishedBookGateLocked(
                pb, HLNodeShared::HLBookSource::Node);
          } else if (pb.has_book_trade()) {
            int64_t t = pb.book_trade().trade_time();
            republish = advanceHLPublishedTradeGateLocked(
                pb, HLNodeShared::HLTradeSource::Node);
            // Freshness marker (see HLNodeShared::last_node_trade_time_ms).
            if (t) {
              g_hl_node.last_node_trade_time_ms.store(t, std::memory_order_relaxed);
            }
          }
        }
        // Per-trade delay log (mirrors the WS-side "HL ws ... delay_ms"
        // line). Every 500th trade plus any trade that lagged >1s, so the
        // journal can be eyeballed for both sources symmetrically.
        if (pb.has_book_trade()) {
          int64_t t = pb.book_trade().trade_time();
          if (t) {
            int64_t delay_ms = now_ms() - t;
            int64_t n = ++node_trade_log_count;
            // Mirror the WS-side throttle: every 500th, or every 5th when
            // delay > 1s.
            if (n % 500 == 0 || (delay_ms > 1000 && n % 5 == 0)) {
              LOG(INFO) << fmt::format(
                  "HL node {} trade trade_time {} delay_ms {}",
                  pb.symbol_id(), t, delay_ms);
            }
          }
        }
        if (republish) {
          pktrade::publish(pb, &publisher);
        }
        if (++local_count % 1000 == 0) {
          LOG(INFO) << fmt::format(
              "HL node {} relay msgs={} node_healthy={} sym={}",
              label, local_count,
              g_hl_node.node_healthy.load() ? "yes" : "no",
              pb.symbol_id());
        }
      }
      LOG(INFO) << fmt::format("HL node {} relay exiting", label);
    });
    // Joinable, not detached — see g_hl_node_threads. main() joins these before
    // closing the zmq context.
    std::lock_guard<std::mutex> lock(g_hl_node_threads_mtx);
    g_hl_node_threads.emplace_back(std::move(relay_thread));
  };

  spawn_relay(books_endpoint, "books");
  spawn_relay(fills_endpoint, "fills");

  // Health thread: 1Hz state machine. Considers the node feed unhealthy
  // when it goes silent for fallback_silent_s OR when chain-time lag
  // exceeds fallback_lag_s. The state drives the one-shot unhealthy-alert
  // email and the freshness-window log; it does NOT gate publishing.
  std::thread([now_ms]() {
    bool last_healthy_state = g_hl_node.node_healthy.load();
    // Rolling 5-minute summaries of node-vs-WS freshness, one for trades
    // and one for book snapshots. Sampled every 3s, accumulator-only
    // (no buffer, no sort), emitted once per window. Mirrors pymultifeed.
    //
    // diff = node_chain_time - ws_chain_time (positive means node has
    // seen a fresher trade/book than WS). The "tied" bucket captures the
    // common case where no new event arrived on either side since the
    // previous sample, so both atomics still hold the same prior chain
    // time.
    struct FreshnessWindow {
      int64_t n = 0;
      int64_t sum = 0;
      int64_t mn = std::numeric_limits<int64_t>::max();
      int64_t mx = std::numeric_limits<int64_t>::min();
      int64_t node_ahead = 0;
      int64_t tied = 0;
      int64_t ws_ahead = 0;
    };
    FreshnessWindow trade_fw;
    FreshnessWindow book_fw;
    constexpr int64_t kFreshnessSampleSecs = 3;
    constexpr int64_t kFreshnessWindowSecs = 300;
    constexpr int64_t kSamplesPerWindow = kFreshnessWindowSecs / kFreshnessSampleSecs;

    int64_t freshness_log_counter = 0;
    auto accumulate = [&](FreshnessWindow& fw, int64_t node_t, int64_t ws_t,
                          const char* label) {
      if (node_t == 0 || ws_t == 0) return;
      int64_t d = node_t - ws_t;
      ++fw.n;
      fw.sum += d;
      if (d < fw.mn) fw.mn = d;
      if (d > fw.mx) fw.mx = d;
      if (d > 0) ++fw.node_ahead;
      else if (d < 0) ++fw.ws_ahead;
      else ++fw.tied;
      if (fw.n >= kSamplesPerWindow) {
        int64_t mean = fw.sum / fw.n;
        LOG(INFO) << fmt::format(
            "HL {} freshness {}s window: n={} mean={:+}ms min={:+} max={:+} "
            "ahead=node {} / tied {} / ws {}",
            label, kFreshnessWindowSecs, fw.n, mean, fw.mn, fw.mx,
            fw.node_ahead, fw.tied, fw.ws_ahead);
        fw = {};
      }
    };
    auto sample_freshness = [&]() {
      if (!g_hl_node.ws_fallback_present) return;
      accumulate(trade_fw,
                 g_hl_node.last_node_trade_time_ms.load(std::memory_order_relaxed),
                 g_hl_node.last_ws_trade_time_ms.load(std::memory_order_relaxed),
                 "trade");
      accumulate(book_fw,
                 g_hl_node.last_event_time_ms.load(std::memory_order_relaxed),
                 g_hl_node.last_ws_book_event_time_ms.load(std::memory_order_relaxed),
                 "book");
    };
    while (!g_shutdown.load()) {
      std::this_thread::sleep_for(std::chrono::seconds(1));
      if (++freshness_log_counter % kFreshnessSampleSecs == 0) sample_freshness();
      if (!g_hl_node.ws_fallback_present) {
        // No fallback — stay node-primary forever, just log every minute.
        LOG_EVERY_N(INFO, 60)
            << fmt::format("HL node health: last_msg {} ms ago, last_event {} ms ago",
                           now_ms() - g_hl_node.last_msg_wall_ms.load(),
                           now_ms() - g_hl_node.last_event_time_ms.load());
        continue;
      }
      int64_t now = now_ms();
      int64_t silent_ms = now - g_hl_node.last_msg_wall_ms.load();
      int64_t lag_ms = now - g_hl_node.last_event_time_ms.load();
      bool silent = silent_ms > (int64_t)(g_hl_node.fallback_silent_s * 1000.0);
      bool lagging = lag_ms > (int64_t)(g_hl_node.fallback_lag_s * 1000.0);
      // Never been healthy yet → stay on WS until we get a msg.
      bool ever_received = g_hl_node.last_msg_wall_ms.load() > 0;
      bool healthy = ever_received && !silent && !lagging;
      if (healthy != last_healthy_state) {
        g_hl_node.node_healthy.store(healthy);
        // Rate-limited to avoid log spam under the low 1.5 s threshold,
        // where short slow-apply windows can cause many flips per minute.
        // The freshness-window log (every 5 min) is the proper signal.
        LOG_EVERY_N(INFO, 50) << fmt::format(
            "HL node_healthy flip: {} -> {} (silent_ms={} lag_ms={})",
            last_healthy_state, healthy, silent_ms, lag_ms);
        last_healthy_state = healthy;
      }
      // First time we detect unhealthy with WS fallback present, fire
      // a one-shot email so on-call knows the node feed is degraded. With
      // freshest-source arbitration the WS path naturally wins more often
      // during these windows — no fallback "switch" happens, just the
      // gate's per-message decision tilting toward WS.
      if (!healthy && !g_hl_node.unhealthy_alert_sent.exchange(true)) {
        try {
          pktrade::util::Mailer::instance().send_mail(
              "HL NODE FEED UNHEALTHY (pkmultifeed)",
              fmt::format(
                  "HL node feed went unhealthy:\n"
                  "  silent={} (silent_ms={}, threshold={}s)\n"
                  "  lagging={} (lag_ms={}, threshold={}s)\n"
                  "  WS path is winning the per-message arbitration during "
                  "these windows.\n"
                  "  (One alert per run; check the journal for further flips.)",
                  silent, silent_ms, g_hl_node.fallback_silent_s,
                  lagging, lag_ms, g_hl_node.fallback_lag_s));
        } catch (const std::exception& e) {
          LOG(ERROR) << "Failed to send HL node alert: " << e.what();
        }
      }
    }
  }).detach();
}

void subscribeBybitWebsockets(
    std::string market_name, rapidjson::Value& sub_config, std::vector<ix::WebSocket*>& websockets,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_ticker,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update_50,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update_200,

    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update_500,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_trd,
    std::mutex& t_mutex, zmq::socket_t& publisher,
    std::map<pktrade::Market, int64_t>& last_heartbeat_t,
    std::map<pktrade::Market, std::vector<std::string>>& sub_stream_names) {
  pktrade::Market stream_mkt;

  bool deflate = false;
  if (sub_config.HasMember("wssdeflate")) {
    deflate = sub_config["wssdeflate"].GetBool();
  }

  // I see. This got allocated on the stack and gets deallocated.
  // How annoying.
  // std::vector<std::string> sub_stream_names;

  std::string wss_endpoint = "stream.bybit.com";
  std::string wss_suffix;

  // TODO: Implementing BybitSPOT first, verifying works, then going to add
  // others later.
  if (market_name == "BybitSPOT") {
    stream_mkt = Market::BybitSPOT;
    last_heartbeat_t[stream_mkt] = 0;

    wss_suffix = "v5/public/spot";
    for (rapidjson::Value& sym : sub_config["symbols"].GetArray()) {
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.1.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.50.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.200.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("publicTrade.{}", sym.GetString()));

      mkt_sym_latest_ticker[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_50[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_200[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_500[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_trd[stream_mkt][sym.GetString()] = 0;
    }
  } else if (market_name == "BybitDeriv") {
    stream_mkt = Market::BybitDeriv;
    last_heartbeat_t[stream_mkt] = 0;

    wss_suffix = "v5/public/linear";
    for (rapidjson::Value& sym : sub_config["symbols"].GetArray()) {
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.1.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.50.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.200.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.500.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("publicTrade.{}", sym.GetString()));

      mkt_sym_latest_ticker[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_50[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_200[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_500[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_trd[stream_mkt][sym.GetString()] = 0;
    }

  } else if (market_name == "BybitInverseDeriv") {
    stream_mkt = Market::BybitInverseDeriv;
    last_heartbeat_t[stream_mkt] = 0;

    wss_suffix = "v5/public/inverse";
    for (rapidjson::Value& sym : sub_config["symbols"].GetArray()) {
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.1.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.50.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.200.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("orderbook.500.{}", sym.GetString()));
      sub_stream_names[stream_mkt].push_back(fmt::format("publicTrade.{}", sym.GetString()));

      mkt_sym_latest_ticker[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_50[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_200[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_update_500[stream_mkt][sym.GetString()] = 0;
      mkt_sym_latest_trd[stream_mkt][sym.GetString()] = 0;
    }
  }

  bool do_ip_lookup = false;
  if (sub_config.HasMember("do_ip_lookup")) {
    do_ip_lookup = sub_config["do_ip_lookup"].GetBool();
  }

  std::vector<std::string> wss_tgts =
      pktrade::get_ip_addresses(sub_config["wss_endpoint"].GetString());

  if (!do_ip_lookup) {
    wss_tgts = {wss_endpoint};
  } else {
    int max_websocket_ips = 3;
    if (sub_config.HasMember("max_websocket_ips")) {
      max_websocket_ips = sub_config["max_websocket_ips"].GetInt();
    }
    // Restrict to #threads.
    if (wss_tgts.size() > max_websocket_ips) {
      wss_tgts.resize(max_websocket_ips);
    }
  }

  for (std::string wss_tgt : wss_tgts) {
    std::string sub_url = pktrade::getWebsocketURI(wss_tgt, wss_suffix);
    // Subscribe, set the callback, run
    ix::WebSocket* ws = new ix::WebSocket();
    websockets.push_back(ws);
    ws->setUrl(sub_url);

    if (deflate) {
      ws->enablePerMessageDeflate();
    }
    ix::SocketTLSOptions tlsOptions;
    tlsOptions.caFile = "NONE";

    ws->setTLSOptions(tlsOptions);

    // TODO: Maybe make this a bit more graceful. Maybe config it from the
    // config?
    // Set a ping interval so we can restart the connection if it dies.
    ws->setPingInterval(60);
    ws->enableAutomaticReconnection();           // this is enabled by default but let's
                                                 // make it explicit
    ws->setMinWaitBetweenReconnectionRetries(1); // Set min wait for reconnect
                                                 // to 1ms for faster reconnect

    ws->setOnMessageCallback([&, mkt = stream_mkt, sub_stream_names = sub_stream_names](
                                 const ix::WebSocketMessagePtr& msg) {
      try {
        if (msg->type == ix::WebSocketMessageType::Message) {
          auto now = std::chrono::system_clock::now();

          // VLOG(2) << msg->str;
          std::lock_guard<std::mutex> lock(t_mutex);
          // TODO: figure out how to decode these gracefully for arbitrary
          // markets. This will be necessary when we start subbing to perps
          // data live.

          // TODO: use this to track the order of events.
          std::vector<pktrade::mdmsg::PbMessage> pb_msgs;
          if (mkt == pktrade::Market::BybitSPOT) {
            pb_msgs = pktrade::decodeBybitStreamData(msg->str, Market::BybitSPOT, false, false);
          } else if (mkt == pktrade::Market::BybitDeriv) {
            pb_msgs = pktrade::decodeBybitStreamData(msg->str, Market::BybitDeriv, false, false);
          } else if (mkt == pktrade::Market::BybitInverseDeriv) {
            pb_msgs =
                pktrade::decodeBybitStreamData(msg->str, Market::BybitInverseDeriv, false, false);
          }

          // Potentially send heartbeat.
          for (auto& one_msg : pb_msgs) {
            pktrade::publish(one_msg, &publisher);
          }

          int64_t now_s =
              std::chrono::duration_cast<std::chrono::seconds>(now.time_since_epoch()).count();
          if (now_s - last_heartbeat_t[mkt] > 19) {
            nlohmann::json hb;
            hb["op"] = "ping";
            ws->send(hb.dump());
            last_heartbeat_t[mkt] = now_s;
          }

        } else if (msg->type == ix::WebSocketMessageType::Pong) {
          // We get this every 60s (based on setPingInterval) so just log every 30min
          LOG_EVERY_N(INFO, 30) << "Got pong from Bybit: " << msg->str;

        } else if (msg->type == ix::WebSocketMessageType::Open) {
          LOG(INFO) << "Connection established to " << msg->openInfo.uri;

          // Make subscription requests.
          // Modify it to support subscriptions for multiple symbols.
          // For context, check the bybit docs.
          for (auto stream_name : sub_stream_names.at(mkt)) {
            nlohmann::json sub_details;
            sub_details["op"] = "subscribe";

            nlohmann::json streams_payload;
            streams_payload.push_back(stream_name.c_str());
            sub_details["args"] = streams_payload;
            sub_details["args"] = streams_payload;

            // Send the request payload.
            try {
              ws->send(sub_details.dump());
            } catch (const std::exception& e) {
              LOG(ERROR) << "Failed to send subscription request: " << e.what();
            }
          }

        } else if (msg->type == ix::WebSocketMessageType::Error) {
          LOG(ERROR) << "Connection error: " << msg->errorInfo.reason;
          std::cout << "Connection error: " << msg->errorInfo.reason << std::endl;
        } else if (msg->type == ix::WebSocketMessageType::Close) {
          LOG(INFO) << "Connection closed (Code: " << msg->closeInfo.code
                    << " Reason: " << msg->closeInfo.reason << ")" << std::endl;
          std::cout << "Connection closed (Code: " << msg->closeInfo.code
                    << " Reason: " << msg->closeInfo.reason << ")" << std::endl;
        }
      } catch (const std::exception& e) {
        LOG(ERROR) << "Error in Bybit websocket callback: " << e.what();
      } catch (...) {
        LOG(ERROR) << "Unknown exception in Bybit websocket callback";
      }
    }); // end ws onCallback

    std::cout << " Starting websocket " << std::endl;
    ws->start();
  }
}

////////////////////////////////////////////////////////////////////////////////////
// Databento feed state and subscription functions

struct DatabentoFeedState {
  std::map<uint32_t, std::string> idx_to_sym;
  std::map<std::string, uint32_t> sym_to_idx;
  std::map<std::string, int64_t> last_publish_time;      // wall-clock ms
  std::map<std::string, int64_t> sym_last_transact_time; // event time ms
  int64_t publish_interval_ms = 20;
  int64_t msg_count = 0;
  int64_t real_count = 0;
  int64_t bad_count = 0;
  bool subscribed = false;
  bool got_sym_map = false;

  // CME-only: roll blending
  // base_sym -> leg ("front"|"next") -> (bb, ba, bid_sz, ask_sz, ts_event_ms)
  std::map<std::string, std::map<std::string, std::tuple<double, double, int, int, int64_t>>>
      roll_latest;
  std::map<std::string, std::string> contract_to_leg; // db_sym -> "front"|"next"
  // base_sym -> (front_weight, next_weight)
  std::map<std::string, std::pair<double, double>> blend_weights;
  int64_t last_stale_alert_ms = 0;
  std::atomic<int64_t> last_msg_time_ms{0}; // for silent-disconnect watchdog
};

// Identifies which ingestion path observed a CME event. Used by
// processCmeQuote's stats recording and by log messages so you can tell
// whether the direct databento TCP connection or the UDP relay is winning
// the CAS race for each symbol.
enum class CmeSource : int {
  Tcp = 0,       // subscribeDatabentoCME's direct databento TCP callback
  UdpRelay = 1,  // subscribeRelayDbCme's UDP receiver
};

inline const char* cmeSourceName(CmeSource s) {
  return s == CmeSource::Tcp ? "TCP" : "UDP";
}
inline constexpr int kCmeSourceCount = 2;

// Per-(symbol, source) counters aggregated since pkmultifeed boot.
// Updated on the hot path via atomics; snapshotted by the stats-reporter
// thread for periodic LOG(INFO) dumps.
struct CmeSymStats {
  std::atomic<int64_t> events_seen{0};      // reached the CAS gate
  std::atomic<int64_t> cas_wins{0};         // won the CAS -> published
  std::atomic<int64_t> total_delay_ms{0};   // sum of delay on wins, for avg
  std::atomic<int64_t> max_delay_ms{0};     // max delay on wins
};

// Cross-source CAS gate + fusion stats for the CME fused feed. Both the
// direct databento TCP path and the UDP relay path call processCmeQuote;
// this state ensures we publish each CME event at most once regardless of
// which source delivers it first, and tracks per-source win / delay stats
// per output symbol so we can tell the two paths apart in the logs.
//
// Keyed by our_sym (the output symbol) so blend products dedupe on the
// merged event timestamp.
//
// Lookup is mutex-guarded but brief: we acquire the mutex just long enough
// to find the per-symbol atomic pointers, then drop it and do the CAS /
// atomic updates lock-free on the stable heap-allocated atomics. The
// unique_ptr values in the maps guarantee stable addresses across any
// subsequent map rehash.
//
// The mutex matters during startup: subWebsockets dispatches subscribe
// functions sequentially, but subscribeDatabentoCME spawns its callback
// thread before returning, so by the time subscribeRelayDbCme runs its
// register_symbol() calls the TCP callback thread may already be making
// try_claim() / stats calls. Without synchronisation, a concurrent
// try_emplace() that triggers rehash versus a mid-flight find() is UB.
struct CmeFusionState {
  std::unordered_map<std::string, std::unique_ptr<std::atomic<int64_t>>>
      last_published_ts_event_ms;
  std::unordered_map<std::string,
                     std::unique_ptr<std::array<CmeSymStats, kCmeSourceCount>>>
      per_sym_stats;
  std::mutex mtx;

  // For the stats reporter. Initialised lazily on first register_symbol().
  std::once_flag stats_once;
  std::chrono::steady_clock::time_point boot_tp =
      std::chrono::steady_clock::now();

  // Flips the detailed per-(sym, source) accounting + periodic dump on or
  // off. When false (default), record_seen / record_win early-return after
  // a single atomic-bool load and the reporter thread never spawns, so the
  // hot path pays nothing extra. Per-message delay log lines in the TCP
  // and UDP callbacks stay on regardless of this flag, so ms_behind is
  // always recoverable from the log.
  //
  // Must be set before register_symbol() is called on any symbol (i.e.
  // during main()'s CLI-parse step, before subWebsockets runs), so that
  // the once_flag-guarded reporter thread spawn observes the final value.
  std::atomic<bool> detailed_enabled{false};
  void set_detailed_stats_enabled(bool v) {
    detailed_enabled.store(v, std::memory_order_relaxed);
  }

  // Idempotent. Safe to call concurrently during startup. Also kicks off
  // the stats reporter thread on first call if --cme-fusion-stats is on.
  void register_symbol(const std::string& sym) {
    {
      std::lock_guard<std::mutex> lock(mtx);
      last_published_ts_event_ms.try_emplace(
          sym, std::make_unique<std::atomic<int64_t>>(0));
      per_sym_stats.try_emplace(
          sym, std::make_unique<std::array<CmeSymStats, kCmeSourceCount>>());
    }
    if (detailed_enabled.load(std::memory_order_relaxed)) {
      ensure_stats_thread();
    }
  }

// Returns true iff this thread wins (caller should publish); false
// means a newer-or-equal event was already published by another source,
// so caller should drop.
//
// Important semantic: equality is treated as a duplicate. We dedupe on
// (our_sym, ts_event_ms) only; if two candidate publishes for the same
// output symbol carry the same event timestamp but differ in content, the
// later one is dropped. This is intentional for the current CME fusion
// model: the feed is "newest event timestamp wins", not "last differing
// payload wins".
  //
  // Post-setup the mutex is uncontended (no more register_symbol calls),
  // so acquisition is a handful of ns. The CAS loop after the unlock is
  // fully lock-free.
  bool try_claim(const std::string& sym, int64_t ts_event_ms) {
    std::atomic<int64_t>* atom = nullptr;
    {
      std::lock_guard<std::mutex> lock(mtx);
      auto it = last_published_ts_event_ms.find(sym);
      if (it != last_published_ts_event_ms.end()) {
        atom = it->second.get();
      }
    }
    if (atom == nullptr) {
      // Not registered - degrade to "publish" rather than silently drop.
      // In practice means a symbol leaked past setup, which is a bug.
      LOG_EVERY_N(WARNING, 1000)
          << "CmeFusionState: unregistered symbol '" << sym << "', publishing anyway";
      return true;
    }
    int64_t prev = atom->load(std::memory_order_acquire);
    while (ts_event_ms > prev) {
      if (atom->compare_exchange_weak(prev, ts_event_ms, std::memory_order_acq_rel,
                                      std::memory_order_acquire)) {
        return true;
      }
      // CAS failed: prev was updated to the current stored value; the
      // loop condition re-checks whether our ts_event_ms is still newer.
    }
    return false;
  }

  // Returns a raw pointer to the per-(sym, source) stats block, or
  // nullptr if the symbol wasn't registered. Stable for the life of the
  // process because it's a unique_ptr target in the map.
  CmeSymStats* get_stats(const std::string& sym, CmeSource source) {
    std::array<CmeSymStats, kCmeSourceCount>* arr = nullptr;
    {
      std::lock_guard<std::mutex> lock(mtx);
      auto it = per_sym_stats.find(sym);
      if (it != per_sym_stats.end()) {
        arr = it->second.get();
      }
    }
    if (arr == nullptr) return nullptr;
    return &(*arr)[static_cast<int>(source)];
  }

  // Atomic "store max" helper (C++17; C++20 atomic has fetch_max native).
  static void atomic_store_max(std::atomic<int64_t>& a, int64_t v) {
    int64_t cur = a.load(std::memory_order_relaxed);
    while (v > cur &&
           !a.compare_exchange_weak(cur, v, std::memory_order_relaxed,
                                    std::memory_order_relaxed)) {
    }
  }

  // Called on each message that reached the CAS gate, regardless of win.
  // No-op unless --cme-fusion-stats is enabled at startup.
  void record_seen(const std::string& sym, CmeSource source) {
    if (!detailed_enabled.load(std::memory_order_relaxed)) return;
    if (auto* s = get_stats(sym, source)) {
      s->events_seen.fetch_add(1, std::memory_order_relaxed);
    }
  }

  // Called when this source wins the CAS and publishes.
  // No-op unless --cme-fusion-stats is enabled at startup.
  void record_win(const std::string& sym, CmeSource source, int64_t delay_ms) {
    if (!detailed_enabled.load(std::memory_order_relaxed)) return;
    if (auto* s = get_stats(sym, source)) {
      s->cas_wins.fetch_add(1, std::memory_order_relaxed);
      if (delay_ms > 0) {
        s->total_delay_ms.fetch_add(delay_ms, std::memory_order_relaxed);
        atomic_store_max(s->max_delay_ms, delay_ms);
      }
    }
  }

  // Snapshot all per-symbol stats and dump to LOG(INFO). Called periodically
  // and once on shutdown.
  void dump_stats(const char* reason) {
    auto uptime = std::chrono::steady_clock::now() - boot_tp;
    auto uptime_s =
        std::chrono::duration_cast<std::chrono::seconds>(uptime).count();
    auto hh = uptime_s / 3600;
    auto mm = (uptime_s % 3600) / 60;
    auto ss = uptime_s % 60;

    // Snapshot symbol list under the lock, then release and read atomics.
    std::vector<std::string> syms;
    {
      std::lock_guard<std::mutex> lock(mtx);
      syms.reserve(per_sym_stats.size());
      for (const auto& [sym, _] : per_sym_stats) syms.push_back(sym);
    }
    std::sort(syms.begin(), syms.end());

    std::ostringstream oss;
    oss << fmt::format("CME fusion stats ({}) uptime {}h{:02}m{:02}s:\n",
                       reason, hh, mm, ss);
    for (const auto& sym : syms) {
      for (int i = 0; i < kCmeSourceCount; ++i) {
        auto src = static_cast<CmeSource>(i);
        auto* s = get_stats(sym, src);
        if (s == nullptr) continue;
        int64_t ev = s->events_seen.load(std::memory_order_relaxed);
        int64_t wins = s->cas_wins.load(std::memory_order_relaxed);
        int64_t sumd = s->total_delay_ms.load(std::memory_order_relaxed);
        int64_t maxd = s->max_delay_ms.load(std::memory_order_relaxed);
        double win_rate = ev > 0 ? 100.0 * wins / ev : 0.0;
        int64_t avg = wins > 0 ? sumd / wins : 0;
        oss << fmt::format(
            "  {:<6s} {}: ev={} wins={} ({:.1f}%) avg_delay={}ms max={}ms\n",
            sym, cmeSourceName(src), ev, wins, win_rate, avg, maxd);
      }
    }
    LOG(INFO) << oss.str();
  }

  // Lazily spawn the reporter thread. Thread is pushed into g_db_threads
  // so main's shutdown join picks it up; on g_shutdown the thread wakes
  // within 1s, emits one final dump, and exits.
  void ensure_stats_thread();
};

CmeFusionState& cmeFusionState() {
  static CmeFusionState instance;
  return instance;
}

// Keep LiveThreaded clients alive for the lifetime of the program
static std::list<std::unique_ptr<databento::LiveThreaded>> g_db_clients;
static std::vector<std::thread> g_db_threads;
static std::mutex g_db_threads_mtx;

// Interval between periodic fusion-stats dumps during a session. Also the
// granularity at which the reporter thread checks g_shutdown; keep the
// sleep chunk small so shutdown latency stays bounded.
constexpr std::chrono::minutes kCmeStatsIntervalMin{5};
constexpr std::chrono::seconds kCmeStatsTickSec{1};

void CmeFusionState::ensure_stats_thread() {
  std::call_once(stats_once, [this]() {
    std::lock_guard<std::mutex> lock(g_db_threads_mtx);
    g_db_threads.emplace_back([this]() {
      auto last_dump = std::chrono::steady_clock::now();
      while (!g_shutdown.load()) {
        std::this_thread::sleep_for(kCmeStatsTickSec);
        auto now = std::chrono::steady_clock::now();
        if (now - last_dump >= kCmeStatsIntervalMin) {
          dump_stats("periodic");
          last_dump = now;
        }
      }
      // Final flush on shutdown so EOD always has a snapshot even if it
      // lands between periodic ticks.
      dump_stats("shutdown");
    });
  });
}

void subscribeDatabentoEQ(rapidjson::Value& sub_config, const std::string& api_key,
                          zmq::socket_t& publisher) {
  if (!sub_config.HasMember("symbols") || sub_config["symbols"].GetArray().Empty()) {
    LOG(WARNING) << "DataBentoEQ: no symbols configured";
    return;
  }

  std::vector<std::string> symbols;
  for (auto& sym : sub_config["symbols"].GetArray()) {
    symbols.push_back(sym.GetString());
  }

  LOG(INFO) << "DataBentoEQ: subscribing to " << symbols.size() << " symbols";
  for (const auto& s : symbols) {
    LOG(INFO) << "  EQ sym: " << s;
  }

  auto* pub_ptr = &publisher;
  // Reserve a stable slot in g_db_clients for the EQ client (list iterators
  // remain valid across insertions, so the runner thread can safely reset it).
  auto client_it = g_db_clients.emplace(g_db_clients.end());

  auto runner = [symbols, pub_ptr, client_it, api_key]() mutable {
    constexpr int64_t MAX_BAD_BOOK_T_SECS = 60;

    while (!g_shutdown.load()) {
      try {
        auto state = std::make_shared<DatabentoFeedState>();
        auto eq_bad_book_start = std::make_shared<int64_t>(0);

        auto& client_ptr = *client_it;
        try {
          client_ptr = std::make_unique<databento::LiveThreaded>(
              databento::LiveThreaded::Builder()
                  .SetKey(api_key)
                  .SetDataset("XNAS.BASIC")
                  .SetSendTsOut(true)
                  // 16MB (<<20 is 1mb)
                  .SetBufferSize(1 << 24)
                  .SetSlowReaderBehavior(databento::SlowReaderBehavior::Skip)
                  .BuildThreaded());

          // Combine the base symbols with any intraday-added extras. Rebuilt on
          // every (re)connect so extras are re-applied after any reconnect
          // (silent-feed fallback, stale restart, etc.). Mark them applied so the
          // callback's live-subscribe drain doesn't re-send the same ones.
          std::vector<std::string> extras = extraSymsFor("DataBentoEquities");
          std::vector<std::string> subs = symbols;
          for (const auto& extra_sym : extras) subs.push_back(extra_sym);
          client_ptr->Subscribe(subs, databento::Schema::Cmbp1, databento::SType::RawSymbol);
          g_eq_extra.applied.store(extras.size(), std::memory_order_release);
        } catch (const std::exception& e) {
          LOG(ERROR) << "DataBentoEQ: failed to initialize client: " << e.what();
          client_ptr.reset();
          std::this_thread::sleep_for(std::chrono::seconds(5));
          continue;
        }

        auto restart_requested = std::make_shared<std::atomic<bool>>(false);
        LOG(INFO) << "DataBentoEQ: client started";
        client_ptr->Start([state, pub_ptr, eq_bad_book_start, restart_requested,
                           client_raw = client_ptr.get()](
                              const databento::Record& record) -> databento::KeepGoing {
          try {
          state->msg_count++;

          if (pktrade::g_shutdown.load()) {
            return databento::KeepGoing::Stop;
          }

          // Live-subscribe any intraday-added symbols (no reconnect). Safe here
          // because this callback runs on the client's own internal thread.
          drainDbExtras(g_eq_extra, client_raw, "DataBentoEquities", databento::Schema::Cmbp1,
                        [](const std::string& s) { return std::vector<std::string>{s}; });

          // SymbolMappingMsg
          if (auto* sym_map = record.GetIf<databento::SymbolMappingMsg>()) {
            std::string stype_in(sym_map->stype_in_symbol.data());
            uint32_t iid = sym_map->hd.instrument_id;
            state->idx_to_sym[iid] = stype_in;
            state->sym_to_idx[stype_in] = iid;
            state->got_sym_map = true;
            LOG(INFO) << "EQ SymbolMapping: " << stype_in << " -> " << iid;
            return databento::KeepGoing::Continue;
          }

          // SystemMsg
          if (auto* sys_msg = record.GetIf<databento::SystemMsg>()) {
            std::string msg(sys_msg->msg.data());
            if (msg.find("Subscription") != std::string::npos &&
                msg.find("succeeded") != std::string::npos) {
              state->subscribed = true;
            }
            LOG(INFO) << "EQ System: " << msg;
            return databento::KeepGoing::Continue;
          }

          // StatusMsg
          if (record.Holds<databento::StatusMsg>()) {
            LOG(INFO) << "EQ Status message received";
            return databento::KeepGoing::Continue;
          }

          // ErrorMsg
          if (auto* err_msg = record.GetIf<databento::ErrorMsg>()) {
            LOG(ERROR) << "EQ Error: " << std::string(err_msg->err.data());
            return databento::KeepGoing::Continue;
          }

          // CbboMsg (cmbp-1 schema)
          auto cbbo_view = extractRecordView<databento::Cmbp1Msg>(record);
          if (cbbo_view.msg != nullptr) {
            const auto* cbbo = cbbo_view.msg;
            state->real_count++;

            auto sym_it = state->idx_to_sym.find(cbbo->hd.instrument_id);
            if (sym_it == state->idx_to_sym.end()) {
              LOG(WARNING) << "EQ CMBP1 without mapping: instrument_id=" << cbbo->hd.instrument_id;
              return databento::KeepGoing::Continue;
            }
            const std::string& sym = sym_it->second;

            // Throttle
            auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                              std::chrono::system_clock::now().time_since_epoch())
                              .count();
            state->last_msg_time_ms.store(now_ms);
            auto last_pub_it = state->last_publish_time.find(sym);
            if (last_pub_it != state->last_publish_time.end() &&
                now_ms - last_pub_it->second < state->publish_interval_ms) {

              return databento::KeepGoing::Continue;
            }

            // Bad count restart check
            if (state->bad_count >= kMaxDatabentoBadMsgs) {
              const int64_t bad_count = state->bad_count;
              state->bad_count = 0;
              auto err_msg = fmt::format(
                  "EQ: {} bad DataBento messages received (threshold {}), restarting feed",
                  bad_count, kMaxDatabentoBadMsgs);
              LOG(ERROR) << err_msg;
              try {
                pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_EQ DOWN", err_msg);
              } catch (const std::exception& e) {
                LOG(ERROR) << "Failed to send EQ bad-message alert: " << e.what();
              }
              restart_requested->store(true);
              return databento::KeepGoing::Stop;
            }

            // UNDEF checks
            uint64_t ts_event_raw = cbbo->hd.ts_event.time_since_epoch().count();
            uint64_t ts_recv_raw = cbbo->ts_recv.time_since_epoch().count();
            if (cbbo->price == databento::kUndefPrice ||
                ts_event_raw == databento::kUndefTimestamp ||
                ts_recv_raw == databento::kUndefTimestamp) {

              return databento::KeepGoing::Continue;
            }

            // EQ-specific: skip if bid or ask is UNDEF (happens often on equities)
            if (static_cast<int64_t>(cbbo->levels[0].bid_px) == databento::kUndefPrice ||
                static_cast<int64_t>(cbbo->levels[0].ask_px) == databento::kUndefPrice) {

              return databento::KeepGoing::Continue;
            }

            // F_SNAPSHOT check
            if (cbbo->flags.IsSnapshot()) {
              LOG(WARNING) << "EQ: got snapshot msg for " << sym;
              return databento::KeepGoing::Continue;
            }

            // F_MAYBE_BAD_BOOK with timeout tolerance
            if (cbbo->flags.IsMaybeBadBook()) {
              if (*eq_bad_book_start == 0) {
                *eq_bad_book_start = now_ms / 1000; // seconds
              }
              int64_t now_secs = now_ms / 1000;
              if (now_secs - *eq_bad_book_start <= MAX_BAD_BOOK_T_SECS) {
                state->bad_count++;
                return databento::KeepGoing::Continue;
              }
              // Exceeded timeout, accept the data anyway
            } else {
              *eq_bad_book_start = 0;
            }

            if (cbbo->flags.IsPublisherSpecific()) {
              LOG_EVERY_N(WARNING, 101) << "EQ: publisher_specific flag set for " << sym;
            }

            // Convert prices (databento uses 1e-9 fixed point)
            constexpr double PX_CONVERT = 1e-9;
            double px = std::abs(cbbo->price * PX_CONVERT);
            double bb_px = cbbo->levels[0].bid_px * PX_CONVERT;
            double ba_px = cbbo->levels[0].ask_px * PX_CONVERT;
            int bid_sz = cbbo->levels[0].bid_sz;
            int ask_sz = cbbo->levels[0].ask_sz;

            // Price sanity
            if (px > 1e6 || bb_px > 1e6 || ba_px > 1e6) {
              LOG(WARNING) << "EQ: unusable price for " << sym;
              return databento::KeepGoing::Continue;
            }

            int64_t ts_event_ms = static_cast<int64_t>(ts_event_raw / 1000 / 1000);

            if (!isInUsEquitySession(ts_event_ms)) {
              return databento::KeepGoing::Continue;
            }

            // Don't publish if we have more up-to-date data
            auto last_ts_it = state->sym_last_transact_time.find(sym);
            if (last_ts_it != state->sym_last_transact_time.end() &&
                ts_event_ms < last_ts_it->second) {
              return databento::KeepGoing::Continue;
            }

            // Build and publish PbQuote
            mdmsg::PbMessage pbmsg;
            pbmsg.set_market(mdmsg::PBMARKET_TOPBOOK_EQUITY);
            pbmsg.set_symbol_id(sym);

            auto* pbquote = pbmsg.mutable_quote();
            pbquote->set_best_bid_px(std::to_string(bb_px));
            pbquote->set_best_bid_qty(std::to_string(bid_sz));
            pbquote->set_best_ask_px(std::to_string(ba_px));
            pbquote->set_best_ask_qty(std::to_string(ask_sz));
            pbquote->set_mid_px((bb_px + ba_px) / 2.0);
            pbquote->set_transact_time(ts_event_ms);

            publish(pbmsg, pub_ptr);

            // Logging
            int64_t recv_delay_ms = now_ms - ts_event_ms;
            int64_t ts_out_delay_ms = cbbo_view.has_ts_out ? now_ms - cbbo_view.ts_out_ms : -1;
            if (state->msg_count % 100 == 0 || recv_delay_ms > 1000) {
              std::string ts_out_delay_str =
                  cbbo_view.has_ts_out ? std::to_string(ts_out_delay_ms) : "NA";
              LOG(INFO) << fmt::format("EQ {} msgtime {} delay_ms {} ts_out_delay {}", sym,
                                       ts_event_ms, recv_delay_ms, ts_out_delay_str);
            }

            // Stale feed detection — restart on excessive delay
            if (recv_delay_ms > kEqStaleThresholdMs) {
              std::string err_msg = fmt::format("EQ {} delay {}ms exceeds {}ms threshold", sym,
                                                recv_delay_ms, kEqStaleThresholdMs);
              LOG(ERROR) << err_msg;
              if (now_ms - state->last_stale_alert_ms > kStaleAlertIntervalMs) {
                state->last_stale_alert_ms = now_ms;
                try {
                  pktrade::util::Mailer::instance().send_mail("EQ FEED STALE", err_msg);
                } catch (const std::exception& e) {
                  LOG(ERROR) << "Failed to send EQ stale alert: " << e.what();
                }
              }
              restart_requested->store(true);
              return databento::KeepGoing::Stop;
            }

            state->sym_last_transact_time[sym] = ts_event_ms;
            state->last_publish_time[sym] = now_ms;
          }

          return databento::KeepGoing::Continue;
          } catch (const std::exception& e) {
            LOG(ERROR) << "EQ callback exception: " << e.what();
            restart_requested->store(true);
            return databento::KeepGoing::Stop;
          } catch (...) {
            LOG(ERROR) << "EQ callback unknown exception";
            restart_requested->store(true);
            return databento::KeepGoing::Stop;
          }
        });

        const auto watchdog_timeout = std::chrono::seconds(5);
        const auto eq_watchdog = [&]() {
          int64_t last = state->last_msg_time_ms.load();
          if (last == 0) {
            return;
          }
          auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::system_clock::now().time_since_epoch())
                            .count();
          if (!isInUsEquitySession(now_ms)) {
            return;
          }
          if (now_ms - last > kEqSilentThresholdMs) {
            LOG(ERROR) << fmt::format("EQ watchdog: no data for {}ms (threshold {}ms), restarting",
                                      now_ms - last, kEqSilentThresholdMs);
            try {
              pktrade::util::Mailer::instance().send_mail(
                  "EQ FEED SILENT", fmt::format("No EQ data for {}ms", now_ms - last));
            } catch (const std::exception& e) {
              LOG(ERROR) << "Failed to send EQ silent alert: " << e.what();
            }
            restart_requested->store(true);
            client_ptr.reset();
          }
        };
        int64_t extras_pending_since = 0;
        while (!g_shutdown.load() && client_ptr) {
          auto rc = client_ptr->BlockForStop(watchdog_timeout);
          if (rc == databento::KeepGoing::Stop) {
            break;
          }
          // Extras are normally live-subscribed by the record callback; fall back
          // to a reconnect if that hasn't happened within the timeout.
          if (dbExtrasReconnectDue(g_eq_extra, extras_pending_since)) {
            LOG(INFO) << "DataBentoEQ: extras not live-subscribed within 10s, reconnecting";
            client_ptr.reset();
            break;
          }
          eq_watchdog();
        }
        if (client_ptr) {
          client_ptr.reset();
        }

        if (g_shutdown.load()) {
          LOG(INFO) << "DataBentoEQ client stopped (shutdown)";
          break;
        }
        if (!restart_requested->load()) {
          LOG(WARNING) << "DataBentoEQ connection lost unexpectedly, restarting";
        } else {
          LOG(WARNING) << "DataBentoEQ restarting after stale/silent feed";
        }
        std::this_thread::sleep_for(std::chrono::seconds(5));
      } catch (const std::exception& e) {
        LOG(ERROR) << "DataBentoEQ fatal error: " << e.what();
        try {
          pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_EQ DOWN",
                                                      std::string("Fatal exception: ") + e.what());
        } catch (const std::exception& mail_ex) {
          LOG(ERROR) << "Failed to send EQ fatal alert: " << mail_ex.what();
        }
        (*client_it).reset();
        std::this_thread::sleep_for(std::chrono::seconds(5));
        continue;
      } catch (...) {
        LOG(ERROR) << "DataBentoEQ fatal error: unknown exception";
        try {
          pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_EQ DOWN",
                                                      "Unknown fatal exception");
        } catch (const std::exception& mail_ex) {
          LOG(ERROR) << "Failed to send EQ fatal alert: " << mail_ex.what();
        }
        (*client_it).reset();
        std::this_thread::sleep_for(std::chrono::seconds(5));
        continue;
      }
    }

    (*client_it).reset();
  };

  {
    std::lock_guard<std::mutex> lock(g_db_threads_mtx);
    g_db_threads.emplace_back(std::move(runner));
  }
}

// Subscribes to BOATS (Blue Ocean ATS) overnight US equities via Databento OCEA.MEMOIR
// mbp-1. Mirrors subscribeDatabentoEQ but publishes to the same TopBookEquity protobuf
// market (sessions don't overlap: EQ 04:00-20:00 ET, BOATS 20:00-04:00 ET Sun-Thu).
void subscribeDatabentoBoats(rapidjson::Value& sub_config, const std::string& api_key,
                             zmq::socket_t& publisher) {
  if (!sub_config.HasMember("symbols") || sub_config["symbols"].GetArray().Empty()) {
    LOG(WARNING) << "DataBentoBoats: no symbols configured";
    return;
  }

  std::vector<std::string> symbols;
  for (auto& sym : sub_config["symbols"].GetArray()) {
    symbols.push_back(sym.GetString());
  }

  LOG(INFO) << "DataBentoBoats: subscribing to " << symbols.size() << " symbols";
  for (const auto& s : symbols) {
    LOG(INFO) << "  BOATS sym: " << s;
  }

  auto* pub_ptr = &publisher;
  auto client_it = g_db_clients.emplace(g_db_clients.end());

  auto runner = [symbols, pub_ptr, client_it, api_key]() mutable {
    constexpr int64_t MAX_BAD_BOOK_T_SECS = 60;

    while (!g_shutdown.load()) {
      try {
        auto state = std::make_shared<DatabentoFeedState>();
        auto boats_bad_book_start = std::make_shared<int64_t>(0);

        auto& client_ptr = *client_it;
        try {
          client_ptr = std::make_unique<databento::LiveThreaded>(
              databento::LiveThreaded::Builder()
                  .SetKey(api_key)
                  .SetDataset("OCEA.MEMOIR")
                  .SetSendTsOut(true)
                  .SetBufferSize(1 << 24)
                  .SetSlowReaderBehavior(databento::SlowReaderBehavior::Skip)
                  .BuildThreaded());

          // Combine the base symbols with any intraday-added extras. Rebuilt on
          // every (re)connect so extras are re-applied after any reconnect. Mark
          // them applied so the callback's live-subscribe drain doesn't re-send.
          std::vector<std::string> extras = extraSymsFor("DataBentoBoats");
          std::vector<std::string> subs = symbols;
          for (const auto& extra_sym : extras) subs.push_back(extra_sym);
          client_ptr->Subscribe(subs, databento::Schema::Mbp1, databento::SType::RawSymbol);
          g_boats_extra.applied.store(extras.size(), std::memory_order_release);
        } catch (const std::exception& e) {
          LOG(ERROR) << "DataBentoBoats: failed to initialize client: " << e.what();
          client_ptr.reset();
          std::this_thread::sleep_for(std::chrono::seconds(5));
          continue;
        }

        auto restart_requested = std::make_shared<std::atomic<bool>>(false);
        LOG(INFO) << "DataBentoBoats: client started";
        client_ptr->Start([state, pub_ptr, boats_bad_book_start, restart_requested,
                           client_raw = client_ptr.get()](
                              const databento::Record& record) -> databento::KeepGoing {
          try {
          state->msg_count++;

          if (pktrade::g_shutdown.load()) {
            return databento::KeepGoing::Stop;
          }

          // Live-subscribe any intraday-added symbols (no reconnect). Safe here
          // because this callback runs on the client's own internal thread.
          drainDbExtras(g_boats_extra, client_raw, "DataBentoBoats", databento::Schema::Mbp1,
                        [](const std::string& s) { return std::vector<std::string>{s}; });

          if (auto* sym_map = record.GetIf<databento::SymbolMappingMsg>()) {
            std::string stype_in(sym_map->stype_in_symbol.data());
            uint32_t iid = sym_map->hd.instrument_id;
            state->idx_to_sym[iid] = stype_in;
            state->sym_to_idx[stype_in] = iid;
            state->got_sym_map = true;
            LOG(INFO) << "BOATS SymbolMapping: " << stype_in << " -> " << iid;
            return databento::KeepGoing::Continue;
          }

          if (auto* sys_msg = record.GetIf<databento::SystemMsg>()) {
            std::string msg(sys_msg->msg.data());
            if (msg.find("Subscription") != std::string::npos &&
                msg.find("succeeded") != std::string::npos) {
              state->subscribed = true;
            }
            LOG(INFO) << "BOATS System: " << msg;
            return databento::KeepGoing::Continue;
          }

          if (record.Holds<databento::StatusMsg>()) {
            LOG(INFO) << "BOATS Status message received";
            return databento::KeepGoing::Continue;
          }

          if (auto* err_msg = record.GetIf<databento::ErrorMsg>()) {
            LOG(ERROR) << "BOATS Error: " << std::string(err_msg->err.data());
            return databento::KeepGoing::Continue;
          }

          // Mbp1Msg (mbp-1 schema; same levels[0] shape as Cmbp1)
          auto mbp1_view = extractRecordView<databento::Mbp1Msg>(record);
          if (mbp1_view.msg != nullptr) {
            const auto* mbp1 = mbp1_view.msg;
            state->real_count++;

            auto sym_it = state->idx_to_sym.find(mbp1->hd.instrument_id);
            if (sym_it == state->idx_to_sym.end()) {
              LOG(WARNING) << "BOATS MBP1 without mapping: instrument_id=" << mbp1->hd.instrument_id;
              return databento::KeepGoing::Continue;
            }
            const std::string& sym = sym_it->second;

            auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                              std::chrono::system_clock::now().time_since_epoch())
                              .count();
            state->last_msg_time_ms.store(now_ms);
            auto last_pub_it = state->last_publish_time.find(sym);
            if (last_pub_it != state->last_publish_time.end() &&
                now_ms - last_pub_it->second < state->publish_interval_ms) {
              return databento::KeepGoing::Continue;
            }

            if (state->bad_count >= kMaxDatabentoBadMsgs) {
              const int64_t bad_count = state->bad_count;
              state->bad_count = 0;
              auto err_msg = fmt::format(
                  "BOATS: {} bad DataBento messages received (threshold {}), restarting feed",
                  bad_count, kMaxDatabentoBadMsgs);
              LOG(ERROR) << err_msg;
              try {
                pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_BOATS DOWN", err_msg);
              } catch (const std::exception& e) {
                LOG(ERROR) << "Failed to send BOATS bad-message alert: " << e.what();
              }
              restart_requested->store(true);
              return databento::KeepGoing::Stop;
            }

            uint64_t ts_event_raw = mbp1->hd.ts_event.time_since_epoch().count();
            uint64_t ts_recv_raw = mbp1->ts_recv.time_since_epoch().count();
            if (mbp1->price == databento::kUndefPrice ||
                ts_event_raw == databento::kUndefTimestamp ||
                ts_recv_raw == databento::kUndefTimestamp) {
              return databento::KeepGoing::Continue;
            }

            if (static_cast<int64_t>(mbp1->levels[0].bid_px) == databento::kUndefPrice ||
                static_cast<int64_t>(mbp1->levels[0].ask_px) == databento::kUndefPrice) {
              return databento::KeepGoing::Continue;
            }

            if (mbp1->flags.IsSnapshot()) {
              LOG(WARNING) << "BOATS: got snapshot msg for " << sym;
              return databento::KeepGoing::Continue;
            }

            if (mbp1->flags.IsMaybeBadBook()) {
              if (*boats_bad_book_start == 0) {
                *boats_bad_book_start = now_ms / 1000;
              }
              int64_t now_secs = now_ms / 1000;
              if (now_secs - *boats_bad_book_start <= MAX_BAD_BOOK_T_SECS) {
                state->bad_count++;
                return databento::KeepGoing::Continue;
              }
            } else {
              *boats_bad_book_start = 0;
            }

            if (mbp1->flags.IsPublisherSpecific()) {
              LOG_EVERY_N(WARNING, 101) << "BOATS: publisher_specific flag set for " << sym;
            }

            constexpr double PX_CONVERT = 1e-9;
            double px = std::abs(mbp1->price * PX_CONVERT);
            double bb_px = mbp1->levels[0].bid_px * PX_CONVERT;
            double ba_px = mbp1->levels[0].ask_px * PX_CONVERT;
            int bid_sz = mbp1->levels[0].bid_sz;
            int ask_sz = mbp1->levels[0].ask_sz;

            if (px > 1e6 || bb_px > 1e6 || ba_px > 1e6) {
              LOG(WARNING) << "BOATS: unusable price for " << sym;
              return databento::KeepGoing::Continue;
            }

            int64_t ts_event_ms = static_cast<int64_t>(ts_event_raw / 1000 / 1000);

            if (!isInBoatsSession(ts_event_ms)) {
              return databento::KeepGoing::Continue;
            }

            auto last_ts_it = state->sym_last_transact_time.find(sym);
            if (last_ts_it != state->sym_last_transact_time.end() &&
                ts_event_ms < last_ts_it->second) {
              return databento::KeepGoing::Continue;
            }

            // Publish under the SAME TopBookEquity protobuf market as the EQ feed.
            mdmsg::PbMessage pbmsg;
            pbmsg.set_market(mdmsg::PBMARKET_TOPBOOK_EQUITY);
            pbmsg.set_symbol_id(sym);

            auto* pbquote = pbmsg.mutable_quote();
            pbquote->set_best_bid_px(std::to_string(bb_px));
            pbquote->set_best_bid_qty(std::to_string(bid_sz));
            pbquote->set_best_ask_px(std::to_string(ba_px));
            pbquote->set_best_ask_qty(std::to_string(ask_sz));
            pbquote->set_mid_px((bb_px + ba_px) / 2.0);
            pbquote->set_transact_time(ts_event_ms);

            publish(pbmsg, pub_ptr);

            int64_t recv_delay_ms = now_ms - ts_event_ms;
            int64_t ts_out_delay_ms = mbp1_view.has_ts_out ? now_ms - mbp1_view.ts_out_ms : -1;
            if (state->msg_count % 100 == 0 || recv_delay_ms > 1000) {
              std::string ts_out_delay_str =
                  mbp1_view.has_ts_out ? std::to_string(ts_out_delay_ms) : "NA";
              LOG(INFO) << fmt::format("BOATS {} msgtime {} delay_ms {} ts_out_delay {}", sym,
                                       ts_event_ms, recv_delay_ms, ts_out_delay_str);
            }

            // No per-message stale-data restart: BOATS overnight has long quiet stretches.
            // The silent-watchdog below (no msgs for >threshold during session) covers true feed-down.

            state->sym_last_transact_time[sym] = ts_event_ms;
            state->last_publish_time[sym] = now_ms;
          }

          return databento::KeepGoing::Continue;
          } catch (const std::exception& e) {
            LOG(ERROR) << "BOATS callback exception: " << e.what();
            restart_requested->store(true);
            return databento::KeepGoing::Stop;
          } catch (...) {
            LOG(ERROR) << "BOATS callback unknown exception";
            restart_requested->store(true);
            return databento::KeepGoing::Stop;
          }
        });

        const auto watchdog_timeout = std::chrono::seconds(5);
        const auto boats_watchdog = [&]() {
          int64_t last = state->last_msg_time_ms.load();
          if (last == 0) {
            return;
          }
          auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::system_clock::now().time_since_epoch())
                            .count();
          if (!isInBoatsSession(now_ms)) {
            return;
          }
          if (now_ms - last > kBoatsSilentThresholdMs) {
            LOG(ERROR) << fmt::format("BOATS watchdog: no data for {}ms (threshold {}ms), restarting",
                                      now_ms - last, kBoatsSilentThresholdMs);
            try {
              pktrade::util::Mailer::instance().send_mail(
                  "BOATS FEED SILENT", fmt::format("No BOATS data for {}ms", now_ms - last));
            } catch (const std::exception& e) {
              LOG(ERROR) << "Failed to send BOATS silent alert: " << e.what();
            }
            restart_requested->store(true);
            client_ptr.reset();
          }
        };
        int64_t extras_pending_since = 0;
        while (!g_shutdown.load() && client_ptr) {
          auto rc = client_ptr->BlockForStop(watchdog_timeout);
          if (rc == databento::KeepGoing::Stop) {
            break;
          }
          // Extras are normally live-subscribed by the record callback; fall back
          // to a reconnect if that hasn't happened within the timeout.
          if (dbExtrasReconnectDue(g_boats_extra, extras_pending_since)) {
            LOG(INFO) << "DataBentoBoats: extras not live-subscribed within 10s, reconnecting";
            client_ptr.reset();
            break;
          }
          boats_watchdog();
        }
        if (client_ptr) {
          client_ptr.reset();
        }

        if (g_shutdown.load()) {
          LOG(INFO) << "DataBentoBoats client stopped (shutdown)";
          break;
        }
        if (!restart_requested->load()) {
          LOG(WARNING) << "DataBentoBoats connection lost unexpectedly, restarting";
        } else {
          LOG(WARNING) << "DataBentoBoats restarting after stale/silent feed";
        }
        std::this_thread::sleep_for(std::chrono::seconds(5));
      } catch (const std::exception& e) {
        LOG(ERROR) << "DataBentoBoats fatal error: " << e.what();
        try {
          pktrade::util::Mailer::instance().send_mail(
              "PKFEED DATABENTO_BOATS DOWN", std::string("Fatal exception: ") + e.what());
        } catch (const std::exception& mail_ex) {
          LOG(ERROR) << "Failed to send BOATS fatal alert: " << mail_ex.what();
        }
        (*client_it).reset();
        std::this_thread::sleep_for(std::chrono::seconds(5));
        continue;
      } catch (...) {
        LOG(ERROR) << "DataBentoBoats fatal error: unknown exception";
        try {
          pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_BOATS DOWN",
                                                      "Unknown fatal exception");
        } catch (const std::exception& mail_ex) {
          LOG(ERROR) << "Failed to send BOATS fatal alert: " << mail_ex.what();
        }
        (*client_it).reset();
        std::this_thread::sleep_for(std::chrono::seconds(5));
        continue;
      }
    }

    (*client_it).reset();
  };

  {
    std::lock_guard<std::mutex> lock(g_db_threads_mtx);
    g_db_threads.emplace_back(std::move(runner));
  }
}

// Processes a CME quote from any source (direct databento callback or a
// relay receiver). Handles symbol resolution, per-symbol throttling, session
// filter, price sanity, blend-leg merge, protobuf build, publish, and
// per-(symbol, source) fusion-stats accounting.
//
// For blend legs, ts_event_ms is updated in place to the merged (max of
// both legs) timestamp, so the caller's delay/stale accounting sees the
// same value the published message carries.
//
// `source` identifies which ingestion path is calling (TCP direct or UDP
// relay); used to attribute events_seen / cas_wins / delay in
// CmeFusionState's per-symbol stats so the log-dumped summary can show
// which path is winning the CAS race.
//
// Returns true iff a message was published. Callers may use the return
// value to drive per-source delay logging and stale-feed handling.
bool processCmeQuote(DatabentoFeedState* state, const std::string& db_sym,
                     double bb_px, double ba_px, int bid_sz, int ask_sz,
                     int64_t& ts_event_ms, int64_t now_ms, zmq::socket_t* pub_ptr,
                     CmeSource source) {
  std::string our_sym;
  try {
    our_sym = symbolizer::databento_to_ours(db_sym);
  } catch (const std::exception& e) {
    LOG(WARNING) << "CME: can't resolve " << db_sym << ": " << e.what();
    return false;
  }

  bool is_blend_leg = state->contract_to_leg.count(db_sym) > 0;
  std::string leg;
  if (is_blend_leg) {
    leg = state->contract_to_leg[db_sym];
  }

  if (!is_blend_leg) {
    auto last_pub_it = state->last_publish_time.find(our_sym);
    if (last_pub_it != state->last_publish_time.end() &&
        now_ms - last_pub_it->second < state->publish_interval_ms) {
      return false;
    }
  }

  if (!isInCmeSession(ts_event_ms)) {
    return false;
  }

  if (bb_px > 1e6 || ba_px > 1e6) {
    LOG(WARNING) << "CME: unusable price for " << db_sym;
    return false;
  }

  if (is_blend_leg) {
    state->roll_latest[our_sym][leg] =
        std::make_tuple(bb_px, ba_px, bid_sz, ask_sz, ts_event_ms);

    auto last_pub_it = state->last_publish_time.find(our_sym);
    if (last_pub_it != state->last_publish_time.end() &&
        now_ms - last_pub_it->second < state->publish_interval_ms) {
      return false;
    }

    auto& legs = state->roll_latest[our_sym];
    auto front_it = legs.find("front");
    auto next_it = legs.find("next");
    if (front_it == legs.end() || next_it == legs.end()) {
      return false;
    }

    const auto& [f_bb, f_ba, f_bsz, f_asz, f_ts] = front_it->second;
    const auto& [n_bb, n_ba, n_bsz, n_asz, n_ts] = next_it->second;
    auto [front_w, next_w] = state->blend_weights[our_sym];

    bb_px = front_w * f_bb + next_w * n_bb;
    ba_px = front_w * f_ba + next_w * n_ba;
    bid_sz = f_bsz + n_bsz;
    ask_sz = f_asz + n_asz;
    ts_event_ms = std::max(f_ts, n_ts);

    if (state->msg_count % 1000 == 0 || state->msg_count < 100) {
      double front_mid = (f_bb + f_ba) / 2.0;
      double next_mid = (n_bb + n_ba) / 2.0;
      double blend_mid = front_w * front_mid + next_w * next_mid;
      LOG(INFO) << fmt::format("Blending {} mids: {} x {:.6f} + {} x {:.6f} = {:.6f}",
                               our_sym, front_w, front_mid, next_w, next_mid, blend_mid);
    }
  }

  // Stats: count every event that reached the CAS gate, regardless of
  // outcome, so "events_seen" reflects how many candidate publishes this
  // source got to attempt. Wins / seen then gives the per-source lead rate.
  cmeFusionState().record_seen(our_sym, source);

  // Cross-source dedup: only the first source to reach this point for a
  // given (our_sym, ts_event_ms) wins. The other source's matching or
  // stale event gets dropped. When only one source is active this is a
  // cheap CAS that always succeeds.
  //
  // For roll blends this means equal merged ts_event_ms is considered a
  // duplicate even if the blend payload changed because one leg updated
  // while the max(front_ts, next_ts) stayed constant. That ambiguity is
  // accepted by design: fusion operates on event-time monotonicity, not a
  // full quote-content diff.
  if (!cmeFusionState().try_claim(our_sym, ts_event_ms)) {
    return false;
  }

  // This source won the CAS. Accumulate delay into its stats before
  // publishing so the reporter thread sees a consistent view.
  cmeFusionState().record_win(our_sym, source, now_ms - ts_event_ms);

  mdmsg::PbMessage pbmsg;
  pbmsg.set_market(mdmsg::PBMARKET_TOPBOOK_CME);
  pbmsg.set_symbol_id(our_sym);

  auto* pbquote = pbmsg.mutable_quote();
  pbquote->set_best_bid_px(std::to_string(bb_px));
  pbquote->set_best_bid_qty(std::to_string(bid_sz));
  pbquote->set_best_ask_px(std::to_string(ba_px));
  pbquote->set_best_ask_qty(std::to_string(ask_sz));
  pbquote->set_mid_px((bb_px + ba_px) / 2.0);
  pbquote->set_transact_time(ts_event_ms);

  publish(pbmsg, pub_ptr);

  state->last_publish_time[our_sym] = now_ms;
  return true;
}

// Expand one CME config symbol into its databento contract(s) for `date_int`,
// register it with the fusion state, and populate any roll-blend legs in
// `state`. Returns the contract db_sym(s) to subscribe/accept (one, or two for a
// roll blend), or an empty vector if the symbol is unknown/typo'd. In that case
// it logs an ERROR and returns empty rather than letting the exception
// propagate: symbols can be added intraday, and a throw here would either spin
// the databento client into a reset loop or std::terminate the relay receiver
// thread. `label` tags the log lines ("DataBentoCME" / "RelayDBCME"). Shared by
// both CME ingestion paths so base and extra symbols expand identically.
std::vector<std::string> expandCmeSymbol(const std::string& our_sym, int date_int,
                                         DatabentoFeedState& state, const char* label) {
  std::vector<std::pair<std::string, double>> contracts;
  try {
    contracts = symbolizer::get_all_contracts_for_date(our_sym, date_int);
  } catch (const std::exception& e) {
    LOG(ERROR) << label << ": skipping symbol " << our_sym
               << " (contract resolution failed: " << e.what() << ")";
    return {};
  }
  cmeFusionState().register_symbol(our_sym);
  std::vector<std::string> db_syms;
  for (const auto& [db_sym, weight] : contracts) {
    db_syms.push_back(db_sym);
    LOG(INFO) << label << ": adding contract " << db_sym << " for " << our_sym << " weight "
              << weight;
  }
  if (contracts.size() == 2) {
    const auto& [front_sym, front_w] = contracts[0];
    const auto& [next_sym, next_w] = contracts[1];
    state.blend_weights[our_sym] = {front_w, next_w};
    state.contract_to_leg[front_sym] = "front";
    state.contract_to_leg[next_sym] = "next";
    state.roll_latest[our_sym] = {};
    LOG(INFO) << fmt::format("{} roll blend active for {}: {}({}) + {}({})", label, our_sym,
                             front_sym, front_w, next_sym, next_w);
  }
  return db_syms;
}

void subscribeDatabentoCME(rapidjson::Value& sub_config, const std::string& api_key, int date_int,
                           zmq::socket_t& publisher, bool accept_bad_book) {
  if (!sub_config.HasMember("symbols") || sub_config["symbols"].GetArray().Empty()) {
    LOG(WARNING) << "DataBentoCME: no symbols configured";
    return;
  }
  std::vector<std::string> base_our_syms;
  for (auto& sym : sub_config["symbols"].GetArray()) {
    base_our_syms.push_back(sym.GetString());
  }
  LOG(INFO) << "DataBentoCME: configured " << base_our_syms.size() << " base symbols";

  auto* pub_ptr = &publisher;
  auto client_it = g_db_clients.emplace(g_db_clients.end());

  auto runner = [base_our_syms, pub_ptr, client_it, api_key, accept_bad_book, date_int]() mutable {
    while (!g_shutdown.load()) {
      try {
        auto state = std::make_shared<DatabentoFeedState>();

        // Expand base + intraday-added symbols into databento contracts and
        // roll-blend state via the shared helper. Done here (not once at
        // startup) so base and extra share one path and reload-added symbols
        // are picked up on reconnect. Unknown/typo'd symbols are skipped
        // (logged) rather than thrown, so a bad add can't spin the reconnect.
        std::vector<std::string> extra = extraSymsFor("DataBentoCME");
        std::vector<std::string> our_syms = base_our_syms;
        our_syms.insert(our_syms.end(), extra.begin(), extra.end());
        std::vector<std::string> subs;
        for (const auto& our_sym : our_syms) {
          for (const auto& db_sym : expandCmeSymbol(our_sym, date_int, *state, "DataBentoCME")) {
            subs.push_back(db_sym);
          }
        }
        if (subs.empty()) {
          // Every configured CME symbol failed to resolve. The base list is
          // fixed for the process, so retrying it is futile — wait until a config
          // reload adds a symbol (the extra count changes) or shutdown, then
          // re-expand. Logged at ERROR so monitoring notices the misconfig.
          LOG(ERROR) << "DataBentoCME: no valid contracts resolved from config; "
                        "waiting for a config reload";
          const uint64_t seen = g_cme_extra.count.load();
          while (!g_shutdown.load() && g_cme_extra.count.load() == seen) {
            std::this_thread::sleep_for(std::chrono::seconds(2));
          }
          continue;
        }
        LOG(INFO) << "DataBentoCME: subscribing to " << subs.size() << " contracts";

        auto& client_ptr = *client_it;
        try {
          client_ptr = std::make_unique<databento::LiveThreaded>(
              databento::LiveThreaded::Builder()
                  .SetKey(api_key)
                  .SetDataset("GLBX.MDP3")
                  .SetSendTsOut(true)
                  // 16MB (<<20 is 1mb)
                  .SetBufferSize(1 << 24)
                  .SetSlowReaderBehavior(databento::SlowReaderBehavior::Skip)
                  .BuildThreaded());
          client_ptr->Subscribe(subs, databento::Schema::Mbp1, databento::SType::RawSymbol);
          // Extras in this Subscribe are now applied; the callback drain only
          // handles ones added after this snapshot.
          g_cme_extra.applied.store(extra.size(), std::memory_order_release);
        } catch (const std::exception& e) {
          LOG(ERROR) << "DataBentoCME: failed to initialize client: " << e.what();
          client_ptr.reset();
          std::this_thread::sleep_for(std::chrono::seconds(5));
          continue;
        }

        auto restart_requested = std::make_shared<std::atomic<bool>>(false);
        LOG(INFO) << "DataBentoCME: client started";
        client_ptr->Start([state, pub_ptr, restart_requested, accept_bad_book, date_int,
                           client_raw = client_ptr.get()](
                              const databento::Record& record) -> databento::KeepGoing {
          try {
          state->msg_count++;

          if (pktrade::g_shutdown.load()) {
            return databento::KeepGoing::Stop;
          }

          // Live-subscribe any intraday-added symbols (no reconnect). Safe here
          // because this callback runs on the client's own internal thread; the
          // build fn expands contracts and updates roll state on this same thread.
          drainDbExtras(g_cme_extra, client_raw, "DataBentoCME", databento::Schema::Mbp1,
                        [&](const std::string& our_sym) {
                          return expandCmeSymbol(our_sym, date_int, *state, "DataBentoCME");
                        });

          if (auto* sym_map = record.GetIf<databento::SymbolMappingMsg>()) {
            std::string stype_in(sym_map->stype_in_symbol.data());
            uint32_t iid = sym_map->hd.instrument_id;
            state->idx_to_sym[iid] = stype_in;
            state->sym_to_idx[stype_in] = iid;
            state->got_sym_map = true;
            LOG(INFO) << "CME SymbolMapping: " << stype_in << " -> " << iid;
            return databento::KeepGoing::Continue;
          }

          if (auto* sys_msg = record.GetIf<databento::SystemMsg>()) {
            std::string msg(sys_msg->msg.data());
            if (msg.find("Subscription") != std::string::npos &&
                msg.find("succeeded") != std::string::npos) {
              state->subscribed = true;
            }
            LOG(INFO) << "CME System: " << msg;
            return databento::KeepGoing::Continue;
          }

          if (record.Holds<databento::StatusMsg>()) {
            LOG(INFO) << "CME Status message received";
            return databento::KeepGoing::Continue;
          }

          if (auto* err_msg = record.GetIf<databento::ErrorMsg>()) {
            LOG(ERROR) << "CME Error: " << std::string(err_msg->err.data());
            return databento::KeepGoing::Continue;
          }

          auto mbp1_view = extractRecordView<databento::Mbp1Msg>(record);
          if (mbp1_view.msg != nullptr) {
            const auto* mbp1 = mbp1_view.msg;
            state->real_count++;

            auto sym_it = state->idx_to_sym.find(mbp1->hd.instrument_id);
            if (sym_it == state->idx_to_sym.end()) {
              LOG(WARNING) << "CME MBP1 without mapping: instrument_id=" << mbp1->hd.instrument_id;
              return databento::KeepGoing::Continue;
            }
            const std::string& db_sym = sym_it->second;

            auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                              std::chrono::system_clock::now().time_since_epoch())
                              .count();
            state->last_msg_time_ms.store(now_ms);

            if (state->bad_count >= kMaxDatabentoBadMsgs) {
              const int64_t bad_count = state->bad_count;
              state->bad_count = 0;
              auto err_msg = fmt::format(
                  "CME: {} bad DataBento messages received (threshold {}), restarting feed",
                  bad_count, kMaxDatabentoBadMsgs);
              LOG(ERROR) << err_msg;
              try {
                pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_CME DOWN", err_msg);
              } catch (const std::exception& e) {
                LOG(ERROR) << "Failed to send CME bad-message alert: " << e.what();
              }
              restart_requested->store(true);
              return databento::KeepGoing::Stop;
            }

            uint64_t ts_event_raw = mbp1->hd.ts_event.time_since_epoch().count();
            uint64_t ts_recv_raw = mbp1->ts_recv.time_since_epoch().count();
            bool has_undef_price = mbp1->price == databento::kUndefPrice;
            bool has_undef_event_ts = ts_event_raw == databento::kUndefTimestamp;
            bool has_undef_recv_ts = ts_recv_raw == databento::kUndefTimestamp;
            bool is_snapshot = mbp1->flags.IsSnapshot();
            bool maybe_bad_book = mbp1->flags.IsMaybeBadBook();
            // When --acceptbadbook is set, we publish bad book messages anyway.
            // Databento says F_MAYBE_BAD_BOOK means an unrecoverable gap — the
            // book might be stale but the data is real market data, not corrupt.
            // Without the flag, bad book msgs are skipped and count toward
            // bad_count, which can trigger a restart loop on flaky connections.
            bool invalid_msg = has_undef_price || has_undef_event_ts || has_undef_recv_ts ||
                               is_snapshot || (!accept_bad_book && maybe_bad_book);

            if (maybe_bad_book) {
              // Log every 20 so if we're restarting for every 100 of these, we'll see a few first.
              LOG_EVERY_N(WARNING, 20) << "CME: maybe_bad_book flag set for " << db_sym
                                       << (accept_bad_book ? " (accepted)" : " (skipped)");
            }

            if (mbp1->flags.IsPublisherSpecific()) {
              LOG_EVERY_N(WARNING, 101) << "CME: publisher_specific flag set for " << db_sym;
            }

            if (invalid_msg) {
              state->bad_count++;
              return databento::KeepGoing::Continue;
            }

            constexpr double PX_CONVERT = 1e-9;
            double px = std::abs(mbp1->price * PX_CONVERT);
            double bb_px = mbp1->levels[0].bid_px * PX_CONVERT;
            double ba_px = mbp1->levels[0].ask_px * PX_CONVERT;
            int bid_sz = mbp1->levels[0].bid_sz;
            int ask_sz = mbp1->levels[0].ask_sz;
            int64_t ts_event_ms = static_cast<int64_t>(ts_event_raw / 1000 / 1000);
            int64_t recv_delay_ms = now_ms - ts_event_ms;

            // Delay observability + stale-detect hoisted above processCmeQuote
            // so fusion CAS losses and publish-rate-limited drops still show
            // up in the logs and still trigger reconnects. When pkrelay is
            // healthy the TCP path loses the CAS on essentially every quote,
            // so any check placed after processCmeQuote never fires -- we'd
            // go blind to TCP-path slowdowns until the 60s silence watchdog.
            // Uses the raw per-leg ts_event rather than the blend-merged
            // max(front,next) that processCmeQuote writes for roll symbols:
            // a stale front leg shouldn't be hidden by a fresh next leg.
            if (state->msg_count % 1000 == 0 || recv_delay_ms > 1000) {
              int64_t ts_recv_ms = static_cast<int64_t>(ts_recv_raw / 1000 / 1000);
              int64_t db_delay_ms = ts_recv_ms - ts_event_ms;
              int64_t net_delay_ms = now_ms - ts_recv_ms;
              int64_t ts_out_delay_ms = mbp1_view.has_ts_out ? now_ms - mbp1_view.ts_out_ms : -1;
              std::string ts_out_delay_str =
                  mbp1_view.has_ts_out ? std::to_string(ts_out_delay_ms) : "NA";
              LOG(INFO) << fmt::format(
                  "CME {} msgtime {} tot_delay {} (db {} net {}) ts_out_delay {}", db_sym,
                  ts_event_ms, recv_delay_ms, db_delay_ms, net_delay_ms, ts_out_delay_str);
            }

            if (recv_delay_ms > kCmeStaleThresholdMs) {
              std::string err_msg = fmt::format("CME {} delay {}ms exceeds {}ms threshold", db_sym,
                                                recv_delay_ms, kCmeStaleThresholdMs);
              LOG(ERROR) << err_msg;
              // Skip the email for CME since we have RelayDBCME and the EOD summary already.
              // if (now_ms - state->last_stale_alert_ms > kStaleAlertIntervalMs) {
              //   state->last_stale_alert_ms = now_ms;
              //   try {
              //     pktrade::util::Mailer::instance().send_mail("CME FEED STALE", err_msg);
              //   } catch (const std::exception& e) {
              //     LOG(ERROR) << "Failed to send CME stale alert: " << e.what();
              //   }
              // }
              restart_requested->store(true);
              return databento::KeepGoing::Stop;
            }

            if (px > 1e6) {
              LOG(WARNING) << "CME: unusable price for " << db_sym;
              return databento::KeepGoing::Continue;
            }

            if (!processCmeQuote(state.get(), db_sym, bb_px, ba_px, bid_sz, ask_sz, ts_event_ms,
                                 now_ms, pub_ptr, CmeSource::Tcp)) {
              return databento::KeepGoing::Continue;
            }
          }

          return databento::KeepGoing::Continue;
          } catch (const std::exception& e) {
            LOG(ERROR) << "CME callback exception: " << e.what();
            restart_requested->store(true);
            return databento::KeepGoing::Stop;
          } catch (...) {
            LOG(ERROR) << "CME callback unknown exception";
            restart_requested->store(true);
            return databento::KeepGoing::Stop;
          }
        });

        const auto cme_watchdog_timeout = std::chrono::seconds(5);
        const auto cme_watchdog = [&]() {
          int64_t last = state->last_msg_time_ms.load();
          if (last == 0) {
            return;
          }
          auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::system_clock::now().time_since_epoch())
                            .count();
          if (!isInCmeSession(now_ms)) {
            return;
          }
          if (now_ms - last > kCmeSilentThresholdMs) {
            LOG(ERROR) << fmt::format("CME watchdog: no data for {}ms (threshold {}ms), restarting",
                                      now_ms - last, kCmeSilentThresholdMs);
            try {
              pktrade::util::Mailer::instance().send_mail(
                  "CME FEED SILENT", fmt::format("No CME data for {}ms", now_ms - last));
            } catch (const std::exception& e) {
              LOG(ERROR) << "Failed to send CME silent alert: " << e.what();
            }
            restart_requested->store(true);
            client_ptr.reset();
          }
        };
        int64_t extras_pending_since = 0;
        while (!g_shutdown.load() && client_ptr) {
          auto rc = client_ptr->BlockForStop(cme_watchdog_timeout);
          if (rc == databento::KeepGoing::Stop) {
            break;
          }
          // Extras are normally live-subscribed by the record callback; fall back
          // to a reconnect if that hasn't happened within the timeout.
          if (dbExtrasReconnectDue(g_cme_extra, extras_pending_since)) {
            LOG(INFO) << "DataBentoCME: extras not live-subscribed within 10s, reconnecting";
            client_ptr.reset();
            break;
          }
          cme_watchdog();
        }
        if (client_ptr) {
          client_ptr.reset();
        }

        if (g_shutdown.load()) {
          LOG(INFO) << "DataBentoCME client stopped (shutdown)";
          break;
        }
        if (!restart_requested->load()) {
          LOG(WARNING) << "DataBentoCME connection lost unexpectedly, restarting";
        } else {
          LOG(WARNING) << "DataBentoCME restarting after stale/silent feed";
        }
        std::this_thread::sleep_for(std::chrono::seconds(5));
      } catch (const std::exception& e) {
        LOG(ERROR) << "DataBentoCME fatal error: " << e.what();
        try {
          pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_CME DOWN",
                                   std::string("Fatal exception: ") + e.what());
        } catch (const std::exception& mail_ex) {
          LOG(ERROR) << "Failed to send CME fatal alert: " << mail_ex.what();
        }
        (*client_it).reset();
        std::this_thread::sleep_for(std::chrono::seconds(5));
        continue;
      } catch (...) {
        LOG(ERROR) << "DataBentoCME fatal error: unknown exception";
        try {
          pktrade::util::Mailer::instance().send_mail("PKFEED DATABENTO_CME DOWN",
                                                      "Unknown fatal exception");
        } catch (const std::exception& mail_ex) {
          LOG(ERROR) << "Failed to send CME fatal alert: " << mail_ex.what();
        }
        (*client_it).reset();
        std::this_thread::sleep_for(std::chrono::seconds(5));
        continue;
      }
    }

    (*client_it).reset();
  };

  {
    std::lock_guard<std::mutex> lock(g_db_threads_mtx);
    g_db_threads.emplace_back(std::move(runner));
  }
}

// Parallel subscription that reads CME quotes forwarded by pkrelay over
// UDP. Runs alongside subscribeDatabentoCME on the same pkmultifeed
// process so that either path can cover outages of the other; the
// cmeFusionState CAS gate ensures each CME event is published at most
// once regardless of which path wins the race.
//
// Config (top-level "RelayDBCME" block):
//   {
//     "symbols": ["CL", "HG", "NG"],
//     "listen_port": 4242
//   }
//
// Invalid-message filtering (snapshot, undef timestamps, maybe-bad-book
// subject to --acceptbadbook) is done at the relay. This consumer
// trusts what arrives and just dedupes + publishes.
void subscribeRelayDbCme(rapidjson::Value& sub_config, int date_int,
                         zmq::socket_t& publisher, bool accept_bad_book) {
  if (!sub_config.HasMember("symbols") || sub_config["symbols"].GetArray().Empty()) {
    LOG(WARNING) << "RelayDBCME: no symbols configured";
    return;
  }
  if (!sub_config.HasMember("listen_port") ||
      !sub_config["listen_port"].IsInt()) {
    LOG(WARNING) << "RelayDBCME: missing/invalid listen_port";
    return;
  }
  const uint16_t listen_port =
      static_cast<uint16_t>(sub_config["listen_port"].GetInt());

  // Roll-blend + filter state owned by the receiver thread. Base symbols are
  // expanded now (via the shared helper, so base/extra behave identically);
  // extras added intraday are expanded later on the receiver thread. The relay
  // sprays quotes for the union of symbols any consumer wants, so expected_db_syms
  // filters at ingress to keep unwanted symbols out of processCmeQuote.
  auto state = std::make_shared<DatabentoFeedState>();
  std::unordered_set<std::string> expected_db_syms;
  for (auto& sym : sub_config["symbols"].GetArray()) {
    for (const auto& db_sym : expandCmeSymbol(sym.GetString(), date_int, *state, "RelayDBCME")) {
      expected_db_syms.insert(db_sym);
    }
  }

  int sock = ::socket(AF_INET, SOCK_DGRAM, 0);
  if (sock < 0) {
    LOG(ERROR) << "RelayDBCME: socket() failed: " << std::strerror(errno);
    return;
  }

  // Large UDP receive buffer so bursts don't drop at the kernel.
  // SO_RCVBUF is capped by net.core.rmem_max, which we raised in the
  // TCP-tuning sysctl work.
  int rbuf = 8 * 1024 * 1024;
  if (::setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rbuf, sizeof(rbuf)) < 0) {
    LOG(WARNING) << "RelayDBCME: SO_RCVBUF=" << rbuf
                 << " failed: " << std::strerror(errno);
  }

  // Bounded recv timeout so the receiver thread wakes periodically and
  // can observe g_shutdown even when the relay is dead / silent. Without
  // this, a SIGTERM during a relay outage would hang pkmultifeed shutdown
  // indefinitely inside ::recv().
  struct timeval rcv_timeout{};
  rcv_timeout.tv_sec = 2;
  rcv_timeout.tv_usec = 0;
  if (::setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &rcv_timeout,
                   sizeof(rcv_timeout)) < 0) {
    LOG(WARNING) << "RelayDBCME: SO_RCVTIMEO failed: " << std::strerror(errno);
  }

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(listen_port);
  if (::bind(sock, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) < 0) {
    LOG(ERROR) << "RelayDBCME: bind(:" << listen_port
               << ") failed: " << std::strerror(errno);
    ::close(sock);
    return;
  }
  LOG(INFO) << "RelayDBCME: listening on UDP :" << listen_port;

  auto* pub_ptr = &publisher;

  // Shared across receiver + watchdog threads.
  auto last_seq = std::make_shared<std::atomic<uint64_t>>(0);
  auto seq_initialized = std::make_shared<std::atomic<bool>>(false);
  auto last_packet_ms = std::make_shared<std::atomic<int64_t>>(0);
  auto drop_count = std::make_shared<std::atomic<uint64_t>>(0);

  auto receiver = [state, sock, pub_ptr, accept_bad_book, last_seq,
                   seq_initialized, last_packet_ms, drop_count, date_int,
                   expected_db_syms = std::move(expected_db_syms)]() mutable {
    pktrade::feedrelay::DatabentoCmeMbpPacket pkt;
    // Number of intraday-added symbols already merged into expected_db_syms /
    // state. Applied on this (the only) thread that owns those, so no locking of
    // them is needed. The recv timeout bounds how long a new symbol waits.
    size_t applied_extra_count = 0;
    while (!g_shutdown.load()) {
      // Cheap atomic gate — only lock + copy the extra list when the reload has
      // appended something new. Expanding each extra config symbol mirrors the
      // base setup (fusion registration, contract resolution, roll-blend legs).
      // NB: data only flows once pkrelay is also taught to spray these contracts;
      // until then this just readies pkmultifeed's ingress filter + blend state.
      if (g_relay_cme_extra_count.load(std::memory_order_acquire) > applied_extra_count) {
        std::vector<std::string> extras = extraSymsFor("RelayDBCME");
        for (size_t i = applied_extra_count; i < extras.size(); ++i) {
          for (const auto& db_sym : expandCmeSymbol(extras[i], date_int, *state, "RelayDBCME")) {
            expected_db_syms.insert(db_sym);
          }
        }
        applied_extra_count = extras.size();
      }
      ssize_t n = ::recv(sock, &pkt, sizeof(pkt), 0);
      if (n < 0) {
        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
        LOG_EVERY_N(ERROR, 1000)
            << "RelayDBCME: recv failed: " << std::strerror(errno);
        continue;
      }
      if (n != static_cast<ssize_t>(sizeof(pkt))) {
        LOG_EVERY_N(WARNING, 1000)
            << "RelayDBCME: unexpected packet size " << n
            << " (expected " << sizeof(pkt) << ")";
        continue;
      }
      if (pkt.magic != pktrade::feedrelay::kPkRelayMagic ||
          pkt.version != pktrade::feedrelay::kPkRelayVersion) {
        LOG_EVERY_N(WARNING, 1000)
            << "RelayDBCME: bad magic/version (got 0x" << std::hex << pkt.magic
            << std::dec << "/" << pkt.version << ")";
        continue;
      }

      const int64_t now_ms =
          std::chrono::duration_cast<std::chrono::milliseconds>(
              std::chrono::system_clock::now().time_since_epoch())
              .count();
      last_packet_ms->store(now_ms);
      state->last_msg_time_ms.store(now_ms);

      // Sequence gap tracking for operator diagnostics. This is a
      // best-effort heuristic, not a strict loss detector: pkrelay emits
      // both quotes and heartbeats from the same seq space, UDP may reorder,
      // and this consumer intentionally prefers "publish the newest intact
      // packet we have" over reconstructing a perfect in-order stream.
      //
      // Consequences:
      //   - seq gaps are suggestive of loss / backlog but not proof
      //   - backwards jumps may be relay restart or plain reordering
      //   - we still process any full, valid, newer packet that arrives
      //
      // That tradeoff matches this feed's goal: best-effort freshness,
      // not reliable delivery.
      uint64_t prev_seq = last_seq->load();
      if (!seq_initialized->load()) {
        last_seq->store(pkt.seq);
        seq_initialized->store(true);
      } else if (pkt.seq > prev_seq + 1) {
        uint64_t gap = pkt.seq - prev_seq - 1;
        drop_count->fetch_add(gap);
        LOG_EVERY_N(WARNING, 1000)
            << "RelayDBCME: seq gap " << gap << " (got " << pkt.seq
            << ", expected " << (prev_seq + 1) << ")";
        last_seq->store(pkt.seq);
      } else if (pkt.seq <= prev_seq) {
        // Out-of-order or relay restart. Resync without counting drops.
        last_seq->store(pkt.seq);
      } else {
        last_seq->store(pkt.seq);
      }

      if (pkt.type ==
          static_cast<uint16_t>(pktrade::feedrelay::PacketType::Heartbeat)) {
        continue;
      }
      if (pkt.type !=
          static_cast<uint16_t>(pktrade::feedrelay::PacketType::Quote)) {
        LOG_EVERY_N(WARNING, 1000)
            << "RelayDBCME: unknown packet type " << pkt.type;
        continue;
      }

      // Optional maybe-bad-book filter at the Tokyo side. Mirrors the
      // flag bit from mbp1->flags; the relay already filters unless
      // started with --acceptbadbook, so this mostly matters when the
      // relay and this consumer have different accept_bad_book settings.
      databento::FlagSet flags;
      flags.SetRaw(static_cast<uint8_t>(pkt.flags & 0xFF));
      if (flags.IsMaybeBadBook() && !accept_bad_book) {
        continue;
      }

      std::string db_sym(pkt.db_sym,
                         ::strnlen(pkt.db_sym,
                                   pktrade::feedrelay::kPkRelaySymBytes));

      if (expected_db_syms.find(db_sym) == expected_db_syms.end()) {
        continue;
      }

      constexpr double PX_CONVERT = 1e-9;
      double bb_px = pkt.bid_px_1e9 * PX_CONVERT;
      double ba_px = pkt.ask_px_1e9 * PX_CONVERT;
      int64_t ts_event_ms = pkt.ts_event_ms;

      state->msg_count++;
      if (!processCmeQuote(state.get(), db_sym, bb_px, ba_px, pkt.bid_sz,
                           pkt.ask_sz, ts_event_ms, now_ms, pub_ptr,
                           CmeSource::UdpRelay)) {
        continue;
      }
      state->real_count++;

      int64_t recv_delay_ms = now_ms - ts_event_ms;
      int64_t relay_transit_ms = now_ms - pkt.ts_send_ms;
      if (state->msg_count % 200 == 0 || recv_delay_ms > 1000) {
        LOG(INFO) << fmt::format(
            "RelayDBCME {} msgtime {} delay {}ms relay_transit {}ms seq {} drops {}",
            db_sym, ts_event_ms, recv_delay_ms, relay_transit_ms, pkt.seq,
            drop_count->load());
      }

      // Stale-feed email alert. No restart (the socket is passively
      // listening and the fix is on pkrelay's side), but page so someone
      // investigates. Rate-limited like the TCP path's stale alert.
      if (recv_delay_ms > kCmeStaleThresholdMs) {
        std::string err_msg = fmt::format(
            "RelayDBCME {} delay {}ms exceeds {}ms threshold (not restarting)",
            db_sym, recv_delay_ms, kCmeStaleThresholdMs);
        LOG(ERROR) << err_msg;
        if (now_ms - state->last_stale_alert_ms > kStaleAlertIntervalMs) {
          state->last_stale_alert_ms = now_ms;
          try {
            pktrade::util::Mailer::instance().send_mail("RELAY CME FEED STALE", err_msg);
          } catch (const std::exception& e) {
            LOG(ERROR) << "Failed to send RelayDBCME stale alert: " << e.what();
          }
        }
      }
    }
    ::close(sock);
  };

  // Watchdog: no TCP connection to restart, so "dead" here means the
  // relay stopped sending (relay process down, VPC peering issue, etc.)
  // and all we can do is page. Rate-limited like the TCP stale alert.
  auto watchdog = [last_packet_ms, drop_count]() {
    int64_t last_silent_alert_ms = 0;
    while (!g_shutdown.load()) {
      std::this_thread::sleep_for(std::chrono::seconds(5));
      int64_t last = last_packet_ms->load();
      if (last == 0) continue;
      int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                           std::chrono::system_clock::now().time_since_epoch())
                           .count();
      if (!isInCmeSession(now_ms)) continue;
      if (now_ms - last > kCmeSilentThresholdMs &&
          now_ms - last_silent_alert_ms > kStaleAlertIntervalMs) {
        last_silent_alert_ms = now_ms;
        auto err_msg = fmt::format(
            "RelayDBCME: no packets for {}ms (drop total {})", now_ms - last,
            drop_count->load());
        LOG(ERROR) << err_msg;
        try {
          pktrade::util::Mailer::instance().send_mail("RELAY CME SILENT", err_msg);
        } catch (const std::exception& e) {
          LOG(ERROR) << "RelayDBCME: send_mail failed: " << e.what();
        }
      }
    }
  };

  {
    std::lock_guard<std::mutex> lock(g_db_threads_mtx);
    g_db_threads.emplace_back(std::move(receiver));
    g_db_threads.emplace_back(std::move(watchdog));
  }
}

////////////////////////////////////////////////////////////////////////////////////

// This is kind of a mess, but I think in the end we should just
// separate all the different markets into their own subscriptions.
// At the same time, I still think it's easier to reorg the code like this,
// as it's way too annoying to regenerate the feed configs.
void subWebsockets(
    rapidjson::Document& config_doc, std::vector<ix::WebSocket*>& websockets,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_trd,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_ticker,

    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update_50,
    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update_200,

    std::map<pktrade::Market, std::map<std::string, int64_t>>& mkt_sym_latest_update_500,

    std::mutex& t_mutex, zmq::socket_t& publisher, zmq::context_t& ctx,
    std::map<pktrade::Market, int64_t>& last_heartbeat_t,
    std::map<pktrade::Market, std::vector<std::string>>& sub_stream_names,
    std::map<pktrade::Market, std::string>& mkt_to_apikeys, int trading_date_int,
    bool accept_bad_book
) {
  // Start populating these websockets iteratively.

  rapidjson::Value& sub_config = config_doc["subscriptions"];

  // Pre-pass: record whether the Hyperliquid WS feed is also configured.
  // HyperliquidNode's health logic uses this to decide whether to flip
  // out of node-primary mode at all.
  if (sub_config.HasMember("Hyperliquid")) {
    g_hl_node.ws_fallback_present = true;
  }

  // This is going to subscribe to the
  for (auto& mkt_config_pair : sub_config.GetObject()) {
    rapidjson::Value& mkt_config = mkt_config_pair.value;
    // Used for per-market subscriptions. If not needed,
    // don't worry about it.

    // binance-specific subscriptions
    // Similar subscriptions if we do binance-based things.
    if ((strcmp(mkt_config_pair.name.GetString(), "BinanceSPOT") == 0) ||
        (strcmp(mkt_config_pair.name.GetString(), "BinanceFutures") == 0) ||
        (strcmp(mkt_config_pair.name.GetString(), "BinanceCOINFutures") == 0)) {
      subscribeBinanceWebsockets(mkt_config_pair.name.GetString(), mkt_config, websockets,
                                 mkt_sym_latest_update, mkt_sym_latest_trd, mkt_sym_latest_ticker,
                                 t_mutex, publisher);
      // Do some stuff.
    } else if (strcmp(mkt_config_pair.name.GetString(), "Hyperliquid") == 0) {
      subscribeHyperliquidWebsockets(mkt_config_pair.name.GetString(), mkt_config, websockets,
                                     mkt_sym_latest_update, mkt_sym_latest_trd,
                                     mkt_sym_latest_ticker, t_mutex, publisher);
    } else if (strcmp(mkt_config_pair.name.GetString(), "HyperliquidNode") == 0) {
      subscribeHyperliquidNode(mkt_config, ctx, publisher);
    } else if ((strcmp(mkt_config_pair.name.GetString(), "BybitSPOT") == 0) ||
               (strcmp(mkt_config_pair.name.GetString(), "BybitDeriv") == 0) ||
               (strcmp(mkt_config_pair.name.GetString(), "BybitInverseDeriv") == 0)) {
      subscribeBybitWebsockets(
          mkt_config_pair.name.GetString(), mkt_config, websockets, mkt_sym_latest_ticker,
          mkt_sym_latest_update_50, mkt_sym_latest_update_200, mkt_sym_latest_update_500,
          mkt_sym_latest_trd, t_mutex, publisher, last_heartbeat_t, sub_stream_names);
    } else if (strcmp(mkt_config_pair.name.GetString(), "DataBentoEquities") == 0) {
      auto key_it = mkt_to_apikeys.find(pktrade::Market::TopBookEquity);
      if (key_it != mkt_to_apikeys.end()) {
        std::cout << "Subscribing to databentoeq" << std::endl;
        subscribeDatabentoEQ(mkt_config, key_it->second, publisher);
      } else {
        LOG(ERROR) << "DataBentoEQ: no API key found";
      }
    } else if (strcmp(mkt_config_pair.name.GetString(), "DataBentoCME") == 0) {
      auto key_it = mkt_to_apikeys.find(pktrade::Market::TopBookCme);
      if (key_it != mkt_to_apikeys.end()) {
        subscribeDatabentoCME(mkt_config, key_it->second, trading_date_int, publisher,
                              accept_bad_book);
      } else {
        LOG(ERROR) << "DataBentoCME: no API key found";
      }
    } else if (strcmp(mkt_config_pair.name.GetString(), "DataBentoBoats") == 0) {
      auto key_it = mkt_to_apikeys.find(pktrade::Market::TopBookEquity);
      if (key_it != mkt_to_apikeys.end()) {
        std::cout << "Subscribing to databento BOATS" << std::endl;
        subscribeDatabentoBoats(mkt_config, key_it->second, publisher);
      } else {
        LOG(ERROR) << "DataBentoBoats: no API key found";
      }
    } else if (strcmp(mkt_config_pair.name.GetString(), "RelayDBCME") == 0) {
      subscribeRelayDbCme(mkt_config, trading_date_int, publisher, accept_bad_book);
    }
  }
}

// Re-read `conf` (which may be mid-write) and, for each intraday-reloadable
// market, record symbols not already in `known` and nudge that feed to
// resubscribe. `known` is owned by the caller (main thread only) and updated in
// place, so it needs no locking. Returns true on a clean parse (caller advances
// the stored mtime only then, so a torn read is retried next cycle).
//
// Add-only: symbols present at startup or added on a prior cycle are skipped;
// nothing is ever unsubscribed here.
bool reloadConfigSymbols(const std::string& conf,
                         std::map<std::string, std::unordered_set<std::string>>& known) {
  std::ifstream ifs(conf);
  if (!ifs) {
    LOG(WARNING) << "Config reload: cannot open " << conf;
    return false;
  }
  std::stringstream ss;
  ss << ifs.rdbuf();
  const std::string contents = ss.str();

  rapidjson::Document doc;
  doc.Parse<rapidjson::kParseCommentsFlag>(contents.c_str());
  if (doc.HasParseError() || !doc.IsObject() || !doc.HasMember("subscriptions") ||
      !doc["subscriptions"].IsObject()) {
    // Almost certainly caught the file mid-write; skip and retry next cycle.
    LOG(WARNING) << "Config reload: parse error or unexpected shape, skipping this cycle";
    return false;
  }

  std::vector<std::string> new_hl_syms;  // subscribed live below (no reconnect)
  bool relay_cme_added = false;
  bool node_added = false;

  for (auto& m : doc["subscriptions"].GetObject()) {
    const std::string market = m.name.GetString();
    if (!isIntradayReloadMarket(market)) continue;
    if (!m.value.IsObject() || !m.value.HasMember("symbols") || !m.value["symbols"].IsArray()) {
      continue;
    }
    auto& known_set = known[market];
    for (auto& sym : m.value["symbols"].GetArray()) {
      if (!sym.IsString()) continue;
      std::string s = sym.GetString();
      if (!known_set.insert(s).second) continue;  // already handled (base or prior add)

      LOG(INFO) << "Config reload: new symbol " << s << " for market " << market;

      if (market == "HyperliquidNode") {
        // Swap in a widened filter = current ∪ {s}. If the current filter is
        // empty (accept-all) the node already receives this symbol, so leave
        // it untouched — narrowing to a restricted set would be wrong.
        std::lock_guard<std::mutex> lk(g_hl_node_filter_mtx);
        if (g_hl_node_filter && !g_hl_node_filter->empty()) {
          auto next = std::make_shared<std::unordered_set<std::string>>(*g_hl_node_filter);
          next->insert(s);
          g_hl_node_filter = std::move(next);
          // Publish the swap so the relays refresh their cached copy.
          g_hl_node_filter_version.fetch_add(1, std::memory_order_release);
          node_added = true;
        }
      } else {
        std::lock_guard<std::mutex> lk(g_extra_syms_mtx);
        g_extra_syms[market].push_back(s);
        if (market == "Hyperliquid") new_hl_syms.push_back(s);
        else if (market == "DataBentoEquities")
          g_eq_extra.count.fetch_add(1, std::memory_order_release);
        else if (market == "DataBentoBoats")
          g_boats_extra.count.fetch_add(1, std::memory_order_release);
        else if (market == "DataBentoCME")
          g_cme_extra.count.fetch_add(1, std::memory_order_release);
        else if (market == "RelayDBCME") {
          relay_cme_added = true;
          // Bump under the same lock as the push_back so the receiver, once it
          // observes the higher count, is guaranteed to see the appended symbol.
          g_relay_cme_extra_count.fetch_add(1, std::memory_order_release);
        }
      }
    }
  }

  // Nudge each affected feed once, after all new symbols are recorded.
  if (!new_hl_syms.empty()) {
    // Subscribe the new symbols on the live websocket — no reconnect. If there's
    // no open socket yet (startup race or mid-reconnect), the send is skipped;
    // the Open handler resubscribes everything from g_extra_syms on connect, so
    // the add is still durable.
    if (ix::WebSocket* ws = g_hl_ws.load()) {
      LOG(INFO) << "Config reload: subscribing " << new_hl_syms.size()
                << " new Hyperliquid symbol(s) on the live websocket";
      for (const auto& s : new_hl_syms) {
        hlSubscribeSymbol(ws, s);
      }
    } else {
      LOG(WARNING) << "Config reload: Hyperliquid symbols added but no live WS handle; "
                      "they will be subscribed on the next connect";
    }
  }
  // DataBento EQ/BOATS/CME: no explicit nudge — bumping the extra count above is
  // enough. Each feed's record callback live-subscribes the new symbols on its
  // own thread (drainDbExtras), with a runner-side reconnect fallback if the
  // callback is silent. The per-symbol "new symbol …" line above already logs them.
  if (relay_cme_added) {
    // No pkmultifeed reconnect — the receiver thread applies the additions
    // itself. Data flows once pkrelay is also updated to spray the new symbols.
    LOG(INFO) << "Config reload: RelayDBCME ingress readied for new symbols "
                 "(requires pkrelay to spray them; no pkmultifeed reconnect)";
  }
  if (node_added) {
    LOG(INFO) << "Config reload: HyperliquidNode filter widened (no reconnect needed)";
  }
  return true;
}

} // namespace pktrade

int main(int argc, char* argv[]) {
  std::string conf;
  std::string trading_date_str = "TOMORROW";
  std::string out_dir;
  bool accept_bad_book = false;
  bool cme_fusion_stats = false;

  CLI::App cmd_flags("pkmultifeed");
  cmd_flags.add_option("-f,--conf", conf, "Feed conf")->required();
  cmd_flags
      .add_option("--date", trading_date_str,
                  "Trading date (YYYYMMDD or TODAY/TOMORROW/TOMORROW2/TOMORROW3)")
      ->capture_default_str();
  cmd_flags.add_option("--out-dir", out_dir, "Output/log directory (default: cwd)");
  // Databento sets F_MAYBE_BAD_BOOK when it detects an unrecoverable sequence
  // gap on the venue channel. The book state may be incomplete but the data is
  // real. Without this flag, bad book messages are skipped and count toward the
  // bad_count restart threshold — which can cause a restart loop on machines
  // with frequent reconnects. With this flag, bad book messages are accepted
  // and published. Note: CME also sends test data on holidays/Saturdays that
  // may carry this flag — use with caution on non-trading days.
  cmd_flags.add_flag("--acceptbadbook", accept_bad_book,
                     "Accept and publish DataBento messages with F_MAYBE_BAD_BOOK flag");
  // Enable detailed per-(symbol, source) fusion stats (events_seen,
  // cas_wins, avg/max delay) with a periodic LOG(INFO) dump every 5
  // minutes plus a final dump on shutdown. Off by default: the hot path
  // pays a one-atomic-bool branch and skips all counter work. Per-message
  // delay logs in the TCP and UDP paths stay on in both modes, so basic
  // ms_behind is always grep-able from the log.
  cmd_flags.add_flag(
      "--cme-fusion-stats", cme_fusion_stats,
      "Enable detailed per-(symbol, source) CME fusion stats + periodic dump");
  PARSE(cmd_flags, argc, argv);

  if (out_dir.empty()) {
    out_dir = std::filesystem::current_path().string();
  }
  FLAGS_log_dir = out_dir;

  google::InitGoogleLogging(argv[0]);

  // Set the CME fusion stats mode before any subscribe function runs.
  // The flag is read inside processCmeQuote's record_seen / record_win and
  // inside CmeFusionState::register_symbol (for lazy reporter-thread
  // spawn), so it must be in place before subWebsockets kicks off the CME
  // paths.
  pktrade::cmeFusionState().set_detailed_stats_enabled(cme_fusion_stats);
  LOG(INFO) << "CME fusion stats: "
            << (cme_fusion_stats ? "ENABLED (detailed mode)" : "disabled (fast mode)");

  std::signal(SIGINT, pktrade::handleShutdown);
  std::signal(SIGTERM, pktrade::handleShutdown);
  std::signal(SIGPIPE, SIG_IGN);  // Let writes fail with EPIPE instead of killing the process

  // Parse config file.
  rapidjson::Document config_doc = pktrade::util::read_json_file(conf);

  // Don't need secret keys for feed.
  std::map<pktrade::Market, std::string> mkt_to_apikeys;

  // Remove stale socket file from a previous crash before binding.
  std::remove("/tmp/feed-sock-cpp");

  zmq::context_t ctx;
  zmq::socket_t publisher(ctx, ZMQ_PUB);
  // LINGER=0 so ctx.close() can't block on undelivered messages. zmq defaults to
  // infinite linger, meaning zmq_ctx_term() waits forever for anything still
  // queued to a connected-but-not-draining subscriber — a hang with no timeout,
  // and the prime suspect for the 20260717 gf3 instance that never exited.
  publisher.set(zmq::sockopt::linger, 0);
  const std::string pub_endpoint = "ipc:///tmp/feed-sock-cpp";
  publisher.bind(pub_endpoint);
  LOG(INFO) << "pkmultifeed PUB bound to " << pub_endpoint;

  std::vector<ix::WebSocket*> websockets;

  sleep(4);

  // For tracking whether to publish or not.
  std::map<pktrade::Market, std::map<std::string, int64_t>> mkt_sym_latest_update;
  std::map<pktrade::Market, std::map<std::string, int64_t>> mkt_sym_latest_trd;
  std::map<pktrade::Market, std::map<std::string, int64_t>> mkt_sym_latest_ticker;

  // More of these on bybit, tbh Going to make these bybit-specific.
  // The depth-1 we can use as ticker. Everything else, let's store as its own
  // thing.
  std::map<pktrade::Market, std::map<std::string, int64_t>> mkt_sym_latest_update_50;
  std::map<pktrade::Market, std::map<std::string, int64_t>> mkt_sym_latest_update_200;
  std::map<pktrade::Market, std::map<std::string, int64_t>> mkt_sym_latest_update_500;

  // For markets where we need to send our own heartbeats.
  std::map<pktrade::Market, int64_t> last_heartbeat_t;

  std::map<pktrade::Market, std::vector<std::string>> sub_stream_names;

  // Not sure if I need this in addition to the
  std::mutex t_mutex;

  std::cout << " Starting feed " << std::endl;
  // Read secrets
  pktrade::readSecrets(config_doc["subscriptions"], mkt_to_apikeys);

  int trading_date_int = pktrade::resolveTradingDate(trading_date_str);
  LOG(INFO) << "Trading date resolved to " << trading_date_int;

  // Compute EOD shutdown time: 18:00 ET on the trading date, minus 15 seconds.
  // The 15s buffer gives the old proc time to fully unwind before the next
  // day's pkmultifeed starts at 18:00, avoiding a zmq publisher-bind race.
  {
    int y = trading_date_int / 10000;
    int m = (trading_date_int / 100) % 100;
    int d = trading_date_int % 100;
    auto trading_day = date::local_days{date::year{y} / m / d};
    date::local_time<std::chrono::seconds> eod_local{trading_day.time_since_epoch() +
                                                     std::chrono::hours(18)};
    auto* ny_zone = date::locate_zone("America/New_York");
    auto eod_sys = ny_zone->to_sys(eod_local);
    auto eod_secs =
        std::chrono::duration_cast<std::chrono::seconds>(eod_sys.time_since_epoch()).count() - 15;
    pktrade::g_eod_secs.store(eod_secs);
    LOG(INFO) << "EOD shutdown at epoch " << eod_secs << " (18:00 ET on " << trading_date_int
              << " minus 15s)";
  }

  // handle websocket subs
  pktrade::subWebsockets(config_doc, websockets, mkt_sym_latest_update, mkt_sym_latest_trd,
                         mkt_sym_latest_ticker, mkt_sym_latest_update_50, mkt_sym_latest_update_200,
                         mkt_sym_latest_update_500, t_mutex, publisher, ctx, last_heartbeat_t,
                         sub_stream_names, mkt_to_apikeys, trading_date_int,
                         accept_bad_book);

  std::cout << " Finished subbing to wbsockets" << std::endl;
  sleep(10);

  std::cout << " Asking for snapshots" << std::endl;

  // Handle snapshot requests.
  std::shared_ptr<cpr::Session> session = std::make_shared<cpr::Session>();

  int snapshot_freq = config_doc["snapshot_frequency"].GetDouble();
  int64_t last_snapshot_t = 0;
  // If we exceed the frequency, then send a request you know?
  // Async.
  int64_t now;

  // Just do like, 10 snapshots.
  // I don't really want to debug right now, but sometimes the snapshot for
  // binancefutures is problematic. If this issue happens, have it be in the
  // beginning rather than sometime in the middle of the day.
  int n_snapshots = 0;

  // Intraday config reload state. Seed the tracker with the base symbols
  // already subscribed at startup so the periodic reload only acts on
  // additions. Only the main thread touches known_syms, so it needs no lock.
  std::map<std::string, std::unordered_set<std::string>> known_syms;
  for (auto& m : config_doc["subscriptions"].GetObject()) {
    std::string market = m.name.GetString();
    if (!pktrade::isIntradayReloadMarket(market)) continue;
    if (!m.value.IsObject() || !m.value.HasMember("symbols") || !m.value["symbols"].IsArray())
      continue;
    for (auto& sym : m.value["symbols"].GetArray()) {
      if (sym.IsString()) known_syms[market].insert(sym.GetString());
    }
  }
  int64_t last_config_reload_check = 0;
  std::filesystem::file_time_type last_conf_mtime;
  bool have_conf_mtime = false;
  try {
    last_conf_mtime = std::filesystem::last_write_time(conf);
    have_conf_mtime = true;
  } catch (const std::exception& e) {
    LOG(WARNING) << "Config reload: initial mtime read failed: " << e.what();
  }

  // At some point, stop.
  while (!pktrade::g_shutdown.load()) {
    try {
      now = std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::system_clock::now().time_since_epoch())
                .count();

      // EOD auto-shutdown (18:00 ET on trading date, minus 5s)
      int64_t eod = pktrade::g_eod_secs.load();
      if (eod > 0 && now >= eod) {
        LOG(INFO) << "EOD time reached, initiating shutdown";
        pktrade::g_shutdown.store(true);
        break;
      }

      if (pktrade::g_shutdown.load()) {
        break;
      }

      // Periodically re-read the conf and start subscribing to any symbols
      // added intraday (add-only). Only re-parse when the file's mtime moves.
      if (now - last_config_reload_check >= pktrade::kConfigReloadIntervalSec) {
        last_config_reload_check = now;
        try {
          auto mtime = std::filesystem::last_write_time(conf);
          if (!have_conf_mtime || mtime != last_conf_mtime) {
            LOG(INFO) << "Config reload: conf changed, re-reading " << conf;
            if (pktrade::reloadConfigSymbols(conf, known_syms)) {
              last_conf_mtime = mtime;
              have_conf_mtime = true;
            }
          }
        } catch (const std::exception& e) {
          LOG(ERROR) << "Config reload: mtime check failed: " << e.what();
        }
      }

      if (now - last_snapshot_t > snapshot_freq && n_snapshots < 10) {
        // Consider asking for a snapshot. Iterate through all the symbols we
        // care about and get one. Note that bybit does not have an explicit
        // receive-snapshot function. So we don't need to do this.
        n_snapshots++;
        last_snapshot_t = now;
        std::string api_key;
        // Go through snapshots.
        for (auto& mkt_config_pair : config_doc["subscriptions"].GetObject()) {
          pktrade::Market cur_mkt;

          if ((strcmp(mkt_config_pair.name.GetString(), "BinanceSPOT") == 0) ||
              (strcmp(mkt_config_pair.name.GetString(), "BinanceFutures") == 0) ||
              (strcmp(mkt_config_pair.name.GetString(), "BinanceCOINFutures") == 0)) {
            if ((strcmp(mkt_config_pair.name.GetString(), "BinanceSPOT") == 0)) {
              cur_mkt = pktrade::Market::BinanceSPOT;
              api_key = mkt_to_apikeys[pktrade::Market::BinanceSPOT];
            } else if (strcmp(mkt_config_pair.name.GetString(), "BinanceFutures") == 0) {
              cur_mkt = pktrade::Market::BinanceFutures;
              api_key = mkt_to_apikeys[pktrade::Market::BinanceFutures];
            } else if (strcmp(mkt_config_pair.name.GetString(), "BinanceCOINFutures") == 0) {
              cur_mkt = pktrade::Market::BinanceCOINFutures;
              api_key = mkt_to_apikeys[pktrade::Market::BinanceCOINFutures];
            }

            // Make the call
            for (rapidjson::Value& sym : mkt_config_pair.value["symbols"].GetArray()) {
              // Request a snapshot for this symbol.
              std::string sym_str = sym.GetString();
              std::string tgt_url;
              if (cur_mkt == pktrade::Market::BinanceSPOT) {
                tgt_url = fmt::format("https://api.binance.com/api/v3/depth?symbol={}&limit=1000",
                                      sym_str);
              } else if (cur_mkt == pktrade::Market::BinanceFutures) {
                tgt_url = fmt::format("https://fapi.binance.com/fapi/v1/"
                                      "depth?symbol={}&limit=1000",
                                      sym_str);
              } else if (cur_mkt == pktrade::Market::BinanceCOINFutures) {
                tgt_url = fmt::format("https://dapi.binance.com/dapi/v1/"
                                      "depth?symbol={}&limit=1000",
                                      sym_str);
              }

              cpr::GetCallback(
                  [&, sym = sym_str, mkt = cur_mkt](cpr::Response r) {
                    try {
                      if (r.status_code == 200) {
                        pktrade::mdmsg::PbMessage pb_msg;
                        if (mkt == pktrade::Market::BinanceSPOT) {
                          pb_msg = pktrade::decodeBinanceBookSnapshotCommon(r.text);
                          pb_msg.set_market(pktrade::mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
                        } else if (mkt == pktrade::Market::BinanceFutures) {
                          pb_msg = pktrade::decodeBinanceBookSnapshotCommon(r.text);
                          pb_msg.set_market(pktrade::mdmsg::PbMarket::PBMARKET_BINANCE_FUTURES);
                        } else if (mkt == pktrade::Market::BinanceCOINFutures) {
                          pb_msg = pktrade::decodeBinanceBookSnapshotCommon(r.text);
                          pb_msg.set_market(pktrade::mdmsg::PbMarket::PBMARKET_BINANCE_COINFUTURES);
                        }
                        pb_msg.set_symbol_id(sym);
                        // Guess this is needed.

                        pktrade::publish(pb_msg, &publisher);

                        // OK, publish this now.
                      } else {
                        LOG(ERROR) << "Snapshot call to " << r.url << " failed: code "
                                   << r.status_code << " status_line " << r.status_line;
                      }
                    } catch (const std::exception& e) {
                      LOG(ERROR) << "Error processing snapshot response: " << e.what();
                    }
                    return r.text;
                  },
                  cpr::Url{tgt_url}, cpr::Header{{"X-MBX-APIKEY", api_key}});
            }
          } else if (strcmp(mkt_config_pair.name.GetString(), "Hyperliquid") == 0) {
            // Slightly different request scheme.
            // I don't think I need this anymore tbh.
            /*

            for (rapidjson::Value& sym :
                 mkt_config_pair.value["symbols"].GetArray()) {
                std::string snapshot_endpoint =
                    mkt_config_pair.value["snapshot_endpoint"].GetString();

                session->SetUrl(cpr::Url{snapshot_endpoint});
                nlohmann::json payload = {{"type", "l2Book"},
                                          // 0x hex address.
                                          {"coin", sym.GetString()}};
                session->SetBody(cpr::Body{payload.dump()});
                session->SetHeader(cpr::Header{
                    {"Content-Type", "application/json"},
                });

                cpr::Response r = session->Post();

                // Parse the response snapshot.
                pktrade::mdmsg::PbMessage pb_msg =
                    pktrade::decodeHyperliquidL2BookSnapshot(r.text);
                pb_msg.set_symbol_id(sym.GetString());
                pktrade::publish(pb_msg, &publisher);
            }
            */
          }
        }
      }

      // Add small sleep to prevent tight loop
      std::this_thread::sleep_for(std::chrono::milliseconds(100));

    } catch (const std::exception& e) {
      LOG(ERROR) << "Error in main loop: " << e.what();
      // Sleep a bit longer on error to prevent rapid error loops
      std::this_thread::sleep_for(std::chrono::seconds(1));
    }
  }

  pktrade::g_shutdown.store(true);

  // Shutdown watchdog. Every shutdown trigger (SIGINT/SIGTERM via handleShutdown,
  // or the EOD check) converges here, so arming it once at the top of teardown
  // covers them all — and it must be here in the main thread, not in the signal
  // handler, since spawning a thread isn't async-signal-safe. The teardown below
  // joins several threads and closes zmq, none individually guaranteed to finish
  // (e.g. ws->stop() joins ixwebsocket's internal thread). If any wedges, the
  // process hangs indefinitely. cron replaces this process daily, so a hard exit
  // beats hanging: force it if teardown exceeds the deadline. 10s is comfortably
  // above normal teardown (<2s) yet inside the ~15s EOD buffer before the next
  // day's process binds the IPC socket.
  std::thread([]() {
    constexpr auto kDeadline = std::chrono::seconds(10);
    const auto start = std::chrono::steady_clock::now();
    while (!pktrade::g_teardown_done.load()) {
      if (std::chrono::steady_clock::now() - start > kDeadline) {
        const std::string msg = "Shutdown watchdog: teardown exceeded 10s, forcing "
                                "_exit(0) to avoid a hung process";
        LOG(ERROR) << msg;
        google::FlushLogFiles(google::GLOG_INFO);
        // send_mail_sync (blocking), not send_mail: the latter delivers from a
        // detached thread that the _exit(0) below would kill before it sends.
        try {
          pktrade::util::Mailer::instance().send_mail_sync("PKMULTIFEED SHUTDOWN HANG", msg);
        } catch (const std::exception& e) {
          LOG(ERROR) << "Shutdown watchdog: send_mail_sync failed: " << e.what();
        }
        _exit(0);
      }
      std::this_thread::sleep_for(std::chrono::seconds(1));
    }
  }).detach();

  // Join the HL ping/watchdog threads before touching the websockets: they hold
  // the raw ix::WebSocket* and call send()/close() on it, so deleting underneath
  // them is a use-after-free. They wake within ~1s of g_shutdown
  // (shutdownAwareSleep), so this doesn't meaningfully delay teardown.
  {
    std::lock_guard<std::mutex> lock(pktrade::g_hl_ws_threads_mtx);
    for (auto& th : pktrade::g_hl_ws_threads) {
      if (th.joinable()) {
        th.join();
      }
    }
  }

  for (auto ws : websockets) {
    ws->stop();
    delete ws;
  }

  // Join threads first — they manage their own client lifetime via client_it
  // and must finish before we invalidate iterators by clearing the list.
  {
    std::lock_guard<std::mutex> lock(pktrade::g_db_threads_mtx);
    for (auto& th : pktrade::g_db_threads) {
      if (th.joinable()) {
        th.join();
      }
    }
  }

  // Now safe to destroy any remaining LiveThreaded clients.
  pktrade::g_db_clients.clear();

  // Join the HL node relay threads before touching the zmq context: they own SUB
  // sockets created on `ctx`, and zmq_ctx_term() blocks (no timeout) until every
  // socket on the context is closed. They also publish through `publisher`, so
  // they must be done before we close it — zmq sockets aren't thread-safe.
  {
    std::lock_guard<std::mutex> lock(pktrade::g_hl_node_threads_mtx);
    for (auto& th : pktrade::g_hl_node_threads) {
      if (th.joinable()) {
        th.join();
      }
    }
  }

  publisher.close();
  ctx.close();
  std::remove("/tmp/feed-sock-cpp");

  // Teardown finished cleanly — defuse the shutdown watchdog so it doesn't
  // _exit() during the normal process exit / static destruction that follows.
  pktrade::g_teardown_done.store(true);

  return 0;
}
