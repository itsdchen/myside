#include "symbolizer.h"

#include "pktrade/mdapi.h"
#include "pktrade/types.h"

#include <cmath>
#include <mutex>
#include <rapidjson/document.h>
#include <fmt/format.h>

#include "time_utils.h"

namespace pktrade::symbolizer {

////////////////////////////////////////////////////////////////////////////////
// Databento symbol resolution (ported from symbolizer.py)

// Build reverse map from ours_db_map
static std::map<std::string, std::string> build_reverse_db_map() {
  std::map<std::string, std::string> m;
  for (const auto& [k, v] : ours_db_map) {
    m[v] = k;
  }
  return m;
}

// Mutable reverse map - gets extended at runtime with dated contract mappings.
// Protected by reverse_db_mtx since record callbacks read it from databento threads.
static std::mutex reverse_db_mtx;
static std::map<std::string, std::string> reverse_db_map = build_reverse_db_map();

std::string databento_to_ours(const std::string& databento_sym) {
  std::lock_guard<std::mutex> lock(reverse_db_mtx);
  auto it = reverse_db_map.find(databento_sym);
  if (it == reverse_db_map.end()) {
    throw std::runtime_error(
        fmt::format("databento_to_ours: {} not in reverse_db_map", databento_sym));
  }
  return it->second;
}

std::string ours_to_databento(const std::string& symbol) {
  auto it = ours_db_map.find(symbol);
  if (it == ours_db_map.end()) {
    throw std::runtime_error(
        fmt::format("ours_to_databento: {} not in ours_db_map", symbol));
  }
  return it->second;
}

void register_reverse_mapping(const std::string& db_sym, const std::string& our_sym) {
  std::lock_guard<std::mutex> lock(reverse_db_mtx);
  reverse_db_map[db_sym] = our_sym;
}

// Dated contract map - ported from symbolizer.py
// Maps symbol -> sorted list of (date_int, contract_name)
static const std::map<std::string, std::map<int, std::string>> dated_db_map = {
    {"BZ", {
        // Brent trades one contract month AHEAD of CL/NG per HL convention.
        {20260115, "BZJ6"}, {20260213, "BZK6"}, {20260313, "BZM6"}, {20260415, "BZN6"},
        {20260514, "BZQ6"}, {20260612, "BZU6"}, {20260715, "BZV6"}, {20260814, "BZX6"},
        {20260915, "BZZ6"}, {20261014, "BZF7"}, {20261113, "BZG7"}, {20261214, "BZH7"},
        {20270115, "BZJ7"}, {20270212, "BZK7"}, {20270312, "BZM7"}, {20270414, "BZN7"},
        {20270514, "BZQ7"}, {20270614, "BZU7"}, {20270715, "BZV7"}, {20270813, "BZX7"},
        {20270915, "BZZ7"}, {20271014, "BZF8"}, {20271112, "BZG8"}, {20271214, "BZH8"},
    }},
    {"ES", {
        // Monday before the third Friday (expiry week) roll into the next H/M/U/Z contract.
        {20250701, "ESU5"}, {20250915, "ESZ5"}, {20251215, "ESH6"}, {20260316, "ESM6"},
        {20260615, "ESU6"}, {20260914, "ESZ6"}, {20261214, "ESH7"}, {20270315, "ESM7"},
        {20270614, "ESU7"}, {20270913, "ESZ7"}, {20271213, "ESH8"},
    }},
    {"NQ", {
        // Same equity index rule as ES.
        {20250616, "NQU5"}, {20250915, "NQZ5"}, {20251215, "NQH6"}, {20260316, "NQM6"},
        {20260615, "NQU6"}, {20260914, "NQZ6"}, {20261214, "NQH7"}, {20270315, "NQM7"},
        {20270614, "NQU7"}, {20270913, "NQZ7"}, {20271213, "NQH8"},
    }},
    {"CL", {
        // Monthly CL roll: BD6-BD9 blend, switch after BD9 of the roll month.
        {20251215, "CLG6"}, {20260115, "CLH6"}, {20260213, "CLJ6"}, {20260313, "CLK6"},
        {20260415, "CLM6"}, {20260514, "CLN6"}, {20260612, "CLQ6"}, {20260715, "CLU6"},
        {20260814, "CLV6"}, {20260915, "CLX6"}, {20261014, "CLZ6"}, {20261113, "CLF7"},
        {20261214, "CLG7"},
    }},
    {"HG", {
        // Third-last business day termination (CME spec) with switch just before.
        {20260111, "HGH6"}, {20260213, "HGK6"}, {20260415, "HGN6"}, {20260612, "HGU6"},
        {20260814, "HGZ6"}, {20261113, "HGH7"},
    }},
    {"GC", {
        // 5 business days before the 3rd-last business day of the contract month.
        {20251118, "GCG6"}, {20260121, "GCJ6"}, {20260320, "GCM6"}, {20260520, "GCQ6"},
        {20260722, "GCZ6"}, {20261119, "GCG7"}, {20270120, "GCJ7"}, {20270319, "GCM7"},
        {20270519, "GCQ7"}, {20270721, "GCZ7"}, {20271118, "GCG8"},
    }},
    {"SI", {
        // 6 business days before the 3rd-last business day of the delivery month.
        {20251114, "SIH6"}, {20260217, "SIK6"}, {20260420, "SIN6"}, {20260617, "SIU6"},
        {20260819, "SIZ6"}, {20261118, "SIH7"}, {20270216, "SIK7"}, {20270420, "SIN7"},
        {20270617, "SIU7"}, {20270819, "SIZ7"}, {20271117, "SIH8"},
    }},
    {"PL", {
        // NYMEX Platinum quarterly J/N/V/F (Apr/Jul/Oct/Jan). Roll ~1 week before FND.
        {20250923, "PLF6"}, {20251223, "PLJ6"}, {20260326, "PLN6"}, {20260624, "PLV6"},
        {20260924, "PLF7"}, {20261222, "PLJ7"},
    }},
    {"PA", {
        // NYMEX Palladium quarterly H/M/U/Z (Mar/Jun/Sep/Dec). Same FND-driven roll.
        {20251124, "PAH6"}, {20260224, "PAM6"}, {20260525, "PAU6"}, {20260824, "PAZ6"},
        {20261123, "PAH7"},
    }},
    {"NIY", {
        // CME Nikkei 225 Yen futures — quarterly H/M/U/Z, same roll timing as ES/NQ.
        {20250616, "NIYU5"}, {20250915, "NIYZ5"}, {20251215, "NIYH6"}, {20260316, "NIYM6"},
        {20260615, "NIYU6"}, {20260914, "NIYZ6"}, {20261214, "NIYH7"}, {20270315, "NIYM7"},
        {20270614, "NIYU7"}, {20270913, "NIYZ7"}, {20271213, "NIYH8"},
    }},
    // CME FX futures roll into the next quarterly contract on the Monday
    // preceding the third Wednesday of the contract month.
    {"6E", {
        {20251215, "6EH6"}, {20260316, "6EM6"}, {20260615, "6EU6"}, {20260914, "6EZ6"},
        {20261214, "6EH7"}, {20270315, "6EM7"}, {20270614, "6EU7"}, {20270913, "6EZ7"},
        {20271213, "6EH8"},
    }},
    {"6J", {
        {20251215, "6JH6"}, {20260316, "6JM6"}, {20260615, "6JU6"}, {20260914, "6JZ6"},
        {20261214, "6JH7"}, {20270315, "6JM7"}, {20270614, "6JU7"}, {20270913, "6JZ7"},
        {20271213, "6JH8"},
    }},
    {"6B", {
        {20251215, "6BH6"}, {20260316, "6BM6"}, {20260615, "6BU6"}, {20260914, "6BZ6"},
        {20261214, "6BH7"}, {20270315, "6BM7"}, {20270614, "6BU7"}, {20270913, "6BZ7"},
        {20271213, "6BH8"},
    }},
    {"NG", {
        // Monthly NatGas roll with the same BD6-BD9 blend schedule as CL.
        {20260115, "NGH26"}, {20260213, "NGJ26"}, {20260313, "NGK26"}, {20260415, "NGM26"},
        {20260514, "NGN26"}, {20260612, "NGQ26"}, {20260715, "NGU26"}, {20260814, "NGV26"},
        {20260915, "NGX26"}, {20261014, "NGZ26"}, {20261113, "NGF27"}, {20261214, "NGG27"},
    }},
};

std::string ours_to_databento_dated(const std::string& symbol, int date_int) {
  auto it = dated_db_map.find(symbol);
  if (it == dated_db_map.end()) {
    throw std::runtime_error(
        fmt::format("ours_to_databento_dated: {} not in dated_db_map", symbol));
  }
  const auto& date_map = it->second;
  std::string result;
  for (const auto& [roll_date, contract] : date_map) {
    if (date_int >= roll_date) {
      result = contract;
    }
  }
  if (result.empty()) {
    throw std::runtime_error(
        fmt::format("ours_to_databento_dated: no contract for {} on {}", symbol, date_int));
  }
  // Register in reverse map so databento_to_ours works for this contract
  {
    std::lock_guard<std::mutex> lock(reverse_db_mtx);
    reverse_db_map[result] = symbol;
  }
  return result;
}

// Roll blend schedule - ported from symbolizer.py
static const std::map<std::string, std::map<int, RollBlendEntry>> roll_blend_schedule = {
    {"HG", {
        {20260209, {"HGH6", "HGK6", 0.8}}, {20260210, {"HGH6", "HGK6", 0.6}},
        {20260211, {"HGH6", "HGK6", 0.4}}, {20260212, {"HGH6", "HGK6", 0.2}},
        {20260409, {"HGK6", "HGN6", 0.8}}, {20260410, {"HGK6", "HGN6", 0.6}},
        {20260411, {"HGK6", "HGN6", 0.6}}, {20260412, {"HGK6", "HGN6", 0.6}},
        {20260413, {"HGK6", "HGN6", 0.4}}, {20260414, {"HGK6", "HGN6", 0.2}},
        {20260608, {"HGN6", "HGU6", 0.8}}, {20260609, {"HGN6", "HGU6", 0.6}},
        {20260610, {"HGN6", "HGU6", 0.4}}, {20260611, {"HGN6", "HGU6", 0.2}},
        {20260810, {"HGU6", "HGZ6", 0.8}}, {20260811, {"HGU6", "HGZ6", 0.6}},
        {20260812, {"HGU6", "HGZ6", 0.4}}, {20260813, {"HGU6", "HGZ6", 0.2}},
        {20261109, {"HGZ6", "HGH7", 0.8}}, {20261110, {"HGZ6", "HGH7", 0.6}},
        {20261111, {"HGZ6", "HGH7", 0.4}}, {20261112, {"HGZ6", "HGH7", 0.2}},
    }},
    {"CL", {
        {20260109, {"CLG6", "CLH6", 0.8}}, {20260110, {"CLG6", "CLH6", 0.8}},
        {20260111, {"CLG6", "CLH6", 0.8}}, {20260112, {"CLG6", "CLH6", 0.6}},
        {20260113, {"CLG6", "CLH6", 0.4}}, {20260114, {"CLG6", "CLH6", 0.2}},
        {20260209, {"CLH6", "CLJ6", 0.8}}, {20260210, {"CLH6", "CLJ6", 0.6}},
        {20260211, {"CLH6", "CLJ6", 0.4}}, {20260212, {"CLH6", "CLJ6", 0.2}},
        {20260309, {"CLJ6", "CLK6", 0.8}}, {20260310, {"CLJ6", "CLK6", 0.6}},
        {20260311, {"CLJ6", "CLK6", 0.4}}, {20260312, {"CLJ6", "CLK6", 0.2}},
        {20260409, {"CLK6", "CLM6", 0.8}}, {20260410, {"CLK6", "CLM6", 0.6}},
        {20260411, {"CLK6", "CLM6", 0.6}}, {20260412, {"CLK6", "CLM6", 0.6}},
        {20260413, {"CLK6", "CLM6", 0.4}}, {20260414, {"CLK6", "CLM6", 0.2}},
        {20260508, {"CLM6", "CLN6", 0.8}}, {20260509, {"CLM6", "CLN6", 0.8}},
        {20260510, {"CLM6", "CLN6", 0.8}}, {20260511, {"CLM6", "CLN6", 0.6}},
        {20260512, {"CLM6", "CLN6", 0.4}}, {20260513, {"CLM6", "CLN6", 0.2}},
        {20260608, {"CLN6", "CLQ6", 0.8}}, {20260609, {"CLN6", "CLQ6", 0.6}},
        {20260610, {"CLN6", "CLQ6", 0.4}}, {20260611, {"CLN6", "CLQ6", 0.2}},
        {20260709, {"CLQ6", "CLU6", 0.8}}, {20260710, {"CLQ6", "CLU6", 0.6}},
        {20260711, {"CLQ6", "CLU6", 0.6}}, {20260712, {"CLQ6", "CLU6", 0.6}},
        {20260713, {"CLQ6", "CLU6", 0.4}}, {20260714, {"CLQ6", "CLU6", 0.2}},
        {20260810, {"CLU6", "CLV6", 0.8}}, {20260811, {"CLU6", "CLV6", 0.6}},
        {20260812, {"CLU6", "CLV6", 0.4}}, {20260813, {"CLU6", "CLV6", 0.2}},
        {20260909, {"CLV6", "CLX6", 0.8}}, {20260910, {"CLV6", "CLX6", 0.6}},
        {20260911, {"CLV6", "CLX6", 0.4}}, {20260912, {"CLV6", "CLX6", 0.4}},
        {20260913, {"CLV6", "CLX6", 0.4}}, {20260914, {"CLV6", "CLX6", 0.2}},
        {20261008, {"CLX6", "CLZ6", 0.8}}, {20261009, {"CLX6", "CLZ6", 0.6}},
        {20261010, {"CLX6", "CLZ6", 0.6}}, {20261011, {"CLX6", "CLZ6", 0.6}},
        {20261012, {"CLX6", "CLZ6", 0.4}}, {20261013, {"CLX6", "CLZ6", 0.2}},
        {20261109, {"CLZ6", "CLF7", 0.8}}, {20261110, {"CLZ6", "CLF7", 0.6}},
        {20261111, {"CLZ6", "CLF7", 0.4}}, {20261112, {"CLZ6", "CLF7", 0.2}},
        {20261208, {"CLF7", "CLG7", 0.8}}, {20261209, {"CLF7", "CLG7", 0.6}},
        {20261210, {"CLF7", "CLG7", 0.4}}, {20261211, {"CLF7", "CLG7", 0.2}},
        {20261212, {"CLF7", "CLG7", 0.2}}, {20261213, {"CLF7", "CLG7", 0.2}},
    }},
    {"NG", {
        {20260109, {"NGG26", "NGH26", 0.8}}, {20260110, {"NGG26", "NGH26", 0.8}},
        {20260111, {"NGG26", "NGH26", 0.8}}, {20260112, {"NGG26", "NGH26", 0.6}},
        {20260113, {"NGG26", "NGH26", 0.4}}, {20260114, {"NGG26", "NGH26", 0.2}},
        {20260209, {"NGH26", "NGJ26", 0.8}}, {20260210, {"NGH26", "NGJ26", 0.6}},
        {20260211, {"NGH26", "NGJ26", 0.4}}, {20260212, {"NGH26", "NGJ26", 0.2}},
        {20260309, {"NGJ26", "NGK26", 0.8}}, {20260310, {"NGJ26", "NGK26", 0.6}},
        {20260311, {"NGJ26", "NGK26", 0.4}}, {20260312, {"NGJ26", "NGK26", 0.2}},
        {20260409, {"NGK26", "NGM26", 0.8}}, {20260410, {"NGK26", "NGM26", 0.6}},
        {20260411, {"NGK26", "NGM26", 0.6}}, {20260412, {"NGK26", "NGM26", 0.6}},
        {20260413, {"NGK26", "NGM26", 0.4}}, {20260414, {"NGK26", "NGM26", 0.2}},
        {20260508, {"NGM26", "NGN26", 0.8}}, {20260509, {"NGM26", "NGN26", 0.8}},
        {20260510, {"NGM26", "NGN26", 0.8}}, {20260511, {"NGM26", "NGN26", 0.6}},
        {20260512, {"NGM26", "NGN26", 0.4}}, {20260513, {"NGM26", "NGN26", 0.2}},
        {20260608, {"NGN26", "NGQ26", 0.8}}, {20260609, {"NGN26", "NGQ26", 0.6}},
        {20260610, {"NGN26", "NGQ26", 0.4}}, {20260611, {"NGN26", "NGQ26", 0.2}},
        {20260709, {"NGQ26", "NGU26", 0.8}}, {20260710, {"NGQ26", "NGU26", 0.6}},
        {20260711, {"NGQ26", "NGU26", 0.6}}, {20260712, {"NGQ26", "NGU26", 0.6}},
        {20260713, {"NGQ26", "NGU26", 0.4}}, {20260714, {"NGQ26", "NGU26", 0.2}},
        {20260810, {"NGU26", "NGV26", 0.8}}, {20260811, {"NGU26", "NGV26", 0.6}},
        {20260812, {"NGU26", "NGV26", 0.4}}, {20260813, {"NGU26", "NGV26", 0.2}},
        {20260909, {"NGV26", "NGX26", 0.8}}, {20260910, {"NGV26", "NGX26", 0.6}},
        {20260911, {"NGV26", "NGX26", 0.4}}, {20260912, {"NGV26", "NGX26", 0.4}},
        {20260913, {"NGV26", "NGX26", 0.4}}, {20260914, {"NGV26", "NGX26", 0.2}},
        {20261008, {"NGX26", "NGZ26", 0.8}}, {20261009, {"NGX26", "NGZ26", 0.6}},
        {20261010, {"NGX26", "NGZ26", 0.6}}, {20261011, {"NGX26", "NGZ26", 0.6}},
        {20261012, {"NGX26", "NGZ26", 0.4}}, {20261013, {"NGX26", "NGZ26", 0.2}},
        {20261109, {"NGZ26", "NGF27", 0.8}}, {20261110, {"NGZ26", "NGF27", 0.6}},
        {20261111, {"NGZ26", "NGF27", 0.4}}, {20261112, {"NGZ26", "NGF27", 0.2}},
        {20261208, {"NGF27", "NGG27", 0.8}}, {20261209, {"NGF27", "NGG27", 0.6}},
        {20261210, {"NGF27", "NGG27", 0.4}}, {20261211, {"NGF27", "NGG27", 0.2}},
        {20261212, {"NGF27", "NGG27", 0.2}}, {20261213, {"NGF27", "NGG27", 0.2}},
    }},
    // Brent trades one contract month AHEAD of CL/NG per HL convention.
    {"BZ", {
        {20260109, {"BZH6", "BZJ6", 0.8}}, {20260110, {"BZH6", "BZJ6", 0.8}},
        {20260111, {"BZH6", "BZJ6", 0.8}}, {20260112, {"BZH6", "BZJ6", 0.6}},
        {20260113, {"BZH6", "BZJ6", 0.4}}, {20260114, {"BZH6", "BZJ6", 0.2}},
        {20260209, {"BZJ6", "BZK6", 0.8}}, {20260210, {"BZJ6", "BZK6", 0.6}},
        {20260211, {"BZJ6", "BZK6", 0.4}}, {20260212, {"BZJ6", "BZK6", 0.2}},
        {20260309, {"BZK6", "BZM6", 0.8}}, {20260310, {"BZK6", "BZM6", 0.6}},
        {20260311, {"BZK6", "BZM6", 0.4}}, {20260312, {"BZK6", "BZM6", 0.2}},
        {20260409, {"BZM6", "BZN6", 0.8}}, {20260410, {"BZM6", "BZN6", 0.6}},
        {20260411, {"BZM6", "BZN6", 0.6}}, {20260412, {"BZM6", "BZN6", 0.6}},
        {20260413, {"BZM6", "BZN6", 0.4}}, {20260414, {"BZM6", "BZN6", 0.2}},
        {20260508, {"BZN6", "BZQ6", 0.8}}, {20260509, {"BZN6", "BZQ6", 0.8}},
        {20260510, {"BZN6", "BZQ6", 0.8}}, {20260511, {"BZN6", "BZQ6", 0.6}},
        {20260512, {"BZN6", "BZQ6", 0.4}}, {20260513, {"BZN6", "BZQ6", 0.2}},
        {20260608, {"BZQ6", "BZU6", 0.8}}, {20260609, {"BZQ6", "BZU6", 0.6}},
        {20260610, {"BZQ6", "BZU6", 0.4}}, {20260611, {"BZQ6", "BZU6", 0.2}},
        {20260709, {"BZU6", "BZV6", 0.8}}, {20260710, {"BZU6", "BZV6", 0.6}},
        {20260711, {"BZU6", "BZV6", 0.6}}, {20260712, {"BZU6", "BZV6", 0.6}},
        {20260713, {"BZU6", "BZV6", 0.4}}, {20260714, {"BZU6", "BZV6", 0.2}},
        {20260810, {"BZV6", "BZX6", 0.8}}, {20260811, {"BZV6", "BZX6", 0.6}},
        {20260812, {"BZV6", "BZX6", 0.4}}, {20260813, {"BZV6", "BZX6", 0.2}},
        {20260909, {"BZX6", "BZZ6", 0.8}}, {20260910, {"BZX6", "BZZ6", 0.6}},
        {20260911, {"BZX6", "BZZ6", 0.4}}, {20260912, {"BZX6", "BZZ6", 0.4}},
        {20260913, {"BZX6", "BZZ6", 0.4}}, {20260914, {"BZX6", "BZZ6", 0.2}},
        {20261008, {"BZZ6", "BZF7", 0.8}}, {20261009, {"BZZ6", "BZF7", 0.6}},
        {20261010, {"BZZ6", "BZF7", 0.6}}, {20261011, {"BZZ6", "BZF7", 0.6}},
        {20261012, {"BZZ6", "BZF7", 0.4}}, {20261013, {"BZZ6", "BZF7", 0.2}},
        {20261109, {"BZF7", "BZG7", 0.8}}, {20261110, {"BZF7", "BZG7", 0.6}},
        {20261111, {"BZF7", "BZG7", 0.4}}, {20261112, {"BZF7", "BZG7", 0.2}},
        {20261208, {"BZG7", "BZH7", 0.8}}, {20261209, {"BZG7", "BZH7", 0.6}},
        {20261210, {"BZG7", "BZH7", 0.4}}, {20261211, {"BZG7", "BZH7", 0.2}},
        {20261212, {"BZG7", "BZH7", 0.2}}, {20261213, {"BZG7", "BZH7", 0.2}},
        {20270111, {"BZH7", "BZJ7", 0.8}}, {20270112, {"BZH7", "BZJ7", 0.6}},
        {20270113, {"BZH7", "BZJ7", 0.4}}, {20270114, {"BZH7", "BZJ7", 0.2}},
        {20270208, {"BZJ7", "BZK7", 0.8}}, {20270209, {"BZJ7", "BZK7", 0.6}},
        {20270210, {"BZJ7", "BZK7", 0.4}}, {20270211, {"BZJ7", "BZK7", 0.2}},
        {20270308, {"BZK7", "BZM7", 0.8}}, {20270309, {"BZK7", "BZM7", 0.6}},
        {20270310, {"BZK7", "BZM7", 0.4}}, {20270311, {"BZK7", "BZM7", 0.2}},
        {20270408, {"BZM7", "BZN7", 0.8}}, {20270409, {"BZM7", "BZN7", 0.6}},
        {20270410, {"BZM7", "BZN7", 0.6}}, {20270411, {"BZM7", "BZN7", 0.6}},
        {20270412, {"BZM7", "BZN7", 0.4}}, {20270413, {"BZM7", "BZN7", 0.2}},
        {20270510, {"BZN7", "BZQ7", 0.8}}, {20270511, {"BZN7", "BZQ7", 0.6}},
        {20270512, {"BZN7", "BZQ7", 0.4}}, {20270513, {"BZN7", "BZQ7", 0.2}},
        {20270608, {"BZQ7", "BZU7", 0.8}}, {20270609, {"BZQ7", "BZU7", 0.6}},
        {20270610, {"BZQ7", "BZU7", 0.4}}, {20270611, {"BZQ7", "BZU7", 0.2}},
        {20270612, {"BZQ7", "BZU7", 0.2}}, {20270613, {"BZQ7", "BZU7", 0.2}},
        {20270709, {"BZU7", "BZV7", 0.8}}, {20270710, {"BZU7", "BZV7", 0.8}},
        {20270711, {"BZU7", "BZV7", 0.8}}, {20270712, {"BZU7", "BZV7", 0.6}},
        {20270713, {"BZU7", "BZV7", 0.4}}, {20270714, {"BZU7", "BZV7", 0.2}},
        {20270809, {"BZV7", "BZX7", 0.8}}, {20270810, {"BZV7", "BZX7", 0.6}},
        {20270811, {"BZV7", "BZX7", 0.4}}, {20270812, {"BZV7", "BZX7", 0.2}},
        {20270909, {"BZX7", "BZZ7", 0.8}}, {20270910, {"BZX7", "BZZ7", 0.6}},
        {20270911, {"BZX7", "BZZ7", 0.6}}, {20270912, {"BZX7", "BZZ7", 0.6}},
        {20270913, {"BZX7", "BZZ7", 0.4}}, {20270914, {"BZX7", "BZZ7", 0.2}},
        {20271008, {"BZZ7", "BZF8", 0.8}}, {20271009, {"BZZ7", "BZF8", 0.8}},
        {20271010, {"BZZ7", "BZF8", 0.8}}, {20271011, {"BZZ7", "BZF8", 0.6}},
        {20271012, {"BZZ7", "BZF8", 0.4}}, {20271013, {"BZZ7", "BZF8", 0.2}},
        {20271108, {"BZF8", "BZG8", 0.8}}, {20271109, {"BZF8", "BZG8", 0.6}},
        {20271110, {"BZF8", "BZG8", 0.4}}, {20271111, {"BZF8", "BZG8", 0.2}},
        {20271208, {"BZG8", "BZH8", 0.8}}, {20271209, {"BZG8", "BZH8", 0.6}},
        {20271210, {"BZG8", "BZH8", 0.4}}, {20271211, {"BZG8", "BZH8", 0.4}},
        {20271212, {"BZG8", "BZH8", 0.4}}, {20271213, {"BZG8", "BZH8", 0.2}},
    }},
};

std::vector<std::pair<std::string, double>> get_all_contracts_for_date(
    const std::string& symbol, int date_int) {
  // Check if we're in a roll blend period
  auto sched_it = roll_blend_schedule.find(symbol);
  if (sched_it != roll_blend_schedule.end()) {
    auto date_it = sched_it->second.find(date_int);
    if (date_it != sched_it->second.end()) {
      const auto& entry = date_it->second;
      double next_weight = std::round((1.0 - entry.front_weight) * 100.0) / 100.0;
      // Register both contracts in reverse map
      {
        std::lock_guard<std::mutex> lock(reverse_db_mtx);
        reverse_db_map[entry.front_contract] = symbol;
        reverse_db_map[entry.next_contract] = symbol;
      }
      return {{entry.front_contract, entry.front_weight},
              {entry.next_contract, next_weight}};
    }
  }
  // Not in roll period — use dated map for single contract
  std::string contract = ours_to_databento_dated(symbol, date_int);
  return {{contract, 1.0}};
}

// If we don't have an expiry map ,then it's probably not tied to a "future"
bool is_future(std::string symbol) { return sym_to_expiry_.find(symbol) != sym_to_expiry_.end(); }

// Given a date,
// We do the lookup re: what contract we're at and when
// that contract expires.
int64_t get_expiry_t_epoch(std::string symbol, int64_t start_t) {

  // Just for checking times.
  int counter = 0;

  auto it = sym_to_expiry_.find(symbol);
  if (it != sym_to_expiry_.end()) {
    const auto& expiry_map = it->second;

    int64_t right_roll_t = 0;
    int64_t right_expire_t = 0;

    for (const auto& [roll_dt, expiry_dt] : expiry_map) {

      // Translate that time to epoch times.
      int64_t roll_t = time_utils::strToMs(roll_dt);
      int64_t expire_t = time_utils::strToMs(expiry_dt);

      if (start_t >= roll_t && start_t < expire_t) {
        // Then we got it.
        right_roll_t = roll_t;
        right_expire_t = expire_t;
        //std::cout << fmt::format("Found potential expiry for  {} at {}", symbol, expire_t) << std::endl;

        //return expire_t;
      }
      counter++;
    }
    if (right_roll_t == 0) {
      throw std::runtime_error(fmt::format("Could not find expiry for {}", symbol));
    }
    return right_expire_t;
  } else {
    throw std::runtime_error("Symbol " + symbol + " not found in symbolizer.");
  }

  throw std::runtime_error(
      fmt::format("Could not find appropriate roll for {} on {}", symbol, start_t).c_str());

  return 0;
}

} // namespace pktrade::symbolizer
