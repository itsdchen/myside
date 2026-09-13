#pragma once

#include <memory>

#include "pktrade/types.h"

// OK. This is a bit much.
#include "pktrade/pktrader/pktrader.h"

#include "um_handler.h"

namespace pktrade {

// Contains multiple pktraders.
// This handles the initialization aspect of pktraders.
class TradeCarrier {
 public:
  TradeCarrier(rapidjson::Document& trade_doc, std::string strat_label, std::string output_dir);

  std::vector<BookId> getAllSubs();
  std::vector<BookId> getTradingSubs();

  void postSecmaster();
  void subscribeAndFinalize();

  // For global daystart activities that are shared
  // across the pktrader processes.
  void globalDayStart();

  void heartbeat();

  // Used for printing out acct lines. Esp in sim.
  void printAcctLines();

  // If we do a cancel via SIGINT or something, then we cancel all orders
  // and do EOD processes.
  void emergencyEOD();

  // Force-print acct lines at EOD.
  void eod();

  // Callback to be called from UMHandler
  void handleUM(const UserMsg& um);

  // Loads an inherit schedule CSV and registers onTimeout callbacks for sim replay.
  void loadInheritSchedule(const std::string& path);

  // Loads a usermsg schedule TSV and registers onTimeout callbacks for sim replay.
  void loadUsermsgSchedule(const std::string& path);

  // Returns the global position of given sym, refreshing if necessary
  double getGlobalPosition(SymbolId sym);

  // Returns true iff the Hyperliquid endpoint is not responding normally
  bool isNetworkDown() { return network_down_; }

 private:
  std::vector<SymbolId> traded_symbols_;
  std::vector<PKTrader*> pktraders_;
  std::unordered_map<SymbolId, PKTrader*> sym_to_pktrader_;
  std::unordered_map<SymbolId, double> global_positions_;
  std::string dex_name_ = "";
  int64_t global_positions_t_ = 0; // time global positions last retrieved, in ms
  std::unique_ptr<UMHandler> umh;
  bool network_down_ = false;
  uint64_t last_event_count_ = 0;
  Clock::time_point last_event_check_t_; // time we last called checkEventLoop
  bool alert_sent_ = false;

  // For tracking the official oracle prices and maybe later for checking network status
  ix::WebSocket ws_;

  // Pings Hyperliquid to get all current positions
  void getGlobalPositions();

  // Pings Hyperliquid just to see if it's up
  void checkNetwork();

  // Checks to see if event loop has made progress since the last check
  void checkEventLoop();

  // For subscribing and maintaining Hyperliquid websocket to get oracle prices
  void subscribeHL();
  void heartbeatHL();
};

} // namespace pktrade
