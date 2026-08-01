// ReadAlong — the multi-instance-safe read-along player (2026-07-03 refactor).
//
// Extracted from the reader's app.js: the global timeline over N audio chunks,
// scrubbing, skips, replay-sentence, click-any-word-to-seek, pitch-preserved
// speed, and the follow-along word highlight — all now instance-local. No
// module-level mutable state; each instance owns its own <audio>, job, speed,
// and DOM subtree, so N of them (one per Bat-Speaker turn) coexist.
//
//   new ReadAlong(root, source, opts)
//     root    an element the player fills with its pane + transport controls
//     source  a synth source (see sources.js): synthChunk(i) → {audioUrl,
//             wordTimings|null, duration}
//     opts    { prepare(text, md) → {html, chunks:[[word]]},
//               getMd?() → bool, presets?, speedKey?, defaultSpeed?,
//               compact?: bool (single-line transport + speed stepper),
//               focus?: bool (offer the focus-mode ribbon), focusKey?,
//               focusStyleKey?,
//               onStatus?(msg), onLog?(msg), onFirstAudio?(ms), onNewText?() }
//
// The player drives synthesis through the source only; it never knows whether a
// chunk came from a worker or a fetch.

import { silence, fmt } from "./audioutil.js";

const DEFAULT_PRESETS = [1, 1.25, 1.5, 1.75, 2, 2.5, 2.75, 3];

// Focus-mode view: shared by both control variants. The ribbon (bump style)
// and the rsvp trio (still style) live side by side; the active style shows one.
function focusHTML() {
  return `
  <div class="ra-focus hidden">
    <div class="ra-focus-win">
      <div class="ra-ribbon"></div>
      <div class="ra-rsvp hidden">
        <span class="w rs-prev"></span><span class="w rs-cur"></span><span class="w rs-next"></span>
      </div>
    </div>
  </div>`;
}

function controlsHTML(withNewText, withFocus) {
  return `
  <div class="ra-pane"></div>
  ${withFocus ? focusHTML() : ""}
  <div class="ra-controls">
    <input class="scrub ra-scrub" type="range" min="0" max="0" step="0.05" value="0">
    <div class="row">
      <span class="time ra-time">0:00 / 0:00</span>
      <span class="spacer"></span>
      <span class="status ra-status"></span>
    </div>
    <div class="row transport">
      <button class="icon" data-ra="back15" title="Back 15s">« 15</button>
      <button class="icon" data-ra="back10" title="Back 10s">‹ 10</button>
      <button class="round" data-ra="replay" title="Replay this sentence">↺</button>
      <button class="round primary" data-ra="play" title="Play / pause">▶</button>
      <button class="icon" data-ra="fwd10" title="Forward 10s">10 ›</button>
      <button class="icon" data-ra="fwd30" title="Forward 30s">30 »</button>
    </div>
    <div class="row speeds ra-speeds"></div>
    <div class="row">
      ${withNewText ? `<button data-ra="newText">‹ New text</button>` : ""}
      ${withFocus ? `<button data-ra="focus" title="Focus mode — the text flows past a fixed point (f)">Focus</button>
      <button data-ra="fstyle" class="hidden" title="Focus style — Bump slides the line one word at a time; Still swaps the word in place with no motion">Bump</button>` : ""}
      <button data-ra="stop">Stop</button>
    </div>
  </div>`;
}

