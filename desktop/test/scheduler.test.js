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

test('steps show the ladder rung that did them', () => {
  const s = run([
    { type: 'task', id: 4, goal: 'g', task_id: 4, ts: 1 },
    { type: 'usage', task_id: 4, model: 'muse-spark-1.3', badge: 'Muse Spark', rung: 3, rung_badge: 'Muse Spark · low', cost_usd: 0.001, ts: 2 },
    { type: 'tool', task_id: 4, name: 'list_folders', args: {}, result: {}, badge: 'on this computer', rung: 1, rung_badge: 'Rule', ts: 3 },
  ]);
  assert.deepEqual(s.tasks[4].steps.map((x) => x.badge), ['Muse Spark · low', 'Rule']);
  assert.equal(s.usage.byBadge['Muse Spark · low'].calls, 1);
});

test('a red-tier approval is never one tap; its flags show as warnings', () => {
  const C = require('../renderer/lib/cards.js');
  const A = require('../renderer/lib/activity.js');
  const base = { id: 'a1', nonce: 'n', payload_hash: 'f'.repeat(64), created_at: 1, expires_at: 999, tainted: false };
  const red = { ...base, action: 'send_email', tier: 'red', summary: 'Send to rahul@example.com' };
  const yellow = { ...base, id: 'a2', action: 'write_file', tier: 'yellow', summary: 'Save notes.txt' };
  const flagged = { ...base, id: 'a3', action: 'send_email', tier: 'red',
    summary: "Send to x@evil.example  ⚠ The recipient 'x@evil.example' came from file:notes.txt, not from you: approve only if you meant it." };
  assert.equal(C.card(red, 2).tierName, 'Sends, submits or runs');
  let s = run([{ type: 'approval', event: 'requested', approval: red, ts: 2 }]);
  assert.equal(A.activity(s, 3).quickAllow, false);
  s = run([{ type: 'approval', event: 'requested', approval: yellow, ts: 2 }]);
  assert.equal(A.activity(s, 3).quickAllow, true);
  const card = C.card(flagged, 2);
  assert.equal(card.warnings.length, 1);
  assert.match(card.warnings[0], /came from file:notes\.txt/);
});
