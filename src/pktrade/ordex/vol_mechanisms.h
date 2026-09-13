#pragma once

#include <cmath>
#include <string>

#include <fmt/format.h>
#include <glog/logging.h>
#include <rapidjson/document.h>

#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::ordex {

// Shared vol/liq threshold adjustment mechanisms.
// Owns EMS state and config for mechanisms 1-4.
// Both RelWideMM2 and AlphaRelWideMM compose this.
class VolMechanisms {
 public:
  void parseConfig(const std::string& symbol, const rapidjson::Value& conf) {
    symbol_ = symbol;

    // Mechanism 1: Remote vol EMS widening
    if (conf.HasMember("vol_widen_coef")) {
      vol_widen_coef_ = conf["vol_widen_coef"].GetDouble();
      if (conf.HasMember("vol_widen_tdc"))
        vol_widen_tdc_ms_ = conf["vol_widen_tdc"].GetDouble() * 1000;
      if (conf.HasMember("vol_widen_scale"))
        vol_widen_scale_ = conf["vol_widen_scale"].GetDouble();
      if (conf.HasMember("vol_widen_additive"))
        vol_widen_additive_ = conf["vol_widen_additive"].GetBool();
      LOG(INFO) << fmt::format("({}) vol_widen enabled: coef={} tdc={}s scale={} additive={}",
                               symbol_, vol_widen_coef_, vol_widen_tdc_ms_ / 1000, vol_widen_scale_,
                               vol_widen_additive_);
    }

    // Mechanism 2: Normalized vol widening
    if (conf.HasMember("vol_norm_coef")) {
      vol_norm_coef_ = conf["vol_norm_coef"].GetDouble();
      if (conf.HasMember("vol_norm_tdc"))
        vol_norm_tdc_ms_ = conf["vol_norm_tdc"].GetDouble() * 1000;
      LOG(INFO) << fmt::format("({}) vol_norm enabled: coef={} tdc={}s",
                               symbol_, vol_norm_coef_, vol_norm_tdc_ms_ / 1000);
    }

    // Mechanism 3: Vol ratio widening
    if (conf.HasMember("vol_ratio_coef")) {
      vol_ratio_coef_ = conf["vol_ratio_coef"].GetDouble();
      if (conf.HasMember("vol_ratio_short_tdc"))
        vol_ratio_short_tdc_ms_ = conf["vol_ratio_short_tdc"].GetDouble() * 1000;
      if (conf.HasMember("vol_ratio_long_tdc"))
        vol_ratio_long_tdc_ms_ = conf["vol_ratio_long_tdc"].GetDouble() * 1000;
      LOG(INFO) << fmt::format("({}) vol_ratio enabled: coef={} short_tdc={}s long_tdc={}s",
                               symbol_, vol_ratio_coef_,
                               vol_ratio_short_tdc_ms_ / 1000, vol_ratio_long_tdc_ms_ / 1000);
    }

    // Mechanism 4: Liquidity depth sided widening
    if (conf.HasMember("liq_depth_coef")) {
      liq_depth_coef_ = conf["liq_depth_coef"].GetDouble();
      if (conf.HasMember("liq_depth_notional"))
        liq_depth_notional_ = conf["liq_depth_notional"].GetDouble();
      if (conf.HasMember("liq_depth_tdc"))
        liq_depth_tdc_ms_ = conf["liq_depth_tdc"].GetDouble() * 1000;
      LOG(INFO) << fmt::format("({}) liq_depth enabled: coef={} notional={} tdc={}s",
                               symbol_, liq_depth_coef_, liq_depth_notional_, liq_depth_tdc_ms_ / 1000);
    }

    // Mechanism 5: Implied-horizon vol widening (symmetric).
    //
    // Estimate per-second vol of remote mid as σ_per_√s = sqrt(E[ret²]/Δt),
    // then translate to an implied stdev over a configurable horizon and
    // widen by that. Coef = "how many implied-horizon stdevs of safety
    // margin". The horizon (default 2s) corresponds to roughly the
    // place_rtt + min_ord_lifetime + cancel_rtt window of exposure for an
    // existing quote.
    //
    // Optional shape / reference modes (orthogonal):
    //   shape ∈ {linear, ramp, sigmoid}: how the input value maps to widen
    //   reference ∈ {absolute, zscore, spread_normalized}: what input represents
    //
    // Decoupled from Mechanism 2: uses its own tdc/coef/horizon so the
    // existing vol_norm parameters can remain untouched on configs that use
    // them with the implicit (no-horizon) interpretation.
    if (conf.HasMember("vol_implied_coef")) {
      vol_implied_coef_ = conf["vol_implied_coef"].GetDouble();
      if (conf.HasMember("vol_implied_tdc_s"))
        vol_implied_tdc_ms_ = conf["vol_implied_tdc_s"].GetDouble() * 1000;
      if (conf.HasMember("vol_implied_horizon_s"))
        vol_implied_horizon_s_ = conf["vol_implied_horizon_s"].GetDouble();

      if (conf.HasMember("vol_implied_shape")) {
        std::string s = conf["vol_implied_shape"].GetString();
        if (s == "linear") vol_implied_shape_ = ShapeLinear;
        else if (s == "ramp") vol_implied_shape_ = ShapeRamp;
        else if (s == "sigmoid") vol_implied_shape_ = ShapeSigmoid;
        else throw std::runtime_error(fmt::format(
            "({}) unknown vol_implied_shape: '{}' (linear|ramp|sigmoid)", symbol_, s));
      }
      if (conf.HasMember("vol_implied_shape_thr"))
        vol_implied_shape_thr_ = conf["vol_implied_shape_thr"].GetDouble();
      if (conf.HasMember("vol_implied_shape_scale"))
        vol_implied_shape_scale_ = conf["vol_implied_shape_scale"].GetDouble();
      if (conf.HasMember("vol_implied_shape_max"))
        vol_implied_shape_max_ = conf["vol_implied_shape_max"].GetDouble();

      if (conf.HasMember("vol_implied_reference")) {
        std::string s = conf["vol_implied_reference"].GetString();
        if (s == "absolute") vol_implied_ref_ = RefAbsolute;
        else if (s == "zscore") vol_implied_ref_ = RefZscore;
        else if (s == "spread_normalized") vol_implied_ref_ = RefSpreadNormalized;
        else throw std::runtime_error(fmt::format(
            "({}) unknown vol_implied_reference: '{}' (absolute|zscore|spread_normalized)",
            symbol_, s));
      }
      if (conf.HasMember("vol_implied_baseline_tdc_s"))
        vol_implied_baseline_tdc_ms_ = conf["vol_implied_baseline_tdc_s"].GetDouble() * 1000;

      LOG(INFO) << fmt::format(
          "({}) vol_implied enabled: coef={} tdc={}s horizon={}s shape={} ref={}",
          symbol_, vol_implied_coef_,
          vol_implied_tdc_ms_ / 1000, vol_implied_horizon_s_,
          vol_implied_shape_, vol_implied_ref_);
    }

    // Mechanism 6: Trade-depth widening (symmetric).
    //
    // Per market-trade event, compute depth_rel = max(0, |opposite_best -
    // trade_px|) / mid — i.e. how far past the still-best the aggressor's
    // fill landed. Sum-form EMS at trade_depth_tdc_s (default 30s, matching
    // the DiagnosticsMM signal that scored well in our validation).
    //
    // Shape: ramp (linear is special case with thr=0):
    //   y = max(0, x - thr)
    // Reference modes:
    //   absolute (default): x = ems (unitless ratio); dollar_unit = pred_px.
    //                       coef ≈ "how many recent crossing depths to widen by".
    //   spread_normalized:  x = (ems · pred_px) / spread_ema (unitless = depth-in-spreads).
    //                       dollar_unit = spread_ema.
    //                       coef ≈ "how many spreads to widen per unit of depth-in-spreads".
    if (conf.HasMember("trade_depth_coef")) {
      trade_depth_coef_ = conf["trade_depth_coef"].GetDouble();
      if (conf.HasMember("trade_depth_tdc_s"))
        trade_depth_tdc_ms_ = conf["trade_depth_tdc_s"].GetDouble() * 1000;
      if (conf.HasMember("trade_depth_ramp_thr"))
        trade_depth_ramp_thr_ = conf["trade_depth_ramp_thr"].GetDouble();
      if (conf.HasMember("trade_depth_reference")) {
        std::string s = conf["trade_depth_reference"].GetString();
        if (s == "absolute") trade_depth_ref_ = TDRefAbsolute;
        else if (s == "spread_normalized") trade_depth_ref_ = TDRefSpreadNormalized;
        else throw std::runtime_error(fmt::format(
            "({}) unknown trade_depth_reference: '{}' (absolute|spread_normalized)",
            symbol_, s));
      }
      LOG(INFO) << fmt::format(
          "({}) trade_depth enabled: coef={} tdc={}s ramp_thr={} ref={}",
          symbol_, trade_depth_coef_, trade_depth_tdc_ms_ / 1000,
          trade_depth_ramp_thr_, trade_depth_ref_);
    }
  }

