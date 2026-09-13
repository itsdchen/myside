#include "sim_lvl_inst.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::sim {

// **********************************************************************
// PKSimOrd helpers.

void PKSimOrd::add_add_size(double add_size) {
  // Protect slightly against spammy add/removers.
  if (was_recent_add(add_size)) {
    return;
  }

  if (last_add_sizes.size() < add_sizes_limit) {
    last_add_sizes.push_back(add_size);
  } else {
    last_add_sizes[add_size_idx] = add_size;
    add_size_idx++;
    add_size_idx = add_size_idx % add_sizes_limit;
  }
}

bool PKSimOrd::was_recent_add(double rm_size) {
  // Iterates through the vec.
  for (auto& a : last_add_sizes) {
    if (a == rm_size) {
      return true;
    }
  }
  return false;
}

// **********************************************************************
// Hyperliquid.

HyperliquidSim::HyperliquidSim(SymbolId sym, pktrade::risk::TradeRiskMan* riskman,
                               rapidjson::Value& sim_conf)
    : SimBase(sym, riskman, sim_conf) {
  mkt_ = Market::Hyperliquid;
  // TODO: implement

  // TODO: switch this to fut-specific or something else.
  ord_latency_int_ = (int64_t)(sim_conf["sim_latency_secs"].GetDouble() * 1000);
  ord_latency_ = std::chrono::milliseconds{ord_latency_int_};

  // I don't think we get this info, so I'm going to force this to be false for
  // now.
  use_one_way_latency_ = false;
  if (sim_conf.HasMember("use_one_way_latency")) {
    use_one_way_latency_ = sim_conf["use_one_way_latency"].GetBool();
  }

  
  gtc_ord_latency_int_ = 1200;
  alo_ord_latency_int_ = 550;
  cxl_ord_latency_int_ = 750;
  // From cross sweeps using copper on 20260411.
  ioc_ord_latency_int_ = 700;

  // Defaults for one_way.
  if (use_one_way_latency_) {
    gtc_ord_latency_int_ = 800;
    alo_ord_latency_int_ = 300;
    cxl_ord_latency_int_ = 250;
    ioc_ord_latency_int_ = 800;
  }

  if (sim_conf.HasMember("ioc_ord_latency")) {
    ioc_ord_latency_int_ =
        (int64_t)(sim_conf["ioc_ord_latency"].GetDouble() * 1000);
  }
  ioc_ord_latency_ = std::chrono::milliseconds{ioc_ord_latency_int_};

  if (sim_conf.HasMember("gtc_ord_latency")) {
    gtc_ord_latency_int_ =
        (int64_t)(sim_conf["gtc_ord_latency"].GetDouble() * 1000);
  }
  gtc_ord_latency_ = std::chrono::milliseconds{gtc_ord_latency_int_};

  if (sim_conf.HasMember("alo_ord_latency")) {
    alo_ord_latency_int_ =
        (int64_t)(sim_conf["alo_ord_latency"].GetDouble() * 1000);
  }
  alo_ord_latency_ = std::chrono::milliseconds{alo_ord_latency_int_};

  if (sim_conf.HasMember("cxl_ord_latency")) {
    cxl_ord_latency_int_ =
        (int64_t)(sim_conf["cxl_ord_latency"].GetDouble() * 1000);
  }
  cxl_ord_latency_ = std::chrono::milliseconds{cxl_ord_latency_int_};

  std::cout << fmt::format("owl {} latency {} alo {} cxl {}", 
    use_one_way_latency_, ord_latency_int_, alo_ord_latency_int_, cxl_ord_latency_int_)
    << std::endl;


  cxl_frac_ahead_ = 0.3;
  if (sim_conf.HasMember("cxl_frac_ahead")) {
    cxl_frac_ahead_ = sim_conf["cxl_frac_ahead"].GetDouble();
  }

  if (sim_conf.HasMember("lag_aware_latency")) {
    lag_aware_latency_ = sim_conf["lag_aware_latency"].GetBool();
  }
  if (sim_conf.HasMember("ioc_lag_base_ms")) {
    ioc_lag_base_ms_ = sim_conf["ioc_lag_base_ms"].GetInt64();
  }
  if (sim_conf.HasMember("ioc_lag_coef")) {
    ioc_lag_coef_ = sim_conf["ioc_lag_coef"].GetDouble();
  }
  if (sim_conf.HasMember("alo_lag_base_ms")) {
    alo_lag_base_ms_ = sim_conf["alo_lag_base_ms"].GetInt64();
  }
  if (sim_conf.HasMember("alo_lag_coef")) {
    alo_lag_coef_ = sim_conf["alo_lag_coef"].GetDouble();
  }
  if (sim_conf.HasMember("cxl_lag_base_ms")) {
    cxl_lag_base_ms_ = sim_conf["cxl_lag_base_ms"].GetInt64();
  }
  if (sim_conf.HasMember("cxl_lag_coef")) {
    cxl_lag_coef_ = sim_conf["cxl_lag_coef"].GetDouble();
  }
}

void HyperliquidSim::eod() {
  // TODO: implement
  printf("Hyperliquid EOD: our_cross %d their_cross %d their_add %d\n", n_fills_our_cross_,
         n_fills_other_cross_, n_fills_other_add_);
}

