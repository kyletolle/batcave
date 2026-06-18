#!/usr/bin/env bash
# bootstrap-vps.sh — Post-setup configuration for the knowledge garden environment
#
# Run AFTER setup-vps.sh and Obsidian Sync are working. This script installs
# and configures everything beyond the base OS that makes bruce-vps a fully
# functional knowledge garden workstation.
#
# Prerequisites:
#   - setup-vps.sh completed successfully
#   - Obsidian Sync running (vault files present at ~/vault)
#   - ~/.env.sh populated with API keys
#   - Claude Code installed and authenticated
#
# What it does:
#   1. Installs Bun (for QMD)
#   2. Installs QMD search engine
#   3. Creates CLI wrapper symlinks (todoist, send-to-readwise, etc.)
#   4. Sets up cron jobs (ccusage daily log, disk-watch)
#   5. Configures Claude Code (settings, commands symlink, plugins)
#   6. Patches QMD reranker for CPU-only systems (issue #68)
#   7. Sets up QMD systemd services (MCP daemon + embed timer)
#   8. (deprecated) moshi-notify user template — skipped; broken API removed 2026-05-03
#   9. Installs moshi-hook (Moshi iOS agent daemon via Linuxbrew)
#  10. Copies memory and session logs from old server (if provided)
#
# Usage:
#   bash "$HOME/projects/batcave/vps/bootstrap-vps.sh"
#   bash "$HOME/projects/batcave/vps/bootstrap-vps.sh" --from bruce-vps-old   # migrate from old server
#
# This script is idempotent — safe to re-run.
#
# UNEXERCISED (2026-04-17): Step 4's disk-watch cron line was ported from
# faramir's live config but has not yet been run end-to-end on a fresh provision.
# Step 8 (moshi-notify) is deprecated and skipped. Next real spin-up is the validation.

set -euo pipefail

VAULT_DIR="$HOME/vault"
SCRIPT_DIR="$VAULT_DIR/3 Information/Scripts"
REPO_DIR="$HOME/projects/batcave"   # clone of github.com/kyletolle/batcave
WRAPPER_DIR="$HOME/.local/bin"
CLAUDE_DIR="$HOME/.claude"
CLAUDE_PROJECT_DIR="$CLAUDE_DIR/projects/-home-kyle-vault"
OLD_HOST=""

# --- Parse flags ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --from) OLD_HOST="$2"; shift 2 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

echo "=== Knowledge Garden Bootstrap ==="
echo ""

# --- Preflight checks ---
if [[ ! -d "$VAULT_DIR" ]]; then
  echo "ERROR: Vault not found at $VAULT_DIR. Run setup-vps.sh and Obsidian Sync first."
  exit 1
fi

if [[ ! -f "$HOME/.env.sh" ]]; then
  echo "ERROR: ~/.env.sh not found. Create it with your API keys first."
  exit 1
fi

source "$HOME/.env.sh"

# --- Step 0: Install Homebrew ---
echo "[0/11] Installing Homebrew..."
if command -v brew &>/dev/null || [[ -x /home/linuxbrew/.linuxbrew/bin/brew ]]; then
  eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
  echo "Homebrew already installed: $(brew --version | head -1)"
else
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
  echo "Homebrew installed: $(brew --version | head -1)"
fi
# Ensure brew shellenv is in .bashrc
BASHRC="$HOME/.bashrc"
if ! grep -q 'brew shellenv' "$BASHRC" 2>/dev/null; then
  # Insert before the existing PATH export so brew paths are available
  sed -i '/export PATH=.*\.bun\/bin/i eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"' "$BASHRC"
  echo "brew shellenv added to .bashrc"
fi

# Install hcloud CLI (Hetzner management)
if command -v hcloud &>/dev/null; then
  echo "hcloud already installed."
else
  brew install hcloud
  echo "hcloud installed."
fi

# Install PockeTmux
if command -v pmux &>/dev/null; then
  echo "PockeTmux already installed."
else
  brew install shiftinbits/tap/pmux
  echo "PockeTmux installed."
fi
echo ""

# --- Step 1: Install Bun ---
echo "[1/11] Installing Bun..."
if command -v bun &>/dev/null; then
  echo "Bun already installed: $(bun --version)"
