import { normalizeAuthor, normalizeOptions, normalizePostUrl, normalizeSubfolder, parseTags, validateDate } from './common.js';

const $ = id => document.getElementById(id);
const runtimeReady = Boolean(globalThis.chrome?.runtime?.id && globalThis.chrome.runtime.sendMessage);
const localDate = (date = new Date()) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const DEFAULTS = {
  theme: 'light', tagsText: '', authorsText: '', urlsText: '', searchEnabled: true,
  authorMediaEnabled: true, discoverAuthors: true, downloadSubdir: 'FanArtDownloader',
  folderByUser: true, limitCountEnabled: false, limitCount: 100,
  sinceEnabled: false, since: localDate(), delay: true, retryFailed: true,
};
const CHECKBOXES = ['searchEnabled', 'authorMediaEnabled', 'discoverAuthors', 'folderByUser', 'limitCountEnabled', 'sinceEnabled', 'delay', 'retryFailed'];
const TEXT_FIELDS = ['tagsText', 'authorsText', 'urlsText', 'downloadSubdir', 'limitCount', 'since'];
const ACTIVE = new Set(['running', 'paused', 'pausing', 'resuming', 'stopping', 'queued', 'initializing', 'fetching', 'downloading', 'waiting']);
const state = { job: null, stats: {}, recent: [], logs: [], session: null };
const busy = new Set();
const editedFields = new Set();
let hydrated = false;
let saveTimer = null;
let saveChain = Promise.resolve();
let fallbackPoll = null;
let pollInFlight = false;
let tickInFlight = false;
let port = null;
let previousLogSignature = '';
let renderedLogKeys = [];
let theme = 'light';
let preferredTheme = null;
let calendarPage = new Date();
const dialogTriggers = new WeakMap();

function textNode(tag, value, className = '') {
  const element = document.createElement(tag);
  element.textContent = String(value ?? '');
  if (className) element.className = className;
  return element;
}

function setIcon(button, name) {
  button.querySelector('use')?.setAttribute('href', `#i-${name}`);
}

function message(value, tone = 'info') {
  $('actionMessage').setAttribute('role', tone === 'error' ? 'alert' : 'status');
  $('actionMessage').textContent = String(value || '');
  $('actionMessage').dataset.tone = tone;
  $('actionMessage').hidden = !value;
}

function validatedOptions() {
  const settings = readSettings();
  for (const id of TEXT_FIELDS) $(id).removeAttribute('aria-invalid');
  const checks = [
    ['tagsText', () => parseTags(settings.tagsText)],
    ['authorsText', () => {
      if (settings.authorMediaEnabled) for (const line of settings.authorsText.split(/\r?\n/).filter(line => line.trim())) normalizeAuthor(line);
    }],
    ['urlsText', () => { for (const line of settings.urlsText.split(/\r?\n/).filter(line => line.trim())) normalizePostUrl(line); }],
    ['downloadSubdir', () => normalizeSubfolder(settings.downloadSubdir)],
    ['limitCount', () => {
      if (settings.limitCountEnabled && (!Number.isSafeInteger(settings.limitCount) || settings.limitCount < 1 || settings.limitCount > 1_000_000)) throw new Error('貼文上限須為 1 到 1,000,000 的整數。');
    }],
    ['since', () => {
      if (settings.sinceEnabled && !validateDate(settings.since)) throw new Error('請選擇有效的起始日期。');
    }],
  ];
  for (const [id, check] of checks) {
    try { check(); }
    catch (error) {
      $(id).setAttribute('aria-invalid', 'true');
      $(id).setAttribute('aria-errormessage', 'actionMessage');
      const disclosure = $(id).closest('details');
      if (disclosure) disclosure.open = true;
      $(id).focus();
      throw error;
    }
  }
  return normalizeOptions(settings);
}

function errorMessage(error) {
  return typeof error === 'string' ? error : error?.message || '無法完成這個操作，請稍後再試。';
}

