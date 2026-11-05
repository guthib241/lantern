/* The live activity pill: one line about what Lantern is doing right now, and (when an approval is
 * waiting) a compact card. Pure: (state, now) -> what to show. Uses LanternCards / LanternExpression
 * when they are loaded (browser globals) or required (node --test). */
(function (root, factory) {
  const deps = typeof module === 'object' && module.exports
    ? { cards: require('./cards.js'), expression: require('./expression.js'), state: require('./state.js') }
    : { cards: root.LanternCards, expression: root.LanternExpression, state: root.LanternState };
  const api = factory(deps);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternActivity = api;
})(typeof self !== 'undefined' ? self : this, function ({ cards, expression, state: S }) {
  const RECENT = { done: 8, reminder: 15, stopped: 6 };
  const VERBS = { click: 'Clicking', double_click: 'Double-clicking', type: 'Typing', keypress: 'Pressing keys',
                  scroll: 'Scrolling', drag: 'Dragging', move: 'Moving the pointer', wait: 'Waiting for the page' };

  /** A form step label like "click (429, 313)" as words. */
  function friendly(label) {
    const m = /^([a-z_]+)\b(.*)$/i.exec(String(label || ''));
    if (m && VERBS[m[1].toLowerCase()]) return VERBS[m[1].toLowerCase()] + (m[1].toLowerCase() === 'click' ? ` at${m[2]}` : '');
    return String(label || '');
  }

  function shortBadge(badge) {
    const b = String(badge || '');
    return /on this computer/i.test(b) ? 'Local' : b.replace(/^Muse\s+/i, '');
  }

  /**
   * mode: idle | working | approval | done | failed | reminder | retry | stopped
   * quickAllow: true only for a clean approval (no untrusted content read, no warnings, not expired);
   * anything flagged must be reviewed in the panel, where the whole card is shown.
   */
  function activity(state, now) {
    const pending = S.pendingList(state);
    if (pending.length) {
      const a = pending[0];
      const card = cards.card(a, now);
      return { mode: 'approval', text: card.title, badge: pending.length > 1 ? `+${pending.length - 1}` : '',
               approval: a, card,
               quickAllow: !card.untrusted && !card.warnings.length && !card.expired && card.tier !== 'red',
               detail: card.fields ? `${card.lead} · ${card.fields.length} fields` : card.summary };
    }
    const since = (t) => (t ? now - t : Infinity);
    if (state.lastReminder && since(state.lastReminder.at) < RECENT.reminder) {
      return { mode: 'reminder', text: state.lastReminder.text, badge: state.lastReminder.late ? 'late' : '' };
    }
    if (since(state.lastStop) < RECENT.stopped) return { mode: 'stopped', text: 'Stopped', badge: '' };
    const r = state.lastRetry;
    if (r && state.current != null && since(r.at) < r.wait_s + 2) {
      return { mode: 'retry', text: `Network wobbly · retrying in ${Math.ceil(r.wait_s)} s`, badge: '' };
    }
    if (state.current != null) {
      const t = state.tasks[state.current];
      const last = t && t.steps[t.steps.length - 1];
      return { mode: 'working', text: last ? friendly(last.label) : 'Starting…', badge: last ? shortBadge(last.badge) : '',
               detail: t ? t.goal : '', steps: t ? t.steps.length : 0 };
    }
    const done = state.lastDone;
    if (done && since(done.at) < RECENT.done) {
      const t = state.tasks[done.id];
      const ok = done.status === 'done';
      return { mode: ok ? 'done' : 'failed', text: ok ? (done.verified ? 'Done · verified' : 'Done') : 'Didn’t work',
               badge: '', detail: expression.bubble(state, now, 'smug') || (t && t.reply) || '' };
    }
    return { mode: 'idle', text: '', badge: '' };
  }

  return { activity, friendly, shortBadge };
});
