#pragma once

#include "base_tempo.h"
#include "pktrade/enums/basic_enums.h"
#include "pktrade/mktdata/md_beacon.h"

/*

    Disclaimer. This isn't going to be immediate or most correct.
    But oh well. It's used approximately for measuring rates for the metronome.
    But do NOT use it for something like tradecalling yet. Because we're only
    being approximately correct, not absolutely correct.


*/

namespace pktrade::tempos {

class InsideTempo : public BaseTempo, public pktrade::md::BeaconListener {
 public:
  InsideTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  std::vector<pktrade::BookId> getBookIds() override;

  // BeaconListener callbacks.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {}
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {}
  void onTrade(const LevelBook& bk, const Trade& trd) {}
  void onFinal(const LevelBook& bk);

  // eventually, more callbacks as we implement more things.

 protected:
  SymbolId symbol_;
  std::vector<pktrade::Market> books_;

  double bb_;
  double ba_;
};

}; // namespace pktrade::tempos
