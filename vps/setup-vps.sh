#!/usr/bin/env bash
# setup-vps.sh — Configure a fresh Ubuntu VPS for Claude Code + Obsidian Sync
#
# Run as root on a fresh Hetzner Ubuntu 24.04 box.
# This script is idempotent — safe to re-run (UFW rules are checked, not reset).
#
# Security model:
# - Tailscale is installed FIRST (step 3) to minimize public SSH exposure
# - With --tailscale-key, auth is non-interactive (~60s total public SSH window)
# - UFW restricts SSH to Tailscale subnet only (100.64.0.0/10)
# - Hetzner firewall provides defense-in-depth (caller IP only)
# - SSH hardened: key-only, no root login, fail2ban
#
# What it does:
# - Creates 'kyle' user with sudo
# - Installs and authenticates Tailscale (EARLY — step 3)
# - Locks down UFW to Tailscale-only SSH immediately (+ Netdata on 19999)
# - Installs Node.js 22, Git, tmux, Python 3, fail2ban, earlyoom
# - Installs obsidian-headless and Claude Code (as kyle, not root)
# - Hardens SSH (key-only, no root login — with lockout safeguard + OOMScoreAdjust)
# - Resilience stack (added 2026-04-16): 4GB swap, sysctl tuning, earlyoom config,
#   Netdata (Tailscale-bound), journald cap
# - Sets up ob-sync systemd service
# - Creates .env.sh template and wrapper scripts
# - Configures unattended security upgrades
#
# Assumes minimum 4GB RAM on target box (Netdata steady-state ~80-100MB RSS
# makes this the practical floor for the CX-class Hetzner boxes we use).
#
# UNEXERCISED (2026-04-17): The resilience stack steps (memory pressure,
# Netdata, journald cap) and the sshd OOMScoreAdjust dropin were ported from
# faramir's live config but have not yet been run end-to-end on a fresh provision.
# Next real spin-up is the validation — watch for package name drift, netdata.conf
# grammar, and sed escape edge cases.
#
# Usage:
#   /root/setup-vps.sh --tailscale-key tskey-auth-... --hostname bruce-vps
#   /root/setup-vps.sh   # interactive Tailscale auth (fallback)

set -euo pipefail

# Log everything to a file so we don't lose output on disconnect
LOG_FILE="/root/setup-vps.log"
exec > >(tee "$LOG_FILE") 2>&1
echo "Setup log: ${LOG_FILE}"

# --- Preflight ---
if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root."
  exit 1
fi

USERNAME="kyle"
VAULT_PATH="/home/${USERNAME}/vault"
TAILSCALE_KEY=""
TAILSCALE_HOSTNAME=""

# --- Parse flags ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --tailscale-key) TAILSCALE_KEY="$2"; shift 2 ;;
    --hostname) TAILSCALE_HOSTNAME="$2"; shift 2 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

echo "=== VPS Setup for Claude Code + Obsidian Headless Sync ==="
echo ""

# --- System updates ---
echo "[1/17] Updating system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
# Install curl and jq early — needed by Tailscale install (curl) and status parsing (jq)
apt-get install -y -qq curl jq
echo "Done."
echo ""

# --- Create user ---
echo "[2/17] Setting up user '${USERNAME}'..."
if id "$USERNAME" &>/dev/null; then
  echo "User '${USERNAME}' already exists."
else
  adduser --disabled-password --gecos "" "$USERNAME"
  usermod -aG sudo "$USERNAME"
  # Passwordless sudo — pragmatic for a personal Tailscale-only box where Claude Code
  # needs to restart services. If this box were multi-tenant or public-facing, scope
  # this down to specific commands instead.
  echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME}
  chmod 440 /etc/sudoers.d/${USERNAME}
  echo "User created with sudo access."
fi

# Copy SSH authorized keys from root
if [[ -f /root/.ssh/authorized_keys ]]; then
  mkdir -p /home/${USERNAME}/.ssh
  cp /root/.ssh/authorized_keys /home/${USERNAME}/.ssh/
  chown -R ${USERNAME}:${USERNAME} /home/${USERNAME}/.ssh
  chmod 700 /home/${USERNAME}/.ssh
  chmod 600 /home/${USERNAME}/.ssh/authorized_keys
  echo "SSH keys copied to ${USERNAME}."
