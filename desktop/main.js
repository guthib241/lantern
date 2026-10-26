// Lantern desktop (Electron main process).
// Two windows: a transparent, always-on-top avatar that lets clicks pass through except over
// the sprite, and a panel (chat, approvals, privacy, phone, costs, widgets). Renderers are
// sandboxed with context isolation; they talk to the local Lantern server on 127.0.0.1 with
// the per-install token from ~/.lantern/token. Ctrl/Cmd+Shift+X is the global STOP.
const { app, BrowserWindow, Menu, globalShortcut, ipcMain, screen, session } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const MOCK = process.argv.includes('--mock') || process.env.LANTERN_MOCK === '1';
const STOP_HOTKEY = 'CommandOrControl+Shift+X';
let config = { apiBase: process.env.LANTERN_API || 'http://127.0.0.1:8765', token: '', mock: MOCK };
let avatar = null;
let panel = null;
let quitting = false;
let cameraUntil = 0;   // the camera is allowed for one check-in right after the user clicks

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
  const w = 240;
  const h = 320;
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
  panel = new BrowserWindow({
    width: 460, height: 780, show: false, title: 'Lantern', backgroundColor: '#faf7fc',
    autoHideMenuBar: true, webPreferences: secure('preload.js'),
  });
  panel.loadFile(path.join(__dirname, 'renderer', 'panel.html'));
  panel.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      panel.hide();
    }
  });
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
ipcMain.on('panel:toggle', () => togglePanel());
ipcMain.on('panel:show', () => togglePanel(true));
ipcMain.on('avatar:menu', () => {
  Menu.buildFromTemplate([
    { label: 'Open Lantern', click: () => togglePanel(true) },
    { label: `STOP everything (${STOP_HOTKEY.replace('CommandOrControl', 'Ctrl/Cmd')})`, click: stopEverything },
    { type: 'separator' },
    { label: 'Quit Lantern', click: () => { quitting = true; app.quit(); } },
  ]).popup({ window: avatar });
});

app.whenReady().then(async () => {
  lockDown();
  if (MOCK) {
    const mock = await require('./mock/server.js').start(Number(process.env.LANTERN_MOCK_PORT || 8766));
    config = { apiBase: mock.url, token: mock.token, mock: true };
  } else {
    config.token = process.env.LANTERN_TOKEN || readToken();
  }
  createAvatar();
  createPanel();
  globalShortcut.register(STOP_HOTKEY, stopEverything);
});

app.on('before-quit', () => { quitting = true; });
app.on('will-quit', () => globalShortcut.unregisterAll());
app.on('window-all-closed', () => app.quit());
