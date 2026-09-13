#pragma once

#include <mutex>

#include "local_gateways/local_gw.h"
#include "pktrade/oeapi.h"
#include "pktrade/risk/trade_risk_man.h"

/*
Context to handle live trading, but where the gateway is a part of
the current trader.

*/

namespace pktrade {

class LocalContext {
 public:
  LocalContext(std::string local_risk_file);

  void setRiskMan(SymbolId sym, risk::TradeRiskMan* riskman);

  // Pushing things, TRM->gateway.
  PKOrderId onNewOrd(const NewOrder& ord);
  void onCancelOrd(const CancelOrder& cxl);

  // Pushing things, gateway -> TRM. I'm going to start
  // wrapping all of these in a two-tiered callback scheme, so that we can
  // keep things synchronous in the polling scheme. That keeps us safer from
  // threading-based race conditions. Otherwise, I think I would have to place
  // locks around a ton of places, which gets net very confusing.
  void handleNewOrdAck(PKCOID pk_coid, std::string exch_oid, int64_t exch_transact_t);
  void handleNewOrdAckCB(PKCOID pk_coid, std::string exch_oid, int64_t exch_transact_t);

  void handleCancelAck(PKCOID pk_coid, int64_t exch_transact_t);
  void handleCancelAckCB(PKCOID pk_coid, int64_t exch_transact_t);

  void handleExec(PKCOID pk_coid, Price trd_px, Quantity trd_qty, int64_t trd_id, int64_t trd_t,
                  bool add_liq);

  void handleExecCB(PKCOID pk_coid, Price trd_px, Quantity trd_qty, int64_t trd_id, int64_t trd_t,
                    bool add_liq);

  void handleElim(PKCOID pk_coid);
  void handleElimCB(PKCOID pk_coid);

  void handleNewOrdReject(PKCOID pk_coid, std::string reason);
  void handleNewOrdRejectCB(PKCOID pk_coid, std::string reason);

  /*
  // All right. It seems like we didn't even have this doing anything in
  the original gateway. So maybe we don't need i for now.

  void handleCancelOrdReject(const pktrade::gateway::PbCancelOrderReject& msg);
  void handleCancelOrdRejectFromPKCOID()
 */

  const std::unordered_map<PKOrderId, Order>& getOrders() { return orders_; }
  const Order* getOrder(PKOrderId pk_order_id);

 protected:
  // incr every time.
  int cur_ord_id_;

  // I guess the TradeMan gets to set the PKOrderId?
  // OK. I guess we can live with that.
  std::unordered_map<PKOrderId, Order> orders_;

  // Is this stupid, I'm inntroducing some extra scaffolding here right now that
  // we don't really need. I'm sorry.
  std::map<PKOrderId, PKCOID> exec_oid_to_pkcoid_;
  std::map<PKCOID, PKOrderId> pkcoid_to_exec_oid_;

 private:
  // Lock for when we get a callback from the local_gateway. This is so we
  // don't insert two callbacks simultaneously (and potentially cause
  // a corruption inside the timer, jeez.)
  // Actually, I'm going to have it wrap all the instances where we ping or
  // hear back from the exchange. Because we are multithreadedly updating
  // some of our data structures (eg. exec_oid_to_pkcoid_) in all of these.
  mutable std::recursive_mutex ordio_mtx_;

  pktrade::localgateway::LocalGateway* local_gw_ = nullptr;

  std::unordered_map<SymbolId, risk::TradeRiskMan*> sym_to_riskman_;
};

} // namespace pktrade
