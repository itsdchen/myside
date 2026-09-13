#pragma once

#include "pktrade/enums/basic_enums.h"
#include "pktrade/mktdata/md_beacon.h"
#include "signal.h"

using namespace pktrade;

// Naive impl of weighted mid.
// Again, exists for single-books.

namespace pktrade::signals {

class SigWMid : public Signal, public pktrade::md::BeaconListener {
 public:
  SigWMid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf);
  ~SigWMid(){};

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;

  void subscribeData(MDBeacon* beacon) override;

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd) {};
  void onFinal(const LevelBook& bk) {};

  void calc();
  void fullRecalc();
  void reset();

 protected:
  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  // TODO: when we have more than one book, generalize this.
  double bb_;
  double ba_;

  double bq_;
  double aq_;

  bool first_tick_;
  bool return_price_;
};

} // namespace pktrade::signals
