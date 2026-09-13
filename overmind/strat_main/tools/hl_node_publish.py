#! /usr/bin/env python3

"""
Node-feed publisher.

Reads HL non-validating node `node_raw_book_diffs[_streaming]` JSONL files,
reconstructs per-symbol L2 books from L3 diffs (preserves per-order state
internally so removes/adds at the same price work correctly), and either
prints stats or publishes serialized PbBookSnapshot on a ZMQ PUB socket
that consumers (pymultifeed-style ZMQ SUBs or our hl_book_check tool) can
already consume.

>>> This Python file is paired with the C++ implementation at
    src/pktrade/toolbins/hl_node_publisher.cc, which is the *primary*
    implementation going forward. The two are intentionally tied to each
    other (same on-wire behavior, same CLI surface, same bug semantics).
    Until the C++ port reaches feature parity (currently missing
    bootstrap + ContinuousWS catch-up + fills mode), the Python version
    remains canonical for those flows. Once parity is reached, future
    logic changes should land in the C++ file first and only be mirrored
    here if needed for debugging / reproduction.

Run:
    /home/pktrade/.venvs/v1/bin/python overmind/strat_main/tools/hl_node_publish.py \\
        --file <path-to-book-diffs.jsonl> \\
        --print-only

    /home/pktrade/.venvs/v1/bin/python overmind/strat_main/tools/hl_node_publish.py \\
        --file <path-to-book-diffs.jsonl> \\
        --endpoint ipc:///tmp/hl_node_sock
"""

import argparse
import asyncio
import collections
import datetime
import json
import os
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pyfeed"))
import mdmsg_pb2

HYPERLIQUID_MARKET = 4  # PBMARKET_HYPERLIQUID


def parse_node_time_ms(ts: Optional[str]) -> Optional[int]:
    """Parse HL node ISO timestamps to Unix milliseconds.

    Node timestamps look like `2026-06-10T02:11:51.191853458` and carry
    nanosecond precision without an explicit timezone. Python's stdlib parser
    only accepts microseconds, so truncate fractional seconds to 6 digits and
    treat timezone-less values as UTC.
    """
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    if "." in ts:
        head, tail = ts.split(".", 1)
        frac = tail
        tz = ""
        for sep in ("+", "-"):
            if sep in tail:
                frac, tz_part = tail.split(sep, 1)
                tz = sep + tz_part
                break
        ts = f"{head}.{frac[:6]:0<6}{tz}"
    dt = datetime.datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


# ---- L3 operation tracing (debug; off by default) ----------------------------
# Set by main() from --trace-pxs / --trace-out. _TRACE_PXS holds (side, px)
# tuples to watch. When a state mutation touches a watched (side, px), we emit
# one line summarizing the inputs and the resulting real / synth state.
_TRACE_PXS: set = set()
_TRACE_FH = None
_TRACE_LOCK = threading.Lock()

# ---- Per-emit watch (separate from op trace; dumps state at every emit) ------
# Set by main() from --watch-pxs / --watch-out. At every block-boundary emit,
# we dump the current (real, synth, combined) state at each watched (side, px).
_WATCH_PXS: set = set()
_WATCH_FH = None

# ---- Rule 1 skip log (debug; off by default) ---------------------------------
# Set by main() from --log-rule1 / --log-rule1-out. When non-None, every Rule 1
# skip in `reseed_synth_from_ws` writes a classified line here. Off by default
# because in multi-coin / long-running scenarios it would spam.
_RULE1_LOG_FH = None

def _watch_emit(coin: str, book: "L3Book", t_block_ms: Optional[int]) -> None:
    if _WATCH_FH is None or not _WATCH_PXS:
        return
    t_wall = int(time.time() * 1000)
    with _TRACE_LOCK:
        for side, px in sorted(_WATCH_PXS):
            real = book._real_store(side).get(px, {})
            synth = book._synth_store(side).get(px)
            r_n = len(real)
            r_sz = sum(float(v) for v in real.values()) if real else 0.0
            if synth is not None:
                s_sz = float(synth[0])
                s_n = synth[1]
            else:
                s_sz = 0.0
                s_n = 0
            t_n = r_n + s_n
            t_sz = r_sz + s_sz
            _WATCH_FH.write(
                f"[WATCH] t_wall={t_wall} t_block={t_block_ms} coin={coin} "
                f"{side} px={px} real(n={r_n},sz={r_sz:.4f}) "
                f"synth(n={s_n},sz={s_sz:.4f}) emit(n={t_n},sz={t_sz:.4f})\n")
        _WATCH_FH.flush()

def _trace(coin: str, side: str, px: str, op: str, info: str,
           real_dict: Optional[Dict[int, str]], synth_entry: Optional[list],
           t_block_ms: Optional[int] = None) -> None:
    if _TRACE_FH is None or (side, px) not in _TRACE_PXS:
        return
    if real_dict:
        r_n = len(real_dict)
        r_sz = sum(float(v) for v in real_dict.values())
        r_str = f"n={r_n} sz={r_sz:.4f} oids={sorted(real_dict.keys())}"
    else:
        r_str = "n=0 sz=0"
    if synth_entry is not None:
        s_str = f"sz={float(synth_entry[0]):.4f} n={synth_entry[1]}"
    else:
        s_str = "(none)"
    t_wall = int(time.time() * 1000)
    line = (f"t_wall={t_wall} t_block={t_block_ms} coin={coin} {side} px={px} "
            f"OP={op:18s} {info:40s} | real[{r_str}] synth[{s_str}]\n")
    with _TRACE_LOCK:
        _TRACE_FH.write(line)
        _TRACE_FH.flush()


def format_qty(q: float) -> str:
    """Render a quantity with up to 10 decimal places, no trailing zeros."""
    s = f"{q:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


