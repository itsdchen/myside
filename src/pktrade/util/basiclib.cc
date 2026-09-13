#include "basiclib.h"

#include "pktrade/context/global_vars.h"
#include "time_utils.h"
#include <algorithm>
#include <csv.hpp>
#include <fmt/format.h>
#include <glog/logging.h>
#include <magic_enum.hpp>
#include <thread>
#include <unistd.h>

namespace pktrade::util {

void basicprint() { printf("Hello World, from the library call.\n"); }

rapidjson::Document read_json_file(std::string path_to_json) {
  FILE* fp = fopen(path_to_json.c_str(), "rb"); // non-Windows use "r"
  char readBuffer[65536];
  rapidjson::FileReadStream is(fp, readBuffer, sizeof(readBuffer));

  rapidjson::Document d;
  d.ParseStream<rapidjson::kParseCommentsFlag>(is);

  fclose(fp);
  return d;
}

std::string getNextDate(const std::string& dateStr) {
  // Parse the input string into a std::tm structure
  std::istringstream iss(dateStr);
  std::tm tm = {};
  iss >> std::get_time(&tm, "%Y%m%d");

  // Convert the std::tm structure to a std::chrono::system_clock::time_point
  std::chrono::system_clock::time_point tp(
      std::chrono::system_clock::from_time_t(std::mktime(&tm)));

  // Increment the time_point by one day
  tp += std::chrono::days(1);

  // Convert the time_point back to a std::tm structure
  std::time_t t = std::chrono::system_clock::to_time_t(tp);
  std::tm nextTm = *std::localtime(&t);

  // Format the next date as a string
  std::ostringstream oss;
  oss << std::put_time(&nextTm, "%Y%m%d");
  return oss.str();
}

std::string lower_str(std::string str) {
  std::string lowerStr(str);
  std::transform(lowerStr.begin(), lowerStr.end(), lowerStr.begin(), ::tolower);
  return lowerStr;
}

std::vector<std::pair<std::filesystem::path, Market>> stream_paths(std::string data_dir,
                                                                   std::vector<BookId> book_ids,
                                                                   std::string date, bool pbf,
                                                                   bool compressed,
                                                                   std::string equity_variant) {
  std::string next_date = getNextDate(date);

  std::unordered_map<Market, std::vector<std::string>> mkt_to_streams;
  mkt_to_streams[Market::BinanceSPOT] = {"depth", "depthSnapshot", "bookTicker", "trade"};
  mkt_to_streams[Market::BinanceFutures] = {"depth", "depthSnapshot", "bookTicker", "trade"};
  mkt_to_streams[Market::BinanceCOINFutures] = {"depth", "depthSnapshot", "bookTicker", "trade"};
  mkt_to_streams[Market::BybitDeriv] = {"publicTrade", "orderbook.1", "orderbook.50",
                                        "orderbook.500"};
  mkt_to_streams[Market::BybitInverseDeriv] = {"publicTrade", "orderbook.1", "orderbook.50",
                                               "orderbook.500"};
  mkt_to_streams[Market::BybitSPOT] = {"publicTrade", "orderbook.1", "orderbook.50",
                                       "orderbook.500"};

  mkt_to_streams[Market::Hyperliquid] = {"trades", "l2Book"};
  mkt_to_streams[Market::TopBookCme] = {"quotes"};
  mkt_to_streams[Market::TopBookEquity] = {"quotes"};

  std::vector<std::pair<std::filesystem::path, Market>> paths;

  for (BookId& b_id : book_ids) {
    if (mkt_to_streams.find(b_id.first) != mkt_to_streams.end()) {
      for (std::string& stream_name : mkt_to_streams[b_id.first]) {
        paths.push_back(std::make_pair(
            histdata_path(data_dir, b_id, stream_name, date, pbf, compressed, equity_variant),
            b_id.first));
        paths.push_back(std::make_pair(
            histdata_path(data_dir, b_id, stream_name, next_date, pbf, compressed, equity_variant),
            b_id.first));
      }
    }
  }

  return paths;
}

std::string histdata_path(std::string data_dir, BookId book_id, std::string stream_name,
                          std::string date, bool pbf, bool compressed,
                          std::string equity_variant) {
  std::string book = std::string(magic_enum::enum_name(book_id.first));
  if (!equity_variant.empty() && book_id.first == Market::TopBookEquity) {
    book = book + "_" + equity_variant;
  }
  std::string sym = book_id.second.get();
  // In the case of something like PURR/USDC,
  // replace all '/' to '_'
  std::replace(sym.begin(), sym.end(), '/', '_');

  std::string path;
  if (pbf) {
    // Hardcoded. But let's say that all the pbf files are
    // stored there.
    if (compressed) {
      path = fmt::format("{}/{}/{}_{}_{}.gzpbf", data_dir, book, sym, stream_name, date);

    } else {
      path = fmt::format("{}/{}/{}_{}_{}.pbf", data_dir, book, sym, stream_name, date);
    }
  } else {
    path = fmt::format("{}/{}/{}_{}_{}.jsonl", data_dir, book, sym, stream_name, date);
  }

  return path;
}

void write_csv(std::string file_path, std::vector<std::vector<double>> data) {
  std::ofstream myfile(file_path);

  for (std::vector<double>& row : data) {
    for (uint i = 0; i < row.size(); i++) {
      myfile << row[i];
      if (i == row.size() - 1) {
        myfile << std::endl;
      } else {
        myfile << ",";
      }
    }
  }
  myfile.close();

  // Looks like csvwriter is buggy. It's not writing negative signs,
  // giving me only positive values in the output file.
  // There's also very few docs on the actual site, so I'm
  // going to just skip it.
  /*
    csv::CSVWriter<std::ofstream> writer(myfile);
    for (std::vector<double>& row : data) {
      // Write it...
      std::cout << " arow";
      for (double& d : row) {
        std::cout << d << " ";
      }
      std::cout << std::endl;
      writer << row;
    }

  */
  // I guess this is all.
}

void write_csv_times_and_doubles(std::string file_path, std::vector<int64_t> times,
                                 std::vector<std::vector<double>> data) {
  // Firstly, check that all are the same length.
  size_t agreed_length = times.size();
  for (std::vector<double>& col : data) {
    if (col.size() != agreed_length) {
      throw std::runtime_error("write_csv_times_and_doubles: data cols are not all the same length "
                               ":" +
                               std::to_string(col.size()) + " vs " + std::to_string(agreed_length));
    }
  }

  // Otherwise, write something. You know?
  std::ofstream myfile(file_path);

  // Write for each row.
  for (uint row_idx = 0; row_idx < times.size(); row_idx++) {
    // Write the time.
    myfile << times[row_idx];

    // Go through all the cols and write those.
    for (std::vector<double>& col : data) {
      myfile << "," << col[row_idx];
    }
    myfile << std::endl;
  }

  myfile.close();
}

void write_csv_times(std::string file_path, std::vector<int64_t> data) {
  std::ofstream myfile(file_path);
  // I guess.. I don't need to have an actual csvwriter for this.
  // Doi.

  for (int64_t one_t : data) {
    myfile << one_t << std::endl;
  }
  myfile.close();
}

// To write our returns
void write_csv_returns(std::string file_path, std::vector<double> data) {
  std::ofstream myfile(file_path);

  for (double one_ret : data) {
    myfile << one_ret << std::endl;
    ;
  }
  myfile.close();
}

void write_csv_multireturns(std::string file_path, std::vector<std::vector<double>> data) {
  std::ofstream myfile(file_path);

  for (std::vector<double>& row : data) {
    for (int i = 0; i < row.size(); i++) {
      if (i > 0) {
        myfile << ",";
      }
      myfile << row[i];
    }
    myfile << std::endl;
  }

  myfile.close();
}

std::vector<double> read_csv_doubles(std::string file_path) {
  std::vector<double> to_ret;
  csv::CSVReader reader(file_path.c_str());
  for (csv::CSVRow& row : reader) {
    // Accessing elements
    for (csv::CSVField& field : row) {
      to_ret.push_back(field.get<double>());
    }
  }
  return to_ret;
}

std::vector<int64_t> read_csv_times(std::string file_path, bool has_header /*=false*/) {
  std::vector<int64_t> to_ret;

  csv::CSVFormat format;
  if (!has_header) {
    format.no_header();
  }
  csv::CSVReader reader(file_path.c_str(), format);
  for (csv::CSVRow& row : reader) {
    for (csv::CSVField& field : row) {
      to_ret.push_back(field.get<int64_t>());
    }
  }
  return to_ret;
}

std::string& ltrim(std::string& s, const char* t) {
  s.erase(0, s.find_first_not_of(t));
  return s;
}

// trim from right
std::string& rtrim(std::string& s, const char* t) {
  s.erase(s.find_last_not_of(t) + 1);
  return s;
}

// trim from left & right
std::string& trim(std::string& s, const char* t) { return ltrim(rtrim(s, t), t); }

std::vector<std::string> split_str(std::string cs_str, char delimiter) {
  // Strip string.
  cs_str = trim(cs_str);
  if (cs_str.size() == 0) {
    return {};
  }

  std::vector<std::string> result;
  std::stringstream s_stream(cs_str);
  while (s_stream.good()) {
    std::string substr;
    std::getline(s_stream, substr,
                 delimiter); // get first string delimited by comma
    result.push_back(substr);
  }
  return result;
}

std::string rjson_to_str(const rapidjson::Value& v) {
  rapidjson::StringBuffer sb;
  rapidjson::Writer<rapidjson::StringBuffer> writer(sb);
  v.Accept(writer);
  std::string s = sb.GetString();
  return s;
}

std::string print_bookid(BookId book_id) {
  return "(" + std::string(magic_enum::enum_name(book_id.first)) + ", " + book_id.second.get() +
         ")";
}

BookId make_bookid(std::string market_str, std::string symbol_str) {
  return BookId{*magic_enum::enum_cast<Market>(market_str), SymbolId{symbol_str}};
}

std::string make_pkcoid(std::string strategy_id, int order_num) {
  return strategy_id + "-" + std::to_string(order_num);
}

void print_inside(const LevelBook& book) {
  // Get the bids, asks.
  auto& buy_side = book.side<BuySide>();
  auto& sell_side = book.side<SellSide>();

  printf("Bid (px %f sz %f effsz %f) ask (px %f sz %f effsz %f)\n", buy_side.begin()->px.toDouble(),
         buy_side.begin()->qty.toDouble(), effectiveQty(*buy_side.begin()).toDouble(),
         sell_side.begin()->px.toDouble(), sell_side.begin()->qty.toDouble(),
         effectiveQty(*sell_side.begin()).toDouble());
}

void log_line(std::string line, bool verbose_only) {
  if (pktrade::GlobalVar::logs_out_ == nullptr) {
    return;
  }

  if (verbose_only && !pktrade::GlobalVar::verbose_) {
    return;
  }

  // Otherwise write it.
  *pktrade::GlobalVar::logs_out_ << time_utils::nowToMs() << " " << line << std::endl;
}

Mailer& Mailer::instance() {
  static Mailer m;
  return m;
}

bool Mailer::check_and_record_rate_limit(const std::string& subject, const std::string& receiver,
                                         int rate_limit_min) {
  int64_t now_s = time_utils::systemNowToS();
  std::lock_guard<std::mutex> lk(mu_);
  auto key = std::make_pair(subject, receiver);
  if (rate_limit_min > 0) {
    auto it = last_sent_s_.find(key);
    if (it != last_sent_s_.end() && now_s < it->second + int64_t(rate_limit_min) * 60) {
      LOG(INFO) << "Email \"" << subject << "\" -> " << receiver << " already sent in last "
                << rate_limit_min << " min. Skipping.";
      return false;
    }
  }
  // Record the timestamp even when rate_limit_min == 0, so a later caller passing
  // a non-zero rate_limit_min for the same (subject, receiver) sees a fresh send.
  last_sent_s_[key] = now_s;
  return true;
}

bool Mailer::send_mail_sync(std::string subject, std::string body, std::string receiver,
                            bool add_hostname /*=true*/, int rate_limit_min /*=5*/) {
  if (add_hostname) {
    char hostname[256];
    if (gethostname(hostname, sizeof(hostname)) == 0) {
      subject = fmt::format("{} ({})", subject, hostname);
    }
  }
  if (!check_and_record_rate_limit(subject, receiver, rate_limit_min)) {
    return false;
  }
  std::string command = fmt::format("echo \"{}\" | mail -s \"{}\" {}", body, subject, receiver);
  return (std::system(command.c_str()) == 0);
}

void Mailer::send_mail(std::string subject, std::string body, std::string receiver,
                       bool add_hostname /*=true*/, int rate_limit_min /*=5*/) {
  if (add_hostname) {
    char hostname[256];
    if (gethostname(hostname, sizeof(hostname)) == 0) {
      subject = fmt::format("{} ({})", subject, hostname);
    }
  }
  if (!check_and_record_rate_limit(subject, receiver, rate_limit_min)) {
    return;
  }
  std::thread t([subject, body, receiver]() {
    try {
      std::string command = fmt::format("echo \"{}\" | mail -s \"{}\" {} "
                                        "&& echo \"Email sent.\" || echo \"Email failed.\"",
                                        body, subject, receiver);
      std::system(command.c_str());
    } catch (const std::exception& e) {
      LOG(ERROR) << "send_mail thread exception: " << e.what();
    } catch (...) {
      LOG(ERROR) << "send_mail thread unknown exception";
    }
  });
  t.detach();
}

} // namespace pktrade::util
