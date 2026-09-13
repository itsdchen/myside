#include "pktrader.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/enums/basic_enums.h"
#include "pktrade/risk/trade_risk_man.h"
#include "pktrade/signals/signal.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"

namespace pktrade {

PKTrader::PKTrader(rapidjson::Value& pkt_conf, rapidjson::Value& comms_conf,
                   std::string strat_label)
    : strat_label_(strat_label) {
  // Try to do the appropriate... creation.
  pktrade::GlobalVar::tf_->makeAllTempos(pkt_conf["tempos"].GetArray());
  pktrade::GlobalVar::sf_->makeAllSignals(pkt_conf["signals"].GetArray());

  symbol_ = SymbolId{pkt_conf["traded_symbol"].GetString()};

  // Create the ordex.
  // Later, create diff ordexes.
  ordex_ = pktrade::GlobalVar::of_->makeByConf(symbol_, pkt_conf["ordex"], pktrade::GlobalVar::sf_,
                                               pktrade::GlobalVar::tf_);

  // Make the riskman.

  pktrade::Market traded_mkt = ordex_->getMarkets()[0];

  riskman_ = new pktrade::risk::TradeRiskMan(pkt_conf["traded_symbol"].GetString(), traded_mkt,
                                             pkt_conf["risk"], comms_conf, pktrade::GlobalVar::sf_,
                                             ordex_);

  // Tell the ordex about riskman
  ordex_->setRiskMan(riskman_);

  // Tell the riskman about the signals.
  riskman_->registerPred(ordex_->getPredSignal());
  riskman_->registerMid(ordex_->getMidSignal());

  // For extar subscriptions...
  if (pkt_conf.HasMember("extra_subs")) {
    for (const rapidjson::Value& extra_sub : pkt_conf["extra_subs"].GetArray()) {
      std::string mkt_str_val = extra_sub["market"].GetString();
      std::string sym_str = extra_sub["symbol"].GetString();

      extra_subs_.push_back(pktrade::util::make_bookid(mkt_str_val, sym_str));
    }
  }

  if (pkt_conf.HasMember("tl1_secs")) {
    tl1_secs = pkt_conf["tl1_secs"].GetInt();
  }
  if (pkt_conf.HasMember("tl2_secs")) {
    tl2_secs = pkt_conf["tl2_secs"].GetInt();
  }
  if (pkt_conf.HasMember("tl5_secs")) {
    tl5_secs = pkt_conf["tl5_secs"].GetInt();
  }
  // Make sure that tl5_secs < tl2_secs < tl1_secs <= 900
  if (tl5_secs >= tl2_secs) {
    throw std::runtime_error("EOD TL2 must start before TL5!");
  } else if (tl2_secs >= tl1_secs) {
    throw std::runtime_error("EOD TL1 must start before TL2!");
  } else if (tl1_secs > 900) {
    throw std::runtime_error("EOD TL1 period is suspiciously long!");
  }

  // Multiply maxpos, if we have it.
  double size_mult = 1;
  if (pkt_conf.HasMember("size_mult")) {
    size_mult = pkt_conf["size_mult"].GetDouble();

    if (size_mult <= 0) {
      LOG(WARNING) << "Invalid size_mult " << size_mult << "; ignoring.";
    } else if (size_mult != 1) {
      initial_size_mult_ = size_mult;

      // Update the riskman
      riskman_->setSizeMult(size_mult, MultSource::Conf);
    }
  }
  // Call onDayStart stuff.

  // Slightly different setting for live mode.
  if (pktrade::GlobalVar::live_ &&
      clock_cast<Clock>(std::chrono::system_clock::now()) > pktrade::GlobalVar::start_t_) {
    LOG(INFO) << "Starting live trading in 30s, since started after the "
                 "config's start time\n";
    pktrade::GlobalVar::event_loop_->onTimeout(
        clock_cast<Clock>(std::chrono::system_clock::now()) + std::chrono::seconds(30), [&] {
          LOG(INFO) << "Pktrader for " << this->symbol_ << " is calling onDayStart\n";
          this->onDayStart();
        });

  } else {
    pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_, [&] {
      LOG(INFO) << "Day start. Starting trading\n";
      this->onDayStart();
    });
  }

  // TL1 tl1_secs seconds before EOD (default 10min).
  pktrade::GlobalVar::event_loop_->onTimeout(
      pktrade::GlobalVar::end_t_ - std::chrono::seconds{tl1_secs}, [&] {
        this->riskman_->setTL(pktrade::risk::TradingLevel::TL1, pktrade::risk::TLReason::Normal);
      });

