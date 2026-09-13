// feedquality: assess external market data feeds against HL as ground truth.
//
// HL ("Hyperliquid") is the price the strategy actually trades against, so
// we treat it as ground truth — not because it's "correct," but because
// predicting it is what matters. Each external feed (e.g. CME futures,
// PolygonFX spot) is evaluated for predictive value vs HL.
//
// Subscribes to one HL book and N external feeds, emits four CSVs per run:
//
//   <prefix>_<date>_grid.csv     1 Hz synchronized mids + per-feed state
//                                (use this for regression / lead-lag analysis)
//   <prefix>_<date>_spikes.csv   one row per ext-feed spike event with
//                                classification (HL_FOLLOWED / SPIKE_REVERTED /
//                                BASIS_SHIFT) — measures PRECISION
//   <prefix>_<date>_recall.csv   one row per (HL spike × ext feed) with
//                                classification (EXT_LED / EXT_MISSED) —
//                                measures RECALL
//   <prefix>_<date>_summary.csv  per-feed totals: tick counts, spike rates,
//                                precision, recall
//
// Spike detection (ext side, "did ext correctly anticipate HL?"):
//   On every ext tick, maintain rolling EMA of basis (ext - hl) and residual
//   variance. When |ext - hl - basis_ema| > max(k_sigma * sigma, bps_floor)
//   open a one-at-a-time watch. After reversion-w-secs, classify based on
//   whether HL caught up (HL_FOLLOWED), residual snapped back (SPIKE_REVERTED)
//   or stayed dislocated (BASIS_SHIFT).
//
// Recall detection (HL side, "did ext predict the HL move that just happened?"):
//   On every HL tick, compute HL return over the last hl-lookback-secs window
//   and compare against rolling sigma of windowed returns. When the HL move
//   exceeds threshold, look back at each ext feed: did its residual exceed
//   that ext's spike threshold during the same window? If yes, EXT_LED;
//   otherwise EXT_MISSED.
//
// Per-feed staleness gate: if a feed's last update is older than
// staleness-secs (think CME's daily 5pm-6pm ET maintenance break, weekends,
// outages), freeze its EMAs and pause spike detection for that feed only.
//
// Example:
//   ./bin/feedquality \
//       --hl-symbol "xyz:SILVER" \
//       --external TopBookCme:SI --external TopBookEquity:SILVER \
//       --date 20260423 --out-prefix /tmp/feedquality_silver

#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <glog/logging.h>
#include <magic_enum.hpp>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"

using namespace pktrade;

namespace {

struct External {
  BookId bid;
  std::string label;            // "Market_Symbol", CSV-friendly
  double mid = 0;
  int64_t last_update_ms = 0;
  int64_t tick_count = 0;

  // Basis EMA: ema(ext_mid - hl_mid), price units.
  double basis_ema = 0;
  bool basis_initialized = false;

  // Variance EMA of residual (ext - hl - basis_ema), price-units squared.
  double vol_var = 0;
  bool vol_initialized = false;

  // One-at-a-time spike watch (ext side / precision).
  bool in_watch = false;
  int64_t watch_start_ms = 0;
  double watch_start_ext = 0;
  double watch_start_hl = 0;
  double watch_start_basis = 0;
  double watch_start_sigma = 0;
  double watch_start_residual = 0;

  // Sliding window of recent residuals, used to ask "did this ext deviate
  // during the W-second window before the HL move?" for recall detection.
  std::deque<std::pair<int64_t, double>> recent_residuals;  // (ts_ms, residual)

  // Tally of spike-event classifications (ext-side / precision).
  int64_t hl_followed_count = 0;
  int64_t spike_reverted_count = 0;
  int64_t basis_shift_count = 0;

  // Tally of HL-spike-event classifications (HL-side / recall).
  int64_t led_hl_count = 0;
  int64_t missed_hl_count = 0;

  // For TopBookCme / TopBookEquity feeds the mid arrives via onQuote and is
  // consumed in onFinal (matches vizdata.cc convention).
  double pending_quote_mid = 0;
};

}  // namespace

