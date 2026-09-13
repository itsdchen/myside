#include "wide_mm.h"

#include <fmt/format.h>
#include <glog/logging.h>
#include <magic_enum.hpp>

#include <cmath>
#include <limits>

#include "pktrade/context/global_vars.h"
#include "pktrade/ordex/protection_helpers.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::ordex {

WideMM::WideMM(SymbolId symbol, const rapidjson::Value& ordex_conf,
               pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf)
    : Ordex(symbol, ordex_conf), last_t_place_(0), last_t_cxl_(0) {
  // Read stuff from the config.
  for (const rapidjson::Value& book : ordex_conf["markets"].GetArray()) {
    traded_books_.push_back(
        magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown));
  }

  if (traded_books_.size() > 1) {
    throw std::runtime_error("WideMM only supports trading one market at a time. Update the "
                             "class implementation, then remove this.");
  }
  traded_bid_ = BookId{traded_books_[0], symbol_};

  // Kind of silly I guess. But it is what it is.
  max_pos_ = Quantity{ordex_conf["max_pos"].GetString()};
  order_size_ = Quantity{ordex_conf["order_size"].GetString()};

  if (ordex_conf.HasMember("tgt_order_notional") && ordex_conf.HasMember("tgt_maxpos_notional")) {
    set_size_from_notional_ = true;
    tgt_order_notional_ = ordex_conf["tgt_order_notional"].GetDouble();
    tgt_maxpos_notional_ = ordex_conf["tgt_maxpos_notional"].GetDouble();

    // Basic validation
    if (tgt_order_notional_ <= 0 || tgt_maxpos_notional_ <= 0) {
      throw std::runtime_error(
          fmt::format("Invalid tgt_order_notional ({}) or tgt_maxpos_notional ({}) given",
                      tgt_order_notional_, tgt_maxpos_notional_));
    }

    if (tgt_order_notional_ > 1e6 || tgt_maxpos_notional_ > 1e7) {
      throw std::runtime_error(
          fmt::format("Invalid (TOOBIG) tgt_order_notional ({}) or tgt_maxpos_notional ({}) given",
                      tgt_order_notional_, tgt_maxpos_notional_));
    }

    if (tgt_order_notional_ > tgt_maxpos_notional_) {
      throw std::runtime_error(
          fmt::format("Invalid tgt_order_notional ({}) should be < tgt_maxpos_notional ({})",
                      tgt_order_notional_, tgt_maxpos_notional_));
    }

    // For now, if these are given, then order_size and maxpos are ignored.
    LOG(INFO) << fmt::format("Using tgt_order_notional ({}) and tgt_maxpos_notional ({})",
                             tgt_order_notional_, tgt_maxpos_notional_);
  }

  double cross_size = order_size_.toDouble() * ordex_conf["cross_order_size_mult"].GetDouble();
  cross_order_size_ = Quantity{std::to_string(cross_size)};

  alpha_mult_ = ordex_conf["alpha_mult"].GetDouble();
  base_place_thresh_conf_ = ordex_conf["place_thresh"].GetDouble();

  mid_thresh_ = base_place_thresh_conf_;

  if (ordex_conf.HasMember("cancel_buffer")) {
    cancel_buffer_ = ordex_conf["cancel_buffer"].GetDouble();
    // Reconstruct the implicit fraction so modes 3/4/5 can scale the cancel
    // buffer with the dynamic per-side place_thresh.
    cancel_buffer_frac_ =
        base_place_thresh_conf_ > 0 ? cancel_buffer_ / base_place_thresh_conf_ : 0;
  } else {
    cancel_buffer_frac_ = ordex_conf["cancel_buffer_frac"].GetDouble();
    cancel_buffer_ = cancel_buffer_frac_ * base_place_thresh_conf_;
  }

  cross_thresh_ = ordex_conf["cross_thresh"].GetDouble();
  cur_cross_thresh_ = cross_thresh_;

  want_cross_ = false;
  if (ordex_conf.HasMember("want_cross")) {
    want_cross_ = ordex_conf["want_cross"].GetBool();
  }

  place_thresh_mode_ = 0;
  if (ordex_conf.HasMember("place_thresh_mode")) {
    place_thresh_mode_ = ordex_conf["place_thresh_mode"].GetInt();
  }
  if (place_thresh_mode_ < 0 || place_thresh_mode_ > 5) {
    throw std::runtime_error("Invalid place_thresh_mode");
  }
  if (ordex_conf.HasMember("place_thresh_spread_coef")) {
    place_thresh_spread_coef_ = ordex_conf["place_thresh_spread_coef"].GetDouble();
  }
  if (place_thresh_spread_coef_ < 0 || place_thresh_spread_coef_ > 10) {
    throw std::runtime_error("Invalid place_thresh_spread_coef");
  }
  if (place_thresh_mode_ >= 3) {
    parsePlaceThreshLiqConfig(ordex_conf);
  }

  // Thresh to use in TL2 aggressive exit mode.
  // TODO: Maybe we could use ladder_one_sided=false instead, so we're more aggressive when our
  // position is bigger? Would need to add some checks and limits to that though.
  TL2_aggr_exit_thresh_ = base_place_thresh_conf_ * 0.5;
  if (ordex_conf.HasMember("TL2_aggr_exit_thresh")) {
    TL2_aggr_exit_thresh_ = ordex_conf["TL2_aggr_exit_thresh"].GetDouble();
  }

  mm_gtc_ = false;
  tif_ = TimeInForce::ALO;

  if (ordex_conf.HasMember("mm_gtc")) {
    mm_gtc_ = ordex_conf["mm_gtc"].GetBool();
    if (mm_gtc_) {
      tif_ = TimeInForce::GTC;
    }
  }

  // Which way of curvicity.

  // ladder == linear in ordersize.
  use_ladder_ticks_ = false;
  if (ordex_conf.HasMember("use_ladder_ticks")) {
    use_ladder_ticks_ = ordex_conf["use_ladder_ticks"].GetBool();
  }
  ladder_one_sided_ = false;
  if (ordex_conf.HasMember("ladder_one_sided")) {
    ladder_one_sided_ = ordex_conf["ladder_one_sided"].GetBool();
  }
  per_order_widen_frac_ = 0.1;
  if (ordex_conf.HasMember("per_order_widen_frac")) {
    per_order_widen_frac_ = ordex_conf["per_order_widen_frac"].GetDouble();
  }

  // Curvicity-related params.
  pcurvicity_ = ordex_conf["pcurvicity"].GetDouble();
  pcurvicity_coef_ = ordex_conf["pcurvicity_coef"].GetDouble();

  if (ordex_conf.HasMember("pcurv_denom_ord_mult") &&
      ordex_conf["pcurv_denom_ord_mult"].GetDouble() != -1) {
    pcurv_frac_denom_ = order_size_.toDouble() * ordex_conf["pcurv_denom_ord_mult"].GetDouble();
  } else {
    pcurv_frac_denom_ = max_pos_.toDouble();
  }

  max_back_levels_ = ordex_conf["max_back_levels"].GetInt();
  if (ordex_conf.HasMember("place_back_levels")) {
    place_back_levels_ = ordex_conf["place_back_levels"].GetBool();
  }
  if (ordex_conf.HasMember("will_cancel_isolated")) {
    will_cancel_isolated_ = ordex_conf["will_cancel_isolated"].GetBool();
  }
  rung_spacing_mult_ = ordex_conf["rung_spacing_mult"].GetDouble();
  rung_spacing_px_ = 0;

  if (ordex_conf.HasMember("fixed_rung_spacing_thresh")) {
    fixed_rung_spacing_thresh_ = ordex_conf["fixed_rung_spacing_thresh"].GetDouble();
  }

  if (ordex_conf.HasMember("front_rung_spacing_mult")) {
    front_rung_spacing_mult_ = ordex_conf["front_rung_spacing_mult"].GetDouble();
    if (front_rung_spacing_mult_ > 2 || front_rung_spacing_mult_ <= 0) {
      throw std::runtime_error("Gave improper front_rung_spacing_mult");
    }
  }

  per_backlevel_rung_spacing_mult_ = 1;
  if (ordex_conf.HasMember("per_backlevel_rung_spacing_mult")) {
    per_backlevel_rung_spacing_mult_ = ordex_conf["per_backlevel_rung_spacing_mult"].GetDouble();
  }
  if (per_backlevel_rung_spacing_mult_ < 1) {
    throw std::runtime_error("per_backlevel_rung_spacing_mult must be >= 1");
  }

  if (ordex_conf.HasMember("min_foreign_inside_mult")) {
    min_foreign_inside_mult_ = ordex_conf["min_foreign_inside_mult"].GetDouble();
  }
  if (ordex_conf.HasMember("local_size_cap_mult")) {
    local_size_cap_mult_ = ordex_conf["local_size_cap_mult"].GetDouble();
  }
  if (ordex_conf.HasMember("local_size_cap_tdc_ms")) {
    local_size_cap_tdc_ms_ = ordex_conf["local_size_cap_tdc_ms"].GetDouble();
  }
  if (ordex_conf.HasMember("exec_anchored_mid_tdc_ms")) {
    exec_anchored_mid_tdc_ms_ = ordex_conf["exec_anchored_mid_tdc_ms"].GetDouble();
  }

  if (ordex_conf.HasMember("adding_no_cutin")) {
    adding_no_cutin_ = ordex_conf["adding_no_cutin"].GetBool();
  }

  if (pcurvicity_ <= 0) {
    throw std::runtime_error(fmt::format("Invalid pcurvicity {}, must be positive\n", pcurvicity_));
  }

  ms_min_ord_lifetime_ = 60;
  if (ordex_conf.HasMember("min_ord_lifetime")) {
    ms_min_ord_lifetime_ = int(ordex_conf["min_ord_lifetime"].GetDouble() * 1000);
  }

  if (ordex_conf.HasMember("curv_impulse_tdc")) {
    curv_impulse_tdc_ms_ = ordex_conf["curv_impulse_tdc"].GetDouble() * 1000;
    curv_impulse_coef_ = ordex_conf["curv_impulse_coef"].GetDouble();
  }

  ms_between_cross_ = ordex_conf["ms_between_cross"].GetInt64();
  ms_between_place_ = ordex_conf["ms_between_place"].GetInt64();
  ms_between_cxl_ = ordex_conf["ms_between_cxl"].GetInt64();
  if (ordex_conf.HasMember("ms_between_backlevel")) {
    ms_between_backlevel_ = ordex_conf["ms_between_backlevel"].GetInt64();
  }

  last_t_cross_ = 0;
  last_t_place_ = 0;
  last_t_cxl_ = 0;
  last_t_backlevel_ = 0;

  print_ords_ = ordex_conf["print_ords"].GetBool();

  ref_sig_ = sf->getByName(ordex_conf["ref_sig"].GetString());
  pred_sig_ = sf->getByName(ordex_conf["pred_sig"].GetString());

  needs_first_fire_ = true;
  num_fires_ = 0;
  approx_mid_ = 0;

  if (ref_sig_ == nullptr) {
    throw std::runtime_error("Ref_sig not successfully created");
  }

  if (pred_sig_ == nullptr) {
    throw std::runtime_error("Pred_sig not successfully created");
  }

  if (place_thresh_mode_ >= 3) {
    initPlaceThreshLiqSignal(ordex_conf, sf);
  }

  // For doing mid_move_ems things.
  net_mid_move_ems_ = 0;
  abs_mid_move_ems_ = 0;
  last_mid_px_ = 0;
  last_mid_t_ = 0;

  // Moving this out of the if's, because it's now used for a lot of things.
  if (ordex_conf.HasMember("mid_ems_tdc_s")) {
    mid_move_ems_time_const_ms_ = ordex_conf["mid_ems_tdc_s"].GetDouble() * 1000;
  } else if (ordex_conf.HasMember("mid_move_ems_time_const_s")) {
    mid_move_ems_time_const_ms_ = ordex_conf["mid_move_ems_time_const_s"].GetDouble() * 1000;
  }
  // For modifying thresh based on midmove ems (vs. spread)
  if (ordex_conf.HasMember("scale_thresh_midmove_spread")) {
    scale_thresh_midmove_spread_ = ordex_conf["scale_thresh_midmove_spread"].GetBool();
    scale_thresh_mids_const_ = ordex_conf["scale_thresh_mids_const"].GetDouble();

    scale_midmove_lower_bound_ = ordex_conf["scale_midmove_lower_bound"].GetDouble();
    if (ordex_conf.HasMember("scale_midmove_mult")) {
      scale_midmove_mult_ = ordex_conf["scale_midmove_mult"].GetDouble();

    } else {
      scale_midmove_mult_ = 1;
    }
  } else {
    scale_thresh_midmove_spread_ = false;
    scale_thresh_mids_const_ = 20;
    scale_midmove_lower_bound_ = 1;
    scale_midmove_mult_ = 1;
  }

  if (ordex_conf.HasMember("spreadscale_v2")) {
    spreadscale_v2_ = ordex_conf["spreadscale_v2"].GetBool();

    // If so, gives a biasing (asymmetric) factor
    spreadscale_v2_bias_ = ordex_conf["spreadscale_v2_bias"].GetBool();

    spreadscale_v2_bias_coef_ = ordex_conf["spreadscale_v2_bias_coef"].GetDouble();
    // Uses the momentum term...
    spreadscale_v2_momentum_ = ordex_conf["spreadscale_v2_momentum"].GetBool();

    // Looking for functions that == 0 at 0 and increase to the right.
    // 0: linear (y=x)
    // 1: sqrt(x)
    // 2: log(1+x)
    // 3: 1 - exp(-x)
    spreadscale_v2_shape_ = ordex_conf["spreadscale_v2_shape"].GetInt();

    // If we let the midmoves change our working thresh, we need to impose a
    // lower bound on how small that will go. If 0.3, then the smallest modifier
    // will be 0.3x the original threhs.
    spreadscale_v2_mult_ = ordex_conf["spreadscale_v2_mult"].GetDouble();

    spreadscale_v2_denom_const_ = ordex_conf["spreadscale_v2_denom_const"].GetDouble();
  } else {
    spreadscale_v2_ = false;
    spreadscale_v2_bias_ = false;
    spreadscale_v2_momentum_ = false;
    spreadscale_v2_bias_coef_ = 1;
    spreadscale_v2_shape_ = 0;
    spreadscale_v2_mult_ = 1;
    spreadscale_v2_denom_const_ = 1;
  }

  // Calculating spread ema.
  local_spread_num_ = 0;
  local_spread_denom_ = 0;
  spread_ema_ = 0;

  // Add self as a listener to refsig.
  ref_sig_->addListener(this);

  if (ordex_conf.HasMember("narrow_when_few_trades")) {
    narrow_when_few_trades_ = ordex_conf["narrow_when_few_trades"].GetBool();
    narrow_fewtrade_minmultiple_ = ordex_conf["narrow_fewtrade_minmultiple"].GetDouble();
    narrow_thresh_time_const_ms_ = ordex_conf["narrow_thresh_time_const_s"].GetDouble() * 1000;
    narrow_fewtrade_denom_ = ordex_conf["narrow_fewtrade_denom"].GetDouble();

    narrow_fewtrade_ems_buy_ = 0;
    narrow_fewtrade_ems_sell_ = 0;
  } else {
    // For narrowing our spread based on exec time.
    narrow_when_few_trades_ = false;
    narrow_fewtrade_ems_buy_ = 0;
    narrow_fewtrade_ems_sell_ = 0;
    narrow_fewtrade_minmultiple_ = 1;
    narrow_fewtrade_denom_ = 1;
  }

  // Time decay const, in ms units.

  last_exec_decay_t_ = 0;

  // Vol/liq mechanisms (1-4)
  vol_mechs_.parseConfig(symbol_.get(), ordex_conf);

  // Make the tradecaller.
  // Maybe at some point pull this out, elsewhere.
  tc_tempo_ = tf->getByName(ordex_conf["trade_caller"].GetString());
  tc_tempo_->addListener(this);

  refreshSizeMultBaseFromCurrent();
}

