#!/usr/bin/env bash
# Called by systemd OnFailure= dropins (via moshi-notify@<unit>). Arg 1 is the
# failed unit name.
#
# This hook NEVER pages. Every unit wired to it (netdata, earlyoom, ob-sync,
# qmd-mcp, mempalace-mine) is a chore Bruce can fix, not a "breaks within
# hours" emergency, so the routing is (Kyle's rule, 2026-11-20):
#
#   recovered within GRACE_SEC  -> one Slack line, nothing else
#   still down after GRACE_SEC  -> Slack line + one `bruce` task in Todoist
#
# ob-sync's real outage path is ob-sync-check (restarts, quiet hours, its own
# dedup'd page); disk has disk-watch. Those keep PagerDuty. This one doesn't.
#
# History: until 2026-11-26 this paged critical on every failure, with no dedup
# key and no quiet hours. netdata hung on a routine unattended-upgrades restart,
# got SIGKILLed at the stop timeout, came straight back in 0s, and paged Kyle at
# 06:42 anyway.
#
# Test hooks:
#   NOTIFY_DRY_RUN=1           print actions instead of sending anything
#   NOTIFY_GRACE_SEC=N         override the recovery wait (default 90)
#   NOTIFY_FORCE_STATE=STATE   pretend `systemctl is-active` returned STATE
# Maintenance mute: touch ~/.local/state/notify-failed-service.mute to log only
# for the next hour (the file's mtime counts).
set -u
UNIT="${1:-unknown.service}"

[[ -f "$HOME/.env.sh" ]] && source "$HOME/.env.sh"

HOST="$(hostname)"
GRACE_SEC="${NOTIFY_GRACE_SEC:-90}"
DRY_RUN="${NOTIFY_DRY_RUN:-}"
MUTE_FILE="$HOME/.local/state/notify-failed-service.mute"
WATCHTOWER="$HOME/projects/batcave-private/scripts/watchtower.py"
TODOIST="$HOME/.local/bin/todoist"

log() { echo "notify-failed-service: $*"; }

if [[ -z "$DRY_RUN" && -f "$MUTE_FILE" ]] \
   && (( $(date +%s) - $(stat -c %Y "$MUTE_FILE") < 3600 )); then
    log "muted (maintenance); ${UNIT} failed on ${HOST}, nothing sent"
    exit 0
fi

# Capture the failure excerpt now, before the restart buries it.
LINES="$(journalctl -u "$UNIT" -n 5 --no-pager 2>/dev/null | tail -n 4 || true)"
[[ -z "$LINES" ]] && LINES="$(journalctl --user -u "$UNIT" -n 5 --no-pager 2>/dev/null | tail -n 4 || true)"
EXCERPT="$(printf '%s' "${LINES:-no log excerpt available}" | head -c 600)"

# Give Restart= / a restart job time to bring the unit back.
[[ -z "$DRY_RUN" ]] && sleep "$GRACE_SEC"

if [[ -n "${NOTIFY_FORCE_STATE:-}" ]]; then
    STATE="$NOTIFY_FORCE_STATE"
else
    STATE="$(systemctl is-active "$UNIT" 2>/dev/null)"
    [[ "$STATE" != "active" ]] && STATE="$(systemctl --user is-active "$UNIT" 2>/dev/null || echo "$STATE")"
fi

slack() {  # message
    if [[ -n "$DRY_RUN" ]]; then log "[dry-run] slack: $1"; return; fi
    if [[ -f "$WATCHTOWER" ]]; then
        python3 "$WATCHTOWER" post "$1" >/dev/null 2>&1 && return
    fi
    "$HOME/.local/bin/slack-notify" "$1" >/dev/null 2>&1 || true
}

if [[ "$STATE" == "active" || "$STATE" == "activating" || "$STATE" == "reloading" ]]; then
    slack "🔁 ${UNIT} failed on ${HOST} and was back up within ${GRACE_SEC}s. No action needed; no page sent."
    log "${UNIT} recovered (${STATE})"
    exit 0
fi

# Still down: one bruce task per unit until it's closed.
TITLE="failed-unit: ${UNIT} down on ${HOST}"
DESC="Flagged by notify-failed-service.sh at $(date -Is). State after ${GRACE_SEC}s: ${STATE:-unknown}.

Last log lines:
${EXCERPT}

Bruce: journalctl -u ${UNIT} (or --user) for the cause, fix it, restart the unit,
confirm systemctl is-active, then complete this task."

if [[ -n "$DRY_RUN" ]]; then
    log "[dry-run] todoist add \"${TITLE}\" --project Batcave --labels bruce"
elif "$TODOIST" search "$TITLE" 2>/dev/null | grep -q "^Found [0-9]* tasks"; then
    log "open task already exists for ${UNIT}"
else
    "$TODOIST" add "$TITLE" --project Batcave --labels bruce --priority 3 --due today \
        --description "$DESC" >/dev/null 2>&1 || log "todoist add failed"
fi

slack "❌ ${UNIT} failed on ${HOST} and is still ${STATE:-down} after ${GRACE_SEC}s. Filed a \`bruce\` task in Todoist Batcave; no page sent."
log "${UNIT} still ${STATE:-down}; task filed"
