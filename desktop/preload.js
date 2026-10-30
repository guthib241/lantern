// The only bridge between the sandboxed renderers and the main process.
// Exposes a few narrow calls; no Node APIs, no ipcRenderer itself.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lantern', {
  config: () => ipcRenderer.invoke('config'),          // {apiBase, token, mock}
  stop: () => ipcRenderer.invoke('stop'),
  onStopped: (fn) => ipcRenderer.on('stopped', (_e, result) => fn(result)),
  hover: (over) => ipcRenderer.send('avatar:hover', !!over),
  togglePanel: () => ipcRenderer.send('panel:toggle'),
  showPanel: () => ipcRenderer.send('panel:show'),
  avatarMenu: () => ipcRenderer.send('avatar:menu'),
  allowCameraOnce: () => ipcRenderer.invoke('camera:allow-once'),   // one check-in snapshot, after a click
  allowMicOnce: () => ipcRenderer.invoke('mic:allow-once'),         // push to talk, after a press on the mic
  hideQuick: () => ipcRenderer.send('quick:hide'),
  onQuickShown: (fn) => ipcRenderer.on('quick:shown', () => fn()),
  openBoard: (lesson) => ipcRenderer.invoke('board:open', lesson),   // S16: play a lesson on the board window
});
