#include "pktrade/context/global_vars.h"
#include <cpr/cpr.h>
#include <fmt/format.h>
#include <nlohmann/json.hpp>
#include <rapidjson/document.h>

#include "pktrade/util/urls.h"
namespace pktrade::secmasterfiles {

// I get these strings from jane.
// ./jane.py --mode info --perp
// ./jane.py --mode info

// For the perps



bool loadHyperliquidInfoLive(rapidjson::Document& d) {
  // Don't need it to be a session
  std::shared_ptr<cpr::Session> session = std::make_shared<cpr::Session>();

  session->SetUrl(cpr::Url{pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_)});

  nlohmann::json payload = {
      {"type", "meta"},
  };
  session->SetBody(cpr::Body{payload.dump()});
  session->SetHeader(cpr::Header{
      {"Content-Type", "application/json"},
  });

  cpr::Response r = pktrade::urls::CprRetry::SessionPost(*session);
  if (r.status_code == 200) {
    d.Parse(r.text.c_str());
    return true;
  } else {
    // error code.
    /*
    std::cout << " POST call failed call! code " << r.status_code
              << " status_line " << r.status_line << std::endl;
              */
  }
  return false;
}

bool loadHyperliquidMidsLive(rapidjson::Document& d) {
  std::shared_ptr<cpr::Session> session = std::make_shared<cpr::Session>();

  session->SetUrl(cpr::Url{pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_)});

  // This is what I had to do without nlohmann::json
  // session->SetBody(cpr::Body{"{\"type\": \"meta\"}"});
  nlohmann::json payload = {
      {"type", "allMids"},
  };
  session->SetBody(cpr::Body{payload.dump()});
  session->SetHeader(cpr::Header{
      {"Content-Type", "application/json"},
  });

  cpr::Response r = pktrade::urls::CprRetry::SessionPost(*session);

  if (r.status_code == 200) {
    d.Parse(r.text.c_str());
    return true;
  } else {
    // error code.
    /*
    std::cout << " POST call failed call! code " << r.status_code
              << " status_line " << r.status_line << std::endl;
              */
  }
  return false;
}

// For handling unit dex.

bool loadHyperliquidHip3InfoLive(rapidjson::Document& d) {
  // Don't need it to be a session
  std::shared_ptr<cpr::Session> session = std::make_shared<cpr::Session>();

  session->SetUrl(cpr::Url{pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_)});

  std::string dex_name = "xyz"; 

  nlohmann::json payload{
      {"type", "meta"}, {"dex", dex_name}
  };
  session->SetBody(cpr::Body{payload.dump()});
  session->SetHeader(cpr::Header{
      {"Content-Type", "application/json"},
  });

  cpr::Response r = pktrade::urls::CprRetry::SessionPost(*session);
  if (r.status_code == 200) {
    d.Parse(r.text.c_str());
    int n_perps = (d.HasMember("universe") && d["universe"].IsArray())
                      ? static_cast<int>(d["universe"].Size())
                      : -1;
    std::cout << "HIP3 " << dex_name << ": loaded " << n_perps
              << " perps (" << r.text.size() << " bytes)" << std::endl;
    return true;
  } else {
    std::cout << "HIP3 " << dex_name << " request failed: status " << r.status_code
              << " body=" << r.text.substr(0, 200) << std::endl;

    // error code.
    /*
    std::cout << " POST call failed call! code " << r.status_code
              << " status_line " << r.status_line << std::endl;
              */
  }
  return false;
}

bool loadHyperliquidHip3MidsLive(rapidjson::Document& d) {
  std::shared_ptr<cpr::Session> session = std::make_shared<cpr::Session>();

  session->SetUrl(cpr::Url{pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_)});


  std::string dex_name = "xyz"; 

  // This is what I had to do without nlohmann::json
  // session->SetBody(cpr::Body{"{\"type\": \"meta\"}"});
  nlohmann::json payload{{"type", "allMids"}, {"dex", dex_name}};

  session->SetBody(cpr::Body{payload.dump()});
  session->SetHeader(cpr::Header{
      {"Content-Type", "application/json"},
  });

  cpr::Response r = pktrade::urls::CprRetry::SessionPost(*session);

  if (r.status_code == 200) {
    d.Parse(r.text.c_str());
    return true;
  } else {
    // error code.
    /*
    std::cout << " POST call failed call! code " << r.status_code
              << " status_line " << r.status_line << std::endl;
              */
  }
  return false;
}

// For handling SPOT
// For the spot guys

// This gives us coins like this:

//     {
//      "name": "PEPE",
//      "szDecimals": 2,
//      "weiDecimals": 7,
//      "index": 12,
//      "tokenId": "0x79b6e1596ea0deb2e6912ff8392c9325",
//      "isCanonical": false
//    },

bool loadHyperliquidSpotInfoLive(rapidjson::Document& d) {
  // Don't need it to be a session
  std::shared_ptr<cpr::Session> session = std::make_shared<cpr::Session>();

  std::cout << " Fetching hyperliquidspotInfo live!" << std::endl;

  session->SetUrl(cpr::Url{pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_)});

  nlohmann::json payload = {
      {"type", "spotMeta"},
  };
  session->SetBody(cpr::Body{payload.dump()});
  session->SetHeader(cpr::Header{
      {"Content-Type", "application/json"},
  });

  cpr::Response r = pktrade::urls::CprRetry::SessionPost(*session);

  if (r.status_code == 200) {
    d.Parse(r.text.c_str());
    return true;
    /*
      for (rapidjson::Value& one_sym_dct : d["universe"].GetArray()) {
        std::string symbol = one_sym_dct["name"].GetString();
        if (symbol == "USDC") {
          continue;
        }

        //std::string pair_name = fmt::format("{}/USDC", symbol);
        //int sz_precision = one_sym_dct["szDecimals"].GetDouble();

        // pair_to_sz_precision_[pair_name] = sz_precision;
      }
  */
    // Iterate through the thing and

  } else {
    // error code.
    /*
    std::cout << " POST call failed call! code " << r.status_code
              << " status_line " << r.status_line << std::endl;
              */
  }
  return false;
}

