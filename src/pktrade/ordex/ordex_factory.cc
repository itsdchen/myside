#include "ordex_factory.h"

#include "ordex.h"
#include "pktrade/context/global_vars.h"

#include "alpha_rel_wide_mm.h"
#include "diagnostics_mm.h"
#include "rel_cross.h"
#include "rel_wide_mm_2.h"
#include "trade_recorder.h"
#include "wide_mm.h"

namespace pktrade::ordex {

Ordex* OrdexFactory::makeByConf(SymbolId symbol, rapidjson::Value& conf,
                                pktrade::signals::SignalFactory* sf,
                                pktrade::tempos::TempoFactory* tf) {
  std::string ordex_type = conf["type"].GetString();
  Ordex* new_ordex = nullptr;
  if (ordex_type == "RelWideMM2") {
    new_ordex = new RelWideMM2(symbol, conf, sf, tf);
  } else if (ordex_type == "RelCross") {
    new_ordex = new RelCross(symbol, conf, sf, tf);
  } else if (ordex_type == "AlphaRelWideMM") {
    new_ordex = new AlphaRelWideMM(symbol, conf, sf, tf);
  } else if (ordex_type == "DiagnosticsMM") {
    new_ordex = new DiagnosticsMM(symbol, conf, sf, tf);
  } else if (ordex_type == "TradeRecorder") {
    new_ordex = new TradeRecorder(symbol, conf, sf, tf);
  } else if (ordex_type == "WideMM") {
    new_ordex = new WideMM(symbol, conf, sf, tf);
  } else {
    throw std::runtime_error("OrdexFactory doesn't support type " + ordex_type);
  }

  ordexes_.push_back(new_ordex);

  return new_ordex;
}

std::vector<BookId> OrdexFactory::allUsedBooks() {
  // Ask its children.
  std::set<pktrade::BookId> ids;
  // Add from all signals.
  for (Ordex* ordex : ordexes_) {
    std::vector<pktrade::BookId> sig_bookids = ordex->getBookIds();
    ids.insert(sig_bookids.begin(), sig_bookids.end());
  }
  std::vector<pktrade::BookId> ids_vec(ids.begin(), ids.end());
  return ids_vec;
}

void OrdexFactory::subscribeAll() {
  for (Ordex* ordex_ : ordexes_) {
    ordex_->subscribeData(pktrade::GlobalVar::md_beacon_);
  }
}

} // namespace pktrade::ordex
