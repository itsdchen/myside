#pragma once

#include "ordex.h"

#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/tempos/base_tempo.h"
#include "pktrade/tempos/tempo_factory.h"
#include "pktrade/risk/trade_risk_man.h"
#include "pktrade/signals/signal.h"
#include "pktrade/signals/signal_factory.h"

#include <deque>
#include <fstream>
#include <random>

/*

DiagnosticsMM: A simplified market-making ordex whose sole purpose is to
quote with a constant threshold and record fill diagnostics (markout
tracking, vol EMS at multiple time-decay constants, remote mid ring buffer,
local volume tracking, spread EMA, etc.).

It is intentionally stripped of: crossing, relative mode, TL2/TL3 modes,
equity off-hour VWAP, dynamic thresholds (types 1–4), vol scaling,
trend bias, pred momentum widening, SBCaller, queue jump, etc.

Used by SymDiag.py to generate fill-level diagnostic CSVs for analysis.

*/

namespace pktrade::ordex {

class DiagnosticsMM : public Ordex,
                      public pktrade::tempos::TempoListener,
                      public pktrade::signals::SignalListener,
                      public pktrade::md::BeaconListener {
 public:
  DiagnosticsMM(SymbolId symbol, const rapidjson::Value& ordex_conf,
                pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf);
  ~DiagnosticsMM();

  void postSecMaster() override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  signals::Signal* getPredSignal() override;
  signals::Signal* getMidSignal() override;

  void onTempo(int tempo_id);
  void tryFire() override;
  void onFirstFire(double pred_px);

  void updateThreshes();
  void updatePremiumEma();

  void maybePlaceFront(const LevelBook* lvl_book, double ordex_allowed_buy,
                       double ordex_allowed_sell);
  void maybeCancel(const LevelBook* lvl_book, double ordex_allowed_buy,
                   double ordex_allowed_sell);
  void manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy,
                        double abs_allowed_sell);

  double roundToNearest(double px_extra_precision);
  double roundToSide(double px, bool round_up);
  // Walk a book side accumulating notional until target_notional is reached.
  // Returns price depth as (best_px - last_fill_px) / mid for bids,
  // or (last_fill_px - best_px) / mid for asks. Always >= 0.
  template <typename SideType>
  double liqDepth(const LevelBook& bk, double mid, double target_notional);
  double getRandomValue(bool front);
  double genOrderSize(bool front);
  bool canCancelOrd(SimpleOrder& ord, bool fast = false);

  // Core setters required for all ordexes, for usermsgs and conf settings. No-ops for this ordex.
  void setThresh(double thresh) override {}
  void setThreshMult(double thresh_mult, MultSource src) override {}
  void setOrderSize(double size) override {}
  void setSizeMult(double size_mult, MultSource src) override {}

  Quantity getMaxPos() override;
  SymbolId getTradedSymbols();
  std::vector<Market> getMarkets();
  std::vector<pktrade::BookId> getBookIds();

  // Risk callbacks.
  void newOrdAck(const Order& ord) override;
  void ordUpdate(const Order& ord) override;
  void ordCancel(const Order& ord) override;
  void ordExec(const Order& ord, const OrderExecute& exec) override;
  void ordElim(const Order& ord, const OrderElimination& elim) override;
  void ordElimPrecog(Side side, Quantity qty) override {};
  void ordReject(const Order& ord, const NewOrderReject& rej) override;

  void cancelOutstandingOrds() override;

  // Signal + trade callbacks.
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override {};

