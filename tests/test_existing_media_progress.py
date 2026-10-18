"""Verified existing files remain visible without downloads or completion rewrites."""
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication
import author_avatar
import i18n
import main
import media_preview
from downloader import Cancelled, DownloadEngine, validate_file
from storage import Store
from tests import test_downloader as fixture
from tests.test_downloader import Session, Source, media
from tests.test_author_avatar import FakeManager, URL, png


class ExistingBackendTests(unittest.TestCase):
    setUp = fixture.EngineTests.setUp
    tearDown = fixture.EngineTests.tearDown
    options = fixture.EngineTests.options
    run_engine = fixture.EngineTests.run_engine

    def test_verified_skip_publishes_author_and_local_path_without_network_or_state_rewrite(self):
        url, metadata = media()
        metadata["author"].update(nick="Noon\x00 public", profile_image=URL)
        self.run_engine(Source([(url, metadata)]))
        with Store(self.root / "state.sqlite3") as store:
            row = store.media_state("twitter:1001:1")
            before = dict(row)
            events, session = [], Session()
            engine = DownloadEngine(self.options(), source=Source([]), session=session, progress=events.append, log=self.logs.append)
            engine.store = store
            with patch.object(store, "mark_success", side_effect=AssertionError("skip must not rewrite success")), \
                    patch.object(store, "mark_failed", side_effect=AssertionError("valid skip must not fail")):
                engine._download(row)
            self.assertEqual(store.media_state(row["media_key"]), before)
        self.assertEqual((engine.result.downloaded, engine.result.skipped, session.urls), (0, 1, []))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["state"], events[0]["author_name"], events[0]["author_handle"]),
                         ("skipped", "Noon public", "artista"))
        self.assertEqual((events[0]["avatar_url"], events[0]["path"]), (URL, str(Path(row["path"]).resolve())))
        self.assertIn("⏭️ 🖼️ 已存在，略過：artista\\1001_1.png", self.logs)
        self.assertFalse(any(str(self.root) in line for line in self.logs))

    def test_video_skip_uses_saved_filename_and_short_folder_setting(self):
        url, metadata = media()
        metadata.update(extension="mp4", type="video")
        box = lambda kind, data: (8 + len(data)).to_bytes(4, "big") + kind + data
        body = box(b"ftyp", b"isom0000") + box(b"moov", b"metadata") + box(b"mdat", b"content")
        self.run_engine(Source([(url, metadata)]), Session(body), folder_by_user=False)
        with Store(self.root / "state.sqlite3") as store:
            row = store.media_state("twitter:1001:1")
            original = Path(row["path"])
            renamed = original.with_name("renamed.mp4")
            original.rename(renamed)
            store.mark_success(row["media_key"], str(renamed), *validate_file(renamed, "mp4"))
        events, session = [], Session()
        result = DownloadEngine(self.options(folder_by_user=False), source=Source([(url, metadata)]), session=session,
                                progress=events.append, log=self.logs.append).run()
        self.assertEqual((result.downloaded, result.skipped, session.urls), (0, 1, []))
        self.assertEqual((events[0]["state"], events[0]["media_type"], events[0]["filename"]), ("skipped", "video", "renamed.mp4"))
        self.assertIn("⏭️ 🎬 已存在，略過：renamed.mp4", self.logs)

    def test_stop_during_validation_does_not_publish_existing_file(self):
        self.run_engine(Source([media()]))
        event, events = threading.Event(), []
        with Store(self.root / "state.sqlite3") as store:
            row = store.media_state("twitter:1001:1")
            engine = DownloadEngine(self.options(), source=Source([]), session=Session(), stop_event=event,
                                    progress=events.append, log=self.logs.append)
            engine.store = store
            def stopping(path, extension):
                verified = validate_file(path, extension)
                event.set()
                return verified
            with patch("downloader.validate_file", side_effect=stopping), self.assertRaises(Cancelled):
                engine._download(row)
        self.assertEqual((events, engine.result.skipped), ([], 0))

    def test_completed_resume_source_is_explicitly_logged_without_rescanning(self):
        source = Source(lambda url: [media(), media("1002")] if "/search?" in url else [media("2001")])
        self.run_engine(source, limit_count=1, authors=("artista",))
        self.logs.clear()
        resumed_source = Source(lambda url: [media(), media("1002")] if "/search?" in url else [media("2001")])
        result = self.run_engine(resumed_source, limit_count=1, authors=("artista",))
        self.assertTrue(result.resumed)
        self.assertGreaterEqual(result.sources_skipped, 1)
        self.assertNotIn("https://x.com/artista/media", resumed_source.urls)
        self.assertTrue(any(line.startswith("已完成來源，略過重新掃描：") for line in self.logs))


class ExistingGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture = fixture.EngineTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.previous_language = i18n.language_mode()
        self.palette = self.app.palette()
        (self.root / "data").mkdir()
        (self.root / "data/settings.json").write_text(json.dumps({"language":"zh_TW","auto_driver_update":False}), encoding="utf-8")
        self.manager_patch = patch.object(author_avatar, "QNetworkAccessManager", FakeManager)
        self.manager_patch.start()
        self.preview_load = self.enterContext(patch.object(media_preview.MediaPreviewLoader, "load"))
        self.preview_decode = self.enterContext(patch.object(media_preview, "decode_preview"))
        self.window = main.TwitterDownloader(show_notice=False, base_dir=self.root)
        self.window._download_worker = object()
        self.window.is_downloading = True
        self.output = self.root / "downloads"
        self.output.mkdir()
        self.image = self.output / "fixture.png"
        self.image.write_bytes(png())

    def tearDown(self):
        self.window.is_downloading = False
        self.window._download_worker = None
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.manager_patch.stop()
        i18n.set_language(self.previous_language)
        i18n.install_qt_translator(self.app)
        self.app.setPalette(self.palette)
        self.fixture.tearDown()

    def event(self, **kwargs):
        return {"type":"media", "state":"skipped", "author_handle":"artist", "author_name":"Noon public",
                "avatar_url":URL, "filename":"fixture.png", "display_path":"artist\\fixture.png",
                "media_type":"image", "path":str(self.image), **kwargs}

    def assert_no_inline_preview(self):
        for name in ("_preview_loader", "_preview_kind", "_preview_image", "_preview_output_root",
                     "current_preview_container", "current_preview_label", "current_preview_caption"):
            self.assertFalse(hasattr(self.window, name), name)
        self.preview_load.assert_not_called()
        self.preview_decode.assert_not_called()

    def test_skipped_event_keeps_public_author_file_and_avatar_without_new_download_count(self):
        window = self.window
        window._media_progress(window._download_worker, self.event())
        self.assertEqual(window.current_author_label.literal_text, "Noon public · @artist")
        self.assertEqual(window.current_file_label.literal_text, "artist\\fixture.png")
        self.assertEqual(window._current_media["state"], "skipped")
        self.assertEqual(window.successful_downloads, 0)
        self.assertEqual(len(window._avatar_loader.manager.requests), 1)
        window._avatar_loader.manager.replies[-1].complete(png())
        self.app.processEvents()
        self.assertIsInstance(window._avatar_image, QImage)
        self.assert_no_inline_preview()

    def test_previous_worker_stop_and_new_media_preserve_current_progress_boundaries(self):
        window = self.window
        window._media_progress(object(), self.event())
        self.assertIsNone(window._current_media)
        self.assertEqual(window._avatar_loader.manager.requests, [])
        window._media_progress(window._download_worker, self.event())
        window._media_progress(window._download_worker, self.event(state="downloading", filename="new.png", display_path="artist\\new.png"))
        self.assertEqual(window.current_file_label.literal_text, "artist\\new.png")
        self.assertEqual(window._current_media["state"], "downloading")
        reply = window._avatar_loader.manager.replies[-1]
        window.stop_download()
        self.assertTrue(reply.aborted)
        window._media_progress(window._download_worker, self.event())
        self.assertEqual(window._current_media["filename"], "new.png")
        self.assertEqual(window._current_media["state"], "downloading")
        self.assertEqual(len(window._avatar_loader.manager.requests), 1)
        self.assert_no_inline_preview()

    def test_all_media_states_and_paths_keep_file_progress_without_main_window_decoding(self):
        window = self.window
        outside = self.root / "outside.png"
        outside.write_bytes(png())
        for state in ("downloading", "downloaded", "skipped"):
            for kind, path in (("image", str(self.image)), ("image", str(outside)),
                               ("image", "https://example.com/fixture.png"), ("video", str(self.image))):
                with self.subTest(state=state, kind=kind, path=path):
                    window._media_progress(window._download_worker, self.event(state=state, media_type=kind, path=path))
                    self.assertEqual(window._current_media["state"], state)
                    self.assertEqual(window.current_author_label.literal_text, "Noon public · @artist")
                    self.assertEqual(window.current_file_label.literal_text, "artist\\fixture.png")
                    self.assertEqual(window.successful_downloads, 0)
                    self.assert_no_inline_preview()

    def test_open_history_follows_live_warp_and_port_changes_without_startup_avatar_requests(self):
        window = self.window
        window.show_download_history()
        dialog = window._history_dialog
        with patch.object(dialog, "configure_route", wraps=dialog.configure_route) as route:
            window.warp_proxy_checkbox.setChecked(True)
            route.assert_called_with(True, 40000)
            window.warp_proxy_port_input.setValue(40001)
            route.assert_called_with(True, 40001)
            window.warp_proxy_checkbox.setChecked(False)
            route.assert_called_with(False, 40001)
        self.assertEqual(dialog._avatar_loader.manager.requests, [])
        dialog.close()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
