#include "sig_deep_book.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/time_utils.h"
#include <magic_enum.hpp>

/*
Research ideas:
  - Decrease magnitude of output w.r.t. how many insidechanges it's been since
last time.
  - Raise output to some power
  - Scale (up or down) the prediction w.r.t. the magnitude of the spread.
    - Is a wide spread more impt?
  - Take stats on liquidity.

*/

namespace pktrade::signals {

SigDeepBook::SigDeepBook(int signal_id,
                         const rapidjson::Value& sig_conf,
                         SignalFactory* sf,
                         const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf),
      bb_(0),
      ba_(1e10),
      insides_since_recalc_(0),
      bid_stren_(0),
      ask_stren_(0) {
  symbol_ = SymbolId(sig_conf["symbol"].GetString());

  // Get markets.
  for (auto& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString())
                              .value_or(Market::Unknown);
    books_.push_back(pmk);
  }

  // This can either be given as a percent or as an absolute.
  // In the future, I'm going to
  px_decay_scale_pct_ = sig_conf["px_decay"].GetDouble();
  px_decay_scale_px_ = -1;
  if (sig_conf.HasMember("px_decay_scale_px")) {
    px_decay_scale_px_ = sig_conf["px_decay_scale_px"].GetDouble();
  }

  sz_decay_ = sig_conf["sz_decay"].GetDouble();

  sz_decay_2_ = 10;
  if (sig_conf.HasMember("sz_decay_2")) {
    sz_decay_2_ = sig_conf["sz_decay_2"].GetDouble();
  }

  sz_decay_type_ = magic_enum::enum_cast<SizeDecayType>(
                       sig_conf["sz_decay_type"].GetString())
                       .value_or(SizeDecayType::EXP_SIZE);
  min_px_weight_ = sig_conf["min_px_weight"].GetDouble();

  max_pct_away_ = 0.02;
  if (sig_conf.HasMember("max_pct_away")) {
    max_pct_away_ = sig_conf["max_pct_away"].GetDouble();
  }

  // Defer setting min_px_weight until the first tick.

  insides_before_recalc_live_ = 1000;
  // FYI quite often this is 1.
  if (sig_conf.HasMember("ignore_recalc_thresh_mult")) {
    ignore_recalc_thresh_mult_ =
        sig_conf["ignore_recalc_thresh_mult"].GetDouble();
    //    printf("Setting this mult: %f\n", ignore_recalc_thresh_mult_);
  } else {
    ignore_recalc_thresh_mult_ = 0;
  }

  // Maybe normalize against tot stren.
  totstren_tdc_ = 0;
  if (sig_conf.HasMember("totstren_tdc_s")) {
    totstren_tdc_ = sig_conf["totstren_tdc_s"].GetDouble() * 1000;
    totstren_ema_num_ = 0;
    totstren_ema_denom_ = 0;
    last_totstren_t_ = 0;

    totstren_ema_bound_ = sig_conf["totstren_ema_bound"].GetDouble();
    totstren_ema_power_ = sig_conf["totstren_ema_power"].GetDouble();
  }

  // These shadow values should be re-set upon
  // full recalcs. Use event-based-decays.
  shadow_b_ = 0;
  shadow_a_ = 0;
  shadow_frac_ = 0;
  use_shadow_ = false;
  shadow_decay_frac_ = 0.95;
  if (sig_conf.HasMember("use_shadow") && sig_conf["use_shadow"].GetBool()) {
    use_shadow_ = true;
    shadow_frac_ = sig_conf["shadow_frac"].GetDouble();
    shadow_decay_frac_ = sig_conf["shadow_decay_frac"].GetDouble();
  }

  insides_before_recalc_ = sig_conf["insides_before_recalc"].GetInt();
  bounding_val_ = sig_conf["bounding_val"].GetDouble();
  return_price_ = sig_conf["return_price"].GetBool();

  if (sig_conf.HasMember("use_flagged")) {
    use_flagged_ = sig_conf["use_flagged"].GetBool();
  } else {
    use_flagged_ = false;
  }

  use_flagged_ = true;

  calc_style_ =
      magic_enum::enum_cast<CalcStyle>(sig_conf["calc_style"].GetString())
          .value_or(CalcStyle::DIFF_RATIO);

  if (calc_style_ == CalcStyle::DIFF_RATIO && return_price_) {
    throw std::runtime_error(
        "WARNING: SigDeepBook: return_price_ is true but calc_style is "
        "DIFF_RATIO. We do not allow this.\n");
  }

  if (debug_verbose_) {
    verbose_full_recalc_ = true;
    verbose_msi_ = true;
    verbose_lvlcb_ = true;
  }
}

