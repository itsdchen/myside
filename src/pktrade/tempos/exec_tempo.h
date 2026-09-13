#pragma once

#include "base_tempo.h"
#include "pktrade/enums/basic_enums.h"

namespace pktrade::tempos {

class ExecTempo : public BaseTempo, public pktrade::md::BeaconListener {
 public:
  ExecTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) override;

  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  std::vector<pktrade::BookId> getBookIds() override;

  // Beaconlistener callbacks.

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {}
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {}
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk) {};

 protected:
  SymbolId symbol_;
  std::vector<pktrade::Market> books_;
};

} // namespace pktrade::tempos
