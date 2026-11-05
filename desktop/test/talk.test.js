// node --test: live talk's pure logic (S15).
const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../renderer/lib/talk.js');

test('float frames become 80 ms chunks of 16-bit little-endian PCM at 24 kHz', () => {
  assert.equal(T.CHUNK, 1920);
  const bytes = T.toPcm16(new Float32Array([0, 1, -1, 2, 0.5]));
  const view = new DataView(bytes.buffer);
  assert.deepEqual([0, 2, 4, 6, 8].map((o) => view.getInt16(o, true)), [0, 32767, -32768, 32767, 16383]);
  const c = T.chunker();
  assert.equal(c.push(new Float32Array(1000)).length, 0);
  const ready = c.push(new Float32Array(3000));            // 4000 samples: two whole chunks, 160 left
  assert.deepEqual(ready.map((r) => r.byteLength), [3840, 3840]);
  assert.equal(c.flush().byteLength, 320);
  assert.equal(c.flush(), null);
});

test('the socket URL carries the token, and replies read as live text, a final request or an error', () => {
  assert.equal(T.url('http://127.0.0.1:8765/', 'a b'), 'ws://127.0.0.1:8765/talk?token=a%20b');
  assert.deepEqual(T.reading({ type: 'partial', text: 'remind' }), { live: 'remind' });
  assert.deepEqual(T.reading({ type: 'done', text: 'Remind me at 6.' }), { final: 'Remind me at 6.' });
  assert.deepEqual(T.reading({ type: 'error', text: 'needs a key' }), { error: 'needs a key' });
  assert.deepEqual(T.reading({ type: 'speechStart' }), {});
});
