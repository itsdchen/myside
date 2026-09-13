#include "impt_tempo.h"

#include "pktrade/context/global_vars.h"
namespace pktrade::tempos {

ImptTempo::ImptTempo(const rapidjson::Value& conf, int id, TempoFactory* tf)
    : BaseTempo(id, conf["name"].GetString()) {
  symbol_ = SymbolId{conf["symbol"].GetString()};

  // Grab markets of course.
  for (const rapidjson::Value& mkt : conf["markets"].GetArray()) {
    Market mkt_enum = *magic_enum::enum_cast<Market>(mkt.GetString());
    books_.push_back(mkt_enum);
  }

  // Slow tempo. Do it after the other signals finish updating.
  pri_ = 120;

  bb_ = 0;
  ba_ = 1e10;

  // Settings for the tempo.

  trigger_inside_widen_ = conf["trigger_inside_widen"].GetBool();
  trigger_inside_narrow_ = conf["trigger_inside_narrow"].GetBool();

  wmid_diff_thresh_ = conf["wmid_diff_thresh"].GetDouble();

  do_bunch_check_ = false;
  if (conf.HasMember("do_bunch_check")) {
    do_bunch_check_ = conf["do_bunch_check"].GetBool();
  }
  enable_time_aspect_ = false;
  if (conf.HasMember("enable_time_aspect")) {
    enable_time_aspect_ = conf["enable_time_aspect"].GetBool();
  }
  time_aspect_started_ = false;
  ms_between_fire_ = std::chrono::milliseconds(1);
}

bool ImptTempo::isSame(const rapidjson::Value& other_conf, TempoFactory* tf) {
  if (std::string(other_conf["type"].GetString()) != "ImptTempo") {
    return false;
  }

  // Gotta check other stuff ya
  if (symbol_.get() != other_conf["symbol"].GetString())
    return false;

  // Equivalence == if the sizes are the same and all the members of one
  // are found in the other.
  if (other_conf["markets"].Size() != books_.size())
    return false;

  for (const rapidjson::Value& mkt_str_val : other_conf["markets"].GetArray()) {
    // Check if it exists.
    auto mkt = *magic_enum::enum_cast<Market>(mkt_str_val.GetString());

    if (std::find(books_.begin(), books_.end(), mkt) == books_.end())
      return false;
  }

  if (trigger_inside_widen_ != other_conf["trigger_inside_widen"].GetBool())
    return false;

  if (trigger_inside_narrow_ != other_conf["trigger_inside_narrow"].GetBool())
    return false;

  if (wmid_diff_thresh_ != other_conf["wmid_diff_thresh"].GetDouble())
    return false;

  if (other_conf.HasMember("do_bunch_check") &&
      do_bunch_check_ != other_conf["do_bunch_check"].GetBool()) {
    return false;
  }

  return true;
}

void ImptTempo::subscribeData(pktrade::md::MDBeacon* beacon) {
  std::vector<pktrade::md::BeaconCBType> cb_types = {
      pktrade::md::BeaconCBType::LvlAdd,

      pktrade::md::BeaconCBType::LvlMod, pktrade::md::BeaconCBType::LvlDel,

      pktrade::md::BeaconCBType::Trd,    pktrade::md::BeaconCBType::Final,
  };

  for (Market mkt : books_) {
    BookId book_id{mkt, symbol_};
    beacon->addListener(book_id, this, cb_types);
  }
}

std::vector<pktrade::BookId> ImptTempo::getBookIds() {
  std::vector<pktrade::BookId> ret;

  for (pktrade::Market book : books_) {
    ret.push_back({book, symbol_});
  }
  return ret;
}

void ImptTempo::onTrade(const LevelBook& bk, const Trade& trd) {
  trade_happened_ = true;
  if (do_bunch_check_) {
    if (trd.transact_t != 0 && (trd.transact_t <= last_trade_t_)) {
      // ignore_this_round_ = true;
      same_t_count_++;
      if (same_t_count_ > 5) {
      }
    }
    if (trd.transact_t != 0 && (trd.transact_t > last_trade_t_)) {
      last_trade_t_ = trd.transact_t;
    }
  }
}

void ImptTempo::onFinal(const LevelBook& bk) {
  bool inside_widen = false;
  bool inside_narrow = false;

  double maybe_bb = bk.getSideTop<BuySide>().px.toDouble();
  double maybe_ba = bk.getSideTop<SellSide>().px.toDouble();

  double bq = bk.getSideTop<BuySide>().qty.toDouble();
  double aq = bk.getSideTop<SellSide>().qty.toDouble();

  // I'm doing the
  double cur_wmid = (bq * maybe_ba + aq * maybe_bb) / (bq + aq);
  double cur_spread = maybe_ba - maybe_bb;
  double cur_scaled_wmid = last_fire_scaled_wmid_;
  if (cur_spread > 0) {
    // This is the thing we're comparing against the last thing.
    cur_scaled_wmid = (cur_wmid - (maybe_ba + maybe_ba) / 2) / cur_spread;
  }

  // Check if widened
  if (maybe_bb < bb_) {
    inside_widen = true;
    bb_ = maybe_bb;
  }
  if (maybe_ba > ba_) {
    inside_widen = true;
    ba_ = maybe_ba;
  }

  // Check if narrowed
  if (maybe_bb > bb_) {
    inside_narrow = true;
    bb_ = maybe_bb;
  }
  if (maybe_ba < ba_) {
    inside_narrow = true;
    ba_ = maybe_ba;
  }

  // It's possible both happened. Oh well, in that case.

  bool should_fire = false;
  if (inside_widen) {
    if (trigger_inside_widen_) {
      should_fire = true;
    }

    // Update the scaled wmid anyway.
    last_fire_scaled_wmid_ = cur_scaled_wmid;
  } else if (inside_narrow) {
    if (trigger_inside_narrow_) {
      should_fire = true;
    }
    last_fire_scaled_wmid_ = cur_scaled_wmid;
  } else if (trade_happened_) {
    // Check if the wmid diff is big enough.
    should_fire = true;
  } else if (std::abs(cur_scaled_wmid - last_fire_scaled_wmid_) > wmid_diff_thresh_) {
    should_fire = true;
  }

  if (ignore_this_round_) {
    should_fire = false;
  }

  if (should_fire) {
    // TODO: change this from sim vs live.
    // pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::milliseconds(1),
    //                                           [&] { this->doFire(); });

    // Undo this thing.
    fire();
    // Reset the wmid.
    last_fire_scaled_wmid_ = cur_scaled_wmid;
  }
  // TODO: eventually put this at a threshold of X trade volume or trade
  // messages before we go.
  trade_happened_ = false;

  ignore_this_round_ = false;
  if (!time_aspect_started_) {
    onTimedFire();
    time_aspect_started_ = true;
  }
}

void ImptTempo::doFire() {
  fire();
  is_firing_ = false;
}

void ImptTempo::onTimedFire() {
  if (!enable_time_aspect_) {
    return;
  }

  // return;
  // std::cout <<" firing from timer" << std::endl;
  fire();
  if (Clock::now() >= pktrade::GlobalVar::end_t_) {
    // Stop it.
    return;
  }

  pktrade::GlobalVar::event_loop_->onTimeout(ms_between_fire_, [&] { this->onTimedFire(); });
}

} // namespace pktrade::tempos
