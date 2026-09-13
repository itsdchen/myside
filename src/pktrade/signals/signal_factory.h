#pragma once

#include "signal.h"
#include <rapidjson/document.h>
#include <set>
#include <vector>

/*

Contains and creates Signals.


*/

namespace pktrade::signals {
class Signal;

class SignalFactory {
 public:
  SignalFactory(){};

  Signal* getByName(std::string name);

  Signal* getOrMakeByConf(const rapidjson::Value& conf, const rapidjson::Value& all_signals_array);
  void makeAllSignals(const rapidjson::Value& all_signals_array);
  Signal* getOrMakeNamedSignal(const rapidjson::Value& all_signals_array, std::string name);

  std::vector<pktrade::BookId> getBookIds();

  std::vector<Signal*> getAllSignals();

  void subscribeAll();

 private:
  std::map<int, Signal*> signal_stash_;
  std::map<std::string, int> name_to_id_;

  int curIndex_ = 0;

  // Use this to cache all_signals?
  // Just leave it in vestigial-like for now, and figure it out semi-later.
  rapidjson::Document all_signals_backup_;
};

}; // namespace pktrade::signals
