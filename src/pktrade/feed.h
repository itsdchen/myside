#pragma once

#include <algorithm>
#include <chrono>
#include <deque>
#include <filesystem>
#include <fstream>
#include <optional>

#include <glog/logging.h>
#include <rapidjson/document.h>
#include <zmq.hpp>

#include "pktrade/decoder.h"
#include "pktrade/mdapi.h"
#include "pktrade/pollable.h"

#include "mdmsg.pb.h"

#include <google/protobuf/io/gzip_stream.h>
#include <google/protobuf/io/zero_copy_stream_impl.h>
#include <google/protobuf/util/delimited_message_util.h>
namespace pktrade {

class MDListener {
 public:
  virtual ~MDListener() = default;

  virtual void onMD(const mdmsg::PbMessage&) = 0;

  // Most people don't need this, but used for
  // doing some verification. This is listened to by the debug listener.
  virtual void onLastLine(const std::string& msg_raw_line) {}
};

class LiveFeed : public Pollable {
 public:
  explicit LiveFeed(MDListener* listener);
  ~LiveFeed() {
    sock_.close();
    ctx_.close();
  }

  void dispatch();

  PollResult poll() override;

 private:
  zmq::context_t ctx_;
  zmq::socket_t sock_;
  MDListener* listener_ = nullptr;

  uint64_t msg_count_ = 0;
  std::chrono::steady_clock::time_point created_at_ = std::chrono::steady_clock::now();
  bool alerted_no_data_ = false;
};

class HistoricalFeed : public Pollable {
 public:
  template <typename ForwardIt>
  HistoricalFeed(MDListener* listener, ForwardIt begin, ForwardIt end, int date,
                 bool dispatch_last_lines = false, bool use_pbf = true, bool compress = true)
      : listener_{listener}, dispatch_last_lines_(dispatch_last_lines) {
    for (auto it = begin; it != end; ++it) {
      // Sanity check for histfeeds, if it doesn't exist.
      if (!std::filesystem::exists(it->first)) {
        if (it->first.string().find("depthSnapshot") != std::string::npos) {
          // Potentially be OK if it's a snapshot.
          std::cout << it->first << " does not exist. Giving you a pass for now." << std::endl;
        } else {
          // Don't complain that hard at all, actually.
          // Complain a little bit I guess.

          // LOG(ERROR) << "Historicalfeed: tried to load " << it->first
          //            << " But doesnt exist";
          // throw std::runtime_error(
          //     std::string("HistoricalFeed: tried to load but could not find
          //     ") + std::string(it->first));
        }
      }
      auto& is = streams_.emplace_back(it->first);
      auto& node = heap_.emplace_back(Node{.path = it->first,
                                           .is = &is,
                                           .mkt = it->second,
                                           .is_pbf = use_pbf,
                                           .compress = compress,
                                           .debug_set_last_line = dispatch_last_lines});

      // In the case that it is pbf, set its members up.
      node.init_node();
      bool has_any_message = node.getNextMessage();
      if (!has_any_message) {
        // Comment this out if this is expected behavior...
        // throw std::runtime_error(std::string("Dying because feed file for ")
        // +
        //                         node.path + std::string(" is empty."));
        // std::cout << "Popping " << node.path << " because it was empty"
        //          << std::endl;
        heap_.pop_back();
      }
    }
    std::make_heap(heap_.begin(), heap_.end());
  }

  void dispatch();

  PollResult poll() override;

  void addDebugListener(MDListener* debug_listener, bool dispatch_last_lines) {
    debug_listener_ = debug_listener;
    dispatch_last_lines_ = dispatch_last_lines;

    // Go through nodes and update their settings.
    for (Node& nd : heap_) {
      nd.debug_set_last_line = true;
    }
  }

 private:
  struct Node {
    std::string path;
    std::ifstream* is;

    std::string last_line_;
    int count = 0;

    // Set the next rx_timestamp.
    int64_t rx_timestamp = 0;

    // Does this correspond to a market?
    Market mkt;
    mdmsg::PbMessage last_msg;
    // cached_msgs is used for feeds which return vector of pb messages in a
    // single json line such as Bybit. Handlers for such feeds should refill
    // cached_msgs if it is empty, then pop front of cached_msgs into last_msg.
    std::deque<mdmsg::PbMessage> cached_msgs;

    // Reversing so we can make it a min heap.
    bool operator<(const Node& rhs) const {
      // This used to be
      //       return rx_timestamp > rhs.rx_timestamp;
      // However, this is a little problematic if we have trades and bookTickers
      // at the same time. In that case, we tiebreak by preferring trades first.
      // This isn't always going to happen IRL, sorry. But the important thing
      // is that we have a predictable ordering of streams (if we actually
      // subscribe to mulitple symbols, the original way may have given us a
      // different ordering). The benefit is that this (probably) also prevents
      // us from double-flagging price levels, in books that have flagging.
      // TODO: I don't really love that this is just checking the last_msg.
      // Probably the better long-term answer is to have each node know what its
      // type is and use that type to give an ordering.
      if (rx_timestamp > rhs.rx_timestamp) {
        return true;
      } else if (rx_timestamp < rhs.rx_timestamp) {
        return false;
      } else {
        // Tiebreaking if the rx_times are the same.
        if (last_msg.has_book_trade()) {
          // trades come first.
          return false;
        } else if (last_msg.has_ticker_update() && (!rhs.last_msg.has_book_trade())) {
          // bookticker comes before updates.
          return false;
        } else {
          // This node is like an update or something. it updates later.
          return true;
        }
      }
    }

    bool getNextMessage();

    void callMDListener(MDListener* a_listener);

    bool is_pbf;
    bool compress;
    bool debug_set_last_line = false;
    // Call after construction I guess.
    google::protobuf::io::GzipInputStream* gz_fin = nullptr;
    google::protobuf::io::IstreamInputStream* proto_fin = nullptr;

    void init_node();
    // clean up ourselves
    void close_node();

    // If this flag true, then print every time we call getNextMessage().
    // Just so we can debug with
  };

  std::vector<Node> heap_;
  std::deque<std::ifstream> streams_;
  MDListener* listener_ = nullptr;

  // Kind of a hack. But want to have a way to read in data lines to check
  // correctness.
  MDListener* debug_listener_ = nullptr;
  bool dispatch_last_lines_ = false;

  // For datasets where we store in ms, we'll need to take the rx-ms and turn
  // them into ns. This is just true for tardis datasets.
  bool adjust_ms_to_ns_ = true;
  int64_t last_ms_seen_ = 0;
  int64_t last_ns_delivered_ = 0;
};

} // namespace pktrade
