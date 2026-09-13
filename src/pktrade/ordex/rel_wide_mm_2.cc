#include "rel_wide_mm_2.h"

#include <fmt/format.h>
#include <glog/logging.h>
#include <magic_enum.hpp>

#include <cmath>
#include <limits>

#include "pktrade/util/urls.h"
#include <cpr/cpr.h>
#include <fmt/format.h>
#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/ordex/protection_helpers.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/supabase_credentials.h"
#include "pktrade/util/symbolizer.h"
#include "pktrade/util/time_utils.h"

namespace pktrade::ordex {

RelWideMM2::RelWideMM2(SymbolId symbol, const rapidjson::Value& ordex_conf,
                       pktrade::signals::SignalFactory* sf, pktrade::tempos::TempoFactory* tf)
    : Ordex(symbol, ordex_conf), last_t_place_(0), last_t_cxl_(0), gen_(std::random_device{}())

{

  for (const rapidjson::Value& book : ordex_conf["markets"].GetArray()) {
    traded_books_.push_back(
        magic_enum::enum_cast<Market>(book.GetString()).value_or(Market::Unknown));
  }

  if (traded_books_.size() != 1) {
    throw std::runtime_error("RelWideMM only supports trading one market at a time. Update the "
                             "class implementation, then remove this.");
  }
  traded_bid_ = BookId{traded_books_[0], symbol_};


  if (ordex_conf.HasMember("riskfree_rate")) {
    riskfree_rate_ = ordex_conf["riskfree_rate"].GetDouble();
    LOG(INFO) << fmt::format("({}) Riskfree rate set to {}", symbol_.get(), riskfree_rate_);
  }

  do_discounting_ = symbolizer::is_future(symbol_.get()) && (riskfree_rate_ > 0);
  LOG(INFO) << fmt::format("({}) do_discounting is {}", symbol_.get(), do_discounting_);

  // Then apply the time-based discounting scheme.
  if (do_discounting_) {
    // This set in conjunction w/ bmac. He'll potentially update this
    // value when FOMC meetings happen and
    // when we get new updates to SPY divident estimates.
    // TODO: make an automated way of figuring this out.

    // Given the trading day, find the exact time the underlying expires,
    // so we can discount it properly.
    // n.b., this is only if our remote price is a future.
    expiry_t_ = pktrade::symbolizer::get_expiry_t_epoch(
        symbol_.get(), time_utils::tpToMs(pktrade::GlobalVar::start_t_));

    // Something must be wrong.
    if (expiry_t_ == 0) {
      throw std::runtime_error("Expiry t is 0 for " + symbol_.get());
    }
  }

  if (ordex_conf.HasMember("periodic_print_premium")) {
    periodic_print_premium_ = ordex_conf["periodic_print_premium"].GetBool();
  }

  if (ordex_conf.HasMember("premium_adjust_mode")) {
    premium_adjust_mode_ = ordex_conf["premium_adjust_mode"].GetInt();
  }

  if (premium_adjust_mode_ == 0) {
    // Get premium ema numbers from conf
    if (ordex_conf.HasMember("premium_ema_coef")) {
      premium_ema_coef_ = ordex_conf["premium_ema_coef"].GetDouble();
      if (premium_adjust_mode_ == 0) {
        if (premium_ema_coef_ > 1 || premium_ema_coef_ < 0) {
          throw std::runtime_error(
              fmt::format("Invalid value of premium_ema_coef {} given", premium_ema_coef_));
        }
      }
      // Potentially let the user adjust the tdc here too.
      if (ordex_conf.HasMember("premium_tdc_s")) {
        premium_tdc_ = ordex_conf["premium_tdc_s"].GetDouble() * 1000;
      }
      LOG(INFO) << fmt::format("({}) Using premium_ema_coef {}, premium_tdc {}", symbol_.get(),
                               premium_ema_coef_, premium_tdc_);
    }
  } else if (premium_adjust_mode_ == 1) {
    premium_adjust_max_thresh_mult_ = ordex_conf["premium_adjust_max_thresh_mult"].GetDouble();
    premium_mode1_adjust_coef_ = ordex_conf["premium_mode1_adjust_coef"].GetDouble();
    if (premium_mode1_adjust_coef_ < 0 || premium_mode1_adjust_coef_ > 1) {
      throw std::runtime_error(fmt::format("Invalid value {} for premium_mode1_adjust_coef given",
                                           premium_mode1_adjust_coef_));
    }
    LOG(INFO) << fmt::format("({}) Using premium adjust mode 1: coef {} max_mult {}", symbol_.get(),
                             premium_mode1_adjust_coef_, premium_adjust_max_thresh_mult_);
  } else if (premium_adjust_mode_ == 2) {
    LOG(INFO) << fmt::format("({}) Using premium adjust mode 2 (position-based)", symbol_.get());
  } else {
    throw std::runtime_error("Unsupported premium adjust mode");
  }

  max_pos_ = Quantity{ordex_conf["max_pos"].GetString()};
  order_size_ = Quantity{ordex_conf["order_size"].GetString()};

  if (!pktrade::GlobalVar::live_) {
    gen_.seed(12345);
  }

  if (ordex_conf.HasMember("tgt_order_notional") && ordex_conf.HasMember("tgt_maxpos_notional")) {
    set_size_from_notional_ = true;
    tgt_order_notional_ = ordex_conf["tgt_order_notional"].GetDouble();
    tgt_maxpos_notional_ = ordex_conf["tgt_maxpos_notional"].GetDouble();

    // Basic validation
    if (tgt_order_notional_ <= 0 || tgt_maxpos_notional_ <= 0) {
      throw std::runtime_error(
          fmt::format("({}) Invalid tgt_order_notional ({}) or tgt_maxpos_notional ({}) given",
                      symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_));
    }

    if (tgt_order_notional_ > 1e6 || tgt_maxpos_notional_ > 1e7) {
      throw std::runtime_error(fmt::format(
          "({}) Invalid (TOOBIG) tgt_order_notional ({}) or tgt_maxpos_notional ({}) given",
          symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_));
    }

    if (tgt_order_notional_ > tgt_maxpos_notional_) {
      throw std::runtime_error(
          fmt::format("({}) Invalid tgt_order_notional ({}) should be < tgt_maxpos_notional ({})",
                      symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_));
    }

    // For now, if these are given, then order_size and maxpos are ignored.
    LOG(INFO) << fmt::format("({}) Using tgt_order_notional ({}) and tgt_maxpos_notional ({})",
                             symbol_.get(), tgt_order_notional_, tgt_maxpos_notional_);
  }

  // Catch misconfigurations.
  if (ordex_conf.HasMember("tgt_order_notional") != ordex_conf.HasMember("tgt_maxpos_notional")) {
    throw std::runtime_error(
        "Both tgt_order_notional and tgt_maxpos_notional must be specified together");
  }

  // Bounds of order_size.
  if (ordex_conf.HasMember("order_random_upper_limit")) {
    order_random_upper_limit_ = ordex_conf["order_random_upper_limit"].GetDouble();
    order_random_lower_limit_ = ordex_conf["order_random_lower_limit"].GetDouble();
    if (order_random_upper_limit_ <= order_random_lower_limit_) {
      throw std::runtime_error(
          fmt::format("order_random_upper_limit ({}) must be > order_random_lower_limit ({})",
                      order_random_upper_limit_, order_random_lower_limit_));
    }
    if (order_random_lower_limit_ <= 0) {
      throw std::runtime_error(
          fmt::format("order_random_lower_limit ({}) must be > 0", order_random_lower_limit_));
    }
  }
  if (ordex_conf.HasMember("front_order_random_upper_limit")) {
    front_order_random_upper_limit_ = ordex_conf["front_order_random_upper_limit"].GetDouble();
    front_order_random_lower_limit_ = ordex_conf["front_order_random_lower_limit"].GetDouble();
    if (front_order_random_upper_limit_ <= front_order_random_lower_limit_) {
      throw std::runtime_error(fmt::format(
          "front_order_random_upper_limit ({}) must be > front_order_random_lower_limit ({})",
          front_order_random_upper_limit_, front_order_random_lower_limit_));
    }
    if (front_order_random_lower_limit_ <= 0) {
      throw std::runtime_error(fmt::format("front_order_random_lower_limit ({}) must be > 0",
                                           front_order_random_lower_limit_));
    }
  }

  dis_ =
      std::uniform_real_distribution<double>(order_random_lower_limit_, order_random_upper_limit_);
  dis_front_ = std::uniform_real_distribution<double>(front_order_random_lower_limit_,
                                                      front_order_random_upper_limit_);

  LOG(INFO) << fmt::format(
      "({}) Running with randomized frontlevels in [{}, {}] backlevels in [{}, {}]", symbol_.get(),
      front_order_random_lower_limit_, front_order_random_upper_limit_, order_random_lower_limit_,
      order_random_upper_limit_);

  can_cross_ = ordex_conf["can_cross"].GetBool();
  cross_thresh_ = ordex_conf["cross_thresh"].GetDouble();
  cur_cross_thresh_ = cross_thresh_;
  if (ordex_conf.HasMember("cross_price_mode")) {
    cross_price_mode_ = ordex_conf["cross_price_mode"].GetInt();
  }
  if (ordex_conf.HasMember("exit_adjust")) {
    exit_adjust_ = ordex_conf["exit_adjust"].GetDouble();
  }
  if (ordex_conf.HasMember("cross_sz_mult")) {
    cross_sz_mult_ = ordex_conf["cross_sz_mult"].GetDouble();
  }
  ms_between_cross_ = ordex_conf["ms_between_cross"].GetInt64();
  cross_limit_maxpos_frac_ = ordex_conf["cross_limit_maxpos_frac"].GetDouble();

  // Initialize thresholds.
  base_place_thresh_conf_ = ordex_conf["place_thresh"].GetDouble();
  base_cancel_buffer_conf_ = ordex_conf["cancel_buffer"].GetDouble();
  // For now, disallow negative threshes.
  if (base_place_thresh_conf_ <= 0) {
    throw std::runtime_error(
        fmt::format("({}) place_thresh ({}) must be > 0", symbol_.get(), base_place_thresh_conf_));
  }
  if (base_cancel_buffer_conf_ <= 0 || base_cancel_buffer_conf_ > 1) {
    // <= 0 would cause jitter, > 1 would make slow cancels less cancelly than fast.
    throw std::runtime_error(fmt::format("({}) cancel_buffer ({}) must be > 0 and <= 1",
                                         symbol_.get(), base_cancel_buffer_conf_));
  }
  if (ordex_conf.HasMember("place_thresh_mode")) {
    place_thresh_mode_ = ordex_conf["place_thresh_mode"].GetInt();
  }
  if (place_thresh_mode_ < 0 || place_thresh_mode_ > 5) {
    throw std::runtime_error("Invalid place_thresh_mode");
  }
  if (ordex_conf.HasMember("place_thresh_spread_coef")) {
    place_thresh_spread_coef_ = ordex_conf["place_thresh_spread_coef"].GetDouble();
  }
  if (place_thresh_spread_coef_ < 0 || place_thresh_spread_coef_ > 10) {
    throw std::runtime_error("Invalid place_thresh_spread_coef");
  }
  if (place_thresh_mode_ >= 3) {
    parsePlaceThreshLiqConfig(ordex_conf);
  }
  // This only affects place_thresh_mode 2. AlphaRelWideMM already carries
  // lspread_ema_; RelWideMM2 needs its own local-spread EMA for the same idea.
  if (ordex_conf.HasMember("place_thresh_spread_ema_tdc_s")) {
    place_thresh_spread_ema_tdc_ms_ =
        ordex_conf["place_thresh_spread_ema_tdc_s"].GetDouble() * 1000;
  }
  if (place_thresh_spread_ema_tdc_ms_ <= 0) {
    throw std::runtime_error("Invalid place_thresh_spread_ema_tdc_s");
  }

  // If we want to multiply thresholds when coming back from minFV
  if (ordex_conf.HasMember("thresh_mult_minfv")) {
    thresh_mult_minfv_ = ordex_conf["thresh_mult_minfv"].GetDouble();
    if (thresh_mult_minfv_ < 1) {
      throw std::runtime_error(fmt::format("({}) thresh_mult_minfv ({}) must be >= 1",
                                           symbol_.get(), thresh_mult_minfv_));
    }
    LOG(INFO) << fmt::format("({}) Using thresh_mult_minfv {}", symbol_.get(), thresh_mult_minfv_);
  }

  if (ordex_conf.HasMember("base_cancel_thresh")) {
    base_cancel_thresh_conf_ = ordex_conf["base_cancel_thresh"].GetDouble();
    if (base_cancel_thresh_conf_ >= base_place_thresh_conf_ || base_cancel_thresh_conf_ < 0) {
      // >= place_thresh would cause jitter, < 0 would make slow cancels less cancelly than fast.
      // Note: if place_thresh gets reduced (e.g., from curvicity), this can still be violated,
      // but we don't really use base_cancel_thresh anymore so not gonna deal with it.
      throw std::runtime_error(
          fmt::format("({}) base_cancel_thresh ({}) must be < place_thresh ({}) and >= 0",
                      symbol_.get(), base_cancel_thresh_conf_, base_place_thresh_conf_));
    }
    LOG(INFO) << fmt::format("({}) Running with base_cancel_thresh_conf set to {}", symbol_.get(),
                             base_cancel_thresh_conf_);
  } else {
    LOG(INFO) << fmt::format("({}) Running with cancel_buffer set to {}", symbol_.get(),
                             base_cancel_buffer_conf_);
  }

  base_buy_thresh_ = base_place_thresh_conf_;
  base_sell_thresh_ = base_place_thresh_conf_;

  // Optionally set additional thresholds. Especially for the time
  // where we're doing things
  if (ordex_conf.HasMember("extra_buy_thresh")) {
    extra_buy_thresh_ = ordex_conf["extra_buy_thresh"].GetDouble();
  }
  if (ordex_conf.HasMember("extra_sell_thresh")) {
    extra_sell_thresh_ = ordex_conf["extra_sell_thresh"].GetDouble();
  }

  if (ordex_conf.HasMember("limit_threshes_positive")) {
    if (!ordex_conf["limit_threshes_positive"].GetBool()) {
      LOG(WARNING) << fmt::format("({}) Overriding limit_threshes_positive to true", symbol_.get());
    }
  }

  LOG(INFO) << fmt::format("({}) Running with extra_threshes (bs {} {})", symbol_.get(),
                           extra_buy_thresh_, extra_sell_thresh_);

  recalcThreshes();

  // Thresh to use in TL2 aggressive exit mode.
  TL2_aggr_exit_thresh_ = base_place_thresh_conf_ * 0.5;
  if (ordex_conf.HasMember("TL2_aggr_exit_thresh")) {
    TL2_aggr_exit_thresh_ = ordex_conf["TL2_aggr_exit_thresh"].GetDouble();
  }

  // Note that gtc orders are slower than alo, but can remove liquidity.
  // I don't expect speed to be a huge issue when we start though.
  mm_gtc_ = true;
  tif_ = TimeInForce::GTC;

  if (ordex_conf.HasMember("mm_gtc")) {
    mm_gtc_ = ordex_conf["mm_gtc"].GetBool();
    if (!mm_gtc_) {
      tif_ = TimeInForce::ALO;
    }
  }

  // If we have a sided position, we widen our market on the side that increases
  // exposure (but don't bring in on the other)
  ladder_one_sided_ = ordex_conf["ladder_one_sided"].GetBool();

  // Every ordersize of exposure, we widen by this * the place_thresh.
  per_order_widen_frac_ = ordex_conf["per_order_widen_frac"].GetDouble();

  // Optionally allow narrowing the flatten side when premium_ema makes it hard to
  // tradeout.
  if (ordex_conf.HasMember("order_decrease_coef")) {
    order_decrease_coef_ = ordex_conf["order_decrease_coef"].GetDouble();
    if (order_decrease_coef_ < 0 || order_decrease_coef_ > 1) {
      throw std::runtime_error("order_decrease_coef must be between 0 and 1");
    }
    LOG(INFO) << fmt::format("({}) Order decrease coef enabled: {}", symbol_.get(),
                             order_decrease_coef_);
  }

  // TODO: take this out if we just add this to all live conffiles.
  if (ordex_conf.HasMember("curv_impulse_tdc")) {
    curv_impulse_tdc_ms_ = ordex_conf["curv_impulse_tdc"].GetDouble() * 1000;
    curv_impulse_coef_ = ordex_conf["curv_impulse_coef"].GetDouble();
  }

  if (ordex_conf.HasMember("pred_momentum_tdc") && ordex_conf.HasMember("pred_momentum_coef")) {
    pred_momentum_tdc_ms_ = ordex_conf["pred_momentum_tdc"].GetDouble() * 1000;

    pred_momentum_coef_ = ordex_conf["pred_momentum_coef"].GetDouble();
    LOG(INFO) << fmt::format("({}) Pred momentum tracking enabled: tdc {} ms coef {}",
                             symbol_.get(), pred_momentum_tdc_ms_, pred_momentum_coef_);
  }

  // Vol/liq mechanisms (1-4)
  vol_mechs_.parseConfig(symbol_.get(), ordex_conf);

  max_back_levels_ = ordex_conf["max_back_levels"].GetInt();
  if (ordex_conf.HasMember("place_back_levels")) {
    place_back_levels_ = ordex_conf["place_back_levels"].GetBool();
  }

  rung_spacing_mult_ = ordex_conf["rung_spacing_mult"].GetDouble();
  rung_spacing_px_ = 0;
  if (ordex_conf.HasMember("fixed_rung_spacing_thresh")) {
    fixed_rung_spacing_thresh_ = ordex_conf["fixed_rung_spacing_thresh"].GetDouble();
  }

  per_backlevel_rung_spacing_mult_ = 1;
  if (ordex_conf.HasMember("per_backlevel_rung_spacing_mult")) {
    per_backlevel_rung_spacing_mult_ = ordex_conf["per_backlevel_rung_spacing_mult"].GetDouble();
  }
  if (per_backlevel_rung_spacing_mult_ < 1) {
    throw std::runtime_error("per_backlevel_rung_spacing_mult must be >= 1");
  }

  if (ordex_conf.HasMember("min_foreign_inside_mult")) {
    min_foreign_inside_mult_ = ordex_conf["min_foreign_inside_mult"].GetDouble();
  }
  if (ordex_conf.HasMember("local_size_cap_mult")) {
    local_size_cap_mult_ = ordex_conf["local_size_cap_mult"].GetDouble();
  }
  if (ordex_conf.HasMember("local_size_cap_tdc_ms")) {
    local_size_cap_tdc_ms_ = ordex_conf["local_size_cap_tdc_ms"].GetDouble();
  }
  if (ordex_conf.HasMember("front_rung_spacing_mult")) {
    front_rung_spacing_mult_ = ordex_conf["front_rung_spacing_mult"].GetDouble();
    if (front_rung_spacing_mult_ > 2 || front_rung_spacing_mult_ <= 0) {
      throw std::runtime_error("Gave improper front_rung_spacing_mult");
    }
  }

  if (ordex_conf.HasMember("restrict_near_book")) {
    restrict_near_book_ = ordex_conf["restrict_near_book"].GetBool();
  }

  if (ordex_conf.HasMember("enable_queue_jump")) {
    enable_queue_jump_ = ordex_conf["enable_queue_jump"].GetBool();
    if (enable_queue_jump_) {
      if (ordex_conf.HasMember("queue_jump_size_mult")) {
        queue_jump_size_mult_ = ordex_conf["queue_jump_size_mult"].GetDouble();
        if (queue_jump_size_mult_ <= 0) {
          throw std::runtime_error("queue_jump_size_mult must be > 0");
        }
      }
      LOG(INFO) << fmt::format("({}) Queue jump enabled: size_mult {}", symbol_.get(),
                               queue_jump_size_mult_);
    }
  }

  will_cancel_isolated_ = false;
  if (ordex_conf.HasMember("will_cancel_isolated")) {
    will_cancel_isolated_ = ordex_conf["will_cancel_isolated"].GetBool();
  }

  // TODO: let rungs get wider as we go farther from the inside.

  ms_min_ord_lifetime_ = 200;
  if (ordex_conf.HasMember("min_ord_lifetime")) {
    ms_min_ord_lifetime_ = int(ordex_conf["min_ord_lifetime"].GetDouble() * 1000);
  }

  ms_between_place_ = ordex_conf["ms_between_place"].GetInt64();
  ms_between_cxl_ = ordex_conf["ms_between_cxl"].GetInt64();
  if (ordex_conf.HasMember("ms_between_backlevel")) {
    ms_between_backlevel_ = ordex_conf["ms_between_backlevel"].GetInt64();
  }

  last_t_place_ = 0;
  last_t_cxl_ = 0;
  last_t_backlevel_ = 0;

  // For viewing myself on the book. If so, print it every n tradecalls.
  print_ords_ = ordex_conf["print_ords"].GetBool();
  print_ords_every_ = 100;
  if (ordex_conf.HasMember("print_ords_every")) {
    print_ords_every_ = ordex_conf["print_ords_every"].GetInt();
  }

  needs_first_fire_ = true;
  num_fires_ = 0;

  // The traded symbol (eg. unit:ES)
  local_sig_ = sf->getByName(ordex_conf["local_sig"].GetString());
  if (local_sig_ == nullptr) {
    throw std::runtime_error("local_sig_ not successfully created");
  }
  local_sig_idx_ = local_sig_->getID();
  local_sig_->addListener(this);

  if (place_thresh_mode_ >= 3) {
    initPlaceThreshLiqSignal(ordex_conf, sf);
  }

  if (ordex_conf.HasMember("const_pred_px")) {
    // If we're using a constant pred_px, don't look for remote or relative signals
    use_const_pred_px_ = true;
    const_pred_px_ = ordex_conf["const_pred_px"].GetDouble();
    LOG(INFO) << fmt::format("({}) Using const_pred_px {}", symbol_.get(), const_pred_px_);
  } else {
    // The remote mid we price off of (eg. the closest ES on CME, via DataBento)
    remote_sig_ = sf->getByName(ordex_conf["remote_sig"].GetString());
    if (remote_sig_ == nullptr) {
      throw std::runtime_error("remote_sig_ not successfully created");
    }
    remote_sig_idx_ = remote_sig_->getID();
    remote_sig_->addListener(this);
    last_remote_mid_t_ = 0;
  }

  // Define this before the first call to read_snapshots_supabase.
  if (!pktrade::GlobalVar::live_) {
    sim_snapshot_cache_file_ =
        fmt::format("snapshot_{}_{}.json", symbol_.get(), pktrade::GlobalVar::date_);
  }

  // In case we use a relativesignal for pricing, for situations where the
  // remote book goes away. For example, if we're trying to price SPX over the
  // weekend or over thanksgiving.
  if (!use_const_pred_px_ && ordex_conf.HasMember("relative_sig")) {
    relative_sig_ = sf->getByName(ordex_conf["relative_sig"].GetString());

    if (relative_sig_ == nullptr) {
      throw std::runtime_error("relative_sig_ not successfully created");
    }

    relative_sig_idx_ = relative_sig_->getID();
    relative_sig_->addListener(this);

    // Only do this early snapshot reading in live, because we don't need event_loop time, which
    // isn't initialized yet at this point. In sim, just wait until the first onSignalValue, plus
    // 5 (event_loop) seconds, before looking up the snapshot.
    if (pktrade::GlobalVar::live_) {
      // Initializes snapshot values from the database.
      read_snapshots_supabase();
    }

    // In that case, we also need to set the snapshotting logic.
    // TODO: perhaps write snapshots to disk so we can stop/start
    //       queries back in the middle of the day.
    int snapshot_interval_s_ = ordex_conf["snapshot_interval_s"].GetInt();
    remote_snapshot_interval_ = std::chrono::seconds(snapshot_interval_s_);

    // We gotta do this regression ourselves.
    relative_beta_ = ordex_conf["relative_beta"].GetDouble();
    beta_uncertainty_coef_ = ordex_conf["beta_uncertainty_coef"].GetDouble();
    relative_px_after_n_ms_ = 1000 * snapshot_interval_s_;
    // On the chance that we need to encode the snapshot values ourselves.
    // This is if like, we need to restart halfway through a weekend. In that
    // case, we have to hardcode - there's no way of retrieving this otherwise.
    // Also means we need to output these values so future versions of self
    // can look this up.
    if (ordex_conf.HasMember("last_perm_snapshot_rel_px")) {
      // rel - is the tihng we're pricing from, like btc
      last_perm_snapshot_rel_px_ = ordex_conf["last_perm_snapshot_rel_px"].GetDouble();
      // remote is the thing we're tracking, like ES
      last_translated_px_ = last_perm_snapshot_remote_px_ =
          ordex_conf["last_perm_snapshot_remote_px"].GetDouble();
    }

    // Let's just have this up. Not sure if it should, let's think through this
    // logic a little bit.
    in_relative_mode_ = true;
    recalcThreshes();

    LOG(INFO) << fmt::format("({}) Using relative_sig, got values : interval_s {} "
                             "beta {} last_pxs {} {} relative_sig_idx {}",
                             symbol_.get(), snapshot_interval_s_, relative_beta_,
                             last_perm_snapshot_rel_px_, last_perm_snapshot_remote_px_,
                             relative_sig_idx_);
  }

  if (!use_const_pred_px_ && ordex_conf.HasMember("equity_off_hour_vwap")) {
    equity_off_hour_vwap_ = ordex_conf["equity_off_hour_vwap"].GetBool();
    // This means we have to subscribe to the remote symbol's trades to get the vwap.
  }

  // Make the tradecaller.
  // Maybe at some point pull this out, elsewhere.
  tc_tempo_ = tf->getByName(ordex_conf["trade_caller"].GetString());
  tc_tempo_->addListener(this);

  refreshSizeMultBaseFromCurrent();
}

void RelWideMM2::parsePlaceThreshLiqConfig(const rapidjson::Value& ordex_conf) {
  place_thresh_liq_coef_ = place_thresh_spread_coef_;
  if (ordex_conf.HasMember("place_thresh_liq_coef")) {
    place_thresh_liq_coef_ = ordex_conf["place_thresh_liq_coef"].GetDouble();
  }
  if (place_thresh_liq_coef_ < 0 || place_thresh_liq_coef_ > 10) {
    throw std::runtime_error("Invalid place_thresh_liq_coef");
  }

  if (ordex_conf.HasMember("place_thresh_liq_combine_mode")) {
    std::string combine_mode = ordex_conf["place_thresh_liq_combine_mode"].GetString();
    if (combine_mode == "add") {
      place_thresh_liq_combine_mode_ = 0;
    } else if (combine_mode == "max") {
      place_thresh_liq_combine_mode_ = 1;
    } else {
      throw std::runtime_error("Invalid place_thresh_liq_combine_mode");
    }
  }

  if (ordex_conf.HasMember("place_thresh_liq_notional")) {
    place_thresh_liq_notional_ = ordex_conf["place_thresh_liq_notional"].GetDouble();
  }
  if (place_thresh_liq_notional_ <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_notional");
  }

  if (ordex_conf.HasMember("place_thresh_liq_per_level_decay")) {
    place_thresh_liq_per_level_decay_ =
        ordex_conf["place_thresh_liq_per_level_decay"].GetDouble();
  }
  if (place_thresh_liq_per_level_decay_ <= 0 || place_thresh_liq_per_level_decay_ > 1) {
    throw std::runtime_error("Invalid place_thresh_liq_per_level_decay");
  }

  if (ordex_conf.HasMember("place_thresh_liq_use_flagged")) {
    place_thresh_liq_use_flagged_ = ordex_conf["place_thresh_liq_use_flagged"].GetBool();
  }

  if (place_thresh_mode_ != 5) {
    return;
  }

  if (ordex_conf.HasMember("place_thresh_liq_auto_inside_mult")) {
    place_thresh_liq_auto_inside_mult_ =
        ordex_conf["place_thresh_liq_auto_inside_mult"].GetDouble();
  }
  if (place_thresh_liq_auto_inside_mult_ <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_auto_inside_mult");
  }

  if (ordex_conf.HasMember("place_thresh_liq_auto_min_notional")) {
    place_thresh_liq_auto_min_notional_ =
        ordex_conf["place_thresh_liq_auto_min_notional"].GetDouble();
  }
  if (ordex_conf.HasMember("place_thresh_liq_auto_max_notional")) {
    place_thresh_liq_auto_max_notional_ =
        ordex_conf["place_thresh_liq_auto_max_notional"].GetDouble();
  }
  if (place_thresh_liq_auto_min_notional_ < 0 ||
      place_thresh_liq_auto_max_notional_ <= place_thresh_liq_auto_min_notional_) {
    throw std::runtime_error("Invalid place_thresh_liq_auto_notional bounds");
  }

  if (ordex_conf.HasMember("place_thresh_liq_auto_tdc_s")) {
    place_thresh_liq_auto_tdc_ms_ =
        ordex_conf["place_thresh_liq_auto_tdc_s"].GetDouble() * 1000;
  }
  if (place_thresh_liq_auto_tdc_ms_ <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_auto_tdc_s");
  }
}

void RelWideMM2::initPlaceThreshLiqSignal(const rapidjson::Value& ordex_conf,
                                          pktrade::signals::SignalFactory* sf) {
  rapidjson::Document liq_conf;
  liq_conf.SetObject();
  auto& alloc = liq_conf.GetAllocator();

  std::string name = fmt::format("__{}_place_thresh_liq", symbol_.get());
  liq_conf.AddMember("name", rapidjson::Value(name.c_str(), alloc), alloc);
  liq_conf.AddMember("type", "SigLiqBalance", alloc);
  liq_conf.AddMember("symbol", rapidjson::Value(symbol_.get().c_str(), alloc), alloc);

  rapidjson::Value books(rapidjson::kArrayType);
  books.PushBack(rapidjson::Value(ordex_conf["markets"].GetArray()[0].GetString(), alloc), alloc);
  liq_conf.AddMember("books", books, alloc);

  std::string sz_decay_type = "SIZE";
  if (ordex_conf.HasMember("place_thresh_liq_sz_decay_type")) {
    sz_decay_type = ordex_conf["place_thresh_liq_sz_decay_type"].GetString();
  }
  if (sz_decay_type != "EXP_NOTIONAL" && sz_decay_type != "LOG_NOTIONAL" &&
      sz_decay_type != "SIZE" && sz_decay_type != "EXP_SIZE" &&
      sz_decay_type != "EXP_NOTIONAL_PERORD" && sz_decay_type != "EXP_SIZE_PERORD" &&
      sz_decay_type != "EXP_NOTIONAL_AND_SIZE") {
    throw std::runtime_error("Invalid place_thresh_liq_sz_decay_type");
  }

  double sz_decay = place_thresh_liq_sz_decay_;
  if (ordex_conf.HasMember("place_thresh_liq_sz_decay")) {
    sz_decay = ordex_conf["place_thresh_liq_sz_decay"].GetDouble();
  }
  if (sz_decay <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_sz_decay");
  }

  double sz_decay_2 = place_thresh_liq_sz_decay_2_;
  if (ordex_conf.HasMember("place_thresh_liq_sz_decay_2")) {
    sz_decay_2 = ordex_conf["place_thresh_liq_sz_decay_2"].GetDouble();
  }
  if (sz_decay_2 <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_sz_decay_2");
  }

  double min_sz_wt = place_thresh_liq_min_sz_wt_;
  if (ordex_conf.HasMember("place_thresh_liq_min_sz_wt")) {
    min_sz_wt = ordex_conf["place_thresh_liq_min_sz_wt"].GetDouble();
  }
  if (min_sz_wt <= 0) {
    throw std::runtime_error("Invalid place_thresh_liq_min_sz_wt");
  }

  liq_conf.AddMember("liq_type", "FIXED_LEVELS", alloc);
  liq_conf.AddMember("calc_style", "AVG_LIQ_PX", alloc);
  liq_conf.AddMember("return_price", true, alloc);
  // SigLiqBalance calls this levels_deep, but it is a tick window from the touch.
  constexpr int kPlaceThreshLiqDepthTicks = 200;
  liq_conf.AddMember("levels_deep", kPlaceThreshLiqDepthTicks, alloc);
  liq_conf.AddMember("sz_decay", sz_decay, alloc);
  liq_conf.AddMember("sz_decay_2", sz_decay_2, alloc);
  liq_conf.AddMember("min_sz_wt", min_sz_wt, alloc);
  liq_conf.AddMember("sz_decay_type", rapidjson::Value(sz_decay_type.c_str(), alloc), alloc);
  liq_conf.AddMember("per_level_decay", place_thresh_liq_per_level_decay_, alloc);
  liq_conf.AddMember("notional_to_liquidate", place_thresh_liq_notional_, alloc);
  liq_conf.AddMember("ref_signal", rapidjson::Value(local_sig_->getName().c_str(), alloc), alloc);
  liq_conf.AddMember("use_flagged", place_thresh_liq_use_flagged_, alloc);
  liq_conf.AddMember("sigformer_tag", "internal_place_thresh_liq", alloc);

  rapidjson::Document empty_signals;
  empty_signals.SetArray();
  // TODO: signal_id -1 is fine while this is the only ad-hoc internal signal, but it
  // collides if we ever stand up more of them. Make this more robust by allocating ids
  // through the SignalFactory (or a dedicated negative-id range) so internal signals
  // like this one don't clash with each other or with factory-built signals.
  place_thresh_liq_sig_ =
      std::make_unique<pktrade::signals::SigLiqBalance>(-1, liq_conf, sf, empty_signals);

  LOG(INFO) << fmt::format(
      "({}) Using internal place-thresh liquidity source: mode {} notional {} coef {} "
      "combine {} auto_inside_mult {}",
      symbol_.get(), place_thresh_mode_, place_thresh_liq_notional_, place_thresh_liq_coef_,
      place_thresh_liq_combine_mode_ == 0 ? "add" : "max", place_thresh_liq_auto_inside_mult_);
}

double RelWideMM2::calcPlaceThreshAutoLiqNotional() {
  const LevelBook* lvl_book = pktrade::GlobalVar::book_manager_->find(traded_bid_);
  if (lvl_book == nullptr) {
    return 0;
  }

  double sample_notional = 0;
  int n_sides = 0;
  const auto& buy_side = lvl_book->buySide();
  if (!buy_side.empty()) {
    double px = buy_side.begin()->px.toDouble();
    double qty = pktrade::effectiveQty(*buy_side.begin()).toDouble();
    double own_qty = protection::ownQtyAt(buy_orders_, px, min_tick_);
    double foreign_qty = std::max(0.0, qty - own_qty);
    sample_notional += px * foreign_qty;
    n_sides++;
  }

  const auto& sell_side = lvl_book->sellSide();
  if (!sell_side.empty()) {
    double px = sell_side.begin()->px.toDouble();
    double qty = pktrade::effectiveQty(*sell_side.begin()).toDouble();
    double own_qty = protection::ownQtyAt(sell_orders_, px, min_tick_);
    double foreign_qty = std::max(0.0, qty - own_qty);
    sample_notional += px * foreign_qty;
    n_sides++;
  }

  if (n_sides != 2) {
    return 0;
  }
  sample_notional /= n_sides;
  if (sample_notional <= 0 || !std::isfinite(sample_notional)) {
    return 0;
  }

  int64_t now_ms = now_fire_t_ > 0 ? now_fire_t_ : time_utils::nowToMs();
  if (place_thresh_liq_auto_last_t_ == 0 || place_thresh_liq_auto_notional_ems_ <= 0) {
    place_thresh_liq_auto_notional_ems_ = sample_notional;
  } else {
    int64_t dt = std::max<int64_t>(0, now_ms - place_thresh_liq_auto_last_t_);
    double alpha = 1.0 - std::exp(-dt / place_thresh_liq_auto_tdc_ms_);
    place_thresh_liq_auto_notional_ems_ =
        alpha * sample_notional + (1.0 - alpha) * place_thresh_liq_auto_notional_ems_;
  }
  place_thresh_liq_auto_last_t_ = now_ms;

  double notional = place_thresh_liq_auto_notional_ems_ * place_thresh_liq_auto_inside_mult_;
  notional = std::max(place_thresh_liq_auto_min_notional_, notional);
  notional = std::min(place_thresh_liq_auto_max_notional_, notional);
  if (notional <= 0 || !std::isfinite(notional)) {
    return 0;
  }
  return notional;
}

void RelWideMM2::postSecMaster() {
  min_tick_ = pktrade::GlobalVar::secmaster_->get_tick_size(traded_bid_).toDouble();

  Quantity new_order_size_ =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, order_size_.toDouble(), true);
  order_size_ = new_order_size_;

