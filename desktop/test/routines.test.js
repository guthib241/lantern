// node --test: learned routines in the panel (S4): the line under each routine and its cost chart.
const test = require('node:test');
const assert = require('node:assert/strict');

const R = require('../renderer/lib/routines.js');

const routine = {
  name: 'city-permit-application', title: 'City permit application', version: 2, trusted: true, runs: 3,
  saved_usd: 0.1234, heals: 1, learned_cost_usd: 0.05,
  history: [{ cost_usd: 0, healed: false, ok: true }, { cost_usd: 0.002, healed: true, ok: true },
            { cost_usd: 0, healed: false, ok: true }],
};

test('one line says version, runs, money saved and heals', () => {
  assert.equal(R.line(routine), 'v2 · 3 runs · saved $0.12 · healed once');
  assert.equal(R.line({ version: 1, runs: 1, saved_usd: 0, heals: 0, trusted: false }), 'v1 · 1 run · not verified');
});

test('the chart has the learned task first, a bar per run and a mark where it healed', () => {
  const d = R.chartData(routine);
  assert.equal(d.bars.length, 4);
  assert.deepEqual(d.bars.map((b) => b.cls), ['learned', 'run', 'run', 'run']);
  assert.equal(d.bars.filter((b) => b.heal).length, 1);
  assert.ok(d.bars[2].heal && !d.bars[1].heal);
  assert.equal(d.label, 'Cost per run: $0.05 when learned, then 3 runs, last $0.00, healed 1');
  assert.ok(d.bars[0].h > d.bars[2].h && d.bars[1].h === 1);   // $0 runs are a hairline
});
