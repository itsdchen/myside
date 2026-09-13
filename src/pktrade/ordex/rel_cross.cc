#include "rel_cross.h"

#include <algorithm>

#include <fmt/format.h>
#include <glog/logging.h>
#include <magic_enum.hpp>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::ordex {

RelCross::RelCross(SymbolId symbol, const rapidjson::Value& ordex_conf,
                   pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf)
    : Ordex(symbol, ordex_conf) {

  for (const rapidjson::Value& book : ordex_conf["markets"].GetArray()) {
    traded_books_.push_back(
        magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown));
  }

  if (traded_books_.size() != 1) {
    throw std::runtime_error("RelCross only supports trading one market at a time.");
  }
  traded_bid_ = BookId{traded_books_[0], symbol_};

  // Sizing.
  max_pos_ = Quantity{ordex_conf["max_pos"].GetString()};
  order_size_ = Quantity{ordex_conf["order_size"].GetString()};

  if (ordex_conf.HasMember("tgt_order_notional") && ordex_conf.HasMember("tgt_maxpos_notional")) {
    set_size_from_notional_ = true;
    tgt_order_notional_ = ordex_conf["tgt_order_notional"].GetDouble();
    tgt_maxpos_notional_ = ordex_conf["tgt_maxpos_notional"].GetDouble();
    if (tgt_order_notional_ <= 0 || tgt_maxpos_notional_ <= 0 ||
        tgt_order_notional_ > tgt_maxpos_notional_) {
      throw std::runtime_error(
          fmt::format("({}) Invalid notional sizing: order={} maxpos={}",
                      symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_));
    }
    LOG(INFO) << fmt::format("({}) RelCross using notional sizing: order={} maxpos={}",
                             symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_);
  }

  // Threshold params.
  base_cross_thresh_ = ordex_conf["base_cross_thresh"].GetDouble();

  if (ordex_conf.HasMember("exit_adjust")) {
    exit_adjust_ = ordex_conf["exit_adjust"].GetDouble();
  }

  if (ordex_conf.HasMember("per_order_widen_frac")) {
    per_order_widen_frac_ = ordex_conf["per_order_widen_frac"].GetDouble();
  }

  // Impulse curvicity.
  if (ordex_conf.HasMember("curv_impulse_tdc")) {
    curv_impulse_tdc_ms_ = ordex_conf["curv_impulse_tdc"].GetDouble() * 1000;
    curv_impulse_coef_ = ordex_conf["curv_impulse_coef"].GetDouble();
  }

  // Pred momentum.
  if (ordex_conf.HasMember("pred_momentum_tdc") && ordex_conf.HasMember("pred_momentum_coef")) {
    pred_momentum_tdc_ms_ = ordex_conf["pred_momentum_tdc"].GetDouble() * 1000;
    pred_momentum_coef_ = ordex_conf["pred_momentum_coef"].GetDouble();
    LOG(INFO) << fmt::format("({}) RelCross pred_momentum enabled: tdc={}ms coef={}",
                             symbol_.get(), pred_momentum_tdc_ms_, pred_momentum_coef_);
  }

  // Premium EMA.
  if (ordex_conf.HasMember("premium_ema_coef")) {
    premium_ema_coef_ = ordex_conf["premium_ema_coef"].GetDouble();
    if (premium_ema_coef_ < 0) {
      throw std::runtime_error(
          fmt::format("Invalid premium_ema_coef {} given", premium_ema_coef_));
    }
    if (ordex_conf.HasMember("premium_tdc_s")) {
      premium_tdc_ = ordex_conf["premium_tdc_s"].GetDouble() * 1000;
    }
    LOG(INFO) << fmt::format("({}) RelCross premium_ema: coef={} tdc={}ms",
                             symbol_.get(), premium_ema_coef_, premium_tdc_);
  }

  // Passive exit.
  if (ordex_conf.HasMember("passive_exit_enabled") && ordex_conf["passive_exit_enabled"].GetBool()) {
    passive_exit_enabled_ = true;
    if (ordex_conf.HasMember("passive_exit_pct"))
      passive_exit_pct_ = ordex_conf["passive_exit_pct"].GetDouble();
    if (ordex_conf.HasMember("passive_exit_style"))
      passive_exit_style_ = ordex_conf["passive_exit_style"].GetInt();
    if (ordex_conf.HasMember("passive_exit_decay_per_min"))
      passive_exit_decay_per_min_ = ordex_conf["passive_exit_decay_per_min"].GetDouble();
    if (ordex_conf.HasMember("passive_exit_min_pct"))
      passive_exit_min_pct_ = ordex_conf["passive_exit_min_pct"].GetDouble();
    if (ordex_conf.HasMember("passive_exit_delay_ms"))
      passive_exit_delay_ = std::chrono::milliseconds(ordex_conf["passive_exit_delay_ms"].GetInt64());
    if (ordex_conf.HasMember("passive_exit_refresh_s"))
      passive_exit_refresh_t_ = std::chrono::seconds(ordex_conf["passive_exit_refresh_s"].GetInt64());
    LOG(INFO) << fmt::format("({}) RelCross passive exit: pct={} style={} decay={}/min min_pct={} delay={}ms refresh={}s",
                             symbol_.get(), passive_exit_pct_, passive_exit_style_,
                             passive_exit_decay_per_min_, passive_exit_min_pct_,
                             passive_exit_delay_.count(), passive_exit_refresh_t_.count());
  }

  // Vol/liq mechanisms.
  vol_mechs_.parseConfig(symbol_.get(), ordex_conf);

  // Rate limiting.
  ms_between_cross_ = ordex_conf["ms_between_cross"].GetInt64();

  // Spread-aware entry filter.
  if (ordex_conf.HasMember("min_edge_over_spread_bps")) {
    min_edge_over_spread_bps_ = ordex_conf["min_edge_over_spread_bps"].GetDouble();
    LOG(INFO) << fmt::format("({}) RelCross spread filter: min_edge_over_spread={}bps",
                             symbol_.get(), min_edge_over_spread_bps_);
  }

  if (ordex_conf.HasMember("cross_price_mode")) {
    cross_price_mode_ = ordex_conf["cross_price_mode"].GetInt();
  }

  if (ordex_conf.HasMember("size_signal_max_mult")) {
    size_signal_max_mult_ = ordex_conf["size_signal_max_mult"].GetDouble();
  }

  // Hold time exit decay.
  if (ordex_conf.HasMember("hold_decay_tdc_s")) {
    hold_decay_tdc_s_ = ordex_conf["hold_decay_tdc_s"].GetDouble();
    if (ordex_conf.HasMember("hold_decay_floor"))
      hold_decay_floor_ = ordex_conf["hold_decay_floor"].GetDouble();
    LOG(INFO) << fmt::format("({}) RelCross hold decay: tdc={}s floor={}",
                             symbol_.get(), hold_decay_tdc_s_, hold_decay_floor_);
  }
  if (ordex_conf.HasMember("forced_exit_after_s")) {
    forced_exit_after_s_ = ordex_conf["forced_exit_after_s"].GetDouble();
  }

  // Remote momentum extrapolation.
  if (ordex_conf.HasMember("remote_mom_coef")) {
    remote_mom_coef_ = ordex_conf["remote_mom_coef"].GetDouble();
    if (ordex_conf.HasMember("remote_mom_tdc_ms"))
      remote_mom_tdc_ms_ = ordex_conf["remote_mom_tdc_ms"].GetDouble();
    if (ordex_conf.HasMember("remote_mom_max_thresh_frac"))
      remote_mom_max_thresh_frac_ = ordex_conf["remote_mom_max_thresh_frac"].GetDouble();
    LOG(INFO) << fmt::format("({}) RelCross remote momentum: coef={} tdc={}ms max_thresh_frac={}",
                             symbol_.get(), remote_mom_coef_, remote_mom_tdc_ms_,
                             remote_mom_max_thresh_frac_);
  }

  // SNR-based threshold scaling.
  if (ordex_conf.HasMember("snr_thresh_coef")) {
    snr_thresh_coef_ = ordex_conf["snr_thresh_coef"].GetDouble();
    if (ordex_conf.HasMember("snr_vol_tdc"))
      snr_vol_tdc_ms_ = ordex_conf["snr_vol_tdc"].GetDouble() * 1000;
    LOG(INFO) << fmt::format("({}) RelCross SNR threshold: coef={} vol_tdc={}s",
                             symbol_.get(), snr_thresh_coef_, snr_vol_tdc_ms_ / 1000);
  }

  // Flip-through.
  if (ordex_conf.HasMember("flip_through")) {
    flip_through_ = ordex_conf["flip_through"].GetBool();
    LOG(INFO) << fmt::format("({}) RelCross flip_through={}", symbol_.get(), flip_through_);
  }

  // Trade rate conditioning.
  if (ordex_conf.HasMember("trade_rate_widen_coef")) {
    trade_rate_widen_coef_ = ordex_conf["trade_rate_widen_coef"].GetDouble();
    if (ordex_conf.HasMember("trade_rate_tdc"))
      trade_rate_tdc_ms_ = ordex_conf["trade_rate_tdc"].GetDouble() * 1000;
    if (ordex_conf.HasMember("trade_rate_baseline_tdc"))
      trade_rate_baseline_tdc_ms_ = ordex_conf["trade_rate_baseline_tdc"].GetDouble() * 1000;
    LOG(INFO) << fmt::format("({}) RelCross trade_rate_widen: coef={} tdc={}s baseline_tdc={}s",
                             symbol_.get(), trade_rate_widen_coef_,
                             trade_rate_tdc_ms_ / 1000, trade_rate_baseline_tdc_ms_ / 1000);
  }

  // Precog-miss-aware threshold tightening.
  if (ordex_conf.HasMember("precog_miss_da_enabled") &&
      ordex_conf["precog_miss_da_enabled"].GetBool()) {
    precog_miss_da_enabled_ = true;
    if (ordex_conf.HasMember("precog_miss_max_adjust"))
      precog_miss_max_adjust_ = ordex_conf["precog_miss_max_adjust"].GetDouble();
    if (ordex_conf.HasMember("precog_miss_da_tdc_s"))
      precog_miss_da_tdc_ms_ =
          int64_t(ordex_conf["precog_miss_da_tdc_s"].GetDouble() * 1000);
    if (ordex_conf.HasMember("precog_miss_da_per_unit"))
      precog_miss_da_per_unit_ = ordex_conf["precog_miss_da_per_unit"].GetDouble();
    LOG(INFO) << fmt::format(
        "({}) RelCross precog miss da: max_adjust={} tdc={}ms per_unit={}",
        symbol_.get(), precog_miss_max_adjust_, precog_miss_da_tdc_ms_,
        precog_miss_da_per_unit_);
  }

  // Feed-lag-aware widen (regime-aware backoff for HL congestion bursts).
  if (ordex_conf.HasMember("feed_lag_widen_coef")) {
    feed_lag_widen_coef_ = ordex_conf["feed_lag_widen_coef"].GetDouble();
    if (ordex_conf.HasMember("feed_lag_baseline_ms"))
      feed_lag_baseline_ms_ = ordex_conf["feed_lag_baseline_ms"].GetInt64();
    LOG(INFO) << fmt::format("({}) RelCross feed_lag_widen: coef={} baseline={}ms",
                             symbol_.get(), feed_lag_widen_coef_, feed_lag_baseline_ms_);
  }


  // Signals.
  local_sig_ = sf->getByName(ordex_conf["local_sig"].GetString());
  remote_sig_ = sf->getByName(ordex_conf["remote_sig"].GetString());

  if (local_sig_ == nullptr) {
    throw std::runtime_error("RelCross: local_sig_ not successfully created");
  }
  if (remote_sig_ == nullptr) {
    throw std::runtime_error("RelCross: remote_sig_ not successfully created");
  }

  local_sig_idx_ = local_sig_->getID();
  remote_sig_idx_ = remote_sig_->getID();

  local_sig_->addListener(this);
  remote_sig_->addListener(this);

  // Tempo.
  tc_tempo_ = tf->getByName(ordex_conf["trade_caller"].GetString());
  tc_tempo_->addListener(this);

  LOG(INFO) << fmt::format("({}) RelCross created: base_thresh={} exit_adjust={} "
                           "per_order_widen={} ms_between_cross={} ",
                           symbol_.get(), base_cross_thresh_, exit_adjust_,
                           per_order_widen_frac_, ms_between_cross_);

  refreshSizeMultBaseFromCurrent();
}

