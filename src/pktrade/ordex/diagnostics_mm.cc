#include "diagnostics_mm.h"

#include <fmt/format.h>
#include <glog/logging.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::ordex {

DiagnosticsMM::DiagnosticsMM(SymbolId symbol, const rapidjson::Value& ordex_conf,
                             pktrade::signals::SignalFactory* sf,
                             pktrade::tempos::TempoFactory* tf)
    : Ordex(symbol, ordex_conf), gen_(std::random_device{}()) {

  for (const rapidjson::Value& book : ordex_conf["markets"].GetArray()) {
    traded_books_.push_back(
        magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown));
  }

  if (traded_books_.size() != 1) {
    throw std::runtime_error("DiagnosticsMM only supports trading one market at a time.");
  }
  traded_bid_ = BookId{traded_books_[0], symbol_};

  // Max pos and order size.
  max_pos_ = Quantity{ordex_conf["max_pos"].GetString()};
  order_size_ = Quantity{ordex_conf["order_size"].GetString()};

  if (!pktrade::GlobalVar::live_) {
    gen_.seed(12345);
  }

  // Notional-based sizing.
  if (ordex_conf.HasMember("tgt_order_notional") && ordex_conf.HasMember("tgt_maxpos_notional")) {
    set_size_from_notional_ = true;
    tgt_order_notional_ = ordex_conf["tgt_order_notional"].GetDouble();
    tgt_maxpos_notional_ = ordex_conf["tgt_maxpos_notional"].GetDouble();

    if (tgt_order_notional_ <= 0 || tgt_maxpos_notional_ <= 0) {
      throw std::runtime_error(
          fmt::format("({}) Invalid tgt_order_notional ({}) or tgt_maxpos_notional ({})",
                      symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_));
    }
    if (tgt_order_notional_ > 1e6 || tgt_maxpos_notional_ > 1e7) {
      throw std::runtime_error(fmt::format(
          "({}) tgt_order_notional ({}) or tgt_maxpos_notional ({}) too large",
          symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_));
    }
    if (tgt_order_notional_ > tgt_maxpos_notional_) {
      throw std::runtime_error(
          fmt::format("({}) tgt_order_notional ({}) should be < tgt_maxpos_notional ({})",
                      symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_));
    }
    LOG(INFO) << fmt::format("({}) DiagnosticsMM: tgt_order_notional {} tgt_maxpos_notional {}",
                             symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_);
  }
  if (ordex_conf.HasMember("tgt_order_notional") != ordex_conf.HasMember("tgt_maxpos_notional")) {
    throw std::runtime_error(
        "Both tgt_order_notional and tgt_maxpos_notional must be specified together");
  }

  // Randomization bounds.
  if (ordex_conf.HasMember("order_random_upper_limit")) {
    order_random_upper_limit_ = ordex_conf["order_random_upper_limit"].GetDouble();
    order_random_lower_limit_ = ordex_conf["order_random_lower_limit"].GetDouble();
  }
  if (ordex_conf.HasMember("front_order_random_upper_limit")) {
    front_order_random_upper_limit_ = ordex_conf["front_order_random_upper_limit"].GetDouble();
    front_order_random_lower_limit_ = ordex_conf["front_order_random_lower_limit"].GetDouble();
  }
  dis_ = std::uniform_real_distribution<double>(order_random_lower_limit_, order_random_upper_limit_);
  dis_front_ = std::uniform_real_distribution<double>(front_order_random_lower_limit_,
                                                      front_order_random_upper_limit_);

  // Constant place threshold.
  base_place_thresh_ = ordex_conf["place_thresh"].GetDouble();
  cancel_buffer_ = ordex_conf["cancel_buffer"].GetDouble();

  // Position-based widening.
  per_order_widen_frac_ = ordex_conf.HasMember("per_order_widen_frac")
                              ? ordex_conf["per_order_widen_frac"].GetDouble()
                              : 0;
  ladder_one_sided_ = ordex_conf.HasMember("ladder_one_sided")
                          ? ordex_conf["ladder_one_sided"].GetBool()
                          : true;

  // Rung spacing.
  max_back_levels_ = ordex_conf["max_back_levels"].GetInt();
  rung_spacing_mult_ = ordex_conf["rung_spacing_mult"].GetDouble();
  if (ordex_conf.HasMember("per_backlevel_rung_spacing_mult")) {
    per_backlevel_rung_spacing_mult_ = ordex_conf["per_backlevel_rung_spacing_mult"].GetDouble();
  }

  // Premium EMA.
  if (ordex_conf.HasMember("premium_ema_coef")) {
    premium_ema_coef_ = ordex_conf["premium_ema_coef"].GetDouble();
    if (ordex_conf.HasMember("premium_tdc_s")) {
      premium_tdc_ = ordex_conf["premium_tdc_s"].GetDouble() * 1000;
    }
  }

  // Rate limiters.
  if (ordex_conf.HasMember("ms_between_place")) {
    ms_between_place_ = ordex_conf["ms_between_place"].GetInt64();
  }
  if (ordex_conf.HasMember("ms_between_cxl")) {
    ms_between_cxl_ = ordex_conf["ms_between_cxl"].GetInt64();
  }
  if (ordex_conf.HasMember("ms_between_backlevel")) {
    ms_between_backlevel_ = ordex_conf["ms_between_backlevel"].GetInt64();
  }
  if (ordex_conf.HasMember("min_ord_lifetime")) {
    ms_min_ord_lifetime_ = int(ordex_conf["min_ord_lifetime"].GetDouble() * 1000);
  }

  // Vol EMS time decay.
  if (ordex_conf.HasMember("vol_ems_tdc_s")) {
    vol_ems_tdc_ms_ = ordex_conf["vol_ems_tdc_s"].GetDouble() * 1000;
  }

  // Spread EMA tdc.
  if (ordex_conf.HasMember("spread_ema_tdc_s")) {
    avg_spread_tdc_ms_ = ordex_conf["spread_ema_tdc_s"].GetDouble() * 1000;
  }

  // Hardcoded min_tick override.
  min_tick_ = 0;
  if (ordex_conf.HasMember("hardcoded_min_tick")) {
    min_tick_ = ordex_conf["hardcoded_min_tick"].GetDouble();
  }

  // GTC vs ALO.
  if (ordex_conf.HasMember("mm_gtc") && !ordex_conf["mm_gtc"].GetBool()) {
    tif_ = TimeInForce::ALO;
  }

  // Signals.
  local_sig_ = sf->getByName(ordex_conf["local_sig"].GetString());
  remote_sig_ = sf->getByName(ordex_conf["remote_sig"].GetString());

  if (local_sig_ == nullptr) {
    throw std::runtime_error("DiagnosticsMM: local_sig not successfully created");
  }
  if (remote_sig_ == nullptr) {
    throw std::runtime_error("DiagnosticsMM: remote_sig not successfully created");
  }

  local_sig_idx_ = local_sig_->getID();
  remote_sig_idx_ = remote_sig_->getID();
  local_sig_->addListener(this);
  remote_sig_->addListener(this);

  // Trade caller.
  tc_tempo_ = tf->getByName(ordex_conf["trade_caller"].GetString());
  tc_tempo_->addListener(this);

  // Periodic state-dump cadence (research instrumentation; off when 0).
  if (ordex_conf.HasMember("periodic_dump_ms")) {
    periodic_dump_ms_ = ordex_conf["periodic_dump_ms"].GetInt64();
    if (periodic_dump_ms_ > 0) {
      LOG(INFO) << fmt::format("({}) periodic state dump enabled: {} ms cadence",
                               symbol_.get(), periodic_dump_ms_);
    }
  }

  LOG(INFO) << fmt::format(
      "({}) DiagnosticsMM: place_thresh {} cancel_buffer {} max_back_levels {} rung_spacing_mult {}",
      symbol_.get(), base_place_thresh_, cancel_buffer_, max_back_levels_, rung_spacing_mult_);
}

