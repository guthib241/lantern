// A stand-in for Lantern's local server, for working on the desktop app without Python or an API key.
// Same routes and event shapes as lantern/server.py; plays a short scripted story per goal.
//   node mock/server.js            -> http://127.0.0.1:8766  (token: mock-token)
//   electron . --mock              -> starts this and points the app at it
const http = require('node:http');
const crypto = require('node:crypto');

const TOKEN = 'mock-token';
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

function createMock() {
  const sockets = new Set();
  const tasks = [];
  const approvals = new Map();   // id -> {approval, resolve}
  const phone = [];
  const usage = new Map();
  const audit = { cloud: { calls: 0, bytes: 0, by_model: {}, redactions: {} }, local: { calls: 0, bytes: 0 }, selfhosted: { calls: 0, bytes: 0 } };
  const zones = [
    { path: '/home/you/Documents', zone: 'writable' },
    { path: '/home/you/Private', zone: 'private' },
    { path: '/home/you/.ssh', zone: 'hidden' },
  ];
  let nextTask = 1;

  const now = () => Date.now() / 1000;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  function emit(event) {
    const data = frame(JSON.stringify({ ts: now(), ...event }));
    for (const s of sockets) s.write(data);
  }

  function spend(taskId, model, badge, cost, bytes) {
    const u = usage.get(model) || { model, calls: 0, input_tokens: 0, cached_tokens: 0, output_tokens: 0, cost_usd: 0 };
    u.calls += 1; u.input_tokens += 1200; u.output_tokens += 90; u.cost_usd += cost;
    usage.set(model, u);
    const m = audit.cloud.by_model[model] || { calls: 0, bytes: 0 };
    m.calls += 1; m.bytes += bytes;
    audit.cloud.by_model[model] = m;
    audit.cloud.calls += 1; audit.cloud.bytes += bytes;
    emit({ type: 'usage', model, badge, input_tokens: 1200, cached_tokens: 0, output_tokens: 90, cost_usd: cost, task_id: taskId });
  }

  function phoneSay(text, buttons) {
    phone.push(buttons ? { type: 'buttons', text, buttons, ts: now() } : { type: 'text', text, ts: now() });
  }

  function ask(taskId, action, summary, payload) {
    const id = crypto.randomBytes(4).toString('hex');
    const nonce = crypto.randomBytes(12).toString('base64url');
    const hash = crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex');
    const approval = { id, nonce, action, summary, payload, payload_hash: hash, created_at: now(), expires_at: now() + 600,
                       tainted: action === 'submit_form', status: 'pending' };
    emit({ type: 'approval', event: 'requested', approval, task_id: taskId });
    const h8 = hash.slice(0, 8);
    phoneSay(`🏮 Lantern needs your OK\n\n${summary}\n\n⏳ expires in 10 min · #${id}`,
      [{ id: `approve:${id}:${nonce}:${h8}`, title: '✅ Approve' }, { id: `deny:${id}:${nonce}:${h8}`, title: '❌ Deny' },
       { id: `show:${id}`, title: '🔎 Show' }]);
    return new Promise((resolve) => approvals.set(id, { approval, resolve }));
  }

  function decide(id, nonce, approve) {
    const item = approvals.get(id);
    if (!item || item.approval.nonce !== nonce) return null;
    approvals.delete(id);
    item.approval.status = approve ? 'approved' : 'denied';
    emit({ type: 'approval', event: 'decided', approval: { ...item.approval, nonce: undefined } });
    item.resolve(approve);
    return item.approval;
  }

  async function story(taskId, goal) {
    const g = goal.toLowerCase();
    emit({ type: 'task', id: taskId, goal, task_id: taskId });
    await wait(400);
    spend(taskId, 'muse-spark-1.3', 'Muse Spark', 0.0021, 6200);
    let reply = 'Done. Obviously.';
    let status = 'done';
    if (/form|reimburs/.test(g)) {
      emit({ type: 'form', phase: 'open', url: 'file:///demo/forms/reimbursement.html', task_id: taskId });
      await wait(500);
      emit({ type: 'form', phase: 'filled', filled: 9, left_for_you: [{ label: 'CVV' }, { label: 'PAN (tax ID)' }], badge: 'on this computer', task_id: taskId });
      for (const action of ['click (428, 313)', 'click (428, 392)']) {
        await wait(700);
        spend(taskId, 'muse-wonder-1.0', 'Muse Wonder', 0.0094, 160000);
        emit({ type: 'form', phase: 'step', action, badge: 'Muse Wonder', task_id: taskId });
      }
      await wait(400);
      const ok = await ask(taskId, 'submit_form',
        "Submit 'Hackathon Reimbursement Request' (local demo): Full name: Asha Rao; Email: asha.rao@example.com; " +
        'Expense category: Travel; Amount: 2400; Currency: INR; Purpose: Taxi to the venue. Left for you: Organiser portal password, ' +
        'One-time code sent to your phone, Card number for payout, CVV, PAN (tax ID).  [after reading untrusted content]',
        { url: 'file:///demo/forms/reimbursement.html', fields: 11 });
      await wait(500);
      const result = ok ? { done: true, verified: true, submitted: true, confirmation: { content: 'Request received · Reference REF-7F3A2C' } }
        : { done: false, verified: false, submitted: false, reason: 'user declined' };
      emit({ type: 'tool', name: 'fill_form', args: { url: 'hackathon reimbursement', goal }, result, badge: 'Muse Wonder', task_id: taskId });
      reply = ok ? 'Submitted and verified: reference REF-7F3A2C. I left the card, CVV and PAN for you, as one does.'
        : 'Fine. Nothing was submitted.';
      status = ok ? 'done' : 'failed';
    } else if (/private|salary|offer/.test(g)) {
      audit.local.calls += 1; audit.local.bytes += 2400;
      emit({ type: 'tool', name: 'ask_private', args: { path: '/home/you/Private/offer_letter.txt', question: goal },
             result: { answer: 'From the private file: base salary is listed in section 2.', model: 'Muse Glimmer (local)', file_bytes_to_cloud_model: 0 },
             badge: 'Muse Glimmer (local)', task_id: taskId });
      reply = 'Read it locally with Muse Glimmer. Zero bytes of that file left your laptop.';
    } else if (/torch|project/.test(g)) {
      emit({ type: 'tool', name: 'which_projects_use', args: { package: 'torch' }, result: { projects: ['lantern-vision', 'thesis'] },
             badge: 'on this computer', task_id: taskId });
      reply = 'Two projects use torch: lantern-vision and thesis. No model call needed for that one.';
    } else if (/key|secret/.test(g)) {
      audit.cloud.redactions.stripe_key = (audit.cloud.redactions.stripe_key || 0) + 1;
      reply = 'Found config.env. I redacted the Stripe key before anything left this laptop.';
    } else {
      reply = 'I take tasks on this computer: forms, files, emails, projects. Try “fill the hackathon reimbursement form”.';
    }
    await wait(300);
    spend(taskId, 'muse-spark-1.3', 'Muse Spark', 0.0012, 3100);
    const t = tasks.find((x) => x.id === taskId);
    Object.assign(t, { status, summary: reply.slice(0, 80), updated_at: now() });
    emit({ type: 'done', id: taskId, status, summary: t.summary, reply, cost_usd: 0.02, steps: 4, task_id: taskId });
    phoneSay(reply);
  }

  function submit(goal) {
    const id = nextTask++;
    tasks.push({ id, goal, status: 'active', summary: null, created_at: now(), updated_at: now(), usage: { calls: 0, cost_usd: 0 } });
    emit({ type: 'queued', task_id: id, goal });
    story(id, goal);
    return id;
  }

  function stop() {
    let denied = 0;
    for (const [id, item] of approvals) { decide(id, item.approval.nonce, false); denied += 1; }
    emit({ type: 'stopped', denied_approvals: denied });
    return { stopped: true, denied_approvals: denied };
  }

  function phoneIn(body) {
    if (body.text) {
      phone.push({ type: 'text', text: body.text, ts: now(), from_owner: true });
      if (/^\W*stop/i.test(body.text)) { const r = stop(); phoneSay(`🛑 Stopped. Denied ${r.denied_approvals} approval(s).`); return; }
      const id = submit(`[fake] ${body.text}`);
      phoneSay(`🏮 On it (task #${id}).`);
    } else if (body.button) {
      const [verb, id, nonce] = body.button.split(':');
      const shown = phone.flatMap((m) => m.buttons || []).find((b) => b.id === body.button);
      phone.push({ type: 'text', text: shown ? shown.title : '(button)', ts: now(), from_owner: true });
      if (verb === 'show') { const a = approvals.get(id); phoneSay(a ? `🔎 ${a.approval.action}\n${a.approval.summary}` : 'Not open any more.'); return; }
      const a = decide(id, nonce, verb === 'approve');
      phoneSay(a ? `${verb === 'approve' ? '✅ Approved' : '❌ Denied'}: ${a.summary.slice(0, 120)}` : "That approval isn't open any more.");
    }
  }

  const routes = {
    'GET /health': () => ({ ok: true, version: 'mock', model: 'muse-spark-1.3', badge: 'Muse Spark', has_key: false,
                            private_lane: 'Muse Glimmer (fake)', busy: false, channels: ['fake'], mock: true }),
    'POST /task': (b) => (b.goal && b.goal.trim() ? { task_id: submit(b.goal.trim()) } : [400, { detail: 'goal is empty' }]),
    'GET /tasks': () => tasks.slice().reverse(),
    'GET /approvals': () => [...approvals.values()].map((x) => x.approval),
    'POST /stop': () => stop(),
    'GET /audit': () => ({ summary: { since: 0, ...audit }, entries: [] }),
    'GET /zones': () => ({ zones, choices: ['hidden', 'private', 'open', 'writable'] }),
    'POST /zones': (b) => {
      const z = zones.find((x) => x.path === b.path);
      if (z) z.zone = b.zone; else zones.push({ path: b.path, zone: b.zone });
      return { zones };
    },
    'GET /usage': () => { const rows = [...usage.values()]; return { by_model: rows, total_usd: rows.reduce((a, r) => a + r.cost_usd, 0) }; },
    'GET /memory': () => ({ facts: { 'manager email': 'priya@example.com' } }),
    'GET /channel/messages': () => ({ owner: 'owner', messages: phone }),
    'POST /channel/fake': (b) => { phoneIn(b); return { ok: true }; },
    'GET /widgets': () => ({ widgets: [] }),
  };

  const server = http.createServer((req, res) => {
    const cors = { 'Access-Control-Allow-Origin': req.headers.origin || '*', 'Access-Control-Allow-Headers': 'X-Lantern-Token, Content-Type',
                   'Access-Control-Allow-Methods': 'GET, POST' };
    if (req.method === 'OPTIONS') { res.writeHead(204, cors); res.end(); return; }
    const url = new URL(req.url, 'http://127.0.0.1');
    const send = (status, data) => { res.writeHead(status, { 'Content-Type': 'application/json', ...cors }); res.end(JSON.stringify(data)); };
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const m = url.pathname.match(/^\/approvals\/([\w-]+)$/);
      const key = m ? `${req.method} /approvals/:id` : `${req.method} ${url.pathname}`;
      if (url.pathname !== '/health' && req.headers['x-lantern-token'] !== TOKEN) return send(401, { detail: 'missing or wrong X-Lantern-Token' });
      let parsed = {};
      try { parsed = body ? JSON.parse(body) : {}; } catch { return send(400, { detail: 'bad JSON' }); }
      if (key === 'POST /approvals/:id') {
        const a = decide(m[1], parsed.nonce, !!parsed.approve);
        return a ? send(200, a) : send(409, { detail: 'this approval link is not valid' });
      }
      const fn = routes[key];
      if (!fn) return send(404, { detail: 'not found' });
      const out = fn(parsed);
      return Array.isArray(out) && typeof out[0] === 'number' ? send(out[0], out[1]) : send(200, out);
    });
  });

  server.on('upgrade', (req, socket) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname !== '/events' || url.searchParams.get('token') !== TOKEN) { socket.destroy(); return; }
    const accept = crypto.createHash('sha1').update(req.headers['sec-websocket-key'] + WS_GUID).digest('base64');
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n' +
                 `Sec-WebSocket-Accept: ${accept}\r\n\r\n`);
    sockets.add(socket);
    socket.on('data', (buf) => { if ((buf[0] & 0x0f) === 0x8) socket.end(); });   // client sent close
    socket.on('close', () => sockets.delete(socket));
    socket.on('error', () => sockets.delete(socket));
    socket.write(frame(JSON.stringify({ type: 'hello', version: 'mock', pending: [...approvals.values()].map((x) => x.approval), ts: now() })));
  });

  return { server, submit, stop, phone, sockets };
}

/** A server-to-client text frame (no masking needed in this direction). */
function frame(text) {
  const payload = Buffer.from(text);
  let header;
  if (payload.length < 126) {
    header = Buffer.from([0x81, payload.length]);
  } else if (payload.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x81; header[1] = 126; header.writeUInt16BE(payload.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81; header[1] = 127; header.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  return Buffer.concat([header, payload]);
}

function start(port = 8766) {
  const mock = createMock();
  return new Promise((resolve) => {
    mock.server.listen(port, '127.0.0.1', () => {
      const actual = mock.server.address().port;
      const close = () => {
        for (const s of mock.sockets) s.destroy();
        mock.server.close();
      };
      resolve({ url: `http://127.0.0.1:${actual}`, token: TOKEN, close, mock });
    });
  });
}

module.exports = { start, TOKEN, frame };

if (require.main === module) {
  start(Number(process.env.LANTERN_MOCK_PORT || 8766)).then((m) => console.log(`🏮 mock Lantern on ${m.url} (token ${m.token})`));
}