async function send(type, payload = {}) {
  if (!runtimeReady) throw new Error('介面預覽，無法執行下載。請在 Chrome 載入擴充功能。');
  const response = await chrome.runtime.sendMessage({ type, ...payload });
  if (!response || response.ok === false) throw new Error(errorMessage(response?.error || '擴充功能沒有回應，請重新開啟此頁。'));
  return response.data;
}

function jobStatus() {
  return String(state.job?.status || 'idle');
}

function jobActive() {
  return ACTIVE.has(jobStatus());
}

function readSettings() {
  const settings = { theme };
  for (const id of CHECKBOXES) settings[id] = $(id).checked;
  for (const id of TEXT_FIELDS) settings[id] = $(id).value;
  settings.limitCount = $('limitCount').value === '' ? '' : Number($('limitCount').value);
  return settings;
}

function hydrateSettings(settings = {}, { persistTheme = true } = {}) {
  const values = { ...DEFAULTS, ...settings };
  if (!Object.hasOwn(settings, 'tagsText') && Array.isArray(settings.tags)) values.tagsText = settings.tags.map(tag => `#${tag}`).join(' ');
  if (!Object.hasOwn(settings, 'authorsText') && Array.isArray(settings.authors)) values.authorsText = settings.authors.map(author => `@${author}`).join('\n');
  if (!Object.hasOwn(settings, 'urlsText') && Array.isArray(settings.urls)) values.urlsText = settings.urls.join('\n');
  if (!Object.hasOwn(settings, 'downloadSubdir') && settings.downloadSubfolder) values.downloadSubdir = settings.downloadSubfolder;
  if (!Object.hasOwn(settings, 'folderByUser') && typeof settings.folderByAuthor === 'boolean') values.folderByUser = settings.folderByAuthor;
  // The first GET_STATE and port packet may arrive after typing has started.
  // Apply saved defaults only to untouched controls; never replace a draft.
  for (const id of CHECKBOXES) if (!editedFields.has(id)) $(id).checked = values[id] !== false;
  for (const id of ['limitCountEnabled', 'sinceEnabled']) if (!editedFields.has(id)) $(id).checked = Boolean(values[id]);
  for (const id of TEXT_FIELDS) if (!editedFields.has(id)) $(id).value = values[id] == null ? '' : String(values[id]);
  // This origin's synchronous preference wins over a delayed worker snapshot.
  // A reload must not flash back to a stale chrome.storage theme.
  applyTheme(preferredTheme ?? values.theme, { persist: persistTheme });
  renderControls();
  renderSummary();
}

function applyTheme(value, { persist = false } = {}) {
  theme = value === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme;
  const destination = theme === 'light' ? '深色模式' : '淺色模式';
  $('themeToggle').querySelector('span').textContent = destination;
  $('themeToggle').setAttribute('aria-label', `切換為${destination}`);
  setIcon($('themeToggle'), theme === 'light' ? 'moon' : 'sun');
  if (persist) {
    preferredTheme = theme;
    try { localStorage.setItem('fanart.theme', theme); } catch { /* The worker also receives this preference. */ }
  }
}

function queueSettings() {
  clearTimeout(saveTimer);
  if (!runtimeReady) return;
  saveTimer = setTimeout(() => { void saveSettingsNow().catch(error => message(errorMessage(error), 'error')); }, 450);
}

function saveSettingsNow() {
  clearTimeout(saveTimer);
  if (!runtimeReady) return Promise.resolve();
  const snapshot = readSettings();
  const pending = saveChain.catch(() => {}).then(() => send('SAVE_SETTINGS', { settings: snapshot }));
  saveChain = pending;
  return pending;
}

