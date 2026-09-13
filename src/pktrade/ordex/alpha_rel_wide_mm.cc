#include "alpha_rel_wide_mm.h"

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

AlphaRelWideMM::AlphaRelWideMM(SymbolId symbol, const rapidjson::Value& ordex_conf,
                               pktrade::signals::SignalFactory* sf,
                               pktrade::tempos::TempoFactory* tf)
    : Ordex(symbol, ordex_conf), last_t_place_(0), last_t_cxl_(0) {
  // Read stuff from the config.
  for (const rapidjson::Value& book : ordex_conf["markets"].GetArray()) {
    traded_books_.push_back(
        magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown));
  }

  if (traded_books_.size() > 1) {
    throw std::runtime_error(
        "AlphaRelWideMM only supports trading one market at a time. Update the "
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

  cancel_buffer_ = ordex_conf["cancel_buffer_frac"].GetDouble() * base_place_thresh_conf_;
  if (ordex_conf.HasMember("cancel_buffer")) {
    cancel_buffer_ = ordex_conf["cancel_buffer"].GetDouble();
  }

  want_cross_ = false;
  if (ordex_conf.HasMember("want_cross")) {
    want_cross_ = ordex_conf["want_cross"].GetBool();
  }

  want_add_ = true;
  if (ordex_conf.HasMember("want_add")) {
    want_add_ = ordex_conf["want_add"].GetBool();
  }

  place_thresh_mode_ = 0;
  if (ordex_conf.HasMember("place_thresh_mode")) {
    place_thresh_mode_ = ordex_conf["place_thresh_mode"].GetInt();
  }
  if (place_thresh_mode_ > 2) {
    throw std::runtime_error("Invalid place_thresh_mode");
  }
  if (ordex_conf.HasMember("place_thresh_spread_coef")) {
    place_thresh_spread_coef_ = ordex_conf["place_thresh_spread_coef"].GetDouble();
  }
  if (place_thresh_spread_coef_ < 0 || place_thresh_spread_coef_ > 10) {
    throw std::runtime_error("Invalid place_thresh_spread_coef");
  }

  std::cout << fmt::format(
                   "place_thresh_mode {} place_thresh_spread_coef {} ",
                   place_thresh_mode_, place_thresh_spread_coef_)
            << std::endl;

  adding_no_cutin_ = true;

  if (ordex_conf.HasMember("adding_no_cutin")) {
    adding_no_cutin_ = ordex_conf["adding_no_cutin"].GetBool();
  }

  // Cross_v2 params.
  if (want_cross_) {
    cross_v2_lspread_thresh_ = ordex_conf["cross_v2_lspread_thresh"].GetDouble();
    cross_v2_exit_adjust_ = ordex_conf["cross_v2_exit_adjust"].GetDouble();

    cross_v2_sub_thresh_ = ordex_conf["cross_v2_sub_thresh"].GetBool();

    cross_v2_resize_w_thresh_ = ordex_conf["cross_v2_resize_w_thresh"].GetBool();

    cross_v2_pred_mode_ = ordex_conf["cross_v2_pred_mode"].GetInt();
    cross_v2_lmr_dampener_ = ordex_conf["cross_v2_lmr_dampener"].GetDouble();
    cross_v2_rel_return_filter_ = ordex_conf["cross_v2_rel_return_filter"].GetInt();

    if (cross_v2_lmr_dampener_ > 2) {
      throw std::runtime_error("cross_v2_lmr_dampener_ is out of bounds");
    }
    cur_cross_thresh_ = cross_v2_lspread_thresh_;
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
    if (!mm_gtc_) {
      tif_ = TimeInForce::ALO;
    }
  }

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

  // Give us an option to get rid of the denom ord mult.
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

  return_ems_time_const_ = 1000.0;
  if (ordex_conf.HasMember("return_ems_time_const_s")) {
    return_ems_time_const_ = ordex_conf["return_ems_time_const_s"].GetDouble() * 1000;
  }

  getflat_pct_away_ = ordex_conf["getflat_pct_away"].GetDouble();
  if (getflat_pct_away_ > 0.05 || getflat_pct_away_ <= 0) {
    throw std::runtime_error("getflat_pct_away too high");
  }

  if (pcurvicity_ <= 0) {
    throw std::runtime_error(fmt::format("Invalid pcurvicity {}, must be positive\n", pcurvicity_));
  }

  if (ordex_conf.HasMember("spreadscale_v2")) {
    spreadscale_v2_ = ordex_conf["spreadscale_v2"].GetBool();

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
    spreadscale_v2_momentum_ = false;
    spreadscale_v2_shape_ = 0;
    spreadscale_v2_mult_ = 1;
    spreadscale_v2_denom_const_ = 1;
  }

  ms_min_ord_lifetime_ = 60;
  if (ordex_conf.HasMember("min_ord_lifetime")) {
    ms_min_ord_lifetime_ = int(ordex_conf["min_ord_lifetime"].GetDouble() * 1000);
  }

  ms_between_place_ = ordex_conf["ms_between_place"].GetInt64();
  ms_between_cxl_ = ordex_conf["ms_between_cxl"].GetInt64();
  ms_between_cross_ = ordex_conf["ms_between_cross"].GetInt64();
  ms_between_backlevel_ = ordex_conf["ms_between_backlevel"].GetInt64();

  last_t_place_ = 0;
  last_t_cxl_ = 0;
  last_t_cross_ = 0;
  last_t_backlevel_ = 0;

  print_ords_ = ordex_conf["print_ords"].GetBool();

  local_sig_ = sf->getByName(ordex_conf["local_sig"].GetString());
  remote_sig_ = sf->getByName(ordex_conf["remote_sig"].GetString());
  pred_sig_ = sf->getByName(ordex_conf["pred_sig"].GetString());

  needs_first_fire_ = true;
  num_fires_ = 0;
  approx_mid_ = 0;

  if (local_sig_ == nullptr) {
    throw std::runtime_error("local_sig_ not successfully created");
  }

  if (remote_sig_ == nullptr) {
    throw std::runtime_error("remote_sig_ not successfully created");
  }

  if (pred_sig_ == nullptr) {
    throw std::runtime_error("Pred_sig not successfully created");
  }

  local_sig_idx_ = local_sig_->getID();
  remote_sig_idx_ = remote_sig_->getID();

  local_sig_->addListener(this);
  remote_sig_->addListener(this);

  local_sig_val_ = 0;
  remote_sig_val_ = 0;

  local_minus_remote_ = 0;
  local_minus_remote_denom_ = 0;
  local_minus_remote_ema_ = 0;

  lmr_time_const_ = ordex_conf["lmr_time_const_s"].GetDouble() * 1000;
  last_lmr_update_t_ = 0;
  lmr_ema_mult_ = ordex_conf["lmr_ema_mult"].GetDouble();

  lspread_time_const_ = lmr_time_const_;
  if (ordex_conf.HasMember("lspread_time_const_s")) {
    lspread_time_const_ = ordex_conf["lspread_time_const_s"].GetDouble() * 1000;
  }

  // For doing mid_move_ems things.
  if (ordex_conf.HasMember("thresh_mid_sigmoid_scale")) {
    // New way of formatting this.
    thresh_mid_sigmoid_scale_ = ordex_conf["thresh_mid_sigmoid_scale"].GetBool();
    dynamic_mid_sigmoid_style_ = ordex_conf["dynamic_mid_sigmoid_style"].GetInt();
    dynamic_mid_sigmoid_base_ = ordex_conf["dynamic_mid_sigmoid_base"].GetDouble();
    dynamic_mid_sigmoid_offset_ = ordex_conf["dynamic_mid_sigmoid_offset"].GetDouble();
    dynamic_mid_sigmoid_mult_ = ordex_conf["dynamic_mid_sigmoid_mult"].GetDouble();
  }

  // I'm just going to include this anyway. It gets used too much, by too many
  // parts.
  if (ordex_conf.HasMember("remote_mid_ems_tdc_s")) {
    remote_mid_ems_time_const_ms_ = ordex_conf["remote_mid_ems_tdc_s"].GetDouble() * 1000;
  }

  net_remote_mid_move_ems_ = 0;

  abs_remote_mid_move_ems_ = 0;
  last_local_mid_t_ = 0;
  last_remote_mid_t_ = 0;

  local_reupdate_coef_ = 0;
  if (ordex_conf.HasMember("local_reupdate_coef")) {
    local_reupdate_coef_ = ordex_conf["local_reupdate_coef"].GetDouble();
    local_reupdate_tdc_ms_ = ordex_conf["local_reupdate_tdc"].GetDouble() * 1000;

    if (local_reupdate_coef_ > 1 || local_reupdate_coef_ < 0) {
      throw std::runtime_error("Invalid value of local_reupdate_coef");
    }
  }

  // Parameters for doing scaling.
  if (dynamic_mid_sigmoid_style_ < 0 || dynamic_mid_sigmoid_style_ > 2) {
    throw std::runtime_error("Unsupported dynamic mode " +
                             std::to_string(dynamic_mid_sigmoid_style_));
  }

  // For modifying thresh based on midmove ems (vs. spread)
  if (ordex_conf.HasMember("scale_thresh_midmove_spread")) {
    scale_thresh_midmove_spread_ = ordex_conf["scale_thresh_midmove_spread"].GetBool();
    scale_thresh_mids_const_ = ordex_conf["scale_thresh_mids_const"].GetDouble();

    scale_midmove_lower_bound_ = ordex_conf["scale_midmove_lower_bound"].GetDouble();
  } else {
    scale_thresh_midmove_spread_ = false;
    scale_thresh_mids_const_ = 20;
    scale_midmove_lower_bound_ = 1;
  }
  // Calculating remote spread ema.
  remote_spread_num_ = 0;
  remote_spread_denom_ = 0;
  remote_spread_ema_ = 0;

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

  last_exec_t_ = 0;
  last_exec_decay_t_ = 0;

  // Vol/liq mechanisms (1-4)
  vol_mechs_.parseConfig(symbol_.get(), ordex_conf);

  // Lspread sanitization (default on, can disable for A/B testing).
  if (ordex_conf.HasMember("sanitize_lspread")) {
    sanitize_lspread_ = ordex_conf["sanitize_lspread"].GetBool();
  }

  // Branch 1: Denom-gated warmup widening.
  if (ordex_conf.HasMember("warmup_widen_coef")) {
    warmup_widen_coef_ = ordex_conf["warmup_widen_coef"].GetDouble();
    warmup_widen_denom_target_ = ordex_conf["warmup_widen_denom_target"].GetDouble();
    LOG(INFO) << fmt::format("({}) warmup_widen enabled: coef={} denom_target={}",
                             symbol_.get(), warmup_widen_coef_, warmup_widen_denom_target_);
  }

  // LMR local-only mode: only update LMR on local signal changes.
  if (ordex_conf.HasMember("lmr_local_only")) {
    lmr_local_only_ = ordex_conf["lmr_local_only"].GetBool();
    LOG(INFO) << fmt::format("({}) lmr_local_only={}", symbol_.get(), lmr_local_only_);
  }

  // Branch 4b: Trade-dampened LMR update.
  if (ordex_conf.HasMember("lmr_trade_dampen_factor")) {
    lmr_trade_dampen_enabled_ = true;
    lmr_trade_dampen_factor_ = ordex_conf["lmr_trade_dampen_factor"].GetDouble();
    LOG(INFO) << fmt::format("({}) lmr_trade_dampen enabled: factor={}",
                             symbol_.get(), lmr_trade_dampen_factor_);
  }


  // Branch 6: Impulse-curvicity.
  if (ordex_conf.HasMember("curv_impulse_coef")) {
    curv_impulse_coef_ = ordex_conf["curv_impulse_coef"].GetDouble();
    if (ordex_conf.HasMember("curv_impulse_tdc"))
      curv_impulse_tdc_ms_ = ordex_conf["curv_impulse_tdc"].GetDouble() * 1000;
    LOG(INFO) << fmt::format("({}) curv_impulse enabled: coef={} tdc={}s",
                             symbol_.get(), curv_impulse_coef_, curv_impulse_tdc_ms_ / 1000);
  }

  // Pred momentum EMS.
  if (ordex_conf.HasMember("pred_momentum_coef")) {
    pred_momentum_coef_ = ordex_conf["pred_momentum_coef"].GetDouble();
    if (ordex_conf.HasMember("pred_momentum_tdc"))
      pred_momentum_tdc_ms_ = ordex_conf["pred_momentum_tdc"].GetDouble() * 1000;
    LOG(INFO) << fmt::format("({}) pred_momentum enabled: coef={} tdc={}s",
                             symbol_.get(), pred_momentum_coef_, pred_momentum_tdc_ms_ / 1000);
  }

  // Branch 7: Remote return EMA one-sided widening.
  if (ordex_conf.HasMember("remote_return_widen_coef")) {
    remote_return_widen_coef_ = ordex_conf["remote_return_widen_coef"].GetDouble();
    LOG(INFO) << fmt::format("({}) remote_return_widen enabled: coef={}",
                             symbol_.get(), remote_return_widen_coef_);
  }

  // Branch 8: LMR decay on non-trade local updates.
  if (ordex_conf.HasMember("lmr_local_book_decay_factor")) {
    lmr_local_book_decay_enabled_ = true;
    lmr_local_book_decay_factor_ = ordex_conf["lmr_local_book_decay_factor"].GetDouble();
    LOG(INFO) << fmt::format("({}) lmr_local_book_decay enabled: factor={}",
                             symbol_.get(), lmr_local_book_decay_factor_);
  }

  // Make the tradecaller.
  // Maybe at some point pull this out, elsewhere.
  tc_tempo_ = tf->getByName(ordex_conf["trade_caller"].GetString());
  tc_tempo_->addListener(this);

  refreshSizeMultBaseFromCurrent();
}

void AlphaRelWideMM::postSecMaster() {
  // Is this even set?
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

void AlphaRelWideMM::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Not sure yet but prob can subscribe to mdbeacon.
  // Does nothing right now.

  // Don't do this right now.
  // I had used this when I was gathering stats for the onSignal prints.
  // That's not needed anymore, so I removed this.

  std::vector<pktrade::md::BeaconCBType> cb_types;
  cb_types.push_back(pktrade::md::BeaconCBType::Final);
  if (local_size_cap_mult_ > 0 || min_foreign_inside_mult_ > 0) {
    cb_types.push_back(pktrade::md::BeaconCBType::LvlAdd);
    cb_types.push_back(pktrade::md::BeaconCBType::LvlMod);
    cb_types.push_back(pktrade::md::BeaconCBType::LvlDel);
  }
  if (vol_mechs_.needsTradeCallbacks()) {
    cb_types.push_back(pktrade::md::BeaconCBType::Trd);
  }

  if (cb_types.size() > 0) {
    BookId book_id{traded_books_[0], symbol_};

    beacon->addListener(book_id, this, cb_types);
  }
}

signals::Signal* AlphaRelWideMM::getPredSignal() { return pred_sig_; }

signals::Signal* AlphaRelWideMM::getMidSignal() { return local_sig_; }

Quantity AlphaRelWideMM::getEffectiveOrderSize(Side side) const {
  if (local_size_cap_mult_ <= 0) return order_size_;
  double ems = (side == Side::Buy) ? inside_size_buy_ems_ : inside_size_sell_ems_;
  if (ems <= 0) return order_size_;
  double capped = protection::capBySize(order_size_.toDouble(), local_size_cap_mult_, ems);
  if (capped <= 0) return Quantity{0};
  return pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, capped, false);
}

