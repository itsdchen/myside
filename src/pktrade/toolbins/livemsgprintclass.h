#pragma once

#include "pktrade/book_manager.h"
#include "pktrade/event_loop.h"
#include "pktrade/feed.h"
#include "pktrade/util/cli.h"
#include <algorithm>
#include <fmt/format.h>
#include <ncurses.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

#include <deque>
#include <vector>
#include <memory>
#include <chrono>
#include <variant>
#include <thread>
#include <mutex>
#include <unordered_map>
#include <cmath>

using namespace pktrade;

enum class EventType {
  LEVEL_ADD,
  LEVEL_MODIFY,
  LEVEL_DELETE,
  TRADE,
  BOOK_SNAPSHOT,
  // Order events
  ORDER_NEW,
  ORDER_ACK,
  ORDER_CANCEL,
  ORDER_CANCEL_ACK,
  ORDER_EXEC,
  ORDER_ELIM,
  ORDER_REJECT
};

// Struct to track a live order
struct LiveOrder {
  int order_id;
  Side side;
  double price;
  double initial_qty;
  double remaining_qty;
};

// Class to manage live orders state
class OrderStateManager {
public:
  void addOrder(int order_id, Side side, double price, double qty) {
    live_orders_[order_id] = LiveOrder{order_id, side, price, qty, qty};
  }

  void removeOrder(int order_id) {
    live_orders_.erase(order_id);
  }

  void updateOrderQty(int order_id, double remaining_qty) {
    auto it = live_orders_.find(order_id);
    if (it != live_orders_.end()) {
      it->second.remaining_qty = remaining_qty;
      if (remaining_qty <= 0) {
        live_orders_.erase(it);
      }
    }
  }

  std::vector<LiveOrder> getOrdersAtPrice(double price, Side side) const {
    std::vector<LiveOrder> result;
    for (const auto& [id, order] : live_orders_) {
      if (order.side == side && std::abs(order.price - price) < 1e-6) {
        result.push_back(order);
      }
    }
    return result;
  }

  const std::unordered_map<int, LiveOrder>& getAllOrders() const {
    return live_orders_;
  }

  void clear() {
    live_orders_.clear();
  }

private:
  std::unordered_map<int, LiveOrder> live_orders_;
};

struct BookEvent {
  std::chrono::time_point<Clock> timestamp;
  EventType type;
  std::string description;

  // Event-specific data
  struct LevelData {
    double price;
    double qty;
    double prev_qty;
    Side side;
    int64_t transact_t;
  };

  struct TradeData {
    double price;
    double qty;
    Side passive_side;
    int64_t transact_t;
  };

  struct OrderEventData {
    int order_id;
    Side side;
    double price;
    double qty;           // For NewOrd: initial qty; For Exec: remaining qty after exec
    double exec_qty;      // For Exec: qty filled in this exec
    std::string action;   // "NewOrd", "NewOrdAck", "Cancel", "CancelAck", "Exec", "Elimination"
  };

  std::variant<LevelData, TradeData, OrderEventData> data;
  
  // Full book snapshot at this point in time
  struct BookLevel {
    double price;
    double qty;           // Total quantity
    double flagged_qty;   // Flagged (traded away) quantity
    double net_qty() const { return qty - flagged_qty; }
  };
  
  std::vector<BookLevel> bids;
  std::vector<BookLevel> asks;
};

class EventStore {
public:
  EventStore(int max_events = 10000000) : max_events_(max_events), current_index_(0), auto_follow_(true) {}
  
  void setAutoFollow(bool follow) {
    std::lock_guard<std::mutex> lock(events_mutex_);
    auto_follow_ = follow;
  }
  
  void addEvent(const BookEvent& event);
  const BookEvent* getCurrentEvent() const;
  const BookEvent* getEventAt(size_t index) const;
  
  bool stepForward();
  bool stepBackward();
  bool jumpMinuteForward();  // Jump forward 1 minute
  bool jumpMinuteBackward(); // Jump backward 1 minute
  bool jumpToNextTrade();    // Jump to next trade event
  bool jumpToPrevTrade();    // Jump to previous trade event
  bool jumpToNextExec();     // Jump to next order execution
  bool jumpToPrevExec();     // Jump to previous order execution
  void jumpToIndex(size_t index);
  void jumpToTime(std::chrono::time_point<Clock> target_time);
  bool parseAndJumpToTime(const std::string& time_str); // Parse HH:MM[:SS], YYYYMMDD HH:MM[:SS], or index

  // Find the most recent event at or before current_index that has a non-empty book snapshot
  const BookEvent* getRecentBookSnapshot() const;

  size_t getCurrentIndex() const { 
    std::lock_guard<std::mutex> lock(events_mutex_); 
    return current_index_; 
  }
  size_t getEventCount() const { 
    std::lock_guard<std::mutex> lock(events_mutex_); 
    return events_.size(); 
  }
  bool isEmpty() const { 
    std::lock_guard<std::mutex> lock(events_mutex_); 
    return events_.empty(); 
  }
  
  std::chrono::time_point<Clock> getFirstTime() const;
  std::chrono::time_point<Clock> getLastTime() const;

private:
  std::deque<BookEvent> events_;
  size_t max_events_;
  size_t current_index_;
  bool auto_follow_; // Whether to automatically advance to new events
  mutable std::mutex events_mutex_;
};

enum class PlaybackState {
  PAUSED,
  PLAYING,
  STEP_MODE
};

class SplitScreenUI {
public:
  SplitScreenUI();
  ~SplitScreenUI();
  
  bool initialize();
  void cleanup();
  void handleResize();
  
