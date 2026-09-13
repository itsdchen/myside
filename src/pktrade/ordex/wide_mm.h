#pragma once

#include "ordex.h"
#include "vol_mechanisms.h"

#include <algorithm>

#include "pktrade/mktdata/md_beacon.h"

#include "pktrade/tempos/base_tempo.h"
#include "pktrade/tempos/tempo_factory.h"

#include "pktrade/risk/trade_risk_man.h"
#include "pktrade/signals/sig_liq_balance.h"
#include "pktrade/signals/signal.h"
#include "pktrade/signals/signal_factory.h"

/*

MM strategy that considers wider spreads.

It never places backlevels on the inside. It could possibly do farther-out
backlevels though.

*/

namespace pktrade::ordex {

class WideMM : public Ordex,
               public pktrade::tempos::TempoListener,
               public pktrade::md::BeaconListener,
               public pktrade::signals::SignalListener {
 public:
  WideMM(SymbolId symbol, const rapidjson::Value& ordex_conf, pktrade::signals::SignalFactory* sf,
         pktrade::tempos::TempoFactory* tf);

  void postSecMaster() override;

  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  signals::Signal* getPredSignal() override;
  signals::Signal* getMidSignal() override;

  // tl5 mode.
  void aggFlat();

  void onTempo(int tempo_id);
  void tryFire();

  void trackReturn(double enter_px, bool is_buy, int oid);

  // Let's not do that just yet. 
  void hitMinFv() override {  };

  void updateThreshes();

  bool canCross(); 
  void maybeCross(const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                  double ordex_allowed_sell, 
                  // As opposed to a predicted px.
                  double alpha_pct_scale, double cross_thresh);

  void maybeJoin(const LevelBook* lvl_book, double cur_pos,
                 double ordex_allowed_buy, double ordex_allowed_sell);

  void maybeCancel(
                   const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                   double ordex_allowed_sell);

  void manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy,
                        double abs_allowed_sell);

  void onFirstFire();

  // We have several mechanisms to adjust place threshes. Redo them all here.
  void recalcThreshes();

  // Helpers for place_thresh_mode 3/4/5 (RelWideMM2-style liquidation-based
  // place thresholds). parsePlaceThreshLiqConfig reads config values;
  // initPlaceThreshLiqSignal builds the internal SigLiqBalance once ref_sig_
  // is available; calcPlaceThreshAutoLiqNotional sizes the auto-notional for
  // mode 5 from the local inside book.
  void parsePlaceThreshLiqConfig(const rapidjson::Value& ordex_conf);
  void initPlaceThreshLiqSignal(const rapidjson::Value& ordex_conf,
                                pktrade::signals::SignalFactory* sf);
  double calcPlaceThreshAutoLiqNotional();


  void setThresh(double thresh) override;
  void setThreshMult(double thresh_mult, MultSource src) override;
  void setOrderSize(double size) override;
  void setSizeMult(double size_mult, MultSource src) override;
  Quantity getMaxPos() override;

  SymbolId getTradedSymbols();
  std::vector<Market> getMarkets();
  std::vector<pktrade::BookId> getBookIds();

  // callbacks from risk.
  void newOrdAck(const Order& ord) override;
  void ordUpdate(const Order& ord) override;
  void ordCancel(const Order& ord) override;

  void ordExec(const Order& ord, const OrderExecute& exec) override;
  void ordElim(const Order& ord, const OrderElimination& elim) override;
  // No precog.
  void ordElimPrecog(Side side, Quantity qty) override {};

  void ordReject(const Order& ord, const NewOrderReject& rej) override;

  void flushUnhandledRejectedOrds();

  void cancelOutstandingOrds();

  void printOutstandingOrders();

  bool canCancelOrd(SimpleOrder& ord, bool fast = false);