  // TL2 tl2_secs seconds before EOD (default 3min).
  pktrade::GlobalVar::event_loop_->onTimeout(
      pktrade::GlobalVar::end_t_ - std::chrono::seconds{tl2_secs}, [&] {
        this->riskman_->setTL(pktrade::risk::TradingLevel::TL2, pktrade::risk::TLReason::Normal);
      });

  // TL5 tl5_secs seconds before EOD (default 10s)..
  pktrade::GlobalVar::event_loop_->onTimeout(
      pktrade::GlobalVar::end_t_ - std::chrono::seconds{tl5_secs}, [&] {
        this->riskman_->setTL(pktrade::risk::TradingLevel::TL5, pktrade::risk::TLReason::Normal);
      });
}

Market PKTrader::getTradedMarket() { return ordex_->getMarkets()[0]; }

std::vector<BookId> PKTrader::getExtraSubs() {
  // Get it from its ordex.
  // Maybe at some point will have more.
  return extra_subs_;
}

std::vector<BookId> PKTrader::getTradingSubs() {
  // Get it from its ordex.
  // Maybe at some point will have more.
  return ordex_->getBookIds();
}

// Called in sim_mode.
void PKTrader::createSimulators(rapidjson::Value& sim_conf) {
  // Create simulators for the simverse.
  for (BookId bid : ordex_->getBookIds()) {
    // Create the simverse.
    pktrade::GlobalVar::sim_verse_->addTradedSymbol(bid, riskman_, sim_conf);
  }
}

void PKTrader::subscribe() {
  // Subscribe the traderiskman.
  riskman_->subscribe();
}

//         "sym,net_pnl,currency,closed_pnl,commission,shs_traded,shs_sent,fill_rate,open_"
//        "pos";

void PKTrader::postSecmaster() {
  // give TRM a postsecmaster too.
  // So it can update the positions to the funding master.
  // This ordering of events is going to be confusing soon.

  riskman_->postSecMaster();

  ordex_->postSecMaster();

  // Apply the initial ordex size multiplier only after postSecMaster, because
  // some ordex implementations now use SecMaster lot-size metadata in setSizeMult().
  if (initial_size_mult_ != 1) {
    ordex_->setSizeMult(initial_size_mult_, MultSource::Conf);
  }

  // TODO: move this into ondaystart later.
  // Just... tell them that we want funding for these markets.
  if (riskman_->getUseFunding()) {
    // Go through all markets and start requesting.
    for (const auto& market : ordex_->getMarkets()) {
      pktrade::GlobalVar::funding_master_->wants_funding(market);
    }
  }
}

const std::string PKTrader::getAcctLine() const {
  double fill_rate = 0;
  if (riskman_->getShsSent() > 0) {
    fill_rate = riskman_->getShsTraded() / riskman_->getShsSent();
  }
  double reachable_fill_rate = 0;
  if (riskman_->getReachableShsSent() > 0) {
    reachable_fill_rate = riskman_->getShsTraded() / riskman_->getReachableShsSent();
  }

  double ioc_fill_rate = 0;
  if (riskman_->getIOCShsSent() > 0) {
    ioc_fill_rate = riskman_->getRmLiqShs() / riskman_->getIOCShsSent();
  }

  std::string acct_line = "";
  // We want the date, right?
  acct_line += pktrade::GlobalVar::date_ + ",";
  acct_line += symbol_.get() + ",";

  // Fill rates are typically 0.001-0.05 (0.1%-5%); 6dp gives 1ppm precision.
  // Prior 2dp truncated all sub-1% fill rates to 0.00 in the acct file,
  // forcing downstream tools like pm_daily_timeline to recompute from
  // shs_traded / shs_sent.
  acct_line += fmt::format(
      "{:.2f},{:.2f},{:.2f},{:.2f},{:.2f},{:.3f},{:.3f},{:.6f},{:.6f},{:.6f},{:d},{:d}"
      ",{:.3f}"
      "\n",
      riskman_->getV2NetPnl(), riskman_->getV2ClosedPnl(), riskman_->getCommissions(),
      riskman_->getFunding(), riskman_->getMinPnlSeen(), riskman_->getShsTraded(),
      riskman_->getShsSent(), fill_rate, reachable_fill_rate, ioc_fill_rate,
      riskman_->getTimesTraded(), riskman_->getTimesFlipped(), riskman_->getPos().toDouble());
  return acct_line;
}

