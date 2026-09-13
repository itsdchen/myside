#pragma once

#include "ordex.h"
#include "vol_mechanisms.h"

#include <algorithm>

#include "pktrade/mktdata/md_beacon.h"

#include "pktrade/tempos/base_tempo.h"
#include "pktrade/tempos/tempo_factory.h"

#include "pktrade/risk/trade_risk_man.h"
#include "pktrade/signals/signal.h"
#include "pktrade/signals/signal_factory.h"

/*

Taker-only ordex using relative pricing off a remote signal.
Sends IOC orders when the remote-derived pred price is sufficiently
far from the local best bid/ask.

Uses VolMechanisms for dynamic threshold adjustment (same as RelWideMM2),
plus impulse curvicity and pred momentum for sided widening.

Supports passive exit: after crossing into a position, places a GTC limit
order on the opposite side at a profit target (from SimpleCross pattern).

*/

namespace pktrade::ordex {

class RelCross : public Ordex,
                 public pktrade::tempos::TempoListener,
                 public pktrade::signals::SignalListener,
                 public pktrade::md::BeaconListener {
 public:
  RelCross(SymbolId symbol, const rapidjson::Value& ordex_conf,
           pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf);

  void postSecMaster() override;

  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  signals::Signal* getPredSignal() override;
  signals::Signal* getMidSignal() override;

  // TL5: aggressively flatten position.
  void aggFlat();

  void onTempo(int tempo_id);
  void tryFire();

  void updateThreshes();
  void updatePremiumEma();
  void updatePredMomentumEms(double pred_px);
  void applyExecDecays();

  void maybeCross(double allowed_buy, double allowed_sell);
  void logCrossDebug(PKOrderId pkord_id, Side side, double order_px, double order_qty,
                     double best_bid, double best_ask, double thresh_px, double cur_pos);

  // Passive exit: place GTC limit to take profit on existing position.
  void placePassiveExit();
  void cancelPassiveExit();
  void refreshPassiveExit();

  void onFirstFire(double pred_px);

  // Core setters required for all ordexes, for usermsgs and conf settings.
  void setThresh(double thresh) override;
  void setThreshMult(double thresh_mult, MultSource src) override;
  void setOrderSize(double size) override;
  void setSizeMult(double size_mult, MultSource src) override;

  Quantity getMaxPos() override;

  SymbolId getTradedSymbols();
  std::vector<Market> getMarkets();
  std::vector<pktrade::BookId> getBookIds();

  // Callbacks from risk.
  void newOrdAck(const Order& ord) override;
  void ordUpdate(const Order& ord) override;
  void ordCancel(const Order& ord) override;
  void ordExec(const Order& ord, const OrderExecute& exec) override;
  void ordElim(const Order& ord, const OrderElimination& elim) override;
  void ordElimPrecog(Side side, Quantity qty) override;
  void ordReject(const Order& ord, const NewOrderReject& rej) override;

  void cancelOutstandingOrds() override;

  // Signal callbacks.
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override {};

  // Beacon callbacks (for liq_depth).
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {}
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {}
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) {};

 protected:
  std::vector<Market> traded_books_;
  BookId traded_bid_;

  Quantity max_pos_;
  Quantity order_size_;

  // Notional-based sizing.
  bool set_size_from_notional_ = false;
  double tgt_order_notional_ = 1000;
  double tgt_maxpos_notional_ = 10000;

  // Base values for setSizeMult (snapshot before first mult application).
  // setSizeMult applies mult relative to these, so it doesn't compound.
  bool has_base_sizes_ = false;
  // One slot per MultSource; the effective mult is their product, so a usermsg
  // mult scales relative to the conf'd size instead of replacing it.
  double conf_size_mult_ = 1;
  double um_size_mult_ = 1;
  double dd_size_mult_ = 1;  // DoubleDown MultSource slot
  double base_max_pos_ = 0;
  double base_order_size_ = 0;
  double base_tgt_order_notional_ = 0;
  double base_tgt_maxpos_notional_ = 0;

  double effSizeMult() const { return conf_size_mult_ * um_size_mult_ * dd_size_mult_; }

  void refreshSizeMultBaseFromCurrent();

  // Base cross threshold in fractional units (e.g. 0.0005 = 5bps).
  double base_cross_thresh_;
  // One thresh mult slot per MultSource; the effective mult is the widest of
  // them, so a defensive widen can't be clobbered by an unrelated writer
  // resetting to 1.
  double um_thresh_mult_ = 1;
  double bleed_thresh_mult_ = 1;
  double minfv_thresh_mult_ = 1;

  double effThreshMult() const {
    return std::max({um_thresh_mult_, bleed_thresh_mult_, minfv_thresh_mult_});
  }

  // Dynamic sided thresholds in price units.
  double cur_buy_cross_thresh_px_ = 0;
  double cur_sell_cross_thresh_px_ = 0;

  // Threshold multiplier when flattening position (< 1 = easier to exit).
  double exit_adjust_ = 1.0;