else
  curl -fsSL https://bun.sh/install | bash
  export PATH="$HOME/.bun/bin:$PATH"
  echo "Bun installed: $(bun --version)"
fi
# Ensure bun is on PATH for this session
export PATH="$HOME/.bun/bin:$PATH"
echo ""

# --- Step 2: Install QMD ---
echo "[2/11] Installing QMD..."
if command -v qmd &>/dev/null; then
  echo "QMD already installed: $(qmd --version 2>/dev/null || echo 'version unknown')"
else
  bun install -g @tobilu/qmd
  echo "QMD installed."
fi
echo ""

# --- Step 2b: Install ccusage (npm global) ---
echo "[2b/11] Installing ccusage..."
if npm list -g ccusage 2>/dev/null | grep -q ccusage; then
  echo "ccusage already installed."
else
  npm install -g ccusage
  echo "ccusage installed."
fi
echo ""

# --- Step 3: CLI wrapper symlinks ---
echo "[3/11] Setting up CLI wrapper symlinks..."
mkdir -p "$WRAPPER_DIR"

# Map of wrapper name → script path (relative to the batcave repo)
declare -A WRAPPERS=(
  [todoist]="bin/todoist"
  [send-to-readwise]="bin/send-to-readwise"
  [claude-start]="bin/claude-start"
  [extract-session]="bin/extract-session"
  [llm-panel]="bin/llm-panel"
  [qmd-receive]="bin/qmd-receive"
  [qmd-update]="bin/qmd-update"
  [tts]="scripts/tts.sh"
  [read-aloud]="bin/read-aloud"
  [daily-read-aloud]="bin/daily-read-aloud"
  [batspeaker]="bin/batspeaker"
  [ccusage-log]="vps/ccusage_daily_log.sh"
)

for name in "${!WRAPPERS[@]}"; do
  target="$REPO_DIR/${WRAPPERS[$name]}"
  link="$WRAPPER_DIR/$name"
  if [[ -L "$link" ]]; then
    echo "  $name → already linked"
  elif [[ -f "$target" ]]; then
    ln -sf "$target" "$link"
    echo "  $name → linked"
  else
    echo "  $name → SKIPPED (target not found: $target)"
  fi
  # Obsidian Sync strips execute bits — ensure targets are executable
  [[ -f "$target" ]] && chmod +x "$target"
done
echo ""

# --- Step 4: Cron jobs ---
echo "[4/11] Setting up cron jobs..."
CCUSAGE_LINE='0 1 * * * /bin/bash "$HOME/projects/batcave/vps/ccusage_daily_log.sh" >> "$HOME/vault/3 Information/Scripts/ccusage_cron.log" 2>&1'
DISK_WATCH_LINE='30 8 * * * /bin/bash "$HOME/projects/batcave/vps/disk-watch.sh"'

if crontab -l 2>/dev/null | grep -q "ccusage_daily_log"; then
  echo "ccusage cron job already exists."
else
  (crontab -l 2>/dev/null; echo "$CCUSAGE_LINE") | crontab -
  echo "ccusage daily log cron job installed (runs at 1:00 AM)."
fi

if crontab -l 2>/dev/null | grep -q "disk-watch.sh"; then
  echo "disk-watch cron job already exists."
else
  (crontab -l 2>/dev/null; echo "$DISK_WATCH_LINE") | crontab -
  echo "disk-watch cron job installed (runs daily at 08:30, moshi-alerts >85% full)."
fi
echo ""

# --- Step 5: Claude Code configuration ---
echo "[5/11] Configuring Claude Code..."
mkdir -p "$CLAUDE_DIR"
mkdir -p "$CLAUDE_PROJECT_DIR/memory"

# Commands symlink (vault-level)
COMMANDS_LINK="$VAULT_DIR/.claude/commands"
COMMANDS_TARGET="../3 Information/Claude Code/commands"
if [[ -L "$COMMANDS_LINK" ]]; then
  echo "Commands symlink already exists."
elif [[ -d "$COMMANDS_LINK" ]]; then
  echo "WARNING: $COMMANDS_LINK is a directory, not a symlink. Remove it and re-run."
else
  mkdir -p "$VAULT_DIR/.claude"
  ln -sf "$COMMANDS_TARGET" "$COMMANDS_LINK"
  echo "Commands symlink created."
fi

