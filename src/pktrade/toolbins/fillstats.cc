#include <cmath>
#include <fstream>
#include <iostream>
#include <string>
#include <unordered_map>
#include <vector>

#include "pktrade/book_manager.h"
#include "pktrade/context/global_vars.h"
#include "pktrade/event_loop.h"
#include "pktrade/feed.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

using namespace pktrade;

// ── Trade record (same as markout) ──────────────────────────────────────────

struct TradeRecord {
  std::string raw_line;
  int64_t epoch_ms;
  BookId book_id;
  bool is_buy;
  double exec_price;
  std::string local_sym;  // e.g. "xyz:AAPL"
};

// ── Feature snapshot captured at trade time ─────────────────────────────────

struct FeatureSnapshot {
  double local_mid = NAN;
  double remote_mid = NAN;
  double lmr = NAN;            // local_mid - remote_mid
  double lmr_ema = NAN;
  double local_ret_ema = NAN;
  double remote_ret_ema = NAN;
  double remote_signed_ret_ema_2s = NAN;
  double remote_signed_ret_ema_10s = NAN;
  double remote_signed_ret_ema_60s = NAN;
  double remote_ret_run = NAN;
  double local_spread = NAN;
  double local_spread_ema = NAN;
  double local_notional = NAN;
};

// ── FeatureTracker — BookListener that maintains per-symbol running state ───

class FeatureTracker : public BookListener {
 public:
  FeatureTracker(BookManager* bm) : book_manager_(bm) {}

  // Register a local book to track
  void addLocalBook(const BookId& book_id, const std::string& local_sym) {
    book_to_sym_[book_id] = local_sym;
    is_local_[book_id] = true;
    sym_state_[local_sym] = SymState{};
  }

  // Register a remote book mapping
  void addRemoteBook(const BookId& remote_book_id, const std::string& local_sym) {
    book_to_sym_[remote_book_id] = local_sym;
    is_local_[remote_book_id] = false;
    remote_for_sym_[local_sym] = remote_book_id;
  }

  // Snapshot features for a given local symbol
  FeatureSnapshot snapshot(const std::string& local_sym) {
    FeatureSnapshot fs;
    auto it = sym_state_.find(local_sym);
    if (it == sym_state_.end()) return fs;

    auto& s = it->second;
    fs.local_mid = s.local_mid;
    fs.remote_mid = s.remote_mid;
    fs.lmr_ema = s.lmr_ema;
    fs.local_ret_ema = s.local_ret_ema;
    fs.remote_ret_ema = s.remote_ret_ema;
    fs.remote_signed_ret_ema_2s = s.remote_signed_ret_ema_2s;
    fs.remote_signed_ret_ema_10s = s.remote_signed_ret_ema_10s;
    fs.remote_signed_ret_ema_60s = s.remote_signed_ret_ema_60s;
    fs.remote_ret_run = s.remote_ret_run;
    fs.local_spread_ema = s.local_spread_ema;
    fs.local_notional = s.local_notional;

    if (!std::isnan(s.local_mid) && !std::isnan(s.remote_mid)) {
      fs.lmr = s.local_mid - s.remote_mid;
    }
    if (!std::isnan(s.local_bid) && !std::isnan(s.local_ask)) {
      fs.local_spread = s.local_ask - s.local_bid;
    }

    return fs;
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
    auto sym_it = book_to_sym_.find(bid);
    if (sym_it == book_to_sym_.end()) return;

    auto local_it = is_local_.find(bid);
    if (local_it != is_local_.end() && local_it->second) {
      auto& s = sym_state_[sym_it->second];
      s.local_notional += trd.px.toDouble() * trd.qty.toDouble();
    }
  }

  void onQuote(const LevelBook& bk, const Quote& q) override {
    markDirty(bk);
    const BookId& bid = bk.getBookID();
    auto sym_it = book_to_sym_.find(bid);
    if (sym_it == book_to_sym_.end()) return;

    auto local_it = is_local_.find(bid);
    bool is_local = (local_it != is_local_.end() && local_it->second);
    if (is_local) return;

    double bb = q.bb.toDouble();
    double ba = q.ba.toDouble();
    double mid = 0;
    if (bb > 0 && ba > 0 && ba < bb * 2) {
      mid = (bb + ba) / 2.0;
    } else if (q.mid > 0 && q.mid < 1e9) {
      mid = q.mid;
    }
    if (mid > 0 && mid < 1e9) {
      auto& s = sym_state_[sym_it->second];
      s.remote_mid = mid;
      updateEMAs(s, time_utils::nowToMs());
    }
  }

