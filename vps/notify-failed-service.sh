#!/usr/bin/env bash
# Called by systemd OnFailure= dropins. Arg 1 is the failed unit name.
set -u
UNIT="${1:-unknown.service}"

[[ -f "$HOME/.env.sh" ]] && source "$HOME/.env.sh"

LINES="$(journalctl -u "$UNIT" -n 5 --no-pager 2>/dev/null | tail -n 4 || true)"
[[ -z "$LINES" ]] && LINES="$(journalctl --user -u "$UNIT" -n 5 --no-pager 2>/dev/null | tail -n 4 || true)"

HOST="$(hostname)"
BODY="${LINES:-no log excerpt available}"
BODY_TRUNCATED="$(printf '%s' "$BODY" | head -c 300)"

SUMMARY="⚠️ ${UNIT} failed on ${HOST}: ${BODY_TRUNCATED}"
"$HOME/.local/bin/pagerduty-alert" "$SUMMARY" "critical" "${HOST}/${UNIT}"