DiagnosticsMM::~DiagnosticsMM() {
  flushDiagCsv();
  flushPeriodicCsv();
}

void DiagnosticsMM::postSecMaster() {
  if (min_tick_ == 0) {
    min_tick_ = pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble();
  }

  Quantity new_order_size =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, order_size_.toDouble(), true);
  order_size_ = new_order_size;

  Quantity new_max_pos =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, max_pos_.toDouble(), true);
  max_pos_ = new_max_pos;

  LOG(INFO) << fmt::format("({}) DiagnosticsMM PostSecmaster: min_tick {} order_size {} max_pos {}",
                           symbol_.get(), min_tick_, order_size_.toDouble(), max_pos_.toDouble());
}

void DiagnosticsMM::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Always subscribe to local book trades for local volume tracking.
  std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Trd};
  for (auto& book_id : local_sig_->getBookIds()) {
    beacon->addListener(book_id, this, cb_types);
  }
}

signals::Signal* DiagnosticsMM::getPredSignal() { return remote_sig_; }
signals::Signal* DiagnosticsMM::getMidSignal() { return local_sig_; }

void DiagnosticsMM::onTempo(int tempo_id) { tryFire(); }

void DiagnosticsMM::tryFire() {
  num_fires_++;

  if (!riskman_->canTrade()) {
    return;
  }

  if (local_sig_val_ == 0) {
    local_sig_val_ = local_sig_->getValue();
  }
  if (remote_sig_val_ == 0) {
    remote_sig_val_ = remote_sig_->getValue();
  }

  now_fire_t_ = time_utils::nowToMs();

  // Periodic state-dump (research instrumentation). Independent of trading
  // logic — just snapshots the current EMS state at fixed cadence so we can
  // study signal behavior across spike windows the strategy missed.
  if (periodic_dump_ms_ > 0 && now_fire_t_ - last_periodic_t_ >= periodic_dump_ms_) {
    writePeriodicRow(now_fire_t_);
    last_periodic_t_ = now_fire_t_;
  }

  const LevelBook* trade_bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  // pred_px = remote signal (no discounting, no relative pricing).
  pred_px_ = remote_sig_val_;
  premium_adjusted_pred_px_ = pred_px_;

  if (now_fire_t_ - last_premium_adjust_t_ > 500) {
    updatePremiumEma();
  }

  // Premium adjustment (mode 0 only, simplified).
  if (premium_ema_coef_ > 0) {
    if (cur_premium_ > 0 && premium_ema_ > 0) {
      premium_adjusted_pred_px_ =
          pred_px_ + premium_ema_coef_ * std::min(cur_premium_, premium_ema_);
    } else if (cur_premium_ < 0 && premium_ema_ < 0) {
      premium_adjusted_pred_px_ =
          pred_px_ + premium_ema_coef_ * std::max(cur_premium_, premium_ema_);
    }
  }

  if (pred_px_ <= 0 || std::isnan(pred_px_)) {
    return;
  }
  updateThreshes();
  if (needs_first_fire_) {
    onFirstFire(pred_px_);
    return;
  }

  double abs_allowed_buy = riskman_->getSideAlloc(Side::Buy).toDouble();
  double abs_allowed_sell = riskman_->getSideAlloc(Side::Sell).toDouble();

  double ordex_allowed_buy =
      std::min(max_pos_.toDouble(),
               std::max((max_pos_ - riskman_->getMaxFillPosition(Side::Buy)).toDouble(), 0.0));
  double ordex_allowed_sell =
      std::min(max_pos_.toDouble(),
               std::max((riskman_->getMaxFillPosition(Side::Sell) + max_pos_).toDouble(), 0.0));

  ordex_allowed_buy = std::min(ordex_allowed_buy, abs_allowed_buy);
  ordex_allowed_sell = std::min(ordex_allowed_sell, abs_allowed_sell);

  // No crossing in DiagnosticsMM.

  if (now_fire_t_ - last_t_place_ > ms_between_place_ && riskman_->canPlaceMoreOrders()) {
    maybePlaceFront(trade_bk, ordex_allowed_buy, ordex_allowed_sell);
  }

  maybeCancel(trade_bk, ordex_allowed_buy, ordex_allowed_sell);

  if (now_fire_t_ - last_t_backlevel_ > ms_between_backlevel_) {
    manageBacklevels(trade_bk, ordex_allowed_buy - order_size_.toDouble(),
                     ordex_allowed_sell - order_size_.toDouble());
  }
}

void DiagnosticsMM::onFirstFire(double pred_px) {
  LOG(INFO) << fmt::format("({}) DiagnosticsMM ONFIRSTFIRE", symbol_.get());

  if (pred_px == 0) {
    return;
  }

  double scaling_px = pred_px;
  if (local_sig_val_ != 0 && (
      std::abs(pred_px - local_sig_val_) / local_sig_val_ > 1 ||
      std::abs(pred_px - local_sig_val_) / pred_px > 1)) {
    if (oracle_px_ != 0) {
      scaling_px = oracle_px_;
    } else {
      scaling_px = local_sig_val_;
    }
  }

  // Recalc min_tick.
  double new_min_tick = pktrade::GlobalVar::secmaster_->calc_tick_size(scaling_px).toDouble();
  if (!pktrade::approx_equal(min_tick_, new_min_tick)) {
    LOG(ERROR) << fmt::format("({}) Corrected min_tick during firstfire from {} to {}",
                              symbol_.get(), min_tick_, new_min_tick);
    min_tick_ = new_min_tick;
  }

  // Scale order sizes from notional targets.
  if (set_size_from_notional_) {
    Quantity tgt_order_sz = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_order_notional_ / scaling_px, true);
    Quantity tgt_max_pos = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_maxpos_notional_ / scaling_px, true);
    LOG(INFO) << fmt::format(
        "({}) DiagnosticsMM: Setting sizes from notional, predpx {}: ordersize {} maxpos {}",
        symbol_.get(), pred_px, tgt_order_sz.toDouble(), tgt_max_pos.toDouble());
    order_size_ = tgt_order_sz;
    max_pos_ = tgt_max_pos;
  }

  // Rung spacing.
  rung_spacing_px_ = rung_spacing_mult_ * base_place_thresh_ * scaling_px;
  rung_spacing_px_ = std::max(rung_spacing_px_, min_tick_);

  LOG(INFO) << fmt::format("({}) DiagnosticsMM Firstfire rung_spacing_px {} max_back_levels {}",
                           symbol_.get(), rung_spacing_px_, max_back_levels_);

  first_fire_t_ = time_utils::nowToMs();
  needs_first_fire_ = false;
}

