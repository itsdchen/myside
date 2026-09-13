#pragma once

#include <memory>
#include <unordered_map>
#include <utility>
#include <vector>

#include "pktrade/feed.h"
#include "pktrade/level_book.h"
#include "pktrade/mdapi.h"

namespace pktrade {

class BookListener {
 public:
  virtual ~BookListener() = default;

  virtual void onLevelUpdate(const LevelBook&, const LevelAdd&) = 0;
  virtual void onLevelUpdate(const LevelBook&, const LevelModify&) = 0;
  virtual void onLevelUpdate(const LevelBook&, const LevelDelete&) = 0;
  virtual void onLevelUpdate(const LevelBook&, const Trade&) = 0;

  // For nbbo-like quotes
  virtual void onQuote(const LevelBook&, const Quote&) {};

  // For the end of large diff-based updates. Receive a callback
  // telling you that it is actually final.
  virtual void onFinal(const LevelBook&) = 0;
};

struct BookData {
  std::unique_ptr<LevelBook> book;
  std::vector<BookListener*> listeners;
  int64_t book_update_last_update_id = 0;
  int64_t ticker_update_last_update_id = 0;

  // Again, a detail for flagging/unflagging. In the case
  // where we have a mass trade-through event, the streams can get
  // de-synced. In this case, we
  int64_t last_ticker_event_t_;

  // Could be an updateid, tickerid, or a tradeid. The point is to
  // have some record of what it was that caused us to trade.
  // Note that this doesn't have to be numerically highest.
  int64_t last_msg_id_ = 0;

  // For non-binance, I'm going to track the update_ids on a per-level basis.
  // Because we can/will be getting updates at different depths.
  std::map<int, int64_t> depth_to_last_update_id_;

  // Example. If the depth_1 diff came and updated the best ask, great.
  // But if we got a depth_50 diff that comes in, but the last update_id is not
  // as recent as the depth_1 instance, we still want to refleect the deltas
  // from the later levels that come in.
  std::map<int, double> depth_to_last_price_bids_;
  std::map<int, double> depth_to_last_price_asks_;

  // Similar to last_ticker_event_t_, just for books (eg. bybit) for which
  // we don't have bookTicker msg types.
  int64_t last_book_update_t_;

  // If we successively apply more snapshots, keep track of the
  // largest one we've seen so we can only reapply ones that are larger.
  int max_seen_snapshot_depth_ = 0;

  // **************************************************************
  // Some extra variables for tracking trade throughs. On very violent
  // symbols, we can get tradethroughs of many, many price levels.
  // In that case, we track when the market-recorded time was of a trade on
  // a certain side, and what the price was.
  // If we see bookTicker updates on that side that happen within the same
  // time frame or earlier, we can infer that it was a state update that showed
  // up within the tradethrough, but that it was not necessarily
  // a new add that we can consume.
  // This is visible if one cobserves the book on Solana on an active day and
  // looks for large sequences of trades.

  // If we flagged based on a trade for x time, don't unflag it from an
  // update, unless if the time for the update is >= the time for the trade.
  // Note that this is in units of the MARKET time. not RECEIVE time.
  int64_t last_agg_buy_trd_t_ = 0;
  int64_t last_agg_sell_trd_t_ = 0;
  Price last_agg_buy_trd_px{0};
  Price last_agg_sell_trd_px{0};

  // **************************************************************

  // Used for unflag comparisons in feeds without msg timestamping.
  int64_t last_local_flag_t_ = 0;
  // If we're in something like SPOT and receive messages like:
  // 1 - Trade, goes through 3 levels (flags those 3 levels)
  // 2 - inside, keeps the first level, but not enough time has passed,
  //     we don't let the inside change unflag the actions from (1)
  // This is because there's not enough book information to tell us about the
  // unflagging, but the failure case is pretty bad.
  int64_t unflag_t_buffer_ = 5;

  // Should just store the last Ticker info. Because it might
  // override our notion of the book, even so.
  Price last_ticker_bb{"0"};
  Price last_ticker_ba{"0"};

  // **************************************************************
  // Note for quote feeds. For now, not going to add more stuff to BookData
  // and we can deal with quotes by just subscribing to quotes directly and passing
  // that along. We won't be walking or doing any book-related calcs in the signals,
  // in that case.

