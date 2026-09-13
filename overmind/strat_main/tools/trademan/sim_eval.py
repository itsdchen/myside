"""Sim vs live evaluation pipeline.

Orchestrates: fetch → select → download market data → sim → compare.
"""

import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ── Path setup for imports from strat_main ───────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STRAT_MAIN = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _STRAT_MAIN not in sys.path:
    sys.path.insert(0, _STRAT_MAIN)

from util.pathing import bindir, datadir
from util.sim import sim_one_day


# ── Main entry point ─────────────────────────────────────────────────────────

def run_sim_eval(args, config):
    """Run the full sim-eval pipeline."""
    # Import sibling modules (same directory)
    from sim_eval_select import interactive_select
    from sim_eval_compare import (compare_day, build_comparison_report, load_acct_file,
                                    compute_cancel_fills, compute_order_rtt)
    from sim_eval_logs import parse_log_events

    # Parse dates
    dates = _parse_dates(args)
    date_strs = [d.strftime("%Y%m%d") for d in dates]

    local_base = getattr(args, "local_base", None) or config["local_base"]
    sim_base = getattr(args, "sim_dir", None) or os.path.expanduser("~/scratch/simeval")
    aliases = _parse_aliases(args, config)
    remote_base = config["remote_base"]
    date_offset = getattr(args, "date_offset", 0) or 0

    # ── 1. Fetch ─────────────────────────────────────────────────────────
    if not args.skip_fetch:
        print("=" * 80)
        print("STEP 1: Fetching files from remote machines")
        print("=" * 80)
        _fetch_all(aliases, dates, local_base, remote_base)
    else:
        print("[skip-fetch] Using local files only.")

    # ── 2. Discover available (strat, sym) pairs ─────────────────────────
    print()
    print("=" * 80)
    print("STEP 2: Discovering available (strat, sym) pairs")
    print("=" * 80)

    # Import load_acct_data from trade_manager (same directory)
    from trade_manager import load_acct_data

    acct_data = load_acct_data(local_base, aliases=aliases,
                               date_from=date_strs[0], date_to=date_strs[-1])
    if acct_data.empty:
        print("No acct data found for the specified dates. Run without --skip-fetch?")
        return

    # Apply strat/group substring filters
    strat_filter = getattr(args, "strat", None)
    group_filter = getattr(args, "group", None)
    if strat_filter:
        acct_data = acct_data[acct_data["strat"].str.contains(strat_filter, case=False)]
        print(f"  Filtered strats matching '{strat_filter}'")
    if group_filter:
        acct_data = acct_data[acct_data["group"].str.contains(group_filter, case=False)]
        print(f"  Filtered groups matching '{group_filter}'")
    sym_filter = getattr(args, "sym", None)
    if sym_filter:
        sym_parts = [s.strip().upper() for s in sym_filter.split(",")]
        acct_data = acct_data[acct_data["sym"].str.upper().apply(
            lambda s: any(part in s for part in sym_parts))]
        print(f"  Filtered syms matching '{sym_filter}'")
    if acct_data.empty:
        print("No data after filtering.")
        return

    pairs = (acct_data[["alias", "group", "strat", "sym"]]
             .drop_duplicates()
             .sort_values(["alias", "group", "strat", "sym"])
             .to_dict("records"))

    print(f"Found {len(pairs)} (strat, sym) pair(s).")

    # ── 3. Interactive selection ──────────────────────────────────────────
    if args.auto_all:
        selected = pairs
        print(f"Auto-selected all {len(selected)} pair(s).")
    else:
        selected = interactive_select(pairs)

    if not selected:
        print("Nothing selected.")
        return

    # ── 4. Download market data ──────────────────────────────────────────
    if not args.skip_md:
        print()
        print("=" * 80)
        print("STEP 3: Downloading market data")
        print("=" * 80)
        _download_market_data(selected, dates, date_offset, local_base)
    else:
        print("[skip-md] Skipping market data download.")

    # ── 5. Parse log events (needed before sims for schedule generation) ─
    log_events = parse_log_events(local_base, aliases, date_strs,
                                   event_types={"usermsg", "inherit"})
    if log_events:
        print(f"\nFound {len(log_events)} log event(s) (usermsg/inherit).")

    # ── 6. Run simulations ───────────────────────────────────────────────
    print()
    print("=" * 80)
    print("STEP 4: Running simulations")
    print("=" * 80)

    sim_results = _run_sims(selected, dates, date_offset, local_base, sim_base, log_events)

    # ── 7. Compare and report ────────────────────────────────────────────
    print()
    comparison_rows = []
    cancel_fill_data = {}
    rtt_data = {}
    for sr in sim_results:
        live_acct_path = _find_live_file(
            local_base, sr["alias"], sr["group"], sr["strat"],
            "acct", sr["live_date"]
        )
        rows = compare_day(
            sr["sim_acct_path"], live_acct_path,
            sr["alias"], sr["group"], sr["strat"],
            sr["live_date"], log_events=log_events,
        )
        # Filter to only selected syms for this strat
        selected_syms = {p["sym"] for p in selected
                         if p["alias"] == sr["alias"]
                         and p["group"] == sr["group"]
                         and p["strat"] == sr["strat"]}
        rows = [r for r in rows if r["sym"] in selected_syms]
        comparison_rows.extend(rows)

        # Compute cancel fill rates and RTT from orders files
        live_ord = _find_live_file(
            local_base, sr["alias"], sr["group"], sr["strat"],
            "orders", sr["live_date"]
        )
        sim_ord = os.path.join(
            sim_base, sr["alias"], sr["group"], sr["strat"],
            f"orders_simeval_{sr['live_date']}_{sr['sim_date']}.csv"
        )
        key = (sr["alias"], sr["group"], sr["strat"], sr["live_date"])

        live_cfr = compute_cancel_fills(live_ord)
        sim_cfr = compute_cancel_fills(sim_ord)
        if live_cfr or sim_cfr:
            cancel_fill_data[key] = {"live": live_cfr, "sim": sim_cfr}

        live_rtt = compute_order_rtt(live_ord)
        if live_rtt:
            rtt_data[key] = live_rtt

    report_text, comparison_df = build_comparison_report(
        comparison_rows, date_strs,
        cancel_fill_data=cancel_fill_data,
        rtt_data=rtt_data,
    )
    print(report_text, end="")

    # Save CSV
    os.makedirs(sim_base, exist_ok=True)
    csv_path = os.path.join(sim_base, "comparison_latest.csv")
    if not comparison_df.empty:
        comparison_df.to_csv(csv_path, index=False)
        print(f"Full comparison saved to: {csv_path}")

    # Email report if requested
    if getattr(args, "email", False) and report_text:
        from util import email_utils
        subject = f"Sim-Eval Report — {datetime.today().strftime('%Y-%m-%d')}"
        email_utils.send_mail(subject=subject, body=report_text, monospace=True)
        print("Report emailed.")


