#include "signal_factory.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/signals/sig_file.h"

#include "pktrade/signals/sig_linear.h"
#include "pktrade/signals/sig_liq_balance.h"
#include "pktrade/signals/sig_mid.h"
#include "pktrade/signals/sig_wmid.h"

#include "pktrade/signals/sig_book_impulse.h"
#include "pktrade/signals/sig_deep_book.h"
#include "pktrade/signals/sig_der.h"
#include "pktrade/signals/sig_trd_impulse.h"
#include "pktrade/signals/sig_trd_px.h"
#include "pktrade/signals/sig_libra.h"
#include "pktrade/signals/sig_reference.h"
#include "pktrade/signals/sig_ret_ems.h"

namespace pktrade::signals {

Signal* SignalFactory::getByName(std::string name) {
  std::map<std::string, int>::iterator idx = name_to_id_.find(name);

  if (idx == name_to_id_.end()) {
    return nullptr;
  }
  return signal_stash_[idx->second];
}

Signal* SignalFactory::getOrMakeByConf(const rapidjson::Value& conf,
                                       const rapidjson::Value& all_signals_array) {
  std::string name = conf["name"].GetString();
  std::string sig_type = conf["type"].GetString();

  Signal* sig = nullptr;
  for (std::pair<int, Signal*> sig_pair : signal_stash_) {
    if (sig_pair.second->isSame(conf, *this, all_signals_array)) {
      // If we deduped instead of creating, we need to save the name too.
      name_to_id_[name] = sig_pair.first;
      return signal_stash_[sig_pair.first];
    } else {
    }
  }

  // Did not find. So, create it.
  // This is the big if block that we love to hate.
  if (sig_type == "SigMid") {
    sig = new SigMid(curIndex_++, conf, this);
  } else if (sig_type == "SigQuoteMid") {
    sig = new SigQuoteMid(curIndex_++, conf, this);
  } else if (sig_type == "SigWMid") {
    sig = new SigWMid(curIndex_++, conf, this);
  } else if (sig_type == "SigDer") {
    sig = new SigDer(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigTrdImpulse") {
    sig = new SigTrdImpulse(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigTrdPx") {
    sig = new SigTrdPx(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigBookImpulse") {
    sig = new SigBookImpulse(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigDeepBook") {
    sig = new SigDeepBook(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigLibra") {
    sig = new SigLibra(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigLinear") {
    sig = new SigLinear(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigLiqBalance") {
    sig = new SigLiqBalance(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigBid") {
    sig = new SigBid(curIndex_++, conf, this);
  } else if (sig_type == "SigAsk") {
    sig = new SigAsk(curIndex_++, conf, this);
  } else if (sig_type == "SigRetEms") {
    sig = new SigRetEms(curIndex_++, conf, this, all_signals_array);
  } else if (sig_type == "SigFile") {
    sig = new SigFile(curIndex_++, conf, this);
  } else if (sig_type == "SigReference") {
    // External reference feed (Kalshi WS + Polymarket REST) exposed as a Signal.
    sig = new SigReference(curIndex_++, conf, this);
  } else {
    throw std::runtime_error("SignalFactory doesn't support type " + sig_type);
  }

  // Store it.
  signal_stash_[sig->getID()] = sig;
  name_to_id_[sig->getName()] = sig->getID();
  return sig;
}

void SignalFactory::makeAllSignals(const rapidjson::Value& all_signals_array) {
  all_signals_backup_.CopyFrom(all_signals_array, all_signals_backup_.GetAllocator());

  std::vector<std::string> all_names;
  // Go through the values, get the names, iteratively call gomnts.
  for (const rapidjson::Value& one_signal_conf : all_signals_array.GetArray()) {
    all_names.push_back(one_signal_conf["name"].GetString());
  }

  for (std::string name : all_names) {
    getOrMakeNamedSignal(all_signals_array, name);
  }
}

Signal* SignalFactory::getOrMakeNamedSignal(const rapidjson::Value& all_signals_array,
                                            std::string name) {
  Signal* pot_sig = getByName(name);
  if (pot_sig != nullptr)
    return pot_sig;

  for (const rapidjson::Value& one_signal_conf : all_signals_array.GetArray()) {
    if (one_signal_conf["name"].GetString() == name) {
      return getOrMakeByConf(one_signal_conf, all_signals_array);
    }
  }
  return nullptr;
}

void SignalFactory::subscribeAll() {
  for (const std::pair<int, Signal*> sig_pair : signal_stash_) {
    sig_pair.second->subscribeData(pktrade::GlobalVar::md_beacon_);
  }
}

std::vector<Signal*> SignalFactory::getAllSignals() {
  std::vector<Signal*> sigs;
  for (const std::pair<int, Signal*> sig_pair : signal_stash_) {
    sigs.push_back(sig_pair.second);
  }
  return sigs;
}

std::vector<pktrade::BookId> SignalFactory::getBookIds() {
  std::set<pktrade::BookId> ids;
  // Add from all signals.
  for (const std::pair<int, Signal*> sig_pair : signal_stash_) {
    std::vector<pktrade::BookId> sig_bookids = sig_pair.second->getBookIds();
    ids.insert(sig_bookids.begin(), sig_bookids.end());
  }
  std::vector<pktrade::BookId> ids_vec(ids.begin(), ids.end());
  return ids_vec;
}

} // namespace pktrade::signals
