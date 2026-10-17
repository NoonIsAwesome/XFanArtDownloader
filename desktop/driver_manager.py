"""Update only this project's legacy ChromeDriver from Google's official CfT.

Selection follows https://developer.chrome.com/docs/chromedriver/downloads/version-selection:
prefer MAJOR.MINOR.BUILD, then fall back to the same milestone (Chrome 115+).
The v2 downloader does not depend on this compatibility helper.
"""

from __future__ import annotations

import base64
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import tempfile
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

try:
    import winreg
except ImportError:  # Allow tests and imports on non-Windows hosts.
    winreg = None


BUILD_URL = "https://googlechromelabs.github.io/chrome-for-testing/latest-patch-versions-per-build-with-downloads.json"
MILESTONE_URL = "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json"
TOTAL_TIMEOUT = 25.0
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_EXE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 96 * 1024 * 1024
VERSION = re.compile(r"\d+\.\d+\.\d+\.\d+")


def _remaining(deadline: float, ceiling: float = 2.0) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("檢查已超過 25 秒，請稍後再試")
    return min(remaining, ceiling)


def _valid_version(value: object) -> str | None:
    return value if isinstance(value, str) and VERSION.fullmatch(value) else None


def _run(argv: list[str], deadline: float, *, env: dict | None = None) -> str:
    result = subprocess.run(
        argv, shell=False, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=True, timeout=_remaining(deadline, 4.0),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env,
    )
    return result.stdout.strip()


def _chrome_version(deadline: float) -> str:
    if winreg is not None:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                _remaining(deadline)
                try:
                    with winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon", 0,
                                        winreg.KEY_READ | view) as key:
                        version = _valid_version(winreg.QueryValueEx(key, "version")[0])
                    if version:
                        return version
                except OSError:
                    continue

    # A constant encoded script reads the path from a child-only environment
    # variable, so spaces, quotes and PowerShell syntax in paths stay data.
    script = ("$ErrorActionPreference='Stop'; "
              "(Get-Item -LiteralPath $env:FANART_CHROME_FILE).VersionInfo.FileVersion")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    seen: set[Path] = set()
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if not root:
            continue
        chrome = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if chrome in seen or not chrome.is_file():
            continue
        seen.add(chrome)
        try:
            output = _run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                deadline, env={**os.environ, "FANART_CHROME_FILE": str(chrome)},
            )
            match = VERSION.search(output)
            if match:
                return match.group(0)
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeError("找不到本機 Google Chrome 版本；請先安裝 Chrome。新版下載功能仍可使用")


def _platform_tag() -> str:
    architecture = " ".join((os.environ.get("PROCESSOR_ARCHITEW6432", ""),
                              os.environ.get("PROCESSOR_ARCHITECTURE", ""), platform.machine()))
    return "win64" if "64" in architecture else "win32"


def _driver_version(executable: Path, deadline: float) -> str:
    output = _run([str(executable), "--version"], deadline)
    match = re.match(r"ChromeDriver\s+(\d+\.\d+\.\d+\.\d+)(?:\s|$)", output)
    if not match:
        raise RuntimeError("ChromeDriver 未回傳有效版本")
    return match.group(1)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise RuntimeError("拒絕下載來源重新導向，請稍後重試官方下載")


def _open_url(url: str, timeout: float):
    return build_opener(_NoRedirect()).open(
        Request(url, headers={"User-Agent": "FanArtDownloader/2 ChromeDriverCheck"}), timeout=timeout
    )


