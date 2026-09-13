#pragma once

#include "pktrade/book_manager.h"
#include "pktrade/context/live_context.h"
#include "pktrade/context/local_context.h"

#include "pktrade/event_loop.h"
#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/ordex/ordex_factory.h"
#include "pktrade/pktrader/trade_carrier.h"
#include "pktrade/signals/signal_factory.h"
#include "pktrade/sim/sim_verse.h"
#include "pktrade/tempos/tempo_factory.h"

#include "pktrade/util/funding_master.h"
#include "pktrade/util/secmaster.h"

#include <filesystem>

namespace pktrade {

class GlobalVar {
 public:
  static pktrade::EventLoop* event_loop_;

  static pktrade::md::MDBeacon* md_beacon_;
  // Contains all our books.
  static pktrade::BookManager* book_manager_;

  // Factories
  static pktrade::tempos::TempoFactory* tf_;
  static pktrade::signals::SignalFactory* sf_;
  static pktrade::ordex::OrdexFactory* of_;

  // Date
  // Start and end times.
  static std::string date_;

  // The actual start-stop times of our process.
  static Clock::time_point start_t_;
  static Clock::time_point end_t_;

  static pktrade::TradeCarrier* trade_carrier_;
  static pktrade::SecMaster* secmaster_;

  static pktrade::FundingMaster* funding_master_;

  // If we're in live mode or not.
  static bool live_;
  static pktrade::LiveContext* live_context_;

  // Even in live, we have the choice of using the local- or remote- gateway.
  static bool use_local_context_;
  static pktrade::LocalContext* local_context_;

  // Special mode for live.
  static bool paper_trading_mode_;

  // The simverse. I don't know how to best separate things out per-
  // symbol, so let's just start out with a single -verse.
  static pktrade::sim::SimVerse* sim_verse_;

  // The name of the strat
  static std::string strat_id_;

  // true iff we're running on hyperliquid testnet
  static bool hl_testnet_;

  // wallet address on hyperliquid
  static std::string hl_address_;

  // true iff we're running a vault query
  static bool vault_;

  // ignore usermsgs if set
  static bool ignore_usermsgs_;

  // In sim mode, if we want to slow down a market.
  static Market slowdown_market_;
  // a value of 10 would slow down our histfeed of
  // this market by 10ms.
  static int64_t slowdown_market_offset_;

  // For outputting.

  // storing the path instead of the ostream because we'll rewrite it
  // periodically.
  static std::string acct_path_;
  static std::ofstream* acct_out_;
  static std::ofstream* trades_out_;
  static std::ofstream* orders_out_;
  static std::ofstream* logs_out_;

  // For setting where we read/
  static std::string out_dir_;

  // Print verbosely to the log.
  static bool verbose_;
};

} // namespace pktrade
