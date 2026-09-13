#pragma once

#include <cstddef>
#include <cstdint>
#include <glog/logging.h>
#include <iomanip>
#include <iostream>
#include <limits>
#include <ostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>

#include <boost/integer.hpp>
#include <boost/operators.hpp>

namespace pktrade {

/// Small epsilon used for floating‑point tolerance checks.
/// Value: 1 × 10⁻⁸
constexpr double EPS = 1e-8;

constexpr bool approx_equal(double x, double y) { return std::abs(x - y) < EPS; }

namespace detail {

// This is just 10 ^(num_digits)
constexpr uint64_t scale(size_t num_digits) { return num_digits ? scale(num_digits - 1) * 10 : 1; }

std::string_view remove_trailing_zeroes(std::string_view str);

std::string_view truncate_fractional_digits(std::string_view str, int num_fractional_digits);
} // namespace detail

struct strip_trailing_zeroes_t {
  explicit strip_trailing_zeroes_t() = default;
};

inline constexpr strip_trailing_zeroes_t strip_trailing_zeroes = strip_trailing_zeroes_t();

struct truncate_fractional_digits_t {
  explicit truncate_fractional_digits_t() = default;
};

inline constexpr truncate_fractional_digits_t truncate_fractional_digits =
    truncate_fractional_digits_t();

// Fixed point arithmetic type where underlying values are stored in base-10
// representation. The template parameters I and F represent the maximum
// precision in digits of the integral and decimal components, respectively.
//
// To construct a fixed point object from a floating-point value, use the
// Fixed(std::string) constructor. There is no Fixed(double) constructor to
// avoid precision-loss. Constructors will throw std::out_of_range if a value
// that exceeds the maximum precision of the type is passed.
//
// If two fixed point objects have the same decimal precision, their
// sum/difference will be promoted to the highest precision integral value of
// the two operands. Addition and subtraction of objects with different decimal
// precision is not allowed.
template <size_t I, size_t F>
class Fixed : boost::additive<Fixed<I, F>>,
              boost::multipliable<Fixed<I, F>, int>,
              boost::totally_ordered<Fixed<I, F>> {
  // static_assert(I + F < 19, "native type for Fixed<I, F> is 64-bits at
  // most");

 public:
  static constexpr size_t fractional_digits = F;
  static constexpr size_t integer_digits = I;
  static constexpr size_t total_digits = I + F;

  static constexpr int64_t scaling_factor = detail::scale(fractional_digits);

  // if we support total_digit number of digits, the largest number we can
  // represent is scale(total_digits) - 1.
  static constexpr int64_t max_value = detail::scale(total_digits) - 1LL;

  // smallest signed integer type that can represent max_value
  using base_type = typename boost::int_max_value_t<max_value>::least;

  // TODO: check that this actually works properly.
  static Fixed fromFixed(const Fixed& f) { return Fixed::fromRaw(f.raw()); }

  static Fixed fromRaw(base_type raw) {
    Fixed f;
    f.data_ = raw;
    return f;
  };

  static constexpr Fixed max() { return Fixed::fromRaw(std::numeric_limits<base_type>::max()); }

  static constexpr Fixed min() { return Fixed::fromRaw(std::numeric_limits<base_type>::min()); }

  Fixed() = default;

  Fixed(std::string_view n) {
    auto is_negative = false;
    auto it = n.begin();
    if (*it == '-') {
      is_negative = true;
      ++it;
    }

    data_ = 0;
    // Before we hit the decimal point, always -1.
    // After we hit the decimal point, starts counting how many decimals in we
    // are.

    int decimal = -1;

    // Just counts how many integer points we're in.
    // And if we're over the limit for integer_digits, then it tells us there's
    // an error.
    int integer = 0;

    bool needs_max = false;
    bool needs_min = false;

    // Data is getting multiplied by 10 every single round.
    while (it != n.end()) {
      char c = *it++;
      if (c == '.') {
        decimal = 0;
      } else {
        data_ = data_ * 10 + c - '0';
        if (decimal >= 0) {
          if (++decimal > static_cast<int>(fractional_digits)) {
            needs_min = true;
            // Demoted from LOG(WARNING) to VLOG(1): this fires on ordinary float-repr noise
            // (e.g. "3898.0000000001") and the truncation below is the intended behavior, not a
            // bug. At WARNING it was 1.5M lines / 0.31 GB per 10 days across the trade boxes --
            // ~40% of all WARNING volume -- and glog also duplicates it into the INFO log.
            // Re-enable with --v=1 to diagnose a specific truncation.
            VLOG(1) << " Fixed_point bug, scaling/truncating for " << n;

            break;
            // I'm inserting a
            // throw std::out_of_range("fixed_point: loss of decimal precision "
            // +
            //                         std::string(n));
          }
        } else if (data_ > 0) {
          if (++integer > static_cast<int>(integer_digits)) {
            needs_max = true;
            LOG(WARNING) << "Fixed_point bug, scaling to absolute max for " << n;
            break;
            // throw std::out_of_range("fixed_point: loss of integral precision
            // " +
            //                         std::string(n));
          }
        }
      }
    }

    // Scale all of them until we get to the right number of digits.
    // Check that this is true.
    if (needs_max) {
      data_ = max_value;
    } else if (needs_min) {
      // Actually, if we got to this point, we don't really need to do this
      // last division.
      if (data_ == 0) {
        data_ = 1;
      } else {
        data_ /= 10;
      }
    } else {
      if (decimal < 0) {
        data_ *= scaling_factor;
      } else {
        data_ *= detail::scale(fractional_digits - decimal);
      }
    }

    if (is_negative)
      data_ = -data_;
  }

  Fixed(const char* n) : Fixed(std::string_view(n)) {}
  Fixed(const std::string& n) : Fixed(std::string_view(n)) {}

  // This constructor overload strips trailing zeroes from value prior to
  // determining required precision, at a slight performance penalty.
  Fixed(std::string_view n, strip_trailing_zeroes_t) : Fixed(detail::remove_trailing_zeroes(n)) {}

  // This constructor overload truncates a string's fractional part prior
  // to determining required percision, at a slight performance penalty.
  Fixed(std::string_view n, truncate_fractional_digits_t)
      : Fixed(detail::truncate_fractional_digits(n, fractional_digits)) {}

  Fixed(std::integral auto n) : data_(n * scaling_factor) {
    if (n * scaling_factor > max_value) {
      throw std::out_of_range("loss of precision " + std::to_string(n));
    }
  }

  bool operator==(const Fixed& o) const { return data_ == o.data_; }
  bool operator<(const Fixed& o) const { return data_ < o.data_; }

  Fixed operator-() const {
    Fixed t(*this);
    t.data_ = -t.data_;
    return t;
  }

  Fixed operator+() const { return *this; }

  Fixed& operator+=(const Fixed& n) {
    data_ += n.data_;
    return *this;
  }

  Fixed& operator-=(const Fixed& n) {
    data_ -= n.data_;
    return *this;
  }

  Fixed& operator*=(int n) {
    data_ *= n;
    return *this;
  }

  // Basic conversion functions

  int64_t toInteger() const { return data_ / scaling_factor; }

  double toDouble() const { return static_cast<double>(data_) / scaling_factor; }

  base_type raw() const { return data_; }

 private:
  base_type data_;
};

// if we have the same fractional portion, but differing integer portions, we
// trivially upgrade the smaller type
template <size_t I1, size_t I2, size_t F>
auto operator+(const Fixed<I1, F>& lhs, const Fixed<I2, F>& rhs) {
  using T = typename std::conditional_t<I1 >= I2, Fixed<I1, F>, Fixed<I2, F>>;

  T l = T::fromRaw(lhs.raw());
  T r = T::fromRaw(rhs.raw());
  return l + r;
}

template <size_t I1, size_t I2, size_t F>
auto operator-(const Fixed<I1, F>& lhs, const Fixed<I2, F>& rhs) {
  using T = typename std::conditional_t<I1 >= I2, Fixed<I1, F>, Fixed<I2, F>>;

  T l = T::fromRaw(lhs.raw());
  T r = T::fromRaw(rhs.raw());
  return l - r;
}

template <size_t I, size_t F>
std::ostream& operator<<(std::ostream& os, const Fixed<I, F>& f) {
  auto default_precision = os.precision();
  auto default_flags = os.flags();
  os << std::fixed << std::setprecision(F) << f.toDouble() << std::setprecision(default_precision);
  os.flags(default_flags);
  return os;
}
} // namespace pktrade
