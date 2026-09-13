#include "commissioner.h"

#include <fmt/format.h>
#include <magic_enum.hpp>

namespace pktrade::risk {

// STATIC things.
std::map<Market, std::map<std::string, Commission>> Commissioner::mkt_to_fees_ = {};

// https://www.binance.com/en/fee/schedule

bool Commissioner::growth_mode_ = true;

void Commissioner::initCommissions() {
  // Binance Spot commissions.
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["Regular"] = Commission(0.001, 0.001, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP1"] = Commission(0.0009, 0.001, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP2"] = Commission(0.0008, 0.001, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP3"] = Commission(0.0007, 0.001, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP4"] = Commission(0.0002, 0.0004, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP5"] = Commission(0.0002, 0.0004, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP6"] = Commission(0.0002, 0.0004, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP7"] = Commission(0.0002, 0.0004, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP8"] = Commission(0.0002, 0.0004, 0.75);
  Commissioner::mkt_to_fees_[Market::BinanceSPOT]["VIP9"] = Commission(0.0002, 0.0004, 0.75);

  // Binance USDT-M commissions
  // https://www.binance.com/en/fee/futureFee
  // For payment in USDT
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["RegularUSDT"] =
      Commission(0.0002, 0.0004, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP1USDT"] =
      Commission(0.00016, 0.0004, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP2USDT"] =
      Commission(0.00014, 0.00035, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP3USDT"] =
      Commission(0.00012, 0.00032, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP4USDT"] =
      Commission(0.0001, 0.00030, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP5USDT"] =
      Commission(0.00008, 0.00027, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP6USDT"] =
      Commission(0.00006, 0.00025, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP7USDT"] =
      Commission(0.00004, 0.00022, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP8USDT"] =
      Commission(0.00002, 0.00020, 0.90);
  Commissioner::mkt_to_fees_[Market::BinanceFutures]["VIP9USDT"] =
      Commission(0.0000, 0.00017, 0.90);

  // Skip directly to the best tiers.
  /*
  (v1) ubuntu@ip-172-31-46-228:~/tools$ ./live_cockpit.py comms -m BinanceFutures -sym BTCUSDT
    Running at 20240905 18:18
    Maker: -0.000050
    Taker: 0.000153
    (v1) ubuntu@ip-172-31-46-228:~/tools$ ./live_cockpit.py comms -m BinanceFutures -sym BTCUSDC
    Running at 20240905 18:18
    Maker: -0.000080
    Taker: 0.000153
  */

  Commissioner::mkt_to_fees_[Market::BinanceFutures]["KRONOS"] = Commission(-0.00005, 0.000153, 1);

  // For payment in USDT
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["Regular"] = Commission(0.0001, 0.0005, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP1"] = Commission(0.00008, 0.00045, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP2"] = Commission(0.00005, 0.00040, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP3"] = Commission(0.00003, 0.0003, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP4"] = Commission(0.0000, 0.00025, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP5"] = Commission(-0.00005, 0.00024, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP6"] = Commission(-0.00006, 0.00024, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP7"] = Commission(-0.00007, 0.00024, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP8"] = Commission(-0.00008, 0.00024, 1);
  Commissioner::mkt_to_fees_[Market::BinanceCOINFutures]["VIP9"] = Commission(-0.00009, 0.00024, 1);

  // Let's skip the other markets now. Let's start out here.

  // Hyperliquid
  // https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
  // Updating this.

  // doesn't exist, just stop making this thing crash out.
  // This is if we have no staked.

  // HYPERLIQUID PERPS
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["VIP0"] = Commission(0.00015, 0.00045, 1);
  // 5M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["VIP1"] = Commission(0.00012, 0.0004, 1);
  // 25M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["VIP2"] = Commission(0.00008, 0.00035, 1);
  // 100M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["VIP3"] = Commission(0.00004, 0.00030, 1);
  // 500M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["VIP4"] = Commission(0.00, 0.00028, 1);
  // 2B last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["VIP5"] = Commission(0.00, 0.00026, 1);
  // 7B last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["VIP6"] = Commission(0.00, 0.00024, 1);

  // SPOT
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["SVIP0"] = Commission(0.0004, 0.0007, 1);
  // 5M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["SVIP1"] = Commission(0.0003, 0.0006, 1);
  // 25M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["SVIP2"] = Commission(0.0002, 0.0005, 1);
  // 100M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["SVIP3"] = Commission(0.0001, 0.0004, 1);
  // 500M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["SVIP4"] = Commission(0.00, 0.00035, 1);
  // 2B last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["SVIP5"] = Commission(0.00, 0.0003, 1);
  // 7B last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["SVIP6"] = Commission(0.00, 0.00025, 1);

  // Just for MM program. Check the schedule on their site.
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["MM1"] = Commission(-0.00001, 0.00019, 1);
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["MM2"] = Commission(-0.00002, 0.00019, 1);
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["MM3"] = Commission(-0.00003, 0.00019, 1);

  // HYPERLIQUID PERPS ON HIP3
  // Same tier requirements re: volume. But 2x'd.
  // Little funky but I just want to separate out the growth mode multiplier for the tiers and
  // for just hip3-specific things. And we already have one promo_mult.
  // https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
  // For each user, there is one fee tier across all assets, including perps, HIP-3 perps, and spot.
  // When growth mode is activated for an HIP-3 perp, protocol fees and rebates are reduced by 90%.
  double GROWTH_MULT = 1.0;
  if (growth_mode_) {
    GROWTH_MULT = 0.1;
  }

  Commissioner::mkt_to_fees_[Market::Hyperliquid]["HIP3_0"] =
      Commission(0.00030 * GROWTH_MULT, 0.0009 * GROWTH_MULT, 1);
  // 5M last 14 days
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["HIP3_1"] =
      Commission(0.00024 * GROWTH_MULT, 0.0008 * GROWTH_MULT, 1);
  // 25M
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["HIP3_2"] =
      Commission(0.00016 * GROWTH_MULT, 0.0007 * GROWTH_MULT, 1);
  // 100M
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["HIP3_3"] =
      Commission(0.00008 * GROWTH_MULT, 0.00060 * GROWTH_MULT, 1);
  // 500M
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["HIP3_4"] =
      Commission(0.00, 0.00056 * GROWTH_MULT, 1);
  // 2B
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["HIP3_5"] =
      Commission(0.00, 0.00052 * GROWTH_MULT, 1);
  // 7B
  Commissioner::mkt_to_fees_[Market::Hyperliquid]["HIP3_6"] =
      Commission(0.00, 0.00048 * GROWTH_MULT, 1);
}

// CLASS-SPECIFIC THINGS.
Commissioner::Commissioner() {
  // Just initialize for everyone. Do it.
  Commissioner::initCommissions();
}

void Commissioner::setTier(Market mkt, std::string tier, bool use_promo, std::string promo_tier) {
  // Check if mkt exists.
  if (Commissioner::mkt_to_fees_.find(mkt) == Commissioner::mkt_to_fees_.end()) {
    throw std::runtime_error(fmt::format("Commissioner::setTier : Market {} does not exist.",
                                         magic_enum::enum_name(mkt)));
  }

  // Check if the tier exists.
  if (Commissioner::mkt_to_fees_[mkt].find(tier) == Commissioner::mkt_to_fees_[mkt].end()) {
    throw std::runtime_error(
        fmt::format("Commissioner::setTier : Tier {} does not exist for market {}.", tier,
                    magic_enum::enum_name(mkt)));
  }

  // std::cout << "Commissioner: Setting tier for " <<
  // magic_enum::enum_name(mkt)
  //           << " to " << tier << std::endl;
  used_mkt_to_fees_[mkt] = mkt_to_fees_[mkt][tier];
  use_promo_ = use_promo;

  // Update for hyperliquid promo mults (staking).
  if (promo_tier != "" && mkt == Market::Hyperliquid) {
    double promo_mult = 1;
    if (promo_tier == "Wood") {
      promo_mult = 0.95;
    } else if (promo_tier == "Bronze") {
      promo_mult == 0.9;
    } else if (promo_tier == "Silver") {
      promo_mult = 0.85;
    } else if (promo_tier == "Gold") {
      promo_mult == 0.8;
    } else if (promo_tier == "Platinum") {
      promo_mult = 0.70;
    } else if (promo_tier == "Diamond") {
      promo_mult = 0.60;
    }
    used_mkt_to_fees_[mkt].promo_mult_ = promo_mult;
  }
}

double Commissioner::getCommission(Price price, Quantity qty, SymbolId symbol_id, Market mkt,
                                   bool add_liq) {

  // We cannot use promo if it's gold or mstr

  Commission cmsh = used_mkt_to_fees_[mkt];
  double notional = price.toDouble() * qty.toDouble();

  double net_comm = notional * (add_liq ? cmsh.maker_fee_ : cmsh.taker_fee_);
  if (use_promo_) {
    net_comm *= cmsh.promo_mult_;
  }
  net_comm += cmsh.fixed_cost_;

  // Undo growth mode for undeserving symbols :(
  // https://docs.trade.xyz/trading/fees
  if (growth_mode_ && (symbol_id.get() == "xyz:GOLD" || symbol_id.get() == "xyz:MSTR")) {
    net_comm *= 10;
  }

  return net_comm;
}

} // namespace pktrade::risk
