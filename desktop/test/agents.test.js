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

test('schedule words mirror the server: every day at 09:00, every 2 hours', () => {
  assert.equal(G.scheduleText({ every: 'day', at: '09:00' }), 'every day at 09:00');
  assert.equal(G.scheduleText({ every: 'weekday', at: '17:00' }), 'every weekday at 17:00');
  assert.equal(G.scheduleText({ every: 'week', at: '17:00' }), 'every week at 17:00');
  assert.equal(G.scheduleText({ every: 'hours', hours: 2 }), 'every 2 hours');
  assert.equal(G.scheduleText({ every: 'hours', hours: 1 }), 'every 1 hour');
  assert.equal(G.scheduleText({ every: 'minutely' }), '');
  assert.equal(G.scheduleText({ every: 'day', at: '9am' }), '');
  assert.equal(G.scheduleText(null), '');
});

test('next-run dates: today, tomorrow, then a weekday date', () => {
  const now = new Date(2026, 10, 2, 8, 0).getTime();   // Mon 2 Nov 2026, local
  const at = (d, h, m) => new Date(2026, 10, d, h, m).getTime() / 1000;
  assert.equal(G.formatDue(at(2, 9, 0), now), 'today 09:00');
  assert.equal(G.formatDue(at(3, 9, 0), now), 'tomorrow 09:00');
  assert.equal(G.formatDue(at(9, 9, 0), now), 'Mon 9 Nov 09:00');
  assert.equal(G.formatDue(null, now), '');
});

test('loop view: schedule line, next and last run, off state', () => {
  const now = new Date(2026, 10, 2, 8, 0).getTime();
  assert.deepEqual(G.loopView({}, now), { schedulable: false, enabled: false });
  const sched = { every: 'day', at: '09:00' };
  const off = G.loopView({ schedule: sched, loop: { enabled: false } }, now);
  assert.equal(off.schedulable, true);
  assert.equal(off.schedule, 'every day at 09:00');
  assert.equal(off.next, 'Off');
  assert.equal(off.last, 'Never run yet');
  const next = new Date(2026, 10, 2, 9, 0).getTime() / 1000;
  const on = G.loopView({ schedule: sched, loop: { enabled: true, next_run: next, last_task: 7, last_status: 'done' } }, now);
  assert.equal(on.enabled, true);
  assert.equal(on.next, 'Next run today 09:00');
  assert.equal(on.last, 'Last run #7 · ok');
  assert.equal(G.loopView({ schedule: sched, loop: { last_task: 8, last_status: 'failed' } }, now).last, 'Last run #8 · failed');
  assert.equal(G.loopView({ schedule: sched, loop: { last_task: 9, last_status: 'cancelled' } }, now).last, 'Last run #9 · stopped');
});

test('surfaces: laptop by default, Runs-on labels for the switch', () => {
  assert.deepEqual(G.surfaceView({}), {
    surface: 'laptop', label: 'Runs on: Laptop', options: ['laptop', 'phone', 'both'],
  });
  assert.equal(G.surfaceView({ surface: 'phone' }).label, 'Runs on: Phone');
  assert.equal(G.surfaceView({ surface: 'both' }).label, 'Runs on: Both');
  assert.equal(G.surfaceView({ surface: 'watch' }).surface, 'laptop');
  assert.deepEqual(G.SURFACES, ['laptop', 'phone', 'both']);
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

test('skill import: preview shapes the form, tools apply, A2A file names', () => {
  const preview = {
    pack: { name: 'my-skill', title: 'My Skill', description: 'Does things.',
      tools: ['list_folders', 'read_file'] },
    skipped: ['helper.py'], truncated: true, source: 'imported skill',
    available_tools: ['list_folders', 'read_file', 'write_file'],
  };
  const view = G.importView(preview);
  assert.equal(view.name, 'my-skill');
  assert.equal(view.title, 'My Skill');
  assert.deepEqual(view.tools, ['list_folders', 'read_file']);
  assert.deepEqual(view.available, ['list_folders', 'read_file', 'write_file']);
  assert.deepEqual(view.skipped, ['helper.py']);
  assert.equal(view.truncated, true);
  assert.equal(view.source, 'imported skill');
  assert.equal(G.importView(null).title, 'Untitled agent');
  assert.deepEqual(G.importView({ pack: null }).tools, []);
  const withTools = G.packWithTools(preview.pack, ['list_folders', 'write_file']);
  assert.deepEqual(withTools.tools, ['list_folders', 'write_file']);
  assert.equal(withTools.name, 'my-skill');   // everything else rides along
  assert.deepEqual(G.packWithTools(preview.pack, 'nope').tools, []);
  assert.equal(G.a2aFileName({ name: 'tidy-downloads' }), 'tidy-downloads.agent-card.json');
  assert.equal(G.a2aFileName({}), 'agent.agent-card.json');
});
