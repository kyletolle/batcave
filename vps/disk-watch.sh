#!/usr/bin/env bash
# Daily disk check. Alerts via PagerDuty if any monitored mount is over threshold.
# Run from cron. Stateless — alerts every run if over threshold.
set -u

THRESHOLD="${DISK_WATCH_THRESHOLD:-85}"
[[ -f "$HOME/.env.sh" ]] && source "$HOME/.env.sh"

ALERTS=()
while read -r fs size used avail pct mount; do
  [[ "$pct" == "Use%" ]] && continue
  pct_num="${pct%\%}"
  if (( pct_num >= THRESHOLD )); then
    ALERTS+=("${mount} ${pct} (${used}/${size})")
  fi
done < <(df -h --output=source,size,used,avail,pcent,target / /var /home 2>/dev/null | awk 'NR>1 {print}')

if (( ${#ALERTS[@]} > 0 )); then
  HOST="$(hostname)"
  BODY="$(printf '%s\n' "${ALERTS[@]}")"
  "$HOME/.local/bin/pagerduty-alert" "💾 disk >${THRESHOLD}% on ${HOST}: ${BODY}" "warning" "${HOST}/disk"
fi
