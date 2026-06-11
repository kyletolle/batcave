#!/usr/bin/env python3
"""
Shard Gardens spelling and grammar audit.

Walks the vault for notes with `publish: true`, strips markdown chrome,
sends paragraphs to the LanguageTool Premium API, and writes a grouped
report to the Batcave for review.

Usage:
    shard_gardens_audit.py                    # full run over all published notes
    shard_gardens_audit.py --limit 10         # tuning pass
    shard_gardens_audit.py --file path.md     # single-file check
    shard_gardens_audit.py --dry-run          # strip + paragraph-split, no API calls

Environment:
    LANGUAGETOOL_API_KEY       Required
    LANGUAGETOOL_USERNAME      Required (LT Premium account email)

Dependencies: requests
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' package is required. Install with: pip install requests")
    sys.exit(1)


VAULT_ROOT = Path("/home/kyle/vault")
SCRIPT_DIR = VAULT_ROOT / "3 Information" / "Scripts"
DATA_DIR = SCRIPT_DIR / "data"
DICT_PATH = DATA_DIR / "shard_gardens_dictionary.txt"
SKIP_PATH = DATA_DIR / "shard_gardens_skip.txt"
DISABLED_RULES_PATH = DATA_DIR / "shard_gardens_disabled_rules.txt"
CACHE_PATH = DATA_DIR / "lt_audit_cache.json"
MATCHES_CACHE_PATH = DATA_DIR / "lt_matches_cache.json"
REPORT_DIR = VAULT_ROOT / "0 Inbox" / "The Batcave"

LT_ENDPOINT = "https://api.languagetoolplus.com/v2/check"
LT_LANGUAGE = "en-US"
THROTTLE_SECONDS = 0.1  # LT Premium tolerates ~80 req/min; we're well under
BATCH_CHAR_TARGET = 15000  # combine paragraphs up to ~15KB per API call
MAX_CHUNK_CHARS = 18000  # hard ceiling — leave margin under LT's ~20KB cap


def read_disabled_rules():
    """Load disabled LT rule IDs from config file."""
    if not DISABLED_RULES_PATH.exists():
        return []
    rules = []
    for line in DISABLED_RULES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            rules.append(line)
    return rules


DISABLED_RULES = read_disabled_rules()


@dataclass
class Paragraph:
    start_line: int
    text: str


@dataclass
class Match:
    file: Path
    line: int
    column: int
    rule_id: str
    category: str
    message: str
    context: str
    suggestions: list = field(default_factory=list)
    offending: str = ""


def read_dictionary():
    if not DICT_PATH.exists():
        return set()
    words = set()
    for line in DICT_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.add(line.lower())
    return words


def read_skip_patterns():
    if not SKIP_PATH.exists():
        return []
    patterns = []
    for line in SKIP_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def is_skipped(rel_path: str, patterns: list) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def find_published_notes(root: Path):
    out = []
    fm_pattern = re.compile(r"^publish:\s*true\s*$", re.MULTILINE)
    for path in root.rglob("*.md"):
        # Skip hidden and archive-adjacent dirs
        parts = path.relative_to(root).parts
        if any(p.startswith(".") for p in parts):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
        except (OSError, UnicodeDecodeError):
            continue
        # Only check frontmatter region
        if not head.startswith("---"):
            continue
        end = head.find("---", 3)
        if end == -1:
            continue
        if fm_pattern.search(head[3:end]):
            out.append(path)
    return sorted(out)


FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)
FENCE_RE = re.compile(r"^(\s*)```")
WIKILINK_RE = re.compile(r"!?\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
URL_RE = re.compile(r"https?://\S+")
HEADER_HASH_RE = re.compile(r"^#{1,6}\s+")
LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
BLOCKQUOTE_RE = re.compile(r"^\s*>+\s?")


def strip_to_paragraphs(content: str):
    """Strip markdown chrome and return (start_line, paragraph_text) tuples.

    Line numbers are 1-indexed and point to the *original* file so the
    report can cite them back.
    """
    # Remove frontmatter (count its lines for offset)
    fm_match = FRONTMATTER_RE.match(content)
    offset = 0
    if fm_match:
        offset = content[:fm_match.end()].count("\n")
        content = content[fm_match.end():]

    lines = content.split("\n")
    stripped_lines = []
    in_fence = False
    for i, raw in enumerate(lines):
        line_no = offset + i + 1
        if FENCE_RE.match(raw):
            in_fence = not in_fence
            stripped_lines.append((line_no, ""))
            continue
        if in_fence:
            stripped_lines.append((line_no, ""))
            continue
        s = raw
        s = HTML_COMMENT_RE.sub("", s)
        s = INLINE_CODE_RE.sub("", s)
        # Wikilinks: keep display text (or target name)
        s = WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), s)
        # Markdown links: keep text, drop URL
        s = MD_LINK_RE.sub(lambda m: m.group(1), s)
        # Raw URLs
        s = URL_RE.sub("", s)
        # Strip leading chrome
        s = HEADER_HASH_RE.sub("", s)
        s = BLOCKQUOTE_RE.sub("", s)
        s = LIST_MARKER_RE.sub("", s)
        stripped_lines.append((line_no, s.rstrip()))

    paragraphs = []
    buf_start = None
    buf = []
    for line_no, text in stripped_lines:
        if text.strip():
            if buf_start is None:
                buf_start = line_no
            buf.append(text)
        else:
            if buf:
                paragraphs.append(Paragraph(buf_start, "\n".join(buf)))
            buf = []
            buf_start = None
    if buf:
        paragraphs.append(Paragraph(buf_start, "\n".join(buf)))

    return paragraphs


def lt_check(text: str, username: str, api_key: str):
    """POST to LanguageTool Premium and return the matches list."""
    data = {
        "text": text,
        "language": LT_LANGUAGE,
        "username": username,
        "apiKey": api_key,
        "disabledRules": ",".join(DISABLED_RULES) if DISABLED_RULES else "",
    }
    for attempt in range(3):
        try:
            resp = requests.post(LT_ENDPOINT, data=data, timeout=30)
        except requests.RequestException as e:
            print(f"    network error: {e}; retry {attempt+1}/3", file=sys.stderr)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"    rate limited; sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            print("    401 unauthorized — check LANGUAGETOOL_USERNAME and LANGUAGETOOL_API_KEY", file=sys.stderr)
            sys.exit(2)
        if resp.status_code != 200:
            print(f"    API {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return []
        return resp.json().get("matches", [])
    return []


def match_line_column(para: Paragraph, offset: int):
    """Translate an LT character offset within the paragraph into (line, col)."""
    before = para.text[:offset]
    line_in_para = before.count("\n")
    col = offset - (before.rfind("\n") + 1) if "\n" in before else offset
    return para.start_line + line_in_para, col + 1


def file_hash(path: Path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_cache(cache):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def load_matches_cache():
    if MATCHES_CACHE_PATH.exists():
        try:
            return json.loads(MATCHES_CACHE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_matches_cache(cache):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MATCHES_CACHE_PATH.write_text(json.dumps(cache))


def build_batches(paragraphs: list):
    """Pack paragraphs into ~15KB batches joined by a distinctive separator.

    Yields (batch_text, [(paragraph, offset_within_batch), ...]).
    The separator is two newlines — matches LT's natural paragraph boundary.
    """
    SEP = "\n\n"
    batch_parts = []
    batch_paras = []
    cur_len = 0
    for para in paragraphs:
        text = para.text
        if not text.strip():
            continue
        # Oversized single paragraph — flush then emit alone in chunks
        if len(text) > MAX_CHUNK_CHARS:
            if batch_parts:
                yield "".join(batch_parts), batch_paras
                batch_parts, batch_paras, cur_len = [], [], 0
            for i in range(0, len(text), MAX_CHUNK_CHARS):
                chunk = text[i:i+MAX_CHUNK_CHARS]
                # Synthesize a paragraph with adjusted start line (approximate)
                sub_para = Paragraph(
                    start_line=para.start_line + text[:i].count("\n"),
                    text=chunk,
                )
                yield chunk, [(sub_para, 0)]
            continue
        sep = SEP if batch_parts else ""
        add_len = len(sep) + len(text)
        if cur_len + add_len > BATCH_CHAR_TARGET and batch_parts:
            yield "".join(batch_parts), batch_paras
            batch_parts, batch_paras, cur_len = [], [], 0
            sep = ""
            add_len = len(text)
        batch_parts.append(sep + text)
        batch_paras.append((para, cur_len + len(sep)))
        cur_len += add_len
    if batch_parts:
        yield "".join(batch_parts), batch_paras


def locate_in_batch(offset: int, batch_paras: list):
    """Given an offset in a batch, return (paragraph, offset_within_paragraph)."""
    current = batch_paras[0]
    for entry in batch_paras:
        if entry[1] <= offset:
            current = entry
        else:
            break
    para, para_offset = current
    return para, offset - para_offset


def filter_and_build_matches(path: Path, raw_batches: list, dictionary: set):
    """Apply current filters (dictionary, em-dash) to cached raw batches.

    raw_batches is a list of dicts with keys: batch_text, batch_paras, matches.
    Returns a list of Match objects.
    """
    matches = []
    for rb in raw_batches:
        batch_text = rb["batch_text"]
        # Rehydrate batch_paras as list of (Paragraph, offset)
        batch_paras = [
            (Paragraph(start_line=p["start_line"], text=p["text"]), offset)
            for p, offset in rb["batch_paras"]
        ]
        for m in rb["matches"]:
            offset = m.get("offset", 0)
            length = m.get("length", 0)
            offending = batch_text[offset:offset+length]
            if offending.lower() in dictionary:
                continue
            token = re.sub(r"[^\w'-]", "", offending).lower()
            if token and token in dictionary:
                continue
            replacements = [r.get("value", "") for r in m.get("replacements", [])]
            if any("—" in r for r in replacements):
                continue
            para, offset_in_para = locate_in_batch(offset, batch_paras)
            line, col = match_line_column(para, offset_in_para)
            ctx = m.get("context", {}).get("text", "")
            matches.append(Match(
                file=path,
                line=line,
                column=col,
                rule_id=m.get("rule", {}).get("id", ""),
                category=m.get("rule", {}).get("category", {}).get("name", ""),
                message=m.get("message", ""),
                context=ctx,
                suggestions=replacements[:5],
                offending=offending,
            ))
    return matches


def audit_file(path: Path, username: str, api_key: str, dictionary: set, dry_run: bool):
    """Hit the LT API for this file, return (matches, n_paras, raw_batches)."""
    content = path.read_text(encoding="utf-8", errors="ignore")
    paragraphs = strip_to_paragraphs(content)
    raw_batches = []
    if dry_run:
        return [], len(paragraphs), raw_batches

    for batch_text, batch_paras in build_batches(paragraphs):
        if not batch_text.strip():
            continue
        raw_matches = lt_check(batch_text, username, api_key)
        raw_batches.append({
            "batch_text": batch_text,
            "batch_paras": [({"start_line": p.start_line, "text": p.text}, off) for p, off in batch_paras],
            "matches": raw_matches,
        })
        time.sleep(THROTTLE_SECONDS)

    matches = filter_and_build_matches(path, raw_batches, dictionary)
    return matches, len(paragraphs), raw_batches


def render_report(matches_by_file, files_checked, out_path):
    from datetime import date
    total = sum(len(v) for v in matches_by_file.values())
    by_category = {}
    for ms in matches_by_file.values():
        for m in ms:
            by_category[m.category] = by_category.get(m.category, 0) + 1
    cat_line = " · ".join(f"{k} {v}" for k, v in sorted(by_category.items(), key=lambda x: -x[1]))

    # Rank rule IDs by frequency — Kyle uses this to tune disabled_rules
    rule_counts = {}
    files_per_rule = {}
    for path, ms in matches_by_file.items():
        for m in ms:
            rule_counts[m.rule_id] = rule_counts.get(m.rule_id, 0) + 1
            files_per_rule.setdefault(m.rule_id, set()).add(path)

    top_rules = sorted(rule_counts.items(), key=lambda x: -x[1])[:15]
    noisiest_lines = []
    for rid, count in top_rules:
        if not rid:
            continue
        fcount = len(files_per_rule[rid])
        noisiest_lines.append(f"- `{rid}` — {count} flag(s) across {fcount} file(s)")

    files_ranked = sorted(matches_by_file.items(), key=lambda x: -len(x[1]))[:10]
    noisiest_files = []
    for path, ms in files_ranked:
        if not ms:
            continue
        noisiest_files.append(f"- [[{path.stem}]] — {len(ms)} flag(s)")

    lines = [
        "---",
        "origin: ai",
        "publish: false",
        "tags:",
        "  - shard-gardens",
        "  - audit",
        "---",
        "",
        f"> [!note] Audit run {date.today().isoformat()}",
        f"> Files checked: **{files_checked}** · Issues flagged: **{total}**",
        f"> By category: {cat_line or 'none'}",
        "",
        "## How to read this",
        "",
        "- Flags grouped by file. Line numbers point to the original vault file.",
        "- Suggestions are LT's top 5. Use judgment — Kyle's voice wins.",
        "- Proper nouns already in the custom dict are suppressed. New false positives → add to `data/shard_gardens_dictionary.txt` and re-run.",
        "- **To suppress noisy rules:** add rule IDs (below) to `data/shard_gardens_disabled_rules.txt` and re-run `shard-audit` — it uses `--resume` cache by default so only changed files re-check.",
        "",
        "## Top rules by flag count (for tuning)",
        "",
    ] + (noisiest_lines or ["_(none)_"]) + [
        "",
        "## Noisiest files",
        "",
    ] + (noisiest_files or ["_(none)_"]) + [
        "",
        "## All flags by file",
        "",
    ]

    for path in sorted(matches_by_file):
        ms = matches_by_file[path]
        if not ms:
            continue
        rel = path.relative_to(VAULT_ROOT)
        title = path.stem
        lines.append(f"## [[{title}]]")
        lines.append(f"*{rel}* — {len(ms)} issue(s)")
        lines.append("")
        for m in ms:
            lines.append(f"### L{m.line} — {m.category or 'Other'}")
            lines.append(f"**{m.rule_id}** — {m.message}")
            lines.append("")
            lines.append(f"> …{m.context}…")
            lines.append("")
            if m.suggestions:
                suggs = ", ".join(f"`{s}`" for s in m.suggestions if s)
                lines.append(f"Suggest: {suggs}")
            else:
                lines.append("Suggest: _(none)_")
            lines.append("")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="Max files to check")
    p.add_argument("--file", type=str, action="append", default=None, help="Check a specific file. Repeat --file for multiple. Bypasses publish: true requirement.")
    p.add_argument("--dry-run", action="store_true", help="Strip and paragraph-split only, no API calls")
    p.add_argument("--resume", action="store_true", help="Skip files whose hash matches cache")
    p.add_argument("--force", action="store_true", help="Clear the resume cache and re-check every file")
    p.add_argument("--regenerate", action="store_true", help="Rebuild report from cached matches — no API calls. Applies current dictionary/disabled-rules/filters.")
    p.add_argument("--output", type=str, default=None, help="Override report path")
    args = p.parse_args()

    api_key = os.environ.get("LANGUAGETOOL_API_KEY")
    username = os.environ.get("LANGUAGETOOL_USERNAME")
    if not args.dry_run and not args.regenerate and not api_key:
        print("Error: LANGUAGETOOL_API_KEY not set. source ~/.env.sh first.", file=sys.stderr)
        sys.exit(1)

    dictionary = read_dictionary()
    print(f"Dictionary: {len(dictionary)} terms loaded from {DICT_PATH.name}")
    skip_patterns = read_skip_patterns()
    print(f"Skip list: {len(skip_patterns)} pattern(s) loaded from {SKIP_PATH.name}")

    if args.regenerate:
        matches_cache = load_matches_cache()
        print(f"Regenerating report from {len(matches_cache)} cached file(s) — no API calls")
        matches_by_file = {}
        for rel_str, entry in matches_cache.items():
            path = VAULT_ROOT / rel_str
            if not path.exists():
                continue
            matches = filter_and_build_matches(path, entry["raw_batches"], dictionary)
            matches_by_file[path] = matches
        from datetime import date
        out_path = Path(args.output) if args.output else REPORT_DIR / f"Shard Gardens Spelling Audit — {date.today().isoformat()}.md"
        render_report(matches_by_file, len(matches_by_file), out_path)
        print(f"\nReport: {out_path}")
        return

    if args.file:
        files = [Path(f).resolve() for f in args.file]
    else:
        all_files = find_published_notes(VAULT_ROOT)
        files = []
        skipped = 0
        for f in all_files:
            rel = str(f.relative_to(VAULT_ROOT))
            if is_skipped(rel, skip_patterns):
                skipped += 1
                continue
            files.append(f)
        print(f"Found {len(all_files)} published notes ({skipped} skipped, {len(files)} in scope)")

    if args.force:
        if CACHE_PATH.exists():
            CACHE_PATH.unlink()
        if MATCHES_CACHE_PATH.exists():
            MATCHES_CACHE_PATH.unlink()
        print("Cache cleared (--force)")
    cache = load_cache() if args.resume else {}
    matches_cache = load_matches_cache()
    if args.limit:
        files = files[:args.limit]

    matches_by_file = {}
    checked = 0
    for idx, path in enumerate(files, 1):
        rel = path.relative_to(VAULT_ROOT)
        fh = file_hash(path)
        if args.resume and cache.get(str(rel)) == fh:
            # Re-use cached raw matches if available
            entry = matches_cache.get(str(rel))
            if entry:
                matches_by_file[path] = filter_and_build_matches(path, entry["raw_batches"], dictionary)
            print(f"[{idx}/{len(files)}] skip (cached): {rel}")
            continue
        print(f"[{idx}/{len(files)}] {rel}")
        matches, n_paras, raw_batches = audit_file(path, username, api_key, dictionary, args.dry_run)
        matches_by_file[path] = matches
        cache[str(rel)] = fh
        matches_cache[str(rel)] = {"hash": fh, "raw_batches": raw_batches}
        checked += 1
        if matches:
            print(f"    {len(matches)} match(es) in {n_paras} para(s)")
        if idx % 10 == 0 and not args.dry_run:
            save_cache(cache)
            save_matches_cache(matches_cache)

    if not args.dry_run:
        save_cache(cache)
        save_matches_cache(matches_cache)

    if args.dry_run:
        print("\nDry run complete. No report written.")
        return

    from datetime import date
    out_path = Path(args.output) if args.output else REPORT_DIR / f"Shard Gardens Spelling Audit — {date.today().isoformat()}.md"
    render_report(matches_by_file, checked, out_path)
    print(f"\nReport: {out_path}")


if __name__ == "__main__":
    main()
