// Bat-Speaker adoption of the shared ReadAlong player (2026-07-03, step 2;
// progressive chunked synthesis 2026-07-14, the planned fast-follow).
//
// Replaces the native <audio controls> per-turn play path with a real read-along
// player: big transport, scrub, click-to-seek, and client-side pitch-preserved
// speed (persisted). Exposes window.mountReadAlong for the page's inline script.
//
// Two source modes behind the same synthChunk seam:
//   whole   one /tts call → one /audio mp3 → one chunk. Used when the server
//           already has the turn cached (listen-mode pre-synth) — instant.
//   chunked the client splits the turn (chunker.js ramp 140→260→400) and
//           requests each piece from /tts; playback starts on chunk 1 while the
//           rest synthesizes. Word-timestamp engines (inworld, unreal) return
//           chunk-local timings per piece — sample-accurate highlight with no
//           cross-chunk offset math.

import { ReadAlong } from "./readalong.js";
import { silence } from "./audioutil.js";
import { chunkText } from "./chunker.js";
import { KokoroWorkerSource } from "./sources.js";

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function esc(s) {
  return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// Word-spans come from the SAME spoken text the server synthesizes
// (turn_spoken_text), so the highlight tracks the audio's word order.
function spanHTML(chunks) {
  return chunks.map((words, c) =>
    words.map((w, i) => `<span class="w" data-c="${c}" data-w="${i}">${esc(w)}</span>`).join(" ")
  ).join(" ");
}

// Formatted pane: the server renders the turn's markdown with one
// <span class="w" data-g=N> per spoken word, N indexing the spoken text's
// whitespace split (core.read_along). Chunking only regroups that split, so
// stamping each span with its chunk/word coordinates is all the shared player
// needs to highlight and seek inside real headings, lists and tables. Any
// count mismatch returns null and the caller falls back to flat spans.
function stampSpans(html, chunks) {
  if (!html) return null;
  const coords = [];
  chunks.forEach((words, c) => words.forEach((_, w) => coords.push([c, w])));
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const spans = tpl.content.querySelectorAll(".w[data-g]");
  if (spans.length !== coords.length) return null;
  for (const s of spans) {
    const at = coords[+s.dataset.g];
    if (!at) return null;
    s.dataset.c = at[0]; s.dataset.w = at[1];
    s.removeAttribute("data-g");
  }
  return tpl.innerHTML;
}

// whole-turn mode: a single chunk spanning the turn.
function prepareOne(text, html) {
  const words = (text || "").split(/\s+/).filter(Boolean);
  const chunks = [words];
  return { html: stampSpans(html, chunks) || spanHTML(chunks), chunks };
}

// chunked mode: chunker.js ramps 140→260→400 chars so chunk 1 synthesizes fast
// (TTFA) and later chunks amortize request overhead.
function prepareChunked(text, html) {
  const chunks = chunkText(text || "")
    .map(c => c.split(/\s+/).filter(Boolean))
    .filter(w => w.length);
  return { html: stampSpans(html, chunks) || spanHTML(chunks), chunks };
}

// Real per-word timestamps only when they line up 1:1 with the chunk's rendered
// word-spans; otherwise the player's length-weighted estimate takes over.
function realTimings(words, spanWords) {
  const api = words || [];
  return (api.length && api.length === spanWords.length)
    ? api.map(w => ({ start: w.start, end: w.end }))
    : null;
}

// Synth source: fetch turn audio from Bat-Speaker's /tts. Matches the
// KokoroWorkerSource/CloudUnrealSource contract (attach / begin / synthChunk /
// cancel). opts.whole picks the single-call path over per-chunk calls.
class BatSpeakerSource {
  constructor(opts) { this.opts = opts; this.ctx = { status() {}, log() {} }; this.job = null; this.slots = []; }
  attach(ctx) { this.ctx = ctx; }
  begin(job) {
    this.job = job;
    job.durations = job.durations || new Array(job.texts.length).fill(null);
    this.slots = job.texts.map(() => deferred());
    if (this.opts.whole) this._runWhole(job);
    else this._runChunked(job);
  }
  synthChunk(i) {
    return this.slots[i] ? this.slots[i].promise
      : Promise.resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 });
  }
  cancel() { this.job = null; }

  async _post(body) {
    // Relative: fetch resolves against the document base URL (not this module's
    // /web/ path), so the call lands on the same mount the page was served from.
    const r = await fetch("./tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok || d.error || !d.url) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  async _runWhole(job) {
    this.ctx.status("synthesizing…");
    try {
      const d = await this._post({ session: this.opts.session, turn: this.opts.turnId });
      if (this.job !== job) return;
      const duration = d.duration || 0.2;
      const timings = realTimings(d.words, (job.chunks[0] && job.chunks[0].words) || []);
      this.slots[0].resolve({ audioUrl: d.url, wordTimings: timings, duration });
      this.ctx.status("");
      this.ctx.log(`tts ok: ${duration}s${timings ? `, ${timings.length} real word timings` : " (estimate)"}`);
    } catch (e) {
      if (this.job !== job) return;
      this.ctx.status("TTS failed");
      this.ctx.log(`tts failed: ${e.message}`);
      job.durations[0] = 0.2;
      this.slots[0].resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 });
    }
  }

  // Sequential per-chunk requests: the server synthesizes serially anyway, and
  // in-order arrival keeps the playable prefix growing from the front. A failed
  // chunk resolves as a blip of silence so the rest of the turn still plays.
  async _runChunked(job) {
    for (let i = 0; i < job.texts.length; i++) {
      if (this.job !== job) return;
      this.ctx.status(`synthesizing ${i + 1}/${job.texts.length}…`);
      try {
        const d = await this._post({
          session: this.opts.session, turn: this.opts.turnId,
          i, n: job.texts.length, text: job.texts[i],
        });
        if (this.job !== job) return;
        const timings = realTimings(d.words, job.chunks[i].words);
        this.slots[i].resolve({ audioUrl: d.url, wordTimings: timings, duration: d.duration || 0.2 });
        this.ctx.log(`chunk ${i + 1}/${job.texts.length}: ${d.duration}s${timings ? `, ${timings.length} word timings` : " (estimate)"}`);
      } catch (e) {
        if (this.job !== job) return;
        this.ctx.log(`chunk ${i + 1} failed: ${e.message}`);
        this.slots[i].resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 });
      }
    }
    if (this.job === job) this.ctx.status("");
  }
}