void HyperliquidSim::subscribeData(pktrade::md::MDBeacon* beacon) {
  BookId book_id{Market::Hyperliquid, sym_};
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd, pktrade::md::BeaconCBType::LvlMod,
      pktrade::md::BeaconCBType::LvlDel, pktrade::md::BeaconCBType::Trd};

  beacon->addListener(book_id, this, cb_types);

  bk_ = pktrade::GlobalVar::book_manager_->find(BookId(mkt_, sym_));
  if (bk_ == nullptr) {
    throw std::runtime_error("HyperliquidSim: Could not find book!");
  }

  // Subscribe HYPE trades as an HL-wide feed-lag heartbeat. HYPE trades arrive
  // frequently, so last_feed_lag_ms_ stays fresh even when our symbol is sparse.
  // (Previously used BTC; switched after BTC mktdata storage issues from 2026-06-11.)
  // Only attach if HYPE was loaded into the run.
  if (lag_aware_latency_ && sym_.get() != "HYPE") {
    BookId hype_id{Market::Hyperliquid, SymbolId{"HYPE"}};
    if (pktrade::GlobalVar::book_manager_->find(hype_id) != nullptr) {
      beacon->addListener(hype_id, this,
                          {pktrade::md::BeaconCBType::Trd});
    } else {
      std::cout << "HyperliquidSim " << sym_.get()
                << ": lag_aware_latency=true but HYPE book not loaded; "
                   "lag will only update from this symbol's trades."
                << std::endl;
    }
  }
}

std::vector<BookId> HyperliquidSim::getRequiredDataSubs() {
  if (lag_aware_latency_ && sym_.get() != "HYPE") {
    return {{Market::Hyperliquid, SymbolId{"HYPE"}}};
  }
  return {};
}

void HyperliquidSim::onSentOrder(pktrade::Order* ord) {
  // TODO: implement

  // Effective per-TIF delay. When lag-aware mode is on:
  //   eff = <tif>_lag_base_ms_ + <tif>_lag_coef_ * last_feed_lag_ms_
  // Decomposes fixed network/floor (base) from HL congestion (coef * one-way
  // feed lag observed from any HL trade's transact_t).
  int64_t eff_ioc_lat_int = ioc_ord_latency_int_;
  int64_t eff_alo_lat_int = alo_ord_latency_int_;
  if (lag_aware_latency_) {
    eff_ioc_lat_int = ioc_lag_base_ms_ +
        static_cast<int64_t>(ioc_lag_coef_ * last_feed_lag_ms_);
    eff_alo_lat_int = alo_lag_base_ms_ +
        static_cast<int64_t>(alo_lag_coef_ * last_feed_lag_ms_);
  }
  auto eff_ioc_lat = std::chrono::milliseconds{eff_ioc_lat_int};
  auto eff_alo_lat = std::chrono::milliseconds{eff_alo_lat_int};

  // TODO: when we add liq, support cancels and ish to this too.
  if (use_one_way_latency_) {
    // Lookahead version.

    // TODO: add the correct latency number for this.
    if (ord->time_in_force == TimeInForce::IOC) {
      // Double it or something, idk.
      place_ioc_ords_q_.push_back(ord->pk_order_id);

      place_ioc_transact_ts_.push_back(time_utils::nowToMs() +
                                       eff_ioc_lat_int);

    }

    else if (ord->time_in_force == TimeInForce::GTC) {
      place_gtc_ords_q_.push_back(ord->pk_order_id);
      place_gtc_transact_ts_.push_back(time_utils::nowToMs() +
                                       gtc_ord_latency_int_);

    }

    else if (ord->time_in_force == TimeInForce::ALO) {
      place_alo_ords_q_.push_back(ord->pk_order_id);
      place_alo_transact_ts_.push_back(time_utils::nowToMs() +
                                       eff_alo_lat_int);
    }

  } else {
    // Classic version.
    // Wait some amount of time before it hits book.

    // If it's an ioc, then maybe extend it.
    if (ord->time_in_force == TimeInForce::IOC) {
      // Double it or something, idk.
      pktrade::GlobalVar::event_loop_->onTimeout(
          eff_ioc_lat, [this, ord] { this->onOrderHitsME(ord); });
    }

    else if (ord->time_in_force == TimeInForce::GTC) {
      pktrade::GlobalVar::event_loop_->onTimeout(
          gtc_ord_latency_, [this, ord] { this->onOrderHitsME(ord); });
    }

    else if (ord->time_in_force == TimeInForce::ALO) {
      pktrade::GlobalVar::event_loop_->onTimeout(
          eff_alo_lat, [this, ord] { this->onOrderHitsME(ord); });
    }

    else {
      pktrade::GlobalVar::event_loop_->onTimeout(
          ord_latency_, [this, ord] { this->onOrderHitsME(ord); });
    }
  }
}

