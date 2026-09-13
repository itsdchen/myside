#include "sig_libra.h"

#include "pktrade/util/time_utils.h"
namespace pktrade::signals {

SigLibra::SigLibra(int signal_id,
                   const rapidjson::Value& sig_conf,
                   SignalFactory* sf,
                   const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf), a_buffer_(0), b_buffer_(0) {
  sig_a_ = sf->getOrMakeNamedSignal(all_signals_array,
                                    sig_conf["sig_a"].GetString());
  if (sig_a_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing sig_a ") +
                             sig_conf["sig_a"].GetString() + " in SigLibra");
  }

  sig_a_->addListener(this);
  sig_a_id_ = sig_a_->getID();
  sig_a_val_ = 0;

  sig_b_ = sf->getOrMakeNamedSignal(all_signals_array,
                                    sig_conf["sig_b"].GetString());
  if (sig_b_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing sig_b ") +
                             sig_conf["sig_b"].GetString() + " in SigLibra");
  }
  sig_b_->addListener(this);
  sig_b_id_ = sig_b_->getID();
  sig_b_val_ = 0;

  max_impulse_ = sig_conf["max_impulse"].GetDouble();
  max_bound_mult_ = 50;
  if (sig_conf.HasMember("max_bound_mult")) {
    max_bound_mult_ = sig_conf["max_bound_mult"].GetDouble();
  }
  bound_ = max_impulse_ * max_bound_mult_;

  // For the final combination
  sig_a_scale_ = sig_conf["sig_a_scale"].GetDouble();
  sig_b_scale_ = sig_conf["sig_b_scale"].GetDouble();

  // For self-elimination
  sig_a_selfscale_ = sig_conf["sig_a_selfscale"].GetDouble();
  sig_b_selfscale_ = sig_conf["sig_b_selfscale"].GetDouble();

  // These numbers need to be bounded by 1.01 or something. Something to
  // encourage self-elimination.
  sig_a_selfscale_ = std::max(sig_a_selfscale_, 1.01);
  sig_b_selfscale_ = std::max(sig_b_selfscale_, 1.01);

  // It should also be bounded below...
  sig_a_selfscale_ = std::min(sig_a_selfscale_, 5.0);
  sig_b_selfscale_ = std::min(sig_b_selfscale_, 5.0);

  // For mutual elimination.
  ab_elim_scale_ = sig_conf["ab_elim_scale"].GetDouble();
  ab_elim_diff_scale_ = sig_conf["ab_elim_diff_scale"].GetDouble();

  // Bound these by  1/10 and 10 too.
  ab_elim_scale_ = std::max(ab_elim_scale_, 0.1);
  ab_elim_scale_ = std::min(ab_elim_scale_, 10.);

  ab_elim_diff_scale_ = std::max(ab_elim_diff_scale_, 0.1);
  ab_elim_diff_scale_ = std::min(ab_elim_diff_scale_, 10.);

  apply_basic_decay_ = sig_conf["apply_basic_decay"].GetBool();
  tick_decay_ = sig_conf["tick_decay"].GetDouble();
  time_decay_ = sig_conf["time_decay"].GetDouble();
  last_decay_t_ = time_utils::nowToMs();

  output_sigmoid_ = false;
  if (sig_conf.HasMember("output_sigmoid")) {
    output_sigmoid_ = sig_conf["output_sigmoid"].GetBool();
    output_sigmoid_denom_ = sig_conf["output_sigmoid_denom"].GetDouble();
  }

  value_ = 0;

  // sanity check...
  if (ab_elim_scale_ < 0) {
    throw std::runtime_error("ab_elim_scale_ must be >= 0");
  }

  if (ab_elim_diff_scale_ < 0) {
    throw std::runtime_error("ab_elim_diff_scale_ must be >= 0");
  }
}

