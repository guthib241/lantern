/* Agent packs in the desktop panel: cards, the run form, drafts. Pure: no DOM, no fetch.
 * Panel.js calls api.* and renders; this module only shapes data, so node --test covers it. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternAgents = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const FORMAT = 'lantern-agent/1';
  const MAX_VALUE = 500;   // run-time values are plain text, like lantern/agentpacks.py

  /** What a card shows: title, who made it, what it may use, whether it can run. */
  function cardView(pack) {
    const p = pack || {};
    const tools = Array.isArray(p.tools) ? p.tools.filter((t) => typeof t === 'string') : [];
    const inputs = Array.isArray(p.inputs) ? p.inputs : [];
    const sub = [p.author, p.version ? `v${p.version}` : ''].filter(Boolean).join(' · ');
    return {
      name: String(p.name || ''),
      title: String(p.title || p.name || 'Untitled agent'),
      description: String(p.description || ''),
      author: String(p.author || ''),
      version: String(p.version || ''),
      sub,
      tools,
      inputs,
      disabled: !!p.disabled,
      runnable: !p.disabled && !!p.name,
    };
  }

  const SURFACES = ['laptop', 'phone', 'both'];

  /** Where an agent can start: the store's Runs-on setting (laptop when missing). Pure. */
  function surfaceView(pack) {
    const p = pack || {};
    const surface = SURFACES.includes(p.surface) ? p.surface : 'laptop';
    const labels = { laptop: 'Laptop', phone: 'Phone', both: 'Both' };
    return { surface, label: `Runs on: ${labels[surface]}`, options: SURFACES };
  }

  /** Values from the run form -> {ok, inputs} or {ok:false, error}. Every declared
   *  input is needed: the server renders each {blank} of the goal from one. */
  function runPayload(pack, values) {
    const inputs = (pack && pack.inputs) || [];
    const given = values || {};
    const out = {};
    for (const input of inputs) {
      const text = String(given[input.key] == null ? '' : given[input.key]).trim();
      if (!text) return { ok: false, error: `“${input.question || input.key}” needs an answer.` };
      if (text.length > MAX_VALUE) return { ok: false, error: `“${input.key}” is too long (at most ${MAX_VALUE} characters).` };
      out[input.key] = text;
    }
    return { ok: true, inputs: out };
  }

  /** A dropped or picked file -> {ok, data} or {ok:false, error}. The server validates
   *  the pack itself; this only checks it parses as a JSON object. */
  function parsePackFile(text) {
    let data;
    try {
      data = JSON.parse(String(text == null ? '' : text));
    } catch {
      return { ok: false, error: 'That file is not JSON.' };
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return { ok: false, error: 'That file is not an agent pack (it should be one JSON object).' };
    }
    return { ok: true, data };
  }

  /** The exact bytes to share: the pack file, for the save dialog. */
  function packFileName(pack) {
    const name = String((pack && pack.name) || 'agent').trim() || 'agent';
    return `${name}.lantern-agent.json`;
  }

  function packFileText(pack) {
    return `${JSON.stringify(pack, null, 2)}\n`;
  }

  /** A server draft -> editable form fields (all text; tools one per line). */
  function draftFields(draft) {
    const d = draft || {};
    const inputs = Array.isArray(d.inputs) ? d.inputs : [];
    return {
      name: String(d.name || ''),
      title: String(d.title || ''),
      description: String(d.description || ''),
      author: String(d.author || ''),
      version: String(d.version || '1.0.0'),
      goal: String(d.goal || ''),
      instructions: String(d.instructions || ''),
      tools: (Array.isArray(d.tools) ? d.tools : []).join('\n'),
      inputs: inputs.map((i) => ({ key: i.key || '', question: i.question || '', example: i.example || '' })),
    };
  }

  /** Edited draft fields -> the pack JSON to POST to /agents/install. */
  function packFromFields(fields) {
    const f = fields || {};
    const tools = String(f.tools || '').split(/[\n,]+/).map((t) => t.trim()).filter(Boolean);
    return {
      format: FORMAT,
      name: String(f.name || '').trim(),
      title: String(f.title || '').trim(),
      description: String(f.description || '').trim(),
      author: String(f.author || '').trim(),
      version: String(f.version || '').trim() || '1.0.0',
      inputs: (Array.isArray(f.inputs) ? f.inputs : [])
        .filter((i) => i && (i.key || i.question))
        .map((i) => ({ key: String(i.key || '').trim(), question: String(i.question || '').trim(),
                       example: String(i.example || '').trim() }))
        .filter((i) => i.key && i.question),
      goal: String(f.goal || '').trim(),
      instructions: String(f.instructions || '').trim(),
      tools,
    };
  }

  /** What the signature badge shows: who sealed the pack, or that nobody did. Pure. */
  function signatureBadge(pack) {
    const p = pack || {};
    if (p.signature_status === 'signed') {
      const fp = typeof p.fingerprint === 'string' && p.fingerprint ? ` · key ${p.fingerprint}` : '';
      return { kind: 'signed', text: `✓ Signed by ${p.author || 'unknown'}${fp}` };
    }
    if (p.signature_status === 'tampered') {
      return { kind: 'tampered', text: 'Changed after it was signed — not installed' };
    }
    return { kind: 'unsigned', text: 'Unsigned: anyone could have changed it' };
  }

  /** The server's face descriptor ({emoji} | {icon} | null) -> what the card shows. Pure. */
  function faceView(pack) {
    const f = (pack && pack.face) || null;
    if (f && typeof f.emoji === 'string' && f.emoji) return { kind: 'emoji', emoji: f.emoji };
    if (f && f.icon) return { kind: 'icon' };
    return { kind: 'none' };
  }

  /** Words for a pack's schedule: 'every day at 09:00'. Mirrors lantern/agentpacks.py. */
  function scheduleText(sched) {
    const s = sched || {};
    if (s.every === 'hours') {
      const n = Number(s.hours);
      if (!(n >= 1)) return '';
      return `every ${n} hour${n === 1 ? '' : 's'}`;
    }
    if (s.every === 'day' || s.every === 'weekday' || s.every === 'week') {
      if (typeof s.at !== 'string' || !/^\d\d:\d\d$/.test(s.at)) return '';
      const every = s.every === 'day' ? 'every day' : s.every === 'weekday' ? 'every weekday' : 'every week';
      return `${every} at ${s.at}`;
    }
    return '';
  }

  /** 'today 09:00', 'tomorrow 09:00' or 'Mon 3 Nov 09:00' for a loop's next run (seconds). Pure. */
  function formatDue(ts, nowMs) {
    if (ts == null) return '';
    const now = new Date(nowMs);
    const due = new Date(ts * 1000);
    const hhmm = `${String(due.getHours()).padStart(2, '0')}:${String(due.getMinutes()).padStart(2, '0')}`;
    const atMidnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const diff = Math.round((atMidnight(due) - atMidnight(now)) / 86400000);
    if (diff === 0) return `today ${hhmm}`;
    if (diff === 1) return `tomorrow ${hhmm}`;
    const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return `${DAYS[due.getDay()]} ${due.getDate()} ${MONTHS[due.getMonth()]} ${hhmm}`;
  }

  /** A finished run's status in the card's words: done -> ok, failed stays, anything stopped. */
  function lastWord(status) {
    if (status === 'done') return 'ok';
    if (status === 'failed') return 'failed';
    if (status === 'cancelled' || status === 'interrupted') return 'stopped';
    return String(status || 'unknown');
  }

  /** Loop state for a card: schedule line, next/last run. `nowMs` is Date.now(). Pure. */
  function loopView(pack, nowMs) {
    const p = pack || {};
    if (!p.schedule) return { schedulable: false, enabled: false };
    const loop = p.loop || {};
    return {
      schedulable: true,
      enabled: !!loop.enabled,
      schedule: scheduleText(p.schedule),
      next: loop.enabled && loop.next_run ? `Next run ${formatDue(loop.next_run, nowMs)}`
        : loop.enabled ? 'Due on the next check' : 'Off',
      last: loop.last_status ? `Last run #${loop.last_task} · ${lastWord(loop.last_status)}` : 'Never run yet',
      lastTask: loop.last_task || null,
    };
  }

  /** An emoji typed for "Make a face" -> {ok, emoji} or {ok:false, error}. */
  function parseEmoji(text) {
    const emoji = String(text == null ? '' : text).trim();
    if (!emoji) return { ok: false, error: 'Type an emoji first, or ask Lantern to draw one.' };
    if ([...emoji].length > 8) return { ok: false, error: 'One short emoji is enough for a face.' };
    return { ok: true, emoji };
  }

  /** An /agents/import-skill preview -> what the import form shows. Pure. */
  function importView(preview) {
    const p = (preview && preview.pack) || {};
    const tools = Array.isArray(p.tools) ? p.tools.filter((t) => typeof t === 'string') : [];
    const available = Array.isArray(preview && preview.available_tools)
      ? preview.available_tools.filter((t) => typeof t === 'string') : [];
    const skipped = Array.isArray(preview && preview.skipped)
      ? preview.skipped.filter((t) => typeof t === 'string') : [];
    return {
      name: String(p.name || ''),
      title: String(p.title || p.name || 'Untitled agent'),
      description: String(p.description || ''),
      tools, available,
      skipped, truncated: !!(preview && preview.truncated),
      source: String((preview && preview.source) || ''),
    };
  }

  /** The previewed pack with the user's tool checkboxes applied. Pure. */
  function packWithTools(pack, tools) {
    const p = pack || {};
    const picked = (Array.isArray(tools) ? tools : []).filter((t) => typeof t === 'string');
    return { ...p, tools: picked };
  }

  /** The exact file name to share an A2A card under. */
  function a2aFileName(pack) {
    const name = String((pack && pack.name) || 'agent').trim() || 'agent';
    return `${name}.agent-card.json`;
  }

  return { FORMAT, MAX_VALUE, SURFACES, surfaceView, cardView, runPayload, parsePackFile, packFileName, packFileText,
           draftFields, packFromFields, faceView, parseEmoji, signatureBadge, importView, packWithTools, a2aFileName,
           scheduleText, formatDue, loopView };
});