void DiagnosticsMM::updateThreshes() {
  // Constant threshold + position-based widening.
  cur_buy_thresh_px_ = base_place_thresh_ * pred_px_;
  cur_sell_thresh_px_ = base_place_thresh_ * pred_px_;

  double cur_pos = riskman_->getPos().toDouble();
  double bias_buy =
      std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * cur_buy_thresh_px_;
  double bias_sell =
      std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * cur_sell_thresh_px_;

  if (cur_pos > 0) {
    cur_buy_thresh_px_ += bias_buy;
    if (!ladder_one_sided_) {
      cur_sell_thresh_px_ -= bias_sell;
    }
  } else {
    cur_sell_thresh_px_ += bias_sell;
    if (!ladder_one_sided_) {
      cur_buy_thresh_px_ -= bias_buy;
    }
  }

  // Floor at 0.5 bps.
  double min_thresh_px = 0.00005 * pred_px_;
  cur_buy_thresh_px_ = std::max(cur_buy_thresh_px_, min_thresh_px);
  cur_sell_thresh_px_ = std::max(cur_sell_thresh_px_, min_thresh_px);

  // Cancel thresholds.
  cur_buy_cancel_thresh_px_ = cur_buy_thresh_px_ * (1 - cancel_buffer_);
  cur_sell_cancel_thresh_px_ = cur_sell_thresh_px_ * (1 - cancel_buffer_);

  double min_cxl = 0.000025 * pred_px_;
  cur_buy_cancel_thresh_px_ = std::max(cur_buy_cancel_thresh_px_, min_cxl);
  cur_sell_cancel_thresh_px_ = std::max(cur_sell_cancel_thresh_px_, min_cxl);
}

void DiagnosticsMM::updatePremiumEma() {
  if (pred_px_ == 0 || local_sig_val_ == 0 || !std::isfinite(pred_px_) ||
      !std::isfinite(local_sig_val_)) {
    return;
  }

  cur_premium_ = local_sig_val_ - pred_px_;

  int64_t dt = now_fire_t_ - last_premium_adjust_t_;
  double alpha = 1.0 - std::exp(-dt / premium_tdc_);
  premium_ema_ = alpha * cur_premium_ + (1.0 - alpha) * premium_ema_;

  last_premium_adjust_t_ = now_fire_t_;
}

// /////////////////////////////////////////////////////////
// Order placement (simplified from RelWideMM2: no queue jump, no TL modes).

void DiagnosticsMM::maybePlaceFront(const LevelBook* lvl_book, double ordex_allowed_buy,
                                    double ordex_allowed_sell) {
  double best_bid = local_sig_->getBBMeasure();
  double best_ask = local_sig_->getBAMeasure();

  if (best_bid == 0 || best_ask == 1e10) {
    if (!lvl_book->side<BuySide>().empty()) {
      best_bid = lvl_book->side<BuySide>().begin()->px.toDouble();
    }
    if (!lvl_book->side<SellSide>().empty()) {
      best_ask = lvl_book->side<SellSide>().begin()->px.toDouble();
    }
  }

  // Buy.
  if (ordex_allowed_buy >= order_size_.toDouble() && buy_orders_.size() < max_back_levels_) {
    double best_buy_px = premium_adjusted_pred_px_ - cur_buy_thresh_px_;
    best_buy_px = roundToNearest(best_buy_px);
    if (best_ask > 0) {
      best_buy_px = std::min(best_ask - min_tick_, best_buy_px);
    }
    if (best_bid > 0) {
      best_buy_px = std::min(best_bid + min_tick_, best_buy_px);
    }

    bool too_crowded =
        (buy_orders_.size() > 0 &&
         buy_orders_.front().px.toDouble() + rung_spacing_px_ > best_buy_px);

    if (!too_crowded) {
      double new_order_size = genOrderSize(true);
      new_order_size = std::min(new_order_size, ordex_allowed_buy);
      Quantity new_order_qty =
          pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, new_order_size, false);
      if (new_order_qty.toDouble() > 0) {
        NewOrder ord{symbol_,
                     traded_books_[0],
                     Side::Buy,
                     new_order_qty,
                     Price{std::to_string(best_buy_px)},
                     OrderType::Limit,
                     tif_,
                     false,
                     false};
        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Front);

          buy_orders_.emplace(buy_orders_.begin(),
                              SimpleOrder{pkord_id, Side::Buy, Price{std::to_string(best_buy_px)},
                                          new_order_qty, SimpleOrderState::LIVE, now_fire_t_, 0});
          last_t_place_ = now_fire_t_;
        }
      }
    }
  }

  // Sell.
  if (ordex_allowed_sell >= order_size_.toDouble() && sell_orders_.size() < max_back_levels_) {
    double best_sell_px = premium_adjusted_pred_px_ + cur_sell_thresh_px_;
    best_sell_px = roundToNearest(best_sell_px);
    if (best_bid > 0) {
      best_sell_px = std::max(best_bid + min_tick_, best_sell_px);
    }
    if (best_ask > 0) {
      best_sell_px = std::max(best_ask - min_tick_, best_sell_px);
    }

    bool too_crowded =
        (sell_orders_.size() > 0 &&
         sell_orders_.front().px.toDouble() - rung_spacing_px_ < best_sell_px);

    if (!too_crowded) {
      double new_order_size = genOrderSize(true);
      new_order_size = std::min(new_order_size, ordex_allowed_sell);
      Quantity new_order_qty =
          pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, new_order_size, false);
      if (new_order_qty.toDouble() > 0) {
        NewOrder ord{symbol_,
                     traded_books_[0],
                     Side::Sell,
                     new_order_qty,
                     Price{std::to_string(best_sell_px)},
                     OrderType::Limit,
                     tif_,
                     false,
                     false};
        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Front);
          sell_orders_.emplace(sell_orders_.begin(),
                               SimpleOrder{pkord_id, Side::Sell,
                                           Price{std::to_string(best_sell_px)}, new_order_qty,
                                           SimpleOrderState::LIVE, now_fire_t_, 0});
          last_t_place_ = now_fire_t_;
        }
      }
    }
  }
}

void DiagnosticsMM::maybeCancel(const LevelBook* lvl_book, double ordex_allowed_buy,
                                double ordex_allowed_sell) {
  bool should_cancel = false;
  int n_iters = 0;

  // Buy side.
  for (auto& one_buy_order : buy_orders_) {
    double one_buy_px = one_buy_order.px.toDouble();
    should_cancel = false;

    if (canCancelOrd(one_buy_order)) {
      if (premium_adjusted_pred_px_ - one_buy_px < cur_buy_cancel_thresh_px_) {
        should_cancel = true;
      } else if (n_iters == 0 && ordex_allowed_buy <= 0) {
        should_cancel = true;
      }
    } else if (canCancelOrd(one_buy_order, true)) {
      if (premium_adjusted_pred_px_ - one_buy_px < 0) {
        should_cancel = true;
      }
    }

    if (should_cancel) {
      one_buy_order.cancel_time = now_fire_t_;
      CancelOrder cancelOrd{one_buy_order.pk_oid};
      riskman_->cancelOrd(cancelOrd);
      last_t_cxl_ = now_fire_t_;
    }
    n_iters++;
  }

  // Sell side.
  n_iters = 0;
  for (auto& one_sell_order : sell_orders_) {
    double one_sell_px = one_sell_order.px.toDouble();
    should_cancel = false;

    if (canCancelOrd(one_sell_order)) {
      if (one_sell_px - premium_adjusted_pred_px_ < cur_sell_cancel_thresh_px_) {
        should_cancel = true;
      } else if (n_iters == 0 && ordex_allowed_sell <= 0) {
        should_cancel = true;
      }
    } else if (canCancelOrd(one_sell_order, true)) {
      if (one_sell_px - premium_adjusted_pred_px_ < 0) {
        should_cancel = true;
      }
    }

    if (should_cancel) {
      one_sell_order.cancel_time = now_fire_t_;
      CancelOrder cancelOrd{one_sell_order.pk_oid};
      riskman_->cancelOrd(cancelOrd);
      last_t_cxl_ = now_fire_t_;
    }
    n_iters++;
  }
}