  // Per-unit position bias on thresholds.
  double per_order_widen_frac_ = 0;

  // Post-fill impulse widening (same as RelWideMM2).
  double curv_impulse_tdc_ms_ = 1000;
  double curv_impulse_coef_ = 0;
  double curv_impulse_ = 0;
  int64_t last_exec_decay_t_ = 0;

  // Pred momentum-based sided widening (same as RelWideMM2).
  double pred_momentum_tdc_ms_ = 1000 * 30;
  double pred_momentum_coef_ = 0;
  double pred_momentum_ems_ = 0;
  double prev_pred_px_ = 0;
  int64_t last_pred_momentum_update_t_ = 0;

  // Passive exit: after crossing into a position, place a GTC limit order
  // to take profit passively. Adapted from SimpleCross.
  bool passive_exit_enabled_ = false;
  // Delay after fill before placing passive exit order.
  std::chrono::milliseconds passive_exit_delay_ = std::chrono::milliseconds(2 * 60 * 1000);
  // Target profit as a fraction (e.g. 0.001 = 10bps).
  double passive_exit_pct_ = 0.001;
  // 0: relative to avg exec px, 1: relative to remote signal, 2: relative to local mid
  int passive_exit_style_ = 0;
  // Decay: reduce profit target by this much per minute.
  double passive_exit_decay_per_min_ = 0.0005;
  // Floor for profit target.
  double passive_exit_min_pct_ = 0.0002;
  // How often to refresh (cancel + re-place at updated price).
  std::chrono::seconds passive_exit_refresh_t_ = std::chrono::seconds(60);
  // Time of last fill (for decay calculation).
  int64_t last_fill_t_ = 0;

  // Passive exit order state.
  bool passive_exit_live_ = false;
  bool passive_exit_inflight_ = false;
  bool passive_exit_refresh_scheduled_ = false;
  bool passive_exit_place_scheduled_ = false;  // A placePassiveExit timeout is pending.
  SimpleOrder passive_exit_order_;

  // Premium EMA tracking (mode 0 only, from RelWideMM2).
  double premium_tdc_ = 1000 * 60 * 20;
  int64_t last_premium_adjust_t_ = 0;
  double cur_premium_ = 0;
  double premium_ema_ = 0;
  double premium_ema_coef_ = 0;
  double premium_adjusted_pred_px_ = 0;

  // Shared vol/liq mechanisms.
  VolMechanisms vol_mechs_;

  // Rate limiting.
  int64_t ms_between_cross_ = 1000;
  int64_t last_t_cross_ = 0;

  // Spread-aware entry filter: only fire if the dislocation exceeds
  // the local spread by at least min_edge_bps (in bps of pred_px).
  // This directly enforces "don't trade if edge < cost." When spread
  // widens (which correlates with vol spikes), the filter auto-backs-off.
  // 0 = disabled.
  double min_edge_over_spread_bps_ = 0;

  // Cross pricing mode:
  // 0: cross at best bid/ask (default, conservative)
  // 1: cross at pred_px (price up to fair value, more aggressive fill rate)
  // 2: cross at pred_px - thresh (price up to fair value minus threshold)
  int cross_price_mode_ = 0;

  // Signal-proportional sizing: scale order size by signal strength.
  // size = order_size * min(premium / threshold, size_signal_max_mult)
  // 0 = disabled (always use order_size).
  double size_signal_max_mult_ = 0;

  // Hold time exit decay: ease exit threshold over time.
  // After holding for hold_decay_tdc_s_ seconds, exit_adjust approaches
  // hold_decay_floor_ (e.g. 0.1 = after enough time, thresh drops to 10% for exits).
  double hold_decay_tdc_s_ = 0;  // 0 = disabled.
  double hold_decay_floor_ = 0.1;
  // Forced IOC exit after this many seconds holding. 0 = disabled.
  double forced_exit_after_s_ = 0;
  int64_t last_entry_t_ = 0;

  // Remote momentum extrapolation on pred_px.
  // Tracks a fast EMS of remote signal changes, then:
  //   adjusted_pred_px += remote_mom_coef_ * remote_move_ems_
  double remote_mom_tdc_ms_ = 1000;  // ~1s default
  double remote_mom_coef_ = 0;       // 0 = disabled
  double remote_mom_max_thresh_frac_ = 0;  // cap final momentum px adjustment; 0 = disabled
  double remote_move_ems_ = 0;
  double last_remote_mom_adjust_px_ = 0;
  double last_remote_mom_cap_px_ = 0;
  double prev_remote_val_ = 0;
  int64_t last_remote_mom_t_ = 0;