void WideMM::postSecMaster() {
  min_tick_ = pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble();

  Quantity new_order_size_ =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, order_size_.toDouble(), true);
  order_size_ = new_order_size_;

  Quantity new_max_pos_ =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, max_pos_.toDouble(), true);
  max_pos_ = new_max_pos_;

  printf("PostSecmaster (%s): min_tick %f new_ord_size %f max_pos %f\n", symbol_.get().c_str(),
         min_tick_, order_size_.toDouble(), max_pos_.toDouble());
}

void WideMM::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Trd is always needed (vol_mechs_.onBookUpdate; exec-anchored mid when on).
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::Trd,
  };

  // Lvl callbacks needed for inside-size protections and for the internal
  // liquidation signal used by place_thresh_mode 3/4/5.
  if (local_size_cap_mult_ > 0 || min_foreign_inside_mult_ > 0 ||
      place_thresh_liq_sig_ != nullptr) {
    cb_types.push_back(pktrade::md::BeaconCBType::LvlAdd);
    cb_types.push_back(pktrade::md::BeaconCBType::LvlMod);
    cb_types.push_back(pktrade::md::BeaconCBType::LvlDel);
  }

  beacon->addListener(traded_bid_, this, cb_types);

  if (place_thresh_liq_sig_ != nullptr) {
    place_thresh_liq_sig_->subscribeData(beacon);
  }
}

signals::Signal* WideMM::getPredSignal() { return pred_sig_; }

signals::Signal* WideMM::getMidSignal() { return ref_sig_; }

Quantity WideMM::getEffectiveOrderSize(Side side) const {
  if (local_size_cap_mult_ <= 0) return order_size_;
  double ems = (side == Side::Buy) ? inside_size_buy_ems_ : inside_size_sell_ems_;
  if (ems <= 0) return order_size_;
  double capped = protection::capBySize(order_size_.toDouble(), local_size_cap_mult_, ems);
  if (capped <= 0) return Quantity{0};
  return pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, capped, false);
}

// tl5 mode.
// TODO: actually trade up to .20 bps away from the inside.
void WideMM::aggFlat() {
  Quantity pos = riskman_->getPos();
  if (pos == Quantity{0}) {
    return;
  }

  Market mkt = traded_books_[0];
  const LevelBook* trade_bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  if (pos > Quantity{0}) {
    // Sell maybe.

    Quantity post_sell_amt = riskman_->getMaxFillPosition(Side::Sell);
    if (post_sell_amt <= Quantity{0}) {
      return;
    }

    auto& bk_side = trade_bk->buySide();

    // List the price.
    Price sell_px = bk_side.begin()->px;
    double top_sell_px = bk_side.begin()->px.toDouble();

    NewOrder ord{symbol_,          traded_books_[0], Side::Sell, post_sell_amt, sell_px,
                 OrderType::Limit, TimeInForce::IOC, false,      true};
    riskman_->sendOrd(ord);

    // Don't bother with flagging shares, if we're TL5.
  } else if (pos < Quantity{0}) {
    // Buy to flat.
    // Opposite direction as other.
    Quantity post_buy_amt = riskman_->getMaxFillPosition(Side::Buy);
    if (post_buy_amt >= Quantity{0}) {
      return;
    }
    Quantity buy_qty = -post_buy_amt;
    // Otherwise, send the buy.

    auto& bk_side = trade_bk->sellSide();
    Price buy_px = bk_side.begin()->px;
    double top_buy_px = bk_side.begin()->px.toDouble();

    NewOrder ord{symbol_,          traded_books_[0], Side::Buy, buy_qty, buy_px,
                 OrderType::Limit, TimeInForce::IOC, false,     true};
    riskman_->sendOrd(ord);
  }
}

void WideMM::onTempo(int tempo_id) { tryFire(); }

void WideMM::tryFire() {
  num_fires_++;

  // Must be allowed to trade.
  if (!riskman_->canTrade()) {
    return;
  }

  now_fire_t_ = time_utils::nowToMs();

  // TODO: update this to behave like relwidemm's TL's.

  const LevelBook* trade_bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  mid_px_ = ref_sig_->getValue();

  // Do capping of extreme alpha values.
  double alpha = pred_sig_->getValue();
  alpha = alpha * alpha_mult_;
  pred_px_ = mid_px_ * (1 + alpha);

  // Exec-anchored mid blend: pick the less-favorable-to-us reference per side,
  // so phantom book moves have to be confirmed by trade prints before they
  // pull pred_px. When disabled, both sides equal pred_px_.
  if (exec_anchored_mid_tdc_ms_ > 0 && exec_vwap_denom_ems_ > pktrade::EPS) {
    double exec_vwap = exec_vwap_num_ems_ / exec_vwap_denom_ems_;
    double blend_mid_buy  = std::min(exec_vwap, mid_px_);
    double blend_mid_sell = std::max(exec_vwap, mid_px_);
    pred_px_buy_  = blend_mid_buy  * (1 + alpha);
    pred_px_sell_ = blend_mid_sell * (1 + alpha);
  } else {
    pred_px_buy_  = pred_px_;
    pred_px_sell_ = pred_px_;
  }

  updateThreshes();

  if (needs_first_fire_ && mid_px_ != 0) {
    onFirstFire();
    return;
  }

  // Don't trade... if there's no mid.
  if (mid_px_ == 0) {
    return;
  }

  // safety check. 
  if (!std::isfinite(pred_px_)) {
    return;
  }

  double cur_pos = riskman_->getPos().toDouble();

  // Absolute allotment. Based on the om.maxposition.
  double abs_allowed_buy = riskman_->getSideAlloc(Side::Buy).toDouble();
  double abs_allowed_sell = riskman_->getSideAlloc(Side::Sell).toDouble();

  // I should also have relative allotment too. Based on the ordex maxpos.
  double ordex_allowed_buy =
      std::min(max_pos_.toDouble(),
               std::max((max_pos_ - riskman_->getMaxFillPosition(Side::Buy)).toDouble(), 0.0));
  double ordex_allowed_sell =
      std::min(max_pos_.toDouble(),
               std::max((riskman_->getMaxFillPosition(Side::Sell) + max_pos_).toDouble(), 0.0));

  ordex_allowed_buy = std::min(ordex_allowed_buy, abs_allowed_buy);
  ordex_allowed_sell = std::min(ordex_allowed_sell, abs_allowed_sell);

  if (pktrade::GlobalVar::live_) {
    LOG_EVERY_N(INFO, 1009) << " WideMM::tryFire. " << symbol_.get() << " alpha " << alpha
                            << " mid " << mid_px_  << " pred_px " << pred_px_ << " pos " << cur_pos << " allotted "
                            << ordex_allowed_buy << " | " << ordex_allowed_sell << " sizes "
                            << buy_orders_.size() << " | " << sell_orders_.size();
  }
  vol_mechs_.logState();
  int64_t now_t = time_utils::nowToMs();

  if (print_ords_ && num_fires_ % 100 == 0) {
    printOutstandingOrders();
  }

  // If in no-new-orders mode, maybeCancel is all we can do.

  if (!TL7_no_new_ords_) {
    // Consider firing.
    if (canCross()) {
      maybeCross(trade_bk, cur_pos, ordex_allowed_buy, ordex_allowed_sell, alpha, cross_thresh_);
    }

    if (TL6_cross_exit_) {
      // If in cross exit mode, don't place, and we've already canceled all outstanding orders.
      return;
    }

    // Only place if we just got a bookupdate. Let's run this expmt.

    if (now_t - last_t_place_ > ms_between_place_ && riskman_->canPlaceMoreOrders()) {
      maybeJoin(trade_bk, cur_pos, ordex_allowed_buy, ordex_allowed_sell);
    }
  }

  // Cancel should be the highest priority, but moving it up introduced diffs so reverting for now.
  if (now_t - last_t_cxl_ > ms_between_cxl_) {
    maybeCancel(trade_bk, cur_pos, ordex_allowed_buy, ordex_allowed_sell);
  }

  if (TL7_no_new_ords_) {
    return;
  }

  // Maybe add or rm backlevels too.
  // Always reserve space for the non-backlevels.
  if (now_fire_t_ - last_t_backlevel_ > ms_between_backlevel_) {
    manageBacklevels(trade_bk, ordex_allowed_buy - order_size_.toDouble(),
                     ordex_allowed_sell - order_size_.toDouble());
  }

  if (print_ords_ && num_fires_ % 100 == 0) {
    // Sort our orders every so often.
    // sort(buy_orders_.begin(), buy_orders_.end(), WideMM::compareBuyOrds);
    // sort(sell_orders_.begin(), sell_orders_.end(), WideMM::compareSellOrds);
    sort(buy_orders_.begin(), buy_orders_.end(), WideMM::compareBuyOrds);
    sort(sell_orders_.begin(), sell_orders_.end(), WideMM::compareSellOrds);
  }

  // Try to flush the rejects.
  if (num_fires_ % 50 == 0) {
    flushUnhandledRejectedOrds();
  }
}

