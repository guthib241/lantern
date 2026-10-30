// The avatar overlay: Lantern's face plus the live activity island above it.
// Click-through everywhere except the face and the island. Server text is always set as text.
(async function () {
  const $ = (id) => document.getElementById(id);
  const spriteEl = $('sprite');
  const island = $('island');
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  let state = LanternState.initial();
  let shown = '';
  let hovering = false;
  let deciding = false;

  const now = () => Date.now() / 1000;
  const LEAD = { approval: '!', done: '✓', failed: '!', stopped: '■', reminder: '⏰' };

  // The face is Muse's, in three fallbacks (LanternAvatar.pickSource): the design on the
  // server (/avatar/manifest), then desktop/assets/avatar/manifest.js, then the built-in glow.
  // Bundled entries name animated loops (.webm, from Vibes / Muse Video) or images (.webp/.png,
  // Muse Image). Expressions without their own file show idle.
  let portraits = {};
  let broken = false;

  async function loadPortraits() {
    const bundled = (window.LanternAvatarManifest && window.LanternAvatarManifest.expressions) || {};
    let server = null;
    try { server = await api.get('/avatar/manifest'); } catch (e) { server = null; }
    const from = LanternAvatar.pickSource(server, window.LanternAvatarManifest);
    if (from === 'server') {
      portraits = Object.fromEntries(Object.entries(server.expressions).map(([k, v]) => [k, cfg.apiBase + v.file]));
    } else if (from === 'bundled') {
      portraits = Object.fromEntries(Object.entries(bundled).map(([k, v]) => [k, `../assets/avatar/${v.file}`]));
    } else {
      portraits = {};
    }
    broken = false;
    shown = '';   // the face may have changed: draw it again
  }

  function setSprite(expression) {
    if (expression === shown) return;
    shown = expression;
    spriteEl.className = 'sprite ' + expression;
    spriteEl.textContent = '';
    const src = portraits[expression] || portraits.idle;
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

  function render() {
    const t = now();
    setSprite(LanternExpression.pick(state, t));
    const a = LanternActivity.activity(state, t);
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
    else render();
  }, (up) => { if (!up) state = LanternState.reduce(state, { type: 'disconnected', ts: now() }); render(); });

  window.lantern.onStopped(() => { state = LanternState.reduce(state, { type: 'stopped', ts: now() }); render(); });
  setInterval(render, 1000);
  loadPortraits().then(render);

  // Click-through everywhere except the face and the island.
  for (const el of [spriteEl, island]) {
    el.addEventListener('mouseenter', () => { window.lantern.hover(true); if (el === island) { hovering = true; render(); } });
    el.addEventListener('mouseleave', () => { window.lantern.hover(false); if (el === island) { hovering = false; render(); } });
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); window.lantern.avatarMenu(); });
  }
  spriteEl.addEventListener('click', () => window.lantern.togglePanel());
})();
