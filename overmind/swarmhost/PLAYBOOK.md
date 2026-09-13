# swarmhost — playbook

> For CME/HIP3 RelWide retrains, use
> `overmind/playbooks/cme_retrains_playbook.md` as the canonical workflow.
> The commands below explain farm mechanics but do not define a current
> training window or fresh-versus-resume policy.

Task-oriented "I want to do X" reference. For concepts and architecture, see [README.md](README.md).

---

## 1. Run a SimVariations sweep on the farm

The main workflow.

```bash
./jammybuild.sh pktrade        # only if you changed pktrade C++
~/.venvs/v1/bin/python overmind/strat_main/stratbuilder/SimVariations.py \
    --workdir ~/scratch/sweeps/my-sweep \
    --conf    vars.json \
    --pk      base_pk.json \
    --start   20260501 --end 20260601 \
    --remote
```

Outputs in `~/scratch/sweeps/my-sweep/`:

- `results.txt` — pure-CSV ranked variants. Cat-and-sort works directly: `tail -n +2 results.txt | sort -t, -k5 -nr` ranks by sharpe (col 5). Rewritten after each chunk (mid-sweep peek). The `n_dates_ok` column shows how many of the requested dates actually produced output for that variant — filter `< N` to spot partial-success "winners" that aren't really comparable.
- `metadata.json` — sweep metadata (experiment_id, binhash, dates, hosts, start/end times, failure count). Separate from results.txt so the CSV stays clean. `jq < metadata.json` for a quick look.
- `scratch/<i>/sim_results.json` — per-variant cache (drives `--resume`).
- `scratch/<i>/acct_<i>_<date>.csv` — raw fetched acct rows per (variant, date).
- `repro.sh` — local-rerun helper (see §5).
- `sweep.log` — sim_logger output (chunk progress, failures, timing); survives past your terminal.
- `progress.json` — atomic per-chunk rewrite of sweep state. `watch -n 2 cat <workdir>/progress.json` for live progress.

At the end you'll see either `sweep done; all N jobs succeeded.` or a per-failure list with the manifest path for deeper inspection.

Useful flags:
- `--dry-run` — print what would dispatch (variants × dates × jobs) and exit. Sanity-check before a 2000-job sweep.
- `--resume` — skip variants whose `sim_results.json` already exists.
- `--chunk-size 25` — smaller = more frequent results.txt updates.
- `--random-sample 200` — pick a random subset of the grid.
- `--hosts <path>` — custom inventory file.