void WideMM::updateThreshes() {
  // otay
  if (TL6_cross_exit_) {
    // In TL6, we use 0 cross thresh. No need to set place threshes since we only cross.
    cur_cross_thresh_ = 0;
    return;
  }
  // Otherwise, reset cur_cross_thresh. All other TLs use standard cross_thresh.
  cur_cross_thresh_ = cross_thresh_;

  place_thresh_px_ = mid_thresh_ * mid_px_;
  cancel_buffer_px_ = cancel_buffer_ * mid_px_;

  if (TL2_aggr_exit_) {
    // In TL2, we use TL2_aggr_exit_thresh and skip all thresh adjustments.
    place_thresh_buy_ = place_thresh_sell_ = TL2_aggr_exit_thresh_ * pred_px_;
    return;
  }

  // Modify threshes (some sort of dynamic adjustment)
  if (scale_thresh_midmove_spread_) {
    double modifier = 1;
    double spread_pct = spread_ema_ / mid_px_;

    // Divide move_ems (units of % return) but spread (in units of % of mid).
    double nu_mid_ratio = abs_mid_move_ems_ / spread_pct;

    // If scale_midmove_lower_bound_ == 0.3, then we do
    // 0.3 + 0.7 * decayed_exp().
    // And in particular, if the midmove is higher, then we want
    // the effective multiplier to be higher. If the midmove is lower,
    // then we want the threshold to be lower.
    // So basically just want to account for vol spikes.
    modifier = scale_midmove_lower_bound_ +
               /*(1 - scale_midmove_lower_bound_) * */
               // If the inside ratio is higher, then the exp should be
               // closer to 0. So then 1 - that is larger.
               // If inside ratio is lower, then the exp should be
               // closer to 1, so then the whole term is higher.
               (1 - scale_midmove_lower_bound_) * scale_midmove_mult_ *
                   (1 - std::exp(-nu_mid_ratio / scale_thresh_mids_const_));

    // Rate limit this a little.

    /*
    if (false && num_fires_ % 100 == 0) {
      printf("%s: Hello: alpha %f netmid_ems %f absmid_ems %f spread_ema %.8f ( "
             "%f %f ) nu_ratio %f "
             "inside_ratio %f "
             " modifier % f so it is % f | final % f \n ",

             time_utils::nowToStr().c_str(), alpha, net_mid_move_ems_, abs_mid_move_ems_,
             spread_ema_, local_spread_num_, local_spread_denom_, nu_mid_ratio,
             nu_mid_ratio / scale_thresh_mids_const_, modifier, place_thresh_px * modifier,
             std::max(place_thresh_px * modifier, spread_ema_));
     */

    place_thresh_px_ *= modifier;
    cancel_buffer_px_ *= modifier;

    // Bound it below by the spread. Can't get too small now.
    place_thresh_px_ = std::max(place_thresh_px_, spread_ema_);
    cancel_buffer_px_ = std::max(cancel_buffer_px_, spread_ema_);
  } else if (spreadscale_v2_) {

    if (spreadscale_v2_bias_ && abs_mid_move_ems_ > 1e-12) {
      // If bias is positive, then net_mid_move is positive, then
      // second term is negative, so it's 0.
      // So we are letting ourselves buy.
      // If bias is negative, the net mid_move is negative, so the second term
      // is positive, and then we are making the buy bias bigger, which means
      // we buy lower, which is what we want (since it will try to trend towards
      // our lower order.)
      cur_buy_bias_mult_ =
          1 + spreadscale_v2_bias_coef_ * std::max(0.0, -(net_mid_move_ems_ / abs_mid_move_ems_));

      cur_sell_bias_mult_ =
          1 + spreadscale_v2_bias_coef_ * (std::max(0.0, net_mid_move_ems_ / abs_mid_move_ems_));

      cur_buy_bias_mult_ = std::max(std::min(cur_buy_bias_mult_, 4.0), 0.25);
      cur_sell_bias_mult_ = std::max(std::min(cur_sell_bias_mult_, 4.0), 0.25);

    } else {
      cur_buy_bias_mult_ = 1;
      cur_sell_bias_mult_ = 1;
    }

    double modifier = 1;
    double spread_pct = spread_ema_ / mid_px_;

    double activity_ratio = abs_mid_move_ems_ / spread_pct / spreadscale_v2_denom_const_;

    // Make sure this is strictly nonnegative.
    double mod_inside = std::max(activity_ratio - 1.0, 0.0);

    if (spreadscale_v2_momentum_) {
      // OK, then, incorporate momentum.
      double momentum = abs(net_mid_move_ems_) / (abs_mid_move_ems_ + 1e-8);

      mod_inside = (activity_ratio * (1 + momentum));
    }

    if (spreadscale_v2_shape_ == 0) {
      modifier = 1 + spreadscale_v2_mult_ * mod_inside;
    } else if (spreadscale_v2_shape_ == 1) {
      modifier = 1 + spreadscale_v2_mult_ * std::sqrt(mod_inside);
    } else if (spreadscale_v2_shape_ == 2) {
      // log.
      modifier = 1 + spreadscale_v2_mult_ * std::log(1 + mod_inside);
    } else if (spreadscale_v2_shape_ == 3) {
      modifier = 1 + spreadscale_v2_mult_ * std::exp(-mod_inside);
    }

    if (false && num_fires_ % 100 == 0) {
      double momentum = abs(net_mid_move_ems_) / (abs_mid_move_ems_ + 1e-8);

      printf("%s: spreadv2 net_ems %f abs_ems %f spread_ema %f activity %f "
             "mod_inside %f momentum %f mod %f\n",
             time_utils::nowToStr().c_str(), net_mid_move_ems_, abs_mid_move_ems_, spread_ema_,
             activity_ratio, mod_inside, momentum, modifier

      );
    }

    place_thresh_px_ *= modifier;
    cancel_buffer_px_ *= modifier;

    // Bound it below by the spread. Can't get too small now.
    place_thresh_px_ = std::max(place_thresh_px_, spread_ema_);
    cancel_buffer_px_ = std::max(cancel_buffer_px_, spread_ema_);
  }

  // Adjust for modes and bias multiples.
  // If we do start incorporating liquidity, then maybe move this
  // down below to modify sided threshes directly.
  if (place_thresh_mode_ == 1) {
    place_thresh_px_ += local_spread_ * place_thresh_spread_coef_;
  } else if (place_thresh_mode_ == 2) {
    place_thresh_px_ += spread_ema_ * place_thresh_spread_coef_;
  }

  // Start moving to sided things.
  place_thresh_buy_ = place_thresh_px_;
  place_thresh_sell_ = place_thresh_px_;

  // Modes 3/4/5: derive a liquidation-based place threshold per side from
  // SigLiqBalance. Mode 3 is symmetric (half spread of the liq band), modes
  // 4 and 5 are sided (distance from local mid to each side's liq price).
  // Mode 5 auto-sizes the notional from the local inside book.
  if (place_thresh_mode_ >= 3 && place_thresh_liq_sig_ != nullptr) {
    bool have_liq_notional = place_thresh_mode_ != 5;
    if (place_thresh_mode_ == 5) {
      double auto_notional = calcPlaceThreshAutoLiqNotional();
      if (auto_notional > 0) {
        place_thresh_liq_sig_->setNotionalToLiquidate(auto_notional);
        place_thresh_liq_sig_->setMinTick();
        place_thresh_liq_sig_->fullRecalc();
        have_liq_notional = true;
      }
    }

    double liq_bid = place_thresh_liq_sig_->getBBMeasure();
    double liq_ask = place_thresh_liq_sig_->getBAMeasure();
    // mid_px_ > 0 guards against the pre-first-fire state where ref_sig_
    // hasn't published yet. updateThreshes() runs before tryFire's mid_px_==0
    // early return, so without this check modes 4/5 would compute
    // sell_liq_thresh_px = liq_ask * coef (a wildly large value).
    if (have_liq_notional && place_thresh_liq_sig_->getValid() && liq_bid > 0 &&
        liq_ask > liq_bid && mid_px_ > 0) {
      double buy_liq_thresh_px = 0;
      double sell_liq_thresh_px = 0;
      if (place_thresh_mode_ == 3) {
        // Symmetric: half-spread distance from the liq mid.
        buy_liq_thresh_px = sell_liq_thresh_px =
            (liq_ask - liq_bid) * 0.5 * place_thresh_liq_coef_;
      } else {
        // Sided (modes 4 and 5): distance from our mid to each liq edge.
        buy_liq_thresh_px = std::max(0.0, mid_px_ - liq_bid) * place_thresh_liq_coef_;
        sell_liq_thresh_px = std::max(0.0, liq_ask - mid_px_) * place_thresh_liq_coef_;
      }

      if (buy_liq_thresh_px > 0 || sell_liq_thresh_px > 0) {
        if (place_thresh_liq_combine_mode_ == 0) {
          place_thresh_buy_ += buy_liq_thresh_px;
          place_thresh_sell_ += sell_liq_thresh_px;
        } else {
          place_thresh_buy_ = std::max(place_thresh_buy_, buy_liq_thresh_px);
          place_thresh_sell_ = std::max(place_thresh_sell_, sell_liq_thresh_px);
        }
      }
    }
  }

  // Curvicity adjustments.
  double cur_pos = riskman_->getPos().toDouble();

  double bias_buy = 0;
  double bias_sell = 0;
  if (use_ladder_ticks_) {

    bias_buy =
        std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * place_thresh_buy_;
    bias_sell =
        std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * place_thresh_sell_;

  } else {
    // traditional Curvicity

    double curv_frac = std::min(1.0, std::abs(cur_pos / pcurv_frac_denom_));

    // double curv_offset = pcurvicity_coef_ * std::pow(curv_frac, pcurvicity_) * place_thresh_px_;

    // Make this sided.
    bias_buy = pcurvicity_coef_ * std::pow(curv_frac, pcurvicity_) * place_thresh_buy_;
    bias_sell = pcurvicity_coef_ * std::pow(curv_frac, pcurvicity_) * place_thresh_sell_;
  }

  // Maybe we should discuss, but I kind of like the premium_ema and order_decrease_coef
  // way of doing things better.
  if (cur_pos > 0) {
    // Bias towards selling.
    place_thresh_buy_ += bias_buy;
    if (!ladder_one_sided_) {
      place_thresh_sell_ -= bias_sell;
    }
  } else {
    place_thresh_sell_ += bias_sell;
    if (!ladder_one_sided_) {
      place_thresh_buy_ -= bias_buy;
    }
  }

  // Do side-based narrowing if we haven't traded much on one side.
  double narrow_amount = 1;
  if (narrow_when_few_trades_) {
    double buy_narrow_amount =
        narrow_fewtrade_minmultiple_ +
        (1 - narrow_fewtrade_minmultiple_) *
            (1 - std::exp(-narrow_fewtrade_ems_buy_ / narrow_fewtrade_denom_));

    double sell_narrow_amount =
        narrow_fewtrade_minmultiple_ +
        (1 - narrow_fewtrade_minmultiple_) *
            (1 - std::exp(-narrow_fewtrade_ems_sell_ / narrow_fewtrade_denom_));

    place_thresh_buy_ *= buy_narrow_amount;
    place_thresh_sell_ *= sell_narrow_amount;

    /*
        printf("narrow_amount %f %f  ( %f %f ) insides (%f %f )\n",
         buy_narrow_amount, sell_narrow_amount,
          narrow_fewtrade_ems_buy_, narrow_fewtrade_ems_sell_,
        narrow_fewtrade_ems_buy_ / narrow_fewtrade_denom_,
        narrow_fewtrade_ems_sell_ / narrow_fewtrade_denom_
        );
    */
    // And then do the modifying.

    // Finally, handle cancel threses too.
  }

  // Use these biases (if unset, set to 1)
  place_thresh_buy_ *= cur_buy_bias_mult_;
  place_thresh_sell_ *= cur_sell_bias_mult_;

  // Curv impulse: one-sided widening based on recent fills.
  if (curv_impulse_coef_ > 0) {
    double impulse_curv_offset = curv_impulse_ * curv_impulse_coef_;
    if (curv_impulse_ > 0) {
      // Bought recently, make it harder to buy.
      place_thresh_buy_ += impulse_curv_offset * place_thresh_buy_;
    } else if (curv_impulse_ < 0) {
      // Sold recently, make it harder to sell.
      place_thresh_sell_ += std::abs(impulse_curv_offset) * place_thresh_sell_;
    }
  }

  // Vol/liq mechanisms (1-4)
  {
    auto [bw, sw] = vol_mechs_.getVolWidenPx(pred_px_, place_thresh_buy_, place_thresh_sell_, spread_ema_);
    place_thresh_buy_ += bw;
    place_thresh_sell_ += sw;
    auto [ldb, lds] = vol_mechs_.getLiqDepthWidenPx(pred_px_);
    place_thresh_buy_ += ldb;
    place_thresh_sell_ += lds;
  }

  // Set sided cancel buffers. Under modes 0-2 we preserve the original static
  // semantics (single cancel_buffer_px_ for both sides). Under modes 3/4/5 the
  // place_thresh is dynamic and sided, so the cancel buffer must track it —
  // otherwise the static cancel zone becomes tiny relative to the now-wider
  // placement zone and we churn orders at every book tick.
  if (place_thresh_mode_ >= 3) {
    cancel_buffer_buy_px_ = cancel_buffer_frac_ * place_thresh_buy_;
    cancel_buffer_sell_px_ = cancel_buffer_frac_ * place_thresh_sell_;
  } else {
    cancel_buffer_buy_px_ = cancel_buffer_px_;
    cancel_buffer_sell_px_ = cancel_buffer_px_;
  }
}

