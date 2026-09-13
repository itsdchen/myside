#!/usr/bin/env python

"""
SVKat - Interactive SimVariations Katamari

Browse SimVariations results interactively, pick the best variant from each,
then combine the selected pk_full.json files via PKatamari.

Usage:
    SVKat.py --dirs /path/v1_googl /path/v1_tsla [--out combined_pk.json] [--standardize]

Keys:
    Up/Down     Navigate rows
    Left/Right  Scroll columns horizontally
    Tab         Sort by highlighted column (toggle asc/desc)
    Enter       Select current row's variant
    s           Skip this symbol (don't include in combined config)
    q           Quit (abort)
"""

import argparse
import csv
import curses
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "stratbuilder"))
from PKatamari import combine


def parse_results(results_path):
    """Parse a results.txt CSV file. Returns (headers, rows) where rows is list of list of str."""
    with open(results_path, "r") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = [row for row in reader if row]
    return headers, rows


def try_numeric(val):
    """Try to convert a string to float for sorting purposes."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return val


def sort_rows(rows, col_idx, reverse):
    """Sort rows by column index, numeric if possible, else string."""
    def key_fn(row):
        if col_idx >= len(row):
            return (1, "")
        v = try_numeric(row[col_idx])
        if isinstance(v, float):
            return (0, v)
        return (1, v)
    return sorted(rows, key=key_fn, reverse=reverse)


def compute_col_widths(headers, rows, max_width=20):
    """Compute column widths based on header and data content."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(val))
    return [min(w, max_width) for w in widths]


