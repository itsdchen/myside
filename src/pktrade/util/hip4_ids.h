#pragma once

// HIP-4 identifier helpers.
//
// Ported from the Python reference in overmind/strat_main/tools/hip4_utils.py and
// hip4maker/src/hip4maker/hip4.py. HIP-4 outcome identifiers deliberately stay separate
// from ordinary Hyperliquid spot/perp identifiers.
//
// The scheme:
//   encoding    = 10 * outcome_id + side      (side is 0 or 1)
//   book coin   = "#" + encoding              (order/trade/book symbol, e.g. "#9610")
//   token       = "+" + encoding              (spot-balance token name, e.g. "+9610")
//   asset id    = 100'000'000 + encoding       (integer `asset` for a signed order action)
//
// Header-only: this is pure integer/string math with no dependencies, so callers just
// include it. The actual L1 signing of the split/merge action lives in the Python
// wsgateway; these helpers are for sizing/routing and asset-id lookups on the C++ side.

#include <cmath>
#include <stdexcept>
#include <string>

namespace pktrade::util::hip4 {

// Offset that separates HIP-4 outcome asset ids from ordinary spot/perp asset ids.
inline constexpr int OUTCOME_ASSET_OFFSET = 100'000'000;

// Whole shares only. Measured on testnet: a fractional size is rejected with
// "Order has invalid size." There is no szDecimals field published for outcomes, so this
// is a hard-coded constant rather than a per-market lookup. See hip4_utils.py.
inline constexpr int OUTCOME_SZ_DECIMALS = 0;

// Price tick is a FIXED 0.00001 (5 decimal places), NOT the significant-figure rule that
// spot/perps use. A fixed decimal grid is required because the merged book means an order
// to buy YES at p is the same as a sell of NO at 1-p, and both prices must be
// representable. See round_outcome_px below.
inline constexpr int OUTCOME_PX_DECIMALS = 5;

// (outcome id, side 0/1) -> the shared integer encoding.
inline int outcome_encoding(int outcome_id, int side) {
  if (outcome_id < 0) {
    throw std::invalid_argument("outcome_id must be nonnegative");
  }
  if (side != 0 && side != 1) {
    throw std::invalid_argument("HIP-4 side must be 0 or 1");
  }
  return 10 * outcome_id + side;
}

// Inverse of outcome_encoding: encoding -> (outcome id, side).
inline std::pair<int, int> decode_outcome(int encoding) {
  return {encoding / 10, encoding % 10};
}

// Book/trade/order symbol, e.g. "#9610".
inline std::string outcome_coin(int outcome_id, int side) {
  return "#" + std::to_string(outcome_encoding(outcome_id, side));
}

// Spot-balance token name, e.g. "+9610".
inline std::string outcome_token(int outcome_id, int side) {
  return "+" + std::to_string(outcome_encoding(outcome_id, side));
}

// Integer `asset` for a signed order/cancel action, e.g. 100009610.
inline int outcome_asset_id(int outcome_id, int side) {
  return OUTCOME_ASSET_OFFSET + outcome_encoding(outcome_id, side);
}

// Round a probability onto the exchange's price grid (fixed 0.00001 tick / 5 dp).
// Getting this wrong is silent until you quote below 0.1, so keep it a uniform grid,
// never a significant-figure rule.
inline double round_outcome_px(double px) {
  double scale = 100000.0; // 10^OUTCOME_PX_DECIMALS
  return std::round(px * scale) / scale;
}

// Round a size to whole shares (OUTCOME_SZ_DECIMALS == 0). Truncates toward zero so we
// never round a fundable size up past available collateral.
inline double round_outcome_sz(double sz) {
  return std::trunc(sz);
}

} // namespace pktrade::util::hip4
