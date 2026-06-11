#!/usr/bin/env python3
"""
Send AI Note to Readwise Reader

Reads a weekly AI note, strips the structural header sections (Models to Use,
Review Prompt, etc.), keeps only the model responses, converts to HTML, and
sends to Readwise Reader.

This is a thin wrapper around send_to_readwise.py that adds AI-note-specific
preprocessing. For sending arbitrary vault notes, use send_to_readwise.py directly.

Usage:
    python send_ai_note.py <ai_note_path>
    python send_ai_note.py <ai_note_path> --dry-run

Environment variables required:
    READWISE_TOKEN    Your Readwise access token (readwise.io/access_token)

Dependencies:
    pip install requests markdown
"""

import sys
import os
import re
import argparse

# Import shared functions from the generic sender
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from send_to_readwise import (
    read_file,
    markdown_to_html,
    send_to_readwise,
    strip_wikilinks,
)


def extract_model_responses(content):
    """Strip frontmatter and structural sections, keep only model responses.

    Returns (week_label, response_markdown).
    """
    # Extract week label from the review prompt
    week_match = re.search(r"FYI (20\d{2}-W\d{2}) is the current week", content)
    week_label = week_match.group(1) if week_match else "Weekly Review"

    lines = content.split("\n")
    result = []
    i = 0
    in_structural_section = False
    in_code_block = False
    found_first_model = False

    structural_headings = {"Models to Use", "Model Names", "Review Prompt"}

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            in_code_block = not in_code_block

        heading_match = re.match(r"^# (.+)$", line)

        if heading_match and not in_code_block:
            heading = heading_match.group(1).strip()
            if heading in structural_headings:
                in_structural_section = True
            else:
                if found_first_model:
                    result.append("")
                    result.append("---")
                    result.append("")
                in_structural_section = False
                found_first_model = True
                result.append(line)
        elif not in_structural_section and found_first_model:
            result.append(line)

        i += 1

    response_md = strip_wikilinks("\n".join(result))
    return week_label, response_md


def main():
    parser = argparse.ArgumentParser(
        description="Convert a weekly AI note to HTML and send it to Readwise Reader."
    )
    parser.add_argument("ai_note", help="Path to the weekly AI note")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Convert to HTML and save locally instead of sending to Readwise",
    )
    args = parser.parse_args()

    content = read_file(args.ai_note)
    week_label, response_md = extract_model_responses(content)
    title = f"{week_label} AI Weekly Review"

    print(f"AI note:   {args.ai_note}")
    print(f"Week:      {week_label}")
    print(f"Title:     {title}")

    html = markdown_to_html(response_md, title)
    print(f"HTML:      {len(html):,} chars")

    if args.dry_run:
        out_path = args.ai_note.rsplit(".", 1)[0] + ".html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Saved to {out_path}")
        return

    token = os.environ.get("READWISE_TOKEN")
    if not token:
        print("Error: READWISE_TOKEN environment variable not set")
        print("Get your token from: https://readwise.io/access_token")
        sys.exit(1)

    print("Sending to Readwise Reader...")
    result = send_to_readwise(
        token, title, html,
        tags=["weekly-review"],
        url_slug=f"weekly-review/{week_label}",
    )
    print(f"Done! Document ID: {result.get('id', 'unknown')}")
    reader_url = result.get("url", "")
    if reader_url:
        print(f"URL: {reader_url}")


if __name__ == "__main__":
    main()