fi

# Generate bruce inter-server SSH key (allows Bruce boxes to SSH to each other)
BRUCE_KEY="/home/${USERNAME}/.ssh/id_bruce"
if [[ ! -f "$BRUCE_KEY" ]]; then
  sudo -u "$USERNAME" ssh-keygen -t ed25519 -f "$BRUCE_KEY" -N "" -C "bruce-inter-server"
  cat "${BRUCE_KEY}.pub" >> "/home/${USERNAME}/.ssh/authorized_keys"
  echo "Bruce inter-server key generated and added to authorized_keys."
else
  echo "Bruce inter-server key already exists."
fi

# SSH config for inter-server connections via Tailscale
BRUCE_SSH_CONFIG="/home/${USERNAME}/.ssh/config"
if ! grep -q "id_bruce" "$BRUCE_SSH_CONFIG" 2>/dev/null; then
  cat >> "$BRUCE_SSH_CONFIG" << 'SSHCFG'
# Bruce inter-server SSH: matches bruce-vps* and known LOTR-named servers
Host bruce-vps* faramir orthanc amon-sul erebor
  IdentityFile ~/.ssh/id_bruce
  User kyle
  StrictHostKeyChecking accept-new
SSHCFG
  chown ${USERNAME}:${USERNAME} "$BRUCE_SSH_CONFIG"
  chmod 600 "$BRUCE_SSH_CONFIG"
  echo "SSH config for bruce-to-bruce connections created."
fi
echo ""

# --- Tailscale (EARLY — minimize public SSH exposure) ---
echo "[3/17] Installing Tailscale..."
if command -v tailscale &>/dev/null; then
  echo "Tailscale already installed."
else
  # Install curl first if not present (minimal Ubuntu images may lack it)
  command -v curl &>/dev/null || apt-get install -y -qq curl
  # Download install script first, then run
  TAILSCALE_SCRIPT="/tmp/tailscale_install.sh"
  curl -fsSL https://tailscale.com/install.sh -o "$TAILSCALE_SCRIPT"
  echo "Tailscale install script downloaded."
  echo "Script size: $(wc -c < "$TAILSCALE_SCRIPT") bytes, SHA256: $(sha256sum "$TAILSCALE_SCRIPT" | cut -d' ' -f1)"
  sh "$TAILSCALE_SCRIPT"
  rm -f "$TAILSCALE_SCRIPT"
fi

# Authenticate Tailscale
# No --ssh flag: we use regular OpenSSH over Tailscale instead of Tailscale's
# built-in SSH, which would bypass our SSH hardening config.
TS_UP_ARGS=()
if [[ -n "$TAILSCALE_KEY" ]]; then
  TS_UP_ARGS+=(--authkey "$TAILSCALE_KEY")
  echo "Authenticating Tailscale with pre-auth key (non-interactive)..."
else
  echo ""
  echo "  ┌─────────────────────────────────────────────┐"
  echo "  │  Tailscale authentication required.          │"
  echo "  │  A URL will appear — open it in your browser │"
  echo "  │  to approve this device on your tailnet.     │"
  echo "  └─────────────────────────────────────────────┘"
  echo ""
fi
if [[ -n "$TAILSCALE_HOSTNAME" ]]; then
  TS_UP_ARGS+=(--hostname "$TAILSCALE_HOSTNAME")
fi

tailscale up "${TS_UP_ARGS[@]}"
echo ""
echo "Tailscale connected!"
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
TS_DNS_NAME=$(tailscale status --self --json 2>/dev/null | jq -r '.Self.DNSName // "unknown"' | sed 's/\.$//')
echo "  Tailscale IP: ${TAILSCALE_IP}"
echo "  Tailscale hostname: ${TS_DNS_NAME}"
echo ""

