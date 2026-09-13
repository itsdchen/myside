#include "multi_tempo.h"

#include "pktrade/context/global_vars.h"

#include <magic_enum.hpp>

namespace pktrade::tempos {

MultiTempo::MultiTempo(const rapidjson::Value& conf, int id, TempoFactory* tf)
    : BaseTempo(id, conf["name"].GetString()) {
  // Grab its threshold
  threshold_ = conf["threshold"].GetDouble();
  running_sum_ = 0.;
  // Grab the names_to_weights.
  for (const auto& tempos_to_weights : conf["tempos_to_weights"].GetObject()) {
    std::string tempo_name = tempos_to_weights.name.GetString();
    double weight = tempos_to_weights.value.GetDouble();

    // Let's allow for 0 weights for now. But negativity is deceptive, let's not
    // do that.
    if (weight < 0) {
      throw std::runtime_error("MultiTempo doesn't support negative weights.");
    }

    tempo_names_to_weights_[tempo_name] = weight;
  }
}

bool MultiTempo::isSame(const rapidjson::Value& other_conf, TempoFactory* tf) {
  // Check these things are the same.
  if (std::string(other_conf["type"].GetString()) != "MultiTempo") {
    return false;
  }

  if (threshold_ != other_conf["threshold"].GetDouble()) {
    return false;
  }

  // Kind of stupid I guess. But let's go both ways to make sure the dicts are
  // the same. Checks that our stuff is all contained in the other.
  for (auto our_name_to_weight : tempo_names_to_weights_) {
    if (!other_conf["tempos_to_weights"].HasMember(our_name_to_weight.first.c_str())) {
      return false;
    }
    if (other_conf["tempos_to_weights"][our_name_to_weight.first.c_str()].GetDouble() !=
        our_name_to_weight.second) {
      return false;
    }
  }

  // Should check the other way around too.
  for (const auto& other_tempos_to_weights : other_conf["tempos_to_weights"].GetObject()) {
    // Check key
    if (!tempo_names_to_weights_.contains(other_tempos_to_weights.name.GetString())) {
      return false;
    }

    // Check value.
    if (tempo_names_to_weights_[other_tempos_to_weights.name.GetString()] !=
        other_tempos_to_weights.value.GetDouble()) {
      return false;
    }
  }

  return true;
}

void MultiTempo::subscribeData(pktrade::md::MDBeacon* beacon) {
  // Go through my factory and subscribe.
  for (auto& tempo_name_to_weight : tempo_names_to_weights_) {
    BaseTempo* tempo = pktrade::GlobalVar::tf_->getByName(tempo_name_to_weight.first);

    // Add its weight.
    tempo_ids_to_weights_[tempo->getID()] = tempo_name_to_weight.second;
    tempo->addListener(this);
  }
}

// One day we should make this happen, yeah.
std::vector<pktrade::BookId> MultiTempo::getBookIds() { return {}; }

void MultiTempo::onTempo(int tempo_id) {
  running_sum_ += tempo_ids_to_weights_[tempo_id];
  while (running_sum_ > threshold_) {
    fire();
    running_sum_ -= threshold_;
  }
}

} // namespace pktrade::tempos
