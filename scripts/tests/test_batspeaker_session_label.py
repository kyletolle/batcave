"""Tab labels: the session name (custom-title) wins over aiTitle / first prompt."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import batspeaker_core as core  # noqa: E402


def _write(tmp_path, entries):
    p = tmp_path / "abc123.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(p)


USER = {"type": "user", "sessionId": "abc123", "timestamp": "2026-11-09T01:00:00Z",
        "message": {"role": "user", "content": "start the boabw session"}}


def test_session_name_is_the_label(tmp_path):
    meta = core._light_session_meta(_write(tmp_path, [
        {"type": "custom-title", "customTitle": "2026-11-09:1", "sessionId": "abc123"},
        USER,
        {"type": "ai-title", "aiTitle": "BoaBW session", "sessionId": "abc123"},
        {"type": "custom-title", "customTitle": "2026-11-09:3", "sessionId": "abc123"},
    ]))
    assert meta["title"] == "2026-11-09:3"      # last name wins
    assert meta["topic"] == "BoaBW session"


def test_falls_back_without_a_session_name(tmp_path):
    meta = core._light_session_meta(_write(tmp_path, [USER]))
    assert meta["title"] == meta["topic"] == "start the boabw session"
