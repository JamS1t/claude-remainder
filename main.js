'use strict';
// Main process: one transparent, click-through, always-on-top window along the right edge of the
// primary display. The renderer draws the glass dock, popovers and panel inside it.
const { app, BrowserWindow, Menu, desktopCapturer, ipcMain, screen } = require('electron');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { Worker } = require('worker_threads');
const data = require('./data');

// Started from inside a Claude Code session (dev runs), the marker would leak into every session the
// projects launcher opens and turn their transcript saving off.
delete process.env.CLAUDE_CODE_CHILD_SESSION;

const WIDTH = 480; // room for dock + gap + panel + shadow
const LIMITS_EVERY = 120e3, SCAN_EVERY = 60e3, CURSOR_EVERY = 33;
// --snap=<dir>: render each UI state, save PNGs of the page and quit (visual checks; the window
// itself is invisible to screenshots because it is excluded from capture)
const SNAP_DIR = (process.argv.find(a => a.startsWith('--snap=')) || '').slice(7);

if (!SNAP_DIR && !app.requestSingleInstanceLock()) app.exit(0); // snapshots run beside a live dock

let win, display, worker;
const state = { limits: null, rtk: null, summary: null, err: '', plan: data.planName() };

const frameBounds = () => {
  const a = display.workArea;
  return { x: a.x + a.width - WIDTH, y: a.y, width: WIDTH, height: a.height };
};
const geometry = () => ({ win: win.getBounds(), display: display.bounds });
const send = (channel, value) => { if (win && !win.isDestroyed()) win.webContents.send(channel, value); };
const push = () => send('state', data.viewModel(state));
const wait = ms => new Promise(r => setTimeout(r, ms));

function createWindow() {
  display = screen.getPrimaryDisplay();
  win = new BrowserWindow({
    ...frameBounds(), show: false, frame: false, transparent: true, backgroundColor: '#00000000',
    resizable: false, movable: false, minimizable: false, maximizable: false, fullscreenable: false,
    skipTaskbar: true, focusable: false, hasShadow: false, alwaysOnTop: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), backgroundThrottling: false },
  });
  win.setAlwaysOnTop(true, 'floating');
  // Excluded from screen capture, so the glass can sample what is behind it without seeing itself.
  win.setContentProtection(true);
  win.setIgnoreMouseEvents(true, { forward: true });
  win.loadFile(path.join(__dirname, 'index.html'));
  win.once('ready-to-show', () => win.showInactive());
}