  // Call on each remote signal update. Updates all vol EMS trackers.
  void onRemoteUpdate(double new_val) {
    int64_t now = time_utils::nowToMs();

    if (prev_remote_val_ > 0) {
      double ret = (new_val - prev_remote_val_) / prev_remote_val_;
      double abs_ret = std::abs(ret);

      // Mechanism 1: absolute return EMS
      if (vol_widen_coef_ > 0 && last_vol_widen_t_ > 0) {
        double dt = now - last_vol_widen_t_;
        double decay = std::exp(-dt / vol_widen_tdc_ms_);
        remote_vol_ems_ = abs_ret + decay * remote_vol_ems_;
      }

      // Mechanism 2: squared return EMS
      if (vol_norm_coef_ > 0 && last_vol_norm_t_ > 0) {
        double dt = now - last_vol_norm_t_;
        double decay = std::exp(-dt / vol_norm_tdc_ms_);
        vol_norm_var_ems_ = ret * ret + decay * vol_norm_var_ems_;
      }

      // Mechanism 3: short and long absolute return EMS
      if (vol_ratio_coef_ > 0 && last_vol_ratio_t_ > 0) {
        double dt = now - last_vol_ratio_t_;
        double short_decay = std::exp(-dt / vol_ratio_short_tdc_ms_);
        double long_decay = std::exp(-dt / vol_ratio_long_tdc_ms_);
        vol_ratio_short_ems_ = abs_ret + short_decay * vol_ratio_short_ems_;
        vol_ratio_long_ems_ = abs_ret + long_decay * vol_ratio_long_ems_;
      }

      // Mechanism 5: squared-return EMS (sum form).
      // E[EMS_ss(ret²)] ≈ σ²_per_s · tdc, so var_ems / tdc_s ≈ σ²_per_s.
      if (vol_implied_coef_ > 0 && last_vol_implied_t_ > 0) {
        double dt = now - last_vol_implied_t_;
        double decay = std::exp(-dt / vol_implied_tdc_ms_);
        vol_implied_var_ems_ = ret * ret + decay * vol_implied_var_ems_;

        // For zscore reference, maintain a long-window rolling mean and
        // variance of σ_per_√s in mean-form EMA so they're tdc-independent.
        if (vol_implied_ref_ == RefZscore) {
          double tdc_s = vol_implied_tdc_ms_ / 1000.0;
          double sigma_per_sqrt_s = std::sqrt(std::max(0.0, vol_implied_var_ems_) / tdc_s);
          double alpha = 1.0 - std::exp(-dt / vol_implied_baseline_tdc_ms_);
          if (last_baseline_t_ == 0) {
            baseline_mean_ema_ = sigma_per_sqrt_s;
            baseline_var_ema_ = 0;
          } else {
            double dev = sigma_per_sqrt_s - baseline_mean_ema_;
            baseline_mean_ema_ = baseline_mean_ema_ + alpha * dev;
            baseline_var_ema_ = (1 - alpha) * (baseline_var_ema_ + alpha * dev * dev);
          }
          last_baseline_t_ = now;
        }
      }
    }

    prev_remote_val_ = new_val;
    if (vol_widen_coef_ > 0) last_vol_widen_t_ = now;
    if (vol_norm_coef_ > 0) last_vol_norm_t_ = now;
    if (vol_ratio_coef_ > 0) last_vol_ratio_t_ = now;
    if (vol_implied_coef_ > 0) last_vol_implied_t_ = now;
  }

