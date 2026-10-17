import { isAllowedMediaUrl, normalizeAuthor, normalizeTag, validateDate } from './common.js';

const VERSION = 1;
const STATES = new Set(['pending', 'submitting', 'downloading', 'completed', 'failed']);
const IMAGE_EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'avif']);
const VIDEO_EXTENSIONS = new Set(['mp4', 'webm']);
const copy = value => value === undefined ? undefined : structuredClone(value);
const unique = values => [...new Set(values)];

function backupTags(value) {
  if (!Array.isArray(value) || value.length > 100 || value.some(tag => typeof tag !== 'string' || tag.length > 32768)) {
    throw new Error('備份含有無效或過多標籤。');
  }
  try { return unique(value.map(normalizeTag)); }
  catch { throw new Error('備份標籤格式無效。'); }
}

function backupAuthor(value) {
  try { return normalizeAuthor(value); }
  catch { throw new Error('備份作者格式無效。'); }
}

function backupDate(value, optional = false) {
  if (optional && (value === null || value === undefined)) return null;
  if (typeof value === 'string') {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error('備份日期格式無效。');
    validateDate(value.slice(0, 10));
  } else if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new Error('備份日期格式無效。');
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error('備份日期格式無效。');
  return date.toISOString();
}

function result(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function finished(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = resolve;
    transaction.onabort = () => reject(transaction.error || new Error('資料庫交易已取消。'));
    transaction.onerror = () => {};
  });
}

function discovery(old, incoming) {
  if (!old) return {
    state: 'pending', attempts: 0, error: '', downloadId: null,
    ...copy(incoming), jobIds: unique(incoming.jobIds || []),
  };
  // Discovery may refresh an expired URL, but never resets download state.
  const merged = { ...old, ...copy(incoming),
    state: old.state, attempts: old.attempts, error: old.error,
    downloadId: old.downloadId, jobIds: unique([...(old.jobIds || []), ...(incoming.jobIds || [])]),
    sources: unique([...(old.sources || []), ...(incoming.sources || [])]),
    token: old.token || incoming.token, relativePath: old.relativePath || incoming.relativePath,
  };
  for (const key of ['completedAt', 'completedJobId', 'fileSize', 'mime', 'actualFilename', 'verifiedForJob',
    'submittedAt', 'submittedUrl', 'submissionPath', 'submissionOwner', 'imported']) {
    if (key in old) merged[key] = old[key];
  }
  return merged;
}

/** IndexedDB contains public post metadata and this extension's queue, never cookies. */
export class Store {
  constructor({ name = 'fanart-downloader', indexedDB = globalThis.indexedDB } = {}) {
    this.name = name;
    this.indexedDB = indexedDB;
    this.db = null;
    this.opening = null;
  }

