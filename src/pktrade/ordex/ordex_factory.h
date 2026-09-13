#pragma once

#include "ordex.h"

#include <rapidjson/document.h>
#include <set>
#include <vector>

namespace pktrade::signals {
class SignalFactory;
}

namespace pktrade::tempos {
class TempoFactory;
}

namespace pktrade::ordex {

class Ordex;

class OrdexFactory {
 public:
  OrdexFactory(){};

  // I don't think the ordexes should be named.
  // So let's just... make them?

  Ordex* makeByConf(SymbolId symbol, rapidjson::Value& conf, pktrade::signals::SignalFactory* sf,
                    pktrade::tempos::TempoFactory* tf);

  // This gets all the markets we
  std::vector<BookId> allUsedBooks();

  void subscribeAll();

 private:
  std::vector<Ordex*> ordexes_;
};

} // namespace pktrade::ordex
