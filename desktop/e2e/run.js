// End to end: the real Electron app against the real Lantern server in rehearsal mode.
//   npm run e2e                 (Linux without a screen: xvfb-run -a npm run e2e)
// Starts `python -m lantern --serve --demo` on a free port with its own LANTERN_HOME, launches the
// desktop app pointed at it, and clicks through the demo story in the panel exactly as a person would:
// type a goal, press Allow on approval cards, read the results. Screenshots go to $E2E_OUT.
// Needs: Python with Lantern installed, Electron (npm install), Node Playwright, Chromium for the forms.
const { _electron: electron } = require('playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const DESKTOP = path.resolve(__dirname, '..');
const REPO = path.resolve(DESKTOP, '..');
const OUT = process.env.E2E_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'lantern-e2e-shots-'));
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
    await sleep(150);
  }
  throw new Error(`timed out waiting for ${what} (last: ${last})`);
}

function check(cond, msg) {
  if (!cond) throw new Error(`check failed: ${msg}`);
  console.log(`  ✓ ${msg}`);
}

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lantern-e2e-'));
  const home = path.join(tmp, 'lantern-home');
  const port = await freePort();
  const env = { ...process.env, LANTERN_HOME: home, LANTERN_KEY_STORE: 'file', LANTERN_FORMS_HEADLESS: '1' };
  delete env.MODEL_API_KEY;   // rehearsal: the scripted model, never the real API
  const server = spawn(PYTHON, ['-m', 'lantern', '--serve', '--demo', '--demo-dir', path.join(tmp, 'rehearsal'),
                                '--port', String(port)], { cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let serverLog = '';
  server.stdout.on('data', (d) => { serverLog += d; });
  server.stderr.on('data', (d) => { serverLog += d; });
  const api = `http://127.0.0.1:${port}`;
  let app;
  try {
    await until('the Lantern server', async () => (await fetch(`${api}/health`)).ok, 30000);
    console.log(`server up on ${api} (rehearsal)`);

    const args = [DESKTOP];
    if (process.getuid && process.getuid() === 0) args.push('--no-sandbox');   // containers run as root
    app = await electron.launch({ executablePath: require('electron'), args, env: { ...env, LANTERN_API: api } });
    const pages = await until('both windows', async () => {
      const w = app.windows();
      const panel = w.find((p) => p.url().endsWith('panel.html'));
      const avatar = w.find((p) => p.url().endsWith('avatar.html'));
      return panel && avatar && { panel, avatar };
    });
    const { panel, avatar } = pages;
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().forEach((w) => { if (w.getTitle() === 'Lantern') w.show(); }));
    console.log('desktop app launched (avatar + panel windows)');

    await until('connected header', async () => /rehearsal/i.test(await panel.textContent('#conn')));
    check(true, 'panel connected to the real server and shows rehearsal mode');
    const secure = await panel.evaluate(() => typeof require === 'undefined' && typeof process === 'undefined');
    check(secure, 'renderer has no Node access (sandbox + context isolation)');

    async function ask(goal) {
      await panel.fill('#goal', goal);
      await panel.click('#ask button[type=submit]');
    }
    async function latestTask() {
      return panel.evaluate(() => {
        const t = document.querySelector('#tasks .task');
        return t ? { goal: t.querySelector('.goal').textContent, status: t.querySelector('.status').textContent,
                     reply: (t.querySelector('.reply') || {}).textContent || '',
                     steps: [...t.querySelectorAll('.steps li')].map((li) => li.textContent) } : null;
      });
    }
    async function finish(goal, allows) {
      const before = (await latestTask()) || { status: '' };
      await ask(goal);
      for (let i = 0; i < allows; i++) {
        await panel.waitForSelector('.approval .allow', { timeout: 60000 });
        const card = await panel.textContent('.approval');
        if (/\[object /.test(card)) throw new Error(`approval card shows a rendering bug: ${card.slice(0, 120)}`);
        if (!fs.existsSync(path.join(OUT, '0-approval.png'))) {
          await sleep(400);   // let the sheet finish animating in
          await panel.screenshot({ path: path.join(OUT, '0-approval.png') });
          await avatar.screenshot({ path: path.join(OUT, '0-avatar-approval.png') });
        }
        await panel.click('.approval .allow');
        console.log(`    ⏸ approved: ${card.replace(/\s+/g, ' ').slice(0, 110)}…`);
        await until('the card to close', async () => !(await panel.$('.approval .allow')) || i < allows - 1, 15000);
      }
      return until(`"${goal}" to finish`, async () => {
        const t = await latestTask();
        return t && t.goal.includes(goal.replace(/^\[.*?\]\s*/, '').slice(0, 20)) && /· (done|failed)/.test(t.status)
          && t.status !== before.status && t;
      }, 120000);
    }

    // 1. the form: computer use in real Chromium, Submit approved on a card, verified, then the email
    let t = await finish('Fill the hackathon reimbursement form for my taxi and email it to the organiser.', 2);
    check(/· done/.test(t.status) && /verified ✓/.test(t.status), `form task done and verified (${t.status})`);
    check(t.steps.some((s) => /Muse Wonder/.test(s)), 'browser steps carry the Muse Wonder badge');
    check(t.steps.some((s) => /Email sent/.test(s)), 'email step shown');
    await panel.screenshot({ path: path.join(OUT, '1-form-verified.png') });
    const bubble = await until('the avatar bubble', async () => (await avatar.textContent('#bubble')).trim());
    check(bubble.length > 0, `avatar reacts: “${bubble.slice(0, 60)}”`);
    await avatar.screenshot({ path: path.join(OUT, '1-avatar.png') });

    // 2. private file on the local lane, 3. the planted key
    t = await finish('What base salary does the offer letter in my Private folder say?', 0);
    check(/Base salary/.test(t.reply) && t.steps.some((s) => /Glimmer/.test(s)), 'private question answered on the local lane');
    t = await finish('Anything I forgot in my project notes?', 0);
    check(/redacted/.test(t.reply), 'planted key reported as redacted');
    await panel.click('.tabs button[data-tab=privacy]');
    const redactions = await until('audit redactions', async () => panel.textContent('#audit-redactions'));
    check(redactions.trim() === 'Redacted before sending: aws access key ×1',
      `privacy tab shows exactly the planted key, no false alarms (${redactions.trim()})`);
    check(/would have left this laptop/.test(await panel.textContent('#audit-headline')), 'privacy headline is honest about rehearsal');
    check(!/Library\/Application Support/.test(await panel.textContent('#zones')), "other systems' hidden defaults are folded away");
    await panel.screenshot({ path: path.join(OUT, '2-privacy.png') });
    await panel.click('.tabs button[data-tab=tasks]');

    // 4. projects, 5. the widget
    t = await finish('Which projects use torch?', 0);
    check(/vision-lab/.test(t.reply), 'projects answered from the local map');
    t = await finish('Make me a water timer.', 1);
    check(/· done/.test(t.status), 'widget saved after approval');
    await panel.click('.tabs button[data-tab=widgets]');
    await panel.click('#widget-list button >> text=/water/i');
    const frame = await until('the widget frame', async () => panel.$('#widget-host'));
    check(frame && (await frame.getAttribute('sandbox')) === 'allow-scripts', 'widget runs in a sandboxed frame');
    await sleep(800);
    await panel.screenshot({ path: path.join(OUT, '3-widget.png') });
    await panel.click('.tabs button[data-tab=tasks]');

    // extras: a reminder and tidying Downloads
    t = await finish('Remind me in 20 minutes to record the demo video.', 0);
    const reminders = await until('the reminders card', async () => panel.textContent('#reminders'));
    check(/record the demo video/.test(reminders), 'reminder listed with a Cancel button');
    t = await finish('Tidy my Downloads folder.', 1);
    check(/Moved 6 files/.test(t.reply), 'Downloads tidied with one approval');
    await panel.screenshot({ path: path.join(OUT, '4-tasks.png') });

    // the phone, the setup check, the costs
    await panel.click('.tabs button[data-tab=phone]');
    const phone = await until('phone messages', async () => panel.textContent('#phone-msgs'));
    check(/Lantern needs your OK/.test(phone), 'approval cards also went to the simulated phone');
    check(/→ home\/Downloads\/Images/.test(phone) && !/→ \//.test(phone), 'tidy card shows short paths, not /tmp/...');
    await panel.screenshot({ path: path.join(OUT, '5-phone.png') });
    await panel.click('.tabs button[data-tab=setup]');
    const checks = await until('the setup checks', async () => (await panel.$$('#doctor .check')).length);
    check(checks >= 10, `setup tab lists ${checks} checks`);
    await panel.screenshot({ path: path.join(OUT, '6-setup.png') });
    await panel.click('.tabs button[data-tab=costs]');
    await sleep(500);
    check(/\$0\.00/.test(await panel.textContent('#today-cost')), 'rehearsal cost stays $0.00');

    // dark appearance (macOS/Windows pass the system theme through; headless Linux can't, so emulate it)
    await panel.emulateMedia({ colorScheme: 'dark' });
    await panel.click('.tabs button[data-tab=tasks]');
    await sleep(500);
    const dark = await panel.evaluate(() => getComputedStyle(document.body).backgroundColor);
    check(dark === 'rgb(0, 0, 0)', `dark appearance applies (${dark})`);
    await panel.screenshot({ path: path.join(OUT, '7-dark.png') });
    await panel.emulateMedia({ colorScheme: 'light' });

    check(!/\[object |undefined|NaN/.test(await panel.textContent('main')), 'no rendering glitches ([object …], undefined, NaN) anywhere in the panel');

    // STOP from the panel
    await panel.click('#stop');
    await until('the stop event', async () => /Stopped|stopped/.test(await avatar.textContent('#bubble')), 10000);
    check(true, 'STOP reaches the server and the avatar');
    console.log(`\nall end-to-end checks passed · screenshots in ${OUT}`);
  } catch (e) {
    console.error(`\n✗ ${e.message}\n--- server log ---\n${serverLog.slice(-3000)}`);
    process.exitCode = 1;
  } finally {
    if (app) await app.close().catch(() => {});
    server.kill();
  }
}

main();
