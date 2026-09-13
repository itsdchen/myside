#include "pktrade/context/global_vars.h"

#include "pktrade/util/basiclib.h"

#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"

#include <algorithm>
#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <magic_enum.hpp>
#include <set>
#include <stdio.h>

/*
  Narang-like. Sample command:

./bin.debug/symstats --book BinanceSPOT --symbol DYDXUSDT --date 20230106

Output:
Symbol: DYDXUSDT
Market: BinanceSPOT
Num_Trades: 13095
Volume: 3408855.590000
Avg_Mid: 1.164717
Avg_Spread: 0.001448
Num_Mid_Changes: 3732
Num locked 75 times
Num crossed 9 times

TODO: Multi markets and ish. Also some notion of ticksize, yeah?

*/

using namespace pktrade;

class SymStats : public pktrade::md::BeaconListener {
 public:
  SymStats(SymbolId sym, Market mkt, bool verbose, bool csvmode);

  void subscribeData();

  void checkInside();

  // copied over from msgprint.
  void printBookInner();

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) override;
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {};
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) override;
  void onTrade(const LevelBook& bk, const Trade& trd) override;

  // Move all the inside checks to onFinal.
  void onFinal(const LevelBook& bk) override;
  void finalize();

  void snapshotInside();

 protected:
  SymbolId sym_;
  // This should be
  Market mkt_;

  double bb_;
  double ba_;

  // Just counting vol traded
  int num_trds_;
  double vol_;

  int64_t last_inside_t_;

  double time_avg_denom_;

  // For running mid
  double last_mid_;
  double mid_num_;

  // Does it matter to track
  int num_mid_changes_;

  double max_spread_ = 0;

  // For running spread
  double spread_num_;

  double last_spread_;

  int num_crossed_;
  int num_locked_;

  bool final_should_oninside_ = false;

  // If it's been nonzero time since locked and crossed,
  // Then append to our counts.
  bool cur_crossed_;
  bool cur_locked_;

  // The time the current locked book, locked itself
  int64_t t_locked_;
  int64_t t_crossed_;

  // How long we wait before we figure the book is in a bad state.
  int64_t t_before_bookbad_;

  bool good_yet_;
  bool verbose_;
  bool csvmode_;

  // Don't let this go on forever.
  int cur_trd_pos_;
  int max_trd_samples_;
  // samples of trade sizes. Use to sample for median size.
  std::vector<double> trd_sizes_;

  // Samples of inside sizes. How much liquidity on inside.
  std::vector<double> inside_sizes_;

  std::chrono::seconds s_between_sample_;

  // Depth stat: for each snapshot, walk the book on each side consuming
  // fixed dollar-notional levels; record the bps-from-mid at which the
  // threshold is reached. Reported as medians at finalize.
  // Notional interpretation: price × qty. Prices are USDC-native on
  // Hyperliquid so px×qty is USD-equivalent directly. For non-USDC-native
  // venues, thresholds are interpreted in the venue's price currency.
  // Thresholds: $30k / $60k / $250k / $2M — chosen to cover typical
  // small MM clip → single institutional slice → whole-day-of-edge.
  std::vector<double> depth_thresh_notional_ = {3.0e4, 6.0e4, 2.5e5, 2.0e6};
  // depth_bps_bid_[i][k] = kth sample of bps-from-mid to consume
  // depth_thresh_notional_[i] on the bid side (i.e. selling into bids).
  std::vector<std::vector<double>> depth_bps_bid_;
  std::vector<std::vector<double>> depth_bps_ask_;
  // How many snapshots had enough book depth for each threshold. Reported
  // as a "coverage" hint so downstream tools know when the median is thin.
  std::vector<int> depth_n_covered_bid_;
  std::vector<int> depth_n_covered_ask_;
  int depth_n_snapshots_ = 0;
};

// Walk one side of the book from inside out until cumulative notional (px×qty)
// reaches `thresh_notional`. Returns the depth in bps-from-mid at which the
// threshold was reached, or NaN if the visible book cannot fill the size.
// `side_range` is the buy_side or sell_side view; `mid` is the current mid.
// `is_bid` chooses the sign of (touch - mid) so both sides come out positive.
template <typename SideRange>
static double depth_bps_for_notional(const SideRange& side, double mid,
                                     double thresh_notional, bool is_bid) {
  if (mid <= 0) return std::nan("");
  double cum = 0;
  double last_px = 0;
  for (auto& lvl : side) {
    double px = lvl.px.toDouble();
    double qty = lvl.qty.toDouble();
    if (px <= 0 || qty <= 0) continue;
    cum += px * qty;
    last_px = px;
    if (cum >= thresh_notional) {
      double signed_diff = is_bid ? (mid - last_px) : (last_px - mid);
      // Guard against negative bps (can happen if book is locked/crossed at
      // the touch); report 0 in that case, since consuming at mid is the
      // best-case zero-impact fill.
      if (signed_diff < 0) signed_diff = 0;
      return signed_diff / mid * 1e4;
    }
  }
  return std::nan("");
}


