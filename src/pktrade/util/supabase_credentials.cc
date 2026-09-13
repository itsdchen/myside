#include <chrono>
#include <filesystem>
#include <fmt/format.h>
#include <glog/logging.h>
#include <stdexcept>
#include <thread>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include "pktrade/util/urls.h"

#include "supabase_credentials.h"

namespace pktrade::util {

// Static member definitions
std::once_flag SupabaseCredentials::init_flag_;
std::unique_ptr<SupabaseCredentials> SupabaseCredentials::instance_;

SupabaseCredentials& SupabaseCredentials::getInstance() {
  std::call_once(init_flag_, []() { instance_.reset(new SupabaseCredentials()); });
  return *instance_;
}

SupabaseCredentials::SupabaseCredentials() { loadCredentials(); }

void SupabaseCredentials::loadCredentials() {
  // Build path to credentials file
  std::filesystem::path sb_creds_path = getenv("HOME");
  if (pktrade::GlobalVar::vault_) {
    sb_creds_path = sb_creds_path / ".creds" / ".Supabase.vault.creds.json";
  } else {
    sb_creds_path = sb_creds_path /".creds" / ".Supabase.creds.json";
  }

  // Read JSON file
  rapidjson::Document sb_creds_conf = pktrade::util::read_json_file(sb_creds_path.string());

  // Validate required fields
  if (!sb_creds_conf.HasMember("project_id") || !sb_creds_conf.HasMember("api_key")) {
    throw std::runtime_error(
        fmt::format("Supabase credentials file {} is missing required keys (project_id, api_key).",
                    sb_creds_path.string()));
  }

  // Load core credentials
  project_id_ = sb_creds_conf["project_id"].GetString();
  api_key_ = sb_creds_conf["api_key"].GetString();

  // Load table names if present

  if (pktrade::GlobalVar::hl_testnet_) {
    // testnet table
    if (sb_creds_conf.HasMember("testnet_position_table")) {
      position_table_ = sb_creds_conf["testnet_position_table"].GetString();
    } else {
      throw std::runtime_error("Supabase creds needs position_table entry");
    }

  } else {
    // mainnet table
    if (sb_creds_conf.HasMember("mainnet_position_table")) {
      position_table_ = sb_creds_conf["mainnet_position_table"].GetString();
    } else {
      throw std::runtime_error("Supabase creds needs position_table entry");
    }
  }

  if (sb_creds_conf.HasMember("closing_print_table")) {
    closing_print_table_ = sb_creds_conf["closing_print_table"].GetString();
  } else {
    throw std::runtime_error("Supabase creds needs closing_print_table entry");
  }

  if (sb_creds_conf.HasMember("snapshot_table")) {
    snapshot_table_ = sb_creds_conf["snapshot_table"].GetString();
  } else {
    throw std::runtime_error("Supabase creds needs snapshot_table entry");
  }

  // Note: this requires a usermsg_table entry in the creds file even if we won't use it
  if (pktrade::GlobalVar::hl_testnet_) {
    if (sb_creds_conf.HasMember("testnet_usermsg_table")) {
      usermsg_table_ = sb_creds_conf["testnet_usermsg_table"].GetString();
    } else {
      throw std::runtime_error("Supabase creds needs usermsg_table entry");
    }
  } else {
    if (sb_creds_conf.HasMember("mainnet_usermsg_table")) {
      usermsg_table_ = sb_creds_conf["mainnet_usermsg_table"].GetString();
    } else {
      throw std::runtime_error("Supabase creds needs usermsg_table entry");
    }
  }

  // Construct derived values
  base_url_ = fmt::format("https://{}.supabase.co", project_id_);
  closing_print_url_ = cpr::Url{fmt::format("{}/rest/v1/{}", base_url_, closing_print_table_)};
  position_url_ = cpr::Url{fmt::format("{}/rest/v1/{}", base_url_, position_table_)};
  snapshot_url_ = cpr::Url{fmt::format("{}/rest/v1/{}", base_url_, snapshot_table_)};
  usermsg_url_ = cpr::Url{fmt::format("{}/rest/v1/{}", base_url_, usermsg_table_)};
  header_["apikey"] = api_key_;
  header_["Content-Type"] = "application/json";
  usermsg_rt_url_ =
      fmt::format("wss://{}.supabase.co/realtime/v1/websocket?apikey={}", project_id_, api_key_);
  auth_url_ = cpr::Url{fmt::format("{}/auth/v1/token?grant_type=password", base_url_)};
  refresh_url_ = cpr::Url{fmt::format("{}/auth/v1/token?grant_type=refresh_token", base_url_)};

  LOG(INFO) << "Supabase credentials loaded successfully for project: " << project_id_;

  // Use Supabase Auth if email and password provided
  if (sb_creds_conf.HasMember("email") && sb_creds_conf["email"].IsString() &&
      sb_creds_conf.HasMember("password") && sb_creds_conf["password"].IsString()
      // Potentially turn this off if we deal with 429's properly. 
      && pktrade::GlobalVar::live_
    ) {
    LOG(INFO) << "Trying to authenticate";
    authenticate(sb_creds_conf["email"].GetString(), sb_creds_conf["password"].GetString());
  }
}

bool SupabaseCredentials::authenticate(const std::string& email, const std::string& password) {
  std::cout <<"Authenticating" << std::endl;
  cpr::Body payload{"{\"email\": \"" + email + "\", \"password\": \"" + password + "\"}"};
  cpr::Response r = pktrade::urls::CprRetry::Post(auth_url_, header_, payload);
  if (r.status_code == 200 && parseTokens(r.text)) {
    LOG(INFO) << "Successfully authenticated. Expires in: "
              << expires_at_ - time_utils::systemNowToS();
    header_["Authorization"] = "Bearer " + access_token_;
    use_auth_ = true;
    return true;
  }
  LOG(ERROR) << "Failed to authenticate (status " << r.status_code << "): " << r.text;
  LOG(ERROR) << "Falling back to anon access";
  use_auth_ = false;
  return false;
}

bool SupabaseCredentials::refreshAuth() {
  cpr::Body payload{"{\"refresh_token\": \"" + refresh_token_ + "\"}"};
  cpr::Response r = pktrade::urls::CprRetry::Post(refresh_url_, header_, payload);
  if (r.status_code == 200 && parseTokens(r.text)) {
    LOG(INFO) << "Successfully refreshed authentication. Expires in: "
              << expires_at_ - time_utils::systemNowToS();
    header_["Authorization"] = "Bearer " + access_token_;
    use_auth_ = true;
    return true;
  }
  LOG(ERROR) << "Failed to refresh authentication (status " << r.status_code << "): " << r.text;
  LOG(ERROR) << "Falling back to anon access";
  // Could use the anon key as the Bearer token (same anon fallback). Just don't leave the expired
  // token in there or we'll get a 401 Unauthorized rejection.
  header_.erase("Authorization");
  use_auth_ = false;
  return false;
}

bool SupabaseCredentials::parseTokens(std::string& rtext) {
  rapidjson::Document doc;
  doc.Parse(rtext.c_str());
  if (doc.HasParseError() || !doc.HasMember("access_token") || !doc["access_token"].IsString() ||
      !doc.HasMember("refresh_token") || !doc["refresh_token"].IsString() ||
      !doc.HasMember("expires_in") || !doc["expires_in"].IsInt()) {
    LOG(ERROR) << "Error parsing auth tokens from Supabase response.";
    return false;
  }
  access_token_ = doc["access_token"].GetString();
  refresh_token_ = doc["refresh_token"].GetString();
  expires_at_ = time_utils::systemNowToS() + doc["expires_in"].GetInt();
  return true;
}

bool SupabaseCredentials::authExpired() {
  // Expire 10s early to be sure
  return time_utils::systemNowToS() > expires_at_ - 10;
}

cpr::Response SupabaseCredentials::cprGetClosingPrint(const cpr::Parameters& params,
                                                      const cpr::Timeout& timeout) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }
  return cpr::Get(closing_print_url_, params, header_, timeout);
}