// This gives us something like
//

bool loadHyperliquidSpotMidsLive(rapidjson::Document& d) {
  std::shared_ptr<cpr::Session> session = std::make_shared<cpr::Session>();

  std::cout << " Fetching hyperliquidspotMids live!" << std::endl;

  session->SetUrl(cpr::Url{pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_)});

  // This is what I had to do without nlohmann::json
  // session->SetBody(cpr::Body{"{\"type\": \"meta\"}"});
  nlohmann::json payload = {
      {"type", "spotMetaAndAssetCtxs"},
  };
  session->SetBody(cpr::Body{payload.dump()});
  session->SetHeader(cpr::Header{
      {"Content-Type", "application/json"},
  });

  cpr::Response r = pktrade::urls::CprRetry::SessionPost(*session);

  if (r.status_code == 200) {
    d.Parse(r.text.c_str());

    // Going to take advantage of the obvservation that they
    // return this to us in sorted form. I don't really
    // want to multiple lookups here.

    /*
    std::vector<std::string> pair_names;
    for (rapidjson::Value& one_sym_dct : d[0]["tokens"].GetArray()) {
      std::string symbol = one_sym_dct["name"].GetString();
      if (symbol == "USDC") {
        continue;
      }

      std::string pair_name = fmt::format("{}/USDC", symbol);
      pair_names.push_back(pair_name);
    }

    // Next, go through the mids.
    int counter = 0;
    for (rapidjson::Value& one_sym_px : d[1].GetArray()) {
      std::string pair_name = pair_names[counter];
      double mid_px = one_sym_px["midPx"].GetDouble();

      counter += 1;
    }
*/
    return true;
  } else {
    // error code.
    /*
    std::cout << " POST call failed call! code " << r.status_code
              << " status_line " << r.status_line << std::endl;
              */
  }
  return false;
}


