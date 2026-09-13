#pragma once

#include <assert.h>

#include <chrono>
#include <functional>
#include <iostream>
#include <optional>
#include <queue>
#include <unordered_set>
#include <utility>
#include <vector>

/*
Note (since I don't fully understand all this):

If multiple threads are creating callbacks with the timer, we can encounter a race condition. If we
see a random corruption issue upstream of set(), consider making the parent process thread-safe. I'm
not going to introduce locks here, I guess, because it feels a lot more universal.

*/

namespace pktrade {

class BaseTimer {
 public:
  static uint64_t next_handle_id;
};

class TimerHandle {
 public:
  // A handle to a timer callback event. Is used to check whether the callback
  // is still valid (via operator bool()) or cancel the callback.
  TimerHandle(uint64_t handle_id, std::unordered_set<uint64_t>& active_ids)
      : handle_id_{handle_id}, active_ids_{active_ids} {}

  bool operator==(const TimerHandle& other) const { return handle_id_ == other.handle_id_; }

  explicit operator bool() const { return active_ids_.find(handle_id_) != active_ids_.end(); }

  // Cancels the callback that is associated with this timer handle. Returns
  // true if the callback was successfully canceled. Otherwise, this is a
  // no-op.
  bool cancel() { return active_ids_.erase(handle_id_); }

 private:
  uint64_t handle_id_;
  std::unordered_set<uint64_t>& active_ids_;
};

template <typename Clock>
class Timer : private BaseTimer {
  struct TimerInfo {
    uint64_t timer_info_id;
    typename Clock::time_point times_up;
    std::function<void()> cb;
  };

  struct TimerInfoComparator {
    bool operator()(const TimerInfo& a, const TimerInfo& b) {
      return std::tie(a.times_up, b.timer_info_id) > std::tie(b.times_up, b.timer_info_id);
    }
  };

 public:
  // Set a callback event elapsed time into the future. Returns a TimerHandle
  // object which can be used to check the state of the callback event, or to
  // cancel the callback event before it is triggered.
  template <typename F>
  TimerHandle set(typename Clock::duration elapsed, F&& f) {
    TimerInfo info = {
        .timer_info_id = next_handle_id++,
        .times_up = Clock::now() + elapsed,
        .cb = std::forward<F>(f),
    };

    queue_.emplace(info);
    active_ids_.insert(info.timer_info_id);

    return {info.timer_info_id, active_ids_};
  }

  // Start invoking callback events that are past due in chronological order.
  void poll() {
    while (!queue_.empty() && queue_.top().times_up <= Clock::now()) {
      pop();
    }
  }

  // Invoke the next callback that is past due.
  void pollOnce() {
    if (!queue_.empty() && queue_.top().times_up <= Clock::now()) {
      pop();
    }
  }

  // Returns the next scheduled callback time if it exists.
  std::optional<typename Clock::time_point> nextTimeout() const {
    if (queue_.empty()) {
      return std::nullopt;
    }
    return queue_.top().times_up;
  }

  void reset() {
    queue_ = {};
    active_ids_.clear();
  }

 private:
  void pop() {
    assert(!queue_.empty());
    auto& top = queue_.top();
    if (active_ids_.erase(top.timer_info_id)) {
      top.cb();
    }
    queue_.pop();
  }

  std::priority_queue<TimerInfo, std::vector<TimerInfo>, TimerInfoComparator> queue_;
  std::unordered_set<uint64_t> active_ids_;
};

} // namespace pktrade
