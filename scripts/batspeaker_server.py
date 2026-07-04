#!/usr/bin/env python3
"""Bat-Speaker v2 — transcript-driven web reader/listener.

A small stdlib HTTP server that renders Claude Code's own per-session transcripts
as a tabbed, per-conversation reading surface, with on-demand and per-session
auto TTS. Run on demand (or under systemd):

    batspeaker serve [--port 8765]

v2 reverses v1's source of truth. v1 had the Stop hook write a single rolling
markdown note that N racing background workers fought over (the lost-turns bug);
this server reads the transcript JSONL files directly. No hook, no shared note,
no race. Audio is generated lazily per turn — click "speak" on any turn, or flip
a conversation into listen mode and new turns are voiced as they land.

Endpoints:
    GET  /                       the page
    GET  /sessions               JSON: list of conversations (tabs)
    GET  /session?id=&n=         JSON: last n turns of one conversation
    POST /tts   {session,turn}   synthesize one turn -> {url,duration}
    GET  /audio/<id>.mp3         cached turn audio (HTTP Range for iOS)
    GET  /events?session=        SSE: new turns for one conversation (+audio in listen mode)
    GET  /config / POST /config  voice/engine/speed; per-session listen toggles
    GET  /silence.mp3            silent prime that unlocks iOS autoplay
    POST /restart                restart the systemd unit (pick up code edits)
"""
import os, re, json, time, socket, argparse, tempfile, subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import batspeaker_core as core

SERVICE = "batspeaker-serve"
DEFAULT_N = 12                       # turns rendered per conversation
LISTEN_FILE = os.path.join(core.HOME, ".cache", "batspeaker", "listen.json")

# Voice settings are the only editable config keys; everything else in
# config.json (if present) is left untouched.
EDITABLE = {"engine", "voice", "model", "deepgram_voice",
            "elevenlabs_voice", "elevenlabs_model", "unreal_voice", "speed"}
ENGINES = {"openai", "deepgram", "elevenlabs", "unreal"}
OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable",
                 "nova", "onyx", "sage", "shimmer"]
UNREAL_VOICES = core.UNREAL_VOICES   # v7 + v8 rosters, routed per-voice in core

# Read-along player assets (shared ES modules copied from the reader; step 3 will
# formalize these into one nullsix package). Resolved beside this script — stable
# within the repo, unlike the vault-root __file__ idiom that broke at the cutover.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batspeaker_web")

ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------- config ----------

