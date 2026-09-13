#pragma once

namespace cpr {
class Response;
}

namespace pktrade::util {

// So far, callers are not informed if the call has an error.
// Not sure how to handle it best, yet.
class SBCaller {
 public:
  virtual ~SBCaller() = default;
  virtual void onGetClosingPrint(const cpr::Response& res) {};
  virtual void onGetPosition(const cpr::Response& res) {};
  virtual void onPostPosition(const cpr::Response& res) {};
  virtual void onGetSnapshot(const cpr::Response& res) {};
  virtual void onGetUsermsgs(const cpr::Response& res) {};
};

} // namespace pktrade::util
