import { Store } from './db.js';
import { normalizeOptions, normalizeAuthor, normalizePostUrl, postIdFromUrl, normalizeTag,
  matchesTags, taipeiDate, isAllowedMediaUrl, safeSegment } from './common.js';

const ACTIVE = new Set(['submitting', 'downloading']);
const EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'avif', 'mp4', 'webm']);
const uuid = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const unique = values => [...new Set(values)];
const escapeRegex = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const clock = () => globalThis.performance?.now?.() ?? Date.now();

function source(type, value, options) {
  const id = `${type}:${type === 'post' ? postIdFromUrl(value) : value}`;
  return { id, type, kind: type, value, ...(type === 'search' ? { tag: value } :
    type === 'author' ? { author: value, handle: value } : { url: value }),
    since: options.since, cursor: null, done: false, pages: 0, retryAt: 0, errors: [] };
}

function addSource(job, type, value) {
  const entry = source(type, value, job.options);
  if (!job.sources.some(existing => existing.id === entry.id)) job.sources.push(entry);
}

function postTags(post) {
  return unique((Array.isArray(post.hashtags) ? post.hashtags : []).flatMap(tag => {
    try { return [normalizeTag(typeof tag === 'object' ? tag?.text ?? tag?.tag : tag)]; }
    catch { return []; }
  }));
}

function accepted(post, options) {
  if (!matchesTags(post, options.tags)) return false;
  return !options.since || (taipeiDate(post.createdAt) !== null && taipeiDate(post.createdAt) >= options.since);
}

function addMessage(job, kind, message) {
  if (!job[kind].includes(message)) job[kind].push(message);
  job[kind] = job[kind].slice(-30);
}

function downloadValid(item, row) {
  if (item?.state !== 'complete' || item.exists !== true || !Number.isFinite(item.fileSize) || !(item.fileSize > 0) ||
    (item.finalUrl && !isAllowedMediaUrl(item.finalUrl))) return false;
  const mime = String(item.mime || '').split(';')[0].trim().toLowerCase();
  if (mime.startsWith('text/') || mime.includes('html') || mime.includes('json')) return false;
  const video = ['mp4', 'webm'].includes(row.extension);
  return !mime || mime === 'application/octet-stream' || mime.startsWith(video ? 'video/' : 'image/');
}

/** Serial, checkpointed work. One step fetches at most one source page. */
export class Engine {
  constructor({ store = new Store(), client, downloads, now = Date.now, onChange = null } = {}) {
    this.store = store;
    this.client = client;
    this.downloads = downloads;
    this.now = now;
    this.onChange = onChange;
    this.owner = uuid();
    this.serial = Promise.resolve();
    this.abortController = null;
    this.requested = null;
  }

  _operation(action) {
    const run = async () => {
      await this.store.open();
      if (!await this.store.acquireLease(this.owner, this.now())) return { ...await this.snapshot(), busy: true };
      let value;
      try { value = await action(); }
      finally { await this.store.releaseLease(this.owner); }
      if (this.onChange) { try { await this.onChange(value); } catch {} }
      return value;
    };
    const pending = this.serial.then(run, run);
    this.serial = pending.catch(() => {});
    return pending;
  }

  async snapshot() {
    await this.store.open();
    const job = await this.store.getJob();
    const stats = { posts: 0, completed: 0, skipped: 0, failed: 0, pending: 0, downloading: 0, filtered: 0 };
    if (job) {
      stats.posts = Object.keys(job.acceptedPostIds || {}).length;
      stats.filtered = Object.keys(job.filteredPostIds || {}).length;
      stats.skipped = (job.skippedMediaKeys || []).length;
      for (const row of await this.store.listMedia({ jobId: job.id })) {
        if (row.state === 'completed') { if (row.completedJobId === job.id) stats.completed++; }
        else if (ACTIVE.has(row.state)) stats.downloading++;
        else if (row.state === 'failed') stats.failed++;
        else stats.pending++;
      }
    }
    return { job, stats, knownAuthors: await this.store.knownAuthors() };
  }

