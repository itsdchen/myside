#pragma once

#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/oeapi.h"
#include "pktrade/risk/trade_risk_man.h"
#include "sim_base.h"
#include <rapidjson/document.h>
#include <unordered_map>
#include <vector>

namespace pktrade::sim {

class SimBase;

// Container for sims. Right now, counterpart to the LiveContext.
// Changing it to contain similar handlers to the LiveContext too.
class SimVerse {
 public:
  SimVerse(){};

  void addTradedSymbol(BookId bid, pktrade::risk::TradeRiskMan* riskman,
                       rapidjson::Value& sim_conf);

  void setRiskMan(SymbolId sym, risk::TradeRiskMan* riskman);
  void subscribeData(pktrade::md::MDBeacon* beacon);
  std::vector<BookId> getRequiredDataSubs();
  void eod();

  PKOrderId onNewOrder(const pktrade::NewOrder& new_ord);
  void onCancelOrd(const CancelOrder& cxl);

  // Handling "callbacks" from the simulators.

  // No such thing as a gateway ack here.

  // We received and have placed this NewOrd.
  void newOrderAck(PKOrderId pk_ord_id);
  void cancelOrderAck(PKOrderId pk_ord_id);
  void handleOrderExecute(OrderExecute exec);
  void handleOrderElimination(OrderElimination oelim);
  void handleOrderReject();
  void handleCancelOrderReject(CancelOrderReject cxl_reject);

  const std::unordered_map<PKOrderId, Order>& getOrders() { return orders_; }

  Order* getOrder(PKOrderId ord_id);

 protected:
  std::unordered_map<BookId, SimBase*> simmers_;

  // This is to send back.
  std::unordered_map<PKOrderId, Order> orders_;
  std::unordered_map<SymbolId, risk::TradeRiskMan*> sym_to_riskman_;

  PKOrderId cur_ord_id_ = 0;
};

} // namespace pktrade::sim
