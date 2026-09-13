#! /usr/bin/env python

"""

Basic lib to run and continuously run batched processes.


"""
import os
import subprocess
import sys
import time
from datetime import datetime


def run_batches(cmds_lst, batch_size):
    print("Starting run_batch, {} cmds".format(len(cmds_lst)))

    cur_run_idx = 0

    cur_wait_idx = 0

    # Keep the proc + command so we can error if any child fails.
    procs = []

    while cur_wait_idx < len(cmds_lst) and cur_run_idx < len(cmds_lst):

        # We have space to run more things.
        while cur_run_idx  < cur_wait_idx + batch_size and cur_run_idx < len(cmds_lst):

#            print("{} : Right now, running {}, appending more".format(
#                datetime.now().time(),
#                cur_run_idx-cur_wait_idx))

            cmd = cmds_lst[cur_run_idx]
            new_proc = subprocess.Popen(cmd, shell=True)
            procs.append((new_proc, cmd))
            cur_run_idx += 1


        # Check the status of the procecsses and iterate.
        if cur_wait_idx < len(procs):
            proc, cmd = procs[cur_wait_idx]
            ret = proc.wait()
            if ret != 0:
                raise RuntimeError(
                    "run_batches: command failed (exit {}): {}".format(ret, cmd)
                )
            cur_wait_idx += 1

    # Wait for the rest of things to be done.
    while cur_wait_idx < len(procs):
        proc, cmd = procs[cur_wait_idx]
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(
                "run_batches: command failed (exit {}): {}".format(ret, cmd)
            )
        cur_wait_idx += 1
