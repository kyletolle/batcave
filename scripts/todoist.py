#!/usr/bin/env python3
"""Todoist CLI — Bruce's interface to Kyle's task list.

Usage:
    todoist.py projects                          List all projects
    todoist.py list [--project NAME] [--limit N] List active tasks (grouped by section)
    todoist.py today                             Tasks due today + overdue
    todoist.py due DATE [--project NAME]         Tasks due on a specific date (today, tomorrow, friday, +3, 2026-04-10)
    todoist.py week                              Tasks due this week
    todoist.py add CONTENT [options]             Create a task (supports --deadline)
    todoist.py update TASK_ID [options]          Update a task (supports --deadline, --no-deadline)
    todoist.py postpone TASK_ID DATE             Reschedule (recurring or one-off)
    todoist.py complete TASK_ID                  Complete a task
    todoist.py uncomplete TASK_ID                Reopen a completed task (undo)
    todoist.py search QUERY                      Search tasks by content
    todoist.py sections [--project NAME]         List sections in a project
    todoist.py add-section NAME [--project NAME] Create a new section
    todoist.py move-section SECTION ID [ID ...]  Move task(s) to a section
    todoist.py reminders TASK_ID                 List reminders for a task
    todoist.py add-reminder TASK_ID OFFSET [...] Add reminder(s) before due date
    todoist.py remove-reminder REMINDER_ID       Delete a reminder
    todoist.py add-project NAME [options]        Create a project (--color, --parent)
    todoist.py rename-project NAME NEW_NAME      Rename a project
    todoist.py delete-project NAME --yes         Delete a project (irreversible)
    todoist.py bulk FILE [--dry-run]             Run many ops from JSON (each logged)

All mutations are logged to todoist_audit.jsonl (append-only JSONL, auto-rotated at 5 MB).
NEVER route mutations around this script (no raw curl/API for writes) — bulk especially.
Use 'bulk' for batch add/update/move-section; every op lands in the audit log.
Writes are NOT restricted to any single project; --project targets any project, and
the project commands above operate account-wide. Bare 'add'/'add-section' still default
to the Batcave project for convenience when --project is omitted.
Completing a parent task requires --cascade flag to prevent accidental subtask loss.

IMPORTANT: Prefer 'postpone' over 'update --due' for rescheduling.
'postpone' handles both recurring and one-off tasks correctly.
For recurring tasks, 'update --due' destroys the recurrence pattern,
while 'postpone' preserves it.

Requires TODOIST_API_TOKEN in environment (source ~/.env.sh).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

import requests

API_URL = "https://api.todoist.com/api/v1"
TOKEN = os.environ.get("TODOIST_API_TOKEN")
BATCAVE_PROJECT = "Batcave"  # Default project for commands when --project is omitted.


def check_token():
    if not TOKEN:
        print("ERROR: TODOIST_API_TOKEN not found in environment.")
        print("Run: source ~/.env.sh")
        sys.exit(1)


def headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def api_get(endpoint, params=None):
    """GET with automatic cursor pagination."""
    all_results = []
    p = dict(params) if params else {}
    while True:
        resp = requests.get(f"{API_URL}/{endpoint}", headers=headers(), params=p)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "results" in data:
            all_results.extend(data["results"])
            cursor = data.get("next_cursor")
            if cursor:
                p["cursor"] = cursor
            else:
                break
        else:
            return data
    return all_results


def api_post(endpoint, json_data=None):
    resp = requests.post(f"{API_URL}/{endpoint}", headers=headers(), json=json_data)
    resp.raise_for_status()
    if resp.status_code == 204:
        return None
    return resp.json()


def api_delete(endpoint):
    resp = requests.delete(f"{API_URL}/{endpoint}", headers=headers())
    resp.raise_for_status()
    if resp.status_code in (200, 204) and not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# --- Project helpers ---

_project_cache = None


def get_projects():
    global _project_cache
    if _project_cache is None:
        _project_cache = api_get("projects")
    return _project_cache


def find_project(name):
    """Find a project by name (case-insensitive partial match)."""
    projects = get_projects()
    name_lower = name.lower()
    # Exact match first
    for p in projects:
        if p["name"].lower() == name_lower:
            return p
    # Partial match
    for p in projects:
        if name_lower in p["name"].lower():
            return p
    return None


def project_map():
    return {p["id"]: p["name"] for p in get_projects()}


# --- Section helpers ---

_section_cache = {}


def get_sections(project_id):
    """Get all sections for a project (cached)."""
    if project_id not in _section_cache:
        _section_cache[project_id] = api_get("sections", {"project_id": project_id})
    return _section_cache[project_id]


def find_section(project_id, name):
    """Find a section by name (case-insensitive partial match)."""
    sections = get_sections(project_id)
    name_lower = name.lower()
    for s in sections:
        if s["name"].lower() == name_lower:
            return s
    for s in sections:
        if name_lower in s["name"].lower():
            return s
    return None


def section_map(project_id):
    """Map section IDs to names for a project."""
    return {s["id"]: s["name"] for s in get_sections(project_id)}


# --- Display helpers ---
def parse_due_date(due):
    """Extract a date object from a task's due dict."""
    if not due:
        return None
    date_str = due.get("date", "")
    if not date_str:
        return None
    if "T" in date_str:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def format_task(t, pmap, show_recurring=True):
    """Format a single task for display."""
    proj_name = pmap.get(t.get("project_id"), "???")
    due = t.get("due")
    due_str = due.get("date", "") if due else ""
    deadline = t.get("deadline")
    priority = t.get("priority", 1)
    p_marker = "!" * (priority - 1) if priority > 1 else " "
    labels = t.get("labels", [])
    label_str = " " + " ".join(f"#{l}" for l in labels) if labels else ""

    recur_str = ""
    if show_recurring and due:
        if due.get("is_recurring"):
            recur_str = f" [RECURRING: {due.get('string', '?')}]"
        else:
            recur_str = " [ONE-OFF]"

    lines = [f"  {p_marker} [{proj_name}] {t['content']}{recur_str}{label_str}"]
    if t.get("description"):
        desc = t["description"][:80]
        lines.append(f"      desc: {desc}")
    if due_str:
        lines.append(f"      due: {due_str}")
    if deadline:
        lines.append(f"      deadline: {deadline.get('date', '?')}")
    lines.append(f"      id: {t['id']}")
    return "\n".join(lines)


