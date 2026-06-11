#!/usr/bin/env python3
"""
todoist_brief.py — sanitized Todoist snapshot for morning-brief / review-prep consumers.

Outputs JSON by default (newline-delimited sections or a single blob).
Emoji and unicode project prefixes are stripped so the output stays inside the
character set Claude Code auto-allows. The brief format categorizes tasks into
four tiers: overdue / today / important-not-urgent / upcoming.

Usage:
    todoist-brief                       # JSON blob for today + upcoming week
    todoist-brief --markdown            # human-readable markdown
    todoist-brief --days 14             # wider upcoming window
    todoist-brief --project Batcave     # filter to a single project
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

API_BASE = "https://api.todoist.com/api/v1"
TZ = timezone(timedelta(hours=-6))  # America/Denver-ish; deadlines don't need DST precision here

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001FA70-\U0001FAFF"  # extended symbols
    "]+",
    flags=re.UNICODE,
)


def token() -> str:
    t = os.environ.get("TODOIST_API_TOKEN")
    if not t:
        sys.stderr.write("TODOIST_API_TOKEN not set. Source ~/.env.sh first.\n")
        sys.exit(2)
    return t


def api_get(path: str, params: dict | None = None) -> Any:
    url = f"{API_BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def clean(s: str) -> str:
    if not s:
        return ""
    s = EMOJI_RE.sub("", s)
    # drop variation selectors and other format controls left after emoji removal
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = unicodedata.normalize("NFKC", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def get_projects_map() -> dict[str, str]:
    data = api_get("projects")
    items = data.get("results", data) if isinstance(data, dict) else data
    return {p["id"]: clean(p["name"]) for p in items}


def get_active_tasks(filter_project_id: str | None = None) -> list[dict]:
    # Todoist v1 returns paginated results
    all_tasks: list[dict] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        if filter_project_id:
            params["project_id"] = filter_project_id
        data = api_get("tasks", params)
        page = data.get("results", data) if isinstance(data, dict) else data
        all_tasks.extend(page)
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor:
            break
    return all_tasks


def parse_due(due: dict | None) -> date | None:
    if not due:
        return None
    d = due.get("date")
    if not d:
        return None
    try:
        if "T" in d:
            return datetime.fromisoformat(d.replace("Z", "+00:00")).astimezone(TZ).date()
        return date.fromisoformat(d[:10])
    except ValueError:
        return None


def categorize(tasks: list[dict], today: date, upcoming_days: int, projects: dict[str, str]) -> dict:
    overdue, due_today, important, upcoming = [], [], [], []
    horizon = today + timedelta(days=upcoming_days)

    for t in tasks:
        due_date = parse_due(t.get("due"))
        priority = t.get("priority", 1)  # Todoist: 1=lowest, 4=highest (P1)
        project = projects.get(t.get("project_id", ""), "?")
        entry = {
            "id": t.get("id"),
            "content": clean(t.get("content", "")),
            "project": project,
            "priority": priority,
            "labels": [clean(l) for l in t.get("labels", [])],
            "due": due_date.isoformat() if due_date else None,
            "is_recurring": bool(t.get("due", {}).get("is_recurring")) if t.get("due") else False,
            "url": t.get("url"),
        }

        if due_date and due_date < today:
            overdue.append(entry)
        elif due_date == today:
            due_today.append(entry)
        elif priority >= 3 and (not due_date or due_date <= horizon):
            # P2+ without a pressing deadline → "important, not urgent"
            important.append(entry)
        elif due_date and today < due_date <= horizon:
            upcoming.append(entry)

    # sort each tier
    def sort_key(e):
        return (-(e["priority"]), e["due"] or "9999-99-99", e["content"])

    for lst in (overdue, due_today, important, upcoming):
        lst.sort(key=sort_key)

    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "today": today.isoformat(),
        "do_now": overdue,
        "do_today": due_today,
        "important_not_urgent": important,
        "upcoming": upcoming,
    }


def render_markdown(brief: dict) -> str:
    out = [f"# Todoist Brief — {brief['today']}", ""]
    sections = [
        ("Do Now (Overdue)", brief["do_now"]),
        ("Do Today", brief["do_today"]),
        ("Important, Not Urgent", brief["important_not_urgent"]),
        ("Upcoming", brief["upcoming"]),
    ]
    for title, items in sections:
        out.append(f"## {title}")
        if not items:
            out.append("_(none)_")
        else:
            for e in items:
                prio = f"P{5 - e['priority']}" if e["priority"] > 1 else ""
                bits = [e["content"]]
                meta = []
                if prio:
                    meta.append(prio)
                meta.append(e["project"])
                if e["due"]:
                    meta.append(e["due"])
                if e["is_recurring"]:
                    meta.append("recurring")
                if e["labels"]:
                    meta.append(" ".join(f"@{l}" for l in e["labels"]))
                bits.append(f"({', '.join(meta)})")
                out.append(f"- {' '.join(bits)}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markdown", action="store_true", help="Render as markdown instead of JSON")
    ap.add_argument("--days", type=int, default=7, help="Upcoming window in days (default 7)")
    ap.add_argument("--project", help="Filter to a single project by name")
    args = ap.parse_args()

    projects = get_projects_map()
    project_id = None
    if args.project:
        matches = [pid for pid, name in projects.items() if name.lower() == args.project.lower()]
        if not matches:
            sys.stderr.write(f"Project not found: {args.project}\n")
            return 1
        project_id = matches[0]

    tasks = get_active_tasks(filter_project_id=project_id)
    today = datetime.now(TZ).date()
    brief = categorize(tasks, today, args.days, projects)

    if args.markdown:
        print(render_markdown(brief))
    else:
        print(json.dumps(brief, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