  // Callbacks from MDBeacon
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) {};

  // For dynamic thresholds based on midmoves.
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override {};

  // Do decays for things that would otherwise decay on infrequent instances.
  // For example, narrow_when_exec, that previously only decayed on new execs,
  // which is very wrong
  void applyExecDecays();

 protected:
  std::vector<Market> traded_books_;
  BookId traded_bid_;

  // Ordex maxpos.
  Quantity max_pos_;

  // Fixed order size.
  Quantity order_size_;

  // Lets people specify this in dollars.
  // We set these on first fire, and then round to nearest integer.
  bool set_size_from_notional_ = false;
  // conservative but real defaults.
  double tgt_order_notional_ = 1000;
  double tgt_maxpos_notional_ = 10000;


  // Maybe just have a multiple or something.
  Quantity cross_order_size_;

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
  double base_cross_order_size_ = 0;
  double base_tgt_order_notional_ = 0;
  double base_tgt_maxpos_notional_ = 0;
  double base_pcurv_frac_denom_ = 0;

  double effSizeMult() const { return conf_size_mult_ * um_size_mult_ * dd_size_mult_; }

  void refreshSizeMultBaseFromCurrent();

  double alpha_mult_;

  double base_place_thresh_conf_;
  double base_cancel_buffer_conf_;

  // One thresh mult slot per MultSource; the effective mult is the widest of
  // them, so a defensive widen can't be clobbered by an unrelated writer
  // resetting to 1.
  double um_thresh_mult_ = 1;
  double bleed_thresh_mult_ = 1;
  double minfv_thresh_mult_ = 1;

  double effThreshMult() const {
    return std::max({um_thresh_mult_, bleed_thresh_mult_, minfv_thresh_mult_});
  }

  // buy/sell threshes based on semipermanent changes (eg. if we got a usermsg
  // telling us to widen buyside by 1.5x)
  double mid_thresh_;

  double cancel_buffer_;
  // Cancel buffer as a fraction of place_thresh. Used by modes 3/4/5 to keep
  // the cancel hysteresis proportional to the (now dynamic and sided) place
  // thresholds; under modes 0-2 we fall back to the static cancel_buffer_px_.
  double cancel_buffer_frac_ = 0;

  // In price units (versus pct units)
  double place_thresh_px_ = 0;
  double cancel_buffer_px_ = 0;

  // The things that are actually used for placement.
  // These get recalculated every tradecall.
  double place_thresh_buy_ = 0;
  double place_thresh_sell_ = 0;
  // Sided cancel buffers. Under modes 0-2 these equal cancel_buffer_px_;
  // under modes 3/4/5 they scale with the per-side place_thresh so the
  // cancel zone stays a constant fraction of the placement zone.
  double cancel_buffer_buy_px_ = 0;
  double cancel_buffer_sell_px_ = 0;

  // 0 == normal (eg. place_thresh = place_thresh)
  // 1 == place_thresh + place_thresh_spread_coef * spread
  // 2 == place_thresh + place_thresh_spread_coef * spread_ema
  // 3 == fixed-notional liquidation spread (symmetric)
  // 4 == fixed-notional sided liquidation distance
  // 5 == auto-notional sided liquidation distance
  // I took a brief look over these, and it looks like maybe
  // mode1 and mode2 might have some better per-trade pnls
  // than the others. So maybe it's worth it?
  int place_thresh_mode_ = 0;
  double place_thresh_spread_coef_ = 0.5;
  // RelWideMM2 ports modes 3/4/5: per-side place threshold derived from a
  // SigLiqBalance instance that prices sweeping a fixed (or auto-sized)
  // notional through the book.
  std::unique_ptr<pktrade::signals::SigLiqBalance> place_thresh_liq_sig_;
  // 0 == add liquidation threshold to base threshold, 1 == max(base, liquidation).
  int place_thresh_liq_combine_mode_ = 0;
  double place_thresh_liq_coef_ = 0.5;
  double place_thresh_liq_notional_ = 10000;
  double place_thresh_liq_per_level_decay_ = 1.0;
  double place_thresh_liq_sz_decay_ = 1.0;
  double place_thresh_liq_sz_decay_2_ = 1.0;
  double place_thresh_liq_min_sz_wt_ = 1e-12;
  bool place_thresh_liq_use_flagged_ = true;

  // Mode 5 only: auto-notional EMS tuned to inside-size.
  double place_thresh_liq_auto_inside_mult_ = 1.0;
  double place_thresh_liq_auto_min_notional_ = 0;
  double place_thresh_liq_auto_max_notional_ = 1e12;
  double place_thresh_liq_auto_tdc_ms_ = 60 * 1000;
  double place_thresh_liq_auto_notional_ems_ = 0;
  int64_t place_thresh_liq_auto_last_t_ = 0;

  bool mm_gtc_;
  TimeInForce tif_;

  // rung_spacing_mult is a fxn of place_thresh. We'll set it when we get the
  // first midprice.

  // If set, lets us cut in front of the front order by less than a whole
  // thresh.
  // This way, lets us follow the book more aggressively while it's moving.
  // Similar to something we've talked about before.
  double front_rung_spacing_mult_ = 1;

  double rung_spacing_mult_;

  // A parameter for sparsifying as we place more backlevels.
  // If this is 1, then all backlevel rungs are equal. If it is something like
  // 1.1, then the i-th rung is 1.1^(num_orders) times the rung spacing.
  // Note: only used for placing new back levels, not for canceling/moving
  // existing ones when we insert a new back level somewhere in the middle
  double per_backlevel_rung_spacing_mult_;

  double rung_spacing_px_;
  // If we want to specify rungage independently of the place thresh.
  double fixed_rung_spacing_thresh_ = 0;

  bool adding_no_cutin_ = true;

  double cross_thresh_;
  double cur_cross_thresh_;
  bool want_cross_;

  int max_back_levels_;

  // Backlevel sparsity (weekend protection #1): when > 0, require at least
  // min_foreign_inside_mult_ * inside_size_{buy,sell}_ems_ shares of foreign
  // liquidity (effectiveQty minus our own) between a new backlevel and the
  // previous same-side order. Sided: buy backlevels use bid-side EMA, sells use
  // ask-side. Self-scales across thick/thin symbols. 0 = disabled.
  double min_foreign_inside_mult_ = 0;

  // Local-liquidity size cap (weekend protection #2): when > 0, cap inside
  // (front) placement qty at local_size_cap_mult_ * inside_size_{buy,sell}_ems_.
  // Sided: buy uses bid-side, sell uses ask-side. Backlevels remain at full
  // order_size_; their protection is min_foreign_inside_mult_ instead.
  // 0 = disabled.
  double local_size_cap_mult_ = 0;
  double local_size_cap_tdc_ms_ = 60000;
  // Sided foreign-depth EMAs: buy uses bid-side, sell uses ask-side.
  double inside_size_buy_ems_ = 0;
  int64_t inside_size_buy_last_t_ = 0;
  double inside_size_sell_ems_ = 0;
  int64_t inside_size_sell_last_t_ = 0;

  // Exec-anchored mid (weekend protection #3): size-weighted EMA of trade
  // px*qty and qty, reduced to a VWAP of recent trade prints. Used to blend
  // against displayed mid (pick the less-favorable-to-us side), so phantom
  // book moves can't drag pred_px before real trades confirm. 0 = disabled.
  double exec_anchored_mid_tdc_ms_ = 0;
  double exec_vwap_num_ems_ = 0;
  double exec_vwap_denom_ems_ = 0;
  int64_t exec_vwap_last_t_ = 0;

  // Per-side pred_px, populated each fire. When exec-anchored mid is off,
  // both equal pred_px_.
  double pred_px_buy_ = 0;
  double pred_px_sell_ = 0;

  // Returns capped order size (as Quantity, rounded down) if cap is active,
  // else returns order_size_ unchanged. Sided: buy uses bid-side EMA, sell ask.
  Quantity getEffectiveOrderSize(Side side) const;

  // If false, we don't place back levels, just let front levels become back levels
  bool place_back_levels_ = true;

  // if true, will try to cancel if alone at top with no level behind me.
  bool will_cancel_isolated_ = false;

  // Thresh to use for TL2. Bypasses all adjustments above. Cancel thresh scales from this in TL2.
  double TL2_aggr_exit_thresh_;


  // If ladder_ticks is set on, then we use THAT instead of curvicity
  // to determine offset.
  bool use_ladder_ticks_;
  bool ladder_one_sided_;
  double per_order_widen_frac_;

  // We use this to scale pcurvicity at a saturating point other than the
  // maxpos. For example: Suppose maxpos = 8x order_size. Well, we can set
  // pcurv_frac_denom to be 4, so that the current fraction used for pcurv is
  // actually 4x ordersize instaed of 8x.
  double pcurv_frac_denom_;

  // Introduces an offset to the placement thresholds, based on the pos/maxpos
  // fraction, and specified by the function y = x^(pc). It takes a value from
  // [0, 1]
  double pcurvicity_;

  // Multiply the above fxn by this value. Usually I might just set this at 1.
  // But maybe can multiply too.
  double pcurvicity_coef_;

  // Curv impulse: one-sided widening based on recent fills.
  // If we just bought, widen buy side (make it harder to buy more).
  double curv_impulse_tdc_ms_ = 1000;
  double curv_impulse_coef_ = 0;
  double curv_impulse_ = 0;

  // Insert and delete from the front- and back-
  std::vector<SimpleOrder> buy_orders_;
  std::vector<SimpleOrder> sell_orders_;

  // This is the pk_coids that were rejected, but which we couldn't yet clear out.
  std::vector<PKOrderId> unhandled_rejected_pkcoids_;

  // Some natural rate limiters.
  // dont cancel an ord if it's been alive for less than this long.
  int64_t ms_min_ord_lifetime_;
  int64_t ms_between_place_ = 100;
  int64_t ms_between_cxl_ = 100;
  int64_t ms_between_cross_ = 100;
  int64_t ms_between_backlevel_ = 2000; // 2s to reduce backlevel order spam

  int64_t last_t_place_;
  int64_t last_t_cxl_;
  int64_t last_t_cross_;
  int64_t last_t_backlevel_;

  pktrade::signals::Signal* ref_sig_;
  pktrade::signals::Signal* pred_sig_;

  int num_fires_ = 0;
  int64_t now_fire_t_ = 0;


  // Again, min_tick to figure out the
  double min_tick_;

  bool needs_first_fire_;
  double approx_mid_;

  bool print_ords_;

  // After we place a cancel, if we don't hear back within this time, we let
  // ourselves try again.
  // I increased this to 12s because I am observing 10s rtt's for cancels (esp
  // if I overload the machine).
  int64_t CANCEL_RETRY_INTERVAL = 12 * 1000;

  // When we TL5, how far away are we letting ourselves cross to get flat.
  // For hip3 things, we may not want to crossout. 
  // In that case, we wait for tl6 to cross out. 
  bool tl5_crossout_ = false;

  // ---------------------------------------------------------------
  // Potentially scale thresh based on midmove, relative to spread_ema.

  // Compare the midmove to the spread size and use that to modulate
  // the thresh.
  bool scale_thresh_midmove_spread_;

  /*
   This is the modification style.

    double nu_mid_ratio = abs_mid_move_ems_ / spread_pct;
    modifier = scale_midmove_lower_bound_ +
               (1 - scale_midmove_lower_bound_) *
                   (std::exp(-nu_mid_ratio / scale_thresh_mids_const_));

  */

  double scale_thresh_mids_const_;

  // If we let the midmoves change our working thresh, we need to impose a lower
  // bound on how small that will go. If 0.3, then the smallest modifier will be
  // 0.3x the original threhs.
  double scale_midmove_lower_bound_;
  double scale_midmove_mult_;

  // scale thresh dynamically.
  // If true, will adopt a biasing scheme like:
  /*

    Let's just call this spreadscale_v2
    // I'm going to rename some the params above because
    // it might get confusing between one version and another.

    Base case:
    activity_ratio = abs_mid_ems / spread_ema / scale_thresh_mids_const
    scaling_factor = scale_midmove_lower_bound_ + scale_midmove_mult_ * (activity_ratio - 1.0)

    If bias_direction, will consider max(0, net_mid_ems/abs_mid_ems)
    bias = state.decayed_returns / state.abs_decayed_returns
    In that case, we multiply the scaling factor by

    buying:
    (1 - bias * bias_coef) * scaling_factor
    selling:
    (1 + bias * bias_coef) * scaling factor

    That way, if bias is positive, we make it easier to buy, and
    vice versa.
    bias_coef is gonna have to be in [0, 1]

    Also:
    momentum = abs(state.decayed_returns) / (state.abs_decayed_returns + 1e-10)

    # Combine with overall activity level
    activity_ratio = state.abs_decayed_returns / state.base_spread

    scaling_factor = 1.0 + self.sensitivity * (activity_ratio * (1 + momentum))

  */

  bool spreadscale_v2_;

  // If so, gives a biasing (asymmetric) factor
  bool spreadscale_v2_bias_;
  double spreadscale_v2_bias_coef_;

  // Uses the momentum term...
  bool spreadscale_v2_momentum_;

  // Looking for functions that == 0 at 0 and increase to the right.
  // 0: linear (y=x)
  // 1: sqrt(x)
  // 2: log(1+x)
  // 3: 1 - exp(-x)
  int spreadscale_v2_shape_;

  // If we let the midmoves change our working thresh, we need to impose a lower
  // bound on how small that will go. If 0.3, then the smallest modifier will be
  // 0.3x the original threhs.
  double spreadscale_v2_mult_;

  // Normalizing constant for things (eg. if we divide some EMS by the spread)
  double spreadscale_v2_denom_const_;

  // If we do momentum-based biasing.
  double cur_buy_bias_mult_ = 1;
  double cur_sell_bias_mult_ = 1;

  // This also uses the abs_mid_move_ems_ var.
  // Also keeps track of spread
  double local_spread_num_;
  double local_spread_denom_;
  double spread_ema_;
  double local_spread_;

  // ---------------------------------------------------------------
  // saved stuff from each tradecall.
  double mid_px_ = 0;
  double pred_px_ = 0;

  // ---------------------------------------------------------------
  // We should be doing this decay more...
  int64_t last_exec_decay_t_;

  // ---------------------------------------------------------------
  // dynamic thresholding based on market moves.
  // Track an EMS of how much the market has been moving, and scale threshold
  // w.r.t. that.

  double net_mid_move_ems_;
  // Use the net move or not.
  double abs_mid_move_ems_;
  double last_mid_px_;
  double mid_move_ems_time_const_ms_;
  int64_t last_mid_t_;

  // ---------------------------------------------------------------
  // Dynamic threhsolding based on recent (in)-activity
  // Track when our last trade was, and adjust threshold down, accordingly.

  // For narrowing markets after a long time of no action
  bool narrow_when_few_trades_;

  // THe most we can decrease our thresh, due to this exec thing.
  double narrow_fewtrade_ems_buy_ = 0;
  double narrow_fewtrade_ems_sell_ = 0;

  // THe most we can decrease our thresh, due to this exec thing.
  double narrow_fewtrade_minmultiple_;
  double narrow_fewtrade_denom_;

  // Time decay const, in ms units.
  double narrow_thresh_time_const_ms_;

  // Shared vol/liq mechanisms (1-4).
  VolMechanisms vol_mechs_;

  static bool compareSellOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px < rord.px); }

  static bool compareBuyOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px > rord.px); }
};

} // namespace pktrade::ordex
