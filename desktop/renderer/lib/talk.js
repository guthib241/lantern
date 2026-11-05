/* Live talk (S15) in the panel: microphone frames → 16-bit PCM chunks for Lantern's /talk, and what each reply
   means. Pure: the page owns the microphone and the socket. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternTalk = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const RATE = 24000;                 // the realtime endpoint's native rate (PCM_24KHZ)
  const CHUNK = RATE * 80 / 1000;     // 80 ms per frame, as the docs' examples pace it

  /** Float samples in [-1, 1] → signed 16-bit little-endian bytes. */
  function toPcm16(floats) {
    const out = new Int16Array(floats.length);
    for (let i = 0; i < floats.length; i++) {
      const s = Math.max(-1, Math.min(1, floats[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return new Uint8Array(out.buffer);
  }

  /** Collects frames of any size and hands back whole 80 ms chunks (flush() returns what's left). */
  function chunker() {
    let buf = new Float32Array(0);
    return {
      push(frame) {
        const next = new Float32Array(buf.length + frame.length);
        next.set(buf);
        next.set(frame, buf.length);
        buf = next;
        const ready = [];
        while (buf.length >= CHUNK) {
          ready.push(toPcm16(buf.subarray(0, CHUNK)));
          buf = buf.slice(CHUNK);
        }
        return ready;
      },
      flush() {
        const rest = buf.length ? toPcm16(buf) : null;
        buf = new Float32Array(0);
        return rest;
      },
    };
  }

  function url(base, token) {
    return String(base || '').replace(/\/$/, '').replace(/^http/, 'ws') + '/talk?token=' + encodeURIComponent(token || '');
  }

  /** A server message → what the ask field shows: {live, final, error}. */
  function reading(msg) {
    if (!msg || typeof msg !== 'object') return {};
    if (msg.type === 'partial') return { live: msg.text || '' };
    if (msg.type === 'final' || msg.type === 'done') return { final: msg.text || '' };
    if (msg.type === 'error') return { error: msg.text || 'live talk stopped' };
    return {};
  }

  return { RATE, CHUNK, toPcm16, chunker, url, reading };
});