bool WideMM ::canCross() {
  // Rate limit crossing in all cases.
  if (now_fire_t_ - last_t_cross_ < ms_between_cross_) {
    return false;
  }

  // TL6 cross exit mode overrides all other restrictions.
  if (TL6_cross_exit_) {
    return true;
  }

  // If not in TL6, check other restrictions normally.

  // Don't cross if config doesn't allow it.
  if (!want_cross_) {
    return false;
  }

  // Otherwise, we can cross!
  return true;
}

void WideMM::maybeCross(const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                        double ordex_allowed_sell,
                        // As opposed to a predicted px.
                        double alpha_pct_scale, double cross_thresh

) {
  // Then we can simplistically send a buy- or sell-

  if (alpha_pct_scale > cross_thresh) {
    // Then we can buy with abandon.
    double this_cross_sz = std::min(cross_order_size_.toDouble(), ordex_allowed_buy);
    if (this_cross_sz <= 0) return;

    // Gonna follow the naive approach, and just try to hit it versus do the
    // scaled approach.
    double this_cross_px = mid_px_ * (1 + alpha_pct_scale);

    // Make sure order size meets min notional.
    if (this_cross_sz * this_cross_px < 11) { // real min notional is 10, but adding buffer
      LOG(WARNING) << fmt::format(
          "({}) Buy order size below min notional of 10 "
          "(px {} sz {} cross_order_size {} allowed_buy {}). Sending min size instead.",
          symbol_.get(), this_cross_px, this_cross_sz, cross_order_size_.toDouble(),
          ordex_allowed_buy);
      this_cross_sz = 11.0 / this_cross_px;
    }

    // Sorry for silly naming.
    Quantity this_ord_qty =
        pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, this_cross_sz, true);
    Price this_ord_px = Price(std::to_string(roundToSide(this_cross_px, false)));

    if (this_ord_qty > Quantity{0}) {
      NewOrder ord{symbol_,          traded_books_[0], Side::Buy, this_ord_qty, this_ord_px,
        OrderType::Limit, TimeInForce::IOC, false,     false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        last_t_cross_ = time_utils::nowToMs();
      }
    }

  } else if (alpha_pct_scale < -cross_thresh) {
    // Then we can sell.
    double this_cross_sz = std::min(cross_order_size_.toDouble(), ordex_allowed_sell);
    if (this_cross_sz <= 0) return;

    // Gonna follow the naive approach, and just try to hit it versus do the
    // scaled approach.
    double this_cross_px = mid_px_ * (1 + alpha_pct_scale);

    // Make sure order size meets min notional.
    if (this_cross_sz * this_cross_px < 11) { // real min notional is 10, but adding buffer
      LOG(WARNING) << fmt::format(
          "({}) Sell order size below min notional of 10 "
          "(px {} sz {} cross_order_size {} allowed_sell {}). Sending min size instead.",
          symbol_.get(), this_cross_px, this_cross_sz, cross_order_size_.toDouble(),
          ordex_allowed_sell);
      this_cross_sz = 11.0 / this_cross_px;
    }

    // Sorry for silly naming.
    Quantity this_ord_qty =
        pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, this_cross_sz, true);
    Price this_ord_px = Price(std::to_string(roundToSide(this_cross_px, true)));

    if (this_ord_qty > Quantity{0}) {
      NewOrder ord{symbol_,          traded_books_[0], Side::Sell, this_ord_qty, this_ord_px,
        OrderType::Limit, TimeInForce::IOC, false,      false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        last_t_cross_ = time_utils::nowToMs();
      }
    }
  }
}

void WideMM::maybeJoin(const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                       double ordex_allowed_sell) {

  Quantity eff_buy_sz = getEffectiveOrderSize(Side::Buy);
  Quantity eff_sell_sz = getEffectiveOrderSize(Side::Sell);
  double eff_buy_sz_dbl = eff_buy_sz.toDouble();
  double eff_sell_sz_dbl = eff_sell_sz.toDouble();
  if (eff_buy_sz_dbl <= 0 && eff_sell_sz_dbl <= 0) return;

  // Buy?
  // We allow ourselves to place one more than the total order size, to let us
  // kick an old order out.
  if (eff_buy_sz_dbl > 0 && ordex_allowed_buy > eff_buy_sz_dbl
      // Restrict the outstanding backlevels.
      && buy_orders_.size() <= max_back_levels_) {
    // Consider the best buy price we'd do.
    double best_buy_px = pred_px_buy_ - place_thresh_buy_;

    // If it's more aggressive than BB, round to it.
    double top_bid = lvl_book->buySide().begin()->px.toDouble();

    if (best_buy_px > top_bid && adding_no_cutin_) {
      // Undo this purposefully.
      best_buy_px = top_bid;
    }

    // Tick rounding needs to happen after all changes to best_buy_px have been made.
    best_buy_px = roundToSide(best_buy_px, false);

    // printf("MaybeJoin pred_price %f curv_offset %f buy_thresh %f best_buy_px
    // %f\n",
    //   pred_px, curv_offset, buy_thresh, best_buy_px
    //);

    bool dont_place = false;
    for (auto& buy_order : buy_orders_) {
      if (buy_order.cancel_time != 0) {
        continue;
      }

      if (std::abs(buy_order.px.toDouble() - best_buy_px) <
          front_rung_spacing_mult_ * rung_spacing_px_) {
        // Too crowded.
        // Take this out for now.
        dont_place = true;
      }

      if (buy_order.px.toDouble() > best_buy_px) {
        // If there's already a more aggressive buy, don't join.
        dont_place = true;
      } else {
      }
    }

    if (!dont_place && best_buy_px > 0 && std::isfinite(best_buy_px)) {
      // Make sure order size meets min notional.
      if (eff_buy_sz_dbl * best_buy_px < 11) { // real min notional is 10, but adding buffer
        LOG(WARNING) << fmt::format(
            "({}) Buy order size below min notional of 10 "
            "(px {} sz {}). Sending min size instead.",
            symbol_.get(), best_buy_px, eff_buy_sz_dbl);
        eff_buy_sz_dbl = 11.0 / best_buy_px;
        eff_buy_sz = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, eff_buy_sz_dbl, true);
      }

      if (eff_buy_sz > Quantity{0}) {
        // Place.

        NewOrder ord{symbol_,
          traded_books_[0],
          Side::Buy,
          eff_buy_sz,
          Price{std::to_string(best_buy_px)},
          OrderType::Limit,
          tif_,
          false,
          false};
        // Send it. Add it to container.

        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          buy_orders_.emplace(buy_orders_.begin(),
                              SimpleOrder{pkord_id, Side::Buy, Price{std::to_string(best_buy_px)},
                                          eff_buy_sz, SimpleOrderState::LIVE, time_utils::nowToMs(),
                                          0});
          last_t_place_ = time_utils::nowToMs();
        }
      }
    }
  }

  // Sell?
  if (eff_sell_sz_dbl > 0 && ordex_allowed_sell > eff_sell_sz_dbl
      // Restrict the outstanding backlevels.
      && sell_orders_.size() <= max_back_levels_) {
    double best_sell_px = pred_px_sell_ + place_thresh_sell_;
    // Potentially modify placement if we're close to being profitable.
    // But do it differently than the thesh_take_profit way. This is an
    // opposite view of the

    double top_ask = lvl_book->sellSide().begin()->px.toDouble();

    if (best_sell_px < top_ask && adding_no_cutin_) {
      // Undo this purposefully.
      best_sell_px = top_ask;
    }

    // Tick rounding needs to happen after all changes to best_sell_px have been made.
    best_sell_px = roundToSide(best_sell_px, true);

    bool dont_place = false;
    for (auto& sell_order : sell_orders_) {
      if (sell_order.cancel_time != 0) {
        continue;
      }

      if (std::abs(sell_order.px.toDouble() - best_sell_px) <
          front_rung_spacing_mult_ * rung_spacing_px_) {
        // Too crowded.
        dont_place = true;
      }

      if (sell_order.px.toDouble() < best_sell_px) {
        // If there's already a more aggressive buy, don't join.
        dont_place = true;
      } else {
      }
    }

    if (!dont_place && best_sell_px > 0 && std::isfinite(best_sell_px)) {
      // Make sure order size meets min notional.
      if (eff_sell_sz_dbl * best_sell_px < 11) { // real min notional is 10, but adding buffer
        LOG(WARNING) << fmt::format(
            "({}) Sell order size below min notional of 10 "
            "(px {} sz {}). Sending min size instead.",
            symbol_.get(), best_sell_px, eff_sell_sz_dbl);
        eff_sell_sz_dbl = 11.0 / best_sell_px;
        eff_sell_sz = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, eff_sell_sz_dbl, true);
      }

      if (eff_sell_sz > Quantity{0}) {
        NewOrder ord{symbol_,
          traded_books_[0],
          Side::Sell,
          eff_sell_sz,
          Price{std::to_string(best_sell_px)},
          OrderType::Limit,
          tif_,
          false,
          false};
        // Send it. Add it to container.
        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          sell_orders_.emplace(sell_orders_.begin(),
                               SimpleOrder{pkord_id, Side::Sell,
                                           Price{std::to_string(best_sell_px)}, eff_sell_sz,
                                           SimpleOrderState::LIVE, time_utils::nowToMs(), 0});
          last_t_place_ = time_utils::nowToMs();
        }
      }
    }
  }

  // Print this out.
  // std::cout << cur_ord_str << std::endl;
}

