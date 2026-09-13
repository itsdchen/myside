#include "sig_trd_px.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

#include "pktrade/util/basiclib.h"

namespace pktrade::signals {

SigTrdPx::SigTrdPx(int signal_id,
                   const rapidjson::Value& sig_conf,
                   SignalFactory* sf,
                   const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf), running_num_(0), running_denom_(0) {
  symbol_ = SymbolId(sig_conf["symbol"].GetString());
  for (auto& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString())
                              .value_or(Market::Unknown);
    books_.push_back(pmk);
  }

  sz_decay_ = sig_conf["sz_decay"].GetDouble();
  sz_decay_type_ = magic_enum::enum_cast<SizeDecayType>(
                       sig_conf["sz_decay_type"].GetString())
                       .value_or(SizeDecayType::EXP_SIZE);
  time_decay_ = sig_conf["time_decay"].GetDouble();
  tick_decay_ = sig_conf["tick_decay"].GetDouble();

  calc_style_ = CalcStyle::AVG_PX;
  if (sig_conf.HasMember("calc_style")) {
    calc_style_ =
        magic_enum::enum_cast<CalcStyle>(sig_conf["calc_style"].GetString())
            .value_or(CalcStyle::AVG_PX);
  }

  return_price_ = sig_conf["return_price"].GetBool();

  ref_signal_ = sf->getOrMakeNamedSignal(all_signals_array,
                                         sig_conf["ref_signal"].GetString());

  if (ref_signal_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing ref_signal ") +
                             sig_conf["ref_signal"].GetString() +
                             " in SigTrdPx");
  }

  ref_signal_->addListener(this);
  ref_signal_val_ = 0;
  last_decay_t_ = time_utils::nowToMs();
}

bool SigTrdPx::isSame(const rapidjson::Value& other_conf,
                      SignalFactory& sf,
                      const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigTrdPx") {
    return false;
  }

  if (!(symbol_ == SymbolId(other_conf["symbol"].GetString()))) {
    return false;
  }

  // Check the other's markets.
  auto other_books = other_conf["books"].GetArray();
  if (other_books.Size() != books_.size()) {
    return false;
  }

  for (auto& book_str : other_books) {
    pktrade::Market book = magic_enum::enum_cast<Market>(book_str.GetString())
                               .value_or(Market::Unknown);
    if (std::find(books_.begin(), books_.end(), book) == books_.end()) {
      return false;
    }
  }

  if (sz_decay_ != other_conf["sz_decay"].GetDouble()) {
    return false;
  }

  if (sz_decay_type_ != magic_enum::enum_cast<SizeDecayType>(
                            other_conf["sz_decay_type"].GetString())
                            .value_or(SizeDecayType::EXP_SIZE)) {
    return false;
  }

  if (time_decay_ != other_conf["time_decay"].GetDouble()) {
    return false;
  }
  if (tick_decay_ != other_conf["tick_decay"].GetDouble()) {
    return false;
  }

  if (return_price_ != other_conf["return_price"].GetBool()) {
    return false;
  }

  // Checking size_decay_types
  if (!other_conf.HasMember("sz_decay_type") &&
      sz_decay_type_ != SizeDecayType::EXP_SIZE) {
    // Default, because I added this feature in later.
    return false;
  } else if (other_conf.HasMember("sz_decay_type") &&
             sz_decay_type_ != magic_enum::enum_cast<SizeDecayType>(
                                   other_conf["sz_decay_type"].GetString())
                                   .value_or(SizeDecayType::EXP_SIZE)) {
    return false;
  }

  // For calc_styles
  if (!other_conf.HasMember("calc_style") && calc_style_ != CalcStyle::AVG_PX) {
    // Default, because I added this feature in later.
    return false;
  } else if (other_conf.HasMember("calc_style") &&
             calc_style_ != magic_enum::enum_cast<CalcStyle>(
                                other_conf["calc_style"].GetString())
                                .value_or(CalcStyle::AVG_PX)) {
    return false;
  }

  // Check the ref signal now.
  Signal* other_ref_sig = sf.getOrMakeNamedSignal(
      all_signals_array, other_conf["ref_signal"].GetString());
  if (other_ref_sig != ref_signal_) {
    return false;
  }

  return true;
}

std::vector<pktrade::BookId> SigTrdPx::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