class FeedQuality : public BookListener {
 public:
  FeedQuality(BookId hl_bid, std::string hl_label,
              std::vector<External> externals,
              const std::string& grid_path, const std::string& spikes_path,
              const std::string& recall_path, const std::string& summary_path,
              int64_t grid_interval_ms, double k_sigma, double bps_floor,
              double reversion_w_secs, double staleness_secs,
              double basis_tau_secs, double vol_tau_secs,
              double hl_lookback_secs, double hl_debounce_secs,
              double k_sigma_hl, int64_t session_start_ms)
      : hl_bid_(hl_bid),
        hl_label_(std::move(hl_label)),
        externals_(std::move(externals)),
        summary_path_(summary_path),
        grid_interval_ms_(grid_interval_ms),
        k_sigma_(k_sigma),
        bps_floor_(bps_floor),
        reversion_w_ms_(int64_t(reversion_w_secs * 1000)),
        staleness_ms_(int64_t(staleness_secs * 1000)),
        basis_tau_secs_(basis_tau_secs),
        vol_tau_secs_(vol_tau_secs),
        hl_lookback_ms_(int64_t(hl_lookback_secs * 1000)),
        hl_debounce_ms_(int64_t(hl_debounce_secs * 1000)),
        k_sigma_hl_(k_sigma_hl),
        session_start_ms_(session_start_ms) {
    grid_out_.open(grid_path);
    if (!grid_out_.is_open()) {
      throw std::runtime_error("Cannot open grid output: " + grid_path);
    }
    spikes_out_.open(spikes_path);
    if (!spikes_out_.is_open()) {
      throw std::runtime_error("Cannot open spikes output: " + spikes_path);
    }
    recall_out_.open(recall_path);
    if (!recall_out_.is_open()) {
      throw std::runtime_error("Cannot open recall output: " + recall_path);
    }
    writeGridHeader();
    writeSpikesHeader();
    writeRecallHeader();
  }

  ~FeedQuality() {
    if (grid_out_.is_open()) grid_out_.close();
    if (spikes_out_.is_open()) spikes_out_.close();
    if (recall_out_.is_open()) recall_out_.close();
  }

  void subscribe(BookManager* bm) {
    bm->subscribe(hl_bid_, this);
    for (auto& ext : externals_) {
      bm->subscribe(ext.bid, this);
    }
  }

  void scheduleGridSnapshots() {
    GlobalVar::event_loop_->onTimeout(
        GlobalVar::start_t_ + std::chrono::milliseconds(grid_interval_ms_),
        [this] { this->snapshotGrid(); });
  }

  // BookListener — onFinal does the real work.
  void onLevelUpdate(const LevelBook&, const LevelAdd&) override {}
  void onLevelUpdate(const LevelBook&, const LevelModify&) override {}
  void onLevelUpdate(const LevelBook&, const LevelDelete&) override {}
  void onLevelUpdate(const LevelBook&, const Trade&) override {}

  void onQuote(const LevelBook& bk, const Quote& q) override {
    // Session-start gate: HistoricalFeed loads full UTC days; ignore events
    // before the requested session window so EMAs / counters / CSVs don't
    // pick up pre-session state.
    if (time_utils::nowToMs() < session_start_ms_) return;
    int idx = findExternal(bk.getBookID());
    if (idx < 0) return;
    double bb = q.bb.toDouble();
    double ba = q.ba.toDouble();
    double mid = 0;
    if (bb > 0 && ba > 0 && ba >= bb && ba < bb * 2) {
      mid = (bb + ba) / 2.0;
    } else if (q.mid > 0 && q.mid < 1e9) {
      mid = q.mid;
    }
    if (mid > 0 && mid < 1e9) {
      externals_[idx].pending_quote_mid = mid;
    }
  }