# --- UFW Firewall (lock down SSH to Tailscale immediately) ---
echo "[4/17] Configuring firewall (Tailscale-only SSH)..."
if ufw status | grep -q "Status: active"; then
  echo "UFW already active. Verifying rules..."
  # Check for Tailscale-restricted SSH rule
  if ufw status | grep -q "100.64.0.0/10"; then
    echo "SSH already restricted to Tailscale subnet."
  else
    # Remove any wide-open SSH rules and replace with Tailscale-only
    ufw delete allow 22/tcp 2>/dev/null || true
    ufw allow from 100.64.0.0/10 to any port 22 proto tcp comment "SSH (Tailscale only)"
    echo "SSH rule tightened to Tailscale subnet."
  fi
  ufw status | grep -q "41641/udp" || ufw allow 41641/udp comment "Tailscale WireGuard"
  ufw status | grep -q "19999" || ufw allow from 100.64.0.0/10 to any port 19999 proto tcp comment "Netdata (Tailscale only)"
  echo "UFW rules verified."
else
  ufw default deny incoming
  ufw default allow outgoing
  # SSH restricted to Tailscale subnet (100.64.0.0/10 = CGNAT range used by Tailscale)
  ufw allow from 100.64.0.0/10 to any port 22 proto tcp comment "SSH (Tailscale only)"
  ufw allow 41641/udp comment "Tailscale WireGuard"
  # Netdata dashboard: loopback + Tailscale only, never public
  ufw allow from 100.64.0.0/10 to any port 19999 proto tcp comment "Netdata (Tailscale only)"
  ufw --force enable
  echo "UFW enabled. SSH restricted to Tailscale subnet. WireGuard + Netdata allowed."
fi
echo ""
echo "  ┌──────────────────────────────────────────────────────┐"
echo "  │  PUBLIC SSH IS NOW BLOCKED.                          │"
echo "  │  Your current session will survive, but if dropped,  │"
echo "  │  reconnect via Tailscale: ssh root@${TAILSCALE_IP}   │"
echo "  └──────────────────────────────────────────────────────┘"
echo ""

# --- Install system packages ---
echo "[5/17] Installing system packages..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  git \
  tmux \
  python3 \
  python3-pip \
  python3-venv \
  python3-pytest \
  fail2ban \
  ufw \
  unattended-upgrades \
  apt-listchanges \
  curl \
  jq \
  earlyoom \
  netdata
echo "Done."
echo ""

# --- Install Node.js 22 ---
echo "[6/17] Installing Node.js 22 LTS..."
if command -v node &>/dev/null && [[ "$(node --version | cut -d. -f1 | tr -d v)" -ge 22 ]]; then
  echo "Node.js $(node --version) already installed."
else
  # Download the setup script first, then inspect and run
  NODESOURCE_SCRIPT="/tmp/nodesource_setup.sh"
  curl -fsSL https://deb.nodesource.com/setup_22.x -o "$NODESOURCE_SCRIPT"
  echo "NodeSource setup script downloaded to ${NODESOURCE_SCRIPT}"
  echo "Script size: $(wc -c < "$NODESOURCE_SCRIPT") bytes, SHA256: $(sha256sum "$NODESOURCE_SCRIPT" | cut -d' ' -f1)"
  bash "$NODESOURCE_SCRIPT"
  rm -f "$NODESOURCE_SCRIPT"
  apt-get install -y -qq nodejs
  echo "Node.js $(node --version) installed."
fi
echo ""

# --- Install obsidian-headless (npm) and Claude Code (native installer) ---
echo "[7/17] Installing obsidian-headless and Claude Code..."
# obsidian-headless: npm as kyle user (not root) to avoid supply chain risk
sudo -u "$USERNAME" bash -c '
  mkdir -p ~/.npm-global
  npm config set prefix ~/.npm-global
  npm install -g obsidian-headless --loglevel=warn
'
# Claude Code: native installer (npm package is deprecated)
# Install URL changed from cli.anthropic.com to claude.ai as of early March 2026
sudo -u "$USERNAME" bash -c '
  curl -fsSL https://claude.ai/install.sh | bash
'
echo "obsidian-headless installed to /home/${USERNAME}/.npm-global/"
echo "Claude Code installed via native installer"
echo ""

# --- Install Python packages (for vault scripts) ---
echo "[8/17] Installing Python packages..."
pip3 install --break-system-packages -q requests markdown 2>/dev/null || \
  pip3 install -q requests markdown
echo "Done."
echo ""

