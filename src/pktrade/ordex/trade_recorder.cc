#include "trade_recorder.h"

#include <fmt/format.h>
#include <glog/logging.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::ordex {

TradeRecorder::TradeRecorder(SymbolId symbol, const rapidjson::Value& ordex_conf,
                             pktrade::signals::SignalFactory* sf,
                             pktrade::tempos::TempoFactory* tf)
    : Ordex(symbol, ordex_conf) {

  for (const rapidjson::Value& book : ordex_conf["markets"].GetArray()) {
    traded_books_.push_back(
        magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown));
  }

  if (traded_books_.size() != 1) {
    throw std::runtime_error("TradeRecorder only supports trading one market at a time.");
  }
  traded_bid_ = BookId{traded_books_[0], symbol_};

  // Hardcoded min_tick override.
  min_tick_ = 0;
  if (ordex_conf.HasMember("hardcoded_min_tick")) {
    min_tick_ = ordex_conf["hardcoded_min_tick"].GetDouble();
  }

  // Delay config.
  if (ordex_conf.HasMember("maker_delay_s")) {
    maker_delay_s_ = ordex_conf["maker_delay_s"].GetDouble();
  }
  if (ordex_conf.HasMember("taker_delay_s")) {
    taker_delay_s_ = ordex_conf["taker_delay_s"].GetDouble();
  }

  // Signals.
  local_sig_ = sf->getByName(ordex_conf["local_sig"].GetString());
  remote_sig_ = sf->getByName(ordex_conf["remote_sig"].GetString());

  if (local_sig_ == nullptr) {
    throw std::runtime_error("TradeRecorder: local_sig not successfully created");
  }
  if (remote_sig_ == nullptr) {
    throw std::runtime_error("TradeRecorder: remote_sig not successfully created");
  }

  local_sig_idx_ = local_sig_->getID();
  remote_sig_idx_ = remote_sig_->getID();
  local_sig_->addListener(this);
  remote_sig_->addListener(this);

  // Trade caller tempo (fires tryFire which is no-op).
  tc_tempo_ = tf->getByName(ordex_conf["trade_caller"].GetString());
  tc_tempo_->addListener(this);
  tc_tempo_id_ = tc_tempo_->getID();

  // Snapshot tempo (100ms timer for feature ring buffer).
  auto* snapshot_tempo = tf->getByName(ordex_conf["snapshot_tempo"].GetString());
  snapshot_tempo->addListener(this);
  snapshot_tempo_id_ = snapshot_tempo->getID();

  LOG(INFO) << fmt::format(
      "({}) TradeRecorder: maker_delay {}s taker_delay {}s",
      symbol_.get(), maker_delay_s_, taker_delay_s_);
}

TradeRecorder::~TradeRecorder() { flushCsv(); }

void TradeRecorder::postSecMaster() {
  if (min_tick_ == 0) {
    min_tick_ = pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble();
  }
  LOG(INFO) << fmt::format("({}) TradeRecorder PostSecmaster: min_tick {}",
                           symbol_.get(), min_tick_);
}

void TradeRecorder::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Subscribe to local book trades.
  std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Trd};
  for (auto& book_id : local_sig_->getBookIds()) {
    beacon->addListener(book_id, this, cb_types);
  }
}

signals::Signal* TradeRecorder::getPredSignal() { return remote_sig_; }
signals::Signal* TradeRecorder::getMidSignal() { return local_sig_; }

void TradeRecorder::onTempo(int tempo_id) {
  if (tempo_id == snapshot_tempo_id_) {
    snapshotFeatures();
  }
  // Trade caller tempo: no-op (tryFire is no-op).
}

