#include "pktrade/book_manager.h"

#include <algorithm>

#include <fmt/format.h>
#include <fmt/ostream.h>
#include <glog/logging.h>

#include "pktrade/type_traits.h"

#include "pktrade/context/global_vars.h"

#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

/*


TODO:
  - Hyperliquid: some actions do not give back the last update type.
    This is kind of wrong, but also affects a bunch of existing live trading, so
I don't wan tot just turn it on wholesale. Because it will require a bit of
testing to make sure we don't just change live behavior a ton. So I'm just
noting that we should do it eventually.
*/

namespace pktrade {

namespace {

Market pbMarketToMarket(mdmsg::PbMarket market) noexcept {
  // PbMarket is a wire value. magic_enum::enum_value treats the integer as an
  // index into Market's compiled enum list and asserts on schema skew.
  switch (market) {
  case mdmsg::PBMARKET_UNKNOWN:
    return Market::Unknown;
  case mdmsg::PBMARKET_BINANCE_SPOT:
    return Market::BinanceSPOT;
  case mdmsg::PBMARKET_BINANCE_FUTURES:
    return Market::BinanceFutures;
  case mdmsg::PBMARKET_BINANCE_COINFUTURES:
    return Market::BinanceCOINFutures;
  case mdmsg::PBMARKET_HYPERLIQUID:
    return Market::Hyperliquid;
  case mdmsg::PBMARKET_BYBITSPOT:
    return Market::BybitSPOT;
  case mdmsg::PBMARKET_BYBITDERIV:
    return Market::BybitDeriv;
  case mdmsg::PBMARKET_BYBITINVERSEDERIV:
    return Market::BybitInverseDeriv;
  case mdmsg::PBMARKET_TOPBOOK_EQUITY:
    return Market::TopBookEquity;
  case mdmsg::PBMARKET_TOPBOOK_CME:
    return Market::TopBookCme;
  }
  // Out-of-range wire value. LiveFeed and pkmultifeed validate/log this
  // earlier in the live path; keep this noexcept fallback non-throwing.
  return Market::Unknown;
}

} // namespace

// When we get a level update.
bool ALWAYS_UNFLAG = true;

[[nodiscard]] const LevelBook* BookManager::find(const BookId& book_id) const {
  auto it = books_.find(book_id);
  if (it != books_.end()) {
    return it->second.book.get();
  }
  return nullptr;
}

const BookData* BookManager::findBookData(const BookId& book_id) const {
  auto it = books_.find(book_id);
  if (it != books_.end()) {
    return &(it->second);
  }
  return nullptr;
}

const int64_t BookManager::getLastTickerUpdateId(const BookId& book_id) const {
  auto it = books_.find(book_id);
  if (it != books_.end()) {
    return it->second.ticker_update_last_update_id;
  }
  // Did not find it.
  return -1;
}

const int64_t BookManager::getLastMsgId(const BookId& book_id) const {
  auto it = books_.find(book_id);
  if (it != books_.end()) {
    return it->second.last_msg_id_;
  }
  // Did not find it.
  return -1;
}

void BookManager::subscribe(const BookId& book_id, BookListener* listener) {
  auto& listeners = books_.at(book_id).listeners;
  if (std::find(listeners.begin(), listeners.end(), listener) != listeners.end()) {
    throw std::runtime_error("Listener already registered");
  }
  listeners.emplace_back(listener);
}

void BookManager::onMD(const mdmsg::PbMessage& msg) {
  auto book_id = getBookId(msg);

  auto it = books_.find(book_id);
  if (it == books_.end()) {
    if (pktrade::GlobalVar::live_) {
      // Don't complain, a feed might be listening to multiple things.
      // But might be a reason why we have issues. Something might've been
      // dropped if the live feed was not configuring stuff correctly.
      return;
    }
    // In histmode, this might be a problem.

    // This is ok for now. I know I made some custom datafiles, do not worry
    // about it. But DONOTMERGE
    return;
    throw std::runtime_error(
        "Received unexpected BookUpdate: " + std::string(magic_enum::enum_name(msg.market())) +
        " " + msg.symbol_id());
  }

  if (book_id.first == Market::BinanceSPOT || book_id.first == Market::BinanceFutures ||
      book_id.first == Market::BinanceCOINFutures) {
    if (msg.has_book_update()) {
      onBinanceMDDiff(msg.book_update(), &it->second);
    } else if (msg.has_ticker_update()) {
      onBinanceMDBookTicker(msg.ticker_update(), &it->second);
    } else if (msg.has_book_trade()) {
      onBinanceMDTrade(msg.book_trade(), &it->second);
    } else if (msg.has_book_snapshot()) {
      onBinanceMDSnapshot(msg.book_snapshot(), &it->second);
    }
  } else if (book_id.first == Market::Hyperliquid) {
    if (msg.has_book_update()) {
      onHyperliquidMDDiff(msg.book_update(), &it->second);
    } else if (msg.has_book_trade()) {
      onHyperliquidMDTrade(msg.book_trade(), &it->second);
    } else if (msg.has_book_snapshot()) {
      onHyperliquidMDSnapshot(msg.book_snapshot(), &it->second);
    } else {
      LOG_EVERY_N(INFO, 101) << "book_manager: received unhandled hyperliquid "
                             << msg.ShortDebugString();
    }
  } else if (book_id.first == Market::BybitDeriv || book_id.first == Market::BybitInverseDeriv ||
             book_id.first == Market::BybitSPOT) {
    if (msg.has_book_update()) {
      if (msg.book_update().depth() == 1) {
        onBybitMDInsideDiff(msg.book_update(), &it->second);
      } else {
        onBybitMDDiff(msg.book_update(), &it->second);
      }
    } else if (msg.has_book_trade()) {
      onBybitMDTrade(msg.book_trade(), &it->second);
    } else if (msg.has_book_snapshot()) {
      if (msg.book_snapshot().bids().size() == 1) {
        onBybitMDInsideSnapshot(msg.book_snapshot(), &it->second);
      } else {
        onBybitMDSnapshot(msg.book_snapshot(), &it->second);
      }
    }
  } else if (book_id.first == Market::TopBookEquity || book_id.first == Market::TopBookCme) {
    if (msg.has_quote()) {
      onTopBookQuote(msg.quote(), &it->second);
    } else if (msg.has_book_trade()) {
      onTopBookTrade(msg.book_trade(), &it->second);
    }

    // Ah, ok. I guess I'm not passing those messages.

  }

  else {
    LOG_EVERY_N(INFO, 101) << "book_manager: received unhandled mkt " << msg.ShortDebugString();
  }
}

/////////////////////////////////////////////////////////////////////////
// For snapshot updates.
void BookManager::onBinanceMDDiff(const mdmsg::PbBookUpdate& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  auto& last_update_id = data->book_update_last_update_id;

  data->last_msg_id_ = msg.last_update_id();

  // Snapshot
  /*
  if (!data->seen_first_snapshot) {
    // std::cout << " Returning depth diff bc did not get snapshot yet "
    //           << std::endl;
    return;
  }

  I'm removing this check right now because there are several instances
  where we do not have historical snapshots. I guess, we just let it go for a
  few diffs and then fill things in after that. It's not the craziest thing in
  the world.
  */

  // Maybe remove this again, one day, when we do have snapshots.
  if (!data->seen_first_snapshot) {
    data->diffs_seen_no_snapshot++;

    if (data->diffs_seen_no_snapshot > data->diffs_seen_before_force_snapshot) {
      data->seen_first_snapshot = true;
    }
  }

  if (last_update_id >= msg.last_update_id()) {
    return;
  }
  last_update_id = msg.last_update_id();

  auto& bids_container = msg.bids();
  auto* buy_side = &book->side<BuySide>();

  auto& asks_container = msg.asks();
  auto* sell_side = &book->side<SellSide>();

  for (auto bid_update : bids_container) {
    Quantity qty = Quantity{bid_update.qty()};
    Price px = Price{bid_update.px()};

    if (data->book_update_last_update_id < data->ticker_update_last_update_id) {
      if (px >= data->last_ticker_bb) {
        // std::cout << time_utils::nowToStr() << " Batch: maybe skip " <<
        // qty.toDouble() << "@ "<< px.toDouble() << " on bid bc updatedBid is "
        // << data->last_ticker_bb.toDouble() << std::endl;

        // Then this shouldn't apply. We are later than the last bookticker, and
        // have an older version of this price or it was implied to have been
        // removed.
        continue;
      }
    }

    if (!(qty == Quantity{0})) {
      auto [it, success] = buy_side->emplace(px, qty);
      if (success) {
        auto lvl_action = LevelAdd{
            .px = px,
            .side = Side::Buy,
            .qty = qty,
            .unflagged_qty = qty,
            .last_msg_type = LastMsgType::BookUpdate,
        };
        notify(*book, lvl_action, listeners);
      } else {
        // Let's make these all be the flagged qty, and not the full on
        // qty.

        if (qty != it->qty) {
          Quantity pre_shares = effectiveQty(*it);
          Quantity pre_unflagged_shares = it->qty;

          // Always unflag when we get new levl state.
          if (ALWAYS_UNFLAG || qty < it->qty) {
            unflagShares(*it, it->qty - qty);
          }
          // If the new qty is less, then unflag.
          it->qty = qty;
          Quantity post_shares = effectiveQty(*it);

          auto lvl_action = LevelModify{
              .px = px,
              .side = Side::Buy,
              .qty = post_shares,
              .unflagged_qty = it->qty,
              .prev_qty = pre_shares,
              .prev_unflagged_qty = pre_unflagged_shares,
              .last_msg_type = LastMsgType::BookUpdate,

          };

          notify(*book, lvl_action, listeners);
        } else if (qty == it->qty && it->flagged_shares.toDouble() > 0) {
          // We may need to unflag here...? The update has told us we have
          // nothing missing.
          unflagShares(*it, it->qty - qty);
        }
      }
    } else {
      // update.qty == 0
      auto it = buy_side->find(px);
      if (it != buy_side->end()) {
        auto lvl_action = LevelDelete{
            .px = px,
            .side = Side::Buy,
            .prev_qty = effectiveQty(*it),
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::BookUpdate,

        };
        buy_side->erase(it);

        // Don't call onDelete if we already effectively removed
        // that level due to flagging.
        if (lvl_action.prev_qty != Quantity{0}) {
          notify(*book, lvl_action, listeners);
        }
      }
    }
  }

  // UpdateAsks.

  for (auto ask_update : asks_container) {
    Quantity qty = Quantity{ask_update.qty()};
    Price px = Price{ask_update.px()};

    // Be careful here. If we are behind the insideTicker and the price is at or
    // more aggressive than the inside, then skip.
    if (data->book_update_last_update_id < data->ticker_update_last_update_id) {
      if (px <= data->last_ticker_ba) {
        // std::cout << time_utils::nowToStr() <<  " Batch: maybe skip " <<
        // qty.toDouble() << "@ " << px.toDouble() << " on ask bc updatedAsk is
        // " << data->last_ticker_ba.toDouble() << std::endl;
        continue;
      }
    }

    if (!(qty == Quantity{0})) {
      auto [it, success] = sell_side->emplace(px, qty);
      if (success) {
        auto lvl_action = LevelAdd{
            .px = px,
            .side = Side::Sell,
            .qty = qty,
            .unflagged_qty = qty,
            .last_msg_type = LastMsgType::BookUpdate,

        };
        notify(*book, lvl_action, listeners);
      } else {
        if (qty != it->qty) {
          // Let's make these all be the flagged qty, and not the full on
          // qty.
          Quantity pre_shares = effectiveQty(*it);
          Quantity pre_unflagged_shares = it->qty;
          if (ALWAYS_UNFLAG || qty < it->qty) {
            unflagShares(*it, it->qty - qty);
          }
          // If the new qty is less, then unflag.
          it->qty = qty;
          Quantity post_shares = effectiveQty(*it);

          auto lvl_action = LevelModify{
              .px = px,
              .side = Side::Sell,
              .qty = post_shares,
              .unflagged_qty = it->qty,
              .prev_qty = pre_shares,
              .prev_unflagged_qty = pre_unflagged_shares,
              .last_msg_type = LastMsgType::BookUpdate,

          };
          it->qty = qty;
          notify(*book, lvl_action, listeners);
        } else if (qty == it->qty && it->flagged_shares.toDouble() > 0) {
          // We may need to unflag here...? The update has told us we have
          // nothing missing.
          unflagShares(*it, it->qty - qty);
        }
      }
    } else {
      // update.qty == 0
      auto it = sell_side->find(px);
      if (it != sell_side->end()) {
        auto lvl_action = LevelDelete{
            .px = px,
            .side = Side::Sell,
            .prev_qty = effectiveQty(*it),
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::BookUpdate,

        };
        sell_side->erase(it);
        // Don't call onDelete if we already effectively removed
        // that level due to flagging.
        if (lvl_action.prev_qty != Quantity{0}) {
          notify(*book, lvl_action, listeners);
        }
      }
    }
  }

  // After all updates, notify on final.
  notifyFinal(*book, listeners);
}

// Update the inside of the book.
void BookManager::onBinanceMDBookTicker(const mdmsg::PbTickerUpdate& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  auto& last_update_id = data->ticker_update_last_update_id;

  data->last_msg_id_ = msg.update_id();

  // Don't apply updates if we don't know the book state yet.
  if (!data->seen_first_snapshot) {
    // std::cout << " Returning tickerupdate bc did not get snapshot yet "
    //           << std::endl;
    return;
  }

  if (last_update_id >= msg.update_id()) {
    // notifyFinal(*book, listeners);
    return;
  }

  data->last_ticker_event_t_ = msg.transact_time();
  last_update_id = msg.update_id();

  if (data->ticker_update_last_update_id < data->book_update_last_update_id) {
    /*

    std::cout << time_utils::nowToStr() << " " << time_utils::nowToMs()
              << " TickerUpdate: maybe skip it because "
              << data->ticker_update_last_update_id << " < "
              << data->book_update_last_update_id << std::endl;
  */
    // Don't process if we presumably already handled this in a depth snapshot.
    // notifyFinal(*book, listeners);

    // Let's not call onFinal if there's literally a no-op from this (stale)
    // data.
    return;
  }

  auto best_bid_px = Price{msg.best_bid_px()};
  auto best_bid_qty = Quantity{msg.best_bid_qty()};
  auto best_ask_px = Price{msg.best_ask_px()};
  auto best_ask_qty = Quantity{msg.best_ask_qty()};

  /*
      printf("Bro bookticker: bid (%s %s) ask (%s %s) t %ld\n",
     msg.best_bid_px().c_str(), msg.best_bid_qty().c_str(),
     msg.best_ask_px().c_str(), msg.best_ask_qty().c_str(),
     msg.transact_time());
*/
  // Update Bids
  auto* buy_side = &book->side<BuySide>();
  auto buy_lbound_it = buy_side->lower_bound(best_bid_px);

  // Delete all price levels more aggressive than px
  for (auto it2 = buy_side->begin(); it2 != buy_lbound_it;) {
    auto lvl_action = LevelDelete{
        .px = it2->px,
        .side = Side::Buy,
        .prev_qty = effectiveQty(*it2),
        .prev_unflagged_qty = it2->qty,
        .last_msg_type = LastMsgType::BookTicker,
        .transact_t = msg.transact_time(),
    };
    it2 = buy_side->erase(it2);
    // Don't call onDelete if we already effectively removed
    // that level due to flagging.
    if (lvl_action.prev_qty != Quantity{0}) {
      notify(*book, lvl_action, listeners);
    }
  }

  if (buy_lbound_it->px == best_bid_px) {
    // Don't levelModify if qty is the same

    Quantity pre_shares = effectiveQty(*buy_lbound_it);
    Quantity pre_unflagged_shares = buy_lbound_it->qty;

    // ALWAYS_UNFLAG means we unflag even if the next version of the book
    // might be more shares. But there might still be
    // good reason for us to not unflag.
    if (ALWAYS_UNFLAG) {
      // Unflag all the shares.
      if ((data->book->getMarket() == Market::BinanceFutures ||
           data->book->getMarket() == Market::BinanceCOINFutures)) {
        // Then we can check the transact time. If the
        // incoming message is more "current", then we use that
        // and unflag all shares.
        if (data->last_agg_sell_trd_t_ < msg.transact_time()) {
          unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, true);
        } else if (best_bid_qty < buy_lbound_it->qty) {
          // Otherwise, our trade came after the update, only unflag the
          // difference.
          unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, false);
        }

        // Make a call. If the trade-through is past this level, then
        // we need to flag the entire thing.
        if (data->last_agg_sell_trd_t_ >= msg.transact_time()) {
          if (data->last_agg_sell_trd_px < best_bid_px) {
            // Then we have to flag all its shares.
            flagShares(*buy_lbound_it, buy_lbound_it->qty);
          } else if (data->last_agg_sell_trd_px == best_bid_px) {
            // I'm not sure re: the right action to take here, but I don't
            // know if we *want* to do anything..
            // TODO: come back to this.
          }
        }

      } else {
        // Well. Let's actually decide if we want to unflag by
        // comparing the local time.
        if (data->book->getMarket() == Market::BinanceSPOT) {
          if (time_utils::nowToMs() - data->last_local_flag_t_ > data->unflag_t_buffer_) {
            unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, true);
          } else {
            // std::cout << " Decided to not unflag because of " <<
            // time_utils::nowToMs() - data->last_local_flag_t_  << " < " <<
            // data->unflag_t_buffer_ << std::endl;
          }
        }
        // If we're in a market without trade_times, I guess unflag everything.
      }
    } else if (best_bid_qty < buy_lbound_it->qty) {
      // Unflag the normal way, if we're not always_unflag.
      unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, false);
    }

    // Here. We should figure out if we were receiving something
    // old or something.

    buy_lbound_it->qty = best_bid_qty;
    Quantity post_shares = effectiveQty(*buy_lbound_it);

    if (pre_shares != post_shares) {
      auto lvl_action = LevelModify{
          .px = best_bid_px,
          .side = Side::Buy,
          .qty = post_shares,
          .unflagged_qty = buy_lbound_it->qty,
          .prev_qty = pre_shares,
          .prev_unflagged_qty = pre_unflagged_shares,
          .last_msg_type = LastMsgType::BookTicker,
          .transact_t = msg.transact_time(),

      };
      notify(*book, lvl_action, listeners);
    }

  } else {
    // New level needs to be inserted
    auto inserted_lvl = buy_side->emplace(best_bid_px, best_bid_qty);
    // Check if we should flag the whole thing.
    if (data->last_agg_sell_trd_t_ >= msg.transact_time()) {
      if (data->last_agg_sell_trd_px < best_bid_px) {
        // Then we have to flag all its shares.
        flagShares(*inserted_lvl.first, inserted_lvl.first->qty);
      }
    }

    Quantity add_qty = effectiveQty(*(inserted_lvl.first));

    if (add_qty != Quantity{0}) {
      auto lvl_action = LevelAdd{
          .px = best_bid_px,
          .side = Side::Buy,
          .qty = add_qty,
          .unflagged_qty = best_bid_qty,
          .last_msg_type = LastMsgType::BookTicker,
          .transact_t = msg.transact_time(),

      };
      notify(*book, lvl_action, listeners);
    }
  }

  // Update asks
  auto* sell_side = &book->side<SellSide>();
  auto sell_lbound_it = sell_side->lower_bound(best_ask_px);

  // Delete all price levels more aggressive than px
  for (auto it2 = sell_side->begin(); it2 != sell_lbound_it;) {
    auto lvl_action = LevelDelete{
        .px = it2->px,
        .side = Side::Sell,
        .prev_qty = effectiveQty(*it2),
        .prev_unflagged_qty = it2->qty,
        .transact_t = msg.transact_time(),

    };
    it2 = sell_side->erase(it2);
    // Don't call onDelete if we already effectively removed
    // that level due to flagging.
    if (lvl_action.prev_qty != Quantity{0}) {
      notify(*book, lvl_action, listeners);
    }
  }

  if (sell_lbound_it->px == best_ask_px) {
    // Don't levelModify if qty is the same

    Quantity pre_shares = effectiveQty(*sell_lbound_it);
    Quantity pre_unflagged_shares = sell_lbound_it->qty;

    if (ALWAYS_UNFLAG) {
      // Unflag all the shares.
      if ((data->book->getMarket() == Market::BinanceFutures ||
           data->book->getMarket() == Market::BinanceCOINFutures)) {
        // Then we can check the transact time. If the
        // incoming message is more "current", then we use that
        // and unflag all shares.
        if (data->last_agg_buy_trd_t_ < msg.transact_time()) {
          unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, true);
        } else if (best_ask_qty < sell_lbound_it->qty) {
          // Otherwise, our trade came after the update, only unflag the
          // difference.
          unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, false);
        }

        // Make a call. If the trade-through is past this level, then
        // we need to flag the entire thing.
        if (data->last_agg_buy_trd_t_ >= msg.transact_time()) {
          if (data->last_agg_buy_trd_px > best_ask_px) {
            // Then we have to flag all its shares.
            flagShares(*sell_lbound_it, sell_lbound_it->qty);
          } else if (data->last_agg_buy_trd_px == best_ask_px) {
            // I'm not sure re: the right action to take here, but I don't
            // know if we *want* to do anything..
            // TODO: come back to this.
          }
        }

      } else {
        // If we're in a market without trade_times, I guess unflag everything.
        unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, true);
      }
    } else if (best_ask_qty < sell_lbound_it->qty) {
      // Unflag the normal way, if we're not always_unflag.
      unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, false);
    }

    sell_lbound_it->qty = best_ask_qty;
    Quantity post_shares = effectiveQty(*sell_lbound_it);

    if (pre_shares != post_shares) {
      auto lvl_action = LevelModify{
          .px = best_ask_px,
          .side = Side::Sell,
          .qty = post_shares,
          .unflagged_qty = sell_lbound_it->qty,
          .prev_qty = pre_shares,
          .prev_unflagged_qty = pre_unflagged_shares,
          .last_msg_type = LastMsgType::BookTicker,
          .transact_t = msg.transact_time(),

      };
      notify(*book, lvl_action, listeners);
    }
  } else {
    // New level needs to be inserted
    auto inserted_lvl = sell_side->emplace(best_ask_px, best_ask_qty);

    // Check if we should flag the whole thing.
    if (data->last_agg_buy_trd_t_ >= msg.transact_time()) {
      if (data->last_agg_buy_trd_px > best_ask_px) {
        // Then we have to flag all its shares.
        flagShares(*inserted_lvl.first, inserted_lvl.first->qty);
      }
    }

    Quantity add_qty = effectiveQty(*(inserted_lvl.first));
    if (add_qty != Quantity{0}) {
      auto lvl_action = LevelAdd{
          .px = best_ask_px,
          .side = Side::Sell,
          .qty = add_qty,
          .unflagged_qty = best_ask_qty,
          .last_msg_type = LastMsgType::BookTicker,
          .transact_t = msg.transact_time(),

      };
      notify(*book, lvl_action, listeners);
    }
  }

  data->last_ticker_bb = best_bid_px;
  data->last_ticker_ba = best_ask_px;

  //  After all updates, notify on final.
  notifyFinal(*book, listeners);
}