void RelCross::postSecMaster() {
  min_tick_ = pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble();

  Quantity new_order_size =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, order_size_.toDouble(), true);
  order_size_ = new_order_size;

  Quantity new_max_pos =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, max_pos_.toDouble(), true);
  max_pos_ = new_max_pos;

  LOG(INFO) << fmt::format("({}) RelCross postSecMaster: min_tick={} order_size={} max_pos={}",
                           symbol_.get(), min_tick_, order_size_.toDouble(), max_pos_.toDouble());
}

void RelCross::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Subscribe to trades for liq_depth, trade_rate, or feed-lag mechanisms.
  if (vol_mechs_.hasLiqDepth() || trade_rate_widen_coef_ > 0 ||
      feed_lag_widen_coef_ > 0) {
    std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Trd};
    for (auto& book_id : local_sig_->getBookIds()) {
      beacon->addListener(book_id, this, cb_types);
    }
  }

  // BTC heartbeat: when feed-lag-aware widening is on, also listen to BTC
  // trades. BTC trades arrive every 10s of ms so last_feed_lag_ms_ stays
  // fresh even when our symbol is sparse (pre-market, low-vol equities, etc.).
  // Requires BTC to be loaded into the run (e.g. via extra_subs on this
  // pktrader); silently skipped if not.
  if (feed_lag_widen_coef_ > 0 && symbol_.get() != "BTC") {
    BookId btc_id{Market::Hyperliquid, SymbolId{"BTC"}};
    if (pktrade::GlobalVar::book_manager_->find(btc_id) != nullptr) {
      beacon->addListener(btc_id, this, {pktrade::md::BeaconCBType::Trd});
    } else {
      LOG(WARNING) << fmt::format(
          "({}) RelCross feed_lag_widen_coef>0 but BTC book not loaded; "
          "lag will only update from this symbol's trades.", symbol_.get());
    }
  }
}