// Compact transport (opts.compact): scrub + a single control line — seek ‹10 /
// play / 10›, a prev/next speed stepper, time, stop. Drops «15 / 30» / replay to
// fit narrow screens. The speed stepper (see _renderSpeeds) steps through the
// presets and clamps at the ends, so 3× steps down to 2.75× rather than wrapping.
function compactControlsHTML(withFocus) {
  return `
  <div class="ra-pane"></div>
  ${withFocus ? focusHTML() : ""}
  <div class="ra-controls compact">
    <input class="scrub ra-scrub" type="range" min="0" max="0" step="0.05" value="0">
    <div class="row transport compact-bar">
      <button class="icon" data-ra="back10" title="Back 10s">‹10</button>
      <button class="round primary" data-ra="play" title="Play / pause">▶</button>
      <button class="icon" data-ra="fwd10" title="Forward 10s">10›</button>
      <span class="ra-speeds spd-stepper"></span>
      ${withFocus ? `<button class="icon" data-ra="focus" title="Focus mode — the text flows past a fixed point">◎</button>` : ""}
      <span class="time ra-time">0:00 / 0:00</span>
      <button class="icon" data-ra="stop" title="Stop">⏹</button>
    </div>
    <div class="row">
      ${withFocus ? `<button class="icon hidden" data-ra="fstyle" title="Focus style — Bump slides the line one word at a time; Still swaps the word in place with no motion">Bump</button>` : ""}
      <span class="status ra-status"></span>
    </div>
  </div>`;
}

export class ReadAlong {
  constructor(root, source, opts = {}) {
    this.root = root;
    this.source = source;
    this.opts = opts;
    this.presets = opts.presets || DEFAULT_PRESETS;
    this.speedKey = opts.speedKey || "reader.speed";
    this.speed = +(localStorage.getItem(this.speedKey) || opts.defaultSpeed || 1.5);

    // playback state — all instance-local
    this.job = null;
    this.curIdx = 0;
    this.gen = 0;            // cancels superseded chunk loads
    this.srcGen = 0;         // gen when the current audio.src was installed
    this.scrubbing = false;
    this._curWord = null;

    // focus mode (the ribbon): opt-in via opts.focus, persisted per instance key
    this.focusKey = opts.focusKey || "reader.focus";
    this.focusOn = !!opts.focus && localStorage.getItem(this.focusKey) === "1";
    this.focusStyleKey = opts.focusStyleKey || "reader.focusStyle";
    this.focusStyle = localStorage.getItem(this.focusStyleKey) === "bump" ? "bump" : "still";
    this._rsvpG = -1;          // flat word index currently shown by the still style
    this._raf = 0;             // rAF handle for the ribbon loop
    this._ribX = null;         // ribbon rest position (px into the line)
    this._ribbonJobId = null;  // job the ribbon DOM was built for
    this._ribSpans = null;     // flat span list, global word order
    this._centers = null;      // center-x of each span within the ribbon
    this._chunkBase = null;    // chunk index → first global word index
    this._curRibbonWord = null;

    this.unlockPending = false;
    this.firstAudioReported = false;
    this.speakT0 = 0;

    const audio = new Audio();
    audio.preservesPitch = true;
    if ("mozPreservesPitch" in audio) audio.mozPreservesPitch = true;
    if ("webkitPreservesPitch" in audio) audio.webkitPreservesPitch = true;
    this.audio = audio;

    this._build();
    source.attach({ status: m => this._status(m), log: m => this._log(m) });
  }

