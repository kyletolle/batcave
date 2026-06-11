#!/usr/bin/env python3
"""
Read Aloud — Convert articles, feeds, or ebooks to audio via TTS.

Usage:
    read-aloud URL                          # Fetch via defuddle, convert to audio
    read-aloud --readwise ID                # Fetch from Readwise by document ID
    read-aloud --file path.txt              # Read from local file
    read-aloud --readwise ID --chapter 3    # Specific chapter from an ebook
    read-aloud --list-chapters ID           # List available chapters in an ebook

Options:
    --provider openai|deepgram      TTS provider (default: deepgram)
    --voice VOICE                   Voice name (default: orpheus for deepgram, ash for openai)
    --model MODEL                   Model (default: tts-1-hd for openai)
    --speed SPEED                   Playback speed (default: 1.5)
    --output-dir DIR                Output directory (default: /tmp/read-aloud)
    --telegram CHAT_ID              Send audio files to Telegram chat
    --dry-run                       Show what would be generated without calling TTS
    --chapter N                     Chapter number for ebooks (1-indexed)
    --list-chapters                 List chapters and exit
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path


def get_env(key):
    val = os.environ.get(key)
    if not val:
        print(f"Error: {key} not set. Run: source ~/.env.sh", file=sys.stderr)
        sys.exit(1)
    return val


class HTMLToText(HTMLParser):
    """Strip HTML tags, keep text content with paragraph breaks."""
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip_tags = {'style', 'script', 'head'}
        self.in_skip = 0
        self.in_block = False

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self.in_skip += 1
        if tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'br', 'li'):
            self.text.append('\n\n')

    def handle_endtag(self, tag):
        if tag in self.skip_tags:
            self.in_skip -= 1

    def handle_data(self, data):
        if self.in_skip <= 0:
            self.text.append(data)

    def get_text(self):
        raw = ''.join(self.text)
        # Collapse whitespace but preserve paragraph breaks
        raw = re.sub(r'[ \t]+', ' ', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        return raw.strip()


def html_to_text(html):
    parser = HTMLToText()
    parser.feed(html)
    return parser.get_text()


def strip_markdown(text):
    """Remove markdown formatting for clean TTS."""
    # Remove images
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    # Convert links to just text
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    # Remove emphasis markers
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}(.*?)_{1,3}', r'\1', text)
    # Remove headers markers (keep text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove horizontal rules
    text = re.sub(r'^---+\s*$', '', text, flags=re.MULTILINE)
    # Remove blockquote markers
    text = re.sub(r'^>\s?', '', text, flags=re.MULTILINE)
    # Remove inline code backticks
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Collapse blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def strip_boilerplate(text):
    """Remove newsletter CTAs, subscription pitches, and other boilerplate."""
    lines = text.split('\n')
    cleaned = []
    skip_until_blank = False

    for line in lines:
        lower = line.lower().strip()

        # Skip blank lines when in skip mode, reset on next content
        if skip_until_blank:
            if not lower:
                skip_until_blank = False
            continue

        # Common newsletter CTA patterns
        cta_patterns = [
            r'subscribe\s+(to\s+)?(my|our|the)\s+(premium\s+)?newsletter',
            r'if you (like|enjoy|appreciate) this (piece|article|post|newsletter)',
            r'(click|tap)\s+(here|the|that)\s+(button|link|circle)',
            r'in the bottom right (hand )?corner',
            r'(monthly|annual|yearly)\s+(subscription|plan)',
            r'(get|sign up for|join)\s+(my|our|the)\s+(premium|paid|newsletter)',
            r'(share|forward)\s+this\s+(article|post|newsletter|email)',
            r'(follow|find)\s+me\s+on\s+(twitter|x|bluesky|mastodon|substack)',
            r'(patreon|ko-fi|buy me a coffee|tip jar)',
            r'(paid subscribers?|premium members?)\s+(get|receive|have access)',
            r'you\'re gonna love it',
            r'(leave a|write a|drop a)\s+comment',
            r'reply to this email',
            r'(thanks|thank you) for (reading|subscribing|supporting)',
            r'if you\'re (not )?already (a )?(paid )?subscrib',
            r'(free|paid) (subscribers?|readers?|members?) (can|get|receive)',
            r'\$\d+\s+(a|per)\s+(year|month)',
            r'several books\' worth of content',
            r'I (just )?put out a (massive|huge|big|new)',
            r'I am regularly several steps ahead',
            r'absolute ton of value',
        ]

        is_cta = any(re.search(p, lower) for p in cta_patterns)

        if is_cta:
            skip_until_blank = True
            continue

        cleaned.append(line)

    result = '\n'.join(cleaned)

    # Pluralistic-specific: cut everything from "Hey look at this" onward
    hey_look = re.search(r'^\s*(?:#+\s*)?Hey look at this', result, re.IGNORECASE | re.MULTILINE)
    if hey_look:
        result = result[:hey_look.start()]

    # Pluralistic-specific: remove "Today's links" header line
    result = re.sub(r'^\s*(?:#+\s*)?Today\'s links.*$', '', result, flags=re.MULTILINE | re.IGNORECASE)

    # Remove #Xyrsago link roundup entries (Pluralistic "this day in history" links)
    result = re.sub(r'^#\d+yrs?ago\b.*$', '', result, flags=re.MULTILINE)

    # Remove bare URLs on their own lines (common in RSS/newsletter content)
    result = re.sub(r'^\s*https?://\S+\s*$', '', result, flags=re.MULTILINE)

    # Clean up residual blank lines from removals
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def fetch_url(url):
    """Fetch article content via defuddle."""
    try:
        result = subprocess.run(
            ['npx', 'defuddle', 'parse', '--md', url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return strip_markdown(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: try without --md
    try:
        result = subprocess.run(
            ['npx', 'defuddle', 'parse', url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if 'content' in data:
                return strip_markdown(html_to_text(data['content']))
    except Exception:
        pass

    print(f"Error: could not fetch {url}", file=sys.stderr)
    sys.exit(1)


def fetch_readwise(doc_id):
    """Fetch document content from Readwise Reader."""
    token = get_env('READWISE_TOKEN')
    import urllib.request

    url = f"https://readwise.io/api/v3/list/?id={doc_id}&withHtmlContent=1"
    req = urllib.request.Request(url, headers={'Authorization': f'Token {token}'})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if not data.get('results'):
        print(f"Error: document {doc_id} not found in Readwise", file=sys.stderr)
        sys.exit(1)

    doc = data['results'][0]
    title = doc.get('title', 'Unknown')
    html_content = doc.get('html_content', '')
    category = doc.get('category', '')

    if not html_content:
        print(f"Error: no content available for '{title}'", file=sys.stderr)
        sys.exit(1)

    return title, html_content, category


def extract_chapters(html_content):
    """Extract chapters from ebook HTML content."""
    # Split on h1/h2 chapter headings
    parts = re.split(r'(<h[12][^>]*>.*?</h[12]>)', html_content, flags=re.IGNORECASE | re.DOTALL)

    chapters = []
    current_title = None
    current_content = []

    for part in parts:
        heading_match = re.match(r'<h[12][^>]*>(.*?)</h[12]>', part, re.IGNORECASE | re.DOTALL)
        if heading_match:
            if current_title is not None or current_content:
                text = html_to_text(''.join(current_content))
                if len(text.strip()) > 100:  # Skip tiny sections
                    chapters.append({
                        'title': html_to_text(current_title) if current_title else 'Preamble',
                        'text': text.strip()
                    })
            current_title = heading_match.group(1)
            current_content = []
        else:
            current_content.append(part)

    # Don't forget the last chapter
    if current_content:
        text = html_to_text(''.join(current_content))
        if len(text.strip()) > 100:
            chapters.append({
                'title': html_to_text(current_title) if current_title else 'Final Section',
                'text': text.strip()
            })

    return chapters


def chunk_text(text, max_chars=4000):
    """Split text into chunks at paragraph boundaries, staying under max_chars."""
    paragraphs = text.split('\n\n')
    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        # If a single paragraph exceeds max, split on sentences
        if para_len > max_chars:
            if current:
                chunks.append('\n\n'.join(current))
                current = []
                current_len = 0

            sentences = re.split(r'(?<=[.!?])\s+', para)
            sent_chunk = []
            sent_len = 0
            for sent in sentences:
                if sent_len + len(sent) + 1 > max_chars and sent_chunk:
                    chunks.append(' '.join(sent_chunk))
                    sent_chunk = []
                    sent_len = 0
                sent_chunk.append(sent)
                sent_len += len(sent) + 1
            if sent_chunk:
                chunks.append(' '.join(sent_chunk))
            continue

        if current_len + para_len + 2 > max_chars and current:
            chunks.append('\n\n'.join(current))
            current = []
            current_len = 0

        current.append(para)
        current_len += para_len + 2

    if current:
        chunks.append('\n\n'.join(current))

    return chunks


def generate_openai(text, voice, model, speed, output_path):
    """Generate audio via OpenAI TTS API."""
    api_key = get_env('OPENAI_API_KEY')
    import urllib.request

    payload = json.dumps({
        'model': model,
        'input': text,
        'voice': voice,
        'speed': speed
    }).encode()

    req = urllib.request.Request(
        'https://api.openai.com/v1/audio/speech',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
    )

    with urllib.request.urlopen(req) as resp:
        with open(output_path, 'wb') as f:
            f.write(resp.read())


def generate_deepgram(text, voice, speed, output_path, keep_original=False):
    """Generate audio via Deepgram Aura TTS API, then speed up with ffmpeg."""
    api_key = get_env('DEEPGRAM_API_KEY')
    import urllib.request

    model_name = f"aura-{voice}-en"
    payload = json.dumps({'text': text}).encode()

    req = urllib.request.Request(
        f'https://api.deepgram.com/v1/speak?model={model_name}',
        data=payload,
        headers={
            'Authorization': f'Token {api_key}',
            'Content-Type': 'application/json'
        }
    )

    if speed == 1.0:
        # No speed adjustment needed
        with urllib.request.urlopen(req) as resp:
            with open(output_path, 'wb') as f:
                f.write(resp.read())
    else:
        # Generate at 1x, then use ffmpeg to adjust speed
        orig_path = output_path.replace('.mp3', '_1x.mp3')
        with urllib.request.urlopen(req) as resp:
            with open(orig_path, 'wb') as f:
                f.write(resp.read())

        subprocess.run(
            ['ffmpeg', '-y', '-i', orig_path, '-filter:a', f'atempo={speed}', output_path],
            capture_output=True, timeout=30
        )
        if not keep_original:
            os.unlink(orig_path)


def generate_audio(text, provider, voice, model, speed, output_path, keep_original=False):
    """Generate audio using the specified provider."""
    if provider == 'openai':
        generate_openai(text, voice, model, speed, output_path)
    elif provider == 'deepgram':
        generate_deepgram(text, voice, speed, output_path, keep_original=keep_original)
    else:
        print(f"Error: unknown provider '{provider}'", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Convert text to audio via TTS')
    parser.add_argument('source', nargs='?', help='URL to fetch and convert')
    parser.add_argument('--readwise', metavar='ID', help='Readwise document ID')
    parser.add_argument('--file', metavar='PATH', help='Local file to read')
    parser.add_argument('--provider', default='deepgram', choices=['openai', 'deepgram'])
    parser.add_argument('--voice', help='Voice name')
    parser.add_argument('--model', default='tts-1-hd', help='Model (OpenAI only)')
    parser.add_argument('--speed', type=float, default=1.5, help='Playback speed')
    parser.add_argument('--output-dir', default='/tmp/read-aloud', help='Output directory')
    parser.add_argument('--dry-run', action='store_true', help='Show chunks without generating audio')
    parser.add_argument('--chapter', type=int, help='Chapter number (1-indexed, ebooks only)')
    parser.add_argument('--list-chapters', action='store_true', help='List chapters and exit')
    parser.add_argument('--keep-originals', action='store_true', help='Keep 1x originals when speed != 1.0')

    args = parser.parse_args()

    # Set default voices per provider
    if not args.voice:
        args.voice = 'ash' if args.provider == 'openai' else 'orpheus'

    # Get content
    title = 'read-aloud'
    if args.readwise:
        title, html_content, category = fetch_readwise(args.readwise)

        if category == 'epub' or args.list_chapters or args.chapter:
            chapters = extract_chapters(html_content)

            if args.list_chapters:
                print(f"\n{title} — {len(chapters)} chapters:\n")
                for i, ch in enumerate(chapters, 1):
                    words = len(ch['text'].split())
                    print(f"  {i:3d}. {ch['title']} ({words:,} words)")
                return

            if args.chapter:
                if args.chapter < 1 or args.chapter > len(chapters):
                    print(f"Error: chapter {args.chapter} out of range (1-{len(chapters)})", file=sys.stderr)
                    sys.exit(1)
                ch = chapters[args.chapter - 1]
                text = ch['text']
                title = f"{title} — {ch['title']}"
                print(f"Chapter {args.chapter}: {ch['title']} ({len(text.split()):,} words)")
            else:
                # Default: convert the full text
                text = html_to_text(html_content)
        else:
            text = strip_boilerplate(html_to_text(html_content))

    elif args.source:
        text = strip_boilerplate(fetch_url(args.source))
        # Extract title from URL
        title = args.source.split('/')[-2] if args.source.endswith('/') else args.source.split('/')[-1]
        title = title.replace('-', ' ').replace('_', ' ')

    elif args.file:
        with open(args.file) as f:
            text = f.read()
        title = Path(args.file).stem

    else:
        parser.print_help()
        sys.exit(1)

    # Chunk the text — Deepgram has a ~2000 char limit, OpenAI allows 4096
    max_chars = 1900 if args.provider == 'deepgram' else 4000
    chunks = chunk_text(text, max_chars=max_chars)
    total_chars = sum(len(c) for c in chunks)
    total_words = sum(len(c.split()) for c in chunks)

    print(f"\n{title}")
    print(f"  {total_words:,} words, {total_chars:,} chars, {len(chunks)} chunks")
    print(f"  Provider: {args.provider}, Voice: {args.voice}, Speed: {args.speed}x")

    if args.provider == 'openai':
        cost_per_char = 0.000030 if args.model == 'tts-1-hd' else 0.000015
        est_cost = total_chars * cost_per_char
        print(f"  Estimated cost: ${est_cost:.2f}")
    elif args.provider == 'deepgram':
        est_cost = total_chars * 0.0000043
        print(f"  Estimated cost: ${est_cost:.4f}")

    if args.dry_run:
        print(f"\nChunks:")
        for i, chunk in enumerate(chunks, 1):
            preview = chunk[:80].replace('\n', ' ')
            print(f"  {i:3d}. [{len(chunk):,} chars] {preview}...")
        return

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Generate audio for each chunk
    safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip().replace(' ', '_')
    output_files = []

    for i, chunk in enumerate(chunks, 1):
        output_path = os.path.join(args.output_dir, f"{safe_title}_{i:03d}.mp3")
        print(f"  Generating {i}/{len(chunks)} ({len(chunk):,} chars)...", end='', flush=True)

        try:
            generate_audio(chunk, args.provider, args.voice, args.model, args.speed, output_path, keep_original=args.keep_originals)
            size = os.path.getsize(output_path)
            print(f" {size:,} bytes")
            output_files.append(output_path)
        except Exception as e:
            print(f" ERROR: {e}", file=sys.stderr)

    print(f"\nGenerated {len(output_files)} audio files in {args.output_dir}")

    # Print file list for easy piping
    for f in output_files:
        print(f)


if __name__ == '__main__':
    main()
