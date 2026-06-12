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

# Default output path
if [ -z "$OUTPUT" ]; then
    OUTPUT="/tmp/bruce_tts_$(date +%s).mp3"
fi

# One OpenAI TTS request -> mp3 file. Exits non-zero on API error.
synth_one() {
    local text="$1" out="$2" code
    code=$(curl -s -w "%{http_code}" https://api.openai.com/v1/audio/speech \
        -H "Authorization: Bearer $OPENAI_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg model "$MODEL" --arg voice "$VOICE" --arg input "$text" --argjson speed "$SPEED" \
            '{model: $model, input: $input, voice: $voice, speed: $speed}')" \
        --output "$out")
    if [ "$code" -ne 200 ]; then
        echo "Error: API returned HTTP $code" >&2
        if [ -f "$out" ]; then cat "$out" >&2; rm -f "$out"; fi
        exit 1
    fi
}

# OpenAI caps each request at 4096 chars. Past that, chunk on paragraph/
# sentence boundaries and concat the parts rather than truncating the text.
LIMIT=3900
if [ "${#TEXT}" -le "$LIMIT" ]; then
    synth_one "$TEXT" "$OUTPUT"
else
    if ! command -v ffmpeg >/dev/null 2>&1; then
        echo "Error: text is ${#TEXT} chars (>$LIMIT); ffmpeg is required to concat chunks." >&2
        exit 1
    fi
    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    N=$(printf '%s' "$TEXT" | python3 -c '
import sys, re, os
text = sys.stdin.read()
limit, outdir = int(sys.argv[1]), sys.argv[2]
chunks, cur = [], ""
for para in re.split(r"(\n\n+)", text):
    if len(cur) + len(para) <= limit:
        cur += para
    elif len(para) <= limit:
        if cur.strip(): chunks.append(cur)
        cur = para
    else:
        for sent in re.split(r"(?<=[.!?])\s+", para):
            if len(cur) + len(sent) + 1 <= limit:
                cur += ((" " if cur else "") + sent)
            else:
                if cur.strip(): chunks.append(cur)
                cur = sent
if cur.strip(): chunks.append(cur)
chunks = [c for c in chunks if c.strip()]
for i, c in enumerate(chunks):
    with open(os.path.join(outdir, "chunk_%03d.txt" % i), "w") as f:
        f.write(c)
print(len(chunks))
' "$LIMIT" "$WORK")
    LIST="$WORK/list.txt"
    : > "$LIST"
    for cf in "$WORK"/chunk_*.txt; do
        part="${cf%.txt}.mp3"
        synth_one "$(cat "$cf")" "$part"
        echo "file '$part'" >> "$LIST"
    done
    ffmpeg -y -f concat -safe 0 -i "$LIST" -c copy "$OUTPUT" >/dev/null 2>&1
    echo "Chunked ${#TEXT} chars into ${N} parts." >&2
fi

SIZE=$(stat -c%s "$OUTPUT" 2>/dev/null || stat -f%z "$OUTPUT" 2>/dev/null)
echo "$OUTPUT"
echo "Generated ${SIZE} bytes (voice: $VOICE, model: $MODEL, speed: ${SPEED}x)" >&2