void HyperliquidSim::onOrderHitsME(Order* ord) {
  // Shares_remaining is the shares remaining on our order.
  double shares_remaining = ord->curr_qty.toDouble();
  double total_exec_qty = 0;

  // If it was an ALO order, then there's a possibility the market cancels us.
  bool cancelled_alo = false;


  if (ord->side == Side::Buy) {
    // Match against the asks.
    auto& ask_side = bk_->side<SellSide>();

    for (auto& lvl : ask_side) {
      if (ord->order_type == OrderType::Limit && lvl.px > ord->px) {
        // Done.
        break;
      }

      // Otherwise, might execute. Let's check against flagged_shares.
      double flagged_shares = 0;
      if (flagged_shares_.find(lvl.px.toDouble()) != flagged_shares_.end()) {
        // Incorporate the flagged shares.
        flagged_shares = flagged_shares_[lvl.px.toDouble()].qty;
      }

      double avail_liq = effectiveQty(lvl).toDouble() - flagged_shares;
      double exec_qty = 0;
      if (avail_liq > 0) {
        // We can execute.
        exec_qty = std::min(avail_liq, shares_remaining);
      }

      if (exec_qty > 0) {
        // It means we would've matched, which means we would've been cancelled
        // back.
        if (ord->time_in_force == TimeInForce::ALO) {
          cancelled_alo = true;
          exec_qty = 0;
          break;
        }

        // Flag the shares.
        if (flagged_shares_.find(lvl.px.toDouble()) == flagged_shares_.end()) {
          // Then add our own.
          flagged_shares_.emplace(lvl.px.toDouble(),
                                  SimFlaggedShares{exec_qty, time_utils::nowToMs()});
        } else {
          flagged_shares_[lvl.px.toDouble()].qty += exec_qty;
        }

        // Create the execution.
        OrderExecute ex{
            ord->pk_order_id,     Quantity{std::to_string(exec_qty)}, lvl.px, ord->side, false, 0,
            time_utils::nowToMs()};
        pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);

        // Inform the riskman.
        // riskman_->onOE(*ord, ex);
        total_exec_qty += exec_qty;
        shares_remaining -= exec_qty;

        n_fills_our_cross_++;

        // Hard cut.
        if (shares_remaining <= 0) {
          break;
        }
      }
    }

  } else if (ord->side == Side::Sell) {
    // Match against the bids.
    auto& buy_side = bk_->side<BuySide>();

    for (auto& lvl : buy_side) {
     
      if (ord->order_type == OrderType::Limit && lvl.px < ord->px) {
        // Done.
        break;
      }

      // Otherwise, might execute. Let's check against flagged_shares.
      double flagged_shares = 0;
      if (flagged_shares_.find(lvl.px.toDouble()) != flagged_shares_.end()) {
        // Incorporate the flagged shares.
        flagged_shares = flagged_shares_[lvl.px.toDouble()].qty;
      }

      double avail_liq = effectiveQty(lvl).toDouble() - flagged_shares;
      double exec_qty = 0;
      if (avail_liq > 0) {
        // We can execute.
        exec_qty = std::min(avail_liq, shares_remaining);
      }

      if (exec_qty > 0) {
        if (ord->time_in_force == TimeInForce::ALO) {
          cancelled_alo = true;
          exec_qty = 0;
          break;
        }

        // Flag the shares.
        if (flagged_shares_.find(lvl.px.toDouble()) == flagged_shares_.end()) {
          // Then add our own.
          flagged_shares_.emplace(lvl.px.toDouble(),
                                  SimFlaggedShares{exec_qty, time_utils::nowToMs()});
        } else {
          flagged_shares_[lvl.px.toDouble()].qty += exec_qty;
        }

        // Create the execution.
        OrderExecute ex{
            ord->pk_order_id,     Quantity{std::to_string(exec_qty)}, lvl.px, ord->side, false, 0,
            time_utils::nowToMs()};

        pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);
        // Inform the riskman.
        // riskman_->onOE(*ord, ex);
        total_exec_qty += exec_qty;
        shares_remaining -= exec_qty;
        n_fills_our_cross_++;

        // Hard cut.
        if (shares_remaining <= 0) {
          break;
        }
      }
    }
  }

  if (ord->time_in_force == TimeInForce::ALO && cancelled_alo) {
    // Do not add shares to the book, we get cancelled due to ALO.
    OrderElimination oelim(ord->pk_order_id);
    // Send it out.
    // OK, this should really be talking back to the universe.
    // riskman_->onOE(*ord, oelim);
    pktrade::GlobalVar::sim_verse_->handleOrderElimination(oelim);

    return;
  }

  // Potential cancel.
  // Btw. Right this moment, are not supporting add-liq orders.
  if (shares_remaining > 0) {
    if (ord->time_in_force == TimeInForce::GTC || ord->time_in_force == TimeInForce::ALO) {
      // Add liquidity to the book.

      double shares_ahead = 0;
      if (ord->side == Side::Buy) {
        auto& buy_side = bk_->side<BuySide>();

        for (auto& lvl : buy_side) {
          if (lvl.px == ord->px) {
            shares_ahead = (lvl.qty - lvl.flagged_shares).toDouble();
            break;
          }
        }

        buy_add_ords_[ord->px.toDouble()].emplace_back(ord->pk_order_id,
                                                       shares_ahead, 0);
        // Record that this order is now live. For quick lookup when we
        // do cancels.
        live_order_ids_.insert(ord->pk_order_id);

      } else {
        auto& sell_side = bk_->side<SellSide>();

        for (auto& lvl : sell_side) {
          if (lvl.px == ord->px) {
            shares_ahead = (lvl.qty - lvl.flagged_shares).toDouble();
            break;
          }
        }
        sell_add_ords_[ord->px.toDouble()].emplace_back(ord->pk_order_id,
                                                        shares_ahead, 0);
        // Record that this order is now live. For quick lookup when we
        // do cancels.
        live_order_ids_.insert(ord->pk_order_id);
      }
      // Sends back the cancel to the riskman.
      pktrade::GlobalVar::sim_verse_->newOrderAck(ord->pk_order_id);

    } else {
      // Cancel.
      OrderElimination oelim(ord->pk_order_id);
      // Send it out.
      // OK, this should really be talking back to the universe.
      // riskman_->onOE(*ord, oelim);
      pktrade::GlobalVar::sim_verse_->handleOrderElimination(oelim);
    }
  }
}

