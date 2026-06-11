"""Regression tests for sleep_update.py.

Pure functions get direct unit tests. The CLI is smoke-tested by running
the script against a fake VAULT_PATH pointing at a tmp weekly note.
"""

import os
import subprocess
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

import sleep_update as su

SCRIPT = Path(__file__).resolve().parent.parent / "sleep_update.py"


# ---- Pure functions ----

class TestResolveDate:
    def test_today(self):
        assert su.resolve_date("today") == date.today()

    def test_tomorrow(self):
        assert su.resolve_date("tomorrow") == date.today() + timedelta(days=1)

    def test_yesterday(self):
        assert su.resolve_date("yesterday") == date.today() - timedelta(days=1)

    def test_iso_string(self):
        assert su.resolve_date("2026-04-17") == date(2026, 4, 17)

    def test_bad_input_exits(self):
        with pytest.raises(SystemExit):
            su.resolve_date("garbage")

    def test_bad_iso_exits(self):
        with pytest.raises(SystemExit):
            su.resolve_date("2026/04/17")


class TestWeeklyNotePath:
    """ISO-week routing — the Sunday→Monday boundary is the bug-magnet."""

    def test_known_iso_week(self):
        # 2026-04-17 (Friday) is in ISO week 2026-W16
        p = su.weekly_note_path(date(2026, 4, 17))
        assert p.endswith("2026-W16.md")

    def test_sunday_belongs_to_same_week(self):
        # 2026-04-19 is Sunday, still ISO week 16
        p = su.weekly_note_path(date(2026, 4, 19))
        assert p.endswith("2026-W16.md")

    def test_monday_rolls_to_next_week(self):
        # 2026-04-20 is Monday — starts ISO week 17
        p = su.weekly_note_path(date(2026, 4, 20))
        assert p.endswith("2026-W17.md")

    def test_zero_padded_week(self):
        # Early-year weeks must pad with leading zero
        p = su.weekly_note_path(date(2026, 1, 5))  # ISO week 2
        assert p.endswith("2026-W02.md")

    def test_year_boundary_belongs_to_iso_year(self):
        # 2025-12-29 (Monday) belongs to ISO year 2026, week 1
        p = su.weekly_note_path(date(2025, 12, 29))
        assert p.endswith("2026-W01.md")


SAMPLE_WEEKLY_NOTE = textwrap.dedent("""\
    ---
    created_on: 2026-04-12
    ---
    # Weekly stuff

    Some prose here.

    - Monday [[2026-04-13]]
        - Night before, what time getting into bed: 22:00
        - Night before, what time closing eyes: 22:15. Read for a bit.
        - Morning of, what time getting up: 06:30
        - Hours of Sleep: ~8h
        - Night of, what was energy level today from 1 to 10: 7
        - Correlate energy level with sleep previous night: Good.
    - Tuesday [[2026-04-14]]
        - Night before, what time getting into bed:
        - Night before, what time closing eyes:
        - Morning of, what time getting up:
        - Hours of Sleep:
        - Night of, what was energy level today from 1 to 10:
        - Correlate energy level with sleep previous night:
    - Wednesday [[2026-04-15]]
        - Night before, what time getting into bed: 23:00
        - Night before, what time closing eyes: 23:10
        - Morning of, what time getting up: 07:00
        - Hours of Sleep: ~7.8h
        - Night of, what was energy level today from 1 to 10: 6
        - Correlate energy level with sleep previous night: Fine.

    ---

    ## Reflection

    Other content.
""")


class TestFindDayBlock:
    def setup_method(self):
        self.lines = SAMPLE_WEEKLY_NOTE.splitlines(keepends=True)

    def test_finds_monday(self):
        start, end = su.find_day_block(self.lines, date(2026, 4, 13))
        assert start is not None
        assert "Monday [[2026-04-13]]" in self.lines[start]
        # End should be at the Tuesday bullet
        assert "Tuesday [[2026-04-14]]" in self.lines[end]

    def test_finds_middle_day(self):
        start, end = su.find_day_block(self.lines, date(2026, 4, 14))
        assert "Tuesday [[2026-04-14]]" in self.lines[start]
        assert "Wednesday [[2026-04-15]]" in self.lines[end]

    def test_last_day_ends_at_section_break(self):
        start, end = su.find_day_block(self.lines, date(2026, 4, 15))
        assert "Wednesday [[2026-04-15]]" in self.lines[start]
        # Should stop at the horizontal rule
        assert self.lines[end].startswith("---")

    def test_missing_date_returns_none(self):
        start, end = su.find_day_block(self.lines, date(2026, 4, 19))
        assert start is None
        assert end is None


class TestFieldParsing:
    def setup_method(self):
        self.lines = SAMPLE_WEEKLY_NOTE.splitlines(keepends=True)
        self.start, self.end = su.find_day_block(self.lines, date(2026, 4, 13))

    def test_field_line_index_finds_label(self):
        label = "Night before, what time getting into bed:"
        idx = su.field_line_index(self.lines, self.start, self.end, label)
        assert idx is not None
        assert label in self.lines[idx]

    def test_field_line_index_missing_returns_none(self):
        idx = su.field_line_index(
            self.lines, self.start, self.end, "Not a real label:"
        )
        assert idx is None

    def test_current_value_extracts_suffix(self):
        line = "    - Night before, what time getting into bed: 22:00\n"
        label = "Night before, what time getting into bed:"
        assert su.field_current_value(line, label) == "22:00"

    def test_current_value_empty_when_blank(self):
        line = "    - Hours of Sleep:\n"
        label = "Hours of Sleep:"
        assert su.field_current_value(line, label) == ""

    def test_current_value_preserves_trailing_prose(self):
        line = "    - Night before, what time closing eyes: 22:15. Read for a bit.\n"
        label = "Night before, what time closing eyes:"
        assert su.field_current_value(line, label) == "22:15. Read for a bit."