Unreachable hosts in `hosts.yaml` are auto-dropped at sweep start (a stale entry won't kill the run).

**Other sweep-style tools** also accept `--remote / --hosts / --chunk-size` and dispatch to the farm via the same path:

- `overmind/strat_main/tools/trademan/alpha_relwide_autosearch.py run-spec --remote ...`
- `overmind/strat_main/tools/trademan/coverage_autosearch.py --run-spec ... --remote ...`
- `pybin/gen_relcross.py --remote ...` (forwards to `SimVariations.py`)

Default is local, so existing workflows are unchanged — opt in with `--remote` when you want the farm.

## 2. Quick one-off sim

For ad-hoc debugging of a single conf:

```bash
./overmind/swarmhost/run.py \
    --name    dbg-armory \
    --binary  jammy.bin/pktrade \
    --conf    overmind/modelbuild_confs/xyz_armory.json \
    --dates   20260529 \
    --hosts   overmind/swarmhost/hosts.yaml
```

Outputs fetched to `~/scratch/remote-sims/<exp>/<run>/`. Use `--dates 20260101:20260131` for ranges.

Mktdata is auto-staged via `mktdata.stage_for_conf` (uses `pktrade --dry-run` to discover what's needed). Pass `--no-stage-mktdata` to skip if you've staged manually or want to use already-cached data on the boxes.

## 3. Debug a failed sim

```bash
cat ~/scratch/remote-sims/<exp>/<run>/manifest.json
```

For any failure entry, take the `local_reproducer` cmdline and paste-and-run it locally — the paths are pre-rewritten to your local checkout.

If the failure was "creds file not found," see §9.

## 4. Kill a running sweep

**Most of the time you don't need this** — SimVariations and `run.py` both install a SIGINT handler, so Ctrl+C automatically kills the remote `pktrade` procs for the current experiment before exiting. Reach for `kill.py` only when:

- You closed the terminal without Ctrl+C (no chance for the handler to fire).
- You're killing a sweep someone *else* started.
- The auto-kill itself failed (network blip mid-shutdown).

```bash
# See what would be killed.
./overmind/swarmhost/kill.py 20260603_GoingMerry_my-sweep_a1b2c3d4 --dry-run

# For real (sends TERM, waits 2s, sends KILL to stragglers).
./overmind/swarmhost/kill.py 20260603_GoingMerry_my-sweep_a1b2c3d4
```

Get the `experiment_id` from the sweep's log (`experiment: ...` line) or from `~/scratch/remote-sims/<experiment_id>/`. The pattern match is scoped to `/opt/pktrade/experiments/<experiment_id>/` so it can't hit other users' processes. Output dirs are left in place — you may still want partial results.

## 5. Inspect a winning variant locally

After a sweep, you pick a variant and want to see its trades / logs that don't come back from remote (only acct + trades CSVs are fetched).

```bash
bash <workdir>/repro.sh                          # show usage + list available variants
bash <workdir>/repro.sh 42                       # rerun variant 42 across all sweep dates
bash <workdir>/repro.sh 42 20260529              # just one date
bash <workdir>/repro.sh 42 20260527 20260528     # subset
JOBS=10 bash <workdir>/repro.sh 42                # 10-way parallel (default 5)
```

Sims run in parallel up to `JOBS` at a time (default 5). Each date's stdout/stderr goes to its per-date dir as `stdout.log`, so live output doesn't interleave. Output lands under `<workdir>/scratch/<i>/local/<date>/` — `acct_<date>.csv`, `trades_<date>.csv`, `pos_<date>.json`, snapshot files. Kept separate from the remote-fetched `acct_<i>_<date>.csv` so you can diff them if you want.

Reproducibility caveat: results may differ from remote on session-boundary dates (Mondays) because of glibc version mismatch. See the note in `experiment.py`.

## 6. Add a new Hetzner box

Phase A (one-time, manual). Differs by Hetzner product:

**Hetzner Cloud** (`console.hetzner.cloud`) — what we currently use:
1. Project → **Security** → **SSH Keys** → add your `~/.ssh/id_ed25519.pub` once (key may already be there; check the MD5 fingerprint).
2. **Add Server** → CPX / dedicated. **Tick your SSH key in the order dialog** (don't accept a temp root password — that opens a brute-force window).
3. Choose Ubuntu 22.04 image. Hostname `hetzN`.
4. Once running, note the public IP. Add to `~/.ssh/config`:
   ```
   Host hetzN
       HostName <ip>
       User root
       IdentityFile ~/.ssh/id_ed25519
   ```

**Hetzner Robot** (`robot.hetzner.com`) — dedicated servers, monthly billing:
- Same idea: order with SSH key attached, choose Jammy in installimage, add SSH alias.

Phase B (scripted, same for both products):

```bash
./overmind/swarmhost/bootstrap.sh hetzN
$EDITOR overmind/swarmhost/hosts.yaml         # add: - alias: hetzN, cores: 16
./overmind/swarmhost/status.py                # confirm clean
```

Bootstrap is idempotent — re-run on an existing box to repair config drift (e.g. after a crash, a manual change, or a stub-creds list extension).

## 7. Recover a compromised host

**How you'll spot it:** `swarmhost/status.py` now auto-flags suspicious activity at the top of its output — known miner names, processes running from `/dev/shm` or `/tmp/.<hidden>`, or anomalous load with no `pktrade` procs. If you see a `!! SUSPICIOUS ACTIVITY DETECTED` banner, treat it as a compromise until proven otherwise. Hetzner IPs get brute-forced within minutes of going live; any window of password auth is risky.

**Don't try to clean it up — wipe.** An attacker with root could've planted backdoors anywhere on disk. Rebuild is the only safe path.

**Cloud rebuild flow** (what we used):

1. Drop the box from the pool immediately so dispatchers stop using it:
   ```bash
   $EDITOR overmind/swarmhost/hosts.yaml   # comment out the entry
   ```
2. Hetzner Cloud console → server → **Rebuild** tab → choose Ubuntu 22.04 → **tick your SSH key in the dialog** (so no password window). If Cloud forces a temp root password anyway:
3. Set a long random password when prompted on first SSH (it'll be locked in a minute):
   ```bash
   openssl rand -base64 24    # local; paste when SSH prompts
   ```
4. From local, clear the stale host key and push your pubkey:
   ```bash
   IP=$(ssh -G hetzN | awk '/^hostname / {print $2}')
   ssh-keygen -f ~/.ssh/known_hosts -R "$IP"
   ssh-copy-id hetzN          # uses the temp password
   ```
5. Bootstrap immediately — step 2 locks the root password:
   ```bash
   ./overmind/swarmhost/bootstrap.sh hetzN
   ```
6. Re-add to `hosts.yaml`. Verify with `status.py`.

**The stub-creds invariant is what makes this recoverable.** An attacker with root on a swarmhost gets you nothing of value because real exchange keys never live there. Never relax this rule.

## 8. Drop a host from the pool

Edit `overmind/swarmhost/hosts.yaml`, remove the entry. Done. Next dispatch uses the smaller pool. No on-box action needed.

## 9. Stub a new exchange (creds file not found)

When pktrade fails with `<Venue> creds file /root/.creds/.<Venue>.creds.json not found`:

1. Look at the local file's JSON keys (DON'T read values — use `jq 'keys'` or similar).
2. Add a heredoc-stub block to `bootstrap.sh` step 7, with zero/placeholder values for each key.
3. Rerun `./overmind/swarmhost/bootstrap.sh hetzN` on every host (idempotent).

Hard invariant: swarmhost must never hold real exchange keys. Stubs only.

---

## Less common but you'll hit them

- **Check pool health** → `./overmind/swarmhost/status.py` (table of load/mem/disk/pktrade-procs/exp-count per host, with auto-flagged compromise indicators). Add `--verbose` to list active experiment dirs, or `--watch 2` for a self-refreshing view while a sweep runs.
- **A new symbol/date isn't on a box** → SimVariations calls `mktdata.stage_for_conf` itself; for one-off sims via `run.py`, pre-stage manually:
  ```python
  from overmind.swarmhost.mktdata import stage_for_conf
  stage_for_conf("hetz0", Path("my.conf"), ["20260601"], Path("jammy.bin/pktrade"))
  ```
- **Check what a host is doing right now** → `ssh hetzN top` or `ssh hetzN 'ps -ef | grep pktrade'`.
- **Free up disk on a host** → cleanup cron runs daily; manual: `ssh hetzN ~/bin/mktdata_cleanup.sh --dry-run` and `experiments_cleanup.sh --dry-run` to see what would go.
- **Two people on the same pool** → safe (experiment_id and run_id both include hostname). Expect halved throughput per user.
