#include "time_tempo.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::tempos {

TimeTempo::TimeTempo(const rapidjson::Value& conf, int id, TempoFactory* tf)
    : BaseTempo(id, conf["name"].GetString()) {
  // "thresh" is shared between a bunch of types of tempos. FYI.
  // secs_between_fire_ = conf["thresh"].GetDouble();

  ms_between_fire_ = std::chrono::milliseconds((int)(conf["thresh"].GetDouble() * 1000));

  // Place initial callback for start time.
  // Tempo creation should happen after loop creation.

  // printf("Time_tempo trying to start us at %s\n",
  //        pktrade::time_utils::tpToString(pktrade::GlobalVar::start_t_).c_str());
  pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_,
                                             [&] { this->onTimer(); });
}

bool TimeTempo::isSame(const rapidjson::Value& other_conf, TempoFactory* tf) {
  if (std::string(other_conf["type"].GetString()) != "TimeTempo") {
    return false;
  }

  // Check that times are the same.
  return ms_between_fire_.count() == int(1000 * other_conf["thresh"].GetDouble());
}

void TimeTempo::subscribeData(pktrade::md::MDBeacon* beacon) {
  // zoro_nothing_happened.jpg
}

std::vector<pktrade::BookId> TimeTempo::getBookIds() { return {}; }

void TimeTempo::onTimer() {
  // Stop thyself after end time.

  // Fire! Lol.
  fire();
  if (Clock::now() >= pktrade::GlobalVar::end_t_) {
    // Stop it.
    return;
  }

  pktrade::GlobalVar::event_loop_->onTimeout(ms_between_fire_, [&] { this->onTimer(); });
}

} // namespace pktrade::tempos
