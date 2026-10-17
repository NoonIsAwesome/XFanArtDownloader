# -*- mode: python ; coding: utf-8 -*-
"""Compatibility alias for packaging/XFanArtDownloader.spec."""
from pathlib import Path

ROOT = Path(SPECPATH).parent
canonical_spec = ROOT / "packaging" / "XFanArtDownloader.spec"
exec(compile(canonical_spec.read_text(encoding="utf-8"), str(canonical_spec), "exec"))
