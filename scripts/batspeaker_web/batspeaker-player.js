// Bat-Speaker adoption of the shared ReadAlong player (2026-07-03, step 2).
//
// Replaces the native <audio controls> per-turn play path with a real read-along
// player: big transport, scrub, click-to-seek, and client-side pitch-preserved
// speed (persisted). Exposes window.mountReadAlong for the page's inline script.
//
// v1 is deliberately whole-turn: one /tts call → one /audio mp3 → one chunk, with
// the length-weighted estimate highlight. The fast-follow swaps this source for a
// chunk-level one that returns Unreal's real per-word timestamps (sample-accurate
// highlight + progressive TTFA). The player itself won't change — that's the point
// of the seam.

import { ReadAlong } from "./readalong.js";
import { silence } from "./audioutil.js";

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function esc(s) {
  return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// v1: the whole turn is a single chunk. Word-spans come from the SAME spoken
// text the server synthesizes (turn_spoken_text), so the highlight tracks the
// audio's word order. Estimate highlight spans the whole turn's duration.
function prepareOne(text) {
  const words = (text || "").split(/\s+/).filter(Boolean);
  const html = words.map((w, i) => `<span class="w" data-c="0" data-w="${i}">${esc(w)}</span>`).join(" ");
  return { html, chunks: [words] };
}

// Synth source: fetch the whole turn's 1x mp3 from Bat-Speaker's /tts and hand
// the player a single chunk. Matches the KokoroWorkerSource/CloudUnrealSource
// contract (attach / begin / synthChunk / cancel).
class BatSpeakerSource {
  constructor(opts) { this.opts = opts; this.ctx = { status() {}, log() {} }; this.job = null; this.slots = []; }
  attach(ctx) { this.ctx = ctx; }
  begin(job) {
    this.job = job;
    job.durations = job.durations || new Array(job.texts.length).fill(null);
    this.slots = job.texts.map(() => deferred());
    this._run(job);
  }
  synthChunk(i) {
    return this.slots[i] ? this.slots[i].promise
      : Promise.resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 });
  }
  cancel() { this.job = null; }
  async _run(job) {
    this.ctx.status("synthesizing…");
    try {
      const r = await fetch("/tts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session: this.opts.session, turn: this.opts.turnId }),
      });
      const d = await r.json();
      if (this.job !== job) return;
      if (!r.ok || d.error || !d.url) throw new Error(d.error || `HTTP ${r.status}`);
      const duration = d.duration || 0.2;
      job.durations[0] = duration;
      // Real Unreal per-word timestamps → sample-accurate highlight, but only if
      // they line up 1:1 with the rendered word-spans; otherwise let the player's
      // length-weighted estimate take over.
      const span = (job.chunks[0] && job.chunks[0].words) || [];
      const api = d.words || [];
      const timings = (api.length && api.length === span.length)
        ? api.map(w => ({ start: w.start, end: w.end }))
        : null;
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
}

// Mount a player into `root` for one turn and start it. Returns the ReadAlong
// instance. opts: { session, turnId, text, voice, onDone }.
window.mountReadAlong = function (root, opts) {
  const source = new BatSpeakerSource({
    session: opts.session, turnId: opts.turnId, getVoice: opts.getVoice || (() => opts.voice),
  });
  const player = new ReadAlong(root, source, {
    prepare: t => prepareOne(t),
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
