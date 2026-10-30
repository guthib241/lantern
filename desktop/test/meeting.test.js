// node --test: the Meetings panel's pure logic (S15).
const test = require('node:test');
const assert = require('node:assert/strict');
const M = require('../renderer/lib/meeting.js');

const RESULT = {
  id: 'm7', seconds: 22.4, dropped: 1,
  turns: [{ speaker: 'A', text: 'I will send the budget.', start_ms: 0 }, { speaker: 'B', text: 'Remind me at 3.', start_ms: 6700 },
          { speaker: 'A', text: 'Book the room.', start_ms: 65000 }],
  items: [{ n: 1, kind: 'task', text: 'Send the budget sheet', speaker: 'A', due_at: null },
          { n: 2, kind: 'reminder', text: 'Call the venue', speaker: 'B', due_at: 1793286000 },
          { n: 3, kind: 'reminder', text: 'No time given', speaker: '?', due_at: null }],
};

test('turns keep speaker labels with a colour slot each, and a clock', () => {
  const t = M.turns(RESULT);
  assert.deepEqual(t.map((x) => [x.who, x.slot, x.at]), [['A', 0, '0:00'], ['B', 1, '0:06'], ['A', 0, '1:05']]);
});

test('items start ticked; only a reminder with a time is shown as one; confirm sends only ticked numbers', () => {
  const i = M.items(RESULT, 'en-GB');
  assert.deepEqual(i.map((x) => [x.n, x.kind, x.checked]), [[1, 'task', true], [2, 'reminder', true], [3, 'task', true]]);
  assert.ok(i[1].when && !i[0].when);
  assert.deepEqual(M.confirm(RESULT, new Set([3, 1])), { id: 'm7', picks: [1, 3] });
  assert.equal(M.summary(RESULT), '3 follow-ups from 0:22 of audio · left out 1 with an ID or card number');
  assert.deepEqual(M.items({}), []);
});
