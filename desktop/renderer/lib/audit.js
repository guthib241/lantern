/* "What left my laptop today": turns GET /audit into something a person can read. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternAudit = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const NAMES = {
    'muse-spark-1.3': 'Muse Spark', 'muse-wonder-1.0': 'Muse Wonder', 'muse-image-1.0': 'Muse Image',
    'muse-voice-transcribe-1.0': 'Muse Voice Transcribe', 'sam-3.1': 'SAM 3.1',
    whatsapp: 'WhatsApp messages', telegram: 'Telegram messages', email: 'Emails',
  };

  function bytes(n) {
    if (!n) return '0 bytes';
    if (n < 1024) return `${n} bytes`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  }

  function money(usd) {
    if (!usd) return '$0.00';
    return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
  }

  /** opts.rehearsal: the server runs --demo, so nothing actually left; say what *would* have. */
  function view(audit, opts) {
    const rehearsal = !!(opts && opts.rehearsal);
    const s = (audit && audit.summary) || { cloud: { calls: 0, bytes: 0, by_model: {}, redactions: {} },
                                            local: { calls: 0, bytes: 0 }, selfhosted: { calls: 0, bytes: 0 } };
    const c = s.cloud;
    const rows = Object.entries(c.by_model || {})
      .map(([model, m]) => ({ name: NAMES[model] || model, model, calls: m.calls, bytes: bytes(m.bytes), raw: m.bytes }))
      .sort((a, b) => b.raw - a.raw);
    const redactions = Object.entries(c.redactions || {}).map(([k, v]) => `${k.replace(/_/g, ' ')} ×${v}`);
    return {
      amount: c.calls ? bytes(c.bytes) : '0 bytes',
      caption: c.calls
        ? `${rehearsal ? 'would have left' : 'left'} this laptop today, in ${c.calls} call${c.calls === 1 ? '' : 's'}`
          + (rehearsal ? ' · rehearsal, nothing was sent' : '')
        : 'nothing has left this laptop today',
      headline: c.calls
        ? `${bytes(c.bytes)} ${rehearsal ? 'would have left' : 'left'} this laptop today, in ${c.calls} call${c.calls === 1 ? '' : 's'}`
          + (rehearsal ? ' (rehearsal: nothing was sent)' : '')
        : 'Nothing has left this laptop today',
      rows,
      redactions,
      redactedCount: Object.values(c.redactions || {}).reduce((a, b) => a + b, 0),
      privateLine: s.local.calls
        ? `${s.local.calls} private question${s.local.calls === 1 ? '' : 's'} answered on this computer · ${bytes(s.local.bytes)} stayed here`
        : 'No private files were read today',
      selfhosted: s.selfhosted && s.selfhosted.calls ? `${s.selfhosted.calls} calls to your own private server` : '',
    };
  }

  return { view, bytes, money };
});
