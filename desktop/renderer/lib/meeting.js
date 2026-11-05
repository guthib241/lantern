/* Meeting recording → follow-ups (S15) in the panel: how turns and proposed items read, and what Add sends. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternMeeting = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const clock = (ms) => {
    const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  };

  /** Speakers keep their label ("A", "B"; not names) and a colour slot in order of first appearance. */
  function turns(result) {
    const slots = new Map();
    return ((result && result.turns) || []).map((t) => {
      const who = String(t.speaker || '?');
      if (!slots.has(who)) slots.set(who, slots.size % 4);
      return { who, slot: slots.get(who), text: String(t.text || ''), at: clock(t.start_ms) };
    });
  }

  /** Every proposed item starts ticked; a reminder says when, in the computer's own time. */
  function items(result, locale) {
    return ((result && result.items) || []).map((i) => ({
      n: Number(i.n), text: String(i.text || ''), checked: true,
      kind: i.kind === 'reminder' && i.due_at ? 'reminder' : 'task',
      speaker: String(i.speaker || '?'),
      when: i.kind === 'reminder' && i.due_at
        ? new Date(i.due_at * 1000).toLocaleString(locale, { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : '',
    }));
  }

  /** The POST /meeting/confirm body: only the ticked numbers, for this proposal. */
  function confirm(result, ticked) {
    return { id: String(result.id), picks: [...ticked].map(Number).sort((a, b) => a - b) };
  }

  function summary(result) {
    const n = (result && result.items || []).length;
    const secs = Math.round((result && result.seconds) || 0);
    return `${n} follow-up${n === 1 ? '' : 's'} from ${clock(secs * 1000)} of audio` +
      (result && result.dropped ? ` · left out ${result.dropped} with an ID or card number` : '');
  }

  return { turns, items, confirm, summary, clock };
});