  // Call on each market-trade event (Mechanism 6). Updates trade_depth EMS
  // using the same formula as DiagnosticsMM::onTrade — non-negative
  // depth_rel = how far past still-best the aggressor's fill landed.
  void onTrade(const LevelBook& bk, const Trade& trd) {
    if (trade_depth_coef_ <= 0) return;
    if (bk.nonFull()) return;
    double best_bid = bk.side<BuySide>().begin()->px.toDouble();
    double best_ask = bk.side<SellSide>().begin()->px.toDouble();
    double mid = (best_bid + best_ask) / 2.0;
    if (mid <= 0) return;
    double trade_px = trd.px.toDouble();
    double depth_px = 0;
    if (trd.passive_side == Side::Buy) {
      depth_px = std::max(best_bid - trade_px, 0.0);
    } else if (trd.passive_side == Side::Sell) {
      depth_px = std::max(trade_px - best_ask, 0.0);
    }
    double depth_rel = depth_px / mid;
    int64_t now = time_utils::nowToMs();
    double decay = 0;
    if (last_trade_depth_t_ > 0) {
      decay = std::exp(-(double)(now - last_trade_depth_t_) / trade_depth_tdc_ms_);
    }
    trade_depth_ems_ = depth_rel + decay * trade_depth_ems_;
    last_trade_depth_t_ = now;
  }

