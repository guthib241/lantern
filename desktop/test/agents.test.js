// node --test: agent packs in the desktop panel (cards, run form, drafts). Logic only.
const test = require('node:test');
const assert = require('node:assert/strict');

const G = require('../renderer/lib/agents.js');

const PACK = {
  name: 'tidy-downloads', title: 'Tidy Downloads', description: 'Sorts a messy folder.',
  author: 'lantern', version: '1.0.0', disabled: false,
  inputs: [{ key: 'folder', question: 'Which folder to tidy?', example: 'Downloads' }],
  goal: 'Tidy {folder}.', instructions: 'List, then organize.',
  tools: ['list_dir', 'search_files', 'organize_files'],
};

test('card view: title, author line, tool list, disabled state', () => {
  const c = G.cardView(PACK);
  assert.equal(c.title, 'Tidy Downloads');
  assert.equal(c.sub, 'lantern · v1.0.0');
  assert.deepEqual(c.tools, ['list_dir', 'search_files', 'organize_files']);
  assert.equal(c.runnable, true);
  assert.equal(G.cardView({ ...PACK, disabled: true }).runnable, false);
  assert.equal(G.cardView({}).title, 'Untitled agent');
});

test('run payload: every input needs an answer, at most 500 characters', () => {
  assert.deepEqual(G.runPayload(PACK, { folder: 'Downloads' }), { ok: true, inputs: { folder: 'Downloads' } });
  assert.deepEqual(G.runPayload(PACK, { folder: '  ' }).ok, false);
  assert.match(G.runPayload(PACK, {}).error, /Which folder/);
  assert.equal(G.runPayload(PACK, { folder: 'x'.repeat(501) }).ok, false);
  assert.deepEqual(G.runPayload({ inputs: [] }, {}), { ok: true, inputs: {} });
});

test('pack files: JSON parses, junk is refused, names end in .lantern-agent.json', () => {
  assert.equal(G.parsePackFile('{"name":"x"}').ok, true);
  assert.equal(G.parsePackFile('nope').ok, false);
  assert.equal(G.parsePackFile('[1]').ok, false);
  assert.equal(G.packFileName(PACK), 'tidy-downloads.lantern-agent.json');
  assert.equal(G.packFileName({}), 'agent.lantern-agent.json');
  assert.ok(G.packFileText(PACK).endsWith('\n'));
});

test('signature badges: signed names who and the key, unsigned warns, tampered refuses', () => {
  assert.deepEqual(
    G.signatureBadge({ signature_status: 'signed', author: 'achan', fingerprint: '3F2A-91C0' }),
    { kind: 'signed', text: '✓ Signed by achan · key 3F2A-91C0' });
  assert.equal(G.signatureBadge({ signature_status: 'signed', author: 'achan' }).kind, 'signed');
  assert.deepEqual(G.signatureBadge({ signature_status: 'unsigned' }),
    { kind: 'unsigned', text: 'Unsigned: anyone could have changed it' });
  assert.equal(G.signatureBadge({}).kind, 'unsigned');
  assert.equal(G.signatureBadge(null).kind, 'unsigned');
  assert.equal(G.signatureBadge({ signature_status: 'tampered' }).kind, 'tampered');
});

test('faces: drawn icons and emoji from the server, short emoji from the form', () => {
  assert.deepEqual(G.faceView({ face: { icon: true } }), { kind: 'icon' });
  assert.deepEqual(G.faceView({ face: { emoji: '📦' } }), { kind: 'emoji', emoji: '📦' });
  for (const pack of [{}, { face: null }, { face: {} }, { face: { emoji: '' } }]) {
    assert.deepEqual(G.faceView(pack), { kind: 'none' });
  }
  assert.deepEqual(G.parseEmoji(' 📦 '), { ok: true, emoji: '📦' });
  assert.equal(G.parseEmoji('').ok, false);
  assert.equal(G.parseEmoji(null).ok, false);
  assert.equal(G.parseEmoji('x'.repeat(9)).ok, false);
});

test('draft round-trips through editable fields', () => {
  const fields = G.draftFields({ ...PACK, extra: 1 });
  assert.equal(fields.tools, 'list_dir\nsearch_files\norganize_files');
  assert.equal(fields.inputs[0].example, 'Downloads');
  const back = G.packFromFields({ ...fields, tools: 'list_dir, search_files\norganize_files' });
  assert.equal(back.format, 'lantern-agent/1');
  assert.equal(back.name, 'tidy-downloads');
  assert.deepEqual(back.tools, ['list_dir', 'search_files', 'organize_files']);
  assert.deepEqual(G.packFromFields({ tools: '' }).tools, []);
  assert.deepEqual(G.packFromFields({ inputs: [{ key: '', question: '' }] }).inputs, []);
});
