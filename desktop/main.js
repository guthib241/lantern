// Lantern desktop (Electron main process).
// Windows: a transparent, always-on-top avatar that lets clicks pass through except over
// the sprite, a panel (chat, approvals, privacy, phone, costs, widgets), Quick Ask, and the teaching board. Renderers are
// sandboxed with context isolation; they talk to the local Lantern server on 127.0.0.1 with
// the per-install token from ~/.lantern/token. Ctrl/Cmd+Shift+X is the global STOP.
const { app, BrowserWindow, Menu, Notification, Tray, globalShortcut, ipcMain, nativeImage, nativeTheme, screen, session } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const MOCK = process.argv.includes('--mock') || process.env.LANTERN_MOCK === '1';
// Separate app data (window state, the welcome flag, widget storage) for tests and rehearsals.
if (process.env.LANTERN_USER_DATA) app.setPath('userData', process.env.LANTERN_USER_DATA);
const STOP_HOTKEY = 'CommandOrControl+Shift+X';
const ASK_HOTKEY = 'CommandOrControl+Shift+Space';
// Native glass behind Quick Ask: macOS vibrancy, Windows 11 acrylic. Elsewhere the page draws its own surface.
const NATIVE_MATERIAL = process.platform === 'darwin' || process.platform === 'win32';
let config = { apiBase: process.env.LANTERN_API || 'http://127.0.0.1:8765', token: '', mock: MOCK,
               platform: process.platform, nativeMaterial: NATIVE_MATERIAL, askHotkey: ASK_HOTKEY };
let avatar = null;
let panel = null;
let quick = null;
let board = null;   // the teaching board (S16), opened from the panel with a lesson
let tray = null;
// What the menu bar icon, badge and notifications last showed (also read by the end-to-end test).
const nativeStatus = { line: 'Starting…', badge: 0, notified: [] };
global.lanternNative = nativeStatus;
let quitting = false;
let cameraUntil = 0;   // the camera is allowed for one check-in right after the user clicks
let serverProc = null; // the Lantern server this app started (and stops on quit), if none was running

async function healthy() {
  try {
    return (await fetch(`${config.apiBase}/health`, { signal: AbortSignal.timeout(1000) })).ok;
  } catch {
    return false;
  }
}

// `npm start` is the only command: with no Lantern server answering, start one from this checkout on the
// configured local port (`npm run rehearsal` starts it in --demo mode). LANTERN_NO_AUTOSTART=1 opts out.
async function ensureServer() {
  if (MOCK || process.env.LANTERN_NO_AUTOSTART === '1') return 'off';
  if (await healthy()) return 'running';
  const url = new URL(config.apiBase);
  if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return 'remote';
  const python = process.env.LANTERN_PYTHON || (process.platform === 'win32' ? 'py' : 'python3');
  const args = ['-m', 'lantern', '--serve', '--port', url.port || '8765'];
  if (process.argv.includes('--rehearsal')) args.push('--demo');
  const log = fs.openSync(path.join(app.getPath('userData'), 'server.log'), 'w');   // why it failed, if it does
  serverProc = spawn(python, args, { cwd: path.resolve(__dirname, '..'), env: process.env, stdio: ['ignore', log, log], windowsHide: true });
  fs.closeSync(log);
  serverProc.on('error', () => { serverProc = null; });   // no Python: the panel shows how to start it by hand
  serverProc.on('exit', () => { serverProc = null; });
  for (let i = 0; i < 60 && serverProc; i++) {
    if (await healthy()) return 'started';
    await new Promise((r) => setTimeout(r, 500));
  }
  return 'failed';
}

function readToken() {
  const home = process.env.LANTERN_HOME || path.join(os.homedir(), '.lantern');
  try {
    return fs.readFileSync(path.join(home, 'token'), 'utf8').trim();
  } catch {
    return '';
  }
}

const secure = (preload) => ({
  preload: path.join(__dirname, preload),
  contextIsolation: true,
  nodeIntegration: false,
  sandbox: true,
  webSecurity: true,
});

function createAvatar() {
  const { workArea } = screen.getPrimaryDisplay();
  const w = 340;   // room for the live activity island to expand above the face
  const h = 430;
  avatar = new BrowserWindow({
    width: w, height: h, x: workArea.x + workArea.width - w - 16, y: workArea.y + workArea.height - h - 8,
    transparent: true, frame: false, alwaysOnTop: true, skipTaskbar: true, resizable: false,
    hasShadow: false, backgroundColor: '#00000000', webPreferences: secure('preload.js'),
  });
  avatar.setAlwaysOnTop(true, 'floating');
  avatar.setIgnoreMouseEvents(true, { forward: true }); // clicks pass through until the pointer is on the sprite
  avatar.loadFile(path.join(__dirname, 'renderer', 'avatar.html'));
}