# Statusline script
STATUSLINE_SRC="$VAULT_DIR/3 Information/Scripts/statusline-command.sh"
STATUSLINE_DST="$CLAUDE_DIR/statusline-command.sh"
if [[ -f "$STATUSLINE_SRC" ]]; then
  cp "$STATUSLINE_SRC" "$STATUSLINE_DST"
  chmod +x "$STATUSLINE_DST"
  echo "Statusline script installed."
elif [[ -f "$STATUSLINE_DST" ]]; then
  echo "Statusline script already exists (no vault source found)."
else
  echo "WARNING: No statusline script found at $STATUSLINE_SRC — skipped."
fi

# Global settings.json — only copy if it doesn't exist (don't overwrite)
SETTINGS_SRC="$VAULT_DIR/3 Information/Claude Code/settings.json"
SETTINGS_DST="$CLAUDE_DIR/settings.json"
if [[ -f "$SETTINGS_DST" ]]; then
  echo "Global settings.json already exists (not overwriting)."
elif [[ -f "$SETTINGS_SRC" ]]; then
  cp "$SETTINGS_SRC" "$SETTINGS_DST"
  echo "Global settings.json copied from vault."
else
  echo "WARNING: No settings template found. Configure manually."
fi

# Project-level settings.local.json (auto-allow permissions for wrappers)
LOCAL_SETTINGS_SRC="$VAULT_DIR/3 Information/Claude Code/settings.local.json"
LOCAL_SETTINGS_DST="$CLAUDE_PROJECT_DIR/settings.local.json"
if [[ -f "$LOCAL_SETTINGS_DST" ]]; then
  echo "Project settings.local.json already exists (not overwriting)."
elif [[ -f "$LOCAL_SETTINGS_SRC" ]]; then
  cp "$LOCAL_SETTINGS_SRC" "$LOCAL_SETTINGS_DST"
  echo "Project settings.local.json copied (auto-allow: todoist, send-to-readwise, moshi-notify, etc.)."
else
  echo "WARNING: No project settings template found at $LOCAL_SETTINGS_SRC."
fi

echo ""

# --- Step 6: Patch QMD reranker for CPU-only systems ---
echo "[6/11] Patching QMD reranker (issue #68 workaround)..."
PATCH_SCRIPT="$REPO_DIR/scripts/patch-qmd-reranker.sh"
if [[ -f "$PATCH_SCRIPT" ]]; then
  bash "$PATCH_SCRIPT"
else
  echo "WARNING: patch-qmd-reranker.sh not found. QMD vector search may crash."
  echo "Run the patch manually after vault sync completes."
fi
echo ""

# --- Step 7: Set up QMD systemd services ---
echo "[7/11] Setting up QMD systemd services..."
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

# QMD MCP HTTP daemon (keeps models warm, serves search queries)
cat > "$SYSTEMD_USER_DIR/qmd-mcp.service" << 'UNIT'
[Unit]
Description=QMD MCP HTTP Server (semantic search daemon)
After=network.target ob-sync.service

[Service]
Type=simple
ExecStart=/home/kyle/.bun/bin/qmd mcp --http --port 8181
ExecStop=/home/kyle/.bun/bin/qmd mcp stop
Restart=on-failure
RestartSec=10
WorkingDirectory=/home/kyle/vault
Environment=HOME=/home/kyle
Environment=PATH=/home/kyle/.bun/bin:/home/kyle/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=NODE_LLAMA_CPP_GPU=false

[Install]
WantedBy=default.target
UNIT

# QMD embed service (re-indexes vault, runs on timer)
cat > "$SYSTEMD_USER_DIR/qmd-embed.service" << 'UNIT'
[Unit]
Description=QMD index update and embed
After=network.target ob-sync.service

[Service]
Type=oneshot
ExecStart=/home/kyle/.local/bin/qmd-update
WorkingDirectory=/home/kyle/vault
Environment=HOME=/home/kyle
Environment=PATH=/home/kyle/.bun/bin:/home/kyle/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=NODE_LLAMA_CPP_GPU=false
UNIT

# QMD embed timer (every 5 minutes)
cat > "$SYSTEMD_USER_DIR/qmd-embed.timer" << 'UNIT'
[Unit]
Description=Re-index and embed QMD collections every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT

