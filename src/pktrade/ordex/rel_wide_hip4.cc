#include "rel_wide_hip4.h"

#include <fmt/format.h>
#include <glog/logging.h>
#include <magic_enum.hpp>

#include <algorithm>
#include <cmath>

#include "pktrade/context/global_vars.h"
#include "pktrade/risk/trade_risk_man.h"
#include "pktrade/util/hip4_ids.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::ordex {

RelWideHip4::RelWideHip4(SymbolId symbol, const rapidjson::Value& ordex_conf,
                         pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf)
    : Ordex(symbol, ordex_conf) {
  for (const rapidjson::Value& book : ordex_conf["markets"].GetArray()) {
    traded_books_.push_back(
        magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown));
  }
  if (traded_books_.size() != 1) {
    throw std::runtime_error("RelWideHip4 supports exactly one traded market (the YES coin book)");
  }
  traded_bid_ = BookId{traded_books_[0], symbol_};

  max_pos_ = Quantity{ordex_conf["max_pos"].GetString()};
  order_size_ = Quantity{ordex_conf["order_size"].GetString()};

  // Basis / fair value (basis.py).
  if (ordex_conf.HasMember("apply_fraction")) {
    apply_fraction_ = ordex_conf["apply_fraction"].GetDouble();
  }
  if (ordex_conf.HasMember("ema_time_constant_ms")) {
    ema_tdc_ms_ = ordex_conf["ema_time_constant_ms"].GetDouble();
  }

  // Quoting (quotes.py).
  place_thresh_ = ordex_conf["place_thresh"].GetDouble();
  if (ordex_conf.HasMember("rung_threshold_multiplier")) {
    rung_mult_ = ordex_conf["rung_threshold_multiplier"].GetDouble();
  }
  if (ordex_conf.HasMember("max_back_levels")) {
    max_back_levels_ = ordex_conf["max_back_levels"].GetInt();
  }
  if (ordex_conf.HasMember("cancel_buffer")) {
    cancel_buffer_ = ordex_conf["cancel_buffer"].GetDouble();
  }

  // Startup capitalization requirement. The strategy declares how many complete sets it needs
  // (defaults to max_pos); the gateway reads on-chain balances, mints the shortfall, and gates
  // orders on the ticker until it is capitalized. A config override wins over max_pos.
  if (ordex_conf.HasMember("outcome_id")) {
    outcome_id_ = ordex_conf["outcome_id"].GetInt();
  }
  if (ordex_conf.HasMember("startup_complete_sets")) {
    target_complete_sets_ = ordex_conf["startup_complete_sets"].GetDouble();
  }
  cap_enabled_ = (outcome_id_ >= 0);

  // Signals + trade caller (same wiring as RelWideMM2).
  local_sig_ = sf->getByName(ordex_conf["local_sig"].GetString());
  if (local_sig_ == nullptr) {
    throw std::runtime_error("RelWideHip4: local_sig not found");
  }
  local_sig_id_ = local_sig_->getID();
  local_sig_->addListener(this);

  remote_sig_ = sf->getByName(ordex_conf["remote_sig"].GetString());
  if (remote_sig_ == nullptr) {
    throw std::runtime_error("RelWideHip4: remote_sig not found");
  }
  remote_sig_id_ = remote_sig_->getID();
  remote_sig_->addListener(this);

  tc_tempo_ = tf->getByName(ordex_conf["trade_caller"].GetString());
  tc_tempo_->addListener(this);

  LOG(INFO) << fmt::format(
      "({}) RelWideHip4: outcome={} apply_fraction={} ema_tdc_ms={} place_thresh={} "
      "rung_mult={} max_back_levels={} target_sets_override={} (0=use max_pos)",
      symbol_.get(), outcome_id_, apply_fraction_, ema_tdc_ms_, place_thresh_, rung_mult_,
      max_back_levels_, target_complete_sets_);
}