  void onFinal(const LevelBook& bk) override {
    int64_t now_ms = time_utils::nowToMs();
    if (now_ms < session_start_ms_) return;  // see onQuote comment
    const BookId& bid = bk.getBookID();

    if (bid == hl_bid_) {
      double mid = midFromBook(bk);
      if (mid > 0) {
        hl_mid_ = mid;
        hl_last_update_ms_ = now_ms;
        hl_tick_count_++;
        // Push current HL price to recent-mids deque, prune old entries.
        hl_recent_mids_.emplace_back(now_ms, mid);
        int64_t hl_prune_before = now_ms - 2 * hl_lookback_ms_;
        while (!hl_recent_mids_.empty() &&
               hl_recent_mids_.front().first < hl_prune_before) {
          hl_recent_mids_.pop_front();
        }
        checkHlSpike(now_ms);
      }
      return;
    }

    int idx = findExternal(bid);
    if (idx < 0) return;
    auto& ext = externals_[idx];
    double mid = ext.pending_quote_mid;
    ext.pending_quote_mid = 0;
    if (mid <= 0 && !bk.nonFull()) {
      mid = midFromBook(bk);
    }
    if (mid <= 0) return;
    ext.mid = mid;
    ext.tick_count++;
    processExternalTick(idx, now_ms);
  }

  // Accessors used for the end-of-run summary.
  int64_t spikeCount() const { return spike_count_; }
  int64_t gridCount() const { return grid_count_; }
  int64_t hlSpikeCount() const { return hl_spike_count_; }
  int64_t hlTickCount() const { return hl_tick_count_; }
  const std::string& hlLabel() const { return hl_label_; }
  const std::vector<External>& externals() const { return externals_; }
  int64_t sessionMs() const {
    auto duration = GlobalVar::end_t_ - GlobalVar::start_t_;
    return std::chrono::duration_cast<std::chrono::milliseconds>(duration).count();
  }

 private:
  static double midFromBook(const LevelBook& bk) {
    if (bk.nonFull()) return 0;
    if (bk.side<BuySide>().empty() || bk.side<SellSide>().empty()) return 0;
    double bb = bk.getSideTop<BuySide>().px.toDouble();
    double ba = bk.getSideTop<SellSide>().px.toDouble();
    if (bb <= 0 || ba <= 0 || ba < bb) return 0;
    return (bb + ba) / 2.0;
  }

  int findExternal(const BookId& bid) const {
    for (size_t i = 0; i < externals_.size(); ++i) {
      if (externals_[i].bid == bid) return int(i);
    }
    return -1;
  }

  void processExternalTick(int idx, int64_t now_ms) {
    auto& ext = externals_[idx];

    if (hl_mid_ <= 0 || hl_last_update_ms_ == 0) {
      ext.last_update_ms = now_ms;
      return;
    }

    bool hl_stale = (now_ms - hl_last_update_ms_) > staleness_ms_;
    bool ext_was_stale = (ext.last_update_ms == 0) ||
                        ((now_ms - ext.last_update_ms) > staleness_ms_);

    // Initialize basis on the very first usable tick, regardless of staleness.
    if (!ext.basis_initialized) {
      ext.basis_ema = ext.mid - hl_mid_;
      ext.basis_initialized = true;
      ext.last_update_ms = now_ms;
      return;
    }

    // After staleness, freeze EMAs and skip detection — basis estimate is
    // unreliable until we get a fresh tick run.
    if (hl_stale || ext_was_stale) {
      ext.last_update_ms = now_ms;
      return;
    }

    double dt_secs = (now_ms - ext.last_update_ms) / 1000.0;
    if (dt_secs < 0) dt_secs = 0;

    double alpha_basis = 1.0 - std::exp(-dt_secs / basis_tau_secs_);
    ext.basis_ema += alpha_basis * (ext.mid - hl_mid_ - ext.basis_ema);

    double residual = ext.mid - hl_mid_ - ext.basis_ema;

    double alpha_vol = 1.0 - std::exp(-dt_secs / vol_tau_secs_);
    if (!ext.vol_initialized) {
      ext.vol_var = residual * residual;
      ext.vol_initialized = true;
    } else {
      ext.vol_var += alpha_vol * (residual * residual - ext.vol_var);
    }

    ext.last_update_ms = now_ms;

    // Push residual to the lookback deque used for recall classification.
    ext.recent_residuals.emplace_back(now_ms, residual);
    int64_t prune_before = now_ms - 2 * hl_lookback_ms_;
    while (!ext.recent_residuals.empty() &&
           ext.recent_residuals.front().first < prune_before) {
      ext.recent_residuals.pop_front();
    }

    if (ext.in_watch) return;
    double sigma = std::sqrt(std::max(ext.vol_var, 0.0));
    double threshold = std::max(k_sigma_ * sigma,
                                bps_floor_ / 10000.0 * hl_mid_);
    if (std::abs(residual) <= threshold) return;

    ext.in_watch = true;
    ext.watch_start_ms = now_ms;
    ext.watch_start_ext = ext.mid;
    ext.watch_start_hl = hl_mid_;
    ext.watch_start_basis = ext.basis_ema;
    ext.watch_start_sigma = sigma;
    ext.watch_start_residual = residual;
    GlobalVar::event_loop_->onTimeout(
        std::chrono::milliseconds(reversion_w_ms_),
        [this, idx] { this->classifyWatch(idx); });
  }

