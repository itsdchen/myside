#! /usr/bin/env python3

"""
Side-by-side comparator: HL public WS l2Book vs our node-feed publisher.

One coin per run. Subscribes concurrently to:
  - HL public WS l2Book (slow / 20-level) for the requested coin
  - Our publisher's ZMQ PUB socket (parsed PbMessage with book_snapshot)

Writes two JSONL files (one event per line) for offline diff:
  - public_ws_{coin}_{utc_ts}.jsonl    — raw WS l2Book messages
  - node_pub_{coin}_{utc_ts}.jsonl     — node publisher emissions, protobuf
                                         deserialized to JSON

Logs live divergence stats to stdout every --report-interval seconds:
  - comparison count
  - top-bid match %, top-ask match %  (exact string equality)
  - count of crossed books seen on each side
  - count of cases where each side was "ahead" (more recent timestamp)
  - average level counts per side

Run on n1 where both the publisher's ZMQ socket and the public WS are reachable.

Typical use:
  # Term 1 — publisher (must be in non-print-only mode)
  ~/.venvs/v1/bin/python ~/hl_node_publish.py \\
      --tail-dir ~/hl/data/node_raw_book_diffs_streaming/hourly \\
      --bootstrap \\
      --endpoint ipc:///tmp/hl_node_sock

  # Term 2 — comparator
  ~/.venvs/v1/bin/python ~/hl_node_vs_ws_compare.py \\
      --coin xyz:XYZ100 \\
      --zmq-endpoint ipc:///tmp/hl_node_sock \\
      --output-dir /tmp/hl_compare
"""

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from typing import Optional, Tuple

import websockets
import zmq
import zmq.asyncio

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pyfeed"))
import mdmsg_pb2


def make_l2book_sub(coin: str) -> dict:
    sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
    if ":" in coin:
        sub["subscription"]["dex"] = coin.split(":", 1)[0]
    return sub