void DiagnosticsMM::manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy,
                                     double abs_allowed_sell) {
  // Cancel excess buy backlevels.
  if (buy_orders_.size() > 1 &&
      (buy_orders_.size() >= max_back_levels_ || abs_allowed_buy < order_size_.toDouble())) {
    double already_cxl_shrs = 0;
    int already_cxl_orders = 0;

    for (auto it = buy_orders_.rbegin(); it != buy_orders_.rend(); it++) {
      if (it->pk_oid == buy_orders_.begin()->pk_oid) break;
      if (buy_orders_.size() - already_cxl_orders < max_back_levels_ &&
          already_cxl_shrs + abs_allowed_buy > order_size_.toDouble()) {
        break;
      }
      if (it->cancel_time != 0) {
        already_cxl_shrs += order_size_.toDouble();
        already_cxl_orders++;
      }
      if (canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
        break;
      }
    }
  } else if (
      (now_fire_t_ - last_t_place_ > ms_between_backlevel_) && buy_orders_.size() > 0 &&
      buy_orders_.size() < max_back_levels_ - 2 && abs_allowed_buy > order_size_.toDouble()) {
    // Place buy backlevels.
    auto prev_buy_order = buy_orders_.begin();
    double prev_buy_px = prev_buy_order->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;
    double new_backlevel_px = roundToSide(prev_buy_px - effective_rung_spacing, false);
    auto next_buy_order = prev_buy_order + 1;

    while (next_buy_order != buy_orders_.end()) {
      double next_buy_px = next_buy_order->px.toDouble();
      if (new_backlevel_px - next_buy_px > effective_rung_spacing) break;
      effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      prev_buy_px = next_buy_px;
      new_backlevel_px = roundToSide(prev_buy_px - effective_rung_spacing, false);
      prev_buy_order = next_buy_order;
      next_buy_order++;
    }

    double new_order_size = genOrderSize(false);
    new_order_size = std::min(new_order_size, abs_allowed_buy);
    Quantity new_order_qty =
        pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, new_order_size, false);
    if (new_order_qty.toDouble() > 0) {
      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Buy,
                   new_order_qty,
                   Price{std::to_string(new_backlevel_px)},
                   OrderType::Limit,
                   tif_,
                   false,
                   false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Back);
        buy_orders_.emplace(next_buy_order,
                            SimpleOrder{pkord_id, Side::Buy,
                                        Price{std::to_string(new_backlevel_px)}, new_order_qty,
                                        SimpleOrderState::LIVE, now_fire_t_, 0});
      }
    }
  }

  // Cancel excess sell backlevels.
  if (sell_orders_.size() > 1 &&
      (sell_orders_.size() >= max_back_levels_ || abs_allowed_sell < order_size_.toDouble())) {
    double already_cxl_shrs = 0;
    int already_cxl_orders = 0;

    for (auto it = sell_orders_.rbegin(); it != sell_orders_.rend(); it++) {
      if (it->pk_oid == sell_orders_.begin()->pk_oid) break;
      if (sell_orders_.size() - already_cxl_orders < max_back_levels_ &&
          already_cxl_shrs + abs_allowed_sell > order_size_.toDouble()) {
        break;
      }
      if (it->cancel_time != 0) {
        already_cxl_shrs += order_size_.toDouble();
        already_cxl_orders++;
      }
      if (canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
        break;
      }
    }
  } else if (
      (now_fire_t_ - last_t_place_ > ms_between_backlevel_) && sell_orders_.size() > 0 &&
      sell_orders_.size() < max_back_levels_ - 2 && abs_allowed_sell > order_size_.toDouble()) {
    // Place sell backlevels.
    auto prev_sell_order = sell_orders_.begin();
    double prev_sell_px = prev_sell_order->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;
    double new_backlevel_px = roundToSide(prev_sell_px + effective_rung_spacing, true);
    auto next_sell_order = prev_sell_order + 1;

    while (next_sell_order != sell_orders_.end()) {
      double next_sell_px = next_sell_order->px.toDouble();
      if (next_sell_px - new_backlevel_px > effective_rung_spacing) break;
      effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      prev_sell_px = next_sell_px;
      new_backlevel_px = roundToSide(prev_sell_px + effective_rung_spacing, true);
      prev_sell_order = next_sell_order;
      next_sell_order++;
    }

    double new_order_size = genOrderSize(false);
    new_order_size = std::min(new_order_size, abs_allowed_sell);
    Quantity new_order_qty =
        pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, new_order_size, false);
    if (new_order_qty.toDouble() > 0) {
      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Sell,
                   new_order_qty,
                   Price{std::to_string(new_backlevel_px)},
                   OrderType::Limit,
                   tif_,
                   false,
                   false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Back);
        sell_orders_.emplace(next_sell_order,
                             SimpleOrder{pkord_id, Side::Sell,
                                         Price{std::to_string(new_backlevel_px)}, new_order_qty,
                                         SimpleOrderState::LIVE, now_fire_t_, 0});
      }
    }
  }

  // Cancel buy backlevels that violate rung spacing.
  if (buy_orders_.size() > 1) {
    auto prev_buy_it = buy_orders_.begin();
    double prev_buy_px = prev_buy_it->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;

    for (auto it = prev_buy_it + 1; it != buy_orders_.end(); it++) {
      double cur_buy_px = it->px.toDouble();
      double spacing = prev_buy_px - cur_buy_px;
      if (spacing < effective_rung_spacing && canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
      } else {
        prev_buy_px = cur_buy_px;
        effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      }
    }
  }

  // Cancel sell backlevels that violate rung spacing.
  if (sell_orders_.size() > 1) {
    auto prev_sell_it = sell_orders_.begin();
    double prev_sell_px = prev_sell_it->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;

    for (auto it = prev_sell_it + 1; it != sell_orders_.end(); it++) {
      double cur_sell_px = it->px.toDouble();
      double spacing = cur_sell_px - prev_sell_px;
      if (spacing < effective_rung_spacing && canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
      } else {
        prev_sell_px = cur_sell_px;
        effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      }
    }
  }

  last_t_backlevel_ = now_fire_t_;
}

// /////////////////////////////////////////////////////////
// Risk callbacks.

void DiagnosticsMM::newOrdAck(const Order& ord) {}
void DiagnosticsMM::ordUpdate(const Order& ord) {}

void DiagnosticsMM::ordCancel(const Order& ord) {
  if (ord.side == Side::Buy) {
    for (auto it = buy_orders_.begin(); it != buy_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        buy_orders_.erase(it);
        break;
      }
    }
  } else if (ord.side == Side::Sell) {
    for (auto it = sell_orders_.begin(); it != sell_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        sell_orders_.erase(it);
        break;
      }
    }
  }
}

