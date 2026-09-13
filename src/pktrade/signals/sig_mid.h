#pragma once

#include "pktrade/enums/basic_enums.h"
#include "pktrade/mktdata/md_beacon.h"
#include "signal.h"

using namespace pktrade;

namespace pktrade::signals {

class SigMid : public Signal, public pktrade::md::BeaconListener {
 public:
  SigMid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf);
  ~SigMid() {};

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;

  void subscribeData(MDBeacon* beacon) override;

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  // From our interpretation of level-based books, mods only change size,
  // not price. So not used for midpx. This might change if we ever
  // deal with other styles of books.
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd);
  void onFinal(const LevelBook& bk);

  void calc();
  void fullRecalc();
  void reset();

  double getBBMeasure() override { return bb_; };
  double getBAMeasure() override { return ba_; };

  double getBBSize() override { return bbsize_; };
  double getBASize() override { return basize_; };

  double getEstimatedValue() override;

  std::string getPrimarySymbol() const override;

 protected:
  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  // TODO: when we have more than one book, generalize this into multibook
  // inside.
  double bb_;
  double ba_;

  // The second-last
  double bbsize_;
  double basize_;

  bool first_tick_;

  double last_buy_trd_px_;
  double last_sell_trd_px_;

  // I'm just placing this here. But we might need to track how long a book ahs
  // been stuck.
  int64_t last_good_val_ms_;

  bool use_flagged_;
};

// Just adding in bid/ask signals too. So I can start plotting the
// book inside. This is a pretty low-stakes signal, don't
// put too much stock into it.
class SigBid : public Signal, public pktrade::md::BeaconListener {
 public:
  SigBid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf);
  ~SigBid() {};

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;

  void subscribeData(MDBeacon* beacon) override;

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  // From our interpretation of level-based books, mods only change size,
  // not price. So not used for midpx. This might change if we ever
  // deal with other styles of books.
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd) {};
  void onFinal(const LevelBook& bk) {};

  void calc();
  void fullRecalc();
  void reset();
  std::string getPrimarySymbol() const override;

 protected:
  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  // TODO: when we have more than one book, generalize this into multibook
  // inside.
  double bb_;
  bool first_tick_;
};

// Just adding in bid/ask signals too. So I can start plotting the
// book inside. This is a pretty low-stakes signal, don't
// put too much stock into it.
class SigAsk : public Signal, public pktrade::md::BeaconListener {
 public:
  SigAsk(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf);
  ~SigAsk() {};

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;

  void subscribeData(MDBeacon* beacon) override;

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add);
  // From our interpretation of level-based books, mods only change size,
  // not price. So not used for midpx. This might change if we ever
  // deal with other styles of books.
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod);
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del);
  void onTrade(const LevelBook& bk, const Trade& trd) {};
  void onFinal(const LevelBook& bk) {};

  void calc();
  void fullRecalc();
  void reset();
  std::string getPrimarySymbol() const override;

 protected:
  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  // TODO: when we have more than one book, generalize this into multibook
  // inside.
  double ba_;
  bool first_tick_;
};

// Like sigmid, but pulls its value from a quote feed. It doesn't do anything besides either
// get the mid directly, or calculate from the nbbo bid/ask.
class SigQuoteMid : public Signal, public pktrade::md::BeaconListener {
 public:
  SigQuoteMid(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf);
  ~SigQuoteMid() {};

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  std::vector<pktrade::BookId> getBookIds() const override;

  void subscribeData(MDBeacon* beacon) override;

  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {}
  // From our interpretation of level-based books, mods only change size,
  // not price. So not used for midpx. This might change if we ever
  // deal with other styles of books.
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {}
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {}
  void onTrade(const LevelBook& bk, const Trade& trd) {}

  // This is the only thing that matters.
  void onQuote(const LevelBook& bk, const Quote& quote) override;

  // Don't do anything yet I guess.
  void onFinal(const LevelBook& bk) {};

  void reset();
  std::string getPrimarySymbol() const override;

 protected:
  SymbolId symbol_;
  // So far, just one book.
  std::vector<pktrade::Market> books_;

  // Some reference instruments quote the reciprocal price. For example,
  // CME 6J is USD per JPY while xyz:JPY is USDJPY.
  bool invert_ = false;

  // If we skip too many in a row (bid, ask are reasonable), it might've been
  // the old price that was wrong. If we skip 10 in a row then I think we
  // probably accept the new price.
  int n_skipped_in_row_ = 0;

  // Skip the first these of the day
  int first_skipped_count_ = 0;
  int SKIP_FIRST_N = 5;
};

} // namespace pktrade::signals
