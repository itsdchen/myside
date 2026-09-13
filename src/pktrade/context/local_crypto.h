#pragma once

#include <string>
#include <string_view>

// Libraries for signing actions.
// This is essentially copied from crypto.h in the /gateway dir, I just
// didn't want to deal with the include structure because my skills are lacking.
// Sorry.

namespace pktrade::localgateway {

std::string computeHmacSha256(std::string_view key, std::string_view msg);

std::string hexEncode(const std::string& in);

} // namespace pktrade::localgateway