// Load hip3 stuff. 
// ./jane.py --dex xyz --mode info --perps
void loadHyperliquidHip3Info(rapidjson::Document& d) {
  std::string exch_info_str = R"(
{
  "universe": [
    {
      "szDecimals": 4,
      "name": "xyz:XYZ100",
      "maxLeverage": 30,
      "marginTableId": 30,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:TSLA",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:NVDA",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 4,
      "name": "xyz:GOLD",
      "maxLeverage": 25,
      "marginTableId": 25
    },
    {
      "szDecimals": 3,
      "name": "xyz:HOOD",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 2,
      "name": "xyz:INTC",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:PLTR",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:COIN",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:META",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:AAPL",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:MSFT",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:ORCL",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:GOOGL",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:AMZN",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:AMD",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:MU",
      "maxLeverage": 10,
      "marginTableId": 10,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:SNDK",
      "maxLeverage": 10,
      "marginTableId": 10,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-11-23T17:37:10.033211662"
    },
    {
      "szDecimals": 3,
      "name": "xyz:MSTR",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "lastGrowthModeChangeTime": "2026-01-14T16:05:30.519245305"
    },
    {
      "szDecimals": 3,
      "name": "xyz:CRCL",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-05T17:07:43.712558241"
    },
    {
      "szDecimals": 3,
      "name": "xyz:NFLX",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-05T17:07:43.712558241"
    },
    {
      "szDecimals": 4,
      "name": "xyz:COST",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-05T17:07:43.712558241"
    },
    {
      "szDecimals": 4,
      "name": "xyz:LLY",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-05T17:07:43.712558241"
    },
    {
      "szDecimals": 3,
      "name": "xyz:SKHX",
      "maxLeverage": 10,
      "marginTableId": 10,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-19T16:55:24.230127845"
    },
    {
      "szDecimals": 3,
      "name": "xyz:TSM",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-19T16:55:24.230127845"
    },
    {
      "szDecimals": 2,
      "name": "xyz:JPY",
      "maxLeverage": 50,
      "marginTableId": 50,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-19T16:55:24.230127845"
    },
    {
      "szDecimals": 1,
      "name": "xyz:EUR",
      "maxLeverage": 50,
      "marginTableId": 50,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-19T16:55:24.230127845"
    },
    {
      "szDecimals": 2,
      "name": "xyz:SILVER",
      "maxLeverage": 25,
      "marginTableId": 25,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2025-12-27T14:46:13.933247634"
    },
    {
      "szDecimals": 2,
      "name": "xyz:RIVN",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-06T14:22:02.431616165"
    },
    {
      "szDecimals": 3,
      "name": "xyz:BABA",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-06T14:22:02.431616165"
    },
    {
      "szDecimals": 3,
      "name": "xyz:CL",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-06T14:22:02.431616165"
    },
    {
      "szDecimals": 2,
      "name": "xyz:COPPER",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-09T00:07:27.711071079"
    },
    {
      "szDecimals": 1,
      "name": "xyz:NATGAS",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-17T00:52:17.773623405"
    },
    {
      "szDecimals": 3,
      "name": "xyz:URANIUM",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-26T16:47:08.614356922"
    },
    {
      "szDecimals": 4,
      "name": "xyz:ALUMINIUM",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-21T21:02:05.395415841"
    },
    {
      "szDecimals": 3,
      "name": "xyz:SMSN",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-26T16:47:08.614356922"
    },
    {
      "szDecimals": 4,
      "name": "xyz:PLATINUM",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-26T16:47:08.614356922"
    },
    {
      "szDecimals": 2,
      "name": "xyz:USAR",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-26T16:47:08.614356922"
    },
    {
      "szDecimals": 2,
      "name": "xyz:CRWV",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-26T23:31:34.059495489"
    },
    {
      "szDecimals": 2,
      "name": "xyz:URNM",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-29T14:25:47.800206028"
    },
    {
      "szDecimals": 4,
      "name": "xyz:PALLADIUM",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-01-31T00:37:41.755544137"
    },
    {
      "szDecimals": 2,
      "name": "xyz:DXY",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-01T17:33:54.214113650"
    },
    {
      "szDecimals": 2,
      "name": "xyz:GME",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-03T19:32:57.961597289"
    },
    {
      "szDecimals": 4,
      "name": "xyz:KR200",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-05T03:32:50.957071742"
    },
    {
      "szDecimals": 3,
      "name": "xyz:SOFTBANK",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-10T19:10:39.792893920"
    },
    {
      "szDecimals": 5,
      "name": "xyz:JP225",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-12T00:07:23.069735778"
    },
    {
      "szDecimals": 3,
      "name": "xyz:HYUNDAI",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-18T01:46:52.673000860"
    },
    {
      "szDecimals": 3,
      "name": "xyz:KIOXIA",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-25T01:02:01.358848807"
    },
    {
      "szDecimals": 3,
      "name": "xyz:EWY",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-02-28T21:59:11.850632195"
    },
    {
      "szDecimals": 3,
      "name": "xyz:EWJ",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-02T00:41:25.683109868"
    },
    {
      "szDecimals": 2,
      "name": "xyz:BRENTOIL",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-04T14:33:56.611209792"
    },
    {
      "szDecimals": 2,
      "name": "xyz:VIX",
      "maxLeverage": 3,
      "marginTableId": 3,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-09T19:53:24.389395191"
    },
    {
      "szDecimals": 2,
      "name": "xyz:HIMS",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-15T17:57:18.478880296"
    },
    {
      "szDecimals": 3,
      "name": "xyz:SP500",
      "maxLeverage": 50,
      "marginTableId": 50,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-18T11:38:49.335772800"
    },
    {
      "szDecimals": 2,
      "name": "xyz:DKNG",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-23T12:47:36.299219957"
    },
    {
      "szDecimals": 3,
      "name": "xyz:LITE",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-27T21:12:35.323224713"
    },
    {
      "szDecimals": 0,
      "name": "xyz:CORN",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-29T19:28:21.135127220"
    },
    {
      "szDecimals": 2,
      "name": "xyz:XLE",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-03-31T19:51:50.241985327"
    },
    {
      "szDecimals": 0,
      "name": "xyz:WHEAT",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-02T02:42:07.320427073"
    },
    {
      "szDecimals": 1,
      "name": "xyz:TTF",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-03T19:07:01.933950208"
    },
    {
      "szDecimals": 2,
      "name": "xyz:BX",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-06T15:39:22.032937119"
    },
    {
      "szDecimals": 0,
      "name": "xyz:PURRDAT",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "lastGrowthModeChangeTime": "2026-05-09T05:06:30.219278272"
    },
    {
      "szDecimals": 2,
      "name": "xyz:MRVL",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-11T05:57:11.042604593"
    },
    {
      "szDecimals": 2,
      "name": "xyz:RKLB",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-13T15:14:54.385823035"
    },
    {
      "szDecimals": 1,
      "name": "xyz:BIRD",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-15T21:47:45.104299047"
    },
    {
      "szDecimals": 1,
      "name": "xyz:VOL",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-17T15:51:21.894541784"
    },
    {
      "szDecimals": 1,
      "name": "xyz:DRAM",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-22T14:35:27.161106929"
    },
    {
      "szDecimals": 2,
      "name": "xyz:CBRS",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-24T01:16:48.392583764"
    },
    {
      "szDecimals": 2,
      "name": "xyz:EWZ",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-27T15:13:21.741505858"
    },
    {
      "szDecimals": 3,
      "name": "xyz:KRW",
      "maxLeverage": 50,
      "marginTableId": 50,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-04-30T23:33:46.845378252"
    },
    {
      "szDecimals": 3,
      "name": "xyz:ZM",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-03T17:13:06.829773905"
    },
    {
      "szDecimals": 2,
      "name": "xyz:EBAY",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-05T14:22:12.576535126"
    },
    {
      "szDecimals": 0,
      "name": "xyz:H100",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-07T00:49:40.291714731"
    },
    {
      "szDecimals": 4,
      "name": "xyz:NIFTY",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-09T05:04:37.536668407"
    },
    {
      "szDecimals": 2,
      "name": "xyz:ARM",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-11T15:05:18.433457126"
    },
    {
      "szDecimals": 2,
      "name": "xyz:EWT",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-13T15:30:23.954993706"
    },
    {
      "szDecimals": 0,
      "name": "xyz:GBP",
      "maxLeverage": 50,
      "marginTableId": 50,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-16T01:31:07.460142934"
    },
    {
      "szDecimals": 2,
      "name": "xyz:SPCX",
      "maxLeverage": 20,
      "marginTableId": 20,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-17T14:46:21.424795458"
    },
    {
      "szDecimals": 5,
      "name": "xyz:IBOV",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-20T00:21:19.388863967"
    },
    {
      "szDecimals": 3,
      "name": "xyz:ASML",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-21T17:10:42.165373019"
    },
    {
      "szDecimals": 2,
      "name": "xyz:MINIMAX",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-25T03:02:14.645754442"
    },
    {
      "szDecimals": 1,
      "name": "xyz:BB",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-26T11:58:14.597789923"
    },
    {
      "szDecimals": 2,
      "name": "xyz:QNT",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-27T18:45:40.981927871"
    },
    {
      "szDecimals": 2,
      "name": "xyz:DELL",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-05-29T14:13:26.025311074"
    },
    {
      "szDecimals": 2,
      "name": "xyz:IBM",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-01T01:54:01.428026772"
    },
    {
      "szDecimals": 2,
      "name": "xyz:AVGO",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-03T02:16:30.639600468"
    },
    {
      "szDecimals": 2,
      "name": "xyz:NOW",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-04T12:20:18.819600270"
    },
    {
      "szDecimals": 2,
      "name": "xyz:NBIS",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-05T18:52:56.692372884"
    },
    {
      "szDecimals": 3,
      "name": "xyz:WDC",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-07T23:35:34.038712300"
    },
    {
      "szDecimals": 2,
      "name": "xyz:NOK",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-10T17:30:46.846902410"
    },
    {
      "szDecimals": 3,
      "name": "xyz:SMH",
      "maxLeverage": 20,
      "marginTableId": 20,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-12T01:31:23.485005655"
    },
    {
      "szDecimals": 2,
      "name": "xyz:BE",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-15T13:23:22.863731938"
    },
    {
      "szDecimals": 3,
      "name": "xyz:ZHIPU",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-18T19:36:57.128750613"
    },
    {
      "szDecimals": 2,
      "name": "xyz:QCOM",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-21T01:24:18.359451315"
    },
    {
      "szDecimals": 1,
      "name": "xyz:STRC",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross"
    },
    {
      "szDecimals": 2,
      "name": "xyz:BOT",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-25T03:09:24.902520455"
    },
    {
      "szDecimals": 3,
      "name": "xyz:AMAT",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-26T17:58:24.444075483"
    },
    {
      "szDecimals": 2,
      "name": "xyz:IBIDEN",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-06-29T15:03:39.631650333"
    },
    {
      "szDecimals": 2,
      "name": "xyz:GIGADEV",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-07-02T17:26:59.213958008"
    },
    {
      "szDecimals": 2,
      "name": "xyz:SHAZ",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-07-06T15:56:01.551322287"
    },
    {
      "szDecimals": 2,
      "name": "xyz:SKHY",
      "maxLeverage": 10,
      "marginTableId": 10,
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-07-08T12:37:52.833102267"
    },
    {
      "szDecimals": 2,
      "name": "xyz:KSTR",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-07-12T22:55:25.293073960"
    },
    {
      "szDecimals": 1,
      "name": "xyz:CXMT",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "marginMode": "noCross",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-07-14T17:24:08.849924124"
    },
    {
      "szDecimals": 3,
      "name": "xyz:GEV",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-07-21T00:01:45.341962512"
    },
    {
      "szDecimals": 1,
      "name": "xyz:KORU",
      "maxLeverage": 10,
      "marginTableId": 10,
      "onlyIsolated": true,
      "isDelisted": true,
      "marginMode": "strictIsolated",
      "growthMode": "enabled",
      "lastGrowthModeChangeTime": "2026-08-01T12:05:27.720981723"
    }
  ],
  "marginTables": [
    [
      50,
      {
        "description": "",
        "marginTiers": [
          {
            "lowerBound": "0.0",
            "maxLeverage": 50
          }
        ]
      }
    ]
  ],
  "collateralToken": 0
}
    )";

  d.Parse(exch_info_str.c_str());
}

