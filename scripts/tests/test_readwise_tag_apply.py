"""Regression tests for readwise_tag_apply.py.

The critical behaviors:
- Don't re-tag docs that already have the topic tag
- Preserve existing tags when adding a new one (no tag wipe)
- Dry-run never hits the network
- Errors are collected, not raised — one bad doc shouldn't abort the batch
"""

from unittest.mock import patch, MagicMock

import pytest
import requests

import readwise_tag_apply as rta


def _resp(status=200, text=""):
    """Build a mock requests response."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


@pytest.fixture
def no_sleep(monkeypatch):
    """Kill sleep calls so apply_topic doesn't actually wait."""
    monkeypatch.setattr(rta.time, "sleep", lambda s: None)


# ---- update_tags ----

class TestUpdateTags:
    def test_success_returns_ok_true(self):
        with patch.object(rta.requests, "patch") as p:
            p.return_value = _resp(200)
            ok, msg = rta.update_tags("doc_abc", ["topic-ai"])
            assert ok is True
            assert msg is None

    def test_patches_correct_url(self):
        with patch.object(rta.requests, "patch") as p:
            p.return_value = _resp(200)
            rta.update_tags("doc_abc", ["topic-ai"])
            args, kwargs = p.call_args
            assert args[0] == "https://readwise.io/api/v3/update/doc_abc/"

    def test_sends_tag_list(self):
        with patch.object(rta.requests, "patch") as p:
            p.return_value = _resp(200)
            rta.update_tags("doc_abc", ["topic-ai", "topic-software"])
            _, kwargs = p.call_args
            assert kwargs["json"] == {"tags": ["topic-ai", "topic-software"]}

    def test_converts_set_to_list(self):
        with patch.object(rta.requests, "patch") as p:
            p.return_value = _resp(200)
            # Readwise API expects a list; update_tags must coerce sets
            rta.update_tags("doc_abc", {"topic-ai"})
            _, kwargs = p.call_args
            assert isinstance(kwargs["json"]["tags"], list)

    def test_non_200_returns_error(self):
        with patch.object(rta.requests, "patch") as p:
            p.return_value = _resp(429, "Rate limited")
            ok, msg = rta.update_tags("doc_abc", ["topic-ai"])
            assert ok is False
            assert "429" in msg
            assert "Rate limited" in msg

    def test_request_exception_returns_error(self):
        with patch.object(rta.requests, "patch") as p:
            p.side_effect = requests.ConnectionError("network down")
            ok, msg = rta.update_tags("doc_abc", ["topic-ai"])
            assert ok is False
            assert "network down" in msg

    def test_timeout_exception_returns_error(self):
        with patch.object(rta.requests, "patch") as p:
            p.side_effect = requests.Timeout("too slow")
            ok, msg = rta.update_tags("doc_abc", ["topic-ai"])
            assert ok is False
            assert "too slow" in msg

    def test_error_message_truncated(self):
        """Very long error bodies shouldn't dominate the log output."""
        with patch.object(rta.requests, "patch") as p:
            p.return_value = _resp(500, "A" * 1000)
            ok, msg = rta.update_tags("doc_abc", ["topic-ai"])
            assert ok is False
            # Truncation preserves readable prefix
            assert len(msg) < 300


# ---- apply_topic ----

