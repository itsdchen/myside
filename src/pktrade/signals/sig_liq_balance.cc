#include "sig_liq_balance.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

namespace pktrade::signals {

SigLiqBalance::SigLiqBalance(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf,
                             const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf) {
  // Read in the rest.
  symbol_ = SymbolId(sig_conf["symbol"].GetString());

  // Get markets.
  for (auto& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown);
    books_.push_back(pmk);
  }

  liq_type_ = magic_enum::enum_cast<LiqType>(sig_conf["liq_type"].GetString())
                  .value_or(LiqType::FIXED_LEVELS);

  // Cool, so we have both.
  calc_style_ = magic_enum::enum_cast<CalcStyle>(sig_conf["calc_style"].GetString())
                    .value_or(CalcStyle::DEEPWP1);

  levels_deep_ = sig_conf["levels_deep"].GetInt();

  sz_decay_ = sig_conf["sz_decay"].GetDouble();
  sz_decay_2_ = 10;
  if (sig_conf.HasMember("sz_decay_2")) {
    sz_decay_2_ = sig_conf["sz_decay_2"].GetDouble();
  }

  sz_decay_type_ = magic_enum::enum_cast<SizeDecayType>(sig_conf["sz_decay_type"].GetString())
                       .value_or(SizeDecayType::EXP_SIZE);

  min_sz_wt_ = 1e-12;
  if (sig_conf.HasMember("min_sz_wt")) {
    min_sz_wt_ = sig_conf["min_sz_wt"].GetDouble();
  }

  liq_px_ = 0;

  buy_stren_ = 0;
  buy_mass_ = 0;
  sell_stren_ = 0;
  sell_mass_ = 0;

  best_bid_ = Price{0};
  worst_bid_ = Price{0};

  best_ask_ = Price{0};
  worst_ask_ = Price{0};

  min_tick_ = 0;

  per_level_decay_ = sig_conf["per_level_decay"].GetDouble();

  always_full_recalc_ = false;
  if (sig_conf.HasMember("always_full_recalc")) {
    always_full_recalc_ = sig_conf["always_full_recalc"].GetBool();
  }

  notional_to_liquidate_ = 1e10;
  if (sig_conf.HasMember("notional_to_liquidate")) {
    notional_to_liquidate_ = sig_conf["notional_to_liquidate"].GetDouble();
  }

  if (sig_conf.HasMember("use_flagged")) {
    use_flagged_ = sig_conf["use_flagged"].GetBool();
  } else {
    use_flagged_ = false;
  }

  first_tick_ = true;

  should_recalc_ = true;

  return_price_ = sig_conf["return_price"].GetBool();

  ref_signal_ = sf->getOrMakeNamedSignal(all_signals_array, sig_conf["ref_signal"].GetString());

  // ref_signal_ is required even for the return_price_ path: we still listen to it
  // and its validity gates ours (see onSignalValidity). Both old branches added the
  // listener; only the !return_price_ one null-checked, so return_price_ could deref
  // null. Single null-check + single addListener covers both.
  if (ref_signal_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing ref_signal ") +
                             sig_conf["ref_signal"].GetString() + " in SigLiqBalance");
  }
  ref_signal_->addListener(this);
  ref_signal_val_ = 0;
  ref_signal_valid_ = return_price_ || ref_signal_->getValid();
  invalidate();
}

