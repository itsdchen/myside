#! /usr/bin/env python

"""
To help me write sims and ish.

"""
import argparse

import os
import subprocess
import numpy as np
import pandas as pd
import sys
import traceback
import json

##################################
# For running stuff in directory and through pybin.
script_dir = os.path.dirname(os.path.realpath(__file__))
# Add the script directory to sys.path
sys.path.insert(0, script_dir)
x = os.path.join(script_dir, "..")
sys.path.append(x)
##################################

from util.chron import dates_list, dates_avail
from util.pathing import bindir
import subprocess
pk_bin = os.path.join(bindir(), "pktrade")


def sim_one_day(pk_bin, sim_dir, conf_path, date, acct_suffix="", trd_suffix="", print_cmd=False, full_output=False, logger=None, extra_args=""):
    if acct_suffix != "" and acct_suffix[0] != "_":
        acct_suffix = "_" + acct_suffix

    acct_file = "acct{}_{}.csv".format(acct_suffix, date)
    if not full_output:
        acct_file = os.path.join(sim_dir, acct_file)

    trd_file = "/dev/null"
    if trd_suffix != "":
        trd_file = "trd_{}_{}.csv".format(trd_suffix, date)
        trd_file = os.path.join(sim_dir, trd_file)


    # When not full_output, acct_file is already joined with sim_dir above.
    # When full_output, acct_file is just the basename — join it here.
    if full_output:
        full_acct_path = os.path.join(sim_dir, acct_file)
    else:
        full_acct_path = acct_file

    pkt_cmd = "{PK_BIN} --date {DATE} --conf {CONF} --acct {ACCT_OUT} --trd {TRD_FILE}"
    if full_output:
        pkt_cmd += " --out-dir {DIR} --full-output".format(DIR=sim_dir)

    pkt_cmd = pkt_cmd.format(
        PK_BIN=pk_bin, DATE=date, CONF=conf_path, ACCT_OUT=acct_file,TRD_FILE=trd_file
    )

    if extra_args:
        pkt_cmd += " " + extra_args.strip()

    # Just default log some of these outputs too.
    pkt_cmd += " > {}/out_{}.txt".format(sim_dir, date)


    # Mayybe print cmd.
    if print_cmd:
        if logger is None:
            print("sim: ")
            print(pkt_cmd)
        else:
            logger.info(pkt_cmd)

    sim_lines = subprocess.run(pkt_cmd, shell=True, capture_output=True, text=True)

    return full_acct_path