bool SigDeepBook::isSame(const rapidjson::Value& other_conf,
                         SignalFactory& sf,
                         const rapidjson::Value& all_signals_array) const {
  // Check these things are the same.
  if (std::string(other_conf["type"].GetString()) != "SigDeepBook") {
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

  // If we specify the pxdecayscalepx, then we check if the other one did
  // properly too.
  if (px_decay_scale_px_ != -1 &&
      ((!other_conf.HasMember("px_decay_scale_px")) ||
       (px_decay_scale_px_ != other_conf["px_decay_scale_px"].GetDouble()))) {
    return false;
  } else if (px_decay_scale_px_ == -1) {
    // Otherwise, we're probably in the pct-mode version.
    // In that case,
    if (other_conf.HasMember("px_decay_scale_px")) {
      return false;
    }
    if (px_decay_scale_pct_ != other_conf["px_decay"].GetDouble()) {
      return false;
    }
  }

  if (sz_decay_ != other_conf["sz_decay"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("totstren_tdc_s")) {
    if (totstren_tdc_ != other_conf["totstren_tdc_s"].GetDouble()) {
      return false;
    }

    if (totstren_ema_bound_ != other_conf["totstren_ema_bound"].GetDouble()) {
      return false;
    }

    if (totstren_ema_power_ != other_conf["totstren_ema_power"].GetDouble()) {
      return false;
    }
  }

  if (other_conf.HasMember("sz_decay_2")) {
    if (sz_decay_2_ != other_conf["sz_decay_2"].GetDouble()) {
      return false;
    }
  }

  if (sz_decay_type_ != magic_enum::enum_cast<SizeDecayType>(
                            other_conf["sz_decay_type"].GetString())
                            .value_or(SizeDecayType::EXP_SIZE)) {
    return false;
  }

  if (min_px_weight_ != other_conf["min_px_weight"].GetDouble()) {
    return false;
  }

  // Not the most incorrect.
  if (other_conf.HasMember("max_pct_away")) {
    if (max_pct_away_ != other_conf["max_pct_away"].GetDouble()) {
      return false;
    }
  }

  if (ignore_recalc_thresh_mult_ !=
      other_conf["ignore_recalc_thresh_mult"].GetDouble()) {
    return false;
  }
  if (insides_before_recalc_ != other_conf["insides_before_recalc"].GetInt()) {
    return false;
  }

  if (bounding_val_ != other_conf["bounding_val"].GetDouble()) {
    return false;
  }
  if (return_price_ != other_conf["return_price"].GetBool()) {
    return false;
  }

  bool other_sig_flagged = false;
  if (other_conf.HasMember("use_flagged")) {
    other_sig_flagged = other_conf["use_flagged"].GetBool();
  }
  if (use_flagged_ != other_sig_flagged) {
    return false;
  }

  if (calc_style_ !=
      magic_enum::enum_cast<CalcStyle>(other_conf["calc_style"].GetString())
          .value_or(CalcStyle::DIFF_RATIO)) {
    return false;
  }

  // TODO: consolidate this use_shadow stuff.

  // Shadow.

  bool other_use_shadow = false;
  if (other_conf.HasMember("use_shadow")) {
    other_use_shadow = other_conf["use_shadow"].GetBool();
  }
  if (other_use_shadow != use_shadow_) {
    return false;
  }

  if (use_shadow_ && other_use_shadow) {
    // Then check the shadow_frac and shadow_decay_frac.
    // Should assume then that the other has shadow_fracs and stuff.

    if (other_conf["shadow_frac"].GetDouble() != shadow_frac_) {
      return false;
    }

    if (other_conf["shadow_decay_frac"].GetDouble() != shadow_decay_frac_) {
      return false;
    }
  }

  return true;
}

std::vector<pktrade::BookId> SigDeepBook::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

void SigDeepBook::subscribeData(pktrade::md::MDBeacon* beacon) {
  for (pktrade::Market one_bk : books_) {
    BookId book_id{one_bk, symbol_};
    std::vector<pktrade::md::BeaconCBType> cb_types = {
        pktrade::md::BeaconCBType::LvlAdd, pktrade::md::BeaconCBType::LvlMod,
        pktrade::md::BeaconCBType::LvlDel, pktrade::md::BeaconCBType::Final};

    beacon->addListener(book_id, this, cb_types);
  }
}

void SigDeepBook::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (lvl_add.last_msg_type == LastMsgType::BookUpdate) {
    last_msg_book_update_ = true;
  } else {
    last_msg_book_update_ = false;
  }

  if (first_tick_) {
    onFirstTick();
    first_tick_ = false;
    return;
  }

  if (lvl_add.side == Side::Buy) {
    shadow_b_ *= shadow_decay_frac_;
  } else {
    shadow_a_ *= shadow_decay_frac_;
  }

  // Eventually, wrap this in a check to be more efficient.
  // ok let's be more efficient now...
  if ((lvl_add.side == Side::Buy && lvl_add.px.toDouble() > bb_) ||
      (lvl_add.side == Side::Sell && lvl_add.px.toDouble() < ba_)) {
    // std::cout << " MIS del " << lvl_add.px.toDouble() << std::endl;

    maybeShiftInside(bk, lvl_add.side);
  }
  // maybeShiftInside(bk, lvl_add.side);

  double weight = calcImpulse(lvl_add.qty, lvl_add.px, lvl_add.side);

  if (lvl_add.side == Side::Buy) {
    bid_stren_ += weight;
  } else {
    ask_stren_ += weight;
  }

  // Decay my side shadow.
  if (lvl_add.side == Side::Buy) {
    shadow_b_ -= weight * shadow_frac_;
    shadow_b_ = std::max(shadow_b_, 0.);
    // std::cout << "AddBuy sending " << weight * shadow_frac_ << std::endl;
  } else {
    shadow_a_ -= weight * shadow_frac_;
    shadow_a_ = std::max(shadow_a_, 0.);
    // std::cout << "AddSell sending " << weight * shadow_frac_ << std::endl;
  }

  if (weight != 0) {
    if (verbose_lvlcb_) {
      printf(
          "%ld lvlAdd side %s bb ba (%f %f) px %f sz %f sa sb ( %f %f ) "
          "impulse %f bs as %f "
          "%f\n",
          time_utils::nowToMs(), lvl_add.side == Side::Buy ? "Buy" : "Sell",
          bb_, ba_, lvl_add.px.toDouble(), lvl_add.qty.toDouble(), shadow_a_,
          shadow_b_, weight, bid_stren_, ask_stren_);
    }
  }

  // Reset it after a snapshot.
  if (lvl_add.last_msg_type == LastMsgType::BookUpdate) {
    shadow_a_ = 0;
    shadow_b_ = 0;
    // std::cout << " add is resetting << " << std::endl;
  }

  calc_this_round_ = true;
  if (
      // the bookUpdate thing is to kind of guarantee that we have
      // a fuller book to recalc with, should we want to recalc.
      (insides_since_recalc_ >= insides_before_recalc_ ||
       insides_since_recalc_ >= insides_before_recalc_live_)) {
    needs_full_recalc_ = true;
  }
}

void SigDeepBook::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (lvl_mod.last_msg_type == LastMsgType::BookUpdate) {
    last_msg_book_update_ = true;
  } else {
    last_msg_book_update_ = false;
  }

  if (first_tick_) {
    onFirstTick();
    first_tick_ = false;
    return;
  }

  if (lvl_mod.side == Side::Buy) {
    shadow_b_ *= shadow_decay_frac_;
  } else {
    shadow_a_ *= shadow_decay_frac_;
  }

  // std::cout << "from lvlmod px " << lvl_mod.px.toDouble() << " oldsz "
  //           << lvl_mod.prev_qty.toDouble() << " new_qty "
  //           << lvl_mod.qty.toDouble() << std::endl;

  double old_weight = calcImpulse(lvl_mod.prev_qty, lvl_mod.px, lvl_mod.side);
  if (lvl_mod.side == Side::Buy) {
    bid_stren_ -= old_weight;
  } else {
    ask_stren_ -= old_weight;
  }

  // In the L2 world, level modifies never change price. So
  // we never check for maybeShiftInsides.
  // Correction. with flagging we do.
  // Also possible that a previously flagged-away level came back.
  if ((lvl_mod.qty == Quantity{0} || lvl_mod.prev_qty == Quantity{0}) &&
      ((lvl_mod.side == Side::Buy && lvl_mod.px.toDouble() >= bb_) ||
       (lvl_mod.side == Side::Sell && lvl_mod.px.toDouble() <= ba_))) {
    // std::cout << " MIS mod " << lvl_mod.px.toDouble() << std::endl;

    maybeShiftInside(bk, lvl_mod.side);
  }

  double new_weight = calcImpulse(lvl_mod.qty, lvl_mod.px, lvl_mod.side);
  if (lvl_mod.side == Side::Buy) {
    bid_stren_ += new_weight;
  } else {
    ask_stren_ += new_weight;
  }
  if (old_weight != 0) {
    if (verbose_lvlcb_) {
      printf(
          "%ld lvlMod side %s bb ba (%f %f) px %f oldsz %f sz %f  sa sb ( %f %f ) oldimpulse %f "
          "impulse %f bs as %f %f\n",
          time_utils::nowToMs(), lvl_mod.side == Side::Buy ? "Buy " : "Sell",
          bb_, ba_, lvl_mod.px.toDouble(), lvl_mod.prev_qty.toDouble(),
          lvl_mod.qty.toDouble(),
          shadow_a_,
          shadow_b_,
          old_weight, new_weight, bid_stren_,
          ask_stren_);
    }
  }

  // Decay my side shadow.
  if (lvl_mod.side == Side::Buy && lvl_mod.qty > lvl_mod.prev_qty) {
    shadow_b_ -= (new_weight - old_weight) * shadow_frac_;
    shadow_b_ = std::max(shadow_b_, 0.);
    // std::cout << "AddModBuy sending "
    //           << (new_weight - old_weight) * shadow_frac_ << std::endl;

  } else if (lvl_mod.side == Side::Sell && lvl_mod.qty > lvl_mod.prev_qty) {
    shadow_a_ -= (new_weight - old_weight) * shadow_frac_;
    shadow_a_ = std::max(shadow_a_, 0.);
    // std::cout << "AddModSell sending "
    //           << (new_weight - old_weight) * shadow_frac_ << std::endl;
  }

  // Reset it after a snapshot.
  if (lvl_mod.last_msg_type == LastMsgType::BookUpdate) {
    shadow_a_ = 0;
    shadow_b_ = 0;
    // std::cout << " Mod is resetting << " << std::endl;
  }

  calc_this_round_ = true;

  if ((insides_since_recalc_ >= insides_before_recalc_ ||
       insides_since_recalc_ >= insides_before_recalc_live_)) {
    needs_full_recalc_ = true;
    return;
  }

  // If the added strength is greater than current strength, something is wrong
  // and we should recalc. This can happen in the case of extreme values and
  // floats. And it can propagate into large errors.
  if ((lvl_mod.side == Side::Buy && new_weight > bid_stren_) ||
      (lvl_mod.side == Side::Sell && new_weight > ask_stren_) ||
      (bid_stren_ < 0) || (ask_stren_ < 0)) {
    needs_full_recalc_ = true;
    return;
  }
}

