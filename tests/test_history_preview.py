"""Synthetic history records and local rasters; no real database or network fixtures."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QObject, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtNetwork import QNetworkReply, QNetworkProxy
from PyQt6.QtWidgets import QApplication
import author_avatar
import history_dialog as history
import i18n
import media_preview
import storage

AVATAR = "https://pbs.twimg.com/profile_images/123/synthetic.png"


class HeldPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


class FakeReply(QObject):
    metaDataChanged = pyqtSignal()
    readyRead = pyqtSignal()
    downloadProgress = pyqtSignal(int, int)
    finished = pyqtSignal()

    def __init__(self, url, parent):
        super().__init__(parent)
        self.address, self.data, self.aborted = QUrl(url), bytearray(), False

    def setReadBufferSize(self, _value):
        pass

    def header(self, _header):
        return None

    def attribute(self, _attribute):
        return 200

    def read(self, count):
        data = bytes(self.data[:count])
        del self.data[:count]
        return QByteArray(data)

    def bytesAvailable(self):
        return len(self.data)

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def url(self):
        return self.address

    def abort(self):
        self.aborted = True
        self.finished.emit()

    def complete(self, data):
        self.data.extend(data)
        self.readyRead.emit()
        self.finished.emit()


class FakeManager(QObject):
    authenticationRequired = pyqtSignal(object, object)
    proxyAuthenticationRequired = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests, self.replies = [], []

    def setCookieJar(self, jar):
        self.jar = jar

    def clearConnectionCache(self):
        pass

    def clearAccessCache(self):
        pass

    def setProxy(self, proxy):
        self.proxy = proxy

    def get(self, request):
        self.requests.append(request)
        reply = FakeReply(request.url(), self)
        self.replies.append(reply)
        return reply


def png_bytes():
    image = QImage(48, 32, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.cyan)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class HistoryPresentationStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name).resolve()
        self.db = self.root / "synthetic.sqlite3"
        self.store = storage.Store(self.db)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def record(self, metadata, number="123", filename="image.png"):
        self.store.record_post(number, "artist", "private post body", [], "fixture", True)
        key = f"twitter:{number}:1"
        self.store.record_media(number, key, "https://example.invalid/never-preview", metadata, str(self.root))
        path = self.root / filename
        path.write_bytes(png_bytes())
        self.store.mark_success(key, str(path), path.stat().st_size, "synthetic")
        return key

    def test_only_sanitized_actual_author_fields_leave_read_only_query(self):
        key = self.record({"extension":"png", "author":{"nick":"<b>Art</b>\x00\nName", "profile_image":AVATAR},
                           "user":{"nick":"Wrong author"}, "cookies":"secret-cookie", "headers":{"Authorization":"secret"}})
        self.store.record_author("artist", "Stored name")
        before = self.store.media_state(key)
        row = storage.fetch_history(self.db)["items"][0]
        self.assertEqual((row["author_name"], row["avatar_url"], row["media_type"]), ("<b>Art</b>Name", AVATAR, "image"))
        self.assertTrue(row["preview_allowed"])
        self.assertNotIn("metadata", row)
        self.assertNotIn("presentation", row)
        self.assertNotIn("url", row)
        self.assertNotIn("secret", json.dumps(row))
        self.assertNotIn("private post body", json.dumps(row))
        self.assertEqual(self.store.media_state(key), before)

    def test_unrelated_timeline_user_and_invalid_avatar_are_not_used(self):
        self.record({"extension":"png", "user":{"nick":"Timeline author", "profile_image":AVATAR},
                     "author":{"profile_image":"https://pbs.twimg.com/media/private.png"}})
        self.store.record_author("artist", "Stored\x00 Name")
        row = storage.fetch_history(self.db)["items"][0]
        self.assertEqual(row["author_name"], "Stored Name")
        self.assertIsNone(row["avatar_url"])

    def test_legacy_optional_author_table_and_malformed_metadata_are_safe(self):
        self.record({"extension":"png"})
        self.store._db.execute("UPDATE media SET metadata = ?", ("{invalid",))
        self.store._db.execute("DROP TABLE authors")
        self.store._db.commit()
        row = storage.fetch_history(self.db)["items"][0]
        self.assertEqual((row["author_name"], row["avatar_url"], row["media_type"]), ("", None, "image"))

    def test_video_file_suffix_takes_precedence_over_image_metadata(self):
        self.record({"extension":"png"}, filename="movie.webm")
        self.assertEqual(storage.fetch_history(self.db)["items"][0]["media_type"], "video")

    def test_reparse_file_or_output_root_never_becomes_preview_boundary(self):
        self.record({"extension":"png"})
        original_lstat = Path.lstat
        for unsafe in (self.root, self.root / "image.png"):
            with self.subTest(unsafe=unsafe):
                def changed(path, *args, **kwargs):
                    value = original_lstat(path, *args, **kwargs)
                    return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400) if path == unsafe else value
                with patch.object(Path, "lstat", changed):
                    self.assertFalse(storage.fetch_history(self.db)["items"][0]["preview_allowed"])


class HistoryPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.old_language, self.old_palette = i18n.language_mode(), self.app.palette()
        i18n.set_language("zh_TW")
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name).resolve()
        self.rows = []
        for index, kind in enumerate(("image", "image", "video"), 1):
            path = self.root / f"{index}.{'mp4' if kind == 'video' else 'png'}"
            path.write_bytes(png_bytes())
            self.rows.append({"media_key":f"twitter:{index}:1", "post_id":str(index), "state":"downloaded", "author":"artist",
                "author_name":"<b>Synthetic Artist</b>", "avatar_url":AVATAR.replace("123", str(index)), "media_type":kind,
                "filename":path.name, "path":str(path), "target_dir":str(self.root), "safe_file_path":str(path),
                "safe_folder_path":str(self.root), "file_exists":True, "preview_allowed":True, "error":"",
                "downloaded_at":"", "updated_at":"", "latest_redownload":None})
        self.manager_patch = patch.object(author_avatar, "QNetworkAccessManager", FakeManager)
        self.manager_patch.start()
        self.query_patch = patch.object(storage, "fetch_history", side_effect=lambda *_args, **_kw:
                                        {"items":copy.deepcopy(self.rows), "page":1, "total":len(self.rows)})
        self.query_patch.start()
        self.dialog = history.HistoryDialog(db_path=self.root / "absent.sqlite3")
        self.query_pool, self.decode_pool = HeldPool(), HeldPool()
        self.dialog._pool, self.dialog._preview_loader._pool = self.query_pool, self.decode_pool
        self.dialog.show()
        self.query_pool.tasks[-1].run()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.query_patch.stop()
        self.manager_patch.stop()
        self.directory.cleanup()
        i18n.set_language(self.old_language)
        self.app.setPalette(self.old_palette)

    def select(self, index):
        self.dialog.history_table.selectRow(index)

    def test_single_completed_selection_shows_literal_name_handle_avatar_and_local_image(self):
        with patch.object(history.QDesktopServices, "openUrl") as opened:
            self.assertEqual(self.dialog._avatar_loader.manager.requests, [])
            self.select(0)
            self.assertEqual(self.dialog.preview_author_label.text(), "<b>Synthetic Artist</b>")
            self.assertEqual(self.dialog.preview_handle_label.text(), "@artist")
            self.assertEqual(self.dialog.preview_author_label.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(self.dialog._preview_kind, "loading")
            self.assertIn("未提供頭像", self.dialog.preview_avatar_label.toolTip())
            self.decode_pool.tasks[0].run()
            self.dialog._avatar_loader.manager.replies[0].complete(png_bytes())
            self.assertEqual(self.dialog._preview_kind, "image")
            self.assertIsInstance(self.dialog._preview_image, QImage)
            self.assertIsInstance(self.dialog._avatar_image, QImage)
            self.assertEqual(self.dialog.preview_avatar_label.toolTip(), i18n.tr("作者公開頭像"))
            self.assertEqual(len(self.dialog._avatar_loader.manager.requests), 1)
            opened.assert_not_called()
            self.assertFalse((self.root / "absent.sqlite3").exists())

    def test_changed_row_rejects_late_avatar_and_decode_results(self):
        self.select(0)
        first = self.dialog._avatar_loader.manager.replies[0]
        task = self.decode_pool.tasks[0]
        self.select(1)
        self.assertTrue(first.aborted)
        self.assertTrue(task.cancelled.is_set())
        first.complete(png_bytes())
        task.run()
        self.assertIsNone(self.dialog._avatar_image)
        self.assertIsNone(self.dialog._preview_image)
        self.assertEqual(len(self.decode_pool.tasks), 2)
        self.decode_pool.tasks[1].run()
        self.dialog._avatar_loader.manager.replies[1].complete(png_bytes())
        self.assertEqual(self.dialog._preview_key[0], "twitter:2:1")
        self.assertEqual(self.dialog._preview_kind, "image")

    def test_multiple_none_and_failed_selection_cancel_both_loaders(self):
        self.dialog._rows[2]["state"] = "failed"
        for action in (self.dialog.history_table.selectAll, self.dialog.history_table.clearSelection,
                       lambda:self.select(2)):
            with self.subTest(action=action):
                self.select(0)
                task = self.decode_pool.tasks[-1]
                reply = self.dialog._avatar_loader.manager.replies[-1]
                action()
                self.assertTrue(task.cancelled.is_set())
                self.assertTrue(reply.aborted)
                task.run()
                reply.complete(png_bytes())
                self.assertIsNone(self.dialog._preview_key)
                self.assertIsNone(self.dialog._preview_image)
                self.assertTrue(self.dialog.preview_author_row.isHidden())

    def test_missing_file_keeps_author_and_public_avatar_without_loading_media(self):
        Path(self.dialog._rows[0]["path"]).unlink()
        self.select(0)
        self.assertEqual(self.dialog._preview_kind, "unavailable")
        self.assertEqual(self.decode_pool.tasks, [])
        self.assertEqual(len(self.dialog._avatar_loader.manager.requests), 1)
        self.assertEqual(self.dialog.preview_handle_label.text(), "@artist")

    def test_large_video_is_explicit_and_never_decoded_or_played(self):
        path = Path(self.dialog._rows[2]["path"])
        with path.open("r+b") as stream:
            stream.truncate(media_preview.MAX_FILE_BYTES + 1)
        with patch.object(history.QDesktopServices, "openUrl") as opened:
            self.select(2)
            self.assertEqual(self.dialog._preview_kind, "video")
            self.assertIn("不會自動播放", self.dialog.preview_status_label.text())
            self.assertEqual(self.decode_pool.tasks, [])
            self.assertEqual(len(self.dialog._avatar_loader.manager.requests), 1)
            opened.assert_not_called()

    def test_short_window_selection_reveals_preview_and_open_actions(self):
        self.dialog.resize(800, 590)
        self.select(0)
        self.app.processEvents()
        viewport = self.dialog.tabs.widget(0).viewport()
        for widget in (self.dialog.preview_author_label, self.dialog.preview_image_label, self.dialog.file_button):
            with self.subTest(widget=widget):
                top = widget.mapTo(viewport, widget.rect().topLeft())
                bottom = widget.mapTo(viewport, widget.rect().bottomRight())
                self.assertGreaterEqual(top.y(), 0)
                self.assertLess(bottom.y(), viewport.height())

    def test_remote_media_and_unsafe_reparse_snapshot_cannot_start_decode(self):
        for path, root in (("https://pbs.twimg.com/media/never.png", str(self.root)),
                           (str(self.root / "1.png"), str(self.root))):
            with self.subTest(path=path):
                self.dialog.history_table.clearSelection()
                self.dialog._rows[0].update(safe_file_path=path, safe_folder_path=root, preview_allowed=False)
                self.select(0)
                self.assertEqual(self.dialog._preview_kind, "unavailable")
                self.assertEqual(self.decode_pool.tasks, [])

    def test_filter_hide_reject_and_tab_change_make_pending_results_harmless(self):
        for action in (self.dialog.refresh, self.dialog.hide, self.dialog.reject,
                       lambda:self.dialog.tabs.setCurrentIndex(1)):
            with self.subTest(action=action):
                self.dialog.tabs.setCurrentIndex(0)
                self.dialog.show()
                if self.dialog._active is not None:
                    self.query_pool.tasks[-1].run()
                if self.dialog._active is not None:
                    self.query_pool.tasks[-1].run()
                self.dialog.history_table.clearSelection()
                self.select(0)
                task, reply = self.decode_pool.tasks[-1], self.dialog._avatar_loader.manager.replies[-1]
                action()
                self.assertTrue(task.cancelled.is_set())
                self.assertTrue(reply.aborted)
                task.run()
                reply.complete(png_bytes())
                self.assertIsNone(self.dialog._preview_key)

    def test_route_language_and_theme_refresh_preserve_selection_and_preview(self):
        self.select(0)
        old = self.dialog._avatar_loader.manager.replies[-1]
        self.dialog.configure_route(True, 40001)
        self.assertTrue(old.aborted)
        self.assertEqual(self.dialog._avatar_loader.manager.proxy.type(), QNetworkProxy.ProxyType.Socks5Proxy)
        self.assertEqual(self.dialog._avatar_loader.manager.proxy.port(), 40001)
        requests = len(self.dialog._avatar_loader.manager.requests)
        active = self.dialog._preview_loader._pending
        self.dialog.configure_route(True, 40001)
        self.assertEqual(len(self.dialog._avatar_loader.manager.requests), requests)
        self.assertEqual(self.dialog._preview_loader._pending, active)
        for language in ("zh_TW", "en", "ja", "ko", "zh_CN"):
            i18n.set_language(language)
            self.dialog.retranslate_ui()
            for theme in ("light", "dark"):
                self.dialog.set_theme(theme)
                self.assertEqual(self.dialog._preview_key[0], "twitter:1:1")
                self.assertEqual(self.dialog.preview_handle_label.text(), "@artist")
                self.assertIn(history.theme_colors(theme)["log"], self.dialog.preview_image_label.styleSheet())
                self.assertEqual(self.dialog.preview_image_label.height(), 132)
        self.assertEqual(len(self.dialog._avatar_loader.manager.requests), requests)
        old = self.dialog._avatar_loader.manager.replies[-1]
        self.dialog.configure_route(False, 40001)
        self.assertTrue(old.aborted)
        self.assertEqual(self.dialog._avatar_loader.manager.proxy.type(), QNetworkProxy.ProxyType.DefaultProxy)
        self.assertEqual(len(self.dialog._avatar_loader.manager.requests), requests + 1)

    def test_close_then_reopen_reuses_live_loaders_and_can_preview_again(self):
        self.select(0)
        first = self.decode_pool.tasks[0]
        self.dialog.close()
        self.assertTrue(first.cancelled.is_set())
        self.assertFalse(self.dialog._avatar_loader._closed)
        self.assertFalse(self.dialog._preview_loader._closed)
        first.run()
        self.dialog.show()
        self.query_pool.tasks[-1].run()
        self.select(0)
        self.decode_pool.tasks[-1].run()
        self.dialog._avatar_loader.manager.replies[-1].complete(png_bytes())
        self.assertEqual(self.dialog._preview_kind, "image")
        self.assertIsInstance(self.dialog._avatar_image, QImage)

    def test_destroyed_dialog_closes_loaders_and_worker_does_not_keep_window_alive(self):
        self.select(0)
        avatar, preview, task = self.dialog._avatar_loader, self.dialog._preview_loader, self.decode_pool.tasks[0]
        self.dialog.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(avatar._closed)
        self.assertTrue(preview._closed)
        self.assertTrue(task.cancelled.is_set())
        task.run()
        # Supply a harmless replacement so common cleanup never touches a deleted QObject.
        self.dialog = history.HistoryDialog(db_path=self.root / "absent.sqlite3")


if __name__ == "__main__":
    unittest.main()
