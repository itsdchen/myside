#include "trade_risk_man.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/context/live_context.h"

#include "pktrade/oeapi.h"

#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include <rapidjson/document.h>

namespace pktrade::risk {

std::string_view to_str(PlaceReason reason) {
  switch (reason) {
    case PlaceReason::Unset:
      return "Unset";
    case PlaceReason::Front:
      return "Front";
    case PlaceReason::Back:
      return "Back";
    case PlaceReason::Cross:
      return "Cross";
    default:
      return "Unknown";
  }
};

TradeRiskMan::TradeRiskMan(std::string symbol_id, Market mkt, const rapidjson::Value& risk_conf,
                           const rapidjson::Value& comms_conf, pktrade::signals::SignalFactory* sf,
                           pktrade::ordex::Ordex* ordex)
    : book_id_{mkt, symbol_id}, symbol_id_(symbol_id), mkt_(mkt), ordex_(ordex),
      global_positioner_(this) {
  // Set the commission.
  // Ideally, read this from config.

  // Get this from the risk conf.
  // Iterate through the commissions tiers and set them.
  bool use_promo = false;
  if (comms_conf.HasMember("use_promo")) {
    use_promo = comms_conf["use_promo"].GetBool();
  }

  std::string promo_tier = "";
  if (comms_conf.HasMember("promo_tier")) {
    promo_tier = comms_conf["promo_tier"].GetString();
  }

  // TODO: make this happen.
  // Iterate through the comms dir and set tiers.
  for (const auto& tierMapping : comms_conf["tiers"].GetObject()) {
    Market mkt = magic_enum::enum_cast<Market>(tierMapping.name.GetString()).value();
    std::string tier(tierMapping.value.GetString());

    commissioner_.setTier(mkt, tier, use_promo, promo_tier);
  }

  // max_pos and max_notional should both be respected.
  max_pos_ = Quantity{risk_conf["max_position"].GetString()};
  max_orders_ = risk_conf["max_orders"].GetInt();

  // This would require updating a whole bunch of tingz.
  // OK, provide a default since this is causing a bunch of errors.
  max_notional_ = 100000;
  if (risk_conf.HasMember("max_notional")) {
    max_notional_ = risk_conf["max_notional"].GetDouble();
  }

  // If it exists.
  if (risk_conf.HasMember("shortable_amount")) {
    shortable_amt_ = Quantity{risk_conf["shortable_amount"].GetString()};
  } else {
    shortable_amt_ = max_pos_;
  }

  position_ = Quantity{"0"};
  outstanding_buy_ = Quantity{"0"};
  outstanding_sell_ = Quantity{"0"};

  outstanding_buy_not_ = 0;
  outstanding_sell_not_ = 0;
  outstanding_buy_cx_ = Quantity{"0"};
  outstanding_sell_cx_ = Quantity{"0"};

  // Check if we're in liquidating mode. If so, then we actually want to
  // initialize it with a position.
  if (risk_conf.HasMember("hardcoded_position")) {
    // Then we are in liquidating mode.
    std::cout << "TradeRiskMan initialized with hardcoded_position "
              << risk_conf["hardcoded_position"].GetString() << " : liquidating." << std::endl;
    position_ = Quantity{risk_conf["hardcoded_position"].GetString()};
  }

  tot_sent_ = Quantity{"0"};
  tot_traded_ = Quantity{"0"};
  tot_precog_missed_ = 0;

  tot_notional_traded_ = 0;
  times_traded_ = 0;
  pos_dir_strict_ = 0;
  times_flipped_ = 0;

  ioc_shs_sent_ = 0;
  rmliq_shs_ = 0;

  min_pnl_seen_ = 0;

  base_currency_ =
      magic_enum::enum_cast<BaseCurrency>(risk_conf["base_currency"].GetString()).value();

  commissions_ = 0;

  // **********************************************
  // Start Funding-related fields.
  use_funding_ = false;
  net_funding_ = 0;
  last_funding_t_ = 0;
  next_funding_t_ = 0;
  started_funding_calcs_ = false;

  // IF we use_funding, then on the first exec we prep ourselves for
  // the next funding time.

  // End Funding-related fields
  // **********************************************
  min_pnl_ = risk_conf["min_pnl"].GetDouble();
  min_pnl_incr_ = min_pnl_;

  num_fv_ = 0;
  fv_limit_ = risk_conf["fv_limit"].GetInt();

  running_notional_basecur_ = 0;
  v2_closed_pnl_ = 0;
  avg_entry_px_ = 0;
  tot_entry_notional_ = 0;
  v3_closed_pnl_ = 0;
  highest_closed_pnl_ = 0;

  // Probably grab this from config at some point.
  timeout_minfv_ = risk_conf["timeout_minfv"].GetInt();

  // Initialize in TL5. Sets can_trade_pk and can_increase_risk to false.
  for (const auto reason : magic_enum::enum_values<TLReason>()) {
    reason_to_tl_[reason] = TradingLevel::TL0;
  }
  cur_tl_ = TradingLevel::TL0;
  setTL(TradingLevel::TL5, TLReason::Normal);

  minfv_t_ = time_utils::nowToMs();


  use_funding_ = false;
  if (risk_conf.HasMember("use_funding")) {
    use_funding_ = risk_conf["use_funding"].GetBool();
  }

  live_context_ = nullptr;
  local_context_ = nullptr;

  precog_miss_ = false;
  precog_fastest_possible_rtt_ = 1000;
  if (risk_conf.HasMember("precog_miss")) {
    precog_miss_ = risk_conf["precog_miss"].GetBool();
  }
  if (risk_conf.HasMember("precog_fastest_possible_rtt")) {
    precog_fastest_possible_rtt_ =
        int64_t(1000 * risk_conf["precog_fastest_possible_rtt"].GetDouble());
  }

  precog_penalty_ratio_ = 0;
  if (risk_conf.HasMember("precog_penalty_ratio")) {
    precog_penalty_ratio_ = risk_conf["precog_penalty_ratio"].GetDouble();
  }

  outstanding_buy_premisses_ = Quantity{0};
  outstanding_sell_premisses_ = Quantity{0};

  num_buy_premisses_ = 0;
  num_sell_premisses_ = 0;

  LOG(INFO) << " Risk:  with precog_misses," << precog_miss_ << " and rtt is "
            << precog_fastest_possible_rtt_ << "\n";

  if (risk_conf.HasMember("global_inherit")) {
    do_global_inherit_ = risk_conf["global_inherit"].GetBool();
    if (do_global_inherit_) {
      LOG(INFO) << fmt::format("({}) Participating in global inherit.", symbol_id_.get());
    } else {
      LOG(INFO) << fmt::format("({}) Not participating in global inherit.", symbol_id_.get());
    }
  }

  if (risk_conf.HasMember("bleed_detector")) {
    use_bleed_detector_ = true;
    const auto& bd = risk_conf["bleed_detector"];
    bleed_tdc_ = bd["tdc"].GetDouble();
    bleed_deadzone_thresh_ = bd["deadzone_thresh"].GetDouble();
    bleed_score_widen_ = bd["bleed_score_widen"].GetDouble();
    bleed_score_stop_ = bd["bleed_score_stop"].GetDouble();
    bleed_widen_thresh_mult_ = bd["bleed_widen_thresh_mult"].GetDouble();
    if (bd.HasMember("bleed_stop_timeout")) {
      bleed_stop_timeout_s_ = int64_t(bd["bleed_stop_timeout"].GetDouble());
    }
    bleed_max_ = 1.0 / (1.0 - std::exp(-1.0 / bleed_tdc_));
    LOG(INFO) << fmt::format(
        "({}) Bleed detector enabled: tdc={} deadzone={} score_widen={} score_stop={} "
        "thresh_mult={} stop_timeout={}s",
        symbol_id_.get(), bleed_tdc_, bleed_deadzone_thresh_, bleed_score_widen_, bleed_score_stop_,
        bleed_widen_thresh_mult_, bleed_stop_timeout_s_);
  }

  if (risk_conf.HasMember("double_down")) {
    use_double_down_ = true;
    const auto& dd = risk_conf["double_down"];
    dd_enter_edge_bps_ = dd["enter_edge_bps"].GetDouble();
    if (dd.HasMember("exit_edge_bps")) {
      throw std::runtime_error(fmt::format(
          "({}) double_down: 'exit_edge_bps' was renamed to 'exit_edge_frac' "
          "(exit threshold as a fraction of enter_edge_bps)",
          symbol_id_.get()));
    }
    if (dd.HasMember("exit_edge_frac")) {
      dd_exit_edge_frac_ = dd["exit_edge_frac"].GetDouble();
    }
    if (dd.HasMember("long_tdc_s")) {
      dd_long_tdc_ms_ = dd["long_tdc_s"].GetDouble() * 1000;
    }
    if (dd.HasMember("short_tdc_s")) {
      dd_short_tdc_ms_ = dd["short_tdc_s"].GetDouble() * 1000;
    }
    if (dd.HasMember("step_mult")) {
      dd_step_mult_ = dd["step_mult"].GetDouble();
    }
    if (dd.HasMember("max_mult")) {
      dd_max_mult_ = dd["max_mult"].GetDouble();
    }
    if (dd.HasMember("dwell_s")) {
      dd_dwell_ms_ = int64_t(dd["dwell_s"].GetDouble() * 1000);
    }
    if (dd.HasMember("warmup_closes")) {
      dd_warmup_closes_ = dd["warmup_closes"].GetInt();
    }
    if (dd.HasMember("warmup_s")) {
      dd_warmup_ms_ = int64_t(dd["warmup_s"].GetDouble() * 1000);
    }
    if (dd.HasMember("min_churn_ratio")) {
      dd_min_churn_ratio_ = dd["min_churn_ratio"].GetDouble();
    }
    if (dd.HasMember("min_closed_frac")) {
      dd_min_closed_frac_ = dd["min_closed_frac"].GetDouble();
    }
    if (dd.HasMember("max_mkt_frac")) {
      dd_max_mkt_frac_ = dd["max_mkt_frac"].GetDouble();
    }
    if (dd.HasMember("min_hold_ntl_frac")) {
      dd_min_hold_ntl_frac_ = dd["min_hold_ntl_frac"].GetDouble();
    }
    if (dd.HasMember("eval_period_s")) {
      dd_eval_period_s_ = int64_t(dd["eval_period_s"].GetDouble());
    }
    if (dd.HasMember("revert_on_short_negative")) {
      dd_revert_on_short_negative_ = dd["revert_on_short_negative"].GetBool();
    }

    // Exit threshold is a fraction of enter (hysteresis auto-scales with enter).
    dd_exit_edge_bps_ = dd_exit_edge_frac_ * dd_enter_edge_bps_;

    if (use_bleed_detector_) {
      throw std::runtime_error(fmt::format(
          "({}) double_down and bleed_detector can't both be enabled", symbol_id_.get()));
    }
    if (dd_exit_edge_frac_ < 0.0 || dd_exit_edge_frac_ >= 1.0 || dd_step_mult_ <= 1.0 ||
        dd_max_mult_ < dd_step_mult_ || dd_long_tdc_ms_ <= 0 || dd_short_tdc_ms_ <= 0 ||
        dd_eval_period_s_ <= 0) {
      throw std::runtime_error(fmt::format(
          "({}) double_down config invalid: need 0 <= exit_edge_frac < 1, "
          "step_mult > 1, max_mult >= step_mult, positive tdcs and eval_period",
          symbol_id_.get()));
    }

    LOG(INFO) << fmt::format(
        "({}) Double-down enabled: enter={}bps exit_frac={}(={:.2f}bps) long_tdc={}s "
        "short_tdc={}s step={} max={} dwell={}s warmup={}closes/{}s churn>={} closed_frac>={} "
        "mkt_frac<={} hold_ntl_frac>={} eval={}s revert_on_short_neg={}",
        symbol_id_.get(), dd_enter_edge_bps_, dd_exit_edge_frac_, dd_exit_edge_bps_,
        dd_long_tdc_ms_ / 1000, dd_short_tdc_ms_ / 1000, dd_step_mult_, dd_max_mult_,
        dd_dwell_ms_ / 1000, dd_warmup_closes_, dd_warmup_ms_ / 1000, dd_min_churn_ratio_,
        dd_min_closed_frac_, dd_max_mkt_frac_, dd_min_hold_ntl_frac_, dd_eval_period_s_,
        dd_revert_on_short_negative_);
  }

  refreshSizeMultBaseFromCurrent();
}

void TradeRiskMan::registerPred(pktrade::signals::Signal* sig) { pred_sig_ = sig; }

// Notion of the mid px.
void TradeRiskMan::registerMid(pktrade::signals::Signal* sig) { mid_sig_ = sig; }

void TradeRiskMan::subscribe() {
  if (precog_miss_) {
    // Then potentially subscribe.
    for (const pktrade::BookId& book_id : ordex_->getBookIds()) {
      std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::LvlMod,
                                                         pktrade::md::BeaconCBType::LvlDel};

      pktrade::GlobalVar::md_beacon_->addListener(book_id, this, cb_types);
    }
  }

  // Double-down's market-presence gate needs the market trade feed.
  if (use_double_down_ && dd_max_mkt_frac_ > 0) {
    for (const pktrade::BookId& book_id : ordex_->getBookIds()) {
      std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Trd};
      pktrade::GlobalVar::md_beacon_->addListener(book_id, this, cb_types);
    }
  }
}

void TradeRiskMan::onDayStart() {
  // We can start trading.
  setTL(TradingLevel::TL0, TLReason::Normal);

  // Double-down needs a periodic eval so decay-driven step-downs happen even
  // when fills stop arriving.
  if (use_double_down_ && !dd_timer_started_) {
    dd_timer_started_ = true;
    doubleDownTimerLoop();
  }

  // Start the global_positioner if needed.
  // Don't participate in this if in sim or paper mode. We could mess up the real live guys.
  if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
    global_positioner_.onDayStart();
  }
}

