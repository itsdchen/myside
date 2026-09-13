#include "pktrade/feed.h"

#include <fcntl.h>
#include <fmt/format.h>
#include <glog/logging.h>
#include <google/protobuf/io/zero_copy_stream_impl.h>
#include <google/protobuf/util/delimited_message_util.h>

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"

namespace pktrade {

namespace {

std::string hexPrefix(const void* data, size_t len, size_t max_bytes = 128) {
  static constexpr char kHex[] = "0123456789abcdef";
  const auto* bytes = static_cast<const unsigned char*>(data);
  size_t n = len < max_bytes ? len : max_bytes;
  std::string out;
  out.reserve(n * 3 + 32);
  for (size_t i = 0; i < n; ++i) {
    if (i) out.push_back(' ');
    unsigned char b = bytes[i];
    out.push_back(kHex[b >> 4]);
    out.push_back(kHex[b & 0x0f]);
  }
  if (len > n) {
    out += fmt::format(" ...(+{} bytes)", len - n);
  }
  return out;
}

} // namespace

LiveFeed::LiveFeed(MDListener* listener) : sock_{ctx_, ZMQ_SUB}, listener_{listener} {
  sock_.connect("ipc:///tmp/feed-sock");
  sock_.connect("ipc:///tmp/feed-sock-cpp");
  sock_.set(zmq::sockopt::subscribe, "");
}

void LiveFeed::dispatch() {
  zmq::message_t msg;
  auto rc = sock_.recv(msg);
  CHECK(rc);
  if (rc) {
    mdmsg::PbMessage pb_msg;
    if (!pb_msg.ParseFromArray(msg.data(), msg.size())) {
      LOG_EVERY_N(ERROR, 100)
          << "LiveFeed: failed to parse mdmsg PbMessage len=" << msg.size()
          << " hex_prefix=" << hexPrefix(msg.data(), msg.size());
      return;
    }

    listener_->onMD(pb_msg);

    // Compare it against the time in msg.
  }
}

PollResult LiveFeed::poll() {
  zmq::pollitem_t items[] = {
      {sock_, 0, ZMQ_POLLIN, 0},
  };

  zmq::poll(&items[0], 1, std::chrono::milliseconds{0});
  if (items[0].revents & ZMQ_POLLIN) {
    ++msg_count_;
    return PollResult{clock_cast<Clock>(std::chrono::system_clock::now()), [this] { dispatch(); }};
  }

  if (pktrade::GlobalVar::live_ && !pktrade::GlobalVar::paper_trading_mode_ &&
      msg_count_ == 0 && !alerted_no_data_ &&
      std::chrono::steady_clock::now() - created_at_ > std::chrono::minutes(5)) {
    alerted_no_data_ = true;
    LOG(ERROR) << "LiveFeed: no messages received after 5 minutes!";
    auto subject = fmt::format("{}: LiveFeed no data after 5 min", pktrade::GlobalVar::strat_id_);
    pktrade::util::Mailer::instance().send_mail(
        subject, "LiveFeed has not received any market data messages since startup.");
  }

  return PollResult{};
}

// NB: heap property should be maintained as both pre and post conditions
// of this function
void HistoricalFeed::dispatch() {
  std::pop_heap(heap_.begin(), heap_.end());
  auto& back_node = heap_.back();

  if (dispatch_last_lines_) {
    // Share the raw last lines to the debug listener.
    debug_listener_->onLastLine(back_node.last_line_);
  }

  if (debug_listener_ != nullptr) {
    back_node.callMDListener(debug_listener_);
  }

  // Dispatch
  back_node.callMDListener(listener_);

  bool has_next_message = back_node.getNextMessage();
  back_node.count++;

  if (has_next_message) {
    std::push_heap(heap_.begin(), heap_.end());
  } else {
    heap_.pop_back();
  }
}

PollResult HistoricalFeed::poll() {
  if (heap_.empty()) {
    return PollResult::done();
  }

  int64_t front_ts = heap_.front().rx_timestamp;

  if (adjust_ms_to_ns_) {
    if (last_ms_seen_ == front_ts) {
      // Same ms time. Increment the last delivered ns time and use that
      // as the timestamp.
      last_ns_delivered_++;
    } else {
      // ms to ns.
      // Note: I multiply by 1000000 explicitly because
      // 1e6 resolves to the double, and that gives us some rounding issue.
      last_ns_delivered_ = front_ts * 1000000;
      last_ms_seen_ = front_ts;
    }

  } else {
    // Otherwise, we're already using ns?
    last_ns_delivered_ = front_ts;
  }

  return PollResult{TimePoint{std::chrono::nanoseconds{last_ns_delivered_}},
                    [this] { dispatch(); }};
}

void HistoricalFeed::Node::init_node() {
  // Mainly do stuff for pbf.
  if (is_pbf) {
    proto_fin = new google::protobuf::io::IstreamInputStream(is);
    if (compress) {
      gz_fin = new google::protobuf::io::GzipInputStream(proto_fin);
    }
  }
}

bool HistoricalFeed::Node::getNextMessage() {
  // If classic mode, we read the jsonls.
  if (is_pbf) {
    // Read protobuf stuff.
    bool has_more = false;

    last_msg.Clear();
    if (compress) {
      has_more =
          google::protobuf::util::ParseDelimitedFromZeroCopyStream(&last_msg, gz_fin, nullptr);
    } else {
      has_more =
          google::protobuf::util::ParseDelimitedFromZeroCopyStream(&last_msg, proto_fin, nullptr);
    }
    rx_timestamp = last_msg.rx_timestamp();

    if (mkt == pktrade::GlobalVar::slowdown_market_) {
      // 120 + 1100;
      // I'm gonna try this for a few other symbols too...
      rx_timestamp += pktrade::GlobalVar::slowdown_market_offset_;
    }

    // If we're in debug momde, set last_line too.
    if (debug_set_last_line) {
      last_line_ = last_msg.DebugString();
    }

    return has_more;
  } else {

    // hardcoded this for now.
    bool our_capture = false;
    bool store_ns = false;

    // classic, jsonl stuff. Tbh, we almost never use this anymore.
    if (std::getline(*(is), last_line_)) {
      // More data to read
      if (mkt == Market::BinanceSPOT) {
        last_msg = decodeBinanceStreamData(last_line_, our_capture, store_ns);
        rx_timestamp = last_msg.rx_timestamp();
        return true;
      } else if (mkt == Market::BinanceFutures) {
        last_msg = decodeBinanceFuturesStreamData(last_line_, our_capture, store_ns);
        rx_timestamp = last_msg.rx_timestamp();
        return true;
      } else if (mkt == Market::BinanceCOINFutures) {
        last_msg = decodeBinanceCOINFuturesStreamData(last_line_, our_capture, store_ns);
        rx_timestamp = last_msg.rx_timestamp();
        return true;
      } else if (mkt == Market::BybitSPOT || mkt == Market::BybitDeriv ||
                 mkt == Market::BybitInverseDeriv) {
        if (cached_msgs.empty()) {
          auto msgs = decodeBybitStreamData(last_line_, mkt, our_capture, store_ns);
          cached_msgs.insert(cached_msgs.begin(), msgs.begin(), msgs.end());
        }
        last_msg = cached_msgs.front();
        cached_msgs.pop_front();
        rx_timestamp = last_msg.rx_timestamp();
        return true;
      }
    }
    return false;
  }
  return false;
}

void HistoricalFeed::Node::callMDListener(MDListener* a_listener) { a_listener->onMD(last_msg); }

} // namespace pktrade
