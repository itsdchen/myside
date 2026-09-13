#include "sig_wmid.h"

#include "pktrade/book.h"
#include "pktrade/context/global_vars.h"
#include "pktrade/side.h"

namespace pktrade::signals {

SigWMid::SigWMid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf)
    : Signal(signal_id, sig_conf, sf), bb_(0), ba_(1e10), bq_(0), aq_(0), first_tick_(true) {
  symbol_ = SymbolId(sig_conf["symbol"].GetString());

  // Get markets.
  for (auto& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown);
    books_.push_back(pmk);
  }

  if (sig_conf.HasMember("return_price")) {
    return_price_ = sig_conf["return_price"].GetBool();
  } else {
    return_price_ = false;
  }
}

std::vector<pktrade::BookId> SigWMid::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

bool SigWMid::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                     const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigWMid") {
    return false;
  }

  // Check if signal and books are identical.
  if (symbol_.get() != other_conf["symbol"].GetString()) {
    return false;
  }

  // Checks that books are same.
  auto other_books = other_conf["books"].GetArray();

  if (other_books.Size() != books_.size()) {
    return false;
  }

  for (auto& book_str : other_books) {
    pktrade::Market book =
        magic_enum::enum_cast<Market>(book_str.GetString()).value_or(Market::Unknown);
    if (std::find(books_.begin(), books_.end(), book) == books_.end()) {
      return false;
    }
  }

  return true;
}

void SigWMid::subscribeData(MDBeacon* beacon) {
  BookId book_id{books_[0], symbol_};
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd,
      pktrade::md::BeaconCBType::LvlMod,
      pktrade::md::BeaconCBType::LvlDel,
  };

  beacon->addListener(book_id, this, cb_types);
}

void SigWMid::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  if (lvl_add.side == Side::Buy && lvl_add.px.toDouble() > bb_) {
    bb_ = lvl_add.px.toDouble();
    bq_ = lvl_add.qty.toDouble();
    calc();
  } else if (lvl_add.side == Side::Sell && lvl_add.px.toDouble() < ba_) {
    ba_ = lvl_add.px.toDouble();
    aq_ = lvl_add.qty.toDouble();
    calc();
  }
}

void SigWMid::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  // check if modifying.
  if (lvl_mod.side == Side::Buy && lvl_mod.px.toDouble() == bb_) {
    bq_ = lvl_mod.qty.toDouble();
    calc();
  } else if (lvl_mod.side == Side::Sell && lvl_mod.px.toDouble() == ba_) {
    aq_ = lvl_mod.qty.toDouble();
    calc();
  }
}

void SigWMid::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  if (lvl_del.side == Side::Buy && lvl_del.px.toDouble() == bb_) {
    // Get the new bb, bq.
    const LevelBook* bk =
        pktrade::GlobalVar::book_manager_->find(BookId{books_[0], SymbolId{symbol_}});

    auto top_bid = bk->side<BuySide>().begin();
    bb_ = top_bid->px.toDouble();
    bq_ = top_bid->qty.toDouble();
    calc();
  } else if (lvl_del.side == Side::Sell && lvl_del.px.toDouble() == ba_) {
    // Get the new ba, aq.
    const LevelBook* bk =
        pktrade::GlobalVar::book_manager_->find(BookId{books_[0], SymbolId{symbol_}});

    auto top_ask = bk->side<SellSide>().begin();
    ba_ = top_ask->px.toDouble();
    aq_ = top_ask->qty.toDouble();

    calc();
  }
}

void SigWMid::calc() {
  // Basic sanity checking. Being more careful than I really need to be I guess.
  if (bb_ == 0 || ba_ == 1e10 || bq_ == 0 || aq_ == 0) {
    return;
  }

  if (bb_ < ba_) {
    // Only update when book isn't in a locked/crossed state.
    double val = (bb_ * aq_ + ba_ * bq_) / (bq_ + aq_);
    if (!return_price_) {
      val = val / ((bb_ + ba_) / 2) - 1;
    }

    printf("I should be setting val %f\n", val);

    setVV(val, true);
  } else {
    // I guess, do nothing.
  }
}

void SigWMid::fullRecalc() {
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], SymbolId{symbol_}});

  if (bk->nonFull()) {
    // Not valid. Still in a bad state.
    first_tick_ = false;
    return;
  }

  const auto& top = bk->getTopBook<BuySide>();

  // Otherwise, populate the wmid sig.
  bb_ = std::get<0>(top).px.toDouble();
  bq_ = std::get<0>(top).qty.toDouble();

  ba_ = std::get<1>(top).px.toDouble();
  aq_ = std::get<1>(top).qty.toDouble();

  // Calc.
  calc();
}

void SigWMid::reset() {
  first_tick_ = true;
  bb_ = 0;
  ba_ = 1e10;

  bq_ = 0;
  aq_ = 0;
}

} // namespace pktrade::signals
