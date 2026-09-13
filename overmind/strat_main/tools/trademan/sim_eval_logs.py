"""Parse pktrade INFO logs for actionable events.

Parses glog-format INFO logs from pktrade for:
  - Usermsg (UM) events: thresh mult changes, throttle level overrides
  - Position inheritance: global positioner inherit events
  - Risk level transitions: TL0-TL5
  - Cancel reject pauses
  - Feed latency (ms_behind per symbol)
  - Order event anomalies (cancel rejects, ack-not-found, erase-non-existent)
  - Overnight position load attempts

Log format: I{YYYYMMDD} {HH:MM:SS.uuuuuu} {threadid} {source}:{line}] {msg}
"""

import os
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import pytz

# ── glog line parser ─────────────────────────────────────────────────────
_GLOG_RE = re.compile(
    r"^[IW](\d{8})\s+(\d{2}:\d{2}:\d{2}\.\d+)\s+\d+\s+(\S+):(\d+)\]\s+(.*)$"
)

# ── event matchers ───────────────────────────────────────────────────────
# Each matcher: (event_type, source_file_or_None, line_num_or_None, regex_on_msg)

# Usermsg received and applied
_UM_RECEIVED_RE = re.compile(
    r"Received UM targeting strat_id (\S+): "
    r"timestamp:(\S+) user:(\S+) strat_id_regex:(\S+) sym:(\S*) "
    r"um_id:(\d+) args:(\{.*\})"
)
_UM_IGNORED_STRAT_RE = re.compile(
    r"Ignoring UM not targeted at strat_id (\S+): "
    r"timestamp:(\S+) user:(\S+) strat_id_regex:(\S+) sym:(\S*) "
    r"um_id:(\d+) args:(\{.*\})"
)
_UM_PROCESSING_RE = re.compile(
    r"Processing UM for (\S+) trading (\S+): "
    r"timestamp:(\S+) user:(\S+) strat_id_regex:(\S+) sym:(\S*) "
    r"um_id:(\d+) args:(\{.*\})"
)
_UM_EFFECT_RE = re.compile(
    r"UM: (.+)"
)
# Thresh mult, from a usermsg or from a bleed/minfv widen. The applied mult is
# the widest of the per-source slots, reported as "eff".
_THRESH_SET_RE = re.compile(
    r"\((\S+)\) Thresh mult set to ([\d.e+-]+) from (\w+)\. "
    r"um ([\d.e+-]+) bleed ([\d.e+-]+) minfv ([\d.e+-]+) eff ([\d.e+-]+)\. "
    r"Final threshes: ([\d.e+-]+) x ([\d.e+-]+)"
)

# Position inheritance
_INHERITED_RE = re.compile(
    r"\((\S+)\) Inherited position \(([\d.e+-]+)\)\. New position: ([\d.e+-]+)"
)
_INHERIT_CALC_RE = re.compile(
    r"\((\S+)\) GPCalc: median global position ([\d.e+-]+), "
    r"excess position ([\d.e+-]+), .* my_inherit ([\d.e+-]+)"
)

# Risk level transitions
_TL_RE = re.compile(
    r"\((\S+)\) (TL\d): (.+?) \(pos ([\d.e+-]+)\)"
)

# Cancel reject pauses
_PAUSE_RE = re.compile(
    r"\((\S+)\) Pausing trading for (\d+) seconds because of: '(.+)'"
)

# Overnight position loads
_OVERNIGHT_RE = re.compile(
    r"Tried to load_overnight_pos with ([\d.e+-]+) for (\S+) wasn't set up for it"
)

# setSizeMult at startup (Conf), from a usermsg (Usermsg), or from the
# double-down detector (DoubleDown). The applied mult is the product of the
# per-source slots, reported as "eff".
_SIZE_MULT_RE = re.compile(
    r"\((\S+)\) setSizeMult \(([\d.e+-]+) from (\w+)\) "
    r"conf ([\d.e+-]+) um ([\d.e+-]+) dd ([\d.e+-]+) eff ([\d.e+-]+) "
    r"maxpos ([\d.e+-]+) -> ([\d.e+-]+)"
)

# Feed latency (ms_behind from TryFire)
_FEED_LATENCY_RE = re.compile(
    r"\((\S+)\) TryFire, .* \((\d+) ms_behind\)"
)

# Cancel order reject
_CANCEL_REJECT_RE = re.compile(
    r"\((\S+)\) TradeRiskMan: CancelOrderReject (.+)"
)

# OE ack not found in annotations
_OE_ACK_NOT_FOUND_RE = re.compile(
    r"\((\S+)\) onOE NewOrderAck: order (\d+) not found in open_ord_annotations_"
)

