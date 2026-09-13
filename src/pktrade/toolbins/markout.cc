#include <cmath>
#include <fstream>
#include <iostream>
#include <string>
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

struct TradeRecord {
  std::string raw_line;
  int64_t epoch_ms;
  BookId book_id;
  bool is_buy;
  double exec_price;
};

int main(int argc, char** argv) {
  std::string trades_path;
  std::string date;
  std::string start_t_str = "00:00:00 America/New_York";
  std::string end_t_str = "23:59:00 America/New_York";
  std::string markout_str = "1,10,30,60,120";
  std::string output_path;
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";

  CLI::App cmd_flags("markout");
  cmd_flags.add_option("--trades", trades_path, "Path to input trades CSV")->required();
  cmd_flags.add_option("-d,--date", date, "Date YYYYMMDD")->required();
  cmd_flags.add_option("-m,--markouts", markout_str, "Comma-separated markout seconds");
  cmd_flags.add_option("-o,--output", output_path, "Output path");
  cmd_flags.add_option("--datadir", data_dir, "Data dir");
  cmd_flags.add_option("--start", start_t_str, "Start Time");
  cmd_flags.add_option("--end", end_t_str, "End Time");

  PARSE(cmd_flags, argc, argv);

  // Parse markout offsets
  std::vector<int> offsets;
  for (auto& s : util::split_str(markout_str, ',')) {
    offsets.push_back(std::stoi(s));
  }

  // Default output path: insert _markout before .csv
  if (output_path.empty()) {
    auto pos = trades_path.rfind(".csv");
    if (pos != std::string::npos) {
      output_path = trades_path.substr(0, pos) + "_markout.csv";
    } else {
      output_path = trades_path + "_markout";
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

  // Phase 2: Set up infrastructure
  GlobalVar::date_ = date;
  std::string start_dt = date + " " + start_t_str;
  std::string end_dt = date + " " + end_t_str;

  date::zoned_seconds start_t = time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = time_utils::dtToSecs(end_dt);

  GlobalVar::start_t_ = time_utils::zsToTp(start_t);
  GlobalVar::end_t_ = time_utils::zsToTp(end_t);

  std::vector<BookId> books(unique_books.begin(), unique_books.end());

  EventLoop loop;
  BookManager book_manager(books.begin(), books.end());

  std::vector<std::pair<std::filesystem::path, Market>> streams =
      util::stream_paths(data_dir, books, date, true, true);

  HistoricalFeed feed(&book_manager, streams.begin(), streams.end(),
                      std::stoi(GlobalVar::date_), false, true, true);
  loop.setPoller(std::make_unique<SimPoller>());
  loop.registerPollables(feed);

  // Phase 3: Register markout callbacks
  size_t num_trades = trades.size();
  size_t num_offsets = offsets.size();
  std::vector<std::vector<double>> markout_mids(num_trades,
                                                 std::vector<double>(num_offsets, NAN));

  for (size_t i = 0; i < num_trades; ++i) {
    for (size_t j = 0; j < num_offsets; ++j) {
      int64_t target_ms = trades[i].epoch_ms + static_cast<int64_t>(offsets[j]) * 1000;
      Clock::time_point tp = time_utils::msToTp(target_ms);
      BookId bid = trades[i].book_id;

      loop.onTimeout(tp, [i, j, bid, &markout_mids, &book_manager]() {
        const LevelBook* bk = book_manager.find(bid);
        if (bk && !bk->nonFull()) {
          double bb = bk->getSideTop<BuySide>().px.toDouble();
          double ba = bk->getSideTop<SellSide>().px.toDouble();
          markout_mids[i][j] = (bb + ba) / 2.0;
        }
      });
    }
  }

  std::cout << "Registered " << (num_trades * num_offsets) << " markout callbacks" << std::endl;

  // Phase 4: Run event loop
  loop.run();

  // Phase 5: Write output
  std::ofstream outfile(output_path);
  if (!outfile.is_open()) {
    std::cerr << "Cannot open output file: " << output_path << std::endl;
    return 1;
  }

  // Write header
  outfile << "timestamp,time_ms,order_num,sym,market,side,"
          << "price,size,end_pos,closed_pnl,commission,"
          << "col11,bid,ask,col14,mid,col16,"
          << "col17,col18,col19,add_remove,front_flag";
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
  std::cout << "Markout offsets (seconds):";
  for (int o : offsets) std::cout << " " << o;
  std::cout << std::endl;

  return 0;
}
