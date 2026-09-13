#include "urls.h"

namespace pktrade::urls {

std::string hyperliquid_info_url(bool is_testnet) {
  if (is_testnet) {
    return HYPERLIQUID_TESTNET_API + HYPERLIQUID_INFO_ENDPOINT;
  } else {
    return HYPERLIQUID_MAINNET_API + HYPERLIQUID_INFO_ENDPOINT;
  }
}

} // namespace pktrade::urls