// I got this with a call of jane.py:
// ./jane.py --dex xyz --mode mids --perps
// Remember to update when they launch more.
void loadHyperliquidHip3Mids(rapidjson::Document& d) {
  std::string exch_info_str = R"(
{
  "xyz:AAPL": "306.455",
  "xyz:ALUMINIUM": "3080.0",
  "xyz:AMAT": "504.34",
  "xyz:AMD": "470.615",
  "xyz:AMZN": "283.88",
  "xyz:ARM": "231.01",
  "xyz:ASML": "1625.75",
  "xyz:AVGO": "381.28",
  "xyz:BABA": "127.965",
  "xyz:BB": "8.39585",
  "xyz:BE": "216.86",
  "xyz:BIRD": "2.1993",
  "xyz:BOT": "26.746",
  "xyz:BRENTOIL": "83.496",
  "xyz:BX": "129.975",
  "xyz:CBRS": "204.735",
  "xyz:CL": "79.444",
  "xyz:COIN": "146.165",
  "xyz:COPPER": "6.4747",
  "xyz:CORN": "4.61",
  "xyz:COST": "958.69",
  "xyz:CRCL": "58.572",
  "xyz:CRWV": "79.12",
  "xyz:CXMT": "7.70365",
  "xyz:DELL": "403.97",
  "xyz:DKNG": "23.501",
  "xyz:DRAM": "49.07",
  "xyz:DXY": "97.15",
  "xyz:EBAY": "110.52",
  "xyz:EUR": "1.1519",
  "xyz:EWJ": "92.577",
  "xyz:EWT": "96.8",
  "xyz:EWY": "155.42",
  "xyz:EWZ": "36.315",
  "xyz:GBP": "1.3448",
  "xyz:GEV": "1080.0",
  "xyz:GIGADEV": "50.5245",
  "xyz:GME": "19.338",
  "xyz:GOLD": "4035.55",
  "xyz:GOOGL": "368.41",
  "xyz:H100": "2.6",
  "xyz:HIMS": "30.362",
  "xyz:HOOD": "89.443",
  "xyz:HYUNDAI": "275.045",
  "xyz:IBIDEN": "96.534",
  "xyz:IBM": "225.56",
  "xyz:IBOV": "175000.0",
  "xyz:INTC": "87.9155",
  "xyz:JP225": "62663.5",
  "xyz:JPY": "156.515",
  "xyz:KIOXIA": "300.29",
  "xyz:KORU": "15.0",
  "xyz:KR200": "954.22",
  "xyz:KRW": "1428.3",
  "xyz:KSTR": "22.37",
  "xyz:LITE": "726.46",
  "xyz:LLY": "1126.2",
  "xyz:META": "592.625",
  "xyz:MINIMAX": "32.2345",
  "xyz:MRVL": "183.945",
  "xyz:MSFT": "486.76",
  "xyz:MSTR": "94.483",
  "xyz:MU": "798.015",
  "xyz:NATGAS": "2.75715",
  "xyz:NBIS": "204.175",
  "xyz:NFLX": "73.272",
  "xyz:NIFTY": "24250.0",
  "xyz:NOK": "9.0567",
  "xyz:NOW": "116.28",
  "xyz:NVDA": "205.08",
  "xyz:ORCL": "136.465",
  "xyz:PALLADIUM": "1257.85",
  "xyz:PLATINUM": "1624.35",
  "xyz:PLTR": "124.805",
  "xyz:PURRDAT": "6.4221",
  "xyz:QCOM": "146.245",
  "xyz:QNT": "53.7465",
  "xyz:RIVN": "15.5705",
  "xyz:RKLB": "67.3845",
  "xyz:SHAZ": "49.5335",
  "xyz:SILVER": "57.2065",
  "xyz:SKHX": "1063.75",
  "xyz:SKHY": "137.745",
  "xyz:SMH": "534.645",
  "xyz:SMSN": "160.915",
  "xyz:SNDK": "1215.65",
  "xyz:SOFTBANK": "32.72",
  "xyz:SP500": "7560.15",
  "xyz:SPCX": "111.025",
  "xyz:STRC": "90.915",
  "xyz:TSLA": "317.82",
  "xyz:TSM": "400.785",
  "xyz:TTF": "57.0",
  "xyz:URANIUM": "85.0",
  "xyz:URNM": "49.7345",
  "xyz:USAR": "15.3545",
  "xyz:VIX": "20.0",
  "xyz:VOL": "15.0",
  "xyz:WDC": "508.73",
  "xyz:WHEAT": "6.3",
  "xyz:XLE": "59.0035",
  "xyz:XYZ100": "28485.5",
  "xyz:ZHIPU": "121.8",
  "xyz:ZM": "98.526"
}
    )";

  d.Parse(exch_info_str.c_str());
}

