// node --test: resume → profile (S15) in the panel: what starts ticked, and what Save sends.
const test = require('node:test');
const assert = require('node:assert/strict');

const C = require('../renderer/lib/cv.js');

test('new and changed values start ticked; saved ones do not; only ticked ones are sent', () => {
  const list = C.rows({ fields: [{ key: 'full name', value: 'Asha Rao', current: 'Asha R.' },
                                 { key: 'email', value: 'asha@example.com', current: '' },
                                 { key: 'city', value: 'Bengaluru', current: 'Bengaluru' }] });
  assert.deepEqual(list.map((r) => [r.key, r.checked, r.note]), [
    ['full name', true, 'replaces “Asha R.”'], ['email', true, 'new'], ['city', false, 'already saved']]);
  assert.deepEqual(C.saves(list, new Set(['email'])), [{ key: 'email', value: 'asha@example.com', section: 'profile' }]);
  assert.deepEqual(C.rows(null), []);
});
