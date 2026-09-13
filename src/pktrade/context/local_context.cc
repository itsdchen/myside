#include "local_context.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/context/local_gateways/local_binance_gw.h"
#include "pktrade/risk/trade_risk_man.h"
#include "pktrade/util/basiclib.h"

namespace pktrade {

LocalContext::LocalContext(std::string local_risk_file) {
  cur_ord_id_ = 0;
  // Read the config, set us up the correct local_gateway.

  rapidjson::Document risk_conf = pktrade::util::read_json_file(local_risk_file);

  if (!risk_conf.IsObject()) {
    throw std::runtime_error("Local Risk Conf at " + local_risk_file +
                             " did not process properly. Perhaps it's well-formatted");
  }

  // Risk_conf format is like:
  /*
      "market": "BinanceFutures",
      "num_sessions": 15,
      "http_api_endpoint": "https://fapi.binance.com",
      "api_key":"INSERT_HERE",
      "api_secret": "INSERT_HERE"
  */

  // Some of these fields will be specific to the target mkt. However, the
  // "market" field is read and used all the time.
  Market tgt_market = magic_enum::enum_cast<Market>(risk_conf["market"].GetString()).value();
  if (tgt_market == Market::BinanceFutures) {
    local_gw_ = new pktrade::localgateway::LocalBinanceGateway(risk_conf, this);
  } else {
    throw std::runtime_error("Unsupported market in local risk file.");
  }
}

void LocalContext::setRiskMan(SymbolId sym, risk::TradeRiskMan* riskman) {
  sym_to_riskman_[sym] = riskman;
}

// This, and onCancelOrd do not need CB versions, because they get called from
// within the main event loop. As such, they are also synchronous. Note: if we
// ever put the inputs for THIS to be multithreaded, this will need to be made
// thread-safe.
PKOrderId LocalContext::onNewOrd(const NewOrder& nord) {
  // LOG(INFO) << " START LOCK NEWORD";
  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);

  // Some early bookkeeping.
  std::string pk_coid = util::make_pkcoid(pktrade::GlobalVar::strat_id_, cur_ord_id_);

  exec_oid_to_pkcoid_[cur_ord_id_] = pk_coid;
  pkcoid_to_exec_oid_[pk_coid] = cur_ord_id_;

  orders_.insert({cur_ord_id_,
                  {cur_ord_id_, nord.symbol, nord.mkt, nord.side, nord.qty, nord.px,
                   nord.time_in_force, nord.order_type}});
  Order& the_ord = orders_.find(cur_ord_id_)->second;
  the_ord.curr_qty = nord.qty;

  cur_ord_id_++;

  // send it to local gateway.
  local_gw_->onNewOrd(nord, pk_coid);
  // LOG(INFO) << " END LOCK NEWORD";

  return the_ord.pk_order_id;
}

void LocalContext::onCancelOrd(const CancelOrder& cxl) {
  // LOG(INFO) << " START LOCK CXLORD";

  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);

  // This shouldn't happen, but still, let's be safe.
  if (!exec_oid_to_pkcoid_.contains(cxl.pk_order_id)) {
    LOG(ERROR) << "LocalContext: onCancelOrd: order id " << cxl.pk_order_id
               << " not found in exec_oid_to_pkcoid_";
    return;
  }

  std::string pk_coid = exec_oid_to_pkcoid_[cxl.pk_order_id];

  Order& the_ord = orders_.find(cxl.pk_order_id)->second;
  local_gw_->onCancelOrd(cxl, the_ord.exchange_order_id, the_ord.symbol.get(), pk_coid);
  // LOG(INFO) << " END LOCK CXLORD";
}

