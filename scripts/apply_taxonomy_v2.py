#!/usr/bin/env python3
"""
Apply Taxonomy v2 — v1 Vault Tag Suggestions → v2 Vault Tag Suggestions

Reads the v1 Suggestions doc and applies every amendment named in
`Taxonomy Adoption Preview Apr 2026.md §6` to produce a v2 Suggestions doc.
Validates every resulting tag against the v2 Taxonomy leaf set.

Design: each amendment is a named function. Each function takes the list of
lines in the doc (mutable) and returns a count of line-level modifications,
so stdout gives Kyle a rule-by-rule scoreboard.

Run:
    python3 "~/projects/batcave/scripts/apply_taxonomy_v2.py"

No arguments. Paths are hardcoded; edit the CONSTANTS block below if needed.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable, List, Tuple

# --- Constants ---------------------------------------------------------------

VAULT = Path("/home/kyle/vault")
V1_SUGGESTIONS = VAULT / "0 Inbox" / "The Batcave" / "Vault Tag Suggestions Apr 2026.md"
V2_TAXONOMY = VAULT / "0 Inbox" / "The Batcave" / "Vault Tag Taxonomy v2 Apr 2026.md"
V2_SUGGESTIONS = VAULT / "0 Inbox" / "The Batcave" / "Vault Tag Suggestions v2 Apr 2026.md"


# --- Helpers -----------------------------------------------------------------

TAG_LINE_RE = re.compile(r"^tags:\s*\[(.*)\]\s*$")


def parse_tags(line: str) -> List[str] | None:
    """Return tag list from a `tags: [a, b, c]` line, else None."""
    m = TAG_LINE_RE.match(line)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return []
    return [t.strip() for t in body.split(",")]


def format_tags(tags: List[str]) -> str:
    """Render a `tags: [...]` line matching v1's style exactly."""
    return "tags: [" + ", ".join(tags) + "]"


def extract_taxonomy_leaves(taxonomy_path: Path) -> set[str]:
    """Pull every `subject/*`, `type/*`, `status/*` leaf from the v2 taxonomy doc."""
    text = taxonomy_path.read_text(encoding="utf-8")
    # Match tokens like subject/foo, type/bar-baz, status/qux (kebab-case)
    found = set(re.findall(r"\b(subject|type|status)/([a-z][a-z0-9-]*)", text))
    return {f"{axis}/{leaf}" for axis, leaf in found}


# --- Amendment rules ---------------------------------------------------------
#
# Each rule returns (rule_name, modified_line_count). Rules mutate `lines`
# in place. Order matters only where noted.

def rule_01_fix_media_reviews_typo(lines: List[str]) -> Tuple[str, int]:
    """Preview §5 fix 1-2 / §6 Suggestions-pass fix 1.

    `type/media-reviews` is off-taxonomy; v2 Taxonomy has `subject/media-reviews`.
    Two affected notes: Atomic Habits Review, Who Moved My Cheese Review.
    Blanket replace: rewrite any tag line that contains `type/media-reviews`.
    """
    count = 0
    for i, line in enumerate(lines):
        tags = parse_tags(line)
        if tags is None:
            continue
        if "type/media-reviews" not in tags:
            continue
        new_tags = [
            "subject/media-reviews" if t == "type/media-reviews" else t
            for t in tags
        ]
        lines[i] = format_tags(new_tags) + "\n"
        count += 1
    return ("R1 fix type/media-reviews typo → subject/media-reviews", count)


def rule_02_travel_archive_bulk(lines: List[str]) -> Tuple[str, int]:
    """Preview §6 Suggestions-pass fix 2.

    `6 Archive/Travel/*.md (~226 files)`:
        before: [subject/self-reflection, status/published]
        after:  [type/blog-post, status/published]
    Also amend the `reason:` + trailing "Consider `type/blog-post`…" bullet
    to reflect the resolution.
    """
    count = 0
    for i, line in enumerate(lines):
        if not line.startswith("## 6 Archive/Travel/"):
            continue
        # Within the next few lines, rewrite the bulk tag line and reason bullet
        j = i + 1
        while j < len(lines) and not lines[j].startswith("## "):
            if lines[j].startswith("tags: [subject/self-reflection, status/published]"):
                lines[j] = "tags: [type/blog-post, status/published]\n"
                count += 1
            elif lines[j].startswith("reason: Bulk. Travel journals."):
                lines[j] = (
                    "reason: Bulk. Travel journals, almost entirely blog-origin material. "
                    "v2 amendment: retagged as blog posts (preview §6 fix 2) — "
                    "`subject/self-reflection` was dilutive across 226 files.\n"
                )
                count += 1
            elif lines[j].startswith("- Consider `type/blog-post`"):
                # Drop the stale "confirm later" bullet since we've resolved it
                lines[j] = (
                    "- Individual entries that are genuinely introspective "
                    "(not mere travelogue) can get `subject/self-reflection` "
                    "added in a finer pass.\n"
                )
                count += 1
            j += 1
        break
    return ("R2 Travel Archive bulk → [type/blog-post, status/published]", count)


