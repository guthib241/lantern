// The board window's bridge (S16): the main process hands it a lesson to play (with its saved name, so the
// board can ask questions about it), plus the local server's address and token and a one-shot microphone
// grant for push-to-talk, exactly as the panel gets them. Nothing else is exposed.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lanternBoardHost', {
  onLesson: (fn) => ipcRenderer.on('board:lesson', (_e, payload) => fn(payload)),
  config: () => ipcRenderer.invoke('config'),
  allowMicOnce: () => ipcRenderer.invoke('mic:allow-once'),
});
