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
    healed:           { a: '#d8ffe9', b: '#3ddc97', c: '#1fb6a6', glow: '#3ddc97', spin: 8 },    // a routine fixed itself
    listening:        { a: '#d6f1ff', b: '#4cc3ff', c: '#5b7cff', glow: '#4cc3ff', spin: 10 },   // push to talk is open
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
    healed: 'Lantern fixed a learned routine',
    listening: 'Lantern is listening',
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

  /** Build the orb inside `el` with DOM calls (no HTML parsing): the same structure as markup(). */
  function build(el) {
    const doc = el.ownerDocument;
    const div = (cls) => { const d = doc.createElement('div'); d.className = cls; return d; };
    const sphere = div('sphere');
    sphere.append(div('flow f1'), div('flow f2'), div('flow f3'), div('sheen'));
    const sats = ['s1', 's2', 's3'].map((s) => { const i = doc.createElement('i'); i.className = 'sat ' + s; return i; });
    el.replaceChildren(div('halo'), div('ring'), div('ring r2'), sphere, ...sats);
    return el;
  }

  /** The glow: a faceless radial light in the state's colours (the "glow" look, and the face's fallback until
   *  Lantern's Muse-made portraits exist). Our own trusted SVG, no user text inside. */
  function svg(expression) {
    const p = palette(expression);
    const name = LABELS[expression] || 'Lantern';
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 240" role="img" aria-label="${name}">
  <defs>
    <radialGradient id="g" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="${p.a}"/><stop offset="70%" stop-color="${p.b}" stop-opacity="0.85"/>
      <stop offset="100%" stop-color="${p.b}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <circle class="orb" cx="100" cy="130" r="92" fill="url(#g)"/>
</svg>`;
  }

  return { PALETTES, LABELS, palette, style, markup, build, svg };
});
