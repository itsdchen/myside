// hl_book_check
//
// Subscribes to a ZMQ PUB socket carrying serialized mdmsg::PbMessage events
// (as produced by pymultifeed or the dedicated test publisher), applies each
// HL message via BookManager::onMD(), and periodically prints the
// BookManager-built book.
//
// Intended to validate the C++ BookManager path against our fast+slow merge
// on testnet. The companion Python publisher
// (overmind/strat_main/tools/hl_testnet_publish.py) produces the input feed.
//
// Sample command:
//   ./bin/hl_book_check --book Hyperliquid --sym BTC \
//       --endpoint ipc:///tmp/feed-sock --levels 25 --print-every 1

#include <algorithm>
#include <chrono>
#include <csignal>
#include <iostream>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include <fmt/format.h>
#include <magic_enum.hpp>
#include <zmq.hpp>

#include "mdmsg.pb.h"
#include "pktrade/book_manager.h"
#include "pktrade/level_book.h"
#include "pktrade/util/cli.h"

using namespace pktrade;

namespace {

std::sig_atomic_t g_stop = 0;
void on_signal(int) { g_stop = 1; }

class CountingListener : public BookListener {
 public:
  void onLevelUpdate(const LevelBook&, const LevelAdd&) override { ++adds; }
  void onLevelUpdate(const LevelBook&, const LevelModify&) override { ++modifies; }
  void onLevelUpdate(const LevelBook&, const LevelDelete&) override { ++deletes; }
  void onLevelUpdate(const LevelBook&, const Trade&) override { ++trades; }
  void onFinal(const LevelBook&) override { ++finals; }
  int64_t adds = 0, modifies = 0, deletes = 0, trades = 0, finals = 0;
};

template <typename T>
std::string to_str(const T& v) {
  std::ostringstream os;
  os << v;
  return os.str();
}

void print_book(const LevelBook& book, int max_levels) {
  const auto& bids = book.side<BuySide>();
  const auto& asks = book.side<SellSide>();

  auto bit = bids.begin();
  auto ait = asks.begin();
  int bid_n = static_cast<int>(bids.size());
  int ask_n = static_cast<int>(asks.size());

  fmt::print("    | {:>16} {:>14} | {:>16} {:>14} |\n", "BID PX", "QTY", "ASK PX", "QTY");
  for (int i = 0; i < max_levels && (bit != bids.end() || ait != asks.end()); ++i) {
    std::string bpx, bqty, apx, aqty;
    if (bit != bids.end()) {
      bpx = to_str(bit->px);
      bqty = to_str(bit->qty);
      ++bit;
    }
    if (ait != asks.end()) {
      apx = to_str(ait->px);
      aqty = to_str(ait->qty);
      ++ait;
    }
    fmt::print("{:>3} | {:>16} {:>14} | {:>16} {:>14} |\n", i + 1, bpx, bqty, apx, aqty);
  }
  fmt::print("    bids={} asks={}\n", bid_n, ask_n);
}

} // namespace

int main(int argc, char** argv) {
  std::string book_str = "Hyperliquid";
  std::string sym_str;
  std::string endpoint = "ipc:///tmp/feed-sock";
  int max_levels = 25;
  int print_every = 1;
  bool quiet = false;

  CLI::App cmd("hl_book_check - apply live merged HL feed through BookManager and print");
  cmd.add_option("-b,--book", book_str, "Market enum name (default Hyperliquid)");
  cmd.add_option("-s,--sym", sym_str, "Symbol id (e.g. BTC)")->required();
  cmd.add_option("-e,--endpoint", endpoint, "ZMQ SUB endpoint");
  cmd.add_option("-l,--levels", max_levels, "Max levels to print per side");
  cmd.add_option("--print-every", print_every, "Print book every N applied messages (0 = only on quit)");
  cmd.add_flag("-q,--quiet", quiet, "Suppress per-message book prints; only print summary on quit");
  PARSE(cmd, argc, argv);

  auto mkt_opt = magic_enum::enum_cast<Market>(book_str);
  if (!mkt_opt.has_value()) {
    fmt::print(stderr, "Unknown market: {}\n", book_str);
    return 1;
  }
  Market mkt = *mkt_opt;
  BookId book_id{mkt, SymbolId{sym_str}};

  std::vector<BookId> ids{book_id};
  BookManager bm(ids.begin(), ids.end());
  CountingListener listener;
  bm.subscribe(book_id, &listener);

  zmq::context_t ctx{1};
  zmq::socket_t sub(ctx, zmq::socket_type::sub);
  sub.set(zmq::sockopt::subscribe, "");
  sub.connect(endpoint);
  fmt::print("hl_book_check: connected SUB to {} (book={} sym={})\n", endpoint, book_str, sym_str);

  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);

  int64_t received = 0;
  int64_t applied = 0;
  int64_t parse_errors = 0;
  int64_t market_mismatch = 0;
  int64_t sym_mismatch = 0;
  auto t0 = std::chrono::steady_clock::now();

  zmq::pollitem_t items[] = {{sub, 0, ZMQ_POLLIN, 0}};
  while (!g_stop) {
    int rc;
    try {
      rc = zmq::poll(items, 1, std::chrono::milliseconds{500});
    } catch (const zmq::error_t& e) {
      if (e.num() == EINTR) break;
      throw;
    }
    if (rc <= 0) continue;
    if (!(items[0].revents & ZMQ_POLLIN)) continue;

    zmq::message_t zmsg;
    zmq::recv_result_t recvd;
    try {
      recvd = sub.recv(zmsg, zmq::recv_flags::none);
    } catch (const zmq::error_t& e) {
      if (e.num() == EINTR) break;
      throw;
    }
    if (!recvd) continue;
    ++received;

    mdmsg::PbMessage pb;
    if (!pb.ParseFromArray(zmsg.data(), zmsg.size())) {
      ++parse_errors;
      continue;
    }

    if (pb.market() != static_cast<int32_t>(mdmsg::PbMarket::PBMARKET_HYPERLIQUID)) {
      ++market_mismatch;
      continue;
    }
    if (pb.symbol_id() != sym_str) {
      ++sym_mismatch;
      continue;
    }

    bm.onMD(pb);
    ++applied;

    if (!quiet && print_every > 0 && applied % print_every == 0) {
      auto* lb = bm.find(book_id);
      if (lb != nullptr) {
        fmt::print("\n=== applied={} (recv={}) ===\n", applied, received);
        print_book(*lb, max_levels);
      }
    }
  }

  auto elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  fmt::print("\n=== shutdown ===\n");
  fmt::print("elapsed_s={:.1f} recv={} applied={} parse_errors={} market_mismatch={} sym_mismatch={}\n",
             elapsed, received, applied, parse_errors, market_mismatch, sym_mismatch);
  fmt::print("listener: adds={} modifies={} deletes={} trades={} finals={}\n",
             listener.adds, listener.modifies, listener.deletes, listener.trades, listener.finals);
  auto* lb = bm.find(book_id);
  if (lb != nullptr) {
    fmt::print("\nfinal book:\n");
    print_book(*lb, max_levels);
  }
  return 0;
}
