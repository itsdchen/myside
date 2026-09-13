#include "pktrade/risk/commissioner.h"

#include <stdexcept>

#include <gtest/gtest.h>

namespace pktrade::risk {
namespace {
TEST(CommissionerTest, InitializeCommissioner) {
  Commissioner commissioner;

  // Test that I can initialize tiers.
  commissioner.setTier(Market::BinanceSPOT, "VIP1");
  // These tiers are different.
  commissioner.setTier(Market::BinanceUSDM, "VIP9USDT");
  commissioner.setTier(Market::BinanceUSDM, "VIP5BUSD");
}

// Test some calculations.

TEST(CommissionerTest, TestMake) {
  Commissioner commissioner;

  // Test that I can initialize tiers.
  commissioner.setTier(Market::BinanceSPOT, "VIP1");
  // These tiers are different.
  commissioner.setTier(Market::BinanceUSDM, "VIP9USDT");

  ASSERT_DOUBLE_EQ(
      commissioner.getCommission(Price("100.0"), Quantity("1.0"), Market::BinanceSPOT, true), 0.09);

  ASSERT_DOUBLE_EQ(
      commissioner.getCommission(Price("100.0"), Quantity("1.0"), Market::BinanceUSDM, true), 0);
}

TEST(CommissionerTest, TestTake) {
  Commissioner commissioner;

  // Test that I can initialize tiers.
  commissioner.setTier(Market::BinanceSPOT, "VIP1");
  // These tiers are different.
  commissioner.setTier(Market::BinanceUSDM, "VIP9USDT");

  // Calculate basic commissions...
  ASSERT_DOUBLE_EQ(
      commissioner.getCommission(Price("100.0"), Quantity("1.0"), Market::BinanceSPOT, false), 0.1);

  ASSERT_DOUBLE_EQ(
      commissioner.getCommission(Price("100.0"), Quantity("1.0"), Market::BinanceUSDM, false),
      0.017);
}

TEST(CommissionerTest, TestPromo) {
  Commissioner commissioner;

  // Test that I can initialize tiers.
  commissioner.setTier(Market::BinanceSPOT, "VIP1");
  // These tiers are different.
  commissioner.setTier(Market::BinanceUSDM, "VIP9USDT");

  // Calculate basic commissions...
  ASSERT_DOUBLE_EQ(
      commissioner.getCommission(Price("100.0"), Quantity("1.0"), Market::BinanceSPOT, false, true),
      0.075);

  ASSERT_DOUBLE_EQ(
      commissioner.getCommission(Price("100.0"), Quantity("1.0"), Market::BinanceUSDM, false, true),
      0.0153);
}

} // namespace

} // namespace pktrade::risk
