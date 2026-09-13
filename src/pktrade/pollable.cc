#include "pktrade/pollable.h"

#include <algorithm>

#include "pktrade/util/time_utils.h"
#include <glog/logging.h>

namespace pktrade {

void SimPoller::dispatch(const std::vector<Pollable*>& pollables, Timer<Clock>& timer) {
  // Invoke the earliest PollResult object if it has a task to perform.
  // Otherwise, do nothing. We do this by looping through the list of pollables
  // and timer, and remembering the earliest task we encounter.
  std::optional<PollResult> earliest_task;

  std::ranges::for_each(pollables, [&](Pollable* p) {
    if (auto r = p->poll()) {
      if (!earliest_task || r.time() < earliest_task->time()) {
        earliest_task = r;
      }
    }
  });

  if (timer.nextTimeout().has_value() &&
      (!earliest_task || timer.nextTimeout().value() < earliest_task->time())) {
    earliest_task = PollResult{timer.nextTimeout().value(), [&] { timer.poll(); }};
  }

  if (earliest_task) {
    (*earliest_task)();
  }
  // Are we... setting the clock?
}

void LivePoller::dispatch(const std::vector<Pollable*>& pollables, Timer<Clock>& timer) {
  if (timer.nextTimeout().has_value() && Clock::now() > timer.nextTimeout().value()) {
    Clock::set(timer.nextTimeout().value());
    timer.poll();
    return;
  }

  for (auto* p : pollables) {
    auto poll_result = p->poll();
    if (poll_result) {
      poll_result();
      return;
    }
  }

  Clock::set(clock_cast<Clock>(std::chrono::system_clock::now()));
}

} // namespace pktrade
