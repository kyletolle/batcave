#!/usr/bin/env bash
# Generate speech audio from text via OpenAI TTS API
# Usage: tts "Text to speak"
#        tts -o output.mp3 "Text to speak"
#        tts -v nova "Text to speak"
#        tts -i input.txt
#        echo "piped text" | tts
#
# Voices: alloy, ash, coral, echo, fable, nova, onyx, sage, shimmer
# Models: tts-1 (fast), tts-1-hd (higher quality)

set -euo pipefail

if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "Error: OPENAI_API_KEY not set. Add it to ~/.env.sh" >&2
    exit 1
fi

# Defaults
VOICE="onyx"
MODEL="tts-1"
SPEED="1.0"
OUTPUT=""
INPUT_FILE=""
TEXT=""

# Parse args
while [ "$#" -gt 0 ]; do
    case "$1" in
        -v|--voice) VOICE="$2"; shift 2 ;;
        -m|--model) MODEL="$2"; shift 2 ;;
        -s|--speed) SPEED="$2"; shift 2 ;;
        -o|--output) OUTPUT="$2"; shift 2 ;;
        -i|--input) INPUT_FILE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: tts [options] \"Text to speak\""
            echo "       tts -i input.txt"
            echo "       echo \"text\" | tts"
            echo ""
            echo "Options:"
            echo "  -v, --voice   Voice: alloy, ash, coral, echo, fable, nova, onyx (default), sage, shimmer"
            echo "  -m, --model   Model: tts-1 (default, fast), tts-1-hd (higher quality)"
            echo "  -s, --speed   Speed: 0.25 to 4.0 (default: 1.0)"
            echo "  -o, --output  Output file path (default: /tmp/bruce_tts_TIMESTAMP.mp3)"
            echo "  -i, --input   Read text from file instead of argument"
            exit 0
            ;;
        -*) echo "Unknown flag: $1" >&2; exit 1 ;;
        *) TEXT="$1"; shift ;;
    esac
done

# Get text from file, stdin, or argument
if [ -n "$INPUT_FILE" ]; then
    if [ ! -f "$INPUT_FILE" ]; then
        echo "Error: input file not found: $INPUT_FILE" >&2
        exit 1
    fi
    TEXT=$(cat "$INPUT_FILE")
elif [ -z "$TEXT" ] && [ ! -t 0 ]; then
    TEXT=$(cat)
fi

if [ -z "$TEXT" ]; then
    echo "Error: no text provided. Pass as argument, -i file, or pipe to stdin." >&2
    exit 1
fi

# OpenAI TTS has a 4096 character limit per request
if [ "${#TEXT}" -gt 4096 ]; then
    echo "Warning: text is ${#TEXT} chars (limit 4096). Truncating." >&2
    TEXT="${TEXT:0:4096}"
fi

# Default output path
if [ -z "$OUTPUT" ]; then
    OUTPUT="/tmp/bruce_tts_$(date +%s).mp3"
fi

# Generate audio
HTTP_CODE=$(curl -s -w "%{http_code}" https://api.openai.com/v1/audio/speech \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg model "$MODEL" --arg voice "$VOICE" --arg input "$TEXT" --argjson speed "$SPEED" \
        '{model: $model, input: $input, voice: $voice, speed: $speed}')" \
    --output "$OUTPUT")

if [ "$HTTP_CODE" -ne 200 ]; then
    echo "Error: API returned HTTP $HTTP_CODE" >&2
    # Output file might contain error JSON
    if [ -f "$OUTPUT" ]; then
        cat "$OUTPUT" >&2
        rm -f "$OUTPUT"
    fi
    exit 1
fi

SIZE=$(stat -c%s "$OUTPUT" 2>/dev/null || stat -f%z "$OUTPUT" 2>/dev/null)
echo "$OUTPUT"
echo "Generated ${SIZE} bytes (voice: $VOICE, model: $MODEL, speed: ${SPEED}x)" >&2
