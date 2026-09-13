#include <filesystem>
#include <unordered_set>
#include <limits>
#include <map>
#include <thread>
#include <cstdlib>
#include <sys/select.h>
#include <unistd.h>
#include <sstream>
#include <ctime>
#include <fstream>
#include <cctype>
#include <optional>

#include "livemsgprintclass.h"

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
./bin/livemsgprint -d 20230103 -s "BTCBUSD" -b "BinanceSPOT"

Interactive controls:
  Space: Pause/Resume playback
  ←/→: Step backward/forward
  ↑/↓: Increase/decrease playback speed
  Home: Jump to beginning
  End: Jump to end
  g: Go to specific time
  h: Show help
  q: Quit

**For livemsgprint (interactive book visualization):**
The livemsgprint tool requires ncurses for terminal UI. Install with:
```bash
sudo apt update
sudo apt install libncurses5-dev libncurses5

(if that doesn't work, try this):
sudo apt install libncurses-dev

```


*/

int main(int argc, char** argv) {
  std::string date;
  std::string start_t_str = "00:00:00 America/New_York";
  std::string end_t_str = "23:59:00 America/New_York";

  std::string book;
  std::string sym;
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";
  std::string equity_variant;

  bool time_fullstr = false;
  int print_n_levels = 20;
  // Just going to make it bigger, 300M max events stored.
  int max_events = 300000000;
  std::string orders_file;

  CLI::App cmd_flags("livemsgprint - Interactive book visualization");
  cmd_flags.add_option("-d,--date", date, "Date")->required();
  cmd_flags.add_option("--start", start_t_str, "Initial display anchor time");
  cmd_flags.add_option("--end", end_t_str, "Replay end time");

  cmd_flags.add_option("-b,--book", book, "Book")->required();
  cmd_flags.add_option("-s,--symbol", sym, "Symbol")->required();

  cmd_flags.add_flag("-T,--time-fullstr", time_fullstr,
                     "Print time in full string format");

  cmd_flags.add_option("--print-n-levels", print_n_levels,
                       "Display N levels of the book (default: 20)");
  cmd_flags.add_option("--max-events", max_events,
                       "Maximum events to store (default: 300M)");
  cmd_flags.add_option("--orders", orders_file,
                       "Path to orders CSV file for overlay visualization");
  cmd_flags.add_option("--data", data_dir,
                       "Where you get data");
  cmd_flags.add_option("--equity-variant", equity_variant,
                       "Suffix for TopBookEquity dir (e.g. 'Boats' loads from TopBookEquity_Boats/)");

  PARSE(cmd_flags, argc, argv);

  pktrade::GlobalVar::date_ = date;
  std::string start_dt = date + " " + start_t_str;
  std::string end_dt = date + " " + end_t_str;

  date::zoned_seconds start_t = pktrade::time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = pktrade::time_utils::dtToSecs(end_dt);

  pktrade::GlobalVar::date_ = date;
  pktrade::GlobalVar::start_t_ = pktrade::time_utils::zsToTp(start_t);
  pktrade::GlobalVar::end_t_ = pktrade::time_utils::zsToTp(end_t);
  if (pktrade::GlobalVar::end_t_ < pktrade::GlobalVar::start_t_) {
    pktrade::GlobalVar::end_t_ += std::chrono::hours(24);
  }

  Market mkt = magic_enum::enum_cast<Market>(book).value();

  std::vector<BookId> books = {
      {mkt, SymbolId{sym}},
  };

  EventLoop loop;
  BookManager book_manager(books.begin(), books.end());

  LiveMsgPrinter live_printer(time_fullstr, print_n_levels, books[0], &book_manager, max_events, orders_file, sym);
  live_printer.loop = &loop;

  if (!live_printer.initialize()) {
    std::cerr << "Failed to initialize live message printer" << std::endl;
    return 1;
  }

  for (auto& e : books) {
    book_manager.subscribe(e, &live_printer);
  }

  std::vector<std::pair<std::filesystem::path, Market>> streams =
      pktrade::util::stream_paths(data_dir, books, date, true, true, equity_variant);

  HistoricalFeed feed(&book_manager, streams.begin(), streams.end(),
                      std::stoi(pktrade::GlobalVar::date_), false, true, true);
  loop.setPoller(std::make_unique<SimPoller>());

  feed.addDebugListener(&live_printer, false);

  loop.registerPollables(feed);

  // Run in a separate thread to allow UI interaction
  std::thread feed_thread([&loop]() {
    loop.run();
  });

  // Run the interactive UI
  live_printer.run();

  // Cleanup
  feed_thread.join();
  live_printer.cleanup();

  return 0;
}

// Implementation of EventStore methods
void EventStore::addEvent(const BookEvent& event) {
  std::lock_guard<std::mutex> lock(events_mutex_);

  bool was_at_end = (current_index_ + 1 == events_.size()) || events_.empty();

  events_.push_back(event);

  if (events_.size() > max_events_) {
    events_.pop_front();
    if (current_index_ > 0) {
      current_index_--;
    }
  }

  // If we were at the end and auto-follow is enabled, automatically advance to the new event
  if (auto_follow_ && was_at_end && !events_.empty()) {
    current_index_ = events_.size() - 1;
  }
}

const BookEvent* EventStore::getCurrentEvent() const {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (current_index_ >= events_.size()) {
    return nullptr;
  }
  return &events_[current_index_];
}

const BookEvent* EventStore::getEventAt(size_t index) const {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (index >= events_.size()) {
    return nullptr;
  }
  return &events_[index];
}

bool EventStore::stepForward() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (current_index_ + 1 < events_.size()) {
    current_index_++;
    return true;
  }
  // Can't step forward - we're at the end of available events
  return false;
}

bool EventStore::stepBackward() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (current_index_ > 0) {
    current_index_--;
    auto_follow_ = false; // Disable auto-follow when manually stepping backward
    return true;
  }
  return false;
}

bool EventStore::jumpMinuteForward() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty() || current_index_ >= events_.size()) return false;

  auto current_time = events_[current_index_].timestamp;
  auto target_time = current_time + std::chrono::minutes(1);

  // Find the closest event at or after target_time
  for (size_t i = current_index_ + 1; i < events_.size(); ++i) {
    if (events_[i].timestamp >= target_time) {
      current_index_ = i;
      auto_follow_ = false;
      return true;
    }
  }

  // If no event found after target time, jump to last event
  if (current_index_ < events_.size() - 1) {
    current_index_ = events_.size() - 1;
    auto_follow_ = false;
    return true;
  }

  return false;
}

bool EventStore::jumpMinuteBackward() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty() || current_index_ == 0) return false;

  auto current_time = events_[current_index_].timestamp;
  auto target_time = current_time - std::chrono::minutes(1);

  // Find the closest event at or before target_time (search backwards)
  for (size_t i = current_index_; i > 0; --i) {
    if (events_[i - 1].timestamp <= target_time) {
      current_index_ = i - 1;
      auto_follow_ = false;
      return true;
    }
  }

  // If no event found before target time, jump to first event
  current_index_ = 0;
  auto_follow_ = false;
  return true;
}

