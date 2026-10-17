"""Offline language selection, persistence and live-worker regression tests."""
import json
import os
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

from PyQt6.QtCore import QDate, QEvent, QLocale, QPoint, QRect, Qt
from PyQt6.QtGui import QFontDatabase, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QFileDialog

import i18n
import main


class MainLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        if os.name == "nt" and os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            for filename in ("msjh.ttc", "msjhbd.ttc", "segoeui.ttf", "segoeuib.ttf", "malgun.ttf", "YuGothM.ttc", "msyh.ttc"):
                path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))

    def setUp(self):
        self.previous_language = i18n.language_mode()
        self.previous_locale = QLocale()
        self.previous_palette = self.app.palette()
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.stop_download()
            window.close()
            self.wait_until(lambda: window._download_thread is None and window._login_thread is None)
            window.deleteLater()
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.directory.cleanup()
        i18n.set_language(self.previous_language)
        QLocale.setDefault(self.previous_locale)
        i18n.install_qt_translator(self.app)
        self.app.setPalette(self.previous_palette)

    def wait_until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), "Background operation did not complete")

    def make_window(self, language="zh_TW", *, show_notice=False, settings=None):
        if language is not None:
            directory = self.root / "data"
            directory.mkdir(exist_ok=True)
            data = {"language": language, "auto_driver_update": False, **(settings or {})}
            (directory / "settings.json").write_text(json.dumps(data), encoding="utf-8")
        window = main.TwitterDownloader(show_notice=show_notice, base_dir=self.root)
        self.windows.append(window)
        return window

    def choose(self, window, language):
        window.language_combo.setCurrentIndex(window.language_combo.findData(language))
        self.app.processEvents()

    def test_fresh_system_default_native_choices_and_saved_mode(self):
        window = self.make_window(None)
        self.assertEqual(window.language_mode, "system")
        expected = ["system", "zh_TW", "zh_CN", "en", "ja", "ko"]
        self.assertEqual([window.language_combo.itemData(index) for index in range(6)], expected)
        native_names = ["繁體中文", "简体中文", "English", "日本語", "한국어"]
        for language in expected:
            self.choose(window, language)
            self.assertEqual([window.language_combo.itemText(index) for index in range(1, 6)], native_names)
            self.assertEqual(window.language_combo.currentData(), language)
        saved = json.loads(window.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["language"], "ko")

    def test_saved_language_is_applied_before_first_run_guide(self):
        window = self.make_window("en", show_notice=True, settings={"hashtag": "#chosen_art"})
        self.wait_until(lambda: window._usage_guide is not None)
        self.assertEqual(window.language_mode, "en")
        self.assertEqual(window.download_btn.text(), i18n.tr("開始收集"))
        self.assertNotEqual(window.download_btn.text(), "開始收集")
        self.assertEqual(window._usage_guide.title_label.text(), i18n.tr("四步開始收集"))
        self.assertEqual(window.hashtag_input.text(), "#chosen_art")
        self.assertFalse(window._usage_guide.isModal())
        self.assertIsNone(window._driver_thread)

    def test_switch_preserves_inputs_date_theme_and_open_guide(self):
        window = self.make_window()
        window.hashtag_input.setText("#自訂 #Second")
        window.authors_input.setPlainText("@artist\nhttps://x.com/another/media")
        window.tweet_urls_input.setPlainText("https://x.com/artist/status/123")
        window.limit_date_checkbox.setChecked(True)
        window.limit_date_input.setDate(QDate(2020, 8, 17))
        window.limit_date_input.calendarWidget().setCurrentPage(2019, 7)
        window.limit_count_checkbox.setChecked(True)
        window.limit_count_input.setText("42")
        window.toggle_theme()
        window.show_usage_guide()
        guide = window._usage_guide
        original = window.snapshot_options()
        for language in ("en", "ja", "ko", "zh_CN", "zh_TW"):
            self.choose(window, language)
            self.assertEqual(window.snapshot_options(), original)
            self.assertEqual(window.theme_mode, "dark")
            self.assertIs(window._usage_guide, guide)
            self.assertTrue(guide.isVisible())
            self.assertEqual(guide.title_label.text(), i18n.tr("四步開始收集"))
            self.assertEqual(window.limit_date_input.date(), QDate(2020, 8, 17))
            self.assertEqual(window.limit_date_input.calendarWidget().yearShown(), 2019)
            self.assertEqual(window.limit_date_input.calendarWidget().monthShown(), 7)
            self.assertEqual(window.limit_date_input.calendarWidget().locale().name(), i18n.locale().name())
            self.assertEqual(window.authors_input.placeholderText(), i18n.tr("@作者帳號\n或 https://x.com/作者/media"))
            self.assertEqual(window.path_label.text(), str(original.output_dir))

    def test_language_can_change_during_download_without_recreating_worker(self):
        started = threading.Event()

        class Engine:
            def __init__(engine, options, log, stop_event):
                engine.stop_event = stop_event

            def run(engine):
                started.set()
                engine.stop_event.wait(3)
                return SimpleNamespace(downloaded=0, skipped=0, failed=0, filtered=0, posts=0,
                                       errors=[], stopped=engine.stop_event.is_set())

        window = self.make_window()
        window.hashtag_input.setText("#my_art")
        with patch.object(main, "DownloadEngine", Engine):
            window.start_download_thread()
            self.wait_until(started.is_set)
            worker, thread = window._download_worker, window._download_thread
            try:
                for language in ("en", "ja", "ko", "zh_CN", "zh_TW"):
                    self.choose(window, language)
                    self.assertIs(window._download_worker, worker)
                    self.assertIs(window._download_thread, thread)
                    self.assertTrue(window.is_downloading)
                    self.assertFalse(window.hashtag_input.isEnabled())
                    self.assertFalse(window.download_btn.isEnabled())
                    self.assertTrue(window.stop_btn.isEnabled())
                    self.assertTrue(window.language_combo.isEnabled())
                    self.assertFalse(window.warp_proxy_checkbox.isEnabled())
                    self.assertFalse(window.warp_proxy_port_input.isEnabled())
                    self.assertFalse(window.reply_media_checkbox.isEnabled())
            finally:
                window.stop_download()
                self.wait_until(lambda: window._download_thread is None)

    def test_existing_counts_logs_and_plain_text_retranslate_without_losing_values(self):
        window = self.make_window()
        result = SimpleNamespace(downloaded=2, skipped=3, failed=1, posts=4, filtered=5,
                                 errors=[], stopped=False)
        window._download_completed(result)
        path = "D:/開始收集/artist/example.png"
        window._log("📁 已選擇儲存路徑：{folder}", folder=path)
        literal = "<b>user-authored literal text</b>"
        window.append_status(literal)
        original_log = window.status_box.toPlainText()
        self.choose(window, "en")
        self.assertIn(path, window.status_box.toPlainText())
        self.assertIn(literal, window.status_box.toPlainText())
        self.assertNotIn("下載 2 個媒體", window.result_detail_label.text())
        self.assertIn("2", window.result_detail_label.text())
        self.assertEqual(window.run_state_label.text(), i18n.translate_text("部分完成／有失敗項目"))
        self.choose(window, "zh_TW")
        self.assertEqual(window.status_box.toPlainText(), original_log)

    def test_selected_language_applies_to_nonnative_file_dialogs(self):
        window = self.make_window("en")
        with patch.object(QFileDialog, "getExistingDirectory", return_value="") as folder:
            window.select_download_path()
        self.assertEqual(folder.call_args.args[1], i18n.tr("選擇下載資料夾"))
        self.assertTrue(folder.call_args.kwargs["options"] & QFileDialog.Option.DontUseNativeDialog)
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")) as cookies:
            window.import_cookie_file()
        self.assertEqual(cookies.call_args.args[1], i18n.tr("匯入 X Cookie（Netscape TXT 或舊版 JSON）"))
        self.assertTrue(cookies.call_args.kwargs["options"] & QFileDialog.Option.DontUseNativeDialog)

    def test_invalid_saved_language_uses_system_without_discarding_targets(self):
        window = self.make_window("unsupported", settings={"hashtag": "#saved_art"})
        self.assertEqual(window.language_mode, "system")
        self.assertEqual(window.hashtag_input.text(), "#saved_art")

    def test_warp_proxy_is_opt_in_and_roundtrips_without_touching_system_settings(self):
        window = self.make_window()
        window.hashtag_input.setText("#chosen_art")
        self.assertFalse(window.warp_proxy_checkbox.isChecked())
        self.assertEqual(window.warp_proxy_port_input.value(), 40000)
        self.assertFalse(window.warp_proxy_port_input.isEnabled())
        self.assertFalse(window.snapshot_options().warp_proxy)
        self.assertEqual(window.snapshot_options().warp_proxy_port, 40000)
        window.warp_proxy_checkbox.setChecked(True)
        self.assertTrue(window.warp_proxy_port_input.isEnabled())
        window.warp_proxy_port_input.setValue(40123)
        options = window.snapshot_options()
        self.assertTrue(options.warp_proxy)
        self.assertEqual(options.warp_proxy_port, 40123)
        window._save_settings()
        saved = json.loads(window.settings_file.read_text(encoding="utf-8"))
        self.assertIs(saved["warp_proxy"], True)
        self.assertEqual(saved["warp_proxy_port"], 40123)
        reopened = main.TwitterDownloader(show_notice=False, base_dir=self.root)
        self.windows.append(reopened)
        self.assertTrue(reopened.warp_proxy_checkbox.isChecked())
        self.assertEqual(reopened.warp_proxy_port_input.value(), 40123)
        self.assertEqual(reopened.snapshot_options().hashtags, ("chosen_art",))
        self.choose(reopened, "en")
        self.assertTrue(reopened.warp_proxy_checkbox.isChecked())
        self.assertEqual(reopened.warp_proxy_port_input.value(), 40123)

    def test_invalid_saved_proxy_port_keeps_safe_default(self):
        for value in (0, -1, 65536, True, "40000", None):
            with self.subTest(value=value):
                window = self.make_window(settings={"warp_proxy": True, "warp_proxy_port": value})
                self.assertTrue(window.warp_proxy_checkbox.isChecked())
                self.assertEqual(window.warp_proxy_port_input.value(), 40000)
                window.close()

    def test_reply_option_defaults_on_and_persists_without_relaxing_other_source_controls(self):
        window = self.make_window()
        window.hashtag_input.setText("#chosen_art")
        self.assertTrue(window.reply_media_checkbox.isChecked())
        self.assertTrue(window.snapshot_options().include_author_replies)
        window.reply_media_checkbox.setChecked(False)
        window._save_settings()
        self.assertIs(json.loads(window.settings_file.read_text(encoding="utf-8"))["include_author_replies"], False)
        reopened = main.TwitterDownloader(show_notice=False,base_dir=self.root)
        self.windows.append(reopened)
        self.assertFalse(reopened.reply_media_checkbox.isChecked())
        self.assertFalse(reopened.snapshot_options().include_author_replies)
        self.assertTrue(reopened.author_media_checkbox.isChecked())

    def test_standard_dialog_palette_matches_app_theme_instead_of_os_theme(self):
        window = self.make_window()
        for theme in ("light", "dark"):
            if window.theme_mode != theme:
                window.toggle_theme()
            dialog = QFileDialog(window)
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
            try:
                dialog.show()
                self.app.processEvents()
                self.assertEqual(dialog.palette().color(QPalette.ColorRole.Window),
                                 window.palette().color(QPalette.ColorRole.Window))
                self.assertNotEqual(dialog.palette().color(QPalette.ColorRole.Window),
                                    dialog.palette().color(QPalette.ColorRole.WindowText))
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_header_controls_fit_minimum_window_in_each_language(self):
        window = self.make_window()
        window.show()
        for language in ("zh_TW", "zh_CN", "en", "ja", "ko"):
            with self.subTest(language=language):
                self.choose(window, language)
                window.resize(1020, 740)
                self.app.processEvents()
                self.assertEqual(window.size().width(), 1020)
                for widget in (window.language_combo, window.theme_btn, window.help_btn, window.about_btn,
                               window.cookie_btn, window.cookie_save_btn, window.cookie_import_btn,
                               window.download_btn, window.stop_btn):
                    position = widget.mapTo(window, QPoint())
                    self.assertGreaterEqual(position.x(), 0)
                    self.assertGreaterEqual(position.y(), 0)
                    self.assertLessEqual(position.x() + widget.width(), window.width())
                    self.assertLessEqual(position.y() + widget.height(), window.height())

    def test_advanced_translations_wrap_inside_viewport_and_keep_checkbox_keys(self):
        window = self.make_window()
        window.show()
        window.advanced_toggle.setChecked(True)
        for language in ("zh_TW", "zh_CN", "en", "ja", "ko"):
            with self.subTest(language=language):
                self.choose(window, language)
                window.resize(1020, 740)
                self.app.processEvents()
                viewport = window.advanced_panel.viewport()
                self.assertLessEqual(window.advanced_panel.widget().width(), viewport.width())
                checkbox = window.human_behavior_checkbox
                self.assertEqual(checkbox.text(), i18n.tr("下載間隨機休息（假裝自己不是機器人）"))
                self.assertGreaterEqual(checkbox.height(), checkbox.heightForWidth(checkbox.width()))
                before = checkbox.isChecked()
                checkbox.setFocus()
                QTest.keyClick(checkbox, Qt.Key.Key_Space)
                self.assertIs(checkbox.isChecked(), not before)

    def test_login_help_reserves_wrapped_height_when_language_and_window_width_change(self):
        window = self.make_window("ja")
        window.show()
        for language in ("ja", "en", "ko", "zh_TW", "ja"):
            self.choose(window, language)
            for width in (1160, 1020):
                with self.subTest(language=language, width=width):
                    window.resize(width, 740)
                    self.app.processEvents()
                    label = window.login_help_label
                    self.assertLessEqual(label.width(), 530)
                    self.assertTrue(label.sizePolicy().hasHeightForWidth())
                    expected = label.heightForWidth(label.width())
                    self.assertGreaterEqual(label.height(), expected)
                    text_height = label.fontMetrics().boundingRect(
                        QRect(0, 0, label.contentsRect().width(), 10000),
                        int(Qt.TextFlag.TextWordWrap), label.text(),
                    ).height()
                    self.assertGreaterEqual(label.contentsRect().height(), text_height)
                    self.assertEqual(window.width(), width)
                    for button in (window.download_btn, window.stop_btn):
                        position = button.mapTo(window, QPoint())
                        self.assertLessEqual(position.y() + button.height(), window.height())


if __name__ == "__main__":
    unittest.main()
