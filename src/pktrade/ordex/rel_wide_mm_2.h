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
#include "pktrade/signals/sig_liq_balance.h"
#include "pktrade/util/sbcaller.h"

#include <memory>
#include <random>

// For websocket subscription.
#include <ixwebsocket/IXWebSocket.h>

/*

Basic mm strategy that prices based off of a remote signal (eg. the same symbol
but on a different venue. Should NOT assume that there is liquidity on the local
book.)

TOOD:

  Adding in a hyperliquid subscription for tracking oracle prices.



*/

namespace pktrade::ordex {

class RelWideMM2 : public Ordex,
                   public pktrade::tempos::TempoListener,
                   public pktrade::signals::SignalListener,
                   public pktrade::md::BeaconListener,
                   public pktrade::util::SBCaller

{
 public:
  RelWideMM2(SymbolId symbol, const rapidjson::Value& ordex_conf,
             pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf);

  void postSecMaster() override;

  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  signals::Signal* getPredSignal() override;
  signals::Signal* getMidSignal() override;

  // tl5 mode. not implemented.
  void aggFlat();

  void onTempo(int tempo_id);
  void tryFire();

  // If we have a direct feed to the product, this is just the remote price.
  // But if we do some beta-adjusted price, then we'll need to do something
  // extra.
  double translatePrice();

  // We should know when it actually expires.
  double discountPrice(double cur_px);

  // Update the thresholds on buy and sell sides. As well as the sided cancel
  // buffers.
  void updateThreshes();

  // If we do relative pricing, then we need to do periodic snapshots.
  // I want these to be synchronized between
  void snapshotRelativePricing();

  bool canCross();
  void maybeCross(const LevelBook* lvl_book, double ordex_allowed_buy, double ordex_allowed_sell);

  void maybePlaceFront(const LevelBook* lvl_book, double ordex_allowed_buy,
                       double ordex_allowed_sell);

  bool shouldCancelPx(double px, Side side, bool fast) const;
  void maybeCancel(const LevelBook* lvl_book, double ordex_allowed_buy, double ordex_allowed_sell);

  // 1 - place orders at the back so we can have fuller liquidity
  // 2 - cancel orders from the back (if we have too much outstanding risk)
  void manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy, double abs_allowed_sell);

  void onFirstFire(double pred_px);

  void updatePremiumEma();

  void updatePredMomentumEms(double discounted_final_px);

  // We have several mechanisms to adjust place threshes. Redo them all here.
  void recalcThreshes();

  void setThresh(double thresh) override;
  void setThreshMult(double thresh_mult, MultSource src) override;
  void setOrderSize(double size) override;
  void setSizeMult(double size_mult, MultSource src) override;

  void setRelativeBeta(double beta) override;

  void setExtraThreshes(double extra_buy, double extra_sell) override;

  void setConstPredPx(double px) override;

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

  void cancelOutstandingOrds() override;

  void backFromMinFv() override;

  void printOutstandingOrders();

  bool canCancelOrd(SimpleOrder& ord, bool fast = false);

  // For dynamic thresholds based on midmoves.
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override {};

  // Do decays for things that would otherwise decay on infrequent instances.
  void applyExecDecays();

  // We became a beaconlistener so we can calculate vwap inside here.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) {};

  // SBCaller interface.
  // I'm putting the declarations together, but the implementations next to where they get
  // initiated, in the .cc file.
  void onGetClosingPrint(const cpr::Response& res) override;
  void onGetSnapshot(const cpr::Response& res) override;
  void onSnapshotJson(rapidjson::Document& snapshot_doc);

  // If we're using equity_off_hour_vwap_, then we gotta look up the closing cross from supabase.
  void read_closing_cross_supabase();

  // If we use relative pricing - potentially use supabase to update on rel- and remote-
  // prices.
  void read_snapshots_supabase();

  double getRandomValue(bool front);

  // Generate a random order size.
  double genOrderSize(bool front);

 protected:
  void parsePlaceThreshLiqConfig(const rapidjson::Value& ordex_conf);
  void initPlaceThreshLiqSignal(const rapidjson::Value& ordex_conf,
                                pktrade::signals::SignalFactory* sf);
  double calcPlaceThreshAutoLiqNotional();

  std::vector<Market> traded_books_;
  BookId traded_bid_;

  // Ordex maxpos.
  Quantity max_pos_;

  // Fixed order size.
  Quantity order_size_;

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