  Quantity new_max_pos_ =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, max_pos_.toDouble(), true);
  max_pos_ = new_max_pos_;

  LOG(INFO) << fmt::format("({}) PostSecmaster: min_tick {} new_ord_size {} max_pos {}",
                           symbol_.get(), min_tick_, order_size_.toDouble(), max_pos_.toDouble());
}

void RelWideMM2::subscribeData(pktrade::md::MDBeacon* beacon) {
  if (place_thresh_liq_sig_ != nullptr) {
    place_thresh_liq_sig_->subscribeData(beacon);
  }

  // **********************************************
  // For POLYGON
  // Potentially subscribe to md_beacon if we use off_hour_vwap.
  // Can't do it earlier because md_beacon isn't initilaized yet.
  if (equity_off_hour_vwap_) {
    std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Trd};
    for (auto& book_id : remote_sig_->getBookIds()) {
      // Does this exist?
      beacon->addListener(book_id, this, cb_types);
    }
  }

  // Subscribe to trades for liq_depth mechanism.
  if (vol_mechs_.hasLiqDepth()) {
    std::vector<pktrade::md::BeaconCBType> cb_types = {pktrade::md::BeaconCBType::Trd};
    for (auto& book_id : local_sig_->getBookIds()) {
      beacon->addListener(book_id, this, cb_types);
    }
  }

  // Subscribe to local level updates for inside-size based protections.
  if (local_size_cap_mult_ > 0 || min_foreign_inside_mult_ > 0) {
    std::vector<pktrade::md::BeaconCBType> cb_types = {
        pktrade::md::BeaconCBType::LvlAdd,
        pktrade::md::BeaconCBType::LvlMod,
        pktrade::md::BeaconCBType::LvlDel,
    };
    for (auto& book_id : local_sig_->getBookIds()) {
      beacon->addListener(book_id, this, cb_types);
    }
  }
}

