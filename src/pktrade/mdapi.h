#pragma once

#include <optional>

#include <boost/functional/hash.hpp>

#include "pktrade/side.h"
#include "pktrade/types.h"

namespace pktrade {

enum class Market {
  Unknown,
  BinanceSPOT,
  // Binance USDT futures
  BinanceFutures,

  // Coin-delivered futures.
  BinanceCOINFutures,

  Hyperliquid,

  // Being a little aspirational.
  BybitSPOT,
  BybitDeriv,
  // Like their version of COIN-M
  BybitInverseDeriv,

  // Starting out with one type, might have more. Might diversify this into specific ones
  // given the source (eg. BlueOcean, DataBento, CME , etc.)
  TopBookEquity,
  // At some point include blueocean as a separate type but we're starting w/ CME

  TopBookCme,

};

enum class LastMsgType {
  Unknown,
  BookTicker,
  Trade,
  // multi level update.
  BookUpdate,
};

using BookId = std::pair<Market, SymbolId>;

struct LevelAdd {
  Price px;
  Side side;
  Quantity qty;
  // qty without the flagged_shares.
  Quantity unflagged_qty;
  std::optional<int> num_orders;
  LastMsgType last_msg_type;
  // This could be 0 if we don't have a timestamp.
  // Also, this is not necessarily supplied everywhere (for example, spot
  // bookTicker) So do not always rely on this.
  int64_t transact_t = 0;
};

// Note that the quantities here are after applying flagging,
// if we have flagging turned on.
struct LevelModify {
  Price px;
  Side side;
  Quantity qty;
  Quantity unflagged_qty;
  std::optional<int> num_orders;
  Quantity prev_qty;
  Quantity prev_unflagged_qty;
  std::optional<int> prev_num_orders;
  LastMsgType last_msg_type;
  int64_t transact_t = 0;
};

struct LevelDelete {
  Price px;
  Side side;
  Quantity prev_qty;
  Quantity prev_unflagged_qty;
  std::optional<int> prev_num_orders;
  LastMsgType last_msg_type;
  int64_t transact_t = 0;
};

struct Trade {
  Price px;
  Quantity qty;
  Side passive_side;
  int64_t transact_t = 0;
};

struct Quote {
  Price bb;
  Price ba;
  Quantity bs;
  Quantity as;
  double mid;
  int64_t source_t = 0;
};

} // namespace pktrade

namespace std {

template <>
struct hash<pktrade::BookId> {
  size_t operator()(const pktrade::BookId& id) const {
    size_t seed = 0;
    boost::hash_combine(seed, id.first);

    // fluent::Hashable doesn't specialize boost::hash<SymbolId>
    boost::hash_combine(seed, id.second.get());

    return seed;
  }
};

} // namespace std