bool EventStore::jumpToNextTrade() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty()) return false;

  // Search forward from current position for next trade
  for (size_t i = current_index_ + 1; i < events_.size(); ++i) {
    if (events_[i].type == EventType::TRADE) {
      current_index_ = i;
      auto_follow_ = false;
      return true;
    }
  }
  return false; // No more trades found
}

bool EventStore::jumpToPrevTrade() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty() || current_index_ == 0) return false;

  // Search backward from current position for previous trade
  for (size_t i = current_index_; i > 0; --i) {
    if (events_[i - 1].type == EventType::TRADE) {
      current_index_ = i - 1;
      auto_follow_ = false;
      return true;
    }
  }
  return false; // No previous trades found
}

bool EventStore::jumpToNextExec() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty()) return false;

  // Search forward from current position for next order execution
  for (size_t i = current_index_ + 1; i < events_.size(); ++i) {
    if (events_[i].type == EventType::ORDER_EXEC) {
      current_index_ = i;
      auto_follow_ = false;
      return true;
    }
  }
  return false; // No more execs found
}

bool EventStore::jumpToPrevExec() {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty() || current_index_ == 0) return false;

  // Search backward from current position for previous order execution
  for (size_t i = current_index_; i > 0; --i) {
    if (events_[i - 1].type == EventType::ORDER_EXEC) {
      current_index_ = i - 1;
      auto_follow_ = false;
      return true;
    }
  }
  return false; // No previous execs found
}

void EventStore::jumpToIndex(size_t index) {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (index < events_.size()) {
    current_index_ = index;
  }
}

void EventStore::jumpToTime(std::chrono::time_point<Clock> target_time) {
  std::lock_guard<std::mutex> lock(events_mutex_);
  for (size_t i = 0; i < events_.size(); ++i) {
    if (events_[i].timestamp >= target_time) {
      current_index_ = i;
      return;
    }
  }
  // If not found, jump to end
  if (!events_.empty()) {
    current_index_ = events_.size() - 1;
  }
}

std::chrono::time_point<Clock> EventStore::getFirstTime() const {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty()) {
    return Clock::now();
  }
  return events_[0].timestamp;
}

std::chrono::time_point<Clock> EventStore::getLastTime() const {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty()) {
    return Clock::now();
  }
  return events_.back().timestamp;
}

bool EventStore::parseAndJumpToTime(const std::string& time_str) {
  std::lock_guard<std::mutex> lock(events_mutex_);

  if (events_.empty()) return false;

  auto first = std::find_if_not(time_str.begin(), time_str.end(),
                                [](unsigned char c) { return std::isspace(c); });
  auto last = std::find_if_not(time_str.rbegin(), time_str.rend(),
                               [](unsigned char c) { return std::isspace(c); }).base();
  if (first >= last) {
    return false;
  }
  std::string input(first, last);

  auto jump_to_first_at_or_after = [&](std::chrono::time_point<Clock> target_time) {
    for (size_t i = 0; i < events_.size(); ++i) {
      if (events_[i].timestamp >= target_time) {
        current_index_ = i;
        auto_follow_ = false;
        return true;
      }
    }

    // If the resolved target is beyond the loaded data, park at the end rather
    // than selecting an earlier "closest" event behind the user.
    current_index_ = events_.size() - 1;
    auto_follow_ = false;
    return true;
  };

  // Explicit date-time input bypasses loaded-range clock-time inference. This
  // is the escape hatch for jumping to an absolute timestamp.
  if (input.size() > 9 && input[8] == ' ' &&
      std::all_of(input.begin(), input.begin() + 8,
                  [](unsigned char c) { return std::isdigit(c); })) {
    try {
      std::istringstream explicit_ss(input.substr(9));
      std::string clock_str;
      std::string tz_str = "America/New_York";
      explicit_ss >> clock_str;
      if (clock_str.empty()) {
        return false;
      }
      explicit_ss >> tz_str;

      auto normalized_clock = time_utils::normalizeClockTime(clock_str);
      if (!normalized_clock) {
        return false;
      }

      std::string target_str =
          fmt::format("{} {} {}", input.substr(0, 8), *normalized_clock, tz_str);
      return jump_to_first_at_or_after(time_utils::strToClockTP(target_str));
    } catch (...) {
      return false;
    }
  }

  // Preserve numeric index jumps for inputs like "18"; time jumps need a colon
  // unless the user provides an explicit date above.
  if (input.find(':') == std::string::npos) {
    try {
      size_t parsed_chars = 0;
      size_t index = std::stoull(input, &parsed_chars);
      if (parsed_chars != input.size()) {
        return false;
      }
      if (index < events_.size()) {
        current_index_ = index;
        auto_follow_ = false;
        return true;
      }
      return false;
    } catch (...) {
      return false;
    }
  }

  auto normalized_clock = time_utils::normalizeClockTime(input);
  if (!normalized_clock) {
    return false;
  }

  std::optional<std::chrono::time_point<Clock>> first_market_time;
  std::optional<std::chrono::time_point<Clock>> last_market_time;
  for (const BookEvent& event : events_) {
    if (std::holds_alternative<BookEvent::OrderEventData>(event.data)) {
      continue;
    }
    if (!first_market_time) {
      first_market_time = event.timestamp;
    }
    last_market_time = event.timestamp;
  }

  if (!first_market_time || !last_market_time) {
    return false;
  }

  std::string first_market_date = time_utils::tpToString(*first_market_time).substr(0, 8);
  std::string target_str =
      fmt::format("{} {} America/New_York", first_market_date, *normalized_clock);
  auto target_time = time_utils::strToClockTP(target_str);
  while (target_time < *first_market_time) {
    target_time += std::chrono::hours(24);
  }

  if (target_time > *last_market_time) {
    return false;
  }

  return jump_to_first_at_or_after(target_time);
}

const BookEvent* EventStore::getRecentBookSnapshot() const {
  std::lock_guard<std::mutex> lock(events_mutex_);
  if (events_.empty()) return nullptr;

  // Search backwards from current_index_ to find an event with a non-empty book snapshot
  for (size_t i = current_index_ + 1; i > 0; --i) {
    const BookEvent& event = events_[i - 1];
    if (!event.bids.empty() || !event.asks.empty()) {
      return &event;
    }
  }
  return nullptr;
}

// Implementation of TimeNavigator methods
bool TimeNavigator::shouldStep() {
  if (playback_state_ != PlaybackState::PLAYING) {
    return false;
  }

  auto now = Clock::now();
  auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_step_time_);

  // Calculate step interval based on playback speed
  int step_interval_ms = static_cast<int>(100.0 / playback_speed_); // Base 100ms interval

  if (elapsed.count() >= step_interval_ms) {
    last_step_time_ = now;
    return true;
  }

  return false;
}

// Implementation of SplitScreenUI methods
SplitScreenUI::SplitScreenUI()
  : book_win_(nullptr), event_win_(nullptr), status_win_(nullptr),
    screen_height_(0), screen_width_(0) {}

SplitScreenUI::~SplitScreenUI() {
  cleanup();
}