  // **************************************************************

  // For quote-based feeds (eg. databento equities), track the latest transact_time
  // per symbol so we can discard stale quotes when multiple feed sources are active.
  int64_t last_quote_transact_t_ = 0;

  bool seen_first_snapshot = false;

  // There may be situations where we're missing booksnapshots. If that is the
  // case, then let the book update itself 20x before it forces a snapshot.
  int diffs_seen_no_snapshot = 0;
  int diffs_seen_before_force_snapshot = 20;

  // Let's make sure we don't handicap ourselves later on.
  int max_levels = 1000;
};

class BookManager final : public MDListener {
 public:
  // [begin, end] marks a container containing
  // for the most part, BookIds (eg. what we subscribe to).
  template <typename ForwardIt>
  BookManager(ForwardIt begin, ForwardIt end) {
    for (auto it = begin; it != end; ++it) {
      auto [it2, success] = books_.try_emplace(*it);
      if (!success) {
        throw std::runtime_error("Duplicate BookId");
      }
      it2->second.book = std::make_unique<LevelBook>(*it);
    }
  }

  [[nodiscard]] const LevelBook* find(const BookId& book_id) const;

  const BookData* findBookData(const BookId& book_id) const;

  const std::unordered_map<BookId, BookData>& getBooks() { return books_; }

  // Silly. But retrieves the ticker_update_last_update_id, as a way of
  // telling us when in the datastream we were reacting.
  const int64_t getLastTickerUpdateId(const BookId& book_id) const;

  const int64_t getLastMsgId(const BookId& book_id) const;

  void onMD(const mdmsg::PbMessage& msg) override;

  void subscribe(const BookId& book_id, BookListener* listener);

 private:
  // ///////////////////////////////////////////////////////////
  void onBinanceMDDiff(const mdmsg::PbBookUpdate& msg, BookData* data);
  void onBinanceMDBookTicker(const mdmsg::PbTickerUpdate& msg, BookData* data);
  void onBinanceMDTrade(const mdmsg::PbTrade& msg, BookData* data);
  void onBinanceMDSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data);

  // ///////////////////////////////////////////////////////////
  void onHyperliquidMDDiff(const mdmsg::PbBookUpdate& msg, BookData* data);
  void onHyperliquidMDSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data);
  void onHyperliquidMDTrade(const mdmsg::PbTrade& msg, BookData* data);

  // ///////////////////////////////////////////////////////////
  void onBybitMDDiff(const mdmsg::PbBookUpdate& msg, BookData* data);

  void onBybitMDTrade(const mdmsg::PbTrade& msg, BookData* data);

  void onBybitMDInsideDiff(const mdmsg::PbBookUpdate& msg, BookData* data);

  // Essentially a bookTicker update, but is done for depth-1 snapshots. Because
  // those are behaviorally bookTickers.
  void onBybitMDInsideSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data);
  void onBybitMDSnapshot(const mdmsg::PbBookSnapshot& msg, BookData* data);

  // ///////////////////////////////////////////////////////////
  // Almost like a bookticker, but not quite
  void onTopBookQuote(const mdmsg::PbQuote& msg, BookData* data);
  void onTopBookTrade(const mdmsg::PbTrade& msg, BookData* data);

  // ///////////////////////////////////////////////////////////
  [[nodiscard]] BookId getBookId(const mdmsg::PbMessage& msg) const noexcept;

  template <typename T>
  void notify(const LevelBook& book, const T& msg,
              const std::vector<BookListener*>& listeners) const {
    for (auto* listener : listeners) {
      listener->onLevelUpdate(book, msg);
    }
  }

  template <typename T>
  void notifyQuote(const LevelBook& book, const T& msg,
                   const std::vector<BookListener*>& listeners) const {
    for (auto* listener : listeners) {
      listener->onQuote(book, msg);
    }
  }

  void notifyFinal(const LevelBook& book, const std::vector<BookListener*>& listeners) {
    for (auto* listener : listeners) {
      listener->onFinal(book);
    }
  }

  std::unordered_map<BookId, BookData> books_;
};

} // namespace pktrade