# --- SSH hardening ---
echo "[9/17] Hardening SSH..."
SSHD_CONFIG="/etc/ssh/sshd_config"

# Disable password auth
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "$SSHD_CONFIG"

# Lockout safeguard: verify kyle can authenticate before disabling root
KYLE_AUTH="/home/${USERNAME}/.ssh/authorized_keys"
if [[ -f "$KYLE_AUTH" ]] && [[ -s "$KYLE_AUTH" ]]; then
  KEY_COUNT=$(wc -l < "$KYLE_AUTH")
  echo "Verified: ${USERNAME} has ${KEY_COUNT} SSH key(s) in authorized_keys."
  sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONFIG"
  echo "Root login disabled."
else
  echo "WARNING: ${USERNAME}'s authorized_keys is missing or empty!"
  echo "Leaving root login enabled (prohibit-password) as safety fallback."
  sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' "$SSHD_CONFIG"
fi

# sshd OOM protection — if the kernel OOM killer ever fires, sshd should be
# last on the hit list. Guarantees we can still get back in after a memory spike.
mkdir -p /etc/systemd/system/ssh.service.d
cat > /etc/systemd/system/ssh.service.d/oom.conf << 'OOM'
[Service]
OOMScoreAdjust=-900
OOM

systemctl daemon-reload
# Ubuntu 24.04 uses 'ssh' not 'sshd' as the service name
systemctl restart ssh
echo "SSH hardened: password auth disabled, OOMScoreAdjust=-900."
echo ""

# --- fail2ban configuration ---
echo "[10/17] Configuring fail2ban (Tailscale-aware)..."
cat > /etc/fail2ban/jail.local << 'JAIL'
[sshd]
enabled = true
port = 22
maxretry = 3
bantime = 3600
findtime = 600
backend = systemd
# Ubuntu 24.04 uses 'ssh' not 'sshd' as the systemd unit name
journalmatch = _SYSTEMD_UNIT=ssh.service + _COMM=sshd
JAIL
systemctl restart fail2ban
echo "fail2ban configured (3 retries, 1 hour ban)."
echo ""

# --- Memory pressure layer (swap + sysctl + earlyoom) ---
echo "[11/17] Configuring memory pressure layer..."

# 4GB swap file (safety buffer, not for active paging)
if swapon --show | grep -q "/swapfile"; then
  echo "Swap file already active."
else
  # fallocate is fastest; dd is the fallback on filesystems that don't support it.
  if fallocate -l 4G /swapfile 2>/dev/null; then
    echo "Swap file allocated via fallocate."
  else
    dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none
    echo "Swap file allocated via dd."
  fi
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  if ! grep -q "^/swapfile" /etc/fstab; then
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
  fi
  echo "4GB swap file created and activated."
fi

# sysctl tuning: keep swap as a safety net, not an active paging layer
cat > /etc/sysctl.d/99-swap-tuning.conf << 'SYSCTL'
vm.swappiness=10
vm.vfs_cache_pressure=50
SYSCTL
sysctl --system >/dev/null
echo "sysctl tuning applied (swappiness=10, vfs_cache_pressure=50)."

# earlyoom config — kills biggest RAM hog before kernel OOM killer hangs the box
cat > /etc/default/earlyoom << 'EARLYOOM'
# Trigger at <10% available RAM or <5% free swap; report hourly.
# --avoid: never kill critical session/init processes (keeps SSH+tmux alive).
# --prefer: preferentially target known memory hogs.
EARLYOOM_ARGS="-r 3600 -m 10 -s 5 --avoid '^(sshd|systemd|bash|tmux|cron)$' --prefer '^(node|python|chrome|firefox|claude)$'"
EARLYOOM
systemctl enable --now earlyoom
systemctl restart earlyoom
echo "earlyoom configured and running."
echo ""

# --- Netdata (live monitoring, Tailscale-bound) ---
echo "[12/17] Configuring Netdata..."
# Opt out of Netdata Cloud anonymous telemetry (sentinel file)
touch /etc/netdata/.opt-out-from-anonymous-statistics