  // SNR (signal-to-noise ratio) threshold scaling.
  //
  // A 10bps remote move means different things depending on context:
  //   - If the remote has been jittering ±15bps/s, 10bps is noise.
  //   - If the remote has been flat and then moves 10bps, it's a real signal.
  //
  // We track the "noise level" as an EMS of recent absolute remote returns
  // (snr_vol_ems_, decaying with snr_vol_tdc). Then on each fire:
  //
  //   dislocation = |pred_px - local_mid| / pred_px   (the "signal")
  //   noise       = snr_vol_ems_
  //   SNR         = dislocation / noise
  //
  // High SNR → clean move relative to noise → reduce threshold (cross sooner).
  // Low SNR  → dislocation is within normal jitter → keep threshold unchanged.
  //
  //   effective_thresh = base_thresh / max(SNR * snr_thresh_coef, 1.0)
  //
  // snr_thresh_coef = 0 disables this feature.
  double snr_thresh_coef_ = 0;
  double snr_vol_tdc_ms_ = 5000;  // 5s default for noise EMS
  double snr_vol_ems_ = 0;        // Running noise estimate (abs return EMS)
  int64_t last_snr_vol_t_ = 0;


  // Flip-through: when holding a position and the signal exceeds the full
  // (non-exit-adjusted) threshold, size the order to flip through flat to
  // order_size on the other side, rather than just flattening.
  bool flip_through_ = false;

  // Trade rate conditioning.
  // Oracle DP shows tk_trade_rate is consistently negative across all symbols —
  // when trade rate is high (noisy/crowded market), crossing is less profitable.
  // Tracks local trade count EMS, widens threshold when rate exceeds baseline.
  //   widen = trade_rate_widen_coef * max(rate_ems / rate_baseline - 1, 0) * base_thresh_px
  // 0 = disabled.
  double trade_rate_widen_coef_ = 0;
  double trade_rate_tdc_ms_ = 10000;    // 10s default for rate EMS
  double trade_rate_ems_ = 0;           // Running trade count EMS
  double trade_rate_baseline_ = 0;      // Long-term baseline rate EMS
  double trade_rate_baseline_tdc_ms_ = 120000;  // 2min baseline
  int64_t last_trade_rate_t_ = 0;

  // Precog-miss-aware threshold tightening.
  //
  // TradeRiskMan tracks every outbound IOC and watches local-book LvlMod
  // (qty -> 0) and LvlDel events. If the level we were targeting
  // disappears within risk.precog_fastest_possible_rtt (seconds), it
  // fires ordElimPrecog(side, qty) — meaning "you almost got that;
  // someone won the race by a hair." We respond by accumulating a
  // signed decaying impulse (+ for buy misses, − for sell misses) and
  // multiplying the matching side's threshold by a smaller-than-1 adjust:
  //   adjust = (1 − miss_max_adjust) * exp(−|effective_da|) + miss_max_adjust
  // where effective_da is the accumulator decayed by elapsed time.
  // 0 = disabled. Recommended start: max_adjust=0.7, tdc_s=20, per_unit=2.
  // Requires risk.precog_miss=true and risk.precog_fastest_possible_rtt set.
  bool precog_miss_da_enabled_ = false;
  double precog_miss_max_adjust_ = 0.7;
  int64_t precog_miss_da_tdc_ms_ = 20 * 1000;
  double precog_miss_da_per_unit_ = 2.0;
  double precog_miss_da_ = 0;
  int64_t last_precog_miss_t_ = 0;
  double last_buy_miss_adjust_ = 1.0;
  double last_sell_miss_adjust_ = 1.0;

  // Feed-lag-aware threshold widening (regime-aware backoff for HL bursts).
  //
  // During HL server-wide congestion (e.g. macro-release moments, market open),
  // HL's feed publication and order processing both slow down 4-10x —
  // baseline feed lag ~485ms can spike to 3-5s for ~50-second windows. Our
  // IOCs sent during these windows arrive stale and get adversely selected.
  // We measured this directly in June 2026; see overmind/studies/svl_lag_aware_iteration_20260609.md.
  //
  // Mechanism: track (now - trd.transact_t) on every HL trade (own symbol or
  // BTC heartbeat), then widen threshold proportional to lag excess:
  //   widen = feed_lag_widen_coef * lag_excess_s * base_thresh_px
  // where lag_excess_s = max(0, last_feed_lag_ms - feed_lag_baseline_ms) / 1000.
  // Applied to both buy and sell thresholds (entries and exits both miss
  // during congestion; exits during burst arguably should hold).
  //
  // 0 = disabled. Recommended start: feed_lag_widen_coef ~ 0.5-1.0,
  // feed_lag_baseline_ms ~ 500.
  double feed_lag_widen_coef_ = 0;
  int64_t feed_lag_baseline_ms_ = 500;
  int64_t last_feed_lag_ms_ = 0;

  // TODO: Add optional alpha signal (pred_sig) support, similar to AlphaRelWideMM.
  // Would bias pred_px via (1 + alpha_mult * alpha) to incorporate directional predictions.
  // See alpha_rel_wide_mm.h for reference implementation.

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

  double min_tick_ = 0;
  bool needs_first_fire_ = true;
};

} // namespace pktrade::ordex
