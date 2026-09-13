#! /usr/bin/env python3

"""
Daemon script that subscribes to a Hyperliquid wallet's perp fills via WebSocket
and writes them to CSV files compatible with pnl.py.

Usage:
    python hl_trade_sub.py <wallet_address> --output-dir <dir>
"""

import argparse
import csv
import glob
import json
import logging
import os
import sys
import threading
import time
import datetime as dt
from io import StringIO
from zoneinfo import ZoneInfo

import requests
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL
from hyperliquid.websocket_manager import WebsocketManager

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils

NY_TZ = ZoneInfo("America/New_York")
NUM_COLS = 22
RECONNECT_DELAY_S = 5
SHUTDOWN_HOUR = 18
MARK_INTERVAL_S = 60

logger = logging.getLogger("hl_trade_sub")


class GapDetected(Exception):
    """Raised when a reconnect snapshot has no overlap with prior fills."""
    pass


class DayEnd(Exception):
    """Raised when the shutdown time is reached."""
    pass


class LibLogForwarder(logging.Handler):
    """Re-emits records from third-party loggers through our own logger, tagged
    with the originating logger's name."""

    def emit(self, record):
        logger.log(record.levelno, "[%s] %s", record.name, record.getMessage(),
                   exc_info=record.exc_info)


def capture_lib_logging():
    """Route third-party logging through our logger instead of stderr.

    websocket-client logs the expected disconnects at ERROR ("Connection to remote
    host was lost. - goodbye") on the 'websocket' logger, which carries only a
    NullHandler, so the records propagate to the root logger. Root ends up with a
    stderr handler because hyperliquid.websocket_manager uses the module-level
    logging.debug(), which triggers an implicit logging.basicConfig(). Claiming
    both loggers here (before any hyperliquid call, so basicConfig() becomes a
    no-op) keeps stderr clean while still logging the messages the way we want."""
    forwarder = LibLogForwarder()
    root = logging.getLogger()
    root.setLevel(logging.WARNING)  # don't pull in libraries' debug chatter
    root.addHandler(forwarder)
    ws_logger = logging.getLogger("websocket")
    ws_logger.setLevel(logging.INFO)  # keeps 'Websocket connected' / reconnect lines
    ws_logger.propagate = False       # already handled here; don't double-log via root
    ws_logger.addHandler(forwarder)


def log_thread_exception(args):
    """threading.excepthook replacement — library worker threads (e.g. hyperliquid's
    ping sender racing a dying socket) otherwise dump tracebacks straight to stderr."""
    if args.exc_type is SystemExit:
        return
    logger.error("Unhandled exception in thread %s",
                 args.thread.name if args.thread else "?",
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def setup_logging(log_dir):
    """Configure logging to both stdout and a dated log file."""
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    logger.addHandler(stdout_handler)

    os.makedirs(log_dir, exist_ok=True)
    date_str = trade_day(dt.datetime.now(NY_TZ))
    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"hl_trade_sub_{date_str}.log"))
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Nest email_utils under us, so its records land in our handlers above.
    email_utils.use_parent_logger(logger)

    capture_lib_logging()
    threading.excepthook = log_thread_exception


def epoch_ms_to_ny(epoch_ms):
    """Convert epoch millis to a New York datetime."""
    return dt.datetime.fromtimestamp(epoch_ms / 1000.0, tz=NY_TZ)


def trade_day(ny_dt):
    """Return the trade date string (YYYYMMDD) for a given NY datetime.
    Day boundary is 18:00 ET — trades at/after 18:00 belong to the next calendar day."""
    if ny_dt.hour >= 18:
        return (ny_dt + dt.timedelta(days=1)).strftime("%Y%m%d")
    return ny_dt.strftime("%Y%m%d")


def format_timestamp(ny_dt):
    """Format timestamp: 'YYYYMMDD HH:MM:SS.nnnnnnnnn EST'"""
    micros = ny_dt.strftime("%f")  # 6 digits
    ns_str = micros + "000"  # pad to 9 digits
    tz_abbr = ny_dt.strftime("%Z")  # EST or EDT
    return ny_dt.strftime("%Y%m%d %H:%M:%S.") + ns_str + " " + tz_abbr


def last_1800_et():
    """Return epoch millis of the most recent 18:00 ET boundary."""
    now_ny = dt.datetime.now(NY_TZ)
    boundary = now_ny.replace(hour=18, minute=0, second=0, microsecond=0)
    if now_ny < boundary:
        boundary -= dt.timedelta(days=1)
    return int(boundary.timestamp() * 1000)


