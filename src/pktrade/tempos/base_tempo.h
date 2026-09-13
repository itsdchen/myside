#pragma once

#include "pktrade/mktdata/md_beacon.h"
#include <rapidjson/document.h>
#include <string>
#include <vector>

/*

Base class of tempos. Defines how we keep track of time, whether in eventspace
or otherwise.

*/

namespace pktrade::tempos {

class TempoFactory;

class TempoListener {
 public:
  virtual ~TempoListener() = default;

  virtual void onTempo(int tempo_id) = 0;
};

class BaseTempo {
 public:
  BaseTempo(int id, std::string name);
  virtual ~BaseTempo() = default;

  std::string getName() const;
  int getID() const;
  virtual bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) = 0;

  virtual std::vector<pktrade::BookId> getBookIds() = 0;

  // talk to the beacon. Subscribe.
  // Each subclass should decide for themselves.
  virtual void subscribeData(pktrade::md::MDBeacon* beacon) = 0;
  void addListener(TempoListener* listener);
  // Alert its listeners.
  void fire();

 protected:
  int id_;
  std::string name_;
  std::vector<TempoListener*> listeners_;
};

} // namespace pktrade::tempos
