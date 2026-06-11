#!/usr/bin/env python3
"""Bat-Speaker live listen server (Rung 2).

A small stdlib HTTP server that auto-plays new Bat-Speaker clips in a mobile
browser over Tailscale — no Obsidian, no sync wait. Run on demand:

    batspeaker serve [--port 8765]

Decoupled by design: this is a pure *reader* of the artifacts the Stop hook
already produces (MP3s, the JSONL log, the Live note). It never writes them and
makes zero changes to batspeaker_hook.py, so if it breaks, Rung 1 is untouched.

The hook's worker writes in order: MP3 -> Live-note entry -> JSONL worker/success
record. We key off that success record (written last), so the MP3 and the note
entry always exist by the time we emit an event. No race.
"""
import sys, os, re, json, time, socket, argparse, tempfile, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VAULT = os.environ.get("BATSPEAKER_VAULT") or os.path.expanduser("~/vault")
MP3_DIR = os.path.join(VAULT, "3 Information", "Attachments", "batspeaker")
NOTE = os.path.join(VAULT, "0 Inbox", "Bat-Speaker Live.md")
LOG = os.path.expanduser("~/.local/state/batspeaker.jsonl")
TOGGLE = os.path.expanduser("~/.batspeaker-on")   # presence = TTS generation on
SERVICE = "batspeaker-serve"                        # systemd --user unit name

MP3_RE = re.compile(r"^bruce-[0-9A-Za-z\-]+\.mp3$")   # filename whitelist
HISTORY = 15                                          # clips shown in the list

# Config is shared with the Stop hook (it reads this file fresh each turn via
# load_config()), so a change made from the page is picked up on the NEXT turn.
CONFIG = os.path.expanduser("~/.config/batspeaker/config.json")
# Mirror the hook's defaults for the keys the panel surfaces, so the UI shows
# real current values even when config.json doesn't set them yet.
CFG_DEFAULTS = {
    "engine": "openai", "voice": "ash", "model": "tts-1-hd",
    "deepgram_voice": "orpheus",
    "elevenlabs_voice": "nPczCjzI2devNBz1zQrb",
    "elevenlabs_model": "eleven_multilingual_v2",
    "speed": "1.0",
    "notify": True, "append_when_off": True, "keep": 15, "min_chars": 12,
}
EDITABLE = {"engine", "voice", "model", "deepgram_voice",
            "elevenlabs_voice", "elevenlabs_model", "speed",
            "notify", "append_when_off", "keep", "min_chars"}
BOOL_KEYS = {"notify", "append_when_off"}
INT_KEYS = {"keep": (1, 100), "min_chars": (0, 10000)}   # key -> (lo, hi) clamp
ENGINES = {"openai", "deepgram", "elevenlabs"}


# ---------- artifact readers ----------

def iter_success_records():
    """Yield every worker/success record in the JSONL log, in order."""
    if not os.path.exists(LOG):
        return
    with open(LOG) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if (r.get("event") == "worker" and r.get("status") == "success"
                    and (r.get("mp3") or r.get("id"))):
                yield r


def record_id(r):
    """Stable id for a clip record. Newer records carry an explicit `id`;
    legacy audio records are identified by their MP3 filename stem."""
    rid = r.get("id")
    if rid:
        return rid
    mp3 = r.get("mp3")
    return mp3[:-4] if mp3 else None


def spoken_text(mp3):
    """Full spoken text for a clip (paragraphs preserved as \\n\\n), read from
    the Live note's blockquote. Best-effort: '' if the entry isn't found."""
    try:
        with open(NOTE) as f:
            content = f.read()
    except Exception:
        return ""
    marker = f"![[batspeaker/{mp3}]]"
    idx = content.find(marker)
    if idx == -1:
        return ""
    after = content[idx + len(marker):]
    paras, cur = [], []
    for ln in after.splitlines():
        s = ln.strip()
        if s.startswith(">"):
            body = s.lstrip(">").strip()
            if body:
                cur.append(body)
            elif cur:                      # blank quote line -> paragraph break
                paras.append(" ".join(cur)); cur = []
        elif cur or paras:
            break                          # blockquote ended
        # leading blank lines before the quote starts are skipped
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras)


