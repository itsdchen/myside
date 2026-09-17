#include "live_context.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/risk/trade_risk_man.h"

namespace pktrade {

LiveContext::LiveContext() : oe_subscriber_{ctx_, ZMQ_SUB}, oe_publisher_{ctx_, ZMQ_PUB} {
  // Set the socket addresses.

  // TODO: check this is the exact one that the gateway uses.
  // The socket we publish to when we make new orders.
  oe_outbound_socket_ = "ipc:///tmp/gateway-oe-sub-sock";

  // The socket we listen to when we get things back from gateway.
  oe_inbound_socket_ = "ipc:///tmp/gateway-oe-pub-sock";

  cur_ord_id_ = 0;
}

void LiveContext::setRiskMan(SymbolId sym, risk::TradeRiskMan* riskman) {
  sym_to_riskman_[sym] = riskman;
}

void LiveContext::connect() {
  oe_subscriber_.connect(oe_inbound_socket_);
  oe_subscriber_.set(zmq::sockopt::subscribe, "");

  oe_publisher_.connect(oe_outbound_socket_);

  // Gonna just.. poll it.
  // Had to change this to be sooner bc we were starting to poll after we started putting out
  // orders.
  pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                 std::chrono::seconds(1),
                                             [&] { this->pollOnce(); });
}

void LiveContext::disconnect() {
  oe_publisher_.disconnect(oe_outbound_socket_);
  LOG(INFO) << "Disconnected ZMQ OE publisher from " << oe_outbound_socket_;

  oe_subscriber_.disconnect(oe_inbound_socket_);
}

// The intent is for the LiveContext to be a pollable object.
// So like, create it, register w/ event loop ~creation time.
// Hearing back from the gateway.
void LiveContext::pollOnce() {
  LOG_FIRST_N(INFO, 5) << " LiveContext: Starting to poll";
  // call itself in the future.

  zmq::pollitem_t items[] = {
      {oe_subscriber_, 0, ZMQ_POLLIN, 0},
  };
  zmq_pollitem_t m;
  zmq::message_t msg;

  // The "1" is the number of pollable items.
  // The 3rd argument is crucial. If you don't don't set it to 0, it will block
  zmq::poll(&items[0], 1, std::chrono::seconds(0));

  if (items[0].revents & ZMQ_POLLIN) {
    auto rc = oe_subscriber_.recv(msg);
    if (rc) {
      pktrade::gateway::PbMessage pb_msg;
      auto success = pb_msg.ParseFromArray(msg.data(), msg.size());
      if (success) {
        VLOG(1) << pb_msg.ShortDebugString();
        std::lock_guard<std::mutex> lck(mtx_);
        handle(pb_msg);
        // Don't think we need this anymore tbh.
        // LOG_EVERY_N(INFO, 53) << " LiveContext::pollOnce got msg from gateway: "
        //                      << pb_msg.ShortDebugString();

      } else {
        LOG(ERROR) << "Failed to decode incoming data on OE queue";
      }
    }
  }

  // Decreases the polling loop so we can get updated faster.
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::milliseconds(10),
                                             [&] { this->pollOnce(); });

  LOG_FIRST_N(INFO, 10) << " LiveContext: end PollOnce process.";
}

