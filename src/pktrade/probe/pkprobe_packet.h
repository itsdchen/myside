// Wire format for the pkprobe path-clustering experiment.
//
// pkprobe_send (us-east-2) ships fixed-size 88-byte UDP datagrams to
// pkprobe_recv (Tokyo) on N lanes, where each lane is a distinct
// destination port. Receiver logs per-packet one-way times for
// post-analysis of ECMP path clusters across the AWS backbone.
//
// 88 bytes matches DatabentoCmeMbpPacket so any path effect that
// depends on datagram size is the same as production. Layout is host
// (little-endian) byte order with no byteswapping; both endpoints are
// x86_64 Linux.
//
// See EXPERIMENT.md in this directory for the design rationale.

#pragma once

#include <cstdint>
#include <type_traits>

namespace pktrade::probe {

// 'P','A','T','H' in little-endian. First byte on the wire is 'P'.
inline constexpr uint32_t kPkProbeMagic = 0x48544150;
inline constexpr uint16_t kPkProbeVersion = 1;

// Match DatabentoCmeMbpPacket so the probe datagram is the same size
// as production's relay datagram.
inline constexpr std::size_t kPkProbePacketBytes = 88;

struct PkProbePacket {
  uint32_t magic;       // kPkProbeMagic
  uint16_t version;     // kPkProbeVersion
  uint16_t lane_id;     // 0 .. num_lanes - 1
  uint64_t seq;         // monotonic per-lane on the sender
  int64_t  send_ts_ns;  // CLOCK_REALTIME at sendto()
  uint8_t  pad[kPkProbePacketBytes - 24]; // zero-fill to 88 bytes
};

static_assert(std::is_trivially_copyable_v<PkProbePacket>,
              "PkProbePacket must be trivially copyable for memcpy on the wire");
static_assert(std::is_standard_layout_v<PkProbePacket>,
              "PkProbePacket must be standard layout so member offsets are stable");
static_assert(sizeof(PkProbePacket) == kPkProbePacketBytes,
              "PkProbePacket wire size must equal kPkProbePacketBytes");

} // namespace pktrade::probe