void loadHyperliquidInfo(rapidjson::Document& d) {
  std::string exch_info_str = R"(
{
  "universe": [
    {
      "szDecimals": 5,
      "name": "BTC",
      "maxLeverage": 50,
      "onlyIsolated": false
    },
    {
      "szDecimals": 4,
      "name": "ETH",
      "maxLeverage": 50,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "ATOM",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "MATIC",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "DYDX",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "SOL",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "AVAX",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 3,
      "name": "BNB",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "APE",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "OP",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "LTC",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "ARB",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "DOGE",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "INJ",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "SUI",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "kPEPE",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "CRV",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "LDO",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "LINK",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "STX",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "RNDR",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "CFX",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "FTM",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "GMX",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "SNX",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "XRP",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 3,
      "name": "BCH",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "APT",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "AAVE",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "COMP",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 4,
      "name": "MKR",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "WLD",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "FXS",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "HPOS",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 0,
      "name": "RLB",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 3,
      "name": "UNIBOT",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 0,
      "name": "YGG",
      "maxLeverage": 3,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "TRX",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "kSHIB",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "UNI",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "SEI",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "RUNE",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "OX",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 1,
      "name": "FRIEND",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 0,
      "name": "SHIA",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 1,
      "name": "CYBER",
      "maxLeverage": 3,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "ZRO",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 0,
      "name": "BLZ",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "DOT",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "BANANA",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 2,
      "name": "TRB",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "FTT",
      "maxLeverage": 3,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "LOOM",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "OGN",
      "maxLeverage": 3,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "RDNT",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "ARK",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "BNT",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "CANTO",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "REQ",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "BIGTIME",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "KAS",
      "maxLeverage": 3,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "ORBS",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "BLUR",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "TIA",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "BSV",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "ADA",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "TON",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "MINA",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "POLYX",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "GAS",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "PENDLE",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "STG",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "FET",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "STRAX",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "NEAR",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "MEME",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "ORDI",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "BADGER",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "NEO",
      "maxLeverage": 20,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "ZEN",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "FIL",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "PYTH",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "SUSHI",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "ILV",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "IMX",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "kBONK",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "GMT",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "SUPER",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "USTC",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "NFTI",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 0,
      "name": "JUP",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "kLUNC",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "RSR",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "GALA",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "JTO",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "NTRN",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "ACE",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "MAV",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "WIF",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "CAKE",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "PEOPLE",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "ENS",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "ETC",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "XAI",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "MANTA",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "UMA",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "ONDO",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "ALT",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "ZETA",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "DYM",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "MAVIA",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "W",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 5,
      "name": "PANDORA",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 1,
      "name": "STRK",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "PIXEL",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "AI",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 3,
      "name": "TAO",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "AR",
      "maxLeverage": 10,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "MYRO",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "kFLOKI",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "BOME",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "ETHFI",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "ENA",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "MNT",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "TNSR",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 1,
      "name": "SAGA",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "MERL",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "HBAR",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "POPCAT",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "OMNI",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 2,
      "name": "EIGEN",
      "maxLeverage": 3,
      "onlyIsolated": true
    },
    {
      "szDecimals": 0,
      "name": "REZ",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "NOT",
      "maxLeverage": 5,
      "onlyIsolated": false
    },
    {
      "szDecimals": 0,
      "name": "HYPE",
      "maxLeverage": 10,
      "onlyIsolated": false
    },

    {
      "szDecimals": 0,
      "name": "TURBO",
      "maxLeverage": 5,
      "onlyIsolated": false
    }

  ]
}

    )";

  d.Parse(exch_info_str.c_str());
}

