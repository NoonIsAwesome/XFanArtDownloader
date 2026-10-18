"""Offline helper-version bridge without importing gallery-dl into the GUI."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from gallery_client import GallerySource, GallerySourceError


class HelperVersionTests(unittest.TestCase):
    def test_version_requires_a_bounded_version_string_and_keeps_probe_contract(self):
        names = ["TwitterSearchExtractor", "TwitterMediaExtractor", "TwitterTweetExtractor"]
        record = {"type": "done", "ok": True, "count": 0, "extractors": names, "version": "1.32.16"}
        with patch.object(GallerySource, "_records", return_value=iter([record])):
            self.assertEqual(GallerySource.version(), "1.32.16")
        with patch.object(GallerySource, "_records", return_value=iter([record])):
            self.assertEqual(GallerySource.probe(), tuple(names))
        for value in (None, "", "secret\nvalue", "a" * 100, 123):
            with self.subTest(value=value), patch.object(GallerySource, "_records", return_value=iter([{**record, "version": value}])), self.assertRaises(GallerySourceError):
                GallerySource.version()

    def test_real_source_helper_reports_version_without_gui_gallery_import(self):
        command = "import sys,json;sys.path.insert(0,'desktop');from gallery_client import GallerySource;v=GallerySource.version();print(json.dumps([v,any(k=='gallery_dl' or k.startswith('gallery_dl.') for k in sys.modules)]))"
        result = subprocess.run([sys.executable, "-c", command], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        version, imported = json.loads(result.stdout)
        self.assertRegex(version, r"^\d+\.\d+")
        self.assertFalse(imported)


if __name__ == "__main__":
    unittest.main()
