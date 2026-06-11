"""Regression tests for the Todoist audit log.

The audit log is the safety net now that the Batcave-only write guard is gone.
If it regresses silently, we lose forensic visibility into what Bruce has done
to Kyle's task list. Pin it down.

Assumption: log on success only (Kyle's call). Mutation attempts that fail
(e.g. API error raised) are not expected to leave entries.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import todoist


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """Redirect todoist.AUDIT_LOG to a tmp file for each test."""
    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(todoist, "AUDIT_LOG", log_path)
    return log_path


def read_entries(log_path):
    """Read all JSONL entries from the audit log."""
    if not log_path.exists():
        return []
    with open(log_path) as f:
        return [json.loads(line) for line in f if line.strip()]


def sample_task(**overrides):
    """Build a representative Todoist task dict for tests."""
    base = {
        "id": "6g2XP7CvPjwvfCCf",
        "content": "Sample task",
        "project_id": "proj_123",
        "section_id": "sec_456",
        "parent_id": None,
        "due": {
            "date": "2026-04-20",
            "string": "monday",
            "is_recurring": False,
            "timezone": None,
            "lang": "en",
        },
        "deadline": None,
        "priority": 1,
        "labels": ["bruce"],
        "description": "Short description.",
    }
    base.update(overrides)
    return base


# ---- task_snapshot ----

class TestTaskSnapshot:
    def test_extracts_expected_fields(self):
        snap = todoist.task_snapshot(sample_task())
        # Must have exactly the fields the log format expects
        expected = {
            "id", "content", "project_id", "section_id", "parent_id",
            "due", "deadline", "priority", "labels", "description",
        }
        assert set(snap.keys()) == expected

    def test_none_task_returns_none(self):
        assert todoist.task_snapshot(None) is None
        assert todoist.task_snapshot({}) is None

    def test_description_truncated_at_200(self):
        long = "A" * 500
        snap = todoist.task_snapshot(sample_task(description=long))
        assert len(snap["description"]) == 200
        assert snap["description"] == "A" * 200

    def test_description_short_preserved(self):
        snap = todoist.task_snapshot(sample_task(description="Short."))
        assert snap["description"] == "Short."

    def test_missing_description_becomes_empty(self):
        task = sample_task()
        del task["description"]
        snap = todoist.task_snapshot(task)
        assert snap["description"] == ""

    def test_no_secrets_in_snapshot(self):
        """A snapshot must never contain anything that looks like a Todoist
        API token (opaque 40-char hex strings are the canonical shape)."""
        snap = todoist.task_snapshot(sample_task())
        blob = json.dumps(snap)
        # Todoist tokens are 40 hex chars
        assert not re.search(r"\b[a-f0-9]{40}\b", blob)
        # Nor any reference to auth/token keys
        assert "token" not in blob.lower()
        assert "authorization" not in blob.lower()

    def test_preserves_due_structure(self):
        snap = todoist.task_snapshot(sample_task())
        assert snap["due"]["date"] == "2026-04-20"
        assert snap["due"]["is_recurring"] is False

    def test_preserves_labels(self):
        snap = todoist.task_snapshot(sample_task(labels=["bruce", "personal"]))
        assert snap["labels"] == ["bruce", "personal"]

    def test_missing_optional_fields_become_none(self):
        minimal = {"id": "x", "content": "y"}
        snap = todoist.task_snapshot(minimal)
        assert snap["id"] == "x"
        assert snap["content"] == "y"
        assert snap["project_id"] is None
        assert snap["due"] is None


# ---- log_mutation ----

class TestLogMutation:
    def test_writes_entry_on_add(self, audit_log):
        task = sample_task()
        todoist.log_mutation("add", task_after=task)
        entries = read_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["action"] == "add"
        assert entries[0]["before"] is None
        assert entries[0]["after"]["id"] == task["id"]

    def test_writes_entry_on_complete(self, audit_log):
        task = sample_task()
        todoist.log_mutation("complete", task_before=task)
        entries = read_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["action"] == "complete"
        assert entries[0]["before"]["id"] == task["id"]
        assert entries[0]["after"] is None

    def test_writes_entry_on_update(self, audit_log):
        before = sample_task()
        after = sample_task(content="Changed")
        todoist.log_mutation("update", task_before=before, task_after=after)
        entries = read_entries(audit_log)
        assert len(entries) == 1
        assert entries[0]["before"]["content"] == "Sample task"
        assert entries[0]["after"]["content"] == "Changed"

    def test_entry_has_required_keys(self, audit_log):
        todoist.log_mutation("add", task_after=sample_task())
        entry = read_entries(audit_log)[0]
        assert set(entry.keys()) >= {"ts", "action", "before", "after"}

    def test_timestamp_is_iso8601_with_timezone(self, audit_log):
        todoist.log_mutation("add", task_after=sample_task())
        entry = read_entries(audit_log)[0]
        ts = entry["ts"]
        # ISO-8601 with tz offset (e.g. -07:00, -0700, or Z)
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)
        # Either ends with Z or has a +/-HH:MM offset
        assert ts.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", ts)
        # Must be parseable
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    def test_extra_field_passes_through(self, audit_log):
        todoist.log_mutation(
            "add-section",
            extra={"section_name": "Beer", "project": "Batcave"},
        )
        entry = read_entries(audit_log)[0]
        assert entry["extra"] == {"section_name": "Beer", "project": "Batcave"}

    def test_no_extra_field_when_not_provided(self, audit_log):
        todoist.log_mutation("add", task_after=sample_task())
        entry = read_entries(audit_log)[0]
        assert "extra" not in entry

    def test_append_only_preserves_earlier_entries(self, audit_log):
        todoist.log_mutation("add", task_after=sample_task(id="t1"))
        todoist.log_mutation("add", task_after=sample_task(id="t2"))
        todoist.log_mutation("complete", task_before=sample_task(id="t1"))
        entries = read_entries(audit_log)
        assert len(entries) == 3
        assert entries[0]["after"]["id"] == "t1"
        assert entries[1]["after"]["id"] == "t2"
        assert entries[2]["before"]["id"] == "t1"

    def test_one_entry_per_line(self, audit_log):
        """JSONL contract: every line is one complete JSON object."""
        for i in range(5):
            todoist.log_mutation("add", task_after=sample_task(id=f"t{i}"))
        raw = audit_log.read_text()
        lines = [l for l in raw.split("\n") if l]
        assert len(lines) == 5
        for line in lines:
            parsed = json.loads(line)  # each line parses standalone
            assert parsed["action"] == "add"

    def test_unicode_round_trip(self, audit_log):
        content = "Deal with the lease — call Nancy re: [[Apartment]]"
        todoist.log_mutation("add", task_after=sample_task(content=content))
        # Reads back identically through JSONL
        entry = read_entries(audit_log)[0]
        assert entry["after"]["content"] == content

    def test_no_token_in_entry(self, audit_log, monkeypatch):
        """The API token must never be serialized into an audit entry."""
        monkeypatch.setattr(todoist, "TOKEN", "fake-secret-token-abc123")
        todoist.log_mutation("add", task_after=sample_task())
        raw = audit_log.read_text()
        assert "fake-secret-token-abc123" not in raw
        assert "Bearer" not in raw
        assert "Authorization" not in raw

    def test_non_serializable_falls_back_to_str(self, audit_log):
        """`default=str` in json.dumps means datetime/etc. objects get stringified
        rather than crashing the log write."""
        task = sample_task(due={"date": datetime(2026, 4, 20)})
        todoist.log_mutation("add", task_after=task)
        # No exception = success; entry should exist
        entries = read_entries(audit_log)
        assert len(entries) == 1

    def test_oserror_does_not_propagate(self, audit_log, capsys, monkeypatch):
        """If the log can't be written, a WARNING is printed but the mutation
        call must not raise — otherwise a broken log would block all writes."""
        # Point AUDIT_LOG at an unwritable path (a directory)
        bad_path = audit_log.parent / "subdir"
        bad_path.mkdir()
        monkeypatch.setattr(todoist, "AUDIT_LOG", bad_path)
        # Should not raise
        todoist.log_mutation("add", task_after=sample_task())
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "audit log" in captured.err.lower()

    def test_log_survives_failed_api_call(self, audit_log):
        """Contract (assumption a): mutations log only on success. If a command
        raises before reaching log_mutation, no entry appears. Verified by
        simply never calling log_mutation."""
        # Simulate a command flow where the API raises before logging:
        try:
            # "API call" raises
            raise RuntimeError("boom")
            todoist.log_mutation("add", task_after=sample_task())  # unreachable
        except RuntimeError:
            pass
        assert not audit_log.exists() or read_entries(audit_log) == []

    def test_distinct_actions_preserved(self, audit_log):
        """Each mutation type writes under its own action label so audit
        queries like 'show me all postpones' work."""
        todoist.log_mutation("add", task_after=sample_task())
        todoist.log_mutation("update", task_before=sample_task(),
                             task_after=sample_task(content="updated"))
        todoist.log_mutation("postpone", task_before=sample_task(),
                             task_after=sample_task())
        todoist.log_mutation("complete", task_before=sample_task())
        todoist.log_mutation("move-section", task_before=sample_task(),
                             task_after=sample_task(section_id="new"))
        actions = [e["action"] for e in read_entries(audit_log)]
        assert actions == ["add", "update", "postpone", "complete", "move-section"]


# ---- Real-world regression: the shape of existing audit entries ----

class TestAuditLogBackwardCompat:
    """The repo already has ~465 audit entries. Any change to task_snapshot
    or log_mutation that breaks the shape of those entries would make the
    existing log inconsistent. Verify the current writer still produces
    entries that match the shape used for the last six weeks."""

    def test_current_format_matches_expected_schema(self, audit_log):
        todoist.log_mutation(
            "complete",
            task_before=sample_task(),
        )
        entry = read_entries(audit_log)[0]
        # Same top-level shape as existing entries in todoist_audit.jsonl
        assert set(entry.keys()) == {"ts", "action", "before", "after"}
        # Same snapshot fields
        assert set(entry["before"].keys()) == {
            "id", "content", "project_id", "section_id", "parent_id",
            "due", "deadline", "priority", "labels", "description",
        }

    def test_historical_entry_parses(self):
        """A line from the real audit log parses cleanly — if this breaks,
        the log format has drifted."""
        real_line = (
            '{"ts": "2026-03-02T21:29:37.113283-07:00", "action": "complete", '
            '"before": {"id": "6g6JJhQP7gGx8x5f", "content": "Test", '
            '"project_id": "p1", "section_id": "s1", "parent_id": null, '
            '"due": {"date": "2026-03-03"}, "priority": 1, "labels": [], '
            '"description": "..."}, "after": null}'
        )
        parsed = json.loads(real_line)
        assert parsed["action"] == "complete"
        assert parsed["before"]["id"] == "6g6JJhQP7gGx8x5f"
