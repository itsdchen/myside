#include "pktrade/level_book.h"

#include <gmock/gmock.h>
#include <gtest/gtest.h>

#include "pktrade/side.h"
#include "pktrade/types.h"

using ::testing::ElementsAre;

namespace pktrade {
namespace {

TEST(LevelBookTest, Ranges) {
  LevelBook book{Market::BinanceSPOT, SymbolId{"BTC-USDT"}};
  auto& buy_side = book.side<BuySide>();
  auto& sell_side = book.side<SellSide>();

  sell_side.emplace(Price{100}, Quantity{3});
  sell_side.emplace(Price{101}, Quantity{10});
  sell_side.emplace(Price{110}, Quantity{1});

  buy_side.emplace(Price{99}, Quantity{2});
  buy_side.emplace(Price{90}, Quantity{8});
  buy_side.emplace(Price{89}, Quantity{5});
  buy_side.emplace(Price{88}, Quantity{10});

  ASSERT_THAT(buy_side,
              ElementsAre(BookLevel{Price{99}, Quantity{2}}, BookLevel{Price{90}, Quantity{8}},
                          BookLevel{Price{89}, Quantity{5}}, BookLevel{Price{88}, Quantity{10}}));

  ASSERT_THAT(sell_side,
              ElementsAre(BookLevel{Price{100}, Quantity{3}}, BookLevel{Price{101}, Quantity{10}},
                          BookLevel{Price{110}, Quantity{1}}));
}

TEST(LevelBookTest, Equality) {
  LevelBook book1{Market::BinanceSPOT, SymbolId{"BTC-USDT"}};

  book1.side<SellSide>().emplace(Price{101}, Quantity{30});
  book1.side<SellSide>().emplace(Price{100}, Quantity{25});
  book1.side<BuySide>().emplace(Price{99}, Quantity{30});

  LevelBook book2{Market::BinanceSPOT, SymbolId{"BTC-USDT"}};

  book2.side<SellSide>().emplace(Price{101}, Quantity{30});
  book2.side<SellSide>().emplace(Price{100}, Quantity{25});
  book2.side<BuySide>().emplace(Price{99}, Quantity{30});

  ASSERT_EQ(book1, book2);

  book2.side<BuySide>().emplace(Price{102}, Quantity{30});

  ASSERT_NE(book1, book2);
}

} // namespace
} // namespace pktrade
