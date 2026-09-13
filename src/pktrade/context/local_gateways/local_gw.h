#include "pktrade/mdapi.h"
#include "pktrade/oeapi.h"

#pragma once

namespace pktrade {
class LocalContext;
}; // namespace pktrade

namespace pktrade::localgateway {

class LocalGateway {
 public:
  LocalGateway(Market mkt, LocalContext* lcl_ctx) : mkt_(mkt), lcl_ctx_(lcl_ctx) {}

  virtual void onNewOrd(const NewOrder& msg, std::string pk_coid) = 0;
  virtual void onCancelOrd(const CancelOrder& msg, ExchOID exch_oid, std::string sym,
                           std::string pk_coid) = 0;

 protected:
  LocalContext* lcl_ctx_ = nullptr;
  Market mkt_;
};

} // namespace pktrade::localgateway