  void updateBookDisplay(const BookEvent* event, const OrderStateManager* order_state = nullptr);
  void updateEventLog(const BookEvent* event);
  void updateEventLogWindow(const EventStore& store); // Show events around current position
  void updateStatusBar(const EventStore& store, PlaybackState state, double speed);
  
  void refresh();
  int getInput();
  
  void showHelp();
  std::string showGotoDialog();

private:
  bool use_ncurses_;
  void initializeTextMode();
  void printBookLadderText(const std::vector<BookEvent::BookLevel>& asks,
                          const std::vector<BookEvent::BookLevel>& bids);
  void printStatusText(const EventStore& store, PlaybackState state, double speed);
  int getTextInput();
  
private:
  WINDOW* book_win_;
  WINDOW* event_win_;
  WINDOW* status_win_;
  
  int screen_height_;
  int screen_width_;
  
  std::deque<std::string> event_log_;
  static const int MAX_EVENT_LOG_SIZE = 100;
  
  void drawBookLadder(const std::vector<BookEvent::BookLevel>& asks,
                     const std::vector<BookEvent::BookLevel>& bids,
                     const BookEvent* event = nullptr,
                     const OrderStateManager* order_state = nullptr);
  void drawEventLog();
  void drawStatusBar(const std::string& status);
  
  void initializeColors();
};

class TimeNavigator {
public:
  TimeNavigator(EventStore* store) 
    : store_(store), 
      playback_state_(PlaybackState::PAUSED),
      playback_speed_(1.0),
      last_step_time_(Clock::now()) {}

  // Initialize in paused state
  void initialize() { playback_state_ = PlaybackState::PAUSED; }
  
  void setPlaybackSpeed(double speed) { playback_speed_ = speed; }
  double getPlaybackSpeed() const { return playback_speed_; }
  
  void pause() { playback_state_ = PlaybackState::PAUSED; }
  void play() { playback_state_ = PlaybackState::PLAYING; }
  void stepMode() { playback_state_ = PlaybackState::STEP_MODE; }
  
  PlaybackState getState() const { return playback_state_; }
  
  bool shouldStep();
  bool stepForward() { return store_->stepForward(); }
  bool stepBackward() { return store_->stepBackward(); }
  
  void jumpToBeginning() { store_->jumpToIndex(0); }
  void jumpToEnd() { store_->jumpToIndex(store_->getEventCount() - 1); }
  void jumpToTime(std::chrono::time_point<Clock> target_time) { 
    store_->jumpToTime(target_time); 
  }

private:
  EventStore* store_;
  PlaybackState playback_state_;
  double playback_speed_;
  std::chrono::time_point<Clock> last_step_time_;
};

class LiveMsgPrinter : public BookListener, public MDListener {
public:
  LiveMsgPrinter(bool time_fullstr,
                 int print_n_levels,
                 BookId book_id,
                 const BookManager* book_manager,
                 int max_events = 10000000,
                 const std::string& orders_file = "",
                 const std::string& target_symbol = "")
      : time_fullstr_(time_fullstr),
        print_n_levels_(print_n_levels),
        first_tick_(true),
        our_book_(nullptr),
        book_id_(book_id),
        bm_(book_manager),
        event_store_(max_events),
        navigator_(&event_store_),
        ui_(),
        orders_file_(orders_file),
        target_symbol_(target_symbol),
        order_events_index_(0),
        last_order_state_index_(SIZE_MAX),
        display_anchor_t_(pktrade::GlobalVar::start_t_),
        display_anchor_pending_(true) {};

  EventLoop* loop = nullptr;
  
  bool initialize();
  void run();
  void cleanup();
  
  void onLevelUpdate(const LevelBook& book, const LevelAdd&) override;
  void onLevelUpdate(const LevelBook& book, const LevelModify&) override;
  void onLevelUpdate(const LevelBook& book, const LevelDelete&) override;
  void onLevelUpdate(const LevelBook& book, const Trade&) override;
  void onFinal(const LevelBook& book) override;
  
  void onMD(const mdmsg::PbMessage& msg) override;
  void onLastLine(const std::string& line) override;
  
  bool outOfBounds();

private:
  bool time_fullstr_;
  int print_n_levels_;
  bool first_tick_;
  const LevelBook* our_book_;
  BookId book_id_;
  const BookManager* bm_;
  
  EventStore event_store_;
  TimeNavigator navigator_;
  SplitScreenUI ui_;
  
  void captureBookSnapshot(BookEvent& event, const LevelBook& book);
  void addEventToStore(EventType type, const std::string& description,
                      const std::variant<BookEvent::LevelData, BookEvent::TradeData, BookEvent::OrderEventData>& data);

  // Order file handling
  void loadOrdersFile();
  void insertPendingOrderEvents(int64_t current_timestamp_ms);
  void rebuildOrderStateForIndex(size_t target_index);
  void applyOrderEventToState(const BookEvent& event);
  void anchorDisplayIfNeeded(const BookEvent& event);

  bool handleKeyboardInput();
  void updateDisplay();

  bool running_;

  // Order-related members
  std::string orders_file_;
  std::string target_symbol_;                    // Symbol to filter orders by
  std::vector<BookEvent> pending_order_events_;  // Pre-parsed order events sorted by timestamp
  size_t order_events_index_;                    // Next order event to insert
  OrderStateManager order_state_;                // Current live orders state
  size_t last_order_state_index_;                // Last event index for which order state was computed

  // Display starts parked at --start, without dropping earlier timeline events.
  std::chrono::time_point<Clock> display_anchor_t_;
  bool display_anchor_pending_;
};
