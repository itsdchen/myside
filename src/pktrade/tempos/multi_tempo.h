#pragma once

#include "base_tempo.h"
#include "pktrade/enums/basic_enums.h"

#include <map>

/*
    Essentially, a thing that listens to multiple tempos and adds things up.
    When we cross the threshold, we deliver a callback.

*/

namespace pktrade::tempos {

class MultiTempo : public BaseTempo, public pktrade::tempos::TempoListener {
 public:
  MultiTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  std::vector<pktrade::BookId> getBookIds() override;

  void onTempo(int tempo_id);

 protected:
  std::map<std::string, double> tempo_names_to_weights_;
  std::map<int, double> tempo_ids_to_weights_;

  double threshold_ = 1.0;
  double running_sum_ = 0.0;

}; // MultiTempo

}; // namespace pktrade::tempos
