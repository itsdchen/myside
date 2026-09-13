
#include "pktrade/signals/signal.h"
#include "pktrade/signals/signal_factory.h"

#include "pktrade/tempos/base_tempo.h"
#include "pktrade/tempos/tempo_factory.h"

#include <rapidjson/document.h>
#include <rapidjson/filereadstream.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"

#include "pktrade/clock.h"
#include "pktrade/event_loop.h"

#include "pktrade/mktdata/md_beacon.h"

#include "pktrade/mdapi.h"

#include "msgprintclass.h"

#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <magic_enum.hpp>

#include <filesystem>
#include <glog/logging.h>
#include <set>
#include <stdio.h>

/*

Sample command:

To do a naive scan:
bin.debug/signalscanner --date 20230103 --feature-conf SigMid.json
--secs-between-print 5 --time-fullstr --sigs a_name --mode BASIC

Can also give --signals-path <out_csv>
to have it write to csv.

For the regress chain:

Returns mode:
../../bin.debug/signalscanner --date 20230103 --sampling-conf regress.json
--mode REGRESS --regress-stage RETURNS --times-path matic_times_20230103.csv
--returns-path matic_returns_20230103.csv

// note: for returns mode, can give a multiflier option to expand the returns.

Features mode:
../../bin/signalscanner --date 20230103 --sampling-conf regress.json
--mode REGRESS --regress-stage FEATURES --times-path matic_times_20230103.csv
--signals-path libra_vals_20230103.csv --feature-conf AvgLibraVariations.json
--sigs libra_v1

Dense mode:
../../bin.debug/signalscanner --date 20230103 --sampling-conf regress.json
--mode DENSE
--signals-path matic_returns_20230103.csv

Features mode:
../../bin/signalscanner --date 20230103 --sampling-conf regress.json
--mode REGRESS --regress-stage FEATURES --times-path matic_times_20230103.csv
--signals-path libra_vals_20230103.csv --feature-conf AvgLibraVariations.json
--sigs libra_v1

XTX mode:
TODO


FastSim mode:

~/tradefi/pkt_3/bin/signalscanner --date 20230501 --sampling-conf
sampling_pred.json --mode FASTSIM --sigs "pred_model_Linear" --signals-path
pred_vals.csv

~/tradefi/pkt_3/bin.debug/signalscanner --date 20230501 --sampling-conf
sampling_ref.json --mode FASTSIM --sigs "ref_Mid" --signals-path ref_vals.csv

*/

enum class TimeFmt { HWTS, FULL };
enum class RegressStage { RETURNS, FEATURES, DENSE, XTX };

// introducing... another signalscanning mode for fastsims.
// This will take in a tempo(tradecaller), a signal, and scribe values out to a
// file.
enum class Mode { BASIC, REGRESS, FASTSIM };

using namespace pktrade;

class SignalScanner : public pktrade::tempos::TempoListener,
                      public pktrade::signals::SignalListener {
 public:
  SignalScanner() {};

  // Prints out features at regular- or per-update cadences.
  void initBasicScanner(rapidjson::Document& feature_conf, double secs_between_print,
                        bool time_fullstr, std::vector<std::string> sig_names,
                        std::string sigs_path);

  // The two-step regression chain.
  void initRegressScanner(rapidjson::Document& sampling_conf, rapidjson::Document& feature_conf,
                          RegressStage regress_stage, std::string times_path,
                          std::string returns_path, std::string sigs_path,
                          std::vector<std::string> sig_names, double gettable_filter_t,
                          std::vector<double> markout_mults);

  void initFastsimScanner(rapidjson::Document& fastsim_conf, std::string times_path,
                          std::string sigs_path, std::vector<std::string> sig_names);

  void subscribe();

  // Happens for basic scanner
  void basicScan();

  void onTempo(int tempo_id) override;

  void onSignalValue(int sig_id, double value) override;
  void onSignalValidity(int sig_id, bool valid) override;

  std::vector<pktrade::BookId> getBookIds();

  // Writes out queues to disk, if we so wish.
  void finalize();

 private:
  TimeFmt time_fmt_;

  // For writing.
  std::string returns_path_;
  std::string sigs_path_;
  std::string times_path_;

  Mode mode_;

  // In both cases, this is only the signals we care about. Not all the signals
  // in the world.
  std::vector<pktrade::signals::Signal*> signals_;

  // **************************************
  // For BASIC mode.
  std::vector<bool> print_this_sig_;

  // If we elect to print at a regular cadence (vs. on every signal update.)
  double secs_between_scan_;
  int64_t t_last_scan_;
  bool time_fullstr_;
  pktrade::tempos::BaseTempo* basic_tempo_;

  // In basic mode, we might just write this out.
  std::ofstream* returns_fstr_ = nullptr;
  std::ofstream* sigs_fstr_ = nullptr;

  // **************************************
  // REGRESS mode.
  RegressStage regress_stage_;
  bool old_style_rets_ = false;

  pktrade::tempos::BaseTempo* sampling_tempo_;
  pktrade::tempos::BaseTempo* markout_tempo_;

  int sampling_tempo_id_;
  int markout_tempo_id_;

  int sampling_tempo_count_;
  int sampling_tempo_thresh_;

  int markout_tempo_count_;
  std::vector<int> markout_tempo_threshes_;

  // If we want to limit the scale of returns.
  double markout_cap_ = 1;

  pktrade::signals::Signal* tgt_sig_;

  std::vector<std::string> pred_feat_names_;

  // The samples we want to write.
  std::vector<int64_t> sample_t_;

  std::vector<int> sample_mkout_counts_;
  std::vector<double> sample_tgt_val_;

  int cur_processing_sample_idx_;

  std::vector<std::vector<double>> markouts_;

  // For writing actual features out to disk.
  // This can probably be made cleaner. I'm sorry mother.
  std::vector<std::vector<double>> sampled_sig_vals_;

  // Basic implementation of a gettablefilter.
  // rtt gettablefilter.
  double gettable_filter_t_ms_ = 0;
  int n_gettable_filtered_out_ = 0;

  // **************************************
  // FASTSIM mode.
  // In this case, we keep track of a few things too, that are similar types
  // as above, but I'm going to keep them as different objects and names so
  // it's easier to keep track of. Can refactor this later but for now,
  // easier to understand and debug.
  std::vector<int64_t> fastsim_times_;
  std::vector<double> fastsim_sig_vals_;

  pktrade::signals::Signal* fastsim_signal_;
  pktrade::tempos::BaseTempo* fastsim_sampler_;
};

