#include "pktrade/book_manager.h"
#include "pktrade/event_loop.h"
#include "pktrade/feed.h"
#include "pktrade/util/cli.h"
#include <algorithm>
#include <deque>
#include <iostream>
#include <string>
#include <fmt/format.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

/*

Does msgprinting.

Moved this out into its own class so that we can use it in other signalscanner
too.


*/

using namespace pktrade;

class MsgPrinter : public BookListener, public MDListener {
 public:
  MsgPrinter(bool time_fullstr, double print_full_book_every, int print_n_levels, bool skip_msgs,
             BookId book_id, const BookManager* book_manager)
      : time_fullstr_(time_fullstr), print_full_book_every_(print_full_book_every),
        print_n_levels_(print_n_levels), first_tick_(true), our_book_(nullptr),
        skip_msgs_(skip_msgs), book_id_(book_id), bm_(book_manager){};

  EventLoop* loop = nullptr;
  int cnt = 0;

  void onLevelUpdate(const LevelBook& book, const LevelAdd&) override;
  void onLevelUpdate(const LevelBook& book, const LevelModify&) override;
  void onLevelUpdate(const LevelBook& book, const LevelDelete&) override;
  void onLevelUpdate(const LevelBook& book, const Trade&) override;
  void onQuote(const LevelBook&, const Quote&) override;

  // Do nothing on final.
  void onFinal(const LevelBook& book) override;

  void printBook(const LevelBook& book);
  void printBookInner();

  // MDListener overrides.
  void onMD(const mdmsg::PbMessage& msg) override {
    if (outOfBounds()) {
      return;
    }

    // std::cout << " -------------------- " << std::endl;
    // std::cout << msg.DebugString() << std::endl;
  }

  // Potentially do something here.
  void onLastLine(const std::string& line) override;

  // Check vs the gv bounds.
  bool outOfBounds();

 private:
  bool time_fullstr_;

  // if 0, print it on every callback.
  double print_full_book_every_;

  // Print the first n levels.
  int print_n_levels_ = 5;

  bool first_tick_;
  const LevelBook* our_book_;
  bool skip_msgs_;
  BookId book_id_;
  const BookManager* bm_ = nullptr;
  bool trade_happened_ = false;
};

void MsgPrinter::printBook(const LevelBook& book) {
  // Print out book
  if (first_tick_) {
    first_tick_ = false;
    our_book_ = &book;
    if (print_full_book_every_ > 0) {
      // Then maybe printBook;
      printBookInner();
    }
  }

  if (print_full_book_every_ == 0) {
    printBookInner();
  }
}

void MsgPrinter::onLevelUpdate(const LevelBook& book, const LevelAdd& lvl_add) {
  if (outOfBounds()) {
    return;
  }

  if (!skip_msgs_) {
    // Print out time.
    if (time_fullstr_) {
      // print out
      printf("%s %lld ", time_utils::nowToStr().c_str(), time_utils::nowToMs());
    } else {
      printf("%lld ", time_utils::nowToMs());
    }

    printf("LevelAdd Price %f Qty %f Side %s transact_t %ld lastticker %ld \n",
           lvl_add.px.toDouble(), lvl_add.qty.toDouble(),
           std::string(magic_enum::enum_name(lvl_add.side)).c_str(), lvl_add.transact_t,
           bm_->getLastTickerUpdateId(book_id_));
  }

  printBook(book);
}

void MsgPrinter::onLevelUpdate(const LevelBook& book, const LevelModify& lvl_mod) {
  if (outOfBounds()) {
    return;
  }

  if (!skip_msgs_) {
    if (time_fullstr_) {
      // print out
      printf("%s %lld ", time_utils::nowToStr().c_str(), time_utils::nowToMs());
    } else {
      printf("%lld ", time_utils::nowToMs());
    }

    // Print out book
    printf("LevelMod Price %f Qty %f oldQty %f Side %s transact_t %ld lastticker "
           "%ld \n",
           lvl_mod.px.toDouble(), lvl_mod.qty.toDouble(), lvl_mod.prev_qty.toDouble(),
           std::string(magic_enum::enum_name(lvl_mod.side)).c_str(), lvl_mod.transact_t,
           bm_->getLastTickerUpdateId(book_id_));
  }

  printBook(book);
}

