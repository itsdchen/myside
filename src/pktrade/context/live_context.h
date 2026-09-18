#pragma once

#include "gateway.pb.h"
#include "pktrade/oeapi.h"
#include "pktrade/risk/trade_risk_man.h"
#include <zmq.hpp>

/*

Context to handle live trading.

Mainly, it keeps track of the zmq/way we talk to the gateway.


*/

namespace pktrade {

class LiveContext {
 public:
  LiveContext();

  void setRiskMan(SymbolId sym, risk::TradeRiskMan* riskman);

  void connect();
  void disconnect();

  // Hearing things back from gateway

  // I guess, poll my zmq every so often.
  void pollOnce();

  // Pushing things to the gateway.
  PKOrderId onNewOrd(const NewOrder& ord);
  void onCancelOrd(const CancelOrder& cxl);
  // HIP-4 capitalization requirement (strat -> gateway only). Serializes a PbCapitalReq and
  // publishes it; the gateway owns minting and order gating, so there is no reply. `sym` is the
  // requesting strategy's symbol (carried in the message for the gateway's block set).
  void onCapitalReq(const CapitalReq& req, SymbolId sym);

  // Handle messages from the gateway. These are basically acks from the market
  // or the gateway.
  void handle(const pktrade::gateway::PbMessage& msg);
  void handle(const pktrade::gateway::PbNewOrderAck& msg);
  void handle(const pktrade::gateway::PbCancelOrderAck& msg);
  void handle(const pktrade::gateway::PbOrderExecute& msg);
  void handle(const pktrade::gateway::PbOrderElimination& msg);
  void handle(const pktrade::gateway::PbNewOrderReject& msg);
  void handle(const pktrade::gateway::PbCancelOrderReject& msg);
  // Maybe... deal with these differently?
  void handle(const pktrade::gateway::PbControl& msg);
  void handle(const pktrade::gateway::PbGatewayAck& msg);
  void publish(const pktrade::gateway::PbMessage& msg);

  const std::unordered_map<PKOrderId, Order>& getOrders() { return orders_; }
  const Order* getOrder(PKOrderId pk_order_id);

 private:
  // incr every time.
  int cur_ord_id_;

  std::unordered_map<SymbolId, risk::TradeRiskMan*> sym_to_riskman_;

  // I guess the TradeMan gets to set the PKOrderId?
  // OK. I guess we can live with that.
  std::unordered_map<PKOrderId, Order> orders_;

  // For communicating w/ the gateway, which should be on the same machine.
  std::string oe_outbound_socket_;
  std::string oe_inbound_socket_;

  // For handling events.
  mutable std::mutex mtx_;

  // zmq types for pub/sub with gateway.
  zmq::context_t ctx_;
  // These are the opposite sides of what the gateway has.
  zmq::socket_t oe_subscriber_;
  zmq::socket_t oe_publisher_;
};

} // namespace pktrade