# --- Audit log ---

AUDIT_LOG = Path.home() / "vault" / "3 Information" / "Scripts" / "todoist_audit.jsonl"
AUDIT_MAX_BYTES = 5 * 1024 * 1024  # rotate once the log crosses 5 MB
AUDIT_KEEP = 3                     # keep todoist_audit.jsonl.1 .. .3


def rotate_audit_log():
    """Roll the audit log when it exceeds AUDIT_MAX_BYTES.

    Shifts .2 -> .3, .1 -> .2, live -> .1, then a fresh live file is created
    on the next append. Best-effort: rotation failures never block a mutation.
    """
    try:
        if not AUDIT_LOG.exists() or AUDIT_LOG.stat().st_size < AUDIT_MAX_BYTES:
            return
        oldest = AUDIT_LOG.with_suffix(AUDIT_LOG.suffix + f".{AUDIT_KEEP}")
        if oldest.exists():
            oldest.unlink()
        for i in range(AUDIT_KEEP - 1, 0, -1):
            src = AUDIT_LOG.with_suffix(AUDIT_LOG.suffix + f".{i}")
            if src.exists():
                src.rename(AUDIT_LOG.with_suffix(AUDIT_LOG.suffix + f".{i + 1}"))
        AUDIT_LOG.rename(AUDIT_LOG.with_suffix(AUDIT_LOG.suffix + ".1"))
    except OSError as e:
        print(f"  WARNING: Could not rotate audit log: {e}", file=sys.stderr)


def task_snapshot(task):
    """Extract the fields worth logging from a task dict."""
    if not task:
        return None
    return {
        "id": task.get("id"),
        "content": task.get("content"),
        "project_id": task.get("project_id"),
        "section_id": task.get("section_id"),
        "parent_id": task.get("parent_id"),
        "due": task.get("due"),
        "deadline": task.get("deadline"),
        "priority": task.get("priority"),
        "labels": task.get("labels"),
        "description": task.get("description", "")[:200],
    }


def log_mutation(action, task_before=None, task_after=None, extra=None):
    """Append one JSONL line to the audit log."""
    entry = {
        "ts": datetime.now().astimezone().isoformat(),
        "action": action,
        "before": task_snapshot(task_before),
        "after": task_snapshot(task_after),
    }
    if extra:
        entry["extra"] = extra
    try:
        rotate_audit_log()
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        print(f"  WARNING: Could not write audit log: {e}", file=sys.stderr)


def get_subtasks(parent_id, all_tasks=None):
    """Find all direct children of a task."""
    if all_tasks is None:
        all_tasks = api_get("tasks")
    return [t for t in all_tasks if t.get("parent_id") == parent_id]


# --- Commands ---

def cmd_projects(args):
    projects = get_projects()
    print(f"Found {len(projects)} projects:\n")
    for p in projects:
        print(f"  {p['name']:30s}  id: {p['id']}")


def cmd_list(args):
    pmap = project_map()

    params = {}
    proj = None
    if args.project:
        proj = find_project(args.project)
        if not proj:
            print(f"No project matching '{args.project}'. Use 'todoist.py projects' to see all.")
            sys.exit(1)
        params["project_id"] = proj["id"]
    else:
        print("All active tasks:\n")

    tasks = api_get("tasks", params)
    limit = args.limit or len(tasks)
    shown = tasks[:limit]

    # If viewing a single project, group by section
    if proj:
        sections = get_sections(proj["id"])
        smap = {s["id"]: s["name"] for s in sections}
        section_order = [s["id"] for s in sections]

        by_section = defaultdict(list)
        for t in shown:
            sid = t.get("section_id")
            by_section[sid].append(t)

        header = f"Tasks in {proj['name']}"

        # Count for header
        total = len(shown)
        section_count = len(sections)
        print(f"{header} ({total} tasks, {section_count} sections):\n")

        # Unsectioned tasks first
        unsectioned = by_section.pop(None, []) + by_section.pop("", [])
        if unsectioned:
            print(f"  ── (No section) ── ({len(unsectioned)} tasks)\n")
            for t in unsectioned:
                print(format_task(t, pmap))
            print()

        # Then by section order
        for sid in section_order:
            section_tasks = by_section.get(sid, [])
            section_name = smap.get(sid, "???")
            print(f"  ── {section_name} ── ({len(section_tasks)} tasks)\n")
            for t in section_tasks:
                print(format_task(t, pmap))
            print()
    else:
        for t in shown:
            print(format_task(t, pmap))

    if len(tasks) > limit:
        print(f"\n  ... and {len(tasks) - limit} more")


def cmd_today(args):
    """Show tasks due today and overdue."""
    pmap = project_map()
    tasks = api_get("tasks")
    today = datetime.now().date()

    overdue = []
    due_today = []

    for t in tasks:
        d = parse_due_date(t.get("due"))
        if d is not None:
            if d < today:
                overdue.append(t)
            elif d == today:
                due_today.append(t)

    # Sort by priority (highest first), then by due time
    def sort_key(t):
        return (-t.get("priority", 1), t.get("due", {}).get("date", ""))

    if overdue:
        overdue.sort(key=sort_key)
        print(f"=== OVERDUE ({len(overdue)} tasks) ===\n")
        for t in overdue:
            print(format_task(t, pmap))
        print()

    due_today.sort(key=sort_key)
    day_name = today.strftime("%A")
    print(f"=== TODAY {today} ({day_name}) — {len(due_today)} tasks ===\n")
    for t in due_today:
        print(format_task(t, pmap))

    total = len(overdue) + len(due_today)
    print(f"\nTotal: {total} tasks ({len(overdue)} overdue + {len(due_today)} today)")


