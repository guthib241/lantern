// The avatar overlay: follows Lantern's events and changes face. Click-through except over the sprite.
(async function () {
  const spriteEl = document.getElementById('sprite');
  const bubbleEl = document.getElementById('bubble');
  const badgeEl = document.getElementById('badge');
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  let state = LanternState.initial();
  let shown = '';
  let lastBadge = '';
  let lastBadgeAt = 0;

  const now = () => Date.now() / 1000;

  // The face is Muse's: desktop/assets/avatar/manifest.js lists, per expression, an animated loop
  // (.webm, from Vibes / Muse Video) or an image (.webp/.png, Muse Image). Expressions without
  // their own file show idle. The glow from sprite.js only stands in until any art exists.
  const sheet = (window.LanternAvatarManifest && window.LanternAvatarManifest.expressions) || {};
  let broken = false;

  function setSprite(expression) {
    if (expression === shown) return;
    shown = expression;
    spriteEl.className = 'sprite ' + expression;
    spriteEl.textContent = '';
    const entry = sheet[expression] || sheet.idle;
    if (entry && !broken) {
      const video = /\.(webm|mp4)$/i.test(entry.file);
      const media = video ? document.createElement('video') : new Image();
      if (video) Object.assign(media, { autoplay: true, loop: true, muted: true, playsInline: true });
      media.className = 'portrait';
      media.setAttribute('aria-label', `Lantern looks ${expression}`);
      media.onerror = () => { broken = true; shown = ''; render(); };
      media.src = `../assets/avatar/${entry.file}`;
      spriteEl.appendChild(media);
    } else {
      spriteEl.innerHTML = LanternSprite.svg(expression);   // our own trusted SVG, no user text inside
    }
  }

  function render() {
    const t = now();
    const face = LanternExpression.pick(state, t);
    setSprite(face);
    const line = LanternExpression.bubble(state, t, 'smug');
    bubbleEl.hidden = !line;
    bubbleEl.textContent = line || '';
    bubbleEl.classList.toggle('urgent', face === 'needs-approval');
    const fresh = t - lastBadgeAt < 4 && state.current != null;
    badgeEl.hidden = !fresh;
    badgeEl.textContent = lastBadge;
  }

  api.events((e) => {
    state = LanternState.reduce(state, { ts: now(), ...e });
    if (e.badge && (e.type === 'usage' || e.type === 'tool' || e.type === 'form')) {
      lastBadge = e.badge;
      lastBadgeAt = now();
    }
    render();
  }, (up) => { state = LanternState.reduce(state, { type: up ? 'hello' : 'disconnected', ts: now() }); render(); });

  window.lantern.onStopped(() => { state = LanternState.reduce(state, { type: 'stopped', ts: now() }); render(); });
  setInterval(render, 1000);
  render();

  // Click-through everywhere except the sprite and the bubble.
  for (const el of [spriteEl, bubbleEl]) {
    el.addEventListener('mouseenter', () => window.lantern.hover(true));
    el.addEventListener('mouseleave', () => window.lantern.hover(false));
    el.addEventListener('click', () => window.lantern.togglePanel());
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); window.lantern.avatarMenu(); });
  }
})();
