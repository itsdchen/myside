#include "pktrade/pollable.h"

#include <gmock/gmock.h>

using namespace std::chrono_literals;

namespace pktrade {
namespace {

class MockPollable : public Pollable {
 public:
  MOCK_METHOD(PollResult, poll, (), (override));
  MOCK_METHOD(void, task, ());
};

TEST(PollResultTest, Dispatch) {
  MockPollable p;

  auto result = PollResult(TimePoint{12345us}, [&] {
    EXPECT_EQ(Clock::now(), TimePoint{12345us});
    p.task();
  });

  EXPECT_CALL(p, task());
  result();
}

TEST(PollResultTest, Compare) {
  MockPollable p;

  PollResult r1(TimePoint{12345us}, [] {});
  PollResult r2(TimePoint{123456789us}, [] {});
  EXPECT_LT(r1, r2);
}

} // namespace
} // namespace pktrade
