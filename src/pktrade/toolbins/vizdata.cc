// vizdata: Historical market data CSV dumper for the visualizer.
//
// Replays gzpbf historical data for a local (Hyperliquid) and remote
// (TopBookEquity / TopBookCme) symbol pair, computing prices, EMS features,
// and book depth metrics.  Outputs a CSV suitable for the Dash visualizer.
//
// Example:
//   ./vizdata --book Hyperliquid --symbol xyz:META \
//             --remote-book TopBookEquity --remote-symbol META \
//             --date 20260220 --out /tmp/vizdata_meta.csv --interval-ms 100

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"

#include <algorithm>
#include <cmath>
#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <fstream>
#include <glog/logging.h>
#include <magic_enum.hpp>
#include <stdio.h>

using namespace pktrade;

// ── VizDataDumper ────────────────────────────────────────────────────────────

class VizDataDumper : public BookListener {
 public:
  VizDataDumper(BookId local_bid, BookId remote_bid, BookManager* bm,
                const std::string& out_path, int64_t interval_ms,
                const std::string& trades_out_path = "")
      : local_bid_(local_bid),
        remote_bid_(remote_bid),
        bm_(bm),
        interval_ms_(interval_ms),
        trades_enabled_(!trades_out_path.empty()) {
    out_.open(out_path);
    if (!out_.is_open()) {
      throw std::runtime_error("Cannot open output file: " + out_path);
    }
    writeHeader();

    if (trades_enabled_) {
      trades_out_.open(trades_out_path);
      if (!trades_out_.is_open()) {
        throw std::runtime_error("Cannot open trades file: " + trades_out_path);
      }
      trades_out_ << "time_ms,price,qty,side\n";
    }
  }

  ~VizDataDumper() {
    if (out_.is_open()) out_.close();
    if (trades_out_.is_open()) trades_out_.close();
  }

  void subscribe() {
    bm_->subscribe(local_bid_, this);
    bm_->subscribe(remote_bid_, this);
  }

  // BookListener interface
  void onLevelUpdate(const LevelBook& bk, const LevelAdd&) override {
    markDirty(bk);
  }
  void onLevelUpdate(const LevelBook& bk, const LevelModify&) override {
    markDirty(bk);
  }
  void onLevelUpdate(const LevelBook& bk, const LevelDelete&) override {
    markDirty(bk);
  }

  void onLevelUpdate(const LevelBook& bk, const Trade& trd) override {
    markDirty(bk);
    const BookId& bid = bk.getBookID();
    if (bid != local_bid_) return;

    // Trade microstructure EMS updates.
    int64_t now_t = time_utils::nowToMs();
    double trade_qty = trd.qty.toDouble();
    double trade_px = trd.px.toDouble();
    double trade_notional = trade_px * trade_qty;
    double sign = (trd.passive_side == Side::Buy) ? -1.0 : 1.0;

    double decay = 0;
    if (last_trade_t_ > 0) {
      double dt = now_t - last_trade_t_;
      decay = std::exp(-dt / trade_micro_tdc_ms_);
    }

    trade_rate_ems_ = 1.0 + decay * trade_rate_ems_;
    trade_notional_signed_ems_ = sign * trade_notional + decay * trade_notional_signed_ems_;

    last_trade_t_ = now_t;

    if (trades_enabled_) {
      const char* side = (trd.passive_side == Side::Sell) ? "buy" : "sell";
      trades_out_ << fmt::format("{},{:.6f},{:.6f},{}\n",
                                  now_t, trade_px, trade_qty, side);
      trade_count_++;
    }
  }

  void onQuote(const LevelBook& bk, const Quote& q) override {
    const BookId& bid = bk.getBookID();
    if (bid == remote_bid_) {
      // TopBookEquity/TopBookCme deliver price via Quote, not book levels.
      // Use bb/ba (more reliable than mid which may be 0).
      double bb = q.bb.toDouble();
      double ba = q.ba.toDouble();
      double mid = 0;
      if (bb > 0 && ba > 0 && ba < bb * 2) {
        mid = (bb + ba) / 2.0;
      } else if (q.mid > 0 && q.mid < 1e9) {
        mid = q.mid;
      }
      if (mid > 0 && mid < 1e9) {
        pending_remote_mid_ = mid;
      }
    }
  }

  void onFinal(const LevelBook& bk) override {
    const BookId& bid = bk.getBookID();
    int64_t now_ms = time_utils::nowToMs();

    if (bid == local_bid_) {
      updateLocalState(bk, now_ms);
      local_dirty_ = false;
    } else if (bid == remote_bid_) {
      updateRemoteState(bk, now_ms);
      remote_dirty_ = false;
    }

    // Only emit rows on local book updates, subject to interval.
    if (bid == local_bid_ && local_mid_ > 0) {
      if (interval_ms_ <= 0 || now_ms - last_emit_t_ >= interval_ms_) {
        emitRow(now_ms);
        last_emit_t_ = now_ms;
      }
    }
  }