bool SigLiqBalance::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                           const rapidjson::Value& all_signals_array) const {
  // Check these things are the same.

  if (std::string(other_conf["type"].GetString()) != "SigLiqBalance") {
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
    pktrade::Market book =
        magic_enum::enum_cast<Market>(book_str.GetString()).value_or(Market::Unknown);
    if (std::find(books_.begin(), books_.end(), book) == books_.end()) {
      return false;
    }
  }

  if (magic_enum::enum_name(liq_type_) != other_conf["liq_type"].GetString()) {
    return false;
  }

  if (levels_deep_ != other_conf["levels_deep"].GetDouble()) {
    return false;
  }

  if (magic_enum::enum_name(calc_style_) != other_conf["calc_style"].GetString()) {
    return false;
  }

  if (sz_decay_ != other_conf["sz_decay"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("sz_decay_2")) {
    if (sz_decay_2_ != other_conf["sz_decay_2"].GetDouble()) {
      return false;
    }
  }

  if (sz_decay_type_ !=
      magic_enum::enum_cast<SizeDecayType>(other_conf["sz_decay_type"].GetString())
          .value_or(SizeDecayType::EXP_SIZE)) {
    return false;
  }

  if (other_conf.HasMember("min_sz_wt")) {
    if (min_sz_wt_ != other_conf["min_sz_wt"].GetDouble()) {
      return false;
    }
  }

  if (per_level_decay_ != other_conf["per_level_decay"].GetDouble()) {
    return false;
  }

  bool other_sig_flagged = false;
  if (other_conf.HasMember("use_flagged")) {
    other_sig_flagged = other_conf["use_flagged"].GetBool();
  }
  if (use_flagged_ != other_sig_flagged) {
    return false;
  }

  Signal* other_ref_sig =
      sf.getOrMakeNamedSignal(all_signals_array, other_conf["ref_signal"].GetString());
  if (other_ref_sig != ref_signal_) {
    return false;
  }

  return true;
}

std::vector<pktrade::BookId> SigLiqBalance::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

void SigLiqBalance::subscribeData(pktrade::md::MDBeacon* beacon) {
  for (pktrade::Market one_bk : books_) {
    BookId book_id{one_bk, symbol_};
    std::vector<pktrade::md::BeaconCBType> cb_types = {
        pktrade::md::BeaconCBType::LvlAdd, pktrade::md::BeaconCBType::LvlMod,
        pktrade::md::BeaconCBType::LvlDel, pktrade::md::BeaconCBType::Final};
    beacon->addListener(book_id, this, cb_types);
  }
}

bool SigLiqBalance::shouldIgnore(Price px, Side side) {
  if (side == Side::Buy && px < worst_bid_) {
    return true;
  }

  if (side == Side::Sell && px > worst_ask_) {
    return true;
  }
  return false;
}

void SigLiqBalance::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (first_tick_) {
    first_tick_ = false;
    should_recalc_ = true;
    setMinTick();
    return;
  }

  if (always_full_recalc_) {
    // fullRecalc();
    return;
  }

  if (shouldIgnore(lvl_add.px, lvl_add.side)) {
    return;
  }

  if ((lvl_add.side == Side::Buy && lvl_add.px > best_bid_) ||
      (lvl_add.side == Side::Sell && lvl_add.px < best_ask_)) {
    should_recalc_ = true;
    return;
  } else {
    // std::cout << " Calling from onLvlAdd sz " << lvl_add.qty.toDouble() << "
    // px "
    //           << lvl_add.px.toDouble() << " side " << (lvl_add.side ==
    //           Side::Buy ? " Buy " : " Sell " ) << std::endl;

    // in-place update
    double effective_sz = getSizeDecay(lvl_add.qty.toDouble(), lvl_add.px.toDouble());
    if (lvl_add.side == Side::Buy) {
      double lvl_decay =
          std::pow(per_level_decay_, (best_bid_ - lvl_add.px).toDouble() / min_tick_);

      buy_stren_ += effective_sz * lvl_add.px.toDouble() * lvl_decay;
      buy_mass_ += effective_sz * lvl_decay;
    } else {
      double lvl_decay =
          std::pow(per_level_decay_, (lvl_add.px - best_ask_).toDouble() / min_tick_);

      sell_stren_ += effective_sz * lvl_add.px.toDouble() * lvl_decay;
      sell_mass_ += effective_sz * lvl_decay;
    }
  }
}

