#pragma once

#include "pktrade/mktdata/md_beacon.h"
#include "signal.h"

namespace pktrade::signals {

class SigDeepBook : public Signal, public pktrade::md::BeaconListener {
 public:
  SigDeepBook(int signal_id,
              const rapidjson::Value& sig_conf,
              SignalFactory* sf,
              const rapidjson::Value& all_signals_array);
  ~SigDeepBook() = default;

  bool isSame(const rapidjson::Value& other_conf,
              SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) override;
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) override;
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) override;
  void onTrade(const LevelBook& bk, const Trade& trd) override{};

  void onFinal(const LevelBook& bk) override;

  double calcImpulse(Quantity qty, Price px, Side side);

  void maybeShiftInside(const LevelBook& bk, Side side);

  void calc();
  void fullRecalc();

  // Initialize some price-scaled things.
  void onFirstTick();

  void reset() override;

 protected:
  //
  enum class SizeDecayType {
    EXP_NOTIONAL,
    LOG_NOTIONAL,
    SIZE,
    EXP_SIZE,
    EXP_NOTIONAL_PERORD,
    EXP_SIZE_PERORD,
    EXP_NOTIONAL_AND_SIZE
  };
  // One day, clearing price.
  enum class CalcStyle { DIFF_RATIO, LOG_RATIO };

  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  // In real life, we want to use the decay_px version of the signal.
  // decay_pct is for anchoring the pxdecayconst at start of day.
  // I'm not sure if one is better than the other rn.
  double px_decay_scale_pct_;
  double px_decay_scale_px_;

  double sz_decay_;
  // For a second sense of size decay. Used only for some sizedecaytypes.
  double sz_decay_2_;
  SizeDecayType sz_decay_type_;

  double min_px_weight_;

  // Secondary min_px_weight calculation. To prevent the climb from sending us
  // out of control. For example, if pxdecay is really high (eg. px_decay_ ==
  // 1), then we might go through the entirety of the book, which is could be
  // pretty crazy. Let's not do this.
  double max_pct_away_;

  // If this is set, then on our first full recalc,
  // during our first recalc we check how many levels we go before we
  // die out, and then multiply by this amount to get
  // insides_before_recalc_live_;
  double ignore_recalc_thresh_mult_;
  int insides_before_recalc_live_;

  int insides_before_recalc_;

  double bounding_val_;
  bool return_price_;
  CalcStyle calc_style_;
  bool use_flagged_;

  bool first_tick_ = true;
  double bb_;
  double ba_;
  int insides_since_recalc_;
  double bid_stren_;
  double ask_stren_;

  // Used for shadow calc.
  double shadow_b_;
  double shadow_a_;
  bool use_shadow_;
  double shadow_frac_;
  double shadow_decay_frac_;

  bool calc_this_round_ = false;

  // Let's swith to doing full_recalcs only after depth updates, for the sake of
  // data quality. So we mark when we want to do a full_recalc, and then wait
  // until the next depth update occurs.
  bool needs_full_recalc_ = false;
  bool last_msg_book_update_ = false;

  // For debug quality of life.
  bool verbose_full_recalc_ = false;
  bool verbose_msi_ = false;
  bool verbose_lvlcb_ = false;

  double totstren_tdc_;
  double totstren_ema_num_;
  double totstren_ema_denom_;
  int64_t last_totstren_t_;

  double totstren_ema_bound_;
  double totstren_ema_power_;
};

}  // namespace pktrade::signals