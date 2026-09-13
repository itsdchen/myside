#include "time_utils.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <fmt/ostream.h>
#include <iomanip>
#include <iostream>
#include <regex>
#include <vector>

namespace pktrade::time_utils {

using date::operator<<;

int64_t strToMs(std::string time_str) {
  // Honestly retarded.

  date::zoned_seconds start_t = dtToSecs(time_str);
  Clock::time_point start_t_ = zsToTp(start_t);
  int64_t start_ms = tpToMs(start_t_);
  return start_ms;
}

int64_t tpToMs(Clock::time_point tp) {
  return std::chrono::duration_cast<std::chrono::milliseconds>(tp.time_since_epoch()).count();
}

int64_t tpToNs(Clock::time_point tp) {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(tp.time_since_epoch()).count();
}

Clock::time_point msToTp(int64_t ms) {
  return clock_cast<Clock>(std::chrono::system_clock::time_point{std::chrono::milliseconds{ms}});
}

Clock::time_point nsToTp(int64_t ns) {
  return clock_cast<Clock>(std::chrono::system_clock::time_point{std::chrono::nanoseconds{ns}});
}

std::string mins_ago_supabase_fmt(int minutes_back) {
  auto now = Clock::now();
  auto cutoff = now - std::chrono::minutes(minutes_back);
  auto system_cutoff = clock_cast<std::chrono::system_clock>(cutoff);
  return date::format("%Y-%m-%dT%H:%M:%SZ", system_cutoff);
}

int64_t postgresTimestampToSecs(const std::string& timestamp_str) {
  // Parse ISO 8601 timestamp (e.g., "2024-01-15T10:30:45.123456+00:00")
  std::istringstream in{timestamp_str};
  date::sys_time<std::chrono::seconds> tp;
  in >> date::parse("%FT%T", tp); // Parses up to seconds, ignores fractional/timezone

  if (in.fail()) {
    throw std::runtime_error("Failed to parse timestamp: " + timestamp_str);
  }

  return tp.time_since_epoch().count();
}

int64_t nowToS() {
  return std::chrono::duration_cast<std::chrono::seconds>(Clock::now().time_since_epoch()).count();
}

int64_t nowToMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch())
      .count();
}

int64_t nowToNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch())
      .count();
}

std::string nowToStr(std::string time_zone) {
  std::chrono::time_point<std::chrono::system_clock> tp =
      clock_cast<std::chrono::system_clock>(Clock::now());
  auto zoned_tp = date::make_zoned("America/New_York", tp);
  std::string zoned_str = date::format("%Y%m%d %H:%M:%S %Z", zoned_tp);
  return zoned_str;
}

int64_t systemNowToS() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::string prev_date(std::string date) {
  std::tm tm = {};
  std::istringstream(date) >> std::get_time(&tm, "%Y%m%d");
  tm.tm_mday -= 1;  // Subtract one day
  std::mktime(&tm); // Normalize the date
  char buffer[9];
  std::strftime(buffer, sizeof(buffer), "%Y%m%d", &tm);
  return buffer;
}

Clock::time_point zsToTp(date::zoned_seconds zs) {
  std::chrono::time_point<std::chrono::system_clock> a_tp =
      std::chrono::time_point_cast<std::chrono::system_clock::duration>(zs.get_sys_time());

  Clock::time_point ctp = clock_cast<Clock>(a_tp);
  return ctp;
}

std::string tpToString(Clock::time_point tp, std::string time_zone) {
  // I don't... know what to do here.
  // I'm going to do the dumbest possible thing ot make this work.
  int64_t ms = std::chrono::duration_cast<std::chrono::milliseconds>(tp.time_since_epoch()).count();
  std::chrono::system_clock::time_point new_tp{std::chrono::milliseconds{ms}};

  if (time_zone != "") {
    auto zoned_tp = date::make_zoned("America/New_York", new_tp);
    std::string zoned_str = date::format("%Y%m%d %H:%M:%S %Z", zoned_tp);
    return zoned_str;
  }

  // Otherwise.

  return date::format("%Y%m%d %H:%M:%S %Z", new_tp);
}

std::string msToString(int64_t ms, std::string time_zone) {
  std::chrono::system_clock::time_point tp{std::chrono::milliseconds{ms}};

  if (time_zone != "") {
    auto zoned_tp = date::make_zoned(time_zone.c_str(), tp);
    std::string zoned_str = date::format("%Y%m%d %H:%M:%S %Z", zoned_tp);
    return zoned_str;
  }

  return date::format("%Y%m%d %H:%M:%S %Z", tp);
}