  // Call on book updates for liq_depth tracking.
  void onBookUpdate(const LevelBook& bk) {
    if (liq_depth_coef_ == 0) return;
    if (bk.nonFull()) return;
    double mid = (bk.side<BuySide>().begin()->px.toDouble() +
                  bk.side<SellSide>().begin()->px.toDouble()) / 2.0;
    if (mid <= 0) return;
    int64_t now = time_utils::nowToMs();
    double decay = 0;
    if (last_liq_depth_t_ > 0) {
      decay = std::exp(-(double)(now - last_liq_depth_t_) / liq_depth_tdc_ms_);
    }
    liq_depth_buy_ems_ = liqDepth<BuySide>(bk, mid, liq_depth_notional_) + decay * liq_depth_buy_ems_;
    liq_depth_sell_ems_ = liqDepth<SellSide>(bk, mid, liq_depth_notional_) + decay * liq_depth_sell_ems_;
    last_liq_depth_t_ = now;
  }

  // Apply vol widening (mechanisms 1, 2, 3, 5) to buy and sell thresholds.
  // Takes current buy/sell thresh px (needed for mechanism 1 multiplicative mode).
  // Optional spread_ema is consumed only by Mechanism 5 in spread_normalized
  // reference mode; default 0 disables that mode without affecting others.
  // Returns {buy_widen_px, sell_widen_px}.
  std::pair<double, double> getVolWidenPx(double pred_px,
                                          double cur_buy_thresh_px,
                                          double cur_sell_thresh_px,
                                          double spread_ema = 0.0) const {
    double buy_widen = 0, sell_widen = 0;

    // Mechanism 1
    if (vol_widen_coef_ > 0 && remote_vol_ems_ > 0) {
      if (vol_widen_additive_) {
        double vol_add = vol_widen_coef_ * remote_vol_ems_ * pred_px;
        buy_widen += vol_add;
        sell_widen += vol_add;
      } else {
        double vol_mult = vol_widen_coef_ * (remote_vol_ems_ / (remote_vol_ems_ + vol_widen_scale_));
        buy_widen += vol_mult * cur_buy_thresh_px;
        sell_widen += vol_mult * cur_sell_thresh_px;
      }
    }

    // Mechanism 2
    if (vol_norm_coef_ > 0 && vol_norm_var_ems_ > 0) {
      double sigma_per_sqrt_s = std::sqrt(vol_norm_var_ems_ / (vol_norm_tdc_ms_ / 1000.0));
      double w = vol_norm_coef_ * sigma_per_sqrt_s * pred_px;
      buy_widen += w;
      sell_widen += w;
    }

    // Mechanism 3
    if (vol_ratio_coef_ > 0 && vol_ratio_long_ems_ > 1e-12) {
      double ratio = vol_ratio_short_ems_ / vol_ratio_long_ems_;
      double w = vol_ratio_coef_ * ratio * pred_px;
      buy_widen += w;
      sell_widen += w;
    }

    // Mechanism 5: implied-horizon vol widening, with optional shape and
    // reference modes.
    //
    // Pipeline:
    //   1. compute base value x in σ_per_√s (default) or transformed by ref.
    //      - absolute:           x = σ_per_√s
    //      - zscore:             x = (σ_per_√s - rolling_mean) / rolling_std (unitless)
    //      - spread_normalized:  x = (σ_per_√s · √horizon · pred_px) / spread_ema  (unitless)
    //   2. apply shape: y = shape(x)
    //      - linear:  y = x
    //      - ramp:    y = max(0, x - thr)
    //      - sigmoid: y = max · 1/(1 + exp(-(x - thr)/scale))   (max default 1)
    //   3. multiply by appropriate dollar_unit so widen has $ units:
    //      - absolute:           dollar_unit = √horizon · pred_px
    //      - zscore:             dollar_unit = rolling_std · √horizon · pred_px
    //      - spread_normalized:  dollar_unit = spread_ema
    //   4. widen = coef · y · dollar_unit (symmetric)
    if (vol_implied_coef_ > 0 && vol_implied_var_ems_ > 0) {
      double tdc_s = vol_implied_tdc_ms_ / 1000.0;
      double sigma_per_sqrt_s = std::sqrt(vol_implied_var_ems_ / tdc_s);

      double x = sigma_per_sqrt_s;
      double dollar_unit = std::sqrt(vol_implied_horizon_s_) * pred_px;
      bool valid = true;

      switch (vol_implied_ref_) {
        case RefAbsolute:
          // x in 1/√s, dollar_unit = √horizon · pred_px (already set).
          break;
        case RefZscore:
          if (baseline_var_ema_ <= 0) {
            valid = false;
          } else {
            double rolling_std = std::sqrt(baseline_var_ema_);
            x = (sigma_per_sqrt_s - baseline_mean_ema_) / rolling_std;
            dollar_unit = rolling_std * std::sqrt(vol_implied_horizon_s_) * pred_px;
          }
          break;
        case RefSpreadNormalized:
          if (spread_ema <= 0) {
            valid = false;
          } else {
            double implied_move_dollars =
                sigma_per_sqrt_s * std::sqrt(vol_implied_horizon_s_) * pred_px;
            x = implied_move_dollars / spread_ema;
            dollar_unit = spread_ema;
          }
          break;
      }

      if (valid) {
        double y = 0;
        switch (vol_implied_shape_) {
          case ShapeLinear:
            y = x;
            break;
          case ShapeRamp:
            y = std::max(0.0, x - vol_implied_shape_thr_);
            break;
          case ShapeSigmoid:
            y = vol_implied_shape_max_
                / (1.0 + std::exp(-(x - vol_implied_shape_thr_) / vol_implied_shape_scale_));
            break;
        }
        double w = vol_implied_coef_ * y * dollar_unit;
        buy_widen += w;
        sell_widen += w;
      }
    }

    // Mechanism 6: trade-depth widening. Symmetric.
    if (trade_depth_coef_ > 0 && trade_depth_ems_ > 0) {
      double x = trade_depth_ems_;
      double dollar_unit = pred_px;
      bool valid = true;
      if (trade_depth_ref_ == TDRefSpreadNormalized) {
        if (spread_ema <= 0) {
          valid = false;
        } else {
          x = (trade_depth_ems_ * pred_px) / spread_ema;  // depth-in-spreads
          dollar_unit = spread_ema;
        }
      }
      if (valid) {
        double y = std::max(0.0, x - trade_depth_ramp_thr_);
        double w = trade_depth_coef_ * y * dollar_unit;
        buy_widen += w;
        sell_widen += w;
      }
    }

    return {buy_widen, sell_widen};
  }

