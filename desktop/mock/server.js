// A stand-in for Lantern's local server, for working on the desktop app without Python or an API key.
// Same routes and event shapes as lantern/server.py; plays a short scripted story per goal.
//   node mock/server.js            -> http://127.0.0.1:8766  (token: mock-token)
//   electron . --mock              -> starts this and points the app at it
const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

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
  const reminders = [];   // {id, text, due_at, due, repeat, status}
  let nextReminder = 1;
  const agents = new Map([   // name -> {pack, disabled}: two samples shaped like agents/*.lantern-agent.json
    ['tidy-downloads', { disabled: false, pack: {
      format: 'lantern-agent/1', name: 'tidy-downloads', title: 'Tidy Downloads',
      description: 'Sorts a messy folder into subfolders, one approval for the whole plan.',
      author: 'lantern', version: '1.0.0',
      inputs: [{ key: 'folder', question: 'Which folder to tidy?', example: 'Downloads' }],
      goal: 'Tidy {folder} into subfolders by type.', instructions: 'List the folder, then organize it.',
      tools: ['list_folders', 'list_dir', 'search_files', 'organize_files', 'move_file'],
      schedule: { every: 'day', at: '09:00', inputs: { folder: 'Downloads' } } } }],
    ['hello-agent', { disabled: false, pack: {
      format: 'lantern-agent/1', name: 'hello-agent', title: 'Hello agent',
      description: 'A tiny sample agent for the panel.', author: 'lantern', version: '1.0.0',
      inputs: [], goal: 'Say hello.', instructions: 'Reply with one friendly line.',
      tools: ['search_files'] } }],
  ]);
  const settings = { persona: 'smug', skin: 'muse', localsend: false };

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
    } else if (/remind/.test(g)) {
      const text = (goal.match(/remind me (?:in \S+ \S+ )?to (.+)$/i) || [null, 'the thing'])[1];
      const r = { id: nextReminder++, text, due_at: now() + 5, due: 'in a few seconds', repeat: '', status: 'pending' };
      reminders.push(r);
      emit({ type: 'tool', name: 'set_reminder', args: { text, when: 'in 5 seconds' },
             result: { done: true, verified: true, id: r.id, due: r.due }, badge: 'on this computer', task_id: taskId });
      emit({ type: 'reminders', changed: 'set', id: r.id });
      setTimeout(() => {
        if (r.status !== 'pending') return;
        r.status = 'delivered';
        emit({ type: 'reminder', id: r.id, text: r.text, late: false });
        phoneSay(`⏰ Reminder: ${r.text}`);
      }, 5000);
      reply = `Set. I'll ping your phone and the desktop in a few seconds: “${text}”.`;
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

  function submit(goal, pack) {
    const id = nextTask++;
    tasks.push({ id, goal, status: 'active', summary: null, created_at: now(), updated_at: now(), usage: { calls: 0, cost_usd: 0 } });
    emit({ type: 'queued', task_id: id, goal, pack: pack || null });
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
    'GET /usage': () => {
      const rows = [...usage.values()];
      const spent = rows.reduce((a, r) => a + r.cost_usd, 0);
      return { by_model: rows, total_usd: spent, budget: { spent_usd: spent, today_usd: spent, total_usd: 150, daily_usd: null,
                                                             task_usd: 3, left_usd: Math.max(0, 150 - spent) } };
    },
    'GET /memory': () => ({ facts: { 'manager email': 'priya@example.com' } }),
    'GET /channel/messages': () => ({ owner: 'owner', messages: phone }),
    'POST /channel/fake': (b) => { phoneIn(b); return { ok: true }; },
    'POST /forget/everything': (b) => {
      if ((b.confirm || '').toLowerCase() !== 'forget everything') return [400, { detail: 'confirm first' }];
      tasks.length = 0; phone.length = 0; usage.clear();
      Object.assign(audit.cloud, { calls: 0, bytes: 0, by_model: {}, redactions: {} });
      emit({ type: 'forgot', everything: true });
      return { memory: {}, audit: 0 };
    },
    'GET /widgets': () => ({ widgets: [{ name: 'water-timer', title: 'Water break timer', source: 'built-in', ok: true }] }),
    'GET /avatar/manifest': () => ({ active: null, expressions: {}, designs: [] }),
    'POST /avatar/design': (b) => {
      if (!b.brief || !String(b.brief).trim()) return [400, { detail: 'describe the avatar in a few words first' }];
      if (String(b.brief).trim().length > 600) return [400, { detail: 'the brief is too long (at most 600 characters)' }];
      if (!['3d', 'flat', 'pixel', 'neon'].includes(b.style)) return [400, { detail: 'style must be one of 3d, flat, pixel, neon' }];
      return { id: 'mock-design', brief: String(b.brief).trim(), style: b.style, cost_usd: 0.01, preview: '' };
    },
    'GET /reminders': () => reminders.filter((r) => r.status === 'pending'),
    'POST /tasks/resume': () => ({ task_ids: [] }),
    'GET /settings': () => ({ persona: settings.persona, personas: ['helpful', 'sarcastic', 'smug'],
      skin: settings.skin, skins: ['muse', 'glow', 'clock'], localsend: settings.localsend }),
    'POST /settings': (b) => {
      if (b.persona !== undefined && !['helpful', 'sarcastic', 'smug'].includes(b.persona)) {
        return [400, { detail: 'unknown persona' }];
      }
      if (b.skin !== undefined && !['muse', 'glow', 'clock'].includes(b.skin)) {
        return [400, { detail: 'unknown skin' }];
      }
      if (b.persona !== undefined) settings.persona = b.persona;
      if (b.skin !== undefined) settings.skin = b.skin;
      if (b.localsend !== undefined) settings.localsend = !!b.localsend;
      return { persona: settings.persona, personas: ['helpful', 'sarcastic', 'smug'],
        skin: settings.skin, skins: ['muse', 'glow', 'clock'], localsend: settings.localsend };
    },
    'GET /doctor': () => ({
      checks: [
        { id: 'python', title: 'Python 3.11+', status: 'ok', detail: '3.12.4', fix: '', needed_for: ['rehearsal', 'demo'] },
        { id: 'key', title: 'Meta Model API key', status: 'fail', detail: 'MODEL_API_KEY is not set',
          fix: 'Add MODEL_API_KEY=<your key from dev.meta.ai> to .env, then run python scripts/smoke.py', needed_for: ['demo'] },
        { id: 'glimmer', title: 'Private lane (Muse Glimmer)', status: 'warn', detail: 'not set: private questions are refused until it is',
          fix: 'Set LANTERN_GLIMMER_URL=http://127.0.0.1:8080/v1', needed_for: ['demo'] },
      ],
      ready: { rehearsal: { blocked_by: [], to_do: [] }, demo: { blocked_by: ['key'], to_do: ['glimmer'] } },
    }),
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
      const w = url.pathname.match(/^\/widgets\/([a-z0-9-]+)$/);
      if (w && req.headers['x-lantern-token'] === TOKEN) {
        try {
          return send(200, { name: w[1], html: fs.readFileSync(path.join(__dirname, '..', '..', 'widgets', `${w[1]}.html`), 'utf8') });
        } catch {
          return send(404, { detail: 'no such widget' });
        }
      }
      const f = url.pathname.match(/^\/forget\/task\/(\d+)$/);
      if (f && req.method === 'POST' && req.headers['x-lantern-token'] === TOKEN) {
        const i = tasks.findIndex((x) => x.id === Number(f[1]));
        if (i < 0) return send(404, { detail: 'no such task' });
        tasks.splice(i, 1);
        emit({ type: 'forgot', task_id: Number(f[1]) });
        return send(200, { forgot_task: Number(f[1]) });
      }
      const rt = url.pathname.match(/^\/tasks\/(\d+)\/receipt$/);
      if (rt && req.method === 'GET' && req.headers['x-lantern-token'] === TOKEN) {   // same shape as lantern/receipt.py
        const t = tasks.find((x) => x.id === Number(rt[1]));
        if (!t) return send(404, { detail: 'no such task' });
        return send(200, { task: { id: t.id, goal: t.goal, status: t.status, summary: t.summary }, actions: [], looked: 1,
                           checked: 0, unchecked: 0, approvals: [], cost_usd: t.usage.cost_usd,
                           models: [{ model: 'muse-spark-1.3', badge: 'Muse Spark', calls: t.usage.calls, cost_usd: t.usage.cost_usd }],
                           sent: [{ lane: 'cloud', to: "Meta's API", simulated: true, kinds: ['model'], calls: t.usage.calls, bytes: 4096,
                                    redactions: {} }], left_bytes: 0, stayed_local: 0, redactions: {}, text: '' });
      }
      const rc = url.pathname.match(/^\/reminders\/(\d+)\/cancel$/);
      if (rc && req.method === 'POST' && req.headers['x-lantern-token'] === TOKEN) {
        const r = reminders.find((x) => x.id === Number(rc[1]));
        if (!r) return send(404, { detail: 'no such reminder' });
        if (r.status !== 'pending') return send(409, { detail: "that reminder isn't pending" });
        r.status = 'cancelled';
        emit({ type: 'reminders', changed: 'cancelled', id: r.id });
        return send(200, { cancelled: r.id });
      }
      const loopOf = (a) => a.loop || (a.loop = { enabled: false, next_run: null, last_task: null,
        last_status: null, failures: 0, running: null });
      const describeSchedule = (s) => !s ? '' : s.every === 'hours' ? `every ${s.hours} hour${s.hours === 1 ? '' : 's'}`
        : `every ${s.every} at ${s.at}`;
      const loopView = (name, a) => ({ name, title: a.pack.title, scheduled: !!a.pack.schedule,
        schedule: a.pack.schedule || null, description: describeSchedule(a.pack.schedule), ...loopOf(a) });
      const signedView = (a) => ({ ...a.pack, disabled: a.disabled, face: a.face || null, loop: loopOf(a),
        signature_status: a.pack.signature ? 'signed' : 'unsigned', fingerprint: null, mine: false });
      const lp = url.pathname.match(/^\/agents\/([^/]+)\/loop\/(resume|pause|run-now)$/);
      if (lp && req.headers['x-lantern-token'] === TOKEN) {
        const item = agents.get(lp[1]);
        if (!item) return send(404, { detail: `no agent called '${lp[1]}'` });
        if (!item.pack.schedule) return send(400, { detail: `agent '${lp[1]}' has no schedule` });
        if (lp[2] === 'pause') {   // the mock approves at once, like its other shortcuts
          loopOf(item).enabled = false;
          return send(200, { name: lp[1], enabled: false });
        }
        if (lp[2] === 'resume') {
          const l = loopOf(item);
          l.enabled = true; l.failures = 0; l.next_run = now() + 3600;
          return send(200, { enabled: true, name: lp[1], next_run: l.next_run, budget_usd: 0.1 });
        }
        if (item.disabled) return send(400, { detail: `agent '${lp[1]}' is disabled` });
        const goal = `[agent:${lp[1]}] ` + item.pack.goal.replace(/\{(\w+)\}/g, (_, k) => item.pack.schedule.inputs[k] || `{${k}}`);
        return send(200, { task_id: submit(goal, lp[1]) });
      }
      if (req.method === 'GET' && url.pathname === '/loops' && req.headers['x-lantern-token'] === TOKEN) {
        return send(200, { loops: [...agents.entries()].filter(([, a]) => a.pack.schedule)
          .map(([name, a]) => loopView(name, a)) });
      }
      const g = url.pathname.match(/^\/agents(?:\/([^/]+)(?:\/(run|disable|enable|export|face|sign))?)?$/);
      if (g && req.headers['x-lantern-token'] === TOKEN) {
        // Same shapes as lantern/server.py's /agents routes; installs approve at once (no card in mock).
        if (req.method === 'GET' && !g[1]) {
          return send(200, { agents: [...agents.values()].map(signedView) });
        }
        if (req.method === 'POST' && url.pathname === '/agents/install') {
          let data = {};
          try { data = body ? JSON.parse(body) : {}; } catch { return send(400, { detail: 'bad JSON' }); }
          if (!data || typeof data.name !== 'string' || !data.name) return send(400, { detail: 'not an agent pack' });
          const old = agents.get(data.name);
          const replace = url.searchParams.get('replace') === 'true';
          if (old && old.pack.author !== data.author && !replace) {
            return send(200, { installed: false, reason: `an agent called '${data.name}' is already installed (pass replace=true)`, name: data.name });
          }
          agents.set(data.name, { disabled: false, pack: data });
          return send(200, { installed: true, verified: true, name: data.name });
        }
        if (req.method === 'POST' && url.pathname === '/agents/draft') {
          const sample = agents.get('tidy-downloads').pack;
          return send(200, { ...sample, author: '', version: '1.0.0' });
        }
        const item = g[1] && agents.get(g[1]);
        if (!item) return send(404, { detail: `no agent called '${g[1] || ''}'` });
        if (req.method === 'GET' && !g[2]) return send(200, signedView(item));
        if (req.method === 'GET' && g[2] === 'export') return send(200, item.pack);
        if (req.method === 'POST' && g[2] === 'sign') {   // the mock approves at once (no card)
          item.pack.signature = { alg: 'ed25519', public_key: 'bW9jaw', sig: 'bW9jaw' };
          return send(200, { signed: true, verified: true, name: g[1], fingerprint: 'MOCK-0000' });
        }
        if (req.method === 'POST' && g[2] === 'run') {
          if (item.disabled) return send(400, { detail: `agent '${g[1]}' is disabled` });
          let inputs = {};
          try { inputs = body ? JSON.parse(body).inputs || {} : {}; } catch { return send(400, { detail: 'bad JSON' }); }
          const goal = `[agent:${g[1]}] ` + item.pack.goal.replace(/\{(\w+)\}/g, (_, k) => inputs[k] || `{${k}}`);
          return send(200, { task_id: submit(goal, g[1]) });
        }
        if (g[2] === 'face') {
          if (req.method === 'GET') return send(404, { detail: 'no face for that agent' });   // the mock keeps no bytes
          let b = {};
          try { b = body ? JSON.parse(body) : {}; } catch { return send(400, { detail: 'bad JSON' }); }
          if (typeof b.emoji === 'string' && b.emoji.trim()) {
            item.face = { emoji: b.emoji.trim() };
            return send(200, { made: true, face: item.face, cost_usd: 0, model_calls: 0 });
          }
          if (b.generate) {
            item.face = { icon: true };
            return send(200, { made: true, verified: true, face: item.face, cost_usd: 0.01, model_calls: 1 });
          }
          return send(400, { detail: 'give an emoji or generate=true' });
        }
        if (req.method === 'POST' && (g[2] === 'disable' || g[2] === 'enable')) {
          item.disabled = g[2] === 'disable';
          return send(200, { name: g[1], disabled: item.disabled });
        }
        return send(404, { detail: 'not found' });
      }
      const av = url.pathname.match(/^\/avatar\/design\/([a-z0-9-]+)\/accept$/);
      if (av && req.method === 'POST' && req.headers['x-lantern-token'] === TOKEN) {
        // Mock approves at once (no card); the real server waits for the approval.
        emit({ type: 'avatar', active: av[1] });
        return send(200, { accepted: true, verified: true, id: av[1], cost_usd: 0.06, model_calls: 0 });
      }
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
    socket.write(frame(JSON.stringify({ type: 'hello', version: 'mock', pending: [...approvals.values()].map((x) => x.approval),
                                        interrupted: [], ts: now() })));
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
