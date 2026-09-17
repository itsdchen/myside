#pragma once

// RelWideHip4: relative-value market maker for a HIP-4 outcome market on Hyperliquid.
//
// This is the C++ port of the standalone Python `hip4maker`. It quotes both sides of one
// outcome's YES-coin book, pricing off a reference prediction market (Kalshi/Polymarket)
// delivered through a SigReference signal, and capitalizes the book at startup by minting
// collateral into YES+NO tokens via the gateway's split action.
//
// Logic ported from hip4maker, using RelWideMM2's vocabulary:
//   basis.py   -> premium EMA + fair value (premium_adjusted_pred_px = ref_mid +
//                 premium_ema_coef * EMA(local - ref); hip4maker's "basis" == the premium)
//   quotes.py  -> inventory-skewed center (adjusted_pred_px) + laddered quotes
//   runner.py  -> the per-tempo cycle and the startup split capitalization
//
// It deliberately does NOT copy RelWideMM2's equities machinery (premium EMA, closing-cross,
// supabase snapshots, momentum), none of which hip4maker uses.
//
// MODELING NOTE: a TradeRiskMan (and hence an Ordex) is scoped to a single symbol/book, so
// this quotes canonically on the YES coin only: a bid buys YES with collateral, an ask sells
// YES from inventory (the startup split provides that inventory). hip4maker additionally
// routes each leg through the YES or NO token to meet min-notional and fund from either
// balance (quotes.py::_route_leg); reproducing that needs two-book support and is a follow-up.

#include "pktrade/ordex/ordex.h"
#include "pktrade/signals/signal.h"
#include "pktrade/tempos/tempo_factory.h"

#include <random>
#include <vector>

