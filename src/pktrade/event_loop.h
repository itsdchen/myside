#pragma once

#include <csignal>

#include <memory>
#include <utility>
#include <vector>

#include "pktrade/clock.h"
#include "pktrade/pollable.h"
#include "pktrade/timer.h"

namespace pktrade {

class EventLoop final {
 public:
  EventLoop() = default;

  // Disallow copy/assign
  EventLoop(EventLoop&) = delete;
  EventLoop& operator=(EventLoop&) = delete;

  void setPoller(std::unique_ptr<Poller> poller);

  template <typename... Args>
  void registerPollables(Args&&... args) {
    (pollables_.emplace_back(&args), ...);
  }

  void stop();

  void runOnce();

  void runUntil(TimePoint t);

  void run();

  const uint64_t getEventCount() const { return event_count_; }

  // Registers a callback to be invoked after a specified duration
  template <typename F>
  TimerHandle onTimeout(TimePoint::duration delay, F&& f) {
    return timer_.set(delay, std::forward<F>(f));
  }

  // Registers a callback to be invoked at a specified time
  template <typename F>
  TimerHandle onTimeout(TimePoint when, F&& f) {
    return timer_.set(when - Clock::now(), std::forward<F>(f));
  }

  // Registers a callback to be invoked immediately (as soon as the next poll
  // cycle)
  template <typename F>
  TimerHandle onTimeout(F&& f) {
    return timer_.set(TimePoint::duration{0}, std::forward<F>(f));
  }

 private:
  static volatile std::sig_atomic_t stop_flag_;

  static void handle_signal(int) { stop_flag_ = true; }

  std::unique_ptr<Poller> poller_;
  std::vector<Pollable*> pollables_;
  Timer<Clock> timer_;

  uint64_t event_count_ = 0;
};

} // namespace pktrade
