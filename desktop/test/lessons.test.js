// node --test: Teaching Mode (S16) in the panel: the lesson form, the saved lessons' rows, Check my work.
const test = require('node:test');
const assert = require('node:assert/strict');

const L = require('../renderer/lib/lessons.js');

test('the lesson form needs a request of a sentence or two', () => {
  assert.equal(L.check('solve x^2 - 5x + 6 = 0'), '');
  assert.match(L.check('   '), /what you want to learn/);
  assert.match(L.check('x'.repeat(501)), /sentence or two/);
});

test('saved lessons say how many steps SymPy checked, and when there is no ✓', () => {
  assert.deepEqual(L.rows([{ name: 'solve-x2', title: 'Solve x²', subject: 'algebra', verified: 4 },
                           { name: 'one', title: 'One step', subject: 'calculus', verified: 1 },
                           { name: 'triangles', title: 'Triangles', subject: 'geometry', verified: 0 }]), [
    { name: 'solve-x2', title: 'Solve x²', line: '4 steps checked by SymPy ✓' },
    { name: 'one', title: 'One step', line: '1 step checked by SymPy ✓' },
    { name: 'triangles', title: 'Triangles', line: 'No ✓: not algebra or calculus' }]);
  assert.deepEqual(L.rows(null), []);
});

test('check my work takes one photo and shows the reply with the circled copy', () => {
  assert.match(L.photoCheck(null), /Choose a photo/);
  assert.match(L.photoCheck({ type: 'application/pdf', size: 10 }), /JPEG, PNG or WebP/);
  assert.match(L.photoCheck({ type: 'image/png', size: 16 * 1024 * 1024 }), /15 MB/);
  assert.equal(L.photoCheck({ type: 'image/jpeg', size: 2000 }), '');
  assert.deepEqual(L.result({ type: 'checkwork', wrong: 3, reply: 'Line 3 doesn\'t follow.',
                              image: '/home/a/.lantern/media/checked-20261028-101500-123.png' }),
                   { text: 'Line 3 doesn\'t follow.', image: 'checked-20261028-101500-123.png', tone: 'wrong' });
  assert.equal(L.result({ type: 'checkwork', unreadable: 2, reply: 'x', image: '../../etc/passwd' }).image, '');
  assert.equal(L.result({ type: 'checkwork', reply: 'All 4 lines check out' }).tone, 'ok');
  assert.equal(L.result({ type: 'done' }), null);
});