std::string nsToString(int64_t ns, std::string time_zone) {
  std::chrono::system_clock::time_point tp{std::chrono::nanoseconds{ns}};

  if (time_zone != "") {
    auto zoned_tp = date::make_zoned("America/New_York", tp);
    std::string zoned_str = date::format("%Y%m%d %H:%M:%S %Z", zoned_tp);
    return zoned_str;
  }

  return date::format("%Y%m%d %H:%M:%S %Z", tp);
}

std::string dateToStr(const date::year_month_day& ymd) {
  std::stringstream ss;
  ss << std::setw(4) << std::setfill('0') << static_cast<int>(ymd.year());
  ss << std::setw(2) << std::setfill('0') << static_cast<unsigned>(ymd.month());
  ss << std::setw(2) << std::setfill('0') << static_cast<unsigned>(ymd.day());
  return ss.str();
}

date::year_month_day today() {
  // auto tp = clock_cast<std::chrono::system_clock>(Clock::now());
  //  Not sure if Clock::now is going to be correct at startup, so I'm just
  //  going off of system_clock for now. It resolves... to the "right" thing rn.
  std::chrono::system_clock::time_point now_tp = std::chrono::system_clock::now();
  auto day_tp = date::floor<date::days>(now_tp);

  date::year_month_day tdy{day_tp};
  return tdy;
}

std::optional<std::string> normalizeClockTime(std::string clock_str) {
  auto first = std::find_if_not(clock_str.begin(), clock_str.end(),
                                [](unsigned char c) { return std::isspace(c); });
  auto last = std::find_if_not(clock_str.rbegin(), clock_str.rend(),
                               [](unsigned char c) { return std::isspace(c); }).base();
  if (first >= last) {
    return std::nullopt;
  }
  clock_str = std::string(first, last);

  std::vector<std::string> parts;
  std::stringstream ss(clock_str);
  std::string token;
  while (std::getline(ss, token, ':')) {
    if (token.empty() ||
        !std::all_of(token.begin(), token.end(),
                     [](unsigned char c) { return std::isdigit(c); })) {
      return std::nullopt;
    }
    parts.push_back(token);
  }

  if (parts.empty() || parts.size() > 3) {
    return std::nullopt;
  }

  try {
    int hours = std::stoi(parts[0]);
    int minutes = parts.size() >= 2 ? std::stoi(parts[1]) : 0;
    int seconds = parts.size() >= 3 ? std::stoi(parts[2]) : 0;

    if (hours < 0 || hours > 23 || minutes < 0 || minutes > 59 || seconds < 0 ||
        seconds > 59) {
      return std::nullopt;
    }

    return fmt::format("{:02d}:{:02d}:{:02d}", hours, minutes, seconds);
  } catch (...) {
    return std::nullopt;
  }
}

// I literally don't care about efficiency rn. I'm just gonna
// do this quickly and optimize later.
date::zoned_seconds dtToSecs(std::string date_time) {
  int date_separator = date_time.find(" ");

  int tz_separator = date_time.find(" ", date_separator + 1);

  std::string date_str = date_time.substr(0, date_separator);
  std::string clock_str = date_time.substr(date_separator + 1, tz_separator - date_separator - 1);
  std::string tz_str = date_time.substr(tz_separator + 1, date_time.size() - tz_separator - 1);

  return splitDTToSecs(date_str, clock_str, tz_str);
}

// Annoying. The timezone is annoying.
date::zoned_seconds splitDTToSecs(std::string date_str, std::string clock_str, std::string tz_str) {
  if (date_str.length() != 8 && date_str.length() != 10) {
    throw std::invalid_argument("Date string must be of the form YYYYMMDD or YYYY-MM-DD");
  }

  auto normalized_clock = normalizeClockTime(clock_str);
  if (!normalized_clock) {
    throw std::invalid_argument("Clock string must be H, H:MM, or H:MM:SS");
  }
  clock_str = *normalized_clock;

  std::stringstream ss;
  ss << date_str << " " << clock_str << " " << tz_str;

  std::istringstream in(ss.str());
  date::local_seconds tp;
  // This needs to be exact. The formats are at
  // https://howardhinnant.github.io/date/date.html#to_stream_formatting
  in >> date::parse("%Y%m%d %T %Z", tp, tz_str);

  date::zoned_seconds zoned_time = date::make_zoned(tz_str, tp);

  return zoned_time;
}

Clock::time_point strToClockTP(std::string date_time) {
  date::zoned_seconds zs = dtToSecs(date_time);
  date::sys_seconds ss = zs.get_sys_time();
  Clock::time_point my_tp = clock_cast<Clock>(ss);
  return my_tp;
}

std::string zsToStr(date::zoned_seconds zs) { return date::format("%Y%m%d %T %Z", zs); }
} // namespace pktrade::time_utils
