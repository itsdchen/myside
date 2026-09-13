#pragma once

#include <map>
#include <string>
#include <tuple>
#include <vector>

namespace pktrade::symbolizer {

bool is_future(std::string symbol);

// Databento symbol maps - ported from symbolizer.py
// Maps our internal symbol -> databento continuous symbol (e.g. "NQ" -> "NQ.v.0")
const std::map<std::string, std::string> ours_db_map = {
    {"ES", "ES.v.0"}, {"NQ", "NQ.v.0"}, {"YM", "YM.v.0"}, {"RTY", "RTY.v.0"},
    {"CL", "CL.v.0"}, {"HG", "HG.v.0"}, {"NG", "NG.v.0"}, {"BZ", "BZ.v.0"},
    {"SI", "SI.v.0"}, {"GC", "GC.v.0"},
    {"PL", "PL.v.0"}, {"PA", "PA.v.0"},
    {"NIY", "NIY.v.0"},
    {"6E", "6E.v.0"}, {"6J", "6J.v.0"}, {"6B", "6B.v.0"}, {"6S", "6S.v.0"}, {"6A", "6A.v.0"}, {"6C", "6C.v.0"},
};

// Reverse map: databento continuous -> ours (built at startup)
std::string databento_to_ours(const std::string& databento_sym);
std::string ours_to_databento(const std::string& symbol);

// Dated contract map: maps (symbol, date_int) -> specific contract (e.g. ("NQ", 20260316) -> "NQM6")
std::string ours_to_databento_dated(const std::string& symbol, int date_int);

// Roll blend schedule: maps (symbol, date_int) -> (front_contract, next_contract, front_weight)
struct RollBlendEntry {
  std::string front_contract;
  std::string next_contract;
  double front_weight;
};

// Returns all contracts needed for a date with their weights.
// Non-roll: [(contract, 1.0)]. During roll: [(front, front_w), (next, next_w)]
std::vector<std::pair<std::string, double>> get_all_contracts_for_date(
    const std::string& symbol, int date_int);

// Register a contract -> base symbol mapping in the reverse map at runtime
void register_reverse_mapping(const std::string& db_sym, const std::string& our_sym);

// Note: the symbol names correspond to the ACTUAL
// traded symbols on the dex, not the names of the reference symbol on
// CME or whatnot.
const std::map<std::string, std::map<std::string, std::string>> sym_to_expiry_ = {
    {"z:ES",
     {
         // First entry is when volume rolls to this
         //                                    Second is when this contract expires.
         {"20250617 00:00:00 America/New_York",
          "20250919 17:00:00 America/New_York"}, // ESU25 starts being most liquid after June
                                                 // expiry
         {"20250916 00:00:00 America/New_York", "20251219 17:00:00 America/New_York"}, // ESZ25
         {"20251216 00:00:00 America/New_York", "20260320 17:00:00 America/New_York"}, // ESH26
         {"20260317 00:00:00 America/New_York", "20260619 17:00:00 America/New_York"}, // ESM26
         {"20260616 00:00:00 America/New_York", "20260918 17:00:00 America/New_York"}, // ESU26
         {"20260915 00:00:00 America/New_York", "20261218 17:00:00 America/New_York"}, // ESZ26
         {"20261215 00:00:00 America/New_York", "20270319 17:00:00 America/New_York"}, // ESH27
     }},
    {"xyz:XYZ100",
     {
         {"20250617 00:00:00 America/New_York", "20250919 17:00:00 America/New_York"}, // NQU25
         {"20250916 00:00:00 America/New_York", "20251219 17:00:00 America/New_York"}, // NQZ25
         {"20251214 18:00:00 America/New_York", "20260320 09:30:00 America/New_York"}, // NQH26
         {"20260313 18:00:00 America/New_York", "20260618 09:30:00 America/New_York"}, // NQM26
         {"20260614 18:00:00 America/New_York", "20260918 09:30:00 America/New_York"}, // NQU26
         {"20260913 18:00:00 America/New_York", "20261218 09:30:00 America/New_York"}, // NQZ26
         {"20261213 18:00:00 America/New_York", "20270319 09:30:00 America/New_York"}, // NQH27
     }},
    // CL is monthly.
    // The actual expiry time doesn't matter, because we plan to not
    // apply a discount rate to it.

    /*
    CL will launch tracking front month / active CL contract - currently CLG6.
    The contract will roll on the weekend between the 6th and 10th business day of
    the calendar month, prior to CME open for the monday session (Sunday evening eastern time).
    Specifically, CL will roll from G6 to H6 at 2026-01-11T21:30:00Z. On close Friday Jan 9 the
    oracle will be publishing CLG6 prices. At open Jan 11 the oracle will be publishing CLH6 prices.
    In the near future, we plan on upgrading this roll logic for the CL perp market to follow the
    methodology of the Bloomberg commodity index. Post upgrade, the "roll period" is the 6th through
    10th business days of the month, during which the weight of the oracle price will shift from the
    front month to the deferred at a rate of 20% per day. For more information see section 3.1 of
    the following document: https://assets.bbhub.io/professional/sites/10/BCOM-Methodology.pdf



    */

    {"xyz:CL",
     {
         {"20251220 18:00:00 America/New_York", "20260121 17:00:00 America/New_York"}, // CLG6
         {"20260111 17:00:00 America/New_York", "20260220 17:00:00 America/New_York"}, // CLH6
         {"20260213 17:00:00 America/New_York", "20260320 17:00:00 America/New_York"}, // CLJ6
         {"20250101 17:00:00 America/New_York", "20280221 17:00:00 America/New_York"}, // backstop

     }},

    {"xyz:COPPER",
     {
        // Make it earlier just so we can dl data for other symbols. 
        // Doesn't have to be real. 
         {"20201231 17:00:00 America/New_York", "20260327 13:00:00 America/New_York"}, // HGH6
         {"20260320 17:00:00 America/New_York", "20260527 13:00:00 America/New_York"}, // HGK6
         {"20250101 17:00:00 America/New_York", "20280221 17:00:00 America/New_York"}, // backstop

     }},


     // Trading terminates on the 3rd last business day of the month prior to the contract month. 
     // last day for h6 is 25th: 
     // https://www.cmegroup.com/markets/energy/natural-gas/natural-gas.calendar.html

    {"xyz:NATGAS",
     {
         {"20131127 17:00:00 America/New_York", "20260225 17:00:00 America/New_York"}, // NGH26
         {"20260218 17:00:00 America/New_York", "20260327 17:00:00 America/New_York"}, // NGJ26
         {"20250101 17:00:00 America/New_York", "20280221 17:00:00 America/New_York"}, // backstop

     }},


    {"z:XYZ100",
     {
         {"20250617 00:00:00 America/New_York", "20250919 17:00:00 America/New_York"}, // NQU25
         {"20250916 00:00:00 America/New_York", "20251219 17:00:00 America/New_York"}, // NQZ25
         {"20251214 18:00:00 America/New_York", "20260320 17:00:00 America/New_York"}, // NQH26
         {"20260313 18:00:00 America/New_York", "20260619 17:00:00 America/New_York"}, // NQM26
         {"20260614 18:00:00 America/New_York", "20260918 17:00:00 America/New_York"}, // NQU26
         {"20260913 18:00:00 America/New_York", "20261218 17:00:00 America/New_York"}, // NQZ26
         {"20261213 18:00:00 America/New_York", "20270319 17:00:00 America/New_York"}, // NQH27
     }},

    // CME Nikkei 225 Yen futures — quarterly H/M/U/Z
    {"xyz:JP225",
     {
         {"20260313 18:00:00 America/New_York", "20260612 09:30:00 America/New_York"}, // NIYM6
         {"20260614 18:00:00 America/New_York", "20260911 09:30:00 America/New_York"}, // NIYU6
         {"20260913 18:00:00 America/New_York", "20261211 09:30:00 America/New_York"}, // NIYZ6
         {"20261213 18:00:00 America/New_York", "20270312 09:30:00 America/New_York"}, // NIYH7
     }},

};

// Given a date,
// We do the lookup re: what contract we're at and when
// that contract expires.
int64_t get_expiry_t_epoch(std::string symbol, int64_t start_t);

} // namespace pktrade::symbolizer