signals::Signal* RelCross::getPredSignal() { return remote_sig_; }
signals::Signal* RelCross::getMidSignal() { return local_sig_; }

void RelCross::aggFlat() {
  Quantity pos = riskman_->getPos();
  if (pos == Quantity{0}) {
    return;
  }

  const LevelBook* trade_bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  if (pos > Quantity{0}) {
    // Sell to flatten.
    Quantity sell_amt = riskman_->getMaxFillPosition(Side::Sell, true);
    if (sell_amt <= Quantity{0}) {
      return;
    }
    auto& bk_side = trade_bk->buySide();
    if (bk_side.empty()) return;

    Price sell_px = bk_side.begin()->px;
    NewOrder ord{symbol_, traded_books_[0], Side::Sell, sell_amt, sell_px,
                 OrderType::Limit, TimeInForce::IOC, false, true};
    riskman_->sendOrd(ord);
  } else {
    // Buy to flatten.
    Quantity buy_amt = riskman_->getMaxFillPosition(Side::Buy, true);
    if (buy_amt >= Quantity{0}) {
      return;
    }
    Quantity buy_qty = -buy_amt;
    auto& bk_side = trade_bk->sellSide();
    if (bk_side.empty()) return;

    Price buy_px = bk_side.begin()->px;
    NewOrder ord{symbol_, traded_books_[0], Side::Buy, buy_qty, buy_px,
                 OrderType::Limit, TimeInForce::IOC, false, true};
    riskman_->sendOrd(ord);
  }
}

void RelCross::onTempo(int tempo_id) { tryFire(); }

void RelCross::tryFire() {
  num_fires_++;

  if (!riskman_->canTrade() || TL7_no_new_ords_) {
    return;
  }

  // Ensure signals have values.
  if (local_sig_val_ == 0) {
    local_sig_val_ = local_sig_->getValue();
  }
  if (remote_sig_val_ == 0) {
    remote_sig_val_ = remote_sig_->getValue();
  }

  now_fire_t_ = time_utils::nowToMs();

  // Bail if remote signal data is stale (> 1s old).
  int64_t remote_age_ms = now_fire_t_ - remote_sig_->getLastTransactTime();
  if (remote_age_ms > 1000) {
    LOG_EVERY_N(INFO, 37) << fmt::format(
        "({}) RelCross skipping cross: remote signal stale ({}ms old)",
        symbol_.get(), remote_age_ms);
    return;
  } else {
    /*
    Only for debug. 
    LOG_EVERY_N(INFO, 37) << 
    fmt::format(
              "({}) RelCross OKWITH cross: remote signal stale ({}ms old)",
        symbol_.get(), remote_age_ms);
        */
  }

  // TL5 → aggFlat (IOC to flatten).
  if (TL6_cross_exit_) {
    aggFlat();
    return;
  }

  // Compute pred_px from remote signal.
  pred_px_ = remote_sig_val_;
  premium_adjusted_pred_px_ = pred_px_;

  if (pred_px_ <= 0 || std::isnan(pred_px_)) {
    return;
  }

  // Update premium EMA.
  if (now_fire_t_ - last_premium_adjust_t_ > 500) {
    updatePremiumEma();
  }

  // Apply premium adjustment to pred_px. Use the EMA directly rather than
  // min/max-clamping with cur_premium: the old clamping meant a fresh basis
  // shock under-adjusted (EMA lags) and a closing shock also under-adjusted
  // (cur_premium near 0), so we both over-fired entries and failed to exit.
  if (premium_ema_coef_ > 0) {
    premium_adjusted_pred_px_ = pred_px_ + premium_ema_coef_ * premium_ema_;
  }

  // Apply remote momentum extrapolation.
  last_remote_mom_adjust_px_ = 0;
  last_remote_mom_cap_px_ = 0;
  if (remote_mom_coef_ != 0) {
    double remote_mom_adjust_px = remote_mom_coef_ * remote_move_ems_;
    if (remote_mom_max_thresh_frac_ > 0) {
      double cap_px = remote_mom_max_thresh_frac_ * base_cross_thresh_ * effThreshMult() * pred_px_;
      last_remote_mom_cap_px_ = cap_px;
      remote_mom_adjust_px = std::clamp(remote_mom_adjust_px, -cap_px, cap_px);
    }
    last_remote_mom_adjust_px_ = remote_mom_adjust_px;
    premium_adjusted_pred_px_ += remote_mom_adjust_px;
  }

  // Update pred momentum.
  updatePredMomentumEms(pred_px_);

  // Update thresholds.
  updateThreshes();

  if (needs_first_fire_) {
    onFirstFire(pred_px_);
    return;
  }

  // Risk allotments.
  double abs_allowed_buy = riskman_->getSideAlloc(Side::Buy).toDouble();
  double abs_allowed_sell = riskman_->getSideAlloc(Side::Sell).toDouble();

  double ordex_allowed_buy =
      std::min(max_pos_.toDouble(),
               std::max((max_pos_ - riskman_->getMaxFillPosition(Side::Buy, true)).toDouble(), 0.0));
  double ordex_allowed_sell =
      std::min(max_pos_.toDouble(),
               std::max((riskman_->getMaxFillPosition(Side::Sell, true) + max_pos_).toDouble(), 0.0));

  ordex_allowed_buy = std::min(ordex_allowed_buy, abs_allowed_buy);
  ordex_allowed_sell = std::min(ordex_allowed_sell, abs_allowed_sell);

  LOG_EVERY_N(INFO, 307) << fmt::format(
      "({}) RelCross tryFire: local={} remote={} pred={} prem_pred={} "
      "buy_thresh={:.4f} sell_thresh={:.4f} alloc=({:.0f},{:.0f})",
      symbol_.get(), local_sig_val_, remote_sig_val_, pred_px_, premium_adjusted_pred_px_,
      cur_buy_cross_thresh_px_, cur_sell_cross_thresh_px_,
      ordex_allowed_buy, ordex_allowed_sell);

  vol_mechs_.logState();

  // Forced exit after max hold time.
  if (forced_exit_after_s_ > 0 && last_entry_t_ > 0 &&
      riskman_->getPos().toDouble() != 0) {
    double held_s = (now_fire_t_ - last_entry_t_) / 1000.0;
    if (held_s > forced_exit_after_s_) {
      aggFlat();
      return;
    }
  }

  // Rate-limit crossing.
  if (now_fire_t_ - last_t_cross_ >= ms_between_cross_) {
    maybeCross(ordex_allowed_buy, ordex_allowed_sell);
  }
}

