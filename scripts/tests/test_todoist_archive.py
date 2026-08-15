"""Regression tests for `todoist archive-project` / `unarchive-project`.

Archive is the gentle sibling of delete-project: it hides a project and its
tasks from every list and count without destroying anything. That difference
drives two design decisions these tests pin down:

  1. No --yes gate. delete-project demands confirmation because it cannot be
     undone; archive costs nothing to reverse, so a gate would be friction for
     its own sake.
  2. The audit entry is written AFTER the API call succeeds, not before.
     cmd_delete logs first because the log is the only surviving record of a
     destroyed task. An archived project is still retrievable from Todoist via
     archived-projects, so the log is not the last copy — and logging after
     success avoids recording archives that never happened.

Also pinned: unarchive looks projects up in the ARCHIVED list, since archived
projects vanish from GET /projects and a plain find_project would never see them.
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


def project(pid, name):
    return {"id": pid, "name": name}


def task(tid, project_id="p1"):
    return {"id": tid, "content": f"Task {tid}", "project_id": project_id}


@pytest.fixture
def api(monkeypatch):
    """Stub the API surface and record the call sequence."""
    calls = []
    state = {"projects": [], "archived": [], "tasks": []}

    def fake_get(endpoint, params=None):
        calls.append(("get", endpoint))
        if endpoint == "projects/archived":
            return state["archived"]
        if endpoint == "tasks":
            return state["tasks"]
        return state["projects"]

    def fake_post(endpoint, json_data=None):
        calls.append(("post", endpoint))
        return None

    monkeypatch.setattr(todoist, "api_get", fake_get)
    monkeypatch.setattr(todoist, "api_post", fake_post)
    monkeypatch.setattr(todoist, "get_projects", lambda: state["projects"])
    return SimpleNamespace(calls=calls, state=state)


def args(project_name):
    return SimpleNamespace(project=project_name)


class TestArchive:
    def test_archives_and_logs_with_task_count(self, api, audit_log):
        api.state["projects"] = [project("p1", "Sample Project")]
        api.state["tasks"] = [task("a"), task("b"), task("c")]

        todoist.cmd_archive_project(args("Sample Project"))

        assert ("post", "projects/p1/archive") in api.calls
        entries = read_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["action"] == "archive-project"
        assert entries[0]["extra"]["project_id"] == "p1"
        assert entries[0]["extra"]["project_name"] == "Sample Project"
        assert entries[0]["extra"]["task_count"] == 3

    def test_needs_no_confirmation_flag(self, api, audit_log):
        """Archive is reversible, so unlike delete-project it just runs."""
        api.state["projects"] = [project("p1", "Sample Project")]
        todoist.cmd_archive_project(args("Sample Project"))
        assert ("post", "projects/p1/archive") in api.calls

    def test_prints_the_undo_command(self, api, audit_log, capsys):
        api.state["projects"] = [project("p1", "Sample Project")]
        todoist.cmd_archive_project(args("Sample Project"))
        out = capsys.readouterr().out
        assert "unarchive-project" in out
        assert "Sample Project" in out

    def test_exits_when_project_missing(self, api, audit_log):
        api.state["projects"] = [project("p1", "Something else")]
        with pytest.raises(SystemExit) as exc:
            todoist.cmd_archive_project(args("Nonexistent"))
        assert exc.value.code == 1
        assert not any(c[0] == "post" for c in api.calls)
        assert read_entries(audit_log) == []

    def test_logs_after_successful_api_call(self, api, audit_log, monkeypatch):
        """Inverse of cmd_delete's ordering, and deliberately so — a failed
        archive must not leave an audit entry claiming it happened."""
        order = []
        real_log = todoist.log_mutation

        def spy_log(*a, **kw):
            order.append("log")
            return real_log(*a, **kw)

        def boom(endpoint, json_data=None):
            order.append("post")
            raise RuntimeError("API exploded")

        monkeypatch.setattr(todoist, "log_mutation", spy_log)
        monkeypatch.setattr(todoist, "api_post", boom)
        api.state["projects"] = [project("p1", "Sample Project")]

        with pytest.raises(RuntimeError):
            todoist.cmd_archive_project(args("Sample Project"))

        assert order == ["post"]
        assert read_entries(audit_log) == []


class TestUnarchive:
    def test_finds_project_in_archived_list_not_active(self, api, audit_log):
        """The whole point: an archived project is absent from GET /projects."""
        api.state["projects"] = []
        api.state["archived"] = [project("p1", "Sample Project")]

        todoist.cmd_unarchive_project(args("Sample Project"))

        assert ("post", "projects/p1/unarchive") in api.calls
        entries = read_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["action"] == "unarchive-project"
        assert entries[0]["extra"]["project_name"] == "Sample Project"

    def test_exits_and_points_at_listing_when_missing(self, api, audit_log, capsys):
        api.state["archived"] = []
        with pytest.raises(SystemExit) as exc:
            todoist.cmd_unarchive_project(args("Sample Project"))
        assert exc.value.code == 1
        assert "archived-projects" in capsys.readouterr().out
        assert read_entries(audit_log) == []


class TestFindArchivedProject:
    def test_exact_match_beats_partial(self, api):
        api.state["archived"] = [
            project("p1", "Sample Project Archive"),
            project("p2", "Sample Project"),
        ]
        assert todoist.find_archived_project("Sample Project")["id"] == "p2"

    def test_falls_back_to_partial(self, api):
        api.state["archived"] = [project("p1", "Sample Project 2024")]
        assert todoist.find_archived_project("Sample Project")["id"] == "p1"

    def test_case_insensitive(self, api):
        api.state["archived"] = [project("p1", "Sample Project")]
        assert todoist.find_archived_project("SAMPLE PROJECT")["id"] == "p1"

    def test_returns_none_when_absent(self, api):
        api.state["archived"] = [project("p1", "Something else")]
        assert todoist.find_archived_project("Sample Project") is None


class TestArchivedProjectsListing:
    def test_is_read_only(self, api, audit_log, capsys):
        api.state["archived"] = [project("p1", "Sample Project")]
        todoist.cmd_archived_projects(SimpleNamespace())
        out = capsys.readouterr().out
        assert "Sample Project" in out
        assert not any(c[0] == "post" for c in api.calls)
        assert read_entries(audit_log) == []

    def test_handles_empty(self, api, capsys):
        api.state["archived"] = []
        todoist.cmd_archived_projects(SimpleNamespace())
        assert "No archived projects" in capsys.readouterr().out
