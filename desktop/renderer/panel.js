// The panel: tasks with model badges, approval cards, privacy view, simulated phone, costs, widgets.
// Everything from the server or a web page is shown with textContent, never as HTML.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  let state = LanternState.initial();
  let health = {};
  const now = () => Date.now() / 1000;

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function chip(badge) {
    const b = String(badge || 'on this computer');
    const kind = /wonder/i.test(b) ? 'wonder' : /glimmer/i.test(b) ? 'glimmer' : /spark/i.test(b) ? 'spark'
      : /voice/i.test(b) ? 'voice' : /image/i.test(b) ? 'image' : /sam/i.test(b) ? 'sam' : '';
    return h('span', { class: 'chip ' + kind }, b);
  }

  function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
  }

  // ---- tabs ---------------------------------------------------------------------------------
  const loaders = { privacy: loadPrivacy, phone: loadPhone, costs: loadCosts, widgets: loadWidgets };
  let tab = 'tasks';
  for (const b of document.querySelectorAll('.tabs button')) {
    b.addEventListener('click', () => {
      tab = b.dataset.tab;
      document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('on', x === b));
      document.querySelectorAll('main > section').forEach((s) => s.classList.toggle('on', s.dataset.panel === tab));
      if (loaders[tab]) loaders[tab]();
    });
  }

  // ---- approvals ------------------------------------------------------------------------------
  async function decide(a, approve) {
    try {
      await api.post(`/approvals/${a.id}`, LanternCards.decision(a, approve));
      toast(approve ? 'Allowed. Lantern will check the result.' : 'Denied.');
    } catch (e) {
      toast(`Couldn't answer: ${e.message}`);
    }
  }

  function renderApprovals() {
    const box = $('approvals');
    box.textContent = '';
    for (const a of LanternState.pendingList(state)) {
      const c = LanternCards.card(a, now());
      box.append(h('div', { class: 'approval' },
        h('div', { class: 'meta' }, h('span', {}, `#${c.id} · ${a.action}`), h('span', {}, c.expired ? 'expired' : `expires in ${c.expiresIn}`)),
        h('h3', {}, c.title),
        h('p', { class: 'summary' }, c.summary),
        c.untrusted ? h('div', { class: 'warn untrusted' }, 'Lantern read untrusted content (a file, web page or email) during this task. Check the details.') : null,
        c.warnings.map((w) => h('div', { class: 'warn' }, w)),
        c.leftForYou.length ? h('p', { class: 'left' }, 'Left for you: ' + c.leftForYou.join(', ')) : null,
        h('div', { class: 'hash' }, `bound to sha256 ${c.hash}…`),
        h('div', { class: 'row' },
          h('button', { class: 'allow', disabled: c.expired, onclick: () => decide(a, true) }, c.allowLabel),
          h('button', { class: 'deny', onclick: () => decide(a, false) }, 'Deny'))));
    }
  }

  // ---- tasks ------------------------------------------------------------------------------------
  function renderTasks() {
    const box = $('tasks');
    box.textContent = '';
    const tasks = LanternState.recentTasks(state, 12);
    if (!tasks.length) box.append(h('p', { class: 'empty' }, 'No tasks yet. Try “which projects use torch?”'));
    for (const t of tasks) {
      box.append(h('div', { class: 'task' },
        h('div', { class: 'goal' }, t.goal || `Task #${t.id}`),
        h('div', { class: 'status' }, `#${t.id} · ${t.status}${t.cost ? ' · ' + LanternAudit.money(t.cost) : ''}${t.verified ? ' · verified ✓' : ''}`),
        h('ul', { class: 'steps' }, t.steps.slice(-12).map((s) => h('li', { class: s.ok ? '' : 'bad' }, chip(s.badge), h('span', {}, s.label)))),
        t.reply ? h('p', { class: 'reply' }, t.reply) : null));
    }
    $('today-cost').textContent = LanternAudit.money(state.usage.total);
  }

  $('ask').addEventListener('submit', async (e) => {
    e.preventDefault();
    const goal = $('goal').value.trim();
    if (!goal) return;
    try {
      await api.post('/task', { goal });
      $('goal').value = '';
    } catch (err) {
      toast(`Couldn't start: ${err.message}`);
    }
  });

  $('stop').addEventListener('click', async () => {
    const r = await window.lantern.stop();
    toast(r && r.stopped ? `Stopped. ${r.denied_approvals || 0} approval(s) denied.` : 'Could not reach Lantern to stop it.');
  });

  // ---- privacy --------------------------------------------------------------------------------------
  async function loadPrivacy() {
    try {
      const v = LanternAudit.view(await api.get('/audit'));
      $('audit-headline').textContent = v.headline;
      $('audit-private').textContent = v.privateLine + (v.selfhosted ? ' · ' + v.selfhosted : '');
      const rows = $('audit-rows');
      rows.textContent = '';
      for (const r of v.rows) rows.append(h('tr', {}, h('td', {}, r.name), h('td', {}, `${r.calls} · ${r.bytes}`)));
      $('audit-redactions').textContent = v.redactions.length ? 'Redacted before sending: ' + v.redactions.join(', ') : '';
      const z = await api.get('/zones');
      const list = $('zones');
      list.textContent = '';
      for (const zone of z.zones) {
        const sel = h('select', { onchange: (e) => setZone(zone.path, e.target.value) },
          z.choices.map((c) => h('option', c === zone.zone ? { selected: true } : {}, c)));
        list.append(h('div', { class: 'zone' }, h('code', {}, zone.path), sel));
      }
    } catch (e) {
      $('audit-headline').textContent = `Can't reach Lantern (${e.message})`;
    }
  }

  async function setZone(path, zone) {
    try {
      await api.post('/zones', { path, zone });
      toast(`${path} is now ${zone}`);
      loadPrivacy();
    } catch (e) {
      toast(`Couldn't change the zone: ${e.message}`);
    }
  }

  $('zone-add').addEventListener('submit', (e) => {
    e.preventDefault();
    const p = $('zone-path').value.trim();
    if (p) setZone(p, $('zone-kind').value);
  });

  // ---- simulated phone ----------------------------------------------------------------------------------
  async function loadPhone() {
    const box = $('phone-msgs');
    if (!(health.channels || []).includes('fake')) {
      $('phone-note').textContent = 'real channels: ' + ((health.channels || []).join(', ') || 'none');
      box.textContent = '';
      box.append(h('p', { class: 'empty' }, 'The simulated phone is off. Lantern is talking to your real WhatsApp/Telegram.'));
      return;
    }
    $('phone-note').textContent = 'simulated phone (rehearsal)';
    try {
      const { messages } = await api.get('/channel/messages');
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.textContent = '';
      for (const m of messages) {
        box.append(h('div', { class: 'msg' + (m.from_owner ? ' me' : '') }, m.text,
          m.buttons ? h('div', { class: 'btns' }, m.buttons.map((b) =>
            h('button', { onclick: () => phoneSend({ button: b.id }) }, b.title))) : null));
      }
      if (atBottom) box.scrollTop = box.scrollHeight;
    } catch (e) {
      box.textContent = `Can't reach the phone (${e.message})`;
    }
  }

  async function phoneSend(body) {
    try {
      await api.post('/channel/fake', body);
      setTimeout(loadPhone, 300);
    } catch (e) {
      toast(`Phone: ${e.message}`);
    }
  }

  $('phone-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const text = $('phone-text').value.trim();
    if (text) phoneSend({ text });
    $('phone-text').value = '';
  });

  // ---- costs ------------------------------------------------------------------------------------------------
  async function loadCosts() {
    try {
      const u = await api.get('/usage');
      const rows = $('cost-rows');
      rows.textContent = '';
      for (const r of u.by_model) {
        rows.append(h('tr', {}, h('td', {}, r.model), h('td', {}, `${r.calls} calls · ${LanternAudit.money(r.cost_usd)}`)));
      }
      $('cost-total').textContent = `Total so far: ${LanternAudit.money(u.total_usd)} of the $150 hackathon budget`;
    } catch (e) {
      $('cost-total').textContent = `Can't reach Lantern (${e.message})`;
    }
  }

  // ---- widgets (sandboxed host; see docs/WIDGETS.md) ----------------------------------------------------------
  const host = $('widget-host');
  let currentWidget = null;

  async function loadWidgets() {
    const list = $('widget-list');
    list.textContent = '';
    let widgets = [];
    try {
      widgets = (await api.get('/widgets')).widgets || [];
    } catch (e) { /* no widget support on this server yet */ }
    if (!widgets.length) list.append(h('p', { class: 'hint' }, 'No widgets yet. Ask “make me a water timer”.'));
    for (const w of widgets) list.append(h('button', { onclick: () => openWidget(w.name) }, w.title || w.name));
  }

  async function openWidget(name) {
    const w = await api.get(`/widgets/${encodeURIComponent(name)}`);
    currentWidget = name;
    host.contentWindow.postMessage({ type: 'lantern.load', html: w.html }, '*');
  }

  window.addEventListener('message', (ev) => {
    if (ev.source !== host.contentWindow || !ev.data || ev.data.type !== 'lantern.widget' || !currentWidget) return;
    const checked = LanternWidgets.check(ev.data.payload);
    if (!checked.ok) return;
    const m = checked.msg;
    const store = LanternWidgets.storage(window.localStorage, currentWidget);
    if (m.type === 'notify') toast(`${currentWidget}: ${m.text}`);
    else if (m.type === 'storage.get') host.contentWindow.postMessage({ type: 'lantern.reply', payload: { id: m.id, value: store.get(m.key) } }, '*');
    else if (m.type === 'storage.set') {
      try { store.set(m.key, m.value); } catch (e) { toast(e.message); }
    }
  });

  // ---- live events -------------------------------------------------------------------------------------------
  function renderAll() {
    renderApprovals();
    renderTasks();
  }

  api.events((e) => {
    state = LanternState.reduce(state, { ts: now(), ...e });
    renderAll();
    if (e.type === 'approval' && e.event === 'requested') window.lantern.showPanel();
    if (e.type === 'done' && tab === 'privacy') loadPrivacy();
    if (tab === 'phone' && (e.type === 'approval' || e.type === 'done' || e.type === 'queued')) setTimeout(loadPhone, 300);
  }, async (up) => {
    $('conn').textContent = up ? (cfg.mock ? 'connected (mock)' : 'connected') : 'offline: is `python -m lantern --serve` running?';
    $('conn').className = up ? 'up' : 'down';
    if (up) {
      try { health = await api.get('/health'); } catch (e) { health = {}; }
    }
  });

  window.lantern.onStopped(() => { state = LanternState.reduce(state, { type: 'stopped', ts: now() }); renderAll(); });
  setInterval(renderApprovals, 1000);   // countdowns
  setInterval(() => { if (tab === 'phone') loadPhone(); }, 3000);
  renderAll();
})();