// tl5 mode.
// TODO: actually trade up to .20 bps away from the inside.
void AlphaRelWideMM::aggFlat() {
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

    // Liste the price.
    Price sell_px = bk_side.begin()->px;
    double top_sell_px = bk_side.begin()->px.toDouble();

    for (auto& lvl : bk_side) {
      if ((top_sell_px - lvl.px.toDouble()) / top_sell_px < getflat_pct_away_) {
        sell_px = lvl.px;
      } else {
        break;
      }
    }

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

    for (auto& lvl : bk_side) {
      if ((lvl.px.toDouble() - top_buy_px) / top_buy_px < getflat_pct_away_) {
        buy_px = lvl.px;
      } else {
        break;
      }
    }

    NewOrder ord{symbol_,          traded_books_[0], Side::Buy, buy_qty, buy_px,
                 OrderType::Limit, TimeInForce::IOC, false,     true};
    riskman_->sendOrd(ord);
  }
}

void AlphaRelWideMM::onTempo(int tempo_id) { tryFire(); }

void AlphaRelWideMM::tryFire() {
  num_fires_++;

  // Must be allowed to trade.
  if (!riskman_->canTrade()) {
    return;
  }

  now_fire_t_ = time_utils::nowToMs();

  const LevelBook* trade_bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  alpha_ = pred_sig_->getValue();

  // Compute effective LMR for FV, possibly clamped (Branch 5).
  double effective_lmr = local_minus_remote_ema_ * lmr_ema_mult_;

  if (pred_mode_ == 0) {
    double remote_pred_price = remote_sig_val_ * (1 + alpha_mult_ * alpha_);
    // Shift from the remote price.
    shifted_local_mid_ = effective_lmr + remote_sig_val_;
    // predict by shifting from the predicted remote price.
    pred_px_ = effective_lmr + remote_pred_price;
  } else if (pred_mode_ == 1) {
    // Shift from the remote price.
    shifted_local_mid_ = effective_lmr + remote_sig_val_;
    // predict on the shifted local mid.
    pred_px_ = (1 + alpha_mult_ * alpha_) * (shifted_local_mid_);
  }

  /*
    std::cout << fmt::format("Fire {} alpha {} lmr {} remote {} shifted_local_mid {} pred_px {}
    threshes {} {}", time_utils::nowToStr(), alpha_, local_minus_remote_ema_, remote_sig_val_,
      shifted_local_mid_,
      pred_px_,
      place_thresh_buy_, place_thresh_sell_
    ) << std::endl;
  */

  // note, this age stuff might be better if the local stuff didn't use
  // flagging callbacks.
  // Note, this is kind of experimental.
  if (local_reupdate_coef_ != 0) {
    // Yank it back towards the local mid
    double local_age = (time_utils::nowToMs() - last_local_mid_t_);
    double local_weight = local_reupdate_coef_ * (std::exp(-local_age / local_reupdate_tdc_ms_));

    // Maybe do some sort of clamping too.

    // But ultimately do a weighted blend of the two.
    pred_px_ = pred_px_ * (1 - local_weight) + local_sig_val_ * local_weight;
  }

  if (needs_first_fire_ && shifted_local_mid_ != 0) {
    onFirstFire();
    return;
  }

  // Recheck min_tick in case price crossed a scale boundary (e.g. 100).
  // calc_tick_size is just a few FPU ops (pow, log10, ceil), negligible cost.
  // Use local_sig_val_ (the actual market mid) so that both buy and sell sides
  // get the correct tick for where the market is trading.
  double new_min_tick = pktrade::GlobalVar::secmaster_->calc_tick_size(local_sig_val_).toDouble();
  if (!pktrade::approx_equal(min_tick_, new_min_tick)) {
    LOG(WARNING) << fmt::format("({}) min_tick changed from {} to {} (local_sig_val {})",
                                symbol_.get(), min_tick_, new_min_tick, local_sig_val_);
    min_tick_ = new_min_tick;
    rung_spacing_px_ = std::max(rung_spacing_px_, min_tick_);
  }

  // Don't trade without a remote signal — local_reupdate_coef can accidentally
  // keep pred_px > 0 even when remote is missing, causing bad placements.
  if (remote_sig_val_ == 0) {
    LOG_EVERY_N(ERROR, 211) << fmt::format(
        "({}) remote_sig_val is 0, not trading (local {} pred_px {})",
        symbol_.get(), local_sig_val_, pred_px_);
    return;
  }

  // Don't trade... if there's no mid.
  if (local_sig_val_ == 0) {
    return;
  }
  updatePredMomentumEms();
  updateThreshes();

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

  // Limit ourselves by the absolute maxposition.
  ordex_allowed_buy = std::min(ordex_allowed_buy, abs_allowed_buy);
  ordex_allowed_sell = std::min(ordex_allowed_sell, abs_allowed_sell);

  // OK, I think we should not allow placement on sides where we don't know where the price should
  // resolve yet?
  if (local_sig_->getBASize() == 0) {
    // std::cout << " DISALLOWED SELL BC of IMPROPER BA SIZE " << std::endl;
    ordex_allowed_sell = 0;
  }
  if (local_sig_->getBBSize() == 0) {
    // std::cout << " DISALLOWED BUY BC of IMPROPER BB SIZE " << std::endl;
    ordex_allowed_buy = 0;
  }

  LOG_EVERY_N(INFO, 103) << fmt::format(
      "{} ({}) tryFire: local {:.4f} remote {:.4f} lmr_ema {:.2f} lmr {:.2f} pred_px {:.4f} alpha {:.5f} "
      "place_thresh_buy {} place_thresh_sell {} pos {}",
      time_utils::nowToStr(), 
      symbol_.get(), local_sig_val_, remote_sig_val_, local_minus_remote_ema_,
      local_sig_val_ - remote_sig_val_, 
      pred_px_, alpha_, place_thresh_buy_, place_thresh_sell_,
      riskman_->getPos().toDouble());
  vol_mechs_.logState();
  if (pred_momentum_coef_ > 0) {
    LOG_EVERY_N(INFO, 307) << fmt::format("({}) pred_momentum_ems={:.6f} ({:.2f} bps)",
                                          symbol_.get(), pred_momentum_ems_,
                                          pred_momentum_ems_ * 10000);
  }

  /*
  pktrade::util::log_line(
      fmt::format("{} ({})  alpha {} mid_px {} lmr_ema {} pred_px {} "
                  "abs_allowed ({} {}) ordex_allowed ({} {})",
                  time_utils::nowToStr().c_str(), symbol_.get(), alpha,
                  mid_price, local_minus_remote_ema_, pred_price,

                  abs_allowed_buy, abs_allowed_sell, ordex_allowed_buy,
                  ordex_allowed_sell), false);
  */

  int64_t now_t = time_utils::nowToMs();

  if (print_ords_ && num_fires_ % 100 == 0) {
    // std::cout << " num_buy " << buy_orders_.size() << " num_sell "
    //           << sell_orders_.size() << std::endl;

    printOutstandingOrders();
  }

  // If in no-new-orders mode, maybeCancel is all we can do.

  if (!TL7_no_new_ords_) {
    // Consider firing.
    if (canCross()) {
      // v2: compare the predicted price with the bb/ba.
      maybeCrossV2(trade_bk, cur_pos, ordex_allowed_buy, ordex_allowed_sell);
    }
    if (TL6_cross_exit_) {
      // If in cross exit mode, don't place, and we've already canceled all outstanding orders.
      return;
    }

    if (want_add_) {
      if (now_t - last_t_place_ > ms_between_place_ && riskman_->canPlaceMoreOrders()) {
        maybeJoin(trade_bk, cur_pos, ordex_allowed_buy, ordex_allowed_sell);
      }
    }
  }

  // Cancel should be the highest priority, but moving it up introduced diffs so reverting for now.
  if (want_add_) {
    if (now_t - last_t_cxl_ > ms_between_cxl_) {
      maybeCancel(trade_bk, cur_pos, ordex_allowed_buy, ordex_allowed_sell);
    }
  }

  if (TL7_no_new_ords_) {
    return;
  }

  if (want_add_) {
    // Maybe add or rm backlevels too.
    // Always reserve space for the non-backlevels.
    if (now_fire_t_ - last_t_backlevel_ > ms_between_backlevel_) {
      manageBacklevels(trade_bk, ordex_allowed_buy - order_size_.toDouble(),
                       ordex_allowed_sell - order_size_.toDouble());
    }

    if (print_ords_ && num_fires_ % 2 == 0) {
      // Sort our orders every so often.
      sort(buy_orders_.begin(), buy_orders_.end(), AlphaRelWideMM::compareBuyOrds);
      sort(sell_orders_.begin(), sell_orders_.end(), AlphaRelWideMM::compareSellOrds);
    }
  }
}

