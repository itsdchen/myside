#!/usr/bin/env python3
"""Summarize delay incidents in feed-process logs.

Handles three log formats:
  - pymultifeed.py          (Python `logging` format)
  - pkmultifeed             (C++ glog format)
  - pkrelay                 (C++ glog format, Ohio relay)

Within each log, messages are split into per-stream timelines:
  pymultifeed : CME, DBEquity
  pkmultifeed : CME, RelayDBCME (gv0 only), EQ, HL
  pkrelay     : pkrelay (single stream)

An elevation window opens at the first sample with delay >= 1000 ms and
closes at the first subsequent sample < 100 ms. `End` and `Duration` are
measured against the LAST sample >= 1000 ms during the window (so a single
blip does not inflate the duration with the gap to the next sampled line).

A window is reported as an "incident" if EITHER
  (A) max delay >= --threshold (default 3000 ms), OR
  (B) duration >= --min-duration (default 60 s).
Windows with a restart event are also reported.

Multiple log files may be passed; each (file, stream) pair is reported as
its own section, followed by a summary table.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# email_utils lives one directory up, under util/. Resolve through symlinks
# so the import works when the script is invoked via the pybin/ symlink.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
from util import email_utils

# Sentinel for --email given without an explicit address (so we omit the
# receiver kwarg in send_mail() and let it use its own default).
_EMAIL_DEFAULT = object()


ELEVATION_THRESHOLD = 1000  # ms; fixed delay at which an elevation window opens


PY_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - ')
PY_SAMPLE_RE = re.compile(
    r'(CME|DBEquity) (\S+) msgtime \d+ tot_delay_ms (\d+) recv_delay_ms \d+ '
    r'ts_out_delay_ms (\d+) queue_len'
)
PY_RESTART_RE = re.compile(
    r'(CME|DBEquity) (\S+) delay (\d+)ms exceeds \d+ms threshold, forcing reconnect'
)

GLOG_TS_RE = re.compile(r'^[IWEF](\d{8} \d{2}:\d{2}:\d{2}\.\d{6}) ')
PK_CME_RE = re.compile(
    r'\] CME (\S+) msgtime \d+ tot_delay (\d+) \(db \d+ net \d+\) ts_out_delay (\d+)'
)
PK_RELAYDBCME_RE = re.compile(
    r'\] RelayDBCME (\S+) msgtime \d+ delay (\d+)ms relay_transit \d+ms seq \d+ drops \d+'
)
PK_EQ_RE = re.compile(
    r'\] EQ (\S+) msgtime \d+ delay_ms (\d+) ts_out_delay (\d+)'
)
# Legacy single HL stream (pre-node-rollout logs: "HL BTC trade ... delay_ms N").
# The lookahead keeps it from swallowing the post-rollout "HL node"/"HL ws" lines.
PK_HL_RE = re.compile(
    r'\] HL (?!(?:node|ws) )(\S+) \S+ \S+ \d+ delay_ms (\d+)'
)
# Post-rollout HL split into distinct streams:
#   HL_node        - self-hosted non-validating node feed (trade delays)
#   HL_ws_fast     - backup WS book feed, fast top-5 snapshot (fast=true)
#   HL_ws_slow     - backup WS book feed, slow top-20 snapshot (fast=false)
#   HL_ws_trade    - backup WS trade feed (no fast/slow marker on the line)
PK_HL_NODE_RE = re.compile(
    r'\] HL node (\S+) \S+ \S+ \d+ delay_ms (\d+)'
)
PK_HL_WS_BOOK_RE = re.compile(
    r'\] HL ws (\S+) l2Book event_time \d+ delay_ms (\d+) \(fast=(true|false)'
)
PK_HL_WS_TRADE_RE = re.compile(
    r'\] HL ws (\S+) trade trade_time \d+ delay_ms (\d+)'
)
PK_RESTART_RE = re.compile(
    r'\] (CME|RelayDBCME|EQ|HL) (\S+) delay (\d+)ms exceeds \d+ms threshold'
)

PKRELAY_SAMPLE_RE = re.compile(
    r'\] pkrelay (\S+) msgtime \d+ delay (\d+)ms ts_out_delay (\S+) seq \d+'
)
# Since pkrelay.cc a51102fd9, a stale feed only restarts after 3 consecutive
# strikes. Strikes 1..N-1 are logged at WARNING as "... strike K"; only the
# final strike that triggers the restart adds ", restarting". Anchor on that
# suffix so a single 3-strike restart counts as one event, not three.
PKRELAY_RESTART_RE = re.compile(
    r'\] pkrelay (\S+) delay (\d+)ms exceeds \d+ms threshold, strike \d+, restarting'
)


# 'HL' is the legacy single stream (old logs); HL_node/HL_ws_* are the
# post-rollout split (new logs). Both are listed so either log format works;
# whichever produced no samples is dropped from the report automatically.
HL_STREAMS = ['HL', 'HL_node', 'HL_ws_fast', 'HL_ws_slow', 'HL_ws_trade']
STREAMS_BY_FORMAT = {
    'pymultifeed': ['CME', 'DBEquity'],
    'pkmultifeed': ['CME', 'RelayDBCME', 'EQ'] + HL_STREAMS,
    'pkrelay': ['pkrelay'],
}
DEFAULT_STREAMS_BY_FORMAT = {
    'pymultifeed': ['CME'],
    'pkmultifeed': ['CME', 'RelayDBCME'] + HL_STREAMS,
    'pkrelay': ['pkrelay'],
}

# HL feeds carry a few-hundred-ms baseline delay, well above the default
# resume floor, so a window opened by a >=1000 ms spike would never close.
# They still open at the usual ELEVATION_THRESHOLD but only resume once delay
# falls back below this (their normal range). Other streams use --resume-below.
HL_RESUME_BELOW_MS = 600
RESUME_BELOW_BY_STREAM = {s: HL_RESUME_BELOW_MS for s in HL_STREAMS}


@dataclass
class RestartEvent:
    ts: datetime
    sym: str
    delay_ms: int


@dataclass
class Incident:
    stream: str
    start: datetime
    start_sym: str
    start_delay: int
    # End/duration are measured to the last sample >= ELEVATION_THRESHOLD,
    # not to the sub-100 ms sample that closes the window.
    end: datetime | None = None
    end_delay: int | None = None
    # Sample that actually closed the window (first delay < resume-below).
    resume_ts: datetime | None = None
    resume_delay: int | None = None
    # True when the window was closed by the max-gap rule (feed went quiet)
    # rather than by a recovery sample; resume_ts stays None in that case.
    auto_resumed: bool = False
    max_delay: int = 0
    max_delay_sym: str = ''
    max_delay_ts: datetime | None = None
    max_ts_out_delay: int | None = None
    max_ts_out_delay_sym: str = ''
    max_ts_out_delay_ts: datetime | None = None
    restart_events: list = field(default_factory=list)
    still_elevated_after_restart: bool = False
    # Classification ("A" = high delay, "B" = long duration). Filled in
    # after analyze() finishes, from args.threshold and args.min_duration.
    types: set = field(default_factory=set)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def detect_format(path: str) -> str:
    with open(path) as f:
        head = [f.readline() for _ in range(30)]
    joined = ''.join(head)
    if 'pkrelay.cc' in joined:
        return 'pkrelay'
    if 'pkmultifeed.cc' in joined:
        return 'pkmultifeed'
    return 'pymultifeed'


def parse_py_ts(s: str) -> datetime:
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S,%f')


def parse_glog_ts(s: str) -> datetime:
    return datetime.strptime(s, '%Y%m%d %H:%M:%S.%f')


def iter_events(path: str, fmt: str):
    """Yield (ts, kind, payload) where kind in {'sample', 'restart'}.

    sample payload:  (stream, sym, delay_ms, ts_out_delay_ms_or_None)
    restart payload: (stream, sym, delay_ms)
    """
    if fmt == 'pymultifeed':
        ts_re, ts_parse = PY_TS_RE, parse_py_ts
    else:
        ts_re, ts_parse = GLOG_TS_RE, parse_glog_ts

    with open(path) as f:
        for line in f:
            m = ts_re.match(line)
            if not m:
                continue
            ts = ts_parse(m.group(1))

            if fmt == 'pymultifeed':
                s = PY_SAMPLE_RE.search(line)
                if s:
                    yield ts, 'sample', (s.group(1), s.group(2), int(s.group(3)), int(s.group(4)))
                    continue
                r = PY_RESTART_RE.search(line)
                if r:
                    yield ts, 'restart', (r.group(1), r.group(2), int(r.group(3)))
                continue

            if fmt == 'pkmultifeed':
                s = PK_CME_RE.search(line)
                if s:
                    yield ts, 'sample', ('CME', s.group(1), int(s.group(2)), int(s.group(3)))
                    continue
                s = PK_RELAYDBCME_RE.search(line)
                if s:
                    yield ts, 'sample', ('RelayDBCME', s.group(1), int(s.group(2)), None)
                    continue
                s = PK_EQ_RE.search(line)
                if s:
                    yield ts, 'sample', ('EQ', s.group(1), int(s.group(2)), int(s.group(3)))
                    continue
                s = PK_HL_NODE_RE.search(line)
                if s:
                    yield ts, 'sample', ('HL_node', s.group(1), int(s.group(2)), None)
                    continue
                s = PK_HL_WS_BOOK_RE.search(line)
                if s:
                    stream = 'HL_ws_fast' if s.group(3) == 'true' else 'HL_ws_slow'
                    yield ts, 'sample', (stream, s.group(1), int(s.group(2)), None)
                    continue
                s = PK_HL_WS_TRADE_RE.search(line)
                if s:
                    yield ts, 'sample', ('HL_ws_trade', s.group(1), int(s.group(2)), None)
                    continue
                s = PK_HL_RE.search(line)
                if s:
                    yield ts, 'sample', ('HL', s.group(1), int(s.group(2)), None)
                    continue
                r = PK_RESTART_RE.search(line)
                if r:
                    yield ts, 'restart', (r.group(1), r.group(2), int(r.group(3)))
                continue

            # pkrelay
            s = PKRELAY_SAMPLE_RE.search(line)
            if s:
                # ts_out_delay is "NA" when the source carried no ts_out.
                ts_out = int(s.group(3)) if s.group(3).isdigit() else None
                yield ts, 'sample', ('pkrelay', s.group(1), int(s.group(2)), ts_out)
                continue
            r = PKRELAY_RESTART_RE.search(line)
            if r:
                yield ts, 'restart', ('pkrelay', r.group(1), int(r.group(2)))


def find_elevation_windows(path, fmt, resume_below, restart_window_s,
                           streams_filter, max_gap_s):
    """Walk the log and return (elevation windows, set of streams seen).

    `streams seen` is the subset of `streams_filter` that had at least one
    sample or restart event — useful for suppressing streams that a given
    machine simply does not receive.

    An extended (Type-B) incident is meant to capture *dense* delayed traffic
    over a sustained period, not a lone spike followed by quiet. `max_gap_s`
    enforces that: if the stream produces NO sample at all (delayed or not) for
    `max_gap_s`, the feed has gone quiet, so any open window is closed at its
    last elevated sample rather than bridging the gap to a later, unrelated
    spike (which would inflate duration / Type-B counts). A window therefore
    spans only a continuous run of live, still-delayed traffic.
    """
    completed: list[Incident] = []
    open_incident: dict[str, Incident] = {}
    last_sample_ts: dict[str, datetime] = {}
    pending: list[dict] = []  # {'stream', 'window_end', 'incident'}
    restart_window = timedelta(seconds=restart_window_s)
    max_gap = timedelta(seconds=max_gap_s)
    streams_seen: set = set()

    def expire_pending(now):
        nonlocal pending
        pending = [p for p in pending if p['window_end'] >= now]

    for ts, kind, payload in iter_events(path, fmt):
        stream = payload[0]
        if stream not in streams_filter:
            continue
        streams_seen.add(stream)

        expire_pending(ts)

        if kind == 'sample':
            _, sym, delay, ts_out = payload
            stream_resume = RESUME_BELOW_BY_STREAM.get(stream, resume_below)

            # Max-gap auto-resume: if no sample (delayed or not) has arrived for
            # this stream in max_gap, the feed went quiet. Close any open window
            # at its last elevated sample (inc.end) rather than letting this
            # sample extend or merge into it. resume_ts stays unset since we
            # never observed the recovery.
            prev_ts = last_sample_ts.get(stream)
            last_sample_ts[stream] = ts
            inc = open_incident.get(stream)
            if inc is not None and prev_ts is not None and ts - prev_ts > max_gap:
                inc.auto_resumed = True
                completed.append(inc)
                open_incident.pop(stream)
                inc = None

            # Pending-restart check: any delay >= elevation threshold within
            # 30 s of a restart means the restart did not fix things.
            for p in pending:
                if p['stream'] == stream and delay >= ELEVATION_THRESHOLD:
                    p['incident'].still_elevated_after_restart = True

            if inc is None:
                if delay >= ELEVATION_THRESHOLD:
                    inc = Incident(
                        stream=stream,
                        start=ts,
                        start_sym=sym,
                        start_delay=delay,
                        end=ts,
                        end_delay=delay,
                        max_delay=delay,
                        max_delay_sym=sym,
                        max_delay_ts=ts,
                    )
                    if ts_out is not None:
                        inc.max_ts_out_delay = ts_out
                        inc.max_ts_out_delay_sym = sym
                        inc.max_ts_out_delay_ts = ts
                    open_incident[stream] = inc
            else:
                if delay < stream_resume:
                    # Closing sample sits at normal levels; don't let it
                    # affect any max/end stats for the window.
                    inc.resume_ts = ts
                    inc.resume_delay = delay
                    completed.append(inc)
                    open_incident.pop(stream)
                else:
                    if delay >= ELEVATION_THRESHOLD:
                        inc.end = ts
                        inc.end_delay = delay
                    if delay > inc.max_delay:
                        inc.max_delay = delay
                        inc.max_delay_sym = sym
                        inc.max_delay_ts = ts
                    if ts_out is not None and (inc.max_ts_out_delay is None
                                               or ts_out > inc.max_ts_out_delay):
                        inc.max_ts_out_delay = ts_out
                        inc.max_ts_out_delay_sym = sym
                        inc.max_ts_out_delay_ts = ts

        else:  # restart
            _, sym, delay = payload
            inc = open_incident.get(stream)
            if inc is None:
                # Restart without an active window: unusual but we still
                # surface it as a synthetic incident.
                inc = Incident(
                    stream=stream,
                    start=ts,
                    start_sym=sym,
                    start_delay=delay,
                    end=ts,
                    end_delay=delay,
                    max_delay=delay,
                    max_delay_sym=sym,
                    max_delay_ts=ts,
                )
                open_incident[stream] = inc
            inc.restart_events.append(RestartEvent(ts=ts, sym=sym, delay_ms=delay))
            pending.append({
                'stream': stream,
                'window_end': ts + restart_window,
                'incident': inc,
            })

    # Flush any windows still open at EOF.
    for inc in open_incident.values():
        completed.append(inc)

    completed.sort(key=lambda i: i.start)
    return completed, streams_seen


def classify(inc: Incident, threshold_ms: int, min_duration: timedelta) -> set:
    """Return the set of incident types this window qualifies for."""
    types = set()
    if inc.max_delay >= threshold_ms:
        types.add('A')
    if inc.duration >= min_duration:
        types.add('B')
    return types


def fmt_ts(ts: datetime) -> str:
    return ts.strftime('%Y-%m-%d %H:%M:%S.') + f'{ts.microsecond // 1000:03d}'


def fmt_duration(delta: timedelta) -> str:
    total = delta.total_seconds()
    if total < 60:
        return f'{total:.3f}s'
    mins, secs = divmod(total, 60)
    if mins < 60:
        return f'{int(mins)}m{secs:.3f}s'
    hours, mins = divmod(mins, 60)
    return f'{int(hours)}h{int(mins)}m{secs:.3f}s'


def type_label(types: set) -> str:
    if not types:
        return '(restart only)'
    parts = []
    if 'A' in types:
        parts.append('A: high delay')
    if 'B' in types:
        parts.append('B: long duration')
    return ' + '.join(parts)


def render_section(path, fmt, stream, incidents, threshold, min_duration_s,
                   delayed_time):
    out = []
    out.append(f'=== {path}  [{stream}] ===')
    out.append(f'Format:   {fmt}')
    out.append(f'Criteria: A = max delay >= {threshold} ms, '
               f'B = duration >= {min_duration_s} s')
    out.append(f'Incidents: {len(incidents)}')
    out.append(f'Delayed time: {fmt_duration(delayed_time)}  '
               f'(total time delay >= {ELEVATION_THRESHOLD} ms, all elevation windows)')
    out.append('')
    for i, inc in enumerate(incidents, 1):
        out.append(f'Incident {i}  [{inc.stream}]  type {type_label(inc.types)}')
        out.append(f'  Start:    {fmt_ts(inc.start)}  '
                   f'({inc.start_sym} delay {inc.start_delay} ms)')
        out.append(f'  End:      {fmt_ts(inc.end)}  '
                   f'(last sample >= {ELEVATION_THRESHOLD} ms, delay {inc.end_delay} ms)')
        out.append(f'  Duration: {fmt_duration(inc.duration)}')
        if inc.resume_ts is not None:
            out.append(f'  Resume:   {fmt_ts(inc.resume_ts)}  '
                       f'(delay {inc.resume_delay} ms)')
        elif inc.auto_resumed:
            out.append(f'  Resume:   <feed went quiet; auto-resumed after gap>')
        else:
            out.append(f'  Resume:   <still elevated at end of log>')
        out.append(f'  Max delay:        {inc.max_delay} ms  '
                   f'({inc.max_delay_sym} at {fmt_ts(inc.max_delay_ts)})')
        if inc.max_ts_out_delay is not None:
            out.append(f'  Max ts_out_delay: {inc.max_ts_out_delay} ms  '
                       f'({inc.max_ts_out_delay_sym} at '
                       f'{fmt_ts(inc.max_ts_out_delay_ts)})')
        else:
            out.append(f'  Max ts_out_delay: n/a')
        if inc.restart_events:
            first = inc.restart_events[0]
            extra = (f' (+{len(inc.restart_events) - 1} more)'
                     if len(inc.restart_events) > 1 else '')
            out.append(f'  Restart:          yes  '
                       f'({first.sym} delay {first.delay_ms} ms at '
                       f'{fmt_ts(first.ts)}){extra}')
            out.append(f'  Still elevated:   '
                       f'{"yes" if inc.still_elevated_after_restart else "no"}  '
                       f'(any sample >= {ELEVATION_THRESHOLD} ms in '
                       f'30 s after a restart)')
        else:
            out.append(f'  Restart:          no')
        out.append('')
    return '\n'.join(out)


def render_summary_table(rows):
    if not rows:
        return ''
    headers = ('File', 'Stream', 'Incidents', 'TypeA', 'TypeB', 'DelayedTime', 'Restarts')
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
    return '\n'.join(out)


def resolve_streams(fmt, streams_arg):
    all_streams = STREAMS_BY_FORMAT[fmt]
    if streams_arg is None:
        return DEFAULT_STREAMS_BY_FORMAT[fmt]
    if streams_arg.lower() == 'all':
        return all_streams
    requested = [s.strip() for s in streams_arg.split(',') if s.strip()]
    unknown = [s for s in requested if s not in all_streams]
    if unknown:
        raise ValueError(f'unknown streams for {fmt}: {unknown}. '
                         f'Valid: {all_streams}')
    return requested


def main():
    p = argparse.ArgumentParser(
        description='Summarize delay incidents in feed-process logs.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('logs', nargs='+', help='Path to one or more log files.')
    p.add_argument('--mode', choices=['pymultifeed', 'pkmultifeed', 'pkrelay', 'auto'],
                   default='auto', help='Log format (auto-detected by default).')
    p.add_argument('--threshold', type=int, default=3000,
                   help='Type-A incident threshold: max delay in ms.')
    p.add_argument('--min-duration', type=int, default=60,
                   help='Type-B incident threshold: elevation duration in seconds '
                        '(elevation means delay >= 1000 ms).')
    p.add_argument('--resume-below', type=int, default=100,
                   help='Elevation window ends at first sample strictly below this (ms).')
    p.add_argument('--restart-window', type=int, default=30,
                   help='Seconds after a restart to check for further elevated samples.')
    p.add_argument('--max-gap', type=int, default=10,
                   help='Auto-resume an open window if no sample at all (delayed '
                        'or not) arrives for this many seconds. An extended '
                        'incident is dense delayed traffic; a lone spike followed '
                        'by quiet should not count, and the journal logs >1 s '
                        'delays far more densely than normal traffic, so without '
                        'this an unrelated later spike can merge in and inflate '
                        'duration / Type-B counts.')
    p.add_argument('--streams', default=None,
                   help='Comma-separated streams to analyze '
                        '(default depends on mode; "all" selects every stream).')
    p.add_argument('--email', nargs='?', const=_EMAIL_DEFAULT, default=None,
                   help='Email the report. Pass an address to override '
                        'email_utils.DEFAULT_RECEIVER; pass --email alone to use it.')
    args = p.parse_args()

    missing = [pth for pth in args.logs if not os.path.isfile(pth)]
    if missing:
        for m in missing:
            print(f'error: no such file: {m}', file=sys.stderr)
        return 2

    min_duration = timedelta(seconds=args.min_duration)
    summary_rows = []
    sections = []

    for path in args.logs:
        fmt = args.mode if args.mode != 'auto' else detect_format(path)
        try:
            streams = resolve_streams(fmt, args.streams)
        except ValueError as e:
            print(f'error ({path}): {e}', file=sys.stderr)
            return 2

        windows, streams_seen = find_elevation_windows(
            path, fmt,
            resume_below=args.resume_below,
            restart_window_s=args.restart_window,
            streams_filter=set(streams),
            max_gap_s=args.max_gap,
        )

        # Drop streams that produced no data in this log (e.g. gf0/gf1/gf2
        # currently don't receive RelayDBCME).
        streams_present = [s for s in streams if s in streams_seen]

        # Reported incidents (by_stream) are a subset of every elevation
        # window (all_by_stream). Delayed time is summed over ALL windows so
        # it reflects total time in a delayed state, not just the windows that
        # cross a Type-A/Type-B/restart reporting bar.
        by_stream = {s: [] for s in streams_present}
        all_by_stream = {s: [] for s in streams_present}
        for w in windows:
            w.types = classify(w, args.threshold, min_duration)
            all_by_stream.setdefault(w.stream, []).append(w)
            # Report if Type A, Type B, or any restart during the window.
            if w.types or w.restart_events:
                by_stream.setdefault(w.stream, []).append(w)

        for stream in streams_present:
            incidents = by_stream.get(stream, [])
            delayed_time = sum((w.duration for w in all_by_stream.get(stream, [])),
                               timedelta())
            sections.append(render_section(path, fmt, stream, incidents,
                                           args.threshold, args.min_duration,
                                           delayed_time))
            type_a = sum(1 for i in incidents if 'A' in i.types)
            type_b = sum(1 for i in incidents if 'B' in i.types)
            restarts = sum(len(i.restart_events) for i in incidents)
            summary_rows.append((
                path, stream, len(incidents),
                type_a, type_b, fmt_duration(delayed_time), restarts,
            ))

    sections_text = '\n'.join(sections)
    summary_text = render_summary_table(summary_rows)

    # Console: details first, summary at the bottom.
    print(sections_text)
    if summary_text:
        print(summary_text)

    if args.email is not None:
        # Email: summary first (so the at-a-glance view sits on top),
        # details below.
        body_parts = []
        if summary_text:
            body_parts.append(summary_text)
        body_parts.append(sections_text)
        body = '\n\n'.join(body_parts)
        subject = f'Feed delay report {datetime.now().strftime("%Y%m%d")}'
        kwargs = dict(subject=subject, body=body, monospace=True)
        if args.email is not _EMAIL_DEFAULT:
            kwargs['receiver'] = args.email
        email_utils.send_mail(**kwargs)

    return 0


if __name__ == '__main__':
    sys.exit(main())
