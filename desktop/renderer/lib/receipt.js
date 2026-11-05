/* A task's receipt as sections of rows for the receipt sheet. Pure; the wording comes from the server
 * (lantern/receipt.py), so the desktop and the phone say the same thing. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternReceipt = api;
})(typeof self !== 'undefined' ? self : this, function () {
  function money(usd) {
    if (!usd) return '$0.00';
    return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
  }

  function size(n) {
    return n < 10000 ? `${n.toLocaleString('en-US')} bytes` : `${Math.round(n / 1024).toLocaleString('en-US')} KB`;
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  /** r: GET /tasks/{id}/receipt. Returns {title, status, sections: [{title, rows: [{tone, mark, text, detail}]}]}. */
  function view(r) {
    const did = r.actions.map((a) => ({ tone: a.verified ? 'good' : 'bad', mark: a.verified ? '✓' : '✗', text: a.what,
                                         detail: a.verified ? 'checked' : (a.note || 'not checked') }));
    if (r.looked) {
      did.push({ tone: 'quiet', mark: '·', text: r.looked === 1 ? 'Looked something up' : `Looked things up ${r.looked} times`,
                 detail: 'reading and searching; nothing changed' });
    }
    const sections = [{ title: 'What it did', rows: did.length ? did : [{ tone: 'quiet', mark: '·', text: 'Nothing changed', detail: '' }] }];
    if (r.approvals.length) {
      sections.push({ title: 'What you approved', rows: r.approvals.map((a) => ({
        tone: a.status === 'approved' ? 'good' : 'bad', mark: a.status === 'approved' ? '✓' : '✗', text: a.asked,
        detail: [`${a.status} ${a.where}`.trim(), a.used ? 'used once' : '', `bound to ${a.payload_hash.slice(0, 12)}`]
          .filter(Boolean).join(' · ') })) });
    }
    const data = r.sent.map((s) => ({
      tone: s.lane === 'local' || s.simulated ? 'quiet' : 'info', mark: s.lane === 'local' ? '⌂' : s.simulated ? '○' : '↑',
      text: s.lane === 'local' ? `${plural(s.calls, 'question')} to the private lane` : `${size(s.bytes)} to ${s.to}`,
      detail: s.lane === 'local' ? 'stayed on this computer'
        : [s.kinds.join(', '), plural(s.calls, 'call'), s.simulated ? 'simulated: nothing actually left' : ''].filter(Boolean).join(' · ') }));
    const red = Object.entries(r.redactions || {});
    if (red.length) data.push({ tone: 'good', mark: '✓', text: 'Redacted before sending',
                                detail: red.map(([k, v]) => `${k.replace(/_/g, ' ')} ×${v}`).join(', ') });
    sections.push({ title: 'What left this laptop', rows: data.length ? data : [{ tone: 'good', mark: '✓', text: 'Nothing', detail: 'no calls were made' }] });
    sections.push({ title: 'Cost', rows: [{ tone: 'quiet', mark: '$', text: money(r.cost_usd),
                                            detail: r.models.map((m) => `${m.badge} ×${m.calls}`).join(', ') || 'no model calls' }] });
    return { title: r.task.goal.split('\n')[0], status: r.task.status, sections };
  }

  return { view, money, size };
});