void TradeRecorder::snapshotFeatures() {
  int64_t now = time_utils::nowToMs();

  FeatureSnapshot snap{};
  snap.time_ms = now;
  snap.remote_mid = remote_sig_val_;
  snap.local_mid = local_sig_val_;

  // Spread.
  double bb = local_sig_->getBBMeasure();
  double ba = local_sig_->getBAMeasure();
  snap.spread = (bb > 0 && ba > 0) ? (ba - bb) : avg_local_spread_;
  snap.bid_size = 0;
  snap.ask_size = 0;

  // Snapshot book sizes if available.
  const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);
  if (bk && !bk->nonFull()) {
    if (!bk->side<BuySide>().empty()) {
      snap.bid_size = bk->side<BuySide>().begin()->qty.toDouble();
    }
    if (!bk->side<SellSide>().empty()) {
      snap.ask_size = bk->side<SellSide>().begin()->qty.toDouble();
    }
  }

  // Vol EMS.
  snap.vol_100ms = abs_remote_mid_move_ems_100ms_;
  snap.vol_1s = abs_remote_mid_move_ems_1s_;
  snap.vol_4s = abs_remote_mid_move_ems_4s_;
  snap.vol_10s = abs_remote_mid_move_ems_10s_;
  snap.vol_30s = abs_remote_mid_move_ems_30s_;
  snap.vol_60s = abs_remote_mid_move_ems_60s_;
  snap.vol_300s = abs_remote_mid_move_ems_300s_;

  // Momentum EMS.
  snap.mom_100ms = momentum_ems_100ms_;
  snap.mom_1s = momentum_ems_1s_;
  snap.mom_4s = momentum_ems_4s_;
  snap.mom_10s = momentum_ems_10s_;
  snap.mom_30s = momentum_ems_30s_;

  // Trade microstructure EMS.
  snap.trade_rate = trade_rate_ems_;
  snap.trade_size = trade_size_ems_;
  snap.trade_notional_signed = trade_notional_signed_ems_;
  snap.trade_notional_abs = trade_notional_abs_ems_;
  snap.book_depth_bid = book_depth_bid_ems_;
  snap.book_depth_ask = book_depth_ask_ems_;
  snap.buy_trade_depth = buy_trade_depth_ems_;
  snap.sell_trade_depth = sell_trade_depth_ems_;
  snap.large_trade_rate = large_trade_rate_ems_;
  snap.trade_depth = trade_depth_ems_;

  feature_ring_buffer_.push_back(snap);

  // Trim entries older than 5s.
  while (!feature_ring_buffer_.empty() &&
         feature_ring_buffer_.front().time_ms < now - kRingBufferMaxMs) {
    feature_ring_buffer_.pop_front();
  }
}

FeatureSnapshot TradeRecorder::lookupDelayedFeatures(int64_t now_ms, double delay_s) {
  int64_t target_ms = now_ms - static_cast<int64_t>(delay_s * 1000);
  FeatureSnapshot best{};
  int64_t best_diff = INT64_MAX;

  for (const auto& snap : feature_ring_buffer_) {
    int64_t diff = std::abs(snap.time_ms - target_ms);
    if (diff < best_diff) {
      best_diff = diff;
      best = snap;
    }
  }

  return best;
}

void TradeRecorder::onTrade(const LevelBook& bk, const Trade& trd) {
  int64_t now_t = time_utils::nowToMs();
  double trade_qty = trd.qty.toDouble();
  double trade_px = trd.px.toDouble();

  // Update trade microstructure EMS (same as diagnostics_mm.cc).
  double decay = 0;
  if (last_local_vol_t_ > 0) {
    double dt = now_t - last_local_vol_t_;
    decay = std::exp(-dt / trade_micro_tdc_ms_);
  }

  trade_rate_ems_ = 1.0 + decay * trade_rate_ems_;
  trade_size_ems_ = trade_qty + decay * trade_size_ems_;

  double trade_notional = trade_px * trade_qty;
  double sign = (trd.passive_side == Side::Buy) ? -1.0 : 1.0;
  trade_notional_signed_ems_ = sign * trade_notional + decay * trade_notional_signed_ems_;
  trade_notional_abs_ems_ = trade_notional + decay * trade_notional_abs_ems_;

  // Large trade detection.
  double avg_size = (trade_rate_ems_ > 1) ? trade_size_ems_ / trade_rate_ems_ : trade_qty;
  if (trade_qty > 2.0 * avg_size) {
    large_trade_rate_ems_ = 1.0 + decay * large_trade_rate_ems_;
  } else {
    large_trade_rate_ems_ = decay * large_trade_rate_ems_;
  }

  // Trade depth.
  if (!bk.nonFull()) {
    double best_bid = bk.side<BuySide>().begin()->px.toDouble();
    double best_ask = bk.side<SellSide>().begin()->px.toDouble();
    double mid = (best_bid + best_ask) / 2.0;
    if (mid > 0) {
      double depth_px = 0;
      if (trd.passive_side == Side::Buy) {
        depth_px = std::max(best_bid - trade_px, 0.0);
      } else if (trd.passive_side == Side::Sell) {
        depth_px = std::max(trade_px - best_ask, 0.0);
      }
      double depth_rel = depth_px / mid;
      trade_depth_ems_ = depth_rel + decay * trade_depth_ems_;

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

      // Book depth within 5 ticks.
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
    }
  }

  last_local_vol_t_ = now_t;

  // Aggregate by transact_t.
  int64_t this_transact = trd.transact_t;

  if (has_pending_agg_ && this_transact != cur_transact_t_) {
    // New transact_t arrived, flush the previous aggregation.
    flushAggregatedTrade();
  }

  if (!has_pending_agg_) {
    // Start new aggregation.
    has_pending_agg_ = true;
    cur_transact_t_ = this_transact;
    agg_px_sum_ = 0;
    agg_qty_sum_ = 0;
    agg_signed_qty_ = 0;
    agg_n_trades_ = 0;
    agg_time_ms_ = now_t;
  }

  agg_px_sum_ += trade_px * trade_qty;
  agg_qty_sum_ += trade_qty;
  agg_signed_qty_ += sign * trade_qty;
  agg_n_trades_++;
}