# ── Fetch helpers ────────────────────────────────────────────────────────────

def _fetch_all(aliases, dates, local_base, remote_base):
    """Fetch acct, trades, configs, orders, and INFO logs from remotes.

    scp_files dedupes per-file, so re-running is cheap: SSH discovery on each
    alias, then only missing files cross the wire.
    """
    from trade_manager import ssh_run, scp_files

    date_strs = [d.strftime("%Y%m%d") for d in dates]

    for alias in aliases:
        print(f"\n[{alias}] Discovering files...")

        # Build one big find command for everything we need
        name_patterns = []
        for ds in date_strs:
            name_patterns.append(f'-name "acct_{ds}.csv"')
            name_patterns.append(f'-name "trades_{ds}.csv"')
            name_patterns.append(f'-name "orders_{ds}.csv"')
            name_patterns.append(f'-name "pk_*.json.{ds}"')
            # Match both pktrade.HOST.log.INFO.* and pktrade_N.HOST.log.INFO.*
            name_patterns.append(f'-name "pktrade*.log.INFO.{ds}-*"')
        name_expr = " -o ".join(name_patterns)
        cmd = f'find {remote_base} -type f \\( {name_expr} \\)'
        lines = ssh_run(alias, cmd, timeout=60)

        if not lines:
            print(f"  No files found on {alias}")
            continue

        # Parse into (remote_path, group, strat, filename)
        base_len = len(remote_base.rstrip("/")) + 1
        by_dir = defaultdict(list)
        skipped = 0
        for line in lines:
            rel = line[base_len:]
            parts = rel.split("/")
            if len(parts) == 3:
                group, strat, fname = parts
            elif len(parts) == 2:
                group, fname = parts
                strat = "_root"
            else:
                continue

            local_path = os.path.join(local_base, alias, group, strat, fname)
            if os.path.exists(local_path):
                skipped += 1
                continue
            by_dir[(group, strat)].append(line)

        fetched = 0
        for (group, strat), paths in by_dir.items():
            local_dir = os.path.join(local_base, alias, group, strat)
            fetched += scp_files(alias, paths, local_dir)

        print(f"  Fetched {fetched}, skipped {skipped} (already local)")


# ── Market data helpers ──────────────────────────────────────────────────────

