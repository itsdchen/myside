
#include "pktrade/event_loop.h"
#include "pktrader.h"
#include "trade_carrier.h"
#include "version.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/sim/sim_verse.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"

#include <rapidjson/document.h>
#include <rapidjson/filereadstream.h>

#include <csignal>
#include <fcntl.h>
#include <filesystem>
#include <fmt/format.h>

#include <glog/logging.h>
#include <memory>
#include <stdio.h>

// For signal handling...
#include <csignal>

using namespace pktrade;

// Helper function to backup existing files with numeric postscripts
// If file exists, rename it to file.1 (or .2, .3, etc. if those already exist)
void backup_existing_file(const std::filesystem::path& filepath) {
  if (!std::filesystem::exists(filepath)) {
    return;
  }

  // If it's /dev/null, don't do it, we'll have issues.
  if (filepath == "/dev/null") {
    return;
  }

  // Find the next available postscript number
  int postscript = 1;
  std::filesystem::path backup_path;
  do {
    backup_path = filepath.string() + "." + std::to_string(postscript);
    postscript++;
  } while (std::filesystem::exists(backup_path));

  // Rename the existing file to the backup path
  std::error_code ec;
  std::filesystem::rename(filepath, backup_path, ec);
  if (ec) {
    std::cerr << "Warning: Failed to backup existing file " << filepath << " to " << backup_path
              << ": " << ec.message() << "\n";
  } else {
    std::cout << "Backed up existing file: " << filepath << " -> " << backup_path << "\n";
  }
}

/*
Example cmdline:

SIM:
../../bin/pktrade --date 20230103 --conf pktrade.json  --acct acct.csv

LIVE:
./pktrade --conf sui_pk.json --live --strat-id "sui-trader" --trd
trds.today.csv --ord ords.today.csv --date "20230626"

./pktrade --conf sui_pk.json --live --out-dir . --strat-id "sui-trader"
--trd trds.today.csv --ord ords.today.csv --date "20230627" 2>&1
| tee out.today.txt


*/

// Global cleanup function

void handle_signal(int signum) {
  LOG(ERROR) << "Signal " << signum << " received, cleaning up...";

  // Go through the tradecarrier and emergencycancel everything.
  pktrade::GlobalVar::trade_carrier_->emergencyEOD();

  // Stop the event loop so we go through the rest of standard EOD
  pktrade::GlobalVar::event_loop_->stop();
}

