#!/usr/bin/env python3
"""Extract conversation text from Claude Code JSONL session logs.

Reads a Claude Code session JSONL file and outputs the human/assistant
conversation as clean markdown, skipping tool use blocks.

Usage:
    extract-session <session_id_or_path> [output_path]
    extract-session --list                # list sessions with first user message
    extract-session --search <keyword>    # find sessions mentioning a keyword

Session ID can be a full path, a UUID, or a partial UUID prefix.
Output defaults to /tmp/session_<id_prefix>.md

Examples:
    extract-session 911047b4
    extract-session 911047b4 ~/vault/0\\ Inbox/session_dump.md
    extract-session --list
    extract-session --search "Mediary"
"""

import json
import os
import sys
import glob
import argparse

SESSIONS_DIR = os.path.expanduser(
    "~/.claude/projects/-home-kyle-vault"
)


def find_session_file(session_id):
    """Resolve a session ID (full path, UUID, or prefix) to a JSONL file."""
    if os.path.isfile(session_id):
        return session_id

    # Try as full UUID
    candidate = os.path.join(SESSIONS_DIR, f"{session_id}.jsonl")
    if os.path.isfile(candidate):
        return candidate

    # Try as prefix
    matches = glob.glob(os.path.join(SESSIONS_DIR, f"{session_id}*.jsonl"))
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        print(f"Ambiguous prefix '{session_id}', matches:")
        for m in sorted(matches):
            print(f"  {os.path.basename(m)}")
        sys.exit(1)

    print(f"No session found for '{session_id}'")
    sys.exit(1)


def extract_text(content):
    """Extract text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        parts.append(text.strip())
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        return "\n\n".join(parts)
    return ""


def extract_session(filepath):
    """Extract user/assistant messages from a session JSONL file."""
    messages = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")

            if msg_type == "user":
                content = obj.get("message", {}).get("content", "")
                text = extract_text(content)
                if text:
                    messages.append(("USER", text, obj.get("timestamp", "")))

            elif msg_type == "assistant":
                content = obj.get("message", {}).get("content", [])
                text = extract_text(content)
                if text:
                    messages.append(("ASSISTANT", text, obj.get("timestamp", "")))

    return messages


def format_output(messages, timestamps=True):
    """Format extracted messages as markdown."""
    lines = []
    for role, text, ts in messages:
        lines.append(f"--- {role} ---")
        if timestamps and ts:
            lines.append(f"[{ts}]")
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def list_sessions():
    """List all sessions with date and first user message."""
    files = sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl")))
    for f in files:
        sid = os.path.basename(f).replace(".jsonl", "")
        first_ts = ""
        first_msg = ""
        try:
            with open(f) as fh:
                for line in fh:
                    obj = json.loads(line)
                    if not first_ts and obj.get("timestamp"):
                        first_ts = obj["timestamp"][:10]
                    if obj.get("type") == "user" and not first_msg:
                        first_msg = extract_text(
                            obj.get("message", {}).get("content", "")
                        )[:120]
                    if first_ts and first_msg:
                        break
        except Exception:
            pass
        print(f"{sid[:8]}  {first_ts}  {first_msg}")


def search_sessions(keyword):
    """Find sessions containing a keyword and count mentions."""
    keyword_lower = keyword.lower()
    files = sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl")))
    results = []
    for f in files:
        sid = os.path.basename(f).replace(".jsonl", "")
        count = 0
        first_ts = ""
        first_msg = ""
        try:
            with open(f) as fh:
                for line in fh:
                    obj = json.loads(line)
                    if not first_ts and obj.get("timestamp"):
                        first_ts = obj["timestamp"][:10]
                    if obj.get("type") == "user" and not first_msg:
                        first_msg = extract_text(
                            obj.get("message", {}).get("content", "")
                        )[:100]
                    # Count in user/assistant text only
                    if obj.get("type") in ("user", "assistant"):
                        text = extract_text(
                            obj.get("message", {}).get("content", "")
                        ).lower()
                        count += text.count(keyword_lower)
        except Exception:
            pass
        if count > 0:
            results.append((count, sid, first_ts, first_msg))

    results.sort(reverse=True)
    for count, sid, ts, msg in results:
        print(f"{count:4d}x  {sid[:8]}  {ts}  {msg}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract conversation from Claude Code session logs"
    )
    parser.add_argument(
        "session", nargs="?", help="Session ID, UUID prefix, or file path"
    )
    parser.add_argument("output", nargs="?", help="Output file path")
    parser.add_argument(
        "--list", action="store_true", help="List all sessions"
    )
    parser.add_argument(
        "--search", metavar="KEYWORD", help="Search sessions for a keyword"
    )
    parser.add_argument(
        "--no-timestamps", action="store_true",
        help="Omit timestamps from output"
    )

    args = parser.parse_args()

    if args.list:
        list_sessions()
        return

    if args.search:
        search_sessions(args.search)
        return

    if not args.session:
        parser.print_help()
        sys.exit(1)

    filepath = find_session_file(args.session)
    sid = os.path.basename(filepath).replace(".jsonl", "")
    output = args.output or f"/tmp/session_{sid[:8]}.md"

    messages = extract_session(filepath)
    text = format_output(messages, timestamps=not args.no_timestamps)

    with open(output, "w") as f:
        f.write(text)

    size_kb = os.path.getsize(output) / 1024
    print(f"Extracted {len(messages)} messages ({size_kb:.1f} KB) → {output}")


if __name__ == "__main__":
    main()