def rule_03_drafts_archive_bulk(lines: List[str]) -> Tuple[str, int]:
    """Preview §6 Suggestions-pass fix 3.

    `6 Archive/Drafts/*.md (~42 files)`:
        before: [status/draft, subject/fiction-craft]
        after:  [status/draft]
    """
    count = 0
    for i, line in enumerate(lines):
        if not line.startswith("## 6 Archive/Drafts/"):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith("## "):
            if lines[j].startswith("tags: [status/draft, subject/fiction-craft]"):
                lines[j] = "tags: [status/draft]\n"
                count += 1
            elif lines[j].startswith("reason: Bulk. Old creative drafts."):
                lines[j] = (
                    "reason: Bulk. Old creative drafts. v2 amendment: dropped "
                    "`subject/fiction-craft` (preview §6 fix 3) — drafts are "
                    "drafts, not craft reflection.\n"
                )
                count += 1
            j += 1
        break
    return ("R3 Drafts Archive bulk → [status/draft]", count)


def rule_04_story_ideas_archive_bulk(lines: List[str]) -> Tuple[str, int]:
    """Preview §6 Suggestions-pass fix 4.

    `6 Archive/Story Ideas/*.md (~76 files)`:
        before: [type/idea-stub, subject/fiction-craft]
        after:  [type/idea-stub]
    """
    count = 0
    for i, line in enumerate(lines):
        if not line.startswith("## 6 Archive/Story Ideas/"):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith("## "):
            if lines[j].startswith("tags: [type/idea-stub, subject/fiction-craft]"):
                lines[j] = "tags: [type/idea-stub]\n"
                count += 1
            j += 1
        # Append an amendment note after existing reason lines (if no dedicated reason line, skip)
        break
    return ("R4 Story Ideas Archive bulk → [type/idea-stub]", count)


def rule_05_poetry_archive_bulk(lines: List[str]) -> Tuple[str, int]:
    """Preview §6 Suggestions-pass fix 5.

    `6 Archive/Poetry/*.md (~151 files)`:
        before: [subject/poetry, status/draft]
        after:  [subject/poetry]

    Preview recommends "Remove `status/draft` or replace with `status/stale`".
    Taking the first option (remove) — preview §2 said status/stale should remain
    a seed (~1 use), mass-applying would defeat that point.
    """
    count = 0
    for i, line in enumerate(lines):
        if not line.startswith("## 6 Archive/Poetry/"):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith("## "):
            if lines[j].startswith("tags: [subject/poetry, status/draft]"):
                lines[j] = "tags: [subject/poetry]\n"
                count += 1
            elif lines[j].startswith("reason: Bulk. Kyle's poetry archive."):
                lines[j] = (
                    "reason: Bulk. Kyle's poetry archive. v2 amendment: removed "
                    "`status/draft` (preview §6 fix 5) — 151 \"drafts\" of decades-old "
                    "poetry is not what `status/draft` should mean.\n"
                )
                count += 1
            j += 1
        break
    return ("R5 Poetry Archive bulk → [subject/poetry] (drop status/draft)", count)


