// hl_node_publisher
//
// >>> THIS FILE IS THE PRIMARY IMPLEMENTATION GOING FORWARD. <<<
// Its sibling is overmind/strat_main/tools/hl_node_publish.py — the two are
// intentionally tied to each other (same on-wire behavior, same CLI surface,
// same bug semantics). Until C++ reaches feature parity (currently:
// `--tail-fills-dir` fills mode and the debug instrumentation are still
// Python-only), the Python publisher remains canonical for those flows.
// Once parity is reached, future logic changes should land here first.
//
// What this file does (Passes 1+2):
//   - Reads HL non-validating node `node_raw_book_diffs` JSONL files (a
//     single file via --file, or the live hourly dir via --tail-dir).
//   - Maintains a per-coin L3 book from new/update/remove diffs (real),
//     plus an aggregated synth side seeded from public WS l2Book snapshots
//     when --bootstrap is on.
//   - Optional --bootstrap: discovers all syms via the `/info` HTTP
//     endpoint, runs a ContinuousWS thread that subscribes to the public
//     WS l2Book for every sym, applies Rules 1+2 reseed logic, and uses a
//     time-indexed history of recent emit-time top-5s for catch-up so the
//     criterion isn't broken by between-block WS samples or slow WS feeds.
//     Once a sym's real top-5 matches WS 3 in a row, the publisher
//     unsubscribes that sym and clears its synth.
//   - Emits one mdmsg::PbMessage with a PbBookSnapshot per coin per block
//     boundary on a ZMQ PUB socket.
//
// Sample commands:
//   ./bin/hl_node_publisher \
//       --tail-dir /home/ubuntu/hl/data/node_raw_book_diffs_streaming/hourly \
//       --bootstrap --coin xyz:XYZ100 \
//       --endpoint ipc:///tmp/hl_books.sock -v
//
//   ./bin/hl_node_publisher \
//       --file <path-to-book-diffs.jsonl> \
//       --print-only --coin BTC -v

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <cpr/cpr.h>
#include <fmt/format.h>
#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXSocketTLSOptions.h>
#include <ixwebsocket/IXWebSocket.h>
#include <rapidjson/document.h>
#include <rapidjson/error/en.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>
#include <zmq.hpp>

#include "mdmsg.pb.h"
#include "pktrade/util/cli.h"

using namespace pktrade;

