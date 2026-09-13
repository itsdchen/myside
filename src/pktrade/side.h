#pragma once

#include <functional>
#include <utility>

#include "pktrade/types.h"
#include <magic_enum.hpp>

namespace pktrade {

enum class Side {
  Unknown,
  Buy,
  Sell,
};

struct BuySide;
struct SellSide;

struct BuySide {
  static constexpr auto side = Side::Buy;
  static constexpr auto sign = -1;

  using OppositeSide = SellSide;

  // The following comparison functions all compare a notion of "aggressiveness"
  // which carries semantic meaning depending on the side. The direction of more
  // aggressive is the direction of tightening the spread.
  using MoreAggressive = std::greater<Price>;
  using LessAggressive = std::less<Price>;
};

struct SellSide {
  static constexpr auto side = Side::Sell;
  static constexpr auto sign = 1;

  using OppositeSide = BuySide;

  // The following comparison functions all compare a notion of "aggressiveness"
  // which carries semantic meaning depending on the side. The direction of more
  // aggressive is the direction of tightening the spread.
  using MoreAggressive = std::less<Price>;
  using LessAggressive = std::greater<Price>;
};

template <typename F>
decltype(auto) apply(F&& f, Side side) {
  switch (side) {
    case Side::Buy:
      return std::invoke(std::forward<F>(f), BuySide{});
    case Side::Sell:
      return std::invoke(std::forward<F>(f), SellSide{});
    default:
      throw std::runtime_error("pktrade::apply<F>(F&&, Side) called with Side::Unknown");
  }
}

} // namespace pktrade
