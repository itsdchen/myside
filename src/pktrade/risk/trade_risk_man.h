#pragma once

#include "pktrade/ordex/ordex.h"
#include "pktrade/risk/commissioner.h"
#include "pktrade/signals/signal_factory.h"
#include "pktrade/types.h"
#include <rapidjson/document.h>
#include <unordered_map>

#include "global_positioner.h"
#include "pktrade/oeapi.h"
#include "pktrade/sim/sim_verse.h"

/*

Kind of an all-in-one class that handles risk management, order placement, etc.

*/

namespace pktrade {
class LiveContext;
class LocalContext;
} // namespace pktrade

namespace pktrade::risk {

// Note: TRM only enforces exit-only. It's up to the ordex to enforce 0 thresh and crossing.
enum class TradingLevel {
  TL0 = 0, // normal trading
  TL1 = 1, // exit-only trading (crossing based on normal settings)
  TL2 = 2, // exit-only trading with aggressive thresh (crossing based on normal settings)
  TL3 = 3, // exit-only trading with max aggression (no predpx, 0 thresh)
  TL5 = 5, // cancel orders and pause trading for adders, cross aggressively to exit for crossers
  TL6 = 6, // cancel orders and cross aggressively to exit (even for adders)
  TL7 = 7, // pause new orders, but don't cancel existing ones (except for "normal" reasons)
};

// Sources of TL settings. Actual TL will be the max TL over all TLReasons.
enum class TLReason {
  Normal,       // set by DayStart, DayEnd, and other normal trading sequences
  Usermsg,      // set by Usermsg
  MinFV,        // set by MinFV
  Bleed,        // set by Bleed detector
  CancelReject, // set by TRM on receiving a cancel reject
  OrderReject,  // set by TRM on receiving certain types of order reject
  Derisk,       // set by scheduled de-risk (e.g. earnings)
  Misconfig,    // set by TRM on receiving a misconfigured order
};

// Type of order when we place it, for post-trade analysis
enum class PlaceReason {
  Unset = 0,
  Front = 100, // front level adding order
  Back = 101,  // back level adding order
  Cross = 200, // crossing order
};

// Known reasons for a NewOrderReject -- partly from those listed here (though incomplete):
// https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses
// Note: these reasons are mostly HL-specific.
enum class NewOrdRejReason {
  Unknown,     // log to ERROR and email
  Tick,        // log to ERROR, pause trading, and email
  MinTradeNtl, // log to ERROR, pause trading, and email
  PerpMargin,  // log to ERROR, pause trading, don't email (global so gateway already emails)
  BadAloPx,    // log to INFO
  IocCancel,   // don't log, most crosser orders will get this
  Oracle,      // log to ERROR and email
  OICap,       // log to ERROR, pause trading, and email
  Restart,     // log to INFO
  Expired,     // log to INFO
  L1Congested, // log to ERROR, pause trading, don't email (global so gateway already emails)
  TooManyOps,  // log to ERROR, pause trading, and email
  TooLarge,    // log to ERROR and email
  Pause429,    // log to ERROR and email
  Halted,      // log to ERROR, TL5, and email
  Liquidated,  // log to ERROR, TL5, and email
};

// Extra info for trades file (and maybe orders file)
struct OrdAnnot {
  // Fill this in later.
  BookId book_id{};
  PKOrderId pk_order_id{};

  double bb_ = 0.0;
  double ba_ = 0.0;

  double pred_px_ = 0.0;
  double mid_px_ = 0.0;
  int64_t t_sent_ = 0;
  int64_t last_uid_bk_ = 0;

  // Only used if we tried to cancel.
  int64_t cancel_t_ = 0;

  // was ioc?
  bool ioc_ = false;

  // For IOCs, we might get an elim before execs so want to keep the annotations around a bit longer
  // before erasing. This marks it so when we later do erase it, we don't duplicate bookkeeping.
  bool eliminated_ = false;

