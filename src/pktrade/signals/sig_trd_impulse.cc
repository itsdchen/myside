#include "sig_trd_impulse.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/time_utils.h"

/*

Ideas:
 - in onTrade, px_scale doesn't need to be linear.
 - Take stats on trade sizes and adjust impulse size.

*/

namespace pktrade::signals {

SigTrdImpulse::SigTrdImpulse(int signal_id,
                             const rapidjson::Value& sig_conf,
                             SignalFactory* sf,
                             const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf), bb_(0), ba_(1e10), mid_(1) {
  symbol_ = SymbolId(sig_conf["symbol"].GetString());
  for (auto& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString())
                              .value_or(Market::Unknown);
    books_.push_back(pmk);
  }

  px_decay_ = -1;
  if (sig_conf.HasMember("px_decay")) {
    px_decay_ = sig_conf["px_decay"].GetDouble();
  }

  // Let's impose some restrictions on how much this decays.
  px_decay_ = std::max(px_decay_, 1e-8);

  sz_decay_ = sig_conf["sz_decay"].GetDouble();
  sz_decay_type_ = magic_enum::enum_cast<SizeDecayType>(
                       sig_conf["sz_decay_type"].GetString())
                       .value_or(SizeDecayType::EXP_SIZE);

  normalize_size_draped_inside_ = false;
  if (sig_conf.HasMember("normalize_size_draped_inside")) {
    normalize_size_draped_inside_ =
        sig_conf["normalize_size_draped_inside"].GetBool();

    // This literally cant be. before we start. lol.
    draped_inside_shs_ = 0;
    draped_tdc_ = sig_conf["draped_tdc_s"].GetDouble() * 1000;
    last_draped_calc_t_ = time_utils::nowToMs();
  }

  // For batching.
  batch_ = false;
  running_shs_ = 0.;
  running_notional_ = 0;
  running_side_ = Side::Unknown;
  if (sig_conf.HasMember("batch")) {
    batch_ = sig_conf["batch"].GetBool();
  }

  // For otf batching, which we can try w/ binance.
  perm_value_ = 0;
  tmp_value_ = 0;
  tmp_value_shs_ = 0;
  tmp_value_px_ = 0;
  last_trd_transact_t_ = 0;

  use_killed_lvl_boost_ = false;
  if (sig_conf.HasMember("use_killed_lvl_boost")) {
    use_killed_lvl_boost_ = sig_conf["use_killed_lvl_boost"].GetBool();
    killed_lvl_boost_ = sig_conf["killed_lvl_boost"].GetDouble();
  }

  time_decay_ = sig_conf["time_decay"].GetDouble();
  tick_decay_ = sig_conf["tick_decay"].GetDouble();

  tgt_sig_decay_ = sig_conf["tgt_sig_decay"].GetDouble();

  tgt_decay_disagree_ = tgt_sig_decay_;
  if (sig_conf.HasMember("tgt_decay_disagree")) {
    tgt_decay_disagree_ = sig_conf["tgt_decay_disagree"].GetDouble();
  }

  tgt_signal_ = sf->getOrMakeNamedSignal(all_signals_array,
                                         sig_conf["tgt_signal"].GetString());
  if (tgt_signal_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing tgt_signal ") +
                             sig_conf["tgt_signal"].GetString() +
                             " in SigTrdImpulse");
  }

  tgt_signal_->addListener(this);
  tgt_signal_val_ = 0;

  bungee_denom_ = -1;
  bungee_mult_frac_ = 0;

  if (sig_conf.HasMember("bungee_denom") &&
      sig_conf.HasMember("bungee_mult_frac")) {
    bungee_denom_ = sig_conf["bungee_denom"].GetDouble();
    bungee_mult_frac_ = sig_conf["bungee_mult_frac"].GetDouble();

    if (bungee_mult_frac_ > 1 || bungee_mult_frac_ < 0) {
      throw std::runtime_error(
          std::string("bungee_mult_frac must be between 0 and 1, but got ") +
          std::to_string(bungee_mult_frac_));
    }
  }

  min_tgt_diff_bps_ = sig_conf["min_tgt_diff_bps"].GetDouble();

  last_decay_t_ = 0;
  value_ = 0;
}