function createPanel() {
  // A Mac-style window: a sidebar of sections and a content area. On macOS the traffic lights sit inset over the
  // sidebar, which gets the system's own sidebar material; elsewhere the window keeps its normal frame.
  const mac = process.platform === 'darwin'
    ? { titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 16, y: 18 }, vibrancy: 'sidebar', visualEffectState: 'followWindow',
        backgroundColor: '#00000000' }
    : { backgroundColor: nativeTheme.shouldUseDarkColors ? '#232325' : '#ececf0' };
  panel = new BrowserWindow({
    width: 1080, height: 720, minWidth: 780, minHeight: 540, show: false, title: 'Lantern',
    autoHideMenuBar: true, ...mac, webPreferences: secure('preload.js'),
  });
  panel.loadFile(path.join(__dirname, 'renderer', 'panel.html'));
  panel.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      panel.hide();
    }
  });
}

function createQuick() {
  const material = process.platform === 'darwin' ? { vibrancy: 'hud', visualEffectState: 'active', transparent: true }
    : process.platform === 'win32' ? { backgroundMaterial: 'acrylic' } : { transparent: true };
  quick = new BrowserWindow({
    width: 640, height: 400, show: false, frame: false, resizable: false, movable: false, minimizable: false,
    maximizable: false, fullscreenable: false, alwaysOnTop: true, skipTaskbar: true, title: 'Lantern Quick Ask',
    backgroundColor: '#00000000', ...material, webPreferences: secure('preload.js'),
  });
  quick.setAlwaysOnTop(true, 'pop-up-menu');
  quick.loadFile(path.join(__dirname, 'renderer', 'quick.html'));
  quick.on('blur', () => quick.hide());   // like Spotlight: clicking elsewhere closes it
}

// The teaching board (S16): a full window that plays a lesson the panel fetched from the server. The board page
// checks the lesson itself (lib/timeline.js) and shows a ✓ only on lines Lantern verified with SymPy.
function openBoard(lesson) {
  const send = () => board.webContents.send('board:lesson', lesson);
  if (board && !board.isDestroyed()) {
    if (board.webContents.isLoading()) board.webContents.once('did-finish-load', send);
    else send();
    board.show();
    board.focus();
    return;
  }
  board = new BrowserWindow({
    width: 1280, height: 800, title: 'Lantern board', backgroundColor: '#1e2c26', autoHideMenuBar: true,
    webPreferences: secure('board-preload.js'),
  });
  board.webContents.once('did-finish-load', send);
  board.on('closed', () => { board = null; });
  board.loadFile(path.join(__dirname, 'board', 'index.html'));
}

function toggleQuick(show) {
  if (!quick) return;
  const want = show === undefined ? !quick.isVisible() : show;
  if (!want) {
    quick.hide();
    return;
  }
  const { workArea } = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const [w] = quick.getSize();
  quick.setPosition(Math.round(workArea.x + (workArea.width - w) / 2), Math.round(workArea.y + workArea.height * 0.2));
  quick.show();
  quick.focus();
  quick.webContents.send('quick:shown');
}

function togglePanel(show) {
  if (!panel) return;
  const want = show === undefined ? !panel.isVisible() : show;
  if (want) {
    panel.show();
    panel.focus();
  } else {
    panel.hide();
  }
}

async function stopEverything() {
  let result = { stopped: false };
  try {
    const r = await fetch(`${config.apiBase}/stop`, { method: 'POST', headers: { 'X-Lantern-Token': config.token } });
    result = await r.json();
  } catch (e) {
    result = { stopped: false, error: String(e) };
  }
  for (const w of [avatar, panel]) if (w) w.webContents.send('stopped', result);
  return result;
}

function lockDown() {
  // No navigation, no new windows, no device permissions from any renderer.
  app.on('web-contents-created', (_e, contents) => {
    contents.on('will-navigate', (e, url) => {
      if (!url.startsWith('file://')) e.preventDefault();
    });
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  });
  // No device permissions, except the camera for a few seconds after a click on "Check-in" in the panel.
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb, details) => {
    const video = permission === 'media' && (details.mediaTypes || []).every((t) => t === 'video');
    cb(Boolean(video && panel && wc === panel.webContents && Date.now() < cameraUntil));
  });
}

ipcMain.handle('config', () => config);
ipcMain.handle('stop', () => stopEverything());
ipcMain.handle('camera:allow-once', () => { cameraUntil = Date.now() + 5000; return true; });
ipcMain.on('avatar:hover', (_e, over) => {
  if (avatar) avatar.setIgnoreMouseEvents(!over, { forward: true });
});
ipcMain.handle('board:open', (e, lesson) => {
  // Only the panel may open the board, and only with something shaped like a lesson.
  if (!panel || e.sender !== panel.webContents) return false;
  if (!lesson || typeof lesson !== 'object' || !Array.isArray(lesson.actions) || lesson.actions.length > 200) return false;
  openBoard(lesson);
  return true;
});
ipcMain.on('panel:toggle', () => togglePanel());
ipcMain.on('panel:show', () => togglePanel(true));
ipcMain.on('quick:hide', () => toggleQuick(false));
ipcMain.on('avatar:menu', () => {
  Menu.buildFromTemplate([
    { label: 'Open Lantern', click: () => togglePanel(true) },
    { label: `Ask Lantern… (${ASK_HOTKEY.replace('CommandOrControl', 'Ctrl/Cmd')})`, click: () => toggleQuick(true) },
    { label: `STOP everything (${STOP_HOTKEY.replace('CommandOrControl', 'Ctrl/Cmd')})`, click: stopEverything },
    { type: 'separator' },
    { label: 'Quit Lantern', click: () => { quitting = true; app.quit(); } },
  ]).popup({ window: avatar });
});

