#pragma once

#include "pktrade/mdapi.h"
#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/risk/trade_risk_man.h"
#include <rapidjson/document.h>

#include "pktrade/oeapi.h"

namespace pktrade::sim {

// Internal type for our own add-liq order.
// These are tied to the orders_, but contain
// sim-specific metadata.
struct SimOrder {
  int our_ord_id_;
  int mkt_ord_id_;

  Price px_;
  Quantity qty_;
  Side side_;

  // After we get executed against,
  Quantity orig_qty_;

  // When this order "went live".
  // If we do set this, compare it against the transaction_time of the live
  // orders.
  int64_t time_received_;

  // For estimating PIQ.
  Quantity shares_ahead_lvl_;
};

// Internal type for our flagging.
struct SimFlaggedShares {
  // I'm ok allowing some double-ness here...
  double qty;
  int64_t flagged_time;
};

// Should all be MDBeacon listeners so they can
class SimBase : public pktrade::md::BeaconListener {
 public:
  SimBase(SymbolId sym, pktrade::risk::TradeRiskMan* riskman, rapidjson::Value& sim_conf);

  // Each instantiation should do its own subscription.
  virtual void subscribeData(pktrade::md::MDBeacon* beacon) = 0;
  virtual std::vector<BookId> getRequiredDataSubs() { return {}; }

  // Beacon callbacks.
  virtual void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) = 0;
  virtual void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) = 0;
  virtual void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) = 0;
  virtual void onTrade(const LevelBook& bk, const Trade& trd) = 0;
  virtual void onFinal(const LevelBook& bk) = 0;

  // For order gateway stuff...
  virtual void onSentOrder(Order* ord) = 0;
  virtual void maybeOrderHitsBook(int64_t cur_book_transact_t) = 0;
  // Happens when our order would hit the book.
  // Could be set by our tttimes estimate, or offset+transact time
  // offset. Implementation determines this.
  virtual void onOrderHitsME(Order* ord) = 0;

  virtual void onCancelOrder(const CancelOrder& cancel) = 0;
  virtual void maybeCxlHitsBook(int64_t cur_book_transact_t) = 0;
  virtual void onCancelHitsME(PKOrderId cancel_ord_id) = 0;

  // EOD stuff. If I want to display eod stats.
  virtual void eod() {}

 protected:
  SymbolId sym_;
  // Set for each market already.
  Market mkt_;

  // Collection of add-liq orders.
  // Sorted by price (more aggressive to less aggressive)
  std::vector<SimOrder> buy_orders_;
  std::vector<SimOrder> sell_orders_;

  pktrade::risk::TradeRiskMan* riskman_;
};

} // namespace pktrade::sim