void TradeRecorder::flushAggregatedTrade() {
  if (!has_pending_agg_ || agg_qty_sum_ <= 0) {
    has_pending_agg_ = false;
    return;
  }

  TradeEventEntry entry{};
  entry.time_ms = agg_time_ms_;
  entry.agg_px = agg_px_sum_ / agg_qty_sum_;  // VWAP
  entry.agg_qty = agg_qty_sum_;
  entry.net_signed_qty = agg_signed_qty_;
  entry.n_trades = agg_n_trades_;
  entry.remote_mid_at_trade = remote_sig_val_;

  // Lookup delayed features.
  entry.maker_features = lookupDelayedFeatures(agg_time_ms_, maker_delay_s_);
  entry.taker_features = lookupDelayedFeatures(agg_time_ms_, taker_delay_s_);

  pending_markouts_.push_back(entry);
  has_pending_agg_ = false;
}

void TradeRecorder::onSignalValue(int sig_id, double value) {
  if (sig_id == remote_sig_idx_) {
    if (value != 0) {
      // Update vol EMS at all time decay constants.
      if (prev_remote_sig_val_ != 0 && last_remote_mid_t_ != 0) {
        double ret = (value - prev_remote_sig_val_) / prev_remote_sig_val_;
        double dt = time_utils::nowToMs() - last_remote_mid_t_;
        double abs_ret = std::abs(ret);

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

        momentum_ems_100ms_ = ret + std::exp(-dt / 100.0) * momentum_ems_100ms_;
        momentum_ems_1s_ = ret + std::exp(-dt / 1000.0) * momentum_ems_1s_;
        momentum_ems_4s_ = ret + std::exp(-dt / 4000.0) * momentum_ems_4s_;
        momentum_ems_10s_ = ret + std::exp(-dt / 10000.0) * momentum_ems_10s_;
        momentum_ems_30s_ = ret + std::exp(-dt / 30000.0) * momentum_ems_30s_;
      }
      prev_remote_sig_val_ = value;
      remote_sig_val_ = value;
      last_remote_mid_t_ = time_utils::nowToMs();

      // Process pending markouts against new remote mid.
      if (!pending_markouts_.empty()) {
        processPendingMarkouts(time_utils::nowToMs(), value);
      }
    }
  }

  if (sig_id == local_sig_idx_) {
    local_sig_val_ = value;
    last_mid_t_ = time_utils::nowToMs();

    // Track average local spread.
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

void TradeRecorder::processPendingMarkouts(int64_t now_ms, double cur_remote_mid) {
  auto it = pending_markouts_.begin();
  while (it != pending_markouts_.end()) {
    auto& e = *it;
    int64_t elapsed = now_ms - e.time_ms;
    for (int i = 0; i < TradeEventEntry::kNumHorizons; ++i) {
      if (!e.horizon_done[i] && elapsed >= TradeEventEntry::kHorizonMs[i]) {
        e.horizon_remote_mid[i] = cur_remote_mid;
        e.horizon_done[i] = true;
        e.horizons_filled++;
      }
    }
    if (e.horizons_filled >= TradeEventEntry::kNumHorizons) {
      writeCsvRow(e);
      it = pending_markouts_.erase(it);
    } else {
      ++it;
    }
  }
}

void TradeRecorder::openCsv() {
  if (csv_opened_) return;
  std::string path = fmt::format("{}/trade_record_{}.csv",
                                  pktrade::GlobalVar::out_dir_,
                                  pktrade::GlobalVar::date_);
  csv_.open(path);
  if (!csv_.is_open()) {
    LOG(WARNING) << fmt::format("({}) Failed to open trade record CSV: {}", symbol_.get(), path);
    return;
  }
  csv_ << "time_ms,agg_px,agg_qty,net_signed_qty,n_trades,remote_mid_at_trade,"
       << "mk_remote_mid,mk_local_mid,mk_spread,mk_bid_size,mk_ask_size,"
       << "mk_vol_100ms,mk_vol_1s,mk_vol_4s,mk_vol_10s,mk_vol_30s,mk_vol_60s,mk_vol_300s,"
       << "mk_mom_100ms,mk_mom_1s,mk_mom_4s,mk_mom_10s,mk_mom_30s,"
       << "mk_trade_rate,mk_trade_size,mk_trade_notional_signed,mk_trade_notional_abs,"
       << "mk_book_depth_bid,mk_book_depth_ask,mk_buy_trade_depth,mk_sell_trade_depth,"
       << "mk_large_trade_rate,mk_trade_depth,"
       << "tk_remote_mid,tk_local_mid,tk_spread,tk_bid_size,tk_ask_size,"
       << "tk_vol_100ms,tk_vol_1s,tk_vol_4s,tk_vol_10s,tk_vol_30s,tk_vol_60s,tk_vol_300s,"
       << "tk_mom_100ms,tk_mom_1s,tk_mom_4s,tk_mom_10s,tk_mom_30s,"
       << "tk_trade_rate,tk_trade_size,tk_trade_notional_signed,tk_trade_notional_abs,"
       << "tk_book_depth_bid,tk_book_depth_ask,tk_buy_trade_depth,tk_sell_trade_depth,"
       << "tk_large_trade_rate,tk_trade_depth,"
       << "markout_30s,markout_300s"
       << "\n";
  csv_opened_ = true;
  LOG(INFO) << fmt::format("({}) Opened trade record CSV: {}", symbol_.get(), path);
}

void TradeRecorder::writeCsvRow(const TradeEventEntry& e) {
  if (!csv_opened_) openCsv();
  if (!csv_.is_open()) return;

  auto writeFeatures = [&](const FeatureSnapshot& f) {
    csv_ << f.remote_mid << ","
         << f.local_mid << ","
         << f.spread << ","
         << f.bid_size << ","
         << f.ask_size << ","
         << f.vol_100ms << ","
         << f.vol_1s << ","
         << f.vol_4s << ","
         << f.vol_10s << ","
         << f.vol_30s << ","
         << f.vol_60s << ","
         << f.vol_300s << ","
         << f.mom_100ms << ","
         << f.mom_1s << ","
         << f.mom_4s << ","
         << f.mom_10s << ","
         << f.mom_30s << ","
         << f.trade_rate << ","
         << f.trade_size << ","
         << f.trade_notional_signed << ","
         << f.trade_notional_abs << ","
         << f.book_depth_bid << ","
         << f.book_depth_ask << ","
         << f.buy_trade_depth << ","
         << f.sell_trade_depth << ","
         << f.large_trade_rate << ","
         << f.trade_depth << ",";
  };

  csv_ << e.time_ms << ","
       << e.agg_px << ","
       << e.agg_qty << ","
       << e.net_signed_qty << ","
       << e.n_trades << ","
       << e.remote_mid_at_trade << ",";

  writeFeatures(e.maker_features);
  writeFeatures(e.taker_features);

  // Markout columns: remote_mid at horizon - remote_mid at trade time.
  for (int i = 0; i < TradeEventEntry::kNumHorizons; ++i) {
    if (e.horizon_done[i]) {
      csv_ << e.horizon_remote_mid[i];
    }
    if (i < TradeEventEntry::kNumHorizons - 1) {
      csv_ << ",";
    }
  }
  csv_ << "\n";
  csv_.flush();
}

void TradeRecorder::flushCsv() {
  // Flush any pending aggregation.
  if (has_pending_agg_) {
    flushAggregatedTrade();
  }
  // Write all remaining markout entries (partial).
  for (const auto& e : pending_markouts_) {
    writeCsvRow(e);
  }
  pending_markouts_.clear();
  if (csv_.is_open()) {
    csv_.flush();
    csv_.close();
  }
  csv_opened_ = false;
}

void TradeRecorder::cancelOutstandingOrds() {
  flushCsv();
}

Quantity TradeRecorder::getMaxPos() { return Quantity{"0"}; }
SymbolId TradeRecorder::getTradedSymbols() { return symbol_; }
std::vector<Market> TradeRecorder::getMarkets() { return {traded_books_}; }
std::vector<pktrade::BookId> TradeRecorder::getBookIds() {
  std::vector<pktrade::BookId> book_ids;
  for (pktrade::Market m : traded_books_) {
    book_ids.push_back({m, symbol_});
  }
  return book_ids;
}

} // namespace pktrade::ordex
