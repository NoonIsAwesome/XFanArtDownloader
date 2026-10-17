"""Resolve bundled image files for source runs and PyInstaller builds.

Assets stay as ordinary, reviewable files. No shared temporary files or embedded
base64 copies are generated at runtime.
"""
from pathlib import Path
import sys


def _asset_path(filename: str) -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "assets"))
    path = root / filename
    if not path.is_file():
        raise FileNotFoundError(f"Missing application asset: {filename}")
    return str(path)


def get_icon_path() -> str:
    return _asset_path("icon.ico")


def get_author_image_path() -> str:
    return _asset_path("author.png")