std::vector<pktrade::BookId> SigTrdImpulse::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

bool SigTrdImpulse::isSame(const rapidjson::Value& other_conf,
                           SignalFactory& sf,
                           const rapidjson::Value& all_signals_array) const {
  // Check parameters and child signals. If they're good, we're good.
  if (std::string(other_conf["type"].GetString()) != "SigTrdImpulse") {
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

  if (other_conf.HasMember("px_decay") &&
      px_decay_ != other_conf["px_decay"].GetDouble()) {
    return false;
  }

  if (sz_decay_ != other_conf["sz_decay"].GetDouble()) {
    return false;
  }
  if (sz_decay_type_ != magic_enum::enum_cast<SizeDecayType>(
                            other_conf["sz_decay_type"].GetString())
                            .value_or(SizeDecayType::EXP_SIZE)) {
    return false;
  }

  if (use_killed_lvl_boost_) {
    if (!other_conf.HasMember("use_killed_lvl_boost")) {
      return false;
    }
    if (!other_conf["use_killed_lvl_boost"].GetBool()) {
      return false;
    }
    if (killed_lvl_boost_ != other_conf["killed_lvl_boost"].GetDouble()) {
      return false;
    }
  }

  if (time_decay_ != other_conf["time_decay"].GetDouble()) {
    return false;
  }
  if (tick_decay_ != other_conf["tick_decay"].GetDouble()) {
    return false;
  }

  if (normalize_size_draped_inside_) {
    if (!other_conf.HasMember("normalize_size_draped_inside")) {
      return false;
    }

    if (!other_conf["normalize_size_draped_inside"].GetBool()) {
      return false;
    }

    if (draped_tdc_ != other_conf["draped_tdc_s"].GetDouble() * 1000) {
      return false;
    }
  }

  // Check the tgt signal now.
  Signal* other_tgt_sig = sf.getOrMakeNamedSignal(
      all_signals_array, other_conf["tgt_signal"].GetString());
  if (other_tgt_sig != tgt_signal_) {
    return false;
  }
  if (tgt_sig_decay_ != other_conf["tgt_sig_decay"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("tgt_decay_disagree")) {
    if (tgt_decay_disagree_ != other_conf["tgt_decay_disagree"].GetDouble()) {
      return false;
    }
  }

  if (batch_ != other_conf["batch"].GetBool()) {
    return false;
  }

  if (other_conf.HasMember("bungee_denom") &&
      bungee_denom_ != other_conf["bungee_denom"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("bungee_mult_frac") &&
      bungee_mult_frac_ != other_conf["bungee_mult_frac"].GetDouble()) {
    return false;
  }

  if (min_tgt_diff_bps_ != other_conf["min_tgt_diff_bps"].GetDouble()) {
    return false;
  }

  return true;
}

void SigTrdImpulse::subscribeData(MDBeacon* beacon) {
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

void SigTrdImpulse::applyDecay() {
  // Decay the value.
  // TOOD: get real time.
  double decay = 1;
  decay *= tick_decay_;

  int64_t cur_t = time_utils::nowToMs();
  // TODO: agree on timescale.
  decay *= std::exp(-((double)((cur_t - last_decay_t_) / 1000.)) / time_decay_);

  if (debug_verbose_) {
    // printf("Decay: tick %f tdec %f tdiff %ld dec %f val %f\n", tick_decay_,
    //        std::exp(-((double)((cur_t - last_decay_t_) / 1e3)) /
    //        time_decay_), cur_t - last_decay_t_, decay, value_ * decay);
  }

  // Only decay the permanent portion.
  perm_value_ *= decay;

  // value_ *= decay;
  last_decay_t_ = cur_t;
}

// For decays.
void SigTrdImpulse::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (first_tick_) {
    onFirstTick();
  }

  // std::cout <<" Add side " << (lvl_add.side == Side::Buy? " Buy" : "Sell") <<
  // " px " << lvl_add.px.toDouble() << std::endl;

  if ((lvl_add.side == Side::Buy && lvl_add.px.toDouble() > bb_) ||
      (lvl_add.side == Side::Sell && lvl_add.px.toDouble() < ba_)) {
    // std::cout << " MIS del " << lvl_add.px.toDouble() << std::endl;
    maybeUpdateInside(bk, lvl_add.side);
    maybeUpdateDraped();
  }

  // Basic decay.
  applyDecay();
}

void SigTrdImpulse::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (first_tick_) {
    onFirstTick();
  }
  // std::cout <<" Mod side " << (lvl_mod.side == Side::Buy? " Buy" : "Sell") <<
  // " px " << lvl_mod.px.toDouble() << std::endl;

  // Potential unflagging.
  if ((lvl_mod.prev_qty == Quantity{0}) &&
      ((lvl_mod.side == Side::Buy && lvl_mod.px.toDouble() >= bb_) ||
       (lvl_mod.side == Side::Sell && lvl_mod.px.toDouble() <= ba_))) {
    // std::cout << " MIS mod " << lvl_mod.px.toDouble() << std::endl;

    maybeUpdateInside(bk, lvl_mod.side);
  }

  // flagging.
  if ((lvl_mod.qty == Quantity{0}) &&
      ((lvl_mod.side == Side::Buy && lvl_mod.px.toDouble() >= bb_) ||
       (lvl_mod.side == Side::Sell && lvl_mod.px.toDouble() <= ba_))) {
    // std::cout << " MIS mod " << lvl_mod.px.toDouble() << std::endl;

    maybeUpdateInside(bk, lvl_mod.side);
  }

  if ((lvl_mod.side == Side::Buy && lvl_mod.px.toDouble() >= bb_) ||
      (lvl_mod.side == Side::Sell && lvl_mod.px.toDouble() <= ba_)) {
    maybeUpdateDraped();
  }

  applyDecay();
}

void SigTrdImpulse::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (first_tick_) {
    onFirstTick();
  }

  if (lvl_del.prev_qty == Quantity{0}) {
    // Don't do anything, even decay, because this is a no-op. We already
    // consumed the data.
    return;
  }
  // std::cout <<" Del side " << (lvl_del.side == Side::Buy? " Buy" : "Sell") <<
  // " px " << lvl_del.px.toDouble() << std::endl;

  if ((lvl_del.side == Side::Buy && lvl_del.px.toDouble() >= bb_) ||
      (lvl_del.side == Side::Sell && lvl_del.px.toDouble() <= ba_)) {
    // std::cout << " MIS del " << lvl_del.px.toDouble() << std::endl;
    maybeUpdateInside(bk, lvl_del.side);
    maybeUpdateDraped();
  }

  applyDecay();
}

// We like this.
void SigTrdImpulse::onTrade(const LevelBook& bk, const Trade& trd) {
  if (first_tick_) {
    onFirstTick();
  }

  // You need a mid to have this.
  if (tgt_signal_val_ == 0)
    return;

  if (batch_) {
    // Potentially batch or flush.
    if (running_shs_ == 0) {
      // Start the batch.
      running_shs_ = trd.qty.toDouble();
      running_notional_ = trd.qty.toDouble() * trd.px.toDouble();
      running_side_ = trd.passive_side;
      // Insert a callback to flush my batch

      pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::milliseconds(1),
                                                 [&] { this->flushBatch(); });
    } else {
      running_shs_ += trd.qty.toDouble();
      running_notional_ += trd.qty.toDouble() * trd.px.toDouble();
    }

    return;
  }

  // Check if we want to batch this trade with the previous trade.
  // By the way, this way of batching is valid for just the
  if (trd.transact_t != last_trd_transact_t_ ||
      trd.px.toDouble() != tmp_value_px_) {
    // Flush batch
    perm_value_ += tmp_value_;
    tmp_value_ = 0;
    tmp_value_shs_ = 0;
    tmp_value_px_ = 0;
  }

  tmp_value_px_ = trd.px.toDouble();

  if (normalize_size_draped_inside_) {
    tmp_value_shs_ += trd.qty.toDouble() / draped_inside_shs_;

  } else {
    tmp_value_shs_ += trd.qty.toDouble();
  }

  // Check vs target signal.
  double impulse = 1;

  // TODO: Parameterize this.
  // Apply a scaling factor. Maybe make it larger when spread
  // is larger. Also, higher than tgt_sig means positive. Lower means negative.
  /*
    double px_scale =
        fabs((trd.px.toDouble() - tgt_signal_val_) / tgt_signal_val_) +
        min_tgt_diff_bps_;
  */

  // Let's check that the price hasn't moved past us. Could happen if
  // diff mkt feeds are out of sync.

  // If the aggressor sold but now the price is outside the ask.
  double px_scale = 1;

  // I kind of want to try setting the px_scale somewhat proportional to the
  // diff between the trade px and the OTHER book side. I feel like it's more
  // reliable? Guess it depends how behind this thing is. OK, let's try this
  // now.
  if (trd.passive_side == Side::Buy) {
    // The order was a sell, let's compare it to the best ask price.
    px_scale = fabs((ba_ - tmp_value_px_));
  } else {
    px_scale = fabs((tmp_value_px_ - bb_));
  }

  px_scale = std::max(px_scale, min_tgt_diff_bps_);

  if (debug_verbose_) {
    printf("Px situations look bb ba (%f %f) trade_px %f px_scale %f\n", bb_,
           ba_, trd.px.toDouble(), px_scale);
  }

  if (px_decay_ != -1) {
    px_scale = 1 - std::exp(-px_scale / px_decay_);
  }

  // If aggressive side is a sell, then we have negative impulse.
  px_scale *= trd.passive_side == Side::Buy ? -1 : 1;

  impulse *= px_scale;

  impulse *= 1 - (std::exp(-tmp_value_shs_ / sz_decay_));

  // Apply bungee.
  if (bungee_denom_ != -1 &&
      ((impulse > 0 && perm_value_ > 0) || (impulse < 0 && perm_value_ < 0))) {
    double bungee_mult = std::exp(-std::abs(perm_value_ / bungee_denom_));
    double bungee_scale =
        bungee_mult_frac_ * bungee_mult + (1 - bungee_mult_frac_);
    impulse *= bungee_scale;
  }

  tmp_value_ = impulse;

  if (use_killed_lvl_boost_) {
    // Can I check out if the trade took out the level or not?
    // I guess, let's walk the book and check if the thing would have taken it
    // out.
    bool took_out_lvl = false;
    // Let's go in.

    if (trd.passive_side == Side::Buy) {
      // Walk the buy book. At this point, the level shouldn't be flagged yet,
      // so walk to that level and compare whether it got filled or not.
      for (auto& lvl : bk.side<BuySide>()) {
        if (lvl.px.toDouble() == trd.px.toDouble()) {
          // This is the level.
          if (effectiveQty(lvl) < trd.qty) {
            // This trade took out the level.
            took_out_lvl = true;
          }
          break;
        } else if (lvl.px.toDouble() < trd.px.toDouble()) {
          // We're past the level.
          break;
        }
      }

    } else {
      // Walk the sell side, same same but different.
      for (auto& lvl : bk.side<SellSide>()) {
        if (lvl.px.toDouble() == trd.px.toDouble()) {
          // This is the level.
          if (effectiveQty(lvl) < trd.qty) {
            // This trade took out the level.
            took_out_lvl = true;
          }
          break;
        } else if (lvl.px.toDouble() > trd.px.toDouble()) {
          // We're past the level.
          break;
        }
      }
    }
    if (took_out_lvl) {
      // Compounded by num of levels taken out.
      tmp_value_ *= killed_lvl_boost_;
    }
  }

  // In the dumb way, let's try to walk the book first to check.

  //  if (std::abs(trd.px.toDouble() - tgt_signal_val_) < 0.000001) {
  if (debug_verbose_) {
    printf(
        "%s %ld OnTrade (px %f qty %f tgt %f pxdiff %f ) px_scale %f impulse "
        "%f permvalue %f newvalue %f\n ",

        time_utils::nowToStr().c_str(), time_utils::nowToMs(),
        trd.px.toDouble(), trd.qty.toDouble(), tgt_signal_val_,
        trd.px.toDouble() - tgt_signal_val_, px_scale, impulse, perm_value_,
        impulse + perm_value_);
  }

  // Add it in.
  // setValue(value_ + impulse);

  //  setValue(perm_value_ + tmp_value_);

  /*
    std::cout << time_utils::nowToStr() << " onTrade perm " << perm_value_ << "
    tmp " << tmp_value_
              << std::endl;
              */
  calcValue();
};

