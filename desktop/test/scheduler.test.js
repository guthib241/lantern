// node --test: the desktop side of the scheduler (S1): parked tasks, questions, juggling.
const test = require('node:test');
const assert = require('node:assert/strict');

const S = require('../renderer/lib/state.js');
const X = require('../renderer/lib/expression.js');

const run = (events) => events.reduce(S.reduce, S.initial());

test('a parked task shows as parked with its question, and running again once answered', () => {
  let s = run([
    { type: 'hello', pending: [], questions: [], ts: 1 },
    { type: 'queued', task_id: 3, goal: 'fill the permit form', ts: 2 },
    { type: 'task', id: 3, goal: 'fill the permit form', task_id: 3, ts: 3 },
    { type: 'task_parked', task_id: 3, reason: 'waiting_user', question: 'Which permit type?', options: ['Home renovation'], ts: 4 },
    { type: 'questions', items: [{ task_id: 3, question: 'Which permit type?', options: ['Home renovation'], goal: 'fill', tainted: false }], ts: 4 },
  ]);
  assert.equal(s.tasks[3].status, 'parked');
  assert.equal(s.tasks[3].parked, 'waiting_user');
  assert.equal(s.tasks[3].steps.at(-1).label, 'Asked you: Which permit type?');
  assert.equal(s.current, null);
  assert.equal(X.pick(s, 5), 'needs-approval');
  assert.equal(X.bubble(s, 5, 'smug'), 'Task #3 asks: Which permit type?');
  s = [{ type: 'task_resumed', task_id: 3, reason: 'answered', ts: 6 }, { type: 'questions', items: [], ts: 6 }]
    .reduce(S.reduce, s);
  assert.equal(s.tasks[3].status, 'running');
  assert.equal(s.questions.length, 0);
  assert.notEqual(X.pick(s, 7), 'needs-approval');
});

test('the avatar juggles while more than one task is in flight', () => {
  const s = run([
    { type: 'hello', pending: [], ts: 1 },
    { type: 'task', id: 1, goal: 'a', task_id: 1, ts: 2 },
    { type: 'juggling', on: true, active: 3, running: [1, 2], queued: [], parked: [{ task_id: 3, reason: 'waiting_approval' }], ts: 3 },
  ]);
  assert.equal(X.pick(s, 4), 'juggling');
  assert.equal(X.bubble(s, 4, 'smug'), 'Juggling 3 tasks…');
  const calm = S.reduce(s, { type: 'juggling', on: false, active: 1, running: [1], queued: [], parked: [], ts: 5 });
  assert.equal(X.pick(calm, 6), 'thinking');
});

test('hello restores open questions after the panel reconnects', () => {
  const s = run([{ type: 'hello', pending: [], questions: [{ task_id: 9, question: 'Friday?', options: [] }], ts: 1 }]);
  assert.equal(s.questions.length, 1);
  assert.equal(X.pick(s, 2), 'needs-approval');
});
