// The board window's only bridge (S16): the main process hands it a lesson to play. Nothing else is exposed.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lanternBoardHost', {
  onLesson: (fn) => ipcRenderer.on('board:lesson', (_e, lesson) => fn(lesson)),
});
