#!/usr/bin/env bash
# provision-vps.sh — Create a Hetzner VPS from the command line
#
# Run this from your local machine (WSL). It will:
# 1. Install hcloud CLI if needed (with checksum verification)
# 2. Create/reuse an SSH key in Hetzner
# 3. Create a firewall with SSH access
# 4. Spin up the server
# 5. Copy the setup script to the server
# 6. Print next steps
#
# Usage: bash "vps/provision-vps.sh" [--location nbg1] [--name bruce-vps] [--tailscale-key tskey-auth-...]

set -euo pipefail

# --- Configuration (override with flags or env vars) ---
SERVER_NAME="${VPS_NAME:-bruce-vps}"
SERVER_TYPE="${VPS_TYPE:-}"  # auto-detected below
IMAGE="ubuntu-24.04"
LOCATION="${VPS_LOCATION:-nbg1}"
SSH_KEY_NAME="kyle-vps"
SSH_KEY_FILE="${HOME}/.ssh/id_ed25519.pub"
FIREWALL_NAME="bruce-fw"
TAILSCALE_KEY=""
MIGRATE_FROM=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Parse flags ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --location) LOCATION="$2"; shift 2 ;;
    --name) SERVER_NAME="$2"; shift 2 ;;
    --type) SERVER_TYPE="$2"; shift 2 ;;
    --ssh-key) SSH_KEY_FILE="$2"; shift 2 ;;
    --tailscale-key) TAILSCALE_KEY="$2"; shift 2 ;;
    --from) MIGRATE_FROM="$2"; shift 2 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

echo "=== Hetzner VPS Provisioning ==="
echo ""

# --- Step 1: Check for hcloud CLI ---
if ! command -v hcloud &>/dev/null; then
  echo "hcloud CLI not found. Installing..."
  HCLOUD_TMP=$(mktemp -d)
  cd "$HCLOUD_TMP"
  curl -sSLO https://github.com/hetznercloud/cli/releases/latest/download/hcloud-linux-amd64.tar.gz
  curl -sSLO https://github.com/hetznercloud/cli/releases/latest/download/checksums.txt
  # Verify checksum before installing
  if sha256sum -c checksums.txt --ignore-missing 2>/dev/null | grep -q "OK"; then
    echo "Checksum verified."
  else
    echo "WARNING: Checksum verification failed or checksums.txt not available."
    echo "The downloaded binary may not be authentic."
    read -rp "Continue anyway? (y/N) " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
      rm -rf "$HCLOUD_TMP"
      exit 1
    fi
  fi
  sudo tar -C /usr/local/bin --no-same-owner -xzf hcloud-linux-amd64.tar.gz hcloud
  cd - >/dev/null
  rm -rf "$HCLOUD_TMP"
  echo "hcloud installed: $(hcloud version)"
else
  echo "hcloud found: $(hcloud version)"
fi

# --- Step 2: Authenticate with Hetzner API ---
# hcloud supports two auth methods:
#   1. HCLOUD_TOKEN env var (used directly for all API calls, no stored context needed)
#   2. Stored context via `hcloud context create` (prompts interactively for token)
# We prefer the env var when available — no interactive prompt, no stored credentials.

if [[ -n "${HETZNER_API_TOKEN:-}" ]]; then
  export HCLOUD_TOKEN="$HETZNER_API_TOKEN"
  echo "Using HETZNER_API_TOKEN from environment."
elif [[ -n "${HCLOUD_TOKEN:-}" ]]; then
  echo "Using existing HCLOUD_TOKEN from environment."
elif hcloud context active &>/dev/null 2>&1; then
  echo "Using existing hcloud context."
else
  echo ""
  echo "No Hetzner API token found. You need one to continue."
  echo ""
  echo "Option A (recommended): Add to ~/.env.sh and source it:"
  echo "  export HETZNER_API_TOKEN=\"your-token-here\""
  echo "  source ~/.env.sh"
  echo ""
  echo "Option B: Create a stored hcloud context:"
  echo "  hcloud context create bruce"
  echo ""
  echo "Get a token from: https://console.hetzner.cloud/ → Your Project → Security → API Tokens"
  echo "(Create a token with Read & Write permissions)"
  exit 1
fi

# Verify connection
echo "Verifying Hetzner API connection..."
if ! hcloud datacenter list &>/dev/null; then
  echo ""
  echo "ERROR: Could not connect to Hetzner API."
  echo "Your token may be invalid, expired, or lack Read & Write permissions."
  echo "Generate a new one at: https://console.hetzner.cloud/ → Security → API Tokens"
  exit 1
fi
echo "Connected."
echo ""