ipcMain.handle('geometry', () => geometry());
ipcMain.handle('capture-source', async () => {
  const sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 0, height: 0 } });
  const src = sources.find(s => s.display_id === String(display.id)) || sources[0];
  const f = display.scaleFactor;
  return { id: src.id, width: Math.round(display.size.width * f), height: Math.round(display.size.height * f) };
});
ipcMain.on('ignore', (_, ignore) => win.setIgnoreMouseEvents(ignore, { forward: true }));
ipcMain.on('menu', () => Menu.buildFromTemplate([
  { label: 'Refresh Now', click: () => refreshAll(true) }, // still waits out a backoff
  { type: 'separator' },
  { label: 'Quit Claude Dock', click: () => app.quit() },
]).popup({ window: win }));
ipcMain.on('ready', push);
// The window is normally unfocusable so hovering never steals focus; the search field borrows it.
ipcMain.handle('focus', (_, on) => {
  if (on) { win.setFocusable(true); win.focus(); } else { win.blur(); win.setFocusable(false); }
});
// Projects launcher: resume Claude in a project folder. The folder must be one our own scan found,
// so the page can never choose an arbitrary path to launch in.
ipcMain.on('open-project', (_, cwd) => {
  const r = ((state.summary && state.summary.recent) || []).find(x => x.cwd === cwd);
  if (!r || !fs.existsSync(r.cwd)) return;
  const fallback = () => spawn('cmd.exe', ['/c', 'start', '""', '/D', r.cwd, 'cmd', '/k', 'claude --continue'],
    { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  const wt = spawn('wt.exe', ['-d', r.cwd, 'cmd', '/k', 'claude --continue'], { detached: true, stdio: 'ignore' });
  wt.on('error', fallback); // no Windows Terminal: plain console window
  wt.unref();
});

// Hover must work while the window ignores the mouse, and must notice the pointer leaving the
// window, so the cursor position is polled and sent window-relative.
function pollCursor() {
  let last = '';
  setInterval(() => {
    if (!win || win.isDestroyed()) return;
    const p = screen.getCursorScreenPoint(), b = win.getBounds();
    const rel = { x: p.x - b.x, y: p.y - b.y };
    const key = `${rel.x},${rel.y}`;
    if (key !== last) { last = key; send('cursor', rel); }
  }, CURSOR_EVERY);
}

// Last good limits live on disk, so a restart shows them at once and skips the request while they
// are fresh. Failures back off (Retry-After, else doubling up to 30 min) instead of retrying every
// poll, which only prolongs a rate limit.
const limitsPath = () => path.join(app.getPath('userData'), 'limits.json');
let nextLimits = 0, backoff = 0;
function loadLimits() {
  try {
    const c = JSON.parse(fs.readFileSync(limitsPath(), 'utf8'));
    Object.assign(state, { limits: c.limits, limitsAt: c.at });
    nextLimits = c.at + LIMITS_EVERY;
  } catch { /* first run */ }
}
async function refreshLimits(force) {
  if (Date.now() >= nextLimits || (force === true && !backoff)) {
    try {
      state.limits = await data.fetchLimits();
      state.limitsAt = Date.now();
      state.err = '';
      backoff = 0;
      nextLimits = state.limitsAt + LIMITS_EVERY;
      fs.writeFileSync(limitsPath(), JSON.stringify({ at: state.limitsAt, limits: state.limits }));
    } catch (e) {
      backoff = Math.min(Math.max(backoff * 2, LIMITS_EVERY), 30 * 60e3);
      const wait = Math.max(e.retryAfter || 0, backoff);
      nextLimits = Date.now() + wait;
      state.err = `Couldn't ${state.limits ? 'refresh' : 'load'} limits (${e.message}). Retrying in ${Math.ceil(wait / 60e3)} min.`;
    }
  }
  push();
  state.rtk = await data.fetchRtk();
  push();
}

function startWorker() {
  worker = new Worker(path.join(__dirname, 'scan-worker.js'),
    { workerData: { cachePath: path.join(app.getPath('userData'), 'scan-cache.json') } });
  worker.on('message', m => {
    if (m.ok) state.summary = m.summary;
    else state.err = `Couldn't scan transcripts (${m.error}).`;
    push();
    snapTour();
  });
  worker.on('error', e => { state.err = `Couldn't scan transcripts (${e.message}).`; push(); });
}

function refreshAll(force) {
  refreshLimits(force);
  worker.postMessage('scan');
}

let snapped = false;
async function snapTour() {
  if (!SNAP_DIR || snapped || !state.summary) return;
  snapped = true;
  // --white: a plain white window behind the dock, to check the glass against bright backgrounds
  // --split: black left, white right, the edge under the middle of the panel (mixed backdrops)
  const split = process.argv.includes('--split');
  if (split || process.argv.includes('--white')) {
    const a = display.workArea;
    const bg = new BrowserWindow({ ...a, frame: false, backgroundColor: '#ffffff', skipTaskbar: true, focusable: false });
    if (split) bg.loadURL(`data:text/html,<body style="margin:0;height:100vh;background:linear-gradient(90deg,%23000 ${a.width - 250}px,%23fff 0)">`);
    bg.showInactive();
  }
  await wait(2500);
  fs.mkdirSync(SNAP_DIR, { recursive: true });
  const steps = [
    ['01-dock', {}, 1200], ['02-fly-session', { fly: 'session' }, 900], ['03-fly-weekly', { fly: 'weekly' }, 900],
    ['04-closing', {}, 120], ['05-closed', {}, 900], ['06-opening', { panel: true, tab: 0 }, 130],
    ['07-overview', { panel: true, tab: 0 }, 1100], ['08-projects', { panel: true, tab: 1 }, 800],
    ['09-insights', { panel: true, tab: 2 }, 800], ['10-plugins', { panel: true, tab: 3 }, 800],
    ['11-morph-to-weekly', { fly: 'weekly' }, 140], ['12-weekly', { fly: 'weekly' }, 900],
    ['13-morph-to-panel', { panel: true, tab: 3 }, 140], ['14-panel', { panel: true, tab: 3 }, 900],
    ['15-morph-to-projects', { projects: true }, 140], ['16-projects', { projects: true }, 900],
    ['17-search', { projects: true, q: 'claude' }, 300],
  ];
  for (const [name, s, ms] of steps) {
    await win.webContents.executeJavaScript(`window.__dock.force(${JSON.stringify(s)})`);
    await wait(ms);
    fs.writeFileSync(path.join(SNAP_DIR, `${name}.png`), (await win.webContents.capturePage()).toPNG());
  }
  fs.writeFileSync(path.join(SNAP_DIR, 'bounds.json'), JSON.stringify(win.getBounds()));
  app.quit();
}

app.whenReady().then(() => {
  loadLimits();
  createWindow();
  startWorker();
  refreshAll();
  setInterval(refreshLimits, LIMITS_EVERY);
  setInterval(() => worker.postMessage('scan'), SCAN_EVERY);
  pollCursor();
  screen.on('display-metrics-changed', () => {
    display = screen.getPrimaryDisplay();
    win.setBounds(frameBounds());
    send('geometry', geometry());
  });
});
app.on('window-all-closed', () => app.quit());
