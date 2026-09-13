"""Analyze orders_*.csv files for RTT percentiles, reject rates, and cancel fill rates.

Orders CSV format (no header):
  col0: datetime (YYYYMMDD HH:MM:SS.nnnnnnnnn TZ)
  col1: epoch_ms
  col2: event_type (NewOrd, NewOrdAck, Cancel, CancelAck, CancelOrdReject, Exec, NewOrderReject)
  col3+: varies by event type (order_id is col3 for NewOrd/NewOrdAck/Cancel/CancelAck/Exec)

RTT = NewOrdAck.epoch_ms - NewOrd.epoch_ms (matched by order_id)
"""

import os
from pathlib import Path
from collections import defaultdict


# ── Column indices by event type ─────────────────────────────────────────────
# NewOrd:          ts, epoch_ms, NewOrd, order_id, sym, exchange, side, px, sz, ..., ADD|REM
#                  (last field ADD=passive/maker/ALO, REM=aggressive/taker/IOC)
# NewOrdAck:       ts, epoch_ms, NewOrdAck, order_id, sym, exchange, side, ...
# Cancel:          ts, epoch_ms, Cancel, order_id, sym, exchange
# CancelAck:       ts, epoch_ms, CancelAck, order_id, sym, exchange, ...
# CancelOrdReject: ts, epoch_ms, CancelOrdReject, _, order_id, sym, exchange, ...
# Exec:            ts, epoch_ms, Exec, order_id, sym, exchange, side, px, sz, ...
# Elimination:     ts, epoch_ms, Elimination, sym, exchange, order_id, _, sz
# NewOrderReject:  ts, epoch_ms, NewOrderReject, sym, exchange, order_id, side, ...
# (Exec / Elimination / NewOrderReject are the terminal events for a REM/IOC order)