cpr::Response SupabaseCredentials::cprGetPosition(const cpr::Parameters& params,
                                                  const cpr::Timeout& timeout) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }
  return pktrade::urls::CprRetry::Get(position_url_, params, header_, timeout);
}

cpr::Response SupabaseCredentials::cprPostPosition(const cpr::Body& body,
                                                   const cpr::Timeout& timeout) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }
  return pktrade::urls::CprRetry::Post(position_url_, header_, body, timeout);
}

cpr::Response SupabaseCredentials::cprGetSnapshot(const cpr::Parameters& params,
                                                  const cpr::Timeout& timeout) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }
  return pktrade::urls::CprRetry::Get(snapshot_url_, params, header_, timeout);
}

cpr::Response SupabaseCredentials::cprGetUsermsgs(const cpr::Parameters& params,
                                                  const cpr::Timeout& timeout) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }
  return pktrade::urls::CprRetry::Get(usermsg_url_, params, header_, timeout);
}

// Async versions.
// tbh - I don't want to deal with the stuff you have in urls.h right now.
void SupabaseCredentials::cprGetClosingPrintAS(const cpr::Parameters& params,
                                               const cpr::Timeout& timeout, SBCaller* caller) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }

  cpr::GetCallback(
      [this, caller = caller, params = params, timeout = timeout](cpr::Response resp) {
        if (resp.status_code == 200) {
          caller->onGetClosingPrint(resp);
        } else {
          // We're already in a separate thread here, so just retry with sync version (with retries)
          LOG(WARNING) << "Failed to get closing print with first async try (status "
                       << resp.status_code << ": " << resp.text << "). Retrying with sync version.";
          cpr::Response sync_resp = cprGetClosingPrint(params, timeout);
          caller->onGetClosingPrint(sync_resp);
        }
      },
      cpr::Url{closing_print_url_}, params, header_, timeout);
}

