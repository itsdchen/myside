#pragma once

#include "base_tempo.h"
#include "pktrade/enums/basic_enums.h"
#include "pktrade/mktdata/md_beacon.h"

namespace pktrade::tempos {

class MsgTempo : public BaseTempo, public pktrade::md::BeaconListener {
 public:
  MsgTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  std::vector<pktrade::BookId> getBookIds() override;

  // BeaconListener callbacks.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) {}

  // eventually, more callbacks as we implement more things.

 protected:
  SymbolId symbol_;
  std::vector<pktrade::Market> books_;
};

}; // namespace pktrade::tempos