void BookManager::onBinanceMDTrade(const mdmsg::PbTrade& msg, BookData* data) {
  Trade trd{
      .px = Price{msg.px()},
      .qty = Quantity{msg.qty()},
      .passive_side = msg.passive_was_buyer() ? Side::Buy : Side::Sell,
      .transact_t = msg.trade_time(),
  };
  // 20240927: Binance started publishing trades with px/sz 0. If that's the
  // case then we gotta not do anything.
  if (trd.qty.toDouble() == 0 || trd.px.toDouble() == 0) {
    return;
  }

  auto& book = data->book;
  auto& listeners = data->listeners;
  // data->last_trd_t_ = msg.trade_time();
  data->last_msg_id_ = msg.trade_id();
  data->last_local_flag_t_ = time_utils::nowToMs();

  // Always deliver trades. They aren't book-modifying.
  notify(*book, trd, listeners);

  // If we're older than the last trade on a side, don't do flagging (because we
  // likely are out of date).
  if (msg.passive_was_buyer() && data->last_agg_sell_trd_t_ > msg.trade_time()) {
    /*
printf(
    "Bro, last aggressive sell trade happened at %ld but we got a trade at "
    "%d at px %f, no longer flagging\n",
    data->last_agg_sell_trd_t_, msg.trade_time(), msg.px());
    */
    return;
  } else if (!msg.passive_was_buyer() && data->last_agg_buy_trd_t_ > msg.trade_time()) {
    /*
printf(
"Bro, last aggressive buy trade happened at %ld but we got a trade at "
"%d at px %f, no longer flagging\n",
data->last_agg_buy_trd_t_, msg.trade_time(), msg.px());
*/
    return;
  }

  // Don't flag if the trade hapens before the previous bookTicker
  // Otherwise, we'll probably flagging away a new best-book that we really
  // shouldn't be.
  // For safety, let's just onFinal and return here.
  // This is now replaced by the the price-and-time check below.
  /*
  if (msg.trade_time() < data->last_ticker_event_t_) {
    notifyFinal(*book, listeners);
    return;
  }
  */

  // Flag its level.
  if (msg.passive_was_buyer()) {
    // Update the bookdata's bookkeeping.
    data->last_agg_sell_trd_t_ = msg.trade_time();
    data->last_agg_sell_trd_px = msg.px();

    // Look on bids.
    auto* buy_side = &book->side<BuySide>();
    // OK, the right way to flag this is to actually walk the book.
    // Annoying as it is, but still. I found an instance where
    // there were trades that clearly traded through a level (and onto
    // the next level, but the sum of trade quantities we got
    // did not sum up to the quantity of the level. maybe this means
    // a message was dropped. It's possible.

    for (auto it = buy_side->begin(); it != buy_side->end(); ++it) {
      // This prevents us from flagging stuff that might have been introduced by
      // bookTickers that happened after the trade happeneed.
      if (it->px > trd.px && msg.trade_time() < data->last_ticker_event_t_) {
        continue;
      }

      if (it->px > trd.px) {
        // For orders before the trade, make sure it's fully flagged.
        // TODO: this might actually be more efficient to
        // check the toDouble(). Not sure.
        if (effectiveQty(*it) != Quantity{0}) {
          Quantity to_flag = effectiveQty(*it);
          /*
          std::cout << "BUY flagging " << to_flag.toDouble() << " at "
                    << it->px.toDouble()
                    << " bc of trade-through existingFlag" << it->flagged_shares
                           .toDouble()
                    << std::endl;
                    */
          flagShares(*it, to_flag);
          // This coming from a trade, the unflagged shares are the same.
          auto lvl_action = LevelModify{
              .px = it->px,
              .side = Side::Buy,
              .qty = Quantity{0},
              .unflagged_qty = it->qty,
              .prev_qty = to_flag,
              .prev_unflagged_qty = it->qty,
              .last_msg_type = LastMsgType::Trade,
              .transact_t = trd.transact_t,

          };
          notify(*book, lvl_action, listeners);
          // LOG(INFO) <<  time_utils::nowToMs() << " BUY Potentially
          // extraflagging at price "
          //           << it->px.toDouble() << " shares " << to_flag.toDouble()
          //           << " Due to trade at " << trd.px.toDouble() << std::endl;
        }

      } else if (it->px == trd.px) {
        // Flag the current level.
        Quantity pre_shares = effectiveQty(*it);
        flagShares(*it, trd.qty);
        Quantity post_shares = effectiveQty(*it);
        auto lvl_action = LevelModify{
            .px = trd.px,
            .side = Side::Buy,
            .qty = post_shares,
            .unflagged_qty = it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::Trade,
            .transact_t = trd.transact_t,

        };
        notify(*book, lvl_action, listeners);

        // LOG(INFO) <<  time_utils::nowToMs() << " BUY flagging at price " <<
        // it->px.toDouble()
        //           << " shares " << trd.qty.toDouble() << " result "
        //           << post_shares.toDouble() << std::endl;

        break;
      } else if (it->px < trd.px) {
        // If we're on the buy side and we iterated past
        // the trade price, break. There is nothing to do
        // on the book here.
        break;
      }
    }

  } else {
    // Update the bookdata's bookkeeping.
    data->last_agg_buy_trd_t_ = msg.trade_time();
    data->last_agg_buy_trd_px = msg.px();

    // Look on asks.
    auto* sell_side = &book->side<SellSide>();

    for (auto it = sell_side->begin(); it != sell_side->end(); ++it) {
      // This prevents us from flagging stuff that might have been introduced by
      // bookTickers that happened after the trade happeneed.
      if (it->px < trd.px && msg.trade_time() < data->last_ticker_event_t_) {
        continue;
      }

      if (it->px < trd.px) {
        // For orders before the trade, make sure it's fully flagged.
        // TODO: this might actually be more efficient to
        // check the toDouble(). Not sure.
        if (effectiveQty(*it) != Quantity{0}) {
          Quantity to_flag = effectiveQty(*it);
          /*
          std::cout << "SELL flagging " << to_flag.toDouble() << " at "
                    << it->px.toDouble()
                    << " bc of trade-through existingFlag" << it->flagged_shares
                           .toDouble()
                    << std::endl;
*/
          flagShares(*it, to_flag);
          auto lvl_action = LevelModify{
              .px = it->px,
              .side = Side::Sell,
              .qty = Quantity{0},
              .unflagged_qty = it->qty,

              .prev_qty = to_flag,
              .prev_unflagged_qty = it->qty,

              .last_msg_type = LastMsgType::Trade,
              .transact_t = trd.transact_t,

          };
          notify(*book, lvl_action, listeners);
          // LOG(INFO) << time_utils::nowToMs() << " SELL Potentially
          // extraflagging at price "
          //           << it->px.toDouble() << " shares " << to_flag.toDouble()
          //           << " Due to trade at " << trd.px.toDouble() << std::endl;
        }
      } else if (it->px == trd.px) {
        // Flag the current level.
        Quantity pre_shares = effectiveQty(*it);
        /*
        std::cout
            << "SELL flagging " << trd.qty.toDouble() << " at "
            << it->px.toDouble()
            << " bc of trade-at existingFlag" << it->flagged_shares.toDouble()
            << std::endl;
*/
        flagShares(*it, trd.qty);
        Quantity post_shares = effectiveQty(*it);
        auto lvl_action = LevelModify{
            .px = trd.px,
            .side = Side::Sell,
            .qty = post_shares,
            .unflagged_qty = it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::Trade,
            .transact_t = trd.transact_t,

        };
        notify(*book, lvl_action, listeners);
        // LOG(INFO) <<  time_utils::nowToMs() << " SELL flagging at price " <<
        // it->px.toDouble()
        //           << " shares " << trd.qty.toDouble() << " result "
        //           << post_shares.toDouble() << std::endl;
        break;
      } else if (it->px > trd.px) {
        // If we're on the buy side and we iterated past
        // the trade price, break. There is nothing to do
        // on the book here.
        break;
      }
    }
  }

  notifyFinal(*book, listeners);
}

