#include "sig_mid.h"

#include "pktrade/book.h"
#include "pktrade/context/global_vars.h"
#include "pktrade/side.h"

#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::signals {

SigMid::SigMid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf)
    : Signal(signal_id, sig_conf, sf), bb_(0), ba_(1e10), bbsize_(0), basize_(0),
      first_tick_(true) {
  valid_ = false;
  symbol_ = SymbolId{sig_conf["symbol"].GetString()};
  for (const rapidjson::Value& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown);
    books_.push_back(pmk);
  }
  if (sig_conf.HasMember("use_flagged")) {
    use_flagged_ = sig_conf["use_flagged"].GetBool();
  } else {
    use_flagged_ = false;
  }
  use_flagged_ = true;

  // midprice gets a faster update.
  pri_ = 70;
}

std::vector<pktrade::BookId> SigMid::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

bool SigMid::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                    const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigMid") {
    return false;
  }

  // Check if signal and books are identical.
  if (symbol_.get() != other_conf["symbol"].GetString()) {
    return false;
  }
  // Checks that books are same.
  const auto& other_books = other_conf["books"].GetArray();

  if (other_books.Size() != books_.size()) {
    return false;
  }

  for (const rapidjson::Value& book_str : other_books) {
    pktrade::Market book =
        magic_enum::enum_cast<Market>(book_str.GetString()).value_or(Market::Unknown);
    if (std::find(books_.begin(), books_.end(), book) == books_.end()) {
      return false;
    }
  }

  // TODO: revert this bc it unconfuses me. Bro.
  bool other_sig_flagged = false;
  if (other_conf.HasMember("use_flagged")) {
    other_sig_flagged = other_conf["use_flagged"].GetBool();
  }
  if (use_flagged_ != other_sig_flagged) {
    return false;
  }

  return true;
}

std::string SigMid::getPrimarySymbol() const { return symbol_.get(); }

void SigMid::subscribeData(MDBeacon* beacon) {
  // Subscribe to add and delete so far.
  // Snapshot-driven modifies do not
  // change the inside for now.

  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd, pktrade::md::BeaconCBType::LvlMod,
      pktrade::md::BeaconCBType::LvlDel, pktrade::md::BeaconCBType::Trd,
      pktrade::md::BeaconCBType::Final};

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);
  }
}

void SigMid::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  //  std::cout << time_utils::nowToStr() <<  " Something happening from add
  //  Side " << (lvl_add.side == Side::Buy? "Buy": "Sell" ) << " px " <<
  //  lvl_add.px.toDouble() <<  std::endl;

  // Check.
  if (lvl_add.side == Side::Buy && lvl_add.px.toDouble() > bb_) {
    bb_ = lvl_add.px.toDouble();
    calc();
    // Update and stuff.
  } else if (lvl_add.side == Side::Sell && lvl_add.px.toDouble() < ba_) {
    ba_ = lvl_add.px.toDouble();
    calc();
  }
}

// modifies can now include mid-changing situations now.
void SigMid::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  if (lvl_mod.side == Side::Buy &&
      // Flagging
      ((lvl_mod.qty == Quantity{0} && lvl_mod.px.toDouble() == bb_) ||
       // unflagging
       (lvl_mod.px.toDouble() > bb_ && lvl_mod.qty > Quantity{0}))) {
    // Do a lookup.
    // double new_bb = 0;
    fullRecalc();
  } else if (lvl_mod.side == Side::Sell &&

             ((lvl_mod.qty == Quantity{0} && lvl_mod.px.toDouble() == ba_) ||
              (lvl_mod.px.toDouble() < ba_ && lvl_mod.qty > Quantity{0}))) {
    // Do a lookup.
    // double new_ba = 0;
    fullRecalc();
  }

  //  std::cout << time_utils::nowToStr() <<  " Something happening from mod
  //  Side " << (lvl_mod.side == Side::Buy? "Buy": "Sell") << " px " <<
  //  lvl_mod.px.toDouble() <<  std::endl;

  //  pktrade::util::printInside(bk);
}

void SigMid::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  if (lvl_del.prev_qty == Quantity{0}) {
    // Don't do anything, this was already handled.
    return;
  }

  //  std::cout <<  time_utils::nowToStr() << " Something happening from del
  //  Side " << (lvl_del.side == Side::Buy? "Buy": "Sell") << " px " <<
  //  lvl_del.px.toDouble() <<  std::endl;

  //  pktrade::util::printInside(bk);
  if (lvl_del.side == Side::Buy && lvl_del.px.toDouble() >= bb_) {
    // Do a lookup.
    // double new_bb = 0;
    fullRecalc();
  } else if (lvl_del.side == Side::Sell && lvl_del.px.toDouble() <= ba_) {
    // Do a lookup.
    // double new_ba = 0;
    fullRecalc();
  }
}