bool SigLibra::isSame(const rapidjson::Value& other_conf,
                      SignalFactory& sf,
                      const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigLibra") {
    return false;
  }

  // Check signals are the same
  Signal* other_sig_a = sf.getOrMakeNamedSignal(
      all_signals_array, other_conf["sig_a"].GetString());
  if (other_sig_a != sig_a_) {
    return false;
  }

  Signal* other_sig_b = sf.getOrMakeNamedSignal(
      all_signals_array, other_conf["sig_b"].GetString());
  if (other_sig_b != sig_b_) {
    return false;
  }

  // Check the params are the same.

  if (max_impulse_ != other_conf["max_impulse"].GetDouble()) {
    return false;
  }
  if (other_conf.HasMember("max_bound_mult")) {
    if (max_bound_mult_ != other_conf["max_bound_mult"].GetDouble())
      return false;
  }

  if (sig_a_scale_ != other_conf["sig_a_scale"].GetDouble()) {
    return false;
  }
  if (sig_b_scale_ != other_conf["sig_b_scale"].GetDouble()) {
    return false;
  }

  if (sig_a_selfscale_ != other_conf["sig_a_selfscale"].GetDouble()) {
    return false;
  }
  if (sig_b_selfscale_ != other_conf["sig_b_selfscale"].GetDouble()) {
    return false;
  }

  if (ab_elim_scale_ != other_conf["ab_elim_scale"].GetDouble()) {
    return false;
  }
  if (ab_elim_diff_scale_ != other_conf["ab_elim_diff_scale"].GetDouble()) {
    return false;
  }

  if (apply_basic_decay_ != other_conf["apply_basic_decay"].GetBool()) {
    return false;
  }

  if (tick_decay_ != other_conf["tick_decay"].GetDouble()) {
    return false;
  }
  if (other_conf.HasMember("output_sigmoid")) {
    if (output_sigmoid_ != other_conf["output_sigmoid"].GetBool()) {
      return false;
    }
  }

  if (time_decay_ != other_conf["time_decay"].GetDouble()) {
    return false;
  }

  return true;
}

void SigLibra::basicDecay() {
  // Tick decay
  double dec = tick_decay_;
  // Time decay
  dec *=
      std::exp(-(time_utils::nowToMs() - last_decay_t_) / 1000. / time_decay_);

  last_decay_t_ = time_utils::nowToMs();

  // Apply it.
  a_buffer_ *= dec;
  b_buffer_ *= dec;
}

// This is for mutual elimination.
void SigLibra::tryEliminate() {
  // Check for same sign, perform ab elimination.
  if (a_buffer_ * b_buffer_ > 0) {
    // eg. if a = 1, b = 10, scale = 2, then
    // in elimination, mult a by 2.
    // double ab_elim_scale_;

    double a_elim_amt =
        std::min(std::abs(a_buffer_), std::abs(b_buffer_ / ab_elim_scale_));

    a_elim_amt = copysign(a_elim_amt, a_buffer_);

    if (std::abs(a_elim_amt) > 1e-8) {
      //      printf("TRYELIMINATE: a_amt %f b_amt %f buffers %f %f \n",
      //        a_elim_amt, a_elim_amt * ab_elim_scale_, a_buffer_, b_buffer_
      //      );
    }

    // Eliminate from a.
    a_buffer_ -= a_elim_amt;

    // Eliminate from b.
    b_buffer_ -= a_elim_amt * ab_elim_scale_;
  } else if (ab_elim_diff_scale_ != 0 && a_buffer_ * b_buffer_ < 0) {
    // Use the other elimination scheme.
    double a_elim_amt = std::min(std::abs(a_buffer_),
                                 std::abs(b_buffer_ / ab_elim_diff_scale_));

    a_elim_amt = copysign(a_elim_amt, a_buffer_);

    a_buffer_ -= a_elim_amt;

    // Eliminate from b.
    b_buffer_ += a_elim_amt * ab_elim_diff_scale_;
  }
}