  // type of order when we place
  PlaceReason reason = PlaceReason::Unset;
};

// Info for doing precog-based unflagging.
struct PrecogOrdInfo {
  PKOrderId pk_order_id;
  int64_t t_sent;
  Price px;
  Quantity qty;
  bool precogged;
};

class TradeRiskMan : public Executor, public OEListener, public pktrade::md::BeaconListener {
 public:
  TradeRiskMan(std::string symbol_id, Market mkt, const rapidjson::Value& risk_conf,
               const rapidjson::Value& comms_conf, pktrade::signals::SignalFactory* sf,
               pktrade::ordex::Ordex* ordex);

  void registerPred(pktrade::signals::Signal* signal);
  void registerMid(pktrade::signals::Signal* signal);

  void postSecMaster();

  void subscribe();

  void onDayStart();

  SymbolId getSymbol() { return symbol_id_; }
  Market getMarket() { return mkt_; }

  // Executor interface.
  PKOrderId sendOrd(const NewOrder& ord) override;
  void cancelOrd(const CancelOrder& cxl) override;
  void modOrd(const ModifyOrder& mod) override;
  // HIP-4 capitalization requirement: the strategy declares how many complete sets it needs;
  // the gateway owns minting the shortfall and gating orders. Live-only; forwards to the live
  // context. Balance-aware sizing lives on the gateway (it reads on-chain balances).
  void sendCapitalReq(const CapitalReq& req) override;

  // OEListener interface callbacks.
  void onOE(const Order& ord, const GatewayAck& ack) override;
  void onOE(const Order& ord, const NewOrderAck& ack) override;
  void onOE(const Order& ord, const NewOrderReject& rej) override;
  void onOE(const Order& ord, const CancelOrderAck& ack) override;
  void onOE(const Order& ord, const CancelOrderReject& rej) override;
  void onOE(const Order& ord, const ModifyOrderAck& ack) override;
  void onOE(const Order& ord, const ModifyOrderReject& rej) override;
  void onOE(const Order& ord, const OrderExecute& exec) override;
  void onOE(const Order& ord, const OrderElimination& elim) override;
  // /////////////////////////////////////

