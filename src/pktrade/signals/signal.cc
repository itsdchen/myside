#include "signal.h"
#include <algorithm>
#include <fmt/format.h>

namespace pktrade::signals {

Signal::Signal(int signal_id, const rapidjson::Value& conf, SignalFactory* sf)
    : signal_id_(signal_id) {
  name_ = conf["name"].GetString();
  // Default this to true for now.
  valid_ = true;

  if (conf.HasMember("bound")) {
    bound_ = conf["bound"].GetDouble();
  }

  debug_verbose_ = false;
  if (conf.HasMember("debug_verbose")) {
    debug_verbose_ = conf["debug_verbose"].GetBool();
  }
}

void Signal::addListener(SignalListener* listener) {
  if (std::find(listeners_.begin(), listeners_.end(), listener) == listeners_.end()) {
    listeners_.push_back(listener);
  }
}

void Signal::broadcastValue() {
  last_pub_value_ = value_;
  for (SignalListener* listener : listeners_) {
    // Zero out values past the bound.
    // TODO: implement where you just cap it at the bound.
    if (std::abs(value_) > bound_ && zero_out_past_bound_) {
      listener->onSignalValue(signal_id_, 0);
    } else {
      listener->onSignalValue(signal_id_, value_);
    }
  }
}

void Signal::broadcastValidity() const {
  for (SignalListener* listener : listeners_) {
    listener->onSignalValidity(signal_id_, valid_);
  }
}

double Signal::getValue() {
  if (std::abs(value_) > bound_ && zero_out_past_bound_)
    return 0;
  if (std::isnan(value_)) {
    return 0;
  }

  return value_;
}
bool Signal::getValid() const { return valid_; }
std::string Signal::getName() const { return name_; }
int Signal::getID() const { return signal_id_; }

void Signal::setValue(double value) {
  value_ = value;
  if (valid_ && std::abs(last_pub_value_ - value_) >= pub_thresh_) {
    broadcastValue();
  } else {
    //  std::cout << " Didnt pub val " << value << " last_pub_val " <<
    //  last_pub_value_ << std::endl;
  }
}

void Signal::setValidity(bool valid) {
  if (valid_ != valid) {
    valid_ = valid;
    broadcastValidity();
  }
}

void Signal::setVV(double value, bool valid) {
  if (valid != valid_) {
    setValue(value);
    setValidity(valid);
    broadcastValidity();
  } else if (valid) {
    setValue(value);
  }
}

std::string Signal::getPrimarySymbol() const {
  throw std::runtime_error(
      fmt::format("Signal::getPrimarySymbol not implemented for {}", name_).c_str());
  return "";
}

} // namespace pktrade::signals