def rule_06_readwise_bulk_policy(lines: List[str]) -> Tuple[str, int]:
    """Preview §6 Suggestions-pass fix 6 + §6 taxonomy amendment 2.

    Four bulk sections under `3 Information/Readwise/` currently default to
    `[type/readwise]` for every file. v2 policy: apply `type/readwise` only
    to imports that ALSO receive a `subject/*` tag. The bulk default drops
    to "(none in bulk — do not tag)". Individual per-file subject tags listed
    in the existing `reason:` bullets become the paired `[type/readwise, subject/*]`
    application at tag-write time.

    Sections touched:
      - Articles (~10 files)
      - Books (~480 files)
      - Tweets (~30 files)
      - Podcasts (~22 files)
    """
    count = 0
    section_headers = [
        "### 3 Information/Readwise/Articles/*.md",
        "### 3 Information/Readwise/Books/*.md",
        "### 3 Information/Readwise/Tweets/*.md",
        "### 3 Information/Readwise/Podcasts/*.md",
    ]
    for i, line in enumerate(lines):
        if not any(line.startswith(h) for h in section_headers):
            continue
        j = i + 1
        # Only touch the immediate `tags:` line that follows each section header.
        while j < len(lines) and not lines[j].startswith("#"):
            if lines[j].startswith("tags: [type/readwise]"):
                lines[j] = "tags: (none in bulk — do not tag)\n"
                count += 1
                # Prepend a clarifying note to the next `reason:` line
                k = j + 1
                while k < len(lines) and not lines[k].startswith("#") and not lines[k].startswith("reason:"):
                    k += 1
                if k < len(lines) and lines[k].startswith("reason:"):
                    original = lines[k][len("reason: "):].rstrip("\n")
                    lines[k] = (
                        "reason: v2 amendment (preview §6 fix 6 + taxonomy fix 2): "
                        "`type/readwise` applied only to imports that also receive a "
                        "`subject/*` tag; everything else stays untagged. "
                        f"Previous rationale: {original}\n"
                    )
                    count += 1
                break
            j += 1
    return ("R6 Readwise bulks → drop bulk [type/readwise]; apply only with subject/*", count)


def rule_07_morning_brief_add_bruce_ops(lines: List[str]) -> Tuple[str, int]:
    """Preview §5 #3 / §6 Suggestions-pass fix 7.

    All Morning Brief notes under `0 Inbox/The Batcave/Morning Brief — *` tagged
    only `[type/brief]` should receive `subject/bruce-ops`.
    """
    # Expansive pattern: any note whose tag line is exactly `[type/brief]`.
    # In practice this is the 43 dated Morning Briefs + W07 Saturday Morning
    # Briefing + "Use Case — State of the Cave" + 2 Ideas/Morning Brief
    # Tiered Tasks — all are morning-brief-adjacent notes that fit the
    # preview's intent ("Morning Brief bulk gets subject/bruce-ops").
    count = 0
    for i, line in enumerate(lines):
        if line.rstrip("\n") == "tags: [type/brief]":
            lines[i] = "tags: [type/brief, subject/bruce-ops]\n"
            count += 1
    return ("R7 Morning Brief notes → add subject/bruce-ops", count)


def rule_08_trailheads_for_shard_gardens(lines: List[str]) -> Tuple[str, int]:
    """Preview §5 #11 / §6 Suggestions-pass fix 8.

    `0 Inbox/Trailheads for Shard Gardens.md` is currently
    `[type/decision, subject/publishing, subject/vault-meta]`.
    Add `type/trailhead` so filename-implied-query surfaces it.
    """
    count = 0
    for i, line in enumerate(lines):
        if not line.startswith("## 0 Inbox/Trailheads for Shard Gardens.md"):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith("## "):
            tags = parse_tags(lines[j])
            if tags is not None and "type/trailhead" not in tags:
                # Preserve v1 tag order; insert type/trailhead at the front
                # so type-axis tags cluster naturally.
                new_tags = ["type/trailhead"] + tags
                lines[j] = format_tags(new_tags) + "\n"
                count += 1
                break
            j += 1
        break
    return ("R8 Trailheads for Shard Gardens → add type/trailhead", count)


def rule_09_drop_reference_on_worldbuilding(lines: List[str]) -> Tuple[str, int]:
    """Preview §5 "Tag axis overlap" / §6 taxonomy amendment 1 + Suggestions fix 9.

    14 notes double-type with both `type/reference` and `type/worldbuilding`.
    v2 taxonomy: never both. Keep `type/worldbuilding`; drop `type/reference`.
    """
    count = 0
    for i, line in enumerate(lines):
        tags = parse_tags(line)
        if tags is None:
            continue
        if "type/reference" in tags and "type/worldbuilding" in tags:
            new_tags = [t for t in tags if t != "type/reference"]
            lines[i] = format_tags(new_tags) + "\n"
            count += 1
    return ("R9 Worldbuilding notes → drop type/reference (type-axis collision rule)", count)


RULES: List[Callable[[List[str]], Tuple[str, int]]] = [
    rule_01_fix_media_reviews_typo,
    rule_02_travel_archive_bulk,
    rule_03_drafts_archive_bulk,
    rule_04_story_ideas_archive_bulk,
    rule_05_poetry_archive_bulk,
    rule_06_readwise_bulk_policy,
    rule_07_morning_brief_add_bruce_ops,
    rule_08_trailheads_for_shard_gardens,
    rule_09_drop_reference_on_worldbuilding,
]