void SignalScanner::initBasicScanner(rapidjson::Document& signal_conf, double secs_between_scan,
                                     bool time_fullstr, std::vector<std::string> sig_names,
                                     std::string sigs_path) {
  secs_between_scan_ = secs_between_scan;
  mode_ = Mode::BASIC;
  time_fullstr_ = time_fullstr;
  sigs_path_ = sigs_path;
  if (sigs_path_ != "") {
    // Create the ofstream, output the signal names.
    sigs_fstr_ = new std::ofstream(sigs_path_);
  }

  if (secs_between_scan_ > 0) {
    // Construct a tempo in-place.
    // TODO: do this.
    std::string time_based_conf =
        "{\"name\": \"time_based\", \"type\": \"TimeTempo\", \"thresh\":" +
        std::to_string(secs_between_scan_) + "}";
    rapidjson::Document tempo_doc;
    tempo_doc.Parse(time_based_conf.c_str());
    basic_tempo_ = pktrade::GlobalVar::tf_->getOrMakeByConf(tempo_doc);
    basic_tempo_->addListener(this);
  }

  // std::cout << pktrade::util::rjson_to_str(signal_conf["signals"]) <<
  // std::endl;

  pktrade::GlobalVar::sf_->makeAllSignals(signal_conf["signals"].GetArray());

  if (sig_names.size() == 0) {
    // If no specification, print them all.
    signals_ = pktrade::GlobalVar::sf_->getAllSignals();
    print_this_sig_.assign(signals_.size(), true);
  } else {
    for (auto sig_name : sig_names) {
      signals_.push_back(pktrade::GlobalVar::sf_->getByName(sig_name));
      print_this_sig_.push_back(true);
    }
  }

  for (int i = 0; i < signals_.size(); i++) {
    // Potentially subscribe if we're updating at every.
    if (secs_between_scan_ == 0) {
      signals_[i]->addListener(this);
    }
  }

  if (sigs_fstr_ != nullptr) {
    // Create the ofstream, output the signal names.
    *sigs_fstr_ << "time,";
    for (size_t i = 0; i < sig_names.size(); i++) {
      if (i == sig_names.size() - 1) {
        *sigs_fstr_ << sig_names[i] << std::endl;
      } else {
        *sigs_fstr_ << sig_names[i] << ",";
      }
    }
  } else {
    std::cout << "Time,";
    for (int i = 0; i < signals_.size(); i++) {
      std::cout << signals_[i]->getName();
      if (i != signals_.size() - 1) {
        std::cout << ",";
      }
    }
    std::cout << std::endl;
  }
}