namespace {

const std::string kDefaultPublicWsUrl = "wss://api.hyperliquid.xyz/ws";
const std::string kDefaultInfoUrl = "https://api.hyperliquid.xyz/info";

std::sig_atomic_t g_stop = 0;
void on_signal(int) { g_stop = 1; }

// ---- format_qty -------------------------------------------------------------
// Match Python's format_qty: render with up to 10 decimal places, strip
// trailing zeros and a trailing dot. Keeps the emitted PbBookSnapshot bytes
// equal to what Python produced.
std::string format_qty(double q) {
  if (q <= 0.0) return "0";
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%.10f", q);
  std::string s(buf);
  size_t end = s.find_last_not_of('0');
  if (end != std::string::npos && s[end] == '.') --end;
  s.resize(end + 1);
  if (s.empty()) return "0";
  return s;
}

// ---- parse_node_time_ms -----------------------------------------------------
// "2026-06-10T02:11:51.191853458" → Unix ms (truncates fractional to ms).
// -1 on failure.
int64_t parse_node_time_ms(const std::string& s) {
  if (s.empty()) return -1;
  std::tm tm{};
  int year, mon, mday, hh, mm, ss;
  int n = std::sscanf(s.c_str(), "%4d-%2d-%2dT%2d:%2d:%2d",
                      &year, &mon, &mday, &hh, &mm, &ss);
  if (n != 6) return -1;
  tm.tm_year = year - 1900;
  tm.tm_mon = mon - 1;
  tm.tm_mday = mday;
  tm.tm_hour = hh;
  tm.tm_min = mm;
  tm.tm_sec = ss;
  time_t t = timegm(&tm);
  if (t == (time_t)-1) return -1;
  int64_t ms = static_cast<int64_t>(t) * 1000;
  size_t dot = s.find('.');
  if (dot != std::string::npos) {
    int64_t frac_ms = 0;
    int digits = 0;
    for (size_t i = dot + 1; i < s.size() && digits < 3; ++i, ++digits) {
      char c = s[i];
      if (c < '0' || c > '9') break;
      frac_ms = frac_ms * 10 + (c - '0');
    }
    for (int i = digits; i < 3; ++i) frac_ms *= 10;
    ms += frac_ms;
  }
  return ms;
}

int64_t now_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// ---- Debug loggers (all off by default; opt-in via CLI flags) ---------------
//
// These three loggers are for investigating publisher correctness. They are
// off in production and only fire when the operator passes a corresponding
// CLI flag.
//
// trace_op   : --trace-pxs / --trace-out
//   "Show me everything that touches THESE specific price levels."
//   Whenever L3Book applies a state change (apply_new, apply_update,
//   apply_remove, plus the synth_* helpers), it calls trace_op(...). If
//   the (side, px) being mutated is in the watch set, trace_op writes
//   one line to the log: timestamp, coin, side, px, what op, plus a
//   snapshot of the real + synth state at that level after the op.
//   You enable it for a few specific prices when chasing a bug like
//   "why does this level look wrong vs the public WS feed".
//
// watch_emit : --watch-pxs / --watch-out
//   "At every block-boundary emit, show me state at THESE prices."
//   Fires once per emit (not per mutation), so it gives you a cleaner
//   per-block picture than trace_op.
//
// rule1_log  : --log-rule1 / --log-rule1-out
//   Logs every Rule 1 skip in reseed_synth_from_ws, classified as
//   would_lock / would_cross / would_inside_spread. Used to diagnose
//   why a WS-seeded synth level was thrown away.
//
// All three are silent when neither a watch list nor an output file is
// configured. The functions themselves cheaply early-out, but callers
// still build the format string (the `info` argument) unconditionally —
// that's tracked as fix E1 in overmind/studies/hl_feed/hl_publisher_perf_followups_20260611.md.

// State for the trace_op + watch_emit loggers.
//   pxs : the watch set; (side, px) pairs the operator wants logged.
//         Empty == nothing to log. Populated from --trace-pxs / --watch-pxs.
//   os  : the output stream. nullptr == feature off. Pointed at a file
//         opened via --trace-out / --watch-out, or stderr by default.
//   mu  : guards the writes so the WS thread and tail thread don't
//         interleave each other's lines.
struct TraceCfg {
  std::set<std::pair<std::string, std::string>> pxs;
  std::ostream* os = nullptr;
  std::mutex mu;
};
TraceCfg g_trace;  // for trace_op (per-mutation log)
TraceCfg g_watch;  // for watch_emit (per-emit log)
// Same idea, but rule1_log has no watch set — it logs every Rule 1 skip
// when the file is open.
struct Rule1Cfg {
  std::ostream* os = nullptr;
  std::mutex mu;
};
Rule1Cfg g_rule1;

// Forward declarations — the concrete bodies live further down, after
// L3Book is defined, because they reach into L3Book's nested types.
// L3RealLevel / L3SynthEntry are the per-(side,px) state buckets defined
// just below (see the "L3 data model" section).
class L3Book;
struct L3RealLevel;
struct L3SynthEntry;

// trace_op: called from inside every L3Book mutation. If logging is OFF
// (the production case), the function does ~nothing and returns; if ON
// for this (side, px), it writes one formatted line to the trace stream.
//
// Args:
//   coin / side / px : the level being mutated. side is "B" (bid) or "A" (ask).
//   op               : a short tag like "new", "remove_known", "update_unknown(add)"
//                       — describes which mutation just happened.
//   info             : a pre-built info string (e.g. "oid=123 sz=4.5") for
//                       human-readable context. See TRACE_OP below — call
//                       sites should NOT pre-build this string themselves.
//   real_lvl, synth  : pointers to the level's current state AFTER the op.
//                       Either can be nullptr (e.g. real_lvl == nullptr means
//                       no orders at this px right now).
//   t_block_ms       : which HL block we're processing (Unix ms).
void trace_op(const std::string& coin, const std::string& side, const std::string& px,
              const std::string& op, const std::string& info,
              const L3RealLevel* real_lvl,
              const L3SynthEntry* synth, int64_t t_block_ms);

// TRACE_OP — wrapper macro around trace_op() that short-circuits the
// fmt::format work when tracing is OFF.
//
// Why: calling fmt::format("oid={} sz={}", oid, sz) builds a std::string
// every time, even when trace_op would immediately return. In production
// the trace is always off, so that work is pure waste — call sites fire
// on every L3 mutation (~10s of thousands/sec at peak load). Pre-checking
// the same condition trace_op uses internally lets us skip the fmt cost.
//
// Usage (note: format-string and its fmt args go LAST):
//   TRACE_OP(coin, side, px, "new", &lvl, synth_e, t_block_ms,
//            "oid={} sz={}", oid, sz);
//
// Mechanics:
//   - `do { ... } while (0)` makes the macro behave like a single statement
//     so it composes correctly inside `if`/`else` without surprises.
//   - `##__VA_ARGS__` is a GNU-extension that drops the leading comma when
//     no fmt args are passed, so a literal-string-only call like
//     TRACE_OP(..., "no args") still expands cleanly.
#define TRACE_OP(coin_, side_, px_, op_, real_lvl_, synth_, t_, fmt_str, ...) \
  do {                                                                       \
    if (g_trace.os != nullptr &&                                             \
        g_trace.pxs.count({(side_), (px_)})) {                               \
      trace_op((coin_), (side_), (px_), (op_),                               \
               fmt::format(fmt_str, ##__VA_ARGS__),                          \
               (real_lvl_), (synth_), (t_));                                 \
    }                                                                        \
  } while (0)

// watch_emit: called once per coin per emit, AFTER the dirty-coins flush.
// If watch logging is OFF, returns immediately. Else, for each watched
// (side, px) it dumps real + synth + their sum at that level.
void watch_emit(const std::string& coin, const L3Book& book, int64_t t_block_ms);

// rule1_log: called from reseed_synth_from_ws whenever Rule 1 throws
// away a synth level. Logs once per skip when the file is open.
void rule1_log(const std::string& coin, const std::string& side, const std::string& px,
               double ws_sz, int32_t ws_n,
               std::optional<double> real_bid_top, std::optional<double> real_ask_top,
               const std::string& cls);

// ---- L3 data model ----------------------------------------------------------
// Per-(side, px) aggregate of the real ledger. Each `RealLevel` carries the
// per-oid sizes (needed so `apply_event_remove(oid)` knows what to subtract)
// AND running aggregates so `l2()` doesn't have to sum every emit. `px_d` is
// the price parsed once on level creation so the sort comparator doesn't keep
// calling `std::stod`.
struct L3RealLevel {
  double px_d = 0.0;       // px parsed once on level creation (sort key)
  double total_sz_d = 0.0; // running sum of orders[*]; maintained incrementally
  std::map<int64_t, double> orders;  // oid -> sz_d
};

// Synth (WS-seeded overlay) entry. Aggregated, not per-oid.
struct L3SynthEntry {
  double sz_d = 0.0;
  int32_t n = 0;
  double px_d = 0.0;  // px parsed once on first set
};

// ---- L3Book -----------------------------------------------------------------
// Per-coin L3 state.
//
//   real (`bids` / `asks`):    px -> RealLevel(orders, total_sz_d, px_d)
//      Per-order ledger from `new`/`update`/`remove` events in the diff stream.
//      Aggregate maintained incrementally; never re-summed at emit time.
//   synth (`bid_synth` / `ask_synth`): px -> SynthEntry(sz_d, n, px_d)
//      Aggregated overlay seeded from public WS l2Book snapshots, draining
//      via Rules 1+2 on each WS reseed and via _synth_estimate_remove /
//      _synth_extract_known when unknown-oid events touch a known synth px.
//
// Reconciliation invariants:
//   1. `real` is chain-truth for every oid we have seen since process start
//      and have not intentionally trimmed/dropped. It is the only state with
//      per-oid identity, so exact remove/update accounting is possible only
//      for orders present in `real`.
//   2. `synth` is only a bootstrap/resync overlay. It represents "WS says this
//      much L2 size exists here, but the diff stream has not yet identified
//      the individual oids to us." It must never be allowed to create a locked
//      or crossed emitted book against real chain state.
//   3. Public WS reseeds replace synth from scratch using `max(0, ws - real)`
//      at each level. This is what bounds accumulated error from unknown
//      removes: an unknown remove may subtract an average-sized synthetic
//      order, but the next WS snapshot re-estimates the remaining synthetic
//      size/count for that level.
//   4. Unknown `update` with `origSz` is more informative than unknown
//      `remove`: we can subtract the prior size exactly from synth before
//      adding the updated oid to real. Unknown `remove` has no size, so it
//      can only decrement synth by one average synthetic order.
//   5. When a new real order would lock/cross opposite-side synth, drop that
//      synth. When it would lock/cross opposite-side real, drop the opposite
//      real levels too. The latter is intentionally destructive: it prevents
//      publishing an invalid book after a midday start or missed earlier state;
//      later remove/update messages for those forgotten oids will simply take
//      the unknown path.
//   6. After catch-up, synth should be empty for that coin. From that point on,
//      unknown removes/updates are expected only from trimmed/dropped/missed
//      real state and should not fabricate new size.
//
// `emit_top_history` is a 60-deep deque of (event_time_ms, top5) snapshots
// so the catch-up check can compare WS at its `time` field against our
// state from that block, not whichever we happen to hold right now.
class L3Book {
 public:
  std::string coin;

  std::map<std::string, L3RealLevel> bids;
  std::map<std::string, L3RealLevel> asks;

  // Alias for the trace_op forward-decl's friend; keep the historical name
  // visible inside L3Book methods.
  using SynthEntry = L3SynthEntry;
  std::map<std::string, SynthEntry> bid_synth;
  std::map<std::string, SynthEntry> ask_synth;

  struct Level {
    std::string px;
    std::string sz;
    int32_t n;
    bool operator==(const Level& o) const {
      return px == o.px && std::stod(sz) == std::stod(o.sz) && n == o.n;
    }
  };

  // (event_time_ms, (bids_top5, asks_top5)) — newest at back.
  static constexpr size_t kEmitHistMax = 60;
  std::deque<std::pair<int64_t, std::pair<std::vector<Level>, std::vector<Level>>>>
      emit_top_history;

  explicit L3Book(std::string c = "") : coin(std::move(c)) {}

  // ---- synth lifecycle ----------------------------------------------------

  void reset_synth(int64_t t_block_ms = 0) {
    // Trace each existing synth that's about to be wiped.
    for (const auto* store : {&bid_synth, &ask_synth}) {
      std::string side = (store == &bid_synth) ? "B" : "A";
      for (const auto& [px, entry] : *store) {
        const auto& real_lvl = real_store(side).find(px);
        TRACE_OP(coin, side, px, "reset_synth",
                 real_lvl != real_store(side).end() ? &real_lvl->second : nullptr,
                 &entry, t_block_ms, "");
      }
    }
    bid_synth.clear();
    ask_synth.clear();
  }

  void seed_level(const std::string& side, const std::string& px,
                  const std::string& sz, int32_t n, int64_t t_block_ms = 0) {
    auto& target = (side == "B") ? bid_synth : ask_synth;
    auto& e = target[px];
    e.sz_d = std::stod(sz);
    e.n = n;
    if (e.px_d == 0.0) e.px_d = std::stod(px);
    auto rit = real_store(side).find(px);
    TRACE_OP(coin, side, px, "seed_synth",
             rit != real_store(side).end() ? &rit->second : nullptr,
             &e, t_block_ms,
             "sz={} n={}", sz, n);
  }

  void clear_synth(int64_t t_block_ms = 0) {
    for (const auto* store : {&bid_synth, &ask_synth}) {
      std::string side = (store == &bid_synth) ? "B" : "A";
      for (const auto& [px, entry] : *store) {
        const auto& real_lvl = real_store(side).find(px);
        TRACE_OP(coin, side, px, "clear_synth",
                 real_lvl != real_store(side).end() ? &real_lvl->second : nullptr,
                 &entry, t_block_ms,
                 "post-catchup wipe");
      }
    }
    bid_synth.clear();
    ask_synth.clear();
  }

  // ---- synth mutation helpers --------------------------------------------

  // Unknown-oid remove: subtract avg per-order from synth at px, decrement n.
  // No-op if no synth at px.
  void synth_estimate_remove(const std::string& side, const std::string& px,
                             int64_t t_block_ms = 0) {
    auto& synth = (side == "B") ? bid_synth : ask_synth;
    auto rit = real_store(side).find(px);
    auto* real_lvl = rit != real_store(side).end() ? &rit->second : nullptr;
    auto it = synth.find(px);
    if (it == synth.end()) {
      TRACE_OP(coin, side, px, "synth_rm_unknown(noop)",
               real_lvl, nullptr, t_block_ms,
               "no synth here");
      return;
    }
    double sz = it->second.sz_d;
    int32_t n = it->second.n;
    if (n <= 1) {
      synth.erase(it);
      TRACE_OP(coin, side, px, "synth_rm_unknown(drop)",
               real_lvl, nullptr, t_block_ms,
               "n<=1 -> drop level");
      return;
    }
    double new_sz = std::max(0.0, sz - sz / n);
    it->second.sz_d = new_sz;
    it->second.n = n - 1;
    TRACE_OP(coin, side, px, "synth_rm_unknown(dec)",
             real_lvl, &it->second, t_block_ms,
             "avg={:.4f} sub from sz", sz / n);
  }

  // Unknown-oid update: we know the order's prior size from origSz, so
  // remove exactly that from synth. No-op if no synth at px.
  void synth_extract_known(const std::string& side, const std::string& px,
                           const std::string& orig_sz_str, int64_t t_block_ms = 0) {
    auto& synth = (side == "B") ? bid_synth : ask_synth;
    auto rit = real_store(side).find(px);
    auto* real_lvl = rit != real_store(side).end() ? &rit->second : nullptr;
    auto it = synth.find(px);
    if (it == synth.end()) {
      TRACE_OP(coin, side, px, "synth_extract(noop)",
               real_lvl, nullptr, t_block_ms,
               "orig_sz={}", orig_sz_str);
      return;
    }
    double sz = it->second.sz_d;
    int32_t n = it->second.n;
    double new_sz = std::max(0.0, sz - std::stod(orig_sz_str));
    int32_t new_n = std::max(0, n - 1);
    if (new_n == 0 || new_sz <= 0.0) {
      synth.erase(it);
      TRACE_OP(coin, side, px, "synth_extract(drop)",
               real_lvl, nullptr, t_block_ms,
               "orig_sz={}", orig_sz_str);
      return;
    }
    it->second.sz_d = new_sz;
    it->second.n = new_n;
    TRACE_OP(coin, side, px, "synth_extract(dec)",
             real_lvl, &it->second, t_block_ms,
             "orig_sz={}", orig_sz_str);
  }

  // Real order added at (real_side, real_px) invalidates any synth on the
  // opposite side that would cross/lock with it.
  int32_t drop_inverted_synth(const std::string& real_side, const std::string& real_px,
                              int64_t t_block_ms = 0) {
    auto& opp = (real_side == "B") ? ask_synth : bid_synth;
    std::string opp_side = (real_side == "B") ? "A" : "B";
    double real_px_f = std::stod(real_px);
    std::vector<std::string> to_drop;
    if (real_side == "B") {
      for (const auto& [px, _] : opp) {
        if (std::stod(px) <= real_px_f) to_drop.push_back(px);
      }
    } else {
      for (const auto& [px, _] : opp) {
        if (std::stod(px) >= real_px_f) to_drop.push_back(px);
      }
    }
    for (const auto& px : to_drop) {
      auto rit = real_store(opp_side).find(px);
      const auto& entry = opp[px];
      TRACE_OP(coin, opp_side, px, "drop_inv_synth",
               rit != real_store(opp_side).end() ? &rit->second : nullptr,
               &entry, t_block_ms,
               "by real {}@{}", real_side, real_px);
      opp.erase(px);
    }
    return static_cast<int32_t>(to_drop.size());
  }

  // Real order added at (real_side, real_px) invalidates any REAL on the
  // opposite side at a crossing or locked price. The newer message wins:
  // a real bid at 10 means the chain "knows" any resting ask at <=10 was
  // filled/canceled. We may not have seen the corresponding remove event
  // yet (it may arrive in a later block, or we may have missed it), but
  // emitting a crossed book is never correct — HL's matching engine
  // wouldn't let one persist.
  //
  // Consequence: subsequent chain remove events for the dropped oids will
  // miss in real.find(oid) and fall into the unknown-remove path. That's
  // noisy but handled by synth_estimate_remove. Net: we trade a clean
  // book here for some unknown-remove warnings later — an acceptable
  // tradeoff since the alternative is publishing crossed L2.
  int32_t drop_inverted_real(const std::string& real_side, const std::string& real_px,
                             int64_t t_block_ms = 0) {
    auto& opp = real_store((real_side == "B") ? "A" : "B");
    std::string opp_side = (real_side == "B") ? "A" : "B";
    double real_px_f = std::stod(real_px);
    std::vector<std::string> to_drop;
    if (real_side == "B") {
      for (const auto& [px, lvl] : opp) {
        if (lvl.px_d <= real_px_f) to_drop.push_back(px);
      }
    } else {
      for (const auto& [px, lvl] : opp) {
        if (lvl.px_d >= real_px_f) to_drop.push_back(px);
      }
    }
    for (const auto& px : to_drop) {
      auto it = opp.find(px);
      if (it == opp.end()) continue;
      auto* synth_e = _synth_at(opp_side, px);
      TRACE_OP(coin, opp_side, px, "drop_inv_real",
               &it->second, synth_e, t_block_ms,
               "by real {}@{} (dropped {} oids)",
               real_side, real_px, it->second.orders.size());
      opp.erase(it);
    }
    return static_cast<int32_t>(to_drop.size());
  }

  // ---- apply --------------------------------------------------------------

  enum class DiffKind { kRemove, kNew, kUpdate, kUnknown };

  void apply_new(const std::string& side, const std::string& px,
                 int64_t oid, const std::string& sz, int64_t t_block_ms = 0) {
    double sz_d = std::stod(sz);
    auto& lvl = real_store(side)[px];
    if (lvl.px_d == 0.0) lvl.px_d = std::stod(px);
    // try_emplace: leaves an existing oid alone so we can read prior_sz.
    // Duplicate `new` for an oid already on the book at this px is a
    // protocol oddity, but if it ever happens we must keep total_sz_d
    // consistent (CORR-DUP — see overmind/studies/hl_feed/hl_publisher_perf_followups_20260611.md).
    auto [oit, inserted] = lvl.orders.try_emplace(oid, sz_d);
    if (inserted) {
      lvl.total_sz_d += sz_d;
    } else {
      lvl.total_sz_d += (sz_d - oit->second);
      oit->second = sz_d;
    }
    auto* synth_e = _synth_at(side, px);
    TRACE_OP(coin, side, px, "new",
             &lvl, synth_e, t_block_ms,
             "oid={} sz={}", oid, sz);
    drop_inverted_synth(side, px, t_block_ms);
    drop_inverted_real(side, px, t_block_ms);
  }

  // Returns true if applied as known, false if unknown-oid path taken.
  // E3: structure these to do exactly one orders.find(oid) instead of the
  // old count(oid)-then-find(oid) pattern. The "known" path returns early
  // so we don't need a singular default-constructed iterator hanging
  // around for the unknown path.
  void apply_update(const std::string& side, const std::string& px,
                    int64_t oid, const std::string& new_sz,
                    const std::string& orig_sz, int64_t t_block_ms = 0) {
    auto& real = real_store(side);
    auto it = real.find(px);
    auto* synth_e = _synth_at(side, px);
    double new_sz_d = std::stod(new_sz);

    // KNOWN-oid path: the (px, oid) pair currently has a real order.
    if (it != real.end()) {
      auto& lvl = it->second;
      auto oit = lvl.orders.find(oid);
      if (oit != lvl.orders.end()) {
        double prior_sz_d = oit->second;
        if (new_sz_d == 0.0) {
          lvl.total_sz_d -= prior_sz_d;
          lvl.orders.erase(oit);
          bool removed_level = lvl.orders.empty();
          if (removed_level) real.erase(it);
          // After removal, fetch updated state for the trace.
          auto rit = real.find(px);
          TRACE_OP(coin, side, px, "update_known(rm)",
                   rit != real.end() ? &rit->second : nullptr,
                   (synth_e = _synth_at(side, px)), t_block_ms,
                   "oid={} newSz=0", oid);
        } else {
          lvl.total_sz_d += (new_sz_d - prior_sz_d);
          oit->second = new_sz_d;
          TRACE_OP(coin, side, px, "update_known",
                   &lvl, synth_e, t_block_ms,
                   "oid={} newSz={}", oid, new_sz);
          drop_inverted_synth(side, px, t_block_ms);
        }
        return;
      }
    }

    // UNKNOWN-oid path: either no level at px, or px exists but oid isn't.
    // Trace the pre-state before we touch synth.
    auto* real_lvl = (it != real.end()) ? &it->second : nullptr;
    TRACE_OP(coin, side, px, "update_unknown(pre)",
             real_lvl, synth_e, t_block_ms,
             "oid={} origSz={} newSz={}", oid, orig_sz, new_sz);
    synth_extract_known(side, px, orig_sz, t_block_ms);
    if (new_sz_d > 0.0) {
      auto& rlvl = real[px];
      if (rlvl.px_d == 0.0) rlvl.px_d = std::stod(px);
      // try_emplace: same CORR-DUP guard as apply_new.
      auto [oit, inserted] = rlvl.orders.try_emplace(oid, new_sz_d);
      if (inserted) {
        rlvl.total_sz_d += new_sz_d;
      } else {
        rlvl.total_sz_d += (new_sz_d - oit->second);
        oit->second = new_sz_d;
      }
      synth_e = _synth_at(side, px);
      TRACE_OP(coin, side, px, "update_unknown(add)",
               &rlvl, synth_e, t_block_ms,
               "oid={} newSz={}", oid, new_sz);
      drop_inverted_synth(side, px, t_block_ms);
      drop_inverted_real(side, px, t_block_ms);
    }
  }

  void apply_remove(const std::string& side, const std::string& px,
                    int64_t oid, int64_t t_block_ms = 0) {
    auto& real = real_store(side);
    auto it = real.find(px);
    auto* synth_e = _synth_at(side, px);

    // KNOWN-oid path: one find on the px map, one find on the orders map.
    if (it != real.end()) {
      auto& lvl = it->second;
      auto oit = lvl.orders.find(oid);
      if (oit != lvl.orders.end()) {
        lvl.total_sz_d -= oit->second;
        lvl.orders.erase(oit);
        bool removed_level = lvl.orders.empty();
        if (removed_level) real.erase(it);
        auto rit = real.find(px);
        TRACE_OP(coin, side, px, "remove_known",
                 rit != real.end() ? &rit->second : nullptr,
                 synth_e, t_block_ms,
                 "oid={}", oid);
        return;
      }
    }

    // UNKNOWN-oid path.
    auto* real_lvl = (it != real.end()) ? &it->second : nullptr;
    TRACE_OP(coin, side, px, "remove_unknown(pre)",
             real_lvl, synth_e, t_block_ms,
             "oid={}", oid);
    synth_estimate_remove(side, px, t_block_ms);
  }

 private:
  // Helper for trace sites — peek at synth at (side, px); nullptr if absent.
  const SynthEntry* _synth_at(const std::string& side, const std::string& px) const {
    const auto& s = (side == "B") ? bid_synth : ask_synth;
    auto it = s.find(px);
    return it == s.end() ? nullptr : &it->second;
  }
  SynthEntry* _synth_at(const std::string& side, const std::string& px) {
    auto& s = (side == "B") ? bid_synth : ask_synth;
    auto it = s.find(px);
    return it == s.end() ? nullptr : &it->second;
  }
 public:

  // ---- aggregation -------------------------------------------------------

  // Aggregate real + synth, sorted by price (bids descending, asks ascending).
  // Walks each side's map once; no per-order summation (running total_sz_d on
  // each level keeps the L2 always-up-to-date). Sort uses cached px_d.
  std::vector<Level> l2(const std::string& side) const {
    const auto& real = (side == "B") ? bids : asks;
    const auto& synth = (side == "B") ? bid_synth : ask_synth;
    // (px_d, px_str, total_sz, total_n) — sortable by price without stod.
    struct Item { double px_d; const std::string* px; double sz; int32_t n; };
    std::vector<Item> items;
    items.reserve(real.size() + synth.size());
    for (const auto& [px, lvl] : real) {
      double sz = lvl.total_sz_d;
      int32_t n = static_cast<int32_t>(lvl.orders.size());
      if (auto sit = synth.find(px); sit != synth.end()) {
        sz += sit->second.sz_d;
        n += sit->second.n;
      }
      if (sz > 0.0 && n > 0) items.push_back({lvl.px_d, &px, sz, n});
    }
    // synth-only levels (no real at this px).
    for (const auto& [px, s] : synth) {
      if (real.find(px) != real.end()) continue;
      if (s.sz_d > 0.0 && s.n > 0) items.push_back({s.px_d, &px, s.sz_d, s.n});
    }
    if (side == "B") {
      std::sort(items.begin(), items.end(),
                [](const Item& a, const Item& b) { return a.px_d > b.px_d; });
    } else {
      std::sort(items.begin(), items.end(),
                [](const Item& a, const Item& b) { return a.px_d < b.px_d; });
    }
    std::vector<Level> out;
    out.reserve(items.size());
    for (auto& it : items) {
      out.push_back({*it.px, format_qty(it.sz), it.n});
    }
    return out;
  }

  // Trim the L3 real book to at most `cap` levels per side, dropping the
  // worst-priced (deepest, furthest from the inside). Per-oid state for
  // dropped levels is forgotten; subsequent update/remove events for those
  // oids fall through to the unknown-oid path in apply_update / apply_remove
  // (which is a no-op post-catch-up). Returns total levels dropped across
  // both sides.
  //
  // Memory motivation: long-lived deep levels accumulate oids without bound
  // if HL never sends a `remove` for them (rare, but possible for orders
  // that never close). Capping at, say, 100 levels per side leaves plenty
  // of headroom over the wire emit cap (--max-levels 40) while bounding
  // RSS deterministically.
  //
  // Ranks by px_d (parsed double); the map keys are strings, so iterating
  // the map in lex order does not always match numeric order — sort
  // explicitly. O(N log N) per call but called once per block per sym.
  int32_t trim_to(int32_t cap) {
    if (cap <= 0) return 0;
    auto trim_side = [&](std::map<std::string, L3RealLevel>& side,
                         bool higher_is_better) -> int32_t {
      if (static_cast<int32_t>(side.size()) <= cap) return 0;
      std::vector<std::pair<double, const std::string*>> ranked;
      ranked.reserve(side.size());
      for (const auto& [px, lvl] : side) ranked.emplace_back(lvl.px_d, &px);
      if (higher_is_better) {
        std::sort(ranked.begin(), ranked.end(),
                  [](const auto& a, const auto& b) { return a.first > b.first; });
      } else {
        std::sort(ranked.begin(), ranked.end(),
                  [](const auto& a, const auto& b) { return a.first < b.first; });
      }
      int32_t dropped = 0;
      for (size_t i = static_cast<size_t>(cap); i < ranked.size(); ++i) {
        side.erase(*ranked[i].second);
        ++dropped;
      }
      return dropped;
    };
    return trim_side(bids, /*higher_is_better=*/true) +
           trim_side(asks, /*higher_is_better=*/false);
  }

  // Real-only L2 (no synth contribution). Used for catch-up checks.
  std::vector<Level> real_l2(const std::string& side) const {
    const auto& real = (side == "B") ? bids : asks;
    struct Item { double px_d; const std::string* px; double sz; int32_t n; };
    std::vector<Item> items;
    items.reserve(real.size());
    for (const auto& [px, lvl] : real) {
      if (lvl.total_sz_d > 0.0 && !lvl.orders.empty()) {
        items.push_back({lvl.px_d, &px, lvl.total_sz_d,
                         static_cast<int32_t>(lvl.orders.size())});
      }
    }
    if (side == "B") {
      std::sort(items.begin(), items.end(),
                [](const Item& a, const Item& b) { return a.px_d > b.px_d; });
    } else {
      std::sort(items.begin(), items.end(),
                [](const Item& a, const Item& b) { return a.px_d < b.px_d; });
    }
    std::vector<Level> out;
    out.reserve(items.size());
    for (auto& it : items) {
      out.push_back({*it.px, format_qty(it.sz), it.n});
    }
    return out;
  }

  // ---- catch-up history --------------------------------------------------

  void record_emit_top(int32_t n_levels, int64_t event_time_ms) {
    std::vector<Level> bid_top = real_l2("B");
    std::vector<Level> ask_top = real_l2("A");
    if ((int32_t)bid_top.size() > n_levels) bid_top.resize(n_levels);
    if ((int32_t)ask_top.size() > n_levels) ask_top.resize(n_levels);
    emit_top_history.emplace_back(
        event_time_ms, std::make_pair(std::move(bid_top), std::move(ask_top)));
    while (emit_top_history.size() > kEmitHistMax) emit_top_history.pop_front();
  }

  std::optional<std::pair<std::vector<Level>, std::vector<Level>>>
  get_top_at_or_before(int64_t target_time_ms) const {
    std::optional<std::pair<std::vector<Level>, std::vector<Level>>> best;
    for (const auto& [t, snap] : emit_top_history) {
      if (t <= target_time_ms) best = snap;
      else break;
    }
    return best;
  }

 private:
  std::map<std::string, L3RealLevel>&
  real_store(const std::string& side) {
    return (side == "B") ? bids : asks;
  }
  const std::map<std::string, L3RealLevel>&
  real_store(const std::string& side) const {
    return (side == "B") ? bids : asks;
  }
};

// ---- Debug-logger writers (defined here so they can reference L3Book) ------

// Implementation of trace_op (declared way above near the TraceCfg structs).
// Two-step early-out:
//   1. g_trace.os == nullptr  : --trace-out not configured at all → nothing to do.
//   2. (side, px) not in pxs  : not on the watch list → nothing to log.
// In production both checks short-circuit immediately. We then build a
// human-readable line and write it under g_trace.mu (because two L3Book
// mutations from different threads — tail thread and WS thread — can race).
void trace_op(const std::string& coin, const std::string& side, const std::string& px,
              const std::string& op, const std::string& info,
              const L3RealLevel* real_lvl,
              const L3SynthEntry* synth, int64_t t_block_ms) {
  if (g_trace.os == nullptr || !g_trace.pxs.count({side, px})) return;
  std::string r_str;
  if (real_lvl && !real_lvl->orders.empty()) {
    std::string oids;
    bool first = true;
    for (const auto& [oid, _] : real_lvl->orders) {
      if (!first) oids += ", ";
      oids += std::to_string(oid);
      first = false;
    }
    r_str = fmt::format("n={} sz={:.4f} oids=[{}]",
                        real_lvl->orders.size(), real_lvl->total_sz_d, oids);
  } else {
    r_str = "n=0 sz=0";
  }
  std::string s_str = synth
      ? fmt::format("sz={:.4f} n={}", synth->sz_d, synth->n)
      : "(none)";
  std::string line = fmt::format(
      "t_wall={} t_block={} coin={} {} px={} OP={:18s} {:40s} | real[{}] synth[{}]\n",
      now_ms(), t_block_ms, coin, side, px, op, info, r_str, s_str);
  std::lock_guard<std::mutex> g(g_trace.mu);
  *g_trace.os << line;
  g_trace.os->flush();
}

void watch_emit(const std::string& coin, const L3Book& book, int64_t t_block_ms) {
  if (g_watch.os == nullptr || g_watch.pxs.empty()) return;
  int64_t t_wall = now_ms();
  std::lock_guard<std::mutex> g(g_watch.mu);
  for (const auto& [side, px] : g_watch.pxs) {
    const auto& real = (side == "B") ? book.bids : book.asks;
    const auto& synth = (side == "B") ? book.bid_synth : book.ask_synth;
    int32_t r_n = 0;
    double r_sz = 0.0;
    if (auto it = real.find(px); it != real.end()) {
      r_n = static_cast<int32_t>(it->second.orders.size());
      r_sz = it->second.total_sz_d;
    }
    double s_sz = 0.0;
    int32_t s_n = 0;
    if (auto it = synth.find(px); it != synth.end()) {
      s_sz = it->second.sz_d;
      s_n = it->second.n;
    }
    *g_watch.os << fmt::format(
        "[WATCH] t_wall={} t_block={} coin={} {} px={} "
        "real(n={},sz={:.4f}) synth(n={},sz={:.4f}) emit(n={},sz={:.4f})\n",
        t_wall, t_block_ms, coin, side, px,
        r_n, r_sz, s_n, s_sz, r_n + s_n, r_sz + s_sz);
  }
  g_watch.os->flush();
}

void rule1_log(const std::string& coin, const std::string& side, const std::string& px,
               double ws_sz, int32_t ws_n,
               std::optional<double> real_bid_top, std::optional<double> real_ask_top,
               const std::string& cls) {
  if (g_rule1.os == nullptr) return;
  std::lock_guard<std::mutex> g(g_rule1.mu);
  *g_rule1.os << fmt::format(
      "[RULE1] coin={} side={} px={} ws_sz={} ws_n={} "
      "real_bid_top={} real_ask_top={} class={}\n",
      coin, side, px, ws_sz, ws_n,
      real_bid_top ? fmt::format("{}", *real_bid_top) : std::string("none"),
      real_ask_top ? fmt::format("{}", *real_ask_top) : std::string("none"),
      cls);
  g_rule1.os->flush();
}

// ---- BookSet ----------------------------------------------------------------
// Per-coin L3Book registry plus per-block dirty tracking and the shared mutex.
// All field accesses go through the methods below so the WS thread and the
// main tail thread don't trample each other.
class BookSet {
 public:
  std::mutex mu;
  std::unordered_map<std::string, std::unique_ptr<L3Book>> books;
  std::unordered_set<std::string> dirty_this_block;
  int64_t current_block = -1;
  int64_t latest_block_time_ms = 0;
  int64_t events_seen = 0;
  // L3 cap: trim each L3Book's bids/asks to at most this many levels per side
  // at emit time. 0 or negative = no trim. See L3Book::trim_to for rationale.
  int32_t l3_cap = 0;
  // Running tally of levels dropped by the cap (for periodic logging).
  int64_t l3_levels_trimmed = 0;
  // Restart resilience (see overmind/studies/hl_feed/publisher_dedup_design_20260625.md).
  // L1a: drop replay packets whose block_no <= highest_block_seen. Ratcheted
  // lazily in advance_block from the OUTGOING current_block, so during a
  // multi-packet block N the dedup check sees the prior transition's value
  // (N-1 at the first packet of N; remains N-1 for subsequent N-packets).
  int64_t highest_block_seen = -1;
  int64_t duplicates_dropped = 0;
  // L1b: forward gap counter and callback fired into ContinuousWS::force_recatch_up.
  // Small skip-ahead gaps are NORMAL on HL Mainnet — the streaming file omits
  // packets for blocks with no book-diff events across any coin (observed
  // ~5-6% on live data). The recovery callback only fires when gap >=
  // kGapTriggerThreshold, where a real catch-up replay would span thousands
  // of blocks. small_gaps_observed counts the empty-block skips silently
  // for visibility.
  int64_t gaps_observed = 0;
  int64_t small_gaps_observed = 0;
  std::function<void()> gap_callback;

  L3Book* get_unlocked(const std::string& coin) {
    auto it = books.find(coin);
    if (it == books.end()) {
      auto [ins, _] = books.emplace(coin, std::make_unique<L3Book>(coin));
      return ins->second.get();
    }
    return it->second.get();
  }

  // Apply one event from the diff stream. *_unlocked variants: caller must
  // hold `mu`. handle_line batches all events from one packet under a single
  // lock acquisition (LOCK-PACKET in overmind/studies/hl_feed/hl_publisher_perf_followups_20260611.md).
  void apply_event_new_unlocked(const std::string& coin, const std::string& side,
                                const std::string& px, int64_t oid, const std::string& sz) {
    get_unlocked(coin)->apply_new(side, px, oid, sz, latest_block_time_ms);
    dirty_this_block.insert(coin);
    ++events_seen;
  }
  void apply_event_update_unlocked(const std::string& coin, const std::string& side,
                                   const std::string& px, int64_t oid,
                                   const std::string& new_sz, const std::string& orig_sz) {
    get_unlocked(coin)->apply_update(side, px, oid, new_sz, orig_sz, latest_block_time_ms);
    dirty_this_block.insert(coin);
    ++events_seen;
  }
  void apply_event_remove_unlocked(const std::string& coin, const std::string& side,
                                   const std::string& px, int64_t oid) {
    get_unlocked(coin)->apply_remove(side, px, oid, latest_block_time_ms);
    dirty_this_block.insert(coin);
    ++events_seen;
  }

  std::vector<std::string> take_dirty() {
    std::lock_guard<std::mutex> g(mu);
    std::vector<std::string> out(dirty_this_block.begin(), dirty_this_block.end());
    dirty_this_block.clear();
    return out;
  }

  // For emit: copy out the publishable L2 atomically. Also records the
  // post-emit real top-5 for the catch-up history.
  struct EmitSnap {
    std::vector<L3Book::Level> bids, asks;
    int64_t event_time_ms = 0;
  };
  std::optional<EmitSnap> snapshot_l2_for_emit(const std::string& coin) {
    std::lock_guard<std::mutex> g(mu);
    auto it = books.find(coin);
    if (it == books.end()) return std::nullopt;
    EmitSnap snap;
    snap.bids = it->second->l2("B");
    snap.asks = it->second->l2("A");
    snap.event_time_ms = latest_block_time_ms;
    it->second->record_emit_top(/*n_levels=*/5, latest_block_time_ms);
    watch_emit(coin, *it->second, latest_block_time_ms);
    // Trim deep L3 levels after the snapshot is built, so the wire emit is
    // never short-changed by the trim. Once-per-block-per-sym cost.
    if (l3_cap > 0) l3_levels_trimmed += it->second->trim_to(l3_cap);
    return snap;
  }

  // For the ContinuousWS catch-up check: our real-only top-N as of the
  // chain time WS labels its msg with. nullopt if we have no snapshot for
  // that time yet.
  std::optional<std::pair<std::vector<L3Book::Level>, std::vector<L3Book::Level>>>
  snapshot_real_l2_top_at(const std::string& coin, int32_t n_levels, int64_t target_time_ms) {
    std::lock_guard<std::mutex> g(mu);
    auto it = books.find(coin);
    if (it == books.end()) return std::nullopt;
    auto snap = it->second->get_top_at_or_before(target_time_ms);
    if (!snap) return std::nullopt;
    if ((int32_t)snap->first.size() > n_levels) snap->first.resize(n_levels);
    if ((int32_t)snap->second.size() > n_levels) snap->second.resize(n_levels);
    return snap;
  }

  // Rules 1+2 reseed from a fresh WS l2Book msg.
  struct WsLevel {
    std::string px;
    std::string sz;
    int32_t n;
  };

  bool reseed_synth_from_ws(const std::string& coin,
                            const std::vector<WsLevel>& bids,
                            const std::vector<WsLevel>& asks,
                            std::string* bad_top_detail = nullptr) {
    std::lock_guard<std::mutex> g(mu);
    auto* book = get_unlocked(coin);

    // Snapshot pre-reset "known" pxs and real top-of-book for Rule 1.
    //
    // "Known" means "we have chain-truth evidence this px exists." Prior
    // synth does NOT count — synth is itself derived from WS, so including
    // it would let a previously-stale synth re-validate itself on every
    // reseed even if it's been wrong all along (see hl_publisher
    // followups doc for the PNUT case). Real-only known_pxs means Rule 1
    // re-asks "should this WS level exist?" each reseed.
    std::unordered_set<std::string> known_bid_pxs;
    for (const auto& [px, _] : book->bids) known_bid_pxs.insert(px);
    std::unordered_set<std::string> known_ask_pxs;
    for (const auto& [px, _] : book->asks) known_ask_pxs.insert(px);

    std::optional<double> real_bid_top;
    const std::string* real_bid_top_px = nullptr;
    for (const auto& [px, lvl] : book->bids) {
      if (!real_bid_top || lvl.px_d > *real_bid_top) {
        real_bid_top = lvl.px_d;
        real_bid_top_px = &px;
      }
    }
    std::optional<double> real_ask_top;
    const std::string* real_ask_top_px = nullptr;
    for (const auto& [px, lvl] : book->asks) {
      if (!real_ask_top || lvl.px_d < *real_ask_top) {
        real_ask_top = lvl.px_d;
        real_ask_top_px = &px;
      }
    }

    bool bad_top_price = false;
    if (bad_top_detail) {
      try {
        bool bad_bid = real_bid_top && !bids.empty() &&
                       *real_bid_top > std::stod(bids.front().px);
        bool bad_ask = real_ask_top && !asks.empty() &&
                       *real_ask_top < std::stod(asks.front().px);
        if (bad_bid || bad_ask) {
          bad_top_price = true;
          *bad_top_detail = fmt::format(
              "bad_bid={} real_bid={} ws_bid={} bad_ask={} real_ask={} ws_ask={}",
              bad_bid ? "yes" : "no",
              real_bid_top_px ? *real_bid_top_px : std::string("none"),
              bids.empty() ? "none" : bids.front().px,
              bad_ask ? "yes" : "no",
              real_ask_top_px ? *real_ask_top_px : std::string("none"),
              asks.empty() ? "none" : asks.front().px);
        } else {
          bad_top_detail->clear();
        }
      } catch (const std::exception&) {
        bad_top_detail->clear();
      }
    }

    int64_t t = latest_block_time_ms;
    book->reset_synth(t);

    auto handle_side = [&](const std::string& side,
                           const std::vector<WsLevel>& lvls,
                           const std::unordered_set<std::string>& known_pxs,
                           const auto& real_store) {
      for (const auto& lvl : lvls) {
        double ws_sz = std::stod(lvl.sz);
        int32_t ws_n = lvl.n;
        double real_sz = 0.0;
        int32_t real_n = 0;
        if (auto rit = real_store.find(lvl.px); rit != real_store.end()) {
          real_sz = rit->second.total_sz_d;
          real_n = static_cast<int32_t>(rit->second.orders.size());
        }
        // Rule 1: skip new aggressive levels at continuous resync.
        //
        // Two reasons to skip an unknown WS level:
        //  (a) "more aggressive than own real top" — diff stream should be
        //      ahead of WS, so a genuinely-new aggressive level would have
        //      reached us as a real `new` event first. WS proposing one
        //      means WS is stale, OR the level's about to arrive — either
        //      way, fabricating it from WS just creates an inconsistent
        //      book until reconciliation.
        //  (b) "would cross or lock the OPPOSITE real top" — seeding this
        //      synth would publish an invalid (crossed/locked) book.
        //      HL's actual book is essentially never crossed; if it were
        //      we'd see the matching/fill events in the diff stream.
        //      So skipping is strictly correct.
        //
        // (a) was the historical check. (b) was missing — discovered when
        // PNUT (and a few other low-tick syms) showed 100% crossed books
        // on the wire while HL's public WS showed 0% — see comparison in
        // overmind/studies/hl_feed/hl_publisher_perf_followups_20260611.md.
        bool is_known = known_pxs.count(lvl.px) > 0;
        if (!is_known) {
          double pxd = std::stod(lvl.px);
          bool skip = false;
          std::optional<double> opp_top;
          if (side == "B") {
            if (real_bid_top && pxd > *real_bid_top) {
              skip = true; opp_top = real_ask_top;
            }
            if (real_ask_top && pxd >= *real_ask_top) {
              skip = true; opp_top = real_ask_top;
            }
          } else { // side == "A"
            if (real_ask_top && pxd < *real_ask_top) {
              skip = true; opp_top = real_bid_top;
            }
            if (real_bid_top && pxd <= *real_bid_top) {
              skip = true; opp_top = real_bid_top;
            }
          }
          if (skip) {
            std::string cls;
            if (!opp_top) cls = "no_opposite";
            else if (pxd == *opp_top) cls = "would_lock";
            else if ((side == "B" && pxd > *opp_top) ||
                     (side == "A" && pxd < *opp_top)) cls = "would_cross";
            else cls = "would_inside_spread";
            rule1_log(coin, side, lvl.px, ws_sz, ws_n, real_bid_top, real_ask_top, cls);
            continue;
          }
        }
        // Rule 2 / bootstrap: synth = max(0, ws - real).
        double synth_sz = std::max(0.0, ws_sz - real_sz);
        int32_t synth_n = std::max(0, ws_n - real_n);
        if (synth_sz > 0.0 && synth_n > 0) {
          book->seed_level(side, lvl.px, format_qty(synth_sz), synth_n, t);
        }
      }
    };
    handle_side("B", bids, known_bid_pxs, book->bids);
    handle_side("A", asks, known_ask_pxs, book->asks);

    dirty_this_block.insert(coin);
    return bad_top_price;
  }

  void clear_synth_for(const std::string& coin) {
    std::lock_guard<std::mutex> g(mu);
    auto it = books.find(coin);
    if (it == books.end()) return;
    it->second->clear_synth(latest_block_time_ms);
    dirty_this_block.insert(coin);
  }

  // For block-boundary advance from the tail thread.
  //
  // Lazy ratchet for L1a dedup: when we transition off block N (i.e., this
  // call is replacing current_block=N with a different new_block_no), the
  // OUTGOING current_block "graduates" into highest_block_seen. So during
  // multi-packet block N, highest_block_seen stays at N-1 (the value set
  // when we transitioned out of N-1 into N), and the <= check correctly
  // passes block N's packets. See "Why <= works given lazy update" in
  // overmind/studies/hl_feed/publisher_dedup_design_20260625.md.
  void advance_block(int64_t block_no, int64_t new_block_time_ms) {
    std::lock_guard<std::mutex> g(mu);
    if (new_block_time_ms > 0) latest_block_time_ms = new_block_time_ms;
    if (current_block > 0 && current_block > highest_block_seen) {
      highest_block_seen = current_block;
    }
    current_block = block_no;
  }
  bool block_changed(int64_t block_no) {
    std::lock_guard<std::mutex> g(mu);
    return block_no != current_block;
  }
};

// HIP-3 builder dexes to seed during bootstrap. Mirrors Python's
// HIP3_DEXES_TO_SEED ("xyz",) — extend when we trade more builders.
const std::array<const char*, 1> kHIP3DexesToSeed = {"xyz"};

// ---- discover_bootstrap_syms ------------------------------------------------
// Hit /info to fetch the sym universe. Returns: native perps from
// {type:"meta"} (no prefix on the names) + each HIP-3 dex from
// {type:"meta", dex:"<name>"} (names already arrive prefixed, e.g.
// "xyz:XYZ100"). Delisted syms and blacklisted syms are dropped.
//
// Mirrors Python's discover_bootstrap_syms. We deliberately do NOT walk
// /info {type:"perpDexs"} dynamically — the publisher should only seed syms
// for builders we actually want, controlled by kHIP3DexesToSeed above.
std::unordered_set<std::string> discover_bootstrap_syms(
    const std::string& info_url, const std::unordered_set<std::string>& blacklist) {
  std::unordered_set<std::string> out;
  // /info can blip. Retry each request a few times with explicit logging
  // before giving up — without it a single transient failure starts the
  // publisher with an empty sym set, the WS sub set is 0, and we tail
  // EOF with no meaningful state.
  auto post = [&](const std::string& body,
                  const std::string& label) -> std::optional<rapidjson::Document> {
    constexpr int kAttempts = 3;
    for (int attempt = 1; attempt <= kAttempts; ++attempt) {
      cpr::Response r = cpr::Post(cpr::Url{info_url},
                                  cpr::Header{{"Content-Type", "application/json"}},
                                  cpr::Body{body},
                                  cpr::Timeout{10000});
      if (r.error.code != cpr::ErrorCode::OK) {
        fmt::print(stderr, "HL /info {} attempt {}/{} failed: {}\n",
                   label, attempt, kAttempts, r.error.message);
      } else if (r.status_code != 200) {
        fmt::print(stderr, "HL /info {} attempt {}/{} returned HTTP {}\n",
                   label, attempt, kAttempts, r.status_code);
      } else {
        rapidjson::Document d;
        d.Parse(r.text.c_str(), r.text.size());
        if (!d.HasParseError()) return d;
        fmt::print(stderr, "HL /info {} attempt {}/{} parse error: {}\n",
                   label, attempt, kAttempts,
                   rapidjson::GetParseError_En(d.GetParseError()));
      }
      if (attempt < kAttempts) std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    return std::nullopt;
  };
  auto is_delisted = [](const rapidjson::Value& u) {
    auto it = u.FindMember("isDelisted");
    return it != u.MemberEnd() && it->value.IsBool() && it->value.GetBool();
  };

  // Native perps via /info { type: "meta" }. Names are unprefixed
  // ("BTC", "ETH", ...). Hard-fail if this comes back empty — without the
  // native universe we have nothing meaningful to bootstrap against.
  auto native = post(R"({"type":"meta"})", "native meta");
  if (!native) {
    throw std::runtime_error("HL bootstrap failed: unable to fetch native /info meta");
  }
  if (native->IsObject() && native->HasMember("universe") && (*native)["universe"].IsArray()) {
    for (const auto& u : (*native)["universe"].GetArray()) {
      if (!u.IsObject() || !u.HasMember("name") || !u["name"].IsString()) continue;
      if (is_delisted(u)) continue;
      std::string name = u["name"].GetString();
      if (!blacklist.count(name)) out.insert(name);
    }
  }

  // HIP-3 dexes: names come back ALREADY PREFIXED (e.g. "xyz:XYZ100"), so we
  // insert them verbatim — do NOT prepend the dex name again. Soft-fail
  // these: native is enough to operate; a missing dex just means we
  // bootstrap that builder's syms via incremental diffs only.
  for (const char* dex : kHIP3DexesToSeed) {
    std::string body = fmt::format(R"({{"type":"meta","dex":"{}"}})", dex);
    auto d = post(body, fmt::format("{} meta", dex));
    if (!d) {
      fmt::print(stderr, "HL bootstrap warning: skipping dex {} after failed /info retries\n", dex);
      continue;
    }
    if (!d->IsObject() || !d->HasMember("universe") || !(*d)["universe"].IsArray()) {
      fmt::print(stderr, "HL bootstrap warning: skipping dex {} due to malformed /info response\n", dex);
      continue;
    }
    for (const auto& u : (*d)["universe"].GetArray()) {
      if (!u.IsObject() || !u.HasMember("name") || !u["name"].IsString()) continue;
      if (is_delisted(u)) continue;
      std::string name = u["name"].GetString();
      if (!blacklist.count(name)) out.insert(name);
    }
  }
  if (out.empty()) {
    throw std::runtime_error("HL bootstrap failed: /info discovery returned zero symbols");
  }
  return out;
}

// ---- ContinuousWS -----------------------------------------------------------
// Worker thread. Subscribes to the public WS l2Book for every sym in the
// universe and STAYS subscribed forever (L1b always-subscribed model;
// see overmind/studies/hl_feed/publisher_dedup_design_20260625.md).
// Per WS msg: if the sym is in `caught_up_`, drop it; otherwise check the
// 3-streak catch-up against our time-indexed real top-5, succeed → mark
// caught_up_ + clear synth (no unsub send); else reseed synth.
// A forward block-number gap from the tail handler calls force_recatch_up
// which clears caught_up_ — the in-flight WS stream then drives reseed
// for every sym automatically.
class ContinuousWS {
 public:
  static constexpr int kMatchLevels = 5;
  static constexpr int kMatchStreakRequired = 3;

  ContinuousWS(BookSet* bookset, std::unordered_set<std::string> syms,
               std::string ws_url, bool verbose, std::atomic<bool>* stop_flag)
      : bookset_(bookset),
        syms_(std::move(syms)),
        ws_url_(std::move(ws_url)),
        verbose_(verbose),
        stop_flag_(stop_flag) {}

  void start() {
    ws_.setUrl(ws_url_);
    // Enable ixwebsocket's auto-reconnect: if HL drops us we want it back up
    // automatically. Our on_message callback re-subscribes on each Open.
    ws_.enableAutomaticReconnection();
    // ixwebsocket's setPingInterval sends WebSocket-protocol PING frames.
    // HL's "Inactive" check is at the application layer and ignores them, so
    // we ALSO run an app-level ping loop (see ping_thread_ below) that sends
    // {"method":"ping"} — mirrors pymultifeed.py:hl_ping and the Python
    // hl_node_publish.py ContinuousWS ping_task. Both layers cost nothing.
    ws_.setPingInterval(25);
    ix::SocketTLSOptions tls;
    tls.tls = true;
    if (std::filesystem::exists("/etc/ssl/certs/ca-certificates.crt")) {
      tls.caFile = "/etc/ssl/certs/ca-certificates.crt";
    }
    ws_.setTLSOptions(tls);
    ws_.setOnMessageCallback([this](const ix::WebSocketMessagePtr& msg) {
      this->on_message(msg);
    });
    ws_.start();

    // App-level ping loop. HL closes the connection at 60s of app-level
    // silence; 25s gives us two retries before that timer. Thread exits
    // when stop() is called.
    ping_stop_.store(false);
    ping_thread_ = std::thread([this]() {
      const std::string ping_msg = R"({"method":"ping"})";
      while (!ping_stop_.load()) {
        // Sleep 25s in 100ms slices so stop() returns promptly.
        for (int i = 0; i < 250 && !ping_stop_.load(); ++i) {
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        if (ping_stop_.load()) break;
        try {
          ws_.send(ping_msg);
        } catch (...) {
          // Best effort; if the socket's bad, auto-reconnect will recover.
        }
      }
    });
  }

  void stop() {
    ping_stop_.store(true);
    if (ping_thread_.joinable()) ping_thread_.join();
    ws_.stop();
  }

  size_t caught_up_count() {
    std::lock_guard<std::mutex> g(state_mu_);
    return caught_up_.size();
  }

  // L1b: clear catch-up state so the next WS l2Book messages reseed synth
  // for every sym again. No (un)subscribe traffic — we stay subscribed to the
  // full universe at all times. Called from BookSet::gap_callback when the
  // tail handler detects a forward block-number jump.
  void force_recatch_up() {
    std::lock_guard<std::mutex> g(state_mu_);
    caught_up_.clear();
    match_streak_.clear();
    ++post_gap_epochs_;
    if (verbose_) {
      fmt::print(stderr,
                 "[POST_GAP_PRICE] starting monitor epoch={} syms={}\n",
                 post_gap_epochs_, syms_.size());
    }
  }

  // L1c: freshest WS l2Book data.time seen across all syms, used by
  // wait_for_bootstrap_stream_fresh to gate publisher startup until the
  // node streaming file's block_time has caught up to within a few seconds
  // of WS. Updated with monotonic-max semantics so a late per-symbol WS
  // message can never move the reference clock backward.
  int64_t latest_l2book_time() const {
    return latest_l2book_time_ms_.load(std::memory_order_relaxed);
  }

  struct PostGapPriceStats {
    int64_t epochs = 0;
    int64_t checks = 0;
    int64_t bad_top_price_events = 0;
  };
  PostGapPriceStats post_gap_price_stats() {
    std::lock_guard<std::mutex> g(state_mu_);
    return {post_gap_epochs_, post_gap_price_checks_, post_gap_bad_top_price_events_};
  }

 private:
  void on_message(const ix::WebSocketMessagePtr& msg) {
    if (msg->type == ix::WebSocketMessageType::Open) {
      // Always-subscribed model (L1b): every reconnect re-subscribes to the
      // full universe so the steady-state flow is restored even after a long
      // disconnect. The caught_up_ flag governs only message handling
      // (reseed vs ignore), never subscription state.
      for (const auto& sym : syms_) ws_.send(make_l2book_sub(sym));
      if (verbose_) {
        size_t cu;
        {
          std::lock_guard<std::mutex> g(state_mu_);
          cu = caught_up_.size();
        }
        fmt::print("  [ws] (re)subscribed all {} syms (caught_up so far: {})\n",
                   syms_.size(), cu);
      }
      return;
    }
    if (msg->type == ix::WebSocketMessageType::Error) {
      if (verbose_) fmt::print("  [ws] error: {}\n", msg->errorInfo.reason);
      return;
    }
    if (msg->type == ix::WebSocketMessageType::Close) {
      if (verbose_) {
        fmt::print("  [ws] closed code={} reason={} remote={}\n",
                   msg->closeInfo.code, msg->closeInfo.reason,
                   msg->closeInfo.remote ? "yes" : "no");
      }
      return;
    }
    if (msg->type != ix::WebSocketMessageType::Message) return;
    handle_l2book(msg->str);
  }

  static std::string make_l2book_sub(const std::string& coin) {
    rapidjson::StringBuffer buf;
    rapidjson::Writer<rapidjson::StringBuffer> w(buf);
    w.StartObject();
    w.Key("method"); w.String("subscribe");
    w.Key("subscription");
    w.StartObject();
    w.Key("type"); w.String("l2Book");
    w.Key("coin"); w.String(coin.c_str());
    if (auto pos = coin.find(':'); pos != std::string::npos) {
      w.Key("dex"); w.String(coin.substr(0, pos).c_str());
    }
    w.EndObject();
    w.EndObject();
    return buf.GetString();
  }

  static std::string make_l2book_unsub(const std::string& coin) {
    rapidjson::StringBuffer buf;
    rapidjson::Writer<rapidjson::StringBuffer> w(buf);
    w.StartObject();
    w.Key("method"); w.String("unsubscribe");
    w.Key("subscription");
    w.StartObject();
    w.Key("type"); w.String("l2Book");
    w.Key("coin"); w.String(coin.c_str());
    if (auto pos = coin.find(':'); pos != std::string::npos) {
      w.Key("dex"); w.String(coin.substr(0, pos).c_str());
    }
    w.EndObject();
    w.EndObject();
    return buf.GetString();
  }

  void handle_l2book(const std::string& raw) {
    rapidjson::Document d;
    d.Parse(raw.c_str(), raw.size());
    if (d.HasParseError() || !d.IsObject()) return;
    auto ch_it = d.FindMember("channel");
    if (ch_it == d.MemberEnd() || !ch_it->value.IsString()) return;
    if (std::string(ch_it->value.GetString()) != "l2Book") return;
    auto data_it = d.FindMember("data");
    if (data_it == d.MemberEnd() || !data_it->value.IsObject()) return;
    const auto& data = data_it->value;
    if (!data.HasMember("coin") || !data["coin"].IsString()) return;
    if (!data.HasMember("time") || !data["time"].IsInt64()) return;
    if (!data.HasMember("levels") || !data["levels"].IsArray() || data["levels"].Size() < 2) return;
    std::string coin = data["coin"].GetString();
    int64_t ws_t = data["time"].GetInt64();

    // L1c: ratchet the freshest WS l2Book time forward. Done BEFORE the
    // caught_up_ check so even messages we'd otherwise drop still contribute
    // to the freshness clock used by wait_for_bootstrap_stream_fresh.
    int64_t prev = latest_l2book_time_ms_.load(std::memory_order_relaxed);
    while (ws_t > prev &&
           !latest_l2book_time_ms_.compare_exchange_weak(
               prev, ws_t, std::memory_order_relaxed)) {}

    int64_t post_gap_epoch = 0;
    {
      std::lock_guard<std::mutex> g(state_mu_);
      // L1b: caught_up_ now means "ignore further WS for this sym" — we stay
      // subscribed, but we no longer drive reseed. Cleared en masse by
      // force_recatch_up on a forward gap.
      if (caught_up_.count(coin)) return;
      post_gap_epoch = post_gap_epochs_;
    }

    auto extract = [](const rapidjson::Value& arr) {
      // Require all three fields up-front. A missing px or sz would leave
      // the level with an empty string, and reseed_synth_from_ws's std::stod
      // on lvl.sz / map lookup on lvl.px would crash the WS thread.
      // Better to drop one malformed level than to take down the whole
      // catch-up cycle on a single bad packet.
      std::vector<BookSet::WsLevel> out;
      out.reserve(arr.Size());
      for (const auto& v : arr.GetArray()) {
        if (!v.IsObject()) continue;
        if (!v.HasMember("px") || !v["px"].IsString()) continue;
        if (!v.HasMember("sz") || !v["sz"].IsString()) continue;
        if (!v.HasMember("n") || !v["n"].IsInt()) continue;
        BookSet::WsLevel l;
        l.px = v["px"].GetString();
        l.sz = v["sz"].GetString();
        l.n = v["n"].GetInt();
        out.push_back(std::move(l));
      }
      return out;
    };
    auto ws_bids = extract(data["levels"][0]);
    auto ws_asks = extract(data["levels"][1]);

    // 1. Catch-up check (real-only, time-indexed against ws_t).
    bool matched = coin_matches_ws(coin, ws_t, ws_bids, ws_asks);
    if (matched) {
      int32_t streak;
      {
        std::lock_guard<std::mutex> g(state_mu_);
        streak = ++match_streak_[coin];
      }
      if (streak >= kMatchStreakRequired) {
        // L1b: do NOT send an unsubscribe — stay subscribed forever. Just
        // mark caught_up_ so subsequent WS messages early-return. The
        // make_l2book_unsub helper is kept around in case a future operator
        // wants to use it, but no production code path calls it now.
        {
          std::lock_guard<std::mutex> g(state_mu_);
          caught_up_.insert(coin);
        }
        bookset_->clear_synth_for(coin);
        if (verbose_) {
          fmt::print("  [ws] CAUGHT UP: {} (total caught_up: {}/{})\n",
                     coin, caught_up_count(), syms_.size());
        }
        return;
      }
    } else {
      std::lock_guard<std::mutex> g(state_mu_);
      match_streak_[coin] = 0;
    }

    // 2. Reseed synth from this WS snapshot (Rules 1+2).
    std::string bad_top_detail;
    bool check_bad_top = post_gap_epoch > 0 && !matched;
    bool bad_top = bookset_->reseed_synth_from_ws(
        coin, ws_bids, ws_asks, check_bad_top ? &bad_top_detail : nullptr);
    if (check_bad_top) {
      observe_post_gap_price(post_gap_epoch, coin, ws_t, bad_top, bad_top_detail);
    }
  }

  void observe_post_gap_price(int64_t epoch, const std::string& coin, int64_t ws_t,
                              bool bad, const std::string& detail) {
    int64_t bad_events = 0;
    {
      std::lock_guard<std::mutex> g(state_mu_);
      if (epoch != post_gap_epochs_) return;
      ++post_gap_price_checks_;
      if (bad) {
        bad_events = ++post_gap_bad_top_price_events_;
      }
    }

    if (!bad) return;
    if (bad_events > 20 && !verbose_) return;
    fmt::print(stderr,
               "[POST_GAP_PRICE] epoch={} event={} coin={} ws_t={} {}\n",
               epoch, bad_events, coin, ws_t, detail);
  }

  bool coin_matches_ws(const std::string& coin, int64_t ws_t,
                       const std::vector<BookSet::WsLevel>& ws_bids,
                       const std::vector<BookSet::WsLevel>& ws_asks) {
    auto snap = bookset_->snapshot_real_l2_top_at(coin, kMatchLevels, ws_t);
    if (!snap) return false;
    const auto& [nb, na] = *snap;
    if ((int32_t)nb.size() < kMatchLevels || (int32_t)na.size() < kMatchLevels) return false;
    if ((int32_t)ws_bids.size() < kMatchLevels || (int32_t)ws_asks.size() < kMatchLevels) return false;
    auto eq_lvl = [](const L3Book::Level& a, const BookSet::WsLevel& b) {
      return a.px == b.px && std::stod(a.sz) == std::stod(b.sz) && a.n == b.n;
    };
    for (int i = 0; i < kMatchLevels; ++i) if (!eq_lvl(nb[i], ws_bids[i])) return false;
    for (int i = 0; i < kMatchLevels; ++i) if (!eq_lvl(na[i], ws_asks[i])) return false;
    return true;
  }

  BookSet* bookset_;
  std::unordered_set<std::string> syms_;
  std::string ws_url_;
  bool verbose_;
  std::atomic<bool>* stop_flag_;

  std::mutex state_mu_;
  std::unordered_set<std::string> caught_up_;
  std::unordered_map<std::string, int32_t> match_streak_;
  int64_t post_gap_epochs_ = 0;
  int64_t post_gap_price_checks_ = 0;
  int64_t post_gap_bad_top_price_events_ = 0;

  // L1c: monotonic-max snapshot of WS l2Book data.time.
  std::atomic<int64_t> latest_l2book_time_ms_{0};

  ix::WebSocket ws_;
  std::thread ping_thread_;
  std::atomic<bool> ping_stop_{false};
};

// ---- emit -------------------------------------------------------------------
struct EmitStats {
  int64_t emitted = 0;
  int64_t anomalies = 0;
};

bool levels_crossed_or_locked(const std::vector<L3Book::Level>& bids,
                              const std::vector<L3Book::Level>& asks) {
  if (bids.empty() || asks.empty()) return false;
  return std::stod(bids.front().px) >= std::stod(asks.front().px);
}

void emit_coin(const std::string& coin, BookSet& bookset, zmq::socket_t* pub,
               bool print_only, bool verbose, int32_t max_levels,
               EmitStats& stats) {
  auto snap = bookset.snapshot_l2_for_emit(coin);
  if (!snap) return;

  if (snap->bids.empty() && snap->asks.empty()) {
    ++stats.anomalies;
    if (stats.anomalies <= 5 || verbose) {
      fmt::print("[ANOM] {}: dropping empty book snapshot\n", coin);
    }
    return;
  }

  try {
    if (levels_crossed_or_locked(snap->bids, snap->asks)) {
      ++stats.anomalies;
      if (stats.anomalies <= 5 || verbose) {
        fmt::print("[ANOM] {}: dropping crossed/locked book bid={} ask={}\n",
                   coin, snap->bids.front().px, snap->asks.front().px);
      }
      return;
    }
  } catch (const std::exception& e) {
    ++stats.anomalies;
    if (stats.anomalies <= 5 || verbose) {
      fmt::print(stderr, "[ANOM] {}: dropping book with invalid top price: {}\n",
                 coin, e.what());
    }
    return;
  }

  if (print_only) {
    if (verbose || stats.emitted < 6) {
      fmt::print("  {:14s} bids={:3d} asks={:3d}", coin, snap->bids.size(), snap->asks.size());
      if (!snap->bids.empty() && !snap->asks.empty()) {
        fmt::print(" top={}/{}\n", snap->bids.front().px, snap->asks.front().px);
      } else {
        fmt::print(" (one-sided)\n");
      }
    }
  } else {
    // E2: reuse a thread_local PbMessage + payload buffer across emits.
    // protobuf's Clear() zeros fields without freeing the internal repeated-
    // field arenas (bids/asks), so the next emit reuses the capacity.
    // Same idea for `payload` — its std::string buffer keeps its capacity
    // after clear(), avoiding a heap alloc on every emit.
    thread_local mdmsg::PbMessage msg;
    thread_local std::string payload;
    msg.Clear();
    msg.set_symbol_id(coin);
    msg.set_market(mdmsg::PBMARKET_HYPERLIQUID);
    auto* pbs = msg.mutable_book_snapshot();
    if (snap->event_time_ms) pbs->set_event_time(snap->event_time_ms);
    // Truncate to top max_levels per side. <=0 means "emit all".
    int32_t n_bids = (int32_t)snap->bids.size();
    int32_t n_asks = (int32_t)snap->asks.size();
    if (max_levels > 0) {
      if (n_bids > max_levels) n_bids = max_levels;
      if (n_asks > max_levels) n_asks = max_levels;
    }
    for (int32_t i = 0; i < n_bids; ++i) {
      const auto& l = snap->bids[i];
      auto* pl = pbs->add_bids();
      pl->set_px(l.px); pl->set_qty(l.sz); pl->set_numords(l.n);
    }
    for (int32_t i = 0; i < n_asks; ++i) {
      const auto& l = snap->asks[i];
      auto* pl = pbs->add_asks();
      pl->set_px(l.px); pl->set_qty(l.sz); pl->set_numords(l.n);
    }
    payload.clear();
    msg.SerializeToString(&payload);
    pub->send(zmq::buffer(payload), zmq::send_flags::none);
  }
  ++stats.emitted;
}

// ---- handle_line ------------------------------------------------------------
struct CoinFilter {
  std::optional<std::string> only_coin;
  std::unordered_set<std::string> blacklist;
  bool allows(const std::string& coin) const {
    if (only_coin && coin != *only_coin) return false;
    if (blacklist.count(coin)) return false;
    return true;
  }
};

// Emit-and-clear the dirty-coin set. Pulled out of handle_line so the same
// call can run at hour rotation and process shutdown — both points where
// the next block boundary that would normally trigger an emit is no longer
// going to come, and any unemitted dirty coins would otherwise be lost.
void flush_dirty(BookSet& bookset, zmq::socket_t* pub, const CoinFilter& filter,
                 bool print_only, bool verbose, int32_t max_levels,
                 EmitStats& stats) {
  for (const auto& coin : bookset.take_dirty()) {
    if (!filter.allows(coin)) continue;
    emit_coin(coin, bookset, pub, print_only, verbose, max_levels, stats);
  }
}

void handle_line(std::string_view line, BookSet& bookset, zmq::socket_t* pub,
                 const CoinFilter& filter, bool print_only, bool verbose,
                 int32_t max_levels, EmitStats& stats) {
  rapidjson::Document d;
  // rapidjson::Parse takes (data, length); doesn't require null-termination.
  d.Parse(line.data(), line.size());
  if (d.HasParseError() || !d.IsObject()) return;

  int64_t block_no = -1;
  if (auto it = d.FindMember("block_number"); it != d.MemberEnd() && it->value.IsInt64()) {
    block_no = it->value.GetInt64();
  }
  int64_t new_block_time_ms = 0;
  if (auto it = d.FindMember("block_time"); it != d.MemberEnd() && it->value.IsString()) {
    new_block_time_ms = parse_node_time_ms(it->value.GetString());
  }

  // L1a: backward-replay dedup. After a visor restart, catch-up re-streams
  // blocks snap+1..H. We drop everything <= our last fully-processed block.
  // Guarded on block_no > 0 so malformed packets fall through to the
  // existing block_changed/events flow (legacy behavior).
  if (block_no > 0 && block_no <= bookset.highest_block_seen) {
    ++bookset.duplicates_dropped;
    // Always log first occurrence + every 100k drops so a real catch-up replay
    // (~5M packets observed on n2) leaves a visible trail in production logs.
    // Verbose tightens to every 10k for tighter observation during testing.
    int64_t period = verbose ? 10000 : 100000;
    if (bookset.duplicates_dropped == 1 ||
        bookset.duplicates_dropped % period == 0) {
      fmt::print(stderr,
                 "[DEDUP] drop replay block {} (highest_seen={}, total dropped={})\n",
                 block_no, bookset.highest_block_seen, bookset.duplicates_dropped);
    }
    return;
  }

  // L1b: forward-gap detection. Compare against current_block, NOT
  // highest_block_seen (which lags by one block due to lazy ratcheting and
  // would false-fire on every normal transition). Triggers WS re-catch-up
  // via the gap_callback wired in from run_tail. See publisher_dedup_design_20260625.md.
  //
  // Threshold: HL Mainnet's streaming file omits packets for blocks with
  // no book-diff events across any coin (~5-6% of blocks). Treating those
  // as "real gaps" would fire WS re-catch-up several times per minute and
  // keep the publisher in perpetual bootstrap. A real catch-up replay
  // spans thousands of blocks (~9 min × ~2 blocks/s after a visor restart),
  // so the 100-block threshold cleanly distinguishes empty-block skips
  // from genuine forward gaps without missing real problems.
  constexpr int64_t kGapTriggerThreshold = 100;
  if (block_no > 0 && bookset.current_block > 0 &&
      block_no > bookset.current_block + 1) {
    int64_t gap = block_no - bookset.current_block - 1;
    if (gap >= kGapTriggerThreshold) {
      ++bookset.gaps_observed;
      fmt::print(stderr,
                 "[GAP] forward jump {} -> {} (gap={}); {}\n",
                 bookset.current_block, block_no, gap,
                 bookset.gap_callback ? "triggering WS re-catch-up"
                                      : "no WS callback registered");
      if (bookset.gap_callback) bookset.gap_callback();
    } else {
      ++bookset.small_gaps_observed;
    }
  }

  if (bookset.block_changed(block_no)) {
    flush_dirty(bookset, pub, filter, print_only, verbose, max_levels, stats);
    bookset.advance_block(block_no, new_block_time_ms);
  }

  auto evs = d.FindMember("events");
  if (evs == d.MemberEnd() || !evs->value.IsArray()) return;
  // LOCK-PACKET: hold BookSet::mu once for the whole events loop instead
  // of re-acquiring per event. Drops ~10 lock acquisitions per packet to 1.
  std::lock_guard<std::mutex> g(bookset.mu);
  for (const auto& ev : evs->value.GetArray()) {
    if (!ev.IsObject()) continue;
    const char* coin = ev.HasMember("coin") && ev["coin"].IsString() ? ev["coin"].GetString() : nullptr;
    const char* side = ev.HasMember("side") && ev["side"].IsString() ? ev["side"].GetString() : nullptr;
    const char* px = ev.HasMember("px") && ev["px"].IsString() ? ev["px"].GetString() : nullptr;
    int64_t oid = ev.HasMember("oid") && ev["oid"].IsInt64() ? ev["oid"].GetInt64() : 0;
    if (!coin || !side || !px) continue;
    std::string coin_s = coin;
    if (!filter.allows(coin_s)) continue;

    auto diff_it = ev.FindMember("raw_book_diff");
    if (diff_it == ev.MemberEnd()) continue;
    const auto& diff = diff_it->value;

    // std::stod inside apply_* throws std::invalid_argument / std::out_of_range
    // on malformed numeric strings. The whole publisher running as a
    // systemd daemon means an unhandled throw would crash the process,
    // systemd would restart it, the new process would --bootstrap, and
    // consumers would briefly fall back to WS — all because of one bad
    // line. Contain the damage: skip the event, count it as an anomaly,
    // continue. Doesn't affect the happy path (no throw → no catch cost).
    try {
      if (diff.IsString() && std::string(diff.GetString()) == "remove") {
        bookset.apply_event_remove_unlocked(coin_s, side, px, oid);
      } else if (diff.IsObject() && diff.HasMember("new") && diff["new"].IsObject()
                 && diff["new"].HasMember("sz") && diff["new"]["sz"].IsString()) {
        bookset.apply_event_new_unlocked(coin_s, side, px, oid, diff["new"]["sz"].GetString());
      } else if (diff.IsObject() && diff.HasMember("update") && diff["update"].IsObject()
                 && diff["update"].HasMember("newSz") && diff["update"]["newSz"].IsString()) {
        std::string orig = "0";
        if (diff["update"].HasMember("origSz") && diff["update"]["origSz"].IsString()) {
          orig = diff["update"]["origSz"].GetString();
        }
        bookset.apply_event_update_unlocked(coin_s, side, px, oid,
                                            diff["update"]["newSz"].GetString(), orig);
      }
    } catch (const std::exception& e) {
      ++stats.anomalies;
      if (stats.anomalies <= 5 || verbose) {
        fmt::print(stderr, "[ANOM] handle_line: skipping malformed event for {} {}@{}: {}\n",
                   coin_s, side, px, e.what());
      }
    }
  }
}

// ---- hourly_path ------------------------------------------------------------
std::string hourly_path(const std::string& data_dir) {
  std::time_t t = std::time(nullptr);
  std::tm tm{};
  gmtime_r(&t, &tm);
  char date_buf[32];
  std::snprintf(date_buf, sizeof(date_buf), "%04d%02d%02d",
                tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday);
  return fmt::format("{}/{}/{}", data_dir, date_buf, tm.tm_hour);
}

// Read the last fully-terminated line of `path`. Reads up to the last 16 KiB
// of the file, walks back to find the trailing '\n' (end of last complete
// line), then back again to find the '\n' before it (start of that line).
// Returns "" on any I/O / framing issue — caller treats that as "try again".
std::string read_last_complete_line(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return "";
  f.seekg(0, std::ios::end);
  std::streamoff size = f.tellg();
  if (size <= 0) return "";
  std::streamoff back = std::min<std::streamoff>(16384, size);
  f.seekg(size - back, std::ios::beg);
  std::string buf;
  buf.resize(static_cast<size_t>(back));
  f.read(buf.data(), back);
  buf.resize(static_cast<size_t>(f.gcount()));
  size_t end_nl = buf.find_last_of('\n');
  if (end_nl == std::string::npos || end_nl == 0) return "";
  size_t prev_nl = buf.find_last_of('\n', end_nl - 1);
  size_t start = (prev_nl == std::string::npos) ? 0 : prev_nl + 1;
  return buf.substr(start, end_nl - start);
}

// L1c: gate publisher startup on the node stream catching up to within
// `freshness_ms` of the freshest WS l2Book time. See
// overmind/studies/hl_feed/publisher_dedup_design_20260625.md.
// Returns true on success, false on timeout or g_stop. Caller must seek the
// streaming file to EOF AFTER this returns; any historical replay bytes
// appended while we waited must not be tailed as live.
bool wait_for_bootstrap_stream_fresh(const std::string& data_dir,
                                     const ContinuousWS& cws,
                                     int64_t freshness_ms,
                                     int64_t max_wait_s,
                                     bool verbose) {
  auto start = std::chrono::steady_clock::now();
  fmt::print("  [L1c] waiting for stream block_time within {} ms of WS l2Book "
             "(timeout {} s)\n", freshness_ms, max_wait_s);
  int64_t last_log_s = 0;
  while (!g_stop) {
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::steady_clock::now() - start).count();
    if (elapsed > max_wait_s) {
      fmt::print(stderr,
                 "[L1c] timeout after {} s waiting for stream freshness; "
                 "aborting startup\n", max_wait_s);
      return false;
    }
    std::string hour_file = hourly_path(data_dir);
    std::string last_line = read_last_complete_line(hour_file);
    int64_t ws_ms = cws.latest_l2book_time();
    int64_t stream_ms = -1;
    if (!last_line.empty()) {
      rapidjson::Document d;
      d.Parse(last_line.c_str(), last_line.size());
      if (!d.HasParseError() && d.IsObject()) {
        if (auto it = d.FindMember("block_time");
            it != d.MemberEnd() && it->value.IsString()) {
          stream_ms = parse_node_time_ms(it->value.GetString());
        }
      }
    }
    if (ws_ms > 0 && stream_ms > 0) {
      int64_t lag = ws_ms - stream_ms;
      if (lag <= freshness_ms) {
        fmt::print("  [L1c] stream fresh: ws_t={} stream_t={} lag_ms={}\n",
                   ws_ms, stream_ms, lag);
        return true;
      }
      if (verbose || elapsed - last_log_s >= 15) {
        fmt::print("  [L1c] catching up: lag {} s (elapsed {} s)\n",
                   lag / 1000, elapsed);
        last_log_s = elapsed;
      }
    } else if (verbose || elapsed - last_log_s >= 15) {
      fmt::print("  [L1c] waiting for {} (ws_ms={}, stream_ms={}, elapsed {} s)\n",
                 ws_ms <= 0 && stream_ms <= 0 ? "WS+stream"
                 : ws_ms <= 0 ? "WS" : "stream",
                 ws_ms, stream_ms, elapsed);
      last_log_s = elapsed;
    }
    std::this_thread::sleep_for(std::chrono::seconds(2));
  }
  return false;
}

// ---- run_file ---------------------------------------------------------------
void run_file(const std::string& path, const std::string& endpoint,
              const CoinFilter& filter, bool print_only, bool verbose,
              int32_t max_levels, int32_t l3_max_levels_per_side) {
  zmq::context_t ctx{1};
  std::unique_ptr<zmq::socket_t> pub;
  if (!print_only) {
    pub = std::make_unique<zmq::socket_t>(ctx, zmq::socket_type::pub);
    pub->bind(endpoint);
    fmt::print("PUB bound to {}\n", endpoint);
    std::this_thread::sleep_for(std::chrono::milliseconds{300});
  }
  BookSet bookset;
  bookset.l3_cap = l3_max_levels_per_side;
  EmitStats stats;
  std::ifstream f(path);
  if (!f) {
    fmt::print(stderr, "cannot open {}\n", path);
    return;
  }
  std::string line;
  int64_t n_lines = 0;
  while (!g_stop && std::getline(f, line)) {
    ++n_lines;
    handle_line(line, bookset, pub.get(), filter, print_only, verbose,
                max_levels, stats);
  }
  flush_dirty(bookset, pub.get(), filter, print_only, verbose, max_levels, stats);
  fmt::print("\n=== done === lines={} events={} emitted={} anomalies={} "
             "duplicates_dropped={} gaps_observed={} small_gaps={}\n",
             n_lines, bookset.events_seen, stats.emitted, stats.anomalies,
             bookset.duplicates_dropped, bookset.gaps_observed, bookset.small_gaps_observed);
}

// ---- fills mode -------------------------------------------------------------
//
// Tails `node_fills_streaming/hourly/{YYYYMMDD}/{H}` files. Each fill arrives
// twice (once per counterparty); we pair by `tid` and emit a single PbTrade
// with passive_was_buyer set from the maker's side (the one with
// crossed=false). Mirrors Python's run_tail_fills.

struct Fill {
  std::string user;
  std::string coin;
  std::string side;   // "B" or "A"
  std::string px;
  std::string sz;
  int64_t oid = 0;
  int64_t tid = 0;
  int64_t time_ms = 0;
  bool crossed = false;
  int64_t packet_rx_ms = 0;  // local_time of the packet; latest of the two used as rx_timestamp
  int64_t arrival_wall_ms = 0;  // for TTL cleanup of unmatched
};

struct FillsStats {
  int64_t lines = 0;
  int64_t events = 0;
  int64_t pairs_emitted = 0;
  int64_t unpaired_flushed = 0;
  int64_t anomalies = 0;
  int64_t rotations = 0;
  // L1a/L1b: restart resilience for fills.
  //   highest_block_seen / current_block: mirror BookSet's lazy ratchet so the
  //   same dedup/gap reasoning applies to fills. Fills lines carry
  //   block_number at the packet level (verified on n1); we just hadn't
  //   parsed it before.
  //   duplicates_dropped / gaps_observed: counters surfaced in exit summary.
  //   Fills has no WS reseed, so gaps are LOG-ONLY — see "Fills caveat" in
  //   the design doc.
  int64_t highest_block_seen = -1;
  int64_t current_block = -1;
  int64_t duplicates_dropped = 0;
  int64_t gaps_observed = 0;
  int64_t small_gaps_observed = 0;
};

void emit_trade_pair(const Fill& a, const Fill& b, zmq::socket_t* pub,
                     bool print_only, bool verbose, FillsStats& stats,
                     bool log_emit_time = false) {
  // Maker = the side with crossed == false (resting order).
  const Fill& maker = !a.crossed ? a : b;
  const Fill& taker = !a.crossed ? b : a;
  if (maker.crossed) {
    // Both sides crossed — shouldn't happen.
    ++stats.anomalies;
    return;
  }
  if (maker.side == taker.side) {
    ++stats.anomalies;
    return;
  }
  bool passive_was_buyer = (maker.side == "B");
  int64_t buy_oid  = passive_was_buyer ? maker.oid : taker.oid;
  int64_t sell_oid = passive_was_buyer ? taker.oid : maker.oid;

  if (print_only) {
    if (verbose || stats.pairs_emitted < 10) {
      const char* aggressor = passive_was_buyer ? "seller" : "buyer";
      fmt::print("  {:14s} px={:>10} sz={:>10} buy={} sell={} aggressor={}\n",
                 maker.coin, maker.px, maker.sz, buy_oid, sell_oid, aggressor);
    }
  } else {
    // Same reuse pattern as emit_coin. Fills are emitted on a different
    // thread than books (separate `run_tail_fills` vs `run_tail` process
    // for the books and fills services, but use a different thread_local
    // here regardless so the threading story stays clean).
    thread_local mdmsg::PbMessage msg;
    thread_local std::string payload;
    msg.Clear();
    msg.set_symbol_id(maker.coin);
    msg.set_market(mdmsg::PBMARKET_HYPERLIQUID);
    int64_t rx = std::max(a.packet_rx_ms, b.packet_rx_ms);
    if (rx > 0) msg.set_rx_timestamp(rx);
    auto* trade = msg.mutable_book_trade();
    trade->set_px(maker.px);
    trade->set_qty(maker.sz);
    trade->set_trade_id(maker.tid);
    trade->set_buy_ord_id(buy_oid);
    trade->set_sell_ord_id(sell_oid);
    trade->set_trade_time(maker.time_ms);
    trade->set_passive_was_buyer(passive_was_buyer);
    payload.clear();
    msg.SerializeToString(&payload);
    // DIAG: capture wall_ms immediately before the ZMQ send so the gap
    // between fills-line read (arrival_wall_ms on each Fill) and PUB send
    // can be measured per-trade against a consumer's recv timestamp.
    // Off by default; gated by --log-emit-time.
    int64_t emit_wall = log_emit_time ? now_ms() : 0;
    pub->send(zmq::buffer(payload), zmq::send_flags::none);
    if (log_emit_time) {
      // read_ms = the LATER of the two fills' arrival (when the pair
      // became emittable). dur_ms = read→PUB-send delay introduced by
      // the publisher.
      int64_t read_ms = std::max(a.arrival_wall_ms, b.arrival_wall_ms);
      fmt::print("EMIT_DIAG tid={} trade_time={} read_ms={} emit_ms={} dur_ms={}\n",
                 maker.tid, maker.time_ms, read_ms, emit_wall,
                 emit_wall - read_ms);
    }
  }
  ++stats.pairs_emitted;
}

void handle_fills_line(const std::string& line,
                       std::unordered_map<int64_t, Fill>& pending,
                       const CoinFilter& filter, zmq::socket_t* pub,
                       bool print_only, bool verbose, FillsStats& stats,
                       bool log_emit_time = false) {
  ++stats.lines;
  rapidjson::Document d;
  d.Parse(line.c_str(), line.size());
  if (d.HasParseError() || !d.IsObject()) return;
  int64_t packet_rx_ms = 0;
  if (auto it = d.FindMember("local_time"); it != d.MemberEnd() && it->value.IsString()) {
    int64_t v = parse_node_time_ms(it->value.GetString());
    if (v > 0) packet_rx_ms = v;
  }

  // L1a/L1b: same logic as books handle_line. Fills lines also carry
  // block_number at the packet level — see publisher_dedup_design_20260625.md.
  int64_t block_no = -1;
  if (auto it = d.FindMember("block_number"); it != d.MemberEnd() && it->value.IsInt64()) {
    block_no = it->value.GetInt64();
  }
  // Dedup. Same first + every 100k (non-verbose) / 10k (verbose) cadence as
  // books mode so a real catch-up replay leaves a trail in production logs.
  if (block_no > 0 && block_no <= stats.highest_block_seen) {
    ++stats.duplicates_dropped;
    int64_t period = verbose ? 10000 : 100000;
    if (stats.duplicates_dropped == 1 ||
        stats.duplicates_dropped % period == 0) {
      fmt::print(stderr,
                 "[DEDUP] fills drop replay block {} (highest_seen={}, total dropped={})\n",
                 block_no, stats.highest_block_seen, stats.duplicates_dropped);
    }
    return;
  }
  // Gap (log only — no WS reseed in fills mode). Same 100-block threshold as
  // books mode to avoid spamming on empty-block skips.
  constexpr int64_t kGapTriggerThreshold = 100;
  if (block_no > 0 && stats.current_block > 0 &&
      block_no > stats.current_block + 1) {
    int64_t gap = block_no - stats.current_block - 1;
    if (gap >= kGapTriggerThreshold) {
      ++stats.gaps_observed;
      fmt::print(stderr,
                 "[GAP] fills forward jump {} -> {} (gap={}); no recovery available\n",
                 stats.current_block, block_no, gap);
    } else {
      ++stats.small_gaps_observed;
    }
  }
  // Lazy ratchet: graduate the outgoing current_block when we transition.
  if (block_no > 0 && block_no != stats.current_block) {
    if (stats.current_block > 0 && stats.current_block > stats.highest_block_seen) {
      stats.highest_block_seen = stats.current_block;
    }
    stats.current_block = block_no;
  }

  auto evs = d.FindMember("events");
  if (evs == d.MemberEnd() || !evs->value.IsArray()) return;

  int64_t now_ms_val = now_ms();
  for (const auto& ev : evs->value.GetArray()) {
    // [user_addr, fill_dict]
    if (!ev.IsArray() || ev.Size() != 2) continue;
    if (!ev[0].IsString() || !ev[1].IsObject()) continue;
    const auto& f = ev[1];
    if (!f.HasMember("coin") || !f["coin"].IsString()) continue;
    std::string coin = f["coin"].GetString();
    if (!filter.allows(coin)) continue;
    if (!f.HasMember("tid") || !f["tid"].IsInt64()) continue;
    int64_t tid = f["tid"].GetInt64();

    Fill fill;
    fill.user = ev[0].GetString();
    fill.coin = std::move(coin);
    fill.tid = tid;
    fill.packet_rx_ms = packet_rx_ms;
    fill.arrival_wall_ms = now_ms_val;
    if (f.HasMember("side") && f["side"].IsString()) fill.side = f["side"].GetString();
    if (f.HasMember("px") && f["px"].IsString()) fill.px = f["px"].GetString();
    if (f.HasMember("sz") && f["sz"].IsString()) fill.sz = f["sz"].GetString();
    if (f.HasMember("oid") && f["oid"].IsInt64()) fill.oid = f["oid"].GetInt64();
    if (f.HasMember("time") && f["time"].IsInt64()) fill.time_ms = f["time"].GetInt64();
    if (f.HasMember("crossed") && f["crossed"].IsBool()) fill.crossed = f["crossed"].GetBool();

    ++stats.events;
    auto it = pending.find(tid);
    if (it != pending.end()) {
      Fill other = std::move(it->second);
      pending.erase(it);
      emit_trade_pair(other, fill, pub, print_only, verbose, stats, log_emit_time);
    } else {
      pending.emplace(tid, std::move(fill));
    }
  }
  // Periodic TTL cleanup of stranded singletons.
  if (stats.events && stats.events % 5000 == 0) {
    int64_t cutoff = now_ms_val - 60000;  // 60s TTL
    std::vector<int64_t> stale;
    for (const auto& [t, f] : pending) {
      if (f.arrival_wall_ms < cutoff) stale.push_back(t);
    }
    for (int64_t t : stale) pending.erase(t);
    stats.unpaired_flushed += static_cast<int64_t>(stale.size());
    if (verbose && !stale.empty()) {
      fmt::print("  [flushed {} stale unpaired tids; pending={}]\n",
                 stale.size(), pending.size());
    }
  }
}

void run_tail_fills(const std::string& data_dir, const std::string& endpoint,
                    const CoinFilter& filter, bool print_only, bool verbose,
                    bool from_start, double poll_interval_s,
                    bool log_emit_time = false) {
  zmq::context_t ctx{1};
  std::unique_ptr<zmq::socket_t> pub;
  if (!print_only) {
    pub = std::make_unique<zmq::socket_t>(ctx, zmq::socket_type::pub);
    pub->bind(endpoint);
    fmt::print("PUB bound to {}\n", endpoint);
    std::this_thread::sleep_for(std::chrono::milliseconds{300});
  }

  std::unordered_map<int64_t, Fill> pending;
  FillsStats stats;

  std::string current_path = hourly_path(data_dir);
  fmt::print("TAIL FILLS mode: watching {}\ncurrent hour file: {}\n", data_dir, current_path);
  while (!g_stop) {
    std::ifstream check(current_path);
    if (check) break;
    std::this_thread::sleep_for(std::chrono::duration<double>{poll_interval_s});
    std::string np = hourly_path(data_dir);
    if (np != current_path) current_path = np;
  }
  if (g_stop) return;

  std::ifstream f(current_path);
  if (!from_start) f.seekg(0, std::ios::end);
  std::string buf;
  buf.reserve(65536);
  while (!g_stop) {
    char chunk[65536];
    f.clear();
    f.read(chunk, sizeof(chunk));
    std::streamsize got = f.gcount();
    if (got > 0) {
      buf.append(chunk, chunk + got);
      size_t pos = 0, nl;
      while ((nl = buf.find('\n', pos)) != std::string::npos) {
        if (nl > pos) {
          handle_fills_line(buf.substr(pos, nl - pos), pending, filter,
                            pub.get(), print_only, verbose, stats, log_emit_time);
        }
        pos = nl + 1;
      }
      buf.erase(0, pos);
      continue;
    }
    std::string np = hourly_path(data_dir);
    if (np != current_path) {
      // FINAL DRAIN: hl-visor's writes to the closing hour file may not
      // have landed in our page cache when this poll returned 0 bytes.
      // Give the OS write buffers a moment, then read until N consecutive
      // empty polls. Without this we silently drop trailing fills that
      // were written in the last ~100 ms of the hour — see the 4-tid
      // case in hour 8 of 20260612 traced via simple_datalog.
      std::this_thread::sleep_for(std::chrono::seconds(1));
      int drained_lines = 0;
      for (int empty_reads = 0; empty_reads < 3 && !g_stop;) {
        f.clear();
        f.read(chunk, sizeof(chunk));
        std::streamsize gtmp = f.gcount();
        if (gtmp > 0) {
          buf.append(chunk, chunk + gtmp);
          size_t pos = 0, nl;
          while ((nl = buf.find('\n', pos)) != std::string::npos) {
            if (nl > pos) {
              handle_fills_line(buf.substr(pos, nl - pos), pending, filter,
                                pub.get(), print_only, verbose, stats, log_emit_time);
              ++drained_lines;
            }
            pos = nl + 1;
          }
          buf.erase(0, pos);
          empty_reads = 0;
        } else {
          ++empty_reads;
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
      }
      ++stats.rotations;
      fmt::print("\n=== rotation {}: {} -> {} === lines={} events={} pairs={} "
                 "unpaired_flushed={} anomalies={} duplicates_dropped={} "
                 "gaps_observed={} small_gaps={} (drained {} late lines)\n",
                 stats.rotations, current_path, np, stats.lines, stats.events,
                 stats.pairs_emitted, stats.unpaired_flushed, stats.anomalies,
                 stats.duplicates_dropped, stats.gaps_observed,
                 stats.small_gaps_observed, drained_lines);
      current_path = np;
      f.close();
      while (!g_stop) {
        std::ifstream check(current_path);
        if (check) break;
        std::this_thread::sleep_for(std::chrono::duration<double>{poll_interval_s});
      }
      if (g_stop) break;
      f.open(current_path);
      buf.clear();
      continue;
    }
    std::this_thread::sleep_for(std::chrono::duration<double>{poll_interval_s});
  }

  fmt::print("\n=== exit === lines={} events={} pairs={} pending_unmatched={} "
             "unpaired_flushed={} anomalies={} rotations={} duplicates_dropped={} "
             "gaps_observed={} small_gaps={}\n",
             stats.lines, stats.events, stats.pairs_emitted, pending.size(),
             stats.unpaired_flushed, stats.anomalies, stats.rotations,
             stats.duplicates_dropped, stats.gaps_observed, stats.small_gaps_observed);
}

// ---- run_tail ---------------------------------------------------------------
void run_tail(const std::string& data_dir, const std::string& endpoint,
              const CoinFilter& filter, bool print_only, bool verbose,
              bool from_start, double poll_interval_s,
              bool bootstrap, const std::string& public_ws_url,
              const std::string& info_url, int32_t max_levels,
              int32_t l3_max_levels_per_side) {
  zmq::context_t ctx{1};
  std::unique_ptr<zmq::socket_t> pub;
  if (!print_only) {
    pub = std::make_unique<zmq::socket_t>(ctx, zmq::socket_type::pub);
    pub->bind(endpoint);
    fmt::print("PUB bound to {}\n", endpoint);
    std::this_thread::sleep_for(std::chrono::milliseconds{300});
  }

  BookSet bookset;
  bookset.l3_cap = l3_max_levels_per_side;
  EmitStats stats;
  std::string current_path = hourly_path(data_dir);
  fmt::print("TAIL mode: watching {}\ncurrent hour file: {} (l3_cap={})\n",
             data_dir, current_path, l3_max_levels_per_side);

  std::atomic<bool> ws_stop{false};
  std::unique_ptr<ContinuousWS> cws;
  if (bootstrap) {
    fmt::print("\n=== BOOTSTRAP v2 (continuous WS resync) ===\n");
    std::unordered_set<std::string> syms;
    if (filter.only_coin) {
      // --coin shortcut: don't depend on /info for single-coin bootstrap.
      // We already know exactly what we want; one transient /info blip
      // shouldn't be able to prevent us from starting up.
      syms.insert(*filter.only_coin);
      fmt::print("  --coin '{}' given; skipping /info discovery and seeding only that coin\n",
                 *filter.only_coin);
    } else {
      fmt::print("  discovering syms via {} ...\n", info_url);
      syms = discover_bootstrap_syms(info_url, filter.blacklist);
      int xyz_count = 0;
      for (const auto& s : syms) if (s.rfind("xyz:", 0) == 0) ++xyz_count;
      fmt::print("  {} syms to track ({} xyz, {} native)\n",
                 syms.size(), xyz_count, syms.size() - xyz_count);
    }
    cws = std::make_unique<ContinuousWS>(&bookset, std::move(syms),
                                         public_ws_url, verbose, &ws_stop);
    // L1b: wire the gap callback so handle_line can trigger a WS re-catch-up
    // when a forward block-number jump is observed. ContinuousWS outlives the
    // last handle_line call (cws->stop() runs before bookset goes out of
    // scope at function end), so the raw-pointer capture is safe.
    {
      ContinuousWS* cws_raw = cws.get();
      bookset.gap_callback = [cws_raw]() { cws_raw->force_recatch_up(); };
    }
    cws->start();
    fmt::print("  WS thread started; first reseeds will land within a few seconds\n");
    fmt::print("=== entering tail loop ===\n\n");
  }

  // Wait for the current hour file to exist.
  while (!g_stop) {
    std::ifstream check(current_path);
    if (check) break;
    std::this_thread::sleep_for(std::chrono::duration<double>{poll_interval_s});
    std::string np = hourly_path(data_dir);
    if (np != current_path) {
      current_path = np;
      fmt::print("  hour rolled while waiting; now watching {}\n", current_path);
    }
  }
  if (g_stop) {
    if (cws) cws->stop();
    return;
  }

  // L1c: in bootstrap mode, gate startup on the stream's last block_time
  // being within ~3 s of the freshest WS l2Book time. Closes the
  // "publisher + visor restart together" hole where the publisher would
  // otherwise tail mid-replay as if live. Skipped when not in bootstrap
  // (no ContinuousWS to read from) and when --from-start is set (operator
  // explicitly wants the file read from byte 0).
  if (cws && !from_start) {
    constexpr int64_t kL1cFreshnessMs = 3000;
    constexpr int64_t kL1cMaxWaitS = 1200;
    if (!wait_for_bootstrap_stream_fresh(data_dir, *cws,
                                         kL1cFreshnessMs, kL1cMaxWaitS,
                                         verbose)) {
      fmt::print(stderr, "[L1c] freshness gate failed; exiting\n");
      cws->stop();
      return;
    }
    // The current hour file may have rotated while we waited.
    current_path = hourly_path(data_dir);
  }

  std::ifstream f(current_path);
  if (bootstrap || !from_start) f.seekg(0, std::ios::end);
  std::string buf;
  buf.reserve(65536);
  while (!g_stop) {
    char chunk[65536];
    f.clear();
    f.read(chunk, sizeof(chunk));
    std::streamsize got = f.gcount();
    if (got > 0) {
      buf.append(chunk, chunk + got);
      size_t pos = 0, nl;
      while ((nl = buf.find('\n', pos)) != std::string::npos) {
        if (nl > pos) {
          // string_view into buf — no allocation per line.
          handle_line(std::string_view(buf.data() + pos, nl - pos),
                      bookset, pub.get(),
                      filter, print_only, verbose, max_levels, stats);
        }
        pos = nl + 1;
      }
      buf.erase(0, pos);
      continue;
    }
    std::string np = hourly_path(data_dir);
    if (np != current_path) {
      // FINAL DRAIN before rotation — see the matching block in
      // run_tail_fills for the rationale. Without this, raw book-diff
      // events written in the last ~100ms of an hour can be dropped.
      std::this_thread::sleep_for(std::chrono::seconds(1));
      int drained_lines = 0;
      for (int empty_reads = 0; empty_reads < 3 && !g_stop;) {
        f.clear();
        f.read(chunk, sizeof(chunk));
        std::streamsize gtmp = f.gcount();
        if (gtmp > 0) {
          buf.append(chunk, chunk + gtmp);
          size_t pos = 0, nl;
          while ((nl = buf.find('\n', pos)) != std::string::npos) {
            if (nl > pos) {
              handle_line(std::string_view(buf.data() + pos, nl - pos),
                          bookset, pub.get(), filter, print_only, verbose,
                          max_levels, stats);
              ++drained_lines;
            }
            pos = nl + 1;
          }
          buf.erase(0, pos);
          empty_reads = 0;
        } else {
          ++empty_reads;
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
      }
      // The next block boundary that would normally fire emit_coin lives
      // in the new hour file. Without this, the last dirty coins from the
      // closing hour aren't published until something in the new hour
      // touches them — and for thin syms that can mean lost emits.
      flush_dirty(bookset, pub.get(), filter, print_only, verbose, max_levels, stats);
      fmt::print("\n=== rotation: {} -> {} === events={} emitted={} anomalies={} "
                 "l3_trimmed={} duplicates_dropped={} gaps_observed={} small_gaps={} "
                 "(drained {} late lines)\n",
                 current_path, np, bookset.events_seen, stats.emitted, stats.anomalies,
                 bookset.l3_levels_trimmed, bookset.duplicates_dropped,
                 bookset.gaps_observed, bookset.small_gaps_observed, drained_lines);
      if (cws) {
        auto pg = cws->post_gap_price_stats();
        fmt::print("  [post-gap price stats] epochs={} checks={} bad_top_price_events={}\n",
                   pg.epochs, pg.checks, pg.bad_top_price_events);
      }
      current_path = np;
      f.close();
      while (!g_stop) {
        std::ifstream check(current_path);
        if (check) break;
        std::this_thread::sleep_for(std::chrono::duration<double>{poll_interval_s});
      }
      if (g_stop) break;
      f.open(current_path);
      buf.clear();
      continue;
    }
    std::this_thread::sleep_for(std::chrono::duration<double>{poll_interval_s});
  }

  if (cws) cws->stop();
  // Same reason as the rotation flush: SIGINT comes from systemd at restart
  // time, after which there is no next block boundary to ride to publication.
  flush_dirty(bookset, pub.get(), filter, print_only, verbose, max_levels, stats);
  fmt::print("\n=== exit === events={} emitted={} anomalies={} "
             "duplicates_dropped={} gaps_observed={} small_gaps={}\n",
             bookset.events_seen, stats.emitted, stats.anomalies,
             bookset.duplicates_dropped, bookset.gaps_observed, bookset.small_gaps_observed);
  if (cws) {
    auto pg = cws->post_gap_price_stats();
    fmt::print("  [post-gap price stats] epochs={} checks={} bad_top_price_events={}\n",
               pg.epochs, pg.checks, pg.bad_top_price_events);
  }
}

}  // namespace

int main(int argc, char** argv) {
  // Force stdout to line-buffered so log lines flush promptly when redirected
  // to a file (e.g. under systemd). Matches the Python publisher's `-u` flag.
  std::setvbuf(stdout, nullptr, _IOLBF, 0);

  std::string file_path;
  std::string tail_dir;
  std::string tail_fills_dir;
  std::string endpoint = "ipc:///tmp/hl_node_sock";
  std::string only_coin_arg;
  std::string blacklist_file;
  bool print_only = false;
  bool from_start = false;
  bool verbose = false;
  bool log_emit_time = false;
  bool bootstrap = false;
  std::string public_ws_url = kDefaultPublicWsUrl;
  std::string info_url = kDefaultInfoUrl;
  double poll_interval = 0.1;
  int32_t max_levels = 40;
  int32_t l3_max_levels_per_side = 100;

  CLI::App cmd{"hl_node_publisher — C++ HL non-validating node publisher"};
  auto* g_src = cmd.add_option_group("source");
  g_src->add_option("--file", file_path,
                    "Single JSONL file of book diffs; read end-to-end then exit");
  g_src->add_option("--tail-dir", tail_dir,
                    "Hourly book-diff dir to tail "
                    "(e.g. ~/hl/data/node_raw_book_diffs_streaming/hourly)");
  g_src->add_option("--tail-fills-dir", tail_fills_dir,
                    "Hourly fills dir to tail "
                    "(e.g. ~/hl/data/node_fills_streaming/hourly). "
                    "Pairs fills by tid and emits PbTrade with passive_was_buyer.");
  g_src->require_option(1);

  cmd.add_option("--endpoint", endpoint, "ZMQ PUB endpoint");
  cmd.add_option("--coin", only_coin_arg, "Optionally focus on one coin");
  cmd.add_option("--coins-blacklist-file", blacklist_file,
                 "Path to a file with one coin name per line; skipped entirely");
  cmd.add_flag("--print-only", print_only, "Don't publish, just print summaries");
  cmd.add_flag("--from-start", from_start, "Tail mode: read current hour file from start");
  cmd.add_option("--poll-interval", poll_interval,
                 "Tail mode: seconds between polls when no new data");
  cmd.add_flag("--bootstrap", bootstrap,
               "Tail mode: discover syms via /info, run ContinuousWS catch-up");
  cmd.add_option("--public-ws-url", public_ws_url, "HL public WS URL");
  cmd.add_option("--info-url", info_url, "HL /info HTTP URL");
  cmd.add_flag("-v,--verbose", verbose);
  cmd.add_flag("--log-emit-time", log_emit_time,
               "Fills-mode diag: print 'EMIT_DIAG tid=N trade_time=T read_ms=A "
               "emit_ms=B dur_ms=(B-A)' per emitted PbTrade. Measures the "
               "publisher's read-to-PUB-send delay. Off by default.");
  cmd.add_option("--max-levels", max_levels,
                 "Max book levels per side to publish (default 40). "
                 "Use 0 or negative to emit all levels.");
  cmd.add_option("--l3-max-levels-per-side", l3_max_levels_per_side,
                 "Cap on L3 book depth per side; deeper levels are dropped "
                 "at emit time to bound RSS. Default 100 (well above --max-levels). "
                 "Set to 0 to disable the cap (legacy behavior).");

  // Debug loggers — all opt-in, all silent by default.
  std::string trace_pxs_arg, trace_out_path;
  std::string watch_pxs_arg, watch_out_path;
  bool log_rule1 = false;
  std::string log_rule1_path;
  cmd.add_option("--trace-pxs", trace_pxs_arg,
                 "Comma-sep list of <side>:<px> (e.g. 'B:28411,A:28412') to log "
                 "every L3 state mutation on.");
  cmd.add_option("--trace-out", trace_out_path,
                 "File for --trace-pxs output (default stderr).");
  cmd.add_option("--watch-pxs", watch_pxs_arg,
                 "Comma-sep list of <side>:<px> to dump real+synth+emit state "
                 "at every block-boundary emit.");
  cmd.add_option("--watch-out", watch_out_path,
                 "File for --watch-pxs output (default stderr).");
  cmd.add_flag("--log-rule1", log_rule1,
               "Log every Rule 1 skip in reseed_synth_from_ws with its "
               "would_lock/would_cross/would_inside_spread class.");
  cmd.add_option("--log-rule1-out", log_rule1_path,
                 "File for --log-rule1 output (default stderr).");

  PARSE(cmd, argc, argv);

  auto parse_pxs = [](const std::string& s,
                      std::set<std::pair<std::string, std::string>>* out) {
    std::stringstream ss(s);
    std::string tok;
    while (std::getline(ss, tok, ',')) {
      // strip whitespace
      auto a = tok.find_first_not_of(" \t");
      auto b = tok.find_last_not_of(" \t");
      if (a == std::string::npos) continue;
      tok = tok.substr(a, b - a + 1);
      auto colon = tok.find(':');
      if (colon == std::string::npos) continue;
      std::string side = tok.substr(0, colon);
      std::string px = tok.substr(colon + 1);
      if (side != "B" && side != "A") continue;
      out->emplace(std::move(side), std::move(px));
    }
  };
  // These ofstreams have to outlive main(); allocate on the heap and leak —
  // process exit cleans up, and the file gets flushed on each write anyway.
  if (!trace_pxs_arg.empty()) {
    parse_pxs(trace_pxs_arg, &g_trace.pxs);
    g_trace.os = trace_out_path.empty()
        ? &std::cerr
        : new std::ofstream(trace_out_path);
    fmt::print("L3 op trace on for {} pxs -> {}\n", g_trace.pxs.size(),
               trace_out_path.empty() ? "(stderr)" : trace_out_path);
  }
  if (!watch_pxs_arg.empty()) {
    parse_pxs(watch_pxs_arg, &g_watch.pxs);
    g_watch.os = watch_out_path.empty()
        ? &std::cerr
        : new std::ofstream(watch_out_path);
    fmt::print("Per-emit watch on for {} pxs -> {}\n", g_watch.pxs.size(),
               watch_out_path.empty() ? "(stderr)" : watch_out_path);
  }
  if (log_rule1) {
    g_rule1.os = log_rule1_path.empty()
        ? &std::cerr
        : new std::ofstream(log_rule1_path);
    fmt::print("Rule 1 skip log on -> {}\n",
               log_rule1_path.empty() ? "(stderr)" : log_rule1_path);
  }

  CoinFilter filter;
  if (!only_coin_arg.empty()) filter.only_coin = only_coin_arg;
  if (!blacklist_file.empty()) {
    std::ifstream bf(blacklist_file);
    std::string l;
    while (std::getline(bf, l)) {
      size_t a = l.find_first_not_of(" \t\r\n");
      size_t b = l.find_last_not_of(" \t\r\n");
      if (a == std::string::npos) continue;
      std::string s = l.substr(a, b - a + 1);
      if (s.empty() || s[0] == '#') continue;
      filter.blacklist.insert(s);
    }
    fmt::print("loaded {} coins from blacklist {}\n",
               filter.blacklist.size(), blacklist_file);
  }

  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);

  ix::initNetSystem();

  if (!file_path.empty()) {
    run_file(file_path, endpoint, filter, print_only, verbose, max_levels,
             l3_max_levels_per_side);
  } else if (!tail_fills_dir.empty()) {
    run_tail_fills(tail_fills_dir, endpoint, filter, print_only, verbose,
                   from_start, poll_interval, log_emit_time);
  } else {
    run_tail(tail_dir, endpoint, filter, print_only, verbose, from_start,
             poll_interval, bootstrap, public_ws_url, info_url, max_levels,
             l3_max_levels_per_side);
  }
  ix::uninitNetSystem();
  return 0;
}
