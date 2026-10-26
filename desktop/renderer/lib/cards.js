/* Approval cards: what the user sees before saying yes. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternCards = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const TITLES = {
    submit_form: 'Submit this form?',
    send_email: 'Send this email?',
    write_file: 'Save this file?',
    move_file: 'Move this file?',
    remember: 'Remember this?',
    forget: 'Forget this?',
    cua_safety_check: 'Safety check from the browser model',
    save_widget: 'Add this widget?',
    run_in_project: 'Run this command?',
  };
  const UNTRUSTED_MARK = '[after reading untrusted content]';

  function mmss(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  /** approval: as sent by the server (id, nonce, action, summary, payload_hash, expires_at, tainted). */
  function card(approval, now) {
    let summary = String(approval.summary || '');
    const untrusted = !!approval.tainted || summary.includes(UNTRUSTED_MARK);
    summary = summary.replace(UNTRUSTED_MARK, '').trim();
    const left = summary.match(/\s*Left for you: ([^.]*)\./);
    if (left) summary = summary.replace(left[0], '').trim();   // shown on its own line
    const warnings = [];
    const pieces = summary.split(/\s(?=[⚠️🔒])/u);
    const main = pieces.shift();
    for (const w of pieces) warnings.push(w.trim());
    const remaining = (approval.expires_at || 0) - now;
    return {
      id: approval.id,
      action: approval.action,
      title: TITLES[approval.action] || `Allow ${String(approval.action || 'this').replace(/_/g, ' ')}?`,
      summary: main,
      warnings,
      leftForYou: left ? left[1].split(/,\s*/) : [],
      untrusted,
      expired: remaining <= 0,
      expiresIn: mmss(remaining),
      hash: String(approval.payload_hash || '').slice(0, 12),
      allowLabel: approval.action === 'submit_form' ? 'Allow Submit' : approval.action === 'send_email' ? 'Allow Send' : 'Allow',
    };
  }

  /** The POST /approvals/{id} body: the nonce from the card and the hash it showed. */
  function decision(approval, approve) {
    return { nonce: approval.nonce, approve: !!approve, expected_hash: approval.payload_hash };
  }

  return { card, decision, mmss, TITLES };
});
