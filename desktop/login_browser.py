"""An isolated, visible X login browser and validated Netscape cookie export.

The profile belongs to this application. This module never opens a normal Chrome
or Edge profile and does not need Selenium, ChromeDriver, or a driver download.
"""

from __future__ import annotations

import ctypes
import copy
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import warnings


LOGIN_URL = "https://x.com/i/flow/login"
EXPORT_TIMEOUT_SECONDS = 12.0
_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9-]+\Z")
_COOKIE_BASES = ("x.com", "twitter.com")

# Named Win32 values keep the native ownership/locking checks reviewable.
_HWND_MESSAGE = -3
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_COOKIE_URLS = ("https://x.com/", "https://twitter.com/")
_CLOSE_LOGIN_MESSAGE = "請先關閉剛才用來登入的所有專用瀏覽器視窗，再回來按「儲存登入」。不要關閉您的日常瀏覽器。"


class LoginWindowStillOpen(RuntimeError):
    """The application profile is still in use; never force it closed."""


class LoginWindowUnavailable(RuntimeError):
    """Ownership or safe access could not be established."""


class _CDPError(RuntimeError):
    """A credential-free error message created by this module."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _restrict_permissions(path: Path, *, directory: bool = False) -> None:
    """Restrict the new file before writing secrets, including its Windows ACL."""
    os.chmod(path, 0o700 if directory else 0o600)
    if os.name != "nt":
        return
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    inherit = "OICI" if directory else ""
    # Owner, SYSTEM and local administrators; no inherited broad permissions.
    sddl = f"D:P(A;{inherit};FA;;;OW)(A;{inherit};FA;;;SY)(A;{inherit};FA;;;BA)"
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _cookie_base(domain: str) -> str | None:
    domain = domain.lower().removeprefix(".")
    for base in _COOKIE_BASES:
        if domain == base or domain.endswith("." + base):
            return base
    return None


def _normalise_cookie(record: dict[str, Any], now: float) -> http.cookiejar.Cookie | None:
    domain = record.get("domain")
    name = record.get("name")
    value = record.get("value")
    path = record.get("path", "/")
    if not all(isinstance(item, str) for item in (domain, name, value, path)):
        return None
    domain = domain.lower()
    if (not _cookie_base(domain) or domain.startswith("..")
            or not name or not path.startswith("/")):
        return None
    if any(ord(char) < 32 or ord(char) == 127
           for item in (domain, name, value, path) for char in item):
        return None
    if name in {"auth_token", "ct0"} and not value.strip():
        return None
    expiry = record.get("expires", record.get("expiry"))
    if isinstance(expiry, bool):
        return None
    if expiry is None or (record.get("session") is True and expiry in (-1, 0)):
        expires = None  # Chromium's session-cookie sentinels.
    else:
        try:
            numeric_expiry = float(expiry)
            if not math.isfinite(numeric_expiry) or numeric_expiry <= now:
                return None
            expires = int(numeric_expiry)
            if expires <= now:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
    rest = {"HTTPOnly": None} if record.get("httpOnly") else {}
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=domain.startswith("."),
        domain_initial_dot=domain.startswith("."), path=path,
        path_specified=True, secure=True, expires=expires,
        discard=expires is None, comment=None, comment_url=None,
        rest=rest, rfc2109=False,
    )


def _validated_cookies(records: list[dict[str, Any]], *,
                       canonical_only: bool = False) -> list[http.cookiejar.Cookie]:
    now = time.time()
    jar = http.cookiejar.CookieJar()
    for record in records:
        if isinstance(record, dict):
            cookie = _normalise_cookie(record, now)
            if cookie is not None:
                jar.set_cookie(cookie)
    cookies = list(jar)
    # Never splice a token pair from different cookie domains or accounts. The
    # downloader expects .x.com, so promote a legacy Twitter pair together only.
    domains = (".x.com",) if canonical_only else (".x.com", "x.com", ".twitter.com", "twitter.com")
    for domain in domains:
        pair = {cookie.name: cookie for cookie in cookies
                if cookie.domain == domain and cookie.path == "/"
                and cookie.name in {"auth_token", "ct0"}}
        if {"auth_token", "ct0"}.issubset(pair):
            result = [cookie for cookie in cookies if cookie.name not in {"auth_token", "ct0"}]
            for name in ("auth_token", "ct0"):
                cookie = copy.copy(pair[name])
                cookie.domain = ".x.com"
                cookie.domain_specified = cookie.domain_initial_dot = True
                result.append(cookie)
            return result
    raise ValueError("Cookie 缺少同一個 X／Twitter 網域的有效 auth_token 或 ct0，請先完成登入。")


def _write_cookies(records: list[dict[str, Any]], destination: Path) -> int:
    cookies = _validated_cookies(records)
    destination = Path(destination).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".cookies-", suffix=".tmp",
                                               dir=destination.parent)
        os.close(descriptor)
        temporary = Path(filename)
        _restrict_permissions(temporary)
        jar = http.cookiejar.MozillaCookieJar(str(temporary))
        for cookie in cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True, ignore_expires=False)
        with temporary.open("r+b") as cookie_file:
            os.fsync(cookie_file.fileno())
        os.replace(temporary, destination)
        temporary = None
    except (OSError, UnicodeError):
        raise RuntimeError("無法安全儲存 Cookie，請檢查儲存位置與存取權限。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(cookies)


def _read_cookie_records(source: Path, *, allow_json: bool = True) -> list[dict[str, Any]]:
    source = Path(source)
    try:
        if source.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Cookie 檔案過大，請匯入 X 專用 Cookie 檔案。")
        content = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("無法讀取 Cookie 檔案，請檢查檔案及存取權限。") from None
    try:
        if content.lstrip().startswith(("[", "{")):
            if not allow_json:
                raise ValueError
            records = json.loads(content)
            if not isinstance(records, list):
                raise ValueError
        else:
            # MozillaCookieJar owns the Netscape parser, including HttpOnly rows.
            jar = http.cookiejar.MozillaCookieJar(str(source))
            # Its malformed-row warning includes parser exceptions; keep all
            # cookie-file content out of stderr as well as user-facing errors.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                jar.load(ignore_discard=True, ignore_expires=True)
            records = [{"name": item.name, "value": item.value,
                        "domain": item.domain, "path": item.path,
                        "expires": item.expires, "secure": item.secure,
                        "session": item.expires is None,
                        "httpOnly": item.has_nonstandard_attr("HTTPOnly")}
                       for item in jar]
    except (ValueError, OSError, UnicodeError, http.cookiejar.LoadError):
        raise ValueError("Cookie 檔案格式無效，請使用 Netscape TXT 或舊版 Selenium JSON 清單。") from None
    return records


def import_cookies(source: Path, destination: Path) -> int:
    """Import Netscape text or a legacy Selenium JSON list without exposing it."""
    return _write_cookies(_read_cookie_records(Path(source)), Path(destination))


def validate_cookies(path: Path) -> int:
    """Read-only validation of a canonical Netscape file ready for gallery-dl."""
    records = _read_cookie_records(Path(path), allow_json=False)
    return len(_validated_cookies(records, canonical_only=True))


def _find_browser() -> Path:
    candidates: list[Path] = []
    for environment_key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                            "LOCALAPPDATA"):
        if directory := os.environ.get(environment_key):
            candidates.extend((Path(directory) / "Google/Chrome/Application/chrome.exe",
                               Path(directory) / "Microsoft/Edge/Application/msedge.exe"))
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                for executable in ("chrome.exe", "msedge.exe"):
                    key = "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + executable
                    try:
                        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                            value, _ = winreg.QueryValueEx(handle, None)
                        if isinstance(value, str):
                            candidates.append(Path(os.path.expandvars(value.strip('"'))))
                    except OSError:
                        pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("找不到 Chrome 或 Microsoft Edge，請先安裝其中一個瀏覽器。")


def _validate_websocket(endpoint: str, port: int, browser_path: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        return (parsed.scheme == "ws" and parsed.hostname == "127.0.0.1"
                and parsed.port == port and parsed.path == browser_path
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def _connect_websocket(endpoint: str, timeout: float):
    try:
        import websocket
    except ImportError:
        raise _CDPError("缺少 websocket-client，請先安裝專案相依套件。") from None
    return websocket.create_connection(
        endpoint, timeout=timeout, suppress_origin=True,
        http_no_proxy=["127.0.0.1", "localhost"], enable_multithread=True,
    )


def _profile_locked(profile_dir: Path) -> bool:
    """Probe Chromium's Windows singleton lock without reading profile data.

    Chromium opens this file for GENERIC_WRITE, with FILE_SHARE_READ only.
    Requesting write access therefore fails while *any* process owns the profile.
    OPEN_EXISTING leaves stale files untouched and never creates a fake lock.
    """
    if os.name != "nt":
        raise LoginWindowUnavailable("此版本僅支援 Windows 的專用登入瀏覽器；可改匯入 X Cookie。")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(profile_dir / "lockfile"), _GENERIC_WRITE, 0, None,
                    _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in (2, 3):  # no file / no directory
            return False
        if error in (32, 33, 303):  # sharing, lock, delete pending
            return True
        raise LoginWindowUnavailable("無法確認專用登入視窗是否已關閉；請關閉該視窗並檢查設定檔權限後重試。")
    close(handle)
    return False


def _profile_belongs_to_process(profile_dir: Path, pid: int) -> bool:
    """Use Chromium's exact-profile singleton window, never a global scan."""
    if os.name != "nt":
        raise LoginWindowUnavailable("無法在此平台確認匯出瀏覽器的擁有權；可改匯入 X Cookie。")
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    find = user.FindWindowExW
    find.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    find.restype = wintypes.HWND
    owner = user.GetWindowThreadProcessId
    owner.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    owner.restype = wintypes.DWORD
    # Chromium MessageWindow::FindWindow uses HWND_MESSAGE, this class and UDD title.
    window = find(wintypes.HWND(_HWND_MESSAGE), None, "Chrome_MessageWindow", str(profile_dir))
    if not window:
        return False
    actual_pid = wintypes.DWORD()
    if not owner(window, ctypes.byref(actual_pid)):
        raise LoginWindowUnavailable("無法確認專用設定檔的程序擁有權；請稍後重試。")
    return actual_pid.value == pid


