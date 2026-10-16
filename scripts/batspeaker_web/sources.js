// Synth sources — the pluggable seam behind the ReadAlong player (2026-07-03).
//
// A source turns chunk text into playable audio (+ optional real per-word
// timings). The player owns orchestration (which chunk, when, at what speed);
// the source owns HOW a chunk is synthesized and any host-specific caching.
//
// Contract:
//   attach(ctx)      one-time. ctx = { status(msg), log(msg) } scoped to the
//                    player instance the source is feeding.
//   begin(job)       a new speak(). job = { id, texts: [string] }. Batch
//                    engines (the kokoro worker) kick off all synthesis here;
//                    per-request engines record and lazily fetch. Sets up one
//                    deferred per chunk.
//   synthChunk(i)    → Promise<{ audioUrl, wordTimings|null, duration }>.
//                    Memoized per job (returns the same promise on repeat calls,
//                    so the player can both eager-fire for the timeline and
//                    await it in gotoChunk without double-synthesizing). null
//                    wordTimings means "no real timings — use the estimate."
//   cancel()         abandon the current job's in-flight work.
//
// Both sources today resolve every chunk exactly once (a failure resolves to a
// beat of silence), so nothing downstream can hang. Behavior is deliberately
// eager/sequential — matches the pre-refactor reader exactly. Progressive
// pull + server cache is a later (Bat-Speaker) concern layered on this seam.

import { encodeWav, b64ToUrl, silence } from "./audioutil.js";

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/* ---- on-device kokoro (WASM / WebGPU), via the synthesis Web Worker ---- */
export class KokoroWorkerSource {
  // opts: { workerUrl, getVoice }
  constructor(opts = {}) {
    this.opts = opts;
    this.ctx = { status() {}, log() {} };
    this.job = null;
    this.slots = [];            // deferred per chunk of the current job
    this.ready = false;
    this._loadResolve = null;
    this._loadReject = null;
    this._onProgress = null;
    // heartbeat: a phone can take tens of seconds per chunk, so a silent
    // "synthesizing…" reads as a hang. Tick a live elapsed + ready count.
    this._hbIdx = -1; this._hbAt = 0; this._hbStage = ""; this._hbTimer = null;

    const worker = new Worker(opts.workerUrl || "./worker.bundle.js", { type: "module" });
    this.worker = worker;
    worker.onerror = e => {
      this.ctx.status("worker failed to start");
      this.ctx.log(`WORKER ERROR: ${e.message || "(no message)"} @ ${e.filename || "?"}:${e.lineno || "?"}`);
      this._loadFailed(new Error(e.message || "worker failed to start"));
    };
    worker.onmessage = e => this._onMessage(e.data);
  }

  attach(ctx) { this.ctx = ctx; }

  // Load (or reload) the model. Driven by the reader's engine/dtype UI; the
  // returned promise resolves with { voices, device, dtype, ... } on "ready".
  load(device, dtype, onProgress) {
    this.ready = false;
    this._onProgress = onProgress || null;
    return new Promise((res, rej) => {
      this._loadResolve = res;
      this._loadReject = rej;
      this.worker.postMessage({ type: "load", device, dtype });
    });
  }

  // A load error must settle the load promise — otherwise the UI waits on
  // "loading model…" forever with the failure invisible (the 2026-07-05 hang).
  _loadFailed(err) {
    if (!this._loadReject) return;
    const rej = this._loadReject;
    this._loadReject = null; this._loadResolve = null;
    rej(err);
  }

  _hb() {
    if (!this.job || this._hbIdx < 0) return;
    const el = ((performance.now() - this._hbAt) / 1000).toFixed(0);
    const ready = this.job.durations.filter(d => d != null).length;
    this.ctx.status(`synthesizing ${this._hbIdx + 1}/${this.job.texts.length}${this._hbStage} · ${el}s · ${ready}/${this.job.texts.length} ready`);
  }
  _hbStart() { this._hb(); if (!this._hbTimer) this._hbTimer = setInterval(() => this._hb(), 500); }
  _hbStop() { this._hbIdx = -1; if (this._hbTimer) { clearInterval(this._hbTimer); this._hbTimer = null; } }

  begin(job) {
    this.job = job;
    job.durations = job.durations || new Array(job.texts.length).fill(null);
    this.slots = job.texts.map(() => deferred());
    this.worker.postMessage({
      type: "synth",
      id: job.id,
      chunks: job.texts,
      voice: (this.opts.getVoice && this.opts.getVoice()) || "af_heart",
      speed: 1.0,   // playback speed is applied by the player, pitch-preserved
    });
  }

