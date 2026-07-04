#!/usr/bin/env python3
"""Bat-Speaker v2 core: shared transcript parsing, TTS synthesis, and a tiny
markdown renderer.

The v2 server treats Claude Code's own per-session transcript JSONL files as the
source of truth (no hand-built rolling note, no Stop-hook race). This module
holds everything the server needs that isn't HTTP: scan/parse transcripts into
per-session turns, synthesize a turn to mp3 via the configured engine, and turn
a turn's markdown into readable HTML.

Much of the synthesis + sanitize logic is lifted from batspeaker_hook.py (the v1
pipeline, kept registered as a fallback until v2 is confirmed). Once v2 is
proven, the hook + Live note can be retired and this becomes the only path.
"""

import os, re, json, glob, html, subprocess, time

# ---------- paths / config ----------

HOME = os.path.expanduser("~")
PROJECTS_ROOT = os.path.join(HOME, ".claude", "projects")
CONFIG = os.path.join(HOME, ".config", "batspeaker", "config.json")
AUDIO_CACHE = os.path.join(HOME, ".cache", "batspeaker", "audio")
TTS = os.path.join(HOME, ".local", "bin", "tts")

DEFAULTS = {
    "engine": "openai",    # openai | deepgram | elevenlabs | unreal
    "voice": "ash",        # OpenAI voice
    "model": "tts-1-hd",   # OpenAI model
    "deepgram_voice": "orpheus",
    "elevenlabs_voice": "nPczCjzI2devNBz1zQrb",
    "elevenlabs_model": "eleven_multilingual_v2",
    "unreal_voice": "Scarlett",   # Unreal Speech: Scarlett|Dan|Liv|Will|Amy
    "speed": "1.0",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def get_env(name):
    """Env var, falling back to parsing ~/.env.sh (the server may not inherit
    keys the `tts` wrapper sources for itself)."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        with open(os.path.join(HOME, ".env.sh")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith(name + "="):
                    val = line.split("=", 1)[1].strip()
                    if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                        val = val[1:-1]
                    return val
    except Exception:
        pass
    return None


# ---------- transcript parsing ----------

NARRATION_MAX = 120   # short pre-tool prose treated as "let me check..." narration


def is_real_user(entry):
    """A genuine user prompt, not a tool_result echo or meta line."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


def _user_text(entry):
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p.strip()).strip()
    return ""


def _join_runs(runs):
    return "\n\n".join(t for t, _ in runs if t.strip())


def _spoken_runs(runs):
    """Keep substantial runs and the turn's final prose; drop only short
    narration runs that precede a tool call."""
    out = []
    for i, (text, followed_by_tool) in enumerate(runs):
        is_last = i == len(runs) - 1
        if (not followed_by_tool) or is_last or len(text.strip()) > NARRATION_MAX:
            out.append((text, followed_by_tool))
    return out


def parse_turns(path):
    """Parse a transcript into ordered turns. Each turn is a dict:
        id       stable id = the opening user message's uuid (stable while the
                 assistant's reply is still growing, so live updates and the TTS
                 cache key by the same value start to finish)
        ts       ISO timestamp of the turn's last assistant message
        user     the user prompt text that opened the turn
        runs     list of [text, followed_by_tool] assistant prose runs
    Only turns with assistant prose are returned.
    """
    turns = []
    user_text = ""
    user_uuid = None
    start_ts = None
    last_ts = None
    runs = []
    buf = []

    def close_run(followed_by_tool):
        text = "\n\n".join(t for t in buf if t.strip())
        buf.clear()
        if text.strip():
            runs.append([text, followed_by_tool])
        elif followed_by_tool and runs:
            runs[-1][1] = True

    def end_turn():
        nonlocal runs
        close_run(False)
        if runs:
            turns.append({
                "id": user_uuid or (start_ts or str(len(turns))),
                "ts": last_ts or start_ts,
                "user": user_text,
                "runs": [list(r) for r in runs],
            })
        runs = []

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if is_real_user(e):
                    end_turn()
                    user_text = _user_text(e)
                    user_uuid = e.get("uuid")
                    start_ts = e.get("timestamp")
                    last_ts = None
                elif e.get("type") == "assistant":
                    last_ts = e.get("timestamp") or last_ts
                    content = e.get("message", {}).get("content")
                    if isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "text":
                                buf.append(b.get("text", ""))
                            elif b.get("type") == "tool_use":
                                close_run(True)
    except FileNotFoundError:
        return []
    end_turn()
    return turns


