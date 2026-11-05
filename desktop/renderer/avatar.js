// The avatar overlay: Lantern's face plus the live activity island above it.
// Click-through everywhere except the face and the island. Server text is always set as text.
(async function () {
  const $ = (id) => document.getElementById(id);
  const spriteEl = $('sprite');
  const facesEl = $('faces');
  const island = $('island');
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  let state = LanternState.initial();
  let shown = '';
  let hovering = false;
  let deciding = false;

  const now = () => Date.now() / 1000;
  const LEAD = { approval: '!', done: '✓', failed: '!', stopped: '■', reminder: '⏰' };

  // Lantern's look (Settings → Avatar → Look, GET /settings "skin"; lib/skins.js):
  //   muse  its Muse-made face, in three fallbacks (LanternAvatar.pickSource): the design on the server
  //         (/avatar/manifest), then desktop/assets/avatar/manifest.js, then the glow. Bundled entries name
  //         animated loops (.webm, from Vibes / Muse Video) or images (.webp/.png, Muse Image); expressions
  //         without their own file show idle.
  //   orb   a glass sphere with light inside (lib/sprite.js + orb.css): built once, then a state change only
  //         swaps its class and colours, which glide from one state to the next.
  //   glow  the faceless stand-in; clock  a live analog clock (T4b).
  let portraits = {};
  let broken = false;
  let skin = 'muse';
  spriteEl.setAttribute('role', 'button');
  spriteEl.tabIndex = 0;

  async function loadSkin() {
    try {
      const st = await api.get('/settings');
      skin = LanternSkins.normalize(st && st.skin);
    } catch (e) {
      skin = 'muse';
    }
  }

  async function loadPortraits() {
    const bundled = (window.LanternAvatarManifest && window.LanternAvatarManifest.expressions) || {};
    let server = null;
    try { server = await api.get('/avatar/manifest'); } catch (e) { server = null; }
    const from = LanternAvatar.pickSource(server, window.LanternAvatarManifest);
    if (from === 'server') {
      // The server's files need the auth token, which an <img src> can't send: fetch each one and
      // show it from a blob: URL (allowed by avatar.html's CSP). Old blob URLs are released first.
      Object.values(portraits).forEach((u) => { if (String(u).startsWith('blob:')) URL.revokeObjectURL(u); });
      const entries = await Promise.all(Object.entries(server.expressions).map(async ([k, v]) => {
        try { return [k, URL.createObjectURL(await api.blob(v.file))]; } catch (e) { return null; }
      }));
      portraits = Object.fromEntries(entries.filter(Boolean));
    } else if (from === 'bundled') {
      portraits = Object.fromEntries(Object.entries(bundled).map(([k, v]) => [k, `../assets/avatar/${v.file}`]));
    } else {
      portraits = {};
    }
    broken = false;
    shown = '';   // the face may have changed: draw it again
  }

  let orbBuilt = false;
  function setSprite(expression, mode) {
    if (skin === 'clock') {   // the live clock redraws every second: never cached, no portraits
      spriteEl.className = 'sprite skin-clock clock';
      spriteEl.removeAttribute('style');
      spriteEl.innerHTML = LanternSkins.clockSvg(new Date(), LanternSkins.clockStyle(mode));
      spriteEl.setAttribute('aria-label', 'Lantern (a clock). Open Lantern');
      orbBuilt = false;
      shown = `clock:${expression}`;
      return;
    }
    const key = `${skin}:${expression}`;
    if (key === shown) return;
    shown = key;
    spriteEl.className = `sprite skin-${skin} ${expression}`;
    spriteEl.setAttribute('aria-label', `${LanternSprite.LABELS[expression] || 'Lantern'}. Open Lantern`);
    if (skin === 'orb') {
      if (!orbBuilt) LanternSprite.build(spriteEl);
      orbBuilt = true;
      spriteEl.setAttribute('style', LanternSprite.style(expression));
      return;
    }
    orbBuilt = false;
    spriteEl.removeAttribute('style');
    spriteEl.textContent = '';
    const src = skin === 'glow' ? null : portraits[expression] || portraits.idle;
    if (src && !broken) {
      const video = /\.(webm|mp4)$/i.test(src);
      const media = video ? document.createElement('video') : new Image();
      if (video) Object.assign(media, { autoplay: true, loop: true, muted: true, playsInline: true });
      media.className = 'portrait';
      media.setAttribute('aria-label', `Lantern looks ${expression}`);
      media.onerror = () => { broken = true; shown = ''; render(); };
      media.src = src;
      spriteEl.appendChild(media);
    } else {
      spriteEl.innerHTML = LanternSprite.svg(expression);   // our own trusted SVG, no user text inside
    }
  }

  let current = null;   // the activity on screen (for the buttons)

  // ---- running agents: one small face per pack task (T4c) ----------------------------------
  let facesKey = ' ';         // "task:pack,..." of what's on screen; repaint only when it changes
  let agentFaces = {};        // pack name -> {icon: url} | {emoji}

  async function loadAgentFaces() {
    try {
      const list = await api.get('/agents');
      const next = {};
      for (const a of (list.agents || [])) {
        if (!a || !a.name) continue;
        if (a.face && a.face.icon) {
          next[a.name] = { icon: `/agents/${encodeURIComponent(a.name)}/face` };
        } else if (a.face && a.face.emoji) {
          next[a.name] = { emoji: String(a.face.emoji) };
        }
      }
      agentFaces = next;
    } catch (e) {
      agentFaces = {};
    }
  }

  async function iconURL(path) {
    try {
      const blob = await api.blob(path);
      return URL.createObjectURL(blob);
    } catch (e) {
      return '';   // no icon on disk (yet): the letter stays
    }
  }

  function paintFaces(runs, key) {
    facesEl.textContent = '';
    facesEl.hidden = !runs.length;
    if (!runs.length) return;
    loadAgentFaces().then(() => {
      if (key !== facesKey) return;   // another task started or finished while loading: repaint, don't mix
      for (const r of runs) {
        const el = document.createElement('div');
        el.className = 'face';
        el.setAttribute('role', 'button');
        el.setAttribute('aria-label', `Agent ${r.pack}, task ${r.task_id}: open its timeline`);
        el.title = `${r.pack} · task ${r.task_id}: click for its timeline, right-click to stop`;
        const face = agentFaces[r.pack];
        if (face && face.emoji) el.textContent = face.emoji;   // server text, never HTML
        else el.textContent = String(r.pack).slice(0, 1).toUpperCase() || '?';
        if (face && face.icon) {
          iconURL(face.icon).then((url) => {
            if (!url || key !== facesKey || !el.isConnected) return;
            el.textContent = '';
            const img = document.createElement('img');
            img.alt = '';
            img.src = url;
            el.appendChild(img);
          });
        }
        const dot = document.createElement('span');
        dot.className = 'dot';
        dot.setAttribute('aria-hidden', 'true');
        el.appendChild(dot);
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          if (window.lantern.showTask) window.lantern.showTask(r.task_id);
          else window.lantern.showPanel();
        });
        el.addEventListener('contextmenu', (e) => { e.preventDefault(); e.stopPropagation(); window.lantern.stop(); });
        el.addEventListener('mouseenter', () => window.lantern.hover(true));
        el.addEventListener('mouseleave', () => window.lantern.hover(false));
        facesEl.appendChild(el);
      }
    });
  }

  function renderFaces() {
    const runs = state.agents || [];
    const key = runs.map((r) => `${r.task_id}:${r.pack}`).join(',');
    if (key === facesKey) return;
    facesKey = key;
    paintFaces(runs, key);
  }

  function render() {
    const t = now();
    const a = LanternActivity.activity(state, t);
    setSprite(LanternExpression.pick(state, t), a.mode);
    renderFaces();   // repaints only when the running-agents set changes
    current = a;
    island.hidden = a.mode === 'idle';
    if (island.hidden) return;
    island.dataset.mode = a.mode;
    $('island-lead').textContent = LEAD[a.mode] || '';
    $('island-text').textContent = a.text;
    $('island-badge').hidden = !a.badge;
    $('island-badge').textContent = a.badge || '';
    $('island-detail').textContent = a.detail || '';
    const buttons = $('island-buttons');
    buttons.hidden = a.mode !== 'approval';
    buttons.classList.toggle('review-only', a.mode === 'approval' && !a.quickAllow);
    const open = a.mode === 'approval' || a.mode === 'done' || a.mode === 'failed' || a.mode === 'reminder'
      || (hovering && !!a.detail);
    island.classList.toggle('open', open);
  }

  async function decide(approve) {
    if (!current || current.mode !== 'approval' || deciding) return;
    if (approve && !current.quickAllow) return;   // flagged approvals are only answered in the panel
    deciding = true;
    try {
      await api.post(`/approvals/${current.approval.id}`, LanternCards.decision(current.approval, approve));
    } catch (e) {
      window.lantern.showPanel();   // let the panel show what went wrong
    } finally {
      deciding = false;
    }
  }

  $('island-allow').addEventListener('click', (e) => { e.stopPropagation(); decide(true); });
  $('island-deny').addEventListener('click', (e) => { e.stopPropagation(); decide(false); });
  $('island-review').addEventListener('click', (e) => { e.stopPropagation(); window.lantern.showPanel(); });
  island.addEventListener('click', () => { if (!current || current.mode !== 'approval') window.lantern.togglePanel(); });

  api.events((e) => {
    state = LanternState.reduce(state, { ts: now(), ...e });
    if (e.type === 'avatar') loadPortraits().then(render);   // a new face was accepted: show it
    else if (e.type === 'settings') loadSkin().then(render);   // the look changed in the panel
    else render();
  }, (up) => { if (!up) state = LanternState.reduce(state, { type: 'disconnected', ts: now() }); render(); });

  window.lantern.onStopped(() => { state = LanternState.reduce(state, { type: 'stopped', ts: now() }); render(); });
  setInterval(render, 1000);
  setInterval(() => loadSkin().then(render), 30000);   // the panel saves the skin; pick it up shortly after
  loadSkin().then(() => loadPortraits()).then(render);

  // Click-through everywhere except the face, the agent faces and the island.
  for (const el of [spriteEl, facesEl, island]) {
    el.addEventListener('mouseenter', () => { window.lantern.hover(true); if (el === island) { hovering = true; render(); } });
    el.addEventListener('mouseleave', () => { window.lantern.hover(false); if (el === island) { hovering = false; render(); } });
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); window.lantern.avatarMenu(); });
  }
  spriteEl.addEventListener('click', () => window.lantern.togglePanel());
  spriteEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); window.lantern.togglePanel(); }
  });
})();
