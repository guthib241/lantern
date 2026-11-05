/* Teach once by demonstration (S8) in the panel: the "watching you" card while Lantern records, and the start
 * form's checks. Pure: events in, plain data out. The recording's values never reach the panel: teach events carry
 * only what kind of step happened and how many. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternTeach = api;
})(typeof self !== 'undefined' ? self : this, function () {
  /** The session after one event: null when nothing is being recorded. */
  function apply(session, e) {
    if (!e || e.type !== 'teach') return session;
    if (e.phase === 'recording') return { taskId: e.task_id, url: e.url || '', seen: 0, notes: 0 };
    if (!session || session.taskId !== e.task_id) return session;
    if (e.phase === 'event') return { ...session, seen: Number(e.count) || session.seen + 1 };
    if (e.phase === 'note') return { ...session, notes: session.notes + 1 };
    if (e.phase === 'stopped') return null;
    return session;
  }

  function host(url) {
    try { return new URL(url).host; } catch (e) { return ''; }
  }

  /** What the card says. */
  function card(session, goal) {
    if (!session) return null;
    const where = host(session.url);
    const n = session.seen;
    return {
      title: 'Watching you',
      text: `${goal ? `${goal}: d` : 'D'}o it once in Lantern's browser${where ? ` on ${where}` : ''}. ` +
            'Press Done after the confirmation page.',
      count: n === 0 ? 'Nothing seen yet' : n === 1 ? '1 step seen' : `${n} steps seen`,
      notes: session.notes ? `${session.notes} note${session.notes === 1 ? '' : 's'}` : '',
    };
  }

  /** '' when the start form can be sent, else what's wrong. */
  function check(url, goal) {
    if (!/^https?:\/\/\S+$/i.test(String(url || '').trim())) return 'Give the web address where the task starts.';
    if (!String(goal || '').trim()) return 'Say what the task is, for example "apply for a city permit".';
    return '';
  }

  return { apply, card, check, host };
});