void HyperliquidSim::maybeOrderHitsBook(int64_t cur_book_transact_t) {
  // Iterate through outstanding ords.

  // std::cout << fmt::format("Hitting maybeOrdHits  time {} cur_idx {} size {}\n",
   //  cur_book_transact_t, place_alo_ords_idx_, place_alo_ords_q_.size()
  // );

  // ALO
  for (uint i = place_alo_ords_idx_; i < place_alo_ords_q_.size(); i++) {
    // Check if it's time to place the order.
    if (cur_book_transact_t >= place_alo_transact_ts_[i]) {
      // Place the order.
      Order* ord =
          pktrade::GlobalVar::sim_verse_->getOrder(place_alo_ords_q_[i]);
      if (ord == nullptr) {
        continue;
      }

      // Place the order.
      onOrderHitsME(ord);
      place_alo_ords_idx_++;
    } else {
      // We're done.
      break;
    }
  }

  // GTC
  for (uint i = place_gtc_ords_idx_; i < place_gtc_ords_q_.size(); i++) {
    // Check if it's time to place the order.
    if (cur_book_transact_t >= place_gtc_transact_ts_[i]) {
      // Place the order.
      Order* ord =
          pktrade::GlobalVar::sim_verse_->getOrder(place_gtc_ords_q_[i]);
      if (ord == nullptr) {
        continue; 
      }

      // Place the order.
      onOrderHitsME(ord);
      place_gtc_ords_idx_++;
    } else {
      // We're done.
      break;
    }
  }

  // IOC

  for (uint i = place_ioc_ords_idx_; i < place_ioc_ords_q_.size(); i++) {
    // Check if it's time to place the order.
    if (cur_book_transact_t >= place_ioc_transact_ts_[i]) {
      // Place the order.
      Order* ord =
          pktrade::GlobalVar::sim_verse_->getOrder(place_ioc_ords_q_[i]);
      if (ord == nullptr) {
        continue; 
      }

      // Place the order.
      onOrderHitsME(ord);
      place_ioc_ords_idx_++;
    } else {
      // We're done.
      break;
    }
  }
}

void HyperliquidSim::onCancelOrder(const CancelOrder& cancel) {
  // Effective cancel delay: same decomposition as IOC/ALO when lag-aware
  // mode is on. Captures HL congestion delaying cancels — important for MM
  // strategies where cancel delay drives adverse selection.
  int64_t eff_cxl_lat_int = cxl_ord_latency_int_;
  if (lag_aware_latency_) {
    eff_cxl_lat_int = cxl_lag_base_ms_ +
        static_cast<int64_t>(cxl_lag_coef_ * last_feed_lag_ms_);
  }
  auto eff_cxl_lat = std::chrono::milliseconds{eff_cxl_lat_int};

  if (use_one_way_latency_) {
    // Lookahead version.
    cxl_ords_q_.push_back(cancel.pk_order_id);
    cxl_transact_t_.push_back(time_utils::nowToMs() + eff_cxl_lat_int);
  } else {
    // Classic version.
    // Wait some amount of time before it hits book.
    pktrade::GlobalVar::event_loop_->onTimeout(
        eff_cxl_lat, [this, cancel_ord_id = cancel.pk_order_id] {
          this->onCancelHitsME(cancel_ord_id);
        });
  }
}

void HyperliquidSim::maybeCxlHitsBook(int64_t cur_book_transact_t) {
  // Iterate through outstanding cxls.
  for (uint i = cxl_ords_idx_; i < cxl_ords_q_.size(); i++) {
    // Check if it's time to place the order.
    if (cur_book_transact_t >= cxl_transact_t_[i]) {
      // Make the cancel.

      onCancelHitsME(cxl_ords_q_[i]);
      cxl_ords_idx_++;
    } else {
      // We're done.
      break;
    }
  }
}

void HyperliquidSim::onCancelHitsME(PKOrderId cancel_ord_id) {
  // Grabs the order.
  Order* ord = pktrade::GlobalVar::sim_verse_->getOrder(cancel_ord_id);
  if (ord == nullptr || ord->curr_qty.toDouble() == 0) {
    // Nothing to do here. We got executed.
    return;
  }

  // In the hyperliquid case, it's possible that the cancel arrives before
  // the order placement. So let's check if we should cancelreject instead.
  // Do this by looking through the place_ords to find one  s that are still
  // outstanding.

  if (live_order_ids_.find(cancel_ord_id) == live_order_ids_.end()) {
    // Then we should cancelreject.

    CancelOrderReject cxl_reject;
    cxl_reject.pk_order_id = cancel_ord_id;
    cxl_reject.reason = "Tried to cancel order before it went live.";
    pktrade::GlobalVar::sim_verse_->handleCancelOrderReject(cxl_reject);
    return;
  }

  // Otherwise, we can actually cancel.
  std::vector<PKSimOrd>* px_sim_ords = nullptr;
  if (ord->side == Side::Buy) {
    px_sim_ords = &buy_add_ords_[ord->px.toDouble()];
  } else {
    px_sim_ords = &sell_add_ords_[ord->px.toDouble()];
  }

  // Removes it from the lvlpiqsimulator

  std::vector<PKSimOrd>::iterator erase_it = px_sim_ords->end();
  for (std::vector<PKSimOrd>::iterator it = px_sim_ords->begin(); it != px_sim_ords->end(); it++) {
    if (it->pk_order_id == cancel_ord_id) {
      erase_it = it;
      break;
    }
  }

  // Not sure when this could happen but maybe it could.
  // Maybe happens if we sent two cancels.
  if (erase_it != px_sim_ords->end()) {
    px_sim_ords->erase(erase_it);
  }

  // Remove from live order tracking.
  live_order_ids_.erase(cancel_ord_id);

  // Sends back the cancel to the riskman.
  pktrade::GlobalVar::sim_verse_->cancelOrderAck(cancel_ord_id);
}

