"""No-network checks for the UI / worker boundary and cooperative shutdown."""

import json
from contextlib import closing
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QDate, QEvent, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import main
from locale_fixture import traditional_chinese_system


def result(**values):
    defaults = dict(downloaded=0, skipped=0, failed=0, filtered=0, posts=0, errors=[], stopped=False)
    defaults.update(values)
    return SimpleNamespace(**defaults)


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        traditional_chinese_system(self)
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.window = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
        self.window.hashtag_input.setText("#noonle")  # Explicit target used by these job fixtures.

    def wait_until(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), "Background Qt operation did not finish in time")

    def tearDown(self):
        self.window.close()
        self.wait_until(lambda: self.window._download_thread is None and self.window._login_thread is None
                        and self.window._driver_thread is None and self.window._update_thread is None)
        self.app.processEvents()
        self.window.deleteLater()
        # 測試未執行 app.exec()，須明確送出延後刪除事件，先釋放原生視窗樹。
        # Without app.exec(), explicitly flush deferred deletion before removing fixture data.
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        # Windows 可能短暫保留已刪除項目；有限重試仍會揭露持續性的清理錯誤。
        # Retry only transient Windows directory-not-empty errors; persistent failures still fail.
        for delay in (0.05, 0.1, 0.2, 0.4, 0):
            try:
                self.directory.cleanup()
                break
            except OSError as error:
                if os.name != "nt" or getattr(error, "winerror", None) != 145 or not delay:
                    raise
                time.sleep(delay)

    def test_snapshot_and_source_master_switch(self):
        self.assertFalse(self.window.reply_media_checkbox.isChecked())
        self.assertTrue(self.window.search_checkbox.isChecked())
        self.assertTrue(self.window.author_media_checkbox.isChecked())
        self.assertTrue(self.window.discover_authors_checkbox.isChecked())
        self.assertTrue(self.window.retry_failed_checkbox.isChecked())
        self.assertTrue(self.window.auto_driver_checkbox.isChecked())
        self.assertIsNone(self.window._driver_thread)  # tests never auto-connect
        self.window.hashtag_input.setText(" FanArt_圖 ")
        self.window.authors_input.setPlainText("@artist\nhttps://twitter.com/other/media\n@artist")
        self.window.tweet_urls_input.setPlainText("https://x.com/artist/status/123/photo/1?s=20")
        self.window.limit_count_checkbox.setChecked(True)
        self.window.limit_count_input.setText("7")
        self.window.limit_date_checkbox.setChecked(True)
        self.window.limit_date_input.setDate(QDate(2025, 1, 2))
        options = self.window.snapshot_options()
        self.assertEqual(options.hashtag, "#FanArt_圖")
        self.assertEqual(options.authors, ("artist", "other"))
        self.assertEqual(options.tweet_urls, ("https://x.com/artist/status/123",))
        self.assertEqual(options.limit_count, 7)
        self.assertEqual(options.since.isoformat(), "2025-01-02")
        self.assertEqual(options.database_path, Path(self.directory.name) / "data" / "downloads.sqlite3")
        self.assertIsNone(options.cookie_file)
        self.window.hashtag_input.setText("#changed")
        self.assertEqual(options.hashtag, "#FanArt_圖")
        self.window.author_media_checkbox.setChecked(False)
        disabled = self.window.snapshot_options()
        self.assertEqual(disabled.authors, ())
        self.assertFalse(disabled.discover_authors)
        self.assertFalse(self.window.authors_input.isEnabled())

    def test_legacy_reply_default_is_reset_once_and_new_opt_in_is_preserved(self):
        self.window.data_dir.mkdir(exist_ok=True)
        self.window.settings_file.write_text(json.dumps({"include_author_replies": True,
            "hashtag": "#saved", "delay": False}), encoding="utf-8")
        self.window._load_settings()
        self.assertFalse(self.window.reply_media_checkbox.isChecked())
        self.assertFalse(self.window.snapshot_options().include_author_replies)
        self.assertEqual(self.window.hashtag_input.text(), "#saved")
        self.assertFalse(self.window.human_behavior_checkbox.isChecked())
        self.assertIn("（會多花一些時間載入）", self.window.reply_media_checkbox.text())
        self.window.reply_media_checkbox.setChecked(True)
        self.window._save_settings()
        self.window.reply_media_checkbox.setChecked(False)
        self.window._load_settings()
        self.assertTrue(self.window.reply_media_checkbox.isChecked())
        self.assertTrue(self.window.snapshot_options().include_author_replies)

    def test_fresh_target_is_blank_and_saved_legacy_target_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            fresh = main.TwitterDownloader(show_notice=False, base_dir=Path(directory))
            try:
                self.assertEqual(fresh.hashtag_input.text(), "")
                self.assertEqual(fresh.hashtag_input.placeholderText(), "#角色名稱 #作品標籤")
                with self.assertRaises(ValueError):
                    fresh.snapshot_options()
                fresh.hashtag_input.setText("#LegacyTarget")
                fresh._save_settings()
            finally:
                fresh.close()
                fresh.deleteLater()
            reopened = main.TwitterDownloader(show_notice=False, base_dir=Path(directory))
            try:
                self.assertEqual(reopened.hashtag_input.text(), "#LegacyTarget")
                self.assertEqual(reopened.snapshot_options().hashtags, ("legacytarget",))
            finally:
                reopened.close()
                reopened.deleteLater()

    def test_validation_rejects_invalid_values_without_starting_a_worker(self):
        for tag in ("", "#one OR #two", "##tag", "https://x.com"):
            self.window.hashtag_input.setText(tag)
            with self.assertRaises(ValueError):
                self.window.snapshot_options()
        self.window.hashtag_input.setText("#noonle")
        self.window.limit_count_checkbox.setChecked(True)
        for count in ("0", "-1", "1.5", "n/a"):
            self.window.limit_count_input.setText(count)
            with self.assertRaises(ValueError):
                self.window.snapshot_options()
        with patch.object(self.window, "_validation_error") as validation:
            self.window.start_download_thread()
            validation.assert_called_once()
        self.assertIsNone(self.window._download_thread)
        self.window.limit_count_checkbox.setChecked(False)
        self.window.search_checkbox.setChecked(False)
        self.window.author_media_checkbox.setChecked(False)
        self.window.retry_failed_checkbox.setChecked(False)
        with self.assertRaises(ValueError):
            self.window.snapshot_options()
        self.window.tweet_urls_input.setPlainText("https://example.com/artist/status/123")
        with self.assertRaises(ValueError):
            self.window.snapshot_options()
        self.window.tweet_urls_input.clear()
        self.window.author_media_checkbox.setChecked(True)
        self.window.authors_input.setPlainText("https://x.com/artist/status/123")
        with self.assertRaises(ValueError):
            self.window.snapshot_options()

    def test_download_runs_off_ui_thread_and_stops_without_blocking(self):
        started = threading.Event()
        calls = []
        gui_thread = threading.get_ident()
        self.window.hashtag_input.setText("#First，#Second")

        class Engine:
            def __init__(self, options, log, stop_event, progress=None):
                self.options, self.log, self.stop_event = options, log, stop_event
                calls.append(options)

            def run(self):
                calls.append(threading.get_ident())
                started.set()
                self.log("<b>literal log text</b>")
                self.stop_event.wait(3)
                return result(stopped=self.stop_event.is_set())

        with patch.object(main, "DownloadEngine", Engine):
            self.window.start_download_thread()
            self.wait_until(started.is_set)
            self.assertEqual(calls[0].hashtags, ("first", "second"))
            self.assertNotEqual(calls[1], gui_thread)
            self.assertFalse(self.window.hashtag_input.isEnabled())
            self.assertFalse(self.window.download_btn.isEnabled())
            self.assertTrue(self.window.stop_btn.isEnabled())
            theme_before = self.window.theme_mode
            self.assertTrue(self.window.theme_btn.isEnabled())
            self.window.theme_btn.click()
            self.assertNotEqual(self.window.theme_mode, theme_before)
            self.assertTrue(self.window.is_downloading)
            self.assertFalse(self.window.hashtag_input.isEnabled())
            self.assertFalse(self.window.download_btn.isEnabled())
            self.assertTrue(self.window.stop_btn.isEnabled())
            self.window.hashtag_input.setText("#changed_programmatically")
            self.assertEqual(calls[0].hashtags, ("first", "second"))
            self.window.start_download_thread()
            self.assertEqual(len(calls), 2)
            before_stop = time.monotonic()
            self.window.stop_download()
            self.assertLess(time.monotonic() - before_stop, 0.3)
            self.assertTrue(self.window._stop_event.is_set())
            self.assertFalse(self.window.stop_btn.isEnabled())
            self.wait_until(lambda: self.window._download_thread is None)
        self.assertTrue(self.window.download_btn.isEnabled())
        self.assertIn("<b>literal log text</b>", self.window.status_box.toPlainText())
        self.assertIn("已停止", self.window.status_box.toPlainText())

    def test_close_waits_for_download_cancellation(self):
        started = threading.Event()

        class Engine:
            def __init__(self, options, log, stop_event, progress=None):
                self.stop_event = stop_event

            def run(self):
                started.set()
                self.stop_event.wait(3)
                time.sleep(0.08)  # represent flushing the database before shutdown
                return result(stopped=True)

        self.window.show()
        with patch.object(main, "DownloadEngine", Engine):
            self.window.start_download_thread()
            self.wait_until(started.is_set)
            self.assertFalse(self.window.close())
            self.assertTrue(self.window._close_pending)
            self.assertTrue(self.window._stop_event.is_set())
            self.assertTrue(self.window.isVisible())
            self.wait_until(lambda: self.window._download_thread is None and not self.window.isVisible())

    def test_login_browser_open_save_and_close_stay_on_one_worker(self):
        calls = []
        gui_thread = threading.get_ident()

        class Browser:
            def __init__(self, profile, on_progress=None):
                calls.append(("create", threading.get_ident()))

            def open(self):
                calls.append(("open", threading.get_ident()))

            def save_cookies(self, destination):
                calls.append(("save", threading.get_ident()))
                return 2

            def close(self):
                calls.append(("close", threading.get_ident()))

        with patch.object(main, "LoginBrowser", Browser):
            self.window.open_browser()
            self.assertTrue(self.window._login_busy)
            self.assertFalse(self.window.cookie_btn.isEnabled())
            self.wait_until(lambda: self.window._login_browser_open)
            self.window.save_cookies()
            self.wait_until(lambda: not self.window._login_busy)
            self.window.close()
            self.wait_until(lambda: self.window._login_thread is None)
        self.assertEqual([call[0] for call in calls], ["create", "open", "save", "close"])
        self.assertEqual(len({call[1] for call in calls}), 1)
        self.assertNotEqual(calls[0][1], gui_thread)

    def test_close_during_login_waits_for_open_and_closes_browser(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        class Browser:
            def __init__(self, profile, on_progress=None):
                pass

            def open(self):
                entered.set()
                release.wait(2)

            def close(self):
                closed.set()

        self.window.show()
        with patch.object(main, "LoginBrowser", Browser):
            self.window.open_browser()
            self.wait_until(entered.is_set)
            self.assertFalse(self.window.close())
            self.assertTrue(self.window.isVisible())
            release.set()
            self.wait_until(lambda: self.window._login_thread is None and not self.window.isVisible())
        self.assertTrue(closed.is_set())

    def test_driver_check_is_independent_and_failure_does_not_disable_download(self):
        entered = threading.Event()
        release = threading.Event()

        def check(base_dir, log):
            entered.set()
            release.wait(2)
            raise RuntimeError("offline test")

        with patch("driver_manager.ensure_chromedriver", check):
            self.window.check_driver_update()
            self.wait_until(entered.is_set)
            self.assertTrue(self.window.download_btn.isEnabled())
            self.assertTrue(self.window.cookie_btn.isEnabled())
            self.assertFalse(self.window.driver_check_btn.isEnabled())
            self.window.close()
            self.assertTrue(self.window._close_pending)
            release.set()
            self.wait_until(lambda: self.window._driver_thread is None)
        self.assertIn("新版下載不需要此驅動", self.window.status_box.toPlainText())

    def test_settings_roundtrip_does_not_save_cookie_secrets(self):
        self.window.hashtag_input.setText("#測試")
        self.window.authors_input.setPlainText("@artist")
        self.window.auto_driver_checkbox.setChecked(False)
        self.window._save_settings()
        saved = json.loads(self.window.settings_file.read_text(encoding="utf-8"))
        self.assertFalse(saved["auto_driver_update"])
        self.assertNotIn("cookies", saved)
        self.assertFalse(self.window.settings_file.with_suffix(".json.tmp").exists())
        other = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
        try:
            self.assertEqual(other.hashtag_input.text(), "#測試")
            self.assertEqual(other.authors_input.toPlainText(), "@artist")
            self.assertFalse(other.auto_driver_checkbox.isChecked())
        finally:
            other.close()
            other.deleteLater()

    def test_theme_switch_preserves_date_calendar_page_and_collection_options(self):
        self.window.hashtag_input.setText("#one #two")
        self.window.authors_input.setPlainText("@artist")
        self.window.limit_count_checkbox.setChecked(True)
        self.window.limit_count_input.setText("7")
        self.window.limit_date_checkbox.setChecked(True)
        self.window.limit_date_input.setDate(QDate(2024, 2, 29))
        calendar = self.window.limit_date_input.calendarWidget()
        calendar.setCurrentPage(2027, 9)
        before = self.window.snapshot_options()
        light_window = self.window.palette().color(QPalette.ColorRole.Window).lightness()
        light_editor = self.window.limit_date_input.palette().color(QPalette.ColorRole.Base).lightness()

        self.window.theme_btn.click()
        self.assertEqual(self.window.theme_mode, "dark")
        self.assertEqual(self.window.snapshot_options(), before)
        self.assertTrue(self.window.limit_date_input.isEnabled())
        self.assertEqual(self.window.limit_date_input.date(), QDate(2024, 2, 29))
        self.assertEqual(calendar.selectedDate(), QDate(2024, 2, 29))
        self.assertEqual((calendar.yearShown(), calendar.monthShown()), (2027, 9))
        dark_window = self.window.palette().color(QPalette.ColorRole.Window).lightness()
        self.assertLess(dark_window, light_window)
        self.assertLess(self.window.limit_date_input.palette().color(QPalette.ColorRole.Base).lightness(), light_editor)
        for widget in (self.window.hashtag_input, self.window.limit_date_input, calendar):
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
                self.assertGreater(widget.palette().color(group, QPalette.ColorRole.Text).lightness(),
                                   widget.palette().color(group, QPalette.ColorRole.Base).lightness())

        self.window.theme_btn.click()
        self.assertEqual(self.window.theme_mode, "light")
        self.assertEqual(self.window.snapshot_options(), before)
        self.assertGreater(self.window.palette().color(QPalette.ColorRole.Window).lightness(), dark_window)
        self.assertEqual((calendar.yearShown(), calendar.monthShown()), (2027, 9))

    def test_theme_preference_roundtrip_preserves_collection_settings(self):
        self.window.hashtag_input.setText("#First #第二")
        self.window.limit_date_checkbox.setChecked(True)
        self.window.limit_date_input.setDate(QDate(2026, 10, 17))
        self.window.theme_btn.click()
        before = self.window.snapshot_options()
        self.window._save_settings()
        saved = json.loads(self.window.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["theme_mode"], "dark")
        other = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
        try:
            self.assertEqual(other.theme_mode, "dark")
            self.assertEqual(other.snapshot_options(), before)
            self.assertEqual(other.limit_date_input.calendarWidget().selectedDate(), QDate(2026, 10, 17))
        finally:
            other.close()
            other.deleteLater()

    def test_legacy_or_unknown_theme_preference_uses_light_without_resetting_targets(self):
        self.window.data_dir.mkdir(exist_ok=True)
        preferences = ({}, {"theme_mode": "system"}, {"theme_mode": None}, {"theme_mode": 42}, {"theme_mode": []})
        for preference in preferences:
            with self.subTest(preference=preference):
                settings = {"hashtag": "#one #two", **preference}
                self.window.settings_file.write_text(json.dumps(settings), encoding="utf-8")
                other = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
                try:
                    self.assertEqual(other.theme_mode, "light")
                    self.assertEqual(other.snapshot_options().hashtags, ("one", "two"))
                finally:
                    other.close()
                    other.deleteLater()

    def test_multiple_tags_share_one_snapshot_and_survive_reopening(self):
        self.window.hashtag_input.setText("#Noonle，#另一個Tag; #NOONLE #FanArt_圖")
        options = self.window.snapshot_options()
        self.assertEqual(options.hashtags, ("noonle", "另一個tag", "fanart_圖"))
        self.assertEqual(options.hashtag, "#Noonle")
        self.window._save_settings()
        saved = json.loads(self.window.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["hashtags"], list(options.hashtags))
        other = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
        try:
            self.assertEqual(other.snapshot_options().hashtags, options.hashtags)
            self.assertIn("#另一個tag", other.tag_summary_label.text())
        finally:
            other.close()
            other.deleteLater()

    def test_settings_migrate_legacy_or_list_only_targets_and_preserve_unfinished_edits(self):
        self.window.data_dir.mkdir(exist_ok=True)
        cases = (
            ({"hashtag": "#LegacyTag"}, "#LegacyTag", ("legacytag",)),
            ({"hashtags": ["#One", "TWO", "#one"]}, "#one #two", ("one", "two")),
            ({"hashtag": None, "hashtags": ["#One", "TWO"]}, "#one #two", ("one", "two")),
            ({"hashtags": ["#e\u0301", "＃É", "#第二"]}, "#é #第二", ("é", "第二")),
            ({"hashtag": "#unfinished ##tag", "hashtags": []}, "#unfinished ##tag", None),
        )
        for settings, visible, expected in cases:
            with self.subTest(settings=settings):
                self.window.settings_file.write_text(json.dumps(settings), encoding="utf-8")
                other = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
                try:
                    self.assertEqual(other.hashtag_input.text(), visible)
                    if expected is None:
                        with self.assertRaises(ValueError):
                            other.snapshot_options()
                    else:
                        self.assertEqual(other.snapshot_options().hashtags, expected)
                finally:
                    other.close()
                    other.deleteLater()

    def test_invalid_utf8_settings_use_defaults_and_allow_a_new_valid_run(self):
        self.window.data_dir.mkdir(exist_ok=True)
        self.window.settings_file.write_bytes(b'\xff\xfe{"hashtag":"broken"}')
        other = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
        try:
            self.assertEqual(other.hashtag_input.text(), "")
            with self.assertRaises(ValueError):
                other.snapshot_options()
            self.assertIn("無法讀取設定", other.status_box.toPlainText())
            other.hashtag_input.setText("#next #後續")
            self.assertEqual(other.snapshot_options().hashtags, ("next", "後續"))
        finally:
            other.close()
            other.deleteLater()

    def test_snapshot_normalizes_unicode_and_rejects_an_invalid_later_target_before_worker(self):
        self.window.hashtag_input.setText("#e\u0301，＃É #第二")
        options = self.window.snapshot_options()
        self.assertEqual(options.hashtags, ("é", "第二"))
        self.assertEqual(options.hashtag, "#e\u0301")  # Legacy field preserves the first raw token.
        self.window.hashtag_input.setText("#valid ##invalid")
        with patch.object(self.window, "_validation_error") as validation:
            self.window.start_download_thread()
        validation.assert_called_once()
        self.assertIn("##invalid", validation.call_args.args[0])
        self.assertIsNone(self.window._download_thread)
        self.assertFalse(self.window.is_downloading)

    def test_automatic_authors_need_no_input_and_manual_authors_are_independent(self):
        self.window.search_checkbox.setChecked(False)
        self.window.retry_failed_checkbox.setChecked(False)
        automatic = self.window.snapshot_options()
        self.assertEqual(automatic.authors, ())
        self.assertTrue(automatic.discover_authors)
        self.window.discover_authors_checkbox.setChecked(False)
        self.window.authors_input.setPlainText("@artist\nhttps://x.com/other/media")
        manual = self.window.snapshot_options()
        self.assertEqual(manual.authors, ("artist", "other"))
        self.assertFalse(manual.discover_authors)
        self.assertTrue(self.window.authors_input.isEnabled())
        self.window.author_media_checkbox.setChecked(False)
        self.assertEqual(self.window.authors_input.toPlainText(), "@artist\nhttps://x.com/other/media")
        self.window.author_media_checkbox.setChecked(True)
        self.assertEqual(self.window.snapshot_options().authors, manual.authors)

    def test_advanced_disclosure_preserves_options_and_saved_database(self):
        self.window.hashtag_input.setText("#one #two")
        self.window.data_dir.mkdir()
        database = self.window.data_dir / "downloads.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE existing_history (media TEXT)")
            connection.execute("INSERT INTO existing_history VALUES ('previous-download')")
        before = self.window.snapshot_options()
        self.window.advanced_toggle.setChecked(True)
        self.assertEqual(self.window.snapshot_options(), before)
        self.window.advanced_toggle.setChecked(False)
        self.assertEqual(self.window.snapshot_options(), before)
        self.window._save_settings()
        other = main.TwitterDownloader(show_notice=False, base_dir=Path(self.directory.name))
        try:
            self.assertEqual(other.snapshot_options().database_path, database)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT media FROM existing_history").fetchone()[0], "previous-download")
        finally:
            other.close()
            other.deleteLater()

    def test_sidebar_reports_actual_result_and_does_not_infer_login_validity(self):
        self.assertEqual(self.window.run_state_label.text(), "尚未開始")
        self.window.data_dir.mkdir()
        self.window.cookie_file.write_text("# dummy cookie file, not a valid session\n", encoding="utf-8")
        self.window._refresh_summary()
        self.assertEqual(self.window.login_state_label.text(), "已儲存登入資料")
        self.window._download_completed(result(downloaded=3, posts=2))
        self.assertEqual(self.window.run_state_label.text(), "本輪處理完成")
        self.assertIn("下載 3", self.window.result_detail_label.text())
        self.assertIn("處理 2", self.window.status_box.toPlainText())
        self.window._download_failed("offline")
        self.assertEqual(self.window.run_state_label.text(), "下載未完成")

    def test_author_and_url_editors_keep_newlines_and_tab_through_the_workflow(self):
        self.window.show()
        self.window.authors_input.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.authors_input, Qt.Key.Key_Return)
        self.assertEqual(self.window.authors_input.toPlainText(), "\n")
        QTest.keyClick(self.window.authors_input, Qt.Key.Key_Tab)
        self.assertTrue(self.window.tweet_urls_input.hasFocus())
        QTest.keyClick(self.window.tweet_urls_input, Qt.Key.Key_Return)
        self.assertEqual(self.window.tweet_urls_input.toPlainText(), "\n")
        QTest.keyClick(self.window.tweet_urls_input, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(self.window.authors_input.hasFocus())

    def test_source_errors_do_not_report_a_successful_run(self):
        self.window._download_completed(result(errors=["X authentication required"]))
        self.assertIn("❌ 未完成", self.window.status_box.toPlainText())
        self.assertNotIn("✅", self.window.status_box.toPlainText())
        self.window.status_box.clear()
        self.window._download_completed(result(downloaded=2, failed=1))
        self.assertIn("⚠️ 部分完成", self.window.status_box.toPlainText())
        self.assertNotIn("✅", self.window.status_box.toPlainText())

    def test_packaged_dist_uses_project_data_and_standalone_uses_own_directory(self):
        self.assertEqual(main.application_dir(), Path(main.__file__).resolve().parents[1])
        project = Path(self.directory.name)
        distribution = project / "dist"
        distribution.mkdir()
        executable = distribution / "XFanArtDownloader.exe"
        with patch.object(main.sys, "frozen", True, create=True), patch.object(main.sys, "executable", str(executable)):
            self.assertEqual(main.application_dir(), distribution)
            (project / "desktop").mkdir()
            (project / "desktop" / "main.py").write_text("# source", encoding="utf-8")
            self.assertEqual(main.application_dir(), project)
        standalone = project / "standalone"
        with patch.object(main.sys, "frozen", True, create=True), patch.object(main.sys, "executable", str(standalone / "XFanArtDownloader.exe")):
            self.assertEqual(main.application_dir(), standalone)


if __name__ == "__main__":
    unittest.main()
