// The panel: tasks with model badges, approval cards, privacy view, simulated phone, costs, widgets.
// Everything from the server or a web page is shown with textContent, never as HTML.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  let state = LanternState.initial();
  let health = {};
  const now = () => Date.now() / 1000;

  // Lantern's look in the panel: its Muse-made face by default (desktop/assets/avatar); with the orb look, a live
  // orb in the sidebar and on Today instead, following what Lantern is doing. Under the greeting, a line says what
  // Lantern is working on right now, whatever the look.
  const face = ((window.LanternAvatarManifest || {}).expressions || {}).idle;
  const MOODS = /\b(idle|thinking|juggling|smug|celebrating|needs-approval|annoyed|sleeping|healed|listening)\b/g;
  let orbs = [];
  let orbShown = '';
  function showOrbs(on) {   // swap the sidebar mark and the Today face between the portrait and live orbs
    orbs = [];
    orbShown = '';
    for (const [el, size] of [[$('mark'), 'mini'], [document.querySelector('.chat-face'), 'hero-orb']]) {
      if (!el) continue;
      el.replaceChildren();
      el.classList.toggle('orb-host', on);
      if (!on) { el.append(lanternFace()); continue; }   // the Muse portrait, or the lantern icon without one
      const orb = h('div', { class: `sprite ${size} idle`, role: 'img', 'aria-label': 'Lantern is ready' });
      LanternSprite.build(orb);
      el.append(orb);
      orbs.push(orb);
    }
  }
  function renderOrb() {
    const t = now();
    const mood = LanternExpression.pick(state, t);
    if (orbs.length && mood !== orbShown) {
      orbShown = mood;
      for (const o of orbs) {
        o.className = `${o.className.replace(MOODS, '').trim()} ${mood}`;
        o.setAttribute('style', LanternSprite.style(mood));
        o.setAttribute('aria-label', LanternSprite.LABELS[mood] || 'Lantern');
      }
    }
    const status = $('hero-status');
    if (!status) return;
    const a = LanternActivity.activity(state, t);
    const line = a.mode === 'idle' ? (mood === 'sleeping' ? 'Resting. Ask me anything.' : 'Ready when you are.')
      : a.mode === 'working' ? `${a.text}${a.badge ? ' · ' + a.badge : ''}` : a.text;
    if (status.textContent !== line) status.textContent = line;
    status.dataset.mode = a.mode;
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
  const loaders = { privacy: loadPrivacy, phone: loadPhone, costs: loadCosts, learn: loadRoutines, teach: loadLessons,
                    widgets: loadWidgets, reminders: () => loadReminders(), setup: () => { loadSettings(); loadPairing(); loadDoctor(); } };
  if (cfg.platform === 'darwin') document.body.classList.add('mac');   // traffic lights over the sidebar; vibrancy
  const tabButtons = [...document.querySelectorAll('.tabs button')];
  let tab = 'tasks';
  document.body.classList.add('chatting');
  function showTab(name) {
    tab = name;
    tabButtons.forEach((x) => { x.classList.toggle('on', x.dataset.tab === name); x.setAttribute('aria-selected', String(x.dataset.tab === name)); });
    document.querySelectorAll('main > section').forEach((s) => s.classList.toggle('on', s.dataset.panel === name));
    const b = tabButtons.find((x) => x.dataset.tab === name);
    $('section-title').textContent = b ? b.querySelector('span').textContent.trim() : 'Lantern';
    document.title = `Lantern · ${$('section-title').textContent}`;
    document.body.classList.toggle('chatting', name === 'tasks');
    const main = document.querySelector('main');
    main.scrollTo({ top: name === 'tasks' ? main.scrollHeight : 0 });   // a conversation opens at its latest message
    if (loaders[name]) loaders[name]();
  }
  for (const b of tabButtons) b.addEventListener('click', () => showTab(b.dataset.tab));
  // Ctrl/Cmd+1…9 go to the first nine sections, in sidebar order.
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
        h('div', { class: 'meta' }, h('span', {}, `#${c.id}`), c.tierName ? h('span', { class: `tier ${c.tier}` }, c.tierName) : null,
          h('span', {}, c.expired ? 'expired' : `expires in ${c.expiresIn}`)),
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

  // ---- questions from parked tasks (ask_user): answering one lets that task continue -------------------
  let shownQuestions = '';
  async function answer(q, text) {
    if (!String(text || '').trim()) return;
    try {
      await api.post(`/tasks/${q.task_id}/answer`, { answer: String(text).trim() });
      toast(`Thanks. Task #${q.task_id} continues.`);
    } catch (e) {
      toast(`Couldn't answer: ${e.message}`);
    }
  }

  function renderQuestions() {
    const qs = state.questions || [];
    const key = JSON.stringify(qs.map((q) => [q.task_id, q.question]));
    if (key === shownQuestions) return;   // keep what's being typed: only redraw when the questions change
    shownQuestions = key;
    const box = $('questions');
    box.textContent = '';
    for (const q of qs) {
      const input = h('input', { type: 'text', class: 'answer', 'aria-label': `Answer for task #${q.task_id}`,
                                 placeholder: 'Your answer' });
      input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') answer(q, input.value); });
      box.append(h('div', { class: 'approval question', role: 'group', 'aria-label': `Task #${q.task_id} asks` },
        h('div', { class: 'meta' }, h('span', {}, `#${q.task_id}`), h('span', {}, 'waiting for you · other tasks keep going')),
        h('h3', {}, q.question),
        h('p', { class: 'summary' }, q.goal || ''),
        q.tainted ? h('div', { class: 'warn untrusted' }, 'Asked after reading untrusted content (a file, web page or email).') : null,
        (q.options || []).length
          ? h('div', { class: 'row-btns' }, q.options.map((o) => h('button', { class: 'allow', onclick: () => answer(q, o) }, o)))
          : null,
        h('div', { class: 'row-btns' }, input, h('button', { class: 'allow', onclick: () => answer(q, input.value) }, 'Answer'))));
    }
  }

  // ---- learned routines (S4): offers to learn a task, and the list with cost per run ------------------
  const offers = new Map();   // task id -> goal, offered after a verified task

  async function learn(taskId) {
    offers.delete(taskId);
    renderOffers();
    try {
      const r = await api.post('/routines/learn', { task_id: taskId });
      toast(`Learning it as a routine (task #${r.task_id}).`);
    } catch (e) {
      toast(`Couldn't learn that one: ${e.message}`);
    }
  }

  function renderOffers() {
    const box = $('offers');
    box.textContent = '';
    for (const [taskId, goal] of offers) {
      box.append(h('div', { class: 'approval offer', role: 'group', 'aria-label': `Learn task #${taskId}` },
        h('div', { class: 'meta' }, h('span', {}, `#${taskId}`), h('span', {}, 'done and checked')),
        h('h3', {}, 'Learn this?'),
        h('p', { class: 'summary' }, `${goal} — next time it runs as a small program, with no model calls.`),
        h('div', { class: 'row-btns' },
          h('button', { class: 'deny', onclick: () => { offers.delete(taskId); renderOffers(); } }, 'Not now'),
          h('button', { class: 'allow', onclick: () => learn(taskId) }, 'Learn this'))));
    }
  }

  // ---- teach once by demonstration (S8): the start form and the "watching you" card -------------------
  let teaching = null;   // LanternTeach session while Lantern records

  function renderTeach() {
    const box = $('teaching');
    box.textContent = '';
    const goal = teaching && state.tasks[teaching.taskId] ? state.tasks[teaching.taskId].goal : '';
    const c = LanternTeach.card(teaching, goal);
    if (!c) return;
    const note = h('input', { type: 'text', placeholder: 'Say something about a field (optional)', 'aria-label': 'Note' });
    box.append(h('div', { class: 'approval teach', role: 'group', 'aria-label': c.title },
      h('div', { class: 'meta' }, h('span', {}, `#${teaching.taskId}`), h('span', {}, c.count),
        c.notes ? h('span', {}, c.notes) : null),
      h('h3', {}, c.title),
      h('p', { class: 'summary' }, c.text),
      h('div', { class: 'row-btns' }, note,
        h('button', { class: 'deny', onclick: () => teachNote(note) }, 'Add note'),
        h('button', { class: 'allow', onclick: teachDone }, 'Done'))));
  }

  async function teachStart(ev) {
    ev.preventDefault();
    const url = $('teach-url').value.trim(), goal = $('teach-goal').value.trim();
    const problem = LanternTeach.check(url, goal);
    if (problem) return toast(problem);
    try {
      const r = await api.post('/teach', { url, goal });
      toast(`Lantern's browser is opening (task #${r.task_id}). Do the task once, then press Done.`);
    } catch (e) {
      toast(`Couldn't start: ${e.message}`);
    }
  }

  async function teachDone() {
    try { await api.post('/teach/stop', { task_id: teaching ? teaching.taskId : null }); } catch (e) { toast(e.message); }
  }

  async function teachNote(input) {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    try { await api.post('/teach/note', { task_id: teaching ? teaching.taskId : null, text }); } catch (e) { toast(e.message); }
  }

  // ---- lessons on the board and Check my work (S16) ---------------------------------------------------------
  async function loadLessons() {
    let list;
    try { list = await api.get('/lessons'); } catch (e) { return; }
    const box = $('lessons');
    box.textContent = '';
    const rows = LanternLessons.rows(list);
    if (!rows.length) box.append(h('div', { class: 'row' }, h('span', { class: 'sub' }, 'No lessons yet.')));
    for (const r of rows) {
      box.append(h('div', { class: 'row' }, h('div', {}, h('div', {}, r.title), h('div', { class: 'sub' }, r.line)),
        h('button', { class: 'tinted', onclick: () => openLesson(r.name) }, 'Open board')));
    }
  }

  async function openLesson(name) {
    try {
      const data = await api.get(`/lessons/${encodeURIComponent(name)}`);
      if (!(await window.lantern.openBoard(data.lesson, name))) toast("The board couldn't open that lesson.");
    } catch (e) {
      toast(`Couldn't open the board: ${e.message}`);
    }
  }

  async function lessonStart(ev) {
    ev.preventDefault();
    const request = $('lesson-request').value.trim(), notes = $('lesson-notes').value.trim();
    const problem = LanternLessons.check(request);
    if (problem) return toast(problem);
    try {
      const r = await api.post('/lesson', { request, notes });
      toast(`Planning the lesson (task #${r.task_id}); the board opens when every step is checked.`);
    } catch (e) {
      toast(`Couldn't start: ${e.message}`);
    }
  }

  async function checkworkStart(ev) {
    ev.preventDefault();
    const file = $('checkwork-file').files[0];
    const problem = LanternLessons.photoCheck(file);
    if (problem) return toast(problem);
    const image = await new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = () => reject(fr.error);
      fr.readAsDataURL(file);
    });
    try {
      const r = await api.post('/checkwork', { image });
      toast(`Checking it line by line (task #${r.task_id}).`);
    } catch (e) {
      toast(`Couldn't check it: ${e.message}`);
    }
  }

  async function showCheckwork(e) {
    const r = LanternLessons.result(e);
    if (!r) return;
    const box = $('checkwork-result');
    box.textContent = '';
    box.hidden = false;
    box.append(h('div', { class: `row checkwork ${r.tone}` }, h('span', {}, r.text)));
    if (r.image) {
      try {
        box.append(h('img', { class: 'checkwork-photo', alt: 'Your working, with the first wrong line circled',
                              src: await api.dataURL(`/checkwork/image/${encodeURIComponent(r.image)}`) }));
      } catch (err) { /* the reply already says which line */ }
    }
  }

  // ---- resume → profile (S15): a proposal; only what the user ticks is saved -----------------------------------
  async function cvRead(ev) {
    ev.preventDefault();
    const path = $('cv-path').value.trim();
    if (!path) return toast('Give the path of your resume (a PDF or a text file).');
    let proposal;
    try {
      proposal = await api.post('/profile/resume', { path });
    } catch (e) {
      return toast(`Couldn't read it: ${e.message}`);
    }
    const list = LanternCV.rows(proposal);
    const box = $('cv-proposal');
    box.textContent = '';
    box.hidden = false;
    if (!list.length) return box.append(h('div', { class: 'row' }, h('span', { class: 'sub' }, 'Nothing to add from that file.')));
    const ticked = new Set(list.filter((r) => r.checked).map((r) => r.key));
    for (const r of list) {
      const id = `cv-${r.key.replace(/\W+/g, '-')}`;
      box.append(h('label', { class: 'row', for: id },
        h('input', { type: 'checkbox', id, checked: r.checked,
                     onchange: (e) => (e.target.checked ? ticked.add(r.key) : ticked.delete(r.key)) }),
        h('div', {}, h('div', {}, `${r.key}: ${r.value}`), h('div', { class: 'sub' }, r.note))));
    }
    if (proposal.dropped) box.append(h('div', { class: 'row' }, h('span', { class: 'sub' }, `Left out ${proposal.dropped} ID or card number${proposal.dropped === 1 ? '' : 's'}.`)));
    box.append(h('div', { class: 'row' }, h('button', { class: 'tinted', onclick: async () => {
      const bodies = LanternCV.saves(list, ticked);
      try {
        for (const b of bodies) await api.post('/profile', b);
        toast(`Saved ${bodies.length} value${bodies.length === 1 ? '' : 's'} to your profile.`);
        box.hidden = true;
      } catch (e) {
        toast(`Couldn't save: ${e.message}`);
      }
    } }, 'Save ticked')));
  }

  // ---- live talk (S15): hold the mic, speak, let go; what was heard goes in the ask field and is sent ----------
  let talking = null;
  async function talkStart() {
    if (talking) return;
    const btn = $('talk');
    const live = $('talk-live');
    const session = { ws: null, ctx: null, stream: null, chunks: LanternTalk.chunker(), ended: false };
    talking = session;
    btn.classList.add('on');
    btn.setAttribute('aria-pressed', 'true');
    live.hidden = false;
    live.textContent = 'Listening…';
    try {
      await window.lantern.allowMicOnce();
      session.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      session.ctx = new AudioContext({ sampleRate: LanternTalk.RATE });
      await session.ctx.audioWorklet.addModule('lib/pcm-worklet.js');
      const tap = new AudioWorkletNode(session.ctx, 'pcm-tap');
      session.ctx.createMediaStreamSource(session.stream).connect(tap);
      session.ws = new WebSocket(LanternTalk.url(cfg.apiBase, cfg.token));
      session.ws.binaryType = 'arraybuffer';
      session.ws.onmessage = (m) => {
        let msg = {};
        try { msg = JSON.parse(m.data); } catch (e) { return; }
        const r = LanternTalk.reading(msg);
        if (r.live !== undefined) live.textContent = r.live || 'Listening…';
        if (r.error) { toast(`Live talk: ${r.error}`); talkClose(session); }
        if (msg.type === 'done') {
          talkClose(session);
          if (r.final) { $('goal').value = r.final; $('ask').requestSubmit(); }
          else toast('I didn\'t catch anything. Hold the mic while you speak.');
        }
      };
      session.ws.onclose = () => talkClose(session);
      tap.port.onmessage = (ev) => {
        if (session.ended || !session.ws || session.ws.readyState !== WebSocket.OPEN) return;
        for (const chunk of session.chunks.push(ev.data)) session.ws.send(chunk);
      };
    } catch (e) {
      toast(`Live talk needs the microphone: ${e.message || e}`);
      talkClose(session);
    }
  }
  function talkStop() {   // the button was let go: send what's left and say the turn is over
    const session = talking;
    if (!session || session.ended) return;
    session.ended = true;
    $('talk-live').textContent = 'Got it…';
    if (session.ws && session.ws.readyState === WebSocket.OPEN) {
      const rest = session.chunks.flush();
      if (rest) session.ws.send(rest);
      session.ws.send(JSON.stringify({ type: 'end' }));
    } else talkClose(session);
    if (session.stream) session.stream.getTracks().forEach((t) => t.stop());
  }
  function talkClose(session) {
    if (session.stream) session.stream.getTracks().forEach((t) => t.stop());
    if (session.ctx) session.ctx.close().catch(() => {});
    if (session.ws && session.ws.readyState <= WebSocket.OPEN) session.ws.close();
    if (talking === session) {
      talking = null;
      $('talk').classList.remove('on');
      $('talk').setAttribute('aria-pressed', 'false');
      $('talk-live').hidden = true;
    }
  }
  $('talk').addEventListener('pointerdown', (e) => { e.preventDefault(); talkStart(); });
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) $('talk').addEventListener(ev, talkStop);
  $('talk').addEventListener('keydown', (e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); talkStart(); } });
  $('talk').addEventListener('keyup', (e) => { if (e.key === ' ' || e.key === 'Enter') talkStop(); });

  // ---- meeting recording → follow-ups (S15): nothing is added until ticked ---------------------------------------
  async function meetingListen(ev) {
    ev.preventDefault();
    const path = $('meeting-path').value.trim();
    if (!path) return toast('Give the path of a recording in your folders.');
    const go = $('meeting-go');
    go.disabled = true;
    go.textContent = 'Listening…';
    let result;
    try {
      result = await api.post('/meeting', { path });
    } catch (e) {
      return toast(`Couldn't read that recording: ${e.message}`);
    } finally {
      go.disabled = false;
      go.textContent = 'Listen';
    }
    const list = LanternMeeting.items(result);
    const ticked = new Set(list.map((i) => i.n));
    const box = $('meeting-items');
    box.textContent = '';
    if (!list.length) box.append(h('p', { class: 'empty' }, 'Nobody agreed to do anything in that recording.'));
    for (const i of list) {
      const id = `meeting-${i.n}`;
      box.append(h('label', { class: 'row meeting-item', for: id },
        h('input', { type: 'checkbox', id, checked: i.checked,
                     onchange: (e) => (e.target.checked ? ticked.add(i.n) : ticked.delete(i.n)) }),
        h('div', { class: 'row-text' }, h('span', {}, i.text),
          h('small', {}, `Speaker ${i.speaker}${i.when ? ' · ' + i.when : ''}`)),
        h('span', { class: `kind ${i.kind}` }, i.kind === 'reminder' ? 'Reminder' : 'Task')));
    }
    const turnsBox = $('meeting-turns');
    turnsBox.textContent = '';
    for (const t of LanternMeeting.turns(result)) {
      turnsBox.append(h('div', { class: 'row turn' }, h('span', { class: `who s${t.slot}`, 'aria-label': `Speaker ${t.who}` }, t.who),
        h('div', { class: 'row-text' }, h('time', {}, t.at), h('p', {}, t.text))));
    }
    $('meeting-note').textContent = LanternMeeting.summary(result);
    $('meeting-confirm').hidden = !list.length;
    $('meeting-confirm').onclick = async () => {
      try {
        const done = await api.post('/meeting/confirm', LanternMeeting.confirm(result, ticked));
        toast(`Added ${done.reminders.length} reminder${done.reminders.length === 1 ? '' : 's'} and started ` +
              `${done.tasks.length} task${done.tasks.length === 1 ? '' : 's'}.`);
        $('meeting-result').hidden = true;
        if (done.tasks.length) showTab('tasks');
      } catch (e) {
        toast(`Couldn't add them: ${e.message}`);
      }
    };
    $('meeting-result').hidden = false;
  }
  $('meeting-form').addEventListener('submit', meetingListen);

  // ---- decoy screens (S7): what you see / what the cloud saw ------------------------------------------------
  async function showDecoy(e) {
    const ok = /^decoy_\d{8}_\d{6}_[0-9a-f]{6}-(seen|sent)\.jpg$/;
    if (!ok.test(String(e.seen)) || !ok.test(String(e.sent))) return;
    try {
      $('decoy-seen').src = await api.dataURL(`/decoy/image/${encodeURIComponent(e.seen)}`);
      $('decoy-sent').src = await api.dataURL(`/decoy/image/${encodeURIComponent(e.sent)}`);
    } catch (err) {
      return;
    }
    const n = Number(e.replaced) || 0;
    $('decoy-note').textContent = n ? `${n} of your value${n === 1 ? '' : 's'} showed as stand-ins; Lantern typed the real ones here.`
      : 'None of your values was on this screen.';
    $('decoy-view').hidden = false;
  }

  function routineChart(r) {   // SVG through DOM calls only
    const NS = 'http://www.w3.org/2000/svg';
    const d = LanternRoutines.chartData(r);
    const svg = document.createElementNS(NS, 'svg');
    for (const [k, v] of [['class', 'routine-chart'], ['viewBox', `0 0 ${d.w} ${d.h}`], ['width', d.w], ['height', d.h],
                          ['role', 'img'], ['aria-label', d.label]]) svg.setAttribute(k, String(v));
    for (const b of d.bars) {
      const rect = document.createElementNS(NS, 'rect');
      for (const [k, v] of [['class', b.cls], ['x', b.x], ['y', b.y], ['width', b.w], ['height', b.h], ['rx', 2]]) {
        rect.setAttribute(k, String(v));
      }
      svg.append(rect);
      if (b.heal) {
        const mark = document.createElementNS(NS, 'path');
        mark.setAttribute('class', 'heal');
        mark.setAttribute('d', b.heal);
        svg.append(mark);
      }
    }
    return svg;
  }

  async function loadRoutines() {
    const box = $('routines');
    let list = [];
    try { list = await api.get('/routines'); } catch (e) { list = []; }
    box.textContent = '';
    if (!list.length) {
      box.append(h('p', { class: 'row muted' }, 'Nothing learned yet. After a task finishes and checks out, choose “Learn this”.'));
      return;
    }
    for (const r of list) {
      const chart = h('div', { class: 'chart' });
      chart.append(routineChart(r));
      box.append(h('div', { class: 'row routine' },
        h('div', { class: 'grow' }, h('div', { class: 'title' }, r.title || r.name),
          h('div', { class: 'sub' }, LanternRoutines.line(r))), chart));
    }
  }

  // ---- tasks ------------------------------------------------------------------------------------
  const STATE_MARK = { done: '✓', failed: '!', cancelled: '✕', interrupted: '‖', resumed: '↻' };
  const openSteps = new Set();   // tasks whose steps the user expanded

  // Today is a conversation: what you asked on the right, Lantern's answer on the left, oldest first, the
  // composer pinned below. Everything else lives in its own section.
  let lastThread = '';
  function lanternFace() {
    return face && /\.(webp|png)$/.test(face.file)
      ? h('img', { src: `../assets/avatar/${face.file}`, alt: '' })
      : h('svg', { class: 'ico' }, h('use', { href: '#i-lantern' }));
  }

  function renderTasks() {
    const main = document.querySelector('main');
    const atBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 80;
    const box = $('tasks');
    box.textContent = '';
    const tasks = LanternState.recentTasks(state, 20).reverse();
    $('chat-empty').hidden = tasks.length > 0 || state.interrupted.length > 0;
    if (state.interrupted.length) {
      const n = state.interrupted.length;
      box.append(h('div', { class: 'interrupted' },
        h('b', {}, `${n} task${n > 1 ? 's were' : ' was'} cut short when Lantern stopped`),
        h('ul', {}, state.interrupted.slice(0, 4).map((t) => h('li', {}, `#${t.id} ${t.goal}`))),
        h('button', { class: 'tinted', onclick: resumeAll }, 'Resume all')));
    }
    tasks.forEach((t, i) => {
      const live = t.status === 'running' || t.status === 'queued';
      const steps = h('details', { open: live || openSteps.has(t.id) || undefined,
                                   ontoggle: (e) => { if (!live) (e.target.open ? openSteps.add(t.id) : openSteps.delete(t.id)); } },
        h('summary', {}, `${t.steps.length} step${t.steps.length === 1 ? '' : 's'}`),
        h('ul', { class: 'steps' }, t.steps.slice(-12).map((s) => h('li', { class: s.ok ? '' : 'bad' }, chip(s.badge), h('span', {}, s.label)))));
      const said = t.reply || (live ? (t.steps.length ? t.steps[t.steps.length - 1].label + '…' : 'On it…') : '');
      box.append(h('article', { class: 'task' },
        h('div', { class: 'me' }, h('p', { class: 'goal' }, t.goal || `Task #${t.id}`)),
        h('div', { class: 'them' },
          h('span', { class: 'face', 'aria-hidden': 'true' }, lanternFace()),
          h('div', { class: 'bubble' },
            h('div', { class: 'head' },
              h('span', { class: `state ${t.status}`, 'aria-hidden': 'true' }, STATE_MARK[t.status] || ''),
              h('span', { class: 'status' }, `#${t.id} · ${t.status}${t.cost ? ' · ' + LanternAudit.money(t.cost) : ''}${t.verified ? ' · verified ✓' : ''}`)),
            said ? h('p', { class: t.reply ? 'reply' : 'reply working' }, said) : null,
            t.steps.length ? steps : null,
            h('div', { class: 'actions' },
              t.undoable && !t.undoneBy && !live
                ? h('button', { class: 'forget', onclick: () => undoTask(t.id), title: 'Put the files back (asks first)' }, 'Undo') : null,
              t.undoneBy ? h('span', { class: 'undone' }, `Undone in #${t.undoneBy}`) : null,
              ['failed', 'cancelled', 'interrupted'].includes(t.status)
                ? h('button', { class: 'forget', onclick: () => retryTask(t.id) }, t.status === 'interrupted' ? 'Resume' : 'Try again') : null,
              !live ? h('button', { class: 'forget', onclick: () => showReceipt(t.id) }, 'Receipt') : null,
              !live ? h('button', { class: 'forget quiet', onclick: () => forgetTask(t.id) }, 'Forget') : null)))));
    });
    // follow the conversation when you were already at its end, or when a new message arrived
    const sig = tasks.map((t) => `${t.id}:${t.status}:${t.steps.length}`).join(',');
    if (tab === 'tasks' && sig !== lastThread && (atBottom || !lastThread || tasks.length && !lastThread.includes(`${tasks[tasks.length - 1].id}:`))) {
      main.scrollTo({ top: main.scrollHeight });
    }
    lastThread = sig;
    $('today-cost').textContent = LanternAudit.money(state.usage.total);
    document.body.classList.toggle('busy', tasks.some((t) => t.status === 'running' || t.status === 'queued'));
    badge('badge-tasks', Object.keys(state.approvals || {}).length, 'waiting for you');
    renderGlance();
  }

  function badge(id, n, what) {
    const b = $(id);
    b.hidden = !n;
    b.textContent = n ? String(n) : '';
    b.setAttribute('aria-label', n ? `${n} ${what}` : '');
  }

  showOrbs(false);   // the portrait until the avatar settings say otherwise

  // ---- welcome (first launch) and preferences ------------------------------------------------------
  const keyName = (k) => k.replace('CommandOrControl', cfg.platform === 'darwin' ? '⌘' : 'Ctrl').replace(/Shift/, '⇧')
    .replace(/\+/g, ' ');
  if (cfg.askHotkey) {
    $('kbd-ask').textContent = keyName(cfg.askHotkey);
    $('welcome-ask').textContent = keyName(cfg.askHotkey);
    $('kbd-stop').textContent = keyName('CommandOrControl+Shift+X');
    $('kbd-stop-inline').textContent = keyName('CommandOrControl+Shift+X');
    $('kbd-tabs').textContent = keyName('CommandOrControl+1') + ' – ' + Math.min(9, tabButtons.length);
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
  }

  // ---- pair a phone (lantern/pairing.py) ----------------------------------------------------------
  async function loadPairing() {
    const box = $('pair-devices');
    if (!box) return;
    box.textContent = '';
    try {
      const { devices } = await api.get('/devices');
      if (!devices.length) box.append(h('p', { class: 'empty' }, 'No phones paired.'));
      for (const d of devices) {
        box.append(h('div', { class: 'row' },
          h('div', { class: 'row-text' }, h('b', {}, d.name || 'phone'),
            h('small', {}, d.revoked ? 'revoked' : `paired · ${d.id}`)),
          d.revoked ? null : h('button', { class: 'forget', onclick: () => revokeDevice(d) }, 'Revoke')));
      }
    } catch (e) { box.append(h('p', { class: 'empty' }, 'Pairing needs this Lantern.')); }
  }

  async function revokeDevice(d) {
    try {
      await api.post(`/devices/${d.id}/revoke`);
      toast('Phone unpaired.');
    } catch (e) {
      toast(`Couldn't revoke it: ${e.message}`);
    }
    loadPairing();
  }

  const pairBtn = $('pair-start');
  if (pairBtn) pairBtn.addEventListener('click', async () => {
    try {
      const r = await api.post('/pair/start');
      $('pair-code-row').hidden = false;
      $('pair-code').textContent = r.code.split('').join(' ');
      $('pair-url').textContent = `${r.url} · expires in ${Math.round(r.expires_in / 60)} min`;
      toast('Type the code on the phone.');
    } catch (e) {
      toast(`Couldn't start pairing: ${e.message}`);
    }
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
    badge('badge-reminders', items.length, items.length === 1 ? 'reminder' : 'reminders');
    if (!items.length) {
      box.append(h('p', { class: 'empty' }, 'No reminders. Try “remind me in 20 minutes to stretch” in Today.'));
      return;
    }
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

  $('teach-form').addEventListener('submit', teachStart);
  $('lesson-form').addEventListener('submit', lessonStart);
  $('checkwork-form').addEventListener('submit', checkworkStart);
  $('cv-form').addEventListener('submit', cvRead);

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

  // ---- live events -------------------------------------------------------------------------------------------
  function renderAll() {
    renderOrb();
    renderApprovals();
    renderQuestions();
    renderTasks();
  }

  api.events((e) => {
    state = LanternState.reduce(state, { ts: now(), ...e });
    renderAll();
    if ((e.type === 'approval' && e.event === 'requested') || (e.type === 'task_parked' && e.reason === 'waiting_user')) window.lantern.showPanel();
    if (e.type === 'done' && tab === 'privacy') loadPrivacy();
    if (tab === 'phone' && (e.type === 'approval' || e.type === 'done' || e.type === 'queued' || e.type === 'reminder')) setTimeout(loadPhone, 300);
    if (e.type === 'reminder') toast(`⏰ ${e.text}`);
    if (e.type === 'digest' && e.text) toast(e.text);   // S11: the morning digest after the nightly check
    if (e.type === 'reminder' || e.type === 'reminders') loadReminders();
    if (e.type === 'budget') toast(`Lantern has spent ${LanternAudit.money(e.spent_usd)} of its ${LanternAudit.money(e.limit_usd)} limit.`);
    if (e.type === 'done' || e.type === 'budget') loadCosts();
    if (e.type === 'learn_offer' && !e.auto) { offers.set(e.task_id, e.goal || ''); renderOffers(); }
    if (e.type === 'teach') {
      teaching = LanternTeach.apply(teaching, e);
      renderTeach();
      if (e.phase === 'recording') window.lantern.showPanel();
    }
    if (e.type === 'lesson') {   // S16: planned and checked; play it
      toast(`Lesson ready: ${e.title} (${e.note}).`);
      loadLessons();
      openLesson(e.name);
    }
    if (e.type === 'checkwork') showCheckwork(e);
    if (e.type === 'decoy') showDecoy(e);
    if (e.type === 'routine_learned' || e.type === 'routine_healed' || e.type === 'routines' ||
        (e.type === 'done' && tab === 'learn')) loadRoutines();
    if (e.type === 'routine_healed') toast(`The page had changed: healed ${(e.steps || []).length} step(s), now v${e.version}.`);
  }, async (up) => {
    $('conn').textContent = up ? (cfg.mock ? 'Connected · mock' : 'Connected') : 'Offline: is `lantern --serve` running?';
    $('conn').className = 'status ' + (up ? 'up' : 'down');
    if (up) {
      try { health = await api.get('/health'); } catch (e) { health = {}; }
      if (health.demo) {
        $('conn').textContent = 'Rehearsal mode';
        $('conn').title = 'Rehearsal: a scripted model; nothing is sent anywhere';
      }
      loadReminders();
      loadCosts();
      loadLessons();
    }
  });

  window.lantern.onStopped(() => { state = LanternState.reduce(state, { type: 'stopped', ts: now() }); renderAll(); });
  setInterval(renderApprovals, 1000);   // countdowns
  setInterval(renderOrb, 1000);          // done → ready, ready → resting
  setInterval(() => { if (tab === 'phone') loadPhone(); }, 3000);
  renderAll();
})();