class L3Book:
    """L3 state per symbol, with separate tracking for synthetic levels seeded
    from public WS snapshots.

    - Real orders (positive oids from node diff stream): `bids` / `asks`
      keyed by px → {oid: sz}.
    - Synthetic level state from seeding: `bid_synth` / `ask_synth` keyed
      by px → [sz_remaining_str, n_remaining_int]. Represents "unseen
      resting orders at this price" — their per-order sizes are unknown,
      so we work with aggregate (sz, n).

    Handling of unknown-oid events (orders placed before our state existed):
    - `remove` of unknown oid → decrement synth by avg per-order (sz/n);
      n--; drop synth if n reaches 0.
    - `update` of unknown oid: extract this order from synth (decrement
      sz by origSz, n--); if newSz>0, add as real with newSz.
    - `new` event of any oid: add as real, then drop any synthetic level
      on the OPPOSITE side that's now crossed or locked by this real
      price (they're provably stale).
    """

    def __init__(self, coin: str = "") -> None:
        self.coin = coin
        self.bids: Dict[str, Dict[int, str]] = collections.defaultdict(dict)
        self.asks: Dict[str, Dict[int, str]] = collections.defaultdict(dict)
        self.bid_synth: Dict[str, list] = {}   # px -> [sz_str, n_int]
        self.ask_synth: Dict[str, list] = {}
        # Catch-up support: time-indexed history of recent real-only top-N
        # snapshots. WS samples carry a `time` field; we compare against the
        # snapshot whose event_time ≤ ws.time so timing skew (including
        # arbitrary WS lag) does not break catch-up. Buffer of 60 entries
        # covers ~5s at typical xyz emit rate; bump if WS lag exceeds that.
        # Entries are (event_time_ms, (bids_top5, asks_top5)), strictly
        # non-decreasing in event_time.
        self.emit_top_history: collections.deque = collections.deque(maxlen=60)

    def record_emit_top(self, n_levels: int, event_time_ms: int) -> None:
        """Save real-only top-N at this emit's event_time. Called from
        BookSet.snapshot_l2 on every block-boundary emit."""
        bids = self.real_l2("B")[:n_levels]
        asks = self.real_l2("A")[:n_levels]
        self.emit_top_history.append((event_time_ms, (bids, asks)))

    def get_top_at_or_before(self, target_time_ms: int) -> Optional[Tuple[list, list]]:
        """Return the recorded top-N whose event_time is the latest one
        ≤ target_time_ms — i.e., our state as it was at chain time
        `target_time_ms`. Returns None if we have no such snapshot."""
        best = None
        for t, snap in self.emit_top_history:
            if t <= target_time_ms:
                best = snap
            else:
                break
        return best

    # ---- synth lifecycle -------------------------------------------------

    def reset_synth(self, t_block_ms: Optional[int] = None) -> None:
        """Wipe all synthetic state. Use before re-seeding from a fresh WS
        snapshot."""
        # Trace for any watched px before clearing
        for side, store in (("B", self.bid_synth), ("A", self.ask_synth)):
            for px, entry in list(store.items()):
                _trace(self.coin, side, px, "reset_synth", "",
                       self._real_store(side).get(px), entry, t_block_ms)
        self.bid_synth.clear()
        self.ask_synth.clear()

    def seed_level(self, side: str, px: str, sz: str, n: int,
                   t_block_ms: Optional[int] = None) -> None:
        """Record a synthetic level: total size sz across n orders.
        Overwrites any prior synth at (side, px)."""
        target = self.bid_synth if side == "B" else self.ask_synth
        target[px] = [sz, n]
        _trace(self.coin, side, px, "seed_synth", f"sz={sz} n={n}",
               self._real_store(side).get(px), target[px], t_block_ms)

    # ---- helpers ---------------------------------------------------------

    def _real_store(self, side: str) -> Dict[str, Dict[int, str]]:
        return self.bids if side == "B" else self.asks

    def _synth_store(self, side: str) -> Dict[str, list]:
        return self.bid_synth if side == "B" else self.ask_synth

    def _synth_estimate_remove(self, side: str, px: str,
                               t_block_ms: Optional[int] = None) -> None:
        """An unknown-oid remove at this level: one of the synthetic orders
        is gone. Subtract avg per-order size from sz, decrement n."""
        synth = self._synth_store(side)
        entry = synth.get(px)
        if entry is None:
            _trace(self.coin, side, px, "synth_rm_unknown(noop)", "no synth here",
                   self._real_store(side).get(px), None, t_block_ms)
            return
        sz, n = float(entry[0]), entry[1]
        if n <= 1:
            del synth[px]
            _trace(self.coin, side, px, "synth_rm_unknown(drop)", "n<=1 → drop level",
                   self._real_store(side).get(px), None, t_block_ms)
            return
        new_sz = max(0.0, sz - sz / n)
        entry[0] = format_qty(new_sz)
        entry[1] = n - 1
        _trace(self.coin, side, px, "synth_rm_unknown(dec)",
               f"avg={sz/n:.4f} sub from sz", self._real_store(side).get(px),
               entry, t_block_ms)

    def _synth_extract_known(self, side: str, px: str, orig_sz_str: str,
                             t_block_ms: Optional[int] = None) -> None:
        """An unknown-oid update — we now know the order's prior size.
        Remove that exact contribution: sz -= orig_sz, n--. Drop if depleted."""
        synth = self._synth_store(side)
        entry = synth.get(px)
        if entry is None:
            _trace(self.coin, side, px, "synth_extract(noop)", f"orig_sz={orig_sz_str}",
                   self._real_store(side).get(px), None, t_block_ms)
            return
        sz = float(entry[0])
        n = entry[1]
        new_sz = max(0.0, sz - float(orig_sz_str))
        new_n = max(0, n - 1)
        if new_n == 0 or new_sz <= 0:
            del synth[px]
            _trace(self.coin, side, px, "synth_extract(drop)", f"orig_sz={orig_sz_str}",
                   self._real_store(side).get(px), None, t_block_ms)
            return
        entry[0] = format_qty(new_sz)
        entry[1] = new_n
        _trace(self.coin, side, px, "synth_extract(dec)", f"orig_sz={orig_sz_str}",
               self._real_store(side).get(px), entry, t_block_ms)

    def _drop_inverted_synth(self, real_side: str, real_px: str,
                             t_block_ms: Optional[int] = None) -> int:
        """A real order at (real_side, real_px). Drop synthetic levels on the
        opposite side that this would cross or lock. Returns count dropped."""
        opposite_synth = self.ask_synth if real_side == "B" else self.bid_synth
        opp_side = "A" if real_side == "B" else "B"
        real_px_f = float(real_px)
        if real_side == "B":
            to_drop = [px for px in opposite_synth if float(px) <= real_px_f]
        else:
            to_drop = [px for px in opposite_synth if float(px) >= real_px_f]
        for px in to_drop:
            _trace(self.coin, opp_side, px, "drop_inv_synth",
                   f"by real {real_side}@{real_px}",
                   self._real_store(opp_side).get(px), opposite_synth[px], t_block_ms)
            del opposite_synth[px]
        return len(to_drop)

    def _drop_inverted_real(self, real_side: str, real_px: str,
                            t_block_ms: Optional[int] = None) -> int:
        """A real order at (real_side, real_px). Drop REAL levels on the
        opposite side that would cross or lock with it.

        Newer message wins: if a chain new arrives at price 10 and we have a
        resting real ask at 10, that ask must have been filled/canceled —
        the chain wouldn't sustain a locked book. We may not have seen the
        corresponding remove yet (later block, or missed), but emitting a
        crossed book is never correct.

        Cost: subsequent chain remove events for the dropped oids will miss
        in real.find and fall into the unknown-remove path
        (_synth_estimate_remove). Noisy but handled.
        """
        opposite_real = self._real_store("A" if real_side == "B" else "B")
        opp_side = "A" if real_side == "B" else "B"
        real_px_f = float(real_px)
        if real_side == "B":
            to_drop = [px for px in opposite_real if float(px) <= real_px_f]
        else:
            to_drop = [px for px in opposite_real if float(px) >= real_px_f]
        for px in to_drop:
            entry = opposite_real[px]
            _trace(self.coin, opp_side, px, "drop_inv_real",
                   f"by real {real_side}@{real_px} (dropped {len(entry)} oids)",
                   entry, self._synth_store(opp_side).get(px), t_block_ms)
            del opposite_real[px]
        return len(to_drop)

    # ---- main apply ------------------------------------------------------

    def apply(self, side: str, px: str, oid: int, diff,
              t_block_ms: Optional[int] = None) -> None:
        real = self._real_store(side)
        real_level = real.get(px, {})
        is_known = oid in real_level
        synth_entry = self._synth_store(side).get(px)

        if diff == "remove":
            if is_known:
                real_level.pop(oid, None)
                if not real_level:
                    del real[px]
                _trace(self.coin, side, px, "remove_known", f"oid={oid}",
                       real.get(px), self._synth_store(side).get(px), t_block_ms)
            else:
                _trace(self.coin, side, px, "remove_unknown(pre)", f"oid={oid}",
                       real_level, synth_entry, t_block_ms)
                self._synth_estimate_remove(side, px, t_block_ms)
        elif isinstance(diff, dict) and "new" in diff:
            sz = diff["new"]["sz"]
            real[px][oid] = sz
            _trace(self.coin, side, px, "new", f"oid={oid} sz={sz}",
                   real[px], synth_entry, t_block_ms)
            # This real order may invalidate stale state on the other side
            self._drop_inverted_synth(side, px, t_block_ms)
            self._drop_inverted_real(side, px, t_block_ms)
        elif isinstance(diff, dict) and "update" in diff:
            new_sz = diff["update"]["newSz"]
            orig_sz = diff["update"].get("origSz", "0")
            if is_known:
                if float(new_sz) == 0.0:
                    real_level.pop(oid, None)
                    if not real_level:
                        del real[px]
                    _trace(self.coin, side, px, "update_known(rm)",
                           f"oid={oid} newSz=0", real.get(px),
                           self._synth_store(side).get(px), t_block_ms)
                else:
                    real[px][oid] = new_sz
                    _trace(self.coin, side, px, "update_known", f"oid={oid} newSz={new_sz}",
                           real[px], synth_entry, t_block_ms)
                    self._drop_inverted_synth(side, px, t_block_ms)
            else:
                # Unknown oid being updated: we know its prior size from origSz
                _trace(self.coin, side, px, "update_unknown(pre)",
                       f"oid={oid} origSz={orig_sz} newSz={new_sz}",
                       real_level, synth_entry, t_block_ms)
                self._synth_extract_known(side, px, orig_sz, t_block_ms)
                if float(new_sz) > 0:
                    real[px][oid] = new_sz
                    _trace(self.coin, side, px, "update_unknown(add)",
                           f"oid={oid} newSz={new_sz}", real[px],
                           self._synth_store(side).get(px), t_block_ms)
                    self._drop_inverted_synth(side, px, t_block_ms)
                    self._drop_inverted_real(side, px, t_block_ms)
        else:
            raise ValueError(f"Unknown raw_book_diff: {diff!r}")

    # ---- emit -----------------------------------------------------------

    def l2(self, side: str) -> List[Tuple[str, str, int]]:
        """Aggregate real + synthetic per price. Sorted aggressively.

        A level appears in the output iff (real_sz + synth_sz) > 0 AND
        (real_n + synth_n) > 0.
        """
        real = self._real_store(side)
        synth = self._synth_store(side)
        all_pxs = set(real.keys()) | set(synth.keys())
        if side == "B":
            sorted_pxs = sorted(all_pxs, key=lambda p: -float(p))
        else:
            sorted_pxs = sorted(all_pxs, key=lambda p: float(p))
        out = []
        for px in sorted_pxs:
            real_level = real.get(px, {})
            real_sz = sum(float(s) for s in real_level.values())
            real_n = len(real_level)
            synth_entry = synth.get(px)
            if synth_entry is not None:
                synth_sz = float(synth_entry[0])
                synth_n = synth_entry[1]
            else:
                synth_sz = 0.0
                synth_n = 0
            total_sz = real_sz + synth_sz
            total_n = real_n + synth_n
            if total_sz > 0 and total_n > 0:
                out.append((px, format_qty(total_sz), total_n))
        return out

    def real_l2(self, side: str) -> List[Tuple[str, str, int]]:
        """L2 from REAL orders only — no synth contribution. Used for
        caught-up detection: when real-only L2 matches the WS snapshot's
        top levels, our diff-based state is self-sufficient and we can
        unsubscribe from WS for this sym."""
        real = self._real_store(side)
        if side == "B":
            sorted_pxs = sorted(real.keys(), key=lambda p: -float(p))
        else:
            sorted_pxs = sorted(real.keys(), key=lambda p: float(p))
        out = []
        for px in sorted_pxs:
            lvl = real[px]
            sz = sum(float(s) for s in lvl.values())
            if sz > 0 and lvl:
                out.append((px, format_qty(sz), len(lvl)))
        return out

    def clear_synth(self, t_block_ms: Optional[int] = None) -> None:
        """Drop all synth state. Used when we unsubscribe a sym from WS —
        from that point on we rely on real diffs only."""
        for side, store in (("B", self.bid_synth), ("A", self.ask_synth)):
            for px, entry in list(store.items()):
                _trace(self.coin, side, px, "clear_synth", "post-catchup wipe",
                       self._real_store(side).get(px), entry, t_block_ms)
        self.bid_synth.clear()
        self.ask_synth.clear()


