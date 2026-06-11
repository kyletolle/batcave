"""
Add 'day' and 'publish: false' frontmatter to daily notes.

Scans daily notes in the vault and adds:
  - day: <day of week>  (e.g. Monday, Tuesday, etc.)
  - publish: false      (only if neither 'publish:' nor skipping due to existing)

Handles multiple frontmatter structures:
  - Notes with WeekNote: inserts day/publish after WeekNote line
  - Notes without WeekNote: appends day/publish at end of frontmatter
  - Notes with existing 'public: false' (older format): leaves it alone,
    still adds 'publish: false' separately
  - Notes that already have 'day:': skips entirely

Usage:
    python add_day_and_publish_to_daily_notes.py [--dry-run]

    --dry-run   Show what would be changed without writing files.

By default, processes all daily notes under:
    4 Time/Daily Notes/

Adjust VAULT_ROOT and DAILY_NOTES_REL below if your paths differ.
"""

import datetime
import glob
import os
import sys

# ─── Configuration ───────────────────────────────────────────────
VAULT_ROOT = r"C:\Users\kyle\Documents\everything"
DAILY_NOTES_REL = r"4 Time\Daily Notes"
# ─────────────────────────────────────────────────────────────────

DAYS_OF_WEEK = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


def process_file(filepath, dry_run=False):
    """Process a single daily note. Returns a status string."""
    fname = os.path.basename(filepath)
    date_str = fname.replace(".md", "")

    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        return f"SKIP (not a date filename): {fname}"

    day_name = DAYS_OF_WEEK[d.weekday()]

    with open(filepath, "r", encoding="utf-8") as fh:
        content = fh.read()

    parts = content.split("---", 2)
    if len(parts) < 3:
        return f"ERROR (no frontmatter): {fname}"

    fm = parts[1]
    body = parts[2]

    # Already processed
    if "day:" in fm:
        return f"SKIP (already has day): {fname}"

    fm_lines = fm.strip().split("\n")
    has_publish = "publish:" in fm
    has_weeknote = "WeekNote:" in fm

    new_fm_lines = []
    inserted_day = False

    for line in fm_lines:
        new_fm_lines.append(line)
        # Insert after WeekNote if present
        if line.startswith("WeekNote:") and not inserted_day:
            new_fm_lines.append(f"day: {day_name}")
            if not has_publish:
                new_fm_lines.append("publish: false")
            inserted_day = True

    # If no WeekNote found, append at end of frontmatter
    if not inserted_day:
        new_fm_lines.append(f"day: {day_name}")
        if not has_publish:
            new_fm_lines.append("publish: false")

    new_content = "---\n" + "\n".join(new_fm_lines) + "\n---" + body

    if not dry_run:
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(new_content)

    prefix = "DRY RUN" if dry_run else "UPDATED"
    return f"{prefix}: {fname} -> {day_name}"


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=== DRY RUN MODE (no files will be modified) ===\n")

    base = os.path.join(VAULT_ROOT, DAILY_NOTES_REL)

    # Find all year subdirectories
    year_dirs = sorted(
        d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))
    )

    total_updated = 0
    total_skipped = 0
    total_errors = 0

    for year_dir in year_dirs:
        year_path = os.path.join(base, year_dir)
        files = sorted(glob.glob(os.path.join(year_path, "*.md")))

        if not files:
            continue

        updated = 0
        skipped = 0
        errors = 0

        for f in files:
            result = process_file(f, dry_run=dry_run)

            if result.startswith("UPDATED") or result.startswith("DRY RUN"):
                updated += 1
            elif result.startswith("SKIP"):
                skipped += 1
            elif result.startswith("ERROR"):
                errors += 1
                print(f"  {result}")

        print(f"{year_dir}: {updated} updated, {skipped} skipped, {errors} errors")
        total_updated += updated
        total_skipped += skipped
        total_errors += errors

    print(f"\nTotal: {total_updated} updated, {total_skipped} skipped, {total_errors} errors")


if __name__ == "__main__":
    main()
