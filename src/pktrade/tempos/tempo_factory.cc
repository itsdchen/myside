#include "tempo_factory.h"

#include "exec_tempo.h"
#include "file_tempo.h"
#include "final_tempo.h"
#include "impt_tempo.h"
#include "inside_tempo.h"
#include "msg_tempo.h"
#include "or_tempo.h"
#include "pktrade/context/global_vars.h"
#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/util/basiclib.h"
#include "time_tempo.h"

namespace pktrade::tempos {

BaseTempo* TempoFactory::getByName(std::string name) {
  std::map<std::string, int>::iterator idx = name_to_id_.find(name);

  if (idx == name_to_id_.end()) {
    return nullptr;
  }
  return tempo_stash_[idx->second];
}

BaseTempo* TempoFactory::getOrMakeByConf(const rapidjson::Value& conf) {
  std::string name = conf["name"].GetString();
  std::string tempo_type = conf["type"].GetString();

  BaseTempo* tempo = nullptr;
  for (std::pair<int, BaseTempo*> tempo_pair : tempo_stash_) {
    if (tempo_pair.second->isSame(conf, this)) {
      if (name_to_id_.find(name) == name_to_id_.end()) {
        name_to_id_[name] = tempo_pair.first;
      }
      return tempo_stash_[tempo_pair.first];
    }
  }

  // Did not find, create.
  if (tempo_type == "MsgTempo") {
    // UNCHECKED
    tempo = new MsgTempo(conf, cur_index_++, this);
  } else if (tempo_type == "TimeTempo") {
    // CHECKED.
    tempo = new TimeTempo(conf, cur_index_++, this);
  } else if (tempo_type == "ImptTempo") {
    tempo = new ImptTempo(conf, cur_index_++, this);
  } else if (tempo_type == "InsideTempo") {
    tempo = new InsideTempo(conf, cur_index_++, this);
  } else if (tempo_type == "FinalTempo") {
    // CHECKED
    tempo = new FinalTempo(conf, cur_index_++, this);
  } else if (tempo_type == "FileTempo") {
    // CHECKED
    tempo = new FileTempo(conf, cur_index_++, this);
  } else if (tempo_type == "ExecTempo") {
    tempo = new ExecTempo(conf, cur_index_++, this);
  } else if (tempo_type == "OrTempo") {
    tempo = new OrTempo(conf, cur_index_++, this);
  } else {
    throw std::runtime_error("TempoFactory doesn't support type " + tempo_type);
  }

  // Store records.
  tempo_stash_[tempo->getID()] = tempo;
  name_to_id_[tempo->getName()] = tempo->getID();
  return tempo;
}

// This "conf" should be an array of
void TempoFactory::makeAllTempos(const rapidjson::Value& tempos_array) {
  std::vector<std::string> all_names;
  // Go through the values, get the names, iteratively call gomnts.
  for (const rapidjson::Value& one_tempo_conf : tempos_array.GetArray()) {
    all_names.push_back(one_tempo_conf["name"].GetString());
  }

  // Then, call the gomNTs.
  for (std::string name : all_names) {
    getOrMakeNamedTempo(tempos_array, name);
  }
}

BaseTempo* TempoFactory::getOrMakeNamedTempo(const rapidjson::Value& tempos_array,
                                             std::string name) {
  for (const rapidjson::Value& one_tempo_conf : tempos_array.GetArray()) {
    if (one_tempo_conf["name"].GetString() == name) {
      return getOrMakeByConf(one_tempo_conf);
    }
  }
  return nullptr;
}

void TempoFactory::subscribeAll() {
  for (const std::pair<int, BaseTempo*> tempo_pair : tempo_stash_) {
    tempo_pair.second->subscribeData(pktrade::GlobalVar::md_beacon_);
  }
}

std::vector<pktrade::BookId> TempoFactory::getBookIds() {
  std::set<pktrade::BookId> ids;
  // Add from all tempos
  for (const std::pair<int, BaseTempo*> tempo_pair : tempo_stash_) {
    std::vector<pktrade::BookId> tempo_bookids = tempo_pair.second->getBookIds();
    ids.insert(tempo_bookids.begin(), tempo_bookids.end());
  }
  std::vector<pktrade::BookId> ids_vec(ids.begin(), ids.end());
  return ids_vec;
}

} // namespace pktrade::tempos
