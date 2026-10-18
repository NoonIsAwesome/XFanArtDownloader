# SPDX-License-Identifier: GPL-3.0-only
"""Subprocess client for the independent URL-to-JSONL gallery helper.

Only standard-library modules are imported here. No gallery-dl code or objects
enter the Qt process; extraction is an ordinary command-line invocation.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from network import validate_proxy_url

MAX_LINE_BYTES = 1024 * 1024
IDLE_TIMEOUT = 240.0  # Includes the helper's bounded retries on a slow network.
QUEUE_SIZE = 32
_SECRET = re.compile(r"cookie|header|authorization|password|passwd|secret|token|csrf|session|credential", re.I)
_EXTRACTORS = {"TwitterSearchExtractor", "TwitterMediaExtractor", "TwitterTweetExtractor"}


class Cancelled(Exception):
    pass


SOURCE_REASONS = {
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
    "protocol": "擷取工具回傳無效或未允許的資料；此來源未完整完成，請重新安裝完整版本或稍後重試。",
    "incomplete": "擷取工具提早結束或未完整完成；請查看來源紀錄後重試。",
    "exit": "擷取工具異常結束；此來源未完整完成，請稍後重試。",
    "startup": "找不到或無法啟動 gallery-dl-helper.exe；請檢查完整安裝後重試。",
    "cookies": "尚未儲存本機登入資料，請先登入並儲存。",
    "cleanup": "擷取工具尚未完全關閉；請稍候再重試。",
}


class GallerySourceError(RuntimeError):
    """A fixed, public reason; upstream exception text never becomes UI copy."""
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in SOURCE_REASONS else "source"
        self.public_reason = SOURCE_REASONS[self.code]
        super().__init__(self.public_reason)


def _source_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        allowed = (isinstance(value, str) and len(value) <= 8192 and parsed.scheme == "https"
            and parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
            and not parsed.username and not parsed.password and not parsed.port and not parsed.fragment)
        path = (parsed.path == "/search" and bool(parse_qs(parsed.query).get("q"))) or bool(re.fullmatch(
            r"/(?:[A-Za-z0-9_]{1,15}/media|(?:[A-Za-z0-9_]{1,15}/status|i/(?:web/)?status)/\d+)/?", parsed.path))
        if not allowed or not path:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("僅接受 HTTPS 的 X 搜尋、作者媒體頁或貼文網址。") from None
    return value


def _thread_url(value: str) -> str:
    _source_url(value)
    if not re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}/status|i/(?:web/)?status)/\d+/?", urlsplit(value).path):
        raise ValueError("僅接受 HTTPS 的 X 貼文網址來擷取討論串。")
    return value


def _media_url(value) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname in {"pbs.twimg.com", "video.twimg.com"}
                and not parsed.username and not parsed.password and not parsed.port and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def _safe_text(value) -> str:
    if not isinstance(value, str):
        return "擷取工具回報未預期的來源訊息。"
    if _SECRET.search(value):
        return "擷取工具回報登入或連線問題，詳細認證資訊已隱藏。"
    return "".join(char for char in value[:600] if char in "\n\t" or ord(char) >= 32)


def _public_metadata(value, depth=0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, dict):
        return all(isinstance(key, str) and not key.startswith("_") and not _SECRET.search(key)
                   and _public_metadata(item, depth + 1) for key, item in value.items())
    if isinstance(value, list):
        return all(_public_metadata(item, depth + 1) for item in value)
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (str, int, bool))


class _WindowsProcessJob:
    """Kill only this helper's process tree, including a one-file bootloader."""

    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("basic", BASIC), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("helper process management unavailable")
        limits = EXTENDED()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.kernel.CloseHandle(handle)
            raise OSError("helper process management unavailable")
        if not self.kernel.AssignProcessToJobObject(handle, int(process._handle)):
            self.kernel.CloseHandle(handle)
            if process.poll() is None:
                raise OSError("helper process isolation unavailable")
            return
        self.handle = handle

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class GallerySource:
    def __init__(self, cookie_file: Path | None, log, stop_event: threading.Event, *, proxy_url: str | None = None):
        self.cookie_file = Path(cookie_file) if cookie_file is not None else None
        self.log = log
        self.stop_event = stop_event
        self.proxy_url = validate_proxy_url(proxy_url) if proxy_url is not None else None

    def _check_stop(self):
        if self.stop_event.is_set():
            raise Cancelled()

    def _fail(self, code: str):
        self._check_stop()
        raise GallerySourceError(code) from None

    @staticmethod
    def _command():
        if getattr(sys, "frozen", False):
            executable_dir = Path(sys.executable).resolve().parent
            bundle_dir = Path(getattr(sys, "_MEIPASS", executable_dir))
            for directory in (bundle_dir, executable_dir):
                helper = directory / "gallery-dl-helper.exe"
                if helper.is_file():
                    return [str(helper)]
            raise GallerySourceError("startup")
        helper = Path(__file__).resolve().parents[1] / "extractor" / "gallery_bridge.py"
        if not helper.is_file():
            raise GallerySourceError("startup")
        return [sys.executable, str(helper)]

    @staticmethod
    def _reader(stream, output: queue.Queue, stop: threading.Event):
        def push(item):
            while not stop.is_set():
                try:
                    output.put(item, timeout=0.1)
                    return
                except queue.Full:
                    pass

        try:
            while not stop.is_set():
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    push(("error", "擷取工具回傳過大或不完整的資料行。"))
                    return
                push(("line", line))
        except (OSError, ValueError):
            if not stop.is_set():
                push(("error", "擷取工具的輸出連線已中斷。"))
        finally:
            push(("eof", None))

    def _records(self, url=None, *, smoke=False, conversation=False):
        self._check_stop()
        try:
            args = self._command()
        except GallerySourceError:
            self._check_stop()
            raise
        if smoke:
            args += ["--smoke-test"]
        else:
            args += ["--url", (_thread_url if conversation else _source_url)(url)]
            if self.cookie_file is None:
                self._fail("cookies")
            args += ["--cookies", str(self.cookie_file.resolve())]
            if self.proxy_url is not None:
                args += ['--proxy', self.proxy_url]
            if conversation:
                args += ['--conversation']
        environment = os.environ.copy()
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYINSTALLER_RESET_ENVIRONMENT": "1"})
        process = None
        guard = None
        reader = None
        reader_stop = threading.Event()
        output = queue.Queue(maxsize=QUEUE_SIZE)
        completed = None
        count = 0
        last_activity = time.monotonic()
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            guard = _WindowsProcessJob(process)
            reader = threading.Thread(target=self._reader, args=(process.stdout, output, reader_stop),
                                      name="gallery-jsonl-reader", daemon=True)
            reader.start()
            while True:
                if self.stop_event.is_set():
                    raise Cancelled()
                try:
                    kind, value = output.get(timeout=0.1)
                except queue.Empty:
                    limit = 3.0 if completed is not None else IDLE_TIMEOUT
                    if time.monotonic() - last_activity > limit:
                        self._fail("timeout")
                    continue
                self._check_stop()
                last_activity = time.monotonic()
                if kind == "error":
                    self._fail("protocol")
                if kind == "eof":
                    break
                if completed is not None:
                    self._fail("protocol")
                try:
                    record = json.loads(value.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    self._fail("protocol")
                if not isinstance(record, dict):
                    self._fail("protocol")
                record_type = record.get("type")
                if record_type == "error":
                    code = record.get("code")
                    if not isinstance(code, str) or code not in SOURCE_REASONS:
                        # Older helpers emitted these exact fixed reasons. Only
                        # exact allowlisted copy is recognized, never raw text.
                        code = next((name for name, reason in SOURCE_REASONS.items()
                                     if record.get("message") == reason), "source")
                    self._fail(code)
                if record_type == "log":
                    self.log(_safe_text(record.get("message")))
                elif record_type == "metadata":
                    if not _media_url(record.get("url")) or not isinstance(record.get("metadata"), dict) or not _public_metadata(record["metadata"]):
                        self._fail("protocol")
                    count += 1
                    yield record
                elif record_type == "post":
                    if not conversation or not isinstance(record.get("metadata"), dict) or not _public_metadata(record["metadata"]):
                        self._fail("protocol")
                    count += 1
                    yield record
                elif record_type == "done":
                    if record.get("ok") is not True:
                        self._fail("incomplete")
                    if type(record.get("count")) is not int or record["count"] != count:
                        self._fail("protocol")
                    completed = record
                elif record_type != "log":
                    self._fail("protocol")
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._fail("exit")
            self._check_stop()
            if returncode != 0:
                self._fail("exit")
            if completed is None:
                self._fail("incomplete")
            yield completed
        except OSError:
            self._fail("startup")
        finally:
            reader_stop.set()
            if guard is not None:
                guard.close()
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass  # The owned Job Object may already have ended it.
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except OSError:
                            pass
                        process.wait(timeout=2)
                if reader is not None:
                    reader.join(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()
                if reader is not None:
                    reader.join(timeout=0.2)

    def iter_media(self, url: str):
        for record in self._records(url):
            if record["type"] == "metadata":
                yield record["url"], record["metadata"]

    def iter_thread(self, url: str):
        for record in self._records(url, conversation=True):
            if record["type"] == "post":
                yield None, record["metadata"]
            elif record["type"] == "metadata":
                yield record["url"], record["metadata"]

    @classmethod
    def probe(cls) -> tuple[str, ...]:
        source = cls(None, lambda message: None, threading.Event())
        records = list(source._records(smoke=True))
        names = records[-1].get("extractors") if records else None
        if not isinstance(names, list) or len(names) != 3 or set(names) != _EXTRACTORS:
            raise GallerySourceError("protocol")
        return tuple(names)
