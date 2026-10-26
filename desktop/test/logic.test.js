// node --test: the desktop's pure logic (no Electron needed).
const test = require('node:test');
const assert = require('node:assert/strict');

const S = require('../renderer/lib/state.js');
const X = require('../renderer/lib/expression.js');
const C = require('../renderer/lib/cards.js');
const A = require('../renderer/lib/audit.js');
const W = require('../renderer/lib/widgets.js');
const P = require('../renderer/lib/sprite.js');

const run = (events) => events.reduce(S.reduce, S.initial());

const approval = {
  id: 'ab12cd34', nonce: 'n0nce', action: 'submit_form', payload_hash: 'f'.repeat(64), created_at: 100,
  expires_at: 700, tainted: true,
  summary: "Submit 'Hackathon Reimbursement Request' (local demo): Full name: Asha Rao; Amount: 2400. " +
           'Left for you: CVV, PAN (tax ID).  [after reading untrusted content]',
};

test('reducer follows a task from queue to verified done, keeping badges', () => {
  const s = run([
    { type: 'hello', pending: [], ts: 1 },
    { type: 'queued', task_id: 7, goal: 'fill the form', ts: 2 },
    { type: 'task', id: 7, goal: 'fill the form', task_id: 7, ts: 3 },
    { type: 'usage', badge: 'Muse Spark', cost_usd: 0.002, input_tokens: 900, output_tokens: 50, task_id: 7, ts: 4 },
    { type: 'form', phase: 'step', action: 'click (412, 315)', badge: 'Muse Wonder', task_id: 7, ts: 5 },
    { type: 'approval', event: 'requested', approval, task_id: 7, ts: 6 },
    { type: 'approval', event: 'decided', approval: { id: approval.id }, task_id: 7, ts: 7 },
    { type: 'tool', name: 'fill_form', args: {}, result: { verified: true, done: true }, badge: 'Muse Wonder', task_id: 7, ts: 8 },
    { type: 'done', id: 7, status: 'done', summary: 'submitted', reply: 'Done. Obviously.', cost_usd: 0.05, task_id: 7, ts: 9 },
  ]);
  const t = s.tasks[7];
  assert.equal(t.status, 'done');
  assert.equal(t.verified, true);
  assert.deepEqual(t.steps.map((x) => x.badge), ['Muse Spark', 'Muse Wonder', 'Muse Wonder']);
  assert.equal(t.steps[0].label, 'Thinking');
  assert.equal(t.steps[2].label, 'Form submitted and verified ✓');
  assert.equal(s.current, null);
  assert.deepEqual(s.approvals, {});
  assert.equal(s.usage.byBadge['Muse Spark'].calls, 1);
  assert.deepEqual(s.lastDone, { id: 7, status: 'done', verified: true, at: 9 });
});

test('reducer does not mutate its input and caps history', () => {
  const s0 = S.initial();
  const s1 = S.reduce(s0, { type: 'queued', task_id: 1, goal: 'x', ts: 1 });
  assert.deepEqual(s0.tasks, {});
  let s = s1;
  for (let i = 2; i < 50; i++) s = S.reduce(s, { type: 'queued', task_id: i, goal: 'g', ts: i });
  assert.equal(s.order.length, 30);
  assert.equal(S.recentTasks(s, 3)[0].id, 49);
});

test('stop clears approvals; hello restores pending ones', () => {
  let s = run([{ type: 'approval', event: 'requested', approval, ts: 1 }, { type: 'stopped', ts: 2 }]);
  assert.deepEqual(s.approvals, {});
  s = S.reduce(s, { type: 'hello', pending: [approval], ts: 3 });
  assert.equal(S.pendingList(s)[0].id, approval.id);
});

test('expressions follow what Lantern is doing', () => {
  const base = S.initial();
  assert.equal(X.pick(base, 10), 'idle');
  assert.equal(X.pick(run([{ type: 'task', id: 1, goal: 'g', ts: 1 }]), 2), 'thinking');
  assert.equal(X.pick(run([{ type: 'approval', event: 'requested', approval, ts: 1 }]), 2), 'needs-approval');
  const verified = run([{ type: 'tool', name: 'fill_form', result: { verified: true }, task_id: 1, ts: 1 },
                        { type: 'done', id: 1, status: 'done', reply: 'Done.', ts: 2 }]);
  assert.equal(X.pick(verified, 3), 'celebrating');
  assert.equal(X.pick(verified, 30), 'idle');
  const plain = run([{ type: 'done', id: 1, status: 'done', reply: 'Hi.', ts: 2 }]);
  assert.equal(X.pick(plain, 3), 'smug');
  assert.equal(X.pick(run([{ type: 'done', id: 1, status: 'failed', ts: 2 }]), 3), 'annoyed');
  assert.equal(X.pick(run([{ type: 'stopped', ts: 2 }]), 3), 'annoyed');
  assert.equal(X.pick(plain, 2 + 6 * 60), 'sleeping');
  assert.equal(X.bubble(verified, 3), 'Done.');
  assert.ok(X.EXPRESSIONS.every((e) => P.svg(e).startsWith('<svg') && P.svg(e).includes(`is ${e}`)));
});

