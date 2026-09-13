#!/bin/bash
# OOM canary — bounded between-restart memory protection.
# Runs every 30 min via cron. Emails if hl-node VmRSS > threshold.
# Rate-limited: at most one alert per RATE_LIMIT_HOURS to avoid spam.
#
# Cron entry (every 30 min):
#   */30 * * * * /usr/local/bin/hl_oom_canary.sh
#
# Override threshold (default 95 GB = 99614720 kB):
#   THRESHOLD_KB=80000000 /usr/local/bin/hl_oom_canary.sh
#
# Override rate limit (default 2 hours):
#   RATE_LIMIT_HOURS=1 /usr/local/bin/hl_oom_canary.sh
set -u

HOST=$(hostname)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
NOW_TS=$(date +%s)
THRESHOLD_KB="${THRESHOLD_KB:-99614720}"
RATE_LIMIT_HOURS="${RATE_LIMIT_HOURS:-2}"
STATEDIR=/var/lib/hl_oom_canary
LOG_TAG="hl_oom_canary"

mkdir -p "$STATEDIR" 2>/dev/null || true

PID=$(pgrep -fo "hl-node --chain Mainnet")
if [ -z "$PID" ]; then
  # No hl-node = nothing to alert on. Silent (visor watchdog handles).
  exit 0
fi

VMRSS_KB=$(awk '/^VmRSS:/ {print $2}' /proc/$PID/status 2>/dev/null)
if [ -z "$VMRSS_KB" ]; then
  exit 0
fi

if [ "$VMRSS_KB" -lt "$THRESHOLD_KB" ]; then
  # Under threshold — silent.
  exit 0
fi

# Rate-limit
MARKER="$STATEDIR/last_alert"
LAST=0
[ -f "$MARKER" ] && LAST=$(cat "$MARKER")
LIMIT_SEC=$((RATE_LIMIT_HOURS * 3600))
if [ $((NOW_TS - LAST)) -lt $LIMIT_SEC ]; then
  logger -t "$LOG_TAG" -p user.info "$TS  RSS=${VMRSS_KB}kB over threshold but rate-limited (last alert $((NOW_TS - LAST))s ago)"
  exit 0
fi

VMRSS_GB=$((VMRSS_KB / 1024 / 1024))
THRESHOLD_GB=$((THRESHOLD_KB / 1024 / 1024))
VMHWM_KB=$(awk '/^VmHWM:/ {print $2}' /proc/$PID/status 2>/dev/null)
ETIME=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')

logger -t "$LOG_TAG" -p user.warning "$TS  ALERT: hl-node RSS=${VMRSS_GB}GB exceeds threshold ${THRESHOLD_GB}GB (PID=$PID uptime=$ETIME)"

(echo "From: ubuntu@${HOST}"
 echo "To: itsdchen@gmail.com"
 echo "Subject: [${HOST}] OOM CANARY: hl-node RSS ${VMRSS_GB}GB exceeds ${THRESHOLD_GB}GB"
 echo ""
 echo "hl-node memory has exceeded the OOM canary threshold."
 echo "This box has 123 GB total RAM; OOM territory is near."
 echo ""
 echo "Current state:"
 echo "  PID:       $PID"
 echo "  uptime:    $ETIME"
 echo "  VmRSS:     ${VMRSS_KB} kB (${VMRSS_GB} GB)"
 echo "  VmHWM:     ${VMHWM_KB} kB ($((VMHWM_KB/1024/1024)) GB)"
 echo "  threshold: ${THRESHOLD_KB} kB (${THRESHOLD_GB} GB)"
 echo ""
 echo "Recommended action:"
 echo "  ssh n1 '/usr/local/bin/hl_weekend_restart.sh --force'"
 echo ""
 echo "This will restart hl-visor + both publishers (~80-90 s wallclock)."
 echo "Consumers will fall back to WS during the restart."
 echo ""
 echo "Rate-limited: next alert at most every ${RATE_LIMIT_HOURS} h."
) | /usr/bin/msmtp itsdchen@gmail.com 2>&1 | logger -t "$LOG_TAG" -p user.warning

echo "$NOW_TS" > "$MARKER"
