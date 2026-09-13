
#include "mdmsg.pb.h"
#include <algorithm>
#include <map>
#include <rapidjson/document.h>
#include <string>
#include <vector>

#include "pktrade/decoder.h"
#include "pktrade/mdapi.h"
#include "pktrade/util/cli.h"

#include <google/protobuf/io/gzip_stream.h>

#include <google/protobuf/io/zero_copy_stream_impl.h>
#include <google/protobuf/util/delimited_message_util.h>

#include <fcntl.h>

using namespace pktrade;

// =============================================================================
// HL fast+slow l2Book merge (optional, opt-in via --hl-merge-fast-slow).
// =============================================================================
//
// Why this lives here:
//
// simple_datalog --market Hyperliquid subscribes to BOTH the default
// ("slow", top-20 @ ~5s) AND "fast" (top-5 @ ~0.5s) l2Book streams and
// writes both raw to disk. The live feeds (pymultifeed, pkmultifeed) do
// NOT publish the raw streams — they keep a per-sym cache of the latest
// fast and slow snapshots and publish a MERGED book on every l2Book
// event (fast levels first, slow contributes only px strictly outside
// fast's range; see mergeHyperliquidFastSlow in pkmultifeed.cc and
// hl_merge_book in pymultifeed.py).
//
// So if we blindly decode each captured l2Book line into its own
// PbBookSnapshot, the gzpbf used for replay would have depth alternate
// between 5 and 20 on every record — the receiving BookManager would do
// a delete/add trampoline for levels 6-20 on every fast push. That's
// NOT what the live feed produced.
//
// With --hl-merge-fast-slow on, the HL l2Book path here mirrors the live
// feed's merge: cache the latest fast and slow per sym, emit one merged
// PbBookSnapshot per incoming line. trades and other channels are
// unaffected. Default OFF so the raw "what HL actually sent" stream is
// still recoverable from the gzpbf without this flag.
//
// IMPORTANT: this is HL-WS-specific. Don't generalize without thought —
// no other market in this tree uses a "fast" companion stream today.
//
// If you change the merge rule here, also change it in
// pkmultifeed.cc (mergeHyperliquidFastSlow) and pymultifeed.py
// (hl_merge_book). The three must stay byte-for-byte equivalent or
// replay won't match live.
struct HLWsLevel {
  std::string px;
  std::string sz;
  int32_t n;
};
struct HLWsBook {
  std::vector<HLWsLevel> bids;
  std::vector<HLWsLevel> asks;
};

// Mirrors pkmultifeed::mergeHyperliquidFastSlow and pymultifeed.hl_merge_book.
// Fast top-5 levels first, then slow contributes only px strictly outside
// fast's price range, so the merged book stays monotonic by price.
static void mergeHyperliquidFastSlow(const HLWsBook* fast, const HLWsBook* slow,
                                     std::vector<HLWsLevel>* out_bids,
                                     std::vector<HLWsLevel>* out_asks) {
  out_bids->clear();
  out_asks->clear();
  if (!fast && !slow) return;
  if (!fast) { *out_bids = slow->bids; *out_asks = slow->asks; return; }
  if (!slow) { *out_bids = fast->bids; *out_asks = fast->asks; return; }

  // Bids: sorted high → low. Fast's last px is its lowest covered bid.
  if (!fast->bids.empty()) {
    double cutoff = std::stod(fast->bids.back().px);
    *out_bids = fast->bids;
    for (const auto& s : slow->bids) {
      if (std::stod(s.px) < cutoff) out_bids->push_back(s);
    }
  } else {
    *out_bids = slow->bids;
  }
  // Asks: sorted low → high. Fast's last px is its highest covered ask.
  if (!fast->asks.empty()) {
    double cutoff = std::stod(fast->asks.back().px);
    *out_asks = fast->asks;
    for (const auto& s : slow->asks) {
      if (std::stod(s.px) > cutoff) out_asks->push_back(s);
    }
  } else {
    *out_asks = slow->asks;
  }
}