def normalize_side(side):
    """Normalize side field — WS may return 'B'/'A' or 'Buy'/'Sell'."""
    if side in ("B", "Buy"):
        return "Buy"
    return "Sell"


def find_pos_file(pos_dir, ref_dt, max_lookback_days=2):
    """Locate the start-of-day positions file for the current trade day.

    The pos day is the calendar day before the trade day (trading starts at 18:00
    that day), matching webpnl.sh; this file should always exist. The one-extra-day
    lookback is only a safety net for the rare case it's missing due to an error.
    Returns a path or None."""
    pos_dir = os.path.expanduser(pos_dir)
    # trade_day(ref_dt) is pos_day + 1 calendar day, so start one day before it.
    base = dt.datetime.strptime(trade_day(ref_dt), "%Y%m%d")
    for back in range(1, max_lookback_days + 1):
        day = (base - dt.timedelta(days=back)).strftime("%Y%m%d")
        fp = os.path.join(pos_dir, f"pos_{day}.log")
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            return fp
    return None


def load_pos_symbols(pos_dir):
    """Return the set of symbols we hold, parsed from the current trade day's pos
    file. Exits if no pos file is found, since marking depends on knowing which
    positions we hold."""
    fp = find_pos_file(pos_dir, dt.datetime.now(NY_TZ))
    if fp is None:
        raise SystemExit(f"No pos file found in {os.path.expanduser(pos_dir)} "
                         f"(checked pos day and one day prior); cannot mark.")
    syms = set()
    with open(fp) as f:
        for line in f:
            if line.startswith("***"):
                break
            parts = line.split()
            if parts:
                syms.add(parts[0].rstrip(":"))
    logger.info("Loaded %d position symbols to mark from %s", len(syms), fp)
    return syms


class FillWriter:
    """Handles CSV writing and deduplication for fills."""

    def __init__(self, output_dir):
        self.output_dir = os.path.expanduser(output_dir)
        self.seen_tids = set()  # set of tid values (int or str)
        today_str = trade_day(dt.datetime.now(NY_TZ))
        self.trades_file = os.path.join(self.output_dir, f"trades_{today_str}.csv")

    def load_existing(self):
        """Scan current trade-day CSV file to populate dedup set from tid in col[8]."""
        fp = self.trades_file
        count = 0
        if os.path.exists(fp):
            try:
                with open(fp) as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if len(row) > 8 and row[8]:
                            self.seen_tids.add(row[8])
                            count += 1
            except Exception as e:
                logger.warning("Error reading %s for dedup: %s", fp, e)
        logger.info("Loaded %d existing tids from %s", count, fp)

    def _fill_to_row(self, fill):
        """Convert a fill dict to a 22-column CSV row list."""
        epoch_ms = fill["time"]
        ny_dt = epoch_ms_to_ny(epoch_ms)

        row = [""] * NUM_COLS
        row[0] = format_timestamp(ny_dt)
        row[1] = str(epoch_ms)
        row[2] = str(fill["oid"])
        row[3] = fill["coin"]
        row[4] = "Hyperliquid"
        row[5] = normalize_side(fill["side"])
        row[6] = fill["px"]
        row[7] = fill["sz"]
        row[8] = str(fill["tid"])
        row[10] = fill["fee"]
        row[20] = "REM" if fill["crossed"] else "ADD"
        row[21] = fill.get("cloid") or "NOCLOID"
        return row

    def write(self, fill):
        """Write a single fill to CSV. Returns True if written (not a duplicate)."""
        tid = str(fill["tid"])

        if tid in self.seen_tids:
            return False

        row = self._fill_to_row(fill)
        fp = self.trades_file

        buf = StringIO()
        csv.writer(buf).writerow(row)
        with open(fp, "a") as f:
            f.write(buf.getvalue())
            f.flush()

        self.seen_tids.add(tid)
        return True


class MarkWriter:
    """Writes periodic synthetic 'MARK' rows (0-size re-marks) to a separate CSV.

    These let pnl.py re-price open positions to the latest mid even when no trade
    occurs, so the time-series graph doesn't flatline-then-spike for a big position
    in a symbol that isn't trading. Kept in a separate file so the real trades file
    stays clean; pnl.py combines and sorts all files by timestamp anyway."""

    def __init__(self, output_dir):
        self.output_dir = os.path.expanduser(output_dir)
        today_str = trade_day(dt.datetime.now(NY_TZ))
        self.marks_file = os.path.join(self.output_dir, f"marks_{today_str}.csv")

    def write_mids(self, mids):
        """Append one MARK row per (symbol, mid) at the current time. Returns count."""
        now_ny = dt.datetime.now(NY_TZ)
        ts = format_timestamp(now_ny)
        epoch_ms = str(int(now_ny.timestamp() * 1000))
        buf = StringIO()
        writer = csv.writer(buf)
        for sym, px in mids.items():
            row = [""] * NUM_COLS
            row[0] = ts
            row[1] = epoch_ms
            row[3] = sym
            row[5] = "Buy"  # unused for MARK rows, but keep the column populated
            row[6] = str(px)
            row[7] = "0"
            row[20] = "MARK"
            writer.writerow(row)
        with open(self.marks_file, "a") as f:
            f.write(buf.getvalue())
            f.flush()
        return len(mids)


