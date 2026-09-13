// pkrelay: ingests databento MBP-1 CME feed on a short (us-east-2)
// path and forwards each quote over UDP to pkmultifeed consumers on
// the AWS backbone. The Tokyo-direct databento TCP connection has a
// ~155 ms RTT and suffers long-fat-network stalls during bursts; by
// running this relay close to databento (~15 ms RTT) and forwarding
// via UDP, we eliminate the backlog behaviour without moving
// pkmultifeed itself.
//
// The relay is intentionally dumb: no symbol-to-ours mapping, no
// session filter, no blend-leg logic, no protobuf. It ships the raw
// MBP-1 top-of-book using the wire format in pkrelay_packet.h, and
// each consumer does its own downstream processing. Keeping the
// relay trivial means it rarely needs redeploy.
//
// Usage:
//   ./pkrelay --conf relay_config.json [--date TOMORROW]

#include <algorithm>
#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <memory>
#include <netdb.h>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include <cctype>
#include <map>
#include <stdexcept>

#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <glog/logging.h>
#include <rapidjson/document.h>

#include "pktrade/feedrelay/pkrelay_packet.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"
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

std::atomic<bool> g_shutdown{false};
void handleShutdown(int) { g_shutdown.store(true); }

// EOD auto-shutdown epoch (18:00 ET on trading date, minus 15s). 0 means
// "not set"; the heartbeat thread flips g_shutdown when wall time reaches
// this. Mirrors pkmultifeed's g_eod_secs so both processes unwind before
// the next day's replacements start at 18:00.
std::atomic<int64_t> g_eod_secs{0};

// Match pkmultifeed's CME thresholds. On this short leg 4s of delay
// really does indicate something's wrong, so the values make sense
// here without modification.
// Sometimes random messages get delayed that don't mean a connection issue,
// so we only restart if 3 messages in a row exceed the threshold.
constexpr int64_t kCmeStaleThresholdMs = 4'000;
constexpr int kCmeStaleStrikes = 3;
constexpr int64_t kCmeSilentThresholdMs = 60'000;
constexpr int64_t kStaleAlertIntervalMs = 60'000;
constexpr int kMaxDatabentoBadMsgs = 100;

struct Destination {
  std::string host;
  uint16_t port = 0;
  sockaddr_in addr{};
};

struct RelayConfig {
  std::string api_key_path = "~/.creds/.DataBento.creds.json";
  std::vector<std::string> symbols; // base symbols (e.g. "CL", "HG")
  std::vector<Destination> destinations;
  uint32_t heartbeat_interval_ms = 1000;
};

// Per-connection state. Re-created on each reconnect; the outer
// g_seq counter survives across reconnects so consumers see a single
// monotonic sequence for the life of the process.
struct FeedState {
  std::map<uint32_t, std::string> idx_to_sym;
  std::atomic<int64_t> last_msg_time_ms{0};
  int64_t bad_count = 0;
  int64_t last_stale_alert_ms = 0;
  int consec_stale_msgs = 0;
};

std::atomic<uint64_t> g_seq{0};

// Mirrors pktrade::resolveTradingDate in pkmultifeed.cc. Duplicated here
// to keep pkrelay decoupled; if another binary needs it, factor into util.
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

int64_t nowWallMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// Approximate CME GLBX trading session check for the stale-feed watchdog.
// Matches the schedule encoded by pkmultifeed's generateCmeIntervals:
//   Sun 18:00 ET -> Fri 17:00 ET, with a daily break 17:00-18:00 ET.
// Inlined here to avoid porting the whole SessionIntervalCache over;
// close enough to keep the watchdog from paging when the market is
// legitimately closed. Holiday parity with pkmultifeed (both miss
// holidays - we don't consume the exchange calendar).
bool isInCmeSessionApprox(int64_t ts_ms) {
  if (ts_ms <= 0)
    return false;
  const auto* ny_zone = date::locate_zone("America/New_York");
  std::chrono::system_clock::time_point tp{std::chrono::milliseconds(ts_ms)};
  date::zoned_time ny_time{ny_zone, tp};
  auto local_time = ny_time.get_local_time();
  auto local_day = date::floor<date::days>(local_time);
  date::weekday wd{date::sys_days{date::year_month_day{local_day}}};
  auto tod = local_time - local_day;
  int hour = static_cast<int>(std::chrono::duration_cast<std::chrono::hours>(tod).count() % 24);
  switch (wd.c_encoding()) {
    case 6:
      return false; // Saturday: closed
    case 0:
      return hour >= 18; // Sunday: reopens 18:00 ET
    case 5:
      return hour < 17; // Friday: closes 17:00 ET
    default:
      return hour < 17 || hour >= 18; // Mon-Thu with daily break
  }
}