void WideMM::maybeCancel(
    // Just the pure alpha, not even the predpx.
    const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
    double ordex_allowed_sell) {
  double buy_total_thresh = place_thresh_buy_ - cancel_buffer_buy_px_;
  double sell_total_thresh = place_thresh_sell_ - cancel_buffer_sell_px_;

  // Check the book side.
  bool should_cancel = false;

  // Cancels due to predpx.
  // Start from the inside.
  int n_buy_iters = 0;
  for (auto& topbuy_it : buy_orders_) {

    if (canCancelOrd(topbuy_it)) {
      double top_buy_px = topbuy_it.px.toDouble();

      should_cancel = false;

      if (pred_px_buy_ - top_buy_px < buy_total_thresh) {
        should_cancel = true;
      }

      // Maybe this should be moved to the backlevel side actually.
      if (ordex_allowed_buy < 0 || cur_pos >= max_pos_.toDouble()) {
        // printf("ordex_allowed_buy caused: %f \n", ordex_allowed_buy);
        should_cancel = true;
      } else if (n_buy_iters == 0 && will_cancel_isolated_ &&
                 pktrade::approx_equal(top_buy_px, ref_sig_->getBBMeasure()) &&
                 topbuy_it.init_qty.toDouble() >= ref_sig_->getBBSize() - pktrade::EPS) {
        // We might be stranded at top. Check the next level.
        auto& buy_side = lvl_book->buySide();
        auto next_lvl_it = std::next(buy_side.begin());
        if (next_lvl_it != buy_side.end() &&
            top_buy_px - next_lvl_it->px.toDouble() > min_tick_ + pktrade::EPS) {
          should_cancel = true;
        }
      }

      // Front size-cap re-enforcement: cancel if outstanding front qty is
      // meaningfully above the current sided cap. 1.3x band avoids flicker;
      // maybeJoin re-quotes at the new (lower) cap on the next tick.
      if (n_buy_iters == 0 && local_size_cap_mult_ > 0 && inside_size_buy_ems_ > 0 &&
          topbuy_it.init_qty.toDouble() >
              1.3 * local_size_cap_mult_ * inside_size_buy_ems_) {
        should_cancel = true;
      }

      // cancel_buffer is positive.
      if (should_cancel) {
        // Then cancel.
        /*
              printf("Cancelling %d buy because of %f %f (%f vs %f) || allowed
           %f\n", buy_orders_.begin()->pk_oid, pred_px, top_buy_px, pred_px -
           top_buy_px, buy_thresh - cancel_buffer_px, ordex_allowed_buy);
                     */
        // Add a cancelTime to this.
        topbuy_it.cancel_time = time_utils::nowToMs();
        CancelOrder cancelOrd{topbuy_it.pk_oid};
        riskman_->cancelOrd(cancelOrd);
        last_t_cxl_ = time_utils::nowToMs();
      }
    }
    n_buy_iters++;
  }
  // Asks.

  int n_sell_iters = 0;
  for (auto& topsell_it : sell_orders_) {
    if (canCancelOrd(topsell_it)) {
      double top_sell_px = topsell_it.px.toDouble();

      should_cancel = false;
      // Check thresh.

      if (top_sell_px - pred_px_sell_ < sell_total_thresh) {
        should_cancel = true;
      }

      if (ordex_allowed_sell < 0 && cur_pos <= -max_pos_.toDouble()) {
        should_cancel = true;
        // std::cout << " Cancelling sell because of maxpos reasons" <<
        // std::endl; printf("ordex_allowed_sell caused: %f \n",
        // ordex_allowed_sell);
      } else if (n_sell_iters == 0 && will_cancel_isolated_ &&
                 pktrade::approx_equal(top_sell_px, ref_sig_->getBAMeasure()) &&
                 topsell_it.init_qty.toDouble() >= ref_sig_->getBASize() - pktrade::EPS) {
        // We might be stranded at top. Check the next level.
        auto& sell_side = lvl_book->sellSide();
        auto next_lvl_it = std::next(sell_side.begin());
        if (next_lvl_it != sell_side.end() &&
            next_lvl_it->px.toDouble() - top_sell_px > min_tick_ + pktrade::EPS) {
          should_cancel = true;
        }
      }

      // Front size-cap re-enforcement (sided): see buy-side comment above.
      if (n_sell_iters == 0 && local_size_cap_mult_ > 0 && inside_size_sell_ems_ > 0 &&
          topsell_it.init_qty.toDouble() >
              1.3 * local_size_cap_mult_ * inside_size_sell_ems_) {
        should_cancel = true;
      }

      // cancel_buffer is positive.
      if (should_cancel) {
        // Then cancel.
        /*
            printf(
                "%s: Cancelling sell %d because of %f %f (%f vs %f) || allowed
           %f\n", time_utils::nowToStr().c_str(),
           sell_orders_.begin()->pk_oid, top_sell_px, pred_px, top_sell_px -
           pred_px, sell_thresh - cancel_buffer_px, ordex_allowed_sell);
      */
        if (ordex_allowed_sell == 0) {
          // printOutstandingOrders();
        }

        topsell_it.cancel_time = time_utils::nowToMs();
        CancelOrder cancelOrd{topsell_it.pk_oid};
        riskman_->cancelOrd(cancelOrd);

        last_t_cxl_ = time_utils::nowToMs();
        // Do not remove this until we get the ordCancel.
      }
    }
    n_sell_iters++;
  }
}

void WideMM::manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy,
                              double abs_allowed_sell) {

  // Backlevels use full order_size_, not the inside-cap. The sparsity check
  // (min_foreign_inside_mult) is the backlevel-side protection; capping size
  // there too would over-restrict deep orders that aren't the front target.
  Quantity eff_sz = order_size_;
  double eff_sz_dbl = eff_sz.toDouble();

  // Cancel backlevels due to number of backlevels or remaining allowed size.
  if (buy_orders_.size() > 1 &&
      (buy_orders_.size() >= max_back_levels_ || abs_allowed_buy < eff_sz_dbl)) {
    // Cancel stuff from the end.

    // Also possible that the #cancel_shares already gets us past
    // abs_allowed_sell. So let's keep a count.
    double already_cxl_shrs = 0;
    int already_cxl_orders_ = 0;

    for (auto it = buy_orders_.rbegin(); it != buy_orders_.rend(); it++) {
      // Backlevels should not care about the front order.
      if (it->pk_oid == buy_orders_.begin()->pk_oid) {
        break;
      }

      if (buy_orders_.size() - already_cxl_orders_ < max_back_levels_ &&
          already_cxl_shrs + abs_allowed_buy > eff_sz_dbl) {
        break;
      }

      if (it->cancel_time != 0) {
        already_cxl_shrs += eff_sz_dbl;
        already_cxl_orders_++;
      }

      if (canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;

        // Should be enough.
        break;
      }
    }
  } else if (
      place_back_levels_ && eff_sz_dbl > 0 &&
      buy_orders_.size() > 0 && buy_orders_.size() < max_back_levels_ - 2 &&
             abs_allowed_buy > eff_sz_dbl) {

    // We have space in the stack, so we can almost certainly place a
    // backlevel. We iterate through the orders to see the next spot
    // we can place it. We track this with new_backlevel_px.

    auto prev_buy_order = buy_orders_.begin();
    double prev_buy_px = prev_buy_order->px.toDouble();

    // Create a variable rung spacing
    // Give it a little more rungsize so we don't flicker backlevels.
    double effective_rung_spacing = rung_spacing_px_;

    // Potential price where we would place a new backlevel.
    // It might be between two orders, or at the end of the stack.
    double new_backlevel_px = roundToSide(prev_buy_px - effective_rung_spacing, false);

    auto next_buy_order = prev_buy_order + 1;

    while (next_buy_order != buy_orders_.end()) {
      double next_buy_px = next_buy_order->px.toDouble();

      // Check if we have space.
      if (new_backlevel_px - next_buy_px > effective_rung_spacing) {
        break;
      }
      // Otherwise, continue and reset our state.
      effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      prev_buy_px = next_buy_px;
      new_backlevel_px = roundToSide(prev_buy_px - effective_rung_spacing, false);
      prev_buy_order = next_buy_order;
      next_buy_order++;
    }

    // Sparsity check: require min_foreign_inside_mult_ * inside_size_buy_ems_
    // shares of foreign liquidity between prev_buy_px and new_backlevel_px.
    // Pushes new_backlevel_px deeper if needed; NaN if book lacks the liquidity.
    // Skipped until EMS warms up.
    if (min_foreign_inside_mult_ > 0 && inside_size_buy_ems_ > 0) {
      double sparse_thresh = min_foreign_inside_mult_ * inside_size_buy_ems_;
      double sparse_px = protection::sparseBuyBacklevelPx(
          lvl_book, prev_buy_px, new_backlevel_px, sparse_thresh,
          buy_orders_, min_tick_);
      if (!std::isfinite(sparse_px)) {
        // Not enough foreign liquidity anywhere below us; skip this placement.
        new_backlevel_px = std::numeric_limits<double>::quiet_NaN();
      } else {
        new_backlevel_px = roundToSide(sparse_px, false);
      }
    }

    // Place backlevel size.
    // Then, we can place the backlevel.
    // Place the backlevel.
    if (new_backlevel_px > 0 && std::isfinite(new_backlevel_px)) {
      LOG(INFO) << "BACKBUY " << new_backlevel_px;
      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Buy,
                   eff_sz,
                   Price{std::to_string(new_backlevel_px)},
                   OrderType::Limit,
                   tif_,
                   false,
                   false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Back);

        // Place it in the back.
        buy_orders_.emplace(next_buy_order,
                            SimpleOrder{pkord_id, Side::Buy,
                                        Price{std::to_string(new_backlevel_px)}, eff_sz,
                                        SimpleOrderState::LIVE, now_fire_t_, 0});
      }
    }
    // Just place one
  }

  // Sell.
  if (sell_orders_.size() > 1 &&
      (sell_orders_.size() >= max_back_levels_ || abs_allowed_sell < eff_sz_dbl)) {
    double already_cxl_shrs = 0;
    int already_cxl_orders_ = 0;

    for (auto it = sell_orders_.rbegin(); it != sell_orders_.rend(); it++) {
      // If it's just one size. Then it might not be a backlevel.
      if (it->pk_oid == sell_orders_.begin()->pk_oid) {
        break;
      }

      if (sell_orders_.size() - already_cxl_orders_ < max_back_levels_ &&
          already_cxl_shrs + abs_allowed_sell > eff_sz_dbl) {
        break;
      }

      if (it->cancel_time != 0) {
        already_cxl_shrs += eff_sz_dbl;
        already_cxl_orders_++;
      }

      if (canCancelOrd(*it)) {
        // std::cout << " cancelbacklevels 1 for " << it->pk_oid << std::endl;
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
        break;
      }
    }
  } else if (
      place_back_levels_ && eff_sz_dbl > 0 &&
      sell_orders_.size() > 0 && sell_orders_.size() < max_back_levels_ - 2 &&
             abs_allowed_sell > eff_sz_dbl) {
    // We have space in the stack, so we can almost certainly place a
    // backlevel.

    auto prev_sell_order = sell_orders_.begin();
    double prev_sell_px = prev_sell_order->px.toDouble();

    // Create a variable rung spacing
    double effective_rung_spacing = rung_spacing_px_;

    double new_backlevel_px = roundToSide(prev_sell_px + effective_rung_spacing, true);
    auto next_sell_order = prev_sell_order + 1;

    while (next_sell_order != sell_orders_.end()) {
      double next_sell_px = next_sell_order->px.toDouble();

      if (next_sell_px - new_backlevel_px > effective_rung_spacing) {
        break;
      }

      effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      prev_sell_px = next_sell_px;
      new_backlevel_px = roundToSide(prev_sell_px + effective_rung_spacing, true);
      prev_sell_order = next_sell_order;
      next_sell_order++;
    }

    if (min_foreign_inside_mult_ > 0 && inside_size_sell_ems_ > 0) {
      double sparse_thresh = min_foreign_inside_mult_ * inside_size_sell_ems_;
      double sparse_px = protection::sparseSellBacklevelPx(
          lvl_book, prev_sell_px, new_backlevel_px, sparse_thresh,
          sell_orders_, min_tick_);
      if (!std::isfinite(sparse_px)) {
        new_backlevel_px = std::numeric_limits<double>::quiet_NaN();
      } else {
        new_backlevel_px = roundToSide(sparse_px, true);
      }
    }

    if (new_backlevel_px > 0 && std::isfinite(new_backlevel_px)) {

      // Place the backlevel.
      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Sell,
                   eff_sz,
                   Price{std::to_string(new_backlevel_px)},
                   OrderType::Limit,
                   tif_,
                   false,
                   false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Back);

        // Place it in the back.
        sell_orders_.emplace(next_sell_order,
                             SimpleOrder{pkord_id, Side::Sell,
                                         Price{std::to_string(new_backlevel_px)}, eff_sz,
                                         SimpleOrderState::LIVE, now_fire_t_, 0});
      }
    }
    // Just place one.
  }

  // Cancel backlevels that violate rung_spacing constraints.
  // Walk through buy orders and ensure proper spacing.
  if (buy_orders_.size() > 1) {
    auto prev_buy_it = buy_orders_.begin();
    double prev_buy_px = prev_buy_it->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;

    for (auto it = prev_buy_it + 1; it != buy_orders_.end(); it++) {
      double cur_buy_px = it->px.toDouble();
      double spacing = prev_buy_px - cur_buy_px;

      // If spacing is too tight, cancel this order
      if (spacing < effective_rung_spacing && canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
      } else {
        // Update for next iteration
        prev_buy_px = cur_buy_px;
        effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      }
    }
  }

  // Walk through sell orders and ensure proper spacing.
  if (sell_orders_.size() > 1) {
    auto prev_sell_it = sell_orders_.begin();
    double prev_sell_px = prev_sell_it->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;

    for (auto it = prev_sell_it + 1; it != sell_orders_.end(); it++) {
      double cur_sell_px = it->px.toDouble();
      double spacing = cur_sell_px - prev_sell_px;

      // If spacing is too tight, cancel this order
      if (spacing < effective_rung_spacing && canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
      } else {
        // Update for next iteration
        prev_sell_px = cur_sell_px;
        effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      }
    }
  }

  last_t_backlevel_ = now_fire_t_;
}

