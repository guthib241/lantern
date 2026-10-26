/* Which face the avatar shows, from the reducer state. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternExpression = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const EXPRESSIONS = ['idle', 'thinking', 'smug', 'annoyed', 'celebrating', 'needs-approval', 'sleeping'];
  const SLEEP_AFTER = 5 * 60;   // seconds without events
  const CELEBRATE_FOR = 8;
  const SMUG_FOR = 6;
  const ANNOYED_FOR = 8;

  /** now: seconds since epoch (same clock as event.ts). */
  function pick(state, now) {
    if (Object.keys(state.approvals || {}).length) return 'needs-approval';
    const since = (t) => (t ? now - t : Infinity);
    if (since(state.lastStop) < ANNOYED_FOR) return 'annoyed';
    const done = state.lastDone;
    if (done && since(done.at) < ANNOYED_FOR && done.status === 'failed') return 'annoyed';
    if (since(state.lastError) < ANNOYED_FOR) return 'annoyed';
    if (state.lastCheckin && since(state.lastCheckin.at) < SMUG_FOR) return state.lastCheckin.found ? 'smug' : 'annoyed';
    if (done && done.verified && since(done.at) < CELEBRATE_FOR) return 'celebrating';
    if (done && done.status === 'done' && since(done.at) < SMUG_FOR) return 'smug';
    if (state.current != null) return 'thinking';
    if (state.lastEvent && since(state.lastEvent) > SLEEP_AFTER) return 'sleeping';
    return 'idle';
  }

  /** A short line for the speech bubble. */
  function bubble(state, now, persona) {
    const face = pick(state, now);
    const c = state.lastCheckin;
    if (c && now - c.at < 8 && face !== 'needs-approval') return c.reaction;
    const r = state.lastRetry;
    if (r && now - r.at < r.wait_s + 2 && state.current != null) return `The network's wobbly. Retrying in ${Math.ceil(r.wait_s)} s…`;
    const n = Object.keys(state.approvals || {}).length;
    const lines = {
      'needs-approval': n > 1 ? `${n} things need your OK.` : 'I need your OK for this one.',
      annoyed: state.lastStop && now - state.lastStop < ANNOYED_FOR ? 'Fine. Stopped.' : 'That did not go to plan.',
      celebrating: 'Done, and I checked it. You are welcome.',
      smug: 'Done. Obviously.',
      thinking: 'Working on it…',
      sleeping: 'zzz',
      idle: '',
    };
    if (persona === 'helpful') {
      lines.smug = 'All done!';
      lines.celebrating = 'Done and verified!';
      lines.annoyed = 'Hmm, that did not work.';
    }
    if (face === 'smug' || face === 'celebrating') {
      const t = state.lastDone && state.tasks[state.lastDone.id];
      if (t && t.reply) return t.reply.length > 140 ? t.reply.slice(0, 139) + '…' : t.reply;
    }
    return lines[face];
  }

  return { EXPRESSIONS, pick, bubble };
});