void RelCross::updateThreshes() {
  double base_px = base_cross_thresh_ * effThreshMult() * pred_px_;
  cur_buy_cross_thresh_px_ = base_px;
  cur_sell_cross_thresh_px_ = base_px;

  // 1. Position bias: widen against adding.
  double cur_pos = riskman_->getPos().toDouble();
  double bias = std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * base_px;

  if (cur_pos > 0) {
    // Long: widen buy side (harder to add), optionally narrow sell side.
    cur_buy_cross_thresh_px_ += bias;
  } else if (cur_pos < 0) {
    // Short: widen sell side (harder to add), optionally narrow buy side.
    cur_sell_cross_thresh_px_ += bias;
  }

  // 2. Impulse curvicity: after fill, widen same direction.
  if (curv_impulse_coef_ > 0) {
    double impulse_offset = std::abs(curv_impulse_) * curv_impulse_coef_ * base_px;
    if (curv_impulse_ > 0) {
      cur_buy_cross_thresh_px_ += impulse_offset;
    } else if (curv_impulse_ < 0) {
      cur_sell_cross_thresh_px_ += impulse_offset;
    }
  }

  // 3. Pred momentum: if price rising, widen sell side.
  if (pred_momentum_coef_ > 0) {
    if (pred_momentum_ems_ > 0) {
      cur_sell_cross_thresh_px_ += pred_momentum_ems_ * pred_momentum_coef_ * pred_px_;
    } else if (pred_momentum_ems_ < 0) {
      cur_buy_cross_thresh_px_ += std::abs(pred_momentum_ems_) * pred_momentum_coef_ * pred_px_;
    }
  }

  // 4. VolMechanisms: vol_widen + vol_norm + vol_ratio + liq_depth.
  {
    auto [bw, sw] = vol_mechs_.getVolWidenPx(pred_px_, cur_buy_cross_thresh_px_,
                                              cur_sell_cross_thresh_px_);
    cur_buy_cross_thresh_px_ += bw;
    cur_sell_cross_thresh_px_ += sw;
    auto [ldb, lds] = vol_mechs_.getLiqDepthWidenPx(pred_px_);
    cur_buy_cross_thresh_px_ += ldb;
    cur_sell_cross_thresh_px_ += lds;
  }

  // 5. SNR-based threshold scaling.
  // How far apart are local and remote (the "signal") relative to recent
  // remote jitter (the "noise")?  When SNR is high the remote move is
  // clean, so we reduce the threshold and cross sooner.
  if (snr_thresh_coef_ > 0 && snr_vol_ems_ > 0 && pred_px_ > 0) {
    double dislocation = std::abs(premium_adjusted_pred_px_ - local_sig_val_) / pred_px_;
    double snr = dislocation / snr_vol_ems_;
    // Only narrow when SNR exceeds 1/coef; otherwise leave threshold alone.
    double snr_divisor = std::max(snr * snr_thresh_coef_, 1.0);
    cur_buy_cross_thresh_px_ /= snr_divisor;
    cur_sell_cross_thresh_px_ /= snr_divisor;
  }

  // 6. Trade rate conditioning.
  // Oracle DP shows crossing is less profitable when trade rate is high.
  // Widen threshold proportionally to how much current rate exceeds baseline.
  if (trade_rate_widen_coef_ > 0 && trade_rate_baseline_ > 1) {
    double rate_ratio = trade_rate_ems_ / trade_rate_baseline_;
    double rate_excess = std::max(rate_ratio - 1.0, 0.0);
    double rate_widen = trade_rate_widen_coef_ * rate_excess * base_cross_thresh_ * pred_px_;
    cur_buy_cross_thresh_px_ += rate_widen;
    cur_sell_cross_thresh_px_ += rate_widen;
  }

  // 7. Feed-lag-aware widen. During HL congestion bursts, IOCs sent against
  // a stale book get adversely selected. Widen proportional to the lag
  // excess so we naturally back off (applied to both sides — exits also
  // miss during congestion, and arguably should hold).
  if (feed_lag_widen_coef_ > 0) {
    int64_t lag_excess_ms = std::max<int64_t>(0, last_feed_lag_ms_ - feed_lag_baseline_ms_);
    double lag_factor_s = lag_excess_ms / 1000.0;
    double lag_widen = feed_lag_widen_coef_ * lag_factor_s * base_cross_thresh_ * pred_px_;
    cur_buy_cross_thresh_px_ += lag_widen;
    cur_sell_cross_thresh_px_ += lag_widen;
  }

  // 8. Precog-miss-aware tighten. After near-miss IOCs, tighten the matching
  // side's threshold to try harder next time. Multiplicative, decays smoothly
  // over precog_miss_da_tdc_ms_. Updated by ordElimPrecog (called from riskman).
  last_buy_miss_adjust_ = 1.0;
  last_sell_miss_adjust_ = 1.0;
  if (precog_miss_da_enabled_ && precog_miss_da_ != 0) {
    int64_t elapsed_ms = now_fire_t_ - last_precog_miss_t_;
    double decay = std::exp(-double(elapsed_ms) / precog_miss_da_tdc_ms_);
    double effective_da = precog_miss_da_ * decay;
    if (std::abs(effective_da) > 0.01) {
      double adjust = (1.0 - precog_miss_max_adjust_) * std::exp(-std::abs(effective_da)) +
                      precog_miss_max_adjust_;
      if (effective_da > 0) {
        cur_buy_cross_thresh_px_ *= adjust;
        last_buy_miss_adjust_ = adjust;
      } else {
        cur_sell_cross_thresh_px_ *= adjust;
        last_sell_miss_adjust_ = adjust;
      }
    }
  }

  // 9. Floor at 0.
  cur_buy_cross_thresh_px_ = std::max(cur_buy_cross_thresh_px_, 0.0);
  cur_sell_cross_thresh_px_ = std::max(cur_sell_cross_thresh_px_, 0.0);
}