void SigTrdImpulse::flushBatch() {
  // Similar thing to the normal impulse.
  double impulse = 1;

  /*
   double px_scale =
      fabs(((running_notional_ / running_shs_) - tgt_signal_val_) /
           tgt_signal_val_) +
      min_tgt_diff_bps_;
*/

  double px_scale = 1;
  double batch_px = running_notional_ / running_shs_;
  if (batch_px > ba_ || batch_px < bb_) {
    px_scale = (ba_ - bb_) / mid_ * 0.1;
  } else {
    // This is the normal version.
    px_scale = fabs((batch_px - mid_) / mid_);
  }

  if (running_shs_ == 0 || std::isnan(batch_px)) {
    return;
  }

  px_scale = std::max(px_scale, min_tgt_diff_bps_);

  if (px_decay_ != -1) {
    px_scale = 1 - std::exp(-px_scale / px_decay_);
  }

  // If aggressive side is a sell, then we have negative impulse.
  px_scale *= running_side_ == Side::Buy ? -1 : 1;

  impulse *= px_scale;

  impulse *= 1 - (std::exp(-running_shs_ / sz_decay_));

  if (bungee_denom_ != -1 &&
      ((impulse > 0 && value_ > 0) || (impulse < 0 && value_ < 0))) {
    double bungee_mult = std::exp(-std::abs(value_ / bungee_denom_));
    double bungee_scale =
        bungee_mult_frac_ * bungee_mult + (1 - bungee_mult_frac_);
    impulse *= bungee_scale;
  }

  if (debug_verbose_) {
    printf(
        "%s %ld FlushBatch (running_px %f running_shs %f tgt %f pxdiff %f ) "
        "px_scale %f impulse "
        "%f oldval %f\n ",

        time_utils::nowToStr().c_str(), time_utils::nowToMs(), batch_px,
        running_shs_, tgt_signal_val_, batch_px - tgt_signal_val_, px_scale,
        impulse, value_);
  }

  setValue(value_ + impulse);

  // Reset values.
  running_shs_ = 0;
}