// Let's not support order modifies for now.
// Executor interface.
PKOrderId TradeRiskMan::sendOrd(const NewOrder& ord) {
  // These should never happen.
  if (!can_trade_pk_) {
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendOrd when can_trade_pk is false!",
                              ord.symbol.get());
    return -1;
  } else if (cur_tl_ == TradingLevel::TL5) {
    // TODO: This needs to change once we have crossers, since they can trade in TL5.
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendOrd when in TL5!", ord.symbol.get());
    return -1;
  } else if (cur_tl_ == TradingLevel::TL6 && ord.time_in_force != TimeInForce::IOC) {
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendOrd a non-IOC in TL6!", ord.symbol.get());
    return -1;
  } else if (cur_tl_ == TradingLevel::TL7) {
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendOrd in TL7!", ord.symbol.get());
    return -1;
  }

  // Make sure order size meets min notional.
  if (ord.qty.toDouble() * ord.px.toDouble() <= 10.0 + pktrade::EPS) {
    // Prevent orders going out that would be rejected by HL anyway.
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendOrd at notional less than 10 ({} @ {}).",
                              ord.symbol.get(), ord.qty.toDouble(), ord.px.toDouble());
    return -1;
  }

  // Make sure the BookId matches and order price isn't wildly far from the BookId's mid.
  BookId bid(ord.mkt, ord.symbol);
  double ord_px = ord.px.toDouble();
  if (bid != book_id_) {
    // Ordex sent order for wrong BookId.
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendOrd for wrong symbol (TRM symbol {})",
                              ord.symbol.get(), symbol_id_.get());
    setTL(TradingLevel::TL5, TLReason::Misconfig);
    if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
      pktrade::util::Mailer::instance().send_mail(
          fmt::format("Misconfigured order for {}", ord.symbol.get()),
          fmt::format("{}:{} TL5'ed.\nTRM symbol: {}",
                      pktrade::GlobalVar::strat_id_, ord.symbol.get(), symbol_id_.get()),
          60);
    }
    return -1;
  }
  if (approx_mid_ > 0 && std::abs(ord_px / approx_mid_ - 1) > 0.5) {
    // Ordex sent order with probably wrong price.
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendOrd with bad price {} (approx_mid {})",
                              ord.symbol.get(), ord_px, approx_mid_);
    setTL(TradingLevel::TL5, TLReason::Misconfig);
    if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
      pktrade::util::Mailer::instance().send_mail(
          fmt::format("Mispriced order for {}", ord.symbol.get()),
          fmt::format("{}:{} TL5'ed.\nOrder price {} too far from approx_mid {}",
                      pktrade::GlobalVar::strat_id_, ord.symbol.get(), ord_px, approx_mid_),
          60);
    }
    return -1;
  }

  PKOrderId oid;
  if (pktrade::GlobalVar::live_) {
    if (!pktrade::GlobalVar::paper_trading_mode_) {
      if (local_context_ != nullptr) {
        oid = local_context_->onNewOrd(ord);
      } else if (live_context_ != nullptr) {
        oid = live_context_->onNewOrd(ord);
      } else {
        LOG(ERROR) << fmt::format("({}) TradeRiskMan: cancelOrd: no context set.",
                                  ord.symbol.get());
      }
    } else {
      // Otherwise, sim_verse.
      oid = pktrade::GlobalVar::sim_verse_->onNewOrder(ord);
    }
  } else {
    // Otherwise, sim_verse.
    oid = pktrade::GlobalVar::sim_verse_->onNewOrder(ord);
  }
  live_order_ids_.insert(oid);

  const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(bid);
  // TODO: this can be better.
  double bb = bk->side<BuySide>().begin()->px.toDouble();
  double ba = bk->side<SellSide>().begin()->px.toDouble();

  // Use this to update approx_mid for next time, in case the price drifts. Note that this is the
  // mid from looking up the book for this BookId (bid == book_id_), not just mid_sig_. Also note
  // that this only gets updated after a successful send, so if the price moves 50% between sends
  // this can cause a TL5. But that seems more likely to be a bug than a real situation anyway.
  if (0 < bb && bb < ba) {
    approx_mid_ = (bb + ba) * 0.5;
  }

  tot_sent_ += ord.qty;
  if (ord.time_in_force == TimeInForce::IOC) {
    ioc_shs_sent_ += ord.qty.toDouble();
  }

  open_ord_annotations_.emplace(
      oid, OrdAnnot{bid, oid, bb, ba, pred_sig_ == nullptr ? 0 : pred_sig_->getValue(),
                    mid_sig_->getValue(), time_utils::nowToMs(),
                    pktrade::GlobalVar::book_manager_->getLastTickerUpdateId(bid), 0,
                    ord.time_in_force == TimeInForce::IOC, false, PlaceReason::Unset});

  if (ord.side == Side::Buy) {
    outstanding_buy_ += ord.qty;
    outstanding_buy_not_ += ord.qty.toDouble() * ord_px;
    // Sanity check?
    if (ord.time_in_force != TimeInForce::GTC && ord.time_in_force != TimeInForce::ALO) {
      outstanding_buy_cx_ += ord.qty;
    }
  } else {
    outstanding_sell_ += ord.qty;
    outstanding_sell_not_ += ord.qty.toDouble() * ord_px;
    if (ord.time_in_force != TimeInForce::GTC && ord.time_in_force != TimeInForce::ALO) {
      outstanding_sell_cx_ += ord.qty;
    }
  }

  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // LOG(INFO) << " WRITING NEWORD OUT\n";

    // What's the last updateid?
    int64_t last_uid = pktrade::GlobalVar::book_manager_->getLastTickerUpdateId(bid);

    double bq = bk->side<BuySide>().begin()->qty.toDouble();
    double aq = bk->side<SellSide>().begin()->qty.toDouble();

    bool isInside = ord_px == bb || ord_px == ba;

    // Write stuff out.
    // time,symbol,market,side,qty,px,triggerid,inside,addrm
    *pktrade::GlobalVar::orders_out_
        << time_utils::nowToStr() << "," << time_utils::nowToMs() << ",NewOrd," << oid << ","
        << symbol_id_.get() << "," << magic_enum::enum_name(ord.mkt) << ","
        << (ord.side == Side::Buy ? "Buy" : "Sell") << "," << ord_px << ","
        << ord.qty.toDouble() << ",triggerID:" << last_uid << "," << aq << "@" << ba << "-" << bq
        << "@" << bb << "," << (isInside ? "inside" : "backlevel") << ","
        << ((ord.time_in_force == TimeInForce::GTC || ord.time_in_force == TimeInForce::ALO)
                ? "ADD"
                : "REM")
        << std::endl;
  }

  // Store records for precog misses.
  if (precog_miss_ && ord.time_in_force == TimeInForce::IOC) {
    if (ord.side == Side::Buy) {
      precogged_buys_.insert(
          {oid, {.pk_order_id = oid, .t_sent = time_utils::nowToMs(), .px = ord.px, .qty = ord.qty}

          });
    } else if (ord.side == Side::Sell) {
      precogged_sells_.insert(
          {oid, {.pk_order_id = oid, .t_sent = time_utils::nowToMs(), .px = ord.px, .qty = ord.qty}

          });
    }
  }

  // Start the cleanup process if not started.
  if (!started_clean_iocs_) {
    LOG(INFO) << fmt::format("({}) Starting IOC cleanup loop", symbol_id_.get());
    started_clean_iocs_ = true;
    cleanOldIOCs();
  }

  return oid;
}

PKOrderId TradeRiskMan::sendSplit(const SplitOutcome& split) {
  // HIP-4 collateral action. Cheap sanity checks only: the strategy owns account state and is
  // responsible for balance-aware sizing (min_free_quote, complete-sets target, etc.).
  if (!can_trade_pk_) {
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendSplit when can_trade_pk is false!",
                              symbol_id_.get());
    return -1;
  }
  if (split.amount.toDouble() <= 0) {
    LOG(ERROR) << fmt::format("({}) Ordex trying to sendSplit with non-positive amount {}",
                              symbol_id_.get(), split.amount.toDouble());
    return -1;
  }

  // Split is a Hyperliquid-only L1 action executed by the live wsgateway. There is no on-chain
  // collateral in sim / paper mode and the local (Binance) gateway does not support it, so skip.
  if (!pktrade::GlobalVar::live_ || pktrade::GlobalVar::paper_trading_mode_) {
    LOG(INFO) << fmt::format("({}) sendSplit skipped (not in live non-paper mode): {}",
                             symbol_id_.get(), split.to_string());
    return -1;
  }
  if (live_context_ == nullptr) {
    LOG(ERROR) << fmt::format("({}) TradeRiskMan: sendSplit: no live context set.",
                              symbol_id_.get());
    return -1;
  }

  LOG(INFO) << fmt::format("({}) sendSplit: {}", symbol_id_.get(), split.to_string());
  return live_context_->onSplitOutcome(split, symbol_id_);
}

void TradeRiskMan::onSplitOE(const SplitOutcomeAck& ack) {
  LOG(INFO) << fmt::format("({}) split ack id {} (t {})", symbol_id_.get(), ack.pk_order_id,
                           ack.exch_transact_time);
  if (ordex_ != nullptr) {
    ordex_->splitAck(ack);
  }
}

void TradeRiskMan::onSplitOE(const SplitOutcomeReject& rej) {
  LOG(ERROR) << fmt::format("({}) split reject id {}: {}", symbol_id_.get(), rej.pk_order_id,
                            rej.reason);
  if (ordex_ != nullptr) {
    ordex_->splitReject(rej);
  }
}

// Send this cancel to the simulator.
void TradeRiskMan::cancelOrd(const CancelOrder& cxl) {
  // Update the annotation too.
  // Do a quick check if this exists or is problematic in some way.
  if (!open_ord_annotations_.contains(cxl.pk_order_id)) {
    LOG(ERROR) << fmt::format(
        "({}) TradeRiskMan: cancelOrd: order not found in open_ord_annotations_ {}",
        symbol_id_.get(), cxl.pk_order_id);
    return;
  }

  // LOG(INFO) << "TradeRiskMan: cancelOrd: " << cxl.pk_order_id << "\n";
  if (pktrade::GlobalVar::live_) {
    if (!pktrade::GlobalVar::paper_trading_mode_) {
      if (local_context_ != nullptr) {
        local_context_->onCancelOrd(cxl);
      } else if (live_context_ != nullptr) {
        live_context_->onCancelOrd(cxl);
      } else {
        LOG(ERROR) << fmt::format("({}) TradeRiskMan: cancelOrd: no context set.",
                                  symbol_id_.get());
      }
    } else {
      // Otherwise, sim_verse.
      pktrade::GlobalVar::sim_verse_->onCancelOrd(cxl);
    }
  } else {
    // Otherwise, sim_verse.
    pktrade::GlobalVar::sim_verse_->onCancelOrd(cxl);
  }

  // Record that we sent the cancels.
  // But do not update bookkeeping until we get the cancel ack back from the
  // mkt.

  OrdAnnot& annot = open_ord_annotations_[cxl.pk_order_id];
  annot.cancel_t_ = time_utils::nowToMs();

  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // LOG(INFO) << " WRITING NEWORD OUT\n";

    /*
    const LevelBook* bk =
        pktrade::GlobalVar::book_manager_->find(annot.book_id);

    double bb = bk->side<BuySide>().begin()->px.toDouble();
    double ba = bk->side<SellSide>().begin()->px.toDouble();
    double bq = bk->side<BuySide>().begin()->qty.toDouble();
    double aq = bk->side<SellSide>().begin()->qty.toDouble();
  */
    // For now let's not do the lookup. Maybe eventually?

    // Write stuff out.
    // time,symbol,market,side,qty,px
    *pktrade::GlobalVar::orders_out_ << time_utils::nowToStr() << "," << time_utils::nowToMs()
                                     << ",Cancel," << cxl.pk_order_id << "," << symbol_id_.get()
                                     << "," << magic_enum::enum_name(annot.book_id.first)
                                     << std::endl;
    /*<< "," << aq << "@" << ba
    << "-" << bq << "@" << bb */
  }
}

void TradeRiskMan::modOrd(const ModifyOrder& mod) {
  // Right now, don't do anything.
  throw std::runtime_error("ModifyOrder is not implemented yet.");
}

// OEListener interface callbacks.
void TradeRiskMan::onOE(const Order& ord, const GatewayAck& ack) {
  // I guess if the gateway rejects, then we should do something.
  // Specifically in the NewOrder case.

  // No action necessary if not rejected.
  if (!ack.rejected) {
    // LOG(INFO) << " ack but not rejected, returning " << "\n";
    return;
  }

  // If we had sent an order and it was rejected,
  // stop tracking it in our outstanding orders.
  if (std::holds_alternative<NewOrder>(ack.action)) {
    const NewOrder& new_ord = std::get<NewOrder>(ack.action);

    // Remove tm_it.
    // This outstanding_buy/sell stuff should just be in its own fxn.
    rmOutstandingShs(
        ord.curr_qty, ord.px.toDouble(), ord.side,
        (ord.time_in_force != TimeInForce::GTC && ord.time_in_force != TimeInForce::ALO));

    live_order_ids_.erase(ord.pk_order_id);

    // Maybe just show the reason.
    std::cout << " GatewayRej, reason " << ack.reason << std::endl;

    // Handle the precogs too.
    cleanPrecog(ord.pk_order_id, ord.side);

    // Also remove from open_ord_annotations_ (outstanding already removed above).
    open_ord_annotations_.erase(ord.pk_order_id);

    if (pktrade::GlobalVar::orders_out_ != nullptr) {
      // Symbol,market,side,qty,px
      *pktrade::GlobalVar::orders_out_
          << time_utils::nowToStr() << ","

          << time_utils::nowToMs() << ",GatewayReject," << ord.symbol.get() << ","
          << magic_enum::enum_name(ord.mkt) << "," << ord.pk_order_id << ","
          << (ord.side == Side::Buy ? "Buy" : "Sell") << "," << ord.open_qty.toDouble() << ","
          << ord.px.toDouble() << "," << "\n";
      // Ack from the gateway.
    }
  }

  LOG(INFO) << fmt::format(
      "({}) GatewayAck(rej), reason {} outstanding_buy {} outstanding_sell {} position {}",
      ord.symbol.get(), ack.reason, outstanding_buy_.toDouble(), outstanding_sell_.toDouble(),
      position_.toDouble());

  // pauseTrading(10, "Pausing for 10 seconds because received a gateway
  // reject");
}