// Replace one decoded book_snapshot with a merged version using the
// per-sym fast/slow caches. Caller decides classification by depth.
// In-place rewrite of `msg`'s book_snapshot bids/asks.
static void mergeHyperliquidL2BookInPlace(
    mdmsg::PbMessage& msg,
    std::map<std::string, HLWsBook>* fast_books,
    std::map<std::string, HLWsBook>* slow_books) {
  if (!msg.has_book_snapshot()) return;
  const auto& bs_in = msg.book_snapshot();
  const std::string& sym = msg.symbol_id();

  // Pull this snapshot's levels into a local HLWsBook, then classify
  // (depth ≤ 5 → fast bucket) and stash in the per-sym cache.
  HLWsBook this_snap;
  for (const auto& l : bs_in.bids()) {
    this_snap.bids.push_back({l.px(), l.qty(), l.numords()});
  }
  for (const auto& l : bs_in.asks()) {
    this_snap.asks.push_back({l.px(), l.qty(), l.numords()});
  }
  int32_t depth = static_cast<int32_t>(
      std::max(this_snap.bids.size(), this_snap.asks.size()));
  bool is_fast = depth <= 5;
  (is_fast ? *fast_books : *slow_books)[sym] = std::move(this_snap);

  // Merge against whatever's cached now and rewrite the proto's levels.
  const HLWsBook* fast_p = nullptr;
  const HLWsBook* slow_p = nullptr;
  if (auto it = fast_books->find(sym); it != fast_books->end()) fast_p = &it->second;
  if (auto it = slow_books->find(sym); it != slow_books->end()) slow_p = &it->second;
  std::vector<HLWsLevel> mb, ma;
  mergeHyperliquidFastSlow(fast_p, slow_p, &mb, &ma);

  auto* mbs = msg.mutable_book_snapshot();
  mbs->clear_bids();
  mbs->clear_asks();
  for (const auto& l : mb) {
    auto* pl = mbs->add_bids();
    pl->set_px(l.px); pl->set_qty(l.sz); pl->set_numords(l.n);
  }
  for (const auto& l : ma) {
    auto* pl = mbs->add_asks();
    pl->set_px(l.px); pl->set_qty(l.sz); pl->set_numords(l.n);
  }
}

/*
Example usage:
./bin/jsontoproto --src
~/tardis_datasets/BinanceFutures/ethusdt_trade_20230501.jsonl --dest
~/pkprojects/speed_protobuf_benchmarking/ethusdt_trade_20230501.jsonl.v2 --mkt
BinanceFutures


Basically, does what the deocder does: given a jsonl file and a notion of what
stream it is, will try to decode it to the appropriate proto file and write
things out.

Also, a lot of annoyance in learning how to write delimited protobufs.
SOme references:


https://stackoverflow.com/questions/2340730/are-there-c-equivalents-for-the-protocol-buffers-delimited-i-o-functions-in-ja
https://www.reddit.com/r/cpp/comments/j9mqsu/stdiostreams_are_a_bit_too_oversized_for_both/
https://github.com/protocolbuffers/protobuf/blob/main/src/google/protobuf/util/delimited_message_util.h
https://github.com/protocolbuffers/protobuf/pull/710


*/

// Maybe have a diff section for each of these.

// **********************************************************************************
// writing fxns