void SigMid::onTrade(const LevelBook& bk, const Trade& trd) {
  just_got_trade_ = true;
  if (trd.passive_side == Side::Buy) {
    last_sell_trd_px_ = trd.px.toDouble();
  } else {
    last_buy_trd_px_ = trd.px.toDouble();
  }
}

// Initializes sigmid properly.
void SigMid::onFinal(const LevelBook& bk) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
  }
  just_got_trade_ = false;
}

// TODO: probably only send the calc at the EOM.
void SigMid::calc() {
  // Can't set a price if it's one-sided.
  if (bb_ == 0 || ba_ == 1e10) {
    // Should probably setValidity here.
    /*
    util::log_line(fmt::format("{}: bb ba {} {} setValidity false",
                               time_utils::nowToStr().c_str(), bb_, ba_),
                   false);
                   */
    setValidity(false);
    return;
  }
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], SymbolId{symbol_}});

  // pktrade::util::printInside(*bk);
  if (ba_ > bb_) {
    setValidity(true);
    setVV((bb_ + ba_) / 2, true);
  } else {
    // setValidity(false);
  }
}

void SigMid::fullRecalc() {
  // Writing this out explicitly for practice.
  // Realize I can use getTopBook more easily.

  // std::cout << " ===START=== " << std::endl;
  // std::cout << time_utils::nowToMs() << " ( " << time_utils::nowToStr()
  //           << " ) ";

  bb_ = 0;
  bbsize_ = 0;
  ba_ = 1e10;
  basize_ = 0;
  // Obv kind of cheating here. Should be handling multiple books.
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], SymbolId{symbol_}});
  const Book<BookLevel>::BookSide<BuySide>& buy_side = bk->side<BuySide>();
  if (!buy_side.empty()) {
    for (auto& lvl : buy_side) {
      if (effectiveQty(lvl, use_flagged_) > Quantity{0}) {
        bb_ = lvl.px.toDouble();
        bbsize_ = effectiveQty(lvl, use_flagged_).toDouble();
        //    std::cout << " Got " << lvl.px.toDouble() << std::endl;
        break;
      } else {
        //    std::cout << " Skipped " << lvl.px.toDouble() << std::endl;
      }
    }

  } else {
    bb_ = 0;
    bbsize_ = 0;
  }

  const Book<BookLevel>::BookSide<SellSide>& sell_side = bk->side<SellSide>();
  if (!sell_side.empty()) {
    for (auto& lvl : sell_side) {
      if (effectiveQty(lvl, use_flagged_) > Quantity{0}) {
        ba_ = lvl.px.toDouble();
        basize_ = effectiveQty(lvl, use_flagged_).toDouble();
        //    std::cout << " Got " << lvl.px.toDouble() << std::endl;
        break;
      } else {
        //     std::cout << " Skipped " << lvl.px.toDouble() << std::endl;
      }
    }

  } else {
    ba_ = 1e10;
    basize_ = 0;
  }
  // std::cout << " ===END=== " << std::endl;

  calc();
}

/*
This is for use if part of the book goes away.
I had this originally defined for hyperliquid, because they only give us
20 levels of the thing. But this might be... yeah.plenty of stuff.

*/
double SigMid::getEstimatedValue() {
  if (valid_) {
    return value_;
  }
  // Otherwise, probably one of the sides is empty.
  // In that case, create an estimated value around the last
  // executed price on that side.
  if (bb_ == 0) {
    // Maybe get the last sell price and take the averge between
    // it and the last value.
    return (last_sell_trd_px_ + value_) / 2;
  } else if (ba_ == 1e10) {
    return (last_buy_trd_px_ + value_) / 2;
  }

  // Otherwise, just return the value still.
  return value_;
}

void SigMid::reset() {
  first_tick_ = true;
  bb_ = 0;
  ba_ = 1e10;
  bbsize_ = 0;
  basize_ = 0;
}

// ***************************************************************
// SigBid

SigBid::SigBid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf)
    : Signal(signal_id, sig_conf, sf), bb_(0), first_tick_(true) {
  symbol_ = SymbolId{sig_conf["symbol"].GetString()};
  for (const rapidjson::Value& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown);
    books_.push_back(pmk);
  }
  // midprice gets a faster update.
  pri_ = 80;
}

std::vector<pktrade::BookId> SigBid::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