  // Lets people specify this in dollars.
  // We set these on first fire, and then round to nearest integer.
  bool set_size_from_notional_ = false;
  // conservative but real defaults.
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

  // Allow for randomizing.
  double order_random_upper_limit_ = 1.7;
  double order_random_lower_limit_ = 0.3;

  // Keep the front closer to the encoded order size.
  double front_order_random_upper_limit_ = 1.3;
  double front_order_random_lower_limit_ = 0.7;

  std::mt19937 gen_;
  std::uniform_real_distribution<double> dis_;
  std::uniform_real_distribution<double> dis_front_;

  bool can_cross_;
  double cross_thresh_;     // from conf
  double cur_cross_thresh_; // currently used

  // Cross pricing mode (mirrors RelCross):
  // 0: cross at best bid/ask (default, conservative)
  // 1: cross at pred_px (price up to fair value, more aggressive fill rate)
  // 2: cross at pred_px -/+ cross_thresh (price up to fair value minus threshold)
  int cross_price_mode_ = 0;

  // Cross threshold multiplier when the cross would flatten (reduce) our
  // position, i.e. crossing to exit rather than enter. < 1 makes it easier to
  // cross out of inventory than into it. 1 = symmetric (mirrors RelCross).
  double exit_adjust_ = 1.0;

  // Prevent us from crossing in large size.
  // For example, if we're way off, I don't want us to aggressively cross against
  // a quote all the way to our maxpos. Set to 0 for exit-only crossing: the
  // guard then permits crossing only on the position-reducing side, and the
  // no-overshoot clamp in maybeCross keeps each such cross a pure exit.
  double cross_limit_maxpos_frac_ = 0.25;

  // Multiplier on order_size to set the base cross size (before allowed/exit
  // clamps). Crossing need not be limited to the same size as passive adds.
  double cross_sz_mult_ = 1.0;

  int64_t ms_between_cross_ = 1000;
  int64_t last_t_cross_ = 0;

  //////////////////////////////////////////////
  // The amounts of offset we allow ourselves

  // How much buffer (as a frac) we allow ourselves to keep orders,
  // relative to the place thresh.
  double base_place_thresh_conf_;
  double base_cancel_buffer_conf_;

  // Lowest value cancel threshes can go to.
  double min_cxl_thresh_ = 0.000025;

  double base_cancel_thresh_conf_ = 0;

  // buy/sell threshes based on conf, but could be overriden by setThresh usermsg
  double base_buy_thresh_;
  double base_sell_thresh_;

  // 0 == normal
  // 1 == place_thresh + place_thresh_spread_coef * spread
  // 2 == place_thresh + place_thresh_spread_coef * spread_ema
  // 3 == fixed-notional liquidation spread
  // 4 == fixed-notional sided liquidation distance
  // 5 == auto-notional sided liquidation distance
  int place_thresh_mode_ = 0;
  double place_thresh_spread_coef_ = 0.5;
  // RelWideMM2 does not have AlphaRelWideMM's lspread_ema_; this dedicated
  // spread EMA backs place_thresh_mode 2. Default 120s matches alpha configs.
  double place_thresh_spread_ema_tdc_ms_ = 120 * 1000;
  double local_spread_ema_ = 0;
  int64_t last_local_spread_update_t_ = 0;

  // place_thresh_mode >= 3 owns its own local liquidation-price source.
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

  double place_thresh_liq_auto_inside_mult_ = 1.0;
  double place_thresh_liq_auto_min_notional_ = 0;
  double place_thresh_liq_auto_max_notional_ = 1e12;
  double place_thresh_liq_auto_tdc_ms_ = 60 * 1000;
  double place_thresh_liq_auto_notional_ems_ = 0;
  int64_t place_thresh_liq_auto_last_t_ = 0;

  // If we're careful we can let thresholds be negative too. For now, disallowed.
  const bool limit_threshes_positive_ = true;

  // One thresh mult slot per MultSource; the effective mult is the widest of
  // them, so a defensive widen can't be clobbered by an unrelated writer
  // resetting to 1.
  double um_thresh_mult_ = 1;
  double bleed_thresh_mult_ = 1;
  double minfv_thresh_mult_ = 1;
  // this is what we'll set the minfv slot to when coming back from minfv
  double thresh_mult_minfv_ = 1.5;

  double effThreshMult() const {
    return std::max({um_thresh_mult_, bleed_thresh_mult_, minfv_thresh_mult_});
  }