// Beacon callbacks.
void HyperliquidSim::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  last_delta_t_from_mkt_ = time_utils::nowToMs() - lvl_add.transact_t;
  if (lvl_add.side == Side::Buy) {
    bool has_empty_lvl = false;

    // Match against sell ords.
  for (auto simOrdsIt = sell_add_ords_.begin(); simOrdsIt != sell_add_ords_.end(); ++simOrdsIt) {
    
      if (simOrdsIt->second.size() == 0) {
        has_empty_lvl = true;
      }

      if (simOrdsIt->first > lvl_add.px.toDouble()) {
        break;
      }

      for (auto pso_iter = simOrdsIt->second.begin();
           pso_iter != simOrdsIt->second.end();) {
        // automatically trade through all of them.
        Order* src_ord =
            pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

        double exec_shs = src_ord->curr_qty.toDouble();
        // Modify the order
        OrderExecute ex{pso_iter->pk_order_id,
                        Quantity{std::to_string(exec_shs)},
                        src_ord->px,
                        Side::Sell,
                        true,
                        0,
                        time_utils::nowToMs()};
        pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);
        n_fills_other_add_++;
        // Maybe rm this.
        Order* updated_ord =
            pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

        if (updated_ord->state == OrderState::Closed) {
          live_order_ids_.erase(pso_iter->pk_order_id);
          pso_iter = simOrdsIt->second.erase(pso_iter);
        } else {
          pso_iter++;
        }

        if (pso_iter == simOrdsIt->second.end()) {
          break;
        }
      }
    }
    if (has_empty_lvl) {
      // Walk through the sell_ords and clean things up.
      auto it = sell_add_ords_.begin();
      while (it != sell_add_ords_.end()) {
        if (it->second.empty()) {
          it = sell_add_ords_.erase(it);
        } else {
          ++it;
        }
      }
    }

  } else if (lvl_add.side == Side::Sell) {
    bool has_empty_lvl = false;

    // Match against buy ords.
    for (auto simOrdsIt = buy_add_ords_.begin(); simOrdsIt != buy_add_ords_.end(); ++simOrdsIt) {
      if (simOrdsIt->second.size() == 0) {
        has_empty_lvl = true;
      }

      if (simOrdsIt->first < lvl_add.px.toDouble()) {
        break;
      }

      for (auto pso_iter = simOrdsIt->second.begin();
           pso_iter != simOrdsIt->second.end();) {
        // automatically trade through all of them.
        Order* src_ord =
            pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

        double exec_shs = src_ord->curr_qty.toDouble();
        // Modify the order
        OrderExecute ex{pso_iter->pk_order_id,
                        Quantity{std::to_string(exec_shs)},
                        src_ord->px,
                        Side::Buy,
                        true,
                        0,
                        time_utils::nowToMs()};
        pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);
        n_fills_other_add_++;

        // Maybe rm this.
        Order* updated_ord =
            pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);
        if (updated_ord->state == OrderState::Closed) {
          live_order_ids_.erase(pso_iter->pk_order_id);
          pso_iter = simOrdsIt->second.erase(pso_iter);
        } else {
          pso_iter++;
        }

        if (pso_iter == simOrdsIt->second.end()) {
          break;
        }
      }
    }
    if (has_empty_lvl) {
      // Walk through the buy_add_ords and clean things up.
      auto it = buy_add_ords_.begin();
      while (it != buy_add_ords_.end()) {
        if (it->second.empty()) {
          it = buy_add_ords_.erase(it);
        } else {
          ++it;
        }
      }
    }
  }

  if (use_one_way_latency_) {
    if (lvl_add.last_msg_type == LastMsgType::BookTicker ||
        lvl_add.last_msg_type == LastMsgType::Trade) {
      maybeOrderHitsBook(lvl_add.transact_t);
      maybeCxlHitsBook(lvl_add.transact_t);
    }
  }
}

