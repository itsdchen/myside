#include "or_tempo.h"

#include "pktrade/context/global_vars.h"

#include <magic_enum.hpp>

namespace pktrade::tempos {

OrTempo::OrTempo(const rapidjson::Value& conf, int id, TempoFactory* tf)
    : BaseTempo(id, conf["name"].GetString()) {
  // Grab the names_to_weights.
  for (const rapidjson::Value& tempo_name : conf["child_names"].GetArray()) {
    child_names_.push_back(tempo_name.GetString());
  }
}

bool OrTempo::isSame(const rapidjson::Value& other_conf, TempoFactory* tf) {
  // Check these things are the same.
  if (std::string(other_conf["type"].GetString()) != "OrTempo") {
    return false;
  }

  if (other_conf["child_names"].GetArray().Size() != child_names_.size()) {
    return false;
  }

  // Kind of stupid I guess. But let's go both ways to make sure the dicts are
  // the same. Checks that our stuff is all contained in the other.

  for (const rapidjson::Value& other_tempo_name : other_conf["child_names"].GetArray()) {
    if (std::find(child_names_.begin(), child_names_.end(), other_tempo_name.GetString()) ==
        child_names_.end()) {
      return false;
    }
  }

  return true;
}

void OrTempo::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Go through my factory and subscribe.
  for (std::string& child_name : child_names_) {
    BaseTempo* tempo = pktrade::GlobalVar::tf_->getByName(child_name);

    tempo->addListener(this);
  }
}

// One day we should make this happen, yeah.
std::vector<pktrade::BookId> OrTempo::getBookIds() { return {}; }

void OrTempo::onTempo(int tempo_id) { fire(); }

}; // namespace pktrade::tempos
