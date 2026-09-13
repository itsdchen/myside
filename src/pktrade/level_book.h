#pragma once

#include "pktrade/book.h"
#include "pktrade/fixed_point.h"
#include "pktrade/types.h"

namespace pktrade {

struct BookLevel {
  BookLevel(Price px, Quantity qty, Quantity flagged_qty = Quantity{0})
      : px{px}, qty{qty}, flagged_shares{flagged_qty} {}

  Price px;
  mutable Quantity qty;
  mutable Quantity flagged_shares;
};

inline bool operator==(const BookLevel& lhs, const BookLevel& rhs) {
  return std::tie(lhs.px, lhs.qty) == std::tie(rhs.px, rhs.qty);
}

inline bool operator!=(const BookLevel& lhs, const BookLevel& rhs) { return !(lhs == rhs); }

using LevelBook = Book<BookLevel>;

inline bool operator==(const LevelBook& lhs, const LevelBook& rhs) {
  return std::tie(lhs.side<BuySide>(), lhs.side<SellSide>()) ==
         std::tie(rhs.side<BuySide>(), rhs.side<SellSide>());
}

inline bool operator!=(const LevelBook& lhs, const LevelBook& rhs) { return !(lhs == rhs); }

// Note: you best make sure level is an actual thing (eg. if an iterator,
// make sure it's not the end(). Otherwise you'll get a segfault.
inline void flagShares(const BookLevel& level, Quantity flag_amt) {
  // Flag
  level.flagged_shares += flag_amt;

  if (level.flagged_shares > level.qty) {
    level.flagged_shares = level.qty;
  }
}

// unflag_all is default true because a levelmodify can encapsulate
// many transactions, and we understand it to be the latest view
// of that price level. If there were flagged shares on that level,
// we may want update our notion of that price. Also on adds.
inline void unflagShares(const BookLevel& level, Quantity unflag_amt, bool unflag_all = true) {
  if (unflag_all) {
    level.flagged_shares = Quantity{0};
    return;
  }
  // Unflag
  level.flagged_shares -= unflag_amt;

  if (level.flagged_shares < Quantity{0}) {
    level.flagged_shares = Quantity{0};
  }
  // Don't have it negative.
}

// Never have this be negative.
inline Quantity effectiveQty(const BookLevel& level, bool use_flagged = true) {
  if (!use_flagged) {
    // Use in old-style signals where we fit without using flagging.
    return level.qty;
  }

  if (level.flagged_shares > level.qty) {
    return Quantity{0};
  }

  // Otherwise.
  return level.qty - level.flagged_shares;
}

} // namespace pktrade
