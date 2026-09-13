#include "funding_master.h"
#include "pktrade/mdapi.h"
#include "pktrade/util/cli.h"
#include "pktrade/util/urls.h"
#include <cmath>
#include <fmt/format.h>
#include <glog/logging.h>
#include <mutex>
#include <string>
#include <string_view>
// For asking for snapshots.
#include <cpr/cpr.h>

#include <magic_enum.hpp>
#include <nlohmann/json.hpp>
#include <rapidjson/document.h>
#include <rapidjson/istreamwrapper.h>
// For doing ip address lookup
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/types.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
namespace pktrade {

FundingMaster::FundingMaster() {
  // Let's just set up some init things.

  // Binance is pretty steady. Re-rquest every 3 hours.
  binance_request_t_ = std::chrono::seconds(60 * 60 * 3);

  // Hyperliquid is vry dynamic. Re-request very 7 monutes.
  hyperliquid_request_t_ = std::chrono::seconds(60 * 7);

  last_binance_crossover_t_ = 0;
  last_hyperliquid_crossover_t_ = 0;

  /*
  Don't request funding on startup, our notion of time is not accurate.
  pktrade::GlobalVar::event_loop_->onTimeout(
      binance_request_t_, [&] { this->refresh_binance_funding(); });
      */
}

void FundingMaster::wants_funding(Market mkt) {
  if (mkt == Market::BinanceFutures) {
    want_binance_ = true;
  } else if (mkt == Market::Hyperliquid) {
    want_hyperliquid_ = true;
  } else {
    // do nothing for now.
  }
}

void FundingMaster::start_funding_requests() {
  // Do a quick check, we shouldn't be running this in sim mode.
  if (!pktrade::GlobalVar::live_) {
    // Send a warning message.
    util::log_line("Trying to start_funding_requests in sim mode, that is unexpected. "
                   "Exiting",
                   false);
    return;
  }

  std::cout << " Fundingmaster: starting to request funding" << std::endl;
  if (want_binance_) {
    std::cout << " Wants binance. Going" << std::endl;
    start_request_binance_funding();
  }
  if (want_hyperliquid_) {
    std::cout << " Wants hyperliquid. Going" << std::endl;
    start_request_hyperliquid_funding();
  }
}

void FundingMaster::start_request_hyperliquid_funding() {
  if (hl_started_) {
    return;
  }
  hl_started_ = true;

  refresh_hyperliquid_funding();
}

void FundingMaster::start_request_binance_funding() {
  if (binance_started_) {
    return;
  }
  binance_started_ = true;

  refresh_binance_funding();
  ;
}

void FundingMaster::refresh_hyperliquid_funding() {

  // Make the http request.  Record and save them all.
  std::string tgt_url = pktrade::urls::hyperliquid_info_url(pktrade::GlobalVar::hl_testnet_);

  // Since hyperliquid is expecting this as a json, I need to include the params
  // as a string in the body.
  nlohmann::json json_data = {{"type", "metaAndAssetCtxs"}};

  // Convert it to a string
  std::string json_string = json_data.dump();

  // Also, just get the next hour's timestamp.
  auto now = std::chrono::system_clock::now();

  // Convert to time_t to manipulate hour
  int64_t cur_t = time_utils::nowToMs();
  int64_t ms_in_h = 60 * 60 * 1000;

  int64_t next_h_pd = (cur_t / ms_in_h + 1) * ms_in_h;
  int64_t last_h_pd = (cur_t / ms_in_h) * ms_in_h;

  // Not super accurate but probably good enough.
  hyperliquid_next_funding_t_ = next_h_pd;

  // TODO: make sure this is right.
  // Potential "lock in" the last known funding rate for the period.
  // Yep, this is definitely wrong, because this is always
  // going to fucking run.
  // TODO: FIX THIS.
  if (last_h_pd > last_hyperliquid_crossover_t_) {
    // Go and add it all in.
    for (auto& [key, value] : hyperliquid_funding) {
      // Set their previous_fundings.
      value.last_realized_funding_time = last_h_pd;
      value.last_realized_funding_rate = value.immediate_rate;
    }
    last_hyperliquid_crossover_t_ = last_h_pd;
  }

  cpr::PostCallback(
      [&, next_hour_ms = next_h_pd](cpr::Response r) {
        if (r.status_code == 200) {
          // Parse it and check if the symbol is in the list.
          rapidjson::Document response_doc;
          response_doc.Parse(r.text.c_str());

          if (response_doc.HasParseError()) {
            std::cout << " Hyperliquid receceived ill-parsed message: " << r.text << std::endl;
            return;
          }

          // Just logging that we asked them.
          /*
          util::log_line(
              fmt::format(
                  "{}: HyperliquidFundingMaster::refresh_hyperliquid_funding. "
                  "{}",
                  time_utils::nowToStr(), r.text),
              false);
*/
          std::vector<std::string> wanted_syms_their_ordering;

          // Step 1, Parse the universe and look for symbols.
          for (const auto& item : response_doc.GetArray()[0]["universe"].GetArray()) {
            // Check for symbol equivalence.
            std::string cur_sym = item["name"].GetString();
            wanted_syms_their_ordering.push_back(cur_sym);
          }

          // Step 2, Look through the funding dicts and get them.
          int counter = 0;
          for (const auto& item : response_doc.GetArray()[1].GetArray()) {
            std::string funding_amt_str = item["funding"].GetString();

            // We have it.
            // Create the pbmessage and publish it.

            double immediate_funding = std::stof(funding_amt_str);
            double naive_annualized_funding = hl_naive_annualize(immediate_funding);
            double compounding_annualized_funding = hl_compounding_annualize(immediate_funding);
            std::string symbol = wanted_syms_their_ordering[counter];

            hyperliquid_funding[symbol] = {Market::Hyperliquid, immediate_funding,
                                           naive_annualized_funding, compounding_annualized_funding,
                                           // Next hour is always the next time we get funding.
                                           next_hour_ms};
            if (counter % 70 == 0 || symbol == "PURR" || symbol == "HYPE") {
              // Print periodically.

              std::cout << " Example: got hyperliquid funding for " << symbol << " "
                        << immediate_funding << " naive_annualized " << naive_annualized_funding
                        << " compounding_annualized " << compounding_annualized_funding
                        << std::endl;
            }
            counter++;
          }

        } else {
          LOG(ERROR) << "Hyperliquid funding request failed with status " << r.status_code
                     << ": " << r.error.message;
        }
      },
      cpr::Url{tgt_url}, cpr::Header{{"Content-Type", "application/json"}}, cpr::Body{json_string},
      cpr::Timeout{8000});

  // Call myself again.
  pktrade::GlobalVar::event_loop_->onTimeout(hyperliquid_request_t_,
                                             [&] { this->refresh_hyperliquid_funding(); });
}

void FundingMaster::refresh_binance_funding() {
  std::cout << time_utils::nowToStr() << " Start of req binance funding" << std::endl;

  // TODO
  std::string tgt_url = fmt::format("{}{}", pktrade::urls::BINANCE_FUTURES_API,
                                    pktrade::urls::BINANCE_PREMIUM_INDEX_ENDPOINT);

  // Can just use the linden key or something. Srsly.
  std::string api_key = "";

  // OK,
  // Let's get the time of the next expected funding.

  int64_t cur_t = time_utils::nowToMs();
  int64_t ms_in_8h = 8 * 60 * 60 * 1000;

  int64_t next_8h_pd = (cur_t / ms_in_8h + 1) * ms_in_8h;
  int64_t last_8h_pd = (cur_t / ms_in_8h) * ms_in_8h;

  // Slightly inaccurate, but this is probably good enough for now.
  binance_next_funding_t_ = next_8h_pd;

  // OK, check if we just barely cross the precipice and haven't
  // updated things yet.
  if (last_8h_pd > last_binance_crossover_t_) {
    // Go through all the btc entries, and update them.
    for (auto& [key, value] : binance_funding) {
      // Set their previous_fundings.
      value.last_realized_funding_time = last_8h_pd;
      value.last_realized_funding_rate = value.immediate_rate;
    }
    last_binance_crossover_t_ = last_8h_pd;
  }

  cpr::GetCallback(
      [&, api_key = api_key](cpr::Response r) {
        if (r.status_code == 200) {
          // Write this to file.
          // Parse the response.
          rapidjson::Document response_doc;
          response_doc.Parse(r.text.c_str());

          if (response_doc.HasParseError()) {
            std::cout << " BinanceFunding receceived ill-parsed message: " << r.text << std::endl;
            return;
          }

          if (response_doc.HasParseError()) {
            std::cout << "Failed to parse Binance Funding response: " << r.text << std::endl;
            return;
          } else {
            int counter = 0;

            for (auto& item : response_doc.GetArray()) {
              // Let's get it..
              int64_t next_funding_t = item["nextFundingTime"].GetInt64();
              double immediate_funding = std::stof(item["lastFundingRate"].GetString());
              double naive_annualized_funding = binance_naive_annualize(immediate_funding);
              double compounding_annualized_funding =
                  binance_compounding_annualize(immediate_funding);
              std::string symbol = item["symbol"].GetString();

              if (counter % 150 == 0) {
                std::cout << " Example: Got binance funding for " << symbol << " "
                          << immediate_funding << " naive_annualized " << naive_annualized_funding
                          << " compounding_annualized " << compounding_annualized_funding
                          << std::endl;
              }

              binance_funding[symbol] = {Market::BinanceFutures, immediate_funding,
                                         naive_annualized_funding, compounding_annualized_funding,
                                         // Next hour is always the next time we get funding.
                                         next_funding_t};

              counter++;
            }
          }

        } else {
          LOG(ERROR) << "Binance funding request failed with status " << r.status_code
                     << ": " << r.error.message;
        }
        return;

        // return r.text;
      },
      cpr::Url{tgt_url}, cpr::Header{{"X-MBX-APIKEY", api_key}},
      cpr::Timeout{8000});

  pktrade::GlobalVar::event_loop_->onTimeout(binance_request_t_,
                                             [&] { this->refresh_binance_funding(); });
}

double FundingMaster::get_funding(Market mkt, SymbolId sym) {
  if (mkt == Market::Hyperliquid) {
    if (hyperliquid_funding.find(sym.get()) != hyperliquid_funding.end()) {
      return hyperliquid_funding[sym.get()].immediate_rate;
    }
  } else if (mkt == Market::BinanceFutures) {
    if (binance_funding.find(sym.get()) != binance_funding.end()) {
      return binance_funding[sym.get()].immediate_rate;
    }
  }

  return 0.0;
}

double FundingMaster::get_compounding_annualized_funding(Market mkt, SymbolId sym) {
  if (mkt == Market::Hyperliquid) {
    if (hyperliquid_funding.find(sym.get()) != hyperliquid_funding.end()) {
      return hyperliquid_funding[sym.get()].compounding_annualized_rate;
    }
  } else if (mkt == Market::BinanceFutures) {
    if (binance_funding.find(sym.get()) != binance_funding.end()) {
      return binance_funding[sym.get()].compounding_annualized_rate;
    }
  }
  return 0.0;
}

double FundingMaster::get_naive_annualized_funding(Market mkt, SymbolId sym) {
  if (mkt == Market::Hyperliquid) {
    return hyperliquid_funding[sym.get()].naive_annualized_rate;
  } else if (mkt == Market::BinanceFutures) {
    return binance_funding[sym.get()].naive_annualized_rate;
  }
  return 0.0;
}

// Hyperliquid pays out once every hour.

double FundingMaster::hl_naive_annualize(double cur_funding) { return cur_funding * 24 * 365; }

double FundingMaster::hl_compounding_annualize(double cur_funding) {
  double full_amt_pd = 1 + cur_funding;
  // let's forget about leap years and stuff.
  double pow_amt = 24 * 365;
  double annualized_amt = pow(full_amt_pd, pow_amt);
  // Should be the % return.
  return annualized_amt - 1;
}

double FundingMaster::binance_naive_annualize(double cur_funding) { return cur_funding * 3 * 365; }

double FundingMaster::binance_compounding_annualize(double cur_funding) {
  // MOST things pay out every 8 hours, but SOME
  // symbols pay out every 4 hours.
  // TODO: keep an updated list, or do something with it.
  double full_amt_pd = 1 + cur_funding;
  // let's forget about leap years and stuff.
  double pow_amt = 3 * 365;
  double annualized_amt = pow(full_amt_pd, pow_amt);
  // Should be the % return.
  return annualized_amt - 1;
}

// If position is positive and funding rate is positive, then
// we pay money, so the payment is negative.
double FundingMaster::hyperliquid_calc_funding(std::string symbol, double notional_position) {
  // Try to find it.
  if (hyperliquid_funding.find(symbol) != hyperliquid_funding.end()) {
    util::log_line(fmt::format("{}: FundingMaster::hyperliquid_calc_funding. "
                               " sym {}  rate {} notional {}",
                               time_utils::nowToStr(), symbol,
                               hyperliquid_funding[symbol].immediate_rate, notional_position),
                   false);
    return -hyperliquid_funding[symbol].immediate_rate * notional_position;
  }

  // Worst case, return 0.
  util::log_line(fmt::format("{}: FundingMaster::hyperliquid_calc_funding "
                             "couldnt find {}, returning 0",
                             time_utils::nowToStr(), symbol),
                 false);
  return 0;
}

// TODO: this will need to be updated when I do binance SPOT too. Because there
// is no difference in symbol names.
double FundingMaster::binance_calc_funding(std::string symbol, double notional_position) {
  // Try to find it.
  if (binance_funding.find(symbol) != binance_funding.end()) {
    util::log_line(fmt::format("{}: FundingMaster::binance_calc_funding. "
                               " sym {}  rate {} notional {}",
                               time_utils::nowToStr(), symbol,
                               binance_funding[symbol].immediate_rate, notional_position),
                   false);

    return -binance_funding[symbol].immediate_rate * notional_position;
  }

  // Worst case, return 0.
  util::log_line(fmt::format("{}: FundingMaster::binance_calc_funding couldnt "
                             "find {}, returning 0",
                             time_utils::nowToStr(), symbol),
                 false);
  return 0;
}

// Note that pos is the actual position. Not the notional.
// Also, this should be done from the risk_manager, not ordexes. Should be safer
// that way.
void FundingMaster::updatePosition(BookId b_id, double pos) {
  // Updates position registered to this bookid.
  symbol_to_pos_[b_id] = pos;

  // Informs the listeners.
  for (auto listener : pos_listeners_[b_id]) {
    listener->onPosChange(b_id, pos);
  }
}
double FundingMaster::getPosition(BookId b_id) {
  if (symbol_to_pos_.find(b_id) == symbol_to_pos_.end()) {
    // Probably an issue.
    LOG(ERROR) << "Could not find position for " << util::print_bookid(b_id);
    return 0.0;
  }

  return symbol_to_pos_[b_id];
}

void FundingMaster::addPositionListener(BookId b_id, FundingPosListener* listener) {
  pos_listeners_[b_id].push_back(listener);
}

} // namespace pktrade