  // Get sided widening from liq_depth. Returns {buy_widen_px, sell_widen_px}.
  std::pair<double, double> getLiqDepthWidenPx(double pred_px) const {
    if (liq_depth_coef_ <= 0) return {0, 0};
    return {liq_depth_coef_ * liq_depth_buy_ems_ * pred_px,
            liq_depth_coef_ * liq_depth_sell_ems_ * pred_px};
  }

  // Periodic logging of mechanism state.
  void logState() const {
    if (vol_widen_coef_ > 0) {
      // LOG_EVERY_N(INFO, 307) << fmt::format("({}) remote_vol_ems={:.6f}", symbol_, remote_vol_ems_);
    }
    if (vol_norm_coef_ > 0) {
      // LOG_EVERY_N(INFO, 307) << fmt::format("({}) vol_norm sigma_per_sqrt_s={:.6f}",
      //    symbol_, std::sqrt(vol_norm_var_ems_ / (vol_norm_tdc_ms_ / 1000.0)));
    }
    if (vol_ratio_coef_ > 0 && vol_ratio_long_ems_ > 1e-12) {
      LOG_EVERY_N(INFO, 307) << fmt::format("({}) vol_ratio short={:.6f} long={:.6f} ratio={:.3f}",
          symbol_, vol_ratio_short_ems_, vol_ratio_long_ems_,
          vol_ratio_short_ems_ / vol_ratio_long_ems_);
    }
    if (liq_depth_coef_ > 0) {
      LOG_EVERY_N(INFO, 307) << fmt::format("({}) liq_depth buy={:.4f} sell={:.4f}",
          symbol_, liq_depth_buy_ems_, liq_depth_sell_ems_);
    }
  }

