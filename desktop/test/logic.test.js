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
  assert.equal(C.card({ ...approval, action: 'install_agent',
    summary: "Install agent 'Tidy Downloads' by lantern (v1.0.0). It may use: organize_files. Full instructions: Tidy." }, 100).title,
    'Install this agent?');
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

test('interrupted tasks come with hello and leave when resumed', () => {
  let s = run([{ type: 'hello', pending: [], interrupted: [{ id: 4, goal: 'fill the form' }], ts: 1 },
               { type: 'queued', task_id: 4, goal: 'fill the form', ts: 1 }]);
  assert.deepEqual(s.interrupted, [{ id: 4, goal: 'fill the form' }]);
  s = S.reduce(s, { type: 'resumed', from: 4, task_id: 9, ts: 2 });
  assert.deepEqual(s.interrupted, []);
  assert.equal(s.tasks[4].status, 'resumed');
});

test('a task that moved files offers Undo until it is undone', () => {
  let s = run([{ type: 'task', id: 5, goal: 'tidy downloads', ts: 1 },
               { type: 'tool', name: 'list_dir', args: {}, result: { entries: [] }, task_id: 5, ts: 2 }]);
  assert.equal(s.tasks[5].undoable, false);
  s = S.reduce(s, { type: 'tool', name: 'organize_files', args: {}, result: { verified: true, moved: 3 }, task_id: 5, ts: 3 });
  s = S.reduce(s, { type: 'done', id: 5, status: 'done', summary: 'tidied', ts: 4 });
  assert.equal(s.tasks[5].undoable, true);
  assert.equal(s.tasks[5].undoneBy, null);
  s = [{ type: 'task', id: 6, goal: 'Undo task #5: put back 3 files', ts: 5 },
           { type: 'tool', name: 'undo_moves', args: { of_task: 5 }, result: { verified: true, moved: 2, already_back: 1 },
             task_id: 6, ts: 6 },
           { type: 'undone', from: 5, task_id: 6, moved: 2, ts: 6 }].reduce(S.reduce, s);
  assert.equal(s.tasks[5].undoneBy, 6);
  assert.equal(s.tasks[6].steps[0].label, 'Put 3 file(s) back, each checked ✓');
  const declined = run([{ type: 'tool', name: 'organize_files', args: {}, result: { done: false, reason: 'user declined' },
                          task_id: 7, ts: 1 }]);
  assert.equal(declined.tasks[7].undoable, false);
  assert.equal(C.card({ action: 'undo_moves', summary: 'Undo task #5: put back 3 files: 3 → Downloads', expires_at: 10 }, 0).title,
               'Put these files back?');
});

test('costs: spent against the limits Lantern enforces', () => {
  const T = require('../renderer/lib/today.js');
  const b = { spent_usd: 121.5, today_usd: 4, total_usd: 150, daily_usd: 10, task_usd: 3, left_usd: 28.5 };
  assert.deepEqual(T.budgetView(b, 121.5), { headline: '$121.50 of $150.00 spent', percent: 81,
    limits: 'Stops itself at $150.00 · $3.00 per task · $10.00 a day ($4.00 today).' });
  assert.equal(T.budgetView({ ...b, spent_usd: 400 }).percent, 100);
  assert.deepEqual(T.budgetView({ ...b, total_usd: null, left_usd: null }, 2),
    { headline: '$121.50 spent', percent: 0, limits: 'No total limit set (LANTERN_BUDGET_USD).' });
  assert.equal(T.budgetView(null, 2).headline, '$2.00 spent');
  const spend = (budget) => T.glance(S.initial(), { now: 1000, budget }).find((g) => g.id === 'spend').note;
  assert.equal(spend(b), '$28.50 left of $150.00');
  assert.equal(spend({ ...b, total_usd: null }), 'no spending limit set');
  assert.equal(spend(undefined), 'model calls only');
});