void BookManager::onBinanceMDSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  auto& last_update_id = data->book_update_last_update_id;

  // Might actually want to just reapply the snapshot in this case.
  if (data->seen_first_snapshot) {
    // std::cout << " Returning from snapshot, because I already got a snapshot
    // "
    //           << std::endl;
    return;
  }

  last_update_id = msg.last_update_id();

  auto update_side = [&](auto& container, auto* book_side) {
    using ComparatorSide =
        typename std::remove_reference_t<decltype(*book_side)>::key_compare::Side;
    int lvl_count = 0;

    // Create a new book side from snapshot
    LevelBook::BookSide<ComparatorSide> new_book_side;
    for (auto& update : container) {
      auto qty = Quantity{update.qty()};
      auto px = Price{update.px()};
      new_book_side.emplace(px, qty);
    }

    auto it_old = book_side->begin();
    auto it_new = new_book_side.begin();

    // If first update, don't notify listeners.

    // While both books still levels, iterate through them ensuring that old_px
    // is not less aggressive than new_px. If old_px is not present in
    // new_book_side, delete it. If old_px is present and qty differ, modify the
    // level. Otherwise, if new_px is not present in old_book, add it.
    while (it_old != book_side->end() && it_new != new_book_side.end()) {
      if (lvl_count > data->max_levels) {
        //  break;
      }

      if ((ComparatorSide::side == Side::Buy && it_old->px > it_new->px) ||
          (ComparatorSide::side == Side::Sell && it_old->px < it_new->px)) {
        auto lvl_action = LevelDelete{
            .px = it_old->px,
            .side = ComparatorSide::side,
            .prev_qty = it_old->qty,
            .prev_unflagged_qty = it_old->qty,
        };
        it_old = book_side->erase(it_old);
        if (data->seen_first_snapshot) {
          notify(*book, lvl_action, listeners);
        }
      } else if (it_old->px == it_new->px) {
        if (it_old->qty != it_new->qty) {
          // TOOD: maybe we need to unflag here too.
          auto lvl_action = LevelModify{
              .px = it_old->px,
              .side = ComparatorSide::side,
              .qty = it_new->qty,
              .unflagged_qty = it_new->qty,

              .prev_qty = it_old->qty,
              .prev_unflagged_qty = it_old->qty,

          };
          it_old->qty = it_new->qty;

          if (data->seen_first_snapshot) {
            notify(*book, lvl_action, listeners);
          }
        }
        ++it_old;
        ++it_new;
        lvl_count++;
      } else if ((ComparatorSide::side == Side::Buy && it_old->px < it_new->px) ||
                 (ComparatorSide::side == Side::Sell && it_old->px > it_new->px)) {
        auto lvl_action = LevelAdd{.px = it_new->px,
                                   .side = ComparatorSide::side,
                                   .qty = it_new->qty,
                                   .unflagged_qty = it_new->qty};
        book_side->insert(*it_new);
        if (data->seen_first_snapshot) {
          notify(*book, lvl_action, listeners);
        }
        ++it_new;
        lvl_count++;
      }
    }

    // If there are any levels in old book left over, delete them.
    while (it_old != book_side->end()) {
      auto lvl_action = LevelDelete{
          .px = it_old->px,
          .side = ComparatorSide::side,
          .prev_qty = it_old->qty,
          .prev_unflagged_qty = it_old->qty,
      };
      it_old = book_side->erase(it_old);
      if (data->seen_first_snapshot) {
        notify(*book, lvl_action, listeners);
      }
    }

    // If there are any levels left over in new book, add them to old book.
    for (; it_new != new_book_side.end(); ++it_new) {
      if (lvl_count > data->max_levels) {
        // break;
      }
      auto lvl_action = LevelAdd{
          .px = it_new->px,
          .side = ComparatorSide::side,
          .qty = it_new->qty,
          .unflagged_qty = it_new->qty,
      };
      book_side->insert(*it_new);
      if (data->seen_first_snapshot) {
        // printf("Bro am notifying bro\n");
        notify(*book, lvl_action, listeners);
      }
      lvl_count++;
    }
  };

  update_side(msg.bids(), &book->side<BuySide>());
  update_side(msg.asks(), &book->side<SellSide>());

  // Maybe just check if I might have incurred an issue.

  /*
  if (data->book_update_last_update_id < data->ticker_update_last_update_id) {
    std::cout << " It's possible I overwrote something crucial" << std::endl;
  }

  */

  notifyFinal(*book, listeners);
  data->seen_first_snapshot = true;
  // Get the min px diff.
  /*
  printf(
      "After the snapshot, min_px_diff is %f, and book sides are size %d %d\n",
      min_pxdiff, (int)book->side<BuySide>().size(),
      (int)book->side<SellSide>().size());
*/
  LOG(INFO) << " BookManager: received snapshot for "
            << pktrade::util::print_bookid(data->book->getBookID()) << std::endl;
}