  void onFinal(const LevelBook& bk) override {
    const BookId& bid = bk.getBookID();
    auto sym_it = book_to_sym_.find(bid);
    if (sym_it == book_to_sym_.end()) return;

    const std::string& local_sym = sym_it->second;
    auto& s = sym_state_[local_sym];

    auto local_it = is_local_.find(bid);
    bool is_local = (local_it != is_local_.end() && local_it->second);

    int64_t now_ms = time_utils::nowToMs();

    if (is_local) {
      // Update local mid/spread from the book
      if (!bk.nonFull()) {
        double bb = bk.getSideTop<BuySide>().px.toDouble();
        double ba = bk.getSideTop<SellSide>().px.toDouble();
        s.local_bid = bb;
        s.local_ask = ba;
        s.local_mid = (bb + ba) / 2.0;
      }
    } else {
      // Remote book — update remote mid
      if (!bk.nonFull()) {
        double bb = bk.getSideTop<BuySide>().px.toDouble();
        double ba = bk.getSideTop<SellSide>().px.toDouble();
        s.remote_mid = (bb + ba) / 2.0;
      }
    }

    updateEMAs(s, now_ms);
  }

 private:
  struct SymState {
    double local_mid = NAN;
    double remote_mid = NAN;
    double local_bid = NAN;
    double local_ask = NAN;

    // EMAs (10s time constant)
    double lmr_ema = 0;
    double local_ret_ema = 0;
    double remote_ret_ema = 0;
    double remote_signed_ret_ema_2s = 0;
    double remote_signed_ret_ema_10s = 0;
    double remote_signed_ret_ema_60s = 0;
    double remote_ret_run = 0;
    double local_spread_ema = 0;

    // Decayed accumulator
    double local_notional = 0;

    int64_t last_update_ms = 0;
    double prev_local_mid = NAN;
    double prev_remote_mid = NAN;

    bool ema_initialized = false;
  };

  void markDirty(const LevelBook&) {
    // Nothing needed — all work done in onFinal
  }

  void updateEMAs(SymState& s, int64_t now_ms) {
    if (s.last_update_ms == 0) {
      s.last_update_ms = now_ms;
      s.prev_local_mid = s.local_mid;
      s.prev_remote_mid = s.remote_mid;
      // Initialize EMAs from current values
      if (!std::isnan(s.local_bid) && !std::isnan(s.local_ask)) {
        s.local_spread_ema = s.local_ask - s.local_bid;
      }
      if (!std::isnan(s.local_mid) && !std::isnan(s.remote_mid)) {
        s.lmr_ema = s.local_mid - s.remote_mid;
      }
      s.ema_initialized = true;
      return;
    }

    double dt = (now_ms - s.last_update_ms) / 1000.0;
    if (dt <= 0) {
      // Still update prev values
      s.prev_local_mid = s.local_mid;
      s.prev_remote_mid = s.remote_mid;
      return;
    }

    double decay = std::exp(-dt / 10.0);
    double alpha = 1.0 - decay;

    // Decay notional accumulator
    s.local_notional *= decay;

    // Update spread EMA
    if (!std::isnan(s.local_bid) && !std::isnan(s.local_ask)) {
      double spread = s.local_ask - s.local_bid;
      s.local_spread_ema = s.local_spread_ema * (1.0 - alpha) + spread * alpha;
    }

    // Update LMR EMA
    if (!std::isnan(s.local_mid) && !std::isnan(s.remote_mid)) {
      double lmr = s.local_mid - s.remote_mid;
      s.lmr_ema = s.lmr_ema * (1.0 - alpha) + lmr * alpha;
    }

    // Update local return EMA
    if (!std::isnan(s.local_mid) && !std::isnan(s.prev_local_mid) &&
        s.prev_local_mid != 0) {
      double local_ret = std::abs(s.local_mid - s.prev_local_mid) / s.prev_local_mid;
      s.local_ret_ema = s.local_ret_ema * (1.0 - alpha) + local_ret * alpha;
    }

    // Update remote return EMA
    if (!std::isnan(s.remote_mid) && !std::isnan(s.prev_remote_mid) &&
        s.prev_remote_mid != 0) {
      double remote_signed_ret = (s.remote_mid - s.prev_remote_mid) / s.prev_remote_mid;
      double remote_ret = std::abs(remote_signed_ret);
      s.remote_ret_ema = s.remote_ret_ema * (1.0 - alpha) + remote_ret * alpha;

      double alpha_2s = 1.0 - std::exp(-dt / 2.0);
      double alpha_60s = 1.0 - std::exp(-dt / 60.0);
      s.remote_signed_ret_ema_2s =
          s.remote_signed_ret_ema_2s * (1.0 - alpha_2s) + remote_signed_ret * alpha_2s;
      s.remote_signed_ret_ema_10s =
          s.remote_signed_ret_ema_10s * (1.0 - alpha) + remote_signed_ret * alpha;
      s.remote_signed_ret_ema_60s =
          s.remote_signed_ret_ema_60s * (1.0 - alpha_60s) + remote_signed_ret * alpha_60s;

      if ((s.remote_ret_run > 0 && remote_signed_ret < 0) ||
          (s.remote_ret_run < 0 && remote_signed_ret > 0)) {
        s.remote_ret_run = remote_signed_ret;
      } else {
        s.remote_ret_run += remote_signed_ret;
      }
    }

    s.last_update_ms = now_ms;
    s.prev_local_mid = s.local_mid;
    s.prev_remote_mid = s.remote_mid;
  }

