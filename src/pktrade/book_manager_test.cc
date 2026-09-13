#include "pktrade/book_manager.h"

#include <set>
#include <vector>

#include <gmock/gmock.h>
#include <gtest/gtest.h>

#include "pktrade/mdapi.h"

#include "mdmsg.pb.h"

using ::testing::Ref;
using ::testing::Sequence;
using ::testing::StrictMock;

namespace pktrade {

inline bool operator==(const LevelAdd& lhs, const LevelAdd& rhs) {
  return std::tie(lhs.px, lhs.side, lhs.qty, lhs.num_orders) ==
         std::tie(rhs.px, rhs.side, rhs.qty, rhs.num_orders);
}

inline bool operator==(const LevelModify& lhs, const LevelModify& rhs) {
  return std::tie(lhs.px, lhs.side, lhs.qty, lhs.num_orders, lhs.prev_qty, lhs.prev_num_orders) ==
         std::tie(rhs.px, rhs.side, rhs.qty, rhs.num_orders, rhs.prev_qty, rhs.prev_num_orders);
}
inline bool operator==(const LevelDelete& lhs, const LevelDelete& rhs) {
  return std::tie(lhs.px, lhs.side, lhs.prev_qty, lhs.prev_num_orders) ==
         std::tie(rhs.px, rhs.side, rhs.prev_qty, rhs.prev_num_orders);
}

namespace {

class MockBookListener : public BookListener {
 public:
  MOCK_METHOD(void, onLevelUpdate, (const LevelBook&, const LevelAdd&), (override));
  MOCK_METHOD(void, onLevelUpdate, (const LevelBook&, const LevelModify&), (override));
  MOCK_METHOD(void, onLevelUpdate, (const LevelBook&, const LevelDelete&), (override));
  MOCK_METHOD(void, onLevelUpdate, (const LevelBook&, const Trade&), (override));

  MOCK_METHOD(void, onFinal, (const LevelBook&), (override));
};

TEST(BookManager, Init) {
  std::set<BookId> book_ids = {
      {Market::BinanceSPOT, SymbolId{"BTCUSDT"}},
      {Market::BinanceSPOT, SymbolId{"BTCUSDC"}},
  };

  auto book_manager = BookManager(book_ids.begin(), book_ids.end());

  EXPECT_NE(book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDT"}}), nullptr);
  EXPECT_NE(book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDC"}}), nullptr);
  EXPECT_EQ(book_manager.find({Market::BinanceSPOT, SymbolId{"ETHUSDC"}}), nullptr);
}

TEST(BookManager, Duplicates) {
  std::vector<BookId> book_ids = {
      {Market::BinanceSPOT, SymbolId{"BTCUSDT"}},
      {Market::BinanceSPOT, SymbolId{"BTCUSDT"}},
  };

  EXPECT_THROW(BookManager(book_ids.begin(), book_ids.end()), std::runtime_error);
}

TEST(BookManager, ProcessBookUpdate) {
  std::set<BookId> book_ids = {
      {Market::BinanceSPOT, SymbolId{"BTCUSDT"}},
      {Market::BinanceSPOT, SymbolId{"BTCUSDC"}},
  };

  auto book_manager = BookManager(book_ids.begin(), book_ids.end());

  auto listener = StrictMock<MockBookListener>();
  book_manager.subscribe({Market::BinanceSPOT, SymbolId{"BTCUSDT"}}, &listener);

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
    update.set_symbol_id("BTCUSDT");

    auto* book_update = update.mutable_book_update();
    book_update->set_event_time(420);
    book_update->set_first_update_id(10);
    book_update->set_last_update_id(22);

    auto* level = book_update->add_bids();
    level->set_px("100.00");
    level->set_qty("25.0000");

    level = book_update->add_bids();
    level->set_px("99.99");
    level->set_qty("3.10000");

    level = book_update->add_bids();
    level->set_px("50.01");
    level->set_qty("100.5000");

    level = book_update->add_asks();
    level->set_px("110.00");
    level->set_qty("52.0000");

    level = book_update->add_asks();
    level->set_px("105.99");
    level->set_qty("1.30000");

    level = book_update->add_asks();
    level->set_px("101.01");
    level->set_qty("500.1000");

    auto* book = book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDT"}});

    Sequence s;

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"100.00"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"25.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"99.99"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"3.100"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"50.01"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"100.500"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"110.00"},
                                            .side = Side::Sell,
                                            .qty = Quantity{"52.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"105.99"},
                                            .side = Side::Sell,
                                            .qty = Quantity{"1.300"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"101.01"},
                                            .side = Side::Sell,
                                            .qty = Quantity{"500.100"},
                                        }))
        .InSequence(s);

    book_manager.onMD(update);
  }

  {
    auto* book = book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDT"}});

    auto [best_bid, best_ask] = book->getTopBook<BuySide>();
    EXPECT_EQ(best_bid.px, Price{"100.00"});
    EXPECT_EQ(best_bid.qty, Quantity{"25.0000"});
    EXPECT_EQ(best_ask.px, Price{"101.01"});
    EXPECT_EQ(best_ask.qty, Quantity{"500.1000"});
  }