# Bat-Speaker live-listen server (auto-TTS playback page over Tailscale).
# Canonical base unit. Any host-specific ExecStartPost/ExecStopPost hooks are
# layered on by local provisioning after bootstrap, not committed here.
cat > "$SYSTEMD_USER_DIR/batspeaker-serve.service" << 'UNIT'
[Unit]
Description=Bat-Speaker live listen server (auto-TTS playback page over Tailscale)
After=network.target

[Service]
Type=simple
ExecStart=/home/kyle/.local/bin/batspeaker serve --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=10
WorkingDirectory=/home/kyle/vault
Environment=HOME=/home/kyle
Environment=PATH=/home/kyle/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
# Enable but don't start until migration/sync is complete
systemctl --user enable qmd-mcp.service qmd-embed.timer batspeaker-serve.service 2>/dev/null || true
echo "QMD + batspeaker systemd services created and enabled."
echo "Start with: systemctl --user start qmd-mcp qmd-embed.timer batspeaker-serve"
echo ""

# --- Step 8: (deprecated) moshi-notify user template ---
echo "[8/11] moshi-notify deprecated — skipping (push API removed 2026-05-03)."
echo ""

# --- Step 9: Install moshi-hook (Moshi iOS agent daemon) ---
echo "[9/11] Installing moshi-hook (Moshi iOS agent daemon)..."
if ! command -v moshi-hook &>/dev/null; then
  brew tap rjyo/moshi
  brew install moshi-hook
  echo "moshi-hook installed."
else
  echo "moshi-hook already installed."
fi

# Start via systemd. brew services on Linux creates a systemd user service.
# Note: brew services warns when run inside tmux but installs correctly regardless.
if ! systemctl --user is-active --quiet homebrew.moshi-hook 2>/dev/null; then
  brew services start moshi-hook 2>/dev/null || true
fi

if moshi-hook status 2>/dev/null | grep -q "status:.*paired"; then
  moshi-hook install 2>/dev/null || true
  echo "moshi-hook paired — hooks written to ~/.claude/settings.json."
else
  echo ""
  echo "  ACTION REQUIRED after bootstrap: pair moshi-hook with the Moshi iOS app:"
  echo "    moshi-hook pair --token <YOUR_PAIRING_TOKEN>  # token from Moshi app Settings"
  echo "    moshi-hook install                             # writes hooks to ~/.claude/settings.json"
fi
echo ""

