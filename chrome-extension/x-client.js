/* SPDX-License-Identifier: GPL-2.0-only
 * Copyright 2025-2026 Mike Fährmann (gallery-dl Twitter extractor).
 * JavaScript adaptation for FanArtDownloader, 2026.
 * Endpoint defaults, request features and transaction integration are derived
 * from gallery-dl 1.32.16, distributed under the GNU GPL version 2.
 * This uses the X website session, not the paid developer API or an OAuth grant.
 */
import { XTransaction } from './x-transaction.js';

const ROOT = 'https://x.com';
const PUBLIC_BEARER = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA';
export const DEFAULT_QUERY_IDS = Object.freeze({
  SearchTimeline: '4fpceYZ6-YQCx_JSl_Cn_A', UserMedia: 'jCRhbOzdgOHp6u9H4g2tEg',
  TweetResultByRestId: 'qxWQxcMLiTPcavz9Qy5hwQ', UserByScreenName: 'ck5KkZ8t5cOmoLssopN99Q',
});
const FEATURES = Object.freeze({
  rweb_video_screen_enabled: false, payments_enabled: false, rweb_xchat_enabled: false,
  profile_label_improvements_pcf_label_in_post_enabled: true, rweb_tipjar_consumption_enabled: true,
  verified_phone_label_enabled: false, creator_subscriptions_tweet_preview_api_enabled: true,
  responsive_web_graphql_timeline_navigation_enabled: true,
  responsive_web_graphql_skip_user_profile_image_extensions_enabled: false,
  premium_content_api_read_enabled: false, communities_web_enable_tweet_community_results_fetch: true,
  c9s_tweet_anatomy_moderator_badge_enabled: true, responsive_web_grok_analyze_button_fetch_trends_enabled: false,
  responsive_web_grok_analyze_post_followups_enabled: true, responsive_web_jetfuel_frame: true,
  responsive_web_grok_share_attachment_enabled: true, articles_preview_enabled: true,
  responsive_web_edit_tweet_api_enabled: true, graphql_is_translatable_rweb_tweet_is_translatable_enabled: true,
  view_counts_everywhere_api_enabled: true, longform_notetweets_consumption_enabled: true,
  responsive_web_twitter_article_tweet_consumption_enabled: true, tweet_awards_web_tipping_enabled: false,
  responsive_web_grok_show_grok_translated_post: false, responsive_web_grok_analysis_button_from_backend: true,
  creator_subscriptions_quote_tweet_preview_enabled: false, freedom_of_speech_not_reach_fetch_enabled: true,
  standardized_nudges_misinfo: true, tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled: true,
  longform_notetweets_rich_text_read_enabled: true, longform_notetweets_inline_media_enabled: true,
  responsive_web_grok_image_annotation_enabled: true, responsive_web_grok_imagine_annotation_enabled: true,
  responsive_web_grok_community_note_auto_translation_is_enabled: false, responsive_web_enhance_cards_enabled: false,
  hidden_profile_subscriptions_enabled: true, highlights_tweets_tab_ui_enabled: true,
  responsive_web_twitter_article_notes_tab_enabled: true, subscriptions_feature_can_gift_premium: true,
  subscriptions_verification_info_is_identity_verified_enabled: true, subscriptions_verification_info_verified_since_enabled: true,
});

export class XClientError extends Error {
  constructor(code, message, { status = 0, retryAfter = 0, needsLogin = false } = {}) {
    super(message); this.name = 'XClientError'; this.code = code;
    this.status = status; this.retryAfter = retryAfter; this.needsLogin = needsLogin;
  }
}

function validNetworkURL(value) {
  let url;
  try { url = new URL(value); } catch { return false; }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) return false;
  if (url.hostname === 'x.com') return url.pathname === '/home' || url.pathname.startsWith('/i/api/');
  return url.hostname === 'abs.twimg.com' && /^\/responsive-web\/client-web(?:-legacy)?\/(?:main|ondemand\.s)\.[\w.-]+\.js$/.test(url.pathname);
}

function mediaURL(value, host) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === host && !url.port && !url.username && !url.password ? url : null;
  } catch { return null; }
}

