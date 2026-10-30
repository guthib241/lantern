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
  const loaders = { privacy: loadPrivacy, phone: loadPhone, costs: loadCosts, widgets: loadWidgets,
                    agents: loadAgents, setup: () => { loadSettings(); loadDoctor(); loadAvatar(); } };
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
  // The avatar window's agent faces open a task's timeline here (T4c).
  if (window.lantern.onShowTask) window.lantern.onShowTask((id) => {
    openSteps.add(Number(id));
    showTab('tasks');
    renderTasks();
    const el = document.getElementById(`task-${Number(id)}`);
    if (el) el.scrollIntoView({ block: 'center' });
  });
  // Ctrl/Cmd+1…7 switch sections, like any native app with a tab bar.
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
      box.append(h('article', { class: 'task', id: `task-${t.id}` },
        h('div', { class: 'head' },
          h('span', { class: `state ${t.status}`, 'aria-hidden': 'true' }, STATE_MARK[t.status] || ''),
          h('div', {},
            h('div', { class: 'goal' }, t.goal || `Task #${t.id}`),
            h('div', { class: 'status' }, `#${t.id} · ${t.status}${t.cost ? ' · ' + LanternAudit.money(t.cost) : ''}${t.verified ? ' · verified ✓' : ''}`))),
        t.steps.length ? steps : null,
        t.reply ? h('p', { class: 'reply' }, t.reply) : null,
        h('div', { class: 'actions' },
          t.undoable && !t.undoneBy && !live
            ? h('button', { class: 'forget', onclick: () => undoTask(t.id), title: 'Put the files back (asks first)' }, 'Undo') : null,
          t.undoneBy ? h('span', { class: 'undone' }, `Undone in #${t.undoneBy}`) : null,
          ['failed', 'cancelled', 'interrupted'].includes(t.status)
            ? h('button', { class: 'forget', onclick: () => retryTask(t.id) }, t.status === 'interrupted' ? 'Resume' : 'Try again') : null,
          ['done', 'failed'].includes(t.status)
            ? h('button', { class: 'forget', onclick: () => draftFromTask(t.id), title: 'Turn this finished task into a shareable agent' }, 'Make an agent') : null,
          !live ? h('button', { class: 'forget', onclick: () => showReceipt(t.id) }, 'Receipt') : null,
          !live ? h('button', { class: 'forget quiet', onclick: () => forgetTask(t.id) }, 'Forget') : null)));
    });
    $('today-cost').textContent = LanternAudit.money(state.usage.total);
    renderGlance();
  }

  // ---- welcome (first launch) and preferences ------------------------------------------------------
  const keyName = (k) => k.replace('CommandOrControl', cfg.platform === 'darwin' ? '⌘' : 'Ctrl').replace(/Shift/, '⇧')
    .replace(/\+/g, ' ');
  if (cfg.askHotkey) {
    $('kbd-ask').textContent = keyName(cfg.askHotkey);
    $('welcome-ask').textContent = keyName(cfg.askHotkey);
    $('kbd-stop').textContent = keyName('CommandOrControl+Shift+X');
    $('kbd-tabs').textContent = keyName('CommandOrControl+1') + ' – ' + tabButtons.length;
  }
  function seen() {
    try { return window.localStorage.getItem('lantern.welcomed') === '1'; } catch (e) { return false; }
  }
  function closeWelcome(then) {
    try { window.localStorage.setItem('lantern.welcomed', '1'); } catch (e) { /* private storage: show again next time */ }
    $('welcome').hidden = true;
    if (then) then();
  }
  if (!seen()) $('welcome').hidden = false;
  $('welcome-go').addEventListener('click', () => closeWelcome());
  $('welcome-setup').addEventListener('click', () => closeWelcome(() => showTab('setup')));

  const PERSONA_NAMES = { smug: 'Smug', sarcastic: 'Sarcastic', helpful: 'Helpful' };
  async function loadSettings() {
    let st;
    try { st = await api.get('/settings'); } catch (e) { return; }
    $('persona').replaceChildren(...st.personas.map((p) => h('button', {
      role: 'radio', 'aria-checked': String(p === st.persona), type: 'button',
      onclick: async () => {
        try {
          await api.post('/settings', { persona: p });
          toast(`Lantern will be ${PERSONA_NAMES[p] || p} from the next task.`);
        } catch (err) {
          toast(`Couldn't change it: ${err.message}`);
        }
        loadSettings();
      },
    }, PERSONA_NAMES[p] || p)));
    const SKIN_NAMES = { muse: 'Face', glow: 'Glow', clock: 'Clock' };
    $('avatar-skins').replaceChildren(...(st.skins || ['muse', 'glow', 'clock']).map((s) => h('button', {
      role: 'radio', 'aria-checked': String(s === (st.skin || 'muse')), type: 'button',
      onclick: async () => {
        try {
          await api.post('/settings', { skin: s });
          toast(s === 'clock' ? 'Lantern is a clock now.' : `Lantern's skin is ${SKIN_NAMES[s] || s}.`);
        } catch (err) {
          toast(`Couldn't change it: ${err.message}`);
        }
        loadSettings();
      },
    }, SKIN_NAMES[s] || s)));
  }

  // ---- avatar studio (Setup → Avatar; routes are lantern/server.py's /avatar) -------------------------
  const AVATAR_STYLES = ['3d', 'flat', 'pixel', 'neon'];
  let avatarStyle = '3d';
  let avatarPreview = null;   // {id, preview, cost_usd} waiting for “Use this”

  function renderAvatarStyles() {
    $('avatar-styles').replaceChildren(...AVATAR_STYLES.map((s) => h('button', {
      role: 'radio', 'aria-checked': String(s === avatarStyle), type: 'button',
      onclick: () => { avatarStyle = s; renderAvatarStyles(); },
    }, s)));
  }

  function avatarRow(imgSrc, title, note, button) {
    return h('div', { class: 'row avatar-row' },
      imgSrc ? h('img', { class: 'avatar-thumb', src: imgSrc, alt: '' }) : null,
      h('div', { class: 'row-text' }, h('b', {}, title), note ? h('small', {}, note) : null),
      button);
  }

  async function avatarThumb(id) {
    try {
      const blob = await api.blob(`/avatar/file/${encodeURIComponent(id)}/idle`);
      return URL.createObjectURL(blob);
    } catch (e) {
      return '';   // no idle image yet (a bare preview): the row shows text only
    }
  }

  async function loadAvatar() {
    renderAvatarStyles();
    const box = $('avatar-now');
    box.textContent = '';
    let manifest = null;
    try { manifest = await api.get('/avatar/manifest'); } catch (e) { manifest = null; }
    if (!manifest) {
      box.append(h('p', { class: 'hint' }, 'Avatar designs need the newest Lantern server.'));
      return;
    }
    if (avatarPreview) {
      box.append(avatarRow(avatarPreview.preview,
        `Preview · ${LanternAudit.money(avatarPreview.cost_usd)}`,
        'One image. “Use this” draws the other six after you approve.',
        h('button', { class: 'tinted', onclick: acceptAvatarPreview }, 'Use this')));
    }
    const designs = manifest.designs || [];
    if (!designs.length && !avatarPreview) {
      box.append(h('p', { class: 'hint' }, 'No designs yet. Describe the face above and tap Preview.'));
    }
    for (const d of designs) {
      const thumb = d.status === 'complete' ? await avatarThumb(d.id) : '';
      const label = d.active ? 'On screen now' : d.status === 'complete' ? 'Finished' : 'Preview';
      const button = !d.active && d.status === 'complete'
        ? h('button', { class: 'tinted', onclick: () => useAvatarDesign(d.id) }, 'Use') : null;
      box.append(avatarRow(thumb, `${d.style} · ${label}`, String(d.brief || '').slice(0, 140), button));
    }
  }

  async function acceptAvatarPreview() {
    if (!avatarPreview) return;
    try {
      const r = await api.post(`/avatar/design/${encodeURIComponent(avatarPreview.id)}/accept`);
      toast(r.accepted ? 'Lantern has a new face.' : `Not changed: ${r.reason || 'declined'}.`);
      if (r.accepted) avatarPreview = null;
    } catch (e) {
      toast(`Couldn't finish it: ${e.message}`);
    }
    loadAvatar();
  }

  async function useAvatarDesign(id) {
    try {   // a finished design just becomes active again: no model call
      const r = await api.post(`/avatar/design/${encodeURIComponent(id)}/accept`);
      toast(r.accepted ? 'Lantern has a new face.' : `Not changed: ${r.reason || 'declined'}.`);
    } catch (e) {
      toast(`Couldn't switch: ${e.message}`);
    }
    loadAvatar();
  }

  $('avatar-preview').addEventListener('click', async () => {
    const brief = $('avatar-brief').value.trim();
    if (!brief) {
      toast('Describe the face first.');
      return;
    }
    try {
      avatarPreview = await api.post('/avatar/design', { brief, style: avatarStyle });
      toast(`Preview drawn for ${LanternAudit.money(avatarPreview.cost_usd)}.`);
    } catch (e) {
      toast(`Couldn't draw it: ${e.message}`);
      avatarPreview = null;
    }
    loadAvatar();
  });

  // ---- today: greeting, suggestions, at-a-glance tiles ----------------------------------------------
  let upcoming = [];
  let budget = null;   // {spent_usd, total_usd, task_usd, left_usd, ...} from /usage: the limits enforced in code
  function renderGlance() {
    const t = now();
    $('today-date').textContent = LanternToday.dateLine(t);
    $('today-greeting').textContent = LanternToday.greeting(t);
    $('glance').replaceChildren(...LanternToday.glance(state, { reminders: upcoming, now: t, budget }).map((g) =>
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

  // ---- receipts: what a task changed and checked, what you approved, what left, what it cost --------
  let receiptFrom = null;   // the button that opened the sheet gets focus back
  async function showReceipt(id) {
    try {
      const v = LanternReceipt.view(await api.get(`/tasks/${id}/receipt`));
      $('receipt-eyebrow').textContent = `Receipt · task #${id} · ${v.status}`;
      $('receipt-title').textContent = v.title;
      $('receipt-body').replaceChildren(...v.sections.flatMap((sec) => [
        h('h2', {}, sec.title),
        h('ul', {}, sec.rows.map((r) => h('li', { class: r.tone }, h('span', { class: 'mark', 'aria-hidden': 'true' }, r.mark),
          h('div', {}, h('b', {}, r.text), r.detail ? h('small', {}, r.detail) : null))))]));
      receiptFrom = document.activeElement;
      $('receipt').hidden = false;
      document.querySelector('#receipt .sheet').scrollTop = 0;   // open at the title
      $('receipt-done').focus({ preventScroll: true });
    } catch (e) {
      toast(`Couldn't load the receipt: ${e.message}`);
    }
  }
  function closeReceipt() {
    $('receipt').hidden = true;
    if (receiptFrom && receiptFrom.focus) receiptFrom.focus();
  }
  $('receipt-done').addEventListener('click', closeReceipt);
  $('receipt').addEventListener('click', (e) => { if (e.target === $('receipt')) closeReceipt(); });
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('receipt').hidden) closeReceipt(); });

  async function undoTask(id) {
    try {
      await api.post(`/tasks/${id}/undo`);   // a new task with one approval card; no model, $0
    } catch (e) {
      toast(`Couldn't undo: ${e.message}`);
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
        const name = zone.path.split(/[\\/]/).filter(Boolean).pop() || zone.path;
        const sel = h('select', { onchange: (e) => setZone(zone.path, e.target.value), 'aria-label': `Zone for ${zone.path}` },
          z.choices.map((c) => h('option', c === zone.zone ? { selected: true } : {}, c)));
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
      budget = u.budget || null;
      const b = LanternToday.budgetView(budget, u.total_usd);
      $('cost-total').textContent = b.headline;
      $('cost-bar').style.width = `${b.percent}%`;
      $('cost-bar').classList.toggle('high', b.percent >= 80);
      $('cost-limits').textContent = b.limits;
      renderGlance();
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

  // ---- agents (shareable packs; routes are lantern/server.py's /agents) ------------------------------------
  let agentPacks = [];
  let agentRunTask = {};    // pack name -> task id of its latest run, for the Watch/Stop line
  let agentRunOpen = null;  // pack name with its run form open
  let agentFaceOpen = null; // pack name with its face form open ("Make a face")
  let agentFaceURL = {};    // pack name -> object URL of its drawn icon
  let agentDraft = null;    // {fields, from} a draft being edited, or null
  let draftRefs = {};       // field name -> input element in the draft editor

  async function loadAgents() {
    const list = $('agent-list');
    try {
      agentPacks = (await api.get('/agents')).agents || [];
    } catch (e) {
      list.textContent = '';
      list.append(h('p', { class: 'empty' }, `Can't reach Lantern (${e.message})`));
      return;
    }
    await loadAgentFaces();
    renderAgents();
    renderAgentDraft();
  }

  async function loadAgentFaces() {
    const next = {};
    for (const pack of agentPacks) {
      if (pack.face && pack.face.icon && pack.name) {
        try {   // the icon needs the token header, so it loads as a blob, never a bare <img src>
          const blob = await api.blob(`/agents/${encodeURIComponent(pack.name)}/face`);
          next[pack.name] = URL.createObjectURL(blob);
        } catch (e) { /* no icon on disk yet: the emoji or the Face button shows */ }
      }
    }
    agentFaceURL = next;
  }

  function agentTaskStatus(name) {
    const id = agentRunTask[name];
    const t = id != null && state.tasks[id];
    return t ? { id, status: t.status, live: t.status === 'running' || t.status === 'queued' } : null;
  }

  function renderAgents() {
    const list = $('agent-list');
    list.textContent = '';
    if (!agentPacks.length) {
      list.append(h('p', { class: 'empty' }, 'No agents yet. Add a .lantern-agent.json file above to get started.'));
    }
    for (const pack of agentPacks) list.append(agentCard(pack));
  }

  function agentFaceThumb(pack, face) {
    if (face.kind === 'icon' && agentFaceURL[pack.name]) {
      return h('img', { class: 'agent-face', src: agentFaceURL[pack.name], alt: '' });
    }
    if (face.kind === 'emoji') {
      return h('span', { class: 'agent-face emoji', role: 'img', 'aria-label': 'Agent face' }, face.emoji);
    }
    return null;
  }

  function agentCard(pack) {
    const c = LanternAgents.cardView(pack);
    const face = LanternAgents.faceView(pack);
    const run = agentTaskStatus(c.name);
    const refs = {};
    const form = agentRunOpen === c.name && c.runnable
      ? h('div', { class: 'agent-form' },
        c.inputs.map((i) => {
          const el = h('input', { value: '', placeholder: i.example || '', 'aria-label': i.question || i.key });
          refs[i.key] = el;
          return h('label', {}, i.question || i.key, el);
        }),
        h('div', { class: 'row-btns' },
          h('button', { class: 'tinted', onclick: () => startAgentRun(pack, refs) }, 'Start')))
      : null;
    const faceRefs = {};
    const faceForm = agentFaceOpen === c.name && !pack.disabled
      ? h('div', { class: 'agent-form' },
        h('label', {}, 'An emoji for this agent',
          (() => { const el = h('input', { value: '', placeholder: '📦', 'aria-label': 'Emoji for this agent', maxlength: '8' });
                   faceRefs.emoji = el; return el; })()),
        h('div', { class: 'row-btns' },
          h('button', { class: 'forget', onclick: () => saveAgentFace(pack, faceRefs.emoji.value) }, 'Save emoji'),
          h('button', { class: 'tinted', onclick: () => drawAgentFace(pack) }, 'Draw one (~$0.01)')))
      : null;
    return h('article', { class: 'task agent' },
      h('div', { class: 'head' },
        agentFaceThumb(pack, face),
        h('div', {},
          h('div', { class: 'goal' }, c.title, c.disabled ? h('span', { class: 'chip' }, 'off') : null),
          c.sub ? h('div', { class: 'status' }, c.sub) : null)),
      c.description ? h('p', { class: 'reply' }, c.description) : null,
      c.tools.length ? h('div', { class: 'agent-tools' }, c.tools.map((t) => h('span', { class: 'chip' }, t))) : null,
      run ? h('div', { class: 'agent-run' },
        h('span', {}, run.live ? `Running as task #${run.id}` : `Last run: #${run.id} · ${run.status}`),
        h('div', { class: 'row-btns' },
          h('button', { class: 'tinted', onclick: () => showTab('tasks') }, 'Watch'),
          run.live ? h('button', { class: 'stop-big', onclick: stopEverything }, 'Stop') : null)) : null,
      form,
      faceForm,
      h('div', { class: 'actions' },
        c.runnable && agentRunOpen !== c.name
          ? h('button', { class: 'forget', onclick: () => { agentRunOpen = c.name; renderAgents(); } }, 'Run') : null,
        !pack.disabled && agentFaceOpen !== c.name
          ? h('button', { class: 'forget', onclick: () => { agentFaceOpen = c.name; renderAgents(); } },
            face.kind === 'none' ? 'Make a face' : 'Change face') : null,
        h('button', { class: 'forget', onclick: () => shareAgent(pack) }, 'Share'),
        c.disabled
          ? h('button', { class: 'forget', onclick: () => setAgentEnabled(pack, true) }, 'Enable')
          : h('button', { class: 'forget', onclick: () => setAgentEnabled(pack, false) }, 'Disable')));
  }

  async function saveAgentFace(pack, text) {
    const parsed = LanternAgents.parseEmoji(text);
    if (!parsed.ok) {
      toast(parsed.error);
      return;
    }
    try {   // an emoji is free: no model call, no approval
      await api.post(`/agents/${encodeURIComponent(pack.name)}/face`, { emoji: parsed.emoji });
      agentFaceOpen = null;
      toast(`“${pack.title || pack.name}” now looks like ${parsed.emoji}.`);
    } catch (e) {
      toast(`Couldn't set it: ${e.message}`);
      return;
    }
    loadAgents();
  }

  async function drawAgentFace(pack) {
    try {   // one Muse Image icon, behind an approval showing the cost
      const r = await api.post(`/agents/${encodeURIComponent(pack.name)}/face`, { generate: true });
      agentFaceOpen = null;
      toast(r.made ? `Drew a face for “${pack.title || pack.name}”.` : `Not drawn: ${r.reason || 'declined'}.`);
    } catch (e) {
      toast(`Couldn't draw it: ${e.message}`);
      return;
    }
    loadAgents();
  }

  async function startAgentRun(pack, refs) {
    const values = {};
    for (const i of pack.inputs || []) values[i.key] = refs[i.key] ? refs[i.key].value : '';
    const payload = LanternAgents.runPayload(pack, values);
    if (!payload.ok) {
      toast(payload.error);
      return;
    }
    try {
      const r = await api.post(`/agents/${encodeURIComponent(pack.name)}/run`, { inputs: payload.inputs });
      agentRunTask[pack.name] = r.task_id;
      agentRunOpen = null;
      renderAgents();
      showTab('tasks');   // the live timeline is the Today task view; Stop is in the header
    } catch (e) {
      toast(`Couldn't start it: ${e.message}`);
    }
  }

  async function setAgentEnabled(pack, on) {
    try {
      await api.post(`/agents/${encodeURIComponent(pack.name)}/${on ? 'enable' : 'disable'}`);
      loadAgents();
    } catch (e) {
      toast(`Couldn't change it: ${e.message}`);
    }
  }

  async function shareAgent(pack) {
    if (!window.lantern.saveAgentFile) {
      toast('Sharing needs the newest Lantern app.');
      return;
    }
    try {
      const text = await api.raw(`/agents/${encodeURIComponent(pack.name)}/export`);   // the exact file bytes
      const r = await window.lantern.saveAgentFile({ name: LanternAgents.packFileName(pack), text });
      if (r && !r.cancelled) toast(`Saved ${r.path || 'the agent file'}.`);
    } catch (e) {
      toast(`Couldn't share it: ${e.message}`);
    }
  }

  // ---- install: the Add… button, or a file dropped on the row -----------------------------------
  // Returns true when the pack was installed (so the draft editor can clear itself).
  async function installPackText(text, replace) {
    const parsed = LanternAgents.parsePackFile(text);
    if (!parsed.ok) {
      toast(parsed.error);
      return false;
    }
    try {
      const r = await api.post(`/agents/install${replace ? '?replace=true' : ''}`, parsed.data);
      if (r.installed) {
        toast(`Agent “${r.name}” installed.`);
        loadAgents();
        return true;
      }
      if (!replace && /already installed|take its place/i.test(r.reason || '')) {
        if (window.confirm(`${r.reason} Install anyway, replacing it?`)) return installPackText(text, true);
        return false;
      }
      toast(`Couldn't install it: ${r.reason || 'the server refused it'}`);
      return false;
    } catch (e) {
      toast(`Couldn't install it: ${e.message}`);   // a bad pack explains itself here
      return false;
    }
  }

  async function pickAgentFile() {
    if (window.lantern.openAgentFile) {
      const r = await window.lantern.openAgentFile();
      if (r && !r.cancelled) installPackText(r.text || '');
      return;
    }
    $('agent-file').click();   // older app: the hidden file input
  }

  $('agent-add').addEventListener('click', pickAgentFile);
  $('agent-file').addEventListener('change', (e) => {
    const f = e.target.files[0];
    e.target.value = '';
    if (f) f.text().then((t) => installPackText(t));
  });
  for (const ev of ['dragenter', 'dragover']) {
    $('agent-drop').addEventListener(ev, (e) => { e.preventDefault(); $('agent-drop').classList.add('over'); });
  }
  for (const ev of ['dragleave', 'drop']) {
    $('agent-drop').addEventListener(ev, (e) => { e.preventDefault(); $('agent-drop').classList.remove('over'); });
  }
  $('agent-drop').addEventListener('drop', (e) => {
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) f.text().then((t) => installPackText(t));
  });

  // ---- draft: turn a finished task into a new agent ----------------------------------------------
  async function draftFromTask(id) {
    let draft;
    try {
      draft = await api.post('/agents/draft', { task_id: id });
    } catch (e) {
      toast(`Couldn't draft it: ${e.message}`);
      return;
    }
    agentDraft = { fields: LanternAgents.draftFields(draft), from: id };
    agentRunOpen = null;
    showTab('agents');
    renderAgents();
    renderAgentDraft();
    window.scrollTo({ top: 0 });
  }

  function draftField(label, key, rows) {
    const el = rows ? h('textarea', { rows }, draftRefs[key] || agentDraft.fields[key] || '')
                    : h('input', { value: agentDraft.fields[key] || '' });
    draftRefs[key] = el;
    return h('label', {}, label, el);
  }

  function renderAgentDraft() {
    const box = $('agent-draft');
    box.textContent = '';
    draftRefs = {};
    if (!agentDraft) return;
    const inputs = agentDraft.fields.inputs || [];
    box.append(h('article', { class: 'task agent draft-form' },
      h('div', { class: 'head' }, h('div', {},
        h('div', { class: 'goal' }, 'New agent'),
        h('div', { class: 'status' }, `Drafted from task #${agentDraft.from} · edit it, then install`))),
      h('div', { class: 'agent-form' },
        draftField('Name (lowercase, dashes)', 'name'),
        draftField('Title', 'title'),
        draftField('What it does', 'description'),
        draftField('Author', 'author'),
        draftField('Version', 'version'),
        draftField('Goal (use {blanks} for the fill-ins)', 'goal', 3),
        draftField('Instructions', 'instructions', 4),
        draftField('Tools (one per line)', 'tools', 3)),
      inputs.length ? h('div', { class: 'agent-tools' },
        inputs.map((i) => h('span', { class: 'chip' }, `{${i.key}} ${i.question}`))) : null,
      h('p', { class: 'hint' }, 'Installing asks first, bound to the exact file. The draft never picks a model.'),
      h('div', { class: 'row-btns' },
        h('button', { class: 'tinted', onclick: saveAgentDraft }, 'Review & install'),
        h('button', { class: 'forget', onclick: () => { agentDraft = null; renderAgentDraft(); } }, 'Discard'))));
  }

  async function saveAgentDraft() {
    if (!agentDraft) return;
    for (const key of ['name', 'title', 'description', 'author', 'version', 'goal', 'instructions', 'tools']) {
      agentDraft.fields[key] = draftRefs[key] ? draftRefs[key].value : '';
    }
    const data = LanternAgents.packFromFields({ ...agentDraft.fields, inputs: agentDraft.fields.inputs });
    const replace = agentPacks.some((p) => p.name === data.name);
    if (replace && !window.confirm(`An agent called “${data.name}” is already installed. Replace it?`)) return;
    if (await installPackText(JSON.stringify(data), replace || undefined)) {
      agentDraft = null;
      renderAgentDraft();
    }
  }

  async function stopEverything() {
    const r = await window.lantern.stop();
    toast(r && r.stopped ? `Stopped. ${r.denied_approvals || 0} approval(s) denied.` : 'Could not reach Lantern to stop it.');
  }

  // ---- live events -------------------------------------------------------------------------------------------
  function renderAll() {
    renderApprovals();
    renderTasks();
  }

  api.events((e) => {
    state = LanternState.reduce(state, { ts: now(), ...e });
    renderAll();
    // A tracked agent run finished or stopped: refresh its Watch/Stop line, unless the
    // user is mid-form (re-rendering would wipe what they typed).
    if ((e.type === 'done' || e.type === 'stopped') && tab === 'agents' && !agentRunOpen && !agentDraft) {
      renderAgents();
    }
    if (e.type === 'approval' && e.event === 'requested') window.lantern.showPanel();
    if (e.type === 'done' && tab === 'privacy') loadPrivacy();
    if (tab === 'phone' && (e.type === 'approval' || e.type === 'done' || e.type === 'queued' || e.type === 'reminder')) setTimeout(loadPhone, 300);
    if (e.type === 'reminder') toast(`⏰ ${e.text}`);
    if (e.type === 'reminder' || e.type === 'reminders') loadReminders();
    if (e.type === 'budget') toast(`Lantern has spent ${LanternAudit.money(e.spent_usd)} of its ${LanternAudit.money(e.limit_usd)} limit.`);
    if (e.type === 'done' || e.type === 'budget') loadCosts();
    if (e.type === 'avatar' && tab === 'setup') loadAvatar();
  }, async (up) => {
    $('conn').textContent = up ? (cfg.mock ? 'Connected · mock' : 'Connected') : 'Offline: is `lantern --serve` running?';
    $('conn').className = 'status ' + (up ? 'up' : 'down');
    if (up) {
      try { health = await api.get('/health'); } catch (e) { health = {}; }
      if (health.demo) $('conn').textContent = 'Rehearsal · scripted model, nothing sent';
      loadReminders();
      loadCosts();
    }
  });

  window.lantern.onStopped(() => { state = LanternState.reduce(state, { type: 'stopped', ts: now() }); renderAll(); });
  setInterval(renderApprovals, 1000);   // countdowns
  setInterval(() => { if (tab === 'phone') loadPhone(); }, 3000);
  renderAll();
})();
