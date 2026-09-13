#include "pktrade/decoder.h"

#include <string>

#include <gtest/gtest.h>

using namespace std::string_literals;

namespace pktrade {
namespace {

TEST(Capture, DecodeDepthUpdateBinanceProto) {
  auto data =
      R"({"stream": "btcusdt@depth@100ms", "data": {"e":"depthUpdate","E":1681147805350,"s":"BTCUSDT","U":36156836055,"u":36156836073,"b":[["29112.03000000","0.00230000"],["29111.07000000","0.00687000"],["29110.18000000","0.00069000"],["29109.95000000","0.00000000"],["29101.89000000","0.00000000"],["29100.97000000","0.00000000"],["29100.67000000","0.00000000"],["29083.51000000","0.00000000"],["29081.07000000","0.00000000"],["29037.00000000","0.06867000"]],"a":[["29112.83000000","0.00687000"],["29116.50000000","0.00067000"],["29117.04000000","0.00067000"],["29117.58000000","0.00067000"],["29137.39000000","0.31269000"],["29152.81000000","0.00000000"],["29157.90000000","0.34364000"],["29224.40000000","0.00000000"],["29236.50000000","0.01000000"]]}})"s;

  auto msg = decodeBinanceStreamData(data);

  EXPECT_EQ(msg.symbol_id(), "BTCUSDT");

  auto& payload = msg.book_update();

  EXPECT_EQ(payload.event_time(), 1681147805350);
  EXPECT_EQ(payload.first_update_id(), 36156836055);
  EXPECT_EQ(payload.last_update_id(), 36156836073);

  EXPECT_EQ(payload.bids_size(), 10);
  EXPECT_EQ(payload.asks_size(), 9);
}

TEST(Capture, DecodeBookSnapshotToBinanceProto) {
  auto data =
      R"({"stream": "btcusdt@depthSnapshot", "generated": true, "data": {"lastUpdateId": 36234830175, "bids": [["30466.93000000", "1.00035000"], ["30466.92000000", "0.01014$000"]], "asks": [["30348.97000000", "0.00685000"], ["30348.46000000", "0.00054000"]]}, "rx": 1234})"s;
  auto msg = decodeBinanceStreamData(data);

  EXPECT_EQ(msg.symbol_id(), "BTCUSDT");
  EXPECT_EQ(msg.rx_timestamp(), 1234);

  auto& payload = msg.book_snapshot();

  EXPECT_EQ(payload.last_update_id(), 36234830175);
  EXPECT_EQ(payload.bids().size(), 2);
  EXPECT_EQ(payload.asks().size(), 2);
}

} // namespace
} // namespace pktrade