  std::unordered_map<BookId, std::string> book_to_sym_;
  std::unordered_map<BookId, bool> is_local_;
  std::unordered_map<std::string, BookId> remote_for_sym_;
  std::unordered_map<std::string, SymState> sym_state_;
  BookManager* book_manager_;
};

// ── Parse remote map string ─────────────────────────────────────────────────

struct RemoteMapping {
  std::string local_sym;     // e.g. "xyz:AAPL"
  std::string remote_sym;    // e.g. "AAPL"
  std::string remote_market; // e.g. "TopBookEquity"
};

std::vector<RemoteMapping> parseRemoteMap(const std::string& map_str) {
  std::vector<RemoteMapping> result;
  if (map_str.empty()) return result;

  for (auto& entry : util::split_str(map_str, ',')) {
    // Format: "xyz:AAPL=AAPL@TopBookEquity"
    auto eq_pos = entry.find('=');
    if (eq_pos == std::string::npos) continue;

    std::string local_sym = entry.substr(0, eq_pos);
    std::string rest = entry.substr(eq_pos + 1);

    auto at_pos = rest.find('@');
    if (at_pos == std::string::npos) continue;

    RemoteMapping rm;
    rm.local_sym = local_sym;
    rm.remote_sym = rest.substr(0, at_pos);
    rm.remote_market = rest.substr(at_pos + 1);
    result.push_back(std::move(rm));
  }
  return result;
}

// ── Main ────────────────────────────────────────────────────────────────────

