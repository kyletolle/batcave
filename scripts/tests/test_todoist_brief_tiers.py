"""Regression tests for todoist_brief.categorize() tiering.

The bug these guard against: categorize() takes `today` as a parameter, but
main() used to hardcode it to the real current date. A caller building a brief
for a *different* day (an evening run producing tomorrow's brief) silently got
today's tiers — tomorrow's tasks buried in "upcoming", today's leftovers
presented as "do today". Nothing errored; the answer was just wrong.
"""

from datetime import date, timedelta

import todoist_brief


PROJECTS = {"p1": "Batcave"}


def task(tid, content, due=None, priority=1, project_id="p1"):
    return {
        "id": tid,
        "content": content,
        "project_id": project_id,
        "priority": priority,
        "labels": [],
        "due": {"date": due} if due else None,
    }


def contents(tier):
    return [t["content"] for t in tier]


class TestCategorizeRespectsTodayParam:
    """The same task set tiers differently depending on the `today` passed."""

    MON = date(2026, 8, 10)
    TUE = date(2026, 8, 11)

    TASKS = [
        task("a", "monday leftover", due="2026-08-10"),
        task("b", "tuesday item", due="2026-08-11"),
        task("c", "wednesday item", due="2026-08-12"),
    ]

    def test_tiers_against_monday(self):
        out = todoist_brief.categorize(self.TASKS, self.MON, 7, PROJECTS)
        assert contents(out["do_now"]) == []
        assert contents(out["do_today"]) == ["monday leftover"]
        assert set(contents(out["upcoming"])) == {"tuesday item", "wednesday item"}
        assert out["today"] == "2026-08-10"

    def test_tiers_against_tuesday(self):
        out = todoist_brief.categorize(self.TASKS, self.TUE, 7, PROJECTS)
        assert contents(out["do_now"]) == ["monday leftover"]
        assert contents(out["do_today"]) == ["tuesday item"]
        assert contents(out["upcoming"]) == ["wednesday item"]
        assert out["today"] == "2026-08-11"

    def test_upcoming_horizon_moves_with_today(self):
        """The horizon is today+days, so it slides with the target day."""
        edge = task("d", "edge of window", due="2026-08-18")
        tasks = [edge]

        mon = todoist_brief.categorize(tasks, self.MON, 7, PROJECTS)
        assert contents(mon["upcoming"]) == []  # 08-18 is 8 days out from Monday

        tue = todoist_brief.categorize(tasks, self.TUE, 7, PROJECTS)
        assert contents(tue["upcoming"]) == ["edge of window"]  # 7 days out from Tuesday


class TestTierBoundaries:
    TODAY = date(2026, 8, 11)

    def test_overdue_is_strictly_before_today(self):
        tasks = [
            task("a", "yesterday", due="2026-08-10"),
            task("b", "today", due="2026-08-11"),
        ]
        out = todoist_brief.categorize(tasks, self.TODAY, 7, PROJECTS)
        assert contents(out["do_now"]) == ["yesterday"]
        assert contents(out["do_today"]) == ["today"]

    def test_undated_high_priority_is_important_not_urgent(self):
        tasks = [task("a", "someday but P2+", priority=3)]
        out = todoist_brief.categorize(tasks, self.TODAY, 7, PROJECTS)
        assert contents(out["important_not_urgent"]) == ["someday but P2+"]

    def test_undated_low_priority_is_dropped(self):
        tasks = [task("a", "someday, low", priority=1)]
        out = todoist_brief.categorize(tasks, self.TODAY, 7, PROJECTS)
        for tier in ("do_now", "do_today", "important_not_urgent", "upcoming"):
            assert contents(out[tier]) == []

    def test_dated_beyond_horizon_is_dropped(self):
        far = (self.TODAY + timedelta(days=30)).isoformat()
        tasks = [task("a", "far future", due=far, priority=1)]
        out = todoist_brief.categorize(tasks, self.TODAY, 7, PROJECTS)
        assert contents(out["upcoming"]) == []