/////////////////////////////////////////////////////////////////////////
// Hyperliquid

// This is not supported right now.
// Because the hyperliquid feed does not give us diffs, but just
// a series of snapshots instead.
void BookManager::onHyperliquidMDDiff(const mdmsg::PbBookUpdate& msg, BookData* data) {
  throw std::runtime_error("HyperliquidMDDiff not supported right now!");
}

// Hyperliquid has the same thing for snapshots and l2 updates.
// Let'
void BookManager::onHyperliquidMDSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  auto& last_update_id = data->book_update_last_update_id;

  // Might actually want to just reapply the snapshot in this case.
  auto update_side = [&](auto& container, auto* book_side) {
    using ComparatorSide =
        typename std::remove_reference_t<decltype(*book_side)>::key_compare::Side;

    int lvl_count = 0;

    // Create a new book side from snapshot
    LevelBook::BookSide<ComparatorSide> new_book_side;
    for (auto& update : container) {
      auto qty = Quantity{update.qty()};
      auto px = Price{update.px()};
      new_book_side.emplace(px, qty);
    }

    auto it_old = book_side->begin();
    auto it_new = new_book_side.begin();

    // For below: if first update, don't notify listeners.

    // While both books still levels, iterate through them ensuring that old_px
    // is not less aggressive than new_px. If old_px is not present in
    // new_book_side, delete it. If old_px is present and qty differ, modify the
    // level. Otherwise, if new_px is not present in old_book, add it.
    while (it_old != book_side->end() && it_new != new_book_side.end()) {
      if ((ComparatorSide::side == Side::Buy && it_old->px > it_new->px) ||
          (ComparatorSide::side == Side::Sell && it_old->px < it_new->px)) {
        auto lvl_action = LevelDelete{
            .px = it_old->px,
            .side = ComparatorSide::side,
            .prev_qty = it_old->qty,
            .prev_unflagged_qty = it_old->qty,
        };
        it_old = book_side->erase(it_old);
        if (data->seen_first_snapshot) {
          notify(*book, lvl_action, listeners);
        }
      } else if (it_old->px == it_new->px) {
        // Unflag all the shares on the new one.
        // The annoying thing is, the old one is what actually gets saved,
        // which is why we're calling unflagShares on it_old. Sorry for
        // confusing naming.
        unflagShares(*it_old, Quantity{0}, true);

        if (it_old->qty != it_new->qty) {
          // TOOD: maybe we need to unflag here too.
          auto lvl_action = LevelModify{
              .px = it_old->px,
              .side = ComparatorSide::side,
              .qty = it_new->qty,
              .unflagged_qty = it_new->qty,

              .prev_qty = it_old->qty,
              .prev_unflagged_qty = it_old->qty,

          };
          it_old->qty = it_new->qty;

          if (data->seen_first_snapshot) {
            notify(*book, lvl_action, listeners);
          }
        }
        ++it_old;
        ++it_new;
        lvl_count++;
      } else if ((ComparatorSide::side == Side::Buy && it_old->px < it_new->px) ||
                 (ComparatorSide::side == Side::Sell && it_old->px > it_new->px)) {
        auto lvl_action = LevelAdd{.px = it_new->px,
                                   .side = ComparatorSide::side,
                                   .qty = it_new->qty,
                                   .unflagged_qty = it_new->qty};
        book_side->insert(*it_new);
        if (data->seen_first_snapshot) {
          notify(*book, lvl_action, listeners);
        }
        ++it_new;
        lvl_count++;
      }
    }

    // Hyperliquid snapshots are depth-bounded, not whole-book complete.
    // Node publishes deeper snapshots than public WS; when arbitration moves
    // from node to WS, keep the older deeper tail instead of erasing levels
    // beyond the shallow WS snapshot's last price.

    // If there are any levels left over in new book, add them to old book.
    for (; it_new != new_book_side.end(); ++it_new) {
      if (lvl_count > data->max_levels) {
        // break;
      }
      auto lvl_action = LevelAdd{
          .px = it_new->px,
          .side = ComparatorSide::side,
          .qty = it_new->qty,
          .unflagged_qty = it_new->qty,
      };
      book_side->insert(*it_new);
      if (data->seen_first_snapshot) {
        // printf("Bro am notifying bro\n");
        notify(*book, lvl_action, listeners);
      }
      lvl_count++;
    }
  };

  update_side(msg.bids(), &book->side<BuySide>());
  update_side(msg.asks(), &book->side<SellSide>());

  notifyFinal(*book, listeners);
  data->seen_first_snapshot = true;
  // LOG_EVERY_N(INFO, 1000) << "book_manager: received Hyperliquid Snapshot"
  //                         << msg.ShortDebugString();

  // Get the min px diff.

  // LOG(INFO) << " BookManager: received snapshot for "
  //           << pktrade::util::print_bookid(data->book->getBookID())
  //           << std::endl;
}