void SigLiqBalance::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (first_tick_) {
    first_tick_ = false;
    should_recalc_ = true;
    setMinTick();
    return;
  }
  if (always_full_recalc_) {
    // fullRecalc();
    return;
  }

  if (shouldIgnore(lvl_mod.px, lvl_mod.side)) {
    return;
  }

  // Flagging
  if ((lvl_mod.qty == Quantity{0} || lvl_mod.prev_qty == Quantity{0}) &&
      ((lvl_mod.side == Side::Buy && lvl_mod.px >= best_bid_) ||
       (lvl_mod.side == Side::Sell && lvl_mod.px <= best_ask_))) {
    // std::cout << " MIS mod " << lvl_mod.px.toDouble() << std::endl;
    should_recalc_ = true;
  } else {
    // std::cout << " Calling from onLvlMod sz " << lvl_mod.qty.toDouble() << "
    // prev_sz " << lvl_mod.prev_qty.toDouble() << " px "
    //           << lvl_mod.px.toDouble() << " side " << (lvl_mod.side ==
    //           Side::Buy ? " Buy " : " Sell " ) << std::endl;

    // in-place update
    double effective_sz_old = getSizeDecay(lvl_mod.prev_qty.toDouble(), lvl_mod.px.toDouble());
    double effective_sz_new = getSizeDecay(lvl_mod.qty.toDouble(), lvl_mod.px.toDouble());

    if (lvl_mod.side == Side::Buy) {
      double lvl_decay =
          std::pow(per_level_decay_, (best_bid_ - lvl_mod.px).toDouble() / min_tick_);

      buy_stren_ -= effective_sz_old * lvl_mod.px.toDouble() * lvl_decay;
      buy_mass_ -= effective_sz_old * lvl_decay;

      buy_stren_ += effective_sz_new * lvl_mod.px.toDouble() * lvl_decay;
      buy_mass_ += effective_sz_new * lvl_decay;

    } else {
      double lvl_decay =
          std::pow(per_level_decay_, (lvl_mod.px - best_ask_).toDouble() / min_tick_);

      sell_stren_ -= effective_sz_old * lvl_mod.px.toDouble() * lvl_decay;
      sell_mass_ -= effective_sz_old * lvl_decay;

      sell_stren_ += effective_sz_new * lvl_mod.px.toDouble() * lvl_decay;
      sell_mass_ += effective_sz_new * lvl_decay;
    }
  }
}

void SigLiqBalance::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (first_tick_) {
    first_tick_ = false;
    should_recalc_ = true;
    setMinTick();
    return;
  }
  if (always_full_recalc_) {
    // fullRecalc();
    return;
  }

  if (shouldIgnore(lvl_del.px, lvl_del.side)) {
    return;
  }

  if ((lvl_del.side == Side::Buy && lvl_del.px >= best_bid_) ||
      (lvl_del.side == Side::Sell && lvl_del.px <= best_ask_)) {
    should_recalc_ = true;
  } else {
    // std::cout << " Calling from onLvlDel prevsz "  <<
    // lvl_del.prev_qty.toDouble() << " px "
    //           << lvl_del.px.toDouble() << " side " << (lvl_del.side ==
    //           Side::Buy ? " Buy " : " Sell " ) << std::endl;

    // in-place update.
    double effective_sz_old = getSizeDecay(lvl_del.prev_qty.toDouble(), lvl_del.px.toDouble());

    if (lvl_del.side == Side::Buy) {
      double lvl_decay =
          std::pow(per_level_decay_, (best_bid_ - lvl_del.px).toDouble() / min_tick_);
      buy_stren_ -= effective_sz_old * lvl_del.px.toDouble() * lvl_decay;
      buy_mass_ -= effective_sz_old * lvl_decay;
    } else {
      double lvl_decay =
          std::pow(per_level_decay_, (lvl_del.px - best_ask_).toDouble() / min_tick_);
      sell_stren_ -= effective_sz_old * lvl_del.px.toDouble() * lvl_decay;
      sell_mass_ -= effective_sz_old * lvl_decay;
    }
  }
}

void SigLiqBalance::onFinal(const LevelBook& bk) {
  //
  if (always_full_recalc_ || should_recalc_) {
    fullRecalc();
  } else {
    calc();
  }
}