signals::Signal* RelWideMM2::getPredSignal() { return remote_sig_; }

signals::Signal* RelWideMM2::getMidSignal() { return local_sig_; }

// I don't want this to accidentally cross out of position.
// So let's not have a real tl5 right now.
void RelWideMM2::aggFlat() {}

// Note that this tempo will need to be on the remote, not the local.
void RelWideMM2::onTempo(int tempo_id) { tryFire(); }

void RelWideMM2::tryFire() {
  num_fires_++;

  // Must be allowed to trade.
  if (!riskman_->canTrade()) {
    return;
  }

  // This is kind of an edge case, but if the book is simply
  // inactive then we may never get the callback to set these values.
  // This is most extreme in testnet unit:ES. Not expected for a
  // healthy book, but we'll be entering lots of situations
  // with thin liquidity.
  if (local_sig_val_ == 0) {
    local_sig_val_ = local_sig_->getValue();
  }
  if (remote_sig_ != nullptr && remote_sig_val_ == 0) {
    remote_sig_val_ = remote_sig_->getValue();
  }
  if (relative_sig_ != nullptr && relative_sig_val_ == 0) {
    relative_sig_val_ = relative_sig_->getValue();
  }

  now_fire_t_ = time_utils::nowToMs();

  const LevelBook* trade_bk = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  // Update predicted price and thresholds.
  pred_px_ = translatePrice();

  premium_adjusted_pred_px_ = pred_px_;

  // std::cout << "Inside onFire, pred_px is now " << pred_px_ << std::endl;

  if (now_fire_t_ - last_premium_adjust_t_ > 500) {
    updatePremiumEma();
  }

  // Update pred price momentum EMS based on price moves.
  updatePredMomentumEms(pred_px_);

  // premium adjust this.
  // If in TL2, override and use a premium_ema_coef of 0.9.

  if (TL2_aggr_exit_) {
    // If in TL2, override and use a premium_ema_coef of 0.9.
    if (cur_premium_ > 0 && premium_ema_ > 0) {
      premium_adjusted_pred_px_ =
          pred_px_ + TL2_aggr_exit_premium_ema_coef_ * std::min(cur_premium_, premium_ema_);
    } else if (cur_premium_ < 0 && premium_ema_ < 0) {
      premium_adjusted_pred_px_ =
          pred_px_ + TL2_aggr_exit_premium_ema_coef_ * std::max(cur_premium_, premium_ema_);
    }

  } else if (TL3_aggr_exit_) {
    // If in TL3, override and use a premium_ema_coef of 1.
    premium_adjusted_pred_px_ = local_sig_val_;
  } else {
    // TL0 or TL1 mode

    if (premium_adjust_mode_ == 0) {
      if (premium_ema_coef_ > 0) {
        if (cur_premium_ > 0 && premium_ema_ > 0) {
          premium_adjusted_pred_px_ =
              pred_px_ + premium_ema_coef_ * std::min(cur_premium_, premium_ema_);
        } else if (cur_premium_ < 0 && premium_ema_ < 0) {
          premium_adjusted_pred_px_ =
              pred_px_ + premium_ema_coef_ * std::max(cur_premium_, premium_ema_);
        }
        LOG_EVERY_N(INFO, 211) << fmt::format(
            "({}) pred_px {} premium {} premium_ema {} final_adjusted_px {}", symbol_.get(),
            pred_px_, cur_premium_, premium_ema_, premium_adjusted_pred_px_);
      }

    } else if (premium_adjust_mode_ == 1) {
      premium_adjusted_pred_px_ =
          local_sig_val_ -
          premium_mode1_adjust_coef_ * std::copysign(1.0, cur_premium_) *
              std::min(std::abs(cur_premium_), std::abs(base_buy_thresh_ * local_sig_val_ *
                                                        premium_adjust_max_thresh_mult_));

      /*
LOG(INFO) << fmt::format("premium_adjust_mode 1 local_sig {} inside_term {} outside_min {} product
final_px {}", local_sig_val_,std::max(cur_buy_thresh_px_, cur_sell_thresh_px_),
std::min(cur_premium_, std::max(cur_buy_thresh_px_, cur_sell_thresh_px_) *
             premium_adjust_max_thresh_mult_),
             premium_adjusted_pred_px_
);
*/

    } else if (premium_adjust_mode_ == 2) {
      // In this mode, start at pred_px. At 0 position, we adjust 0 and use oracle price.
      // At maxpos, we adjust by the full cur_premium and use local_sig_val.
      double pos = riskman_->getPos().toDouble();
      if (cur_premium_ * pos < 0) { // local price above predpx and we're short, or vice versa
        double coef = std::abs(pos) / max_pos_.toDouble();
        if (coef > 1) {
          LOG_EVERY_N(ERROR, 211) << fmt::format("({}) pos {} exceeds max_pos {}, capping coef",
                                                 symbol_.get(), pos, max_pos_.toDouble());
          coef = 1;
        }
        premium_adjusted_pred_px_ = pred_px_ + coef * cur_premium_;
        LOG_EVERY_N(INFO, 211) << fmt::format(
            "({}) pred_px {} premium {} pos {} max_pos {} coef {} final_adjusted_px {}",
            symbol_.get(), pred_px_, cur_premium_, pos, max_pos_.toDouble(), coef,
            premium_adjusted_pred_px_);
      }
    }
  }

  // Make sure pred_px_ is valid. For now, negative prices are bad.
  if ((pred_px_ <= 0 || std::isnan(pred_px_))) {
    LOG_EVERY_N(INFO, 101) << fmt::format("({}) Got a bad predpx {}, not placing", symbol_.get(),
                                          pred_px_);
    return;
  }
  updateThreshes();
  if (needs_first_fire_) {
    onFirstFire(pred_px_);
    return;
  }

  // Recheck min_tick in case price crossed a scale boundary (e.g. 100).
  // calc_tick_size is just a few FPU ops (pow, log10, ceil), negligible cost.
  // Use local_sig_val_ (the actual market mid) so that both buy and sell sides
  // get the correct tick for where the market is trading.
  if (local_sig_val_ > 0 && std::isfinite(local_sig_val_)) {
    double new_min_tick =
        pktrade::GlobalVar::secmaster_->calc_tick_size(local_sig_val_).toDouble();
    if (!pktrade::approx_equal(min_tick_, new_min_tick)) {
      LOG(WARNING) << fmt::format("({}) min_tick changed from {} to {} (local_sig_val {})",
                                  symbol_.get(), min_tick_, new_min_tick, local_sig_val_);
      min_tick_ = new_min_tick;
      rung_spacing_px_ = std::max(rung_spacing_px_, min_tick_);
    }
  }

  // Check if network is down.
  if (pktrade::GlobalVar::trade_carrier_->isNetworkDown()) {
    LOG_EVERY_N(ERROR, 503) << fmt::format("({}) Network is down, not trading", symbol_.get());
    recovering_from_network_down_ = true; // prep recovery for when network is back up
    return;
  } else if (recovering_from_network_down_) { // network back up
    // Cancel our existing orders.
    cancelOutstandingOrds();
    LOG(ERROR) << fmt::format("({}) RECOVERY MODE, CANCELLING ORDERS", symbol_.get());
    // Exit recovery mode so we can start trading again.
    recovering_from_network_down_ = false;
    return;
  }

  if (periodic_print_premium_) {
    LOG_EVERY_N(INFO, 101) << fmt::format("{}: premium {} premium_ema {} vs_thresh {}",
                                          time_utils::nowToStr(), cur_premium_, premium_ema_,
                                          cur_premium_ / mid_buy_thresh_);
  }

  LOG_EVERY_N(INFO, 307) << fmt::format("({}) TryFire, local_sig {} remote_px {} ({} ms_behind) "
                                        "rel_px {} -> pred_px [{} premium_pred_px {}]",
                                        symbol_.get(), local_sig_val_, remote_sig_val_,
                                        remote_sig_ == nullptr ? 0 : now_fire_t_ - remote_sig_->getLastTransactTime(),
                                        relative_sig_val_, pred_px_, premium_adjusted_pred_px_);

  if (pred_momentum_coef_ > 0) {
    LOG_EVERY_N(INFO, 307) << fmt::format("({}) PredMomentum: ems {:.6f} ({:.2f} bps)",
                                          symbol_.get(), pred_momentum_ems_,
                                          pred_momentum_ems_ * 10000);
  }
  vol_mechs_.logState();
  // Risk-related things.
  // Absolute allotment. Based on the om.maxposition.
  double abs_allowed_buy = riskman_->getSideAlloc(Side::Buy).toDouble();
  double abs_allowed_sell = riskman_->getSideAlloc(Side::Sell).toDouble();

  // I should also have relative allotment too. Based on the ordex maxpos.
  double ordex_allowed_buy =
      std::min(max_pos_.toDouble(),
               std::max((max_pos_ - riskman_->getMaxFillPosition(Side::Buy)).toDouble(), 0.0));
  double ordex_allowed_sell =
      std::min(max_pos_.toDouble(),
               std::max((riskman_->getMaxFillPosition(Side::Sell) + max_pos_).toDouble(), 0.0));

  // Limit ourselves by the absolute maxposition.
  ordex_allowed_buy = std::min(ordex_allowed_buy, abs_allowed_buy);
  ordex_allowed_sell = std::min(ordex_allowed_sell, abs_allowed_sell);
  int64_t now_t = time_utils::nowToMs();

  if (print_ords_ && num_fires_ % print_ords_every_ == 0) {
    // Prints the book every so often.
    printOutstandingOrders();
  }

  // If in TL7 no-new-orders mode, maybeCancel is all we can do.
  if (!TL7_no_new_ords_) {
    if (canCross()) {
      maybeCross(trade_bk, ordex_allowed_buy, ordex_allowed_sell);
    }

    if (TL6_cross_exit_) {
      // If in cross exit mode, don't place, and we've already canceled all outstanding orders.
      return;
    }

    if (now_fire_t_ - last_t_place_ > ms_between_place_ && riskman_->canPlaceMoreOrders()) {
      maybePlaceFront(trade_bk, ordex_allowed_buy, ordex_allowed_sell);
    }
  }

  // Cancel should be the highest priority, but moving it up introduced diffs so reverting for now.
  // Do all throttling for cancels inside maybeCancel and canCancelOrd.
  maybeCancel(trade_bk, ordex_allowed_buy, ordex_allowed_sell);

  if (TL7_no_new_ords_) {
    return;
  }

  // Maybe add or rm backlevels too.
  // Always reserve space for the non-backlevels.
  if (now_fire_t_ - last_t_backlevel_ > ms_between_backlevel_
      /* &&
      now_t - last_t_place_ > ms_between_place_ */
  ) {
    manageBacklevels(trade_bk, ordex_allowed_buy - order_size_.toDouble(),
                     ordex_allowed_sell - order_size_.toDouble());
  }

  if (print_ords_ && num_fires_ % 50 == 0) {
    // Sort our orders every so often, but only if needed.
    if (!std::is_sorted(buy_orders_.begin(), buy_orders_.end(), RelWideMM2::compareBuyOrds)) {
      LOG(WARNING) << fmt::format("({}) buy_orders is not sorted!", symbol_.get());
      sort(buy_orders_.begin(), buy_orders_.end(), RelWideMM2::compareBuyOrds);
    }
    if (!std::is_sorted(sell_orders_.begin(), sell_orders_.end(), RelWideMM2::compareSellOrds)) {
      LOG(WARNING) << fmt::format("({}) sell_orders is not sorted!", symbol_.get());
      sort(sell_orders_.begin(), sell_orders_.end(), RelWideMM2::compareSellOrds);
    }
  }
}

double RelWideMM2::translatePrice() {

  // Special case for using constant pred_px.
  if (use_const_pred_px_) {
    return const_pred_px_;
  }

  // Special case for these equities.
  if (equity_off_hour_vwap_ && running_vwap_ != 0 && !std::isnan(running_vwap_)) {
    return running_vwap_;
  }

  double final_trans_price = 0;

  // Pricing off the remote signal (eg. using ES for unit:ES)
  if (!in_relative_mode_) {

    // Do one last round of sanity checking.

    bool still_keep_relative_mode = false;
    if (last_perm_snapshot_remote_px_ != 0 &&
        (remote_sig_val_ / last_perm_snapshot_remote_px_ > 2 ||
         remote_sig_val_ / last_perm_snapshot_remote_px_ < 0.5)) {
      // We got a bad quote.
      LOG(WARNING) << fmt::format("({}) TranslatePx: remote is up({}) but way off from the last "
                                  "known snapshot at {}, returning",
                                  symbol_.get(), remote_sig_val_, last_perm_snapshot_remote_px_);
      still_keep_relative_mode = true;
    }

    double est_val = remote_sig_val_;

    // This is just a sanity check print
    if (last_perm_snapshot_remote_px_ != 0) {
      double rel_return = (last_relative_px_ / last_perm_snapshot_rel_px_ - 1);
      est_val = (relative_beta_ * rel_return + 1) * last_perm_snapshot_remote_px_;
      rel_px_thresh_ = std::abs(beta_uncertainty_coef_ * relative_beta_ * rel_return);
      // No recalcThreshes since we haven't updated anything relevant to non-relative mode.
      // Note that updating est_val here is important, because below we might switch back to
      // relative mode and need to use it.

      // Throttled 101 -> 1009: 0.92 GB / 3.0M lines over 10 days (~17% of pktrade log volume).
      LOG_EVERY_N(INFO, 1009) << fmt::format(
          "({}) TranslatePx: remote is up ({}) but would have translated predpx to {} ( "
          "last_rel {} last_rel_snapshot {} last_remote_snapshot {} | "
          "rel_px_thresh {:.6f} mid_buy_thresh {:.6f} mid_sell_thresh {:.6f} )",
          symbol_.get(), remote_sig_val_, est_val, last_relative_px_, last_perm_snapshot_rel_px_,
          last_perm_snapshot_remote_px_, rel_px_thresh_, mid_buy_thresh_, mid_sell_thresh_);
    }
    if (still_keep_relative_mode && relative_sig_) {
      in_relative_mode_ = true;
      recalcThreshes();
      final_trans_price = est_val;
    } else {
      final_trans_price = remote_sig_val_;
    }
  } else { // in relative mode

    // Just checking, if we don't have real values for these snapshots,
    // they're not legit.
    if (last_perm_snapshot_rel_px_ == 0 || last_perm_snapshot_remote_px_ == 0) {
      LOG_EVERY_N(INFO, 101) << fmt::format("({}) TranslatePx: snapshot prices remote {} rel {} "
                                            ": can't translate, quitting",
                                            symbol_.get(), last_perm_snapshot_remote_px_,
                                            last_perm_snapshot_rel_px_);
      final_trans_price = 0;
    } else {
      // Only do this sort of translation if we have nonzero values.

      // Just in case we haven't gotten a price update yet.
      if (last_relative_px_ == 0) {
        // printf("last_relative_px 0, getting the remote %f \n",
        // last_perm_snapshot_remote_px_);
        final_trans_price = last_perm_snapshot_remote_px_;
      }

      // Otherwise. I don't know the best way to estimate, so I guess we can just
      // use the basic beta to estimate.
      double est_val = (relative_beta_ * (last_relative_px_ / last_perm_snapshot_rel_px_ - 1) + 1) *
                       last_perm_snapshot_remote_px_;
      LOG_EVERY_N(INFO, 503) << fmt::format(
          "({}) TRANSLATEPRICE last_rel_px {} last_perm_snapshot_rel_px {} "
          "last_perm_snapshot_remote_px_ {} est {}",
          symbol_.get(), last_relative_px_, last_perm_snapshot_rel_px_,
          last_perm_snapshot_remote_px_, est_val);
      final_trans_price = est_val;
    }

    // Just in case we haven't gotten a price update yet.
    if (last_relative_px_ == 0) {
      // printf("last_relative_px 0, getting the remote %f \n",
      // last_perm_snapshot_remote_px_);
      final_trans_price = last_perm_snapshot_remote_px_;
    }

    // Otherwise. I don't know the best way to estimate, so I guess we can just
    // use the basic beta to estimate.
    double rel_return = (last_relative_px_ / last_perm_snapshot_rel_px_ - 1);
    double est_val = (relative_beta_ * rel_return + 1) * last_perm_snapshot_remote_px_;
    rel_px_thresh_ = std::abs(beta_uncertainty_coef_ * relative_beta_ * rel_return);
    recalcThreshes();
    LOG_EVERY_N(INFO, 503) << fmt::format(
        "({}) TRANSLATEPRICE last_rel_px {} last_perm_snapshot_rel_px {} "
        "last_perm_snapshot_remote_px_ "
        "{} "
        "est {} | rel_px_thresh {:.6f} mid_buy_thresh {:.6f} mid_sell_thresh {:.6f}",
        symbol_.get(), last_relative_px_, last_perm_snapshot_rel_px_, last_perm_snapshot_remote_px_,
        est_val, rel_px_thresh_, mid_buy_thresh_, mid_sell_thresh_);
    final_trans_price = est_val;
  }

  if (final_trans_price <= 0) {
    LOG(WARNING) << fmt::format(
        "({}) Invalid translated price: {} -- using last known good price. {}", symbol_.get(),
        final_trans_price, last_translated_px_);
    // Use last known good price (translated, but not yet discounted)
    final_trans_price = last_translated_px_;
  } else {
    last_translated_px_ = final_trans_price;
  }

  double discounted_final_price = final_trans_price;

  if (do_discounting_) {
    // Discount it with the rfr.
    discounted_final_price = discountPrice(final_trans_price);
  } else {
    // Likely situation, we're pricing off somethingl ike cash equities.
  }

  // If oracle price exists, and we seem to be far away from the oracle, then
  // defer to them and complain. I think 15 bps is probably ok as a berth though.
  if (oracle_px_ != 0 && (discounted_final_price > (1 + oracle_diff_thresh_) * oracle_px_ ||
                          discounted_final_price < (1 - oracle_diff_thresh_) * oracle_px_)) {

    // Check how long it's been.
    if (dislocated_from_oracle_start_t_ == 0) {
      dislocated_from_oracle_start_t_ = now_fire_t_;
    }

    // Only do this if we think our prices are legitimately slow.
    if (((now_fire_t_ - remote_sig_->getLastTransactTime() > 10000) ||
         (in_relative_mode_ && now_fire_t_ - relative_sig_->getLastTransactTime() > 10000)) &&
        now_fire_t_ - dislocated_from_oracle_start_t_ > dislocated_from_oracle_thresh_ms_) {

      // We've been dislocated long enough, snap it to the oracle price.
      LOG_EVERY_N(WARNING, 101) << fmt::format(
          "({}) DISCOUNTED_PRICE {:.4f} too far from oracle (%delta {:.4f} "
          "), snapping to ORACLE {}",
          symbol_.get(), discounted_final_price, (discounted_final_price - oracle_px_) / oracle_px_,
          oracle_px_);
      discounted_final_price = oracle_px_;
      snapped_to_oracle_ = true;
    }
  } else {
    // Reset our dislocated_from_oracle tracker.
    dislocated_from_oracle_start_t_ = 0;
    snapped_to_oracle_ = false;
  }
  // Throttled 101 -> 1009: 0.69 GB / 5.1M lines over 10 days (~13% of pktrade log volume).
  LOG_EVERY_N(INFO, 1009) << fmt::format("({}) TRANSLATEPRICE final_trans_price {:.4f} "
                                         "discounted_final_price {}",
                                         symbol_.get(), final_trans_price, discounted_final_price);

  // Save this as the most recent valid translated (and discounted) price
  return discounted_final_price;
}