void SigDeepBook::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (lvl_del.last_msg_type == LastMsgType::BookUpdate) {
    last_msg_book_update_ = true;
  } else {
    last_msg_book_update_ = false;
  }

  if (first_tick_) {
    onFirstTick();
    first_tick_ = false;
    return;
  }

  if (lvl_del.prev_qty == Quantity{0}) {
    // Don't do anything, even decay, because this is a no-op. We already
    // consumed the data.
    return;
  }

  // Decay my side shadow.
  if (lvl_del.side == Side::Buy) {
    shadow_b_ *= shadow_decay_frac_;
  } else {
    shadow_a_ *= shadow_decay_frac_;
  }

  double weight = calcImpulse(lvl_del.prev_qty, lvl_del.px, lvl_del.side);
  if (lvl_del.side == Side::Buy) {
    bid_stren_ -= weight;
  } else {
    ask_stren_ -= weight;
  }
  // std::cout << " weight was " << weight << " prevqty " << lvl_del.prev_qty <<
  // " px " << lvl_del.px <<  " type " <<
  // magic_enum::enum_name(lvl_del.last_msg_type) << std::endl;

  if (weight != 0) {
    if (verbose_lvlcb_) {
      printf(
          "%ld lvlDel side %s bb ba (%f %f ) px %f sz %f  sa sb ( %f %f ) "
          "impulse %f bs as %f "
          "%f\n",
          time_utils::nowToMs(), lvl_del.side == Side::Buy ? "Buy" : "Sell",
          bb_, ba_, lvl_del.px.toDouble(), lvl_del.prev_qty.toDouble(),
          shadow_a_, shadow_b_,

          weight, bid_stren_, ask_stren_);
    }
  }

  if ((lvl_del.side == Side::Buy && lvl_del.px.toDouble() >= bb_) ||
      (lvl_del.side == Side::Sell && lvl_del.px.toDouble() <= ba_)) {
    // std::cout << " MIS del " << lvl_del.px.toDouble() << std::endl;
    maybeShiftInside(bk, lvl_del.side);
  }

  // Reset it after a snapshot.
  if (lvl_del.last_msg_type == LastMsgType::BookUpdate) {
    shadow_a_ = 0;
    shadow_b_ = 0;
    // std::cout << " del is resetting << " << std::endl;
  }

  // maybeShiftInside(bk, lvl_del.side);
  calc_this_round_ = true;

  if ((insides_since_recalc_ >= insides_before_recalc_ ||
       insides_since_recalc_ >= insides_before_recalc_live_)) {
    needs_full_recalc_ = true;
    return;
  }

  if ((bid_stren_ < 0) || (ask_stren_ < 0)) {
    needs_full_recalc_ = true;
    return;
  }
}

