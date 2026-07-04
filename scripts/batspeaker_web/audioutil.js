// Shared audio helpers, used by both the ReadAlong player and its synth sources.
// Extracted from app.js's original module body (2026-07-03 refactor) so the
// player and the sources can share them without a circular import.

// Float32 PCM (kokoro's raw output) → a playable WAV blob URL.
export function encodeWav(f32, rate) {
  const len = f32.length;
  const buf = new ArrayBuffer(44 + len * 2);
  const v = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); v.setUint32(4, 36 + len * 2, true); ws(8, "WAVE");
  ws(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  ws(36, "data"); v.setUint32(40, len * 2, true);
  for (let i = 0; i < len; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
}

// base64 audio bytes (the cloud proxy's mp3) → blob URL.
export function b64ToUrl(b64, mime) {
  const bin = atob(b64);
  const a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return URL.createObjectURL(new Blob([a], { type: mime }));
}

// A short silent WAV. Two uses: unlock the audio element inside a tap gesture
// (iOS rejects play() calls that aren't gesture-adjacent), and stand in for a
// failed chunk so nothing downstream hangs waiting on its blob.
export function silence(samples = 4800, rate = 24000) {
  return encodeWav(new Float32Array(samples), rate);
}

export const fmt = s => {
  s = Math.max(0, s | 0);
  return `${(s / 60) | 0}:${String(s % 60).padStart(2, "0")}`;
};