  void classifyWatch(int idx) {
    auto& ext = externals_[idx];
    if (!ext.in_watch) return;

    int64_t now_ms = time_utils::nowToMs();
    double hl_now = hl_mid_;
    double ext_now = ext.mid;

    double current_residual = ext_now - hl_now - ext.basis_ema;
    double hl_delta = hl_now - ext.watch_start_hl;
    double ext_delta = ext_now - ext.watch_start_ext;

    double r0_bps = ext.watch_start_hl > 0
        ? ext.watch_start_residual / ext.watch_start_hl * 10000.0 : 0;
    double r1_bps = hl_now > 0
        ? current_residual / hl_now * 10000.0 : 0;
    double hl_delta_bps = ext.watch_start_hl > 0
        ? hl_delta / ext.watch_start_hl * 10000.0 : 0;
    double ext_delta_bps = ext.watch_start_ext > 0
        ? ext_delta / ext.watch_start_ext * 10000.0 : 0;
    double sigma_bps = ext.watch_start_hl > 0
        ? ext.watch_start_sigma / ext.watch_start_hl * 10000.0 : 0;

    bool hl_followed = (hl_delta * ext.watch_start_residual > 0) &&
                       (std::abs(hl_delta) > 0.3 * std::abs(ext.watch_start_residual));
    bool reverted = std::abs(current_residual) < 0.5 * std::abs(ext.watch_start_residual);
    const char* klass = hl_followed ? "HL_FOLLOWED"
                       : reverted   ? "SPIKE_REVERTED"
                                    : "BASIS_SHIFT";
    if (hl_followed) ext.hl_followed_count++;
    else if (reverted) ext.spike_reverted_count++;
    else ext.basis_shift_count++;

    spikes_out_ << fmt::format(
        "{},{},{},{},{:.6f},{:.6f},{:.6f},{:.6f},"
        "{:.4f},{:.4f},{:.4f},{:.4f},{:.4f},"
        "{:.6f},{:.6f},{}\n",
        ext.watch_start_ms, now_ms, ext.label, klass,
        ext.watch_start_hl, ext.watch_start_ext,
        ext.watch_start_basis, ext.watch_start_sigma,
        r0_bps, r1_bps, hl_delta_bps, ext_delta_bps, sigma_bps,
        hl_now, ext_now, now_ms - ext.watch_start_ms);

    spike_count_++;
    ext.in_watch = false;
  }

