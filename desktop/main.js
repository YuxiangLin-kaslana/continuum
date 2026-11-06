// Continuum desktop: a small always-at-hand window, a tray icon and a global shortcut. The window talks to the
// Continuum server through this process (lib/client.js); the renderer only sees what preload.js exposes.
const path = require('path');
const { app, BrowserWindow, Tray, Menu, Notification, globalShortcut, ipcMain, nativeImage, net, safeStorage } =
  require('electron');
const { Client } = require('./lib/client');
const configStore = require('./lib/config');

const os = require('os');

const SMOKE = process.argv.includes('--smoke');
// The server this build talks to unless the person changes it on the login screen.
const DEFAULT_SERVER = 'https://ai-api.yuxianglin.xyz';
const SHORTCUT = 'CommandOrControl+Shift+Space';
let win = null;
let tray = null;
let quitting = false;
let config = null;
let connection = 'offline';

if (!SMOKE && !app.requestSingleInstanceLock()) app.quit();

const client = new Client({
  fetch: (url, options) => net.fetch(url, options),
  onEvent: (event) => {
    send('event', event);
    notify(event);
  },
  onState: (state) => {
    connection = state;
    send('state', state);
    updateTray();
  },
});

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function notify(event) {
  if (!Notification.isSupported() || (win && win.isVisible() && win.isFocused())) return;
  let title = null;
  let body = null;
  if (event.type === 'message' && event.message.role === 'reply') {
    title = 'Continuum 有新回复';
    body = event.message.text;
  } else if (event.type === 'ticket' && event.ticket.question) {
    title = '需要你拍板';
    body = event.ticket.question;
  }
  if (!title) return;
  const n = new Notification({ title, body: body.slice(0, 200) });
  n.on('click', show);
  n.show();
}

function icon() {
  return nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.png'));
}

// The tray gets its own small renders (tray.png and tray@2x.png), which stay sharp at 16 and 32 pixels.
function trayIcon() {
  return nativeImage.createFromPath(path.join(__dirname, 'assets', 'tray.png'));
}

function createWindow() {
  win = new BrowserWindow({
    width: 520,
    height: 780,
    minWidth: 480,
    minHeight: 560,
    show: false,
    title: 'Continuum',
    icon: icon(),
    backgroundColor: '#000000',
    // The window draws its own title bar (renderer/index.html .titlebar).
    frame: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => {
    if (!SMOKE) win.show();
  });
  // Closing the window keeps the app in the tray; quitting comes from the tray menu.
  win.on('close', (event) => {
    if (!quitting && !SMOKE) {
      event.preventDefault();
      win.hide();
    }
  });
  // Links in messages open in the browser, never inside the app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) require('electron').shell.openExternal(url);
    return { action: 'deny' };
  });
}

function show() {
  if (!win) return;
  win.show();
  win.focus();
}

function toggle() {
  if (!win) return;
  if (win.isVisible() && win.isFocused()) win.hide();
  else show();
}

function updateTray() {
  if (!tray) return;
  const label = { online: '已连接', connecting: '连接中…', offline: '未连接', unauthorized: '登录已失效' }[connection];
  tray.setToolTip(`Continuum · ${label || connection}`);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `状态：${label || connection}`, enabled: false },
    { type: 'separator' },
    { label: '显示 / 隐藏（Ctrl+Shift+Space）', click: toggle },
    {
      label: '窗口置顶', type: 'checkbox', checked: !!(win && win.isAlwaysOnTop()),
      click: (item) => win && win.setAlwaysOnTop(item.checked),
    },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit(); } },
  ]));
}

function connect() {
  const saved = config.load();
  if (saved.server && saved.token) {
    client.configure(saved.server, saved.token);
    client.connect();
  }
}

// --- IPC: the renderer's only way out -------------------------------------------------------------------------

function handle(channel, fn) {
  ipcMain.handle(channel, async (_event, ...args) => {
    try {
      return { ok: true, data: await fn(...args) };
    } catch (error) {
      return { ok: false, error: error.message, status: error.status || 0 };
    }
  });
}

handle('config:get', () => {
  const saved = config.load();
  return { server: saved.server || DEFAULT_SERVER, user: saved.user, configured: !!(saved.server && saved.token),
    connection };
});
handle('config:save', async ({ server, username, password }) => {
  const address = (server || DEFAULT_SERVER).trim().replace(/\/+$/, '');
  const device = `${os.hostname()} (${process.platform})`;
  const session = await Client.login((url, options) => net.fetch(url, options), address, username, password, device);
  config.save({ server: address, user: session.user, token: session.token });
  connect();
  return { user: session.user };
});
handle('config:logout', async () => {
  await client.logout();
  config.clear();
  connection = 'offline';
  updateTray();
  return true;
});
handle('api:threads', () => client.request('GET', '/api/v1/threads'));
handle('api:history', (thread, after) =>
  client.request('GET', `/api/v1/threads/${encodeURIComponent(thread)}/messages?after=${Number(after) || 0}`));
handle('api:post', (thread, text) =>
  client.request('POST', `/api/v1/threads/${encodeURIComponent(thread)}/messages`, { text }));
handle('api:steps', (thread) =>
  client.request('GET', `/api/v1/threads/${encodeURIComponent(thread)}/steps`));
handle('api:tickets', () => client.request('GET', '/api/v1/tickets'));
handle('api:stop', (id) => client.request('POST', `/api/v1/tickets/${encodeURIComponent(id)}/stop`, {}));
handle('api:retry', (id) => client.request('POST', `/api/v1/tickets/${encodeURIComponent(id)}/retry`));

ipcMain.on('window:minimize', () => win && win.minimize());
ipcMain.on('window:hide', () => win && win.hide());
ipcMain.on('window:maximize', () => win && (win.isMaximized() ? win.unmaximize() : win.maximize()));

// --- lifecycle ----------------------------------------------------------------------------------------------------

app.on('second-instance', show);
app.on('before-quit', () => { quitting = true; });
app.on('window-all-closed', () => { /* stays in the tray */ });

app.whenReady().then(() => {
  config = configStore.create({ app, safeStorage });
  createWindow();
  if (SMOKE) {
    // CI: the window loads, the renderer starts, and the app exits on its own.
    const timeout = setTimeout(() => { console.error('smoke: timed out'); app.exit(1); }, 20000);
    win.webContents.once('did-finish-load', async () => {
      const ready = await win.webContents.executeJavaScript('Boolean(document.querySelector("#app[data-ready]"))');
      clearTimeout(timeout);
      console.log(`smoke: renderer ready=${ready}`);
      app.exit(ready ? 0 : 1);
    });
    return;
  }
  tray = new Tray(trayIcon());
  tray.on('click', toggle);
  updateTray();
  globalShortcut.register(SHORTCUT, toggle);
  connect();
});

app.on('will-quit', () => globalShortcut.unregisterAll());
