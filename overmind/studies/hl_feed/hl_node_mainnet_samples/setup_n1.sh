#!/usr/bin/env bash
# Adapted from ~/tradefi/pkguide/setup/setup_trademachine.sh — slimmed for
# an HL non-validating node box (Ubuntu 24.04, no trade machine bits).
set -euo pipefail
step() { echo; echo "==== $* ===="; }
skip() { echo "  [skip] $*"; }
did() { echo "  [done] $*"; }
if [[ "${EUID}" -eq 0 ]]; then echo "Run as ubuntu, not root." >&2; exit 1; fi

step "apt update + base packages"
sudo apt-get update -y
sudo apt-get install -y \
  vim emacs gcc cmake tmux csvkit unzip binutils-dev mosh \
  htop jq ncdu net-tools fd-find less git \
  msmtp msmtp-mta mailutils \
  python3-venv python3-pip

step "hostname -> n1"
current_host=$(hostnamectl --static 2>/dev/null || echo "")
if [[ "$current_host" == "n1" ]]; then
  skip "hostname already n1"
else
  sudo hostnamectl set-hostname n1
  did "set hostname (was $current_host)"
fi

step "timezone -> America/New_York"
current_tz=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "")
if [[ "$current_tz" == "America/New_York" ]]; then
  skip "timezone already America/New_York"
else
  sudo timedatectl set-timezone America/New_York
  sudo systemctl restart cron
  did "set timezone (was $current_tz)"
fi

step "coredumps"
LIMITS=/etc/security/limits.conf
for line in "* soft core unlimited" "* hard core unlimited"; do
  if sudo grep -Fxq "$line" "$LIMITS"; then skip "$LIMITS: $line"; else
    echo "$line" | sudo tee -a "$LIMITS" >/dev/null; did "appended $line"
  fi
done
if [[ -f /etc/default/apport ]] && grep -q "^enabled=1" /etc/default/apport; then
  sudo sed -i "s/enabled=1/enabled=0/" /etc/default/apport; did "disabled apport"
else skip "apport already off"; fi
sudo systemctl stop apport 2>/dev/null || true
sudo systemctl disable apport 2>/dev/null || true
SYSCTL=/etc/sysctl.d/60-coredump.conf
DESIRED=$(printf '%s\n' 'kernel.core_pattern=/var/lib/core/core.%e.%p.%h.%t' 'kernel.core_uses_pid=1')
if [[ -f "$SYSCTL" ]] && [[ "$(sudo cat "$SYSCTL")" == "$DESIRED" ]]; then
  skip "$SYSCTL already set"
else
  printf '%s\n' "$DESIRED" | sudo tee "$SYSCTL" >/dev/null; did "wrote $SYSCTL"
fi
sudo sysctl --system >/dev/null
[[ -d /var/lib/core ]] || { sudo mkdir -p /var/lib/core; did "mkdir /var/lib/core"; }
sudo chmod 777 /var/lib/core
echo "  core_pattern -> $(cat /proc/sys/kernel/core_pattern)"

step "wire bash_aliases into .bashrc"
if grep -Fq "source ~/.bash_aliases" ~/.bashrc; then
  skip ".bashrc already sources .bash_aliases"
else
  echo "source ~/.bash_aliases" >> ~/.bashrc
  did "appended source line to .bashrc"
fi

step "done"
echo "Reboot or 'exec bash' to pick up hostname + aliases."
