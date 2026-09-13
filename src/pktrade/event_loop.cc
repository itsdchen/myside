#include "pktrade/event_loop.h"

#include "pktrade/util/time_utils.h"
#include <glog/logging.h>

namespace pktrade {

volatile std::sig_atomic_t EventLoop::stop_flag_ = false;

void EventLoop::setPoller(std::unique_ptr<Poller> poller) {
  poller_ = CHECK_NOTNULL(std::move(poller));
}

void EventLoop::stop() {
  LOG(INFO) << "stopping event loop";
  stop_flag_ = true;
}

void EventLoop::runOnce() { poller_->dispatch(pollables_, timer_); }

void EventLoop::runUntil(TimePoint t) {
  Clock::set(TimePoint{});
  // Register signal handler for exiting event loop

  // This used to be here, but I'm stopping it so I can handle SIGINT at a higher level.
  // I don't think I need this guy to catch important signals.
  // std::signal(SIGINT, handle_signal);

  // LOG(INFO) << "starting event loop";

  while (!stop_flag_ && Clock::now() < t) {
    runOnce();
    ++event_count_;
  }

  // LOG(INFO) << "stopped event loop";

  // Deregister the signal handler that was registered above. Now that the
  // event loop has stopped, We don't want to keep this signal handler around
  // anymore to swallow subsequent SIGINT signals.
  // std::signal(SIGINT, SIG_DFL);
}

void EventLoop::run() { runUntil(TimePoint::max()); }

} // namespace pktrade
