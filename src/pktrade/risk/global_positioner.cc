#include "global_positioner.h"
#include "pktrade/context/global_vars.h"
#include "pktrade/util/supabase_credentials.h"
#include "pktrade/util/time_utils.h"
#include <cpr/cpr.h>
#include <glog/logging.h>
#include <random>
#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>
#include <string>

#include <algorithm> // For std::sort

#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <sstream>

#include "pktrade/util/basiclib.h"
#include "pktrade/util/supabase_credentials.h"

#include <filesystem>
#include <stdexcept>

namespace pktrade::risk {

namespace {
constexpr std::chrono::seconds kSupabaseBackoffInitial{5};
constexpr std::chrono::seconds kSupabaseBackoffMax{120};
} // namespace

GlobalPositioner::GlobalPositioner(TradeRiskMan* riskman) {
  std::string hl_creds_path =
      (std::filesystem::path(getenv("HOME")) / ".creds" / ".Hyperliquid.creds.json").string();

  if (!std::filesystem::exists(hl_creds_path)) {
    throw std::runtime_error(fmt::format("Hyperliquid creds file {} not found", hl_creds_path));
  }
  rapidjson::Document hl_creds_conf = pktrade::util::read_json_file(hl_creds_path);

  if (!hl_creds_conf.HasMember("address")) {
    throw std::runtime_error(
        fmt::format("Tried to read address from {} but didnt find it", hl_creds_path));
  }

  pktrade::GlobalVar::hl_address_ = hl_creds_conf["address"].GetString();
  // If there's a subaccount, then use that instead.
  if (hl_creds_conf.HasMember("subaccount_address")) {
    pktrade::GlobalVar::hl_address_ = hl_creds_conf["subaccount_address"].GetString();
  }

  if (riskman == nullptr) {
    throw std::runtime_error("Tried to initialize GlobalPositioner with a nullptr riskman");
  }

  riskman_ = riskman;
  symbol_ = riskman_->getSymbol();
}

void GlobalPositioner::onDayStart() {
  // Start the periodic read/write of the position.
  // Schedule the first call at the next 5min-multiple-plus-1min mark
  // We don't start right away bc that caused some issues with Supabase, but maybe that's ok now?
  pktrade::GlobalVar::event_loop_->onTimeout(secsUntilNextPublish(),
                                             [&] { this->publishPositionLoop(); });
}

const std::chrono::seconds GlobalPositioner::secsUntilNextPublish() const {
  // (1) If we're just starting up, schedule the first publish at the next 5min-multiple-plus-1min.
  //     This is for synchronicity among the live queries, and the 1min offset is bc we want to
  //     inherit ASAP after startup, which is usually at round times (e.g., 18:00).
  // (2) If we just published for the first time, wait just 30s bc that's when we'll do the
  //     inheriting, and we want to do that quickly.
  // (3) If this is our 2nd or later time through, wait until the next 5min-multiple-plus-1min to
  //     get back or stay on the regular schedule.
  // And everything repeats after num_pubs_before_reinherit_ publishes.
  if (num_times_published_ % num_pubs_before_reinherit_ == 1) {
    // This is case (2): we're about to inherit, so only wait 30s.
    return std::chrono::seconds(30);
  }
  // All other cases: wait until the next 5min-multiple-plus-1min.
  // Add a random jitter up to 10s to spread the cpr calls out a bit.
  thread_local std::mt19937 gen{std::random_device{}()};
  std::uniform_int_distribution<int> dis(0, 10);
  int64_t now_seconds = pktrade::time_utils::nowToMs() / 1000;
  return std::chrono::seconds(300 - ((now_seconds - 60) % 300) + dis(gen));
}

// Pings hyperliquid to get my current position.
double GlobalPositioner::getGlobalPosition() {
  // Make sure we've initialized everything.
  LOG(INFO) << fmt::format("({}) Running getGlobalPosition", symbol_.get());

  if (symbol_.get() == "") {
    throw std::runtime_error("GlobalPositioner has not been initialized properly.");
  }

  return pktrade::GlobalVar::trade_carrier_->getGlobalPosition(symbol_);
}

/*
A position is only inheritable if nobody has it.
We track global_position through all the queries. Each inheriting query
independently calculates inheritance.
Basically we just keep track of how much position each other query had at the
last time of publishing, figure out how much was missing via the global
position, and then use that.

This *should* be a more robust way of passing things along but it can get
messier if we do things with variable start/stop times.
*/
void GlobalPositioner::startCalcMyInheritance(double my_pos, double my_max_pos,
                                              double global_position) {

  // Easy peasy.
  if (global_position == 0) {
    LOG(INFO) << fmt::format("({}) Quitting because global_position 0", symbol_.get());
    return;
  }

  // Get Supabase credentials from singleton
  auto& sb_creds = pktrade::util::SupabaseCredentials::getInstance();

  // Build the query URL
  const cpr::Parameters params{{"select", "*"},
                               {"symbol", fmt::format("eq.{}", symbol_.get())},
                               // Consider all publishes in the last 1 minute.
                               // We only need to go ~30s back to the last publish, but we go a bit
                               // past just in case. We definitely don't want to go more than 5m30s
                               // back though, otherwise we could pick up a stale publish from a
                               // possibly no-longer-live query.
                               {"created_at", "gte." + time_utils::mins_ago_supabase_fmt(1)},
                               {"limit", "100"},
                               // Most recent first.
                               {"order", "id.desc"}};

  // Note: the random jitter means some inheriting queries will likely see the post-inheritance
  // publish from other inheriting queries, but this should be fine since those publishes will
  // indicate they're no longer inheriting. E.g., 300 shs to inherit between 3 queries (all with the
  // same big maxpos): 1st query inherits 1/3 * 300 = 100 shs, 2nd query inherits 1/2 * 200 = 100
  // shs, 3rd query inherits all of remaining 100 shs.

  // Make the request via sb_creds
  const cpr::Timeout timeout{std::chrono::seconds(3)};
  sb_creds.cprGetPositionAS(params, timeout, this);

  // const cpr::Response& res = sb_creds.cprGetPosition(params, timeout);
}

// Does the actual inheritance calculation step.
// This is the second half of startCalcMyInheritance.
void GlobalPositioner::onGetPosition(const cpr::Response& res) {
  //
  if (res.status_code != 200) {
    LOG(ERROR) << fmt::format("({}) onGetPosition: Error - status: {}, error: {}, text: {}",
                              symbol_.get(), res.status_code, res.error.message, res.text);
    return;
  }

  if (res.error.code != cpr::ErrorCode::OK) {
    LOG(ERROR) << fmt::format("({}) onGetPosition: CPR error: {}", symbol_.get(),
                              res.error.message);
    return;
  }

  LOG(INFO) << fmt::format("({}) Response for positions: {}", symbol_.get(), res.text);

  rapidjson::Document response_doc;
  response_doc.Parse(res.text.c_str());

  // Safety checks.
  if (response_doc.HasParseError()) {
    // bail.
    LOG(ERROR) << fmt::format("({}) onGetPosition: call to Supabase got a bad json response: {}",
                              symbol_.get(), res.text);
    return;
  }

  // OK, now we can go through all these things and figure them out.
  // Count myself in here ofc.

  // Because we're measuring specifically the positions at the time of the last
  // publishing, we don't count ourself at the moment. We count ourself at the
  // previous db write.
  double running_eligible_global_maxpos = 0;
  double running_global_accounted_position = 0;

  std::vector<double> global_measured_positions;
  std::set<std::string> handled_strat_ids;

  if (!response_doc.IsArray()) {
    LOG(ERROR) << fmt::format("({}) GlobalPositioner call to supabase got non-array response {}",
                              symbol_.get(), res.text);
    return;
  }
  double my_pos = riskman_->getPos().toDouble();
  double my_max_pos = riskman_->getMaxPosBoundByNotional();

  for (const auto& one_postgres_row : response_doc.GetArray()) {

    if (!one_postgres_row.IsObject() ||
        (!one_postgres_row.HasMember("strat_id") || !one_postgres_row["strat_id"].IsString()) ||
        (!one_postgres_row.HasMember("global_pos") || !one_postgres_row["global_pos"].IsNumber()) ||
        (!one_postgres_row.HasMember("my_pos") || !one_postgres_row["my_pos"].IsNumber()) ||
        (!one_postgres_row.HasMember("my_max_pos") || !one_postgres_row["my_max_pos"].IsNumber()) ||
        (!one_postgres_row.HasMember("can_inherit") || !one_postgres_row["can_inherit"].IsBool())) {
      LOG(ERROR) << fmt::format("({}) skipping a malformed row from Supabase response {}",
                                symbol_.get(), res.text);
      continue;
    }

    if (handled_strat_ids.contains(one_postgres_row["strat_id"].GetString())) {
      // Don't double-count
      continue;
    }

    // Otherwise, add up the maxpos and then move on.
    global_measured_positions.push_back(one_postgres_row["global_pos"].GetDouble());

    if (one_postgres_row["can_inherit"].GetBool()) {

      // std::cout << "GP: ADDING from " << one_postgres_row["strat_id"].GetString() << " TO
      // ELIGIBLE GLOBAL MAXPOS: " << one_postgres_row["my_max_pos"].GetDouble() << std::endl;

      running_eligible_global_maxpos += one_postgres_row["my_max_pos"].GetDouble();
    }

    // global_accounted_position needs to be included whether or not they can-
    // or cannot- inherit.
    running_global_accounted_position += one_postgres_row["my_pos"].GetDouble();

    handled_strat_ids.insert(one_postgres_row["strat_id"].GetString());

    // Print this out.
    std::cout << fmt::format("GP: HANDLED STRAT ID: {}, GLOBAL POS: {}, MY_POS: {}, MY_MAX_POS: {} "
                             "TOTAL_MY_MAX_POS {} RUNNING_ACCOUNTED_GLOBAL_POSITION {}",
                             one_postgres_row["strat_id"].GetString(),
                             one_postgres_row["global_pos"].GetDouble(),
                             one_postgres_row["my_pos"].GetDouble(),
                             one_postgres_row["my_max_pos"].GetDouble(),
                             running_eligible_global_maxpos, running_global_accounted_position)
              << std::endl;
  }

  // If we somehow didn't handle the current strategy, add that in.
  if (!handled_strat_ids.contains(pktrade::GlobalVar::strat_id_)) {
    LOG(ERROR) << fmt::format(
        "({}) somehow didn't account for own maxpos in previous publish, adding in now",
        symbol_.get());
    running_eligible_global_maxpos += my_max_pos;
  }

  // This shouldn't really be 0
  if (global_measured_positions.empty()) {
    LOG(ERROR) << fmt::format(
        "({}) GlobalPositioner::calcMyInheritance: No global measured positions found, returning 0",
        symbol_.get());
    return;
  }

  std::sort(global_measured_positions.begin(), global_measured_positions.end());
  size_t med_idx = global_measured_positions.size() / 2;
  double median_global_pos = global_measured_positions[med_idx];

  // Log in case these are out of sync, do a sanity check.
  if (global_measured_positions.size() > 1) {
    double largest_size = global_measured_positions.back();
    double smallest_size = global_measured_positions.front();
    if (smallest_size > 0 && (largest_size - smallest_size) / smallest_size > 0.05) {
      // Probably an exec happened right around when everyone was querying HL for global positions
      std::string err_str = fmt::format("({}) GlobalPositioner measured positions are out of sync. "
                                        "Largest {} smallest {} diff {} |||| FULL SET",
                                        symbol_.get(), largest_size, smallest_size,
                                        (largest_size - smallest_size) / smallest_size);
      for (double size : global_measured_positions) {
        err_str += fmt::format(" {}", size);
      }
      LOG(WARNING) << err_str;
    }
  }

  // Everything that's not a position in a query that can take position... is
  // position that can be inherited.
  double excess_position = median_global_pos - running_global_accounted_position;

  // This also shouldn't happen if my own maxpos is included in there.
  // Suggests a database issue.
  if (running_eligible_global_maxpos == 0) {
    LOG(ERROR) << fmt::format(
        "({}) GlobalPositioner::calcMyInheritance: No eligible global maxpos found, returning 0",
        symbol_.get());
    return;
  }

  double my_inherit_amount = excess_position * (my_max_pos / running_eligible_global_maxpos);

  // Cap it by how much we can actually take.
  // Only allow it to take us to half maxpos.
  if (my_inherit_amount > 0) {
    my_inherit_amount = std::min(my_inherit_amount, (my_max_pos / 2) - my_pos);
  } else if (my_inherit_amount < 0) {
    // Don't let it get past the negative maxpos.
    my_inherit_amount = std::max(my_inherit_amount, -(my_max_pos / 2) - my_pos);
  }

  // Print out the calculations.
  LOG(INFO) << fmt::format("({}) GPCalc: median global position {}, excess position {}, eligible "
                           "maxpos {}, my_maxpos {}, my_inherit {}",
                           symbol_.get(), median_global_pos, excess_position,
                           running_eligible_global_maxpos, my_max_pos, my_inherit_amount);

  inherited_position_ = my_inherit_amount;
  // Give this position to the trademan.
  riskman_->addInheritPosition(inherited_position_);

  // After calculating inheritance, publish our position.
  // Note, here we're kind of using median_global_pos instead of my measured one.
  performPublishPosition(median_global_pos, my_pos, my_max_pos);
}

void GlobalPositioner::forceInheritLoop() {
  if (!riskman_->doingGlobalInherit()) {
    LOG(WARNING) << fmt::format(
        "({}) FORCE_INHERIT_LOOP: ignoring because global inheritance is disabled", symbol_.get());
    return;
  }
  // Reset to 0 so the next scheduled publishPositionLoop publishes can_inherit=true
  // (0 % 48 == 0), waits 30s, then does the inherit calc (1 % 48 == 1).
  // This reuses the normal 2-step flow instead of bypassing it.
  LOG(INFO) << fmt::format("({}) FORCE_INHERIT_LOOP: resetting num_times_published to 0, "
                           "next scheduled loop will run the inherit flow",
                           symbol_.get());
  num_times_published_ = 0;
}

// Publish my own position every so often.
// Start this during the DayStart() calls.
void GlobalPositioner::publishPositionLoop() {
  // Step 1:

  // Note that getGlobalPosition makes a synchronous request to Hyperliquid, and may
  // end up being a bottleneck. Down the line if we asyncify everything, we may just want to
  // restructure our code.
  double sym_position = getGlobalPosition();
  double my_position = riskman_->getPos().toDouble();
  double my_max_pos = riskman_->getMaxPosBoundByNotional();

  // Only consider inheritance if this is the 2nd or later periodic time we hit this loop.
  if (riskman_->doingGlobalInherit() && num_times_published_ % num_pubs_before_reinherit_ == 1) {
    // Read in everyone's position,

    LOG(INFO) << fmt::format("({}) Running calcMyInheritance with {} {} {}", symbol_.get(),
                             my_position, my_max_pos, sym_position);
    startCalcMyInheritance(my_position, my_max_pos, sym_position);

  } else {
    // If not calculating inheritance, publish position immediately.
    performPublishPosition(sym_position, my_position, my_max_pos);
  }
  ++num_times_published_;
  // Schedule the next loop.
  pktrade::GlobalVar::event_loop_->onTimeout(secsUntilNextPublish(),
                                             [&] { this->publishPositionLoop(); });
}

void GlobalPositioner::performPublishPosition(double sym_position, double my_position,
                                              double my_max_pos) {
  // Write inherited position, etc. to the db.

  const auto remaining = supabaseBackoffRemaining();
  if (remaining.count() > 0) {
    LOG(WARNING) << fmt::format(
        "({}) Skipping Supabase write while backing off ({}s remaining)", symbol_.get(),
        remaining.count());
    return;
  }

  rapidjson::Document doc;
  doc.SetObject();
  auto& allocator = doc.GetAllocator();

  doc.AddMember("strat_id", rapidjson::Value(pktrade::GlobalVar::strat_id_.c_str(), allocator),
                allocator);
  doc.AddMember("symbol", rapidjson::Value(symbol_.get().c_str(), allocator), allocator);
  doc.AddMember("global_pos", sym_position, allocator);
  doc.AddMember("my_pos", my_position, allocator);
  doc.AddMember("my_max_pos", my_max_pos, allocator);
  doc.AddMember("my_inherit_amount", inherited_position_, allocator);
  // A query can inherit only if it has just started - otherwise, for the global calculation,
  // assume it is done.
  bool can_inherit =
      (riskman_->doingGlobalInherit() && num_times_published_ % num_pubs_before_reinherit_ == 0);
  doc.AddMember("can_inherit", can_inherit, allocator);

  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  doc.Accept(writer);

  // Get Supabase credentials from singleton
  auto& sb_creds = pktrade::util::SupabaseCredentials::getInstance();

  // Build the query URL and send it via sb_creds
  const cpr::Body body{buffer.GetString()};
  const cpr::Timeout timeout{std::chrono::seconds(3)};

  LOG(INFO) << fmt::format("({}) Writing position to supabase: global_pos {} my_pos {} "
                           "my_max_pos {} my_inherit_amount {} can_inherit {}",
                           symbol_.get(), sym_position, my_position, my_max_pos,
                           inherited_position_, can_inherit);
  sb_creds.cprPostPositionAS(body, timeout, this);
}

void GlobalPositioner::onPostPosition(const cpr::Response& res) {
  if (res.status_code != 200 && res.status_code != 201) {
    LOG(ERROR) << fmt::format(
        "({}) Error writing position to Supabase - status: {}, error: {}, text: {}", symbol_.get(),
        res.status_code, res.error.message, res.text);
    recordSupabaseFailure(fmt::format("status {}", res.status_code));
  } else if (res.error.code != cpr::ErrorCode::OK) {
    LOG(ERROR) << fmt::format("({}) CPR error writing position to Supabase: {}", symbol_.get(),
                              res.error.message);
    recordSupabaseFailure(res.error.message);
  } else {
    // Logging to world that I wrote this.
    resetSupabaseBackoff();
    LOG(INFO) << fmt::format("({}) Wrote position to supabase", symbol_.get());
  }
}

bool GlobalPositioner::inSupabaseBackoff() const {
  return std::chrono::steady_clock::now() < supabase_backoff_until_;
}

std::chrono::seconds GlobalPositioner::supabaseBackoffRemaining() const {
  if (!inSupabaseBackoff()) {
    return std::chrono::seconds(0);
  }
  return std::chrono::duration_cast<std::chrono::seconds>(supabase_backoff_until_ -
                                                          std::chrono::steady_clock::now());
}

void GlobalPositioner::recordSupabaseFailure(const std::string& reason) {
  supabase_backoff_attempts_ = std::min(supabase_backoff_attempts_ + 1, 10);
  const int shift = std::min(supabase_backoff_attempts_ - 1, 5);
  auto delay = kSupabaseBackoffInitial * (1 << shift);
  if (delay > kSupabaseBackoffMax) {
    delay = kSupabaseBackoffMax;
  }
  supabase_backoff_until_ = std::chrono::steady_clock::now() + delay;
  LOG(WARNING) << fmt::format("({}) Supabase write failure ({}). Backing off for {}s (attempt {})",
                              symbol_.get(), reason, delay.count(), supabase_backoff_attempts_);
}

void GlobalPositioner::resetSupabaseBackoff() {
  supabase_backoff_attempts_ = 0;
  supabase_backoff_until_ = std::chrono::steady_clock::time_point::min();
}

}; // namespace pktrade::risk