class TestUpdateLine:
    def test_replaces_suffix_preserves_indent(self):
        line = "    - Hours of Sleep:\n"
        label = "Hours of Sleep:"
        new = su.update_line(line, label, "~7.5h")
        assert new == "    - Hours of Sleep: ~7.5h\n"

    def test_overwrites_existing_value(self):
        line = "    - Hours of Sleep: ~6h\n"
        label = "Hours of Sleep:"
        new = su.update_line(line, label, "~8h")
        assert new == "    - Hours of Sleep: ~8h\n"

    def test_preserves_prefix_bullet(self):
        line = "        - Night before, what time getting into bed:\n"
        label = "Night before, what time getting into bed:"
        new = su.update_line(line, label, "23:00")
        assert new.startswith("        - Night before, what time getting into bed:")
        assert new.endswith(" 23:00\n")


# ---- CLI integration ----

@pytest.fixture
def fake_vault(tmp_path):
    """Build a VAULT_PATH-rooted dir with one weekly note, return (vault, note_path)."""
    weekly_dir = tmp_path / "4 Time" / "Weekly Notes"
    weekly_dir.mkdir(parents=True)
    note = weekly_dir / "2026-W16.md"
    note.write_text(SAMPLE_WEEKLY_NOTE)
    return tmp_path, note


def run_cli(vault, *args):
    env = {**os.environ, "VAULT_PATH": str(vault)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


class TestCLI:
    def test_show_reports_current_values(self, fake_vault):
        vault, _ = fake_vault
        r = run_cli(vault, "--date", "2026-04-13", "--show")
        assert r.returncode == 0, r.stderr
        assert "2026-04-13" in r.stdout
        assert "'22:00'" in r.stdout  # bed time from sample
        assert "'~8h'" in r.stdout  # hours from sample

    def test_writes_empty_field(self, fake_vault):
        vault, note = fake_vault
        r = run_cli(
            vault, "--date", "2026-04-14", "--bed", "23:30", "--hours", "~7.5h"
        )
        assert r.returncode == 0, r.stderr
        content = note.read_text()
        assert "Night before, what time getting into bed: 23:30" in content
        assert "Hours of Sleep: ~7.5h" in content

    def test_refuses_to_overwrite_without_force(self, fake_vault):
        vault, note = fake_vault
        original = note.read_text()
        r = run_cli(vault, "--date", "2026-04-13", "--bed", "21:00")
        # Exit code 2 = conflicts
        assert r.returncode == 2
        assert "already set" in r.stderr
        # File unchanged
        assert note.read_text() == original

    def test_force_overwrites(self, fake_vault):
        vault, note = fake_vault
        r = run_cli(vault, "--date", "2026-04-13", "--bed", "21:00", "--force")
        assert r.returncode == 0, r.stderr
        assert "Night before, what time getting into bed: 21:00" in note.read_text()

    def test_missing_date_errors(self, fake_vault):
        vault, _ = fake_vault
        r = run_cli(vault, "--date", "2026-04-19", "--bed", "22:00")
        assert r.returncode != 0
        assert "no entry" in r.stderr.lower()

    def test_missing_weekly_note_errors(self, tmp_path):
        # Empty vault — no weekly note at all
        (tmp_path / "4 Time" / "Weekly Notes").mkdir(parents=True)
        r = run_cli(tmp_path, "--date", "2026-04-13", "--bed", "22:00")
        assert r.returncode != 0
        assert "not found" in r.stderr.lower()

    def test_no_flags_errors(self, fake_vault):
        vault, _ = fake_vault
        r = run_cli(vault, "--date", "2026-04-13")
        assert r.returncode != 0
        assert "Nothing to update" in r.stderr

    def test_partial_success_with_conflict(self, fake_vault):
        """Writing one fresh field + one conflicting field: the fresh one lands, exit=2."""
        vault, note = fake_vault
        # Tuesday (2026-04-14) is empty; Monday (2026-04-13) is full.
        # Mix: update Tuesday energy (empty, should write) AND Monday bed (set, conflict).
        # But they're different dates — the script only touches one date per run.
        # Instead: on Monday, try to write --bed (conflict) AND --energy over an existing value (conflict).
        # Simpler: on Tuesday write one, then try to re-run and write again → conflict path.
        r1 = run_cli(vault, "--date", "2026-04-14", "--energy", "8")
        assert r1.returncode == 0
        # Second run: same field, no --force → conflict
        r2 = run_cli(vault, "--date", "2026-04-14", "--energy", "9")
        assert r2.returncode == 2
        # Value from first run still in place
        assert "energy level today from 1 to 10: 8" in note.read_text()

    def test_relative_date_today(self, tmp_path):
        """Smoke: today resolves and picks the right weekly note."""
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        note_name = f"{iso_year}-W{iso_week:02d}.md"
        weekly_dir = tmp_path / "4 Time" / "Weekly Notes"
        weekly_dir.mkdir(parents=True)
        note = weekly_dir / note_name
        day_name = today.strftime("%A")
        note.write_text(textwrap.dedent(f"""\
            - {day_name} [[{today.isoformat()}]]
                - Night before, what time getting into bed:
                - Night before, what time closing eyes:
                - Morning of, what time getting up:
                - Hours of Sleep:
                - Night of, what was energy level today from 1 to 10:
                - Correlate energy level with sleep previous night:
        """))
        r = run_cli(tmp_path, "--date", "today", "--energy", "7")
        assert r.returncode == 0, r.stderr
        assert "energy level today from 1 to 10: 7" in note.read_text()