void writeProtoStream(std::string src, std::string dest, Market mkt, bool compress,
                      int compress_level, bool our_capture, bool store_ns,
                      bool hl_merge_fast_slow) {
  // Per-sym fast/slow l2Book caches, only touched when mkt == Hyperliquid
  // and hl_merge_fast_slow is on. See the comment at the top of this file
  // for what this is and why it lives here.
  std::map<std::string, HLWsBook> hl_fast_books;
  std::map<std::string, HLWsBook> hl_slow_books;
  std::ifstream is(src);
  std::string last_line_;

  std::cout << dest << std::endl;
  int outfd = open(dest.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
  google::protobuf::io::FileOutputStream fout(outfd);

  google::protobuf::io::GzipOutputStream::Options opts;
  // A number between 0 and 9, where 0 is no compression and 9 is best
  // compression.  Defaults to Z_DEFAULT_COMPRESSION (see zlib.h). Right now
  // about 6.
  opts.compression_level = compress_level;

  google::protobuf::io::GzipOutputStream gzip_stream(&fout, opts);

  bool success;
  int count = 0;
  while (true) {
    std::vector<mdmsg::PbMessage> msgs;
    if (std::getline(is, last_line_)) {
      // Check if it's well-formatted.
      if (last_line_[0] != '{' || last_line_[last_line_.size() - 1] != '}') {
        // Likely ill-formatted. Skip this line.
        continue;
      }
      if (mkt == Market::BinanceFutures) {
        msgs.push_back(decodeBinanceFuturesStreamData(last_line_, our_capture, store_ns));
      } else if (mkt == Market::BinanceSPOT) {
        msgs.push_back(decodeBinanceStreamData(last_line_, our_capture, store_ns));
        // success = google::protobuf::util::SerializeDelimitedToZeroCopyStream(
        //     msg, &fout);
      } else if (mkt == Market::BinanceCOINFutures) {
        msgs.push_back(decodeBinanceCOINFuturesStreamData(last_line_, our_capture, store_ns));
        // success = google::protobuf::util::SerializeDelimitedToZeroCopyStream(
        //     msg, &fout);
      } else if (mkt == Market::BybitDeriv || mkt == Market::BybitInverseDeriv ||
                 mkt == Market::BybitSPOT) {
        msgs = decodeBybitStreamData(last_line_, mkt, our_capture, store_ns);
      } else if (mkt == Market::Hyperliquid) {
        rapidjson::Document doc;
        doc.Parse(last_line_.c_str());
        // Should be stored in the rx field.
        int64_t rx = doc["rx"].GetInt64();
        msgs = decodeHyperliquidStreamData(doc, rx, our_capture, store_ns);
        // Optional: collapse the raw fast/slow split-stream capture into
        // the merged form pkmultifeed/pymultifeed publish live. Affects
        // book_snapshot only; book_trade passes through unchanged.
        if (hl_merge_fast_slow) {
          for (auto& msg : msgs) {
            if (msg.has_book_snapshot()) {
              mergeHyperliquidL2BookInPlace(msg, &hl_fast_books, &hl_slow_books);
            }
          }
        }
      }

      // Write out each msg
      for (auto msg : msgs) {
        if (compress) {
          google::protobuf::util::SerializeDelimitedToZeroCopyStream(msg, &gzip_stream);

        } else {
          google::protobuf::util::SerializeDelimitedToZeroCopyStream(msg, &fout);
        }
        count++;
      }
    } else {
      // Done!
      break;
    }
  }

  std::cout << "Done, wrote " << count << " messages" << std::endl;

  // Let's read and process I guess.
  //    binancefutures::PbMessage binance_futures_msg =
  //    decodeBinanceFuturesStreamData(last_line_);
}

// **********************************************************************************

// Things we need:
// Path of source
// What market
// Where to write.
int main(int argc, char** argv) {
  std::string src_path = "/home/david/tardis_datasets/BinanceFutures/ethusdt_trade_20230501.jsonl";
  std::string dest_path = "/home/david/pkprojects/speed_protobuf_benchmarking/eth_fut_out.pbf";
  std::string mkt_str;

  // Use gzip
  bool compress = false;
  // For our purposes, looks like level 5 is a good amount
  int compress_level = 5;

  // If true, we need to take the rx and convert it to ms.
  bool our_capture = false;
  // If true, converts things down to nanoseconds instead of
  // ms. Make this conversion when we're ready.
  bool store_ns = false;

  CLI::App cmd_flags("jsontoproto");
  cmd_flags.add_option("--src", src_path, "Source path")->required();
  cmd_flags.add_option("--dest", dest_path, "Dest path")->required();
  cmd_flags.add_option("--mkt", mkt_str, "Market")->required();
  cmd_flags.add_flag("--compress", compress, "Compress output in gzip");
  cmd_flags.add_option("--compress_level", compress_level, "Compress level");

  cmd_flags.add_flag("--our-capture", our_capture);
  cmd_flags.add_flag("--store_ms", store_ns);

  // Hyperliquid-only opt-in: cache fast+slow l2Book snapshots per sym and
  // emit a merged book per incoming record, mirroring the live feed's
  // mergeHyperliquidFastSlow / hl_merge_book. See the long comment at the
  // top of this file for rationale. Ignored for every other market.
  bool hl_merge_fast_slow = false;
  cmd_flags.add_flag("--hl-merge-fast-slow", hl_merge_fast_slow,
                     "(Hyperliquid only) Merge fast+slow l2Book into one "
                     "snapshot per record so replay matches live feed output.");

  // Not required for now.
  PARSE(cmd_flags, argc, argv);

  // OK, now we can read it. Let's read it. Need to also combine this w/
  Market mkt = magic_enum::enum_cast<Market>(mkt_str).value();

  // OK. Now I'm going to read this file and 1-by-1 encode it to protobuf.
  if (mkt == Market::BinanceFutures || mkt == Market::BinanceSPOT ||
      mkt == Market::BinanceCOINFutures || mkt == Market::Hyperliquid ||
      mkt == Market::BybitDeriv || mkt == Market::BybitInverseDeriv || mkt == Market::BybitSPOT) {
    writeProtoStream(src_path, dest_path, mkt, compress, compress_level, our_capture, store_ns,
                     hl_merge_fast_slow);
  } else {
    throw std::runtime_error(std::string("Unsupported market ") + mkt_str + "\n");
  }

  return 0;
}