  async open() {
    if (this.db) return this;
    if (this.opening) return this.opening;
    if (!this.indexedDB) throw new Error('瀏覽器不支援下載紀錄資料庫。');
    this.opening = new Promise((resolve, reject) => {
      const request = this.indexedDB.open(this.name, VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        db.createObjectStore('meta', { keyPath: 'key' });
        db.createObjectStore('jobs', { keyPath: 'id' });
        const media = db.createObjectStore('media', { keyPath: 'key' });
        media.createIndex('state', 'state');
        media.createIndex('downloadId', 'downloadId');
        db.createObjectStore('authors', { keyPath: 'username' });
      };
      request.onsuccess = () => {
        this.db = request.result;
        this.db.onversionchange = () => this.close();
        resolve(this);
      };
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('請先關閉舊版擴充功能頁面，再重新開啟資料庫。'));
    });
    try { return await this.opening; } finally { this.opening = null; }
  }

  close() { this.db?.close(); this.db = null; }

  async _read(store, key) {
    await this.open();
    return result(this.db.transaction(store).objectStore(store).get(key));
  }

  async _all(store) {
    await this.open();
    return result(this.db.transaction(store).objectStore(store).getAll());
  }

  async getSetting(key, fallback = null) {
    const row = await this._read('meta', `setting:${key}`);
    return row ? row.value : fallback;
  }

  async setSetting(key, value) {
    await this.open();
    const tx = this.db.transaction('meta', 'readwrite');
    const done = finished(tx);
    tx.objectStore('meta').put({ key: `setting:${key}`, value: copy(value) });
    await done;
  }

  async getJob(id = null) {
    if (id === null) id = (await this._read('meta', 'activeJob'))?.value;
    return id ? (await this._read('jobs', id)) || null : null;
  }

  async putJob(job, { activate = true } = {}) {
    await this.open();
    const tx = this.db.transaction(['jobs', 'meta'], 'readwrite');
    const done = finished(tx);
    tx.objectStore('jobs').put(copy(job));
    if (activate) tx.objectStore('meta').put({ key: 'activeJob', value: job.id });
    await done;
    return copy(job);
  }

  async getMedia(key) { return (await this._read('media', key)) || null; }

  async listMedia({ states = [], jobId = null, limit = Infinity } = {}) {
    const rows = await this._all('media');
    return rows.filter(row => (!states.length || states.includes(row.state)) &&
      (!jobId || (row.jobIds || []).includes(jobId))).slice(0, limit);
  }

  async findByDownloadId(id) {
    await this.open();
    return result(this.db.transaction('media').objectStore('media').index('downloadId').getAll(id));
  }

  async updateMedia(key, update) {
    await this.open();
    const tx = this.db.transaction('media', 'readwrite');
    const done = finished(tx);
    const store = tx.objectStore('media');
    let updated = null;
    const request = store.get(key);
    request.onsuccess = () => {
      const old = request.result;
      if (!old) return;
      const change = typeof update === 'function' ? update(copy(old)) : { ...old, ...copy(update) };
      if (change === null || change === undefined) return;
      updated = { ...change, key: old.key };
      store.put(updated);
    };
    await done;
    return copy(updated);
  }

  async upsertMedia(row) {
    await this.open();
    const tx = this.db.transaction('media', 'readwrite');
    const done = finished(tx);
    const store = tx.objectStore('media');
    let merged;
    const request = store.get(row.key);
    request.onsuccess = () => { merged = discovery(request.result, row); store.put(merged); };
    await done;
    return copy(merged);
  }

  async claimMedia(key, owner, now) {
    return this.updateMedia(key, old => old.state === 'pending' ? {
      ...old, state: 'submitting', attempts: (old.attempts || 0) + 1,
      submittedAt: now, submittedUrl: old.url, submissionPath: old.relativePath,
      submissionOwner: owner, downloadId: null, error: '', updatedAt: now,
      completedJobId: null, completedAt: null, imported: false,
    } : null);
  }

  async knownAuthors(tags = []) {
    const rows = await this._all('authors');
    return rows.filter(row => !tags.length || (row.tags || []).some(tag => tags.includes(tag)))
      .map(row => row.username).sort();
  }

  async rememberAuthor(username, tags = [], now = Date.now()) {
    await this.open();
    const tx = this.db.transaction('authors', 'readwrite');
    const done = finished(tx);
    const store = tx.objectStore('authors');
    const request = store.get(username);
    request.onsuccess = () => store.put({ username,
      tags: unique([...(request.result?.tags || []), ...tags]),
      createdAt: request.result?.createdAt || now, updatedAt: now });
    await done;
  }

  /** Queue discoveries and the page cursor commit together, so a wakeup can replay safely. */
  async commitPage(job, rows = [], authors = []) {
    await this.open();
    const tx = this.db.transaction(['jobs', 'media', 'authors'], 'readwrite');
    const done = finished(tx);
    tx.objectStore('jobs').put(copy(job));
    const media = tx.objectStore('media');
    for (const row of rows) {
      const request = media.get(row.key);
      request.onsuccess = () => media.put(discovery(request.result, row));
    }
    const authorStore = tx.objectStore('authors');
    for (const author of authors) {
      const request = authorStore.get(author.username);
      request.onsuccess = () => authorStore.put({ ...author,
        tags: unique([...(request.result?.tags || []), ...(author.tags || [])]),
        createdAt: request.result?.createdAt || author.updatedAt });
    }
    await done;
  }

  async acquireLease(owner, now, ttl = 20_000) {
    await this.open();
    const tx = this.db.transaction('meta', 'readwrite');
    const done = finished(tx);
    const store = tx.objectStore('meta');
    let acquired = false;
    const request = store.get('engineLease');
    request.onsuccess = () => {
      const old = request.result;
      if (old && old.owner !== owner && old.expiresAt > now) return;
      acquired = true;
      store.put({ key: 'engineLease', owner, expiresAt: now + ttl });
    };
    await done;
    return acquired;
  }

  async releaseLease(owner) {
    await this.open();
    const tx = this.db.transaction('meta', 'readwrite');
    const done = finished(tx);
    const store = tx.objectStore('meta');
    const request = store.get('engineLease');
    request.onsuccess = () => { if (request.result?.owner === owner) store.delete('engineLease'); };
    await done;
  }

  async history({ limit = 100 } = {}) {
    return (await this._all('media')).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)).slice(0, limit);
  }

  async stats(jobId = null) {
    const counts = { completed: 0, failed: 0, pending: 0, downloading: 0, total: 0 };
    for (const row of await this.listMedia({ jobId })) {
      counts.total++;
      const state = row.state === 'submitting' ? 'downloading' : row.state;
      if (state in counts) counts[state]++;
    }
    return counts;
  }

  async exportBackup() {
    // Settings and meta are deliberately excluded: they are not download history.
    return { schema: 'fanart-extension-backup', version: 1, createdAt: new Date().toISOString(),
      media: await this._all('media'), authors: await this._all('authors') };
  }

  async importBackup(backup) {
    if (!backup || backup.schema !== 'fanart-extension-backup' || backup.version !== 1 ||
      !Array.isArray(backup.media) || !Array.isArray(backup.authors) ||
      backup.media.length > 200_000 || backup.authors.length > 100_000) {
      throw new Error('下載紀錄備份格式不正確。');
    }
    const rows = [...new Map(backup.media.map(row => {
      const identity = /^twitter:(\d+):([1-9]\d*)$/.exec(row?.key);
      if (!identity || String(row.postId) !== identity[1] || Number(row.mediaIndex) !== Number(identity[2]) ||
        !Number.isSafeInteger(Number(row.mediaIndex)) || typeof row.url !== 'string' || row.url.length > 8192 ||
        !isAllowedMediaUrl(row.url) || !STATES.has(row.state) || typeof row.author !== 'string' ||
        typeof row.extension !== 'string' || typeof row.type !== 'string' ||
        (row.text !== undefined && (typeof row.text !== 'string' || row.text.length > 100_000)) ||
        (row.attempts !== undefined && (!Number.isSafeInteger(row.attempts) || row.attempts < 0))) throw new Error('備份含有無效媒體紀錄。');
      const extension = row.extension.toLowerCase();
      const type = row.type.toLowerCase();
      if (!(type === 'photo' || type === 'image' ? IMAGE_EXTENSIONS.has(extension) :
        type === 'video' ? VIDEO_EXTENSIONS.has(extension) :
        type === 'animated_gif' && (extension === 'gif' || VIDEO_EXTENSIONS.has(extension)))) throw new Error('備份媒體類型或副檔名不受支援。');
      return { key: row.key, postId: String(row.postId), mediaIndex: Number(row.mediaIndex),
        author: backupAuthor(row.author), hashtags: backupTags(row.hashtags),
        text: row.text || '', createdAt: backupDate(row.createdAt), url: row.url,
        type, extension, relativePath: '', token: '',
        sources: [], jobIds: [], state: row.state === 'completed' ? 'completed' : 'failed',
        attempts: Number.isSafeInteger(row.attempts) ? Math.max(0, row.attempts) : 0,
        error: row.state === 'completed' ? '' : '已還原未完成紀錄，待補抓。',
        imported: true, downloadId: null, updatedAt: Date.now(),
        completedAt: row.completedAt == null ? null : Date.parse(backupDate(row.completedAt)) };
    }).map(row => [row.key, row])).values()];
    const normalizedAuthors = new Map();
    for (const row of backup.authors) {
      if (!row || typeof row.username !== 'string') throw new Error('備份作者格式無效。');
      const username = backupAuthor(row.username);
      const tags = backupTags(row.tags);
      if (row.createdAt != null) backupDate(row.createdAt);
      if (row.updatedAt != null) backupDate(row.updatedAt);
      normalizedAuthors.set(username, { username, tags: unique([...(normalizedAuthors.get(username)?.tags || []), ...tags]) });
    }
    await this.open();
    const tx = this.db.transaction(['media', 'authors'], 'readwrite');
    const done = finished(tx);
    let imported = 0;
    const media = tx.objectStore('media');
    for (const row of rows) {
      const request = media.get(row.key);
      request.onsuccess = () => { if (!request.result) { media.put(row); imported++; } };
    }
    for (const row of normalizedAuthors.values()) {
      const authors = tx.objectStore('authors');
      const request = authors.get(row.username);
      request.onsuccess = () => authors.put({ username: row.username,
        tags: unique([...(request.result?.tags || []), ...row.tags]),
        createdAt: request.result?.createdAt || Date.now(), updatedAt: Date.now() });
    }
    await done;
    return { imported, existing: rows.length - imported };
  }
}