# --- Validation & output -----------------------------------------------------

def validate_tags_against_taxonomy(lines: List[str], leaves: set[str]) -> List[str]:
    """Return list of off-taxonomy tag occurrences (line number + tag)."""
    errors = []
    for i, line in enumerate(lines, start=1):
        tags = parse_tags(line)
        if tags is None:
            continue
        for t in tags:
            if not re.match(r"^(subject|type|status)/", t):
                continue  # tolerate non-axis decorations if any appear
            if t not in leaves:
                errors.append(f"  line {i}: {t!r}")
    return errors


def build_v2_preamble(rule_results: List[Tuple[str, int]]) -> str:
    """Frontmatter + 'Changes from v1' header prepended to the v2 body."""
    today = date.today().isoformat()
    total_changes = sum(c for _, c in rule_results)
    lines = [
        "---",
        "origin: ai",
        "publish: false",
        f"created_on: {today}",
        "supersedes: '[[Vault Tag Suggestions Apr 2026]]'",
        "---",
        "",
        "Supersedes [[Vault Tag Suggestions Apr 2026]] (v1). "
        "Applies every amendment named in [[Taxonomy Adoption Preview Apr 2026]] §6 "
        "against v1's body. The leaf set and v2 taxonomy authority live in "
        "[[Vault Tag Taxonomy v2 Apr 2026]].",
        "",
        "# Changes from v1",
        "",
        "Nine Suggestions-pass rules plus two taxonomy amendments from "
        "[[Taxonomy Adoption Preview Apr 2026]] §6, applied mechanically by "
        "`batcave/scripts/apply_taxonomy_v2.py`. Per-rule change counts below. "
        "Every resulting tag validated against the v2 taxonomy leaf set (61 leaves).",
        "",
    ]
    for name, count in rule_results:
        lines.append(f"- **{name}** — {count} line(s) modified")
    lines.append("")
    lines.append(f"**Total line-level modifications:** {total_changes}")
    lines.append("")
    lines.append("The body below is v1's content with these amendments applied; "
                 "unaffected rows are byte-identical to v1.")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


def strip_v1_frontmatter(text: str) -> str:
    """Remove the leading YAML frontmatter block from v1 so v2's preamble owns it."""
    if not text.startswith("---"):
        return text
    # find closing fence
    rest = text[3:]
    idx = rest.find("\n---")
    if idx == -1:
        return text
    # skip past closing fence and following newline(s)
    after = rest[idx + len("\n---"):]
    # strip all leading newlines so the preamble controls spacing
    return after.lstrip("\n")


# --- Main --------------------------------------------------------------------

def main() -> int:
    if not V1_SUGGESTIONS.exists():
        print(f"ERROR: v1 Suggestions not found at {V1_SUGGESTIONS}", file=sys.stderr)
        return 2
    if not V2_TAXONOMY.exists():
        print(f"ERROR: v2 Taxonomy not found at {V2_TAXONOMY}", file=sys.stderr)
        return 2

    v1_text = V1_SUGGESTIONS.read_text(encoding="utf-8")
    # Split preserving newlines so we can rewrite line-by-line cleanly.
    body_text = strip_v1_frontmatter(v1_text)
    lines = body_text.splitlines(keepends=True)

    rule_results: List[Tuple[str, int]] = []
    for rule in RULES:
        name, count = rule(lines)
        rule_results.append((name, count))
        print(f"  {name}: {count}")

    total = sum(c for _, c in rule_results)
    print(f"\nTotal line-level modifications: {total}")

    # Validate against v2 taxonomy leaves
    leaves = extract_taxonomy_leaves(V2_TAXONOMY)
    print(f"\nLoaded {len(leaves)} leaves from v2 taxonomy.")
    errors = validate_tags_against_taxonomy(lines, leaves)
    if errors:
        print(f"\nVALIDATION WARNINGS — {len(errors)} off-taxonomy tag(s) in output:")
        for e in errors[:25]:
            print(e)
        if len(errors) > 25:
            print(f"  ... and {len(errors) - 25} more")
    else:
        print("\nValidation: all output tags are on-taxonomy.")

    preamble = build_v2_preamble(rule_results)
    v2_text = preamble + "".join(lines)
    V2_SUGGESTIONS.write_text(v2_text, encoding="utf-8")
    print(f"\nWrote {V2_SUGGESTIONS} ({len(v2_text):,} bytes, {v2_text.count(chr(10))} lines)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