test('approval card shows what matters and builds the decision', () => {
  const c = C.card(approval, 100);
  assert.equal(c.title, 'Submit this form?');
  assert.equal(c.allowLabel, 'Allow Submit');
  assert.equal(c.untrusted, true);
  assert.ok(!c.summary.includes('untrusted content'));
  assert.deepEqual(c.leftForYou, ['CVV', 'PAN (tax ID)']);
  assert.ok(!c.summary.includes('Left for you'));
  assert.equal(c.expiresIn, '10:00');
  assert.equal(C.card(approval, 701).expired, true);
  assert.deepEqual(C.decision(approval, true), { nonce: 'n0nce', approve: true, expected_hash: 'f'.repeat(64) });
  const email = C.card({ ...approval, action: 'send_email', tainted: false,
    summary: 'Send email to x@evil.test: “Files”. Body (8 chars): attached ⚠️ x@evil.test appeared only in a file/web page, not in your request' }, 100);
  assert.equal(email.title, 'Send this email?');
  assert.equal(email.warnings.length, 1);
  assert.ok(email.warnings[0].startsWith('⚠️'));
});

test('audit view: plain words for what left the laptop', () => {
  const v = A.view({ summary: {
    cloud: { calls: 3, bytes: 245760, by_model: { 'muse-spark-1.3': { calls: 2, bytes: 5120 }, whatsapp: { calls: 1, bytes: 240640 } },
             redactions: { aws_access_key: 1 } },
    local: { calls: 2, bytes: 9000 }, selfhosted: { calls: 0, bytes: 0 } } });
  assert.equal(v.headline, '240 KB left this laptop today, in 3 calls');
  assert.deepEqual(v.rows.map((r) => r.name), ['WhatsApp messages', 'Muse Spark']);
  assert.deepEqual(v.redactions, ['aws access key ×1']);
  assert.equal(v.privateLine, '2 private questions answered on this computer · 8.8 KB stayed here');
  assert.equal(A.view(null).headline, 'Nothing has left this laptop today');
  assert.equal(A.money(0.0042), '$0.0042');
});

test('widget bridge allows only notify and storage', () => {
  assert.equal(W.SANDBOX, 'allow-scripts');
  assert.ok(!W.SANDBOX.includes('same-origin'));
  assert.ok(W.CSP.startsWith("default-src 'none'"));
  assert.deepEqual(W.check({ type: 'notify', text: 'Drink some water? Kidding. Timer done.' }).ok, true);
  for (const bad of [null, 'x', { type: 'fetch', url: 'https://x' }, { type: 'storage.get', key: '../etc' },
                     { type: 'notify', text: '' }, { type: 'storage.set', key: 'k', value: 'x'.repeat(5000) }]) {
    assert.equal(W.check(bad).ok, false);
  }
  const mem = new Map();
  const store = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v) };
  const a = W.storage(store, 'water'); const b = W.storage(store, 'other');
  a.set('count', 3);
  assert.equal(a.get('count'), 3);
  assert.equal(b.get('count'), null);
});

test('forget removes a task, or everything', () => {
  let s = run([{ type: 'queued', task_id: 1, goal: 'a', ts: 1 }, { type: 'queued', task_id: 2, goal: 'b', ts: 2 },
               { type: 'usage', badge: 'Muse Spark', cost_usd: 0.01, task_id: 2, ts: 3 }]);
  s = S.reduce(s, { type: 'forgot', task_id: 1, ts: 4 });
  assert.deepEqual(s.order, [2]);
  assert.equal(s.tasks[1], undefined);
  s = S.reduce(s, { type: 'forgot', everything: true, ts: 5 });
  assert.deepEqual(s.order, []);
  assert.equal(s.usage.total, 0);
});

test('a camera check-in shows its reaction for a few seconds', () => {
  const s = run([{ type: 'checkin', concept: 'coffee mug', found: 1, reaction: 'Ah, coffee. Good.', ts: 10 }]);
  assert.equal(X.bubble(s, 12), 'Ah, coffee. Good.');
  assert.equal(X.pick(s, 12), 'smug');
  assert.equal(X.bubble(s, 30), '');
});

test('retries show in the bubble while a task runs', () => {
  const s = run([{ type: 'task', id: 1, goal: 'g', ts: 1 }, { type: 'retry', wait_s: 2, error: 'no internet', ts: 2 }]);
  assert.match(X.bubble(s, 3), /wobbly/);
  assert.equal(X.bubble(s, 10), 'Working on it…');
});

test('a reminder shows in the bubble, then fades; tool steps read plainly', () => {
  const s = run([{ type: 'reminder', id: 3, text: 'submit the video', late: false, ts: 100 }]);
  assert.equal(X.bubble(s, 105), '⏰ submit the video');
  assert.equal(X.bubble(s, 200), '');
  const t = run([{ type: 'tool', name: 'set_reminder', args: {}, result: { verified: true, due: 'today 18:00' }, task_id: 1, ts: 1 }]);
  assert.equal(t.tasks[1].steps[0].label, 'Reminder set for today 18:00 ✓');
  assert.equal(t.tasks[1].verified, true);
});