void HyperliquidSim::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  last_delta_t_from_mkt_ = time_utils::nowToMs() - lvl_mod.transact_t;

  // Potentially match against ourselves on the other side.
  if (lvl_mod.qty > lvl_mod.prev_qty) {
    if (lvl_mod.side == Side::Buy) {
      // Match against sell ords.
      bool has_empty_lvl = false;

      for (auto simOrdsIt = sell_add_ords_.begin(); simOrdsIt != sell_add_ords_.end(); ++simOrdsIt) {
          if (simOrdsIt->second.size() == 0) {
          has_empty_lvl = true;
        }

        if (simOrdsIt->first > lvl_mod.px.toDouble()) {
          break;
        }

        for (auto pso_iter = simOrdsIt->second.begin();
             pso_iter != simOrdsIt->second.end();) {
          // automatically trade through all of them.
          Order* src_ord =
              pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

          double exec_shs = src_ord->curr_qty.toDouble();
          // Modify the order
          OrderExecute ex{pso_iter->pk_order_id,
                          Quantity{std::to_string(exec_shs)},
                          src_ord->px,
                          Side::Sell,
                          true,
                          0,
                          time_utils::nowToMs()};
          pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);
          n_fills_other_cross_++;

          // Maybe rm this.
          Order* updated_ord =
              pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);
          if (updated_ord->state == OrderState::Closed) {
            live_order_ids_.erase(pso_iter->pk_order_id);
            pso_iter = simOrdsIt->second.erase(pso_iter);
          } else {
            pso_iter++;
          }

          if (pso_iter == simOrdsIt->second.end()) {
            break;
          }
        }
      }
      if (has_empty_lvl) {
        // Walk through the sell_add_ords and clean things up.
        auto it = sell_add_ords_.begin();
        while (it != sell_add_ords_.end()) {
          if (it->second.empty()) {
            it = sell_add_ords_.erase(it);
          } else {
            ++it;
          }
        }
      }

    } else if (lvl_mod.side == Side::Sell) {
      bool has_empty_lvl = false;

      // Match against buy ords.
      for (auto simOrdsIt = buy_add_ords_.begin(); simOrdsIt != buy_add_ords_.end(); ++simOrdsIt) {
          if (simOrdsIt->second.size() == 0) {
          has_empty_lvl = true;
        }

        if (simOrdsIt->first < lvl_mod.px.toDouble()) {
          break;
        }

        for (auto pso_iter = simOrdsIt->second.begin();
             pso_iter != simOrdsIt->second.end();) {
          // automatically trade through all of them.
          Order* src_ord =
              pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

          double exec_shs = src_ord->curr_qty.toDouble();
          // Modify the order
          OrderExecute ex{pso_iter->pk_order_id,
                          Quantity{std::to_string(exec_shs)},
                          src_ord->px,
                          Side::Buy,
                          true,
                          0,
                          time_utils::nowToMs()};
          pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);
          n_fills_other_cross_++;

          // Maybe rm this.
          Order* updated_ord =
              pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

          if (updated_ord->state == OrderState::Closed) {
            live_order_ids_.erase(pso_iter->pk_order_id);
            pso_iter = simOrdsIt->second.erase(pso_iter);
          } else {
            pso_iter++;
          }

          if (pso_iter == simOrdsIt->second.end()) {
            break;
          }
        }
      }
      if (has_empty_lvl) {
        // Walk through the buy_add_ords and clean things up.
        auto it = buy_add_ords_.begin();
        while (it != buy_add_ords_.end()) {
          if (it->second.empty()) {
            it = buy_add_ords_.erase(it);
          } else {
            ++it;
          }
        }
      }
    }
  }

  // Potentially changes sharesAhead. For level enlargenings, add it to the
  // recently seen sizes.
  if (lvl_mod.qty > lvl_mod.prev_qty) {
    double qty_added = (lvl_mod.qty - lvl_mod.prev_qty).toDouble();
    if (lvl_mod.side == Side::Buy) {
      auto ords = buy_add_ords_.find(lvl_mod.px.toDouble());
      if (ords != buy_add_ords_.end()) {
        // Iterate through these ords and remove all shares_ahead or behind.
        for (auto& pk_sim_ord : ords->second) {
          pk_sim_ord.add_add_size(qty_added);
        }
      }
    } else if (lvl_mod.side == Side::Sell) {
      auto ords = sell_add_ords_.find(lvl_mod.px.toDouble());
      if (ords != sell_add_ords_.end()) {
        // Iterate through these ords and remove all shares_ahead or behind.
        for (auto& pk_sim_ord : ords->second) {
          pk_sim_ord.add_add_size(qty_added);
        }
      }
    }

  } else {
    // Check to decrease sharesahead/behind.
    // printf("%ld: Mod px %f, removing %f sharesAhead for sim at px level\n",
    // time_utils::nowToMs(), lvl_mod.px.toDouble(), (lvl_mod.prev_qty -
    // lvl_mod.qty).toDouble());

    double qty_rm = (lvl_mod.prev_qty - lvl_mod.qty).toDouble();

    // Check if we have any orders.
    if (lvl_mod.side == Side::Buy) {
      auto ords = buy_add_ords_.find(lvl_mod.px.toDouble());
      if (ords != buy_add_ords_.end()) {
        // Iterate through these ords and remove all shares_ahead or behind.
        for (auto& pk_sim_ord : ords->second) {
          double shs_rm_ahead = 0;

          if (pk_sim_ord.was_recent_add(qty_rm)) {
            // Stay 0.
          } else {
            shs_rm_ahead = cxl_frac_ahead_ * (lvl_mod.prev_qty - lvl_mod.qty).toDouble();
          }

          if (shs_rm_ahead > pk_sim_ord.shares_ahead) {
            shs_rm_ahead = pk_sim_ord.shares_ahead;
          }

          // Limit if the sz_remove is

          // Do some sanity check s.t. shares_ahead, shares_behind add up to
          // the right thing.

          // printf("%s Sym %s (Side Buy px %f) For ord %d, %f - %f == %f ()\n",
          //        time_utils::nowToStr().c_str(), sym_.get().c_str(),
          //        lvl_mod.px.toDouble(), pk_sim_ord.pk_order_id,
          //        pk_sim_ord.shares_ahead, shs_rm_ahead,
          //        pk_sim_ord.shares_ahead - shs_rm_ahead);

          pk_sim_ord.shares_ahead -= shs_rm_ahead;
          if (pk_sim_ord.shares_ahead < 0) {
            pk_sim_ord.shares_ahead = 0;
          }
        }
      }
    } else if (lvl_mod.side == Side::Sell) {
      auto ords = sell_add_ords_.find(lvl_mod.px.toDouble());
      if (ords != sell_add_ords_.end()) {
        for (auto& pk_sim_ord : ords->second) {
          double shs_rm_ahead = 0;

          if (pk_sim_ord.was_recent_add(qty_rm)) {
            // Stay 0.
          } else {
            shs_rm_ahead =
                cxl_frac_ahead_ * (lvl_mod.prev_qty - lvl_mod.qty).toDouble();
          }

          if (shs_rm_ahead > pk_sim_ord.shares_ahead) {
            shs_rm_ahead = pk_sim_ord.shares_ahead;
          }

          // printf("%s (Side Sell px %f) For ord %d, %f - %f == %f ()\n",
          //        time_utils::nowToStr().c_str(), lvl_mod.px.toDouble(),
          //        pk_sim_ord.pk_order_id, pk_sim_ord.shares_ahead,
          //        shs_rm_ahead, pk_sim_ord.shares_ahead - shs_rm_ahead);

          pk_sim_ord.shares_ahead -= shs_rm_ahead;
          if (pk_sim_ord.shares_ahead < 0) {
            pk_sim_ord.shares_ahead = 0;
          }
        }
      }
    }
  }

  // Also, check flagged shares.
  auto flagged_shares_lvl = flagged_shares_.find(lvl_mod.px.toDouble());
  if (flagged_shares_lvl != flagged_shares_.end()) {
    // If the level shrank, release any flagged shares that were accounting for
    // that latent removal so we don't permanently discount future liq.
    if (lvl_mod.qty < lvl_mod.prev_qty) {
      double qty_removed = (lvl_mod.prev_qty - lvl_mod.qty).toDouble();
      flagged_shares_lvl->second.qty =
          std::max(0.0, flagged_shares_lvl->second.qty - qty_removed);
      if (flagged_shares_lvl->second.qty == 0) {
        flagged_shares_.erase(flagged_shares_lvl);
      }
    }
  }

  if (use_one_way_latency_) {
    if (lvl_mod.last_msg_type == LastMsgType::BookTicker ||
        lvl_mod.last_msg_type == LastMsgType::Trade) {
      maybeOrderHitsBook(lvl_mod.transact_t);
      maybeCxlHitsBook(lvl_mod.transact_t);
    }
  }
}