  double extra_buy_thresh_ = 0;
  double extra_sell_thresh_ = 0;

  // buy/sell threshes based on semipermanent changes (eg. if we got a usermsg
  // telling us to widen buyside by 1.5x)
  double mid_buy_thresh_;
  double mid_sell_thresh_;

  // After we apply all changes, including offsets due to position, what are our
  // buy/sell threshes?
  double cur_buy_thresh_px_;
  double cur_sell_thresh_px_;

  // if true, will try to cancel if alone at top with no level behind me.
  bool will_cancel_isolated_ = false;

  // In units of price.
  // This cancelthresh is the effective thresh relative to the predpx
  // that we're willing to keep our orders at.
  double cur_buy_cancel_thresh_px_;
  double cur_sell_cancel_thresh_px_;

  // Thresh to use for TL2. Bypasses all adjustments above. Cancel thresh scales from this in TL2.
  double TL2_aggr_exit_thresh_;

  bool mm_gtc_;
  TimeInForce tif_;

  // True iff the remote signal is the price of the true asset, versus some
  // proxy that we are using to benchmark against.
  bool remote_feed_self_ = true;

  // rung_spacing_mult is a fxn of place_thresh. We'll set it when we get the
  // first midprice.
  double rung_spacing_mult_;
  // Translates the rung spacing to price space.
  double rung_spacing_px_;
  // If we want to specify rungage independently of the place thresh.
  double fixed_rung_spacing_thresh_ = 0;

  // If set, lets us cut in front of the front order by less than a whole
  // thresh.
  // This way, lets us follow the book more aggressively while it's moving.
  // Similar to something we've talked about before.
  double front_rung_spacing_mult_ = 1;

  // There might still be cases where the book is empty. If that's the case,
  // we make a setting that lets us skip bounding to best-book+1 tick, as that will
  // still create very wide books.
  bool restrict_near_book_ = true;

  // Queue position optimization: jump ahead of large passive size if within cancel range
  bool enable_queue_jump_ = false;
  double queue_jump_size_mult_ = 3.0; // Consider size "large" if >= this multiple of order_size

  // Max number of back levels we're willing to have out (not counting front level).
  int max_back_levels_;

  // If false, we don't place back levels, just let front levels become back levels
  bool place_back_levels_ = false;

  // A parameter for sparsifying as we place more backlevels.
  // If this is 1, then all backlevel rungs are equal. If it is something like
  // 1.1, then the i-th rung is 1.1^(num_orders) times the rung spacing.
  // Note: only used for placing new back levels, not for canceling/moving
  // existing ones when we insert a new back level somewhere in the middle
  double per_backlevel_rung_spacing_mult_;

  // If ladder_ticks is set on, then we use THAT instead of curvicity
  // to determine offset.
  bool use_ladder_ticks_;
  bool ladder_one_sided_;
  // Every time we get filled, we check how many ordersize we were filled, and then
  // widen by this factor.
  double per_order_widen_frac_;

  // Threshold narrowing going towards flat
  // When we have inventory and premium_ema indicates market is trading away from our fair
  // in the direction we need to flatten (e.g., premium_ema > 0 when short, and
  // we can't buy back easily), narrow the flatten side by this fraction of the widen amount.
  double order_decrease_coef_ = 0;

  // Apply additional curvicity based on recent impulses.
  // eg. if we get filled 2x ordersize long in quick succession, back off harder in the
  // near term. There may be a market event we don't know about yet.
  double curv_impulse_tdc_ms_ = 1000;
  double curv_impulse_coef_ = 0;
  double curv_impulse_ = 0;

  int64_t last_exec_decay_t_ = 0;

  // /////////////////////////////////////////////////////////
  // Pred price momentum-based threshold adjustment.
  // Track recent price moves via EMA of returns and widen thresholds on the side
  // momentum is pointing to avoid getting picked off by sustained directional moves.

  double pred_momentum_tdc_ms_ = 1000 * 30;
  double pred_momentum_coef_ = 0;
  double pred_momentum_ems_ = 0;
  double prev_discounted_final_px_ = 0;
  int64_t last_pred_momentum_update_t_ = 0;

  // Shared vol/liq mechanisms (1-4).
  VolMechanisms vol_mechs_;

  // /////////////////////////////////////////////////////////