void WideMM::onFirstFire() {
  LOG(INFO) << "ONFIRSTFIRE HAPPENING";

  approx_mid_ = ref_sig_->getValue();

  // Scale the order sizes from their notional targets.
  if (set_size_from_notional_) {
    Quantity tgt_order_sz = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_order_notional_ / approx_mid_, true);
    Quantity tgt_max_pos = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_maxpos_notional_ / approx_mid_, true);

    LOG(INFO) << fmt::format("Setting order size from notional, midpx {} : ordersize {} maxpos {}",
                             approx_mid_, tgt_order_sz.toDouble(), tgt_max_pos.toDouble());

    // Sanity check: ordex maxpos should not exceed riskman's max_position.
    if (riskman_ != nullptr) {
      double risk_maxpos = riskman_->getMaxPos().toDouble();
      if (tgt_max_pos.toDouble() > risk_maxpos) {
        LOG(ERROR) << fmt::format(
            "({}) Ordex notional-derived maxpos ({}) exceeds risk max_position ({}). "
            "Orders will be rejected. Increase risk max_position.",
            symbol_.get(), tgt_max_pos.toDouble(), risk_maxpos);
      }
      if (tgt_order_sz.toDouble() > risk_maxpos) {
        LOG(ERROR) << fmt::format(
            "({}) Ordex notional-derived order_size ({}) exceeds risk max_position ({}). "
            "No orders will be placed. Increase risk max_position.",
            symbol_.get(), tgt_order_sz.toDouble(), risk_maxpos);
      }
    }

    order_size_ = tgt_order_sz;
    max_pos_ = tgt_max_pos;
  }

  // Scale the backlevel-related threhsolds.
  if (fixed_rung_spacing_thresh_ == 0) {
    rung_spacing_px_ = effThreshMult() * rung_spacing_mult_ * base_place_thresh_conf_ * approx_mid_;
  } else {
    rung_spacing_px_ = fixed_rung_spacing_thresh_ * approx_mid_;
  }
  // Minimize it with min_tick.

  rung_spacing_px_ = std::max(
      rung_spacing_px_, pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble());

  LOG(INFO) << fmt::format("Firstfire rung_spacing_px {} max_back_levels {}", rung_spacing_px_,
                           max_back_levels_);

  needs_first_fire_ = false;

  refreshSizeMultBaseFromCurrent();
}

// This should be called every time any component changes, including in_relative_mode_.
void WideMM::recalcThreshes() { mid_thresh_ = base_place_thresh_conf_ * effThreshMult(); }

void WideMM::setThresh(double thresh) {
  if (thresh < 0.000005 || thresh > 0.2) {
    LOG(INFO) << fmt::format("({}) Probably sent wrong threshold {}, skipping", symbol_.get(),
                             thresh);
    return;
  }
  base_place_thresh_conf_ = thresh;
  // Override and reset the usermsg thresh mult to 1, but preserve extra
  // threshes. The bleed/minfv slots are left alone: they're defensive state
  // owned by their own writers, and clearing them here would desync us from the
  // riskman's bleed_widened_ flag (it wouldn't re-widen).
  um_thresh_mult_ = 1;
  recalcThreshes();
  LOG(INFO) << fmt::format("({}) Base thresh set to {}. Final threshes: {}", symbol_.get(), thresh,
                           mid_thresh_);
}

void WideMM::setThreshMult(double thresh_mult, MultSource src) {
  // Create some sanity checks...
  if (thresh_mult < 0.1 || thresh_mult > 100) {
    LOG(ERROR) << fmt::format("({}) Probably sent wrong threshold multiplier {}, skipping",
                              symbol_.get(), thresh_mult);
    return;
  }
  // Remember the mult so we can reapply it on other usermsgs adjusting thresh
  if (src == MultSource::Usermsg) {
    um_thresh_mult_ = thresh_mult;
  } else if (src == MultSource::Bleed) {
    bleed_thresh_mult_ = thresh_mult;
  } else if (src == MultSource::MinFv) {
    minfv_thresh_mult_ = thresh_mult;
  } else {
    LOG(ERROR) << fmt::format("({}) Unrecognized MultSource {} for thresh mult, skipping",
                              symbol_.get(), magic_enum::enum_name(src));
    return;
  }
  double eff_mult = effThreshMult();

  if (fixed_rung_spacing_thresh_ == 0 && (!needs_first_fire_)) {
    rung_spacing_px_ = eff_mult * rung_spacing_mult_ * base_place_thresh_conf_ * approx_mid_;
    rung_spacing_px_ = std::max(
        rung_spacing_px_, pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble());
  }


  recalcThreshes();
  LOG(INFO) << fmt::format(
      "({}) Thresh mult set to {} from {}. um {} bleed {} minfv {} eff {}. Final threshes: {}",
      symbol_.get(), thresh_mult, magic_enum::enum_name(src), um_thresh_mult_, bleed_thresh_mult_,
      minfv_thresh_mult_, eff_mult, mid_thresh_);
}

// I think I won't accept an order size.
void WideMM::setOrderSize(double size) {
  if (size <= 0) {
    LOG(ERROR) << "Invalid order size " << size << " , skipping";
    return;
  }
  LOG(INFO) << "WideMM: setOrderSize " << order_size_ << " -> " << size;
  order_size_ = Quantity{std::to_string(size)};
  refreshSizeMultBaseFromCurrent();
}

void WideMM::setSizeMult(double size_mult, MultSource src) {
  if (size_mult <= 0) {
    LOG(ERROR) << fmt::format("({}) Invalid size mult {}", symbol_.get(), size_mult);
    return;
  }

  if (!has_base_sizes_) {
    refreshSizeMultBaseFromCurrent();
  }
  if (src == MultSource::Conf) {
    conf_size_mult_ = size_mult;
  } else if (src == MultSource::Usermsg) {
    um_size_mult_ = size_mult;
  } else if (src == MultSource::DoubleDown) {
    dd_size_mult_ = size_mult;
  } else {
    LOG(ERROR) << fmt::format("({}) Unrecognized MultSource {}, skipping", symbol_.get(),
                              magic_enum::enum_name(src));
    return;
  }
  double eff_mult = effSizeMult();

  Quantity new_maxpos =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, base_max_pos_ * eff_mult, true);
  Quantity new_order_size =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, base_order_size_ * eff_mult, true);
  Quantity new_cross_order_size = pktrade::GlobalVar::secmaster_->round_qty(
      traded_bid_, base_cross_order_size_ * eff_mult, true);

  LOG(INFO) << fmt::format("({}) setSizeMult ({} from {}) conf {} um {} dd {} eff {} maxpos {} -> {} "
                           "ordersize {} -> {} crossordersize {} -> {}",
                           symbol_.get(), size_mult, magic_enum::enum_name(src), conf_size_mult_,
                           um_size_mult_, dd_size_mult_, eff_mult, max_pos_.toDouble(),
                           new_maxpos.toDouble(),
                           order_size_.toDouble(), new_order_size.toDouble(),
                           cross_order_size_.toDouble(), new_cross_order_size.toDouble());
  if (set_size_from_notional_) {
    double new_tgt_order_notional = base_tgt_order_notional_ * eff_mult;
    double new_tgt_maxpos_notional = base_tgt_maxpos_notional_ * eff_mult;

    LOG(INFO) << fmt::format("({}) notionaltgts changed order ({} -> {}) maxpos ({} -> {})",
                             symbol_.get(), tgt_order_notional_, new_tgt_order_notional,
                             tgt_maxpos_notional_, new_tgt_maxpos_notional);
    tgt_order_notional_ = new_tgt_order_notional;
    tgt_maxpos_notional_ = new_tgt_maxpos_notional;
  }

  max_pos_ = new_maxpos;
  order_size_ = new_order_size;
  cross_order_size_ = new_cross_order_size;

  // Also needs to update the pcurv_frac_denom.
  pcurv_frac_denom_ = base_pcurv_frac_denom_ * eff_mult;
}

Quantity WideMM::getMaxPos() { return max_pos_; }

void WideMM::refreshSizeMultBaseFromCurrent() {
  double denom = effSizeMult();
  if (denom <= 0) {
    denom = 1;
  }
  base_max_pos_ = max_pos_.toDouble() / denom;
  base_order_size_ = order_size_.toDouble() / denom;
  base_cross_order_size_ = cross_order_size_.toDouble() / denom;
  base_tgt_order_notional_ = tgt_order_notional_ / denom;
  base_tgt_maxpos_notional_ = tgt_maxpos_notional_ / denom;
  base_pcurv_frac_denom_ = pcurv_frac_denom_ / denom;
  has_base_sizes_ = true;
}


SymbolId WideMM::getTradedSymbols() { return symbol_; }

std::vector<Market> WideMM::getMarkets() { return {traded_books_}; }

std::vector<pktrade::BookId> WideMM::getBookIds() {
  std::vector<pktrade::BookId> book_ids;
  for (pktrade::Market m : traded_books_) {
    book_ids.push_back({m, symbol_});
  }
  return book_ids;
}

// callbacks from risk.
// I guess this doesn't do anything either. OK.
void WideMM::newOrdAck(const Order& ord) {}

// This doesn't do anything.
void WideMM::ordUpdate(const Order& ord) {}

// We can remove it from our data structure then.
void WideMM::ordCancel(const Order& ord) {
  if (ord.side == Side::Buy) {
    for (auto it = buy_orders_.begin(); it != buy_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        it = buy_orders_.erase(it);

        break;
      }
    }
  } else if (ord.side == Side::Sell) {
    for (auto it = sell_orders_.begin(); it != sell_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        it = sell_orders_.erase(it);
        break;
      }
    }
  }
}

