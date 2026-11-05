/* Live skins for the avatar window: what Lantern looks like besides the drawn faces.
 * A skin is one of 'muse' (Muse Image portraits), 'orb' (a glass sphere with light inside),
 * 'glow' (the faceless fallback) or 'clock' (an analog clock showing the real local time). Pure.
 * Keep each skin one small function so new ones (e.g. a mood ring) slot in here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternSkins = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const SKINS = ['muse', 'orb', 'glow', 'clock'];

  /** Unknown names fall back to the drawn faces, so the window never goes blank. */
  function normalize(skin) {
    return SKINS.includes(skin) ? skin : 'muse';
  }

  /** A Date -> hand angles in degrees, clockwise from 12. */
  function clockAngles(date) {
    const s = date.getSeconds();
    const m = date.getMinutes();
    const h = date.getHours() % 12;
    return { second: s * 6, minute: m * 6 + s * 0.1, hour: h * 30 + m * 0.5 };
  }

  /** Lantern's activity mode -> how the clock ring looks. */
  function clockStyle(mode) {
    if (mode === 'approval') return { ring: '#ff9f0a', pulse: false };   // waiting for you: amber
    if (mode === 'stopped') return { ring: '#8e8e93', pulse: false };    // stopped: grey
    return { ring: '#ffb340', pulse: mode === 'working' };               // working: the ring pulses
  }

  /** An analog clock as our own SVG: face, ticks and three hands, no text inside. */
  function clockSvg(date, style) {
    const a = clockAngles(date);
    const ring = (style && style.ring) || '#ffb340';
    const pulse = style && style.pulse ? ' pulse' : '';
    let ticks = '';
    for (let i = 0; i < 12; i++) {
      const r = (i * 30 * Math.PI) / 180;
      const big = i % 3 === 0;
      const r1 = big ? 62 : 66, r2 = 72;
      ticks += `<line x1="${(100 + r1 * Math.sin(r)).toFixed(1)}" y1="${(130 - r1 * Math.cos(r)).toFixed(1)}" ` +
        `x2="${(100 + r2 * Math.sin(r)).toFixed(1)}" y2="${(130 - r2 * Math.cos(r)).toFixed(1)}" ` +
        `stroke="#fff" stroke-opacity="${big ? 0.9 : 0.4}" stroke-width="${big ? 4 : 2}" stroke-linecap="round"/>`;
    }
    const hand = (deg, len, width, color) =>
      `<line x1="100" y1="130" x2="100" y2="${130 - len}" stroke="${color}" ` +
      `stroke-width="${width}" stroke-linecap="round" transform="rotate(${deg} 100 130)"/>`;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 240" role="img" aria-label="Lantern clock">` +
      `<circle cx="100" cy="130" r="80" fill="#1b1b1f"/>` +
      `<circle class="clock-ring${pulse}" cx="100" cy="130" r="86" fill="none" stroke="${ring}" stroke-width="6"/>` +
      ticks + hand(a.hour, 40, 7, '#fff') + hand(a.minute, 58, 5, '#fff') + hand(a.second, 62, 2.5, ring) +
      `<circle cx="100" cy="130" r="5" fill="${ring}"/></svg>`;
  }

  return { SKINS, normalize, clockAngles, clockStyle, clockSvg };
});