class _CDPConnection:
    """One bounded browser connection, including flattened page sessions."""

    def __init__(self, endpoint: str, deadline: float, check: Callable[[], None] | None = None):
        try:
            parsed = urlsplit(endpoint)
            valid = _BROWSER_PATH.fullmatch(parsed.path) and _validate_websocket(endpoint, parsed.port or 0, parsed.path)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise _CDPError("匯出瀏覽器的本機連線位址無效。")
        self.deadline = deadline
        self.check = check
        self.sequence = 0
        self.connection = None
        try:
            if check:
                check()
            self.connection = _connect_websocket(endpoint, max(0.01, min(2.0, deadline - time.monotonic())))
        except (LoginWindowStillOpen, LoginWindowUnavailable, _CDPError):
            raise
        except Exception:
            raise _CDPError("無法連線到專用匯出瀏覽器，請稍後重試。") from None

    def call(self, method: str, params: dict | None = None, *, session_id: str | None = None) -> dict:
        try:
            if self.check:
                self.check()
            self.sequence += 1
            command = {"id": self.sequence, "method": method, "params": params or {}}
            if session_id is not None:
                command["sessionId"] = session_id
            self.connection.settimeout(max(0.01, min(2.0, self.deadline - time.monotonic())))
            if time.monotonic() >= self.deadline:
                raise _CDPError("匯出登入狀態逾時，請稍後重試。")
            self.connection.send(json.dumps(command))
            while time.monotonic() < self.deadline:
                self.connection.settimeout(max(0.01, min(2.0, self.deadline - time.monotonic())))
                raw = self.connection.recv()
                if not isinstance(raw, (str, bytes)) or len(raw) > 8 * 1024 * 1024:
                    raise _CDPError("匯出瀏覽器回應格式無效。")
                response = json.loads(raw)
                if not isinstance(response, dict):
                    raise _CDPError("匯出瀏覽器回應格式無效。")
                if response.get("id") == self.sequence and response.get("sessionId") == session_id:
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise _CDPError("瀏覽器無法擷取 X 登入狀態，請重新登入後重試。")
                    return response["result"]
            raise _CDPError("匯出登入狀態逾時，請稍後重試。")
        except (LoginWindowStillOpen, LoginWindowUnavailable, _CDPError):
            raise
        except Exception:
            # Neither protocol error payloads nor library exceptions are user text.
            raise _CDPError("無法擷取 X 登入狀態，請關閉專用視窗後重試。") from None

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None


