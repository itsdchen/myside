#! /usr/bin/env python3

"""
Clean head-to-head latency measurement: HL public WS trades vs our node-feed
fills publisher (which emits PbTrade keyed by tid).

For each trade tid that arrives in both streams, computes:
    delta_ms = node_recv_wall - ws_recv_wall
    > 0  =>  WS delivered first; node was slower by delta ms
    < 0  =>  node delivered first; WS was slower by |delta| ms

Pairs by trade tid. Both streams report tid on the same trade event so
matching is unambiguous.

Run on n1 alongside a publisher in fills mode:
  # Term 1
  ~/.venvs/v1/bin/python ~/hl_node_publish.py \\
      --tail-fills-dir ~/hl/data/node_fills_streaming/hourly \\
      --endpoint ipc:///tmp/hl_node_fills_sock

  # Term 2
  ~/.venvs/v1/bin/python ~/hl_trade_latency_compare.py \\
      --coin BTC --zmq-endpoint ipc:///tmp/hl_node_fills_sock
"""

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from typing import Dict, Optional

import websockets
import zmq
import zmq.asyncio

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pyfeed"))
import mdmsg_pb2


def make_trades_sub(coin: str) -> dict:
    sub = {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}
    if ":" in coin:
        sub["subscription"]["dex"] = coin.split(":", 1)[0]
    return sub


class PerCoin:
    """All measurement state for one coin."""

    def __init__(self, coin: str, deltas_f) -> None:
        self.coin = coin
        self.deltas_f = deltas_f
        self.pending: Dict[int, tuple] = {}   # tid -> (recv_wall_ms, source)
        self.deltas_ms: list = []
        self.stats = {
            "ws_trades": 0,
            "node_trades": 0,
            "paired": 0,
            "ws_first": 0,
            "node_first": 0,
            "ttl_drops": 0,
            "duplicate_arrival": 0,
        }


