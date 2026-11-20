#!/usr/bin/env bash
# Daily disk check, routed by severity:
#   >= THRESHOLD (85%): a Todoist task in Batcave, labeled `bruce`, plus a
#      #watchtower post. Bruce picks it up at session start. No page: a
#      creeping disk is a chore, not an emergency.
#   >= CRITICAL (95%): PagerDuty, under a stable dedup key so repeat runs
#      update one incident, and resolved automatically once usage drops back.
# Dedup: no new task while an open "disk-watch:" task exists.
set -u

THRESHOLD="${DISK_WATCH_THRESHOLD:-85}"
CRITICAL="${DISK_WATCH_CRITICAL:-95}"
[[ -f "$HOME/.env.sh" ]] && source "$HOME/.env.sh"

HOST="$(hostname)"
STATE_DIR="$HOME/.local/state/disk-watch"
PAGED_FLAG="$STATE_DIR/paged"
DEDUP_KEY="${HOST}/disk-critical"
WATCHTOWER="$HOME/projects/batcave-private/scripts/watchtower.py"

WARN=() CRIT=() MAX=0
# One line per filesystem (/, /var, /home often share one)
while read -r src size used avail pct mount; do
  n="${pct%\%}"
  (( n > MAX )) && MAX=$n
  line="${mount} ${pct} (${used}/${size})"
  (( n >= THRESHOLD )) && WARN+=("$line")
  (( n >= CRITICAL )) && CRIT+=("$line")
done < <(df -h --output=source,size,used,avail,pcent,target / /var /home 2>/dev/null \
           | awk 'NR>1 && !seen[$1]++')

# Critical tier: page, or resolve a page we sent earlier
if (( ${#CRIT[@]} > 0 )); then
  mkdir -p "$STATE_DIR"
  "$HOME/.local/bin/pagerduty-alert" -k "$DEDUP_KEY" \
    "💾 disk >${CRITICAL}% on ${HOST}: ${CRIT[*]}" "critical" "${HOST}/disk" \
    && touch "$PAGED_FLAG"
elif [[ -f "$PAGED_FLAG" ]]; then
  "$HOME/.local/bin/pagerduty-alert" --resolve -k "$DEDUP_KEY" && rm -f "$PAGED_FLAG"
fi

# Warning tier: one Bruce task until it's closed
(( ${#WARN[@]} == 0 )) && exit 0
if todoist search "disk-watch:" 2>/dev/null | grep -q "^Found [0-9]* tasks"; then
  echo "disk-watch: open task already exists (${WARN[*]})"
  exit 0
fi

BODY="$(printf -- '- %s\n' "${WARN[@]}")"
DESC="Flagged by disk-watch.sh at $(date -Is).
${BODY}

Bruce: find what grew (du -xh --max-depth=2 / | sort -rh | head), clear safe
caches (cache_prune.sh, old Claude Code versions, stale backups), confirm with
df -h /, then complete this task."

todoist add "disk-watch: ${HOST} root disk at ${MAX}%" \
  --project Batcave --labels bruce --priority 3 --due today \
  --description "$DESC"

if [[ -f "$WATCHTOWER" ]]; then
  python3 "$WATCHTOWER" post "💾 disk-watch: ${HOST} at ${MAX}% (over ${THRESHOLD}%). Filed a \`bruce\` task in Todoist Batcave; no page sent." \
    || true
fi
