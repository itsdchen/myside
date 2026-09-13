#pragma once

#include <filesystem>
#include <map>
#include <mutex>
#include <stdio.h>

#include "pktrade/mdapi.h"
#include <rapidjson/document.h>
#include <rapidjson/filereadstream.h>
#include <vector>

#include "pktrade/level_book.h"
#include "rapidjson/stringbuffer.h"
#include <rapidjson/writer.h>

namespace pktrade::util {

void basicprint();

rapidjson::Document read_json_file(std::string path_to_json);

// lowercase a symbol. Stupid but want to do it easily.
std::string lower_str(std::string str);

std::string getNextDate(const std::string& dateStr);

std::vector<std::pair<std::filesystem::path, Market>>
stream_paths(std::string data_dir, std::vector<BookId> book_ids, std::string date, bool pbf = true,
             bool compressed = true, std::string equity_variant = "");

// get the path of a file.
// equity_variant: if non-empty and book_id is TopBookEquity, the directory becomes
// "TopBookEquity_<equity_variant>" (e.g. "TopBookEquity_Boats"). Other markets unaffected.
std::string histdata_path(std::string data_dir, BookId book_id, std::string stream_name,
                          std::string date, bool pbf = true, bool compressed = true,
                          std::string equity_variant = "");
// note: docs for csv library are at:
// https://github.com/vincentlaucsb/csv-parser
// https://github.com/vincentlaucsb/csv-parser/blob/master/single_include/csv.hpp

// No header. Just write this to file.
// Note that right now, I'm just going to
void write_csv(std::string file_path, std::vector<std::vector<double>> data);

// Like the above, but the first column is int64_t representing a timestamp
void write_csv_times_and_doubles(std::string file_path, std::vector<int64_t> times,
                                 std::vector<std::vector<double>> data);

// I know there's a better way of doing this. I just don't want to
// deal with code dedup rn so I can just move on.
// To write our times.
void write_csv_times(std::string file_path, std::vector<int64_t> data);

// To write our returns
void write_csv_returns(std::string file_path, std::vector<double> data);

void write_csv_multireturns(std::string file_path, std::vector<std::vector<double>> data);

// For now, I think I'm just going to be reading in one-width
// things. When the time comes I guess I'll do the other thing.
std::vector<double> read_csv_doubles(std::string file_path);
std::vector<int64_t> read_csv_times(std::string file_path, bool has_header = false);

std::string& ltrim(std::string& s, const char* t = " \t\n\r\f\v");
// trim from right
std::string& rtrim(std::string& s, const char* t = " \t\n\r\f\v");

// trim from left & right
std::string& trim(std::string& s, const char* t = " \t\n\r\f\v");

std::vector<std::string> split_str(std::string cs_str, char delimiter = ',');

std::string rjson_to_str(const rapidjson::Value& v);

// Just... pls.
std::string print_bookid(BookId book_id);

BookId make_bookid(std::string market_str, std::string symbol_str);

std::string make_pkcoid(std::string strategy_id, int order_num);

void print_inside(const LevelBook& book);

// Logs to the log file.
// If verbose_only, then skip if we're not running in verbose mode.
void log_line(std::string line, bool verbose_only);

// Sends emails via the `mail` command (needs msmtp set up on machine), with rate-limiting
// keyed on (subject, receiver) — matches the rate_limit behavior of the Python
// send_mail in overmind/strat_main/util/email_utils.py.
class Mailer {
 public:
  static Mailer& instance();

  Mailer(const Mailer&) = delete;
  Mailer& operator=(const Mailer&) = delete;

  // Sends an email synchronously. Returns true iff the send was attempted and the command
  // returned success. If rate-limited, returns false without sending.
  // rate_limit_min: skip the send if an email with the same (subject, receiver) was sent
  // within the last rate_limit_min minutes. Pass 0 to disable.
  bool send_mail_sync(std::string subject, std::string body,
                      std::string receiver = "pktrade@googlegroups.com",
                      bool add_hostname = true, int rate_limit_min = 5);

  // Sends an email from a detached thread, so caller doesn't block.
  // Note: fine to use from pktrade, but a command-line tool that exits quickly shouldn't
  // call this, since the thread is killed when the main process exits and sending may not
  // have time to finish.
  void send_mail(std::string subject, std::string body,
                 std::string receiver = "pktrade@googlegroups.com",
                 bool add_hostname = true, int rate_limit_min = 5);

  // Overloaded version to specify rate_limit_min but keep other defaults.
  void send_mail(std::string subject, std::string body, int rate_limit_min) {
    send_mail(subject, body, "pktrade@googlegroups.com", true, rate_limit_min);
  }

 private:
  Mailer() = default;

  // Returns true iff we should send (i.e. not rate-limited). When returning true, records
  // `now` as the last-send time for this key so subsequent calls within the window skip.
  bool check_and_record_rate_limit(const std::string& subject, const std::string& receiver,
                                   int rate_limit_min);

  std::mutex mu_;
  std::map<std::pair<std::string, std::string>, int64_t> last_sent_s_;
};

} // namespace pktrade::util