function renderSummary() {
  const list = $('tagSummary');
  list.replaceChildren();
  try {
    const tags = parseTags($('tagsText').value);
    for (const tag of tags) list.append(textNode('span', `#${tag}`, 'tag-chip'));
    list.title = tags.map(tag => `#${tag}`).join(' ');
  } catch {
    list.append(textNode('span', $('tagsText').value.trim() ? '請檢查標籤' : '尚未填寫標籤', 'helper'));
    list.removeAttribute('title');
  }
  const sources = [];
  if ($('searchEnabled').checked) sources.push('標籤搜尋');
  if ($('authorMediaEnabled').checked) sources.push('作者媒體補抓');
  if ($('urlsText').value.trim()) sources.push('貼文網址');
  if (!sources.length && $('retryFailed').checked) sources.push('重試未完成');
  $('sourceSummary').textContent = sources.length ? sources.join('、') : '請選擇收集來源';
  $('downloadSubdir').title = $('downloadSubdir').value;
}

function renderControls() {
  const active = jobActive();
  const locked = active || busy.has('START_PREPARE');
  const commandBusy = [...busy].some(type => ['START_PREPARE', 'START_JOB', 'PAUSE_JOB', 'RESUME_JOB', 'STOP_JOB'].includes(type));
  const status = jobStatus();
  $('sourceFields').disabled = locked;
  $('advancedFields').disabled = locked;
  $('downloadSubdir').disabled = locked;
  $('authorsText').disabled = locked || !$('authorMediaEnabled').checked;
  $('discoverAuthors').disabled = locked || !$('authorMediaEnabled').checked;
  $('limitCount').disabled = locked || !$('limitCountEnabled').checked;
  $('since').disabled = locked || !$('sinceEnabled').checked;
  $('calendarBtn').disabled = locked || !$('sinceEnabled').checked;
  if ($('since').disabled && $('calendarDialog').open) $('calendarDialog').close();
  $('startBtn').disabled = !runtimeReady || active || commandBusy;
  const resumable = ['paused', 'stopped', 'partial', 'failed'].includes(status);
  $('pauseBtn').hidden = resumable;
  $('pauseBtn').disabled = !runtimeReady || status !== 'running' || commandBusy;
  $('resumeBtn').hidden = !resumable;
  $('resumeBtn').disabled = !runtimeReady || !resumable || commandBusy;
  $('stopBtn').disabled = !runtimeReady || !active || status === 'stopping' || commandBusy;
  $('loginBtn').disabled = !runtimeReady || busy.has('OPEN_X');
  $('checkSessionBtn').disabled = !runtimeReady || busy.has('CHECK_SESSION');
  $('openDownloadsBtn').disabled = !runtimeReady || busy.has('OPEN_DOWNLOADS');
  $('exportHistoryBtn').disabled = !runtimeReady || busy.has('EXPORT_HISTORY');
  $('importHistoryBtn').disabled = !runtimeReady || active || busy.has('IMPORT_HISTORY');
  $('startBtn').setAttribute('aria-busy', String(busy.has('START_PREPARE') || busy.has('START_JOB')));
  $('checkSessionBtn').setAttribute('aria-busy', String(busy.has('CHECK_SESSION')));
}