# Bind Netdata to loopback + Tailscale IP only (never public)
NETDATA_CONF="/etc/netdata/netdata.conf"
TAILSCALE_IP4=$(tailscale ip -4 2>/dev/null | head -1 || echo "")
if [[ -n "$TAILSCALE_IP4" ]]; then
  BIND_LINE="bind socket to IP = 127.0.0.1 ${TAILSCALE_IP4}"
else
  # Fallback: loopback only if Tailscale isn't up yet (shouldn't happen at this point)
  BIND_LINE="bind socket to IP = 127.0.0.1"
fi

if grep -q "^\s*bind socket to IP" "$NETDATA_CONF" 2>/dev/null; then
  sed -i "s|^\s*bind socket to IP.*|\t${BIND_LINE}|" "$NETDATA_CONF"
else
  # Insert under [web] section, creating it if absent
  if grep -q "^\[web\]" "$NETDATA_CONF" 2>/dev/null; then
    sed -i "/^\[web\]/a\\\t${BIND_LINE}" "$NETDATA_CONF"
  else
    printf '\n[web]\n\t%s\n' "$BIND_LINE" >> "$NETDATA_CONF"
  fi
fi
systemctl enable --now netdata
systemctl restart netdata
echo "Netdata bound to 127.0.0.1 ${TAILSCALE_IP4:-} — dashboard at http://${TAILSCALE_IP4:-<tailscale-ip>}:19999"
echo "  NOTE: use the IP, not the hostname. The v2 SPA half-renders on hostname URLs."
echo ""

# --- journald cap (disk hygiene) ---
echo "[13/17] Capping journald..."
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/size-limit.conf << 'JOURNALD'
[Journal]
SystemMaxUse=500M
SystemKeepFree=1G
SystemMaxFileSize=50M
MaxRetentionSec=2month
JOURNALD
systemctl restart systemd-journald
echo "journald capped: 500M max, 2-month retention, 50M/file."
echo ""

# --- Vault directory ---
echo "[14/17] Setting up vault directory..."
mkdir -p "$VAULT_PATH"
chown ${USERNAME}:${USERNAME} "$VAULT_PATH"
echo "Vault path: ${VAULT_PATH}"
echo ""

# --- Systemd service for ob sync ---
echo "[15/17] Creating ob-sync systemd service..."
cat > /etc/systemd/system/ob-sync.service << UNIT
[Unit]
Description=Obsidian Headless Sync (continuous)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USERNAME}
Environment=HOME=/home/${USERNAME}
Environment=PATH=/home/${USERNAME}/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/home/${USERNAME}/.npm-global/bin/ob sync --continuous --path /home/${USERNAME}/vault
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
# Don't enable yet — ob login hasn't happened
echo "ob-sync service created (not yet enabled — run ob login first)."
echo ""

# --- Step 16: (deprecated) moshi-notify system template ---
echo "[16/17] moshi-notify deprecated — skipping (push API removed 2026-05-03)."
echo ""

# --- User environment setup ---
echo "[17/17] Setting up user environment..."

# Create .env.sh template
ENV_FILE="/home/${USERNAME}/.env.sh"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" << 'ENV'
# Secrets for Claude Code, Todoist, Readwise, etc.
# Fill these in after setup, then: source ~/.env.sh

export ANTHROPIC_API_KEY=""
export TODOIST_API_TOKEN=""
export OPENAI_API_KEY=""
export READWISE_TOKEN=""
export OBSIDIAN_AUTH_TOKEN=""
export HETZNER_API_TOKEN=""
ENV
  chmod 600 "$ENV_FILE"
  chown ${USERNAME}:${USERNAME} "$ENV_FILE"
  echo "Created ${ENV_FILE} (fill in your keys)."
else
  echo "${ENV_FILE} already exists, not overwriting."
fi

# Create wrapper scripts directory
WRAPPER_DIR="/home/${USERNAME}/.local/bin"
mkdir -p "$WRAPPER_DIR"

# Todoist wrapper (sources .env.sh itself — no need for bashrc auto-source)
cat > "${WRAPPER_DIR}/todoist" << 'WRAPPER'
#!/usr/bin/env bash
source ~/.env.sh
python3 ~/vault/3\ Information/Scripts/todoist.py "$@"
WRAPPER
chmod +x "${WRAPPER_DIR}/todoist"