def cmd_due(args):
    """Show tasks due on a specific date. Accepts: today, tomorrow, friday, +3, 2026-04-10."""
    pmap = project_map()

    target_iso = resolve_deadline_date(args.date)
    target = datetime.strptime(target_iso, "%Y-%m-%d").date()

    params = {}
    proj = None
    if args.project:
        proj = find_project(args.project)
        if not proj:
            print(f"No project matching '{args.project}'. Use 'todoist.py projects' to see all.")
            sys.exit(1)
        params["project_id"] = proj["id"]

    tasks = api_get("tasks", params)

    matching = []
    for t in tasks:
        d = parse_due_date(t.get("due"))
        if d == target:
            matching.append(t)

    def sort_key(t):
        return (-t.get("priority", 1), t.get("due", {}).get("date", ""))

    matching.sort(key=sort_key)

    day_name = target.strftime("%A")
    proj_label = f" in {proj['name']}" if proj else ""
    print(f"=== {target} ({day_name}){proj_label} — {len(matching)} tasks ===\n")
    for t in matching:
        print(format_task(t, pmap))

    if not matching:
        print("(none)")


def cmd_week(args):
    """Show tasks due this week, grouped by day."""
    pmap = project_map()
    tasks = api_get("tasks")
    today = datetime.now().date()
    # Through Sunday
    end_of_week = today + timedelta(days=(6 - today.weekday()))

    by_date = defaultdict(list)
    overdue = []

    for t in tasks:
        d = parse_due_date(t.get("due"))
        if d is not None:
            if d < today:
                overdue.append(t)
            elif d <= end_of_week:
                by_date[d].append(t)

    def sort_key(t):
        return (-t.get("priority", 1), t.get("due", {}).get("date", ""))

    if overdue:
        overdue.sort(key=sort_key)
        print(f"=== OVERDUE ({len(overdue)} tasks) ===\n")
        for t in overdue:
            print(format_task(t, pmap))
        print()

    total = len(overdue)
    for d in sorted(by_date.keys()):
        day_tasks = by_date[d]
        day_tasks.sort(key=sort_key)
        day_name = d.strftime("%A")
        marker = " <<<< TODAY" if d == today else ""
        print(f"=== {d} ({day_name}){marker} — {len(day_tasks)} tasks ===\n")
        for t in day_tasks:
            print(format_task(t, pmap))
        print()
        total += len(day_tasks)

    print(f"Total: {total} tasks this week")


def cmd_add(args):
    project_name = args.project or BATCAVE_PROJECT
    proj = find_project(project_name)
    if not proj:
        print(f"No project matching '{project_name}'.")
        sys.exit(1)

    payload = {"content": args.content}

    if args.parent:
        # Subtasks inherit project from parent — don't send project_id or the API
        # returns 404 "Section not found" when they're in different projects.
        payload["parent_id"] = args.parent
    else:
        payload["project_id"] = proj["id"]

    if args.section:
        if args.parent:
            print("Cannot specify --section with --parent (subtasks inherit section).")
            sys.exit(1)
        section = find_section(proj["id"], args.section)
        if not section:
            print(f"No section matching '{args.section}' in {proj['name']}.")
            print("Use 'todoist sections' to see available sections.")
            sys.exit(1)
        payload["section_id"] = section["id"]

    if args.due:
        payload["due_string"] = args.due

    if args.priority:
        payload["priority"] = args.priority

    if args.description:
        payload["description"] = args.description

    if args.labels:
        payload["labels"] = args.labels

    if args.deadline:
        payload["deadline_date"] = resolve_deadline_date(args.deadline)
        payload["deadline_lang"] = "en"

    task = api_post("tasks", payload)
    log_mutation("add", task_after=task)

    proj_name = ""
    if task.get("project_id"):
        proj = next((p for p in get_projects() if p["id"] == task["project_id"]), None)
        proj_name = f" in {proj['name']}" if proj else ""

    due = task.get("due")
    due_str = f" (due: {due['date']})" if due else ""
    section_info = ""
    if args.section:
        section_info = f" [section: {args.section}]"
    print(f"Created: {task['content']}{proj_name}{due_str}{section_info}")
    print(f"  id: {task['id']}")


def cmd_update(args):
    """Update a task's due date, content, or priority."""
    # Fetch the task first
    tasks = api_get("tasks")
    task = next((t for t in tasks if t["id"] == args.task_id), None)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)

    payload = {}
    if args.due is not None:
        # Safety check: warn if updating due on a recurring task
        due = task.get("due")
        if due and due.get("is_recurring"):
            print(f"BLOCKED: This is a recurring task ('{due.get('string', '?')}').")
            print(f"  'update --due' will DESTROY the recurrence pattern.")
            print(f"  Use 'postpone {args.task_id} {args.due}' instead to move")
            print(f"  the current occurrence while preserving recurrence.")
            print(f"  If you truly want to replace the recurrence, add --force.")
            if not getattr(args, "force", False):
                sys.exit(1)
            print(f"  --force set: proceeding with destructive update.")
        payload["due_string"] = args.due
    if args.content is not None:
        payload["content"] = args.content
    if args.priority is not None:
        payload["priority"] = args.priority
    if args.description is not None:
        payload["description"] = args.description

    if getattr(args, "no_deadline", False):
        payload["deadline_date"] = None
        payload["deadline_lang"] = None
    elif getattr(args, "deadline", None) is not None:
        payload["deadline_date"] = resolve_deadline_date(args.deadline)
        payload["deadline_lang"] = "en"

    if not payload:
        print("Nothing to update. Use --due, --content, --priority, --description, or --deadline.")
        sys.exit(1)

    pmap = project_map()
    proj_name = pmap.get(task.get("project_id"), "???")
    print(f"Updating: [{proj_name}] {task['content']}")

    updated = api_post(f"tasks/{args.task_id}", payload)
    log_mutation("update", task_before=task, task_after=updated)

    due = updated.get("due")
    due_str = f" (due: {due['date']})" if due else ""
    print(f"Updated:  [{proj_name}] {updated['content']}{due_str}")
    print(f"  id: {updated['id']}")


def resolve_deadline_date(date_str):
    """Resolve a deadline string to ISO date (YYYY-MM-DD). Same formats as postpone's day part."""
    today = datetime.now().date()
    s = date_str.strip().lower()
    if s == "today":
        return today.isoformat()
    if s == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if s.startswith("+") and s[1:].isdigit():
        return (today + timedelta(days=int(s[1:]))).isoformat()
    if s in WEEKDAYS:
        return next_weekday(today, WEEKDAYS[s]).isoformat()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        print(f"Cannot parse deadline date: '{date_str}'")
        print("  Accepted: tomorrow, friday, +3, 2026-03-15")
        sys.exit(1)


WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def next_weekday(from_date, target_weekday):
    """Find the next occurrence of a weekday (0=Mon, 6=Sun)."""
    days_ahead = target_weekday - from_date.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return from_date + timedelta(days=days_ahead)


def parse_time_str(time_str):
    """Parse time strings like '9am', '9:30pm', '21:00', '2pm' → 'HH:MM:SS'."""
    t = time_str.strip().lower()
    try:
        # Try 24h format first: "21:00", "14:30"
        dt = datetime.strptime(t, "%H:%M")
        return dt.strftime("%H:%M:%S")
    except ValueError:
        pass
    # Try 12h formats: "9am", "9:30pm", "2pm", "12:30am"
    for fmt in ("%I:%M%p", "%I%p", "%I:%M %p", "%I %p"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt.strftime("%H:%M:%S")
        except ValueError:
            pass
    return None


def parse_postpone_target(target_str, original_date_str):
    """Parse a postpone target, preserving original time if user doesn't specify one.

    Accepts: 'tomorrow', 'friday', '+3', '2026-02-21', 'tomorrow at 9pm',
             'friday at 14:00', ISO datetime strings.
    """
    today = datetime.now().date()

    # Extract time from original if present
    original_time = None
    if "T" in original_date_str:
        original_time = original_date_str.split("T")[1]

    target = target_str.strip()
    target_lower = target.lower()

    # ISO datetime passthrough
    if "T" in target and len(target) >= 16:
        return target

    # Split on " at " to separate day and time parts
    user_time = None
    day_part = target_lower
    if " at " in target_lower:
        day_part, time_part = target_lower.split(" at ", 1)
        day_part = day_part.strip()
        user_time = parse_time_str(time_part)
        if not user_time:
            print(f"Cannot parse time: '{time_part}'")
            sys.exit(1)

    # Parse the day part
    target_date = None
    if day_part == "today":
        target_date = today
    elif day_part == "tomorrow":
        target_date = today + timedelta(days=1)
    elif day_part.startswith("+") and day_part[1:].isdigit():
        target_date = today + timedelta(days=int(day_part[1:]))
    elif day_part in WEEKDAYS:
        target_date = next_weekday(today, WEEKDAYS[day_part])
    else:
        # Try ISO date
        try:
            target_date = datetime.strptime(day_part, "%Y-%m-%d").date()
        except ValueError:
            print(f"Cannot parse date: '{target_str}'")
            print("  Accepted formats: tomorrow, friday, +3, 2026-02-21, 'friday at 2pm'")
            sys.exit(1)

    # Determine the time: user-specified > original > none
    final_time = user_time or original_time
    if final_time:
        return f"{target_date.isoformat()}T{final_time}"
    else:
        return target_date.isoformat()


def cmd_postpone(args):
    """Reschedule a task to a new date. Handles both recurring and one-off tasks.

    For recurring tasks: sends both due_string (original recurrence pattern)
    and due_date/due_datetime (new target date), preserving recurrence.
    For one-off tasks: sends due_string with the new date (same as update --due).
    """
    tasks = api_get("tasks")
    task = next((t for t in tasks if t["id"] == args.task_id), None)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)

    due = task.get("due")
    if not due:
        print("This task has no due date. Nothing to postpone.")
        sys.exit(1)

    is_recurring = due.get("is_recurring", False)
    original_date = due.get("date", "")

    # Parse the target
    target = parse_postpone_target(args.date, original_date)

    pmap = project_map()
    proj_name = pmap.get(task.get("project_id"), "???")

    if is_recurring:
        # Recurring: preserve recurrence pattern with the Todoist trick
        original_string = due.get("string", "")
        has_time = "T" in target
        payload = {"due_string": original_string}
        if has_time:
            payload["due_datetime"] = target
        else:
            payload["due_date"] = target

        print(f"Postponing: [{proj_name}] {task['content']}")
        print(f"  From: {original_date}")
        print(f"  To:   {target}")
        print(f"  Recurrence: {original_string}")

        updated = api_post(f"tasks/{args.task_id}", payload)
        log_mutation("postpone", task_before=task, task_after=updated)

        new_due = updated.get("due", {})
        if new_due.get("is_recurring"):
            print(f"  Recurrence preserved")
        else:
            print(f"  WARNING: Recurrence was lost! Check the task in Todoist.")
        print(f"  Result: {new_due.get('date')}")
    else:
        # One-off: simple due_string update (same as update --due)
        payload = {"due_string": args.date}

        print(f"Rescheduling: [{proj_name}] {task['content']}")
        print(f"  From: {original_date}")
        print(f"  To:   {target}")

        updated = api_post(f"tasks/{args.task_id}", payload)
        log_mutation("postpone", task_before=task, task_after=updated)

        new_due = updated.get("due", {})
        print(f"  Result: {new_due.get('date')}")


def cmd_complete(args):
    tasks = api_get("tasks")
    task = next((t for t in tasks if t["id"] == args.task_id), None)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)

    # Cascade-complete warning: check for subtasks
    children = get_subtasks(args.task_id, all_tasks=tasks)
    if children and not getattr(args, "cascade", False):
        print(f"BLOCKED: This task has {len(children)} subtask(s) that will be cascade-completed:")
        pmap = project_map()
        for c in children:
            due = c.get("due")
            due_str = f" (due: {due['date']})" if due else ""
            print(f"  - {c['content']}{due_str}  id: {c['id']}")
        print(f"\nUse --cascade to confirm you want to complete all of them.")
        print(f"Or complete individual subtasks by their IDs instead.")
        sys.exit(1)

    # Log before completing (can't fetch after it's gone)
    extra = None
    if children:
        extra = {"cascade_children": [task_snapshot(c) for c in children]}
    log_mutation("complete", task_before=task, extra=extra)

    api_post(f"tasks/{args.task_id}/close")
    print(f"Completed: {task['content']} (id: {args.task_id})")
    if children:
        print(f"  (cascade-completed {len(children)} subtask(s))")