PKOrderId LiveContext::onNewOrd(const NewOrder& nord) {
  // Create the appropriate proto, serialize, and publish.
  // Also, create an order object for ourselves.
  // Order* ord = new Order(cur_ord_id_, nord.symbol, nord.mkt, nord.side,
  //                       nord.qty, nord.px);

  orders_.insert({cur_ord_id_,
                  {cur_ord_id_, nord.symbol, nord.mkt, nord.side, nord.qty, nord.px,
                   nord.time_in_force, nord.order_type}});
  Order& the_ord = orders_.find(cur_ord_id_)->second;
  the_ord.curr_qty = nord.qty;

  // ord->curr_qty = nord.qty;
  // orders_[ord->order_id] = ord;

  cur_ord_id_++;

  pktrade::gateway::PbMessage msg;
  auto* new_ord = msg.mutable_new_order();

  // RIP
  //  new_ord->set_executor_id(1);
  new_ord->set_executor_order_id(the_ord.pk_order_id);
  new_ord->set_symbol(nord.symbol.get());
  new_ord->set_market(static_cast<std::underlying_type<Market>::type>(nord.mkt));
  new_ord->set_side(static_cast<std::underlying_type<Side>::type>(nord.side));
  new_ord->set_qty(std::to_string(nord.qty.toDouble()));
  new_ord->set_px(std::to_string(nord.px.toDouble()));
  new_ord->set_order_type(static_cast<std::underlying_type<OrderType>::type>(nord.order_type));

  // TODO: in the future, maybe also allow nonioc.

  new_ord->set_time_in_force(
      static_cast<std::underlying_type<TimeInForce>::type>(nord.time_in_force));
  new_ord->set_post_only(false);
  new_ord->set_strategy_id(pktrade::GlobalVar::strat_id_);
  publish(msg);

  return the_ord.pk_order_id;
}

void LiveContext::onCapitalReq(const CapitalReq& req, SymbolId sym) {
  // Fire-and-forget: the gateway owns minting and order gating. No Order object, no reply.
  pktrade::gateway::PbMessage msg;
  auto* pb_cap = msg.mutable_capital_req();
  pb_cap->set_strategy_id(pktrade::GlobalVar::strat_id_);
  pb_cap->set_symbol(sym.get());
  pb_cap->set_outcome(req.outcome);
  pb_cap->set_target_complete_sets(std::to_string(req.target_complete_sets.toDouble()));
  publish(msg);
}

void LiveContext::onCancelOrd(const CancelOrder& cxl) {
  pktrade::gateway::PbMessage msg;
  auto* cxl_ord = msg.mutable_cancel_order();
  // What's my acutal ID? TODO: set this.
  // cxl_ord->set_executor_id(1);
  cxl_ord->set_executor_order_id(cxl.pk_order_id);
  cxl_ord->set_strategy_id(pktrade::GlobalVar::strat_id_);
  // Publish to gateway.

  publish(msg);
}

void LiveContext::handle(const pktrade::gateway::PbMessage& msg) {
  // LOG(INFO) << " live_context: handling a gateway message";

  if (msg.has_new_ack()) {
    // LOG(INFO) << " live_context: handling a new ack";

    // If we're running with multiple strat_ids, make sure we're
    // only handling messages for our own strat_id.
    if (msg.new_ack().strategy_id() != pktrade::GlobalVar::strat_id_) {
      return;
    }

    handle(msg.new_ack());
  } else if (msg.has_cancel_ack()) {
    if (msg.cancel_ack().strategy_id() != pktrade::GlobalVar::strat_id_) {
      return;
    }

    handle(msg.cancel_ack());
  } else if (msg.has_elim()) {
    // LOG(INFO) << " live_context: handling a new elim";
    if (msg.elim().strategy_id() != pktrade::GlobalVar::strat_id_) {
      return;
    }

    handle(msg.elim());
  } else if (msg.has_execution()) {
    // LOG(INFO) << " live_context: handling a new execution";
    if (msg.execution().strategy_id() != pktrade::GlobalVar::strat_id_) {
      return;
    }
    handle(msg.execution());
  } else if (msg.has_new_reject()) {
    if (msg.new_reject().strategy_id() != pktrade::GlobalVar::strat_id_) {
      return;
    }
    handle(msg.new_reject());
  } else if (msg.has_cancel_reject()) {
    if (msg.cancel_reject().strategy_id() != pktrade::GlobalVar::strat_id_) {
      return;
    }
    handle(msg.cancel_reject());
  } else if (msg.has_gateway_ack()) {
    // LOG(INFO) << " live_context: handling gateway ack";
    if (msg.gateway_ack().strategy_id() != pktrade::GlobalVar::strat_id_) {
      return;
    }
    handle(msg.gateway_ack());
  }

  else {
    std::cout << " ***********************************" << std::endl;
    std::cout << " Gateway: I don't know what it really was..." << std::endl;
    std::cout << msg.DebugString() << std::endl;
    std::cout << " ***********************************" << std::endl;
  }
}

