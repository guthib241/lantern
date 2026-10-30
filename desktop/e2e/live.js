// Live check: the real Electron app against a real Lantern server on the LIVE Meta API.
//   MODEL_API_KEY=... xvfb-run -a node e2e/live.js
// Unlike e2e/run.js (rehearsal, scripted model, free), this spends real credit: a few cents on Muse Spark.
// It copies demo/home into a temp folder, starts `python -m lantern --serve` on it, then types two goals in
// the panel (one read-only, one that needs an approval), presses Allow, and checks the replies and badges.
const { _electron: electron } = require('playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const DESKTOP = path.resolve(__dirname, '..');
const REPO = path.resolve(DESKTOP, '..');
const OUT = process.env.E2E_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'lantern-live-shots-'));
const PYTHON = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

async function until(what, fn, timeout = 60000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try { last = await fn(); if (last) return last; } catch (e) { last = e; }
    await sleep(250);
  }
  throw new Error(`timed out waiting for ${what} (last: ${last})`);
}

function check(cond, msg) {
  if (!cond) throw new Error(`check failed: ${msg}`);
  console.log(`  ✓ ${msg}`);
}

async function main() {
  if (!process.env.MODEL_API_KEY) throw new Error('set MODEL_API_KEY: this check uses the live API');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lantern-live-'));
  const home = path.join(tmp, 'lantern-home');
  const roots = path.join(tmp, 'home');
  fs.cpSync(path.join(REPO, 'demo', 'home'), roots, { recursive: true });
  const port = await freePort();
  const env = { ...process.env, LANTERN_HOME: home, LANTERN_ROOTS: roots, LANTERN_KEY_STORE: 'file',
                LANTERN_CHANNELS: 'none', LANTERN_FORMS_HEADLESS: '1' };
  const server = spawn(PYTHON, ['-m', 'lantern', '--serve', '--port', String(port)],
                       { cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let serverLog = '';
  server.stdout.on('data', (d) => { serverLog += d; });
  server.stderr.on('data', (d) => { serverLog += d; });
  const api = `http://127.0.0.1:${port}`;
  let app;
  try {
    await until('the Lantern server', async () => (await fetch(`${api}/health`)).ok, 30000);
    const health = await (await fetch(`${api}/health`)).json();
    check(health.has_key === true, `server is live (model ${health.model})`);
    const args = [DESKTOP];
    if (process.getuid && process.getuid() === 0) args.push('--no-sandbox');
    app = await electron.launch({ executablePath: require('electron'), args,
                                  env: { ...env, LANTERN_API: api, LANTERN_USER_DATA: path.join(tmp, 'electron-data') } });
    const panel = await until('the panel window', async () => app.windows().find((p) => p.url().endsWith('panel.html')));
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().forEach((w) => { if (w.getTitle() === 'Lantern') w.show(); }));
    await panel.waitForSelector('#welcome:not([hidden])', { timeout: 15000 }).then(() => panel.click('#welcome-go')).catch(() => {});
    await until('connected header', async () => /connected|live|ready/i.test(await panel.textContent('#conn')), 20000);
    console.log(`panel connected: “${(await panel.textContent('#conn')).trim()}”`);

    const latest = () => panel.evaluate(() => {
      const t = [...document.querySelectorAll('#tasks .task')].pop();
      return t ? { goal: t.querySelector('.goal').textContent, status: t.querySelector('.status').textContent,
                   reply: (t.querySelector('.reply') || {}).textContent || '',
                   steps: [...t.querySelectorAll('.steps li')].map((li) => li.textContent) } : null;
    });
    async function run(goal, allow) {
      const t0 = Date.now();
      await panel.fill('#goal', goal);
      await panel.click('#ask button[type=submit]');
      await until('the task card', async () => { const t = await latest(); return t && t.goal.includes(goal.slice(0, 20)); }, 20000);
      let allowed = 0;
      const t = await until(`“${goal}” to finish`, async () => {
        if (allow && await panel.$('.approval .allow')) {
          await panel.click('.approval .allow');
          allowed += 1;
          console.log('    ⏸ pressed Allow');
        }
        const cur = await latest();
        return cur && /done|failed|cancelled/i.test(cur.status) && cur;
      }, 300000);
      console.log(`  ${t.status} in ${((Date.now() - t0) / 1000).toFixed(0)}s · ${allowed} approval(s)\n    🏮 ${t.reply.trim().slice(0, 300)}`);
      return { ...t, allowed };
    }

    const a = await run('Which projects use torch?', false);
    check(/done/i.test(a.status) && /vision|voice|torch/i.test(a.reply), 'read-only question answered from the project map');
    check(a.steps.some((s) => /Muse Spark/.test(s)) || (await panel.textContent('#tasks')).includes('Muse Spark'),
          'the step list shows the Muse Spark badge');
    await panel.screenshot({ path: path.join(OUT, 'live-1-torch.png') });

    const b = await run('Tidy my Downloads folder.', true);
    check(/done/i.test(b.status) && b.allowed >= 1, 'tidy ran after pressing Allow on the approval card');
    const after = fs.readdirSync(path.join(roots, 'Downloads'));
    check(after.every((n) => fs.statSync(path.join(roots, 'Downloads', n)).isDirectory()), `Downloads now holds only folders: ${after.join(', ')}`);
    await panel.screenshot({ path: path.join(OUT, 'live-2-tidy.png') });

    const usage = await (await fetch(`${api}/usage`, { headers: { 'X-Lantern-Token': fs.readFileSync(path.join(home, 'token'), 'utf8').trim() } })).json();
    console.log(`\nlive desktop check passed · $${(usage.total_usd || 0).toFixed(4)} · screenshots in ${OUT}`);
  } catch (e) {
    console.error(String(e && e.stack || e));
    console.error('--- server log ---\n' + serverLog.slice(-3000));
    process.exitCode = 1;
  } finally {
    if (app) await app.close().catch(() => {});
    server.kill();
  }
}

main();