# send-to-readwise wrapper
cat > "${WRAPPER_DIR}/send-to-readwise" << 'WRAPPER'
#!/usr/bin/env bash
source ~/.env.sh
python3 ~/vault/3\ Information/Scripts/send_to_readwise.py "$@"
WRAPPER
chmod +x "${WRAPPER_DIR}/send-to-readwise"

# Claude Code launcher (sources .env.sh before starting)
cat > "${WRAPPER_DIR}/claude-start" << 'WRAPPER'
#!/usr/bin/env bash
source ~/.env.sh
cd ~/vault
claude "$@"
WRAPPER
chmod +x "${WRAPPER_DIR}/claude-start"

chown -R ${USERNAME}:${USERNAME} "/home/${USERNAME}/.local"

# Add bun, npm-global, and .local/bin to PATH (but NOT auto-sourcing secrets into every shell)
BASHRC="/home/${USERNAME}/.bashrc"
if ! grep -q '.npm-global/bin' "$BASHRC" 2>/dev/null; then
  echo 'export PATH="$HOME/.bun/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"' >> "$BASHRC"
fi

# tmux config for a nicer experience
TMUX_CONF="/home/${USERNAME}/.tmux.conf"
if [[ ! -f "$TMUX_CONF" ]]; then
  cat > "$TMUX_CONF" << 'TMUX'
set -g mouse on
set -g history-limit 50000
set -g default-terminal "screen-256color"
set -s escape-time 0
TMUX
  chown ${USERNAME}:${USERNAME} "$TMUX_CONF"
fi

chown ${USERNAME}:${USERNAME} "$BASHRC"
echo "User environment configured."
echo ""

# --- Unattended upgrades ---
echo "Configuring unattended security upgrades..."
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
APT
echo "Unattended upgrades enabled."
echo ""

# --- Copy verify script ---
if [[ -f /root/verify-vps.sh ]]; then
  cp /root/verify-vps.sh /home/${USERNAME}/verify-vps.sh
  chmod +x /home/${USERNAME}/verify-vps.sh
  chown ${USERNAME}:${USERNAME} /home/${USERNAME}/verify-vps.sh
fi

# --- Done ---
echo ""
echo "=========================================="
echo "  Setup complete!"
echo "=========================================="
echo ""
echo "  Tailscale hostname: ${TS_DNS_NAME}"
echo "  Tailscale IP:       ${TAILSCALE_IP}"
echo "  Vault path:         ${VAULT_PATH}"
echo "  Setup log:          ${LOG_FILE}"
echo ""
echo "  SECURITY STATUS:"
echo "    UFW: SSH restricted to Tailscale subnet (100.64.0.0/10)"
echo "    SSH: key-only auth, root login disabled, fail2ban active"
echo "    Hetzner FW: keep the SSH rule as defense-in-depth"
echo ""
echo "  NEXT STEPS (as user '${USERNAME}'):"
echo ""
echo "  1. Reconnect via Tailscale:"
echo "     ssh kyle@${TS_DNS_NAME}"
echo ""
echo "  2. Fill in your secrets:"
echo "     nano ~/.env.sh"
echo ""
echo "  3. Log into Obsidian:"
echo "     ob login"
echo ""
echo "  4. Set up vault sync:"
echo "     ob sync-setup --vault everything --path ~/vault --device-name hetzner-vps"
echo ""
echo "  5. Enable all file types and config sync (appearance, snippets via appearance-data):"
echo "     ob sync-config --path ~/vault --file-types \"image,audio,video,pdf,unsupported\" --configs \"appearance,appearance-data\""
echo ""
echo "  6. Test sync:"
echo "     ob sync --path ~/vault"
echo ""
echo "  7. Copy auth token to .env.sh:"
echo "     cat ~/.config/obsidian-headless/auth_token"
echo ""
echo "  8. Enable continuous sync:"
echo "     sudo systemctl enable ob-sync"
echo "     sudo systemctl start ob-sync"
echo ""
echo "  9. Verify everything:"
echo "     ~/verify-vps.sh"
echo ""
echo "  10. Start Claude Code:"
echo "      tmux new -s claude"
echo "      claude-start"
echo ""
echo "  Root login is now disabled. Use 'kyle' user only."
echo ""
