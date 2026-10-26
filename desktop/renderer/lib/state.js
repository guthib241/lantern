/* Lantern desktop: the event reducer. Pure: (state, event) -> new state.
 * Events come from the local server's websocket (/events): hello, queued, task, tool, form,
 * usage, approval, done, stopped, channel_error. Every step keeps its model badge. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternState = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const MAX_TASKS = 30;
  const MAX_STEPS = 80;

  function initial() {
    return {
      tasks: {},          // id -> {id, goal, status, steps, reply, summary, cost, verified, startedAt, endedAt}
      order: [],          // task ids, oldest first
      current: null,
      approvals: {},      // pending approvals by id (with nonce, for the card)
      usage: { byBadge: {}, total: 0 },
      lastDone: null,     // {id, status, verified, at}
      lastStop: 0,
      lastError: 0,
      lastEvent: 0,
      lastCheckin: null,  // {reaction, found, at}
      lastRetry: null,    // {at, wait_s, error}: the network wobbled and Lantern is retrying
      lastReminder: null, // {text, late, at}: a reminder just went off
      connected: false,
    };
  }

  function ensureTask(s, id, goal, at) {
    if (id == null) return null;
    if (!s.tasks[id]) {
      s.tasks[id] = { id, goal: goal || '', status: 'queued', steps: [], reply: '', summary: '',
                      cost: 0, verified: false, startedAt: at, endedAt: null };
      s.order.push(id);
      while (s.order.length > MAX_TASKS) delete s.tasks[s.order.shift()];
    } else if (goal && !s.tasks[id].goal) {
      s.tasks[id].goal = goal;
    }
    return s.tasks[id];
  }

  function addStep(task, step) {
    if (!task) return;
    task.steps.push(step);
    if (task.steps.length > MAX_STEPS) task.steps.splice(0, task.steps.length - MAX_STEPS);
  }

  function toolLabel(e) {
    const r = e.result || {};
    const a = e.args || {};
    if (r.error) return `${e.name}: ${String(r.error).slice(0, 90)}`;
    switch (e.name) {
      case 'fill_form':
        return r.verified ? 'Form submitted and verified ✓'
          : r.needs_user ? `Form: ${r.reason || 'your turn'}` : `Form not submitted: ${r.reason || 'see details'}`;
      case 'send_email':
        return r.sent ? (r.verified ? `Email sent to ${(r.to || []).join(', ')} ✓` : 'Email sent (not yet seen in Sent)')
          : `Email not sent: ${r.reason || ''}`;
      case 'ask_private': return 'Asked the private lane about ' + baseName(a.path);
      case 'read_file': return 'Read ' + baseName(a.path);
      case 'write_file': return r.verified ? 'Wrote ' + baseName(a.path) + ' ✓' : 'Write: ' + (r.reason || 'not done');
      case 'move_file': return r.verified ? 'Moved ' + baseName(a.source) + ' ✓' : 'Move: ' + (r.reason || 'not done');
      case 'search_files': return `Searched “${a.query || ''}” (${r.count != null ? r.count : '?'} found)`;
      case 'list_dir': return 'Looked in ' + (baseName(a.path) || 'your folder');
      case 'remember': return 'Remembered ' + (a.key || '');
      case 'set_reminder': return r.verified ? `Reminder set for ${r.due || 'later'} ✓` : 'Reminder: ' + (r.reason || 'not set');
      case 'cancel_reminder': return r.verified ? `Reminder #${r.id} cancelled ✓` : 'Reminder: ' + (r.reason || 'not cancelled');
      case 'list_reminders': return `Checked reminders (${r.count != null ? r.count : '?'})`;
      case 'finish': return 'Finished';
      default: return e.name;
    }
  }

  function baseName(p) {
    if (!p) return '';
    const parts = String(p).split(/[\\/]/);
    return parts[parts.length - 1] || String(p);
  }

  function reduce(prev, e) {
    const s = JSON.parse(JSON.stringify(prev || initial()));
    const at = e.ts || 0;
    s.lastEvent = at || s.lastEvent;
    const tid = e.task_id != null ? e.task_id : e.id;
    switch (e.type) {
      case 'hello':
        s.connected = true;
        s.approvals = {};
        for (const a of e.pending || []) s.approvals[a.id] = a;
        break;
      case 'disconnected':
        s.connected = false;
        break;
      case 'queued':
        ensureTask(s, e.task_id, e.goal, at);
        break;
      case 'task': {
        const t = ensureTask(s, e.id, e.goal, at);
        t.status = 'running';
        s.current = e.id;
        break;
      }
      case 'tool': {
        const t = ensureTask(s, e.task_id, '', at);
        if (e.name === 'finish') break;
        const r = e.result || {};
        addStep(t, { kind: 'tool', name: e.name, label: toolLabel(e), badge: e.badge || 'on this computer',
                     ok: !r.error && r.done !== false && r.sent !== false, at });
        if (r.verified === true) t.verified = true;
        break;
      }
      case 'form': {
        const t = ensureTask(s, e.task_id, '', at);
        if (e.phase === 'step') addStep(t, { kind: 'form', label: e.action || 'browser step', badge: e.badge, ok: true, at });
        else if (e.phase === 'filled') addStep(t, { kind: 'form', label: `Filled ${e.filled} field(s)` +
          ((e.left_for_you || []).length ? `, left ${e.left_for_you.length} for you` : ''), badge: e.badge, ok: true, at });
        break;
      }
      case 'usage': {
        const badge = e.badge || e.model || 'model';
        const u = s.usage.byBadge[badge] || { calls: 0, cost: 0, input: 0, output: 0 };
        u.calls += 1;
        u.cost += e.cost_usd || 0;
        u.input += e.input_tokens || 0;
        u.output += e.output_tokens || 0;
        s.usage.byBadge[badge] = u;
        s.usage.total += e.cost_usd || 0;
        const t = e.task_id != null ? s.tasks[e.task_id] : null;
        if (t) {
          t.cost += e.cost_usd || 0;
          const last = t.steps[t.steps.length - 1];
          const label = /wonder/i.test(badge) ? 'Looking at the screen' : /voice/i.test(badge) ? 'Listening' : 'Thinking';
          if (!last || last.badge !== badge) addStep(t, { kind: 'model', label, badge, ok: true, at });
        }
        break;
      }
      case 'approval': {
        const a = e.approval || {};
        if (e.event === 'requested') s.approvals[a.id] = a;
        else delete s.approvals[a.id];
        break;
      }
      case 'done': {
        const t = ensureTask(s, e.id, '', at);
        t.status = e.status;
        t.summary = e.summary || '';
        t.reply = e.reply || '';
        t.endedAt = at;
        if (typeof e.cost_usd === 'number') t.cost = e.cost_usd;
        if (s.current === e.id) s.current = null;
        s.lastDone = { id: e.id, status: e.status, verified: !!t.verified, at };
        if (e.status === 'failed') s.lastError = at;
        break;
      }
      case 'stopped':
        s.lastStop = at;
        s.approvals = {};
        break;
      case 'channel_error':
        s.lastError = at;
        break;
      case 'retry':
        s.lastRetry = { at, wait_s: e.wait_s || 0, error: e.error || '' };
        break;
      case 'reminder':
        s.lastReminder = { text: e.text || '', late: !!e.late, at };
        break;
      case 'checkin':
        s.lastCheckin = { reaction: e.reaction || '', found: e.found || 0, at };
        break;
      case 'forgot':
        if (e.everything) {
          s.tasks = {};
          s.order = [];
          s.usage = { byBadge: {}, total: 0 };
          s.lastDone = null;
        } else if (e.task_id != null) {
          delete s.tasks[e.task_id];
          s.order = s.order.filter((id) => id !== e.task_id);
        }
        break;
      default:
        break;
    }
    return s;
  }

  function pendingList(s) {
    return Object.values(s.approvals).sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
  }

  function recentTasks(s, n) {
    return s.order.slice(-(n || 10)).reverse().map((id) => s.tasks[id]);
  }

  return { initial, reduce, pendingList, recentTasks, toolLabel };
});