void SigDeepBook::onFinal(const LevelBook& bk) {
  if (needs_full_recalc_ && last_msg_book_update_) {
    fullRecalc();
  }

  // This is ostensibly because I didn't set the last_msg_book_update_ flag
  // properly inside the hyperliquid handler. Let's start with this and then
  // fix it in the future.
  if (needs_full_recalc_ && books_[0] == Market::Hyperliquid) {
    fullRecalc();
  }

  // Only perform a relcalc if we are not waiting for a full_recalc.
  if (!needs_full_recalc_ && calc_this_round_) {
    calc();
  }
}

double SigDeepBook::calcImpulse(Quantity qty, Price px, Side side) {
  double px_dub = px.toDouble();
  double size_dub = qty.toDouble();

  double price_weight = 1;
  double size_weight = 1;

  // Can happen for flagging.
  if (qty.toDouble() == 0) {
    return 0;
  }

  // Don't do anything for strange state books.
  if (side == Side::Buy && bb_ == 0)
    return 0;
  if (side == Side::Sell && ba_ == 0)
    return 0;

  if (side == Side::Buy) {
    price_weight = std::exp(-(bb_ - px_dub) / px_decay_scale_px_);
  } else {
    price_weight = std::exp(-(px_dub - ba_) / px_decay_scale_px_);
  }
  // Hard exit in this case. Note that this happens w.r.t.
  // the stored bb, ba.
  if (price_weight < min_px_weight_) {
    // printf("Triggered this exit in calcImpulse \n");
    return 0;
  }

  if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL) {
    size_weight = 1 - std::exp(-(std::abs(px_dub * size_dub)) / sz_decay_);
  } else if (sz_decay_type_ == SizeDecayType::LOG_NOTIONAL) {
    size_weight = std::log(1 + std::abs(px_dub * size_dub / sz_decay_));
  } else if (sz_decay_type_ == SizeDecayType::SIZE) {
    size_weight = std::abs(size_dub);
  } else if (sz_decay_type_ == SizeDecayType::EXP_SIZE) {
    size_weight = 1 - std::exp(-(std::abs(size_dub)) / sz_decay_);
  } else if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL_PERORD) {
    size_weight = (px_dub * size_dub / sz_decay_) *
                  (1 - std::exp(-(std::abs(px_dub * size_dub)) / (sz_decay_)));
  } else if (sz_decay_type_ == SizeDecayType::EXP_SIZE_PERORD) {
    size_weight = (size_dub / sz_decay_) *
                  (1 - std::exp(-(std::abs(size_dub)) / (sz_decay_)));

  } else if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL_AND_SIZE) {
    size_weight = ((1 - std::exp(-(std::abs(size_dub)) / (sz_decay_2_)))) *
                  (1 - std::exp(-(std::abs(px_dub * size_dub)) / (sz_decay_)));
  }

  // Styles that do order-weighted smoothing.

  /*
  printf(
      "calcImpulse bb ba (%f %f ) side %s px %f qty %f pxwt %.12f wt %.8f \n",
      bb_, ba_, side == Side::Buy ? "Buy" : "Sell", px_dub, size_dub,
      price_weight, price_weight * size_weight);
*/
  return price_weight * size_weight;
}