function imageURL(value) {
  const url = mediaURL(value, 'pbs.twimg.com');
  if (!url || !url.pathname.startsWith('/media/')) return null;
  const match = url.pathname.match(/\.(jpe?g|png|gif|webp|avif)(?::\w+)?$/i);
  const extension = (url.searchParams.get('format') || match?.[1] || '').toLowerCase();
  if (!/^(jpe?g|png|gif|webp|avif)$/.test(extension)) return null;
  if (match) url.pathname = url.pathname.slice(0, match.index);
  url.search = ''; url.searchParams.set('format', extension); url.searchParams.set('name', 'orig');
  return { url: url.href, extension };
}

function unwrapTweet(value) {
  for (let i = 0; value && i < 3; i++) {
    if (value.tweet) value = value.tweet; else break;
  }
  return value;
}

export function normalizeTweet(value) {
  const tweet = unwrapTweet(value), legacy = tweet?.legacy;
  if (!legacy || legacy.retweeted_status_result || legacy.retweeted_status_id_str || tweet.retweeted_status_result) return null;
  const id = String(tweet.rest_id || legacy.id_str || '');
  const user = tweet.core?.user_results?.result;
  const author = user?.core?.screen_name || user?.legacy?.screen_name;
  if (!/^\d+$/.test(id) || !/^[A-Za-z0-9_]{1,15}$/.test(author || '')) return null;
  const note = tweet.note_tweet?.note_tweet_results?.result;
  const entities = [legacy.entities, note?.entity_set];
  const hashtags = [...new Set(entities.flatMap(entity => (entity?.hashtags || []).map(tag => tag.text || tag.tag).filter(tag => typeof tag === 'string')))];
  let date = new Date(legacy.created_at || tweet.core?.created_at || '');
  if (!Number.isFinite(date.getTime())) {
    try { date = new Date(Number((BigInt(id) >> 22n) + 1288834974657n)); } catch { return null; }
  }
  if (!Number.isFinite(date.getTime())) return null;
  const media = [];
  const authorId = String(user?.rest_id || legacy.user_id_str || '');
  for (const [index, item] of (legacy.extended_entities?.media || legacy.entities?.media || []).entries()) {
    // Never attribute another post's quoted/retweeted media to this post.
    if (item.source_status_id_str && item.source_status_id_str !== id) continue;
    if (item.source_user_id_str && authorId && item.source_user_id_str !== authorId) continue;
    if (item.type === 'photo') {
      const image = imageURL(item.media_url_https || item.media_url);
      if (image) media.push({ index: index + 1, ...image, type: 'image' });
    } else if (item.type === 'video' || item.type === 'animated_gif') {
      const variants = (item.video_info?.variants || []).filter(variant => variant.content_type === 'video/mp4' && mediaURL(variant.url, 'video.twimg.com'));
      variants.sort((a, b) => Number(b.bitrate || 0) - Number(a.bitrate || 0));
      if (variants.length) media.push({ index: index + 1, url: variants[0].url, type: 'video', extension: 'mp4' });
    }
  }
  return { id, author, text: note?.text ?? legacy.full_text ?? legacy.text ?? '', hashtags,
    createdAt: date.toISOString(), media, url: `${ROOT}/${author}/status/${id}` };
}

