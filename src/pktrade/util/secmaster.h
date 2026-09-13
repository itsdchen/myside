#pragma once

#include "pktrade/mdapi.h"
#include "pktrade/types.h"

#include <map>
#include <rapidjson/document.h>
/*

Loads and gives us basic stats for a symbol.

I'm going to do the quick and easy thing now, and then do the scaleable thing
later.

For binance, this is like the stuff from getExchangeinfo.

You know, it might be ok to just have this stored in code too.
I'm not opposed to that as a temporary thing. And then I wouldn't
need to refer to huge files and shit.
OK let's do it.

11/30
OK. I think this is where we are. I'm just going to download and save
the getExchangeInfo strings from an offline call.
TODO: either update these strings regularly or download it from
getExchangeInfo.


*/

namespace pktrade {

// Basically corresponds to an ExchangeInfo.
struct OneSymbolStat {
  Market mkt;
  SymbolId symbol;

  int symbol_idx;

  int price_precision;
  int quantity_precision;

  // min tick size.
  Price tick_size;
  Quantity lot_size;

  // store the approx mid price for sanity checks
  double approx_mid = 0;

  // Should also have a min_notional_size too.
};

class SecMaster {
 public:
  // TODO: these should actually be somewehat gathered from
  // live market data (eg. make a call to getExchangeInfo instead of
  // storing saved jsons)
  // is_mainnet is true iff it's the live trading location,
  // versus teh testnet version.
  void load_from_exchange_info(std::vector<BookId> wanted_book_ids, bool fetch_from_network);

  // More targeted loading.
  void load_sym_stat(BookId b_id);

  // For example, hyperliquid has an indexing in their stuff.
  int get_sym_idx(BookId book_id);

  Price calc_tick_size(double approx_mid);
  Price get_tick_size(BookId book_id);
  Quantity get_lot_size(BookId book_id);
  double get_approx_mid(BookId book_id);

  Price ticks_away(BookId book_id, Price p, int ticks, Side side, bool more_aggressive);

  Price ticks_away_dbl(BookId book_id, double p, int ticks, Side side, bool more_aggressive);

  int approx_ticks_between(BookId book_id, Price p1, Price p2);

  // Given a double, use ticksize to find the closest price to it (in the more-
  // or less- aggressive direction)
  Price closest_price(BookId book_id, double p, Side side, bool more_aggressive);

  Quantity round_qty(BookId book_id, double qty, bool round_up);

  // /////////////////////////////////////////////////////////////
  // Loading helpers.

  // tbh. I think it's more efficient to read these in on loadtime and
  // then jusst refer to a smaller bookid_map during runtime.

  void loadHyperliquidSym(BookId book_id, bool fetch_from_network);
  // /////////////////////////////////////////////////////////////

 protected:
  std::map<BookId, OneSymbolStat> bookid_map_;

  // Cached hyperliquid info
  rapidjson::Document hyperliquid_exchinfo_doc_;
  rapidjson::Document hyperliquid_mids_doc_;

  rapidjson::Document hyperliquid_spot_exchinfo_doc_;
  rapidjson::Document hyperliquid_spot_mids_doc_;

  // Really just for the xyz dex. 
  rapidjson::Document hyperliquid_hip3_exchinfo_doc_;
  rapidjson::Document hyperliquid_hip3_mids_doc_;
  
  // Just doing it this way because this is just getting too annoying.
  std::map<std::string, double> hyperliquid_spot_to_mid_;
  std::map<std::string, int> hyperliquid_spot_to_sz_precision_;

  bool rapidjson_exchinfo_loaded_ = false;
};

} // namespace pktrade