void SignalScanner::initRegressScanner(rapidjson::Document& sampling_conf,
                                       rapidjson::Document& signal_conf, RegressStage regress_stage,
                                       std::string times_path, std::string returns_path,
                                       std::string sigs_path, std::vector<std::string> sig_names,
                                       double gettable_filter_t,
                                       std::vector<double> markout_mults) {
  mode_ = Mode::REGRESS;
  regress_stage_ = regress_stage;
  cur_processing_sample_idx_ = 0;
  // Save these for writing, yeah?
  times_path_ = times_path;
  returns_path_ = returns_path;
  sigs_path_ = sigs_path;

  gettable_filter_t_ms_ = gettable_filter_t;

  if (regress_stage == RegressStage::RETURNS) {
    // Make tempos
    pktrade::GlobalVar::tf_->makeAllTempos(sampling_conf["tempos"].GetArray());

    old_style_rets_ = false;
    if (sampling_conf.HasMember("old_style_rets")) {
      old_style_rets_ = sampling_conf["old_style_rets"].GetBool();
    }

    // Make signals
    pktrade::GlobalVar::sf_->makeAllSignals(sampling_conf["signals"].GetArray());

    // Get the sampling and markout tempos.
    sampling_tempo_ = pktrade::GlobalVar::tf_->getByName(
        sampling_conf["signalscanner"]["sampling_tempo"].GetString());
    if (sampling_tempo_ == nullptr) {
      throw std::runtime_error("SignalScanner: error creating sampling tempo");
    }

    sampling_tempo_id_ = sampling_tempo_->getID();

    markout_tempo_ = pktrade::GlobalVar::tf_->getByName(
        sampling_conf["signalscanner"]["markout_tempo"].GetString());
    if (markout_tempo_ == nullptr) {
      throw std::runtime_error("SignalScanner: error creating markout tempo");
    }
    markout_tempo_id_ = markout_tempo_->getID();

    sampling_tempo_->addListener(this);
    markout_tempo_->addListener(this);

    // Get the thresholds.
    sampling_tempo_count_ = 0;
    sampling_tempo_thresh_ = sampling_conf["signalscanner"]["sampling_thresh"].GetDouble();

    markout_tempo_count_ = 0;
    double base_markout_thresh = sampling_conf["signalscanner"]["markout_thresh"].GetDouble();

    // Then, go and push these into the various markout threshes.
    for (const double& markout_mult : markout_mults) {
      markout_tempo_threshes_.push_back(int(base_markout_thresh * markout_mult));
    }

    if (sampling_conf["signalscanner"].HasMember("markout_cap")) {
      markout_cap_ = sampling_conf["signalscanner"]["markout_cap"].GetDouble();
    }

    // Get the ref signal.
    tgt_sig_ =
        pktrade::GlobalVar::sf_->getByName(sampling_conf["signalscanner"]["tgt_sig"].GetString());
    if (tgt_sig_ == nullptr) {
      throw std::runtime_error(std::string("SignalScanner: Could not create tgt_sig.") +
                               sampling_conf["signalscanner"]["tgt_sig"].GetString());
    }

    // Subscribe to the ref signal.
    tgt_sig_->addListener(this);
  } else if (regress_stage == RegressStage::FEATURES) {
    if (sig_names.size() == 0) {
      throw std::runtime_error("SignalScanner: Must specify signal names for regress stage.");
    }

    // construct a filetempo in-place.
    // The rough format that this should follow.
    std::string filetempo_str =
        "{\"name\": \"file_tempo\", \"type\": \"FileTempo\", \"file_path\": "
        "\"" +
        times_path + "\", \"thresh\": 1}";

    rapidjson::Document tempo_doc;
    tempo_doc.Parse(filetempo_str.c_str());

    sampling_tempo_ = pktrade::GlobalVar::tf_->getOrMakeByConf(tempo_doc);
    sampling_tempo_->addListener(this);
    sampling_tempo_id_ = sampling_tempo_->getID();

    pktrade::GlobalVar::sf_->makeAllSignals(signal_conf["signals"].GetArray());

    for (std::string sig_name : sig_names) {
      pktrade::signals::Signal* sig = pktrade::GlobalVar::sf_->getByName(sig_name);
      if (sig == nullptr) {
        throw std::runtime_error(std::string("SignalScanner: Could not create signal: ") +
                                 sig_name);
      }
      signals_.push_back(sig);
    }
  } else if (regress_stage == RegressStage::DENSE) {
    pktrade::GlobalVar::tf_->makeAllTempos(sampling_conf["tempos"].GetArray());

    old_style_rets_ = false;
    if (sampling_conf.HasMember("old_style_rets")) {
      old_style_rets_ = sampling_conf["old_style_rets"].GetBool();
    }

    // Make all of these signals.
    pktrade::GlobalVar::sf_->makeAllSignals(sampling_conf["signals"].GetArray());
    pktrade::GlobalVar::sf_->makeAllSignals(signal_conf["signals"].GetArray());
    sampling_tempo_ = pktrade::GlobalVar::tf_->getByName(
        sampling_conf["signalscanner"]["sampling_tempo"].GetString());
    if (sampling_tempo_ == nullptr) {
      throw std::runtime_error("SignalScanner: error creating sampling tempo");
    }

    sampling_tempo_id_ = sampling_tempo_->getID();
    markout_tempo_ = pktrade::GlobalVar::tf_->getByName(
        sampling_conf["signalscanner"]["markout_tempo"].GetString());
    if (markout_tempo_ == nullptr) {
      throw std::runtime_error("SignalScanner: error creating markout tempo");
    }
    markout_tempo_id_ = markout_tempo_->getID();
    sampling_tempo_->addListener(this);
    markout_tempo_->addListener(this);

    sampling_tempo_count_ = 0;
    sampling_tempo_thresh_ = sampling_conf["signalscanner"]["sampling_thresh"].GetDouble();

    markout_tempo_count_ = 0;

    // we don't support multiples in dense mode. It's too hard right now.
    double markout_tempo_thresh = sampling_conf["signalscanner"]["markout_thresh"].GetDouble();
    if (markout_mults.size() > 1) {
      throw std::runtime_error("SignalScanner: markout_mults not supported in dense mode.");
    }
    markout_tempo_threshes_.push_back(int(markout_tempo_thresh));

    if (sampling_conf["signalscanner"].HasMember("markout_cap")) {
      markout_cap_ = sampling_conf["signalscanner"]["markout_cap"].GetDouble();
    }

    tgt_sig_ =
        pktrade::GlobalVar::sf_->getByName(sampling_conf["signalscanner"]["tgt_sig"].GetString());

    if (tgt_sig_ == nullptr) {
      throw std::runtime_error(std::string("SignalScanner: Could not create tgt_sig.") +
                               sampling_conf["signalscanner"]["tgt_sig"].GetString());
    }

    for (std::string sig_name : sig_names) {
      pktrade::signals::Signal* sig = pktrade::GlobalVar::sf_->getByName(sig_name);
      if (sig == nullptr) {
        throw std::runtime_error(std::string("SignalScanner: Could not create signal: ") +
                                 sig_name);
      }
      signals_.push_back(sig);
    }

  } else if (regress_stage == RegressStage::XTX) {
    // TODO
  }
}

