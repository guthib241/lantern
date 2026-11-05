// node --test: live skins (the clock). Logic only.
const test = require('node:test');
const assert = require('node:assert/strict');

const K = require('../renderer/lib/skins.js');

const at = (h, m, s) => new Date(2026, 9, 30, h, m, s);

test('clock hands point at the real local time', () => {
  assert.deepEqual(K.clockAngles(at(3, 0, 0)), { second: 0, minute: 0, hour: 90 });
  assert.deepEqual(K.clockAngles(at(6, 30, 0)), { second: 0, minute: 180, hour: 195 });
  assert.deepEqual(K.clockAngles(at(0, 0, 45)), { second: 270, minute: 4.5, hour: 0 });
  assert.deepEqual(K.clockAngles(at(10, 9, 0)), { second: 0, minute: 54, hour: 304.5 });
});

test("Lantern's state tints the clock", () => {
  assert.deepEqual(K.clockStyle('working'), { ring: '#ffb340', pulse: true });    // the ring pulses
  assert.deepEqual(K.clockStyle('approval'), { ring: '#ff9f0a', pulse: false });  // waiting: amber
  assert.deepEqual(K.clockStyle('stopped'), { ring: '#8e8e93', pulse: false });   // stopped: grey
  for (const calm of ['idle', 'done', 'failed', 'reminder', 'retry']) {
    assert.deepEqual(K.clockStyle(calm), { ring: '#ffb340', pulse: false });
  }
});

test('the clock is our own SVG with no text inside', () => {
  const svg = K.clockSvg(at(10, 9, 30), K.clockStyle('working'));
  assert.ok(svg.startsWith('<svg'));
  assert.ok(svg.includes('class="clock-ring pulse"'));
  assert.ok(svg.includes('aria-label="Lantern clock"'));
  assert.ok(!svg.includes('<text'));
  const calm = K.clockSvg(at(10, 9, 30), K.clockStyle('idle'));
  assert.ok(calm.includes('class="clock-ring"') && !calm.includes('pulse'));
});

test('unknown skins fall back to the drawn face', () => {
  assert.deepEqual(K.SKINS, ['muse', 'orb', 'glow', 'clock']);
  assert.equal(K.normalize('orb'), 'orb');
  assert.equal(K.normalize('clock'), 'clock');
  assert.equal(K.normalize('glow'), 'glow');
  for (const bad of ['weather', '', null, undefined, 7]) assert.equal(K.normalize(bad), 'muse');
});

test('the glow and the orb share one palette, including the newer moods', () => {
  const P = require('../renderer/lib/sprite.js');
  for (const mood of ['idle', 'thinking', 'needs-approval', 'healed', 'listening']) {
    const svg = P.svg(mood);
    assert.ok(svg.startsWith('<svg') && svg.includes(P.PALETTES[mood].a), mood);
    assert.ok(!svg.includes('<text') && !svg.includes('<script'), mood);   // our own markup, no text in it
  }
  assert.ok(P.svg('no-such-mood').includes(P.PALETTES.idle.a));          // unknown moods glow like idle
});
