// pkprobe_recv: Tokyo-side receiver for the AWS-backbone path-cluster
// experiment. Listens on N UDP ports (one per lane), spawns one thread per
// lane, and logs each received probe to a per-lane CSV with the one-way
// transit time. Per-lane files mean each thread owns its FILE* outright -
// no shared file, no locks, no atomicity assumptions.
//
// EOD shutdown at 18:00 ET (matches pkrelay/pkmultifeed and the
// cron-driven daily restart pattern). On EOD or SIGINT/SIGTERM, the main
// thread flips g_shutdown; each lane thread notices on its next 2s recv
// timeout, drains its FILE* buffer, and exits.
//
// See EXPERIMENT.md in this directory for the full design.
//
// Usage:
//   ./pkprobe_recv --conf pkprobe_config.json [--out-dir DIR]

#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <netinet/in.h>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <sys/time.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <glog/logging.h>
#include <rapidjson/document.h>

#include "pktrade/probe/pkprobe_packet.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"

namespace {

std::atomic<bool> g_shutdown{false};
void handleShutdown(int) { g_shutdown.store(true); }

struct RecvConfig {
  uint16_t base_port = 5000;
  int num_lanes = 16;
  std::string output_dir;
};

RecvConfig loadConfig(const std::string& path) {
  auto doc = pktrade::util::read_json_file(path);
  RecvConfig c;
  if (!doc.HasMember("base_port") || !doc["base_port"].IsUint()) {
    throw std::runtime_error("config missing uint 'base_port'");
  }
  c.base_port = static_cast<uint16_t>(doc["base_port"].GetUint());
  if (doc.HasMember("num_lanes") && doc["num_lanes"].IsInt()) {
    c.num_lanes = doc["num_lanes"].GetInt();
  }
  if (doc.HasMember("output_dir") && doc["output_dir"].IsString()) {
    c.output_dir = doc["output_dir"].GetString();
  }
  if (c.num_lanes <= 0 || c.num_lanes > 1024) {
    throw std::runtime_error("num_lanes out of range (1..1024)");
  }
  return c;
}

// Mirrors pkprobe_send::computeEodSecs. Duplicated rather than factored into
// util because the probe binaries are intentionally throwaway.
int64_t computeEodSecs() {
  using namespace std::chrono;
  auto now = system_clock::now();
  auto threshold = now + hours(23);
  auto* ny = date::locate_zone("America/New_York");
  auto threshold_local = date::zoned_time{ny, threshold}.get_local_time();
  auto base_day = date::floor<date::days>(threshold_local);
  for (int i = 0; i < 3; ++i) {
    auto eod_local = date::local_time<seconds>{
        (base_day + date::days(i)).time_since_epoch() + hours(18)};
    auto eod_sys = ny->to_sys(eod_local);
    if (eod_sys >= threshold) {
      return duration_cast<seconds>(eod_sys.time_since_epoch()).count() - 15;
    }
  }
  throw std::runtime_error("computeEodSecs: could not resolve EOD within 3 days");
}

std::string startupStampNy() {
  auto* ny = date::locate_zone("America/New_York");
  auto now_s = std::chrono::floor<std::chrono::seconds>(std::chrono::system_clock::now());
  return date::format("%Y%m%d_%H%M%S", date::zoned_time{ny, now_s});
}

int64_t nowEpochSecs() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

int64_t nowEpochNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

struct LaneCtx {
  int lane_id;
  int sock;
  FILE* csv;
};

void receiverLoop(LaneCtx ctx) {
  pktrade::probe::PkProbePacket pkt{};
  uint64_t recv_ok = 0;
  uint64_t bad = 0;
  while (!g_shutdown.load()) {
    ssize_t n = ::recv(ctx.sock, &pkt, sizeof(pkt), 0);
    if (n < 0) {
      if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
        continue; // 2s SO_RCVTIMEO; loop checks g_shutdown
      }
      LOG_EVERY_N(ERROR, 1000) << "pkprobe_recv lane " << ctx.lane_id
                               << " recv failed: " << std::strerror(errno);
      continue;
    }
    const int64_t recv_ts_ns = nowEpochNs();
    if (n != static_cast<ssize_t>(sizeof(pkt))) {
      ++bad;
      LOG_EVERY_N(WARNING, 1000)
          << "pkprobe_recv lane " << ctx.lane_id << " short packet: " << n;
      continue;
    }
    if (pkt.magic != pktrade::probe::kPkProbeMagic ||
        pkt.version != pktrade::probe::kPkProbeVersion) {
      ++bad;
      LOG_EVERY_N(WARNING, 1000)
          << "pkprobe_recv lane " << ctx.lane_id << " bad magic/version (got 0x"
          << std::hex << pkt.magic << std::dec << "/" << pkt.version << ")";
      continue;
    }
    if (pkt.lane_id != static_cast<uint16_t>(ctx.lane_id)) {
      // Defense in depth: someone sent to the wrong port. Drop, log rarely.
      ++bad;
      LOG_EVERY_N(WARNING, 1000)
          << "pkprobe_recv lane " << ctx.lane_id
          << " received packet with lane_id " << pkt.lane_id;
      continue;
    }
    const int64_t one_way_ns = recv_ts_ns - pkt.send_ts_ns;
    std::fprintf(ctx.csv, "%llu,%lld,%lld,%lld\n",
                 static_cast<unsigned long long>(pkt.seq),
                 static_cast<long long>(pkt.send_ts_ns),
                 static_cast<long long>(recv_ts_ns),
                 static_cast<long long>(one_way_ns));
    ++recv_ok;
  }
  LOG(INFO) << "pkprobe_recv lane " << ctx.lane_id << " exiting; recv_ok=" << recv_ok
            << " bad=" << bad;
}

} // namespace