def cmd_uncomplete(args):
    """Reopen a completed task (undo a complete). Completed tasks aren't returned
    by the active-tasks endpoint, so reopen first, then fetch for the snapshot."""
    api_post(f"tasks/{args.task_id}/reopen")
    task = next((t for t in api_get("tasks") if t["id"] == args.task_id), None)
    log_mutation("uncomplete", task_after=task, extra={"task_id": args.task_id})
    if task:
        print(f"Reopened: {task['content']} (id: {args.task_id})")
    else:
        print(f"Reopened task {args.task_id}.")


def cmd_search(args):
    pmap = project_map()

    tasks = api_get("tasks")
    query = args.query.lower()
    matches = [t for t in tasks if query in t["content"].lower()
               or query in t.get("description", "").lower()]

    if not matches:
        print(f"No tasks matching '{args.query}'.")
        return

    print(f"Found {len(matches)} tasks matching '{args.query}':\n")
    for t in matches:
        print(format_task(t, pmap))


def cmd_sections(args):
    """List sections for a project."""
    project_name = args.project or BATCAVE_PROJECT
    proj = find_project(project_name)
    if not proj:
        print(f"No project matching '{project_name}'.")
        sys.exit(1)

    sections = get_sections(proj["id"])
    if not sections:
        print(f"No sections in {proj['name']}.")
        return

    print(f"Sections in {proj['name']} ({len(sections)}):\n")
    for i, s in enumerate(sections):
        print(f"  {i + 1}. {s['name']:30s}  id: {s['id']}")


def cmd_add_section(args):
    """Create a new section in a project."""
    project_name = args.project or BATCAVE_PROJECT
    proj = find_project(project_name)
    if not proj:
        print(f"No project matching '{project_name}'.")
        sys.exit(1)

    payload = {"name": args.name, "project_id": proj["id"]}
    if args.order is not None:
        payload["order"] = args.order

    section = api_post("sections", payload)
    log_mutation("add-section", extra={"section_name": section["name"],
                                       "section_id": section["id"],
                                       "project": proj["name"]})
    # Invalidate cache for this project
    _section_cache.pop(proj["id"], None)
    print(f"Created section: {section['name']} (id: {section['id']})")


def cmd_move_section(args):
    """Move one or more tasks to a section within the same project."""
    all_tasks = api_get("tasks")

    # Resolve section once (from the first task's project)
    section = None
    section_obj = None
    moved = 0
    errors = 0

    for task_id in args.task_ids:
        task = next((t for t in all_tasks if t["id"] == task_id), None)
        if not task:
            print(f"  SKIP: Task {task_id} not found.")
            errors += 1
            continue

        proj_id = task.get("project_id")

        # Resolve the section (lazily, from the task's project)
        if section_obj is None or section_obj.get("project_id") != proj_id:
            section_obj = find_section(proj_id, args.section)
            if not section_obj:
                pmap = project_map()
                proj_name = pmap.get(proj_id, "???")
                print(f"  ERROR: No section matching '{args.section}' in {proj_name}.")
                print("  Use 'todoist sections' to see available sections.")
                sys.exit(1)

        payload = {"section_id": section_obj["id"]}
        task_before = dict(task)
        api_post(f"tasks/{task_id}/move", payload)
        task_after = dict(task)
        task_after["section_id"] = section_obj["id"]
        log_mutation("move-section", task_before=task_before, task_after=task_after,
                     extra={"target_section": section_obj["name"]})
        print(f"  Moved: {task['content']} → '{section_obj['name']}'")
        moved += 1

    print(f"\nDone: {moved} moved, {errors} skipped.")


def cmd_move_project(args):
    """Move one or more tasks to a different project."""
    target = find_project(args.project)
    if not target:
        print(f"Project '{args.project}' not found.")
        sys.exit(1)

    all_tasks = api_get("tasks")
    pmap = project_map()
    moved = 0
    errors = 0

    for task_id in args.task_ids:
        task = next((t for t in all_tasks if t["id"] == task_id), None)
        if not task:
            print(f"  SKIP: Task {task_id} not found.")
            errors += 1
            continue

        old_proj = pmap.get(task.get("project_id"), "???")
        if task.get("project_id") == target["id"]:
            print(f"  SKIP: '{task['content']}' already in {target['name']}.")
            continue

        payload = {"project_id": target["id"]}
        task_before = dict(task)
        api_post(f"tasks/{task_id}/move", payload)
        task_after = dict(task)
        task_after["project_id"] = target["id"]
        log_mutation("move-project", task_before=task_before, task_after=task_after,
                     extra={"from_project": old_proj, "to_project": target["name"]})
        print(f"  Moved: '{task['content']}' — {old_proj} → {target['name']}")
        moved += 1

    print(f"\nDone: {moved} moved, {errors} skipped.")


def cmd_reparent(args):
    """Make one or more tasks subtasks of a parent task."""
    all_tasks = api_get("tasks")
    pmap = project_map()

    parent = next((t for t in all_tasks if t["id"] == args.parent_id), None)
    if not parent:
        print(f"Parent task {args.parent_id} not found.")
        sys.exit(1)

    print(f"Parent: '{parent['content']}' [{pmap.get(parent.get('project_id'), '???')}]")
    moved = 0
    errors = 0

    for task_id in args.task_ids:
        task = next((t for t in all_tasks if t["id"] == task_id), None)
        if not task:
            print(f"  SKIP: Task {task_id} not found.")
            errors += 1
            continue

        if task.get("parent_id") == args.parent_id:
            print(f"  SKIP: '{task['content']}' already a subtask.")
            continue

        payload = {"parent_id": args.parent_id}
        task_before = dict(task)
        api_post(f"tasks/{task_id}/move", payload)
        task_after = dict(task)
        task_after["parent_id"] = args.parent_id
        task_after["project_id"] = parent.get("project_id")
        log_mutation("reparent", task_before=task_before, task_after=task_after,
                     extra={"parent_task": parent["content"]})
        print(f"  Reparented: '{task['content']}' → subtask of '{parent['content']}'")
        moved += 1

    print(f"\nDone: {moved} reparented, {errors} skipped.")


