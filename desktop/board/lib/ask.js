/* Teaching board: asking a question mid-lesson (S16). The pure parts (no DOM): how the question is asked
 * (voice through Lantern's /talk, or typed when there is no key / in rehearsal), what Space and the
 * "Hold to ask" button mean in each phase, and what may be shown from the server's answer. The board page
 * owns the microphone, the socket and the board; the server checks every math line before it gets here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternBoardAsk = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const MAX_ACTIONS = 3;
  const DO = ['write_math', 'write_text', 'draw', 'highlight', 'erase', 'pause'];

  /** Voice needs a real server with a key; rehearsal (demo) and keyless runs fall back to typing. */
  function mode(health) {
    return health && !health.demo && health.has_key ? 'voice' : 'typed';
  }

  /** The POST /lesson/ask body: the saved lesson's name, the step the board has reached, the question. */
  function askBody(name, step, question) {
    const q = String(question || '').trim().replace(/\s+/g, ' ').slice(0, 500);
    if (!name || !q) return null;
    const n = Math.max(0, Math.floor(Number(step) || 0));
    return { name: String(name), step: n, question: q };
  }

  /** What the board may show from an answer: the spoken line, and at most 3 actions with a known `do`.
   * Math lines keep their `verified` flag exactly as the server set it (only SymPy-checked lines true). */
  function showable(data) {
    if (!data || typeof data.spoken !== 'string' || !data.spoken.trim()) return null;
    const actions = (Array.isArray(data.actions) ? data.actions : [])
      .filter((a) => a && DO.includes(a.do)).slice(0, MAX_ACTIONS)
      .map((a) => (a.do === 'write_math' ? { ...a, verified: a.verified === true } : a));
    return { spoken: data.spoken.trim(), related: data.related !== false, actions };
  }

  /**
   * What a Space press (phase 'down') or release ('up') should do, given the ask state:
   *   idle     the lesson is playing/paused, no question open -> holding starts a question
   *   asking   voice is recording (or the typed box is open) -> releasing ends a voice question
   *   thinking the question is with the server -> nothing
   *   answered the answer is on the board -> a tap resumes the lesson
   */
  function space(state, phase) {
    if (phase === 'down') {
      if (state === 'answered') return 'resume';
      if (state === 'idle') return 'start';
      return 'none';
    }
    return state === 'asking' ? 'stop' : 'none';
  }

  return { mode, askBody, showable, space, MAX_ACTIONS };
});