class Comparator:
    """Holds latest snapshot from each side; recomputes diff stats on each update."""

    def __init__(self, coin: str, ws_jsonl_path: str, node_jsonl_path: str) -> None:
        self.coin = coin
        self.ws_f = open(ws_jsonl_path, "w")
        self.node_f = open(node_jsonl_path, "w")
        # State: (timestamp_ms, bids, asks). bids/asks are lists of dicts with
        # at least 'px' and 'sz' keys (matching the WS / our JSON shape).
        self.latest_ws: Optional[Tuple[int, list, list]] = None
        self.latest_node: Optional[Tuple[int, list, list]] = None
        self.stats = {
            "ws_msgs": 0,
            "node_msgs": 0,
            "comparisons": 0,
            "top_bid_match": 0,
            "top_ask_match": 0,
            "node_crossed": 0,
            "ws_crossed": 0,
            "node_ahead_count": 0,
            "ws_ahead_count": 0,
            "node_levels_total": 0,
            "ws_levels_total": 0,
            "both_one_sided": 0,
        }
        # Time-diff samples (ms). Populated whenever we have both an
        # event_time (block_time of last applied block on node side) and
        # a WS data.time (HL server's "this snapshot's as-of time").
        # diff = node_event_time - ws_time, so:
        #   > 0  => node's last block is MORE RECENT than the WS snapshot
        #   < 0  => WS snapshot is more recent than node's last block
        # If our path is faster end-to-end, distribution should be biased >0.
        self.event_vs_ws_time_diffs_ms: list = []
        # WS-side network latency: when the snapshot was generated on HL's
        # server vs when we received it.
        self.ws_path_latency_ms: list = []
        # Node-side delivery latency: from chain block_time to ZMQ recv.
        self.node_path_latency_ms: list = []

    async def public_ws_loop(self, ws_url: str) -> None:
        sub = make_l2book_sub(self.coin)
        async with websockets.connect(ws_url, ping_interval=None) as ws:
            await ws.send(json.dumps(sub))
            print(f"  [public ws] subscribed: {sub}", flush=True)
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                ch = msg.get("channel")
                if ch == "subscriptionResponse":
                    print(f"  [public ws] subResp: {msg['data']}", flush=True)
                    continue
                if ch == "error":
                    print(f"  [public ws] error: {msg.get('data')}", flush=True)
                    continue
                if ch != "l2Book":
                    continue
                data = msg.get("data") or {}
                if data.get("coin") != self.coin:
                    continue
                recv_wall = int(time.time() * 1000)
                rec = {"recv_wall_ms": recv_wall, **msg}
                self.ws_f.write(json.dumps(rec) + "\n")
                self.ws_f.flush()
                self.stats["ws_msgs"] += 1
                ws_time = int(data["time"])
                self.ws_path_latency_ms.append(recv_wall - ws_time)
                self.latest_ws = (ws_time, data["levels"][0], data["levels"][1])
                self.compare_now()

    async def node_zmq_loop(self, zmq_endpoint: str) -> None:
        ctx = zmq.asyncio.Context()
        sub = ctx.socket(zmq.SUB)
        sub.connect(zmq_endpoint)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        print(f"  [node zmq] SUB connected to {zmq_endpoint}", flush=True)
        while True:
            raw_bytes = await sub.recv()
            try:
                pb = mdmsg_pb2.PbMessage()
                pb.ParseFromString(raw_bytes)
            except Exception as e:
                continue
            if pb.symbol_id != self.coin:
                continue
            if not pb.HasField("book_snapshot"):
                continue
            self.stats["node_msgs"] += 1
            snap = pb.book_snapshot
            bids = [{"px": b.px, "sz": b.qty, "n": b.numords} for b in snap.bids]
            asks = [{"px": a.px, "sz": a.qty, "n": a.numords} for a in snap.asks]
            now_ms = int(time.time() * 1000)
            # Persist parsed node emission
            rec = {
                "recv_wall_ms": now_ms,
                "symbol_id": pb.symbol_id,
                "market": pb.market,
                "rx_timestamp": pb.rx_timestamp,
                "event_time": snap.event_time if snap.event_time else None,
                "last_update_id": snap.last_update_id,
                "bids": bids,
                "asks": asks,
            }
            self.node_f.write(json.dumps(rec) + "\n")
            self.node_f.flush()
            if snap.event_time:
                self.node_path_latency_ms.append(now_ms - snap.event_time)
                tnode = snap.event_time
            else:
                tnode = now_ms
            self.latest_node = (tnode, bids, asks)
            self.compare_now()

    def compare_now(self) -> None:
        if self.latest_ws is None or self.latest_node is None:
            return
        ws_t, ws_b, ws_a = self.latest_ws
        node_t, node_b, node_a = self.latest_node
        self.stats["comparisons"] += 1
        self.stats["ws_levels_total"] += len(ws_b) + len(ws_a)
        self.stats["node_levels_total"] += len(node_b) + len(node_a)

        if ws_b and node_b and ws_b[0]["px"] == node_b[0]["px"]:
            self.stats["top_bid_match"] += 1
        if ws_a and node_a and ws_a[0]["px"] == node_a[0]["px"]:
            self.stats["top_ask_match"] += 1

        if not (ws_b and ws_a) and not (node_b and node_a):
            self.stats["both_one_sided"] += 1
        if ws_b and ws_a and float(ws_b[0]["px"]) >= float(ws_a[0]["px"]):
            self.stats["ws_crossed"] += 1
        if node_b and node_a and float(node_b[0]["px"]) >= float(node_a[0]["px"]):
            self.stats["node_crossed"] += 1

        if node_t > ws_t:
            self.stats["node_ahead_count"] += 1
        elif ws_t > node_t:
            self.stats["ws_ahead_count"] += 1
        # If both have HL-side timestamps (node's event_time and ws's data.time),
        # record the diff. Skip if node fell back to wall-clock (event_time=0).
        if node_t > 1_000_000_000_000:  # millisecond ts > year 2001
            self.event_vs_ws_time_diffs_ms.append(node_t - ws_t)

    @staticmethod
    def _percentiles(data: list, ps=(50, 95)) -> dict:
        if not data:
            return {f"p{p}": None for p in ps}
        s = sorted(data)
        out = {}
        for p in ps:
            idx = max(0, min(len(s) - 1, int(p / 100 * len(s))))
            out[f"p{p}"] = s[idx]
        return out

    async def report_loop(self, interval_s: float) -> None:
        await asyncio.sleep(interval_s)
        while True:
            s = self.stats
            n = s["comparisons"]
            if n == 0:
                print(f"[{self.coin}] no comparisons yet "
                      f"(ws_msgs={s['ws_msgs']}, node_msgs={s['node_msgs']})",
                      flush=True)
            else:
                avg_node_lvls = s["node_levels_total"] / n
                avg_ws_lvls = s["ws_levels_total"] / n
                print(
                    f"[{self.coin}] n={n} "
                    f"top_bid_match={100*s['top_bid_match']/n:.1f}% "
                    f"top_ask_match={100*s['top_ask_match']/n:.1f}% "
                    f"crossed node/ws={s['node_crossed']}/{s['ws_crossed']} "
                    f"avg_lvls node/ws={avg_node_lvls:.1f}/{avg_ws_lvls:.1f}",
                    flush=True,
                )
                # Time-diff stats
                diffs = self.event_vs_ws_time_diffs_ms
                if diffs:
                    pcs = self._percentiles(diffs, ps=(5, 50, 95))
                    mean = sum(diffs) / len(diffs)
                    node_ahead = sum(1 for d in diffs if d > 0)
                    print(
                        f"  time-diff (node_block_time - ws_time, ms) n={len(diffs)}: "
                        f"mean={mean:+.0f} p5={pcs['p5']:+.0f} p50={pcs['p50']:+.0f} p95={pcs['p95']:+.0f} "
                        f" node-ahead-of-ws: {100*node_ahead/len(diffs):.0f}%",
                        flush=True,
                    )
                if self.ws_path_latency_ms:
                    pcs = self._percentiles(self.ws_path_latency_ms, ps=(50, 95))
                    print(
                        f"  ws path latency (recv - ws.time, ms): "
                        f"p50={pcs['p50']} p95={pcs['p95']} n={len(self.ws_path_latency_ms)}",
                        flush=True,
                    )
                if self.node_path_latency_ms:
                    pcs = self._percentiles(self.node_path_latency_ms, ps=(50, 95))
                    print(
                        f"  node path latency (recv - block_time, ms): "
                        f"p50={pcs['p50']} p95={pcs['p95']} n={len(self.node_path_latency_ms)}",
                        flush=True,
                    )
            await asyncio.sleep(interval_s)

    async def run(self, ws_url: str, zmq_endpoint: str, report_interval_s: float) -> None:
        try:
            await asyncio.gather(
                self.public_ws_loop(ws_url),
                self.node_zmq_loop(zmq_endpoint),
                self.report_loop(report_interval_s),
            )
        finally:
            self.ws_f.close()
            self.node_f.close()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--coin", required=True, help="e.g. xyz:XYZ100 or BTC")
    p.add_argument("--ws-url", default="wss://api.hyperliquid.xyz/ws")
    p.add_argument("--zmq-endpoint", required=True,
                   help="publisher's ZMQ PUB endpoint, e.g. ipc:///tmp/hl_node_sock")
    p.add_argument("--output-dir", default="/tmp/hl_compare")
    p.add_argument("--report-interval", type=float, default=10.0)
    args = p.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    coin_safe = args.coin.replace(":", "_").replace("/", "_")
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    ws_path = os.path.join(args.output_dir, f"public_ws_{coin_safe}_{ts}.jsonl")
    node_path = os.path.join(args.output_dir, f"node_pub_{coin_safe}_{ts}.jsonl")
    print(f"Writing WS  -> {ws_path}")
    print(f"Writing node -> {node_path}")

    c = Comparator(args.coin, ws_path, node_path)
    try:
        await c.run(args.ws_url, args.zmq_endpoint, args.report_interval)
    except KeyboardInterrupt:
        print("\n=== interrupted ===")
        s = c.stats
        for k, v in s.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