// TODO: check that this is right.
double RelWideMM2::discountPrice(double cur_px) {

  // Calc time to expiry.
  int64_t ms_to_exp = expiry_t_ - now_fire_t_;

  // Convert seconds to fractional years
  // This is because our rfr is defined in years.
  double yearsToExpiry = ms_to_exp / (365.0 * 24 * 60 * 60 * 1000);

  // Compute discount factor: e^(r * t)
  // riskfreerate = specified stuff.

  double discountFactor = std::exp(riskfree_rate_ * yearsToExpiry);

  // Return discounted price
  return cur_px / discountFactor;
}

// Call this every time we tradecall.
void RelWideMM2::updateThreshes() {
  if (TL6_cross_exit_) {
    // In TL6, we use 0 cross thresh. No need to set place threshes since we only cross.
    cur_cross_thresh_ = 0;
    return;
  }
  // Otherwise, reset cur_cross_thresh. All other TLs use standard cross_thresh.
  cur_cross_thresh_ = cross_thresh_;

  if (TL2_aggr_exit_) {
    // In TL2, we use TL2_aggr_exit_thresh and skip all thresh adjustments.
    cur_buy_thresh_px_ = cur_sell_thresh_px_ = TL2_aggr_exit_thresh_ * pred_px_;
    cur_buy_cancel_thresh_px_ = cur_sell_cancel_thresh_px_ =
        cur_buy_thresh_px_ * (1 - base_cancel_buffer_conf_);
    return;
  } else if (TL3_aggr_exit_) {
    // In TL3, we use 0 thresh and skip all thresh adjustments.
    // Note: 0 cancel thresh is probably okay bc we're making a 0-wide market around something
    // like the mid, but we're still limited to cutting in 1 tick from best bid/ask, so shouldn't
    // flicker too much.
    cur_buy_thresh_px_ = cur_sell_thresh_px_ = 0;
    cur_buy_cancel_thresh_px_ = cur_sell_cancel_thresh_px_ = 0;
    return;
  }

  // Update the buy- and sell- side threshes.

  // Most basic impl, just look at position and then adjust buy- and sell-
  // positions.

  cur_buy_thresh_px_ = mid_buy_thresh_ * pred_px_;
  cur_sell_thresh_px_ = mid_sell_thresh_ * pred_px_;

  double spread_thresh_px = 0;
  if (place_thresh_mode_ == 1 || place_thresh_mode_ == 2) {
    double best_bid = local_sig_->getBBMeasure();
    double best_ask = local_sig_->getBAMeasure();
    if (best_bid > 0 && best_ask > best_bid && std::isfinite(best_bid) &&
        std::isfinite(best_ask)) {
      double local_spread = best_ask - best_bid;
      if (last_local_spread_update_t_ == 0) {
        local_spread_ema_ = local_spread;
      } else {
        int64_t dt = std::max<int64_t>(0, now_fire_t_ - last_local_spread_update_t_);
        double alpha = 1.0 - std::exp(-dt / place_thresh_spread_ema_tdc_ms_);
        local_spread_ema_ = alpha * local_spread + (1.0 - alpha) * local_spread_ema_;
      }
      last_local_spread_update_t_ = now_fire_t_;

      if (place_thresh_mode_ == 1) {
        spread_thresh_px = local_spread * place_thresh_spread_coef_;
      }
    }
    if (place_thresh_mode_ == 2 && local_spread_ema_ > 0) {
      spread_thresh_px = local_spread_ema_ * place_thresh_spread_coef_;
    }
  }
  cur_buy_thresh_px_ += spread_thresh_px;
  cur_sell_thresh_px_ += spread_thresh_px;

  if (place_thresh_mode_ >= 3 && place_thresh_liq_sig_ != nullptr) {
    bool have_liq_notional = place_thresh_mode_ != 5;
    if (place_thresh_mode_ == 5) {
      double auto_notional = calcPlaceThreshAutoLiqNotional();
      if (auto_notional > 0) {
        place_thresh_liq_sig_->setNotionalToLiquidate(auto_notional);
        place_thresh_liq_sig_->setMinTick();
        place_thresh_liq_sig_->fullRecalc();
        have_liq_notional = true;
      }
    }

    double liq_bid = place_thresh_liq_sig_->getBBMeasure();
    double liq_ask = place_thresh_liq_sig_->getBAMeasure();
    // getValid() is the liq signal's own notion that both sides have positive,
    // finite mass with an uncrossed book, so liq_bid/liq_ask are finite and
    // sensible. It replaces the explicit isfinite() guards; the >0 / ordering
    // checks stay as belt-and-suspenders semantic sanity.
    if (have_liq_notional && place_thresh_liq_sig_->getValid() && liq_bid > 0 &&
        liq_ask > liq_bid) {
      double buy_liq_thresh_px = 0;
      double sell_liq_thresh_px = 0;
      if (place_thresh_mode_ == 3) {
        // 0.5 so each side is a half-spread distance from the liq mid, consistent
        // with the else-branch modes (local_mid - liq_bid / liq_ask - local_mid) and
        // our usual thresh_px convention. A constant factor is absorbable into
        // place_thresh_liq_coef_ when searching, but keep it explicit for clarity.
        buy_liq_thresh_px = sell_liq_thresh_px =
            (liq_ask - liq_bid) * 0.5 * place_thresh_liq_coef_;
      } else if (local_sig_->getValid()) {
        // local_sig_ isn't necessarily a mid signal, so don't reconstruct a center
        // from its BB/BA measures. Trust its own validity and published value.
        buy_liq_thresh_px = std::max(0.0, local_sig_val_ - liq_bid) * place_thresh_liq_coef_;
        sell_liq_thresh_px = std::max(0.0, liq_ask - local_sig_val_) * place_thresh_liq_coef_;
      }

      if (buy_liq_thresh_px > 0 || sell_liq_thresh_px > 0) {
        if (place_thresh_liq_combine_mode_ == 0) {
          cur_buy_thresh_px_ += buy_liq_thresh_px;
          cur_sell_thresh_px_ += sell_liq_thresh_px;
        } else {
          cur_buy_thresh_px_ = std::max(cur_buy_thresh_px_, buy_liq_thresh_px);
          cur_sell_thresh_px_ = std::max(cur_sell_thresh_px_, sell_liq_thresh_px);
        }
      }
    }
  }

  double cur_pos = riskman_->getPos().toDouble();
  double bias_buy =
      std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * cur_buy_thresh_px_;
  double bias_sell =
      std::abs(cur_pos / order_size_.toDouble()) * per_order_widen_frac_ * cur_sell_thresh_px_;

  // I know this can be written with fewer lines, it's just easy for me
  // to read this way.

  // Maybe we should discuss, but I kind of like the premium_ema and order_decrease_coef
  // way of doing things better.
  if (cur_pos > 0) {
    // Bias towards selling.
    cur_buy_thresh_px_ += bias_buy;
    if (!ladder_one_sided_) {
      cur_sell_thresh_px_ -= bias_sell;
    }
  } else {
    cur_sell_thresh_px_ += bias_sell;
    if (!ladder_one_sided_) {
      cur_buy_thresh_px_ -= bias_buy;
    }
  }

  bool did_flatten_decrease = false;
  // Apply position-based narrowing on flatten side when premium_ema indicates
  // market is trading away from our fair in the direction we need to trade.
  if (order_decrease_coef_ > 0) {
    if (cur_pos > 0 && premium_ema_ < 0) {
      // Long and market below fair -> easier to sell out, narrow sell thresh
      cur_sell_thresh_px_ -= order_decrease_coef_ * bias_sell;
      did_flatten_decrease = true;
    } else if (cur_pos < 0 && premium_ema_ > 0) {
      // Short and market above fair -> easier to buy back, narrow buy thresh
      cur_buy_thresh_px_ -= order_decrease_coef_ * bias_buy;
      did_flatten_decrease = true;
    }
  }

  // Only do one-sided widening for the impulse curvicity.
  // Apply these adjustments after position-based logic bc signage could've gotten confused.
  double impulse_curv_offset = curv_impulse_ * curv_impulse_coef_;
  if (curv_impulse_ > 0) {
    // curv_impulse positive means we bought recently, so we make it harder to buy
    cur_buy_thresh_px_ += impulse_curv_offset * cur_buy_thresh_px_;
  } else if (curv_impulse_ < 0) {
    // curv_impulse negative means we sold recently, so we make it harder to sell.
    cur_sell_thresh_px_ += std::abs(impulse_curv_offset) * cur_sell_thresh_px_;
  }

  // One-sided widening based on pred price momentum.
  // If momentum is positive (price rising), widen sell side to avoid getting picked off.
  if (pred_momentum_ems_ > 0) {
    cur_sell_thresh_px_ += pred_momentum_ems_ * pred_momentum_coef_ * pred_px_;
  } else if (pred_momentum_ems_ < 0) {
    cur_buy_thresh_px_ += std::abs(pred_momentum_ems_) * pred_momentum_coef_ * pred_px_;
  }

  // Vol/liq mechanisms (1-4)
  {
    auto [bw, sw] = vol_mechs_.getVolWidenPx(pred_px_, cur_buy_thresh_px_, cur_sell_thresh_px_);
    cur_buy_thresh_px_ += bw;
    cur_sell_thresh_px_ += sw;
    auto [ldb, lds] = vol_mechs_.getLiqDepthWidenPx(pred_px_);
    cur_buy_thresh_px_ += ldb;
    cur_sell_thresh_px_ += lds;
  }

  if (limit_threshes_positive_) {
    // Don't reduce threshes to smaller than 0.5bps
    double min_thresh_px = 0.00005 * pred_px_;
    cur_buy_thresh_px_ = std::max(cur_buy_thresh_px_, min_thresh_px);
    cur_sell_thresh_px_ = std::max(cur_sell_thresh_px_, min_thresh_px);
  } else {
    /*
     Checking how often we can go negative.
    LOG(INFO) << fmt::format("UpdateThreshes {} {}",
      cur_buy_thresh_px_,
      cur_sell_thresh_px_
    );
    */
  }
  // Cancel thresh is a better name.

  cur_buy_cancel_thresh_px_ = cur_buy_thresh_px_ * (1 - base_cancel_buffer_conf_);
  cur_sell_cancel_thresh_px_ = cur_sell_thresh_px_ * (1 - base_cancel_buffer_conf_);

  if (base_cancel_thresh_conf_ != 0) {
    cur_buy_cancel_thresh_px_ = base_cancel_thresh_conf_ * pred_px_;
    cur_sell_cancel_thresh_px_ = base_cancel_thresh_conf_ * pred_px_;
  }

  // Apply minima.
  cur_buy_cancel_thresh_px_ = std::max(cur_buy_cancel_thresh_px_, min_cxl_thresh_ * pred_px_);
  cur_sell_cancel_thresh_px_ = std::max(cur_sell_cancel_thresh_px_, min_cxl_thresh_ * pred_px_);

  // Throttled 101 -> 1009: at 101 this was the single largest log source on the trade boxes
  // (1.1 GB / 5.1M lines over 10 days, ~21% of all pktrade log volume).
  LOG_EVERY_N(INFO, 1009) << fmt::format(
      "({}) updateThresh biases ({:.4f} {:.4f}) flatten_decrease {} curv_impulse {:.6f} "
      "impulse_offset {:.4f} pred_ems {:.6f} pred_ems_offset {:.4f} threshes ({:.4f} {:.4f})",
      symbol_.get(), bias_buy, bias_sell, did_flatten_decrease, curv_impulse_, impulse_curv_offset,
      pred_momentum_ems_, pred_momentum_ems_ * pred_momentum_coef_ * pred_px_, cur_buy_thresh_px_,
      cur_sell_thresh_px_);
}

// We do a variety of checks before crossing, so consolidate them here.
bool RelWideMM2::canCross() {
  // Rate limit crossing in all cases.
  if (now_fire_t_ - last_t_cross_ < ms_between_cross_) {
    return false;
  }

  // TL6 cross exit mode overrides all other restrictions.
  if (TL6_cross_exit_) {
    return true;
  }

  // If not in TL6, check other restrictions normally.

  // Don't cross if config doesn't allow it.
  if (!can_cross_) {
    return false;
  }

  // Don't cross if we're in relative mode or predpx is dislocated from oracle.
  if (in_relative_mode_ || dislocated_from_oracle_start_t_ != 0) {
    return false;
  }

  // Don't cross in the first 30s of the day.
  if (now_fire_t_ - first_fire_t_ < 30'000) {
    return false;
  }

  // Otherwise, we can cross!
  return true;
}

// Let's rate limit crossing too.
// This is a very basic crossing logic.
void RelWideMM2::maybeCross(const LevelBook* lvl_book, double ordex_allowed_buy,
                            double ordex_allowed_sell) {
  double best_bid = local_sig_->getBBMeasure();
  double best_ask = local_sig_->getBAMeasure();
  double best_bid_shs = local_sig_->getBBSize();
  double best_ask_shs = local_sig_->getBASize();

  double cur_pos = riskman_->getPos().toDouble();

  // Base cross size can differ from the passive order_size via mult; the
  // allowed/exit clamps below still apply on top of this.
  double base_cross_sz = order_size_.toDouble() * cross_sz_mult_;

  // predict lower than the bb.
  if (pred_px_ < best_bid && best_bid > 0 && ordex_allowed_sell > 0) {

    // We would sell, but don't sell if we have a sizable negative position.
    if (cur_pos <= -max_pos_.toDouble() * cross_limit_maxpos_frac_) {
      return;
    }

    // Selling reduces a long position -> exit. Make it easier to cross out
    // than in by scaling the threshold by exit_adjust_ (< 1 = easier exit).
    double sell_thresh = cur_cross_thresh_;
    if (cur_pos > 0) {
      sell_thresh *= exit_adjust_;
    }

    // Otherwise it can be nan.
    double edge = (best_bid - pred_px_) / pred_px_;
    if (edge > sell_thresh) {
      // we cross against it.
      double cross_sz = std::min({base_cross_sz, ordex_allowed_sell});
      // Never overshoot flat: when long, selling reduces the position (an exit,
      // gated by the exit-adjusted threshold), so clamp to |cur_pos|. Entering a
      // short only happens from flat/short, where exit_adjust isn't applied and
      // the full enter threshold gates it.
      if (cur_pos > 0) {
        cross_sz = std::min(cross_sz, std::abs(cur_pos));
      }

      double cross_px;
      if (cross_price_mode_ == 1) {
        // Sell down to fair value.
        cross_px = roundToSide(pred_px_, true);
      } else if (cross_price_mode_ == 2) {
        // Sell down to fair value plus threshold (keep some edge).
        cross_px = roundToSide(pred_px_ * (1 + sell_thresh), true);
      } else {
        // Mode 0: cross at best bid.
        cross_px = best_bid;
      }
      Price cur_cross_px = Price(std::to_string(cross_px));

      // Make sure order size meets min notional.
      if (cross_sz * cross_px < 11) { // real min notional is 10, but adding buffer
        // If this is an exit clamped to a tiny |cur_pos|, bumping up to min
        // notional would overshoot flat, so leave the residual to passive.
        if (cur_pos > 0) {
          return;
        }
        LOG(WARNING) << fmt::format(
            "({}) Sell order size below min notional of 10 "
            "(px {} sz {} order_size {} allowed_sell {}). Sending min size instead.",
            symbol_.get(), cross_px, cross_sz, order_size_.toDouble(),
            ordex_allowed_sell);
        cross_sz = 11.0 / cross_px;
      }

      Quantity cur_cross_qty = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, cross_sz, true);
      if (cur_cross_qty > Quantity{0}) {
        // Cross at order_size too.
        NewOrder ord{symbol_,          traded_books_[0], Side::Sell, cur_cross_qty, cur_cross_px,
          OrderType::Limit, TimeInForce::IOC, false,      false};
        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Cross);
          last_t_cross_ = now_fire_t_;
          LOG(INFO) << fmt::format(
              "({}) RelWideMM2 SELL cross: px={} qty={} pred={:.4f} bid={:.4f} "
              "thresh={:.5f} mode={}",
              symbol_.get(), cross_px, cross_sz, pred_px_, best_bid, sell_thresh,
              cross_price_mode_);
        }
      }
    }

  } else if (pred_px_ > best_ask && best_ask > 0 && ordex_allowed_buy > 0) {
    // predict higher than the ba.

    // We would buy, but don't if we have a sizable positive position.
    if (cur_pos >= max_pos_.toDouble() * cross_limit_maxpos_frac_) {
      return;
    }

    // Buying reduces a short position -> exit. Make it easier to cross out
    // than in by scaling the threshold by exit_adjust_ (< 1 = easier exit).
    double buy_thresh = cur_cross_thresh_;
    if (cur_pos < 0) {
      buy_thresh *= exit_adjust_;
    }

    double edge = (pred_px_ - best_ask) / pred_px_;
    if (edge > buy_thresh) {
      // we cross against it.
      double cross_sz = std::min({base_cross_sz, ordex_allowed_buy});
      // Never overshoot flat: when short, buying reduces the position (an exit,
      // gated by the exit-adjusted threshold), so clamp to |cur_pos|. Entering a
      // long only happens from flat/long, where exit_adjust isn't applied and
      // the full enter threshold gates it.
      if (cur_pos < 0) {
        cross_sz = std::min(cross_sz, std::abs(cur_pos));
      }

      double cross_px;
      if (cross_price_mode_ == 1) {
        // Pay up to fair value.
        cross_px = roundToSide(pred_px_, false);
      } else if (cross_price_mode_ == 2) {
        // Pay up to fair value minus threshold (keep some edge).
        cross_px = roundToSide(pred_px_ * (1 - buy_thresh), false);
      } else {
        // Mode 0: cross at best ask.
        cross_px = best_ask;
      }
      Price cur_cross_px = Price(std::to_string(cross_px));

      // Make sure order size meets min notional.
      if (cross_sz * cross_px < 11) { // real min notional is 10, but adding buffer
        // If this is an exit clamped to a tiny |cur_pos|, bumping up to min
        // notional would overshoot flat, so leave the residual to passive.
        if (cur_pos < 0) {
          return;
        }
        LOG(WARNING) << fmt::format(
            "({}) Buy order size below min notional of 10 "
            "(px {} sz {} order_size {} allowed_buy {}). Sending min size instead.",
            symbol_.get(), cross_px, cross_sz, order_size_.toDouble(),
            ordex_allowed_buy);
        cross_sz = 11.0 / cross_px;
      }

      Quantity cur_cross_qty = pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, cross_sz, true);
      if (cur_cross_qty > Quantity{0}) {
        // Cross at order_size too.
        NewOrder ord{symbol_,          traded_books_[0], Side::Buy, cur_cross_qty, cur_cross_px,
          OrderType::Limit, TimeInForce::IOC, false,     false};
        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Cross);
          last_t_cross_ = now_fire_t_;
          LOG(INFO) << fmt::format(
              "({}) RelWideMM2 BUY cross: px={} qty={} pred={:.4f} ask={:.4f} "
              "thresh={:.5f} mode={}",
              symbol_.get(), cross_px, cross_sz, pred_px_, best_ask, buy_thresh,
              cross_price_mode_);
        }
      }
    }
  }
}

