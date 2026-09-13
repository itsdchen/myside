#include "sig_book_impulse.h"

#include "pktrade/context/global_vars.h"

#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

/*
Ideas:
 - Push through a sigmoid



*/

namespace pktrade::signals {

SigBookImpulse::SigBookImpulse(int signal_id,
                               const rapidjson::Value& sig_conf,
                               SignalFactory* sf,
                               const rapidjson::Value& all_signals_array)
    : Signal(signal_id, sig_conf, sf), bb_(0), ba_(1e10) {
  symbol_ = SymbolId(sig_conf["symbol"].GetString());
  // Get markets.
  for (auto& book : sig_conf["books"].GetArray()) {
    pktrade::Market pmk = magic_enum::enum_cast<Market>(book.GetString())
                              .value_or(Market::Unknown);
    books_.push_back(pmk);
  }

  px_decay_ = sig_conf["px_decay"].GetDouble();
  sz_decay_ = sig_conf["sz_decay"].GetDouble();
  sz_decay_type_ = magic_enum::enum_cast<SizeDecayType>(
                       sig_conf["sz_decay_type"].GetString())
                       .value_or(SizeDecayType::EXP_SIZE);

  // Let's impose some restrictions on how much this decays.
  px_decay_ = std::max(px_decay_, 1e-8);

  ticker_weight_ = 1;
  update_weight_ = 1;
  trade_weight_ = 1;

  // New versions of the signal should have them all.
  if (sig_conf.HasMember("ticker_weight")) {
    ticker_weight_ = sig_conf["ticker_weight"].GetDouble();
    update_weight_ = sig_conf["update_weight"].GetDouble();
    trade_weight_ = sig_conf["trade_weight"].GetDouble();
  }

  // This is given in seconds.
  time_decay_ = sig_conf["time_decay"].GetDouble();
  tick_decay_ = sig_conf["tick_decay"].GetDouble();
  last_decay_t_ = time_utils::nowToMs();

  if (sig_conf.HasMember("min_tick_decay")) {
    min_tick_decay_ = sig_conf["min_tick_decay"].GetDouble();
  } else {
    min_tick_decay_ = 0.9999;
  }

  if (sig_conf.HasMember("pred_decay")) {
    pred_decay_ = sig_conf["pred_decay"].GetDouble();
  } else {
    pred_decay_ = 0.98;
  }

  pred_decay_agree_ = pred_decay_;
  if (sig_conf.HasMember("pred_decay_agree")) {
    pred_decay_agree_ = sig_conf["pred_decay_agree"].GetDouble();
  }

  pred_decay_ = std::min(pred_decay_, 0.98);
  pred_decay_agree_ = std::min(pred_decay_agree_, 0.98);

  pred_decay_denom_ = -1;
  // TODO: implement this properly.
  if (sig_conf.HasMember("pred_decay_denom")) {
    pred_decay_denom_ = sig_conf["pred_decay_denom"].GetDouble();
  }

  // If we don't use it at all.
  time_behind_const_ = -1;
  if (sig_conf.HasMember("time_behind_const")) {
    // Change to ms.
    time_behind_const_ = sig_conf["time_behind_const"].GetDouble() * 1000;
  }

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

  // This is given in seconds in the conffile, so here we're just multiplying by
  // 1e3 preemptively.
  use_pred_boost_ = false;
  last_pred_change_t_ = 0;
  if (sig_conf.HasMember("pred_boost_timeconst_s")) {
    use_pred_boost_ = true;
    // time const given in s, convert to ms.
    pred_boost_timeconst_ =
        sig_conf["pred_boost_timeconst_s"].GetDouble() * 1e3;
    pred_boost_offset_ = sig_conf["pred_boost_offset"].GetDouble();
  } else {
    pred_boost_timeconst_ = 1e20;
    pred_boost_offset_ = 10;
  }

  ref_signal_ = sf->getOrMakeNamedSignal(all_signals_array,
                                         sig_conf["ref_signal"].GetString());

  if (ref_signal_ == nullptr) {
    throw std::runtime_error(std::string("Failed constructing ref_signal ") +
                             sig_conf["ref_signal"].GetString() +
                             " in SigBookImpulse");
  }

  ref_signal_->addListener(this);
  ref_signal_val_ = 0;

  value_ = 0;
}

bool SigBookImpulse::isSame(const rapidjson::Value& other_conf,
                            SignalFactory& sf,
                            const rapidjson::Value& all_signals_array) const {
  if (std::string(other_conf["type"].GetString()) != "SigBookImpulse") {
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

  if (px_decay_ != other_conf["px_decay"].GetDouble()) {
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

  if (other_conf.HasMember("ticker_weight") &&
      ticker_weight_ != other_conf["ticker_weight"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("update_weight") &&
      update_weight_ != other_conf["update_weight"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("trade_weight") &&
      trade_weight_ != other_conf["trade_weight"].GetDouble()) {
    return false;
  }

  if (time_decay_ != other_conf["time_decay"].GetDouble()) {
    return false;
  }

  if (tick_decay_ != other_conf["tick_decay"].GetDouble()) {
    return false;
  }

  if (min_tick_decay_ != other_conf["min_tick_decay"].GetDouble()) {
    return false;
  }

  if (pred_decay_ != other_conf["pred_decay"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("pred_decay_agree") &&
      pred_decay_agree_ != other_conf["pred_decay_agree"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("pred_decay_denom") &&
      pred_decay_denom_ != other_conf["pred_decay_denom"].GetDouble()) {
    return false;
  }

  if (other_conf.HasMember("time_behind_const") &&
      time_behind_const_ != other_conf["time_behind_const"].GetDouble() * 1e3) {
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

  if (other_conf.HasMember("pred_boost_timeconst_s") &&
      pred_boost_timeconst_ !=
          other_conf["pred_boost_timeconst_s"].GetDouble() * 1e3) {
    return false;
  }

  if (other_conf.HasMember("pred_boost_offset") &&
      pred_boost_offset_ != other_conf["pred_boost_offset"].GetDouble()) {
    return false;
  }

  return true;
}

std::vector<pktrade::BookId> SigBookImpulse::getBookIds() const {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

void SigBookImpulse::subscribeData(pktrade::md::MDBeacon* beacon) {
  for (pktrade::Market bk : books_) {
    BookId book_id{bk, symbol_};
    std::vector<pktrade::md::BeaconCBType> cb_types = {
        pktrade::md::BeaconCBType::LvlAdd, pktrade::md::BeaconCBType::LvlMod,
        pktrade::md::BeaconCBType::LvlDel, pktrade::md::BeaconCBType::Final};

    beacon->addListener(book_id, this, cb_types);
  }
}

double SigBookImpulse::getDecay() {
  double decay = 1;
  decay *= tick_decay_;
  decay *= min_tick_decay_;

  int64_t cur_t = time_utils::nowToMs();
  // Time-based decay.
  if (last_decay_t_ != 0) {
    decay *= std::exp(-((double)(cur_t - last_decay_t_) / 1000.) / time_decay_);
  }

  // std::cout << "Decay " << tick_decay_ << " t_diff " << (double)(cur_t -
  // last_decay_t_) / 1e3 << " decay " << decay << std::endl;

  last_decay_t_ = cur_t;

  return decay;
}

void SigBookImpulse::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  // If there's no ref_signal, then don't do anything.
  if (ref_signal_val_ == 0) {
    ref_signal_val_ = ref_signal_->getValue();
    return;
  }

  if (first_tick_) {
    onFirstTick();
  }


  if ((lvl_add.side == Side::Buy && lvl_add.px.toDouble() > bb_) ||
      (lvl_add.side == Side::Sell && lvl_add.px.toDouble() < ba_)) {
    // std::cout << " MIS del " << lvl_add.px.toDouble() << std::endl;

    maybeUpdateInside(bk, lvl_add.side);
  }

  // Don't include impulses that could be dangerous.
  if (bb_ == 0 || ba_ == 0 || ba_ == 1e10) {
    return;
  }

  // double decay = getDecay();
  double px_dub = lvl_add.px.toDouble();
  double size_dub = lvl_add.qty.toDouble();

  double impulse = 1;
  // price weight.
  // Traditionally you'd do this from the inside. But here I'm using the mid
  // sig, I guess. Could yield a difference in behavior.
  double price_diff;
  if (lvl_add.side == Side::Buy) {
    price_diff = (bb_ - lvl_add.px.toDouble()) / bb_;
  } else {
    price_diff = (lvl_add.px.toDouble() - ba_) / ba_;
  }

  double px_weight = std::exp(-price_diff / px_decay_);

  px_weight = std::min(px_weight, 1.0);

  impulse *= px_weight;

  // size weight.
  double sz_weight = 1;
  if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL) {
    sz_weight = 1 - std::exp(-(std::abs(px_dub * size_dub)) / sz_decay_);
  } else if (sz_decay_type_ == SizeDecayType::LOG_NOTIONAL) {
    sz_weight = std::log(1 + std::abs(px_dub * size_dub / sz_decay_));
  } else if (sz_decay_type_ == SizeDecayType::SIZE) {
    sz_weight = std::abs(size_dub);
  } else if (sz_decay_type_ == SizeDecayType::EXP_SIZE) {
    sz_weight = 1 - std::exp(-(std::abs(size_dub)) / sz_decay_);
  }

  impulse *= sz_weight;
  // impulse = 1;

  // Time behind decay.
  if (time_behind_const_ != -1 && lvl_add.transact_t != 0) {
    impulse *= std::exp(
        -std::max(double(time_utils::nowToMs() - lvl_add.transact_t), 0.0) /
        time_behind_const_);
  }

  // delta boost.
  if (use_pred_boost_) {
    impulse *=
        (1 - std::exp(-double(time_utils::nowToMs() - last_pred_change_t_) /
                      pred_boost_timeconst_)) +
        pred_boost_offset_;
  }

  // Side...
  if (lvl_add.side == Side::Sell) {
    impulse *= -1;
  } else if (lvl_add.side == Side::Buy) {
    // do nothing
  }

  if (lvl_add.last_msg_type == LastMsgType::BookTicker) {
    impulse *= ticker_weight_;
  } else if (lvl_add.last_msg_type == LastMsgType::BookUpdate) {
    impulse *= update_weight_;
  } else if (lvl_add.last_msg_type == LastMsgType::Trade) {
    impulse *= trade_weight_;
  }

  // Apply bungee.
  if (bungee_denom_ != -1 &&
      ((impulse > 0 && value_ > 0) || (impulse < 0 && value_ < 0))) {
    double bungee_mult = std::exp(-std::abs(value_ / bungee_denom_));
    double bungee_scale =
        bungee_mult_frac_ * bungee_mult + (1 - bungee_mult_frac_);
    impulse *= bungee_scale;
  }

  if (debug_verbose_) {
    printf(
        "%s lvlAdd side %s px %f qty %f bb ba (%f %f) refval %f  px_weight %f "
        "size_weight %f "
        "impulse %f value %f \n",
        time_utils::nowToStr().c_str(),

        lvl_add.side == Side::Buy ? "Buy" : "Sell", lvl_add.px.toDouble(),
        lvl_add.qty.toDouble(), bb_, ba_,

        ref_signal_val_, px_weight, sz_weight, impulse, value_);
  }
  // setValue(value_ * decay + impulse);
  setValue(value_ + impulse);
}

// If we're applying an impulse to the mod, the sz diff should be the diff of
// the old and news, not their difference.
void SigBookImpulse::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (ref_signal_val_ == 0) {
    ref_signal_val_ = ref_signal_->getValue();
    return;
  }

  if (first_tick_) {
    onFirstTick();
  }


  // Can happen with flagging.
  if ((lvl_mod.prev_qty == Quantity{0}) && lvl_mod.qty == Quantity{0}) {
    return;
  }

  // In the L2 world, level modifies never change price. So
  // we never check for maybeShiftInsides.
  // Correction. with flagging we do.
  // Also possible that a previously flagged-away level came back.
  if ((lvl_mod.prev_qty == Quantity{0}) &&
      ((lvl_mod.side == Side::Buy && lvl_mod.px.toDouble() >= bb_) ||
       (lvl_mod.side == Side::Sell && lvl_mod.px.toDouble() <= ba_))) {
    // std::cout << " MIS mod " << lvl_mod.px.toDouble() << std::endl;

    maybeUpdateInside(bk, lvl_mod.side);
  }


  // double decay = getDecay();

  double impulse = 1;
  double px_dub = lvl_mod.px.toDouble();
  // double size_dub = (lvl_mod.qty - lvl_mod.prev_qty).toDouble();

  // TODO: this should probably be broken up before- and after-, depending on
  // whether we are adding or removing shares. Because the inside could have
  // actually changed.
  double price_diff;
  if (lvl_mod.side == Side::Buy) {
    price_diff = (bb_ - lvl_mod.px.toDouble()) / bb_;
  } else {
    price_diff = (lvl_mod.px.toDouble() - ba_) / ba_;
  }

  double px_weight = std::exp(-price_diff / px_decay_);

  if ((lvl_mod.qty == Quantity{0}) &&
      ((lvl_mod.side == Side::Buy && lvl_mod.px.toDouble() >= bb_) ||
       (lvl_mod.side == Side::Sell && lvl_mod.px.toDouble() <= ba_))) {
    // std::cout << " MIS mod " << lvl_mod.px.toDouble() << std::endl;

    maybeUpdateInside(bk, lvl_mod.side);
  }

  // Don't include impulses that could be dangerous.
  if (bb_ == 0 || ba_ == 0 || ba_ == 1e10) {
    return;
  }


  px_weight = std::min(px_weight, 1.0);

  impulse *= px_weight;
  double size_dub = (lvl_mod.qty - lvl_mod.prev_qty).toDouble();

  // size weight.
  // size weight.
  double sz_weight = 1;
  if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL) {
    // sz_weight of
    sz_weight = 1 - std::exp(-(std::abs(px_dub * size_dub)) / sz_decay_);
  } else if (sz_decay_type_ == SizeDecayType::LOG_NOTIONAL) {
    sz_weight = std::log(1 + std::abs(px_dub * size_dub / sz_decay_));
  } else if (sz_decay_type_ == SizeDecayType::SIZE) {
    sz_weight = std::abs(size_dub);

  } else if (sz_decay_type_ == SizeDecayType::EXP_SIZE) {
    sz_weight = 1 - std::exp(-(std::abs(size_dub)) / sz_decay_);
  }
  impulse *= sz_weight;

  if (time_behind_const_ != -1 && lvl_mod.transact_t != 0) {
    double dt =
        std::max(double(time_utils::nowToMs() - lvl_mod.transact_t), 0.0);
    impulse *= std::exp(-dt / time_behind_const_);
  }

  if (use_pred_boost_) {
    impulse *=
        (1 - std::exp(-double(time_utils::nowToMs() - last_pred_change_t_) /
                      pred_boost_timeconst_)) +
        pred_boost_offset_;
  }

  // Directionality
  if (lvl_mod.side == Side::Sell) {
    impulse *= -1;
  } else if (lvl_mod.side == Side::Buy) {
    // do nothing
  }
  if (lvl_mod.qty < lvl_mod.prev_qty) {
    impulse *= -1;
  }

  if (lvl_mod.last_msg_type == LastMsgType::BookTicker) {
    impulse *= ticker_weight_;
  } else if (lvl_mod.last_msg_type == LastMsgType::BookUpdate) {
    impulse *= update_weight_;
  } else if (lvl_mod.last_msg_type == LastMsgType::Trade) {
    impulse *= trade_weight_;
  }

  // Apply bungee.
  if (bungee_denom_ != -1 &&
      ((impulse > 0 && value_ > 0) || (impulse < 0 && value_ < 0))) {
    double bungie_mult = std::exp(-std::abs(value_ / bungee_denom_));
    double bungie_scale =
        bungee_mult_frac_ * bungie_mult + (1 - bungee_mult_frac_);
    impulse *= bungie_scale;
  }

  if (debug_verbose_) {
    printf(
        "%s lvlMod side %s px %f qty (old %f new %f) bb ba (%f %f) refval %f "
        "px_weight %f "
        "size_weight %f "
        "maybe_pred_boost %f impulse %f value %f  \n",
        time_utils::nowToStr().c_str(),

        lvl_mod.side == Side::Buy ? "Buy" : "Sell", lvl_mod.px.toDouble(),
        lvl_mod.prev_qty.toDouble(), lvl_mod.qty.toDouble(), bb_, ba_,
        ref_signal_val_, px_weight, sz_weight,
        double(time_utils::nowToMs() - lvl_mod.transact_t),
        impulse, value_);
  }
  // Combine.
  // setValue(value_ * decay + impulse);
  setValue(value_ + impulse);
}

void SigBookImpulse::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (ref_signal_val_ == 0) {
    ref_signal_val_ = ref_signal_->getValue();
    return;
  }

  if (first_tick_) {
    onFirstTick();
  }

  // double decay = getDecay();

  double impulse = 1;
  double px_dub = lvl_del.px.toDouble();
  double size_dub = lvl_del.prev_qty.toDouble();

  // Flagged.
  if (size_dub == 0) {
    return;
  }

  // price weight.
  double price_diff;
  if (lvl_del.side == Side::Buy) {
    price_diff = (bb_ - lvl_del.px.toDouble()) / bb_;
  } else {
    price_diff = (lvl_del.px.toDouble() - ba_) / ba_;
  }

  double px_weight = std::exp(-price_diff / px_decay_);
  px_weight = std::min(px_weight, 1.0);

  impulse *= px_weight;

  if ((lvl_del.side == Side::Buy && lvl_del.px.toDouble() >= bb_) ||
      (lvl_del.side == Side::Sell && lvl_del.px.toDouble() <= ba_)) {
    // std::cout << " MIS del " << lvl_del.px.toDouble() << std::endl;
    maybeUpdateInside(bk, lvl_del.side);
  }

  // Don't include impulses that could be dangerous.
  if (bb_ == 0 || ba_ == 0 || ba_ == 1e10) {
    return;
  }


  // size weight.
  // size weight.
  double sz_weight = 1;
  if (sz_decay_type_ == SizeDecayType::EXP_NOTIONAL) {
    sz_weight = 1 - std::exp(-(std::abs(px_dub * size_dub)) / sz_decay_);
  } else if (sz_decay_type_ == SizeDecayType::LOG_NOTIONAL) {
    sz_weight = std::log(1 + std::abs(px_dub * size_dub / sz_decay_));
  } else if (sz_decay_type_ == SizeDecayType::SIZE) {
    sz_weight = std::abs(size_dub);
  } else if (sz_decay_type_ == SizeDecayType::EXP_SIZE) {
    sz_weight = 1 - std::exp(-(std::abs(size_dub)) / sz_decay_);
  }
  impulse *= sz_weight;

  // impulse = -1;

  // Side.
  if (lvl_del.side == Side::Buy) {
    impulse *= -1;
  } else if (lvl_del.side == Side::Sell) {
    // do nothing
  }

  if (lvl_del.last_msg_type == LastMsgType::BookTicker) {
    impulse *= ticker_weight_;
  } else if (lvl_del.last_msg_type == LastMsgType::BookUpdate) {
    impulse *= update_weight_;
  } else if (lvl_del.last_msg_type == LastMsgType::Trade) {
    impulse *= trade_weight_;
  }

  if (time_behind_const_ != -1 && lvl_del.transact_t != 0) {
    impulse *= std::exp(
        -std::max(double(time_utils::nowToMs() - lvl_del.transact_t), 0.0) /
        time_behind_const_);
  }

  // Apply bungee.
  if (bungee_denom_ != -1 &&
      ((impulse > 0 && value_ > 0) || (impulse < 0 && value_ < 0))) {
    double bungie_mult = std::exp(-std::abs(value_ / bungee_denom_));
    double bungie_scale =
        bungee_mult_frac_ * bungie_mult + (1 - bungee_mult_frac_);
    impulse *= bungie_scale;
  }

  if (use_pred_boost_) {
    impulse *=
        (1 - std::exp(-double(time_utils::nowToMs() - last_pred_change_t_) /
                      pred_boost_timeconst_)) +
        pred_boost_offset_;
  }

  if (debug_verbose_) {
    printf(
        "%s lvlDel side %s px %f qty %f bb ba (%f %f) refval %f px_weight %f "
        "size_weight %f "
        "impulse %f value %f  \n",
        time_utils::nowToStr().c_str(),

        lvl_del.side == Side::Buy ? "Buy" : "Sell", lvl_del.px.toDouble(),
        lvl_del.prev_qty.toDouble(), bb_, ba_, ref_signal_val_, px_weight,
        sz_weight, impulse, value_);
  }
  // Combine.
  // setValue(value_ * decay + impulse);
  setValue(value_ + impulse);
}

void SigBookImpulse::maybeUpdateInside(const LevelBook& bk, Side side) {
  double bp;
  if (side == Side::Buy) {
    bp = 0;
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
    bp = 1e10;
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
}

void SigBookImpulse::onFinal(const LevelBook& bk) {
  double decay = getDecay();
  setValue(value_ * decay);
}

void SigBookImpulse::onSignalValue(int sig_id, double val) {
  last_pred_change_t_ = time_utils::nowToMs();

  double decay = 1;

  if (pred_decay_denom_ == -1) {
    if ((val - ref_signal_val_) * value_ > 0) {
      // Same direction.
      decay *= pred_decay_agree_;
    } else {
      decay *= pred_decay_;
    }
  } else {
    // Scaled decay.
    double delta_perc = std::abs(val - ref_signal_val_) / ref_signal_val_;

    if ((val - ref_signal_val_) * value_ > 0) {
      // Same direction.
      decay *= std::pow(pred_decay_agree_, delta_perc / pred_decay_denom_);
    } else {
      decay *= std::pow(pred_decay_, delta_perc / pred_decay_denom_);
    }
  }

  // Apply the decay.
  setValue(value_ * decay);
  ref_signal_val_ = val;
}

void SigBookImpulse::onSignalValidity(int sig_id, bool valid) {
  ref_signal_val_ = 0;
}

void SigBookImpulse::reset() {
  // I guess, just set value to 0.

  setValue(0);
}

void SigBookImpulse::onFirstTick() {
  first_tick_ = false;
  // Peg the price-based values.
  const LevelBook* bk =
      pktrade::GlobalVar::book_manager_->find(BookId{books_[0], symbol_});

  const Book<BookLevel>::BookSide<BuySide>& buy_side = bk->side<BuySide>();
  if (!buy_side.empty()) {
    for (auto& lvl : buy_side) {
      if (effectiveQty(lvl) > Quantity{0}) {
        bb_ = lvl.px.toDouble();
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
        //    std::cout << " Got " << lvl.px.toDouble() << std::endl;
        break;
      } else {
        //     std::cout << " Skipped " << lvl.px.toDouble() << std::endl;
      }
    }

  } else {
    ba_ = 1e10;
  }

  if (bb_ == 0 || ba_ == 0 || ba_ == 1e10) {
    first_tick_ = true;
  }
}

}  // namespace pktrade::signals