class OrderUpdateWriter:
    """Appends raw orderUpdates WS messages to a JSONL file, one per line,
    wrapped in {rx_ms, msg}. No parsing — purely for offline analysis."""

    def __init__(self, output_dir):
        self.output_dir = os.path.expanduser(output_dir)
        today_str = trade_day(dt.datetime.now(NY_TZ))
        self.path = os.path.join(self.output_dir, f"orders_{today_str}.jsonl")

    def write(self, msg):
        line = json.dumps({"rx_ms": int(time.time() * 1000), "msg": msg},
                          separators=(",", ":"))
        with open(self.path, "a") as f:
            f.write(line + "\n")
            f.flush()


class ClearingStateWriter:
    """Appends clearinghouseState snapshots to JSONL, one line per (dex, poll)."""

    def __init__(self, output_dir):
        self.output_dir = os.path.expanduser(output_dir)
        today_str = trade_day(dt.datetime.now(NY_TZ))
        self.path = os.path.join(self.output_dir, f"clearinghouse_{today_str}.jsonl")

    def write(self, dex, state):
        line = json.dumps(
            {"rx_ms": int(time.time() * 1000), "dex": dex or "(main)", "state": state},
            separators=(",", ":"))
        with open(self.path, "a") as f:
            f.write(line + "\n")
            f.flush()


