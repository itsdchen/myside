#pragma once

#include "pktrade/ordex/ordex.h"
#include "pktrade/risk/trade_risk_man.h"
#include "pktrade/types.h"
#include "um_handler.h"

namespace pktrade {

// An instance of a trader.
class PKTrader {
 public:
  PKTrader(rapidjson::Value& pkt_conf, rapidjson::Value& comms_conf, std::string strat_label);

  Market getTradedMarket();

  std::vector<BookId> getExtraSubs();
  std::vector<BookId> getTradingSubs();

  // Tell the sim_verse to create my appropriate simulators.
  void createSimulators(rapidjson::Value& sim_conf);

  void subscribe();

  void postSecmaster();

  const std::string getAcctLine() const;

  void onDayStart();

  pktrade::risk::TradeRiskMan* getRiskMan() { return riskman_; }

  void handleUM(const UserMsg& um);

  void setOraclePx(double oracle_px) { ordex_->setOraclePx(oracle_px); }

 protected:
  // Each one trades a single symbol.
  SymbolId symbol_;
  std::string strat_label_;

  pktrade::ordex::Ordex* ordex_ = nullptr;
  pktrade::risk::TradeRiskMan* riskman_ = nullptr;

  std::vector<pktrade::BookId> extra_subs_;
  double initial_size_mult_ = 1;

  int tl1_secs = 10 * 60; // TL1 10min before EOD
  int tl2_secs = 3 * 60; // TL2 3min before EOD
  int tl5_secs = 10; // TL5 10s before EOD
};

} // namespace pktrade
