#pragma once

#include "pktrade/mktdata/md_beacon.h"
#include "signal.h"

namespace pktrade::signals {

class SigBookImpulse : public Signal,
                       public pktrade::md::BeaconListener,
                       public pktrade::signals::SignalListener {
 public:
  SigBookImpulse(int signal_id,
                 const rapidjson::Value& sig_conf,
                 SignalFactory* sf,
                 const rapidjson::Value& all_signals_array);
  ~SigBookImpulse() = default;

  bool isSame(const rapidjson::Value& other_conf,
              SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;

  double getDecay();

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) override;
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) override;
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) override;
  void onTrade(const LevelBook& bk, const Trade& trd) override{};
  void onFinal(const LevelBook& bk);

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  void reset() override;

  void maybeUpdateInside(const LevelBook& bk, Side side);
  // Initialize some price-scaled things.
  void onFirstTick();

 protected:
  enum class SizeDecayType { EXP_NOTIONAL, LOG_NOTIONAL, SIZE, EXP_SIZE };

  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  double px_decay_;
  double sz_decay_;
  SizeDecayType sz_decay_type_;

  // DIfferentiating types of events
  double ticker_weight_;
  double update_weight_;
  double trade_weight_;

  double time_decay_;
  double tick_decay_;
  double min_tick_decay_;
  double pred_decay_;

  // Adding options for differentiated pred decay.
  double pred_decay_agree_;
  double pred_decay_denom_;

  int64_t last_decay_t_;

  // For weighing things that are slower than others.
  double time_behind_const_;

  // Introduce a term that stops the signal from straying too far in any
  // direction. The denom takes the current signal value (if impulse dir is the
  // same way) and applies a decaying exponential to it. The second value
  // accepts a number frac in (0, 1) which takes the result res and creates a
  // decay dec = frac*resul + (1 - frac)
  double bungee_denom_;
  double bungee_mult_frac_;

  // That way, suff that happens pretty recently gets a boost.
  // But stuff later on isn't that badly hurt either.
  // exp (-(t)/time_const) + 0.3
  // If right after the inside, give a boost.
  bool use_pred_boost_;
  double pred_boost_timeconst_;
  int64_t last_pred_change_t_;
  double pred_boost_offset_;

  Signal* ref_signal_;
  double ref_signal_val_;

  bool first_tick_ = true;
  double bb_;
  double ba_;
};

}  // namespace pktrade::signals