#include "sig_ret_ems.h"

#include "pktrade/util/time_utils.h"

namespace pktrade::signals {

SigRetEms::SigRetEms(int signal_id,
                     const rapidjson::Value& sig_conf,
                     SignalFactory* sf,
                     const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf) {
  mid_signal_ = sf->getOrMakeNamedSignal(all_signals_array,
                                         sig_conf["mid_signal"].GetString());
  if (mid_signal_ == nullptr) {
    throw std::runtime_error(
        std::string("SigRetEms: failed to find mid_signal ") +
        sig_conf["mid_signal"].GetString());
  }

  time_const_ = sig_conf["time_const"].GetDouble();

  mid_id_ = mid_signal_->getID();
  mid_signal_->addListener(this);

  last_mid_ = 0;
  last_t_ = 0;
}

bool SigRetEms::isSame(const rapidjson::Value& other_conf,
                       SignalFactory& sf,
                       const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigRetEms") {
    return false;
  }
  if (time_const_ != other_conf["time_const"].GetDouble()) {
    return false;
  }
  Signal* other_mid = sf.getOrMakeNamedSignal(
      all_signals_array, other_conf["mid_signal"].GetString());
  return other_mid == mid_signal_;
}

void SigRetEms::onSignalValue(int sig_id, double val) {
  int64_t now = time_utils::nowToMs();

  if (last_mid_ == 0) {
    last_mid_ = val;
    last_t_ = now;
    return;
  }

  double ret = (val - last_mid_) / std::abs(last_mid_);
  last_mid_ = val;

  double dt = (now - last_t_) / 1000.0;
  last_t_ = now;

  double decay = std::exp(-dt / time_const_);
  double new_val = value_ * decay + ret;

  setValue(new_val);
}

void SigRetEms::onSignalValidity(int sig_id, bool valid) {
  if (!valid) {
    reset();
    setValidity(false);
  } else {
    if (mid_signal_->getValid()) {
      last_mid_ = mid_signal_->getValue();
      last_t_ = time_utils::nowToMs();
      setValidity(true);
    }
  }
}

void SigRetEms::reset() {
  last_mid_ = 0;
  last_t_ = 0;
  value_ = 0;
}

}  // namespace pktrade::signals
