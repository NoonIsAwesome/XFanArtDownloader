"""Main-window page ownership, safe activity refresh and global actions, entirely offline."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_history_session_log as log_fixture
from PyQt6.QtCore import Qt
from PyQt6.QtCore import QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QTabWidget
import history_dialog
import main
import storage
from tests.test_history_preview import HeldPool, png_bytes


class MainTabsTests(unittest.TestCase):
    setUpClass = log_fixture.HistorySessionLogTests.setUpClass.__func__
    setUpClass = classmethod(setUpClass)
    setUp = log_fixture.HistorySessionLogTests.setUp
    tearDown = log_fixture.HistorySessionLogTests.tearDown
    wait_until = log_fixture.HistorySessionLogTests.wait_until

    def test_exactly_two_main_pages_embed_activity_without_redundant_history_button_or_window(self):
        window = self.window
        window.show()
        self.assertEqual(window.main_tabs.count(), 2)
        self.assertEqual(window.findChildren(QTabWidget), [window.main_tabs])
        self.assertIsInstance(window.history_panel, history_dialog.HistoryPanel)
        self.assertNotIsInstance(window.history_panel, QDialog)
        self.assertFalse(window.history_panel.isWindow())
        self.assertIsNone(window.history_panel.tabs)
        self.assertFalse(hasattr(window, "history_button"))
        for widget in (window.hashtag_input, window.authors_input, window.choose_path_btn, window.advanced_panel):
            self.assertTrue(window.settings_page.isAncestorOf(widget))
        for widget in (window.status_box, window.current_media_row, window.history_panel.preview_image_label):
            self.assertTrue(window.activity_page.isAncestorOf(widget))
            self.assertFalse(window.settings_page.isAncestorOf(widget))
        for widget in (window.language_combo, window.theme_btn, window.tools_btn,
                       window.download_btn, window.stop_btn, window.result_detail_label):
            self.assertFalse(window.main_tabs.isAncestorOf(widget))
        for widget in (window.cookie_btn, window.cookie_save_btn, window.cookie_import_btn, window.login_state_label,
                       window.login_help_label):
            self.assertTrue(window.settings_page.isAncestorOf(widget))
            self.assertTrue(window.login_section.isAncestorOf(widget))
        self.assertFalse(hasattr(window, "help_btn"))
        self.assertFalse(hasattr(window, "about_btn"))
        self.assertIn(window.help_action, window.tools_menu.actions())
        self.assertIn(window.about_action, window.tools_menu.actions())
        before = set(QApplication.topLevelWidgets())
        window.show_download_history()
        self.wait_until(lambda: window.history_panel._active is None)
        self.assertEqual(window.main_tabs.currentIndex(), 1)
        self.assertEqual(set(QApplication.topLevelWidgets()), before)
        for page in (0, 1):
            window.main_tabs.setCurrentIndex(page)
            self.app.processEvents()
            self.assertTrue(window.download_btn.isVisible())
            self.assertTrue(window.stop_btn.isVisible())
            self.assertEqual(window.login_section.isVisible(), page == 0)

    def test_more_menu_help_and_about_remain_manual_and_invoke_their_original_features(self):
        window = self.window
        self.assertIsNone(window._usage_guide)
        window.help_action.trigger()
        self.assertIsNotNone(window._usage_guide)
        window._usage_guide.close()
        with patch.object(main.AuthorInfoDialog, "exec", return_value=QDialog.DialogCode.Accepted) as about:
            window.about_action.trigger()
        about.assert_called_once()

    def test_chinese_login_actions_share_a_row_when_the_standard_sidebar_has_room(self):
        window = self.window
        window.resize(1280, 880)
        window.show()
        self.app.processEvents()
        buttons = (window.cookie_btn, window.cookie_save_btn, window.cookie_import_btn)
        needed = sum(button.sizeHint().width() for button in buttons) + 2*window.login_button_flow.spacing()
        self.assertLessEqual(needed, window.login_buttons.width())
        self.assertEqual(len({button.y() for button in buttons}), 1)
        self.assertEqual(len({button.height() for button in buttons}), 1)
        self.assertLess(buttons[-1].geometry().bottom(), window.login_state_label.y())
        for button in buttons:
            self.assertGreaterEqual(button.width(), button.sizeHint().width())
            self.assertLessEqual(button.geometry().right(), window.login_buttons.width())

    def test_login_actions_reflow_with_longer_language_and_narrow_width_without_clipping(self):
        window = self.window
        window.show()
        window.resize(1020, 740)
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        self.app.processEvents()
        buttons = (window.cookie_btn, window.cookie_save_btn, window.cookie_import_btn)
        self.assertGreater(len({button.y() for button in buttons}), 1)
        for first, second in zip(buttons, buttons[1:]):
            self.assertFalse(first.geometry().intersects(second.geometry()))
            self.assertLessEqual(first.y(), second.y())
        for button in buttons:
            self.assertGreaterEqual(button.width(), button.sizeHint().width())
            self.assertLessEqual(button.geometry().right(), window.login_buttons.width())
            self.assertLessEqual(button.geometry().bottom(), window.login_buttons.height())
        window.resize(1280, 880)
        window.language_combo.setCurrentIndex(window.language_combo.findData("zh_TW"))
        self.app.processEvents()
        self.assertEqual(len({button.y() for button in buttons}), 1)

    def test_invalid_start_keeps_settings_with_visible_feedback_and_starts_no_job(self):
        self.window.show()
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok) as warning:
            self.window.start_download_thread()
        warning.assert_called_once()
        self.assertEqual(self.window.main_tabs.currentIndex(), 0)
        self.assertIsNone(self.window._download_thread)
        self.assertIsNotNone(self.window._result_detail)
        self.assertTrue(self.window.result_detail_label.isVisible())
        self.assertIn("⚠", self.window.result_detail_label.text())

    def test_update_failure_remains_visible_on_settings_without_opening_another_page(self):
        self.window.show()
        self.window._update_check_failed("版本檢查未完成，請稍後重試。")
        self.assertEqual(self.window.main_tabs.currentIndex(), 0)
        self.assertTrue(self.window.result_detail_label.isVisible())
        self.assertIn("版本檢查未完成", self.window.result_detail_label.text())
        self.assertIn("⚠", self.window.result_detail_label.text())
        self.assertIn("版本檢查未完成", self.window.status_box.toPlainText())

    def test_embedded_activity_backgrounds_follow_the_actual_dark_theme_button(self):
        self.window.show()
        self.window.main_tabs.setCurrentIndex(1)
        self.window.theme_btn.click()
        self.app.processEvents()
        panel = self.window.history_panel
        self.assertEqual(panel.theme_mode, "dark")
        rendered = panel.grab().toImage()
        expected = history_dialog.theme_colors("dark")["window"].lower()
        for name in ("session_log_hint", "progress_toggle", "message_label", "preview_status_label"):
            with self.subTest(surface=name):
                widget = getattr(panel, name)
                point = widget.mapTo(panel, QPoint(max(1, widget.width()-3), max(1, widget.height()//2)))
                self.assertEqual(rendered.pixelColor(point).name(), expected)
        self.assertEqual(rendered.pixelColor(1, 1).name(), expected)

    def test_valid_start_opens_activity_and_later_progress_never_steals_settings_or_stops_the_job(self):
        started = threading.Event()

        class Engine:
            def __init__(engine, options, log, stop_event, progress=None):
                engine.stopped = stop_event

            def run(engine):
                started.set()
                engine.stopped.wait(4)
                return SimpleNamespace(downloaded=0, skipped=0, failed=0, filtered=0, posts=0,
                                       errors=[], stopped=engine.stopped.is_set())

        window = self.window
        window.show()
        window.hashtag_input.setText("#synthetic")
        with patch.object(main, "DownloadEngine", Engine):
            window.start_download_thread()
            self.wait_until(started.is_set)
            try:
                self.assertEqual(window.main_tabs.currentIndex(), 1)
                window.main_tabs.setCurrentIndex(0)
                window._media_progress(window._download_worker, {"type":"media", "state":"skipped",
                    "author_handle":"artist", "author_name":"Synthetic", "filename":"fixture.png", "display_path":"fixture.png"})
                self.app.processEvents()
                self.assertEqual(window.main_tabs.currentIndex(), 0)
                self.assertTrue(window.is_downloading)
                self.assertFalse(window._stop_event.is_set())
                self.assertFalse(window.history_panel._live_refresh.isActive())
            finally:
                window.stop_download()
                self.wait_until(lambda: window._download_thread is None)

    def test_debounced_record_refresh_preserves_selected_image_and_hiding_cancels_only_ui_work(self):
        window = self.window
        window.show()
        output = self.root / "downloads"
        output.mkdir()
        image = output / "123.png"
        image.write_bytes(png_bytes())
        with storage.Store(self.root / "data/downloads.sqlite3") as store:
            store.record_post("123", "artist", "synthetic", [], "fixture", True)
            store.record_media("123", "twitter:123:1", "https://example.invalid/never-read",
                               {"extension":"png"}, str(output))
            store.mark_success("twitter:123:1", str(image), image.stat().st_size, "fixture")
            job_id = store.create_job("synthetic", {}, [{"key":"synthetic", "kind":"search", "url":"https://example.invalid",
                "name":"synthetic", "data":{}}])["job_id"]
            store.set_source_state(job_id, "synthetic", "partial")
            store.set_job_state(job_id, "partial")
        window.show_download_history()
        panel = window.history_panel
        self.wait_until(lambda: panel._active is None and not panel._loading)
        panel.history_table.selectRow(0)
        self.wait_until(lambda: panel._preview_kind == "image")
        previous = panel._preview_image
        panel.progress_toggle.click()
        self.wait_until(lambda: not panel._loading and panel._active is None and bool(panel._sources))
        self.assertEqual(panel._sources[0]["state"], "partial")
        with storage.Store(self.root / "data/downloads.sqlite3") as store:
            store.set_source_state(job_id, "synthetic", "completed")
            store.set_job_state(job_id, "completed")
        panel.refresh(preserve_preview=True)
        self.wait_until(lambda: not panel._loading and panel._active is None and panel._sources[0]["state"] == "completed")
        self.assertEqual(panel._jobs[0]["state"], "completed")
        self.assertIs(panel._preview_image, previous)
        panel.progress_toggle.click()
        held = HeldPool()
        panel._pool = held
        for _ in range(20):
            panel.schedule_refresh()
        self.assertTrue(panel._live_refresh.isActive())
        self.assertEqual(held.tasks, [])
        panel._live_refresh.stop()
        panel.refresh(preserve_preview=True)
        self.assertIs(panel._preview_image, previous)
        self.assertEqual(panel._selected_media()[0]["media_key"], "twitter:123:1")
        held.tasks[0].run()
        self.assertIs(panel._preview_image, previous)
        self.assertEqual(panel._selected_media()[0]["media_key"], "twitter:123:1")
        panel.schedule_refresh()
        window.main_tabs.setCurrentIndex(0)
        self.assertFalse(panel._live_refresh.isActive())
        self.assertIsNone(panel._preview_key)
        self.assertFalse(window._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
