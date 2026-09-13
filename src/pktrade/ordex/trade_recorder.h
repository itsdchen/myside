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

/*

TradeRecorder: A passive ordex that records ALL book trades with delayed
market feature snapshots, for use in oracle DP optimization.

No orders are placed. All risk callbacks are no-ops.

Features are snapshotted every 100ms into a ring buffer, then looked up
at trade time minus a configurable delay (1s for maker, 1.5s for taker).

Trades are aggregated by transact_t, then tracked for 30s/300s markouts.

Used by TradeRecorder.py to generate trade-level CSVs for oracle_dp.py.

*/

namespace pktrade::ordex {

struct FeatureSnapshot {
  int64_t time_ms;
  double remote_mid;
  double local_mid;
  double spread;
  double bid_size;
  double ask_size;
  // Multi-TDC vol EMS (absolute return): 7 values.
  double vol_100ms;
  double vol_1s;
  double vol_4s;
  double vol_10s;
  double vol_30s;
  double vol_60s;
  double vol_300s;
  // Multi-TDC momentum EMS (signed return): 5 values.
  double mom_100ms;
  double mom_1s;
  double mom_4s;
  double mom_10s;
  double mom_30s;
  // Trade microstructure EMS: 10 values.
  double trade_rate;
  double trade_size;
  double trade_notional_signed;
  double trade_notional_abs;
  double book_depth_bid;
  double book_depth_ask;
  double buy_trade_depth;
  double sell_trade_depth;
  double large_trade_rate;
  double trade_depth;
};

struct TradeEventEntry {
  int64_t time_ms;
  double agg_px;       // VWAP of aggregated trades
  double agg_qty;      // total quantity
  double net_signed_qty; // net signed quantity (buy+ sell-)
  int n_trades;
  double remote_mid_at_trade;  // current remote mid at trade time

  FeatureSnapshot maker_features;   // looked up at trade_time - 1.0s
  FeatureSnapshot taker_features;   // looked up at trade_time - 1.5s

  // Markout tracking.
  static constexpr int kNumHorizons = 2;
  static constexpr int64_t kHorizonMs[kNumHorizons] = {30000, 300000};
  double horizon_remote_mid[kNumHorizons] = {};
  bool horizon_done[kNumHorizons] = {};
  int horizons_filled = 0;
};

class TradeRecorder : public Ordex,
                      public pktrade::tempos::TempoListener,
                      public pktrade::signals::SignalListener,
                      public pktrade::md::BeaconListener {
 public:
  TradeRecorder(SymbolId symbol, const rapidjson::Value& ordex_conf,
                pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf);
  ~TradeRecorder();

  void postSecMaster() override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  signals::Signal* getPredSignal() override;
  signals::Signal* getMidSignal() override;

  void onTempo(int tempo_id);
  void tryFire() override {}  // no-op

  Quantity getMaxPos() override;
  SymbolId getTradedSymbols();
  std::vector<Market> getMarkets();
  std::vector<pktrade::BookId> getBookIds();

  // Core setters required for all ordexes, for usermsgs and conf settings. No-ops for this ordex.
  void setThresh(double thresh) override {}
  void setThreshMult(double thresh_mult, MultSource src) override {}
  void setOrderSize(double size) override {}
  void setSizeMult(double size_mult, MultSource src) override {}

  // Risk callbacks — all no-ops.
  void newOrdAck(const Order& ord) override {}
  void ordUpdate(const Order& ord) override {}
  void ordCancel(const Order& ord) override {}
  void ordExec(const Order& ord, const OrderExecute& exec) override {}
  void ordElim(const Order& ord, const OrderElimination& elim) override {}
  void ordElimPrecog(Side side, Quantity qty) override {}
  void ordReject(const Order& ord, const NewOrderReject& rej) override {}

  void cancelOutstandingOrds() override;

  // Signal callbacks.
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override {}

  // BeaconListener: only onTrade used.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {}
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {}
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) {}

