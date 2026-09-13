# swarmhost

Run `pktrade` sims on a pool of rented Hetzner boxes instead of locally.
Designed for sweeps (SimVariations, future PnlClimber) but works for
one-off sims too.

**Just want to do something?** See [PLAYBOOK.md](PLAYBOOK.md) — task-oriented
recipes for the common actions. This file is the conceptual reference.

## Why

Local sims serialized on one box. Sweeps over hundreds-to-thousands of
variants take hours. A small pool of cheap Hetzner dedicated boxes turns
that into minutes for the same money, with the local laptop free.

## Concepts

- **Host** — one Hetzner box reachable by SSH alias. N cores → N concurrent pktrade workers.
- **HostPool** — collection of Hosts, loaded from `hosts.yaml`.
- **Experiment** — a specific pktrade binary paired with a bundle of conffiles. Identified by `<YYYYMMDD>_<name>_<binhash8>`. Pushed once to each host; lives at `/opt/pktrade/experiments/<id>/`. Many can coexist.
- **Run** — one invocation of an Experiment over a list of `(conf, date)` jobs. Identified by UTC timestamp or caller label. Outputs land under `runs/<run_id>/<conf_stem>/<date>/`.

## Architecture (on each host)

```
/opt/pktrade/
  experiments/
    20260603_my-sweep_a1b2c3d4/
      bin/pktrade                       # jammy-built
      conf/                             # 1..N JSON confs
      manifest.json
      runs/
        <run_id>/<conf_stem>/<date>/
          acct_<date>.csv
          trades_<date>.csv
          pos_<date>.json
~/tardis_datasets/gzpbf/<market>/       # shared mktdata pool (LRU-cleaned)
~/.creds/                               # STUB exchange creds only
~/bin/                                  # md_exists.py + cleanup scripts
/etc/cron.d/pktrade-cleanup             # daily LRU cleanup
```

## Adding a host

**Phase A (manual, one-time per box):**
1. Lease the box on Hetzner.
2. At provisioning, attach your SSH key. (Or `ssh-copy-id` after install — see "Don't" below.)
3. installimage Ubuntu 22.04 Jammy.
4. Add an SSH config alias (e.g. `hetzN`) to `~/.ssh/config`.

**Phase B (scripted):**

```bash
./overmind/swarmhost/bootstrap.sh hetzN
```

