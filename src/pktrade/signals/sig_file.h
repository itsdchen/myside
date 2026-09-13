#pragma once

#include "pktrade/enums/basic_enums.h"
#include "signal.h"

/*
    Signal that reads from a file formatted like
    <timestamp>,<value>

    and stores them. Right now, implemented s.t. it only updates itself
    when we call getValue() on it. In the future, can prob implement itself s.t.
   it updates itself. Just would need that part handled very carefully.

    (Thus, rn, just call getValue() on this. Eventually, reimplement and
     make it useful for subscribers.
    )

    Example conf:
    {
        "name": "file_ref_sig",
        "file_path": "/path/to/file/ref_sig_{}.csv"
    }
*/

using namespace pktrade;

namespace pktrade::signals {

class SigFile : public Signal {
 public:
  SigFile(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf);
  ~SigFile(){};

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  // We don't need no books.
  std::vector<pktrade::BookId> getBookIds() const override { return {}; }

  void subscribeData(MDBeacon* beacon) override;

  // Dont need to reset anything.
  void reset() {};

  //
  double getValue() override;

 protected:
  // potentially include {}'s for dates.
  std::string file_path_tmpl_;

  std::vector<int64_t> timestamps_;
  std::vector<double> values_;

  // The idx of current value that we've taken on.
  size_t cur_val_idx_;

  // Just for ease of use.
  double cur_val_;

  // If it exists, the time corresponding to cur_val_idx_+1
  int64_t next_time_;
};

} // namespace pktrade::signals
