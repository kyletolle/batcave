#!/usr/bin/env bash
# patch-qmd-reranker.sh — Disable QMD reranker for CPU-only systems
#
# QMD issue #68: the reranker crashes with "Object is disposed" errors on
# CPU-only systems due to a race condition when loading models concurrently
# under Bun. This patch adds `rerank: false` to both store.search() call
# sites in the MCP server, bypassing the reranker entirely.
#
# BM25 and vector search still work. Results just skip the reranking step.
#
# Run this after `bun install -g @tobilu/qmd` or any QMD upgrade.
# Remove this script entirely when upstream fixes issue #68.
#
# Usage: bash "~/projects/batcave/scripts/patch-qmd-reranker.sh"

set -euo pipefail

# Find QMD's MCP server file
QMD_MCP=""
for candidate in \
  "$HOME/.bun/install/global/node_modules/@tobilu/qmd/dist/mcp/server.js" \
  "$HOME/.npm-global/lib/node_modules/@tobilu/qmd/dist/mcp/server.js"; do
  if [[ -f "$candidate" ]]; then
    QMD_MCP="$candidate"
    break
  fi
done

if [[ -z "$QMD_MCP" ]]; then
  echo "ERROR: Could not find QMD MCP server.js. Is QMD installed?"
  exit 1
fi

echo "Patching: $QMD_MCP"

# Check if already patched
if grep -q "rerank: false" "$QMD_MCP"; then
  echo "Already patched (rerank: false found). Nothing to do."
  exit 0
fi

# Back up the original
cp "$QMD_MCP" "${QMD_MCP}.bak"
echo "Backup saved to ${QMD_MCP}.bak"

# Patch: add `rerank: false,` after each `intent,` or `intent:` line
# inside store.search() blocks. The pattern is:
#   intent,          (first call site)
#   intent: params.intent,  (second call site)
# We add `rerank: false,` on the next line after each.
sed -i '/store\.search({/,/});/ {
  /intent[,:]/a\            rerank: false,
}' "$QMD_MCP"

# Verify the patch applied
PATCH_COUNT=$(grep -c "rerank: false" "$QMD_MCP")
if [[ "$PATCH_COUNT" -eq 2 ]]; then
  echo "Patch applied successfully ($PATCH_COUNT call sites patched)."
  echo "Restart qmd-mcp service: systemctl --user restart qmd-mcp"
else
  echo "WARNING: Expected 2 patched sites, found $PATCH_COUNT."
  echo "The QMD server.js may have changed. Inspect manually:"
  echo "  grep -n 'rerank' $QMD_MCP"
  echo "Restoring backup..."
  cp "${QMD_MCP}.bak" "$QMD_MCP"
  exit 1
fi