  bool hasAnyMechanism() const {
    return vol_widen_coef_ > 0 || vol_norm_coef_ > 0 || vol_ratio_coef_ > 0
           || liq_depth_coef_ > 0 || vol_implied_coef_ > 0
           || trade_depth_coef_ > 0;
  }

  bool hasLiqDepth() const { return liq_depth_coef_ > 0; }
  bool hasTradeDepth() const { return trade_depth_coef_ > 0; }
  bool needsTradeCallbacks() const { return hasLiqDepth() || hasTradeDepth(); }

 private:
  template <typename SideType>
  static double liqDepth(const LevelBook& bk, double mid, double target_notional) {
    if (mid <= 0 || bk.side<SideType>().empty()) return 0;
    double best_px = bk.side<SideType>().begin()->px.toDouble();
    double accum_notional = 0;
    double last_px = best_px;
    for (const auto& lvl : bk.side<SideType>()) {
      double px = lvl.px.toDouble();
      double qty = lvl.qty.toDouble();
      accum_notional += px * qty;
      last_px = px;
      if (accum_notional >= target_notional) break;
    }
    return std::abs(best_px - last_px) / mid;
  }

  std::string symbol_;

  // Shared across vol mechanisms
  double prev_remote_val_ = 0;

  // Mechanism 1: Remote vol EMS
  double vol_widen_coef_ = 0;
  double vol_widen_tdc_ms_ = 4000;
  double vol_widen_scale_ = 0.001;
  bool vol_widen_additive_ = false;
  double remote_vol_ems_ = 0;
  int64_t last_vol_widen_t_ = 0;

