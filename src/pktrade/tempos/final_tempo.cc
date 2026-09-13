#include "final_tempo.h"

#include <magic_enum.hpp>

#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::tempos {

FinalTempo::FinalTempo(const rapidjson::Value& conf, int id, TempoFactory* tf)
    : BaseTempo(id, conf["name"].GetString()) {
  symbol_ = SymbolId{conf["symbol"].GetString()};

  // Grab the markets.
  for (const rapidjson::Value& mkt : conf["markets"].GetArray()) {
    Market mkt_enum = *magic_enum::enum_cast<Market>(mkt.GetString());
    books_.push_back(mkt_enum);
  }

  if (conf.HasMember("suppress_trades")) {
    suppress_trades_ = conf["suppress_trades"].GetBool();
  }

  // Set the priority to be lower than the default.
  // TODO: some of these priorities should just be enums.
  pri_ = 90;
}

bool FinalTempo::isSame(const rapidjson::Value& other_conf, TempoFactory* tf) {
  // Check these things are the same.
  if (std::string(other_conf["type"].GetString()) != "FinalTempo") {
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

void FinalTempo::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Add self as a listener.
  std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Trd,
                                                     pktrade::md::BeaconCBType::Final};

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);

    printf("%s is subscribing to finalcallbacks on %s \n", getName().c_str(),
           util::print_bookid(book_id).c_str());
  }
}

std::vector<pktrade::BookId> FinalTempo::getBookIds() {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

void FinalTempo::onTrade(const LevelBook& bk, const Trade& trd) { has_trade_this_batch_ = true; }

void FinalTempo::onFinal(const LevelBook& bk) {
  if (suppress_trades_ && has_trade_this_batch_) {
    // suppress it.
  } else {
    fire();
  }

  // reset this.
  has_trade_this_batch_ = false;
}

} // namespace pktrade::tempos