def _cdp_request(endpoint: str, method: str, params: dict | None = None,
                 timeout: float = 2.0) -> dict:
    """Compatibility helper for one browser-domain request."""
    connection = _CDPConnection(endpoint, time.monotonic() + timeout)
    try:
        return connection.call(method, params)
    finally:
        connection.close()


class LoginBrowser:
    """Human login is a normal browser; only a later, owned export uses CDP."""

    def __init__(self, profile_dir: Path, on_progress: Callable[[str], None] | None = None):
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        if base := os.environ.get("LOCALAPPDATA"):
            for relative in ("Google/Chrome/User Data", "Microsoft/Edge/User Data"):
                personal = (Path(base) / relative).resolve()
                if self.profile_dir == personal or personal in self.profile_dir.parents:
                    raise ValueError("請使用本程式專用的登入設定檔，不可使用日常瀏覽器設定檔。")
        self.on_progress = on_progress
        self.status = "idle"
        self._login_process: subprocess.Popen | None = None
        self._export_process: subprocess.Popen | None = None
        self._endpoint: str | None = None
        self._browser: Path | None = None
        self._lock = threading.RLock()

    def _progress(self, status: str, message: str) -> None:
        self.status = status
        if self.on_progress:
            try:
                self.on_progress(message)
            except Exception:
                pass  # A UI callback must never interrupt safe cleanup.

    def _assert_profile_idle(self) -> None:
        if self._export_process is not None and not self._stop_export_process():
            raise LoginWindowUnavailable("前次背景擷取瀏覽器尚未完全關閉；請稍後重試結束或儲存登入。")
        if self._login_process is not None and self._login_process.poll() is None:
            raise LoginWindowStillOpen(_CLOSE_LOGIN_MESSAGE)
        if _profile_locked(self.profile_dir):
            raise LoginWindowStillOpen(_CLOSE_LOGIN_MESSAGE)

    def _prepare_profile(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _restrict_permissions(self.profile_dir, directory=True)

    @staticmethod
    def _launch(command: list[str]) -> subprocess.Popen:
        try:
            return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True,
                                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError:
            raise LoginWindowUnavailable("無法啟動專用瀏覽器，請檢查 Chrome 或 Edge 安裝後重試。") from None

    def open(self) -> None:
        """Open visible normal login. Never attach to or kill any login window."""
        with self._lock:
            try:
                self._prepare_profile()
                self._assert_profile_idle()
                self._browser = _find_browser()
                command = [str(self._browser), f"--user-data-dir={self.profile_dir}",
                           "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
                           "--new-window", LOGIN_URL]
                self._login_process = self._launch(command)
                if self._login_process.poll() is not None:
                    raise LoginWindowUnavailable("登入瀏覽器未持續啟動；請先關閉既有的專用登入視窗後重試。")
                self._progress("login_open", "請在一般瀏覽器完成 X 登入；登入後關閉這個專用視窗，再回來按「儲存登入」。")
            except (LoginWindowStillOpen, LoginWindowUnavailable):
                self.status = "error"
                raise
            except OSError:
                self.status = "error"
                raise LoginWindowUnavailable("無法建立專用登入設定檔，請檢查儲存位置與權限。") from None

    def _assert_export_alive(self) -> None:
        if self._export_process is None or self._export_process.poll() is not None:
            raise LoginWindowStillOpen(_CLOSE_LOGIN_MESSAGE)

    def _assert_owned_endpoint(self, endpoint: str) -> None:
        self._assert_export_alive()
        parsed = urlsplit(endpoint)
        if not _profile_belongs_to_process(self.profile_dir, self._export_process.pid):
            raise LoginWindowUnavailable("匯出連線不屬於本次啟動的程序；" + _CLOSE_LOGIN_MESSAGE)
        self._assert_export_alive()

    def _probe_profile_endpoint(self, timeout: float = 0.5) -> str | None:
        self._assert_export_alive()
        try:
            active_port = self.profile_dir / "DevToolsActivePort"
            if active_port.stat().st_size > 1024:
                return None
            lines = active_port.read_text(encoding="utf-8").splitlines()
            if len(lines) < 2 or not lines[0].isdigit():
                return None
            port, browser_path = int(lines[0]), lines[1]
            if not 1 <= port <= 65535 or not _BROWSER_PATH.fullmatch(browser_path):
                return None
            if not _profile_belongs_to_process(self.profile_dir, self._export_process.pid):
                raise LoginWindowUnavailable("匯出連線不屬於本次啟動的程序；" + _CLOSE_LOGIN_MESSAGE)
            self._assert_export_alive()
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            request = Request(f"http://127.0.0.1:{port}/json/version", headers={"Accept": "application/json"})
            with opener.open(request, timeout=timeout) as response:
                payload = response.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                return None
            endpoint = json.loads(payload).get("webSocketDebuggerUrl")
            self._assert_export_alive()
            if isinstance(endpoint, str) and _validate_websocket(endpoint, port, browser_path):
                return endpoint
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        return None

    def save_cookies(self, destination: Path) -> int:
        """Export after all normal login windows close, including after app restart."""
        with self._lock:
            connection = None
            saved = False
            try:
                if not self.profile_dir.is_dir():
                    raise LoginWindowUnavailable("尚無專用登入設定檔；請先開啟登入瀏覽器並完成 X 登入。")
                self._assert_profile_idle()
                self._prepare_profile()
                self._browser = self._browser or _find_browser()
                # A previous app/CDP instance is never adopted, even if its file is valid.
                (self.profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
                deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS
                self._progress("exporting", "正在短暫擷取專用設定檔的 X 登入狀態；不會讀取其他網站的登入資料。")
                command = [str(self._browser), f"--user-data-dir={self.profile_dir}",
                           "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                           "--headless=new", "--no-first-run", "--no-default-browser-check",
                           "--disable-background-mode", "about:blank"]
                self._export_process = self._launch(command)
                while time.monotonic() < deadline - 3.0:
                    self._assert_export_alive()
                    endpoint = self._probe_profile_endpoint(timeout=min(0.3, max(0.01, deadline - time.monotonic())))
                    if endpoint:
                        self._endpoint = endpoint
                        break
                    time.sleep(0.05)
                if self._endpoint is None:
                    raise LoginWindowUnavailable("匯出瀏覽器啟動逾時；" + _CLOSE_LOGIN_MESSAGE)
                self._assert_owned_endpoint(self._endpoint)
                connection = _CDPConnection(self._endpoint, deadline, lambda: self._assert_owned_endpoint(self._endpoint))
                target = connection.call("Target.createTarget", {"url": "about:blank"})
                target_id = target.get("targetId")
                if not isinstance(target_id, str) or not target_id:
                    raise _CDPError("無法建立專用 X 登入擷取頁面，請稍後重試。")
                attached = connection.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                session_id = attached.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise _CDPError("無法連線到專用 X 登入擷取頁面，請稍後重試。")
                payload = connection.call("Network.getCookies", {"urls": list(_COOKIE_URLS)}, session_id=session_id)
                records = payload.get("cookies")
                if not isinstance(records, list):
                    raise _CDPError("瀏覽器沒有傳回可用的 X 登入狀態。")
                count = _write_cookies(records, Path(destination))
                saved = True
            except (RuntimeError, ValueError):
                self.status = "error"
                raise
            except Exception:
                self.status = "error"
                raise LoginWindowUnavailable("無法安全擷取登入狀態；請關閉專用瀏覽器後重試。") from None
            finally:
                if connection is not None:
                    try:
                        connection.call("Browser.close")
                    except Exception:
                        pass
                    connection.close()
                if not self._stop_export_process():
                    self.status = "error"
                    message = ("X 登入檔案已儲存，但背景擷取瀏覽器尚未完全關閉；" if saved else
                               "背景擷取瀏覽器尚未完全關閉，登入操作未完成；")
                    raise LoginWindowUnavailable(message + "請稍後重試結束或儲存登入。") from None
            self._progress("saved", "X 登入狀態已儲存；下載時會再由 X 確認有效性。")
            return count

    def _stop_export_process(self) -> bool:
        """Retain the owned handle and endpoint until exit is confirmed."""
        process = self._export_process
        if process is None:
            self._endpoint = None
            return True

        def finished() -> bool:
            try:
                return process.poll() is not None
            except OSError:
                return False

        def clear() -> bool:
            self._export_process = None
            self._endpoint = None
            return True

        if finished():
            return clear()
        try:
            process.wait(timeout=0.3)
            return clear()
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
            return clear()
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
            return clear()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return clear() if finished() else False

    def close(self) -> None:
        """Stop only the temporary exporter. Human login windows remain open."""
        with self._lock:
            if not self._stop_export_process():
                self.status = "error"
                raise LoginWindowUnavailable("背景擷取瀏覽器尚未完全關閉；請稍後再試一次結束程式。")
