#!/usr/bin/env python3
"""Summarize issues in pygateway (wsgateway.py) logs.

Counts incidents of three signals:

  1. "All WS connections exhausted" — every minute while all WS connections
     are 429-paused, the gateway emits a hold-cancels line. The HL rate limit
     resets at the round minute boundary; a single incident covers one
     round-minute bucket, with a 5 s slop into the next minute to absorb
     clock skew between us and HL.

  2. "L1 is congested" — HL declined an action because of L1 congestion. The
     gateway backs off for 60 s from the first occurrence, but the message
     can still repeat from in-flight actions. One incident per
     [first_ts, first_ts + 60 s] window.

  3. "Error from cancel for <id>" — HL rejected a cancel because the order
     was already canceled, executed, or never received. Each unique order id
     is one incident, classified by which marker accompanies it:
       (a) "Ignoring cancel error for <id>", OR "Sending cxlack early for <id>
           (terminal state on HL: got resting ack)" — order executed before the
           cancel landed; benign missed cancel. The early/resting variant is the
           same case where HL's cancel-reject merely lapped the fill back to us.
       (b) none of (a), (c), (d)              — likely raced with placement
       (c) "Sending cxlack anyway for <id>"   — gateway gave up retrying after
                                                >5 attempts; HL state diverged
       (d) "Sending cxlack early for <id> (terminal state on HL: expired)" — we
           sent the order >3s ago, never got a resting ack, and HL now says it
           was never placed. We acked the cancel without ever confirming the
           order rested, so its true state is genuinely uncertain — the one to
           watch. Classified before (a) because the single insurance cancel it
           fires later trips the (a) "Ignoring cancel error" marker for the same
           id.
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# email_utils lives one directory up, under util/. Resolve through symlinks
# so the import works when the script is invoked via a pybin/ symlink.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
from util import email_utils

_EMAIL_DEFAULT = object()


TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - ')
EXHAUSTED_RE = re.compile(r'All WS connections exhausted')
L1_RE = re.compile(r'L1 is congested')
ERR_CANCEL_RE = re.compile(r'Error from cancel for (\S+):')
IGNORE_CANCEL_RE = re.compile(r'Ignoring cancel error for (\S+) because')
CXLACK_RE = re.compile(r'Sending cxlack anyway for (\S+)')
# The trailing terminal-type distinguishes the benign case ("got resting ack":
# order filled, cancel-reject just lapped the fill -> type a) from the uncertain
# one ("expired": order sent >3s ago, never acked, HL says gone -> type d).
CXLACK_EARLY_RE = re.compile(r'Sending cxlack early for (\S+) \(terminal state on HL: ([^)]*)\)')

# End-of-day RTT histogram dump emitted by wsgateway._log_rtt_stats().
RTT_STATS_RE = re.compile(
    r'RTT_STATS cat=(\w+) count=(\d+) min_ms=([\d.]+) max_ms=([\d.]+) '
    r'bin_ms=(\d+) overflow=(\d+) hist=([\d,]*)')
# Ordered categories and their display labels for the RTT tables.
RTT_CATS = ('Alo', 'Ioc', 'Cxl')
RTT_CAT_LABEL = {'Alo': 'ALO orders', 'Ioc': 'IOC orders', 'Cxl': 'Cancels'}

EXHAUSTED_BUFFER_S = 5  # slop into the minute after the round boundary
L1_WINDOW_S = 60        # back-off duration


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S,%f')


def iter_log(path):
    """Yield (ts, line) for parseable lines."""
    with open(path) as f:
        for line in f:
            m = TS_RE.match(line)
            if not m:
                continue
            yield parse_ts(m.group(1)), line


def machine_label(path):
    """Machine name for a log path: its parent directory (e.g. .../gf2/pygateway
    .log -> 'gf2'), falling back to the filename when there's no parent dir."""
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return parent or os.path.basename(path)


def parse_rtt_stats(line):
    """Parse one RTT_STATS line into (cat, stats-dict), or None if no match."""
    m = RTT_STATS_RE.search(line)
    if not m:
        return None
    cat, count, min_ms, max_ms, bin_ms, overflow, hist_str = m.groups()
    hist = [int(x) for x in hist_str.split(',')] if hist_str else []
    return cat, {
        'count': int(count),
        'min_ms': float(min_ms),
        'max_ms': float(max_ms),
        'bin_ms': int(bin_ms),
        'overflow': int(overflow),
        'hist': hist,
    }