Bootstrap is idempotent — safe to re-run anytime to repair drift. It:
- Hardens sshd (no password auth, root password locked) FIRST
- Sets TZ to America/New_York (matches local; avoids session-boundary divergence)
- apt-installs runtime deps + comfort tools (vim/emacs/tmux/mosh/...)
- Creates the venv + `/opt/pktrade/experiments/` + mktdata dir
- Pushes `md_exists.py`, AWS read-only creds (if you've provisioned them), stub exchange creds
- Installs cleanup crons + (optional) AWS creds
- Pushes your dotfiles for SSH comfort
- Self-tests

Then append the host to `hosts.yaml`:

```yaml
hosts:
  - alias: hetzN
    cores: 16
```

That's it. The dispatcher picks up the larger pool automatically.

**Don't** lease a box with a weak root password and leave it sitting on a
public IP — Hetzner IPs get brute-forced within minutes. Use SSH keys at
provisioning. We learned this the hard way.

## Hard invariants

These are deliberate. Don't loosen them.

1. **No real exchange keys on swarmhost.** Bootstrap installs zero-address
   stubs for `~/.creds/.Hyperliquid.creds.json` and `.Supabase.creds.json`.
   If pktrade complains about a missing venue stub, add it to bootstrap.sh
   step 7 — never copy real keys.
2. **SSH password auth off, root password locked.** Bootstrap step 2.
   Defense in depth: a config drift won't reopen the door.
3. **AWS creds on swarmhost are read-only IAM only.** A dedicated user, S3
   read-only policy. Sourced from `~/.aws/hetzner-readonly` on local; never
   from your real `~/.aws/credentials`.

## Usage

### One-off sim (CLI)

```bash
./overmind/swarmhost/run.py \
    --name my-test \
    --binary jammy.bin/pktrade \
    --conf overmind/modelbuild_confs/xyz.json \
    --dates 20260101:20260131 \
    --hosts overmind/swarmhost/hosts.yaml
```

### Python API

```python
from overmind.swarmhost import Experiment, HostPool
from overmind.swarmhost.mktdata import stage_for_conf

pool = HostPool.from_yaml("overmind/swarmhost/hosts.yaml")

# Stage mktdata once (uses pktrade --dry-run to discover what's needed).
for host in pool.hosts:
    stage_for_conf(host.alias, conf_path, dates, local_pktrade="jammy.bin/pktrade")

# One experiment, many runs.
exp = Experiment.create(name="my-sweep", binary_path="jammy.bin/pktrade",
                        confs=[Path("v_0.json"), Path("v_1.json"), ...])
exp.push(pool)  # idempotent rsync

jobs = [(conf_name, date) for conf_name in [...] for date in [...]]
result = exp.run(pool, jobs)  # dispatches, fetches, returns RunResult
```

### SimVariations sweep

```bash
python overmind/strat_main/stratbuilder/SimVariations.py \
    --conf vars.json \
    --pk <base-pk-conf>.json \
    --start 20260501 --end 20260601 \
    --workdir /tmp/my-sweep \
    --remote                        # dispatch to swarmhost pool
    --hosts overmind/swarmhost/hosts.yaml   # optional; defaults shown
    --chunk-size 50                 # variants per dispatch chunk
```

What you get:
- One Experiment for the whole sweep (binary + all variant confs).
- Single mktdata stage per host.
- Variants dispatched in chunks of `--chunk-size` (default 50). After each
  chunk: outputs reshaped into the local `<workdir>/scratch/<i>/` layout,
  per-variant `sim_results.json` cached, `results.txt` rewritten with
  everything accumulated so far.
- `--resume` works: cached variants are skipped.
- Partial success: a variant with ≥1 successful date is still scored;
  zero-success variants get empty stats + a log warning.

## Mktdata

Three ways to get gzpbf files onto a host. Pick whichever you have creds/data for.

| Helper | Source | When to use |
|---|---|---|
| `mktdata.stage_for_conf(alias, conf, dates, local_pktrade)` | local `~/tardis_datasets/gzpbf/` | Default. Uses `pktrade --dry-run` to auto-discover what the conf needs (sym × channel × date, plus prev-day). |
| `mktdata.stage_local(alias, paths)` | local, explicit file list | When you know exactly which files. |
| `mktdata.ensure(alias, md_exists_args)` | S3 bucket (via `md_exists.py` on remote) | Cleanest long-term, but needs read-only IAM creds at `~/.aws/credentials` on remote. **Not exercised yet.** |

All three preserve the per-market subdir layout (`Hyperliquid/`, `TopBookCme/`, ...).

## Maintenance

Two cron-installed scripts run daily on each box. Both use `atime` (not
`mtime`) so anything currently in use stays:

- `~/bin/mktdata_cleanup.sh` — removes gzpbf files with atime > 30d. If a
  later sim needs a pruned slice, `md_exists.py` re-pulls.
- `~/bin/experiments_cleanup.sh` — removes whole `experiments/<id>/` dirs
  whose newest file has atime > 30d. Active experiments survive because
  every `.run()` touches files inside.

Both support `--dry-run`.

## Failure handling

Per-run manifest at `<scratch>/remote-sims/<exp>/<run>/manifest.json`. For
each failed job:
- `host`, `conf`, `date`, `returncode`
- `remote_cmd` — the exact cmdline that ran
- `local_reproducer` — same cmdline with paths rewritten to your local
  checkout, so you can paste-and-run to debug.
- `stderr_tail` — last ~400 bytes of stderr.

No log fetching (glog files stay on remote — they're large and the manifest
usually has what you need).

## Reproducibility

- **Hetzner ↔ Hetzner**: bit-identical across all boxes in the pool.
  Confirmed in our smoke test.
- **Hetzner ↔ local-dev**: may diverge when local glibc differs from
  Hetzner's (Jammy = glibc 2.35). In a smoke test, 8/11 dates matched
  bit-perfectly; the 3 that diverged were all Mondays (session-boundary
  days where many events share timestamps and heap pop order becomes
  glibc-dependent).

For day-to-day work treat Hetzner as canonical. Don't trust exact-cent
comparisons between local and remote. See the long note at the top of
`experiment.py` for the full discussion.

## Open items

- **AWS read-only IAM user not provisioned** → can't test `mktdata.ensure`
  S3 path. Currently using `stage_for_conf` (local→remote rsync) instead.
  Works but local needs the data first.
- **PnlClimber integration not done.** Same pattern as SimVariations
  (one Experiment, chunked dispatch); just hasn't been wired yet.
- **More stub venues** as new confs need them — bootstrap currently
  ships Hyperliquid + Supabase only.
- **`jammybuild.sh` not TTY-safe** — needs manual `-it`→`-i` to run
  non-interactively.

## File map

```
overmind/swarmhost/
  README.md                    this file
  __init__.py                  exports Experiment, HostPool, RunResult
  bootstrap.sh                 Phase-B box setup; idempotent
  hosts.yaml                   inventory (alias + cores)
  experiment.py                Experiment / HostPool / RunResult
  dispatch.py                  per-host thread pool, round-robin assignment
  mktdata.py                   ensure / stage_local / stage_for_conf
  _ssh.py                      strict SSH + rsync wrappers
  run.py                       CLI wrapper
  mktdata_cleanup.sh           daily cron (atime > 30d)
  experiments_cleanup.sh       daily cron (atime > 30d)
```