// Note: sometimes, msi could create rescalings that make one side inf or nan
// due to double imprecision. If that's the case, we should probably wait until
// we do a full recalc.
void SigDeepBook::maybeShiftInside(const LevelBook& bk, Side side) {
  if (verbose_msi_) {
    printf("*******\n");
    printf("%s\n", time_utils::nowToStr().c_str());
    printf("MSI start: bb ba %f %f bs as (%f %f)\n", bb_, ba_, bid_stren_,
           ask_stren_);
  }
  double bp;

  if (side == Side::Buy) {
    // Calc the best inside.

    const Book<BookLevel>::BookSide<BuySide>& buy_side = bk.side<BuySide>();

    for (auto& lvl : buy_side) {
      if (effectiveQty(lvl, use_flagged_) > Quantity{0}) {
        bp = lvl.px.toDouble();
        break;
      }
    }

    // If we reach an empty book, don't recalc. And force a recalc. 
    if (bp == 0) {
      if (verbose_msi_) {
        printf("msi: seeing bestbid is 0, forcing recalc next round\n");
      }
      needs_full_recalc_ = true;
      return;
    }

    if (bp != bb_) {
      double pbs = bid_stren_;
      double shift = (bp - bb_);
      double scale_factor = std::exp(-shift / px_decay_scale_px_);
      if (verbose_msi_) {
        printf("Bidshift bb %f -> %f diff %f scale %f\n", bb_, bp, shift,
               scale_factor);
      }
      bid_stren_ *= scale_factor;
      bb_ = bp;
      insides_since_recalc_++;

      if (use_shadow_) {
        if (pbs > bid_stren_) {
          // If we decrease the bid_stren, add some frac of that to the shadow.
          shadow_b_ += (pbs - bid_stren_) * shadow_frac_;
          if (verbose_msi_) {
            std::cout << " MSI: pbs " << pbs << "bs " << bid_stren_
                      << " adding " << (pbs - bid_stren_) * shadow_frac_
                      << std::endl;
          }
        } else {
          // If we increase it, scale it down.
          // shadow_b_ *= scale_factor;
          // Not sure if I should scale the shadow down
          // or not.
          shadow_b_ += (pbs - bid_stren_) * shadow_frac_;
          shadow_b_ *= shadow_decay_frac_;
          shadow_b_ = std::max(shadow_b_, 0.);
          if (verbose_msi_) {
            std::cout << "MSI: shadow_b scaling by " << 1 / scale_factor
                      << " to " << shadow_b_ << std::endl;
          }
        }
      }
      // std::cout << " bid_stren_ rn is " << bid_stren_ << std::endl;
    }
  } else {
    const Book<BookLevel>::BookSide<SellSide>& sell_side = bk.side<SellSide>();
    bp = 1e10;

    for (auto& lvl : sell_side) {
      if (effectiveQty(lvl, use_flagged_) > Quantity{0}) {
        bp = lvl.px.toDouble();
        break;
      }
    }
    // Don't entertain this. 
    if (bp ==  1e10) {
      if (verbose_msi_) {
        printf("msi: seeing bestask gone, forcing recalc next round\n");
      }

      needs_full_recalc_ = true;
      return;
    }

    if (bp != ba_) {
      double pba = ask_stren_;
      double shift = ba_ - bp;
      double scale_factor = std::exp(-shift / px_decay_scale_px_);
      if (verbose_msi_) {
        printf("Askshift ba %f -> %f diff %f scale %f\n", ba_, bp, shift,
               scale_factor);
      }
      ask_stren_ *= scale_factor;
      ba_ = bp;
      insides_since_recalc_++;

      if (use_shadow_) {
        if (pba > ask_stren_) {
          // If we decrease the bid_stren, add some frac of that to the shadow.
          shadow_a_ += (pba - ask_stren_) * shadow_frac_;
          if (verbose_msi_) {
            std::cout << " MSI: pba " << pba << " as " << ask_stren_
                      << " adding  " << (pba - ask_stren_) * shadow_frac_
                      << std::endl;
          }
        } else {
          // If we increase it, scale it down.
          // shadow_b_ *= scale_factor;
          // Not sure if I should scale the shadow down
          // or not.
          shadow_a_ += (pba - ask_stren_) * shadow_frac_;
          shadow_a_ *= shadow_decay_frac_;
          shadow_a_ = std::max(shadow_a_, 0.);
          // shadow_a_ /= scale_factor;
          if (verbose_msi_) {
            std::cout << "MSI: shadow_a scaling by " << 1 / scale_factor
                      << " to " << shadow_a_ << std::endl;
          }
        }
      }
    }
  }

  // Do a safecheck. There's a possibility that we expanded errors a ton.
  // Or just created nonfinite values.
  if (bid_stren_ > ask_stren_ * 8 || ask_stren_ > bid_stren_ * 8 ||
      !std::isfinite(bid_stren_) || !std::isfinite(ask_stren_)) {
    // Something probably went wrong here.
    needs_full_recalc_ = true;
    if (verbose_msi_) {
      printf(" >> MSI mid: strenght imba %f vs %f : doing a full recalc \n",
             bid_stren_, ask_stren_);
    }
  }

  if (verbose_msi_) {
    printf("MSI end  : bb ba %f %f bs as (%f %f)  insides_since %d \n", bb_,
           ba_, bid_stren_, ask_stren_, insides_since_recalc_

    );
  }
}