// Not sure what to do just yet here.
void TradeRiskMan::onOE(const Order& ord, const NewOrderAck& ack) {
  // I guess this means we look through and find the neworder.
  // And the push in an ord that's the same as the existing ord.

  /*
   LOG(INFO) << "newOrderAck, outstanding_buy " << outstanding_buy_.toDouble()
             << " outstanding_sell " << outstanding_sell_.toDouble()
             << " position " << position_.toDouble() << "\n";
*/

  ordex_->newOrdAck(ord);

  // Log the ack, with PlaceReason (this wasn't populated yet when we sent the order out).
  PKOrderId oid = ord.pk_order_id;
  PlaceReason reason = PlaceReason::Unset;
  if (!open_ord_annotations_.contains(oid)) {
    // Order probably got lapped by cancel or exec'ed away before ack
    LOG(INFO) << fmt::format("({}) onOE NewOrderAck: order {} not found in open_ord_annotations_",
                             ord.symbol.get(), oid);
  } else {
    reason = open_ord_annotations_[oid].reason;
  }
  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // Symbol,market,side,qty,px
    *pktrade::GlobalVar::orders_out_
        << time_utils::nowToStr() << "," << time_utils::nowToMs() << ",NewOrdAck," << oid << ","
        << ord.symbol.get() << "," << magic_enum::enum_name(ord.mkt) << ","
        << (ord.side == Side::Buy ? "Buy" : "Sell") << "," << ack.exch_transact_time
        << ",PR:" << to_str(reason) << std::endl;
  }
}

// Can happen for all sorts of reasons. This shows up a lot when the exchange
// can be in a very transitory state. Not going to pause, might be a good time
// to send more orders.
void TradeRiskMan::onOE(const Order& ord, const NewOrderReject& rej) {
  rmOutstandingShs(
      ord.curr_qty, ord.px.toDouble(), ord.side,
      (ord.time_in_force != TimeInForce::GTC && ord.time_in_force != TimeInForce::ALO));

  live_order_ids_.erase(ord.pk_order_id);

  // Inform the ordex.
  ordex_->ordReject(ord, rej);

  std::cout << " NewOrderRej outstanding_buy " << outstanding_buy_.toDouble()
            << " outstanding_sell " << outstanding_sell_.toDouble() << " reason: " << rej.reason
            << std::endl;

  // Handle the precogs too.
  cleanPrecog(ord.pk_order_id, ord.side);

  // Also remove from open_ord_annotations_.
  open_ord_annotations_.erase(ord.pk_order_id);

  // Log the reject.
  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // Symbol,market,side,qty,px
    *pktrade::GlobalVar::orders_out_
        << time_utils::nowToStr() << ","

        << time_utils::nowToMs() << ",NewOrderReject," << ord.symbol.get() << ","
        << magic_enum::enum_name(ord.mkt) << "," << ord.pk_order_id << ","
        << (ord.side == Side::Buy ? "Buy" : "Sell") << "," << ord.open_qty.toDouble() << ","
        << ord.px.toDouble() << "," << rej.reason << "\n";
  }

  // Note: these error-string-to-reason mapping are HL-specific.
  NewOrdRejReason reason = NewOrdRejReason::Unknown;
  if (rej.reason.find("Price must be divisible by tick size") != std::string::npos) {
    reason = NewOrdRejReason::Tick;
  } else if (rej.reason.find("Order must have minimum value") != std::string::npos) {
    reason = NewOrdRejReason::MinTradeNtl;
  } else if (rej.reason.find("Insufficient margin to place order") != std::string::npos) {
    reason = NewOrdRejReason::PerpMargin;
  } else if (rej.reason.find("Post only order would have immediately matched") != std::string::npos) {
    reason = NewOrdRejReason::BadAloPx;
  } else if (rej.reason.find("Order could not immediately match") != std::string::npos) {
    reason = NewOrdRejReason::IocCancel;
  } else if (rej.reason.find("open interest") != std::string::npos) {
    // There are a few different types of these, lumping into one here.
    reason = NewOrdRejReason::OICap;
  } else if (rej.reason.find("Price too far from oracle") != std::string::npos ||
             rej.reason.find("Order has invalid price") != std::string::npos ||
             rej.reason.find("Order price cannot be more than") != std::string::npos) {
    reason = NewOrdRejReason::Oracle;
  } else if (rej.reason.find("restarting") != std::string::npos) {
    reason = NewOrdRejReason::Restart;
  } else if (rej.reason.find("Expired after 3s without ack") != std::string::npos) {
    reason = NewOrdRejReason::Expired;
  } else if (rej.reason.find("L1 congested") != std::string::npos) {
    reason = NewOrdRejReason::L1Congested;
  } else if (rej.reason.find("Too many abstraction operations") != std::string::npos) {
    reason = NewOrdRejReason::TooManyOps;
  } else if (rej.reason.find("Order value too large") != std::string::npos) {
    reason = NewOrdRejReason::TooLarge;
  } else if (rej.reason.find("429 pause") != std::string::npos) {
    reason = NewOrdRejReason::Pause429;
  } else if (rej.reason.find("Trading is halted") != std::string::npos) {
    reason = NewOrdRejReason::Halted;
  } else if (rej.reason.find("blocked after liquidation") != std::string::npos) {
    reason = NewOrdRejReason::Liquidated;
  }

  auto log_msg =
      fmt::format("({}) NewOrderReject: outstanding_buy {} outstanding_sell {} pos {} reason: {}",
                  ord.symbol.get(), outstanding_buy_.toDouble(), outstanding_sell_.toDouble(),
                  position_.toDouble(), rej.reason);
  auto reason_str = magic_enum::enum_name(reason);

  switch (reason) {
    case NewOrdRejReason::IocCancel: {
      // Crossers will get this all the time so don't even bother logging.
      break;
    }
    case NewOrdRejReason::BadAloPx:
    case NewOrdRejReason::Restart:
    case NewOrdRejReason::Expired: {
      // These are expected to happen every once in a while, so just log to INFO.
      LOG(INFO) << log_msg;
      break;
    }
    case NewOrdRejReason::Tick:
    case NewOrdRejReason::MinTradeNtl:
    case NewOrdRejReason::OICap:
    case NewOrdRejReason::TooManyOps: {
      // These are bad and will keep spamming orders if we don't pause. Also email.
      LOG(ERROR) << log_msg;
      pauseTrading(60, TLReason::OrderReject,
                   fmt::format("Pausing for 1 minute because of {} order reject", reason_str));
      pktrade::util::Mailer::instance().send_mail(
          fmt::format("NewOrderReject for {} on {}", reason_str, symbol_id_.get()),
          fmt::format("{}:{} paused for 1 minute.\nReason: {}", pktrade::GlobalVar::strat_id_,
                      symbol_id_.get(), rej.reason),
          60);
      break;
    }
    case NewOrdRejReason::Halted:
    case NewOrdRejReason::Liquidated: {
      // These are probably a misconfig that will keep spamming orders, so TL5 and email.
      LOG(ERROR) << log_msg;
      setTL(TradingLevel::TL5, TLReason::OrderReject);
      pktrade::util::Mailer::instance().send_mail(
          fmt::format("NewOrderReject for {} on {}", reason_str, symbol_id_.get()),
          fmt::format("{}:{} TL5'ed.\nReason: {}", pktrade::GlobalVar::strat_id_, symbol_id_.get(),
                      rej.reason),
          60);
      break;
    }
    case NewOrdRejReason::PerpMargin:
    case NewOrdRejReason::L1Congested: {
      // These are also bad but are global issues handled in the gateway, so pause but don't email.
      LOG(ERROR) << log_msg;
      pauseTrading(60, TLReason::OrderReject,
                   fmt::format("Pausing for 1 minute because of {} order reject", reason_str));
      break;
    }
    case NewOrdRejReason::Oracle:
    case NewOrdRejReason::TooLarge:
      // These shouldn't happen anymore but if they do, don't pause but email, same as default.
    case NewOrdRejReason::Pause429:
      // These rate-limit 429s usually only last a few seconds, and are handled in the gateway.
    default: {
      // Don't know what these are yet, so alert but don't pause. HL could change the wording on
      // these, or introduce a new reject type, so we don't want to block trading if that happens.
      LOG(ERROR) << log_msg;
      pktrade::util::Mailer::instance().send_mail(
          fmt::format("NewOrderReject for {}", reason_str),
          fmt::format("Strat: {}:{}\nReason: {}", pktrade::GlobalVar::strat_id_,
                      symbol_id_.get(), rej.reason),
          60);
      break;
    }
  }
}

void TradeRiskMan::onOE(const Order& ord, const CancelOrderAck& ack) {

  if (live_order_ids_.count(ord.pk_order_id) == 0) {
    LOG(WARNING) << fmt::format("({}) Attempting to erase non-existent order ID: {}",
                                ord.symbol.get(), ord.pk_order_id);
    return;
  }

  // Update its notion of outstanding.
  rmOutstandingShs(ord.curr_qty, ord.px.toDouble(), ord.side, false);

  live_order_ids_.erase(ord.pk_order_id);

  // Inform the ordex.
  ordex_->ordCancel(ord);

  // Handle the precogs too.
  cleanPrecog(ord.pk_order_id, ord.side);

  open_ord_annotations_.erase(ord.pk_order_id);
  /*
  LOG(INFO) << "TradeRiskMan : CancelOrderAck, "
            << " OBS " << outstanding_buy_.toDouble() << " OSS "
            << outstanding_sell_.toDouble() << "\n";
  */
  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // LOG(INFO) << " WRITING NEWORD OUT\n";
    // Write stuff out.
    // time,symbol,market,side,qty,px
    *pktrade::GlobalVar::orders_out_
        << time_utils::nowToStr() << ","

        << time_utils::nowToMs() << ",CancelAck," << ord.pk_order_id << "," << symbol_id_.get()
        << "," << magic_enum::enum_name(ord.mkt) << "," << ack.exch_transact_time << std::endl;
  }
}

void TradeRiskMan::onOE(const Order& ord, const CancelOrderReject& rej) {
  // Since we didn't cancel successfully, don't

  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // oid,symbol,market,side,qty,px
    *pktrade::GlobalVar::orders_out_
        << time_utils::nowToStr() << ","

        << time_utils::nowToMs() << ",CancelOrdReject," << "," << ord.pk_order_id << ","
        << ord.symbol.get() << "," << magic_enum::enum_name(ord.mkt) << "," << ord.exchange_order_id
        << "\n";
    // Ack from the gateway.
  }

  LOG(INFO) << fmt::format("({}) TradeRiskMan: CancelOrderReject {}", ord.symbol.get(), rej.reason);
  pauseTrading(5, TLReason::CancelReject, "Pausing for 5 seconds because received a cancel reject");
}

void TradeRiskMan::onOE(const Order& ord, const ModifyOrderAck& ack) {
  // Do nothing for now.
}

void TradeRiskMan::onOE(const Order& ord, const ModifyOrderReject& rej) {
  // Nothing to do rn.
}