def sim_dates(pk_bin, sim_dir, conf_path, dates, acct_suffix="", trd_suffix="", print_cmd=False, full_output=False, logger=None, batch_size=24, remote=False, hosts_yaml=None, experiment_name=None, extra_args=""):
    # remote=True dispatches to Hetzner workers via overmind.swarmhost.Experiment
    # instead of running locally. Hand over to the dedicated helper so the
    # local-batch logic below stays untouched. (extra_args is local-only for
    # now — the swarmhost dispatcher's cmd builder doesn't thread it through;
    # add support there if you need it for remote sweeps.)
    if remote:
        if extra_args:
            msg = f"warn: extra_args={extra_args!r} ignored in remote mode"
            if logger is None:
                print(msg)
            else:
                logger.warning(msg)
        return _sim_dates_remote(
            pk_bin=pk_bin, sim_dir=sim_dir, conf_path=conf_path, dates=dates,
            acct_suffix=acct_suffix, hosts_yaml=hosts_yaml,
            experiment_name=experiment_name, logger=logger,
        )

    if acct_suffix != "" and acct_suffix[0] != "_":
        acct_suffix = "_" + acct_suffix

    sim_out_accts = []
    sim_cmds = []


    for one_date in dates:

        acct_file = "acct{}_{}.csv".format(acct_suffix, one_date)
        if not full_output:
            acct_file = os.path.join(sim_dir, acct_file)


        trd_file = "/dev/null"
        if trd_suffix != "":
            trd_file = "trd_{}_{}.csv".format(trd_suffix, one_date)
        if not full_output:
            trd_file = os.path.join(sim_dir, trd_file)

        if full_output:
            full_acct_path = os.path.join(sim_dir, acct_file)
        else:
            full_acct_path = acct_file

        one_pkt_cmd = "{PK_BIN} --date {DATE} --conf {CONF} --acct {ACCT_OUT}  --trd {TRD_FILE}"
        if full_output:
            one_pkt_cmd += " --out-dir {DIR} --full-output".format(DIR=sim_dir)


        # Just default log some of these outputs too.
        #one_pkt_cmd += " > {DIR}/out_{DATE}.txt".format(DIR=sim_dir, DATE=one_date)
        one_pkt_cmd += " > /dev/null"

        one_pkt_cmd = one_pkt_cmd.format(
            PK_BIN=pk_bin, DATE=one_date, CONF=conf_path, ACCT_OUT=acct_file, TRD_FILE=trd_file
        )

        if extra_args:
            # Inject before the redirect at the tail of one_pkt_cmd.
            redirect_idx = one_pkt_cmd.rfind(" > ")
            if redirect_idx >= 0:
                one_pkt_cmd = one_pkt_cmd[:redirect_idx] + " " + extra_args.strip() + one_pkt_cmd[redirect_idx:]
            else:
                one_pkt_cmd += " " + extra_args.strip()
        print(one_pkt_cmd)

        # Mayybe print cmd.
        if print_cmd and len(sim_cmds) == 0:
            if logger is None:
                print("sim: ")
                print(one_pkt_cmd)
            else:
                logger.info(one_pkt_cmd)
        # Add these lines.
        sim_cmds.append(one_pkt_cmd)
        sim_out_accts.append(full_acct_path)
    #return sim_out_accts

    # batch run these.
    if (len(sim_cmds) < batch_size):
        batch_size = len(sim_cmds)
    n_batches = (len(sim_cmds) // (batch_size)) + 1

    print("N_batches is {}".format(n_batches))
    for i in range(n_batches):
        print("Handling batch {}".format(i))
        cur_batch_procs = []
        cur_batch_cmds = sim_cmds[i*batch_size:(i+1)*batch_size]
        for one_cmd in cur_batch_cmds:
            p = subprocess.Popen(one_cmd, shell=True, text=True)
            cur_batch_procs.append(p)

        for one_proc in cur_batch_procs:
            print("Waiting ")
            one_proc.wait()

    return sim_out_accts


def _sim_dates_remote(pk_bin, sim_dir, conf_path, dates, acct_suffix, hosts_yaml, experiment_name, logger):
    """Remote path for sim_dates: dispatch to Hetzner via Experiment API.

    Roundtrips so the caller (and sim_date_range below) gets the same shape
    of return value as the local path — a list of paths to acct CSVs under
    sim_dir, named "acct{suffix}_{date}.csv".

    Args:
        pk_bin: path to the jammy-built pktrade (used as the experiment's binary).
        sim_dir: where to materialize fetched acct CSVs locally.
        conf_path: single conffile path.
        dates: list of YYYYMMDD strings.
        acct_suffix: matched to local-path naming (prepended with '_').
        hosts_yaml: path to a hosts.yaml inventory. Defaults to
            <repo>/overmind/swarmhost/hosts.yaml.
        experiment_name: optional label; defaults to slug of conffile basename.
        logger: optional logger to use for progress messages.

    Returns: list of acct CSV paths in sim_dir, matching the local-path shape.
    """
    # Lazy import — sim.py is imported widely and shouldn't pay for
    # overmind.swarmhost (and its yaml dep) unless remote=True is used.
    _repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from overmind.swarmhost.experiment import Experiment, HostPool

    if hosts_yaml is None:
        hosts_yaml = os.path.join(_repo_root, "overmind", "swarmhost", "hosts.yaml")
    if experiment_name is None:
        experiment_name = os.path.splitext(os.path.basename(conf_path))[0]

    if acct_suffix and acct_suffix[0] != "_":
        acct_suffix = "_" + acct_suffix

    pool = HostPool.from_yaml(hosts_yaml)
    msg = f"[sim_dates remote] experiment={experiment_name} dates={len(dates)} hosts={len(pool.hosts)}"
    print(msg) if logger is None else logger.info(msg)

    exp = Experiment.create(name=experiment_name, binary_path=pk_bin, confs=[conf_path])
    exp.push(pool)
    conf_basename = os.path.basename(conf_path)
    jobs = [(conf_basename, d) for d in dates]
    result = exp.run(pool, jobs, fetch=True)

    if result.failures:
        # Surface failures loudly so callers don't silently consume empty results.
        for jr in result.failures[:3]:
            print(f"  remote failure: {jr.host} {jr.job.conf} {jr.job.date} rc={jr.returncode}")
        print(f"  total failures: {len(result.failures)} (see {result.fetched_to}/manifest.json)")

    # Reshape into sim_dir/acct{suffix}_{date}.csv. Fetched layout is:
    #   result.fetched_to/<conf_stem>/<date>/acct_<date>.csv
    # We copy (not symlink) so subsequent local cleanup of result.fetched_to
    # doesn't yank out files the caller is still using.
    import shutil
    os.makedirs(sim_dir, exist_ok=True)
    conf_stem = os.path.splitext(conf_basename)[0]
    out_paths = []
    for d in dates:
        src = os.path.join(str(result.fetched_to), conf_stem, d, f"acct_{d}.csv")
        dst = os.path.join(sim_dir, f"acct{acct_suffix}_{d}.csv")
        if os.path.exists(src):
            shutil.copy2(src, dst)
            out_paths.append(dst)
        # If src doesn't exist (job failed or never ran), out_paths skips it.
        # sim_date_range tolerates missing files (try/except around pd.read_csv).
    return out_paths


def _empty_stats():
    """Zero-initialized stats dict — returned when a sim batch fails or has
    no successful days. Same keys as compute_acct_stats output."""
    return {
        "avg_wins_pnl": 0,
        "avg_pnl": 0,
        "avg_closed_pnl": 0,
        "avg_low_pnls": 0,
        "avg_comm": 0,
        "sharpe": 0,
        "avg_numtrds": 0,
        "avg_med_numtrds": 0,
        "med_numtrds": 0,
        "avg_flipped": 0,
        "pct_positive": 0,
        "avg_fillrate": 0,
        "n_dates_succeeded": 0,
    }


def compute_acct_stats(acct_files, dates_count=None):
    """Compute the per-variant stats dict from a list of acct CSV paths.

    Pulled out of sim_date_range so the remote-dispatch path (SimVariations
    via swarmhost) can compute identical stats without re-running pktrade
    locally. Tolerates missing/unreadable files (logs + skips).

    Args:
        acct_files: list of paths to acct_*.csv produced by pktrade.
        dates_count: original number of requested dates — controls the
            winsorization branch (which has a >1 guard). Defaults to the
            number of acct files that loaded successfully.

    Returns: stats dict (same keys as _empty_stats), or _empty_stats() if no
    files could be loaded.
    """
    pnls_lst = []
    for one_acct_file in acct_files:
        try:
            pnls_lst.append(pd.read_csv(one_acct_file))
        except Exception:
            print("Sim: Received an eror when reading {}".format(one_acct_file))
    if not pnls_lst:
        return _empty_stats()
    if dates_count is None:
        dates_count = len(pnls_lst)
    all_pnls = pd.concat(pnls_lst)
    stats = _compute_stats_from_pnls(all_pnls, dates_count)
    # Surface partial-success: how many of the requested dates actually
    # produced a usable acct file. Lets results.txt readers spot variants
    # that look like winners but only ran on 1-of-N dates.
    stats["n_dates_succeeded"] = len(pnls_lst)
    return stats


def _compute_stats_from_pnls(all_pnls, dates_count):
    """Internal: stats logic that used to live inline in sim_date_range.

    Kept verbatim (zero-day filter, winsorization, sharpe, etc.) so existing
    callers see identical numbers.
    """

    wanted_cols = [
        "net_pnl",
        "closed_pnl",
        "commission",
        "shs_traded",
        "shs_sent",
        "times_traded",
        "times_flipped"
    ]

    # Filter out zero-data days (no trades sent or filled).
    all_pnls = all_pnls[(all_pnls["shs_traded"] > 0) | (all_pnls["shs_sent"] > 0)]
    if all_pnls.empty:
        return _empty_stats()

    med_acct = all_pnls[wanted_cols].median()
    avg_acct = all_pnls[wanted_cols].mean()
    sum_acct = all_pnls[wanted_cols].sum()

    avg_pnl = avg_acct["net_pnl"]
    avg_closed_pnl = avg_acct["closed_pnl"]
    avg_comm = avg_acct["commission"]

    avg_fillrate = sum_acct["shs_traded"] / sum_acct["shs_sent"] if sum_acct["shs_sent"] > 0 else 0
    pct_positive = len(all_pnls[all_pnls["net_pnl"] > 0]) / len(all_pnls) if len(all_pnls) > 0 else 0

    # Can winsorize pnls to protect a bit against outliers.
    # This is ridiculous.
    if dates_count > 1:
        all_net_pnls = all_pnls["net_pnl"]

        # Winsorization can be specified here, just going to set it at 3 for now.
        # The sort_values(ascending=False) is sorted s.t. the 0-th value
        # is the highest, then the second highesst, etc.
        # So having this limit be "2" means we restrict to like, the third-highest
        # value. Hopefully this means we don't get affected so much.

        if len(all_net_pnls) > 4:
            limit_idx = min(2, len(all_net_pnls)-1)
        else:
            limit_idx = min(1, len(all_net_pnls)-1)

        upper_limit = all_net_pnls.sort_values(ascending=False).iloc[limit_idx]
        lower_limit =  all_net_pnls.sort_values(ascending=True).iloc[limit_idx]

        lowest_some_pnls = all_net_pnls.sort_values(ascending=True)[:int(len(all_net_pnls)*.80)]

        winsorized_pnls = all_net_pnls.clip(lower=lower_limit, upper=upper_limit)
        avg_wins = winsorized_pnls.mean()
        avg_low_pnls = lowest_some_pnls.mean()
        avg_wins_pnl = avg_wins
    else:
        avg_low_pnls = avg_pnl
        avg_wins_pnl = avg_pnl

    # Calculate volume_weighted pnl.
    trd_scaled_pnls = []
    for i, net_pnl in enumerate(all_pnls["net_pnl"]):
        day_volume = all_pnls["times_traded"].iloc[i]
        vol_factor = np.sqrt(day_volume)
        nu_pnl = 0 if vol_factor == 0 else net_pnl / vol_factor
        trd_scaled_pnls.append(nu_pnl)
    avg_trd_scaled_pnl = np.mean(trd_scaled_pnls)

    # Note: this is not annualized.
    sharpe = 0
    if all_pnls.shape[0] > 1:
        stddev_acct = all_pnls[wanted_cols].std()
        if stddev_acct["net_pnl"] != 0:
            sharpe = avg_pnl / stddev_acct["net_pnl"]

    avg_numtrds = avg_acct["times_traded"]
    avg_med_numtrds = 0.5 * (avg_acct["times_traded"] + med_acct["times_traded"])
    med_numtrds =  med_acct["times_traded"]
    pct_20_numtrds = np.percentile(all_pnls["times_traded"], 20, method="linear")
    avg_flipped = avg_acct["times_flipped"]

    return {
        "avg_trd_scaled_pnl": avg_trd_scaled_pnl,
        "avg_wins_pnl": avg_wins_pnl,
        "avg_pnl": avg_pnl,
        "avg_closed_pnl": avg_closed_pnl,
        "avg_low_pnls": avg_low_pnls,
        "avg_comm": avg_comm,
        "sharpe": sharpe,
        "avg_numtrds": avg_numtrds,
        "avg_med_numtrds": avg_med_numtrds,
        "med_numtrds": med_numtrds,
        "pct_20_numtrds": pct_20_numtrds,
        "avg_flipped": avg_flipped,
        "pct_positive": pct_positive,
        "avg_fillrate": avg_fillrate,
    }


def sim_date_range(pk_bin, sim_dir, conf_path, dates_list, acct_suffix="", trd_suffix="", full_output=False, print_cmd=False, logger=None, extra_args=""):
    """Run pktrade for `dates_list` and return per-variant stats."""
    try:
        acct_files = sim_dates(pk_bin, sim_dir, conf_path, dates_list,
                               acct_suffix, trd_suffix, print_cmd,
                               full_output=full_output, logger=logger,
                               extra_args=extra_args)
        stats = compute_acct_stats(acct_files, dates_count=len(dates_list))
        print(json.dumps(stats, indent=2))
        return stats
    except Exception as e:
        print("Got some error while simming: {}".format(e))
        traceback.print_exc()
        return _empty_stats()


def main():
    parser = argparse.ArgumentParser()
    # If not given, will use the current working directory.
    parser.add_argument("--workdir", type=str, required=False)

    parser.add_argument("--conf", required=True)

    # Let's spec out dates too? Not inclusive.
    parser.add_argument("--start", type=str, required=True)
    parser.add_argument("--end", type=str, required=True)
    parser.add_argument("--dates_method", type=str, default="WEEKDAYS")

    args = parser.parse_args()
    dates = dates_list(args.start, args.end, dates_method=args.dates_method)

    workdir = os.path.join(os.path.dirname(args.conf), "simoos")
    if args.workdir:
        workdir = args.workdir
    if not os.path.exists(workdir):
        os.mkdir(workdir)
        print("Workdir: {}".format(workdir))

    results = sim_date_range(pk_bin, workdir, args.conf, dates, acct_suffix="", trd_suffix="0", full_output=True, logger=None)

    print("Done")


if __name__ == "__main__":
    main()