test('receipt: what it did, what you approved, what left, what it cost', () => {
  const R = require('../renderer/lib/receipt.js');
  const v = R.view({
    task: { id: 1, goal: 'Fill the form\n[Resuming…]', status: 'done' }, looked: 1,
    actions: [{ what: 'Submitted a form', verified: true, note: '' }, { what: 'Sent an email', verified: false, note: 'user declined' }],
    approvals: [{ asked: 'Submit the form', status: 'approved', where: 'on WhatsApp', used: true, payload_hash: 'ab'.repeat(32) }],
    sent: [{ lane: 'cloud', to: "Meta's API", simulated: false, kinds: ['model'], calls: 6, bytes: 283000, redactions: {} },
           { lane: 'local', to: '127.0.0.1', simulated: false, kinds: ['model'], calls: 1, bytes: 900, redactions: {} }],
    redactions: { aws_access_key: 1 }, cost_usd: 0.0412, models: [{ badge: 'Muse Spark', calls: 4 }, { badge: 'Muse Wonder', calls: 2 }],
  });
  assert.equal(v.title, 'Fill the form');
  assert.deepEqual(v.sections.map((s) => s.title), ['What it did', 'What you approved', 'What left this laptop', 'Cost']);
  assert.deepEqual(v.sections[0].rows.map((r) => [r.mark, r.text, r.detail]), [
    ['✓', 'Submitted a form', 'checked'], ['✗', 'Sent an email', 'user declined'],
    ['·', 'Looked something up', 'reading and searching; nothing changed']]);
  assert.equal(v.sections[1].rows[0].detail, `approved on WhatsApp · used once · bound to ${'ab'.repeat(6)}`);
  assert.deepEqual(v.sections[2].rows.map((r) => r.text), ["276 KB to Meta's API", '1 question to the private lane', 'Redacted before sending']);
  assert.equal(v.sections[2].rows[1].detail, 'stayed on this computer');
  assert.deepEqual(v.sections[3].rows[0], { tone: 'quiet', mark: '$', text: '$0.04', detail: 'Muse Spark ×4, Muse Wonder ×2' });
  const empty = R.view({ task: { goal: 'hi', status: 'done' }, looked: 0, actions: [], approvals: [], sent: [], redactions: {},
                         cost_usd: 0, models: [] });
  assert.deepEqual(empty.sections.map((s) => s.rows[0].text), ['Nothing changed', 'Nothing', '$0.00']);
});

test('setup check: failures first, fixes only where needed, a plain readiness line', () => {
  const Setup = require('../renderer/lib/setup.js');
  const v = Setup.view({
    checks: [{ id: 'python', title: 'Python', status: 'ok', detail: '3.12', fix: 'never shown' },
             { id: 'glimmer', title: 'Private lane', status: 'warn', detail: 'stand-in', fix: 'set the URL' },
             { id: 'key', title: 'Key', status: 'fail', detail: 'missing', fix: 'add it' }],
    ready: { demo: { blocked_by: ['key'], to_do: ['glimmer'] }, rehearsal: { blocked_by: [], to_do: [] } },
  });
  assert.deepEqual(v.rows.map((r) => r.id), ['key', 'glimmer', 'python']);
  assert.deepEqual(v.rows.map((r) => r.mark), ['✗', '!', '✓']);
  assert.equal(v.rows[2].fix, '');
  assert.equal(v.demo, 'Real demo: blocked by key · then glimmer');
  assert.equal(v.rehearsal, 'Rehearsal: ready ✓');
  assert.deepEqual(Setup.view(null).rows, []);
});

test('audit view is honest in rehearsal: nothing actually left', () => {
  const audit = { summary: { cloud: { calls: 2, bytes: 2048, by_model: {}, redactions: {} }, local: { calls: 0, bytes: 0 },
                             selfhosted: { calls: 0, bytes: 0 } } };
  assert.equal(A.view(audit).headline, '2.0 KB left this laptop today, in 2 calls');
  assert.equal(A.view(audit, { rehearsal: true }).headline,
    '2.0 KB would have left this laptop today, in 2 calls (rehearsal: nothing was sent)');
});