void SigTrdPx::subscribeData(MDBeacon* beacon) {
  // This is kinda weak. I'm oversubscribing to I guess keep up the
  // decay. Probably not needed.
  BookId book_id{books_[0], symbol_};
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd,
      pktrade::md::BeaconCBType::LvlMod,
      pktrade::md::BeaconCBType::LvlDel,
      pktrade::md::BeaconCBType::Trd,
  };

  beacon->addListener(book_id, this, cb_types);
}

void SigTrdPx::applyDecay() {
  double decay = 1;
  decay *= tick_decay_;

  int64_t cur_t = time_utils::nowToMs();

  // Time-based decay.
  if (last_decay_t_ != 0) {
    decay *= std::exp(-(double)((cur_t - last_decay_t_) / 1e3) / time_decay_);
  }

  last_decay_t_ = cur_t;

  // So many decays.
  // printf("Decay. tick %f time %f (diff %f) tot %f \n", tick_decay_,
  // std::exp(-(double)((cur_t - last_decay_t_) / 1e3) / time_decay_),
  // (double)(cur_t - last_decay_t_), decay );

  running_num_ *= decay;
  running_denom_ *= decay;
}

void SigTrdPx::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  applyDecay();
}

void SigTrdPx::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  applyDecay();
}

void SigTrdPx::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  applyDecay();
}

// We like this.
void SigTrdPx::onTrade(const LevelBook& bk, const Trade& trd) {
  applyDecay();
  double impulse = 1;

  if (std::isnan(ref_signal_val_) || ref_signal_val_ == 0) {
    return;
  }

  // Larger size should have more impulse.

  if (sz_decay_type_ == SizeDecayType::EXP_SIZE) {
    impulse *= (1 - std::exp(-(trd.qty.toDouble()) / sz_decay_));
  } else if (sz_decay_type_ == SizeDecayType::SIZE) {
    impulse *= trd.qty.toDouble();
  } else if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL) {
    impulse *=
        (1 - std::exp(-(trd.qty.toDouble() * trd.px.toDouble()) / sz_decay_));
  } else if (sz_decay_type_ == SizeDecayType::LOG_NOTIONAL) {
    impulse *= std::log(
        1 + std::abs(trd.qty.toDouble() * trd.px.toDouble() / sz_decay_));
  }

  if (debug_verbose_) {
    printf("Trd happened. Size %f px %f Impulse %f num %f denom %f oldpx %f\n",
           trd.qty.toDouble(), trd.px.toDouble(), impulse, running_num_,
           running_denom_, running_num_ / running_denom_);
  }

  if (calc_style_ == CalcStyle::AVG_PX) {
    // Do nothing.
    running_num_ += impulse * trd.px.toDouble();
    running_denom_ += impulse;

  } else if (calc_style_ == CalcStyle::AVG_REL_PRICE) {
    running_num_ += impulse * (trd.px.toDouble() / ref_signal_val_ - 1);
    running_denom_ += impulse;
  }

  calc();
}

void SigTrdPx::onSignalValue(int sig_id, double value) {
  ref_signal_val_ = value;
  // If the midprice changes, we should probably recalc.
  calc();
}

void SigTrdPx::onSignalValidity(int sig_id, bool valid) { ref_signal_val_ = 0; }

void SigTrdPx::calc() {
  if (running_denom_ < min_denom_) {
    return;
  }
  // OK, then we have a working price.

  if (calc_style_ == CalcStyle::AVG_PX) {
    if (return_price_) {
      // printf("TrdPx: %f %f %f\n", running_num_, running_denom_, running_num_
      // / running_denom_);

      setValue(running_num_ / running_denom_);
      return;
    } else {
      if (ref_signal_val_ == 0) {
        return;
      }

      // printf("TrdPx: %f %f %f\n", running_num_, running_denom_,
      // ref_signal_val_);

      setValue((running_num_ / running_denom_) / ref_signal_val_ - 1);
    }
  } else if (calc_style_ == CalcStyle::AVG_REL_PRICE) {
    if (return_price_) {
      setValue((running_num_ / running_denom_ + 1) * ref_signal_val_);
      return;
    } else {
      if (ref_signal_val_ == 0) {
        return;
      }

      if (debug_verbose_) {
        printf("TrdPx: %f %f %f\n", running_num_, running_denom_,
               ref_signal_val_);
      }

      setValue(running_num_ / running_denom_);
    }
  }
}

void SigTrdPx::reset() {
  // Reset some of the stuff.
  running_num_ = 0;
  running_denom_ = 0;
  ref_signal_val_ = ref_signal_->getValue();
}

}  // namespace pktrade::signals