  int64_t rowCount() const { return row_count_; }
  int64_t tradeCount() const { return trade_count_; }

 private:
  void markDirty(const LevelBook& bk) {
    const BookId& bid = bk.getBookID();
    if (bid == local_bid_) local_dirty_ = true;
    else if (bid == remote_bid_) remote_dirty_ = true;
  }

  void updateLocalState(const LevelBook& bk, int64_t now_ms) {
    if (bk.nonFull()) return;

    double bb = bk.getSideTop<BuySide>().px.toDouble();
    double ba = bk.getSideTop<SellSide>().px.toDouble();
    if (bb <= 0 || ba <= 0 || ba < bb) return;

    local_bb_ = bb;
    local_ba_ = ba;
    local_mid_ = (bb + ba) / 2.0;
    local_spread_ = ba - bb;

    // Top-of-book sizes.
    local_bb_size_ = bk.getSideTop<BuySide>().qty.toDouble();
    local_ba_size_ = bk.getSideTop<SellSide>().qty.toDouble();

    // Liquidity depth.
    liq_depth_buy_100k_ = liqDepth<BuySide>(bk, local_mid_, 100000);
    liq_depth_buy_250k_ = liqDepth<BuySide>(bk, local_mid_, 250000);
    liq_depth_sell_100k_ = liqDepth<SellSide>(bk, local_mid_, 100000);
    liq_depth_sell_250k_ = liqDepth<SellSide>(bk, local_mid_, 250000);

    // Deep book notional within range of mid.
    computeDeepNotional(bk);
  }

  void updateRemoteState(const LevelBook& bk, int64_t now_ms) {
    // For TopBook markets, mid comes from onQuote (stored in pending_remote_mid_).
    // For full-book markets, read from the book levels.
    double mid = pending_remote_mid_;
    if (mid <= 0 && !bk.nonFull()) {
      double bb = bk.getSideTop<BuySide>().px.toDouble();
      double ba = bk.getSideTop<SellSide>().px.toDouble();
      mid = (bb + ba) / 2.0;
    }
    pending_remote_mid_ = 0;
    if (mid <= 0) return;

    // Vol and momentum EMS from remote returns.
    if (prev_remote_mid_ > 0 && last_remote_t_ > 0) {
      double ret = (mid - prev_remote_mid_) / prev_remote_mid_;
      double dt = now_ms - last_remote_t_;
      double abs_ret = std::abs(ret);

      vol_ems_remote_4s_ = abs_ret + std::exp(-dt / 4000.0) * vol_ems_remote_4s_;
      vol_ems_remote_30s_ = abs_ret + std::exp(-dt / 30000.0) * vol_ems_remote_30s_;

      momentum_1s_ = ret + std::exp(-dt / 1000.0) * momentum_1s_;
      momentum_4s_ = ret + std::exp(-dt / 4000.0) * momentum_4s_;
    }

    prev_remote_mid_ = remote_mid_;
    remote_mid_ = mid;
    last_remote_t_ = now_ms;
  }

  template <typename SideType>
  double liqDepth(const LevelBook& bk, double mid, double target_notional) {
    if (mid <= 0 || bk.side<SideType>().empty()) return 0;
    double best_px = bk.side<SideType>().begin()->px.toDouble();
    double accum_notional = 0;
    double last_px = best_px;
    for (const auto& lvl : bk.side<SideType>()) {
      double px = lvl.px.toDouble();
      double qty = lvl.qty.toDouble();
      accum_notional += px * qty;
      last_px = px;
      if (accum_notional >= target_notional) break;
    }
    return std::abs(best_px - last_px) / mid;
  }

  void computeDeepNotional(const LevelBook& bk) {
    double range = deep_book_range_frac_ * local_mid_;
    deep_bid_notional_ = 0;
    deep_ask_notional_ = 0;

    if (!bk.side<BuySide>().empty()) {
      double best_bid = bk.side<BuySide>().begin()->px.toDouble();
      for (const auto& lvl : bk.side<BuySide>()) {
        if (best_bid - lvl.px.toDouble() > range) break;
        deep_bid_notional_ += lvl.px.toDouble() * lvl.qty.toDouble();
      }
    }
    if (!bk.side<SellSide>().empty()) {
      double best_ask = bk.side<SellSide>().begin()->px.toDouble();
      for (const auto& lvl : bk.side<SellSide>()) {
        if (lvl.px.toDouble() - best_ask > range) break;
        deep_ask_notional_ += lvl.px.toDouble() * lvl.qty.toDouble();
      }
    }
  }

