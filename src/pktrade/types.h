#pragma once

#include <cstdint>

#include <string>
#include <tuple> // Required to build, as NamedType/named_type is missing this header

#include "NamedType/named_type.hpp"

#include "pktrade/fixed_point.h"

namespace pktrade {

// NB: If T is trivially constructible, fluent::NamedType<T>{} does not
// zero-initialize the underlying data member the way that T{} would. Namely,
// Price{} leaves the data uninitilized, even though Fixed<10,8>{} results in
// proper zero-initialization. To properly zero-initialize, need to actually
// supply the value: Price{0}.
//
// See: https://github.com/joboccara/NamedType/issues/79

// using Quantity =
//     fluent::NamedType<Fixed<10, 8>, struct QuantityTag, fluent::Arithmetic>;

using Quantity = Fixed<10, 8>;
using Price = Fixed<10, 8>;

/*
using Price = fluent::NamedType<Fixed<10, 8>,
                                struct PriceTag,
                                fluent::Comparable,
                                fluent::Printable,
                                fluent::Hashable>;
*/

using SymbolId = fluent::NamedType<std::string, struct SymbolIdTag, fluent::Printable,
                                   fluent::Comparable, fluent::Hashable>;

} // namespace pktrade