function finiteCount(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function renderRun() {
  const stats = { ...state.stats };
  if (stats.downloaded === undefined && stats.completed !== undefined) stats.downloaded = stats.completed;
  const status = jobStatus();
  const sourceErrors = Array.isArray(state.job?.errors) ? state.job.errors : [];
  const completedWithErrors = status === 'completed' && ((finiteCount(stats.failed) || 0) > 0 || sourceErrors.length > 0);
  const statusCopy = {
    idle: '尚未開始', queued: '準備收集', initializing: '準備收集', running: '正在收集',
    fetching: '正在收集', downloading: '正在下載', waiting: '等待下一批資料',
    pausing: '正在暫停…', paused: '已暫停', resuming: '正在繼續…', stopping: '正在安全停止…',
    stopped: '已停止', completed: '本輪處理完成', partial: '部分完成／有來源未完成',
    failed: '未完成', error: '未完成', needs_login: '需要在 X 登入', needslogin: '需要在 X 登入',
  };
  $('runState').textContent = completedWithErrors ? '部分完成／有失敗項目' : statusCopy[status] || '處理狀態已更新';
  $('runState').dataset.tone = completedWithErrors || status === 'partial' ? 'partial' : ['failed', 'error', 'needs_login', 'needslogin'].includes(status) ? 'error' : 'normal';
  const details = {
    idle: '開始後會在下方顯示下載進度。', running: '依指定標籤收集，成功與失敗都會留下紀錄。',
    paused: '進度已保留；按「繼續收集」接著處理。', stopping: '等待目前操作結束並保存紀錄。',
    stopped: '進度已保存，可以重新開始或重試未完成項目。',
    completed: '本輪已結束；X 未提供的貼文仍可能無法取得。',
    partial: '請查看紀錄中的來源或下載錯誤，再決定是否重試。',
  };
  const warning = Array.isArray(state.job?.warnings) ? state.job.warnings.at(-1) : '';
  $('resultDetail').textContent = state.job?.message || warning || (completedWithErrors ? details.partial : details[status]) || '請查看下方執行紀錄。';
  const hasActualStats = Boolean(state.job?.id || (status !== 'idle' && state.job) || Object.values(stats).some(value => finiteCount(value) > 0));
  const countItems = [['downloaded', '已下載媒體'], ['posts', '處理貼文'], ['skipped', '已略過'], ['failed', '下載失敗'], ['filtered', '標籤／日期不符'], ['pending', '等待處理'], ['downloading', '下載中']];
  $('stats').replaceChildren();
  for (const [key, label] of countItems) {
    const value = finiteCount(stats[key]);
    if (value === null) continue;
    const item = document.createElement('div');
    item.className = 'stat';
    item.append(textNode('dt', label), textNode('dd', value.toLocaleString('zh-TW')));
    $('stats').append(item);
  }
  $('stats').hidden = !hasActualStats || !$('stats').children.length;
  $('sourceErrors').hidden = !sourceErrors.length;
  $('sourceErrorList').replaceChildren();
  for (const error of sourceErrors.slice(-10)) $('sourceErrorList').append(textNode('li', typeof error === 'string' ? error : error.message || error.error || '來源未完成，請查看執行紀錄。'));
}

function displayTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-TW', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
}