# --- Auto-detect server type if not specified ---
if [[ -z "$SERVER_TYPE" ]]; then
  echo "Detecting cheapest available server type for location '${LOCATION}'..."
  # EU locations (nbg1, fsn1, hel1) use cx-series shared vCPU
  # US locations (ash, hil) use ccx-series dedicated vCPU (shared not available)
  case "$LOCATION" in
    ash|hil)
      # US: cheapest is ccx13 (2 vCPU, 8GB RAM, $14.49/mo + $0.60 IPv4)
      if hcloud server-type describe ccx13 &>/dev/null 2>&1; then
        SERVER_TYPE="ccx13"
      else
        echo "Could not find ccx13. Available types for ${LOCATION}:"
        hcloud server-type list --sort name | head -10
        read -rp "Enter server type: " SERVER_TYPE
      fi
      ;;
    *)
      # EU: cheapest is cx23 (2 vCPU, 4GB RAM, €4.99/mo) or cx22 (legacy)
      if hcloud server-type describe cx23 &>/dev/null 2>&1; then
        SERVER_TYPE="cx23"
      elif hcloud server-type describe cx22 &>/dev/null 2>&1; then
        SERVER_TYPE="cx22"
      else
        echo "Could not auto-detect. Available shared types:"
        hcloud server-type list | grep -i "cx\|shared" | head -5
        read -rp "Enter server type: " SERVER_TYPE
      fi
      ;;
  esac
  echo "Using server type: ${SERVER_TYPE}"
fi

echo "Server: ${SERVER_NAME} (${SERVER_TYPE}) in ${LOCATION}"
echo ""

# --- Step 3: SSH key ---
if ! [[ -f "$SSH_KEY_FILE" ]]; then
  echo "SSH public key not found at ${SSH_KEY_FILE}"
  echo "Generate one with: ssh-keygen -t ed25519"
  exit 1
fi

if hcloud ssh-key describe "$SSH_KEY_NAME" &>/dev/null 2>&1; then
  echo "SSH key '${SSH_KEY_NAME}' already exists in Hetzner."
else
  echo "Uploading SSH key '${SSH_KEY_NAME}' to Hetzner..."
  hcloud ssh-key create --name "$SSH_KEY_NAME" --public-key-from-file "$SSH_KEY_FILE"
fi
echo ""

# --- Step 4: Firewall ---
if hcloud firewall describe "$FIREWALL_NAME" &>/dev/null 2>&1; then
  echo "Firewall '${FIREWALL_NAME}' already exists."
else
  echo "Creating firewall '${FIREWALL_NAME}'..."
  hcloud firewall create --name "$FIREWALL_NAME"

  # Allow SSH — restricted to current IP only (temporary — remove after Tailscale setup)
  echo "Detecting your public IP for SSH firewall rule..."
  MY_IP=$(curl -s --max-time 5 ifconfig.me || curl -s --max-time 5 icanhazip.com || echo "")
  if [[ -n "$MY_IP" ]]; then
    echo "Your IP: ${MY_IP}"
    hcloud firewall add-rule "$FIREWALL_NAME" \
      --direction in --protocol tcp --port 22 \
      --source-ips "${MY_IP}/32" \
      --description "SSH (temporary — ${MY_IP} only, remove after Tailscale setup)"
  else
    echo "WARNING: Could not detect public IP. Falling back to open SSH access."
    echo "Lock this down ASAP after Tailscale is configured."
    hcloud firewall add-rule "$FIREWALL_NAME" \
      --direction in --protocol tcp --port 22 \
      --source-ips 0.0.0.0/0 --source-ips ::/0 \
      --description "SSH (temporary — remove after Tailscale setup)"
  fi

  # Allow ICMP for diagnostics
  hcloud firewall add-rule "$FIREWALL_NAME" \
    --direction in --protocol icmp \
    --source-ips 0.0.0.0/0 --source-ips ::/0 \
    --description "ICMP (ping)"

  # Allow Tailscale UDP (WireGuard)
  hcloud firewall add-rule "$FIREWALL_NAME" \
    --direction in --protocol udp --port 41641 \
    --source-ips 0.0.0.0/0 --source-ips ::/0 \
    --description "Tailscale WireGuard"
fi
echo ""

# --- Step 5: Check if server already exists ---
if hcloud server describe "$SERVER_NAME" &>/dev/null 2>&1; then
  echo "Server '${SERVER_NAME}' already exists!"
  SERVER_IP=$(hcloud server ip "$SERVER_NAME")
  echo "IP: ${SERVER_IP}"
else
  echo "Creating server '${SERVER_NAME}'..."
  hcloud server create \
    --name "$SERVER_NAME" \
    --type "$SERVER_TYPE" \
    --image "$IMAGE" \
    --location "$LOCATION" \
    --ssh-key "$SSH_KEY_NAME" \
    --firewall "$FIREWALL_NAME"

  SERVER_IP=$(hcloud server ip "$SERVER_NAME")
  echo ""
  echo "Server created! IP: ${SERVER_IP}"