  // Recall side: on every HL tick, ask "did HL just move a lot? If so, did
  // each ext predict it?"
  void checkHlSpike(int64_t now_ms) {
    if (hl_recent_mids_.size() < 2) return;
    if (now_ms - last_hl_spike_ms_ < hl_debounce_ms_ &&
        last_hl_spike_ms_ > 0) {
      return;
    }
    // Find the most recent entry at-or-before now - hl_lookback_ms.
    int64_t target = now_ms - hl_lookback_ms_;
    double anchor = 0;
    for (auto& kv : hl_recent_mids_) {
      if (kv.first <= target) anchor = kv.second;
      else break;
    }
    if (anchor <= 0) return;  // Not enough history yet.

    double hl_change_bps = (hl_mid_ - anchor) / anchor * 10000.0;
    double sample = hl_change_bps * hl_change_bps;

    // EMA over change samples. Use a fixed alpha; samples are correlated
    // (overlapping windows) so this is a "typical magnitude" estimator, not
    // a clean stddev.
    if (!hl_change_var_initialized_) {
      hl_change_var_ema_ = sample;
      hl_change_var_initialized_ = true;
      return;  // Don't trigger on the very first sample.
    }
    constexpr double alpha = 0.005;  // ~200-tick effective window.
    hl_change_var_ema_ += alpha * (sample - hl_change_var_ema_);

    double sigma_bps = std::sqrt(std::max(hl_change_var_ema_, 0.0));
    double threshold_bps = std::max(k_sigma_hl_ * sigma_bps, bps_floor_);
    if (std::abs(hl_change_bps) <= threshold_bps) return;

    // HL spike confirmed. For each ext, look back at residuals during the
    // window and classify as LED or MISSED.
    last_hl_spike_ms_ = now_ms;
    hl_spike_count_++;

    int64_t window_start = now_ms - hl_lookback_ms_;
    for (auto& ext : externals_) {
      // Skip ext if it never initialized a basis (no usable data).
      if (!ext.basis_initialized || !ext.vol_initialized) continue;

      // Track the SIGNED residual at the position of max |residual|. EXT_LED
      // requires both magnitude > threshold AND sign agreement with the HL
      // move — an opposite-direction ext blip isn't leading anything.
      double max_abs_resid = 0;
      double max_signed_resid = 0;
      for (auto& kv : ext.recent_residuals) {
        if (kv.first >= window_start) {
          double a = std::abs(kv.second);
          if (a > max_abs_resid) {
            max_abs_resid = a;
            max_signed_resid = kv.second;
          }
        }
      }
      double sigma = std::sqrt(std::max(ext.vol_var, 0.0));
      double ext_threshold_price =
          std::max(k_sigma_ * sigma, bps_floor_ / 10000.0 * hl_mid_);

      bool led = (max_abs_resid > ext_threshold_price) &&
                 (max_signed_resid * hl_change_bps > 0);
      const char* klass = led ? "EXT_LED" : "EXT_MISSED";
      if (led) ext.led_hl_count++;
      else ext.missed_hl_count++;

      double max_resid_bps = hl_mid_ > 0
          ? max_abs_resid / hl_mid_ * 10000.0 : 0;
      double resid_threshold_bps = hl_mid_ > 0
          ? ext_threshold_price / hl_mid_ * 10000.0 : 0;

      recall_out_ << fmt::format(
          "{},{},{:.4f},{:.4f},{:.6f},{:.6f},{},{:.4f},{:.4f},{}\n",
          now_ms, hl_lookback_ms_, hl_change_bps, sigma_bps,
          anchor, hl_mid_, ext.label,
          max_resid_bps, resid_threshold_bps, klass);
    }
  }

  void snapshotGrid() {
    int64_t now_ms = time_utils::nowToMs();
    if (hl_mid_ > 0) {
      std::string line = fmt::format("{},{:.6f},{}", now_ms, hl_mid_,
                                     now_ms - hl_last_update_ms_);
      for (auto& ext : externals_) {
        double sigma_bps = (hl_mid_ > 0 && ext.vol_var > 0)
            ? std::sqrt(ext.vol_var) / hl_mid_ * 10000.0 : 0;
        int64_t age = ext.last_update_ms ? (now_ms - ext.last_update_ms) : -1;
        line += fmt::format(",{:.6f},{},{:.6f},{:.4f}",
                            ext.mid, age, ext.basis_ema, sigma_bps);
      }
      grid_out_ << line << "\n";
      grid_count_++;
    }
    GlobalVar::event_loop_->onTimeout(
        std::chrono::milliseconds(grid_interval_ms_),
        [this] { this->snapshotGrid(); });
  }

  void writeGridHeader() {
    std::string h = "time_ms,hl_mid,hl_age_ms";
    for (auto& ext : externals_) {
      h += fmt::format(",{0}_mid,{0}_age_ms,{0}_basis,{0}_sigma_bps", ext.label);
    }
    grid_out_ << h << "\n";
  }

  void writeSpikesHeader() {
    spikes_out_ << "start_ms,end_ms,source,classification,"
                   "hl_at_start,ext_at_start,basis_at_start,sigma_at_start,"
                   "r0_bps,r1_bps,hl_delta_bps,ext_delta_bps,sigma_bps,"
                   "hl_at_end,ext_at_end,duration_ms\n";
  }

