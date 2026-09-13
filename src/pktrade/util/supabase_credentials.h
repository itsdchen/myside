#pragma once

#include <cpr/cpr.h>
#include <ixwebsocket/IXWebSocket.h>
#include <memory>
#include <mutex>
#include <rapidjson/document.h>
#include <string>
#include <utility>

#include "pktrade/util/sbcaller.h"

namespace pktrade::util {

class SupabaseCredentials {
 public:
  // Get singleton instance
  static SupabaseCredentials& getInstance();

  // Delete copy constructor and assignment operator
  SupabaseCredentials(const SupabaseCredentials&) = delete;
  SupabaseCredentials& operator=(const SupabaseCredentials&) = delete;

  // Getters for table names
  const std::string& getClosingPrintTable() const { return closing_print_table_; }
  const std::string& getPositionTable() const { return position_table_; }
  const std::string& getSnapshotTable() const { return snapshot_table_; }
  const std::string& getUsermsgTable() const { return usermsg_table_; }

  // Wrappers for cpr::Get and cpr::Post, filling in the appropriate url and credentials
  cpr::Response cprGetClosingPrint(const cpr::Parameters& params, const cpr::Timeout& timeout);
  cpr::Response cprGetPosition(const cpr::Parameters& params, const cpr::Timeout& timeout);
  cpr::Response cprPostPosition(const cpr::Body& body, const cpr::Timeout& timeout);
  cpr::Response cprGetSnapshot(const cpr::Parameters& params, const cpr::Timeout& timeout);
  cpr::Response cprGetUsermsgs(const cpr::Parameters& params, const cpr::Timeout& timeout);

  void cprGetClosingPrintAS(const cpr::Parameters& params, const cpr::Timeout& timeout,
                            SBCaller* caller);
  void cprGetPositionAS(const cpr::Parameters& params, const cpr::Timeout& timeout,
                        SBCaller* caller);
  void cprPostPositionAS(const cpr::Body& body, const cpr::Timeout& timeout, SBCaller* caller);
  void cprGetSnapshotAS(const cpr::Parameters& params, const cpr::Timeout& timeout,
                        SBCaller* caller);
  void cprGetUsermsgsAS(const cpr::Parameters& params, const cpr::Timeout& timeout,
                        SBCaller* caller);

  // Set the websocket with the usermsg URL so we don't explicitly pass around the API key
  // Note: this doesn't totally keep the API key contained within this class, because the
  // websocket still has the URL, which contains the key, but it's slightly obfuscated away
  void setUsermsgUrl(ix::WebSocket& webSocket) const { webSocket.setUrl(usermsg_rt_url_); }

 private:
  SupabaseCredentials();
  void loadCredentials();

  // For Supabase Auth
  bool authenticate(const std::string& email, const std::string& password);
  bool refreshAuth();
  bool parseTokens(std::string& rtext);
  bool authExpired();

  // Credentials
  std::string project_id_;
  std::string api_key_;
  cpr::Header header_;

  // Supabase Auth tokens, etc.
  bool use_auth_ = false;
  std::string access_token_ = "";
  std::string refresh_token_ = "";
  int64_t expires_at_ = 0;

  // Table names
  std::string closing_print_table_;
  std::string position_table_;
  std::string snapshot_table_;
  std::string usermsg_table_;

  // URLs
  std::string base_url_;
  cpr::Url closing_print_url_;
  cpr::Url position_url_;
  cpr::Url snapshot_url_;
  cpr::Url usermsg_url_;
  std::string usermsg_rt_url_; // contains API key
  cpr::Url auth_url_;
  cpr::Url refresh_url_;

  // For thread-safe initialization
  static std::once_flag init_flag_;
  static std::unique_ptr<SupabaseCredentials> instance_;
};

} // namespace pktrade::util
