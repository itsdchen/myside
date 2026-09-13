#!/usr/bin/env bash
# experiments_cleanup.sh
#
# Remove whole experiment directories under /opt/pktrade/experiments/
# whose newest access (atime) is >30 days old. Each experiment dir is
# self-contained (binary + confs + runs), so removing the dir is the
# right unit.
#
# Active experiments are protected because any new .run() touches files
# inside the dir, resetting the atime clock.
#
# Usage:
#   sudo ./experiments_cleanup.sh              # delete eligible dirs
#   sudo ./experiments_cleanup.sh --dry-run    # just list what would go
#
# Installed by bootstrap.sh as a daily cron via /etc/cron.d/pktrade-cleanup.

set -euo pipefail

EXP_DIR="/opt/pktrade/experiments"
AGE_DAYS=30

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

if [[ ! -d "$EXP_DIR" ]]; then
    echo "[experiments_cleanup] No exp dir at $EXP_DIR, nothing to do."
    exit 0
fi

# -mindepth/-maxdepth 1 restricts to direct children (the experiment dirs).
# -atime +30 on a directory looks at the dir's own atime; we instead want
# "no file inside has been touched in 30 days". `find ... -newer` over the
# whole tree is the way: skip dirs that contain any recent file.
while IFS= read -r exp; do
    # Find any file inside this exp with atime in the last 30 days.
    recent=$(find "$exp" -type f -atime "-${AGE_DAYS}" -print -quit 2>/dev/null || true)
    if [[ -n "$recent" ]]; then
        continue
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[experiments_cleanup] DRY RUN — would delete: $exp"
    else
        echo "[experiments_cleanup] deleting: $exp"
        rm -rf "$exp"
    fi
done < <(find "$EXP_DIR" -mindepth 1 -maxdepth 1 -type d)