def parse_orders_file(path):
    """Parse an orders CSV into structured events.

    Returns dict with:
        rtt_by_sym: {sym: [rtt_ms, ...]}            (all NewOrd→Ack, both types)
        rtt_add_by_sym: {sym: [rtt_ms, ...]}        (ADD = passive/maker/ALO only)
        rtt_rem_by_sym: {sym: [rtt_ms, ...]}        (REM = aggressive/taker/IOC only)
        cancel_rtt_by_sym: {sym: [rtt_ms, ...]}
        rejects: {reject_type: {sym: count}}
        event_counts: {event_type: count}
    """
    new_ords = {}     # order_id -> (epoch_ms, sym, flag)  flag = ADD/REM
    cancels = {}      # order_id -> (epoch_ms, sym)
    rtt_by_sym = defaultdict(list)
    # RTT split by the ADD/REM marker on each NewOrd line: ADD = passive/maker
    # (resting, ALO-style), REM = aggressive/taker (crossing, IOC-style). Lets us
    # see latency separately for the two order types even within one strat.
    rtt_add_by_sym = defaultdict(list)
    rtt_rem_by_sym = defaultdict(list)
    # REM (IOC) orders never get a NewOrdAck — they live until a terminal event
    # (Exec / Elimination / NewOrderReject). We measure their round trip as
    # NewOrd → EARLIEST terminal for that order_id (first time we hear back).
    # Earliest, not last, because a partial fill's Elimination can be logged a
    # few ms before its Exec.  oid -> earliest terminal epoch_ms.
    rem_terminal = {}
    cancel_rtt_by_sym = defaultdict(list)
    rejects = {
        "CancelOrdReject": defaultdict(int),
        "NewOrderReject": defaultdict(int),
    }
    event_counts = defaultdict(int)

    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue

                try:
                    epoch_ms = int(parts[1])
                except ValueError:
                    continue
                etype = parts[2]
                event_counts[etype] += 1

                if etype == "NewOrd" and len(parts) >= 5:
                    oid = parts[3]
                    sym = parts[4]
                    # Last field is the ADD/REM liquidity marker (see header note).
                    flag = parts[-1].strip()
                    new_ords[oid] = (epoch_ms, sym, flag)

                elif etype == "NewOrdAck" and len(parts) >= 5:
                    oid = parts[3]
                    sym = parts[4]
                    if oid in new_ords:
                        send_ms, _, flag = new_ords[oid]
                        rtt = epoch_ms - send_ms
                        if 0 <= rtt < 60000:  # sanity: under 60s
                            rtt_by_sym[sym].append(rtt)
                            # Bucket by order type; unrecognized flags stay in the
                            # total only (none observed in practice, but be safe).
                            if flag == "ADD":
                                rtt_add_by_sym[sym].append(rtt)
                            elif flag == "REM":
                                rtt_rem_by_sym[sym].append(rtt)

                elif etype == "Cancel" and len(parts) >= 5:
                    oid = parts[3]
                    sym = parts[4]
                    cancels[oid] = (epoch_ms, sym)

                elif etype == "CancelAck" and len(parts) >= 5:
                    oid = parts[3]
                    sym = parts[4]
                    if oid in cancels:
                        send_ms, _ = cancels[oid]
                        rtt = epoch_ms - send_ms
                        if 0 <= rtt < 60000:
                            cancel_rtt_by_sym[sym].append(rtt)

                elif etype == "Exec" and len(parts) >= 4:
                    # Exec: ..., Exec, order_id, sym, ...  Terminal for a REM order.
                    oid = parts[3]
                    no = new_ords.get(oid)
                    if no and no[2] == "REM":
                        prev = rem_terminal.get(oid)
                        if prev is None or epoch_ms < prev:
                            rem_terminal[oid] = epoch_ms

                elif etype == "Elimination" and len(parts) >= 6:
                    # Elimination: ..., Elimination, sym, exchange, order_id, ...
                    oid = parts[5]
                    no = new_ords.get(oid)
                    if no and no[2] == "REM":
                        prev = rem_terminal.get(oid)
                        if prev is None or epoch_ms < prev:
                            rem_terminal[oid] = epoch_ms

                elif etype == "CancelOrdReject" and len(parts) >= 6:
                    # col3 is empty, col4 is order_id, col5 is sym
                    sym = parts[5]
                    rejects["CancelOrdReject"][sym] += 1

                elif etype == "NewOrderReject" and len(parts) >= 4:
                    sym = parts[3]
                    rejects["NewOrderReject"][sym] += 1
                    # NewOrderReject: ..., NewOrderReject, sym, exchange, order_id, ...
                    # Also a terminal event for a REM order (immediate no-match).
                    if len(parts) >= 6:
                        oid = parts[5]
                        no = new_ords.get(oid)
                        if no and no[2] == "REM":
                            prev = rem_terminal.get(oid)
                            if prev is None or epoch_ms < prev:
                                rem_terminal[oid] = epoch_ms

    except OSError as e:
        print(f"  [warn] Could not read {path}: {e}")

    # Emit REM (IOC) round trips: NewOrd → earliest terminal event. Kept out of
    # rtt_by_sym (the ALO-ack total used by the existing report) on purpose, so
    # the emailed report's RTT numbers don't shift; only the trends view reads it.
    for oid, term_ms in rem_terminal.items():
        send_ms, sym, _ = new_ords[oid]
        rtt = term_ms - send_ms
        if 0 <= rtt < 60000:  # sanity: under 60s
            rtt_rem_by_sym[sym].append(rtt)

    return {
        "rtt_by_sym": dict(rtt_by_sym),
        "rtt_add_by_sym": dict(rtt_add_by_sym),
        "rtt_rem_by_sym": dict(rtt_rem_by_sym),
        "cancel_rtt_by_sym": dict(cancel_rtt_by_sym),
        "rejects": rejects,
        "event_counts": dict(event_counts),
    }


def analyze_orders(local_base, aliases, date_strs, strat_filter=None, group_filter=None):
    """Parse orders files across aliases/dates and aggregate results.

    Returns dict keyed by (alias, group, strat, date) with parse_orders_file results.
    """
    results = {}
    base = Path(local_base)
    if not base.exists():
        return results

    for alias_dir in sorted(base.iterdir()):
        if not alias_dir.is_dir() or alias_dir.name not in aliases:
            continue
        alias = alias_dir.name
        for group_dir in sorted(alias_dir.iterdir()):
            if not group_dir.is_dir():
                continue
            group = group_dir.name
            if group_filter and group_filter not in group:
                continue
            for strat_dir in sorted(group_dir.iterdir()):
                if not strat_dir.is_dir():
                    continue
                strat = strat_dir.name
                if strat_filter and strat_filter not in strat:
                    continue
                for ds in date_strs:
                    orders_file = strat_dir / f"orders_{ds}.csv"
                    if orders_file.exists():
                        results[(alias, group, strat, ds)] = parse_orders_file(orders_file)

    return results