def update_cfg(updates):
    clean = {}
    for k, v in (updates or {}).items():
        if k not in EDITABLE:
            continue
        if k == "engine" and v not in ENGINES:
            continue
        if k == "speed":
            v = str(v)
        clean[k] = v
    cfg = {}
    try:
        with open(core.CONFIG) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.update(clean)
    os.makedirs(os.path.dirname(core.CONFIG), exist_ok=True)
    with open(core.CONFIG, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return clean


# ---------- per-session listen mode ----------

_listen_lock = threading.Lock()
_listen = set()


def _load_listen():
    global _listen
    try:
        with open(LISTEN_FILE) as f:
            _listen = set(json.load(f))
    except Exception:
        _listen = set()


def _save_listen():
    try:
        os.makedirs(os.path.dirname(LISTEN_FILE), exist_ok=True)
        with open(LISTEN_FILE, "w") as f:
            json.dump(sorted(_listen), f)
    except Exception:
        pass


def is_listening(session_id):
    with _listen_lock:
        return session_id in _listen


def set_listening(session_id, on):
    with _listen_lock:
        if on:
            _listen.add(session_id)
        else:
            _listen.discard(session_id)
        _save_listen()
        return session_id in _listen


# ---------- payload builders ----------

def turn_payload(turn, with_html=True):
    audio = core.audio_path_for(turn["id"])
    has_audio = os.path.exists(audio) and os.path.getsize(audio) > 0
    p = {
        "id": turn["id"],
        "ts": turn.get("ts"),
        "user": core.caption(turn.get("user", ""), 200),
        "has_audio": has_audio,
        "audio_url": f"/audio/{turn['id']}.mp3" if has_audio else None,
    }
    if with_html:
        p["html"] = core.md_to_html(core.turn_full_md(turn))
        # Spoken text (markdown stripped) drives the player's word-spans, so the
        # read-along highlight tracks the exact word order the TTS speaks.
        p["text"] = core.turn_spoken_text(turn)
    return p


def session_turns(session_id, n=DEFAULT_N):
    path = core.session_path(session_id)
    if not path:
        return None
    turns = core.parse_turns(path)
    return [turn_payload(t) for t in turns[-n:]]


# ---------- silent priming clip (iOS autoplay unlock) ----------

_SILENCE = None


def get_silence():
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


def restart_service():
    subprocess.Popen(
        ["bash", "-lc", f"sleep 1; systemctl --user restart {SERVICE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tailnet_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.strip().splitlines()
        return out[0] if out else None
    except Exception:
        return None


# ---------- HTML page ----------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Bat-Speaker</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --panel2:#1f232c; --fg:#e7e9ee;
          --mut:#8b93a3; --acc:#f2b53b; --acc2:#3b82f6; --line:#272c36; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html,body { margin:0; height:100%; }
  body { background:var(--bg); color:var(--fg); font:16px/1.55 -apple-system,
         BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:.5rem; padding:.55rem .7rem;
           background:var(--panel); border-bottom:1px solid var(--line);
           padding-top:max(.55rem, env(safe-area-inset-top)); }
  header .title { font-weight:700; color:var(--acc); letter-spacing:.2px; }
  header .sp { flex:1; }
  header .dot { font-size:.7rem; color:#888; }
  button { font:inherit; color:var(--fg); background:var(--panel2);
           border:1px solid var(--line); border-radius:.5rem; padding:.35rem .6rem;
           cursor:pointer; }
  button:active { transform:translateY(1px); }
  .icon { padding:.35rem .5rem; }
  #tabs { display:flex; gap:.4rem; overflow-x:auto; padding:.5rem .6rem;
          background:var(--panel); border-bottom:1px solid var(--line);
          scrollbar-width:none; }
  #tabs::-webkit-scrollbar { display:none; }
  .tab { flex:0 0 auto; max-width:62vw; padding:.4rem .6rem; border-radius:.6rem;
         background:var(--panel2); border:1px solid var(--line); color:var(--fg);
         white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:.9rem; }
  .tab.active { border-color:var(--acc); color:var(--acc); }
  .tab .cnt { color:var(--mut); font-size:.78rem; margin-left:.35rem; }
  .tab .ear { margin-left:.3rem; }
  #thread { flex:1; overflow-y:auto;
            padding:.7rem .7rem max(.9rem, env(safe-area-inset-bottom));
            -webkit-overflow-scrolling:touch; }
  .turn { background:var(--panel); border:1px solid var(--line);
          border-radius:.7rem; padding:.7rem .8rem; margin:0 auto .8rem; max-width:760px; }
  .turn .q { color:var(--mut); font-size:.86rem; border-left:2px solid var(--line);
             padding-left:.55rem; margin-bottom:.55rem; white-space:pre-wrap; }
  .turn .body p:first-child { margin-top:0; }
  .turn .body p:last-child { margin-bottom:0; }
  .turn .body pre { background:#0b0d11; border:1px solid var(--line);
                    border-radius:.4rem; padding:.6rem; overflow-x:auto; font-size:.85rem; }
  .turn .body code { background:#0b0d11; padding:.05rem .3rem; border-radius:.3rem; font-size:.9em; }
  .turn .body blockquote { color:var(--mut); border-left:3px solid var(--line);
                           margin:.5rem 0; padding-left:.7rem; }
  .turn .body h1,.turn .body h2,.turn .body h3 { font-size:1.05rem; margin:.7rem 0 .35rem; }
  .row { display:flex; align-items:center; gap:.6rem; margin-top:.6rem; }
  .row audio { flex:1; height:34px; }
  .speak { font-size:.85rem; }
  .speak.busy { opacity:.5; }
  .sub { color:var(--mut); font-size:.78rem; }
  .switch { display:inline-flex; align-items:center; gap:.5rem; font-size:.95rem; }
  .switch input { width:38px; height:22px; }
  #panel .sect { color:var(--mut); font-size:.78rem; text-transform:uppercase;
                 letter-spacing:.4px; margin:1rem 0 .35rem; }
  #panel .sect:first-of-type { margin-top:.4rem; }
  #panel { position:fixed; inset:0; background:rgba(0,0,0,.5); display:none;
           align-items:flex-end; z-index:20; }
  #panel.open { display:flex; }
  #panel .card { background:var(--panel); width:100%; max-width:760px; margin:0 auto;
                 border-radius:.8rem .8rem 0 0; padding:1rem; border:1px solid var(--line); }
  #panel label { display:block; margin:.6rem 0 .2rem; color:var(--mut); font-size:.85rem; }
  #panel select, #panel input[type=number] { width:100%; padding:.45rem; background:var(--panel2);
       color:var(--fg); border:1px solid var(--line); border-radius:.45rem; }
  .empty { color:var(--mut); text-align:center; margin-top:3rem; }
  .toast { position:fixed; left:50%; bottom:2.5rem; transform:translateX(-50%);
           background:#2a2f3a; border:1px solid var(--line); padding:.5rem .8rem;
           border-radius:.5rem; opacity:0; transition:opacity .2s; pointer-events:none; }
  .toast.show { opacity:1; }

  /* read-along player (shared ReadAlong module) */
  .player { margin-top:.2rem; }
  .ra-pane { font-size:1rem; line-height:1.9; }
  .ra-pane .w { padding:0 1px; border-radius:3px; cursor:pointer; }
  .ra-pane .w.cur { background:var(--acc); color:#14161a; }
  .ra-controls { position:sticky; bottom:0; background:var(--panel);
                 padding-top:.4rem; margin-top:.5rem; border-top:1px solid var(--line); }
  .ra-controls .row { display:flex; align-items:center; gap:.4rem; margin:.4rem 0; flex-wrap:wrap; }
  .ra-controls button { padding:.3rem .5rem; font-size:.85rem; }
  .ra-controls button.icon { min-width:38px; font-variant-numeric:tabular-nums; }
  .ra-controls button.round { width:40px; height:40px; border-radius:50%; font-size:1rem; padding:0; }
  .ra-controls button.primary { background:var(--acc2); border-color:var(--acc2); color:#fff; }
  .ra-controls .transport { justify-content:center; }
  .ra-controls .scrub { width:100%; accent-color:var(--acc2); margin:.2rem 0; }
  .ra-controls .time { color:var(--mut); font-size:.78rem; font-variant-numeric:tabular-nums; }
  .ra-controls .status { color:var(--mut); font-size:.78rem; min-height:1.2em; }
  .ra-controls .speeds { flex-wrap:nowrap; overflow-x:auto; gap:.3rem; }
  .ra-controls .speeds button { flex:0 0 auto; padding:.25rem .5rem; font-size:.78rem; }
  .ra-controls .speeds button.active { background:var(--acc2); border-color:var(--acc2); color:#fff; }
  .ra-controls .spacer { flex:1; }
</style>
<script type="module" src="/web/batspeaker-player.js"></script>
</head>
<body>
<header>
  <span class="title">🔊 Bat-Speaker</span>
  <span class="sub" id="now"></span>
  <span class="sp"></span>
  <span class="dot" id="liveDot" title="Live connection">●</span>
  <button class="icon" id="menuBtn" title="Menu">☰</button>
</header>
<div id="tabs"></div>
<div id="thread"><div class="empty">Pick a conversation above.</div></div>

<div id="panel"><div class="card">
  <div class="sect">Audio</div>
  <button id="startBtn" title="Unlock audio">▶︎ Listen (unlock audio)</button>
  <label class="switch" style="margin-top:.7rem;">
    <input type="checkbox" id="listenToggle">
    <span>Auto-speak this conversation</span></label>

  <div class="sect">Voice</div>
  <label>Engine</label>
  <select id="cfgEngine">
    <option value="openai">OpenAI</option>
    <option value="deepgram">Deepgram</option>
    <option value="elevenlabs">ElevenLabs</option>
    <option value="unreal">Unreal Speech</option>
  </select>
  <label>OpenAI voice</label>
  <select id="cfgVoice"></select>
  <label>Unreal voice</label>
  <select id="cfgUnrealVoice"></select>
  <p class="sub" style="margin:.5rem 0 0;">Voice changes apply to the next turn you synthesize. Speed lives on each turn's player now (it remembers your last choice).</p>

  <div class="sect">Actions</div>
  <div class="row">
    <button id="reloadBtn">↻ Refresh tabs</button>
    <button id="cfgRestart">Restart server</button>
  </div>

  <div class="row" style="margin-top:1.2rem;">
    <button id="cfgSave">Save</button>
    <span class="grow"></span>
    <button id="panelClose">Close</button>
  </div>
</div></div>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let sessions = [], active = null, es = null;
let audioUnlocked = false, queue = [], playing = false;
const seen = new Set();
const unlockEl = new Audio();

function toast(msg){ const t=$("#toast"); t.textContent=msg; t.classList.add("show");
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove("show"),1800); }

function fmtTime(ts){ if(!ts) return ""; const d=new Date(ts);
  return isNaN(d)?"":d.toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}); }

