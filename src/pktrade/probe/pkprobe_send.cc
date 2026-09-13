// pkprobe_send: Ohio-side sender for the AWS-backbone path-cluster
// experiment. Sends fixed-size UDP probes to a single Tokyo destination
// across N lanes (one dst port per lane) at a steady configured rate,
// and logs each successful sendto to a per-lane CSV for post-analysis.
//
// One socket, no bind: the kernel-assigned ephemeral src port is shared
// across all N lanes within a single run, so only the dst port differs
// across lanes - which is exactly the 5-tuple variation we want to study.
// Each daily restart (cron at 18:00 ET) gets a fresh ephemeral src port,
// mirroring pkrelay.
//
// See EXPERIMENT.md in this directory for the full design.
//
// Usage:
//   ./pkprobe_send --conf pkprobe_config.json [--out-dir DIR]

#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <netdb.h>
#include <netinet/in.h>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
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

struct ProbeConfig {
  std::string tokyo_ip;
  uint16_t base_port = 5000;
  int num_lanes = 16;
  int rate_per_lane_hz = 10;
  std::string output_dir;
};

ProbeConfig loadConfig(const std::string& path) {
  auto doc = pktrade::util::read_json_file(path);
  ProbeConfig c;
  if (!doc.HasMember("tokyo_ip") || !doc["tokyo_ip"].IsString()) {
    throw std::runtime_error("config missing string 'tokyo_ip'");
  }
  c.tokyo_ip = doc["tokyo_ip"].GetString();
  if (!doc.HasMember("base_port") || !doc["base_port"].IsUint()) {
    throw std::runtime_error("config missing uint 'base_port'");
  }
  c.base_port = static_cast<uint16_t>(doc["base_port"].GetUint());
  if (doc.HasMember("num_lanes") && doc["num_lanes"].IsInt()) {
    c.num_lanes = doc["num_lanes"].GetInt();
  }
  if (doc.HasMember("rate_per_lane_hz") && doc["rate_per_lane_hz"].IsInt()) {
    c.rate_per_lane_hz = doc["rate_per_lane_hz"].GetInt();
  }
  if (doc.HasMember("output_dir") && doc["output_dir"].IsString()) {
    c.output_dir = doc["output_dir"].GetString();
  }
  if (c.num_lanes <= 0 || c.num_lanes > 1024) {
    throw std::runtime_error("num_lanes out of range (1..1024)");
  }
  if (c.rate_per_lane_hz <= 0 || c.rate_per_lane_hz > 100000) {
    throw std::runtime_error("rate_per_lane_hz out of range (1..100000)");
  }
  return c;
}

sockaddr_in resolveAddr(const std::string& host, uint16_t port) {
  addrinfo hints{};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_DGRAM;
  addrinfo* res = nullptr;
  int rc = getaddrinfo(host.c_str(), nullptr, &hints, &res);
  if (rc != 0 || res == nullptr) {
    throw std::runtime_error(
        fmt::format("getaddrinfo({}) failed: {}", host, gai_strerror(rc)));
  }
  sockaddr_in out{};
  out.sin_family = AF_INET;
  out.sin_port = htons(port);
  out.sin_addr = reinterpret_cast<const sockaddr_in*>(res->ai_addr)->sin_addr;
  freeaddrinfo(res);
  return out;
}

// Next 18:00 ET that is >= 23h after startup, minus 15s. Matches pkrelay's
// EOD pattern; gives ~24h runs anchored to the cron-driven 18:00 ET start
// regardless of small jitter in when cron actually fires.
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

} // namespace