def _read_url(url: str, deadline: float, max_bytes: int) -> bytes:
    with _open_url(url, _remaining(deadline)) as response:
        if response.geturl() != url:
            raise RuntimeError("下載網址被重新導向，已拒絕")
        content_length = response.headers.get("Content-Length")
        if content_length and (not content_length.isdigit() or int(content_length) > max_bytes):
            raise RuntimeError("官方下載檔案大小不合法或超過限制")
        chunks = []
        size = 0
        while True:
            _remaining(deadline)
            # HTTPResponse.read1 performs at most one buffered/raw read; read()
            # can wait for a full buffer while a server trickles bytes forever.
            chunk = response.read1(min(65536, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise RuntimeError("官方下載檔案超過大小限制")
            chunks.append(chunk)
        if content_length and size != int(content_length):
            raise RuntimeError("官方下載檔案不完整，未替換現有版本")
        return b"".join(chunks)


def _read_metadata(url: str, deadline: float) -> dict:
    if url not in (BUILD_URL, MILESTONE_URL):
        raise RuntimeError("拒絕非官方版本清單網址")
    data = json.loads(_read_url(url, deadline, MAX_METADATA_BYTES).decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("官方版本清單格式錯誤")
    return data


def _download_url(url: object, version: str, platform_tag: str) -> str:
    if not isinstance(url, str):
        raise RuntimeError("官方清單缺少 ChromeDriver 下載網址")
    parsed = urlsplit(url)
    expected_path = f"/chrome-for-testing-public/{version}/{platform_tag}/chromedriver-{platform_tag}.zip"
    if (parsed.scheme != "https" or parsed.netloc != "storage.googleapis.com"
            or parsed.path != expected_path or parsed.query or parsed.fragment):
        raise RuntimeError("拒絕非官方 ChromeDriver HTTPS 下載網址")
    return url


def _select_driver(chrome_version: str, platform_tag: str, deadline: float) -> tuple[str, str]:
    build = ".".join(chrome_version.split(".")[:3])
    major = chrome_version.split(".")[0]
    data = _read_metadata(BUILD_URL, deadline)
    entry = data.get("builds", {}).get(build)
    fallback = entry is None
    if fallback:
        data = _read_metadata(MILESTONE_URL, deadline)
        entry = data.get("milestones", {}).get(major)
    if not isinstance(entry, dict):
        raise RuntimeError(f"官方尚未提供 Chrome {chrome_version} 的相容 ChromeDriver")
    version = _valid_version(entry.get("version"))
    if (not version or version.split(".")[0] != major
            or (not fallback and ".".join(version.split(".")[:3]) != build)):
        raise RuntimeError("官方清單的 ChromeDriver 版本與本機 Chrome 不相符")
    downloads = entry.get("downloads", {}).get("chromedriver", [])
    for download in downloads:
        if isinstance(download, dict) and download.get("platform") == platform_tag:
            return version, _download_url(download.get("url"), version, platform_tag)
    raise RuntimeError(f"官方尚未提供 ChromeDriver {version} 的 {platform_tag} 下載")


def _extract_driver(payload: bytes, platform_tag: str, deadline: float) -> bytes:
    expected = f"chromedriver-{platform_tag}/chromedriver.exe"
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        members = archive.infolist()
        if not members or len(members) > 16:
            raise RuntimeError("ChromeDriver ZIP 的檔案數量不合法")
        names: set[str] = set()
        total_size = 0
        for member in members:
            _remaining(deadline)
            # ZipInfo normalizes backslashes on Windows; inspect the raw name
            # as well so normalization cannot hide an unsafe archive member.
            name = member.orig_filename
            parts = name.rstrip("/").split("/")
            if (name in names or "\\" in name or ":" in name or "\x00" in name
                    or PurePosixPath(name).is_absolute() or any(part in ("", ".", "..") for part in parts)
                    or parts[0] != f"chromedriver-{platform_tag}"
                    or stat.S_ISLNK(member.external_attr >> 16)
                    or member.flag_bits & 1
                    or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise RuntimeError("ChromeDriver ZIP 含有不安全路徑或項目")
            names.add(name)
            total_size += member.file_size
            if member.file_size > MAX_EXE_BYTES or total_size > MAX_UNCOMPRESSED_BYTES:
                raise RuntimeError("ChromeDriver ZIP 解壓大小超過限制")
        if expected not in names:
            raise RuntimeError("ChromeDriver ZIP 缺少預期執行檔")
        # Reads verify every member's ZIP CRC without extracting any archive path.
        if archive.testzip() is not None:
            raise RuntimeError("ChromeDriver ZIP CRC 驗證失敗")
        _remaining(deadline)
        executable = archive.read(expected)
    if len(executable) < 2 or not executable.startswith(b"MZ"):
        raise RuntimeError("ChromeDriver 執行檔缺少有效的 MZ 標頭")
    return executable


def ensure_chromedriver(base_dir: Path, log: Callable[[str], None] = lambda message: None) -> str:
    """Return the verified driver version, updating only base_dir/chromedriver.exe.

    No Chrome browser, global driver, registry entry, or login data is modified.
    An existing driver with the same MAJOR.MINOR.BUILD needs no network request.
    Every failure leaves the old driver intact. Network and process work share a
    25-second budget (an in-flight socket read can take up to two extra seconds).
    """
    deadline = time.monotonic() + TOTAL_TIMEOUT
    temporary: Path | None = None
    try:
        chrome_version = _chrome_version(deadline)
        if not _valid_version(chrome_version):
            raise RuntimeError("無法辨識本機 Chrome 版本")
        log(f"偵測到 Google Chrome {chrome_version}")
        base_dir = Path(base_dir).resolve()
        executable = base_dir / "chromedriver.exe"
        if executable.is_file():
            try:
                existing = _driver_version(executable, deadline)
            except (OSError, subprocess.SubprocessError, RuntimeError):
                existing = ""
            if existing.split(".")[:3] == chrome_version.split(".")[:3]:
                log(f"ChromeDriver {existing} 已與 Chrome 版本相符")
                return existing
        if int(chrome_version.split(".")[0]) < 115:
            raise RuntimeError("自動更新需要 Chrome 115 以上版本，請先更新 Chrome")
        platform_tag = _platform_tag()
        version, url = _select_driver(chrome_version, platform_tag, deadline)
        log(f"從 Google 官方下載 ChromeDriver {version}（{platform_tag}）")
        payload = _read_url(url, deadline, MAX_ZIP_BYTES)
        binary = _extract_driver(payload, platform_tag, deadline)
        base_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".chromedriver-", suffix=".exe", dir=base_dir)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(binary)
            handle.flush()
            os.fsync(handle.fileno())
        actual_version = _driver_version(temporary, deadline)
        if actual_version != version:
            raise RuntimeError(f"下載檔案版本不相符：預期 {version}，實際 {actual_version}")
        _remaining(deadline)
        os.replace(temporary, executable)
        temporary = None
        log(f"ChromeDriver 已更新並驗證：{version}")
        return version
    except Exception as error:
        raise RuntimeError(f"ChromeDriver 自動檢查失敗：{error}。現有 driver 保留，新版下載功能仍可使用") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A scanner may briefly hold the failed executable. Never mask
                # the useful validation failure with a cleanup error.
                pass
