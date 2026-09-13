#include "sim_base.h"

namespace pktrade::sim {

SimBase::SimBase(SymbolId sym, pktrade::risk::TradeRiskMan* riskman, rapidjson::Value& sim_conf)
    : sym_(sym), riskman_(riskman) {
  // Set a low beacon priority so we get callbacks early.
  pri_ = 50;
}

} // namespace pktrade::sim