class BookSet:
    """Per-coin L3Book registry + per-block dirty tracking.

    Thread-safe: a continuous WS task (in a separate thread) calls
    `reseed_synth_from_ws()` to update synth state while the main tail
    loop is calling `apply_event()`. A single Lock protects all state
    mutations; the lock is fine-grained enough that contention is low.
    """

    def __init__(self) -> None:
        self.books: Dict[str, L3Book] = {}
        self.dirty_this_block: set = set()
        self.current_block: int = -1
        self.events_seen = 0
        self.lock = threading.Lock()
        # The block_time of the last block whose events we finished applying.
        # Used as event_time on emitted PbBookSnapshot messages so downstream
        # can compute end-to-end latency. Set by the tail loop right after
        # advancing to a new block, AFTER emitting dirty coins from the
        # previous block.
        self.latest_block_time_ms: int = 0
        # Latest local node receive time for the block being emitted. This is
        # stamped into PbMessage.rx_timestamp so historical readers can order
        # node-derived snapshots by receive time.
        self.latest_local_time_ms: int = 0

    def _book_unlocked(self, coin: str) -> L3Book:
        b = self.books.get(coin)
        if b is None:
            b = L3Book(coin=coin)
            self.books[coin] = b
        return b

    def _book(self, coin: str) -> L3Book:
        with self.lock:
            return self._book_unlocked(coin)

    def apply_event(self, ev: dict) -> None:
        with self.lock:
            self._book_unlocked(ev["coin"]).apply(
                ev["side"], ev["px"], ev["oid"], ev["raw_book_diff"],
                t_block_ms=self.latest_block_time_ms,
            )
            self.dirty_this_block.add(ev["coin"])
            self.events_seen += 1

    def take_dirty(self) -> List[str]:
        with self.lock:
            out = list(self.dirty_this_block)
            self.dirty_this_block.clear()
            return out

    def reseed_synth_from_ws(self, coin: str, bids: list, asks: list) -> None:
        """Wipe and re-seed synth state for `coin` from a fresh WS snapshot.
        Real (diff-derived) state is preserved.

        Two rules govern what we plant:

        Rule 1: At continuous resync (i.e., we already have *some* book
        state), do NOT add a new synth level that is more aggressive
        than our current inside of book. A genuinely-new aggressive
        level would have reached us as a `new` event in the diffs;
        if it's in WS but not in our state, either it's about to arrive
        or WS is stale, and fabricating it from WS would cause crosses
        or locks. Back levels (less aggressive than our top) we still
        take from WS for texture.

        Rule 2: For each price WS reports that we ALREADY know about
        (have real or had prior synth), set
            synth = max(0, ws - real)
        per (sz, n). This keeps `emit = real + synth = WS` at reseed
        time without double-counting orders we already track.

        Bootstrap is unrestricted by Rule 1 because real is empty, so
        there's no top of book to be more aggressive than.

        Rule 1 hits are logged to stderr with a `[RULE1]` tag so we can
        tally frequency and what would have happened (lock / cross /
        inside-spread) afterward.
        """
        with self.lock:
            book = self._book_unlocked(coin)
            t = self.latest_block_time_ms

            # Snapshot pre-reset "known" pxs and real top-of-book for Rule 1.
            #
            # "Known" means "we have chain-truth evidence this px exists."
            # Prior synth does NOT count — synth is itself WS-derived, so
            # including it would let a previously-stale synth re-validate
            # itself on every reseed even when wrong (PNUT case). Real-only
            # known_pxs means Rule 1 re-asks "should this WS level exist?"
            # each reseed.
            known_bid_pxs = set(book.bids.keys())
            known_ask_pxs = set(book.asks.keys())
            real_bid_top = max((float(px) for px in book.bids), default=None)
            real_ask_top = min((float(px) for px in book.asks), default=None)

            book.reset_synth(t_block_ms=t)

            for side, lvls, real_store, known_pxs in (
                ("B", bids, book.bids, known_bid_pxs),
                ("A", asks, book.asks, known_ask_pxs),
            ):
                for lvl in lvls:
                    px_str = lvl["px"]
                    ws_sz = float(lvl["sz"])
                    ws_n = int(lvl.get("n", 1))
                    real_level = real_store.get(px_str, {})
                    real_sz = sum(float(s) for s in real_level.values())
                    real_n = len(real_level)

                    # Rule 1: skip new aggressive or book-invalid levels.
                    # Two reasons to skip an unknown WS level:
                    #   (a) "more aggressive than own real top" — diff stream
                    #       should be ahead of WS; a real new aggressive
                    #       level would have arrived as a chain event.
                    #   (b) "would cross or lock the OPPOSITE real top" —
                    #       seeding this would publish an invalid book.
                    #       HL is essentially never crossed; if it were
                    #       we'd see fill/remove events in the diff stream.
                    # (b) was missing until 2026-06-12 — caused stuck-stale
                    # synth levels for PNUT-class syms.
                    is_known = px_str in known_pxs
                    if not is_known:
                        px_f = float(px_str)
                        skip = False
                        opp_top = None
                        if side == "B":
                            if real_bid_top is not None and px_f > real_bid_top:
                                skip = True
                                opp_top = real_ask_top
                            if real_ask_top is not None and px_f >= real_ask_top:
                                skip = True
                                opp_top = real_ask_top
                        else:  # side == "A"
                            if real_ask_top is not None and px_f < real_ask_top:
                                skip = True
                                opp_top = real_bid_top
                            if real_bid_top is not None and px_f <= real_bid_top:
                                skip = True
                                opp_top = real_bid_top
                        if skip:
                            if _RULE1_LOG_FH is not None:
                                if opp_top is None:
                                    cls = "no_opposite"
                                elif px_f == opp_top:
                                    cls = "would_lock"
                                elif (side == "B" and px_f > opp_top) or \
                                     (side == "A" and px_f < opp_top):
                                    cls = "would_cross"
                                else:
                                    cls = "would_inside_spread"
                                _RULE1_LOG_FH.write(
                                    f"[RULE1] coin={coin} side={side} px={px_str} "
                                    f"ws_sz={ws_sz} ws_n={ws_n} "
                                    f"real_bid_top={real_bid_top} "
                                    f"real_ask_top={real_ask_top} class={cls}\n")
                                _RULE1_LOG_FH.flush()
                            continue

                    # Rule 2 / bootstrap: synth = max(0, ws - real).
                    synth_sz = max(0.0, ws_sz - real_sz)
                    synth_n = max(0, ws_n - real_n)
                    if synth_sz > 0 and synth_n > 0:
                        book.seed_level(side, px_str, format_qty(synth_sz),
                                        synth_n, t_block_ms=t)
            self.dirty_this_block.add(coin)

    def clear_synth_for(self, coin: str) -> None:
        """Drop synth state for a sym — called when WS unsubscribes a coin
        that's been validated as caught-up."""
        with self.lock:
            book = self.books.get(coin)
            if book is not None:
                book.clear_synth(t_block_ms=self.latest_block_time_ms)
                self.dirty_this_block.add(coin)

    def snapshot_real_l2_top(self, coin: str, n_levels: int) -> Optional[Tuple[list, list]]:
        """For caught-up detection: snapshot the real-only L2 top N levels
        per side. Returns (bids, asks) as lists of (px, sz, n) tuples, or
        None if the coin isn't tracked yet."""
        with self.lock:
            book = self.books.get(coin)
            if book is None:
                return None
            return (book.real_l2("B")[:n_levels], book.real_l2("A")[:n_levels])

    def snapshot_real_l2_top_at(self, coin: str, n_levels: int,
                                 target_time_ms: int) -> Optional[Tuple[list, list]]:
        """For catch-up: return our real-only top-N as it was at chain time
        `target_time_ms`. We look up the historical emit snapshot whose
        event_time is the latest ≤ target_time_ms — i.e., the state WS would
        have observed when it sampled at target_time_ms. This handles
        arbitrary WS lag without false negatives."""
        with self.lock:
            book = self.books.get(coin)
            if book is None:
                return None
            return book.get_top_at_or_before(target_time_ms)

    def snapshot_l2(self, coin: str) -> Optional[Tuple[list, list, int, int]]:
        """Atomically copy publishable L2 state and timestamp metadata."""
        with self.lock:
            book = self.books.get(coin)
            if book is None:
                return None
            _watch_emit(coin, book, self.latest_block_time_ms)
            result = (book.l2("B"), book.l2("A"),
                      self.latest_block_time_ms, self.latest_local_time_ms)
            # Record the real-only top-5 we just emitted, tagged with this
            # block's event_time, so the catch-up check can later compare
            # an incoming WS msg against the state we held at WS's chain
            # time. Must equal ContinuousWS.MATCH_LEVELS (hardcoded here
            # to avoid a forward ref).
            book.record_emit_top(n_levels=5,
                                 event_time_ms=self.latest_block_time_ms)
            return result


