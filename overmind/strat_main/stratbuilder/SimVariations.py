#! /usr/bin/env python

"""
Not quite pnlclimb, but not unlike it either.

Gotta create a spec json, that specifies a set of overrides.
We sim the overrides over those days, then go back and score it.

I guess, importantly, I'm not actually using any of the templatizers.
Just going to spec out overrides.

"""

import datetime
import os
import subprocess
import sys
import argparse
import json
import copy
import types
import shutil
import time

import numpy as np
import pandas as pd


##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from util.chron import dates_list, dates_avail, blacklist
from util.pathing import bindir
import subprocess
from util.sim import sim_date_range, compute_acct_stats, _empty_stats
from util.log import getLogger
from stratbuilder import PnlClimb


pk_bin = os.path.join(bindir(), "pktrade")
scan_bin = os.path.join(bindir(), "signalscanner")
# Note: which binary to SHIP to the swarmhost pool is picked at dispatch
# time via overmind.swarmhost.experiment.select_remote_binary() based on
# the dispatcher's local distro (jammy → bin/pktrade; other → jammy.bin/pktrade).


default_variations = [
    # If a single setting, always override.
    {"path": ["settings", "commissions", "tiers", "BinanceFutures"], "val": "VIP1USDT"},
    {"path": ["pktraders", "ordex", "max_pos"], "val": ["10", "20", "40"]},
    {
        "path": ["pktraders", "ordex", "enter_thresh"],
        "val": [7.4e-5, 7.9e-5, 8.4e-5, 8.9e-5, 9.4e-5, 9.9e-5],
    },
    {
        "path": ["pktraders", "ordex", "alpha_mult"],
        "val": [0.17, 0.2, 0.2376, 0.24, 0.25, 0.28, 0.31],
    },
]


def gen_cache_fastmode(pk_dict, dates_to_sim, work_dir):
    cache_dir = os.path.join(work_dir, "fast_cache")
    if not os.path.exists(cache_dir):
        os.mkdir(cache_dir)

    # Step 1:
    tradecall_tempos = pk_dict["pktraders"][0]["tempos"]
    tradecall_name = tradecall_tempos[0]["name"]

    signals_dicts = pk_dict["pktraders"][0]["signals"]

    # Kind of assumes that the ordexes use this structure.

    ref_name = pk_dict["pktraders"][0]["ordex"].get("ref_sig", "")
    if ref_name == "":
        ref_name = pk_dict["pktraders"][0]["ordex"]["local_sig"]


    pred_name = pk_dict["pktraders"][0]["ordex"]["pred_sig"]

    # Creates the pred scanner file.
    # Can be used for creating both the pred and ref sigs.
    # Being a little silly, but I'm
    scan_dict = {
        "signalscanner": {"tradecall_tempo": tradecall_name},
        "tempos": tradecall_tempos,
        "signals": signals_dicts,
    }

    scan_path = os.path.join(cache_dir, "scan.json")
    with open(scan_path, "w") as f:
        f.write(json.dumps(scan_dict, indent=2))

    ref_vals_tmpl = os.path.join(cache_dir, "ref_vals_{}.csv")
    pred_vals_tmpl = os.path.join(cache_dir, "pred_vals_{}.csv")

    time_vals_tmpl = os.path.join(cache_dir, "times_{}.csv")

    scan_cmd = '{SCANNER_BIN} --date {DATE} --start "{START}" --end "{END}" --sampling-conf {SAMPLING_CONF} --mode FASTSIM --signals-path {DEST_PATH} --sigs {SIGNAL_NAME} --times-path {TIME_PATH}'

    procs = []
    for one_date in dates_to_sim:
        pred_dest = pred_vals_tmpl.format(one_date)
        ref_dest = ref_vals_tmpl.format(one_date)
        one_pred_scan_cmd = scan_cmd.format(
            SCANNER_BIN=scan_bin,
            DATE=one_date,
            START=pk_dict["settings"]["start_t"],
            END=pk_dict["settings"]["end_t"],
            SAMPLING_CONF=scan_path,
            DEST_PATH=pred_dest,
            SIGNAL_NAME=pred_name,
            TIME_PATH="/dev/null",
        )

        one_ref_scan_cmd = scan_cmd.format(
            SCANNER_BIN=scan_bin,
            DATE=one_date,
            START=pk_dict["settings"]["start_t"],
            END=pk_dict["settings"]["end_t"],
            SAMPLING_CONF=scan_path,
            DEST_PATH=ref_dest,
            SIGNAL_NAME=ref_name,
            TIME_PATH=time_vals_tmpl.format(one_date),
        )

        if not os.path.exists(pred_dest):
            pred_proc = subprocess.Popen(one_pred_scan_cmd, shell=True)
            procs.append(pred_proc)

        # Run them.
        # Not necessarily.
        if not os.path.exists(ref_dest):
            ref_proc = subprocess.Popen(one_ref_scan_cmd, shell=True)
            procs.append(ref_proc)

    for one_proc in procs:
        one_proc.wait()

    pred_sigfile_dct = {
        "name": pred_name,
        "type": "SigFile",
        "file_path_tmpl": pred_vals_tmpl,
    }

    ref_sigfile_dct = {
        "name": ref_name,
        "type": "SigFile",
        "file_path_tmpl": ref_vals_tmpl,
    }

    # In case we have local_ and remote_ sigs too.
    # This is a list, so down below we use + instead of putting it all in a list.
    actual_ref_sig_dcts = signals_dicts[-3:]

    # Switch this fastsim to using the actual refsig.
    fast_sigs_dicts = [pred_sigfile_dct] +  actual_ref_sig_dcts

    file_tempo_dct = {
        "name": tradecall_name,
        "type": "FileTempo",
        "file_path": time_vals_tmpl,
    }

    # This gets used by the fastsim process.
    return {"sigs": fast_sigs_dicts, "tempos": [file_tempo_dct]}