// The only thing to do is to remove it from the data structure.
void WideMM::ordExec(const Order& ord, const OrderExecute& exec) {
  if (print_ords_) {
    printf("----------------------\n");
    printf("%s: exec on ord %d px %f size %f\n", time_utils::nowToStr().c_str(), ord.pk_order_id,
           ord.px.toDouble(), exec.qty.toDouble());
    printOutstandingOrders();

    // Also print some of these other things out.
    double mid_price = ref_sig_->getValue();
    // Do capping of extreme alpha values.
    double alpha = pred_sig_->getValue();

    printf("----------------------\n");
  }

  if (ord.curr_qty == Quantity{0}) {
    // Remove it from the
    ordCancel(ord);
  }

  // Update the narrowing sum.
  if (narrow_when_few_trades_) {
    // Time decay since the last one.
    double decay =
        std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / narrow_thresh_time_const_ms_);

    narrow_fewtrade_ems_buy_ *= decay;
    narrow_fewtrade_ems_sell_ *= decay;

    // Add proportional exec size.

    if (ord.side == Side::Buy) {
      narrow_fewtrade_ems_buy_ += exec.qty.toDouble() / max_pos_.toDouble();
    } else {
      narrow_fewtrade_ems_sell_ += exec.qty.toDouble() / max_pos_.toDouble();
    }
  }

  // Decay and update curv_impulse.
  if (curv_impulse_coef_ > 0) {
    double impulse_decay =
        std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / curv_impulse_tdc_ms_);
    curv_impulse_ *= impulse_decay;
    double xtra_impulse = exec.qty.toDouble() / order_size_.toDouble();
    if (ord.side == Side::Sell) {
      xtra_impulse *= -1;
    }
    curv_impulse_ += xtra_impulse;
  }

  // Record that we have an actual exec.
  last_exec_decay_t_ = time_utils::nowToMs();
}

// Shouldn't happen. But it might. Let's just call the same thing as
// ordcancel.
void WideMM::ordElim(const Order& ord, const OrderElimination& elim) { ordCancel(ord); }

void WideMM::ordReject(const Order& ord, const NewOrderReject& rej) {
  // Copying from ordCancel, but I want to log this explicitly too.

  // OK, this is kind of a crazy case, but I'm going to just addresss it here.
  // In one case, I saw us hear the reject almost immediately, and "before" we
  // got to the NewOrd-ness? So, just putting an extra check here,
  bool found = false;

  if (ord.side == Side::Buy) {
    for (auto it = buy_orders_.begin(); it != buy_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        it = buy_orders_.erase(it);
        found = true;
        break;
      }
    }
  } else if (ord.side == Side::Sell) {
    for (auto it = sell_orders_.begin(); it != sell_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        it = sell_orders_.erase(it);
        found = true;
        break;
      }
    }
  }

  if (!found) {
    // Probably some race condition. Deal with it later.
    // Also might just be iocs. We get rejections too. 
    LOG(ERROR) << "WideMM: could NOT find rejected order " << ord.pk_order_id << " to remove";

    // Push this onto the reject list.
    unhandled_rejected_pkcoids_.push_back(ord.pk_order_id);
  }
}

void WideMM::flushUnhandledRejectedOrds() {
  for (PKOrderId pk_ord_id : unhandled_rejected_pkcoids_) {
    bool found = false;

    for (auto it = buy_orders_.begin(); it != buy_orders_.end(); it++) {
      if (it->pk_oid == pk_ord_id) {
        it = buy_orders_.erase(it);
        found = true;
        break;
      }
    }

    for (auto it = sell_orders_.begin(); it != sell_orders_.end(); it++) {
      if (it->pk_oid == pk_ord_id) {
        it = sell_orders_.erase(it);
        found = true;
        break;
      }
    }

    if (!found) {
      // Still didn't find it on FLUSH
      LOG(ERROR) << "WideMM: could NOT find rejected order, on FLUSH " << pk_ord_id;
    }
  }
  // At the end, we should empty this.
  unhandled_rejected_pkcoids_.clear();
}
// Manage and upkeep on refsig-based threshing.
void WideMM::onSignalValue(int sig_id, double value) {
  if (last_mid_px_ == 0) {
    // Early exit if we haven't initialized.

    last_mid_px_ = value;
    last_mid_t_ = time_utils::nowToMs();
    return;
  }

  // Otherwise.
  // Calculate the return
  double ret = (value - last_mid_px_) / last_mid_px_;

  // Decay the EMS's

  int64_t delta_t = time_utils::nowToMs() - last_mid_t_;

  double decay = std::exp(-delta_t / mid_move_ems_time_const_ms_);

  // Decay the ems' for midmove.

  net_mid_move_ems_ *= decay;
  net_mid_move_ems_ += ret;

  abs_mid_move_ems_ *= decay;
  abs_mid_move_ems_ += std::abs(ret);

  // ------------------------------------------------------------
  // Update spread too.
  const LevelBook* local_bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  double lbb = local_bk->buySide().begin()->px.toDouble();
  double lba = local_bk->sellSide().begin()->px.toDouble();
  local_spread_num_ *= decay;
  local_spread_denom_ *= decay;

  double lbsize = local_bk->buySide().begin()->qty.toDouble();

  local_spread_ = ref_sig_->getBAMeasure() - ref_sig_->getBBMeasure();

  local_spread_num_ += (local_spread_);
  local_spread_denom_ += 1;
  // Reset the count.

  if (local_spread_denom_ > 0) {
    spread_ema_ = local_spread_num_ / local_spread_denom_;
  }

  // ------------------------------------------------------------

  last_mid_px_ = value;
  last_mid_t_ = time_utils::nowToMs();

  // Update vol mechanisms (1-3).
  vol_mechs_.onRemoteUpdate(value);

  // exec decays.
  applyExecDecays();
}

// ord rejections.

void WideMM::cancelOutstandingOrds() {
  if (buy_orders_.size() > 0) {
    for (auto it = buy_orders_.begin(); it != buy_orders_.end(); it++) {
      // Cancel it.
      if (canCancelOrd(*it, true)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = time_utils::nowToMs();
        last_t_cxl_ = time_utils::nowToMs();
      }
    }
  }

  if (sell_orders_.size() > 0) {
    for (auto it = sell_orders_.begin(); it != sell_orders_.end(); it++) {
      // Cancel it.
      if (canCancelOrd(*it, true)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = time_utils::nowToMs();
        last_t_cxl_ = time_utils::nowToMs();
      }
    }
  }
}

void WideMM::printOutstandingOrders() {
  const LevelBook* lvl_book = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  auto& buy_side = lvl_book->buySide();
  double top_bid_px = buy_side.begin()->px.toDouble();

  auto& sell_side = lvl_book->sellSide();
  double top_ask_px = sell_side.begin()->px.toDouble();

  printf("*************** WideMM_ORDERS ***************\n");
  // Do capping of extreme alpha values.
  double alpha = pred_sig_->getValue();
  std::string out_str = "";
  out_str += fmt::format("{}: BB BA {} {} pos {} alpha {} predpx {} market ({} {})\n \n",
                         time_utils::nowToStr().c_str(), top_bid_px, top_ask_px,
                         riskman_->getPos().toDouble(), alpha,

                         pred_px_, pred_px_ + place_thresh_sell_, pred_px_ - place_thresh_buy_);

  // Also print other stats
  out_str += fmt::format("Stats:  mm_ems (net abs) ({} {}) spread_ema {} spread_ratio "
                         "%f \n",
                         net_mid_move_ems_, abs_mid_move_ems_, spread_ema_, spread_ema_ / mid_px_);

  // OK, I'm going to do something inefficient but want to just print out the
  // state of the book too. Let's push all of these things into a vector and
  // then display the vector.

  std::vector<std::string> sell_lvls;
  std::vector<std::string> buy_lvls;

  // The sell side.
  auto sell_order_it = sell_orders_.begin();
  for (auto ask_it = sell_side.begin();
       ask_it != sell_side.end() && sell_order_it != sell_orders_.end(); ask_it++) {
    // Step 1. Our orders might be alone at the top.
    if (ask_it->px > sell_order_it->px) {
      // We're on a price level that's not reflected by the book update.
      // Repeatedly increment the buy_order_it until it's on the same level.
      while (sell_order_it != sell_orders_.end() && ask_it->px > sell_order_it->px) {
        sell_lvls.push_back(fmt::format("{} : Ord {} (next_lvl {}) \n",
                                        sell_order_it->px.toDouble(), sell_order_it->pk_oid,
                                        ask_it->px.toDouble()));
        sell_order_it++;
      }
    }

    if (sell_order_it == sell_orders_.end()) {
      break;
    }

    // After we potentially incremented a lot. Are we at a price level
    // with our orders?
    if (ask_it->px == sell_order_it->px) {
      // We're on a price level with lit orders.
      // Now, showing the flagged shares.
      sell_lvls.push_back(fmt::format("{} {} ({} flagged): Ord {} \n", ask_it->px.toDouble(),
                                      ask_it->qty.toDouble() - ask_it->flagged_shares.toDouble(),
                                      ask_it->flagged_shares.toDouble(), sell_order_it->pk_oid));
      sell_order_it++;
      for (; sell_order_it->px == ask_it->px && sell_order_it != sell_orders_.end();
           sell_order_it++) {
        sell_lvls.push_back(fmt::format("  (same px)      : Ord {} \n", sell_order_it->pk_oid));
      }

    } else {
      // The book level doesn't contain an order.
      sell_lvls.push_back(fmt::format("{} {}  ({} flagged)\n", ask_it->px.toDouble(),
                                      ask_it->qty.toDouble() - ask_it->flagged_shares.toDouble(),
                                      ask_it->flagged_shares.toDouble()));
    }
  }
  // Go through the rest of the orders.
  for (; sell_order_it != sell_orders_.end(); sell_order_it++) {
    sell_lvls.push_back(
        fmt::format("{} : Ord {} \n", sell_order_it->px.toDouble(), sell_order_it->pk_oid));
  }

  // The buy side
  auto buy_order_it = buy_orders_.begin();
  for (auto bid_it = buy_side.begin();
       bid_it != buy_side.end() && buy_order_it != buy_orders_.end(); bid_it++) {
    // Are we alone, at the top?
    if (bid_it->px < buy_order_it->px) {
      // We're on a level by ourselves.
      // Repeatedly increment the buy_order_it until it's on the same level.
      while (buy_order_it != buy_orders_.end() && bid_it->px < buy_order_it->px) {
        buy_lvls.push_back(fmt::format("{} : Ord {} (next_lvl {}) \n", buy_order_it->px.toDouble(),
                                       buy_order_it->pk_oid, bid_it->px.toDouble()));
        buy_order_it++;
      }
    }

    if (buy_order_it == buy_orders_.end()) {
      break;
    }

    // Do we have a sell order here?
    if (bid_it->px == buy_order_it->px) {
      buy_lvls.push_back(fmt::format("{} {} ({} flagged): Ord {} \n", bid_it->px.toDouble(),
                                     bid_it->qty.toDouble() - bid_it->flagged_shares.toDouble(),
                                     bid_it->flagged_shares.toDouble(), buy_order_it->pk_oid));
      buy_order_it++;
      for (; buy_order_it->px == bid_it->px && buy_order_it != buy_orders_.end(); buy_order_it++) {
        buy_lvls.push_back(fmt::format("  (same px)      : Ord {} \n", buy_order_it->pk_oid));
      }

    } else {
      buy_lvls.push_back(fmt::format("{} {}  ({} flagged)\n", bid_it->px.toDouble(),
                                     bid_it->qty.toDouble() - bid_it->flagged_shares.toDouble(),
                                     bid_it->flagged_shares.toDouble()

                                         ));
    }
  }
  // Go through the rest of the orders.
  for (; buy_order_it != buy_orders_.end(); buy_order_it++) {
    buy_lvls.push_back(
        fmt::format("{} : Ord {} \n", buy_order_it->px.toDouble(), buy_order_it->pk_oid));
  }

  // OK, print them now too.
  pktrade::util::log_line(out_str, false);
  for (auto sells_it = sell_lvls.rbegin(); sells_it != sell_lvls.rend(); sells_it++) {
    pktrade::util::log_line(*sells_it, false);
  }
  pktrade::util::log_line("\n<>\n", false);

  for (auto buys_it = buy_lvls.begin(); buys_it != buy_lvls.end(); buys_it++) {
    pktrade::util::log_line(*buys_it, false);
  }
  pktrade::util::log_line("*************** END_PRINT ***************\n", false);
}

bool WideMM::canCancelOrd(SimpleOrder& ord, bool fast /*= false*/) {
  int64_t now_ms = time_utils::nowToMs();
  if (!fast && now_ms - ord.place_time < ms_min_ord_lifetime_) {
    return false;
  }
  return (ord.cancel_time == 0 || now_ms - ord.cancel_time > CANCEL_RETRY_INTERVAL);
}