def bucket_exhausted(events):
    """One incident per round-minute bucket, with a 5 s slop into the next.

    The first event opens an incident whose window ends at
    `next_round_minute(first_ts) + EXHAUSTED_BUFFER_S`. Subsequent events
    inside that window collapse into the same incident; the first event past
    it opens a new one.
    """
    incidents = []
    window_end = None
    for ts in events:
        if window_end is None or ts >= window_end:
            next_min = ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
            window_end = next_min + timedelta(seconds=EXHAUSTED_BUFFER_S)
            incidents.append(ts)
    return incidents


def bucket_l1(events):
    """One incident per 60 s window anchored at first occurrence."""
    incidents = []
    cutoff = None
    for ts in events:
        if cutoff is None or ts >= cutoff:
            incidents.append(ts)
            cutoff = ts + timedelta(seconds=L1_WINDOW_S)
    return incidents


def classify_cancel_errors(err_counts, ignored_ids, cxlack_ids,
                           early_resting_ids, early_expired_ids):
    """Return (type_a, type_b, type_c, type_d) lists of order ids.

    The early_* sets are checked first: those ids also land in ignored_ids (the
    gateway's one insurance cancel trips the "Ignoring cancel error" marker for
    the same id), so testing ignored_ids first would mask them. "got resting ack"
    early acks are a benign missed cancel (type a); only "expired" early acks —
    where the order was never confirmed to rest — are the uncertain type d.
    """
    a, b, c, d = [], [], [], []
    for oid in err_counts:
        if oid in early_expired_ids:
            d.append(oid)
        elif oid in early_resting_ids or oid in ignored_ids:
            a.append(oid)
        elif oid in cxlack_ids:
            c.append(oid)
        else:
            b.append(oid)
    return a, b, c, d


def analyze(paths):
    """Analyze one machine's whole day. paths is every log file for that machine
    (the current pygateway.<date>.log plus any rotated .log.1/.log.2/... backups);
    they're accumulated together so signals that span a rotation boundary — a
    cancel's error and its later ignore/cxlack line, ordered exhausted/L1 events,
    summed RTT deltas — are still counted correctly."""
    exhausted_events = []
    l1_events = []
    err_counts = defaultdict(int)
    ignored_ids = set()
    cxlack_ids = set()
    early_resting_ids = set()
    early_expired_ids = set()
    # The gateway clears its counters after each dump, so RTT_STATS blocks are
    # non-overlapping deltas (across reconnects, rotated files, and any mid-day
    # process restarts). Collect them all and merge per category below.
    rtt_blocks = defaultdict(list)

    for path in paths:
        for ts, line in iter_log(path):
            parsed = parse_rtt_stats(line)
            if parsed:
                cat, stats = parsed
                rtt_blocks[cat].append(stats)
                continue
            if EXHAUSTED_RE.search(line):
                exhausted_events.append(ts)
                continue
            if L1_RE.search(line):
                l1_events.append(ts)
                continue
            m = ERR_CANCEL_RE.search(line)
            if m:
                err_counts[m.group(1)] += 1
                continue
            m = IGNORE_CANCEL_RE.search(line)
            if m:
                ignored_ids.add(m.group(1))
                continue
            m = CXLACK_RE.search(line)
            if m:
                cxlack_ids.add(m.group(1))
                continue
            m = CXLACK_EARLY_RE.search(line)
            if m:
                if m.group(2) == 'expired':
                    early_expired_ids.add(m.group(1))
                else:
                    early_resting_ids.add(m.group(1))

    # Sort so the time-window bucketing is chronological regardless of the order
    # files were passed in (e.g. rotated backups interleaved with the main file).
    exhausted_events.sort()
    l1_events.sort()

    a, b, c, d = classify_cancel_errors(err_counts, ignored_ids, cxlack_ids,
                                        early_resting_ids, early_expired_ids)
    rtt = {cat: merge_rtt(blocks) for cat, blocks in rtt_blocks.items()}
    return {
        'exhausted': len(bucket_exhausted(exhausted_events)),
        'l1': len(bucket_l1(l1_events)),
        'cancel_a': len(a),
        'cancel_b': len(b),
        'cancel_c': len(c),
        'cancel_d': len(d),
        'rtt': rtt,
    }


