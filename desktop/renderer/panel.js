// The panel: tasks with model badges, approval cards, privacy view, simulated phone, costs, widgets.
// Everything from the server or a web page is shown with textContent, never as HTML.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  let state = LanternState.initial();
  let health = {};
  const now = () => Date.now() / 1000;

  // Lantern's Muse-made face in the header, when the avatar sheet exists.
  const face = ((window.LanternAvatarManifest || {}).expressions || {}).idle;
  if (face && /\.(webp|png)$/.test(face.file)) {
    const img = h('img', { src: `../assets/avatar/${face.file}`, alt: 'Lantern' });
    $('mark').replaceChildren(img);
  }

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

  function bellIcon() {   // our own static icon from the sprite in panel.html
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ico');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-bell');
    svg.append(use);
    return svg;
  }

  function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 2500);
  }

  // ---- tabs ---------------------------------------------------------------------------------
  const loaders = { privacy: loadPrivacy, phone: loadPhone, costs: loadCosts, widgets: loadWidgets, setup: loadDoctor };
  const tabButtons = [...document.querySelectorAll('.tabs button')];
  let tab = 'tasks';
  function showTab(name) {
    tab = name;
    tabButtons.forEach((x) => { x.classList.toggle('on', x.dataset.tab === name); x.setAttribute('aria-selected', String(x.dataset.tab === name)); });
    document.querySelectorAll('main > section').forEach((s) => s.classList.toggle('on', s.dataset.panel === name));
    window.scrollTo({ top: 0 });
    if (loaders[name]) loaders[name]();
  }
  for (const b of tabButtons) b.addEventListener('click', () => showTab(b.dataset.tab));
  // Ctrl/Cmd+1…6 switch sections, like any native app with a tab bar.
  window.addEventListener('keydown', (e) => {
    const n = Number(e.key);
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && n >= 1 && n <= tabButtons.length) {
      e.preventDefault();
      showTab(tabButtons[n - 1].dataset.tab);
    }
  });

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
      box.append(h('div', { class: 'approval', role: 'alertdialog', 'aria-label': c.title },
        h('div', { class: 'meta' }, h('span', {}, `#${c.id}`), h('span', {}, c.expired ? 'expired' : `expires in ${c.expiresIn}`)),
        h('h3', {}, c.title),
        c.fields
          ? [h('p', { class: 'summary lead' }, c.lead),
             h('dl', { class: 'fields' }, c.fields.flatMap((f) => [h('dt', {}, f.label), h('dd', {}, f.value)]))]
          : h('p', { class: 'summary' }, c.summary),
        c.untrusted ? h('div', { class: 'warn untrusted' }, 'Lantern read untrusted content (a file, web page or email) during this task. Check the details.') : null,
        c.warnings.map((w) => h('div', { class: 'warn' }, w)),
        c.leftForYou.length ? h('p', { class: 'left' }, 'Left for you: ' + c.leftForYou.join(', ')) : null,
        h('div', { class: 'hash' }, `bound to sha256 ${c.hash}…`),
        h('div', { class: 'row-btns' },
          h('button', { class: 'deny', onclick: () => decide(a, false) }, 'Deny'),
          h('button', { class: 'allow', disabled: c.expired, onclick: () => decide(a, true) }, c.allowLabel))));
    }
  }

  // ---- tasks ------------------------------------------------------------------------------------
  const STATE_MARK = { done: '✓', failed: '!', cancelled: '✕', interrupted: '‖', resumed: '↻' };
  const openSteps = new Set();   // tasks whose steps the user expanded

  function renderTasks() {
    const box = $('tasks');
    box.textContent = '';
    const tasks = LanternState.recentTasks(state, 12);
    if (state.interrupted.length) {
      const n = state.interrupted.length;
      box.append(h('div', { class: 'interrupted' },
        h('b', {}, `${n} task${n > 1 ? 's were' : ' was'} cut short when Lantern stopped`),
        h('ul', {}, state.interrupted.slice(0, 4).map((t) => h('li', {}, `#${t.id} ${t.goal}`))),
        h('button', { class: 'tinted', onclick: resumeAll }, 'Resume all')));
    }
    if (!tasks.length && !state.interrupted.length) {
      box.append(h('p', { class: 'empty' }, 'Nothing yet today. Tap a suggestion, or ask for anything.'));
    } else if (tasks.length) {
      box.append(h('h2', { class: 'section' }, 'Recent'));
    }
    tasks.forEach((t, i) => {
      const live = t.status === 'running' || t.status === 'queued';
      const steps = h('details', { open: live || i === 0 || openSteps.has(t.id) || undefined,
                                   ontoggle: (e) => (e.target.open ? openSteps.add(t.id) : openSteps.delete(t.id)) },
        h('summary', {}, `${t.steps.length} step${t.steps.length === 1 ? '' : 's'}`),
        h('ul', { class: 'steps' }, t.steps.slice(-12).map((s) => h('li', { class: s.ok ? '' : 'bad' }, chip(s.badge), h('span', {}, s.label)))));
      box.append(h('article', { class: 'task' },
        h('div', { class: 'head' },
          h('span', { class: `state ${t.status}`, 'aria-hidden': 'true' }, STATE_MARK[t.status] || ''),
          h('div', {},
            h('div', { class: 'goal' }, t.goal || `Task #${t.id}`),
            h('div', { class: 'status' }, `#${t.id} · ${t.status}${t.cost ? ' · ' + LanternAudit.money(t.cost) : ''}${t.verified ? ' · verified ✓' : ''}`))),
        t.steps.length ? steps : null,
        t.reply ? h('p', { class: 'reply' }, t.reply) : null,
        h('div', { class: 'actions' },
          ['failed', 'cancelled', 'interrupted'].includes(t.status)
            ? h('button', { class: 'forget', onclick: () => retryTask(t.id) }, t.status === 'interrupted' ? 'Resume' : 'Try again') : null,
          !live ? h('button', { class: 'forget quiet', onclick: () => forgetTask(t.id) }, 'Forget') : null)));
    });
    $('today-cost').textContent = LanternAudit.money(state.usage.total);
    renderGlance();
  }

  // ---- today: greeting, suggestions, at-a-glance tiles ----------------------------------------------
  let upcoming = [];
  function renderGlance() {
    const t = now();
    $('today-date').textContent = LanternToday.dateLine(t);
    $('today-greeting').textContent = LanternToday.greeting(t);
    $('glance').replaceChildren(...LanternToday.glance(state, { reminders: upcoming, now: t }).map((g) =>
      h('div', { class: `tile ${g.tone}`, 'data-tile': g.id },
        h('span', { class: 't-label' }, g.label), h('span', { class: 't-value' }, g.value), h('span', { class: 't-note' }, g.note))));
  }
  $('suggestions').replaceChildren(...LanternToday.SUGGESTIONS.map((s) => h('button', { class: 'chip-s', type: 'button', onclick: () => {
    $('goal').value = s.goal;
    $('goal').focus();
    $('goal').setSelectionRange(s.goal.length, s.goal.length);
  } }, s.label)));

  // ---- reminders ----------------------------------------------------------------------------------
  async function loadReminders() {
    let items = [];
    try { items = await api.get('/reminders'); } catch (e) { items = []; }
    upcoming = items;
    renderGlance();
    const box = $('reminders');
    box.textContent = '';
    box.hidden = !items.length;
    if (!items.length) return;
    for (const r of items) {
      box.append(h('div', { class: 'row reminder' },
        h('span', { class: 'row-icon orange' }, bellIcon()),
        h('span', { class: 'what' }, r.text),
        h('span', { class: 'when' }, r.due + (r.repeat ? ` · ${r.repeat}` : '')),
        h('button', { class: 'forget', onclick: () => cancelReminder(r) }, 'Cancel')));
    }
  }

  async function cancelReminder(r) {
    try {
      await api.post(`/reminders/${r.id}/cancel`);
      toast(`Reminder cancelled: ${r.text}`);
    } catch (e) {
      toast(`Couldn't cancel it: ${e.message}`);
    }
    loadReminders();
  }

  async function resumeAll() {
    try {
      const r = await api.post('/tasks/resume');
      toast(r.task_ids.length ? `Resuming ${r.task_ids.length} task(s).` : 'Nothing to resume.');
    } catch (e) {
      toast(`Couldn't resume: ${e.message}`);
    }
  }

  // ---- setup check (lantern --doctor) ------------------------------------------------------------
  async function loadDoctor() {
    const box = $('doctor');
    box.textContent = 'Checking…';
    let d;
    try { d = await api.get('/doctor'); } catch (e) { box.textContent = `Couldn't check: ${e.message}`; return; }
    const v = LanternSetup.view(d);
    $('ready-demo').textContent = v.demo;
    $('ready-rehearsal').textContent = v.rehearsal;
    box.textContent = '';
    for (const c of v.rows) {
      box.append(h('div', { class: `check ${c.status}` },
        h('span', { class: 'mark' }, c.mark),
        h('div', {}, h('b', {}, c.title), h('div', { class: 'detail' }, c.detail), c.fix ? h('code', {}, c.fix) : null)));
    }
  }
  $('doctor-again').addEventListener('click', loadDoctor);

  async function retryTask(id) {
    try {
      await api.post(`/tasks/${id}/retry`);
    } catch (e) {
      toast(`Couldn't retry: ${e.message}`);
    }
  }

  async function forgetTask(id) {
    if (!window.confirm(`Really forget task #${id}? Its timeline and audit rows are deleted for good.`)) return;
    try {
      await api.post(`/forget/task/${id}`);
      state = LanternState.reduce(state, { type: 'forgot', task_id: id, ts: now() });
      renderAll();
      toast(`Task #${id} forgotten.`);
    } catch (e) {
      toast(`Couldn't forget it: ${e.message}`);
    }
  }

  $('forget-all').addEventListener('click', async () => {
    if (!window.confirm('Forget everything? Memory, audit log, voice notes, screenshots and the form profile are erased for good.')) return;
    try {
      await api.post('/forget/everything', { confirm: 'forget everything' });
      state = LanternState.reduce(state, { type: 'forgot', everything: true, ts: now() });
      renderAll();
      loadPrivacy();
      toast('Everything forgotten.');
    } catch (e) {
      toast(`Couldn't forget: ${e.message}`);
    }
  });

  // ---- SAM 3.1 check-in: one snapshot on click, objects only, never stored -------------------------
  (async () => {
    let ideas = ['coffee mug', 'water glass', 'plant'];
    try { ideas = (await api.get('/checkin/ideas')).ideas; } catch (e) { /* older server */ }
    $('checkin-concept').replaceChildren(...ideas.map((i) => h('option', {}, i)));
  })();

  async function snapshot() {
    await window.lantern.allowCameraOnce();
    const stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false });
    try {
      const video = h('video', { muted: true, playsinline: true });
      video.srcObject = stream;
      await video.play();
      const canvas = h('canvas', { width: 640, height: 480 });
      canvas.getContext('2d').drawImage(video, 0, 0, 640, 480);
      return canvas.toDataURL('image/jpeg', 0.8);
    } finally {
      stream.getTracks().forEach((t) => t.stop());   // camera off right away
    }
  }

  $('checkin').addEventListener('click', async () => {
    try {
      const r = await api.post('/checkin', { image: await snapshot(), concept: $('checkin-concept').value });
      toast(r.reaction);
    } catch (e) {
      toast(`Check-in: ${e.message}`);
    }
  });

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
      const v = LanternAudit.view(await api.get('/audit'), { rehearsal: !!health.demo });
      $('audit-headline').replaceChildren(h('span', { class: 'big' }, v.amount), h('span', { class: 'caption' }, v.caption));
      $('audit-private').textContent = v.privateLine + (v.selfhosted ? ' · ' + v.selfhosted : '');
      const rows = $('audit-rows');
      rows.textContent = '';
      for (const r of v.rows) rows.append(h('tr', {}, h('td', {}, r.name), h('td', {}, `${r.calls} · ${r.bytes}`)));
      $('audit-redactions').textContent = v.redactions.length ? 'Redacted before sending: ' + v.redactions.join(', ') : '';
      const z = await api.get('/zones');
      const list = $('zones');
      list.textContent = '';
      // Built-in hidden paths for other systems (macOS keychains on Linux, ...) don't exist here: fold them away.
      const folded = z.zones.filter((zone) => zone.zone === 'hidden' && zone.exists === false);
      for (const zone of z.zones.filter((x) => !folded.includes(x))) {
        const sel = h('select', { onchange: (e) => setZone(zone.path, e.target.value) },
          z.choices.map((c) => h('option', c === zone.zone ? { selected: true } : {}, c)));
        const name = zone.path.split(/[\\/]/).filter(Boolean).pop() || zone.path;
        list.append(h('div', { class: 'zone' }, h('div', { class: 'zone-name' }, h('b', {}, name), h('code', {}, zone.path)), sel));
      }
      if (folded.length) list.append(h('p', { class: 'hint' }, `+ ${folded.length} built-in hidden paths that don't exist on this computer`));
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
      $('cost-total').textContent = `${LanternAudit.money(u.total_usd)} of $150 spent`;
      $('cost-bar').style.width = `${Math.min(100, (u.total_usd / 150) * 100)}%`;
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
    for (const w of widgets) list.append(h('button', { onclick: () => openWidget(w.name) }, w.title || w.name, h('small', {}, w.source === 'built-in' ? 'Built in' : 'Made for you')));
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
    if (tab === 'phone' && (e.type === 'approval' || e.type === 'done' || e.type === 'queued' || e.type === 'reminder')) setTimeout(loadPhone, 300);
    if (e.type === 'reminder') toast(`⏰ ${e.text}`);
    if (e.type === 'reminder' || e.type === 'reminders') loadReminders();
  }, async (up) => {
    $('conn').textContent = up ? (cfg.mock ? 'Connected · mock' : 'Connected') : 'Offline: is `lantern --serve` running?';
    $('conn').className = 'status ' + (up ? 'up' : 'down');
    if (up) {
      try { health = await api.get('/health'); } catch (e) { health = {}; }
      if (health.demo) $('conn').textContent = 'Rehearsal · scripted model, nothing sent';
      loadReminders();
    }
  });

  window.lantern.onStopped(() => { state = LanternState.reduce(state, { type: 'stopped', ts: now() }); renderAll(); });
  setInterval(renderApprovals, 1000);   // countdowns
  setInterval(() => { if (tab === 'phone') loadPhone(); }, 3000);
  renderAll();
})();
