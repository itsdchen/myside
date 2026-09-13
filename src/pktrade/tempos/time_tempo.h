#pragma once

#include "base_tempo.h"

namespace pktrade::tempos {

class TimeTempo : public BaseTempo {
 public:
  TimeTempo(const rapidjson::Value& conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_conf, TempoFactory* tf) override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  std::vector<pktrade::BookId> getBookIds() override;

  void onTimer();

 protected:
  // This should translate to
  std::chrono::milliseconds ms_between_fire_;
  // double secs_between_fire_;
};
} // namespace pktrade::tempos