// ord should be the new state of the order, and
// exec should be the exec details.
// I should use exec to calculate pnl, but
// use ord to modify our notion of outstanding.
void TradeRiskMan::onOE(const Order& ord, const OrderExecute& exec) {
  // LOG(INFO) << " orderExecute happened "
  //           << " Side " << (ord.side == Side::Buy ? "Buy" : "Sell") << " qty
  //           "
  //           << exec.qty.toDouble();

  if (traded_ords_.find(ord.pk_order_id) == traded_ords_.end()) {
    times_traded_++;
    traded_ords_.insert(ord.pk_order_id);
  }

  // Order& local_ord = open_ords_[ord.pk_order_id];
  //  Copy state from the ord.
  // local_ord = ord;
  //  I guess I just gotta put it in place.

  // Note: don't [] into open_ord_annotations_ before checking it's there, ow it'll insert one!
  auto annot_it = open_ord_annotations_.find(exec.pk_order_id);
  OrdAnnot blank_annot;
  OrdAnnot& annot = (annot_it != open_ord_annotations_.end()) ? annot_it->second : blank_annot;

  // Modify our outstanding shares, unless the order was already eliminated or the annot erased.
  // Every path that erases the annot (cancel-ack, elim, full-fill, reject, cleanOldIOCs) first
  // removes the order's outstanding shares, so if the annot is gone the outstanding has already
  // been accounted for. This covers two races where an exec arrives after the order is terminal:
  //  - IOC elim before exec: annotation kept but flagged eliminated_ (elim removed the full qty).
  //  - resting order partially filled then cancelled, cancel-ack laps the exec: annotation already
  //    erased (cancel-ack removed the full qty).
  if (annot_it != open_ord_annotations_.end() && !annot.eliminated_) {
    rmOutstandingShs(
        exec.qty, exec.px.toDouble(), ord.side,
        (ord.time_in_force != TimeInForce::GTC && ord.time_in_force != TimeInForce::ALO));
  }

  tot_traded_ += exec.qty;
  tot_notional_traded_ += exec.px.toDouble() * exec.qty.toDouble();

  double last_pos_f = position_.toDouble();

  // Update position, pnl amounts.
  // I'm being very explicit with this to make it easier for me to understand.
  if (ord.side == Side::Buy) {
    position_ = exec.qty + position_;
  } else {
    // Side == Sell
    position_ = position_ - exec.qty;
  }

  double pos_f = position_.toDouble();

  // We count 1 to -1 as 1 flip. We also count 1 to 0 to -1 as 1 flip.
  if (pos_f > 0) {
    if (pos_dir_strict_ == -1) {
      times_flipped_++;
    }
    pos_dir_strict_ = 1;
  } else if (pos_f < 0) {
    if (pos_dir_strict_ == 1) {
      times_flipped_++;
    }
    pos_dir_strict_ = -1;
  }

  commissions_ += commissioner_.getCommission(exec.px, exec.qty, ord.symbol, ord.mkt, exec.add_liq);

  // Update v3 pnl numbers: tot_entry_notional_, v3_closed_pnl_, avg_entry_px_, etc.
  // tot_entry_notional_ is the total we spent to enter into our current position (positive if we're
  // long, negative if we're short). In theory, open pnl is what we would get if we closed out our
  // current position (at the current mid price), minus tot_entry_notional_, but we are using
  // running_notional_basecur_ for total pnl calcs (v2) instead, which should give the same result.
  // To update tot_entry_notional_ and closed pnl with this exec:
  // Case 1: We added to existing position (e.g., 1 to 2, -1 to -2).
  //   Add exec notional ("spend") to tot_entry_notional_
  //   No change to closed pnl
  // Case 2: We reduced existing position (e.g., 2 to 1, -2 to -1).
  //   Scale down tot_entry_notional_ proportionally to reduced position
  //   Closed pnl updated by exec amount, using avg entry price to exec price
  // Case 3: We flipped sides (e.g., 1 to -1, -1 to 1)
  //   Reset tot_entry_notional_ based on new position and exec price
  //   Closed pnl updated by amount bringing old position to 0, using avg entry price to exec price
  double exec_notional = (exec.qty.toDouble() * exec.px.toDouble() *
                          // if we sold, what we "spent" is negative
                          (ord.side == Side::Buy ? 1 : -1));
  if ((0 < last_pos_f && last_pos_f < pos_f) || // long to longer
      (0 > last_pos_f && last_pos_f > pos_f)) { // short to shorter
    tot_entry_notional_ += exec_notional;
  } else if ((0 < pos_f && pos_f < last_pos_f) || // long to less long
             (0 > pos_f && pos_f > last_pos_f)) { // short to less short
    tot_entry_notional_ *= (pos_f / last_pos_f);
    v3_closed_pnl_ += (exec.qty.toDouble() * (avg_entry_px_ - exec.px.toDouble()) *
                       (ord.side == Side::Buy ? 1 : -1));
    // We're closing out exec.qty shares (positive number), so closed pnl should be incremented by
    // exec.qty * (sell price - buy price), whichever one is enter/exit. If we just bought, sell
    // price is avg_entry_px_, so this is right. If we just sold, sell price is exec.px, so flipping
    // the sign is right.
  } else {
    // pos_f < 0 < last_pos_f (long to short) or last_pos_f < 0 < pos_f (short to long)
    // Also covers all cases to or from a 0 position.
    tot_entry_notional_ = pos_f * exec.px.toDouble();
    v3_closed_pnl_ += last_pos_f * (exec.px.toDouble() - avg_entry_px_);
    // Note 1: If last_pos_f is positive (we were long), we just sold at exec price. If last_pos_f
    // is negative (we were short), we just bought at exec price, so the double negatives cancel.
    // Note 2: This is the only case where last_pos_f can be 0, therefore where avg_entry_px_ can be
    // invalid (it'll just have the last valid value), but in that case v3_closed_pnl_ += 0.
  }

  // Recalc avg entry price.
  if (pos_f != 0) {
    avg_entry_px_ = tot_entry_notional_ / pos_f;
  }

  //////////////////////////////////////////////////////////
  // Bleed detector: update on position closes.
  if (use_bleed_detector_ && ((0 < pos_f && pos_f < last_pos_f) || // long to less long
                              (0 > pos_f && pos_f > last_pos_f) || // short to less short
                              (pos_f <= 0 && last_pos_f > 0) ||    // long to flat/short
                              (pos_f >= 0 && last_pos_f < 0))) {   // short to flat/long

    double close_pnl = v3_closed_pnl_ - bleed_prev_v3_closed_pnl_;
    bleed_prev_v3_closed_pnl_ = v3_closed_pnl_;

    double shares_closed = std::min(std::abs(last_pos_f), std::abs(last_pos_f - pos_f));
    double close_notional = shares_closed * exec.px.toDouble();

    int indicator = 0;
    if (close_notional > 0) {
      double close_frac = close_pnl / close_notional;
      if (close_frac > bleed_deadzone_thresh_) {
        indicator = 1;
      } else if (close_frac < -bleed_deadzone_thresh_) {
        indicator = -1;
      }
    }

    if (indicator != 0) {
      double decay = std::exp(-1.0 / bleed_tdc_);
      bleed_score_ = bleed_score_ * decay + indicator;
      bleed_n_++;
    }

    double normalized = bleed_score_ / bleed_max_;

    if (bleed_n_ > bleed_tdc_) {
      if (normalized < bleed_score_stop_ && !bleed_stopped_) {
        setTL(TradingLevel::TL5, TLReason::Bleed);
        bleed_stopped_ = true;

        if (bleed_score_stop_ >= -1.0 / bleed_max_) {
          // If stop threshold is already so low that a single negative trade will trigger it,
          // don't try to recover. We're done for the day.
          LOG(ERROR) << fmt::format("({}) Bleed detector: normalized score {:.3f} < stop {:.3f}. "
                                    "Stopping trading (TL5) for the day.",
                                    symbol_id_.get(), normalized, bleed_score_stop_);
          // Bleed emails suppressed (2026-06-29): too noisy — dozens per weekend
          // day on a heavy universe. Rely on log grep for post-hoc review.
          // if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
          //   pktrade::util::Mailer::instance().send_mail(
          //       fmt::format("{} {} bleed detector STOP", pktrade::GlobalVar::strat_id_,
          //                   symbol_id_.get()),
          //       fmt::format("normalized_score: {:.3f}\nbleed_score_stop: {:.3f}\n"
          //                   "closes: {}\npnl: {:.2f}\ndone for the day",
          //                   normalized, bleed_score_stop_, bleed_n_, v3_closed_pnl_));
          // }

        } else {
          LOG(ERROR) << fmt::format("({}) Bleed detector: normalized score {:.3f} < stop {:.3f}. "
                                    "Stopping trading (TL5) for {}s.",
                                    symbol_id_.get(), normalized, bleed_score_stop_,
                                    bleed_stop_timeout_s_);
          // Schedule recovery after timeout. Score IS reset — any previous bleed widening will
          // also be reset. But the thresholds to trigger both stop and widen is halved.
          pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(bleed_stop_timeout_s_),
                                                     [&] { this->bleedRecovery(); });
          // Bleed emails suppressed (see comment above).
          // if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
          //   pktrade::util::Mailer::instance().send_mail(
          //       fmt::format("{} {} bleed detector STOP", pktrade::GlobalVar::strat_id_,
          //                   symbol_id_.get()),
          //       fmt::format("normalized_score: {:.3f}\nbleed_score_stop: {:.3f}\n"
          //                   "closes: {}\npnl: {:.2f}\nresume in {}s",
          //                   normalized, bleed_score_stop_, bleed_n_, v3_closed_pnl_,
          //                   bleed_stop_timeout_s_));
          // }
        }

      } else if (normalized < bleed_score_widen_ && !bleed_widened_ && !bleed_stopped_) {
        LOG(WARNING) << fmt::format("({}) Bleed detector: normalized score {:.3f} < widen {:.3f}. "
                                    "Widening thresh to {:.1f}x.",
                                    symbol_id_.get(), normalized, bleed_score_widen_,
                                    bleed_widen_thresh_mult_);
        ordex_->setThreshMult(bleed_widen_thresh_mult_, MultSource::Bleed);
        bleed_widened_ = true;
      }

      // Widen recovery: score back above 0 (only for widen, not stop).
      if (normalized > 0 && bleed_widened_ && !bleed_stopped_) {
        LOG(INFO) << fmt::format(
            "({}) Bleed detector: normalized score {:.3f} recovered from widen. "
            "Resetting thresh.",
            symbol_id_.get(), normalized);
        ordex_->setThreshMult(1.0, MultSource::Bleed);
        bleed_widened_ = false;
      }
    }
  }

  //////////////////////////////////////////////////////////
  // Double-down detector: track own volume on every exec, score on closes.
  if (use_double_down_) {
    int64_t now = time_utils::nowToMs();

    double exec_ntl = exec.px.toDouble() * exec.qty.toDouble();
    double own_decay =
        (dd_own_last_t_ > 0) ? std::exp(-double(now - dd_own_last_t_) / dd_long_tdc_ms_) : 0;
    dd_own_ntl_ems_ = exec_ntl + own_decay * dd_own_ntl_ems_;
    dd_own_last_t_ = now;

    // Same close condition as the bleed detector, but with our own closed-pnl
    // anchor so the two features stay independent.
    if ((0 < pos_f && pos_f < last_pos_f) || // long to less long
        (0 > pos_f && pos_f > last_pos_f) || // short to less short
        (pos_f <= 0 && last_pos_f > 0) ||    // long to flat/short
        (pos_f >= 0 && last_pos_f < 0)) {    // short to flat/long
      double close_pnl = v3_closed_pnl_ - dd_prev_v3_closed_pnl_;
      dd_prev_v3_closed_pnl_ = v3_closed_pnl_;

      // On flips, only the closed portion counts.
      double shares_closed = std::min(std::abs(last_pos_f), std::abs(last_pos_f - pos_f));
      double close_ntl = shares_closed * exec.px.toDouble();

      double long_decay = 0;
      double short_decay = 0;
      if (dd_close_last_t_ > 0) {
        long_decay = std::exp(-double(now - dd_close_last_t_) / dd_long_tdc_ms_);
        short_decay = std::exp(-double(now - dd_close_last_t_) / dd_short_tdc_ms_);
      }
      dd_pnl_long_ems_ = close_pnl + long_decay * dd_pnl_long_ems_;
      dd_ntl_long_ems_ = close_ntl + long_decay * dd_ntl_long_ems_;
      dd_pnl_short_ems_ = close_pnl + short_decay * dd_pnl_short_ems_;
      dd_ntl_short_ems_ = close_ntl + short_decay * dd_ntl_short_ems_;
      dd_close_last_t_ = now;

      dd_closes_++;
      if (dd_first_close_t_ == 0) {
        dd_first_close_t_ = now;
      }

      evaluateDoubleDown();
    }
  }

  //////////////////////////////////////////////////////////
  // PNL calc v2.
  // Just gonna make this easier.
  // running_notional is the amount of our cash balance. If we short, then we
  // add positive amount to that amount. If we buy, we subtract a positve amount
  // to that amount. Whatever excess position we have, there's a little bit of
  // open pnl that we can explain away.
  running_notional_basecur_ +=
      (ord.side == Side::Buy ? -1 : 1) * (exec.px.toDouble() * exec.qty.toDouble());

  // If we sold, running_notional_basecur_ is positive. Position is negative. So
  // we should add the other side to get the mark-to-mid closed_pnl (approx).
  // All this is done after the position_ gets updated, of course.
  double mid_px = mid_sig_->getValue();
  if (mid_px == 0) {
    LOG(ERROR) << fmt::format("({}) PNL Calc onOE: no mid price so using exec_px {}",
                              symbol_id_.get(), exec.px.toDouble());
    mid_px = exec.px.toDouble();
  } else if (std::abs(mid_px - exec.px.toDouble()) / exec.px.toDouble() > 0.1) {
    double best_bid = mid_sig_->getBBMeasure();
    double best_ask = mid_sig_->getBAMeasure();
    // In case one side of the book disappears or something that causes a wild mid_px, use exec_px.
    LOG(ERROR) << fmt::format(
        "({}) PNL Calc onOE: mid price {} too far off (bb {} ba {}), using exec_px {}",
        symbol_id_.get(), mid_px, best_bid, best_ask, exec.px.toDouble());
    mid_px = exec.px.toDouble();
  }
  v2_closed_pnl_ = running_notional_basecur_ + (pos_f * mid_px);

  // What we should get from all this:
  //   open_pnl = position * mid_px - tot_entry_notional_
  //   closed_pnl = running_notional_basecur_ + tot_entry_notional_
  //   total_pnl = open_pnl + closed_pnl = running_notional_basecur_ + position * mid_px
  double v3_open_pnl = pos_f * mid_px - tot_entry_notional_;
  double v3_total_pnl = v3_open_pnl + v3_closed_pnl_;
  LOG_EVERY_N(INFO, 101) << fmt::format(
      "({}) PNL Calc onOE: side {} qty {} px {} old_pos {} new_pos {} mid_px {} "
      "tot_entry_notional {} running_notional_basecur {} v3_open_pnl {} v3_closed_pnl {} "
      "v3_total_pnl {} v2_closed_pnl {}",
      symbol_id_.get(), (ord.side == Side::Buy ? "Buy" : "Sell"), exec.qty.toDouble(),
      exec.px.toDouble(), last_pos_f, pos_f, mid_px, tot_entry_notional_, running_notional_basecur_,
      v3_open_pnl, v3_closed_pnl_, v3_total_pnl, v2_closed_pnl_);
  // Check for any accumulation of rounding errors or other drift between v2 and v3
  double diff = std::abs(v3_total_pnl - v2_closed_pnl_);
  if (diff > 0.01 && v2_closed_pnl_ != 0) {
    double pct_diff = std::abs(diff / v2_closed_pnl_);
    if (pct_diff > 0.001) {
      LOG(WARNING) << fmt::format(
          "({}) PNL CALC DRIFT: v2_closed_pnl_ {} v3_total_pnl {} diff {} pct_diff {}",
          symbol_id_.get(), v2_closed_pnl_, v3_total_pnl, diff, pct_diff);
    }
  }

  if (v2_closed_pnl_ > highest_closed_pnl_) {
    pktrade::util::log_line(
        fmt::format("{}: Updating highest_close from {} to {}, updating minfv too.",
                    time_utils::nowToStr().c_str(), highest_closed_pnl_, v2_closed_pnl_),
        false);
  }
  highest_closed_pnl_ = std::max(highest_closed_pnl_, v2_closed_pnl_);

  // What if I ratchet up the min_pnl by a little?

  // if (highest_closed_pnl_ + min_pnl_ > (min_pnl)

  // Resets the min_pnl a little bit with respect to how much pnl we've
  // obtained.
  min_pnl_ = 1 * (highest_closed_pnl_ - commissions_) + (min_pnl_incr_ * (num_fv_ + 1));

  //////////////////////////////////////////////////////////

  // Check out if we should have minfv'd. If so, set ourselves unable to trade,
  // but also reset in 20 minutes.
  if (v2_closed_pnl_ - commissions_ < min_pnl_) {
    hitMinFv();
  }

  // inform the ordex.
  ordex_->ordExec(ord, exec);

  if (v2_closed_pnl_ < min_pnl_seen_) {
    min_pnl_seen_ = v2_closed_pnl_;
  }

  /*
  LOG(INFO) << time_utils::nowToStr() << "TradeRiskMan onOE, "
            << " Size " << exec.qty.toDouble() << " Px " << exec.px.toDouble()
            << " Side " << (exec.side == Side::Buy ? " Buy " : " Sell ")
            << " Pos " << position_.toDouble() << " OBS "
            << outstanding_buy_.toDouble() << " OSS "
            << outstanding_sell_.toDouble() << " closed_pnl " << v2_closed_pnl_
            << " comm " << commissions_ << "\n";
*/

  // Potentially write the exec out.
  if (pktrade::GlobalVar::trades_out_ != nullptr) {
    // Could I not find it?
    if (annot_it == open_ord_annotations_.end()) {
      // This was never reachable before bc of the indexing bug. Now, it's reachable in at least 1
      // common case: partial fill (of a resting order) followed by successful cancel, but the
      // cancel-ack laps the exec to us. Cancel-ack erases the annot, so when we get here it'll be
      // gone. Could fix this by having a delayed cleanup similar to cleanOldIOCs, but let's punt
      // until we actually need it. Note that these execs in the orders file will always be
      // immediately preceded by a cancel-ack.
      LOG(INFO) << fmt::format("({}) Could not find annotation for order id {}", symbol_id_.get(),
                               exec.pk_order_id);
    } else {
      // Deal with ioc shares if applicable.
      if (annot.ioc_) {
        rmliq_shs_ += exec.qty.toDouble();
      }
    }

    // LOG(INFO) << " WRITING TRADE OUT\n";
    //  time,oid,symbol,market,side,price,size,size_left,position,closed_pnl,addrm
    *pktrade::GlobalVar::trades_out_
        << time_utils::nowToStr() << ","
        << time_utils::nowToMs()
        // Just... easier to read for me.
        << "," << exec.pk_order_id << "," << symbol_id_.get() << ","
        << magic_enum::enum_name(ord.mkt) << "," << (exec.side == Side::Buy ? "Buy" : "Sell") << ","
        << exec.px.toDouble() << "," << exec.qty.toDouble() << "," << position_.toDouble() << ","
        << v2_closed_pnl_ << "," << commissions_ << ","
        << net_funding_
        // Add annotation info.
        << "," << annot.bb_ << "," << annot.ba_ << "," << annot.pred_px_ << "," << annot.mid_px_
        << "," << annot.t_sent_ << "," << annot.last_uid_bk_ << "," << exec.feed_trade_id << ","
        << exec.exch_transact_time << "," << (exec.add_liq ? "ADD" : "REM")
        << ",PR:" << to_str(annot.reason) << std::endl;
    //
  }

  // Potentially write this to the order too.
  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // LOG(INFO) << " WRITING ORDER EXEC OUT\n";
    //  Symbol,market,side,qty,px
    *pktrade::GlobalVar::orders_out_
        << time_utils::nowToStr() << ","

        << time_utils::nowToMs() << "," << "Exec," << exec.pk_order_id << "," << symbol_id_.get()
        << "," << magic_enum::enum_name(ord.mkt) << "," << (exec.side == Side::Buy ? "Buy" : "Sell")
        << "," << exec.px.toDouble() << "," << exec.qty.toDouble() << "," << ord.curr_qty.toDouble()
        << "," << position_.toDouble() << "," << v2_closed_pnl_ << "," << exec.feed_trade_id << ","
        << exec.exch_transact_time
        // No annotation info.
        << std::endl;
    // Ack from the gateway.
  }

  // Remove if fully deleted, unless curr_qty was just zeroed out bc of an elim (partially-filled
  // IOC), in which case defer until cleanOldIOCs.
  if (ord.curr_qty == Quantity{0} && !annot.eliminated_) {
    live_order_ids_.erase(ord.pk_order_id);

    open_ord_annotations_.erase(ord.pk_order_id);

    // Handle the precogs too, in this case.
    cleanPrecog(ord.pk_order_id, ord.side);
  }

  // Potentially start our funding calcs, if not done yet.
  // TODO: just put this in another function.
  // There's some race condition in clock setting that can have this set before
  // a trm knows what time it is. So we're just making the last thing a check to
  // prevent an infinite loop.
  if (use_funding_ && !started_funding_calcs_ && time_utils::nowToMs() > 1000000) {
    if (mkt_ == Market::Hyperliquid) {
      next_funding_t_ = pktrade::GlobalVar::funding_master_->get_next_hyperliquid_time();

      Clock::time_point next_funding_tp = time_utils::msToTp(next_funding_t_);
      // Set the callback.
      pktrade::GlobalVar::event_loop_->onTimeout(next_funding_tp,
                                                 [&] { this->recordFundingPayments(); });
    } else if (mkt_ == Market::BinanceFutures) {
      next_funding_t_ = pktrade::GlobalVar::funding_master_->get_next_binance_time();

      Clock::time_point next_funding_tp = time_utils::msToTp(next_funding_t_);

      // Set the callback.
      pktrade::GlobalVar::event_loop_->onTimeout(next_funding_tp,
                                                 [&] { this->recordFundingPayments(); });
    }
    started_funding_calcs_ = true;
  }

  // Update our positions.
  pktrade::GlobalVar::funding_master_->updatePosition(book_id_, position_.toDouble());

  // LOG(INFO) << "exec, outstanding_buy " << outstanding_buy_.toDouble()
  //           << " outstanding_sell " << outstanding_sell_.toDouble()
  //           << " position " << position_.toDouble() << "\n";
}