void RelWideHip4::postSecMaster() {
  // Outcome books use a fixed 0.00001 price grid and whole-share sizes (hip4_ids.h), which the
  // secmaster does not publish per-market, so use the HIP-4 constants directly.
  min_tick_ = 1.0 / 100000.0;
  order_size_ =
      Quantity{std::to_string(pktrade::util::hip4::round_outcome_sz(order_size_.toDouble()))};
  max_pos_ = Quantity{std::to_string(pktrade::util::hip4::round_outcome_sz(max_pos_.toDouble()))};
}

void RelWideHip4::postSetRiskMan() {}

void RelWideHip4::subscribeData(pktrade::md::MDBeacon* beacon) {
  // The signals subscribe themselves; nothing extra to add here (we drive off the tempo).
}

void RelWideHip4::onSignalValue(int sig_id, double value) {
  if (sig_id == local_sig_id_) {
    local_mid_ = value;
  } else if (sig_id == remote_sig_id_) {
    remote_mid_ = value;
  }
}

bool RelWideHip4::updatePredPx() {
  // Gate on both feeds being valid (basis.py staleness / mapping gates).
  if (local_sig_ == nullptr || remote_sig_ == nullptr) {
    return false;
  }
  if (!local_sig_->getValid() || !remote_sig_->getValid()) {
    return false;
  }
  local_mid_ = local_sig_->getValue();
  remote_mid_ = remote_sig_->getValue();
  if (local_mid_ <= 0.0 || local_mid_ >= 1.0 || remote_mid_ <= 0.0 || remote_mid_ >= 1.0) {
    return false;
  }

  // Time-aware EMA of the basis (local - reference).
  double raw_basis = local_mid_ - remote_mid_;
  int64_t now = now_fire_t_;
  if (!basis_initialized_) {
    basis_ema_ = raw_basis;
    basis_initialized_ = true;
  } else {
    double dt = static_cast<double>(now - basis_last_t_);
    if (dt < 0) {
      dt = 0;
    }
    double alpha = 1.0 - std::exp(-dt / ema_tdc_ms_);
    basis_ema_ = alpha * raw_basis + (1.0 - alpha) * basis_ema_;
  }
  basis_last_t_ = now;

  // pred_px = reference_mid + apply_fraction * basis, clipped to [0, 1].
  pred_px_ = std::clamp(remote_mid_ + apply_fraction_ * basis_ema_, 0.0, 1.0);
  return true;
}

void RelWideHip4::adjustPredPx() {
  // Apply inventory skew to pred_px_ at quote time (quotes.py): a long position shifts the price
  // we quote around DOWN so we lean to sell it off, and vice versa.
  //   skew = (directional_shares / order_size) * 0.5 * place_thresh
  double order_sz = std::max(1.0, order_size_.toDouble());
  double pos = riskman_ == nullptr ? 0.0 : riskman_->getPos().toDouble();
  double eff_thresh = place_thresh_ * thresh_mult_;
  double skew = (pos / order_sz) * 0.5 * eff_thresh;
  adjusted_pred_px_ = std::clamp(pred_px_ - skew, 0.0, 1.0);
}

double RelWideHip4::roundPxToSide(double px, bool round_up) {
  // Like Ordex::roundToSide, but onto the fixed 5dp HIP-4 outcome grid (the secmaster tick isn't
  // configured for outcomes). Biased so buys round down and sells round up (never self-cross).
  double scaled = px / min_tick_;
  double ticks = round_up ? std::ceil(scaled) : std::floor(scaled);
  return pktrade::util::hip4::round_outcome_px(ticks * min_tick_);
}

bool RelWideHip4::shouldCancelPx(double px, Side side) const {
  // Cancel a resting order once its price has drifted past cancel_buffer_ * place_thresh from
  // every desired rung on its side (asymmetric hold band around the ladder). Mirrors the role of
  // RelWideMM2::shouldCancelPx (there vs pred_px +/- cancel_thresh; here vs the adjusted_pred_px rungs).
  double eff_thresh = place_thresh_ * thresh_mult_;
  for (int level = 0; level < max_back_levels_; ++level) {
    double distance = eff_thresh * (1.0 + level * rung_mult_);
    double rung = (side == Side::Buy) ? (adjusted_pred_px_ - distance) : (adjusted_pred_px_ + distance);
    if (std::abs(px - rung) <= cancel_buffer_ * eff_thresh) {
      return false;  // still near a desired rung -> keep
    }
  }
  return true;
}