def _percentiles(vals):
    """Compute p50, p75, p90, p99, max from sorted list. Returns tuple or None."""
    if not vals:
        return None
    sv = sorted(vals)
    n = len(sv)
    return (
        sv[int(n * 0.50)],
        sv[int(n * 0.75)],
        sv[int(n * 0.90)],
        sv[min(int(n * 0.99), n - 1)],
        sv[-1],
    )


def summarize_orders(results):
    """Print human-readable summary of order analysis results."""
    # Group by (alias, group, strat)
    by_strat = defaultdict(dict)
    for (alias, group, strat, ds), data in results.items():
        by_strat[(alias, group, strat)][ds] = data

    for key in sorted(by_strat):
        alias, group, strat = key
        dates_data = by_strat[key]

        print(f"\n{'='*80}")
        print(f"  {alias}/{group}/{strat}")
        print(f"{'='*80}")

        for ds in sorted(dates_data):
            data = dates_data[ds]
            ec = data["event_counts"]
            print(f"\n  {ds}  ({ec.get('NewOrd', 0)} orders, {ec.get('Exec', 0)} fills, "
                  f"{ec.get('Cancel', 0)} cancels)")

            # NewOrd RTT
            all_rtt = []
            for vals in data["rtt_by_sym"].values():
                all_rtt.extend(vals)
            if all_rtt:
                print(f"\n  NewOrd → NewOrdAck RTT (ms):")
                print(f"    {'sym':8s}  {'count':>6s}  {'p50':>5s}  {'p75':>5s}  {'p90':>5s}  {'p99':>5s}  {'max':>5s}")
                for sym in sorted(data["rtt_by_sym"]):
                    p = _percentiles(data["rtt_by_sym"][sym])
                    s = sym.replace("xyz:", "")
                    n = len(data["rtt_by_sym"][sym])
                    print(f"    {s:8s}  {n:6d}  {p[0]:5d}  {p[1]:5d}  {p[2]:5d}  {p[3]:5d}  {p[4]:5d}")
                # All syms combined
                p = _percentiles(all_rtt)
                print(f"    {'ALL':8s}  {len(all_rtt):6d}  {p[0]:5d}  {p[1]:5d}  {p[2]:5d}  {p[3]:5d}  {p[4]:5d}")

            # Cancel RTT
            all_cancel_rtt = []
            for vals in data["cancel_rtt_by_sym"].values():
                all_cancel_rtt.extend(vals)
            if all_cancel_rtt:
                print(f"\n  Cancel → CancelAck RTT (ms):")
                print(f"    {'sym':8s}  {'count':>6s}  {'p50':>5s}  {'p75':>5s}  {'p90':>5s}  {'p99':>5s}  {'max':>5s}")
                for sym in sorted(data["cancel_rtt_by_sym"]):
                    p = _percentiles(data["cancel_rtt_by_sym"][sym])
                    s = sym.replace("xyz:", "")
                    n = len(data["cancel_rtt_by_sym"][sym])
                    print(f"    {s:8s}  {n:6d}  {p[0]:5d}  {p[1]:5d}  {p[2]:5d}  {p[3]:5d}  {p[4]:5d}")
                p = _percentiles(all_cancel_rtt)
                print(f"    {'ALL':8s}  {len(all_cancel_rtt):6d}  {p[0]:5d}  {p[1]:5d}  {p[2]:5d}  {p[3]:5d}  {p[4]:5d}")

            # Rejects
            has_rejects = False
            for rtype in ["CancelOrdReject", "NewOrderReject"]:
                sym_counts = data["rejects"][rtype]
                if sym_counts:
                    if not has_rejects:
                        print(f"\n  Rejects:")
                        has_rejects = True
                    total = sum(sym_counts.values())
                    label = "Cancel rejects" if rtype == "CancelOrdReject" else "NewOrder rejects"
                    print(f"    {label}: {total}")
                    for sym, cnt in sorted(sym_counts.items(), key=lambda x: -x[1]):
                        s = sym.replace("xyz:", "")
                        print(f"      {s:8s}: {cnt}")


