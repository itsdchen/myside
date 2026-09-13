#!/usr/bin/env bash
# bootstrap.sh — Phase-B setup of a freshly-leased Hetzner Ubuntu 22.04 box.
#
# What Phase A (manual) did first:
#   1. Lease box, installimage Ubuntu 22.04 Jammy via Hetzner rescue OS.
#   2. Add your pubkey to ~/.ssh/authorized_keys on the box.
#   3. Add an SSH config alias for the box (e.g. `hetzner-1`) to ~/.ssh/config.
#
# What this script does (run from local):
#   - Verifies SSH reachability + that remote is Jammy.
#   - apt-installs packages required to run pktrade + md_exists.py + cron.
#   - Creates ~/.venvs/v1/ on remote and pip-installs requirements.
#   - Creates /opt/pktrade/experiments/ and ~/tardis_datasets/gzpbf/.
#   - Pushes md_exists.py.
#   - Pushes AWS credentials (read-only IAM user) from ~/.aws/hetzner-readonly.
#   - Installs /etc/cron.d/pktrade-cleanup for daily mktdata + experiments cleanup.
#   - Pushes the cleanup scripts themselves.
#   - Self-test: md_exists.py reachable, dirs exist, cron file present.
#
# Idempotent — safe to re-run anytime to repair drift.
#
# Usage:
#   ./overmind/swarmhost/bootstrap.sh <ssh-alias>
#
# Examples:
#   ./overmind/swarmhost/bootstrap.sh hetzner-1
#   ./overmind/swarmhost/bootstrap.sh shd0
#
# Args:
#   <ssh-alias>   The SSH alias for the target box, resolvable via ~/.ssh/config.
#
# This is an iterative tool — when it falls behind reality, edit it.

set -euo pipefail

# ── Arg parsing ─────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    echo "usage: $0 <ssh-alias>" >&2
    exit 2
fi
ALIAS="$1"

# Resolve paths relative to this script's location, so the script works
# regardless of CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MD_EXISTS_LOCAL="${REPO_ROOT}/overmind/strat_main/tools/md_exists.py"
PIP_REQS_LOCAL="${HOME}/tradefi/pkguide/setup/pip_reqs.txt"
AWS_CREDS_LOCAL="${HOME}/.aws/hetzner-readonly"
DOTFILES_LOCAL="${HOME}/tradefi/pkguide/dotfiles"
# Dotfiles to push for SSH comfort. Skip .aws on purpose — its credentials
# file holds a full-access key, and we want only the dedicated read-only
# IAM user (above) on Hetzner.
DOTFILES_TO_PUSH=(.bash_aliases .emacs .emacs.d .vimrc .vim)

# ── 1. Reachability + version check ─────────────────────────────────────────
echo "==> [1/11] Checking SSH + Jammy on ${ALIAS}"
remote_release=$(ssh -o BatchMode=yes "$ALIAS" 'lsb_release -cs' 2>/dev/null || true)
if [[ "$remote_release" != "jammy" ]]; then
    echo "ERROR: remote ${ALIAS} is not Ubuntu 22.04 Jammy (got: '${remote_release}')." >&2
    echo "Bootstrap currently assumes Jammy to match the local jammy.bin build." >&2
    exit 1
fi
echo "    ok: jammy."

# ── 2. SSH hardening (run first — fresh Hetzner boxes get brute-forced
#                       within minutes of getting a public IP) ──────────────
# Three layers:
#   - PasswordAuthentication no  → sshd refuses password auth entirely
#   - PermitRootLogin prohibit-password → root may key-auth, never pw-auth
#   - passwd -l root → no password on the system can ever match
# Defense in depth so a future config drift doesn't reopen the door.
echo "==> [2/11] Hardening sshd (disable password auth, lock root password)"
ssh "$ALIAS" 'set -e
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d-%H%M%S) 2>/dev/null || true
sudo sed -i \
    -e "s/^#*PasswordAuthentication .*/PasswordAuthentication no/" \
    -e "s/^#*PermitRootLogin .*/PermitRootLogin prohibit-password/" \
    /etc/ssh/sshd_config
sudo sshd -t                              # validate before restart, or we lose access
sudo systemctl restart ssh
sudo passwd -l root >/dev/null
echo "    sshd hardened; root password locked."'

# ── 3. apt-installs ─────────────────────────────────────────────────────────
# Two sets:
#   REQUIRED — needed for pktrade + tooling + cleanup cron.
#   COMFORT  — editors and TTY tools so SSHing in to debug isn't bleak.
# Re-run `ldd jammy.bin/pktrade` on the remote if something's missing from
# REQUIRED and add it to this list.
echo "==> [3/11] Installing OS packages"
ssh "$ALIAS" 'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq'
ssh "$ALIAS" 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    rsync \
    python3.10-venv \
    python3-pip \
    awscli \
    cron \
    git \
    libgoogle-glog0v5 \
    libgflags2.2 \
    libsnappy1v5 \
    libprotobuf23 \
    zlib1g \
    libssl3 \
    ca-certificates \
    vim \
    emacs-nox \
    tmux \
    mosh \
    csvkit \
    fd-find \
    unzip \
    net-tools'

