// A screenshot of every section of the real app, light and dark, against the real server in rehearsal mode.
//   xvfb-run -a node e2e/tour.js            (screenshots in $TOUR_OUT, default a temp folder)
// For design review: it clicks nothing but the tabs, after one rehearsal task so the lists aren't empty.
const { _electron: electron } = require('playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const DESKTOP = path.resolve(__dirname, '..');
const REPO = path.resolve(DESKTOP, '..');
const OUT = process.env.TOUR_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'lantern-tour-'));
const PYTHON = process.env.PYTHON || 'python3';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lantern-tour-'));
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
    let panel, avatar;
    for (let i = 0; i < 200 && !(panel && avatar); i++) {
      panel = app.windows().find((p) => p.url().endsWith('panel.html'));
      avatar = app.windows().find((p) => p.url().endsWith('avatar.html'));
      await sleep(150);
    }
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().forEach((w) => { if (w.getTitle() === 'Lantern') w.show(); }));
    await sleep(1500);
    await panel.screenshot({ path: path.join(OUT, 'welcome.png') });
    if (await panel.isVisible('#welcome-go')) await panel.click('#welcome-go');
    const extra = (process.env.TOUR_GOAL || 'Tidy my Downloads folder.');
    if (extra) {
      await panel.fill('#goal', extra).catch(() => {});
      await panel.press('#goal', 'Enter').catch(() => {});
      await sleep(4000);
      await panel.screenshot({ path: path.join(OUT, 'working.png') });
      await avatar.screenshot({ path: path.join(OUT, 'avatar.png') });
      for (let i = 0; i < 40; i++) {   // approve what the rehearsal asks, so the task finishes
        const allow = await panel.$('[data-act="allow"], button.allow');
        if (allow) { await allow.click().catch(() => {}); }
        await sleep(500);
      }
    }
    const tabs = await panel.$$eval('nav.tabs [role=tab]', (bs) => bs.map((b) => b.dataset.tab));
    for (const scheme of ['light', 'dark']) {
      await panel.emulateMedia({ colorScheme: scheme });
      for (const t of tabs) {
        await panel.click(`nav.tabs [data-tab="${t}"]`);
        await sleep(700);
        await panel.screenshot({ path: path.join(OUT, `${scheme}-${t}.png`) });
        const h = await panel.evaluate(() => document.querySelector('main, .content, #content')?.scrollHeight || document.body.scrollHeight);
        await panel.screenshot({ path: path.join(OUT, `${scheme}-${t}-full.png`), fullPage: true }).catch(() => {});
        if (h) {}
      }
    }
    console.log(`tour screenshots in ${OUT}`);
  } finally {
    if (app) await app.close().catch(() => {});
    server.kill();
  }
}
main().catch((e) => { console.error(e); process.exit(1); });