// TODO: convert
void BookManager::onHyperliquidMDTrade(const mdmsg::PbTrade& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  // Always deliver trades. They aren't book-modifying.

  // TODO: at some point might want to check if we can
  // shave though. Also, some state to deal w/ w.r.t. aggtrades.
  Trade trd{
      .px = Price{msg.px()},
      .qty = Quantity{msg.qty()},
      .passive_side = msg.passive_was_buyer() ? Side::Buy : Side::Sell,
      .transact_t = msg.trade_time(),
  };
  notify(*book, trd, listeners);

  // Maybe let's do some flagging...?
  // I'm going to disable any of the more "advanced" flagging things in here.

  // Flag its level.
  if (msg.passive_was_buyer()) {
    // Update the bookdata's bookkeeping.

    // Look on bids.
    auto* buy_side = &book->side<BuySide>();
    for (auto it = buy_side->begin(); it != buy_side->end(); ++it) {
      // This prevents us from flagging stuff that might have been introduced by
      // bookTickers that happened after the trade happeneed.

      if (it->px > trd.px) {
        // For orders before the trade, make sure it's fully flagged.
        // TODO: this might actually be more efficient to
        // check the toDouble(). Not sure.
        if (effectiveQty(*it) != Quantity{0}) {
          Quantity to_flag = effectiveQty(*it);
          flagShares(*it, to_flag);
          // This coming from a trade, the unflagged shares are the same.
          auto lvl_action = LevelModify{
              .px = it->px,
              .side = Side::Buy,
              .qty = Quantity{0},
              .unflagged_qty = it->qty,
              .prev_qty = to_flag,
              .prev_unflagged_qty = it->qty,
              .last_msg_type = LastMsgType::Trade,
              .transact_t = trd.transact_t,

          };
          notify(*book, lvl_action, listeners);
        }

      } else if (it->px == trd.px) {
        // Flag the current level.
        Quantity pre_shares = effectiveQty(*it);
        flagShares(*it, trd.qty);
        Quantity post_shares = effectiveQty(*it);
        auto lvl_action = LevelModify{
            .px = trd.px,
            .side = Side::Buy,
            .qty = post_shares,
            .unflagged_qty = it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::Trade,
            .transact_t = trd.transact_t,

        };
        notify(*book, lvl_action, listeners);

        break;
      } else if (it->px < trd.px) {
        // If we're on the buy side and we iterated past
        // the trade price, break. There is nothing to do
        // on the book here.
        break;
      }
    }

  } else {
    // Update the bookdata's bookkeeping.

    // Look on asks.
    auto* sell_side = &book->side<SellSide>();

    for (auto it = sell_side->begin(); it != sell_side->end(); ++it) {
      // This prevents us from flagging stuff that might have been introduced by
      // bookTickers that happened after the trade happeneed.

      if (it->px < trd.px) {
        // For orders before the trade, make sure it's fully flagged.
        // TODO: this might actually be more efficient to
        // check the toDouble(). Not sure.
        if (effectiveQty(*it) != Quantity{0}) {
          Quantity to_flag = effectiveQty(*it);
          flagShares(*it, to_flag);
          auto lvl_action = LevelModify{
              .px = it->px,
              .side = Side::Sell,
              .qty = Quantity{0},
              .unflagged_qty = it->qty,

              .prev_qty = to_flag,
              .prev_unflagged_qty = it->qty,

              .last_msg_type = LastMsgType::Trade,
              .transact_t = trd.transact_t,

          };
          notify(*book, lvl_action, listeners);
          // LOG(INFO) << time_utils::nowToMs() << " SELL Potentially
          // extraflagging at price "
          //           << it->px.toDouble() << " shares " << to_flag.toDouble()
          //           << " Due to trade at " << trd.px.toDouble() << std::endl;
        }
      } else if (it->px == trd.px) {
        // Flag the current level.
        Quantity pre_shares = effectiveQty(*it);
        flagShares(*it, trd.qty);
        Quantity post_shares = effectiveQty(*it);
        auto lvl_action = LevelModify{
            .px = trd.px,
            .side = Side::Sell,
            .qty = post_shares,
            .unflagged_qty = it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::Trade,
            .transact_t = trd.transact_t,

        };
        notify(*book, lvl_action, listeners);
        // LOG(INFO) <<  time_utils::nowToMs() << " SELL flagging at price " <<
        // it->px.toDouble()
        //           << " shares " << trd.qty.toDouble() << " result "
        //           << post_shares.toDouble() << std::endl;
        break;
      } else if (it->px > trd.px) {
        // If we're on the buy side and we iterated past
        // the trade price, break. There is nothing to do
        // on the book here.
        break;
      }
    }
  }
  // Don't flag.
  notifyFinal(*book, listeners);

  // LOG_EVERY_N(INFO, 1000) << "book_manager: received Hyperliquid Trade"
  //                        << msg.ShortDebugString();
}

