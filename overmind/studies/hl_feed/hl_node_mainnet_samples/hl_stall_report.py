#!/usr/bin/env python3
"""
Daily summary of hl-node fast/slow pipeline stalls.
Reads ~/hl/data/node_{fast,slow}_block_times/<date> and
~/hl/data/periodic_abci_states/<date>/*.rmp for the given UTC date
(default: yesterday).

Output: plain text suitable for email body.

See overmind/studies/hl_feed/hl_node_snapshot_stalls_20260620.md for
context, and hl_node_snapshot_stalls_experiment_plan_20260620.md (E1)
for what we're tracking.
"""
import json, sys, os
from datetime import datetime, timedelta, timezone

def parse_ts(t):
    s, ns = t.split('.')
    dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S')
    return dt.timestamp() + int(ns[:9].ljust(9, '0')) / 1e9

def main():
    if len(sys.argv) > 1:
        date = sys.argv[1]
    else:
        y = datetime.now(timezone.utc) - timedelta(days=1)
        date = y.strftime('%Y%m%d')

    DATA = '/home/ubuntu/hl/data'
    fast_path = f'{DATA}/node_fast_block_times/{date}'
    slow_path = f'{DATA}/node_slow_block_times/{date}'
    rmp_dir = f'{DATA}/periodic_abci_states/{date}'

    if not os.path.exists(fast_path) or not os.path.exists(slow_path):
        print(f'hl_stall_report: missing {fast_path} or {slow_path}')
        sys.exit(1)

    # --- slow apply_duration ---
    snap = []
    nonsnap = []
    slow_begin_by_h = {}
    nonsnap_outliers = []
    with open(slow_path) as f:
        for line in f:
            try: o = json.loads(line)
            except: continue
            h = o.get('height'); d = o.get('apply_duration')
            bbt = o.get('begin_block_wall_time')
            if h is None or d is None: continue
            slow_begin_by_h[h] = bbt
            if h % 10000 == 0:
                snap.append((h, d, bbt))
            else:
                nonsnap.append(d)
                if d > 1.0:
                    nonsnap_outliers.append((h, d, bbt))

    nonsnap_sorted = sorted(nonsnap)
    n_ns = len(nonsnap_sorted)
    p50_ns = nonsnap_sorted[n_ns // 2] if n_ns else 0
    p99_ns = nonsnap_sorted[int(n_ns * 0.99)] if n_ns else 0
    max_ns = nonsnap_sorted[-1] if n_ns else 0

    snap_sorted = sorted(snap, key=lambda x: x[1])
    n_s = len(snap_sorted)
    snap_med = snap_sorted[n_s // 2][1] if n_s else 0
    snap_min = snap_sorted[0][1] if n_s else 0
    snap_max = snap_sorted[-1][1] if n_s else 0

    # --- fast pipeline gaps ---
    gaps = []
    prev = None
    with open(fast_path) as f:
        for line in f:
            try: o = json.loads(line)
            except: continue
            h = o.get('height'); bbt = o.get('begin_block_wall_time')
            if h is None or bbt is None: continue
            ts = parse_ts(bbt)
            if prev is not None and ts - prev[1] > 1.0:
                gaps.append((h, ts - prev[1], bbt))
            prev = (h, ts)

    b12 = sum(1 for _,g,_ in gaps if 1.0 <= g < 2.0)
    b23 = sum(1 for _,g,_ in gaps if 2.0 <= g < 3.0)
    b35 = sum(1 for _,g,_ in gaps if 3.0 <= g < 5.0)
    b5p = sum(1 for _,g,_ in gaps if g >= 5.0)

    # --- overlap with snapshots ---
    big = [g for g in gaps if g[1] >= 5.0]
    overlaps = 0
    outside = 0
    big_details = []
    for h, g, bbt in big:
        snap_h = (h // 10000) * 10000
        snap_bbt = slow_begin_by_h.get(snap_h)
        snap_d = next((d for sh,d,_ in snap if sh == snap_h), None)
        in_window = False
        if snap_bbt and snap_d:
            fast_gap_start = parse_ts(bbt) - g
            snap_begin = parse_ts(snap_bbt)
            snap_end = snap_begin + snap_d
            in_window = fast_gap_start < snap_end and parse_ts(bbt) > snap_begin
        if in_window: overlaps += 1
        else: outside += 1
        big_details.append((h, g, bbt, snap_h, h - snap_h, 'OVERLAPS_SNAP' if in_window else 'outside_snap_window'))

    # --- snapshot files on disk ---
    rmps = []
    if os.path.isdir(rmp_dir):
        for fn in sorted(os.listdir(rmp_dir)):
            if fn.endswith('.rmp'):
                p = os.path.join(rmp_dir, fn)
                try:
                    rmps.append((fn, os.path.getsize(p)))
                except OSError:
                    pass

    # --- output ---
    out = []
    out.append(f'HL stall report {date}')
    out.append('=' * 40)
    out.append('')
    out.append(f'Snapshots: {n_s}')
    if n_s:
        out.append(f'  slow apply_duration: median={snap_med:.2f}s min={snap_min:.2f}s max={snap_max:.2f}s')
        out.append('  longest 3:')
        for h, d, _ in sorted(snap, key=lambda x: -x[1])[:3]:
            out.append(f'    block {h}  apply={d:.2f}s')
    out.append('')
    out.append(f'Non-snapshot slow apply: count={n_ns} median={p50_ns*1000:.1f}ms p99={p99_ns*1000:.1f}ms max={max_ns:.2f}s')
    if nonsnap_outliers:
        out.append(f'  {len(nonsnap_outliers)} non-snapshot blocks took >1s slow apply (top 3):')
        for h, d, bbt in sorted(nonsnap_outliers, key=lambda x: -x[1])[:3]:
            out.append(f'    block {h}  apply={d:.2f}s  begin={bbt[11:23]}')
    out.append('')
    out.append(f'Fast pipeline gaps (>1s): {len(gaps)} total')
    out.append(f'  [1,2)s:  {b12}')
    out.append(f'  [2,3)s:  {b23}')
    out.append(f'  [3,5)s:  {b35}')
    out.append(f'  >=5s:    {b5p}   <-- consumer-impacting')
    out.append('')
    out.append(f'>=5s fast stalls: {len(big)}  ({overlaps} snapshot-correlated, {outside} unexplained outliers)')
    if big_details:
        out.append('')
        out.append('  All >=5s fast stalls:')
        for h, g, bbt, snap_h, dist, tag in sorted(big_details, key=lambda x: parse_ts(x[2])):
            out.append(f'    {bbt[11:19]}  gap={g:.2f}s  block={h} ({"+" if dist >= 0 else ""}{dist} past snap {snap_h})  {tag}')
    out.append('')
    if rmps:
        out.append(f'Snapshot files currently on disk ({len(rmps)} retained):')
        for fn, sz in rmps[-6:]:
            out.append(f'  {fn}  {sz/1e9:.2f} GB')
    out.append('')
    out.append('Reference: overmind/studies/hl_feed/hl_node_snapshot_stalls_20260620.md')
    print('\n'.join(out))

if __name__ == '__main__':
    main()
