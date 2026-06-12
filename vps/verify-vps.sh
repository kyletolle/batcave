#!/usr/bin/env bash
# verify-vps.sh — Check that everything is properly configured
#
# Run as kyle on the VPS after setup + interactive steps.
#
# Deliberately omits set -e: this is a diagnostic script that should run all
# checks even if individual ones fail. The ((errors++)) construct would also
# exit under -e when errors==0 in some bash versions.

set -uo pipefail

PASS="[OK]"
FAIL="[!!]"
WARN="[??]"
errors=0

echo "=== VPS Verification ==="
echo ""

# --- System ---
echo "--- System ---"
printf "  %-35s " "Ubuntu version"
if grep -q "24.04" /etc/os-release 2>/dev/null; then
  echo "$PASS $(lsb_release -d -s 2>/dev/null)"
else
  echo "$WARN $(lsb_release -d -s 2>/dev/null)"
fi

printf "  %-35s " "Disk usage"
DISK_PCT=$(df / --output=pcent | tail -1 | tr -d ' %')
if [[ $DISK_PCT -lt 80 ]]; then
  echo "$PASS ${DISK_PCT}% used"
else
  echo "$WARN ${DISK_PCT}% used"
fi

printf "  %-35s " "RAM available"
FREE_MB=$(free -m | awk '/Mem:/ {print $7}')
echo "$PASS ${FREE_MB}MB free"

# --- Node.js ---
echo ""
echo "--- Node.js ---"
printf "  %-35s " "Node.js version"
if command -v node &>/dev/null; then
  NODE_VER=$(node --version)
  NODE_MAJOR=$(echo "$NODE_VER" | cut -d. -f1 | tr -d v)
  if [[ $NODE_MAJOR -ge 22 ]]; then
    echo "$PASS $NODE_VER"
  else
    echo "$FAIL $NODE_VER (need 22+)"
    ((errors++))
  fi
else
  echo "$FAIL not installed"
  ((errors++))
fi

printf "  %-35s " "npm version"
if command -v npm &>/dev/null; then
  echo "$PASS $(npm --version)"
else
  echo "$FAIL not installed"
  ((errors++))
fi

# --- Key tools ---
echo ""
echo "--- Tools ---"
for tool in git tmux python3 jq curl ffmpeg; do
  printf "  %-35s " "$tool"
  if command -v "$tool" &>/dev/null; then
    echo "$PASS"
  else
    echo "$FAIL not installed"
    ((errors++))
  fi
done

printf "  %-35s " "obsidian-headless (ob)"
if command -v ob &>/dev/null; then
  echo "$PASS $(ob --version 2>/dev/null || echo 'installed')"
else
  echo "$FAIL not installed"
  ((errors++))
fi

printf "  %-35s " "Claude Code"
if command -v claude &>/dev/null; then
  echo "$PASS $(claude --version 2>/dev/null || echo 'installed')"
else
  echo "$FAIL not installed"
  ((errors++))
fi

# --- Tailscale ---
echo ""
echo "--- Tailscale ---"
printf "  %-35s " "Tailscale installed"
if command -v tailscale &>/dev/null; then
  echo "$PASS"
else
  echo "$FAIL"
  ((errors++))
fi

printf "  %-35s " "Tailscale connected"
if tailscale status &>/dev/null 2>&1; then
  TS_IP=$(tailscale ip -4 2>/dev/null || echo "?")
  echo "$PASS (IP: ${TS_IP})"
else
  echo "$FAIL not connected"
  ((errors++))
fi

# --- Services ---
echo ""
echo "--- Services ---"
# Ubuntu 24.04 uses 'ssh' as the service name (not 'sshd')
for svc in fail2ban ufw ssh tailscaled; do
  printf "  %-35s " "$svc"
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    echo "$PASS running"
  else
    echo "$FAIL not running"
    ((errors++))
  fi
done

printf "  %-35s " "ob-sync"
if systemctl is-active --quiet ob-sync 2>/dev/null; then
  echo "$PASS running"
elif systemctl is-enabled --quiet ob-sync 2>/dev/null; then
  echo "$WARN enabled but not running"
else
  echo "$WARN not enabled (run ob login first)"
fi