def cmd_add_project(args):
    """Create a new project (audit-logged)."""
    payload = {"name": args.name}
    if args.color:
        payload["color"] = args.color
    if args.parent:
        parent = find_project(args.parent)
        if not parent:
            print(f"Parent project '{args.parent}' not found.")
            sys.exit(1)
        payload["parent_id"] = parent["id"]

    project = api_post("projects", payload)
    log_mutation("add-project", extra={
        "project_id": project["id"],
        "project_name": project["name"],
        "color": project.get("color"),
        "parent_id": project.get("parent_id"),
    })
    where = f" (under {args.parent})" if args.parent else ""
    print(f"Created project: {project['name']}{where}")
    print(f"  id: {project['id']}")


def cmd_rename_project(args):
    """Rename an existing project (audit-logged)."""
    project = find_project(args.project)
    if not project:
        print(f"Project '{args.project}' not found.")
        sys.exit(1)

    old_name = project["name"]
    if old_name == args.new_name:
        print(f"Project is already named '{old_name}'. Nothing to do.")
        return

    updated = api_post(f"projects/{project['id']}", {"name": args.new_name})
    log_mutation("rename-project", extra={
        "project_id": project["id"],
        "old_name": old_name,
        "new_name": updated.get("name", args.new_name),
    })
    print(f"Renamed: '{old_name}' → '{updated.get('name', args.new_name)}'")


def cmd_delete_project(args):
    """Delete a project. Irreversible — requires --yes to execute."""
    project = find_project(args.project)
    if not project:
        print(f"Project '{args.project}' not found.")
        sys.exit(1)

    # Count contents so the confirmation (and the audit record) is honest.
    tasks = api_get("tasks", {"project_id": project["id"]})
    task_count = len(tasks)

    if not args.yes:
        print(f"About to delete project '{project['name']}' (id: {project['id']}).")
        print(f"  Contains {task_count} active task(s) — deletion is IRREVERSIBLE.")
        print("  Re-run with --yes to confirm.")
        sys.exit(1)

    api_delete(f"projects/{project['id']}")
    log_mutation("delete-project", extra={
        "project_id": project["id"],
        "project_name": project["name"],
        "task_count": task_count,
    })
    print(f"Deleted project: '{project['name']}' ({task_count} task(s) removed with it).")


def cmd_bulk(args):
    """Execute many mutations from a JSON file/stdin — each one audit-logged.

    This exists so bulk work NEVER has to go off the audited path. Input is a
    JSON array of op objects:

      {"op":"add","content":"...","project":"Name","section":"Name",
       "parent":"ID","due":"...","priority":N,"description":"...",
       "labels":[...],"deadline":"...","child_order":N}
      {"op":"update","id":"...","content":"...","priority":N,
       "description":"...","due":"...","deadline":"..."}
      {"op":"move-section","id":"...","section":"Name"}   # section in task's own project
      {"op":"move-parent","id":"...","parent":"ID"}       # nest task under parent (same project)

    'add' defaults to the Batcave project when 'project' is omitted. Every op is
    logged with extra.bulk=true and its index. Failing ops are reported and
    skipped, not aborted. Use --dry-run to preview.
    """
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    try:
        ops = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}")
        sys.exit(1)
    if not isinstance(ops, list):
        print("Bulk input must be a JSON array of op objects.")
        sys.exit(1)

    dry = args.dry_run
    need_tasks = any(o.get("op") in ("update", "move-section") for o in ops)
    task_by_id = {t["id"]: t for t in (api_get("tasks") if need_tasks else [])}
    ok = errs = 0

    for i, op in enumerate(ops):
        kind = op.get("op")
        try:
            if kind == "add":
                proj = find_project(op.get("project") or BATCAVE_PROJECT)
                if not proj:
                    raise ValueError(f"project '{op.get('project')}' not found")
                payload = {"content": op["content"]}
                if op.get("parent"):
                    payload["parent_id"] = op["parent"]
                else:
                    payload["project_id"] = proj["id"]
                if op.get("section"):
                    sec = find_section(proj["id"], op["section"])
                    if not sec:
                        raise ValueError(f"section '{op['section']}' not in {proj['name']}")
                    payload["section_id"] = sec["id"]
                for src, dst in (("due", "due_string"), ("priority", "priority"),
                                 ("description", "description"), ("labels", "labels"),
                                 ("child_order", "child_order")):
                    if op.get(src) is not None:
                        payload[dst] = op[src]
                if op.get("deadline"):
                    payload["deadline_date"] = resolve_deadline_date(op["deadline"])
                    payload["deadline_lang"] = "en"
                if dry:
                    dest = proj["name"] + (f" / {op['section']}" if op.get("section") else "")
                    print(f"  [{i}] add: '{op['content']}' → {dest}")
                else:
                    task = api_post("tasks", payload)
                    log_mutation("add", task_after=task, extra={"bulk": True, "bulk_index": i})
                    print(f"  [{i}] added: '{task['content']}'")

            elif kind == "update":
                task = task_by_id.get(op.get("id"))
                if not task:
                    raise ValueError(f"task {op.get('id')} not found")
                payload = {}
                for src, dst in (("content", "content"), ("priority", "priority"),
                                 ("description", "description"), ("due", "due_string")):
                    if op.get(src) is not None:
                        payload[dst] = op[src]
                if op.get("deadline"):
                    payload["deadline_date"] = resolve_deadline_date(op["deadline"])
                    payload["deadline_lang"] = "en"
                if not payload:
                    raise ValueError("update op has no fields to change")
                if dry:
                    print(f"  [{i}] update {op['id']}: {payload}")
                else:
                    updated = api_post(f"tasks/{op['id']}", payload)
                    log_mutation("update", task_before=task, task_after=updated,
                                 extra={"bulk": True, "bulk_index": i})
                    print(f"  [{i}] updated: '{updated.get('content', task['content'])}'")

            elif kind == "move-section":
                task = task_by_id.get(op.get("id"))
                if not task:
                    raise ValueError(f"task {op.get('id')} not found")
                sec = find_section(task["project_id"], op["section"])
                if not sec:
                    raise ValueError(f"section '{op['section']}' not found in task's project")
                if dry:
                    print(f"  [{i}] move {op['id']} → section '{op['section']}'")
                else:
                    before = dict(task)
                    api_post(f"tasks/{op['id']}/move", {"section_id": sec["id"]})
                    after = dict(task)
                    after["section_id"] = sec["id"]
                    log_mutation("move-section", task_before=before, task_after=after,
                                 extra={"bulk": True, "bulk_index": i, "target_section": sec["name"]})
                    print(f"  [{i}] moved: '{task['content']}' → '{sec['name']}'")

            elif kind == "move-parent":
                task = task_by_id.get(op.get("id"))
                if not task:
                    raise ValueError(f"task {op.get('id')} not found")
                parent = task_by_id.get(op.get("parent"))
                if not parent:
                    raise ValueError(f"parent task {op.get('parent')} not found")
                if dry:
                    print(f"  [{i}] reparent {op['id']} → under '{parent['content']}'")
                else:
                    before = dict(task)
                    api_post(f"tasks/{op['id']}/move", {"parent_id": op["parent"]})
                    after = dict(task)
                    after["parent_id"] = op["parent"]
                    log_mutation("move-parent", task_before=before, task_after=after,
                                 extra={"bulk": True, "bulk_index": i,
                                        "target_parent": parent["content"]})
                    print(f"  [{i}] reparented: '{task['content']}' → under '{parent['content']}'")

            else:
                raise ValueError(f"unknown op '{kind}'")
            ok += 1
        except Exception as e:
            print(f"  [{i}] ERROR ({kind}): {e}")
            errs += 1

    verb = "DRY-RUN (no changes made)" if dry else "Done"
    print(f"\n{verb}: {ok} ok, {errs} failed of {len(ops)}")