# ── 3. Git config (best-effort; mirrors install_lines.txt values) ──────────
echo "==> [4/11] Setting global git config"
ssh "$ALIAS" 'git config --global user.email "itsdchen@gmail.com" && \
              git config --global user.name "David Chen" && \
              git config --global core.editor "vim"'

# ── 4b. Timezone (must match local) ─────────────────────────────────────────
# pktrade interprets `--date YYYYMMDD` relative to system local time. If the
# swarmhost is UTC and local is EDT, the day-boundary slices the data
# window differently and you get divergent sim outputs on dates straddling
# the session-open boundary (e.g. Sunday-evening CME re-open → Monday).
# Pinning TZ to America/New_York keeps remote ↔ local results bit-identical.
echo "    setting TZ to America/New_York"
ssh "$ALIAS" 'sudo timedatectl set-timezone America/New_York'

# ── 5. Python venv + pip reqs ───────────────────────────────────────────────
echo "==> [5/11] Setting up ~/.venvs/v1 + pip requirements"
ssh "$ALIAS" 'mkdir -p ~/.venvs && [ -d ~/.venvs/v1 ] || python3 -m venv ~/.venvs/v1'

if [[ -f "$PIP_REQS_LOCAL" ]]; then
    scp -q "$PIP_REQS_LOCAL" "${ALIAS}:/tmp/pip_reqs.txt"
    ssh "$ALIAS" '~/.venvs/v1/bin/pip install --quiet --upgrade pip && \
                  ~/.venvs/v1/bin/pip install --quiet -r /tmp/pip_reqs.txt'
else
    echo "    warn: no pip_reqs.txt at ${PIP_REQS_LOCAL}; skipping pip install."
fi

# ── 5. Remote directory tree ────────────────────────────────────────────────
echo "==> [6/11] Creating /opt/pktrade and ~/tardis_datasets"
ssh "$ALIAS" 'sudo mkdir -p /opt/pktrade/experiments && \
              sudo chown -R $USER:$USER /opt/pktrade && \
              mkdir -p ~/tardis_datasets/gzpbf'

# ── 6. md_exists.py ─────────────────────────────────────────────────────────
echo "==> [7/11] Pushing md_exists.py"
if [[ ! -f "$MD_EXISTS_LOCAL" ]]; then
    echo "ERROR: md_exists.py not found at ${MD_EXISTS_LOCAL}" >&2
    exit 1
fi
ssh "$ALIAS" 'mkdir -p ~/bin'
scp -q "$MD_EXISTS_LOCAL" "${ALIAS}:~/bin/md_exists.py"
ssh "$ALIAS" 'chmod +x ~/bin/md_exists.py'

# ── 7. Credentials ──────────────────────────────────────────────────────────
# AWS (read-only IAM user, for md_exists.py to pull mktdata from S3) +
# stubbed exchange creds (so pktrade can load them but CANNOT send real
# signed orders). HARD INVARIANT: swarmhost must never hold valid exchange
# keys. If pktrade complains about a missing venue stub, add it below.
# Never copy real keys from ~/.creds to remote.
echo "==> [8/11] Pushing AWS credentials + stubbed exchange creds"

# 7a. AWS read-only (optional — md_exists.py without it can't pull S3).
if [[ -f "$AWS_CREDS_LOCAL" ]]; then
    ssh "$ALIAS" 'mkdir -p ~/.aws && chmod 700 ~/.aws'
    scp -q "$AWS_CREDS_LOCAL" "${ALIAS}:~/.aws/credentials"
    ssh "$ALIAS" 'chmod 600 ~/.aws/credentials'
else
    echo "    warn: no read-only creds file at ${AWS_CREDS_LOCAL}."
    echo "    Create one (with read-only IAM user) before md_exists.py will work on remote."
fi

# 7b. Stub exchange/storage creds — pktrade loads these on init even in
# sim mode. Zero-address / placeholder values; auth + signing would fail
# if anything ever tried to use them.
ssh "$ALIAS" 'mkdir -p ~/.creds && chmod 700 ~/.creds'
ssh "$ALIAS" 'cat > ~/.creds/.Hyperliquid.creds.json && chmod 600 ~/.creds/.Hyperliquid.creds.json' <<'EOF'
{
  "address": "0x0000000000000000000000000000000000000000",
  "subaccount_address": "0x0000000000000000000000000000000000000000",
  "secret_key": "0x0000000000000000000000000000000000000000000000000000000000000000"
}
EOF
ssh "$ALIAS" 'cat > ~/.creds/.Supabase.creds.json && chmod 600 ~/.creds/.Supabase.creds.json' <<'EOF'
{
  "note": "STUB — swarmhost must not perform real Supabase ops",
  "project_id": "stub-project",
  "key was found": "no",
  "api_key": "stub-key",
  "closing_print_table": "stub_table",
  "mainnet_position_table": "stub_table",
  "testnet_position_table": "stub_table",
  "snapshot_table": "stub_table",
  "mainnet_usermsg_table": "stub_table",
  "testnet_usermsg_table": "stub_table",
  "email": "stub@example.invalid",
  "password": "stub"
}
EOF
echo "    installed stubs: .Hyperliquid.creds.json, .Supabase.creds.json"

