#include "sig_der.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::signals {

SigDer::SigDer(int signal_id,
               const rapidjson::Value& sig_conf,
               SignalFactory* sf,
               const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf) {
  pred_signal_ = sf->getOrMakeNamedSignal(all_signals_array,
                                          sig_conf["pred_signal"].GetString());
  tgt_signal_ = sf->getOrMakeNamedSignal(all_signals_array,
                                         sig_conf["tgt_signal"].GetString());

  if (pred_signal_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing pred_signal ") +
                             sig_conf["pred_signal"].GetString() +
                             " in SigDer");
  }
  if (tgt_signal_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing tgt_signal ") +
                             sig_conf["tgt_signal"].GetString() + " in SigDer");
  }

  // Get the decay params.
  time_decay_ = sig_conf["time_decay"].GetDouble();
  tick_decay_ = sig_conf["tick_decay"].GetDouble();

  tgt_decay_agree_ = sig_conf["tgt_decay_agree"].GetDouble();
  tgt_decay_disagree_ = sig_conf["tgt_decay_disagree"].GetDouble();
  take_pred_returns_ = sig_conf["take_pred_returns"].GetBool();

  // Create some bounds around decays. So we don't get carried away...
  // I think this is probably sufficient.
  tgt_decay_agree_ = std::min(tgt_decay_agree_, 0.98);
  tgt_decay_disagree_ = std::min(tgt_decay_disagree_, 0.98);

  // OK. Let's do this then.

  tgt_decay_denom_ = -1;
  // TODO: implement this properly.
  if (sig_conf.HasMember("tgt_decay_denom")) {
    tgt_decay_denom_ = sig_conf["tgt_decay_denom"].GetDouble();
  }

  if (sig_conf.HasMember("min_tick_decay")) {
    min_tick_decay_ = sig_conf["min_tick_decay"].GetDouble();
  } else {
    min_tick_decay_ = 0.9999;
  }

  output_sigmoid_ = false;
  if (sig_conf.HasMember("output_sigmoid")) {
    output_sigmoid_ = sig_conf["output_sigmoid"].GetBool();
    output_sigmoid_denom_ = sig_conf["output_sigmoid_denom"].GetDouble();
  }

  defer_decay_ = sig_conf["defer_decay"].GetBool();
  defer_decay_t_ = std::chrono::milliseconds(0);

  last_pred_val_ = 0;
  last_tgt_val_ = 0;

  last_decay_t_ = time_utils::nowToMs();

  max_impulse_ = sig_conf["max_impulse"].GetDouble();

  max_bound_mult_ = 50;
  if (sig_conf.HasMember("max_bound_mult")) {
    max_bound_mult_ = sig_conf["max_bound_mult"].GetDouble();
  }
  bound_ = max_impulse_ * max_bound_mult_;

  /////////////////////////////////////////////////////////

  // Subscribe to the children.
  pred_id_ = pred_signal_->getID();
  tgt_id_ = tgt_signal_->getID();

  //  std::cout << " Yo this is the der, name is " << name_ << " , predsig is "
  //            << pred_signal_->getName() << " addy is " << pred_signal_
  //            << " id is " << pred_id_ << std::endl;

  pred_signal_->addListener(this);
  tgt_signal_->addListener(this);
}

