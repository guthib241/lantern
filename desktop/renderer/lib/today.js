/* The Today view: four at-a-glance tiles and a few one-tap suggestions. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternToday = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const SUGGESTIONS = [
    { label: 'Fill the reimbursement form', goal: 'Fill the hackathon reimbursement form for my taxi and email it to the organiser.' },
    { label: 'Tidy Downloads', goal: 'Tidy my Downloads folder.' },
    { label: 'Projects using torch', goal: 'Which projects use torch?' },
    { label: 'Remind me in 20 min', goal: 'Remind me in 20 minutes to ' },
    { label: 'Water timer', goal: 'Make me a water timer.' },
  ];

  function startOfDay(nowSec) {
    const d = new Date(nowSec * 1000);
    d.setHours(0, 0, 0, 0);
    return d.getTime() / 1000;
  }

  function money(usd) {
    if (!usd) return '$0.00';
    return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
  }

  /** state: the reducer state; extra: {reminders: [{due, text}], now: seconds, budget: /usage's budget}. */
  function glance(state, extra) {
    const now = (extra && extra.now) || Date.now() / 1000;
    const since = startOfDay(now);
    const tasks = Object.values(state.tasks || {});
    const doneToday = tasks.filter((t) => t.status === 'done' && (t.endedAt || 0) >= since);
    const verified = doneToday.filter((t) => t.verified).length;
    const pending = Object.keys(state.approvals || {}).length;
    const running = state.current != null ? state.tasks[state.current] : null;
    const next = ((extra && extra.reminders) || [])[0];
    return [
      { id: 'needs', label: 'Needs you', tone: pending ? 'orange' : 'gray',
        value: pending ? String(pending) : running ? '…' : '0',
        note: pending ? (pending === 1 ? 'approval waiting' : 'approvals waiting') : running ? 'working on it' : 'nothing waiting' },
      { id: 'done', label: 'Done today', tone: doneToday.length ? 'green' : 'gray', value: String(doneToday.length),
        note: doneToday.length ? `${verified} verified` : 'no tasks yet' },
      { id: 'next', label: 'Next reminder', tone: next ? 'blue' : 'gray', value: next ? next.due : '—',
        note: next ? next.text : 'none set' },
      { id: 'spend', label: 'Spent today', tone: 'purple', value: money((state.usage && state.usage.total) || 0),
        note: spendNote(extra && extra.budget) },
    ];
  }

  function spendNote(b) {
    if (!b) return 'model calls only';
    if (b.total_usd == null) return 'no spending limit set';
    return `${money(b.left_usd)} left of ${money(b.total_usd)}`;
  }

  /** The Costs tab: what's spent against the limits Lantern enforces (budget.py), in plain words. */
  function budgetView(b, totalUsd) {
    const spent = b ? b.spent_usd : (totalUsd || 0);
    if (!b || b.total_usd == null) {
      return { headline: `${money(spent)} spent`, percent: 0, limits: b ? 'No total limit set (LANTERN_BUDGET_USD).' : '' };
    }
    const parts = [`Stops itself at ${money(b.total_usd)}`];
    if (b.task_usd != null) parts.push(`${money(b.task_usd)} per task`);
    if (b.daily_usd != null) parts.push(`${money(b.daily_usd)} a day (${money(b.today_usd)} today)`);
    return { headline: `${money(spent)} of ${money(b.total_usd)} spent`,
             percent: Math.min(100, Math.round((spent / b.total_usd) * 1000) / 10), limits: parts.join(' · ') + '.' };
  }

  function greeting(nowSec) {
    const h = new Date(nowSec * 1000).getHours();
    return h < 5 ? 'Up late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  }

  function dateLine(nowSec) {
    return new Date(nowSec * 1000).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
  }

  return { glance, greeting, dateLine, budgetView, SUGGESTIONS };
});
