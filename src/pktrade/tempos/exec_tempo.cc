#include "exec_tempo.h"

#include <magic_enum.hpp>

namespace pktrade::tempos {

ExecTempo::ExecTempo(const rapidjson::Value& conf, int id, TempoFactory* tf)
    : BaseTempo(id, conf["name"].GetString()) {
  symbol_ = SymbolId{conf["symbol"].GetString()};

  // Get markets.
  for (const rapidjson::Value& mkt : conf["markets"].GetArray()) {
    Market mkt_enum = *magic_enum::enum_cast<Market>(mkt.GetString());
    books_.push_back(mkt_enum);
  }
}

bool ExecTempo::isSame(const rapidjson::Value& other_conf, TempoFactory* tf) {
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

void ExecTempo::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Add self as a listener.
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::Trd,
  };

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);
  }
}

std::vector<pktrade::BookId> ExecTempo::getBookIds() {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

void ExecTempo::onTrade(const LevelBook& bk, const Trade& trd) { fire(); }

} // namespace pktrade::tempos
