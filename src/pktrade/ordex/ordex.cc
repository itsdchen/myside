#include "ordex.h"

#include <cmath>
#include <fmt/format.h>
#include <glog/logging.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/secmaster.h"

namespace pktrade::ordex {

double Ordex::roundToNearest(double px_extra_precision) {
  // Compute tick from the order price itself, not min_tick_, because the order
  // may be in a different tick regime than the mid (e.g. mid < 100, order > 100).
  double tick = pktrade::GlobalVar::secmaster_->calc_tick_size(px_extra_precision).toDouble();
  if (tick == 0) {
    LOG(ERROR) << fmt::format(
        "({}) roundToNearest: px {} tick empty - returning.",
        symbol_.get(), px_extra_precision);
    return px_extra_precision;
  }
  return std::round(px_extra_precision / tick) * tick;
}

double Ordex::roundToSide(double px_extra_precision, bool round_up) {
  // Compute tick from the order price itself, not min_tick_, because the order
  // may be in a different tick regime than the mid (e.g. mid < 100, order > 100).
  double tick = pktrade::GlobalVar::secmaster_->calc_tick_size(px_extra_precision).toDouble();
  if (tick == 0) {
    LOG(ERROR) << fmt::format(
        "({}) roundToSide: px {} tick empty - returning.",
        symbol_.get(), px_extra_precision);
    return px_extra_precision;
  }
  if (round_up) {
    return std::ceil((px_extra_precision - pktrade::EPS) / tick) * tick;
  }
  // else
  return std::floor((px_extra_precision + pktrade::EPS) / tick) * tick;
}

} // namespace pktrade::ordex