# --- Step 10: Migrate from old server (optional) ---
if [[ -n "$OLD_HOST" ]]; then
  echo "[10/11] Migrating from $OLD_HOST..."

  # Session logs (the big one — 87 files, ~97MB on current server)
  echo "  Copying Claude Code session logs..."
  ssh "$OLD_HOST" "ls ~/.claude/projects/-home-kyle-vault/*.jsonl 2>/dev/null | wc -l" && \
    rsync -avz --progress \
      "$OLD_HOST:~/.claude/projects/-home-kyle-vault/*.jsonl" \
      "$CLAUDE_PROJECT_DIR/" 2>/dev/null || \
    echo "  WARNING: Could not copy session logs. Try manually with rsync."

  # Memory files
  echo "  Copying Claude Code memory..."
  rsync -avz --progress \
    "$OLD_HOST:~/.claude/projects/-home-kyle-vault/memory/" \
    "$CLAUDE_PROJECT_DIR/memory/" 2>/dev/null || \
    echo "  WARNING: Could not copy memory files. Try manually."

  # Global settings (only if we don't have one)
  if [[ ! -f "$SETTINGS_DST" ]]; then
    echo "  Copying global settings.json..."
    scp "$OLD_HOST:~/.claude/settings.json" "$SETTINGS_DST" 2>/dev/null || \
      echo "  WARNING: Could not copy settings.json."
  fi

  # Statusline (if we didn't get it from vault)
  if [[ ! -f "$STATUSLINE_DST" ]]; then
    echo "  Copying statusline script..."
    scp "$OLD_HOST:~/.claude/statusline-command.sh" "$STATUSLINE_DST" 2>/dev/null || true
  fi

  # Todoist audit log (append-only, important for debugging)
  AUDIT_LOG="$VAULT_DIR/3 Information/Scripts/todoist_audit.jsonl"
  if [[ ! -f "$AUDIT_LOG" ]]; then
    echo "  Copying Todoist audit log..."
    scp "$OLD_HOST:~/vault/3 Information/Scripts/todoist_audit.jsonl" "$AUDIT_LOG" 2>/dev/null || \
      echo "  No audit log found on old server (that's fine)."
  fi

  # .env.sh (secrets — the most important file)
  echo "  Copying .env.sh..."
  scp "$OLD_HOST:~/.env.sh" "$HOME/.env.sh" 2>/dev/null && \
    chmod 600 "$HOME/.env.sh" && \
    echo "  .env.sh copied (chmod 600)." || \
    echo "  WARNING: Could not copy .env.sh. You'll need to fill it in manually."

  # .bashrc customizations (PATH additions, aliases, etc.)
  echo "  Copying .bashrc..."
  scp "$OLD_HOST:~/.bashrc" "$HOME/.bashrc" 2>/dev/null && \
    echo "  .bashrc copied." || \
    echo "  WARNING: Could not copy .bashrc. PATH may need manual setup."

  # .tmux.conf
  echo "  Copying .tmux.conf..."
  scp "$OLD_HOST:~/.tmux.conf" "$HOME/.tmux.conf" 2>/dev/null || true

  # Claude Code OAuth credentials (avoids re-authentication)
  CREDS_FILE="$CLAUDE_DIR/.credentials.json"
  if [[ ! -f "$CREDS_FILE" ]]; then
    echo "  Copying Claude Code OAuth credentials..."
    scp "$OLD_HOST:~/.claude/.credentials.json" "$CREDS_FILE" 2>/dev/null && \
      chmod 600 "$CREDS_FILE" && \
      echo "  Credentials copied (chmod 600). May still need to re-auth if token expired." || \
      echo "  No credentials found — you'll need to run 'claude' and log in."
  else
    echo "  Claude Code credentials already exist."
  fi

  # Obsidian headless auth token (avoids re-running ob login)
  OB_AUTH_DIR="$HOME/.config/obsidian-headless"
  if [[ ! -f "$OB_AUTH_DIR/auth_token" ]]; then
    echo "  Copying Obsidian auth token..."
    mkdir -p "$OB_AUTH_DIR"
    scp "$OLD_HOST:~/.config/obsidian-headless/auth_token" "$OB_AUTH_DIR/auth_token" 2>/dev/null && \
      chmod 600 "$OB_AUTH_DIR/auth_token" && \
      echo "  Auth token copied. Still need to run ob sync-setup for vault binding." || \
      echo "  No auth token found — you'll need to run 'ob login'."
  else
    echo "  Obsidian auth token already exists."
  fi

  # QMD config (if any local settings exist)
  QMD_CONFIG_DIR="$HOME/.config/qmd"
  if ssh "$OLD_HOST" "test -d ~/.config/qmd" 2>/dev/null; then
    echo "  Copying QMD config..."
    mkdir -p "$QMD_CONFIG_DIR"
    rsync -avz "$OLD_HOST:~/.config/qmd/" "$QMD_CONFIG_DIR/" 2>/dev/null || true
  fi

  # QMD SQLite index + embedding models (~2.3GB) — CRITICAL
  # Embeddings require a GPU to generate. The VPS has no GPU, so this index
  # cannot be rebuilt on-server. Must be copied from old server or generated
  # on a machine with a GPU and transferred.
  QMD_CACHE_DIR="$HOME/.cache/qmd"
  echo "  Copying QMD index and embedding models (this is large, ~2.3GB)..."
  mkdir -p "$QMD_CACHE_DIR"
  rsync -avz --progress \
    "$OLD_HOST:~/.cache/qmd/" \
    "$QMD_CACHE_DIR/" 2>/dev/null && \
    echo "  QMD index copied." || \
    echo "  WARNING: Could not copy QMD index. Without this, search will not work (no GPU to regenerate embeddings)."

  echo "  Migration complete."
else
  echo "[10/11] No --from flag — skipping migration."
fi
echo ""

# --- Step 9: Verify ---
echo "[11/11] Verifying bootstrap..."
echo ""

ERRORS=0

check() {
  local desc="$1" cmd="$2"
  if eval "$cmd" &>/dev/null; then
    echo "  ✓ $desc"
  else
    echo "  ✗ $desc"
    ERRORS=$((ERRORS + 1))
  fi
}

