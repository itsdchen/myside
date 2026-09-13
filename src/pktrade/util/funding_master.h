#pragma once

#include <rapidjson/document.h>

#include "pktrade/mdapi.h"
#include "pktrade/types.h"
#include "pktrade/util/time_utils.h"
#include <chrono>

#include <map>

/*

Level 1:
This does the reading and calculating for funding rates


Level 2 (and I'm kind of uncomfortable with this):
  - This keeps track of all positions, and informs other listeners
    when positions change.
    - This also lets ordexes become listeners so they know when positions
change.
    - I know this kind of breaks some separation, it's just maybe the
      fastest way to pierce the veil of position management.
    - This should probably be its own thing. But for speed/simplicity I'm
      keeping it here.

*/

namespace pktrade {

class FundingPosListener {
 public:
  virtual ~FundingPosListener() = default;
  virtual void onPosChange(BookId book_id, double pos) = 0;
};

// I'm storing both the "next" and "last" funding rates and times because
// the TradeRiskMan might come to us a little after the funding period happens.
// In that case, I want to apply the last funding rate that was not
// given to the position yet - whether that comes from one or the other.
// TBH, it *should* come from the last_realized_funding_rate, I just want to be
// sure.
struct OneFundingStat {
  Market mkt;

  double immediate_rate;

  // Calculated based on the immediate rate.
  double naive_annualized_rate;
  double compounding_annualized_rate;
  int64_t next_funding_time;

  // Also store the last "definite" paid time and last "definite" funding rate.
  // Do this because:
  // 1: Hyperliquid funding floats a little.
  // 2: We want to make sure we don't give ourselves the same funding twice.
  double last_realized_funding_rate;
  int64_t last_realized_funding_time;
};

class FundingMaster {
 public:
  FundingMaster();

  // PKTraders who want a market should tell me.
  void wants_funding(Market mkt);

  // Call this on daystart. NOT during postsecmaster.
  void start_funding_requests();

  double get_funding(Market mkt, SymbolId sym);

  double get_naive_annualized_funding(Market mkt, SymbolId sym);
  double get_compounding_annualized_funding(Market mkt, SymbolId sym);

  // Stupid, yes, but I'm going to give the
  double hl_naive_annualize(double cur_funding);
  double hl_compounding_annualize(double cur_funding);

  double binance_naive_annualize(double cur_funding);
  double binance_compounding_annualize(double cur_funding);

  int64_t get_next_binance_time() {
    // If it's pretty much the next funding time, then set the next funding time as
    // the the dfinite next time.
    if (std::abs(binance_next_funding_t_ - time_utils::nowToMs()) < 60 * 1000) {
      binance_next_funding_t_ = binance_next_funding_t_ + 1000 * 60 * 60 * 8;
    }

    return binance_next_funding_t_;
  }

  int64_t get_next_hyperliquid_time() {
    if (std::abs(hyperliquid_next_funding_t_ - time_utils::nowToMs()) < 60 * 1000) {
      hyperliquid_next_funding_t_ = hyperliquid_next_funding_t_ + 1000 * 60 * 60 * 1;
    }

    return hyperliquid_next_funding_t_;
  }

  double hyperliquid_calc_funding(std::string symbol, double notional_position);
  double binance_calc_funding(std::string symbol, double notional_position);

  // Note that pos is the actual position. Not the notional. Not the delta. The
  // position.
  void updatePosition(BookId b_id, double pos);

  double getPosition(BookId b_id);

  void addPositionListener(BookId b_id, FundingPosListener* listener);

 private:
  // Putting these here to try to maintain that only the fundingmaster itself starts
  // the request cycle.
  void start_request_hyperliquid_funding();
  void start_request_binance_funding();
  void refresh_hyperliquid_funding();
  void refresh_binance_funding();

  // By default, we don't make these requests because they can take up ip quota (eg. hyperliquid).
  // If we use funding, though, the *pktraders* in their postsecmaster, will tell the
  // funding_master what markets they want.
  bool want_hyperliquid_ = false;
  bool want_binance_ = false;

  // How often to ask binance for funding.

  std::chrono::seconds binance_request_t_;
  std::chrono::seconds hyperliquid_request_t_;

  bool hl_started_ = false;
  bool binance_started_ = false;

  int64_t hyperliquid_last_request_t_;
  int64_t hyperliquid_last_funding_t_;
  int64_t hyperliquid_next_funding_t_;

  int64_t binance_last_request_t_;
  int64_t binance_last_funding_t_;
  int64_t binance_next_funding_t_;

  int64_t last_binance_crossover_t_;
  int64_t last_hyperliquid_crossover_t_;

  std::unordered_map<std::string, OneFundingStat> hyperliquid_funding;
  std::unordered_map<std::string, OneFundingStat> binance_funding;

  std::map<BookId, std::vector<FundingPosListener*>> pos_listeners_;

  // BookId -> position.
  std::unordered_map<BookId, double> symbol_to_pos_;
};

} // namespace pktrade
