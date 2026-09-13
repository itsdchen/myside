#pragma once

#include <optional>
#include <set>
#include <tuple>

#include "pktrade/mdapi.h"
#include "pktrade/side.h"
#include "pktrade/type_traits.h"

namespace pktrade {

// Generic book data structure.
//
// T can be any generic type that contains a price (px data member) which is
// totally ordered. Book uses lvl.px as the sort key in its std::set internal
// representation.
//
// side<BuySide>().begin() and side<SellSide>().begin() are the best bid/offer.
template <typename T>
class Book {
  template <typename SideT>
  struct BookLevelComparator {
    using is_transparent = void; // Enable heterogenous lookup

    using PriceCmp = typename SideT::MoreAggressive;
    using Side = SideT;

    bool operator()(const T& lhs, const T& rhs) const { return PriceCmp()(lhs.px, rhs.px); }

    // Transparent overloads
    bool operator()(Price px, const T& rhs) const { return PriceCmp()(px, rhs.px); }

    bool operator()(const T& lhs, Price px) const { return PriceCmp()(lhs.px, px); }

    bool operator()(Price lhs, Price rhs) const { return PriceCmp()(lhs, rhs); }
  };

 public:
  template <typename Side>
  using BookSide = std::set<T, BookLevelComparator<Side>>;

  explicit Book(BookId book_id) : book_id_{book_id} {}
  Book(Market market, SymbolId symbol) : Book(std::make_pair(market, symbol)) {}

  // Returns an std::tuple of const lvalue references to the best inside levels.
  // The first element is the side corresponding to SideT and the second element
  // is the side corresponding to SideT::OppositeSide.
  //
  // Calling this function when the book is not two-sided is undefined behavior.
  template <typename SideT>
  auto getTopBook() const {
    return std::tie(*side<SideT>().begin(), *side<typename SideT::OppositeSide>().begin());
  }

  template <typename SideT>
  auto getSideTop() const {
    return *side<SideT>().begin();
  }

  // true if book is not two-sided.
  bool nonFull() const { return side<BuySide>().empty() || side<SellSide>().empty(); }

  bool empty() const { return side<BuySide>().empty() && side<SellSide>().empty(); }

  template <typename SideT>
  auto& side() const {
    if constexpr (SideT::side == Side::Buy) {
      return bid_;
    } else if constexpr (SideT::side == Side::Sell) {
      return ask_;
    } else {
      static_assert(always_false<SideT>::value, "side must be buy or sell");
    }
  }

  template <typename SideT>
  auto& side() {
    return const_cast<BookSide<SideT>&>(static_cast<const Book<T>*>(this)->side<SideT>());
  }

  // wtf is going on.
  auto& buySide() const { return bid_; }

  auto& sellSide() const { return ask_; }

  void clear() {
    bid_.clear();
    ask_.clear();
  }

  // There's a core dump here if I make this return
  // a copy and not the reference. Just.. yeah.
  const BookId& getBookID() const { return book_id_; }

  const Market& getMarket() const { return book_id_.first; }
  const SymbolId& getSymbol() const { return book_id_.second; }

 private:
  BookSide<BuySide> bid_;
  BookSide<SellSide> ask_;
  // Commenting this out because it's causing compile errors in the tests.
  // Fix and revise later.
  BookId book_id_;
};

} // namespace pktrade
