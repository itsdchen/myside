#pragma once

#include <map>
#include <rapidjson/document.h>
#include <string>
#include <memory>
#include <mutex>

/*
Alerter is a little class that helps us send alerts from the c++ end. 
For example, 

*/

namespace pktrade::util {

class Alerter {

 public:
  static Alerter& getInstance();

  // Delete copy constructor and assignment operator
  Alerter(const Alerter&) = delete;
  Alerter& operator=(const Alerter&) = delete;

  void send_mail(std::string title, std::string body, std::string receiver="itsdchen@gmail.com") const;

 private:
  Alerter();
  void loadCredentials();

  std::string email_api_key_;
  std::string email_base_url_;
  std::string email_endpoint_;
  std::string default_email_sender_ = "David <david@mg.itsdchen.com>";


  // For thread-safe initialization
  static std::once_flag init_flag_;
  static std::unique_ptr<Alerter> instance_;
};

} // namespace pktrade::util
