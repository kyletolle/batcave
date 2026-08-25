"""Regression tests for `todoist complete --forever`.

Todoist's close endpoint only advances a recurring task to its next occurrence,
so completing "Alice batch cook session" forever is not one API call — the
recurrence has to be stripped first, then the task closed. That ordering is the
whole point: if the strip is skipped the series survives, and if the audit entry
is written after the strip the original due string is already gone.

These pin down: the strip-then-close ordering, the audit record that carries the
dropped recurrence, and the no-op path for non-recurring tasks.
"""

import json
from types import SimpleNamespace

import pytest

import todoist


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(todoist, "AUDIT_LOG", log_path)
    return log_path


def read_entries(log_path):
    if not log_path.exists():
        return []
    with open(log_path) as f:
        return [json.loads(line) for line in f if line.strip()]


def task(tid, content="Task", parent_id=None, recurring=True, **overrides):
    base = {
        "id": tid,
        "content": content,
        "project_id": "proj_1",
        "section_id": None,
        "parent_id": parent_id,
        "due": {"date": "2026-08-26", "string": "every wednesday at 9am",
                "is_recurring": recurring},
        "deadline": None,
        "priority": 1,
        "labels": [],
        "description": "",
    }
    base.update(overrides)
    return base


@pytest.fixture
def api(monkeypatch):
    """Stub the API surface and record the call sequence."""
    calls = []
    state = {"tasks": []}

    def fake_get(endpoint, params=None):
        calls.append(("get", endpoint, None))
        return state["tasks"]

    def fake_post(endpoint, json_data=None):
        calls.append(("post", endpoint, json_data))
        return {}

    monkeypatch.setattr(todoist, "api_get", fake_get)
    monkeypatch.setattr(todoist, "api_post", fake_post)
    return SimpleNamespace(calls=calls, state=state)


def args(task_id, cascade=False, forever=False):
    return SimpleNamespace(task_id=task_id, cascade=cascade, forever=forever)


class TestForeverEndsTheSeries:
    def test_strips_recurrence_before_closing(self, api, audit_log):
        api.state["tasks"] = [task("A", "Alice batch cook session")]
        todoist.cmd_complete(args("A", forever=True))

        posts = [c for c in api.calls if c[0] == "post"]
        assert posts == [
            ("post", "tasks/A", {"due_string": "today"}),
            ("post", "tasks/A/close", None),
        ], "recurrence must be stripped before the close, or the series survives"

    def test_audit_records_dropped_recurrence(self, api, audit_log):
        api.state["tasks"] = [task("A", "Alice batch cook session")]
        todoist.cmd_complete(args("A", forever=True))

        entries = read_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["action"] == "complete-forever"
        assert entries[0]["before"]["id"] == "A"
        assert entries[0]["extra"]["forever"] is True
        assert entries[0]["extra"]["dropped_recurrence"] == "every wednesday at 9am"

    def test_logs_before_touching_the_api(self, api, audit_log, monkeypatch):
        """The audit entry is the only record of the original recurrence."""
        api.state["tasks"] = [task("A", "Alice batch cook session")]
        order = []
        real_log = todoist.log_mutation

        def spy_log(*a, **kw):
            order.append("log")
            return real_log(*a, **kw)

        monkeypatch.setattr(todoist, "log_mutation", spy_log)
        real_post = todoist.api_post

        def spy_post(endpoint, json_data=None):
            order.append("post")
            return real_post(endpoint, json_data)

        monkeypatch.setattr(todoist, "api_post", spy_post)
        todoist.cmd_complete(args("A", forever=True))
        assert order[0] == "log"

    def test_reports_the_ended_series(self, api, audit_log, capsys):
        api.state["tasks"] = [task("A", "Alice batch cook session")]
        todoist.cmd_complete(args("A", forever=True))
        out = capsys.readouterr().out
        assert "series ended" in out
        assert "every wednesday at 9am" in out


class TestForeverOnNonRecurring:
    def test_is_a_no_op_that_says_so(self, api, audit_log, capsys):
        api.state["tasks"] = [task("A", "One-off errand", recurring=False)]
        todoist.cmd_complete(args("A", forever=True))

        posts = [c for c in api.calls if c[0] == "post"]
        assert posts == [("post", "tasks/A/close", None)], "no recurrence to strip"
        assert "no-op" in capsys.readouterr().out

    def test_due_is_absent_entirely(self, api, audit_log):
        api.state["tasks"] = [task("A", "Someday task", due=None)]
        todoist.cmd_complete(args("A", forever=True))
        posts = [c for c in api.calls if c[0] == "post"]
        assert posts == [("post", "tasks/A/close", None)]


class TestPlainCompleteUnchanged:
    def test_does_not_strip_recurrence(self, api, audit_log):
        api.state["tasks"] = [task("A", "Take out trash")]
        todoist.cmd_complete(args("A"))

        posts = [c for c in api.calls if c[0] == "post"]
        assert posts == [("post", "tasks/A/close", None)]
        entries = read_entries(audit_log)
        assert entries[0]["action"] == "complete"
        assert "forever" not in (entries[0].get("extra") or {})