# --- Reminder helpers ---

def parse_offset(offset_str):
    """Parse an offset string like '1d', '7d', '2w', '3h', '30m' into minutes."""
    s = offset_str.strip().lower()
    multipliers = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    if len(s) < 2 or s[-1] not in multipliers:
        return None
    try:
        value = int(s[:-1])
    except ValueError:
        return None
    return value * multipliers[s[-1]]


def format_offset(minutes):
    """Format minutes into a human-readable offset string."""
    if minutes >= 10080 and minutes % 10080 == 0:
        return f"{minutes // 10080}w"
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def cmd_reminders(args):
    """List reminders for a task."""
    all_reminders = api_get("reminders")
    task_reminders = [r for r in all_reminders if r.get("item_id") == args.task_id]

    # Fetch the task for display
    tasks = api_get("tasks")
    task = next((t for t in tasks if t["id"] == args.task_id), None)
    pmap = project_map()

    if task:
        proj_name = pmap.get(task.get("project_id"), "???")
        print(f"Reminders for: [{proj_name}] {task['content']}")
    else:
        print(f"Reminders for task {args.task_id}:")

    if not task_reminders:
        print("  (none)")
        return

    print()
    for r in sorted(task_reminders, key=lambda x: x.get("minute_offset", 0), reverse=True):
        rtype = r.get("type", "?")
        if rtype == "relative":
            offset = r.get("minute_offset", 0)
            print(f"  {format_offset(offset)} before  (id: {r['id']})")
        else:
            due = r.get("due", {})
            print(f"  absolute: {due.get('date', '?')}  (id: {r['id']})")


def cmd_add_reminder(args):
    """Add one or more relative reminders to a task.

    Offsets: 30m, 1h, 1d, 3d, 7d, 1w, 2w, etc.
    """
    # Validate all offsets first
    parsed = []
    for offset_str in args.offsets:
        minutes = parse_offset(offset_str)
        if minutes is None:
            print(f"Cannot parse offset: '{offset_str}'")
            print("  Accepted: 30m, 1h, 1d, 3d, 7d, 1w, 2w")
            sys.exit(1)
        parsed.append((offset_str, minutes))

    # Fetch task for display
    tasks = api_get("tasks")
    task = next((t for t in tasks if t["id"] == args.task_id), None)
    if not task:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)

    pmap = project_map()
    proj_name = pmap.get(task.get("project_id"), "???")
    print(f"Adding reminders to: [{proj_name}] {task['content']}\n")

    for offset_str, minutes in parsed:
        payload = {
            "task_id": args.task_id,
            "type": "relative",
            "minute_offset": minutes,
        }
        reminder = api_post("reminders", payload)
        log_mutation("add-reminder", extra={
            "reminder_id": reminder.get("id"),
            "task_id": args.task_id,
            "task_content": task.get("content"),
            "minute_offset": minutes,
            "offset_string": offset_str,
        })
        fire_date = reminder.get("due", {}).get("date", "?")
        print(f"  + {format_offset(minutes)} before  (fires: {fire_date}, id: {reminder['id']})")


def cmd_remove_reminder(args):
    """Delete a reminder by ID."""
    # Fetch the reminder first for audit logging
    all_reminders = api_get("reminders")
    reminder = next((r for r in all_reminders if r["id"] == args.reminder_id), None)

    if not reminder:
        print(f"Reminder {args.reminder_id} not found.")
        sys.exit(1)

    resp = requests.delete(
        f"{API_URL}/reminders/{args.reminder_id}",
        headers=headers(),
    )
    resp.raise_for_status()

    log_mutation("remove-reminder", extra={
        "reminder_id": args.reminder_id,
        "task_id": reminder.get("item_id"),
        "minute_offset": reminder.get("minute_offset"),
        "type": reminder.get("type"),
    })

    offset = reminder.get("minute_offset", 0)
    print(f"Removed: {format_offset(offset)} before  (id: {args.reminder_id})")


