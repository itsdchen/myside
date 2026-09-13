#include "alerts.h"
#include "pktrade/util/basiclib.h"
#include <filesystem>
#include <fmt/format.h>
#include <cpr/cpr.h>


namespace pktrade::util {
std::once_flag Alerter::init_flag_;
std::unique_ptr<Alerter> Alerter::instance_;

Alerter& Alerter::getInstance() {
  std::call_once(init_flag_, []() { instance_.reset(new Alerter()); });
  return *instance_;
}

Alerter::Alerter() { loadCredentials(); }

void Alerter::loadCredentials() {
  // Build path to credentials file
  std::filesystem::path creds_path =
      std::filesystem::path(getenv("HOME")) / ".creds" / ".Alerter.creds.json";

    if (!std::filesystem::exists(creds_path)) {
        throw std::runtime_error("Could not find alerter creds");
    }

  // Read JSON file
  rapidjson::Document alerter_creds_conf = pktrade::util::read_json_file(creds_path.string());

  // Email credentials. 
  // This is just going to be mailgun for now. 
  if (!alerter_creds_conf.HasMember("email_api_key") || !alerter_creds_conf.HasMember("email_base_url") 
) {
    throw std::runtime_error("alerter creds file needs email api_key and base_url!");
}

    // Populates fields. 
    email_api_key_ = alerter_creds_conf["email_api_key"].GetString();
    email_base_url_  = alerter_creds_conf["email_base_url"].GetString();
    email_endpoint_ = fmt::format("{}/messages", email_base_url_);
}

void Alerter::send_mail(std::string title, std::string body, std::string receiver) const {
  // Send email via CPR (matching Python requests.post)
  auto response = cpr::Post(
      cpr::Url{email_endpoint_},
      cpr::Authentication{"api", email_api_key_, cpr::AuthMode::BASIC},
      cpr::Payload{
          {"from", default_email_sender_},
          {"to", receiver},
          {"subject", title},
          {"text", body}
      }
  );

  // Check response
  if (response.status_code != 200) {
    throw std::runtime_error(
        fmt::format("Failed to send email. Status code: {}, Response: {}",
                    response.status_code, response.text));
  }
}

} // namespace pktrade::util