  // *********************************************************
  // Params for doing relative pricing based on a remote signal.
  // TODO: at some point, make this take in multiple relative signals, and if
  // we start losing any one of them we panic.
  // Naming note. "Remote" is the remote "true price" that we are pegging to.
  // "relative" is the signal of something we are using as a proxy. FOr example,
  // btc. "local" is the price of the symbol we're currently trading (which may be empty tbh).

  double relative_beta_ = 0;
  double last_relative_px_ = 0;
  double beta_uncertainty_coef_ = 0; // specified as frac of relative_beta

  double last_perm_snapshot_rel_px_ = 0;
  double last_perm_snapshot_remote_px_ = 0;
  int64_t last_perm_snapshot_t_ = 0;
  double last_translated_px_ = 0; // last known valid translated price, before discounting

  // For sims, if we want to use supabase snapshots, we can write this to a file to prevent
  // excessive db accesses.
  std::string sim_snapshot_cache_file_ = "";

  // In sim mode, just read supabase once so we can initialize ourselves.
  bool sim_read_supabase_already_ = false;

  // Additional widening of thresholds because of uncertainty in relative pricing.
  // This should scale linearly with returns in the relative price since last snapshot.
  // Specifically: rel_px_thresh = |beta_uncertainty_coef * relative_beta * rel_return|
  double rel_px_thresh_ = 0;

  bool periodic_print_premium_ = false;

  // /////////////////////////////////////////////////////////
  // Params to account for premium.
  // If the book is just trading over our predpx by a large amount, we could
  // take on a large imbalanced position over the period.
  // This might put us on more risk than we'd like to take. So my solution is to
  // keep track of the current premium over the mid and to keep an EMA.
  // If the premium is large and sustained, then we apply this premium
  // to the predpx.

  // 20 minutes seems reasonable?
  double premium_tdc_ = 1000 * 60 * 20;
  int64_t last_premium_adjust_t_ = 0;
  double cur_premium_ = 0;
  double premium_ema_ = 0;
  double premium_ema_coef_ = 0;
  double premium_adjusted_pred_px_ = 0;

  // Specifies how we adjust our pred-px based on the remote(oracle) price versus the local
  // traded book.
  // mode == 0: the traditional approach. We take the premium_ema, start from the
  //   oracle price, and then adjust by premium_ema_coef_ * (min(cur_premium, premium_ema))
  //   So if there is a persistent 30 bps premium between local_mid and remote_mid,
  //   and the premium_ema_coef_ = 0.5, then we move the pred_px 15 bps towards the
  //   direction of the local_mid.
  // mode == 1: Start from the local_price and moves the pred_px towards the remote.
  //   The premium_adjust_max_thresh_mult_ is a limit on how far from the local_mid we
  //   adjust our pred_price. For example, if premium_adjust_max_thresh_mult_ = 4, then
  //   we start from the local_mid, and start moving towards the remote. But we limit
  //   ourselves to 4 * thresh_px. This is so that if the premium blows out, like, we've
  //   seen 1% on a lot of days, 4% last Friday 1/30, then we basically needed a thumb on the
  //   scale to have us trade biased toward the premium, but doesn't prevent us from
  //   trading if the premium hits like 1%
  // mode == 2: Start at pred_px. At 0 position, we adjust 0 and use pred_px. At maxpos,
  //   we adjust by the full cur_premium and use local_sig_val.
  int premium_adjust_mode_ = 0;

  // At most adjust by this many.
  double premium_adjust_max_thresh_mult_ = 4;
  double premium_mode1_adjust_coef_ = 1;

  // Premium ema coef to use for TL2. Still uses conf-set premium_tdc if any.
  double TL2_aggr_exit_premium_ema_coef_ = 0.9;

  // /////////////////////////////////////////////////////////

  // tbh, I'm not sure if we actually want this. So jI'm gonna let this go for a bit.

  bool needs_start_snapshotting_ = true;
  std::chrono::seconds remote_snapshot_interval_;

  // If it's been this many ms since we've heard from the remote sig, then
  // it's possible that feed went down. So then start switching over to the relative sig.
  int64_t relative_px_after_n_ms_;

  // If we are gonna price based off a relative thing, like btc.
  bool in_relative_mode_ = false;

  // MAYBE do some snapshotted pricing.

  // *********************************************************
  // For equities (eg. usequities), we may want to use a
  // time-decayed VwAP (anchoring at the closing print) to set
  // the current predprice.
  // In that case, we do a lookup of the closing print, and
  // apply a time-decayed vwap (initialized at the supposed closing print).
  // I think this should be set at the config level.
  // We periodically try to read supabase to find the relevant closing print.
  // If we don't find one, then just use the mid- or current vwap as needed.