int main(int argc, char* argv[]) {
  std::string conf_path;
  std::string out_dir_override;

  CLI::App cmd("pkprobe_recv");
  cmd.add_option("-f,--conf", conf_path, "Probe config JSON")->required();
  cmd.add_option("--out-dir", out_dir_override, "Output dir (overrides config)");
  PARSE(cmd, argc, argv);

  RecvConfig cfg;
  try {
    cfg = loadConfig(conf_path);
  } catch (const std::exception& e) {
    fmt::print(stderr, "pkprobe_recv: config load failed: {}\n", e.what());
    return 1;
  }

  std::string out_dir = out_dir_override.empty() ? cfg.output_dir : out_dir_override;
  if (out_dir.empty()) {
    out_dir = std::filesystem::current_path().string();
  }
  std::filesystem::create_directories(out_dir);
  FLAGS_log_dir = out_dir;

  google::InitGoogleLogging(argv[0]);
  std::signal(SIGINT, handleShutdown);
  std::signal(SIGTERM, handleShutdown);
  std::signal(SIGPIPE, SIG_IGN);

  LOG(INFO) << "pkprobe_recv: base_port=" << cfg.base_port
            << " num_lanes=" << cfg.num_lanes;

  int64_t eod_secs = 0;
  try {
    eod_secs = computeEodSecs();
  } catch (const std::exception& e) {
    LOG(ERROR) << "pkprobe_recv: " << e.what();
    return 1;
  }
  LOG(INFO) << "pkprobe_recv: EOD epoch " << eod_secs;

  const std::string stamp = startupStampNy();
  std::vector<LaneCtx> lanes(cfg.num_lanes, LaneCtx{-1, -1, nullptr});

  auto cleanup = [&]() {
    for (auto& l : lanes) {
      if (l.csv != nullptr) {
        std::fflush(l.csv);
        std::fclose(l.csv);
        l.csv = nullptr;
      }
      if (l.sock >= 0) {
        ::close(l.sock);
        l.sock = -1;
      }
    }
  };

  for (int i = 0; i < cfg.num_lanes; ++i) {
    int sock = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
      LOG(ERROR) << "pkprobe_recv: socket() failed: " << std::strerror(errno);
      cleanup();
      return 1;
    }

    int rbuf = 8 * 1024 * 1024;
    if (::setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rbuf, sizeof(rbuf)) < 0) {
      LOG(WARNING) << "pkprobe_recv lane " << i
                   << ": SO_RCVBUF failed: " << std::strerror(errno);
    }
    struct timeval tmo{};
    tmo.tv_sec = 2;
    tmo.tv_usec = 0;
    if (::setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tmo, sizeof(tmo)) < 0) {
      LOG(WARNING) << "pkprobe_recv lane " << i
                   << ": SO_RCVTIMEO failed: " << std::strerror(errno);
    }

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(static_cast<uint16_t>(cfg.base_port + i));
    if (::bind(sock, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) < 0) {
      LOG(ERROR) << "pkprobe_recv: bind(:" << (cfg.base_port + i)
                 << ") failed: " << std::strerror(errno);
      ::close(sock);
      cleanup();
      return 1;
    }

    auto path = fmt::format("{}/recv_{}_lane{:02}.csv", out_dir, stamp, i);
    FILE* f = std::fopen(path.c_str(), "w");
    if (f == nullptr) {
      LOG(ERROR) << "pkprobe_recv: fopen " << path
                 << " failed: " << std::strerror(errno);
      ::close(sock);
      cleanup();
      return 1;
    }
    std::setvbuf(f, nullptr, _IOLBF, BUFSIZ);
    std::fprintf(f, "seq,send_ts_ns,recv_ts_ns,one_way_ns\n");

    lanes[i] = LaneCtx{i, sock, f};
    LOG(INFO) << "pkprobe_recv: listening UDP :" << (cfg.base_port + i)
              << " -> " << path;
  }

  std::vector<std::thread> threads;
  threads.reserve(cfg.num_lanes);
  for (const auto& l : lanes) {
    threads.emplace_back(receiverLoop, l);
  }

  while (!g_shutdown.load()) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
    if (nowEpochSecs() >= eod_secs) {
      LOG(INFO) << "pkprobe_recv: EOD reached";
      g_shutdown.store(true);
      break;
    }
  }

  for (auto& t : threads) {
    if (t.joinable()) t.join();
  }
  cleanup();
  LOG(INFO) << "pkprobe_recv: shutdown complete";
  return 0;
}
