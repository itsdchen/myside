#pragma once

#include "ordex.h"

#include "pktrade/level_book.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

namespace pktrade::ordex {

namespace protection {

// Sum own resting (non-canceled) qty at a given price.
inline double ownQtyAt(const std::vector<SimpleOrder>& own, double px, double min_tick) {
  double tol = std::max(min_tick * 0.5, 1e-9);
  double sum = 0;
  for (const auto& o : own) {
    if (o.cancel_time != 0) continue;
    if (std::abs(o.px.toDouble() - px) < tol) sum += o.init_qty.toDouble();
  }
  return sum;
}

// Walks buySide downward from prev_my_px (exclusive), accumulating foreign qty
// per level (effectiveQty minus our own resting). Returns the first book level
// px where cumulative foreign size >= threshold (shares). Returns quiet_NaN if
// never reached on the current book (caller should skip placement).
//
// Callers typically pass threshold = mult * inside_size_ems, so the bar scales
// with the symbol's own typical book depth instead of being an absolute shares
// count (which doesn't transfer across thick vs thin symbols).
//
// If threshold <= 0 (disabled), returns default_px.
inline double sparseBuyBacklevelPx(
    const pktrade::LevelBook* lvl_book, double prev_my_px, double default_px,
    double threshold, const std::vector<SimpleOrder>& own_buys, double min_tick) {
  if (threshold <= 0) return default_px;
  double accumulated = 0;
  for (const auto& level : lvl_book->buySide()) {
    double lvl_px = level.px.toDouble();
    if (lvl_px >= prev_my_px) continue;
    double own = ownQtyAt(own_buys, lvl_px, min_tick);
    double foreign = std::max(0.0, pktrade::effectiveQty(level).toDouble() - own);
    accumulated += foreign;
    if (accumulated >= threshold) {
      // Push just below this level so foreign liquidity sits BETWEEN our prev
      // order and the new backlevel. Don't be more aggressive than default_px.
      double beneath = lvl_px - min_tick;
      return std::min(default_px, beneath);
    }
  }
  return std::numeric_limits<double>::quiet_NaN();
}

inline double sparseSellBacklevelPx(
    const pktrade::LevelBook* lvl_book, double prev_my_px, double default_px,
    double threshold, const std::vector<SimpleOrder>& own_sells, double min_tick) {
  if (threshold <= 0) return default_px;
  double accumulated = 0;
  for (const auto& level : lvl_book->sellSide()) {
    double lvl_px = level.px.toDouble();
    if (lvl_px <= prev_my_px) continue;
    double own = ownQtyAt(own_sells, lvl_px, min_tick);
    double foreign = std::max(0.0, pktrade::effectiveQty(level).toDouble() - own);
    accumulated += foreign;
    if (accumulated >= threshold) {
      double above = lvl_px + min_tick;
      return std::max(default_px, above);
    }
  }
  return std::numeric_limits<double>::quiet_NaN();
}

// Update sided EMAs of inside foreign size. Each side's sample is the
// effective best-{bid,ask} size minus our own resting at that price. Sides are
// updated independently — an empty side just skips its own update.
inline void updateInsideSizeEms(
    const pktrade::LevelBook& bk, double tdc_ms, int64_t now_ms,
    const std::vector<SimpleOrder>& own_buys, const std::vector<SimpleOrder>& own_sells,
    double min_tick,
    double& buy_ems, int64_t& buy_last_t,
    double& sell_ems, int64_t& sell_last_t) {
  if (tdc_ms <= 0) return;
  const auto& bs = bk.buySide();
  const auto& ss = bk.sellSide();
  if (!bs.empty()) {
    double bb_px = bs.begin()->px.toDouble();
    double bb_sz = pktrade::effectiveQty(*bs.begin()).toDouble();
    double own_bb = ownQtyAt(own_buys, bb_px, min_tick);
    double sample = std::max(0.0, bb_sz - own_bb);
    if (buy_last_t == 0 || buy_ems <= 0) {
      buy_ems = sample;
      buy_last_t = now_ms;
    } else {
      double dt_ms = std::max<double>(0.0, static_cast<double>(now_ms - buy_last_t));
      double decay = std::exp(-dt_ms / tdc_ms);
      buy_ems = decay * buy_ems + (1.0 - decay) * sample;
      buy_last_t = now_ms;
    }
  }
  if (!ss.empty()) {
    double ba_px = ss.begin()->px.toDouble();
    double ba_sz = pktrade::effectiveQty(*ss.begin()).toDouble();
    double own_ba = ownQtyAt(own_sells, ba_px, min_tick);
    double sample = std::max(0.0, ba_sz - own_ba);
    if (sell_last_t == 0 || sell_ems <= 0) {
      sell_ems = sample;
      sell_last_t = now_ms;
    } else {
      double dt_ms = std::max<double>(0.0, static_cast<double>(now_ms - sell_last_t));
      double decay = std::exp(-dt_ms / tdc_ms);
      sell_ems = decay * sell_ems + (1.0 - decay) * sample;
      sell_last_t = now_ms;
    }
  }
}

// Returns input_size unchanged if disabled (cap_mult <= 0 or inside_size <= 0).
// Otherwise returns min(input_size, cap_mult * inside_size_ems), floored at 0.
inline double capBySize(double input_size, double cap_mult, double inside_size_ems) {
  if (cap_mult <= 0 || inside_size_ems <= 0) return input_size;
  return std::max(0.0, std::min(input_size, cap_mult * inside_size_ems));
}

// Maintain size-weighted exponential moving averages of trade px*qty and qty,
// such that (num_ems / denom_ems) gives a size-weighted trade VWAP. A phantom
// book can move the displayed mid, but it can't fake a trade print — so this
// EMA anchors to prices that actually cleared.
//
// Caller queries exec_vwap = (denom_ems > eps) ? num_ems / denom_ems : fallback.
inline void updateExecVwapEms(
    double trade_px, double trade_qty, double tdc_ms, int64_t now_ms,
    double& num_ems, double& denom_ems, int64_t& last_t) {
  if (tdc_ms <= 0 || trade_qty <= 0 || !std::isfinite(trade_px)) return;
  double num_sample = trade_px * trade_qty;
  double denom_sample = trade_qty;
  if (last_t == 0 || denom_ems <= 0) {
    num_ems = num_sample;
    denom_ems = denom_sample;
    last_t = now_ms;
    return;
  }
  double dt_ms = std::max<double>(0.0, static_cast<double>(now_ms - last_t));
  double decay = std::exp(-dt_ms / tdc_ms);
  num_ems   = decay * num_ems   + num_sample;
  denom_ems = decay * denom_ems + denom_sample;
  last_t = now_ms;
}

} // namespace protection
} // namespace pktrade::ordex