void HyperliquidSim::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  last_delta_t_from_mkt_ = time_utils::nowToMs() - lvl_del.transact_t;

  // Potentially changes sharesAhead
  // printf("%ld: lvlDel px %f, clearing sharesAhead for sim at px level\n",
  // time_utils::nowToMs(), lvl_del.px.toDouble());

  // Check if we have any orders.
  if (lvl_del.side == Side::Buy) {
    auto ords = buy_add_ords_.find(lvl_del.px.toDouble());
    if (ords != buy_add_ords_.end()) {
      // Iterate through these ords and remove all shares_ahead or behind.
      for (auto& pk_sim_ord : ords->second) {
        pk_sim_ord.shares_ahead = 0;
      }
    }
  } else if (lvl_del.side == Side::Sell) {
    auto ords = sell_add_ords_.find(lvl_del.px.toDouble());
    if (ords != sell_add_ords_.end()) {
      for (auto& pk_sim_ord : ords->second) {
        pk_sim_ord.shares_ahead = 0;
      }
    }
  }

  // Also, check the flagged shares. If the level is gone we do not flag those
  // shares anymore.
  auto flagged_shares_lvl = flagged_shares_.find(lvl_del.px.toDouble());
  if (flagged_shares_lvl != flagged_shares_.end()) {
    flagged_shares_.erase(lvl_del.px.toDouble());
  }

  if (use_one_way_latency_) {
    if (lvl_del.last_msg_type == LastMsgType::BookTicker ||
        lvl_del.last_msg_type == LastMsgType::Trade) {
      maybeOrderHitsBook(lvl_del.transact_t);
      maybeCxlHitsBook(lvl_del.transact_t);
    }
  }
}