bool SplitScreenUI::initialize() {
  // Try to initialize ncurses first
  use_ncurses_ = true;

  if (initscr() == NULL) {
    use_ncurses_ = false;
    initializeTextMode();
    return true;
  }
  if (has_colors()) {
    start_color();
    initializeColors();
  }

  cbreak();
  noecho();
  keypad(stdscr, TRUE);
  nodelay(stdscr, TRUE);
  curs_set(0);

  getmaxyx(stdscr, screen_height_, screen_width_);

  // Create windows
  int book_width = screen_width_ / 2;
  int event_width = screen_width_ - book_width;
  int main_height = screen_height_ - 2; // Reserve 2 lines for status

  book_win_ = newwin(main_height, book_width, 0, 0);
  event_win_ = newwin(main_height, event_width, 0, book_width);
  status_win_ = newwin(2, screen_width_, main_height, 0);

  if (!book_win_ || !event_win_ || !status_win_) {
    cleanup();
    return false;
  }

  // Set window properties
  scrollok(event_win_, TRUE);

  // Draw initial borders
  box(book_win_, 0, 0);
  box(event_win_, 0, 0);

  mvwprintw(book_win_, 0, 2, " Book Ladder ");
  mvwprintw(event_win_, 0, 2, " Events ");

  refresh();
  return true;
}

void SplitScreenUI::cleanup() {
  if (book_win_) {
    delwin(book_win_);
    book_win_ = nullptr;
  }
  if (event_win_) {
    delwin(event_win_);
    event_win_ = nullptr;
  }
  if (status_win_) {
    delwin(status_win_);
    status_win_ = nullptr;
  }
  endwin();
}

void SplitScreenUI::handleResize() {
  getmaxyx(stdscr, screen_height_, screen_width_);

  // Recreate windows with new size
  cleanup();
  initialize();
}

void SplitScreenUI::initializeColors() {
  init_pair(1, COLOR_GREEN, COLOR_BLACK);  // Bids
  init_pair(2, COLOR_RED, COLOR_BLACK);    // Asks
  init_pair(3, COLOR_YELLOW, COLOR_BLACK); // Trades
  init_pair(4, COLOR_CYAN, COLOR_BLACK);   // Headers
  init_pair(5, COLOR_WHITE, COLOR_BLUE);   // Status bar
}

void SplitScreenUI::updateBookDisplay(const BookEvent* event, const OrderStateManager* order_state) {
  if (!event || !book_win_) return;

  wclear(book_win_);
  box(book_win_, 0, 0);
  mvwprintw(book_win_, 0, 2, " Book Ladder ");

  drawBookLadder(event->asks, event->bids, event, order_state);
  wrefresh(book_win_);
}

void SplitScreenUI::drawBookLadder(const std::vector<BookEvent::BookLevel>& asks,
                                  const std::vector<BookEvent::BookLevel>& bids,
                                  const BookEvent* event,
                                  const OrderStateManager* order_state) {
  int max_y, max_x;
  getmaxyx(book_win_, max_y, max_x);

  int center_y = max_y / 2;
  int y_pos = 2;

  // Check if current event is a trade
  double trade_price = -1.0;
  if (event && event->type == EventType::TRADE) {
    if (std::holds_alternative<BookEvent::TradeData>(event->data)) {
      trade_price = std::get<BookEvent::TradeData>(event->data).price;
    }
  }

  // Build merged ask levels: real book levels + synthetic levels for out-of-book orders
  struct MergedLevel {
    double price;
    double qty;           // Book quantity (0 if no book level)
    double flagged_qty;   // Flagged quantity
    std::vector<LiveOrder> orders;  // Orders at this price
    bool is_synthetic;    // True if this level has no book quantity (order-only)
  };

  std::vector<MergedLevel> merged_asks;
  std::vector<MergedLevel> merged_bids;

  // Start with real book levels for asks
  for (const auto& level : asks) {
    MergedLevel ml;
    ml.price = level.price;
    ml.qty = level.qty;
    ml.flagged_qty = level.flagged_qty;
    ml.is_synthetic = false;
    if (order_state) {
      ml.orders = order_state->getOrdersAtPrice(level.price, Side::Sell);
    }
    merged_asks.push_back(ml);
  }

  // Start with real book levels for bids
  for (const auto& level : bids) {
    MergedLevel ml;
    ml.price = level.price;
    ml.qty = level.qty;
    ml.flagged_qty = level.flagged_qty;
    ml.is_synthetic = false;
    if (order_state) {
      ml.orders = order_state->getOrdersAtPrice(level.price, Side::Buy);
    }
    merged_bids.push_back(ml);
  }

  // Add synthetic levels for out-of-book orders
  if (order_state) {
    std::unordered_set<double> ask_prices_in_merged;
    std::unordered_set<double> bid_prices_in_merged;
    for (const auto& ml : merged_asks) ask_prices_in_merged.insert(ml.price);
    for (const auto& ml : merged_bids) bid_prices_in_merged.insert(ml.price);

    // Group orders by price for synthetic levels
    std::map<double, std::vector<LiveOrder>> synthetic_ask_orders;
    std::map<double, std::vector<LiveOrder>> synthetic_bid_orders;

    for (const auto& [id, order] : order_state->getAllOrders()) {
      bool found = false;
      if (order.side == Side::Sell) {
        for (double px : ask_prices_in_merged) {
          if (std::abs(order.price - px) < 1e-6) {
            found = true;
            break;
          }
        }
        if (!found) {
          synthetic_ask_orders[order.price].push_back(order);
        }
      } else {
        for (double px : bid_prices_in_merged) {
          if (std::abs(order.price - px) < 1e-6) {
            found = true;
            break;
          }
        }
        if (!found) {
          synthetic_bid_orders[order.price].push_back(order);
        }
      }
    }

    // Create synthetic ask levels and insert at proper positions
    for (const auto& [price, orders] : synthetic_ask_orders) {
      MergedLevel ml;
      ml.price = price;
      ml.qty = 0;
      ml.flagged_qty = 0;
      ml.orders = orders;
      ml.is_synthetic = true;
      merged_asks.push_back(ml);
    }

    // Create synthetic bid levels and insert at proper positions
    for (const auto& [price, orders] : synthetic_bid_orders) {
      MergedLevel ml;
      ml.price = price;
      ml.qty = 0;
      ml.flagged_qty = 0;
      ml.orders = orders;
      ml.is_synthetic = true;
      merged_bids.push_back(ml);
    }

    // Sort asks by price ascending (best ask first)
    std::sort(merged_asks.begin(), merged_asks.end(),
              [](const MergedLevel& a, const MergedLevel& b) { return a.price < b.price; });

    // Sort bids by price descending (best bid first)
    std::sort(merged_bids.begin(), merged_bids.end(),
              [](const MergedLevel& a, const MergedLevel& b) { return a.price > b.price; });
  }

  // Draw asks anchored at the SPREAD line: merged_asks[0] (best ask) goes
  // immediately above center_y, and worse asks stack upward. When more
  // asks exist than rows fit on screen, the worst asks (far from spread)
  // are cropped off the top — not the best asks near the spread, which
  // is what the spread label and the eye both expect to see at the
  // bottom of the ask ladder.
  wattron(book_win_, COLOR_PAIR(2));
  int max_ask_rows = std::max(0, center_y - 2);
  int n_ask_to_draw = std::min(static_cast<int>(merged_asks.size()), max_ask_rows);
  for (int k = 0; k < n_ask_to_draw; ++k) {
    int i = n_ask_to_draw - 1 - k;
    int row = center_y - n_ask_to_draw + k;
    const auto& level = merged_asks[i];

    std::string level_str;
    if (level.is_synthetic) {
      // Synthetic level - no book quantity, just show price and orders
      level_str = fmt::format("{:10.4f} : {:>8s}", level.price, "---");
    } else if (level.flagged_qty > 0) {
      level_str = fmt::format("{:10.4f} : {:8.4f} ({:6.4f})", level.price, level.qty - level.flagged_qty, level.flagged_qty);
    } else {
      level_str = fmt::format("{:10.4f} : {:8.4f}", level.price, level.qty);
    }

    // Append orders at this price level
    for (const auto& ord : level.orders) {
      level_str += fmt::format(" (#{},{:.1f})", ord.order_id, ord.remaining_qty);
    }

    if (trade_price > 0 && std::abs(level.price - trade_price) < 1e-6) {
      mvwprintw(book_win_, row, 2, "%s <--TRD", level_str.c_str());
    } else {
      mvwprintw(book_win_, row, 2, "%s", level_str.c_str());
    }
  }
  wattroff(book_win_, COLOR_PAIR(2));

  // Draw spread line with actual spread info
  std::string spread_line = "--- SPREAD ---";
  if (!asks.empty() && !bids.empty()) {
    double best_ask = asks[0].price;
    double best_bid = bids[0].price;
    double spread_price = best_ask - best_bid;
    double mid_price = (best_ask + best_bid) / 2.0;
    double spread_bps = (spread_price / mid_price) * 10000.0; // Convert to basis points

    spread_line = fmt::format("--- SPREAD: {:.4f} ({:.1f}bps) ---", spread_price, spread_bps);

    // Check if trade was inside the spread
    if (trade_price > 0 && trade_price > best_bid && trade_price < best_ask) {
      spread_line += " <--TRD";
    }
  }
  mvwprintw(book_win_, center_y, 2, "%-*s", max_x - 4, spread_line.substr(0, max_x - 4).c_str());

  // Draw bids
  y_pos = center_y + 1;
  wattron(book_win_, COLOR_PAIR(1));
  for (size_t i = 0; i < merged_bids.size() && y_pos < max_y - 1; ++i, ++y_pos) {
    const auto& level = merged_bids[i];

    std::string level_str;
    if (level.is_synthetic) {
      // Synthetic level - no book quantity, just show price and orders
      level_str = fmt::format("{:10.4f} : {:>8s}", level.price, "---");
    } else if (level.flagged_qty > 0) {
      level_str = fmt::format("{:10.4f} : {:8.4f} ({:6.4f})", level.price, level.qty - level.flagged_qty, level.flagged_qty);
    } else {
      level_str = fmt::format("{:10.4f} : {:8.4f}", level.price, level.qty);
    }

    // Append orders at this price level
    for (const auto& ord : level.orders) {
      level_str += fmt::format(" (#{},{:.1f})", ord.order_id, ord.remaining_qty);
    }

    if (trade_price > 0 && std::abs(level.price - trade_price) < 1e-6) {
      mvwprintw(book_win_, y_pos, 2, "%s <--TRD", level_str.c_str());
    } else {
      mvwprintw(book_win_, y_pos, 2, "%s", level_str.c_str());
    }
  }
  wattroff(book_win_, COLOR_PAIR(1));
}

