"""FanArt collection: independent extraction, exact hashtag gates, durable state.

The gallery client invokes an independent URL-to-JSONL helper. No gallery-dl
code, browser DOM, scroll count or screen position enters this download engine.
"""
from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
import hashlib
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
import unicodedata
from urllib.parse import quote, urljoin, urlparse

import requests
from PIL import Image

from storage import Store
from gallery_client import Cancelled, GallerySource, GallerySourceError
from network import check_proxy_available, configure_session, warp_proxy_url
from thread_collection import (MAX_DEFERRED_MEDIA, context_date, eligible_reply, post_context,
                               reply_relation, same_author, same_context)
from collection_jobs import JobLedger, job_fingerprint, search_sources, source_spec

# 共用安全界線：日期統一換算台灣時間，媒體僅接受指定的 X CDN。
# Shared boundaries: use Taiwan dates and allow only the designated X media CDNs.
TAIPEI = timezone(timedelta(hours=8))
MEDIA_HOSTS = frozenset({'pbs.twimg.com', 'video.twimg.com'})
MEDIA_TIMEOUT = (15, 45)
MAX_MEDIA_BYTES = 1024 * 1024 * 1024
MAX_MEDIA_REDIRECTS = 5
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_POST_PATH = re.compile(r"/(?:[A-Za-z0-9_]{1,15}/status|i/(?:web/)?status)/(\d+)(?:/.*)?\Z")


class LimitReached(Exception):
    """End the current tag search without stopping the author's media stage."""


def _validated_media_url(value: str) -> str:
    """Check every hop before a request; never follow an off-site redirect."""
    # 首次請求與每次重新導向都檢查，避免下載途中被帶往其他網站。
    # Validate the initial URL and every redirect to prevent off-site requests.
    try:
        parsed = urlparse(value)
        if (len(value) > 8192 or parsed.scheme != 'https' or parsed.hostname not in MEDIA_HOSTS
                or parsed.username or parsed.password or parsed.port or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('媒體網址必須是 X 圖片或影片主機的 HTTPS 連結。') from None
    return value


@contextmanager
def _media_response(session, url: str):
    """Allow a few CDN redirects while validating TLS and host at every hop."""
    current = _validated_media_url(url)
    # 關閉 requests 自動跟隨，先驗證下一站再發出請求。
    # Disable automatic redirects so the next destination is checked before contact.
    for _ in range(MAX_MEDIA_REDIRECTS + 1):
        with session.get(current, stream=True, timeout=MEDIA_TIMEOUT, allow_redirects=False,
                         headers={'Referer': 'https://x.com/', 'Accept-Encoding': 'identity'}) as response:
            _validated_media_url(response.url)
            if getattr(response, 'status_code', 200) in {301, 302, 303, 307, 308}:
                location = response.headers.get('Location')
                if not isinstance(location, str) or not location:
                    raise ValueError('媒體重新導向缺少有效網址。')
                current = _validated_media_url(urljoin(current, location))
                continue
            response.raise_for_status()
            yield response
            return
    raise ValueError('媒體重新導向次數過多，已停止此檔案。')


def _tag_character(char: str) -> bool:
    return unicodedata.category(char)[0] in "LMN" or char == "_"


# 輸入與來源資料使用相同正規化；完整標籤比對不接受前綴或搜尋運算子。
# Normalize inputs and source tags consistently; match whole tags, not prefixes or query syntax.
def normalize_hashtag(value: str) -> str:
    value = unicodedata.normalize("NFC", value.strip())
    if value.startswith(("#", "＃")):
        value = value[1:]
    if not value or not all(_tag_character(c) for c in value):
        raise ValueError("請輸入一個完整 hashtag，例如 #角色名稱；不要加入空白或搜尋運算子。")
    return value.casefold()


def parse_hashtags(value: str | Iterable[str]) -> tuple[str, ...]:
    """Parse exact OR targets, preserving first occurrence and rejecting queries.

    Spaces, newlines and Western/Chinese commas or semicolons separate tags.
    Explicit ``#OR`` remains a valid tag; a bare search operator is not a tag.
    """
    values = (value,) if isinstance(value, str) else value
    targets: dict[str, None] = {}
    for group in values:
        if not isinstance(group, str):
            raise ValueError("Hashtag 必須是文字，請以空白、逗號或換行分隔。")
        for token in re.split(r"[\s,，;；]+", group.strip()):
            if not token:
                continue
            if token.upper() in {"OR", "AND", "NOT"}:
                raise ValueError(f"請移除搜尋運算子「{token}」；多個 Hashtag 以空白、逗號或換行分隔，例如 #角色名稱 #作品標籤。")
            try:
                normalized = normalize_hashtag(token)
            except ValueError as error:
                raise ValueError(
                    f"Hashtag「{token}」格式不正確；每個標籤僅可包含文字、數字或底線，"
                    "並以空白、逗號或換行分隔，例如 #角色名稱 #作品標籤。"
                ) from error
            targets[normalized] = None
    if not targets:
        raise ValueError("請至少輸入一個完整 Hashtag，例如 #角色名稱。")
    return tuple(targets)


def metadata_hashtags(metadata: dict) -> list[str]:
    """Use the post's own entities; never classify image alt text or quotes."""
    raw = metadata.get("hashtags")
    # 明確回傳空標籤清單就代表沒有標籤，不借用替代文字或引用貼文。
    # An explicit empty tag list stays empty; never borrow alt text or quoted-post tags.
    if raw is not None:
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        result = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("text", value.get("tag", ""))
            try:
                result.append(normalize_hashtag(str(value)))
            except ValueError:
                continue
        return list(dict.fromkeys(result))
    # Older metadata may have no entities. Only tokenize actual post text.
    content = re.sub(r"https?://\S+", "", str(metadata.get("content") or ""))
    tags = []
    for index, char in enumerate(content):
        if char not in "#＃" or (index and (_tag_character(content[index - 1]) or content[index - 1] in "/#＃")):
            continue
        end = index + 1
        while end < len(content) and _tag_character(content[end]):
            end += 1
        if end > index + 1:
            tags.append(normalize_hashtag(content[index + 1:end]))
    return list(dict.fromkeys(tags))


def matches_hashtag(metadata: dict, target: str) -> bool:
    return normalize_hashtag(target) in metadata_hashtags(metadata)


# 將 @帳號、作者網址與貼文網址收斂成固定格式，方便去重與安全命名。
# Canonicalize handles and URLs for deduplication and safe filenames.
def normalize_author(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise ValueError("作者網址必須是 https://x.com/帳號/media。")
        parts = parsed.path.strip("/").split("/")
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "media"):
            raise ValueError("請輸入作者帳號或作者媒體頁網址。")
        value = parts[0]
    value = value.lstrip("@").lower()
    if not _HANDLE.fullmatch(value) or value in {"home", "search", "explore", "settings", "i", "hashtag"}:
        raise ValueError("作者帳號只能包含 1–15 個英文字母、數字或底線。")
    return value


def normalize_tweet_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} or not _POST_PATH.fullmatch(parsed.path):
        raise ValueError("請貼入完整的公開 X 貼文網址，例如 https://x.com/作者/status/貼文編號。")
    # Canonical lookup avoids tracking parameters and /photo/1 limiting media.
    post_id = _POST_PATH.fullmatch(parsed.path)[1]
    return f"https://x.com/i/web/status/{post_id}"