# Erase non-existent order (comes from WARNING lines that also appear in INFO log)
_ERASE_NONEXISTENT_RE = re.compile(
    r"\((\S+)\) Attempting to erase non-existent order ID: (\d+)"
)

# Known UM IDs (update as more are discovered)
UM_ID_NAMES = {
    "302": "SET_THRESH_MULT",
    "403": "SET_THROTTLE_LEVEL",
}


def parse_log_events(local_base, aliases, date_strs, event_types=None):
    """Scan INFO logs for events.

    Args:
        local_base: path to local tradeperf directory
        aliases: list of alias names to scan
        date_strs: list of date strings (YYYYMMDD)
        event_types: optional set of event types to collect.
            None = all. Options: "usermsg", "usermsg_ignored", "inherit",
            "risk_level", "cancel_pause", "feed_latency", "reject",
            "overnight_pos", "size_mult"

    Returns:
        list of event dicts with keys:
            date, time, alias, group, strat, event_type, sym, detail, line
    """
    events = []
    base = Path(local_base)
    if not base.exists():
        return events

    for alias_dir in sorted(base.iterdir()):
        if not alias_dir.is_dir() or alias_dir.name not in aliases:
            continue
        alias = alias_dir.name
        for group_dir in sorted(alias_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name
                for ds in date_strs:
                    # Match both `pktrade.HOST.log.INFO...` and
                    # `pktrade_0.HOST.log.INFO...` (multi-instance) patterns.
                    log_files = list(strat_dir.glob(f"pktrade*.log.INFO.{ds}-*"))
                    for log_file in log_files:
                        events.extend(
                            _parse_one_log(log_file, alias, group, strat, ds, event_types)
                        )
    return events


def _parse_one_log(log_path, alias, group, strat, date_str, event_types):
    """Parse a single INFO log for events."""
    events = []
    collect_all = event_types is None

    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                m = _GLOG_RE.match(line)
                if not m:
                    continue
                ts = m.group(2)  # HH:MM:SS.uuuuuu
                source = m.group(3)
                msg = m.group(5)

                # ── Usermsg received (applied to this strat) ──
                if (collect_all or "usermsg" in event_types) and source == "trade_carrier.cc":
                    um = _UM_RECEIVED_RE.match(msg)
                    if um:
                        um_id = um.group(6)
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "usermsg",
                            sym="",
                            detail={
                                "strat_id": um.group(1),
                                "um_timestamp": um.group(2),
                                "user": um.group(3),
                                "strat_id_regex": um.group(4),
                                "target_sym": um.group(5),
                                "um_id": um_id,
                                "um_name": UM_ID_NAMES.get(um_id, f"UNKNOWN_{um_id}"),
                                "args": um.group(7),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Usermsg ignored (not targeted at this strat) ──
                if (collect_all or "usermsg_ignored" in event_types) and source == "trade_carrier.cc":
                    um = _UM_IGNORED_STRAT_RE.match(msg)
                    if um:
                        um_id = um.group(6)
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "usermsg_ignored",
                            sym="",
                            detail={
                                "strat_id": um.group(1),
                                "um_timestamp": um.group(2),
                                "user": um.group(3),
                                "strat_id_regex": um.group(4),
                                "target_sym": um.group(5),
                                "um_id": um_id,
                                "um_name": UM_ID_NAMES.get(um_id, f"UNKNOWN_{um_id}"),
                                "args": um.group(7),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Thresh mult applied per-sym (usermsg, or a bleed/minfv widen) ──
                if (collect_all or "usermsg" in event_types
                        or "thresh_widen" in event_types) and source == "rel_wide_mm_2.cc":
                    tm = _THRESH_SET_RE.match(msg)
                    if tm:
                        mult_src = tm.group(3)
                        # Only a Usermsg-sourced mult is a usermsg effect; Bleed and
                        # MinFv write the same line from their own slots.
                        etype = ("usermsg_effect" if mult_src == "Usermsg"
                                 else "thresh_widen")
                        events.append(_evt(
                            date_str, ts, alias, group, strat, etype,
                            sym=tm.group(1),
                            detail={
                                "effect": "thresh_mult_set",
                                "mult": tm.group(2),
                                "source": mult_src,
                                "um_mult": tm.group(4),
                                "bleed_mult": tm.group(5),
                                "minfv_mult": tm.group(6),
                                "eff_mult": tm.group(7),
                                "buy_thresh": tm.group(8),
                                "sell_thresh": tm.group(9),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Position inherited ──
                if (collect_all or "inherit" in event_types) and source == "trade_risk_man.cc":
                    inh = _INHERITED_RE.match(msg)
                    if inh:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "inherit",
                            sym=inh.group(1),
                            detail={
                                "inherited_qty": inh.group(2),
                                "new_position": inh.group(3),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Inherit calculation from global positioner ──
                if (collect_all or "inherit" in event_types) and source == "global_positioner.cc":
                    gc = _INHERIT_CALC_RE.match(msg)
                    if gc:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "inherit_calc",
                            sym=gc.group(1),
                            detail={
                                "median_global_pos": gc.group(2),
                                "excess_position": gc.group(3),
                                "my_inherit": gc.group(4),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Risk level transitions ──
                if (collect_all or "risk_level" in event_types) and source == "trade_risk_man.cc":
                    tl = _TL_RE.match(msg)
                    if tl:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "risk_level",
                            sym=tl.group(1),
                            detail={
                                "level": tl.group(2),
                                "description": tl.group(3),
                                "position": tl.group(4),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Cancel reject pauses ──
                if (collect_all or "cancel_pause" in event_types) and source == "trade_risk_man.cc":
                    cp = _PAUSE_RE.match(msg)
                    if cp:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "cancel_pause",
                            sym=cp.group(1),
                            detail={
                                "pause_seconds": cp.group(2),
                                "reason": cp.group(3),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Overnight position loads ──
                if (collect_all or "overnight_pos" in event_types) and source == "trade_risk_man.cc":
                    ov = _OVERNIGHT_RE.match(msg)
                    if ov:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "overnight_pos",
                            sym=ov.group(2),
                            detail={
                                "position": ov.group(1),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── setSizeMult at startup ──
                if (collect_all or "size_mult" in event_types) and source == "trade_risk_man.cc":
                    sm = _SIZE_MULT_RE.match(msg)
                    if sm:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "size_mult",
                            sym=sm.group(1),
                            detail={
                                "multiplier": sm.group(2),
                                "source": sm.group(3),
                                "conf_mult": sm.group(4),
                                "um_mult": sm.group(5),
                                "dd_mult": sm.group(6),
                                "eff_mult": sm.group(7),
                                "old_maxpos": sm.group(8),
                                "new_maxpos": sm.group(9),
                            },
                            line=line.strip(),
                        ))
                        continue

                # ── Feed latency (ms_behind) ──
                if (collect_all or "feed_latency" in event_types) and source == "rel_wide_mm_2.cc":
                    fl = _FEED_LATENCY_RE.match(msg)
                    if fl:
                        ms = int(fl.group(2))
                        if ms <= 10000:  # filter out garbage values (remote_px=0 etc)
                            events.append(_evt(
                                date_str, ts, alias, group, strat, "feed_latency",
                                sym=fl.group(1),
                                detail={"ms_behind": ms},
                                line="",  # too many lines, don't store raw
                            ))
                        continue

                # ── Cancel order reject ──
                if (collect_all or "reject" in event_types) and source == "trade_risk_man.cc":
                    cr = _CANCEL_REJECT_RE.match(msg)
                    if cr:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "cancel_reject",
                            sym=cr.group(1),
                            detail={"reason": cr.group(2)},
                            line=line.strip(),
                        ))
                        continue

                # ── OE ack not found ──
                if (collect_all or "reject" in event_types) and source == "trade_risk_man.cc":
                    anf = _OE_ACK_NOT_FOUND_RE.match(msg)
                    if anf:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "oe_ack_not_found",
                            sym=anf.group(1),
                            detail={"order_id": anf.group(2)},
                            line="",  # high volume, skip raw
                        ))
                        continue

                # ── Erase non-existent order ──
                if (collect_all or "reject" in event_types) and source == "trade_risk_man.cc":
                    en = _ERASE_NONEXISTENT_RE.match(msg)
                    if en:
                        events.append(_evt(
                            date_str, ts, alias, group, strat, "oe_erase_nonexistent",
                            sym=en.group(1),
                            detail={"order_id": en.group(2)},
                            line="",  # high volume, skip raw
                        ))
                        continue

    except OSError as e:
        print(f"  [warn] Could not read {log_path}: {e}")
    return events


def _evt(date, time, alias, group, strat, event_type, sym="", detail=None, line=""):
    return {
        "date": date,
        "time": time,
        "alias": alias,
        "group": group,
        "strat": strat,
        "event_type": event_type,
        "sym": sym,
        "detail": detail or {},
        "line": line,
    }


def has_event(events, alias, group, strat, date_str, event_type):
    """Check if a specific event type occurred for a given strat/date."""
    for e in events:
        if (e["date"] == date_str and e["alias"] == alias and
                e["group"] == group and e["strat"] == strat and
                e["event_type"] == event_type):
            return True
    return False


def summarize_events(events):
    """Print a human-readable summary of parsed events.

    Groups by (alias, group, strat, date) and shows counts per event type,
    plus key details for usermsgs and inherits.
    """
    by_key = defaultdict(list)
    for e in events:
        key = (e["alias"], e["group"], e["strat"], e["date"])
        by_key[key].append(e)

    for key in sorted(by_key):
        alias, group, strat, date = key
        evts = by_key[key]
        counts = defaultdict(int)
        for e in evts:
            counts[e["event_type"]] += 1

        print(f"\n{'='*70}")
        print(f"  {alias}/{group}/{strat}  ({date})")
        print(f"{'='*70}")

        # Event counts
        for etype in ["usermsg", "usermsg_ignored", "usermsg_effect",
                       "thresh_widen", "inherit", "inherit_calc", "risk_level",
                       "cancel_pause", "cancel_reject", "oe_ack_not_found",
                       "oe_erase_nonexistent", "feed_latency",
                       "overnight_pos", "size_mult"]:
            if counts[etype]:
                print(f"  {etype:22s} : {counts[etype]}")

        # UM timeline
        ums = [e for e in evts if e["event_type"] == "usermsg"]
        if ums:
            print(f"\n  Usermsg timeline:")
            for e in ums:
                d = e["detail"]
                sym_str = f" sym={d['target_sym']}" if d.get("target_sym") else ""
                print(f"    {e['time'][:8]}  {d['um_name']}"
                      f"  user={d['user']}{sym_str}  {d['args']}")

        # Inherit events
        inhs = [e for e in evts if e["event_type"] == "inherit"]
        if inhs:
            print(f"\n  Position inheritance:")
            for e in inhs:
                d = e["detail"]
                sym = e["sym"].replace("xyz:", "")
                print(f"    {e['time'][:8]}  {sym:6s}  "
                      f"inherited={d['inherited_qty']:>12s}  "
                      f"new_pos={d['new_position']:>12s}")

        # Risk level changes (skip startup TL5→TL0 and end-of-day TL→TL5)
        rls = [e for e in evts if e["event_type"] == "risk_level"
               and e["detail"]["level"] not in ("TL0",)]
        # Only show non-TL5 mid-day or TL5 mid-day (not startup/shutdown)
        mid_rls = [e for e in rls
                   if not (e["detail"]["level"] == "TL5"
                           and (e["time"] < "09:30" or e["time"] > "15:55"))]
        if mid_rls:
            print(f"\n  Risk level changes (mid-day, non-TL0):")
            for e in mid_rls[:20]:  # cap at 20
                d = e["detail"]
                sym = e["sym"].replace("xyz:", "")
                print(f"    {e['time'][:8]}  {sym:6s}  {d['level']}  "
                      f"pos={d['position']:>12s}  {d['description']}")
            if len(mid_rls) > 20:
                print(f"    ... and {len(mid_rls)-20} more")

        # Cancel pauses
        cps = [e for e in evts if e["event_type"] == "cancel_pause"]
        if cps:
            sym_counts = defaultdict(int)
            for e in cps:
                sym_counts[e["sym"].replace("xyz:", "")] += 1
            print(f"\n  Cancel reject pauses: {len(cps)} total")
            for sym, cnt in sorted(sym_counts.items(), key=lambda x: -x[1]):
                print(f"    {sym:6s}: {cnt}")

        # Feed latency percentiles
        fl_evts = [e for e in evts if e["event_type"] == "feed_latency"]
        if fl_evts:
            _print_feed_latency(fl_evts)

        # Reject / OE anomaly summary
        reject_types = ["cancel_reject", "oe_ack_not_found", "oe_erase_nonexistent"]
        reject_evts = [e for e in evts if e["event_type"] in reject_types]
        if reject_evts:
            _print_reject_summary(reject_evts)


def _print_feed_latency(fl_evts):
    """Print per-symbol feed latency percentiles from feed_latency events."""
    by_sym = defaultdict(list)
    for e in fl_evts:
        by_sym[e["sym"].replace("xyz:", "")].append(e["detail"]["ms_behind"])

    print(f"\n  Feed latency (ms_behind) — {len(fl_evts)} samples:")
    header = f"    {'sym':6s}  {'count':>6s}  {'p50':>5s}  {'p75':>5s}  {'p90':>5s}  {'p99':>5s}  {'max':>5s}"
    print(header)
    for sym in sorted(by_sym):
        vals = sorted(by_sym[sym])
        n = len(vals)
        p50 = vals[int(n * 0.50)]
        p75 = vals[int(n * 0.75)]
        p90 = vals[int(n * 0.90)]
        p99 = vals[min(int(n * 0.99), n - 1)]
        mx = vals[-1]
        print(f"    {sym:6s}  {n:6d}  {p50:5d}  {p75:5d}  {p90:5d}  {p99:5d}  {mx:5d}")


def _print_reject_summary(reject_evts):
    """Print per-symbol reject/OE anomaly counts."""
    # Group by (event_type, sym)
    by_type = defaultdict(lambda: defaultdict(int))
    for e in reject_evts:
        by_type[e["event_type"]][e["sym"].replace("xyz:", "")] += 1

    type_labels = {
        "cancel_reject": "Cancel rejects",
        "oe_ack_not_found": "OE ack not found",
        "oe_erase_nonexistent": "Erase non-existent order",
    }
    print(f"\n  Order event anomalies:")
    for etype in ["cancel_reject", "oe_ack_not_found", "oe_erase_nonexistent"]:
        if etype not in by_type:
            continue
        sym_counts = by_type[etype]
        total = sum(sym_counts.values())
        label = type_labels.get(etype, etype)
        print(f"    {label}: {total} total")
        for sym, cnt in sorted(sym_counts.items(), key=lambda x: -x[1]):
            print(f"      {sym:6s}: {cnt}")


# ── Schedule file generators ─────────────────────────────────────────────

_NY_TZ = pytz.timezone("America/New_York")


def _glog_time_to_epoch_ms(date_str, time_str):
    """Convert glog date (YYYYMMDD) + time (HH:MM:SS.uuuuuu) to epoch_ms.

    The timestamp is interpreted in America/New_York timezone.
    """
    # time_str may have variable microsecond precision; normalize to 6 digits
    parts = time_str.split(".")
    if len(parts) == 2:
        frac = parts[1][:6].ljust(6, "0")
        time_str = f"{parts[0]}.{frac}"
    dt_str = f"{date_str} {time_str}"
    dt_naive = datetime.strptime(dt_str, "%Y%m%d %H:%M:%S.%f")
    dt_local = _NY_TZ.localize(dt_naive)
    epoch_ms = int(dt_local.timestamp() * 1000)
    return epoch_ms


def generate_usermsg_schedule(events, date_str, output_path, sim_date_str=None):
    """Generate a usermsg schedule TSV from parsed log events.

    Filters for event_type == "usermsg" matching date_str.
    Writes TSV: epoch_ms\\tsym\\tum_id\\targs

    Returns count of events written.
    """
    count = 0
    offset_ms = _calc_date_offset_ms(date_str, sim_date_str)
    with open(output_path, "w") as f:
        for e in events:
            if e["event_type"] != "usermsg" or e["date"] != date_str:
                continue
            d = e["detail"]
            epoch_ms = _glog_time_to_epoch_ms(e["date"], e["time"])
            epoch_ms += offset_ms
            sym = d.get("target_sym", "")
            um_id = d["um_id"]
            args = d["args"]
            f.write(f"{epoch_ms}\t{sym}\t{um_id}\t{args}\n")
            count += 1
    return count


def generate_inherit_schedule(events, date_str, output_path, sim_date_str=None):
    """Generate an inherit schedule CSV from parsed log events.

    Filters for event_type == "inherit" matching date_str.
    Writes CSV: epoch_ms,sym,inherited_qty

    Returns count of events written.
    """
    count = 0
    offset_ms = _calc_date_offset_ms(date_str, sim_date_str)
    with open(output_path, "w") as f:
        for e in events:
            if e["event_type"] != "inherit" or e["date"] != date_str:
                continue
            d = e["detail"]
            epoch_ms = _glog_time_to_epoch_ms(e["date"], e["time"])
            epoch_ms += offset_ms
            sym = e["sym"]
            inherited_qty = d["inherited_qty"]
            f.write(f"{epoch_ms},{sym},{inherited_qty}\n")
            count += 1
    return count


def _calc_date_offset_ms(live_date_str, sim_date_str):
    """Return epoch offset (ms) between live and sim calendar days."""
    if not sim_date_str or sim_date_str == live_date_str:
        return 0
    live_dt = datetime.strptime(live_date_str, "%Y%m%d")
    sim_dt = datetime.strptime(sim_date_str, "%Y%m%d")
    delta = sim_dt - live_dt
    return int(delta.total_seconds() * 1000)
