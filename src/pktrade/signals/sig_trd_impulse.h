#pragma once

#include "pktrade/enums/basic_enums.h"
#include "pktrade/mktdata/md_beacon.h"
#include "signal.h"

using namespace pktrade;

namespace pktrade::signals {

class SigTrdImpulse : public Signal,
                      public pktrade::md::BeaconListener,
                      public SignalListener {
 public:
  SigTrdImpulse(int signal_id,
                const rapidjson::Value& sig_conf,
                SignalFactory* sf,
                const rapidjson::Value& all_signals_array);

  bool isSame(const rapidjson::Value& other_conf,
              SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;

  void subscribeData(MDBeacon* beacon) override;

  void applyDecay();

  // For decays.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);

  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk){};

  void flushBatch();

  void maybeUpdateInside(const LevelBook& bk, Side side);
  void onFirstTick();

  void maybeUpdateDraped();

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  void reset() override;

  // Does the actual calculation.
  void calcValue();

 protected:
  enum class SizeDecayType { EXP_NOTIONAL, LOG_NOTIONAL, EXP_SIZE };

  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  double px_decay_;

  double sz_decay_;
  SizeDecayType sz_decay_type_;

  // Maybe don't.
  bool use_killed_lvl_boost_;
  double killed_lvl_boost_;

  double time_decay_;
  double tick_decay_;

  int64_t last_decay_t_;

  // *****************************************************************
  // Calculating draped liquidity.
  // If this happens, we check if we need to update on all ticks.
  // Probably more complete

  bool normalize_size_draped_inside_;
  double draped_tdc_;
  double draped_inside_shs_;
  int64_t last_draped_calc_t_;

  // *****************************************************************

  // For decaying based on the trading symbol
  bool first_tick_ = true;
  double bb_;
  double ba_;
  double mid_;

  // This is really the signal for the
  Signal* tgt_signal_;
  double tgt_signal_val_;
  double tgt_sig_decay_;
  double tgt_decay_disagree_;

  // For otf-batching.
  double perm_value_;
  double tmp_value_;
  double tmp_value_shs_;
  double tmp_value_px_;
  int64_t last_trd_transact_t_;

  // For batching.
  bool batch_;
  double running_shs_;
  double running_notional_;
  Side running_side_;

  // Bungee scaling. Adopted from the volev group.
  double bungee_denom_;
  double bungee_mult_frac_;

  // We scale this by difference from the tgt value. But let's also create a min
  // diff and then use the direction of the exec to show which side we apply the
  // impulse.
  double min_tgt_diff_bps_;


  // TODO: create a tgt scaling Type here.
};

}  // namespace pktrade::signals