void SigDeepBook::calc() {
  // Do not try to update value if the book is one-sided.
  // Just exit out instead.
  // We shouldn't give a good value here.
  if (bb_ == 0 || ba_ ==  1e10) {
    return;
  }

  // ignore if bad state
  // ignore if bad state
  double intermediate_val = 0;

  if (calc_style_ == CalcStyle::DIFF_RATIO) {
    if (use_shadow_) {
      intermediate_val = (bid_stren_ + shadow_b_ - ask_stren_ - shadow_a_) /
                         (bid_stren_ + shadow_b_ + ask_stren_ + shadow_a_);
    } else {
      intermediate_val = (bid_stren_ - ask_stren_) / (bid_stren_ + ask_stren_);
    }

  } else if (calc_style_ == CalcStyle::LOG_RATIO && ask_stren_ != 0) {
    if (use_shadow_) {
      intermediate_val =
          0.5 * std::log((bid_stren_ + shadow_b_) / (ask_stren_ + shadow_a_));
    } else {
      intermediate_val = 0.5 * std::log((bid_stren_) / (ask_stren_));
    }
  }

  // We can hit error cases if the pxdecay is too small.
  if (!(std::isfinite(intermediate_val) && std::isfinite(bid_stren_) &&
        std::isfinite(ask_stren_))) {
    // Option to alert in the future. Or throw.
    needs_full_recalc_ = true;
    calc_this_round_ = true;
    return;
  }

  // bound if needed.
  // Adding it before the multiplication of scale_px so we're working more in
  // pct space.
  if (fabs(intermediate_val) > bounding_val_) {
    intermediate_val = std::copysign(bounding_val_, intermediate_val);
  }

  intermediate_val = intermediate_val * px_decay_scale_px_;

  if (debug_verbose_) {
    printf("%s: sig_a Calc: bs as (%f %f) sb sa (%f %f) iv %f\n",
           time_utils::nowToStr().c_str(), bid_stren_, ask_stren_, shadow_b_,
           shadow_a_, intermediate_val);
  }

  // Don't let ourselves get hurt. Doing this instead of isnan to try out fast
  // math optimizations.
  // if ((ask_stren_ > 0 && ask_stren_ <= 1e-14) || (ask_stren_<0 &&
  // ask_stren_
  // >= -1e-14)) {
  if (std::isnan(intermediate_val)) {
    intermediate_val = 0;
  }

  double mid_px = (bb_ + ba_) / 2;

  double ivv = intermediate_val;

  // Normalize by tot strength vs historical tot strenght.
  if (totstren_tdc_ != 0) {
    if (last_totstren_t_ == 0) {
      totstren_ema_num_ = bid_stren_ + ask_stren_;
      totstren_ema_denom_ = 1;
      last_totstren_t_ = time_utils::nowToMs();
    } else {
      // Do the time_decayed thing.

      // Only update the historical stuff if

      double time_decay_factor =
          std::exp(-(time_utils::nowToMs() - last_totstren_t_) / totstren_tdc_);
      totstren_ema_num_ =
          totstren_ema_num_ * time_decay_factor + (bid_stren_ + ask_stren_);
      totstren_ema_denom_ = totstren_ema_denom_ * time_decay_factor + 1;
      last_totstren_t_ = time_utils::nowToMs();
    }

    double stren_ema_ = totstren_ema_num_ / totstren_ema_denom_;

    // Bound stren_ema. Just to be natty.
    stren_ema_ = std::max(stren_ema_, 1 / totstren_ema_bound_);
    stren_ema_ = std::min(stren_ema_, totstren_ema_bound_);

    stren_ema_ = std::pow(stren_ema_, totstren_ema_power_);

    // Scale ivv by the ratio of the current totstren to the historical
    ivv = (bid_stren_ + ask_stren_) / stren_ema_ * ivv;

    // Also sacle ivv to the size of the spread
    // double spread_factor = std::exp(-(ba_ - bb_)/0.1) + 0.5;
    // ivv *= spread_factor;

    if (debug_verbose_) {
      printf(
          "%ld tot_stren %f historical_stren %f ratio %f ivv %f finalval %f\n",

          time_utils::nowToMs(), bid_stren_ + ask_stren_, stren_ema_,
          (bid_stren_ + ask_stren_) / stren_ema_, ivv, ivv / mid_px);
    }
  }

  if (return_price_) {
    if (mid_px != 0) {
      setVV(ivv + mid_px, true);
    }
  } else {
    setVV(ivv / mid_px, true);
  }
  calc_this_round_ = false;
}

