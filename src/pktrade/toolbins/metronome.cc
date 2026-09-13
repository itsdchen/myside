#include "pktrade/tempos/base_tempo.h"
#include "pktrade/tempos/tempo_factory.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"
#include "pktrade/util/time_utils.h"

#include "pktrade/clock.h"
#include "pktrade/event_loop.h"

#include "pktrade/mktdata/md_beacon.h"

#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>

#include <rapidjson/document.h>
#include <rapidjson/filereadstream.h>

#include <glog/logging.h>
#include <set>
#include <stdio.h>

using namespace pktrade;

/*

Given a tempo conf, runs through and counts the number of fires within the tp.

Sample command:
/home/david/tradefi/pkt_0/bin/metronome --date 20230103 --start "03:00:00
America/New_York" --end "04:00:00 America/New_York" --conf
/home/david/modeltrain/modelv1/tickrate/tickrate_tempo.json

Sample output:
num_tempo_fires: 38248
tot_secs: 3600.000000
ticks_per_sec: 10.624444


*/

class TimerMew : public pktrade::tempos::TempoListener {
 public:
  TimerMew();

  void setConf(rapidjson::Value& doc, std::string wanted_tempo_name = "");

  std::vector<pktrade::BookId> getBookIds();

  void subscribe();
  void onTempo(int tempo_id) override;
  void finalize();

 protected:
  int num_tempo_calls_;
  // I should be getting the start_t and end_t, no?
  pktrade::tempos::BaseTempo* wanted_tempo_;

  // Time in ms
  bool first_tick_ = true;
  int64_t first_t_ = 0;
  int64_t latest_t_ = 0;
};

TimerMew::TimerMew() : num_tempo_calls_(0) {
  // Do some construction of tempos.
}

void TimerMew::setConf(rapidjson::Value& doc, std::string wanted_tempo_name) {
  // Step 1. Grab all the tempos.

  if (!doc.HasMember("tempos")) {
    throw std::runtime_error("No tempos provided in metronome conf");
  }

  rapidjson::Value& tempos = doc["tempos"];
  if (!tempos.IsArray()) {
    throw std::runtime_error("tempos should be an array of used tempos");
  }

  // Now, this should be an array of tempos.
  for (rapidjson::Value& tempo_conf : tempos.GetArray()) {
    if (!tempo_conf.IsObject()) {
      throw std::runtime_error("Tempo conf should be a dict");
    }

    if (!tempo_conf.HasMember("name")) {
      throw std::runtime_error("Tempo has no name");
    }

    std::string tempo_name = tempo_conf["name"].GetString();
    wanted_tempo_ = pktrade::GlobalVar::tf_->getOrMakeByConf(tempo_conf);
    // Notice me senpai
    wanted_tempo_->addListener(this);
  }
  // I guess, create the tempos then.
}

std::vector<pktrade::BookId> TimerMew::getBookIds() {
  // Get this from the tempo_factory.
  return pktrade::GlobalVar::tf_->getBookIds();
}

void TimerMew::subscribe() {
  // I haven't made up my mind yet, whether TimerMew should be the subscriber or
  // other people should. But for now, let's do it here. The containing object
  // does the subscriptions. We should probably just always have subscriptions
  // to the book be managed by the factories. And then subscriptions to other
  // objects be managed by the other objects.
  pktrade::GlobalVar::tf_->subscribeAll();
}

void TimerMew::onTempo(int tempo_id) {
  num_tempo_calls_++;

  if (first_tick_) {
    first_t_ = time_utils::nowToMs();
    first_tick_ = false;
  }
  latest_t_ = time_utils::nowToMs();
}

void TimerMew::finalize() {
  // Changing this up to be last_time minus first_time.
  // Because some days we have truncaed data, it's not exactly right to assume
  // we hvae tick data starting from start and ending at end.

  // Display time elapsed and num_ticks.
  /*
  std::chrono::seconds secs_elapsed_t =
      std::chrono::duration_cast<std::chrono::seconds>(
          pktrade::GlobalVar::end_t_ - pktrade::GlobalVar::start_t_);
          */
  // These t's were in ms.
  double secs_elapsed_d = (latest_t_ - first_t_) / 1000;

  printf("num_tempo_fires: %d\n", num_tempo_calls_);
  printf("tot_secs: %f\n", secs_elapsed_d);
  printf("ticks_per_sec: %f\n", num_tempo_calls_ / secs_elapsed_d);
}

int main(int argc, char** argv) {
  google::InitGoogleLogging(argv[0]);

  printf("Running cmd:\n");
  for (int i = 0; i < argc; i++) {
    printf("%s ", argv[i]);
  }
  printf("\n");

  std::string date;
  std::string start_date;
  std::string end_date;
  std::string conf_path;
  // If we only want tick density between certain times.
  // This paradigm will have to be followed in a lot of places anyway.
  std::string start_t_str = "03:00:00 America/New_York";
  std::string end_t_str = "16:00:00 America/New_York";
  std::string data_dir = std::string(getenv("HOME")) + "/tardis_datasets/gzpbf";

  // Read in CLI options.
  CLI::App cmd_flags("metronome");
  cmd_flags.add_option("-d,--date", date, "Date")->required();
  cmd_flags.add_option("--end-date", end_date, "End Date");
  cmd_flags.add_option("-f,--conf", conf_path, "Conf Path")->required();
  cmd_flags.add_option("-s,--start", start_t_str, "Start Time");
  cmd_flags.add_option("-e,--end", end_t_str, "End Time");
  cmd_flags.add_option("--datadir", data_dir, "Data Dir");

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

  // Can I get the normal time from that?

  // Set the globalvars.
  pktrade::GlobalVar::start_t_ = pktrade::time_utils::zsToTp(start_t);
  pktrade::GlobalVar::end_t_ = pktrade::time_utils::zsToTp(end_t);

  // Handle case where start time is after end time (e.g., 18:01 start, 16:00 end next day)
  if (pktrade::GlobalVar::start_t_ > pktrade::GlobalVar::end_t_) {
    pktrade::GlobalVar::start_t_ -= std::chrono::hours(24);
  }

  std::cout << "start_t: " << start_t << std::endl;

  pktrade::GlobalVar::event_loop_ = new pktrade::EventLoop();

  // Create factories.
  pktrade::GlobalVar::tf_ = new pktrade::tempos::TempoFactory();

  TimerMew tm;
  rapidjson::Document d = pktrade::util::read_json_file(conf_path);
  tm.setConf(d);

  // TODO: get this stream translation thing set up, and for more books than
  // just BinanceSPOT. Push it to a library, yeah?

  // Get the BookIDs
  std::vector<pktrade::BookId> sub_bookids = tm.getBookIds();

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

  // The TM should handle subscriptions.
  tm.subscribe();
  // pktrade::GlobalVar::tf_->subscribeAll();
  pktrade::GlobalVar::md_beacon_->subscribeAndPrepare(pktrade::GlobalVar::book_manager_);

  // Initialize context data.
  // pktrade::GlobalVar::event_loop_->run();
  pktrade::GlobalVar::event_loop_->runUntil(pktrade::GlobalVar::end_t_);

  // finalize.
  tm.finalize();
  return 0;
}
