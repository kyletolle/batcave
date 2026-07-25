"""Regression tests for `todoist delete`.

Deletion is the one mutation with no undo: `complete` leaves a task that
`uncomplete` can reopen, but a deleted task is gone from Todoist entirely.
That makes the audit log the only surviving record, and makes the ordering
(log first, THEN call the API) load-bearing rather than stylistic.

These pin down: the --yes gate, the audit entry, the cascade record, and the
write-before-delete ordering.
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


def task(tid, content="Task", parent_id=None, **overrides):
    base = {
        "id": tid,
        "content": content,
        "project_id": "proj_1",
        "section_id": None,
        "parent_id": parent_id,
        "due": {"date": "2026-07-28", "string": "every tuesday", "is_recurring": True},
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
        calls.append(("get", endpoint))
        return state["tasks"]

    def fake_delete(endpoint):
        calls.append(("delete", endpoint))
        return None

    monkeypatch.setattr(todoist, "api_get", fake_get)
    monkeypatch.setattr(todoist, "api_delete", fake_delete)
    return SimpleNamespace(calls=calls, state=state)


def args(*task_ids, yes=False):
    return SimpleNamespace(task_ids=list(task_ids), yes=yes)


class TestConfirmationGate:
    def test_refuses_without_yes(self, api, audit_log, capsys):
        api.state["tasks"] = [task("A", "Water the plants")]
        with pytest.raises(SystemExit) as exc:
            todoist.cmd_delete(args("A"))
        assert exc.value.code == 1
        # Nothing deleted, nothing logged
        assert not any(c[0] == "delete" for c in api.calls)
        assert read_entries(audit_log) == []
        assert "IRREVERSIBLE" in capsys.readouterr().out

    def test_preview_lists_targets(self, api, audit_log, capsys):
        api.state["tasks"] = [task("A", "Water the plants")]
        with pytest.raises(SystemExit):
            todoist.cmd_delete(args("A"))
        out = capsys.readouterr().out
        assert "Water the plants" in out
        assert "A" in out

    def test_exits_when_no_targets_found(self, api, audit_log):
        api.state["tasks"] = []
        with pytest.raises(SystemExit) as exc:
            todoist.cmd_delete(args("nope", yes=True))
        assert exc.value.code == 1
        assert read_entries(audit_log) == []


class TestDeletion:
    def test_deletes_and_logs(self, api, audit_log):
        api.state["tasks"] = [task("A", "Water the plants")]
        todoist.cmd_delete(args("A", yes=True))

        assert ("delete", "tasks/A") in api.calls
        entries = read_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["action"] == "delete"
        assert entries[0]["before"]["id"] == "A"
        assert entries[0]["before"]["content"] == "Water the plants"
        # Nothing exists afterwards, so there is no "after" state
        assert entries[0]["after"] is None

    def test_logs_before_api_call(self, api, audit_log, monkeypatch):
        """The audit write must land before the irreversible call. If the API
        errors mid-flight we still want the record of what was attempted on
        a task we can no longer fetch."""
        order = []

        real_log = todoist.log_mutation

        def spy_log(*a, **kw):
            order.append("log")
            return real_log(*a, **kw)

        def boom(endpoint):
            order.append("delete")
            raise RuntimeError("API exploded")

        monkeypatch.setattr(todoist, "log_mutation", spy_log)
        monkeypatch.setattr(todoist, "api_delete", boom)
        api.state["tasks"] = [task("A")]

        with pytest.raises(RuntimeError):
            todoist.cmd_delete(args("A", yes=True))

        assert order == ["log", "delete"]
        assert len(read_entries(audit_log)) == 1

    def test_multiple_ids(self, api, audit_log):
        api.state["tasks"] = [task("A", "One"), task("B", "Two")]
        todoist.cmd_delete(args("A", "B", yes=True))
        assert ("delete", "tasks/A") in api.calls
        assert ("delete", "tasks/B") in api.calls
        assert len(read_entries(audit_log)) == 2

    def test_skips_unknown_ids_but_deletes_known(self, api, audit_log, capsys):
        api.state["tasks"] = [task("A", "Real")]
        todoist.cmd_delete(args("A", "ghost", yes=True))
        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "1 deleted, 1 skipped" in out
        assert len(read_entries(audit_log)) == 1


class TestCascade:
    def test_children_recorded_in_audit(self, api, audit_log):
        api.state["tasks"] = [
            task("P", "Weekly chores"),
            task("C1", "Sweep the porch", parent_id="P"),
            task("C2", "Take out recycling", parent_id="P"),
        ]
        todoist.cmd_delete(args("P", yes=True))

        entry = read_entries(audit_log)[0]
        kids = entry["extra"]["cascade_children"]
        assert {k["content"] for k in kids} == {
            "Sweep the porch",
            "Take out recycling",
        }

    def test_children_shown_in_preview(self, api, audit_log, capsys):
        api.state["tasks"] = [
            task("P", "Weekly chores"),
            task("C1", "Sweep the porch", parent_id="P"),
        ]
        with pytest.raises(SystemExit):
            todoist.cmd_delete(args("P"))
        out = capsys.readouterr().out
        assert "subtask: Sweep the porch" in out
        assert "1 subtask(s) go with their parent(s)" in out

    def test_childless_task_has_no_extra(self, api, audit_log):
        api.state["tasks"] = [task("A", "Lonely")]
        todoist.cmd_delete(args("A", yes=True))
        assert "extra" not in read_entries(audit_log)[0]
