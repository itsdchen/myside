#pragma once

#include "pktrade/enums/basic_enums.h"
#include "pktrade/mdapi.h"
#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/oeapi.h"
#include "pktrade/tempos/base_tempo.h"
#include "pktrade/util/time_utils.h"
#include <vector>

namespace pktrade::risk {
class TradeRiskMan;
}

namespace pktrade::signals {
class Signal;
}

namespace pktrade::ordex {

enum class SimpleOrderState { LIVE, CANCEL_INFLIGHT };

struct SimpleOrder {
  // not sure if this is the right orderid or not.
  PKOrderId pk_oid;

  // Don't have symbol and market, that is obvious.
  Side side;
  Price px;
  Quantity init_qty;

  SimpleOrderState state;
  int64_t place_time;
  int64_t cancel_time;
};

// Basic interface for all ordex.
// If we use arbs, this will be the way too.
class Ordex {
 public:
  Ordex(SymbolId symbol, const rapidjson::Value& conf) : symbol_(symbol) {
    debug_verbose_ = false;
    if (conf.HasMember("debug_verbose")) {
      debug_verbose_ = conf["debug_verbose"].GetBool();
    }
  }

  void setRiskMan(pktrade::risk::TradeRiskMan* riskman) {
    riskman_ = riskman;
    postSetRiskMan();
  }

  // Subscriptions after we make the secmaster.
  virtual void postSecMaster() {}

  // Some ordexes might have settings that rely on risman settings (eg. risk,
  // notional, etc.) But
  virtual void postSetRiskMan() {}

  virtual void tryFire() = 0;

  virtual SymbolId getTradedSymbols() = 0;
  virtual std::vector<BookId> getBookIds() = 0;
  virtual std::vector<Market> getMarkets() = 0;

  virtual void subscribeData(pktrade::md::MDBeacon* beacon) = 0;

  virtual pktrade::signals::Signal* getPredSignal() = 0;
  virtual pktrade::signals::Signal* getMidSignal() = 0;

  // Ordexes will need to implement them separately.
  // Creating hooks for usermsgs - for widening spreads, changing size, etc.

  // These are core setters that every ordex needs to implement.
  // Set the place thresh.
  virtual void setThresh(double thresh) = 0;
  // Scale the effective market by a multiplier. Each MultSource is a separate
  // slot; the effective mult is the widest of them. See MultSource.
  virtual void setThreshMult(double thresh_mult, MultSource src) = 0;
  // Set the order size (without changing max pos).
  virtual void setOrderSize(double size) = 0;
  // Scale the order size and both max pos by a multiplier. Each MultSource is a
  // separate slot; the effective mult is their product. See MultSource.
  virtual void setSizeMult(double size_mult, MultSource src) = 0;

  // Set the relative beta, in case the relative price goes too crazy
  virtual void setRelativeBeta(double beta) {}

  // Set the extra buy and sell threshes, in case we need to bias our market up or down
  virtual void setExtraThreshes(double extra_buy, double extra_sell) {}

  // Set the const_pred_px, in case we're using that mode
  virtual void setConstPredPx(double px) {}

  // The ordex maxpos, not the risk maxpos.
  virtual Quantity getMaxPos() = 0;

  // Things that the riskman can call with.
  virtual void newOrdAck(const Order& ord) = 0;
  virtual void ordUpdate(const Order& ord) = 0;
  virtual void ordCancel(const Order& ord) = 0;
  virtual void ordExec(const Order& ord, const OrderExecute& exec) = 0;
  virtual void ordElim(const Order& ord, const OrderElimination& elim) = 0;
  virtual void ordElimPrecog(Side side, Quantity qty) = 0;
  virtual void ordReject(const Order& ord, const NewOrderReject& rej) = 0;

  // HIP-4 collateral (mint/split, or merge) replies from the gateway. Defaults are no-ops so
  // only HIP-4 strategies that issue splits need to handle them.
  virtual void splitAck(const SplitOutcomeAck& ack) {}
  virtual void splitReject(const SplitOutcomeReject& rej) {}

  virtual void cancelOutstandingOrds() {}

  virtual void hitMinFv() {}
  virtual void backFromMinFv() {}

  void setTL2AggrExit(bool TL2_aggr_exit) { TL2_aggr_exit_ = TL2_aggr_exit; }
  void setTL3AggrExit(bool TL3_aggr_exit) { TL3_aggr_exit_ = TL3_aggr_exit; }
  void setTL6CrossExit(bool TL6_cross_exit) { TL6_cross_exit_ = TL6_cross_exit; }
  void setTL7NoNewOrds(bool TL7_no_new_ords) { TL7_no_new_ords_ = TL7_no_new_ords; }

  void setOraclePx(double oracle_px) { oracle_px_ = oracle_px; }

 protected:
  // Prob every ordex should have just one symbol?
  SymbolId symbol_;
  pktrade::risk::TradeRiskMan* riskman_ = nullptr;

  // Just make sure I can tradecall.
  pktrade::tempos::BaseTempo* tc_tempo_ = nullptr;
  bool debug_verbose_;

  // For TL2. Mode for aggressively exiting. Up to ordex, but should be something like add to exit
  // with 0.5x thresh, cross to exit with normal thresh.
  bool TL2_aggr_exit_ = false;

  // For TL3. Mode for max aggressively exiting. Up to ordex, but should be disregarding predpx,
  // 0 thresh, basically just placing at the top of the book to exit.
  bool TL3_aggr_exit_ = false;

  // For TL6. Mode for aggressively crossing to exit, no adding. Up to ordex, but should be
  // something like crossing with 0 thresh to exit, overriding other crossing settings.
  bool TL6_cross_exit_ = false;

  // For TL7. Mode for pausing new orders but keeping old ones, unless they're canceled for normal
  // trading reasons (e.g., predpx).
  bool TL7_no_new_ords_ = false;

  // For tracking the official oracle price.
  double oracle_px_ = 0;

  // These are used in all ordexes so consolidating them here to avoid copy/pasting.
  // Given a price, round to the nearest value.
  double roundToNearest(double px_extra_precision);

  // Buy orders should round down, sell orders should round up.
  double roundToSide(double px_extra_precision, bool round_up);
};

} // namespace pktrade::ordex
