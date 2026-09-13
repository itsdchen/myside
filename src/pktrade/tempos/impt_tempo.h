#pragma once

#include "base_tempo.h"
#include "pktrade/enums/basic_enums.h"
#include "pktrade/mktdata/md_beacon.h"

/*

Sample on:

1 - wmid diff (spread-adjust wmid diff from the last trigger point)
2 - Trades. Sample on trade-finals.
3 - Mid diff (widen)
4 - Mid diff (narrow)

Priority should happen after all signals.

Set pri to 120.

*/

namespace pktrade::tempos {

class ImptTempo : public BaseTempo, public pktrade::md::BeaconListener {
 public:
  ImptTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  std::vector<pktrade::BookId> getBookIds() override;

  // BeaconListener callbacks.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
    if (do_bunch_check_) {
      if (lvl_add.transact_t != 0 && (lvl_add.transact_t <= last_trade_t_)) {
        // ignore_this_round_ = true;
        same_t_count_++;
        if (same_t_count_ > 5) {
        }
      }
      if (lvl_add.transact_t != 0 && (lvl_add.transact_t > last_trade_t_)) {
        last_trade_t_ = lvl_add.transact_t;
      }
    }
  }
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
    if (do_bunch_check_) {
      if (lvl_mod.transact_t != 0 && (lvl_mod.transact_t <= last_trade_t_)) {
        // ignore_this_round_ = true;
        same_t_count_++;
        if (same_t_count_ > 5 && lvl_mod.qty == 0) {
          ignore_this_round_ = true;
        }
      }
      if (lvl_mod.transact_t != 0 && (lvl_mod.transact_t > last_trade_t_)) {
        last_trade_t_ = lvl_mod.transact_t;
      }
    }
  }

  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
    if (do_bunch_check_) {
      if (lvl_del.transact_t != 0 && (lvl_del.transact_t <= last_trade_t_)) {
        // ignore_this_round_ = true;
        same_t_count_++;
        if (same_t_count_ > 5) {
          ignore_this_round_ = true;
        }
      }
      if (lvl_del.transact_t != 0 && (lvl_del.transact_t > last_trade_t_)) {
        last_trade_t_ = lvl_del.transact_t;
      }
    }
  }

  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk);

  // eventually, more callbacks as we implement more things.

  void onTimedFire();

  void doFire();

 protected:
  SymbolId symbol_;
  std::vector<pktrade::Market> books_;

  bool trigger_inside_widen_ = false;
  bool trigger_inside_narrow_ = false;
  double wmid_diff_thresh_ = 0.3;

  double bb_;
  double ba_;

  // Only fires on the final callback. So we keep track of what happened this
  // round.
  bool trade_happened_ = false;
  double last_fire_scaled_wmid_ = 0;

  bool ignore_this_round_ = false;

  // Perhaps condition.
  bool do_bunch_check_;
  int64_t last_trade_t_;
  int same_t_count_ = 0;

  std::chrono::milliseconds ms_between_fire_;
  bool time_aspect_started_;
  bool enable_time_aspect_;

  // Batches impttempo firing.
  bool is_firing_ = false;
};

}; // namespace pktrade::tempos
