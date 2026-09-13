#include "pktrade/context/local_crypto.h"

#include <array>
#include <iomanip>
#include <openssl/hmac.h>
#include <openssl/sha.h>

namespace pktrade::localgateway {

std::string computeHmacSha256(std::string_view key, std::string_view msg) {
  std::array<unsigned char, EVP_MAX_MD_SIZE> hash;
  unsigned int len;

  HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()),
       reinterpret_cast<unsigned char const*>(msg.data()), static_cast<int>(msg.size()),
       hash.data(), &len);

  return std::string{reinterpret_cast<char const*>(hash.data()), len};
}

std::string hexEncode(const std::string& in) {
  std::stringstream ss;

  ss << std::hex << std::setfill('0');
  for (size_t i = 0; in.length() > i; ++i) {
    ss << std::setw(2) << static_cast<unsigned int>(static_cast<unsigned char>(in[i]));
  }

  return ss.str();
}

} // namespace pktrade::localgateway