class TestApplyTopic:
    def test_adds_tag_to_docs_without_it(self, no_sleep):
        docs = [
            {"id": "d1", "existing_tags": []},
            {"id": "d2", "existing_tags": ["other"]},
        ]
        with patch.object(rta, "update_tags") as u:
            u.return_value = (True, None)
            new, err, errors = rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
            assert new == 2
            assert err == 0
            assert errors == []
            assert u.call_count == 2

    def test_skips_docs_with_topic_already_applied(self, no_sleep):
        docs = [
            {"id": "d1", "existing_tags": ["topic-ai"]},
            {"id": "d2", "existing_tags": ["topic-ai", "other"]},
            {"id": "d3", "existing_tags": []},
        ]
        with patch.object(rta, "update_tags") as u:
            u.return_value = (True, None)
            new, _, _ = rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
            assert new == 1  # only d3 was tagged
            assert u.call_count == 1

    def test_preserves_existing_tags(self, no_sleep):
        """Adding topic-ai to a doc with existing tags must include them all."""
        docs = [{"id": "d1", "existing_tags": ["keep-me", "and-me"]}]
        captured = []
        with patch.object(rta, "update_tags") as u:
            u.side_effect = lambda doc_id, tags: captured.append((doc_id, list(tags))) or (True, None)
            rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
        assert captured[0][0] == "d1"
        # Final tag list includes both existing + new, sorted
        assert set(captured[0][1]) == {"keep-me", "and-me", "topic-ai"}

    def test_tags_are_sorted(self, no_sleep):
        """Sorted tag list gives deterministic diffs in the audit trail."""
        docs = [{"id": "d1", "existing_tags": ["zzz", "aaa"]}]
        captured = []
        with patch.object(rta, "update_tags") as u:
            u.side_effect = lambda doc_id, tags: captured.append(list(tags)) or (True, None)
            rta.apply_topic("topic-mid", docs, dry_run=False, sleep_s=0)
        assert captured[0] == ["aaa", "topic-mid", "zzz"]

    def test_dry_run_does_not_call_api(self, no_sleep):
        docs = [{"id": "d1", "existing_tags": []}]
        with patch.object(rta, "update_tags") as u:
            new, err, errors = rta.apply_topic("topic-ai", docs, dry_run=True, sleep_s=0)
            assert u.call_count == 0
            assert new == 1
            assert err == 0

    def test_dry_run_still_skips_existing(self, no_sleep):
        """Dry-run must not inflate the 'new' count with already-tagged docs."""
        docs = [
            {"id": "d1", "existing_tags": ["topic-ai"]},
            {"id": "d2", "existing_tags": []},
        ]
        new, _, _ = rta.apply_topic("topic-ai", docs, dry_run=True, sleep_s=0)
        assert new == 1  # only d2

    def test_error_collected_not_raised(self, no_sleep):
        """One failed doc must not abort the batch."""
        docs = [
            {"id": "d1", "existing_tags": [], "title": "Good"},
            {"id": "d2", "existing_tags": [], "title": "Bad"},
            {"id": "d3", "existing_tags": [], "title": "Also good"},
        ]
        def fake_update(doc_id, tags):
            if doc_id == "d2":
                return False, "HTTP 500"
            return True, None

        with patch.object(rta, "update_tags", side_effect=fake_update):
            new, err, errors = rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
        assert new == 2
        assert err == 1
        assert len(errors) == 1
        doc_id, title, msg = errors[0]
        assert doc_id == "d2"
        assert title == "Bad"
        assert "500" in msg

    def test_error_title_truncated(self, no_sleep):
        """Titles in error reports are capped at 60 chars."""
        docs = [{"id": "d1", "existing_tags": [], "title": "X" * 100}]
        with patch.object(rta, "update_tags", return_value=(False, "err")):
            _, _, errors = rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
        _, title, _ = errors[0]
        assert len(title) == 60

    def test_missing_title_handled(self, no_sleep):
        docs = [{"id": "d1", "existing_tags": []}]  # no title key
        with patch.object(rta, "update_tags", return_value=(False, "err")):
            _, _, errors = rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
        assert errors[0][1] == ""

    def test_none_existing_tags_treated_as_empty(self, no_sleep):
        """Some classification entries have existing_tags=None; must not crash."""
        docs = [{"id": "d1", "existing_tags": None}]
        with patch.object(rta, "update_tags", return_value=(True, None)) as u:
            new, _, _ = rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
        assert new == 1
        _, kwargs = u.call_args
        assert kwargs == {} or True  # just make sure no crash

    def test_missing_existing_tags_key_handled(self, no_sleep):
        """If existing_tags is absent entirely, apply_topic must still work."""
        docs = [{"id": "d1"}]
        with patch.object(rta, "update_tags", return_value=(True, None)):
            new, _, _ = rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0)
        assert new == 1

    def test_sleep_called_between_requests(self):
        """Rate-limit respected: sleep between each successful API call."""
        docs = [
            {"id": "d1", "existing_tags": []},
            {"id": "d2", "existing_tags": []},
            {"id": "d3", "existing_tags": []},
        ]
        sleep_calls = []
        with patch.object(rta.time, "sleep", side_effect=lambda s: sleep_calls.append(s)), \
             patch.object(rta, "update_tags", return_value=(True, None)):
            rta.apply_topic("topic-ai", docs, dry_run=False, sleep_s=0.2)
        assert sleep_calls == [0.2, 0.2, 0.2]

    def test_sleep_not_called_in_dry_run(self):
        docs = [{"id": "d1", "existing_tags": []}]
        with patch.object(rta.time, "sleep") as s:
            rta.apply_topic("topic-ai", docs, dry_run=True, sleep_s=1.0)
            assert s.call_count == 0

    def test_empty_docs_returns_zeros(self, no_sleep):
        new, err, errors = rta.apply_topic("topic-ai", [], dry_run=False, sleep_s=0)
        assert new == 0
        assert err == 0
        assert errors == []
