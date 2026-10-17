import { Store } from './db.js';
import { Engine } from './engine.js';
import { XClient } from './x-client.js';
import { normalizeOptions, taipeiDate } from './common.js';

const ALARM = 'fanart-queue';
const APP_URL = chrome.runtime.getURL('app.html');
const DEFAULTS = Object.freeze({
  theme: 'light', tagsText: '', authorsText: '', urlsText: '',
  searchEnabled: true, authorMediaEnabled: true, discoverAuthors: true,
  downloadSubdir: 'FanArtDownloader', folderByUser: true,
  limitCountEnabled: false, limitCount: 100, sinceEnabled: false,
  since: taipeiDate(Date.now()), delay: true, retryFailed: true,
});
const BOOL_SETTINGS = ['searchEnabled', 'authorMediaEnabled', 'discoverAuthors', 'folderByUser', 'limitCountEnabled', 'sinceEnabled', 'delay', 'retryFailed'];
const TEXT_SETTINGS = ['tagsText', 'authorsText', 'urlsText', 'downloadSubdir', 'since'];
const ports = new Set();
const store = new Store();
let session = { ready: null, state: 'unknown', message: '尚未檢查登入；請先在一般 X 分頁登入。' };
let drivePromise = null;
let driveAgain = false;
let broadcastPromise = null;
let logSerial = Promise.resolve();

function safeError(error) {
  const value = String(error?.message || error || '無法完成操作，請稍後再試。');
  // Network adapters use localized errors. Never forward raw server responses or credentials.
  if (/authorization|bearer\s|auth_token|csrf.?token|cookie[=:]|ct0[=:]/i.test(value)) return 'X 請求未完成；請檢查登入狀態後再試。';
  return value.slice(0, 600);
}

async function appendLog(message, level = 'info') {
  const run = async () => {
    const logs = await store.getSetting('logs', []);
    logs.push({ time: Date.now(), level, message: safeError(message) });
    await store.setSetting('logs', logs.slice(-250));
  };
  const result = logSerial.then(run, run);
  logSerial = result.catch(() => {});
  return result;
}

async function noteSnapshot(snapshot) {
  if (!snapshot?.job) return;
  const job = snapshot.job;
  const signature = { id: job.id, status: job.status, warnings: job.warnings || [], errors: job.errors || [] };
  const old = await store.getSetting('lastNotice', {});
  if (old.id !== job.id || old.status !== job.status) {
    const label = { running: '開始／繼續收集。', paused: '已暫停，分頁進度已保存。', stopped: '已停止，下載紀錄已保存。',
      completed: '本輪處理完成；X 未提供的貼文仍可能無法取得。', partial: '部分完成，請查看來源與下載錯誤。', failed: '工作未完成。' };
    await appendLog(label[job.status] || '工作狀態已更新。', job.status === 'partial' ? 'warn' : 'info');
  }
  for (const [key, level] of [['warnings', 'warn'], ['errors', 'error']]) {
    for (const message of signature[key]) if (old.id !== job.id || !(old[key] || []).includes(message)) await appendLog(message, level);
  }
  await store.setSetting('lastNotice', signature);
  if (job.needsLogin) session = { ready: false, state: 'needs_login', message: '請先在一般 X 分頁重新登入，再按「繼續收集」。' };
}

const client = new XClient();
const engine = new Engine({
  store, client,
  downloads: {
    download: options => chrome.downloads.download(options),
    search: query => chrome.downloads.search(query),
    cancel: id => chrome.downloads.cancel(id),
  },
  onChange: async snapshot => { await noteSnapshot(snapshot); await broadcast(); },
});

function settingsFrom(value = {}) {
  const settings = {};
  for (const key of BOOL_SETTINGS) if (typeof value[key] === 'boolean') settings[key] = value[key];
  for (const key of TEXT_SETTINGS) if (typeof value[key] === 'string') settings[key] = value[key].slice(0, key === 'urlsText' || key === 'authorsText' ? 65536 : 32768);
  if (value.theme === 'light' || value.theme === 'dark') settings.theme = value.theme;
  if (value.limitCount === '') settings.limitCount = '';
  else if (Number.isSafeInteger(value.limitCount) && value.limitCount >= 1 && value.limitCount <= 1000000) settings.limitCount = value.limitCount;
  return settings;
}

async function getState() {
  const [snapshot, storage, recent, logs] = await Promise.all([
    engine.snapshot(), chrome.storage.local.get('settings'), store.history({ limit: 200 }), store.getSetting('logs', []),
  ]);
  const { job, stats } = snapshot;
  return {
    settings: { ...DEFAULTS, ...settingsFrom(storage.settings) },
    job: job ? { id: job.id, status: job.status, options: job.options, createdAt: job.createdAt, updatedAt: job.updatedAt,
      nextRunAt: job.nextRunAt, needsLogin: job.needsLogin, warnings: job.warnings, errors: job.errors,
      sourceCount: job.sources.length, completedSources: job.sources.filter(source => source.done).length } : null,
    stats: { ...stats, downloaded: stats.completed },
    recent: recent.map(row => ({ key: row.key, tweetId: row.postId, postId: row.postId, author: row.author,
      url: `https://x.com/${row.author || 'i/web'}/status/${row.postId}`, status: row.state, state: row.state,
      filename: row.actualFilename || row.relativePath, relativePath: row.relativePath, imported: row.imported,
      error: row.error, createdAt: row.createdAt, updatedAt: row.updatedAt })),
    logs, session,
  };
}

