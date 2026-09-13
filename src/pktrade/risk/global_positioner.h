#pragma once

// #include "trade_risk_man.h"
#include <chrono>
#include <string>

#include "pktrade/types.h"
#include "pktrade/util/sbcaller.h"

/*
  Used to track positions of 24/7-traded strategies. When we do switchovers due
  to trading period, this tries to calculate orphaned positions, how much
  capacity we have, and tries to inherit some orphaned position.

*/

namespace pktrade::risk {

// Stub for later.
class TradeRiskMan;

class GlobalPositioner : public pktrade::util::SBCaller {

 public:
  GlobalPositioner(TradeRiskMan* riskman);

  // This starts off the process. Starts our pinging/writing process.
  void onDayStart();

  // Calculate the number of seconds until the next publishPositionLoop
  const std::chrono::seconds secsUntilNextPublish() const;

  // Does the hyperliquid lookup re: how much position we have.
  double getGlobalPosition();

  // This will add overnight inheritance, as opposed to *set* it.
  void startCalcMyInheritance(double my_pos, double my_max_pos, double global_position);

  // Publish my own position every so often.
  // Start this during the DayStart() calls.
  void publishPositionLoop();

  // Force an immediate inherit calculation (triggered by usermsg).
  void forceInheritLoop();

  // The last part of publishing position
  void performPublishPosition(double sym_position, double my_position, double my_max_pos);

  double getInheritedPosition() { return inherited_position_; }

  // SBCaller interface.
  void onGetPosition(const cpr::Response& res) override;
  void onPostPosition(const cpr::Response& res) override;

 private:
  TradeRiskMan* riskman_ = nullptr;
  SymbolId symbol_;

  int num_times_published_ = 0;

  // 5 mins between each loop. 48 loops = 4 hours.
  int num_pubs_before_reinherit_ = 48;

  // Originally: only calculate this once.
  // Now: calculate it every few hours.
  double inherited_position_ = 0;

  std::chrono::steady_clock::time_point supabase_backoff_until_ =
      std::chrono::steady_clock::time_point::min();
  int supabase_backoff_attempts_ = 0;

  bool inSupabaseBackoff() const;
  std::chrono::seconds supabaseBackoffRemaining() const;
  void recordSupabaseFailure(const std::string& reason);
  void resetSupabaseBackoff();
};

} // namespace pktrade::risk