  // BeaconListener: only onTrade used (for local volume tracking).
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {}
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {}
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) {};

 protected:
  // /////////////////////////////////////////////////////////
  // Core quoting state (simplified from RelWideMM2).

  std::vector<Market> traded_books_;
  BookId traded_bid_;

  Quantity max_pos_;
  Quantity order_size_;

  bool set_size_from_notional_ = false;
  double tgt_order_notional_ = 1000;
  double tgt_maxpos_notional_ = 10000;

  // Randomization.
  double order_random_upper_limit_ = 1.7;
  double order_random_lower_limit_ = 0.3;
  double front_order_random_upper_limit_ = 1.3;
  double front_order_random_lower_limit_ = 0.7;
  std::mt19937 gen_;
  std::uniform_real_distribution<double> dis_;
  std::uniform_real_distribution<double> dis_front_;

  // Constant place threshold (no dynamic modes).
  double base_place_thresh_;
  double cancel_buffer_;

  double cur_buy_thresh_px_;
  double cur_sell_thresh_px_;
  double cur_buy_cancel_thresh_px_;
  double cur_sell_cancel_thresh_px_;

  // Position-based widening.
  double per_order_widen_frac_;
  bool ladder_one_sided_;

  // Rung spacing.
  double rung_spacing_mult_;
  double rung_spacing_px_ = 0;
  int max_back_levels_;
  double per_backlevel_rung_spacing_mult_ = 1;

  // Premium EMA.
  double premium_tdc_ = 1000 * 60 * 20;
  int64_t last_premium_adjust_t_ = 0;
  double cur_premium_ = 0;
  double premium_ema_ = 0;
  double premium_ema_coef_ = 0;
  double premium_adjusted_pred_px_ = 0;

  // Orders.
  std::vector<SimpleOrder> buy_orders_;
  std::vector<SimpleOrder> sell_orders_;

  // Rate limiters.
  int64_t ms_min_ord_lifetime_ = 200;
  int64_t ms_between_place_ = 100;
  int64_t ms_between_cxl_ = 100;
  int64_t ms_between_backlevel_ = 2000;
  int64_t last_t_place_ = 0;
  int64_t last_t_cxl_ = 0;
  int64_t last_t_backlevel_ = 0;
  int64_t CANCEL_RETRY_INTERVAL = 12 * 1000;

  // Signals.
  pktrade::signals::Signal* local_sig_ = nullptr;
  pktrade::signals::Signal* remote_sig_ = nullptr;
  int local_sig_idx_;
  int remote_sig_idx_;
  double local_sig_val_ = 0;
  double remote_sig_val_ = 0;
  double pred_px_ = 0;

  int num_fires_ = 0;
  int64_t first_fire_t_ = 0;
  int64_t now_fire_t_ = 0;

  double min_tick_;
  bool needs_first_fire_ = true;

  TimeInForce tif_ = TimeInForce::GTC;

  int64_t last_mid_t_ = 0;
  int64_t last_remote_mid_t_ = 0;

  // /////////////////////////////////////////////////////////
  // Fill diagnostics state.

  struct FillDiagEntry {
    int64_t fill_time_ms;
    double fill_px;
    int side_sign;  // +1 buy, -1 sell
    double fill_qty;
    double local_mid;
    double remote_mid;
    double local_bid;
    double local_ask;
    double local_spread;
    double avg_local_spread;
    double vol_ems_remote;
    double vol_ems_local;
    double position_after;
    double pred_px;
    double premium_ema;
    bool add_liq;
    // Multi-tdc vol EMS (absolute return).
    double vol_ems_remote_100ms;
    double vol_ems_remote_1s;
    double vol_ems_remote_4s;
    double vol_ems_remote_10s;
    double vol_ems_remote_30s;
    double vol_ems_remote_60s;
    double vol_ems_remote_300s;
    // Multi-tdc momentum EMS (signed return).
    double momentum_100ms;
    double momentum_1s;
    double momentum_4s;
    double momentum_10s;
    double momentum_30s;
    // Book sizes.
    double local_bb_size;
    double local_ba_size;
    // Local volume EMS.
    double local_vol_ems;
    // Trailing remote returns (signed, looking back).
    double trailing_ret_30s;
    double trailing_ret_60s;
    double trailing_ret_300s;
    // Trade microstructure features.
    double trade_rate_ems;       // recent trade count (exponentially weighted)
    double trade_size_ems;       // recent trade size (exponentially weighted)
    double large_trade_rate_ems; // recent large trade count
    double trade_depth_ems;      // how deep into book recent trades went (relative to mid)
    double buy_trade_depth_ems;  // depth of buy-side trades only (aggressor buying)
    double sell_trade_depth_ems; // depth of sell-side trades only (aggressor selling)
    double trade_notional_signed_ems;  // EMS of signed trade notional (buy +, sell -)
    double trade_notional_abs_ems;     // EMS of |trade notional|
    double book_depth_bid;       // total bid size within 5 ticks of best (snapshot at trade time EMS)
    double book_depth_ask;       // total ask size within 5 ticks of best (snapshot at trade time EMS)
    // Deep book depth (snapshot at fill time, within range of mid).
    double deep_bid_notional;    // total bid-side notional within deep_book_range of mid
    double deep_ask_notional;    // total ask-side notional within deep_book_range of mid
    // Notional-based book depth: price depth (relative to mid) to liquidate N notional.
    // Instantaneous snapshots at fill time.
    double liq_depth_buy_100k;   // price depth to sell $100k into bids
    double liq_depth_buy_250k;   // price depth to sell $250k into bids
    double liq_depth_sell_100k;  // price depth to buy $100k from asks
    double liq_depth_sell_250k;  // price depth to buy $250k from asks
    // EMS versions (30s TDC, updated on each trade).
    double liq_depth_buy_100k_ems;
    double liq_depth_buy_250k_ems;
    double liq_depth_sell_100k_ems;
    double liq_depth_sell_250k_ems;
    // Mid at each markout horizon (0 = not yet recorded)
    static constexpr int kNumHorizons = 5;
    static constexpr int64_t kHorizonMs[kNumHorizons] = {1000, 5000, 30000, 60000, 300000};
    double horizon_local_mid[kNumHorizons] = {};
    double horizon_remote_mid[kNumHorizons] = {};
    bool horizon_done[kNumHorizons] = {};
    int horizons_filled = 0;
  };

  // Fill diagnostics methods.
  void openDiagCsv();
  void processPendingMarkouts(int64_t now_ms, double cur_remote_mid);
  void writeDiagRow(const FillDiagEntry& entry);
  void flushDiagCsv();

  std::ofstream diag_csv_;
  bool diag_csv_opened_ = false;
  std::deque<FillDiagEntry> pending_markouts_;

  // Periodic state-dump CSV (research instrumentation, off by default).
  // When periodic_dump_ms_ > 0, every periodic_dump_ms_ ms during tradecalls
  // we emit a row of the current EMS state to a separate CSV. Useful for
  // studying signal behavior across spike windows, since the regular
  // fill-conditional diag row only fires when DiagnosticsMM happens to fill.
  void openPeriodicCsv();
  void writePeriodicRow(int64_t now_ms);
  void flushPeriodicCsv();
  int64_t periodic_dump_ms_ = 0;
  std::ofstream periodic_csv_;
  bool periodic_csv_opened_ = false;
  int64_t last_periodic_t_ = 0;

  // Multi-tdc vol EMS for diagnostics (absolute return).
  double abs_remote_mid_move_ems_100ms_ = 0;
  double abs_remote_mid_move_ems_1s_ = 0;
  double abs_remote_mid_move_ems_4s_ = 0;
  double abs_remote_mid_move_ems_10s_ = 0;
  double abs_remote_mid_move_ems_30s_ = 0;
  double abs_remote_mid_move_ems_60s_ = 0;
  double abs_remote_mid_move_ems_300s_ = 0;
  // Multi-tdc momentum EMS (signed return).
  double momentum_ems_100ms_ = 0;
  double momentum_ems_1s_ = 0;
  double momentum_ems_4s_ = 0;
  double momentum_ems_10s_ = 0;
  double momentum_ems_30s_ = 0;
  double prev_remote_sig_val_ = 0;

  // Deep book depth range (fraction of mid, default 50 bps).
  double deep_book_range_frac_ = 0.005;

  // Ring buffer for trailing returns.
  struct RemoteMidEntry {
    int64_t time_ms;
    double value;
  };
  std::deque<RemoteMidEntry> remote_mid_history_;

  // Local volume EMS.
  double local_vol_ems_ = 0;
  double local_vol_ems_tdc_ms_ = 60000;  // 60s
  int64_t last_local_vol_t_ = 0;

  // Trade microstructure EMS (all updated in onTrade).
  double trade_rate_ems_ = 0;        // count of recent trades
  double trade_size_ems_ = 0;        // recent trade sizes
  double large_trade_rate_ems_ = 0;  // count of above-average trades
  double trade_depth_ems_ = 0;       // how far into book trades went (relative to mid)
  double buy_trade_depth_ems_ = 0;   // depth of buy-side (aggressor buying) trades
  double sell_trade_depth_ems_ = 0;  // depth of sell-side (aggressor selling) trades
  double trade_notional_signed_ems_ = 0;  // signed trade notional (buy +, sell -)
  double trade_notional_abs_ems_ = 0;     // |trade notional|
  double book_depth_bid_ems_ = 0;    // bid depth within 5 ticks (snapshotted at each trade)
  double book_depth_ask_ems_ = 0;    // ask depth within 5 ticks
  // Notional-based liquidity depth EMS (30s TDC, updated on each trade).
  double liq_depth_buy_100k_ems_ = 0;
  double liq_depth_buy_250k_ems_ = 0;
  double liq_depth_sell_100k_ems_ = 0;
  double liq_depth_sell_250k_ems_ = 0;
  double trade_micro_tdc_ms_ = 30000;  // 30s decay for all trade microstructure EMS

  // Spread EMA.
  double avg_local_spread_ = 0;
  double avg_spread_tdc_ms_ = 60000;
  int64_t last_spread_update_t_ = 0;

  // Single-tdc vol EMS (used in vol_ems_remote/local columns).
  double abs_remote_mid_move_ems_ = 0;
  double abs_local_mid_move_ems_ = 0;
  double vol_ems_tdc_ms_ = 120 * 1000;
  double prev_local_sig_val_ = 0;

  static bool compareSellOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px < rord.px); }
  static bool compareBuyOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px > rord.px); }
};

} // namespace pktrade::ordex