test('today: glance tiles and suggestions', () => {
  const T = require('../renderer/lib/today.js');
  const now = new Date(2026, 9, 27, 15, 0).getTime() / 1000;
  let s = run([{ type: 'task', id: 1, goal: 'g', ts: now - 60 },
               { type: 'tool', name: 'fill_form', result: { verified: true }, task_id: 1, ts: now - 50 },
               { type: 'done', id: 1, status: 'done', reply: 'ok', ts: now - 40 },
               { type: 'usage', badge: 'Muse Spark', cost_usd: 0.0123, task_id: 1, ts: now - 45 }]);
  let tiles = T.glance(s, { now, reminders: [{ due: 'today 18:00', text: 'send the video' }] });
  assert.deepEqual(tiles.map((t) => [t.id, t.value]), [['needs', '0'], ['done', '1'], ['next', 'today 18:00'], ['spend', '$0.01']]);
  assert.equal(tiles[1].note, '1 verified');
  s = S.reduce(s, { type: 'approval', event: 'requested', approval, ts: now });
  tiles = T.glance(s, { now });
  assert.equal(tiles[0].value, '1');
  assert.equal(tiles[0].tone, 'orange');
  assert.equal(tiles[2].value, '—');
  assert.equal(T.greeting(now), 'Good afternoon');
  assert.ok(T.SUGGESTIONS.length >= 4 && T.SUGGESTIONS.every((x) => x.label && x.goal));
  const yesterday = run([{ type: 'done', id: 9, status: 'done', ts: now - 86400 * 2 }]);
  assert.equal(T.glance(yesterday, { now })[1].value, '0');   // only today counts
});

test('form approvals read as a field list, and fall back to the raw text when unsure', () => {
  const ok = C.card({ ...approval, summary: "Submit 'Claim' (local demo): Full name: Asha Rao; Amount: 2400; Note: a: b. Left for you: CVV." }, 100);
  assert.equal(ok.lead, "Submit 'Claim' (local demo)");
  assert.deepEqual(ok.fields, [{ label: 'Full name', value: 'Asha Rao' }, { label: 'Amount', value: '2400' }, { label: 'Note: a', value: 'b' }]);
  assert.deepEqual(ok.leftForYou, ['CVV']);
  const odd = C.card({ ...approval, summary: "Submit 'Claim': Full name: Asha; just some text" }, 100);
  assert.equal(odd.fields, null);
  assert.ok(odd.summary.includes('just some text'));   // nothing hidden
  assert.equal(C.card({ ...approval, action: 'send_email', summary: 'Send email to a@b.c: “x”.' }, 100).fields, null);
});

test('live activity: what the pill says, and quick Allow only for clean approvals', () => {
  const L = require('../renderer/lib/activity.js');
  assert.equal(L.activity(S.initial(), 10).mode, 'idle');
  let s = run([{ type: 'task', id: 1, goal: 'fill the form', ts: 1 },
               { type: 'form', phase: 'step', action: 'click (412, 315)', badge: 'Muse Wonder', task_id: 1, ts: 2 }]);
  let a = L.activity(s, 3);
  assert.deepEqual([a.mode, a.text, a.badge, a.detail], ['working', 'Clicking at (412, 315)', 'Wonder', 'fill the form']);
  const clean = { ...approval, action: 'save_widget', tainted: false, summary: "Add widget 'water-timer' (4,402 bytes)." };
  a = L.activity(S.reduce(s, { type: 'approval', event: 'requested', approval: clean, ts: 4 }), 5);
  assert.equal(a.mode, 'approval');
  assert.equal(a.text, 'Add this widget?');
  assert.equal(a.quickAllow, true);
  a = L.activity(S.reduce(s, { type: 'approval', event: 'requested', approval, ts: 4 }), 5);   // tainted form submit
  assert.equal(a.quickAllow, false);
  assert.match(a.detail, /fields$/);
  const warned = { ...clean, summary: 'Send email to x@evil.test ⚠️ x@evil.test appeared only in a file' };
  assert.equal(L.activity(S.reduce(s, { type: 'approval', event: 'requested', approval: warned, ts: 4 }), 5).quickAllow, false);
  assert.equal(L.activity(S.reduce(s, { type: 'approval', event: 'requested', approval: clean, ts: 4 }), 800).quickAllow, false);   // expired
  s = S.reduce(s, { type: 'done', id: 1, status: 'done', reply: 'Submitted.', ts: 10 });
  assert.equal(L.activity(s, 12).mode, 'done');
  assert.equal(L.activity(s, 30).mode, 'idle');
  assert.equal(L.activity(S.reduce(s, { type: 'reminder', text: 'call Priya', ts: 40 }), 41).text, 'call Priya');
  assert.equal(L.activity(S.reduce(s, { type: 'stopped', ts: 50 }), 51).mode, 'stopped');
  assert.equal(L.friendly('type "Asha"'), 'Typing');
  assert.equal(L.shortBadge('on this computer'), 'Local');
});