void TradeRiskMan::onOE(const Order& ord, const OrderElimination& elim) {
  // Modify our outstanding shares.
  rmOutstandingShs(
      ord.curr_qty, ord.px.toDouble(), ord.side,
      (ord.time_in_force != TimeInForce::GTC && ord.time_in_force != TimeInForce::ALO));
  live_order_ids_.erase(ord.pk_order_id);

  // std::cout << " Elim outstanding_buy " << outstanding_buy_.toDouble()
  //           << " outstanding_sell " << outstanding_sell_.toDouble()
  //           << std::endl;

  // Handle the precogs too.
  cleanPrecog(ord.pk_order_id, ord.side);

  // For an IOC, we might receive an elim before execs, in which case we do the above bookkeeping,
  // but don't erase the annotations yet, so it's available when the execs hit. We just mark it
  // eliminated so when we do erase it (in cleanOldIOCs), we don't duplicate the bookkeeping.
  // For a non-IOC, just erase it now.
  auto annot_it = open_ord_annotations_.find(ord.pk_order_id);
  if (annot_it != open_ord_annotations_.end()) {
    if (ord.time_in_force == TimeInForce::IOC) {
      annot_it->second.eliminated_ = true;
    } else {
      // This is only ever reachable in sim. In live, only IOCs can have elims.
      open_ord_annotations_.erase(annot_it);
    }
  }

  // inform the ordex.
  ordex_->ordElim(ord, elim);

  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    // Symbol,market,side,qty,px
    *pktrade::GlobalVar::orders_out_ << time_utils::nowToStr() << ","

                                     << time_utils::nowToMs() << ",Elimination," << ord.symbol.get()
                                     << "," << magic_enum::enum_name(ord.mkt) << ","
                                     << ord.pk_order_id << "," << ord.exchange_order_id << ","
                                     << ord.curr_qty.toDouble()
                                     // No annotation info.
                                     << std::endl;
    // Ack from the gateway.
  }

  // LOG(INFO) << "eliminate, outstanding_buy " << outstanding_buy_.toDouble()
  //           << " outstanding_sell " << outstanding_sell_.toDouble()
  //           << " position " << position_.toDouble() << "\n";
}

void TradeRiskMan::recordFundingPayments() {
  util::log_line(fmt::format("TRM: starting to record funding payments!"), false);

  // TODO: there might be some drift based on timing.
  // We can tighten this.
  double cur_notional = position_.toDouble() * mid_sig_->getValue();
  double funding_amount = 0;
  last_funding_t_ = time_utils::nowToMs();
  if (mkt_ == Market::Hyperliquid) {
    funding_amount = pktrade::GlobalVar::funding_master_->hyperliquid_calc_funding(symbol_id_.get(),
                                                                                   cur_notional);
    net_funding_ += funding_amount;
    LOG(INFO) << symbol_id_.get() << " funding_received: " << funding_amount
              << " net_funding: " << net_funding_ << std::endl;

    util::log_line(fmt::format("{}: TradeRiskMan::recordFundingPayments. "
                               " sym {}  funding_amount {:.4f} net_funding {:.4f}",
                               time_utils::nowToStr(), symbol_id_.get(), funding_amount,
                               net_funding_),
                   false);

    // Set the next calc time.
    next_funding_t_ = pktrade::GlobalVar::funding_master_->get_next_hyperliquid_time() + 1000;

    Clock::time_point next_funding_tp = time_utils::msToTp(next_funding_t_);
    // Set the callback.
    pktrade::GlobalVar::event_loop_->onTimeout(next_funding_tp,
                                               [&] { this->recordFundingPayments(); });

  } else if (mkt_ == Market::BinanceFutures) {
    funding_amount =
        pktrade::GlobalVar::funding_master_->binance_calc_funding(symbol_id_.get(), cur_notional);
    net_funding_ += funding_amount;
    LOG(INFO) << symbol_id_.get() << " funding_received: " << funding_amount
              << " net_funding: " << net_funding_ << std::endl;

    util::log_line(fmt::format("{}: TradeRiskMan::recordFundingPayments. "
                               " sym {}  funding_amount {:.4f} net_funding {:.4f}",
                               time_utils::nowToStr(), symbol_id_.get(), funding_amount,
                               net_funding_),
                   false);

    // Wait a little more than the next funding time, so
    // we can be more sure we can calculate these properly.
    next_funding_t_ = pktrade::GlobalVar::funding_master_->get_next_binance_time() + 1000;

    Clock::time_point next_funding_tp = time_utils::msToTp(next_funding_t_);

    // Set the callback.
    pktrade::GlobalVar::event_loop_->onTimeout(next_funding_tp,
                                               [&] { this->recordFundingPayments(); });
  }
}

void TradeRiskMan::setTL(TradingLevel tl) {
  // Lower every reason-specific TL above tl down to tl
  int tl_int = static_cast<int>(tl);
  for (const auto& pair : reason_to_tl_) {
    if (static_cast<int>(pair.second) > tl_int) {
      setTL(tl, pair.first);
    }
  }
  // Raise the Normal TL to tl if it's lower
  if (reason_to_tl_[TLReason::Normal] != tl) {
    setTL(tl, TLReason::Normal);
  }
  // Sanity check: make sure final TL is correct
  if (getTL() != tl) {
    LOG(ERROR) << fmt::format("({}) setTL {} failed, TL still {}", symbol_id_.get(), tl_int,
                              magic_enum::enum_name(getTL()));
  }
}