def _download_market_data(selected, dates, date_offset, local_base):
    """Download market data for selected configs via md_exists.py."""
    # Group by (alias, group, strat) to avoid duplicate downloads
    strat_configs = defaultdict(set)
    for p in selected:
        strat_configs[(p["alias"], p["group"], p["strat"])].add(p["sym"])

    md_exists_script = os.path.join(_STRAT_MAIN, "tools", "md_exists.py")
    python_bin = sys.executable

    for (alias, group, strat), syms in strat_configs.items():
        for date in dates:
            live_date_str = date.strftime("%Y%m%d")
            sim_date = date + timedelta(days=date_offset)
            sim_date_str = sim_date.strftime("%Y%m%d")
            # end date needs to be day after sim date (dates_list is exclusive of end)
            end_date_str = (sim_date + timedelta(days=1)).strftime("%Y%m%d")

            config_path = _find_dated_config(local_base, alias, group, strat, live_date_str)
            if not config_path:
                print(f"  [warn] No config for {alias}/{group}/{strat} on {live_date_str}, skipping md")
                continue

            print(f"  Checking data for {alias}/{group}/{strat} date={sim_date_str}...")
            cmd = [python_bin, md_exists_script,
                   "--pk", config_path,
                   "--start", sim_date_str, "--end", end_date_str]
            # Pipe "y" to auto-confirm S3 downloads
            subprocess.run(cmd, cwd=_STRAT_MAIN, input="y\n", text=True)


# ── Simulation helpers ───────────────────────────────────────────────────────

