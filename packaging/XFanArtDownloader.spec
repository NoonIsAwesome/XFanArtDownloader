# -*- mode: python ; coding: utf-8 -*-
"""Freeze the desktop GUI; embed the separately built extractor as data only."""
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = Path(SPECPATH).parent
# Resolve native dependencies against Windows and this environment. Developer
# tool PATH entries (e.g. Poppler ICU) must not contaminate a frozen Qt build.
if sys.platform == "win32":
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    os.environ["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), str(windows / "System32"), str(windows)))
RUNTIME_DISTRIBUTIONS = (
    "PyQt6", "PyQt6-Qt6", "PyQt6-sip", "Pillow",
    "websocket-client", "requests", "certifi", "charset-normalizer", "idna", "urllib3",
)
datas = [(str(ROOT / "assets" / name), ".") for name in ("icon.ico", "author.png")]
# Keep the GPL v2 extractor as an unchanged, separate executable inside this
# self-extracting distribution. It never becomes a GUI Python module.
helper = ROOT / "dist" / "gallery-dl-helper.exe"
if not helper.is_file():
    raise SystemExit("Build packaging/gallery-dl-helper.spec first.")
datas.append((str(helper), "."))
for license_name in ("GPL-2.0.txt", "GPL-3.0.txt"):
    datas.append((str(ROOT / "licenses" / license_name), "licenses"))
for name in ("README.md", "THIRD_PARTY_NOTICES.txt", "LICENSE"):
    if (ROOT / name).is_file():
        datas.append((str(ROOT / name), "."))
datas += collect_data_files("certifi")
for distribution in RUNTIME_DISTRIBUTIONS + ("pyinstaller",):
    datas += copy_metadata(distribution)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    datas.append((str(python_license), "licenses/Python"))

a = Analysis(
    [str(ROOT / "desktop" / "main.py")],
    pathex=[str(ROOT / "desktop")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "requests.certs", "certifi", "websocket",
        "PIL.JpegImagePlugin", "PIL.PngImagePlugin", "PIL.GifImagePlugin",
        "PIL.WebPImagePlugin", "PIL.BmpImagePlugin", "PIL.TiffImagePlugin",
        "PIL.IcoImagePlugin", "PIL.AvifImagePlugin",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["gallery_dl", "gallery_bridge", "selenium", "yt_dlp", "youtube_dl", "PyQt5", "PySide2", "PySide6", "tkinter"],
    noarchive=False,
    optimize=0,
)
# Qt 6.10 on Windows imports the OS ICU API (unversioned symbols). A Poppler
# icuuc.dll exports *_78 instead and causes QtCore error 127 at startup.
wrong_icu = {"icuuc.dll", "icuin.dll", "icu.dll", "icudt78.dll"}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in wrong_icu]
assert not any(Path(entry[0]).name.lower() in wrong_icu for entry in a.binaries)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='XFanArtDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / "packaging" / "version.txt"),
    icon=[str(ROOT / "assets" / "icon.ico")],
)