def clip_payload(rec):
    mp3 = rec.get("mp3")
    # Newer records log the read-along text directly (works for text-only
    # entries and survives note pruning); fall back to the note for legacy ones.
    text = rec.get("text")
    if text is None:
        text = spoken_text(mp3) if mp3 else ""
    one = " ".join(text.split())
    caption = (one[:160].rstrip() + "…") if len(one) > 160 else one
    return {
        "id": record_id(rec),
        "mp3": mp3,
        "url": f"/clips/{mp3}" if mp3 else None,   # text-only clips have no player
        "duration": rec.get("duration"),
        "ts": rec.get("ts"),
        "caption": caption,   # short label for the history list
        "text": text,         # full read-along text
    }


def recent_clips(n=HISTORY):
    recs = list(iter_success_records())
    return [clip_payload(r) for r in reversed(recs[-n:])]   # newest first


# ---------- config (shared with the Stop hook) ----------

def as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)


def tts_enabled():
    """TTS generation is gated by the presence of the ~/.batspeaker-on file —
    the same toggle the `batspeaker on|off` CLI flips and the hook reads."""
    return os.path.exists(TOGGLE)


def set_tts(on):
    """Create/remove the toggle file. The hook reads it fresh each turn, so the
    change takes effect on the next turn Bruce speaks — no restart needed."""
    if on:
        open(TOGGLE, "a").close()
    else:
        try:
            os.remove(TOGGLE)
        except FileNotFoundError:
            pass
    return tts_enabled()