void SigLiqBalance::onSignalValue(int sig_id, double value) {
  ref_signal_val_ = value;
  // A value broadcast only happens while ref is valid, so ref is usable now.
  ref_signal_valid_ = true;
  if (!first_tick_) {
    calc();
  }
}

void SigLiqBalance::onSignalValidity(int sig_id, bool valid) {
  // In return_price_ mode the output is book-only, so ref validity doesn't gate us.
  // Let calc() be the single place that decides our own validity.
  ref_signal_valid_ = return_price_ || valid;
  if (!first_tick_) {
    ref_signal_val_ = ref_signal_->getValue();
    calc();
  }
}

double SigLiqBalance::getSizeDecay(double size, double px) {
  double size_weight = 1;
  if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL) {
    size_weight = 1 - std::exp(-(std::abs(px * size)) / sz_decay_);
  } else if (sz_decay_type_ == SizeDecayType::LOG_NOTIONAL) {
    size_weight = std::log(1 + std::abs(px * size / sz_decay_));
  } else if (sz_decay_type_ == SizeDecayType::SIZE) {
    size_weight = std::abs(size);
  } else if (sz_decay_type_ == SizeDecayType::EXP_SIZE) {
    size_weight = 1 - std::exp(-(std::abs(size)) / sz_decay_);
  } else if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL_PERORD) {
    size_weight = (px * size / sz_decay_) * (1 - std::exp(-(std::abs(px * size)) / (sz_decay_)));
  } else if (sz_decay_type_ == SizeDecayType::EXP_SIZE_PERORD) {
    size_weight = (size / sz_decay_) * (1 - std::exp(-(std::abs(size)) / (sz_decay_)));

  } else if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL_AND_SIZE) {
    size_weight = ((1 - std::exp(-(std::abs(size)) / (sz_decay_2_)))) *
                  (1 - std::exp(-(std::abs(px * size)) / (sz_decay_)));
  }

  size_weight = std::max(size_weight, min_sz_wt_);

  return size_weight;
}

void SigLiqBalance::calc() {
  if (buy_mass_ <= 0 || sell_mass_ <= 0) {
    invalidate();
    return;
  }

  if (!ref_signal_valid_ || (!return_price_ && ref_signal_val_ == 0)) {
    invalidate();
    return;
  }

  // Set some range bounds for doing calc if the buy- and sell- masses
  // are too off.

  if (calc_style_ == CalcStyle::AVG_LIQ_PX) {
    // Unweighted mid of the two side-average liquidation prices. Doesn't weigh by
    // the strength of either side. Falls through to the shared publish block below.
    double avg_bid_px = buy_stren_ / buy_mass_;
    double avg_ask_px = sell_stren_ / sell_mass_;
    liq_px_ = (avg_bid_px + avg_ask_px) / 2.0;
  } else if (calc_style_ == CalcStyle::DEEPWP1) {
    // Prob better ways of doing it.
    liq_px_ = (buy_stren_ + sell_stren_) / (buy_mass_ + sell_mass_);

    // printf("DEEPWP1: buy_mass %f sell_mass %f liq_px %f liq_return %f \n",
    // buy_mass_, sell_mass_,
    //    liq_px_, liq_px_ / ref_signal_val_ - 1);
  } else if (calc_style_ == CalcStyle::DEEPWMP2) {
    double mid = (best_bid_.toDouble() + best_ask_.toDouble()) / 2;
    double avg_bid_px = buy_stren_ / buy_mass_;
    double avg_ask_px = sell_stren_ / sell_mass_;
    // Similar to the liqpx version.
    liq_px_ = (avg_bid_px * sell_mass_ - avg_ask_px * buy_mass_) / (buy_mass_ + sell_mass_) * mid;

    // OK, I think this needs some more work. I'm not sure that any of this is
    // right actually.
    /*

      printf("DEEP2: abp %f aap %f buy_mass %f sell_mass %f liq_px %f\n",

             avg_bid_px, avg_ask_px, buy_mass_, sell_mass_, liq_px_);
             */
  }

  /*
  printf(
      "%s: From calc. buys (%f %f) sell (%f %f) bb ba (%f %f) mid %f ref %f\n",
      time_utils::nowToStr().c_str(), buy_stren_, buy_mass_, sell_stren_,
      sell_mass_, best_bid_.toDouble(), best_ask_.toDouble(),
      (best_bid_.toDouble() + best_ask_.toDouble()) / 2, ref_signal_val_);
      */

  if (return_price_) {
    setVV(liq_px_, true);
    return;
  }

  // ref_signal_val_ is already guaranteed valid and non-zero by the guard at the
  // top of calc(), so no re-check / invalidate() is needed here.
  // DEEPWP1 keeps its historical opposite sign; it should really be liq_px/ref - 1.
  double ivv = (calc_style_ == CalcStyle::DEEPWP1) ? (1 - liq_px_ / ref_signal_val_)
                                                   : (liq_px_ / ref_signal_val_ - 1);
  setVV(ivv, true);
}