SymStats::SymStats(SymbolId sym, Market mkt, bool verbose, bool csvmode)
    : sym_(sym), mkt_(mkt), verbose_(verbose), csvmode_(csvmode) {
  // Probably nothing to do for now.

  // Just initialize a bunch of stuff.
  bb_ = 0;
  ba_ = 1e20;
  num_trds_ = 0;
  vol_ = 0;
  last_inside_t_ = 0;
  time_avg_denom_ = 0;
  last_mid_ = 0;
  mid_num_ = 0;
  num_mid_changes_ = 0;
  max_spread_ = 0;
  spread_num_ = 0;
  last_spread_ = 0;
  num_crossed_ = 0;
  num_locked_ = 0;

  cur_crossed_ = false;
  cur_locked_ = false;
  t_locked_ = 0;
  t_crossed_ = 0;

  t_before_bookbad_ = 30;

  good_yet_ = false;

  cur_trd_pos_ = 0;
  // Just hardcode this for now.
  max_trd_samples_ = 10000;

  // Depth stat: one sample vector per threshold per side.
  depth_bps_bid_.resize(depth_thresh_notional_.size());
  depth_bps_ask_.resize(depth_thresh_notional_.size());
  depth_n_covered_bid_.assign(depth_thresh_notional_.size(), 0);
  depth_n_covered_ask_.assign(depth_thresh_notional_.size(), 0);

  // Add a callback to start sampling the inside too.
  // Sample it like, every 2 minutes, idk.
  s_between_sample_ = std::chrono::seconds(60 * 2);

  pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_ + s_between_sample_,
                                             [&] { this->snapshotInside(); });
}

void SymStats::subscribeData() {
  // Assume that the beacon, bookmanager already exist. Let's add stuff in.
  BookId book_id{mkt_, sym_};

  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd, pktrade::md::BeaconCBType::LvlMod,
      pktrade::md::BeaconCBType::LvlDel, pktrade::md::BeaconCBType::Trd,
      pktrade::md::BeaconCBType::Final};

  pktrade::GlobalVar::md_beacon_->addListener(book_id, this, cb_types);
}

void SymStats::printBookInner() {
  // Walk the book and push them into deques.

  std::deque<std::string> bids, asks;

  const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(BookId{mkt_, sym_});

  auto& buy_side = bk->side<BuySide>();
  int buy_count = 0;
  auto printLevel = [](const BookLevel& level) -> std::string {
    std::string s = fmt::format("{} : {}", level.px.toDouble(), level.qty.toDouble());
    if (level.flagged_shares > 0) {
      s += fmt::format(" ({} flagged)", level.flagged_shares.toDouble());
    }
    return s;
  };
  for (auto& buy_lvl : buy_side) {
    if (buy_count >= 10) {
      break;
    }

    bids.push_back(printLevel(buy_lvl));
    buy_count++;
  }

  int sell_count = 0;
  auto& sell_side = bk->side<SellSide>();
  for (auto& sell_lvl : sell_side) {
    if (sell_count >= 10) {
      break;
    }
    asks.push_back(printLevel(sell_lvl));
    sell_count++;
  }

  // Now print them.

  std::cout << "~~~~~~~~~~~~~~~~~~~~~~ FULL BOOK ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~" << std::endl;

  std::cout << time_utils::nowToStr() << " | " << time_utils::nowToMs() << std::endl;

  // Iterate backwards on the asks
  for (auto it = asks.crbegin(); it != asks.crend(); ++it) {
    std::cout << *it << std::endl;
  }

  std::cout << "              " << std::endl;

  for (auto it = bids.cbegin(); it != bids.cend(); ++it) {
    std::cout << *it << std::endl;
  }

  // Iterate forwards on the bids

  std::cout << "~~~~~~~~~~~~~~~~~~~~~~    END   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~" << std::endl
            << std::endl;
}

