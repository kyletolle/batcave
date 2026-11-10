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


def _session(tmp_path, stem, entries):
    p = tmp_path / f"{stem}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(p)


def _user(sid, ts, text):
    return {"type": "user", "sessionId": sid, "timestamp": ts,
            "message": {"role": "user", "content": text}}


def test_duplicate_names_get_a_letter_in_start_order(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    name = {"type": "custom-title", "customTitle": "2026-11-09:1"}
    _session(proj, "later", [name, _user("later", "2026-11-09T02:00:00Z", "after the clear")])
    _session(proj, "first", [name, _user("first", "2026-11-09T01:00:00Z", "before the clear")])
    _session(proj, "other", [{"type": "custom-title", "customTitle": "2026-11-09:2"},
                             _user("other", "2026-11-09T03:00:00Z", "unrelated")])
    _session(proj, "unnamed1", [_user("unnamed1", "2026-11-09T04:00:00Z", "same words")])
    _session(proj, "unnamed2", [_user("unnamed2", "2026-11-09T05:00:00Z", "same words")])
    monkeypatch.setattr(core, "PROJECTS_ROOT", str(tmp_path))
    titles = {m["id"]: m["title"] for m in core.list_sessions(active_within_hours=None)}
    assert titles["first"] == "2026-11-09:1"
    assert titles["later"] == "2026-11-09:1b"
    assert titles["other"] == "2026-11-09:2"
    # only real session names are lettered, never first-prompt fallbacks
    assert titles["unnamed1"] == titles["unnamed2"] == "same words"


AWAY = {"type": "system", "subtype": "away_summary", "timestamp": "2026-11-09T01:30:00Z",
        "content": "Fixing the tabs; tests pass. (disable recaps in /config)"}


def test_recap_is_the_latest_away_summary(tmp_path):
    r = core.session_recap(_session(tmp_path, "s", [
        _user("s", "2026-11-09T01:00:00Z", "fix the tabs"),
        dict(AWAY, content="older recap"),
        AWAY,
    ]))
    assert r["recap"] == "Fixing the tabs; tests pass."     # /config hint stripped
    assert r["recap_ts"] == "2026-11-09T01:30:00Z"
    assert r["turns_since"] == 0


def test_recap_counts_prompts_since(tmp_path):
    r = core.session_recap(_session(tmp_path, "s", [
        _user("s", "2026-11-09T01:00:00Z", "fix the tabs"),
        AWAY,
        _user("s", "2026-11-09T02:00:00Z", "now the recap card"),
        _user("s", "2026-11-09T03:00:00Z", "and a jump button"),
    ]))
    assert r["turns_since"] == 2
    assert r["last_prompt"] == "and a jump button"


def test_recap_falls_back_to_prompts(tmp_path):
    r = core.session_recap(_session(tmp_path, "s", [
        _user("s", "2026-11-09T01:00:00Z", "<command-name>/clear</command-name>"),
        _user("s", "2026-11-09T01:01:00Z", "<command-name>/start-boabw</command-name>"),
        _user("s", "2026-11-09T02:00:00Z", "x" * 400),
        _user("s", "2026-11-09T02:01:00Z", "<local-command-stdout>Set model</local-command-stdout>"),
    ]))
    assert r["recap"] is None and r["turns_since"] == 0
    assert r["first_prompt"] == "/start-boabw"              # /clear is not a prompt
    assert len(r["last_prompt"]) == 220 and r["last_prompt"].endswith("…")


def test_question_line_shows_a_slash_command_as_typed():
    wrapped = ("<command-message>sleep-morning</command-message>\n"
               "<command-name>/sleep-morning</command-name>\n"
               "<command-args>up at 07:40</command-args>")
    assert core.prompt_caption(wrapped) == "/sleep-morning up at 07:40"
    assert core.prompt_caption("<command-name>/clear</command-name>\n  "
                               "<command-message>clear</command-message>\n  "
                               "<command-args></command-args>") == "/clear"
    assert core.prompt_caption("<command-message>wrap</command-message>") == "/wrap"


def test_question_line_leaves_ordinary_prompts_alone():
    assert core.prompt_caption("why does  <div> break\nthe layout?") == "why does <div> break the layout?"
    assert core.prompt_caption("x" * 300, 200) == "x" * 199 + "…"
    assert core.prompt_caption('<pasted_content id="ab12"> look at\nthis </pasted_content> please') == "look at this please"