  /* ---- setup ---- */
  _build() {
    this.root.innerHTML = this.opts.compact
      ? compactControlsHTML(!!this.opts.focus)
      : controlsHTML(!!this.opts.onNewText, !!this.opts.focus);
    const q = s => this.root.querySelector(s);
    this.el = {
      pane: q(".ra-pane"), controls: q(".ra-controls"), scrub: q(".ra-scrub"),
      time: q(".ra-time"), status: q(".ra-status"), speeds: q(".ra-speeds"),
      play: q('[data-ra="play"]'),
      focus: q(".ra-focus"), focusWin: q(".ra-focus-win"), ribbon: q(".ra-ribbon"),
      focusBtn: q('[data-ra="focus"]'), fstyleBtn: q('[data-ra="fstyle"]'),
      rsvp: q(".ra-rsvp"), rsPrev: q(".rs-prev"), rsCur: q(".rs-cur"), rsNext: q(".rs-next"),
    };

    const on = (name, fn) => { const b = q(`[data-ra="${name}"]`); if (b) b.onclick = fn; };
    on("play", () => this.togglePlay());
    on("focus", () => this.toggleFocus());
    on("fstyle", () => this.toggleFocusStyle());
    on("stop", () => this.stop());
    on("replay", () => this.seek(this.offsets()[this.curIdx], true));   // restart current sentence
    on("back15", () => this.skip(-15));
    on("back10", () => this.skip(-10));
    on("fwd10", () => this.skip(10));
    on("fwd30", () => this.skip(30));
    on("newText", () => { this.stop(); this.opts.onNewText && this.opts.onNewText(); });

    this.el.scrub.oninput = () => {
      this.scrubbing = true;
      this.el.time.textContent = `${fmt(+this.el.scrub.value)} / ${fmt(this.totalKnown())}`;
    };
    this.el.scrub.onchange = () => { this.scrubbing = false; this.seek(+this.el.scrub.value); };

    const wordClick = e => {                                // click any word → jump & play
      const s = e.target.closest(".w");
      if (!s || !this.job || s.dataset.c == null) return;   // (empty rsvp neighbor has no target)
      const c = +s.dataset.c, w = +s.dataset.w;
      const t = this.wordTimings(c);
      if (t) this.seek(this.offsets()[c] + t[w].start, true);
      else this.gotoChunk(c, 0, true);   // chunk not synthesized yet — jump there and wait
    };
    this.el.pane.addEventListener("click", wordClick);
    this.el.focus.addEventListener("click", wordClick);     // ribbon words seek too

    const audio = this.audio;
    audio.onplay = () => { this.el.play.textContent = "⏸"; };
    audio.onpause = () => { this.el.play.textContent = "▶"; };
    audio.onended = () => {
      // The unlock silence ending must not advance the player (or crash pre-job),
      // and neither may audio a newer seek has already superseded.
      if (this.unlockPending || !this.job) { this.unlockPending = false; return; }
      if (this.srcGen !== this.gen) return;
      if (this.curIdx + 1 < this.job.n) this.gotoChunk(this.curIdx + 1, 0, true);
      else this._status("done");
    };
    audio.ontimeupdate = () => {
      if (!this.job) return;
      const gt = this.globalTime();
      if (!this.scrubbing) this.el.scrub.value = gt;
      this.el.time.textContent = `${fmt(gt)} / ${fmt(this.totalKnown())}`;
      this.highlight(gt);
    };

    this._renderSpeeds();
    this._applyFocusView();   // restore a saved focus preference
  }

  /* ---- focus mode: the ribbon ----
     One non-wrapping line of the whole text under a fixed focal point; the eye
     stays put and the words come to it. The current word RESTS centered on the
     focal mark and the ribbon bumps one whole word at a time when the spoken
     word changes (continuous per-word interpolation was tried first and reads
     as a blur at high speeds — 2026-07-24). Word boundaries come from the same
     wordTimings() the highlight uses (real cloud timestamps or the length
     estimate). Driven by rAF because audio timeupdate only fires a few times a
     second and the bump easing needs frames. */
  toggleFocus(force) {
    if (!this.opts.focus) return;
    this.focusOn = force != null ? !!force : !this.focusOn;
    localStorage.setItem(this.focusKey, this.focusOn ? "1" : "0");
    this._applyFocusView();
  }

  _applyFocusView() {
    if (!this.opts.focus) return;
    this.el.focus.classList.toggle("hidden", !this.focusOn);
    this.el.pane.classList.toggle("hidden", this.focusOn);
    this.el.focusBtn.classList.toggle("active", this.focusOn);
    this.el.fstyleBtn.classList.toggle("hidden", !this.focusOn);
    if (this.focusOn) { this._buildRibbon(); this._applyFocusStyle(); this._startRibbon(); }
    else this._stopRibbon();
  }

  // Two focus styles, switchable mid-playback for side-by-side feel:
  //   still — RSVP: the word swaps in place at the focal point, zero motion,
  //           dimmed prev/next neighbors for context (default)
  //   bump  — the ribbon line slides one word at a time
  toggleFocusStyle() {
    this.focusStyle = this.focusStyle === "still" ? "bump" : "still";
    localStorage.setItem(this.focusStyleKey, this.focusStyle);
    this._applyFocusStyle();
  }