def turn_full_md(turn):
    """All assistant prose in the turn (markdown), for the reading surface."""
    return _join_runs(turn["runs"])


def turn_spoken_text(turn):
    """The substance of the turn, sanitized for TTS (whole turn, narration
    trimmed)."""
    return sanitize(_join_runs(_spoken_runs(turn["runs"])))


# ---------- session discovery ----------

def _clean_label(text):
    """Turn a first-prompt into a readable tab label: unwrap slash-command XML,
    strip remaining tags, collapse whitespace."""
    if not text:
        return ""
    m = re.search(r"<command-name>\s*([^<]+?)\s*</command-name>", text)
    if m:
        return m.group(1).strip()[:80]
    m = re.search(r"<command-message>\s*([^<]+?)\s*</command-message>", text)
    if m:
        return m.group(1).strip()[:80]
    text = re.sub(r"<[^>]+>", " ", text)            # drop any stray tags
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80]


def _light_session_meta(path):
    """Cheap scan of a transcript for tab metadata, without building full turns.
    Returns dict or None if the file has no real turns."""
    title = None          # last aiTitle wins
    first_prompt = None
    cwd = None
    branch = None
    turn_count = 0
    last_ts = None
    session_id = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("aiTitle"):
                    title = e["aiTitle"]
                if cwd is None and e.get("cwd"):
                    cwd = e["cwd"]
                if branch is None and e.get("gitBranch"):
                    branch = e["gitBranch"]
                if e.get("sessionId"):
                    session_id = e["sessionId"]
                if is_real_user(e):
                    turn_count += 1
                    if first_prompt is None:
                        first_prompt = _user_text(e)
                    if e.get("timestamp"):
                        last_ts = e["timestamp"]
                elif e.get("type") == "assistant" and e.get("timestamp"):
                    last_ts = e["timestamp"]
    except Exception:
        return None
    if turn_count == 0:
        return None
    label = title or _clean_label(first_prompt) or session_id[:8]
    return {
        "id": session_id,
        "path": path,
        "title": label,
        "cwd": cwd or "",
        "branch": branch or "",
        "turns": turn_count,
        "last_ts": last_ts,
        "mtime": os.path.getmtime(path),
    }


def list_sessions(active_within_hours=48, limit=40):
    """All Claude Code sessions on the box with real turns, most recently
    conversed-in first. `active_within_hours` filters by file mtime; None
    disables it."""
    metas = []
    cutoff = time.time() - active_within_hours * 3600 if active_within_hours else None
    for path in glob.glob(os.path.join(PROJECTS_ROOT, "*", "*.jsonl")):
        try:
            if cutoff and os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        m = _light_session_meta(path)
        if m:
            metas.append(m)
    # Order by the last real conversation message, not file mtime — transcripts
    # get touched by non-turn writes (titles, summaries), which made tabs drift.
    metas.sort(key=lambda m: (m["last_ts"] or "", m["mtime"]), reverse=True)
    return metas[:limit]