// TODO: perhaps update transact times for flagging purposes.
// Not sure if that was done yet.
void BookManager::onBybitMDDiff(const mdmsg::PbBookUpdate& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;

  // It could be 1, 50, 200, etc.
  int update_depth = msg.depth();

  // If we look through all the previously modified
  double highest_touchable_bid = 1e20;
  double lowest_touchable_ask = 0;

  // Look through the seen updateids and check where I might fit in.
  for (auto& depth_to_uid : data->depth_to_last_update_id_) {
    if (depth_to_uid.second >= msg.last_update_id()) {
      // Then set bounds via the depth.
      // n.b. there are potential issues here, but probably not so big in the
      // grand scheme of things.

      // If top20 levels showed up with a later updateid than the current
      // message and the last bid was px 100, then we don't want to look at any
      // bid updates at 100 or higher for the current message. Little poorly
      // named. We CAN'T touch highest_touchable_bid, the highest we can touch
      // is that -1 tick. Just don't know how to name it well tho.

      highest_touchable_bid =
          std::min(highest_touchable_bid, data->depth_to_last_price_bids_[depth_to_uid.first]);

      lowest_touchable_ask =
          std::max(lowest_touchable_ask, data->depth_to_last_price_asks_[depth_to_uid.first]);
    }
  }

  data->last_msg_id_ = msg.last_update_id();

  if (!data->seen_first_snapshot) {
    // Do not handle a diff if there is no snapshot to base off of.
    return;
  }

  if (data->depth_to_last_update_id_[update_depth] >= msg.last_update_id()) {
    // Got an old message (from this channel), so return.
    return;
  }

  data->last_book_update_t_ = msg.event_time();

  // Update specs re: this book update.
  data->depth_to_last_update_id_[update_depth] = msg.last_update_id();

  if (msg.bids().size() > 0) {
    data->depth_to_last_price_bids_[update_depth] = std::stof(msg.bids().rbegin()->px());
  }
  // Otherwise... nothing? There is still an issue that can occur here. But
  // maybe it's not so bad? an alternative is to almost zero this out.

  if (msg.asks().size() > 0) {
    data->depth_to_last_price_asks_[update_depth] = std::stof(msg.asks().rbegin()->px());
  }
  // Otherwise... nothing? There is still an issue that can occur here. But
  // maybe it's not so bad?

  auto& bids_container = msg.bids();
  auto* buy_side = &book->side<BuySide>();

  auto& asks_container = msg.asks();
  auto* sell_side = &book->side<SellSide>();

  // For first bid/asks, we might need to flag the other side. Annoyingly
  // enough.
  bool first_bid = true;
  bool first_ask = true;

  for (auto bid_update : bids_container) {
    Quantity qty = Quantity{bid_update.qty()};
    Price px = Price{bid_update.px()};

    // Like stated earlier, do not update bids when we are not the most
    // up-to-date.
    if (px.toDouble() >= highest_touchable_bid) {
      continue;
    }

    // TODO:
    // I haven't looked closely enough at bybit data. But there may be instances
    // where we have similar timing shenanigans as in binance, and I'll have to
    // deal with timing discrepencies between the feeds.
    // For example, if it is such that a huge trade-through happens and we
    // process an add-level, or unflag event, then it's pretty poor.

    if (!(qty == Quantity{0})) {
      auto [it, success] = buy_side->emplace(px, qty);
      if (success) {
        // Check if we need to flag the other side.
        if (first_bid && px >= sell_side->begin()->px) {
          // Go through and flag the other side.
          for (auto ask_it = sell_side->begin(); ask_it != sell_side->end(); ++ask_it) {
            if (ask_it->px <= px) {
              Quantity prev_shs = effectiveQty(*ask_it);

              // Then, flag the other side.
              flagShares(*ask_it, prev_shs);

              // Reflect the flagging action.
              auto lvl_action = LevelModify{
                  .px = ask_it->px,
                  .side = Side::Sell,
                  .qty = Quantity{0},
                  .unflagged_qty = ask_it->qty,
                  .prev_qty = prev_shs,
                  .prev_unflagged_qty = ask_it->qty,
                  .last_msg_type = LastMsgType::BookUpdate,
              };
              notify(*book, lvl_action, listeners);

            } else {
              break;
            }
          }
        }

        auto lvl_action = LevelAdd{
            .px = px,
            .side = Side::Buy,
            .qty = qty,
            .unflagged_qty = qty,
            .last_msg_type = LastMsgType::BookUpdate,
            .transact_t = msg.event_time(),
        };
        notify(*book, lvl_action, listeners);
      } else {
        // Let's make these all be the flagged qty, and not the full on
        // qty.

        if (qty != it->qty) {
          // Check if we should be unflagging.

          // We are on the
          if (msg.event_time() < data->last_agg_sell_trd_t_ && px >= data->last_agg_sell_trd_px) {
            /*
        std::cout << " Skipping unflag because we're probably out of date"
                  << std::endl;
        std::cout << px.toDouble() << " vs "
                  << data->last_agg_sell_trd_px.toDouble() << std::endl;
                  */
            // Then, probably don't unflag here. Because we are more out of date
            // than the flagging action that led us here.
            continue;
          } else {
            Quantity pre_shares = effectiveQty(*it);
            Quantity pre_unflagged_shares = it->qty;

            // Always unflag when we get new levl state.
            if (ALWAYS_UNFLAG || qty < it->qty) {
              unflagShares(*it, it->qty - qty);
            }
            // If the new qty is less, then unflag.
            it->qty = qty;
            Quantity post_shares = effectiveQty(*it);

            auto lvl_action = LevelModify{
                .px = px,
                .side = Side::Buy,
                .qty = post_shares,
                .unflagged_qty = it->qty,
                .prev_qty = pre_shares,
                .prev_unflagged_qty = pre_unflagged_shares,
                .last_msg_type = LastMsgType::BookUpdate,
                .transact_t = msg.event_time(),

            };

            notify(*book, lvl_action, listeners);
          }
        }
      }
    } else {
      // update.qty == 0
      auto it = buy_side->find(px);
      if (it != buy_side->end()) {
        auto lvl_action = LevelDelete{
            .px = px,
            .side = Side::Buy,
            .prev_qty = effectiveQty(*it),
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::BookUpdate,
            .transact_t = msg.event_time(),

        };
        buy_side->erase(it);

        // Don't call onDelete if we already effectively removed
        // that level due to flagging.
        if (lvl_action.prev_qty != Quantity{0}) {
          notify(*book, lvl_action, listeners);
        }
      }
    }
    first_bid = false;
  }

  // UpdateAsks.

  for (auto ask_update : asks_container) {
    Quantity qty = Quantity{ask_update.qty()};
    Price px = Price{ask_update.px()};

    if (px.toDouble() <= lowest_touchable_ask) {
      continue;
    }

    if (!(qty == Quantity{0})) {
      auto [it, success] = sell_side->emplace(px, qty);
      if (success) {
        // Check if we need to flag the other side.
        if (first_ask && px <= buy_side->begin()->px) {
          // Go through and flag the other side.
          for (auto bid_it = buy_side->begin(); bid_it != buy_side->end(); ++bid_it) {
            if (bid_it->px >= px) {
              // Then, flag the other side.

              Quantity prev_shs = effectiveQty(*bid_it);
              flagShares(*bid_it, bid_it->qty);

              // Reflect the flagging action.
              auto lvl_action = LevelModify{
                  .px = bid_it->px,
                  .side = Side::Buy,
                  .qty = Quantity{0},
                  .unflagged_qty = bid_it->qty,
                  .prev_qty = prev_shs,
                  .prev_unflagged_qty = bid_it->qty,
                  .last_msg_type = LastMsgType::BookUpdate,
              };

              notify(*book, lvl_action, listeners);

            } else {
              break;
            }
          }
        }

        auto lvl_action = LevelAdd{
            .px = px,
            .side = Side::Sell,
            .qty = qty,
            .unflagged_qty = qty,
            .last_msg_type = LastMsgType::BookUpdate,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      } else {
        if (qty != it->qty) {
          if (msg.event_time() < data->last_agg_buy_trd_t_ && px <= data->last_agg_buy_trd_px) {
            // Then, probably don't unflag here. Because we are more out of date
            // than the flagging action that led us here.
            /*
            std::cout << " Skipping unflag because we're probably out of date"
                      << std::endl;
                      */
            continue;
          } else {
            // Let's make these all be the flagged qty, and not the full on
            // qty.
            Quantity pre_shares = effectiveQty(*it);
            Quantity pre_unflagged_shares = it->qty;
            if (ALWAYS_UNFLAG || qty < it->qty) {
              unflagShares(*it, it->qty - qty);
            }
            // If the new qty is less, then unflag.
            it->qty = qty;
            Quantity post_shares = effectiveQty(*it);

            auto lvl_action = LevelModify{
                .px = px,
                .side = Side::Sell,
                .qty = post_shares,
                .unflagged_qty = it->qty,
                .prev_qty = pre_shares,
                .prev_unflagged_qty = pre_unflagged_shares,
                .last_msg_type = LastMsgType::BookUpdate,
                .transact_t = msg.event_time(),

            };
            it->qty = qty;
            notify(*book, lvl_action, listeners);
          }
        }
      }
    } else {
      // update.qty == 0
      auto it = sell_side->find(px);
      if (it != sell_side->end()) {
        auto lvl_action = LevelDelete{
            .px = px,
            .side = Side::Sell,
            .prev_qty = effectiveQty(*it),
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::BookUpdate,
            .transact_t = msg.event_time(),

        };
        sell_side->erase(it);
        // Don't call onDelete if we already effectively removed
        // that level due to flagging.
        if (lvl_action.prev_qty != Quantity{0}) {
          notify(*book, lvl_action, listeners);
        }
      }
    }
    first_ask = false;
  }

  // After all updates, notify on final.
  notifyFinal(*book, listeners);
}

void BookManager::onBybitMDTrade(const mdmsg::PbTrade& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  // data->last_trd_t_ = msg.trade_time();
  data->last_msg_id_ = msg.trade_id();
  data->last_local_flag_t_ = time_utils::nowToMs();

  // Always deliver trades. They aren't book-modifying.
  Trade trd{
      .px = Price{msg.px()},
      .qty = Quantity{msg.qty()},
      .passive_side = msg.passive_was_buyer() ? Side::Buy : Side::Sell,
      .transact_t = msg.trade_time(),
  };
  notify(*book, trd, listeners);

  // Don't flag if the trade happens before the previous book-update.
  if (msg.trade_time() < data->last_book_update_t_ ||
      msg.trade_time() < data->last_ticker_event_t_) {
    notifyFinal(*book, listeners);
    return;
  }

  // Flag its level.
  if (msg.passive_was_buyer()) {
    // Update the bookdata's bookkeeping.
    data->last_agg_sell_trd_t_ = msg.trade_time();
    data->last_agg_sell_trd_px = msg.px();

    // Look on bids.
    auto* buy_side = &book->side<BuySide>();
    // OK, the right way to flag this is to actually walk the book.
    // Annoying as it is, but still. I found an instance where
    // there were trades that clearly traded through a level (and onto
    // the next level, but the sum of trade quantities we got
    // did not sum up to the quantity of the level. maybe this means
    // a message was dropped. It's possible.

    for (auto it = buy_side->begin(); it != buy_side->end(); ++it) {
      if (it->px > trd.px) {
        // For orders before the trade, make sure it's fully flagged.
        // TODO: this might actually be more efficient to
        // check the toDouble(). Not sure.
        if (effectiveQty(*it) != Quantity{0}) {
          Quantity to_flag = effectiveQty(*it);

          flagShares(*it, to_flag);
          // This coming from a trade, the unflagged shares are the same.
          auto lvl_action = LevelModify{
              .px = it->px,
              .side = Side::Buy,
              .qty = Quantity{0},
              .unflagged_qty = it->qty,
              .prev_qty = to_flag,
              .prev_unflagged_qty = it->qty,
              .last_msg_type = LastMsgType::Trade,
              .transact_t = trd.transact_t,
          };
          notify(*book, lvl_action, listeners);
        }

      } else if (it->px == trd.px) {
        // Flag the current level.
        Quantity pre_shares = effectiveQty(*it);
        /*
        std::cout
            << "BUY flagging " << trd.qty.toDouble() << " at "
            << it->px.toDouble()
            << " bc of trade-at existingFlag" << it->flagged_shares.toDouble()
            << std::endl;
            */
        flagShares(*it, trd.qty);
        Quantity post_shares = effectiveQty(*it);
        auto lvl_action = LevelModify{
            .px = trd.px,
            .side = Side::Buy,
            .qty = post_shares,
            .unflagged_qty = it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::Trade,
            .transact_t = trd.transact_t,

        };
        notify(*book, lvl_action, listeners);

        break;
      } else if (it->px < trd.px) {
        // If we're on the buy side and we iterated past
        // the trade price, break. There is nothing to do
        // on the book here.
        break;
      }
    }

  } else {
    // Update the bookdata's bookkeeping.
    data->last_agg_buy_trd_t_ = msg.trade_time();
    data->last_agg_buy_trd_px = msg.px();

    // Look on asks.
    auto* sell_side = &book->side<SellSide>();

    for (auto it = sell_side->begin(); it != sell_side->end(); ++it) {
      if (it->px < trd.px) {
        // For orders before the trade, make sure it's fully flagged.
        // TODO: this might actually be more efficient to
        // check the toDouble(). Not sure.
        if (effectiveQty(*it) != Quantity{0}) {
          Quantity to_flag = effectiveQty(*it);

          flagShares(*it, to_flag);
          auto lvl_action = LevelModify{
              .px = it->px,
              .side = Side::Sell,
              .qty = Quantity{0},
              .unflagged_qty = it->qty,

              .prev_qty = to_flag,
              .prev_unflagged_qty = it->qty,

              .last_msg_type = LastMsgType::Trade,
              .transact_t = trd.transact_t,

          };
          notify(*book, lvl_action, listeners);
        }
      } else if (it->px == trd.px) {
        // Flag the current level.
        Quantity pre_shares = effectiveQty(*it);
        /*
        std::cout
            << "SELL flagging " << trd.qty.toDouble() << " at "
            << it->px.toDouble()
            << " bc of trade-at existingFlag" << it->flagged_shares.toDouble()
            << std::endl;
*/
        flagShares(*it, trd.qty);
        Quantity post_shares = effectiveQty(*it);
        auto lvl_action = LevelModify{
            .px = trd.px,
            .side = Side::Sell,
            .qty = post_shares,
            .unflagged_qty = it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = it->qty,
            .last_msg_type = LastMsgType::Trade,
            .transact_t = trd.transact_t,

        };
        notify(*book, lvl_action, listeners);

        break;
      } else if (it->px > trd.px) {
        // If we're on the buy side and we iterated past
        // the trade price, break. There is nothing to do
        // on the book here.
        break;
      }
    }
  }

  notifyFinal(*book, listeners);
}

// Update the inside of the book.
// Actually, I guess, I think we could use this for both snapshots and diff
// updates.
// One thing. There are instances when a level goes away, but we get a diff
// update, that we actually get a "0" for the side going away. I should probably
// just address this by finding the first level that isn't zero, and then
// treating it like a bookTicker. Easier that way.
void BookManager::onBybitMDInsideDiff(const mdmsg::PbBookUpdate& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  auto& last_update_id = data->book_update_last_update_id;

  data->last_msg_id_ = msg.last_update_id();

  // snapshot_depth should be 1.

  if (last_update_id >= msg.last_update_id()) {
    // notifyFinal(*book, listeners);
    return;
  }

  // Used for flagging purposes.
  data->last_ticker_event_t_ = msg.event_time();
  last_update_id = msg.last_update_id();

  // Update the last_update_id too, for this side.
  data->depth_to_last_update_id_[1] = msg.last_update_id();

  auto* buy_side = &book->side<BuySide>();
  auto* sell_side = &book->side<SellSide>();

  // Handle bids side.
  // Possible that bids are empty. We can see a one-sided book too.
  if (msg.bids().size() > 0) {
    Price best_bid_px = Price{msg.bids()[msg.bids().size() - 1].px()};
    Quantity best_bid_qty = Quantity{msg.bids()[msg.bids().size() - 1].qty()};

    // Update Bids
    auto buy_lbound_it = buy_side->lower_bound(best_bid_px);

    // Delete all price levels more aggressive than px
    for (auto it2 = buy_side->begin(); it2 != buy_lbound_it;) {
      auto lvl_action = LevelDelete{
          .px = it2->px,
          .side = Side::Buy,
          .prev_qty = effectiveQty(*it2),
          .prev_unflagged_qty = it2->qty,
          .last_msg_type = LastMsgType::BookTicker,
          .transact_t = msg.event_time(),
      };
      it2 = buy_side->erase(it2);
      // Don't call onDelete if we already effectively removed
      // that level due to flagging.
      if (lvl_action.prev_qty != Quantity{0}) {
        notify(*book, lvl_action, listeners);
      }
    }
    if (buy_lbound_it->px == best_bid_px) {
      // Don't levelModify if qty is the same

      Quantity pre_shares = effectiveQty(*buy_lbound_it);
      Quantity pre_unflagged_shares = buy_lbound_it->qty;

      // ALWAYS_UNFLAG means we unflag even if the next version of the book
      // might be more shares. But there might still be
      // good reason for us to not unflag.
      if (ALWAYS_UNFLAG) {
        // Unflag all the shares.

        // Then we can check the transact time. If the
        // incoming message is more "current", then we use that
        // and unflag all shares.
        if (data->last_agg_sell_trd_t_ < msg.event_time()) {
          unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, true);
        } else if (best_bid_qty < buy_lbound_it->qty) {
          // Otherwise, our trade came after the update, only unflag the
          // difference.
          unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, false);
        }

        // Make a call. If the trade-through is past this level, then
        // we need to flag the entire thing.
        if (data->last_agg_sell_trd_t_ >= msg.event_time()) {
          if (data->last_agg_sell_trd_px < best_bid_px) {
            // Then we have to flag all its shares.
            flagShares(*buy_lbound_it, buy_lbound_it->qty);
          } else if (data->last_agg_sell_trd_px == best_bid_px) {
            // I'm not sure re: the right action to take here, but I don't
            // know if we *want* to do anything..
            // TODO: come back to this.
          }
        }

      } else if (best_bid_qty < buy_lbound_it->qty) {
        // Unflag the normal way, if we're not always_unflag.
        unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, false);
      }

      // Here. We should figure out if we were receiving something
      // old or something.

      buy_lbound_it->qty = best_bid_qty;
      Quantity post_shares = effectiveQty(*buy_lbound_it);

      if (pre_shares != post_shares) {
        auto lvl_action = LevelModify{
            .px = best_bid_px,
            .side = Side::Buy,
            .qty = post_shares,
            .unflagged_qty = buy_lbound_it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = pre_unflagged_shares,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }

    } else {
      // New level needs to be inserted
      auto inserted_lvl = buy_side->emplace(best_bid_px, best_bid_qty);
      // Check if we should flag the whole thing.
      if (data->last_agg_sell_trd_t_ >= msg.event_time()) {
        if (data->last_agg_sell_trd_px < best_bid_px) {
          // Then we have to flag all its shares.
          flagShares(*inserted_lvl.first, inserted_lvl.first->qty);
        }
      }

      Quantity add_qty = effectiveQty(*(inserted_lvl.first));

      if (add_qty != Quantity{0}) {
        // Flag the other side, potentially.

        if (best_bid_px >= sell_side->begin()->px) {
          // Go through and flag the other side.
          for (auto ask_it = sell_side->begin(); ask_it != sell_side->end(); ++ask_it) {
            if (ask_it->px <= best_bid_px) {
              Quantity prev_shs = effectiveQty(*ask_it);

              // Then, flag the other side.
              flagShares(*ask_it, prev_shs);

              // Reflect the flagging action.
              auto lvl_action = LevelModify{
                  .px = ask_it->px,
                  .side = Side::Sell,
                  .qty = Quantity{0},
                  .unflagged_qty = ask_it->qty,
                  .prev_qty = prev_shs,
                  .prev_unflagged_qty = ask_it->qty,
                  .last_msg_type = LastMsgType::BookUpdate,
              };
              notify(*book, lvl_action, listeners);

            } else {
              break;
            }
          }
        }

        auto lvl_action = LevelAdd{
            .px = best_bid_px,
            .side = Side::Buy,
            .qty = add_qty,
            .unflagged_qty = best_bid_qty,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }
    }
    data->last_ticker_bb = best_bid_px;
  }

  if (msg.asks().size() > 0) {
    Price best_ask_px = Price{msg.asks()[msg.asks().size() - 1].px()};
    Quantity best_ask_qty = Quantity{msg.asks()[msg.asks().size() - 1].qty()};

    // Update asks
    auto sell_lbound_it = sell_side->lower_bound(best_ask_px);

    // Delete all price levels more aggressive than px
    for (auto it2 = sell_side->begin(); it2 != sell_lbound_it;) {
      auto lvl_action = LevelDelete{
          .px = it2->px,
          .side = Side::Sell,
          .prev_qty = effectiveQty(*it2),
          .prev_unflagged_qty = it2->qty,
          .transact_t = msg.event_time(),

      };
      it2 = sell_side->erase(it2);
      // Don't call onDelete if we already effectively removed
      // that level due to flagging.
      if (lvl_action.prev_qty != Quantity{0}) {
        notify(*book, lvl_action, listeners);
      }
    }

    if (sell_lbound_it->px == best_ask_px) {
      // Don't levelModify if qty is the same

      Quantity pre_shares = effectiveQty(*sell_lbound_it);
      Quantity pre_unflagged_shares = sell_lbound_it->qty;

      if (ALWAYS_UNFLAG) {
        // Unflag all the shares.

        // Then we can check the transact time. If the
        // incoming message is more "current", then we use that
        // and unflag all shares.
        if (data->last_agg_buy_trd_t_ < msg.event_time()) {
          unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, true);
        } else if (best_ask_qty < sell_lbound_it->qty) {
          // Otherwise, our trade came after the update, only unflag the
          // difference.
          unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, false);
        }

        // Make a call. If the trade-through is past this level, then
        // we need to flag the entire thing.
        if (data->last_agg_buy_trd_t_ >= msg.event_time()) {
          if (data->last_agg_buy_trd_px > best_ask_px) {
            // Then we have to flag all its shares.
            flagShares(*sell_lbound_it, sell_lbound_it->qty);
          } else if (data->last_agg_buy_trd_px == best_ask_px) {
            // I'm not sure re: the right action to take here, but I don't
            // know if we *want* to do anything..
            // TODO: come back to this.
          }
        }

      } else if (best_ask_qty < sell_lbound_it->qty) {
        // Unflag the normal way, if we're not always_unflag.
        unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, false);
      }

      sell_lbound_it->qty = best_ask_qty;
      Quantity post_shares = effectiveQty(*sell_lbound_it);

      if (pre_shares != post_shares) {
        auto lvl_action = LevelModify{
            .px = best_ask_px,
            .side = Side::Sell,
            .qty = post_shares,
            .unflagged_qty = sell_lbound_it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = pre_unflagged_shares,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }
    } else {
      // New level needs to be inserted
      auto inserted_lvl = sell_side->emplace(best_ask_px, best_ask_qty);

      // Check if we should flag the whole thing.
      if (data->last_agg_buy_trd_t_ >= msg.event_time()) {
        if (data->last_agg_buy_trd_px > best_ask_px) {
          // Then we have to flag all its shares.
          flagShares(*inserted_lvl.first, inserted_lvl.first->qty);
        }
      }

      Quantity add_qty = effectiveQty(*(inserted_lvl.first));
      if (add_qty != Quantity{0}) {
        if (best_ask_px <= buy_side->begin()->px) {
          // Go through and flag the other side.
          for (auto bid_it = buy_side->begin(); bid_it != buy_side->end(); ++bid_it) {
            if (bid_it->px >= best_ask_px) {
              // Then, flag the other side.

              Quantity prev_shs = effectiveQty(*bid_it);
              flagShares(*bid_it, bid_it->qty);

              // Reflect the flagging action.
              auto lvl_action = LevelModify{
                  .px = bid_it->px,
                  .side = Side::Buy,
                  .qty = Quantity{0},
                  .unflagged_qty = bid_it->qty,
                  .prev_qty = prev_shs,
                  .prev_unflagged_qty = bid_it->qty,
                  .last_msg_type = LastMsgType::BookUpdate,
              };

              notify(*book, lvl_action, listeners);

            } else {
              break;
            }
          }
        }

        auto lvl_action = LevelAdd{
            .px = best_ask_px,
            .side = Side::Sell,
            .qty = add_qty,
            .unflagged_qty = best_ask_qty,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }
    }
    data->last_ticker_ba = best_ask_px;
  }

  //  After all updates, notify on final.
  notifyFinal(*book, listeners);
}