void SignalScanner::initFastsimScanner(rapidjson::Document& fastsim_conf, std::string times_path,
                                       std::string sigs_path, std::vector<std::string> sig_names) {
  mode_ = Mode::FASTSIM;

  times_path_ = times_path;
  sigs_path_ = sigs_path;

  // There should only be one.
  pktrade::GlobalVar::tf_->makeAllTempos(fastsim_conf["tempos"].GetArray());

  // Get the sampling and markout tempos.
  fastsim_sampler_ = pktrade::GlobalVar::tf_->getByName(
      fastsim_conf["signalscanner"]["tradecall_tempo"].GetString());
  if (fastsim_sampler_ == nullptr) {
    throw std::runtime_error("SignalScanner: error creating tradecalling tempo");
  }
  // Don't keep its id, there should only be one tradecaller.

  fastsim_sampler_->addListener(this);

  pktrade::GlobalVar::sf_->makeAllSignals(fastsim_conf["signals"].GetArray());
  fastsim_signal_ = pktrade::GlobalVar::sf_->getByName(sig_names[0]);
  if (fastsim_signal_ == nullptr) {
    throw std::runtime_error(std::string("SignalScanner: Could not create signal: ") +
                             sig_names[0]);
  }
}

void SignalScanner::subscribe() {
  // Subscribe tempos
  pktrade::GlobalVar::tf_->subscribeAll();

  // Subscribe signals
  pktrade::GlobalVar::sf_->subscribeAll();
}

// Happens for basic scanner
// There's got to be a better way. Oh well for now.
void SignalScanner::basicScan() {
  if (sigs_fstr_ == nullptr) {
    if (time_fullstr_) {
      std::cout << time_utils::nowToStr() << ",";
    } else {
      std::cout << time_utils::nowToNs() << ",";
    }

    for (int i = 0; i < signals_.size(); i++) {
      std::cout << signals_[i]->getValue();
      if (i < signals_.size() - 1) {
        std::cout << ",";
      }
    }
    std::cout << std::endl;
  } else {
    if (time_fullstr_) {
      *sigs_fstr_ << time_utils::nowToStr() << ",";
    } else {
      *sigs_fstr_ << time_utils::nowToNs() << ",";
    }

    for (int i = 0; i < signals_.size(); i++) {
      *sigs_fstr_ << signals_[i]->getValue();
      if (i < signals_.size() - 1) {
        *sigs_fstr_ << ",";
      }
    }
    *sigs_fstr_ << std::endl;
  }
}

void SignalScanner::onTempo(int tempo_id) {
  if (mode_ == Mode::BASIC) {
    // Do a basicScan. This was a callback from our timing tempo.
    basicScan();
  } else if (mode_ == Mode::REGRESS) {
    if (regress_stage_ == RegressStage::RETURNS) {
      // Increment in both
      if (tempo_id == sampling_tempo_id_) {
        sampling_tempo_count_++;
      }
      if (tempo_id == markout_tempo_id_) {
        markout_tempo_count_++;
      }

      if (tempo_id == sampling_tempo_id_) {
        if (sampling_tempo_count_ > sampling_tempo_thresh_) {
          //  TODO: apply some transformation or filter.
          sample_t_.push_back(pktrade::time_utils::nowToNs());
          // For calculating return.

          // Call it at the end of this time interval so we can mark after
          // tradeflagging happens.
          // This is more important to have right then
          // the markout timer. Also because our signals
          // (for regression) will be sampled at EOPacket too.

          if (old_style_rets_) {
            sample_tgt_val_.push_back(tgt_sig_->getValue());
            sample_mkout_counts_.push_back(markout_tempo_count_);

          } else {
            pktrade::GlobalVar::event_loop_->onTimeout(
                std::chrono::milliseconds(0), [&, mkout_count = markout_tempo_count_] {
                  this->sample_tgt_val_.push_back(this->tgt_sig_->getValue());
                  this->sample_mkout_counts_.push_back(mkout_count);
                });
          }

          // Reset the count. Until next time. Do it before
          // the callback.
          sampling_tempo_count_ = 0;
        }
      }
      // Not an "else if", because dedup could have made
      // them the same tempo.
      if (tempo_id == markout_tempo_id_) {
        // Try to flush my queues.
        size_t num_samples_ = sample_t_.size();
        for (size_t i = cur_processing_sample_idx_; i < sample_mkout_counts_.size(); i++) {
          // For the current sample, check if we should be
          // calculating further markouts.
          while (markouts_.size() <= i) {
            markouts_.push_back({});
          }
          std::vector<double>& cur_sample_markouts = markouts_[i];
          for (int markout_thresh_idx = cur_sample_markouts.size();
               markout_thresh_idx < markout_tempo_threshes_.size(); markout_thresh_idx++) {
            // Check if we can
            if (markout_tempo_count_ - sample_mkout_counts_[i] <
                markout_tempo_threshes_[markout_thresh_idx]) {
              // Do not flush, so do not pass go.
              break;
            } else {
              double px_return = (tgt_sig_->getValue() - sample_tgt_val_[i]) / sample_tgt_val_[i];

              if (std::abs(px_return) > markout_cap_) {
                // copysign: magnitude, sign
                px_return = std::copysign(markout_cap_, px_return);
              }
              cur_sample_markouts.push_back(px_return);
            }
            if (cur_sample_markouts.size() == markout_tempo_threshes_.size()) {
              cur_processing_sample_idx_++;
            }
          }
        }
      }

    } else if (regress_stage_ == RegressStage::FEATURES) {
      // Just let me know. Did I do it or did I do it.

      // Collect all features. Write them out.
      // When calling, we should sample these feature values and
      // add them to feat_vals.
      std::vector<double> sample_sig_vals_;

      for (pktrade::signals::Signal* pred_sig_ : signals_) {
        sample_sig_vals_.push_back(pred_sig_->getValue());
      }
      // Add this to the full collection.
      sampled_sig_vals_.push_back(sample_sig_vals_);
    } else if (regress_stage_ == RegressStage::DENSE) {
      // Very similar to a both the returns and features modes
      if (tempo_id == sampling_tempo_id_) {
        sampling_tempo_count_++;
      }
      if (tempo_id == markout_tempo_id_) {
        markout_tempo_count_++;
      }

      if (tempo_id == sampling_tempo_id_) {
        if (sampling_tempo_count_ > sampling_tempo_thresh_) {
          sample_t_.push_back(pktrade::time_utils::nowToMs());
          if (old_style_rets_) {
            sample_tgt_val_.push_back(tgt_sig_->getValue());
            sample_mkout_counts_.push_back(markout_tempo_count_);

          } else {
            pktrade::GlobalVar::event_loop_->onTimeout(
                std::chrono::milliseconds(0), [&, mkout_count = markout_tempo_count_] {
                  this->sample_tgt_val_.push_back(this->tgt_sig_->getValue());
                  this->sample_mkout_counts_.push_back(mkout_count);
                });
          }

          // Collect all features. Write them out.
          // When calling, we should sample these feature values and
          // add them to feat_vals.
          std::vector<double> sample_sig_vals_;

          for (pktrade::signals::Signal* pred_sig_ : signals_) {
            sample_sig_vals_.push_back(pred_sig_->getValue());
          }
          // Add this to the full collection.
          sampled_sig_vals_.push_back(sample_sig_vals_);

          // Reset the count. Until next time. Do it before
          // the callback.
          sampling_tempo_count_ = 0;
        }
      }
      // Not an "else if", because dedup could have made
      // them the same tempo.
      if (tempo_id == markout_tempo_id_) {
        // Try to flush my queues.
        size_t num_samples_ = sample_t_.size();
        for (size_t i = cur_processing_sample_idx_; i < sample_mkout_counts_.size(); i++) {
          // Check if we should flush.

          // Recall in dense mode we only have one markout tempo thresh.
          if (markout_tempo_count_ - sample_mkout_counts_[i] < markout_tempo_threshes_[0]) {
            // Do not flush, so do not pass go.
            break;
          } else {
            // Otherwise. Might have a chance.
            double px_return = (tgt_sig_->getValue() - sample_tgt_val_[i]) / sample_tgt_val_[i];

            if (std::abs(px_return) > markout_cap_) {
              // copysign: magnitude, sign
              px_return = std::copysign(markout_cap_, px_return);
            }

            markouts_.push_back({px_return});
            cur_processing_sample_idx_++;
          }
        }
      }
    }
  } else if (mode_ == Mode::FASTSIM) {
    // Potentially push things back. But might overwrite things if
    // we're getting the callback at the same timestamp.
    if (fastsim_times_.size() == 0) {
      fastsim_times_.push_back(time_utils::nowToNs());
      fastsim_sig_vals_.push_back(fastsim_signal_->getValue());
    } else if (fastsim_times_[fastsim_times_.size() - 1] == time_utils::nowToNs()) {
      // Overwrite the final signal value.
      // Specific to this guy.
      fastsim_sig_vals_[fastsim_sig_vals_.size() - 1] = fastsim_signal_->getValue();
    } else {
      // Push back.
      fastsim_times_.push_back(time_utils::nowToNs());
      fastsim_sig_vals_.push_back(fastsim_signal_->getValue());
    }
  }
}

