#pragma once

#include "pktrade/signals/signal.h"

namespace pktrade::signals {

class SigLibra : public Signal, public SignalListener {
 public:
  SigLibra(int signal_id,
           const rapidjson::Value& sig_conf,
           SignalFactory* sf,
           const rapidjson::Value& all_signals_array);

  void subscribeData(pktrade::md::MDBeacon* beacon) override{};

  bool isSame(const rapidjson::Value& other_conf,
              SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override { return {}; }

  void basicDecay();

  void tryEliminate();

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  void reset() override;
  void calc();

 protected:
  Signal* sig_a_;
  Signal* sig_b_;

  int sig_a_id_;
  int sig_b_id_;

  // For final prediction.
  double sig_a_scale_;
  double sig_b_scale_;

  // For self-elimination.
  double sig_a_selfscale_;

  // For now, let's just set this at 1.
  double sig_b_selfscale_;

  // For mutual elimination.
  // eg. if a = 1, b = 10, scale = 2, then
  // in elimination, mult a by 2.
  double ab_elim_scale_;
  // Potentially eliminate from a, b, even if diff signs.
  double ab_elim_diff_scale_;

  double max_impulse_;

  // Cannot exceed this.
  double max_bound_mult_;
  double bound_;

  // TODO: make this have tick and time decays.

  // By default, take returns.
  double sig_a_val_;
  double sig_b_val_;

  double a_buffer_;
  double b_buffer_;

  // OK, I'm going to be a little baby and basically turn this into
  // a conbuf too. wah wah wah.
  bool apply_basic_decay_;
  double tick_decay_;
  double time_decay_;
  int64_t last_decay_t_;

  bool output_sigmoid_;
  double output_sigmoid_denom_;

};

}  // namespace pktrade::signals
