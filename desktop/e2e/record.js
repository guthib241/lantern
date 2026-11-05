// Drives the real app through the demo story at a human pace, for a screen recording (README, video).
//   RECORD_W=1440 RECORD_H=900 xvfb-run -a -s "-screen 0 1920x1080x24" node e2e/record.js
// The panel window is placed at the top-left corner of the screen, so a screen capture of that area (for example
// ffmpeg -f x11grab) films only Lantern. Each scene prints "MARK <name> <ms since start>" for cutting.
// Rehearsal mode: the real server, real Chromium, real approvals; a scripted stand-in plays the Meta models, so it
// costs nothing and needs no key. Approvals stay on screen for a moment, then they're allowed.
const { _electron: electron } = require('playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const DESKTOP = path.resolve(__dirname, '..');
const REPO = path.resolve(DESKTOP, '..');
const PYTHON = process.env.PYTHON || 'python3';
const W = Number(process.env.RECORD_W || 1440);
const H = Number(process.env.RECORD_H || 900);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();
const mark = (name) => console.log(`MARK ${name} ${Date.now() - t0}`);

async function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

async function until(check, ms = 60000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await check().catch(() => false)) return true;
    await sleep(200);
  }
  return false;
}

async function ask(panel, text) {
  await panel.click('#goal');
  await panel.type('#goal', text, { delay: 32 });   // typed, so it reads like someone using it
  await sleep(600);
  await panel.press('#goal', 'Enter');
}

async function tasksText(panel) {
  return (await panel.textContent('#tasks').catch(() => '')) || '';
}

// The sheet re-renders every second (its countdown), so the button is found again at the moment of the click.
async function approveAll(panel, settle = 3800, quietFor = 10) {
  let allowed = 0;
  for (let quiet = 0; quiet < quietFor;) {
    const allow = panel.locator('#approvals button.allow').first();
    if (await allow.count() && await allow.isVisible().catch(() => false)) {
      mark('approval');
      await sleep(settle);
      await allow.click({ timeout: 5000 }).catch(() => {});
      allowed += 1;
      quiet = 0;
      await sleep(900);
    } else {
      quiet += 1;
      await sleep(500);
    }
  }
  return allowed;
}

async function tab(panel, name, hold) {
  await panel.click(`nav.tabs [data-tab="${name}"]`);
  mark(`tab-${name}`);
  await sleep(hold);
}

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lantern-record-'));
  const port = await freePort();
  const env = { ...process.env, LANTERN_HOME: path.join(tmp, 'home'), LANTERN_KEY_STORE: 'file', LANTERN_FORMS_HEADLESS: '1' };
  delete env.MODEL_API_KEY;
  const server = spawn(PYTHON, ['-m', 'lantern', '--serve', '--demo', '--demo-dir', path.join(tmp, 'rehearsal'),
                                '--port', String(port)], { cwd: REPO, env, stdio: 'ignore' });
  const api = `http://127.0.0.1:${port}`;
  let app;
  try {
    for (let i = 0; i < 200; i++) { try { if ((await fetch(`${api}/health`)).ok) break; } catch {} await sleep(150); }
    const args = [DESKTOP];
    if (process.getuid && process.getuid() === 0) args.push('--no-sandbox');
    app = await electron.launch({ executablePath: require('electron'), args,
                                  env: { ...env, LANTERN_API: api, LANTERN_USER_DATA: path.join(tmp, 'electron') } });
    let panel;
    for (let i = 0; i < 200 && !panel; i++) {
      panel = app.windows().find((p) => p.url().endsWith('panel.html'));
      await sleep(150);
    }
    await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows().forEach((w) => {
      if (w.getTitle() === 'Lantern' && w.webContents.getURL().endsWith('panel.html')) {
        w.setBounds({ x: 0, y: 0, width: size.w, height: size.h });
        w.show();
      }
    }), { w: W, h: H });
    await panel.emulateMedia({ colorScheme: 'light' });
    await sleep(2500);
    mark('welcome');
    if (await panel.isVisible('#welcome-go').catch(() => false)) {
      await sleep(3500);
      await panel.click('#welcome-go');
    }
    await sleep(2500);
    mark('today');

    // 1. The boring computer work: a form, then the email, each with its own approval.
    mark('ask-form');
    await ask(panel, 'Fill the hackathon reimbursement form for my taxi and email it to the organiser.');
    await until(async () => await panel.locator('#approvals button.allow').count() > 0, 120000);
    await approveAll(panel);
    await until(async () => /verified/.test(await tasksText(panel)), 120000);
    mark('form-done');
    await sleep(4000);

    // 2. A private file: answered on the private lane, never by a cloud API.
    mark('ask-private');
    await ask(panel, 'What base salary does the offer letter in my Private folder say?');
    await until(async () => (await panel.$$('#tasks article')).length >= 2 && !/running/.test(await tasksText(panel)), 90000);
    mark('private-done');
    await sleep(4500);

    // 3. A question about this computer that needs no model at all, then a widget.
    mark('ask-torch');
    await ask(panel, 'Which projects use torch?');
    await until(async () => (await panel.$$('#tasks article')).length >= 3 && !/running/.test(await tasksText(panel)), 90000);
    mark('torch-done');
    await sleep(3500);
    mark('ask-timer');
    await ask(panel, 'Make me a water timer.');
    await until(async () => await panel.locator('#approvals button.allow').count() > 0 ||
      ((await panel.$$('#tasks article')).length >= 4 && !/running/.test(await tasksText(panel))), 90000);
    await approveAll(panel, 2500, 6);
    await until(async () => (await panel.$$('#tasks article')).length >= 4 && !/running/.test(await tasksText(panel)), 90000);
    mark('timer-done');
    await sleep(2500);
    await tab(panel, 'widgets', 4500);

    // 4. What left the laptop, the learned routines, the agents, and Lantern's look.
    await tab(panel, 'privacy', 5000);
    await tab(panel, 'agents', 2500);
    // Two of the sample packs in agents/, installed the way any shared pack is: each shows its own install card.
    const token = fs.readFileSync(path.join(env.LANTERN_HOME, 'token'), 'utf8').trim();
    for (const name of ['tidy-downloads', 'github-helper']) {
      const body = fs.readFileSync(path.join(REPO, 'agents', `${name}.lantern-agent.json`), 'utf8');
      const install = fetch(`${api}/agents/install`, { method: 'POST', body,
        headers: { 'Content-Type': 'application/json', 'X-Lantern-Token': token } }).catch(() => {});
      await until(async () => await panel.locator('#approvals button.allow').count() > 0, 30000);
      await approveAll(panel, 3000, 4);
      await install;
      mark(`installed-${name}`);
    }
    await tab(panel, 'agents', 4500);
    await tab(panel, 'setup', 1500);
    const look = panel.locator('#avatar-skins button', { hasText: 'Orb' });
    await look.scrollIntoViewIfNeeded();
    await sleep(1500);
    mark('look-orb');
    await look.click();
    await sleep(4000);
    await tab(panel, 'tasks', 3000);
    await panel.emulateMedia({ colorScheme: 'dark' });
    mark('dark');
    await sleep(4000);
    mark('end');
  } finally {
    if (app) await app.close().catch(() => {});
    server.kill();
  }
}
main().catch((e) => { console.error(e); process.exit(1); });
