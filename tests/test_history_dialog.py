"""Native history behavior against a real local SQLite fixture, without network access."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from PyQt6.QtCore import QEvent, QLocale, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
import history_dialog as history
import i18n
from storage import Store


class HeldPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


class HistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.old_language, self.old_locale, self.old_palette = i18n.language_mode(), QLocale(), self.app.palette()
        i18n.set_language("zh_TW")
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name).resolve()
        self.db_path = self.root / "history.sqlite3"
        self.store = Store(self.db_path)
        self.add_media(101, "downloaded")
        self.add_media(102, "failed")
        self.add_media(103, "pending")
        self.dialog = history.HistoryDialog(db_path=self.db_path)

    def tearDown(self):
        self.dialog.close()
        self.wait_until(lambda:self.dialog._active is None)
        self.dialog.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.store.close()
        self.directory.cleanup()
        i18n.set_language(self.old_language)
        QLocale.setDefault(self.old_locale)
        i18n.install_qt_translator(self.app)
        self.app.setPalette(self.old_palette)

    def add_media(self, number, state, author="artist"):
        key = f"twitter:{number}:1"
        self.store.record_post(str(number), author, "fixture", ["art"], "search", True)
        self.store.record_media(str(number), key, "https://pbs.twimg.com/media/fixture.jpg",
                                {"extension":"jpg","num":1}, str(self.root))
        if state == "downloaded":
            path = self.root / f"{number}_1.jpg"
            path.write_bytes(b"local fixture")
            self.store.mark_success(key, str(path), path.stat().st_size, "fixture")
        elif state == "failed":
            self.store.mark_failed(key, "<b>literal failure</b>")
        return key

    def wait_until(self, predicate, seconds=4):
        deadline = time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), "History background operation did not finish")

    def loaded(self):
        self.wait_until(lambda:not self.dialog._loading and self.dialog._active is None
                        and self.dialog._pending is None and not self.dialog._debounce.isActive())

    def show(self):
        self.dialog.show()
        self.loaded()

    def select(self, key):
        row = next(index for index,item in enumerate(self.dialog._rows) if item["media_key"] == key)
        self.dialog.history_table.selectRow(row)

    def test_filter_search_pagination_and_live_database_updates(self):
        self.show()
        self.assertEqual(self.dialog.history_table.rowCount(), 3)
        self.dialog.status_combo.setCurrentIndex(self.dialog.status_combo.findData("failed"))
        self.loaded()
        self.assertEqual([row["state"] for row in self.dialog._rows], ["failed"])
        self.dialog.search_input.setText("missing")
        self.loaded()
        self.assertEqual(self.dialog._total, 0)
        self.dialog.search_input.clear()
        self.dialog.status_combo.setCurrentIndex(0)
        self.loaded()
        for number in range(104, 145):
            self.add_media(number, "pending")
        self.dialog.refresh()
        self.loaded()
        self.assertEqual((self.dialog._total, self.dialog.history_table.rowCount()), (44,40))
        QTest.mouseClick(self.dialog.next_button, Qt.MouseButton.LeftButton)
        self.loaded()
        self.assertEqual((self.dialog._page, self.dialog.history_table.rowCount()), (2,4))

    def test_retry_signal_contains_only_selected_failed_ids_and_respects_busy_gate(self):
        self.show()
        emitted = []
        self.dialog.retryRequested.connect(emitted.append)
        self.dialog.history_table.selectAll()
        self.assertTrue(self.dialog.retry_button.isEnabled())
        self.dialog.set_retry_enabled(False)
        self.dialog._retry_selected()
        self.assertEqual(emitted, [])
        self.dialog.set_retry_enabled(True)
        QTest.mouseClick(self.dialog.retry_button, Qt.MouseButton.LeftButton)
        self.assertEqual(emitted, [["twitter:102:1"]])
        self.assertEqual(self.store.media_state("twitter:102:1")["state"], "failed")
        self.assertEqual(self.store.media_state("twitter:101:1")["state"], "downloaded")

    def test_open_actions_are_explicit_and_revalidate_missing_files(self):
        with patch.object(history.QDesktopServices, "openUrl") as opened:
            self.show()
            opened.assert_not_called()
            self.select("twitter:101:1")
            QTest.mouseClick(self.dialog.post_button, Qt.MouseButton.LeftButton)
            self.assertEqual(opened.call_args[0][0].toString(), "https://x.com/i/web/status/101")
            QTest.mouseClick(self.dialog.file_button, Qt.MouseButton.LeftButton)
            self.assertEqual(Path(opened.call_args[0][0].toLocalFile()), self.root / "101_1.jpg")
            QTest.mouseClick(self.dialog.folder_button, Qt.MouseButton.LeftButton)
            self.assertEqual(Path(opened.call_args[0][0].toLocalFile()), self.root)
            (self.root / "101_1.jpg").unlink()
            opened.reset_mock()
            QTest.mouseClick(self.dialog.file_button, Qt.MouseButton.LeftButton)
            opened.assert_not_called()
            self.assertIn("不存在", self.dialog.message_label.text())

    def test_language_theme_preserve_selection_and_external_text_stays_literal(self):
        self.show()
        self.select("twitter:102:1")
        self.assertIn("<b>literal failure</b>", self.dialog.detail_label.text())
        self.assertEqual(self.dialog.detail_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("&lt;b&gt;", self.dialog.detail_label.toolTip())
        for language in ("en","ja","ko","zh_CN","zh_TW"):
            i18n.set_language(language)
            self.dialog.refresh_language()
            for theme in ("light","dark"):
                self.dialog.set_theme(theme)
                self.assertEqual(self.dialog.retry_button.text(), i18n.tr("重試選取的失敗項目"))
                self.assertEqual(self.dialog._selected_media()[0]["media_key"], "twitter:102:1")
                palette = self.dialog.palette()
                self.assertNotEqual(palette.color(QPalette.ColorRole.Window), palette.color(QPalette.ColorRole.WindowText))

    def test_jobs_show_persisted_incomplete_sources_before_completed_sources(self):
        sources = [{"key":key,"kind":kind,"url":"https://x.com/artist/media","name":key,"data":{}}
                   for key,kind in (("done","author"),("partial","thread"),("queued","search"))]
        job_id = self.store.create_job("fixture", {"tags":["art"]}, sources)["job_id"]
        self.store.set_source_state(job_id, "done", "completed", progress={"records":2})
        self.store.set_source_state(job_id, "partial", "partial", "fixed message", {"records":3})
        self.show()
        self.dialog.tabs.setCurrentIndex(1)
        self.loaded()
        self.assertEqual(len(self.dialog._jobs), 1)
        self.assertEqual([row["key"] for row in self.dialog._sources], ["partial","queued","done"])
        self.assertEqual(self.dialog.sources_table.item(0,3).text(), i18n.tr("已讀取 {count} 筆",count=3))
        self.assertFalse(self.dialog.retry_button.isVisible())
        self.store.set_source_state(job_id, "partial", "partial", progress={"records":5})
        self.dialog.refresh()
        self.loaded()
        self.assertEqual([row["key"] for row in self.dialog._sources], ["partial","queued","done"])
        self.assertEqual(self.dialog.sources_table.item(0,3).text(), i18n.tr("已讀取 {count} 筆",count=5))

    def test_close_drops_late_queries_and_reopen_refreshes_without_accumulating_work(self):
        pool = HeldPool()
        self.dialog._pool = pool
        self.dialog.show()
        self.dialog._new_search()
        self.dialog._new_search()
        self.assertEqual(len(pool.tasks), 1)
        self.assertIsNotNone(self.dialog._pending)
        self.dialog.close()
        pool.tasks[0].run()
        self.assertEqual(self.dialog.history_table.rowCount(), 0)
        self.assertEqual(len(pool.tasks), 1)
        self.dialog.show()
        self.assertEqual(len(pool.tasks), 2)
        pool.tasks[1].run()
        self.assertEqual(self.dialog.history_table.rowCount(), 3)
        self.assertIsNone(self.dialog._active)


if __name__ == "__main__":
    unittest.main()