// Note that there is an inconsistency that gets introduced.
// We can make this better. I'm going to do that soon.

// I can probably turn this more incremental.
void SigLiqBalance::fullRecalc() {
  // One day: support this across multiple books.
  const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(BookId{books_[0], symbol_});

  // printf("Start fullrecalc. buys (%f %f) sells (%f %f)\n", buy_stren_,
  //         buy_mass_, sell_stren_, sell_mass_);

  best_bid_ = Price{0};
  worst_bid_ = Price{0};

  best_ask_ = Price{0};
  worst_ask_ = Price{0};

  double amt_liq_buy = 0;
  double amt_liq_sell = 0;

  // printf("-------------- START FULLRECALC --------------\n");
  //  Liquidate bids:
  auto& buy_side = bk->side<BuySide>();

  buy_stren_ = 0;
  buy_mass_ = 0;
  double buy_decay = 1;

  for (auto& lvl : buy_side) {
    // Check if this level was flagged away. If it was, then
    // do not count it when liquidating.
    double lvl_shs = effectiveQty(lvl).toDouble();
    if (lvl_shs == 0) {
      continue;
    }

    if (best_bid_ == Price{0}) {
      best_bid_ = lvl.px;
    }
    worst_bid_ = lvl.px;

    double wanted_sz =
        std::min((notional_to_liquidate_ - amt_liq_buy) / lvl.px.toDouble(), lvl_shs);

    // TODO: maybe introduce some... size...decay.
    double effective_sz = getSizeDecay(wanted_sz, lvl.px.toDouble());

    buy_stren_ += effective_sz * lvl.px.toDouble() * buy_decay;
    buy_mass_ += effective_sz * buy_decay;

    amt_liq_buy += wanted_sz * lvl.px.toDouble();
    /*
        printf("BUYLVL px %f sz %f wanted_sz %f effective_sz %f buy_decay %f
       px_decay %f buy_stren %.8f buy_mass %.8f \n", lvl.px.toDouble(), lvl_shs,
          wanted_sz,
          effective_sz,
          buy_decay,
          px_decay,
          buy_stren_,
          buy_mass_
        );
    */
    buy_decay *= per_level_decay_;
    // The 0.001 is for some float wiggle
    if ((best_bid_ - lvl.px).toDouble() >= min_tick_ * (levels_deep_ - pktrade::EPS)) {
      break;
    }

    if (amt_liq_buy >= notional_to_liquidate_) {
      break;
    }
  }

  // printf("DONE LIQ BUYS: buy_stren %f bu_mass %f amt_liq_buy %f\n",
  //        buy_stren_, buy_mass_, amt_liq_buy);

  // Liquidate asks:
  auto& sell_side = bk->side<SellSide>();
  sell_stren_ = 0;
  sell_mass_ = 0;
  double sell_decay = 1;
  for (auto& lvl : sell_side) {
    // Check if this level was flagged away. If it was, then
    // do not count it when liquidating.
    double lvl_shs = effectiveQty(lvl).toDouble();
    if (lvl_shs == 0) {
      continue;
    }

    if (best_ask_ == Price{0}) {
      best_ask_ = lvl.px;
    }
    worst_ask_ = lvl.px;

    double wanted_sz =
        std::min((notional_to_liquidate_ - amt_liq_sell) / lvl.px.toDouble(), lvl_shs);

    double effective_sz = getSizeDecay(wanted_sz, lvl.px.toDouble());

    sell_stren_ += effective_sz * lvl.px.toDouble() * sell_decay;
    sell_mass_ += effective_sz * sell_decay;

    amt_liq_sell += wanted_sz * lvl.px.toDouble();
    /*

      printf("SELLLVL px %f sz %f wanted_sz %f effective_sz %f sell_decay %f
      px_decay %f sell_stren %.8f sell_mass %.8f \n", lvl.px.toDouble(),
        lvl_shs,
        wanted_sz,
        effective_sz,
        sell_decay,
        px_decay,
        sell_stren_,
        sell_mass_
      );
  */
    sell_decay *= per_level_decay_;

    // The 0.001 is for some float wiggle
    if ((lvl.px - best_ask_).toDouble() >= min_tick_ * (levels_deep_ - pktrade::EPS)) {
      break;
    }
    if (amt_liq_sell >= notional_to_liquidate_) {
      break;
    }
  }
  // printf("DONE LIQ SELLS: sell_stren %f sell_mass %f amt_liq_sell %f\n",
  //      sell_stren_, sell_mass_, amt_liq_sell);

  // printf("Just conducted fullrecalc. \n");

  // printf("-------------- END FULLRECALC --------------\n");

  calc();

  /*
  printf("After fullcalc buys (%f %f) sells (%f %f) liq_px %f mid %f val %f\n ",
             buy_stren_,
         buy_mass_, sell_stren_, sell_mass_, liq_px_, ref_signal_val_, value_);
         */
  should_recalc_ = false;
} // namespace pktrade::signals

