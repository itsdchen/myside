#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>

#include "pktrade/mdapi.h"
#include "pktrade/types.h"

namespace pktrade {

// Order state transition diagram:
//
// START --> [Pending (NewOrder)]
// Pending --> [Open (NewOrderAck), Closed (OrderExecute, NewOrderReject)]
// Open --> [Closed (OrderExecute, CancelOrderAck)]
enum class OrderState {
  Unknown,
  Pending,
  Open,
  Closed,
};

// Ephemeral/internal order ID for convenience of look-up. Only unique per
// running process. Should not be persisted or used for purposes of
// cross-referencing order info across processes.

// THe int identifier for an order, for a given pktrade process.
using PKOrderId = int32_t;
using StrategyId = std::string;

// Our internal identifier that we send when placing external orders.
using PKCOID = std::string;

// This is the uid of the order that we get back from the exchange. On binance,
// it is clientOrderId, on other exchanges, others.
using ExchOID = std::string;

enum class OrderType {
  Unknown,
  Limit,
  Market,
};

enum class TimeInForce {
  Unknown,
  GTC,
  IOC,
  // Main diff here is that FOK must be completely filled.
  FOK,
  // Add Liquidity Only.
  ALO,
};

// TODO: this should contain some of the NewOrder info (eg. time_in_force,
// post_only) so
//       the simulator can do something with those.
struct Order {
  // TODO: let's rename this to pk_order_id because it's less confusing.
  PKOrderId pk_order_id;
  OrderState state;
  ExchOID exchange_order_id;
  SymbolId symbol;
  Market mkt;
  Side side;
  Quantity open_qty;
  Quantity curr_qty;
  Price px;
  TimeInForce time_in_force;
  OrderType order_type;
  bool pending_modify;
  bool pending_cancel;

  Order(PKOrderId pk_order_id, const SymbolId& symbol, Market market, Side side, Quantity qty,
        Price px, TimeInForce time_in_force, OrderType order_type)
      : pk_order_id{pk_order_id}, state{OrderState::Pending}, symbol{symbol}, mkt{market},
        side{side}, open_qty{qty}, px{px}, order_type{order_type}, pending_modify{false},
        pending_cancel{false}, time_in_force{time_in_force} {}

  std::string to_string() {
    return std::string("ORDER ") + symbol.get() + " " + std::string(magic_enum::enum_name(mkt)) +
           " " + std::string(magic_enum::enum_name(side)) + " open_qty " +
           std::to_string(open_qty.toDouble()) + " curr_qty " +
           std::to_string(curr_qty.toDouble()) + " px " + std::to_string(px.toDouble());
  }
};

// OE Actions

// Note.
// There is kind of a mess here, with time_in_force, post_only, and also the
// way they're handled on the gateway sides.
// Currently, wide_mm can say it's ALO but not post_only, and that's one thing.
// Also, localgateway will just take GTC's and turn them into GTX's. That's all
// "ok" I guess, but I just want to note that we will need to rethink this in
// the future.
// TODO: address, deal with this technical debt, bullshits.
struct NewOrder {
  SymbolId symbol;
  Market mkt;
  Side side;
  Quantity qty;
  Price px;
  OrderType order_type;
  TimeInForce time_in_force;
  bool post_only;
  bool reduce_only = false;

