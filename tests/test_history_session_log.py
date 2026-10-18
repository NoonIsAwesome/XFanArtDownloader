"""Bounded canonical session logs and compact feedback using synthetic offline state."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QEvent, QLocale
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
import author_avatar
import history_dialog
import i18n
import main
from tests.test_history_preview import FakeManager


class HistorySessionLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.language, self.locale, self.palette = i18n.language_mode(), QLocale(), self.app.palette()
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name).resolve()
        (self.root / "data").mkdir()
        (self.root / "data/settings.json").write_text(json.dumps({"language":"zh_TW", "auto_driver_update":False}), encoding="utf-8")
        self.network = patch.object(author_avatar, "QNetworkAccessManager", FakeManager)
        self.network.start()
        self.window = main.TwitterDownloader(show_notice=False, base_dir=self.root)

    def tearDown(self):
        self.window.close()
        dialog = self.window._history_dialog
        if dialog is not None:
            self.wait_until(lambda: dialog._active is None)
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.network.stop()
        self.directory.cleanup()
        i18n.set_language(self.language)
        QLocale.setDefault(self.locale)
        i18n.install_qt_translator(self.app)
        self.app.setPalette(self.palette)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def open_history(self):
        self.window.show_download_history()
        dialog = self.window._history_dialog
        self.wait_until(lambda: not dialog._loading and dialog._active is None)
        return dialog

    def test_two_tabs_retain_preopen_full_log_and_live_hidden_updates_without_duplicates(self):
        self.window.append_status("開始來源：#synthetic")
        self.window.append_status("✅ 🖼️ 已下載：artist\\<b>fixture</b>.png")
        self.assertNotIn("開始來源", self.window.status_box.toPlainText())
        self.assertIn("✅", self.window.status_box.toPlainText())
        dialog = self.open_history()
        self.assertEqual(dialog.tabs.count(), 2)
        self.assertEqual(dialog.tabs.tabText(0), i18n.tr("紀錄與圖片預覽"))
        self.assertEqual(dialog.tabs.tabText(1), i18n.tr("詳細下載狀況"))
        self.assertTrue(dialog.session_log.isReadOnly())
        self.assertIn("開始來源：#synthetic", dialog.session_log.toPlainText())
        self.assertIn("<b>fixture</b>.png", dialog.session_log.toPlainText())
        dialog.tabs.setCurrentIndex(1)
        self.window.append_status("已完成來源，略過重新掃描：@artist")
        self.assertIn("已完成來源，略過重新掃描", dialog.session_log.toPlainText())
        dialog.close()
        self.window.append_status("開始來源：hidden synthetic")
        self.assertIn("hidden synthetic", dialog.session_log.toPlainText())
        reopened = self.open_history()
        self.assertIs(reopened, dialog)
        self.assertEqual(len(dialog._session_entries), len(self.window._status_entries))
        self.assertEqual(dialog.session_log.toPlainText().count("hidden synthetic"), 1)
        self.assertTrue(hasattr(dialog, "jobs_table") and hasattr(dialog, "sources_table"))

    def test_only_explicit_source_chatter_is_hidden_and_important_feedback_stays_visible(self):
        detailed = ("開始來源：#art", "目標標籤：#art", "已完成來源，略過重新掃描：@artist", "續跑工作 1：來源",
                    "已達本輪 20 則搜尋貼文配額；接著繼續作者補抓。", "📋 來源進度：累計完成 1、本輪沿用 2、未完成 0。")
        important = ("✅ 🖼️ 已下載：artist\\one.png", "⏭️ 🎬 已存在，略過：artist\\two.mp4",
                     "❌ synthetic error", "⚠️ synthetic warning", "開始來源相似但不是來源標記的未知訊息",
                     "本輪搜尋、作者補抓、同作者留言與媒體下載使用 WARP 本機代理；代理失敗不會切回直連。",
                     "同作者留言補抓未完成：請檢查登入或網路後重試。", "📋 仍有 1 個來源未完成；可稍後補抓。",
                     "🔓 請在專用瀏覽器中完成 X 或 Google 登入。")
        for message in (*detailed, *important):
            self.window.append_status(message)
        compact = self.window.status_box.toPlainText()
        for message in detailed:
            self.assertNotIn(i18n.translate_text(message), compact)
        for message in important:
            self.assertIn(i18n.translate_text(message), compact)
        dialog = self.open_history()
        for message in (*detailed, *important):
            self.assertIn(i18n.translate_text(message), dialog.session_log.toPlainText())

    def test_canonical_templates_retranslate_in_both_views_and_keep_external_values_literal(self):
        literal = "D:/開始來源/<b>artist</b>.png"
        self.window._log("📁 已選擇儲存路徑：{folder}", folder=literal)
        self.window.append_status("開始來源：合成來源")
        dialog = self.open_history()
        original = dialog.session_log.toPlainText()
        self.window.language_mode = "en"
        i18n.set_language("en")
        self.window.refresh_language()
        expected = i18n.tr("📁 已選擇儲存路徑：{folder}", folder=literal)
        self.assertIn(expected, dialog.session_log.toPlainText())
        self.assertIn(expected, self.window.status_box.toPlainText())
        self.assertNotIn("合成來源", self.window.status_box.toPlainText())
        self.assertIn(literal, dialog.session_log.toPlainText())
        self.window.language_mode = "zh_TW"
        i18n.set_language("zh_TW")
        self.window.refresh_language()
        self.assertEqual(dialog.session_log.toPlainText(), original)

    def test_main_and_detail_canonical_logs_and_documents_remain_bounded(self):
        dialog = self.open_history()
        for index in range(2005):
            self.window.append_status(f"開始來源：synthetic_{index}")
        self.assertEqual(len(self.window._status_entries), 2000)
        self.assertEqual(len(dialog._session_entries), 2000)
        self.assertTrue(dialog._session_entries[0][0].endswith("synthetic_5"))
        self.assertNotIn("開始來源", self.window.status_box.toPlainText())
        self.assertLessEqual(dialog.session_log.document().blockCount(), 2000)
        for index in range(2005):
            self.window.append_status(f"✅ synthetic_{index}")
        self.assertEqual(len(self.window._compact_status_entries), 2000)
        self.assertEqual(len(self.window._status_entries), 2000)
        self.assertEqual(len(dialog._session_entries), 2000)
        self.assertLessEqual(self.window.status_box.document().blockCount(), 2000)


if __name__ == "__main__":
    unittest.main()
