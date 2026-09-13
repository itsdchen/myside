// Wire formats for the pkrelay family of UDP feeds.
//
// pkrelay ingests market data on a short path near the source (e.g.
// databento CME gateway in us-east-2) and ships each record to
// pkmultifeed consumers on a long path (e.g. Tokyo) as a single
// fixed-size UDP datagram. Both endpoints run x86_64 Linux, so fields
// are written in host (little-endian) byte order with no byteswapping.
// No explicit packing: each struct is laid out with natural 8-byte
// alignment and hand-arranged to have no internal padding, so sizeof()
// is stable across compilers.
//
// All pkrelay packet structs begin with the same leading fields
// (magic, version, type, ...) so receivers can peek at the type tag
// before dispatching to the right struct interpretation. Currently
// only one packet type exists (DatabentoCmeMbpPacket) but the shape
// accommodates adding more (e.g. DatabentoEqMbpPacket, NyseL2Packet)
// without breaking existing consumers.
//
// Bump kPkRelayVersion on any field-level change to an existing
// packet struct; consumers reject packets whose magic or version
// doesn't match. Adding a new packet type adds a new PacketType enum
// value and a new struct — existing readers see an unknown type and
// drop, which is the correct behaviour.

#pragma once

#include <cstdint>
#include <type_traits>

namespace pktrade::feedrelay {

// 'P','K','R','L' in little-endian. First byte on the wire is 'P'.
inline constexpr uint32_t kPkRelayMagic = 0x4C524B50;
inline constexpr uint16_t kPkRelayVersion = 1;

// Max UDP datagram size. The packet itself is far smaller; this is a
// sanity bound callers can use when sizing receive buffers.
inline constexpr uint32_t kPkRelayMaxDatagramBytes = 1400;

enum class PacketType : uint16_t {
  // Quote: a databento-CME MBP-1 top-of-book update. Carried in a
  // DatabentoCmeMbpPacket. Keeping the name generic (rather than
  // "DatabentoCmeMbp") for now so it matches the future
  // consolidated "quote" notion; if a second quote-shaped type
  // gets added, this split becomes clearer.
  Quote = 0,
  // Heartbeat: body fields should be ignored. Emitted periodically by
  // pkrelay even when no market data flows, so consumers can tell
  // "relay up but market is quiet" from "relay / network is down".
  Heartbeat = 1,
};

// Symbol names from databento (e.g. "NQM6", "HGN6") fit easily; 16
// bytes leaves room for longer dated contracts. Null-padded, not
// necessarily null-terminated when the symbol is exactly 16 chars.
inline constexpr std::size_t kPkRelaySymBytes = 16;

// Databento CME MBP-1 top-of-book quote (or heartbeat when
// type == PacketType::Heartbeat, in which case the quote fields are
// zero and should be ignored).
struct DatabentoCmeMbpPacket {
  uint32_t magic;                   // kPkRelayMagic
  uint16_t version;                 // kPkRelayVersion
  uint16_t type;                    // PacketType stored as uint16_t

  char     db_sym[kPkRelaySymBytes]; // databento raw symbol, null-padded

  uint64_t seq;                     // monotonic per relay instance; gaps
                                    // on the consumer side signal drops
  int64_t  ts_send_ms;              // relay wall clock (ms since epoch)
                                    // at sendto(); for transit latency
                                    // diagnostics

  // Quote payload. Ignored for Heartbeat packets (fields may be zero).
  int64_t  ts_event_ms;             // CME event time (ms since epoch)
  int64_t  ts_recv_ms;              // databento gateway receive time
  int64_t  bid_px_1e9;              // databento's native fixed-point:
  int64_t  ask_px_1e9;              //   double_price = raw * 1e-9
  int32_t  bid_sz;
  int32_t  ask_sz;
  uint32_t flags;                   // pass-through of mbp1 flag bits
                                    // (IsMaybeBadBook, IsPublisherSpecific, ...)
  uint32_t reserved;                // must be zero; reserved for future use
};

static_assert(std::is_trivially_copyable_v<DatabentoCmeMbpPacket>,
              "DatabentoCmeMbpPacket must be trivially copyable for memcpy on the wire");
static_assert(std::is_standard_layout_v<DatabentoCmeMbpPacket>,
              "DatabentoCmeMbpPacket must be standard layout so member offsets are stable");
// Stable wire size. If this assertion ever trips, bump kPkRelayVersion and
// update readers / writers rather than silently changing layout.
static_assert(sizeof(DatabentoCmeMbpPacket) == 88,
              "DatabentoCmeMbpPacket wire size must be 88 bytes");

}  // namespace pktrade::feedrelay