def write_results_file(results_path, idx_to_variant, idx_to_results, dummy_pnlclimber, metadata=None):
    """Write the CSV-shaped results table to `results_path`.

    Pure CSV — no comment lines — so `cat results.txt | sort -t, -k4 -n`
    style ranking works without -comment flags. If `metadata` is provided,
    it's written to a sibling `metadata.json` (machine-readable, easy to
    `jq`). Pass metadata=None on intermediate chunk writes; only the final
    call needs to update metadata.json.
    """
    if metadata:
        meta_path = os.path.join(os.path.dirname(results_path), "metadata.json")
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2, default=str)

    all_lines = ""
    top_line = "variant,"
    first_key = next(iter(idx_to_variant))
    for var_dest, var_val in idx_to_variant[first_key].items():
        top_line += "{},".format(var_dest[-1])
    top_line += "avg_pnl,avg_closed_pnl,sharpe,pct_positive,num_trds,fillrate,n_dates_ok,sim_score\n"

    all_lines += top_line

    for i in sorted(idx_to_results.keys()):
        cur_sim_results = idx_to_results[i]

        cur_sim_score = PnlClimb.PnlClimber.score_stats(dummy_pnlclimber, cur_sim_results, idx_to_results)

        # variant,values,avg_pnl,sharpe,avg_numtrds
        cur_line = "{},".format(i)
        for var_dest, var_val in idx_to_variant[i].items():
            cur_line += "{},".format(var_val)
        # Add the results from the sim summary too. n_dates_ok = how many
        # dates actually produced output; <full count means partial success.
        cur_line += "{:.2f},{:.2f},{:.2f},{:.2f},{:.0f},{:.4f},{:d},{:.2f}\n".format(
            cur_sim_results["avg_pnl"],
            cur_sim_results["avg_closed_pnl"],
            cur_sim_results["sharpe"],
            cur_sim_results["pct_positive"],
            cur_sim_results["avg_numtrds"],
            cur_sim_results.get("avg_fillrate", 0.0),
            int(cur_sim_results.get("n_dates_succeeded", 0)),
            cur_sim_score
        )

        all_lines += cur_line

    with open(results_path, "w") as f:
        f.write(all_lines)
    return all_lines


