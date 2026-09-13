#pragma once

#include "base_tempo.h"
#include "pktrade/mktdata/md_beacon.h"

namespace pktrade::tempos {
class FileTempo : public BaseTempo {
 public:
  FileTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf);

  bool isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) override;
  void subscribeData(pktrade::md::MDBeacon* beacon) override {};
  std::vector<pktrade::BookId> getBookIds() override { return {}; }
  void onTimer();

 protected:
  // Some collection of times to create callbacks.
  std::string file_path_;

  // Just place a single cb at a time so we don't
  // add a ton of things in the event loop.
  std::vector<int64_t> cb_times_;
  int cur_cb_idx_;
};
} // namespace pktrade::tempos
