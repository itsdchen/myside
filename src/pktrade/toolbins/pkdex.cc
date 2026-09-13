#include <stdio.h>

#include "pktrade/side.h"

#include "pktrade/mdapi.h"
#include "pktrade/util/secmaster.h"
#include <magic_enum.hpp>

#include "pktrade/enums/basic_enums.h"
#include "pktrade/util/cli.h"
#include <fmt/format.h>

/*

Pretty simple script that exposes some fields from the secmaster in here.
Given symbol, market, I want to retrieve the basicfew sm options.

This is so we don't deal with exposing c++ bindings and ish.

Usage:

./bin/pkdex --action LIST --book BinanceFutures --symbol BTCUSDT
BTCUSDT on BinanceFutures:
LOTSIZE: 0.001000
TICKSIZE: 0.100000

./bin/pkdex --action ROUNDPXUP --book BinanceFutures --symbol BTCUSDT --tgt
50000.01

PX: 50000.100000

./bin/pkdex --action ROUNDPXDOWN --book BinanceFutures --symbol BTCUSDT --tgt
50000.01
PX: 50000.000000


./bin/pkdex --action ROUNDQTY --book BinanceFutures --symbol BTCUSDT --tgt
0.00234 QTY: 0.003000

*/

using namespace pktrade;

int main(int argc, char** argv) {
  std::string book;
  std::string sym;
  std::string action = "LIST";
  double tgt;

  CLI::App cmd_flags("pkdex");
  cmd_flags.add_option("-b,--book", book, "Book")->required();
  cmd_flags.add_option("--symbol", sym, "Symbol")->required();
  cmd_flags.add_option("--action", action, "action");
  cmd_flags.add_option("--tgt", tgt, "Target number");

  PARSE(cmd_flags, argc, argv);
  Market mkt = magic_enum::enum_cast<Market>(book).value();

  // Load secmaster stuff.

  BookId one_bid = BookId{mkt, sym};
  SecMaster secmaster;
  secmaster.load_from_exchange_info({one_bid}, false);

  // Action types:
  // LIST
  // ROUNDPXUP
  // ROUNDPXDOWN
  // ROUNDQTY

  // In all of these, load the secmaster.

  // OK, couple of actions.
  if (action == "LIST") {
    // List secmaster properties.

    Price ts = secmaster.get_tick_size(one_bid);
    Quantity lot_size = secmaster.get_lot_size(one_bid);
    printf("%s on %s:\n", sym.c_str(), book.c_str());
    printf("LOTSIZE: %f\n", lot_size.toDouble());
    printf("TICKSIZE: %f\n", ts.toDouble());

  } else if (action == "ROUNDPXUP") {
    // For now, gonna assume this is uniform ticksize.
    // If we ever switch to more complex books with differing ticksizes,
    // then let's change this.
    // TODO (far in future)
    Price px(std::to_string(tgt));
    Price new_px = secmaster.closest_price(one_bid, px.toDouble(), Side::Buy, true);
    printf("PX: %f\n", new_px.toDouble());
  } else if (action == "ROUNDPXDOWN") {
    Price px(std::to_string(tgt));
    Price new_px = secmaster.closest_price(one_bid, px.toDouble(), Side::Buy, false);
    printf("PX: %f\n", new_px.toDouble());

  } else if (action == "ROUNDQTY") {
    Quantity new_qty = secmaster.round_qty(one_bid, tgt, true);
    printf("QTY: %f\n", new_qty.toDouble());
  }
}