void RelCross::logCrossDebug(PKOrderId pkord_id, Side side, double order_px, double order_qty,
                             double best_bid, double best_ask, double thresh_px, double cur_pos) {
  double local_mid = (best_bid + best_ask) * 0.5;
  double spread_px = best_ask - best_bid;
  double spread_bps = pred_px_ > 0 ? spread_px / pred_px_ * 1e4 : 0;
  double edge_px = 0;
  double order_px_vs_best_px = 0;
  if (side == Side::Buy) {
    edge_px = premium_adjusted_pred_px_ - best_ask - thresh_px;
    order_px_vs_best_px = order_px - best_ask;
  } else {
    edge_px = best_bid - premium_adjusted_pred_px_ - thresh_px;
    order_px_vs_best_px = best_bid - order_px;
  }
  double edge_bps = pred_px_ > 0 ? edge_px / pred_px_ * 1e4 : 0;
  double order_px_vs_best_bps = pred_px_ > 0 ? order_px_vs_best_px / pred_px_ * 1e4 : 0;
  double order_px_vs_best_ticks = min_tick_ > 0 ? order_px_vs_best_px / min_tick_ : 0;
  int64_t local_age_ms = now_fire_t_ - local_sig_->getLastTransactTime();
  int64_t remote_age_ms = now_fire_t_ - remote_sig_->getLastTransactTime();

  LOG(INFO) << fmt::format(
      "({}) RelCrossCrossDebug pk_order_id={} side={} qty={} order_px={} "
      "mode={} pos={} local_bid={} local_ask={} local_mid={} remote_bid={} remote_ask={} "
      "remote_mid={} pred_px={} premium={} premium_ema={} premium_ema_coef={} "
      "premium_adjusted_pred_px={} remote_mom_ems={} remote_mom_adjust_px={} "
      "remote_mom_cap_px={} thresh_px={} base_cross_thresh={} thresh_mult={} "
      "edge_px={} edge_bps={} spread_px={} spread_bps={} order_px_vs_best_px={} "
      "order_px_vs_best_bps={} order_px_vs_best_ticks={} local_sig_age_ms={} "
      "remote_sig_age_ms={} feed_lag_ms={} precog_da={} miss_buy_adj={} miss_sell_adj={} now_ms={}",
      symbol_.get(), pkord_id, side == Side::Buy ? "Buy" : "Sell", order_qty, order_px,
      cross_price_mode_, cur_pos, best_bid, best_ask, local_mid, remote_sig_->getBBMeasure(),
      remote_sig_->getBAMeasure(), remote_sig_val_, pred_px_, cur_premium_, premium_ema_,
      premium_ema_coef_, premium_adjusted_pred_px_, remote_move_ems_, last_remote_mom_adjust_px_,
      last_remote_mom_cap_px_, thresh_px, base_cross_thresh_, effThreshMult(), edge_px, edge_bps,
      spread_px, spread_bps, order_px_vs_best_px, order_px_vs_best_bps, order_px_vs_best_ticks,
      local_age_ms, remote_age_ms, last_feed_lag_ms_, precog_miss_da_,
      last_buy_miss_adjust_, last_sell_miss_adjust_, now_fire_t_);
}

void RelCross::maybeCross(double allowed_buy, double allowed_sell) {
  double best_bid = local_sig_->getBBMeasure();
  double best_ask = local_sig_->getBAMeasure();

  if (best_bid <= 0 || best_ask <= 0) {
    return;
  }

  // Spread-aware entry filter: don't fire if the dislocation minus spread
  // doesn't clear a minimum edge. Only applies to entries (adding to or
  // initiating position), not exits (flattening), so we don't get stuck
  // unable to exit during wide-spread regimes.
  double cur_pos = riskman_->getPos().toDouble();
  if (min_edge_over_spread_bps_ > 0 && pred_px_ > 0) {
    double spread = best_ask - best_bid;
    double local_mid = (best_bid + best_ask) * 0.5;
    double dislocation = std::abs(premium_adjusted_pred_px_ - local_mid);
    double min_edge_px = min_edge_over_spread_bps_ * 1e-4 * pred_px_;
    // Would this trade add to position? Buy when pos >= 0 = entry; sell when pos <= 0 = entry.
    bool would_buy = premium_adjusted_pred_px_ > local_mid;
    bool is_entry = (would_buy && cur_pos >= 0) || (!would_buy && cur_pos <= 0);
    if (is_entry && dislocation - spread < min_edge_px) {
      return;
    }
  }

  double max_pos_d = max_pos_.toDouble();

  // Compute effective exit adjust (possibly decayed by hold time).
  double eff_exit_adjust = exit_adjust_;
  if (hold_decay_tdc_s_ > 0 && last_entry_t_ > 0 && cur_pos != 0) {
    double held_s = (now_fire_t_ - last_entry_t_) / 1000.0;
    double decay = std::exp(-held_s / hold_decay_tdc_s_);
    // Interpolate from exit_adjust_ down to hold_decay_floor_.
    eff_exit_adjust = hold_decay_floor_ + (exit_adjust_ - hold_decay_floor_) * decay;
  }

  // BUY: pred price above best ask + threshold → IOC buy at best ask.
  double buy_thresh = cur_buy_cross_thresh_px_;
  double full_buy_thresh = cur_buy_cross_thresh_px_;
  // Exit adjust: when flattening (short position, buying reduces it), narrow threshold.
  if (cur_pos < 0) {
    buy_thresh *= eff_exit_adjust;
  }

  if (premium_adjusted_pred_px_ - buy_thresh > best_ask) {
    double base_sz = order_size_.toDouble();

    // Does the signal clear the full (non-exit-adjusted) enter threshold?
    bool full_thresh_cleared = premium_adjusted_pred_px_ - full_buy_thresh > best_ask;

    // If we're short and the signal exceeds the full threshold, size to flip
    // through flat to +order_size on the other side.
    if (flip_through_ && cur_pos < 0 && full_thresh_cleared) {
      base_sz = std::abs(cur_pos) + order_size_.toDouble();
    }

    if (size_signal_max_mult_ > 0 && buy_thresh > 0) {
      double signal_mult = (premium_adjusted_pred_px_ - best_ask) / buy_thresh;
      signal_mult = std::max(1.0, std::min(signal_mult, size_signal_max_mult_));
      base_sz *= signal_mult;
    }
    double cross_sz = std::min(base_sz, allowed_buy);
    if (cross_sz <= 0) return;

    // Never overshoot flat on an exit-gated cross. When short, buying reduces
    // the position (gated by the exit-adjusted threshold), so clamp to |cur_pos|
    // -> a pure exit. Only a deliberate flip_through that also clears the full
    // enter threshold may cross through flat into a new position.
    bool exit_clamp = cur_pos < 0 && !(flip_through_ && full_thresh_cleared);
    if (exit_clamp) {
      cross_sz = std::min(cross_sz, std::abs(cur_pos));
    }

    double cross_px_d;
    if (cross_price_mode_ == 1) {
      // Price at pred_px (willing to pay up to fair value).
      cross_px_d = roundToSide(premium_adjusted_pred_px_, false);
    } else if (cross_price_mode_ == 2) {
      // Price at pred_px - threshold (keep some edge).
      cross_px_d = roundToSide(premium_adjusted_pred_px_ - buy_thresh, false);
    } else {
      // Mode 0: cross at best ask.
      cross_px_d = roundToSide(best_ask, false);
    }
    Price cross_px = Price(std::to_string(cross_px_d));

    // Make sure order size meets min notional.
    if (cross_sz * cross_px_d < 11) { // real min notional is 10, but adding buffer
      // An exit clamped below min notional can't be bumped up without
      // overshooting flat, so leave the residual to passive management.
      if (exit_clamp) return;
      LOG(WARNING) << fmt::format(
            "({}) Buy order size below min notional of 10 "
            "(px {} sz {} base_sz {} allowed_buy {}). Sending min size instead.",
            symbol_.get(), cross_px_d, cross_sz, base_sz, allowed_buy);
      cross_sz = 11.0 / cross_px_d;
    }

    Quantity cross_qty = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, cross_sz, true);
    if (cross_qty <= Quantity{0}) return;

    NewOrder ord{symbol_, traded_books_[0], Side::Buy, cross_qty, cross_px,
                 OrderType::Limit, TimeInForce::IOC, false, false};
    PKOrderId pkord_id = riskman_->sendOrd(ord);
    if (pkord_id != -1) {
      riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Cross);
      // logCrossDebug(pkord_id, Side::Buy, cross_px_d, cross_qty.toDouble(), best_bid, best_ask,
      //               buy_thresh, cur_pos);
      last_t_cross_ = now_fire_t_;

      LOG(INFO) << fmt::format(
          "({}) RelCross BUY: px={} qty={} pred={:.4f} ask={:.4f} thresh={:.4f} mode={}",
          symbol_.get(), cross_px_d, cross_sz, premium_adjusted_pred_px_, best_ask, buy_thresh,
          cross_price_mode_);
    }
    return;
  }

  // SELL: pred price below best bid - threshold → IOC sell at best bid.
  double sell_thresh = cur_sell_cross_thresh_px_;
  double full_sell_thresh = cur_sell_cross_thresh_px_;
  // Exit adjust: when flattening (long position, selling reduces it), narrow threshold.
  if (cur_pos > 0) {
    sell_thresh *= eff_exit_adjust;
  }

  if (premium_adjusted_pred_px_ + sell_thresh < best_bid) {
    double base_sz = order_size_.toDouble();

    // Does the signal clear the full (non-exit-adjusted) enter threshold?
    bool full_thresh_cleared = premium_adjusted_pred_px_ + full_sell_thresh < best_bid;

    // If we're long and the signal exceeds the full threshold, size to flip
    // through flat to -order_size on the other side.
    if (flip_through_ && cur_pos > 0 && full_thresh_cleared) {
      base_sz = std::abs(cur_pos) + order_size_.toDouble();
    }

    if (size_signal_max_mult_ > 0 && sell_thresh > 0) {
      double signal_mult = (best_bid - premium_adjusted_pred_px_) / sell_thresh;
      signal_mult = std::max(1.0, std::min(signal_mult, size_signal_max_mult_));
      base_sz *= signal_mult;
    }
    double cross_sz = std::min(base_sz, allowed_sell);
    if (cross_sz <= 0) return;

    // Never overshoot flat on an exit-gated cross. When long, selling reduces
    // the position (gated by the exit-adjusted threshold), so clamp to |cur_pos|
    // -> a pure exit. Only a deliberate flip_through that also clears the full
    // enter threshold may cross through flat into a new position.
    bool exit_clamp = cur_pos > 0 && !(flip_through_ && full_thresh_cleared);
    if (exit_clamp) {
      cross_sz = std::min(cross_sz, std::abs(cur_pos));
    }

    double cross_px_d;
    if (cross_price_mode_ == 1) {
      // Price at pred_px (willing to sell down to fair value).
      cross_px_d = roundToSide(premium_adjusted_pred_px_, true);
    } else if (cross_price_mode_ == 2) {
      // Price at pred_px + threshold (keep some edge).
      cross_px_d = roundToSide(premium_adjusted_pred_px_ + sell_thresh, true);
    } else {
      // Mode 0: cross at best bid.
      cross_px_d = roundToSide(best_bid, true);
    }
    Price cross_px = Price(std::to_string(cross_px_d));

    // Make sure order size meets min notional.
    if (cross_sz * cross_px_d < 11) { // real min notional is 10, but adding buffer
      // An exit clamped below min notional can't be bumped up without
      // overshooting flat, so leave the residual to passive management.
      if (exit_clamp) return;
      LOG(WARNING) << fmt::format(
            "({}) Sell order size below min notional of 10 "
            "(px {} sz {} base_sz {} allowed_sell {}). Sending min size instead.",
            symbol_.get(), cross_px_d, cross_sz, base_sz, allowed_sell);
      cross_sz = 11.0 / cross_px_d;
    }

    Quantity cross_qty = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, cross_sz, true);
    if (cross_qty <= Quantity{0}) return;

    NewOrder ord{symbol_, traded_books_[0], Side::Sell, cross_qty, cross_px,
                 OrderType::Limit, TimeInForce::IOC, false, false};
    PKOrderId pkord_id = riskman_->sendOrd(ord);
    if (pkord_id != -1) {
      riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Cross);
      // logCrossDebug(pkord_id, Side::Sell, cross_px_d, cross_qty.toDouble(), best_bid, best_ask,
      //               sell_thresh, cur_pos);
      last_t_cross_ = now_fire_t_;

      LOG(INFO) << fmt::format(
          "({}) RelCross SELL: px={} qty={} pred={:.4f} bid={:.4f} thresh={:.4f} mode={}",
          symbol_.get(), cross_px_d, cross_sz, premium_adjusted_pred_px_, best_bid, sell_thresh,
          cross_price_mode_);
    }
  }
}

