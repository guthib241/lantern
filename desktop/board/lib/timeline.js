/* Teaching board: the pure parts (no DOM). Validation beyond what the JSON schema can say (ids that must exist
 * earlier, erased items), timing, and data-to-board coordinates. Works in the page and under node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternBoardTimeline = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const W = 1600;
  const H = 900;
  const MARGIN = 80;
  const COLORS = { chalk: '#f4f1e8', yellow: '#ffd75e', blue: '#8ccfff', orange: '#ffad5c', green: '#93e6a0',
                   pink: '#ffa6d2', red: '#ff8a80' };
  const ACTIONS = ['write_math', 'write_text', 'draw', 'highlight', 'erase', 'pause'];
  const SHAPES = ['line', 'arrow', 'circle', 'axes', 'plot'];
  const ID = /^[a-z][a-z0-9_-]{0,31}$/;

  function color(name, fallback) {
    if (!name) return fallback;
    return COLORS[name] || name;
  }

  /** Seconds a person needs to hear a line (about 155 words a minute), used when there's no voice. */
  function speechSeconds(text) {
    const words = String(text || '').trim().split(/\s+/).filter(Boolean).length;
    return words ? 0.4 + words / 2.6 : 0;
  }

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  /** How long an action's own animation takes at speed 1. */
  function revealSeconds(a) {
    if (typeof a.seconds === 'number') return a.seconds;
    switch (a.do) {
      case 'write_math': return clamp(0.6 + a.latex.length * 0.035, 0.8, 4);
      case 'write_text': return clamp(0.4 + a.text.length * 0.03, 0.6, 3.5);
      case 'draw': return { line: 0.7, arrow: 0.7, circle: 0.8, axes: 1.0, plot: 1.6 }[a.shape] || 0.8;
      case 'highlight': return 0.5;
      case 'erase': return 0.45;
      default: return 0;
    }
  }

  /** A data point on an axes action -> board units. */
  function dataToBoard(axes, point) {
    const [x0, x1] = axes.xrange;
    const [y0, y1] = axes.yrange;
    return [axes.x + ((point[0] - x0) / (x1 - x0)) * axes.w, axes.y + axes.h - ((point[1] - y0) / (y1 - y0)) * axes.h];
  }

  function targets(t) {
    return t === 'all' ? [] : Array.isArray(t) ? t : [t];
  }

  /** Problems with a lesson, in plain words; [] when it can be played. */
  function validate(lesson) {
    const errors = [];
    if (!lesson || typeof lesson !== 'object') return ['the lesson is not a JSON object'];
    if (lesson.version !== 1) errors.push('version must be 1');
    if (typeof lesson.title !== 'string' || !lesson.title.trim()) errors.push('the lesson needs a title');
    if (!Array.isArray(lesson.actions) || !lesson.actions.length) return errors.concat('the lesson has no actions');
    const live = new Map();      // id -> action, for items still on the board
    const seen = new Set();
    lesson.actions.forEach((a, i) => {
      const at = `action ${i + 1}${a && a.id ? ` (${a.id})` : ''}`;
      if (!a || !ACTIONS.includes(a.do)) { errors.push(`${at}: unknown action "${a && a.do}"`); return; }
      if (a.id !== undefined) {
        if (!ID.test(a.id)) errors.push(`${at}: id must be lowercase letters, digits, - or _`);
        else if (seen.has(a.id)) errors.push(`${at}: id "${a.id}" is used twice`);
      }
      const need = (cond, msg) => { if (!cond) errors.push(`${at}: ${msg}`); };
      const ref = (id, what) => need(live.has(id), `${what} "${id}" isn't on the board at this point`);
      const inBoard = (x, y) => need((x === undefined || (x >= 0 && x <= W)) && (y === undefined || (y >= 0 && y <= H)),
                                     'position is off the 1600 x 900 board');
      switch (a.do) {
        case 'write_math':
          need(typeof a.latex === 'string' && a.latex.trim(), 'write_math needs latex');
          inBoard(a.x, a.y);
          if (a.check && a.check.of) ref(a.check.of, 'check.of');
          need(!(a.verified === true && !a.check), 'a verified line must say what was checked (check)');
          break;
        case 'write_text':
          need(typeof a.text === 'string' && a.text.trim(), 'write_text needs text');
          inBoard(a.x, a.y);
          break;
        case 'draw':
          need(SHAPES.includes(a.shape), `unknown shape "${a.shape}"`);
          if (a.shape === 'line' || a.shape === 'arrow') {
            need(Array.isArray(a.from) && Array.isArray(a.to), `${a.shape} needs from and to`);
            if (a.from) inBoard(a.from[0], a.from[1]);
            if (a.to) inBoard(a.to[0], a.to[1]);
          }
          if (a.shape === 'circle') {
            need(a.around || (Array.isArray(a.center) && a.r > 0), 'circle needs center and r, or around');
            if (a.around) ref(a.around, 'around');
            if (a.axes) ref(a.axes, 'axes');
          }
          if (a.shape === 'axes') {
            need(a.id, 'axes need an id so plots can use them');
            need(a.w > 0 && a.h > 0 && a.xrange && a.yrange && a.xrange[0] < a.xrange[1] && a.yrange[0] < a.yrange[1],
                 'axes need w, h and increasing xrange/yrange');
            inBoard(a.x, a.y);
            inBoard(a.x + (a.w || 0), a.y + (a.h || 0));
          }
          if (a.shape === 'plot') {
            ref(a.axes, 'axes');
            need(Array.isArray(a.points) && a.points.length >= 2, 'a plot needs at least two points');
            if (live.has(a.axes) && live.get(a.axes).shape !== 'axes') errors.push(`${at}: "${a.axes}" is not axes`);
          }
          break;
        case 'highlight':
          ref(a.target, 'target');
          break;
        case 'erase':
          if (a.target === 'all') live.clear();
          for (const t of targets(a.target)) { ref(t, 'target'); live.delete(t); }
          break;
        case 'pause':
          need(typeof a.seconds === 'number' && a.seconds > 0, 'pause needs seconds');
          break;
        default:
          break;
      }
      if (a.id && a.do !== 'erase') { live.set(a.id, a); seen.add(a.id); }
    });
    return errors;
  }

  /** Rough length of a lesson at speed 1 (animation or speech, whichever is longer per action). */
  function lessonSeconds(lesson) {
    return lesson.actions.reduce((t, a) => t + Math.max(revealSeconds(a), speechSeconds(a.say)), 0);
  }

  return { W, H, MARGIN, COLORS, ACTIONS, color, speechSeconds, revealSeconds, dataToBoard, validate, lessonSeconds, targets };
});