void TradeRiskMan::setTL(TradingLevel tl, TLReason reason) {
  const std::string sym = symbol_id_.get();

  if (!magic_enum::enum_contains(reason)) {
    LOG(ERROR) << fmt::format("({}) setTL called with invalid TLReason value {}, ignoring", sym,
                              static_cast<int>(reason));
    return;
  }

  TradingLevel& cur_reason_tl = reason_to_tl_[reason];
  if (tl == cur_reason_tl) {
    LOG(INFO) << fmt::format("({}) {}-TL already in {}, nothing to do", sym,
                             magic_enum::enum_name(reason), magic_enum::enum_name(tl));
    return;
  }

  cur_reason_tl = tl;
  int max_tl = 0;
  for (const auto& pair : reason_to_tl_) {
    max_tl = std::max(max_tl, static_cast<int>(pair.second));
  }
  TradingLevel new_tl = static_cast<TradingLevel>(max_tl);
  if (cur_tl_ == new_tl) {
    LOG(INFO) << fmt::format("({}) Max TL already in {}, nothing to do", sym,
                             magic_enum::enum_name(new_tl));
    return;
  }

  LOG(INFO) << fmt::format("({}) TL changing from {} to {} for reason {}", sym,
                           magic_enum::enum_name(cur_tl_), magic_enum::enum_name(new_tl),
                           magic_enum::enum_name(reason));
  cur_tl_ = new_tl;
  const double pos = position_.toDouble();
  switch (cur_tl_) {
    case TradingLevel::TL0: {
      LOG(INFO) << fmt::format("({}) TL0: normal trading (pos {})", sym, pos);
      can_trade_pk_ = true;
      can_increase_risk_ = true;
      ordex_->setTL2AggrExit(false);
      ordex_->setTL3AggrExit(false);
      ordex_->setTL6CrossExit(false);
      ordex_->setTL7NoNewOrds(false);
      break;
    }
    case TradingLevel::TL1: {
      LOG(INFO) << fmt::format("({}) TL1: trading towards flat only (pos {})", sym, pos);
      can_trade_pk_ = true;
      can_increase_risk_ = false;
      ordex_->setTL2AggrExit(false);
      ordex_->setTL3AggrExit(false);
      ordex_->setTL6CrossExit(false);
      ordex_->setTL7NoNewOrds(false);
      break;
    }
    case TradingLevel::TL2: {
      LOG(INFO) << fmt::format("({}) TL2: trading aggressively towards flat (pos {})", sym, pos);
      can_trade_pk_ = true;
      can_increase_risk_ = false;
      ordex_->setTL2AggrExit(true);
      ordex_->setTL3AggrExit(false);
      ordex_->setTL6CrossExit(false);
      ordex_->setTL7NoNewOrds(false);
      // Aggressiveness enforced by ordex.
      break;
    }
    case TradingLevel::TL3: {
      LOG(INFO) << fmt::format("({}) TL3: trading max aggressively towards flat (pos {})", sym,
                               pos);
      can_trade_pk_ = true;
      can_increase_risk_ = false;
      ordex_->setTL2AggrExit(false);
      ordex_->setTL3AggrExit(true);
      ordex_->setTL6CrossExit(false);
      ordex_->setTL7NoNewOrds(false);
      // Aggressiveness enforced by ordex.
      break;
    }
    case TradingLevel::TL5: {
      LOG(INFO) << fmt::format("({}) TL5: canceling all orders and pausing trading (pos {})", sym,
                               pos);
      can_trade_pk_ = false;
      can_increase_risk_ = false;
      ordex_->setTL2AggrExit(false);
      ordex_->setTL3AggrExit(false);
      ordex_->setTL6CrossExit(false);
      ordex_->setTL7NoNewOrds(false);
      ordex_->cancelOutstandingOrds();
      break;
    }
    case TradingLevel::TL6: {
      LOG(INFO) << fmt::format("({}) TL6: canceling all orders and crossing out (pos {})", sym,
                               pos);
      can_trade_pk_ = true;
      can_increase_risk_ = false;
      ordex_->setTL2AggrExit(false);
      ordex_->setTL3AggrExit(false);
      ordex_->setTL6CrossExit(true);
      ordex_->setTL7NoNewOrds(false);
      ordex_->cancelOutstandingOrds();
      // Crossing and aggressiveness enforced by ordex.
      break;
    }
    case TradingLevel::TL7: {
      LOG(INFO) << fmt::format("({}) TL7: pausing new orders, keeping old (pos {})", sym, pos);
      can_trade_pk_ = true;
      can_increase_risk_ = false;
      ordex_->setTL2AggrExit(false);
      ordex_->setTL3AggrExit(false);
      ordex_->setTL6CrossExit(false);
      ordex_->setTL7NoNewOrds(true);
      break;
    }
    default: {
      LOG(ERROR) << fmt::format("({}) Unsupported {}!", sym, magic_enum::enum_name(tl));
      return;
    }
  }
}

const bool TradeRiskMan::canTrade() const { return can_trade_pk_; }

const bool TradeRiskMan::canPlaceMoreOrders() const { return live_order_ids_.size() < max_orders_; }

void TradeRiskMan::hitMinFv() {
  // If we're not in TL0, it doesn't count
  if (cur_tl_ != TradingLevel::TL0) {
    // LOG(INFO) << fmt::format("({}) Hit minfv but not in TL0. Ignoring.", symbol_id_.get());
    return;
  }

  LOG(INFO) << fmt::format("({}) Hit minfv ({} < {}). Going to TL1 for {} seconds.",
                           symbol_id_.get(), v2_closed_pnl_ - commissions_, min_pnl_,
                           timeout_minfv_);

  // for FV situations, allow trading only towards flat.
  setTL(TradingLevel::TL1, TLReason::MinFV);
  minfv_t_ = time_utils::nowToMs();
  num_fv_++;

  // Inform the ordex that we hit a minfv.
  ordex_->hitMinFv();

  if (num_fv_ > fv_limit_) {
    LOG(INFO) << fmt::format("({}) Hit minfv limit. Staying in TL1.", symbol_id_.get());
  } else {
    // Sets callback to move ourselves out of TL1.
    pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(timeout_minfv_), [&] {
      // You can leave timeout.
      this->backFromMinFv();
      // Sets a new minfv.
      // This was the old style of setting minfv.
      /*
        this->min_pnl_ =
        this->v2_closed_pnl_ - commissions_ + this->min_pnl_incr_;
      */
      // this->min_pnl_ = this->min_pnl_ + this->min_pnl_incr_;
    });
  }

  // Send an email
  if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
    pktrade::util::Mailer::instance().send_mail(
        fmt::format("{} {} hit minfv", pktrade::GlobalVar::strat_id_, symbol_id_.get()),
        fmt::format("pnl: {:.2f}\nmin_pnl_: {:.2f}\nnum_fv_: {}\nfv_limit_: {}",
                    v2_closed_pnl_ - commissions_, min_pnl_, num_fv_, fv_limit_));
  }
}

void TradeRiskMan::backFromMinFv() {
  ordex_->backFromMinFv();
  setTL(TradingLevel::TL0, TLReason::MinFV);
  LOG(INFO) << fmt::format("({}) Left minfv. New min_pnl {}", symbol_id_.get(), min_pnl_);
}

void TradeRiskMan::bleedRecovery() {
  if (!bleed_stopped_) {
    return;
  }
  // Reset score to 0 but halve the stop threshold — each successive
  // stop becomes twice as easy to trigger.
  bleed_score_ = 0;
  bleed_score_stop_ /= 2.0;
  bleed_score_widen_ /= 2.0;

  LOG(INFO) << fmt::format("({}) Bleed detector: resuming after {}s timeout. Score reset to 0. "
                           "New thresholds: widen={:.3f} stop={:.3f} (tightened).",
                           symbol_id_.get(), bleed_stop_timeout_s_, bleed_score_widen_,
                           bleed_score_stop_);
  setTL(TradingLevel::TL0, TLReason::Bleed);
  ordex_->setThreshMult(1.0, MultSource::Bleed);
  bleed_widened_ = false;
  bleed_stopped_ = false;
}

void TradeRiskMan::onTrade(const LevelBook& bk, const Trade& trd) {
  // Market traded notional EMS for double-down's market-presence gate.
  if (!use_double_down_ || dd_max_mkt_frac_ <= 0) {
    return;
  }
  int64_t now = time_utils::nowToMs();
  double decay =
      (dd_mkt_last_t_ > 0) ? std::exp(-double(now - dd_mkt_last_t_) / dd_long_tdc_ms_) : 0;
  dd_mkt_ntl_ems_ = trd.px.toDouble() * trd.qty.toDouble() + decay * dd_mkt_ntl_ems_;
  dd_mkt_last_t_ = now;
}

void TradeRiskMan::evaluateDoubleDown() {
  if (!use_double_down_) {
    return;
  }
  int64_t now = time_utils::nowToMs();

  // Fold decay into the gate EMSs (sum-form decay is associative, so decaying
  // now and adding samples later gives the same result).
  auto decay_to_now = [now](double& ems, int64_t& last_t, double tdc_ms) {
    if (last_t > 0) {
      ems *= std::exp(-double(now - last_t) / tdc_ms);
      last_t = now;
    }
  };
  decay_to_now(dd_own_ntl_ems_, dd_own_last_t_, dd_long_tdc_ms_);
  decay_to_now(dd_mkt_ntl_ems_, dd_mkt_last_t_, dd_long_tdc_ms_);

  // Do NOT decay the pnl/ntl score EMSs here: the edge is a ratio of two
  // same-decay EMSs, so it's invariant under common decay, and decaying
  // between closes would corrupt the per-close anchoring. Staleness is
  // instead handled by the activity floor below, which uses a decayed *view*
  // of the close-notional EMS.
  double ntl_long_now = dd_ntl_long_ems_;
  if (dd_close_last_t_ > 0) {
    ntl_long_now *= std::exp(-double(now - dd_close_last_t_) / dd_long_tdc_ms_);
  }

  bool edge_valid = dd_ntl_long_ems_ > 0 && dd_ntl_short_ems_ > 0;
  double edge_long_bps = edge_valid ? 1e4 * dd_pnl_long_ems_ / dd_ntl_long_ems_ : 0;
  double edge_short_bps = edge_valid ? 1e4 * dd_pnl_short_ems_ / dd_ntl_short_ems_ : 0;

  // 1) Hard revert: anything else took over the TL (minfv, wind-down, pause),
  // the long-horizon pattern is outright negative, or (optionally) the
  // short-horizon edge has turned negative — the fastest signal that a good
  // regime just ended.
  bool short_neg = dd_revert_on_short_negative_ && edge_valid && edge_short_bps < 0;
  if (cur_tl_ != TradingLevel::TL0 || (edge_valid && edge_long_bps < 0) || short_neg) {
    if (dd_level_ > 0) {
      setDoubleDownLevel(
          0, fmt::format("hard revert: tl={} edge_long={:.2f}bps edge_short={:.2f}bps",
                         magic_enum::enum_name(cur_tl_), edge_long_bps, edge_short_bps));
    }
    return;
  }

  // 2) Hold check: edge above exit AND recent closed activity above floor.
  bool hold_ok = edge_valid && edge_long_bps >= dd_exit_edge_bps_ &&
                 ntl_long_now >= dd_min_hold_ntl_frac_ * base_max_notional_;
  if (!hold_ok) {
    // Step down at most once per eval period so we don't collapse the ladder
    // in one burst of evals.
    if (dd_level_ > 0 && now - dd_last_level_change_t_ >= dd_eval_period_s_ * 1000) {
      setDoubleDownLevel(dd_level_ - 1, fmt::format("decay: edge_long={:.2f}bps ntl_ems={:.0f}",
                                                    edge_long_bps, ntl_long_now));
    }
    return;
  }
  if (dd_size_mult_ >= dd_max_mult_) {
    return;
  }

  // 3) Step-up check.
  bool warm = dd_closes_ >= dd_warmup_closes_ && dd_first_close_t_ > 0 &&
              now - dd_first_close_t_ >= dd_warmup_ms_;
  double pos_ntl = std::abs(getPosNotional());
  bool churn_ok = std::isfinite(pos_ntl) && dd_own_ntl_ems_ > dd_min_churn_ratio_ * pos_ntl;
  double open_pnl = std::abs(getV2ApproxOpenPnl());
  bool closed_frac_ok =
      dd_min_closed_frac_ <= 0 ||
      std::abs(dd_pnl_long_ems_) / (std::abs(dd_pnl_long_ems_) + open_pnl + 1e-9) >=
          dd_min_closed_frac_;
  bool mkt_ok = dd_max_mkt_frac_ <= 0 ||
                (dd_mkt_ntl_ems_ > 0 && dd_own_ntl_ems_ < dd_max_mkt_frac_ * dd_mkt_ntl_ems_);
  bool edges_ok = edge_long_bps >= dd_enter_edge_bps_ && edge_short_bps >= dd_enter_edge_bps_;
  bool dwell_ok = now - dd_last_level_change_t_ >= dd_dwell_ms_;

  LOG_EVERY_N(INFO, 10) << fmt::format(
      "({}) dd state: lvl={} mult={:.2f} edgeL={:.2f}bps edgeS={:.2f}bps closes={} warm={} "
      "churn={} cfrac={} mkt={} dwell={}",
      symbol_id_.get(), dd_level_, dd_size_mult_, edge_long_bps, edge_short_bps, dd_closes_, warm,
      churn_ok, closed_frac_ok, mkt_ok, dwell_ok);

  if (warm && churn_ok && closed_frac_ok && mkt_ok && edges_ok && dwell_ok) {
    setDoubleDownLevel(dd_level_ + 1,
                       fmt::format("edge_long={:.2f}bps edge_short={:.2f}bps closes={}",
                                   edge_long_bps, edge_short_bps, dd_closes_));
  }
}

void TradeRiskMan::setDoubleDownLevel(int level, const std::string& why) {
  level = std::max(0, level);
  double mult = std::min(dd_max_mult_, std::pow(dd_step_mult_, level));

  if (level > dd_level_) {
    LOG(INFO) << fmt::format("({}) Double-down STEP UP {} -> {} (mult {:.2f} -> {:.2f}): {}",
                             symbol_id_.get(), dd_level_, level, dd_size_mult_, mult, why);
    if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
      pktrade::util::Mailer::instance().send_mail(
          fmt::format("{} {} double-down step up to {:.2f}x", pktrade::GlobalVar::strat_id_,
                      symbol_id_.get(), mult),
          fmt::format("{}\nclosed_pnl: {:.2f}\nconf_size_mult: {}\num_size_mult: {}\n",
                      why, v3_closed_pnl_, conf_size_mult_, um_size_mult_));
    }
  } else {
    LOG(INFO) << fmt::format("({}) Double-down step down {} -> {} (mult {:.2f} -> {:.2f}): {}",
                             symbol_id_.get(), dd_level_, level, dd_size_mult_, mult, why);
    if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_) {
      pktrade::util::Mailer::instance().send_mail(
          fmt::format("{} {} double-down step down to {:.2f}x", pktrade::GlobalVar::strat_id_,
                      symbol_id_.get(), mult),
          fmt::format("{}\nclosed_pnl: {:.2f}\nconf_size_mult: {}\num_size_mult: {}\n",
                      why, v3_closed_pnl_, conf_size_mult_, um_size_mult_));
    }
  }

  dd_level_ = level;
  dd_last_level_change_t_ = time_utils::nowToMs();
  // Apply double-down as its own MultSource slot on both the riskman (max
  // pos/notional) and the ordex (order size). setSizeMult writes dd_size_mult_.
  setSizeMult(mult, MultSource::DoubleDown);
  ordex_->setSizeMult(mult, MultSource::DoubleDown);
}

