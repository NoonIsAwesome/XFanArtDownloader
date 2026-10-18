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


def _post_date(metadata: dict) -> datetime | None:
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

    def _failure(self, key: str, message: str):
        self.store.mark_failed(key, message)
        self._failed_media.add(key)
        self.result.failed = len(self._failed_media)

    def _media_progress(self, state: str, row: dict, metadata: dict, file_name: str, destination: Path) -> str:
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
        # Search has its own quota, regardless of prior direct/retry discovery.
        if post_id not in self._search_posts:
            if self._search_quota_full():
                raise LimitReached()
            self._search_posts.add(post_id)
            self.result.search_posts = len(self._search_posts)

    def _saved_seed(self, post_id: str) -> dict | None:
        row = self.store.post_state(post_id)
        if row and self._gate({"hashtags": row["hashtags"], "date": row["created_at"]}):
            return row
        return None

    def _retry_seed(self, row: dict) -> str | None:
        if self._gate(row["metadata"]):
            return str(row["post_id"])
        if not self.options.include_author_replies:
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

    def run(self) -> RunResult:
        self.tags = parse_hashtags(self.options.hashtags or self.options.hashtag)
        self.tag = self.tags[0]  # Keep the original single-target attribute usable.
        self._target_tags = frozenset(self.tags)
        if self.options.limit_count is not None and self.options.limit_count < 1:
            raise ValueError("搜尋貼文配額必須大於 0。")
        authors = {normalize_author(author) for author in self.options.authors}
        urls = list(dict.fromkeys(normalize_tweet_url(url) for url in self.options.tweet_urls))
        if self.proxy_url is not None:
            self._check_stop()
            check_proxy_available(self.proxy_url)
            self.log('本輪搜尋、作者補抓、同作者留言與媒體下載使用 WARP 本機代理；代理失敗不會切回直連。')
        if self.source is None:
            from login_browser import validate_cookies
            if not self.options.cookie_file or not self.options.cookie_file.is_file():
                raise ValueError("尚未儲存登入 Cookie。請先開啟瀏覽器登入 X，再儲存 Cookie 或匯入 cookies.txt。")
            validate_cookies(self.options.cookie_file)
            self.source = GallerySource(self.options.cookie_file, self.log, self.stop_event,
                                        proxy_url=self.proxy_url)
        self.options.output_dir.mkdir(parents=True, exist_ok=True)
        target_names = "、".join(f"#{tag}" for tag in self.tags)
        self.log(f"目標標籤：{target_names}；原貼文會逐篇核對，符合任一完整標籤才收集；同作者留言僅在明確開啟並通過串關聯驗證後例外補抓。")
        if self.options.include_author_replies:
            self.log('同作者留言補抓已開啟：僅從符合標籤的原貼文延伸，驗證同作者與回覆關聯；留言可不含標籤。日期以起點計算，搜尋配額不會截斷同串媒體。')
        try:
            with Store(self.options.database_path) as self.store:
                if self.options.retry_failed:
                    for row in self.store.pending_media():
                        self._check_stop()
                        seed_id = self._retry_seed(row)
                        if seed_id:
                            self._accept_post(seed_id)
                            self._retry_posts.add(seed_id)
                            self._download(row)
                if self.options.discover_authors:
                    authors.update(self.store.authors())
                # Refresh expired URLs and every image of interrupted posts.
                # Direct/retry/author sources never consume the search quota.
                plans = [(f"https://x.com/i/web/status/{post_id}", "補抓貼文更新", False) for post_id in sorted(self._retry_posts)]
                plans.extend((url, "貼文網址", False) for url in urls if url not in {item[0] for item in plans})
                if self.options.search_enabled:
                    for tag in self.tags:
                        query = f"#{tag} filter:media"
                        if self.options.since:
                            # One day of overlap; the local Taiwan-date gate is authoritative.
                            query += " since:" + (self.options.since - timedelta(days=1)).isoformat()
                        plans.append(("https://x.com/search?q=" + quote(query, safe="") + "&f=live", f"標籤搜尋 #{tag}", True))
                for url, source_name, search in plans:
                    self._check_stop()
                    if search and self._search_quota_full():
                        continue
                    self._collect(url, source_name, search=search)
                if self._search_quota_full():
                    self.log(f"已達本輪 {self.options.limit_count} 則搜尋貼文配額；接著繼續作者補抓，直接網址與重試也不受此配額限制。")
                if self.options.discover_authors:
                    authors.update(self._authors)
                for author in sorted(authors):
                    self._check_stop()
                    self._collect(f"https://x.com/{author}/media", f"作者媒體 @{author}")
                if not plans and not authors and not self._attempted:
                    self.result.errors.append("沒有可執行的來源。請啟用標籤搜尋、輸入作者或貼入貼文網址。")
        except Cancelled:
            self.result.stopped = True
            self.log("已停止；尚未完成的媒體保留於資料庫，下次可重試。")
        finally:
            if self._owns_session:
                self.session.close()
        self.log(f"本輪：{self.result.posts} 則符合貼文，下載 {self.result.downloaded}、已存在 {self.result.skipped}、失敗 {self.result.failed}、排除 {self.result.filtered} 則。")
        return self.result

    def _collect(self, url: str, source_name: str, *, search: bool = False):
        self._check_stop()
        if (self.options.include_author_replies and hasattr(self.source, "iter_thread")
                and "/status/" in url):
            seed_id = _POST_PATH.fullmatch(urlparse(url).path)[1]
            self._collect_thread(url, source_name, seed_id, known=seed_id in self._retry_posts)
            return
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
                tags = metadata_hashtags(metadata)
                matched = self._gate(metadata)
                if matched and search:
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
                self._save_media(media_url, metadata, context)
                if self.options.include_author_replies and hasattr(self.source, "iter_thread"):
                    self._collect_thread(f"https://x.com/i/web/status/{post_id}",
                                         "同作者留言補抓", post_id, known=True)
            if count == 0:
                self.log(f"{source_name} 未返回媒體；這不代表 X 上沒有其他符合的作品。")
        except Cancelled:
            raise
        except LimitReached:
            # Close only this search generator. Its last accepted seed has
            # already yielded all media, and its thread was collected inline.
            if not search:
                raise
        except Exception as error:
            self._check_stop()
            message = f"{source_name} 未完成：{type(error).__name__}: {str(error)[:400]}"
            self.result.errors.append(message)
            self.log(message)
        finally:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()

    def _save_media(self, media_url: str, metadata: dict, context: dict):
        post_id, author = context["post_id"], context["author"]
        num = int(metadata.get("num") or 1)
        if num < 1:
            raise ValueError("來源媒體序號無效。")
        key = f"twitter:{post_id}:{num}"
        target = self.options.output_dir / author if self.options.folder_by_user else self.options.output_dir
        self.store.record_media(post_id, key, media_url, metadata, str(target))
        self._download(self.store.media_state(key))

    def _collect_thread(self, url: str, source_name: str, seed_id: str, *, known: bool):
        if self._thread_rate_limited or seed_id in self._thread_done:
            return
        self._check_stop()
        seed = self._saved_seed(seed_id) if known else None
        seed_context = seed["context"] if seed else None
        if seed_context:
            self._thread_done.add(seed_id)
        deferred = []
        incomplete = False
        saw_seed = False
        seed_rejected = False

        def accept_reply(media_url, metadata, context):
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
                try:
                    context = post_context(metadata)
                except ValueError:
                    incomplete = True
                    continue
                if context["post_id"] == seed_id:
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
                    seed_context = context
                    seed_rejected = False
                    self._authors.add(context["author"])
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
        except (Cancelled, LimitReached):
            raise
        except GallerySourceError as error:
            self._check_stop()
            if error.code == "rate_limit":
                self._thread_rate_limited = True
                message = 'X 已限制討論串請求頻率；本輪暫停後續同作者留言補抓，搜尋與作者媒體仍會繼續。部分留言尚未完成，請稍後補抓。'
            else:
                message = f'同作者留言補抓未完成：{error.public_reason}'
            self.result.errors.append(message)
            self.log(message)
        except Exception as error:
            self._check_stop()
            # Do not expose upstream URLs, account headers or cookie payloads.
            message = f'同作者留言補抓未完成（{type(error).__name__}）；請檢查登入或網路後重試。'
            self.result.errors.append(message)
            self.log(message)

    def _download(self, row: dict):
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
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
