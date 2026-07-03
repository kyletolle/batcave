#!/usr/bin/env python3
"""
Sleep Tracker Field Writer

Surgically updates sleep tracker fields in the appropriate weekly note.
Replaces the manual Edit-tool dance in /sleep-evening and /sleep-morning.

Fields per day (matching the weekly-note template):
    --bed         "Night before, what time getting into bed:"
    --eyes        "Night before, what time closing eyes:"
    --wake        "Morning of, what time getting up:"
    --hours       "Hours of Sleep:"
    --energy      "Night of, what was energy level today from 1 to 10:"
    --correlation "Correlate energy level with sleep previous night:"  (rows before 2026-07-03)
    --agency      "Night of, what agency did I have today (...):"      (rows from 2026-07-03 on)

A day's row has one of --correlation / --agency, never both; writing the
flag the row doesn't carry reports a "field not found" conflict.

Date selection:
    --date today | tomorrow | yesterday | YYYY-MM-DD     (default: today)

The script picks the right weekly note based on the date's ISO week
(handles Sunday→Monday boundary automatically).

Examples:
    # Evening: log tonight's bedtime + today's energy
    sleep-update --date today --energy "8"
    sleep-update --date today --correlation "Good sleep, steady energy."
    sleep-update --date tomorrow --bed 23:30 --eyes "23:45. Read for 15 min."

    # Morning: log wake-up + computed sleep hours
    sleep-update --date today --wake "07:20. Fragmented night." --hours "~6.5h"

    # Backfill an old day
    sleep-update --date 2026-04-15 --energy "6→7"

By default, refuses to overwrite a field that already has content. Use --force
to overwrite. Use --show to read current values without writing.
"""

import os
import re
import sys
import argparse
from datetime import date, timedelta

VAULT = os.environ.get("VAULT_PATH", "/home/kyle/vault")
WEEKLY_DIR = os.path.join(VAULT, "4 Time", "Weekly Notes")

FIELDS = [
    ("bed",         "Night before, what time getting into bed:"),
    ("eyes",        "Night before, what time closing eyes:"),
    ("wake",        "Morning of, what time getting up:"),
    ("hours",       "Hours of Sleep:"),
    ("energy",      "Night of, what was energy level today from 1 to 10:"),
    ("correlation", "Correlate energy level with sleep previous night:"),
    ("agency",      "Night of, what agency did I have today (something of my own, or none):"),
]
FIELD_BY_FLAG = {flag: label for flag, label in FIELDS}


def resolve_date(s):
    today = date.today()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s == "yesterday":
        return today - timedelta(days=1)
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        raise SystemExit(f"Error: bad --date '{s}' (use today/tomorrow/yesterday or YYYY-MM-DD)")


def weekly_note_path(d):
    iso_year, iso_week, _ = d.isocalendar()
    return os.path.join(WEEKLY_DIR, f"{iso_year}-W{iso_week:02d}.md")


def find_day_block(lines, target_date):
    """
    Return (start_idx, end_idx) of the day's bullet block, exclusive on end.
    Day blocks look like:
        - Friday [[2026-04-17]]
            - Night before, what time getting into bed: ...
            - ...
        - Saturday [[2026-04-18]]
    """
    date_str = target_date.isoformat()
    target_marker = f"[[{date_str}]]"
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("- ") and target_marker in line:
            start = i
            break
    if start is None:
        return None, None
    # End at next top-level "- " bullet (same indent as the day header) or section break
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if line.lstrip().startswith("---") and indent == 0:
            end = j
            break
        if line.lstrip().startswith("- ") and indent == base_indent:
            end = j
            break
    return start, end


def field_line_index(lines, start, end, label):
    """Return the line index of the field label within the day block, or None."""
    for i in range(start, end):
        if label in lines[i]:
            return i
    return None


def field_current_value(line, label):
    """Extract whatever follows 'label' on the line (after the colon)."""
    idx = line.find(label)
    if idx < 0:
        return ""
    rest = line[idx + len(label):]
    return rest.strip()


def update_line(line, label, new_value):
    """Replace the suffix after 'label:' with new_value, preserving prefix/indent."""
    idx = line.find(label)
    prefix = line[:idx + len(label)]
    return f"{prefix} {new_value}\n"


def main():
    parser = argparse.ArgumentParser(
        description="Update sleep tracker fields in the weekly note",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date", default="today", help="today | tomorrow | yesterday | YYYY-MM-DD")
    for flag, _label in FIELDS:
        parser.add_argument(f"--{flag}", help=f"Set the '{_label}' field")
    parser.add_argument("--force", action="store_true", help="Overwrite existing field values")
    parser.add_argument("--show", action="store_true", help="Read current values without writing")
    args = parser.parse_args()

    target = resolve_date(args.date)
    note_path = weekly_note_path(target)

    if not os.path.exists(note_path):
        print(f"Error: weekly note not found: {note_path}", file=sys.stderr)
        sys.exit(1)

    with open(note_path) as f:
        lines = f.readlines()

    start, end = find_day_block(lines, target)
    if start is None:
        print(f"Error: no entry for {target.isoformat()} in {os.path.basename(note_path)}",
              file=sys.stderr)
        sys.exit(1)

    if args.show:
        print(f"{target.isoformat()} ({target.strftime('%A')}) — {os.path.basename(note_path)}")
        for flag, label in FIELDS:
            idx = field_line_index(lines, start, end, label)
            if idx is None:
                continue
            current = field_current_value(lines[idx], label)
            print(f"  {flag:<12} {current!r}")
        return

    # Collect updates. None = flag not passed; "" = explicit clear (allowed with --force).
    updates = [(flag, label, getattr(args, flag)) for flag, label in FIELDS if getattr(args, flag) is not None]
    if not updates:
        print("Nothing to update. Pass one of:", ", ".join(f"--{f}" for f, _ in FIELDS), file=sys.stderr)
        sys.exit(1)

    changes = []
    conflicts = []

    for flag, label, new_value in updates:
        idx = field_line_index(lines, start, end, label)
        if idx is None:
            conflicts.append((flag, f"field not found in day block"))
            continue
        current = field_current_value(lines[idx], label)
        if current and not args.force:
            conflicts.append((flag, f"already set: {current!r} (use --force)"))
            continue
        new_line = update_line(lines[idx], label, new_value)
        changes.append((flag, idx, lines[idx], new_line, current))

    if conflicts:
        print(f"Refusing to write — {len(conflicts)} conflict(s):", file=sys.stderr)
        for flag, msg in conflicts:
            print(f"  --{flag}: {msg}", file=sys.stderr)
        if not changes:
            sys.exit(2)
        print(f"\nProceeding with {len(changes)} non-conflicting change(s).", file=sys.stderr)

    for _flag, idx, _old, new_line, _ in changes:
        lines[idx] = new_line

    with open(note_path, "w") as f:
        f.writelines(lines)

    print(f"Updated {target.isoformat()} ({target.strftime('%A')}) in {os.path.basename(note_path)}:")
    for flag, _idx, _old, new_line, prev in changes:
        new_val = field_current_value(new_line, FIELD_BY_FLAG[flag])
        if prev:
            print(f"  {flag:<12} {prev!r} → {new_val!r}")
        else:
            print(f"  {flag:<12} {new_val!r}")

    if conflicts:
        sys.exit(2)


if __name__ == "__main__":
    main()
