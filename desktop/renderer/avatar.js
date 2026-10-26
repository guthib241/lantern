// The avatar overlay: follows Lantern's events and changes face. Click-through except over the sprite.
(async function () {
  const spriteEl = document.getElementById('sprite');
  const bubbleEl = document.getElementById('bubble');
  const badgeEl = document.getElementById('badge');
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  let state = LanternState.initial();
  let shown = '';
  let imageOk = {};       // expression -> how many image formats failed so far
  let lastBadge = '';
  let lastBadgeAt = 0;

  const now = () => Date.now() / 1000;

  // The face is Muse's: assets/avatar/<expression>.webm (an animated loop from Muse Video / Vibes),
  // .webp or .png (Muse Image, via scripts/make_avatar.py or imported from the MetaMuse app).
  // The glow from sprite.js is only a stand-in until those exist (no key, tests, mock mode).
  const FORMATS = ['webm', 'webp', 'png'];

  function setSprite(expression) {
    if (expression === shown) return;
    shown = expression;
    spriteEl.className = 'sprite ' + expression;
    spriteEl.textContent = '';
    const tried = imageOk[expression] || 0;
    if (tried < FORMATS.length) {
      const format = FORMATS[tried];
      const media = format === 'webm' ? document.createElement('video') : new Image();
      if (format === 'webm') Object.assign(media, { autoplay: true, loop: true, muted: true, playsInline: true });
      media.setAttribute('aria-label', `Lantern looks ${expression}`);
      media.onerror = () => { imageOk[expression] = tried + 1; shown = ''; render(); };
      media.src = `../assets/avatar/${expression}.${format}`;
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