class HLTradeSubscriber:
    """Subscribes to Hyperliquid wallet fills and writes CSVs."""

    def __init__(self, address, output_dir, pos_dir, mark_interval=None, ntfy=None,
                 subscribe_orders=False, clearing_interval=None):
        if address is not None:
            self.address = address
        else:
            creds_path = os.path.expanduser("~/.creds/.Hyperliquid.creds.json")
            with open(creds_path) as f:
                hyper_creds = json.load(f)
            self.address = hyper_creds["address"]
        self.output_dir = os.path.expanduser(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.info = Info(MAINNET_API_URL, skip_ws=True)
        self.writer = FillWriter(self.output_dir)
        # Periodic position re-marking is opt-in (mark_interval is None when off),
        # so the script can be used purely for recording trades without needing a
        # pos file or doing the extra allMids work.
        self.mark_interval = mark_interval
        if mark_interval is not None:
            self.mark_writer = MarkWriter(self.output_dir)
            # Only re-mark symbols we actually hold, per the start-of-day pos file.
            self.mark_syms = load_pos_symbols(pos_dir)
        else:
            self.mark_writer = None
            self.mark_syms = None
        self.subscribe_orders = subscribe_orders
        self.order_writer = OrderUpdateWriter(self.output_dir) if subscribe_orders else None
        self.clearing_interval = clearing_interval
        self.clearing_writer = ClearingStateWriter(self.output_dir) if clearing_interval else None
        self.has_connected = False
        # Compute the next 18:00 ET as our shutdown deadline
        now_ny = dt.datetime.now(NY_TZ)
        shutdown = now_ny.replace(hour=SHUTDOWN_HOUR, minute=0, second=0, microsecond=0)
        if now_ny >= shutdown:
            shutdown += dt.timedelta(days=1)
        self.shutdown_time = shutdown
        # Remember ntfy recipient if given
        self.ntfy = ntfy

    def backfill(self):
        """Query historical fills since the last 18:00 ET boundary."""
        start_ms = last_1800_et()
        logger.info("Backfilling fills since %s",
                    epoch_ms_to_ny(start_ms).strftime("%Y-%m-%d %H:%M:%S %Z"))
        total = 0
        cursor = start_ms
        while True:
            fills = self.info.user_fills_by_time(self.address, cursor)
            if not fills:
                break
            written = 0
            for fill in fills:
                self._track_symbol(fill["coin"])
                if self.writer.write(fill):
                    written += 1
                    logger.debug(f"BACKFILL: {fill}")
            total += written
            logger.info("Backfill batch: %d fills fetched, %d new written", len(fills), written)
            if len(fills) < 2000:
                break
            # Page forward using the last fill's time
            cursor = fills[-1]["time"]
            # To avoid 429s, space out the queries so we don't exceed 1200/min limit.
            # Each query costs 1, and returns up to 2000 responses => 1 + 2000 / 20 = 101 total.
            # So cap at no more than 1200 / 101 = 11.9 queries per min => sleep 6 between queries.
            logger.info("Sleeping 6s before next backfill query")
            time.sleep(6)
        logger.info("Backfill complete: %d new fills written", total)

    def _track_symbol(self, coin):
        """Ensure a traded symbol is in the re-mark set, so positions opened
        intraday (not in the start-of-day pos file) start getting re-marked."""
        if self.mark_syms is None:
            return
        if coin not in self.mark_syms:
            self.mark_syms.add(coin)
            logger.info("Now marking newly traded symbol %s", coin)

    def _get_all_mids(self):
        """Query allMids for both the main perp dex and the xyz dex.

        Mirrors pnl.py's get_current_mids so the symbol keys here match the ones
        pnl.py expects when matching marks against accounts."""
        endpt = MAINNET_API_URL + "/info"
        mids = {}
        for payload in ({"type": "allMids"}, {"type": "allMids", "dex": "xyz"}):
            r = requests.post(endpt, json=payload, timeout=10,
                              headers={"Content-Type": "application/json"})
            r.raise_for_status()
            for sym, px in r.json().items():
                mids[sym] = px
        return mids

    def _mark_loop(self, stop_event):
        """Periodically snapshot all mids into the marks file until told to stop."""
        while not stop_event.is_set():
            try:
                mids = {s: p for s, p in self._get_all_mids().items()
                        if s in self.mark_syms}
                n = self.mark_writer.write_mids(mids)
                logger.debug("Wrote %d marks to %s", n, self.mark_writer.marks_file)
            except Exception as e:
                logger.warning("Mark snapshot failed: %s", e)
            stop_event.wait(self.mark_interval)

    def _clearing_loop(self, stop_event):
        """Periodically poll clearinghouseState for both the main perp dex and
        the xyz dex, dumping each raw response to the clearinghouse JSONL."""
        while not stop_event.is_set():
            for dex in ("", "xyz"):
                try:
                    state = self.info.user_state(self.address, dex=dex)
                    self.clearing_writer.write(dex, state)
                except Exception as e:
                    logger.warning("Clearinghouse poll failed (dex=%s): %s",
                                   dex or "(main)", e)
            stop_event.wait(self.clearing_interval)

    def _on_ws_message(self, msg):
        """Callback for WebSocket fill messages."""
        if msg.get("channel") != "userFills":
            logger.debug("Ignoring non-userFills message: %s", msg.get("channel"))
            return

        data = msg["data"]
        is_snapshot = data.get("isSnapshot", False)
        fills = data.get("fills", [])
        if is_snapshot:
            cutoff_ms = last_1800_et()
            pre_cutoff = len(fills)
            fills = [f for f in fills if f["time"] >= cutoff_ms]
            logger.info("Processing WS snapshot: %d fills, %d after 18:00 cutoff",
                        pre_cutoff, len(fills))

        written = 0
        for fill in fills:
            coin = fill["coin"]
            self._track_symbol(coin)
            if self.writer.write(fill):
                written += 1
                side = normalize_side(fill["side"])
                sz, px = fill["sz"], fill["px"]
                source = "GW" if fill.get("cloid") else "UI"
                logger.debug(f"FILL: {fill}")
                # logger.info("FILL %s %s %s@%s fee=%s source=%s",
                #             coin, side, sz, px, fill["fee"], source)
                if self.ntfy and source == "UI":
                    email_utils.send_ntfy_alert(
                        msg=f"{coin}: {side} {sz}@{px}", title=f"UI fill for {coin}", to=self.ntfy)

        if is_snapshot and self.has_connected and fills and written == len(fills):
            logger.warning("Reconnect snapshot had 0 overlapping fills (%d written, %d total) "
                           "— gap in coverage, will backfill", written, len(fills))
            raise GapDetected()
        if is_snapshot:
            self.has_connected = True

    def _run_ws(self):
        """Run a single WebSocket session. Returns when the connection drops."""
        wsm = WebsocketManager(MAINNET_API_URL)
        wsm.daemon = True
        self._ws_exception = None

        def on_message_wrapper(msg):
            try:
                self._on_ws_message(msg)
            except GapDetected as e:
                self._ws_exception = e
                wsm.stop()

        wsm.subscribe({"type": "userFills", "user": self.address}, on_message_wrapper)
        if self.subscribe_orders:
            wsm.subscribe({"type": "orderUpdates", "user": self.address},
                          self.order_writer.write)
            logger.info("Also subscribed to orderUpdates -> %s",
                        self.order_writer.path)
        logger.info("WebSocket connecting for %s", self.address)
        wsm.start()
        try:
            while wsm.is_alive():
                remaining = (self.shutdown_time - dt.datetime.now(NY_TZ)).total_seconds()
                if remaining <= 0:
                    logger.info("Reached shutdown time %s ET, exiting",
                                self.shutdown_time.strftime("%Y-%m-%d %H:%M"))
                    wsm.stop()
                    wsm.join()
                    raise DayEnd()
                wsm.join(timeout=remaining)
        except KeyboardInterrupt:
            logger.info("Interrupted, shutting down")
            wsm.stop()
            raise
        # Re-raise gap exception from the callback
        if self._ws_exception is not None:
            raise self._ws_exception
        # Log why we exited
        if not wsm.ws.keep_running:
            close_code = getattr(wsm.ws, "close_status_code", None)
            close_reason = getattr(wsm.ws, "close_reason", None)
            logger.warning("WebSocket disconnected: code=%s reason=%s",
                           close_code, close_reason)

    def run(self):
        """Main loop: backfill, then subscribe with auto-reconnect."""
        self.writer.load_existing()
        self.backfill()

        logger.info("Starting WebSocket subscription for %s", self.address)
        logger.info("Writing fills to %s", os.path.abspath(self.output_dir))

        mark_stop = None
        if self.mark_interval is not None:
            mark_stop = threading.Event()
            mark_thread = threading.Thread(target=self._mark_loop, args=(mark_stop,), daemon=True)
            mark_thread.start()
            logger.info("Started periodic mark writer (every %ds) -> %s",
                        self.mark_interval, self.mark_writer.marks_file)

        clearing_stop = None
        if self.clearing_interval is not None:
            clearing_stop = threading.Event()
            clearing_thread = threading.Thread(target=self._clearing_loop,
                                               args=(clearing_stop,), daemon=True)
            clearing_thread.start()
            logger.info("Started clearinghouseState poller (every %ds) -> %s",
                        self.clearing_interval, self.clearing_writer.path)

        try:
            while True:
                try:
                    self._run_ws()
                except (KeyboardInterrupt, DayEnd):
                    break
                except GapDetected:
                    logger.info("Re-running backfill to cover gap")
                    self.writer.load_existing()
                    self.backfill()
                logger.warning("Reconnecting in %ds...", RECONNECT_DELAY_S)
                self.writer.load_existing()
                time.sleep(RECONNECT_DELAY_S)
        finally:
            if mark_stop is not None:
                mark_stop.set()
            if clearing_stop is not None:
                clearing_stop.set()


def main():
    parser = argparse.ArgumentParser(description="Subscribe to Hyperliquid wallet fills")
    parser.add_argument("--address", help="Wallet address to subscribe to")
    parser.add_argument("--output-dir", default=".", help="Directory to write CSV and log files")
    parser.add_argument("--pos-dir", default="~/scratch/start_pos",
                        help="Directory holding pos_<YYYYMMDD>.log files (default: ~/scratch/start_pos)")
    parser.add_argument("--mark", dest="mark_interval", nargs="?", type=int,
                        const=MARK_INTERVAL_S, default=None, metavar="SECONDS",
                        help=f"Periodically re-mark open positions to the latest mid "
                             f"(needs a pos file). Optionally set the interval in seconds "
                             f"(default {MARK_INTERVAL_S}). Omit to disable marking.")
    parser.add_argument("--order-updates", action="store_true",
                        help="Also subscribe to orderUpdates and dump raw messages "
                             "to orders_<day>.jsonl (no parsing)")
    parser.add_argument("--clearing-interval", type=int, default=None, metavar="SECONDS",
                        help="Poll clearinghouseState (main + dex=xyz) every N seconds, "
                             "dumping raw responses to clearinghouse_<day>.jsonl")
    parser.add_argument("--ntfy", nargs="?", const="kdb", default=None,
                        choices=list(email_utils.NTFY_TOPICS.keys()),
                        help="ntfy recipient for UI-sourced fills (default: kdb)")
    args = parser.parse_args()

    setup_logging(args.output_dir)

    sub = HLTradeSubscriber(args.address, args.output_dir, args.pos_dir,
                            mark_interval=args.mark_interval, subscribe_orders=args.order_updates,
                            clearing_interval=args.clearing_interval, ntfy=args.ntfy)
    sub.run()


if __name__ == "__main__":
    main()
