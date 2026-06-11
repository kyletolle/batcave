#!/usr/bin/env bash
set -euo pipefail
# qmd-sync — Full QMD embed-and-transfer pipeline (run from WSL/Dreadcore4)
#
# Embeds on local GPU, transfers to VPS, restarts services.
# Install: cp to ~/.local/bin/qmd-sync && chmod +x ~/.local/bin/qmd-sync
#
# Requires:
#   - qmd CLI with collections configured
#   - SSH key for bruce-vps (id_bruce_vps, no passphrase)
#   - qmd-receive on VPS at ~/.local/bin/qmd-receive

VPS="kyle@bruce-vps"
INDEX="$HOME/.cache/qmd/index.sqlite"

echo "=== QMD Sync: WSL → VPS ==="
echo ""

# 1. Update and embed locally (GPU)
echo "Step 1: Update + embed (local GPU)..."
cd ~/vault
qmd update
qmd embed
echo ""

# 2. Checkpoint WAL for clean transfer
echo "Step 2: WAL checkpoint..."
sqlite3 "$INDEX" "PRAGMA wal_checkpoint(TRUNCATE);"
echo ""

# 3. Prep VPS (stop services, clean old files)
echo "Step 3: Prep VPS (stopping services)..."
ssh "$VPS" "qmd-receive prep"
echo ""

# 4. Transfer
echo "Step 4: SCP index to VPS..."
scp "$INDEX" "${VPS}:${INDEX}"
echo ""

# 5. Finish VPS (clean WAL, restart, verify)
echo "Step 5: Finish VPS (restart + verify)..."
ssh "$VPS" "qmd-receive finish"
echo ""

echo "=== QMD Sync complete ==="
