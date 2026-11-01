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
  showTask: (id) => ipcRenderer.send('panel:show-task', id),   // an agent face opens its task's timeline
  onShowTask: (fn) => ipcRenderer.on('panel:show-task', (_e, id) => fn(id)),
  avatarMenu: () => ipcRenderer.send('avatar:menu'),
  openAgentFile: () => ipcRenderer.invoke('agent:open'),   // pick a .lantern-agent.json file to install
  openSkillFile: () => ipcRenderer.invoke('skill:open'),   // pick a SKILL.md file or folder to import
  saveAgentFile: ({ name, text }) => ipcRenderer.invoke('agent:save', { name, text }),   // share a pack as a file
  allowCameraOnce: () => ipcRenderer.invoke('camera:allow-once'),   // one check-in snapshot, after a click
  hideQuick: () => ipcRenderer.send('quick:hide'),
  onQuickShown: (fn) => ipcRenderer.on('quick:shown', () => fn()),
});