# --- SSH config ---
echo ""
echo "--- SSH Hardening ---"
printf "  %-35s " "Password auth disabled"
if grep -q "^PasswordAuthentication no" /etc/ssh/sshd_config 2>/dev/null; then
  echo "$PASS"
else
  echo "$FAIL"
  ((errors++))
fi

printf "  %-35s " "Root login disabled"
if grep -q "^PermitRootLogin no" /etc/ssh/sshd_config 2>/dev/null; then
  echo "$PASS"
else
  # Check for the safety fallback
  if grep -q "^PermitRootLogin prohibit-password" /etc/ssh/sshd_config 2>/dev/null; then
    echo "$WARN prohibit-password (key-only, not fully disabled)"
  else
    echo "$FAIL"
    ((errors++))
  fi
fi

printf "  %-35s " "SSH restricted to Tailscale"
if sudo ufw status 2>/dev/null | grep -q "100.64.0.0/10"; then
  echo "$PASS (UFW: Tailscale subnet only)"
elif sudo ufw status 2>/dev/null | grep "22/tcp" | grep -q "Anywhere"; then
  echo "$FAIL SSH open to 0.0.0.0/0 (should be 100.64.0.0/10)"
  ((errors++))
else
  echo "$WARN could not determine UFW SSH rule"
fi

printf "  %-35s " "fail2ban SSH jail"
if sudo fail2ban-client status sshd &>/dev/null 2>&1; then
  # Verify the jail watches the correct systemd unit (Ubuntu 24.04 = ssh.service, not sshd.service)
  JAIL_MATCH=$(sudo fail2ban-client get sshd journalmatch 2>/dev/null || true)
  if echo "$JAIL_MATCH" | grep -q "ssh\.service"; then
    echo "$PASS active"
  else
    echo "$WARN jail active but watching wrong unit (expected ssh.service, fix journalmatch in jail.local)"
  fi
else
  echo "$WARN not active"
fi

# --- Vault ---
echo ""
echo "--- Vault ---"
printf "  %-35s " "Vault directory exists"
if [[ -d "$HOME/vault" ]]; then
  FILE_COUNT=$(find "$HOME/vault" -type f 2>/dev/null | head -100 | wc -l)
  if [[ $FILE_COUNT -gt 0 ]]; then
    echo "$PASS (${FILE_COUNT}+ files)"
  else
    echo "$WARN exists but empty (sync not run yet?)"
  fi
else
  echo "$WARN not created (run ob sync-setup first)"
fi

printf "  %-35s " "CLAUDE.md present"
if [[ -f "$HOME/vault/CLAUDE.md" ]]; then
  echo "$PASS"
else
  echo "$WARN not found (vault may not be synced yet)"
fi

# --- Environment ---
echo ""
echo "--- Environment ---"
printf "  %-35s " ".env.sh exists"
if [[ -f "$HOME/.env.sh" ]]; then
  PERMS=$(stat -c %a "$HOME/.env.sh" 2>/dev/null)
  if [[ "$PERMS" == "600" ]]; then
    echo "$PASS (permissions: 600)"
  else
    echo "$WARN (permissions: ${PERMS}, should be 600)"
  fi
else
  echo "$FAIL"
  ((errors++))
fi

printf "  %-35s " "ANTHROPIC_API_KEY set"
source "$HOME/.env.sh" 2>/dev/null || true
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "$PASS (set)"
else
  echo "$WARN not set (fill in ~/.env.sh)"
fi

printf "  %-35s " "todoist wrapper"
if [[ -x "$HOME/.local/bin/todoist" ]]; then
  echo "$PASS"
else
  echo "$WARN missing"
fi

printf "  %-35s " "send-to-readwise wrapper"
if [[ -x "$HOME/.local/bin/send-to-readwise" ]]; then
  echo "$PASS"
else
  echo "$WARN missing"
fi

printf "  %-35s " "claude-start wrapper"
if [[ -x "$HOME/.local/bin/claude-start" ]]; then
  echo "$PASS"
else
  echo "$WARN missing"
fi

# --- Summary ---
echo ""
echo "=========================================="
if [[ $errors -eq 0 ]]; then
  echo "  All checks passed!"
else
  echo "  ${errors} issue(s) found. Review above."
fi
echo "=========================================="
