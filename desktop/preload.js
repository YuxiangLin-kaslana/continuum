// What the window may ask of the app: a fixed set of calls, never the token or Node itself.
const { contextBridge, ipcRenderer } = require('electron');

const call = (channel) => (...args) => ipcRenderer.invoke(channel, ...args);

contextBridge.exposeInMainWorld('continuum', {
  getConfig: call('config:get'),
  saveConfig: call('config:save'),
  logout: call('config:logout'),
  threads: call('api:threads'),
  history: call('api:history'),
  post: call('api:post'),
  steps: call('api:steps'),
  tickets: call('api:tickets'),
  workers: call('api:workers'),
  approve: call('api:approve'),
  ssh: call('api:ssh'),
  sshSave: call('api:sshSave'),
  sshDelete: call('api:sshDelete'),
  sshTest: call('api:sshTest'),
  stop: call('api:stop'),
  retry: call('api:retry'),
  minimize: () => ipcRenderer.send('window:minimize'),
  hide: () => ipcRenderer.send('window:hide'),
  maximize: () => ipcRenderer.send('window:maximize'),
  updateInfo: call('update:get'),
  checkUpdate: call('update:check'),
  installUpdate: () => ipcRenderer.send('update:install'),
  onUpdate: (fn) => ipcRenderer.on('update', (_e, update) => fn(update)),
  onEvent: (fn) => ipcRenderer.on('event', (_e, event) => fn(event)),
  onState: (fn) => ipcRenderer.on('state', (_e, state) => fn(state)),
});