void SignalScanner::onSignalValue(int sig_id, double value) {
  // Only care about the things we want to basicscan. So, basicscan.
  if (mode_ == Mode::BASIC) {
    // Only care about the things we want to basicscan. So, basicscan.
    basicScan();

  } else if (mode_ == Mode::REGRESS && regress_stage_ == RegressStage::RETURNS) {
    // Consider doing gettablefilter stuff.
    if (old_style_rets_) {
      for (auto i = sample_t_.size(); i > 0; i--) {
        if (time_utils::nowToNs() - gettable_filter_t_ms_ * 1000 < sample_t_[i - 1]) {
          // I could record how many samples I've popped.
          sample_t_.pop_back();
          sample_tgt_val_.pop_back();
          sample_mkout_counts_.pop_back();
          n_gettable_filtered_out_++;
        } else {
          break;
        }
        if (sample_t_.size() == 0) {
          break;
        }
      }

    } else {
      pktrade::GlobalVar::event_loop_->onTimeout(
          std::chrono::milliseconds(0),
          [&, mkout_count = markout_tempo_count_, now_t = time_utils::nowToNs()] {
            // ONLY filter stuff out if sample_t and sample_tgt_val_ have the
            // same size. Kind of unfortunate but it's because setting callbacks
            // via onTimeout right now does not necessarily mean the callbacks
            // get delivered in the same order. So we might filter based on
            // sample_t but then pop_back on sample_tgt_val unnecessarily. This
            // means that gettablefilter won't be fully correct, but it'd work
            // some of the time. And we don't have to rewrite the poller.
            if (sample_t_.size() != sample_tgt_val_.size()) {
              return;
            }

            for (auto i = sample_t_.size(); i > 0; i--) {
              if (now_t - this->gettable_filter_t_ms_ * 1000 < this->sample_t_[i - 1]) {
                // I could record how many samples I've popped.
                this->sample_t_.pop_back();
                this->sample_tgt_val_.pop_back();
                this->sample_mkout_counts_.pop_back();

                this->n_gettable_filtered_out_++;
              } else {
                break;
              }
              // We may have gone too far.
              if (this->sample_t_.size() == 0) {
                break;
              }
            }
          });
    }
  }
}

void SignalScanner::onSignalValidity(int sig_id, bool valid) {
  // Prob do nothing.
}

