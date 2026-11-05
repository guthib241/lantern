/* Learned routines (S4) in the panel: one line per routine and a small chart of cost per run.
 * Pure: data in, plain data out. The panel builds the chart's SVG elements from `chartData` with DOM calls
 * (renderers never use innerHTML); names and titles are always set as text. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternRoutines = api;
})(typeof self !== 'undefined' ? self : this, function () {
  function money(usd) {
    const v = Number(usd) || 0;
    return v === 0 ? '$0.00' : v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
  }

  /** "v2 · 3 runs · saved $0.12 · healed once" */
  function line(r) {
    const parts = [`v${r.version}`, `${r.runs} run${r.runs === 1 ? '' : 's'}`];
    if (r.saved_usd > 0) parts.push(`saved ${money(r.saved_usd)}`);
    if (r.heals) parts.push(r.heals === 1 ? 'healed once' : `healed ${r.heals} times`);
    if (!r.trusted) parts.push('not verified');
    return parts.join(' · ');
  }

  /** Cost per run: the task it was learned from first, then every run; a mark over the runs that healed. */
  function chartData(r, width, height) {
    const w = width || 240, h = height || 56, pad = 4;
    const points = [{ cost: Number(r.learned_cost_usd) || 0, learned: true }]
      .concat((r.history || []).map((x) => ({ cost: Number(x.cost_usd) || 0, healed: !!x.healed, ok: x.ok !== false })));
    const max = Math.max(...points.map((p) => p.cost), 0.0001);
    const step = (w - 2 * pad) / Math.max(points.length, 1);
    const barW = Math.max(2, Math.min(18, step * 0.7));
    const bars = points.map((p, i) => {
      const bh = p.cost > 0 ? Math.max(2, (p.cost / max) * (h - 18)) : 1;
      const x = pad + i * step + (step - barW) / 2, y = h - 4 - bh;
      const cls = p.learned ? 'learned' : p.ok === false ? 'failed' : 'run';
      return { cls, x: +x.toFixed(1), y: +y.toFixed(1), w: +barW.toFixed(1), h: +bh.toFixed(1),
               heal: p.healed ? `M${(x + barW / 2).toFixed(1)} ${(y - 9).toFixed(1)} l4 6 h-8 z` : null };
    });
    const runs = points.length - 1;
    const label = `Cost per run: ${money(points[0].cost)} when learned, then ${runs} run${runs === 1 ? '' : 's'}` +
      (runs ? `, last ${money(points[points.length - 1].cost)}` : '') + (r.heals ? `, healed ${r.heals}` : '');
    return { w, h, label, bars };
  }

  return { line, chartData, money };
});