void RelCross::updatePremiumEma() {
  if (pred_px_ == 0 || local_sig_val_ == 0 ||
      !std::isfinite(pred_px_) || !std::isfinite(local_sig_val_)) {
    return;
  }

  cur_premium_ = local_sig_val_ - pred_px_;

  int64_t dt = now_fire_t_ - last_premium_adjust_t_;
  double alpha = 1.0 - std::exp(-dt / premium_tdc_);
  premium_ema_ = alpha * cur_premium_ + (1.0 - alpha) * premium_ema_;

  last_premium_adjust_t_ = now_fire_t_;
}

void RelCross::updatePredMomentumEms(double pred_px) {
  if (pred_momentum_coef_ == 0) return;
  if (pred_px <= 0 || !std::isfinite(pred_px)) return;

  if (prev_pred_px_ == 0) {
    prev_pred_px_ = pred_px;
    last_pred_momentum_update_t_ = now_fire_t_;
    return;
  }

  int64_t dt = now_fire_t_ - last_pred_momentum_update_t_;
  if (dt < 500) return;

  double price_return = (pred_px - prev_pred_px_) / prev_pred_px_;
  price_return = std::max(std::min(price_return, 0.01), -0.01);

  if (dt > 0) {
    double decay = std::exp(-dt / pred_momentum_tdc_ms_);
    pred_momentum_ems_ = price_return + decay * pred_momentum_ems_;
  }

  prev_pred_px_ = pred_px;
  last_pred_momentum_update_t_ = now_fire_t_;
}

void RelCross::applyExecDecays() {
  double impulse_decay =
      std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / curv_impulse_tdc_ms_);
  curv_impulse_ *= impulse_decay;
  last_exec_decay_t_ = time_utils::nowToMs();
}

void RelCross::onFirstFire(double pred_px) {
  needs_first_fire_ = false;
  first_fire_t_ = now_fire_t_;

  if (set_size_from_notional_ && pred_px > 0) {
    double raw_order = tgt_order_notional_ / pred_px;
    double raw_maxpos = tgt_maxpos_notional_ / pred_px;
    order_size_ = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, raw_order, true);
    max_pos_ = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, raw_maxpos, true);
    LOG(INFO) << fmt::format("({}) RelCross notional sizing: pred_px={} order_size={} max_pos={}",
                             symbol_.get(), pred_px, order_size_.toDouble(), max_pos_.toDouble());
  }

  LOG(INFO) << fmt::format("({}) RelCross first fire: pred_px={}", symbol_.get(), pred_px);

  refreshSizeMultBaseFromCurrent();
}