std::string expandHome(std::string path) {
  if (!path.empty() && path[0] == '~') {
    if (const char* home = std::getenv("HOME")) {
      path.replace(0, 1, home);
    }
  }
  return path;
}

Destination resolveDestination(const std::string& hostport) {
  auto colon = hostport.rfind(':');
  if (colon == std::string::npos) {
    throw std::runtime_error(fmt::format("destination '{}' missing :port", hostport));
  }
  Destination dst;
  dst.host = hostport.substr(0, colon);
  dst.port = static_cast<uint16_t>(std::stoi(hostport.substr(colon + 1)));

  addrinfo hints{};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_DGRAM;
  addrinfo* res = nullptr;
  int rc = getaddrinfo(dst.host.c_str(), nullptr, &hints, &res);
  if (rc != 0 || res == nullptr) {
    throw std::runtime_error(
        fmt::format("getaddrinfo failed for '{}': {}", dst.host, gai_strerror(rc)));
  }
  const auto* in4 = reinterpret_cast<const sockaddr_in*>(res->ai_addr);
  dst.addr = {};
  dst.addr.sin_family = AF_INET;
  dst.addr.sin_port = htons(dst.port);
  dst.addr.sin_addr = in4->sin_addr;
  freeaddrinfo(res);
  return dst;
}

RelayConfig loadConfig(const std::string& path) {
  rapidjson::Document doc = pktrade::util::read_json_file(path);
  RelayConfig c;

  if (doc.HasMember("api_key_path") && doc["api_key_path"].IsString()) {
    c.api_key_path = doc["api_key_path"].GetString();
  }
  c.api_key_path = expandHome(c.api_key_path);

  if (!doc.HasMember("symbols") || !doc["symbols"].IsArray() || doc["symbols"].GetArray().Empty()) {
    throw std::runtime_error("config missing non-empty 'symbols' array");
  }
  for (const auto& s : doc["symbols"].GetArray()) {
    c.symbols.emplace_back(s.GetString());
  }

  if (!doc.HasMember("destinations") || !doc["destinations"].IsArray() ||
      doc["destinations"].GetArray().Empty()) {
    throw std::runtime_error("config missing non-empty 'destinations' array");
  }
  for (const auto& d : doc["destinations"].GetArray()) {
    c.destinations.push_back(resolveDestination(d.GetString()));
  }

  if (doc.HasMember("heartbeat_interval_ms") && doc["heartbeat_interval_ms"].IsUint()) {
    c.heartbeat_interval_ms = doc["heartbeat_interval_ms"].GetUint();
  }

  return c;
}

std::string readApiKey(const std::string& path) {
  if (!std::filesystem::exists(path)) {
    throw std::runtime_error(fmt::format("api key path does not exist: {}", path));
  }
  auto doc = pktrade::util::read_json_file(path);
  if (!doc.HasMember("api_key")) {
    throw std::runtime_error(fmt::format("api key file {} missing 'api_key'", path));
  }
  return doc["api_key"].GetString();
}

void packSym(char (&dst)[pktrade::feedrelay::kPkRelaySymBytes], const std::string& src) {
  std::memset(dst, 0, pktrade::feedrelay::kPkRelaySymBytes);
  const auto n = std::min(src.size(), pktrade::feedrelay::kPkRelaySymBytes);
  std::memcpy(dst, src.data(), n);
}

