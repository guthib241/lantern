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
    organize_files: 'Tidy these files?',
    install_agent: 'Install this agent?',
    agent_face: "Draw this agent's face?",
    finish_avatar: 'Make this your avatar?',
    undo_moves: 'Put these files back?',
    set_reminder: 'Set this reminder?',
    cancel_reminder: 'Cancel this reminder?',
    mcp_call: "Use another program's tool?",
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
    const parsed = approval.action === 'submit_form' ? formFields(main) : null;
    return {
      id: approval.id,
      action: approval.action,
      title: TITLES[approval.action] || `Allow ${String(approval.action || 'this').replace(/_/g, ' ')}?`,
      summary: main,
      lead: parsed ? parsed.lead : '',
      fields: parsed ? parsed.fields : null,   // null: show the summary text as is
      warnings,
      leftForYou: left ? left[1].split(/,\s*/) : [],
      untrusted,
      expired: remaining <= 0,
      expiresIn: mmss(remaining),
      hash: String(approval.payload_hash || '').slice(0, 12),
      allowLabel: approval.action === 'submit_form' ? 'Allow Submit' : approval.action === 'send_email' ? 'Allow Send' : 'Allow',
    };
  }

  /** "Submit 'X' (local demo): Full name: Asha; Amount: 2400." -> {lead, fields:[{label, value}]}.
   *  Only when every piece reads as "label: value"; otherwise null, and the card shows the raw text,
   *  so nothing the user approves is ever hidden by the layout. */
  function formFields(text) {
    const cut = text.indexOf('): ');
    if (cut < 0) return null;
    const lead = text.slice(0, cut + 1);
    const body = text.slice(cut + 3).replace(/\.\s*$/, '');
    const fields = [];
    for (const piece of body.split('; ')) {
      const at = piece.lastIndexOf(': ');
      if (at <= 0) return null;
      fields.push({ label: piece.slice(0, at).trim(), value: piece.slice(at + 2).trim() });
    }
    const rebuilt = `${lead}: ${fields.map((f) => `${f.label}: ${f.value}`).join('; ')}`;
    return fields.length && rebuilt.replace(/\.$/, '') === text.replace(/\.\s*$/, '') ? { lead, fields } : null;
  }

  /** The POST /approvals/{id} body: the nonce from the card and the hash it showed. */
  function decision(approval, approve) {
    return { nonce: approval.nonce, approve: !!approve, expected_hash: approval.payload_hash };
  }

  return { card, decision, mmss, formFields, TITLES };
});