void AlphaRelWideMM::trackReturn(double enter_px, bool is_buy, std::string prefix_str) {
  // TODO: check the current val and lmk if there's an expected return

  double diff = local_sig_val_ - enter_px;
  if (!is_buy) {
    diff = -diff;
  }
  double pct_ret = diff / enter_px;
  std::string goodness = pct_ret > 0 ? "GOOD" : "BAD";

  std::string line = fmt::format("{} : ret {:.4f} {}\n", prefix_str, pct_ret, goodness);
  std::cout << line;
  /*
  printf("%s **TRACKRETURN**: oid %d side %s enter_px %f exit_mid %f ~ret %f\n",
         time_utils::nowToStr().c_str(), oid, (is_buy ? "BUY" : "SELL"),
         enter_px, local_sig_val_, return);
         */
}

void AlphaRelWideMM::updateThreshes() {
  // otay
  if (TL6_cross_exit_) {
    // In TL6, we use 0 cross thresh. No need to set place threshes since we only cross.
    cur_cross_thresh_ = 0;
    return;
  }
  cur_cross_thresh_ = cross_v2_lspread_thresh_;

  // Otherwise, reset cur_cross_thresh. All other TLs use standard cross_thresh.
  place_thresh_px_ = mid_thresh_ * local_sig_val_;
  cancel_buffer_px_ = cancel_buffer_ * local_sig_val_;

  if (TL2_aggr_exit_) {
    // In TL2, we use TL2_aggr_exit_thresh and skip all thresh adjustments.
    place_thresh_buy_ = place_thresh_sell_ = TL2_aggr_exit_thresh_ * pred_px_;
    return;
  }

  double modifier = 1;

  // Modify threshes (some sort of dynamic adjustment)
  if (thresh_mid_sigmoid_scale_) {
    // Another way of doing dynamic threshing.
    // 0: sigmoid.  offset + mult * (1 / (1+exp(-abs(ems)/base)))
    // 1: linear. offset + mult * (abs(ems)/base)
    // 2: log. mult * log(1 + abs(ems)/base) + offset

    double remote_ems = std::abs(abs_remote_mid_move_ems_);

    if (dynamic_mid_sigmoid_style_ == 0) {
      modifier =
          dynamic_mid_sigmoid_offset_ +
          dynamic_mid_sigmoid_mult_ * (1 / (1 + std::exp(-remote_ems / dynamic_mid_sigmoid_base_)));
    } else if (dynamic_mid_sigmoid_style_ == 1) {
      modifier = dynamic_mid_sigmoid_offset_ +
                 dynamic_mid_sigmoid_mult_ * (remote_ems / dynamic_mid_sigmoid_base_);
    } else if (dynamic_mid_sigmoid_style_ == 2) {
      modifier = dynamic_mid_sigmoid_offset_ +
                 dynamic_mid_sigmoid_mult_ * std::log(1 + remote_ems / dynamic_mid_sigmoid_base_);
    }

    // OK, now we can multiply and get stuffs
    place_thresh_px_ *= modifier;
    cancel_buffer_px_ *= modifier;

  } else if (scale_thresh_midmove_spread_) {
    double spread_pct = lspread_ema_ / local_sig_val_;

    double nu_mid_ratio = abs_remote_mid_move_ems_ / spread_pct;
    if (spread_pct == 0) {
      // Don't allow for weird values.
      nu_mid_ratio = 0;
    }
    // If scale_midmove_lower_bound_ == 0.3, then we do
    // 0.3 + 0.7 * decayed_exp().
    // And in particular, if the midmove is higher, then we want
    // the effective multiplier to be higher. If the midmove is lower,
    // then we want the threshold to be lower.
    // So basically just want to account for vol spikes.
    modifier =
        scale_midmove_lower_bound_ + (1 - scale_midmove_lower_bound_) *
                                         // If the inside ratio is higher, then the exp should be
                                         // closer to 0. So then 1 - that is larger.
                                         // If inside ratio is lower, then the exp should be
                                         // closer to 1, so then the whole term is higher.
                                         (1 - std::exp(-nu_mid_ratio / scale_thresh_mids_const_));

    /*
    printf(
        "Hello: abs %f spread_ema %.8f ( %f %f )nu_ratio %f inside_ratio %f "
        "modifier %f so it is %f | final %f \n",
        abs_mid_move_ems_, spread_pct, local_spread_num_, local_spread_denom_,

        nu_mid_ratio, nu_mid_ratio / scale_thresh_mids_const_, modifier,
        place_thresh_px * modifier,
        std::max(place_thresh_px * modifier, spread_pct));
   */
    place_thresh_px_ *= modifier;
    cancel_buffer_px_ *= modifier;

    // Bound it below by the spread. Can't get too small now.
    place_thresh_px_ = std::max(place_thresh_px_, lspread_ema_);
    cancel_buffer_px_ = std::max(cancel_buffer_px_, lspread_ema_);

  } else if (spreadscale_v2_) {

    double spread_pct = lspread_ema_ / local_sig_val_;

    // Safe default.
    if (local_sig_val_ == 0 || spread_pct < 1e-12) {
      spread_pct = 0.0001;
    }

    double activity_ratio = abs_remote_mid_move_ems_ / spread_pct / spreadscale_v2_denom_const_;

    // Make sure this is strictly nonnegative.
    double mod_inside = std::max(activity_ratio - 1.0, 0.0);

    if (spreadscale_v2_momentum_) {
      // OK, then, incorporate momentum.
      double momentum = abs(net_remote_mid_move_ems_) / (abs_remote_mid_move_ems_ + 1e-8);

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
      modifier = 1 + spreadscale_v2_mult_ * (1 - std::exp(-mod_inside));
    }

    /*
        std::cout << fmt::format("ssv2: {} remote_sp_ema {} mid {} spread_pct {}
       remote_mm_ems {} activity_ratio {} momentum {} mod_inside {} modifier
       {}", symbol_.get(), remote_spread_ema_, mid_price, spread_pct,
                                 abs_remote_mid_move_ems_, activity_ratio,
                                 abs(net_remote_mid_move_ems_) /
       (abs_remote_mid_move_ems_ + 1e-8), mod_inside, modifier) << std::endl;
                                 */

    place_thresh_px_ *= modifier;
    cancel_buffer_px_ *= modifier;

    // Bound it below by the spread. Can't get too small now.
    place_thresh_px_ = std::max(place_thresh_px_, lspread_ema_);
    cancel_buffer_px_ = std::max(cancel_buffer_px_, lspread_ema_);
  }

  // Adjust for modes and bias multiples.
  // If we do start incorporating liquidity, then maybe move this
  // down below to modify sided threshes directly.
  if (place_thresh_mode_ == 1) {
    place_thresh_px_ += lspread_ * place_thresh_spread_coef_;
  } else if (place_thresh_mode_ == 2) {
    place_thresh_px_ += lspread_ema_ * place_thresh_spread_coef_;
  }

  // Start moving to sided things.
  place_thresh_buy_ = place_thresh_px_;
  place_thresh_sell_ = place_thresh_px_;

  // Curvicity adjustments.
  double cur_pos = riskman_->getPos().toDouble();

  double bias_buy = 0;
  double bias_sell = 0;
  if (use_ladder_ticks_) {

    bias_buy =
        std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * place_thresh_px_;
    bias_sell =
        std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * place_thresh_px_;

  } else {
    // traditional Curvicity

    double curv_frac = std::min(1.0, std::abs(cur_pos / pcurv_frac_denom_));

    // double curv_offset = pcurvicity_coef_ * std::pow(curv_frac, pcurvicity_) * place_thresh_px_;

    // Make this sided.
    bias_buy = pcurvicity_coef_ * std::pow(curv_frac, pcurvicity_) * place_thresh_px_;
    bias_sell = pcurvicity_coef_ * std::pow(curv_frac, pcurvicity_) * place_thresh_px_;
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

  // Vol/liq mechanisms (1-5). lspread_ema_ is passed through so Mechanism 5
  // can use spread_normalized reference mode; ignored by other modes.
  {
    auto [bw, sw] = vol_mechs_.getVolWidenPx(pred_px_, place_thresh_buy_,
                                             place_thresh_sell_, lspread_ema_);
    place_thresh_buy_ += bw;
    place_thresh_sell_ += sw;
    auto [ldb, lds] = vol_mechs_.getLiqDepthWidenPx(pred_px_);
    place_thresh_buy_ += ldb;
    place_thresh_sell_ += lds;
  }

  // Branch 1: Denom-gated warmup widening.
  if (warmup_widen_coef_ > 0 && warmup_widen_denom_target_ > 0) {
    double convergence = std::min(1.0, local_minus_remote_denom_ / warmup_widen_denom_target_);
    double warmup_extra = warmup_widen_coef_ * (1.0 - convergence) * pred_px_;
    place_thresh_buy_ += warmup_extra;
    place_thresh_sell_ += warmup_extra;
  }

  // Branch 6: Impulse-curvicity — one-sided widening from recent fills.
  if (curv_impulse_coef_ > 0 && curv_impulse_ != 0) {
    double impulse_offset = std::abs(curv_impulse_) * curv_impulse_coef_ * place_thresh_px_;
    if (curv_impulse_ > 0) {
      // Bought recently → harder to buy.
      place_thresh_buy_ += impulse_offset;
    } else {
      // Sold recently → harder to sell.
      place_thresh_sell_ += impulse_offset;
    }
  }

  // Branch 7: Remote return EMA one-sided widening.
  // If remote is trending up, widen sell (don't sell into uptrend).
  // If remote is trending down, widen buy (don't buy into downtrend).
  if (remote_return_widen_coef_ > 0 && remote_return_ems_ != 0) {
    double remote_widen = std::abs(remote_return_ems_) * remote_return_widen_coef_ * pred_px_;
    if (remote_return_ems_ > 0) {
      place_thresh_sell_ += remote_widen;
    } else {
      place_thresh_buy_ += remote_widen;
    }
  }

  // Pred momentum EMS one-sided widening.
  // If momentum is positive (price rising), widen sell side to avoid getting picked off.
  if (pred_momentum_coef_ > 0 && pred_momentum_ems_ != 0) {
    if (pred_momentum_ems_ > 0) {
      place_thresh_sell_ += pred_momentum_ems_ * pred_momentum_coef_ * pred_px_;
    } else {
      place_thresh_buy_ += std::abs(pred_momentum_ems_) * pred_momentum_coef_ * pred_px_;
    }
  }
}

void AlphaRelWideMM::updatePredMomentumEms() {
  if (pred_momentum_coef_ == 0) return;
  if (pred_px_ <= 0 || !std::isfinite(pred_px_)) return;

  // Initialize on first valid price.
  if (prev_pred_px_for_momentum_ == 0) {
    prev_pred_px_for_momentum_ = pred_px_;
    last_pred_momentum_update_t_ = now_fire_t_;
    return;
  }

  // Only update every 500ms.
  int64_t dt = now_fire_t_ - last_pred_momentum_update_t_;
  if (dt < 500) return;

  double price_return = (pred_px_ - prev_pred_px_for_momentum_) / prev_pred_px_for_momentum_;
  if (!std::isfinite(price_return)) return;
  // Clamp extreme returns.
  price_return = std::max(std::min(price_return, 0.01), -0.01);

  if (dt > 0) {
    double decay = std::exp(-dt / pred_momentum_tdc_ms_);
    pred_momentum_ems_ = price_return + decay * pred_momentum_ems_;
  }

  prev_pred_px_for_momentum_ = pred_px_;
  last_pred_momentum_update_t_ = now_fire_t_;
}

bool AlphaRelWideMM ::canCross() {
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

void AlphaRelWideMM::maybeCrossV2(const LevelBook* lvl_book, double cur_pos,
                                  double ordex_allowed_buy, double ordex_allowed_sell) {
  // First, check the predicted price.

  // DOn't do it too early.
  if (lspread_ema_ == 0)
    return;

  double pred_price = remote_sig_val_ + cross_v2_lmr_dampener_ * local_minus_remote_ema_;

  if (cross_v2_pred_mode_ == 1) {
    pred_price = remote_sig_val_ * (1 + alpha_mult_ * pred_sig_->getValue()) +
                 cross_v2_lmr_dampener_ * local_minus_remote_ema_;
  }

  // check if it crosses.
  double lba = local_sig_->getBAMeasure();
  double lbb = local_sig_->getBBMeasure();

  // effective thresh, after modifications (like exit adjust).
  double eff_thresh = cur_cross_thresh_;
  if (cur_pos > 0 && pred_price > lba) {
    eff_thresh *= cross_v2_exit_adjust_;
  }
  if (cur_pos < 0 && pred_price < lbb) {
    eff_thresh *= cross_v2_exit_adjust_;
  }

  if (pred_price > lba) {
    // Apply some filters.

    if (cross_v2_rel_return_filter_ == 1) {
      if (remote_return_ems_ < 0) {
        return;
      }
    } else if (cross_v2_rel_return_filter_ == 2) {
      if (remote_return_ems_ < local_return_ems_) {
        return;
      }
    } else if (cross_v2_rel_return_filter_ == 3) {
      // Just asking that we have some clearance.
      if (remote_return_ems_ - local_return_ems_ < local_return_ems_) {
        return;
      }
    }

    // Potential BUY.
    double edge_scale_lspread = (pred_price - lba) / lspread_ema_;

    if (edge_scale_lspread > eff_thresh) {
      /*

          std::string prefix_str = fmt::format(
              "{}: CROSSBUY (local {} remote {} lmr "
              "{:.4f} lmr_ema {:.4f} local_ems {:.5f} remote_ems {:.5f} "
              "lspread_ema {:.4f} alpha "
              "{:.5f})",
              time_utils::nowToStr().c_str(), local_sig_val_, remote_sig_val_,
              local_sig_val_ - remote_sig_val_, local_minus_remote_ema_,
              local_return_ems_, remote_return_ems_, lspread_ema_,
              alpha_mult_ * pred_sig_->getValue());

          pktrade::GlobalVar::event_loop_->onTimeout(
              std::chrono::seconds(120),
              [this, enter_px = lba, prefix_str = prefix_str] {
                this->trackReturn(enter_px, true, prefix_str);
              });
    */
      int ordsize_mult = 1;
      // If we have a large prediction, consider doing something MPO-like.
      if (cross_v2_resize_w_thresh_) {
        ordsize_mult = (int)edge_scale_lspread / eff_thresh;
      }

      double this_cross_sz =
          std::min(ordsize_mult * cross_order_size_.toDouble(), ordex_allowed_buy);

      if (this_cross_sz > 0) {
        // Don't sub thresh.
        // Maybe create an option to actually sub the thresh.
        double this_cross_px = pred_price;
        if (cross_v2_sub_thresh_) {
          this_cross_px = pred_price - eff_thresh * lspread_ema_;
        }

        if (this_cross_px > 0 && std::isfinite(this_cross_px)) {
          Price this_ord_px = Price(std::to_string(roundToSide(this_cross_px, false)));

          // Make sure order size meets min notional.
          double this_ord_px_d = this_ord_px.toDouble();
          if (this_cross_sz * this_ord_px_d < 11) { // real min notional is 10, but adding buffer
            LOG(WARNING) << fmt::format(
                "({}) Buy order size below min notional of 10 "
                "(px {} sz {} base_sz {} allowed_buy {}). Sending min size instead.",
                symbol_.get(), this_ord_px_d, this_cross_sz, cross_order_size_.toDouble(),
                ordex_allowed_buy);
            this_cross_sz = 11.0 / this_ord_px_d;
          }

          Quantity this_ord_qty =
              pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, this_cross_sz, true);

          if (this_ord_qty > Quantity{0}) {
            NewOrder ord{symbol_,          traded_books_[0], Side::Buy, this_ord_qty, this_ord_px,
              OrderType::Limit, TimeInForce::IOC, false,     false};
            PKOrderId pkord_id = riskman_->sendOrd(ord);
            if (pkord_id != -1) {
              last_t_cross_ = time_utils::nowToMs();
            }
          }
        }
      }
    }

  } else if (pred_price < lbb) {
    // Step 1.

    if (cross_v2_rel_return_filter_ == 1) {
      if (remote_return_ems_ > 0) {
        return;
      }
    } else if (cross_v2_rel_return_filter_ == 2) {
      if (remote_return_ems_ > local_return_ems_) {
        return;
      }
    } else if (cross_v2_rel_return_filter_ == 3) {
      // -3 vs -1 == can go
      // -3 vs -2 == cannot go
      // -3 - (-2) == -1 (is less than 2x abs())

      // Let me do the positive side first.

      // Just asking that we have some clearance.
      if (remote_return_ems_ - local_return_ems_ > local_return_ems_) {
        return;
      }
    }

    double edge_scale_lspread = (lbb - pred_price) / lspread_ema_;

    // Potential SELL.
    if (edge_scale_lspread > eff_thresh) {
      /*
      std::string prefix_str = fmt::format(
          "{}: CROSSSELL (local {} remote {} lmr "
          "{:.4f} lmr_ema {:.4f} local_ems {:.5f} remote_ems {:.5f} "
          "lspread_ema {:.4f} alpha "
          "{:.5f})",
          time_utils::nowToStr().c_str(), local_sig_val_, remote_sig_val_,
          local_sig_val_ - remote_sig_val_, local_minus_remote_ema_,
          local_return_ems_, remote_return_ems_, lspread_ema_,
          alpha_mult_ * pred_sig_->getValue());

      pktrade::GlobalVar::event_loop_->onTimeout(
          std::chrono::seconds(120),
          [this, enter_px = lbb, prefix_str = prefix_str] {
            this->trackReturn(enter_px, false, prefix_str);
          });
      */
      int ordsize_mult = 1;
      if (cross_v2_resize_w_thresh_) {
        ordsize_mult = (int)edge_scale_lspread / eff_thresh;
      }

      double this_cross_sz =
          std::min(ordsize_mult * cross_order_size_.toDouble(), ordex_allowed_sell);

      if (this_cross_sz > 0) {
        // Don't sub thresh.
        // Maybe create an option to actually sub the thresh.
        double this_cross_px = pred_price;
        if (cross_v2_sub_thresh_) {
          this_cross_px = pred_price + eff_thresh * lspread_ema_;
        }

        if (this_cross_px > 0 && std::isfinite(this_cross_px)) {
          Price this_ord_px = Price(std::to_string(roundToSide(this_cross_px, true)));

          // Make sure order size meets min notional.
          double this_ord_px_d = this_ord_px.toDouble();
          if (this_cross_sz * this_ord_px_d < 11) { // real min notional is 10, but adding buffer
            LOG(WARNING) << fmt::format(
                "({}) Sell order size below min notional of 10 "
                "(px {} sz {} base_sz {} allowed_sell {}). Sending min size instead.",
                symbol_.get(), this_ord_px_d, this_cross_sz, cross_order_size_.toDouble(),
                ordex_allowed_sell);
            this_cross_sz = 11.0 / this_ord_px_d;
          }

          Quantity this_ord_qty =
              pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, this_cross_sz, true);

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
    }
  }
};

void AlphaRelWideMM::maybeJoin(const LevelBook* lvl_book, double cur_pos, double ordex_allowed_buy,
                               double ordex_allowed_sell) {

  double top_bid = local_sig_->getBBMeasure();
  double top_ask = local_sig_->getBAMeasure();

  Quantity eff_buy_sz = getEffectiveOrderSize(Side::Buy);
  Quantity eff_sell_sz = getEffectiveOrderSize(Side::Sell);
  double eff_buy_sz_dbl = eff_buy_sz.toDouble();
  double eff_sell_sz_dbl = eff_sell_sz.toDouble();
  if (eff_buy_sz_dbl <= 0 && eff_sell_sz_dbl <= 0) return;

  // Buy?
  if (eff_buy_sz_dbl > 0 && ordex_allowed_buy > eff_buy_sz_dbl
      // Restrict the outstanding backlevels.
      && buy_orders_.size() <= max_back_levels_) {
    // Consider the best buy price we'd do.
    double best_buy_px = pred_px_ - place_thresh_buy_;

    // Don't lock or cross the BA
    best_buy_px = std::min(best_buy_px, top_ask - min_tick_);

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

    bool too_crowded = false;
    for (auto buy_order : buy_orders_) {
      if (buy_order.cancel_time != 0) {
        // Already cancelled.
        continue;
      }

      if (std::abs(buy_order.px.toDouble() - best_buy_px) <
          front_rung_spacing_mult_ * rung_spacing_px_) {
        // Too crowded.
        too_crowded = true;
      } else if (buy_order.px.toDouble() > best_buy_px) {
        // If there's already a more aggressive buy, don't join.
        too_crowded = true;
      } else {
      }
    }

    if (!too_crowded && best_buy_px > 0 && std::isfinite(best_buy_px)) {
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
    double best_sell_px = pred_px_ + place_thresh_sell_;

    // Don't lock or cross the BB
    best_sell_px = std::max(best_sell_px, top_bid + min_tick_);

    if (best_sell_px < top_ask && adding_no_cutin_) {
      // Undo this purposefully.
      best_sell_px = top_ask;
    }

    // Tick rounding needs to happen after all changes to best_sell_px have been made.
    best_sell_px = roundToSide(best_sell_px, true);

    bool too_crowded = false;
    for (auto sell_order : sell_orders_) {
      if (sell_order.cancel_time != 0) {
        // Already cancelled.
        continue;
      }

      if (std::abs(sell_order.px.toDouble() - best_sell_px) <
          front_rung_spacing_mult_ * rung_spacing_px_) {
        // Too crowded.
        too_crowded = true;
      } else if (sell_order.px.toDouble() < best_sell_px) {
        // If there's already a more aggressive buy, don't join.
        too_crowded = true;

      } else {
      }
    }

    if (!too_crowded && best_sell_px > 0 && std::isfinite(best_sell_px)) {
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
}

void AlphaRelWideMM::maybeCancel(const LevelBook* lvl_book, double cur_pos,
                                 double ordex_allowed_buy, double ordex_allowed_sell) {

  // Check the book side.
  bool should_cancel = false;

  double buy_total_thresh = place_thresh_buy_ - cancel_buffer_px_;
  double sell_total_thresh = place_thresh_sell_ - cancel_buffer_px_;

  // Cancels due to predpx.
  // Start from the inside.
  // if (buy_orders_.size() > 0) {
  // auto topbuy_it = buy_orders_.begin();

  int n_buy_iters = 0;
  for (auto& topbuy_it : buy_orders_) {
    if (canCancelOrd(topbuy_it)) {
      double top_buy_px = topbuy_it.px.toDouble();

      should_cancel = false;

      if (pred_px_ - top_buy_px < buy_total_thresh) {
        should_cancel = true;
      }

      if (ordex_allowed_buy < 0 || cur_pos >= max_pos_.toDouble()) {
        // printf("ordex_allowed_buy caused: %f \n", ordex_allowed_buy);
        should_cancel = true;
      } else if (n_buy_iters == 0 && will_cancel_isolated_ &&
                 pktrade::approx_equal(top_buy_px, local_sig_->getBBMeasure()) &&
                 topbuy_it.init_qty.toDouble() >= local_sig_->getBBSize() - pktrade::EPS) {
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
           top_buy_px, buy_total_thresh - cancel_buffer_px, ordex_allowed_buy);
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
    //  if (sell_orders_.size() > 0) {
    //    auto topsell_it = sell_orders_.begin();
    if (canCancelOrd(topsell_it)) {
      double top_sell_px = topsell_it.px.toDouble();

      should_cancel = false;
      // Check thresh.

      if (top_sell_px - pred_px_ < sell_total_thresh) {
        should_cancel = true;
      }
      if (ordex_allowed_sell < 0 || cur_pos <= -max_pos_.toDouble()) {
        should_cancel = true;
        // std::cout << " Cancelling sell because of maxpos reasons" <<
        // std::endl; printf("ordex_allowed_sell caused: %f \n",
        // ordex_allowed_sell);
      } else if (n_sell_iters == 0 && will_cancel_isolated_ &&
                 pktrade::approx_equal(top_sell_px, local_sig_->getBAMeasure()) &&
                 topsell_it.init_qty.toDouble() >= local_sig_->getBASize() - pktrade::EPS) {
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
           %f\n", time_utils::nowToStr().c_str(), sell_orders_.begin()->pk_oid,
                top_sell_px, pred_px, top_sell_px - pred_px,
                sell_total_thresh - cancel_buffer_px, ordex_allowed_sell);
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

void AlphaRelWideMM::manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy,
                                      double abs_allowed_sell) {

  // Cancel backlevels due to number of backlevels or remaining allowed size.
  if (buy_orders_.size() > 1 &&
      (buy_orders_.size() >= max_back_levels_ || abs_allowed_buy < order_size_.toDouble())) {
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
          already_cxl_shrs + abs_allowed_buy > order_size_.toDouble()) {
        break;
      }

      if (it->cancel_time != 0) {
        already_cxl_shrs += order_size_.toDouble();
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
      // If false, just let front levels become back levels, don't place back levels explicitly
      place_back_levels_ &&
      buy_orders_.size() > 0 && buy_orders_.size() < max_back_levels_ - 2 &&
             abs_allowed_buy > order_size_.toDouble()) {

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

    // Sparsity check: see wide_mm.cc::manageBacklevels for description.
    if (min_foreign_inside_mult_ > 0 && inside_size_buy_ems_ > 0) {
      double sparse_thresh = min_foreign_inside_mult_ * inside_size_buy_ems_;
      double sparse_px = protection::sparseBuyBacklevelPx(
          lvl_book, prev_buy_px, new_backlevel_px, sparse_thresh,
          buy_orders_, min_tick_);
      if (!std::isfinite(sparse_px)) {
        new_backlevel_px = std::numeric_limits<double>::quiet_NaN();
      } else {
        new_backlevel_px = roundToSide(sparse_px, false);
      }
    }

    // Place backlevel size.
    // Then, we can place the backlevel.
    // Place the backlevel.
    if (new_backlevel_px > 0 && std::isfinite(new_backlevel_px)) {
      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Buy,
                   order_size_,
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
                                        Price{std::to_string(new_backlevel_px)}, order_size_,
                                        SimpleOrderState::LIVE, now_fire_t_, 0});
      }
    }
    // Just place one
  }

  // Sell.
  if (sell_orders_.size() > 1 &&
      (sell_orders_.size() >= max_back_levels_ || abs_allowed_sell < order_size_.toDouble())) {
    double already_cxl_shrs = 0;
    int already_cxl_orders_ = 0;

    for (auto it = sell_orders_.rbegin(); it != sell_orders_.rend(); it++) {
      // If it's just one size. Then it might not be a backlevel.
      if (it->pk_oid == sell_orders_.begin()->pk_oid) {
        break;
      }

      if (sell_orders_.size() - already_cxl_orders_ < max_back_levels_ &&
          already_cxl_shrs + abs_allowed_sell > order_size_.toDouble()) {
        break;
      }

      if (it->cancel_time != 0) {
        already_cxl_shrs += order_size_.toDouble();
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
      // If false, just let front levels become back levels, don't place back levels explicitly
      place_back_levels_ &&
      sell_orders_.size() > 0 && sell_orders_.size() < max_back_levels_ - 2 &&
             abs_allowed_sell > order_size_.toDouble()) {
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

    // Place the backlevel.
    if (new_backlevel_px > 0 && std::isfinite(new_backlevel_px)) {
      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Sell,
                   order_size_,
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
                                         Price{std::to_string(new_backlevel_px)}, order_size_,
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

void AlphaRelWideMM::onFirstFire() {
  approx_mid_ = local_sig_->getValue();
  if (approx_mid_ == 0) {
    return;
  }
  if (set_size_from_notional_) {
    Quantity tgt_order_sz = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_order_notional_ / approx_mid_, true);
    Quantity tgt_max_pos = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_maxpos_notional_ / approx_mid_, true);

    LOG(INFO) << fmt::format("Setting order size from notional, midpx {} : ordersize {} maxpos {}",
                             approx_mid_, tgt_order_sz.toDouble(), tgt_max_pos.toDouble());
    order_size_ = tgt_order_sz;
    max_pos_ = tgt_max_pos;
  }

  // Update min_tick ourselves.
  min_tick_ = pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble();

  // Scale the backlevel-related threhsolds.
  rung_spacing_px_ = effThreshMult() * rung_spacing_mult_ * base_place_thresh_conf_ * approx_mid_;
  // Minimize it with min_tick.

  rung_spacing_px_ = std::max(
      rung_spacing_px_, pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble());

  // Reset the lmr_ema. We might get some early stuff that skews us.
  lspread_ema_ = lspread_;
  lspread_num_ = 0;
  lspread_denom_ = 0;

  needs_first_fire_ = false;

  refreshSizeMultBaseFromCurrent();
}

// This should be called every time any component changes, including in_relative_mode_.
void AlphaRelWideMM::recalcThreshes() { mid_thresh_ = base_place_thresh_conf_ * effThreshMult(); }

void AlphaRelWideMM::setThresh(double thresh) {
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

void AlphaRelWideMM::setThreshMult(double thresh_mult, MultSource src) {
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

  // Scale the backlevel-related threhsolds.
  if (fixed_rung_spacing_thresh_ == 0 && (!needs_first_fire_)) {
    rung_spacing_px_ = eff_mult * rung_spacing_mult_ * base_place_thresh_conf_ * approx_mid_;
    // Minimize it with min_tick.

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
void AlphaRelWideMM::setOrderSize(double size) {
  if (size <= 0) {
    LOG(ERROR) << "Invalid order size " << size << " , skipping";
    return;
  }
  LOG(INFO) << "setOrderSize " << order_size_ << " -> " << size;
  order_size_ = Quantity{std::to_string(size)};
  refreshSizeMultBaseFromCurrent();
}

void AlphaRelWideMM::setSizeMult(double size_mult, MultSource src) {
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

Quantity AlphaRelWideMM::getMaxPos() { return max_pos_; }

// Re-derives base values from current field values by dividing out the
// effective size mult. Called from onFirstFire and setOrderSize, where fields
// change outside of setSizeMult (e.g. notional recalculation). Without this, a
// subsequent setSizeMult would compute from stale base values.
//
// Example: config order_size=10, setSizeMult(2) -> order_size=20, base=10.
// onFirstFire recalcs from notional -> order_size=50 (includes 2x via scaled
// tgt_order_notional_). refreshSizeMultBaseFromCurrent divides by the effective
// mult (2) -> base=25. Now setSizeMult(3) correctly gives 25*3=75.
// Without the refresh, base would still be 10, giving 10*3=30 (wrong).
void AlphaRelWideMM::refreshSizeMultBaseFromCurrent() {
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


SymbolId AlphaRelWideMM::getTradedSymbols() { return symbol_; }

std::vector<Market> AlphaRelWideMM::getMarkets() { return {traded_books_}; }

std::vector<pktrade::BookId> AlphaRelWideMM::getBookIds() {
  std::vector<pktrade::BookId> book_ids;
  for (pktrade::Market m : traded_books_) {
    book_ids.push_back({m, symbol_});
  }
  return book_ids;
}

// callbacks from risk.
// I guess this doesn't do anything either. OK.
void AlphaRelWideMM::newOrdAck(const Order& ord) {}

// This doesn't do anything.
void AlphaRelWideMM::ordUpdate(const Order& ord) {}

// We can remove it from our data structure then.
void AlphaRelWideMM::ordCancel(const Order& ord) {
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
void AlphaRelWideMM::ordExec(const Order& ord, const OrderExecute& exec) {
  if (print_ords_) {
    printf("----------------------\n");
    printf("%s: exec ord %d px %f size %f side %s alpha %f relative_mid %f"
           " lmr_ema %f "
           "\n",
           time_utils::nowToStr().c_str(), ord.pk_order_id, ord.px.toDouble(), exec.qty.toDouble(),
           ord.side == Side::Buy ? "BUY" : "SELL",

           pred_sig_->getValue(), local_minus_remote_ema_ + remote_sig_val_,
           local_minus_remote_ema_);
    printOutstandingOrders();
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

  // Branch 6: Update impulse-curvicity.
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
  last_exec_t_ = time_utils::nowToMs();
  last_exec_decay_t_ = last_exec_t_;
}

// Shouldn't happen. But it might. Let's just call the same thing as
// ordcancel.
void AlphaRelWideMM::ordElim(const Order& ord, const OrderElimination& elim) { ordCancel(ord); }

void AlphaRelWideMM::ordElimPrecog(Side side, Quantity qty) {
  // Assume we missed. In that case, we can fire again.
  last_t_cross_ = 0;
}

void AlphaRelWideMM::ordReject(const Order& ord, const NewOrderReject& rej) { ordCancel(ord); }

// Manage and upkeep on refsig-based threshing.
void AlphaRelWideMM::onSignalValue(int sig_id, double value) {
  if (sig_id == local_sig_idx_) {
    if (local_sig_val_ == 0) {
      // Early exit if we haven't initialized.

      local_sig_val_ = value;
      last_local_mid_t_ = time_utils::nowToMs();
      return;
    }

    double ret = (value - local_sig_val_) / local_sig_val_;

    // Decay the EMS's
    int64_t delta_t = time_utils::nowToMs() - last_local_mid_t_;
    double local_return_ems_decay = std::exp(-delta_t / return_ems_time_const_);
    local_return_ems_ *= local_return_ems_decay;
    local_return_ems_ += ret;

    last_local_mid_t_ = time_utils::nowToMs();
    local_sig_val_ = value;

    // local spread update.
    if (sanitize_lspread_) {
      // Only update from book changes, not trade-caused mid moves.
      // Trade sweeps can blow out one side, giving garbage lspread values.
      if (!local_sig_->justGotTrade()) {
        double raw_lspread = local_sig_->getBAMeasure() - local_sig_->getBBMeasure();
        if (raw_lspread > 0) {
          lspread_ = raw_lspread;

          int64_t lspread_delta_t = time_utils::nowToMs() - lspread_t_;
          lspread_t_ = time_utils::nowToMs();
          double lspread_decay = std::exp(-lspread_delta_t / lspread_time_const_);

          lspread_num_ = lspread_num_ * lspread_decay + lspread_;
          lspread_denom_ = lspread_denom_ * lspread_decay + 1;
          lspread_ema_ = lspread_num_ / lspread_denom_;
        }
      }
    } else {
      // Original behavior: update on every local signal change.
      lspread_ = local_sig_->getBAMeasure() - local_sig_->getBBMeasure();

      int64_t lspread_delta_t = time_utils::nowToMs() - lspread_t_;
      lspread_t_ = time_utils::nowToMs();
      double lspread_decay = std::exp(-lspread_delta_t / lspread_time_const_);

      lspread_num_ = lspread_num_ * lspread_decay + lspread_;
      lspread_denom_ = lspread_denom_ * lspread_decay + 1;
      lspread_ema_ = lspread_num_ / lspread_denom_;
    }
  } else if (sig_id == remote_sig_idx_) {
    LOG_EVERY_N(INFO, 10007) << fmt::format(
        "({}) remote signal alive, value {}", symbol_.get(), value);
    if (remote_sig_val_ != 0) {
      double ret = (value - remote_sig_val_) / remote_sig_val_;

      // Decay and update the remote EMS.
      int64_t delta_t = time_utils::nowToMs() - last_remote_mid_t_;
      double remote_return_ems_decay = std::exp(-delta_t / return_ems_time_const_);

      double decay = std::exp(-delta_t / remote_mid_ems_time_const_ms_);

      remote_return_ems_ *= remote_return_ems_decay;

      net_remote_mid_move_ems_ *= decay;
      net_remote_mid_move_ems_ += ret;

      // Decay the ems' for midmove.
      abs_remote_mid_move_ems_ *= decay;
      abs_remote_mid_move_ems_ += std::abs(ret);

      /*
    printf("%s: onRemoteSignal old %f new %f ret %f decay %f ems %f\n",
      time_utils::nowToStr().c_str(),
      remote_sig_val_, value, ret, decay, remote_return_ems_
    );
    */
      remote_return_ems_ += ret;

      // Update spread too.
      double rbb = remote_sig_->getBBMeasure();
      double rba = remote_sig_->getBAMeasure();

      remote_spread_num_ *= decay;
      remote_spread_denom_ *= decay;

      double effective_spread = remote_sig_->getBAMeasure() - remote_sig_->getBBMeasure();
      remote_spread_num_ += (effective_spread);
      remote_spread_denom_ += 1;
      // Reset the count.

      if (remote_spread_denom_ > 0) {
        /*
        printf(
            "Calculating: effectivespread (%f - %f = %f) | spread_num %f denom "
            "%f \n",
            local_sig_->getBAMeasure(), local_sig_->getBBMeasure(),
            effective_spread, remote_spread_num_, remote_spread_denom_);
          remote_spread_ema_ = remote_spread_num_ / remote_spread_denom_;
          */
      }
    }

    // For calculating lmr.
    remote_sig_val_ = value;
    last_remote_mid_t_ = time_utils::nowToMs();

    // Update vol mechanisms (1-3).
    vol_mechs_.onRemoteUpdate(value);
  }

  // Don't calculate lmr if not ready.
  if (local_sig_val_ == 0 || remote_sig_val_ == 0) {
    return;
  } else {
    // Skip LMR update on remote ticks if lmr_local_only is set.
    bool do_lmr_update = !(lmr_local_only_ && sig_id == remote_sig_idx_);

    if (do_lmr_update) {
      // Branch 8: On non-trade local book updates, apply extra decay to LMR.
      // When the local book genuinely reprices, old LMR observations become less relevant.
      if (lmr_local_book_decay_enabled_ && sig_id == local_sig_idx_ &&
          !local_sig_->justGotTrade()) {
        local_minus_remote_ *= lmr_local_book_decay_factor_;
        local_minus_remote_denom_ *= lmr_local_book_decay_factor_;
      }

      // Branch 4b: Determine LMR sample weight.
      // If the local mid moved due to a trade, dampen this LMR observation.
      double lmr_sample_weight = 1.0;
      if (lmr_trade_dampen_enabled_ && sig_id == local_sig_idx_ &&
          local_sig_->justGotTrade()) {
        lmr_sample_weight = lmr_trade_dampen_factor_;
      }

      // For calculating lmr.
      int64_t lmr_delta_t = time_utils::nowToMs() - last_lmr_update_t_;
      double lmr = local_sig_val_ - remote_sig_val_;
      double lmr_decay = std::exp(-lmr_delta_t / lmr_time_const_);
      local_minus_remote_ = local_minus_remote_ * lmr_decay + lmr * lmr_sample_weight;
      local_minus_remote_denom_ = local_minus_remote_denom_ * lmr_decay + lmr_sample_weight;

      if (local_minus_remote_denom_ > 0.0001) {
        local_minus_remote_ema_ = local_minus_remote_ / local_minus_remote_denom_;
      }

      last_lmr_update_t_ = time_utils::nowToMs();
    }
  }

  // Just do the exec decays here.
  applyExecDecays();
}

// Stats w/ local trds.
void AlphaRelWideMM::onTrade(const LevelBook& bk, const Trade& trd) {
  /*
  This isn't used right now - so commenting it out.
  double ls_ema_ = ls_num_ / ls_denom_;

  // I think I gotta figure out

  bool is_buy = remote_sig_val_ + local_minus_remote_ema_ > local_sig_val_;

  double model_rel_px =
      remote_sig_val_ * (1 + alpha_mult_ * pred_sig_->getValue()) +
      local_minus_remote_ema_;

  double cross_edge_rel;
  double model_edge_rel;

  if (is_buy) {
    cross_edge_rel =
        (remote_sig_val_ + local_minus_remote_ema_ - lba_) / ls_ema_;

    model_edge_rel = (model_rel_px - lba_) / ls_ema_;
  } else {
    cross_edge_rel =
        (lba_ - (remote_sig_val_ + local_minus_remote_ema_)) / ls_ema_;

    model_edge_rel = (lba_ - model_rel_px) / ls_ema_;
  }

  // print trades.
  // px, size, buysell, side,local_mid, remote_mid, lmr,
  // pred_local,lbb,lba,rbb,rba,ls,ls_ema,cross_edge
  std::string tmp_str = fmt::format(
      "{} LOCALEXEC,PX {},SIZE {},SIDE {},LCL {:.2f},REMOTE {:.2f},LMR_EMA "
      "{:.2f},LCL_PRED {:.2f},LBB {},LBA {},LS {},LS_EMA {:.2f},CROSS_EDGE "
      "{:.2f},MODEL_EDGE_REL {:.2f}",
      time_utils::nowToStr().c_str(), trd.px.toDouble(), trd.qty.toDouble(),
      trd.passive_side == Side::Buy ? "SELL" : "BUY", local_sig_val_,
      remote_sig_val_, local_minus_remote_ema_,
      remote_sig_val_ + local_minus_remote_ema_, lbb_, lba_, ls_, ls_ema_,
      cross_edge_rel, model_edge_rel);

  calcPrintReturns(tmp_str, is_buy);
*/
  // Update liq_depth EMS and trade_depth EMS on trades.
  vol_mechs_.onBookUpdate(bk);
  vol_mechs_.onTrade(bk, trd);

  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void AlphaRelWideMM::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void AlphaRelWideMM::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void AlphaRelWideMM::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void AlphaRelWideMM::calcPrintReturns(std::string prefix_str, bool buy_dir) {
  // Get the mid px for now.
  double start_mid = local_sig_val_;

  // Get the mid, I guess.
  pktrade::GlobalVar::event_loop_->onTimeout(
      std::chrono::seconds(120), [this, prefix_str, start_mid, buy_dir] {
        double nu_mid = this->local_sig_val_;
        double ret = (nu_mid - start_mid) / start_mid;
        if (!buy_dir) {
          ret = -ret;
        }

        std::string final_str = "";
        if (ret > 0.0005) {
          final_str = "GOOD";
        } else if (ret < -0.0005) {
          final_str = "BAD";
        }

        printf("%s,%s,%f,%s\n", prefix_str.c_str(), buy_dir ? "BOUGHT" : " SOLD", ret,
               final_str.c_str());

        // What is the return.
        // printf("%s,")
      });
}

// ord rejections.

void AlphaRelWideMM::cancelOutstandingOrds() {
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

void AlphaRelWideMM::printOutstandingOrders() {
  const LevelBook* lvl_book = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  auto& buy_side = lvl_book->buySide();
  double top_bid_px = buy_side.begin()->px.toDouble();

  auto& sell_side = lvl_book->sellSide();
  double top_ask_px = sell_side.begin()->px.toDouble();

  printf("*************** AlphaRelWideMM_ORDERS ***************\n");

  // Do capping of extreme alpha values.
  double alpha = pred_sig_->getValue();
  alpha = alpha * alpha_mult_;

  double remote_pred_price = remote_sig_val_ * (1 + alpha);
  double pred_price = (local_minus_remote_ema_ + remote_pred_price);

  double mid_price = local_minus_remote_ema_ + remote_sig_val_;
  double real_spread = local_sig_->getBAMeasure() - local_sig_->getBBMeasure();

  printf("%s: BB BA %.2f %.2f pos %f local_mid %.2f remote_mid %.2f lmr_ema %.4f "
         "lmr %.2f adjusted_local_mid %.2f local_predpx %.2f  local_spread %.2f "
         "local_ems %.6f "
         "remote_ems %.6f\n \n",
         time_utils::nowToStr().c_str(), local_sig_->getBBMeasure(), local_sig_->getBAMeasure(),
         riskman_->getPos().toDouble(), local_sig_val_, remote_sig_val_, local_minus_remote_ema_,
         local_sig_val_ - remote_sig_val_, mid_price, pred_price, real_spread, local_return_ems_,
         remote_return_ems_);

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
        sell_lvls.push_back(fmt::format(
            "{} : Ord {} {} (next_lvl {}) \n", sell_order_it->px.toDouble(), sell_order_it->pk_oid,
            sell_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
                : "",
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
      sell_lvls.push_back(fmt::format(
          "{} {}: Ord {} {}\n", ask_it->px.toDouble(),
          (ask_it->qty - ask_it->flagged_shares).toDouble(), sell_order_it->pk_oid,
          sell_order_it->cancel_time != 0
              ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
              : ""));
      sell_order_it++;
      for (; sell_order_it != sell_orders_.end() && sell_order_it->px == ask_it->px;
           sell_order_it++) {
        sell_lvls.push_back(fmt::format(
            "  (same px)      : Ord {} {}\n", sell_order_it->pk_oid,
            sell_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
                : ""));
      }

    } else {
      // The book level doesn't contain an order.
      sell_lvls.push_back(fmt::format("{} {}\n", ask_it->px.toDouble(),
                                      (ask_it->qty - ask_it->flagged_shares).toDouble()));
    }
  }
  // Go through the rest of the orders.
  for (; sell_order_it != sell_orders_.end(); sell_order_it++) {
    sell_lvls.push_back(fmt::format(
        "{} : Ord {} {} \n", sell_order_it->px.toDouble(), sell_order_it->pk_oid,
        sell_order_it->cancel_time != 0
            ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
            : ""));
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
        buy_lvls.push_back(fmt::format(
            "{} : Ord {} {} (next_lvl {}) \n", buy_order_it->px.toDouble(), buy_order_it->pk_oid,
            buy_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
                : "",

            bid_it->px.toDouble()));
        buy_order_it++;
      }
    }

    if (buy_order_it == buy_orders_.end()) {
      break;
    }

    // Do we have a sell order here?
    if (bid_it->px == buy_order_it->px) {
      buy_lvls.push_back(fmt::format(
          "{} {}: Ord {} {}\n", bid_it->px.toDouble(),
          (bid_it->qty - bid_it->flagged_shares).toDouble(), buy_order_it->pk_oid,
          buy_order_it->cancel_time != 0
              ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
              : ""));
      buy_order_it++;
      for (; buy_order_it != buy_orders_.end() && buy_order_it->px == bid_it->px; buy_order_it++) {
        buy_lvls.push_back(fmt::format(
            "  (same px)      : Ord {} {}\n", buy_order_it->pk_oid,
            buy_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
                : ""));
      }

    } else {
      buy_lvls.push_back(fmt::format("{} {}\n", bid_it->px.toDouble(),
                                     (bid_it->qty - bid_it->flagged_shares).toDouble()));
    }
  }
  // Go through the rest of the orders.
  for (; buy_order_it != buy_orders_.end(); buy_order_it++) {
    buy_lvls.push_back(fmt::format(
        "{} : Ord {} {}\n", buy_order_it->px.toDouble(), buy_order_it->pk_oid,
        buy_order_it->cancel_time != 0
            ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
            : ""));
  }

  // OK, print them now too.
  for (auto sells_it = sell_lvls.rbegin(); sells_it != sell_lvls.rend(); sells_it++) {
    printf("%s", sells_it->c_str());
  }
  printf("\n<>\n");

  for (auto buys_it = buy_lvls.begin(); buys_it != buy_lvls.end(); buys_it++) {
    printf("%s", buys_it->c_str());
  }
  printf("*************** END_PRINT ***************\n");
}

bool AlphaRelWideMM::canCancelOrd(SimpleOrder& ord, bool fast /*= false*/) {
  int64_t now_ms = time_utils::nowToMs();
  if (!fast && now_ms - ord.place_time < ms_min_ord_lifetime_) {
    return false;
  }
  return (ord.cancel_time == 0 || now_ms - ord.cancel_time > CANCEL_RETRY_INTERVAL);
}

void AlphaRelWideMM::onFinal(const LevelBook& bk) {}

void AlphaRelWideMM::applyExecDecays() {
  if (narrow_when_few_trades_) {
    // Time decay since the last one.
    double decay =
        std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / narrow_thresh_time_const_ms_);
    narrow_fewtrade_ems_buy_ *= decay;
    narrow_fewtrade_ems_sell_ *= decay;
  }

  // Branch 6: Decay impulse-curvicity.
  if (curv_impulse_coef_ > 0) {
    double impulse_decay =
        std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / curv_impulse_tdc_ms_);
    curv_impulse_ *= impulse_decay;
  }

  last_exec_decay_t_ = time_utils::nowToMs();
};

} // namespace pktrade::ordex