void DiagnosticsMM::ordExec(const Order& ord, const OrderExecute& exec) {
  if (ord.curr_qty == Quantity{0}) {
    ordCancel(ord);
  }

  // Always record fill diagnostics.
  openDiagCsv();
  FillDiagEntry entry;
  entry.fill_time_ms = time_utils::nowToMs();
  entry.fill_px = exec.px.toDouble();
  entry.side_sign = (ord.side == Side::Buy) ? 1 : -1;
  entry.fill_qty = exec.qty.toDouble();
  entry.local_mid = local_sig_val_;
  entry.remote_mid = remote_sig_val_;
  entry.local_bid = local_sig_->getBBMeasure();
  entry.local_ask = local_sig_->getBAMeasure();
  entry.local_spread = (entry.local_bid > 0 && entry.local_ask > 0)
                           ? (entry.local_ask - entry.local_bid)
                           : 0;
  entry.avg_local_spread = avg_local_spread_;
  entry.vol_ems_remote = abs_remote_mid_move_ems_;
  entry.vol_ems_local = abs_local_mid_move_ems_;
  entry.position_after = riskman_->getPos().toDouble();
  entry.pred_px = pred_px_;
  entry.premium_ema = premium_ema_;
  entry.add_liq = exec.add_liq;
  // Multi-tdc vol EMS.
  entry.vol_ems_remote_100ms = abs_remote_mid_move_ems_100ms_;
  entry.vol_ems_remote_1s = abs_remote_mid_move_ems_1s_;
  entry.vol_ems_remote_4s = abs_remote_mid_move_ems_4s_;
  entry.vol_ems_remote_10s = abs_remote_mid_move_ems_10s_;
  entry.vol_ems_remote_30s = abs_remote_mid_move_ems_30s_;
  entry.vol_ems_remote_60s = abs_remote_mid_move_ems_60s_;
  entry.vol_ems_remote_300s = abs_remote_mid_move_ems_300s_;
  entry.momentum_100ms = momentum_ems_100ms_;
  entry.momentum_1s = momentum_ems_1s_;
  entry.momentum_4s = momentum_ems_4s_;
  entry.momentum_10s = momentum_ems_10s_;
  entry.momentum_30s = momentum_ems_30s_;
  // Book sizes.
  entry.local_bb_size = local_sig_->getBBSize();
  entry.local_ba_size = local_sig_->getBASize();
  // Local volume EMS.
  entry.local_vol_ems = local_vol_ems_;
  // Trailing remote returns from ring buffer.
  entry.trailing_ret_30s = 0;
  entry.trailing_ret_60s = 0;
  entry.trailing_ret_300s = 0;
  if (remote_sig_val_ > 0 && !remote_mid_history_.empty()) {
    int64_t now = entry.fill_time_ms;
    static constexpr int64_t lookbacks[] = {30000, 60000, 300000};
    double* ret_ptrs[] = {&entry.trailing_ret_30s, &entry.trailing_ret_60s,
                          &entry.trailing_ret_300s};
    for (int i = 0; i < 3; i++) {
      int64_t target_t = now - lookbacks[i];
      double best_val = 0;
      int64_t best_diff = INT64_MAX;
      for (const auto& h : remote_mid_history_) {
        int64_t diff = std::abs(h.time_ms - target_t);
        if (diff < best_diff) {
          best_diff = diff;
          best_val = h.value;
        }
        if (h.time_ms > target_t) break;
      }
      if (best_val > 0) {
        *ret_ptrs[i] = (remote_sig_val_ - best_val) / best_val;
      }
    }
  }
  // Trade microstructure features.
  entry.trade_rate_ems = trade_rate_ems_;
  entry.trade_size_ems = trade_size_ems_;
  entry.large_trade_rate_ems = large_trade_rate_ems_;
  entry.trade_depth_ems = trade_depth_ems_;
  entry.buy_trade_depth_ems = buy_trade_depth_ems_;
  entry.sell_trade_depth_ems = sell_trade_depth_ems_;
  entry.trade_notional_signed_ems = trade_notional_signed_ems_;
  entry.trade_notional_abs_ems = trade_notional_abs_ems_;
  entry.book_depth_bid = book_depth_bid_ems_;
  entry.book_depth_ask = book_depth_ask_ems_;
  // Deep book depth snapshot at fill time.
  entry.deep_bid_notional = 0;
  entry.deep_ask_notional = 0;
  // Instantaneous liquidity depth at fill time.
  entry.liq_depth_buy_100k = 0;
  entry.liq_depth_buy_250k = 0;
  entry.liq_depth_sell_100k = 0;
  entry.liq_depth_sell_250k = 0;
  {
    const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);
    if (bk && !bk->nonFull()) {
      double mid = entry.local_mid > 0 ? entry.local_mid : pred_px_;
      double range = deep_book_range_frac_ * mid;
      double best_bid = bk->side<BuySide>().empty() ? 0 : bk->side<BuySide>().begin()->px.toDouble();
      double best_ask = bk->side<SellSide>().empty() ? 0 : bk->side<SellSide>().begin()->px.toDouble();
      if (best_bid > 0) {
        for (const auto& lvl : bk->side<BuySide>()) {
          if (best_bid - lvl.px.toDouble() > range) break;
          entry.deep_bid_notional += lvl.px.toDouble() * lvl.qty.toDouble();
        }
      }
      if (best_ask > 0) {
        for (const auto& lvl : bk->side<SellSide>()) {
          if (lvl.px.toDouble() - best_ask > range) break;
          entry.deep_ask_notional += lvl.px.toDouble() * lvl.qty.toDouble();
        }
      }
      // Instantaneous notional-based liquidity depth.
      entry.liq_depth_buy_100k = liqDepth<BuySide>(*bk, mid, 100000);
      entry.liq_depth_buy_250k = liqDepth<BuySide>(*bk, mid, 250000);
      entry.liq_depth_sell_100k = liqDepth<SellSide>(*bk, mid, 100000);
      entry.liq_depth_sell_250k = liqDepth<SellSide>(*bk, mid, 250000);
    }
  }
  // EMS versions of liquidity depth.
  entry.liq_depth_buy_100k_ems = liq_depth_buy_100k_ems_;
  entry.liq_depth_buy_250k_ems = liq_depth_buy_250k_ems_;
  entry.liq_depth_sell_100k_ems = liq_depth_sell_100k_ems_;
  entry.liq_depth_sell_250k_ems = liq_depth_sell_250k_ems_;
  pending_markouts_.push_back(entry);
}

void DiagnosticsMM::ordElim(const Order& ord, const OrderElimination& elim) { ordCancel(ord); }
void DiagnosticsMM::ordReject(const Order& ord, const NewOrderReject& rej) { ordCancel(ord); }

void DiagnosticsMM::cancelOutstandingOrds() {
  now_fire_t_ = time_utils::nowToMs();
  for (auto& ord : buy_orders_) {
    if (canCancelOrd(ord, true)) {
      CancelOrder cancelOrd{ord.pk_oid};
      riskman_->cancelOrd(cancelOrd);
      ord.cancel_time = time_utils::nowToMs();
      last_t_cxl_ = time_utils::nowToMs();
    }
  }
  for (auto& ord : sell_orders_) {
    if (canCancelOrd(ord, true)) {
      CancelOrder cancelOrd{ord.pk_oid};
      riskman_->cancelOrd(cancelOrd);
      ord.cancel_time = time_utils::nowToMs();
      last_t_cxl_ = time_utils::nowToMs();
    }
  }

  // Flush remaining fill diagnostics (including partial markouts) at EOD.
  flushDiagCsv();
}

