#include "inside_tempo.h"

namespace pktrade::tempos {

InsideTempo::InsideTempo(const rapidjson::Value& conf, int id, TempoFactory* tf)
    : BaseTempo(id, conf["name"].GetString()) {
  symbol_ = SymbolId{conf["symbol"].GetString()};

  // Grab markets of course.
  for (const rapidjson::Value& mkt : conf["markets"].GetArray()) {
    Market mkt_enum = *magic_enum::enum_cast<Market>(mkt.GetString());
    books_.push_back(mkt_enum);
  }

  pri_ = 90;
  bb_ = 0;
  ba_ = 1e10;
}

bool InsideTempo::isSame(const rapidjson::Value& other_conf, TempoFactory* tf) {
  if (std::string(other_conf["type"].GetString()) != "InsideTempo") {
    return false;
  }

  // Gotta check other stuff ya
  if (symbol_.get() != other_conf["symbol"].GetString())
    return false;

  // Equivalence == if the sizes are the same and all the members of one
  // are found in the other.
  if (other_conf["markets"].Size() != books_.size())
    return false;

  for (const rapidjson::Value& mkt_str_val : other_conf["markets"].GetArray()) {
    // Check if it exists.
    auto mkt = *magic_enum::enum_cast<Market>(mkt_str_val.GetString());

    if (std::find(books_.begin(), books_.end(), mkt) == books_.end())
      return false;
  }

  return true;
}

void InsideTempo::subscribeData(pktrade::md::MDBeacon* beacon) {
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::Final,
  };

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);
  }
}

std::vector<pktrade::BookId> InsideTempo::getBookIds() {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

// We can ignore flagging for this, because it's not sensitive. Tbh.
void InsideTempo::onFinal(const LevelBook& bk) {
  // Check if bb, ba changed. If so, fire.

  // Check bb.
  double maybe_bb = bk.getSideTop<BuySide>().px.toDouble();
  double maybe_ba = bk.getSideTop<SellSide>().px.toDouble();

  // Again. not super correct. But easy enough and approximately correct.
  // Which, I guess, is good enough for us.
  if (maybe_bb != bb_ || maybe_ba != ba_) {
    bb_ = maybe_bb;
    ba_ = maybe_ba;
    fire();
  }
  // Check ba.
}

} // namespace pktrade::tempos
