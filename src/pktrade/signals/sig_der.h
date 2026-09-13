#pragma once

#include "pktrade/signals/signal.h"

namespace pktrade::signals {

class SigDer : public Signal, public SignalListener {
 public:
  SigDer(int signal_id,
         const rapidjson::Value& sig_conf,
         SignalFactory* sf,
         const rapidjson::Value& all_signals_array);

  void subscribeData(pktrade::md::MDBeacon* beacon) override{};

  bool isSame(const rapidjson::Value& other_conf,
              SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override { return {}; }

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  void deferredDecay();

  void reset() override;

 protected:
  Signal* pred_signal_;
  Signal* tgt_signal_;

  int pred_id_;
  int tgt_id_;

  double last_pred_val_;
  double last_tgt_val_;

  // Time decay const.
  double time_decay_;
  double tick_decay_;

  double tgt_decay_agree_;
  double tgt_decay_disagree_;

  // Could be 1.
  double tgt_decay_denom_;

  double min_tick_decay_;

  double max_impulse_;

  // Cannot exceed this.
  double max_bound_mult_;
  double bound_;

  bool defer_decay_;
  std::chrono::milliseconds defer_decay_t_;

  // If we think pred_signal is returning a price,
  // we can take returns instead of diffs.
  bool take_pred_returns_;

  int64_t last_decay_t_;

  bool output_sigmoid_;
  double output_sigmoid_denom_;
  double iv_ = 0.;
};

}  // namespace pktrade::signals