test('quick ask rows: ask first, stop on "stop", approvals, filtered suggestions, recent', () => {
  const Q = require('../renderer/lib/quick.js');
  const T = require('../renderer/lib/today.js');
  const ctx = { suggestions: T.SUGGESTIONS, pending: 1, recent: [{ id: 3, goal: 'Tidy my Downloads folder.', status: 'done' }] };
  let r = Q.rows('', ctx);
  assert.deepEqual(r.map((x) => x.kind), ['approvals', 'suggestion', 'suggestion', 'suggestion', 'suggestion', 'suggestion', 'recent']);
  r = Q.rows('tidy', ctx);
  assert.equal(r[0].kind, 'ask');
  assert.equal(r[0].goal, 'tidy');
  assert.equal(r.find((x) => x.kind === 'suggestion').label, 'Tidy Downloads');
  assert.ok(!r.some((x) => x.kind === 'recent'));
  r = Q.rows('stop', ctx);
  assert.deepEqual(r.slice(0, 2).map((x) => x.kind), ['stop', 'ask']);
  assert.equal(Q.score('rem 20', 'Remind me in 20 min'), 3);
  assert.equal(Q.score('xyz', 'Tidy Downloads'), 0);
  assert.equal(Q.move(0, -1, 4), 3);
  assert.equal(Q.move(3, 1, 4), 0);
  assert.equal(Q.move(0, 1, 0), -1);
});

test('menu bar status, badge and terse notifications', () => {
  const N = require('../renderer/lib/native.js');
  assert.equal(N.status(S.initial(), 1, true).line, 'Idle');
  assert.match(N.status(S.initial(), 1, false).line, /^Offline/);
  let s = run([{ type: 'task', id: 1, goal: 'g', ts: 1 }, { type: 'form', phase: 'step', action: 'click (1, 2)', badge: 'Muse Wonder', task_id: 1, ts: 2 }]);
  assert.equal(N.status(s, 3, true).line, 'Working: Clicking at (1, 2)');
  const req = { type: 'approval', event: 'requested', approval, ts: 4 };
  s = S.reduce(s, req);
  assert.deepEqual([N.status(s, 5, true).line, N.status(s, 5, true).badge], ['1 approval waiting', 1]);
  const n = N.notice(req, s, { panelFocused: false, now: 5 });
  assert.equal(n.title, 'Lantern needs your OK');
  assert.equal(n.body, 'Submit this form? Review it in Lantern.');
  assert.ok(!n.body.includes('Asha'));   // never the values
  assert.equal(N.notice(req, s, { panelFocused: true }), null);
  assert.equal(N.notice({ type: 'reminder', text: 'call Priya', late: true }, s, { panelFocused: true }).body,
    'call Priya (it was due while Lantern was off)');
  assert.equal(N.notice({ type: 'done', status: 'failed', summary: 'no internet' }, s, {}).title, 'Lantern couldn’t finish');
  assert.equal(N.notice({ type: 'done', status: 'done', reply: 'ok' }, s, {}), null);
});

test('widget storage: no key can reach the index, and the cap holds', () => {
  const mem = new Map();
  const store = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v) };
  const s = W.storage(store, 'w');
  s.set('index', 5);           // a widget key named like the index is just data
  s.set('__keys', []);
  assert.equal(s.get('index'), 5);
  for (let i = 0; i < 48; i++) s.set(`k${i}`, i);
  assert.throws(() => s.set('one-too-many', 1), /full/);
  mem.set('lantern.widget.w.index', '5');   // a corrupted index can't break later writes
  s.set('again', 1);
  assert.equal(s.get('again'), 1);
});