 protected:
  // Feature snapshot ring buffer.
  void snapshotFeatures();
  FeatureSnapshot lookupDelayedFeatures(int64_t now_ms, double delay_s);

  // Trade aggregation.
  void flushAggregatedTrade();

  // Markout processing.
  void processPendingMarkouts(int64_t now_ms, double cur_remote_mid);

  // CSV output.
  void openCsv();
  void writeCsvRow(const TradeEventEntry& entry);
  void flushCsv();

  // /////////////////////////////////////////////////////////
  // Core state.

  std::vector<Market> traded_books_;
  BookId traded_bid_;

  // Signals.
  pktrade::signals::Signal* local_sig_ = nullptr;
  pktrade::signals::Signal* remote_sig_ = nullptr;
  int local_sig_idx_;
  int remote_sig_idx_;
  double local_sig_val_ = 0;
  double remote_sig_val_ = 0;

  int64_t last_mid_t_ = 0;
  int64_t last_remote_mid_t_ = 0;

  double min_tick_;

  // Tempo IDs.
  int tc_tempo_id_ = -1;
  int snapshot_tempo_id_ = -1;

  // Config.
  double maker_delay_s_ = 1.0;
  double taker_delay_s_ = 1.5;

  // /////////////////////////////////////////////////////////
  // Multi-TDC vol EMS (absolute return).
  double abs_remote_mid_move_ems_100ms_ = 0;
  double abs_remote_mid_move_ems_1s_ = 0;
  double abs_remote_mid_move_ems_4s_ = 0;
  double abs_remote_mid_move_ems_10s_ = 0;
  double abs_remote_mid_move_ems_30s_ = 0;
  double abs_remote_mid_move_ems_60s_ = 0;
  double abs_remote_mid_move_ems_300s_ = 0;

  // Multi-TDC momentum EMS (signed return).
  double momentum_ems_100ms_ = 0;
  double momentum_ems_1s_ = 0;
  double momentum_ems_4s_ = 0;
  double momentum_ems_10s_ = 0;
  double momentum_ems_30s_ = 0;

  double prev_remote_sig_val_ = 0;

  // Trade microstructure EMS (all updated in onTrade).
  double trade_rate_ems_ = 0;
  double trade_size_ems_ = 0;
  double large_trade_rate_ems_ = 0;
  double trade_depth_ems_ = 0;
  double buy_trade_depth_ems_ = 0;
  double sell_trade_depth_ems_ = 0;
  double trade_notional_signed_ems_ = 0;
  double trade_notional_abs_ems_ = 0;
  double book_depth_bid_ems_ = 0;
  double book_depth_ask_ems_ = 0;
  double trade_micro_tdc_ms_ = 30000;
  int64_t last_local_vol_t_ = 0;

  // Spread EMA.
  double avg_local_spread_ = 0;
  double avg_spread_tdc_ms_ = 60000;
  int64_t last_spread_update_t_ = 0;

  // /////////////////////////////////////////////////////////
  // Feature ring buffer (5s max).
  std::deque<FeatureSnapshot> feature_ring_buffer_;
  static constexpr int64_t kRingBufferMaxMs = 5000;

  // /////////////////////////////////////////////////////////
  // Trade aggregation state.
  bool has_pending_agg_ = false;
  int64_t cur_transact_t_ = 0;
  double agg_px_sum_ = 0;     // sum(px * qty) for VWAP
  double agg_qty_sum_ = 0;    // sum(qty)
  double agg_signed_qty_ = 0; // sum(signed qty)
  int agg_n_trades_ = 0;
  int64_t agg_time_ms_ = 0;   // time of first trade in aggregation

  // /////////////////////////////////////////////////////////
  // Markout queue.
  std::deque<TradeEventEntry> pending_markouts_;

  // /////////////////////////////////////////////////////////
  // CSV output.
  std::ofstream csv_;
  bool csv_opened_ = false;
};

} // namespace pktrade::ordex