  // Mechanism 2: Normalized vol (σ-per-√s)
  double vol_norm_coef_ = 0;
  double vol_norm_tdc_ms_ = 15000;
  double vol_norm_var_ems_ = 0;
  int64_t last_vol_norm_t_ = 0;

  // Mechanism 3: Vol ratio (short/long)
  double vol_ratio_coef_ = 0;
  double vol_ratio_short_tdc_ms_ = 5000;
  double vol_ratio_long_tdc_ms_ = 60000;
  double vol_ratio_short_ems_ = 0;
  double vol_ratio_long_ems_ = 0;
  int64_t last_vol_ratio_t_ = 0;

  // Mechanism 4: Liq depth
  double liq_depth_coef_ = 0;
  double liq_depth_notional_ = 200000;
  double liq_depth_tdc_ms_ = 30000;
  double liq_depth_buy_ems_ = 0;
  double liq_depth_sell_ems_ = 0;
  int64_t last_liq_depth_t_ = 0;

  // Mechanism 5: Implied-horizon vol widening.
  // Same shape as Mechanism 2 (squared-return EMS) but with an explicit
  // horizon parameter so coef is interpretable as "implied-horizon stdevs".
  enum Shape  { ShapeLinear = 0, ShapeRamp = 1, ShapeSigmoid = 2 };
  enum RefMode { RefAbsolute = 0, RefZscore = 1, RefSpreadNormalized = 2 };

  double vol_implied_coef_ = 0;
  double vol_implied_tdc_ms_ = 30000;     // default 30s — the smoothing window
  double vol_implied_horizon_s_ = 2.0;    // default 2s — quote roundtrip exposure
  double vol_implied_var_ems_ = 0;
  int64_t last_vol_implied_t_ = 0;

  // Shape (default linear).
  Shape  vol_implied_shape_ = ShapeLinear;
  double vol_implied_shape_thr_ = 0.0;     // ramp/sigmoid threshold (units match reference)
  double vol_implied_shape_scale_ = 1.0;   // sigmoid steepness
  double vol_implied_shape_max_ = 1.0;     // sigmoid asymptote (in coef-units)

  // Reference (default absolute).
  RefMode vol_implied_ref_ = RefAbsolute;
  // Long-window mean-form EMA stats of σ_per_√s for zscore reference.
  double vol_implied_baseline_tdc_ms_ = 600000;  // 10 min
  double baseline_mean_ema_ = 0;
  double baseline_var_ema_ = 0;
  int64_t last_baseline_t_ = 0;

  // Mechanism 6: Trade-depth widening (symmetric, ramp shape).
  // Sum-form EMS of per-trade depth_rel, matches DiagnosticsMM signal.
  enum TDRefMode { TDRefAbsolute = 0, TDRefSpreadNormalized = 1 };
  double trade_depth_coef_ = 0;
  double trade_depth_tdc_ms_ = 30000;       // default 30s — matches validated signal
  double trade_depth_ramp_thr_ = 0.0;       // 0 = linear
  TDRefMode trade_depth_ref_ = TDRefAbsolute;
  double trade_depth_ems_ = 0;
  int64_t last_trade_depth_t_ = 0;
};

}  // namespace pktrade::ordex
