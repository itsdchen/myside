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

MM strategy that considers wider spreads.

It never places backlevels on the inside. It could possibly do farther-out
backlevels though.

*/

namespace pktrade::ordex {

class AlphaRelWideMM : public Ordex,
                       public pktrade::tempos::TempoListener,
                       public pktrade::signals::SignalListener,
                       public pktrade::md::BeaconListener {
 public:
  AlphaRelWideMM(SymbolId symbol, const rapidjson::Value& ordex_conf,
                 pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf);

  void postSecMaster() override;

  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  signals::Signal* getPredSignal() override;
  signals::Signal* getMidSignal() override;

  // tl5 mode.
  void aggFlat();

  void onTempo(int tempo_id);
  void tryFire();

  void trackReturn(double enter_px, bool is_buy, std::string prefix_str);

  void updateThreshes();
  bool canCross();

  void maybeCrossV2(const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                    double ordex_allowed_sell);

  void maybeJoin(const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                 double ordex_allowed_sell);

  void maybeCancel(const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                   double ordex_allowed_sell);

  void manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy, double abs_allowed_sell);

  void onFirstFire();

  // Given a string, print it in the futuer for great returns.
  void calcPrintReturns(std::string prefix_str, bool buy_dir);

  // We have several mechanisms to adjust place threshes. Redo them all here.
  void recalcThreshes();

  void updatePredMomentumEms();

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

  // Maybe precog.
  void ordElimPrecog(Side side, Quantity qty) override;

  void ordReject(const Order& ord, const NewOrderReject& rej) override;

  void cancelOutstandingOrds();

  void printOutstandingOrders();

  bool canCancelOrd(SimpleOrder& ord, bool fast = false);

  // For dynamic thresholds based on midmoves.
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override {};