function renderLogs() {
  const logs = Array.isArray(state.logs) ? state.logs.slice(-200) : [];
  const keys = logs.map(entry => JSON.stringify(entry));
  const signature = JSON.stringify(logs);
  if (signature === previousLogSignature) return;
  previousLogSignature = signature;
  const box = $('logBox');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 35;
  const appendOnly = renderedLogKeys.length <= keys.length && renderedLogKeys.every((key, index) => key === keys[index]);
  const fragment = document.createDocumentFragment();
  for (const entry of appendOnly ? logs.slice(renderedLogKeys.length) : logs) {
    const item = document.createElement('li');
    item.className = 'log-entry';
    const value = typeof entry === 'string' ? { message: entry } : entry || {};
    item.dataset.level = ['error', 'warn', 'warning', 'info'].includes(value.level) ? value.level : 'info';
    const time = displayTime(value.time || value.timestamp || value.at);
    if (time) item.append(textNode('time', time));
    item.append(textNode('span', value.message ?? value.text ?? ''));
    fragment.append(item);
  }
  if (appendOnly) $('logList').append(fragment);
  else {
    $('logList').setAttribute('aria-live', 'off');
    $('logList').replaceChildren(fragment);
    requestAnimationFrame(() => $('logList').setAttribute('aria-live', 'polite'));
  }
  renderedLogKeys = keys;
  $('logEmpty').hidden = logs.length > 0;
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderSession() {
  const session = state.session;
  let label = '尚未檢查登入';
  if (busy.has('CHECK_SESSION')) label = '正在檢查登入…';
  else if (session) {
    const status = session.state || session.status;
    if (session.message) label = String(session.message);
    else if (session.ready === true || ['signed_in', 'authenticated', 'ready'].includes(status)) label = '已取得 X 登入狀態';
    else if (['signed_out', 'needs_login', 'logged_out'].includes(status)) label = '請先在 X 登入';
    else label = '登入狀態尚未確認；可以嘗試開始收集';
  }
  $('sessionState').textContent = label;
  $('sessionState').title = label;
}

function applyState(data, { forceSettings = false } = {}) {
  if (!data || typeof data !== 'object') return;
  if ((!hydrated || forceSettings) && data.settings && typeof data.settings === 'object') {
    hydrateSettings(data.settings);
    hydrated = true;
  }
  for (const key of ['job', 'stats', 'recent', 'logs', 'session']) if (Object.hasOwn(data, key)) state[key] = data[key];
  renderRun();
  renderLogs();
  renderSession();
  renderControls();
  if ($('historyDialog').open) renderHistory();
}

async function refreshState(options) {
  const data = await send('GET_STATE');
  applyState(data, options);
  return data;
}

async function action(type, payload = {}, { quiet = false } = {}) {
  if (busy.has(type)) return;
  busy.add(type);
  if (!quiet) message('');
  renderControls();
  renderSession();
  try {
    const data = await send(type, payload);
    if (data && typeof data === 'object' && ['job', 'stats', 'logs', 'settings', 'recent', 'session'].some(key => Object.hasOwn(data, key))) applyState(data);
    if (type === 'CHECK_SESSION' && data && !Object.hasOwn(data, 'session')) state.session = data;
    if (['START_JOB', 'PAUSE_JOB', 'RESUME_JOB', 'STOP_JOB', 'IMPORT_HISTORY'].includes(type)) await refreshState();
    return data;
  } catch (error) {
    message(errorMessage(error), 'error');
    throw error;
  } finally {
    busy.delete(type);
    renderControls();
    renderSession();
  }
}

function connectStatePort() {
  try {
    port = chrome.runtime.connect({ name: 'ui' });
    port.onMessage.addListener(packet => {
      if (packet?.type === 'STATE') {
        applyState(packet.data);
        if (fallbackPoll) { clearInterval(fallbackPoll); fallbackPoll = null; }
      }
    });
    port.onDisconnect.addListener(() => { void chrome.runtime.lastError; port = null; startPolling(); });
  } catch { startPolling(); }
}

function startPolling() {
  if (fallbackPoll || !runtimeReady) return;
  fallbackPoll = setInterval(async () => {
    if (document.hidden || pollInFlight) return;
    pollInFlight = true;
    try { await refreshState(); } catch { /* The visible action reports errors when needed. */ }
    finally { pollInFlight = false; }
  }, 5000);
}

async function tickVisibleJob() {
  if (!runtimeReady || document.hidden || tickInFlight || jobStatus() !== 'running') return;
  tickInFlight = true;
  try {
    const data = await send('TICK');
    if (data && typeof data === 'object') applyState(data);
  } catch (error) { message(errorMessage(error), 'error'); }
  finally { tickInFlight = false; }
}

function openDialog(id, trigger) {
  const dialog = $(id);
  if (dialog.open) return;
  dialogTriggers.set(dialog, trigger || document.activeElement);
  dialog.showModal();
}

function closeDialog(id) {
  $(id).close();
}

function renderHistory() {
  const recent = Array.isArray(state.recent) ? state.recent : [];
  const needle = $('historySearch').value.trim().toLocaleLowerCase('zh-TW');
  const records = recent.filter(record => !needle || [record.author, record.tweetId, record.postId, record.filename, record.relativePath, record.actualFilename, record.error, record.url].some(value => String(value || '').toLocaleLowerCase('zh-TW').includes(needle)));
  $('historyRows').replaceChildren();
  const statuses = { downloaded: '已下載', completed: '已下載', success: '已下載', skipped: '已略過', failed: '下載失敗', error: '下載失敗', pending: '等待處理', queued: '等待處理', interrupted: '未完成', filtered: '標籤／日期不符' };
  for (const record of records) {
    const row = document.createElement('tr');
    const post = document.createElement('td');
    const id = record.tweetId || record.postId || '';
    const rawUrl = record.postUrl || record.url || '';
    let url = '';
    try { url = normalizePostUrl(rawUrl); } catch { /* Never turn an untrusted URL into a link. */ }
    if (!url && id) { try { url = normalizePostUrl(`https://x.com/i/status/${id}`); } catch { /* Invalid identifiers remain plain text. */ } }
    if (url) {
      const link = textNode('a', id ? `貼文 ${id}` : '開啟貼文');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      post.append(link);
    } else post.append(textNode('span', id ? `貼文 ${id}` : '未提供貼文網址'));
    if (record.author) post.append(textNode('small', `@${String(record.author).replace(/^@/, '')}`));
    const status = document.createElement('td');
    const recordState = record.status || record.state;
    status.append(textNode('span', record.imported ? '已匯入紀錄' : statuses[recordState] || String(recordState || '已記錄'), 'status-pill'));
    const file = document.createElement('td');
    const filename = record.filename || record.relativePath || record.actualFilename || record.path;
    file.append(textNode('span', filename || record.error || '—'));
    if (filename && record.error) file.append(textNode('small', record.error));
    const recorded = record.createdAt || record.updatedAt || record.downloadedAt;
    if (recorded) {
      const date = new Date(recorded);
      if (Number.isFinite(date.getTime())) file.append(textNode('small', date.toLocaleString('zh-TW', { hour12: false })));
    }
    row.append(post, status, file);
    $('historyRows').append(row);
  }
  $('historyEmpty').hidden = records.length > 0;
  $('historyEmpty').textContent = needle ? '沒有符合搜尋條件的最近紀錄。' : '尚無下載紀錄。開始收集後，結果會保存在這裡。';
  $('historyCaption').textContent = `目前顯示 ${records.length.toLocaleString('zh-TW')} 筆最近紀錄；匯出檔案包含完整可備份紀錄。`;
}

async function exportHistory() {
  try {
    const backup = await action('EXPORT_HISTORY');
    if (backup == null) throw new Error('沒有收到可匯出的紀錄。');
    const data = typeof backup === 'string' ? backup : JSON.stringify(backup, null, 2);
    const url = URL.createObjectURL(new Blob([data], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `fanart-records-${localDate()}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('historyMessage').textContent = '已匯出紀錄；圖片與影片本身不包含在備份檔內。';
  } catch (error) { $('historyMessage').textContent = errorMessage(error); }
}

async function importHistoryFile(file) {
  if (!file) return;
  try {
    if (file.size > 25 * 1024 * 1024) throw new Error('紀錄檔超過 25 MB，請選擇由此工具匯出的 JSON 檔案。');
    const backup = JSON.parse(await file.text());
    const response = await action('IMPORT_HISTORY', { backup });
    const count = response?.imported ?? response?.count;
    $('historyMessage').textContent = typeof count === 'number' ? `已匯入 ${count.toLocaleString('zh-TW')} 筆紀錄。` : '紀錄已匯入並更新。';
    renderHistory();
  } catch (error) { $('historyMessage').textContent = errorMessage(error); }
  finally { $('historyFile').value = ''; }
}

function validDate(value) {
  try { return Boolean(validateDate(value)); } catch { return false; }
}

function openCalendar() {
  if ($('since').disabled) return;
  const selected = validDate($('since').value) ? new Date(`${$('since').value}T12:00:00`) : new Date();
  calendarPage = new Date(Math.max(1752, Math.min(9999, selected.getFullYear())), selected.getMonth(), 1);
  renderCalendar();
  openDialog('calendarDialog', $('calendarBtn'));
  $('calendarDays').querySelector('.selected')?.focus();
}

function renderCalendar() {
  const year = calendarPage.getFullYear();
  const month = calendarPage.getMonth();
  $('calendarYear').value = String(year);
  $('calendarMonth').value = String(month);
  $('previousMonth').disabled = year <= 1752 && month === 0;
  $('nextMonth').disabled = year >= 9999 && month === 11;
  $('calendarDays').replaceChildren();
  const first = new Date(year, month, 1);
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - offset);
  const selected = $('since').value;
  const today = localDate();
  const focusDate = selected.startsWith(`${year}-${String(month + 1).padStart(2, '0')}-`) ? selected :
    today.startsWith(`${year}-${String(month + 1).padStart(2, '0')}-`) ? today : localDate(first);
  for (let index = 0; index < 42; index++) {
    const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index);
    const iso = localDate(date);
    const button = textNode('button', date.getDate(), 'calendar-day');
    button.type = 'button';
    button.classList.toggle('adjacent', date.getMonth() !== month);
    button.classList.toggle('selected', iso === selected);
    button.classList.toggle('today', iso === today);
    button.setAttribute('aria-label', `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日`);
    button.setAttribute('aria-pressed', String(iso === selected));
    button.tabIndex = iso === focusDate ? 0 : -1;
    if (iso === today) button.setAttribute('aria-current', 'date');
    button.disabled = date.getFullYear() < 1752 || date.getFullYear() > 9999;
    button.addEventListener('click', () => chooseDate(iso));
    button.addEventListener('keydown', event => {
      const move = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7,
        Home: -(date.getDay() + 6) % 7, End: 6 - (date.getDay() + 6) % 7 }[event.key];
      if (move === undefined && !['PageUp', 'PageDown'].includes(event.key)) return;
      event.preventDefault();
      let target;
      if (event.key === 'PageUp' || event.key === 'PageDown') {
        const delta = (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1);
        const targetMonth = new Date(date.getFullYear(), date.getMonth() + delta, 1);
        const lastDay = new Date(targetMonth.getFullYear(), targetMonth.getMonth() + 1, 0).getDate();
        target = new Date(targetMonth.getFullYear(), targetMonth.getMonth(), Math.min(date.getDate(), lastDay));
      } else target = new Date(date.getFullYear(), date.getMonth(), date.getDate() + move);
      if (target.getFullYear() < 1752 || target.getFullYear() > 9999) return;
      if (target.getMonth() !== month || target.getFullYear() !== year) { calendarPage = new Date(target.getFullYear(), target.getMonth(), 1); renderCalendar(); }
      const targetLabel = `${target.getFullYear()} 年 ${target.getMonth() + 1} 月 ${target.getDate()} 日`;
      const targetButton = [...$('calendarDays').children].find(cell => cell.getAttribute('aria-label') === targetLabel);
      for (const cell of $('calendarDays').children) cell.tabIndex = cell === targetButton ? 0 : -1;
      targetButton?.focus();
    });
    $('calendarDays').append(button);
  }
}

function chooseDate(iso) {
  if (!validDate(iso) || $('since').disabled) return;
  $('since').value = iso;
  editedFields.add('since');
  closeDialog('calendarDialog');
  queueSettings();
}

function bindEvents() {
  for (const id of [...CHECKBOXES, ...TEXT_FIELDS]) $(id).addEventListener(CHECKBOXES.includes(id) ? 'change' : 'input', () => {
    editedFields.add(id);
    $(id).removeAttribute('aria-invalid');
    renderSummary();
    renderControls();
    queueSettings();
  });
  $('themeToggle').addEventListener('click', () => {
    editedFields.add('theme');
    applyTheme(theme === 'light' ? 'dark' : 'light', { persist: true });
    // Theme changes bypass the draft debounce. localStorage is already written
    // above, so even an immediate reload or a slow worker keeps the new mode.
    void saveSettingsNow().catch(error => message(errorMessage(error), 'error'));
  });
  $('aboutBtn').addEventListener('click', event => openDialog('aboutDialog', event.currentTarget));
  $('noticeBtn').addEventListener('click', event => openDialog('noticeDialog', event.currentTarget));
  $('historyBtn').addEventListener('click', event => { renderHistory(); openDialog('historyDialog', event.currentTarget); });
  $('loginBtn').addEventListener('click', () => { void action('OPEN_X').catch(() => {}); });
  $('checkSessionBtn').addEventListener('click', () => { void action('CHECK_SESSION').catch(() => {}); });
  $('openDownloadsBtn').addEventListener('click', () => { void action('OPEN_DOWNLOADS').catch(() => {}); });
  $('collectionForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (!runtimeReady || jobActive() || busy.has('START_JOB') || busy.has('START_PREPARE')) return;
    try {
      const options = validatedOptions();
      busy.add('START_PREPARE');
      renderControls();
      await saveSettingsNow();
      await action('START_JOB', { options });
    } catch (error) { message(errorMessage(error), 'error'); }
    finally { busy.delete('START_PREPARE'); renderControls(); }
  });
  for (const [id, command] of [['pauseBtn', 'PAUSE_JOB'], ['resumeBtn', 'RESUME_JOB'], ['stopBtn', 'STOP_JOB']]) $(id).addEventListener('click', () => { void action(command).catch(() => {}); });
  $('exportHistoryBtn').addEventListener('click', () => { void exportHistory(); });
  $('importHistoryBtn').addEventListener('click', () => $('historyFile').click());
  $('historyFile').addEventListener('change', () => { void importHistoryFile($('historyFile').files[0]); });
  $('historySearch').addEventListener('input', renderHistory);
  for (const button of document.querySelectorAll('[data-close-dialog]')) button.addEventListener('click', () => closeDialog(button.dataset.closeDialog));
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.addEventListener('close', () => { const trigger = dialogTriggers.get(dialog); if (trigger?.isConnected && !trigger.disabled) trigger.focus(); });
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  }
  $('calendarBtn').addEventListener('click', openCalendar);
  $('since').addEventListener('keydown', event => { if (event.key === 'F4' || (event.altKey && event.key === 'ArrowDown')) { event.preventDefault(); openCalendar(); } });
  $('previousMonth').addEventListener('click', () => { calendarPage = new Date(calendarPage.getFullYear(), calendarPage.getMonth() - 1, 1); renderCalendar(); });
  $('nextMonth').addEventListener('click', () => { calendarPage = new Date(calendarPage.getFullYear(), calendarPage.getMonth() + 1, 1); renderCalendar(); });
  $('calendarMonth').addEventListener('change', () => { calendarPage = new Date(calendarPage.getFullYear(), Number($('calendarMonth').value), 1); renderCalendar(); });
  $('calendarYear').addEventListener('change', () => {
    const year = Number($('calendarYear').value);
    if (Number.isInteger(year) && year >= 1752 && year <= 9999) calendarPage = new Date(year, calendarPage.getMonth(), 1);
    renderCalendar();
  });
  $('calendarToday').addEventListener('click', () => chooseDate(localDate()));
  document.addEventListener('visibilitychange', () => { if (!document.hidden && runtimeReady) void refreshState().catch(() => {}); });
  window.addEventListener('storage', event => {
    if (event.key === 'fanart.theme' && ['light', 'dark'].includes(event.newValue)) {
      preferredTheme = event.newValue;
      applyTheme(preferredTheme);
    }
  });
}

async function initialize() {
  try {
    const remembered = localStorage.getItem('fanart.theme');
    if (['light', 'dark'].includes(remembered)) preferredTheme = remembered;
  } catch { /* The first worker snapshot supplies the initial preference. */ }
  hydrateSettings({ ...DEFAULTS, theme: preferredTheme ?? 'light' }, { persistTheme: false });
  bindEvents();
  if (!runtimeReady) {
    $('previewBanner').hidden = false;
    renderControls();
    return;
  }
  connectStatePort();
  // One bounded engine step at most every eight seconds while this page is
  // visible and running. GET_STATE polling never advances collection work.
  setInterval(() => { void tickVisibleJob(); }, 8000);
  await refreshState();
}

void initialize().catch(error => message(errorMessage(error), 'error'));