void SupabaseCredentials::cprGetPositionAS(const cpr::Parameters& params,
                                           const cpr::Timeout& timeout, SBCaller* caller) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }

  cpr::GetCallback(
      [this, caller = caller, params = params, timeout = timeout](cpr::Response resp) {
        if (resp.status_code == 200) {
          caller->onGetPosition(resp);
        } else {
          // We're already in a separate thread here, so just retry with sync version (with retries)
          LOG(WARNING) << "Failed to get position with first async try (status " << resp.status_code
                       << ": " << resp.text << "). Retrying with sync version.";
          cpr::Response sync_resp = cprGetPosition(params, timeout);
          caller->onGetPosition(sync_resp);
        }
      },
      cpr::Url{position_url_}, params, header_, timeout);
}

void SupabaseCredentials::cprPostPositionAS(const cpr::Body& body, const cpr::Timeout& timeout,
                                            SBCaller* caller) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }
  cpr::PostCallback(
      [this, caller = caller, body = body, timeout = timeout](cpr::Response resp) {
        if (resp.status_code == 200 || resp.status_code == 201) {
          caller->onPostPosition(resp);
        } else {
          // We're already in a separate thread here, so just retry with sync version (with retries)
          LOG(WARNING) << "Failed to post position with first async try (status "
                       << resp.status_code << ": " << resp.text << "). Retrying with sync version.";
          cpr::Response sync_resp = cprPostPosition(body, timeout);
          caller->onPostPosition(sync_resp);
        }
      },
      cpr::Url{position_url_}, body, header_, timeout);
}

void SupabaseCredentials::cprGetSnapshotAS(const cpr::Parameters& params,
                                           const cpr::Timeout& timeout, SBCaller* caller) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }

  cpr::GetCallback(
      [this, caller = caller, params = params, timeout = timeout](cpr::Response resp) {
        if (resp.status_code == 200) {
          caller->onGetSnapshot(resp);
        } else {
          // We're already in a separate thread here, so just retry with sync version (with retries)
          LOG(WARNING) << "Failed to get snapshot with first async try (status " << resp.status_code
                       << ": " << resp.text << "). Retrying with sync version.";
          cpr::Response sync_resp = cprGetSnapshot(params, timeout);
          caller->onGetSnapshot(sync_resp);
        }
      },
      cpr::Url{snapshot_url_}, params, header_, timeout);
}

void SupabaseCredentials::cprGetUsermsgsAS(const cpr::Parameters& params,
                                           const cpr::Timeout& timeout, SBCaller* caller) {
  if (use_auth_ && authExpired()) {
    refreshAuth();
  }

  cpr::GetCallback(
      [this, caller = caller, params = params, timeout = timeout](cpr::Response resp) {
        if (resp.status_code == 200) {
          caller->onGetUsermsgs(resp);
        } else {
          // We're already in a separate thread here, so just retry with sync version (with retries)
          LOG(WARNING) << "Failed to get usermsgs with first async try (status " << resp.status_code
                       << ": " << resp.text << "). Retrying with sync version.";
          cpr::Response sync_resp = cprGetUsermsgs(params, timeout);
          caller->onGetUsermsgs(sync_resp);
        }
      },
      cpr::Url{usermsg_url_}, params, header_, timeout);
}
} // namespace pktrade::util