# 每次開始時固定一份選項；執行中的 UI 變動不改寫本輪條件。
# Freeze one options snapshot per run so later UI edits cannot alter active work.
@dataclass(frozen=True)
class DownloadOptions:
    hashtag: str
    output_dir: Path
    database_path: Path
    cookie_file: Path | None = None
    authors: tuple[str, ...] = ()
    tweet_urls: tuple[str, ...] = ()
    search_enabled: bool = True
    discover_authors: bool = True
    folder_by_user: bool = True
    limit_count: int | None = None  # Unique matching seeds from tag searches only.
    since: date | None = None
    delay: bool = True
    retry_failed: bool = True
    hashtags: tuple[str, ...] = ()
    warp_proxy: bool = False
    warp_proxy_port: int = 40000
    include_author_replies: bool = True
    date_window_search: bool = True
    resume: bool = True
    retry_media_ids: tuple[str, ...] = ()


@dataclass
class RunResult:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    filtered: int = 0
    posts: int = 0
    errors: list[str] = field(default_factory=list)
    stopped: bool = False
    search_posts: int = 0
    job_id: int | None = None
    resumed: bool = False
    search_seen: int = 0
    search_revisited: int = 0
    sources_started: int = 0
    sources_completed: int = 0
    sources_skipped: int = 0
    incomplete_sources: list[dict] = field(default_factory=list)
    pending_media: int = 0
    job_state: str = ""


def _post_date(metadata: dict) -> datetime | None:
    # X 時間戳通常是 UTC；起始日期須以台灣當地日期判斷。
    # X timestamps are normally UTC; compare the cutoff using Taiwan's local date.
    value = metadata.get("date")
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(TAIPEI)