def main():
    parser = argparse.ArgumentParser(description="Todoist CLI for Bruce")
    sub = parser.add_subparsers(dest="command", required=True)

    # projects
    sub.add_parser("projects", help="List all projects")

    # list
    p_list = sub.add_parser("list", help="List active tasks")
    p_list.add_argument("--project", "-p", help="Filter by project name")
    p_list.add_argument("--limit", "-n", type=int, help="Max tasks to show")

    # today
    sub.add_parser("today", help="Tasks due today + overdue")

    # due
    p_due = sub.add_parser("due", help="Tasks due on a specific date")
    p_due.add_argument("date", help="Target date: today, tomorrow, friday, +3, 2026-04-10")
    p_due.add_argument("--project", "-p", help="Filter by project name")

    # week
    sub.add_parser("week", help="Tasks due this week (through Sunday)")

    # add
    p_add = sub.add_parser("add", help="Create a task")
    p_add.add_argument("content", help="Task content/title")
    p_add.add_argument("--project", "-p", help="Project name")
    p_add.add_argument("--section", "-s", help="Section name (within the project)")
    p_add.add_argument("--due", "-d", help="Due date (natural language, e.g. 'tomorrow', 'every monday')")
    p_add.add_argument("--priority", type=int, choices=[1, 2, 3, 4], help="Priority (4=urgent, 1=normal)")
    p_add.add_argument("--description", help="Task description/notes")
    p_add.add_argument("--labels", "-l", nargs="+", help="Labels to add")
    p_add.add_argument("--parent", help="Parent task ID (creates a subtask)")
    p_add.add_argument("--deadline", help="Deadline date (tomorrow, friday, +3, 2026-03-15)")

    # update
    p_update = sub.add_parser("update", help="Update a task")
    p_update.add_argument("task_id", help="Task ID to update")
    p_update.add_argument("--due", "-d", help="New due date (natural language)")
    p_update.add_argument("--content", "-c", help="New task content")
    p_update.add_argument("--priority", type=int, choices=[1, 2, 3, 4], help="New priority")
    p_update.add_argument("--description", help="New description")
    p_update.add_argument("--deadline", help="New deadline date (tomorrow, friday, +3, 2026-03-15)")
    p_update.add_argument("--no-deadline", action="store_true", help="Remove deadline")
    p_update.add_argument("--force", action="store_true",
                          help="Allow destructive replacement of a recurring task's schedule")

    # postpone
    p_postpone = sub.add_parser("postpone", help="Reschedule a task (preserves recurrence for recurring tasks)")
    p_postpone.add_argument("task_id", help="Task ID to postpone")
    p_postpone.add_argument("date", help="Target date: tomorrow, friday, +3, 2026-02-21, 'friday at 2pm'")

    # complete
    p_complete = sub.add_parser("complete", help="Complete a task")
    p_complete.add_argument("task_id", help="Task ID to complete")
    p_complete.add_argument("--cascade", action="store_true", help="Confirm cascade-completing all subtasks")

    p_uncomplete = sub.add_parser("uncomplete", help="Reopen a completed task (undo a complete)")
    p_uncomplete.add_argument("task_id", help="Task ID to reopen")

    # search
    p_search = sub.add_parser("search", help="Search tasks by content")
    p_search.add_argument("query", help="Search query")

    # sections
    p_sections = sub.add_parser("sections", help="List sections in a project")
    p_sections.add_argument("--project", "-p", help="Project name (default: Batcave)")

    # add-section
    p_add_section = sub.add_parser("add-section", help="Create a new section")
    p_add_section.add_argument("name", help="Section name")
    p_add_section.add_argument("--project", "-p", help="Project name (default: Batcave)")
    p_add_section.add_argument("--order", type=int, help="Position order (0-indexed)")

    # move-section
    p_move = sub.add_parser("move-section", help="Move task(s) to a section")
    p_move.add_argument("section", help="Target section name")
    p_move.add_argument("task_ids", nargs="+", help="Task ID(s) to move")

    # move-project
    p_move_proj = sub.add_parser("move-project", help="Move task(s) to a different project")
    p_move_proj.add_argument("project", help="Target project name")
    p_move_proj.add_argument("task_ids", nargs="+", help="Task ID(s) to move")

    # reparent
    p_reparent = sub.add_parser("reparent", help="Make task(s) subtasks of a parent task")
    p_reparent.add_argument("parent_id", help="Parent task ID")
    p_reparent.add_argument("task_ids", nargs="+", help="Task ID(s) to reparent")

    # add-project
    p_add_project = sub.add_parser("add-project", help="Create a new project")
    p_add_project.add_argument("name", help="Project name")
    p_add_project.add_argument("--color", help="Project color (Todoist color name)")
    p_add_project.add_argument("--parent", help="Parent project name (for nesting)")

    # rename-project
    p_rename_project = sub.add_parser("rename-project", help="Rename a project")
    p_rename_project.add_argument("project", help="Current project name")
    p_rename_project.add_argument("new_name", help="New project name")

    # delete-project
    p_delete_project = sub.add_parser("delete-project", help="Delete a project (irreversible)")
    p_delete_project.add_argument("project", help="Project name to delete")
    p_delete_project.add_argument("--yes", action="store_true", help="Confirm deletion (required)")

    # bulk
    p_bulk = sub.add_parser("bulk", help="Execute many mutations from a JSON file/stdin (each audit-logged)")
    p_bulk.add_argument("file", help="Path to a JSON array of ops, or '-' for stdin")
    p_bulk.add_argument("--dry-run", action="store_true", help="Preview ops without executing")

    # reminders
    p_reminders = sub.add_parser("reminders", help="List reminders for a task")
    p_reminders.add_argument("task_id", help="Task ID to list reminders for")

    # add-reminder
    p_add_reminder = sub.add_parser("add-reminder", help="Add reminder(s) before due date")
    p_add_reminder.add_argument("task_id", help="Task ID to add reminders to")
    p_add_reminder.add_argument("offsets", nargs="+", help="Offset(s) before due: 30m, 1h, 1d, 3d, 7d, 1w, 2w")

    # remove-reminder
    p_remove_reminder = sub.add_parser("remove-reminder", help="Delete a reminder")
    p_remove_reminder.add_argument("reminder_id", help="Reminder ID to delete")

    args = parser.parse_args()
    check_token()

    commands = {
        "projects": cmd_projects,
        "list": cmd_list,
        "today": cmd_today,
        "due": cmd_due,
        "week": cmd_week,
        "add": cmd_add,
        "update": cmd_update,
        "postpone": cmd_postpone,
        "complete": cmd_complete,
        "uncomplete": cmd_uncomplete,
        "search": cmd_search,
        "sections": cmd_sections,
        "add-section": cmd_add_section,
        "move-section": cmd_move_section,
        "move-project": cmd_move_project,
        "reparent": cmd_reparent,
        "add-project": cmd_add_project,
        "rename-project": cmd_rename_project,
        "delete-project": cmd_delete_project,
        "bulk": cmd_bulk,
        "reminders": cmd_reminders,
        "add-reminder": cmd_add_reminder,
        "remove-reminder": cmd_remove_reminder,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