def build_pbmessage(coin: str, bids: List[Tuple[str, str, int]],
                    asks: List[Tuple[str, str, int]],
                    event_time_ms: int = 0,
                    rx_timestamp_ms: int = 0) -> bytes:
    msg = mdmsg_pb2.PbMessage()
    msg.symbol_id = coin
    msg.market = HYPERLIQUID_MARKET
    if rx_timestamp_ms:
        msg.rx_timestamp = rx_timestamp_ms
    snap = msg.book_snapshot
    if event_time_ms:
        snap.event_time = event_time_ms
    for px, sz, n in bids:
        lvl = snap.bids.add()
        lvl.px = px
        lvl.qty = sz
        lvl.numords = n
    for px, sz, n in asks:
        lvl = snap.asks.add()
        lvl.px = px
        lvl.qty = sz
        lvl.numords = n
    return msg.SerializeToString()


def check_invariants(coin: str, bids, asks) -> List[str]:
    """Sanity checks. Returns list of anomalies found."""
    anomalies = []
    # Monotonicity
    for i in range(1, len(bids)):
        if float(bids[i][0]) >= float(bids[i - 1][0]):
            anomalies.append(f"{coin}: bids not strictly descending at idx {i}")
            break
    for i in range(1, len(asks)):
        if float(asks[i][0]) <= float(asks[i - 1][0]):
            anomalies.append(f"{coin}: asks not strictly ascending at idx {i}")
            break
    # Crossed
    if bids and asks and float(bids[0][0]) >= float(asks[0][0]):
        anomalies.append(f"{coin}: crossed book bid={bids[0][0]} ask={asks[0][0]}")
    return anomalies


def emit(coin: str, bookset: BookSet, pub_socket, print_only: bool, verbose: bool,
         stats: dict, max_levels: int = 0) -> None:
    snap = bookset.snapshot_l2(coin)
    if snap is None:
        return
    bids, asks, event_time_ms, rx_timestamp_ms = snap
    anomalies = check_invariants(coin, bids, asks)
    for a in anomalies:
        stats["anomalies"] += 1
        if stats["anomalies"] <= 5 or verbose:
            print(f"[ANOM] {a}")
    if print_only:
        if verbose or stats["emitted"] < 6:
            print(f"  {coin:14s} bids={len(bids):3d} asks={len(asks):3d}", end="")
            if bids and asks:
                print(f" top={bids[0][0]}/{asks[0][0]}")
            else:
                print(" (one-sided)")
    else:
        if max_levels and max_levels > 0:
            bids = bids[:max_levels]
            asks = asks[:max_levels]
        payload = build_pbmessage(coin, bids, asks,
                                  event_time_ms=event_time_ms,
                                  rx_timestamp_ms=rx_timestamp_ms)
        pub_socket.send(payload, copy=False)
    stats["emitted"] += 1


def hourly_path(data_dir: str, now: Optional[datetime.datetime] = None) -> str:
    """Compute the current hour's file path inside a {data_dir}/{YYYYMMDD}/{H}
    layout. H is the integer UTC hour with no leading zero (matches what
    hl-node writes)."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return os.path.join(data_dir, now.strftime("%Y%m%d"), str(now.hour))


def tail_process_lines(buf: str, line_callback) -> str:
    """Feed full lines from buf to callback, return the trailing partial."""
    while "\n" in buf:
        line, buf = buf.split("\n", 1)
        if line:
            line_callback(line)
    return buf


# =============================================================================
# D3: Bootstrap snapshot handshake
#
# Problem: when the publisher starts mid-hour, the L3 book it builds from diffs
# alone is incomplete (orders resting from before our start time never appear).
# Solution: at startup, fetch a full L2 snapshot per symbol from HL public WS,
# seed our L3 state with synthetic negative oids, then apply only diffs whose
# block_time > snapshot.time.
#
# Coverage: native HL perps (via /info {"type":"meta"}) + HIP-3 dex `xyz`
# (via /info {"type":"meta","dex":"xyz"}). Other HIP-3 builders are not seeded
# — they'll still accumulate state from real diffs over the next ~hour but
# won't have seeded resting orders. xyz is the priority HIP-3 deployer for us.
# =============================================================================

HL_PUBLIC_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_PUBLIC_INFO_URL = "https://api.hyperliquid.xyz/info"
HIP3_DEXES_TO_SEED = ("xyz",)  # extend later if we trade other builders


def discover_bootstrap_syms(info_url: str, blacklist: frozenset) -> set:
    """Return set of coin names to seed: native HL perps + each HIP-3 dex in
    HIP3_DEXES_TO_SEED. Blacklist is honored. Delisted syms are dropped."""
    import requests
    syms: set = set()
    # 1. Native perps (names like "BTC", "ETH" — no prefix)
    r = requests.post(info_url, json={"type": "meta"}, timeout=10).json()
    syms |= {c["name"] for c in r.get("universe", []) if not c.get("isDelisted")}
    # 2. Each HIP-3 dex we care about — coin names already arrive prefixed
    #    ("xyz:XYZ100" etc.)
    for dex in HIP3_DEXES_TO_SEED:
        try:
            r = requests.post(info_url, json={"type": "meta", "dex": dex}, timeout=10).json()
            syms |= {c["name"] for c in r.get("universe", []) if not c.get("isDelisted")}
        except Exception as e:
            print(f"  WARN: failed to discover dex={dex}: {e}", flush=True)
    return syms - blacklist


def make_l2book_sub(coin: str) -> dict:
    """Build a subscribe message for the slow l2Book (20-level) feed.
    HIP-3 syms carry a `:` prefix; we send `coin` as-is and pull `dex` out."""
    sub: dict = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
    if ":" in coin:
        sub["subscription"]["dex"] = coin.split(":", 1)[0]
    return sub


async def fetch_l2_snapshots(ws_url: str, syms: set, timeout_s: float, verbose: bool) -> dict:
    """Open one WS, subscribe to l2Book for every sym, collect one snapshot
    per sym until all received OR timeout. Returns {coin: (time_ms, bids, asks)}.
    Slow / failing syms don't block the rest — each is independent."""
    import websockets  # type: ignore
    snapshots: dict = {}
    pending = set(syms)
    n_total = len(syms)
    async with websockets.connect(ws_url, ping_interval=None) as ws:
        for s in syms:
            await ws.send(json.dumps(make_l2book_sub(s)))
        deadline = asyncio.get_event_loop().time() + timeout_s
        n_subresp = 0
        n_err = 0
        while pending:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            ch = msg.get("channel")
            if ch == "subscriptionResponse":
                n_subresp += 1
                continue
            if ch == "error":
                n_err += 1
                if verbose:
                    print(f"  [WS error] {msg.get('data')}", flush=True)
                continue
            if ch != "l2Book":
                continue
            data = msg["data"]
            coin = data["coin"]
            if coin in pending:
                snapshots[coin] = (data["time"], data["levels"][0], data["levels"][1])
                pending.discard(coin)
                if verbose and len(snapshots) % 50 == 0:
                    print(f"  ... {len(snapshots)}/{n_total} snapshots received", flush=True)
    if verbose:
        print(f"  WS: {n_subresp} subResp, {n_err} errors, {len(snapshots)}/{n_total} snapshots", flush=True)
    return snapshots