int main(int argc, char** argv) {
  std::string trades_path;
  std::string date;
  std::string start_t_str = "00:00:00 America/New_York";
  std::string end_t_str = "23:59:00 America/New_York";
  std::string markout_str = "1,10,30,60,120";
  std::string remote_map_str;
  std::string output_path;
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";
  std::string equity_variant;

  CLI::App cmd_flags("fillstats");
  cmd_flags.add_option("--trades", trades_path, "Path to input trades CSV")->required();
  cmd_flags.add_option("-d,--date", date, "Date YYYYMMDD")->required();
  cmd_flags.add_option("-m,--markouts", markout_str, "Comma-separated markout seconds");
  cmd_flags.add_option("-o,--output", output_path, "Output path");
  cmd_flags.add_option("--datadir", data_dir, "Data dir");
  cmd_flags.add_option("--equity-variant", equity_variant,
                       "Suffix for TopBookEquity dir (e.g. 'Boats' loads from TopBookEquity_Boats/)");
  cmd_flags.add_option("--start", start_t_str, "Start Time");
  cmd_flags.add_option("--end", end_t_str, "End Time");
  cmd_flags.add_option("--remote-map", remote_map_str,
                        "Per-symbol remote mapping: local=remote@market,...");

  PARSE(cmd_flags, argc, argv);

  // Parse markout offsets
  std::vector<int> offsets;
  for (auto& s : util::split_str(markout_str, ',')) {
    offsets.push_back(std::stoi(s));
  }

  // Default output path
  if (output_path.empty()) {
    auto pos = trades_path.rfind(".csv");
    if (pos != std::string::npos) {
      output_path = trades_path.substr(0, pos) + "_fillstats.csv";
    } else {
      output_path = trades_path + "_fillstats";
    }
  }

  // Phase 1: Parse trades
  std::vector<TradeRecord> trades;
  std::set<BookId> unique_books;

  std::ifstream infile(trades_path);
  if (!infile.is_open()) {
    std::cerr << "Cannot open trades file: " << trades_path << std::endl;
    return 1;
  }

  std::string line;
  while (std::getline(infile, line)) {
    if (line.empty()) continue;

    auto parts = util::split_str(line, ',');
    if (parts.size() < 7) {
      std::cerr << "Skipping malformed line (< 7 fields): " << line << std::endl;
      continue;
    }

    TradeRecord tr;
    tr.raw_line = line;
    tr.epoch_ms = std::stoll(parts[1]);

    std::string sym_str = parts[3];
    std::string market_str = parts[4];
    std::string side_str = parts[5];

    auto mkt_opt = magic_enum::enum_cast<Market>(market_str);
    if (!mkt_opt.has_value()) {
      std::cerr << "Unknown market: " << market_str << ", skipping line" << std::endl;
      continue;
    }

    tr.book_id = BookId{mkt_opt.value(), SymbolId{sym_str}};
    tr.is_buy = (side_str == "Buy");
    tr.exec_price = std::stod(parts[6]);
    tr.local_sym = sym_str;

    unique_books.insert(tr.book_id);
    trades.push_back(std::move(tr));
  }
  infile.close();

  std::cout << "Parsed " << trades.size() << " trades across " << unique_books.size()
            << " books" << std::endl;

  if (trades.empty()) {
    std::cerr << "No trades to process" << std::endl;
    return 1;
  }

  // Phase 2: Parse remote map and build remote BookIds
  auto remote_mappings = parseRemoteMap(remote_map_str);
  std::set<BookId> remote_books;
  // Map from local_sym to remote BookId
  std::unordered_map<std::string, BookId> sym_to_remote;

  for (auto& rm : remote_mappings) {
    auto mkt_opt = magic_enum::enum_cast<Market>(rm.remote_market);
    if (!mkt_opt.has_value()) {
      std::cerr << "Unknown remote market: " << rm.remote_market << std::endl;
      continue;
    }
    BookId remote_bid{mkt_opt.value(), SymbolId{rm.remote_sym}};
    remote_books.insert(remote_bid);
    sym_to_remote[rm.local_sym] = remote_bid;
  }

  // Phase 3: Set up infrastructure
  GlobalVar::date_ = date;
  std::string start_dt = date + " " + start_t_str;
  std::string end_dt = date + " " + end_t_str;

  date::zoned_seconds start_t = time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = time_utils::dtToSecs(end_dt);

  GlobalVar::start_t_ = time_utils::zsToTp(start_t);
  GlobalVar::end_t_ = time_utils::zsToTp(end_t);

  // Combine all books (local + remote)
  std::vector<BookId> all_books(unique_books.begin(), unique_books.end());
  for (auto& rb : remote_books) {
    if (unique_books.find(rb) == unique_books.end()) {
      all_books.push_back(rb);
    }
  }

  EventLoop loop;
  BookManager book_manager(all_books.begin(), all_books.end());

  std::vector<std::pair<std::filesystem::path, Market>> streams =
      util::stream_paths(data_dir, all_books, date, true, true, equity_variant);

  HistoricalFeed feed(&book_manager, streams.begin(), streams.end(),
                      std::stoi(GlobalVar::date_), false, true, true);
  loop.setPoller(std::make_unique<SimPoller>());
  loop.registerPollables(feed);

  // Phase 4: Create FeatureTracker, subscribe to all books
  FeatureTracker tracker(&book_manager);

  // Register local books
  for (auto& bid : unique_books) {
    std::string sym = bid.second.get();
    tracker.addLocalBook(bid, sym);
    book_manager.subscribe(bid, &tracker);
  }

  // Register remote books
  for (auto& rm : remote_mappings) {
    auto mkt_opt = magic_enum::enum_cast<Market>(rm.remote_market);
    if (!mkt_opt.has_value()) continue;
    BookId remote_bid{mkt_opt.value(), SymbolId{rm.remote_sym}};
    tracker.addRemoteBook(remote_bid, rm.local_sym);
    book_manager.subscribe(remote_bid, &tracker);
  }

  // Phase 5: Register callbacks
  size_t num_trades = trades.size();
  size_t num_offsets = offsets.size();

  // Feature snapshots (one per trade)
  std::vector<FeatureSnapshot> features(num_trades);

  // Markout mids (same as markout binary)
  std::vector<std::vector<double>> markout_mids(num_trades,
                                                 std::vector<double>(num_offsets, NAN));

  // Register feature snapshot callbacks at each trade time
  for (size_t i = 0; i < num_trades; ++i) {
    Clock::time_point tp = time_utils::msToTp(trades[i].epoch_ms);
    std::string sym = trades[i].local_sym;

    loop.onTimeout(tp, [i, sym, &features, &tracker]() {
      features[i] = tracker.snapshot(sym);
    });

    // Register markout callbacks
    for (size_t j = 0; j < num_offsets; ++j) {
      int64_t target_ms = trades[i].epoch_ms + static_cast<int64_t>(offsets[j]) * 1000;
      Clock::time_point mo_tp = time_utils::msToTp(target_ms);
      BookId bid = trades[i].book_id;

      loop.onTimeout(mo_tp, [i, j, bid, &markout_mids, &book_manager]() {
        const LevelBook* bk = book_manager.find(bid);
        if (bk && !bk->nonFull()) {
          double bb = bk->getSideTop<BuySide>().px.toDouble();
          double ba = bk->getSideTop<SellSide>().px.toDouble();
          markout_mids[i][j] = (bb + ba) / 2.0;
        }
      });
    }
  }

  std::cout << "Registered " << num_trades << " feature + "
            << (num_trades * num_offsets) << " markout callbacks" << std::endl;

  // Phase 6: Run event loop
  loop.run();

  // Phase 7: Write output
  std::ofstream outfile(output_path);
  if (!outfile.is_open()) {
    std::cerr << "Cannot open output file: " << output_path << std::endl;
    return 1;
  }

  // Write header
  outfile << "timestamp,time_ms,order_num,sym,market,side,"
          << "price,size,end_pos,closed_pnl,commission,"
          << "col11,bid,ask,col14,mid,col16,"
          << "col17,col18,col19,add_remove,front_flag,"
          << "lmr,lmr_ema,local_ret_ema,remote_ret_ema,"
          << "remote_signed_ret_ema_2s,remote_signed_ret_ema_10s,"
          << "remote_signed_ret_ema_60s,remote_ret_run,"
          << "local_spread,local_spread_ema,local_notional";
  for (int o : offsets) {
    outfile << ",mo_" << o << "s";
  }
  outfile << "\n";

  for (size_t i = 0; i < num_trades; ++i) {
    // Strip trailing whitespace from raw line
    std::string& raw = trades[i].raw_line;
    while (!raw.empty() && (raw.back() == '\n' || raw.back() == '\r')) {
      raw.pop_back();
    }

    outfile << raw;

    // Feature columns
    auto& fs = features[i];
    auto writeCol = [&](double v) {
      if (!std::isnan(v)) {
        outfile << "," << v;
      } else {
        outfile << ",";
      }
    };

    writeCol(fs.lmr);
    writeCol(fs.lmr_ema);
    writeCol(fs.local_ret_ema);
    writeCol(fs.remote_ret_ema);
    writeCol(fs.remote_signed_ret_ema_2s);
    writeCol(fs.remote_signed_ret_ema_10s);
    writeCol(fs.remote_signed_ret_ema_60s);
    writeCol(fs.remote_ret_run);
    writeCol(fs.local_spread);
    writeCol(fs.local_spread_ema);
    writeCol(fs.local_notional);

    // Markout columns
    for (size_t j = 0; j < num_offsets; ++j) {
      if (!std::isnan(markout_mids[i][j])) {
        double mo = trades[i].is_buy
                        ? (markout_mids[i][j] - trades[i].exec_price)
                        : (trades[i].exec_price - markout_mids[i][j]);
        outfile << "," << mo;
      } else {
        outfile << ",";
      }
    }
    outfile << "\n";
  }
  outfile.close();

  std::cout << "Wrote " << num_trades << " lines to " << output_path << std::endl;
  std::cout << "Feature columns: lmr, lmr_ema, local_ret_ema, remote_ret_ema, "
            << "local_spread, local_spread_ema, local_notional" << std::endl;
  std::cout << "Markout offsets (seconds):";
  for (int o : offsets) std::cout << " " << o;
  std::cout << std::endl;

  return 0;
}