// ---- menu bar icon, badge, notifications ----------------------------------------------------------
function trayImage() {
  // macOS tints a black "Template" image for light/dark menu bars; elsewhere use the amber glyph.
  const name = process.platform === 'darwin' ? 'lanternTemplate.png' : 'lantern.png';
  const image = nativeImage.createFromPath(path.join(__dirname, 'assets', 'tray', name));
  if (process.platform === 'darwin') image.setTemplateImage(true);
  return image;
}

function trayMenu(st) {
  const hotkey = (k) => k.replace('CommandOrControl', process.platform === 'darwin' ? 'Cmd' : 'Ctrl');
  return Menu.buildFromTemplate([
    { label: st.line, enabled: false },
    { type: 'separator' },
    { label: `Ask Lantern…  (${hotkey(ASK_HOTKEY)})`, click: () => toggleQuick(true) },
    { label: st.pending ? `Review ${st.pending} approval${st.pending === 1 ? '' : 's'}…` : 'Open Lantern', click: () => togglePanel(true) },
    { type: 'separator' },
    { label: `Stop everything  (${hotkey(STOP_HOTKEY)})`, click: stopEverything },
    { type: 'separator' },
    { label: 'Quit Lantern', click: () => { quitting = true; app.quit(); } },
  ]);
}

function notify(n) {
  nativeStatus.notified.push({ kind: n.kind, title: n.title, body: n.body });
  if (nativeStatus.notified.length > 20) nativeStatus.notified.shift();
  if (!Notification.isSupported()) return;
  const note = new Notification({ title: n.title, body: n.body, silent: n.kind !== 'approval' && n.kind !== 'reminder' });
  note.on('click', () => togglePanel(true));
  note.show();
}

function startNative() {
  const S = require('./renderer/lib/state.js');
  const N = require('./renderer/lib/native.js');
  const { Api } = require('./renderer/lib/api.js');
  let state = S.initial();
  let connected = false;
  let shown = '';
  try {
    tray = new Tray(trayImage());
    tray.on('click', () => togglePanel());
  } catch (e) {
    console.warn('Lantern: no system tray here', e.message);
  }
  const refresh = () => {
    const st = N.status(state, Date.now() / 1000, connected);
    Object.assign(nativeStatus, { line: st.line, badge: st.badge });
    const key = JSON.stringify(st);
    if (key === shown) return;
    shown = key;
    app.setBadgeCount(st.badge);
    if (tray) {
      tray.setToolTip(st.tooltip);
      tray.setContextMenu(trayMenu(st));
      if (process.platform === 'darwin') tray.setTitle(st.pending ? String(st.pending) : '');
    }
  };
  new Api(config.apiBase, config.token).events((e) => {
    state = S.reduce(state, { ts: Date.now() / 1000, ...e });
    const n = N.notice(e, state, { panelFocused: !!(panel && panel.isVisible() && panel.isFocused()) });
    if (n) notify(n);
    refresh();
  }, (up) => { connected = up; refresh(); });
  setInterval(refresh, 2000);
  refresh();
}

// One Lantern per user: a second launch (double-clicked shortcut, autostart plus a manual start) would add a
// second tray icon and lose the global shortcuts. It shows the running panel instead.
const firstInstance = app.requestSingleInstanceLock();
if (!firstInstance) app.quit();
app.on('second-instance', () => togglePanel(true));
// Windows shows notifications only for an app with an AppUserModelID; unpackaged, Electron's own path is the ID.
if (process.platform === 'win32') app.setAppUserModelId(app.isPackaged ? 'app.lantern.desktop' : process.execPath);

app.whenReady().then(async () => {
  if (!firstInstance) return;
  lockDown();
  if (MOCK) {
    const mock = await require('./mock/server.js').start(Number(process.env.LANTERN_MOCK_PORT || 8766));
    Object.assign(config, { apiBase: mock.url, token: mock.token, mock: true });
  } else {
    const server = await ensureServer();
    if (server === 'failed') {
      console.warn(`Lantern: could not start the server (see ${path.join(app.getPath('userData'), 'server.log')}); ` +
                   'run `lantern --serve` and `lantern --doctor`.');
    }
    config.token = process.env.LANTERN_TOKEN || readToken();   // after the start: a first run creates the token
  }
  createAvatar();
  createPanel();
  createQuick();
  startNative();
  globalShortcut.register(STOP_HOTKEY, stopEverything);
  if (!globalShortcut.register(ASK_HOTKEY, () => toggleQuick())) {
    console.warn(`Lantern: ${ASK_HOTKEY} is taken by another app; open Quick Ask from the menu instead.`);
  }
});

app.on('before-quit', () => { quitting = true; });
app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (serverProc) serverProc.kill();   // only a server this app started; one you started yourself keeps running
});
app.on('window-all-closed', () => app.quit());
