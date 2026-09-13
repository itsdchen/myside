#include "pktrade/context/global_vars.h"

#include "pktrade/util/basiclib.h"

#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"

#include "pktrade/mktdata/md_beacon.h"

/*
Calculates one-minute returns.
Only allowed for hyperliquid rn.

Sample line:
./bin/returner --symbol BTC --date 20250420 --out my_out_file.csv

*/

using namespace pktrade;

class Returner {
public:
  Returner(SymbolId sym, Market mkt, std::string out_flie);

  void subscribeData();

  void snapshotInside();
  void finalize();

protected:
  SymbolId sym_;
  // This should be
  Market mkt_;

  std::string out_file_;

  std::vector<std::string> times_str_;
  std::vector<int64_t> times_s_;
  std::vector<double> pxs_;
  std::vector<double> returns_;

  std::chrono::seconds s_between_sample_;
};

Returner::Returner(SymbolId sym, Market mkt, std::string out_file)
    : sym_(sym), mkt_(mkt), out_file_(out_file) {
  // Probably nothing to do for now.

  // Add a callback to start sampling the inside too.
  // Sample it like, every 2 minutes, idk.
  s_between_sample_ = std::chrono::seconds(60 * 1);

  pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_ +
                                                 s_between_sample_,
                                             [&] { this->snapshotInside(); });
}

void Returner::subscribeData() {}

void Returner::finalize() {
  // Figure out median inside liq and trade sizes.

  // Go through and calculate returns.
  double last_mid;
  for (int i = 0; i < pxs_.size(); i++) {

    if (i == 0) {
      last_mid = pxs_[i];
      continue;
    } else {
      // Calculate the return.
      double px_return = (pxs_[i] - last_mid) / last_mid;
      returns_.push_back(px_return);
      last_mid = pxs_[i];
    }
  }

  if (out_file_ == "") {
    // print stdout.
    // Go through and print the returns.
    for (uint i = 1; i < pxs_.size(); i++) {
      std::string cur_line = fmt::format("{},{},{},{}", times_str_[i],
                                         times_s_[i], pxs_[i], returns_[i - 1]);
      std::cout << cur_line << std::endl;
    }

  } else {
    // Write to a file.
    std::ofstream myfile(out_file_);
    for (uint i = 1; i < pxs_.size(); i++) {
      std::string cur_line = fmt::format("{},{},{},{}", times_str_[i],
                                         times_s_[i], pxs_[i], returns_[i - 1]);
      myfile << cur_line << std::endl;
    }
    myfile.close();
  }
}

void Returner::snapshotInside() {
  // Check the book's bb/ba, add sizes.

  const LevelBook *bk =
      pktrade::GlobalVar::book_manager_->find(BookId{mkt_, sym_});
  const Book<BookLevel>::BookSide<BuySide> &buy_side = bk->side<BuySide>();
  const Book<BookLevel>::BookSide<SellSide> &sell_side = bk->side<SellSide>();

  double bb = buy_side.begin()->px.toDouble();
  double ba = sell_side.begin()->px.toDouble();
  double mid = (bb + ba) / 2;

  // Only do this when the book kind of exists.
  if (bb != 0 && ba != 0) {
    // Push bids, asks, etc all to the vectors.
    times_str_.push_back(time_utils::nowToStr());
    times_s_.push_back(time_utils::nowToS());

    // Push back mids.
    pxs_.push_back(mid);
  }

  // std::cout << fmt::format("{} {} : {} {} {}", time_utils::nowToS(),
  // time_utils::nowToStr(), mid, bb, ba) << std::endl;

  // Callback myself.
  pktrade::GlobalVar::event_loop_->onTimeout(s_between_sample_,
                                             [&] { this->snapshotInside(); });
}

int main(int argc, char **argv) {
  google::InitGoogleLogging(argv[0]);

  std::string date;
  std::string sym;
  std::string out_file = "";

  // moving everything to utc time.
  std::string start_t_str = "00:00:00 UTC";
  std::string end_t_str = "23:59:00 UTC";
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";

  CLI::App cmd_flags("symstats");
  cmd_flags.add_option("--symbol", sym, "Symbol")->required();
  cmd_flags.add_option("--date", date, "Date")->required();
  cmd_flags.add_option("--out", out_file);

  PARSE(cmd_flags, argc, argv);

  // Only allowed for hyperliquid rn.
  Market mkt = Market::Hyperliquid;

  std::string start_dt = date + " " + start_t_str;
  std::string end_dt = date + " " + end_t_str;
  pktrade::GlobalVar::date_ = date;

  date::zoned_seconds start_t = pktrade::time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = pktrade::time_utils::dtToSecs(end_dt);

  // Can I get the normal time from that?

  // Set the globalvars.
  pktrade::GlobalVar::start_t_ = pktrade::time_utils::zsToTp(start_t);
  pktrade::GlobalVar::end_t_ = pktrade::time_utils::zsToTp(end_t);

  pktrade::GlobalVar::event_loop_ = new pktrade::EventLoop();

  std::vector<BookId> sub_bookids;

  // push_back.
  sub_bookids.emplace_back(BookId{mkt, SymbolId{sym}});

  pktrade::GlobalVar::book_manager_ =
      new BookManager(sub_bookids.begin(), sub_bookids.end());

  // Create the MDBeacon.
  // beacon gets made after the bm.
  pktrade::GlobalVar::md_beacon_ =
      new pktrade::md::MDBeacon(pktrade::GlobalVar::book_manager_,
                                sub_bookids.begin(), sub_bookids.end());
  std::vector<std::pair<std::filesystem::path, Market>> streams =
      pktrade::util::stream_paths(data_dir, sub_bookids, date);

  HistoricalFeed feed(pktrade::GlobalVar::book_manager_, streams.begin(),
                      streams.end(), std::stoi(pktrade::GlobalVar::date_));

  pktrade::GlobalVar::event_loop_->setPoller(std::make_unique<SimPoller>());
  pktrade::GlobalVar::event_loop_->registerPollables(feed);

  // Create the naranger.
  Returner returner(SymbolId{sym}, mkt, out_file);

  // Subscribe the narang.
  returner.subscribeData();

  pktrade::GlobalVar::md_beacon_->subscribeAndPrepare(
      pktrade::GlobalVar::book_manager_);

  pktrade::GlobalVar::event_loop_->runUntil(pktrade::GlobalVar::end_t_);

  // Finalize.
  returner.finalize();

  return 0;
}