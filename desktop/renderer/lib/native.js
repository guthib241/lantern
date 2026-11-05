/* What the menu bar icon, the badge and system notifications say. Pure; used by main.js.
 * Notifications are deliberately terse: an approval shows only its title ("Submit this form?"),
 * never the values, so nothing private lands on a lock screen or in notification history. */
(function (root, factory) {
  const deps = typeof module === 'object' && module.exports
    ? { activity: require('./activity.js'), cards: require('./cards.js'), state: require('./state.js') }
    : { activity: root.LanternActivity, cards: root.LanternCards, state: root.LanternState };
  const api = factory(deps);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternNative = api;
})(typeof self !== 'undefined' ? self : this, function ({ activity, cards, state: S }) {
  function clip(text, n) {
    const t = String(text || '').replace(/\s+/g, ' ').trim();
    return t.length > n ? t.slice(0, n - 1) + '…' : t;
  }

  /** For the tray: tooltip, the status line in the menu, and the badge count. */
  function status(state, now, connected) {
    const pending = S.pendingList(state).length;
    const a = activity.activity(state, now);
    let line = 'Idle';
    if (!connected) line = 'Offline: is `lantern --serve` running?';
    else if (pending) line = `${pending} approval${pending === 1 ? '' : 's'} waiting`;
    else if (a.mode === 'working') line = `Working: ${clip(a.text, 40)}`;
    else if (a.mode !== 'idle') line = clip(a.text, 48);
    return { line, tooltip: `Lantern · ${line}`, pending, badge: pending, working: a.mode === 'working' };
  }

  /** One event -> a notification to show, or null. opts.panelFocused: the panel is in front already. */
  function notice(event, state, opts) {
    const focused = !!(opts && opts.panelFocused);
    if (event.type === 'approval' && event.event === 'requested' && !focused) {
      const c = cards.card(event.approval || {}, (opts && opts.now) || Date.now() / 1000);
      return { kind: 'approval', title: 'Lantern needs your OK', body: c.title + (c.untrusted ? ' Review it in Lantern.' : '') };
    }
    if (event.type === 'reminder') {
      return { kind: 'reminder', title: 'Reminder', body: clip(event.text, 120) + (event.late ? ' (it was due while Lantern was off)' : '') };
    }
    if (event.type === 'done' && event.status === 'failed' && !focused) {
      return { kind: 'failed', title: 'Lantern couldn’t finish', body: clip(event.summary || event.reply, 120) };
    }
    return null;
  }

  return { status, notice, clip };
});