// I'm sorry.
void SigLibra::onSignalValue(int sig_id, double value) {
  if (apply_basic_decay_) {
    basicDecay();
  }

  if (sig_id == sig_a_id_) {
    // Quick exit if first tick.
    if (sig_a_val_ == 0) {
      sig_a_val_ = value;
      return;
    }
    double impulse = value / sig_a_val_ - 1;
    // printf("**** STARTA ******\n");

    /*
        printf("Sig_a: val %f oldval %f impulse %f buf %f\n",
          value,
          sig_a_val_,
          impulse,
          a_buffer_
        );
        */

    sig_a_val_ = value;

    // Bound the impulse. This is easier than copysign.
    if (impulse > 0) {
      impulse = std::min(impulse, max_impulse_);
    } else {
      impulse = std::max(impulse, -max_impulse_);
    }

    // This has got to be bounded by 1.
    // if (abs(impulse) > 1) {
    //  impulse = copysign(1, impulse);
    //}

    // Check for self elimination.
    if (impulse * a_buffer_ < 0) {
      //  Do self elimination.
      double elim_amt =
          std::min(std::abs(impulse), std::abs(a_buffer_ / sig_a_selfscale_));

      //  printf("A elimination: imp %f a_buf %f elim_amt %f \n", impulse,
      //  a_buffer_, elim_amt);

      elim_amt = copysign(elim_amt, impulse);
      impulse -= elim_amt;
      // printf("Eliminated %f from impulse, now %f\n", elim_amt, impulse);
      //  Since the premise is that impulse and a_buffer have opposite signs,
      //  we want to subtract elim_amt from impulse and add it to a_buffer.
      a_buffer_ += elim_amt * sig_a_selfscale_;
      // printf("Eliminated %f * %f from a_buffer, now %f\n", elim_amt,
      // sig_a_selfscale_, a_buffer_);
    }

    a_buffer_ += impulse;

    a_buffer_ = std::max(-bound_, std::min(bound_, a_buffer_));

    //  printf("a_buffer buffer now %f \n", a_buffer_);

    //    printf("**** ENDA ******\n");
    //  Check for reconciliations.
  } else if (sig_id == sig_b_id_) {
    // sig_b update.
    // Do the same thing but reversed.

    if (sig_b_val_ == 0) {
      sig_b_val_ = value;
      return;
    }
    double impulse = value / sig_b_val_ - 1;

    /*
      printf("**** STARTB ******\n");
      printf("Sig_b: val %f oldval %f impulse %f buf %f\n",
        value,
        sig_b_val_,
        impulse,
        b_buffer_
      );

  */
    sig_b_val_ = value;
    // Bound the impulse. This is easier than copysign.
    if (impulse > 0) {
      impulse = std::min(impulse, max_impulse_);
    } else {
      impulse = std::max(impulse, -max_impulse_);
    }

    // This has got to be bounded by 1.
    // if (abs(impulse) > 1) {
    //  impulse = copysign(1, impulse);
    //}

    // Check for self elimination.
    // This is pretty
    if (impulse * b_buffer_ < 0) {
      // Do self elimination.
      double elim_amt =
          std::min(std::abs(impulse), std::abs(b_buffer_ / sig_b_selfscale_));
      //    printf("B elimination: imp %f b_buf %f elim_amt %f \n", impulse,
      //    b_buffer_, elim_amt);

      elim_amt = copysign(elim_amt, impulse);
      impulse -= elim_amt;
      // Similar sign logic as above, with the a-side.
      b_buffer_ += elim_amt * sig_b_selfscale_;
    }

    b_buffer_ += impulse;
    b_buffer_ = std::max(-bound_, std::min(bound_, b_buffer_));

    // Save it for the next go.

    //     printf("**** ENDB ******\n");
  }

  tryEliminate();

  calc();
}

// Ergh.
void SigLibra::onSignalValidity(int sig_id, bool valid) {}

void SigLibra::reset() {
  sig_a_val_ = 0;
  sig_b_val_ = 0;

  a_buffer_ = 0;
  b_buffer_ = 0;
}

void SigLibra::calc() {
  // Combine the two sides.
  /*
  printf("%s : Calc happening. a_buffer buffers (%f %f) scales (%f %f) val
  %f\n", time_utils::nowToStr().c_str(), a_buffer_, b_buffer_, sig_a_scale_,
  sig_b_scale_, a_buffer_ * sig_a_scale_ + b_buffer_ * sig_b_scale_);
*/

  double val = a_buffer_ * sig_a_scale_ + b_buffer_ * sig_b_scale_;
  if (output_sigmoid_) {
    val = 1 / (1 + std::exp(-val / (output_sigmoid_denom_))) - .5;
  }

  setValue(val);
}

}  // namespace pktrade::signals