  void writeHeader() {
    out_ << "time_ms,local_bid,local_ask,local_mid,remote_mid,local_spread,"
         << "premium_bps,vol_ems_remote_4s,vol_ems_remote_30s,"
         << "momentum_1s,momentum_4s,"
         << "trade_rate_ems,trade_notional_signed_ems,"
         << "liq_depth_buy_100k,liq_depth_buy_250k,"
         << "liq_depth_sell_100k,liq_depth_sell_250k,"
         << "deep_bid_notional,deep_ask_notional,"
         << "local_bb_size,local_ba_size\n";
  }

  void emitRow(int64_t now_ms) {
    double premium_bps = 0;
    if (remote_mid_ > 0 && local_mid_ > 0) {
      premium_bps = (local_mid_ - remote_mid_) / remote_mid_ * 10000.0;
    }

    // Decay trade EMS to current time.
    double trd_rate = trade_rate_ems_;
    double trd_notional_signed = trade_notional_signed_ems_;
    if (last_trade_t_ > 0) {
      double dt = now_ms - last_trade_t_;
      double decay = std::exp(-dt / trade_micro_tdc_ms_);
      trd_rate *= decay;
      trd_notional_signed *= decay;
    }

    out_ << fmt::format(
        "{},{:.6f},{:.6f},{:.6f},{:.6f},{:.6f},"
        "{:.4f},{:.10f},{:.10f},"
        "{:.10f},{:.10f},"
        "{:.4f},{:.2f},"
        "{:.8f},{:.8f},"
        "{:.8f},{:.8f},"
        "{:.2f},{:.2f},"
        "{:.6f},{:.6f}\n",
        now_ms, local_bb_, local_ba_, local_mid_, remote_mid_, local_spread_,
        premium_bps, vol_ems_remote_4s_, vol_ems_remote_30s_,
        momentum_1s_, momentum_4s_,
        trd_rate, trd_notional_signed,
        liq_depth_buy_100k_, liq_depth_buy_250k_,
        liq_depth_sell_100k_, liq_depth_sell_250k_,
        deep_bid_notional_, deep_ask_notional_,
        local_bb_size_, local_ba_size_);

    row_count_++;
  }

  // Config.
  BookId local_bid_;
  BookId remote_bid_;
  BookManager* bm_;
  int64_t interval_ms_;
  std::ofstream out_;
  int64_t row_count_ = 0;

  // Trades output.
  std::ofstream trades_out_;
  bool trades_enabled_ = false;
  int64_t trade_count_ = 0;

  // State flags.
  bool local_dirty_ = false;
  bool remote_dirty_ = false;
  int64_t last_emit_t_ = 0;

  // Local book state.
  double local_bb_ = 0;
  double local_ba_ = 0;
  double local_mid_ = 0;
  double local_spread_ = 0;
  double local_bb_size_ = 0;
  double local_ba_size_ = 0;

  // Remote state.
  double remote_mid_ = 0;
  double prev_remote_mid_ = 0;
  double pending_remote_mid_ = 0;  // Set by onQuote, consumed by updateRemoteState.
  int64_t last_remote_t_ = 0;

  // Vol EMS (absolute return).
  double vol_ems_remote_4s_ = 0;
  double vol_ems_remote_30s_ = 0;

  // Momentum EMS (signed return).
  double momentum_1s_ = 0;
  double momentum_4s_ = 0;

  // Trade microstructure EMS.
  static constexpr double trade_micro_tdc_ms_ = 10000.0;
  double trade_rate_ems_ = 0;
  double trade_notional_signed_ems_ = 0;
  int64_t last_trade_t_ = 0;

  // Book depth.
  double liq_depth_buy_100k_ = 0;
  double liq_depth_buy_250k_ = 0;
  double liq_depth_sell_100k_ = 0;
  double liq_depth_sell_250k_ = 0;

  // Deep book notional.
  static constexpr double deep_book_range_frac_ = 0.005;  // 50 bps
  double deep_bid_notional_ = 0;
  double deep_ask_notional_ = 0;
};

// ── Main ─────────────────────────────────────────────────────────────────────