void SigDeepBook::fullRecalc() {
  // Walk both sides of the book.

  double old_bs = bid_stren_;
  double old_as = ask_stren_;
  if (verbose_full_recalc_) {
    printf("%s recalc_start: bb ba (%f %f) bs as (%f %f) \n",
           time_utils::nowToStr().c_str(), bb_, ba_, bid_stren_, ask_stren_);
  }

  bid_stren_ = 0;
  ask_stren_ = 0;
  first_tick_ = false;
  insides_since_recalc_ = 0;

  int count_before_ignore = 0;

  for (pktrade::Market one_bk : books_) {
    const LevelBook* bk =
        pktrade::GlobalVar::book_manager_->find(BookId{one_bk, symbol_});

    // Get the insides.
    // Buy side.

    bool foundbb = false;
    bool foundba = false;

    // I'm going to reinitialize the bb ba here.
    bb_ = 0;
    ba_ = 1e10;

    // These two values are used for calculating relative prices.
    // When we get more books, we'll need to calc the abs best
    // mid from here.
    // Or we can move to relative to midpx.

    auto& buy_side = bk->side<BuySide>();
    // Walk the book.
    for (auto& lvl : buy_side) {
      count_before_ignore++;

      // Don't try to calc for "empty" levels.
      if (effectiveQty(lvl, use_flagged_) == Quantity{0}) {
        continue;
      }

      if (!foundbb) {
        bb_ = lvl.px.toDouble();
        foundbb = true;
      }

      double weight =
          calcImpulse(effectiveQty(lvl, use_flagged_), lvl.px, Side::Buy);
      bid_stren_ += weight;
      if (verbose_full_recalc_) {
        printf("bs %f  wt %f px %f qty %f effQty %f \n", bid_stren_, weight,
               lvl.px.toDouble(), lvl.qty.toDouble(),
               effectiveQty(lvl, true).toDouble());
      }
      // Potential breakage here, but just relying on exact 0 vs small
      // double.
      if (weight == 0) {
        // If we're chaining our insides_before_recalc_live_ to this, then
        // we multiply the two...

        if (ignore_recalc_thresh_mult_ != 0 &&
            insides_before_recalc_live_ == 1000) {
          // If we're using this and we haven't already set it.

          insides_before_recalc_live_ =
              (int)(ignore_recalc_thresh_mult_ * count_before_ignore);

          // Bound it.
          insides_before_recalc_live_ =
              std::max(insides_before_recalc_live_, 10);

          // printf(
          //     "Setting insides_before_recalc_live_ to %d because counted
          //     %d\n", insides_before_recalc_live_, count_before_ignore);
        }

        break;
      }
    }
    if (verbose_full_recalc_) {
      printf("________________\n");
    }
    // Sell side.
    auto& sell_side = bk->side<SellSide>();
    for (auto& lvl : sell_side) {
      // Don't try to calc for "empty" levels.
      if (effectiveQty(lvl, use_flagged_) == Quantity{0}) {
        continue;
      }

      if (!foundba) {
        ba_ = lvl.px.toDouble();
        foundba = true;
      }

      double weight =
          calcImpulse(effectiveQty(lvl, use_flagged_), lvl.px, Side::Sell);
      ask_stren_ += weight;
      if (verbose_full_recalc_) {
        printf("as %f  wt %f px %f qty %f effQty %f \n", ask_stren_, weight,
               lvl.px.toDouble(), lvl.qty.toDouble(),
               effectiveQty(lvl, true).toDouble());
      }
      // printf("Ask_stren %f\n", ask_stren_);
      //  Same as above.
      if (weight == 0) {
        break;
      }
    }
  }

  // Reset shadow energy.
  shadow_b_ = 0;
  shadow_a_ = 0;
  // Resets needingness.
  needs_full_recalc_ = false;
  if (verbose_full_recalc_) {
    // Let's get the old vs new ivvs?
    double old_ivv = (old_bs - old_as) / (old_bs + old_as);
    double new_ivv = (bid_stren_ - ask_stren_) / (bid_stren_ + ask_stren_);
    double ivv_ratio = old_ivv / new_ivv;

    printf(
        "%s: recalc_end: bb ba (%f %f) bs as (%f %f) old bs as(%f %f) "
        "ivv_ratio %f\n",
        time_utils::nowToStr().c_str(), bb_, ba_, bid_stren_, ask_stren_,
        old_bs, old_as, ivv_ratio);
  }

  /*
  I've seen some examples of pretty large diffs here, when the pxdecayconst is
  small and a liquidation through many levels happens. Like, many.
  assert(abs((bid_stren_ - old_bs) / old_bs) < 0.01 || old_bs < 0.0001 ||
         std::isnan(old_bs));
  assert(abs((ask_stren_ - old_as) / old_as) < 0.01 || old_as < 0.0001 ||
         std::isnan(old_as));
*/
}