export function parseQueryIds(script) {
  const ids = {};
  const patterns = [
    /["']?queryId["']?\s*:\s*["']([\w-]+)["']\s*,\s*["']?operationName["']?\s*:\s*["'](SearchTimeline|UserMedia|TweetResultByRestId|UserByScreenName)["']/g,
    /["']?operationName["']?\s*:\s*["'](SearchTimeline|UserMedia|TweetResultByRestId|UserByScreenName)["']\s*,\s*["']?queryId["']?\s*:\s*["']([\w-]+)["']/g,
  ];
  for (const match of script.matchAll(patterns[0])) ids[match[2]] = match[1];
  for (const match of script.matchAll(patterns[1])) ids[match[1]] = match[2];
  return ids;
}

export function parseTimeline(data, previousCursor = null) {
  const instructions = data?.data?.search_by_raw_query?.search_timeline?.timeline?.instructions
    || data?.data?.user?.result?.timeline_v2?.timeline?.instructions
    || data?.data?.user?.result?.timeline?.timeline?.instructions
    || data?.data?.user?.result?.timeline?.instructions;
  if (!Array.isArray(instructions)) throw new XClientError('unavailable', 'X 未提供可讀取的時間軸；作者可能無法存取，或網頁介面已變更。');
  const posts = [], seen = new Set();
  let cursor = null, terminated = false;
  function readItem(item) {
    if (!item || typeof item !== 'object') return;
    if (item.promotedMetadata) return;
    if (item.cursorType === 'Bottom' && typeof item.value === 'string') cursor = item.value;
    if (item.tweet_results) {
      const post = normalizeTweet(item.tweet_results.result);
      if (post && !seen.has(post.id)) { seen.add(post.id); posts.push(post); }
      return; // Quoted tweets inside tweet_results are deliberately not traversed.
    }
    for (const key of ['content', 'itemContent', 'item']) if (item[key]) readItem(item[key]);
    for (const key of ['items', 'moduleItems']) if (Array.isArray(item[key])) item[key].forEach(readItem);
  }
  for (const instruction of instructions) {
    if (instruction.type === 'TimelineTerminateTimeline' && instruction.direction === 'Bottom') terminated = true;
    (instruction.entries || []).forEach(readItem);
    if (instruction.entry) readItem(instruction.entry);
    if (instruction.moduleItems) instruction.moduleItems.forEach(readItem);
  }
  const repeated = Boolean(cursor && cursor === previousCursor);
  return { posts, cursor: terminated || repeated ? null : cursor, done: terminated || repeated || !cursor,
    warnings: repeated ? ['X 回傳相同分頁位置，已停止此來源以避免重複請求。'] : [] };
}

function retrySeconds(response) {
  const value = response.headers?.get('retry-after');
  let result = value && /^\d+$/.test(value) ? Number(value) : value ? (Date.parse(value) - Date.now()) / 1000 : 0;
  const reset = Number(response.headers?.get('x-rate-limit-reset'));
  if (!result && reset) result = reset - Date.now() / 1000;
  return Math.max(1, Math.ceil(Number.isFinite(result) && result > 0 ? result : 60));
}

export class XClient {
  constructor({ fetchFn = globalThis.fetch?.bind(globalThis), cookies = globalThis.chrome?.cookies, onStatus = () => {}, transactionFactory = XTransaction.fromHomepage } = {}) {
    this.fetchFn = fetchFn; this.cookies = cookies; this.onStatus = onStatus;
    this.transactionFactory = transactionFactory; this.queryIds = { ...DEFAULT_QUERY_IDS };
    this.transaction = null; this.bootstrapAt = 0; this.userIds = new Map();
  }

  async _csrf() {
    let cookie;
    try { cookie = await this.cookies?.get({ url: ROOT + '/', name: 'ct0' }); } catch { /* no raw cookie API exception */ }
    if (!cookie?.value || /[\r\n]/.test(cookie.value) || (cookie.expirationDate != null && cookie.expirationDate <= Date.now() / 1000)) {
      throw new XClientError('needs_login', '請先在此 Chrome 登入 X，並允許擴充功能存取 X。', { needsLogin: true });
    }
    return cookie.value;
  }

  async _fetch(url, { signal, headers = {}, json = false } = {}) {
    if (!validNetworkURL(url)) throw new XClientError('invalid_url', '拒絕存取非允許的網路位置。');
    if (signal?.aborted) throw new XClientError('cancelled', '已停止。');
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 15000);
    try {
      const response = await this.fetchFn(url, { method: 'GET', credentials: 'include', redirect: 'error',
        cache: 'no-store', headers, signal: controller.signal, referrerPolicy: 'no-referrer' });
      if (response.redirected || (response.url && !validNetworkURL(response.url))) throw new XClientError('needs_login', 'X 將請求導向其他頁面，請重新確認登入。', { needsLogin: true });
      if (response.status === 401) throw new XClientError('needs_login', 'X 登入已失效，請在 X 分頁重新登入。', { status: 401, needsLogin: true });
      if (response.status === 429 || response.status === 403) throw new XClientError(response.status === 429 ? 'rate_limit' : 'forbidden', response.status === 429 ? 'X 要求稍後重試此來源。' : 'X 拒絕此請求；請檢查帳號存取狀態後重試。', { status: response.status, retryAfter: retrySeconds(response) });
      if (!response.ok) throw new XClientError(response.status === 404 ? 'not_found' : 'http_error', 'X 或其網頁資源暫時無法讀取。', { status: response.status, retryAfter: response.status >= 500 ? 30 : 0 });
      const text = await response.text();
      if (text.length > 8_000_000) throw new XClientError('invalid_response', 'X 回應過大，已停止解析。');
      if (!json) return text;
      let data;
      try { data = JSON.parse(text); } catch { throw new XClientError('invalid_response', 'X 未回傳有效的資料格式。'); }
      const errors = data.errors || [];
      if (errors.some(error => [32, 89, 215, 239, 326].includes(Number(error.code)))) throw new XClientError('needs_login', 'X 要求重新登入或確認帳號狀態。', { needsLogin: true });
      if (errors.some(error => Number(error.code) === 88)) throw new XClientError('rate_limit', 'X 要求稍後重試此來源。', { retryAfter: retrySeconds(response), status: 429 });
      if (errors.length && !data.data) throw new XClientError('api_error', 'X 未能提供此來源資料。', { retryAfter: 30 });
      return data;
    } catch (error) {
      if (error instanceof XClientError) throw error;
      if (controller.signal.aborted) throw new XClientError(signal?.aborted ? 'cancelled' : 'timeout', signal?.aborted ? '已停止。' : 'X 請求逾時，可稍後重試。', { retryAfter: signal?.aborted ? 0 : 5 });
      // Never surface fetch errors that might contain a request, URL or headers.
      throw new XClientError('network', '無法連線至 X；也可能是登入重新導向，請確認 X 分頁與網路。', { retryAfter: 15 });
    } finally {
      clearTimeout(timer); signal?.removeEventListener('abort', abort);
    }
  }

  async _bootstrap(signal, force = false) {
    if (!force && this.transaction && Date.now() - this.bootstrapAt < 3 * 60 * 60 * 1000) return;
    this.onStatus('正在更新 X 網頁介面資料…');
    const html = await this._fetch(ROOT + '/home', { signal });
    const main = html.match(/https:\/\/abs\.twimg\.com\/responsive-web\/client-web(?:-legacy)?\/main\.[\w.-]+\.js/)?.[0];
    this.queryIds = { ...DEFAULT_QUERY_IDS };
    if (main) {
      const script = await this._fetch(main, { signal });
      Object.assign(this.queryIds, parseQueryIds(script));
    }
    try { this.transaction = await this.transactionFactory(html, url => this._fetch(url, { signal })); }
    catch (error) {
      if (error instanceof XClientError) throw error;
      throw new XClientError('adapter_changed', 'X 網頁驗證格式已變更，請更新擴充功能後再試。');
    }
    this.bootstrapAt = Date.now();
  }

  async _api(path, parameters = {}, signal, refresh = true) {
    await this._csrf();
    await this._bootstrap(signal);
    // Homepage requests may rotate ct0; read its current value afterwards.
    const csrf = await this._csrf();
    const url = new URL(path, ROOT);
    for (const [key, value] of Object.entries(parameters)) url.searchParams.set(key, typeof value === 'string' ? value : JSON.stringify(value));
    let transaction;
    try { transaction = await this.transaction.generate('GET', url.pathname); }
    catch { throw new XClientError('adapter_changed', '無法建立 X 網頁請求資料，請更新擴充功能。'); }
    try {
      return await this._fetch(url.href, { signal, json: true, headers: {
        authorization: 'Bearer ' + PUBLIC_BEARER, 'x-csrf-token': csrf,
        'x-twitter-auth-type': 'OAuth2Session', 'x-twitter-active-user': 'yes',
        'x-twitter-client-language': 'en', 'x-client-transaction-id': transaction,
        accept: 'application/json',
      } });
    } catch (error) {
      if (error.status === 404 && refresh && path.includes('/graphql/')) {
        const operation = path.split('/').at(-1);
        await this._bootstrap(signal, true);
        return this._api(`/i/api/graphql/${this.queryIds[operation]}/${operation}`, parameters, signal, false);
      }
      throw error;
    }
  }

  async _graphql(operation, variables, signal) {
    await this._csrf(); await this._bootstrap(signal);
    const features = { ...FEATURES };
    if (operation === 'TweetResultByRestId') delete features.rweb_video_screen_enabled;
    return this._api(`/i/api/graphql/${this.queryIds[operation]}/${operation}`, {
      variables, features, fieldToggles: { withArticleRichContentState: true, withArticlePlainText: false,
        withGrokAnalyze: false, withDisallowedReplyControls: false, withAuxiliaryUserLabels: true },
    }, signal);
  }

  async checkSession() {
    try {
      const data = await this._api('/i/api/1.1/account/verify_credentials.json', { skip_status: 'true', include_entities: 'false' });
      if (/^\d+$/.test(String(data.id_str || '')) && /^[A-Za-z0-9_]{1,15}$/.test(data.screen_name || '')) return { ready: true, state: 'ready', needsLogin: false, message: 'X 已確認此 Chrome 的登入狀態。' };
      return { ready: false, state: 'unknown', needsLogin: false, message: '目前無法確認 X 登入；可開始抓取，由 X 貼文回應再次確認。' };
    } catch (error) {
      const needsLogin = error instanceof XClientError && error.needsLogin;
      return { ready: false, state: needsLogin ? 'needs_login' : 'unknown', needsLogin,
        message: needsLogin ? error.message : '目前無法確認 X 登入；可開始抓取，由 X 貼文回應再次確認。' };
    }
  }

  async fetchPage(source, cursor = null, signal) {
    source = { ...source, kind: source?.kind ?? source?.type,
      tag: source?.tag ?? source?.value, handle: source?.handle ?? source?.author ?? source?.value,
      url: source?.url ?? source?.value };
    if (cursor != null && (typeof cursor !== 'string' || cursor.length > 8192)) throw new XClientError('invalid_source', '無效的分頁位置。');
    let data;
    if (source?.kind === 'search') {
      const tag = String(source.tag || '').replace(/^[#＃]/, '').normalize('NFC');
      if (!/^[\p{L}\p{M}\p{N}_]+$/u.test(tag)) throw new XClientError('invalid_source', '無效的 Hashtag。');
      let rawQuery = `#${tag} filter:media -filter:retweets`;
      if (source.since) {
        const date = String(source.since).slice(0, 10);
        const timestamp = Date.parse(date);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 10) !== date) throw new XClientError('invalid_source', '無效的開始日期。');
        // X's date search is only a coarse bound. Include the preceding UTC day
        // so Taiwan midnight (previous day 16:00 UTC) is never excluded here.
        // The engine applies the exact Taiwan calendar-date gate to createdAt.
        rawQuery += ` since:${new Date(timestamp - 86400000).toISOString().slice(0, 10)}`;
      }
      data = await this._graphql('SearchTimeline', { rawQuery, count: 20, querySource: 'typed_query', product: 'Latest', withGrokTranslatedBio: false, ...(cursor ? { cursor } : {}) }, signal);
    } else if (source?.kind === 'author') {
      const handle = String(source.handle || '').replace(/^@/, '');
      if (!/^[A-Za-z0-9_]{1,15}$/.test(handle)) throw new XClientError('invalid_source', '無效的作者帳號。');
      const cacheKey = handle.toLowerCase();
      let id = this.userIds.get(cacheKey);
      if (!id) {
        const result = await this._graphql('UserByScreenName', { screen_name: handle, withGrokTranslatedBio: false }, signal);
        id = String(result?.data?.user?.result?.rest_id || '');
        if (!/^\d+$/.test(id)) throw new XClientError('unavailable', '無法讀取此作者，可能已刪除、更名或無權存取。');
        this.userIds.set(cacheKey, id);
      }
      data = await this._graphql('UserMedia', { userId: id, count: 50, includePromotedContent: false, withClientEventToken: false, withBirdwatchNotes: false, withVoice: true, ...(cursor ? { cursor } : {}) }, signal);
    } else if (source?.kind === 'post') {
      let id = /^\d+$/.test(String(source.id || '')) ? String(source.id) : null;
      if (!id && source.url) {
        let url;
        try { url = new URL(source.url); } catch { /* validation below */ }
        if (url?.protocol === 'https:' && ['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(url.hostname) && !url.username && !url.password && !url.port) id = url.pathname.match(/^\/(?:[A-Za-z0-9_]{1,15}\/status|i\/(?:web\/)?status)\/(\d+)(?:\/.*)?$/)?.[1];
      }
      if (!/^\d+$/.test(id || '')) throw new XClientError('invalid_source', '無效的 X 貼文網址或 ID。');
      if (cursor) return { posts: [], cursor: null, done: true, warnings: [] };
      data = await this._graphql('TweetResultByRestId', { tweetId: id, withCommunity: false, includePromotedContent: false, withVoice: false }, signal);
      const post = normalizeTweet(data?.data?.tweetResult?.result);
      if (!post || post.id !== id) throw new XClientError('unavailable', '此貼文無法讀取，可能已刪除、受保護或帳號無權存取。');
      return { posts: [post], cursor: null, done: true, warnings: [] };
    } else throw new XClientError('invalid_source', '未知的資料來源。');
    const result = parseTimeline(data, cursor);
    if (source.kind === 'author') result.posts = result.posts.filter(post => post.author.toLowerCase() === source.handle.replace(/^@/, '').toLowerCase());
    return result;
  }
}
