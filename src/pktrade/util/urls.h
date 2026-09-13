#pragma once

#include <chrono>
#include <cpr/cpr.h>
#include <glog/logging.h>
#include <random>
#include <string>

namespace pktrade::urls {

// Hyperliquid API URLs
inline const std::string HYPERLIQUID_MAINNET_API = "https://api.hyperliquid.xyz";
inline const std::string HYPERLIQUID_TESTNET_API = "https://api.hyperliquid-testnet.xyz";
inline const std::string HYPERLIQUID_INFO_ENDPOINT = "/info";

std::string hyperliquid_info_url(bool is_testnet);

// Binance API URLs
inline const std::string BINANCE_SPOT_API = "https://api.binance.com";
inline const std::string BINANCE_FUTURES_API = "https://fapi.binance.com";
inline const std::string BINANCE_DELIVERY_API = "https://dapi.binance.com";

// Binance Endpoints
inline const std::string BINANCE_PREMIUM_INDEX_ENDPOINT = "/fapi/v1/premiumIndex";
inline const std::string BINANCE_POSITION_RISK_ENDPOINT = "/fapi/v2/positionRisk";
inline const std::string BINANCE_DEPTH_ENDPOINT = "/depth";
inline const std::string BINANCE_SPOT_DEPTH_ENDPOINT = "/api/v3/depth";
inline const std::string BINANCE_FUTURES_DEPTH_ENDPOINT = "/fapi/v1/depth";
inline const std::string BINANCE_DELIVERY_DEPTH_ENDPOINT = "/dapi/v1/depth";

// Supabase URL Template
inline const std::string SUPABASE_REST_API_TEMPLATE = "https://{}.supabase.co/rest/v1";

// Class for retryable cpr::Get and cpr::Post calls, with exponential backoff and jitter
class CprRetry {
 private:
  static constexpr int MAX_ATTEMPTS = 5;         // max number of attempts, including the first try
  static constexpr int BACKOFF_INIT = 500;       // ms to back off after first fail
  static constexpr double JITTER_MAX_MULT = 2.0; // jitter multipler in range [1, this)

  static bool isRetryable(const cpr::Response& r) {
    // Network errors (can add more as encountered)
    if (r.error.code != cpr::ErrorCode::OK) {
      auto code = r.error.code;
      return (code == cpr::ErrorCode::OPERATION_TIMEDOUT ||
              code == cpr::ErrorCode::CONNECTION_FAILURE ||
              code == cpr::ErrorCode::NETWORK_RECEIVE_ERROR ||
              code == cpr::ErrorCode::NETWORK_SEND_FAILURE ||
              code == cpr::ErrorCode::PROXY_RESOLUTION_FAILURE ||
              code == cpr::ErrorCode::SSL_CONNECT_ERROR);
    }
    // HTTP status codes that are retryable (can add more as encountered)
    return (r.status_code == 408 || // Request Timeout
            r.status_code == 429 || // Too Many Requests
            r.status_code == 500 || // Internal Server Error
            r.status_code == 502 || // Bad Gateway
            r.status_code == 503 || // Service Unavailable
            r.status_code == 504);  // Gateway Timeout
  }

  static uint64_t calcBackoff(int attempt) {
    // This has a separate instance, and only one, in each thread. So it's thread-safe, and we're
    // also not creating a new one everytime we call this function.
    thread_local std::mt19937 gen{std::random_device{}()};
    std::uniform_real_distribution<double> dis{1, JITTER_MAX_MULT};
    // Note: using bitshift in place of std::pow(2, x)
    return (BACKOFF_INIT << attempt) * dis(gen);
  }

  template <typename Func>
  static cpr::Response retry(Func&& func) {
    cpr::Response r;
    for (int attempt = 0; attempt < MAX_ATTEMPTS; ++attempt) {
      r = func();
      if (attempt == MAX_ATTEMPTS - 1 || !isRetryable(r)) {
        // success or last attempt failed
        return r;
      }
      // On 1st failure, back off for 200-300ms
      // On 4th failure, back off for 1.6-2.4s (cumulative 3-4.5s)
      uint64_t backoff = calcBackoff(attempt);
      LOG(INFO) << "Attempt: " << attempt << ", status: " << r.status_code
                << ", error: " << r.error.message << ". Retrying in " << backoff << "ms";
      std::this_thread::sleep_for(std::chrono::milliseconds(backoff));
    }
    return r; // should never reach here
  }

 public:
  template <typename... Args>
  static cpr::Response Get(Args&&... args) {
    return retry([&]() { return cpr::Get(std::forward<Args>(args)...); });
  }

  template <typename... Args>
  static cpr::Response Post(Args&&... args) {
    return retry([&]() { return cpr::Post(std::forward<Args>(args)...); });
  }

  static cpr::Response SessionGet(cpr::Session& session) {
    return retry([&]() { return session.Get(); });
  }

  static cpr::Response SessionPost(cpr::Session& session) {
    return retry([&]() { return session.Post(); });
  }
};

} // namespace pktrade::urls
