#include "secmaster.h"

#include "pktrade/util/basiclib.h"
#include <filesystem>

#include <rapidjson/document.h>
#include <rapidjson/filereadstream.h>

#include "secmaster_lib/HyperliquidSec.h"

#include <cmath>

namespace pktrade {

// secmaster

// At some point, can also give a path to exchange infos. Or provide
// an actual string.
void SecMaster::load_from_exchange_info(std::vector<BookId> wanted_symbols,
                                        bool fetch_from_network) {
  // Go through the exchange files, and populates the
  // wanted_symbols' properties. Easy peasy.

  for (BookId& book_id : wanted_symbols) {
    // Check if already loaded.
    if (bookid_map_.contains(book_id)) {
      continue;
    }

    if (book_id.first == Market::Hyperliquid) {
      loadHyperliquidSym(book_id, fetch_from_network);
    } else {
      throw std::runtime_error("Have not implemented " +
                               std::string(magic_enum::enum_name(book_id.first)));
    }
  }
}

int SecMaster::get_sym_idx(BookId book_id) {
  auto bookid_iter = bookid_map_.find(book_id);
  if (bookid_iter != bookid_map_.end()) {
    throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
  }
  return bookid_iter->second.symbol_idx;
}

Price SecMaster::calc_tick_size(double approx_mid) {
  // Rules for ticksize:
  // Prices are precise to the lesser of 5 significant figures or 6
  // decimals. For example, 1234.5 is valid but 1234.56 is not.
  // 0.001234 is valid, but 0.0012345 is not.
  // Tested it out, this should be the right thing:
  // >>> x = .0000123456; ts=math.pow(10, max(math.ceil(math.log(x, 10))-5,
  // -6)); print(ts); print(ts*(x//ts))
  double tick_size = pow(10, std::max(std::ceil(log10(approx_mid)) - 5, -6.0));
  return Price(std::to_string(tick_size));
}

Price SecMaster::get_tick_size(BookId book_id) {
  auto bookid_iter = bookid_map_.find(book_id);
  if (bookid_iter != bookid_map_.end()) {
    return bookid_iter->second.tick_size;
  }
  // Otherwise.
  // Should raise an exception actually.
  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
}

Quantity SecMaster::get_lot_size(BookId book_id) {
  auto bookid_iter = bookid_map_.find(book_id);

  if (bookid_iter != bookid_map_.end()) {
    return bookid_iter->second.lot_size;
  }
  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
}

double SecMaster::get_approx_mid(BookId book_id) {
  auto bookid_iter = bookid_map_.find(book_id);

  if (bookid_iter != bookid_map_.end()) {
    return bookid_iter->second.approx_mid;
  }
  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
}

Price SecMaster::ticks_away(BookId book_id, Price p, int ticks, Side side, bool more_aggressive) {
  auto bookid_iter = bookid_map_.find(book_id);

  if (bookid_iter != bookid_map_.end()) {
    Price tick_size = bookid_iter->second.tick_size;

    if (side == Side::Buy) {
      if (more_aggressive) {
        return p + tick_size * ticks;
      } else {
        return p - tick_size * ticks;
      }
    } else {
      if (more_aggressive) {
        return p - tick_size * ticks;
      } else {
        return p + tick_size * ticks;
      }
    }
  }

  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
  return Price{"1.0"};
}

Price SecMaster::ticks_away_dbl(BookId book_id, double p, int ticks, Side side,
                                bool more_aggressive) {
  auto bookid_iter = bookid_map_.find(book_id);

  if (bookid_iter != bookid_map_.end()) {
    double tick_size = bookid_iter->second.tick_size.toDouble();

    if (side == Side::Buy) {
      if (more_aggressive) {
        return closest_price(book_id, p + tick_size * ticks, side, more_aggressive);
      } else {
        return closest_price(book_id, p - tick_size * ticks, side, more_aggressive);
      }
    } else {
      if (more_aggressive) {
        return closest_price(book_id, p - tick_size * ticks, side, more_aggressive);
      } else {
        return closest_price(book_id, p + tick_size * ticks, side, more_aggressive);
      }
    }
  }

  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
  return Price{"1.0"};
}

// Should only be used as an approx.
int SecMaster::approx_ticks_between(BookId book_id, Price p1, Price p2) {
  auto bookid_iter = bookid_map_.find(book_id);

  if (bookid_iter != bookid_map_.end()) {
    Price tick_size = bookid_iter->second.tick_size;

    double px_diff_ticks;
    if (p1 > p2) {
      px_diff_ticks = (p1 - p2).toDouble() / tick_size.toDouble();
    } else {
      px_diff_ticks = (p2 - p1).toDouble() / tick_size.toDouble();
    }

    return round(px_diff_ticks);
  }

  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");

  return 1;
}

Price SecMaster::closest_price(BookId book_id, double p, Side side, bool more_aggressive) {
  auto bookid_iter = bookid_map_.find(book_id);

  if (bookid_iter != bookid_map_.end()) {
    Price tick_size = bookid_iter->second.tick_size;

    double new_px;

    // This is going to be a little wrong, w.r.t. precision, I think.
    // TODO: probably redo this to be absolutely correct when I have tome.

    if ((side == Side::Buy && more_aggressive) || (side == Side::Sell && !more_aggressive)) {
      // Round up
      new_px = tick_size.toDouble() * ceil(p / tick_size.toDouble());
    } else {
      // Round down.
      new_px = tick_size.toDouble() * floor(p / tick_size.toDouble());
    }

    std::string px_str = std::to_string(new_px);
    return Price{px_str};
  }

  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
  return Price{"1.0"};
}

Quantity SecMaster::round_qty(BookId book_id, double qty, bool round_up) {
  auto bookid_iter = bookid_map_.find(book_id);

  if (bookid_iter != bookid_map_.end()) {
    double lot_size = bookid_iter->second.lot_size.toDouble();

    double new_qty;
    // This is going to be a little wrong, w.r.t. precision, I think.
    // TODO: probably redo this to be absolutely correct when I have tome.
    if (round_up) {
      new_qty = lot_size * ceil(qty / lot_size);
    } else {
      // Round down.
      new_qty = lot_size * floor(qty / lot_size);
    }

    return Quantity{std::to_string(new_qty)};
  }

  throw std::runtime_error("Symbol " + util::print_bookid(book_id) + " not found in secmaster.");
  return Quantity{"1.0"};
}

// Note that even though the ui displays stuff like DOGE-USD
// or DOGEUSDPERP, the string names in the exchangeinfo are just
// the raw name, like DOGE. So those wiwll be the names we use.
// Note: if fetch_from_network is called, then we make the actual call
// to hyperliquid to retrieve it.
void SecMaster::loadHyperliquidSym(BookId book_id, bool fetch_from_network) {
  // TODO
  // Read through stuff on hyperliquid.
  if (!rapidjson_exchinfo_loaded_) {
    if (!fetch_from_network) {

      pktrade::secmasterfiles::loadHyperliquidInfo(hyperliquid_exchinfo_doc_);
      pktrade::secmasterfiles::loadHyperliquidMids(hyperliquid_mids_doc_);

      pktrade::secmasterfiles::loadHyperliquidSpotInfo(hyperliquid_spot_exchinfo_doc_);
      pktrade::secmasterfiles::loadHyperliquidSpotMids(hyperliquid_spot_mids_doc_);

      pktrade::secmasterfiles::loadHyperliquidHip3Info(hyperliquid_hip3_exchinfo_doc_);
      pktrade::secmasterfiles::loadHyperliquidHip3Mids(hyperliquid_hip3_mids_doc_);

    } else {
      // Sleep a little bit before running too.
      // This is because we might get rate limited from them.
      // All the hyperliquid info calls are wrapped in loops because
      // they might 429 us from making info calls.
      std::cout << " secmaster starting, loading info from hyperliquid " << std::endl;
      sleep(2);


      bool success = false;
      // Call them.
      success = pktrade::secmasterfiles::loadHyperliquidInfoLive(hyperliquid_exchinfo_doc_);
      if (!success) {
        while (!success) {
          std::cout << " Failed to load hyperliquid info, retrying..." << std::endl;
          sleep(20);
          success = pktrade::secmasterfiles::loadHyperliquidInfoLive(hyperliquid_exchinfo_doc_);
        }
      }

      sleep(2);
      success = pktrade::secmasterfiles::loadHyperliquidMidsLive(hyperliquid_mids_doc_);
      if (!success) {
        while (!success) {
          std::cout << " Failed to load hyperliquid mids, retrying..." << std::endl;
          sleep(20);
          success = pktrade::secmasterfiles::loadHyperliquidMidsLive(hyperliquid_mids_doc_);
        }
      }

      // hip3. 
      std::cout << " Loading HIP3 perps " << std::endl;
      success = pktrade::secmasterfiles::loadHyperliquidHip3InfoLive(hyperliquid_hip3_exchinfo_doc_);
      if (!success) {
        while (!success) {
          std::cout << " Failed to load hip3 info, retrying..." << std::endl;
          sleep(20);
          success = pktrade::secmasterfiles::loadHyperliquidHip3InfoLive(hyperliquid_hip3_exchinfo_doc_);
        }
      }

      sleep(2);
      success = pktrade::secmasterfiles::loadHyperliquidHip3MidsLive(hyperliquid_hip3_mids_doc_);
      if (!success) {
        while (!success) {
          std::cout << " Failed to load hip3 mids, retrying..." << std::endl;
          sleep(20);
          success = pktrade::secmasterfiles::loadHyperliquidHip3MidsLive(hyperliquid_hip3_mids_doc_);
        }
      }


      // Sleep a little between these calls, in case we have a lot of things.
      sleep(2);
      // Maybe slow down a little bit.
      std::cout << " Loading spot " << std::endl;
      success =
          pktrade::secmasterfiles::loadHyperliquidSpotInfoLive(hyperliquid_spot_exchinfo_doc_);
      if (!success) {
        while (!success) {
          std::cout << " Failed to load hyperliquid spot info, retrying..." << std::endl;
          sleep(20);
          success =
              pktrade::secmasterfiles::loadHyperliquidSpotInfoLive(hyperliquid_spot_exchinfo_doc_);
        }
      }

      sleep(2);
      std::cout << " Loading spot mids " << std::endl;
      success = pktrade::secmasterfiles::loadHyperliquidSpotMidsLive(hyperliquid_spot_mids_doc_);
      if (!success) {
        while (!success) {
          std::cout << " Failed to load hyperliquid spot mids, retrying..." << std::endl;
          sleep(20);
          success =
              pktrade::secmasterfiles::loadHyperliquidSpotMidsLive(hyperliquid_spot_mids_doc_);
        }
      }
    }
    rapidjson_exchinfo_loaded_ = true;
  }

  std::string symbol_str = book_id.second.get();
  bool found = false;

  // handle the perps.
  int idx = 0;

  for (rapidjson::Value& sym_section : hyperliquid_exchinfo_doc_["universe"].GetArray()) {
    if (symbol_str == sym_section["name"].GetString()) {
      // We found it I guess. Let's add it in the map.
      std::string ls = "0";

      // Super annoying, huh.
      int num_sz_decimals = sym_section["szDecimals"].GetInt();
      double sz_precision = pow(10, -num_sz_decimals);
      ls = std::to_string(sz_precision);

      // Saving the ticksize.
      double approx_mid = std::stod(hyperliquid_mids_doc_[symbol_str.c_str()].GetString());

      bookid_map_.emplace(book_id, OneSymbolStat{.mkt = book_id.first,
                                                 .symbol = book_id.second,
                                                 .symbol_idx = idx,
                                                 // Not really a thing for hyperliquid.
                                                 .price_precision = 1,
                                                 .quantity_precision = num_sz_decimals,
                                                 .tick_size = calc_tick_size(approx_mid),
                                                 .lot_size = Quantity{ls},
                                                 .approx_mid = approx_mid});
    }
    idx++;
    if (found) {
      break;
    }
  }

  if (found)
    return;

  // Try to look for unit dex symbols. 
  found = false; 

  // A little lazy, but this is what the unit dex is. 
  // It's not used by us. 
  // The lookup for this kind of thing is done in wsgateway btw. 
  idx = 110000;
  for (rapidjson::Value& sym_section : hyperliquid_hip3_exchinfo_doc_["universe"].GetArray()) {
    if (symbol_str == sym_section["name"].GetString()) {
      // We found it I guess. Let's add it in the map.
      std::string ls = "0";

      // Super annoying, huh.
      int num_sz_decimals = sym_section["szDecimals"].GetInt();
      double sz_precision = pow(10, -num_sz_decimals);
      ls = std::to_string(sz_precision);

      // Saving the ticksize.
      double approx_mid = std::stod(hyperliquid_hip3_mids_doc_[symbol_str.c_str()].GetString());

      bookid_map_.emplace(book_id, OneSymbolStat{.mkt = book_id.first,
                                                 .symbol = book_id.second,
                                                 .symbol_idx = idx,
                                                 // Not really a thing for hyperliquid.
                                                 .price_precision = 1,
                                                 .quantity_precision = num_sz_decimals,
                                                 .tick_size = calc_tick_size(approx_mid),
                                                 .lot_size = Quantity{ls},
                                                 .approx_mid = approx_mid});
    }
    idx++;
    if (found) {
      break;
    }
  }


  if (found)
    return;
  // If we haven't found, go on to spot.

  // Handle the spot case.
  int nu_idx = 0;

  std::string ls = "0";

  // I guess let's just make the map of name -> sizePrecision first?

  std::string sym_name;
  std::string pair_name;

  int sz_decimals;
  found = false;
  for (rapidjson::Value& one_sym_dct : hyperliquid_spot_exchinfo_doc_["tokens"].GetArray()) {
    sym_name = one_sym_dct["name"].GetString();
    if (sym_name == "USDC") {
      continue;
    }

    // Get the approx mid price.

    pair_name = fmt::format("{}/USDC", sym_name);
    sz_decimals = one_sym_dct["szDecimals"].GetDouble();

    if (pair_name == book_id.second.get()) {
      // Found it.
      found = true;
      break;
    }
  }
  double sz_precision = pow(10, -sz_decimals);
  ls = std::to_string(sz_precision);

  // Now, get the precisions.

  int coin_idx = 0;
  // Iterate through the tokens dict.

  /*
  {
    "name": "MANLET",
    "szDecimals": 0,
   "weiDecimals": 5,
    "index": 4,
    "tokenId": "0xe9ced9225d2a69ccc8d6a5b224524b99",
    "isCanonical": false
  },

  */

  for (rapidjson::Value& src_token_dct : hyperliquid_spot_mids_doc_[0]["tokens"].GetArray()) {
    std::string pair_name = fmt::format("{}/USDC", src_token_dct["name"].GetString());

    if (symbol_str == pair_name) {
      coin_idx = src_token_dct["index"].GetInt();
      // std::cout << " Found for " << symbol_str << " at " << coin_idx
      //           << std::endl;
      break;
    }
  }

  // Then iterate through the mids part.

  // Few things like this.
  /*
  {
    "prevDayPx" : "12.13",
          "dayNtlVlm" : "456595.1512",
          "markPx" : "11.518",
          "midPx" : "11.5095",
          "circulatingSupply"
        : "999585.0515728",
          "coin" : "@1"
  }
 */
  // std::cout << " for " << sym_name << " sz_decimals is " << sz_decimals
  //           << std::endl;

  if (found) {
    double approx_mid = std::stod(hyperliquid_spot_mids_doc_[1][coin_idx]["midPx"].GetString());
    bookid_map_.emplace(book_id, OneSymbolStat{.mkt = book_id.first,
                                             .symbol = book_id.second,
                                             .symbol_idx = nu_idx,
                                             // Not really a thing for hyperliquid.
                                             .price_precision = 1,
                                             .quantity_precision = sz_decimals,
                                             .tick_size = calc_tick_size(approx_mid),
                                             .lot_size = Quantity{ls},
                                             .approx_mid = approx_mid});
  }
}

} // namespace pktrade
