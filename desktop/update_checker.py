"""Manual, read-only checks against this project's GitHub release and PyPI."""
from __future__ import annotations

import json
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, unquote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from app_version import APP_VERSION
from gallery_client import Cancelled, GallerySource

RELEASES_URL = "https://github.com/NoonIsAwesome/XFanArtDownloader/releases"
GITHUB_API = "https://api.github.com/repos/NoonIsAwesome/XFanArtDownloader/releases/latest"
PYPI_API = "https://pypi.org/pypi/gallery-dl/json"
PYPI_URL = "https://pypi.org/project/gallery-dl/"
MAX_RESPONSE_BYTES = 1024 * 1024
TOTAL_SECONDS = 24.0
_VERSION = re.compile(r"v?(\d{1,6}(?:\.\d{1,6}){1,3})\Z", re.I)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _check_stop(stop_event):
    if stop_event.is_set():
        raise Cancelled()


def _version(value):
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value)
    if match is None:
        return None
    parts = tuple(int(item) for item in match[1].split("."))
    return parts + (0,) * (4 - len(parts))


def release_url(value: str, *, download=False) -> bool:
    # 只讓使用者開啟本專案的 release/tag 或下載路徑，不信任遠端提供的任意連結。
    # Permit only this repository's release/tag or asset paths, never arbitrary remote links.
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
        return False
    try:
        url = urlsplit(value)
        prefix = "/NoonIsAwesome/XFanArtDownloader/releases/" + ("download/" if download else "tag/")
        path = unquote(url.path)
        return (url.scheme == "https" and url.netloc == "github.com"
                and not url.query and not url.fragment and path.startswith(prefix)
                and len(path) > len(prefix) and "\\" not in path
                and not any(ord(char) < 32 or ord(char) == 127 for char in path)
                and not any(piece in {".", ".."} for piece in path.split("/")))
    except (TypeError, ValueError):
        return False


def _read_json(url: str, deadline: float, stop_event):
    # 無 Cookie、認證 handler 或環境代理；只向兩個固定官方端點手動查版本。
    # No cookie/auth handlers or ambient proxies; contact only the two fixed official endpoints.
    if url not in {GITHUB_API, PYPI_API}:
        raise ValueError("unsupported endpoint")
    _check_stop(stop_event)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    request = Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                   "User-Agent": "XFanArtDownloader-UpdateCheck"})
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=min(5.0, remaining)) as response:
        if response.geturl() != url:
            raise ValueError("redirect refused")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            raise ValueError("response too large")
        body = bytearray()
        while True:
            _check_stop(stop_event)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            # read1 不會為填滿整個 buffer 反覆等候慢速串流；每輪都重新核對總時間。
            # read1 performs one buffered/raw read so each iteration can recheck the total deadline.
            chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response too large")
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid metadata")
        return value


def _component(installed, latest, url, downloads=()):
    local, remote = _version(installed), _version(latest)
    status = "unknown" if local is None or remote is None else (
        "update_available" if remote > local else "ahead" if remote < local else "current")
    return {"installed": installed, "latest": latest, "status": status,
            "url": url, "download_urls": list(downloads)}


def check_updates(*, stop_event=None) -> dict:
    """Return independent component results; never download/install an update."""
    stop_event = stop_event or threading.Event()
    _check_stop(stop_event)
    deadline = time.monotonic() + TOTAL_SECONDS
    result = {"app": _component(APP_VERSION, None, RELEASES_URL),
              "gallery_dl": _component(None, None, PYPI_URL)}
    # 個別來源失敗不遮蔽另一個結果；所有錯誤只回傳固定狀態，不公開上游例外。
    # One source may fail independently; expose fixed statuses rather than upstream error text.
    try:
        metadata = _read_json(GITHUB_API, deadline, stop_event)
        tag = metadata.get("tag_name")
        link = metadata.get("html_url")
        if (_version(tag) is None or not release_url(link)
                or metadata.get("draft") is not False or metadata.get("prerelease") is not False):
            raise ValueError("invalid release")
        assets = metadata.get("assets", [])
        if not isinstance(assets, list):
            raise ValueError("invalid release assets")
        downloads = [asset["browser_download_url"] for asset in assets[:100]
                     if isinstance(asset, dict) and release_url(asset.get("browser_download_url"), download=True)]
        result["app"] = _component(APP_VERSION, tag, link, downloads)
    except HTTPError as error:
        result["app"]["status"] = "no_release" if error.code == 404 else "unavailable"
        error.close()
    except Cancelled:
        raise
    except Exception:
        result["app"]["status"] = "unavailable"
    _check_stop(stop_event)
    try:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            result["gallery_dl"]["installed"] = GallerySource.version(stop_event=stop_event, timeout=min(8.0, remaining))
    except Cancelled:
        raise
    except Exception:
        pass
    try:
        metadata = _read_json(PYPI_API, deadline, stop_event)
        version = metadata.get("info", {}).get("version")
        if _version(version) is None:
            raise ValueError("invalid package version")
        result["gallery_dl"] = _component(result["gallery_dl"]["installed"], version, PYPI_URL)
    except HTTPError as error:
        result["gallery_dl"]["status"] = "unavailable"
        error.close()
    except Cancelled:
        raise
    except Exception:
        result["gallery_dl"]["status"] = "unavailable"
    _check_stop(stop_event)
    return result


class UpdateCheckWorker(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stop_event = threading.Event()

    def cancel(self):
        # 可從 GUI 設旗標；不跨執行緒操作 Qt 視窗或強制中斷網路函式。
        # The GUI may set this flag without touching widgets or forcibly interrupting network calls.
        self.stop_event.set()

    @pyqtSlot()
    def run(self):
        try:
            result = check_updates(stop_event=self.stop_event)
            if not self.stop_event.is_set():
                self.completed.emit(result)
        except Cancelled:
            pass
        except Exception:
            self.failed.emit("版本檢查未完成，請稍後重試。")
        finally:
            self.finished.emit()