// Grabs mid prices, which is really just a way of estimating the
// ticksize of the thing. 7
// I got this with a call of jane.py:
// ~/tradefi/pkt_1/overmind/strat_main/hyperliquid/jane.py --mode mids
// Remember to update when they launch more.
void loadHyperliquidMids(rapidjson::Document& d) {
  std::string exch_info_str = R"(
{
  "AAVE": "102.775",
  "ACE": "6.0136",
  "ADA": "0.45816",
  "AI": "1.2041",
  "ALT": "0.3565",
  "APE": "1.3197",
  "APT": "9.2342",
  "AR": "44.43",
  "ARB": "1.10755",
  "ARK": "0.7793",
  "ATOM": "8.5588",
  "AVAX": "36.146",
  "BADGER": "4.9645",
  "BANANA": "41.084",
  "BCH": "477.45",
  "BIGTIME": "0.21908",
  "BLUR": "0.40858",
  "BLZ": "0.251315",
  "BNB": "696.485",
  "BNT": "0.824145",
  "BOME": "0.013347",
  "BSV": "62.785",
  "BTC": "70814.5",
  "CAKE": "3.11415",
  "CANTO": "0.15776",
  "CFX": "0.22421",
  "COMP": "59.6335",
  "CRV": "0.46038",
  "CYBER": "9.031",
  "DOGE": "0.16221",
  "DOT": "7.18695",
  "DYDX": "2.0674",
  "DYM": "3.18045",
  "EIGEN": "8.807",
  "ENA": "0.95095",
  "ENS": "24.664",
  "ETC": "29.4345",
  "ETH": "3802.95",
  "ETHFI": "4.66095",
  "FET": "2.1567",
  "FIL": "5.9964",
  "FRIEND": "4.72",
  "FTM": "0.83038",
  "FTT": "1.59915",
  "FXS": "4.76655",
  "GALA": "0.047024",
  "GAS": "5.04005",
  "GMT": "0.239715",
  "GMX": "38.0255",
  "HBAR": "0.102875",
  "HPOS": "0.14229",
  "HYPE": "30.14",
  "ILV": "89.636",
  "IMX": "2.2896",
  "INJ": "26.3735",
  "JTO": "3.63835",
  "JUP": "1.15165",
  "KAS": "0.18769",
  "LDO": "2.25365",
  "LINK": "17.764",
  "LOOM": "0.082749",
  "LTC": "83.776",
  "MANTA": "1.8289",
  "MATIC": "0.71039",
  "MAV": "0.45491",
  "MAVIA": "3.40165",
  "MEME": "0.029751",
  "MERL": "0.51039",
  "MINA": "0.863165",
  "MKR": "2662.3",
  "MNT": "0.97745",
  "MYRO": "0.262985",
  "NEAR": "7.48985",
  "NEO": "15.1195",
  "NFTI": "8.919",
  "NOT": "0.020305",
  "NTRN": "0.7868",
  "OGN": "0.160365",
  "OMNI": "19.92",
  "ONDO": "1.37605",
  "OP": "2.49415",
  "ORBS": "0.032649",
  "ORDI": "56.351",
  "OX": "0.010308",
  "PANDORA": "3710.5",
  "PENDLE": "6.05825",
  "PEOPLE": "0.10343",
  "PIXEL": "0.49221",
  "POLYX": "0.450645",
  "POPCAT": "0.48784",
  "PURR/USDC": "0.187005",
  "PYTH": "0.46335",
  "RDNT": "0.214725",
  "REQ": "0.14121",
  "REZ": "0.17099",
  "RLB": "0.066261",
  "RNDR": "10.385",
  "RSR": "0.008567",
  "RUNE": "6.23775",
  "SAGA": "2.80595",
  "SEI": "0.519165",
  "SHIA": "0.001533",
  "SNX": "2.78085",
  "SOL": "172.575",
  "STG": "0.647985",
  "STRAX": "1.5829",
  "STRK": "1.3256",
  "STX": "2.25965",
  "SUI": "1.0882",
  "SUPER": "1.1018",
  "SUSHI": "1.16455",
  "TAO": "408.445",
  "TIA": "10.578",
  "TNSR": "1.3312",
  "TON": "7.22885",
  "TRB": "104.29",
  "TRX": "0.114195",
  "TURBO": "0.005857",
  "UMA": "3.31705",
  "UNI": "11.155",
  "UNIBOT": "12.511",
  "USTC": "0.0237",
  "W": "0.63759",
  "WIF": "3.40275",
  "WLD": "4.9263",
  "XAI": "0.88314",
  "XRP": "0.52649",
  "YGG": "1.05285",
  "ZEN": "9.3471",
  "ZETA": "1.338",
  "ZRO": "4.9251",
  "kBONK": "0.034076",
  "kFLOKI": "0.329995",
  "kLUNC": "0.118745",
  "kPEPE": "0.014279",
  "kSHIB": "0.025753"
}
    )";

  d.Parse(exch_info_str.c_str());
}