def browse_table(stdscr, sv_dir, headers, rows, symbol_name=""):
    """Curses-based table browser. Returns the selected row, None if quit, or 'SKIP' to skip."""
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)    # selected row
    curses.init_pair(2, curses.COLOR_YELLOW, -1)                  # header
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # sort col header
    curses.init_pair(4, curses.COLOR_CYAN, -1)                    # highlighted col in data

    ROW_HIGHLIGHT = curses.color_pair(1) | curses.A_BOLD
    HEADER_STYLE = curses.color_pair(2) | curses.A_BOLD
    SORT_COL_HEADER = curses.color_pair(3) | curses.A_BOLD
    COL_HIGHLIGHT = curses.color_pair(4)

    cur_row = 0
    cur_col = 0
    col_offset = 0  # first visible column

    # Default sort by pct_positive descending.
    sort_col = headers.index("pct_positive") if "pct_positive" in headers else None
    sort_reverse = True
    all_rows = list(rows)
    if sort_col is not None:
        display_rows = sort_rows(list(rows), sort_col, sort_reverse)
    else:
        display_rows = list(rows)
    filters = {}  # col_idx -> value string

    col_widths = compute_col_widths(headers, rows)
    COL_GAP = 2

    def apply_filters():
        """Return rows that match all active filters."""
        result = all_rows
        for ci, val in filters.items():
            result = [r for r in result if ci < len(r) and r[ci] == val]
        return result

    def show_help():
        """Show a help overlay and wait for any key."""
        stdscr.erase()
        help_lines = [
            "SVKat Help",
            "",
            "  Up/Down       Navigate rows",
            "  Left/Right    Scroll columns",
            "  PgUp/PgDn     Jump one page",
            "  Home/End      Jump to first/last row",
            "",
            "  Tab           Sort by highlighted column (toggle asc/desc)",
            "  f             Filter: keep only rows matching current cell value",
            "  r             Reset: clear all filters",
            "  Enter         Select current row's variant",
            "  s             Skip this symbol",
            "  q             Quit (abort)",
            "  h / ?         Show this help",
            "",
            "  Press any key to close...",
        ]
        for i, line in enumerate(help_lines):
            try:
                stdscr.addstr(i + 1, 2, line)
            except curses.error:
                pass
        stdscr.refresh()
        stdscr.getch()

    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # Title bar (row 0)
        filter_info = ""
        if filters:
            parts = ["{}={}".format(headers[ci], v) for ci, v in filters.items()]
            filter_info = "  [filters: {}]".format(", ".join(parts))
        sym_label = " [{}]".format(symbol_name) if symbol_name else ""
        title = " SVKat:{} {} ({}/{} rows){} ".format(
            sym_label, os.path.basename(sv_dir), cur_row + 1, len(display_rows), filter_info)
        stdscr.addnstr(0, 0, title.ljust(max_x), max_x - 1, curses.A_REVERSE)

        # Figure out which columns fit on screen
        header_y = 1
        data_start_y = 2
        status_y = max_y - 1
        visible_data_rows = max_y - 3  # title + header + status

        # Compute visible columns from col_offset
        visible_cols = []
        used_x = 0
        for ci in range(col_offset, len(headers)):
            w = col_widths[ci] + COL_GAP
            if used_x + col_widths[ci] > max_x:
                break
            visible_cols.append((ci, used_x, col_widths[ci]))
            used_x += w

        # Ensure cur_col is visible
        if cur_col < col_offset:
            col_offset = cur_col
            # Recompute
            continue
        col_visible_idxs = [vc[0] for vc in visible_cols]
        if cur_col not in col_visible_idxs and cur_col >= col_offset:
            col_offset = cur_col
            continue

        # Draw header
        for ci, x_pos, w in visible_cols:
            label = headers[ci][:w].ljust(w)
            if ci == cur_col and sort_col == ci:
                style = SORT_COL_HEADER
            elif ci == cur_col:
                style = SORT_COL_HEADER
            elif sort_col == ci:
                style = HEADER_STYLE | curses.A_UNDERLINE
            else:
                style = HEADER_STYLE
            # Add sort indicator
            if sort_col == ci:
                indicator = " v" if sort_reverse else " ^"
                label = (headers[ci][:w - 2] + indicator).ljust(w) if w > 2 else label
            try:
                stdscr.addnstr(header_y, x_pos, label, w, style)
            except curses.error:
                pass

        # Scroll row window
        if cur_row < 0:
            cur_row = 0
        if cur_row >= len(display_rows):
            cur_row = len(display_rows) - 1

        # Determine row scroll offset
        row_offset = 0
        if cur_row >= visible_data_rows:
            row_offset = cur_row - visible_data_rows + 1

        # Draw data rows
        for ri in range(row_offset, min(row_offset + visible_data_rows, len(display_rows))):
            screen_y = data_start_y + (ri - row_offset)
            if screen_y >= status_y:
                break
            row = display_rows[ri]
            is_selected = (ri == cur_row)

            for ci, x_pos, w in visible_cols:
                val = row[ci] if ci < len(row) else ""
                # Right-align numeric values
                v = try_numeric(val)
                if isinstance(v, float):
                    cell = val[:w].rjust(w)
                else:
                    cell = val[:w].ljust(w)

                if is_selected:
                    style = ROW_HIGHLIGHT
                elif ci == cur_col:
                    style = COL_HIGHLIGHT
                else:
                    style = 0

                try:
                    stdscr.addnstr(screen_y, x_pos, cell, w, style)
                except curses.error:
                    pass

        # Status bar
        sort_info = ""
        if sort_col is not None:
            direction = "desc" if sort_reverse else "asc"
            sort_info = " | sort: {} {}".format(headers[sort_col], direction)
        status = " Nav:arrows  Tab:sort  f:filter  r:reset  Enter:select  s:skip  q:quit  h:help{}".format(sort_info)
        try:
            stdscr.addnstr(status_y, 0, status.ljust(max_x), max_x - 1, curses.A_REVERSE)
        except curses.error:
            pass

        stdscr.refresh()

        key = stdscr.getch()

        if key == curses.KEY_UP:
            cur_row = max(0, cur_row - 1)
        elif key == curses.KEY_DOWN:
            cur_row = min(len(display_rows) - 1, cur_row + 1)
        elif key == curses.KEY_LEFT:
            cur_col = max(0, cur_col - 1)
            if cur_col < col_offset:
                col_offset = cur_col
        elif key == curses.KEY_RIGHT:
            cur_col = min(len(headers) - 1, cur_col + 1)
        elif key == ord('\t'):
            # Sort by current column
            if sort_col == cur_col:
                sort_reverse = not sort_reverse
            else:
                sort_col = cur_col
                sort_reverse = True
            display_rows = sort_rows(display_rows, sort_col, sort_reverse)
            cur_row = 0
        elif key in (curses.KEY_ENTER, 10, 13):
            return display_rows[cur_row]
        elif key == ord('q'):
            return None
        elif key == ord('s'):
            return "SKIP"
        elif key == ord('f'):
            # Filter by current cell value
            if display_rows and cur_col < len(display_rows[cur_row]):
                val = display_rows[cur_row][cur_col]
                filters[cur_col] = val
                display_rows = apply_filters()
                if sort_col is not None:
                    display_rows = sort_rows(display_rows, sort_col, sort_reverse)
                cur_row = 0
        elif key == ord('r'):
            # Reset all filters
            filters.clear()
            display_rows = list(all_rows)
            if sort_col is not None:
                display_rows = sort_rows(display_rows, sort_col, sort_reverse)
            cur_row = 0
        elif key in (ord('h'), ord('?')):
            show_help()
        elif key == curses.KEY_PPAGE:  # Page Up
            cur_row = max(0, cur_row - visible_data_rows)
        elif key == curses.KEY_NPAGE:  # Page Down
            cur_row = min(len(display_rows) - 1, cur_row + visible_data_rows)
        elif key == curses.KEY_HOME:
            cur_row = 0
        elif key == curses.KEY_END:
            cur_row = len(display_rows) - 1