void RelWideMM2::maybePlaceFront(const LevelBook* lvl_book, double ordex_allowed_buy,
                                 double ordex_allowed_sell) {

  double best_bid = local_sig_->getBBMeasure();
  double best_ask = local_sig_->getBAMeasure();

  // One side has been traded through, so we have no reliable view of the missing side and
  // any fallback price from lvl_book is untrustworthy. getValid() is the signal's own notion
  // of this state: SigMid::calc() sets validity false exactly when bb==0 || ba==1e10. Don't
  // place this tick.
  if (!local_sig_->getValid()) {
    LOG_EVERY_N(WARNING, 503) << fmt::format("({}) skipping place: signal invalid, bba {} {}",
                                             symbol_.get(), best_bid, best_ask);
    return;
  }

  LOG_EVERY_N(INFO, 503) << fmt::format("({}) bba is {} {}", symbol_.get(), best_bid, best_ask);

  // Buy?
  if (ordex_allowed_buy > 0 && buy_orders_.size() < max_back_levels_) {
    double best_buy_px = premium_adjusted_pred_px_ - cur_buy_thresh_px_;

    // Note: these two checks can go before the queue jump logic because that inherently
    // avoids these cases.
    if (best_ask > 0) {
      best_buy_px = std::min(best_ask - min_tick_, best_buy_px);
    }
    if ((restrict_near_book_ || TL3_aggr_exit_) && best_bid > 0) {
      best_buy_px = std::min(best_bid + min_tick_, best_buy_px);
    }

    // Queue position optimization: iterate from best bid towards our placement price
    // If we find a level that:
    //  - has large size
    //  - 1 tick in front of it has small size
    //  - jumping there would keep us within cancel range, and wouldn't run into the opposite side,
    // then that's a candidate jump. Take the candidate that requires the smallest jump.
    if (enable_queue_jump_ && !lvl_book->side<BuySide>().empty()) {
      auto& buy_side = lvl_book->side<BuySide>();

      double large_size = queue_jump_size_mult_ * order_size_.toDouble();
      double small_size = order_size_.toDouble();
      double prev_level_px = buy_side.begin()->px.toDouble() + min_tick_;
      double prev_level_size = 0;
      double final_jump_px = best_buy_px;
      // just for logging
      double final_jump_size;
      double jump_over_size, jump_over_px;

      for (const auto& level : buy_side) {
        double level_px = level.px.toDouble();

        // Stop once we've passed our target placement price
        if (level_px < best_buy_px) {
          break;
        }

        double level_size = level.qty.toDouble();
        double jump_price = level_px + min_tick_;
        double jump_size = prev_level_size;
        if (prev_level_px >= level_px + 2 * min_tick_) {
          jump_size = 0;
        }

        if (level_size >= large_size && jump_size < small_size) {
          // Found large size, and 1 min_tick ahead is empty or small -- check if we can place there
          // and stay in cancel range and not be blocked by the opposite side
          if ((best_ask == 0 || jump_price <= best_ask - min_tick_) &&
              premium_adjusted_pred_px_ - jump_price >= cur_buy_cancel_thresh_px_) {
            // This is a candidate jump, but there might be better (smaller jumps)
            final_jump_px = jump_price;
            final_jump_size = jump_size;
            jump_over_size = level_size;
            jump_over_px = level_px;
          }
        }

        prev_level_px = level_px;
        prev_level_size = level_size;
      }

      // If we found at least 1 candidate jump, the last one is the smallest jump, so take it
      if (final_jump_px > best_buy_px) {
        LOG_EVERY_N(INFO, 101) << fmt::format(
            "({}) BUY Jumping ahead of large size {:.2f} at {:.4f}, jumping from {:.4f} to {:.4f}, "
            "where size is {:.2f}",
            symbol_.get(), jump_over_size, jump_over_px, best_buy_px, final_jump_px,
            final_jump_size);
        best_buy_px = final_jump_px;
      }
    }

    // Tick rounding needs to happen after all changes to best_buy_px have been made.
    best_buy_px = roundToSide(best_buy_px, false);

    bool too_crowded =
        (buy_orders_.size() > 0 &&
         buy_orders_.front().px.toDouble() + (front_rung_spacing_mult_ * rung_spacing_px_) >
             best_buy_px);

    // Would we immediately want to cancel this order?
    // Slow cancels are always more cancelly than fast, so just check for slow here.
    bool would_cancel = shouldCancelPx(best_buy_px, Side::Buy, false);
    if (would_cancel) {
      // Should never happen with place_thresh lower-bounded at 0.5bps.
      LOG(ERROR) << fmt::format("({}) trying to place buy order @ {} that we would immediately "
                                "cancel; skipping! (pred {} thresh {} cxl_thresh {})",
                                symbol_.get(), best_buy_px, premium_adjusted_pred_px_,
                                cur_buy_thresh_px_, cur_buy_cancel_thresh_px_);
    }

    if (!too_crowded && !would_cancel) {

      double new_order_size = genOrderSize(true);
      new_order_size = std::min(new_order_size, ordex_allowed_buy);
      // Local-liquidity size cap: inside-only, sided (buy uses bid-side EMA).
      double capped_order_size = protection::capBySize(new_order_size, local_size_cap_mult_,
                                                       inside_size_buy_ems_);
      if (capped_order_size * best_buy_px < 11) { // real min notional is 10, but adding buffer
        LOG(WARNING) << fmt::format(
            "({}) Capped buy order size below min notional of 10 "
            "(px {} capped {} orig {} mult {} ems {}). Sending min size instead.",
            symbol_.get(), best_buy_px, capped_order_size, new_order_size, local_size_cap_mult_,
            inside_size_buy_ems_);
        // Technically this can be > ordex_allowed_buy, but it's a tiny size that can only get sent
        // once, since the next time round ordex_allowed_buy will be 0, so won't bother rechecking.
        // Also, make sure to round the size up so we don't violate the min notional.
        new_order_size = 11.0 / best_buy_px;
      } else {
        new_order_size = capped_order_size;
      }
      Quantity new_order_qty =
          pktrade::GlobalVar::secmaster_->round_qty(
              traded_bid_, new_order_size, true);
      if (new_order_qty > Quantity{0}) {

        NewOrder ord{symbol_,
                     traded_books_[0],
                     Side::Buy,
                     new_order_qty,
                     Price{std::to_string(best_buy_px)},
                     OrderType::Limit,
                     tif_,
                     false,
                     false};
        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Front);

          buy_orders_.emplace(buy_orders_.begin(),
                              SimpleOrder{pkord_id, Side::Buy, Price{std::to_string(best_buy_px)},
                                          new_order_qty, SimpleOrderState::LIVE, now_fire_t_, 0});
          last_t_place_ = now_fire_t_;
          // Every so often, remind us of threshes and effectiveprice.
          LOG_EVERY_N(INFO, 11) << fmt::format(
              "({}) Sending buyord {} {} at {:.2f}, predpx {} thresh {}", symbol_.get(), pkord_id,
              new_order_size, best_buy_px, premium_adjusted_pred_px_, cur_buy_thresh_px_);
        }
      }
    }
  }

  // Sell
  if (ordex_allowed_sell > 0 && sell_orders_.size() < max_back_levels_

  ) {
    double best_sell_px = premium_adjusted_pred_px_ + cur_sell_thresh_px_;

    if (best_bid > 0) {
      best_sell_px = std::max(best_bid + min_tick_, best_sell_px);
    }
    if ((restrict_near_book_ || TL3_aggr_exit_) && best_ask > 0) {
      best_sell_px = std::max(best_ask - min_tick_, best_sell_px);
    }

    // Queue position optimization: iterate from best ask towards our placement price
    // If we find a level that:
    //  - has large size
    //  - 1 tick in front of it has small size
    //  - jumping there would keep us within cancel range, and wouldn't run into the opposite side,
    // then that's a candidate jump. Take the candidate that requires the smallest jump.
    if (enable_queue_jump_ && !lvl_book->side<SellSide>().empty()) {
      auto& sell_side = lvl_book->side<SellSide>();

      double large_size = queue_jump_size_mult_ * order_size_.toDouble();
      double small_size = order_size_.toDouble();
      double prev_level_px = sell_side.begin()->px.toDouble() - min_tick_;
      double prev_level_size = 0;
      double final_jump_px = best_sell_px;
      // just for logging
      double final_jump_size;
      double jump_over_size, jump_over_px;

      for (const auto& level : sell_side) {
        double level_px = level.px.toDouble();

        // Stop once we've passed our target placement price
        if (level_px > best_sell_px) {
          break;
        }

        double level_size = level.qty.toDouble();
        double jump_price = level_px - min_tick_;
        double jump_size = prev_level_size;
        if (prev_level_px <= level_px - 2 * min_tick_) {
          jump_size = 0;
        }

        if (level_size >= large_size && jump_size < small_size) {
          // Found large size, and 1 min_tick ahead is empty or small -- check if we can place there
          // and stay in cancel range and not be blocked by the opposite side
          if ((best_bid == 0 || jump_price >= best_bid + min_tick_) &&
              jump_price - premium_adjusted_pred_px_ >= cur_sell_cancel_thresh_px_) {
            // This is a candidate jump, but there might be better (smaller jumps)
            final_jump_px = jump_price;
            final_jump_size = jump_size;
            jump_over_size = level_size;
            jump_over_px = level_px;
          }
        }

        prev_level_px = level_px;
        prev_level_size = level_size;
      }

      // If we found at least 1 candidate jump, the last one is the smallest jump, so take it
      if (final_jump_px < best_sell_px) {
        LOG_EVERY_N(INFO, 101) << fmt::format("({}) SELL Jumping ahead of large size {:.2f} at "
                                              "{:.4f}, jumping from {:.4f} to {:.4f}, "
                                              "where size is {:.2f}",
                                              symbol_.get(), jump_over_size, jump_over_px,
                                              best_sell_px, final_jump_px, final_jump_size);
        best_sell_px = final_jump_px;
      }
    }

    // Tick rounding needs to happen after all changes to best_sell_px have been made.
    best_sell_px = roundToSide(best_sell_px, true);

    bool too_crowded =
        (sell_orders_.size() > 0 &&
         sell_orders_.front().px.toDouble() - (front_rung_spacing_mult_ * rung_spacing_px_) <
             best_sell_px);

    // Would we immediately want to cancel this order?
    // Slow cancels are always more cancelly than fast, so just check for slow here.
    bool would_cancel = shouldCancelPx(best_sell_px, Side::Sell, false);
    if (would_cancel) {
      // Should never happen with place_thresh lower-bounded at 0.5bps.
      LOG(ERROR) << fmt::format("({}) trying to place sell order @ {} that we would immediately "
                                "cancel; skipping! (pred {} thresh {} cxl_thresh {})",
                                symbol_.get(), best_sell_px, premium_adjusted_pred_px_,
                                cur_sell_thresh_px_, cur_sell_cancel_thresh_px_);
    }

    if (!too_crowded && !would_cancel) {

      double new_order_size = genOrderSize(true);
      new_order_size = std::min(new_order_size, ordex_allowed_sell);
      // Local-liquidity size cap: inside-only, sided (sell uses ask-side EMA).
      double capped_order_size = protection::capBySize(new_order_size, local_size_cap_mult_,
                                                       inside_size_sell_ems_);
      if (capped_order_size * best_sell_px < 11) { // real min notional is 10, but adding buffer
        LOG(WARNING) << fmt::format(
            "({}) Capped sell order size below min notional of 10 "
            "(px {} capped {} orig {} mult {} ems {}). Sending min size instead.",
            symbol_.get(), best_sell_px, capped_order_size, new_order_size, local_size_cap_mult_,
            inside_size_sell_ems_);
        // Technically this can be > ordex_allowed_sell, but it's a tiny size that can only get sent
        // once, since the next time round ordex_allowed_sell will be 0, so won't bother rechecking.
        // Also, make sure to round the size up so we don't violate the min notional.
        new_order_size = 11.0 / best_sell_px;
      } else {
        new_order_size = capped_order_size;
      }
      Quantity new_order_qty = pktrade::GlobalVar::secmaster_->round_qty(
          traded_bid_, new_order_size, true);
      if (new_order_qty > Quantity{0}) {
        NewOrder ord{symbol_,
                     traded_books_[0],
                     Side::Sell,
                     new_order_qty,
                     Price{std::to_string(best_sell_px)},
                     OrderType::Limit,
                     tif_,
                     false,
                     false};
        // Send it. Add it to container.
        PKOrderId pkord_id = riskman_->sendOrd(ord);
        if (pkord_id != -1) {
          riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Front);
          sell_orders_.emplace(sell_orders_.begin(),
                               SimpleOrder{pkord_id, Side::Sell,
                                           Price{std::to_string(best_sell_px)}, new_order_qty,
                                           SimpleOrderState::LIVE, now_fire_t_, 0});
          last_t_place_ = now_fire_t_;
          LOG_EVERY_N(INFO, 11) << fmt::format(
              "({}) Sending sellord {} {} at {:.2f}, predpx {} thresh {}", symbol_.get(), pkord_id,
              new_order_size, best_sell_px, premium_adjusted_pred_px_, cur_sell_thresh_px_);
        }
      }
    }
  }
}

// Checks if we should (either fast- or slow-) cancel an order at this price, based on price alone.
bool RelWideMM2::shouldCancelPx(double px, Side side, bool fast) const {
  if (side == Side::Buy) {
    if (fast) {
      return (premium_adjusted_pred_px_ < px);
    } else {
      return (premium_adjusted_pred_px_ - cur_buy_cancel_thresh_px_ < px);
    }
  } else {
    if (fast) {
      return (premium_adjusted_pred_px_ > px);
    } else {
      return (premium_adjusted_pred_px_ + cur_sell_cancel_thresh_px_ > px);
    }
  }
}