  {
    auto* book = book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDC"}});
    EXPECT_TRUE(book->empty());
  }

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
    update.set_symbol_id("BTCUSDT");

    auto* book_update = update.mutable_book_update();
    book_update->set_event_time(520);
    book_update->set_first_update_id(23);
    book_update->set_last_update_id(25);

    // 100.00 price level flips offer
    auto* level = book_update->add_bids();
    level->set_px("100.00");
    level->set_qty("0.00000");

    level = book_update->add_asks();
    level->set_px("100.00");
    level->set_qty("5.0000");

    auto* book = book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDT"}});

    Sequence s;

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelDelete{
                                            .px = Price{"100.00"},
                                            .side = Side::Buy,
                                            .prev_qty = Quantity{"25.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"100.00"},
                                            .side = Side::Sell,
                                            .qty = Quantity{"5.000"},
                                        }))
        .InSequence(s);

    book_manager.onMD(update);
  }

  {
    auto* book = book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDT"}});

    auto [best_bid, best_ask] = book->getTopBook<BuySide>();
    EXPECT_EQ(best_bid.px, Price{"99.9900"});
    EXPECT_EQ(best_bid.qty, Quantity{"3.1000"});
    EXPECT_EQ(best_ask.px, Price{"100.00"});
    EXPECT_EQ(best_ask.qty, Quantity{"5.0000"});
  }
}

TEST(BookManager, ProcessTickerUpdate) {
  std::set<BookId> book_ids = {
      {Market::BinanceSPOT, SymbolId{"BTCUSDT"}},
  };

  auto book_manager = BookManager(book_ids.begin(), book_ids.end());

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
    update.set_symbol_id("BTCUSDT");

    auto* book_update = update.mutable_book_update();
    book_update->set_event_time(420);
    book_update->set_first_update_id(10);
    book_update->set_last_update_id(22);

    auto* level = book_update->add_bids();
    level->set_px("100.00");
    level->set_qty("25.0000");

    level = book_update->add_bids();
    level->set_px("99.99");
    level->set_qty("3.10000");

    level = book_update->add_bids();
    level->set_px("50.01");
    level->set_qty("100.5000");

    level = book_update->add_asks();
    level->set_px("110.00");
    level->set_qty("52.0000");

    level = book_update->add_asks();
    level->set_px("105.99");
    level->set_qty("1.30000");

    level = book_update->add_asks();
    level->set_px("101.01");
    level->set_qty("500.1000");

    book_manager.onMD(update);
  }

  auto listener = StrictMock<MockBookListener>();
  book_manager.subscribe({Market::BinanceSPOT, SymbolId{"BTCUSDT"}}, &listener);

  auto* book = book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDT"}});

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
    update.set_symbol_id("BTCUSDT");
    auto* ticker_update = update.mutable_ticker_update();
    ticker_update->set_update_id(30);
    ticker_update->set_best_ask_px("106.00");
    ticker_update->set_best_bid_px("99.99");
    ticker_update->set_best_ask_qty("6.00");
    ticker_update->set_best_bid_qty("1.00");

    Sequence s;

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelDelete{
                                            .px = Price{"100.00"},
                                            .side = Side::Buy,
                                            .prev_qty = Quantity{"25.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelModify{
                                            .px = Price{"99.99"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"1.00"},
                                            .prev_qty = Quantity{"3.10"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelDelete{
                                            .px = Price{"101.01"},
                                            .side = Side::Sell,
                                            .prev_qty = Quantity{"500.1"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelDelete{
                                            .px = Price{"105.99"},
                                            .side = Side::Sell,
                                            .prev_qty = Quantity{"1.30"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"106.00"},
                                            .side = Side::Sell,
                                            .qty = Quantity{"6.00"},
                                        }))
        .InSequence(s);

    book_manager.onMD(update);
  }

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
    update.set_symbol_id("BTCUSDT");

    auto* book_update = update.mutable_book_update();
    book_update->set_event_time(420);
    book_update->set_first_update_id(23);
    book_update->set_last_update_id(25);

    auto* level = book_update->add_bids();
    level->set_px("50.01");
    level->set_qty("10.5000");

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book), LevelModify{
                                                        .px = Price{"50.01"},
                                                        .side = Side::Buy,
                                                        .qty = Quantity{"10.50"},
                                                        .prev_qty = Quantity{"100.50"},
                                                    }));

    book_manager.onMD(update);
  }
}