  synthChunk(i) { return this.slots[i] ? this.slots[i].promise : Promise.resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 }); }

  cancel() {
    this._hbStop();
    // The worker drops any job whose id isn't the newest; the next begin()
    // supersedes. Slots for the abandoned job are simply never awaited.
    this.job = null;
  }

  _onMessage(m) {
    const job = this.job;
    if (m.type === "boot") { this.ctx.log("worker booted"); return; }
    if (m.type === "info") { this.ctx.log(m.message); return; }
    if (m.type === "load-progress") { if (this._onProgress) this._onProgress(m); return; }
    if (m.type === "ready") {
      this.ready = true;
      this.ctx.log(`model loaded in ${(m.loadMs / 1000).toFixed(1)}s  device=${m.device} dtype=${m.dtype} threads=${m.threads}/${m.cores} isolated=${m.isolated}${m.apple ? " apple=1(single-thread)" : ""}`);
      if (this._loadResolve) { this._loadResolve(m); this._loadResolve = null; this._loadReject = null; }
      return;
    }
    if (m.type === "error" && this._loadReject) {
      // Load failure — there's no job yet, so handle it before the job guard
      // below (which would silently drop it, and did until 2026-07-05).
      this.ctx.status("model load failed — see debug log");
      this.ctx.log(`ERROR (${m.where}): ${m.message}`);
      this._loadFailed(new Error(m.message));
      return;
    }
    // Everything below is job-scoped; ignore stragglers from a superseded job.
    if (!job || m.id !== job.id) return;
    if (m.type === "synth-start") {
      this._hbIdx = m.i; this._hbAt = performance.now(); this._hbStage = ""; this._hbStart();
    } else if (m.type === "synth-progress") {
      if (m.stage === "phonemize") this._hbStage = " (phonemizing)";
      else if (m.stage === "generate") this._hbStage = m.pieces > 1 ? ` (piece ${m.piece}/${m.pieces})` : " (generating)";
      this._hb();
    } else if (m.type === "chunk") {
      const rtf = (m.synthMs / 1000) / m.audioSec;
      const split = m.pieces > 1 ? `, ${m.pieces} pieces` : "";
      this.ctx.log(`chunk ${m.i + 1}/${m.n} (${m.chars} chars${split}): ${m.synthMs.toFixed(0)}ms for ${m.audioSec.toFixed(1)}s audio (RTF ${rtf.toFixed(2)})`);
      job.durations[m.i] = m.audioSec;
      this.slots[m.i].resolve({ audioUrl: encodeWav(m.audio, m.sampleRate), wordTimings: null, duration: m.audioSec });
    } else if (m.type === "chunk-error") {
      // Skip the bad chunk with a beat of silence — one failure must not halt
      // the whole read.
      this.ctx.log(`chunk ${m.i + 1} FAILED (skipping): ${m.message}`);
      job.durations[m.i] = 0.2;
      this.slots[m.i].resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 });
    } else if (m.type === "synth-done") {
      this._hbStop(); this.ctx.status("");
    } else if (m.type === "error") {
      this._hbStop();
      this.ctx.status(`error in ${m.where} — see debug log`);
      this.ctx.log(`ERROR (${m.where}): ${m.message}`);
    }
  }
}

/* ---- cloud (Inworld TTS-2 / Unreal Speech), via a same-origin /tts proxy
   that holds the keys and returns mp3 + real per-word timestamps ---- */
const AUTH_MSG = "cloud password rejected — check the Password field";

export class CloudSource {
  // opts: { endpoint = "tts", getEngine, getVoice, getPassword }
  constructor(opts = {}) {
    this.opts = opts;
    this.ctx = { status() {}, log() {} };
    this.job = null;
    this.slots = [];
  }

  attach(ctx) { this.ctx = ctx; }

  // The proxy aligns the engine's tokens onto a /\s+/ split of the chunk text
  // (same split our chunker makes), so words line up 1:1. On any mismatch,
  // return null and let the player's length-weighted estimate stand.
  _realTimings(spanWords, apiWords) {
    if (!apiWords.length || apiWords.length !== spanWords.length) return null;
    return apiWords.map(w => ({ start: w.start, end: w.end }));
  }

  begin(job) {
    this.job = job;
    job.durations = job.durations || new Array(job.texts.length).fill(null);
    this.slots = job.texts.map(() => deferred());
    this._run(job);   // sequential loop, exactly as the pre-refactor cloudSynth
  }

  synthChunk(i) { return this.slots[i] ? this.slots[i].promise : Promise.resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 }); }

  cancel() { this.job = null; }

  async _run(job) {
    const engine = (this.opts.getEngine && this.opts.getEngine()) || "inworld";
    const voice = (this.opts.getVoice && this.opts.getVoice()) ||
      (engine === "unreal" ? "Scarlett" : "Malcolm");
    const endpoint = this.opts.endpoint || "tts";
    const pass = (this.opts.getPassword && this.opts.getPassword()) || "";
    let authFailed = false;
    for (let i = 0; i < job.texts.length; i++) {
      if (this.job !== job) return;                 // superseded by a newer speak
      this.ctx.status(`synthesizing ${i + 1}/${job.texts.length}…`);
      try {
        if (authFailed) throw new Error(AUTH_MSG);   // gate already rejected — stop hammering it
        const r = await fetch(endpoint, {
          method: "POST",
          headers: { "content-type": "application/json", authorization: `Bearer ${pass}` },
          body: JSON.stringify({ text: job.texts[i], voice, engine }),
        });
        if (r.status === 401 || r.status === 403) { authFailed = true; throw new Error(AUTH_MSG); }
        const data = await r.json();
        if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
        if (this.job !== job) return;
        const words = data.words || [];
        const duration = words.length ? words[words.length - 1].end : 0.2;
        job.durations[i] = duration;
        this.slots[i].resolve({
          audioUrl: b64ToUrl(data.audio, data.mime),
          wordTimings: this._realTimings(job.chunks[i].words, words),
          duration,
        });
        this.ctx.log(`chunk ${i + 1}/${job.texts.length}: ${words.length} words, ${duration.toFixed(1)}s (${engine}/${voice})`);
      } catch (e) {
        if (this.job !== job) return;
        this.ctx.log(`chunk ${i + 1} FAILED (skipping): ${e.message}`);
        if (authFailed) this.ctx.status(AUTH_MSG);
        job.durations[i] = 0.2;
        this.slots[i].resolve({ audioUrl: silence(), wordTimings: null, duration: 0.2 });
      }
    }
    if (this.job === job) this.ctx.status(authFailed ? AUTH_MSG : "");
  }
}
