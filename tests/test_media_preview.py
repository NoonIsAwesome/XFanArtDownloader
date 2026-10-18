"""Local-only bounded preview decoding, pending-work and lifetime checks."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
import media_preview as preview


class HeldPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name)
        self.output = self.root / "downloads"
        self.output.mkdir()
        self.paths = []
        for index in range(3):
            path = self.output / f"fixture{index}.png"
            image = QImage(32, 16, QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.cyan)
            self.assertTrue(image.save(str(path)))
            self.paths.append(path)

    def tearDown(self):
        self.directory.cleanup()

    def test_decoder_rejects_external_paths_large_files_pixels_and_bad_content(self):
        self.assertIsNone(preview.preview_signature("https://example.com/a.png", self.output))
        self.assertIsNone(preview.preview_signature(self.root / "missing.png", self.output))
        outside = self.root / "outside.png"
        outside.write_bytes(self.paths[0].read_bytes())
        self.assertIsNone(preview.preview_signature(outside, self.output))
        signature = preview.preview_signature(self.paths[0], self.output)
        with patch.object(preview, "MAX_PIXELS", 100):
            self.assertIsNone(preview.decode_preview(signature, threading.Event()))
        with patch.object(preview, "MAX_FILE_BYTES", 8):
            self.assertIsNone(preview.preview_signature(self.paths[0], self.output))
        self.paths[0].write_bytes(b"not an image")
        self.assertIsNone(preview.decode_preview(preview.preview_signature(self.paths[0], self.output), threading.Event()))

    def test_only_latest_pending_decode_is_kept_and_late_results_are_ignored(self):
        pool = HeldPool()
        loader = preview.MediaPreviewLoader(pool=pool)
        images = []
        loader.ready.connect(images.append)
        loader.load(self.paths[0], self.output)
        loader.load(self.paths[1], self.output)
        loader.load(self.paths[2], self.output)
        self.assertEqual(len(pool.tasks), 1)
        pool.tasks[0].run()
        self.assertEqual(images, [])
        self.assertEqual(len(pool.tasks), 2)
        pool.tasks[1].run()
        self.assertEqual(len(images), 1)
        self.assertIsInstance(images[0], QImage)
        loader.load(self.paths[2], self.output)
        self.assertEqual(len(pool.tasks), 2)
        self.assertEqual(len(images), 2)
        loader.close()

    def test_changed_or_cancelled_file_is_not_decoded_and_close_does_not_restart_pending(self):
        signature = preview.preview_signature(self.paths[0], self.output)
        self.paths[0].write_bytes(b"changed")
        self.assertIsNone(preview.decode_preview(signature, threading.Event()))
        pool = HeldPool()
        loader = preview.MediaPreviewLoader(pool=pool)
        images = []
        loader.ready.connect(images.append)
        loader.load(self.paths[1], self.output)
        loader.load(self.paths[2], self.output)
        loader.close()
        pool.tasks[0].run()
        self.assertEqual(len(pool.tasks), 1)
        self.assertEqual(images, [])

    def test_cached_image_supersedes_a_different_active_and_pending_decode(self):
        pool = HeldPool()
        loader = preview.MediaPreviewLoader(pool=pool)
        images = []
        loader.ready.connect(images.append)
        loader.load(self.paths[0], self.output)
        pool.tasks[0].run()
        cached = images[-1]
        loader.load(self.paths[1], self.output)
        loader.load(self.paths[2], self.output)
        loader.load(self.paths[0], self.output)
        self.assertIs(images[-1], cached)
        self.assertTrue(pool.tasks[1].cancelled.is_set())
        pool.tasks[1].run()
        self.assertEqual(len(pool.tasks), 2)
        self.assertEqual(images, [cached, cached])
        loader.close()

    def test_real_qt_pool_returns_scaled_image_to_gui_thread(self):
        loader = preview.MediaPreviewLoader()
        images, threads = [], []
        gui_thread = threading.get_ident()
        loader.ready.connect(lambda image: (images.append(image), threads.append(threading.get_ident())))
        loader.load(self.paths[0], self.output)
        deadline = time.monotonic() + 3
        while not images and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertEqual(len(images), 1)
        self.assertEqual(threads, [gui_thread])
        self.assertLessEqual(images[0].width(), 520)
        self.assertLessEqual(images[0].height(), 320)
        loader.close()
        loader.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