async def bootstrap_phase(ws_url: str, syms: set, diff_path: str,
                          timeout_s: float, verbose: bool):
    """Run snapshot-fetch + diff-buffering concurrently. Returns:
        snapshots: {coin -> (time_ms, bids, asks)}
        buffered_packets: list of parsed packet dicts received from the diff
            file while bootstrap was in progress
        f: open file handle positioned at the end of buffered data (caller
            continues tailing from here)
        leftover_buf: any partial line at the end of buffered (not yet a full
            packet) — caller should prepend to its read buffer.
    """
    # Open at EOF — we don't want to replay history before bootstrap started.
    f = open(diff_path, "rb")
    f.seek(0, os.SEEK_END)

    done = asyncio.Event()
    buffered = []
    leftover_buf = ""

    async def snapshot_task() -> dict:
        s = await fetch_l2_snapshots(ws_url, syms, timeout_s, verbose)
        done.set()
        return s

    async def buffer_task():
        nonlocal leftover_buf
        local_buf = ""
        while not done.is_set():
            chunk = f.read(65536)
            if chunk:
                local_buf += chunk.decode("utf-8", errors="replace")
                while "\n" in local_buf:
                    line, local_buf = local_buf.split("\n", 1)
                    if line:
                        try:
                            buffered.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            else:
                await asyncio.sleep(0.05)
        leftover_buf = local_buf

    snap_t = asyncio.create_task(snapshot_task())
    buf_t = asyncio.create_task(buffer_task())
    snapshots = await snap_t
    await buf_t
    return snapshots, buffered, f, leftover_buf


def seed_books(bookset: "BookSet", snapshots: dict, verbose: bool) -> int:
    """Inject seeded levels via L3Book.seed_level — preserves (sz, n) per
    level so unknown-oid removes can be decremented accurately."""
    n_levels = 0
    for coin, (ts, bids, asks) in snapshots.items():
        book = bookset._book(coin)
        book.reset_synth()  # in case there was prior synth state
        for lvl in bids:
            book.seed_level("B", lvl["px"], lvl["sz"], int(lvl.get("n", 1)))
            n_levels += 1
        for lvl in asks:
            book.seed_level("A", lvl["px"], lvl["sz"], int(lvl.get("n", 1)))
            n_levels += 1
    if verbose:
        print(f"  seeded {n_levels} levels across {len(snapshots)} coins "
              f"(synthetic state tracked separately from real oids)", flush=True)
    return n_levels