void SymStats::checkInside() {
  // Check bb and ba.
  // Modify spreads if need be.
  double newbb, newba;
  double newspread, newmid;

  // New values.
  const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(BookId{mkt_, sym_});
  const Book<BookLevel>::BookSide<BuySide>& buy_side = bk->side<BuySide>();

  if (!buy_side.empty()) {
    for (auto& lvl : buy_side) {
      if (effectiveQty(lvl) > Quantity{0}) {
        newbb = lvl.px.toDouble();
        //    std::cout << " Got " << lvl.px.toDouble() << std::endl;
        break;
      } else {
        //    std::cout << " Skipped " << lvl.px.toDouble() << std::endl;
      }
    }
  } else {
    newbb = 0;
  }

  const Book<BookLevel>::BookSide<SellSide>& sell_side = bk->side<SellSide>();

  // Let's use flagged to calculate this.

  if (!sell_side.empty()) {
    for (auto& lvl : sell_side) {
      if (effectiveQty(lvl) > Quantity{0}) {
        newba = lvl.px.toDouble();
        //    std::cout << " Got " << lvl.px.toDouble() << std::endl;
        break;
      } else {
        //     std::cout << " Skipped " << lvl.px.toDouble() << std::endl;
      }
    }
  } else {
    newba = 1e20;
  }

  newspread = newba - newbb;
  newmid = (newbb + newba) / 2.0;

  // Locked case.
  if (newbb == newba) {
    if (cur_locked_) {
      // Then do nothing.
    } else {
      cur_locked_ = true;
      t_locked_ = time_utils::nowToMs();
    }
  }

  // Crossed case.
  if (newbb > newba) {
    if (cur_crossed_) {
      // Again, do nothing.
    } else {
      cur_crossed_ = true;
      t_crossed_ = time_utils::nowToMs();
      /*
      std::cout << "CROSSED" << std::endl;
      printBookInner();
    */
    }
  }

  // Good case.
  if (newbb < newba) {
    // Check locked.
    if (cur_locked_ && time_utils::nowToMs() - t_locked_ > t_before_bookbad_) {
      if (verbose_) {
        printf("%s: became locked\n", time_utils::msToString(t_locked_).c_str());
      }
      num_locked_++;
    }
    // Otherwise, reset locked.
    cur_locked_ = false;

    if (cur_crossed_ && time_utils::nowToMs() - t_crossed_ > t_before_bookbad_) {
      if (verbose_) {
        printf("%s: became crossed\n", time_utils::msToString(t_crossed_).c_str());
      }
      num_crossed_++;

      // Boop.
    }
    cur_crossed_ = false;
  }

  bool was_bad = bb_ == 0 || ba_ == 1e20;
  bool is_bad = newbb == 0 || newba == 1e20;

  // Well. Let's just check the states.
  // If we're starting out from 0 data, but we're now in a good state.:
  if (!good_yet_ && !is_bad) {
    num_mid_changes_++;

    bb_ = newbb;
    ba_ = newba;

    last_mid_ = newmid;
    last_spread_ = newspread;

    good_yet_ = true;
    last_inside_t_ = pktrade::time_utils::nowToMs();
    return;
  }

  // Otherwise. If it's in a good state we update.
  if (!is_bad) {
    double delta_t = pktrade::time_utils::nowToMs() - last_inside_t_;
    last_inside_t_ = pktrade::time_utils::nowToMs();

    if (bb_ != newbb || ba_ != newba) {
      num_mid_changes_++;

      bb_ = newbb;
      ba_ = newba;

      // Push the mid, spread nums on.

      mid_num_ += last_mid_ * delta_t;
      spread_num_ += last_spread_ * delta_t;

      if (newspread > max_spread_) {
        // std::cout << fmt::format("{}: New max spread: {}\n", time_utils::nowToStr(),
        // newspread).c_str();
        max_spread_ = newspread;
      }

      time_avg_denom_ += delta_t;

      // Update mid, spread.
      last_mid_ = newmid;
      last_spread_ = newspread;
    }
  }

  // At this point, just not gonna do any updates that's not
  //
}

void SymStats::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (!good_yet_) {
    final_should_oninside_ = true;
    // checkInside();
    return;
  }

  if (lvl_add.side == Side::Buy && lvl_add.px.toDouble() > bb_) {
    final_should_oninside_ = true;
    // checkInside();
    return;
  } else if (lvl_add.side == Side::Sell && lvl_add.px.toDouble() > ba_) {
    final_should_oninside_ = true;
    // checkInside();
    return;
  }
}

