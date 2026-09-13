// hlnode_gzsplitter — postproc for HL node datalog captures.
//
// simple_datalog --hlnode writes the n1 publisher's PbMessages to disk in
// a custom gzip framing: `[8B recv_ts_ns LE][4B pb_len LE][pb_bytes]` per
// record, all gzip-compressed. That's good for capture (cheap, carries
// per-record receive timestamps) but isn't the on-disk format the rest of
// the tree (livemsgprint, the replay pipeline, anyone reading via
// ParseDelimitedFromZeroCopyStream) expects.
//
// This tool does two things at once:
//
//   1. Re-encodes from our framed `.pb.gz` into the canonical `.gzpbf`
//      framing (gzip envelope around varint-length-prefixed PbMessages,
//      what jsontoproto produces from .jsonl WS captures).
//
//   2. Splits the single mixed-sym input into per-(sym, stream) files
//      following the same naming convention as CombinedStreamSplitter +
//      jsontoproto: `<sym>_<stream>_<date>.gzpbf`.
//      stream is "l2Book" for book_snapshot records, "trades" for
//      book_trade records — derived from PbMessage's oneof.
//
// Each record's recv_ts_ns header is preserved as PbMessage.rx_timestamp
// in milliseconds (matching what jsontoproto produces with --our-capture).
//
// Usage:
//   hlnode_gzsplitter --src hl_node_books_<dt>.pb.gz \
//                     --outdir gzpbf/Hyperliquid \
//                     --date 20260612
//
// Run separately for the books and fills captures; the tool reads either
// without configuration since the stream is per-record in the proto.
//
// Notes:
//   - HL fast+slow merge IS NOT relevant on the node side: the n1
//     publisher already emits a merged top-N view (`emit_coin` with
//     --max-levels) per block boundary; there is no separate fast stream
//     in node data. Don't bolt the WS merge logic on here.
//   - One output writer is held open per (sym, stream) pair for the
//     duration of the run. For datalog files spanning a few hundred syms
//     that's a few hundred open fds — within ulimit. If we ever grow
//     into syms-in-thousands territory we'd want LRU-cache the writers.

#include "mdmsg.pb.h"
#include "pktrade/util/cli.h"

#include <google/protobuf/io/gzip_stream.h>
#include <google/protobuf/io/zero_copy_stream_impl.h>
#include <google/protobuf/util/delimited_message_util.h>

#include <fcntl.h>
#include <unistd.h>
#include <zlib.h>

#include <endian.h>
#include <fmt/format.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

// Holds the three layers needed to write delimited PbMessages into a
// gzipped file. They have to stay alive together — the GzipOutputStream
// references the FileOutputStream, which owns the fd.
struct GzpbfWriter {
  int fd = -1;
  std::unique_ptr<google::protobuf::io::FileOutputStream> file_out;
  std::unique_ptr<google::protobuf::io::GzipOutputStream> gz_out;
  int64_t records_written = 0;

  bool open(const std::string& path, int compress_level) {
    fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) {
      std::cerr << "open failed: " << path << " (" << std::strerror(errno) << ")\n";
      return false;
    }
    file_out = std::make_unique<google::protobuf::io::FileOutputStream>(fd);
    google::protobuf::io::GzipOutputStream::Options opts;
    opts.compression_level = compress_level;
    gz_out = std::make_unique<google::protobuf::io::GzipOutputStream>(file_out.get(), opts);
    return true;
  }

  ~GzpbfWriter() {
    if (gz_out) gz_out->Close();
    if (file_out) file_out->Close();
    if (fd >= 0) ::close(fd);
  }

  bool write(const pktrade::mdmsg::PbMessage& msg) {
    if (!google::protobuf::util::SerializeDelimitedToZeroCopyStream(msg, gz_out.get())) {
      std::cerr << "SerializeDelimitedToZeroCopyStream failed\n";
      return false;
    }
    ++records_written;
    return true;
  }
};

// Stream tag derived from the PbMessage oneof. Mirrors what
// CombinedStreamSplitter writes: "l2Book" for snapshots, "trades" for
// trades. Returns empty if the message has neither (caller should skip).
std::string stream_tag(const pktrade::mdmsg::PbMessage& msg) {
  if (msg.has_book_snapshot()) return "l2Book";
  if (msg.has_book_trade()) return "trades";
  return "";
}

}  // namespace

