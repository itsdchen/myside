#pragma once

#include "pktrade/mktdata/md_beacon.h"
#include "signal.h"

namespace pktrade::signals {

class SigTrdPx : public Signal,
                 public pktrade::md::BeaconListener,
                 public SignalListener {
 public:
  SigTrdPx(int signal_id,
           const rapidjson::Value& sig_conf,
           SignalFactory* sf,
           const rapidjson::Value& all_signals_array);

  bool isSame(const rapidjson::Value& other_conf,
              SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;

  void subscribeData(MDBeacon* beacon) override;

  void applyDecay();

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) override{};

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  void calc();
  void reset() override;

  // SizeDecayType.

 protected:
  enum class SizeDecayType { EXP_NOTIONAL, LOG_NOTIONAL, EXP_SIZE, SIZE };

  // AVG_PX: just do a conbuf'd avg px
  // AVG_REL_PX: Calc the instantaneous return relative to the mid
  // and v/t wap vs that.
  enum class CalcStyle { AVG_PX, AVG_REL_PRICE };

  SymbolId symbol_;
  std::vector<pktrade::Market> books_;

  double running_num_;
  double running_denom_;

  // Don't trust the divisions below this amount.
  double min_denom_ = 1e-6;

  double sz_decay_;
  SizeDecayType sz_decay_type_;

  CalcStyle calc_style_;

  double time_decay_;
  double tick_decay_;

  int64_t last_decay_t_;
  bool return_price_;

  // For returning a return vs a price.
  Signal* ref_signal_;
  double ref_signal_val_;
};

}  // namespace pktrade::signals