// do once: get the min tick for this book.
double SigLiqBalance::setMinTick() {
  const LevelBook* bk = pktrade::GlobalVar::book_manager_->find(BookId{books_[0], symbol_});
  auto& buy_side = bk->side<BuySide>();

  min_tick_ = 0;
  double last_px = 0;
  for (auto& lvl : buy_side) {
    if (last_px == 0) {
      last_px = lvl.px.toDouble();
      continue;
    } else {
      if (min_tick_ == 0) {
        double lvl_diff = last_px - lvl.px.toDouble();
        if (min_tick_ == 0 || (lvl_diff > 0 && lvl_diff < min_tick_)) {
          min_tick_ = lvl_diff;
        }
      }
    }
  }
  return min_tick_;
}

// Return both version's of the liquidation price.
double SigLiqBalance::getBBMeasure() { return buy_stren_ / buy_mass_; }
double SigLiqBalance::getBAMeasure() { return sell_stren_ / sell_mass_; }

void SigLiqBalance::setNotionalToLiquidate(double notional_to_liquidate) {
  notional_to_liquidate_ = notional_to_liquidate;
  should_recalc_ = true;
}

void SigLiqBalance::invalidate() {
  value_ = 0;
  setValidity(false);
}

void SigLiqBalance::reset() {
  liq_px_ = 0;
  buy_stren_ = 0;
  buy_mass_ = 0;
  sell_stren_ = 0;
  sell_mass_ = 0;
  best_bid_ = Price{0};
  worst_bid_ = Price{0};
  best_ask_ = Price{0};
  worst_ask_ = Price{0};
  min_tick_ = 0;
  first_tick_ = true;
  should_recalc_ = true;
  ref_signal_val_ = 0;
  ref_signal_valid_ = return_price_ || ref_signal_->getValid();
  invalidate();
}

std::string SigLiqBalance::getPrimarySymbol() const { return symbol_.get(); }

}; // namespace pktrade::signals