def sim_grid(overrides_dct, pk_dct, workdir, dates_to_sim, fast_sim=True, full_output=False, resume=False, random_sample=0, remote=False, hosts_yaml=None, chunk_size=50, dry_run=False, extra_args="", allow_missing_data=False):
    # Let's make this an abspath.
    if not os.path.exists(workdir):
        os.mkdir(workdir)

    sim_logger = getLogger("SimVariations", "simvariations.log")


    scratch_dir = os.path.abspath(os.path.join(workdir, "scratch"))
    # Turn it into an abspath. Makes things easier.

    if not os.path.exists(scratch_dir):
        os.mkdir(scratch_dir)

    # Make a shadow version 
    full_pk_dct = copy.deepcopy(pk_dct)
    

    # If we're running fast, then generate the cache.
    if fast_sim:
        sig_tempo_paths = gen_cache_fastmode(pk_dct, dates_to_sim, scratch_dir)
        pk_dct["pktraders"][0]["signals"] = sig_tempo_paths["sigs"]
        pk_dct["pktraders"][0]["tempos"] = sig_tempo_paths["tempos"]

    # We have to read the overrides_settings and turn them into the useful
    # variations dicts.

    # Let's just store this elsewhere, yeah?
    fixed_overrides = {}
    varying_setting_dests = []
    varying_overrides = {}

    idx_to_variant = {}

    # This doubles as the num variations.
    running_quotient = 1
    # As we enumerate, for all varying settings we gotta know
    # what to divide and mod by.
    setting_dest_to_mod = {}
    setting_dest_to_div = {}


    # Give it a scoring spec. 
    score_spec = {
        "style": "wins_pnl_charminvol_pct",
        "vol_limit": 50,
        "vol_style": "avg_med_numtrds",
        "vol_scale": "linear_frac_bounded",
        "pct_adjust_powr": 2.5,
        "penalty": -10,
        "penalty_mult": 1.5,
        "fillrate_target": 0.005,
    }
    dummy_object = types.SimpleNamespace(name="PnlClimber")
    dummy_object.scoring_spec = score_spec

    # Let it score stuff later. 

    # Quick explain for a dumby like me.
    # Enumerating values like
    # a: [1, 2, 3]
    # b: [1, 2, 3, 4, 5]
    # There are 3*5 = 15 variations.
    # setting_dest_to_mod is like {a: 3, b: 5}
    # settign_dest_to_div is like {a: 1, b: 3}
    # Where the divisor is accumulating from the mods of the previous
    # versions. That way, as we enumerate from 0 to 14,
    # a value goes from i%3, and b value goes from (i/3)%5

    for setting_dict in overrides_dct:
        setting_dest = tuple(setting_dict["path"])
        setting_values = setting_dict["val"]

        if isinstance(setting_values, list):
            # Then yes.
            setting_dest_to_mod[setting_dest] = len(setting_values)
            setting_dest_to_div[setting_dest] = running_quotient
            # This happens after we use the old running_quotient.
            running_quotient *= len(setting_values)
            # Just use the name here.
            varying_setting_dests.append(setting_dest)
            varying_overrides[setting_dest] = setting_values
        else:
            # Nothing really happens here.
            fixed_overrides[setting_dest] = setting_values

    # Determine which variants to run (before creating confs).
    if random_sample > 0 and random_sample < running_quotient:
        import random
        random.seed(42)
        variants_to_run = sorted(random.sample(range(running_quotient), random_sample))
        sim_logger.info(f"Random sample: {random_sample} of {running_quotient} variants")
    else:
        variants_to_run = list(range(running_quotient))

    # Enumerate only the variants we'll actually run.
    for i in variants_to_run:
        cur_varying_overrides = {}
        for setting_dest in varying_setting_dests:
            the_mod = setting_dest_to_mod[setting_dest]
            the_div = setting_dest_to_div[setting_dest]

            setting_val = varying_overrides[setting_dest][(i // the_div) % the_mod]
            cur_varying_overrides[setting_dest] = setting_val

        idx_to_variant[i] = cur_varying_overrides

    # Dry-run short-circuit: print what would dispatch and exit. Done after
    # variant enumeration so the user sees actual counts and varying axes,
    # but before any conf file is written / mktdata staged / sims kicked off.
    if dry_run:
        print(f"DRY RUN — would dispatch:")
        print(f"  variants:       {len(variants_to_run)} (of grid size {running_quotient})")
        print(f"  dates:          {len(dates_to_sim)} ({', '.join(dates_to_sim[:5])}"
              f"{'...' if len(dates_to_sim) > 5 else ''})")
        print(f"  total jobs:     {len(variants_to_run) * len(dates_to_sim)}")
        print(f"  varying axes:   {[d[-1] for d in varying_setting_dests]}")
        print(f"  fixed overrides:{list(fixed_overrides.keys())}")
        print(f"  workdir:        {workdir}")
        if remote:
            print(f"  mode:           REMOTE (chunk_size={chunk_size}, hosts_yaml={hosts_yaml or 'default'})")
        else:
            print(f"  mode:           local")
        return

    # OK, cool. I guess now we can write the modified pktrade conf.

    idx_to_results = {}

    # Create confs only for variants we'll actually run.
    for i in variants_to_run:
        variant_dir = os.path.join(scratch_dir, str(i))
        if not os.path.exists(variant_dir):
            os.mkdir(variant_dir)
        # Write the conf there.

        pk_variant = copy.deepcopy(pk_dct)

        # Apply fixed overrides.
        for fixed_var_dest, fixed_var_val in fixed_overrides.items():
            # Note that fixed_var_dest is a tuple...
            cur_sub_setting = pk_variant

            counter = 1
            # Iterate until we're at the last level.
            # And then overwrite the appropriate value.
            for one_level_label in fixed_var_dest:
                if counter == len(fixed_var_dest):
                    # Then we're at the end.
                    break

                cur_sub_setting = cur_sub_setting[one_level_label]
                # One issue: right now, only override the first level
                # of any list.
                # This is fine for ordex-level changes though...
                if isinstance(cur_sub_setting, list):
                    cur_sub_setting = cur_sub_setting[0]

                counter += 1

            # In the case we're adding something. entirely new.
            if fixed_var_dest[-1] not in cur_sub_setting:
                cur_sub_setting[fixed_var_dest[-1]] = fixed_var_val
            else:
                if isinstance(cur_sub_setting[fixed_var_dest[-1]], list):
                    cur_sub_setting[fixed_var_dest[-1]][0] = [fixed_var_val]
                else:
                    cur_sub_setting[fixed_var_dest[-1]] = fixed_var_val

        # Apply varying params.
        for var_dest, var_val in idx_to_variant[i].items():
            cur_sub_setting = pk_variant
            counter = 1

            for one_level_label in var_dest:
                if counter == len(var_dest):
                    break

                cur_sub_setting = cur_sub_setting[one_level_label]
                if isinstance(cur_sub_setting, list):
                    cur_sub_setting = cur_sub_setting[0]

                counter += 1
            if var_dest[-1] not in cur_sub_setting:
                cur_sub_setting[var_dest[-1]] = var_val
            else:

                if isinstance(cur_sub_setting[var_dest[-1]], list):
                    cur_sub_setting[var_dest[-1]][0] = [var_val]
                else:
                    cur_sub_setting[var_dest[-1]] = var_val

        # OK, the pk conf should be ready.
        pk_dest = os.path.join(variant_dir, "pk.json")
        with open(pk_dest, "w") as f:
            f.write(json.dumps(pk_variant, indent=2))


        full_pk_variant = copy.deepcopy(pk_variant)
        # But set the signals and tempos to the full version.
        full_pk_variant["pktraders"][0]["signals"] = full_pk_dct["pktraders"][0]["signals"]
        full_pk_variant["pktraders"][0]["tempos"] = full_pk_dct["pktraders"][0]["tempos"]


        full_pk_dest = os.path.join(variant_dir, "pk_full.json")

        with open(full_pk_dest, "w") as f:
            f.write(json.dumps(full_pk_variant, indent=2))

        # Done. On to the next one.

    results_file = os.path.join(workdir, "results.txt")

    # Branch: remote-dispatch to swarmhost vs. local serial loop.
    if remote:
        if extra_args:
            sim_logger.warning(f"extra_args={extra_args!r} ignored in remote mode "
                               "(swarmhost dispatcher's cmd builder doesn't thread it through yet)")
        _run_variants_remote(
            variants_to_run, dates_to_sim, scratch_dir, workdir, results_file,
            idx_to_variant, idx_to_results, dummy_object, sim_logger,
            hosts_yaml=hosts_yaml, chunk_size=chunk_size, resume=resume,
            allow_missing_data=allow_missing_data,
        )
    else:
        # Existing local-serial flow.
        for i in variants_to_run:
            variant_dir = os.path.join(scratch_dir, str(i))
            pk_dest = os.path.join(variant_dir, "pk.json")
            results_cache = os.path.join(variant_dir, "sim_results.json")

            # If resuming and we have cached results, skip the sim.
            if resume and os.path.exists(results_cache):
                with open(results_cache, "r") as f:
                    idx_to_results[i] = json.load(f)
                sim_logger.info(f"Resuming: loaded cached results for variant {i}")
                continue

            # Run the sim.
            sim_results = sim_date_range(
                pk_bin,
                variant_dir,
                pk_dest,
                dates_to_sim,
                str(i),
                trd_suffix=str(i),
                full_output=full_output,
                print_cmd=True,
                logger=sim_logger,
                extra_args=extra_args,
            )
            idx_to_results[i] = sim_results
            # Cache the results for potential resume.
            with open(results_cache, "w") as f:
                json.dump(sim_results, f)
            write_results_file(results_file, idx_to_variant, idx_to_results, dummy_object)

            # Also just write the results every so often, if we want intermediate results.
            if i % 10 == 0:
                write_results_file(results_file, idx_to_variant, idx_to_results, dummy_object)


    # Write out the results. In remote mode the helper already wrote a
    # version with full metadata header — re-reading and re-writing here
    # without metadata would clobber that. Just read+display.
    if remote:
        with open(results_file) as f:
            all_lines = f.read()
    else:
        all_lines = write_results_file(results_file, idx_to_variant, idx_to_results, dummy_object)
    print("Finished simming. Results are:")
    print(all_lines)


def _emit_local_repro_script(workdir, dates_to_sim):
    """Write <workdir>/repro.sh — re-runs one variant from this sweep locally.

    Outputs land under <workdir>/scratch/<i>/local/<date>/ so they don't
    clobber the remote-fetched acct_<i>_<date>.csv files. Useful when you
    pick a winner from the sweep and want to inspect its trades/logs that
    don't come back from remote.
    """
    # The repro runs the binary LOCALLY, so use the native local pktrade
    # (matches local glibc; no docker round-trip).
    from overmind.swarmhost.experiment import select_local_pktrade
    pkbin = select_local_pktrade()
    repro_path = os.path.join(workdir, "repro.sh")
    dates_bash = " ".join(dates_to_sim)
    script = f"""#!/usr/bin/env bash
# Re-run one variant from this sweep locally for inspection.
# Auto-generated by SimVariations remote dispatch.
#
# Usage:
#   bash {repro_path} <variant_id>                      # run all sweep dates
#   bash {repro_path} <variant_id> <date> [date...]     # run specific dates
#   JOBS=10 bash {repro_path} <variant_id>              # 10-way parallel (default 5)
#
# Sims run in parallel up to JOBS at a time (default 5). Each date's stdout/
# stderr goes to its per-date dir as `stdout.log`, so output doesn't interleave.
#
# Outputs land in <workdir>/scratch/<variant_id>/local/<date>/  (does NOT
# overwrite the remote-fetched acct_<i>_<date>.csv files at the variant root).

set -euo pipefail
HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
PKBIN="{pkbin}"
DEFAULT_DATES=({dates_bash})
JOBS="${{JOBS:-5}}"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <variant_id> [date...]" >&2
    echo "       available variants: $(ls "$HERE/scratch" 2>/dev/null | tr '\\n' ' ')" >&2
    exit 2
fi
VARIANT="$1"; shift
DATES=("${{@:-${{DEFAULT_DATES[@]}}}}")

SCRATCH="$HERE/scratch/$VARIANT"
[[ -d "$SCRATCH" ]] || {{ echo "no such variant dir: $SCRATCH" >&2; exit 1; }}
CONF="$SCRATCH/pk.json"

echo "running ${{#DATES[@]}} date(s) at JOBS=$JOBS parallel"

run_one() {{
    local d="$1"
    local out="$SCRATCH/local/$d"
    mkdir -p "$out"
    cd "$out"
    "$PKBIN" --date "$d" --conf "$CONF" \\
        --acct "$out/acct_$d.csv" \\
        --trd "$out/trades_$d.csv" \\
        --out-dir "$out" > "$out/stdout.log" 2>&1
    local rc=$?
    echo "  [$d] done rc=$rc  ($out)"
    return $rc
}}
export -f run_one
export SCRATCH CONF PKBIN

# Throttled background-job pool: keep at most $JOBS sims in flight.
fail=0
for d in "${{DATES[@]}}"; do
    run_one "$d" &
    while [[ "$(jobs -r | wc -l)" -ge "$JOBS" ]]; do
        wait -n || fail=1
    done
done
# Drain stragglers.
while wait -n 2>/dev/null; do :; done

echo
echo "done. trades + acct under each $SCRATCH/local/<date>/"
echo "(per-date stdout in $SCRATCH/local/<date>/stdout.log)"
[[ $fail -eq 0 ]] || echo "NOTE: at least one sim returned non-zero — see stdout.log files"
"""
    with open(repro_path, "w") as f:
        f.write(script)
    os.chmod(repro_path, 0o755)


def _attach_sweep_log(logger, workdir):
    """Add a FileHandler to `logger` that writes to <workdir>/sweep.log.

    Returns the handler so the caller can detach it at sweep end (otherwise
    a long-running process reusing the logger leaks an open FD per sweep).
    """
    import logging
    log_path = os.path.join(workdir, "sweep.log")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.DEBUG)  # capture everything, even if console handler is INFO
    logger.addHandler(fh)
    return fh


def _remote_ledger_path(workdir):
    return os.path.join(workdir, "remote_chunks.jsonl")


def _remote_state_path(workdir):
    return os.path.join(workdir, "remote_state.json")


def _utc_now():
    return datetime.datetime.utcnow()


def _utc_now_str():
    return _utc_now().isoformat() + "Z"


def _append_remote_chunk_event(workdir, event):
    event = dict(event)
    event["ts"] = _utc_now_str()
    path = _remote_ledger_path(workdir)
    with open(path, "a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def _write_remote_state(workdir, state):
    state = dict(state)
    state["updated_at"] = _utc_now_str()
    path = _remote_state_path(workdir)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _read_remote_chunk_states(workdir, sim_logger):
    states = {}
    path = _remote_ledger_path(workdir)
    if not os.path.exists(path):
        return states
    with open(path) as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                sim_logger.warning(f"ignoring bad ledger line {path}:{lineno}: {e}")
                continue
            run_id = event.get("run_id")
            if not run_id:
                continue
            cur = states.setdefault(run_id, {})
            cur.update(event)
            status = event.get("status")
            cur["last_status"] = status or cur.get("last_status")
            # Heartbeats describe progress within a submitted chunk; they do
            # not supersede its lifecycle state.  If the dispatcher dies
            # while fetching, the heartbeat is commonly the final ledger
            # event and the chunk must still be recovered on restart.
            if status and status != "heartbeat":
                cur["last_lifecycle_status"] = status
    return states


def _remote_run_live_count(pool, experiment_id, run_id):
    from overmind.swarmhost import _ssh

    pattern = f"/opt/pktrade/experiments/{experiment_id}/runs/{run_id}/"
    total = 0
    for host in pool.hosts:
        cmd = (
            "ps -eo comm,args --no-headers | "
            f"grep -F {pattern!r} | "
            "awk '$1 == \"timeout\" || $1 == \"pktrade\" {n++} END {print n+0}'"
        )
        try:
            res = _ssh.run(host.alias, cmd, timeout=10, check=False)
        except _ssh.SshError:
            continue
        try:
            total += int(res.stdout.strip() or "0")
        except ValueError:
            pass
    return total


def _score_fetched_remote_chunk(fetched_to, chunk, dates_to_sim, scratch_dir,
                                idx_to_results, sim_logger,
                                require_complete_status=False):

    scored = 0
    skipped = 0
    for i in chunk:
        variant_dir = os.path.join(scratch_dir, str(i))
        confname = f"v_{i}.json"
        conf_stem = os.path.splitext(confname)[0]
        local_acct_files = []
        status_files = []
        for d in dates_to_sim:
            status_src = os.path.join(str(fetched_to), conf_stem, d, "job.status")
            if os.path.exists(status_src):
                status_files.append(status_src)
            src = os.path.join(str(fetched_to), conf_stem, d, f"acct_{d}.csv")
            dst = os.path.join(variant_dir, f"acct_{i}_{d}.csv")
            if os.path.exists(src):
                shutil.copy2(src, dst)
                local_acct_files.append(dst)

        if require_complete_status and len(status_files) < len(dates_to_sim):
            sim_logger.warning(
                f"variant {i}: recovery found {len(status_files)}/{len(dates_to_sim)} "
                "job.status files; leaving variant pending")
            skipped += 1
            continue

        if local_acct_files:
            sim_results = compute_acct_stats(local_acct_files,
                                             dates_count=len(dates_to_sim))
        else:
            sim_logger.warning(f"variant {i}: 0 successful dates; using empty stats")
            sim_results = _empty_stats()
        idx_to_results[i] = sim_results

        cache_path = os.path.join(variant_dir, "sim_results.json")
        with open(cache_path, "w") as f:
            json.dump(sim_results, f)
        scored += 1
    return scored, skipped


def _recover_remote_chunks(workdir, scratch_dir, results_file, idx_to_variant,
                           idx_to_results, scoring_object, dates_to_sim,
                           pool, remote_bin, sim_logger):
    from overmind.swarmhost.experiment import Experiment

    states = _read_remote_chunk_states(workdir, sim_logger)
    recoverable = [
        state for state in states.values()
        if state.get("last_lifecycle_status", state.get("last_status")) == "submitted"
    ]
    if not recoverable:
        return 0

    recovered = 0
    for state in sorted(recoverable, key=lambda s: s.get("chunk_idx", 0)):
        experiment_id = state["experiment_id"]
        run_id = state["run_id"]
        chunk = [int(i) for i in state.get("variants", [])]
        if not chunk:
            continue

        sim_logger.info(f"recovering remote chunk {run_id} from experiment {experiment_id}")
        _write_remote_state(workdir, {
            "state": "recovering_chunk",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "chunk_idx": state.get("chunk_idx"),
            "variants_in_chunk": len(chunk),
            "last_check": _utc_now_str(),
        })
        while True:
            live = _remote_run_live_count(pool, experiment_id, run_id)
            _write_remote_state(workdir, {
                "state": "recovering_chunk",
                "experiment_id": experiment_id,
                "run_id": run_id,
                "chunk_idx": state.get("chunk_idx"),
                "variants_in_chunk": len(chunk),
                "live_remote_procs": live,
                "last_check": _utc_now_str(),
            })
            if live == 0:
                break
            sim_logger.info(f"recovery: {run_id} still has {live} live remote proc(s); waiting")
            time.sleep(10)

        _write_remote_state(workdir, {
            "state": "fetching_recovered_chunk",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "chunk_idx": state.get("chunk_idx"),
            "variants_in_chunk": len(chunk),
            "last_check": _utc_now_str(),
        })
        exp = Experiment(experiment_id=experiment_id, name="recovered",
                         binary_path=remote_bin, confs=[], binhash="")
        fetched_to = exp._fetch_run(pool, run_id)
        scored, skipped = _score_fetched_remote_chunk(
            fetched_to, chunk, dates_to_sim, scratch_dir, idx_to_results,
            sim_logger, require_complete_status=True)
        write_results_file(results_file, idx_to_variant, idx_to_results, scoring_object)
        _append_remote_chunk_event(workdir, {
            "status": "scored",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "chunk_idx": state.get("chunk_idx"),
            "variants": chunk,
            "n_variants_scored": scored,
            "n_variants_skipped": skipped,
            "recovered": True,
        })
        sim_logger.info(f"recovered {run_id}; scored {scored} variant(s), "
                        f"left {skipped} incomplete variant(s) pending")
        _write_remote_state(workdir, {
            "state": "recovered_chunk",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "chunk_idx": state.get("chunk_idx"),
            "variants_scored": scored,
            "variants_skipped": skipped,
            "last_check": _utc_now_str(),
        })
        recovered += scored
    return recovered


def _run_variants_remote(variants_to_run, dates_to_sim, scratch_dir, workdir,
                         results_file, idx_to_variant, idx_to_results,
                         scoring_object, sim_logger, hosts_yaml=None,
                         chunk_size=50, resume=False, allow_missing_data=False):
    """Dispatch variant sims to a swarmhost pool, chunked.

    Architecture:
      - One Experiment created from ALL un-cached variants' confs (bundled
        as v_<i>.json so basenames don't collide).
      - Pushed once to the host pool.
      - Mktdata staged once per host via mktdata.stage_for_conf (uses
        pktrade --dry-run to discover what's needed).
      - Variants run in chunks of `chunk_size`. After each chunk:
          * Fetched outputs are reshaped into <scratch>/<i>/acct_<i>_<d>.csv
            so existing scoring + cache layout works unchanged.
          * Per-variant sim_results.json cached.
          * results.txt rewritten with everything accumulated so far.
      - Partial success: a variant with ≥1 successful date is still scored;
        zero-success variants get _empty_stats() and a log warning.
    """
    # Lazy imports — only pay the swarmhost cost when remote=True.
    _strat_main = os.path.abspath(os.path.join(script_dir, "..", "..", "strat_main"))
    _repo_root = os.path.abspath(os.path.join(script_dir, "..", "..", ".."))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from overmind.swarmhost.experiment import (Experiment, HostPool, check_binary_freshness,
                                                  kill_experiment, select_remote_binary,
                                                  select_local_pktrade)
    from overmind.swarmhost.dispatch import request_stop_all
    from overmind.swarmhost.mktdata import stage_for_conf, discover_for_conf, s3_fetch_missing
    from util.sim import _empty_stats

    # Pick which local binary to ship to the (jammy) pool based on the
    # dispatcher's distro: jammy local → bin/pktrade (native); other local
    # (24.04 etc.) → jammy.bin/pktrade (cross-built for jammy).
    remote_bin = select_remote_binary()
    # Pick a native local binary for in-process `pktrade --dry-run` calls.
    local_bin = select_local_pktrade()
    sim_logger.info(f"binary to ship: {remote_bin}")
    sim_logger.info(f"local pktrade for --dry-run: {local_bin}")

    # Stale-binary check (advisory) for whichever one we're about to ship.
    stale = check_binary_freshness(remote_bin)
    if stale:
        sim_logger.warning(stale)

    # Tee everything sim_logger does to <workdir>/sweep.log so chunk
    # progress, failures, and timing survive past the terminal.
    _sweep_log_handler = _attach_sweep_log(sim_logger, workdir)

    # 1. Load host pool. Recovery needs the pool before we know which new
    # variants remain pending.
    if hosts_yaml is None:
        hosts_yaml = os.path.join(_repo_root, "overmind", "swarmhost", "hosts.yaml")
    pool = HostPool.from_yaml(hosts_yaml)
    sim_logger.info(f"pool from yaml: {len(pool.hosts)} hosts")
    # Pre-flight: drop unreachable hosts so a stale entry doesn't kill the
    # sweep with timeouts mid-run.
    pool = pool.reachable()
    if not pool.hosts:
        sim_logger.error("no reachable hosts in pool; aborting sweep")
        raise RuntimeError("no reachable hosts in pool")
    sim_logger.info(f"reachable: {len(pool.hosts)} hosts, {pool.total_cores()} cores total")

    # Remote mode is intentionally restartable by default: existing per-variant
    # caches are loaded without requiring --resume, and any submitted-but-
    # unscored remote chunks in the ledger are fetched/scored before new work
    # is dispatched.
    for i in variants_to_run:
        cache = os.path.join(scratch_dir, str(i), "sim_results.json")
        if os.path.exists(cache):
            with open(cache) as f:
                idx_to_results[i] = json.load(f)
            sim_logger.info(f"loaded cached results for variant {i}")

    recovered = _recover_remote_chunks(
        workdir, scratch_dir, results_file, idx_to_variant, idx_to_results,
        scoring_object, dates_to_sim, pool, remote_bin, sim_logger)
    if recovered:
        sim_logger.info(f"startup recovery scored {recovered} variant(s)")

    # Re-load caches after recovery in case a variant was scored but not yet
    # present in idx_to_results for any reason.
    for i in variants_to_run:
        cache = os.path.join(scratch_dir, str(i), "sim_results.json")
        if i not in idx_to_results and os.path.exists(cache):
            with open(cache) as f:
                idx_to_results[i] = json.load(f)

    pending = []
    for i in variants_to_run:
        if i not in idx_to_results:
            pending.append(i)
    if not pending:
        sim_logger.info("nothing to dispatch (all variants cached/recovered).")
        write_results_file(results_file, idx_to_variant, idx_to_results, scoring_object)
        _write_remote_state(workdir, {
            "state": "done",
            "n_variants_total": len(variants_to_run),
            "n_variants_scored": len(idx_to_results),
            "last_check": _utc_now_str(),
            "note": "all variants cached/recovered",
        })
        sim_logger.removeHandler(_sweep_log_handler)
        _sweep_log_handler.close()
        return

    sim_logger.info(f"remote dispatch: {len(pending)} variants × {len(dates_to_sim)} dates "
                    f"= {len(pending) * len(dates_to_sim)} jobs")

    # 2. Stage variant confs to a flat dir with unique names.
    #    Experiment uses conf basenames as keys, so they must be unique.
    stage_dir = os.path.join(workdir, ".exp_stage")
    os.makedirs(stage_dir, exist_ok=True)
    variant_to_confname = {}  # i -> "v_<i>.json"
    staged_paths = []
    for i in pending:
        src = os.path.join(scratch_dir, str(i), "pk.json")
        confname = f"v_{i}.json"
        dst = os.path.join(stage_dir, confname)
        shutil.copy2(src, dst)
        variant_to_confname[i] = confname
        staged_paths.append(dst)

    # 3. Stage mktdata once per host.
    # All variants share the base conf's symbols, so one discover call
    # against the first variant's conf is enough to know what to stage.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    discovery_conf = staged_paths[0]

    # Discover once (independent of host) so the missing-files prompt fires
    # one time per sweep, not once per host.
    disc = discover_for_conf(discovery_conf, list(dates_to_sim), local_bin)
    if disc["missing"]:
        sim_logger.info(f"{len(disc['missing'])} files not local — trying S3 fallback via md_exists")
        fetch = s3_fetch_missing(disc["missing"])
        if fetch["fetched"]:
            sim_logger.info(f"  S3 fetched {len(fetch['fetched'])}/{fetch['attempted']} missing files")
        if fetch["error"]:
            sim_logger.warning(f"  S3 fetch error: {fetch['error']}")
        # Re-discover so disc reflects the post-S3-fetch state.
        disc = discover_for_conf(discovery_conf, list(dates_to_sim), local_bin)

    if disc["missing"]:
        sim_logger.warning(f"{len(disc['missing'])} mktdata files still missing "
                           f"after S3 fallback (e.g. dropped from your live datalogger):")
        for m in disc["missing"][:10]:
            sim_logger.warning(f"    MISSING: {m}")
        if len(disc["missing"]) > 10:
            sim_logger.warning(f"    ... +{len(disc['missing']) - 10} more")
        # Detached runners have no stdin. They remain fail-closed unless the
        # caller explicitly opts in after reviewing the missing-file list.
        if allow_missing_data:
            sim_logger.warning(
                "proceeding with available mktdata due to "
                "--allow-missing-data; affected jobs may fail")
        elif not sys.stdin.isatty():
            raise RuntimeError(
                f"{len(disc['missing'])} mktdata files missing; aborting non-interactive sweep. "
                f"Run interactively to be prompted, or stage data first."
            )
        else:
            ans = input("  Proceed without them? Per-job sims for affected dates "
                        "will likely fail. [y/N]: ").strip().lower()
            if ans != "y":
                raise RuntimeError(f"aborted by user; {len(disc['missing'])} files missing")

    sim_logger.info(f"staging {len(disc['existing'])} files on {len(pool.hosts)} "
                    f"host(s) in parallel for {len(dates_to_sim)} dates")
    with ThreadPoolExecutor(max_workers=len(pool.hosts)) as ex:
        # allow_missing=True — the caller (us) has already inspected and confirmed.
        futures = {ex.submit(stage_for_conf, h.alias, discovery_conf,
                             list(dates_to_sim), local_bin,
                             allow_missing=True): h.alias
                   for h in pool.hosts}
        for fut in as_completed(futures):
            alias = futures[fut]
            try:
                n = fut.result()
                sim_logger.info(f"  {alias}: {n} files (rsync skips already-synced)")
            except Exception as e:
                # One host failing shouldn't kill the sweep — log it and
                # continue. If the dispatcher later sends jobs to this host,
                # they'll fail and surface in the per-run manifest.
                sim_logger.error(f"  {alias}: mktdata stage FAILED: {e}")

    # 4. Create experiment + push once.
    # Include the workdir's parent in the slug so different per-symbol sweeps
    # in a coverage_autosearch-style layout (results/<sym>/<spec>/) don't
    # collide on a non-unique terminal component (e.g. all syms ending in
    # ".../spec_round_a/" would otherwise produce the same experiment_id).
    abs_wd = os.path.abspath(workdir)
    parent_basename = os.path.basename(os.path.dirname(abs_wd))
    own_basename = os.path.basename(abs_wd)
    if parent_basename and parent_basename not in (".", "/"):
        workdir_slug = f"{parent_basename}-{own_basename}".replace("/", "-")
    else:
        workdir_slug = own_basename.replace("/", "-")
    #
    # CONTAMINATION GUARD: experiment_id is {date}_{host}_{slug}_{binhash}
    # (see swarmhost/experiment.py). The remote result cache is keyed
    # (experiment_id, variant_id) with NO symbol in the key, and variant IDs are
    # deterministic (random.seed(42) + same --random-sample). So if two sweeps
    # share an experiment_id, concurrently-running variants overwrite/cross-serve
    # each other's sims. The id previously depended only on the workdir basename,
    # which was identical ("sv") across symbols -> different symbols silently got
    # each other's results. Fix: fold a fingerprint of WHAT is being simmed (the
    # staged variant confs) plus the traded symbol into the name, so distinct
    # sims => distinct experiment_id => no collision; identical sims => same id
    # (then cache reuse is correct, e.g. --resume). Hash the staged confs:
    import hashlib as _hashlib
    _h = _hashlib.sha256()
    for _p in sorted(staged_paths):
        with open(_p, "rb") as _f:
            _h.update(_f.read())
    content_tag = _h.hexdigest()[:8]
    # Surface the traded symbol too (human-readable remote experiment dirs).
    try:
        with open(staged_paths[0]) as _f:
            _traded = json.load(_f)["pktraders"][0].get("traded_symbol", "")
        sym_slug = _traded.replace(":", "-").replace("/", "-")
    except Exception:
        sym_slug = ""
    workdir_slug = os.path.basename(os.path.abspath(workdir)).replace("/", "-")
    name_parts = ["simvar", workdir_slug] + ([sym_slug] if sym_slug else []) + [content_tag]
    exp = Experiment.create(name="-".join(name_parts),
                            binary_path=remote_bin, confs=staged_paths)
    sim_logger.info(f"experiment: {exp.experiment_id}")
    exp.push(pool)

    # 4b. Emit a local-rerun script. Lets the user re-run any variant locally
    #     to inspect trades/logs that aren't fetched back from remote.
    _emit_local_repro_script(workdir, dates_to_sim)

    # 5. Loop chunks: dispatch, reshape outputs, score, cache, rewrite results.
    # Include hostname in run_id so two users on different machines running
    # concurrent sweeps don't collide on `chunk_0000`.
    import socket as _socket
    started = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    local_host = _socket.gethostname().split(".")[0]
    dispatcher_pid = os.getpid()
    total_jobs = 0
    failures_all = []  # list of (conf, date, host, returncode) across all chunks

    _write_remote_state(workdir, {
        "state": "dispatching",
        "pid": dispatcher_pid,
        "host": local_host,
        "experiment_id": exp.experiment_id,
        "n_variants_total": len(pending),
        "n_variants_scored": len(idx_to_results),
        "chunks_total": (len(pending) + chunk_size - 1) // chunk_size,
        "last_check": _utc_now_str(),
    })

    def _make_chunk_progress_cb(run_id, chunk_idx, chunk):
        last_check = {"dt": None}

        def _progress_cb(progress):
            now = _utc_now()
            if last_check["dt"] is not None:
                gap_s = (now - last_check["dt"]).total_seconds()
                if gap_s > 180:
                    sim_logger.warning(
                        f"dispatcher resumed after {gap_s:.0f}s without heartbeat; "
                        f"reconciling remote chunk {run_id}")
            last_check["dt"] = now

            event = {
                "status": "heartbeat",
                "experiment_id": exp.experiment_id,
                "run_id": run_id,
                "chunk_idx": chunk_idx,
                "completed_jobs": progress.get("completed_jobs", 0),
                "failed_jobs": progress.get("failed_jobs", 0),
                "queued_jobs": progress.get("queued_jobs", 0),
                "running_jobs": progress.get("running_jobs", 0),
                "total_jobs": progress.get("total_jobs", 0),
            }
            _append_remote_chunk_event(workdir, event)
            _write_remote_state(workdir, {
                "state": "running_chunk",
                "pid": dispatcher_pid,
                "host": local_host,
                "experiment_id": exp.experiment_id,
                "run_id": run_id,
                "chunk_idx": chunk_idx,
                "variants_in_chunk": len(chunk),
                "n_variants_total": len(pending),
                "n_variants_scored": len(idx_to_results),
                "chunks_done": chunk_idx,
                "chunks_total": (len(pending) + chunk_size - 1) // chunk_size,
                "last_check": _utc_now_str(),
                "remote_progress": {
                    "completed_jobs": progress.get("completed_jobs", 0),
                    "failed_jobs": progress.get("failed_jobs", 0),
                    "queued_jobs": progress.get("queued_jobs", 0),
                    "running_jobs": progress.get("running_jobs", 0),
                    "total_jobs": progress.get("total_jobs", 0),
                },
            })

        return _progress_cb

    # SIGINT handler: on Ctrl+C, kill all remote pktrade procs for this
    # experiment before exiting. Otherwise the dispatcher dies but the
    # remote procs keep burning cores until the user runs kill.py manually.
    import signal as _signal
    active_run_id = None
    cancelling = {"active": False}
    def _sigint_handler(signum, frame):
        if cancelling["active"]:
            return
        cancelling["active"] = True
        _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
        sim_logger.warning(f"SIGINT — killing remote pktrade procs for {exp.experiment_id}")
        try:
            request_stop_all()
            _write_remote_state(workdir, {
                "state": "cancelling",
                "pid": dispatcher_pid,
                "host": local_host,
                "experiment_id": exp.experiment_id,
                "run_id": active_run_id,
                "last_check": _utc_now_str(),
            })
            if active_run_id is not None:
                _append_remote_chunk_event(workdir, {
                    "status": "cancelled",
                    "experiment_id": exp.experiment_id,
                    "run_id": active_run_id,
                })
            killed = 0
            for attempt in range(2):
                killed += kill_experiment(exp.experiment_id, pool, verbose=False)
                if attempt == 0:
                    time.sleep(1)
            sim_logger.warning(f"killed {killed} proc(s) across pool")
            _write_remote_state(workdir, {
                "state": "cancelled",
                "pid": dispatcher_pid,
                "host": local_host,
                "experiment_id": exp.experiment_id,
                "run_id": active_run_id,
                "remote_processes_killed": killed,
                "last_check": _utc_now_str(),
            })
        except Exception as e:
            sim_logger.error(f"kill on Ctrl+C failed: {e}")
        # Re-raise as KeyboardInterrupt so Python's normal exit path runs.
        raise KeyboardInterrupt()
    _prev_sigint = _signal.signal(_signal.SIGINT, _sigint_handler)

    for chunk_idx in range(0, len(pending), chunk_size):
        chunk = pending[chunk_idx:chunk_idx + chunk_size]
        run_id = f"chunk_{chunk_idx // chunk_size:04d}_{local_host}"
        jobs = [(variant_to_confname[i], d) for i in chunk for d in dates_to_sim]
        sim_logger.info(f"chunk {run_id}: {len(chunk)} variants, {len(jobs)} jobs")
        _append_remote_chunk_event(workdir, {
            "status": "submitted",
            "experiment_id": exp.experiment_id,
            "run_id": run_id,
            "chunk_idx": chunk_idx // chunk_size,
            "variants": chunk,
            "dates": list(dates_to_sim),
            "n_jobs": len(jobs),
        })
        active_run_id = run_id
        result = exp.run(pool, jobs, run_id=run_id, fetch=True,
                         progress_cb=_make_chunk_progress_cb(
                             run_id, chunk_idx // chunk_size, chunk),
                         progress_interval_s=60.0)
        active_run_id = None
        total_jobs += len(jobs)
        _write_remote_state(workdir, {
            "state": "fetching_chunk",
            "pid": dispatcher_pid,
            "host": local_host,
            "experiment_id": exp.experiment_id,
            "run_id": run_id,
            "chunk_idx": chunk_idx // chunk_size,
            "variants_in_chunk": len(chunk),
            "last_check": _utc_now_str(),
        })

        # Log failures (per the partial-success policy, we still score what landed).
        if result.failures:
            sim_logger.warning(f"chunk {run_id}: {len(result.failures)} jobs failed")
            for jr in result.failures[:5]:
                sim_logger.warning(f"  failed: conf={jr.job.conf} date={jr.job.date} rc={jr.returncode}")
            for jr in result.failures:
                failures_all.append((jr.job.conf, jr.job.date, jr.host, jr.returncode))

        _score_fetched_remote_chunk(result.fetched_to, chunk, dates_to_sim,
                                    scratch_dir, idx_to_results, sim_logger)
        _append_remote_chunk_event(workdir, {
            "status": "scored",
            "experiment_id": exp.experiment_id,
            "run_id": run_id,
            "chunk_idx": chunk_idx // chunk_size,
            "variants": chunk,
            "n_variants_scored": len(chunk),
        })

        # Mid-sweep visibility: rewrite results.txt with everything accumulated.
        write_results_file(results_file, idx_to_variant, idx_to_results, scoring_object)
        sim_logger.info(f"chunk {run_id} done; results.txt now has {len(idx_to_results)} variants scored")
        _write_remote_state(workdir, {
            "state": "scored_chunk",
            "pid": dispatcher_pid,
            "host": local_host,
            "experiment_id": exp.experiment_id,
            "run_id": run_id,
            "chunk_idx": chunk_idx // chunk_size,
            "n_variants_total": len(pending),
            "n_variants_scored": len(idx_to_results),
            "chunks_done": chunk_idx // chunk_size + 1,
            "chunks_total": (len(pending) + chunk_size - 1) // chunk_size,
            "last_check": _utc_now_str(),
        })

        # Progress file for `watch -n 2 cat <workdir>/progress.json`.
        # Atomic write (write tmp + rename) so a polling reader never sees
        # a half-written file.
        progress = {
            "experiment_id": exp.experiment_id,
            "started": started,
            "last_update": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_variants_total": len(pending),
            "n_variants_scored": len(idx_to_results),
            "n_jobs_total": len(pending) * len(dates_to_sim),
            "n_jobs_completed": total_jobs,
            "n_jobs_failed": len(failures_all),
            "chunks_done": chunk_idx // chunk_size + 1,
            "chunks_total": (len(pending) + chunk_size - 1) // chunk_size,
            "last_chunk": run_id,
            "hosts": [h.alias for h in pool.hosts],
        }
        prog_path = os.path.join(workdir, "progress.json")
        with open(prog_path + ".tmp", "w") as f:
            json.dump(progress, f, indent=2)
        os.replace(prog_path + ".tmp", prog_path)

    # Final write with full metadata header — sweep id, hosts, binary,
    # start/end times, failure count. Helps when you come back to old
    # results.txt files months later.
    completed = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata = {
        "experiment_id": exp.experiment_id,
        "binary": str(exp.binary_path),
        "binhash": exp.binhash,
        "dates": ",".join(dates_to_sim),
        "n_variants": len(pending),
        "hosts": " ".join(h.alias for h in pool.hosts),
        "started": started,
        "completed": completed,
        "total_jobs": total_jobs,
        "failures": len(failures_all),
    }
    write_results_file(results_file, idx_to_variant, idx_to_results, scoring_object,
                       metadata=metadata)
    _write_remote_state(workdir, {
        "state": "done",
        "pid": dispatcher_pid,
        "host": local_host,
        "experiment_id": exp.experiment_id,
        "n_variants_total": len(pending),
        "n_variants_scored": len(idx_to_results),
        "chunks_done": (len(pending) + chunk_size - 1) // chunk_size,
        "chunks_total": (len(pending) + chunk_size - 1) // chunk_size,
        "n_jobs_total": len(pending) * len(dates_to_sim),
        "n_jobs_failed": len(failures_all),
        "last_check": _utc_now_str(),
    })

    # End-of-sweep failure summary — visible, not buried in chunk logs.
    print()
    if failures_all:
        print(f"sweep done with {len(failures_all)} / {total_jobs} job failures:")
        # Show first 10 inline; full set is in each chunk's manifest.
        for conf, date, host, rc in failures_all[:10]:
            print(f"  {host:>10s}  {conf}  date={date}  rc={rc}")
        if len(failures_all) > 10:
            print(f"  ... +{len(failures_all) - 10} more")
        print(f"  see chunk manifests: ~/scratch/remote-sims/{exp.experiment_id}/<chunk>/manifest.json")
    else:
        print(f"sweep done; all {total_jobs} jobs succeeded.")

    # Detach the per-workdir file handler so the logger doesn't accumulate
    # handlers across multiple sim_grid calls in one process.
    sim_logger.removeHandler(_sweep_log_handler)
    _sweep_log_handler.close()
    # Restore the previous SIGINT handler.
    _signal.signal(_signal.SIGINT, _prev_sigint)


def main():
    parser = argparse.ArgumentParser()
    # If not given, will auto-generate sv_0, sv_1, etc.
    parser.add_argument("--workdir", type=str, required=False, default=None)

    # The spec that tells us all the mappings.
    parser.add_argument("--conf", required=True)

    # The pktrade conf we base ourselves off.
    # If given, selects out only the .pk dict containing this sym. Otherwise,
    # selects out the first thing.
    parser.add_argument("--sym", type=str)
    parser.add_argument("--pk", type=str, required=True)

    # Let's spec out dates too? Not inclusive.
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    # Blacklist file
    parser.add_argument("--blacklist", type=str)
    parser.add_argument("--full-output", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Skip variants that already have cached results")
    parser.add_argument("--random-sample", type=int, default=0,
                        help="Randomly sample N variants from the full grid (0 = run all)")
    parser.add_argument("--extra-args", dest="extra_args", type=str, default="",
                        help="Extra args appended verbatim to each pktrade invocation "
                             "(e.g. '--slowdown-market TopBookCme --slowdown-market-offset=-85')")

    # ── Remote dispatch (swarmhost) ───────────────────────────────────────
    parser.add_argument("--remote", action="store_true",
                        help="Dispatch sims to a Hetzner swarmhost pool instead of running locally.")
    parser.add_argument("--hosts", type=str, default=None,
                        help="Path to swarmhost hosts.yaml (default: overmind/swarmhost/hosts.yaml).")
    parser.add_argument("--chunk-size", type=int, default=50,
                        help="Variants per dispatch chunk (remote only). Smaller = more frequent "
                             "mid-sweep results.txt updates. Default 50.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would dispatch (N variants × M dates, hosts, etc.) and exit "
                        "without running any sims. Sanity check before kicking off a big sweep.")
    parser.add_argument("--allow-missing-data", action="store_true",
                        help="In remote mode, stage available mktdata and continue after the "
                        "missing-file warning. Default is to abort non-interactive runs.")

    args = parser.parse_args()

    with open(args.conf, "r") as f:
        vars_dct = json.loads(f.read())

    with open(args.pk, "r") as f:
        pk_dct = json.loads(f.read())

    # Goes through and potentially selects out a new pk_dct.
    if args.sym:
        pk_dct["pktraders"] = [
            one_pk_section
            for one_pk_section in pk_dct["pktraders"]
            if one_pk_section["traded_symbol"] == args.sym
        ]

    # Auto-generate sv_n directory if workdir not specified
    if args.workdir is None:
        n = 0
        while os.path.exists(f"sv_{n}"):
            n += 1
        workdir = f"sv_{n}"
    else:
        workdir = args.workdir

    # Create workdir and copy source files into it
    if not os.path.exists(workdir):
        os.mkdir(workdir)
    shutil.copy(args.conf, os.path.join(workdir, "vars.json"))
    shutil.copy(args.pk, os.path.join(workdir, "pk_source.json"))
    print(f"Output directory: {workdir}")

    blacklist_dates = []
    if args.blacklist:
        with open(args.blacklist, "r") as f:
            blacklist_dates = f.read().split("\n")
    blacklist_dates = [a_date for a_date in blacklist_dates if a_date]


    # Read in the file to get sym and mkt.

    sym = pk_dct["pktraders"][0]["traded_symbol"]
    mkt = pk_dct["pktraders"][0]["ordex"]["markets"][0]

    dates = dates_avail(args.start, args.end, sym, mkt, dates_method="WEEKDAYS")[
        "good_dates"
    ]
    dates = blacklist(dates, blacklist_dates)

    print("Running on dates {}".format(dates))

    # No signal caching right now. 
    fast_sim = False

    if len(dates) == 0:
        sys.exit("No dates to run on!")
    sim_grid(vars_dct, pk_dct, workdir, dates, fast_sim=fast_sim,
             full_output=args.full_output, resume=args.resume,
             random_sample=args.random_sample,
             remote=args.remote, hosts_yaml=args.hosts, chunk_size=args.chunk_size,
             dry_run=args.dry_run, extra_args=args.extra_args,
             allow_missing_data=args.allow_missing_data)


if __name__ == "__main__":
    main()
