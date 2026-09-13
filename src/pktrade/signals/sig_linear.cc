#include "sig_linear.h"

#include "pktrade/context/global_vars.h"
#include <fmt/format.h>

namespace pktrade::signals {

SigLinear::SigLinear(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf,
                     const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf) {
  // Populate some values...
  offset_ = sig_conf["offset"].GetDouble();

  for (const rapidjson::Value& child_sig_name : sig_conf["sig_names"].GetArray()) {
    Signal* child_sig = sf->getOrMakeNamedSignal(all_signals_array, child_sig_name.GetString());
    // subscribe.
    if (child_sig == nullptr) {
      throw std::runtime_error(std::string("Failed constructing signal ") +
                               child_sig_name.GetString() + " while constructing SigLinear");
    }
    child_sig->addListener(this);

    // We get callbacks with sig_id. And need to know where to index.
    sig_id_to_idx_[child_sig->getID()] = sig_validity_.size();
    child_signals_.push_back(child_sig);

    // Not sure we want a unviersal true/false yet. Maybe signals should send a
    // valid=True on their first publishing?
    sig_validity_.push_back(true);
    sig_vals_.push_back(0);

    // Get the coef.
    std::string coef_label = fmt::format("{}_coef", child_sig_name.GetString());
    // What... is it?
    if (!sig_conf.HasMember(coef_label.c_str())) {
      throw std::runtime_error("Could not find " + coef_label + " in siglinear conf");
    }

    sig_coef_.push_back(sig_conf[coef_label.c_str()].GetDouble());

    // Get bounds, if there are any.
    std::string bound_label = fmt::format("{}_bound", child_sig_name.GetString());
    if (sig_conf.HasMember(coef_label.c_str())) {
      sig_bounds_.push_back(sig_conf[bound_label.c_str()].GetDouble());
    } else {
      sig_bounds_.push_back(0);
    }
  }
  // Start everything at 0.
  reset();
}

bool SigLinear::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                       const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigLinear") {
    return false;
  }

  // Check offsets
  if (offset_ != other_conf["offset"].GetDouble()) {
    return false;
  }

  // Let's be lazy enough and just care about things in the same order.

  // Child sigs must be same size.
  if (other_conf["sig_names"].Size() != sig_validity_.size()) {
    return false;
  }

  int i = 0;
  for (const rapidjson::Value& child_sig_name : other_conf["sig_names"].GetArray()) {
    Signal* other_child_sig =
        sf.getOrMakeNamedSignal(all_signals_array, child_sig_name.GetString());

    // If they were the same, then deduping would have given us the same child
    // signal object.
    if (other_child_sig != child_signals_[i]) {
      return false;
    }

    // Check coef.
    std::string coef_label = fmt::format("{}_coef", child_sig_name.GetString());
    if (other_conf[coef_label.c_str()].GetDouble() != sig_coef_[i]) {
      return false;
    }

    i++;
  }

  // Check children.

  // You win. It's the same.
  return true;
}

// SignalListener callbacks.
void SigLinear::onSignalValue(int sig_id, double value) {
  if (sig_bounds_[sig_id_to_idx_[sig_id]] > 0 &&
      std::abs(value) > sig_bounds_[sig_id_to_idx_[sig_id]]) {
    // Zero out things that went out of bounds.
    // Just do it internally, I guess. This might end up being confusing, so
    // reserve the option of flagging this to be stored but not actually used.
    //    printf("Zeroing out %d because |%f|>%f\n", sig_id, value,
    //    sig_bounds_[sig_id_to_idx_[sig_id]]);
    value = 0;
  }

  double diff = value - sig_vals_[sig_id_to_idx_[sig_id]];

  sig_vals_[sig_id_to_idx_[sig_id]] = value;

  setValue(value_ + diff * sig_coef_[sig_id_to_idx_[sig_id]]);
  // printf("Linear: onChild id %d val %f myVal %f\n", sig_id, value, value_);
  // std::cout << " And valid is " << valid_ << std::endl;
  // fullPrint();
}

void SigLinear::onSignalValidity(int sig_id, bool valid) {
  // If it didn't switch, then do nothing.
  if (sig_validity_[sig_id_to_idx_[sig_id]] == valid) {
    return;
  }

  // Update validity.
  sig_validity_[sig_id_to_idx_[sig_id]] = valid;

  if (valid) {
    setValue(value_ + sig_vals_[sig_id_to_idx_[sig_id]] * sig_coef_[sig_id_to_idx_[sig_id]]);
  } else {
    setValue(value_ - sig_vals_[sig_id_to_idx_[sig_id]] * sig_coef_[sig_id_to_idx_[sig_id]]);
  }
}

void SigLinear::fullRecalc() {
  // Calculate everything.
  double tmp_val = 0;
  for (uint i = 0; i < sig_coef_.size(); i++) {
    if (sig_validity_[i]) {
      if (sig_bounds_[i] > 0 && std::abs(sig_vals_[i]) > sig_bounds_[i]) {
        //      printf("Zeroing out on a recalc because |%f|>%f \n",
        //      sig_vals_[i], sig_bounds_[i]);
        continue;
      }

      tmp_val += sig_vals_[i] * sig_coef_[i];
    }
  }
  tmp_val += offset_;
  setValue(tmp_val);
}

void SigLinear::reset() {
  // Go through and set the child values.
  double tmp_val_ = 0;
  for (uint i = 0; i < sig_coef_.size(); i++) {
    sig_validity_[i] = child_signals_[i]->getValid();
    sig_vals_[i] = child_signals_[i]->getValue();

    if (sig_validity_[i]) {
      tmp_val_ += sig_vals_[i] * sig_coef_[i];
    }
  }

  setValue(tmp_val_ + offset_);
}

void SigLinear::fullPrint() {
  printf("******************** START PRINT ********************\n");
  for (uint i = 0; i < sig_coef_.size(); i++) {
    printf("Sig %s %.4f (coef %.4f) contrib %.6f\n", child_signals_[i]->getName().c_str(),
           sig_vals_[i], sig_coef_[i], sig_vals_[i] * sig_coef_[i]);
  }
  printf("Offset: %f\n", offset_);
  printf("FinalVal: %f\n", value_);
  printf("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ END PRINT ~~~~~~~~~~~~~~~~~~~~\n");
}

std::string SigLinear::fullDebugString() {
  std::string to_ret;
  for (uint i = 0; i < sig_coef_.size(); i++) {
    to_ret += fmt::format(" {} {:.5f} (coef {:.4f}) contrib {:.7f} | ",
                          child_signals_[i]->getName().c_str(), sig_vals_[i], sig_coef_[i],
                          sig_vals_[i] * sig_coef_[i]);
  }
  to_ret += fmt::format(" Final {:.5f}", value_);
  return to_ret;
}

} // namespace pktrade::signals
