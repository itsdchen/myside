#pragma once

#include "sim_base.h"

#include "pktrade/mktdata/md_beacon.h"
#include "pktrade/risk/trade_risk_man.h"

#include <chrono>
namespace pktrade::sim {

// Shared struct for level-based books.

struct PKSimOrd {
  PKOrderId pk_order_id;
  // We don't need to track both.
  // I guess we could if we wanted to do some proportional thing but it's ok.
  double shares_ahead;
  double shares_behind;
  // Assume everything is a normal limit order.

  // In the last few adds-liq events since we placed this sim order,
  // what were the sizes?
  // This helps us identify removes from behind us.
  std::vector<double> last_add_sizes;
  // How long this can go
  int add_sizes_limit = 10;
  int add_size_idx = 0;
  void add_add_size(double add_size);
  bool was_recent_add(double rm_size);
};

// TODO: check that the ordering is correct.
struct BuyAggComparator {
  bool operator()(double lhs, const double& rhs) const { return lhs > rhs; }
};

struct SellAggComparator {
  bool operator()(double lhs, const double& rhs) const { return lhs < rhs; }
};

// Creates a hyperliquid sim just to get things to compile and execute, not to
// actually run. Note: this is not yet implemented, because we don't have
// marketdata yet.
class HyperliquidSim : public SimBase {
 public:
  HyperliquidSim(SymbolId sym, pktrade::risk::TradeRiskMan* riskman, rapidjson::Value& sim_conf);

  // lets us track some ish.
  void eod() override;

  void subscribeData(pktrade::md::MDBeacon* beacon) override;
  std::vector<BookId> getRequiredDataSubs() override;
  void onSentOrder(Order* ord) override;
  void onOrderHitsME(Order* ord) override;

  void onCancelOrder(const CancelOrder& cancel) override;
  void onCancelHitsME(PKOrderId cancel_ord_id) override;

  // In a future case, we might want to compare our live orders on a per-tx
  // basis to check if they would have executed.
  void maybeOrderHitsBook(int64_t cur_book_transact_t) override;

  void maybeCxlHitsBook(int64_t cur_book_transact_t) override;