TEST(BookManager, ProcessBookSnapshot) {
  std::set<BookId> book_ids = {
      {Market::BinanceSPOT, SymbolId{"BTCUSDT"}},
  };

  auto book_manager = BookManager(book_ids.begin(), book_ids.end());

  auto listener = StrictMock<MockBookListener>();
  book_manager.subscribe({Market::BinanceSPOT, SymbolId{"BTCUSDT"}}, &listener);

  auto* book = book_manager.find({Market::BinanceSPOT, SymbolId{"BTCUSDT"}});

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
    update.set_symbol_id("BTCUSDT");

    auto* book_snapshot = update.mutable_book_snapshot();
    book_snapshot->set_last_update_id(1);

    auto* level = book_snapshot->add_bids();
    level->set_px("3.00");
    level->set_qty("30.0000");

    level = book_snapshot->add_bids();
    level->set_px("4.00");
    level->set_qty("40.00000");

    level = book_snapshot->add_bids();
    level->set_px("5.00");
    level->set_qty("50.000");

    Sequence s;

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"5.00"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"50.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"4.00"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"40.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"3.00"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"30."},
                                        }))
        .InSequence(s);

    book_manager.onMD(update);
  }

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_BINANCE_SPOT);
    update.set_symbol_id("BTCUSDT");

    auto* book_snapshot = update.mutable_book_snapshot();
    book_snapshot->set_last_update_id(3);

    auto* level = book_snapshot->add_bids();
    level->set_px("4.00");
    level->set_qty("40.0000");

    level = book_snapshot->add_bids();
    level->set_px("5.00");
    level->set_qty("55.00000");

    level = book_snapshot->add_bids();
    level->set_px("6.00");
    level->set_qty("60.000");

    Sequence s;

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelAdd{
                                            .px = Price{"6.00"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"60.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelModify{
                                            .px = Price{"5.00"},
                                            .side = Side::Buy,
                                            .qty = Quantity{"55.00"},
                                            .prev_qty = Quantity{"50.00"},
                                        }))
        .InSequence(s);

    EXPECT_CALL(listener, onLevelUpdate(Ref(*book),
                                        LevelDelete{
                                            .px = Price{"3.00"},
                                            .side = Side::Buy,
                                            .prev_qty = Quantity{"30.0"},
                                        }))
        .InSequence(s);

    book_manager.onMD(update);
  }
}

TEST(BookManager, HyperliquidShallowSnapshotPreservesDeepTail) {
  std::set<BookId> book_ids = {
      {Market::Hyperliquid, SymbolId{"BTC"}},
  };

  auto book_manager = BookManager(book_ids.begin(), book_ids.end());
  auto* book = book_manager.find({Market::Hyperliquid, SymbolId{"BTC"}});

  auto add_level = [](auto add, const char* px, const char* qty) {
    auto* level = add();
    level->set_px(px);
    level->set_qty(qty);
  };

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_HYPERLIQUID);
    update.set_symbol_id("BTC");

    auto* snap = update.mutable_book_snapshot();
    add_level([&]() { return snap->add_bids(); }, "100.00", "1.00");
    add_level([&]() { return snap->add_bids(); }, "99.00", "2.00");
    add_level([&]() { return snap->add_bids(); }, "98.00", "3.00");
    add_level([&]() { return snap->add_bids(); }, "97.00", "4.00");
    add_level([&]() { return snap->add_asks(); }, "101.00", "1.00");
    add_level([&]() { return snap->add_asks(); }, "102.00", "2.00");
    add_level([&]() { return snap->add_asks(); }, "103.00", "3.00");
    add_level([&]() { return snap->add_asks(); }, "104.00", "4.00");

    book_manager.onMD(update);
  }

  {
    mdmsg::PbMessage update;
    update.set_market(mdmsg::PbMarket::PBMARKET_HYPERLIQUID);
    update.set_symbol_id("BTC");

    auto* snap = update.mutable_book_snapshot();
    add_level([&]() { return snap->add_bids(); }, "100.00", "10.00");
    add_level([&]() { return snap->add_bids(); }, "99.00", "20.00");
    add_level([&]() { return snap->add_asks(); }, "101.00", "10.00");
    add_level([&]() { return snap->add_asks(); }, "102.00", "20.00");

    book_manager.onMD(update);
  }

  ASSERT_EQ(book->side<BuySide>().size(), 4);
  auto bid = book->side<BuySide>().begin();
  EXPECT_EQ(bid->px, Price{"100.00"});
  EXPECT_EQ(bid->qty, Quantity{"10.00"});
  ++bid;
  EXPECT_EQ(bid->px, Price{"99.00"});
  EXPECT_EQ(bid->qty, Quantity{"20.00"});
  ++bid;
  EXPECT_EQ(bid->px, Price{"98.00"});
  EXPECT_EQ(bid->qty, Quantity{"3.00"});
  ++bid;
  EXPECT_EQ(bid->px, Price{"97.00"});
  EXPECT_EQ(bid->qty, Quantity{"4.00"});

  ASSERT_EQ(book->side<SellSide>().size(), 4);
  auto ask = book->side<SellSide>().begin();
  EXPECT_EQ(ask->px, Price{"101.00"});
  EXPECT_EQ(ask->qty, Quantity{"10.00"});
  ++ask;
  EXPECT_EQ(ask->px, Price{"102.00"});
  EXPECT_EQ(ask->qty, Quantity{"20.00"});
  ++ask;
  EXPECT_EQ(ask->px, Price{"103.00"});
  EXPECT_EQ(ask->qty, Quantity{"3.00"});
  ++ask;
  EXPECT_EQ(ask->px, Price{"104.00"});
  EXPECT_EQ(ask->qty, Quantity{"4.00"});
}

} // namespace
} // namespace pktrade
