#!/usr/bin/env python

"""
ium - Interactive UserMsg

A curses-based form for composing and sending usermsgs.

Default view: editable fields (strat, sym, um, args) with send history.
Ctrl-S opens an interactive strat/sym picker (live pairs from last 30min).
Ctrl-U opens a UM type picker.

Usage:
    ium.py [--testnet]
"""

import argparse
import curses
import getpass
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, ".."))
from util import sbcreds
from usermsg import UM_ID_MAP, sendUM, toNumberIfPossible

HISTORY_FILE = os.path.expanduser("~/.ium_history")
HISTORY_MAX = 200
PAIR_REFRESH_SECS = 30 * 60  # 30 minutes
PAIR_FAIL_RETRY_SECS = 60    # back off this long after a failed fetch
PAIR_WINDOW_SECS = 30 * 60   # a (strat,sym) is "live" if seen in this window
PAIR_ROW_LIMIT = 1000        # Supabase server-caps at 1000; newest-by-id is ~0.8s

UM_ID_REVERSE = {v: k for k, v in UM_ID_MAP.items()}


# ── History ──────────────────────────────────────────────────────────────────

def load_history():
    """Load send history from disk. Each entry: {strat, sym, um, args}."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_history(history):
    history = history[-HISTORY_MAX:]
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=1)


def add_to_history(history, entry):
    # Dedup: remove if identical entry exists, then append
    history = [h for h in history if h != entry]
    history.append(entry)
    return history[-HISTORY_MAX:]


# ── Supabase live pairs ─────────────────────────────────────────────────────

def _parse_pg_ts(ts):
    """Parse a Supabase/Postgres timestamp. Fractional seconds can have 1-6
    digits; datetime.fromisoformat only accepts 0/3/6 (prior to Py 3.11)."""
    ts = ts.replace("Z", "+00:00")
    if "." in ts:
        head, tail = ts.split(".", 1)
        tz_idx = max(tail.find("+"), tail.find("-"))
        if tz_idx == -1:
            frac, tz = tail, ""
        else:
            frac, tz = tail[:tz_idx], tail[tz_idx:]
        frac = (frac + "000000")[:6]
        ts = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(ts)


class LivePairCache:
    def __init__(self, testnet, credsfile):
        self.testnet = testnet
        self.credsfile = credsfile
        self.pairs = []
        self.last_fetch = 0       # time of last successful fetch (for UI "refreshed Xs ago")
        self._last_attempt = 0    # time of last fetch attempt (for retry throttling)

    def get_pairs(self):
        now = time.time()
        need_refresh = (now - self.last_fetch > PAIR_REFRESH_SECS) or not self.pairs
        if need_refresh:
            # On failure, don't re-hammer supabase on every keystroke.
            if now - self._last_attempt >= PAIR_FAIL_RETRY_SECS:
                self._fetch()
        return self.pairs

    def stale_secs(self):
        return int(time.time() - self.last_fetch) if self.last_fetch else -1

    def _fetch(self):
        # Filtering by created_at server-side triggers a statement timeout
        # (no usable index). Pull the newest PAIR_ROW_LIMIT rows by id (PK-
        # indexed) and filter in-process. Supabase caps at 1000 rows anyway.
        self._last_attempt = time.time()
        try:
            creds = sbcreds.SupabaseCredentials(self.testnet, self.credsfile)
            params = {
                "select": "strat_id,symbol,created_at",
                "limit": str(PAIR_ROW_LIMIT),
                "order": "id.desc",
            }
            r = creds.get("position", params)
            if r.status_code != 200:
                return
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=PAIR_WINDOW_SECS)
            seen = set()
            pairs = []
            for row in r.json():
                if _parse_pg_ts(row["created_at"]) < cutoff:
                    continue
                key = (row["strat_id"], row["symbol"])
                if key not in seen:
                    seen.add(key)
                    pairs.append(key)
            pairs.sort()
            self.pairs = pairs
            self.last_fetch = time.time()
        except Exception:
            pass  # keep stale data


# ── Strat/Sym picker (multi-select) ─────────────────────────────────────────

def browse_pairs(stdscr, pairs):
    """Curses multi-select browser for (strat, sym). Returns list of selected or None."""
    curses.curs_set(0)

    CURSOR = curses.color_pair(1) | curses.A_BOLD
    HEADER = curses.color_pair(2) | curses.A_BOLD
    SELECTED = curses.color_pair(3)
    SORT_HDR = curses.color_pair(4) | curses.A_BOLD
    CUR_SEL = curses.color_pair(5) | curses.A_BOLD

    headers = ["", "strat_id", "symbol"]
    all_rows = [list(p) for p in pairs]
    display_rows = list(all_rows)
    selected = set()

    cur_row = 0
    cur_col = 1
    sort_col = None
    sort_reverse = False
    filters = {}
    locked_sym = None  # when multi-selecting, locked to this symbol
    COL_GAP = 2

    strat_w = max((len(s) for s, _ in pairs), default=10)
    sym_w = max((len(s) for _, s in pairs), default=10)
    col_widths = [3, min(strat_w, 55), min(sym_w, 25)]

    def apply_filters():
        result = all_rows
        for ci, val in filters.items():
            result = [r for r in result if r[ci - 1] == val]
        return result

    def do_sort(rows):
        if sort_col is None:
            return rows
        idx = sort_col - 1
        return sorted(rows, key=lambda r: r[idx] if idx < len(r) else "", reverse=sort_reverse)

    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        filter_info = ""
        if filters:
            parts = [f"{headers[ci]}={v}" for ci, v in filters.items()]
            filter_info = f"  [filters: {', '.join(parts)}]"
        if locked_sym:
            filter_info += f"  [locked: {locked_sym}]"
        title = f" Picker: {len(display_rows)} pairs, {len(selected)} selected{filter_info} "
        stdscr.addnstr(0, 0, title.ljust(max_x), max_x - 1, curses.A_REVERSE)

        status_y = max_y - 1
        visible_rows = max_y - 3

        # Header
        x = 0
        for ci, (hdr, w) in enumerate(zip(headers, col_widths)):
            label = hdr[:w].ljust(w)
            style = SORT_HDR if ci == cur_col else HEADER
            if sort_col == ci and ci > 0:
                ind = " v" if sort_reverse else " ^"
                label = (hdr[:w - 2] + ind).ljust(w) if w > 2 else label
            try:
                stdscr.addnstr(1, x, label, w, style)
            except curses.error:
                pass
            x += w + COL_GAP

        if cur_row < 0:
            cur_row = 0
        if display_rows and cur_row >= len(display_rows):
            cur_row = len(display_rows) - 1

        row_offset = max(0, cur_row - visible_rows + 1) if cur_row >= visible_rows else 0

        for ri in range(row_offset, min(row_offset + visible_rows, len(display_rows))):
            sy = 2 + (ri - row_offset)
            if sy >= status_y:
                break
            row = display_rows[ri]
            pk = (row[0], row[1])
            is_sel = pk in selected
            is_cur = ri == cur_row

            if is_cur and is_sel:
                style = CUR_SEL
            elif is_cur:
                style = CURSOR
            elif is_sel:
                style = SELECTED
            else:
                style = 0

            check = "[x]" if is_sel else "[ ]"
            try:
                stdscr.addnstr(sy, 0, check.ljust(col_widths[0]), col_widths[0], style)
            except curses.error:
                pass
            cx = col_widths[0] + COL_GAP
            for ci in range(2):
                val = row[ci] if ci < len(row) else ""
                cell = val[:col_widths[ci + 1]].ljust(col_widths[ci + 1])
                try:
                    stdscr.addnstr(sy, cx, cell, col_widths[ci + 1], style)
                except curses.error:
                    pass
                cx += col_widths[ci + 1] + COL_GAP

        status = " Space:toggle  a:all  n:none  Tab:sort  f:filter  r:reset  Enter:confirm  q/Esc:cancel"
        try:
            stdscr.addnstr(status_y, 0, status.ljust(max_x), max_x - 1, curses.A_REVERSE)
        except curses.error:
            pass

        stdscr.refresh()
        ch = stdscr.getch()

        if ch == curses.KEY_UP:
            cur_row = max(0, cur_row - 1)
        elif ch == curses.KEY_DOWN:
            if display_rows:
                cur_row = min(len(display_rows) - 1, cur_row + 1)
        elif ch == curses.KEY_LEFT:
            cur_col = max(0, cur_col - 1)
        elif ch == curses.KEY_RIGHT:
            cur_col = min(len(headers) - 1, cur_col + 1)
        elif ch in (ord(' '), ord('x')):
            if display_rows:
                row = display_rows[cur_row]
                pair = (row[0], row[1])
                if pair in selected:
                    selected.discard(pair)
                    # If nothing left, unlock: show all rows again
                    if not selected:
                        locked_sym = None
                        display_rows = do_sort(apply_filters())
                else:
                    if not selected:
                        # First selection: lock to this symbol
                        locked_sym = row[1]
                        selected.add(pair)
                        display_rows = do_sort([r for r in apply_filters()
                                                if r[1] == locked_sym])
                        cur_row = 0
                    else:
                        selected.add(pair)
                if cur_row < len(display_rows) - 1:
                    cur_row += 1
        elif ch == ord('a'):
            if display_rows:
                if not selected and display_rows:
                    locked_sym = display_rows[0][1]
                    display_rows = do_sort([r for r in apply_filters()
                                            if r[1] == locked_sym])
                for row in display_rows:
                    selected.add((row[0], row[1]))
        elif ch == ord('n'):
            selected.clear()
            locked_sym = None
            display_rows = do_sort(apply_filters())
        elif ch == ord('\t'):
            if cur_col > 0:
                if sort_col == cur_col:
                    sort_reverse = not sort_reverse
                else:
                    sort_col = cur_col
                    sort_reverse = False
                display_rows = do_sort(display_rows)
                cur_row = 0
        elif ch == ord('f'):
            if display_rows and cur_col > 0:
                filters[cur_col] = display_rows[cur_row][cur_col - 1]
                display_rows = do_sort(apply_filters())
                cur_row = 0
        elif ch == ord('r'):
            filters.clear()
            selected.clear()
            locked_sym = None
            display_rows = do_sort(list(all_rows))
            cur_row = 0
        elif ch in (curses.KEY_ENTER, 10, 13):
            if selected:
                return {"mode": "multi", "pairs": sorted(selected)}
            elif display_rows:
                row = display_rows[cur_row]
                if cur_col == 1:  # focused on strat_id
                    return {"mode": "strat", "strat": row[0]}
                elif cur_col == 2:  # focused on symbol
                    return {"mode": "sym", "sym": row[1]}
                else:  # on checkbox col, treat as full pair
                    return {"mode": "pair", "strat": row[0], "sym": row[1]}
            return None
        elif ch in (ord('q'), 27):  # q or Escape
            return None
        elif ch == curses.KEY_PPAGE:
            cur_row = max(0, cur_row - visible_rows)
        elif ch == curses.KEY_NPAGE:
            if display_rows:
                cur_row = min(len(display_rows) - 1, cur_row + visible_rows)


# ── UM picker ────────────────────────────────────────────────────────────────

def pick_um(stdscr, um_names):
    curses.curs_set(0)
    cur = 0
    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, " Select UM type (Enter:confirm  q/Esc:cancel) ".ljust(max_x),
                        max_x - 1, curses.A_REVERSE)
        for i, name in enumerate(um_names):
            style = curses.color_pair(1) | curses.A_BOLD if i == cur else 0
            try:
                stdscr.addnstr(i + 2, 2, name, max_x - 4, style)
            except curses.error:
                pass
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_UP:
            cur = max(0, cur - 1)
        elif ch == curses.KEY_DOWN:
            cur = min(len(um_names) - 1, cur + 1)
        elif ch in (curses.KEY_ENTER, 10, 13):
            return um_names[cur]
        elif ch in (ord('q'), 27):
            return None


# ── Main form ────────────────────────────────────────────────────────────────

FIELD_NAMES = ["strat", "sym", "um", "args"]
FIELD_LABELS = ["strat: ", "sym: ", "um: ", "args: "]

UM_ARG_TEMPLATES = {
    "SET_PLACE_THRESH":  "thresh:",
    "SET_THRESH_MULT":   "mult:",
    "SET_ORDER_SIZE":    "size:",
    "SET_SIZE_MULT":     "mult:",
    "SET_RELATIVE_BETA": "beta:",
    "SET_EXTRA_THRESHES":"buy:,sell:",
    "SET_CONST_PRED_PX": "px:",
    "FORCE_INHERIT":     "pos:",
    "SET_TL":            "tl:",
    "FORCE_INHERIT_LOOP":"",
    "SET_TL_REASON":     "tl:,reason:",
}

UM_EXAMPLES = [
    ("SET_PLACE_THRESH",  "thresh:0.001"),
    ("SET_THRESH_MULT",   "mult:1"),
    ("SET_ORDER_SIZE",    "size:2"),
    ("SET_SIZE_MULT",     "mult:1.5"),
    ("SET_RELATIVE_BETA", "beta:0.14"),
    ("SET_EXTRA_THRESHES","buy:0,sell:0.0001"),
    ("SET_CONST_PRED_PX", "px:100"),
    ("FORCE_INHERIT",     "pos:-20.1"),
    ("SET_TL",            "tl:1"),
    ("FORCE_INHERIT_LOOP",""),
    ("SET_TL_REASON",     "tl:1,reason:Usermsg"),
]


def run_form(stdscr, pair_cache, testnet, user, credsfile):
    curses.curs_set(1)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(6, curses.COLOR_RED, -1)

    LABEL_STYLE = curses.color_pair(2) | curses.A_BOLD
    ACTIVE_LABEL = curses.color_pair(4) | curses.A_BOLD
    STATUS_STYLE = curses.A_REVERSE
    MSG_OK = curses.color_pair(3) | curses.A_BOLD
    MSG_ERR = curses.color_pair(6) | curses.A_BOLD

    history = load_history()
    hist_idx = len(history)  # one past end = "new" entry

    fields = {"strat": "", "sym": "", "um": "", "args": ""}
    active_field = 0  # index into FIELD_NAMES
    cursor_pos = {f: 0 for f in FIELD_NAMES}  # per-field cursor position
    status_msg = ""
    status_style = MSG_OK

    def fields_to_entry():
        return {k: fields[k] for k in FIELD_NAMES}

    def entry_to_fields(entry):
        for k in FIELD_NAMES:
            fields[k] = entry.get(k, "")
            cursor_pos[k] = len(fields[k])

    def send_current():
        nonlocal history, hist_idx, status_msg, status_style

        strat = fields["strat"].strip()
        um_name = fields["um"].strip()
        if not strat:
            status_msg = "Error: strat is required"
            status_style = MSG_ERR
            return
        if not um_name or um_name not in UM_ID_MAP:
            status_msg = f"Error: invalid um (valid: {', '.join(sorted(UM_ID_MAP.keys()))})"
            status_style = MSG_ERR
            return

        sym = fields["sym"].strip() or None
        args_str = fields["args"].strip()
        um_args = None
        if args_str:
            try:
                um_args = {k: v
                           for k, v in [p.split(':') for p in args_str.split(',')]}
            except ValueError:
                status_msg = "Error: bad args format (use key:val,key:val)"
                status_style = MSG_ERR
                return

        try:
            # Suppress stdout from sendUM so it doesn't corrupt curses
            old_stdout = sys.stdout
            sys.stdout = open(os.devnull, "w")
            try:
                sendUM(testnet, user, strat, sym, um_name, um_args, credsfile)
            finally:
                sys.stdout.close()
                sys.stdout = old_stdout
            status_msg = f"Sent {um_name} -> {strat}" + (f" [{sym}]" if sym else "")
            status_style = MSG_OK
        except SystemExit as e:
            status_msg = f"Send failed: {e}"
            status_style = MSG_ERR
            return
        except Exception as e:
            status_msg = f"Send failed: {e}"
            status_style = MSG_ERR
            return

        entry = fields_to_entry()
        history = add_to_history(history, entry)
        save_history(history)
        hist_idx = len(history)

    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # Title bar
        pairs = pair_cache.get_pairs()
        stale = pair_cache.stale_secs()
        if stale < 0:
            age_str = "not loaded"
        elif stale < 60:
            age_str = f"{stale}s ago"
        else:
            age_str = f"{stale // 60}m ago"
        title = f" ium   {len(pairs)} live pairs (refreshed {age_str})   history: {len(history)} "
        stdscr.addnstr(0, 0, title.ljust(max_x), max_x - 1, STATUS_STYLE)

        # Separator
        try:
            stdscr.addnstr(1, 0, "─" * max_x, max_x - 1, curses.color_pair(2))
        except curses.error:
            pass

        # Fields (single horizontal row)
        field_start_y = 2
        GAP = 2
        labels_w = sum(len(l) for l in FIELD_LABELS) + GAP * (len(FIELD_NAMES) - 1)
        remaining = max_x - labels_w - 1
        # Give strat ~40% of space, others split the rest
        fw_strat = max(10, int(remaining * 0.4))
        fw_rest = max(5, (remaining - fw_strat) // 3)
        field_widths = [fw_strat, fw_rest, fw_rest, fw_rest]

        field_x = {}  # field_index -> (x_start_of_value, field_width)
        x = 0
        for fi in range(len(FIELD_NAMES)):
            label = FIELD_LABELS[fi]
            fw = field_widths[fi]
            field_x[fi] = (x + len(label), fw)
            style = ACTIVE_LABEL if fi == active_field else LABEL_STYLE
            try:
                stdscr.addnstr(field_start_y, x, label, len(label), style)
                val_str = fields[FIELD_NAMES[fi]]
                stdscr.addnstr(field_start_y, x + len(label),
                               val_str[:fw].ljust(fw), fw,
                               curses.A_UNDERLINE if fi == active_field else 0)
            except curses.error:
                pass
            x += len(label) + fw + GAP

        # Separator
        sep_y = field_start_y + 1
        try:
            stdscr.addnstr(sep_y, 0, "─" * max_x, max_x - 1, curses.color_pair(2))
        except curses.error:
            pass

        # Status message (extra padding for readability)
        msg_y = sep_y + 4
        if status_msg:
            try:
                stdscr.addnstr(msg_y, 2, status_msg[:max_x - 4], max_x - 4, status_style)
            except curses.error:
                pass

        # UM reference examples (anchored to bottom, above status bar)
        REF_STYLE = curses.color_pair(2)  # dim yellow
        ref_lines = len(UM_EXAMPLES) + 1  # +1 for header
        ref_start_y = max_y - 1 - ref_lines  # just above status bar
        # Only draw if there's enough room (don't overlap with form/status msg)
        if ref_start_y > msg_y + 1:
            try:
                stdscr.addnstr(ref_start_y, 2, "UM reference:", max_x - 4, REF_STYLE | curses.A_BOLD)
            except curses.error:
                pass
            for i, (um, args_ex) in enumerate(UM_EXAMPLES):
                ey = ref_start_y + 1 + i
                if ey >= max_y - 1:
                    break
                line = f"  {um:<20s} {args_ex}"
                try:
                    stdscr.addnstr(ey, 2, line, max_x - 4, REF_STYLE)
                except curses.error:
                    pass

        # Status bar
        status_bar = (" Tab:next  Enter:send  Up/Dn:history  "
                      "Alt-S:picker  Alt-U:um picker  q:quit")
        try:
            stdscr.addnstr(max_y - 1, 0, status_bar.ljust(max_x), max_x - 1, STATUS_STYLE)
        except curses.error:
            pass

        # Position cursor in active field
        fname = FIELD_NAMES[active_field]
        fx, fw = field_x[active_field]
        cx = fx + min(cursor_pos[fname], fw - 1)
        try:
            stdscr.move(field_start_y, min(cx, max_x - 2))
        except curses.error:
            pass

        stdscr.refresh()

        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            return

        fname = FIELD_NAMES[active_field]
        val = fields[fname]
        cpos = cursor_pos[fname]

        if ch == ord('\t'):  # Tab: next field
            active_field = (active_field + 1) % len(FIELD_NAMES)
        elif ch == curses.KEY_BTAB:  # Shift-Tab: autocomplete
            if fname == "um" and val:
                prefix = val[:cpos].upper()
                um_names = sorted(UM_ID_MAP.keys())
                matches = [u for u in um_names if u.startswith(prefix)]
                if len(matches) == 1:
                    fields["um"] = matches[0]
                    cursor_pos["um"] = len(matches[0])
                    if matches[0] in UM_ARG_TEMPLATES:
                        fields["args"] = UM_ARG_TEMPLATES[matches[0]]
                        cursor_pos["args"] = len(fields["args"])
                elif len(matches) > 1:
                    common = matches[0]
                    for m in matches[1:]:
                        while not m.startswith(common):
                            common = common[:-1]
                    if len(common) > len(prefix):
                        fields["um"] = common
                        cursor_pos["um"] = len(common)
                    else:
                        status_msg = "  ".join(matches)
                        status_style = MSG_OK
        elif ch in (curses.KEY_ENTER, 10, 13):  # Enter: send
            send_current()
        elif ch == curses.KEY_UP:  # history back
            if history:
                if hist_idx > 0:
                    hist_idx -= 1
                entry_to_fields(history[hist_idx])
        elif ch == curses.KEY_DOWN:  # history forward
            if hist_idx < len(history) - 1:
                hist_idx += 1
                entry_to_fields(history[hist_idx])
            elif hist_idx == len(history) - 1:
                hist_idx = len(history)
                for f in FIELD_NAMES:
                    fields[f] = ""
                    cursor_pos[f] = 0
        elif ch in (ord('q'),) and not val:  # q quits only if field is empty
            return
        elif ch == curses.KEY_LEFT:
            cursor_pos[fname] = max(0, cpos - 1)
        elif ch == curses.KEY_RIGHT:
            cursor_pos[fname] = min(len(val), cpos + 1)
        elif ch == curses.KEY_HOME:
            cursor_pos[fname] = 0
        elif ch == curses.KEY_END:
            cursor_pos[fname] = len(val)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if cpos > 0:
                fields[fname] = val[:cpos - 1] + val[cpos:]
                cursor_pos[fname] = cpos - 1
        elif ch == curses.KEY_DC:  # Delete
            if cpos < len(val):
                fields[fname] = val[:cpos] + val[cpos + 1:]
        elif ch == 11:  # Ctrl-K: kill to end of line
            fields[fname] = val[:cpos]
        elif ch == 1:  # Ctrl-A: beginning of line
            cursor_pos[fname] = 0
        elif ch == 5:  # Ctrl-E: end of line
            cursor_pos[fname] = len(val)
        elif ch == 23:  # Ctrl-W: delete word back
            i = cpos - 1
            while i > 0 and val[i - 1] == ' ':
                i -= 1
            while i > 0 and val[i - 1] != ' ':
                i -= 1
            fields[fname] = val[:i] + val[cpos:]
            cursor_pos[fname] = i
        elif ch == 27:  # ESC prefix (Alt+key combos)
            stdscr.timeout(50)  # wait up to 50ms for next byte
            nch = stdscr.getch()
            stdscr.timeout(-1)  # back to blocking
            if nch in (127, 8, curses.KEY_BACKSPACE):  # Alt+Backspace: delete word back
                i = cpos - 1
                while i > 0 and val[i - 1] == ' ':
                    i -= 1
                while i > 0 and val[i - 1] != ' ':
                    i -= 1
                fields[fname] = val[:i] + val[cpos:]
                cursor_pos[fname] = i
            elif nch == ord('d'):  # Alt+D: delete word forward
                i = cpos
                while i < len(val) and val[i] == ' ':
                    i += 1
                while i < len(val) and val[i] != ' ':
                    i += 1
                fields[fname] = val[:cpos] + val[i:]
            elif nch == ord('b'):  # Alt+B: word left
                i = cpos - 1
                while i > 0 and val[i - 1] == ' ':
                    i -= 1
                while i > 0 and val[i - 1] != ' ':
                    i -= 1
                cursor_pos[fname] = max(0, i)
            elif nch == ord('f'):  # Alt+F: word right
                i = cpos
                while i < len(val) and val[i] == ' ':
                    i += 1
                while i < len(val) and val[i] != ' ':
                    i += 1
                cursor_pos[fname] = i
            elif nch == ord('s'):  # Alt+S: strat/sym picker
                pairs = pair_cache.get_pairs()
                if pairs:
                    curses.curs_set(0)
                    result = browse_pairs(stdscr, pairs)
                    curses.curs_set(1)
                    if result:
                        mode = result["mode"]
                        if mode == "strat":
                            fields["strat"] = result["strat"]
                            cursor_pos["strat"] = len(fields["strat"])
                        elif mode == "sym":
                            fields["sym"] = result["sym"]
                            cursor_pos["sym"] = len(fields["sym"])
                        elif mode == "pair":
                            fields["strat"] = result["strat"]
                            fields["sym"] = result["sym"]
                            cursor_pos["strat"] = len(fields["strat"])
                            cursor_pos["sym"] = len(fields["sym"])
                        elif mode == "multi":
                            # Multi-select is locked to one sym, varying strats
                            selected_pairs = result["pairs"]
                            strats = sorted(set(s for s, _ in selected_pairs))
                            sym = selected_pairs[0][1]  # all same sym
                            if len(strats) == 1:
                                fields["strat"] = strats[0]
                            else:
                                fields["strat"] = "(" + "|".join(strats) + ")"
                            fields["sym"] = sym
                            cursor_pos["strat"] = len(fields["strat"])
                            cursor_pos["sym"] = len(fields["sym"])
                    status_msg = ""
                else:
                    status_msg = "No live pairs (try again after refresh)"
                    status_style = MSG_ERR
            elif nch == ord('u'):  # Alt+U: um picker
                um_names = sorted(UM_ID_MAP.keys())
                curses.curs_set(0)
                result = pick_um(stdscr, um_names)
                curses.curs_set(1)
                if result:
                    fields["um"] = result
                    cursor_pos["um"] = len(result)
                    if result in UM_ARG_TEMPLATES:
                        fields["args"] = UM_ARG_TEMPLATES[result]
                        cursor_pos["args"] = len(fields["args"])
                status_msg = ""
            # else: plain Esc — ignore
        elif 32 <= ch <= 126:  # printable
            fields[fname] = val[:cpos] + chr(ch) + val[cpos:]
            cursor_pos[fname] = cpos + 1


def main():
    parser = argparse.ArgumentParser(
        description="Interactive usermsg sender.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Main screen: editable form with strat/sym/um/args fields.
  Tab         Next field
  Shift-Tab   Previous field
  Up/Down     Cycle through send history
  Enter       Send the usermsg
  Alt-S       Open live strat/sym picker
  Alt-U       Open UM type picker
  Ctrl-K      Kill to end of line
  Ctrl-W      Delete word back
  q           Quit (when field is empty)
        """
    )
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--user", default=getpass.getuser())
    sbcreds.add_sb_creds_arg(parser)
    args = parser.parse_args()

    creds_path = args.sb_creds
    if args.vault and "vault" not in creds_path:
        creds_path = creds_path.replace(".Supabase", ".Supabase.vault")

    pair_cache = LivePairCache(args.testnet, creds_path)
    # Pre-fetch so the picker is ready
    pair_cache.get_pairs()

    curses.wrapper(lambda stdscr: run_form(stdscr, pair_cache, args.testnet, args.user, creds_path))


if __name__ == "__main__":
    main()