void SigTrdImpulse::maybeUpdateInside(const LevelBook& bk, Side side) {
  double bp;
  if (side == Side::Buy) {
    const Book<BookLevel>::BookSide<BuySide>& buy_side = bk.side<BuySide>();
    for (auto& lvl : buy_side) {
      if (effectiveQty(lvl) > Quantity{0}) {
        bp = lvl.px.toDouble();
        break;
      }
    }
    if (debug_verbose_ && bp != bb_) {
      printf("msi buy: %f->%f\n", bb_, bp);
    }
    bb_ = bp;
  } else {
    const Book<BookLevel>::BookSide<SellSide>& sell_side = bk.side<SellSide>();
    for (auto& lvl : sell_side) {
      if (effectiveQty(lvl) > Quantity{0}) {
        bp = lvl.px.toDouble();
        break;
      }
    }
    if (debug_verbose_ && bp != ba_) {
      printf("msi sell: %f->%f\n", ba_, bp);
    }
    ba_ = bp;
  }
  mid_ = (bb_ + ba_) / 2;
}

void SigTrdImpulse::onFirstTick() {
  first_tick_ = false;

  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], symbol_});

  const Book<BookLevel>::BookSide<BuySide>& buy_side = bk->side<BuySide>();
  if (!buy_side.empty()) {
    for (auto& lvl : buy_side) {
      if (effectiveQty(lvl) > Quantity{0}) {
        bb_ = lvl.px.toDouble();
        draped_inside_shs_ += effectiveQty(lvl).toDouble();
        //    std::cout << " Got " << lvl.px.toDouble() << std::endl;
        break;
      } else {
        //    std::cout << " Skipped " << lvl.px.toDouble() << std::endl;
      }
    }

  } else {
    bb_ = 0;
  }

  const Book<BookLevel>::BookSide<SellSide>& sell_side = bk->side<SellSide>();
  if (!sell_side.empty()) {
    for (auto& lvl : sell_side) {
      if (effectiveQty(lvl) > Quantity{0}) {
        ba_ = lvl.px.toDouble();
        draped_inside_shs_ += effectiveQty(lvl).toDouble();
        //    std::cout << " Got " << lvl.px.toDouble() << std::endl;
        break;
      } else {
        //     std::cout << " Skipped " << lvl.px.toDouble() << std::endl;
      }
    }

  } else {
    ba_ = 1e10;
  }

  mid_ = (bb_ + ba_) / 2;

  if (bb_ == 0 || ba_ == 0) {
    first_tick_ = true;
  }
}