  async start(rawOptions) {
    const options = normalizeOptions(rawOptions);
    return this._operation(async () => {
      const previous = await this.store.getJob();
      if (previous && ['running', 'paused'].includes(previous.status)) throw new Error('已有收集工作；請先繼續或停止目前工作。');
      this.requested = null;
      const now = this.now();
      const job = { id: uuid(), status: 'running', options, createdAt: now, updatedAt: now,
        nextRunAt: now, sources: [], acceptedPostIds: {}, filteredPostIds: {}, skippedMediaKeys: [], warnings: [], errors: [] };
      const retryPosts = new Set();
      if (options.retryFailed) {
        for (const row of await this.store.listMedia({ states: ['pending', 'failed', 'submitting', 'downloading'] })) {
          if (!accepted({ hashtags: row.hashtags, createdAt: row.createdAt }, options)) continue;
          if (!job.acceptedPostIds[row.postId] && options.postLimit && Object.keys(job.acceptedPostIds).length >= options.postLimit) continue;
          job.acceptedPostIds[row.postId] = true;
          retryPosts.add(row.postId);
          await this.store.updateMedia(row.key, old => ({ ...old,
            state: old.state === 'failed' ? 'pending' : old.state,
            jobIds: unique([...(old.jobIds || []), job.id]), updatedAt: now }));
        }
      }
      for (const postId of retryPosts) addSource(job, 'post', `https://x.com/i/web/status/${postId}`);
      for (const url of options.urls) addSource(job, 'post', normalizePostUrl(url));
      if (options.searchEnabled) for (const tag of options.tags) addSource(job, 'search', tag);
      if (options.authorMediaEnabled) {
        for (const author of options.authors) addSource(job, 'author', author);
        if (options.discoverAuthors) for (const author of await this.store.knownAuthors(options.tags)) addSource(job, 'author', author);
      }
      if (!job.sources.length && !Object.keys(job.acceptedPostIds).length) {
        job.status = 'partial';
        addMessage(job, 'warnings', '尚無可補抓的紀錄或作者；請啟用標籤搜尋、加入作者或貼文網址。');
      }
      await this.store.putJob(job);
      return this.snapshot();
    });
  }

  pause() {
    this.requested = 'paused';
    this.abortController?.abort();
    return this._operation(async () => {
      const job = await this.store.getJob();
      if (job && job.status === 'running') { job.status = 'paused'; job.updatedAt = this.now(); await this.store.putJob(job); }
      return this.snapshot();
    });
  }

  resume() {
    return this._operation(async () => {
      const job = await this.store.getJob();
      if (job && ['paused', 'stopped', 'partial', 'failed'].includes(job.status)) {
        this.requested = null;
        job.needsLogin = false;
        if (job.status === 'partial' || job.status === 'failed') {
          job.errors = [];
          for (const entry of job.sources) if (entry.errors?.length) {
            entry.done = false; entry.retryAt = 0; entry.failures = 0; entry.errors = [];
            if (Object.hasOwn(entry, 'replayCursor')) {
              entry.cursor = entry.replayCursor;
              delete entry.replayCursor;
            }
          }
        }
        if (job.options.retryFailed) for (const row of await this.store.listMedia({ states: ['failed'], jobId: job.id })) {
          await this.store.updateMedia(row.key, old => ({ ...old, state: 'pending', error: '' }));
          addSource(job, 'post', `https://x.com/i/web/status/${row.postId}`);
          const entry = job.sources.find(item => item.id === `post:${row.postId}`);
          if (entry) { entry.done = false; entry.retryAt = 0; entry.cursor = null; }
        }
        job.status = 'running'; job.updatedAt = this.now(); job.nextRunAt = this.now();
        await this.store.putJob(job);
      }
      return this.snapshot();
    });
  }

  stop() {
    this.requested = 'stopped';
    this.abortController?.abort();
    return this._operation(async () => {
      const job = await this.store.getJob();
      if (job) {
        job.status = 'stopped'; job.updatedAt = this.now();
        await this.store.putJob(job);
        for (const row of await this.store.listMedia({ states: ['submitting', 'downloading'], jobId: job.id })) {
          if (Number.isSafeInteger(row.downloadId) && row.downloadId >= 0) {
            try { await this.downloads.cancel(row.downloadId); } catch {}
          }
          await this.store.updateMedia(row.key, old => old.state === 'completed' ? null :
            { ...old, state: 'failed', error: '已停止；可於下次補抓。', updatedAt: this.now() });
        }
      }
      return this.snapshot();
    });
  }