void PKTrader::onDayStart() {
  printf("%s: PKTrader::onDayStart. Setting trade validity on\n", time_utils::nowToStr().c_str());
  // Tell the TRM.
  riskman_->onDayStart();
}

void PKTrader::handleUM(const UserMsg& um) {
  if (um.sym.get() != "" && um.sym.get() != symbol_.get()) {
    LOG(INFO) << "Ignoring UM not targeting sym " << symbol_;
    return;
  }

  // Parse arguments in JSON format
  rapidjson::Document json_args;
  if (!um.args.empty()) {
    json_args.Parse(um.args.c_str());
    if (json_args.HasParseError()) {
      LOG(ERROR) << "Failed to parse usermsg arguments: " << um.args;
      return;
    }
  } else {
    json_args.SetObject();
  }

  LOG(INFO) << "Processing UM for " << GlobalVar::strat_id_ << " trading " << symbol_ << ": "
            << um.toString();

  switch (static_cast<UserMsgId>(um.um_id)) {
    case UserMsgId::SET_PLACE_THRESH: {
      // args should be: {thresh:double}
      if (!json_args.HasMember("thresh") || !json_args["thresh"].IsNumber()) {
        LOG(ERROR) << "Failed to parse SET_PLACE_THRESH usermsg args: " << um.args;
        return;
      }
      // Check: this UM should only ever be sent to a single strat/sym
      if (!um.isSingular()) {
        LOG(ERROR) << "UM SET_PLACE_THRESH sent to multiple targets strat_id ("
                   << um.strat_id_regex_str << ") sym (" << um.sym << "). Ignoring.";
        return;
      }
      double thresh = json_args["thresh"].GetDouble();
      LOG(INFO) << "UM: Setting place thresh to " << thresh;
      ordex_->setThresh(thresh);
      break;
    }

    case UserMsgId::SET_THRESH_MULT: {
      // args should be: {mult:double}
      if (!json_args.HasMember("mult") || !json_args["mult"].IsNumber()) {
        LOG(ERROR) << "Failed to parse SET_THRESH_MULT usermsg args: " << um.args;
        return;
      }
      double mult = json_args["mult"].GetDouble();
      // mult is an absolute multiplier for the usermsg slot. The widest slot
      // wins, so an active bleed/minfv widen can still override a tightening.
      LOG(INFO) << "UM: Setting usermsg place thresh multiplier (relative to base) to " << mult;
      ordex_->setThreshMult(mult, MultSource::Usermsg);
      break;
    }

    case UserMsgId::SET_ORDER_SIZE: {
      // args should be: {size:double}
      if (!json_args.HasMember("size") || !json_args["size"].IsNumber()) {
        LOG(ERROR) << "Failed to parse SET_ORDER_SIZE usermsg args: " << um.args;
        return;
      }
      // Check: this UM should only ever be sent to a single strat/sym
      if (!um.isSingular()) {
        LOG(ERROR) << "UM SET_ORDER_SIZE sent to multiple targets strat_id ("
                   << um.strat_id_regex_str << ") sym (" << um.sym << "). Ignoring.";
        return;
      }
      double size = json_args["size"].GetDouble();
      LOG(INFO) << "UM: Setting order size to " << size << " (not changing max pos)";
      ordex_->setOrderSize(size);
      break;
    }

    case UserMsgId::SET_SIZE_MULT: {
      // args should be: {mult:double}
      if (!json_args.HasMember("mult") || !json_args["mult"].IsNumber()) {
        LOG(ERROR) << "Failed to parse SET_SIZE_MULT usermsg args: " << um.args;
        return;
      }
      double mult = json_args["mult"].GetDouble();
      if (mult <= 0) {
        LOG(ERROR) << "SET_SIZE_MULT: non-positive mult " << mult << " not allowed. Ignoring.";
        return;
      }
      // mult is an absolute multiplier for the usermsg slot (not compounding).
      // It stacks multiplicatively with the conf's size_mult (and any active
      // double-down mult), so a blanket mult scales each trader relative to its
      // configured size.
      LOG(INFO) << "UM: Setting usermsg order size multiplier (relative to base) to " << mult;
      riskman_->setSizeMult(mult, MultSource::Usermsg);
      ordex_->setSizeMult(mult, MultSource::Usermsg);
      break;
    }

    case UserMsgId::SET_RELATIVE_BETA: {
      // args should be: {beta:double}
      if (!json_args.HasMember("beta") || !json_args["beta"].IsNumber()) {
        LOG(ERROR) << "Failed to parse SET_RELATIVE_BETA usermsg args: " << um.args;
        return;
      }
      double beta = json_args["beta"].GetDouble();
      LOG(INFO) << "UM: Setting relative beta to " << beta;
      ordex_->setRelativeBeta(beta);
      break;
    }

    case UserMsgId::SET_EXTRA_THRESHES: {
      // args should be: {buy:double, sell:double}
      if (!json_args.HasMember("buy") || !json_args["buy"].IsNumber() ||
          !json_args.HasMember("sell") || !json_args["sell"].IsNumber()) {
        LOG(ERROR) << "Failed to parse SET_EXTRA_THRESH usermsg args: " << um.args;
        return;
      }
      double extra_buy = json_args["buy"].GetDouble();
      double extra_sell = json_args["sell"].GetDouble();
      LOG(INFO) << "UM: Setting extra threshes to " << extra_buy << " and " << extra_sell;
      ordex_->setExtraThreshes(extra_buy, extra_sell);
      break;
    }

    case UserMsgId::SET_CONST_PRED_PX: {
      // args should be: {px:double}
      if (!json_args.HasMember("px") || !json_args["px"].IsNumber()) {
        LOG(ERROR) << "Failed to parse SET_CONST_PRED_PX usermsg args: " << um.args;
        return;
      }
      double px = json_args["px"].GetDouble();
      LOG(INFO) << "UM: Setting const_pred_px to " << px;
      ordex_->setConstPredPx(px);
      break;
    }

    case UserMsgId::FORCE_INHERIT: {
      // args should be: {pos:double}
      if (!json_args.HasMember("pos") || !json_args["pos"].IsNumber()) {
        LOG(ERROR) << "Failed to parse FORCE_INHERIT usermsg args: " << um.args;
        return;
      }
      double pos = json_args["pos"].GetDouble();
      LOG(INFO) << "UM: Forcing inheritance of " << pos;
      riskman_->addInheritPosition(pos);
      break;
    }

    case UserMsgId::FORCE_INHERIT_LOOP: {
      LOG(INFO) << "UM: Forcing inherit loop";
      riskman_->forceInheritLoop();
      break;
    }

    case UserMsgId::SET_TL: {
      // args should be: {tl:int}
      if (!json_args.HasMember("tl") || !json_args["tl"].IsInt()) {
        LOG(ERROR) << "Failed to parse SET_TL usermsg args: " << um.args;
        return;
      }
      int tl_int = json_args["tl"].GetInt();
      int max_valid_tl_int =
          static_cast<int>(magic_enum::enum_values<pktrade::risk::TradingLevel>().back());
      if (tl_int < 0 || tl_int > max_valid_tl_int) {
        LOG(ERROR) << "Invalid TL" << tl_int;
        return;
      }
      LOG(INFO) << "UM: Setting TL" << tl_int;
      riskman_->setTL(static_cast<pktrade::risk::TradingLevel>(tl_int));
      break;
    }

    case UserMsgId::SET_TL_REASON: {
      // args should be: {tl:int,reason:string}
      if (!json_args.HasMember("tl") || !json_args["tl"].IsInt() ||
          !json_args.HasMember("reason") || !json_args["reason"].IsString()) {
        LOG(ERROR) << "Failed to parse SET_TL_REASON usermsg args: " << um.args;
        return;
      }
      int tl_int = json_args["tl"].GetInt();
      std::string reason_str = json_args["reason"].GetString();
      auto reason = magic_enum::enum_cast<pktrade::risk::TLReason>(reason_str);
      int max_valid_tl_int =
          static_cast<int>(magic_enum::enum_values<pktrade::risk::TradingLevel>().back());
      if (tl_int < 0 || tl_int > max_valid_tl_int || !reason.has_value()) {
        LOG(ERROR) << "Invalid TL" << tl_int << " or TLReason " << reason_str;
        return;
      }
      LOG(INFO) << "UM: Setting " << reason_str << " TL" << tl_int;
      riskman_->setTL(static_cast<pktrade::risk::TradingLevel>(tl_int), reason.value());
      break;
    }

    default:
      LOG(ERROR) << "Unrecognized UserMsgId: " << um.um_id;
  }
}

} // namespace pktrade
