#pragma once

#include <type_traits>

namespace pktrade {

template <typename T>
struct always_false : std::false_type {};

template <typename T>
inline constexpr bool always_false_v = always_false<T>::value;

} // namespace pktrade