async function loadSessions(){
  const r = await fetch("/sessions"); sessions = await r.json();
  renderTabs();
  if(!active && sessions.length){ selectSession(sessions[0].id); }
}

function renderTabs(){
  const el = $("#tabs"); el.innerHTML = "";
  sessions.forEach(s => {
    const b = document.createElement("button");
    b.className = "tab" + (s.id===active ? " active":"");
    b.innerHTML = `<span>${escapeHtml(s.title)}</span><span class="cnt">${s.turns}</span>` +
                  (s.listening ? `<span class="ear">🎧</span>`:"");
    b.onclick = () => selectSession(s.id);
    el.appendChild(b);
  });
}

async function selectSession(id){
  active = id;
  const s = sessions.find(x=>x.id===id);
  $("#now").textContent = s ? (s.cwd ? s.cwd.split("/").pop() : "") : "";
  $("#listenToggle").checked = !!(s && s.listening);
  renderTabs();
  seen.clear();
  const thread = $("#thread"); thread.innerHTML = `<div class="empty">Loading…</div>`;
  const r = await fetch(`/session?id=${id}&n=20`);
  if(!r.ok){ thread.innerHTML = `<div class="empty">Couldn't load.</div>`; return; }
  const turns = await r.json();
  thread.innerHTML = "";
  if(!turns.length){ thread.innerHTML = `<div class="empty">No turns yet.</div>`; }
  turns.forEach(t => { seen.add(t.id); thread.appendChild(renderTurn(t)); });
  thread.scrollTop = thread.scrollHeight;
  openStream(id);
}