  std::string to_string() const {
    return std::string("NEWORDER ") + symbol.get() + " " + std::string(magic_enum::enum_name(mkt)) +
           " " + std::string(magic_enum::enum_name(side)) + " qty " +
           std::to_string(qty.toDouble()) + " px " + std::to_string(px.toDouble()) +
           " order_type " + std::string(magic_enum::enum_name(order_type)) + " time_in_force " +
           std::string(magic_enum::enum_name(time_in_force)) + " post_only " +
           std::to_string(post_only) + " reduce_only " + std::to_string(reduce_only);
  }
};

// A pktrader/traderiskman interacts with the market by specifying things with
// their pk_order_id. The live_context can between pk_order_id and PKOrderId or
// ExchOID

/////////////////////////////////////////////////////////
// Outbound: from trader -> gateway
struct CancelOrder {
  PKOrderId pk_order_id;
};

struct ModifyOrder {
  PKOrderId pk_order_id;
  Quantity target_qty;
};

// HIP-4 capitalization requirement: the strategy declares how much inventory (complete sets
// of an outcome) it needs to quote a ticker; the gateway owns minting the shortfall and gating
// order placement on it. One-directional (strat -> gateway); no reply. See gateway.proto
// PbCapitalReq.
struct CapitalReq {
  // The HIP-4 outcome id (not the per-side asset id). Encoding is 10*outcome + side.
  int outcome;
  // How many complete sets the strategy needs (~ its max position). Whole shares.
  Quantity target_complete_sets;

  std::string to_string() const {
    return std::string("CAPITALREQ outcome ") + std::to_string(outcome) + " target_sets " +
           std::to_string(target_complete_sets.toDouble());
  }
};

/////////////////////////////////////////////////////////
// Gateway -> trader
struct GatewayAck {
  std::variant<NewOrder, CancelOrder, ModifyOrder> action;
  bool rejected;
  std::string reason;
};

/////////////////////////////////////////////////////////
// Market -> gateway -> trader
struct NewOrderAck {
  PKOrderId pk_order_id;
  ExchOID exch_oid;
  int64_t exch_transact_time;
};

struct NewOrderReject {
  PKOrderId pk_order_id;
  std::string reason;
};

struct CancelOrderAck {
  PKOrderId pk_order_id;
  int64_t exch_transact_time;
};

struct CancelOrderReject {
  PKOrderId pk_order_id;
  std::string reason;
};

struct ModifyOrderAck {
  PKOrderId pk_order_id;
};

struct ModifyOrderReject {
  PKOrderId pk_order_id;
  std::string reason;
};

struct OrderExecute {
  PKOrderId pk_order_id;
  Quantity qty;
  Price px;
  Side side;
  bool add_liq = false;
  int feed_trade_id = 6;
  int64_t exch_transact_time;
};
/////////////////////////////////////////////////////////

// IOC portion that doesn't get filled.
struct OrderElimination {
  PKOrderId order_id;
};
/*
// Just make this available too.
std::string get_new_client_oid(ExecutorId& executor_id,
                               PKOrderId& order_id) {
  return executor_id + ":" + std::to_string(order_id);
}
*/

class Executor {
 public:
  virtual ~Executor() = default;

  virtual PKOrderId sendOrd(const NewOrder&) = 0;
  virtual void cancelOrd(const CancelOrder&) = 0;
  virtual void modOrd(const ModifyOrder&) = 0;

  // HIP-4 capitalization requirement (strat -> gateway). Live-only: the default throws so
  // sim/local executors that never handle collateral don't have to implement it.
  virtual void sendCapitalReq(const CapitalReq&) {
    throw std::runtime_error("sendCapitalReq is not supported by this Executor");
  }
};

class OEListener {
 public:
  virtual ~OEListener() = default;

  virtual void onOE(const Order&, const GatewayAck&) = 0;
  virtual void onOE(const Order&, const NewOrderAck&) = 0;
  virtual void onOE(const Order&, const NewOrderReject&) = 0;
  virtual void onOE(const Order&, const CancelOrderAck&) = 0;
  virtual void onOE(const Order&, const CancelOrderReject&) = 0;
  virtual void onOE(const Order&, const ModifyOrderAck&) = 0;
  virtual void onOE(const Order&, const ModifyOrderReject&) = 0;
  virtual void onOE(const Order&, const OrderExecute&) = 0;
  virtual void onOE(const Order&, const OrderElimination&) = 0;
};

} // namespace pktrade