void SigDeepBook::onFirstTick() {
  needs_full_recalc_ = true;
  // Peg the price-based values.
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], symbol_});

  // Find the min sz in a lot of levels. It's possible we climbed and saved a
  // param with a weird sz. So let's do a sanity check here.
  auto& sell_side = bk->side<SellSide>();

  int count = 0;
  double min_sz = 1e10;
  double last_px = 0;
  double cur_tick = 0;
  double min_tick = 0;
  for (auto& lvl : sell_side) {
    min_sz = std::min(min_sz, lvl.qty.toDouble());
    if (last_px == 0) {
      last_px = lvl.px.toDouble();
    } else {
      cur_tick = lvl.px.toDouble() - last_px;
      if (min_tick == 0) {
        min_tick = cur_tick;
      } else if (cur_tick > 0) {
        min_tick = std::min(min_tick, cur_tick);
      }
      last_px = lvl.px.toDouble();
    }

    if (count++ > 8) {
      break;
    }
  }

  Price best_bid = bk->side<BuySide>().begin()->px;
  Price best_ask = bk->side<SellSide>().begin()->px;

  bb_ = best_bid.toDouble();
  ba_ = best_ask.toDouble();

  double mid = (best_bid + best_ask).toDouble() / 2;

  if (px_decay_scale_px_ == -1) {
    // Get the mid. Tie the px decays to each other.
    px_decay_scale_px_ = px_decay_scale_pct_ * mid;
  } else {
    px_decay_scale_pct_ = px_decay_scale_px_ / mid;
  }

  // Impose a bound on pct decay. Do not allow smaller than 1e-8 because
  // it creates real issues. Setting this to 1e-8 in part because
  // of btc-related things.
  if (px_decay_scale_pct_ < 1e-8) {
    // Update both of them.
    px_decay_scale_pct_ = 1e-8;
    px_decay_scale_px_ = px_decay_scale_pct_ * mid;
  }

  // Also compare it to the actual approx ticksize.
  if (((min_tick)) / px_decay_scale_px_ > 10) {
    px_decay_scale_px_ = (min_tick) / 10;
    px_decay_scale_pct_ = px_decay_scale_px_ / mid;
  }

  // OK, from this,
  double max_px_away_ = max_pct_away_ * mid;

  double potential_min_wt = std::exp(-max_px_away_ / px_decay_scale_px_);
  min_px_weight_ = std::max(min_px_weight_, potential_min_wt);

  double sz_decay_min_bound_ = 0.02;

  if (sz_decay_type_ == SizeDecayType::EXP_SIZE ||
      sz_decay_type_ == SizeDecayType::EXP_SIZE_PERORD) {
    sz_decay_ = std::max(sz_decay_, min_sz * sz_decay_min_bound_);
  } else if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL ||
             sz_decay_type_ == SizeDecayType::LOG_NOTIONAL) {
    sz_decay_ =
        std::max(sz_decay_, best_bid.toDouble() * min_sz * sz_decay_min_bound_);
  } else if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL_AND_SIZE) {
    sz_decay_ =
        std::max(sz_decay_, best_bid.toDouble() * min_sz * sz_decay_min_bound_);
    sz_decay_2_ = std::max(sz_decay_2_, min_sz * sz_decay_min_bound_);
  }
}

void SigDeepBook::reset() {
  insides_since_recalc_ = 0;
  bid_stren_ = 0;
  ask_stren_ = 0;
  first_tick_ = true;
}

}  // namespace pktrade::signals