function renderTurn(t){
  const card = document.createElement("div");
  card.className = "turn"; card.dataset.id = t.id;
  card._text = t.text || "";
  // Speak + timestamp ride at the TOP of the card, where reading begins, so you
  // don't scroll to the floor to start a turn. The player mounts directly under
  // them (above the static body, which hides once the read-along takes over).
  card.innerHTML =
    (t.user ? `<div class="q">${escapeHtml(t.user)}</div>`:"") +
    `<div class="row">
       <button class="speak">🔊 Speak</button>
       <span class="sub">${fmtTime(t.ts)}</span>
       <span class="grow"></span>
     </div>` +
    `<div class="player"></div>` +
    `<div class="body">${t.html||""}</div>`;
  card.querySelector(".speak").onclick = () => mountPlayer(card);
  return card;
}

// Mount the shared ReadAlong player on a turn card (once) and start it. Hides
// the static body + Speak button; the player's pane becomes the read-along view.
function mountPlayer(card, {onDone=null}={}){
  if(!active || card._player) return card._player;
  const text = card._text || "";
  if(text.trim().length < 2){ toast("nothing to speak"); return; }
  if(!window.mountReadAlong){ toast("player still loading…"); return; }
  const body = card.querySelector(".body"); if(body) body.style.display="none";
  const sp = card.querySelector(".speak"); if(sp) sp.style.display="none";
  card._player = window.mountReadAlong(card.querySelector(".player"),
    {session:active, turnId:card.dataset.id, text, onDone});
  return card._player;
}

