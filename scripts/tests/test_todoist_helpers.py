"""Regression tests for todoist.py pure helpers.

Covers date/time parsing, name-matching helpers, and cascade detection —
the bits that execute locally on every command. API-bound commands
(cmd_add, cmd_complete, etc.) aren't tested here; their integrity rests
on the audit log tests and these helpers being correct.
"""

from datetime import date, datetime, timedelta

import pytest

import todoist


# ---- parse_time_str ----

class TestParseTimeStr:
    @pytest.mark.parametrize("src,expected", [
        ("9am", "09:00:00"),
        ("9AM", "09:00:00"),
        ("9 am", "09:00:00"),
        ("9pm", "21:00:00"),
        ("2pm", "14:00:00"),
        ("12am", "00:00:00"),
        ("12pm", "12:00:00"),
        ("9:30pm", "21:30:00"),
        ("9:30 pm", "21:30:00"),
        ("21:00", "21:00:00"),
        ("14:30", "14:30:00"),
        ("00:00", "00:00:00"),
    ])
    def test_parses_valid_times(self, src, expected):
        assert todoist.parse_time_str(src) == expected

    @pytest.mark.parametrize("bad", ["", "noon", "25:00", "9:99pm", "abc"])
    def test_returns_none_on_garbage(self, bad):
        assert todoist.parse_time_str(bad) is None


# ---- next_weekday ----

class TestNextWeekday:
    def test_next_weekday_is_future(self):
        # Monday (weekday=0) from a Wednesday → the following Monday
        wed = date(2026, 4, 15)  # Wednesday
        result = todoist.next_weekday(wed, 0)  # Monday
        assert result == date(2026, 4, 20)

    def test_same_weekday_skips_today(self):
        """Called with today's weekday, returns next week's same day (never today)."""
        wed = date(2026, 4, 15)
        result = todoist.next_weekday(wed, 2)  # Wednesday
        assert result == date(2026, 4, 22)  # one week later

    def test_friday_from_monday(self):
        mon = date(2026, 4, 13)  # Monday
        assert todoist.next_weekday(mon, 4) == date(2026, 4, 17)  # Fri same week


# ---- parse_postpone_target ----