  // MDBeacon callbacks.
  // This is currently used to do precog_miss.
  void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) override {};
  void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) override;
  void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) override;
  void onTrade(const LevelBook& bk, const Trade& trd) override;
  void onFinal(const LevelBook& bk) override {};

  // Given that an order was cancelled, clean it up from the precog side.
  void cleanPrecog(const PKOrderId oid, Side side);

  // Funding-related related functions.
  // On the time we should have received funding,
  // pay ourselves the approx funding.
  // What we want is to keep track of the last funding
  // rate before the next funding period, and
  // to pay that.
  void recordFundingPayments();

  // Supports TL0, Tl1, TL2, TL3, TL5, TL6, and TL7.
  void setTL(TradingLevel tl); // sets Normal TL and lowers all other TLs if necessary
  void setTL(TradingLevel tl, TLReason reason);
  const TradingLevel getTL() const { return cur_tl_; }
  const TradingLevel getTL(TLReason reason) const { return reason_to_tl_.at(reason); }

  const bool canTrade() const;
  const bool canPlaceMoreOrders() const;

  void hitMinFv();
  void backFromMinFv();

  // For ordex to set PlaceReason of order after calling sendOrd
  void setPlaceReason(PKOrderId oid, PlaceReason reason);

  /////////////////////////////////////////////////////////////////
  // Some stuff to calculate sizes and positions in notional space.

  double getMaxNotional() const;
  // Takes the more pessimistic bound of the maxpos and the maxnotional
  // and gives back a number in shares.
  double getMaxPosBoundByNotional() const;
  double getPosNotional() const;
  double getAvgExecPx() const;

  double getOutstandingBuyNotional() const;
  double getOutstandingSellNotional() const;

  bool getUseFunding() const;

  /////////////////////////////////////////////////////////////////

  bool doingGlobalInherit() { return do_global_inherit_; }
  void forceInheritLoop() { global_positioner_.forceInheritLoop(); }

  // This can happen multiple times, and picks up the position at the current mid.
  void addInheritPosition(double inherited_pos);

  // Scale max pos / max notional by a multiplier. Each MultSource is a separate
  // slot; the effective mult is their product. See MultSource. Double-down uses
  // the DoubleDown slot (see setDoubleDownLevel).
  void setSizeMult(double size_mult, MultSource src);
  const Quantity getPos() const;
  const Quantity getOutstandingShares(Side side) const;

  const Quantity getOutstandingCrossShares(Side side) const;

  // If pos = 2, and we have 3 limit orders on buy (at diff price levels)
  // this returns 5. So what's our exposure to the long side.
  const Quantity getMaxFillPosition(Side side, bool only_cx = false) const;

  // In the above example. If pos = 2, 3 limit orders on buy, and our maxpos
  // is 7. Then, we can still send 2 size before we hit our limits.
  const Quantity getSideAlloc(Side side) const;

  const Quantity getMaxPos() const;

  const int getNumPrecoggedMisses(Side side) const;

  const Quantity getShortableAmt() const;
  void setShortableAmt(Quantity shortable_amt);

  // Some accounting-related calls...
  const double getV2NetPnl() const;
  // This is not quite closedpnl. Forgive me.
  const double getV2ClosedPnl() const;
  const double getV2ApproxOpenPnl() const;

  // Net funding we have.
  const double getFunding() const;
  const double getCommissions() const;
  const double getMinPnlSeen() const;
  const double getShsSent() const;
  const double getShsTraded() const;
  const double getReachableShsSent() const;

  const double getIOCShsSent() const;
  const double getRmLiqShs() const;

  const double getNotionalTraded() const;
  const int getTimesTraded() const;
  const int getTimesFlipped() const;
  const BaseCurrency getBaseCurrency() const;

  void setLiveContext(LiveContext* live_ctx);
  void setLocalContext(LocalContext* local_ctx);

  void pauseTrading(double pause_secs, TLReason reason, std::string reason_str);

  // For situations like if the strategy gets a SIGINT.
  // iterate through all the still-alive orders and send cancels.
  void emergencyCancelOrders();

  const Order* getOrder(PKOrderId pk_order_id);

  void rmOutstandingShs(Quantity shs, double px, Side side, bool is_cx);

  void cleanOldIOCs();

  // TODO: implement this? I don't know if I actually need to, but it feels like
  // I should. In case something funky actually happens. void
  // maintainOutstandingCount();

 protected:
  // So far, I'm making all of these to be contained s.t. they exist within a
  // single trading symbol. Maybe better in the future to have one
  // trade_risk_man, but idk.

  BookId book_id_;

  SymbolId symbol_id_;

  Market mkt_;

  // This needs to be associated with a single trader and symbol. The
  // traderiskman

  // This is passed in and set in the constructor.
  pktrade::ordex::Ordex* ordex_ = nullptr;
  // For marking our open position.
  // Not going to be exactly openpnl, but might be.

  // Supports TL0, TL1, TL2, Tl3, TL5, TL6, and TL7.
  std::unordered_map<TLReason, TradingLevel> reason_to_tl_;
  TradingLevel cur_tl_;

  // To check against other positions.
  bool do_global_inherit_ = false;
  GlobalPositioner global_positioner_;

  pktrade::signals::Signal* pred_sig_;
  pktrade::signals::Signal* mid_sig_;

  // Risk-based settings.
  Quantity max_pos_;
  int max_orders_;
  double max_notional_;

  // Base values for setSizeMult (snapshot before first mult application).
  // One slot per MultSource; the effective mult is their product, so a usermsg
  // mult (or double-down) scales relative to the conf'd size instead of
  // replacing it. The DoubleDown slot lives in dd_size_mult_ (see the
  // double-down members below).
  bool has_base_sizes_ = false;
  double conf_size_mult_ = 1;
  double um_size_mult_ = 1;
  double base_max_pos_ = 0;
  double base_max_notional_ = 0;

  double effSizeMult() const { return conf_size_mult_ * um_size_mult_ * dd_size_mult_; }

  void refreshSizeMultBaseFromCurrent();

  // Basically for Spot. Give us a way of setting shortable limits for ourselves
  // (subject to borrow too).
  Quantity shortable_amt_;

  // Instantaneous risk settings.
  pktrade::Quantity position_;
  pktrade::Quantity outstanding_buy_;
  pktrade::Quantity outstanding_sell_;

  double outstanding_buy_not_;
  double outstanding_sell_not_;
  // Similar to outstanding_buy/sell, but only for IOC and market orders.
  // That way, we can differentiate with crossers that scalpout and do combo
  // orders and stuff.
  pktrade::Quantity outstanding_buy_cx_;
  pktrade::Quantity outstanding_sell_cx_;

  // At the least, I'm going to use this to log when getting execs.
  // This shadows the actual orders.
  std::unordered_map<PKOrderId, OrdAnnot> open_ord_annotations_;

  // For historical.
  // For fill rate calc.
  Quantity tot_sent_;
  // Shs sent but precog tells us we could not have gotten.
  double tot_precog_missed_;
  pktrade::Quantity tot_traded_;
  double tot_notional_traded_;
  int times_traded_;
  int pos_dir_strict_; // for counting flips: starts 0, but once set can only be 1 or -1
  int times_flipped_;

  // These two are for tracking "crossing fillrate".
  // Note that this is not quite correct: an add-liq order
  // can still remove shares if there is no ALO option.
  // However, this is probably good enough for us to track our
  // native crossing fillrate. Because otherwise, passive_out would
  // distort our notion of crossingfillrate.
  double ioc_shs_sent_;
  double rmliq_shs_;

  std::set<PKOrderId> traded_ords_;

  double min_pnl_seen_;

  // What our pnl is measured in (eg. USDT)
  BaseCurrency base_currency_;

  // Total pnl closed.
  double commissions_;

  // For tracking funding.
  bool use_funding_;
  // For tracking funding per symbol.
  double net_funding_;
  // Track funding bsased on the last time we got a funding event.
  int64_t last_funding_t_;
  int64_t next_funding_t_;
  bool started_funding_calcs_ = false;

  // aka minFV
  double min_pnl_;
  // How much we increment each time we hit minfv.
  double min_pnl_incr_;
  int num_fv_;
  int fv_limit_;

  ///////////////////////////////////////////////////
  // For pnl calculation v2.
  // Fewer mistakes.

  // This is running notional in terms of spend. Meaning, when we buy, we are at
  // a balance of negative base_currency. When we sell, we add in base_currency.
  // The closed_pnl_ is just the difference between this running_notional, and
  // the amount we would need to sell the position at the last price we entered
  // at.
  double running_notional_basecur_;
  double v2_closed_pnl_;
  double highest_closed_pnl_;

  // For tracking what our avg exec price is of the current position we're in.
  double avg_entry_px_;
  // Not used for pnl, just used for tracking avg exec price.
  // It was easy for me to make mistakes with doing this fifo thing for
  // calculating pnl. But here, I'm just going to use it for tracking state that
  // other guys can refer to, so we can have a little bit of fudge.
  double tot_entry_notional_;
  // Actual closed pnl, using average cost accounting (not FIFO or LIFO).
  double v3_closed_pnl_;

  ///////////////////////////////////////////////////

  // If we hit a minfv, how long are we out (in ms).
  int64_t timeout_minfv_;

  Commissioner commissioner_;

  // If we can trade away from flat (in TL0)
  bool can_increase_risk_;

  // If we can trade at all
  bool can_trade_pk_;

  // Approx mid price for sanity checking order prices
  double approx_mid_ = 0;

  // If we hit
  int64_t minfv_t_;

  // Some object here will connect to either:
  // 1 - the live market
  // 2 - the simulator.

  // However, we have the option of using a localcontext too.
  LiveContext* live_context_ = nullptr;
  LocalContext* local_context_ = nullptr;

  // Some container for the list of executions that we have.

  std::set<PKOrderId> live_order_ids_;

  // If true, will subscribe to traded get precog_misses.
  bool precog_miss_;
  // If we turn precog on and we hear a lvldelete at or below this level, then
  // we obviously missed.
  // This is in milliseconds.
  int precog_fastest_possible_rtt_;

  std::map<PKOrderId, PrecogOrdInfo> precogged_buys_;
  std::map<PKOrderId, PrecogOrdInfo> precogged_sells_;

  pktrade::Quantity outstanding_buy_premisses_;
  pktrade::Quantity outstanding_sell_premisses_;

  double precog_penalty_ratio_;

  int num_buy_premisses_;
  int num_sell_premisses_;

  // Some handle on the outputs (trades, orders, etc. files that we'll write
  // to). Maybe we do the inefficient thing first and just write it poorly. IDK.

  // If live, there's some connection to gateway I guess.
  // Let's figure that out when we get there.

  // If sim, then I should contain some simulators.
  // These are per symbol, you should know.
  // Maybe the sim_verse should just be in the gv.

  // Bleed detector: detects sustained adverse selection by tracking
  // win/loss rate on position closes. When most recent closes are
  // losses, widens spreads or stops quoting.
  //
  // How it works:
  //   On each exec that reduces position, compute realized PnL since
  //   last close. Score as +1 (win), -1 (loss), or 0 (inside deadzone).
  //   Feed nonzero scores into an EMS with decay per close event.
  //   Normalize to [-1, 1] where -1 = all losses, +1 = all wins.
  //
  // Config ("bleed_detector" in risk conf):
  //   tdc:                   decay constant in # of closes. Controls memory.
  //                          10 means ~last 10 closes dominate the score.
  //   deadzone_thresh:       min |realized pnl / close notional| to count
  //                          as win/loss. 0.0001 = 1bp, 0.001 = 10bp.
  //   bleed_score_widen:     normalized score to trigger spread widening.
  //                          -0.6 ≈ "60% of recent closes were losses."
  //   bleed_score_stop:      normalized score to stop quoting entirely.
  //                          -0.8 ≈ "80% of recent closes were losses."
  //   bleed_widen_thresh_mult: place_thresh multiplier when widening (e.g. 2.0).
  //
  // Recovery:
  //   WIDEN: auto-recovers when normalized score > 0.
  //   STOP: resumes after bleed_stop_timeout_s seconds via TL0.
  //     Score is NOT reset on resume, so if the adversary is still
  //     there, the first losing close will re-trigger STOP immediately.
  bool use_bleed_detector_ = false;
  double bleed_score_ = 0;
  double bleed_tdc_ = 10;
  double bleed_max_ = 1; // normalization: 1/(1-exp(-1/tdc))
  double bleed_deadzone_thresh_ = 0;
  int bleed_n_ = 0; // close count (warmup: no action until n > tdc)
  double bleed_prev_v3_closed_pnl_ = 0;

  double bleed_score_widen_ = -0.6;
  double bleed_score_stop_ = -0.8;
  double bleed_widen_thresh_mult_ = 2.0;
  int64_t bleed_stop_timeout_s_ = 1800; // 30 min default
  bool bleed_widened_ = false;
  bool bleed_stopped_ = false;

  void bleedRecovery();

  // Double-down detector: the optimistic sibling of the bleed detector.
  // Detects sustained, consistent, realized positive edge from two-sided
  // market making and steps size up via a discrete mult ladder
  // (mult = step_mult^level, capped at max_mult). Size reverts toward
  // baseline when the pattern dissipates — decay-driven, no timeout.
  // Mutually exclusive with the bleed detector (config throws if both).
  //
  // Score: sum-form wall-clock EMS of per-close realized pnl dollars,
  //   normalized by a same-decay EMS of close notional -> notional-weighted
  //   average edge in bps over the window (magnitude-aware: a few big wins
  //   outweigh many small losses). Computed on two horizons (long ~25min,
  //   short ~7min); both must clear enter_edge_bps to step up (consistency).
  //
  // Gates (all must pass to step UP):
  //   churn:       own traded notional EMS > min_churn_ratio * |pos notional|
  //                (we're churning both sides, not riding a position)
  //   closed_frac: |pnl_ems| / (|pnl_ems| + |open pnl|) >= min_closed_frac
  //                (edge is realized, not open-position drift; 0 disables)
  //   mkt_frac:    own traded notional EMS < max_mkt_frac * market traded
  //                notional EMS (we're a small part of the flow; needs Trd
  //                subscription; 0 disables). NOTE: live market prints
  //                include our own fills; sim replayed prints do not, so
  //                this gate is slightly stricter in sim.
  //
  // Ladder: step up one level after dwell_s at the current level with
  //   score+gates passing. Step down one level per eval period when the
  //   long edge < exit threshold (exit_edge_frac * enter_edge_bps) or the
  //   activity floor fails (decayed-to-now
  //   close-notional EMS < min_hold_ntl_frac * base_max_notional_ — needed
  //   because the edge is a ratio of same-decay EMSs, hence frozen when
  //   fills stop). Immediate full revert to 1x: TL != TL0, long edge < 0, or
  //   (if revert_on_short_negative) short edge < 0.
  //
  // Evaluated on every position-reducing exec AND a periodic timer
  // (eval_period_s) so decay-driven step-downs happen without fills.
  //
  // Config ("double_down" in risk conf): enter_edge_bps required, the rest
  // optional with the defaults below (times in seconds in the config).
  bool use_double_down_ = false;
  // Horizons (ms).
  double dd_long_tdc_ms_ = 25.0 * 60 * 1000;
  double dd_short_tdc_ms_ = 7.0 * 60 * 1000;
  // Score EMS (sum-form, wall-clock decay; anchored at dd_close_last_t_).
  double dd_pnl_long_ems_ = 0;
  double dd_ntl_long_ems_ = 0;
  double dd_pnl_short_ems_ = 0;
  double dd_ntl_short_ems_ = 0;
  int64_t dd_close_last_t_ = 0;
  double dd_prev_v3_closed_pnl_ = 0; // own anchor, independent of bleed's
  int dd_closes_ = 0;
  int64_t dd_first_close_t_ = 0;
  // Gate EMS (long tdc).
  double dd_own_ntl_ems_ = 0; // own exec notional
  int64_t dd_own_last_t_ = 0;
  double dd_mkt_ntl_ems_ = 0; // market trade notional
  int64_t dd_mkt_last_t_ = 0;
  // Thresholds / config.
  double dd_enter_edge_bps_ = 1.0;
  double dd_exit_edge_frac_ = 0.5;  // exit threshold as fraction of enter_edge_bps
  double dd_exit_edge_bps_ = 0.5;   // derived: dd_exit_edge_frac_ * dd_enter_edge_bps_
  double dd_step_mult_ = 1.5;
  double dd_max_mult_ = 3.0;
  int64_t dd_dwell_ms_ = 5 * 60 * 1000;
  int dd_warmup_closes_ = 20;
  int64_t dd_warmup_ms_ = 15 * 60 * 1000;
  double dd_min_churn_ratio_ = 5.0;
  double dd_min_closed_frac_ = 0.5;    // 0 disables
  double dd_max_mkt_frac_ = 0.05;      // 0 disables
  double dd_min_hold_ntl_frac_ = 0.25; // of base_max_notional_
  int64_t dd_eval_period_s_ = 30;
  // If set, a negative short-horizon edge triggers an immediate full revert
  // to 1x (not just a one-level step down). Reacts faster than the long-edge
  // revert to a good regime ending abruptly, at the cost of more churn.
  bool dd_revert_on_short_negative_ = false;
  // Runtime state.
  int dd_level_ = 0;
  double dd_size_mult_ = 1.0; // the DoubleDown MultSource slot (see effSizeMult)
  int64_t dd_last_level_change_t_ = 0;
  bool dd_timer_started_ = false;

  void evaluateDoubleDown();
  void setDoubleDownLevel(int level, const std::string& why);
  void doubleDownTimerLoop();

  // Periodically go through the stored iocs, and clean them up if needed.
  int64_t CLEAN_IOC_LIFETIME_MS_ = 1000 * 10;
  // Clean it every 1 mins.
  int64_t CLEAN_IOC_PERIOD_S_ = 60 * 1;
  bool started_clean_iocs_ = false;
};

} // namespace pktrade::risk