function openStream(id){
  if(es){ es.close(); es=null; }
  es = new EventSource(`/events?session=${id}`);
  es.onmessage = ev => {
    let t; try{ t = JSON.parse(ev.data); }catch{ return; }
    if(active!==id) return;
    if(seen.has(t.id)){
      // turn grew: refresh its text + body in place, but don't yank the pane
      // out from under a card whose player already took over.
      const card = $(`.turn[data-id="${cssEsc(t.id)}"]`);
      if(card){
        card._text = t.text || card._text;
        const body = card.querySelector(".body");
        if(body && !card._player) body.innerHTML = t.html||"";
      }
      return;
    }
    seen.add(t.id);
    const card = renderTurn(t);
    const thread = $("#thread");
    const atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
    thread.appendChild(card);
    if(atBottom) thread.scrollTop = thread.scrollHeight;
    if(t.audio_url){ enqueue(card); }   // listen mode: server pre-synthed it
  };
  es.onerror = () => { $("#liveDot").style.color="#888"; };
  es.onopen = () => { $("#liveDot").style.color="var(--acc)"; };
}

function enqueue(card){
  if(!audioUnlocked) return;          // listen mode but audio not unlocked yet
  queue.push(card); pump();
}
function pump(){
  if(playing || !queue.length) return;
  const card = queue.shift(); playing = true;
  // Mount + play this turn; chain to the next when its player reports "done".
  mountPlayer(card, {onDone:()=>{ playing=false; pump(); }});
}

// ---- iOS autoplay unlock ----
$("#startBtn").onclick = async () => {
  try{ unlockEl.src="/silence.mp3"; await unlockEl.play(); audioUnlocked=true;
       $("#startBtn").textContent="🔈 On"; toast("Audio unlocked"); }
  catch(e){ toast("Tap again to unlock audio"); }
};

// ---- listen toggle ----
$("#listenToggle").onchange = async e => {
  if(!active) return;
  const on = e.target.checked;
  await fetch("/config", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({listen:{session:active, on}})});
  const s = sessions.find(x=>x.id===active); if(s) s.listening = on;
  renderTabs();
  if(on && !audioUnlocked) toast("Tap ▶︎ Listen to allow audio");
};

// ---- settings ----
async function loadConfig(){
  const r = await fetch("/config"); const c = await r.json();
  const vsel = $("#cfgVoice"); vsel.innerHTML="";
  (c.voices||[]).forEach(v=>{ const o=document.createElement("option"); o.value=v; o.textContent=v;
    if(v===c.voice) o.selected=true; vsel.appendChild(o); });
  const usel = $("#cfgUnrealVoice"); usel.innerHTML="";
  (c.unreal_voices||[]).forEach(v=>{ const o=document.createElement("option"); o.value=v; o.textContent=v;
    if(v===c.unreal_voice) o.selected=true; usel.appendChild(o); });
  $("#cfgEngine").value = c.engine||"openai";
}
$("#menuBtn").onclick = ()=>{ loadConfig(); $("#panel").classList.add("open"); };
$("#panelClose").onclick = ()=> $("#panel").classList.remove("open");
$("#cfgSave").onclick = async ()=>{
  await fetch("/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body: JSON.stringify({engine:$("#cfgEngine").value, voice:$("#cfgVoice").value,
                          unreal_voice:$("#cfgUnrealVoice").value})});
  toast("Saved"); $("#panel").classList.remove("open");
};
$("#cfgRestart").onclick = async ()=>{ toast("Restarting…");
  await fetch("/restart",{method:"POST"}); setTimeout(()=>location.reload(),4000); };
