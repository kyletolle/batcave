// Port of local-tts chunker.py — split text into speak-sized chunks on
// paragraph, then sentence, boundaries. The first chunk is intentionally small
// so playback can start almost immediately; later chunks grow to reduce the
// number of synth calls.
//
// Ramped targets (spike finding, 2026-07-02): jumping straight from a small
// first chunk to 400 chars left an audible gap — chunk 1's playback (~4s)
// ended before chunk 2's synthesis (~10s) finished. Ramping 140→260→400 lets
// the audio buffer build ahead of the playhead.

export const TARGET_RAMP = [140, 260];
export const TARGET = 400;
const HARD_MAX = 700;

const PARA = /\n\s*\n/;
const SENT = /(?<=[.!?])\s+(?=["'([]?[A-Z0-9])/;
const WS = /[ \t]+/g;

function clean(s) {
  return s.replace(WS, " ").trim();
}

function sentences(paragraph) {
  const out = [];
  for (let p of paragraph.split(SENT)) {
    p = p.trim();
    if (!p) continue;
    // A single monster sentence (no terminal punctuation) still has to break
    // somewhere — fall back to the last space before HARD_MAX.
    while (p.length > HARD_MAX) {
      let cut = p.lastIndexOf(" ", HARD_MAX);
      if (cut <= 0) cut = HARD_MAX;
      out.push(p.slice(0, cut).trim());
      p = p.slice(cut).trim();
    }
    out.push(p);
  }
  return out;
}

export function chunkText(text) {
  text = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const sents = [];
  for (let para of text.split(PARA)) {
    para = clean(para);
    if (para) sents.push(...sentences(para));
  }

  const chunks = [];
  let buf = "";
  let ramp = 0;
  let target = TARGET_RAMP[0];
  for (const s of sents) {
    if (buf && buf.length + 1 + s.length > target) {
      chunks.push(buf);
      buf = s;
      ramp++;
      target = ramp < TARGET_RAMP.length ? TARGET_RAMP[ramp] : TARGET;
    } else {
      buf = `${buf} ${s}`.trim();
    }
  }
  if (buf) chunks.push(buf);
  return chunks;
}