void TradeRiskMan::doubleDownTimerLoop() {
  evaluateDoubleDown();
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::seconds(dd_eval_period_s_),
                                             [&] { this->doubleDownTimerLoop(); });
}

void TradeRiskMan::setPlaceReason(PKOrderId oid, PlaceReason reason) {
  if (!open_ord_annotations_.contains(oid)) {
    LOG(ERROR) << fmt::format("({}) setPlaceReason: order {} not found in open_ord_annotations_",
                              symbol_id_.get(), oid);
    return;
  }
  open_ord_annotations_[oid].reason = reason;
}

double TradeRiskMan::getMaxNotional() const { return max_notional_; }
double TradeRiskMan::getMaxPosBoundByNotional() const {
  double notional_shares = max_notional_ / mid_sig_->getValue();
  if (std::isfinite(notional_shares)) {
    return std::min(notional_shares, max_pos_.toDouble());
  } else {
    LOG(ERROR) << fmt::format("({}) Got nonfinite notional_shares", symbol_id_.get());
  }
  return max_pos_.toDouble();
}

double TradeRiskMan::getPosNotional() const { return position_.toDouble() * mid_sig_->getValue(); }

double TradeRiskMan::getAvgExecPx() const { return avg_entry_px_; }

double TradeRiskMan::getOutstandingBuyNotional() const { return outstanding_buy_not_; }

double TradeRiskMan::getOutstandingSellNotional() const { return outstanding_sell_not_; }

bool TradeRiskMan::getUseFunding() const { return use_funding_; }

void TradeRiskMan::setSizeMult(double size_mult, MultSource src) {
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
    LOG(ERROR) << fmt::format("({}) Unrecognized MultSource {}, skipping", symbol_id_.get(),
                              magic_enum::enum_name(src));
    return;
  }
  double eff_mult = effSizeMult();

  double new_maxpos = base_max_pos_ * eff_mult;
  double new_max_notional = base_max_notional_ * eff_mult;
  LOG(INFO) << fmt::format(
      "({}) setSizeMult ({} from {}) conf {} um {} dd {} eff {} maxpos {} -> {} "
      "max_notional {} -> {}",
      symbol_id_.get(), size_mult, magic_enum::enum_name(src), conf_size_mult_, um_size_mult_,
      dd_size_mult_, eff_mult, max_pos_.toDouble(), new_maxpos, max_notional_, new_max_notional);
  max_pos_ = Quantity{std::to_string(new_maxpos)};
  max_notional_ = new_max_notional;

  // Also update shortable amount. Might need updating if we enter a world
  // where we need supply and need to read in amounts.
  setShortableAmt(max_pos_);
}

void TradeRiskMan::refreshSizeMultBaseFromCurrent() {
  double denom = effSizeMult();
  if (denom <= 0) {
    denom = 1;
  }
  base_max_pos_ = max_pos_.toDouble() / denom;
  base_max_notional_ = max_notional_ / denom;
  has_base_sizes_ = true;
}

const Quantity TradeRiskMan::getPos() const { return position_; }

const Quantity TradeRiskMan::getOutstandingShares(Side side) const {
  if (side == Side::Buy) {
    return outstanding_buy_;
  }
  return outstanding_sell_;
}

const Quantity TradeRiskMan::getOutstandingCrossShares(Side side) const {
  if (side == Side::Buy) {
    return outstanding_buy_cx_;
  }
  return outstanding_sell_cx_;
}

// Pick up the position at the current mid price.
// This is mostly similar to the onExec fxn, as we're picking up a position.
void TradeRiskMan::addInheritPosition(double inherited_pos) {

  // Nothing to inherit
  if (inherited_pos == 0) {
    LOG(INFO) << fmt::format("({}) Nothing to inherit", symbol_id_.get());
    return;
  }

  // Limit net inherited pos to maxpos.
  if (inherited_pos > 0) {
    inherited_pos = std::min(inherited_pos, (max_pos_ - position_).toDouble());
  } else {
    inherited_pos = std::max(inherited_pos, (-max_pos_ - position_).toDouble());
  }

  // Skip updating tot_traded_ and tot_notional_traded_ -- they aren't used for
  // risk updates.

  // Inherit at the current mid, which will also be used as the "exec" px for pnl calcs
  double mid_px = mid_sig_->getValue();
  if (mid_px == 0) {
    LOG(ERROR) << fmt::format("({}) No mid_px, refusing inheritance", symbol_id_.get());
    return;
  }

  // Round it properly (toward 0) to a Quantity and multiple of lot size
  Quantity inherited_qty = pktrade::GlobalVar::secmaster_->round_qty(
      book_id_, inherited_pos, inherited_pos < 0);
  // Check again in case we capped or rounded to 0
  if (inherited_qty == 0) {
    LOG(INFO) << fmt::format("({}) Inheritance rounded to 0", symbol_id_.get());
    return;
  }

  // Update the position
  double last_pos_f = position_.toDouble();
  position_ += inherited_qty;
  double pos_f = position_.toDouble();

  LOG(INFO) << fmt::format("({}) Inherited position ({}). New position: {}", symbol_id_.get(),
                           inherited_pos, position_.toDouble());

  // Update v3 pnl numbers: tot_entry_notional_, v3_closed_pnl_, avg_entry_px_, etc.
  // Same calc as in onOE (see there for details), except for 2 things:
  // - inherited_pos is already signed, instead of an absolute amount like exec.qty
  // - we just use the mid price as the "exec" price
  double inherited_notional = inherited_pos * mid_px;
  if ((0 < last_pos_f && last_pos_f < pos_f) || // long to longer
      (0 > last_pos_f && last_pos_f > pos_f)) { // short to shorter
    tot_entry_notional_ += inherited_notional;
  } else if ((0 < pos_f && pos_f < last_pos_f) || // long to less long
             (0 > pos_f && pos_f > last_pos_f)) { // short to less short
    tot_entry_notional_ *= (pos_f / last_pos_f);
    v3_closed_pnl_ += inherited_pos * (avg_entry_px_ - mid_px);
  } else {
    // flip position, including to/from 0
    tot_entry_notional_ = pos_f * mid_px;
    v3_closed_pnl_ += last_pos_f * (mid_px - avg_entry_px_);
  }

  // Recalc avg entry price.
  if (pos_f != 0) {
    avg_entry_px_ = tot_entry_notional_ / pos_f;
  }

  //////////////////////////////////////////////////////////
  // PNL calc v2.
  // Just gonna make this easier.
  // running_notional is the amount of our cash balance. If we short, then we
  // add positive amount to that amount. If we buy, we subtract a positve amount
  // to that amount. Whatever excess position we have, there's a little bit of
  // open pnl that we can explain away.

  // Simplified from the calculation in the onexec.
  // Note that this is now -= instead of +=.
  running_notional_basecur_ -= (mid_px * inherited_pos);

  // If we sold, running_notional_basecur_ is positive. Position is negative. So
  // we should add the other side to get the mark-to-mid closed_pnl (approx).
  // All this is done after the position_ gets updated, of course.
  v2_closed_pnl_ = running_notional_basecur_ + (pos_f * mid_px);

  double v3_open_pnl = pos_f * mid_px - tot_entry_notional_;
  double v3_total_pnl = v3_open_pnl + v3_closed_pnl_;
  LOG(INFO) << fmt::format(
      "({}) PNL Calc addInheritPosition: side {} qty {} px {} old_pos {} new_pos {} mid_px {} "
      "tot_entry_notional {} running_notional_basecur {} v3_open_pnl {} v3_closed_pnl {} "
      "v3_total_pnl {} v2_closed_pnl {}",
      symbol_id_.get(), (inherited_pos > 0 ? "Buy" : "Sell"), std::abs(inherited_pos), mid_px,
      last_pos_f, pos_f, mid_px, tot_entry_notional_, running_notional_basecur_, v3_open_pnl,
      v3_closed_pnl_, v3_total_pnl, v2_closed_pnl_);

  // Inheritance can move v3_closed_pnl_; keep it out of the double-down score.
  dd_prev_v3_closed_pnl_ = v3_closed_pnl_;
}

void TradeRiskMan::postSecMaster() {
  // Update the funding master with our positions.
  // Do it in here instead of setOvernight because that happens before
  // the fundingmaster is created. I'm sorry for this annoying sequence.
  pktrade::GlobalVar::funding_master_->updatePosition(book_id_, position_.toDouble());

  // Initialize approx_mid_ with the one-time lookup from secmaster.
  approx_mid_ = pktrade::GlobalVar::secmaster_->get_approx_mid(book_id_);
}

// If pos = 2, and we have 3 limit orders on buy (at diff price levels)
// If pos = 2, and we have 3 limit orders on buy (at diff price levels)
// this returns 5. So what's our exposure to the long side.
const Quantity TradeRiskMan::getMaxFillPosition(Side side, bool only_cx /*=false*/) const {
  if (side == Side::Buy) {
    if (precog_miss_) {
      // Use the premisses too.
      if (outstanding_buy_premisses_.toDouble() != 0) {
        //  std::cout << " outstanding was " << outstanding_buy_.toDouble()
        //          << " now "
        //          << (outstanding_buy_ -
        //          outstanding_buy_premisses_).toDouble()
        //          << " (obp is ) " << outstanding_buy_premisses_.toDouble()
        //          << std::endl;
      }
      if (only_cx) {
        return position_ + (outstanding_buy_cx_ -
                            Quantity{std::to_string(outstanding_buy_premisses_.toDouble() *
                                                    precog_penalty_ratio_)});
      } else {
        return position_ +
               (outstanding_buy_ - Quantity{std::to_string(outstanding_buy_premisses_.toDouble() *
                                                           precog_penalty_ratio_)});
      }
    } else {
      if (only_cx) {
        return position_ + outstanding_buy_cx_;
      } else {
        return position_ + outstanding_buy_;
      }
    }
  }

  if (precog_miss_) {
    // Use the premisses too.
    if (outstanding_sell_premisses_.toDouble() != 0) {
      // std::cout << " outstanding was " << outstanding_sell_.toDouble() << "
      // now "
      //           << (outstanding_sell_ -
      //           outstanding_sell_premisses_).toDouble()
      //             //<< " (osp is ) " <<
      //             outstanding_sell_premisses_.toDouble()
      //           << std::endl;
    }
    if (only_cx) {
      return position_ - (outstanding_sell_cx_ -
                          Quantity{std::to_string(outstanding_sell_premisses_.toDouble() *
                                                  precog_penalty_ratio_)});
    } else {
      return position_ -
             (outstanding_sell_ - Quantity{std::to_string(outstanding_sell_premisses_.toDouble() *
                                                          precog_penalty_ratio_)});
    }
  }
  // else
  if (only_cx) {
    return position_ - outstanding_sell_cx_;
  }
  return position_ - outstanding_sell_;
}

// In the above example. If pos = 2, 3 limit orders on buy, and our maxpos
// is 7. Then, we can still send 2 size before we hit our limits.
const Quantity TradeRiskMan::getSideAlloc(Side side) const {
  if (side == Side::Buy) {
    // Can only trade towards flat.
    if (!can_increase_risk_) {
      // Instead of position. Probably should work with maxFillPosition.
      // TODO: spec this out proper.
      if (position_ >= Quantity{0}) {
        return Quantity{0};
      } else {
        // position as well as outstanding shares.
        // Position < 0, and we also remove the outstanding_buy orders.
        return std::max(-position_ - outstanding_buy_, Quantity{0});
      }
    }

    return Quantity{std::max(max_pos_ - getMaxFillPosition(side), Quantity{0})};
  }

  // Side::Sell

  // Selling towards flat.
  if (!can_increase_risk_) {
    if (position_ <= Quantity{0}) {
      return Quantity{0};
    } else {
      return std::max(position_ - outstanding_sell_, Quantity{0});
    }
  }

  // Selling and incrementing risk.

  Quantity lower_bound = std::min(shortable_amt_, max_pos_);

  // printf("TRM SELL C %f %f %f \n", shortable_amt_.toDouble(),
  // max_pos_.toDouble(),
  //        (lower_bound + getMaxFillPosition(side)).toDouble());

  return Quantity{std::max(lower_bound + getMaxFillPosition(side), Quantity{0})};
}

const Quantity TradeRiskMan::getMaxPos() const { return max_pos_; }

const int TradeRiskMan::getNumPrecoggedMisses(Side side) const {
  if (side == Side::Buy) {
    return num_buy_premisses_;
  }
  return num_sell_premisses_;
}

const Quantity TradeRiskMan::getShortableAmt() const { return shortable_amt_; }

void TradeRiskMan::setShortableAmt(Quantity shortable_amt) { shortable_amt_ = shortable_amt; }

const double TradeRiskMan::getV2NetPnl() const {
  // Get funding too now.
  return v2_closed_pnl_ - commissions_ + net_funding_;
}

const double TradeRiskMan::getV2ClosedPnl() const { return v2_closed_pnl_; }

const double TradeRiskMan::getV2ApproxOpenPnl() const {
  // return
  // OK, let's see if this makes sense.
  // v2_closed_pnl_ - running_notional_basecur_ =
  // position * mid_sig, at the time when the last exec happened
  // The current pnl then just takes the difference from that point.
  // Yeah, I realize this isn't exact and kind of breaks fifo-attribution,
  // it's just that the other way is a bit more error-prone so I want to
  // make sure it's correct first. Anyway,
  // So in this case, if we mark to mid and we have a positive position,
  // we sell at the current price, and then subtract away the "entry" which
  // is approx at the last transaction point. I know this is not idea,
  // sorry.
  // (position_.toDouble() * mid_sig_->getValue()) - (v2_closed_pnl_ - running_notional_basecur_);

  // Using the corrected tot_entry_notional_, I think this should be the correct openPnl
  return (position_.toDouble() * mid_sig_->getValue()) - tot_entry_notional_;
}

