# SPDX-License-Identifier: GPL-2.0-only
"""Standalone gallery-dl URL extractor with a bounded JSONL stdout interface.

This executable is independent of the Qt application. It accepts a public X
URL and a local Netscape-cookie *path*, and emits metadata/post/log/error/done.
It never imports the GUI, its download engine, or its client adapter.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import logging
import math
from pathlib import Path
import re
import signal
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

MAX_LINE_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 750_000
_GALLERY_LOCK = threading.Lock()
_SECRET_KEY = re.compile(r"cookie|header|authorization|password|passwd|secret|token|csrf|session|credential", re.I)
_REQUEST_HOSTS = frozenset({'x.com', 'www.x.com', 'api.x.com', 'twitter.com',
                            'www.twitter.com', 'mobile.twitter.com', 'api.twitter.com', 'abs.twimg.com'})


class Cancelled(Exception):
    pass


def validate_source_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("invalid source")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
                or parsed.username or parsed.password or parsed.port or parsed.fragment):
            raise ValueError("invalid source")
        valid = (parsed.path == "/search" and bool(parse_qs(parsed.query).get("q"))) or bool(re.fullmatch(
            r"/(?:[A-Za-z0-9_]{1,15}/media|(?:[A-Za-z0-9_]{1,15}/status|i/(?:web/)?status)/\d+)/?", parsed.path))
        if not valid:
            raise ValueError("invalid source")
    except (TypeError, ValueError):
        raise ValueError("僅接受 HTTPS 的 X 標籤搜尋、作者媒體頁或貼文網址。") from None
    return url


def validate_thread_url(url: str) -> str:
    validate_source_url(url)
    if not re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}/status|i/(?:web/)?status)/\d+/?", urlsplit(url).path):
        raise ValueError("conversation requires a tweet URL")
    return url


def valid_media_url(url) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {"pbs.twimg.com", "video.twimg.com"}
                and not parsed.username and not parsed.password and not parsed.port and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def validate_proxy_url(value: str) -> str:
    # Validate this standalone CLI's input independently. Importing the GUI's
    # network module would cross the separate-program/licensing boundary.
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'socks5h' or parsed.hostname != '127.0.0.1' or not port
                or parsed.username or parsed.password or parsed.netloc != f'127.0.0.1:{port}'
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('invalid local proxy') from None
    return value


def validate_request_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in _REQUEST_HOSTS
                or parsed.username or parsed.password or parsed.port or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('unsupported request URL') from None
    return value


def _json_metadata(metadata: dict) -> dict:
    """Keep public metadata only; unsupported/private values never use repr()."""
    budget = [MAX_METADATA_BYTES]

    def clean(value, depth=0):
        if budget[0] <= 0 or depth > 10:
            return None
        if value is None or isinstance(value, (bool, int)):
            budget[0] -= 16
            return value
        if isinstance(value, float):
            budget[0] -= 16
            return value if math.isfinite(value) else None
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        if isinstance(value, str):
            encoded = value[:65536].encode("utf-8")[:max(0, budget[0])]
            budget[0] -= len(encoded)
            return encoded.decode("utf-8", errors="ignore")
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:128]:
                if budget[0] <= 0:
                    break
                if not isinstance(key, str) or key.startswith("_") or _SECRET_KEY.search(key):
                    continue
                key = key[:128]
                budget[0] -= len(key.encode("utf-8")) + 8
                result[key] = clean(item, depth + 1)
            return result
        if isinstance(value, (tuple, list, set)):
            result = []
            for item in list(value)[:256]:
                if budget[0] <= 0:
                    break
                result.append(clean(item, depth + 1))
            return result
        return None

    return clean(metadata) if isinstance(metadata, dict) else {}


ERROR_REASONS = {
    "source": "X 擷取未完整完成；請檢查登入或網路後重試。",
    "cancelled": "擷取已停止。",
    "rate_limit": "X 限制請求頻率；此來源未完整完成，請稍後重試。",
    "login": "X 登入狀態無法使用；請重新登入並儲存後重試。",
    "not_found": "X 來源目前無法讀取或已不存在。",
    "file": "無法讀取本機登入檔案或啟動擷取工具；請檢查安裝與檔案權限。",
    "invalid_source": "來源或擷取資料格式無效；僅支援 X 搜尋、作者媒體頁與貼文。",
    "proxy": "無法使用本機代理；請檢查 WARP 的 Local proxy 與連接埠，此次未改用直連。",
    "redirect": "X 來源要求重新導向，為保護登入資料已停止；請確認登入或稍後重試。",
    "timeout": "擷取工具超過等待時間未回應；此來源未完整完成，請稍後重試。",
    "network": "X 連線未完成；請檢查網路或稍後重試。",
}


def error_code(error: Exception) -> str:
    # Never copy upstream exception/header text, even if it contains a cookie
    # value without an obvious field label. Classify, then return fixed copy.
    text = str(error).lower()
    name = type(error).__name__.lower()
    if isinstance(error, Cancelled):
        return "cancelled"
    if 'proxy' in name:
        return "proxy"
    if 'redirect refused' in text:
        return "redirect"
    if "429" in text or "rate" in name or "rate limit" in text:
        return "rate_limit"
    if any(token in name + " " + text for token in ("authentication", "authorization", "401", "403", "login", "cookie")):
        return "login"
    if "404" in text or "notfound" in name:
        return "not_found"
    if isinstance(error, (FileNotFoundError, PermissionError)):
        return "file"
    if "timeout" in name:
        return "timeout"
    if "connection" in name:
        return "network"
    if isinstance(error, ValueError):
        return "invalid_source"
    return "source"


def safe_error(error: Exception) -> str:
    return ERROR_REASONS[error_code(error)]


class _GalleryLog(logging.Handler):
    def __init__(self, callback):
        super().__init__(logging.WARNING)
        self.callback = callback

    def emit(self, record):
        # Classify upstream messages instead of forwarding possibly sensitive
        # request headers, exception data, local paths, or cookie values.
        self.callback("gallery-dl：" + safe_error(RuntimeError(record.getMessage())))


def get_extractor(url: str):
    validate_source_url(url)
    from gallery_dl.extractor.twitter import TwitterMediaExtractor, TwitterSearchExtractor, TwitterTweetExtractor
    for extractor_class in (TwitterMediaExtractor, TwitterSearchExtractor, TwitterTweetExtractor):
        instance = extractor_class.from_url(url)
        if instance is not None:
            return instance
    raise ValueError("unsupported source")


class GallerySource:
    """Public media and optional conversation context, confined to this process."""

    def __init__(self, cookie_file: Path, log, stop_event: threading.Event, *, proxy_url: str | None = None):
        self.cookie_file, self.log, self.stop_event = Path(cookie_file), log, stop_event
        self.proxy_url = validate_proxy_url(proxy_url) if proxy_url is not None else None

    def iter_media(self, url: str):
        yield from self._iterate(url, conversation=False)

    def iter_thread(self, url: str):
        """Yield every public post context and media; callers decide eligibility."""
        yield from self._iterate(url, conversation=True)

    def _iterate(self, url: str, *, conversation: bool):
        (validate_thread_url if conversation else validate_source_url)(url)
        from gallery_dl import config
        from gallery_dl.extractor.message import Message
        if self.stop_event.is_set():
            raise Cancelled()
        with _GALLERY_LOCK:
            previous = config._config.copy()
            handler = _GalleryLog(self.log)
            logger = logging.getLogger("twitter")
            propagate = logger.propagate
            logger.propagate = False
            logger.addHandler(handler)
            instance = None
            try:
                config._config.clear()
                config.set(("cache",), "file", str(self.cookie_file.parent / "gallery-cache.sqlite3"))
                # Allow slow X/CDN responses without retrying indefinitely.
                config.set(("extractor",), "timeout", (15, 45))
                config.set(("extractor",), "retries", 2)
                if self.proxy_url is not None:
                    config.set(('extractor',), 'proxy', self.proxy_url)
                    config.set(('extractor',), 'proxy-env', False)
                settings = {
                    "cookies": str(self.cookie_file), "transform": True, "size": ["orig"],
                    "videos": True, "retweets": False, "quoted": False, "cards": False,
                    "articles": False, "unique": True, "async": False, "ratelimit": "abort",
                    "retries-api": 2, "search-results": "latest", "search-pagination": "max_id", "search-stop": 5,
                }
                if conversation:
                    settings.update({"conversations": True, "text-tweets": True})
                for key, value in settings.items():
                    config.set(("extractor", "twitter"), key, value)
                instance = get_extractor(url)
                if instance is None or instance.category != "twitter":
                    raise ValueError("unsupported source")
                original_request = instance.request

                def checked_request(*args, **kwargs):
                    if self.stop_event.is_set():
                        raise Cancelled()
                    validate_request_url(args[0] if args else kwargs.get('url'))
                    # Requests strips Authorization on some redirects, but not
                    # X's custom CSRF header. Refuse redirects before any second
                    # request can send account headers to another destination.
                    kwargs['allow_redirects'] = False
                    response = original_request(*args, **kwargs)
                    if 300 <= getattr(response, 'status_code', 200) < 400:
                        response.close()
                        raise RuntimeError('redirect refused')
                    if self.stop_event.is_set():
                        raise Cancelled()
                    return response

                def interruptible_sleep(seconds, *args, **kwargs):
                    remaining = max(0.0, float(seconds))
                    while remaining > 0:
                        duration = min(remaining, 30.0)
                        if self.stop_event.wait(duration):
                            raise Cancelled()
                        remaining -= duration
                        if remaining > 0:
                            self.log("仍在等待 X 回應；可隨時停止。")

                def interruptible_wait(seconds=None, until=None, adjust=1.0, reason="rate limit"):
                    if seconds is None:
                        if isinstance(until, datetime):
                            until = until.timestamp()
                        seconds = float(until) - time.time()
                    self.log("X 要求暫候；可隨時停止。")
                    interruptible_sleep(float(seconds) + adjust)

                instance.request = checked_request
                instance.sleep = interruptible_sleep
                instance.wait = interruptible_wait
                for message, media_url, metadata in instance:
                    if self.stop_event.is_set():
                        raise Cancelled()
                    if message == Message.Url:
                        if not valid_media_url(media_url):
                            raise ValueError("external media URL")
                        yield media_url, _json_metadata(metadata)
                    elif conversation and message == Message.Directory:
                        # Directory metadata comes from the actual tweet, not
                        # the requested account or its surrounding timeline.
                        # Keep text-only contexts so the caller can trace reply
                        # ancestry without treating arbitrary replies as art.
                        context = _json_metadata(metadata)
                        context.setdefault("hashtags", [])
                        yield None, context
                    elif message == Message.Queue:
                        raise ValueError("external extractor queue")
                if instance.status:
                    raise RuntimeError("extractor incomplete")
            finally:
                if instance is not None and instance.session is not None:
                    instance.session.close()
                logger.removeHandler(handler)
                logger.propagate = propagate
                config._config.clear()
                config._config.update(previous)


def _emit(record):
    line = json.dumps(record, ensure_ascii=False, allow_nan=False)
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError("metadata line exceeds limit")
    print(line, flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Extract X media metadata as JSONL using gallery-dl.")
    parser.add_argument("--url", help="HTTPS X search, author media, or post URL")
    parser.add_argument("--cookies", type=Path, help="Local Netscape cookie-file path; never cookie values")
    parser.add_argument("--smoke-test", action="store_true", help="Construct supported extractors without network access")
    parser.add_argument('--proxy', help='Optional local WARP SOCKS5 endpoint; no credentials')
    parser.add_argument('--conversation', action='store_true', help='Emit all post contexts in a tweet conversation')
    args = parser.parse_args(argv)
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict", newline="\n", write_through=True)
    logging.getLogger().handlers[:] = [logging.NullHandler()]
    event = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), lambda signum, frame: event.set())
            except ValueError:
                pass  # Unit-test invocation may run outside the main thread.
    count = 0
    try:
        if args.conversation:
            validate_thread_url(args.url)
        if args.smoke_test:
            names = [type(get_extractor(url)).__name__ for url in (
                "https://x.com/search?q=%23sampleart&f=live", "https://x.com/artist/media", "https://x.com/artist/status/123",
            )]
            _emit({"type": "done", "ok": True, "count": 0, "extractors": names})
            return 0
        if not args.url or args.cookies is None:
            raise ValueError("missing URL or cookie path")
        validate_source_url(args.url)
        cookie_file = args.cookies.expanduser().resolve(strict=True)
        if not cookie_file.is_file() or cookie_file.stat().st_size > 1024 * 1024:
            raise ValueError("invalid cookie file")
        source = GallerySource(cookie_file, lambda value: _emit({"type": "log", "message": value}), event,
                               proxy_url=args.proxy)
        rows = source.iter_thread(args.url) if args.conversation else source.iter_media(args.url)
        for url, metadata in rows:
            if url is None:
                _emit({"type": "post", "metadata": metadata})
            else:
                _emit({"type": "metadata", "url": url, "metadata": metadata})
            count += 1
        _emit({"type": "done", "ok": True, "count": count})
        return 0
    except Exception as error:
        code = error_code(error)
        _emit({"type": "error", "code": code, "message": ERROR_REASONS[code]})
        _emit({"type": "done", "ok": False, "count": count})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