# ── 8. Cleanup scripts + cron ───────────────────────────────────────────────
echo "==> [9/11] Installing cleanup scripts + cron"
ssh "$ALIAS" 'mkdir -p ~/bin'
scp -q "${SCRIPT_DIR}/mktdata_cleanup.sh" "${ALIAS}:~/bin/mktdata_cleanup.sh"
scp -q "${SCRIPT_DIR}/experiments_cleanup.sh" "${ALIAS}:~/bin/experiments_cleanup.sh"
ssh "$ALIAS" 'chmod +x ~/bin/mktdata_cleanup.sh ~/bin/experiments_cleanup.sh'

# Write cron file outright (idempotent — no duplicate-line drift).
# Stagger times so the two jobs don't race for IO.
# Query the actual $HOME (not /home/$USER — fails for root, whose home is /root).
CRON_USER=$(ssh "$ALIAS" 'whoami')
CRON_HOME=$(ssh "$ALIAS" 'echo $HOME')
ssh "$ALIAS" "sudo tee /etc/cron.d/pktrade-cleanup >/dev/null" <<EOF
# pktrade cleanup — installed by overmind/swarmhost/bootstrap.sh.
# Runs daily; both scripts are no-ops if their target dirs don't exist.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 4 * * * ${CRON_USER} ${CRON_HOME}/bin/mktdata_cleanup.sh >> /var/log/pktrade-cleanup.log 2>&1
35 4 * * * root ${CRON_HOME}/bin/experiments_cleanup.sh >> /var/log/pktrade-cleanup.log 2>&1
EOF
ssh "$ALIAS" 'sudo chmod 644 /etc/cron.d/pktrade-cleanup && \
              sudo touch /var/log/pktrade-cleanup.log && \
              sudo systemctl restart cron'

# ── 9. Dotfiles + bashrc + mosh firewall ──────────────────────────────────
echo "==> [10/11] Pushing dotfiles + opening mosh UDP range"
if [[ -d "$DOTFILES_LOCAL" ]]; then
    # rsync each dotfile/dir into $HOME on remote. rsync -a preserves perms;
    # --delete only on dirs we own so we don't nuke files added on remote.
    for d in "${DOTFILES_TO_PUSH[@]}"; do
        src="${DOTFILES_LOCAL}/${d}"
        if [[ ! -e "$src" ]]; then
            echo "    skip: ${src} (not present locally)"
            continue
        fi
        # Trailing slash on dirs keeps rsync's "copy contents" semantics
        # consistent with scp -r semantics.
        if [[ -d "$src" ]]; then
            rsync -a "${src}/" "${ALIAS}:~/${d}/"
        else
            rsync -a "$src" "${ALIAS}:~/${d}"
        fi
    done

    # Idempotent: only append the bash_aliases source line if it isn't already there.
    ssh "$ALIAS" 'grep -qF "source ~/.bash_aliases" ~/.bashrc 2>/dev/null || \
                  echo "source ~/.bash_aliases" >> ~/.bashrc'
else
    echo "    warn: dotfiles dir not found at ${DOTFILES_LOCAL}; skipping."
fi

# Open mosh UDP range if ufw is active. Hetzner doesn't have a security
# group — only the box's own firewall matters. mosh-server is NOT started
# here; the mosh client starts it on demand when you `mosh ${ALIAS}`.
ssh "$ALIAS" 'if command -v ufw >/dev/null && sudo ufw status | grep -q "^Status: active"; then
    sudo ufw allow 60000:61000/udp >/dev/null
    echo "    opened UDP 60000-61000 for mosh (ufw was active)."
else
    echo "    ufw inactive — no firewall changes needed for mosh."
fi'

# ── 10. Self-test ───────────────────────────────────────────────────────────
echo "==> [11/11] Self-test"
ssh "$ALIAS" 'set -e
[ -d /opt/pktrade/experiments ]
[ -d ~/tardis_datasets/gzpbf ]
[ -x ~/bin/md_exists.py ]
[ -x ~/bin/mktdata_cleanup.sh ]
[ -x ~/bin/experiments_cleanup.sh ]
[ -f /etc/cron.d/pktrade-cleanup ]
~/.venvs/v1/bin/python -c "import sys; sys.exit(0)"
command -v mosh >/dev/null
command -v tmux >/dev/null
command -v vim >/dev/null
echo "    all checks passed."'

echo
echo "Bootstrap complete: ${ALIAS} is ready to receive experiments."
echo "Connect with: mosh ${ALIAS}   (or)   ssh ${ALIAS}"