// For getting subscriptions.
std::vector<pktrade::BookId> SignalScanner::getBookIds() {
  // Ask the factories what the subscriptions are.
  std::set<pktrade::BookId> ids;

  std::vector<pktrade::BookId> sig_book_ids = pktrade::GlobalVar::sf_->getBookIds();
  ids.insert(sig_book_ids.cbegin(), sig_book_ids.cend());

  std::vector<pktrade::BookId> tempo_book_ids = pktrade::GlobalVar::tf_->getBookIds();

  ids.insert(tempo_book_ids.cbegin(), tempo_book_ids.cend());

  // Turn it into a vec.
  std::vector<pktrade::BookId> id_vec(ids.cbegin(), ids.cend());
  return id_vec;
}

void SignalScanner::finalize() {
  // Call it if we're in regressmode.
  if (mode_ == Mode::REGRESS) {
    if (regress_stage_ == RegressStage::RETURNS) {
      // Possible that we didn't finish all the sample points. So write
      // out a final limit on the things we print.

      std::cout << " Gettable filter filtered out " << n_gettable_filtered_out_ << " samples "
                << std::endl;

      int num_lines_out = (int)std::min(markouts_.size(), sample_t_.size());

      if (markouts_.size() != sample_t_.size()) {
        markouts_.resize(num_lines_out);
        sample_t_.resize(num_lines_out);
      }

      // Go through sample_t and markouts_ and only add if
      // the returns are valid.
      std::vector<int64_t> filtered_times;
      std::vector<std::vector<double>> filtered_markouts;

      // TODO: This could be more efficient.
      for (auto i = 0; i < markouts_.size(); i++) {
        bool should_skip = false;
        for (int j = 0; j < markouts_[i].size(); j++) {
          if (!std::isfinite(markouts_[i][j])) {
            should_skip = true;
          }
        }

        // Don't print ret-things if not given.
        if (markouts_[i].size() == 0) {
          should_skip = true;
        }
        if (markouts_[i].size() < markout_tempo_threshes_.size()) {
          should_skip = true;
        }

        if (should_skip) {
          continue;
        }

        // Otherwise, go and add them and then do some cappint goo.
        std::vector<double> cur_markouts;
        for (int j = 0; j < markouts_[i].size(); j++) {
          if (std::isfinite(markouts_[i][j])) {
            double cur_mkout = markouts_[i][j];
            if (std::abs(cur_mkout) > markout_cap_) {
              // copysign: magnitude, sign
              cur_mkout = std::copysign(markout_cap_, cur_mkout);
            }
            cur_markouts.push_back(cur_mkout);
          }
        }

        filtered_times.push_back(sample_t_[i]);
        filtered_markouts.push_back(cur_markouts);
      }

      std::cout << "size is " << filtered_times.size() << std::endl;
      pktrade::util::write_csv_times(times_path_, filtered_times);
      pktrade::util::write_csv_multireturns(returns_path_, filtered_markouts);

      // Done, I think.
    } else if (regress_stage_ == RegressStage::FEATURES) {
      // Write it down.
      pktrade::util::write_csv(sigs_path_, sampled_sig_vals_);
    } else if (regress_stage_ == RegressStage::DENSE) {
      // Go through sample_t and markouts_ and only add if
      // the returns are valid.

      int num_lines_out = (int)std::min(markouts_.size(), sampled_sig_vals_.size());

      if (markouts_.size() != sampled_sig_vals_.size()) {
        markouts_.resize(num_lines_out);
        int to_rm = sampled_sig_vals_.size() - num_lines_out;
        // resize() doesn't quite work.x
        for (int i = 0; i < to_rm; i++) {
          sampled_sig_vals_.pop_back();
        }
      }

      std::vector<double> filtered_markouts;
      // TODO: This could be more efficient.
      for (auto i = 0; i < markouts_.size(); i++) {
        if (std::isfinite(markouts_[i][0])) {
          double cur_mkout = markouts_[i][0];
          if (std::abs(cur_mkout) > markout_cap_) {
            // copysign: magnitude, sign
            cur_mkout = std::copysign(markout_cap_, cur_mkout);
          }
          filtered_markouts.push_back(cur_mkout);
        }
      }

      pktrade::util::write_csv_returns(returns_path_, filtered_markouts);
      pktrade::util::write_csv(sigs_path_, sampled_sig_vals_);
    }
  } else if (mode_ == Mode::FASTSIM) {
    // Write out signal values and times to the csv.
    std::vector<std::vector<double>> sigs_data;
    sigs_data.emplace_back(fastsim_sig_vals_);

    // Write it out.
    pktrade::util::write_csv_times_and_doubles(sigs_path_, fastsim_times_, sigs_data);

    // Write the times out.
    pktrade::util::write_csv_times(times_path_, fastsim_times_);
  }
}