def restart_service():
    """Bounce the systemd --user unit so a fresh process picks up code changes.
    Detached + delayed so this request's response flushes before we're killed;
    systemd brings the new process up and the page's SSE auto-reconnects."""
    subprocess.Popen(
        ["sh", "-c", f"sleep 1; systemctl --user restart {SERVICE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def effective_cfg():
    """CFG_DEFAULTS overlaid with whatever's in the config file."""
    cfg = dict(CFG_DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def update_cfg(updates):
    """Merge validated, editable-only keys into the config file. Returns the
    cleaned subset that was applied. Never touches non-editable keys (keep,
    notify, title, etc.) the hook also stores there."""
    clean = {}
    for k, v in (updates or {}).items():
        if k not in EDITABLE:
            continue
        if k == "engine" and v not in ENGINES:
            continue
        if k == "speed":
            v = str(v)
        elif k in BOOL_KEYS:
            v = as_bool(v)
        elif k in INT_KEYS:
            try:
                v = int(v)
            except (ValueError, TypeError):
                continue
            lo, hi = INT_KEYS[k]
            v = max(lo, min(hi, v))
        clean[k] = v
    cfg = {}
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.update(clean)
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return clean


# ---------- silent priming clip (iOS autoplay unlock) ----------

_SILENCE = None


def get_silence():
    """A short, *valid* silent MP3 used to unlock the <audio> element inside the
    Start-tap gesture. iOS only honours a later programmatic play() if a real
    play() succeeded during a user gesture — an empty data-URI doesn't count."""
    global _SILENCE
    if _SILENCE is not None:
        return _SILENCE
    path = os.path.join(tempfile.gettempdir(), "batspeaker-silence.mp3")
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i",
                 "anullsrc=r=44100:cl=mono", "-t", "0.4", "-q:a", "9", path],
                capture_output=True, timeout=15)
        with open(path, "rb") as f:
            _SILENCE = f.read()
    except Exception:
        _SILENCE = b""
    return _SILENCE


# ---------- HTML page ----------

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Bat-Speaker</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 16px/1.5 -apple-system, system-ui, sans-serif;
    background: #0d0f12; color: #e7e9ee;
    padding: env(safe-area-inset-top) 16px env(safe-area-inset-bottom);
  }
  header { padding: 20px 0 12px; }
  h1 { font-size: 1.25rem; margin: 0; letter-spacing: .02em; }
  .sub { color: #8b93a3; font-size: .85rem; margin-top: 2px; }
  button.cta {
    display: block; width: 100%; padding: 18px; margin: 16px 0;
    font-size: 1.1rem; font-weight: 600; color: #0d0f12; background: #f5b945;
    border: 0; border-radius: 14px; cursor: pointer;
  }
  button.cta:active { transform: scale(.99); }
  .status { display: flex; align-items: center; gap: 8px; color: #8b93a3;
    font-size: .85rem; margin: 4px 0 12px; }
  /* config panel */
  #cfgBar { margin-bottom: 16px; }
  button.ghost { width: 100%; text-align: left; background: #15191f;
    color: #cfd4dd; border: 1px solid #232a33; border-radius: 12px;
    padding: 12px 14px; font-size: .9rem; cursor: pointer; }
  button.ghost #cfgSummary { color: #f5b945; }
  #cfgPanel { background: #15191f; border: 1px solid #232a33; border-top: 0;
    border-radius: 0 0 12px 12px; margin-top: -8px; padding: 14px;
    display: flex; flex-direction: column; gap: 12px; }
  /* ID+class beats the #cfgPanel ID selector above, so .hidden can win */
  #cfgPanel.hidden { display: none; }
  #cfgToggle .chev { display: inline-block; transition: transform .15s ease;
    color: #8b93a3; margin-right: 4px; }
  #cfgToggle.open .chev { transform: rotate(90deg); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; }
  button.chip { flex: 1; min-width: 88px; padding: 10px; border-radius: 10px;
    border: 1px solid #2a323d; background: #1b212a; color: #cfd4dd;
    font-size: .9rem; cursor: pointer; }
  button.chip.active { background: #f5b945; color: #0d0f12; border-color: #f5b945;
    font-weight: 600; }
  .lbl { display: flex; justify-content: space-between; align-items: center;
    color: #8b93a3; font-size: .85rem; gap: 12px; }
  .lbl select { background: #1b212a; color: #e7e9ee; border: 1px solid #2a323d;
    border-radius: 8px; padding: 8px 10px; font-size: .9rem; min-width: 130px; }
  .note { color: #6b7280; font-size: .78rem; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #555; flex: none; }
  .dot.live { background: #46d27e; box-shadow: 0 0 8px #46d27e; }
  .dot.playing { background: #f5b945; box-shadow: 0 0 8px #f5b945; }
  #now { background: #15191f; border: 1px solid #232a33; border-radius: 14px;
    padding: 14px; margin-bottom: 18px; }
  /* Player stays pinned to the top of the viewport while you scroll the
     read-along text, so it never leaves the screen and gets auto-paused. */
  #nowHead { position: sticky; top: 0; z-index: 2; background: #15191f;
    padding-bottom: 6px; }
  #now .meta { font-size: .8rem; color: #8b93a3; margin-bottom: 6px; }
  #nowText { white-space: pre-wrap; color: #cfd4dd; margin-top: 12px;
    font-size: .98rem; line-height: 1.55; }
  audio { width: 100%; margin-top: 10px; }
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
    color: #6b7280; margin: 18px 0 8px; }
  .clip { padding: 12px 0; border-top: 1px solid #1c222a; }
  .clip .meta { font-size: .78rem; color: #7c8493; margin-bottom: 4px; }
  .clip .ctext { white-space: pre-wrap; color: #b8bdc8; font-size: .95rem;
    line-height: 1.55; margin-top: 8px; }
  /* big TTS generation switch (the ~/.batspeaker-on toggle) */
  #ttsToggle { display: flex; align-items: center; justify-content: space-between;
    width: 100%; padding: 14px 16px; margin-bottom: 12px; cursor: pointer;
    border-radius: 12px; border: 1px solid #232a33; background: #15191f;
    color: #cfd4dd; font-size: .95rem; font-weight: 600; }
  #ttsToggle .pill { font-size: .8rem; font-weight: 700; padding: 4px 12px;
    border-radius: 999px; background: #2a323d; color: #8b93a3; letter-spacing: .04em; }
  #ttsToggle.on { border-color: #46d27e; }
  #ttsToggle.on .pill { background: #46d27e; color: #07130c; }
  .toggle { min-width: 64px; padding: 7px 12px; border-radius: 8px;
    border: 1px solid #2a323d; background: #1b212a; color: #8b93a3;
    font-size: .85rem; font-weight: 600; cursor: pointer; }
  .toggle.on { background: #46d27e; color: #07130c; border-color: #46d27e; }
  .lbl input[type=number] { background: #1b212a; color: #e7e9ee;
    border: 1px solid #2a323d; border-radius: 8px; padding: 8px 10px;
    font-size: .9rem; width: 90px; }
  #restartBtn { margin-top: 4px; }
  .hr { height: 1px; background: #232a33; margin: 4px 0; }
  .hidden { display: none; }
</style>
</head>
<body>
<header>
  <h1>🔊 Bat-Speaker</h1>
  <div class="sub">Live listen — new clips auto-play as Bruce finishes a turn.</div>
</header>

<button id="start" class="cta">▶ Start Listening</button>

<div class="status"><span class="dot" id="dot"></span><span id="statusText">Tap to begin</span></div>

<button id="ttsToggle" title="Toggle whether Bruce's turns are spoken aloud">
  <span>🎙️ TTS generation</span><span class="pill" id="ttsState">…</span>
</button>

<div id="cfgBar">
  <button id="cfgToggle" class="ghost"><span class="chev">▸</span>⚙ Settings — <span id="cfgSummary">…</span></button>
  <div id="cfgPanel" class="hidden">
    <div class="row" id="engineRow"></div>
    <label class="lbl">Voice <select id="voiceSel"></select></label>
    <label class="lbl">Generation speed <select id="speedSel">
      <option value="0.75">0.75×</option>
      <option value="1.0">1.0×</option>
      <option value="1.25">1.25×</option>
      <option value="1.5">1.5×</option>
      <option value="2.0">2.0×</option>
    </select></label>
    <div class="hr"></div>
    <div class="lbl">Moshi push notifications <button id="notifyBtn" class="toggle"></button></div>
    <div class="lbl">Append text when TTS off <button id="appendBtn" class="toggle"></button></div>
    <label class="lbl">Clips kept <input id="keepInp" type="number" min="1" max="100"></label>
    <label class="lbl">Min characters <input id="minInp" type="number" min="0" max="10000"></label>
    <div class="hr"></div>
    <button id="restartBtn" class="ghost">↻ Restart server (after code changes)</button>
    <div id="cfgNote" class="note">Settings apply to the next turn Bruce speaks — no restart needed.</div>
  </div>
</div>

<div id="now" class="hidden">
  <div id="nowHead">
    <div class="meta" id="nowMeta"></div>
    <audio id="player" controls playsinline preload="auto"></audio>
    <button id="newClip" class="cta hidden">▶ Play new clip</button>
  </div>
  <div id="nowText"></div>
</div>

<h2>Recent</h2>
<div id="history"></div>

<script>
const player = document.getElementById('player');
const startBtn = document.getElementById('start');
const newClipBtn = document.getElementById('newClip');
const dot = document.getElementById('dot');
const statusText = document.getElementById('statusText');
const nowBox = document.getElementById('now');
const nowMeta = document.getElementById('nowMeta');
const nowText = document.getElementById('nowText');
const historyEl = document.getElementById('history');

let queue = [];
let playing = false;
let started = false;
let pending = null;
let currentNow = null;          // clip in the Now Playing box (not yet in the feed)
let currentHistory = [];

function fmtDur(d) { return d ? d + 's' : ''; }
function fmtTs(ts) { if (!ts) return ''; const t = (ts.split('T')[1] || ts); return t.slice(0, 5); }
function setStatus(cls, txt) { dot.className = 'dot ' + cls; statusText.textContent = txt; }

// Each clip renders as its own inline <audio controls> plus the FULL spoken
// text for read-along. Manual taps are user gestures, so the inline players
// never hit autoplay blocks.
function renderHistory(clips) {
  historyEl.innerHTML = '';
  for (const c of clips) {
    const div = document.createElement('div');
    div.className = 'clip';
    const metaDiv = document.createElement('div');
    metaDiv.className = 'meta';
    // text-only clips (toggle off) have no audio — label them, skip the player.
    const tag = c.url ? [] : ['📄 text'];
    metaDiv.textContent = [fmtTs(c.ts), fmtDur(c.duration), ...tag].filter(Boolean).join(' · ');
    div.appendChild(metaDiv);
    if (c.url) {
      const a = document.createElement('audio');
      a.controls = true; a.preload = 'none'; a.src = c.url;
      a.setAttribute('playsinline', '');
      div.appendChild(a);
    }
    const txt = document.createElement('div');
    txt.className = 'ctext';
    txt.textContent = c.text || c.caption || '';
    div.appendChild(txt);
    historyEl.appendChild(div);
  }
}

function playNext() {
  if (!queue.length) { playing = false; setStatus('live', 'Listening — waiting for the next turn'); return; }
  playing = true;
  const c = queue.shift();
  // Demote the previous live clip into the feed so it isn't shown twice.
  if (currentNow) {
    currentHistory = [currentNow, ...currentHistory];
    renderHistory(currentHistory.slice(0, 30));
  }
  currentNow = c;
  nowBox.classList.remove('hidden');
  nowMeta.textContent = [fmtTs(c.ts), fmtDur(c.duration)].filter(Boolean).join(' · ');
  nowText.textContent = c.text || c.caption || '';
  window.scrollTo(0, 0);
  newClipBtn.classList.add('hidden');
  player.src = c.url;
  setStatus('playing', 'Playing' + (queue.length ? ' (' + queue.length + ' queued)' : ''));
  player.play().catch(() => {
    // iOS still blocked it — surface a one-tap fallback (a real gesture).
    playing = false;
    pending = c;
    newClipBtn.classList.remove('hidden');
    setStatus('live', 'New clip ready — tap ▶');
  });
}
player.addEventListener('ended', playNext);

newClipBtn.addEventListener('click', () => {
  newClipBtn.classList.add('hidden');
  if (pending) { queue.unshift(pending); pending = null; }
  if (!playing) playNext();
});

function enqueue(clip) {
  // text-only clips (toggle off): nothing to play — prepend to the live feed.
  if (!clip.url) {
    currentHistory = [clip, ...currentHistory];
    renderHistory(currentHistory.slice(0, 30));
    if (!playing) setStatus('live', 'New text — read below');
    return;
  }
  queue.push(clip);
  if (!playing) playNext();
}

function connect() {
  const es = new EventSource('/events');
  es.onopen = () => { if (!playing && !queue.length) setStatus('live', 'Listening — waiting for the next turn'); };
  es.onmessage = (e) => { try { enqueue(JSON.parse(e.data)); } catch (_) {} };
  es.onerror = () => { setStatus('', 'Reconnecting…'); };  // EventSource auto-retries
}

startBtn.addEventListener('click', () => {
  if (started) return;
  started = true;
  // Unlock autoplay: play a real (silent) clip inside the user gesture.
  player.src = '/silence.mp3';
  player.play().catch(() => {});
  startBtn.classList.add('hidden');
  setStatus('live', 'Listening — waiting for the next turn');
  connect();
});

// ---- in-page config (presets = which engine; each remembers its own voice) ----
const cfgToggle = document.getElementById('cfgToggle');
const cfgPanel = document.getElementById('cfgPanel');
const cfgSummary = document.getElementById('cfgSummary');
const cfgNote = document.getElementById('cfgNote');
const engineRow = document.getElementById('engineRow');
const voiceSel = document.getElementById('voiceSel');
const speedSel = document.getElementById('speedSel');
const ttsToggle = document.getElementById('ttsToggle');
const ttsState = document.getElementById('ttsState');
const notifyBtn = document.getElementById('notifyBtn');
const appendBtn = document.getElementById('appendBtn');
const keepInp = document.getElementById('keepInp');
const minInp = document.getElementById('minInp');
const restartBtn = document.getElementById('restartBtn');

function setToggleBtn(btn, on) {
  btn.textContent = on ? 'On' : 'Off';
  btn.classList.toggle('on', !!on);
}

const ENGINE_LABEL = { openai: 'OpenAI', deepgram: 'Deepgram', elevenlabs: 'ElevenLabs' };
// For each engine: which config key holds its voice, and the [label, value] list.
const VOICES = {
  openai: { key: 'voice', opts: [['alloy','alloy'],['ash','ash'],['coral','coral'],['echo','echo'],['fable','fable'],['nova','nova'],['onyx','onyx'],['sage','sage'],['shimmer','shimmer']] },
  deepgram: { key: 'deepgram_voice', opts: [['orpheus','orpheus'],['asteria','asteria'],['luna','luna'],['stella','stella'],['athena','athena'],['hera','hera'],['orion','orion'],['arcas','arcas'],['perseus','perseus'],['angus','angus'],['helios','helios'],['zeus','zeus']] },
  elevenlabs: { key: 'elevenlabs_voice', opts: [['Brian','nPczCjzI2devNBz1zQrb']] }
};
let cfg = {};

function voiceLabel(engine, val) {
  const spec = VOICES[engine]; if (!spec) return val;
  const m = spec.opts.find(o => o[1] === val); return m ? m[0] : val;
}

function renderCfg() {
  const engine = VOICES[cfg.engine] ? cfg.engine : 'openai';
  engineRow.innerHTML = '';
  for (const e of ['openai', 'deepgram', 'elevenlabs']) {
    const b = document.createElement('button');
    b.className = 'chip' + (engine === e ? ' active' : '');
    b.textContent = ENGINE_LABEL[e];
    b.onclick = () => postCfg({ engine: e });
    engineRow.appendChild(b);
  }
  const spec = VOICES[engine];
  voiceSel.innerHTML = '';
  for (const [label, val] of spec.opts) {
    const o = document.createElement('option');
    o.value = val; o.textContent = label;
    voiceSel.appendChild(o);
  }
  voiceSel.value = cfg[spec.key] || spec.opts[0][1];
  speedSel.value = (cfg.speed && [...speedSel.options].some(o => o.value === cfg.speed)) ? cfg.speed : '1.0';
  ttsState.textContent = cfg.tts ? 'ON' : 'OFF';
  ttsToggle.classList.toggle('on', !!cfg.tts);
  setToggleBtn(notifyBtn, cfg.notify);
  setToggleBtn(appendBtn, cfg.append_when_off);
  if (document.activeElement !== keepInp) keepInp.value = cfg.keep;
  if (document.activeElement !== minInp) minInp.value = cfg.min_chars;
  cfgSummary.textContent = (cfg.tts ? 'TTS on' : 'TTS off') + ' · '
    + ENGINE_LABEL[engine] + ' · ' + voiceLabel(engine, voiceSel.value);
}

function postCfg(updates) {
  fetch('/config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates) })
    .then(r => r.json()).then(c => { cfg = c; renderCfg(); flashSaved(); })
    .catch(() => { cfgNote.textContent = 'Save failed — is the server up?'; });
}

function flashSaved() {
  cfgNote.textContent = 'Saved ✓ — applies to the next turn.';
  setTimeout(() => { cfgNote.textContent = 'Applies to the next turn Bruce speaks.'; }, 1800);
}

cfgToggle.onclick = () => {
  const open = !cfgPanel.classList.toggle('hidden');   // toggle returns true if now hidden
  cfgToggle.classList.toggle('open', open);            // rotate the chevron when open
};
voiceSel.onchange = () => { const spec = VOICES[cfg.engine] || VOICES.openai; postCfg({ [spec.key]: voiceSel.value }); };
speedSel.onchange = () => postCfg({ speed: speedSel.value });
ttsToggle.onclick = () => postCfg({ tts: !cfg.tts });
notifyBtn.onclick = () => postCfg({ notify: !cfg.notify });
appendBtn.onclick = () => postCfg({ append_when_off: !cfg.append_when_off });
keepInp.onchange = () => postCfg({ keep: parseInt(keepInp.value, 10) });
minInp.onchange = () => postCfg({ min_chars: parseInt(minInp.value, 10) });
restartBtn.onclick = () => {
  if (!confirm('Restart the Bat-Speaker server? The page will reconnect in a few seconds.')) return;
  cfgNote.textContent = 'Restarting server…';
  fetch('/restart', { method: 'POST' }).catch(() => {});
  setTimeout(() => location.reload(), 4000);
};

fetch('/config').then(r => r.json()).then(c => { cfg = c; renderCfg(); }).catch(() => {});

// History loads immediately (display + tap-to-replay; does not auto-play).
fetch('/state').then(r => r.json()).then(clips => {
  currentHistory = clips;
  renderHistory(clips);
}).catch(() => {});
</script>
</body>
</html>
"""


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):           # quiet; we print our own startup line
        pass

    def handle_one_request(self):
        # Mobile clients reset keep-alive / SSE connections constantly; that's
        # normal, not an error — swallow it instead of dumping a traceback.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, PAGE)
        if path == "/state":
            return self._send(200, json.dumps(recent_clips()),
                              ctype="application/json")
        if path == "/config":
            cfg = effective_cfg()
            cfg["tts"] = tts_enabled()   # virtual field: the ~/.batspeaker-on toggle
            return self._send(200, json.dumps(cfg), ctype="application/json")
        if path == "/silence.mp3":
            return self._send(200, get_silence(), ctype="audio/mpeg",
                              extra={"Cache-Control": "max-age=3600"})
        if path == "/events":
            return self.stream_events()
        if path.startswith("/clips/"):
            return self.serve_clip(path[len("/clips/"):])
        return self._send(404, "not found", ctype="text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/config":
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(n) if n else b"{}"
                updates = json.loads(body or b"{}")
            except Exception:
                return self._send(400, json.dumps({"error": "bad json"}),
                                  ctype="application/json")
            if "tts" in updates:                 # the on/off file, not config.json
                set_tts(as_bool(updates.pop("tts")))
            update_cfg(updates)
            cfg = effective_cfg()
            cfg["tts"] = tts_enabled()
            return self._send(200, json.dumps(cfg), ctype="application/json")
        if path == "/restart":
            self._send(200, json.dumps({"restarting": True}),
                       ctype="application/json")
            restart_service()                    # fires after the response flushes
            return
        return self._send(404, "not found", ctype="text/plain")

    # ---- SSE: stream only clips produced after this connection opened ----
    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Safari (and some mobile browsers) buffer an event-stream until ~2KB
        # has arrived before dispatching ANY events — the connection looks open
        # but nothing fires. A padding-comment prelude flushes that buffer.
        self.wfile.write(b": " + (b" " * 2048) + b"\n\n")
        self.wfile.write(b"retry: 3000\n\n")
        self.wfile.flush()
        seen = {record_id(r) for r in iter_success_records()}   # ignore backlog
        last_beat = time.time()
        try:
            while True:
                fresh = [r for r in iter_success_records()
                         if record_id(r) not in seen]
                for r in fresh:
                    seen.add(record_id(r))
                    data = json.dumps(clip_payload(r))
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                if time.time() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return

    # ---- audio with HTTP Range support (iOS Safari needs 206) ----
    def serve_clip(self, name):
        name = os.path.basename(name)
        if not MP3_RE.match(name):
            return self._send(404, "not found", ctype="text/plain")
        fpath = os.path.join(MP3_DIR, name)
        if not os.path.isfile(fpath):
            return self._send(404, "not found", ctype="text/plain")
        size = os.path.getsize(fpath)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(fpath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return


def tailnet_ip():
    """Best-effort 100.x Tailscale address of this host."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.strip().splitlines()
        return out[0] if out else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Bat-Speaker live listen server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    get_silence()   # warm the priming clip so the first Start tap is instant

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    host = socket.gethostname()
    ip = tailnet_ip()
    print("🔊 Bat-Speaker listen server")
    print(f"   local : http://localhost:{args.port}")
    print(f"   host  : http://{host}:{args.port}")
    if ip:
        print(f"   tailnet: http://{ip}:{args.port}")
    print("   (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
