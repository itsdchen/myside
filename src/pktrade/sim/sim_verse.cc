#include "sim_verse.h"

#include "sim_lvl_inst.h"
#include <magic_enum.hpp>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/time_utils.h"

#include <fmt/format.h>

namespace pktrade::sim {

void SimVerse::addTradedSymbol(BookId bid, pktrade::risk::TradeRiskMan* riskman,
                               rapidjson::Value& sim_conf) {
  if (simmers_.find(bid) != simmers_.end()) {
    // Already made this simulator. It's ok.
    return;
  }

  SimBase* sb = nullptr;
  Market mkt = bid.first;

  // What I'm going to try now is to give the
  if (mkt == Market::Hyperliquid) {
    sb = new HyperliquidSim(bid.second, riskman, sim_conf["Hyperliquid"]);
  } else {
    throw std::runtime_error(
        fmt::format("Could not recognize market {}", magic_enum::enum_name(mkt)));
  }

  simmers_[bid] = sb;
}

void SimVerse::setRiskMan(SymbolId sym, risk::TradeRiskMan* riskman) {
  sym_to_riskman_[sym] = riskman;
}

void SimVerse::subscribeData(pktrade::md::MDBeacon* beacon) {
  for (auto& [book_id, sim] : simmers_) {
    sim->subscribeData(beacon);
  }
}

std::vector<BookId> SimVerse::getRequiredDataSubs() {
  std::vector<BookId> subs;
  for (auto& [book_id, sim] : simmers_) {
    std::vector<BookId> sim_subs = sim->getRequiredDataSubs();
    subs.insert(subs.end(), sim_subs.begin(), sim_subs.end());
  }
  return subs;
}

void SimVerse::eod() {
  // Try to eod everything.
  for (auto& [book_id, sim] : simmers_) {
    sim->eod();
  }
}

PKOrderId SimVerse::onNewOrder(const pktrade::NewOrder& new_ord) {

  // Multiplex on the bid.
  BookId bid{new_ord.mkt, new_ord.symbol};

  orders_.insert({cur_ord_id_,
                  {cur_ord_id_, new_ord.symbol, new_ord.mkt, new_ord.side, new_ord.qty, new_ord.px,
                   new_ord.time_in_force, new_ord.order_type}});

  Order& the_ord = orders_.find(cur_ord_id_)->second;

  the_ord.curr_qty = new_ord.qty;
  // Send the order.
  simmers_[bid]->onSentOrder(&the_ord);
  cur_ord_id_++;

  return cur_ord_id_ - 1;
}

void SimVerse::onCancelOrd(const pktrade::CancelOrder& cxl) {
  auto ord_iter = orders_.find(cxl.pk_order_id);
  if (ord_iter == orders_.end()) {
    // This is not expected.
    throw std::runtime_error(
        fmt::format("Tried to cancel {} which was not found", cxl.pk_order_id));
  }

  Order& the_ord = ord_iter->second;
  BookId bid{the_ord.mkt, the_ord.symbol};

  // Send cancel to the correct simulator.
  simmers_[bid]->onCancelOrder(cxl);
}

// Callbacks from the simulators.

void SimVerse::newOrderAck(PKOrderId pk_ord_id) {
  NewOrderAck ack;
  ack.pk_order_id = pk_ord_id;
  ack.exch_oid = "";
  ack.exch_transact_time = time_utils::nowToMs();

  // Um. I don't really know how I'm supposed to find out what the Order is?
  // I guess I should populate this and store it locally.
  Order& ord = orders_.find(pk_ord_id)->second;
  ord.state = OrderState::Open;

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[ord.symbol];
  relevant_riskman->onOE(ord, ack);
}

void SimVerse::cancelOrderAck(PKOrderId pk_ord_id) {
  Order& the_ord = orders_.find(pk_ord_id)->second;

  the_ord.state = OrderState::Closed;

  CancelOrderAck ack;
  ack.pk_order_id = pk_ord_id;
  ack.exch_transact_time = time_utils::nowToMs();

  pktrade::risk::TradeRiskMan* relevant_riskman = sym_to_riskman_[the_ord.symbol];
  relevant_riskman->onOE(the_ord, ack);
}

void SimVerse::handleOrderExecute(OrderExecute ex) {
  Order& the_ord = orders_.find(ex.pk_order_id)->second;
  // Modify curr_qty
  the_ord.curr_qty -= ex.qty;
  pktrade::risk::TradeRiskMan* trm = sym_to_riskman_[the_ord.symbol];
  trm->onOE(the_ord, ex);

  if (the_ord.curr_qty.toDouble() <= 0) {
    the_ord.state = OrderState::Closed;
  }
}

void SimVerse::handleOrderElimination(OrderElimination oelim) {
  Order& the_ord = orders_.find(oelim.order_id)->second;
  pktrade::risk::TradeRiskMan* trm = sym_to_riskman_[the_ord.symbol];
  trm->onOE(the_ord, oelim);
  the_ord.curr_qty = Quantity{0};
}

void SimVerse::handleOrderReject() {}

void SimVerse::handleCancelOrderReject(CancelOrderReject cxl_reject) {
  Order& the_ord = orders_.find(cxl_reject.pk_order_id)->second;
  the_ord.state = OrderState::Closed;

  pktrade::risk::TradeRiskMan* trm = sym_to_riskman_[the_ord.symbol];
  trm->onOE(the_ord, cxl_reject);
}

Order* SimVerse::getOrder(PKOrderId ord_id) {
  auto it = orders_.find(ord_id);
  if (it == orders_.end()) {
    return nullptr;
  }
  return &it->second;
}

} // namespace pktrade::sim
