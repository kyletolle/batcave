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

// whole-turn mode: a single chunk spanning the turn.
function prepareOne(text) {
  const words = (text || "").split(/\s+/).filter(Boolean);
  return { html: spanHTML([words]), chunks: [words] };
}

// chunked mode: chunker.js ramps 140→260→400 chars so chunk 1 synthesizes fast
// (TTFA) and later chunks amortize request overhead.
function prepareChunked(text) {
  const chunks = chunkText(text || "")
    .map(c => c.split(/\s+/).filter(Boolean))
    .filter(w => w.length);
  return { html: spanHTML(chunks), chunks };
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
    const r = await fetch("/tts", {
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

// Mount a player into `root` for one turn and start it. Returns the ReadAlong
// instance. opts: { session, turnId, text, whole, onDone }.
window.mountReadAlong = function (root, opts) {
  const source = new BatSpeakerSource({
    session: opts.session, turnId: opts.turnId, whole: !!opts.whole,
  });
  const player = new ReadAlong(root, source, {
    prepare: t => (opts.whole ? prepareOne(t) : prepareChunked(t)),
    getMd: () => false,
    compact: true,          // phone-first: one control line + speed stepper
    speedKey: "batspeaker.speed",
    defaultSpeed: 1.5,
    // The player emits "done" from onended when the last chunk finishes — use
    // it to chain listen-mode without adding a hook to the shared player.
    onStatus: m => { if (m === "done" && opts.onDone) opts.onDone(); },
    onLog: m => { try { console.log("[RA]", m); } catch (e) {} },
  });
  player.speak(opts.text || "");
  return player;
};