int main(int argc, char** argv) {
  // Ignore SIGPIPE before constructing any async network clients. Some libraries
  // may snapshot/restore the current disposition around socket writes; starting
  // with SIG_IGN prevents a later stale TLS/socket write from terminating live
  // trading with exit 141.
  signal(SIGPIPE, SIG_IGN);

  // printf("Running cmd:\n");
  // for (int i = 0; i < argc; i++) {
  //   printf("%s ", argv[i]);
  //  }
  // printf("\n");

  // Basic cmdline args.

  std::string date = "";
  std::string start_date = "";
  std::string end_date = "";
  std::string conf_path;

  // If live, then we use the live feed.
  bool live = false;
  // Can only be true if live is set. And if this is also true, then instead of
  // sending orders to the gateway it constructs a sim_verse and
  bool paper_trading_mode = false;

  // If sim, we need a datadir.
  // tbh, possible to set this in conffile too.
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";

  // Suffix for TopBookEquity dir; e.g. "Boats" loads from TopBookEquity_Boats/.
  std::string equity_variant;

  // Only necessary if live, but if live, is necessary.
  std::string strat_id = "SIM";

  // For outputs: logging, tradesfiles, orders files.
  std::string out_dir;

  std::string trades_file = "";
  // Ords are going to be in a csv format, even though
  std::string orders_file = "";
  std::string logs_file = "";
  std::string acct_file = "";

  // If given and in live mode, we'll run a local_context instead of a
  // live_context.
  std::string local_risk_file = "";

  bool verbose = false;

  // Relative to the output_dir.
  // If full_output, then will output orders, trades, and logs.
  // Otherwise, will just output the accounting.
  bool full_output = false;

  bool dry_run = false;

  bool beacon_always_deliver = false;
  // Only the --testnet flag is actually used, but I want users to be very explicit.
  bool testnet = false;
  bool mainnet = false;

  // Must be specified iff running for vault
  bool vault = false;

  bool ignore_usermsgs = false;

  std::string slowdown_market = "";
  int64_t slowdown_market_offset = 0;

  // If given, will sleep this many secs before starting.
  // That way, we can wait for the gateway to be ready.
  int init_sleep_secs = 0;

  bool log_in_sim = false;

  std::string global_inherit_path = "";
  std::string global_usermsg_path = "";

  CLI::App cmd_flags("pktrader");
  cmd_flags.add_option("-d,--date", date, "Date")->required();

  cmd_flags.add_option("-f,--conf", conf_path, "Conf Path")->required();
  cmd_flags.add_flag("--live", live, "True iff live");
  cmd_flags.add_flag("--paper", paper_trading_mode, "True iff paper trading");
  cmd_flags.add_option("--local", local_risk_file,
                       "Conf Path for localrisk. If given, will send orders to "
                       "LocalContext and not a zmq gateway.");

  cmd_flags.add_option("--datadir", data_dir, "Data Dir");
  cmd_flags.add_option("--equity-variant", equity_variant,
                       "Suffix for TopBookEquity dir (e.g. 'Boats' loads from TopBookEquity_Boats/)");

  cmd_flags.add_option("--init-sleep-secs", init_sleep_secs, "Iniitla sleep seconds");
  // If sim, defaults to "SIM"
  cmd_flags.add_option("-i,--strat-id", strat_id, "Strat ID");

  // If sim, defaults to the current dir.
  cmd_flags.add_option("--out-dir", out_dir, "Out Dir");

  // Do all this eventually.
  // For now, just deal with the acct-file.
  cmd_flags.add_option("--trd,-x", trades_file, "Trades file name");
  cmd_flags.add_option("--ord,-o", orders_file, "Orders file name");
  cmd_flags.add_option("--log,-l", logs_file, "Log file name");
  cmd_flags.add_option("--acct,-a", acct_file, "Accounting file name");
  cmd_flags.add_flag("-v,--verbose", verbose, "Verbose logging");
  cmd_flags.add_flag("--log-in-sim", log_in_sim, "Log even in sim");
  cmd_flags.add_flag("--beacon-always-deliver", beacon_always_deliver, "mdbeacon always delivers");
  cmd_flags.add_flag("--testnet", testnet, "hyperliquid testnet");
  cmd_flags.add_flag("--mainnet", mainnet, "hyperliquid mainnet");
  cmd_flags.add_flag("--vault", vault, "Running as a vault query");

  cmd_flags.add_flag("--full-output", full_output,
                     "If enabled, output trades, orders, logs, accounting, to the output-dir");

  cmd_flags.add_option("--slowdown-market", slowdown_market, "Market to slow down");
  cmd_flags.add_option("--slowdown-market-offset", slowdown_market_offset,
                       "Offset (in ms) for slowdown market");

  // For getting subscription BookIds
  cmd_flags.add_flag("--dry-run", dry_run, "Dry run");

  // For ignoring usermsgs
  cmd_flags.add_flag("--ignore-usermsgs", ignore_usermsgs, "Ignore usermsgs");

  cmd_flags.add_option("--global-inherit-path", global_inherit_path,
                       "Path to inherit schedule CSV for sim replay");

  cmd_flags.add_option("--global-usermsg-path", global_usermsg_path,
                       "Path to usermsg schedule TSV for sim replay");

  PARSE(cmd_flags, argc, argv);
  if (!live && !log_in_sim) {
    // Don't dirty our directories with logging, if in sim (climb) mode.
    FLAGS_minloglevel = 4;
  }

  if (out_dir == "") {
    out_dir = std::filesystem::path(conf_path).parent_path().string();
  }
  FLAGS_log_dir = out_dir;

  google::InitGoogleLogging(argv[0]);

  LOG(INFO) << "pktrade version: " << PKTRADE_GIT_COMMIT
            << " (branch: " << PKTRADE_GIT_BRANCH
            << ", built: " << PKTRADE_BUILD_TIMESTAMP << ")";

  if (init_sleep_secs > 0) {
    LOG(INFO) << "Sleeping for " << init_sleep_secs << " seconds at startup" << std::endl;
    sleep(init_sleep_secs);
  }

  // Do basic sanity checks
  if (!std::filesystem::exists(conf_path)) {
    throw std::runtime_error("Tried to start pktrade with non-existent conffile " + conf_path);
  }

  // Create a dated copy of the config file for record-keeping (only in live mode)
  if (live) {
    std::filesystem::path dated_conf_path = conf_path + "." + date;

    // If a dated copy already exists for today, back it up with .1, .2, etc.
    backup_existing_file(dated_conf_path);

    // Copy the config file to the dated version
    std::error_code ec;
    std::filesystem::copy_file(conf_path, dated_conf_path, ec);
    if (ec) {
      std::cerr << "Warning: Failed to create dated copy of config file: " << ec.message() << "\n";
    } else {
      std::cout << "Created dated config copy: " << dated_conf_path << "\n";
    }
  }

  rapidjson::Document trade_conf = pktrade::util::read_json_file(conf_path);

  if (!trade_conf.IsObject()) {
    throw std::runtime_error("Trade Conf at " + conf_path +
                             " did not process properly. Perhaps it's well-formatted");
  }

  if (live) {
    if (!testnet && !mainnet) {
      throw std::runtime_error("If live, must specify either --testnet or --mainnet");
    }
    if (testnet && mainnet) {
      throw std::runtime_error("Cannot specify both --testnet and --mainnet");
    }
  }
  // Start off with the start- and end- times.
  if (live) {
    // TODO: figure out how to actually get the right date.
    // This goes over to the next day  after like 19:00 or whenever gmt turns
    // to next day.
    // date = time_utils::dateToStr(time_utils::today());
  }
  pktrade::GlobalVar::date_ = date;

  // Got rid of --end-date arg. Just set it to date, and we'll tweak based on start_t/end_t later.
  end_date = date;

  // This can probably exist inside the carrier or something, too.
  std::string start_t_str = trade_conf["settings"]["start_t"].GetString();
  std::string end_t_str = trade_conf["settings"]["end_t"].GetString();

  // Actually, if the start_t_str is between 18:00 and 23:49, then we should
  // consider it the next L1Date (our day rollover period happens at 18:00 every day.)
  // Let's force that.
  start_date = date;

  // Figure out start-, end- times (need to parse first to check timezone).
  std::string start_dt = start_date + " " + start_t_str;
  std::string end_dt = end_date + " " + end_t_str;

  date::zoned_seconds start_t = pktrade::time_utils::dtToSecs(start_dt);
  date::zoned_seconds end_t = pktrade::time_utils::dtToSecs(end_dt);

  // Validate that we're using America/New_York timezone (required for 18:00 ET day rollover logic)
  std::string tz_name = std::string(start_t.get_time_zone()->name());
  if (tz_name != "America/New_York") {
    throw std::runtime_error(
        fmt::format("Day rollover logic requires America/New_York timezone, but conf start_t is "
                    "{}. Fix the conf or time_utils::dtToSecs.",
                    tz_name));
  }
  tz_name = std::string(end_t.get_time_zone()->name());
  if (tz_name != "America/New_York") {
    throw std::runtime_error(
        fmt::format("Day rollover logic requires America/New_York timezone, but conf end_t is "
                    "{}. Fix the conf or time_utils::dtToSecs.",
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

    date::year_month_day ymd = date::year{year} / date::month{month} / date::day{day};
    date::sys_days prev_day = date::sys_days{ymd} - date::days{1};
    date::year_month_day prev_ymd = date::year_month_day{prev_day};

    start_date = fmt::format("{:04d}{:02d}{:02d}", int(prev_ymd.year()), unsigned(prev_ymd.month()),
                             unsigned(prev_ymd.day()));

    // Recreate start_dt and start_t with adjusted start_date
    start_dt = start_date + " " + start_t_str;
    start_t = pktrade::time_utils::dtToSecs(start_dt);
  }

  // Mirror the same 18:00-ET rollover for end_t. A session whose end_t is strictly after
  // 18:00 ET ends on the previous calendar day (e.g. Japan AM 20:00-22:30 ET). The boundary is
  // EXCLUSIVE here (strictly >), unlike start's inclusive >= 18:00: an end at exactly 18:00:00
  // is the close of the current trading day (e.g. postusa 16:00-18:00) and must stay put, or
  // its window would invert. Doing this means callers can always just pass --date <L1Date>.
  auto end_local = end_t.get_local_time();
  auto end_dp = date::floor<date::days>(end_local);
  if (end_local - end_dp > std::chrono::hours(18)) {
    // end_date format is YYYYMMDD
    int year = std::stoi(end_date.substr(0, 4));
    int month = std::stoi(end_date.substr(4, 2));
    int day = std::stoi(end_date.substr(6, 2));

    date::year_month_day ymd = date::year{year} / date::month{month} / date::day{day};
    date::sys_days prev_day = date::sys_days{ymd} - date::days{1};
    date::year_month_day prev_ymd = date::year_month_day{prev_day};

    end_date = fmt::format("{:04d}{:02d}{:02d}", int(prev_ymd.year()), unsigned(prev_ymd.month()),
                           unsigned(prev_ymd.day()));

    // Recreate end_dt and end_t with adjusted end_date
    end_dt = end_date + " " + end_t_str;
    end_t = pktrade::time_utils::dtToSecs(end_dt);
  }

  // Sanity check: a valid session
  //   (1) spans a positive duration no longer than 24h, and
  //   (2) does not (strictly) contain an 18:00-ET rollover.
  // Fail loudly in sim; in live just log so we don't kill live trading.
  // Everything here is ET wall-clock (local) time, so is still valid for DST transitions when
  // physical span might be 25h.
  start_local = start_t.get_local_time();
  end_local = end_t.get_local_time();
  auto span = end_local - start_local;

  // First 18:00-ET instant strictly after start; the window straddles a rollover iff it lands
  // strictly before end.
  date::local_seconds rollover = date::floor<date::days>(start_local) + std::chrono::hours(18);
  if (rollover <= start_local) {
    rollover += date::days{1};
  }
  bool contains_rollover = rollover < end_local;

  if (span <= std::chrono::seconds(0) || span > std::chrono::hours(24) || contains_rollover) {
    auto span_h = std::chrono::duration_cast<std::chrono::hours>(span).count();
    std::string msg = fmt::format(
        "Bad trading window (span {}h, start_dt={}, end_dt={}, contains_1800_rollover={}): "
        "expected 0 < end-start <= 24h with no 18:00-ET rollover strictly inside. Check --date "
        "against the conf's start_t/end_t (there is no --end-date arg).",
        span_h, start_dt, end_dt, contains_rollover);
    if (live) {
      LOG(ERROR) << msg;
    } else {
      throw std::runtime_error(msg);
    }
  }

  // All checks pass so set the actual time points.
  pktrade::GlobalVar::start_t_ = pktrade::time_utils::zsToTp(start_t);
  pktrade::GlobalVar::end_t_ = pktrade::time_utils::zsToTp(end_t);

  pktrade::GlobalVar::strat_id_ = strat_id;

  if (acct_file == "" && full_output) {
    acct_file = fmt::format("acct_{}.csv", date);
  }
  // Outfiles. Logging.
  if (acct_file != "") {
    std::filesystem::path acct_path =
        std::filesystem::path(out_dir) / std::filesystem::path(acct_file);
    backup_existing_file(acct_path);
    pktrade::GlobalVar::acct_path_ = acct_path;

    // pktrade::GlobalVar::acct_out_ = new std::ofstream(acct_path);
  }

  pktrade::GlobalVar::out_dir_ = out_dir;

  if (trades_file == "" && full_output) {
    trades_file = fmt::format("trades_{}.csv", date);
  }
  if (trades_file != "") {
    std::filesystem::path trades_path =
        std::filesystem::path(out_dir) / std::filesystem::path(trades_file);
    backup_existing_file(trades_path);
    pktrade::GlobalVar::trades_out_ = new std::ofstream(trades_path);
    std::cout << "Writing trades to " << trades_path << "\n";
  }

  if (orders_file == "" && full_output) {
    orders_file = fmt::format("orders_{}.csv", date);
  }
  if (orders_file != "") {
    std::filesystem::path orders_path =
        std::filesystem::path(out_dir) / std::filesystem::path(orders_file);
    backup_existing_file(orders_path);
    pktrade::GlobalVar::orders_out_ = new std::ofstream(orders_path);
    std::cout << "Writing orders to " << orders_path << "\n";
  }

  if (logs_file == "" && full_output) {
    logs_file = fmt::format("logs_{}.csv", date);
  }
  if (logs_file != "") {
    std::filesystem::path logs_path =
        std::filesystem::path(out_dir) / std::filesystem::path(logs_file);
    backup_existing_file(logs_path);
    pktrade::GlobalVar::logs_out_ = new std::ofstream(logs_path);
  }

  pktrade::GlobalVar::verbose_ = verbose;

  pktrade::GlobalVar::hl_testnet_ = testnet;
  pktrade::GlobalVar::vault_ = vault;

  // Create the event loop.
  pktrade::GlobalVar::event_loop_ = new pktrade::EventLoop();
  pktrade::GlobalVar::tf_ = new pktrade::tempos::TempoFactory();
  pktrade::GlobalVar::sf_ = new pktrade::signals::SignalFactory();
  pktrade::GlobalVar::of_ = new pktrade::ordex::OrdexFactory();

  pktrade::GlobalVar::live_ = live;
  pktrade::GlobalVar::paper_trading_mode_ = paper_trading_mode;
  if (!live) {
    // Needs to exist before the the carrier.
    pktrade::GlobalVar::sim_verse_ = new pktrade::sim::SimVerse();
  } else {
    // We might still be paper trading.
    if (pktrade::GlobalVar::paper_trading_mode_) {
      std::cout << "Live but in paper trading mode! Creating a sim_verse" << std::endl;
      pktrade::GlobalVar::sim_verse_ = new pktrade::sim::SimVerse();
    } else {
      std::cout << "Trading in full live mode" << std::endl;

      // NOTE (20241218):
      // I want to be very clear here. We can enter a mode
      // where we have both a live_context and a local_context, because
      // all our Hyperliquid order entry goes through Pygateway.
      // This means that IF we are in use_local_context_ mode, we STILL
      // need to check if we want the live_context as well.

      if (local_risk_file == "") {
        // Sending orders to gateway.
        std::cout << "Running live mode with gateway, connecting." << std::endl;
        pktrade::GlobalVar::live_context_ = new pktrade::LiveContext();
      } else {
        std::cout << "Trading with LocalContext, risk file at " << local_risk_file << std::endl;
        pktrade::GlobalVar::use_local_context_ = true;
        pktrade::GlobalVar::local_context_ = new pktrade::LocalContext(local_risk_file);

        // Potentially use live_context if we have hyperliquid.
        // So, I guess it doesn't hurt to create the live_conetxt as well.
        pktrade::GlobalVar::live_context_ = new pktrade::LiveContext();
      }
    }
  }

  // Make sure this is set before the TradeCarrier is created
  pktrade::GlobalVar::ignore_usermsgs_ = ignore_usermsgs;

  // Then, create the tradecarrier.
  // The annoying thing (rn) is that the beacon and bookmanager
  // will need to be created after the carrier.

  TradeCarrier tc(trade_conf, strat_id, out_dir);

  pktrade::GlobalVar::trade_carrier_ = &tc;

  std::cout << " Set up tradecarrier" << std::endl;

  // Handle subscriptions.
  std::vector<BookId> subs = tc.getAllSubs();
  // Used for pushing dryrun outputs (eg. determining gateway setup).
  std::vector<BookId> traded_subs = tc.getTradingSubs();

  // Initialize the fundingmaster, but before we have the
  // postsecmasters,
  pktrade::GlobalVar::funding_master_ = new FundingMaster();

  // Load secmaster info for traded subscriptions.
  // Probably don't need to load for all subs. Because usually
  // we just care about the prices and lot sizes.
  pktrade::GlobalVar::secmaster_ = new SecMaster();
  pktrade::GlobalVar::secmaster_->load_from_exchange_info(traded_subs, live);

  tc.postSecmaster();

  if (dry_run) {
    // Print the subs and then exit.
    std::cout << "Dryrun_subscriptions" << std::endl;

    for (BookId& bid : subs) {
      std::cout << util::print_bookid(bid) << std::endl;
    }
    exit(0);
  }

  // Quick sanity check on subs.
  for (BookId& one_sub : subs) {
    if (one_sub.first == Market::Unknown) {
      throw std::runtime_error(std::string("Unknown market in subscription for ") +
                               std::string(one_sub.second.get()));
    }
  }

  LOG(INFO) << "About to set up signals";
  signal(SIGINT, handle_signal);
  signal(SIGTERM, handle_signal);
  signal(SIGHUP, handle_signal);
  signal(SIGABRT, handle_signal);

  if (!live && slowdown_market != "") {
    // Potentially set market slowdowns.
    pktrade::GlobalVar::slowdown_market_ =
        magic_enum::enum_cast<Market>(slowdown_market).value_or(Market::Unknown);
    pktrade::GlobalVar::slowdown_market_offset_ = slowdown_market_offset;
  }

  // Create the bm, mdbeacon, loop.
  LOG(INFO) << "About to set up BookManager";
  pktrade::GlobalVar::book_manager_ = new BookManager(subs.begin(), subs.end());
  LOG(INFO) << "About to set up MDBeacon";
  pktrade::GlobalVar::md_beacon_ = new pktrade::md::MDBeacon(
      pktrade::GlobalVar::book_manager_, subs.begin(), subs.end(), beacon_always_deliver);

  // Subscribe the event loop to the right streams.
  std::unique_ptr<HistoricalFeed> hist_feed;
  std::unique_ptr<LiveFeed> live_feed;
  if (!live) {

    // Have to get the full set of dates for things that span 24h.

    // start_date because if we grab the next_date's files by default.
    std::vector<std::pair<std::filesystem::path, Market>> streams =
        pktrade::util::stream_paths(data_dir, subs, start_date, true, true, equity_variant);

    hist_feed =
        std::make_unique<HistoricalFeed>(pktrade::GlobalVar::book_manager_, streams.begin(),
                                         streams.end(), std::stoi(pktrade::GlobalVar::date_));
    // HistoricalFeed feed();

    pktrade::GlobalVar::event_loop_->setPoller(std::make_unique<SimPoller>());
    pktrade::GlobalVar::event_loop_->registerPollables(*hist_feed);
  } else {
    // Subscribe to live things.

    LOG(INFO) << "About to set up LiveFeed";
    live_feed = std::make_unique<LiveFeed>(pktrade::GlobalVar::book_manager_);

    LOG(INFO) << "About to set up LivePoller";
    pktrade::GlobalVar::event_loop_->setPoller(std::make_unique<LivePoller>());
    LOG(INFO) << "About to register pollable LiveFeed";
    pktrade::GlobalVar::event_loop_->registerPollables(*live_feed);

    if (!pktrade::GlobalVar::paper_trading_mode_) {
      LOG(INFO) << "About to connect to LiveContext";
      if (local_risk_file == "") {
        pktrade::GlobalVar::live_context_->connect();
      } else {
        // Even if we use localcontext, we might still want to
        // have the livecontext running. So I am still running this
        // connect() call, This is to handle the hyperliquid/binance
        // case.
        pktrade::GlobalVar::live_context_->connect();
      }
    }
  }

  LOG(INFO) << "About to subscribe and finalize TradeCarrier";
  tc.subscribeAndFinalize();
  LOG(INFO) << "About to subscribe and prepare MDBeacon";
  pktrade::GlobalVar::md_beacon_->subscribeAndPrepare(pktrade::GlobalVar::book_manager_);

  if (!live && !global_inherit_path.empty()) {
    tc.loadInheritSchedule(global_inherit_path);
  }

  if (!live && !global_usermsg_path.empty()) {
    tc.loadUsermsgSchedule(global_usermsg_path);
  }

  try {
    // Run the loop.
    LOG(INFO) << "About to run EventLoop";
    pktrade::GlobalVar::event_loop_->runUntil(pktrade::GlobalVar::end_t_);
  } catch (const zmq::error_t& e) {
    if (e.num() != EINTR) { // unknown exception
      throw;
    }
    // When we get a SIGINT and there's an active blocked zmq socket (e.g., in LiveFeed), the socket
    // will throw this exception (after the signal handler finishes). We need to catch it here so
    // the rest of cleanup can finish.
    LOG(ERROR) << "caught zmq::error_t EINTR, ignoring";
  }

  // In case there are still any outstanding orders, make sure we cancel them one last time.
  tc.emergencyEOD();

  // Do all the stuff you were meant to do, m8.
  tc.eod();

  // Close ofstreams.
  if (pktrade::GlobalVar::trades_out_ != nullptr) {
    pktrade::GlobalVar::trades_out_->close();
  }

  if (pktrade::GlobalVar::orders_out_ != nullptr) {
    pktrade::GlobalVar::orders_out_->close();
  }

  if (pktrade::GlobalVar::logs_out_ != nullptr) {
    pktrade::GlobalVar::logs_out_->close();
  }

  if (pktrade::GlobalVar::live_) {
    if (!pktrade::GlobalVar::paper_trading_mode_) {
      if (!pktrade::GlobalVar::use_local_context_) {
        pktrade::GlobalVar::live_context_->disconnect();
      } else {
        // Do this anyway, again because we have the
        // hyperliquid/binance case.
        // Basically, we're moving to a model where we're just always
        // running live_context.
        pktrade::GlobalVar::live_context_->disconnect();
      }
    }
  }

  if (!pktrade::GlobalVar::live_) {
    // Do some EOD stuff.
    pktrade::GlobalVar::sim_verse_->eod();
  }
  return 0;
}