  async _bounded(promise, milliseconds) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => reject(Object.assign(new Error('背景步驟逾時。'), { name: 'TimeoutError' })), Math.max(1, milliseconds));
      })]);
    } finally { clearTimeout(timer); }
  }

  async _recordDownload(row, item, job) {
    if (item?.state === 'complete') {
      if (downloadValid(item, row)) {
        await this.store.updateMedia(row.key, old => ({ ...old, state: 'completed', error: '',
          completedAt: old.completedAt || this.now(), completedJobId: old.completedJobId || job?.id || old.jobIds?.at(-1),
          fileSize: item.fileSize, mime: item.mime || '', actualFilename: item.filename || '', updatedAt: this.now() }));
      } else {
        await this.store.updateMedia(row.key, old => ({ ...old, state: 'failed',
          error: item.exists === false ? '下載檔案已不存在，可重新補抓。' : 'Chrome 回報的檔案大小或媒體格式不符，未標記完成。', updatedAt: this.now() }));
      }
    } else if (item?.state === 'interrupted') {
      await this.store.updateMedia(row.key, old => ({ ...old, state: 'failed', error: 'Chrome 下載已中斷，可重新補抓。', updatedAt: this.now() }));
    }
  }

  async _reconcile(row, job, deadline) {
    if (row.state === 'completed') {
      if (row.verifiedForJob === job.id) return;
      if (row.imported && row.downloadId === null) {
        if (!job.skippedMediaKeys.includes(row.key)) job.skippedMediaKeys.push(row.key);
        addMessage(job, 'warnings', '已還原的完成紀錄會保留去重；此瀏覽器無法自動檢查原電腦上的檔案。');
        await this.store.updateMedia(row.key, { verifiedForJob: job.id });
        return;
      }
      const items = await this._bounded(this.downloads.search({ id: row.downloadId }), Math.min(2000, deadline - clock()));
      const item = items.find(item => item.id === row.downloadId);
      if (item && downloadValid(item, row)) {
        if (row.completedJobId !== job.id && !job.skippedMediaKeys.includes(row.key)) job.skippedMediaKeys.push(row.key);
        await this.store.updateMedia(row.key, { verifiedForJob: job.id });
      } else {
        await this.store.updateMedia(row.key, { state: 'pending', error: '原下載已不存在或無法驗證，重新補抓。', completedJobId: null, completedAt: null });
      }
      return;
    }
    if (!ACTIVE.has(row.state)) return;
    if (Number.isSafeInteger(row.downloadId) && row.downloadId >= 0) {
      const items = await this._bounded(this.downloads.search({ id: row.downloadId }), Math.min(2000, deadline - clock()));
      const item = items.find(item => item.id === row.downloadId);
      if (item) await this._recordDownload(row, item, job);
      else await this.store.updateMedia(row.key, { state: 'failed', error: '找不到前次 Chrome 下載紀錄，請重新補抓。', updatedAt: this.now() });
    } else if (row.state === 'submitting' && row.token && row.submissionPath) {
      const parts = row.submissionPath.split('/');
      const filename = parts.pop();
      const dot = filename.lastIndexOf('.');
      const pattern = parts.map(escapeRegex).join('[/\\\\]') + '[/\\\\]' +
        escapeRegex(filename.slice(0, dot)) + '(?: \\(\\d+\\))?' + escapeRegex(filename.slice(dot)) + '$';
      const items = await this._bounded(this.downloads.search({ filenameRegex: pattern,
        startedAfter: new Date(row.submittedAt - 1000).toISOString(), limit: 30 }), Math.min(2000, deadline - clock()));
      const owned = items.filter(item => item.url === row.submittedUrl && Number.isSafeInteger(item.id) && item.id >= 0 &&
        new RegExp(pattern).test(item.filename || '') && new Date(item.startTime).getTime() >= row.submittedAt - 1000);
      if (owned.length === 1) {
        await this.store.updateMedia(row.key, { downloadId: owned[0].id, state: 'downloading' });
        await this._recordDownload(row, owned[0], job);
      } else if (this.now() - row.submittedAt >= 30_000) {
        await this.store.updateMedia(row.key, { state: 'failed', error: owned.length > 1 ? '前次下載狀態不明，請檢查下載紀錄後重試。' : '前次下載未完成提交，請重新補抓。' });
      }
    }
  }

  _discovery(post, item, job, sourceName) {
    const index = Number(item.index);
    const extension = String(item.extension || '').toLowerCase();
    if (!Number.isSafeInteger(index) || index < 1 || !EXTENSIONS.has(extension) || !isAllowedMediaUrl(item.url)) return null;
    const token = uuid().replace(/[^a-z0-9]/gi, '').slice(-16);
    const folders = [job.options.downloadSubfolder, safeSegment(job.options.tags[0])];
    if (job.options.folderByAuthor) folders.push(safeSegment(post.author));
    return { key: `twitter:${post.id}:${index}`, postId: post.id, mediaIndex: index,
      author: post.author, text: String(post.text || ''), hashtags: postTags(post), createdAt: post.createdAt,
      url: item.url, type: item.type || 'photo', extension, token,
      relativePath: `${folders.join('/')}/${post.id}_${index}_${token}.${extension}`,
      sources: [sourceName], jobIds: [job.id], updatedAt: this.now() };
  }

  async _page(job, entry, signal, deadline) {
    let committing = false;
    this.abortController = new AbortController();
    const abort = () => this.abortController?.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, Math.max(1, deadline - clock()));
    try {
      const page = await this._bounded(this.client.fetchPage(entry, entry.cursor, this.abortController.signal), deadline - clock());
      if (this.abortController.signal.aborted || this.requested) throw Object.assign(new Error(), { name: 'AbortError' });
      if (!page || !Array.isArray(page.posts)) throw new Error('來源格式不正確。');
      const rows = [];
      const authors = [];
      for (const raw of page.posts) {
        if (!/^\d+$/.test(String(raw?.id))) { addMessage(job, 'warnings', '部分貼文缺少有效編號，已略過。'); continue; }
        let author;
        try { author = normalizeAuthor(raw.author); } catch { addMessage(job, 'warnings', '部分貼文缺少有效作者資料，已略過。'); continue; }
        const post = { ...raw, id: String(raw.id), author };
        if (!accepted(post, job.options)) {
          if (!job.acceptedPostIds[post.id]) job.filteredPostIds[post.id] = true;
          continue;
        }
        if (!job.acceptedPostIds[post.id]) {
          if (job.options.postLimit && Object.keys(job.acceptedPostIds).length >= job.options.postLimit) { job.limitReached = true; continue; }
          job.acceptedPostIds[post.id] = true;
        }
        delete job.filteredPostIds[post.id];
        const tags = postTags(post);
        authors.push({ username: author, tags, updatedAt: this.now() });
        if (job.options.authorMediaEnabled && job.options.discoverAuthors) addSource(job, 'author', author);
        for (const item of Array.isArray(post.media) ? post.media : []) {
          const row = this._discovery(post, item, job, entry.id);
          if (row) rows.push(row);
          else {
            const message = '部分媒體網址或格式不受支援，已略過，來源未完整完成。';
            addMessage(job, 'errors', message);
            entry.errors = unique([...(entry.errors || []), message]);
            if (!Object.hasOwn(entry, 'replayCursor')) entry.replayCursor = structuredClone(entry.cursor);
          }
        }
      }
      entry.pages++;
      entry.retryAt = 0;
      entry.failures = 0;
      const nextCursor = page.cursor ?? null;
      if (!page.done && (nextCursor === null || JSON.stringify(nextCursor) === JSON.stringify(entry.cursor))) {
        entry.done = true;
        const message = `${entry.id} 的分頁游標未前進，來源可能未完整結束。`;
        entry.errors = [message];
        addMessage(job, 'errors', message);
      } else { entry.done = Boolean(page.done); entry.cursor = nextCursor; }
      for (const warning of page.warnings || []) if (typeof warning === 'string') addMessage(job, 'warnings', warning.slice(0, 300));
      if (job.options.postLimit && Object.keys(job.acceptedPostIds).length >= job.options.postLimit) {
        job.limitReached = true;
        for (const pending of job.sources) {
          if (pending.type !== 'post' || !job.acceptedPostIds[postIdFromUrl(pending.value)]) pending.done = true;
        }
      }
      job.updatedAt = this.now();
      committing = true;
      await this.store.commitPage(job, rows, authors);
    } catch (error) {
      if (committing) throw new Error('無法保存分頁進度；原游標已保留，請稍後繼續。');
      if (error?.name === 'AbortError' || this.abortController.signal.aborted || this.requested) {
        if (this.requested) job.status = this.requested;
        else job.nextRunAt = this.now() + 1000;
      } else if (error?.needsLogin || error?.code === 'needs_login' || error?.status === 401) {
        job.status = 'paused';
        job.needsLogin = true;
        addMessage(job, 'warnings', 'X 需要重新登入；請在一般 X 分頁完成登入後繼續。');
      } else if (error?.status === 429 || error?.rateLimit) {
        let retry = Number(error.retryAfter);
        if (!Number.isFinite(retry) || retry < 1) retry = 60;
        entry.retryAt = this.now() + Math.min(24 * 3600, retry) * 1000;
        job.nextRunAt = entry.retryAt;
        addMessage(job, 'warnings', 'X 暫時限制請求；已保存進度，等待後再繼續。');
      } else if ((['network', 'timeout', 'http_error', 'api_error'].includes(error?.code) || error?.status >= 500) &&
        (entry.failures || 0) < 2) {
        entry.failures = (entry.failures || 0) + 1;
        const seconds = Math.max(1, Math.min(300, Number(error.retryAfter) || 5 * entry.failures));
        entry.retryAt = this.now() + seconds * 1000;
        job.nextRunAt = entry.retryAt;
        addMessage(job, 'warnings', '來源暫時無法連線；已保存游標，會有限次重試。');
      } else {
        entry.done = true;
        const message = `${entry.id} 未完整完成（${Number(error?.status) || '連線或來源錯誤'}）；其他來源仍會繼續。`;
        entry.errors = [message];
        addMessage(job, 'errors', message);
      }
      job.updatedAt = this.now();
      await this.store.putJob(job);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      this.abortController = null;
    }
  }

  async _submit(row, job, deadline) {
    if (!isAllowedMediaUrl(row.url)) {
      await this.store.updateMedia(row.key, { state: 'failed', error: '媒體網址不是支援的 X HTTPS 來源。' });
      return;
    }
    if (!row.relativePath || !row.token) {
      const regenerated = this._discovery({ id: row.postId, author: row.author, hashtags: row.hashtags,
        createdAt: row.createdAt, text: row.text }, { index: row.mediaIndex, extension: row.extension, url: row.url, type: row.type }, job, '補抓');
      if (!regenerated) { await this.store.updateMedia(row.key, { state: 'failed', error: '還原的媒體紀錄不完整，請重新擷取來源。' }); return; }
      row = await this.store.updateMedia(row.key, { token: regenerated.token, relativePath: regenerated.relativePath });
    }
    const claimed = await this.store.claimMedia(row.key, this.owner, this.now());
    if (!claimed) return;
    const sameSubmission = old => old.submissionOwner === this.owner &&
      old.submittedAt === claimed.submittedAt && old.attempts === claimed.attempts;
    const submitted = Promise.resolve().then(() => this.downloads.download({ url: claimed.url,
      filename: claimed.relativePath, conflictAction: 'uniquify', saveAs: false })).then(async id => {
      if (!Number.isSafeInteger(id) || id < 0) throw new Error('Chrome 未接受下載。');
      await this.store.updateMedia(row.key, old => sameSubmission(old) && ACTIVE.has(old.state) &&
        (old.downloadId == null || old.downloadId === id) ?
        { ...old, downloadId: id, state: 'downloading', error: '', updatedAt: this.now() } : null);
      const current = await this.store.getJob(job.id);
      if (current?.status === 'stopped' || this.requested === 'stopped') {
        try { await this.downloads.cancel(id); } catch {}
        await this.store.updateMedia(row.key, old => sameSubmission(old) && old.state !== 'completed' &&
          (old.downloadId == null || old.downloadId === id) ?
          { ...old, state: 'failed', error: '已停止；可於下次補抓。', updatedAt: this.now() } : null);
      }
      return id;
    });
    try { await this._bounded(submitted, Math.min(2000, deadline - clock())); }
    catch (error) {
      if (error?.name === 'TimeoutError') {
        await this.store.updateMedia(row.key, old => sameSubmission(old) && old.state === 'submitting' ?
          { ...old, error: '正在確認前次下載提交結果。' } : null);
      } else await this.store.updateMedia(row.key, old => sameSubmission(old) && old.state !== 'completed' ?
        { ...old, state: 'failed', error: 'Chrome 未能開始下載，請檢查下載權限或稍後補抓。', updatedAt: this.now() } : null);
    }
    if (job.options.delay) job.nextRunAt = this.now() + 500 + Math.floor(Math.random() * 1000);
  }

  step({ signal, budgetMs = 12_000 } = {}) {
    return this._operation(async () => {
      const job = await this.store.getJob();
      if (!job || job.status !== 'running' || this.requested || signal?.aborted) return this.snapshot();
      const deadline = clock() + Math.min(14_000, Math.max(500, budgetMs));
      if (job.nextRunAt > this.now()) return this.snapshot();
      let rows = await this.store.listMedia({ jobId: job.id });
      for (const row of rows) {
        if (clock() > deadline - 250 || this.requested || signal?.aborted) break;
        if (ACTIVE.has(row.state) || row.state === 'completed') {
          try { await this._reconcile(row, job, deadline); }
          catch { addMessage(job, 'warnings', '暫時無法核對 Chrome 下載紀錄，稍後再確認。'); }
        }
      }
      if (clock() < deadline - 500 && !this.requested && !signal?.aborted) {
        const entry = job.sources.find(item => !item.done && item.retryAt <= this.now());
        if (entry) await this._page(job, entry, signal, deadline - 250);
      }
      if (job.status === 'running' && !this.requested && !signal?.aborted) {
        rows = await this.store.listMedia({ jobId: job.id });
        // Re-discovered completed records need verification before queueing.
        for (const row of rows.filter(row => row.state === 'completed' && row.verifiedForJob !== job.id)) {
          if (clock() > deadline - 250) break;
          try { await this._reconcile(row, job, deadline); } catch {}
        }
        rows = await this.store.listMedia({ jobId: job.id });
        let slots = Math.max(0, 3 - rows.filter(row => ACTIVE.has(row.state)).length);
        for (const row of rows.filter(row => row.state === 'pending')) {
          if (!slots-- || clock() > deadline - 250 || this.requested || signal?.aborted) break;
          await this._submit(row, job, deadline);
          if (job.options.delay) break;
        }
      }
      if (this.requested) job.status = this.requested;
      rows = await this.store.listMedia({ jobId: job.id });
      if (job.status === 'running' && job.sources.every(entry => entry.done) && !rows.some(row => row.state === 'pending' || ACTIVE.has(row.state))) {
        job.status = job.errors.length || rows.some(row => row.state === 'failed') ? 'partial' : 'completed';
      }
      if (job.status === 'running' && job.nextRunAt <= this.now()) {
        const retries = job.sources.filter(entry => !entry.done && entry.retryAt > this.now()).map(entry => entry.retryAt);
        job.nextRunAt = retries.length && !job.sources.some(entry => !entry.done && entry.retryAt <= this.now()) ? Math.min(...retries) : this.now();
        if (rows.some(row => ACTIVE.has(row.state)) && !rows.some(row => row.state === 'pending')) job.nextRunAt = Math.max(job.nextRunAt, this.now() + 1000);
      }
      job.updatedAt = this.now();
      await this.store.putJob(job);
      return this.snapshot();
    });
  }

  handleDownloadChanged(delta) {
    return this._operation(async () => {
      if (!Number.isSafeInteger(delta?.id) || delta.id < 0) return this.snapshot();
      const rows = await this.store.findByDownloadId(delta.id);
      if (!rows.length) return this.snapshot();
      const job = await this.store.getJob();
      for (const row of rows) {
        try {
          const items = await this._bounded(this.downloads.search({ id: delta.id }), 2000);
          const item = items.find(item => item.id === delta.id);
          if (item) await this._recordDownload(row, item, job);
        } catch {}
      }
      if (job && rows.some(row => (row.jobIds || []).includes(job.id))) {
        const current = await this.store.listMedia({ jobId: job.id });
        if (job.status === 'running' && job.sources.every(entry => entry.done) &&
          !current.some(row => row.state === 'pending' || ACTIVE.has(row.state))) {
          job.status = job.errors.length || current.some(row => row.state === 'failed') ? 'partial' : 'completed';
        } else if (job.status === 'completed' && current.some(row => row.state === 'failed')) job.status = 'partial';
        job.updatedAt = this.now();
        await this.store.putJob(job);
      }
      return this.snapshot();
    });
  }
}