void RelWideHip4::maybeCancel() {
  auto cancel_side = [&](std::vector<SimpleOrder>& resting, Side side) {
    for (auto& o : resting) {
      if (o.state == SimpleOrderState::CANCEL_INFLIGHT) {
        continue;
      }
      if (shouldCancelPx(o.px.toDouble(), side)) {
        CancelOrder cxl{o.pk_oid};
        riskman_->cancelOrd(cxl);
        o.state = SimpleOrderState::CANCEL_INFLIGHT;
        o.cancel_time = now_fire_t_;
      }
    }
  };
  cancel_side(buy_orders_, Side::Buy);
  cancel_side(sell_orders_, Side::Sell);
}

void RelWideHip4::placeRung(Side side, double px, std::vector<SimpleOrder>& resting) {
  if (px <= 0.0 || px >= 1.0) {
    return;
  }
  // Skip if we already have a live order at this price.
  for (auto& o : resting) {
    if (o.state == SimpleOrderState::LIVE && std::abs(o.px.toDouble() - px) < 0.5 * min_tick_) {
      return;
    }
  }
  // Outcome sizes are whole shares (hip4_ids.h); secmaster has no per-outcome lot, so round here.
  double order_qty_d = pktrade::util::hip4::round_outcome_sz(order_size_.toDouble() * size_mult_);
  if (order_qty_d <= 0.0) {
    return;
  }
  // Respect max position on this side.
  if (riskman_->getSideAlloc(side).toDouble() < order_qty_d) {
    return;
  }
  // Min-notional guard: a HIP-4 order must clear $10 notional. We quote the YES coin directly,
  // so both a bid and an ask have notional px * size.
  if (px * order_qty_d < 10.0) {
    return;
  }
  Quantity order_qty = Quantity{std::to_string(order_qty_d)};
  NewOrder ord{symbol_,          traded_books_[0], side,  order_qty, Price{std::to_string(px)},
               OrderType::Limit, TimeInForce::ALO, false, false};
  PKOrderId pk = riskman_->sendOrd(ord);
  if (pk != -1) {
    resting.emplace_back(SimpleOrder{pk, side, Price{std::to_string(px)}, order_qty,
                                     SimpleOrderState::LIVE, now_fire_t_, 0});
  }
}

void RelWideHip4::maybePlaceFront() {
  // Front (inside) rung at level 0: adjusted_pred_px +/- place_thresh.
  double eff_thresh = place_thresh_ * thresh_mult_;
  placeRung(Side::Buy, roundPxToSide(adjusted_pred_px_ - eff_thresh, false), buy_orders_);
  placeRung(Side::Sell, roundPxToSide(adjusted_pred_px_ + eff_thresh, true), sell_orders_);
}

void RelWideHip4::manageBacklevels() {
  // Back rungs at levels 1..max_back_levels_-1, spaced by rung_mult_.
  double eff_thresh = place_thresh_ * thresh_mult_;
  for (int level = 1; level < max_back_levels_; ++level) {
    double distance = eff_thresh * (1.0 + level * rung_mult_);
    placeRung(Side::Buy, roundPxToSide(adjusted_pred_px_ - distance, false), buy_orders_);
    placeRung(Side::Sell, roundPxToSide(adjusted_pred_px_ + distance, true), sell_orders_);
  }
}

