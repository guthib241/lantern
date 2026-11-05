// Quick Ask: a Spotlight-style bar on a global hotkey. Type, press Return, and the task starts; the
// live activity island shows the progress. Server text is always set as text.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await window.lantern.config();
  const api = new LanternApi.Api(cfg.apiBase, cfg.token);
  if (cfg.nativeMaterial) document.body.classList.add('native');
  let state = LanternState.initial();
  let recent = [];
  let rows = [];
  let index = 0;
  const KIND_MARK = { ask: '↩', stop: '■', approvals: '!', suggestion: '✦', recent: '↻' };

  function render() {
    rows = LanternQuick.rows($('q').value, {
      suggestions: LanternToday.SUGGESTIONS, pending: Object.keys(state.approvals).length, recent,
    });
    index = Math.min(Math.max(index, 0), Math.max(rows.length - 1, 0));
    $('q-rows').replaceChildren(...rows.map((r, i) => {
      const li = document.createElement('li');
      li.id = `q-row-${i}`;
      li.className = r.kind;
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', String(i === index));
      const k = document.createElement('span');
      k.className = 'k';
      k.textContent = KIND_MARK[r.kind] || '';
      const t = document.createElement('span');
      t.className = 't';
      const b = document.createElement('b');
      b.textContent = r.label;
      const small = document.createElement('small');
      small.textContent = r.sub || '';
      t.append(b, small);
      li.append(k, t);
      li.addEventListener('mousemove', () => { if (index !== i) { index = i; render(); } });
      li.addEventListener('click', () => run(r));
      return li;
    }));
    $('q').setAttribute('aria-activedescendant', rows.length ? `q-row-${index}` : '');
  }

  async function run(r) {
    if (!r) return;
    if (r.kind === 'stop') {
      await window.lantern.stop();
      return close();
    }
    if (r.kind === 'approvals') {
      window.lantern.showPanel();
      return close();
    }
    if (r.kind === 'suggestion' && /\s$/.test(r.goal)) {   // needs finishing ("Remind me in 20 minutes to ")
      $('q').value = r.goal;
      $('q').focus();
      index = 0;
      return render();
    }
    try {
      const { task_id } = await api.post('/task', { goal: r.goal });
      $('q-status').textContent = `Started task #${task_id}`;
      $('q').value = '';
      setTimeout(close, 450);
    } catch (e) {
      $('q-status').textContent = `Couldn't start: ${e.message}`;
    }
  }

  function close() {
    $('q-status').textContent = '';
    window.lantern.hideQuick();
  }

  async function refresh() {
    try {
      recent = (await api.get('/tasks')).slice(0, 3).map((t) => ({ id: t.id, goal: t.goal.split('\n')[0], status: t.status }));
    } catch (e) {
      recent = [];
    }
    render();
  }

  $('q').addEventListener('input', () => { index = 0; render(); });
  $('q-form').addEventListener('submit', (e) => { e.preventDefault(); run(rows[index]); });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      index = LanternQuick.move(index, e.key === 'ArrowDown' ? 1 : -1, rows.length);
      render();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  });
  window.lantern.onQuickShown(() => {
    $('q').focus();
    $('q').select();
    refresh();
  });
  api.events((e) => { state = LanternState.reduce(state, { ts: Date.now() / 1000, ...e }); if (e.type === 'approval' || e.type === 'hello') render(); });
  render();
})();