  _applyFocusStyle() {
    const still = this.focusStyle === "still";
    this.el.ribbon.classList.toggle("hidden", still);
    this.el.rsvp.classList.toggle("hidden", !still);
    this.el.fstyleBtn.textContent = still ? "Still" : "Bump";
    this._ribX = null;     // returning to bump snaps into place, no cross-doc glide
    this._rsvpG = -1;      // re-render the rsvp words on the next frame
  }

  // Build the ribbon spans for the current job and measure each word's center.
  // Must run while the ribbon is visible (offsetLeft is 0 inside display:none).
  _buildRibbon() {
    if (!this.job || this._ribbonJobId === this.job.id) return;
    const rib = this.el.ribbon;
    rib.style.transitionDuration = "0ms";   // never animate a new-job reset
    rib.style.transform = "translate3d(0,0,0)";
    rib.innerHTML = "";
    this._ribSpans = []; this._chunkBase = [];
    let g = 0;
    for (const ch of this.job.chunks) {
      this._chunkBase[ch.i] = g;
      ch.words.forEach((w, wi) => {
        const s = document.createElement("span");
        s.className = "w";
        s.dataset.c = ch.i; s.dataset.w = wi;
        s.textContent = w;
        rib.appendChild(s);
        rib.appendChild(document.createTextNode(" "));
        this._ribSpans.push(s);
        g++;
      });
    }
    // One layout pass for all centers. The ribbon may be hidden right now
    // (still style) — centers measure as 0 inside display:none, so unhide for
    // the measurement; _applyFocusStyle restores the right visibility.
    const wasHidden = rib.classList.contains("hidden");
    if (wasHidden) rib.classList.remove("hidden");
    this._centers = this._ribSpans.map(s => s.offsetLeft + s.offsetWidth / 2);
    if (wasHidden) rib.classList.add("hidden");
    this._ribbonJobId = this.job.id;
    this._ribX = null;          // snap to position on the next frame
    this._curRibbonWord = null;
  }