// Update the inside of the book.
// Actually, I guess, I think we could use this for both snapshots and diff
// updates. Because
void BookManager::onBybitMDInsideSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  auto& last_update_id = data->book_update_last_update_id;

  data->last_msg_id_ = msg.last_update_id();

  // snapshot_depth should be 1.

  if (last_update_id >= msg.last_update_id()) {
    // notifyFinal(*book, listeners);
    return;
  }

  // Used for flagging purposes.
  data->last_ticker_event_t_ = msg.event_time();
  last_update_id = msg.last_update_id();

  // Handle bids side.
  // Possible that bids are empty. We can see a one-sided book too.
  if (msg.bids().size() > 0) {
    Price best_bid_px = Price{msg.bids()[0].px()};
    Quantity best_bid_qty = Quantity{msg.bids()[0].qty()};

    // Update Bids
    auto* buy_side = &book->side<BuySide>();
    auto buy_lbound_it = buy_side->lower_bound(best_bid_px);

    // Delete all price levels more aggressive than px
    for (auto it2 = buy_side->begin(); it2 != buy_lbound_it;) {
      auto lvl_action = LevelDelete{
          .px = it2->px,
          .side = Side::Buy,
          .prev_qty = effectiveQty(*it2),
          .prev_unflagged_qty = it2->qty,
          .last_msg_type = LastMsgType::BookTicker,
          .transact_t = msg.event_time(),
      };
      it2 = buy_side->erase(it2);
      // Don't call onDelete if we already effectively removed
      // that level due to flagging.
      if (lvl_action.prev_qty != Quantity{0}) {
        notify(*book, lvl_action, listeners);
      }
    }
    if (buy_lbound_it->px == best_bid_px) {
      // Don't levelModify if qty is the same

      Quantity pre_shares = effectiveQty(*buy_lbound_it);
      Quantity pre_unflagged_shares = buy_lbound_it->qty;

      // ALWAYS_UNFLAG means we unflag even if the next version of the book
      // might be more shares. But there might still be
      // good reason for us to not unflag.
      if (ALWAYS_UNFLAG) {
        // Unflag all the shares.

        // Then we can check the transact time. If the
        // incoming message is more "current", then we use that
        // and unflag all shares.
        if (data->last_agg_sell_trd_t_ < msg.event_time()) {
          unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, true);
        } else if (best_bid_qty < buy_lbound_it->qty) {
          // Otherwise, our trade came after the update, only unflag the
          // difference.
          unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, false);
        }

        // Make a call. If the trade-through is past this level, then
        // we need to flag the entire thing.
        if (data->last_agg_sell_trd_t_ >= msg.event_time()) {
          if (data->last_agg_sell_trd_px < best_bid_px) {
            // Then we have to flag all its shares.
            flagShares(*buy_lbound_it, buy_lbound_it->qty);
          } else if (data->last_agg_sell_trd_px == best_bid_px) {
            // I'm not sure re: the right action to take here, but I don't
            // know if we *want* to do anything..
            // TODO: come back to this.
          }
        }

      } else if (best_bid_qty < buy_lbound_it->qty) {
        // Unflag the normal way, if we're not always_unflag.
        unflagShares(*buy_lbound_it, buy_lbound_it->qty - best_bid_qty, false);
      }

      // Here. We should figure out if we were receiving something
      // old or something.

      buy_lbound_it->qty = best_bid_qty;
      Quantity post_shares = effectiveQty(*buy_lbound_it);

      if (pre_shares != post_shares) {
        auto lvl_action = LevelModify{
            .px = best_bid_px,
            .side = Side::Buy,
            .qty = post_shares,
            .unflagged_qty = buy_lbound_it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = pre_unflagged_shares,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }

    } else {
      // New level needs to be inserted
      auto inserted_lvl = buy_side->emplace(best_bid_px, best_bid_qty);
      // Check if we should flag the whole thing.
      if (data->last_agg_sell_trd_t_ >= msg.event_time()) {
        if (data->last_agg_sell_trd_px < best_bid_px) {
          // Then we have to flag all its shares.
          flagShares(*inserted_lvl.first, inserted_lvl.first->qty);
        }
      }

      Quantity add_qty = effectiveQty(*(inserted_lvl.first));

      if (add_qty != Quantity{0}) {
        auto lvl_action = LevelAdd{
            .px = best_bid_px,
            .side = Side::Buy,
            .qty = add_qty,
            .unflagged_qty = best_bid_qty,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }
    }
    data->last_ticker_bb = best_bid_px;
  }

  if (msg.asks().size() > 0) {
    Price best_ask_px = Price{msg.asks()[0].px()};
    Quantity best_ask_qty = Quantity{msg.asks()[0].qty()};

    // Update asks
    auto* sell_side = &book->side<SellSide>();
    auto sell_lbound_it = sell_side->lower_bound(best_ask_px);

    // Delete all price levels more aggressive than px
    for (auto it2 = sell_side->begin(); it2 != sell_lbound_it;) {
      auto lvl_action = LevelDelete{
          .px = it2->px,
          .side = Side::Sell,
          .prev_qty = effectiveQty(*it2),
          .prev_unflagged_qty = it2->qty,
          .transact_t = msg.event_time(),

      };
      it2 = sell_side->erase(it2);
      // Don't call onDelete if we already effectively removed
      // that level due to flagging.
      if (lvl_action.prev_qty != Quantity{0}) {
        notify(*book, lvl_action, listeners);
      }
    }

    if (sell_lbound_it->px == best_ask_px) {
      // Don't levelModify if qty is the same

      Quantity pre_shares = effectiveQty(*sell_lbound_it);
      Quantity pre_unflagged_shares = sell_lbound_it->qty;

      if (ALWAYS_UNFLAG) {
        // Unflag all the shares.

        // Then we can check the transact time. If the
        // incoming message is more "current", then we use that
        // and unflag all shares.
        if (data->last_agg_buy_trd_t_ < msg.event_time()) {
          unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, true);
        } else if (best_ask_qty < sell_lbound_it->qty) {
          // Otherwise, our trade came after the update, only unflag the
          // difference.
          unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, false);
        }

        // Make a call. If the trade-through is past this level, then
        // we need to flag the entire thing.
        if (data->last_agg_buy_trd_t_ >= msg.event_time()) {
          if (data->last_agg_buy_trd_px > best_ask_px) {
            // Then we have to flag all its shares.
            flagShares(*sell_lbound_it, sell_lbound_it->qty);
          } else if (data->last_agg_buy_trd_px == best_ask_px) {
            // I'm not sure re: the right action to take here, but I don't
            // know if we *want* to do anything..
            // TODO: come back to this.
          }
        }

      } else if (best_ask_qty < sell_lbound_it->qty) {
        // Unflag the normal way, if we're not always_unflag.
        unflagShares(*sell_lbound_it, sell_lbound_it->qty - best_ask_qty, false);
      }

      sell_lbound_it->qty = best_ask_qty;
      Quantity post_shares = effectiveQty(*sell_lbound_it);

      if (pre_shares != post_shares) {
        auto lvl_action = LevelModify{
            .px = best_ask_px,
            .side = Side::Sell,
            .qty = post_shares,
            .unflagged_qty = sell_lbound_it->qty,
            .prev_qty = pre_shares,
            .prev_unflagged_qty = pre_unflagged_shares,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }
    } else {
      // New level needs to be inserted
      auto inserted_lvl = sell_side->emplace(best_ask_px, best_ask_qty);

      // Check if we should flag the whole thing.
      if (data->last_agg_buy_trd_t_ >= msg.event_time()) {
        if (data->last_agg_buy_trd_px > best_ask_px) {
          // Then we have to flag all its shares.
          flagShares(*inserted_lvl.first, inserted_lvl.first->qty);
        }
      }

      Quantity add_qty = effectiveQty(*(inserted_lvl.first));
      if (add_qty != Quantity{0}) {
        auto lvl_action = LevelAdd{
            .px = best_ask_px,
            .side = Side::Sell,
            .qty = add_qty,
            .unflagged_qty = best_ask_qty,
            .last_msg_type = LastMsgType::BookTicker,
            .transact_t = msg.event_time(),

        };
        notify(*book, lvl_action, listeners);
      }
    }
    data->last_ticker_ba = best_ask_px;
  }

  //  After all updates, notify on final.
  notifyFinal(*book, listeners);
}

