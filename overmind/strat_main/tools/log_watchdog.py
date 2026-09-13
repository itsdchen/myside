#!/usr/bin/env python3
"""
Log watchdog for pktrade processes.

Scans pktrade stdout log files for fatal errors (memory corruption, segfaults, etc.)
and sends email/ntfy alerts when new errors are found.

Designed to run via cron every few minutes:
    */3 * * * * /home/ubuntu/.venvs/v1/bin/python /home/ubuntu/scripts/log_watchdog.py

Historic mode (shows what it would have found, no alerts):
    python log_watchdog.py --historic --date 20260324

Usage:
    python log_watchdog.py                          # scan today's logs, alert on new errors
    python log_watchdog.py --dry-run                # scan today's logs, print but don't alert
    python log_watchdog.py --historic --date 20260324  # scan a past date, report findings
    python log_watchdog.py --date 20260324          # scan a specific date (live mode)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# email_utils is expected at /home/ubuntu/scripts/util/email_utils.py on the remote machine.
# For local development, fall back to the repo copy.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from util import email_utils

DEFAULT_LOG_DIR = "/home/ubuntu/estrader/logs"
DEFAULT_STATE_FILE = "/home/ubuntu/estrader/logs/.log_watchdog_state.json"

# (regex_pattern, label) - label is used in fingerprinting and reporting
FATAL_PATTERNS = [
    (r"malloc.*corrupt",                        "malloc_corruption"),
    (r"double free",                            "double_free"),
    (r"free\(\): invalid",                      "free_invalid"),
    (r"munmap_chunk.*invalid pointer",          "invalid_pointer"),
    (r"SIGABRT",                                "sigabrt"),
    (r"SIGSEGV|[Ss]egfault|Segmentation fault", "segfault"),
    (r"Aborted \(core dumped\)",                "aborted_core_dump"),
    (r"std::bad_alloc",                         "bad_alloc"),
    (r"terminate called",                       "terminate"),
    (r"stack smashing detected",                "stack_smash"),
    (r"buffer overflow detected",               "buffer_overflow"),
]

COMPILED_PATTERNS = [(re.compile(pat), label) for pat, label in FATAL_PATTERNS]

CONTEXT_LINES = 5  # lines before and after a match to include in the alert


def find_log_files(log_dir: str, date_str: str) -> list[Path]:
    """Find all pktrade stdout logs for a trading day.

    A trading day starts at 18:00 the previous calendar day, so we match logs
    from both the given date and the previous date.
    """
    log_path = Path(log_dir)
    prev_date_str = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    today_matches = set(log_path.glob(f"pk_stdout_*{date_str}*.txt"))
    prev_matches = set(log_path.glob(f"pk_stdout_*{prev_date_str}*.txt"))
    matches = sorted(today_matches | prev_matches)
    return matches


def fingerprint(label: str, line: str) -> str:
    """Create a stable fingerprint for a matched error line.

    Strips leading timestamps/line numbers so the same error produces the same
    fingerprint even if log lines shift.
    """
    # Strip common timestamp prefixes: "I0324 09:23:12.123456" or "2026-03-24 09:23:12"
    stripped = re.sub(r"^[IWEF]\d{4}\s+[\d:.]+\s+\d+\s+", "", line.strip())
    stripped = re.sub(r"^\d{4}-\d{2}-\d{2}\s+[\d:.]+\s*", "", stripped)
    raw = f"{label}:{stripped}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_state(state_file: str, date_str: str) -> dict:
    """Load the state file. Resets if the trading date has changed.

    We keep state for both the current and previous date's log files since a
    trading day spans two calendar dates (18:00 prev day to ~17:00 current day).
    """
    try:
        with open(state_file) as f:
            state = json.load(f)
        if state.get("date") != date_str:
            # New trading day. Carry over state for previous day's files (they're
            # still part of this trading day), but drop anything older.
            prev_date_str = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            old_files = state.get("files", {})
            carried = {k: v for k, v in old_files.items() if prev_date_str in k}
            return {"date": date_str, "files": carried}
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": date_str, "files": {}}


def save_state(state: dict, state_file: str):
    """Persist state to disk."""
    Path(state_file).parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def scan_file(filepath: Path, file_state: dict) -> tuple[list[dict], dict]:
    """Scan a single log file for new fatal errors.

    Returns (new_errors, updated_file_state) where each error is a dict with
    keys: label, line_num, line, context, fingerprint.
    """
    offset = file_state.get("offset", 0)
    seen = set(file_state.get("seen", []))

    # Read the entire file (we need context lines around matches)
    try:
        with open(filepath) as f:
            all_lines = f.readlines()
    except OSError as e:
        print(f"Warning: could not read {filepath}: {e}")
        return [], file_state

    new_errors = []
    total_lines = len(all_lines)

    # Only scan lines from offset onwards, but keep all lines for context
    for i in range(offset, total_lines):
        line = all_lines[i]
        for pattern, label in COMPILED_PATTERNS:
            if pattern.search(line):
                fp = fingerprint(label, line)
                if fp in seen:
                    continue
                seen.add(fp)

                # Gather context
                ctx_start = max(0, i - CONTEXT_LINES)
                ctx_end = min(total_lines, i + CONTEXT_LINES + 1)
                context_lines = []
                for j in range(ctx_start, ctx_end):
                    prefix = ">>>" if j == i else "   "
                    context_lines.append(f"{prefix} {j + 1:>6}: {all_lines[j].rstrip()}")

                new_errors.append({
                    "label": label,
                    "line_num": i + 1,
                    "line": line.strip(),
                    "context": "\n".join(context_lines),
                    "fingerprint": fp,
                })
                break  # one match per line is enough

    updated_state = {
        "offset": total_lines,
        "seen": sorted(seen),
    }
    return new_errors, updated_state


def format_report(all_errors: dict[str, list[dict]]) -> str:
    """Format all errors into a readable report.

    all_errors: {filename: [error_dicts]}
    """
    parts = []
    for filename, errors in all_errors.items():
        parts.append(f"{'=' * 60}")
        parts.append(f"FILE: {filename}")
        parts.append(f"{'=' * 60}")
        for err in errors:
            parts.append(f"\n[{err['label']}] line {err['line_num']}:")
            parts.append(err["context"])
        parts.append("")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Watchdog for pktrade log files.")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR,
                        help=f"Directory containing log files (default: {DEFAULT_LOG_DIR})")
    parser.add_argument("--date", default=None,
                        help="Date to scan in YYYYMMDD format (default: today)")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                        help=f"Path to state file (default: {DEFAULT_STATE_FILE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print findings but don't send alerts or update state")
    parser.add_argument("--historic", action="store_true",
                        help="Historic mode: scan a past date, report all findings, no alerts")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y%m%d")

    if args.historic and not args.date:
        parser.error("--historic requires --date")

    # In historic mode, don't use state (scan everything from scratch)
    if args.historic:
        state = {"date": date_str, "files": {}}
    else:
        state = load_state(args.state_file, date_str)

    log_files = find_log_files(args.log_dir, date_str)

    if not log_files:
        print(f"No log files found for {date_str} in {args.log_dir}")
        return

    print(f"Scanning {len(log_files)} log file(s) for {date_str}...")

    all_new_errors = {}
    total_new = 0

    for filepath in log_files:
        fname = filepath.name
        file_state = state.get("files", {}).get(fname, {})
        new_errors, updated_file_state = scan_file(filepath, file_state)

        if new_errors:
            all_new_errors[fname] = new_errors
            total_new += len(new_errors)

        if not args.historic and not args.dry_run:
            state.setdefault("files", {})[fname] = updated_file_state

    if total_new == 0:
        print("No new fatal errors found.")
        if not args.historic and not args.dry_run:
            save_state(state, args.state_file)
        return

    report = format_report(all_new_errors)

    if args.historic:
        print(f"\n--- Historic scan for {date_str}: {total_new} error(s) found ---\n")
        print(report)
        return

    if args.dry_run:
        print(f"\n--- Dry run: {total_new} new error(s) found ---\n")
        print(report)
        return

    # Send alerts
    subject = f"FATAL in pktrade logs ({date_str}): {total_new} error(s)"
    print(f"Sending alert: {subject}")
    try:
        email_utils.send_mail(subject=subject, body=report, monospace=True)
    except Exception as e:
        print(f"Failed to send email: {e}")
    try:
        # Summarize for ntfy (short message)
        files_affected = ", ".join(all_new_errors.keys())
        ntfy_msg = f"{total_new} fatal error(s) in: {files_affected}"
        email_utils.send_ntfy_alert(msg=ntfy_msg, title=subject, priority="high")
    except Exception as e:
        print(f"Failed to send ntfy: {e}")

    save_state(state, args.state_file)
    print("Done.")


if __name__ == "__main__":
    main()