  void writeRecallHeader() {
    recall_out_ << "start_ms,hl_lookback_ms,hl_change_bps,hl_change_sigma_bps,"
                   "hl_at_start,hl_at_end,source,"
                   "max_resid_bps_in_window,resid_threshold_bps,classification\n";
  }

  BookId hl_bid_;
  std::string hl_label_;
  std::vector<External> externals_;
  std::ofstream grid_out_;
  std::ofstream spikes_out_;
  std::ofstream recall_out_;
  std::string summary_path_;
  int64_t grid_interval_ms_;
  double k_sigma_;
  double bps_floor_;
  int64_t reversion_w_ms_;
  int64_t staleness_ms_;
  double basis_tau_secs_;
  double vol_tau_secs_;
  int64_t hl_lookback_ms_;
  int64_t hl_debounce_ms_;
  double k_sigma_hl_;
  int64_t session_start_ms_;

  // HL state.
  double hl_mid_ = 0;
  int64_t hl_last_update_ms_ = 0;
  int64_t hl_tick_count_ = 0;
  std::deque<std::pair<int64_t, double>> hl_recent_mids_;
  double hl_change_var_ema_ = 0;
  bool hl_change_var_initialized_ = false;
  int64_t last_hl_spike_ms_ = 0;
  int64_t hl_spike_count_ = 0;

  int64_t spike_count_ = 0;
  int64_t grid_count_ = 0;
};

namespace {

// Write a tidy per-feed summary CSV. One row per source (HL plus each ext).
void writeSummary(const std::string& path, const FeedQuality& fq, int64_t spike_total) {
  (void)spike_total;
  std::ofstream out(path);
  if (!out.is_open()) {
    fmt::print("warning: could not open summary file {}\n", path);
    return;
  }
  out << "source,is_hl,tick_count,session_ms,ticks_per_sec,"
         "ext_spike_events,hl_followed,spike_reverted,basis_shift,"
         "precision_pct,"
         "hl_spike_events_total,led_hl,missed_hl,recall_pct,"
         "f1_pct\n";

  double session_secs = fq.sessionMs() / 1000.0;
  double hl_tps = session_secs > 0 ? fq.hlTickCount() / session_secs : 0;
  // HL row — most ext-side fields are blank.
  out << fmt::format("{},1,{},{},{:.3f},,,,,,{},,,,\n",
                     fq.hlLabel(), fq.hlTickCount(), fq.sessionMs(), hl_tps,
                     fq.hlSpikeCount());

  int64_t hl_spike_total = fq.hlSpikeCount();
  for (auto& ext : fq.externals()) {
    int64_t ext_total = ext.hl_followed_count + ext.spike_reverted_count + ext.basis_shift_count;
    double precision = ext_total > 0
        ? 100.0 * ext.hl_followed_count / ext_total : 0;
    double recall = hl_spike_total > 0
        ? 100.0 * ext.led_hl_count / hl_spike_total : 0;
    double f1 = (precision + recall > 0)
        ? 2.0 * precision * recall / (precision + recall) : 0;
    double tps = session_secs > 0 ? ext.tick_count / session_secs : 0;
    out << fmt::format(
        "{},0,{},{},{:.3f},"
        "{},{},{},{},{:.2f},"
        "{},{},{},{:.2f},{:.2f}\n",
        ext.label, ext.tick_count, fq.sessionMs(), tps,
        ext_total, ext.hl_followed_count, ext.spike_reverted_count,
        ext.basis_shift_count, precision,
        hl_spike_total, ext.led_hl_count, ext.missed_hl_count, recall, f1);
  }
}

}  // namespace