int main(int argc, char* argv[]) {
  std::string conf_path;
  std::string out_dir_override;

  CLI::App cmd("pkprobe_send");
  cmd.add_option("-f,--conf", conf_path, "Probe config JSON")->required();
  cmd.add_option("--out-dir", out_dir_override, "Output dir (overrides config)");
  PARSE(cmd, argc, argv);

  ProbeConfig cfg;
  try {
    cfg = loadConfig(conf_path);
  } catch (const std::exception& e) {
    fmt::print(stderr, "pkprobe_send: config load failed: {}\n", e.what());
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

  LOG(INFO) << "pkprobe_send: tokyo_ip=" << cfg.tokyo_ip
            << " base_port=" << cfg.base_port << " num_lanes=" << cfg.num_lanes
            << " rate_per_lane_hz=" << cfg.rate_per_lane_hz;

  std::vector<sockaddr_in> dests;
  dests.reserve(cfg.num_lanes);
  try {
    for (int i = 0; i < cfg.num_lanes; ++i) {
      dests.push_back(resolveAddr(cfg.tokyo_ip,
                                   static_cast<uint16_t>(cfg.base_port + i)));
    }
  } catch (const std::exception& e) {
    LOG(ERROR) << "pkprobe_send: " << e.what();
    return 1;
  }

  // One socket, no bind. Kernel-assigned ephemeral src port is shared across
  // all N lanes for this run, so only the dst port differs across lanes.
  int sock = ::socket(AF_INET, SOCK_DGRAM, 0);
  if (sock < 0) {
    LOG(ERROR) << "pkprobe_send: socket() failed: " << std::strerror(errno);
    return 1;
  }

  int64_t eod_secs = 0;
  try {
    eod_secs = computeEodSecs();
  } catch (const std::exception& e) {
    LOG(ERROR) << "pkprobe_send: " << e.what();
    ::close(sock);
    return 1;
  }
  LOG(INFO) << "pkprobe_send: EOD epoch " << eod_secs;

  std::string stamp = startupStampNy();
  std::vector<FILE*> lane_files(cfg.num_lanes, nullptr);
  for (int i = 0; i < cfg.num_lanes; ++i) {
    auto path = fmt::format("{}/send_{}_lane{:02}.csv", out_dir, stamp, i);
    FILE* f = std::fopen(path.c_str(), "w");
    if (f == nullptr) {
      LOG(ERROR) << "pkprobe_send: fopen " << path
                 << " failed: " << std::strerror(errno);
      for (auto* g : lane_files) {
        if (g != nullptr) std::fclose(g);
      }
      ::close(sock);
      return 1;
    }
    // Line-buffered so each row hits the kernel on newline; cheap enough at
    // 10 pps per lane that we don't care about throughput.
    std::setvbuf(f, nullptr, _IOLBF, BUFSIZ);
    std::fprintf(f, "seq,send_ts_ns\n");
    lane_files[i] = f;
  }

  // Pace by absolute steady_clock targets so jitter in one iteration doesn't
  // accumulate. steady_clock is monotonic and immune to system_clock jumps.
  using clock = std::chrono::steady_clock;
  const auto inter_packet = std::chrono::nanoseconds(
      1'000'000'000LL / (static_cast<int64_t>(cfg.num_lanes) * cfg.rate_per_lane_hz));
  const auto t_start = clock::now();

  pktrade::probe::PkProbePacket pkt{};
  pkt.magic = pktrade::probe::kPkProbeMagic;
  pkt.version = pktrade::probe::kPkProbeVersion;

  std::vector<uint64_t> next_seq(cfg.num_lanes, 0);
  uint64_t idx = 0;
  int64_t last_eod_check_idx = 0;
  uint64_t sent = 0;
  uint64_t send_fail = 0;

  while (!g_shutdown.load()) {
    // EOD check every ~1 s of packet ticks; system_clock::now via vDSO is
    // cheap but no reason to call it 160x/sec.
    if (idx - last_eod_check_idx >= static_cast<uint64_t>(cfg.num_lanes) *
                                         static_cast<uint64_t>(cfg.rate_per_lane_hz)) {
      last_eod_check_idx = idx;
      if (nowEpochSecs() >= eod_secs) {
        LOG(INFO) << "pkprobe_send: EOD reached";
        break;
      }
    }

    int lane = static_cast<int>(idx % static_cast<uint64_t>(cfg.num_lanes));
    pkt.lane_id = static_cast<uint16_t>(lane);
    pkt.seq = next_seq[lane]++;
    pkt.send_ts_ns = nowEpochNs();

    ssize_t rc = ::sendto(sock, &pkt, sizeof(pkt), MSG_DONTWAIT,
                          reinterpret_cast<const sockaddr*>(&dests[lane]),
                          sizeof(dests[lane]));
    if (rc < 0) {
      ++send_fail;
      LOG_EVERY_N(WARNING, 1000)
          << "pkprobe_send: sendto lane " << lane
          << " failed: " << std::strerror(errno);
    } else {
      ++sent;
      std::fprintf(lane_files[lane], "%llu,%lld\n",
                   static_cast<unsigned long long>(pkt.seq),
                   static_cast<long long>(pkt.send_ts_ns));
    }

    ++idx;
    std::this_thread::sleep_until(t_start + inter_packet * idx);
  }

  LOG(INFO) << "pkprobe_send: sent=" << sent << " send_fail=" << send_fail;

  for (auto* f : lane_files) {
    if (f != nullptr) {
      std::fflush(f);
      std::fclose(f);
    }
  }
  ::close(sock);
  LOG(INFO) << "pkprobe_send: shutdown complete";
  return 0;
}