$("#reloadBtn").onclick = ()=>{ loadSessions(); $("#panel").classList.remove("open"); };

function escapeHtml(s){ return (s||"").replace(/[&<>"]/g, c=>(
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function cssEsc(s){ return (window.CSS&&CSS.escape)?CSS.escape(s):s.replace(/"/g,'\\"'); }

loadSessions();
setInterval(loadSessions, 12000);    // pick up new conversations / reorder
</script>
</body>
</html>
"""


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
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

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), ctype="application/json")

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return None

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, PAGE)
        if path == "/sessions":
            return self._json(200, self._sessions_payload())
        if path == "/session":
            q = self._query()
            sid = (q.get("id") or [""])[0]
            try:
                n = int((q.get("n") or [DEFAULT_N])[0])
            except ValueError:
                n = DEFAULT_N
            turns = session_turns(sid, max(1, min(n, 100)))
            if turns is None:
                return self._json(404, {"error": "unknown session"})
            return self._json(200, turns)
        if path == "/config":
            cfg = core.load_config()
            return self._json(200, {"engine": cfg.get("engine"), "voice": cfg.get("voice"),
                                    "speed": cfg.get("speed"), "voices": OPENAI_VOICES,
                                    "unreal_voice": cfg.get("unreal_voice"),
                                    "unreal_voices": UNREAL_VOICES})
        if path == "/silence.mp3":
            return self._send(200, get_silence(), ctype="audio/mpeg",
                              extra={"Cache-Control": "max-age=3600"})
        if path == "/events":
            return self.stream_events()
        if path.startswith("/audio/"):
            return self.serve_audio(path[len("/audio/"):])
        if path.startswith("/web/"):
            return self.serve_web(path[len("/web/"):])
        return self._send(404, "not found", ctype="text/plain")

    def serve_web(self, name):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.js", name or ""):
            return self._send(404, "not found", ctype="text/plain")
        fp = os.path.abspath(os.path.join(WEB_DIR, name))
        if not (fp.startswith(WEB_DIR + os.sep) and os.path.isfile(fp)):
            return self._send(404, "not found", ctype="text/plain")
        with open(fp, "rb") as f:
            data = f.read()
        return self._send(200, data, ctype="application/javascript; charset=utf-8",
                          extra={"Cache-Control": "no-cache"})

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/tts":
            body = self._body()
            if not body:
                return self._json(400, {"error": "bad json"})
            return self._tts(body.get("session", ""), body.get("turn", ""))
        if path == "/config":
            body = self._body()
            if body is None:
                return self._json(400, {"error": "bad json"})
            if "listen" in body and isinstance(body["listen"], dict):
                sid = body["listen"].get("session", "")
                if ID_RE.match(sid):
                    set_listening(sid, bool(body["listen"].get("on")))
            update_cfg({k: v for k, v in body.items() if k in EDITABLE})
            cfg = core.load_config()
            return self._json(200, {"engine": cfg.get("engine"), "voice": cfg.get("voice"),
                                    "speed": cfg.get("speed")})
        if path == "/restart":
            self._json(200, {"restarting": True})
            restart_service()
            return
        return self._send(404, "not found", ctype="text/plain")

    # ---- helpers ----
    def _sessions_payload(self):
        out = []
        for s in core.list_sessions():
            out.append({"id": s["id"], "title": s["title"], "cwd": s["cwd"],
                        "branch": s["branch"], "turns": s["turns"],
                        "last_ts": s["last_ts"], "listening": is_listening(s["id"])})
        return out

    def _tts(self, session_id, turn_id):
        if not (ID_RE.match(session_id or "") and turn_id):
            return self._json(400, {"error": "bad request"})
        path = core.session_path(session_id)
        if not path:
            return self._json(404, {"error": "unknown session"})
        turn = next((t for t in core.parse_turns(path) if t["id"] == turn_id), None)
        if not turn:
            return self._json(404, {"error": "unknown turn"})
        text = core.turn_spoken_text(turn)
        if len(text.strip()) < 4:
            return self._json(422, {"error": "nothing to speak"})
        try:
            _, dur = core.synth_turn(turn_id, text)
        except Exception as ex:
            return self._json(500, {"error": f"tts failed: {repr(ex)[:160]}"})
        resp = {"url": f"/audio/{turn_id}.mp3", "duration": dur}
        words = core.load_words(turn_id)   # unreal per-word timestamps, if any
        if words:
            resp["words"] = words
        return self._json(200, resp)

    # ---- SSE: new turns for one conversation ----
    def stream_events(self):
        q = self._query()
        sid = (q.get("session") or [""])[0]
        path = core.session_path(sid) if ID_RE.match(sid or "") else None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Flush Safari's stream buffer (it withholds events until ~2KB arrives).
        self.wfile.write(b": " + (b" " * 2048) + b"\n\n")
        self.wfile.write(b"retry: 3000\n\n")
        self.wfile.flush()
        if not path:
            return
        seen = {t["id"] for t in core.parse_turns(path)}    # ignore the backlog
        voiced = set()              # turns we've auto-synthesized in listen mode
        STABLE = 4.0                # secs a turn must sit unchanged before we voice it
        last_mtime = 0.0
        changed_at = 0.0
        last_beat = time.time()

        def emit(t):
            data = json.dumps(turn_payload(t))   # turn_payload reflects cached audio
            self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()

        try:
            while True:
                now = time.time()
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = last_mtime
                turns = None
                if mtime != last_mtime:
                    last_mtime = mtime
                    changed_at = now
                    turns = core.parse_turns(path)
                    for i, t in enumerate(turns):
                        is_live = (i == len(turns) - 1)
                        if t["id"] in seen and not is_live:
                            continue       # settled turn already sent
                        seen.add(t["id"])
                        emit(t)   # text first, always
                # Listen mode: voice a turn once it's sat unchanged for STABLE secs,
                # so we never synthesize a half-written reply.
                if is_listening(sid) and changed_at and (now - changed_at) > STABLE:
                    if turns is None:
                        turns = core.parse_turns(path)
                    for t in turns:
                        if t["id"] in voiced:
                            continue
                        voiced.add(t["id"])
                        try:
                            text = core.turn_spoken_text(t)
                            if len(text.strip()) >= 4:
                                core.synth_turn(t["id"], text)
                                emit(t)   # now carries audio_url
                        except Exception:
                            pass
                if now - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = now
                time.sleep(1.5)
        except (BrokenPipeError, ConnectionResetError):
            return

    # ---- audio with Range (iOS Safari needs 206) ----
    def serve_audio(self, name):
        name = os.path.basename(name)
        m = re.match(r"^([A-Za-z0-9_-]+)\.mp3$", name)
        if not m:
            return self._send(404, "not found", ctype="text/plain")
        fpath = core.audio_path_for(m.group(1))
        if not os.path.isfile(fpath):
            return self._send(404, "not found", ctype="text/plain")
        size = os.path.getsize(fpath)
        rng = self.headers.get("Range")
        start, end, partial = 0, size - 1, False
        if rng:
            mm = re.match(r"bytes=(\d*)-(\d*)", rng)
            if mm:
                if mm.group(1):
                    start = int(mm.group(1))
                if mm.group(2):
                    end = int(mm.group(2))
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


def _last_id(path):
    turns = core.parse_turns(path)
    return turns[-1]["id"] if turns else None


def main():
    ap = argparse.ArgumentParser(description="Bat-Speaker v2 server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    _load_listen()
    get_silence()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    host = socket.gethostname()
    ip = tailnet_ip()
    print("🔊 Bat-Speaker v2 (transcript-driven)")
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