void HyperliquidSim::onTrade(const LevelBook& bk, const Trade& trd) {
  // HL feed lag heartbeat: any HL trade refreshes our notion of
  // server-side delay. Used by onSentOrder when lag_aware_latency_.
  if (trd.transact_t > 0) {
    last_feed_lag_ms_ =
        std::max<int64_t>(0, time_utils::nowToMs() - trd.transact_t);
  }

  // HYPE heartbeat subscription only updates lag; skip the matching path.
  if (bk.getSymbol().get() != sym_.get()) {
    return;
  }

  // Check if it would have crossed us.
  // In a case where it's ambiguous, check where the current bid/ask is.

  if (trd.passive_side == Side::Buy) {
    // Look at our buy ords and match against them.
    bool has_empty_lvl = false;

    double trd_shs_left = trd.qty.toDouble();
    for (auto simOrdsIt = buy_add_ords_.begin(); simOrdsIt != buy_add_ords_.end(); ++simOrdsIt) {

      if (simOrdsIt->second.size() == 0) {
        has_empty_lvl = true;
      }

      // Our order is less than
      if (simOrdsIt->first < trd.px.toDouble()) {
        break;
      }

      for (auto pso_iter = simOrdsIt->second.begin();
           pso_iter != simOrdsIt->second.end();) {
        bool price_cross = trd.px.toDouble() < simOrdsIt->first;

        // Eat through whatever part of the trade consumed queue ahead of us.
        double consumed_ahead =
            std::min(trd_shs_left, pso_iter->shares_ahead);
        if (consumed_ahead > 0) {
          trd_shs_left -= consumed_ahead;
          pso_iter->shares_ahead -= consumed_ahead;
        }

        if (trd_shs_left <= 0) {
          break;
        }

        // Still queue in front and price didn't cross → nothing fills.
        if (!price_cross && pso_iter->shares_ahead > 0) {
          break;
        }

        Order* src_ord =
            pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

        double exec_shs =
            std::min(trd_shs_left, src_ord->curr_qty.toDouble());

        if (exec_shs > 0) {
          // Modify the order
          OrderExecute ex{pso_iter->pk_order_id,
                          Quantity{std::to_string(exec_shs)},
                          src_ord->px,
                          Side::Buy,
                          true,
                          0,
                          time_utils::nowToMs()};
          pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);
          trd_shs_left -= exec_shs;
          n_fills_other_cross_++;
          Order* updated_ord =
              pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

          if (updated_ord->state == OrderState::Closed) {
            live_order_ids_.erase(pso_iter->pk_order_id);
            pso_iter = simOrdsIt->second.erase(pso_iter);
          } else {
            pso_iter++;
          }
        } else {
          pso_iter++;
        }

        if (pso_iter == simOrdsIt->second.end()) {
          break;
        }
      }

      if (trd_shs_left == 0) {
        break;
      }
      // Otherwise, iterate on the ords in that level.
    }
    if (has_empty_lvl) {
      // Walk through the buy_add_ords and clean things up.
      auto it = buy_add_ords_.begin();
      while (it != buy_add_ords_.end()) {
        if (it->second.empty()) {
          it = buy_add_ords_.erase(it);
        } else {
          ++it;
        }
      }
    }

  } else if (trd.passive_side == Side::Sell) {
    // Look at our sell ords.
    double trd_shs_left = trd.qty.toDouble();
    bool has_empty_lvl = false;

    for (auto simOrdsIt = sell_add_ords_.begin(); simOrdsIt != sell_add_ords_.end(); ++simOrdsIt) {
      if (simOrdsIt->second.size() == 0) {
        has_empty_lvl = true;
      }

      // Our order is less than
      if (simOrdsIt->first > trd.px.toDouble()) {
        break;
      }

      for (auto pso_iter = simOrdsIt->second.begin();
           pso_iter != simOrdsIt->second.end();) {
        bool price_cross = trd.px.toDouble() > simOrdsIt->first;

        double consumed_ahead =
            std::min(trd_shs_left, pso_iter->shares_ahead);
        if (consumed_ahead > 0) {
          trd_shs_left -= consumed_ahead;
          pso_iter->shares_ahead -= consumed_ahead;
        }

        if (trd_shs_left <= 0) {
          break;
        }

        if (!price_cross && pso_iter->shares_ahead > 0) {
          break;
        }

        Order* src_ord =
            pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

        double exec_shs =
            std::min(trd_shs_left, src_ord->curr_qty.toDouble());

        if (exec_shs > 0) {
          // Modify the order
          OrderExecute ex{pso_iter->pk_order_id,
                          Quantity{std::to_string(exec_shs)},
                          src_ord->px,
                          Side::Sell,
                          true,
                          0,
                          time_utils::nowToMs()};
          pktrade::GlobalVar::sim_verse_->handleOrderExecute(ex);
          trd_shs_left -= exec_shs;
          n_fills_other_cross_++;
          Order* updated_ord =
              pktrade::GlobalVar::sim_verse_->getOrder(pso_iter->pk_order_id);

          if (updated_ord->state == OrderState::Closed) {
            live_order_ids_.erase(pso_iter->pk_order_id);
            pso_iter = simOrdsIt->second.erase(pso_iter);
          } else {
            pso_iter++;
          }
        } else {
            pso_iter++;
        }

        // This can happen if we call erase.
        if (pso_iter == simOrdsIt->second.end()) {
          break;
        }
      }

      if (trd_shs_left == 0) {
        break;
      }
      // Otherwise, iterate on the ords in that level.
    }
    if (has_empty_lvl) {
      // Walk through the sell_add_ords and clean things up.
      auto it = sell_add_ords_.begin();
      while (it != sell_add_ords_.end()) {
        if (it->second.empty()) {
          it = sell_add_ords_.erase(it);
        } else {
          ++it;
        }
      }
    }
  }

  if (use_one_way_latency_) {
    maybeOrderHitsBook(trd.transact_t);
    maybeCxlHitsBook(trd.transact_t);
  }
}

}  // namespace pktrade::sim
