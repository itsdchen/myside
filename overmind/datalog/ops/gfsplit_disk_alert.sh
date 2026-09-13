#!/bin/bash
# gfsplit_disk_alert: passive monitor for the datalog/capture box.
#
# Runs from cron every 30 min. Two things it checks:
#
#   1. Disk usage on /. Email warning at >=70%, critical at >=85%.
#      Rate-limited to 1 email/hour per severity.
#
#   2. Long-running capture wrappers. We had a case where a daily-capture.sh
#      from May 1 ran for 43 days continuously, accumulating 386 GB of
#      stderr to a deleted log file, before anyone noticed. Anything
#      named *capture*.sh that's been running > 36 hours is flagged
#      (a healthy daily capture exits within ~25 h).
#
# This script does NOT trigger retention — gfsplit's cleanup is whatever
# its postprocess-captures-*.sh cron jobs do (move to s3 + rm local). If
# disk is filling up, the right operator response is "look at logs/ and
# raw/ and figure out which capture or postproc isn't cleaning up."
#
# Install at /usr/local/bin/gfsplit_disk_alert.sh, chmod +x, cron entry:
#   */30 * * * * /usr/local/bin/gfsplit_disk_alert.sh
#
# Requires msmtp configured for the host's user, same way n1's
# hl_disk_alert.sh expects.

USED_PCT=$(df / --output=pcent | tail -1 | tr -dc '0-9')
USED_GB=$(df -BG / --output=used | tail -1 | tr -dc '0-9')
AVAIL_GB=$(df -BG / --output=avail | tail -1 | tr -dc '0-9')
HOST=$(hostname)
NOW_TS=$(date +%s)
STATEDIR=/var/lib/gfsplit_disk_alert
ALERT_EMAIL=itsdchen@gmail.com

sudo mkdir -p "$STATEDIR" 2>/dev/null
sudo chmod 777 "$STATEDIR" 2>/dev/null

send_mail() {
  local subj="$1"; local body="$2"; local key="$3"
  local marker="$STATEDIR/last_${key}"
  local last=0
  [ -f "$marker" ] && last=$(cat "$marker")
  if [ $((NOW_TS - last)) -lt 3600 ]; then return 0; fi
  (echo "From: ubuntu@${HOST}"
   echo "To: ${ALERT_EMAIL}"
   echo "Subject: ${subj}"
   echo ""
   echo "${body}"
  ) | /usr/bin/msmtp ${ALERT_EMAIL} 2>/dev/null && echo "$NOW_TS" > "$marker"
}

top_consumers() {
  du -sh /home/ubuntu/logs /home/ubuntu/raw /home/ubuntu/gzpbf /home/ubuntu/split 2>/dev/null | sort -h | tail -5
  echo "---"
  du -sh /home/ubuntu/logs/* 2>/dev/null | sort -h | tail -5
}

zombie_check() {
  # Find capture-wrapper processes running > 36 hours (etime hh-mm-ss or d-hh:mm:ss).
  # ps etime formats: SS, MM:SS, HH:MM:SS, DD-HH:MM:SS.
  ps -eo pid,etime,etimes,cmd | awk -v threshold=$((36 * 3600)) '
    NR > 1 && $3 ~ /^[0-9]+$/ && $3 > threshold {
      cmd = ""
      for (i = 4; i <= NF; i++) cmd = cmd " " $i
      if (cmd ~ /capture.*\.sh/ || cmd ~ /simple_datalog/) {
        printf "  PID %s (etime %s): %s\n", $1, $2, cmd
      }
    }'
}

# ---- disk check -------------------------------------------------------------

ZOMBIE_INFO=$(zombie_check)

if [ "$USED_PCT" -ge 85 ]; then
  logger -t gfsplit_disk_alert -p user.crit "CRITICAL: disk ${USED_PCT}% used (${USED_GB}G used, ${AVAIL_GB}G free)"
  BODY="CRITICAL disk usage on ${HOST}: ${USED_PCT}% (${USED_GB}G used, ${AVAIL_GB}G free).

No automatic retention runs on gfsplit — manual investigation required.

$(df -h /)

Top consumers:
$(top_consumers)

Long-running captures (>36h):
${ZOMBIE_INFO:-  (none)}

(Rate-limited: max 1 email/hour while critical.)"
  send_mail "[${HOST}] DISK CRITICAL: ${USED_PCT}%" "$BODY" "crit"
elif [ "$USED_PCT" -ge 70 ]; then
  logger -t gfsplit_disk_alert -p user.warning "WARNING: disk ${USED_PCT}% used (${USED_GB}G used, ${AVAIL_GB}G free)"
  BODY="WARNING disk usage on ${HOST}: ${USED_PCT}% (${USED_GB}G used, ${AVAIL_GB}G free).

$(df -h /)

Top consumers:
$(top_consumers)

Long-running captures (>36h):
${ZOMBIE_INFO:-  (none)}

(Rate-limited: max 1 email/hour while warning.)"
  send_mail "[${HOST}] DISK WARNING: ${USED_PCT}%" "$BODY" "warn"
else
  logger -t gfsplit_disk_alert "info: disk ${USED_PCT}% used (${USED_GB}G used, ${AVAIL_GB}G free)"
fi

# ---- zombie check (independent of disk %) -----------------------------------

if [ -n "$ZOMBIE_INFO" ]; then
  logger -t gfsplit_disk_alert -p user.warning "long-running capture wrappers detected"
  BODY="One or more capture wrappers on ${HOST} have been running > 36h.

A healthy daily capture exits within ~25h. Long-runners typically
indicate a stuck simple_datalog child that's accumulating log noise
to deleted file descriptors — exactly the failure mode that ate 386 GB
of disk on 2026-05-01.

${ZOMBIE_INFO}

To investigate:
  ssh ${HOST} 'ps -ef | grep simple_datalog | grep -v grep'
  ssh ${HOST} 'sudo lsof -nP | grep deleted | sort -k7 -n | tail'

To kill (verify first):
  ssh ${HOST} 'kill -9 <PID>'

(Rate-limited: max 1 email/hour.)"
  send_mail "[${HOST}] CAPTURE WRAPPER STUCK" "$BODY" "zombie"
fi