void SplitScreenUI::updateEventLog(const BookEvent* event) {
  if (!event || !event_win_) return;

  event_log_.push_back(event->description);

  if (event_log_.size() > MAX_EVENT_LOG_SIZE) {
    event_log_.pop_front();
  }

  drawEventLog();
  wrefresh(event_win_);
}

void SplitScreenUI::updateEventLogWindow(const EventStore& store) {
  if (!event_win_) return;

  wclear(event_win_);
  box(event_win_, 0, 0);
  mvwprintw(event_win_, 0, 2, " Events ");

  int max_y, max_x;
  getmaxyx(event_win_, max_y, max_x);

  size_t current_idx = store.getCurrentIndex();
  size_t total_events = store.getEventCount();

  if (total_events == 0) {
    wrefresh(event_win_);
    return;
  }

  // Show events in a window around the current position
  int display_lines = max_y - 3; // Account for borders and header
  int events_before = display_lines / 3; // Show 1/3 before current
  int events_after = display_lines - events_before - 1; // Rest after current

  size_t start_idx = (current_idx >= events_before) ? current_idx - events_before : 0;
  size_t end_idx = std::min(start_idx + display_lines, total_events);

  int y_pos = 1;
  for (size_t i = start_idx; i < end_idx && y_pos < max_y - 1; ++i, ++y_pos) {
    const BookEvent* event = store.getEventAt(i);
    if (!event) continue;

    std::string marker = (i == current_idx) ? "> " : "  ";
    std::string line = marker + event->description;

    if (i == current_idx) {
      wattron(event_win_, A_REVERSE); // Highlight current event
    }

    mvwprintw(event_win_, y_pos, 1, "%-*s", max_x - 2, line.substr(0, max_x - 2).c_str());

    if (i == current_idx) {
      wattroff(event_win_, A_REVERSE);
    }
  }

  wrefresh(event_win_);
}

void SplitScreenUI::drawEventLog() {
  wclear(event_win_);
  box(event_win_, 0, 0);
  mvwprintw(event_win_, 0, 2, " Events ");

  int max_y, max_x;
  getmaxyx(event_win_, max_y, max_x);

  int y_pos = max_y - 2;
  for (auto it = event_log_.rbegin(); it != event_log_.rend() && y_pos > 1; ++it, --y_pos) {
    mvwprintw(event_win_, y_pos, 2, "%-*s", max_x - 4, it->substr(0, max_x - 4).c_str());
  }
}

void SplitScreenUI::updateStatusBar(const EventStore& store, PlaybackState state, double speed) {
  if (!status_win_) return;

  wclear(status_win_);

  wattron(status_win_, COLOR_PAIR(5));

  std::string state_str;
  switch (state) {
    case PlaybackState::PAUSED: state_str = "PAUSED"; break;
    case PlaybackState::PLAYING: state_str = "PLAYING"; break;
    case PlaybackState::STEP_MODE: state_str = "STEP"; break;
  }

  std::string status = fmt::format("State: {} | Speed: {:.1f}x | Event: {}/{} | Controls: Space=Pause ←→=Step ↑↓=Speed q=Quit h=Help",
                                  state_str, speed,
                                  store.getEventCount() > 0 ? store.getCurrentIndex() + 1 : 0,
                                  store.getEventCount());

  mvwprintw(status_win_, 0, 0, "%-*s", screen_width_, status.substr(0, screen_width_).c_str());

  // Second line for time info
  const BookEvent* current = store.getCurrentEvent();
  if (current) {
    std::string time_str = time_utils::tpToString(current->timestamp);
    mvwprintw(status_win_, 1, 0, "Time: %-*s", screen_width_ - 6, time_str.c_str());
  }

  wattroff(status_win_, COLOR_PAIR(5));
  wrefresh(status_win_);
}

