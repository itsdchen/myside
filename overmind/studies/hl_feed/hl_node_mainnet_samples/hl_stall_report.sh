#!/bin/bash
# hl-node fast/slow pipeline stall report.
# Usage: hl_stall_report.sh [YYYYMMDD] [label]
#   Default date: yesterday UTC. Default label: 'HL stall report'.
# Read-only with respect to all node data and services.
# Appends current hl-node live state (RSS, HWM, threads, uptime) so we can
# track memory drift over time via journalctl -t hl_stall_report.
set -u
HOST=$(hostname)
DATE=${1:-$(date -u -d 'yesterday' +%Y%m%d)}
LABEL=${2:-HL stall report}
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BODY=$(/usr/local/bin/hl_stall_report.py "$DATE" 2>&1)
if [ -z "$BODY" ]; then
  BODY="hl_stall_report.py produced empty output for $DATE"
fi

# Append current hl-node live state for memory-trend tracking
PID=$(pgrep -f /home/ubuntu/hl-node | head -1)
if [ -n "$PID" ]; then
  ETIME=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
  MEM_LINES=$(awk '/^VmSize:|^VmRSS:|^VmHWM:|^Threads:|^voluntary_ctxt_switches:|^nonvoluntary_ctxt_switches:/ {print "  "$0}' /proc/$PID/status 2>/dev/null)
  LIVE_STATE="

== hl-node live state at $TS ==
  PID: $PID
  uptime: $ETIME
$MEM_LINES"
else
  LIVE_STATE="

== hl-node live state at $TS ==
  (no hl-node process found)"
fi
BODY="$BODY$LIVE_STATE"

echo "$BODY" | logger -t hl_stall_report -p user.info
(echo "From: ubuntu@${HOST}"
 echo "To: itsdchen@gmail.com"
 echo "Subject: [${HOST}] ${LABEL} ${DATE}"
 echo ""
 echo "$BODY"
 echo ""
 echo "-- generated ${TS} by /usr/local/bin/hl_stall_report.sh on ${HOST}"
) | /usr/bin/msmtp itsdchen@gmail.com 2>&1 | logger -t hl_stall_report -p user.warning
