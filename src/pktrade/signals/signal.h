#pragma once

/*
Base class for signals.

*/

#include "pktrade/mktdata/md_beacon.h"
#include "signal_factory.h"
#include <rapidjson/document.h>
#include <vector>

// When I have time, add the beacon, globalvars.

namespace pktrade {
class GlobalVar;
}

namespace pktrade::signals {

using pktrade::md::MDBeacon;
class SignalFactory;

class SignalListener {
 public:
  virtual ~SignalListener() = default;

  virtual void onSignalValue(int sig_id, double value) = 0;
  virtual void onSignalValidity(int sig_id, bool valid) = 0;
};

class Signal {
 public:
  Signal(int signal_id, const rapidjson::Value& conf, SignalFactory* sf);
  virtual ~Signal() = default;

  virtual void subscribeData(pktrade::md::MDBeacon* beacon) = 0;

  virtual bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                      const rapidjson::Value& all_signals_array) const = 0;

  // Subclasses should only return the BookIds they themselves want.
  virtual std::vector<pktrade::BookId> getBookIds() const = 0;

  virtual void reset() = 0;

  void addListener(SignalListener* listener);

  virtual void broadcastValue();
  virtual void broadcastValidity() const;

  virtual double getValue();
  bool getValid() const;
  std::string getName() const;
  int getID() const;

  // Returns the first symbol (if we have one)
  virtual std::string getPrimarySymbol() const;

  // For signals that are similar to a book-type signal, get their notion of
  // "bid" or "ask". For something like a mid, we can gather their flagged
  // bb/ba. For something like a liqpx, we can get their notion of liq_bid,
  // liq_ask. Otherwise, return nothing.
  virtual double getBBMeasure() { return 0; };
  virtual double getBAMeasure() { return 0; };

  virtual double getBBSize() { return 0; };
  virtual double getBASize() { return 0; };

  // For versions where validity is false,
  virtual double getEstimatedValue() { return value_; };

  virtual void fullPrint() {};
  virtual std::string fullDebugString() { return ""; };
  virtual int64_t getLastTransactTime() { return last_transact_t_; }
  bool justGotTrade() const { return just_got_trade_; }

 protected:
  void setValue(double new_value);
  void setValidity(bool new_validity);
  void setVV(double new_value, bool new_validity);

  std::string name_;
  int signal_id_;

  std::vector<int> child_signal_ids_;

  // For extrema. Applies a hard 0 filter after a certain bound.
  double bound_ = 1e10;
  bool zero_out_past_bound_ = true;

  double value_ = 0;
  double last_pub_value_ = 0;
  double pub_thresh_ = 1e-12;
  // TODO: have others declare this for falsehood.
  bool valid_ = true;
  std::vector<SignalListener*> listeners_;

  bool debug_verbose_;
  int64_t last_transact_t_ = 0;
  bool just_got_trade_ = false;
};

} // namespace pktrade::signals
