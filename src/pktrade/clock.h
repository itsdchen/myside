#pragma once

#include <chrono>

namespace pktrade {

class Clock {
 public:
  using duration = std::chrono::system_clock::duration;
  using rep = duration::rep;
  using period = duration::period;
  using time_point = std::chrono::time_point<Clock>;

  static constexpr bool is_steady = true;

  static Clock& instance() {
    static Clock clock;
    return clock;
  }

  static time_point now() noexcept {
    auto& clock = instance();
    return time_point{clock.time_since_epoch};
  }

  static void set(time_point tp) {
    auto& clock = instance();
    clock.time_since_epoch = tp.time_since_epoch();
  }

  static time_point from_time_t(std::time_t tt) { return time_point{std::chrono::seconds{tt}}; }

  static std::time_t to_time_t(time_point t) {
    return std::chrono::duration_cast<std::chrono::seconds>(t.time_since_epoch()).count();
  }

  Clock(const Clock&) = delete;
  Clock& operator=(const Clock&) = delete;

 private:
  Clock() = default;

  duration time_since_epoch{};
};

using TimePoint = Clock::time_point;

template <typename Dest, typename Source, typename Duration>
auto clock_cast(const std::chrono::time_point<Source, Duration>& tp) {
  return std::chrono::time_point<Dest, Duration>{
      std::chrono::duration_cast<Duration>(tp.time_since_epoch())};
}

} // namespace pktrade