namespace pktrade::ordex {

class RelWideHip4 : public Ordex,
                    public pktrade::tempos::TempoListener,
                    public pktrade::signals::SignalListener,
                    public pktrade::md::BeaconListener {
 public:
  RelWideHip4(SymbolId symbol, const rapidjson::Value& ordex_conf,
              pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf);

  // ---- Ordex interface ----
  void postSecMaster() override;
  void postSetRiskMan() override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  signals::Signal* getPredSignal() override { return remote_sig_; }
  signals::Signal* getMidSignal() override { return local_sig_; }

  void tryFire() override;

  SymbolId getTradedSymbols() override { return symbol_; }
  std::vector<BookId> getBookIds() override { return {traded_bid_}; }
  std::vector<Market> getMarkets() override { return traded_books_; }

  void setThresh(double thresh) override { place_thresh_ = thresh; }
  void setThreshMult(double thresh_mult, MultSource src) override { thresh_mult_ = thresh_mult; }
  void setOrderSize(double size) override { order_size_ = Quantity{std::to_string(size)}; }
  void setSizeMult(double size_mult, MultSource src) override { size_mult_ = size_mult; }
  Quantity getMaxPos() override { return max_pos_; }

  // ---- fill / order callbacks ----
  void newOrdAck(const Order& ord) override {}
  void ordUpdate(const Order& ord) override {}
  void ordCancel(const Order& ord) override;
  void ordExec(const Order& ord, const OrderExecute& exec) override;
  void ordElim(const Order& ord, const OrderElimination& elim) override;
  void ordElimPrecog(Side side, Quantity qty) override {}
  void ordReject(const Order& ord, const NewOrderReject& rej) override;

  void cancelOutstandingOrds() override;

  // ---- Tempo / signal listeners ----
  void onTempo(int tempo_id) override { tryFire(); }
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override {}

  // ---- Beacon (unused; we drive off the tempo) ----
  void onLvlAdd(const LevelBook& bk, const LevelAdd& a) override {}
  void onLvlMod(const LevelBook& bk, const LevelModify& m) override {}
  void onLvlDel(const LevelBook& bk, const LevelDelete& d) override {}
  void onTrade(const LevelBook& bk, const Trade& t) override {}
  void onFinal(const LevelBook& bk) override {}

 protected:
  // Fair value + quoting, named to match RelWideMM2 exactly. The pipeline mirrors it:
  //   pred_px_                  = the reference (remote) price we quote around
  //   premium_ema_              = EMA(local_mid - pred_px_), the local-vs-reference premium
  //     (hip4maker calls this the "basis"; RelWideMM2 calls it the premium -- same quantity)
  //   premium_adjusted_pred_px_ = pred_px_ + premium_ema_coef_ * premium_ema_
  //   adjusted_pred_px_         = premium_adjusted_pred_px_ shifted for inventory skew
  // and the ladder is built around adjusted_pred_px_.
  bool updatePredPx();                             // set pred_px_ = reference mid; false if not ready
  void updatePremiumEma();                         // EMA the local-vs-reference premium -> premium_adjusted_pred_px_
  void adjustPredPx();                             // premium_adjusted_pred_px_ + inventory skew -> adjusted_pred_px_
  double roundPxToSide(double px, bool round_up);  // fixed 5dp outcome grid; cf. Ordex::roundToSide
  bool shouldCancelPx(double px, Side side) const; // is this resting price off the desired rungs?
  void maybeCancel();                              // cancel resting orders off the desired rungs
  void maybePlaceFront();                          // place the front (level-0) rung, both sides
  void manageBacklevels();                         // place the back-level rungs, both sides
  void placeRung(Side side, double px, std::vector<SimpleOrder>& resting); // one rung, if fundable
  void emitCapitalRequirement();                   // one-shot capitalization req (no rel_wide analog)

  void removeOrder(PKOrderId oid);

  // ---- config / wiring ----
  std::vector<Market> traded_books_;
  BookId traded_bid_;

  signals::Signal* local_sig_ = nullptr;   // local HL YES mid
  signals::Signal* remote_sig_ = nullptr;  // reference (SigReference)
  int local_sig_id_ = -1;
  int remote_sig_id_ = -1;

  // premium (local-vs-reference) EMA + fair value. Same mechanism as RelWideMM2's premium_ema.
  double premium_ema_coef_ = 1.0;       // fraction of the premium EMA applied (RelWideMM2 name)
  double premium_tdc_ = 5000.0;         // premium EMA time-decay constant, ms (config: premium_tdc_s)
  double cur_premium_ = 0.0;            // latest local_mid - pred_px_
  double premium_ema_ = 0.0;            // time-decayed EMA of cur_premium_
  bool premium_initialized_ = false;
  int64_t last_premium_adjust_t_ = 0;
  double local_mid_ = 0.0;
  double remote_mid_ = 0.0;
  double pred_px_ = 0.0;                    // reference (remote) mid we quote around
  double premium_adjusted_pred_px_ = 0.0;   // pred_px_ + premium_ema_coef_ * premium_ema_
  double adjusted_pred_px_ = 0.0;           // premium_adjusted_pred_px_ shifted for inventory

  // quoting
  double place_thresh_ = 0.01;
  double rung_mult_ = 1.0;       // rung_threshold_multiplier
  int max_back_levels_ = 1;
  double cancel_buffer_ = 0.5;   // cancel when price drifts > cancel_buffer_ * place_thresh
  Quantity order_size_{0};
  Quantity max_pos_{0};
  double min_tick_ = 1e-5;       // fixed HIP-4 price grid
  double thresh_mult_ = 1.0;
  double size_mult_ = 1.0;

  // Set once the gateway rejects our orders because the outcome is no longer live on its
  // deployer venue (settled / expired / voided). Terminal: we cancel and stop quoting; outcomes
  // never un-resolve. See ordReject.
  bool resolved_ = false;

  // startup capitalization requirement (sent once to the gateway, which owns the mint + gating)
  int outcome_id_ = -1;
  // Target complete sets to request. Defaults to max_pos; a config override wins.
  double target_complete_sets_ = 0.0;
  bool cap_enabled_ = false;
  bool cap_sent_ = false;

  // resting orders (canonical YES book)
  std::vector<SimpleOrder> buy_orders_;
  std::vector<SimpleOrder> sell_orders_;

  int64_t now_fire_t_ = 0;
};

} // namespace pktrade::ordex
