#pragma once

#include "pktrade/signals/signal.h"

namespace pktrade::signals {

// Exponential moving sum of mid-price returns.
// On each mid update: return = (mid - last_mid) / last_mid
// value = value * exp(-dt / time_const) + return
class SigRetEms : public Signal, public SignalListener {
 public:
  SigRetEms(int signal_id,
            const rapidjson::Value& sig_conf,
            SignalFactory* sf,
            const rapidjson::Value& all_signals_array);

  void subscribeData(pktrade::md::MDBeacon* beacon) override {};

  bool isSame(const rapidjson::Value& other_conf,
              SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override { return {}; }

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  void reset() override;

 protected:
  Signal* mid_signal_;
  int mid_id_;

  double last_mid_;
  double time_const_;  // seconds
  int64_t last_t_;
};

}  // namespace pktrade::signals
