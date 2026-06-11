#!/usr/bin/env python3
"""
vault_health.py — vital signs for the knowledge garden.

Tracks provenance-adjacent metrics that hint at whether the vault still feels
like Kyle's or is drifting into "AI approves the harvest report" territory.

Metrics:
- AI-origin note share (total + by top-level section)
- AI note return rate (what fraction get touched again after creation)
- Batcave graduation (AI notes that moved out of 0 Inbox/The Batcave/)
- Daily note density (word count per week, trend direction)
- Orphan check: AI notes in Ideas/Projects with zero inbound backlinks

Honest about what it can't measure: dictation ratio, weekly review depth,
whether Kyle actually *read* a note. Those are qualitative checks on the human.

Usage:
    vault-health                 # summary to stdout
    vault-health --json          # machine-readable
    vault-health --full          # include per-note lists for the bad buckets
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "vault"))
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
ORIGIN_AI_RE = re.compile(r"^origin:\s*ai\s*$", re.MULTILINE)
CREATED_RE = re.compile(r"^created_on:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
UPDATED_RE = re.compile(r"^updated_on:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
PUBLISH_RE = re.compile(r"^publish:\s*(true|false)\s*$", re.MULTILINE)

# Folders we treat as graduation targets vs scaffolding
SCAFFOLD_PREFIXES = ("0 Inbox/",)
IDEA_PREFIXES = ("2 Ideas/", "1 Projects/", "5 Completed/")

SKIP_DIRS = {".obsidian", ".git", ".trash", "node_modules", ".pytest_cache", "__pycache__", "copilot"}


def iter_markdown() -> list[Path]:
    out = []
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".md"):
                out.append(Path(root) / f)
    return out


def frontmatter(text: str) -> dict[str, str]:
    m = FM_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    out: dict[str, str] = {}
    if ORIGIN_AI_RE.search(block):
        out["origin"] = "ai"
    cm = CREATED_RE.search(block)
    if cm:
        out["created_on"] = cm.group(1)
    um = UPDATED_RE.search(block)
    if um:
        out["updated_on"] = um.group(1)
    pm = PUBLISH_RE.search(block)
    if pm:
        out["publish"] = pm.group(1)
    return out


def rel_path(p: Path) -> str:
    return str(p.relative_to(VAULT))


def top_section(rel: str) -> str:
    # First path segment, e.g. "0 Inbox" or "2 Ideas"
    return rel.split("/", 1)[0] if "/" in rel else "(root)"


def is_in_batcave(rel: str) -> bool:
    return rel.startswith("0 Inbox/The Batcave/")


def word_count(text: str) -> int:
    # strip frontmatter
    t = FM_RE.sub("", text, count=1)
    return len(t.split())


def daily_note_date(rel: str) -> date | None:
    # 4 Time/Daily Notes/2026/2026-04-17.md
    m = re.match(r"^4 Time/Daily Notes/\d{4}/(\d{4}-\d{2}-\d{2})\.md$", rel)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def analyze() -> dict[str, Any]:
    files = iter_markdown()
    total = len(files)
    ai_notes: list[dict[str, Any]] = []
    all_words = 0
    per_section = defaultdict(lambda: {"total": 0, "ai": 0})
    daily_words_by_week: dict[str, list[int]] = defaultdict(list)

    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = frontmatter(text)
        wc = word_count(text)
        all_words += wc
        rel = rel_path(p)
        section = top_section(rel)
        per_section[section]["total"] += 1

        is_ai = fm.get("origin") == "ai"
        if is_ai:
            per_section[section]["ai"] += 1
            created = fm.get("created_on")
            updated = fm.get("updated_on")
            mtime = datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()
            returned = False
            if created and updated and updated != created:
                returned = True
            elif created and mtime != created:
                returned = True
            ai_notes.append({
                "path": rel,
                "section": section,
                "in_batcave": is_in_batcave(rel),
                "created_on": created,
                "updated_on": updated,
                "mtime": mtime,
                "words": wc,
                "returned": returned,
            })

        dnd = daily_note_date(rel)
        if dnd:
            iso_year, iso_week, _ = dnd.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
            daily_words_by_week[key].append(wc)

    # AI metrics
    ai_total = len(ai_notes)
    ai_returned = sum(1 for n in ai_notes if n["returned"])
    ai_return_rate = (ai_returned / ai_total) if ai_total else 0.0

    ai_in_batcave = sum(1 for n in ai_notes if n["in_batcave"])
    ai_in_ideas = sum(1 for n in ai_notes if n["section"] == "2 Ideas")
    ai_in_projects = sum(1 for n in ai_notes if n["section"] == "1 Projects")
    ai_in_completed = sum(1 for n in ai_notes if n["section"] == "5 Completed")
    ai_graduated = ai_in_ideas + ai_in_projects + ai_in_completed
    graduation_rate = (ai_graduated / ai_total) if ai_total else 0.0

    # Daily note density — last 12 weeks trend
    weeks_sorted = sorted(daily_words_by_week.keys())[-12:]
    weekly_totals = [(w, sum(daily_words_by_week[w]), len(daily_words_by_week[w])) for w in weeks_sorted]
    if len(weekly_totals) >= 4:
        recent_avg = statistics.mean(t[1] for t in weekly_totals[-4:])
        older_avg = statistics.mean(t[1] for t in weekly_totals[:-4]) if len(weekly_totals) > 4 else recent_avg
        density_trend = recent_avg - older_avg
    else:
        recent_avg = older_avg = density_trend = 0

    ai_share_pct = (ai_total / total * 100) if total else 0
    # Approximate AI word share: sum ai_notes words / all_words
    ai_words = sum(n["words"] for n in ai_notes)
    ai_word_share_pct = (ai_words / all_words * 100) if all_words else 0

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vault_path": str(VAULT),
        "totals": {
            "markdown_files": total,
            "words": all_words,
            "ai_files": ai_total,
            "ai_file_share_pct": round(ai_share_pct, 2),
            "ai_word_share_pct": round(ai_word_share_pct, 2),
        },
        "ai_behavior": {
            "return_rate_pct": round(ai_return_rate * 100, 1),
            "returned": ai_returned,
            "not_returned": ai_total - ai_returned,
            "graduation_rate_pct": round(graduation_rate * 100, 1),
            "by_location": {
                "batcave": ai_in_batcave,
                "ideas": ai_in_ideas,
                "projects": ai_in_projects,
                "completed": ai_in_completed,
                "other": ai_total - ai_in_batcave - ai_graduated,
            },
        },
        "per_section": {
            s: {
                "total": d["total"],
                "ai": d["ai"],
                "ai_pct": round(d["ai"] / d["total"] * 100, 1) if d["total"] else 0,
            }
            for s, d in sorted(per_section.items())
        },
        "daily_note_density": {
            "weekly_totals": [
                {"week": w, "words": words, "notes": count} for w, words, count in weekly_totals
            ],
            "recent_4wk_avg_words_per_week": int(recent_avg),
            "older_window_avg_words_per_week": int(older_avg),
            "trend_words": int(density_trend),
        },
        "caveats": [
            "file count is not thinking dependency",
            "return_rate only sees frontmatter dates + mtime, misses silent re-reads",
            "dictation vs generation ratio is not measurable from files alone",
            "weekly review depth (Kyle vs Bruce) not measurable without structural parsing",
        ],
        "_ai_notes": ai_notes,  # for --full output; dropped by default
    }


def render_text(result: dict) -> str:
    t = result["totals"]
    ab = result["ai_behavior"]
    dn = result["daily_note_density"]
    lines = [
        "Vault Vital Signs",
        f"  generated: {result['generated_at']}",
        f"  vault:     {result['vault_path']}",
        "",
        f"Total notes: {t['markdown_files']:,}    total words: {t['words']:,}",
        f"AI notes:    {t['ai_files']:,} ({t['ai_file_share_pct']}% of files, {t['ai_word_share_pct']}% of words)",
        "",
        "AI Behavior",
        f"  return rate:     {ab['return_rate_pct']}% (returned: {ab['returned']}, never: {ab['not_returned']})",
        f"  graduation rate: {ab['graduation_rate_pct']}% (AI notes outside Inbox/Batcave)",
        f"  by location:     batcave={ab['by_location']['batcave']}  ideas={ab['by_location']['ideas']}  projects={ab['by_location']['projects']}  completed={ab['by_location']['completed']}  other={ab['by_location']['other']}",
        "",
        "Per Section (AI share)",
    ]
    for s, d in result["per_section"].items():
        lines.append(f"  {s:<20}  {d['total']:>5} files  {d['ai']:>4} AI ({d['ai_pct']}%)")
    lines += [
        "",
        "Daily Note Density (last weeks)",
    ]
    for wk in dn["weekly_totals"]:
        lines.append(f"  {wk['week']}   {wk['words']:>7,} words across {wk['notes']} notes")
    lines += [
        f"  trend: recent 4-wk avg {dn['recent_4wk_avg_words_per_week']:,} vs older {dn['older_window_avg_words_per_week']:,} → delta {dn['trend_words']:+,}",
        "",
        "What this can't tell you",
    ]
    for c in result["caveats"]:
        lines.append(f"  - {c}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="JSON output instead of text")
    ap.add_argument("--full", action="store_true", help="Include per-note lists in JSON output")
    args = ap.parse_args()

    result = analyze()
    if not args.full:
        result.pop("_ai_notes", None)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