void SymStats::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (!good_yet_) {
    final_should_oninside_ = true;
    // checkInside();
    return;
  }

  if (lvl_del.side == Side::Buy && lvl_del.px.toDouble() == bb_) {
    final_should_oninside_ = true;
    // checkInside();
    return;
  } else if (lvl_del.side == Side::Sell && lvl_del.px.toDouble() == ba_) {
    final_should_oninside_ = true;
    // checkInside();
    return;
  }
}

void SymStats::onFinal(const LevelBook& bk) {
  if (final_should_oninside_) {
    checkInside();
    final_should_oninside_ = false;
  }
}

void SymStats::onTrade(const LevelBook& bk, const Trade& trd) {
  num_trds_++;
  vol_ += trd.qty.toDouble();

  if (trd_sizes_.size() < max_trd_samples_) {
    trd_sizes_.push_back(trd.qty.toDouble());
  } else {
    // Start ring buffering m8.
    trd_sizes_[cur_trd_pos_] = trd.qty.toDouble();
    cur_trd_pos_++;
    cur_trd_pos_ = cur_trd_pos_ % max_trd_samples_;
  }
}

// Compute the median of `xs`. Returns NaN if empty.
static double median_of(std::vector<double>& xs) {
  if (xs.empty()) return std::nan("");
  std::sort(xs.begin(), xs.end());
  return xs[xs.size() / 2];
}

void SymStats::finalize() {
  // Figure out median inside liq and trade sizes.
  std::sort(trd_sizes_.begin(), trd_sizes_.end());
  std::sort(inside_sizes_.begin(), inside_sizes_.end());

  double med_trd = 0;
  if (trd_sizes_.size() > 0) {
    med_trd = trd_sizes_[(int)trd_sizes_.size() / 2];
  }
  double med_inside = 0;
  if (inside_sizes_.size() > 0) {
    med_inside = inside_sizes_[(int)inside_sizes_.size() / 2];
  }

  // Depth medians: bps-from-mid to consume each notional threshold on each side.
  // NaN if the visible book couldn't fill the threshold in any snapshot.
  std::vector<double> med_bid_bps(depth_thresh_notional_.size());
  std::vector<double> med_ask_bps(depth_thresh_notional_.size());
  for (size_t t = 0; t < depth_thresh_notional_.size(); t++) {
    med_bid_bps[t] = median_of(depth_bps_bid_[t]);
    med_ask_bps[t] = median_of(depth_bps_ask_[t]);
  }

  if (csvmode_) {
    printf("symbol,market,num_trades,vol,med_trdsz,med_inside_liq,avg_mid,avg_"
           "sprd,spread_pct,n_midchanges,"
           "depth_bps_bid_30k,depth_bps_ask_30k,"
           "depth_bps_bid_60k,depth_bps_ask_60k,"
           "depth_bps_bid_250k,depth_bps_ask_250k,"
           "depth_bps_bid_2M,depth_bps_ask_2M,"
           "depth_n_covered_bid_30k,depth_n_covered_ask_30k,"
           "depth_n_covered_bid_60k,depth_n_covered_ask_60k,"
           "depth_n_covered_bid_250k,depth_n_covered_ask_250k,"
           "depth_n_covered_bid_2M,depth_n_covered_ask_2M,"
           "depth_n_snapshots\n");
    printf("%s,%s,%d,%f,%f,%f,%f,%f,%f,%d,"
           "%f,%f,%f,%f,%f,%f,%f,%f,"
           "%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
           sym_.get().c_str(),
           magic_enum::enum_name(mkt_).data(), num_trds_, vol_, med_trd, med_inside,
           mid_num_ / time_avg_denom_, spread_num_ / time_avg_denom_, spread_num_ / mid_num_,
           num_mid_changes_,
           med_bid_bps[0], med_ask_bps[0], med_bid_bps[1], med_ask_bps[1],
           med_bid_bps[2], med_ask_bps[2], med_bid_bps[3], med_ask_bps[3],
           depth_n_covered_bid_[0], depth_n_covered_ask_[0],
           depth_n_covered_bid_[1], depth_n_covered_ask_[1],
           depth_n_covered_bid_[2], depth_n_covered_ask_[2],
           depth_n_covered_bid_[3], depth_n_covered_ask_[3],
           depth_n_snapshots_);

  } else {
    // Do some printfs.
    printf("Symbol: %s\n", sym_.get().c_str());
    // data() is prob not the best way to do it. Oh well...
    printf("Market: %s\n", magic_enum::enum_name(mkt_).data());
    printf("Num_Trades: %d\n", num_trds_);
    printf("Volume: %f\n", vol_);
    printf("~Med_tradesize: %f\n", med_trd);
    printf("~Med_inside_liq: %f\n", med_inside);
    printf("Avg_Mid: %f\n", mid_num_ / time_avg_denom_);
    printf("Max_Spread: %f\n", max_spread_);
    printf("Avg_Spread: %f\n", spread_num_ / time_avg_denom_);
    printf("Spread_pct: %f\n", spread_num_ / mid_num_);
    printf("Num_Mid_Changes: %d\n", num_mid_changes_);
    printf("Num_locked %d\n", num_locked_);
    printf("Num_crossed %d\n", num_crossed_);
    printf("Depth_snapshots: %d\n", depth_n_snapshots_);
    const char* labels[] = {"$30k", "$60k", "$250k", "$2M"};
    for (size_t t = 0; t < depth_thresh_notional_.size(); t++) {
      printf("Depth_bps_%s  bid=%f (n=%d)  ask=%f (n=%d)\n",
             labels[t], med_bid_bps[t], depth_n_covered_bid_[t],
             med_ask_bps[t], depth_n_covered_ask_[t]);
    }
  }
}