KEY_TEXT = (
    '=== Key ===\n'
    'Exhausted  All WS connections 429-paused. One incident per round-minute\n'
    '           bucket (5 s slop into the next minute for clock skew).\n'
    'L1Cong     HL declined an action because L1 was congested. One incident\n'
    "           per 60 s window from the first occurrence (gateway's back-off).\n"
    'CxlA       Cancel rejected but order had executed first; benign missed\n'
    '           cancel. Either "Ignoring cancel error" (exec ack already seen),\n'
    "           or an early cxlack tagged \"got resting ack\" (we had a resting\n"
    '           ack and HL\'s cancel-reject merely lapped the fill back to us).\n'
    'CxlB       Cancel rejected; no ignore, no cxlack-anyway. Either executed\n'
    "           but ack not yet seen, or (more likely) we cancelled before HL\n"
    '           had processed the placement.\n'
    'CxlC       Cancel rejected repeatedly (>5 retries); gateway gave up and\n'
    "           synthesized a cxlack. HL no longer thinks the order exists but\n"
    '           we still do — the most important type to investigate.\n'
    'CxlD       Early cxlack tagged "expired": order sent >3s ago, never acked\n'
    '           as resting, and HL says it was never placed. We acked the cancel\n'
    "           without ever confirming the order rested, so its true state is\n"
    '           genuinely uncertain — watch alongside CxlC.'
)


