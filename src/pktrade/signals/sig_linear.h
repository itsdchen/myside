#pragma once

#include "pktrade/signals/signal.h"

namespace pktrade::signals {

class SigLinear : public Signal, public SignalListener {
 public:
  SigLinear(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf,
            const rapidjson::Value& all_signals_array);

  void subscribeData(pktrade::md::MDBeacon* beacon) override {};
  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override { return {}; }

  // SignalListener callbacks.
  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  // After hearing from my children, let's... calc our value, yeah?

  void fullRecalc();

  void reset() override;

  // For debugging purposes. Print out all its components and values.
  void fullPrint() override;
  std::string fullDebugString() override;

 protected:
  std::unordered_map<int, int> sig_id_to_idx_;
  // In case we need a forced recalc.
  std::vector<Signal*> child_signals_;

  std::vector<bool> sig_validity_;
  std::vector<double> sig_coef_;
  std::vector<double> sig_vals_;

  // If nonzero, apply bounds.
  std::vector<double> sig_bounds_;

  double offset_;

  // min_validity?
  // sig_bounds? Might want to have it.
};

} // namespace pktrade::signals
