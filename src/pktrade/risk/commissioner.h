#pragma once

#include "pktrade/enums/basic_enums.h"
#include "pktrade/mdapi.h"
#include "pktrade/types.h"

#include <map>

namespace pktrade::risk {

// Eventually: this might be dated.
// For now, keep this fixed.
struct Commission {
  double maker_fee_ = 0.;
  double taker_fee_ = 0.;

  double promo_mult_ = 1.0;
  double fixed_cost_ = 0.0;
  BaseCurrency fixed_cost_currency_ = BaseCurrency::UNSET;

 public:
  Commission() = default;
  Commission(double maker_fee, double taker_fee, double promo_mult)
      : maker_fee_(maker_fee), taker_fee_(taker_fee), promo_mult_(promo_mult) {};
};

class Commissioner {
  // Some static values.
  // I guess at some point, it should be dated or something.

  // Overloading price meaning.
 public:
  // Just initialize the thing...
  Commissioner();

  // promo_tier is for stuff like "gold", "silver",e tc.
  void setTier(Market mkt, std::string tier, bool use_promo, std::string promo_tier = "");

  // If we believe we're within "normal" ranges, then
  // returning a double is... ok.
  double getCommission(Price price, Quantity qty, SymbolId symbol_id, Market mkt, bool add_liq);

  // This is the reference.
  static std::map<Market, std::map<std::string, Commission>> mkt_to_fees_;
  static void initCommissions();

  // This is what we actually do.
  std::map<Market, Commission> used_mkt_to_fees_;

 protected:
  bool use_promo_ = false;

  // Ergh. Extra promo tier due to setting growth mode on.
  // xyz is going to have this on, let's just check later on for whether we want this for others
  // too.
  static bool growth_mode_;
};

} // namespace pktrade::risk