class TradeLatencyCompare:
    """Tracks per-tid first-arrival times from each source per coin; computes
    delta on second arrival. Handles multiple coins simultaneously on a single
    WS connection."""

    PAIR_TTL_S = 30.0

    def __init__(self, coins: list, log_dir: str) -> None:
        self.coins = list(coins)
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        self.coin_state: Dict[str, PerCoin] = {}
        for c in self.coins:
            coin_safe = c.replace(":", "_")
            path = os.path.join(log_dir, f"trade_deltas_{coin_safe}_{ts}.jsonl")
            f = open(path, "w")
            self.coin_state[c] = PerCoin(c, f)
            print(f"writing {c} -> {path}")

    def _record_arrival(self, coin: str, tid: int, source: str,
                        recv_wall_ms: int, trade_info: Optional[dict] = None) -> None:
        cs = self.coin_state.get(coin)
        if cs is None:
            return
        if tid in cs.pending:
            other_t, other_source = cs.pending.pop(tid)
            if other_source == source:
                cs.stats["duplicate_arrival"] += 1
                cs.pending[tid] = (min(other_t, recv_wall_ms), source)
                return
            if source == "node":
                ws_t, node_t = other_t, recv_wall_ms
            else:
                ws_t, node_t = recv_wall_ms, other_t
            delta = node_t - ws_t
            cs.deltas_ms.append(delta)
            cs.stats["paired"] += 1
            if delta > 0:
                cs.stats["ws_first"] += 1
            elif delta < 0:
                cs.stats["node_first"] += 1
            cs.deltas_f.write(json.dumps({
                "tid": tid,
                "ws_recv_ms": ws_t,
                "node_recv_ms": node_t,
                "delta_node_minus_ws_ms": delta,
                "info": trade_info or {},
            }) + "\n")
            cs.deltas_f.flush()
        else:
            cs.pending[tid] = (recv_wall_ms, source)

    def _flush_stale_pending(self) -> None:
        cutoff = time.time() - self.PAIR_TTL_S
        cutoff_ms = int(cutoff * 1000)
        for cs in self.coin_state.values():
            stale = [tid for tid, (t, _) in cs.pending.items() if t < cutoff_ms]
            for tid in stale:
                cs.pending.pop(tid, None)
                cs.stats["ttl_drops"] += 1

    async def public_ws_loop(self, ws_url: str) -> None:
        async with websockets.connect(ws_url, ping_interval=None) as ws:
            for coin in self.coins:
                await ws.send(json.dumps(make_trades_sub(coin)))
            print(f"  [ws] subscribed to {len(self.coins)} coins", flush=True)
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                ch = msg.get("channel")
                if ch == "subscriptionResponse":
                    continue
                if ch == "error":
                    print(f"  [ws] error: {msg.get('data')}", flush=True)
                    continue
                if ch != "trades":
                    continue
                recv_wall = int(time.time() * 1000)
                for trd in msg.get("data", []):
                    coin = trd.get("coin")
                    if coin not in self.coin_state:
                        continue
                    tid = trd.get("tid")
                    if tid is None:
                        continue
                    self.coin_state[coin].stats["ws_trades"] += 1
                    self._record_arrival(coin, int(tid), "ws", recv_wall,
                                         trade_info={
                                             "px": trd.get("px"), "sz": trd.get("sz"),
                                             "side": trd.get("side"),
                                         })

    async def node_zmq_loop(self, zmq_endpoint: str) -> None:
        ctx = zmq.asyncio.Context()
        sub = ctx.socket(zmq.SUB)
        sub.connect(zmq_endpoint)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        print(f"  [node zmq] SUB connected to {zmq_endpoint}", flush=True)
        while True:
            raw_bytes = await sub.recv()
            recv_wall = int(time.time() * 1000)
            try:
                pb = mdmsg_pb2.PbMessage()
                pb.ParseFromString(raw_bytes)
            except Exception:
                continue
            coin = pb.symbol_id
            if coin not in self.coin_state:
                continue
            if not pb.HasField("book_trade"):
                continue
            tid = pb.book_trade.trade_id
            if tid <= 0:
                continue
            self.coin_state[coin].stats["node_trades"] += 1
            self._record_arrival(coin, int(tid), "node", recv_wall)

    @staticmethod
    def _percentiles(data: list, ps=(5, 25, 50, 75, 95)) -> dict:
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
            self._flush_stale_pending()
            print(f"\n=== {datetime.datetime.utcnow().strftime('%H:%M:%S')} UTC ===",
                  flush=True)
            for coin in self.coins:
                cs = self.coin_state[coin]
                s = cs.stats
                if s["paired"] == 0:
                    print(f"  {coin:14s}  warming up — ws={s['ws_trades']} "
                          f"node={s['node_trades']} pending={len(cs.pending)}",
                          flush=True)
                    continue
                pcs = self._percentiles(cs.deltas_ms)
                mean = sum(cs.deltas_ms) / len(cs.deltas_ms)
                pct_node = 100 * s["node_first"] / s["paired"]
                print(f"  {coin:14s}  n={s['paired']:5d} "
                      f"delta(node-ws,ms) mean={mean:+5.0f} "
                      f"p5={pcs['p5']:+5.0f} p50={pcs['p50']:+5.0f} p95={pcs['p95']:+5.0f}  "
                      f"node-first={pct_node:.0f}%  "
                      f"ws={s['ws_trades']} node={s['node_trades']} ttl={s['ttl_drops']}",
                      flush=True)
            await asyncio.sleep(interval_s)

    async def run(self, ws_url: str, zmq_endpoint: str, report_interval_s: float) -> None:
        try:
            await asyncio.gather(
                self.public_ws_loop(ws_url),
                self.node_zmq_loop(zmq_endpoint),
                self.report_loop(report_interval_s),
            )
        finally:
            for cs in self.coin_state.values():
                cs.deltas_f.close()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--coins", required=True,
                   help="comma-separated coins, e.g. 'BTC,ETH,HYPE,xyz:XYZ100,xyz:SP500'")
    p.add_argument("--ws-url", default="wss://api.hyperliquid.xyz/ws")
    p.add_argument("--zmq-endpoint", required=True,
                   help="our fills publisher's ZMQ PUB endpoint")
    p.add_argument("--output-dir", default="/tmp/hl_trade_compare")
    p.add_argument("--report-interval", type=float, default=10.0)
    args = p.parse_args()

    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    c = TradeLatencyCompare(coins, args.output_dir)
    try:
        await c.run(args.ws_url, args.zmq_endpoint, args.report_interval)
    except KeyboardInterrupt:
        print("\n=== interrupted ===")
        for coin in c.coins:
            cs = c.coin_state[coin]
            print(f"\n{coin}:")
            for k, v in cs.stats.items():
                print(f"  {k}: {v}")
            if cs.deltas_ms:
                pcs = c._percentiles(cs.deltas_ms, ps=(1, 5, 25, 50, 75, 95, 99))
                print(f"  final deltas n={len(cs.deltas_ms)} percentiles={pcs}")


if __name__ == "__main__":
    asyncio.run(main())