void LocalContext::handleNewOrdAck(PKCOID pk_coid, std::string exch_oid, int64_t exch_transact_t) {
  // LOG(INFO) << " START LOCK NEWORDACK";

  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);
  // LOG(INFO) << " INSIDE NEWORDACK";

  // Prob a different strategy's stuff.
  if (!pkcoid_to_exec_oid_.contains(pk_coid)) {
    return;
  }

  // Make a cb for this.
  handleNewOrdAckCB(pk_coid, exch_oid, exch_transact_t);

  /*
  pktrade::GlobalVar::event_loop_->onTimeout(
     [pk_coid = std::string(pk_coid),
                                     // explicit copy strings for safety
                                     exch_oid = std::string(exch_oid),
                                     // int64, ok.
                                     exch_transact_t, this] {
        this->handleNewOrdAckCB(pk_coid, exch_oid, exch_transact_t);
      });
*/
  // LOG(INFO) << " END LOCK NEWORDACK";
}

void LocalContext::handleNewOrdAckCB(PKCOID pk_coid, std::string exch_oid,
                                     int64_t exch_transact_t) {
  // Prob a different strategy's stuff.
  if (!pkcoid_to_exec_oid_.contains(pk_coid)) {
    return;
  }

  PKOrderId exec_oid = pkcoid_to_exec_oid_[pk_coid];

  NewOrderAck ack;
  ack.pk_order_id = exec_oid;
  ack.exch_oid = exch_oid;
  ack.exch_transact_time = exch_transact_t;

  // Update our local notion of the order, call it open.
  Order& ord = orders_.find(exec_oid)->second;
  ord.exchange_order_id = exch_oid;
  ord.state = OrderState::Open;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, ack);
}

void LocalContext::handleCancelAck(PKCOID pk_coid, int64_t exch_transact_t) {
  // LOG(INFO) << " START LOCK CANCELACK " << pk_coid;;

  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);
  // LOG(INFO) << "LocalContext: handleCancelAck " << pk_coid;
  //  Prob a different strategy's stuff.
  if (!pkcoid_to_exec_oid_.contains(pk_coid)) {
    LOG(INFO) << "Returning due to not containing" << pk_coid;
    return;
  }

  // LOG(INFO) << " ABOUT TO CALL HANDLE_CXL_ACKCB";

  handleCancelAckCB(pk_coid, exch_transact_t);

  // Make a cb for this.
  /*
  pktrade::GlobalVar::event_loop_->onTimeout(
      [
          // explicity copy for safety.
          pk_coid = std::string(pk_coid), exch_transact_t, this] {
        this->handleCancelAckCB(pk_coid, exch_transact_t);
      });
*/
  // LOG(INFO) << " END LOCK CANCELACK";
}
void LocalContext::handleCancelAckCB(PKCOID pk_coid, int64_t exch_transact_t) {
  // LOG(INFO) << "HandleCancelAckCB ran for " << pk_coid;

  PKOrderId exec_oid = pkcoid_to_exec_oid_[pk_coid];

  CancelOrderAck ack;
  ack.pk_order_id = exec_oid;
  ack.exch_transact_time = exch_transact_t;

  // Um. I don't really know how I'm supposed to find out what the Order is?
  // I guess I should populate this and store it locally.

  auto it = orders_.find(exec_oid);
  if (it == orders_.end()) {
    LOG(ERROR) << "Cannot find order for exec_oid: " << exec_oid;
    return;
  }
  Order& ord = it->second;

  ord.state = OrderState::Closed;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, ack);
}

void LocalContext::handleExec(PKCOID pk_coid, Price trd_px, Quantity trd_qty, int64_t trd_id,
                              int64_t trd_t, bool add_liq) {
  // LOG(INFO) << " START LOCK HANDLEEXEC";

  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);
  // Prob a different strategy's stuff.
  if (!pkcoid_to_exec_oid_.contains(pk_coid)) {
    return;
  }

  // Make a cb for this.
  handleExecCB(pk_coid, trd_px, trd_qty, trd_id, trd_t, add_liq);

  /*
  pktrade::GlobalVar::event_loop_->onTimeout(
      [pk_coid = std::string(pk_coid), trd_px_double = trd_px.toDouble(),
       trd_qty_double = trd_qty.toDouble(), trd_id, trd_t, add_liq, this] {
        this->handleExecCB(pk_coid, Price{std::to_string(trd_px_double)},
                           Quantity{std::to_string(trd_qty_double)}, trd_id,
                           trd_t, add_liq);
      });
*/
  // LOG(INFO) << " END LOCK HANDLEEXEC";
}