void RelCross::setThresh(double thresh) {
  if (thresh < 0.000005 || thresh > 0.2) {
    LOG(INFO) << fmt::format("({}) Probably sent wrong threshold {}, skipping", symbol_.get(),
                             thresh);
    return;
  }
  base_cross_thresh_ = thresh;
  // Override and reset the usermsg thresh mult to 1. The bleed/minfv slots are
  // left alone: they're defensive state owned by their own writers, and clearing
  // them here would desync us from the riskman's bleed_widened_ flag (it
  // wouldn't re-widen).
  um_thresh_mult_ = 1;
  // Probably not necessary since this is called every tradecall anyway, but no harm.
  updateThreshes();
  LOG(INFO) << fmt::format("({}) Base cross thresh set to {}.", symbol_.get(), thresh);
}

void RelCross::setThreshMult(double thresh_mult, MultSource src) {
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
  // Probably not necessary since this is called every tradecall anyway, but no harm.
  updateThreshes();
  LOG(INFO) << fmt::format("({}) Thresh mult set to {} from {}. um {} bleed {} minfv {} eff {}. "
                           "New effective cross thresh: {}",
                           symbol_.get(), thresh_mult, magic_enum::enum_name(src), um_thresh_mult_,
                           bleed_thresh_mult_, minfv_thresh_mult_, eff_mult,
                           base_cross_thresh_ * eff_mult);
}

void RelCross::setOrderSize(double size) {
  if (size <= 0) {
    LOG(ERROR) << fmt::format("({}) Invalid order size {}, skipping", symbol_.get(), size);
    return;
  }
  LOG(INFO) << fmt::format("({}) setOrderSize {} -> {}", symbol_.get(), order_size_.toDouble(),
                           size);
  order_size_ = Quantity{std::to_string(size)};
  refreshSizeMultBaseFromCurrent();
}

void RelCross::setSizeMult(double size_mult, MultSource src) {
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

  LOG(INFO) << fmt::format(
      "({}) setSizeMult ({} from {}) conf {} um {} dd {} eff {} maxpos {} -> {} ordersize {} -> {}",
      symbol_.get(), size_mult, magic_enum::enum_name(src), conf_size_mult_, um_size_mult_,
      dd_size_mult_, eff_mult,
      max_pos_.toDouble(), new_maxpos.toDouble(), order_size_.toDouble(),
      new_order_size.toDouble());
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
}

Quantity RelCross::getMaxPos() { return max_pos_; }

SymbolId RelCross::getTradedSymbols() { return symbol_; }

std::vector<Market> RelCross::getMarkets() { return traded_books_; }

std::vector<pktrade::BookId> RelCross::getBookIds() {
  std::vector<pktrade::BookId> ids;
  for (pktrade::Market m : traded_books_) {
    ids.push_back({m, symbol_});
  }
  return ids;
}

// Callbacks from risk.
void RelCross::newOrdAck(const Order& ord) {
  if (passive_exit_inflight_ && ord.pk_order_id == passive_exit_order_.pk_oid) {
    passive_exit_live_ = true;
    passive_exit_inflight_ = false;
  }
}

void RelCross::ordUpdate(const Order& ord) {}

void RelCross::ordCancel(const Order& ord) {
  if (passive_exit_live_ && ord.pk_order_id == passive_exit_order_.pk_oid) {
    passive_exit_live_ = false;
  }
}

void RelCross::ordExec(const Order& ord, const OrderExecute& exec) {
  // Decay and update curv_impulse.
  applyExecDecays();
  double xtra_impulse = exec.qty.toDouble() / order_size_.toDouble();
  if (ord.side == Side::Sell) {
    xtra_impulse *= -1;
  }
  curv_impulse_ += xtra_impulse;

  last_fill_t_ = time_utils::nowToMs();

  // Check if this was the passive exit order getting filled.
  if (ord.pk_order_id == passive_exit_order_.pk_oid) {
    if (ord.curr_qty.toDouble() == 0) {
      passive_exit_live_ = false;
      passive_exit_inflight_ = false;
      last_entry_t_ = 0;  // Flat, reset hold timer.
    }
    return;
  }

  // Track entry time for hold decay.
  double pos_after = riskman_->getPos().toDouble();
  if (pos_after == 0) {
    last_entry_t_ = 0;
  } else if (last_entry_t_ == 0) {
    last_entry_t_ = time_utils::nowToMs();
  }

  // After a crossing fill, update the passive exit.
  if (passive_exit_enabled_ && riskman_->getPos().toDouble() != 0) {
    if (passive_exit_live_) {
      // Cancel current — the refresh cycle or next fill will re-place.
      cancelPassiveExit();
    }
    // Schedule a placement if nothing is already pending.
    if (!passive_exit_place_scheduled_ && !passive_exit_inflight_) {
      passive_exit_place_scheduled_ = true;
      pktrade::GlobalVar::event_loop_->onTimeout(
          passive_exit_delay_, [&] { this->placePassiveExit(); });
    }
  }
}

void RelCross::ordElim(const Order& ord, const OrderElimination& elim) {
  if (ord.pk_order_id == passive_exit_order_.pk_oid) {
    passive_exit_live_ = false;
    passive_exit_inflight_ = false;
  }
}

void RelCross::ordElimPrecog(Side side, Quantity qty) {
  if (!precog_miss_da_enabled_) return;

  // Decay any existing impulse before adding the new one. Smooth decay
  // (no hard reset) — a miss far enough in the past contributes ~0.
  int64_t now = time_utils::nowToMs();
  if (precog_miss_da_ != 0 && last_precog_miss_t_ > 0) {
    int64_t elapsed_ms = now - last_precog_miss_t_;
    double decay = std::exp(-double(elapsed_ms) / precog_miss_da_tdc_ms_);
    precog_miss_da_ *= decay;
  }

  // Add signed impulse: +1 unit (normalized by per_unit_) per qty/order_size
  // of missed buys, −1 unit for missed sells.
  double order_size_d = order_size_.toDouble();
  if (order_size_d <= 0 || precog_miss_da_per_unit_ <= 0) return;
  double impulse = qty.toDouble() / order_size_d / precog_miss_da_per_unit_;
  if (side == Side::Sell) impulse = -impulse;
  precog_miss_da_ += impulse;
  last_precog_miss_t_ = now;

  // Fresh information — try to fire at the adjusted threshold.
  tryFire();
}

void RelCross::ordReject(const Order& ord, const NewOrderReject& rej) {
  LOG(WARNING) << fmt::format("({}) RelCross order rejected: {}", symbol_.get(), rej.reason);
  if (ord.pk_order_id == passive_exit_order_.pk_oid) {
    passive_exit_live_ = false;
    passive_exit_inflight_ = false;
  }
}

void RelCross::cancelOutstandingOrds() {
  cancelPassiveExit();
}

