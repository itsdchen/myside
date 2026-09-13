#include "sig_file.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

#include "csv.h"
#include <csv.hpp>

namespace pktrade::signals {

SigFile::SigFile(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf)
    : Signal(signal_id, sig_conf, sf), cur_val_idx_(0) {
  file_path_tmpl_ = sig_conf["file_path_tmpl"].GetString();
}

bool SigFile::isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
                     const rapidjson::Value& all_signals_array) const {
  // Just check if the paths are the same.

  if (std::string(other_conf["type"].GetString()) != "SigFile") {
    return false;
  }

  // Check if signal and books are identical.
  if (file_path_tmpl_ != std::string(other_conf["file_path_tmpl"].GetString())) {
    return false;
  }

  return true;
};

// OK. Taking a break. I thnk maybe I can try to get rapidcsv intsead. Let's try
// it out.
void SigFile::subscribeData(MDBeacon* beacon) {
  // At this "subscribe" by reading the file and seeking.

  std::string real_file_path =
      fmt::format(fmt::runtime(file_path_tmpl_), pktrade::GlobalVar::date_);

  io::CSVReader<2> in(real_file_path);
  in.set_header("sample_time", "sig_val");
  int64_t sample_time;
  double sig_val;
  while (in.read_row(sample_time, sig_val)) {
    // do stuff with the data
    timestamps_.push_back(sample_time);
    values_.push_back(sig_val);
    // std::cout << time_utils::msToString(time) << " : " << sig_val <<
    // std::endl;
  }

  // OK. I guess the csvreader is stupid and will just break.
  // I don't

  if (timestamps_.size() == 0) {
    throw std::runtime_error("Um, we didn't read any times from the file.");
  }

  cur_val_ = values_[0];
  if (timestamps_.size() > 1) {
    next_time_ = timestamps_[1];
  }
};

//
double SigFile::getValue() {
  if (cur_val_idx_ == timestamps_.size() - 1) {
    // Just return the last value. Nothing to go down.
    return cur_val_;
  }

  // Potentially increment ourselves.
  if (time_utils::nowToNs() >= next_time_) {
    for (uint i = cur_val_idx_; i < timestamps_.size() - 1; i++) {
      // Basically. Increment and update our pointer while we're
      // reading signals from the past.
      if (time_utils::nowToNs() >= timestamps_[i]) {
        cur_val_idx_ = i;
      } else {
        break;
      }
    }

    // with cur_val_idx set properly, update value and next_time.
    cur_val_ = values_[cur_val_idx_];
    if (cur_val_idx_ < timestamps_.size() - 1) {
      next_time_ = timestamps_[cur_val_idx_ + 1];
    }
  }

  // setValue too. This might be used for things, like when the ordex uses
  // midchanges.
  setValue(cur_val_);

  return cur_val_;
};

}; // namespace pktrade::signals
