#pragma once

#include <chrono>
#include <date/date.h>
#include <date/tz.h>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <utility>

#include "pktrade/clock.h"
#include <date/date.h>
#include <date/tz.h>
#include <fmt/format.h>
#include <magic_enum.hpp>

// Ref docs: Howard Hinnant's date library
// Some overlap with refs at
// https://en.cppreference.com/w/cpp/chrono

// For posterity: There's another library at
// https://abseil.io/docs/cpp/guides/time

namespace pktrade::time_utils {

int64_t strToMs(std::string time_str);

int64_t tpToMs(Clock::time_point tp);
int64_t tpToNs(Clock::time_point tp);

Clock::time_point msToTp(int64_t ms);
Clock::time_point nsToTp(int64_t ns);

// Given n minutes ago, calculate time from Clock now
// and format it in the supabase way.
std::string mins_ago_supabase_fmt(int minutes_back);

// Convert PostgreSQL/ISO 8601 timestamp string to epoch seconds
// Format: "2024-01-15T10:30:45.123456+00:00" or "2024-01-15T10:30:45Z"
int64_t postgresTimestampToSecs(const std::string& timestamp_str);

int64_t nowToS();
int64_t nowToMs();
int64_t nowToNs();
std::string nowToStr(std::string time_zone = "America/New_York");

// Like nowToS but using raw system clock instead of EventLoop's Clock
int64_t systemNowToS();

std::string prev_date(std::string date);

// This is pretty silly.
Clock::time_point zsToTp(date::zoned_seconds zs);

std::string tpToString(Clock::time_point tp, std::string time_zone = "America/New_York");

std::string nsToString(int64_t ns, std::string time_zone = "America/New_York");
std::string msToString(int64_t ms, std::string time_zone = "America/New_York");

std::string dateToStr(const date::year_month_day& ymd);

// Normalize H, H:MM, or H:MM:SS to HH:MM:SS. Returns nullopt for invalid input.
std::optional<std::string> normalizeClockTime(std::string clock_str);

// Basically for live-mode. What is today?
date::year_month_day today();

date::zoned_seconds dtToSecs(std::string date_time);
date::zoned_seconds splitDTToSecs(std::string date_str, std::string clock_str, std::string tz_str);

// Turns a string tp into a Clock::time_point that we can
// use to insert callbacks.
Clock::time_point strToClockTP(std::string date_time);

std::string zsToStr(date::zoned_seconds zs);

} // namespace pktrade::time_utils