int main(int argc, char** argv) {
  google::InitGoogleLogging(argv[0]);

  std::string date;
  std::string book;
  std::string sym;
  std::string remote_book;
  std::string remote_sym;
  std::string out_path;
  std::string trades_out_path;
  std::string start_t_str = "18:00:00 America/New_York";
  std::string end_t_str = "17:59:55 America/New_York";
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";
  std::string equity_variant;
  int interval_ms = 100;

  CLI::App cmd_flags("vizdata — historical market data CSV dumper");
  cmd_flags.add_option("-b,--book", book, "Local market (e.g. Hyperliquid)")->required();
  cmd_flags.add_option("--symbol", sym, "Local symbol (e.g. xyz:META)")->required();
  cmd_flags.add_option("--remote-book", remote_book, "Remote market (e.g. TopBookEquity)")->required();
  cmd_flags.add_option("--remote-symbol", remote_sym, "Remote symbol (e.g. META)")->required();
  cmd_flags.add_option("-d,--date", date, "Date YYYYMMDD")->required();
  cmd_flags.add_option("-o,--out", out_path, "Output CSV path")->required();
  cmd_flags.add_option("--datadir", data_dir, "Data directory");
  cmd_flags.add_option("--equity-variant", equity_variant,
                       "Suffix for TopBookEquity dir (e.g. 'Boats' loads from TopBookEquity_Boats/)");
  cmd_flags.add_option("--interval-ms", interval_ms, "Min ms between CSV rows (0 = every update)");
  cmd_flags.add_option("--trades-out", trades_out_path, "Per-trade CSV output path");
  cmd_flags.add_option("--start-time", start_t_str, "Start time");
  cmd_flags.add_option("--end-time", end_t_str, "End time");

  PARSE(cmd_flags, argc, argv);

  Market local_mkt = magic_enum::enum_cast<Market>(book).value();
  Market remote_mkt = magic_enum::enum_cast<Market>(remote_book).value();

  GlobalVar::date_ = date;

  // Time setup — same pattern as symstats.
  std::string start_date = date;
  std::string end_date = date;
  std::string start_dt = start_date + " " + start_t_str;
  std::string end_dt = end_date + " " + end_t_str;

  date::zoned_seconds start_t = time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = time_utils::dtToSecs(end_dt);

  // Handle overnight sessions (start >= 18:00 ET).
  std::string tz_name = std::string(start_t.get_time_zone()->name());
  if (tz_name == "America/New_York") {
    auto start_local = start_t.get_local_time();
    auto start_dp = date::floor<date::days>(start_local);
    auto start_tod = date::make_time(start_local - start_dp);
    int start_hour = start_tod.hours().count();

    if (start_hour >= 18) {
      int year = std::stoi(date.substr(0, 4));
      int month = std::stoi(date.substr(4, 2));
      int day = std::stoi(date.substr(6, 2));
      date::year_month_day ymd = date::year{year}/date::month{unsigned(month)}/date::day{unsigned(day)};
      date::sys_days prev_day = date::sys_days{ymd} - date::days{1};
      date::year_month_day prev_ymd = date::year_month_day{prev_day};
      start_date = fmt::format("{:04d}{:02d}{:02d}",
                               int(prev_ymd.year()),
                               unsigned(prev_ymd.month()),
                               unsigned(prev_ymd.day()));
      start_dt = start_date + " " + start_t_str;
      start_t = time_utils::dtToSecs(start_dt);
    }
  }

  GlobalVar::start_t_ = time_utils::zsToTp(start_t);
  GlobalVar::end_t_ = time_utils::zsToTp(end_t);

  if (GlobalVar::start_t_ > GlobalVar::end_t_) {
    GlobalVar::start_t_ -= std::chrono::hours(24);
  }

  // Book setup — both local and remote.
  BookId local_bid{local_mkt, SymbolId{sym}};
  BookId remote_bid{remote_mkt, SymbolId{remote_sym}};

  std::vector<BookId> all_books = {local_bid, remote_bid};

  GlobalVar::event_loop_ = new EventLoop();
  GlobalVar::book_manager_ = new BookManager(all_books.begin(), all_books.end());

  // Load historical data streams.
  std::vector<std::pair<std::filesystem::path, Market>> streams =
      util::stream_paths(data_dir, all_books, start_date, true, true, equity_variant);

  HistoricalFeed feed(GlobalVar::book_manager_, streams.begin(), streams.end(),
                      std::stoi(GlobalVar::date_));
  GlobalVar::event_loop_->setPoller(std::make_unique<SimPoller>());
  GlobalVar::event_loop_->registerPollables(feed);

  // Create and subscribe the dumper.
  VizDataDumper dumper(local_bid, remote_bid, GlobalVar::book_manager_, out_path, interval_ms,
                       trades_out_path);
  dumper.subscribe();

  fmt::print("vizdata: {} + {} @ {}, interval={}ms, out={}\n",
             sym, remote_sym, date, interval_ms, out_path);

  GlobalVar::event_loop_->runUntil(GlobalVar::end_t_);

  fmt::print("vizdata: done, {} rows written to {}\n", dumper.rowCount(), out_path);
  if (!trades_out_path.empty()) {
    fmt::print("vizdata: {} trades written to {}\n", dumper.tradeCount(), trades_out_path);
  }

  return 0;
}
