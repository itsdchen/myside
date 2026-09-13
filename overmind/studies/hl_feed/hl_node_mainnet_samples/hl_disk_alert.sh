#!/bin/bash
# hl_disk_alert: log + email + (NEW in v3) trigger retention on threshold cross.
#
# - <70%: info heartbeat, no action
# - >=70% (warning): email (1/hr), trigger retention with default thresholds
# - >=85% (critical): email (1/hr), trigger retention with TIGHTER thresholds
#
# Rate-limit only applies to emails. Retention triggers run every check (it's
# idempotent and cheap).

USED_PCT=$(df / --output=pcent | tail -1 | tr -dc '0-9')
USED_GB=$(df -BG / --output=used | tail -1 | tr -dc '0-9')
AVAIL_GB=$(df -BG / --output=avail | tail -1 | tr -dc '0-9')
HOST=$(hostname)
NOW_TS=$(date +%s)
STATEDIR=/var/lib/hl_disk_alert
sudo mkdir -p "$STATEDIR" 2>/dev/null
sudo chmod 777 "$STATEDIR" 2>/dev/null

send_mail() {
  local subj="$1"; local body="$2"; local key="$3"
  local marker="$STATEDIR/last_${key}"
  local last=0
  [ -f "$marker" ] && last=$(cat "$marker")
  if [ $((NOW_TS - last)) -lt 3600 ]; then return 0; fi
  (echo "From: ubuntu@${HOST}"
   echo "To: itsdchen@gmail.com"
   echo "Subject: ${subj}"
   echo ""
   echo "${body}"
  ) | /usr/bin/msmtp itsdchen@gmail.com 2>/dev/null && echo "$NOW_TS" > "$marker"
}

run_retention() {
  local env_vars="$1"
  local marker_key="$2"
  logger -t hl_disk_alert "triggering retention: $env_vars"
  # Run in background so this alert script doesn't block on retention.
  # The retention script uses flock so concurrent triggers won't stack.
  env $env_vars /usr/local/bin/hl_data_retain.sh >/dev/null 2>&1 &
}

if [ "$USED_PCT" -ge 85 ]; then
  logger -t hl_disk_alert -p user.crit "CRITICAL: disk ${USED_PCT}% used (${USED_GB}G used, ${AVAIL_GB}G free) — triggering aggressive retention"
  # Tight thresholds: gzip after 30 min, delete after 24h, aggressive drop after 15 min
  run_retention "GZIP_AFTER_HOURS=0.5 DELETE_AFTER_HOURS=24 AGGRESSIVE_DELETE_HOURS=0.25"
  BODY="CRITICAL disk usage on ${HOST}: ${USED_PCT}% (${USED_GB}G used, ${AVAIL_GB}G free).

Triggered aggressive retention (gzip>30min, delete>24h, aggressive>15min).

$(df -h /)

$(du -sh /home/ubuntu/hl/data/* 2>/dev/null | sort -h | tail -10)

(Rate-limited: max 1 email/hour while critical.)"
  send_mail "[${HOST}] DISK CRITICAL: ${USED_PCT}%" "$BODY" "crit"
elif [ "$USED_PCT" -ge 70 ]; then
  logger -t hl_disk_alert -p user.warning "WARNING: disk ${USED_PCT}% used (${USED_GB}G used, ${AVAIL_GB}G free) — triggering normal retention"
  run_retention ""
  BODY="WARNING disk usage on ${HOST}: ${USED_PCT}% (${USED_GB}G used, ${AVAIL_GB}G free).

Triggered normal retention pass.

$(df -h /)

(Rate-limited: max 1 email/hour while warning.)"
  send_mail "[${HOST}] disk warning: ${USED_PCT}%" "$BODY" "warn"
else
  logger -t hl_disk_alert -p user.info "ok: disk ${USED_PCT}% (${USED_GB}G/${AVAIL_GB}G free)"
fi
