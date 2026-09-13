# Trade-machine disk retention — agent runbook

**Purpose:** reclaim disk on the `gf` trade boxes by archiving old logs to S3 and deleting
the local copies, safely, without disturbing live trading. This encodes the manual procedure
run on 2026-07-20 (which took gf2 from 97% → 56%, and freed ~59 GB across four boxes).

**How to invoke:** open a Claude session *on this machine* (david's laptop/workstation — it
has the SSH keys in `~/aws_pairs/` and host aliases in `~/.ssh/config`) and say:

> "Run the trade-machine disk retention runbook."

A cloud/headless agent CANNOT do this — it has no SSH reach to the boxes. Local session only.

**Autonomy contract (do not violate):**
- The agent runs phases 1–5 (assess → copy → verify → upload → build delete list) autonomously.
- The agent then **STOPS and waits for human approval** before phase 6 (deletion). Always.
- The agent NEVER deletes anything that is not first byte-verified in the local backup AND
  confirmed uploaded to S3.

---

## Fixed facts

- **Hosts:** `gf0 gf1 gf2 gf3` (aliases in `~/.ssh/config`, user `ubuntu`, region ap-northeast-1).
- **Data root on each box:** `~/estrader/`.
- **Local backup dir:** `~/scratch/trademachine_backup/<host>/` (mirrors the estrader tree).
- **S3 archive:** `s3://pktrade-capture/trademachine-archive/<host>/` (Tokyo; versioning suspended
  but current objects never auto-expire — safe. Local creds are IAM user `dkamm`).