void WideMM::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void WideMM::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void WideMM::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  int64_t transact_t = lvl_del.transact_t;
  // cant do notthin
  if (transact_t == 0) {
    return;
  }
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void WideMM::onTrade(const LevelBook& bk, const Trade& trd) {

  // Just show that trades are happening.
  // Let's check the dist_away in bps.
  double dist_away = std::abs(trd.px.toDouble() - ref_sig_->getValue()) / ref_sig_->getValue();

  // printf("%s : onTrade %f %s dist_away %.4f\n", time_utils::nowToStr().c_str(),
  // trd.px.toDouble(), trd.passive_side == pktrade::Side::Buy ? "SELL" : "BUY", dist_away);

  // Update liq_depth EMS on trades.
  vol_mechs_.onBookUpdate(bk);

  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);

  protection::updateExecVwapEms(trd.px.toDouble(), trd.qty.toDouble(),
                                exec_anchored_mid_tdc_ms_, time_utils::nowToMs(),
                                exec_vwap_num_ems_, exec_vwap_denom_ems_,
                                exec_vwap_last_t_);
}

void WideMM::applyExecDecays() {
  if (narrow_when_few_trades_) {
    // Time decay since the last one.
    double decay =
        std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / narrow_thresh_time_const_ms_);
    narrow_fewtrade_ems_buy_ *= decay;
    narrow_fewtrade_ems_sell_ *= decay;
  }

  // Decay curv_impulse.
  if (curv_impulse_coef_ > 0 && last_exec_decay_t_ > 0) {
    double impulse_decay =
        std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / curv_impulse_tdc_ms_);
    curv_impulse_ *= impulse_decay;
  }

  last_exec_decay_t_ = time_utils::nowToMs();
};

void WideMM::parsePlaceThreshLiqConfig(const rapidjson::Value& ordex_conf) {
  place_thresh_liq_coef_ = place_thresh_spread_coef_;
  if (ordex_conf.HasMember("place_thresh_liq_coef")) {
    place_thresh_liq_coef_ = ordex_conf["place_thresh_liq_coef"].GetDouble();
  }
  if (place_thresh_liq_coef_ < 0 || place_thresh_liq_coef_ > 10) {
    throw std::runtime_error("Invalid place_thresh_liq_coef");
  }

  if (ordex_conf.HasMember("place_thresh_liq_combine_mode")) {
    std::string combine_mode = ordex_conf["place_thresh_liq_combine_mode"].GetString();
    if (combine_mode == "add") {
      place_thresh_liq_combine_mode_ = 0;
    } else if (combine_mode == "max") {
      place_thresh_liq_combine_mode_ = 1;
    } else {
      throw std::runtime_error("Invalid place_thresh_liq_combine_mode");
    }
  }

  if (ordex_conf.HasMember("place_thresh_liq_notional")) {
    place_thresh_liq_notional_ = ordex_conf["place_thresh_liq_notional"].GetDouble();
  }
  if (place_thresh_liq_notional_ <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_notional");
  }

  if (ordex_conf.HasMember("place_thresh_liq_per_level_decay")) {
    place_thresh_liq_per_level_decay_ =
        ordex_conf["place_thresh_liq_per_level_decay"].GetDouble();
  }
  if (place_thresh_liq_per_level_decay_ <= 0 || place_thresh_liq_per_level_decay_ > 1) {
    throw std::runtime_error("Invalid place_thresh_liq_per_level_decay");
  }

  if (ordex_conf.HasMember("place_thresh_liq_use_flagged")) {
    place_thresh_liq_use_flagged_ = ordex_conf["place_thresh_liq_use_flagged"].GetBool();
  }

  if (place_thresh_mode_ != 5) {
    return;
  }

  if (ordex_conf.HasMember("place_thresh_liq_auto_inside_mult")) {
    place_thresh_liq_auto_inside_mult_ =
        ordex_conf["place_thresh_liq_auto_inside_mult"].GetDouble();
  }
  if (place_thresh_liq_auto_inside_mult_ <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_auto_inside_mult");
  }

  if (ordex_conf.HasMember("place_thresh_liq_auto_min_notional")) {
    place_thresh_liq_auto_min_notional_ =
        ordex_conf["place_thresh_liq_auto_min_notional"].GetDouble();
  }
  if (ordex_conf.HasMember("place_thresh_liq_auto_max_notional")) {
    place_thresh_liq_auto_max_notional_ =
        ordex_conf["place_thresh_liq_auto_max_notional"].GetDouble();
  }
  if (place_thresh_liq_auto_min_notional_ < 0 ||
      place_thresh_liq_auto_max_notional_ <= place_thresh_liq_auto_min_notional_) {
    throw std::runtime_error("Invalid place_thresh_liq_auto_notional bounds");
  }

  if (ordex_conf.HasMember("place_thresh_liq_auto_tdc_s")) {
    place_thresh_liq_auto_tdc_ms_ =
        ordex_conf["place_thresh_liq_auto_tdc_s"].GetDouble() * 1000;
  }
  if (place_thresh_liq_auto_tdc_ms_ <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_auto_tdc_s");
  }
}

void WideMM::initPlaceThreshLiqSignal(const rapidjson::Value& ordex_conf,
                                      pktrade::signals::SignalFactory* sf) {
  rapidjson::Document liq_conf;
  liq_conf.SetObject();
  auto& alloc = liq_conf.GetAllocator();

  std::string name = fmt::format("__{}_place_thresh_liq", symbol_.get());
  liq_conf.AddMember("name", rapidjson::Value(name.c_str(), alloc), alloc);
  liq_conf.AddMember("type", "SigLiqBalance", alloc);
  liq_conf.AddMember("symbol", rapidjson::Value(symbol_.get().c_str(), alloc), alloc);

  rapidjson::Value books(rapidjson::kArrayType);
  books.PushBack(rapidjson::Value(ordex_conf["markets"].GetArray()[0].GetString(), alloc), alloc);
  liq_conf.AddMember("books", books, alloc);

  std::string sz_decay_type = "SIZE";
  if (ordex_conf.HasMember("place_thresh_liq_sz_decay_type")) {
    sz_decay_type = ordex_conf["place_thresh_liq_sz_decay_type"].GetString();
  }
  if (sz_decay_type != "EXP_NOTIONAL" && sz_decay_type != "LOG_NOTIONAL" &&
      sz_decay_type != "SIZE" && sz_decay_type != "EXP_SIZE" &&
      sz_decay_type != "EXP_NOTIONAL_PERORD" && sz_decay_type != "EXP_SIZE_PERORD" &&
      sz_decay_type != "EXP_NOTIONAL_AND_SIZE") {
    throw std::runtime_error("Invalid place_thresh_liq_sz_decay_type");
  }

  double sz_decay = place_thresh_liq_sz_decay_;
  if (ordex_conf.HasMember("place_thresh_liq_sz_decay")) {
    sz_decay = ordex_conf["place_thresh_liq_sz_decay"].GetDouble();
  }
  if (sz_decay <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_sz_decay");
  }

  double sz_decay_2 = place_thresh_liq_sz_decay_2_;
  if (ordex_conf.HasMember("place_thresh_liq_sz_decay_2")) {
    sz_decay_2 = ordex_conf["place_thresh_liq_sz_decay_2"].GetDouble();
  }
  if (sz_decay_2 <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_sz_decay_2");
  }

  double min_sz_wt = place_thresh_liq_min_sz_wt_;
  if (ordex_conf.HasMember("place_thresh_liq_min_sz_wt")) {
    min_sz_wt = ordex_conf["place_thresh_liq_min_sz_wt"].GetDouble();
  }
  if (min_sz_wt <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_min_sz_wt");
  }

  liq_conf.AddMember("liq_type", "FIXED_LEVELS", alloc);
  liq_conf.AddMember("calc_style", "AVG_LIQ_PX", alloc);
  liq_conf.AddMember("return_price", true, alloc);
  constexpr int kPlaceThreshLiqDepthTicks = 200;
  liq_conf.AddMember("levels_deep", kPlaceThreshLiqDepthTicks, alloc);
  liq_conf.AddMember("sz_decay", sz_decay, alloc);
  liq_conf.AddMember("sz_decay_2", sz_decay_2, alloc);
  liq_conf.AddMember("min_sz_wt", min_sz_wt, alloc);
  liq_conf.AddMember("sz_decay_type", rapidjson::Value(sz_decay_type.c_str(), alloc), alloc);
  liq_conf.AddMember("per_level_decay", place_thresh_liq_per_level_decay_, alloc);
  liq_conf.AddMember("notional_to_liquidate", place_thresh_liq_notional_, alloc);
  liq_conf.AddMember("ref_signal", rapidjson::Value(ref_sig_->getName().c_str(), alloc), alloc);
  liq_conf.AddMember("use_flagged", place_thresh_liq_use_flagged_, alloc);
  liq_conf.AddMember("sigformer_tag", "internal_place_thresh_liq", alloc);

  rapidjson::Document empty_signals;
  empty_signals.SetArray();
  place_thresh_liq_sig_ =
      std::make_unique<pktrade::signals::SigLiqBalance>(-1, liq_conf, sf, empty_signals);

  LOG(INFO) << fmt::format(
      "({}) Using internal place-thresh liquidity source: mode {} notional {} coef {} "
      "combine {} auto_inside_mult {}",
      symbol_.get(), place_thresh_mode_, place_thresh_liq_notional_, place_thresh_liq_coef_,
      place_thresh_liq_combine_mode_ == 0 ? "add" : "max", place_thresh_liq_auto_inside_mult_);
}

double WideMM::calcPlaceThreshAutoLiqNotional() {
  const LevelBook* lvl_book = pktrade::GlobalVar::book_manager_->find(traded_bid_);
  if (lvl_book == nullptr) {
    return 0;
  }

  double sample_notional = 0;
  int n_sides = 0;
  const auto& buy_side = lvl_book->buySide();
  if (!buy_side.empty()) {
    double px = buy_side.begin()->px.toDouble();
    double qty = pktrade::effectiveQty(*buy_side.begin()).toDouble();
    double own_qty = protection::ownQtyAt(buy_orders_, px, min_tick_);
    double foreign_qty = std::max(0.0, qty - own_qty);
    sample_notional += px * foreign_qty;
    n_sides++;
  }

  const auto& sell_side = lvl_book->sellSide();
  if (!sell_side.empty()) {
    double px = sell_side.begin()->px.toDouble();
    double qty = pktrade::effectiveQty(*sell_side.begin()).toDouble();
    double own_qty = protection::ownQtyAt(sell_orders_, px, min_tick_);
    double foreign_qty = std::max(0.0, qty - own_qty);
    sample_notional += px * foreign_qty;
    n_sides++;
  }

  if (n_sides != 2) {
    return 0;
  }
  sample_notional /= n_sides;
  if (sample_notional <= 0 || !std::isfinite(sample_notional)) {
    return 0;
  }

  int64_t now_ms = now_fire_t_ > 0 ? now_fire_t_ : time_utils::nowToMs();
  if (place_thresh_liq_auto_last_t_ == 0 || place_thresh_liq_auto_notional_ems_ <= 0) {
    place_thresh_liq_auto_notional_ems_ = sample_notional;
  } else {
    int64_t dt = std::max<int64_t>(0, now_ms - place_thresh_liq_auto_last_t_);
    double alpha = 1.0 - std::exp(-dt / place_thresh_liq_auto_tdc_ms_);
    place_thresh_liq_auto_notional_ems_ =
        alpha * sample_notional + (1.0 - alpha) * place_thresh_liq_auto_notional_ems_;
  }
  place_thresh_liq_auto_last_t_ = now_ms;

  double notional = place_thresh_liq_auto_notional_ems_ * place_thresh_liq_auto_inside_mult_;
  notional = std::max(place_thresh_liq_auto_min_notional_, notional);
  notional = std::min(place_thresh_liq_auto_max_notional_, notional);
  if (notional <= 0 || !std::isfinite(notional)) {
    return 0;
  }
  return notional;
}

} // namespace pktrade::ordex