// /////////////////////////////////////////////////////////
// Signal and trade callbacks.

void DiagnosticsMM::onSignalValue(int sig_id, double value) {
  if (sig_id == remote_sig_idx_) {
    if (value != 0) {
      // Track absolute returns for vol EMS at all time decay constants.
      if (prev_remote_sig_val_ != 0 && last_remote_mid_t_ != 0) {
        double ret = (value - prev_remote_sig_val_) / prev_remote_sig_val_;
        double dt = time_utils::nowToMs() - last_remote_mid_t_;
        double abs_ret = std::abs(ret);

        abs_remote_mid_move_ems_ =
            abs_ret + std::exp(-dt / vol_ems_tdc_ms_) * abs_remote_mid_move_ems_;

        // Multi-tdc vol EMS (always computed).
        abs_remote_mid_move_ems_100ms_ =
            abs_ret + std::exp(-dt / 100.0) * abs_remote_mid_move_ems_100ms_;
        abs_remote_mid_move_ems_1s_ =
            abs_ret + std::exp(-dt / 1000.0) * abs_remote_mid_move_ems_1s_;
        abs_remote_mid_move_ems_4s_ =
            abs_ret + std::exp(-dt / 4000.0) * abs_remote_mid_move_ems_4s_;
        abs_remote_mid_move_ems_10s_ =
            abs_ret + std::exp(-dt / 10000.0) * abs_remote_mid_move_ems_10s_;
        abs_remote_mid_move_ems_30s_ =
            abs_ret + std::exp(-dt / 30000.0) * abs_remote_mid_move_ems_30s_;
        abs_remote_mid_move_ems_60s_ =
            abs_ret + std::exp(-dt / 60000.0) * abs_remote_mid_move_ems_60s_;
        abs_remote_mid_move_ems_300s_ =
            abs_ret + std::exp(-dt / 300000.0) * abs_remote_mid_move_ems_300s_;

        // Multi-tdc momentum EMS (signed return).
        momentum_ems_100ms_ = ret + std::exp(-dt / 100.0) * momentum_ems_100ms_;
        momentum_ems_1s_ = ret + std::exp(-dt / 1000.0) * momentum_ems_1s_;
        momentum_ems_4s_ = ret + std::exp(-dt / 4000.0) * momentum_ems_4s_;
        momentum_ems_10s_ = ret + std::exp(-dt / 10000.0) * momentum_ems_10s_;
        momentum_ems_30s_ = ret + std::exp(-dt / 30000.0) * momentum_ems_30s_;
      }
      prev_remote_sig_val_ = value;
      remote_sig_val_ = value;
      last_remote_mid_t_ = time_utils::nowToMs();

      // Maintain remote mid history ring buffer for trailing returns.
      int64_t now = time_utils::nowToMs();
      remote_mid_history_.push_back({now, value});
      while (!remote_mid_history_.empty() &&
             remote_mid_history_.front().time_ms < now - 310000) {
        remote_mid_history_.pop_front();
      }

      // Process pending markouts against new remote mid.
      if (!pending_markouts_.empty()) {
        processPendingMarkouts(time_utils::nowToMs(), value);
      }
    }
  }

  if (sig_id == local_sig_idx_) {
    // Track absolute returns for local vol EMS.
    if (prev_local_sig_val_ != 0 && last_mid_t_ != 0) {
      double ret = (value - prev_local_sig_val_) / prev_local_sig_val_;
      double dt = time_utils::nowToMs() - last_mid_t_;
      abs_local_mid_move_ems_ =
          std::abs(ret) + std::exp(-dt / vol_ems_tdc_ms_) * abs_local_mid_move_ems_;
    }
    prev_local_sig_val_ = value;

    local_sig_val_ = value;
    last_mid_t_ = time_utils::nowToMs();

    // Process pending markouts so local_mid horizons resolve promptly
    // even if the remote signal is temporarily quiet.
    if (!pending_markouts_.empty()) {
      processPendingMarkouts(time_utils::nowToMs(), remote_sig_val_);
    }

    // Track average local spread (always, unconditional).
    double bb = local_sig_->getBBMeasure();
    double ba = local_sig_->getBAMeasure();
    if (bb > 0 && ba > 0) {
      double spread = ba - bb;
      if (last_spread_update_t_ == 0) {
        avg_local_spread_ = spread;
      } else {
        double dt = time_utils::nowToMs() - last_spread_update_t_;
        double decay = std::exp(-dt / avg_spread_tdc_ms_);
        avg_local_spread_ = spread * (1 - decay) + avg_local_spread_ * decay;
      }
      last_spread_update_t_ = time_utils::nowToMs();
    }
  }
}

void DiagnosticsMM::onTrade(const LevelBook& bk, const Trade& trd) {
  int64_t now_t = time_utils::nowToMs();
  double trade_qty = trd.qty.toDouble();
  double decay = 0;
  if (last_local_vol_t_ > 0) {
    double dt = now_t - last_local_vol_t_;
    decay = std::exp(-dt / trade_micro_tdc_ms_);
    local_vol_ems_ = trade_qty + std::exp(-dt / local_vol_ems_tdc_ms_) * local_vol_ems_;
  } else {
    local_vol_ems_ = trade_qty;
  }

  // Trade rate: count of recent trades.
  trade_rate_ems_ = 1.0 + decay * trade_rate_ems_;

  // Trade size EMS.
  trade_size_ems_ = trade_qty + decay * trade_size_ems_;

  // Trade notional EMS (signed and absolute).
  double trade_px = trd.px.toDouble();
  double trade_notional = trade_px * trade_qty;
  // passive_side=Buy means aggressor was selling (hit the bid), so signed = negative.
  double sign = (trd.passive_side == Side::Buy) ? -1.0 : 1.0;
  trade_notional_signed_ems_ = sign * trade_notional + decay * trade_notional_signed_ems_;
  trade_notional_abs_ems_ = trade_notional + decay * trade_notional_abs_ems_;

  // Large trade detection: size > 2x the recent average.
  double avg_size = (trade_rate_ems_ > 1) ? trade_size_ems_ / trade_rate_ems_ : trade_qty;
  if (trade_qty > 2.0 * avg_size) {
    large_trade_rate_ems_ = 1.0 + decay * large_trade_rate_ems_;
  } else {
    large_trade_rate_ems_ = decay * large_trade_rate_ems_;
  }

  // Trade depth: how far into the book the trade price went.
  // passive_side is the resting side. If passive_side=Buy, aggressor was selling
  // and hit through the bid. Depth = (best_bid - trade_px) / mid.
  if (!bk.nonFull()) {
    double best_bid = bk.side<BuySide>().begin()->px.toDouble();
    double best_ask = bk.side<SellSide>().begin()->px.toDouble();
    double mid = (best_bid + best_ask) / 2.0;
    if (mid > 0) {
      double depth_px = 0;
      if (trd.passive_side == Side::Buy) {
        // Aggressor was selling, hit through the bid.
        depth_px = std::max(best_bid - trade_px, 0.0);
      } else if (trd.passive_side == Side::Sell) {
        // Aggressor was buying, lifted through the ask.
        depth_px = std::max(trade_px - best_ask, 0.0);
      }
      double depth_rel = depth_px / mid;
      trade_depth_ems_ = depth_rel + decay * trade_depth_ems_;
      // Split by aggressor side: passive_side=Buy means aggressor sold, passive_side=Sell means aggressor bought.
      if (trd.passive_side == Side::Sell) {
        buy_trade_depth_ems_ = depth_rel + decay * buy_trade_depth_ems_;
      } else {
        buy_trade_depth_ems_ = decay * buy_trade_depth_ems_;
      }
      if (trd.passive_side == Side::Buy) {
        sell_trade_depth_ems_ = depth_rel + decay * sell_trade_depth_ems_;
      } else {
        sell_trade_depth_ems_ = decay * sell_trade_depth_ems_;
      }

      // Book depth: sum of sizes within 5 ticks of best bid/ask.
      double range = 5 * min_tick_;
      double bid_depth = 0;
      for (const auto& lvl : bk.side<BuySide>()) {
        if (best_bid - lvl.px.toDouble() > range) break;
        bid_depth += lvl.qty.toDouble();
      }
      double ask_depth = 0;
      for (const auto& lvl : bk.side<SellSide>()) {
        if (lvl.px.toDouble() - best_ask > range) break;
        ask_depth += lvl.qty.toDouble();
      }
      book_depth_bid_ems_ = bid_depth + decay * book_depth_bid_ems_;
      book_depth_ask_ems_ = ask_depth + decay * book_depth_ask_ems_;

      // Notional-based liquidity depth EMS.
      liq_depth_buy_100k_ems_ = liqDepth<BuySide>(bk, mid, 100000) + decay * liq_depth_buy_100k_ems_;
      liq_depth_buy_250k_ems_ = liqDepth<BuySide>(bk, mid, 250000) + decay * liq_depth_buy_250k_ems_;
      liq_depth_sell_100k_ems_ = liqDepth<SellSide>(bk, mid, 100000) + decay * liq_depth_sell_100k_ems_;
      liq_depth_sell_250k_ems_ = liqDepth<SellSide>(bk, mid, 250000) + decay * liq_depth_sell_250k_ems_;
    }
  }

  last_local_vol_t_ = now_t;
}

