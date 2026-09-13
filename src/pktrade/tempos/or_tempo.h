#pragma once

#include "base_tempo.h"
#include <map>

/*

Very simple tempo. Almost like a multitempo but simpler: it just subscribes to
multiple child tempos and then returns if any of its children fire.

*/

namespace pktrade::tempos {

class OrTempo : public BaseTempo, public pktrade::tempos::TempoListener {
 public:
  OrTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) override;

  void subscribeData(pktrade::md::MDBeacon* beacon) override;

  std::vector<pktrade::BookId> getBookIds() override;

  void onTempo(int tempo_id);

 protected:
  std::vector<std::string> child_names_;
};

}; // namespace pktrade::tempos