// Note that all of these are keyed by the executor_order_id. Which is great.
void LiveContext::handle(const pktrade::gateway::PbNewOrderAck& msg) {
  NewOrderAck ack;
  ack.pk_order_id = msg.executor_order_id();
  ack.exch_oid = msg.exch_oid();
  ack.exch_transact_time = msg.exch_transact_time();

  // Um. I don't really know how I'm supposed to find out what the Order is?
  // I guess I should populate this and store it locally.
  Order& ord = orders_.find(msg.executor_order_id())->second;
  ord.exchange_order_id = msg.exch_oid();
  ord.state = OrderState::Open;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, ack);
}

void LiveContext::handle(const pktrade::gateway::PbCancelOrderAck& msg) {
  // Do nothing. We wait until the callback from the market.

  CancelOrderAck ack;
  ack.pk_order_id = msg.executor_order_id();
  ack.exch_transact_time = msg.exch_transact_time();

  // Um. I don't really know how I'm supposed to find out what the Order is?
  // I guess I should populate this and store it locally.
  Order& ord = orders_.find(msg.executor_order_id())->second;
  ord.state = OrderState::Closed;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, ack);
}

void LiveContext::handle(const pktrade::gateway::PbOrderExecute& msg) {
  Order& ord = orders_.find(msg.executor_order_id())->second;
  ord.curr_qty -= Quantity{msg.trade_qty()};
  if (ord.curr_qty.toDouble() <= 0) {
    // An elim followed by an exec could send this negative.
    ord.curr_qty = Quantity{0};
    ord.state = OrderState::Closed;
  }

  OrderExecute exec;
  exec.pk_order_id = msg.executor_order_id();
  exec.qty = Quantity{msg.trade_qty()};
  exec.px = Price{msg.trade_px()};
  // In reality, we should look this up from the ord.
  exec.side = ord.side;
  exec.feed_trade_id = msg.feed_trade_id();
  exec.exch_transact_time = msg.exch_transact_time();
  exec.add_liq = msg.add_liq();

  // Now, inform traderiskman.
  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, exec);
}

void LiveContext::handle(const pktrade::gateway::PbOrderElimination& msg) {
  Order& ord = orders_.find(msg.executor_order_id())->second;

  OrderElimination elim;
  elim.order_id = msg.executor_order_id();
  ord.state = OrderState::Closed;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, elim);
  ord.curr_qty = Quantity{0};
}

void LiveContext::handle(const pktrade::gateway::PbNewOrderReject& msg) {
  Order& ord = orders_.find(msg.executor_order_id())->second;

  NewOrderReject rej;
  rej.pk_order_id = msg.executor_order_id();
  rej.reason = msg.reason();

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, rej);
}

void LiveContext::handle(const pktrade::gateway::PbCancelOrderReject& msg) {
  // Do nothing right now. We aren't doing cancels.

  Order& ord = orders_.find(msg.executor_order_id())->second;
  CancelOrderReject rej;
  rej.pk_order_id = msg.executor_order_id();
  rej.reason = msg.reason();

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, rej);
}

void LiveContext::handle(const pktrade::gateway::PbGatewayAck& msg) {
  // forward it to the trade risk man.
  Order& ord = orders_.find(msg.executor_order_id())->second;

  GatewayAck ack;
  ack.action = NewOrder{
      .symbol = ord.symbol, .mkt = ord.mkt, .side = ord.side, .qty = ord.open_qty, .px = ord.px};
  ack.rejected = msg.rejected();
  ack.reason = msg.reason();
  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, ack);
}

void LiveContext::publish(const pktrade::gateway::PbMessage& msg) {
  zmq::message_t m(msg.ByteSizeLong());
  msg.SerializeToArray(m.data(), msg.ByteSizeLong());
  oe_publisher_.send(m, zmq::send_flags::dontwait);
  // VLOG(1) << msg.ShortDebugString();
}

const Order* LiveContext::getOrder(PKOrderId pk_oid) {
  auto it = orders_.find(pk_oid);
  if (it == orders_.end()) {
    return nullptr;
  }
  return &it->second;
}

} // namespace pktrade