// ---- on-device Kokoro (WebGPU), free: the reader's synthesis worker ----
//
// The same kokoro-js worker the reader runs (worker.bundle.js, synced from its
// build), so the same models: WebGPU fp32 (326 MB) or fp16 (163 MB), fetched
// from Hugging Face once and cached by the browser for this origin. Kokoro has
// no word timestamps, so the highlight is the player's length estimate.
//
// One worker per page — each model load is hundreds of MB of GPU memory — but
// a player per turn. LocalKokoroSource gives each turn its own view of the one
// worker: whoever spoke last owns it, and a turn whose unfinished chunks were
// taken over just re-claims it (synthesis is free) when you go back to it.
let kokoro = null;          // the shared KokoroWorkerSource
let kokoroDtype = null;     // dtype currently loaded (or loading)
let kokoroLoading = null;   // promise for that load
let kokoroVoice = "af_heart";

function ensureKokoro(dtype, onProgress) {
  if (!kokoro) {
    kokoro = new KokoroWorkerSource({ workerUrl: "./web/worker.bundle.js", getVoice: () => kokoroVoice });
  }
  if (kokoroDtype !== dtype || !kokoroLoading) {
    kokoroDtype = dtype;
    kokoroLoading = kokoro.load("webgpu", dtype, onProgress)
      .catch(e => { kokoroLoading = null; kokoroDtype = null; throw e; });
  }
  return kokoroLoading;
}

class LocalKokoroSource {
  constructor(voice) {
    this.voice = voice; this.ctx = { status() {}, log() {} };
    this.job = null; this.slots = []; this.done = [];
  }
  attach(ctx) { this.ctx = ctx; }
  begin(job) { this.job = job; this._claim(); }
  _claim() {
    kokoro.attach(this.ctx);
    kokoroVoice = this.voice;
    kokoro.begin(this.job);
    this.slots = kokoro.slots.slice();
    this.done = this.slots.map(() => false);
    this.slots.forEach((d, i) => d.promise.then(() => { this.done[i] = true; }));
  }
  synthChunk(i) {
    if (!this.job || !this.slots[i]) {
      return Promise.resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 });
    }
    if (!this.done[i] && kokoro.job !== this.job) this._claim();
    return this.slots[i].promise;
  }
  // Deliberately a no-op: after Stop the worker keeps filling this turn's
  // chunks, so replaying it is instant. A newer turn's begin() supersedes.
  cancel() {}
}

// Mount a player into `root` for one turn and start it. Returns the ReadAlong
// instance. opts: { session, turnId, text, html, whole, local, onDone }.
// opts.local = { dtype, voice } voices the turn on this device instead of /tts.
window.mountReadAlong = function (root, opts) {
  const source = opts.local
    ? new LocalKokoroSource(opts.local.voice)
    : new BatSpeakerSource({ session: opts.session, turnId: opts.turnId, whole: !!opts.whole });
  const player = new ReadAlong(root, source, {
    prepare: t => (opts.whole ? prepareOne(t, opts.html) : prepareChunked(t, opts.html)),
    getMd: () => false,
    compact: true,          // phone-first: one control line + speed stepper
    focus: true,            // fixed-gaze focus mode (◎; Bump / Still styles)
    focusKey: "batspeaker.focus",
    focusStyleKey: "batspeaker.focusStyle",
    speedKey: "batspeaker.speed",
    defaultSpeed: 1.5,
    // The player emits "done" from onended when the last chunk finishes — use
    // it to chain listen-mode without adding a hook to the shared player.
    onStatus: m => { if (m === "done" && opts.onDone) opts.onDone(); },
    onLog: m => { try { console.log("[RA]", m); } catch (e) {} },
  });
  if (!opts.local) {
    player.speak(opts.text || "");
    return player;
  }
  // First on-device Speak downloads the model; show progress in the player's
  // own status line, then speak once it's ready.
  const status = m => player._status(m);
  const mb = b => (b / 1048576).toFixed(0);
  if (!kokoro || !kokoro.ready || kokoroDtype !== opts.local.dtype) status("loading on-device voice…");
  ensureKokoro(opts.local.dtype, m => status(m.total
    ? `downloading voice model — ${mb(m.loaded)}/${mb(m.total)} MB (once, then cached)`
    : `downloading voice model — ${mb(m.loaded)} MB (once, then cached)`))
    .then(() => { status(""); player.speak(opts.text || ""); })
    .catch(e => status(`on-device voice failed: ${e.message}`));
  return player;
};

// Can this browser run the on-device voice? Chromium desktop with a WebGPU
// adapter (the page separately checks /config that the worker bundle is
// installed). Firefox's WebGPU garbles Kokoro even
// at fp32, and on iOS WebGPU fp16 is static and fp32 crashes the tab (both
// measured in the reader), so those never see the option.
window.localVoiceSupported = async function () {
  const ua = navigator.userAgent;
  if (!navigator.gpu || !/Chrome\//.test(ua) || /Mobile|Android/.test(ua)) return false;
  try { return !!(await navigator.gpu.requestAdapter()); }
  catch (e) { return false; }
};