class ContinuousWS:
    """Long-running WS task that keeps synth state fresh and auto-unsubscribes
    each sym once its real-only L2 has matched the WS snapshot top-N for K
    consecutive pushes.

    Runs in a dedicated thread (asyncio loop owned by the thread). All shared
    state mutations go through BookSet's lock.
    """

    MATCH_LEVELS = 5         # compare top 5 per side
    MATCH_STREAK_REQUIRED = 3  # K consecutive matches → caught up
    LAG_LOG_INTERVAL_S = 30.0  # how often to log WS lag summary

    def __init__(self, bookset: "BookSet", syms: set, ws_url: str,
                 verbose: bool, stop_event: threading.Event) -> None:
        self.bookset = bookset
        self.syms = set(syms)
        self.ws_url = ws_url
        self.verbose = verbose
        self.stop_event = stop_event
        # Per-sym state machine
        self.caught_up: set = set()
        self.match_streak: Dict[str, int] = {}
        # WS lag tracking: how far behind wall-clock time each WS msg
        # arrives relative to its `time` field. Useful for sizing the
        # emit history buffer and diagnosing WS-server slowness.
        self._lag_samples: List[int] = []
        self._lag_last_log_t: float = time.time()
        # Stats
        self.stats = {
            "msgs_received": 0,
            "reseeds": 0,
            "caught_up": 0,
            "reconnects": 0,
        }

    def _coin_matches_ws(self, coin: str, ws_data: dict) -> bool:
        """True if our real-only top-N at WS's reported chain time matches
        the WS snapshot's top-N on (px, sz, n) for both sides. We look up
        our historical state at `ws_data["time"]` so a slow WS feed
        (large recv−time lag) still gets compared against the right block
        of our own state, not whatever we happen to hold right now.

        Size is compared numerically (not as strings), because our
        format_qty strips trailing zeros ("2") while WS sends them in
        ("2.0") — same value, different string. Price uses string
        equality since both sides use the chain's canonical tick string.
        """
        ws_t = ws_data.get("time")
        if ws_t is None:
            return False
        snap = self.bookset.snapshot_real_l2_top_at(coin, self.MATCH_LEVELS, ws_t)
        if snap is None:
            return False
        node_bids, node_asks = snap
        ws_bids = ws_data["levels"][0][:self.MATCH_LEVELS]
        ws_asks = ws_data["levels"][1][:self.MATCH_LEVELS]
        if (len(node_bids) < self.MATCH_LEVELS or
                len(node_asks) < self.MATCH_LEVELS or
                len(ws_bids) < self.MATCH_LEVELS or
                len(ws_asks) < self.MATCH_LEVELS):
            return False
        def _eq(node_lvl, ws_lvl) -> bool:
            return (node_lvl[0] == ws_lvl["px"]
                    and float(node_lvl[1]) == float(ws_lvl["sz"])
                    and node_lvl[2] == int(ws_lvl["n"]))
        for nb, wb in zip(node_bids, ws_bids):
            if not _eq(nb, wb):
                return False
        for na, wa in zip(node_asks, ws_asks):
            if not _eq(na, wa):
                return False
        return True

    async def _connect_and_run(self) -> None:
        import websockets  # type: ignore
        async with websockets.connect(self.ws_url, ping_interval=None) as ws:
            # HL closes inactive sockets at 60s. Send an app-level ping every
            # 25s so we get two retries before the server times us out. This
            # matters most post-catch-up when our sub set is empty — the
            # socket would otherwise sit silent and be dropped. Mirrors
            # pymultifeed.py's hl_ping. NOTE for the eventual C++/Rust port:
            # this keepalive loop must be recreated.
            async def ping_loop():
                while True:
                    try:
                        await ws.send(json.dumps({"method": "ping"}))
                    except Exception:
                        return
                    await asyncio.sleep(25)
            ping_task = asyncio.create_task(ping_loop())

            try:
                # Subscribe to all syms that aren't already caught up
                to_sub = self.syms - self.caught_up
                for s in to_sub:
                    await ws.send(json.dumps(make_l2book_sub(s)))
                if self.verbose:
                    print(f"  [ws] subscribed {len(to_sub)} syms "
                          f"(caught_up so far: {len(self.caught_up)})", flush=True)

                while not self.stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        # 30s without any message — possible connection hung, force reconnect
                        if self.verbose:
                            print("  [ws] 30s of silence, forcing reconnect", flush=True)
                        break

                    msg = json.loads(raw)
                    ch = msg.get("channel")
                    if ch != "l2Book":
                        continue
                    data = msg["data"]
                    coin = data["coin"]
                    # Track WS lag for each l2Book msg arrival.
                    ws_t = data.get("time")
                    if ws_t is not None:
                        lag = int(time.time() * 1000) - int(ws_t)
                        self._lag_samples.append(lag)
                        now = time.time()
                        if (self.verbose and
                                now - self._lag_last_log_t >= self.LAG_LOG_INTERVAL_S
                                and self._lag_samples):
                            s = sorted(self._lag_samples)
                            p50 = s[len(s)//2]
                            p95 = s[max(0, int(0.95 * len(s)) - 1)]
                            print(f"  [ws] lag (recv-time, ms) over {len(s)} msgs: "
                                  f"p50={p50} p95={p95} max={s[-1]}", flush=True)
                            self._lag_samples = []
                            self._lag_last_log_t = now
                    if coin in self.caught_up:
                        # We already unsubscribed but may still receive a straggler.
                        continue
                    self.stats["msgs_received"] += 1

                    # 1. Check caught-up against the WS state BEFORE reseed —
                    #    we want to know if real-only diffs can produce reality
                    #    by themselves.
                    if self._coin_matches_ws(coin, data):
                        self.match_streak[coin] = self.match_streak.get(coin, 0) + 1
                        if self.match_streak[coin] >= self.MATCH_STREAK_REQUIRED:
                            # Caught up — unsubscribe and clear synth
                            unsub = {
                                "method": "unsubscribe",
                                "subscription": {"type": "l2Book", "coin": coin},
                            }
                            if ":" in coin:
                                unsub["subscription"]["dex"] = coin.split(":", 1)[0]
                            await ws.send(json.dumps(unsub))
                            self.caught_up.add(coin)
                            self.bookset.clear_synth_for(coin)
                            self.stats["caught_up"] += 1
                            if self.verbose:
                                print(f"  [ws] CAUGHT UP and unsubscribed: {coin} "
                                      f"(total caught_up: {len(self.caught_up)}/{len(self.syms)})",
                                      flush=True)
                            continue
                    else:
                        self.match_streak[coin] = 0

                    # 2. Reseed synth from the fresh snapshot
                    self.bookset.reseed_synth_from_ws(
                        coin, data["levels"][0], data["levels"][1]
                    )
                    self.stats["reseeds"] += 1
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except BaseException:
                    pass

    async def _run_with_reconnect(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self._connect_and_run()
            except Exception as e:
                if self.verbose:
                    print(f"  [ws] connection error: {e!r}", flush=True)
                self.stats["reconnects"] += 1
                await asyncio.sleep(2.0)

    def run_thread(self) -> None:
        """Entry point for the thread. Runs an asyncio loop."""
        try:
            asyncio.run(self._run_with_reconnect())
        except Exception as e:
            print(f"[ws thread] exited with error: {e!r}", flush=True)


def replay_buffered_diffs(bookset: "BookSet", buffered: list,
                          snapshot_times: dict, blacklist: frozenset,
                          only_coin: Optional[str]) -> tuple:
    """Apply buffered diffs whose block_time > the snapshot.time for that coin.
    Coins without a snapshot (not seeded) get all buffered events applied
    (consistent with normal tail behavior). Returns (applied, skipped_old,
    skipped_filter)."""
    applied = 0
    skipped_old = 0
    skipped_filter = 0
    for pkt in buffered:
        block_time_ms = parse_node_time_ms(pkt.get("block_time"))
        for ev in pkt.get("events", []):
            coin = ev.get("coin")
            if only_coin and coin != only_coin:
                skipped_filter += 1
                continue
            if coin in blacklist:
                skipped_filter += 1
                continue
            snap_ts = snapshot_times.get(coin)
            if snap_ts is not None and block_time_ms is not None and block_time_ms <= snap_ts:
                skipped_old += 1
                continue
            try:
                bookset.apply_event(ev)
                applied += 1
            except Exception:
                pass
    return applied, skipped_old, skipped_filter


# =============================================================================
# end D3
# =============================================================================


def run_tail(data_dir: str, endpoint: str, print_only: bool, verbose: bool,
             only_coin: Optional[str], blacklist: frozenset,
             from_start: bool, poll_interval_s: float,
             bootstrap: bool = False,
             public_ws_url: str = HL_PUBLIC_WS_URL,
             info_url: str = HL_PUBLIC_INFO_URL,
             bootstrap_timeout_s: float = 10.0,
             max_levels: int = 0) -> None:
    """Tail the current hour file in `data_dir/{YYYYMMDD}/{H}`, rolling over
    when the UTC hour changes."""
    pub_socket = None
    if not print_only:
        import zmq
        ctx = zmq.Context()
        pub_socket = ctx.socket(zmq.PUB)
        pub_socket.bind(endpoint)
        print(f"PUB bound to {endpoint}", flush=True)
        time.sleep(0.3)

    bookset = BookSet()
    stats = {"emitted": 0, "anomalies": 0, "blocks": 0, "lines": 0, "skipped": 0,
             "rotations": 0}

    def handle_line(line: str) -> None:
        stats["lines"] += 1
        try:
            packet = json.loads(line)
        except json.JSONDecodeError:
            return
        block_no = packet.get("block_number", -1)
        # Parse node timestamps for outgoing PbBookSnapshot.event_time and
        # PbMessage.rx_timestamp.
        new_block_time_ms = parse_node_time_ms(packet.get("block_time"))
        new_local_time_ms = parse_node_time_ms(packet.get("local_time"))
        if block_no != bookset.current_block:
            # Emit dirty coins from the PREVIOUS block FIRST (so they carry
            # that block's event_time/rx_timestamp, which are currently in
            # latest_block_time_ms/latest_local_time_ms).
            for coin in bookset.take_dirty():
                if only_coin and coin != only_coin:
                    continue
                if coin in blacklist:
                    continue
                emit(coin, bookset, pub_socket, print_only, verbose, stats, max_levels)
            # Now advance to the new block.
            if new_block_time_ms is not None:
                bookset.latest_block_time_ms = new_block_time_ms
            if new_local_time_ms is not None:
                bookset.latest_local_time_ms = new_local_time_ms
            bookset.current_block = block_no
            stats["blocks"] += 1
        else:
            # Multiple packets can belong to the same block. Keep the latest
            # node receive time so the per-block snapshot rx_timestamp reflects
            # when the final packet for that block reached the node.
            if new_local_time_ms is not None:
                bookset.latest_local_time_ms = max(bookset.latest_local_time_ms,
                                                   new_local_time_ms)
        for ev in packet.get("events", []):
            coin = ev.get("coin")
            if only_coin and coin != only_coin:
                stats["skipped"] += 1
                continue
            if coin in blacklist:
                stats["skipped"] += 1
                continue
            bookset.apply_event(ev)

    print(f"TAIL mode: watching {data_dir}", flush=True)
    current_path = hourly_path(data_dir)
    print(f"current hour file: {current_path}", flush=True)

    # Wait for current file to exist.
    while not os.path.exists(current_path):
        print(f"  waiting for {current_path}...", flush=True)
        time.sleep(poll_interval_s)
        new_path = hourly_path(data_dir)
        if new_path != current_path:
            current_path = new_path
            print(f"  hour rolled while waiting; now watching {current_path}", flush=True)

    ws_stop_event = threading.Event()
    ws_thread = None
    continuous_ws = None

    if bootstrap:
        print(f"\n=== BOOTSTRAP v2 (continuous WS resync) ===", flush=True)
        print(f"  discovering syms via {info_url} ...", flush=True)
        syms = discover_bootstrap_syms(info_url, blacklist)
        xyz_count = sum(1 for s in syms if s.startswith("xyz:"))
        print(f"  {len(syms)} syms to track ({xyz_count} xyz, {len(syms)-xyz_count} native)",
              flush=True)
        # If only_coin is set we still keep the broader sym set for WS — the
        # event-side filter only affects emission, not state. But for
        # debugging a single coin it's cleaner to subscribe only to that one.
        if only_coin:
            syms = {only_coin} if only_coin in syms else {only_coin}
            print(f"  --coin {only_coin!r} given; reducing WS sub set to {len(syms)}",
                  flush=True)

        continuous_ws = ContinuousWS(bookset, syms, public_ws_url, verbose, ws_stop_event)
        ws_thread = threading.Thread(target=continuous_ws.run_thread, daemon=True,
                                     name="hl-pub-ws")
        ws_thread.start()
        print(f"  WS thread started; first reseeds will land within a few seconds",
              flush=True)
        print(f"=== entering tail loop ===\n", flush=True)

    f = open(current_path, "rb")
    if bootstrap or not from_start:
        # In bootstrap mode we always start from EOF — the diffs from before
        # we started would conflict with the WS-derived synth.
        f.seek(0, os.SEEK_END)
    buf = ""

    try:
        while True:
            chunk = f.read(65536)
            if chunk:
                buf += chunk.decode("utf-8", errors="replace")
                buf = tail_process_lines(buf, handle_line)
                continue
            # No data — check rotation.
            new_path = hourly_path(data_dir)
            if new_path != current_path:
                # FINAL DRAIN: hl-visor's writes to the closing hour file
                # may not have landed in our page cache when this poll
                # returned 0 bytes. Give the OS write buffers a moment,
                # then read until 3 consecutive empty reads. Without
                # this, raw book-diff events written in the last ~100 ms
                # of an hour get silently dropped at rotation.
                time.sleep(1.0)
                drained_lines = 0
                empty_reads = 0
                while empty_reads < 3:
                    chunk2 = f.read(65536)
                    if chunk2:
                        before_len = len(buf)
                        buf += chunk2.decode("utf-8", errors="replace")
                        buf = tail_process_lines(buf, handle_line)
                        # Rough count of lines drained: number of \n in
                        # the added chunk plus what's already in buf.
                        drained_lines += chunk2.count(b"\n")
                        empty_reads = 0
                    else:
                        empty_reads += 1
                        time.sleep(0.1)
                f.close()
                stats["rotations"] += 1
                print(f"\n=== rotation {stats['rotations']}: {current_path} -> {new_path} "
                      f"(drained {drained_lines} late lines) ===", flush=True)
                print(f"  cumulative: lines={stats['lines']} blocks={stats['blocks']} "
                      f"emitted={stats['emitted']} anomalies={stats['anomalies']}", flush=True)
                current_path = new_path
                # New hour file might not exist yet — poll.
                while not os.path.exists(current_path):
                    time.sleep(poll_interval_s)
                f = open(current_path, "rb")
                buf = ""
                continue
            time.sleep(poll_interval_s)
    except KeyboardInterrupt:
        # Final flush
        if buf:
            buf = tail_process_lines(buf, handle_line)
        for coin in bookset.take_dirty():
            if only_coin and coin != only_coin:
                continue
            emit(coin, bookset, pub_socket, print_only, verbose, stats, max_levels)
        ws_stop_event.set()
        if continuous_ws is not None:
            ws_stats = continuous_ws.stats
            print(f"\n=== interrupted ===", flush=True)
            print(f"  WS thread: msgs={ws_stats['msgs_received']} "
                  f"reseeds={ws_stats['reseeds']} caught_up={ws_stats['caught_up']} "
                  f"reconnects={ws_stats['reconnects']}", flush=True)
        else:
            print(f"\n=== interrupted ===", flush=True)
        print(f"final stats: lines={stats['lines']} blocks={stats['blocks']} "
              f"events_applied={bookset.events_seen} emitted={stats['emitted']} "
              f"anomalies={stats['anomalies']} skipped={stats['skipped']} "
              f"rotations={stats['rotations']}", flush=True)


def run_tail_fills(data_dir: str, endpoint: str, print_only: bool,
                   verbose: bool, only_coin: Optional[str],
                   blacklist: frozenset,
                   from_start: bool, poll_interval_s: float) -> None:
    """Tail `node_fills_streaming/{YYYYMMDD}/{H}` files. HL emits one event per
    counterparty per fill; we pair by `tid` and emit a single PbTrade
    representing the matched trade.

    `passive_was_buyer` semantics (to match what BookManager expects):
        passive_was_buyer = (maker.side == 'B')
    where the maker is the side with `crossed == false` (the resting order).
    """
    pub_socket = None
    if not print_only:
        import zmq
        ctx = zmq.Context()
        pub_socket = ctx.socket(zmq.PUB)
        pub_socket.bind(endpoint)
        print(f"PUB bound to {endpoint}", flush=True)
        time.sleep(0.3)

    # tid -> (user, fill_dict, arrival_wall_time, node_rx_ms)
    pending: Dict[int, Tuple[str, dict, float, Optional[int]]] = {}
    PENDING_TTL_S = 60.0
    stats = {"events": 0, "pairs_emitted": 0, "unpaired_flushed": 0,
             "rotations": 0, "lines": 0, "anomalies": 0}

    def emit_pair(side_a: Tuple[str, dict, float, Optional[int]],
                  side_b: Tuple[str, dict, float, Optional[int]]) -> None:
        # The maker has crossed == False (resting order).
        if not side_a[1]["crossed"]:
            maker, taker = side_a, side_b
        else:
            maker, taker = side_b, side_a
        if maker[1]["crossed"]:
            # Both sides crossed — shouldn't happen; flag as anomaly and skip.
            stats["anomalies"] += 1
            return
        maker_fill = maker[1]
        taker_fill = taker[1]
        if maker_fill["side"] == taker_fill["side"]:
            # Both same side — shouldn't happen; flag.
            stats["anomalies"] += 1
            return
        coin = maker_fill["coin"]
        passive_was_buyer = (maker_fill["side"] == "B")
        if passive_was_buyer:
            buy_oid = maker_fill["oid"]
            sell_oid = taker_fill["oid"]
        else:
            buy_oid = taker_fill["oid"]
            sell_oid = maker_fill["oid"]

        if print_only:
            if verbose or stats["pairs_emitted"] < 10:
                aggressor = "seller" if passive_was_buyer else "buyer"
                print(f"  {coin:14s} px={maker_fill['px']:>10} sz={maker_fill['sz']:>10} "
                      f"buy={buy_oid} sell={sell_oid} aggressor={aggressor}")
        else:
            msg = mdmsg_pb2.PbMessage()
            msg.symbol_id = coin
            msg.market = HYPERLIQUID_MARKET
            rx_candidates = [x for x in (side_a[3], side_b[3]) if x is not None]
            if rx_candidates:
                msg.rx_timestamp = max(rx_candidates)
            trade = msg.book_trade
            trade.px = maker_fill["px"]
            trade.qty = maker_fill["sz"]
            trade.trade_id = int(maker_fill["tid"])
            trade.buy_ord_id = int(buy_oid)
            trade.sell_ord_id = int(sell_oid)
            # HL "time" field is millis since epoch; PbTrade.trade_time also
            # treats this as ms (consistent with what pymultifeed does).
            trade.trade_time = int(maker_fill["time"])
            trade.passive_was_buyer = passive_was_buyer
            pub_socket.send(msg.SerializeToString(), copy=False)
        stats["pairs_emitted"] += 1

    def handle_line(line: str) -> None:
        stats["lines"] += 1
        try:
            packet = json.loads(line)
        except json.JSONDecodeError:
            return
        packet_rx_ms = parse_node_time_ms(packet.get("local_time"))
        for ev in packet.get("events", []):
            # Each event is [user_addr, fill_dict]
            if not isinstance(ev, list) or len(ev) != 2:
                continue
            user, fill = ev[0], ev[1]
            stats["events"] += 1
            coin = fill.get("coin")
            if only_coin and coin != only_coin:
                continue
            if coin in blacklist:
                continue
            tid = fill["tid"]
            now = time.time()
            other = pending.pop(tid, None)
            if other is not None:
                emit_pair(other, (user, fill, now, packet_rx_ms))
            else:
                pending[tid] = (user, fill, now, packet_rx_ms)
        # Periodic cleanup of unmatched pending tids
        if stats["events"] and stats["events"] % 5000 == 0:
            cutoff = time.time() - PENDING_TTL_S
            stale = [t for t, (_, _, ts, _) in pending.items() if ts < cutoff]
            for t in stale:
                pending.pop(t, None)
            stats["unpaired_flushed"] += len(stale)
            if verbose and stale:
                print(f"  [flushed {len(stale)} stale unpaired tids; pending={len(pending)}]")

    print(f"TAIL FILLS mode: watching {data_dir}", flush=True)
    current_path = hourly_path(data_dir)
    print(f"current hour file: {current_path}", flush=True)
    while not os.path.exists(current_path):
        time.sleep(poll_interval_s)
        new_path = hourly_path(data_dir)
        if new_path != current_path:
            current_path = new_path

    f = open(current_path, "rb")
    if not from_start:
        f.seek(0, os.SEEK_END)
    buf = ""
    try:
        while True:
            chunk = f.read(65536)
            if chunk:
                buf += chunk.decode("utf-8", errors="replace")
                buf = tail_process_lines(buf, handle_line)
                continue
            new_path = hourly_path(data_dir)
            if new_path != current_path:
                # FINAL DRAIN before rotation — same rationale as the
                # books tail. Without this, fill events written in the
                # last ~100 ms of an hour are dropped, leaving one side
                # of a pair stuck in `pending` and eventually flushed
                # as stale. See the 4-tid investigation in
                # hl_publisher_perf_followups_*.md.
                time.sleep(1.0)
                drained_lines = 0
                empty_reads = 0
                while empty_reads < 3:
                    chunk2 = f.read(65536)
                    if chunk2:
                        buf += chunk2.decode("utf-8", errors="replace")
                        buf = tail_process_lines(buf, handle_line)
                        drained_lines += chunk2.count(b"\n")
                        empty_reads = 0
                    else:
                        empty_reads += 1
                        time.sleep(0.1)
                f.close()
                stats["rotations"] += 1
                print(f"\n=== rotation {stats['rotations']}: {current_path} -> {new_path} "
                      f"(drained {drained_lines} late lines) ===", flush=True)
                print(f"  cumulative: lines={stats['lines']} events={stats['events']} "
                      f"pairs={stats['pairs_emitted']} unpaired_flushed={stats['unpaired_flushed']} "
                      f"anomalies={stats['anomalies']}", flush=True)
                current_path = new_path
                while not os.path.exists(current_path):
                    time.sleep(poll_interval_s)
                f = open(current_path, "rb")
                buf = ""
                continue
            time.sleep(poll_interval_s)
    except KeyboardInterrupt:
        if buf:
            buf = tail_process_lines(buf, handle_line)
        print(f"\n=== interrupted ===", flush=True)
        print(f"final: lines={stats['lines']} events={stats['events']} "
              f"pairs={stats['pairs_emitted']} pending_unmatched={len(pending)} "
              f"unpaired_flushed={stats['unpaired_flushed']} anomalies={stats['anomalies']} "
              f"rotations={stats['rotations']}", flush=True)


def run(path: str, endpoint: str, print_only: bool, verbose: bool,
        only_coin: str | None, blacklist: frozenset, throttle_s: float,
        max_levels: int = 0) -> None:
    pub_socket = None
    if not print_only:
        import zmq
        ctx = zmq.Context()
        pub_socket = ctx.socket(zmq.PUB)
        pub_socket.bind(endpoint)
        print(f"PUB bound to {endpoint}", flush=True)
        # SUBs need a moment to attach before we start sending.
        time.sleep(0.3)

    bookset = BookSet()
    stats = {"emitted": 0, "anomalies": 0, "blocks": 0, "lines": 0, "skipped": 0}
    last_throttle_t = 0.0

    with open(path, "r") as f:
        for line in f:
            stats["lines"] += 1
            try:
                packet = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"line {stats['lines']}: bad JSON ({e})")
                continue

            block_no = packet.get("block_number", -1)
            new_block_time_ms = parse_node_time_ms(packet.get("block_time"))
            new_local_time_ms = parse_node_time_ms(packet.get("local_time"))
            if block_no != bookset.current_block:
                # New block boundary — flush previous block's dirty coins.
                for coin in bookset.take_dirty():
                    if only_coin and coin != only_coin:
                        continue
                    if coin in blacklist:
                        continue
                    emit(coin, bookset, pub_socket, print_only, verbose, stats, max_levels)
                if new_block_time_ms is not None:
                    bookset.latest_block_time_ms = new_block_time_ms
                if new_local_time_ms is not None:
                    bookset.latest_local_time_ms = new_local_time_ms
                bookset.current_block = block_no
                stats["blocks"] += 1
                if verbose and stats["blocks"] % 50 == 0:
                    print(f"  ... block {block_no} (cumulative events={bookset.events_seen})")
            else:
                if new_local_time_ms is not None:
                    bookset.latest_local_time_ms = max(bookset.latest_local_time_ms,
                                                       new_local_time_ms)

            for ev in packet.get("events", []):
                coin = ev.get("coin")
                if only_coin and coin != only_coin:
                    stats["skipped"] += 1
                    continue
                if coin in blacklist:
                    stats["skipped"] += 1
                    continue
                bookset.apply_event(ev)

            if throttle_s > 0 and (time.time() - last_throttle_t) < throttle_s:
                continue
            last_throttle_t = time.time()

    # Final flush
    for coin in bookset.take_dirty():
        if only_coin and coin != only_coin:
            continue
        if coin in blacklist:
            continue
        emit(coin, bookset, pub_socket, print_only, verbose, stats, max_levels)

    print(f"\n=== done ===")
    print(f"lines={stats['lines']} blocks={stats['blocks']} "
          f"events_applied={bookset.events_seen} emitted={stats['emitted']} "
          f"anomalies={stats['anomalies']} skipped={stats['skipped']}")
    print(f"per-symbol book counts:")
    for coin, b in sorted(bookset.books.items()):
        print(f"  {coin:14s} bids={len(b.bids):3d} asks={len(b.asks):3d}")


def main() -> None:
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="single JSONL file (book diffs; read end-to-end then exit)")
    src.add_argument("--tail-dir",
                     help="book-diff data dir to tail, e.g. /home/ubuntu/hl/data/node_raw_book_diffs_streaming/hourly")
    src.add_argument("--tail-fills-dir",
                     help="fills data dir to tail, e.g. /home/ubuntu/hl/data/node_fills_streaming/hourly")
    p.add_argument("--endpoint", default="ipc:///tmp/hl_node_sock")
    p.add_argument("--print-only", action="store_true", help="don't publish, just print")
    p.add_argument("--coin", default=None, help="optionally focus on one coin")
    p.add_argument("--coins-blacklist-file", default=None,
                   help="path to a file with one coin name per line; "
                        "these coins are skipped entirely (no state, no emit). "
                        "Lines starting with # are ignored as comments.")
    p.add_argument("--throttle", type=float, default=0.0, help="min seconds between emits per block")
    p.add_argument("--from-start", action="store_true",
                   help="tail mode: read current hour file from start (default: jump to end)")
    p.add_argument("--poll-interval", type=float, default=0.1,
                   help="tail mode: seconds between polls when no new data")
    p.add_argument("--bootstrap", action="store_true",
                   help="book-diff tail mode: seed initial L2 book state from public WS l2Book "
                        "snapshots before tailing diffs. Otherwise the published book is "
                        "incomplete for ~1 hour after startup.")
    p.add_argument("--public-ws-url", default=HL_PUBLIC_WS_URL,
                   help="HL public WS URL used during --bootstrap")
    p.add_argument("--info-url", default=HL_PUBLIC_INFO_URL,
                   help="HL /info URL used during --bootstrap for sym discovery")
    p.add_argument("--bootstrap-timeout", type=float, default=15.0,
                   help="seconds to wait for snapshots during --bootstrap")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--max-levels", type=int, default=40,
                   help="Max book levels per side to publish (default 40). "
                        "Use 0 or negative to emit all levels.")
    p.add_argument("--trace-pxs", default=None,
                   help="comma-sep list of <side>:<px> to log all L3 ops on, "
                        "e.g. 'B:28411,A:28412'. Side is B or A.")
    p.add_argument("--trace-out", default=None,
                   help="file to write trace lines to (default stderr when "
                        "--trace-pxs is set)")
    p.add_argument("--watch-pxs", default=None,
                   help="comma-sep list of <side>:<px> to dump real+synth+emit "
                        "state at every block-emit. Use to inspect emit-time "
                        "state for a specific price.")
    p.add_argument("--watch-out", default=None,
                   help="file to write watch lines to (default stderr when "
                        "--watch-pxs is set)")
    p.add_argument("--log-rule1", action="store_true",
                   help="log every Rule 1 skip in BookSet.reseed_synth_from_ws "
                        "with its would_cross/would_lock/would_inside_spread "
                        "classification. Off by default (would spam in "
                        "multi-coin / long-running scenarios).")
    p.add_argument("--log-rule1-out", default=None,
                   help="file to write Rule 1 log lines to (default stderr "
                        "when --log-rule1 is set)")
    args = p.parse_args()

    # Wire up trace if requested
    global _TRACE_PXS, _TRACE_FH, _WATCH_PXS, _WATCH_FH, _RULE1_LOG_FH
    if args.trace_pxs:
        pairs = set()
        for tok in args.trace_pxs.split(","):
            tok = tok.strip()
            if not tok:
                continue
            side, px = tok.split(":", 1)
            assert side in ("B", "A"), f"trace side must be B or A, got {side!r}"
            pairs.add((side, px))
        _TRACE_PXS = pairs
        _TRACE_FH = open(args.trace_out, "w") if args.trace_out else sys.stderr
        print(f"L3 op trace on for {sorted(pairs)} -> "
              f"{args.trace_out or '(stderr)'}", flush=True)
    if args.watch_pxs:
        pairs = set()
        for tok in args.watch_pxs.split(","):
            tok = tok.strip()
            if not tok:
                continue
            side, px = tok.split(":", 1)
            assert side in ("B", "A"), f"watch side must be B or A, got {side!r}"
            pairs.add((side, px))
        _WATCH_PXS = pairs
        _WATCH_FH = open(args.watch_out, "w") if args.watch_out else sys.stderr
        print(f"Per-emit watch on for {sorted(pairs)} -> "
              f"{args.watch_out or '(stderr)'}", flush=True)
    if args.log_rule1:
        _RULE1_LOG_FH = (open(args.log_rule1_out, "w")
                         if args.log_rule1_out else sys.stderr)
        print(f"Rule 1 skip log on -> {args.log_rule1_out or '(stderr)'}",
              flush=True)

    # Load blacklist from file (one coin per line, # comments allowed)
    blacklist: frozenset = frozenset()
    if args.coins_blacklist_file:
        coins = []
        with open(args.coins_blacklist_file) as bf:
            for line in bf:
                s = line.strip()
                if s and not s.startswith("#"):
                    coins.append(s)
        blacklist = frozenset(coins)
        print(f"loaded {len(blacklist)} coins to blacklist from {args.coins_blacklist_file}", flush=True)

    if args.file:
        run(args.file, args.endpoint, args.print_only, args.verbose, args.coin,
            blacklist, args.throttle, max_levels=args.max_levels)
    elif args.tail_dir:
        run_tail(args.tail_dir, args.endpoint, args.print_only, args.verbose,
                 args.coin, blacklist, args.from_start, args.poll_interval,
                 bootstrap=args.bootstrap,
                 public_ws_url=args.public_ws_url,
                 info_url=args.info_url,
                 bootstrap_timeout_s=args.bootstrap_timeout,
                 max_levels=args.max_levels)
    else:
        run_tail_fills(args.tail_fills_dir, args.endpoint, args.print_only,
                       args.verbose, args.coin, blacklist, args.from_start, args.poll_interval)


if __name__ == "__main__":
    main()
