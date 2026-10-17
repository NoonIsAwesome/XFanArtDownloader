// FanArt Downloader: shared exact matching and browser-safe input boundaries.
import { CASE_FOLD } from './case-fold.js';

export function caseFold(value) {
  return Array.from(String(value).normalize('NFC').toLowerCase(), c => CASE_FOLD[c] ?? c).join('');
}

export function normalizeTag(value) {
  if (typeof value !== 'string') throw new Error('標籤必須是文字。');
  let tag = value.trim().replace(/^[#＃]/u, '');
  if (!tag || !/^[\p{L}\p{M}\p{N}_]+$/u.test(tag)) {
    throw new Error(`標籤「${value.slice(0, 70)}」格式不正確；請輸入完整標籤，以空白或逗號分隔。`);
  }
  return caseFold(tag);
}

export function parseTags(value) {
  const values = Array.isArray(value) ? value : [value];
  const tags = new Set();
  for (const group of values) {
    if (typeof group !== 'string' || group.length > 32768) throw new Error('請輸入合理長度的標籤文字。');
    for (const token of group.trim().split(/[\s,，;；]+/u).filter(Boolean)) {
      if (/^(OR|AND|NOT)$/i.test(token)) throw new Error('不必輸入 OR／AND；多個標籤請用空白或逗號分隔。');
      tags.add(normalizeTag(token));
    }
  }
  if (!tags.size) throw new Error('請至少輸入一個完整標籤，例如 #sampleart。');
  if (tags.size > 100) throw new Error('單次最多設定 100 個標籤。');
  return [...tags];
}

const RESERVED_HANDLES = new Set(['home', 'explore', 'search', 'settings', 'notifications', 'messages', 'i']);
export function normalizeAuthor(value) {
  if (typeof value !== 'string') throw new Error('作者帳號必須是文字。');
  let handle = value.trim();
  if (handle.includes('://')) {
    let url;
    try { url = new URL(handle); } catch { throw new Error('作者網址格式不正確。'); }
    if (!['https:', 'http:'].includes(url.protocol) || !['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(url.hostname) || url.username || url.password || url.port) {
      throw new Error('作者請填 @帳號或 X 的作者媒體頁網址。');
    }
    const match = /^\/([A-Za-z0-9_]{1,15})(?:\/media)?\/?$/.exec(url.pathname);
    if (!match) throw new Error('請填作者帳號或媒體頁；貼文網址請放在貼文欄位。');
    handle = match[1];
  } else handle = handle.replace(/^@/, '');
  if (!/^[A-Za-z0-9_]{1,15}$/.test(handle) || RESERVED_HANDLES.has(handle.toLowerCase())) {
    throw new Error(`作者「${handle.slice(0, 30)}」格式不正確；每行填一個 @帳號或媒體頁網址。`);
  }
  return handle.toLowerCase();
}

export function normalizePostUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('請填完整 X 貼文網址。'); }
  if (!['https:', 'http:'].includes(url.protocol) || !['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(url.hostname) || url.username || url.password || url.port) {
    throw new Error('貼文網址必須來自 X／Twitter。');
  }
  const match = /^(\/(?:[A-Za-z0-9_]{1,15}\/status|i\/(?:web\/)?status)\/(\d+))(?:\/(?:photo|video)\/\d+)?\/?$/.exec(url.pathname);
  if (!match) throw new Error('找不到貼文編號，請檢查貼文網址。');
  return 'https://x.com' + match[1];
}

export function postIdFromUrl(value) { return normalizePostUrl(value).match(/\/(\d+)$/)[1]; }

export function safeSegment(value) {
  let name = Array.from(String(value ?? '').normalize('NFC').replace(/[<>:"/\\|?*\u0000-\u001F\u007F]/g, '_')).slice(0, 120).join('').replace(/[ .]+$/g, '').trim();
  if (!name || name === '.' || name === '..') name = '_';
  if (/^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name)) name = '_' + name;
  return name;
}

export function normalizeSubfolder(value = 'FanArtDownloader') {
  value = String(value || 'FanArtDownloader').trim();
  if (/^(?:[A-Za-z]:|[/\\])/.test(value)) throw new Error('下載位置請填 Chrome 下載資料夾內的子資料夾名稱，不能填磁碟機路徑。');
  const segments = value.replace(/\\/g, '/').split('/');
  if (segments.some(segment => segment === '..' || segment === '.')) throw new Error('下載子資料夾不能包含 . 或 .. 路徑。');
  return segments.filter(Boolean).map(safeSegment).join('/') || 'FanArtDownloader';
}

export function isAllowedMediaUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === 'https:' && !u.username && !u.password && !u.port && ['pbs.twimg.com', 'video.twimg.com'].includes(u.hostname);
  } catch { return false; }
}

export function taipeiDate(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return new Date(date.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

export function validateDate(value) {
  if (value == null || value === '') return null;
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || taipeiDate(value + 'T00:00:00+08:00') !== value) {
    throw new Error('請選擇有效的起始日期。');
  }
  return value;
}

function lines(value) {
  return (Array.isArray(value) ? value : String(value ?? '').split(/\r?\n/)).filter(v => typeof v === 'string' && v.trim());
}

export function normalizeOptions(raw = {}) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('收集設定格式不正確。');
  const authorEnabled = raw.authorMediaEnabled !== false;
  let postLimit = raw.postLimit ?? raw.limitCount ?? null;
  if (raw.limitCountEnabled === true && (postLimit === null || postLimit === '')) throw new Error('已啟用貼文上限，請填入有效的貼文數。');
  if (raw.sinceEnabled === true && !raw.since) throw new Error('已啟用日期篩選，請選擇有效的起始日期。');
  if (raw.limitCountEnabled === false) postLimit = null;
  if (postLimit !== null && postLimit !== '') {
    postLimit = Number(postLimit);
    if (!Number.isSafeInteger(postLimit) || postLimit < 1 || postLimit > 1_000_000) throw new Error('貼文上限須為 1 到 1,000,000 的整數。');
  } else postLimit = null;
  const options = {
    tags: parseTags(raw.tags ?? raw.tagsText ?? ''),
    authors: authorEnabled ? [...new Set(lines(raw.authors ?? raw.authorsText).map(normalizeAuthor))] : [],
    urls: [...new Set(lines(raw.urls ?? raw.tweetUrls ?? raw.urlsText).map(normalizePostUrl))],
    searchEnabled: raw.searchEnabled !== false,
    authorMediaEnabled: authorEnabled,
    discoverAuthors: authorEnabled && raw.discoverAuthors !== false,
    retryFailed: raw.retryFailed !== false,
    folderByAuthor: (raw.folderByAuthor ?? raw.folderByUser) !== false,
    delay: raw.delay !== false,
    postLimit,
    since: raw.sinceEnabled === false ? null : validateDate(raw.since),
    downloadSubfolder: normalizeSubfolder(raw.downloadSubfolder ?? raw.downloadSubdir),
  };
  if (!(options.searchEnabled || options.authors.length || options.discoverAuthors || options.urls.length || options.retryFailed)) throw new Error('請選擇標籤搜尋、作者補抓、重試，或填入貼文網址。');
  return options;
}

export function matchesTags(post, tags) {
  const targets = new Set(tags.map(normalizeTag));
  return (Array.isArray(post.hashtags) ? post.hashtags : []).some(tag => {
    try { return targets.has(normalizeTag(typeof tag === 'object' ? tag.text ?? tag.tag : tag)); } catch { return false; }
  });
}
