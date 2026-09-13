#include "pktrade/fixed_point.h"

namespace pktrade {
namespace detail {

std::string_view remove_trailing_zeroes(std::string_view str) {
  auto p = str.find_last_not_of('0');
  if (p != std::string::npos) {
    return str.substr(0, p);
  }
  return str;
}

std::string_view truncate_fractional_digits(std::string_view str, int num_fractional_digits) {
  auto p = str.find('.');
  int total_fractional_digits = str.size() - p;
  if (total_fractional_digits <= num_fractional_digits) {
    return str;
  }
  return str.substr(0, p + num_fractional_digits + 1);
  return str;
}

} // namespace detail
} // namespace pktrade
