#!/usr/bin/env bash
# mktdata_cleanup.sh
#
# Prune market data files in ~/tardis_datasets/gzpbf that haven't been
# touched (atime) in >30 days. If a sim later needs a pruned slice,
# md_exists.py re-downloads it from S3.
#
# Linux's default `relatime` mount option means atime updates whenever a
# file is read AND the existing atime is older than its mtime — so
# `-atime +30` accurately means "untouched in the last 30 days".
#
# Usage:
#   ./mktdata_cleanup.sh              # delete eligible files
#   ./mktdata_cleanup.sh --dry-run    # just list what would be deleted
#
# Installed by bootstrap.sh as a daily cron via /etc/cron.d/pktrade-cleanup.

set -euo pipefail

DATA_DIR="${HOME}/tardis_datasets/gzpbf"
AGE_DAYS=30

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

if [[ ! -d "$DATA_DIR" ]]; then
    echo "[mktdata_cleanup] No data dir at $DATA_DIR, nothing to do."
    exit 0
fi

if [[ $DRY_RUN -eq 1 ]]; then
    echo "[mktdata_cleanup] DRY RUN — files that would be deleted:"
    find "$DATA_DIR" -type f -atime "+${AGE_DAYS}" -print
else
    # -delete is depth-first, safe for our flat-ish layout.
    find "$DATA_DIR" -type f -atime "+${AGE_DAYS}" -print -delete
fi