void LocalContext::handleExecCB(PKCOID pk_coid, Price trd_px, Quantity trd_qty, int64_t trd_id,
                                int64_t trd_t, bool add_liq) {
  PKOrderId exec_oid = pkcoid_to_exec_oid_[pk_coid];

  Order& ord = orders_.find(exec_oid)->second;
  ord.curr_qty -= Quantity{trd_qty};
  if (ord.curr_qty.toDouble() <= 0) {
    // An elim followed by an exec could send this negative.
    ord.curr_qty = Quantity{0};
    ord.state = OrderState::Closed;
  }

  OrderExecute exec;
  exec.pk_order_id = exec_oid;
  exec.qty = trd_qty;
  exec.px = trd_px;
  // In reality, we should look this up from the ord.
  exec.side = ord.side;
  exec.feed_trade_id = trd_id;
  exec.exch_transact_time = trd_t;
  exec.add_liq = add_liq;

  // Now, inform traderiskman.
  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, exec);
}

void LocalContext::handleElim(PKCOID pk_coid) {
  // LOG(INFO) << " START LOCK HANDLEELIM";

  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);
  // Prob a different strategy's stuff.
  if (!pkcoid_to_exec_oid_.contains(pk_coid)) {
    return;
  }

  // Make a cb for this.
  handleElimCB(pk_coid);
  /*
  pktrade::GlobalVar::event_loop_->onTimeout(
      [pk_coid = std::string(pk_coid), this] { this->handleElimCB(pk_coid); });
      */
  // LOG(INFO) << " END LOCK HANDLEELIM";
}

void LocalContext::handleElimCB(PKCOID pk_coid) {
  PKOrderId exec_oid = pkcoid_to_exec_oid_[pk_coid];

  Order& ord = orders_.find(exec_oid)->second;

  OrderElimination elim;
  elim.order_id = exec_oid;
  ord.state = OrderState::Closed;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, elim);
  ord.curr_qty = Quantity{0};
}

void LocalContext::handleNewOrdReject(PKCOID pk_coid, std::string reason) {
  // LOG(INFO) << " START LOCK NEWREJ";

  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);
  // Prob a different strategy's stuff.
  if (!pkcoid_to_exec_oid_.contains(pk_coid)) {
    return;
  }

  // I'm undoing the callback and replacing it with a direct call, again, because
  // I think there's just some issues with the callback.
  // Unless if I need to just queue it up instead.

  // Maybe let's just push these onto a separate thing and handle them with cancelacks?
  handleNewOrdRejectCB(pk_coid, reason);
  /*
  pktrade::GlobalVar::event_loop_->onTimeout(
      [pk_coid = std::string(pk_coid), reason, this] {
        this->handleNewOrdRejectCB(pk_coid, reason);
      });
  */
  // LOG(INFO) << " END LOCK ORDREJ";
}

void LocalContext::handleNewOrdRejectCB(PKCOID pk_coid, std::string reason) {
  // Acquire a lock here too. Because it's making changes to the order containers.
  std::lock_guard<std::recursive_mutex> lck(ordio_mtx_);

  PKOrderId exec_oid = pkcoid_to_exec_oid_[pk_coid];

  Order& ord = orders_.find(exec_oid)->second;

  NewOrderReject rej;
  rej.pk_order_id = exec_oid;
  rej.reason = reason;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, rej);
}

// TODO: check if this is a thing, I don't know if we have this called at all.
/*
void LocalContext::handleCancelOrdReject(
    const pktrade::gateway::PbCancelOrderReject& msg) {
  // Do nothing right now. We aren't doing cancels.

  Order& ord = orders_.find(msg.executor_order_id())->second;
  CancelOrderReject rej;
  rej.pk_order_id = msg.executor_order_id();
  rej.reason = msg.reason();

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, rej);
}
*/
const Order* LocalContext::getOrder(PKOrderId pk_oid) {
  auto it = orders_.find(pk_oid);
  if (it == orders_.end()) {
    return nullptr;
  }
  return &it->second;
}

} // namespace pktrade
