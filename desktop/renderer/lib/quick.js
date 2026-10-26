/* Quick Ask: the rows under the search field, for a query. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternQuick = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const STOP_WORDS = /^\s*(stop|halt|cancel everything)\s*$/i;

  function words(text) {
    return String(text || '').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  }

  /** How well `label` matches the typed query: every typed word must start some word of the label. */
  function score(query, label) {
    const q = words(query);
    if (!q.length) return 1;
    const l = words(label);
    let total = 0;
    for (const w of q) {
      const hit = l.findIndex((x) => x.startsWith(w));
      if (hit < 0) return 0;
      total += hit === 0 ? 2 : 1;
    }
    return total;
  }

  /**
   * ctx: {suggestions: [{label, goal}], pending: number, recent: [{id, goal, status}]}
   * Rows: {kind: 'ask'|'stop'|'approvals'|'suggestion'|'recent', label, sub, goal?}
   */
  function rows(query, ctx) {
    const q = String(query || '').trim();
    const c = ctx || {};
    const out = [];
    if (STOP_WORDS.test(q)) out.push({ kind: 'stop', label: 'Stop everything', sub: 'Cancel the running task and deny every pending approval' });
    if (q) out.push({ kind: 'ask', label: `Ask Lantern: “${q.length > 70 ? q.slice(0, 69) + '…' : q}”`, sub: 'Return to start', goal: q });
    if (c.pending) {
      out.push({ kind: 'approvals', label: `${c.pending} approval${c.pending === 1 ? '' : 's'} waiting`, sub: 'Open Lantern to review' });
    }
    const sugg = (c.suggestions || []).map((s) => ({ s, n: score(q, s.label + ' ' + s.goal) })).filter((x) => x.n > 0)
      .sort((a, b) => b.n - a.n).slice(0, q ? 3 : 5);
    for (const { s } of sugg) out.push({ kind: 'suggestion', label: s.label, sub: s.goal, goal: s.goal });
    if (!q) {
      for (const t of (c.recent || []).slice(0, 3)) {
        out.push({ kind: 'recent', label: t.goal, sub: `#${t.id} · ${t.status}`, goal: t.goal });
      }
    }
    return out;
  }

  /** Arrow-key movement that wraps around. */
  function move(index, delta, count) {
    if (!count) return -1;
    return ((index + delta) % count + count) % count;
  }

  return { rows, score, move };
});
