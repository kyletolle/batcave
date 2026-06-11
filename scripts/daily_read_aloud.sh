#!/usr/bin/env bash
# Daily Read Aloud — Check Readwise for new unread feed items, convert to audio, send to Telegram
# Designed to run via cron at 9am MT daily
#
# Usage: daily-read-aloud [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source ~/.env.sh
# Telegram bot token lives in the plugin's channel config
source ~/.claude/channels/telegram/.env

# Config
TELEGRAM_CHAT_ID="7590362170"
OUTPUT_BASE="/tmp/read-aloud/daily"
STATE_FILE="$HOME/.local/state/read-aloud-seen.txt"
LOG_FILE="$HOME/.local/state/read-aloud.log"
DRY_RUN="${1:-}"

# Authors to convert (case-insensitive match)
AUTHORS=(
    "Cory Doctorow"
    "Ed Zitron"
    "Edward Zitron"
    "Brian Merchant"
    "Cal Newport"
    "Study Hacks"
)

# Ensure state directory exists
mkdir -p "$(dirname "$STATE_FILE")"
touch "$STATE_FILE"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "Starting daily read-aloud check"

# Build author filter for jq
AUTHOR_FILTER=$(printf '"%s",' "${AUTHORS[@]}")
AUTHOR_FILTER="[${AUTHOR_FILTER%,}]"

# Fetch unread feed items
ITEMS=$(curl -s "https://readwise.io/api/v3/list/?category=rss&location=feed" \
    -H "Authorization: Token $READWISE_TOKEN" | \
    jq --argjson authors "$AUTHOR_FILTER" \
    '[.results[] | select(.reading_progress == 0) |
      select(.author as $a | $authors | any(. as $pat | $a | test($pat; "i"))) |
      {id, title, author, source_url}]')

TOTAL=$(echo "$ITEMS" | jq 'length')
log "Found $TOTAL unread items from tracked authors"

if [ "$TOTAL" -eq 0 ]; then
    log "Nothing new. Done."
    exit 0
fi

# Process each item
PROCESSED=0
SENT_TITLES=()

echo "$ITEMS" | jq -c '.[]' | while read -r item; do
    ID=$(echo "$item" | jq -r '.id')
    TITLE=$(echo "$item" | jq -r '.title')
    AUTHOR=$(echo "$item" | jq -r '.author')
    URL=$(echo "$item" | jq -r '.source_url')

    # Skip if already processed
    if grep -qF "$ID" "$STATE_FILE" 2>/dev/null; then
        log "  Skipping (already processed): $TITLE"
        continue
    fi

    log "  Processing: $TITLE ($AUTHOR)"

    if [ "$DRY_RUN" = "--dry-run" ]; then
        log "    [DRY RUN] Would fetch and convert: $URL"
        continue
    fi

    # Create output directory for this article
    SAFE_TITLE=$(echo "$TITLE" | sed 's/[^a-zA-Z0-9 _-]//g' | head -c 50 | sed 's/ /_/g')
    OUTPUT_DIR="$OUTPUT_BASE/$SAFE_TITLE"
    mkdir -p "$OUTPUT_DIR"

    # Generate audio
    python3 "$SCRIPT_DIR/read_aloud.py" "$URL" \
        --output-dir "$OUTPUT_DIR" \
        --speed 1.0 \
        2>&1 | tee -a "$LOG_FILE"

    # Count generated files
    FILE_COUNT=$(find "$OUTPUT_DIR" -name "*.mp3" ! -name "*_1x.mp3" | wc -l)

    if [ "$FILE_COUNT" -eq 0 ]; then
        log "    No audio generated, skipping Telegram send"
        echo "$ID" >> "$STATE_FILE"
        continue
    fi

    # Send header to Telegram
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg chat "$TELEGRAM_CHAT_ID" \
            --arg text "📖 Daily Read: $TITLE ($AUTHOR) — $FILE_COUNT parts" \
            '{chat_id: $chat, text: $text}')" > /dev/null

    # Send each audio file
    PART=0
    find "$OUTPUT_DIR" -name "*.mp3" ! -name "*_1x.mp3" | sort | while read -r mp3; do
        PART=$((PART + 1))
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
            -F "chat_id=$TELEGRAM_CHAT_ID" \
            -F "document=@$mp3" \
            -F "caption=$PART/$FILE_COUNT" > /dev/null
        sleep 1  # Rate limiting
    done

    log "    Sent $FILE_COUNT parts to Telegram"

    # Mark as processed
    echo "$ID" >> "$STATE_FILE"
    PROCESSED=$((PROCESSED + 1))
done

log "Done. Processed articles today."