// Let's modify this s.t. we do actually handle snapshots as well.
void BookManager::onBybitMDSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  auto& last_update_id = data->book_update_last_update_id;

  int snapshot_depth = std::max(msg.bids().size(), msg.asks().size());

  // Might actually want to just reapply the snapshot in this case.
  if (data->seen_first_snapshot && snapshot_depth <= data->max_seen_snapshot_depth_) {
    return;
  }

  // Otherwise, update.
  data->max_seen_snapshot_depth_ = snapshot_depth;

  // Write in the last sizes.
  last_update_id = msg.last_update_id();

  data->depth_to_last_update_id_[snapshot_depth] = msg.last_update_id();
  data->depth_to_last_price_bids_[snapshot_depth] = std::stof(msg.bids().rbegin()->px());
  data->depth_to_last_price_asks_[snapshot_depth] = std::stof(msg.asks().rbegin()->px());

  auto update_side = [&](auto& container, auto* book_side) {
    using ComparatorSide =
        typename std::remove_reference_t<decltype(*book_side)>::key_compare::Side;
    int lvl_count = 0;

    // Create a new book side from snapshot
    LevelBook::BookSide<ComparatorSide> new_book_side;
    for (auto& update : container) {
      auto qty = Quantity{update.qty()};
      auto px = Price{update.px()};
      new_book_side.emplace(px, qty);
    }

    auto it_old = book_side->begin();
    auto it_new = new_book_side.begin();

    // If first update, don't notify listeners.

    // While both books still levels, iterate through them ensuring that old_px
    // is not less aggressive than new_px. If old_px is not present in
    // new_book_side, delete it. If old_px is present and qty differ, modify the
    // level. Otherwise, if new_px is not present in old_book, add it.
    while (it_old != book_side->end() && it_new != new_book_side.end()) {
      if (lvl_count > data->max_levels) {
        //  break;
      }

      if ((ComparatorSide::side == Side::Buy && it_old->px > it_new->px) ||
          (ComparatorSide::side == Side::Sell && it_old->px < it_new->px)) {
        auto lvl_action = LevelDelete{
            .px = it_old->px,
            .side = ComparatorSide::side,
            .prev_qty = it_old->qty,
            .prev_unflagged_qty = it_old->qty,
        };
        it_old = book_side->erase(it_old);
        if (data->seen_first_snapshot) {
          notify(*book, lvl_action, listeners);
        }
      } else if (it_old->px == it_new->px) {
        if (it_old->qty != it_new->qty) {
          // TOOD: maybe we need to unflag here too.
          auto lvl_action = LevelModify{
              .px = it_old->px,
              .side = ComparatorSide::side,
              .qty = it_new->qty,
              .unflagged_qty = it_new->qty,

              .prev_qty = it_old->qty,
              .prev_unflagged_qty = it_old->qty,

          };
          it_old->qty = it_new->qty;

          if (data->seen_first_snapshot) {
            notify(*book, lvl_action, listeners);
          }
        }
        ++it_old;
        ++it_new;
        lvl_count++;
      } else if ((ComparatorSide::side == Side::Buy && it_old->px < it_new->px) ||
                 (ComparatorSide::side == Side::Sell && it_old->px > it_new->px)) {
        auto lvl_action = LevelAdd{.px = it_new->px,
                                   .side = ComparatorSide::side,
                                   .qty = it_new->qty,
                                   .unflagged_qty = it_new->qty};
        book_side->insert(*it_new);
        if (data->seen_first_snapshot) {
          notify(*book, lvl_action, listeners);
        }
        ++it_new;
        lvl_count++;
      }
    }

    // If there are any levels in old book left over, delete them.
    while (it_old != book_side->end()) {
      auto lvl_action = LevelDelete{
          .px = it_old->px,
          .side = ComparatorSide::side,
          .prev_qty = it_old->qty,
          .prev_unflagged_qty = it_old->qty,
      };
      it_old = book_side->erase(it_old);
      if (data->seen_first_snapshot) {
        notify(*book, lvl_action, listeners);
      }
    }

    // If there are any levels left over in new book, add them to old book.
    for (; it_new != new_book_side.end(); ++it_new) {
      if (lvl_count > data->max_levels) {
        // break;
      }
      auto lvl_action = LevelAdd{
          .px = it_new->px,
          .side = ComparatorSide::side,
          .qty = it_new->qty,
          .unflagged_qty = it_new->qty,
      };
      book_side->insert(*it_new);
      if (data->seen_first_snapshot) {
        notify(*book, lvl_action, listeners);
      }
      lvl_count++;
    }
  };

  update_side(msg.bids(), &book->side<BuySide>());
  update_side(msg.asks(), &book->side<SellSide>());

  notifyFinal(*book, listeners);
  data->seen_first_snapshot = true;
  LOG(INFO) << " BookManager: received snapshot for "
            << pktrade::util::print_bookid(data->book->getBookID()) << std::endl;
}

// **************************************************************
// Topbook Oracle
void BookManager::onTopBookQuote(const mdmsg::PbQuote& msg, BookData* data) {
  // Discard stale quotes (can arrive when multiple feed sources are active).
  if (msg.transact_time() < data->last_quote_transact_t_) {
    return;
  }
  data->last_quote_transact_t_ = msg.transact_time();

  auto& book = data->book;
  auto& listeners = data->listeners;

  auto one_quote = Quote{.bb = msg.best_bid_px(),
                         .ba = msg.best_ask_px(),
                         .bs = msg.best_bid_qty(),
                         .as = msg.best_ask_qty(),
                         .mid = msg.mid_px(),
                         .source_t = msg.transact_time()};
  notifyQuote(*book, one_quote, listeners);
  notifyFinal(*book, listeners);
}

void BookManager::onTopBookTrade(const mdmsg::PbTrade& msg, BookData* data) {
  auto& book = data->book;
  auto& listeners = data->listeners;
  // data->last_trd_t_ = msg.trade_time();
  data->last_msg_id_ = msg.trade_id();
  data->last_local_flag_t_ = time_utils::nowToMs();

  Trade trd{
      .px = Price{msg.px()},
      .qty = Quantity{msg.qty()},
      .passive_side = msg.passive_was_buyer() ? Side::Buy : Side::Sell,
      .transact_t = msg.trade_time(),
  };
  notify(*book, trd, listeners);

  // No flagging for this sort of message.

  notifyFinal(*book, listeners);
}


BookId BookManager::getBookId(const mdmsg::PbMessage& msg) const noexcept {
  return BookId{
      pbMarketToMarket(msg.market()),
      SymbolId{msg.symbol_id()},
  };
}

} // namespace pktrade