int main(int argc, char** argv) {
  std::string src;
  std::string outdir;
  std::string date_str;
  int compress_level = 5;
  std::string only_sym;
  int64_t max_records = 0;

  CLI::App cmd(
      "hlnode_gzsplitter — convert simple_datalog --hlnode .pb.gz captures "
      "into per-(sym, stream) .gzpbf files");
  cmd.add_option("--src", src, "Input .pb.gz path (books or fills)")->required();
  cmd.add_option("--outdir", outdir, "Output dir for per-(sym, stream) .gzpbf files")
      ->required();
  cmd.add_option("--date", date_str,
                 "Date for output filenames (YYYYMMDD)")
      ->required();
  cmd.add_option("--compress-level", compress_level, "gzip level for output (1-9)");
  cmd.add_option("--sym", only_sym,
                 "Optional: restrict to this single symbol_id (debug/slice)");
  cmd.add_option("--max", max_records,
                 "Optional cap on input records read (0 = no cap)");
  PARSE(cmd, argc, argv);

  std::filesystem::create_directories(outdir);

  gzFile gz_in = gzopen(src.c_str(), "rb");
  if (!gz_in) {
    std::cerr << "gzopen failed: " << src << "\n";
    return 1;
  }
  gzbuffer(gz_in, 256 * 1024);

  // (sym, stream) → writer. Held open for the duration of the run.
  std::unordered_map<std::string, std::unique_ptr<GzpbfWriter>> writers;

  auto writer_for = [&](const std::string& sym, const std::string& stream)
      -> GzpbfWriter* {
    std::string key = sym + "|" + stream;
    auto it = writers.find(key);
    if (it != writers.end()) return it->second.get();
    // Sanitize sym for filename use — spot pairs like "PURR/USDC" contain
    // '/' which would be interpreted as a directory separator. The pb
    // record's symbol_id field is left untouched.
    std::string sym_fname = sym;
    std::replace(sym_fname.begin(), sym_fname.end(), '/', '_');
    std::string fname = fmt::format("{}_{}_{}.gzpbf", sym_fname, stream, date_str);
    std::string path = (std::filesystem::path(outdir) / fname).string();
    auto w = std::make_unique<GzpbfWriter>();
    if (!w->open(path, compress_level)) return nullptr;
    auto* raw = w.get();
    writers.emplace(std::move(key), std::move(w));
    return raw;
  };

  std::vector<char> buf(64 * 1024);
  int64_t total_read = 0;
  int64_t total_written = 0;
  int64_t skipped_other_oneof = 0;
  int64_t skipped_sym_filter = 0;

  while (max_records <= 0 || total_read < max_records) {
    uint64_t ts_le = 0;
    uint32_t len_le = 0;
    int got = gzread(gz_in, &ts_le, sizeof(ts_le));
    if (got == 0) break;  // clean EOF
    if (got != static_cast<int>(sizeof(ts_le))) {
      std::cerr << "short read on recv_ts header (" << got << " bytes)\n";
      break;
    }
    got = gzread(gz_in, &len_le, sizeof(len_le));
    if (got != static_cast<int>(sizeof(len_le))) {
      std::cerr << "short read on len header\n";
      break;
    }
    int64_t recv_ts_ns = static_cast<int64_t>(le64toh(ts_le));
    uint32_t pb_len = le32toh(len_le);
    if (pb_len > buf.size()) buf.resize(pb_len);
    got = gzread(gz_in, buf.data(), pb_len);
    if (got != static_cast<int>(pb_len)) {
      std::cerr << "short read on pb payload (got " << got << " want "
                << pb_len << ")\n";
      break;
    }

    pktrade::mdmsg::PbMessage msg;
    if (!msg.ParseFromArray(buf.data(), pb_len)) {
      std::cerr << "pb parse failed at record " << total_read << "\n";
      continue;
    }
    ++total_read;

    if (!only_sym.empty() && msg.symbol_id() != only_sym) {
      ++skipped_sym_filter;
      continue;
    }

    std::string stream = stream_tag(msg);
    if (stream.empty()) {
      ++skipped_other_oneof;
      continue;
    }

    // Stamp rx_timestamp in ms (matches jsontoproto --our-capture
    // convention). The original ns value would be more precise but
    // PbMessage.rx_timestamp is documented as ms elsewhere.
    msg.set_rx_timestamp(recv_ts_ns / 1'000'000);

    auto* w = writer_for(msg.symbol_id(), stream);
    if (!w) {
      std::cerr << "writer_for failed for " << msg.symbol_id() << "/" << stream
                << "; skipping record.\n";
      continue;
    }
    if (!w->write(msg)) {
      std::cerr << "write failed for " << msg.symbol_id() << "/" << stream
                << "; skipping record.\n";
      continue;
    }
    ++total_written;
  }

  gzclose(gz_in);

  std::cerr << fmt::format(
      "Done. read {} records, wrote {} ({} skipped other-oneof, {} skipped sym-filter). "
      "{} output files in {}\n",
      total_read, total_written, skipped_other_oneof, skipped_sym_filter,
      writers.size(), outdir);
  return 0;
}