  // Beacon callbacks.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) override;
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) override;
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) override;
  void onTrade(const LevelBook& bk, const Trade& trd) override;
  void onFinal(const LevelBook& bk) override {};

 protected:
  // In the end state, make this use an enum.
  bool use_one_way_latency_;

  //ioc and gtc
  std::chrono::milliseconds ioc_ord_latency_;
  // This *might* be different? Let's still intsrument this .
  std::chrono::milliseconds gtc_ord_latency_;

  // These *should* be the same
  std::chrono::milliseconds alo_ord_latency_;
  std::chrono::milliseconds cxl_ord_latency_;

  // Default ord latency. Fallback if we need to.
  // I don't know how stuff like FOK gets interpreted.
  std::chrono::milliseconds ord_latency_;
  // just easier to not cast it all the time. I'm lazy sorry.
  int64_t ord_latency_int_;

  int64_t last_delta_t_from_mkt_ = 0;

  // When lag_aware_latency_ is true, per-TIF order delay is computed as
  //   eff_delay_ms = <tif>_lag_base_ms_ + <tif>_lag_coef_ * last_feed_lag_ms_
  // where <tif>_lag_base_ms_ is the fixed network/floor portion (paid even
  // when HL is uncongested) and <tif>_lag_coef_ * last_feed_lag_ms_ scales
  // with HL's observed congestion (one-way feed lag = now - transact_t
  // from the most recent HL trade message). HYPE trades can be subscribed
  // as an always-active HL-wide heartbeat for sparse-symbol windows; see
  // subscribeData. Default off.
  bool lag_aware_latency_ = false;
  // Defaults below were recalibrated 2026-06-28 against live equity-MM (gf3
  // 0616 retrains) over a 4D sim-vs-live sweep. See
  // overmind/studies/lag_aware_sim_recalibration_20260628.md (and the
  // raw sweep data in ~/scratch/relwide_retrains/postmortems/0620_retrains_usday/).
  // Earlier defaults (alo/cxl base=100, coef=0.8, BTC
  // heartbeat) were calibrated against June-2026 cross + MM strategies
  // (overmind/studies/lag_aware_sim_svl_settings_20260609.md) but the
  // BTC mktdata pipeline broke on 2026-06-11 — the BTC heartbeat stopped
  // refreshing, so coef × last_feed_lag_ms went to 0 for any sym whose
  // own trades were sparse. The recalibration:
  //   - Switched heartbeat sym from BTC to HYPE (sim_lvl_inst.cc).
  //   - Bucketed live RTT showed median place ~500ms, cancel ~790ms —
  //     cancel runs ~290ms slower than place across all syms. Old defaults
  //     assumed they were symmetric.
  //   - Refined 36-cell sweep across 18 gf3 retrain syms picked
  //     (alo_b=150, alo_c=1.25, cxl_b=350, cxl_c=1.25) as the global best
  //     by sum(e_fr + e_trd) — matches live order-flow fingerprint while
  //     keeping coefs modestly bumped from the BTC-era 0.8.
  // Turning on lag_aware_latency with no other knobs set gives a reasonable
  // out-of-box fit. Override per-strategy if you have reason to.

  // IOC: matched cross. base ~ network + HL match-engine floor (HL is a
  // blockchain, ~200-400ms block time + our network).
  //
  // Recalibrated 2026-07-21 post-fast-cancel-fix (commit e18e4552, 06-30):
  // extended grid sweep (ib in {500,750,1000,1250} × ic in
  // {0.5..3.0}) over gf1+gf3 crossers with reweighted composite score
  // (shs=0.5, fr=1.0, cfr=1.0, pnl=0.25) picked (750, 1.5) as the best
  // cross-strat compromise: gf3 crossers score 0.24-0.26, gf1 crossers
  // 0.30-0.50. Previous (900, 2.0) was systematically over-slowing IOCs
  // post-fix (eff@baseline 1870ms vs live_p50 790ms = 2.37x); new (750,
  // 1.5) gives eff@baseline 1478ms (1.87x). gf1 residual is
  // multi-host-collision, not lag-fixable. Prior 06-29 (900, 2.0)
  // calibration was pre-fast-cancel-fix.
  int64_t ioc_lag_base_ms_ = 750;
  double ioc_lag_coef_ = 1.5;
  // ALO: add-liquidity ack. No HL matching work, so base is moderate.
  // Recalibrated 2026-07-20 post-fast-cancel-fix (commit e18e4552, 06-30):
  // consensus across gf0/gf2/gf3 usday/allday sweeps (11 strats, 64-combo
  // grid) picked (300, 0.5) as the best cross-strat fit. Matches live
  // alo_p50 ≈ 450ms within 20%.
  int64_t alo_lag_base_ms_ = 300;
  double alo_lag_coef_ = 0.5;
  // Cancel: remove-from-book ack. Same sweep picked (450, 0.55).
  // Note: eff_cxl at baseline ~717ms is ~1.5× live cxl_p50 (460ms) —
  // sim needs cancels modeled slower than reality to counterbalance
  // otherwise-optimistic queue-position modeling. Empirically closes
  // shs_r to ~1.0 on the equity usday/allday family.
  int64_t cxl_lag_base_ms_ = 450;
  double cxl_lag_coef_ = 0.55;
  int64_t last_feed_lag_ms_ = 0;

  // To do the one-way latency thing without any lookahead.
  // Just pushing things onto a queue. This does make us be a little
  // asymmetrically pessimistic, though.

  // Separating these into alo, gtc, ioc
  // ALO
  std::vector<PKOrderId> place_alo_ords_q_;
  std::vector<int64_t> place_alo_transact_ts_;
  int place_alo_ords_idx_ = 0;
  int alo_ord_latency_int_; 

  // GTC
  std::vector<PKOrderId> place_gtc_ords_q_;
  std::vector<int64_t> place_gtc_transact_ts_;
  int place_gtc_ords_idx_ = 0;
  int gtc_ord_latency_int_; 

  // IOC
  std::vector<PKOrderId> place_ioc_ords_q_;
  std::vector<int64_t> place_ioc_transact_ts_;
  int place_ioc_ords_idx_ = 0;
  int ioc_ord_latency_int_ ;

  std::vector<PKOrderId> cxl_ords_q_;
  std::vector<int64_t> cxl_transact_t_;
  int cxl_ords_idx_ = 0;
  int cxl_ord_latency_int_ ;
  const LevelBook* bk_;

  // Keeping it side-agnostic because we can
  // unflag when sides get removed anyway.
  // Also, our fixed type doesn't allow for keying. So let's keep it at double
  // and allow for some issue for now. Should be consistent though if the string
  // repr is the same.
  std::unordered_map<double, SimFlaggedShares> flagged_shares_;

  // Ordered map
  std::map<double, std::vector<PKSimOrd>, BuyAggComparator> buy_add_ords_;
  std::map<double, std::vector<PKSimOrd>, SellAggComparator> sell_add_ords_;

  // For quick lookups. 
  std::set<PKOrderId> live_order_ids_;

  // If someone cancels, how much do we assume that the shares were ahead of our
  // orders, versus behind.
  double cxl_frac_ahead_;

  // This many from when we try crossing.
  int n_fills_our_cross_ = 0;

  // This many from when we get filled from an aggressive trade message
  int n_fills_other_cross_ = 0;

  // This many from when a make-liq order fills us
  // This is the thing I care about the most on hyperliquid, as
  // the incoming orders are snapshots.
  int n_fills_other_add_ = 0;
};

} // namespace pktrade::sim