void SplitScreenUI::refresh() {
  ::refresh();
}

int SplitScreenUI::getInput() {
  return getch();
}

void SplitScreenUI::showHelp() {
  // Create a help window
  WINDOW* help_win = newwin(20, 60, (screen_height_ - 20) / 2, (screen_width_ - 60) / 2);
  box(help_win, 0, 0);
  mvwprintw(help_win, 0, 20, " Help ");

  mvwprintw(help_win, 2, 2, "Keyboard Controls:");
  mvwprintw(help_win, 4, 4, "Space    - Pause/Resume playback");
  mvwprintw(help_win, 5, 4, "←/→      - Navigate backward/forward through events");
  mvwprintw(help_win, 6, 4, "↑/↓      - Increase/decrease playback speed");
  mvwprintw(help_win, 7, 4, "j/k      - Jump forward/backward by 1 minute");
  mvwprintw(help_win, 8, 4, "t/r      - Jump to next/previous trade");
  mvwprintw(help_win, 9, 4, "e/w      - Jump to next/previous order execution");
  mvwprintw(help_win, 10, 4, "Home     - Jump to beginning");
  mvwprintw(help_win, 11, 4, "End      - Jump to end");
  mvwprintw(help_win, 12, 4, "g        - Go to time (HH:MM[:SS], date time, or index)");
  mvwprintw(help_win, 13, 4, "h        - Show this help");
  mvwprintw(help_win, 14, 4, "q        - Quit");

  mvwprintw(help_win, 16, 2, "Trade Location Indicators:");
  mvwprintw(help_win, 17, 4, "[ON_BOOK] [INSIDE_SPREAD] [BELOW_BID] [ABOVE_ASK]");

  mvwprintw(help_win, 18, 20, "Press any key to continue");

  wrefresh(help_win);
  nodelay(stdscr, FALSE);
  getch();
  nodelay(stdscr, TRUE);
  delwin(help_win);

  // Redraw everything
  wclear(stdscr);
  refresh();
}

std::string SplitScreenUI::showGotoDialog() {
  if (!use_ncurses_) {
    // For text mode, just use simple input
    printf("Enter time (HH:MM[:SS], YYYYMMDD HH:MM[:SS], or event index): ");
    std::string input;
    std::getline(std::cin, input);
    return input;
  }

  // Create a dialog window
  int dialog_width = 50;
  int dialog_height = 8;
  WINDOW* dialog_win = newwin(dialog_height, dialog_width,
                             (screen_height_ - dialog_height) / 2,
                             (screen_width_ - dialog_width) / 2);

  box(dialog_win, 0, 0);
  mvwprintw(dialog_win, 0, 15, " Go To Time ");

  mvwprintw(dialog_win, 2, 2, "Enter time or event index:");
  mvwprintw(dialog_win, 3, 2, "Examples: 14:30, 14:30:15, 20260603 18:00, 12345");
  mvwprintw(dialog_win, 5, 2, "Input: ");

  wrefresh(dialog_win);

  // Enable echo and turn off nodelay for input
  echo();
  nodelay(stdscr, FALSE);
  curs_set(1);

  // Get input
  char input_buffer[32];
  wmove(dialog_win, 5, 9);
  wgetstr(dialog_win, input_buffer);

  // Restore ncurses settings
  noecho();
  nodelay(stdscr, TRUE);
  curs_set(0);

  delwin(dialog_win);

  // Redraw everything
  wclear(stdscr);
  refresh();

  return std::string(input_buffer);
}

// Implementation of LiveMsgPrinter methods
bool LiveMsgPrinter::initialize() {
  running_ = true;
  loadOrdersFile();
  return ui_.initialize();
}

void LiveMsgPrinter::cleanup() {
  running_ = false;
  ui_.cleanup();
}

