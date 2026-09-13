#pragma once

#include <functional>
#include <optional>
#include <vector>

#include "pktrade/clock.h"
#include "pktrade/timer.h"

namespace pktrade {

class Pollable;

// A PollResult contains a pointer to a Pollable object and the next event time
// on that object if a Pollable has an event to dispatch. A PollResult is
// comparable and considered smaller if its event time is earlier.
//
// An empty PollResult object (default constructed) means that the Pollable
// object has no events to dispatch.
class PollResult final {
 public:
  using function_t = std::function<void()>;

  // This sentinel value signals to the event loop to unregister the pollable
  // object associated with this result as no further poll events will be
  // emitted.
  static PollResult done() {
    return PollResult{TimePoint::max(), [] {}};
  }

  // Constructs an empty PollResult object
  PollResult() = default;

  // Constructs a PollResult object with a task.
  PollResult(TimePoint time, const function_t& task) : time_{time}, task_{task} {}

  // Returns true if the pollable object has a task
  explicit operator bool() const { return static_cast<bool>(task_); }

  void operator()() {
    Clock::set(time_);
    task_();
  }

  TimePoint time() const noexcept { return time_; }

 private:
  TimePoint time_;
  function_t task_;
};

inline bool operator<(const PollResult& lhs, const PollResult& rhs) {
  return lhs.time() < rhs.time();
}

class Pollable {
 public:
  virtual ~Pollable() = default;
  virtual PollResult poll() = 0;
};

class Poller {
 public:
  virtual ~Poller() = default;

  virtual void dispatch(const std::vector<Pollable*>& pollables, Timer<Clock>& timer) = 0;
};

class SimPoller : public Poller {
 public:
  void dispatch(const std::vector<Pollable*>& pollables, Timer<Clock>& timer) override;
};

class LivePoller : public Poller {
  void dispatch(const std::vector<Pollable*>& pollables, Timer<Clock>& timer) override;
};

} // namespace pktrade
