#!/usr/bin/env bash
# Send push notifications via Moshi (getmoshi.app)
# Usage: moshi-notify "Title" "Message"
#        moshi-notify "Message"              (title defaults to "Bruce")
#        moshi-notify -t "Title" -m "Message"

set -euo pipefail

if [ -z "${MOSHI_API_TOKEN:-}" ]; then
    echo "Error: MOSHI_API_TOKEN not set. Add it to ~/.env.sh" >&2
    exit 1
fi

TITLE="Bruce"
MESSAGE=""

if [ "$#" -eq 0 ]; then
    echo "Usage: moshi-notify \"Title\" \"Message\"" >&2
    echo "       moshi-notify \"Message\"" >&2
    echo "       moshi-notify -t \"Title\" -m \"Message\"" >&2
    exit 1
elif [ "$1" = "-t" ] || [ "$1" = "--title" ]; then
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -t|--title) TITLE="$2"; shift 2 ;;
            -m|--message) MESSAGE="$2"; shift 2 ;;
            *) echo "Unknown flag: $1" >&2; exit 1 ;;
        esac
    done
elif [ "$#" -eq 1 ]; then
    MESSAGE="$1"
elif [ "$#" -eq 2 ]; then
    TITLE="$1"
    MESSAGE="$2"
else
    echo "Too many arguments. Use quotes around title and message." >&2
    exit 1
fi

if [ -z "$MESSAGE" ]; then
    echo "Error: message is required" >&2
    exit 1
fi

RESPONSE=$(curl -s -X POST https://api.getmoshi.app/api/webhook \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg token "$MOSHI_API_TOKEN" --arg title "$TITLE" --arg msg "$MESSAGE" \
        '{token: $token, title: $title, message: $msg}')")

if echo "$RESPONSE" | jq -e '.success' > /dev/null 2>&1; then
    echo "Sent: $TITLE — $MESSAGE"
else
    echo "Failed: $RESPONSE" >&2
    exit 1
fi