  // Set by conffile
  bool equity_off_hour_vwap_ = false;

  // Keep looking it up until we find it.
  bool looked_up_closing_print_ = false;
  double closing_print_px_ = 0;
  double closing_print_qty_ = 0;

  double running_vwap_num_ = 0;
  double running_vwap_denom_ = 0;

  // The running value for the vwap.
  double running_vwap_ = 0;

  // Last time we updated our vwap.
  int64_t last_vwap_t_ = 0;

  // maybe this works?
  // It's in ms.
  double vwap_tdc_ms_ = 2 * 60 * 60 * 1000;
  int times_tried_closing_print_lookup_ = 0;
  std::chrono::seconds closing_print_lookup_interval_ = std::chrono::seconds(60);

  // *********************************************************

  // Insert and delete from the front- and back-
  std::vector<SimpleOrder> buy_orders_;
  std::vector<SimpleOrder> sell_orders_;

  // Some natural rate limiters.
  // dont cancel an ord if it's been alive for less than this long.
  int64_t ms_min_ord_lifetime_;
  int64_t ms_between_place_ = 100;
  int64_t ms_between_cxl_ = 100;
  int64_t ms_between_backlevel_ = 2000; // 2s to reduce backlevel order spam

  int64_t last_t_place_;
  int64_t last_t_cxl_;
  int64_t last_t_backlevel_;

  pktrade::signals::Signal* local_sig_ = nullptr;
  pktrade::signals::Signal* remote_sig_ = nullptr;
  pktrade::signals::Signal* relative_sig_ = nullptr;

  int local_sig_idx_;
  int remote_sig_idx_ = -1;
  int relative_sig_idx_ = -1;

  double local_sig_val_ = 0;
  double remote_sig_val_ = 0;
  double relative_sig_val_ = 0;
  double pred_px_ = 0;

  // For pre-IPO like the upcoming Cerebras and SpaceX.
  // We use a constant pred_px set in conf, and updated by usermsgs.
  bool use_const_pred_px_ = false;
  double const_pred_px_ = 0;

  int num_fires_ = 0;
  int64_t first_fire_t_ = 0;
  // Cache the time of the current tryFire() call.
  int64_t now_fire_t_ = 0;

  // Figures out ticksize.
  double min_tick_;

  bool needs_first_fire_;

  bool print_ords_;
  int print_ords_every_;

  // Look up the expiry date and rfr of these symbols.
  // Coordinate w/ bmac.

  // This needs to be updated eventually, with the forward-looking rate and
  // with the
  double riskfree_rate_ = 0;
  // When we start, we find when it expires.
  int64_t expiry_t_ = 0;

  bool do_discounting_ = true;

  // After we place a cancel, if we don't hear back within this time, we let
  // ourselves try again.
  // I increased this to 12s because I am observing 10s rtt's for cancels (esp
  // if I overload the machine).
  int64_t CANCEL_RETRY_INTERVAL = 12 * 1000;

  // ***************************************************
  // For checking if network is down (done in TradeCarrier)
  // Use this to know when we're coming back from a network down situation
  bool recovering_from_network_down_ = false;

  // ***************************************************

  // t of last local mid t.
  int64_t last_mid_t_ = 0;

  // t of last remote mid update.
  int64_t last_remote_mid_t_ = 0;

  // t of last relative mid update.
  int64_t last_relative_mid_t_ = 0;

  // If we're dislocated from the oracle this amount of time,
  // then clamp
  int64_t dislocated_from_oracle_thresh_ms_ = 7 * 1000;

  // Track when we start dislocating from the oracle.
  int64_t dislocated_from_oracle_start_t_ = 0;
  bool snapped_to_oracle_ = false;

  // If we differ more than this from the oracle, we snap to the oracle.
  // Even if we might be right (and does logwarns), the oracle is what everyone follows.
  // So we still need to follow it.
  // I think 15 bps is a reasonable deviation, and shouldn't affect us too much. It might slow us
  // down in times of extreme move, but I think it's worth it for protection. We can revisit this
  // exact value later on. For now, I want to avoid a situation like the morning of 20251030.
  double oracle_diff_thresh_ = 0.0015;

  static bool compareSellOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px < rord.px); }

  static bool compareBuyOrds(SimpleOrder lord, SimpleOrder rord) { return (lord.px > rord.px); }
};

} // namespace pktrade::ordex