void RelWideHip4::emitCapitalRequirement() {
  // Declare our capitalization requirement to the gateway once. Target defaults to max_pos
  // (the inventory needed to quote asks up to our limit); a config override wins. The gateway
  // reads on-chain balances, mints the shortfall, and refuses to place for this ticker until
  // it is capitalized (and if capitalization fails).
  double target = (target_complete_sets_ > 0.0) ? target_complete_sets_ : max_pos_.toDouble();
  target = pktrade::util::hip4::round_outcome_sz(target);
  CapitalReq req;
  req.outcome = outcome_id_;
  req.target_complete_sets = Quantity{std::to_string(target)};
  riskman_->sendCapitalReq(req);
  cap_sent_ = true;
  LOG(INFO) << fmt::format("({}) RelWideHip4: sent capital req outcome {} target_sets {}",
                           symbol_.get(), outcome_id_, target);
}

void RelWideHip4::tryFire() {
  now_fire_t_ = time_utils::nowToMs();

  // If the outcome has resolved / gone off-venue, we've cancelled and we stay stopped. The
  // gateway is the correctness backstop (it rejects all orders for the coin); this just stops us
  // from churning place/reject against it.
  if (resolved_) {
    return;
  }

  // Declare our capitalization requirement once, before quoting.
  if (cap_enabled_ && !cap_sent_) {
    emitCapitalRequirement();
  }

  if (!updatePredPx()) {
    // Not ready / stale reference -> cancel-only (hip4maker cancel-only branch).
    cancelOutstandingOrds();
    return;
  }

  adjustPredPx();
  maybeCancel();
  maybePlaceFront();
  manageBacklevels();
}

void RelWideHip4::removeOrder(PKOrderId oid) {
  auto erase_from = [&](std::vector<SimpleOrder>& v) {
    v.erase(std::remove_if(v.begin(), v.end(),
                           [&](const SimpleOrder& o) { return o.pk_oid == oid; }),
            v.end());
  };
  erase_from(buy_orders_);
  erase_from(sell_orders_);
}

void RelWideHip4::ordCancel(const Order& ord) { removeOrder(ord.pk_order_id); }

void RelWideHip4::ordExec(const Order& ord, const OrderExecute& exec) {
  // Position is tracked by the risk manager; here we just drop fully-closed orders so the
  // ladder reconciles cleanly next fire.
  if (ord.state == OrderState::Closed || ord.curr_qty.toDouble() <= 0.0) {
    removeOrder(ord.pk_order_id);
  }
}

void RelWideHip4::ordElim(const Order& ord, const OrderElimination& elim) {
  removeOrder(elim.order_id);
}

void RelWideHip4::ordReject(const Order& ord, const NewOrderReject& rej) {
  removeOrder(ord.pk_order_id);

  // Terminal wind-down: the gateway rejects orders for an outcome that is no longer live on its
  // deployer venue (settled / expired / voided) with a reason carrying this marker. Once we see
  // it, cancel everything and stop quoting for good (outcomes never un-resolve). The marker must
  // stay in sync with the gateway (wsgateway.py OUTCOME_NOT_LIVE_MARKER). Note the transient
  // startup reject "outcome gate not ready" deliberately does NOT contain "not live".
  if (!resolved_ && rej.reason.find("outcome not live") != std::string::npos) {
    resolved_ = true;
    LOG(WARNING) << fmt::format(
        "({}) RelWideHip4: outcome not live / resolved -- winding down (cancelling resting "
        "orders and stopping quoting). Gateway reason: {}",
        symbol_.get(), rej.reason);
    cancelOutstandingOrds();
    return;
  }

  LOG(ERROR) << fmt::format("({}) RelWideHip4: order reject: {}", symbol_.get(), rej.reason);
}

void RelWideHip4::cancelOutstandingOrds() {
  auto cancel_all = [&](std::vector<SimpleOrder>& v) {
    for (auto& o : v) {
      if (o.state == SimpleOrderState::CANCEL_INFLIGHT) {
        continue;
      }
      CancelOrder cxl{o.pk_oid};
      riskman_->cancelOrd(cxl);
      o.state = SimpleOrderState::CANCEL_INFLIGHT;
      o.cancel_time = now_fire_t_;
    }
  };
  cancel_all(buy_orders_);
  cancel_all(sell_orders_);
}

} // namespace pktrade::ordex
