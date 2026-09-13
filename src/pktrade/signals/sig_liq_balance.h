#pragma once

#include "signal.h"

/*

Update: changing this to do the per-level based thing.

*/

namespace pktrade::signals {

class SigLiqBalance : public Signal, public pktrade::md::BeaconListener, public SignalListener {
 public:
  SigLiqBalance(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf,
                const rapidjson::Value& all_signals_array);

  ~SigLiqBalance() = default;

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;

  bool shouldIgnore(Price px, Side side);

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) override;
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) override;
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) override;
  void onTrade(const LevelBook& bk, const Trade& trd) override {};

  // Undecided rn if we want to do this just yet.
  // Could be interesting. Let's reserve ability to do it.
  // TODO: make this happen.
  void onFinal(const LevelBook& bk);

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  double getSizeDecay(double qty, double px);

  void calc();
  void fullRecalc();

  double getBBMeasure() override;
  double getBAMeasure() override;
  void setNotionalToLiquidate(double notional_to_liquidate);

  // Should really be its own thing.
  double setMinTick();

  void reset() override;
  std::string getPrimarySymbol() const override;

 protected:
  void invalidate();

  enum class LiqType { FIXED_LEVELS };
  enum class CalcStyle { AVG_LIQ_PX, DEEPWP1, DEEPWMP2 };

  enum class SizeDecayType {
    EXP_NOTIONAL,
    LOG_NOTIONAL,
    SIZE,
    EXP_SIZE,
    EXP_NOTIONAL_PERORD,
    EXP_SIZE_PERORD,
    EXP_NOTIONAL_AND_SIZE
  };

  // We can keep track of how far we liquidated
  // into either side. Yep.
  SymbolId symbol_;
  std::vector<pktrade::Market> books_;

  LiqType liq_type_;
  CalcStyle calc_style_;
  double per_level_decay_;

  // For a snapshot-given book, it's probably efficient enough to just calculate it
  // immediately.
  bool always_full_recalc_;

  // If given, this is how many dollars notional to limit the liquidation to.
  double notional_to_liquidate_;

  int levels_deep_;
  bool return_price_;

  double sz_decay_;
  // For a second sense of size decay. Used only for some sizedecaytypes.
  double sz_decay_2_;
  SizeDecayType sz_decay_type_;

  double min_sz_wt_;

  double liq_px_;

  double buy_stren_;
  double buy_mass_;

  double sell_stren_;
  double sell_mass_;

  bool first_tick_;

  bool should_recalc_;
  bool calc_on_final_;
  bool use_flagged_;

  ///////////////////////////////////////////////////
  // For tracking which levels we care about. Inclusive.
  Price best_bid_;
  Price best_ask_;

  Price worst_bid_;
  Price worst_ask_;

  double min_tick_;

  ///////////////////////////////////////////////////

  Signal* ref_signal_;
  double ref_signal_val_;
  // Cached validity of ref_signal_, kept in sync via onSignalValue/onSignalValidity
  // (symmetric with ref_signal_val_). In return_price_ mode the output is book-only
  // and doesn't use ref, so this is forced true there.
  bool ref_signal_valid_;
};

}; // namespace pktrade::signals