void SigTrdImpulse::maybeUpdateDraped() {
  // This is a bit of a hack. We should probably have a separate signal for
  // this.
  int64_t cur_t = time_utils::nowToMs();

  // Decay draped value.

  double decay = std::exp(-(cur_t - last_draped_calc_t_) / draped_tdc_);
  last_draped_calc_t_ = cur_t;
  draped_inside_shs_ *= decay;

  // Check inside liq.
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], symbol_});

  double inside_shs = bk->side<BuySide>().begin()->qty.toDouble() +
                      bk->side<SellSide>().begin()->qty.toDouble();

  if (inside_shs > draped_inside_shs_) {
    // std::cout << " UPDATE!!!" << std::endl;
    draped_inside_shs_ = inside_shs;
  }
  // std::cout << time_utils::nowToStr() << " - draped " << draped_inside_shs_
  //           << std::endl;
}

void SigTrdImpulse::onSignalValue(int sig_id, double sig_value) {
  // Apply tgt decay.

  double decay = 1;
  if ((sig_value - tgt_signal_val_) * perm_value_ > 0) {
    // Same direction.
    decay *= tgt_sig_decay_;
  } else {
    decay *= tgt_decay_disagree_;
  }

  perm_value_ *= decay;
  tgt_signal_val_ = sig_value;

  /*
  std::cout << time_utils::nowToStr() << " onSigValue  sig " << sig_value
            << " tgt " << tgt_signal_val_ << " perm " << perm_value_ << " tmp "
  << tmp_value_
            << std::endl;
*/
  calcValue();
}

void SigTrdImpulse::calcValue() { setValue(perm_value_ + tmp_value_); }

void SigTrdImpulse::onSignalValidity(int sig_id, bool valid) {
  // Probably should do something smart here.
  // TODO. Test it when you do.
}

void SigTrdImpulse::reset() {
  tgt_signal_val_ = tgt_signal_->getValue();
  setValue(0);
}

}  // namespace pktrade::signals