- **Retention window:** delete only files whose filename date is **older than 30 days** from the
  run date. Compute `CUTOFF=$(date -d '30 days ago' +%Y%m%d)` at run time — never hardcode.
  (Uses a fixed 30 days rather than `1 month ago`, so the window doesn't shift with month length.)
  Measured cost of 30-day vs 7-day retention: an extra 2.2–5.3 GB resident per box, against
  21–30 GB free as of 2026-07-21 — comfortable, and it shrinks once the log-volume reductions
  in `rel_wide_mm_2.cc` / `fixed_point.h` are deployed.
- **What is safe to delete (Tier 1):** `pktrade*` / `pkmultifeed*` `.log.INFO.*` files only.
  These are engine/feed chatter. This is the default and usually all that's needed.
- **What is NOT swept by default** (require separate, explicit approval):
  `logs_*.csv` (Tier 2), `orders_*.csv` / `trades_*.csv` (Tier 3 — audit trail).
- **Live-read constraint (why the window is safe):** live processes read `orders_*.csv`
  only for the **current and previous day** (a separate process copies those), so a 30-day
  window leaves ~4 weeks of margin over anything read live. INFO logs are never read back.
- **Never delete binaries:** compiled executables (`pkmultifeed*`, `pktrade*`, ~100 MB each) sit
  in the same dirs as the logs and differ only by filename suffix. Exclude them by the
  **executable bit**, never by name glob.

---

## Phase 1 — Assess (read-only)

```bash
# Current root-fs usage on every box. Flags which are tight.
for h in gf0 gf1 gf2 gf3; do
  printf '%-5s ' "$h"
  ssh -o ConnectTimeout=8 -o BatchMode=yes "$h" \
    'df -h --output=size,used,avail,pcent / | tail -1'
done
```

Report the table. If nothing is above ~70%, tell the user and ask whether to proceed anyway.

## Phase 2 — Copy each box's estrader tree local (excluding binaries)

```bash
CUTOFF=$(date -d '30 days ago' +%Y%m%d)         # e.g. 20260621 when run on 2026-07-21
D=~/scratch/trademachine_backup
SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=30"

for h in gf0 gf1 gf2 gf3; do
  mkdir -p "$D/$h"
  # Manifest = every regular file EXCEPT executables / shared libs / .bak / __pycache__.
  # -perm -u+x filter is what keeps the compiled binaries out of the copy.
  ssh $SSH_OPTS "$h" 'cd ~/estrader && ionice -c3 nice -n19 find . -type f \
      ! -perm -u+x ! -name "*.so*" ! -name "*.bak" ! -path "*/__pycache__/*" \
      -printf "%P\n" | sort' > "$D/$h.manifest"
  # -az compresses text logs on the wire; --rsync-path nices the REMOTE side so the
  # copy stays off the trading process's CPU/IO path. Resumable if interrupted.
  rsync -az --files-from="$D/$h.manifest" \
    --rsync-path='ionice -c3 nice -n19 rsync' \
    -e "ssh $SSH_OPTS" "$h:estrader/" "$D/$h/"
done
```

Run sequentially (parallel pulls contend for local ingest bandwidth; the observed rate is
~5 GB/min so sequential total is ~20 min for all four). For a slow link, background it and
watch the landing dir size.

## Phase 3 — Verify the copy is byte-identical

```bash
# For each host: build path<TAB>size on both sides, byte-sorted, and diff.
# LC_ALL=C is REQUIRED — a locale sort makes `comm` silently wrong.
for h in gf0 gf1 gf2 gf3; do
  ssh $SSH_OPTS "$h" 'cd ~/estrader && ionice -c3 nice -n19 find . -type f \
     ! -perm -u+x ! -name "*.so*" ! -name "*.bak" ! -path "*/__pycache__/*" \
     -printf "%P\t%s\n"' | LC_ALL=C sort > "$D/$h.r.tsv"
  (cd "$D/$h" && find . -type f -printf "%P\t%s\n") | LC_ALL=C sort > "$D/$h.l.tsv"
  echo "$h mismatched/missing: $(LC_ALL=C comm -23 "$D/$h.r.tsv" "$D/$h.l.tsv" | grep -c .)"
done
```

Expect a handful of mismatches — they will all be *today's/yesterday's* live files still being
appended. **Verify none of them fall inside the delete window** (filename date < CUTOFF). If any
in-window file mismatches, do NOT proceed for that host; re-pull it.

## Phase 4 — Upload to S3, verify object counts

```bash
for h in gf0 gf1 gf2 gf3; do
  aws s3 sync "$D/$h/" "s3://pktrade-capture/trademachine-archive/$h/" --only-show-errors
  echo "$h  local=$(find "$D/$h" -type f | wc -l)  s3=$(aws s3 ls \
    "s3://pktrade-capture/trademachine-archive/$h/" --recursive | grep -c .)"
done
```

Upload is upstream-bandwidth-bound (~5–6 MB/s to Tokyo) — can take hours; it's resumable.
Object count must be ≥ local file count before any deletion.

## Phase 5 — Build the Tier-1 delete list (verified files only)

```bash
for h in gf0 gf1 gf2 gf3; do
  LC_ALL=C comm -12 "$D/$h.r.tsv" "$D/$h.l.tsv" > "$D/$h.verified.tsv"   # identical both sides
  awk -F'\t' -v cut="$CUTOFF" '{p=$1; n=split(p,a,"/"); f=a[n]
     if (f ~ /^(pktrade|pkmultifeed).*\.log\.INFO\./ && match(f,/[0-9]{8}/)) {
       d=substr(f,RSTART,8)+0; if (d < cut+0) print p"\t"$2 }}' \
     "$D/$h.verified.tsv" > "$D/$h.tier1.tsv"
  awk -F'\t' '{s+=$2;c++} END{printf "%s: %d files, %.2f GB\n", "'"$h"'", c, s/1073741824}' \
     "$D/$h.tier1.tsv"
done
```

Also write a human-readable `<host>_tier1_delete_list.txt` (size + path, one per line, with a
header stating the criteria) so the user can eyeball it.

## >>> STOP HERE <<<

Present, per host: file count, GB reclaimed, filename date range, projected `df` after, and the
path to the delete list. **Wait for explicit human approval.** Do not continue without it.

## Phase 6 — Delete (only after approval)

```bash
for h in gf0 gf1 gf2 gf3; do
  # Pre-check: every candidate still present at the exact verified size (guards against drift
  # between copy and delete). Abort this host if anything is missing or changed.
  cut -f1 "$D/$h.tier1.tsv" | ssh $SSH_OPTS "$h" 'cd ~/estrader && while IFS= read -r p; do
      [ -f "$p" ] && echo "$(stat -c%s "$p")	$p" || echo "MISSING	$p"; done' > "$D/$h.precheck"
  # (compare $D/$h.precheck against $D/$h.tier1.tsv; proceed only if 0 missing, 0 size drift)

  # Delete: feed the EXACT path list to the box. No remote glob — xargs removes only these paths.
  # -d "\n" handles odd filenames; -- guards against leading-dash names.
  cut -f1 "$D/$h.tier1.tsv" | ssh $SSH_OPTS "$h" 'cd ~/estrader && xargs -d "\n" rm -f --'

  ssh $SSH_OPTS "$h" 'df -h --output=used,avail,pcent / | tail -1'   # confirm reclaim
done
```

## Phase 7 — Post-delete health check

```bash
for h in gf0 gf1 gf2 gf3; do
  ssh $SSH_OPTS "$h" 'echo "'"$h"' procs=$(pgrep -c -f "pktrade|pkmultifeed") \
    livewrites=$(find ~/estrader -maxdepth 3 -type f -newermt "-15 minutes" | wc -l)"'
done
```

Trade processes must still be running and files still being written. Report the final
before/after disk table.

## Phase 8 — Compress aged CSVs in place (>30 days)

The delete phases only remove INFO logs. `orders_*.csv` / `logs_*.csv` / `trades_*.csv` are the
audit trail and are **never deleted**, so they grow without bound (44.8 GB across the fleet as of
2026-07-21). Compressing them is the lever for that half, and unlike deletion it is **reversible**
(`gunzip`). Measured ratio on a real orders CSV: **7.1x (112 MB -> 15.8 MB, 86% saved)**.
At a 30-day horizon this reclaims roughly **24 GB** fleet-wide.

**Why this is safe despite many readers.** Plenty of tools (`order_analysis.py`,
`trade_manager.py`, `sim_eval*.py`, `pta.py`, `fleet_review.py`, …) build literal paths like
`f"orders_{ds}.csv"` and would skip a gzipped file silently. They don't hit that, because the
strategy CSVs are **scp'd to David's desktop at the end of each trading day** and those local
copies stay uncompressed — the desktop mirror, not the box, is what historical analysis reads.
Compression here only touches on-box files older than 30 days, long after they've been copied off.

Two narrow caveats, neither a blocker:
- **Re-fetching an old date directly from a box** goes through a remote `find -name
  "orders_<date>.csv"` (`trade_manager.py:132-152`, `sim_eval.py:213-236`) which will not match
  a `.gz`. Use the desktop copy, or `gunzip` that file on the box first.
- The C++ tools (`markout.cc`, `fillstats.cc`, `livemsgprint.cc`) use bare `ifstream` and fail
  **loudly** on a `.gz` if invoked by hand on the box. Same workaround.

```bash
CUTOFF=$(date -d '30 days ago' +%Y%m%d)

for h in gf0 gf1 gf2 gf3; do
  echo "=== $h ==="
  ssh $SSH_OPTS "$h" bash -s -- "$CUTOFF" <<'REMOTE'
CUT="$1"
# Select by FILENAME date, not mtime — mtime gets rewritten by copies/restores.
# Already-compressed files are skipped for free: they no longer match *.csv.
ionice -c3 nice -n19 find "$HOME/estrader" -type f \
    \( -name 'orders_*.csv' -o -name 'logs_*.csv' -o -name 'trades_*.csv' \) -printf '%f\t%p\n' \
  | awk -F'\t' -v c="$CUT" '{ if (match($1,/[0-9]{8}/)) { d=substr($1,RSTART,8)+0; if (d < c+0) print $2 } }' \
  > /tmp/gzip_targets.txt

n=$(wc -l < /tmp/gzip_targets.txt)
before=$(du -sm "$HOME/estrader" | cut -f1)
echo "  candidates: $n files"

# Compress and verify ONE file at a time. gzip deletes the original on success, so a bulk
# pass followed by a bulk check would destroy every original before the first bad archive
# is noticed. Aborting on the first failure bounds the blast radius to a single file.
# -6 is gzip's default level; measured ~7.1x on these CSVs, and it keeps CPU modest.
# ionice/nice keep it off the trading process's I/O and CPU path. gzip preserves mtime.
# The list is read on fd 3 so gzip can never consume it from stdin.
ok=0
while IFS= read -r f <&3; do
  # No -q: we want gzip's error text if it fails (pre-existing .gz, ENOSPC, permissions).
  ionice -c3 nice -n19 gzip -6 "$f" </dev/null \
    || { echo "  FATAL: gzip failed on $f (after $ok ok)" >&2; exit 1; }
  # gzip never re-reads what it wrote; -t is what catches a bad write. Quiet on success.
  gzip -t "$f.gz" \
    || { echo "  FATAL: corrupt archive $f.gz (after $ok ok)" >&2; exit 1; }
  ok=$((ok+1))
done 3< /tmp/gzip_targets.txt

after=$(du -sm "$HOME/estrader" | cut -f1)
echo "  compressed: $ok/$n | estrader ${before} MB -> ${after} MB"
rm -f /tmp/gzip_targets.txt
REMOTE
done
```

Notes:
- **Idempotent** — re-running skips anything already `.gz`, so it is safe to run every time.
- **A `FATAL` line means stop.** The loop aborts on the first gzip or integrity failure, so at
  most one original is unaccounted for (recover from `s3://pktrade-capture/trademachine-archive/`).
  Everything printed before it is verified good.
- **On abort, `/tmp/gzip_targets.txt` is deliberately left on the box** — it is the recovery
  manifest, and the `after $ok ok` count tells you how far down the list the run got.
- **`exit 1` only kills that host's remote shell**; the local `for h in gf0 gf1 gf2 gf3` loop
  moves on to the next box. Add `|| break` to the `ssh` line if a bad archive should halt the
  whole fleet run.
- **To reverse:** `find ~/estrader -name '*.csv.gz' -print0 | xargs -0 -r gunzip`.
- Safe to run independently of the delete phases — it touches a disjoint set of files.

## Phase 9 — Cleanup

Once the user confirms the S3 archive is trusted, the local `~/scratch/trademachine_backup/`
copies can be removed to reclaim local disk. Ask first — don't assume.

---

## Failure / edge notes
- **Stale SSH host key** (boxes reprovisioned): `ssh-keygen -R <hostname>` then retry.
- **Decommissioned hosts** timing out (e.g. old `bfx*`): skip, don't block the run.
- **`aws` not on the boxes:** confirmed — S3 must be driven from this local machine, never
  attempt `aws s3` over SSH on a box.
- **If disk hits 100% mid-run:** the fastest safe relief is deleting the *oldest* in-window
  INFO logs first (they're already the least consequential); do the copy afterward.