check "Homebrew installed" "command -v brew"
check "hcloud installed" "command -v hcloud"
check "PockeTmux installed" "command -v pmux"
check "brew shellenv in .bashrc" "grep -q 'brew shellenv' $HOME/.bashrc"
check "Bun installed" "command -v bun"
check "QMD installed" "command -v qmd"
check "todoist wrapper" "test -L $WRAPPER_DIR/todoist"
check "send-to-readwise wrapper" "test -L $WRAPPER_DIR/send-to-readwise"
check "claude-start wrapper" "test -L $WRAPPER_DIR/claude-start"
check "extract-session wrapper" "test -L $WRAPPER_DIR/extract-session"
check "llm-panel wrapper" "test -L $WRAPPER_DIR/llm-panel"
check "qmd-receive wrapper" "test -L $WRAPPER_DIR/qmd-receive"
check "qmd-update wrapper" "test -L $WRAPPER_DIR/qmd-update"
check "tts wrapper" "test -L $WRAPPER_DIR/tts"
check "read-aloud wrapper" "test -L $WRAPPER_DIR/read-aloud"
check "daily-read-aloud wrapper" "test -L $WRAPPER_DIR/daily-read-aloud"
check "batspeaker wrapper" "test -L $WRAPPER_DIR/batspeaker"
check "ccusage npm binary" "test -f \"\$(npm prefix -g)/bin/ccusage\""
check "ccusage-log wrapper" "test -L $WRAPPER_DIR/ccusage-log"
check "Commands symlink" "test -L $VAULT_DIR/.claude/commands"
check "QMD reranker patched" "grep -q 'rerank: false' \"\$(find ~/.bun ~/.npm-global -path '*/qmd/dist/mcp/server.js' 2>/dev/null | head -1)\" 2>/dev/null"
check "QMD MCP service" "test -f $HOME/.config/systemd/user/qmd-mcp.service"
check "QMD embed timer" "test -f $HOME/.config/systemd/user/qmd-embed.timer"
check "batspeaker-serve service" "test -f $HOME/.config/systemd/user/batspeaker-serve.service"
check "ccusage cron job" "crontab -l 2>/dev/null | grep -q ccusage_daily_log"
check "disk-watch cron job" "crontab -l 2>/dev/null | grep -q disk-watch"
check "moshi-hook installed" "command -v moshi-hook"
check "moshi-hook service running" "systemctl --user is-active --quiet homebrew.moshi-hook 2>/dev/null"
check "earlyoom active" "systemctl is-active --quiet earlyoom"
check "netdata active" "systemctl is-active --quiet netdata"
check "swap file active" "swapon --show | grep -q /swapfile"
check "Claude Code memory dir" "test -d $CLAUDE_PROJECT_DIR/memory"
check "Project settings.local.json" "test -f $CLAUDE_PROJECT_DIR/settings.local.json"
check "TODOIST_API_TOKEN set" "test -n \"\${TODOIST_API_TOKEN:-}\""
check "READWISE_TOKEN set" "test -n \"\${READWISE_TOKEN:-}\""

echo ""
if [[ $ERRORS -eq 0 ]]; then
  echo "All checks passed!"
else
  echo "$ERRORS check(s) failed. Review above and fix manually."
fi

echo ""
echo "=========================================="
echo "  Bootstrap complete!"
echo "=========================================="
echo ""
echo "  Next steps:"
echo "    1. Start Claude Code: tmux new -s claude && claude-start"
echo "    2. Verify QMD: qmd status"
echo "    3. Run ccusage backfill: bash \"$HOME/projects/batcave/vps/ccusage_daily_log.sh\" --backfill"
echo "    4. Pair moshi-hook (if not already paired):"
echo "         moshi-hook pair --token <YOUR_TOKEN>  # token from Moshi app Settings"
echo "         moshi-hook install"
echo "    5. Reconnect claude.ai MCP connectors (Gmail, Google Calendar, Readwise):"
echo "       - Go to https://claude.ai/settings/connectors and verify they're connected"
echo "       - Run /mcp in Claude Code to confirm they appear"
echo "       - These are cloud-based (OAuth via claude.ai), NOT local config — they won't"
echo "         migrate with settings files. Must be reconnected per-environment."
echo ""