def validate_file(path: Path, extension: str) -> tuple[int, str]:
    # 寫入成功不等於檔案完整：先驗證格式，再以大小與雜湊供重跑核對。
    # Verify the format first, then retain size and hash for checks on later runs.
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("檔案為空。")
    if extension.lower() in {"jpg", "jpeg", "png", "gif", "webp", "avif"}:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
    elif extension.lower() == "mp4":
        # Validate complete ISO BMFF top-level boxes; an ftyp header alone can
        # also be the beginning of a truncated download or an HTML disguise.
        boxes = set()
        with path.open("rb") as stream:
            offset = 0
            while offset < size:
                if size - offset < 8:
                    raise ValueError("MP4 結尾的媒體區塊不完整。")
                stream.seek(offset)
                header = stream.read(8)
                length = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                header_size = 8
                if length == 1:
                    length = int.from_bytes(stream.read(8), "big")
                    header_size = 16
                elif length == 0:
                    length = size - offset
                if length < header_size or offset + length > size:
                    raise ValueError("MP4 媒體區塊長度無效或傳輸中斷。")
                if kind in {b"mdat", b"moov"} and length <= header_size:
                    raise ValueError("MP4 缺少影像內容或時間軸資料。")
                boxes.add(kind)
                offset += length
        if not {b"ftyp", b"moov", b"mdat"}.issubset(boxes):
            raise ValueError("MP4 缺少完整的檔案類型、時間軸或媒體資料。")
    else:
        raise ValueError(f"不支援的媒體格式：{extension}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return size, digest.hexdigest()


class DownloadEngine:
    def __init__(self, options: DownloadOptions, log=print, stop_event=None, *, source=None, session=None, progress=None):
        self.options = options
        self.log = log
        self.progress = progress
        self.stop_event = stop_event or threading.Event()
        self.source = source
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.proxy_url = warp_proxy_url(options.warp_proxy, options.warp_proxy_port)
        configure_session(self.session, self.proxy_url)
        self.result = RunResult()
        # 這些集合只屬於本輪；跨輪紀錄交給 Store，重新開始仍能發現新作品。
        # These sets are run-local; Store preserves history while new runs discover new work.
        self._posts: set[str] = set()
        self._search_posts: set[str] = set()
        self._filtered: set[str] = set()
        self._attempted: dict[str, str] = {}
        self._authors: set[str] = set()
        self._failed_media: set[str] = set()
        self._retry_posts: set[str] = set()
        self._thread_done: set[str] = set()
        self._thread_posts: set[str] = set()
        self._thread_rate_limited = False
        self._source_rate_limited: set[str] = set()
        self._selected_keys = set(options.retry_media_ids)
        self.ledger = None
        self._search_revisited: set[str] = set()
        self._executed_source_keys: set[str] = set()
        self._refreshed_retry_posts: set[str] = set()

    def _failure(self, key: str, message: str):
        self.store.mark_failed(key, message)
        self._failed_media.add(key)
        self.result.failed = len(self._failed_media)

    def _media_progress(self, state: str, row: dict, metadata: dict, file_name: str, destination: Path) -> str:
        # 進度只負責顯示；成功後才附本機路徑，UI 回呼失敗不影響下載結果。
        # Progress is presentation-only: expose a local path after success and isolate UI errors.
        post = self.store.post_state(row["post_id"])
        handle = str(post["author"] or "").lower()
        if not _HANDLE.fullmatch(handle):
            handle = ""
        display_path = f"{handle}\\{file_name}" if self.options.folder_by_user and handle else file_name
        if self.progress is None:
            return display_path
        # Only the actual tweet author supplies public presentation metadata.
        author = metadata.get("author")
        author = author if isinstance(author, dict) else {}
        name = author.get("nick") or author.get("name")
        name = name if isinstance(name, str) else handle
        name = " ".join("".join(char for char in name if not unicodedata.category(char).startswith("C")).split())[:100]
        avatar = author.get("profile_image")
        avatar = avatar if isinstance(avatar, str) and len(avatar) <= 8192 else None
        event = {"type": "media", "state": state, "author_handle": handle,
                 "author_name": name or handle, "avatar_url": avatar,
                 "filename": file_name, "display_path": display_path,
                 "media_type": "video" if str(metadata.get("extension")).lower() == "mp4" else "image"}
        if state == "downloaded":
            event["path"] = str(destination.resolve())
        try:
            self.progress(event)
        except Exception:
            # Presentation callbacks must not change durable download outcomes.
            pass
        return display_path

    def _check_stop(self):
        if self.stop_event.is_set():
            raise Cancelled()

    def _gate(self, metadata: dict) -> bool:
        # 搜尋、作者頁、直接網址與一般重試，都必須通過同一標籤及日期條件。
        # Search, author pages, direct URLs, and normal retries share the same tag/date gate.
        if not self._target_tags.intersection(metadata_hashtags(metadata)):
            return False
        if self.options.since:
            post_date = _post_date(metadata)
            if post_date is None or post_date.date() < self.options.since:
                return False
        return True

    def _accept_post(self, post_id: str):
        if post_id not in self._posts:
            self._posts.add(post_id)
            self.result.posts += 1

    def _search_quota_full(self) -> bool:
        return (self.options.limit_count is not None
                and len(self._search_posts) >= self.options.limit_count)

    def _accept_search_post(self, post_id: str):
        # 配額只限制搜尋；作者補抓、直接網址、重試與已驗證留言仍可繼續。
        # Only search consumes this quota; author, direct, retry, and verified-reply work continues.
        # Search has its own quota, regardless of prior direct/retry discovery.
        if post_id not in self._search_posts:
            # 續跑仍驗證舊檔，但已計入此工作者不重扣新配額，才能前進到下一批。
            # Rescans still verify old files; prior job IDs do not spend the next batch's budget.
            if self.ledger and post_id in self.ledger.search_seen:
                self._search_revisited.add(post_id)
                self.result.search_revisited = len(self._search_revisited)
                return
            if self._search_quota_full():
                raise LimitReached()
            self._search_posts.add(post_id)
            self.result.search_posts = len(self._search_posts)
            if self.ledger:
                self.ledger.remember_search(post_id)
                self.result.search_seen = len(self.ledger.search_seen)

    def _saved_seed(self, post_id: str) -> dict | None:
        row = self.store.post_state(post_id)
        if row and self._gate({"hashtags": row["hashtags"], "date": row["created_at"]}):
            return row
        return None

    def _retry_seed(self, row: dict, *, strict_history: bool = False) -> str | None:
        # 無標籤留言的重試須重查資料庫證據，不能相信 metadata 自稱的串起點。
        # Tagless retries require stored evidence, not a root claimed by media metadata.
        if self._gate(row["metadata"]):
            if not strict_history:
                return str(row["post_id"])
            post = self.store.post_state(row["post_id"])
            try:
                context = post_context(row["metadata"])
            except ValueError:
                context = None
            if (post and post["matched"] and context and context["post_id"] == str(row["post_id"])
                    and context["author"] == post["author"]
                    and (not post["context"] or same_context(context, post["context"]))
                    and self._target_tags.intersection(post["hashtags"], metadata_hashtags(row["metadata"]))):
                return str(row["post_id"])
        if not self.options.include_author_replies and not self._selected_keys:
            return None
        try:
            candidate = post_context(row["metadata"])
        except ValueError:
            return None
        if candidate["post_id"] != str(row["post_id"]):
            return None
        for seed_id in self.store.thread_roots(row["post_id"]):
            seed = self._saved_seed(seed_id)
            evidence = self.store.thread_context(seed_id, row["post_id"])
            if (seed and evidence and same_context(candidate, evidence)
                    and eligible_reply(seed["context"], evidence,
                        lambda parent: self.store.thread_context(seed_id, parent))):
                return seed_id
        return None

    def _retry_targets(self) -> tuple[str, ...]:
        # 歷史單檔重試可留空輸入，但只能從既有合格貼文或已驗證起點取得標籤。
        # A selected retry may omit input tags, deriving scope only from qualified stored posts/roots.
        tags = []
        for key in self._selected_keys:
            row = self.store.media_state(key)
            if not row:
                continue
            post = self.store.post_state(row["post_id"])
            if post and post["matched"]:
                tags.extend(set(post["hashtags"]).intersection(metadata_hashtags(row["metadata"])))
            for seed_id in self.store.thread_roots(row["post_id"]):
                seed = self.store.post_state(seed_id)
                if seed and seed["matched"]:
                    tags.extend(seed["hashtags"])
        return parse_hashtags(tags) if tags else ()

    def _checkpoint(self, key: str | None, records: int, post_id: str):
        if self.ledger and key:
            progress = dict(self.ledger.sources[key].get("progress") or {})
            progress.update(records=records, last_post_id=post_id, rescan=True)
            self.ledger.state(key, "running", "來源執行中。",
                              progress)

    def _execute_source(self, spec: dict) -> str:
        # 已完成來源跨輪略過；同一輪已試過的來源不因其他階段再次立即重試。
        # Skip durable completions and avoid immediately retrying a source twice in one run.
        row = self.ledger.add(spec)
        if row["key"] in self._executed_source_keys:
            return row["state"]
        if row["state"] == "completed":
            self.result.sources_skipped += 1
            self._executed_source_keys.add(row["key"])
            return "completed"
        kind, key = row["kind"], row["key"]
        if kind == "retry" and not (self.options.retry_failed or self._selected_keys):
            return row["state"]
        if ((kind == "search" and self._search_quota_full()) or kind in self._source_rate_limited):
            return row["state"]
        if kind == "thread" and self._thread_rate_limited:
            self.ledger.state(key, "partial", "討論串請求頻率受限，可稍後續跑。")
            return "partial"
        self._check_stop()
        self._executed_source_keys.add(key)
        self.result.sources_started += 1
        progress = dict(row.get("progress") or {})
        progress.pop("quota_exhausted", None)
        progress.update(records=0, rescan=True)
        self.ledger.state(key, "running", "來源執行中。", progress)
        try:
            data = row.get("data") or {}
            if kind == "thread":
                state = self._collect_thread(row["url"], row["name"], str(data["seed_id"]),
                    known=bool(data.get("known")) or str(data["seed_id"]) in self._retry_posts,
                    ledger_managed=True, source_key=key)
            elif kind == "retry":
                # 選取重試只允許既有資格的指定鍵；更新網址也不擴散下載其他媒體。
                # Selected retries require stored qualification and never expand to other media keys.
                for media_key in data.get("media_keys", []):
                    self._check_stop()
                    media = self.store.media_state(media_key)
                    if not media or media["state"] == "downloaded":
                        continue
                    if self._retry_seed(media, strict_history=bool(self._selected_keys)):
                        self._accept_post(str(data["seed_id"]))
                        self._download(media)
                remaining = [self.store.media_state(media_key) for media_key in data.get("media_keys", [])]
                needs_refresh = any(item and item["state"] != "downloaded" for item in remaining)
                state = "completed"
                if (needs_refresh or not self._selected_keys) and str(data["seed_id"]) not in self._refreshed_retry_posts:
                    self._refreshed_retry_posts.add(str(data["seed_id"]))
                    state = self._collect(row["url"], row["name"], source_key=key)
                if any((self.store.media_state(media_key) or {}).get("state") in {"failed", "pending"}
                       for media_key in data.get("media_keys", [])):
                    state = "partial"
            else:
                state = self._collect(row["url"], row["name"], search=kind == "search",
                                      source_key=key, window=data)
            message = "來源列舉已完成。" if state == "completed" else "來源尚未完整完成，可稍後續跑。"
            self.ledger.state(key, state, message)
            return state
        except Cancelled:
            self.ledger.state(key, "stopped", "來源已停止；下次重新列舉並依紀錄去重。")
            raise
        except Exception:
            self.ledger.state(key, "partial", "來源尚未完整完成，可稍後續跑。")
            raise

    def _retry_plans(self) -> list[dict]:
        # 依可驗證的起點合併待重試媒體，避免每個檔案重複查詢同一貼文。
        # Group pending media by a verified seed to avoid repeated lookups for the same post.
        groups = {}
        rows = [self.store.media_state(key) for key in sorted(self._selected_keys)] if self._selected_keys else self.store.pending_media()
        if not (self.options.retry_failed or self._selected_keys):
            return []
        for row in rows:
            if not row or row["state"] == "downloaded":
                continue
            seed = self._retry_seed(row, strict_history=bool(self._selected_keys))
            if seed:
                groups.setdefault(seed, []).append(row["media_key"])
                self._retry_posts.add(seed)
            elif self._selected_keys:
                self.result.errors.append("選取的媒體缺少可驗證的標籤或討論串關聯，已保留紀錄並略過。")
        plans = []
        for seed, keys in groups.items():
            spec = source_spec("retry", f"https://x.com/i/web/status/{seed}", "補抓貼文更新", seed_id=seed, media_keys=keys)
            spec["key"] += ":" + hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:16]
            plans.append(spec)
        return plans

    def run(self) -> RunResult:
        # 每輪固定條件與路由；工作檢查點只存公開收集條件，不保存登入內容。
        # Freeze scope/routing per run; checkpoints contain public scope, not login material.
        if self.options.limit_count is not None and self.options.limit_count < 1:
            raise ValueError("搜尋貼文配額必須大於 0。")
        authors = {normalize_author(author) for author in self.options.authors} if not self._selected_keys else set()
        urls = list(dict.fromkeys(normalize_tweet_url(url) for url in self.options.tweet_urls)) if not self._selected_keys else []
        if self.proxy_url is not None:
            self._check_stop()
            check_proxy_available(self.proxy_url)
            self.log('本輪搜尋、作者補抓、同作者留言與媒體下載使用 WARP 本機代理；代理失敗不會切回直連。')
        if self.source is None:
            from login_browser import validate_cookies
            if not self.options.cookie_file or not self.options.cookie_file.is_file():
                raise ValueError("尚未儲存登入 Cookie。請先開啟瀏覽器登入 X，再儲存 Cookie 或匯入 cookies.txt。")
            validate_cookies(self.options.cookie_file)
            self.source = GallerySource(self.options.cookie_file, self.log, self.stop_event, proxy_url=self.proxy_url)
        self.options.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with Store(self.options.database_path) as self.store:
                raw_tags = self.options.hashtags or self.options.hashtag
                has_input_tags = bool(raw_tags.strip()) if isinstance(raw_tags, str) else bool(raw_tags)
                self.tags = self._retry_targets() if self._selected_keys and not has_input_tags else parse_hashtags(raw_tags)
                if not self.tags:
                    self.result.errors.append("選取的媒體缺少可驗證的標籤或討論串關聯，已保留紀錄並略過。")
                    return self.result
                self.tag, self._target_tags = self.tags[0], frozenset(self.tags)
                target_names = "、".join(f"#{tag}" for tag in self.tags)
                self.log(f"目標標籤：{target_names}；原貼文會逐篇核對，符合任一完整標籤才收集；同作者留言僅在明確開啟並通過串關聯驗證後例外補抓。")
                if self.options.include_author_replies and not self._selected_keys:
                    self.log('同作者留言補抓已開啟：僅從符合標籤的原貼文延伸，驗證同作者與回覆關聯；留言可不含標籤。日期以起點計算，搜尋配額不會截斷同串媒體。')
                retry_plans = self._retry_plans()
                if self.options.discover_authors and not self._selected_keys:
                    authors.update(self.store.authors())
                initial = list(retry_plans)
                retry_urls = {spec["url"] for spec in retry_plans}
                initial.extend(source_spec("direct", url, "貼文網址") for url in urls if url not in retry_urls)
                if self.options.search_enabled and not self._selected_keys:
                    initial.extend(search_sources(self.tags, self.options.since, self.options.date_window_search,
                                                  datetime.now(TAIPEI).date()))
                initial.extend(source_spec("author", f"https://x.com/{author}/media", f"作者媒體 @{author}", author=author)
                               for author in sorted(authors))
                public = {"tags": sorted(self.tags), "authors": sorted(normalize_author(item) for item in self.options.authors) if not self._selected_keys else [],
                          "urls": sorted(urls), "since": self.options.since.isoformat() if self.options.since else None,
                          "search": self.options.search_enabled and not self._selected_keys,
                          "discover_authors": self.options.discover_authors and not self._selected_keys,
                          "replies": self.options.include_author_replies, "folder_by_user": self.options.folder_by_user,
                          "windowed": self.options.date_window_search,
                          "selected": sorted(self._selected_keys)}
                # 比對收集範圍與目的地；配額、代理或登入更新不另開一份相同工作。
                # Match scope and destination; budget, proxy, and login changes can resume the same job.
                self.ledger = JobLedger(self.store, job_fingerprint(public, str(self.options.output_dir.resolve())),
                                        public, initial, self.options.resume and not self._selected_keys)
                self.result.job_id, self.result.resumed = self.ledger.job_id, self.ledger.resumed
                self.result.search_seen = len(self.ledger.search_seen)
                if self.ledger.resumed:
                    self.log(f"續跑工作 {self.ledger.job_id}：已完成來源略過；未完成來源重新列舉並依媒體紀錄去重，並非從分頁游標續傳。")
                for spec in retry_plans:
                    self.ledger.add(spec)
                for author in authors:
                    self.ledger.author(author)
                try:
                    # 續跑先處理未完成來源；歷史搜尋 ID 不再消耗本輪新貼文配額。
                    # Resume unfinished sources first; prior job search IDs do not consume the new budget.
                    for kind in ("retry", "direct", "thread", "search"):
                        for spec in list(self.ledger.sources.values()):
                            if spec["kind"] == kind:
                                self._check_stop()
                                self._execute_source(spec)
                    if self._search_quota_full():
                        self.log(f"已達本輪 {self.options.limit_count} 則搜尋貼文配額；接著繼續作者補抓，直接網址與重試也不受此配額限制。")
                    if not self._selected_keys:
                        if self.options.discover_authors:
                            authors.update(self._authors)
                            authors.update(self.store.job_authors(self.ledger.job_id))
                        for author in sorted(authors):
                            self.ledger.author(author)
                            self._execute_source(source_spec("author", f"https://x.com/{author}/media", f"作者媒體 @{author}", author=author))
                    if not self.ledger.sources and not self._attempted and not self.result.errors:
                        self.result.errors.append("沒有可執行的來源。請啟用標籤搜尋、輸入作者或貼入貼文網址。")
                except Cancelled:
                    self.result.stopped = True
                finally:
                    # 來源列舉與檔案完成分開統計；仍有待重試媒體時不宣稱工作全數完成。
                    # Source enumeration and file completion differ; pending media keeps a job partial.
                    self.result.stopped = self.result.stopped or self.stop_event.is_set()
                    self.result.pending_media = sum(bool(self._retry_seed(row, strict_history=bool(self._selected_keys)))
                        for row in self.store.pending_media() if not self._selected_keys or row["media_key"] in self._selected_keys)
                    unfinished = self.ledger.finish(self.result.stopped,
                        bool(self.result.errors or self.result.failed or self.result.pending_media))
                    self.result.incomplete_sources = [{key: row.get(key) for key in ("key", "name", "kind", "state", "message", "progress")}
                                                     for row in unfinished]
                    self.result.sources_completed = sum(row["state"] == "completed" for row in self.ledger.sources.values())
                    self.result.search_seen = len(self.ledger.search_seen)
                    self.result.job_state = self.store.get_job(self.ledger.job_id)["state"]
        except Cancelled:
            self.result.stopped = True
        finally:
            if self._owns_session:
                self.session.close()
        if self.result.stopped:
            self.log("已停止；尚未完成的媒體保留於資料庫，下次可重試。")
        self.log(f"本輪：{self.result.posts} 則符合貼文，下載 {self.result.downloaded}、已存在 {self.result.skipped}、失敗 {self.result.failed}、排除 {self.result.filtered} 則。")
        return self.result

    def _collect(self, url: str, source_name: str, *, search: bool = False, source_key: str | None = None,
                 window: dict | None = None) -> str:
        # 一般來源逐筆驗證；直接貼文可另外取得文字起點與討論串上下文。
        # Validate ordinary records individually; direct posts can also provide text-only thread roots.
        self._check_stop()
        if ((self.options.include_author_replies or self._selected_keys) and hasattr(self.source, "iter_thread")
                and "/status/" in url):
            seed_id = _POST_PATH.fullmatch(urlparse(url).path)[1]
            return self._collect_thread(url, source_name, seed_id, known=seed_id in self._retry_posts)
        self.log(f"開始來源：{source_name}")
        count = 0
        iterator = None
        try:
            iterator = self.source.iter_media(url)
            for media_url, metadata in iterator:
                self._check_stop()
                count += 1
                context = post_context(metadata)
                post_id, author = context["post_id"], context["author"]
                self._checkpoint(source_key, count, post_id)
                tags = metadata_hashtags(metadata)
                matched = self._gate(metadata)
                if matched and search and window and window.get("window_end"):
                    post_date = _post_date(metadata)
                    matched = bool(post_date and date.fromisoformat(window["window_start"]) <= post_date.date()
                                   < date.fromisoformat(window["window_end"]))
                if matched and search:
                    # 即使貼文先經直接網址或重試處理，首次出現在搜尋仍占一次配額。
                    # A post first seen through direct/retry work still counts once when search finds it.
                    self._accept_search_post(post_id)
                self.store.record_post(post_id, author, str(metadata.get("content") or ""), tags, source_name, matched, str(metadata.get("date") or ""), context=context)
                if not matched:
                    if post_id not in self._posts and post_id not in self._thread_posts and post_id not in self._filtered:
                        self._filtered.add(post_id)
                        self.result.filtered += 1
                    continue
                self._accept_post(post_id)
                if post_id in self._filtered:
                    self._filtered.remove(post_id)
                    self.result.filtered = len(self._filtered)
                self._authors.add(author)
                if self.ledger:
                    self.ledger.author(author)
                self._save_media(media_url, metadata, context)
                if self.options.include_author_replies and hasattr(self.source, "iter_thread"):
                    self._collect_thread(f"https://x.com/i/web/status/{post_id}",
                                         "同作者留言補抓", post_id, known=True)
            if count == 0:
                self.log(f"{source_name} 未返回媒體；這不代表 X 上沒有其他符合的作品。")
            return "completed"
        except Cancelled:
            raise
        except LimitReached:
            # 只結束這個搜尋來源；最後接受貼文的多媒體及留言已完整處理。
            # Close only this search generator. Its last accepted seed has
            # already yielded all media, and its thread was collected inline.
            if not search:
                raise
            if self.ledger and source_key:
                progress = dict(self.ledger.sources[source_key].get("progress") or {})
                progress["quota_exhausted"] = True
                self.ledger.state(source_key, "partial", "搜尋配額已用完；續跑會略過已計數貼文並接續收集。", progress)
            return "partial"
        except GallerySourceError as error:
            self._check_stop()
            if error.code == "rate_limit" and self.ledger and source_key:
                self._source_rate_limited.add(self.ledger.sources[source_key]["kind"])
            message = f"{source_name} 未完成：{error.public_reason}"
            self.result.errors.append(message)
            self.log(message)
            return "partial"
        except Exception as error:
            self._check_stop()
            message = f"{source_name} 未完成（{type(error).__name__}）；請檢查登入或網路後重試。"
            self.result.errors.append(message)
            self.log(message)
            return "partial"
        finally:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()

    def _save_media(self, media_url: str, metadata: dict, context: dict):
        # 同一媒體跨搜尋、作者頁與討論串，都使用同一個持久化識別鍵。
        # One persistent media key deduplicates the same item across search, author, and thread sources.
        post_id, author = context["post_id"], context["author"]
        num = int(metadata.get("num") or 1)
        if num < 1:
            raise ValueError("來源媒體序號無效。")
        key = f"twitter:{post_id}:{num}"
        if self._selected_keys and key not in self._selected_keys:
            return
        target = self.options.output_dir / author if self.options.folder_by_user else self.options.output_dir
        if self._selected_keys:
            # 歷史單檔重試沿用原目的地，網址更新不搬動使用者先前選好的位置。
            # Selected history retries keep their original destination even when refreshing URLs.
            previous = self.store.media_state(key)
            if previous:
                target = Path(previous["target_dir"])
        self.store.record_media(post_id, key, media_url, metadata, str(target))
        self._download(self.store.media_state(key))

    def _collect_thread(self, url: str, source_name: str, seed_id: str, *, known: bool,
                        ledger_managed: bool = False, source_key: str | None = None) -> str:
        # 只有已驗證起點能授權無標籤留言；一般來源不享有這個例外。
        # Only a verified seed authorizes tagless replies; ordinary sources retain the strict tag gate.
        if self.ledger and not ledger_managed:
            spec = source_spec("thread", url, source_name, seed_id=seed_id, known=known)
            row = self.ledger.add(spec)
            if known and row["state"] == "completed" and not (row.get("progress") or {}).get("seed_accepted"):
                # 未匹配的早期快照不能阻擋稍後真正符合標籤的起點。
                # An earlier untagged snapshot must not consume a later qualified thread lookup.
                self.ledger.state(row["key"], "queued", "已找到符合標籤的起點，重新驗證討論串。")
                self._executed_source_keys.discard(row["key"])
            return self._execute_source(spec)
        if self._thread_rate_limited:
            return "partial"
        if seed_id in self._thread_done:
            return "completed"
        self._check_stop()
        seed = self._saved_seed(seed_id) if known else None
        seed_context = seed["context"] if seed else None
        if seed_context:
            self._thread_done.add(seed_id)
        deferred = []
        # 暫存只處理先到的留言媒體；證據不足或超過上限時回報未完整，不猜關聯。
        # Bound out-of-order buffering; report missing evidence or overflow as partial, never guess.
        incomplete = False
        saw_seed = False
        seed_rejected = False
        count = 0

        def accept_reply(media_url, metadata, context):
            # 保留留言自己的真實標籤，另存已驗證關聯供之後重試使用。
            # Preserve the reply's real tags and store its verified relationship separately for retries.
            self.store.record_post(context["post_id"], context["author"],
                str(metadata.get("content") or ""), metadata_hashtags(metadata),
                "同作者留言補抓", self._gate(metadata), str(metadata.get("date") or ""), context=context)
            self.store.record_thread_context(seed_id, context)
            self.store.record_thread_member(seed_id, context["post_id"])
            self._thread_posts.add(context["post_id"])
            self._filtered.discard(context["post_id"])
            self.result.filtered = len(self._filtered)
            self._save_media(media_url, metadata, context)

        def drain():
            # 新祖先證據到達後再檢查暫存，避免先後順序造成錯認或無限等待。
            # Recheck deferred media when ancestor evidence arrives, without guessing or waiting forever.
            if not seed_context:
                return
            remaining = []
            for candidate_url, candidate_metadata, candidate_context in deferred:
                self._check_stop()
                if eligible_reply(seed_context, candidate_context,
                        lambda parent: self.store.thread_context(seed_id, parent)):
                    accept_reply(candidate_url, candidate_metadata, candidate_context)
                elif reply_relation(seed_context, candidate_context,
                        lambda parent: self.store.thread_context(seed_id, parent)) is None:
                    remaining.append((candidate_url, candidate_metadata, candidate_context))
            deferred[:] = remaining

        try:
            self.log(f"開始來源：{source_name}（貼文 {seed_id} 與同作者留言）")
            for media_url, metadata in self.source.iter_thread(url):
                self._check_stop()
                count += 1
                try:
                    context = post_context(metadata)
                except ValueError:
                    incomplete = True
                    continue
                self._checkpoint(source_key, count, context["post_id"])
                if context["post_id"] == seed_id:
                    # 新起點先核對標籤；既有重試只能沿用 Store 保存的已驗證起點。
                    # Gate new seeds by tag; retries may reuse only a previously verified Store snapshot.
                    saw_seed = True
                    matched = self._gate(metadata)
                    if not seed_context and not matched:
                        # An unrelated or untagged URL cannot authorize replies.
                        self._filtered.add(seed_id)
                        self.result.filtered = len(self._filtered)
                        seed_rejected = True
                        deferred.clear()
                        continue
                    if seed_context and not same_author(seed_context, context):
                        raise ValueError("討論串起點作者與已驗證紀錄不一致，已停止補抓。")
                    self._accept_post(seed_id)
                    self._thread_done.add(seed_id)
                    if self.ledger and source_key:
                        progress = dict(self.ledger.sources[source_key].get("progress") or {})
                        progress["seed_accepted"] = True
                        self.ledger.state(source_key, "running", "來源執行中。", progress)
                    seed_context = context
                    seed_rejected = False
                    self._authors.add(context["author"])
                    if self.ledger:
                        self.ledger.author(context["author"])
                    self._filtered.discard(seed_id)
                    self.result.filtered = len(self._filtered)
                    self.store.record_post(seed_id, context["author"], str(metadata.get("content") or ""),
                        metadata_hashtags(metadata), source_name, matched,
                        str(metadata.get("date") or ""), context=context)
                    self.store.record_thread_context(seed_id, context)
                    if media_url:
                        self._save_media(media_url, metadata, context)
                    drain()
                    continue
                if seed_rejected:
                    continue
                if seed_context:
                    self.store.record_thread_context(seed_id, context)
                    drain()
                if not media_url:
                    continue
                if (seed_context and eligible_reply(seed_context, context,
                        lambda parent: self.store.thread_context(seed_id, parent))):
                    accept_reply(media_url, metadata, context)
                elif (not seed_context or (same_author(seed_context, context)
                        and reply_relation(seed_context, context,
                            lambda parent: self.store.thread_context(seed_id, parent)) is None)):
                    if len(deferred) < MAX_DEFERRED_MEDIA:
                        deferred.append((media_url, metadata, context))
                    else:
                        incomplete = True
                elif (same_author(seed_context, context)
                        and reply_relation(seed_context, context,
                            lambda parent: self.store.thread_context(seed_id, parent)) is True):
                    # Related media with missing or contradictory timestamps
                    # cannot silently turn an unverified extraction into success.
                    incomplete = True
            drain()
            if not saw_seed:
                incomplete = True
            if seed_context and (not seed_context.get("conversation_id") or context_date(seed_context) is None):
                incomplete = True
            if deferred or incomplete:
                message = '同作者留言補抓未完整：部分貼文缺少可驗證的回覆關聯或超過暫存上限，已安全略過；可稍後重試。'
                self.result.errors.append(message)
                self.log(message)
                return "partial"
            return "completed"
        except (Cancelled, LimitReached):
            raise
        except GallerySourceError as error:
            # 只顯示固定公開原因；429 暫停本輪留言請求，不阻擋搜尋與作者媒體。
            # Show fixed public reasons; a 429 pauses this run's threads, not search or author media.
            self._check_stop()
            if error.code == "rate_limit":
                self._thread_rate_limited = True
                message = 'X 已限制討論串請求頻率；本輪暫停後續同作者留言補抓，搜尋與作者媒體仍會繼續。部分留言尚未完成，請稍後補抓。'
            else:
                message = f'同作者留言補抓未完成：{error.public_reason}'
            self.result.errors.append(message)
            self.log(message)
            return "partial"
        except Exception as error:
            self._check_stop()
            # Do not expose upstream URLs, account headers or cookie payloads.
            message = f'同作者留言補抓未完成（{type(error).__name__}）；請檢查登入或網路後重試。'
            self.result.errors.append(message)
            self.log(message)
            return "partial"

    def _download(self, row: dict):
        # 同輪同鍵同網址只嘗試一次；更新網址仍可重試，跨輪則重新驗證既有檔案。
        # Attempt each key/URL once per run; refreshed URLs can retry and later runs revalidate files.
        self._check_stop()
        key, media_url, metadata = row["media_key"], row["url"], row["metadata"]
        if self._attempted.get(key) == media_url:
            return
        self._attempted[key] = media_url
        extension = str(metadata.get("extension") or "").lower()
        if not re.fullmatch(r"[a-z0-9]{2,5}", extension):
            self._failure(key, "來源缺少可識別的媒體副檔名。")
            return
        if row["state"] == "downloaded":
            # 紀錄存在仍須驗檔；檔案遺失或損壞才重新下載。
            # Revalidate recorded files; redownload only when they are missing or invalid.
            try:
                old_path = Path(row["path"])
                size, digest = validate_file(old_path, extension)
                if size == row["size"] and digest == row["sha256"]:
                    self.result.skipped += 1
                    return
                raise ValueError("檔案內容與下載紀錄不同。")
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                self.store.mark_failed(key, "原檔案遺失或驗證失敗，重新下載。")
        try:
            _validated_media_url(media_url)
        except ValueError:
            self._failure(key, "來源不是支援的 X 媒體直連。")
            return
        target_dir = Path(row["target_dir"])
        target_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{row['post_id']}_{int(metadata.get('num') or 1)}.{extension}"
        destination = target_dir / file_name
        self._media_progress("downloading", row, metadata, file_name, destination)
        for attempt in range(3):
            # 每次先寫同目錄暫存檔；限制大小、驗證內容後才原子替換正式檔案。
            # Stream to a local temporary file, enforce size limits, then verify and atomically replace.
            temporary = None
            try:
                self._check_stop()
                with _media_response(self.session, media_url) as response:
                    expected = response.headers.get("Content-Length")
                    if expected and (not expected.isdigit() or int(expected) > MAX_MEDIA_BYTES):
                        raise ValueError('媒體檔案大小無效或超過 1 GiB 安全限制。')
                    written = 0
                    with tempfile.NamedTemporaryFile(dir=target_dir, prefix="." + file_name + ".", suffix=".part", delete=False) as stream:
                        temporary = Path(stream.name)
                        for chunk in response.iter_content(128 * 1024):
                            self._check_stop()
                            if chunk:
                                written += len(chunk)
                                if written > MAX_MEDIA_BYTES:
                                    raise ValueError('媒體檔案超過 1 GiB 安全限制。')
                                stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if expected and written != int(expected):
                        raise ValueError("媒體傳輸不完整，稍後重試。")
                size, digest = validate_file(temporary, extension)
                self._check_stop()
                os.replace(temporary, destination)
                temporary = None
                self.store.mark_success(key, str(destination.resolve()), size, digest)
                # 完成紀錄先落地，再通知 UI 顯示預覽，避免預覽尚未驗證的檔案。
                # Persist completion before notifying the UI, so previews only use verified files.
                self._failed_media.discard(key)
                self.result.failed = len(self._failed_media)
                self.result.downloaded += 1
                icon = "🎬" if extension == "mp4" else "🖼️"
                display_path = self._media_progress("downloaded", row, metadata, file_name, destination)
                self.log(f"{icon} 已下載：{display_path}")
                if self.options.delay and self.stop_event.wait(random.uniform(0.5, 1.5)):
                    raise Cancelled()
                return
            except Cancelled:
                raise
            except Exception as error:
                self.store.mark_failed(key, f"{type(error).__name__}: {str(error)[:400]}")
                if attempt == 2:
                    self._failed_media.add(key)
                    self.result.failed = len(self._failed_media)
                    self.log(f"下載失敗，已保留補抓紀錄：{file_name}（{type(error).__name__}）")
                elif self.stop_event.wait(1 + attempt):
                    raise Cancelled()
            finally:
                # 失敗或停止都清掉本次暫存檔，保留資料庫的可重試狀態。
                # Remove this attempt's temporary file on failure or stop, while preserving retry state.
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
