#pragma once

#include "base_tempo.h"
#include <rapidjson/document.h>
#include <set>
#include <vector>

/*
Like the featurefactory, just different

*/

namespace pktrade::tempos {

class BaseTempo;

class TempoFactory {
 public:
  TempoFactory(){};

  BaseTempo* getByName(std::string name);
  BaseTempo* getOrMakeByConf(const rapidjson::Value& conf);

  // 1.
  void makeAllTempos(const rapidjson::Value& tempos_array);
  BaseTempo* getOrMakeNamedTempo(const rapidjson::Value& tempos_array, std::string name);

  std::vector<pktrade::BookId> getBookIds();

  void subscribeAll();

 private:
  // Not sure if this will be useful.
  // Guess we'll see.
  std::map<int, BaseTempo*> tempo_stash_;
  std::map<std::string, int> name_to_id_;

  // incr as
  int cur_index_ = 0;
};

} // namespace pktrade::tempos
