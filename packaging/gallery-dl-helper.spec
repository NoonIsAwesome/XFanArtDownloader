# -*- mode: python ; coding: utf-8 -*-
"""Freeze extractor/ as a GPL-2.0-only helper without the desktop Qt runtime."""
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = Path(SPECPATH).parent
if sys.platform == 'win32':
    windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    os.environ['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), str(windows / 'System32'), str(windows)))
datas = collect_data_files('gallery_dl') + collect_data_files('certifi')
for distribution in ('gallery-dl', 'requests', 'certifi', 'charset-normalizer', 'idna', 'urllib3', 'PySocks', 'pyinstaller'):
    datas += copy_metadata(distribution)
for filename in ('GPL-2.0.txt',):
    datas.append((str(ROOT / 'licenses' / filename), 'licenses'))
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    datas.append((str(python_license), 'licenses/Python'))
a = Analysis(
    [str(ROOT / 'extractor' / 'gallery_bridge.py')],
    pathex=[str(ROOT / 'extractor')], binaries=[], datas=datas,
    hiddenimports=['gallery_dl.extractor.twitter', 'gallery_dl.extractor.utils.twitter_transaction_id', 'requests.certs', 'certifi', 'socks', 'urllib3.contrib.socks'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PyQt6', 'PyQt5', 'PySide6', 'PySide2', 'tkinter', 'selenium', 'yt_dlp', 'youtube_dl',
              'main', 'gallery_client', 'downloader', 'ui_design', 'ui_theme', 'gallery_widgets', 'author_dialog'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='gallery-dl-helper',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False,
)