bool SigDer::isSame(const rapidjson::Value& other_conf,
                    SignalFactory& sf,
                    const rapidjson::Value& all_signals_array) const {
  // Check parameters and child signals. If they're good, we're good.

  if (std::string(other_conf["type"].GetString()) != "SigDer") {
    return false;
  }

  if (time_decay_ != other_conf["time_decay"].GetDouble()) {
    return false;
  }
  if (tick_decay_ != other_conf["tick_decay"].GetDouble()) {
    return false;
  }
  if (tgt_decay_agree_ != other_conf["tgt_decay_agree"].GetDouble()) {
    return false;
  }
  if (tgt_decay_disagree_ != other_conf["tgt_decay_disagree"].GetDouble()) {
    return false;
  }
  if (take_pred_returns_ != other_conf["take_pred_returns"].GetBool()) {
    return false;
  }

  if (other_conf.HasMember("output_sigmoid")) {
    if (output_sigmoid_ != other_conf["output_sigmoid"].GetBool()) {
      return false;
    }
  }

  if (other_conf.HasMember("output_sigmoid_denom")) {
    if (output_sigmoid_denom_ !=
        other_conf["output_sigmoid_denom"].GetDouble()) {
      return false;
    }
  }

  if (other_conf.HasMember("tgt_decay_denom")) {
    if (tgt_decay_denom_ != other_conf["tgt_decay_denom"].GetDouble())
      return false;
  }

  if (max_impulse_ != other_conf["max_impulse"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("max_bound_mult")) {
    if (max_bound_mult_ != other_conf["max_bound_mult"].GetDouble())
      return false;
  }

  // Check the pred and tgt signals now.
  Signal* other_pred_sig = sf.getOrMakeNamedSignal(
      all_signals_array, other_conf["pred_signal"].GetString());
  if (other_pred_sig != pred_signal_) {
    return false;
  }

  Signal* other_tgt_sig = sf.getOrMakeNamedSignal(
      all_signals_array, other_conf["tgt_signal"].GetString());

  if (other_tgt_sig != tgt_signal_) {
    return false;
  }
  return true;
}

void SigDer::onSignalValue(int sig_id, double val) {
  double decay = min_tick_decay_;
  if (sig_id == pred_id_) {
    // Apply pred diff update.

    decay *= tick_decay_;

    int64_t cur_time = time_utils::nowToMs();

    double t_diff = (cur_time - last_decay_t_) / 1000.;
    decay *= std::exp(-t_diff / time_decay_);
    last_decay_t_ = cur_time;

    if (last_pred_val_ == 0) {
      // std::cout << " Skipping for updates " << skip_for_updates << std::endl;
      last_pred_val_ = val;
      return;
    } else {
      double impulse = val - last_pred_val_;
      if (take_pred_returns_) {
        impulse = impulse / std::abs(last_pred_val_);
      }

      if (last_pred_val_ < 0) {
        // std::cout << " fool " << last_pred_val_ << std::endl;
      }

      // Bound impulse.
      if (impulse > 0) {
        impulse = std::min(impulse, max_impulse_);
      } else {
        impulse = std::max(impulse, -max_impulse_);
      }
      iv_ = iv_ * decay + impulse;
      /*
      if (output_sigmoid_) {
        setValue(1 / (1 + std::exp(-iv_ / (output_sigmoid_denom_))) - .5);
        // std::cout << " output_sigmoid " << iv_ << " " << value_ << " | " <<
        // output_sigmoid_denom_ << " | " <<
        // (1/(1+std::exp(-iv_/(output_sigmoid_denom_))) -.5) << std::endl;
      } else {
        setValue(iv_);
      }
      */
      last_pred_val_ = val;
    }

  } else {
    // Decay signal.
    if (defer_decay_) {
      // Defer it.
      // Not supported yet btw.
      pktrade::GlobalVar::event_loop_->onTimeout(
          defer_decay_t_, [&] { this->deferredDecay(); });

    } else {
      // Decay this right now.
      // Apply tgt decays.
      double decay = min_tick_decay_;

      if (tgt_decay_denom_ == -1) {
        // classic deacy.
        if ((val - last_tgt_val_) * value_ > 0) {
          // Same direction.
          decay *= tgt_decay_agree_;
        } else {
          decay *= tgt_decay_disagree_;
        }
      } else {
        // Scaled decay.
        double delta_perc = std::abs(val - last_tgt_val_) / last_tgt_val_;

        if ((val - last_tgt_val_) * value_ > 0) {
          // Same direction.
          decay *= std::pow(tgt_decay_agree_, delta_perc / tgt_decay_denom_);
        } else {
          decay *= std::pow(tgt_decay_disagree_, delta_perc / tgt_decay_denom_);
        }
      }

      iv_ = iv_ * decay;

      last_tgt_val_ = val;
    }

    // Record the value.
  }

  // bound iv.
  iv_ = std::max(-bound_, std::min(bound_, iv_));

  double ivv = iv_;
  if (output_sigmoid_) {
    ivv = 1 / (1 + std::exp(-iv_ / (output_sigmoid_denom_))) - .5;
  }

  setValue(ivv);
}

void SigDer::onSignalValidity(int sig_id, bool valid) {
  // If it's false, bad.
  if (!valid) {
    reset();
  } else {
    // If I was invalid before:
    // 1 - check if I'm valid now.
    // 2 - Reset my child values.
    if (pred_signal_->getValid() && tgt_signal_->getValid()) {
      // Great.
      // Grab values.
      last_pred_val_ = pred_signal_->getValue();
      last_tgt_val_ = tgt_signal_->getValue();
      setValidity(true);
    }
  }
}

void SigDer::deferredDecay() {
  double tgt_diff = tgt_signal_->getValue() - last_tgt_val_;
  double decay = min_tick_decay_;

  // Potentially do a distance-based decay.
  /*
  if (tgt_diff * value_ > 0) {
    // Same direction.
    decay = tgt_decay_agree_;
  } else if (tgt_diff * value_ < 0) {
    decay = tgt_decay_disagree_;
  }
  */

  if (tgt_decay_denom_ == -1) {
    // classic decay.
    if ((tgt_diff)*value_ > 0) {
      // Same direction.
      decay *= tgt_decay_agree_;
    } else {
      decay *= tgt_decay_disagree_;
    }
  } else {
    // Scaled decay.
    double delta_perc = std::abs(tgt_diff) / last_tgt_val_;

    if ((tgt_diff)*value_ > 0) {
      // Same direction.
      decay *= std::pow(tgt_decay_agree_, delta_perc / tgt_decay_denom_);
    } else {
      decay *= std::pow(tgt_decay_disagree_, delta_perc / tgt_decay_denom_);
    }
  }
  last_tgt_val_ = tgt_signal_->getValue();

  iv_ = iv_ * decay;

  double ivv = iv_;
  if (output_sigmoid_) {
    ivv = 1 / (1 + std::exp(-iv_ / (output_sigmoid_denom_))) - .5;
  }

  setValue(ivv);

  // setValue(value_ * decay);
}

void SigDer::reset() {
  // Reset the children.
  last_pred_val_ = 0;
  last_tgt_val_ = 0;
}

}  // namespace pktrade::signals