bool SigBid::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                    const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigBid") {
    return false;
  }
  // Check if signal and books are identical.
  if (symbol_.get() != other_conf["symbol"].GetString()) {
    return false;
  }
  // Checks that books are same.
  const auto& other_books = other_conf["books"].GetArray();

  if (other_books.Size() != books_.size()) {
    return false;
  }

  for (const rapidjson::Value& book_str : other_books) {
    pktrade::Market book =
        magic_enum::enum_cast<Market>(book_str.GetString()).value_or(Market::Unknown);
    if (std::find(books_.begin(), books_.end(), book) == books_.end()) {
      return false;
    }
  }

  return true;
}

void SigBid::subscribeData(MDBeacon* beacon) {
  // Subscribe to add and delete so far.
  // Snapshot-driven modifies do not
  // change the inside for now.

  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd,
      pktrade::md::BeaconCBType::LvlMod,
      pktrade::md::BeaconCBType::LvlDel,
  };

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);
  }
}

void SigBid::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }
  if (lvl_add.side == Side::Buy && lvl_add.px.toDouble() > bb_) {
    bb_ = lvl_add.px.toDouble();
    calc();
  }
}

void SigBid::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}

void SigBid::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  //  pktrade::util::printInside(bk);
  if (lvl_del.side == Side::Buy && lvl_del.px.toDouble() >= bb_) {
    // Do a lookup.
    // double new_bb = 0;
    fullRecalc();
  }
}

void SigBid::calc() {
  // Can't set a price if it's one-sided.
  if (bb_ == 0) {
    return;
  }
  setVV((bb_), true);
}

void SigBid::fullRecalc() {
  // Writing this out explicitly for practice.
  // Realize I can use getTopBook more easily.

  // Obv kind of cheating here. Should be handling multiple books.
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], SymbolId{symbol_}});
  const Book<BookLevel>::BookSide<BuySide>& buy_side = bk->side<BuySide>();
  if (!buy_side.empty()) {
    bb_ = buy_side.begin()->px.toDouble();
  } else {
    bb_ = 0;
  }

  calc();
}

void SigBid::reset() {
  first_tick_ = true;
  bb_ = 0;
}

std::string SigBid::getPrimarySymbol() const { return symbol_.get(); }

// ***************************************
// SigAsk

SigAsk::SigAsk(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf)
    : Signal(signal_id, sig_conf, sf), ba_(1e10), first_tick_(true) {
  symbol_ = SymbolId{sig_conf["symbol"].GetString()};
  for (const rapidjson::Value& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown);
    books_.push_back(pmk);
  }

  // midprice gets a faster update.
  pri_ = 80;
}

std::vector<pktrade::BookId> SigAsk::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

bool SigAsk::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                    const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigAsk") {
    return false;
  }
  // Check if signal and books are identical.
  if (symbol_.get() != other_conf["symbol"].GetString()) {
    return false;
  }

  // Checks that books are same.
  const auto& other_books = other_conf["books"].GetArray();

  if (other_books.Size() != books_.size()) {
    return false;
  }

  for (const rapidjson::Value& book_str : other_books) {
    pktrade::Market book =
        magic_enum::enum_cast<Market>(book_str.GetString()).value_or(Market::Unknown);
    if (std::find(books_.begin(), books_.end(), book) == books_.end()) {
      return false;
    }
  }

  return true;
}

void SigAsk::subscribeData(MDBeacon* beacon) {
  // TODO: update this when we have multi-books.
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd,
      pktrade::md::BeaconCBType::LvlMod,
      pktrade::md::BeaconCBType::LvlDel,
  };

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);
  }
}

void SigAsk::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }
  if (lvl_add.side == Side::Sell && lvl_add.px.toDouble() < ba_) {
    ba_ = lvl_add.px.toDouble();
    calc();
  }
}

void SigAsk::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}

void SigAsk::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (first_tick_) {
    first_tick_ = false;
    fullRecalc();
    return;
  }

  if (lvl_del.side == Side::Sell && lvl_del.px.toDouble() <= ba_) {
    // Do a lookup.
    // double new_bb = 0;
    fullRecalc();
  }
}

void SigAsk::calc() {
  // Can't set a price if it's one-sided.
  if (ba_ == 1e10) {
    return;
  }
  setVV((ba_), true);
}

void SigAsk::fullRecalc() {
  // Writing this out explicitly for practice.
  // Realize I can use getTopBook more easily.

  // Obv kind of cheating here. Should be handling multiple books.
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], SymbolId{symbol_}});
  const Book<BookLevel>::BookSide<SellSide>& sell_side = bk->side<SellSide>();
  if (!sell_side.empty()) {
    ba_ = sell_side.begin()->px.toDouble();
  } else {
    ba_ = 1e10;
  }

  calc();
}