fi
echo ""

# --- Step 6: Wait for SSH to be ready ---
echo "Waiting for SSH to be ready..."
HOST_KEY_SHOWN=false
for i in $(seq 1 30); do
  if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 root@"$SERVER_IP" "echo ok" &>/dev/null 2>&1; then
    echo "SSH is ready."
    HOST_KEY_SHOWN=true
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "Timed out waiting for SSH. The server may still be booting."
    echo "Try manually: ssh root@${SERVER_IP}"
    exit 1
  fi
  sleep 2
done

# Show the host key fingerprint so the user can verify
echo ""
echo "Host key fingerprint for ${SERVER_IP}:"
ssh-keygen -lf <(ssh-keyscan -t ed25519 "$SERVER_IP" 2>/dev/null) 2>/dev/null || \
  echo "  (could not retrieve — verify manually with: ssh-keygen -lf <(ssh-keyscan ${SERVER_IP}))"
echo "If this doesn't match what you expect, Ctrl+C now and investigate."
echo ""

# --- Step 7: Copy scripts to server ---
echo "Copying setup scripts to server..."
scp -o StrictHostKeyChecking=no \
  "${SCRIPT_DIR}/setup-vps.sh" \
  "${SCRIPT_DIR}/verify-vps.sh" \
  root@"${SERVER_IP}":/root/

echo ""
echo "=========================================="
echo "  Server ready! Next steps:"
echo "=========================================="
echo ""
echo "  1. SSH into the server:"
echo "     ssh root@${SERVER_IP}"
echo ""
if [[ -n "$TAILSCALE_KEY" ]]; then
  echo "  2. Run the setup script (Tailscale will auto-auth):"
  echo "     chmod +x /root/setup-vps.sh /root/verify-vps.sh"
  echo "     /root/setup-vps.sh --tailscale-key ${TAILSCALE_KEY} --hostname ${SERVER_NAME}"
  echo ""
  echo "  3. After Tailscale connects, SSH will be locked to Tailscale only."
  echo "     Your current SSH session will survive, but if disconnected,"
  echo "     reconnect via Tailscale: ssh kyle@${SERVER_NAME}"
else
  echo "  2. Run the setup script:"
  echo "     chmod +x /root/setup-vps.sh /root/verify-vps.sh"
  echo "     /root/setup-vps.sh"
  echo ""
  echo "  3. During setup, you'll be asked to authenticate Tailscale"
  echo "     (open a URL in your browser)"
fi
echo ""
echo "  4. After setup completes, reconnect via Tailscale:"
echo "     ssh kyle@${SERVER_NAME}  (or the Tailscale hostname)"
echo ""
echo "  5. The Hetzner firewall SSH rule remains as defense-in-depth."
echo "     It restricts public SSH to ${MY_IP:-your IP} only."
echo "     UFW on the box restricts SSH to Tailscale subnet (100.64.0.0/10)."
echo "     Both layers must pass for an SSH connection to succeed."
echo ""
echo "  Server IP: ${SERVER_IP}"
echo "  Server name: ${SERVER_NAME}"
if [[ -n "$MIGRATE_FROM" ]]; then
  echo "  Migrating from: ${MIGRATE_FROM}"
fi
echo ""

# --- Step 8: Key exchange for inter-server SSH (if --from specified) ---
if [[ -n "$MIGRATE_FROM" ]]; then
  echo ""
  echo "=========================================="
  echo "  Post-setup: SSH key exchange"
  echo "=========================================="
  echo ""
  echo "  After setup completes on the new server, run this from WSL"
  echo "  to exchange bruce inter-server keys (enables bootstrap --from):"
  echo ""
  echo "  # Copy old server's bruce key to new server (shared identity)"
  echo "  scp ${MIGRATE_FROM}:~/.ssh/id_bruce kyle@${SERVER_NAME}:~/.ssh/id_bruce"
  echo "  scp ${MIGRATE_FROM}:~/.ssh/id_bruce.pub kyle@${SERVER_NAME}:~/.ssh/id_bruce.pub"
  echo ""
  echo "  # Add new server's pub key to old server's authorized_keys"
  echo "  ssh kyle@${SERVER_NAME} 'cat ~/.ssh/id_bruce.pub' | ssh ${MIGRATE_FROM} 'cat >> ~/.ssh/authorized_keys'"
  echo ""
  echo "  # Then on the new server, run bootstrap with migration:"
  echo "  ssh kyle@${SERVER_NAME}"
  echo "  bash ~/vault/3\\ Information/Scripts/bootstrap-vps.sh --from ${MIGRATE_FROM}"
  echo ""
fi