  // Callbacks from MDBeacon
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk);

  // See wide_mm.h for description. Inside-only cap (manageBacklevels uses
  // full order_size_; backlevels are protected by min_foreign_inside_mult_).
  // Sided: buy uses bid-side EMA, sell uses ask-side.
  Quantity getEffectiveOrderSize(Side side) const;

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

  double lmr_ema_mult_ = 1;

  bool want_add_;
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

  // In price units (versus pct units)
  double place_thresh_px_ = 0;
  double cancel_buffer_px_ = 0;

  // The things that are actually used for placement.
  // These get recalculated every tradecall.
  double place_thresh_buy_ = 0;
  double place_thresh_sell_ = 0;

  // 0 == normal (eg. place_thresh = place_thresh)
  // 1 == place_thresh + place_thresh_spread_coef * spread
  // 2 == place_thresh + place_thresh_spread_coef * spread_ema
  int place_thresh_mode_ = 0;
  double place_thresh_spread_coef_ = 0.5;

  bool mm_gtc_;
  TimeInForce tif_;

  // rung_spacing_mult is a fxn of place_thresh. We'll set it when we get the
  // first midprice.

  // If set, lets us cut in front of the front order by less than a whole
  // thresh.
  // This way, lets us follow the book more aggressively while it's moving.
  // Similar to something we've talked about before.
  double front_rung_spacing_mult_ = 1;

  // A parameter for sparsifying as we place more backlevels.
  // If this is 1, then all backlevel rungs are equal. If it is something like
  // 1.1, then the i-th rung is 1.1^(num_orders) times the rung spacing.
  // Note: only used for placing new back levels, not for canceling/moving
  // existing ones when we insert a new back level somewhere in the middle
  double per_backlevel_rung_spacing_mult_;

  double rung_spacing_mult_;
  double rung_spacing_px_;

  // If we want to specify rungage independently of the place thresh.
  double fixed_rung_spacing_thresh_ = 0;

  bool adding_no_cutin_ = true;

  bool want_cross_;

  // Second cross version...
  // Using the relative position of the market. And compares it against the
  // bb/ba of the inside.
  double cross_v2_lspread_thresh_;
  double cur_cross_thresh_;

  // Must be <=1.
  double cross_v2_exit_adjust_;

  // If we're using lmr to dampen, we don't use the full strength of the lmr prediction
  // when crossing.  Basically an alpha mult for the remote.
  double cross_v2_lmr_dampener_ = 1;

  // If we cross, subtract the crossing thresh from the price that we fire to.
  bool cross_v2_sub_thresh_;

  // scale order size by the magnitude of how much over we went.
  // Effectively, an MPO.
  bool cross_v2_resize_w_thresh_;

  // 0 == use the remote_sig + lmr prediction
  // 1 == use the actual prediction.
  int cross_v2_pred_mode_;

  int max_back_levels_;

  // Backlevel sparsity (weekend protection #1). See wide_mm.h. 0 = disabled.
  double min_foreign_inside_mult_ = 0;

  // Local-liquidity size cap (weekend protection #2). Inside-only. 0 = disabled.
  double local_size_cap_mult_ = 0;
  double local_size_cap_tdc_ms_ = 60000;
  // Sided foreign-depth EMAs: buy uses bid-side, sell uses ask-side.
  double inside_size_buy_ems_ = 0;
  int64_t inside_size_buy_last_t_ = 0;
  double inside_size_sell_ems_ = 0;
  int64_t inside_size_sell_last_t_ = 0;

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

  // If true, the offset becomes a less extreme offset.
  bool pcurv_bounded_decrease_;

  // We use this to scale pcurvicity at a saturating point other than the maxpos. For example:
  // Suppose maxpos = 8x order_size. Well, we can set pcurv_frac_denom to be 4, so that
  // the current fraction used for pcurv is actually 4x ordersize instaed of 8x.
  double pcurv_frac_denom_;

  // Introduces an offset to the placement thresholds, based on the pos/maxpos
  // fraction, and specified by the function y = x^(pc). It takes a value from
  // [0, 1]
  double pcurvicity_;

  // Multiply the above fxn by this value. Usually I might just set this at 1.
  // But maybe can multiply too.
  double pcurvicity_coef_;

  // Insert and delete from the front- and back-
  std::vector<SimpleOrder> buy_orders_;
  std::vector<SimpleOrder> sell_orders_;

  // This should be smarter. But oh well.
  std::vector<int64_t> action_times;

  // For rate limiting, how far back in time are we looking.
  int64_t action_time_window_;

  // Some natural rate limiters.
  // dont cancel an ord if it's been alive for less than this long.
  int64_t ms_min_ord_lifetime_;
  int64_t ms_between_place_ = 100;
  int64_t ms_between_cxl_ = 100;
  int64_t ms_between_cross_ = 10000;
  int64_t ms_between_backlevel_ = 2000; // 2s to reduce backlevel order spam

  int64_t last_t_place_;
  int64_t last_t_cxl_;
  int64_t last_t_cross_;
  int64_t last_t_backlevel_;

  pktrade::signals::Signal* local_sig_;
  pktrade::signals::Signal* remote_sig_;
  pktrade::signals::Signal* pred_sig_;

  // ---------------------------------------------------------------
  // saved stuff from each tradecall.

  // Few styles of doing prediction in this lmr way.
  // pred_mode == 0: calculate alpha on remote,
  //                 get prediction on remote mid, and then
  //                 predicted price of local_sig is
  //                 (lmr_ema*coef + (1 + alpha) * (remote)
  //                 Basically, we think we can predict the reference
  //                 signal, and then use the lmr_ema to
  //                 offset to our local book
  // pred_mode == 1: calculate alpha on local.
  //                 prediction = (1 + alpha) * (lmr_ema*coef + remote)
  //                 Basically, we think we can predict our local book.
  //                 We use the lmr_ema to adjust tto the local book, and
  //                 predict how the local book adjusts when the remote
  //                 moves
  // TODO: figure out some method when the local book moves, but only
  //       slowly (eg. remote moves of 1 bp dont matter, but 4 bps does)

  double alpha_ = 0;

  int pred_mode_ = 0;

  // The "ideal mid" if we apply the lmr to the remote
  double shifted_local_mid_ = 0;
  // The "ideal" if we apply a signal to the local price too.

  double pred_px_ = 0;

  int local_sig_idx_;
  int remote_sig_idx_;

  double local_sig_val_ = 0;
  double remote_sig_val_ = 0;

  double local_minus_remote_;
  double local_minus_remote_denom_;
  double local_minus_remote_ema_;

  // Let's also start snapshotting reomte returns since our last local update.
  // Meaning, if the remote moved a LOT recently then we are more willing to believe that
  // the local *should* move.

  // Let's update this snapshot maybe every real local snapshot.
  double last_remote_snapshot_ = 0;
  double remote_diff_spread_ratio_ = 1;

  // Keep spread stats too.
  double lspread_ = 0;
  double lspread_ema_ = 0;
  double lspread_num_ = 0;
  double lspread_denom_ = 0;
  // For updating the local spread, which we use for crossv2 threshes.
  double lspread_time_const_;

  int64_t lspread_t_ = 0;

  double lmr_time_const_;
  int64_t last_lmr_update_t_;

  // For tracking local- and remote- returns too. Not just the
  // ema of the diff. I want to know where the momentum is coming from.
  double remote_return_ems_ = 0;
  double local_return_ems_ = 0;

  // Let's just... hardcode this to be 1s.
  double return_ems_time_const_ = 1000.0;

  // Let us filter cross_v2 in a few ways.
  // filter = 0 :  no filtering.
  // filter = 1 : filter on remote_return_ems
  // filter = 2 : filter on remote_return_ems vs local_return_ems
  // filter = 3 : remote_return_ems needs to be large relative to local_return_ems.
  //              So if we want it to go up, it needs to be 2x as big or something.

  int cross_v2_rel_return_filter_ = 0;

  int num_fires_ = 0;
  int64_t now_fire_t_ = 0;

  // Again, min_tick to figure out the
  double min_tick_;

  bool needs_first_fire_;
  double approx_mid_;

  bool print_ords_;

  // After we place a cancel, if we don't hear back within this time, we let
  // ourselves try again.
  // I increased this to 12s because I am observing 10s rtt's for cancels (esp if I overload the
  // machine).
  int64_t CANCEL_RETRY_INTERVAL = 12 * 1000;

  // When we TL5, how far away are we letting ourselves cross to get flat.
  // For hip3 things, we may not want to crossout.
  // In that case, we wait for tl6 to cross out.
  bool tl5_crossout_ = false;

  // When we TL5, how far away are we letting ourselves cross to get flat.
  double getflat_pct_away_;

  // ---------------------------------------------------------------
  // Potentially scale thresh based on midmove, relative to spread_ema.

  // Compare the midmove to the spread size and use that to modulate
  // the thresh.
  bool scale_thresh_midmove_spread_;

  /*
   This is the modification style.

    double nu_mid_ratio = abs_mid_move_ems_ / spread_ema_;
    modifier = scale_midmove_lower_bound_ +
               (1 - scale_midmove_lower_bound_) *
                   (std::exp(-nu_mid_ratio / scale_thresh_mids_const_));

  */

  double scale_thresh_mids_const_;

  // If we let the midmoves change our working thresh, we need to impose a lower
  // bound on how small that will go. If 0.3, then the smallest modifier will be
  // 0.3x the original threhs.
  double scale_midmove_lower_bound_;

  // This also uses the abs_mid_move_ems_ var.
  // Also keeps track of spread
  double remote_spread_num_;
  double remote_spread_denom_;
  double remote_spread_ema_;

  // ---------------------------------------------------------------

  bool spreadscale_v2_;
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

  // ---------------------------------------------------------------
  // Similar to simple_cross, we do a passive-out to get rid of our srisk.
  int64_t last_exec_t_;
  // We should be doing this decay more...
  int64_t last_exec_decay_t_;

  // ---------------------------------------------------------------
  // dynamic thresholding based on market moves.
  // Track an EMS of how much the market has been moving, and scale threshold
  // w.r.t. that.

  // Tracking mid move ema's for dynamic thresholding.
  bool thresh_mid_sigmoid_scale_;

  double net_remote_mid_move_ems_;

  // Use the net move or not.
  double abs_remote_mid_move_ems_;
  double remote_mid_ems_time_const_ms_ = 120 * 1000;

  int64_t last_local_mid_t_;

  // t of last remote mid update.
  int64_t last_remote_mid_t_;

  // Upon local updates due to snapshot,
  // bias towards the local mid (versus the remote predpx)
  // by this factor.
  // This is kind of experimental.
  double local_reupdate_coef_ = 0;
  double local_reupdate_tdc_ms_ = 200;

  // How to go from ems to an actual modifier for the
  // wide_mm thresh.
  double dynamic_mid_sigmoid_base_;
  double dynamic_mid_sigmoid_offset_;
  double dynamic_mid_sigmoid_mult_;

  // How to scale.
  // 0: sigmoid.  offset + mult * (1 / (1+exp(-abs(ems)/base)))
  // 1: linear. offset + mult * (abs(ems)/base)
  // 2: log. mult * log(1 + abs(ems)/base) + offset
  // This is probably enough... jfc.
  int dynamic_mid_sigmoid_style_;

  // ---------------------------------------------------------------
  // Dynamic threhsolding based on recent (in)-activity
  // Track when our last trade was, and adjust threshold down, accordingly.

  // For narrowing markets after a long time of no action
  bool narrow_when_few_trades_;

  // The most we can decrease our thresh, due to this exec thing.
  double narrow_fewtrade_ems_buy_ = 0;
  double narrow_fewtrade_ems_sell_ = 0;
  // THe most we can decrease our thresh, due to this exec thing.
  double narrow_fewtrade_minmultiple_;
  double narrow_fewtrade_denom_;

  // Time decay const, in ms units.
  double narrow_thresh_time_const_ms_;

  // Shared vol/liq mechanisms (1-4).
  VolMechanisms vol_mechs_;

  // Lspread sanitization: only update lspread from non-trade book changes.
  bool sanitize_lspread_ = true;

  // ---------------------------------------------------------------
  // Branch 1: Denom-gated warmup widening.
  // Widens thresholds when LMR denom is low (untrusted FV).
  double warmup_widen_coef_ = 0;       // 0 = disabled
  double warmup_widen_denom_target_ = 0; // denom value considered "converged"

  // Only update LMR on local signal changes, not remote.
  bool lmr_local_only_ = false;

  // ---------------------------------------------------------------
  // Branch 4b: Trade-dampened LMR update.
  // When local mid moved due to a trade, dampen the LMR update.
  bool lmr_trade_dampen_enabled_ = false;
  double lmr_trade_dampen_factor_ = 0;  // 0 = skip entirely, 0.5 = half-weight

  // ---------------------------------------------------------------
  // Branch 6: Impulse-curvicity.
  // Self-braking: recent fills on one side widen that side's threshold.
  double curv_impulse_coef_ = 0;         // 0 = disabled
  double curv_impulse_tdc_ms_ = 1000;
  double curv_impulse_ = 0;

  // ---------------------------------------------------------------
  // Pred price momentum-based threshold adjustment.
  // Track recent pred price moves via EMS of returns and widen thresholds on the side
  // momentum is pointing to avoid getting picked off by sustained directional moves.
  double pred_momentum_tdc_ms_ = 1000 * 30;
  double pred_momentum_coef_ = 0;
  double pred_momentum_ems_ = 0;
  double prev_pred_px_for_momentum_ = 0;
  int64_t last_pred_momentum_update_t_ = 0;

  // ---------------------------------------------------------------
  // Branch 7: Remote return EMA one-sided widening.
  // If remote is trending, widen the side that would trade into the trend.
  double remote_return_widen_coef_ = 0;  // 0 = disabled

  // ---------------------------------------------------------------
  // Branch 8: LMR decay on non-trade local updates.
  // When local book genuinely reprices (not from a trade), decay LMR num/denom.
  bool lmr_local_book_decay_enabled_ = false;
  double lmr_local_book_decay_factor_ = 1.0;

  static bool compareSellOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px < rord.px); }

  static bool compareBuyOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px > rord.px); }
};

} // namespace pktrade::ordex
