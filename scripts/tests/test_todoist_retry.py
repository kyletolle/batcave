"""Tests for todoist.py transient-failure retry (_request and the api_* wrappers).

requests.request is stubbed with a scripted sequence of responses/exceptions;
_sleep is stubbed so nothing actually waits. These test the retry *decisions*
(what retries, how often, how long it waits), not Todoist's real behaviour.
"""

import pytest
import requests

import todoist


class FakeResp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.headers = headers or {}
        self.text = text
        self.content = b"" if body is None else b"x"

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} Error")


@pytest.fixture
def scripted(monkeypatch):
    """Install a scripted requests.request; returns (script, calls, sleeps)."""
    script, calls, sleeps = [], [], []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(todoist.requests, "request", fake_request)
    monkeypatch.setattr(todoist, "_sleep", sleeps.append)
    monkeypatch.setattr(todoist, "TOKEN", "fake")
    return script, calls, sleeps


class TestGetRetries:
    def test_success_first_try_no_sleep(self, scripted):
        script, calls, sleeps = scripted
        script.append(FakeResp(200, {"results": [1], "next_cursor": None}))
        assert todoist.api_get("tasks") == [1]
        assert len(calls) == 1 and sleeps == []

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_status_then_success(self, scripted, status):
        script, calls, sleeps = scripted
        script += [FakeResp(status), FakeResp(200, {"results": [1], "next_cursor": None})]
        assert todoist.api_get("tasks") == [1]
        assert len(calls) == 2 and sleeps == [2]

    def test_connection_error_then_success(self, scripted):
        script, calls, sleeps = scripted
        script += [requests.ConnectionError("dns"), FakeResp(200, {"id": "p"})]
        assert todoist.api_get("projects/p") == {"id": "p"}
        assert len(calls) == 2

    def test_backoff_doubles(self, scripted):
        script, _, sleeps = scripted
        script += [FakeResp(503), FakeResp(503), FakeResp(503), FakeResp(200, {"id": 1})]
        todoist.api_get("x")
        assert sleeps == [2, 4, 8]

    def test_gives_up_after_attempts_and_raises(self, scripted, capsys):
        script, calls, _ = scripted
        script += [FakeResp(503, text="busy")] * todoist.RETRY_ATTEMPTS
        with pytest.raises(requests.HTTPError):
            todoist.api_get("tasks")
        assert len(calls) == todoist.RETRY_ATTEMPTS
        assert "HTTP 503: busy" in capsys.readouterr().err

    def test_final_connection_error_propagates(self, scripted):
        script, _, _ = scripted
        script += [requests.ConnectionError("down")] * todoist.RETRY_ATTEMPTS
        with pytest.raises(requests.ConnectionError):
            todoist.api_get("tasks")

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_do_not_retry(self, scripted, status):
        script, calls, sleeps = scripted
        script.append(FakeResp(status))
        with pytest.raises(requests.HTTPError):
            todoist.api_get("tasks")
        assert len(calls) == 1 and sleeps == []

    def test_retry_mid_pagination_keeps_cursor(self, scripted):
        script, calls, _ = scripted
        script += [
            FakeResp(200, {"results": [1], "next_cursor": "c1"}),
            FakeResp(502),
            FakeResp(200, {"results": [2], "next_cursor": None}),
        ]
        assert todoist.api_get("tasks") == [1, 2]
        assert len(calls) == 3


class TestRetryAfter:
    def test_honours_retry_after(self, scripted):
        script, _, sleeps = scripted
        script += [FakeResp(429, headers={"Retry-After": "7"}), FakeResp(200, {})]
        todoist.api_get("x")
        assert sleeps == [7]

    def test_caps_retry_after(self, scripted):
        script, _, sleeps = scripted
        script += [FakeResp(429, headers={"Retry-After": "3600"}), FakeResp(200, {})]
        todoist.api_get("x")
        assert sleeps == [todoist.RETRY_AFTER_CAP]

    def test_garbage_retry_after_falls_back(self, scripted):
        script, _, sleeps = scripted
        script += [FakeResp(429, headers={"Retry-After": "soon"}), FakeResp(200, {})]
        todoist.api_get("x")
        assert sleeps == [2]


class TestWritesDoNotDuplicate:
    """A write that may have landed must never be sent again."""

    def test_post_retries_on_429(self, scripted):
        script, calls, _ = scripted
        script += [FakeResp(429), FakeResp(200, {"id": "t"})]
        assert todoist.api_post("tasks", {"content": "x"}) == {"id": "t"}
        assert len(calls) == 2

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_post_does_not_retry_server_errors(self, scripted, status):
        script, calls, _ = scripted
        script.append(FakeResp(status))
        with pytest.raises(requests.HTTPError):
            todoist.api_post("tasks", {"content": "x"})
        assert len(calls) == 1

    def test_post_does_not_retry_connection_error(self, scripted):
        script, calls, _ = scripted
        script.append(requests.ConnectionError("reset"))
        with pytest.raises(requests.ConnectionError):
            todoist.api_post("tasks", {"content": "x"})
        assert len(calls) == 1

    def test_delete_does_not_retry_server_errors(self, scripted):
        script, calls, _ = scripted
        script.append(FakeResp(503))
        with pytest.raises(requests.HTTPError):
            todoist.api_delete("tasks/1")
        assert len(calls) == 1

    def test_delete_retries_on_429(self, scripted):
        script, calls, _ = scripted
        script += [FakeResp(429), FakeResp(204)]
        assert todoist.api_delete("tasks/1") is None
        assert len(calls) == 2


def test_requests_carry_timeout(monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(kwargs)
        return FakeResp(200, {})

    monkeypatch.setattr(todoist.requests, "request", fake_request)
    todoist.api_get("x")
    assert seen["timeout"] == todoist.REQUEST_TIMEOUT