int main(int argc, char** argv) {
  google::InitGoogleLogging(argv[0]);

  // printf("Running cmd:\n");
  // for (int i = 0; i < argc; i++) {
  //   printf("%s ", argv[i]);
  // }
  // printf("\n");

  std::string date;
  std::string start_date;
  std::string end_date;
  std::string feature_conf;
  std::string sampling_conf;

  std::string start_t_str = "03:00:00 America/New_York";
  std::string end_t_str = "16:00:00 America/New_York";
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";

  std::string times_path;
  std::string returns_path;
  std::string signals_path;

  // If given a comma-separated string of multiples, generates
  std::string markout_mults_str = "1";
  std::vector<double> markout_mults;

  // If given, will multiply the
  double markout_thresh_mult = 1;

  double secs_between_print = 0;

  // When we print out, have option of int64 print or the real time.
  bool time_fullstr = false;
  bool regress_mode = false;

  double gettable_filter_t;

  std::string wanted_signals_str;
  std::vector<std::string> wanted_signals;

  std::string regress_stage_str = "RETURNS";
  RegressStage regress_stage = RegressStage::RETURNS;

  bool print_every_tick = false;

  std::string mode_str = "BASIC";

  CLI::App cmd_flags("signalscanner");
  cmd_flags.add_option("-d,--date", date, "Date")->required();
  cmd_flags.add_option("--end-date", end_date, "End Date");
  cmd_flags.add_option("--feature-conf", feature_conf, "Feature Conf Path");
  cmd_flags.add_option("--sampling-conf", sampling_conf, "Sampling Conf Path");

  cmd_flags.add_option("-s,--start", start_t_str, "Start Time");
  cmd_flags.add_option("-e,--end", end_t_str, "End Time");
  cmd_flags.add_option("--datadir", data_dir, "Data Dir");

  // Outputs.
  cmd_flags.add_option("--times-path", times_path, "Times Path");
  cmd_flags.add_option("--returns-path", returns_path, "Returns Path");
  cmd_flags.add_option("--signals-path", signals_path, "Signals Path");

  cmd_flags.add_option("--markout-mults", markout_mults, "Markout multipliers, comma-separated")
      ->delimiter(',');

  cmd_flags.add_option("-t,--secs-between-print", secs_between_print, "Seconds between basic scan");

  cmd_flags.add_flag("--time-fullstr", time_fullstr, "Print time in full string format");

  cmd_flags.add_option("--sigs", wanted_signals_str, "Wanted Signals");
  cmd_flags.add_flag("--regress-mode", regress_mode, "Regress Mode");
  cmd_flags.add_option("--regress-stage", regress_stage_str, "Stage of regress chain");

  cmd_flags.add_option("--mode", mode_str);
  // Basically everytickview.
  cmd_flags.add_flag("--print-every-tick", print_every_tick, "Output every tick of the book.");

  // misc options

  // Use this to scale the markout thresh, for situations where we might regress
  // with multiple targets.
  cmd_flags.add_option("--markout-thresh-mult", markout_thresh_mult,
                       "Scale the markout thresh by this amount");

  PARSE(cmd_flags, argc, argv);

  pktrade::GlobalVar::date_ = date;

  if (end_date == "") {
    end_date = date;
  }

  // If the start_t_str is between 18:00 and 23:59, then we should
  // consider it the next L1Date (our day rollover period happens at 18:00 every day.)
  start_date = date;

  std::string start_dt = start_date + " " + start_t_str;
  std::string end_dt = end_date + " " + end_t_str;

  date::zoned_seconds start_t = pktrade::time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = pktrade::time_utils::dtToSecs(end_dt);

  // Validate that we're using America/New_York timezone (required for 18:00 ET day rollover logic)
  std::string tz_name = std::string(start_t.get_time_zone()->name());
  if (tz_name != "America/New_York") {
    throw std::runtime_error(fmt::format(
      "Day rollover logic requires America/New_York timezone, but got: {}. "
      "Please fix timezone handling in time_utils::dtToSecs or update this logic.",
      tz_name));
  }

  // Parse the hour from start time in America/New_York and adjust start_date if >= 18:00 ET
  auto start_local = start_t.get_local_time();
  auto start_dp = date::floor<date::days>(start_local);
  auto start_tod = date::make_time(start_local - start_dp);
  int start_hour = start_tod.hours().count();

  if (start_hour >= 18) {
    // Subtract one day from the date (trading day starts at 18:00 ET)
    // date format is YYYYMMDD
    int year = std::stoi(date.substr(0, 4));
    int month = std::stoi(date.substr(4, 2));
    int day = std::stoi(date.substr(6, 2));

    date::year_month_day ymd = date::year{year}/date::month{month}/date::day{day};
    date::sys_days prev_day = date::sys_days{ymd} - date::days{1};
    date::year_month_day prev_ymd = date::year_month_day{prev_day};

    start_date = fmt::format("{:04d}{:02d}{:02d}",
                            int(prev_ymd.year()),
                            unsigned(prev_ymd.month()),
                            unsigned(prev_ymd.day()));

    // Recreate start_dt and start_t with adjusted start_date
    start_dt = start_date + " " + start_t_str;
    start_t = pktrade::time_utils::dtToSecs(start_dt);
  }

  // Set the globalvars.
  pktrade::GlobalVar::start_t_ = pktrade::time_utils::zsToTp(start_t);
  pktrade::GlobalVar::end_t_ = pktrade::time_utils::zsToTp(end_t);

  // Handle case where start time is after end time (e.g., 18:01 start, 16:00 end next day)
  if (pktrade::GlobalVar::start_t_ > pktrade::GlobalVar::end_t_) {
    pktrade::GlobalVar::start_t_ -= std::chrono::hours(24);
  }

  pktrade::GlobalVar::event_loop_ = new pktrade::EventLoop();

  // Create factories.
  pktrade::GlobalVar::tf_ = new pktrade::tempos::TempoFactory();
  pktrade::GlobalVar::sf_ = new pktrade::signals::SignalFactory();

  // Parse the wanted-signals.
  wanted_signals = pktrade::util::split_str(wanted_signals_str, ',');

  Mode mode = magic_enum::enum_cast<Mode>(mode_str).value();

  // split the markout mults if given
  std::istringstream mmstr_iss(markout_mults_str);
  std::string one_markout_mult;

  while (std::getline(mmstr_iss, one_markout_mult, ',')) {
    markout_mults.push_back(std::stod(one_markout_mult));
  }

  SignalScanner sscanner;
  rapidjson::Document feature_d;
  if (mode == Mode::BASIC || (mode == Mode::REGRESS && regress_stage_str == "FEATURES") ||
      (mode == Mode::REGRESS && regress_stage_str == "DENSE") ||
      (mode == Mode::REGRESS && regress_stage_str == "XTX")) {
    if (!std::filesystem::exists(feature_conf)) {
      throw std::runtime_error("Feature conf at \"" + feature_conf + "\" does not exist.");
    }

    feature_d = pktrade::util::read_json_file(feature_conf);
    if (!feature_d.IsObject()) {
      throw std::runtime_error("Feature conf at \"" + feature_conf +
                               "\" misparsed. Perhaps the json is ill-formatted..");
    }
  }

  rapidjson::Document sampling_d;
  if (mode == Mode::REGRESS || mode == Mode::FASTSIM) {
    if (!std::filesystem::exists(sampling_conf)) {
      throw std::runtime_error("Expected sampling_conf, but file at \"" + sampling_conf +
                               "\" does not exist.");
    }

    sampling_d = pktrade::util::read_json_file(sampling_conf);
    if (!sampling_d.IsObject()) {
      throw std::runtime_error("Feature conf at \"" + sampling_conf +
                               "\" misparsed. Perhaps the json is ill-formatted..");
    }
  }

  // Basic mode.
  if (mode == Mode::BASIC) {
    sscanner.initBasicScanner(feature_d, secs_between_print, time_fullstr, wanted_signals,
                              signals_path);
  } else if (mode == Mode::REGRESS) {
    regress_stage = magic_enum::enum_cast<RegressStage>(regress_stage_str).value();

    // In regress mode, we need the times_path and signals_path, absolutely.
    // In both cases, either write- or read- the times-path
    // returns mode, we write the returns to the signals path
    // In features mode, we write the predictive features to that path.
    if ((regress_stage == RegressStage::RETURNS || regress_stage == RegressStage::FEATURES) &&
        times_path == "") {
      throw std::runtime_error("Regress mode selected, but no times-path specified.");
    }

    if ((regress_stage == RegressStage::RETURNS) && returns_path == "") {
      throw std::runtime_error("Regress mode (Returns) selected, but no returns-path "
                               "specified.");
    }

    if ((regress_stage == RegressStage::DENSE || regress_stage == RegressStage::FEATURES) &&
        signals_path == "") {
      throw std::runtime_error("Regress mode selected, but no signals-path "
                               "specified.");
    }

    // TODO: test out gettability.
    sscanner.initRegressScanner(sampling_d, feature_d, regress_stage, times_path, returns_path,
                                signals_path, wanted_signals, 0, markout_mults);

  } else if (mode == Mode::FASTSIM) {
    if (times_path == "") {
      throw std::runtime_error("fastsim mode selected, but no times-path specified.");
    }

    // Also expect a signals_path
    if (signals_path == "") {
      throw std::runtime_error("Fastsim mode selected, but no signals-path "
                               "specified.");
    }
    sscanner.initFastsimScanner(sampling_d, times_path, signals_path, wanted_signals);
  }

  // Get the BookIDs
  std::vector<pktrade::BookId> sub_bookids = sscanner.getBookIds();

  if (sub_bookids.size() == 0) {
    // Possible in case of filetempo or timetempo.
    sub_bookids.emplace_back(BookId{Market::BinanceSPOT, SymbolId{"BTCUSDT"}});
  }

  pktrade::GlobalVar::book_manager_ = new BookManager(sub_bookids.begin(), sub_bookids.end());

  // Create the MDBeacon.
  // beacon gets made after the bm.
  pktrade::GlobalVar::md_beacon_ = new pktrade::md::MDBeacon(
      pktrade::GlobalVar::book_manager_, sub_bookids.begin(), sub_bookids.end());

  // My streams.
  // Use start_date to get the correct files for overnight sessions that span 24h.
  std::vector<std::pair<std::filesystem::path, Market>> streams =
      pktrade::util::stream_paths(data_dir, sub_bookids, start_date);

  // I think the histfeed should be global too..

  HistoricalFeed feed(pktrade::GlobalVar::book_manager_, streams.begin(), streams.end(),
                      std::stoi(pktrade::GlobalVar::date_));
  pktrade::GlobalVar::event_loop_->setPoller(std::make_unique<SimPoller>());
  pktrade::GlobalVar::event_loop_->registerPollables(feed);

  sscanner.subscribe();
  // pktrade::GlobalVar::tf_->subscribeAll();
  pktrade::GlobalVar::md_beacon_->subscribeAndPrepare(pktrade::GlobalVar::book_manager_);

  // Possibly use msgprinter
  if (print_every_tick) {
    // This should be fair...
    int print_full_book_every = 1;
    int print_n_levels = 15;
    bool skip_msgs = false;
    MsgPrinter msgprinter(time_fullstr, print_full_book_every, print_n_levels, skip_msgs,
                          sub_bookids[0], pktrade::GlobalVar::book_manager_);

    msgprinter.loop = pktrade::GlobalVar::event_loop_;

    for (auto& e : sub_bookids) {
      pktrade::GlobalVar::book_manager_->subscribe(e, &msgprinter);
    }
    feed.addDebugListener(&msgprinter, false);
  }

  // Eventually.
  pktrade::GlobalVar::event_loop_->runUntil(pktrade::GlobalVar::end_t_);

  sscanner.finalize();

  return 0;
}
