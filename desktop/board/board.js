/* Lantern teaching board: plays a lesson (a JSON timeline of board actions) on a chalkboard.
 *
 * write_math  KaTeX, revealed left to right; a ✓ appears when the lesson marks the step verified (the planner
 *             sets that only after SymPy accepts it; the board never checks math itself)
 * write_text  handwriting-style text, revealed left to right
 * draw        line, arrow, circle (at a point, on axes, or around an item), axes with ticks, plot from points
 * highlight   a marker stroke behind an item;  erase  one item, several, or "all";  pause  n seconds
 * say         every action may carry a line, spoken by the computer's own speech engine (Web Speech API). That is
 *             the operating system's voice, not a Meta model. Captions always show the line.
 *
 * URL options: ?lesson=quadratic  ?speed=1.5  ?mute=1  ?autoplay=1.  window.LanternBoard is the API for the
 * desktop app and tests: load(lesson), play(), pause(), step(), restart(), state().
 */
(function () {
  'use strict';
  const T = window.LanternBoardTimeline;
  const $ = (id) => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const CANCELLED = new Error('cancelled');
  const params = new URLSearchParams(window.location.search);
  const reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const hasVoice = 'speechSynthesis' in window && typeof window.SpeechSynthesisUtterance === 'function';
  const num = (v, d, lo, hi) => { const n = Number(v); return Number.isFinite(n) && v !== null ? Math.min(hi, Math.max(lo, n)) : d; };

  const st = {
    lesson: null, index: 0, playing: false, gen: 0, stepOnce: false, done: false,
    speed: num(params.get('speed'), 1, 0.25, 40),
    muted: params.get('mute') === '1' || !hasVoice,
    items: new Map(),     // id -> {el, extras: [], action, svg: bool}
    lastBottom: 40,
    running: new Set(),   // animations in flight, paused and resumed with the lesson
    waiters: [],
  };

  // ---- time that only runs while playing ------------------------------------------------------
  function wait(ms, gen) {
    return new Promise((resolve, reject) => {
      let left = ms;
      let last = performance.now();
      const tick = () => {
        if (gen !== st.gen) return reject(CANCELLED);
        const now = performance.now();
        if (st.playing) left -= now - last;
        last = now;
        if (left <= 0) return resolve();
        setTimeout(tick, Math.min(50, Math.max(5, left)));
        return undefined;
      };
      tick();
    });
  }

  function whenPlaying(gen) {
    if (gen !== st.gen) return Promise.reject(CANCELLED);
    return st.playing ? Promise.resolve() : new Promise((resolve) => st.waiters.push(resolve));
  }

  function animate(el, keyframes, ms, gen) {
    const anim = el.animate(keyframes, { duration: reduceMotion ? 0 : Math.max(1, ms), easing: 'ease-in-out', fill: 'forwards' });
    st.running.add(anim);
    if (!st.playing) anim.pause();
    return anim.finished.then(
      () => { st.running.delete(anim); if (gen !== st.gen) throw CANCELLED; },
      () => { st.running.delete(anim); throw CANCELLED; });
  }

  const ms = (a) => (T.revealSeconds(a) * 1000) / st.speed;

  // ---- voice and captions -------------------------------------------------------------------------
  function speak(text, gen) {
    if (!text) return Promise.resolve();   // a silent step keeps the last caption up, so it can still be read
    $('caption').textContent = text;
    const estimate = (T.speechSeconds(text) * 1000) / Math.min(st.speed, 2);
    if (st.muted) return wait(estimate / (st.speed > 2 ? st.speed / 2 : 1), gen);
    return new Promise((resolve, reject) => {
      const u = new window.SpeechSynthesisUtterance(text);
      u.lang = (st.lesson && st.lesson.language) || 'en-US';
      u.rate = Math.min(2, Math.max(0.5, st.speed));
      let over = false;
      const finish = () => { if (!over) { over = true; resolve(); } };
      u.onend = finish;
      u.onerror = finish;
      window.speechSynthesis.speak(u);
      // A voice that never reports the end must not stall the lesson.
      wait(estimate * 2 + 3000, gen).then(finish, (e) => { if (!over) { over = true; reject(e); } });
    });
  }

  // ---- placing things ---------------------------------------------------------------------------------
  function place(el, a, size) {
    const x = a.x !== undefined ? a.x : T.MARGIN;
    const y = a.y !== undefined ? a.y : st.lastBottom + 24;
    Object.assign(el.style, { left: `${x}px`, top: `${y}px`, fontSize: `${a.size || size}px`, color: T.color(a.color, 'var(--chalk)') });
    $('items').append(el);
    st.lastBottom = y + el.offsetHeight;
  }

  function register(a, el, svg) {
    if (a.id) st.items.set(a.id, { el, extras: [], action: a, svg: !!svg });
  }

  function box(id) {
    const it = st.items.get(id);
    if (!it) throw new Error(`"${id}" isn't on the board`);
    if (it.svg) { const b = it.el.getBBox(); return { x: b.x, y: b.y, w: b.width, h: b.height }; }
    return { x: it.el.offsetLeft, y: it.el.offsetTop, w: it.el.offsetWidth, h: it.el.offsetHeight };
  }

  function reveal(el, a, gen) {
    return animate(el, [{ clipPath: 'inset(-30% 100% -30% -4%)' }, { clipPath: 'inset(-30% -4% -30% -4%)' }], ms(a), gen);
  }

  function svg(tag, attrs, parent) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    (parent || $('draw')).append(el);
    return el;
  }

  function stroke(el, msTotal, gen) {
    el.setAttribute('pathLength', '1');
    el.setAttribute('stroke-dasharray', '1');
    el.setAttribute('stroke-dashoffset', '1');
    return animate(el, [{ strokeDashoffset: 1 }, { strokeDashoffset: 0 }], msTotal, gen);
  }

  function rgba(c, alpha) {
    const hex = /^#([0-9a-f]{6})$/i.exec(T.color(c, '#ffd75e'));
    if (!hex) return `rgba(255, 215, 94, ${alpha})`;
    const n = parseInt(hex[1], 16);
    return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  // ---- actions ------------------------------------------------------------------------------------------
  async function writeMath(a, gen) {
    const el = document.createElement('div');
    el.className = 'item math';
    const ink = document.createElement('span');
    ink.className = 'ink';
    window.katex.render(a.latex, ink, { throwOnError: false, displayMode: false, output: 'htmlAndMathml', trust: false, strict: 'ignore' });
    const check = document.createElement('span');
    check.className = 'check';
    check.setAttribute('aria-hidden', 'true');
    check.dataset.slot = a.id || '';
    if (a.verified === true) { check.textContent = '✓'; check.title = 'Checked: this step follows from the one before'; }
    el.append(ink, check);
    if (a.step) {
      const note = document.createElement('span');
      note.className = 'step-note';
      note.textContent = a.step;
      el.append(note);
    }
    place(el, a, 46);
    register(a, el);
    await reveal(el, a, gen);
    if (a.verified === true) {
      check.classList.add('on');
      const sr = document.createElement('span');
      sr.className = 'sr';
      sr.textContent = ' (checked)';
      el.append(sr);
    }
  }

  async function writeText(a, gen) {
    const el = document.createElement('div');
    el.className = 'item text';
    el.textContent = a.text;
    place(el, a, 32);
    register(a, el);
    await reveal(el, a, gen);
  }

  function arrowHead(from, to, colour, width, parent) {
    const ang = Math.atan2(to[1] - from[1], to[0] - from[0]);
    const len = 16 + width * 2;
    const pts = [to, [to[0] - len * Math.cos(ang - 0.4), to[1] - len * Math.sin(ang - 0.4)],
                 [to[0] - len * Math.cos(ang + 0.4), to[1] - len * Math.sin(ang + 0.4)]];
    return svg('polygon', { points: pts.map((p) => p.join(',')).join(' '), fill: colour, opacity: 0 }, parent);
  }

  async function drawLine(a, gen) {
    const colour = T.color(a.color, '#f4f1e8');
    const width = a.width || 4;
    const g = svg('g', { 'data-id': a.id || '' });
    const line = svg('path', { d: `M ${a.from[0]} ${a.from[1]} L ${a.to[0]} ${a.to[1]}`, stroke: colour, 'stroke-width': width,
                               'stroke-linecap': 'round', fill: 'none' }, g);
    register(a, g, true);
    await stroke(line, ms(a), gen);
    if (a.shape === 'arrow') await animate(arrowHead(a.from, a.to, colour, width, g), [{ opacity: 0 }, { opacity: 1 }], 120 / st.speed, gen);
  }

  async function drawCircle(a, gen) {
    let cx; let cy; let rx; let ry;
    if (a.around) {
      const b = box(a.around);
      [cx, cy, rx, ry] = [b.x + b.w / 2, b.y + b.h / 2, b.w / 2 + 26, b.h / 2 + 18];
    } else {
      const c = a.axes ? T.dataToBoard(st.items.get(a.axes).action, a.center) : a.center;
      [cx, cy, rx, ry] = [c[0], c[1], a.r, a.r];
    }
    const e = svg('ellipse', { cx, cy, rx, ry, fill: 'none', stroke: T.color(a.color, '#ffd75e'), 'stroke-width': a.width || 4,
                               'stroke-linecap': 'round', 'data-id': a.id || '' });
    register(a, e, true);
    await stroke(e, ms(a), gen);
  }

  async function drawAxes(a, gen) {
    const colour = T.color(a.color, '#c9c4b4');
    const g = svg('g', { 'data-id': a.id });
    const defs = svg('defs', {}, g);
    const clip = svg('clipPath', { id: `clip-${a.id}` }, defs);
    svg('rect', { x: a.x, y: a.y, width: a.w, height: a.h }, clip);
    const [x0, x1] = a.xrange;
    const [y0, y1] = a.yrange;
    const zero = T.dataToBoard(a, [Math.min(Math.max(0, x0), x1), Math.min(Math.max(0, y0), y1)]);
    const xAxis = svg('path', { d: `M ${a.x} ${zero[1]} L ${a.x + a.w} ${zero[1]}`, stroke: colour, 'stroke-width': 3, fill: 'none' }, g);
    const yAxis = svg('path', { d: `M ${zero[0]} ${a.y + a.h} L ${zero[0]} ${a.y}`, stroke: colour, 'stroke-width': 3, fill: 'none' }, g);
    const marks = svg('g', { opacity: 0 }, g);
    arrowHead([a.x, zero[1]], [a.x + a.w + 14, zero[1]], colour, 2, marks).setAttribute('opacity', 1);
    arrowHead([zero[0], a.y + a.h], [zero[0], a.y - 14], colour, 2, marks).setAttribute('opacity', 1);
    const step = a.ticks || 0;
    if (step > 0) {
      const label = (v) => (Math.abs(v) < 1e-9 ? '' : String(Math.round(v * 100) / 100));
      const nx = Math.floor((x1 - x0) / step);
      const ny = Math.floor((y1 - y0) / step);
      const every = (n) => (n > 16 ? Math.ceil(n / 16) : 1);
      for (let i = Math.ceil(x0 / step), k = 0; i * step <= x1 + 1e-9; i += 1, k += 1) {
        const [px] = T.dataToBoard(a, [i * step, 0]);
        svg('line', { x1: px, y1: zero[1] - 6, x2: px, y2: zero[1] + 6, stroke: colour, 'stroke-width': 2 }, marks);
        if (k % every(nx) === 0 && label(i * step)) svg('text', { x: px, y: zero[1] + 30, 'text-anchor': 'middle', 'font-size': 20, fill: colour }, marks).textContent = label(i * step);
      }
      for (let i = Math.ceil(y0 / step), k = 0; i * step <= y1 + 1e-9; i += 1, k += 1) {
        const [, py] = T.dataToBoard(a, [0, i * step]);
        svg('line', { x1: zero[0] - 6, y1: py, x2: zero[0] + 6, y2: py, stroke: colour, 'stroke-width': 2 }, marks);
        if (k % every(ny) === 0 && label(i * step)) svg('text', { x: zero[0] - 12, y: py + 7, 'text-anchor': 'end', 'font-size': 20, fill: colour }, marks).textContent = label(i * step);
      }
    }
    if (a.xlabel) svg('text', { x: a.x + a.w + 20, y: zero[1] + 34, 'font-size': 26, fill: colour, 'font-style': 'italic' }, marks).textContent = a.xlabel;
    if (a.ylabel) svg('text', { x: zero[0] + 14, y: a.y - 10, 'font-size': 26, fill: colour, 'font-style': 'italic' }, marks).textContent = a.ylabel;
    register(a, g, true);
    await Promise.all([stroke(xAxis, ms(a), gen), stroke(yAxis, ms(a), gen)]);
    await animate(marks, [{ opacity: 0 }, { opacity: 1 }], 200 / st.speed, gen);
  }

  async function drawPlot(a, gen) {
    const axes = st.items.get(a.axes).action;
    const colour = T.color(a.color, '#ffad5c');
    const pts = a.points.map((p) => T.dataToBoard(axes, p));
    const g = svg('g', { 'data-id': a.id || '', 'clip-path': `url(#clip-${a.axes})` });
    const path = svg('path', { d: `M ${pts.map((p) => p.map((v) => v.toFixed(1)).join(' ')).join(' L ')}`, stroke: colour,
                               'stroke-width': a.width || 4, fill: 'none', 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, g);
    register(a, g, true);
    await stroke(path, ms(a), gen);
    if (a.label) {
      const visible = pts.filter((p) => p[1] >= axes.y && p[1] <= axes.y + axes.h);
      const end = visible[visible.length - 1] || pts[pts.length - 1];
      const at = a.label_at ? T.dataToBoard(axes, a.label_at) : [Math.min(end[0] + 10, axes.x + axes.w - 10), Math.max(end[1] - 12, axes.y + 24)];
      const text = svg('text', { x: at[0], y: at[1], 'font-size': 24, fill: colour, 'text-anchor': a.label_at ? 'middle' : 'end', opacity: 0 });
      text.textContent = a.label;
      st.items.get(a.id) && st.items.get(a.id).extras.push(text);
      await animate(text, [{ opacity: 0 }, { opacity: 1 }], 200 / st.speed, gen);
    }
  }

  async function highlight(a, gen) {
    const it = st.items.get(a.target);
    const b = box(a.target);
    let mark;
    if (it.svg) {
      mark = document.createElementNS(NS, 'rect');
      for (const [k, v] of Object.entries({ x: b.x - 10, y: b.y - 10, width: b.w + 20, height: b.h + 20, rx: 10, fill: rgba(a.color, 0.22) })) mark.setAttribute(k, v);
      it.el.parentNode.insertBefore(mark, it.el);
    } else {
      mark = document.createElement('div');
      mark.className = 'hl';
      Object.assign(mark.style, { left: `${b.x - 14}px`, top: `${b.y - 6}px`, width: `${b.w + 28}px`, height: `${b.h + 12}px`,
                                  background: rgba(a.color, 0.26) });
      $('items').insertBefore(mark, it.el);
    }
    it.extras.push(mark);
    await animate(mark, [{ transform: 'scaleX(0)', transformOrigin: 'left center', transformBox: 'fill-box' },
                         { transform: 'scaleX(1)', transformOrigin: 'left center', transformBox: 'fill-box' }], ms(a), gen);
  }

  async function erase(a, gen) {
    const ids = a.target === 'all' ? [...st.items.keys()] : T.targets(a.target);
    const els = ids.flatMap((id) => { const it = st.items.get(id); return it ? [it.el, ...it.extras] : []; });
    if (a.target === 'all') els.push(...$('items').children, ...$('draw').children);
    const unique = [...new Set(els)];
    await Promise.all(unique.map((el) => animate(el, [{ opacity: 1 }, { opacity: 0 }], ms(a), gen)));
    unique.forEach((el) => el.remove());
    ids.forEach((id) => st.items.delete(id));
    if (a.target === 'all') st.lastBottom = 40;
  }

  function perform(a, gen) {
    switch (a.do) {
      case 'write_math': return writeMath(a, gen);
      case 'write_text': return writeText(a, gen);
      case 'draw':
        if (a.shape === 'line' || a.shape === 'arrow') return drawLine(a, gen);
        if (a.shape === 'circle') return drawCircle(a, gen);
        if (a.shape === 'axes') return drawAxes(a, gen);
        return drawPlot(a, gen);
      case 'highlight': return highlight(a, gen);
      case 'erase': return erase(a, gen);
      case 'pause': return wait((a.seconds * 1000) / st.speed, gen);
      default: return Promise.resolve();
    }
  }

  // ---- the player ------------------------------------------------------------------------------------
  function progress() {
    const total = st.lesson ? st.lesson.actions.length : 0;
    $('progress-text').textContent = `${st.index} / ${total}`;
    $('progress-fill').style.width = `${total ? (st.index / total) * 100 : 0}%`;
    document.body.dataset.index = String(st.index);
  }

  function setPlaying(on) {
    st.playing = on;
    st.running.forEach((anim) => (on ? anim.play() : anim.pause()));
    if (hasVoice) { if (on) window.speechSynthesis.resume(); else window.speechSynthesis.pause(); }
    $('play').textContent = on ? 'Pause' : st.done ? 'Replay' : 'Play';
    document.body.dataset.state = on ? 'playing' : st.done ? 'done' : 'paused';
    if (on) { const w = st.waiters.splice(0); w.forEach((resolve) => resolve()); }
  }

  async function run(gen) {
    try {
      while (st.index < st.lesson.actions.length) {
        await whenPlaying(gen);
        const a = st.lesson.actions[st.index];
        await Promise.all([perform(a, gen), speak(a.say, gen)]);
        if (gen !== st.gen) return;
        st.index += 1;
        progress();
        if (st.stepOnce) { st.stepOnce = false; setPlaying(false); }
      }
      st.done = true;
      setPlaying(false);
    } catch (e) {
      if (e !== CANCELLED) problems([`This step couldn't be shown: ${e && e.message ? e.message : e}`]);
    }
  }

  function clearBoard() {
    st.running.forEach((anim) => anim.cancel());
    st.running.clear();
    if (hasVoice) window.speechSynthesis.cancel();
    $('items').textContent = '';
    $('draw').textContent = '';
    st.items.clear();
    st.lastBottom = 40;
    $('caption').textContent = '';
  }

  function problems(list) {
    const box_ = $('problems');
    box_.textContent = '';
    if (!list.length) { box_.hidden = true; return; }
    const h = document.createElement('h2');
    h.textContent = "This lesson can't be played";
    const ul = document.createElement('ul');
    list.forEach((p) => { const li = document.createElement('li'); li.textContent = p; ul.append(li); });
    box_.append(h, ul);
    box_.hidden = false;
    document.body.dataset.state = 'error';
  }

  function load(lesson, autoplay) {
    st.gen += 1;
    clearBoard();
    const errs = T.validate(lesson);
    if (!window.katex) errs.push('the math renderer (KaTeX) did not load');
    st.lesson = errs.length ? null : lesson;
    st.index = 0;
    st.done = false;
    st.stepOnce = false;
    $('lesson-title').textContent = lesson && lesson.title ? lesson.title : 'Lesson';
    document.title = `${lesson && lesson.title ? lesson.title : 'Lesson'} · Lantern board`;
    problems(errs);
    if (errs.length) return errs;
    progress();
    setPlaying(false);
    run(st.gen);
    if (autoplay) setPlaying(true);
    return [];
  }

  function restart() {
    if (st.lesson) load(st.lesson, true);
  }

  function step() {
    if (!st.lesson) return;
    if (st.done) { load(st.lesson, false); }
    st.stepOnce = true;
    setPlaying(true);
  }

  function togglePlay() {
    if (!st.lesson) return;
    if (st.done) { restart(); return; }
    setPlaying(!st.playing);
  }

  function setMuted(m) {
    st.muted = m || !hasVoice;
    if (st.muted && hasVoice) window.speechSynthesis.cancel();
    $('mute').setAttribute('aria-pressed', String(st.muted));
    $('mute').textContent = st.muted ? 'Voice off' : 'Voice on';
  }

  // ---- fit the 1600 x 900 board into the window ----------------------------------------------------------
  function fit() {
    const wrap = $('stage-wrap');
    const s = Math.min((wrap.clientWidth - 28) / T.W, (wrap.clientHeight - 28) / T.H);
    Object.assign($('stage').style, { position: 'absolute', left: '50%', top: '50%',
                                      transform: `translate(-50%, -50%) scale(${Math.max(0.1, s)})` });
  }

  // ---- wiring -------------------------------------------------------------------------------------------
  async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  }

  async function start() {
    fit();
    window.addEventListener('resize', fit);
    setMuted(st.muted);
    if (!hasVoice) $('voice-note').textContent = 'Voice: off (no speech engine found); captions only';
    if (![...$('speed').options].some((o) => Number(o.value) === st.speed)) {   // e.g. ?speed=20 for tests
      const o = document.createElement('option');
      o.value = String(st.speed);
      o.textContent = `${st.speed}×`;
      $('speed').append(o);
    }
    $('speed').value = String(st.speed);
    $('play').addEventListener('click', togglePlay);
    $('step').addEventListener('click', step);
    $('restart').addEventListener('click', restart);
    $('mute').addEventListener('click', () => setMuted(!st.muted));
    $('speed').addEventListener('change', (e) => { st.speed = num(e.target.value, 1, 0.25, 40); });
    $('lesson-file').addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      file.text().then((text) => {
        try { load(JSON.parse(text), false); } catch (err) { problems([`That file isn't valid JSON: ${err.message}`]); }
      });
    });
    window.addEventListener('keydown', (e) => {
      if (e.target && /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(e.target.tagName) && e.key !== 'ArrowRight') return;
      if (e.key === ' ') { e.preventDefault(); togglePlay(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); step(); }
      else if (e.key === 'r' || e.key === 'R') restart();
      else if (e.key === 'm' || e.key === 'M') setMuted(!st.muted);
    });
    const autoplay = params.get('autoplay') === '1';
    // In the desktop app, the panel hands the board a lesson Lantern planned and checked (S16).
    let hosted = false;
    if (window.lanternBoardHost) window.lanternBoardHost.onLesson((lesson) => { hosted = true; load(lesson, true); });
    try {
      const index = await fetchJSON('lessons/index.json');
      const pick = $('lesson-pick');
      index.lessons.forEach((l) => { const o = document.createElement('option'); o.value = l.file; o.textContent = l.title; pick.append(o); });
      const wanted = params.get('lesson');
      const first = index.lessons.find((l) => wanted && l.file === `${wanted}.json`) || index.lessons[0];
      pick.value = first.file;
      pick.addEventListener('change', async () => load(await fetchJSON(`lessons/${pick.value}`), false));
      if (!hosted) load(await fetchJSON(`lessons/${first.file}`), autoplay);
    } catch (e) {
      if (hosted) return;
      // Opened straight from disk (file://): lessons can't be fetched, so offer the file picker instead.
      $('lesson-title').textContent = 'Open a lesson file to begin';
      document.body.dataset.state = 'idle';
    }
  }

  window.LanternBoard = {
    load: (lesson, autoplay) => load(lesson, autoplay),
    play: () => st.lesson && setPlaying(true),
    pause: () => setPlaying(false),
    step, restart,
    setSpeed: (s) => { st.speed = num(s, 1, 0.25, 40); },
    setMuted,
    validate: T.validate,
    state: () => ({ title: st.lesson && st.lesson.title, index: st.index, total: st.lesson ? st.lesson.actions.length : 0,
                    playing: st.playing, done: st.done, muted: st.muted, items: [...st.items.keys()] }),
  };
  start();
})();
