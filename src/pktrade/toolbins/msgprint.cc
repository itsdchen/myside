#include <filesystem>
#include <unordered_set>

#include "msgprintclass.h"

#include "pktrade/book_manager.h"
#include "pktrade/event_loop.h"
#include "pktrade/feed.h"
#include "pktrade/util/cli.h"
#include <algorithm>
#include <fmt/format.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

using namespace pktrade;

/*
Sample command:
./bin/msgprint -d 20230103 -s "BTCBUSD" -b "BinanceSPOT" -T
--print-raw-msg -F 0 | l

./bin/msgprint --date 20230601 --book BinanceFutures --symbol SUIUSDT
-F 10 --start "08:00:00 America/New_York" --end "16:00:00
America/New_York" &> out

*/

// Samples:
// 20230103
// btcbusd
// binance
int main(int argc, char** argv) {
  std::string date;
  std::string start_t_str = "00:00:00 America/New_York";
  std::string end_t_str = "23:59:00 America/New_York";

  std::string book;
  std::string sym;
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";
  std::string equity_variant;

  bool time_fullstr = false;
  // If unset, don't print full book.
  // If set, then just print full book every X seconds.
  // If 0, then print it on every callback.
  double print_full_book_every = -1;
  int print_n_levels = 5;

  // If true, this prints the full message
  // that we're PARSING from the feed.
  // eg. the exact string that gets broken down into
  bool print_raw_msg = false;

  bool skip_msgs = false;

  CLI::App cmd_flags("msgprint");
  cmd_flags.add_option("-d,--date", date, "Date")->required();
  cmd_flags.add_option("--start", start_t_str, "Start Time");
  cmd_flags.add_option("--end", end_t_str, "End Time");

  // This book needs to correspond to our Enum names. Meaning that binance needs
  // to map to BinanceSPOT.
  cmd_flags.add_option("-b,--book", book, "Book")->required();
  cmd_flags.add_option("-s,--symbol", sym, "Symbol")->required();
  cmd_flags.add_option("--datadir", data_dir, "Data Dir");
  cmd_flags.add_option("--equity-variant", equity_variant,
                       "Suffix for TopBookEquity dir (e.g. 'Boats' loads from TopBookEquity_Boats/)");

  cmd_flags.add_flag("-T,--time-fullstr", time_fullstr, "Print time in full string format");

  cmd_flags.add_option("-F,--full-book-t", print_full_book_every,
                       "Print full book every X seconds. (0 = never)");
  cmd_flags.add_option("--print-n-levels", print_n_levels, "Print N levels of the book.");

  cmd_flags.add_flag("--print-raw-msg", print_raw_msg, "Print the raw message we're parsing");

  cmd_flags.add_flag("--skip-msgs", skip_msgs,
                     "Don't print normal book messages so we just see the full book");
  cmd_flags.add_option("--data", data_dir,
                       "Where you get data");

  PARSE(cmd_flags, argc, argv);

  pktrade::GlobalVar::date_ = date;
  std::string start_dt = date + " " + start_t_str;
  std::string end_dt = date + " " + end_t_str;

  date::zoned_seconds start_t = pktrade::time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = pktrade::time_utils::dtToSecs(end_dt);

  // Set the globalvars.
  pktrade::GlobalVar::date_ = date;
  pktrade::GlobalVar::start_t_ = pktrade::time_utils::zsToTp(start_t);
  pktrade::GlobalVar::end_t_ = pktrade::time_utils::zsToTp(end_t);

  // OK, now let's create the actual book.
  Market mkt = magic_enum::enum_cast<Market>(book).value();

  std::vector<BookId> books = {
      {mkt, SymbolId{sym}},
  };

  EventLoop loop;

  BookManager book_manager(books.begin(), books.end());

  MsgPrinter msgprinter(time_fullstr, print_full_book_every, print_n_levels, skip_msgs, books[0],
                        &book_manager);
  msgprinter.loop = &loop;

  for (auto& e : books) {
    book_manager.subscribe(e, &msgprinter);
  }

  std::vector<std::pair<std::filesystem::path, Market>> streams =
      pktrade::util::stream_paths(data_dir, books, date, true, true, equity_variant);

  HistoricalFeed feed(&book_manager, streams.begin(), streams.end(),
                      std::stoi(pktrade::GlobalVar::date_), false, true, true);
  loop.setPoller(std::make_unique<SimPoller>());

  // Add my guy to the feed.
  feed.addDebugListener(&msgprinter, print_raw_msg);

  loop.registerPollables(feed);
  loop.run();

  return 0;
}