// /////////////////////////////////////////////////////////
// Helpers.

double DiagnosticsMM::roundToNearest(double px_extra_precision) {
  if (min_tick_ == 0) return px_extra_precision;
  return std::round(px_extra_precision / min_tick_) * min_tick_;
}

double DiagnosticsMM::roundToSide(double px, bool round_up) {
  if (min_tick_ == 0) return px;
  if (round_up) return std::ceil(px / min_tick_) * min_tick_;
  return std::floor(px / min_tick_) * min_tick_;
}

template <typename SideType>
double DiagnosticsMM::liqDepth(const LevelBook& bk, double mid, double target_notional) {
  if (mid <= 0 || bk.side<SideType>().empty()) return 0;
  double best_px = bk.side<SideType>().begin()->px.toDouble();
  double accum_notional = 0;
  double last_px = best_px;
  for (const auto& lvl : bk.side<SideType>()) {
    double px = lvl.px.toDouble();
    double qty = lvl.qty.toDouble();
    double lvl_notional = px * qty;
    accum_notional += lvl_notional;
    last_px = px;
    if (accum_notional >= target_notional) break;
  }
  return std::abs(best_px - last_px) / mid;
}

double DiagnosticsMM::getRandomValue(bool front) {
  if (front) return dis_front_(gen_);
  return dis_(gen_);
}

double DiagnosticsMM::genOrderSize(bool front) {
  return order_size_.toDouble() * getRandomValue(front);
}

bool DiagnosticsMM::canCancelOrd(SimpleOrder& ord, bool fast /*= false*/) {
  if (!fast && (now_fire_t_ - ord.place_time < ms_min_ord_lifetime_ ||
                now_fire_t_ - last_t_cxl_ < ms_between_cxl_)) {
    return false;
  }
  return (ord.cancel_time == 0 || now_fire_t_ - ord.cancel_time > CANCEL_RETRY_INTERVAL);
}

Quantity DiagnosticsMM::getMaxPos() { return max_pos_; }
SymbolId DiagnosticsMM::getTradedSymbols() { return symbol_; }
std::vector<Market> DiagnosticsMM::getMarkets() { return {traded_books_}; }
std::vector<pktrade::BookId> DiagnosticsMM::getBookIds() {
  std::vector<pktrade::BookId> book_ids;
  for (pktrade::Market m : traded_books_) {
    book_ids.push_back({m, symbol_});
  }
  return book_ids;
}

// /////////////////////////////////////////////////////////
// Fill diagnostics CSV implementation.

void DiagnosticsMM::openDiagCsv() {
  if (diag_csv_opened_) return;
  std::string path = fmt::format("{}/fill_diag_{}.csv",
                                  pktrade::GlobalVar::out_dir_,
                                  pktrade::GlobalVar::date_);
  diag_csv_.open(path);
  if (!diag_csv_.is_open()) {
    LOG(WARNING) << fmt::format("({}) Failed to open fill diag CSV: {}", symbol_.get(), path);
    return;
  }
  diag_csv_ << "time_ms,side,fill_px,fill_qty,"
            << "local_mid,remote_mid,local_bid,local_ask,local_spread,avg_local_spread,"
            << "vol_ems_remote,vol_ems_local,"
            << "position_after,pred_px,premium_ema,add_liq,"
            << "vol_ems_remote_100ms,vol_ems_remote_1s,vol_ems_remote_4s,"
            << "vol_ems_remote_10s,vol_ems_remote_30s,vol_ems_remote_60s,vol_ems_remote_300s,"
            << "momentum_100ms,momentum_1s,momentum_4s,momentum_10s,momentum_30s,"
            << "local_bb_size,local_ba_size,local_vol_ems,"
            << "trailing_ret_30s,trailing_ret_60s,trailing_ret_300s,"
            << "trade_rate_ems,trade_size_ems,large_trade_rate_ems,trade_depth_ems,"
            << "buy_trade_depth_ems,sell_trade_depth_ems,"
            << "trade_notional_signed_ems,trade_notional_abs_ems,"
            << "book_depth_bid,book_depth_ask,"
            << "deep_bid_notional,deep_ask_notional,"
            << "liq_depth_buy_100k,liq_depth_buy_250k,liq_depth_sell_100k,liq_depth_sell_250k,"
            << "liq_depth_buy_100k_ems,liq_depth_buy_250k_ems,liq_depth_sell_100k_ems,liq_depth_sell_250k_ems,"
            << "local_mid_1s,local_mid_5s,local_mid_30s,local_mid_60s,local_mid_300s,"
            << "remote_mid_1s,remote_mid_5s,remote_mid_30s,remote_mid_60s,remote_mid_300s"
            << "\n";
  diag_csv_opened_ = true;
  LOG(INFO) << fmt::format("({}) Opened fill diag CSV: {}", symbol_.get(), path);
}