int main(int argc, char** argv) {
  google::InitGoogleLogging(argv[0]);

  std::string date;
  std::string hl_symbol;
  std::vector<std::string> external_specs;
  std::string out_prefix;
  std::string start_t_str = "18:00:00 America/New_York";
  std::string end_t_str = "17:59:55 America/New_York";
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";
  std::string equity_variant;
  int grid_interval_ms = 1000;
  double k_sigma = 5.0;
  double bps_floor = 3.0;
  double reversion_w_secs = 2.0;
  double staleness_secs = 60.0;
  double basis_tau_secs = 300.0;
  double vol_tau_secs = 60.0;
  double hl_lookback_secs = 2.0;
  double hl_debounce_secs = 2.0;
  double k_sigma_hl = 4.0;

  CLI::App cmd_flags("feedquality — assess external feeds vs HL ground truth");
  cmd_flags.add_option("--hl-symbol", hl_symbol,
                       "Hyperliquid symbol used as ground truth (e.g. xyz:SILVER)")
      ->required();
  cmd_flags.add_option("--external", external_specs,
                       "External feed as Market:Symbol; repeatable. "
                       "E.g. TopBookCme:GC, TopBookEquity:GOLD")
      ->required();
  cmd_flags.add_option("-d,--date", date, "Trade date YYYYMMDD")->required();
  cmd_flags.add_option("-o,--out-prefix", out_prefix,
                       "Output file prefix (creates _grid.csv, _spikes.csv, "
                       "_recall.csv, _summary.csv)")
      ->required();
  cmd_flags.add_option("--datadir", data_dir, "Data directory");
  cmd_flags.add_option("--equity-variant", equity_variant,
                       "Suffix for TopBookEquity dir (e.g. 'Boats' loads from TopBookEquity_Boats/)");
  cmd_flags.add_option("--grid-interval-ms", grid_interval_ms,
                       "Grid sample period (ms)");
  cmd_flags.add_option("--k-sigma", k_sigma,
                       "Ext-spike threshold in sigma units (default 5)");
  cmd_flags.add_option("--bps-floor", bps_floor,
                       "Spike floor in bps of HL mid (default 3)");
  cmd_flags.add_option("--reversion-w-secs", reversion_w_secs,
                       "Ext-spike reversion classification window (s)");
  cmd_flags.add_option("--staleness-secs", staleness_secs,
                       "Per-feed staleness threshold; EMAs and detection "
                       "pause for that feed beyond this gap (s)");
  cmd_flags.add_option("--basis-tau-secs", basis_tau_secs,
                       "Basis EMA time constant (s)");
  cmd_flags.add_option("--vol-tau-secs", vol_tau_secs,
                       "Vol EMA time constant (s)");
  cmd_flags.add_option("--hl-lookback-secs", hl_lookback_secs,
                       "HL-move detection window for recall side (s)");
  cmd_flags.add_option("--hl-debounce-secs", hl_debounce_secs,
                       "Minimum gap between successive HL spike events (s)");
  cmd_flags.add_option("--k-sigma-hl", k_sigma_hl,
                       "HL-move threshold in sigma units (default 4)");
  cmd_flags.add_option("--start-time", start_t_str, "Start time of session");
  cmd_flags.add_option("--end-time", end_t_str, "End time of session");

  PARSE(cmd_flags, argc, argv);

  GlobalVar::date_ = date;

  // Overnight-session handling lifted from vizdata.cc.
  std::string start_date = date;
  std::string end_date = date;
  std::string start_dt = start_date + " " + start_t_str;
  std::string end_dt = end_date + " " + end_t_str;
  date::zoned_seconds start_t = time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = time_utils::dtToSecs(end_dt);
  std::string tz_name = std::string(start_t.get_time_zone()->name());
  if (tz_name == "America/New_York") {
    auto start_local = start_t.get_local_time();
    auto start_dp = date::floor<date::days>(start_local);
    auto start_tod = date::make_time(start_local - start_dp);
    int start_hour = start_tod.hours().count();
    if (start_hour >= 18) {
      int year = std::stoi(date.substr(0, 4));
      int month = std::stoi(date.substr(4, 2));
      int day = std::stoi(date.substr(6, 2));
      date::year_month_day ymd = date::year{year} / date::month{unsigned(month)} /
                                  date::day{unsigned(day)};
      date::sys_days prev_day = date::sys_days{ymd} - date::days{1};
      date::year_month_day prev_ymd = date::year_month_day{prev_day};
      start_date = fmt::format("{:04d}{:02d}{:02d}", int(prev_ymd.year()),
                                unsigned(prev_ymd.month()),
                                unsigned(prev_ymd.day()));
      start_dt = start_date + " " + start_t_str;
      start_t = time_utils::dtToSecs(start_dt);
    }
  }

  GlobalVar::start_t_ = time_utils::zsToTp(start_t);
  GlobalVar::end_t_ = time_utils::zsToTp(end_t);
  if (GlobalVar::start_t_ > GlobalVar::end_t_) {
    GlobalVar::start_t_ -= std::chrono::hours(24);
  }

  BookId hl_bid{Market::Hyperliquid, SymbolId{hl_symbol}};
  std::string hl_label = std::string("Hyperliquid_") + hl_symbol;
  std::replace(hl_label.begin(), hl_label.end(), ':', '_');

  std::vector<External> externals;
  std::vector<BookId> all_books = {hl_bid};
  for (const auto& spec : external_specs) {
    auto colon = spec.find(':');
    if (colon == std::string::npos) {
      throw std::runtime_error("Bad --external (need Market:Symbol): " + spec);
    }
    std::string mkt_str = spec.substr(0, colon);
    std::string sym_str = spec.substr(colon + 1);
    auto mkt_opt = magic_enum::enum_cast<Market>(mkt_str);
    if (!mkt_opt) {
      throw std::runtime_error("Unknown market: " + mkt_str);
    }
    External ext;
    ext.bid = BookId{mkt_opt.value(), SymbolId{sym_str}};
    ext.label = mkt_str + "_" + sym_str;
    externals.push_back(std::move(ext));
    all_books.push_back(BookId{mkt_opt.value(), SymbolId{sym_str}});
  }

  GlobalVar::event_loop_ = new EventLoop();
  GlobalVar::book_manager_ = new BookManager(all_books.begin(), all_books.end());

  std::vector<std::pair<std::filesystem::path, Market>> streams =
      util::stream_paths(data_dir, all_books, start_date, true, true, equity_variant);

  bool any_missing = false;
  for (auto& kv : streams) {
    if (!std::filesystem::exists(kv.first)) {
      any_missing = true;
      fmt::print("warning: missing data file {}\n", kv.first.string());
    }
  }
  if (any_missing) {
    fmt::print("hint: download with overmind/strat_main/tools/md_exists.py "
               "(--market <Market> --sym <Sym> --start {} --end {})\n",
               start_date, date);
  }

  HistoricalFeed feed(GlobalVar::book_manager_, streams.begin(), streams.end(),
                      std::stoi(GlobalVar::date_));
  GlobalVar::event_loop_->setPoller(std::make_unique<SimPoller>());
  GlobalVar::event_loop_->registerPollables(feed);

  std::string grid_path    = out_prefix + "_" + date + "_grid.csv";
  std::string spikes_path  = out_prefix + "_" + date + "_spikes.csv";
  std::string recall_path  = out_prefix + "_" + date + "_recall.csv";
  std::string summary_path = out_prefix + "_" + date + "_summary.csv";

  int64_t session_start_ms = time_utils::tpToMs(GlobalVar::start_t_);
  FeedQuality fq(hl_bid, hl_label, std::move(externals),
                 grid_path, spikes_path, recall_path, summary_path,
                 grid_interval_ms, k_sigma, bps_floor, reversion_w_secs,
                 staleness_secs, basis_tau_secs, vol_tau_secs,
                 hl_lookback_secs, hl_debounce_secs, k_sigma_hl,
                 session_start_ms);
  fq.subscribe(GlobalVar::book_manager_);
  fq.scheduleGridSnapshots();

  fmt::print("feedquality: hl={} externals={} date={} grid_ms={} "
             "k_sigma={} bps_floor={} W={}s staleness={}s "
             "hl_lookback={}s hl_debounce={}s k_sigma_hl={}\n",
             hl_symbol, external_specs.size(), date, grid_interval_ms,
             k_sigma, bps_floor, reversion_w_secs, staleness_secs,
             hl_lookback_secs, hl_debounce_secs, k_sigma_hl);

  GlobalVar::event_loop_->runUntil(GlobalVar::end_t_);

  writeSummary(summary_path, fq, fq.spikeCount());

  fmt::print("feedquality: done. {} grid rows, {} ext spikes, {} HL spikes.\n",
             fq.gridCount(), fq.spikeCount(), fq.hlSpikeCount());
  fmt::print("feedquality: outputs:\n  grid:    {}\n  spikes:  {}\n  recall:  {}\n  summary: {}\n",
             grid_path, spikes_path, recall_path, summary_path);
  return 0;
}