// The verbatim thing gotten from spotMeta.
void loadHyperliquidSpotInfo(rapidjson::Document& d) {
  std::string exch_info_str = R"(
{
  "universe": [
    {
      "tokens": [
        1,
        0
      ],
      "name": "PURR/USDC",
      "index": 0,
      "isCanonical": true
    },
    {
      "tokens": [
        2,
        0
      ],
      "name": "@1",
      "index": 1,
      "isCanonical": false
    },
    {
      "tokens": [
        3,
        0
      ],
      "name": "@2",
      "index": 2,
      "isCanonical": false
    },
    {
      "tokens": [
        4,
        0
      ],
      "name": "@3",
      "index": 3,
      "isCanonical": false
    },
    {
      "tokens": [
        5,
        0
      ],
      "name": "@4",
      "index": 4,
      "isCanonical": false
    },
    {
      "tokens": [
        6,
        0
      ],
      "name": "@5",
      "index": 5,
      "isCanonical": false
    },
    {
      "tokens": [
        7,
        0
      ],
      "name": "@6",
      "index": 6,
      "isCanonical": false
    },
    {
      "tokens": [
        8,
        0
      ],
      "name": "@7",
      "index": 7,
      "isCanonical": false
    },
    {
      "tokens": [
        9,
        0
      ],
      "name": "@8",
      "index": 8,
      "isCanonical": false
    },
    {
      "tokens": [
        10,
        0
      ],
      "name": "@9",
      "index": 9,
      "isCanonical": false
    },
    {
      "tokens": [
        11,
        0
      ],
      "name": "@10",
      "index": 10,
      "isCanonical": false
    },
    {
      "tokens": [
        12,
        0
      ],
      "name": "@11",
      "index": 11,
      "isCanonical": false
    },
    {
      "tokens": [
        13,
        0
      ],
      "name": "@12",
      "index": 12,
      "isCanonical": false
    },
    {
      "tokens": [
        14,
        0
      ],
      "name": "@13",
      "index": 13,
      "isCanonical": false
    }
  ],
  "tokens": [
    {
      "name": "USDC",
      "szDecimals": 8,
      "weiDecimals": 8,
      "index": 0,
      "tokenId": "0x6d1e7cde53ba9467b783cb7c530ce054",
      "isCanonical": true
    },
    {
      "name": "PURR",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 1,
      "tokenId": "0xc1fb593aeffbeb02f85e0308e9956a90",
      "isCanonical": true
    },
    {
      "name": "HFUN",
      "szDecimals": 2,
      "weiDecimals": 8,
      "index": 2,
      "tokenId": "0xbaf265ef389da684513d98d68edf4eae",
      "isCanonical": false
    },
    {
      "name": "LICK",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 3,
      "tokenId": "0xba3aaf468f793d9b42fd3328e24f1de9",
      "isCanonical": false
    },
    {
      "name": "MANLET",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 4,
      "tokenId": "0xe9ced9225d2a69ccc8d6a5b224524b99",
      "isCanonical": false
    },
    {
      "name": "JEFF",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 5,
      "tokenId": "0xfcf28885456bf7e7cbe5b7a25407c5bc",
      "isCanonical": false
    },
    {
      "name": "SIX",
      "szDecimals": 2,
      "weiDecimals": 8,
      "index": 6,
      "tokenId": "0x50a9391b4a40caffbe8b16303b95a0c1",
      "isCanonical": false
    },
    {
      "name": "WAGMI",
      "szDecimals": 2,
      "weiDecimals": 8,
      "index": 7,
      "tokenId": "0x649efea44690cf88d464f512bc7e2818",
      "isCanonical": false
    },
    {
      "name": "CAPPY",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 8,
      "tokenId": "0x3f8abf62220007cc7ab6d33ef2963d88",
      "isCanonical": false
    },
    {
      "name": "POINTS",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 9,
      "tokenId": "0xbb03842e1f71ed27ed8fa012b29affd4",
      "isCanonical": false
    },
    {
      "name": "TRUMP",
      "szDecimals": 2,
      "weiDecimals": 7,
      "index": 10,
      "tokenId": "0x368cb581f0d51e21aa19996d38ffdf6f",
      "isCanonical": false
    },
    {
      "name": "GMEOW",
      "szDecimals": 0,
      "weiDecimals": 8,
      "index": 11,
      "tokenId": "0x07615193eaa63d1da6feda6e0ac9e014",
      "isCanonical": false
    },
    {
      "name": "PEPE",
      "szDecimals": 2,
      "weiDecimals": 7,
      "index": 12,
      "tokenId": "0x79b6e1596ea0deb2e6912ff8392c9325",
      "isCanonical": false
    },
    {
      "name": "XULIAN",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 13,
      "tokenId": "0x6cc648be7e4c38a8c7fcd8bfa6714127",
      "isCanonical": false
    },
    {
      "name": "RUG",
      "szDecimals": 0,
      "weiDecimals": 5,
      "index": 14,
      "tokenId": "0x4978f3f49f30776d9d7397b873223c2d",
      "isCanonical": false
    }
  ]
}

    )";

  d.Parse(exch_info_str.c_str());

  // Process them.
}

