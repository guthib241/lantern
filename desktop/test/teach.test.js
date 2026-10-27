// node --test: teach once by demonstration (S8) in the panel: the "watching you" card and the start form's checks.
const test = require('node:test');
const assert = require('node:assert/strict');

const T = require('../renderer/lib/teach.js');

test('the card follows the recording and goes away when it stops', () => {
  let s = T.apply(null, { type: 'teach', phase: 'recording', task_id: 7, url: 'http://127.0.0.1:8770/apply/personal' });
  assert.deepEqual(T.card(s, 'apply for a city permit'), {
    title: 'Watching you',
    text: "apply for a city permit: do it once in Lantern's browser on 127.0.0.1:8770. Press Done after the confirmation page.",
    count: 'Nothing seen yet', notes: '' });
  s = [{ type: 'teach', phase: 'event', task_id: 7, kind: 'field', count: 1 },
       { type: 'teach', phase: 'event', task_id: 9, kind: 'field', count: 5 },     // another task: ignored
       { type: 'teach', phase: 'event', task_id: 7, kind: 'click', count: 2 },
       { type: 'teach', phase: 'note', task_id: 7 },
       { type: 'done', id: 7 }].reduce(T.apply, s);
  assert.equal(T.card(s, '').count, '2 steps seen');
  assert.equal(T.card(s, '').notes, '1 note');
  assert.match(T.card(s, '').text, /^Do it once/);
  assert.equal(T.apply(s, { type: 'teach', phase: 'stopped', task_id: 7 }), null);
  assert.equal(T.card(null, 'x'), null);
});

test('the start form needs a web address and a task', () => {
  assert.equal(T.check('http://127.0.0.1:8770/apply', 'apply for a permit'), '');
  assert.match(T.check('file:///etc/passwd', 'x'), /web address/);
  assert.match(T.check('https://portal.example', '  '), /what the task is/);
});

test('teach events never carry what was typed', () => {
  // The server sends only kind and count (core._teach_job); the card has nowhere to show a value.
  const s = T.apply(null, { type: 'teach', phase: 'recording', task_id: 1, url: 'https://x.example' });
  const after = T.apply(s, { type: 'teach', phase: 'event', task_id: 1, kind: 'field', count: 1, value: 'secret' });
  assert.equal(JSON.stringify(T.card(after, '')).includes('secret'), false);
});
