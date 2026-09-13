#include <stdio.h>

#include <boost/algorithm/string.hpp>
#include <filesystem>
#include <fmt/format.h>
#include <iostream>
#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>
#include <string>

#include "pktrade/util/basiclib.h"
#include "pktrade/util/cli.h"

#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <sstream>

#include <cpr/cpr.h>

/*
I'm going to test two things:

1 - reading from our postgres
2 - writing to our postgres

I'll use this as the basis for figuring out how to do the
global position checker.
*/

using namespace pktrade;

std::string sb_creds_path = fmt::format("{}/.creds/.Supabase.creds.json", getenv("HOME"));
rapidjson::Document sb_creds_conf = pktrade::util::read_json_file(sb_creds_path);

std::string PROJECT_ID = sb_creds_conf["project_id"].GetString();
std::string API_KEY = sb_creds_conf["api_key"].GetString();
std::string TABLE = sb_creds_conf["position_table"].GetString();

// Helper method to get how many minutes back from the present.
// Formats it in a way that the postgres will understand.
std::string cutoff_iso8601_utc(int minutes_back) {
  using namespace std::chrono;
  auto now = system_clock::now();
  auto cutoff = now - minutes(minutes_back);
  std::time_t t = system_clock::to_time_t(cutoff);
  std::tm tm = *gmtime(&t);
  std::ostringstream oss;
  oss << std::put_time(&tm, "%Y-%m-%dT%H:%M:%SZ");
  return oss.str();
}

void read_supabase() {

  std::string BASE = fmt::format("https://{}.supabase.co/rest/v1", PROJECT_ID);
  std::string BEARER = "Bearer " + API_KEY;

  std::string cutoff_time = cutoff_iso8601_utc(150000);
  std::cout << "Cutoff time is " << cutoff_time << std::endl;

  cpr::Response res = cpr::Get(cpr::Url{BASE + "/" + TABLE},
                               cpr::Header{{"apikey", API_KEY}, {"Authorization", BEARER}},
                               cpr::Parameters{{"select", "*"},
                                               // Check that the filter works.
                                               {"symbol", "eq.unit:ES"},
                                               {"created_at", "gte." + cutoff_time},
                                               {"limit", "100"},
                                               {"order", "id.asc"}});

  if (res.status_code != 200) {
    std::cerr << "Error: " << res.status_code << " " << res.text << "\n";
    return;
  }

  std::cout << "Rows: " << res.text << "\n"; // JSON array

  // Try to parse it in rapidjson, get out the positions.
  rapidjson::Document response_doc;
  response_doc.Parse(res.text.c_str());

  // Let's parse through and find all the ones that say es?

  for (auto& one_part : response_doc.GetArray()) {
    if (one_part.HasMember("symbol")) {
      std::cout << one_part["symbol"].GetString() << std::endl;
    }
  }
}

void write_supabase() {
  // Cool, this works.

  std::string BASE = fmt::format("https://{}.supabase.co/rest/v1", PROJECT_ID);
  std::string BEARER = "Bearer " + API_KEY;

  rapidjson::Document doc;
  doc.SetObject();
  auto& allocator = doc.GetAllocator();

  doc.AddMember("strat_id", rapidjson::Value("erg", allocator), allocator);
  doc.AddMember("symbol", rapidjson::Value("unit:NQ", allocator), allocator);
  doc.AddMember("global_pos", 100, allocator);
  doc.AddMember("my_pos", 20, allocator);
  doc.AddMember("my_max_pos", 100, allocator);
  doc.AddMember("my_inherit_amount", 10, allocator);

  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  doc.Accept(writer);

  cpr::Response res = cpr::Post(cpr::Url{BASE + "/" + TABLE},
                                cpr::Header{{"apikey", API_KEY},
                                            {"Authorization", BEARER},
                                            {"Content-Type", "application/json"}},
                                cpr::Body{buffer.GetString()});

  if (res.status_code != 200 && res.status_code != 201) {
    std::cerr << "Error: " << res.status_code << " " << res.text << "\n";
    return;
  }

  std::cout << "Write results: " << res.text << "\n"; // JSON array
}

void hyperliquid_position() {
  // OK, one last thing I'll need. I need to check out the global
  // hyperliquid position for my specific symbol.
  const std::string api_url = "https://api.hyperliquid.xyz/info";
  // TODO: replace this with another symbol.
  const std::string user_address = "";
  const std::string symbol = "BTC";

  rapidjson::Document doc;
  doc.SetObject();
  auto& allocator = doc.GetAllocator();

  doc.AddMember("type", rapidjson::Value("clearinghouseState", allocator), allocator);
  doc.AddMember("user", rapidjson::Value(user_address.c_str(), allocator), allocator);

  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  doc.Accept(writer);

  cpr::Response res =
      cpr::Post(cpr::Url{api_url}, cpr::Header{{"Content-Type", "application/json"}},
                cpr::Body{buffer.GetString()});

  if (res.status_code != 200) {
    std::cerr << "Error " << res.status_code << ": " << res.text << "\n";
    return;
  }

  std::cout << res.text << "\n";

  // Parse JSON

  rapidjson::Document response_doc;
  response_doc.Parse(res.text.c_str());

  if (!response_doc.HasMember("assetPositions")) {
    std::cerr << "No assetPositions field in response\n";
    return;
  }

  // Otherwise, look through.
  for (const auto& entry : response_doc["assetPositions"].GetArray()) {
    const auto& pos = entry["position"];

    if (pos["coin"].GetString() == symbol) {
      std::cout << "Position for " << symbol << ":\n" << pos["szi"].GetString() << "\n";
      return;
    }
  }

  /*
  json j = json::parse(res.text);
  if (!j.contains("assetPositions")) {
    std::cerr << "No assetPositions field in response\n";
    return ;
  }

  const auto& assetPositions = j["assetPositions"];
  for (const auto& entry : assetPositions) {
    const auto& pos = entry["position"];
    if (pos["coin"].get<std::string>() == symbol) {
      std::cout << "Position for " << symbol << ":\n"
                << pos.dump(2) << "\n";
      return 0;
    }
  }
*/
}

int main(int argc, char** argv) {
  std::string sym;

  CLI::App cmd_flags("pkdex");
  cmd_flags.add_option("--symbol", sym, "Symbol");
  PARSE(cmd_flags, argc, argv);

  // hyperliquid_position();

  read_supabase();
  // write_supabase();
}