void DiagnosticsMM::processPendingMarkouts(int64_t now_ms, double cur_remote_mid) {
  auto it = pending_markouts_.begin();
  while (it != pending_markouts_.end()) {
    auto& e = *it;
    int64_t elapsed = now_ms - e.fill_time_ms;
    for (int i = 0; i < FillDiagEntry::kNumHorizons; ++i) {
      if (!e.horizon_done[i] && elapsed >= FillDiagEntry::kHorizonMs[i]) {
        e.horizon_local_mid[i] = local_sig_val_;
        e.horizon_remote_mid[i] = cur_remote_mid;
        e.horizon_done[i] = true;
        e.horizons_filled++;
      }
    }
    if (e.horizons_filled >= FillDiagEntry::kNumHorizons) {
      writeDiagRow(e);
      it = pending_markouts_.erase(it);
    } else {
      ++it;
    }
  }
}

void DiagnosticsMM::writeDiagRow(const FillDiagEntry& e) {
  if (!diag_csv_.is_open()) return;
  diag_csv_ << e.fill_time_ms << ","
            << e.side_sign << ","
            << e.fill_px << ","
            << e.fill_qty << ","
            << e.local_mid << ","
            << e.remote_mid << ","
            << e.local_bid << ","
            << e.local_ask << ","
            << e.local_spread << ","
            << e.avg_local_spread << ","
            << e.vol_ems_remote << ","
            << e.vol_ems_local << ","
            << e.position_after << ","
            << e.pred_px << ","
            << e.premium_ema << ","
            << (e.add_liq ? 1 : 0) << ","
            << e.vol_ems_remote_100ms << ","
            << e.vol_ems_remote_1s << ","
            << e.vol_ems_remote_4s << ","
            << e.vol_ems_remote_10s << ","
            << e.vol_ems_remote_30s << ","
            << e.vol_ems_remote_60s << ","
            << e.vol_ems_remote_300s << ","
            << e.momentum_100ms << ","
            << e.momentum_1s << ","
            << e.momentum_4s << ","
            << e.momentum_10s << ","
            << e.momentum_30s << ","
            << e.local_bb_size << ","
            << e.local_ba_size << ","
            << e.local_vol_ems << ","
            << e.trailing_ret_30s << ","
            << e.trailing_ret_60s << ","
            << e.trailing_ret_300s << ","
            << e.trade_rate_ems << ","
            << e.trade_size_ems << ","
            << e.large_trade_rate_ems << ","
            << e.trade_depth_ems << ","
            << e.buy_trade_depth_ems << ","
            << e.sell_trade_depth_ems << ","
            << e.trade_notional_signed_ems << ","
            << e.trade_notional_abs_ems << ","
            << e.book_depth_bid << ","
            << e.book_depth_ask << ","
            << e.deep_bid_notional << ","
            << e.deep_ask_notional << ","
            << e.liq_depth_buy_100k << ","
            << e.liq_depth_buy_250k << ","
            << e.liq_depth_sell_100k << ","
            << e.liq_depth_sell_250k << ","
            << e.liq_depth_buy_100k_ems << ","
            << e.liq_depth_buy_250k_ems << ","
            << e.liq_depth_sell_100k_ems << ","
            << e.liq_depth_sell_250k_ems;
  for (int i = 0; i < FillDiagEntry::kNumHorizons; ++i) {
    diag_csv_ << ",";
    if (e.horizon_done[i]) {
      diag_csv_ << e.horizon_local_mid[i];
    }
  }
  for (int i = 0; i < FillDiagEntry::kNumHorizons; ++i) {
    diag_csv_ << ",";
    if (e.horizon_done[i]) {
      diag_csv_ << e.horizon_remote_mid[i];
    }
  }
  diag_csv_ << "\n";
  diag_csv_.flush();
}

void DiagnosticsMM::flushDiagCsv() {
  for (const auto& e : pending_markouts_) {
    writeDiagRow(e);
  }
  pending_markouts_.clear();
  if (diag_csv_.is_open()) {
    diag_csv_.flush();
    diag_csv_.close();
  }
  diag_csv_opened_ = false;
}

// /////////////////////////////////////////////////////////
// Periodic state-dump CSV (research instrumentation).

void DiagnosticsMM::openPeriodicCsv() {
  if (periodic_csv_opened_) return;
  std::string path = fmt::format("{}/periodic_diag_{}.csv",
                                  pktrade::GlobalVar::out_dir_,
                                  pktrade::GlobalVar::date_);
  periodic_csv_.open(path);
  if (!periodic_csv_.is_open()) {
    LOG(WARNING) << fmt::format("({}) Failed to open periodic diag CSV: {}",
                                symbol_.get(), path);
    return;
  }
  periodic_csv_ << "time_ms,local_mid,remote_mid,local_bid,local_ask,local_spread,"
                << "vol_ems_remote_100ms,vol_ems_remote_1s,vol_ems_remote_4s,"
                << "vol_ems_remote_10s,vol_ems_remote_30s,vol_ems_remote_60s,vol_ems_remote_300s,"
                << "momentum_100ms,momentum_1s,momentum_4s,momentum_10s,momentum_30s,"
                << "local_vol_ems,"
                << "trade_rate_ems,trade_size_ems,large_trade_rate_ems,trade_depth_ems,"
                << "buy_trade_depth_ems,sell_trade_depth_ems,"
                << "trade_notional_signed_ems,trade_notional_abs_ems,"
                << "book_depth_bid,book_depth_ask,position\n";
  periodic_csv_opened_ = true;
  LOG(INFO) << fmt::format("({}) Opened periodic diag CSV: {}", symbol_.get(), path);
}

void DiagnosticsMM::writePeriodicRow(int64_t now_ms) {
  openPeriodicCsv();
  if (!periodic_csv_.is_open()) return;
  double local_bid = local_sig_->getBBMeasure();
  double local_ask = local_sig_->getBAMeasure();
  double spread = (local_bid > 0 && local_ask > 0) ? (local_ask - local_bid) : 0;
  periodic_csv_ << now_ms << ","
                << local_sig_val_ << "," << remote_sig_val_ << ","
                << local_bid << "," << local_ask << "," << spread << ","
                << abs_remote_mid_move_ems_100ms_ << ","
                << abs_remote_mid_move_ems_1s_ << ","
                << abs_remote_mid_move_ems_4s_ << ","
                << abs_remote_mid_move_ems_10s_ << ","
                << abs_remote_mid_move_ems_30s_ << ","
                << abs_remote_mid_move_ems_60s_ << ","
                << abs_remote_mid_move_ems_300s_ << ","
                << momentum_ems_100ms_ << ","
                << momentum_ems_1s_ << ","
                << momentum_ems_4s_ << ","
                << momentum_ems_10s_ << ","
                << momentum_ems_30s_ << ","
                << local_vol_ems_ << ","
                << trade_rate_ems_ << ","
                << trade_size_ems_ << ","
                << large_trade_rate_ems_ << ","
                << trade_depth_ems_ << ","
                << buy_trade_depth_ems_ << ","
                << sell_trade_depth_ems_ << ","
                << trade_notional_signed_ems_ << ","
                << trade_notional_abs_ems_ << ","
                << book_depth_bid_ems_ << "," << book_depth_ask_ems_ << ","
                << riskman_->getPos().toDouble() << "\n";
}

void DiagnosticsMM::flushPeriodicCsv() {
  if (periodic_csv_.is_open()) {
    periodic_csv_.flush();
    periodic_csv_.close();
  }
  periodic_csv_opened_ = false;
}

} // namespace pktrade::ordex