void RelWideMM2::maybeCancel(const LevelBook* lvl_book, double ordex_allowed_buy,
                             double ordex_allowed_sell) {

  // Check the book side.
  bool should_cancel = false;

  // Cancels due to predpx.
  // Start from the inside.
  double one_buy_px = 0;
  int n_iters = 0;
  for (auto& one_buy_order : buy_orders_) {
    one_buy_px = one_buy_order.px.toDouble();
    should_cancel = false;

    // This logic is a bit confusing so explaining here. We basically have 2 canCancel checks,
    // regular (stricter) and fast (looser), and 2 threshold checks, real cancel thresh (looser) and
    // 0 cancel thresh (stricter). Looser in both cases means more willing to cancel. We cancel in
    // either of 2 cases: regular canCancel and real cancel thresh wants it (stricter + looser), or
    // fast canCancel and 0 cancel thresh wants it (looser + stricter).
    if (canCancelOrd(one_buy_order)) {
      // Check if we should regular cancel (cancel thresh and other normal reasons)
      if (shouldCancelPx(one_buy_px, Side::Buy, false)) {
        should_cancel = true;
      } else if (n_iters == 0 && ordex_allowed_buy <= 0) {
        should_cancel = true;
      } else if (n_iters == 0 && will_cancel_isolated_ &&
                 pktrade::approx_equal(one_buy_px, local_sig_->getBBMeasure()) &&
                 one_buy_order.init_qty.toDouble() >= local_sig_->getBBSize() - pktrade::EPS) {
        // We might be stranded at top. Check the next level.
        // Note that even if we iterate through to multiple orders, only the top one
        // should match the local_sig_->getBBMeasure().

        // Check the second level in the book to see if we're stranded.
        auto& buy_side = lvl_book->buySide();
        auto next_lvl_it = std::next(buy_side.begin());
        if (next_lvl_it != buy_side.end() &&
            one_buy_order.px.toDouble() - next_lvl_it->px.toDouble() > min_tick_ + pktrade::EPS) {
          // Then we probably stranded.
          should_cancel = true;
        }
      }

      // Front size-cap re-enforcement: cancel if outstanding front qty is
      // meaningfully above the current sided cap. 1.3x band avoids flicker;
      // maybePlaceFront re-quotes at the new (lower) cap on the next tick.
      if (n_iters == 0 && local_size_cap_mult_ > 0 && inside_size_buy_ems_ > 0 &&
          one_buy_order.init_qty.toDouble() >
              1.3 * local_size_cap_mult_ * inside_size_buy_ems_) {
        should_cancel = true;
      }

    } else if (canCancelOrd(one_buy_order, true)) {
      // Check if we should fast cancel (pred_px has moved through us)
      if (shouldCancelPx(one_buy_px, Side::Buy, true)) {
        should_cancel = true;
        LOG_EVERY_N(INFO, 11) << fmt::format("({}) Fast cancelling bid at {} (pk_oid {})",
                                             symbol_.get(), one_buy_px, one_buy_order.pk_oid);
      }
    }

    if (should_cancel) {
      // Then cancel.
      // Add a cancelTime to this.
      one_buy_order.cancel_time = now_fire_t_;
      CancelOrder cancelOrd{one_buy_order.pk_oid};
      riskman_->cancelOrd(cancelOrd);
      last_t_cxl_ = now_fire_t_;
    }

    n_iters++;
  }
  // Asks.

  double one_sell_px = 0;
  n_iters = 0;
  for (auto& one_sell_order : sell_orders_) {
    one_sell_px = one_sell_order.px.toDouble();
    should_cancel = false;

    if (canCancelOrd(one_sell_order)) {
      // Check if we should regular cancel (cancel thresh and other normal reasons)
      if (shouldCancelPx(one_sell_px, Side::Sell, false)) {
        should_cancel = true;
      } else if (n_iters == 0 && ordex_allowed_sell <= 0) {
        should_cancel = true;
      } else if (n_iters == 0 && will_cancel_isolated_ &&
                 pktrade::approx_equal(one_sell_px, local_sig_->getBAMeasure()) &&
                 one_sell_order.init_qty.toDouble() >= local_sig_->getBASize() - pktrade::EPS) {
        // We might be stranded at top. Check the next level.
        // Note that even if we iterate through to multiple orders, only the top one
        // should match the local_sig_->getBBMeasure().

        // Check the second level in the book to see if we're stranded.
        auto& sell_side = lvl_book->sellSide();
        auto next_lvl_it = std::next(sell_side.begin());

        if (next_lvl_it != sell_side.end() &&
            next_lvl_it->px.toDouble() - one_sell_order.px.toDouble() > min_tick_ + pktrade::EPS) {

          should_cancel = true;
        }
      }

      // Front size-cap re-enforcement (sided): see buy-side comment above.
      if (n_iters == 0 && local_size_cap_mult_ > 0 && inside_size_sell_ems_ > 0 &&
          one_sell_order.init_qty.toDouble() >
              1.3 * local_size_cap_mult_ * inside_size_sell_ems_) {
        should_cancel = true;
      }
    } else if (canCancelOrd(one_sell_order, true)) {
      // Check if we should fast cancel (pred_px has moved through us)
      if (shouldCancelPx(one_sell_px, Side::Sell, true)) {
        should_cancel = true;
        LOG_EVERY_N(INFO, 11) << fmt::format("({}) Fast cancelling ask at {} (pk_oid {})",
                                             symbol_.get(), one_sell_px, one_sell_order.pk_oid);
      }
    }

    if (should_cancel) {
      // Then cancel.
      if (ordex_allowed_sell == 0) {
        // printOutstandingOrders();
      }
      one_sell_order.cancel_time = now_fire_t_;
      CancelOrder cancelOrd{one_sell_order.pk_oid};
      riskman_->cancelOrd(cancelOrd);
      last_t_cxl_ = now_fire_t_;
      // Do not remove this until we get the ordCancel.
    }

    n_iters++;
  }
}

// Cancel and place orders on the back.
void RelWideMM2::manageBacklevels(const LevelBook* lvl_book, double abs_allowed_buy,
                                  double abs_allowed_sell) {

  // Cancel backlevels due to number of backlevels or remaining allowed size.
  if (buy_orders_.size() > 1 &&
      (buy_orders_.size() >= max_back_levels_ || abs_allowed_buy < order_size_.toDouble())) {
    // Cancel stuff from the end.

    // Also possible that the #cancel_shares already gets us past
    // abs_allowed_sell. So let's keep a count.
    double already_cxl_shrs = 0;
    int already_cxl_orders_ = 0;

    for (auto it = buy_orders_.rbegin(); it != buy_orders_.rend(); it++) {
      // Backlevels should not care about the front order.
      if (it->pk_oid == buy_orders_.begin()->pk_oid) {
        break;
      }

      if (buy_orders_.size() - already_cxl_orders_ < max_back_levels_ &&
          already_cxl_shrs + abs_allowed_buy > order_size_.toDouble()) {
        break;
      }

      if (it->cancel_time != 0) {
        already_cxl_shrs += order_size_.toDouble();
        already_cxl_orders_++;
      }

      if (canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;

        // Should be enough.
        break;
      }
    }
  } else if (
      // If false, just let front levels become back levels, don't place back levels explicitly
      place_back_levels_ &&
      // Force us to add new backlevels only when it's a quiet period.
      (now_fire_t_ - last_t_place_ > ms_between_backlevel_) && buy_orders_.size() > 0 &&
      buy_orders_.size() < max_back_levels_ - 2 && abs_allowed_buy > order_size_.toDouble()) {

    // We have space in the stack, so we can almost certainly place a
    // backlevel. We iterate through the orders to see the next spot
    // we can place it. We track this with new_backlevel_px.

    auto prev_buy_order = buy_orders_.begin();
    double prev_buy_px = prev_buy_order->px.toDouble();

    // Create a variable rung spacing
    // Give it a little more rungsize so we don't flicker backlevels.
    double effective_rung_spacing = rung_spacing_px_;

    // Potential price where we would place a new backlevel.
    // It might be between two orders, or at the end of the stack.
    double new_backlevel_px = roundToSide(prev_buy_px - effective_rung_spacing, false);

    auto next_buy_order = prev_buy_order + 1;

    while (next_buy_order != buy_orders_.end()) {
      double next_buy_px = next_buy_order->px.toDouble();

      // Check if we have space.
      if (new_backlevel_px - next_buy_px > effective_rung_spacing) {
        break;
      }
      // Otherwise, continue and reset our state.
      effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      prev_buy_px = next_buy_px;
      new_backlevel_px = roundToSide(prev_buy_px - effective_rung_spacing, false);
      prev_buy_order = next_buy_order;
      next_buy_order++;
    }

    // Sparsity check: see wide_mm.cc::manageBacklevels for description.
    if (min_foreign_inside_mult_ > 0 && inside_size_buy_ems_ > 0) {
      double sparse_thresh = min_foreign_inside_mult_ * inside_size_buy_ems_;
      double sparse_px = protection::sparseBuyBacklevelPx(
          lvl_book, prev_buy_px, new_backlevel_px, sparse_thresh,
          buy_orders_, min_tick_);
      if (!std::isfinite(sparse_px)) {
        new_backlevel_px = std::numeric_limits<double>::quiet_NaN();
      } else {
        new_backlevel_px = roundToSide(sparse_px, false);
      }
    }

    // Place backlevel size.
    double new_order_size = genOrderSize(false);

    // Min and round.
    new_order_size = std::min(new_order_size, abs_allowed_buy);
    Quantity new_order_qty =
        pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, new_order_size, false);
    if (new_order_qty.toDouble() > 0 && std::isfinite(new_backlevel_px)) {

      // Then, we can place the backlevel.
      // Place the backlevel.
      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Buy,
                   new_order_qty,
                   Price{std::to_string(new_backlevel_px)},
                   OrderType::Limit,
                   tif_,
                   false,
                   false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Back);

        // Place it in the back.
        buy_orders_.emplace(next_buy_order,
                            SimpleOrder{pkord_id, Side::Buy,
                                        Price{std::to_string(new_backlevel_px)}, new_order_qty,
                                        SimpleOrderState::LIVE, now_fire_t_, 0});
      }
    }
  }

  // Sell.
  if (sell_orders_.size() > 1 &&
      (sell_orders_.size() >= max_back_levels_ || abs_allowed_sell < order_size_.toDouble())) {
    double already_cxl_shrs = 0;
    int already_cxl_orders_ = 0;

    for (auto it = sell_orders_.rbegin(); it != sell_orders_.rend(); it++) {
      // If it's just one size. Then it might not be a backlevel.
      if (it->pk_oid == sell_orders_.begin()->pk_oid) {
        break;
      }

      if (sell_orders_.size() - already_cxl_orders_ < max_back_levels_ &&
          already_cxl_shrs + abs_allowed_sell > order_size_.toDouble()) {
        break;
      }

      if (it->cancel_time != 0) {
        already_cxl_shrs += order_size_.toDouble();
        already_cxl_orders_++;
      }

      if (canCancelOrd(*it)) {
        // std::cout << " cancelbacklevels 1 for " << it->pk_oid << std::endl;
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
        break;
      }
    }
  } else if (
      // If false, just let front levels become back levels, don't place back levels explicitly
      place_back_levels_ &&
      // Force us to add new backlevels only when it's a quiet period.
      (now_fire_t_ - last_t_place_ > ms_between_backlevel_) && sell_orders_.size() > 0 &&
      sell_orders_.size() < max_back_levels_ - 2 && abs_allowed_sell > order_size_.toDouble()) {

    // We have space in the stack, so we can almost certainly place a
    // backlevel.

    auto prev_sell_order = sell_orders_.begin();
    double prev_sell_px = prev_sell_order->px.toDouble();

    // Create a variable rung spacing
    double effective_rung_spacing = rung_spacing_px_;

    double new_backlevel_px = roundToSide(prev_sell_px + effective_rung_spacing, true);
    auto next_sell_order = prev_sell_order + 1;

    while (next_sell_order != sell_orders_.end()) {
      double next_sell_px = next_sell_order->px.toDouble();

      if (next_sell_px - new_backlevel_px > effective_rung_spacing) {
        break;
      }

      effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      prev_sell_px = next_sell_px;
      new_backlevel_px = roundToSide(prev_sell_px + effective_rung_spacing, true);
      prev_sell_order = next_sell_order;
      next_sell_order++;
    }

    if (min_foreign_inside_mult_ > 0 && inside_size_sell_ems_ > 0) {
      double sparse_thresh = min_foreign_inside_mult_ * inside_size_sell_ems_;
      double sparse_px = protection::sparseSellBacklevelPx(
          lvl_book, prev_sell_px, new_backlevel_px, sparse_thresh,
          sell_orders_, min_tick_);
      if (!std::isfinite(sparse_px)) {
        new_backlevel_px = std::numeric_limits<double>::quiet_NaN();
      } else {
        new_backlevel_px = roundToSide(sparse_px, true);
      }
    }

    double new_order_size = genOrderSize(false);

    // Min and round.
    new_order_size = std::min(new_order_size, abs_allowed_sell);
    Quantity new_order_qty =
        pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, new_order_size, false);

    // Place the backlevel.
    if (new_order_qty.toDouble() > 0 && std::isfinite(new_backlevel_px)) {

      NewOrder ord{symbol_,
                   traded_books_[0],
                   Side::Sell,
                   new_order_qty,
                   Price{std::to_string(new_backlevel_px)},
                   OrderType::Limit,
                   tif_,
                   false,
                   false};
      PKOrderId pkord_id = riskman_->sendOrd(ord);
      if (pkord_id != -1) {
        riskman_->setPlaceReason(pkord_id, pktrade::risk::PlaceReason::Back);

        // Place it in the back.
        sell_orders_.emplace(next_sell_order,
                             SimpleOrder{pkord_id, Side::Sell,
                                         Price{std::to_string(new_backlevel_px)}, new_order_qty,
                                         SimpleOrderState::LIVE, now_fire_t_, 0});
      }
    }
  }

  // Cancel backlevels that violate rung_spacing constraints.
  // Walk through buy orders and ensure proper spacing.
  if (buy_orders_.size() > 1) {
    auto prev_buy_it = buy_orders_.begin();
    double prev_buy_px = prev_buy_it->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;

    for (auto it = prev_buy_it + 1; it != buy_orders_.end(); it++) {
      double cur_buy_px = it->px.toDouble();
      double spacing = prev_buy_px - cur_buy_px;

      // If spacing is too tight, cancel this order. Allow half-a-tick of FP
      // noise: placement uses floor() so actual spacing is in [rung, rung+tick),
      // and Price<->double round-tripping can shift subtraction results by ~1e-14.
      // A real "too tight" violation is at least a full tick.
      if (spacing < effective_rung_spacing - 0.5 * min_tick_ && canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
      } else {
        // Update for next iteration
        prev_buy_px = cur_buy_px;
        effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      }
    }
  }

  // Walk through sell orders and ensure proper spacing.
  if (sell_orders_.size() > 1) {
    auto prev_sell_it = sell_orders_.begin();
    double prev_sell_px = prev_sell_it->px.toDouble();
    double effective_rung_spacing = rung_spacing_px_;

    for (auto it = prev_sell_it + 1; it != sell_orders_.end(); it++) {
      double cur_sell_px = it->px.toDouble();
      double spacing = cur_sell_px - prev_sell_px;

      // If spacing is too tight, cancel this order. Same FP-tolerance reasoning
      // as the buy-side check above.
      if (spacing < effective_rung_spacing - 0.5 * min_tick_ && canCancelOrd(*it)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = now_fire_t_;
      } else {
        // Update for next iteration
        prev_sell_px = cur_sell_px;
        effective_rung_spacing *= per_backlevel_rung_spacing_mult_;
      }
    }
  }

  last_t_backlevel_ = now_fire_t_;

  // Perhaps place orders on the back too.
}

void RelWideMM2::read_snapshots_supabase() {
  // How about this - we only read it the first time in sim mode, so there is an anchor point
  // But we don't do the repeated reading. It slows us down a lot when we're sims/climbs. And
  // probably overloads supabase too.
  if (!pktrade::GlobalVar::live_) {

    if (sim_read_supabase_already_) {
      // Don't make multiple supabase requests while in sim mode, it'll slow us down.
      static bool logged_skip_supabase = false;
      if (!logged_skip_supabase) {
        LOG(INFO) << fmt::format(
            "({}) RelWideMM: skipping subsequent Supabase snapshot fetch while running in sim",
            symbol_.get());
        logged_skip_supabase = true;
      }
      return;
    } else {
      sim_read_supabase_already_ = true;
    }

    // Check if we already got the snapshot from a previous run.
    if (!sim_snapshot_cache_file_.empty() && std::filesystem::exists(sim_snapshot_cache_file_)) {
      LOG(INFO) << fmt::format("({}) Found snapshot cache file: {}", symbol_.get(),
                               sim_snapshot_cache_file_);

      rapidjson::Document snapshot_json = util::read_json_file(sim_snapshot_cache_file_);
      onSnapshotJson(snapshot_json);
      return;
    } else {
      LOG(INFO) << fmt::format("({}) Didn't see a cache file at {}", symbol_.get(),
                               sim_snapshot_cache_file_);
    }
  }

  // Get the snapshots from supabase.
  const std::string& rel_sym = relative_sig_->getPrimarySymbol();
  const std::string& remote_sym = remote_sig_->getPrimarySymbol();

  try {
    // Get Supabase credentials from singleton
    auto& sb_creds = pktrade::util::SupabaseCredentials::getInstance();

    // Build the query URL
    const std::string& or_string =
        fmt::format("(and(sym_1.eq.{rel_sym},sym_2.eq.{remote_sym})"
                    ",and(sym_1.eq.{remote_sym},sym_2.eq.{rel_sym}))",
                    fmt::arg("rel_sym", rel_sym), fmt::arg("remote_sym", remote_sym));

    cpr::Parameters params;
    if (pktrade::GlobalVar::live_) {
      // In live, get the most recent snapshot
      params = cpr::Parameters{{"or", or_string}, {"order", "created_at.desc"}, {"limit", "1"}};
      LOG(INFO) << fmt::format("({}) Querying Supabase for most recent snapshot", symbol_.get());

    } else {
      // In sim, only get snapshots created before the current sim time
      auto now = pktrade::Clock::now();
      auto system_now = clock_cast<std::chrono::system_clock>(now);
      std::string current_time_str = date::format("%Y-%m-%dT%H:%M:%SZ", system_now);
      params = cpr::Parameters{{"or", or_string},
                               {"created_at", "lt." + current_time_str},
                               {"order", "created_at.desc"},
                               {"limit", "1"}};
      LOG(INFO) << fmt::format("({}) Querying Supabase for snapshots before: {}", symbol_.get(),
                               current_time_str);
    }

    const cpr::Timeout timeout{std::chrono::seconds(3)};

    // Make the request via sb_creds
    // Only use async in live, because in sim, by the time the response comes back, we'd already be
    // done with the day.
    if (pktrade::GlobalVar::live_) {
      sb_creds.cprGetSnapshotAS(params, timeout, this);
    } else {
      onGetSnapshot(sb_creds.cprGetSnapshot(params, timeout));
    }
  } catch (const std::exception& e) {
    LOG(ERROR) << fmt::format("({}) Exception reading snapshots from Supabase: {}", symbol_.get(),
                              e.what());
  }
}

void RelWideMM2::onGetSnapshot(const cpr::Response& res) {

  if (res.status_code != 200) {
    LOG(WARNING) << fmt::format("({}) Failed to read snapshots from Supabase: {} - {}",
                                symbol_.get(), res.status_code, res.text);
    return;
  }
  LOG(INFO) << fmt::format("({}) Got snapshot info: {}", symbol_.get(), res.text);

  // If sim, then we write it to a file.
  if (!pktrade::GlobalVar::live_ && !sim_snapshot_cache_file_.empty()) {
    // Write to the snapshot file.
    std::ofstream out(sim_snapshot_cache_file_);
    out << res.text;
    out.close();
  }

  // Parse the response
  rapidjson::Document json_response;
  json_response.Parse(res.text.c_str());
  onSnapshotJson(json_response);
}

void RelWideMM2::onSnapshotJson(rapidjson::Document& snapshot_doc) {
  const std::string& rel_sym = relative_sig_->getPrimarySymbol();
  const std::string& remote_sym = remote_sig_->getPrimarySymbol();

  if (!snapshot_doc.IsArray() || snapshot_doc.Empty()) {
    LOG(ERROR) << fmt::format("({}) No snapshots found for pair: {}/{}", symbol_.get(), rel_sym,
                              remote_sym);
    return;
  }

  // Extract the prices from the first (latest) snapshot
  const rapidjson::Value& snapshot = snapshot_doc[0];
  if (snapshot.HasMember("px_1") && snapshot.HasMember("px_2") &&
      snapshot.HasMember("created_at")) {

    // Check time difference between then and now.
    std::string created_at_str = snapshot["created_at"].GetString();
    int64_t snapshot_epoch = pktrade::time_utils::postgresTimestampToSecs(created_at_str);
    int64_t snapshot_ms = snapshot_epoch * 1000;
    // Use simulation time instead of wall-clock time for age calculation
    int64_t current_epoch = time_utils::nowToS();

    // Also check if the snapshot is older than our previously measured snapshot.
    if (snapshot_ms < last_perm_snapshot_t_) {
      // Don't use the database snapshot, ours is better.
      return;
    }

    // Check if snapshot is too old
    // Extended this, because we can restart a process over the weekend.
    const int64_t max_age_seconds = 60 * 60 * 24 * 3; // 3 days
    int64_t age_seconds = current_epoch - snapshot_epoch;

    if (age_seconds > max_age_seconds) {
      LOG(WARNING) << fmt::format("({}) Snapshot too old: {} seconds old (max allowed: {})",
                                  symbol_.get(), age_seconds, max_age_seconds);
      return;
    }

    if (rel_sym == snapshot["sym_1"].GetString() && remote_sym == snapshot["sym_2"].GetString()) {
      last_perm_snapshot_rel_px_ = snapshot["px_1"].GetDouble();
      last_translated_px_ = last_perm_snapshot_remote_px_ = snapshot["px_2"].GetDouble();
    } else {
      last_perm_snapshot_rel_px_ = snapshot["px_2"].GetDouble();
      last_translated_px_ = last_perm_snapshot_remote_px_ = snapshot["px_1"].GetDouble();
    }

    last_perm_snapshot_t_ = snapshot_ms;
    LOG(INFO) << fmt::format("({}) Read snapshot from Supabase: {} = {}, {} = {} (age: {} seconds)",
                             symbol_.get(), rel_sym, last_perm_snapshot_rel_px_, remote_sym,
                             last_perm_snapshot_remote_px_, age_seconds);
  }
}