def session_path(session_id):
    """Resolve a session id to its transcript path (exact stem match)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id or ""):
        return None
    hits = glob.glob(os.path.join(PROJECTS_ROOT, "*", session_id + ".jsonl"))
    return hits[0] if hits else None


# ---------- markdown -> speech ----------

def sanitize(md):
    s = md
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!\[\[[^\]]*\]\]", " ", s)
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*_]{3,}\s*$", "", s, flags=re.M)
    s = re.sub(r"[*_~]{1,3}", "", s)
    # TTS engines garble words around em dashes; normalize to commas (Kyle's pref).
    s = re.sub(r"\s*(?:—|--)\s*", ", ", s)
    s = re.sub(r"\s*,(?:\s*,)+", ",", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def caption(text, n=160):
    one = re.sub(r"\s+", " ", text).strip()
    return (one[:n].rstrip() + "…") if len(one) > n else one


# ---------- markdown -> HTML (tiny, dependency-free) ----------

def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    return s


def md_to_html(md):
    """Modest markdown renderer: paragraphs, headings, fenced code, blockquotes,
    bullet/numbered lists, and inline emphasis/code/links. Enough to read Bruce's
    prose comfortably; not a spec-complete converter."""
    out = []
    lines = md.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # fenced code block
        m = re.match(r"^\s*```(\w*)\s*$", line)
        if m:
            i += 1
            code = []
            while i < n and not re.match(r"^\s*```\s*$", lines[i]):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            continue
        # heading
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue
        # horizontal rule
        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue
        # blockquote (collapse consecutive)
        if re.match(r"^\s*>\s?", line):
            quote = []
            while i < n and re.match(r"^\s*>\s?", lines[i]):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + _inline("\n".join(quote)).replace("\n", "<br>") + "</blockquote>")
            continue
        # unordered list
        if re.match(r"^\s*[-*+]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append("<li>" + _inline(re.sub(r"^\s*[-*+]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        # ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append("<li>" + _inline(re.sub(r"^\s*\d+\.\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        # blank
        if not line.strip():
            i += 1
            continue
        # paragraph (gather until blank or block start)
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
                r"^\s*(```|#{1,6}\s|>\s?|[-*+]\s|\d+\.\s|[-*_]{3,}\s*$)", lines[i]):
            para.append(lines[i])
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


# ---------- TTS synthesis ----------

def chunk_text(text, limit):
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in re.split(r"(\n\n+)", text):
        if len(cur) + len(para) <= limit:
            cur += para
        elif len(para) <= limit:
            if cur.strip():
                chunks.append(cur)
            cur = para
        else:
            for sent in re.split(r"(?<=[.!?])\s+", para):
                if len(cur) + len(sent) + 1 <= limit:
                    cur += ((" " if cur else "") + sent)
                else:
                    if cur.strip():
                        chunks.append(cur)
                    cur = sent
    if cur.strip():
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def concat_mp3s(parts, out):
    listf = out + ".list"
    with open(listf, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
                    "-c", "copy", out], capture_output=True)
    try:
        os.remove(listf)
    except Exception:
        pass


def apply_speed(path, speed):
    try:
        if float(speed) == 1.0:
            return
    except (ValueError, TypeError):
        return
    tmp = path + ".spd.mp3"
    r = subprocess.run(["ffmpeg", "-y", "-i", path, "-filter:a", f"atempo={speed}",
                        tmp], capture_output=True)
    if r.returncode == 0 and os.path.exists(tmp):
        os.replace(tmp, path)


def _deepgram_one(text, voice, out):
    import urllib.request
    key = get_env("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY not found")
    req = urllib.request.Request(
        f"https://api.deepgram.com/v1/speak?model=aura-{voice}-en",
        data=json.dumps({"text": text}).encode(),
        headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(out, "wb") as f:
        f.write(resp.read())


def synth_deepgram(text, voice, out):
    chunks = chunk_text(text, 1900)
    if len(chunks) == 1:
        _deepgram_one(chunks[0], voice, out)
        return
    parts = []
    for i, c in enumerate(chunks):
        p = out.replace(".mp3", f".part{i}.mp3")
        _deepgram_one(c, voice, p)
        parts.append(p)
    concat_mp3s(parts, out)
    for p in parts:
        try:
            os.remove(p)
        except Exception:
            pass


def synth_elevenlabs(text, voice_id, model_id, out):
    import urllib.request
    key = get_env("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY not found")
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps({"text": text, "model_id": model_id}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(out, "wb") as f:
        f.write(resp.read())


# Unreal sits behind Cloudflare, which bans urllib's default UA (error 1010).
# A normal User-Agent is required on every hop, including the OutputUri fetch.
_UNREAL_UA = "Mozilla/5.0 (X11; Linux x86_64) batspeaker/1.0"

# v7 and v8 have DISJOINT voice rosters, so each voice routes to its own endpoint.
UNREAL_V7_VOICES = ["Scarlett", "Dan", "Liv", "Will", "Amy"]
# All v8 MALE voices, grouped by accent (Bruce is male-coded). Female v8 voices
# omitted by intent; add from the full roster if ever wanted.
UNREAL_V8_VOICES = [
    "Ethan", "Daniel", "Noah", "Zane", "Rowan", "Jasper", "Caleb", "Ronan",  # American
    "Arthur", "Edward", "Oliver", "Benjamin",                                  # British
    "Mateo", "Javier", "Luca", "Thiago", "Rafael",                            # Spanish/Italian/Portuguese
    "Arjun", "Rohan", "Haruto", "Wei", "Jian", "Hao", "Sheng",               # Hindi/Japanese/Chinese
]
UNREAL_VOICES = UNREAL_V7_VOICES + UNREAL_V8_VOICES       # panel order
_UNREAL_V7 = set(UNREAL_V7_VOICES)


def _unreal_endpoint(voice):
    return ("https://api.v7.unrealspeech.com/speech" if voice in _UNREAL_V7
            else "https://api.v8.unrealspeech.com/speech")


def _unreal_one(text, voice, out):
    import urllib.request
    key = get_env("UNREAL_SPEECH_API_KEY")
    if not key:
        raise RuntimeError("UNREAL_SPEECH_API_KEY not found")
    req = urllib.request.Request(
        _unreal_endpoint(voice),
        data=json.dumps({"Text": text[:3000], "VoiceId": voice}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": _UNREAL_UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        meta = json.loads(resp.read())
    uri = meta.get("OutputUri")
    if not uri:
        raise RuntimeError(f"unreal: no OutputUri ({str(meta)[:150]})")
    audio_req = urllib.request.Request(uri, headers={"User-Agent": _UNREAL_UA})
    with urllib.request.urlopen(audio_req, timeout=120) as resp, open(out, "wb") as f:
        f.write(resp.read())


def synth_unreal(text, voice, out):
    chunks = chunk_text(text, 3000)        # Unreal /speech per-call cap
    if len(chunks) == 1:
        _unreal_one(chunks[0], voice, out)
        return
    parts = []
    for i, c in enumerate(chunks):
        p = out.replace(".mp3", f".part{i}.mp3")
        _unreal_one(c, voice, p)
        parts.append(p)
    concat_mp3s(parts, out)
    for p in parts:
        try:
            os.remove(p)
        except Exception:
            pass


def synth(text, cfg=None, out=None):
    """Render `text` to mp3 `out` via the configured engine. Raises on failure."""
    cfg = cfg or load_config()
    engine = cfg.get("engine", "openai")
    if engine == "openai":
        # tts.sh reads OPENAI_API_KEY from its env, but the systemd-run server
        # doesn't inherit it — inject from ~/.env.sh like the other engines do.
        key = get_env("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not found")
        env = {**os.environ, "OPENAI_API_KEY": key}
        r = subprocess.run([TTS, "-v", cfg["voice"], "-m", cfg["model"],
                            "-s", str(cfg["speed"]), "-o", out],
                           input=text, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"tts rc={r.returncode}: {r.stderr.strip()[:200]}")
        return
    if engine == "deepgram":
        synth_deepgram(text, cfg["deepgram_voice"], out)
    elif engine == "elevenlabs":
        synth_elevenlabs(text, cfg["elevenlabs_voice"], cfg["elevenlabs_model"], out)
    elif engine == "unreal":
        synth_unreal(text, cfg["unreal_voice"], out)
    else:
        raise RuntimeError(f"unknown engine: {engine}")
    apply_speed(out, str(cfg.get("speed", "1.0")))


def duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10)
        return round(float(out.stdout.strip()))
    except Exception:
        return None


def _variant_tag(cfg):
    """Engine+voice+speed discriminator so different renderings of the same turn
    cache to different files (otherwise switching engine/voice served stale audio)."""
    engine = cfg.get("engine", "openai")
    voice = {
        "openai": cfg.get("voice"),
        "deepgram": cfg.get("deepgram_voice"),
        "elevenlabs": cfg.get("elevenlabs_voice"),
        "unreal": cfg.get("unreal_voice"),
    }.get(engine) or ""
    return re.sub(r"[^A-Za-z0-9_.-]", "", f"{engine}-{voice}-s{cfg.get('speed', '1.0')}")


def audio_path_for(turn_id, cfg=None):
    cfg = cfg or load_config()
    safe = re.sub(r"[^A-Za-z0-9_-]", "", turn_id)[:120] or "turn"
    return os.path.join(AUDIO_CACHE, f"{safe}.{_variant_tag(cfg)}.mp3")


def synth_turn(turn_id, text, cfg=None):
    """Synthesize (and cache) a turn's audio. Returns (audio_path, duration_s).
    Reuses the cached mp3 if it already exists."""
    cfg = cfg or load_config()
    os.makedirs(AUDIO_CACHE, exist_ok=True)
    out = audio_path_for(turn_id, cfg)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out, duration(out)
    synth(text, cfg, out)
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        raise RuntimeError("no audio produced")
    return out, duration(out)
