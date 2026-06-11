#!/usr/bin/env python3
"""
Send a vault note to Readwise Reader.

Takes any Obsidian markdown file, strips frontmatter and wikilinks,
converts to styled HTML, and sends it to Readwise Reader via their API.

Usage:
    send_to_readwise.py <file_path>
    send_to_readwise.py <file_path> --title "Custom Title"
    send_to_readwise.py <file_path> --tags weekly-review ai-distilled
    send_to_readwise.py <file_path> --dry-run

Environment variables required:
    READWISE_TOKEN    Your Readwise access token (readwise.io/access_token)

Dependencies:
    pip install requests markdown
"""

import sys
import os
import re
import argparse

try:
    import requests
except ImportError:
    print("Error: 'requests' package is required. Install with: pip install requests")
    sys.exit(1)

try:
    import markdown
except ImportError:
    print("Error: 'markdown' package is required. Install with: pip install markdown")
    sys.exit(1)


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def strip_frontmatter(content):
    """Remove YAML frontmatter (--- delimited block at start of file)."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].lstrip("\n")
    return content


def strip_wikilinks(content):
    """Convert Obsidian wikilinks to plain text.

    [[Note Name]]         -> Note Name
    [[Note Name|Display]] -> Display
    ![[Embedded Note]]    -> (stripped entirely)
    """
    # Remove embeds first
    content = re.sub(r"!\[\[.*?\]\]", "", content)
    # [[target|display]] -> display
    content = re.sub(r"\[\[(?:[^\]|]+\|)([^\]]+)\]\]", r"\1", content)
    # [[target]] -> target
    content = re.sub(r"\[\[([^\]]+)\]\]", r"\1", content)
    return content


def strip_callout_syntax(content):
    """Convert Obsidian callouts to plain blockquotes.

    > [!note] Title  ->  > **Title**
    > [!warning]      ->  > (just the blockquote)
    """
    content = re.sub(
        r"^(>[ \t]*)\[!(\w+)\][ \t]*(.+)$",
        r"\1**\3**",
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r"^(>[ \t]*)\[!(\w+)\][ \t]*$",
        r"\1",
        content,
        flags=re.MULTILINE,
    )
    return content


def title_from_filename(path):
    """Derive a title from the filename (without extension)."""
    return os.path.splitext(os.path.basename(path))[0]


def slug_from_title(title):
    """Create a URL-safe slug from a title."""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug


def markdown_to_html(md_text, title):
    """Convert markdown to styled HTML document."""
    html_body = markdown.markdown(
        md_text,
        extensions=["extra", "sane_lists"],
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    max-width: 720px;
    margin: 0 auto;
    padding: 20px;
    line-height: 1.6;
    color: #1a1a1a;
  }}
  h1 {{ border-bottom: 2px solid #333; padding-bottom: 8px; margin-top: 2em; }}
  h2 {{ color: #2c3e50; margin-top: 1.5em; }}
  h3 {{ color: #34495e; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 2em 0; }}
  blockquote {{
    border-left: 3px solid #ccc;
    margin-left: 0;
    padding-left: 1em;
    color: #555;
  }}
  code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }}
  strong {{ color: #1a1a1a; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

    return html


def send_to_readwise(token, title, html, author="Bruce in the Batcave",
                     tags=None, url_slug=None):
    """Send HTML content to Readwise Reader via their save API."""
    if tags is None:
        tags = ["vault-note"]
    if url_slug is None:
        url_slug = slug_from_title(title)

    resp = requests.post(
        "https://readwise.io/api/v3/save/",
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        },
        json={
            "url": f"https://vault.local/notes/{url_slug}",
            "html": html,
            "title": title,
            "author": author,
            "category": "article",
            "location": "new",
            "tags": tags,
        },
        timeout=30,
    )
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:500]
        print(f"Error {resp.status_code}: {detail}")
        resp.raise_for_status()
    return resp.json()


def prepare_note(content):
    """Full preprocessing pipeline for a vault note."""
    content = strip_frontmatter(content)
    content = strip_wikilinks(content)
    content = strip_callout_syntax(content)
    return content


def main():
    parser = argparse.ArgumentParser(
        description="Send a vault note to Readwise Reader."
    )
    parser.add_argument("file", help="Path to the markdown file")
    parser.add_argument("--title", help="Custom title (default: filename)")
    parser.add_argument("--author", default="Bruce in the Batcave",
                        help="Author name (default: Bruce in the Batcave)")
    parser.add_argument("--tags", nargs="+", default=["vault-note"],
                        help="Tags for Readwise (default: vault-note)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Convert to HTML and save locally instead of sending",
    )
    args = parser.parse_args()

    content = read_file(args.file)
    title = args.title or title_from_filename(args.file)
    processed = prepare_note(content)

    print(f"File:      {args.file}")
    print(f"Title:     {title}")
    print(f"Author:    {args.author}")
    print(f"Tags:      {', '.join(args.tags)}")

    html = markdown_to_html(processed, title)
    print(f"HTML:      {len(html):,} chars")

    if args.dry_run:
        out_path = args.file.rsplit(".", 1)[0] + ".html"
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
    result = send_to_readwise(token, title, html, author=args.author,
                              tags=args.tags)
    print(f"Done! Document ID: {result.get('id', 'unknown')}")
    reader_url = result.get("url", "")
    if reader_url:
        print(f"URL: {reader_url}")


if __name__ == "__main__":
    main()