def _run_sims(selected, dates, date_offset, local_base, sim_base, log_events=None):
    """Run sims for each (strat, date) combination."""
    from sim_eval_logs import generate_usermsg_schedule, generate_inherit_schedule

    pk_bin = os.path.join(bindir(), "pktrade")

    # Group by strat — one sim per (strat, date) since all syms are in one config
    strat_dates = defaultdict(set)
    for p in selected:
        key = (p["alias"], p["group"], p["strat"])
        for date in dates:
            strat_dates[key].add(date)

    results = []
    for (alias, group, strat), date_set in strat_dates.items():
        strat_sim_dir = os.path.join(sim_base, alias, group, strat)
        os.makedirs(strat_sim_dir, exist_ok=True)
        run_info_path = os.path.join(strat_sim_dir, "run_info.txt")

        with open(run_info_path, "w") as run_info:
            run_info.write(f"# sim-eval run info\n")
            run_info.write(f"# generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            run_info.write(f"# alias={alias}  group={group}  strat={strat}\n\n")

            for date in sorted(date_set):
                live_date_str = date.strftime("%Y%m%d")
                sim_date = date + timedelta(days=date_offset)
                sim_date_str = sim_date.strftime("%Y%m%d")

                config_path = _find_dated_config(local_base, alias, group, strat, live_date_str)
                if not config_path:
                    print(f"  [warn] No config for {alias}/{group}/{strat} on {live_date_str}, skipping sim")
                    continue

                acct_suffix = f"simeval_{live_date_str}"
                trd_suffix = f"simeval_{live_date_str}"
                acct_file = f"acct_{acct_suffix}_{sim_date_str}.csv"
                trd_file = f"trd_{trd_suffix}_{sim_date_str}.csv"
                ord_file = f"orders_{acct_suffix}_{sim_date_str}.csv"

                # Generate schedule files from log events if available
                ord_path = os.path.join(strat_sim_dir, ord_file)
                extra_args = f"--ord {ord_path}"
                if log_events:
                    strat_events = [e for e in log_events
                                    if e["alias"] == alias and e["group"] == group
                                    and e["strat"] == strat and e["date"] == live_date_str]
                    if strat_events:
                        um_path = os.path.join(strat_sim_dir, f"usermsg_schedule_{live_date_str}.tsv")
                        um_count = generate_usermsg_schedule(
                            strat_events, live_date_str, um_path, sim_date_str=sim_date_str)
                        if um_count > 0:
                            extra_args += f" --global-usermsg-path {um_path}"
                            print(f"    Generated {um_count} usermsg schedule event(s)")

                        inh_path = os.path.join(strat_sim_dir, f"inherit_schedule_{live_date_str}.csv")
                        inh_count = generate_inherit_schedule(
                            strat_events, live_date_str, inh_path, sim_date_str=sim_date_str)
                        if inh_count > 0:
                            extra_args += f" --global-inherit-path {inh_path}"
                            print(f"    Generated {inh_count} inherit schedule event(s)")

                pkt_cmd = (f"{pk_bin} --date {sim_date_str} --conf {config_path} "
                           f"--acct {os.path.join(strat_sim_dir, acct_file)} "
                           f"--trd {os.path.join(strat_sim_dir, trd_file)} "
                           f"--ord {os.path.join(strat_sim_dir, ord_file)}")
                if extra_args:
                    pkt_cmd += extra_args

                run_info.write(f"live_date={live_date_str}  sim_date={sim_date_str}\n")
                run_info.write(f"config={config_path}\n")
                run_info.write(f"cmd={pkt_cmd}\n\n")

                print(f"  Simming {alias}/{group}/{strat}  sim_date={sim_date_str}  (live={live_date_str})")
                acct_path = sim_one_day(
                    pk_bin, strat_sim_dir, config_path, sim_date_str,
                    acct_suffix=acct_suffix,
                    trd_suffix=trd_suffix,
                    print_cmd=True,
                    extra_args=extra_args,
                )

                results.append({
                    "alias": alias,
                    "group": group,
                    "strat": strat,
                    "live_date": live_date_str,
                    "sim_date": sim_date_str,
                    "sim_acct_path": acct_path,
                    "config_path": config_path,
                })

        print(f"  Run info saved to: {run_info_path}")

    return results


# ── Utility ──────────────────────────────────────────────────────────────────

def _parse_dates(args):
    """Parse --date or --date-range into list of datetime objects."""
    if getattr(args, "date_range", None):
        start_s, end_s = args.date_range.split(":")
        start = datetime.strptime(start_s, "%Y%m%d")
        end = datetime.strptime(end_s, "%Y%m%d")
        dates = []
        cur = start
        while cur <= end:
            dates.append(cur)
            cur += timedelta(days=1)
        return dates
    elif getattr(args, "days", None):
        dates = []
        cur = datetime.today() - timedelta(days=1)  # start from yesterday
        while len(dates) < args.days:
            if cur.weekday() < 5:  # Mon-Fri
                dates.append(cur)
            cur -= timedelta(days=1)
        return sorted(dates)
    elif getattr(args, "date", None):
        return [datetime.strptime(args.date, "%Y%m%d")]
    else:
        print("Error: --date, --date-range, or --days is required for sim-eval.")
        sys.exit(1)


def _parse_aliases(args, config):
    """Parse aliases from args or fall back to config."""
    if getattr(args, "aliases", None):
        return [a.strip() for a in args.aliases.split(",")]
    return config["ssh_aliases"]


def _find_live_file(local_base, alias, group, strat, prefix, session_date_str):
    """Find a live file (acct, trades, orders) for a session date.

    Live files may use the session-start date in the filename, which can be
    D-1 for overnight sessions (start 6pm ET prior day) or D for intraday.
    We check candidate dates and verify by reading the file's timestamps to
    confirm it covers the target session.

    For trades/orders files (no header): the first token is a timestamp like
    "YYYYMMDD HH:MM:SS...". Sessions starting the prior evening will have
    first-line date = D-1 and last-line date = D (the session date).

    For acct files (has header): the date column contains the session-start
    date (same as the filename date), so we can't distinguish sessions by
    the data alone. Instead we use the same candidate logic and check that
    the file's date field matches the candidate filename date.
    """
    strat_dir = Path(local_base) / alias / group / strat
    has_header = (prefix == "acct")

    target = datetime.strptime(session_date_str, "%Y%m%d")
    candidates = [
        (target - timedelta(days=1)).strftime("%Y%m%d"),
        session_date_str,
        (target - timedelta(days=2)).strftime("%Y%m%d"),
    ]

    for cand_date in candidates:
        path = strat_dir / f"{prefix}_{cand_date}.csv"
        if not path.exists():
            continue
        try:
            with open(path) as f:
                first_line = f.readline().strip()
                if has_header:
                    first_line = f.readline().strip()  # skip header
            if not first_line:
                continue
            first_field = first_line.split(",")[0]
            ts_date = first_field.split()[0]

            if has_header:
                # Acct: date field = filename date = session-start date.
                # The file is valid if the data date matches the candidate.
                if ts_date == cand_date:
                    return str(path)
            else:
                # Trades/orders: verify the file spans the session date by
                # checking that the last line's date >= session_date_str.
                if ts_date == cand_date:
                    import subprocess
                    last_line = subprocess.run(
                        ["tail", "-1", str(path)], capture_output=True, text=True
                    ).stdout.strip()
                    if last_line:
                        last_field = last_line.split(",")[0]
                        last_ts_date = last_field.split()[0]
                        if last_ts_date >= session_date_str or cand_date == session_date_str:
                            return str(path)
        except Exception:
            continue

    # Fallback: return the exact date match even if we can't verify
    fallback = strat_dir / f"{prefix}_{session_date_str}.csv"
    if fallback.exists():
        return str(fallback)
    return None


def _find_dated_config(local_base, alias, group, strat, date_str):
    """Find pk_*.json.YYYYMMDD or *_relcross_v*.json.YYYYMMDD file locally for a given strat/date."""
    strat_dir = Path(local_base) / alias / group / strat
    if not strat_dir.exists():
        return None
    configs = list(strat_dir.glob(f"pk_*.json.{date_str}"))
    if not configs:
        # Also try gf{0,1,2,3}_*_relcross_v*.json.YYYYMMDD pattern (new naming)
        configs = list(strat_dir.glob(f"gf?_*relcross*.json.{date_str}"))
    if not configs:
        return None
    return str(configs[0])