void RelWideMM2::snapshotRelativePricing() {

  // OK, I think this logic needs to change soon.
  // Let's walk through each of these carefully.

  read_snapshots_supabase();

  // Snapshot if both the relative and remote signals are valid.
  if (relative_sig_->getValue() != 0 && remote_sig_->getValue() != 0) {
    // Both signals have values.

    // The remote_mid is the one we have worries about. If we haven't
    // heard a remote_update in a long time, then it's probably out.
    // The assumption here is that the relative signal is the one that
    // updates more frequently.
    if (time_utils::nowToMs() - last_remote_mid_t_ > relative_px_after_n_ms_) {
      // In that case, we mark ourselves to relative_mode. We also don't
      // take the double-snapshots, because we only want those to happen
      // at the same time.
      in_relative_mode_ = true;
      recalcThreshes();
    } else {
      last_perm_snapshot_rel_px_ = relative_sig_->getValue();
      last_translated_px_ = last_perm_snapshot_remote_px_ = remote_sig_->getValue();
      last_perm_snapshot_t_ = time_utils::nowToMs();
      LOG(INFO) << fmt::format("({}) SNAPSHOTTING MIDS remote {} relative {}", symbol_.get(),
                               last_perm_snapshot_remote_px_, last_perm_snapshot_rel_px_);
    }
  } else if (relative_sig_->getValue() != 0) {
    // If we hit a point where we haven't received remote updates in a while,
    // then we can assume that it's off (eg. CME is in maintenance mode) and
    // start using relative pricing.
    if (last_perm_snapshot_rel_px_ != 0 && last_perm_snapshot_remote_px_ != 0) {
      // We can now switch to relative mode.
      in_relative_mode_ = true;
      recalcThreshes();
    }
  }

  // Start the next one.
  pktrade::GlobalVar::event_loop_->onTimeout(remote_snapshot_interval_,
                                             [&] { this->snapshotRelativePricing(); });
}

void RelWideMM2::onFirstFire(double pred_px) {

  LOG(INFO) << fmt::format("({}) ONFIRSTFIRE HAPPENING", symbol_.get());

  // There might be a situation where we start up without a remotesignal.
  // Do this because... yeah. Let's review this logic too.

  // double approx_mid = remote_sig_->getValue();
  if (pred_px == 0) {
    return;
  }

  // Sanity check - if the predpx and mid are very off, prefer the mid, because
  // there might be something that's causing us to mess up the predpx calculation
  // (e.g., using a relative price on NQ right at the CME open, without a snapshot)

  double scaling_px = pred_px;
  if (local_sig_val_ != 0 && std::abs(pred_px - local_sig_val_) / local_sig_val_ > 0.1) {

    if (oracle_px_ != 0) {
      LOG(WARNING) << fmt::format(
          "({}) FIRSTFIRE predpx {} seems off, falling back to oracle px {}", symbol_.get(),
          pred_px, oracle_px_);

      scaling_px = oracle_px_;

    } else {
      LOG(WARNING) << fmt::format(
          "({}) FIRSTFIRE predpx {} seems off, falling back to local mid {}", symbol_.get(),
          pred_px, local_sig_val_);

      scaling_px = local_sig_val_;
    }
  }

  // Recalc the min_tick with this price to make sure SecMaster got it right the first time
  double new_min_tick = pktrade::GlobalVar::secmaster_->calc_tick_size(scaling_px).toDouble();
  if (!pktrade::approx_equal(min_tick_, new_min_tick)) {
    LOG(WARNING) << fmt::format(
        "({}) Corrected min_tick during firstfire from {} to {} (scaling_px {})", symbol_.get(),
        min_tick_, new_min_tick, scaling_px);
    min_tick_ = new_min_tick;
  } else {
    LOG(INFO) << fmt::format("({}) Reconfirmed min_tick during firstfire as {} (scaling_px {})",
                             symbol_.get(), min_tick_, scaling_px);
  }

  // Scale the order sizes from their notional targets.
  if (set_size_from_notional_) {
    Quantity tgt_order_sz = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_order_notional_ / scaling_px, true);
    Quantity tgt_max_pos = pktrade::GlobalVar::secmaster_->round_qty(
        traded_bid_, tgt_maxpos_notional_ / scaling_px, true);
    LOG(INFO) << fmt::format(
        "({}) Setting order size from notional, predpx {} : ordersize {} maxpos {}", symbol_.get(),
        pred_px, tgt_order_sz.toDouble(), tgt_max_pos.toDouble());

    // Sanity check: ordex maxpos should not exceed riskman's max_position.
    if (riskman_ != nullptr) {
      double risk_maxpos = riskman_->getMaxPos().toDouble();
      if (tgt_max_pos.toDouble() > risk_maxpos) {
        LOG(ERROR) << fmt::format(
            "({}) Ordex notional-derived maxpos ({}) exceeds risk max_position ({}). "
            "Orders will be rejected. Increase risk max_position.",
            symbol_.get(), tgt_max_pos.toDouble(), risk_maxpos);
      }
      if (tgt_order_sz.toDouble() > risk_maxpos) {
        LOG(ERROR) << fmt::format(
            "({}) Ordex notional-derived order_size ({}) exceeds risk max_position ({}). "
            "No orders will be placed. Increase risk max_position.",
            symbol_.get(), tgt_order_sz.toDouble(), risk_maxpos);
      }
    }

    order_size_ = tgt_order_sz;
    max_pos_ = tgt_max_pos;
  }

  // Scale the backlevel-related threhsolds.
  if (fixed_rung_spacing_thresh_ == 0) {
    rung_spacing_px_ = effThreshMult() * rung_spacing_mult_ * base_place_thresh_conf_ * scaling_px;
  } else {
    rung_spacing_px_ = fixed_rung_spacing_thresh_ * scaling_px;
  }
  // Minimize it with min_tick.
  rung_spacing_px_ = std::max(rung_spacing_px_, min_tick_);

  // Checks if we need to check for twap
  if (equity_off_hour_vwap_ && !looked_up_closing_print_) {
    pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(120),
                                               [&] { this->read_closing_cross_supabase(); });
  }
  LOG(INFO) << fmt::format("({}) Firstfire rung_spacing_px {} max_back_levels {}", symbol_.get(),
                           rung_spacing_px_, max_back_levels_);

  first_fire_t_ = time_utils::nowToMs();
  needs_first_fire_ = false;

  refreshSizeMultBaseFromCurrent();
}

void RelWideMM2::updatePremiumEma() {

  if (pred_px_ == 0 || local_sig_val_ == 0 || !std::isfinite(pred_px_) ||
      !std::isfinite(local_sig_val_)) {
    return;
  }

  // Calculate current premium (difference between local mid and pred_px)
  cur_premium_ = local_sig_val_ - pred_px_;

  // Update EMA with time decay
  int64_t dt = now_fire_t_ - last_premium_adjust_t_;
  double alpha = 1.0 - std::exp(-dt / premium_tdc_);
  premium_ema_ = alpha * cur_premium_ + (1.0 - alpha) * premium_ema_;

  last_premium_adjust_t_ = now_fire_t_;
}

void RelWideMM2::updatePredMomentumEms(double discounted_final_px) {

  if (pred_momentum_coef_ == 0) {
    // Don't do unnecessary stuff.
    return;
  }

  if (discounted_final_px <= 0 || !std::isfinite(discounted_final_px)) {
    // Sanity check.
    return;
  }

  // Initialize on first valid price.
  if (prev_discounted_final_px_ == 0) {
    prev_discounted_final_px_ = discounted_final_px;
    last_pred_momentum_update_t_ = now_fire_t_;
    return;
  }

  // Only do this calc every 500ms.
  int64_t dt = now_fire_t_ - last_pred_momentum_update_t_;
  if (dt < 500) {
    return;
  }

  // Calculate return.
  double price_return =
      (discounted_final_px - prev_discounted_final_px_) / prev_discounted_final_px_;

  // Sanity check for big jumps Cap at 100 bps
  if (std::abs(price_return) > 0.01) {
    LOG(WARNING) << fmt::format(
        "({}) PredMomentum : large return {:.4f} from {} to {}, clamping to 1%", symbol_.get(),
        price_return, prev_discounted_final_px_, discounted_final_px);
    // Maybe just cap it, that's all.
    price_return = std::max(std::min(price_return, 0.01), -0.01);
  }

  // Update EMS (Exponential Moving Sum) with time decay.
  if (dt > 0) {
    double decay = std::exp(-dt / pred_momentum_tdc_ms_);
    pred_momentum_ems_ = price_return + (decay)*pred_momentum_ems_;
  }

  prev_discounted_final_px_ = discounted_final_px;
  last_pred_momentum_update_t_ = now_fire_t_;
}

double RelWideMM2::getRandomValue(bool front) {
  if (front) {
    return dis_front_(gen_);
  }
  return dis_(gen_);
}

// Generate a random order size.
double RelWideMM2::genOrderSize(bool front) {
  return order_size_.toDouble() * getRandomValue(front);
}

// This should be called every time any component changes, including in_relative_mode_.
void RelWideMM2::recalcThreshes() {
  double thresh_mult = effThreshMult();
  mid_buy_thresh_ = base_buy_thresh_ * thresh_mult + extra_buy_thresh_;
  mid_sell_thresh_ = base_sell_thresh_ * thresh_mult + extra_sell_thresh_;
  if (in_relative_mode_) {
    mid_buy_thresh_ += rel_px_thresh_;
    mid_sell_thresh_ += rel_px_thresh_;
  }
}

void RelWideMM2::setThresh(double thresh) {
  if (thresh < 0.000005 || thresh > 0.2) {
    LOG(INFO) << fmt::format("({}) Probably sent wrong threshold {}, skipping", symbol_.get(),
                             thresh);
    return;
  }
  base_buy_thresh_ = base_sell_thresh_ = thresh;
  // Override and reset the usermsg thresh mult to 1, but preserve extra
  // threshes. The bleed/minfv slots are left alone: they're defensive state
  // owned by their own writers, and clearing them here would desync us from the
  // riskman's bleed_widened_ flag (it wouldn't re-widen).
  um_thresh_mult_ = 1;
  recalcThreshes();
  LOG(INFO) << fmt::format("({}) Base thresh set to {}. Final threshes: {} x {}", symbol_.get(),
                           thresh, mid_buy_thresh_, mid_sell_thresh_);
}

void RelWideMM2::setThreshMult(double thresh_mult, MultSource src) {
  // Create some sanity checks...
  if (thresh_mult < 0.1 || thresh_mult > 100) {
    LOG(ERROR) << fmt::format("({}) Probably sent wrong threshold multiplier {}, skipping",
                              symbol_.get(), thresh_mult);
    return;
  }
  // Remember the mult so we can reapply it on other usermsgs adjusting thresh
  if (src == MultSource::Usermsg) {
    um_thresh_mult_ = thresh_mult;
  } else if (src == MultSource::Bleed) {
    bleed_thresh_mult_ = thresh_mult;
  } else if (src == MultSource::MinFv) {
    minfv_thresh_mult_ = thresh_mult;
  } else {
    LOG(ERROR) << fmt::format("({}) Unrecognized MultSource {} for thresh mult, skipping",
                              symbol_.get(), magic_enum::enum_name(src));
    return;
  }
  double eff_mult = effThreshMult();

  // Scale the backlevel-related threhsolds.
  if (fixed_rung_spacing_thresh_ == 0 && (!needs_first_fire_)) {
    rung_spacing_px_ = eff_mult * rung_spacing_mult_ * base_place_thresh_conf_ * pred_px_;
    // Minimize it with min_tick.
    rung_spacing_px_ = std::max(rung_spacing_px_, min_tick_);
  }

  recalcThreshes();
  LOG(INFO) << fmt::format("({}) Thresh mult set to {} from {}. um {} bleed {} minfv {} eff {}. "
                           "Final threshes: {} x {}",
                           symbol_.get(), thresh_mult, magic_enum::enum_name(src), um_thresh_mult_,
                           bleed_thresh_mult_, minfv_thresh_mult_, eff_mult, mid_buy_thresh_,
                           mid_sell_thresh_);
}

void RelWideMM2::setOrderSize(double size) {
  if (size <= 0) {
    LOG(ERROR) << fmt::format("({}) Invalid order size {}, skipping", symbol_.get(), size);
    return;
  }
  LOG(INFO) << fmt::format("({}) setOrderSize {} -> {}", symbol_.get(), order_size_.toDouble(),
                           size);
  order_size_ = Quantity{std::to_string(size)};
  refreshSizeMultBaseFromCurrent();
}

void RelWideMM2::setSizeMult(double size_mult, MultSource src) {
  if (size_mult <= 0) {
    LOG(ERROR) << fmt::format("({}) Invalid size mult {}", symbol_.get(), size_mult);
    return;
  }

  if (!has_base_sizes_) {
    refreshSizeMultBaseFromCurrent();
  }
  if (src == MultSource::Conf) {
    conf_size_mult_ = size_mult;
  } else if (src == MultSource::Usermsg) {
    um_size_mult_ = size_mult;
  } else if (src == MultSource::DoubleDown) {
    dd_size_mult_ = size_mult;
  } else {
    LOG(ERROR) << fmt::format("({}) Unrecognized MultSource {}, skipping", symbol_.get(),
                              magic_enum::enum_name(src));
    return;
  }
  double eff_mult = effSizeMult();

  Quantity new_maxpos =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, base_max_pos_ * eff_mult, true);
  Quantity new_order_size =
      pktrade::GlobalVar::secmaster_->round_qty(traded_bid_, base_order_size_ * eff_mult, true);

  LOG(INFO) << fmt::format(
      "({}) setSizeMult ({} from {}) conf {} um {} dd {} eff {} maxpos {} -> {} ordersize {} -> {}",
      symbol_.get(), size_mult, magic_enum::enum_name(src), conf_size_mult_, um_size_mult_,
      dd_size_mult_, eff_mult, max_pos_.toDouble(), new_maxpos.toDouble(), order_size_.toDouble(),
      new_order_size.toDouble());
  if (set_size_from_notional_) {
    double new_tgt_order_notional = base_tgt_order_notional_ * eff_mult;
    double new_tgt_maxpos_notional = base_tgt_maxpos_notional_ * eff_mult;
    LOG(INFO) << fmt::format("({}) notionaltgts changed order ({} -> {}) maxpos ({} -> {})",
                             symbol_.get(), tgt_order_notional_, new_tgt_order_notional,
                             tgt_maxpos_notional_, new_tgt_maxpos_notional);
    tgt_order_notional_ = new_tgt_order_notional;
    tgt_maxpos_notional_ = new_tgt_maxpos_notional;
  }

  max_pos_ = new_maxpos;
  order_size_ = new_order_size;
}

void RelWideMM2::setRelativeBeta(double beta) {
  LOG(INFO) << fmt::format("({}) setRelativeBeta: {} to {}", symbol_.get(), relative_beta_, beta);
  relative_beta_ = beta;
}

void RelWideMM2::setExtraThreshes(double extra_buy, double extra_sell) {
  extra_buy_thresh_ = extra_buy;
  extra_sell_thresh_ = extra_sell;
  recalcThreshes();
  LOG(INFO) << fmt::format("({}) Extra threshes set to {} x {}. Final threshes: {} x {}",
                           symbol_.get(), extra_buy, extra_sell, mid_buy_thresh_, mid_sell_thresh_);
}

void RelWideMM2::setConstPredPx(double px) {
  if (use_const_pred_px_) {
    LOG(INFO) << fmt::format("({}) setConstPredPx: {} to {}", symbol_.get(), const_pred_px_, px);
    const_pred_px_ = px;
  } else {
    LOG(WARNING) << fmt::format("({}) setConstPredPx: not using const_pred_px, ignoring!",
                                symbol_.get());
  }
}

Quantity RelWideMM2::getMaxPos() { return max_pos_; }

void RelWideMM2::refreshSizeMultBaseFromCurrent() {
  double denom = effSizeMult();
  if (denom <= 0) {
    denom = 1;
  }
  base_max_pos_ = max_pos_.toDouble() / denom;
  base_order_size_ = order_size_.toDouble() / denom;
  base_tgt_order_notional_ = tgt_order_notional_ / denom;
  base_tgt_maxpos_notional_ = tgt_maxpos_notional_ / denom;
  has_base_sizes_ = true;
}


SymbolId RelWideMM2::getTradedSymbols() { return symbol_; }

std::vector<Market> RelWideMM2::getMarkets() { return {traded_books_}; }

std::vector<pktrade::BookId> RelWideMM2::getBookIds() {
  std::vector<pktrade::BookId> book_ids;
  for (pktrade::Market m : traded_books_) {
    book_ids.push_back({m, symbol_});
  }
  return book_ids;
}

// callbacks from risk.
// I guess this doesn't do anything either. OK.
void RelWideMM2::newOrdAck(const Order& ord) {}

// This doesn't do anything.
void RelWideMM2::ordUpdate(const Order& ord) {}

// We can remove it from our data structure then.
void RelWideMM2::ordCancel(const Order& ord) {
  if (ord.side == Side::Buy) {
    for (auto it = buy_orders_.begin(); it != buy_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        it = buy_orders_.erase(it);
        break;
      }
    }
  } else if (ord.side == Side::Sell) {
    for (auto it = sell_orders_.begin(); it != sell_orders_.end(); it++) {
      if (it->pk_oid == ord.pk_order_id) {
        it = sell_orders_.erase(it);
        break;
      }
    }
  }
}

