#include "global_vars.h"

namespace pktrade {
// All these need to get instantiated before
pktrade::EventLoop* GlobalVar::event_loop_ = nullptr;
pktrade::md::MDBeacon* GlobalVar::md_beacon_ = nullptr;
pktrade::BookManager* GlobalVar::book_manager_ = nullptr;

// Factories
pktrade::tempos::TempoFactory* GlobalVar::tf_ = nullptr;
pktrade::signals::SignalFactory* GlobalVar::sf_ = nullptr;
pktrade::ordex::OrdexFactory* GlobalVar::of_ = nullptr;

std::string GlobalVar::date_ = "";

Clock::time_point GlobalVar::start_t_;
Clock::time_point GlobalVar::end_t_;

pktrade::TradeCarrier* GlobalVar::trade_carrier_ = nullptr;
pktrade::SecMaster* GlobalVar::secmaster_ = nullptr;
pktrade::FundingMaster* GlobalVar::funding_master_ = nullptr;

bool GlobalVar::live_ = false;
pktrade::LiveContext* GlobalVar::live_context_ = nullptr;

bool GlobalVar::use_local_context_ = false;
pktrade::LocalContext* GlobalVar::local_context_ = nullptr;

bool GlobalVar::paper_trading_mode_ = false;

pktrade::sim::SimVerse* GlobalVar::sim_verse_ = nullptr;

std::string GlobalVar::strat_id_ = "";

bool GlobalVar::hl_testnet_ = false;
std::string GlobalVar::hl_address_ = "";
bool GlobalVar::vault_ = false;
bool GlobalVar::ignore_usermsgs_ = false;

pktrade::Market GlobalVar::slowdown_market_ = pktrade::Market::Unknown;
int64_t GlobalVar::slowdown_market_offset_ = 0;

std::string GlobalVar::acct_path_ = "";
std::ofstream* GlobalVar::trades_out_ = nullptr;
std::ofstream* GlobalVar::orders_out_ = nullptr;
std::ofstream* GlobalVar::logs_out_ = nullptr;
std::string GlobalVar::out_dir_ = "";

bool GlobalVar::verbose_ = false;

} // namespace pktrade