def render_summary_table(rows):
    headers = ('Machine', 'Exhausted', 'L1Cong', 'CxlA', 'CxlB', 'CxlC', 'CxlD')
    widths = [len(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(str(v)))
    fmt = '  '.join('{:<' + str(w) + '}' for w in widths)
    out = ['=== Summary ===']
    out.append(fmt.format(*headers))
    out.append(fmt.format(*['-' * w for w in widths]))
    for r in rows:
        out.append(fmt.format(*[str(v) for v in r]))
    if len(rows) > 1:
        totals = ['TOTAL'] + [sum(r[i] for r in rows) for i in range(1, len(headers))]
        out.append(fmt.format(*[str(v) for v in totals]))
    return '\n'.join(out)


def pct_from_hist(stats, q):
    """Quantile q (0..1) in ms from a histogram stats dict (bin-resolution).

    Walks cumulative bucket counts to the target rank and returns the bucket
    midpoint, clamped to [min_ms, max_ms]. A rank that falls in the overflow
    region (RTTs beyond the histogram range) returns max_ms.
    """
    count = stats['count']
    if count <= 0:
        return 0.0
    bin_ms = stats['bin_ms']
    need = q * count
    acc = 0
    for idx, v in enumerate(stats['hist']):
        acc += v
        if acc >= need:
            mid = idx * bin_ms + bin_ms / 2.0
            return min(max(mid, stats['min_ms']), stats['max_ms'])
    # Ran past every in-range bucket -> the quantile lives in the overflow tail.
    return stats['max_ms']


def merge_rtt(stats_list):
    """Merge per-machine RTT stats for one category into an overall stats dict.

    Histograms are summed element-wise (percentiles aren't mergeable, bucket
    counts are); count/overflow sum; min/max take the extremes. Blocks with a
    differing bin_ms (version skew) are skipped so we never misalign buckets.
    """
    bin_ms = stats_list[0]['bin_ms']
    usable = [s for s in stats_list if s['bin_ms'] == bin_ms]
    width = max(len(s['hist']) for s in usable)
    hist = [0] * width
    for s in usable:
        for i, v in enumerate(s['hist']):
            hist[i] += v
    return {
        'count': sum(s['count'] for s in usable),
        'min_ms': min(s['min_ms'] for s in usable),
        'max_ms': max(s['max_ms'] for s in usable),
        'bin_ms': bin_ms,
        'overflow': sum(s['overflow'] for s in usable),
        'hist': hist,
    }


def _rtt_row(label, stats):
    return (
        label,
        str(stats['count']),
        '{:.1f}'.format(stats['min_ms']),
        '{:.1f}'.format(pct_from_hist(stats, 0.50)),
        '{:.1f}'.format(pct_from_hist(stats, 0.90)),
        '{:.1f}'.format(pct_from_hist(stats, 0.99)),
        '{:.1f}'.format(stats['max_ms']),
    )


def render_rtt_tables(rtt_rows):
    """rtt_rows: list of (machine_label, rtt-dict). One table per category with a
    row per machine that has data plus an OVERALL row. Returns '' if no data."""
    headers = ('Machine', 'N', 'Min', 'P50', 'P90', 'P99', 'Max')
    sections = []
    for cat in RTT_CATS:
        present = [(label, r[cat]) for label, r in rtt_rows if cat in r]
        if not present:
            continue
        table_rows = [_rtt_row(label, stats) for label, stats in present]
        if len(present) > 1:
            table_rows.append(_rtt_row('OVERALL', merge_rtt([s for _, s in present])))

        widths = [len(h) for h in headers]
        for r in table_rows:
            for i, v in enumerate(r):
                widths[i] = max(widths[i], len(v))
        fmt = '  '.join('{:<' + str(w) + '}' for w in widths)
        out = ['=== RTT: {} (ms) ==='.format(RTT_CAT_LABEL[cat])]
        out.append(fmt.format(*headers))
        out.append(fmt.format(*['-' * w for w in widths]))
        for r in table_rows:
            out.append(fmt.format(*r))
        sections.append('\n'.join(out))
    return '\n\n'.join(sections)


RTT_KEY_TEXT = (
    '=== RTT Key ===\n'
    'Round-trip time (ms), gateway-measured from send to first response, split\n'
    'by ALO orders / IOC orders / cancels. N is the number of samples; Min/Max\n'
    'are exact; P50/P90/P99 are computed from a fixed-width histogram so they are\n'
    'accurate to the bucket width. OVERALL is computed from the merged histogram\n'
    'across machines (not an average of per-machine percentiles).'
)


def main():
    p = argparse.ArgumentParser(
        description='Summarize issues in pygateway logs.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('logs', nargs='+',
                   help='Log files. Files in the same directory are treated as '
                        'one machine, so pass any rotated backups too (e.g. '
                        "'<dir>/pygateway.<date>.log*') to cover a full day.")
    p.add_argument('--email', nargs='?', const=_EMAIL_DEFAULT, default=None,
                   help='Email the report. Pass an address to override '
                        'email_utils.DEFAULT_RECEIVER; pass --email alone to use it.')
    args = p.parse_args()

    missing = [pth for pth in args.logs if not os.path.isfile(pth)]
    if missing:
        for m in missing:
            print(f'error: no such file: {m}', file=sys.stderr)
        return 2

    # Group files by machine (parent dir), preserving first-seen order. A machine
    # contributes several files when its log rotated (pygateway.<date>.log plus
    # .log.1, .log.2, ...); they're analyzed together as one day.
    machines = []
    files_by_machine = {}
    for path in args.logs:
        label = machine_label(path)
        if label not in files_by_machine:
            files_by_machine[label] = []
            machines.append(label)
        files_by_machine[label].append(path)

    rows = []
    rtt_rows = []
    for label in machines:
        stats = analyze(files_by_machine[label])
        rows.append((
            label,
            stats['exhausted'],
            stats['l1'],
            stats['cancel_a'],
            stats['cancel_b'],
            stats['cancel_c'],
            stats['cancel_d'],
        ))
        rtt_rows.append((label, stats['rtt']))

    parts = [render_summary_table(rows), KEY_TEXT]
    rtt_tables = render_rtt_tables(rtt_rows)
    if rtt_tables:
        parts += [rtt_tables, RTT_KEY_TEXT]
    body = '\n\n'.join(parts)
    print(body)

    if args.email is not None:
        subject = f'Gateway issue report {datetime.now().strftime("%Y%m%d")}'
        kwargs = dict(subject=subject, body=body, monospace=True)
        if args.email is not _EMAIL_DEFAULT:
            kwargs['receiver'] = args.email
        email_utils.send_mail(**kwargs)

    return 0


if __name__ == '__main__':
    sys.exit(main())