def interactive_select(sv_dir, symbol_name=""):
    """Run interactive selection for one SV dir. Returns pk_full.json path, 'SKIP', or None."""
    results_path = os.path.join(sv_dir, "results.txt")
    if not os.path.exists(results_path):
        print("Warning: {} not found, skipping".format(results_path))
        return "SKIP"

    headers, rows = parse_results(results_path)
    if not rows:
        print("Warning: {} has no data rows, skipping".format(results_path))
        return "SKIP"

    # Find the variant column index (should be first column)
    variant_col = 0
    if "variant" in headers:
        variant_col = headers.index("variant")

    selected_row = curses.wrapper(
        lambda stdscr: browse_table(stdscr, sv_dir, headers, rows, symbol_name))

    if selected_row is None:
        return None
    if selected_row == "SKIP":
        return "SKIP"

    variant_id = selected_row[variant_col]
    pk_path = os.path.join(sv_dir, "scratch", variant_id, "pk_full.json")

    if not os.path.exists(pk_path):
        print("Warning: {} does not exist".format(pk_path))
        return None

    return pk_path


def main():
    parser = argparse.ArgumentParser(description="Interactive SimVariations Katamari")
    parser.add_argument("--dirs", nargs="+", required=True,
                        help="SimVariations output directories to browse")
    parser.add_argument("--out", default="combined_pk.json",
                        help="Output path for combined config (default: combined_pk.json)")
    parser.add_argument("--standardize", default=False, action="store_true",
                        help="Standardize market hours to 09:00-22:00 ET")

    args = parser.parse_args()

    # Validate dirs
    for d in args.dirs:
        if not os.path.isdir(d):
            print("Error: {} is not a directory".format(d))
            sys.exit(1)

    selected_paths = []

    for sv_dir in args.dirs:
        sv_dir = os.path.abspath(sv_dir)
        # Find the traded symbol from any pk_full.json in the scratch dir.
        symbol_name = os.path.basename(sv_dir)
        scratch_dir = os.path.join(sv_dir, "scratch")
        if os.path.isdir(scratch_dir):
            for sub in os.listdir(scratch_dir):
                pk_full = os.path.join(scratch_dir, sub, "pk_full.json")
                if os.path.exists(pk_full):
                    try:
                        import json as _json
                        with open(pk_full) as _f:
                            symbol_name = _json.load(_f)["pktraders"][0]["traded_symbol"]
                    except Exception:
                        pass
                    break
        print("\n--- Selecting variant from: {} ---".format(symbol_name))
        pk_path = interactive_select(sv_dir, symbol_name)

        if pk_path is None:
            print("Aborted.")
            sys.exit(0)
        if pk_path == "SKIP":
            print("Skipped: {}".format(symbol_name))
            continue

        selected_paths.append(pk_path)
        print("Selected: {}".format(pk_path))

    print("\n--- Combining {} configs ---".format(len(selected_paths)))
    for p in selected_paths:
        print("  {}".format(p))

    combine(selected_paths, args.out, args.standardize)


if __name__ == "__main__":
    main()