async function broadcast() {
  if (!ports.size) return;
  if (broadcastPromise) return broadcastPromise;
  broadcastPromise = (async () => {
    const packet = { type: 'STATE', data: await getState() };
    for (const port of ports) {
      try { port.postMessage(packet); } catch { ports.delete(port); }
    }
  })();
  try { await broadcastPromise; } finally { broadcastPromise = null; }
}

async function ensureAlarm() {
  if (!await chrome.alarms.get(ALARM)) await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
}

// A wakeup does a bounded page/queue step. Progress lives in IndexedDB, not this promise.
function drive() {
  if (drivePromise) { driveAgain = true; return drivePromise; }
  drivePromise = (async () => {
    const started = performance.now();
    let rounds = 0;
    do {
      driveAgain = false;
      const job = await store.getJob();
      if (!job || job.status !== 'running' || job.nextRunAt > Date.now()) break;
      await engine.step({ budgetMs: 12000 });
      rounds++;
    } while (driveAgain && rounds < 2 && performance.now() - started < 14000);
  })().catch(async error => {
    await appendLog(safeError(error), 'error');
    await broadcast();
  }).finally(() => { drivePromise = null; });
  return drivePromise;
}

async function openApp() {
  const tabs = await chrome.tabs.query({ url: APP_URL });
  if (tabs[0]?.id != null) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    if (tabs[0].windowId != null) await chrome.windows.update(tabs[0].windowId, { focused: true });
  } else await chrome.tabs.create({ url: APP_URL });
}

async function handleMessage(message) {
  if (!message || typeof message.type !== 'string') throw new Error('無效的操作。');
  switch (message.type) {
    case 'GET_STATE': return getState();
    case 'SAVE_SETTINGS': {
      const existing = (await chrome.storage.local.get('settings')).settings;
      await chrome.storage.local.set({ settings: { ...settingsFrom(existing), ...settingsFrom(message.settings) } });
      return getState();
    }
    case 'START_JOB':
      await ensureAlarm();
      await engine.start(normalizeOptions(message.options));
      void drive();
      return getState();
    case 'PAUSE_JOB': await engine.pause(); return getState();
    case 'RESUME_JOB':
      await ensureAlarm(); await engine.resume(); void drive(); return getState();
    case 'STOP_JOB': await engine.stop(); return getState();
    case 'TICK':
      await ensureAlarm(); void drive(); return getState();
    case 'CHECK_SESSION':
      session = await client.checkSession(); await broadcast(); return { session };
    case 'OPEN_X': await chrome.tabs.create({ url: 'https://x.com/home' }); return { opened: true };
    case 'OPEN_DOWNLOADS': await chrome.tabs.create({ url: 'chrome://downloads/' }); return { opened: true };
    case 'EXPORT_HISTORY': return store.exportBackup();
    case 'IMPORT_HISTORY': {
      const job = await store.getJob();
      if (job && ['running', 'paused'].includes(job.status)) throw new Error('請先停止目前工作，再匯入下載紀錄。');
      const result = await store.importBackup(message.backup);
      await appendLog(`已匯入 ${result.imported} 筆紀錄；${result.existing} 筆既有紀錄保留。`);
      await broadcast(); return { ...await getState(), imported: result.imported };
    }
    default: throw new Error('不支援的操作。');
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith(chrome.runtime.getURL(''))) return false;
  handleMessage(message).then(data => sendResponse({ ok: true, data }), error => sendResponse({ ok: false, error: safeError(error) }));
  return true;
});
chrome.runtime.onConnect.addListener(port => {
  if (port.name !== 'ui' || port.sender?.id !== chrome.runtime.id || !port.sender?.url?.startsWith(chrome.runtime.getURL(''))) { port.disconnect(); return; }
  ports.add(port);
  port.onDisconnect.addListener(() => ports.delete(port));
  void broadcast().catch(() => {});
});
chrome.action.onClicked.addListener(() => { void openApp().catch(() => {}); });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) void drive(); });
chrome.downloads.onChanged.addListener(delta => {
  if (!(delta.state || delta.exists || delta.error)) return;
  void (async () => {
    if (!(await store.findByDownloadId(delta.id)).length) return;
    await engine.handleDownloadChanged(delta); await drive();
  })().catch(() => {});
});
chrome.runtime.onStartup.addListener(() => { void ensureAlarm().then(drive).catch(() => {}); });
chrome.runtime.onInstalled.addListener(() => { void ensureAlarm().then(broadcast).catch(() => {}); });
