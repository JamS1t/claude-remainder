'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('bridge', {
  onState: fn => ipcRenderer.on('state', (_, v) => fn(v)),
  onCursor: fn => ipcRenderer.on('cursor', (_, v) => fn(v)),
  onGeometry: fn => ipcRenderer.on('geometry', (_, v) => fn(v)),
  geometry: () => ipcRenderer.invoke('geometry'),
  captureSource: () => ipcRenderer.invoke('capture-source'),
  setIgnore: ignore => ipcRenderer.send('ignore', ignore),
  menu: () => ipcRenderer.send('menu'),
  ready: () => ipcRenderer.send('ready'),
});
