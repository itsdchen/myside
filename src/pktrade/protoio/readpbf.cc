// readpbf — dump PB-record files in a few related framings.
//
// We have two related on-disk PB framings in this tree:
//
//   - "delimited": each record is a varint-length-prefixed PbMessage
//     (`SerializeDelimitedToZeroCopyStream`). This is what
//     `jsontoproto` writes (.pbf raw, .gzpbf when gzipped).
//
//   - "hlnode": each record is `[8B recv_ts_ns LE][4B pb_len LE][pb_bytes]`,
//     gzip-wrapped. This is what `simple_datalog --hlnode` writes
//     (.pb.gz), and the only format that carries a per-record receive
//     timestamp at the framing layer.
//
// readpbf walks either framing and prints either:
//   - a one-line-per-record human summary (`--out-format text`, default), or
//   - one JSON line per record (`--out-format jsonl`) shaped like
//     Hyperliquid's public WS JSON so you can diff against a
//     `.capture` file recorded by `simple_datalog --market Hyperliquid`.
//
// Sample:
//   readpbf --src new_hl_node/hl_node_books_20260612_0157.pb.gz \
//           --out-format jsonl --sym xyz:XYZ100 \
//     > new_books_l2book.jsonl
//
// Format / gzip is auto-detected by suffix (.pb.gz / .gzpbf / .pbf);
// use --format and --gzip to override.

#include "mdmsg.pb.h"
#include "pktrade/mdapi.h"
#include "pktrade/util/cli.h"

#include <google/protobuf/io/gzip_stream.h>
#include <google/protobuf/io/zero_copy_stream_impl.h>
#include <google/protobuf/util/delimited_message_util.h>

#include <fcntl.h>
#include <unistd.h>
#include <zlib.h>

#include <endian.h>
#include <fmt/format.h>
#include <nlohmann/json.hpp>

#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

using namespace pktrade;
// ordered_json preserves insertion order, so the field sequence matches
// what HL's public WS produces (and what CombinedStreamSplitter appends
// to each line). Default nlohmann::json would sort keys alphabetically
// and break naive `diff` against a .capture file.
using json = nlohmann::ordered_json;

