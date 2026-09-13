#!/usr/bin/env python3
"""Summarize Hyperliquid hl-node peer behavior from hl-visor journal text.

Usage:
  ssh n1 'sudo journalctl -u hl-visor.service --since "2026-06-23 00:00 UTC" -o short-iso' \
    | python3 overmind/studies/hl_feed/hl_peer_score_report.py

This is intentionally local/read-only. It parses logs emitted by
hl-visor/hl-node and infers peer quality from attempts, accepts,
Peer-full rejections, reader failures, and rough active intervals.
"""

from __future__ import annotations

import argparse
import collections
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime


IP_RE = r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})"


@dataclass
class PeerStats:
    attempts: int = 0
    tcp_connected: int = 0
    greeting: int = 0
    success: int = 0
    success_send_abci_true: int = 0
    success_send_abci_false: int = 0
    peer_full: int = 0
    reader_errors: int = 0
    early_eof: int = 0
    deadline: int = 0
    rocksdb_reads: list[float] = field(default_factory=list)
    active_seconds: float = 0.0


def parse_ts(line: str) -> datetime | None:
    """Parse journalctl -o short-iso timestamp as naive UTC/local wall time."""
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1))
    except ValueError:
        return None


def bump(stats: dict[str, PeerStats], ip: str, attr: str, amount: int = 1) -> None:
    setattr(stats[ip], attr, getattr(stats[ip], attr) + amount)


def parse(lines: list[str]) -> dict[str, PeerStats]:
    stats: dict[str, PeerStats] = collections.defaultdict(PeerStats)
    active_peer: tuple[str, datetime] | None = None

    def close_active(end_ts: datetime | None) -> None:
        nonlocal active_peer
        if not active_peer or end_ts is None:
            return
        ip, start_ts = active_peer
        if end_ts > start_ts:
            stats[ip].active_seconds += (end_ts - start_ts).total_seconds()
        active_peer = None

    for line in lines:
        ts = parse_ts(line)

        if match := re.search(r"connecting to peer: Ip\(" + IP_RE + r"\)", line):
            bump(stats, match.group(1), "attempts")

        if match := re.search(r"connected to abci stream from " + IP_RE + r":4001", line):
            bump(stats, match.group(1), "tcp_connected")

        if match := re.search(r"received abci greeting from " + IP_RE + r":4001", line):
            bump(stats, match.group(1), "greeting")

        if match := re.search(r"error connecting to candidate peer Ip\(" + IP_RE + r"\).*: Peer full", line):
            bump(stats, match.group(1), "peer_full")

        if match := re.search(
            r"successfully received greeting from peer, send_abci=(true|false) Ip\(" + IP_RE + r"\)",
            line,
        ):
            send_abci, ip = match.group(1), match.group(2)
            bump(stats, ip, "success")
            bump(stats, ip, f"success_send_abci_{send_abci}")
            if ts is not None:
                close_active(ts)
                active_peer = (ip, ts)

        if match := re.search(r"client_block_reader error.*peer_addr\(\): Ok\(" + IP_RE + r":4001\)", line):
            bump(stats, match.group(1), "reader_errors")

        if match := re.search(r"early eof.*peer_addr\(\): Ok\(" + IP_RE + r":4001\)", line):
            bump(stats, match.group(1), "early_eof")

        if match := re.search(r"deadline elapsed.*peer_addr\(\): Ok\(" + IP_RE + r":4001\)", line):
            bump(stats, match.group(1), "deadline")

        if match := re.search(
            r"received rocksdb from tcp stream.*peer_addr\(\): Ok\("
            + IP_RE
            + r":4001\).*total_read_duration: Duration\(([0-9.]+)\)",
            line,
        ):
            stats[match.group(1)].rocksdb_reads.append(float(match.group(2)))

    # Use the timestamp on the last journal line as the active interval end.
    last_ts = next((parse_ts(line) for line in reversed(lines) if parse_ts(line)), None)
    close_active(last_ts)
    return stats


def fmt_num(value: float) -> str:
    return f"{value:.1f}"


def print_report(stats: dict[str, PeerStats], min_mentions: int) -> None:
    rows = []
    for ip, s in stats.items():
        mentions = (
            s.attempts
            + s.tcp_connected
            + s.greeting
            + s.success
            + s.peer_full
            + s.reader_errors
            + s.early_eof
            + s.deadline
            + len(s.rocksdb_reads)
        )
        if mentions < min_mentions:
            continue
        rocksdb_med = statistics.median(s.rocksdb_reads) if s.rocksdb_reads else 0.0
        rows.append(
            (
                s.active_seconds,
                s.success,
                -s.peer_full,
                -s.reader_errors,
                ip,
                s,
                rocksdb_med,
            )
        )

    rows.sort(reverse=True)
    print(
        "peer attempts tcp_conn greeting success send_abci_true send_abci_false "
        "peer_full reader_err early_eof deadline active_min rocksdb_n rocksdb_med_s"
    )
    for _, __, ___, ____, ip, s, rocksdb_med in rows:
        print(
            ip,
            s.attempts,
            s.tcp_connected,
            s.greeting,
            s.success,
            s.success_send_abci_true,
            s.success_send_abci_false,
            s.peer_full,
            s.reader_errors,
            s.early_eof,
            s.deadline,
            fmt_num(s.active_seconds / 60.0),
            len(s.rocksdb_reads),
            f"{rocksdb_med:.3f}",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-mentions", type=int, default=1)
    args = parser.parse_args()
    lines = sys.stdin.read().splitlines()
    if not lines:
        print("no input lines", file=sys.stderr)
        return 1
    print_report(parse(lines), args.min_mentions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