class TestParsePostponeTarget:
    def test_tomorrow_no_original_time(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        out = todoist.parse_postpone_target("tomorrow", "2026-04-17")
        assert out == "2026-04-18"

    def test_tomorrow_preserves_original_time(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        out = todoist.parse_postpone_target("tomorrow", "2026-04-17T14:00:00")
        assert out == "2026-04-18T14:00:00"

    def test_user_time_overrides_original(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        out = todoist.parse_postpone_target("tomorrow at 9am", "2026-04-17T14:00:00")
        assert out == "2026-04-18T09:00:00"

    def test_plus_n_days(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        out = todoist.parse_postpone_target("+3", "2026-04-17")
        assert out == "2026-04-20"

    def test_weekday_name(self, monkeypatch):
        # From Friday 2026-04-17, "monday" → 2026-04-20
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        out = todoist.parse_postpone_target("monday", "2026-04-17")
        assert out == "2026-04-20"

    def test_iso_date(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        out = todoist.parse_postpone_target("2026-05-01", "2026-04-17")
        assert out == "2026-05-01"

    def test_iso_datetime_passthrough(self):
        out = todoist.parse_postpone_target("2026-05-01T15:30:00", "2026-04-17")
        assert out == "2026-05-01T15:30:00"

    def test_today(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        out = todoist.parse_postpone_target("today", "2026-04-17")
        assert out == "2026-04-17"

    def test_bad_date_exits(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        with pytest.raises(SystemExit):
            todoist.parse_postpone_target("march 13", "2026-04-17")

    def test_bad_time_exits(self, monkeypatch):
        monkeypatch.setattr(
            todoist, "datetime",
            _FrozenDatetime(date(2026, 4, 17)),
        )
        with pytest.raises(SystemExit):
            todoist.parse_postpone_target("tomorrow at noon", "2026-04-17")


class _FrozenDatetime:
    """Helper: replace todoist.datetime with a shim that pins 'today' for tests.

    parse_postpone_target calls `datetime.now().date()`. We replace the module-
    level datetime symbol with an object that provides a fixed .now() — all
    other datetime.* usage (fromisoformat, strptime) must still work."""
    def __init__(self, today):
        self._today = today

    def now(self):
        return datetime.combine(self._today, datetime.min.time())

    @staticmethod
    def strptime(s, fmt):
        return datetime.strptime(s, fmt)

    @staticmethod
    def fromisoformat(s):
        return datetime.fromisoformat(s)


# ---- resolve_deadline_date ----

class TestResolveDeadlineDate:
    def test_tomorrow(self, monkeypatch):
        monkeypatch.setattr(todoist, "datetime", _FrozenDatetime(date(2026, 4, 17)))
        assert todoist.resolve_deadline_date("tomorrow") == "2026-04-18"

    def test_plus_n(self, monkeypatch):
        monkeypatch.setattr(todoist, "datetime", _FrozenDatetime(date(2026, 4, 17)))
        assert todoist.resolve_deadline_date("+3") == "2026-04-20"

    def test_iso(self, monkeypatch):
        monkeypatch.setattr(todoist, "datetime", _FrozenDatetime(date(2026, 4, 17)))
        assert todoist.resolve_deadline_date("2026-05-01") == "2026-05-01"

    def test_weekday(self, monkeypatch):
        monkeypatch.setattr(todoist, "datetime", _FrozenDatetime(date(2026, 4, 17)))
        # Friday → next Monday = 2026-04-20
        assert todoist.resolve_deadline_date("monday") == "2026-04-20"

    def test_bad_exits(self, monkeypatch):
        monkeypatch.setattr(todoist, "datetime", _FrozenDatetime(date(2026, 4, 17)))
        with pytest.raises(SystemExit):
            todoist.resolve_deadline_date("march 13")


# ---- parse_due_date ----

class TestParseDueDate:
    def test_none_returns_none(self):
        assert todoist.parse_due_date(None) is None

    def test_empty_date_returns_none(self):
        assert todoist.parse_due_date({"date": ""}) is None

    def test_missing_date_key_returns_none(self):
        assert todoist.parse_due_date({}) is None

    def test_iso_date(self):
        assert todoist.parse_due_date({"date": "2026-04-17"}) == date(2026, 4, 17)

    def test_iso_datetime(self):
        result = todoist.parse_due_date({"date": "2026-04-17T15:30:00"})
        assert result == date(2026, 4, 17)

    def test_iso_datetime_with_z(self):
        result = todoist.parse_due_date({"date": "2026-04-17T15:30:00Z"})
        assert result == date(2026, 4, 17)


# ---- find_project / find_section ----

@pytest.fixture
def mock_projects(monkeypatch):
    """Replace the project cache with fixed data, skipping API calls."""
    projects = [
        {"id": "p1", "name": "Batcave"},
        {"id": "p2", "name": "Tolle Household"},
        {"id": "p3", "name": "Yearlies"},
        {"id": "p4", "name": "Inbox"},
    ]
    monkeypatch.setattr(todoist, "_project_cache", projects)
    return projects


@pytest.fixture
def mock_sections(monkeypatch):
    sections = {
        "p1": [
            {"id": "s1", "name": "BoaBW", "project_id": "p1"},
            {"id": "s2", "name": "Sleep & Focus", "project_id": "p1"},
            {"id": "s3", "name": "Life Admin", "project_id": "p1"},
        ],
    }
    monkeypatch.setattr(todoist, "_section_cache", sections)
    return sections


class TestFindProject:
    def test_exact_match(self, mock_projects):
        proj = todoist.find_project("Batcave")
        assert proj["id"] == "p1"

    def test_case_insensitive(self, mock_projects):
        assert todoist.find_project("batcave")["id"] == "p1"
        assert todoist.find_project("BATCAVE")["id"] == "p1"

    def test_partial_match(self, mock_projects):
        # "Tolle" → "Tolle Household"
        assert todoist.find_project("Tolle")["id"] == "p2"

    def test_exact_beats_partial(self, mock_projects):
        """If 'Inbox' matches exactly one project and is a substring of another,
        exact match wins."""
        # Add a project that contains "Inbox" as substring to prove exact wins
        todoist._project_cache.append({"id": "p5", "name": "Inbox Archive"})
        proj = todoist.find_project("Inbox")
        assert proj["id"] == "p4"  # exact match

    def test_no_match_returns_none(self, mock_projects):
        assert todoist.find_project("NonexistentProject") is None


class TestProjectMap:
    def test_maps_id_to_name(self, mock_projects):
        pmap = todoist.project_map()
        assert pmap["p1"] == "Batcave"
        assert pmap["p2"] == "Tolle Household"
        assert len(pmap) == 4


class TestFindSection:
    def test_exact_match(self, mock_sections):
        s = todoist.find_section("p1", "BoaBW")
        assert s["id"] == "s1"

    def test_case_insensitive(self, mock_sections):
        assert todoist.find_section("p1", "boabw")["id"] == "s1"

    def test_partial_match(self, mock_sections):
        # "Sleep" → "Sleep & Focus"
        assert todoist.find_section("p1", "Sleep")["id"] == "s2"

    def test_no_match_returns_none(self, mock_sections):
        assert todoist.find_section("p1", "Nonexistent") is None

    def test_unknown_project_returns_none(self, mock_sections, monkeypatch):
        """If the project isn't in cache, get_sections hits the API — mock that."""
        monkeypatch.setattr(todoist, "api_get", lambda *a, **kw: [])
        assert todoist.find_section("p99", "Anything") is None


class TestSectionMap:
    def test_maps_section_ids(self, mock_sections):
        smap = todoist.section_map("p1")
        assert smap["s1"] == "BoaBW"
        assert smap["s2"] == "Sleep & Focus"


# ---- get_subtasks (cascade-complete safety) ----

class TestGetSubtasks:
    def test_finds_direct_children(self):
        all_tasks = [
            {"id": "parent", "parent_id": None},
            {"id": "child1", "parent_id": "parent"},
            {"id": "child2", "parent_id": "parent"},
            {"id": "unrelated", "parent_id": "other"},
            {"id": "grandchild", "parent_id": "child1"},  # NOT a direct child
        ]
        subs = todoist.get_subtasks("parent", all_tasks)
        ids = [t["id"] for t in subs]
        assert set(ids) == {"child1", "child2"}
        assert "grandchild" not in ids  # only direct children

    def test_no_children_returns_empty(self):
        all_tasks = [{"id": "a", "parent_id": None}]
        assert todoist.get_subtasks("a", all_tasks) == []

    def test_nonexistent_parent_returns_empty(self):
        all_tasks = [{"id": "a", "parent_id": None}]
        assert todoist.get_subtasks("zzz", all_tasks) == []

    def test_fetches_all_tasks_when_none_given(self, monkeypatch):
        tasks = [
            {"id": "p", "parent_id": None},
            {"id": "c", "parent_id": "p"},
        ]
        monkeypatch.setattr(todoist, "api_get", lambda endpoint, *a, **kw: tasks)
        subs = todoist.get_subtasks("p")
        assert [t["id"] for t in subs] == ["c"]