void LiveMsgPrinter::run() {
  navigator_.initialize(); // Start paused
  bool needs_display_update = true;

  while (running_) {
    // Handle input
    bool input_handled = handleKeyboardInput();

    // Check if we should step forward automatically
    if (navigator_.shouldStep()) {
      navigator_.stepForward();
      needs_display_update = true;
    }

    // Only update display when something changed
    if (needs_display_update || input_handled) {
      updateDisplay();
      needs_display_update = false;
    }

    // Sleep longer to reduce CPU usage and flickering
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
}

bool LiveMsgPrinter::handleKeyboardInput() {
  int ch = ui_.getInput();

  if (ch == ERR || ch == -1) {
    return false; // No input
  }

  switch (ch) {
    case ' ': // Space - pause/resume
      if (navigator_.getState() == PlaybackState::PLAYING) {
        navigator_.pause();
      } else {
        navigator_.play();
      }
      return true;

    case KEY_LEFT: // Left arrow - step backward
      navigator_.stepMode();
      event_store_.setAutoFollow(false); // Disable auto-follow for manual stepping
      navigator_.stepBackward();
      return true;

    case KEY_RIGHT: // Right arrow - step forward
      navigator_.stepMode();
      event_store_.setAutoFollow(false); // Disable auto-follow for manual stepping
      navigator_.stepForward();
      return true;

    case KEY_UP: // Up arrow - increase speed
      navigator_.setPlaybackSpeed(std::min(navigator_.getPlaybackSpeed() * 1.5, 10.0));
      return true;

    case KEY_DOWN: // Down arrow - decrease speed
      navigator_.setPlaybackSpeed(std::max(navigator_.getPlaybackSpeed() / 1.5, 0.1));
      return true;

    case 'j':
    case 'J': // j - jump forward 1 minute
      navigator_.stepMode();
      event_store_.jumpMinuteForward();
      return true;

    case 'k':
    case 'K': // k - jump backward 1 minute
      navigator_.stepMode();
      event_store_.jumpMinuteBackward();
      return true;

    case 't':
    case 'T': // t - jump to next trade
      navigator_.stepMode();
      event_store_.jumpToNextTrade();
      return true;

    case 'r':
    case 'R': // r - jump to previous trade
      navigator_.stepMode();
      event_store_.jumpToPrevTrade();
      return true;

    case 'e':
    case 'E': // e - jump to next order execution
      navigator_.stepMode();
      event_store_.jumpToNextExec();
      return true;

    case 'w':
    case 'W': // w - jump to previous order execution
      navigator_.stepMode();
      event_store_.jumpToPrevExec();
      return true;

    case KEY_HOME: // Home - jump to beginning
      navigator_.jumpToBeginning();
      return true;

    case KEY_END: // End - jump to end
      navigator_.jumpToEnd();
      return true;

    case 'h':
    case 'H':
      ui_.showHelp();
      return true;

    case 'g':
    case 'G': {
      std::string time_input = ui_.showGotoDialog();
      if (!time_input.empty()) {
        event_store_.parseAndJumpToTime(time_input);
      }
      return true;
    }

    case 'q':
    case 'Q':
    case 27: // Escape
      running_ = false;
      return true;

    case KEY_RESIZE:
      ui_.handleResize();
      return true;

    default:
      return false; // Unhandled key
  }
}

void LiveMsgPrinter::updateDisplay() {
  const BookEvent* current_event = event_store_.getCurrentEvent();

  // Rebuild order state for current event index
  size_t current_idx = event_store_.getCurrentIndex();
  rebuildOrderStateForIndex(current_idx);

  // If current event has no book snapshot (e.g., order events from file),
  // find the most recent event that does have a book snapshot
  const BookEvent* book_event = current_event;
  if (current_event && current_event->bids.empty() && current_event->asks.empty()) {
    const BookEvent* recent_book = event_store_.getRecentBookSnapshot();
    if (recent_book) {
      book_event = recent_book;
    }
  }

  ui_.updateBookDisplay(book_event, &order_state_);
  ui_.updateEventLogWindow(event_store_); // Show events around current position
  ui_.updateStatusBar(event_store_, navigator_.getState(), navigator_.getPlaybackSpeed());
  ui_.refresh();
}

void LiveMsgPrinter::captureBookSnapshot(BookEvent& event, const LevelBook& book) {
  // Capture bids
  auto& buy_side = book.side<BuySide>();
  int count = 0;
  for (const auto& level : buy_side) {
    if (count >= print_n_levels_) break;
    BookEvent::BookLevel book_level;
    book_level.price = level.px.toDouble();
    book_level.qty = level.qty.toDouble();
    book_level.flagged_qty = level.flagged_shares.toDouble();
    event.bids.push_back(book_level);
    count++;
  }

  // Capture asks
  auto& sell_side = book.side<SellSide>();
  count = 0;
  for (const auto& level : sell_side) {
    if (count >= print_n_levels_) break;
    BookEvent::BookLevel book_level;
    book_level.price = level.px.toDouble();
    book_level.qty = level.qty.toDouble();
    book_level.flagged_qty = level.flagged_shares.toDouble();
    event.asks.push_back(book_level);
    count++;
  }
}

// Helper function to format transact_t (ms since epoch) as time string
static std::string formatTransactTime(int64_t transact_t) {
  if (transact_t <= 0) return "";
  auto tp = time_utils::msToTp(transact_t);
  std::string full_str = time_utils::tpToString(tp);
  // Extract just the time portion (after the space)
  size_t space_pos = full_str.find(' ');
  if (space_pos != std::string::npos && space_pos + 1 < full_str.size()) {
    return " T:" + full_str.substr(space_pos + 1);
  }
  return " T:" + full_str;
}

void LiveMsgPrinter::addEventToStore(EventType type, const std::string& description,
                                    const std::variant<BookEvent::LevelData, BookEvent::TradeData, BookEvent::OrderEventData>& data) {
  if (!our_book_) return;

  // Insert any pending order events that should come before this book event
  int64_t current_ts_ms = time_utils::tpToMs(Clock::now());
  insertPendingOrderEvents(current_ts_ms);

  BookEvent event;
  event.timestamp = Clock::now();
  event.type = type;
  event.description = description;
  event.data = data;

  captureBookSnapshot(event, *our_book_);
  event_store_.addEvent(event);
  anchorDisplayIfNeeded(event);

  // Invalidate order state cache since we added a new event
  last_order_state_index_ = SIZE_MAX;

  // Temporary debug - print every 1000th event
  static int event_count = 0;
  event_count++;
  /*
  // Uncomment this to buffer, but don't do it too much - it messes up the ui.
  if (event_count % 1000 == 0) {
    fprintf(stderr, "Events: %d added, store has %zu\n", event_count, event_store_.getEventCount());
  }
  */
}

void LiveMsgPrinter::onLevelUpdate(const LevelBook& book, const LevelAdd& lvl_add) {
  if (outOfBounds()) return;

  if (first_tick_) {
    first_tick_ = false;
    our_book_ = &book;
  }

  BookEvent::LevelData level_data;
  level_data.price = lvl_add.px.toDouble();
  level_data.qty = lvl_add.qty.toDouble();
  level_data.prev_qty = 0.0;
  level_data.side = lvl_add.side;
  level_data.transact_t = lvl_add.transact_t;

  std::string desc = fmt::format("{} LevelAdd: {:.4f} @ {:.4f}{}",
                                time_utils::nowToStr(),
                                level_data.qty, level_data.price,
                                formatTransactTime(level_data.transact_t));

  addEventToStore(EventType::LEVEL_ADD, desc, level_data);
}

void LiveMsgPrinter::onLevelUpdate(const LevelBook& book, const LevelModify& lvl_mod) {
  if (outOfBounds()) return;

  BookEvent::LevelData level_data;
  level_data.price = lvl_mod.px.toDouble();
  level_data.qty = lvl_mod.qty.toDouble();
  level_data.prev_qty = lvl_mod.prev_qty.toDouble();
  level_data.side = lvl_mod.side;
  level_data.transact_t = lvl_mod.transact_t;

  std::string desc = fmt::format("{} LevelMod: {:.4f} -> {:.4f} @ {:.4f}{}",
                                time_utils::nowToStr(),
                                level_data.prev_qty, level_data.qty, level_data.price,
                                formatTransactTime(level_data.transact_t));

  addEventToStore(EventType::LEVEL_MODIFY, desc, level_data);
}

void LiveMsgPrinter::onLevelUpdate(const LevelBook& book, const LevelDelete& lvl_del) {
  if (outOfBounds()) return;

  BookEvent::LevelData level_data;
  level_data.price = lvl_del.px.toDouble();
  level_data.qty = 0.0;
  level_data.prev_qty = lvl_del.prev_qty.toDouble();
  level_data.side = lvl_del.side;
  level_data.transact_t = lvl_del.transact_t;

  std::string desc = fmt::format("{} LevelDel: {:.4f} @ {:.4f}{}",
                                time_utils::nowToStr(),
                                level_data.prev_qty, level_data.price,
                                formatTransactTime(level_data.transact_t));

  addEventToStore(EventType::LEVEL_DELETE, desc, level_data);
}

void LiveMsgPrinter::onLevelUpdate(const LevelBook& book, const Trade& trd) {
  if (outOfBounds()) return;

  BookEvent::TradeData trade_data;
  trade_data.price = trd.px.toDouble();
  trade_data.qty = trd.qty.toDouble();
  trade_data.passive_side = trd.passive_side;
  trade_data.transact_t = trd.transact_t;

  // Determine trade location relative to book
  std::string location_indicator = "";
  if (!book.side<BuySide>().empty() && !book.side<SellSide>().empty()) {
    double best_bid = book.side<BuySide>().begin()->px.toDouble();
    double best_ask = book.side<SellSide>().begin()->px.toDouble();

    if (trade_data.price == best_bid || trade_data.price == best_ask) {
      location_indicator = " [ON_BOOK]";
    } else if (trade_data.price > best_bid && trade_data.price < best_ask) {
      location_indicator = " [INSIDE_SPREAD]";
    } else if (trade_data.price < best_bid) {
      location_indicator = " [BELOW_BID]";
    } else if (trade_data.price > best_ask) {
      location_indicator = " [ABOVE_ASK]";
    }
  }

  std::string desc = fmt::format("{} Trade: {:.4f} @ {:.4f} ({}{}){}",
                                time_utils::nowToStr(),
                                trade_data.qty, trade_data.price,
                                magic_enum::enum_name(trade_data.passive_side),
                                location_indicator,
                                formatTransactTime(trade_data.transact_t));

  addEventToStore(EventType::TRADE, desc, trade_data);
}

void LiveMsgPrinter::onFinal(const LevelBook& book) {
  if (outOfBounds()) return;
  // Could add a final event marker here if needed
}

void LiveMsgPrinter::onMD(const mdmsg::PbMessage& msg) {
  if (outOfBounds()) return;
  // Handle raw message data if needed
}

void LiveMsgPrinter::onLastLine(const std::string& line) {
  if (outOfBounds()) return;
  // Handle last line if needed
}

// Helper function to split a string by delimiter
static std::vector<std::string> splitString(const std::string& str, char delimiter) {
  std::vector<std::string> tokens;
  std::stringstream ss(str);
  std::string token;
  while (std::getline(ss, token, delimiter)) {
    tokens.push_back(token);
  }
  return tokens;
}

void LiveMsgPrinter::loadOrdersFile() {
  if (orders_file_.empty()) return;

  std::ifstream file(orders_file_);
  if (!file.is_open()) {
    std::cerr << "Warning: Could not open orders file: " << orders_file_ << std::endl;
    return;
  }

  std::string line;
  while (std::getline(file, line)) {
    if (line.empty()) continue;

    auto tokens = splitString(line, ',');
    if (tokens.size() < 3) continue;

    // tokens[0] = timestamp string
    // tokens[1] = epoch_ms
    // tokens[2] = action type
    int64_t epoch_ms = std::stoll(tokens[1]);
    std::string action = tokens[2];

    BookEvent event;
    event.timestamp = time_utils::msToTp(epoch_ms);

    BookEvent::OrderEventData order_data;
    order_data.action = action;

    if (action == "NewOrd") {
      // NewOrd: timestamp,epoch_ms,NewOrd,order_id,symbol,market,side,price,qty,...
      if (tokens.size() < 9) continue;
      if (!target_symbol_.empty() && tokens[4] != target_symbol_) continue;
      order_data.order_id = std::stoi(tokens[3]);
      order_data.side = (tokens[6] == "Buy") ? Side::Buy : Side::Sell;
      order_data.price = std::stod(tokens[7]);
      order_data.qty = std::stod(tokens[8]);
      order_data.exec_qty = 0;
      event.type = EventType::ORDER_NEW;
      event.description = fmt::format("{} NewOrd #{} {} {:.4f} x {:.2f}",
                                      time_utils::tpToString(event.timestamp),
                                      order_data.order_id,
                                      tokens[6],
                                      order_data.price,
                                      order_data.qty);
    } else if (action == "NewOrdAck") {
      // NewOrdAck: timestamp,epoch_ms,NewOrdAck,order_id,symbol,market,side,exch_transact_time,PR:reason
      if (tokens.size() < 7) continue;
      if (!target_symbol_.empty() && tokens[4] != target_symbol_) continue;
      order_data.order_id = std::stoi(tokens[3]);
      order_data.side = (tokens[6] == "Buy") ? Side::Buy : Side::Sell;
      order_data.price = 0;
      order_data.qty = 0;
      order_data.exec_qty = 0;
      event.type = EventType::ORDER_ACK;
      event.description = fmt::format("{} NewOrdAck #{} {}",
                                      time_utils::tpToString(event.timestamp),
                                      order_data.order_id,
                                      tokens[6]);
    } else if (action == "Cancel") {
      // Cancel: timestamp,epoch_ms,Cancel,order_id,symbol,market
      if (tokens.size() < 6) continue;
      if (!target_symbol_.empty() && tokens[4] != target_symbol_) continue;
      order_data.order_id = std::stoi(tokens[3]);
      order_data.side = Side::Buy;  // Not specified in Cancel
      order_data.price = 0;
      order_data.qty = 0;
      order_data.exec_qty = 0;
      event.type = EventType::ORDER_CANCEL;
      event.description = fmt::format("{} Cancel #{}",
                                      time_utils::tpToString(event.timestamp),
                                      order_data.order_id);
    } else if (action == "CancelAck") {
      // CancelAck: timestamp,epoch_ms,CancelAck,order_id,symbol,market,exch_transact_time
      if (tokens.size() < 6) continue;
      if (!target_symbol_.empty() && tokens[4] != target_symbol_) continue;
      order_data.order_id = std::stoi(tokens[3]);
      order_data.side = Side::Buy;  // Not specified in CancelAck
      order_data.price = 0;
      order_data.qty = 0;
      order_data.exec_qty = 0;
      event.type = EventType::ORDER_CANCEL_ACK;
      event.description = fmt::format("{} CancelAck #{}",
                                      time_utils::tpToString(event.timestamp),
                                      order_data.order_id);
    } else if (action == "Exec") {
      // Exec: timestamp,epoch_ms,Exec,order_id,symbol,market,side,exec_px,exec_qty,ord_curr_qty,...
      if (tokens.size() < 10) continue;
      if (!target_symbol_.empty() && tokens[4] != target_symbol_) continue;
      order_data.order_id = std::stoi(tokens[3]);
      order_data.side = (tokens[6] == "Buy") ? Side::Buy : Side::Sell;
      order_data.price = std::stod(tokens[7]);
      order_data.exec_qty = std::stod(tokens[8]);
      order_data.qty = std::stod(tokens[9]);  // ord_curr_qty (remaining after exec)
      event.type = EventType::ORDER_EXEC;
      event.description = fmt::format("{} Exec #{} {} {:.4f} x {:.2f} (rem: {:.2f})",
                                      time_utils::tpToString(event.timestamp),
                                      order_data.order_id,
                                      tokens[6],
                                      order_data.price,
                                      order_data.exec_qty,
                                      order_data.qty);
    } else if (action == "Elimination") {
      // Elimination: timestamp,epoch_ms,Elimination,symbol,market,order_id,exchange_order_id,remaining_qty
      if (tokens.size() < 6) continue;
      if (!target_symbol_.empty() && tokens[3] != target_symbol_) continue;
      order_data.order_id = std::stoi(tokens[5]);
      order_data.side = Side::Buy;  // Not specified
      order_data.price = 0;
      order_data.qty = (tokens.size() > 7) ? std::stod(tokens[7]) : 0;  // remaining_qty
      order_data.exec_qty = 0;
      event.type = EventType::ORDER_ELIM;
      event.description = fmt::format("{} Elimination #{} (rem: {:.2f})",
                                      time_utils::tpToString(event.timestamp),
                                      order_data.order_id,
                                      order_data.qty);
    } else if (action == "NewOrderReject") {
      // NewOrderReject: timestamp,epoch_ms,NewOrderReject,symbol,market,order_id,side,qty,price,reason...
      if (tokens.size() < 9) continue;
      if (!target_symbol_.empty() && tokens[3] != target_symbol_) continue;
      order_data.order_id = std::stoi(tokens[5]);
      order_data.side = (tokens[6] == "Buy") ? Side::Buy : Side::Sell;
      order_data.qty = std::stod(tokens[7]);
      order_data.price = std::stod(tokens[8]);
      order_data.exec_qty = 0;
      event.type = EventType::ORDER_REJECT;
      event.description = fmt::format("{} NewOrderReject #{} {} {:.4f} @ {:.2f}",
                                      time_utils::tpToString(event.timestamp),
                                      order_data.order_id,
                                      tokens[6],
                                      order_data.qty,
                                      order_data.price);
    } else {
      // Unknown action, skip
      continue;
    }

    event.data = order_data;
    if (event.timestamp <= pktrade::GlobalVar::end_t_) {
      pending_order_events_.push_back(event);
    }
  }

  // Sort by timestamp
  std::sort(pending_order_events_.begin(), pending_order_events_.end(),
            [](const BookEvent& a, const BookEvent& b) {
              return a.timestamp < b.timestamp;
            });

  std::cerr << "Loaded " << pending_order_events_.size()
            << " order events for " << target_symbol_ << " through "
            << time_utils::tpToString(pktrade::GlobalVar::end_t_)
            << " from " << orders_file_ << std::endl;
}

void LiveMsgPrinter::insertPendingOrderEvents(int64_t current_timestamp_ms) {
  // Insert any order events that have timestamps <= current book event timestamp
  while (order_events_index_ < pending_order_events_.size()) {
    const BookEvent& order_event = pending_order_events_[order_events_index_];
    int64_t order_ts_ms = time_utils::tpToMs(order_event.timestamp);

    if (order_ts_ms <= current_timestamp_ms) {
      event_store_.addEvent(order_event);
      anchorDisplayIfNeeded(order_event);
      order_events_index_++;
    } else {
      break;
    }
  }
}

void LiveMsgPrinter::rebuildOrderStateForIndex(size_t target_index) {
  if (target_index == last_order_state_index_) {
    return;  // Already up to date
  }

  // Rebuild order state from scratch up to target_index
  order_state_.clear();

  for (size_t i = 0; i <= target_index && i < event_store_.getEventCount(); ++i) {
    const BookEvent* event = event_store_.getEventAt(i);
    if (!event) continue;

    applyOrderEventToState(*event);
  }

  last_order_state_index_ = target_index;
}

void LiveMsgPrinter::anchorDisplayIfNeeded(const BookEvent& event) {
  if (!display_anchor_pending_ || event.timestamp < display_anchor_t_) {
    return;
  }

  size_t event_count = event_store_.getEventCount();
  if (event_count > 0) {
    event_store_.setAutoFollow(false);
    event_store_.jumpToIndex(event_count - 1);
  }
  display_anchor_pending_ = false;
}

void LiveMsgPrinter::applyOrderEventToState(const BookEvent& event) {
  if (!std::holds_alternative<BookEvent::OrderEventData>(event.data)) {
    return;
  }

  const auto& order_data = std::get<BookEvent::OrderEventData>(event.data);

  switch (event.type) {
    case EventType::ORDER_NEW:
      order_state_.addOrder(order_data.order_id, order_data.side,
                            order_data.price, order_data.qty);
      break;
    case EventType::ORDER_CANCEL_ACK:
    case EventType::ORDER_ELIM:
    case EventType::ORDER_REJECT:
      order_state_.removeOrder(order_data.order_id);
      break;
    case EventType::ORDER_EXEC:
      // order_data.qty is ord_curr_qty (remaining after exec)
      order_state_.updateOrderQty(order_data.order_id, order_data.qty);
      break;
    default:
      // ORDER_ACK and ORDER_CANCEL do not change visible open-order state.
      break;
  }
}

bool LiveMsgPrinter::outOfBounds() {
  if (Clock::now() > pktrade::GlobalVar::end_t_) {
    return true;
  }
  return false;
}

// Text mode implementations
void SplitScreenUI::initializeTextMode() {
  printf("=== LIVEMSGPRINT TEXT MODE ===\n");
  printf("Controls: Space=Pause, Left/Right=Step, Up/Down=Speed, h=Help, q=Quit\n");
  printf("Starting in PAUSED mode. Press Space to begin playback.\n\n");
}

void SplitScreenUI::printBookLadderText(const std::vector<BookEvent::BookLevel>& asks,
                                       const std::vector<BookEvent::BookLevel>& bids) {
  printf("\n=== BOOK LADDER ===\n");

  // Print asks (in reverse order, highest first)
  printf("ASKS:\n");
  for (int i = asks.size() - 1; i >= 0; --i) {
    const auto& level = asks[i];
    if (level.flagged_qty > 0) {
      printf("  %10.4f : %8.4f (%6.4f)\n", level.price, level.net_qty(), level.flagged_qty);
    } else {
      printf("  %10.4f : %8.4f\n", level.price, level.qty);
    }
  }

  printf("  --- SPREAD ---\n");

  // Print bids (highest first)
  printf("BIDS:\n");
  for (size_t i = 0; i < bids.size(); ++i) {
    const auto& level = bids[i];
    if (level.flagged_qty > 0) {
      printf("  %10.4f : %8.4f (%6.4f)\n", level.price, level.net_qty(), level.flagged_qty);
    } else {
      printf("  %10.4f : %8.4f\n", level.price, level.qty);
    }
  }
  printf("==================\n");
}

void SplitScreenUI::printStatusText(const EventStore& store, PlaybackState state, double speed) {
  std::string state_str;
  switch (state) {
    case PlaybackState::PAUSED: state_str = "PAUSED"; break;
    case PlaybackState::PLAYING: state_str = "PLAYING"; break;
    case PlaybackState::STEP_MODE: state_str = "STEP"; break;
  }

  printf("\n[%s] Speed: %.1fx | Event: %zu/%zu\n",
         state_str.c_str(), speed, store.getCurrentIndex() + 1, store.getEventCount());

  const BookEvent* current = store.getCurrentEvent();
  if (current) {
    std::string time_str = time_utils::tpToString(current->timestamp);
    printf("Time: %s\n", time_str.c_str());
    printf("Event: %s\n", current->description.c_str());
  }
  printf("--- Press h for help ---\n");
}

int SplitScreenUI::getTextInput() {
  // Simple non-blocking input for text mode
  fd_set readfds;
  struct timeval timeout;

  FD_ZERO(&readfds);
  FD_SET(STDIN_FILENO, &readfds);

  timeout.tv_sec = 0;
  timeout.tv_usec = 10000; // 10ms timeout

  if (select(STDIN_FILENO + 1, &readfds, nullptr, nullptr, &timeout) > 0) {
    return getchar();
  }

  return ERR; // No input available
}