void SymStats::snapshotInside() {
  // Check the book's bb/ba, add sizes.

  double insideLiq = 0;
  const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(BookId{mkt_, sym_});
  const Book<BookLevel>::BookSide<BuySide>& buy_side = bk->side<BuySide>();
  const Book<BookLevel>::BookSide<SellSide>& sell_side = bk->side<SellSide>();
  if (!buy_side.empty() && !sell_side.empty()) {
    insideLiq += buy_side.begin()->qty.toDouble();
    insideLiq += sell_side.begin()->qty.toDouble();
  }
  // Push it.
  inside_sizes_.push_back(insideLiq);

  // Depth sampling: for each $-notional threshold, walk both sides.
  // Uses current best bid/ask from the state we track in checkInside().
  // Skip if book is empty or bad.
  bool book_ok = !buy_side.empty() && !sell_side.empty()
                 && bb_ > 0 && ba_ < 1e19 && bb_ < ba_;
  if (book_ok) {
    double mid = (bb_ + ba_) / 2.0;
    depth_n_snapshots_++;
    for (size_t t = 0; t < depth_thresh_notional_.size(); t++) {
      double thresh = depth_thresh_notional_[t];
      double bid_bps = depth_bps_for_notional(buy_side, mid, thresh, /*is_bid=*/true);
      double ask_bps = depth_bps_for_notional(sell_side, mid, thresh, /*is_bid=*/false);
      if (!std::isnan(bid_bps)) {
        depth_bps_bid_[t].push_back(bid_bps);
        depth_n_covered_bid_[t]++;
      }
      if (!std::isnan(ask_bps)) {
        depth_bps_ask_[t].push_back(ask_bps);
        depth_n_covered_ask_[t]++;
      }
    }
  }

  // Callback myself.
  pktrade::GlobalVar::event_loop_->onTimeout(s_between_sample_, [&] { this->snapshotInside(); });
}

