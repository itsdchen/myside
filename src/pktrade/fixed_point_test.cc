#include "pktrade/fixed_point.h"

#include <stdexcept>

#include <gtest/gtest.h>

namespace pktrade {
namespace {

TEST(FixedPointTest, InitializeInteger) {
  // 9999 is the largest value Fixed<4, 0> can represent
  Fixed<4, 0> fixed(9999);

  ASSERT_EQ(fixed.toInteger(), 9999);
  ASSERT_DOUBLE_EQ(fixed.toDouble(), 9999.0);
}

TEST(FixedPointTest, InitializeNegativeInteger) {
  // -9999 is the smallest value Fixed<4, 0> can represent
  Fixed<4, 0> fixed(-9999);

  ASSERT_EQ(fixed.toInteger(), -9999);
  ASSERT_DOUBLE_EQ(fixed.toDouble(), -9999.0);
}

TEST(FixedPointTest, InitializeIntegerOutOfRange) {
  // Need a type alias since the ASSERT_THROW macro cannot properly parse a
  // templated constructor
  using FixedType = Fixed<3, 1>;

  // 3 digits is not enough to represent 1000
  ASSERT_THROW(FixedType(1000), std::out_of_range);
}

TEST(FixedPointTest, InitializeMaxDouble) {
  // 9999.9 is the largest value Fixed<4, 1> can represent
  Fixed<4, 1> fixed("9999.9");

  ASSERT_EQ(fixed.toInteger(), 9999);
  ASSERT_DOUBLE_EQ(fixed.toDouble(), 9999.9);
}

TEST(FixedPointTest, InitializeMinDouble) {
  // -9999.9 is the smallest value Fixed<4, 1> can represent
  Fixed<4, 1> fixed("-9999.9");

  ASSERT_EQ(fixed.toInteger(), -9999);
  ASSERT_DOUBLE_EQ(fixed.toDouble(), -9999.9);
}

TEST(FixedPointTest, InitializeDoubleIntegerOutOfRange) {
  using FixedType = Fixed<2, 1>;

  // There is not enough precision to represent 199
  ASSERT_THROW(FixedType("199.0"), std::out_of_range);
}

TEST(FixedPointTest, InitializeDoubleDecimalOutOfRange) {
  using FixedType = Fixed<1, 2>;

  // There is not enough precision to represent 0.001
  ASSERT_THROW(FixedType("9.001"), std::out_of_range);
}

TEST(FixedPointTest, InitializeDoubleLeadingZeroes) {
  Fixed<1, 1> fixed("0009.9");

  ASSERT_EQ(fixed.toInteger(), 9);
  ASSERT_DOUBLE_EQ(fixed.toDouble(), 9.9);
}

TEST(FixedPointTest, Addition) {
  Fixed<2, 4> f1(12);
  Fixed<2, 4> f2("41.001");

  auto sum = f1 + f2;
  Fixed<2, 4> expected("53.001");

  ASSERT_EQ(expected, sum);
  ASSERT_EQ(sum.toInteger(), 53);
  ASSERT_DOUBLE_EQ(sum.toDouble(), 53.001);
}

TEST(FixedPointTest, AdditionPromote) {
  Fixed<5, 4> f1(10000);
  Fixed<2, 4> f2("41.001");

  auto sum = f1 + f2;
  Fixed<5, 4> expected("10041.001");

  // f2 should be promoted to Fixed<5, 4>
  ASSERT_EQ(expected, sum);
  ASSERT_EQ(sum.toInteger(), 10041);
  ASSERT_DOUBLE_EQ(sum.toDouble(), 10041.001);
}

TEST(FixedPointTest, Subtraction) {
  Fixed<2, 4> f1(12);
  Fixed<2, 4> f2("41.001");

  auto difference = f1 - f2;
  Fixed<2, 4> expected("-29.001");

  ASSERT_EQ(expected, difference);
  ASSERT_EQ(difference.toInteger(), -29);
  ASSERT_DOUBLE_EQ(difference.toDouble(), -29.001);
}

TEST(FixedPointTest, SubtractionPromote) {
  Fixed<5, 4> f1(10000);
  Fixed<2, 4> f2("41.001");

  auto difference = f1 - f2;
  Fixed<5, 4> expected("9958.999");

  // f2 should be promoted to Fixed<5, 4>
  ASSERT_EQ(expected, difference);
  ASSERT_EQ(difference.toInteger(), 9958);
  ASSERT_DOUBLE_EQ(difference.toDouble(), 9958.999);
}

TEST(FixedPointTest, Factor) {
  Fixed<5, 4> fixed("100.001");

  auto factor = 10 * fixed * 10;
  Fixed<5, 4> expected("10000.1");

  ASSERT_EQ(expected, factor);
  ASSERT_EQ(factor.toInteger(), 10000);
  ASSERT_DOUBLE_EQ(factor.toDouble(), 10000.1);
}

static_assert(std::is_same_v<Fixed<1, 0>::base_type, int8_t>);
static_assert(std::is_same_v<Fixed<1, 1>::base_type, int8_t>);

static_assert(std::is_same_v<Fixed<3, 2>::base_type, int32_t>);
static_assert(std::is_same_v<Fixed<12, 6>::base_type, int64_t>);

static_assert(std::is_constructible_v<Fixed<3, 2>, const char*>);
static_assert(std::is_constructible_v<Fixed<3, 2>, std::string>);
static_assert(std::is_constructible_v<Fixed<3, 2>, std::string_view>);

} // namespace
} // namespace pktrade