def build_report_section(out, results, dates_present, strats_in_data):
    """Write order analysis section for the report command, aggregated per
    (alias, date). All strats on a machine are merged into one row per day —
    order/fill counts sum, RTT percentiles recompute over the combined samples,
    rejects sum. Per-strat detail still available via the verbose
    `order-analysis` CLI command.
    """
    # Filter to strats in the report
    filtered = {}
    for (alias, group, strat, ds), data in results.items():
        if (alias, group, strat) in strats_in_data:
            filtered[(alias, group, strat, ds)] = data

    if not filtered:
        return

    def _is_ioc(strat):
        return "cx" in strat or "cross" in strat

    # Two parallel aggregations — ALO (resting) and IOC (cross) — since their
    # latency / fill / reject profiles look very different and mixing them in
    # one row hides the signal.
    buckets = {
        "alo": defaultdict(lambda: defaultdict(lambda: {
            "n_ords": 0, "n_fills": 0,
            "rtts": [], "cxl_rtts": [],
            "cxl_rej": 0, "ord_rej": 0,
        })),
        "ioc": defaultdict(lambda: defaultdict(lambda: {
            "n_ords": 0, "n_fills": 0,
            "rtts": [], "cxl_rtts": [],
            "cxl_rej": 0, "ord_rej": 0,
        })),
    }
    for (alias, group, strat, ds), data in filtered.items():
        bucket = "ioc" if _is_ioc(strat) else "alo"
        agg = buckets[bucket][alias][ds]
        ec = data["event_counts"]
        agg["n_ords"] += ec.get("NewOrd", 0)
        agg["n_fills"] += ec.get("Exec", 0)
        for vals in data["rtt_by_sym"].values():
            agg["rtts"].extend(vals)
        for vals in data["cancel_rtt_by_sym"].values():
            agg["cxl_rtts"].extend(vals)
        agg["cxl_rej"] += sum(data["rejects"]["CancelOrdReject"].values())
        agg["ord_rej"] += sum(data["rejects"]["NewOrderReject"].values())

    out.write("=" * 90 + "\n")
    out.write("ORDER ANALYSIS\n")
    out.write("=" * 90 + "\n")
    out.write("Per-machine totals; all strats on each alias merged. ALO (resting) "
              "and IOC (cross) split since they have different latency profiles.\n")

    def _render_bucket(label, by_alias):
        if not by_alias:
            return
        out.write(f"\n-- {label} --\n")
        for alias in sorted(by_alias):
            out.write(f"\n  {alias}\n")
            header = (f"    {'date':<10}  {'orders':>9}  {'fills':>7}  "
                      f"{'ord_p50':>7}  {'ord_p99':>7}  "
                      f"{'cxl_p50':>7}  {'cxl_p99':>7}  "
                      f"{'cxl_rej':>7}  {'ord_rej':>7}")
            out.write(header + "\n")
            out.write("    " + "-" * (len(header) - 4) + "\n")
            dates_data = by_alias[alias]
            for ds in sorted(dates_present):
                if ds not in dates_data:
                    continue
                agg = dates_data[ds]
                rtt_p = _percentiles(agg["rtts"])
                cxl_p = _percentiles(agg["cxl_rtts"])
                rtt_50 = f"{rtt_p[0]:5d}ms" if rtt_p else "     --"
                rtt_99 = f"{rtt_p[3]:5d}ms" if rtt_p else "     --"
                cxl_50 = f"{cxl_p[0]:5d}ms" if cxl_p else "     --"
                cxl_99 = f"{cxl_p[3]:5d}ms" if cxl_p else "     --"
                row = (f"    {ds:<10}  {agg['n_ords']:>9,d}  {agg['n_fills']:>7,d}  "
                       f"{rtt_50:>7}  {rtt_99:>7}  "
                       f"{cxl_50:>7}  {cxl_99:>7}  "
                       f"{agg['cxl_rej']:>7,d}  {agg['ord_rej']:>7,d}")
                out.write(row + "\n")

    _render_bucket("ALO (resting)", buckets["alo"])
    _render_bucket("IOC (cross)", buckets["ioc"])

    out.write("\n")