namespace {

enum class InputFormat { Delimited, HlNode };
enum class OutputFormat { Text, Jsonl };

bool ends_with(const std::string& s, const std::string& suffix) {
  return s.size() >= suffix.size() &&
         s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

// Emit one HL-WS-shaped JSON line per PbMessage, so the output can be
// line-compared against a .capture file from --market Hyperliquid.
//   book_snapshot → {"channel":"l2Book","data":{...},"rx":<ns>}
//   book_trade    → {"channel":"trades","data":[{...}],"rx":<ns>}
// `recv_ts_ns < 0` means "framing didn't carry a recv timestamp"; the rx
// field is then omitted.
void emit_jsonl(const mdmsg::PbMessage& msg, int64_t recv_ts_ns) {
  // For delimited inputs (gzpbf), the per-record rx_ns header doesn't exist
  // so caller passes -1; fall back to PbMessage.rx_timestamp (set in ms by
  // jsontoproto / hlnode_gzsplitter / live publishers).
  int64_t rx_out_ns = recv_ts_ns;
  if (rx_out_ns < 0 && msg.rx_timestamp() > 0) {
    rx_out_ns = static_cast<int64_t>(msg.rx_timestamp()) * 1'000'000;
  }
  if (msg.has_book_snapshot()) {
    const auto& bs = msg.book_snapshot();
    json j;
    j["channel"] = "l2Book";
    json d;
    d["coin"] = msg.symbol_id();
    d["time"] = bs.event_time();
    json bids = json::array(), asks = json::array();
    for (const auto& l : bs.bids()) {
      bids.push_back({{"px", l.px()}, {"sz", l.qty()}, {"n", l.numords()}});
    }
    for (const auto& l : bs.asks()) {
      asks.push_back({{"px", l.px()}, {"sz", l.qty()}, {"n", l.numords()}});
    }
    d["levels"] = json::array({bids, asks});
    j["data"] = d;
    if (rx_out_ns >= 0) j["rx"] = rx_out_ns;
    std::cout << j.dump() << "\n";
  } else if (msg.has_book_trade()) {
    const auto& bt = msg.book_trade();
    json j;
    j["channel"] = "trades";
    json trade;
    trade["coin"] = msg.symbol_id();
    // PbTrade.passive_was_buyer carries the PASSIVE (maker) perspective;
    // HL's WS `side` field is the AGGRESSOR (taker) perspective. Flip
    // the boolean so the JSONL matches what a .capture file from HL's
    // public WS would have written.
    trade["side"] = bt.passive_was_buyer() ? "A" : "B";
    trade["px"] = bt.px();
    trade["sz"] = bt.qty();
    trade["time"] = bt.trade_time();
    trade["tid"] = bt.trade_id();
    j["data"] = json::array({trade});
    if (rx_out_ns >= 0) j["rx"] = rx_out_ns;
    std::cout << j.dump() << "\n";
  }
  // Drop other oneofs silently — the comparison only cares about l2 + trades.
}

// Human-readable one-line summary per record.
void emit_text(const mdmsg::PbMessage& msg, int64_t recv_ts_ns) {
  std::string rx_part =
      (recv_ts_ns >= 0) ? fmt::format("rx={}", recv_ts_ns) : "rx=-";
  if (msg.has_book_snapshot()) {
    const auto& bs = msg.book_snapshot();
    fmt::print("{} l2Book sym={} t={} bids={} asks={} top={}/{}\n",
               rx_part, msg.symbol_id(), bs.event_time(), bs.bids_size(),
               bs.asks_size(),
               bs.bids_size() ? bs.bids(0).px() : "-",
               bs.asks_size() ? bs.asks(0).px() : "-");
  } else if (msg.has_book_trade()) {
    const auto& bt = msg.book_trade();
    fmt::print("{} trade  sym={} t={} side={} px={} sz={} tid={}\n",
               rx_part, msg.symbol_id(), bt.trade_time(),
               bt.passive_was_buyer() ? "A" : "B", bt.px(), bt.qty(),
               bt.trade_id());
  } else {
    fmt::print("{} other  sym={} (no known oneof set)\n", rx_part,
               msg.symbol_id());
  }
}

// Walk a delimited stream (one varint-length-prefixed PbMessage per record).
void read_delimited(const std::string& src, bool gzip, OutputFormat out_fmt,
                    const std::string& sym_filter, int64_t max) {
  int infd = open(src.c_str(), O_RDONLY);
  if (infd < 0) {
    std::cerr << "open failed: " << src << " (" << std::strerror(errno) << ")\n";
    return;
  }
  google::protobuf::io::FileInputStream fin(infd);
  google::protobuf::io::GzipInputStream gz(&fin);
  google::protobuf::io::ZeroCopyInputStream* stream =
      gzip ? static_cast<google::protobuf::io::ZeroCopyInputStream*>(&gz)
           : static_cast<google::protobuf::io::ZeroCopyInputStream*>(&fin);

  int64_t count = 0;
  while (max <= 0 || count < max) {
    mdmsg::PbMessage msg;
    bool clean_eof = false;
    bool ok = google::protobuf::util::ParseDelimitedFromZeroCopyStream(
        &msg, stream, &clean_eof);
    if (!ok) {
      if (!clean_eof) {
        std::cerr << "parse error after " << count << " records\n";
      }
      break;
    }
    if (!sym_filter.empty() && msg.symbol_id() != sym_filter) continue;
    if (out_fmt == OutputFormat::Jsonl) emit_jsonl(msg, -1);
    else emit_text(msg, -1);
    ++count;
  }
  std::cerr << "Done. Emitted " << count << " records.\n";
  fin.Close();
  close(infd);
}

// Walk an hlnode-framed stream. zlib's gz API gives byte-level reads
// without an intermediate ZeroCopy adapter, which keeps the 8+4-byte
// header parsing trivial.
void read_hlnode(const std::string& src, OutputFormat out_fmt,
                 const std::string& sym_filter, int64_t max) {
  gzFile gz = gzopen(src.c_str(), "rb");
  if (!gz) {
    std::cerr << "gzopen failed: " << src << "\n";
    return;
  }
  gzbuffer(gz, 256 * 1024);

  std::vector<char> buf(64 * 1024);
  int64_t count = 0;
  int64_t skipped_filter = 0;
  while (max <= 0 || count < max) {
    uint64_t ts_le = 0;
    uint32_t len_le = 0;
    int got = gzread(gz, &ts_le, sizeof(ts_le));
    if (got == 0) break;  // clean EOF
    if (got != static_cast<int>(sizeof(ts_le))) {
      std::cerr << "short read on recv_ts header (" << got << " bytes)\n";
      break;
    }
    got = gzread(gz, &len_le, sizeof(len_le));
    if (got != static_cast<int>(sizeof(len_le))) {
      std::cerr << "short read on len header\n";
      break;
    }
    int64_t recv_ts_ns = static_cast<int64_t>(le64toh(ts_le));
    uint32_t pb_len = le32toh(len_le);
    if (pb_len > buf.size()) buf.resize(pb_len);
    got = gzread(gz, buf.data(), pb_len);
    if (got != static_cast<int>(pb_len)) {
      std::cerr << "short read on pb payload (got " << got << " want "
                << pb_len << ")\n";
      break;
    }
    mdmsg::PbMessage msg;
    if (!msg.ParseFromArray(buf.data(), pb_len)) {
      std::cerr << "pb parse failed at record " << count << "\n";
      continue;
    }
    if (!sym_filter.empty() && msg.symbol_id() != sym_filter) {
      ++skipped_filter;
      continue;
    }
    if (out_fmt == OutputFormat::Jsonl) emit_jsonl(msg, recv_ts_ns);
    else emit_text(msg, recv_ts_ns);
    ++count;
  }
  std::cerr << "Done. Emitted " << count << " records";
  if (!sym_filter.empty()) std::cerr << " (skipped " << skipped_filter << " by sym filter)";
  std::cerr << ".\n";
  gzclose(gz);
}

}  // namespace

int main(int argc, char** argv) {
  std::string src;
  std::string fmt_str = "auto";
  std::string outfmt_str = "text";
  std::string gzip_str = "auto";
  std::string sym;
  int64_t max = 0;

  CLI::App cmd("readpbf - dump PB record files (.pbf / .gzpbf / .pb.gz)");
  cmd.add_option("--src", src, "Input file path")->required();
  cmd.add_option("--format", fmt_str,
                 "Input framing: auto (default), delimited (.pbf/.gzpbf), "
                 "hlnode (.pb.gz from simple_datalog --hlnode)");
  cmd.add_option("--gzip", gzip_str, "auto (default), on, or off");
  cmd.add_option("--out-format", outfmt_str,
                 "text (default) or jsonl (HL-WS-shaped)");
  cmd.add_option("--sym", sym, "Filter to this symbol_id");
  cmd.add_option("--max", max, "Max records to emit (0 = no limit)");
  PARSE(cmd, argc, argv);

  InputFormat fmt;
  bool gzip = false;
  if (fmt_str == "delimited") {
    fmt = InputFormat::Delimited;
  } else if (fmt_str == "hlnode") {
    fmt = InputFormat::HlNode;
  } else if (fmt_str == "auto") {
    if (ends_with(src, ".pb.gz")) {
      fmt = InputFormat::HlNode;
      gzip = true;
    } else if (ends_with(src, ".gzpbf")) {
      fmt = InputFormat::Delimited;
      gzip = true;
    } else if (ends_with(src, ".pbf")) {
      fmt = InputFormat::Delimited;
      gzip = false;
    } else {
      std::cerr << "cannot auto-detect format from suffix; use --format\n";
      return 1;
    }
  } else {
    std::cerr << "unknown --format " << fmt_str << "\n";
    return 1;
  }

  if (gzip_str == "on") gzip = true;
  else if (gzip_str == "off") gzip = false;
  // "auto" leaves whatever the format-detect picked.

  OutputFormat out_fmt =
      (outfmt_str == "jsonl") ? OutputFormat::Jsonl : OutputFormat::Text;

  if (fmt == InputFormat::Delimited) {
    read_delimited(src, gzip, out_fmt, sym, max);
  } else {
    // hlnode is always gzipped today (simple_datalog writes via gzwrite).
    read_hlnode(src, out_fmt, sym, max);
  }

  return 0;
}
