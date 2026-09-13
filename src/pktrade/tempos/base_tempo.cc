#include "base_tempo.h"
#include <algorithm>

namespace pktrade::tempos {

BaseTempo::BaseTempo(int id, std::string name) : id_(id), name_(name) {}

std::string BaseTempo::getName() const { return name_; }

int BaseTempo::getID() const { return id_; }

void BaseTempo::addListener(TempoListener* listener) {
  // Add if not repeated.
  if (std::find(listeners_.begin(), listeners_.end(), listener) == listeners_.end()) {
    listeners_.push_back(listener);
  }
}

void BaseTempo::fire() {
  for (TempoListener* listener : listeners_) {
    listener->onTempo(id_);
  }
}

} // namespace pktrade::tempos