// The only thing to do is to remove it from the data structure.
void RelWideMM2::ordExec(const Order& ord, const OrderExecute& exec) {
  if (print_ords_) {
    pktrade::util::log_line("----------------------", false);
    pktrade::util::log_line(fmt::format("{}: exec ord {} px {} size {} side {} ",
                                        time_utils::nowToStr().c_str(), ord.pk_order_id,
                                        ord.px.toDouble(), exec.qty.toDouble(),
                                        ord.side == Side::Buy ? "BUY" : "SELL"),
                            false);
    printOutstandingOrders();
  }

  if (ord.curr_qty == Quantity{0}) {
    // Remove it from the
    ordCancel(ord);
  }

  // Decay and update curv_impulse.
  applyExecDecays();
  double xtra_impulse = (exec.qty.toDouble() / order_size_.toDouble());
  if (ord.side == Side::Sell) {
    xtra_impulse *= -1;
  }
  curv_impulse_ += xtra_impulse;
}

// Shouldn't happen. But it might. Let's just call the same thing as
// ordcancel.
void RelWideMM2::ordElim(const Order& ord, const OrderElimination& elim) { ordCancel(ord); }

void RelWideMM2::ordReject(const Order& ord, const NewOrderReject& rej) { ordCancel(ord); }

// Manage and upkeep on refsig-based threshing.
void RelWideMM2::onSignalValue(int sig_id, double value) {
  if (sig_id == remote_sig_idx_) {
    if (value != 0) {
      // LOG(INFO) << " onSignalValue " << sig_id << " " << value ;
      remote_sig_val_ = value;
      last_remote_mid_t_ = time_utils::nowToMs();

      // Update vol mechanisms (1-3).
      vol_mechs_.onRemoteUpdate(value);

      // If we thought the remote feed cut off but it came back, we should use
      // it. It is the "true price" of the thing we're trading.
      if (in_relative_mode_) {
        in_relative_mode_ = false;
        recalcThreshes();
      }
    }
  }

  if (sig_id == relative_sig_idx_) {
    last_relative_px_ = value;
    last_relative_mid_t_ = time_utils::nowToMs();

    // printf("Got relvalue %lld %f\n", time_utils::nowToMs(), value);
  }

  if (sig_id == local_sig_idx_) {
    // Maintain the premium ema calculation.
    local_sig_val_ = value;
    last_mid_t_ = time_utils::nowToMs();
  }

  if (needs_start_snapshotting_ && relative_sig_ != nullptr) {
    LOG(INFO) << fmt::format("({}) Scheduling snapshot in 5s", symbol_.get());

    pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(5),
                                               [&] { this->snapshotRelativePricing(); });
    needs_start_snapshotting_ = false;
  }

  // Just do the exec decays here.
  // I have this here so that we decay more often than every time
  // we get an exec (which can be pretty infrequent), but
  // less frequently than every onFire event. Hence, this.
  applyExecDecays();
}

void RelWideMM2::applyExecDecays() {
  // Decay and update curv_impulse.
  double impulse_decay_ =
      std::exp(-(time_utils::nowToMs() - last_exec_decay_t_) / curv_impulse_tdc_ms_);
  curv_impulse_ *= impulse_decay_;

  last_exec_decay_t_ = time_utils::nowToMs();
};

void RelWideMM2::onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) {
  if (bk.getBookID() != traded_bid_) return;
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void RelWideMM2::onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) {
  if (bk.getBookID() != traded_bid_) return;
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void RelWideMM2::onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) {
  if (bk.getBookID() != traded_bid_) return;
  protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                  buy_orders_, sell_orders_, min_tick_,
                                  inside_size_buy_ems_, inside_size_buy_last_t_,
                                  inside_size_sell_ems_, inside_size_sell_last_t_);
}

void RelWideMM2::onTrade(const LevelBook& bk, const Trade& trd) {
  if (bk.getBookID() == traded_bid_) {
    // Mechanism 4 and inside-size protections are based on the local traded book.
    vol_mechs_.onBookUpdate(bk);

    protection::updateInsideSizeEms(bk, local_size_cap_tdc_ms_, time_utils::nowToMs(),
                                    buy_orders_, sell_orders_, min_tick_,
                                    inside_size_buy_ems_, inside_size_buy_last_t_,
                                    inside_size_sell_ems_, inside_size_sell_last_t_);
  }

  // Existing VWAP logic.
  if (!equity_off_hour_vwap_) {
    return;
  }

  int64_t now_t = time_utils::nowToMs();

  // If this is not the first trade, apply time decay to existing VWAP
  if (last_vwap_t_ > 0) {
    int64_t dt = now_t - last_vwap_t_;
    double decay_factor = std::exp(-dt / vwap_tdc_ms_);

    running_vwap_num_ *= decay_factor;
    running_vwap_denom_ *= decay_factor;
  }

  double trade_px = trd.px.toDouble();
  double trade_qty = trd.qty.toDouble();

  running_vwap_num_ += trade_px * trade_qty;
  running_vwap_denom_ += trade_qty;

  // Calculate the new VWAP
  if (running_vwap_denom_ > 0) {
    running_vwap_ = running_vwap_num_ / running_vwap_denom_;
  }

  LOG_EVERY_N(INFO, 101) << fmt::format("({}) onTrade px {} sz {} running_vwap {}", symbol_.get(),
                                        trd.px.toDouble(), trd.qty.toDouble(), running_vwap_);
  /*
  std::cout<< fmt::format("{} : onTrade px {} sz {} running_vwap {}\n",
    time_utils::nowToStr(), trd.px.toDouble(), trd.qty.toDouble(),
    running_vwap_
  ) << std::endl;
   */

  last_vwap_t_ = now_t;
}

void RelWideMM2::read_closing_cross_supabase() {
  // Don't do multiple lookups.
  if (looked_up_closing_print_) {
    return;
  }

  // Do the lookup and find the last closing print that was less than 24h ago.
  const std::string& remote_sym = remote_sig_->getPrimarySymbol();

  try {
    // Get Supabase credentials from singleton
    auto& sb_creds = pktrade::util::SupabaseCredentials::getInstance();

    // Build the query URL
    const cpr::Parameters params{
        {"symbol", fmt::format("eq.{}", remote_sym)}, {"order", "created_at.desc"}, {"limit", "1"}};
    const cpr::Timeout timeout{std::chrono::seconds(3)};

    // Make the request via sb_creds
    // Only use async in live, because in sim, by the time the response comes back, we'd already be
    // done with the day.
    if (pktrade::GlobalVar::live_) {
      sb_creds.cprGetClosingPrintAS(params, timeout, this);
    } else {
      onGetClosingPrint(sb_creds.cprGetClosingPrint(params, timeout));
    }

  } catch (const std::exception& e) {
    LOG(ERROR) << fmt::format("({}) Exception reading snapshots from Supabase: {}", symbol_.get(),
                              e.what());
  }

  times_tried_closing_print_lookup_++;

  // If we haven't been able to find it, try again in a minute.
  // Note that even if our first try was successful, placing this callback-to-self
  // isn't so bad because it will exit early (via looked_up_closing_print_ check)

  // exit if we tried too much.
  if (times_tried_closing_print_lookup_ > 6) {
    LOG(WARNING) << fmt::format("({}) Could not find appropriate closing print for {}",
                                symbol_.get(), remote_sig_->getPrimarySymbol());
  } else {
    // Add another lookup. The snapshotter might not have gone.
    pktrade::GlobalVar::event_loop_->onTimeout(closing_print_lookup_interval_,
                                               [this] { this->read_closing_cross_supabase(); });
  }
}

// Done when we read the closing cross from supabase, and
// potentially want to set the use off_hour vwap.
void RelWideMM2::onGetClosingPrint(const cpr::Response& res) {
  bool can_read = true;
  const std::string& remote_sym = remote_sig_->getPrimarySymbol();

  if (res.status_code != 200) {
    LOG(WARNING) << fmt::format("({}) Failed to read closing print from Supabase: {} - {}",
                                symbol_.get(), res.status_code, res.text);
    can_read = false;
  }

  // Parse the response
  rapidjson::Document json_response;
  json_response.Parse(res.text.c_str());

  if (!json_response.IsArray() || json_response.Empty() || json_response.HasParseError()) {
    LOG(INFO) << fmt::format("({}) No closing prints found for: {}", symbol_.get(), remote_sym);
    can_read = false;
  }

  // Extract the prices from the first (latest) snapshot
  if (can_read) {
    looked_up_closing_print_ = true;

    const rapidjson::Value& closing_print = json_response[0];
    if (closing_print.HasMember("created_at")) {

      // Check time difference between then and now.
      std::string created_at_str = closing_print["created_at"].GetString();
      int64_t closing_print_epoch = pktrade::time_utils::postgresTimestampToSecs(created_at_str);
      int64_t current_epoch = std::time(nullptr);

      // Check if closing print is too old (e.g., more than 1 day)
      const int64_t max_age_seconds = 60 * 60 * 24 * 1; // 1 day
      int64_t age_seconds = current_epoch - closing_print_epoch;

      if (age_seconds > max_age_seconds) {
        LOG(WARNING) << fmt::format("({}) Closing Print too old: {} seconds old (max allowed: {})",
                                    symbol_.get(), age_seconds, max_age_seconds);
      } else {
        // Add it to the tvwap.
        double closing_qty = closing_print["qty"].GetDouble();
        double closing_px = closing_print["px"].GetDouble();

        double closing_num = closing_qty * closing_px;
        double closing_denom = closing_qty;

        // Decay both appropriately to add them to the tvwap.

        double decay_factor = std::exp(-(age_seconds * 1000) / vwap_tdc_ms_);

        closing_num *= decay_factor;
        closing_denom *= decay_factor;

        // decay the vwap too.
        double vwap_decay_factor = std::exp(-(time_utils::nowToMs() - last_vwap_t_) / vwap_tdc_ms_);
        running_vwap_num_ *= vwap_decay_factor;
        running_vwap_denom_ *= vwap_decay_factor;

        // Add both to the vwap.
        running_vwap_num_ += closing_num;
        running_vwap_denom_ += closing_denom;

        last_vwap_t_ = time_utils::nowToMs();

        if (running_vwap_denom_ > 0) {
          running_vwap_ = running_vwap_num_ / running_vwap_denom_;
        }
        // Log.
        LOG(INFO) << fmt::format(
            "({}) Looked up closing print, trade was {} at {}. Now running_vwap is {}",
            symbol_.get(), closing_qty, closing_px, running_vwap_);
      }
    }
  }
}

// Stats w/ local trds.
// ord rejections.

void RelWideMM2::cancelOutstandingOrds() {
  now_fire_t_ = time_utils::nowToMs(); // canCancelOrd needs it updated
  if (buy_orders_.size() > 0) {
    for (auto it = buy_orders_.begin(); it != buy_orders_.end(); it++) {
      // Cancel it.
      if (canCancelOrd(*it, true)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = time_utils::nowToMs();
        last_t_cxl_ = time_utils::nowToMs();
      }
    }
  }

  if (sell_orders_.size() > 0) {
    for (auto it = sell_orders_.begin(); it != sell_orders_.end(); it++) {
      // Cancel it.
      if (canCancelOrd(*it, true)) {
        CancelOrder cancelOrd{it->pk_oid};
        riskman_->cancelOrd(cancelOrd);
        it->cancel_time = time_utils::nowToMs();
        last_t_cxl_ = time_utils::nowToMs();
      }
    }
  }
}

void RelWideMM2::backFromMinFv() {
  // If using this feature, widen the thresh mult when we come back from minfv.
  // This writes the minfv slot; since the effective mult is the widest slot, it
  // only takes effect if it's wider than what's already applied, and it can't be
  // clobbered by another source resetting its own slot to 1.
  if (thresh_mult_minfv_ > 1) {
    LOG(INFO) << fmt::format("({}) Back from minfv with widened thresh mult {}", symbol_.get(),
                             thresh_mult_minfv_);
    setThreshMult(thresh_mult_minfv_, MultSource::MinFv);
  }
}

void RelWideMM2::printOutstandingOrders() {
  const LevelBook* lvl_book = pktrade::GlobalVar::book_manager_->find(traded_bid_);

  auto& buy_side = lvl_book->buySide();
  auto& sell_side = lvl_book->sellSide();

  // Check if book sides are empty to avoid dereferencing invalid iterators
  if (buy_side.empty() || sell_side.empty()) {
    LOG(WARNING) << fmt::format(
        "({}) Cannot print outstanding orders: book side is empty (buy: {}, sell: {})",
        symbol_.get(), buy_side.empty(), sell_side.empty());
    return;
  }

  double best_bid_px = buy_side.begin()->px.toDouble();
  double best_ask_px = sell_side.begin()->px.toDouble();

  pktrade::util::log_line("*************** RelWideMM_ORDERS ***************", false);

  // Do capping of extreme alpha values.
  // OK, I'm going to do something inefficient but want to just print out the
  // state of the book too. Let's push all of these things into a vector and
  // then display the vector.

  std::vector<std::string> sell_lvls;
  std::vector<std::string> buy_lvls;

  double spread = 0;
  double pct_spread = 0;
  if (sell_orders_.size() > 0 && buy_orders_.size() > 0) {
    spread = sell_orders_.begin()->px.toDouble() - buy_orders_.begin()->px.toDouble();
    pct_spread = spread / sell_orders_.begin()->px.toDouble();
  }

  // The sell side.
  auto sell_order_it = sell_orders_.begin();
  for (auto ask_it = sell_side.begin();
       ask_it != sell_side.end() && sell_order_it != sell_orders_.end(); ask_it++) {
    // Step 1. Our orders might be alone at the top.
    if (ask_it->px > sell_order_it->px) {
      // We're on a price level that's not reflected by the book update.
      // Repeatedly increment the buy_order_it until it's on the same level.
      while (sell_order_it != sell_orders_.end() && ask_it->px > sell_order_it->px) {
        sell_lvls.push_back(fmt::format(
            "{} : Ord {} {} (next_lvl {})", sell_order_it->px.toDouble(), sell_order_it->pk_oid,
            sell_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
                : "",
            ask_it->px.toDouble()));
        sell_order_it++;
      }
    }

    if (sell_order_it == sell_orders_.end()) {
      break;
    }

    // After we potentially incremented a lot. Are we at a price level
    // with our orders?
    if (ask_it->px == sell_order_it->px) {
      // We're on a price level with lit orders.
      sell_lvls.push_back(fmt::format(
          "{} {}: Ord {} {}", ask_it->px.toDouble(), ask_it->qty.toDouble(), sell_order_it->pk_oid,
          sell_order_it->cancel_time != 0
              ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
              : ""));
      sell_order_it++;
      for (; sell_order_it != sell_orders_.end() && sell_order_it->px == ask_it->px;
           sell_order_it++) {
        sell_lvls.push_back(fmt::format(
            "  (same px)      : Ord {} {}", sell_order_it->pk_oid,
            sell_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
                : ""));
      }

    } else {
      // The book level doesn't contain an order.
      sell_lvls.push_back(fmt::format("{} {}", ask_it->px.toDouble(), ask_it->qty.toDouble()));
    }
  }
  // Go through the rest of the orders.
  for (; sell_order_it != sell_orders_.end(); sell_order_it++) {
    sell_lvls.push_back(fmt::format(
        "{} : Ord {} {}", sell_order_it->px.toDouble(), sell_order_it->pk_oid,
        sell_order_it->cancel_time != 0
            ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - sell_order_it->cancel_time)
            : ""));
  }

  // The buy side
  auto buy_order_it = buy_orders_.begin();
  for (auto bid_it = buy_side.begin();
       bid_it != buy_side.end() && buy_order_it != buy_orders_.end(); bid_it++) {
    // Are we alone, at the top?
    if (bid_it->px < buy_order_it->px) {
      // We're on a level by ourselves.
      // Repeatedly increment the buy_order_it until it's on the same level.
      while (buy_order_it != buy_orders_.end() && bid_it->px < buy_order_it->px) {
        buy_lvls.push_back(fmt::format(
            "{} : Ord {} {} (next_lvl {}) ", buy_order_it->px.toDouble(), buy_order_it->pk_oid,
            buy_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
                : "",

            bid_it->px.toDouble()));
        buy_order_it++;
      }
    }

    if (buy_order_it == buy_orders_.end()) {
      break;
    }

    // Do we have a sell order here?
    if (bid_it->px == buy_order_it->px) {
      buy_lvls.push_back(fmt::format(
          "{} {}: Ord {} {}", bid_it->px.toDouble(), bid_it->qty.toDouble(), buy_order_it->pk_oid,
          buy_order_it->cancel_time != 0
              ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
              : ""));
      buy_order_it++;
      for (; buy_order_it != buy_orders_.end() && buy_order_it->px == bid_it->px; buy_order_it++) {
        buy_lvls.push_back(fmt::format(
            "  (same px)      : Ord {} {}", buy_order_it->pk_oid,
            buy_order_it->cancel_time != 0
                ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
                : ""));
      }

    } else {
      buy_lvls.push_back(fmt::format("{} {}", bid_it->px.toDouble(), bid_it->qty.toDouble()));
    }
  }
  // Go through the rest of the orders.
  for (; buy_order_it != buy_orders_.end(); buy_order_it++) {
    buy_lvls.push_back(fmt::format(
        "{} : Ord {} {}\n", buy_order_it->px.toDouble(), buy_order_it->pk_oid,
        buy_order_it->cancel_time != 0
            ? fmt::format(" (cxl {}) ", time_utils::nowToMs() - buy_order_it->cancel_time)
            : ""));
  }

  // OK, print them now too.
  for (auto sells_it = sell_lvls.rbegin(); sells_it != sell_lvls.rend(); sells_it++) {
    pktrade::util::log_line(sells_it->c_str(), false);
  }

  pktrade::util::log_line(fmt::format(" <  spread {:.4f} pct_spread {:.4f} > ", spread, pct_spread),
                          false);

  for (auto buys_it = buy_lvls.begin(); buys_it != buy_lvls.end(); buys_it++) {
    pktrade::util::log_line(buys_it->c_str(), false);
  }
  pktrade::util::log_line("*************** END_PRINT ***************", false);
}

bool RelWideMM2::canCancelOrd(SimpleOrder& ord, bool fast /*= false*/) {
  // If we're not trying to do a fast cancel, check the ord lifetime and ms_between_cxl
  // Note: all calls to canCancelOrd come from tryFire, which of course updates now_fire_t_, or
  // cancelOutstandingOrds, which now updates it too.
  if (!fast && (now_fire_t_ - ord.place_time < ms_min_ord_lifetime_ ||
                now_fire_t_ - last_t_cxl_ < ms_between_cxl_)) {
    return false;
  }
  // If we've already sent a cancel, don't try again unless it's been a while.
  return (ord.cancel_time == 0 || now_fire_t_ - ord.cancel_time > CANCEL_RETRY_INTERVAL);
}
} // namespace pktrade::ordex
