// node --test: the teaching board's hold-to-ask logic (S16): voice vs typed, what is sent, what may be
// shown from an answer, and what Space means in each phase. Pure logic only (board/lib/ask.js).
const test = require('node:test');
const assert = require('node:assert/strict');

const fs = require('node:fs');
const path = require('node:path');

const A = require('../board/lib/ask.js');

test('voice needs a real server with a key; rehearsal and keyless runs type instead', () => {
  assert.equal(A.mode({ demo: false, has_key: true }), 'voice');
  assert.equal(A.mode({ demo: true, has_key: true }), 'typed');
  assert.equal(A.mode({ demo: false, has_key: false }), 'typed');
  assert.equal(A.mode(null), 'typed');
});

test('the ask body carries the saved name, the current step and a trimmed question', () => {
  assert.deepEqual(A.askBody('solve-x2-5x-6', 3.9, '  why   those numbers? '),
                   { name: 'solve-x2-5x-6', step: 3, question: 'why those numbers?' });
  assert.equal(A.askBody('', 3, 'why?'), null);
  assert.equal(A.askBody('solve-x2', 3, '   '), null);
  assert.equal(A.askBody('solve-x2', -2, 'why?').step, 0);
});

test('only a spoken line and up to 3 known actions may be shown; verified is the server\'s word', () => {
  const shown = A.showable({ spoken: ' Because… ', related: true, actions: [
    { do: 'write_math', latex: 'x = 2', verified: true },
    { do: 'write_math', latex: 'x = 9' },                      // no verified from the server: never a ✓
    { do: 'teleport' },
    { do: 'highlight', target: 'eq1' },
    { do: 'highlight', target: 'eq2' }] });
  assert.equal(shown.spoken, 'Because…');
  assert.deepEqual(shown.actions.map((a) => a.do), ['write_math', 'write_math', 'highlight']);
  assert.deepEqual(shown.actions.map((a) => a.verified), [true, false, undefined]);
  assert.equal(A.showable({ spoken: ' ' }), null);
  assert.equal(A.showable(null), null);
  assert.deepEqual(A.showable({ spoken: 'That is off topic.', related: false, actions: [] }),
                   { spoken: 'That is off topic.', related: false, actions: [] });
});

test('the answer is shown once: the answer box, never the caption line', () => {
  // Regression guard (the e2e checks the rendered board too): speaking an answer must not write it
  // into the caption, which carries the lesson's lines and the live partial transcript while asking.
  const board = fs.readFileSync(path.join(__dirname, '..', 'board', 'board.js'), 'utf8');
  const answer = board.slice(board.indexOf('async function showAnswer'), board.indexOf('function resumeLesson'));
  assert.ok(answer.includes("$('answer-text').textContent = ans.spoken"));
  assert.ok(!answer.includes("$('caption')"), 'showAnswer must not copy the answer into the caption');
  assert.ok(answer.includes('speak(ans.spoken, gen, false)'), 'the spoken answer skips the caption');
  const speak = board.slice(board.indexOf('function speak('), board.indexOf('// ---- asking'));
  assert.ok(speak.includes('if (showCaption)'), 'speak only writes the caption when asked to');
});

test('Space holds to ask, releases to send, and taps to resume after an answer', () => {
  assert.equal(A.space('idle', 'down'), 'start');
  assert.equal(A.space('asking', 'down'), 'none');
  assert.equal(A.space('asking', 'up'), 'stop');
  assert.equal(A.space('thinking', 'down'), 'none');
  assert.equal(A.space('thinking', 'up'), 'none');
  assert.equal(A.space('answered', 'down'), 'resume');
  assert.equal(A.space('answered', 'up'), 'none');
  assert.equal(A.space('idle', 'up'), 'none');
});