void SigAsk::reset() {
  first_tick_ = true;
  ba_ = 1e10;
}

std::string SigAsk::getPrimarySymbol() const { return symbol_.get(); }

//////////////////////////////////////////////////////////////////////////////////
// SigQuoteMid
SigQuoteMid::SigQuoteMid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf)
    : Signal(signal_id, sig_conf, sf) {
  symbol_ = SymbolId{sig_conf["symbol"].GetString()};
  for (const rapidjson::Value& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown);
    books_.push_back(pmk);
  }
  if (sig_conf.HasMember("invert")) {
    invert_ = sig_conf["invert"].GetBool();
  }

  // midprice gets a faster update.
  pri_ = 70;
}

std::vector<pktrade::BookId> SigQuoteMid::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

bool SigQuoteMid::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                         const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigQuoteMid") {
    return false;
  }

  // Check if signal and books are identical.
  if (symbol_.get() != other_conf["symbol"].GetString()) {
    return false;
  }
  const bool other_invert = other_conf.HasMember("invert") && other_conf["invert"].GetBool();
  if (other_invert != invert_) {
    return false;
  }

  // Checks that books are same.
  const auto& other_books = other_conf["books"].GetArray();

  if (other_books.Size() != books_.size()) {
    return false;
  }

  for (const rapidjson::Value& book_str : other_books) {
    pktrade::Market book =
        magic_enum::enum_cast<Market>(book_str.GetString()).value_or(Market::Unknown);
    if (std::find(books_.begin(), books_.end(), book) == books_.end()) {
      return false;
    }
  }

  return true;
}

void SigQuoteMid::subscribeData(MDBeacon* beacon) {
  // Subscribe to add and delete so far.
  // Snapshot-driven modifies do not
  // change the inside for now.

  std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Quote};

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);
  }
}

// modifies can now include mid-changing situations now.
void SigQuoteMid::onQuote(const LevelBook& bk, const Quote& quote) {

  // Maybe insert a safety valve here too.
  if (quote.bb.toDouble() == 0 || quote.ba.toDouble() == 0) {
    // Exit fast.
    LOG(INFO) << "Exiting sigquote because of bad quote"
              << fmt::format("bid {} ask {} mid {}", quote.bb.toDouble(), quote.ba.toDouble(),
                             quote.mid);
    return;
  }

  // Skip first few because data can be bad.
  if (first_skipped_count_ < SKIP_FIRST_N) {
    first_skipped_count_++;
    return;
  }

  if (quote.mid > 0) {
    const double mid = invert_ ? 1.0 / quote.mid : quote.mid;

    // Do a sanity check.
    // I added this n_skipped_in_row_ thing because I did notice funny behavior
    // around the start of the morning session, we actually started skipping good quotes because it
    // initialized poorly.
    if (value_ != 0 && (mid / value_ > 10 || mid / value_ < 0.1)) {

      if (n_skipped_in_row_ > 10) {
        // Still set the new price then.
      } else {
        n_skipped_in_row_++;

        LOG(WARNING) << fmt::format(
            "Mid Price for {} changed by a TON, lastvalue {}, bb {} ba {} mid {} | skipping {}",
            symbol_.get(), value_, quote.bb.toDouble(), quote.ba.toDouble(), mid,
            n_skipped_in_row_);
        return;
      }
    }
    n_skipped_in_row_ = 0;
    // Update the transact time so our ordexes know to trust this signal.
    last_transact_t_ = quote.source_t;

    setVV(mid, true);
  } else if (quote.bb.toDouble() > 0 && quote.ba.toDouble() > 0) {
    double mid = (quote.bb.toDouble() + quote.ba.toDouble()) / 2;
    if (invert_) {
      mid = 1.0 / mid;
    }
    if (value_ != 0 && (mid / value_ > 10 || mid / value_ < 0.1)) {

      if (n_skipped_in_row_ > 10) {
        // Still set the new price then
      } else {
        n_skipped_in_row_++;

        LOG(WARNING) << fmt::format(
            "Mid Price for {} changed by a TON, lastvalue {}, bb {} ba {} mid {} | skipping",
            symbol_.get(), value_, quote.bb.toDouble(), quote.ba.toDouble(), quote.mid);
        return;
      }
    }
    n_skipped_in_row_ = 0;
    // Update the transact time.
    last_transact_t_ = quote.source_t;

    setVV(mid, true);
  }
}

void SigQuoteMid::reset() { setVV(0, false); }

std::string SigQuoteMid::getPrimarySymbol() const { return symbol_.get(); }

} // namespace pktrade::signals
