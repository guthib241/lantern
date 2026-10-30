/* Lantern's orb: a glass sphere with light moving inside it. No face, no character: colour and motion say
 * what Lantern is doing. This file only holds the palettes and the (trusted, text-free) markup; avatar.css
 * animates it. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternSprite = api;
})(typeof self !== 'undefined' ? self : this, function () {
  // Per state: three lights inside the sphere (a, b, c), the glow around it, and how fast the light moves
  // (seconds per turn of the slowest light; smaller is faster).
  const PALETTES = {
    idle:             { a: '#ffd479', b: '#ff9f43', c: '#ff6b6b', glow: '#ffb347', spin: 14 },
    thinking:         { a: '#ffe08a', b: '#ff8a3d', c: '#ff5fa2', glow: '#ff9f43', spin: 4.5 },
    juggling:         { a: '#ffe08a', b: '#ff8a3d', c: '#c86bff', glow: '#ff9f43', spin: 3.5 },
    smug:             { a: '#fff1b8', b: '#ffc53d', c: '#ff8a3d', glow: '#ffc53d', spin: 9 },
    celebrating:      { a: '#b9ffcf', b: '#34d399', c: '#22d3ee', glow: '#34d399', spin: 5 },
    'needs-approval': { a: '#a5c8ff', b: '#6d7bff', c: '#b06bff', glow: '#7c83ff', spin: 7 },
    annoyed:          { a: '#ffc2a8', b: '#ff6b5a', c: '#ff3b6b', glow: '#ff6b5a', spin: 6 },
    sleeping:         { a: '#d9d4ec', b: '#9a8fb0', c: '#6e7491', glow: '#9a8fb0', spin: 28 },
  };

  const LABELS = {
    idle: 'Lantern is ready',
    thinking: 'Lantern is working',
    juggling: 'Lantern is working on several tasks',
    smug: 'Lantern finished',
    celebrating: 'Lantern finished and checked it',
    'needs-approval': 'Lantern needs your OK',
    annoyed: 'Something went wrong',
    sleeping: 'Lantern is resting',
  };

  function palette(expression) {
    return PALETTES[expression] || PALETTES.idle;
  }

  /** CSS custom properties for a state, as a style string. */
  function style(expression) {
    const p = palette(expression);
    return `--a:${p.a};--b:${p.b};--c:${p.c};--glow:${p.glow};--spin:${p.spin}s`;
  }

  /** The orb's markup: built once; a state change only swaps the class and the custom properties. */
  function markup() {
    return '<div class="halo"></div><div class="ring"></div><div class="ring r2"></div>'
      + '<div class="sphere"><div class="flow f1"></div><div class="flow f2"></div><div class="flow f3"></div>'
      + '<div class="sheen"></div></div>'
      + '<i class="sat s1"></i><i class="sat s2"></i><i class="sat s3"></i>';
  }

  return { PALETTES, LABELS, palette, style, markup };
});