int main(int argc, char** argv) {
  google::InitGoogleLogging(argv[0]);

  std::string date;
  std::string start_date;
  std::string end_date;
  std::string book;
  std::string sym;
  std::string start_t_str = "03:00:00 America/New_York";
  std::string end_t_str = "16:00:00 America/New_York";
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";
  bool csvmode = false;

  bool verbose = false;

  CLI::App cmd_flags("symstats");
  cmd_flags.add_option("-b,--book", book, "Book")->required();
  cmd_flags.add_option("--symbol", sym, "Symbol")->required();
  cmd_flags.add_option("--datadir", data_dir, "Data Dir");
  cmd_flags.add_option("--date", date, "Date")->required();
  cmd_flags.add_option("--end-date", end_date, "End Date");
  cmd_flags.add_option("--start-time", start_t_str, "Start Time");
  cmd_flags.add_option("--end-time", end_t_str, "End Time");
  cmd_flags.add_flag("-v,--verbose", verbose, "Verbose mode");
  cmd_flags.add_flag("--csv", csvmode, "csv mode");

  PARSE(cmd_flags, argc, argv);

  Market mkt = magic_enum::enum_cast<Market>(book).value();

  pktrade::GlobalVar::date_ = date;

  if (end_date == "") {
    end_date = date;
  }

  // If the start_t_str is between 18:00 and 23:59, then we should
  // consider it the next L1Date (our day rollover period happens at 18:00 every day.)
  start_date = date;

  std::string start_dt = start_date + " " + start_t_str;
  std::string end_dt = end_date + " " + end_t_str;

  date::zoned_seconds start_t = pktrade::time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = pktrade::time_utils::dtToSecs(end_dt);

  // Validate that we're using America/New_York timezone (required for 18:00 ET day rollover logic)
  std::string tz_name = std::string(start_t.get_time_zone()->name());
  if (tz_name != "America/New_York") {
    throw std::runtime_error(fmt::format(
      "Day rollover logic requires America/New_York timezone, but got: {}. "
      "Please fix timezone handling in time_utils::dtToSecs or update this logic.",
      tz_name));
  }

  // Parse the hour from start time in America/New_York and adjust start_date if >= 18:00 ET
  auto start_local = start_t.get_local_time();
  auto start_dp = date::floor<date::days>(start_local);
  auto start_tod = date::make_time(start_local - start_dp);
  int start_hour = start_tod.hours().count();

  if (start_hour >= 18) {
    // Subtract one day from the date (trading day starts at 18:00 ET)
    // date format is YYYYMMDD
    int year = std::stoi(date.substr(0, 4));
    int month = std::stoi(date.substr(4, 2));
    int day = std::stoi(date.substr(6, 2));

    date::year_month_day ymd = date::year{year}/date::month{month}/date::day{day};
    date::sys_days prev_day = date::sys_days{ymd} - date::days{1};
    date::year_month_day prev_ymd = date::year_month_day{prev_day};

    start_date = fmt::format("{:04d}{:02d}{:02d}",
                            int(prev_ymd.year()),
                            unsigned(prev_ymd.month()),
                            unsigned(prev_ymd.day()));

    // Recreate start_dt and start_t with adjusted start_date
    start_dt = start_date + " " + start_t_str;
    start_t = pktrade::time_utils::dtToSecs(start_dt);
  }

  // Set the globalvars.
  pktrade::GlobalVar::start_t_ = pktrade::time_utils::zsToTp(start_t);
  pktrade::GlobalVar::end_t_ = pktrade::time_utils::zsToTp(end_t);

  // Handle case where start time is after end time (e.g., 18:01 start, 16:00 end next day)
  if (pktrade::GlobalVar::start_t_ > pktrade::GlobalVar::end_t_) {
    pktrade::GlobalVar::start_t_ -= std::chrono::hours(24);
  }

  // std::cout << "start_t: " << start_t << std::endl;

  pktrade::GlobalVar::event_loop_ = new pktrade::EventLoop();

  std::vector<BookId> sub_bookids;

  // push_back.
  sub_bookids.emplace_back(BookId{mkt, SymbolId{sym}});

  pktrade::GlobalVar::book_manager_ = new BookManager(sub_bookids.begin(), sub_bookids.end());

  // Create the MDBeacon.
  // beacon gets made after the bm.
  pktrade::GlobalVar::md_beacon_ = new pktrade::md::MDBeacon(
      pktrade::GlobalVar::book_manager_, sub_bookids.begin(), sub_bookids.end());
  // Use start_date to get the correct files for overnight sessions that span 24h.
  std::vector<std::pair<std::filesystem::path, Market>> streams =
      pktrade::util::stream_paths(data_dir, sub_bookids, start_date);

  HistoricalFeed feed(pktrade::GlobalVar::book_manager_, streams.begin(), streams.end(),
                      std::stoi(pktrade::GlobalVar::date_));
  pktrade::GlobalVar::event_loop_->setPoller(std::make_unique<SimPoller>());
  pktrade::GlobalVar::event_loop_->registerPollables(feed);

  // Create the naranger.
  SymStats sym_stats(SymbolId{sym}, mkt, verbose, csvmode);

  // Subscribe the narang.
  sym_stats.subscribeData();

  pktrade::GlobalVar::md_beacon_->subscribeAndPrepare(pktrade::GlobalVar::book_manager_);

  pktrade::GlobalVar::event_loop_->runUntil(pktrade::GlobalVar::end_t_);

  // Finalize.
  sym_stats.finalize();

  return 0;
}
