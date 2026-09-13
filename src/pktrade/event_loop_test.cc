#include "pktrade/event_loop.h"

#include <optional>

#include <gmock/gmock.h>

#include "pktrade/clock.h"

using namespace std::chrono_literals;

using ::testing::InSequence;
using ::testing::Invoke;
using ::testing::Return;
using ::testing::Sequence;
using ::testing::StrictMock;

namespace pktrade {
namespace {

class MockPollable : public Pollable {
 public:
  MOCK_METHOD(PollResult, poll, ());
  MOCK_METHOD(void, dispatch, ());
};

class EventLoopTest : public ::testing::Test {
 public:
  EventLoopTest() { Clock::set(TimePoint{}); }

 protected:
  EventLoop loop;
};

// This test constructs two mock pollables and tests that the event loop can
// dispatch them in the correct order.
TEST_F(EventLoopTest, SimPoller) {
  // Use strict mocks to assert failures if some function is called if we didn't
  // explicitly assert that it should. This is mainly to assert that when we do
  //
  // EXPECT_CALL(p1, poll())
  //
  // we also implicitly assert that p2's poll function did NOT get called.
  StrictMock<MockPollable> p1;
  StrictMock<MockPollable> p2;

  loop.setPoller(std::make_unique<SimPoller>());
  loop.registerPollables(p1, p2);

  // Since gmock expectations are "sticky" (i.e. they stick around even after
  // they are satisfied), we need to retire them so that only the most recent
  // EXPECT_CALL is checked. This can be done via Sequences. For more info, see:
  //
  // https://github.com/google/googletest/blob/master/googlemock/docs/CookBook.md#controlling-when-an-expectation-retires
  Sequence s1, s2;
  EXPECT_CALL(p1, poll())
      .InSequence(s1)
      .WillOnce(Return(PollResult(TimePoint{1532094877000000us}, [&] { p1.dispatch(); })));
  EXPECT_CALL(p2, poll())
      .InSequence(s2)
      .WillOnce(Return(PollResult(TimePoint{1532094877500000us}, [&] { p2.dispatch(); })));

  // After the pollables are mocked to return their values, assert that p1's
  // poll() method is called. Since p1 and p2 are strict mocks, this also
  // asserts that p2's poll() function did not get called.
  EXPECT_CALL(p1, dispatch()).InSequence(s1, s2);
  loop.runOnce();

  EXPECT_CALL(p1, poll())
      .InSequence(s1)
      .WillOnce(Return(PollResult(TimePoint{1532094878000000us}, [&] { p1.dispatch(); })));
  EXPECT_CALL(p2, poll())
      .InSequence(s2)
      .WillOnce(Return(PollResult(TimePoint{1532094877500000us}, [&] { p2.dispatch(); })));

  EXPECT_CALL(p2, dispatch()).InSequence(s1, s2);
  loop.runOnce();

  EXPECT_CALL(p1, poll())
      .InSequence(s1)
      .WillOnce(Return(PollResult(TimePoint{1532094878000000us}, [&] { p1.dispatch(); })));
  EXPECT_CALL(p2, poll())
      .InSequence(s2)
      .WillOnce(Return(PollResult(TimePoint{1532094877600000us}, [&] { p2.dispatch(); })));

  EXPECT_CALL(p2, dispatch()).InSequence(s1, s2);
  loop.runOnce();

  EXPECT_CALL(p1, poll())
      .InSequence(s1)
      .WillOnce(Return(PollResult(TimePoint{1532094878000000us}, [&] { p1.dispatch(); })));
  EXPECT_CALL(p2, poll())
      .InSequence(s2)
      .WillOnce(Return(PollResult(TimePoint{1532094878100000us}, [&] { p2.dispatch(); })));

  EXPECT_CALL(p1, dispatch()).InSequence(s1, s2);
  loop.runOnce();

  EXPECT_CALL(p1, poll()).InSequence(s1).WillOnce(Return(PollResult{}));
  EXPECT_CALL(p2, poll())
      .InSequence(s2)
      .WillOnce(Return(PollResult(TimePoint{1532094878100000us}, [&] { p2.dispatch(); })));

  EXPECT_CALL(p2, dispatch()).InSequence(s1, s2);
  loop.runOnce();

  EXPECT_CALL(p1, poll()).InSequence(s1).WillOnce(Return(PollResult{}));
  EXPECT_CALL(p2, poll()).InSequence(s2).WillOnce(Return(PollResult{}));

  loop.runOnce();
}

TEST_F(EventLoopTest, LivePoller) {
  StrictMock<MockPollable> p;

  loop.setPoller(std::make_unique<LivePoller>());
  loop.registerPollables(p);

  auto now = std::chrono::system_clock::now();
  auto first_time = clock_cast<Clock>(now + 10ms);
  auto second_time = clock_cast<Clock>(now + 20ms);
  auto third_time = clock_cast<Clock>(now + 30ms);

  loop.onTimeout(second_time, [&] { EXPECT_EQ(Clock::now(), second_time); });

  // Mock the pollable object to have events 10ms and 20ms from the current time
  EXPECT_CALL(p, poll())
      .WillOnce(Return(PollResult(first_time, [&] { p.dispatch(); })))
      .WillOnce(Return(PollResult(third_time, [&] { p.dispatch(); })))
      .WillOnce(Return(PollResult(clock_cast<Clock>(now + 100ms), [&] { p.dispatch(); })));

  EXPECT_CALL(p, dispatch()).Times(3);
  loop.runUntil(clock_cast<Clock>(now + 50ms));
}

} // namespace
} // namespace pktrade