  _startRibbon() {
    if (this._raf) return;
    const step = ts => { this._raf = requestAnimationFrame(step); this._ribbonFrame(ts); };
    this._raf = requestAnimationFrame(step);
  }
  _stopRibbon() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
  }

  _ribbonFrame() {
    if (!this.job || !this._centers || !this._centers.length) return;
    const t = this.wordTimings(this.curIdx);       // null while chunk unsynthesized → hold
    if (!t) return;
    const localT = this.globalTime() - this.offsets()[this.curIdx];
    let k = t.findIndex(s => localT < s.end);
    if (k < 0) k = t.length - 1;
    const g = this._chunkBase[this.curIdx] + k;

    if (this.focusStyle === "still") { this._rsvpFrame(g); return; }
    const target = this._centers[g];

    if (target !== this._ribX) {
      // The bump: write the transform ONCE per word change and let a CSS
      // transition carry it — duration ~40% of the word's rate-adjusted
      // airtime (words at 2.5× last ~140ms; a JS ease can't settle in that
      // window, which read as a blur). The other ~60% is genuine stillness.
      // Seeks and the first show snap: gliding would smear across the doc.
      const wordMs = (t[k].end - t[k].start) / (this.speed || 1) * 1000;
      const snap = this._ribX == null || Math.abs(target - this._ribX) > 1200;
      this.el.ribbon.style.transitionDuration =
        snap ? "0ms" : `${Math.max(30, Math.min(90, wordMs * 0.4)).toFixed(0)}ms`;
      const mid = this.el.focusWin.clientWidth / 2;
      this.el.ribbon.style.transform = `translate3d(${(mid - target).toFixed(2)}px,0,0)`;
      this._ribX = target;
    }

    const span = this._ribSpans[g];
    if (span && span !== this._curRibbonWord) {
      if (this._curRibbonWord) this._curRibbonWord.classList.remove("cur");
      span.classList.add("cur");
      this._curRibbonWord = span;
    }
  }

  // still style: swap the word in place — no motion at all. The hidden ribbon
  // spans double as the flat word list (text + seek dataset).
  _rsvpFrame(g) {
    if (g === this._rsvpG) return;
    this._rsvpG = g;
    const fill = (el, span) => {
      el.textContent = span ? span.textContent : "";
      if (span) { el.dataset.c = span.dataset.c; el.dataset.w = span.dataset.w; }
      else { delete el.dataset.c; delete el.dataset.w; }
    };
    fill(this.el.rsPrev, this._ribSpans[g - 1]);
    fill(this.el.rsCur, this._ribSpans[g]);
    fill(this.el.rsNext, this._ribSpans[g + 1]);
  }

  // index of the current speed in presets; snaps to nearest if it isn't one
  _speedIndex() {
    const i = this.presets.indexOf(this.speed);
    if (i >= 0) return i;
    let best = 0, bd = Infinity;
    this.presets.forEach((v, k) => { const d = Math.abs(v - this.speed); if (d < bd) { bd = d; best = k; } });
    return best;
  }

  _renderSpeeds() {
    const box = this.el.speeds;
    box.innerHTML = "";
    if (this.opts.compact) {
      // Prev/next stepper: nudge one preset at a time, clamped at the ends — from
      // 3× you step down to 2.75×, never wrap around to 1×.
      const prev = document.createElement("button");
      prev.className = "icon"; prev.textContent = "‹"; prev.title = "Slower";
      const val = document.createElement("span");
      val.className = "spd-val";
      const next = document.createElement("button");
      next.className = "icon"; next.textContent = "›"; next.title = "Faster";
      const render = () => {
        val.textContent = this.speed + "×";
        const i = this._speedIndex();
        prev.disabled = i <= 0;
        next.disabled = i >= this.presets.length - 1;
      };
      const step = d => {
        const i = Math.min(this.presets.length - 1, Math.max(0, this._speedIndex() + d));
        this.speed = this.presets[i];
        this.audio.playbackRate = this.speed;
        localStorage.setItem(this.speedKey, this.speed);
        render();
      };
      prev.onclick = () => step(-1);
      next.onclick = () => step(1);
      box.append(prev, val, next);
      render();
      return;
    }
    for (const v of this.presets) {
      const b = document.createElement("button");
      b.textContent = v + "×";
      b.className = v === this.speed ? "active" : "";
      b.onclick = () => {
        this.speed = v; this.audio.playbackRate = v;
        localStorage.setItem(this.speedKey, v);
        [...box.children].forEach(c => c.classList.toggle("active", c === b));
      };
      box.appendChild(b);
    }
  }

  _status(m) { this.el.status.textContent = m; if (this.opts.onStatus) this.opts.onStatus(m); }
  _log(m) { if (this.opts.onLog) this.opts.onLog(m); }

  // Swap the active synth source (the reader flips kokoro↔cloud at speak-time).
  // Not to be called mid-job; the glue picks the source before each speak().
  setSource(source) {
    this.source = source;
    source.attach({ status: m => this._status(m), log: m => this._log(m) });
  }

  /* ---- timeline math (durations fill in as chunks synthesize, in order) ---- */
  offsets() {
    const o = [0];
    for (let i = 0; i < this.job.n; i++) o[i + 1] = o[i] + (this.job.durations[i] || 0);
    return o;
  }
  totalKnown() {                 // duration of the contiguous ready prefix
    let t = 0;
    for (let i = 0; i < this.job.n; i++) {
      if (this.job.durations[i] == null) break;
      t += this.job.durations[i];
    }
    return t;
  }
  globalTime() { return this.offsets()[this.curIdx] + (this.audio.currentTime || 0); }

  // per-word timing within a chunk: real timings if the source gave them
  // (cloud), else distribute the chunk's audio duration across its words by
  // length + punctuation pause. Re-anchored per chunk, so error can't accumulate.
  wordTimings(i) {
    const c = this.job.chunks[i];
    if (c._timings) return c._timings;
    const dur = this.job.durations[i];
    if (dur == null) return null;
    const weights = c.words.map(w => {
      let base = Math.max(1, w.replace(/[^\w]/g, "").length || 1);
      if (/[.!?]["')\]]?$/.test(w)) base += 6;
      else if (/[,;:]$/.test(w)) base += 3;
      else if (/[—–-]$/.test(w)) base += 2;
      return base;
    });
    const sum = weights.reduce((a, b) => a + b, 0) || 1;
    let t = 0;
    c._timings = c.words.map((w, k) => {
      const d = dur * weights[k] / sum;
      const seg = { start: t, end: t + d };
      t += d;
      return seg;
    });
    return c._timings;
  }

  /* ---- rendering ---- */
  _renderReader(html) {
    const pane = this.el.pane;
    this._ribbonJobId = null;                      // new job → stale ribbon
    if (this.opts.focus && this.focusOn) this._buildRibbon();
    if (html) { pane.innerHTML = html; return; }
    pane.innerHTML = "";
    for (const ch of this.job.chunks) {
      ch.words.forEach((w, wi) => {
        const s = document.createElement("span");
        s.className = "w";
        s.dataset.c = ch.i; s.dataset.w = wi;
        s.textContent = w;
        pane.appendChild(s);
        pane.appendChild(document.createTextNode(" "));
      });
    }
  }
  highlight(gt) {
    const t = this.wordTimings(this.curIdx);
    if (!t) return;
    const localT = gt - this.offsets()[this.curIdx];
    let idx = t.findIndex(s => localT < s.end);
    if (idx < 0) idx = t.length - 1;
    const el = this.el.pane.querySelector(`.w[data-c="${this.curIdx}"][data-w="${idx}"]`);
    if (el && el !== this._curWord) {
      if (this._curWord) this._curWord.classList.remove("cur");
      el.classList.add("cur");
      // in focus mode the pane is hidden — the ribbon handles motion
      if (!this.focusOn) el.scrollIntoView({ block: "center", behavior: "smooth" });
      this._curWord = el;
    }
  }

  /* ---- playback ---- */
  async gotoChunk(c, localT, autoplay) {
    const myGen = ++this.gen;
    this.curIdx = c;
    const starved = this.job.durations[c] == null;
    if (starved) {
      this._status(`buffering — synthesizing chunk ${c + 1}/${this.job.n}…`);
      // Waiting on the very first chunk is just startup (TTFA covers it), not
      // playback outrunning synthesis.
      if (this.totalKnown() > 0) this._log(`STARVED at chunk ${c + 1} — playback outran synthesis (RTF × speed > 1: lower the speed or try another dtype)`);
    }
    let res;
    try { res = await this.source.synthChunk(c); }
    catch (e) { this._status(String(e.message || e)); return; }
    if (starved && c + 1 < this.job.n) {
      // Consolidate stalls: once caught out, buffer one chunk of lookahead
      // before resuming instead of stuttering at every boundary.
      await this.source.synthChunk(c + 1).catch(() => {});
    }
    if (myGen !== this.gen) return;                 // a newer seek superseded us
    this.unlockPending = false;                     // real audio replaces the unlock silence
    this.srcGen = myGen;
    this.audio.src = res.audioUrl;
    this.audio.playbackRate = this.speed;
    await new Promise(r => this.audio.addEventListener("loadedmetadata", r, { once: true }));
    if (myGen !== this.gen) return;
    this.audio.currentTime = Math.min(localT || 0, Math.max(0, (this.audio.duration || 0) - 0.03));
    if (autoplay) {
      try {
        await this.audio.play();
        if (!this.firstAudioReported) {
          this.firstAudioReported = true;
          const ms = performance.now() - this.speakT0;
          this._log(`>>> TIME-TO-FIRST-AUDIO: ${(ms / 1000).toFixed(2)}s`);
          if (this.opts.onFirstAudio) this.opts.onFirstAudio(ms);
        }
      } catch (e) {
        this._status("playback blocked by browser — press ▶");
        this._log(`play failed: ${e.message}`);
      }
    }
  }

  seek(T, forcePlay) {
    const resume = forcePlay || !this.audio.paused;
    T = Math.max(0, Math.min(T, this.totalKnown()));
    const o = this.offsets();
    let c = 0;
    while (c + 1 < this.job.n && o[c + 1] <= T) c++;
    this.gotoChunk(c, T - o[c], resume);
  }
  skip(d) { if (this.job) this.seek(this.globalTime() + d); }

  togglePlay() {
    if (!this.job) return;
    if (!this.audio.src) { this.gotoChunk(this.curIdx, 0, true); return; }
    this.audio.paused ? this.audio.play() : this.audio.pause();
  }

  stop() {
    this.gen++;
    this.source.cancel();
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    this.curIdx = 0;
    this.el.scrub.value = 0;
    this.el.time.textContent = `0:00 / ${fmt(this.job ? this.totalKnown() : 0)}`;
    this.el.pane.querySelectorAll(".w.cur").forEach(e => e.classList.remove("cur"));
    if (this._curRibbonWord) { this._curRibbonWord.classList.remove("cur"); this._curRibbonWord = null; }
  }

  /* ---- speak ---- */
  speak(text) {
    text = (text || "").trim();
    if (!text) { this._status("nothing to speak"); return; }

    // Unlock the audio element inside the tap gesture: synthesis finishes long
    // after the tap, and iOS Safari rejects play() calls that aren't
    // gesture-adjacent.
    this.unlockPending = true;
    this.audio.src = silence(1200, 24000);
    this.audio.play().catch(() => {});

    this.gen++;
    this._curWord = null;
    this.firstAudioReported = false;

    const md = this.opts.getMd ? this.opts.getMd() : false;
    const { html, chunks } = this.opts.prepare(text, md);   // chunks: array of word arrays
    if (!chunks.length) { this._status("no speakable text"); return; }
    const texts = chunks.map(w => w.join(" "));

    const id = Date.now();
    this.job = {
      id,
      n: texts.length,
      texts,
      chunks: chunks.map((w, i) => ({ i, words: w })),
      durations: new Array(texts.length).fill(null),
    };

    this._log(`\nspeak: ${text.length} chars → ${texts.length} chunks (first=${texts[0].length} chars)`);
    this._renderReader(html);
    this.el.scrub.max = 0;
    this.speakT0 = performance.now();

    this.source.cancel();
    this.source.begin(this.job);
    for (let i = 0; i < texts.length; i++) this._pull(i);   // eager: fill the timeline as chunks arrive

    this.curIdx = 0;
    this.gotoChunk(0, 0, true);
  }

  // Await a chunk's result and fold its duration / real timings into the
  // timeline. Memoized in the source, so this shares gotoChunk's promise.
  // Guard on JOB IDENTITY, not gen: gotoChunk bumps gen within the same speak(),
  // so a gen check would wrongly treat this fill as superseded and drop the real
  // timings (leaving the estimate). A new speak() swaps this.job, which is the
  // real supersession signal.
  _pull(i) {
    const myJob = this.job;
    return this.source.synthChunk(i).then(res => {
      if (this.job !== myJob) return res;
      this.job.durations[i] = res.duration;
      if (res.wordTimings) this.job.chunks[i]._timings = res.wordTimings;
      this.el.scrub.max = this.totalKnown() || 0;
      this.el.time.textContent = `${fmt(this.globalTime())} / ${fmt(this.totalKnown())}`;
      return res;
    });
  }

  destroy() {
    this.stop();
    this._stopRibbon();
    this.audio.onplay = this.audio.onpause = this.audio.onended = this.audio.ontimeupdate = null;
    this.root.innerHTML = "";
  }
}
