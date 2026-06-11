#!/bin/bash
# Logs daily Claude Code usage (token counts + hypothetical API cost) to a vault note.
# Supports both VPS and WSL environments with per-source tracking.
# Uses merge-upsert to handle multi-machine sync safely (Obsidian Sync may delay
# propagation, so append-only dedup fails). On conflict (same date+env), keeps
# the entry with higher total tokens (more complete day).
# Requires: npx, ccusage, python3
#
# Usage:
#   ccusage_daily_log.sh              # log yesterday (cron mode)
#   ccusage_daily_log.sh --backfill   # log ALL available days (one-time setup)

set -euo pipefail

VAULT_DIR="$HOME/vault"
LOG_FILE="$VAULT_DIR/0 Inbox/The Batcave/Claude Code Usage Log.md"

# Auto-detect environment from hostname
HOSTNAME=$(hostname)
case "$HOSTNAME" in
    bruce-vps*|faramir) ENV="VPS" ;;
    *)                  ENV="WSL" ;;
esac

# Determine mode: backfill (all days) or cron (yesterday only)
MODE="cron"
if [[ "${1:-}" == "--backfill" ]]; then
    MODE="backfill"
fi

# Get daily data as JSON
DATA=$("$(npm prefix -g)/bin/ccusage" daily --mode calculate --json 2>/dev/null)

# Merge-upsert: parse existing rows, merge new data, rewrite table
python3 -c "
import json, sys, re

data = json.loads(sys.stdin.read())
env = '$ENV'
mode = '$MODE'
log_file = '$LOG_FILE'

# Read existing log
try:
    with open(log_file, 'r') as f:
        content = f.read()
except FileNotFoundError:
    content = ''

# Parse existing data rows into a dict keyed by (date, env)
# Each row: | date | env | total_tokens | input | output | cache_create | cache_read | cost | models |
existing_rows = {}
row_pattern = re.compile(r'^\| (\d{4}-\d{2}-\d{2}) \| (VPS|WSL) \|(.+)$')

lines = content.split('\n')
header_end = -1  # index of last non-data line before the table rows
table_rows_start = -1
for i, line in enumerate(lines):
    m = row_pattern.match(line.strip())
    if m:
        if table_rows_start == -1:
            table_rows_start = i
        d, e = m.group(1), m.group(2)
        # Extract total tokens for conflict resolution (3rd column)
        cols = [c.strip() for c in line.strip().split('|')[1:-1]]  # skip empty first/last from split
        try:
            total_tokens = int(cols[2].replace(',', ''))
        except (IndexError, ValueError):
            total_tokens = 0
        key = (d, e)
        # Keep the entry with higher total tokens
        if key not in existing_rows or total_tokens > existing_rows[key][1]:
            existing_rows[key] = (line.strip(), total_tokens)

# Build new rows from ccusage data
from datetime import date, timedelta
if mode == 'cron':
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    target_dates = {yesterday}
else:
    target_dates = None  # all dates

for day in data.get('daily', []):
    d = day['date']

    # Filter to target dates if in cron mode
    if target_dates and d not in target_dates:
        continue

    total_cost = day['totalCost']
    total_tokens = day['totalTokens']
    input_t = day['inputTokens']
    output_t = day['outputTokens']
    cache_create = day['cacheCreationTokens']
    cache_read = day['cacheReadTokens']

    # Model breakdown
    models = []
    for m_info in day.get('modelBreakdowns', []):
        name = m_info['modelName'].replace('claude-', '').replace('-20251001', '')
        models.append(f'{name}: \${m_info[\"cost\"]:.2f}')
    model_str = ', '.join(models) if models else 'n/a'

    row = f'| {d} | {env} | {total_tokens:,} | {input_t:,} | {output_t:,} | {cache_create:,} | {cache_read:,} | \${total_cost:.2f} | {model_str} |'
    key = (d, env)

    # Upsert: keep higher total tokens
    if key not in existing_rows or total_tokens > existing_rows[key][1]:
        existing_rows[key] = (row, total_tokens)

# Rebuild the file: everything before the first data row + sorted data rows
if table_rows_start == -1:
    # No existing data rows — find the header row (starts with | Date) and keep everything through it
    pre_table = content.rstrip()
else:
    # Keep everything up to (but not including) the first data row
    pre_table = '\n'.join(lines[:table_rows_start]).rstrip()

# Sort rows by date, then env (VPS before WSL for same date)
sorted_rows = sorted(existing_rows.values(), key=lambda x: (x[0].split('|')[1].strip(), x[0].split('|')[2].strip()))
row_lines = [r[0] for r in sorted_rows]

result = pre_table + '\n' + '\n'.join(row_lines) + '\n'

with open(log_file, 'w') as f:
    f.write(result)

# Report what happened
new_count = len(row_lines) - len([k for k in existing_rows if existing_rows[k][0] in content])
if new_count > 0:
    print(f'ccusage: {new_count} row(s) added/updated for {env}')
else:
    print(f'ccusage: {env} already up to date ({len(row_lines)} total rows)')
" <<< "$DATA"
