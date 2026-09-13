#include "hedge_positioner.h"

#include "pktrade/context/global_vars.h"
// Sorry. I'm using nlohmann json for encoding and decoding stuff.
#include "pktrade/util/urls.h"

#include <nlohmann/json.hpp>

#include <magic_enum.hpp>

namespace pktrade::ordex {

HedgePositioner::HedgePositioner() : leader_pos_(0), leader_not_(0) {}

void HedgePositioner::config(SymbolId sym_, const rapidjson::Value& conf) {
  leader_symbol_ = sym_;

  // Look through its
  leader_market_ =
      magic_enum::enum_cast<Market>(conf["leader_market"].GetString()).value_or(Market::Unknown);

  // Look through these and subscribe accordingly.
  if (leader_market_ == Market::BinanceFutures) {
    // TODO: there needs to be better security here, in the future.
    bf_api_key_ = conf["bf_api_key"].GetString();
    bf_secret_key_ = conf["bf_secret_key"].GetString();
    bf_update_fequency_s_ = conf["bf_update_fequency_s"].GetInt();
  } else if (leader_market_ == Market::Hyperliquid) {
    hl_address_ = conf["hl_address"].GetString();
    hl_update_frequency_s_ = conf["hl_update_fequency_s"].GetInt();
  }

  cpr_session_ = std::make_shared<cpr::Session>();

  if (pktrade::GlobalVar::live_ &&
      clock_cast<Clock>(std::chrono::system_clock::now()) > pktrade::GlobalVar::start_t_) {
    pktrade::GlobalVar::event_loop_->onTimeout(clock_cast<Clock>(std::chrono::system_clock::now()) +
                                                   std::chrono::seconds(50),
                                               [&] { this->startProcess(); });

  } else {
    pktrade::GlobalVar::event_loop_->onTimeout(pktrade::GlobalVar::start_t_,
                                               [&] { this->startProcess(); });
  }
}

// This should be for a specific symbol.
double HedgePositioner::getLeaderPosition() { return 0; }

// Potentially use notional if we want to hedge betta when
// it's not 1-1.
double HedgePositioner::getLeaderNotional() { return 0; }

void HedgePositioner::startProcess() {
  if (leader_market_ == Market::BinanceFutures) {
    connectBF();
  } else if (leader_market_ == Market::Hyperliquid) {
  }
}

// position update subscriptions.

// Note that this handle/update stuff is going to be
// semi-shared with the gateway process. I think we want to do this
// versus being a cleaner programmer.

// Binance spot subscription

// //////////////////////////////////////////////////////////////////
// Binance Futures subscription
void HedgePositioner::connectBF() {
  cpr_session_->SetUrl(cpr::Url{"https://fapi.binance.com/fapi/v2/positionRisk"});
  cpr_session_->SetHeader(
      cpr::Header{{"Content-Type", "application/json"}, {"X-MBX-APIKEY", bf_api_key_}});

  int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                       std::chrono::system_clock::now().time_since_epoch())
                       .count();

  std::string queryString("");
  queryString.append("symbol=");
  queryString.append(leader_symbol_.get());
  queryString.append("&timestamp=");
  queryString.append(std::to_string(now_ms));
  std::string signature = hexEncode(computeHmacSha256(bf_secret_key_, queryString));
  queryString.append("&signature=");
  queryString.append(signature);

  cpr::Parameters new_parameters = cpr::Parameters{{"symbol", leader_symbol_.get()},
                                                   {"timestamp", std::to_string(now_ms)},
                                                   {"signature", signature}};
  cpr_session_->SetParameters(new_parameters);

  cpr_session_->PostCallback([&](cpr::Response r) {
    if (r.status_code == 200) {
      rapidjson::Document doc;
      doc.Parse(r.text.c_str());

      // update positions
      for (const rapidjson::Value& posDict : doc.GetArray()) {
        // Should just be one...
        this->leader_pos_ = std::stod(posDict["positionAmt"].GetString());
        this->leader_not_ = std::stod(posDict["notional"].GetString());
      }
    } else {
      // A failure happened. Maybe log it.
    }
  });

  // Call again in the future.
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(bf_update_fequency_s_),
                                             [&] { this->connectBF(); });
}

// //////////////////////////////////////////////////////////////////
// Hyperliquid subscription

void HedgePositioner::connectHyperliquid() {
  // TODO: impelment this.

  cpr_session_->SetUrl(
      cpr::Url{pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_)});

  nlohmann::json payload = {{"type", "clearinghouseState"},
                            // 0x hex address.
                            {"user", hl_address_}};
  cpr_session_->SetBody(cpr::Body{payload.dump()});

  cpr_session_->SetHeader(cpr::Header{{"Content-Type", "application/json"}});

  cpr_session_->PostCallback([&](cpr::Response r) {
    if (r.status_code == 200) {
      rapidjson::Document doc;
      doc.Parse(r.text.c_str());

      // update positions
      for (const rapidjson::Value& posDict : doc.GetArray()) {
        const rapidjson::Value& pos = posDict["position"];
        if (leader_symbol_.get() != pos["coin"].GetString()) {
          continue;
        }

        // Otherwise, we should have it.
        // szi is the size...? I don't know why they call it this.
        this->leader_pos_ = std::stod(posDict["szi"].GetString());

        // Note that hyperliquid only returns to us the magnitude of the
        // position. Not the sign. So I have to use copysign.
        this->leader_not_ =
            std::copysign(std::stod(posDict["positionValue"].GetString()), this->leader_pos_);
      }
    } else {
      // A failure happened. Maybe log it.
    }
  });

  // Call again in the future.
  pktrade::GlobalVar::event_loop_->onTimeout(std::chrono::seconds(hl_update_frequency_s_),
                                             [&] { this->connectHyperliquid(); });
}

// Format looks like this:
/*
Delete this when we're done with this.
x = [
    {
        "position": {
            "coin": "AVAX",
            "cumFunding": {"allTime": "0.0", "sinceChange": "0.0", "sinceOpen":
"0.0"}, "entryPx": "39.97", "leverage": {"type": "cross", "value": 20},
            "liquidationPx": "728.88207967",
            "marginUsed": "0.597915",
            "maxLeverage": 20,
            "positionValue": "11.9583",
            "returnOnEquity": "0.05469005",
            "szi": "-0.3",
            "unrealizedPnl": "0.0327",
        },
        "type": "oneWay",
    }
]


*/

// //////////////////////////////////////////////////////////////////

// Useful utils for signing and stuff.
std::string HedgePositioner::computeHmacSha256(std::string_view key, std::string_view msg) {
  std::array<unsigned char, EVP_MAX_MD_SIZE> hash;
  unsigned int len;

  HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()),
       reinterpret_cast<unsigned char const*>(msg.data()), static_cast<int>(msg.size()),
       hash.data(), &len);

  return std::string{reinterpret_cast<char const*>(hash.data()), len};
}

std::string HedgePositioner::hexEncode(const std::string& in) {
  std::stringstream ss;

  ss << std::hex << std::setfill('0');
  for (size_t i = 0; in.length() > i; ++i) {
    ss << std::setw(2) << static_cast<unsigned int>(static_cast<unsigned char>(in[i]));
  }

  return ss.str();
}

} // namespace pktrade::ordex