// UDP is best-effort and the outer bound is fixed; failures here are
// rate-limited rather than fatal so a single blip on one destination
// doesn't spam the log.
void sendToAll(int sock, const std::vector<Destination>& dests,
               const pktrade::feedrelay::DatabentoCmeMbpPacket& pkt) {
  for (const auto& dest : dests) {
    auto rc = ::sendto(sock, &pkt, sizeof(pkt), MSG_DONTWAIT,
                       reinterpret_cast<const sockaddr*>(&dest.addr), sizeof(dest.addr));
    if (rc < 0) {
      LOG_EVERY_N(WARNING, 1000) << "pkrelay: sendto(" << dest.host << ":" << dest.port
                                 << ") failed: " << std::strerror(errno);
    }
  }
}

} // namespace

int main(int argc, char* argv[]) {
  std::string conf;
  std::string trading_date_str = "TOMORROW";
  std::string out_dir;
  bool accept_bad_book = false;

  CLI::App cmd_flags("pkrelay");
  cmd_flags.add_option("-f,--conf", conf, "Relay conf")->required();
  cmd_flags
      .add_option("--date", trading_date_str,
                  "Trading date (YYYYMMDD or TODAY/TOMORROW/TOMORROW2/TOMORROW3)")
      ->capture_default_str();
  cmd_flags.add_option("--out-dir", out_dir, "Output/log directory (default: cwd)");
  cmd_flags.add_flag("--acceptbadbook", accept_bad_book,
                     "Forward databento messages even when F_MAYBE_BAD_BOOK is set");
  PARSE(cmd_flags, argc, argv);

  if (out_dir.empty()) {
    out_dir = std::filesystem::current_path().string();
  }
  FLAGS_log_dir = out_dir;

  google::InitGoogleLogging(argv[0]);
  std::signal(SIGINT, handleShutdown);
  std::signal(SIGTERM, handleShutdown);
  std::signal(SIGPIPE, SIG_IGN);

  RelayConfig config;
  try {
    config = loadConfig(conf);
  } catch (const std::exception& e) {
    LOG(ERROR) << "pkrelay: failed to load config: " << e.what();
    return 1;
  }

  std::string api_key;
  try {
    api_key = readApiKey(config.api_key_path);
  } catch (const std::exception& e) {
    LOG(ERROR) << "pkrelay: failed to read api key: " << e.what();
    return 1;
  }

  int trading_date_int = resolveTradingDate(trading_date_str);
  LOG(INFO) << "pkrelay: trading date " << trading_date_int;

  // Compute EOD shutdown time: 18:00 ET on the trading date, minus 15s.
  // The 15s buffer gives this proc time to fully unwind before the next
  // day's pkrelay starts at 18:00, avoiding any port-rebind race on the
  // UDP socket or client-reconnect overlap.
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
    g_eod_secs.store(eod_secs);
    LOG(INFO) << "pkrelay: EOD shutdown at epoch " << eod_secs << " (18:00 ET on "
              << trading_date_int << " minus 15s)";
  }

  std::vector<std::string> db_symbols;
  for (const auto& sym : config.symbols) {
    auto contracts = pktrade::symbolizer::get_all_contracts_for_date(sym, trading_date_int);
    for (const auto& [db_sym, weight] : contracts) {
      db_symbols.push_back(db_sym);
      LOG(INFO) << "pkrelay: subscribing " << db_sym << " (base " << sym << ", weight " << weight
                << ")";
    }
  }
  if (db_symbols.empty()) {
    LOG(ERROR) << "pkrelay: no symbols resolved from config";
    return 1;
  }

  int sock = ::socket(AF_INET, SOCK_DGRAM, 0);
  if (sock < 0) {
    LOG(ERROR) << "pkrelay: socket() failed: " << std::strerror(errno);
    return 1;
  }

  LOG(INFO) << "pkrelay: forwarding to " << config.destinations.size() << " destination(s)";
  for (const auto& d : config.destinations) {
    LOG(INFO) << "  -> " << d.host << ":" << d.port;
  }

  // Heartbeat thread: lets consumers tell "quiet market" from "relay
  // down". Keeps going across inner reconnects since it doesn't rely
  // on any databento state.
  std::thread heartbeat_thread([&, sock]() {
    while (!g_shutdown.load()) {
      std::this_thread::sleep_for(std::chrono::milliseconds(config.heartbeat_interval_ms));
      if (g_shutdown.load())
        break;
      // EOD auto-shutdown check. Cheap enough to run every heartbeat tick;
      // piggy-backing here avoids a dedicated watcher thread.
      {
        int64_t eod = g_eod_secs.load();
        int64_t now_s = std::chrono::duration_cast<std::chrono::seconds>(
                            std::chrono::system_clock::now().time_since_epoch())
                            .count();
        if (eod > 0 && now_s >= eod) {
          LOG(INFO) << "pkrelay: EOD time reached, initiating shutdown";
          g_shutdown.store(true);
          break;
        }
      }
      pktrade::feedrelay::DatabentoCmeMbpPacket pkt{};
      pkt.magic = pktrade::feedrelay::kPkRelayMagic;
      pkt.version = pktrade::feedrelay::kPkRelayVersion;
      pkt.type = static_cast<uint16_t>(pktrade::feedrelay::PacketType::Heartbeat);
      pkt.seq = g_seq.fetch_add(1, std::memory_order_relaxed);
      pkt.ts_send_ms = nowWallMs();
      sendToAll(sock, config.destinations, pkt);
    }
  });

  // Outer reconnect loop. Fresh state each iteration; g_seq persists.
  while (!g_shutdown.load()) {
    try {
      auto state = std::make_shared<FeedState>();

      auto client = std::make_unique<databento::LiveThreaded>(
          databento::LiveThreaded::Builder()
              .SetKey(api_key)
              .SetDataset("GLBX.MDP3")
              .SetSendTsOut(true)
              .SetBufferSize(1 << 24) // 16 MB
              .SetSlowReaderBehavior(databento::SlowReaderBehavior::Skip)
              .BuildThreaded());
      client->Subscribe(db_symbols, databento::Schema::Mbp1, databento::SType::RawSymbol);

      auto restart_requested = std::make_shared<std::atomic<bool>>(false);
      LOG(INFO) << "pkrelay: databento client started";

      client->Start([state, sock, restart_requested, accept_bad_book, dests = config.destinations](
                        const databento::Record& record) -> databento::KeepGoing {
        try {
          if (g_shutdown.load()) {
            return databento::KeepGoing::Stop;
          }

          if (auto* sym_map = record.GetIf<databento::SymbolMappingMsg>()) {
            std::string stype_in(sym_map->stype_in_symbol.data());
            state->idx_to_sym[sym_map->hd.instrument_id] = stype_in;
            LOG(INFO) << "pkrelay SymbolMapping: " << stype_in << " -> "
                      << sym_map->hd.instrument_id;
            return databento::KeepGoing::Continue;
          }
          if (auto* sys_msg = record.GetIf<databento::SystemMsg>()) {
            LOG(INFO) << "pkrelay System: " << std::string(sys_msg->msg.data());
            return databento::KeepGoing::Continue;
          }
          if (record.Holds<databento::StatusMsg>()) {
            return databento::KeepGoing::Continue;
          }
          if (auto* err_msg = record.GetIf<databento::ErrorMsg>()) {
            LOG(ERROR) << "pkrelay Error: " << std::string(err_msg->err.data());
            return databento::KeepGoing::Continue;
          }

          // MBP-1 record (possibly wrapped in WithTsOut since we set
          // SendTsOut above).
          const databento::Mbp1Msg* mbp1 = nullptr;
          bool has_ts_out = false;
          int64_t ts_out_ms = 0;
          if (const auto* with_ts = record.GetIf<databento::WithTsOut<databento::Mbp1Msg>>()) {
            mbp1 = &with_ts->rec;
            const auto ts_out_raw = with_ts->ts_out.time_since_epoch().count();
            if (ts_out_raw != databento::kUndefTimestamp) {
              has_ts_out = true;
              ts_out_ms = static_cast<int64_t>(ts_out_raw / 1'000'000);
            }
          } else if (const auto* plain = record.GetIf<databento::Mbp1Msg>()) {
            mbp1 = plain;
          }
          if (mbp1 == nullptr) {
            return databento::KeepGoing::Continue;
          }

          int64_t now_ms = nowWallMs();
          state->last_msg_time_ms.store(now_ms);

          auto sym_it = state->idx_to_sym.find(mbp1->hd.instrument_id);
          if (sym_it == state->idx_to_sym.end()) {
            LOG(WARNING) << "pkrelay MBP1 without mapping: instrument_id="
                         << mbp1->hd.instrument_id;
            return databento::KeepGoing::Continue;
          }

          if (state->bad_count >= kMaxDatabentoBadMsgs) {
            auto err_msg =
                fmt::format("pkrelay: {} bad DataBento messages (threshold {}), restarting",
                            state->bad_count, kMaxDatabentoBadMsgs);
            LOG(ERROR) << err_msg;
            try {
              pktrade::util::Mailer::instance().send_mail("PKRELAY DOWN", err_msg);
            } catch (const std::exception& e) {
              LOG(ERROR) << "pkrelay: send_mail failed: " << e.what();
            }
            state->bad_count = 0;
            restart_requested->store(true);
            return databento::KeepGoing::Stop;
          }

          const uint64_t ts_event_raw = mbp1->hd.ts_event.time_since_epoch().count();
          const uint64_t ts_recv_raw = mbp1->ts_recv.time_since_epoch().count();
          bool maybe_bad_book = mbp1->flags.IsMaybeBadBook();
          bool invalid_msg =
              mbp1->price == databento::kUndefPrice || ts_event_raw == databento::kUndefTimestamp ||
              ts_recv_raw == databento::kUndefTimestamp || mbp1->flags.IsSnapshot() ||
              (!accept_bad_book && maybe_bad_book);
          if (maybe_bad_book) {
            // Log every 20 so if we're restarting for every 100 of these, we'll see a few first.
            LOG_EVERY_N(WARNING, 20) << "maybe_bad_book flag set for " << sym_it->second
                                     << (accept_bad_book ? " (accepted)" : " (skipped)");
          }
          if (invalid_msg) {
            state->bad_count++;
            return databento::KeepGoing::Continue;
          }

          pktrade::feedrelay::DatabentoCmeMbpPacket pkt{};
          pkt.magic = pktrade::feedrelay::kPkRelayMagic;
          pkt.version = pktrade::feedrelay::kPkRelayVersion;
          pkt.type = static_cast<uint16_t>(pktrade::feedrelay::PacketType::Quote);
          packSym(pkt.db_sym, sym_it->second);
          pkt.seq = g_seq.fetch_add(1, std::memory_order_relaxed);
          pkt.ts_send_ms = now_ms;
          pkt.ts_event_ms = static_cast<int64_t>(ts_event_raw / 1'000'000);
          pkt.ts_recv_ms = static_cast<int64_t>(ts_recv_raw / 1'000'000);
          pkt.bid_px_1e9 = mbp1->levels[0].bid_px;
          pkt.ask_px_1e9 = mbp1->levels[0].ask_px;
          pkt.bid_sz = static_cast<int32_t>(mbp1->levels[0].bid_sz);
          pkt.ask_sz = static_cast<int32_t>(mbp1->levels[0].ask_sz);
          pkt.flags = static_cast<uint32_t>(mbp1->flags.Raw());
          pkt.reserved = 0;

          sendToAll(sock, dests, pkt);

          int64_t recv_delay_ms = now_ms - pkt.ts_event_ms;
          if (recv_delay_ms > 1000) {
            // Always log if delay is over 1s, since it rarely is (100ms logs way too much).
            std::string ts_out_delay_str =
                has_ts_out ? std::to_string(now_ms - ts_out_ms) : "NA";
            LOG(INFO) << fmt::format(
                "pkrelay {} msgtime {} delay {}ms ts_out_delay {} seq {}",
                sym_it->second, pkt.ts_event_ms, recv_delay_ms, ts_out_delay_str, pkt.seq);
          } else {
            // Otherwise log only every 1000.
            std::string ts_out_delay_str =
                has_ts_out ? std::to_string(now_ms - ts_out_ms) : "NA";
            LOG_EVERY_N(INFO, 1000) << fmt::format(
                "pkrelay {} msgtime {} delay {}ms ts_out_delay {} seq {}",
                sym_it->second, pkt.ts_event_ms, recv_delay_ms, ts_out_delay_str, pkt.seq);
          }

          // Only treat stale delay as a restart condition when the
          // message's event time falls inside a CME session. Matches
          // pkmultifeed's TCP path, which drops out-of-session messages
          // via isInCmeSession() before the stale check runs. Without
          // this gate, a queued pre-close message delivered just after
          // 17:00 ET (or a session-boundary replay) would spuriously
          // tear down the databento client.
          if (isInCmeSessionApprox(pkt.ts_event_ms) && recv_delay_ms > kCmeStaleThresholdMs) {
            // Count the number of consecutive stale messages and restart if we've hit the limit
            state->consec_stale_msgs++;
            if (state->consec_stale_msgs < kCmeStaleStrikes) {
              LOG(WARNING) << fmt::format("pkrelay {} delay {}ms exceeds {}ms threshold, strike {}",
                                          sym_it->second, recv_delay_ms, kCmeStaleThresholdMs,
                                          state->consec_stale_msgs);
            } else {
              auto err_msg = fmt::format("pkrelay {} delay {}ms exceeds {}ms threshold, strike {}, "
                                         "restarting",
                                         sym_it->second, recv_delay_ms, kCmeStaleThresholdMs,
                                         state->consec_stale_msgs);
              LOG(ERROR) << err_msg;
              if (now_ms - state->last_stale_alert_ms > kStaleAlertIntervalMs) {
                state->last_stale_alert_ms = now_ms;
                try {
                  pktrade::util::Mailer::instance().send_mail("PKRELAY STALE", err_msg);
                } catch (const std::exception& e) {
                  LOG(ERROR) << "pkrelay: send_mail failed: " << e.what();
                }
              }
              restart_requested->store(true);
              return databento::KeepGoing::Stop;
            }
          } else {
            // Saw a non-stale message so reset the count
            state->consec_stale_msgs = 0;
          }
          return databento::KeepGoing::Continue;
        } catch (const std::exception& e) {
          LOG(ERROR) << "pkrelay callback exception: " << e.what();
          restart_requested->store(true);
          return databento::KeepGoing::Stop;
        } catch (...) {
          LOG(ERROR) << "pkrelay callback unknown exception";
          restart_requested->store(true);
          return databento::KeepGoing::Stop;
        }
      });

      const auto watchdog_timeout = std::chrono::milliseconds(kCmeSilentThresholdMs);
      while (!g_shutdown.load() && client) {
        auto rc = client->BlockForStop(watchdog_timeout);
        if (rc == databento::KeepGoing::Stop) {
          break;
        }
        int64_t last = state->last_msg_time_ms.load();
        if (last > 0) {
          int64_t now_ms = nowWallMs();
          // Only fire the silent-feed alert during CME trading hours.
          // Without this, every Friday 17:00 ET close would trip the
          // watchdog 60s later and enter a reconnect loop for the
          // entire weekend.
          if (!isInCmeSessionApprox(now_ms)) {
            continue;
          }
          if (now_ms - last > kCmeSilentThresholdMs) {
            auto err_msg =
                fmt::format("pkrelay watchdog: no data for {}ms (threshold {}ms), restarting",
                            now_ms - last, kCmeSilentThresholdMs);
            LOG(ERROR) << err_msg;
            try {
              pktrade::util::Mailer::instance().send_mail("PKRELAY SILENT", err_msg);
            } catch (const std::exception& e) {
              LOG(ERROR) << "pkrelay: send_mail failed: " << e.what();
            }
            restart_requested->store(true);
            break;
          }
        }
      }

      client.reset();

      if (g_shutdown.load()) {
        break;
      }
      if (!restart_requested->load()) {
        LOG(WARNING) << "pkrelay: databento connection lost unexpectedly, restarting";
      } else {
        LOG(WARNING) << "pkrelay: restarting after stale/silent feed";
      }
      std::this_thread::sleep_for(std::chrono::seconds(5));
    } catch (const std::exception& e) {
      LOG(ERROR) << "pkrelay fatal: " << e.what();
      try {
        pktrade::util::Mailer::instance().send_mail("PKRELAY DOWN",
                                                    std::string("Fatal: ") + e.what());
      } catch (...) {
      }
      std::this_thread::sleep_for(std::chrono::seconds(5));
    } catch (...) {
      LOG(ERROR) << "pkrelay fatal: unknown exception";
      std::this_thread::sleep_for(std::chrono::seconds(5));
    }
  }

  if (heartbeat_thread.joinable()) {
    heartbeat_thread.join();
  }
  ::close(sock);
  LOG(INFO) << "pkrelay: shutdown complete";
  return 0;
}
