#include "file_tempo.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::tempos {

FileTempo::FileTempo(const rapidjson::Value& tempo_conf, int id, TempoFactory* tf)
    : BaseTempo(id, tempo_conf["name"].GetString()) {
  // TODO:
  file_path_ = tempo_conf["file_path"].GetString();

  // Substitute in the date if we can.
  file_path_ = fmt::format(fmt::runtime(file_path_), pktrade::GlobalVar::date_);

  // Read in the times. Place the first callback.
  cb_times_ = pktrade::util::read_csv_times(file_path_);
  cur_cb_idx_ = 0;

  // Place first callback.
  if (cb_times_.size() > 0) {
    // Make callback.

    Clock::time_point cb_time = time_utils::nsToTp(cb_times_[cur_cb_idx_]);

    pktrade::GlobalVar::event_loop_->onTimeout(cb_time, [&] { this->onTimer(); });
    cur_cb_idx_++;
  }
}

bool FileTempo::isSame(const rapidjson::Value& other_tempo_conf, TempoFactory* tf) {
  // Just check that the file is the same.
  return file_path_ == other_tempo_conf["file_path"].GetString();
}

void FileTempo::onTimer() {
  // Fire.
  fire();
  // Make the next cb.

  // Increment.
  if (cur_cb_idx_ < cb_times_.size()) {
    // Make the next call
    Clock::time_point cb_time = time_utils::nsToTp(cb_times_[cur_cb_idx_]);

    pktrade::GlobalVar::event_loop_->onTimeout(cb_time, [&] { this->onTimer(); });

    cur_cb_idx_++;
  }
}

} // namespace pktrade::tempos
