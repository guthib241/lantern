/* The setup check (GET /doctor) as plain lines for the panel. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternSetup = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const MARK = { ok: '✓', warn: '!', fail: '✗', off: '○' };
  const ORDER = { fail: 0, warn: 1, off: 2, ok: 3 };

  function line(goal, r) {
    if (!r) return '';
    if (r.blocked_by.length) return `${goal}: blocked by ${r.blocked_by.join(', ')}` + (r.to_do.length ? ` · then ${r.to_do.join(', ')}` : '');
    return `${goal}: ready ✓` + (r.to_do.length ? ` · still to do: ${r.to_do.join(', ')}` : '');
  }

  /** d: {checks: [{id, title, status, detail, fix}], ready: {rehearsal, demo}} */
  function view(d) {
    const checks = (d && d.checks) || [];
    const rows = checks.map((c) => ({ ...c, mark: MARK[c.status] || '?', fix: c.status === 'ok' ? '' : c.fix || '' }))
      .sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9));
    const ready = (d && d.ready) || {};
    return { rows, demo: line('Real demo', ready.demo), rehearsal: line('Rehearsal', ready.rehearsal) };
  }

  return { view, MARK };
});