const double TradeRiskMan::getFunding() const { return net_funding_; }

const double TradeRiskMan::getCommissions() const { return commissions_; }

const double TradeRiskMan::getMinPnlSeen() const { return min_pnl_seen_; }

const double TradeRiskMan::getShsSent() const { return tot_sent_.toDouble(); }

const double TradeRiskMan::getReachableShsSent() const {
  return tot_sent_.toDouble() - tot_precog_missed_;
}

const double TradeRiskMan::getShsTraded() const { return tot_traded_.toDouble(); }

const double TradeRiskMan::getIOCShsSent() const { return ioc_shs_sent_; }

const double TradeRiskMan::getRmLiqShs() const { return rmliq_shs_; }

const double TradeRiskMan::getNotionalTraded() const { return tot_notional_traded_; }

const int TradeRiskMan::getTimesTraded() const { return times_traded_; }

const int TradeRiskMan::getTimesFlipped() const { return times_flipped_; }

const BaseCurrency TradeRiskMan::getBaseCurrency() const { return base_currency_; }

void TradeRiskMan::setLiveContext(LiveContext* live_context) { live_context_ = live_context; }

void TradeRiskMan::setLocalContext(LocalContext* local_context) { local_context_ = local_context; }

void TradeRiskMan::pauseTrading(double pause_secs, TLReason reason, std::string reason_str) {
  if (getTL(reason) == TradingLevel::TL7) {
    // Already paused, don't do anything.
    return;
  }
  LOG(INFO) << fmt::format("({}) Pausing trading for {} seconds because of {}: '{}'",
                           symbol_id_.get(), pause_secs, magic_enum::enum_name(reason), reason_str);
  // Pause new orders, but don't cancel old ones.
  setTL(TradingLevel::TL7, reason);
  // Set a callback to trade again.
  pktrade::GlobalVar::event_loop_->onTimeout(
      std::chrono::seconds(static_cast<int>(pause_secs)), [this, reason] {
        this->setTL(TradingLevel::TL0, reason);
      });
}

void TradeRiskMan::emergencyCancelOrders() {
  // Iterate through all live orders, send cancels.
  // Do this in two loops bc cancelOrd modifies open_ord_annotations_
  std::vector<PKOrderId> cxl_ids;
  for (const auto& annot_pair : open_ord_annotations_) {
    if (!annot_pair.second.ioc_) { // if IOC, nothing to cancel
      cxl_ids.push_back(annot_pair.first);
    }
  }

  LOG(INFO) << fmt::format("({}) Emergency cancelling all {} orders", symbol_id_.get(),
                           cxl_ids.size());

  for (const auto& cxl_id : cxl_ids) {
    CancelOrder cxl(cxl_id);
    cancelOrd(cxl);
  }
}

// This is only to do precog buys
void TradeRiskMan::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (lvl_mod.qty != Quantity{0}) {
    return;
  }

  if (precog_miss_) {
    int64_t now_ms = time_utils::nowToMs();
    if (lvl_mod.side == Side::Buy) {
      // Check the sells

      for (auto& poi : precogged_sells_) {
        // Match price and no repeat customers.
        if (poi.second.px == lvl_mod.px && !poi.second.precogged &&
            now_ms - poi.second.t_sent < precog_fastest_possible_rtt_) {
          // Do something
          num_sell_premisses_++;
          outstanding_sell_premisses_ += poi.second.qty;
          poi.second.precogged = true;

          ordex_->ordElimPrecog(Side::Sell, poi.second.qty);

          tot_precog_missed_ += poi.second.qty.toDouble();

          /*
            LOG(INFO) << time_utils::nowToStr()
                      << " DeleteTrigger(MOD) Precog tSent " <<
            poi.second.t_sent
                      << " diff " << now_ms - poi.second.t_sent << " oid "
                      << poi.second.pk_order_id << " px "
                      << poi.second.px.toDouble() << " qty "
                      << poi.second.qty.toDouble() << " outstanding_sell "
                      << outstanding_sell_.toDouble()
                      << " outstanding_sell_premisses "
                      << outstanding_sell_premisses_.toDouble() << std::endl;
  */
        }
      }

    } else {
      // Side sell (but for the lvldelete)
      // check the buys

      for (auto& poi : precogged_buys_) {
        if (poi.second.px == lvl_mod.px && !poi.second.precogged &&
            now_ms - poi.second.t_sent < precog_fastest_possible_rtt_) {
          // Do something

          num_buy_premisses_++;
          outstanding_buy_premisses_ += poi.second.qty;
          poi.second.precogged = true;
          ordex_->ordElimPrecog(Side::Buy, poi.second.qty);
          tot_precog_missed_ += poi.second.qty.toDouble();

          /*
          LOG(INFO) << time_utils::nowToStr()
                    << " DeleteTrigger(MOD) Precog tSent " << poi.second.t_sent
                    << " diff " << now_ms - poi.second.t_sent << " oid "
                    << poi.second.pk_order_id << " px "
                    << poi.second.px.toDouble() << " qty "
                    << poi.second.qty.toDouble() << " outstanding_buy "
                    << outstanding_buy_.toDouble()
                    << " outstanding_buy_premisses "
                    << outstanding_buy_premisses_.toDouble() << " id "
                    << poi.second.pk_order_id << " precogged "
                    << poi.second.precogged << std::endl;
                    */
        }
      }
    }
  }
}

// Precog stuff
void TradeRiskMan::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  // Potentially do unflagging here.

  if (precog_miss_) {
    int64_t now_ms = time_utils::nowToMs();
    if (lvl_del.side == Side::Buy) {
      // Check the sells

      for (auto& poi : precogged_sells_) {
        // Match price and no repeat customers.
        if (poi.second.px == lvl_del.px && !poi.second.precogged &&
            now_ms - poi.second.t_sent < precog_fastest_possible_rtt_) {
          // Do something
          num_sell_premisses_++;
          outstanding_sell_premisses_ += poi.second.qty;
          poi.second.precogged = true;

          ordex_->ordElimPrecog(Side::Sell, poi.second.qty);
          tot_precog_missed_ += poi.second.qty.toDouble();
          /*
          LOG(INFO) << time_utils::nowToStr() << " DeleteTrigger Precog tSent "
                    << poi.second.t_sent << " diff "
                    << now_ms - poi.second.t_sent << " oid "
                    << poi.second.pk_order_id << " px "
                    << poi.second.px.toDouble() << " qty "
                    << poi.second.qty.toDouble() << " outstanding_sell "
                    << outstanding_sell_.toDouble()
                    << " outstanding_sell_premisses "
                    << outstanding_sell_premisses_.toDouble() << std::endl;
*/
        }
      }

    } else {
      // Side sell (but for the lvldelete)
      // check the buys

      for (auto& poi : precogged_buys_) {
        if (poi.second.px == lvl_del.px && !poi.second.precogged &&
            now_ms - poi.second.t_sent < precog_fastest_possible_rtt_) {
          // Do something

          num_buy_premisses_++;
          outstanding_buy_premisses_ += poi.second.qty;
          poi.second.precogged = true;
          ordex_->ordElimPrecog(Side::Buy, poi.second.qty);
          tot_precog_missed_ += poi.second.qty.toDouble();

          LOG(INFO) << "(" << symbol_id_.get() << ") " << time_utils::nowToStr()
                    << " DeleteTrigger Precog tSent " << poi.second.t_sent << " diff "
                    << now_ms - poi.second.t_sent << " oid " << poi.second.pk_order_id << " px "
                    << poi.second.px.toDouble() << " qty " << poi.second.qty.toDouble()
                    << " outstanding_buy " << outstanding_buy_.toDouble()
                    << " outstanding_buy_premisses " << outstanding_buy_premisses_.toDouble()
                    << " id " << poi.second.pk_order_id << " precogged " << poi.second.precogged
                    << std::endl;
        }
      }
    }
  }
}

void TradeRiskMan::cleanPrecog(const PKOrderId oid, Side side) {
  if (side == Side::Buy) {
    auto the_pOrd = precogged_buys_.find(oid);
    if (the_pOrd == precogged_buys_.end()) {
      // Nothing to do in this case.
      return;
    }

    // Otherwise, check if we precogged it and remove its stuff from the
    // premisses
    if (the_pOrd->second.precogged) {
      // Remove from the premisses.
      outstanding_buy_premisses_ -= the_pOrd->second.qty;
      num_buy_premisses_--;
      if (outstanding_buy_premisses_ < Quantity{0}) {
        outstanding_buy_premisses_ = Quantity{0};
      }
      if (num_buy_premisses_ < 0) {
        num_buy_premisses_ = 0;
      }
    }
    // Delete it.
    precogged_buys_.erase(oid);

  } else if (side == Side::Sell) {
    auto the_pOrd = precogged_sells_.find(oid);
    if (the_pOrd == precogged_sells_.end()) {
      // Nothing to do in this case.
      return;
    }

    // Otherwise, check if we precogged it and remove its stuff from the
    // premisses
    if (the_pOrd->second.precogged) {
      // Remove from the premisses.
      outstanding_sell_premisses_ -= the_pOrd->second.qty;
      num_sell_premisses_--;
      if (outstanding_sell_premisses_ < Quantity{0}) {
        outstanding_sell_premisses_ = Quantity{0};
      }
      if (num_sell_premisses_ < 0) {
        num_sell_premisses_ = 0;
      }
    }
    // Delete it.
    precogged_sells_.erase(oid);
  }
  /*
  std::cout << " CleanPrecog outstanding_buy " << outstanding_buy_.toDouble()
            << " outstanding_buy_premiss "
            << outstanding_buy_premisses_.toDouble() << " outstanding_sell "
            << outstanding_sell_.toDouble() << " outstanding_sell_premiss "
            << outstanding_sell_premisses_.toDouble() << std::endl;
            */
}

const Order* TradeRiskMan::getOrder(PKOrderId pk_oid) {
  if (pktrade::GlobalVar::live_) {
    if (!pktrade::GlobalVar::paper_trading_mode_) {
      if (pktrade::GlobalVar::use_local_context_) {
        return local_context_->getOrder(pk_oid);
      } else {
        return live_context_->getOrder(pk_oid);
      }
    } else {
      // Otherwise, sim_verse.
      return pktrade::GlobalVar::sim_verse_->getOrder(pk_oid);
    }
  } else {
    // Otherwise, sim_verse.
    return pktrade::GlobalVar::sim_verse_->getOrder(pk_oid);
  }
}

void TradeRiskMan::rmOutstandingShs(Quantity qty, double px, Side side, bool is_cx) {
  if (side == Side::Buy) {
    outstanding_buy_ -= qty;
    outstanding_buy_not_ -= qty.toDouble() * px;
    // Sanity check?
    if (outstanding_buy_ < Quantity(0)) {
      // TODO: probably alert if this happens?
      outstanding_buy_ = Quantity(0);
    }
    if (is_cx) {
      outstanding_buy_cx_ -= qty;
      if (outstanding_buy_cx_ < Quantity(0)) {
        outstanding_buy_cx_ = Quantity(0);
      }
    }

    outstanding_buy_not_ = std::max(0.0, outstanding_buy_not_);

  } else if (side == Side::Sell) {
    outstanding_sell_ -= qty;
    outstanding_sell_not_ -= qty.toDouble() * px;

    // Sanity check?
    if (outstanding_sell_ < Quantity(0)) {
      // TODO: probably alert if this happens?
      outstanding_sell_ = Quantity(0);
    }
    if (is_cx) {
      outstanding_sell_cx_ -= qty;
      if (outstanding_sell_cx_ < Quantity(0)) {
        outstanding_sell_cx_ = Quantity(0);
      }
    }
    outstanding_sell_not_ = std::max(0.0, outstanding_sell_not_);
  }
}

void TradeRiskMan::cleanOldIOCs() {
  // Every so often, go through all my outstanding_ords who are iocs and clean
  // them.

  std::vector<PKOrderId> to_clean;
  // For IOCs, we don't erase annots during elim, so erase them here, but don't redo bookkeeping.
  std::vector<PKOrderId> already_eliminated;

  for (const auto& open_ords : open_ord_annotations_) {
    if (open_ords.second.ioc_ &&
        open_ords.second.t_sent_ < time_utils::nowToMs() - CLEAN_IOC_LIFETIME_MS_) {
      if (open_ords.second.eliminated_) {
        already_eliminated.push_back(open_ords.first);
        continue;
      }
      LOG(INFO) << fmt::format("({}) Cleaning {} from open_ords bc probably expired.",
                               symbol_id_.get(), open_ords.first);
      to_clean.push_back(open_ords.first);
    }
  }

  // These were already eliminated; just erase.
  for (PKOrderId to_del_oid : already_eliminated) {
    open_ord_annotations_.erase(to_del_oid);
  }

  // Erase the ones that need erasing.

  for (PKOrderId to_del_oid : to_clean) {
    const Order* ord = getOrder(to_del_oid);

    rmOutstandingShs(ord->curr_qty, ord->px.toDouble(), ord->side,
                     // always ioc.
                     true);

    // Handle the precogs too.
    cleanPrecog(to_del_oid, ord->side);

    // Send the orderlim

    OrderElimination elim;
    elim.order_id = to_del_oid;
    ordex_->ordElim(*ord, elim);

    live_order_ids_.erase(to_del_oid);
    open_ord_annotations_.erase(to_del_oid);
  }

  // Callback to next time.

  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::seconds(CLEAN_IOC_PERIOD_S_),
                                             [&] { this->cleanOldIOCs(); });
}

} // namespace pktrade::risk