void RelCross::placePassiveExit() {
  passive_exit_place_scheduled_ = false;
  if (passive_exit_inflight_) {
    return;
  }
  if (passive_exit_live_) {
    // Still live (cancel ack hasn't arrived). Retry after 2s.
    cancelPassiveExit();
    passive_exit_place_scheduled_ = true;
    pktrade::GlobalVar::event_loop_->onTimeout(
        std::chrono::seconds(2), [&] { this->placePassiveExit(); });
    return;
  }

  Quantity pos = riskman_->getPos();
  if (pos == Quantity{0}) {
    return;
  }

  // Compute decayed profit target.
  double elapsed_min = (time_utils::nowToMs() - last_fill_t_) / (60.0 * 1000);
  double cur_pct = passive_exit_pct_ - passive_exit_decay_per_min_ * elapsed_min;
  cur_pct = std::max(cur_pct, passive_exit_min_pct_);

  // Schedule a refresh (only if one isn't already pending).
  if (!passive_exit_refresh_scheduled_) {
    passive_exit_refresh_scheduled_ = true;
    pktrade::GlobalVar::event_loop_->onTimeout(
        passive_exit_refresh_t_, [&] {
          this->passive_exit_refresh_scheduled_ = false;
          this->refreshPassiveExit();
        });
  }

  Quantity qty;
  Price px;
  Side side;

  if (pos > Quantity{0}) {
    side = Side::Sell;
    qty = pos;
    double nearest_px;
    if (passive_exit_style_ == 0) {
      nearest_px = riskman_->getAvgExecPx() * (1 + cur_pct);
    } else if (passive_exit_style_ == 2) {
      nearest_px = local_sig_val_ * (1 + cur_pct);
    } else {
      nearest_px = remote_sig_val_ * (1 + cur_pct);
    }
    px = Price(std::to_string(roundToSide(nearest_px, true)));
  } else {
    side = Side::Buy;
    qty = -pos;
    double nearest_px;
    if (passive_exit_style_ == 0) {
      nearest_px = riskman_->getAvgExecPx() * (1 - cur_pct);
    } else if (passive_exit_style_ == 2) {
      nearest_px = local_sig_val_ * (1 - cur_pct);
    } else {
      nearest_px = remote_sig_val_ * (1 - cur_pct);
    }
    px = Price(std::to_string(roundToSide(nearest_px, false)));
  }

  NewOrder ord{symbol_, traded_books_[0], side, qty, px,
               OrderType::Limit, TimeInForce::ALO, false, true};
  PKOrderId pkord_id = riskman_->sendOrd(ord);
  if (pkord_id != -1) {
    riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Cross);

    passive_exit_order_.pk_oid = pkord_id;
    passive_exit_order_.side = side;
    passive_exit_order_.px = px;
    passive_exit_order_.place_time = time_utils::nowToMs();
    passive_exit_order_.cancel_time = 0;

    passive_exit_inflight_ = true;

    LOG(INFO) << fmt::format("({}) RelCross passive exit placed: side={} px={} qty={} pct={:.4f}",
                             symbol_.get(), side == Side::Buy ? "Buy" : "Sell", px.toDouble(),
                             qty.toDouble(), cur_pct);
  }
}

void RelCross::cancelPassiveExit() {
  if (passive_exit_live_ && passive_exit_order_.cancel_time == 0) {
    CancelOrder cancelOrd{passive_exit_order_.pk_oid};
    riskman_->cancelOrd(cancelOrd);
    passive_exit_order_.cancel_time = time_utils::nowToMs();
  }
}

void RelCross::refreshPassiveExit() {
  if (passive_exit_live_) {
    cancelPassiveExit();
    if (!passive_exit_place_scheduled_) {
      passive_exit_place_scheduled_ = true;
      pktrade::GlobalVar::event_loop_->onTimeout(
          passive_exit_delay_, [&] { this->placePassiveExit(); });
    }
  }
}

void RelCross::onSignalValue(int sig_id, double value) {
  if (sig_id == remote_sig_idx_) {
    if (value != 0) {
      // Update remote momentum EMS.
      if (remote_mom_coef_ != 0 && prev_remote_val_ > 0) {
        int64_t now = time_utils::nowToMs();
        int64_t dt = now - last_remote_mom_t_;
        if (dt > 0) {
          double move = value - prev_remote_val_;
          double decay = std::exp(-dt / remote_mom_tdc_ms_);
          remote_move_ems_ = move + decay * remote_move_ems_;
          last_remote_mom_t_ = now;
        }
      }
      // Update SNR noise estimate: EMS of absolute remote returns.
      // This is the denominator of the SNR ratio — how much the remote
      // normally jitters, so we can tell real moves from noise.
      if (snr_thresh_coef_ > 0 && prev_remote_val_ > 0) {
        int64_t now = time_utils::nowToMs();
        double abs_ret = std::abs((value - prev_remote_val_) / prev_remote_val_);
        if (last_snr_vol_t_ > 0) {
          int64_t dt = now - last_snr_vol_t_;
          double decay = std::exp(-dt / snr_vol_tdc_ms_);
          snr_vol_ems_ = abs_ret + decay * snr_vol_ems_;
        } else {
          snr_vol_ems_ = abs_ret;
        }
        last_snr_vol_t_ = now;
      }

      prev_remote_val_ = value;
      if (last_remote_mom_t_ == 0) last_remote_mom_t_ = time_utils::nowToMs();

      remote_sig_val_ = value;
      vol_mechs_.onRemoteUpdate(value);
    }
  }

  if (sig_id == local_sig_idx_) {
    local_sig_val_ = value;
  }

  applyExecDecays();
}

void RelCross::onTrade(const LevelBook& bk, const Trade& trd) {
  // HL feed lag heartbeat: any HL trade refreshes our notion of server-side
  // delay. Used by updateThreshes to widen during congestion bursts.
  if (trd.transact_t > 0) {
    last_feed_lag_ms_ =
        std::max<int64_t>(0, time_utils::nowToMs() - trd.transact_t);
  }

  // The rest only applies to our own symbol's trades. BTC heartbeat trades
  // skip the book-update and trade-rate paths.
  if (bk.getSymbol().get() != symbol_.get()) {
    return;
  }

  vol_mechs_.onBookUpdate(bk);

  // Update trade rate EMS: each trade adds 1.0, decaying over time.
  if (trade_rate_widen_coef_ > 0) {
    int64_t now = time_utils::nowToMs();
    if (last_trade_rate_t_ > 0) {
      int64_t dt = now - last_trade_rate_t_;
      double decay = std::exp(-dt / trade_rate_tdc_ms_);
      trade_rate_ems_ = 1.0 + decay * trade_rate_ems_;
      double baseline_decay = std::exp(-dt / trade_rate_baseline_tdc_ms_);
      trade_rate_baseline_ = 1.0 + baseline_decay * trade_rate_baseline_;
    } else {
      trade_rate_ems_ = 1.0;
      trade_rate_baseline_ = 1.0;
    }
    last_trade_rate_t_ = now;
  }
}

void RelCross::refreshSizeMultBaseFromCurrent() {
  double denom = effSizeMult();
  if (denom <= 0) {
    denom = 1;
  }
  base_max_pos_ = max_pos_.toDouble() / denom;
  base_order_size_ = order_size_.toDouble() / denom;
  base_tgt_order_notional_ = tgt_order_notional_ / denom;
  base_tgt_maxpos_notional_ = tgt_maxpos_notional_ / denom;
  has_base_sizes_ = true;
}

} // namespace pktrade::ordex