void MsgPrinter::onLevelUpdate(const LevelBook& book, const LevelDelete& lvl_del) {
  if (outOfBounds()) {
    return;
  }

  if (!skip_msgs_) {
    if (time_fullstr_) {
      // print out
      printf("%s %lld ", time_utils::nowToStr().c_str(), time_utils::nowToMs());
    } else {
      printf("%lld ", time_utils::nowToMs());
    }

    // Print out book
    printf("LevelDel Price %f oldQty %f Side %s transact_t %ld lastticker %ld\n",
           lvl_del.px.toDouble(),

           lvl_del.prev_qty.toDouble(), std::string(magic_enum::enum_name(lvl_del.side)).c_str(),
           lvl_del.transact_t, bm_->getLastTickerUpdateId(book_id_));
  }

  printBook(book);
}

void MsgPrinter::onLevelUpdate(const LevelBook& book, const Trade& trd) {
  if (outOfBounds()) {
    return;
  }

  if (!skip_msgs_) {
    if (time_fullstr_) {
      // print out
      printf("%s %lld ", time_utils::nowToStr().c_str(), time_utils::nowToMs());
    } else {
      printf("%lld ", time_utils::nowToMs());
    }

    printf("Trade Price %f Qty %f Side %s transact_t %ld\n", trd.px.toDouble(), trd.qty.toDouble(),
           std::string(magic_enum::enum_name(trd.passive_side)).c_str(), trd.transact_t);
  }

  trade_happened_ = true;
  printBook(book);
}

void MsgPrinter::onQuote(const LevelBook& book, const Quote& quote) {
  if (outOfBounds()) {
    return;
  }

  if (!skip_msgs_) {
    if (time_fullstr_) {
      // print out
      printf("%s %lld ", time_utils::nowToStr().c_str(), time_utils::nowToMs());
    } else {
      printf("%lld ", time_utils::nowToMs());
    }

    std::cout << fmt::format("Quote Bid {} @ {} Ask {} @ {} mid {} transact_t {}",
      quote.bs.toDouble(), quote.bb.toDouble(), quote.as.toDouble(), quote.ba.toDouble(),
       quote.mid, quote.source_t) << std::endl;

  }

  printBook(book);
}

void MsgPrinter::onFinal(const LevelBook& book) {
  if (outOfBounds()) {
    return;
  }

  // I uncomment this to help inspect book flagging state when implementing new books.
  // Don't use this normally.
  if (trade_happened_) {
    // printBookInner();
  }
  trade_happened_ = false;

  if (time_fullstr_) {
    // print out
    printf("%s %lld ", time_utils::nowToStr().c_str(), time_utils::nowToMs());
  } else {
    printf("%lld ", time_utils::nowToMs());
  }
  printf(" FINAL\n");
}

void MsgPrinter::printBookInner() {
  // Walk the book and push them into deques.

  std::deque<std::string> bids, asks;

  auto& buy_side = our_book_->side<BuySide>();
  int buy_count = 0;
  auto printLevel = [](const BookLevel& level) -> std::string {
    std::string s = fmt::format("{} : {}", level.px.toDouble(), level.qty.toDouble());
    if (level.flagged_shares > 0) {
      s += fmt::format(" ({} flagged)", level.flagged_shares.toDouble());
    }
    return s;
  };
  for (auto& buy_lvl : buy_side) {
    if (buy_count >= print_n_levels_) {
      break;
    }

    bids.push_back(printLevel(buy_lvl));
    buy_count++;
  }

  int sell_count = 0;
  auto& sell_side = our_book_->side<SellSide>();
  for (auto& sell_lvl : sell_side) {
    if (sell_count >= print_n_levels_) {
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

  if (print_full_book_every_ > 0) {
    // schedule another printbook some time in the future.
    if (outOfBounds()) {
      return;
    }

    loop->onTimeout(std::chrono::milliseconds{int(print_full_book_every_ * 1000)},
                    [&] { this->printBookInner(); });
  }
}

void MsgPrinter::onLastLine(const std::string& line) {
  if (outOfBounds()) {
    return;
  }

  // Bloop. If I'm getting this, I should print out the raw line.
  std::cout << "*************************************************" << std::endl;
  std::cout << line << std::endl;
  std::cout << "*************************************************\n" << std::endl;
}

bool MsgPrinter::outOfBounds() {
  if (Clock::now() < pktrade::GlobalVar::start_t_) {
    return true;
  }

  if (Clock::now() > pktrade::GlobalVar::end_t_) {
    return true;
  }
  return false;
}