// Note that this doesn't call get_mids_old(),
// which actually returns the nicer version of things.
// Grabs mid prices, which is really just a way of estimating the
// ticksize of the thing. 7
// I got this with a call of jane.py:
// ~/tradefi/pkt_1/overmind/strat_main/hyperliquid/jane.py --mode mids
// Remember to update when they launch more.
void loadHyperliquidSpotMids(rapidjson::Document& d) {
  std::string exch_info_str = R"(
[
  {
    "universe": [
      {
        "tokens": [
          1,
          0
        ],
        "name": "PURR/USDC",
        "index": 0,
        "isCanonical": true
      },
      {
        "tokens": [
          2,
          0
        ],
        "name": "@1",
        "index": 1,
        "isCanonical": false
      },
      {
        "tokens": [
          3,
          0
        ],
        "name": "@2",
        "index": 2,
        "isCanonical": false
      },
      {
        "tokens": [
          4,
          0
        ],
        "name": "@3",
        "index": 3,
        "isCanonical": false
      },
      {
        "tokens": [
          5,
          0
        ],
        "name": "@4",
        "index": 4,
        "isCanonical": false
      },
      {
        "tokens": [
          6,
          0
        ],
        "name": "@5",
        "index": 5,
        "isCanonical": false
      },
      {
        "tokens": [
          7,
          0
        ],
        "name": "@6",
        "index": 6,
        "isCanonical": false
      },
      {
        "tokens": [
          8,
          0
        ],
        "name": "@7",
        "index": 7,
        "isCanonical": false
      },
      {
        "tokens": [
          9,
          0
        ],
        "name": "@8",
        "index": 8,
        "isCanonical": false
      },
      {
        "tokens": [
          10,
          0
        ],
        "name": "@9",
        "index": 9,
        "isCanonical": false
      },
      {
        "tokens": [
          11,
          0
        ],
        "name": "@10",
        "index": 10,
        "isCanonical": false
      },
      {
        "tokens": [
          12,
          0
        ],
        "name": "@11",
        "index": 11,
        "isCanonical": false
      },
      {
        "tokens": [
          13,
          0
        ],
        "name": "@12",
        "index": 12,
        "isCanonical": false
      },
      {
        "tokens": [
          14,
          0
        ],
        "name": "@13",
        "index": 13,
        "isCanonical": false
      }
    ],
    "tokens": [
      {
        "name": "USDC",
        "szDecimals": 8,
        "weiDecimals": 8,
        "index": 0,
        "tokenId": "0x6d1e7cde53ba9467b783cb7c530ce054",
        "isCanonical": true
      },
      {
        "name": "PURR",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 1,
        "tokenId": "0xc1fb593aeffbeb02f85e0308e9956a90",
        "isCanonical": true
      },
      {
        "name": "HFUN",
        "szDecimals": 2,
        "weiDecimals": 8,
        "index": 2,
        "tokenId": "0xbaf265ef389da684513d98d68edf4eae",
        "isCanonical": false
      },
      {
        "name": "LICK",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 3,
        "tokenId": "0xba3aaf468f793d9b42fd3328e24f1de9",
        "isCanonical": false
      },
      {
        "name": "MANLET",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 4,
        "tokenId": "0xe9ced9225d2a69ccc8d6a5b224524b99",
        "isCanonical": false
      },
      {
        "name": "JEFF",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 5,
        "tokenId": "0xfcf28885456bf7e7cbe5b7a25407c5bc",
        "isCanonical": false
      },
      {
        "name": "SIX",
        "szDecimals": 2,
        "weiDecimals": 8,
        "index": 6,
        "tokenId": "0x50a9391b4a40caffbe8b16303b95a0c1",
        "isCanonical": false
      },
      {
        "name": "WAGMI",
        "szDecimals": 2,
        "weiDecimals": 8,
        "index": 7,
        "tokenId": "0x649efea44690cf88d464f512bc7e2818",
        "isCanonical": false
      },
      {
        "name": "CAPPY",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 8,
        "tokenId": "0x3f8abf62220007cc7ab6d33ef2963d88",
        "isCanonical": false
      },
      {
        "name": "POINTS",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 9,
        "tokenId": "0xbb03842e1f71ed27ed8fa012b29affd4",
        "isCanonical": false
      },
      {
        "name": "TRUMP",
        "szDecimals": 2,
        "weiDecimals": 7,
        "index": 10,
        "tokenId": "0x368cb581f0d51e21aa19996d38ffdf6f",
        "isCanonical": false
      },
      {
        "name": "GMEOW",
        "szDecimals": 0,
        "weiDecimals": 8,
        "index": 11,
        "tokenId": "0x07615193eaa63d1da6feda6e0ac9e014",
        "isCanonical": false
      },
      {
        "name": "PEPE",
        "szDecimals": 2,
        "weiDecimals": 7,
        "index": 12,
        "tokenId": "0x79b6e1596ea0deb2e6912ff8392c9325",
        "isCanonical": false
      },
      {
        "name": "XULIAN",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 13,
        "tokenId": "0x6cc648be7e4c38a8c7fcd8bfa6714127",
        "isCanonical": false
      },
      {
        "name": "RUG",
        "szDecimals": 0,
        "weiDecimals": 5,
        "index": 14,
        "tokenId": "0x4978f3f49f30776d9d7397b873223c2d",
        "isCanonical": false
      }
    ]
  },
  [
    {
      "prevDayPx": "0.1987",
      "dayNtlVlm": "9213082.91955",
      "markPx": "0.1828",
      "midPx": "0.182695",
      "circulatingSupply": "599213422.54674995",
      "coin": "PURR/USDC"
    },
    {
      "prevDayPx": "12.13",
      "dayNtlVlm": "456595.1512",
      "markPx": "11.518",
      "midPx": "11.5095",
      "circulatingSupply": "999585.0515728",
      "coin": "@1"
    },
    {
      "prevDayPx": "0.00010394",
      "dayNtlVlm": "416553.578055",
      "markPx": "0.00009388",
      "midPx": "0.00009374",
      "circulatingSupply": "6889348399.3898201",
      "coin": "@2"
    },
    {
      "prevDayPx": "0.041682",
      "dayNtlVlm": "937863.113298",
      "markPx": "0.040912",
      "midPx": "0.0409425",
      "circulatingSupply": "9930826.63941",
      "coin": "@3"
    },
    {
      "prevDayPx": "1.7172",
      "dayNtlVlm": "698481.5799",
      "markPx": "1.7982",
      "midPx": "1.79545",
      "circulatingSupply": "998582.97345",
      "coin": "@4"
    },
    {
      "prevDayPx": "0.7858",
      "dayNtlVlm": "2899569.432946",
      "markPx": "0.79288",
      "midPx": "0.791695",
      "circulatingSupply": "6659974.49925132",
      "coin": "@5"
    },
    {
      "prevDayPx": "0.001876",
      "dayNtlVlm": "480563.901248",
      "markPx": "0.001774",
      "midPx": "0.001776",
      "circulatingSupply": "418989729.38579726",
      "coin": "@6"
    },
    {
      "prevDayPx": "0.00058",
      "dayNtlVlm": "2718.686352",
      "markPx": "0.00021",
      "midPx": "0.00059629",
      "circulatingSupply": "999868634.56254995",
      "coin": "@7"
    },
    {
      "prevDayPx": "0.0070146",
      "dayNtlVlm": "199430.858396",
      "markPx": "0.0063939",
      "midPx": "0.00640345",
      "circulatingSupply": "99944522.71342",
      "coin": "@8"
    },
    {
      "prevDayPx": "0.009501",
      "dayNtlVlm": "74138.631873",
      "markPx": "0.008442",
      "midPx": "0.008429",
      "circulatingSupply": "99905414.6858504",
      "coin": "@9"
    },
    {
      "prevDayPx": "0.00089285",
      "dayNtlVlm": "72079.6174013",
      "markPx": "0.00075045",
      "midPx": "0.00074933",
      "circulatingSupply": "999812327.06394243",
      "coin": "@10"
    },
    {
      "prevDayPx": "0.003",
      "dayNtlVlm": "10196.137688",
      "markPx": "0.0039",
      "midPx": "0.003317",
      "circulatingSupply": "99992683.69253901",
      "coin": "@11"
    },
    {
      "prevDayPx": "0.00001441",
      "dayNtlVlm": "195047.451173",
      "markPx": "0.00054772",
      "midPx": "0.00054854",
      "circulatingSupply": "694106076.36664999",
      "coin": "@12"
    },
    {
      "prevDayPx": "0.0001",
      "dayNtlVlm": "170782.3200458",
      "markPx": "0.0079681",
      "midPx": "0.00798005",
      "circulatingSupply": "99985337.33995",
      "coin": "@13"
    }
  ]
]
    )";

  d.Parse(exch_info_str.c_str());
}



}; // namespace pktrade::secmasterfiles
