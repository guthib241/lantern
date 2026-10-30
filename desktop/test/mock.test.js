// node --test: the mock server speaks the same protocol the app expects (REST + websocket events).
const test = require('node:test');
const assert = require('node:assert/strict');
const { start, frame } = require('../mock/server.js');
const { Api } = require('../renderer/lib/api.js');
const S = require('../renderer/lib/state.js');

test('websocket frames use the right length encoding', () => {
  assert.deepEqual([...frame('hi').subarray(0, 2)], [0x81, 2]);
  assert.equal(frame('x'.repeat(200))[1], 126);
  assert.equal(frame('x'.repeat(70000))[1], 127);
});

test('mock plays the form story: approval card, allow, verified done', async (t) => {
  const m = await start(0);
  t.after(() => m.close());
  const api = new Api(m.url, m.token);
  assert.equal((await api.get('/health')).channels[0], 'fake');
  await assert.rejects(new Api(m.url, 'wrong').get('/tasks'), /X-Lantern-Token/);

  let state = S.initial();
  const seen = [];
  const done = new Promise((resolve) => {
    const stop = api.events((e) => {
      seen.push(e.type);
      state = S.reduce(state, e);
      if (e.type === 'approval' && e.event === 'requested') {
        api.post(`/approvals/${e.approval.id}`, { nonce: e.approval.nonce, approve: true, expected_hash: e.approval.payload_hash });
      }
      if (e.type === 'done') { stop(); resolve(); }
    });
  });
  await new Promise((r) => setTimeout(r, 100));
  const { task_id } = await api.post('/task', { goal: 'fill the hackathon reimbursement form' });
  await done;
  const task = state.tasks[task_id];
  assert.equal(task.status, 'done');
  assert.equal(task.verified, true);
  assert.ok(task.steps.some((s) => s.badge === 'Muse Wonder'));
  assert.ok(seen.includes('approval'));
  const phone = (await api.get('/channel/messages')).messages;
  assert.ok(phone.some((msg) => (msg.buttons || []).some((b) => b.title === '✅ Approve')));
  const audit = await api.get('/audit');
  assert.ok(audit.summary.cloud.by_model['muse-wonder-1.0'].calls >= 2);
});

test('mock phone: STOP denies pending approvals', async (t) => {
  const m = await start(0);
  t.after(() => m.close());
  const api = new Api(m.url, m.token);
  await api.post('/channel/fake', { text: 'fill the reimbursement form' });
  for (let i = 0; i < 60 && !(await api.get('/approvals')).length; i++) await new Promise((r) => setTimeout(r, 100));
  assert.equal((await api.get('/approvals')).length, 1);
  await api.post('/channel/fake', { text: 'STOP' });
  assert.equal((await api.get('/approvals')).length, 0);
  const texts = (await api.get('/channel/messages')).messages.map((x) => x.text);
  assert.ok(texts.some((x) => x.startsWith('🛑 Stopped')));
});

test('mock reminders: set, listed, cancel', async (t) => {
  const m = await start(0);
  t.after(() => m.close());
  const api = new Api(m.url, m.token);
  await api.post('/task', { goal: 'remind me to send the video' });
  let list = [];
  for (let i = 0; i < 40 && !list.length; i++) { await new Promise((r) => setTimeout(r, 50)); list = await api.get('/reminders'); }
  assert.equal(list[0].text, 'send the video');
  assert.deepEqual(await api.post(`/reminders/${list[0].id}/cancel`), { cancelled: list[0].id });
  assert.equal((await api.get('/reminders')).length, 0);
  await assert.rejects(api.post(`/reminders/${list[0].id}/cancel`), /pending/);
});

test('mock avatar studio: preview validation, accept, manifest', async (t) => {
  const m = await start(0);
  t.after(() => m.close());
  const api = new Api(m.url, m.token);
  assert.deepEqual(await api.get('/avatar/manifest'), { active: null, expressions: {}, designs: [] });
  await assert.rejects(api.post('/avatar/design', { brief: '  ', style: '3d' }), /few words/);
  await assert.rejects(api.post('/avatar/design', { brief: 'a robot', style: 'watercolor' }), /style/);
  const d = await api.post('/avatar/design', { brief: 'a brass lantern robot', style: 'neon' });
  assert.equal(d.id, 'mock-design');
  assert.equal(d.cost_usd, 0.01);
  const r = await api.post('/avatar/design/mock-design/accept');
  assert.deepEqual([r.accepted, r.verified], [true, true]);
});

test('mock agents: list, run, disable, export, draft', async (t) => {
  const m = await start(0);
  t.after(() => m.close());
  const api = new Api(m.url, m.token);
  const list = await api.get('/agents');
  assert.ok(list.agents.some((a) => a.name === 'tidy-downloads'));
  const one = await api.get('/agents/tidy-downloads');
  assert.equal(one.title, 'Tidy Downloads');
  const { task_id } = await api.post('/agents/tidy-downloads/run', { inputs: { folder: 'Downloads' } });
  assert.equal(typeof task_id, 'number');
  assert.deepEqual(await api.post('/agents/hello-agent/disable'), { name: 'hello-agent', disabled: true });
  await assert.rejects(api.post('/agents/hello-agent/run', { inputs: {} }), /disabled/);
  assert.deepEqual(await api.post('/agents/hello-agent/enable'), { name: 'hello-agent', disabled: false });
  const exported = await api.raw('/agents/tidy-downloads/export');
  assert.equal(JSON.parse(exported).name, 'tidy-downloads');
  const draft = await api.post('/agents/draft', { task_id });
  assert.equal(draft.name, 'tidy-downloads');
  await assert.rejects(api.get('/agents/no-such-agent'), /no agent/);
});
