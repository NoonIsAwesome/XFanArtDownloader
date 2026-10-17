"""Offline regression coverage for the desktop two-phase login workflow."""

import contextlib
import io
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import main
from locale_fixture import traditional_chinese_system
import save_cookies as console_login


class ObservedWindow(main.TwitterDownloader):
    def __init__(self, *args, **kwargs):
        self.progress_threads = []
        super().__init__(*args, **kwargs)

    @pyqtSlot(str)
    def _login_progress(self, message):
        self.progress_threads.append(threading.get_ident())
        super()._login_progress(message)


class LoginGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        traditional_chinese_system(self)
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name)
        self.window = ObservedWindow(show_notice=False, base_dir=self.root)

    def wait_until(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), "Qt login operation did not finish in time")

    def tearDown(self):
        self.window.close()
        self.wait_until(lambda: self.window._login_thread is None)
        self.app.processEvents()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_three_explicit_actions_and_persistent_close_then_save_instructions(self):
        self.assertEqual(self.window.cookie_btn.text(), "開啟瀏覽器登入")
        self.assertEqual(self.window.cookie_save_btn.text(), "儲存登入狀態")
        self.assertEqual(self.window.cookie_import_btn.text(), "匯入 Cookie 檔")
        self.assertIn("登入完成後，請關閉剛才的專用瀏覽器視窗，再回來按儲存", self.window.login_help_label.text())
        self.assertIn("專用瀏覽器設定檔與 X Cookie 只保留在本機 data", self.window.login_help_label.text())
        self.window.toggle_theme()
        self.assertIn("先關閉專用登入視窗", self.window.cookie_save_btn.toolTip())
        self.assertTrue(self.window.cookie_save_btn.isEnabled())
        self.assertTrue(self.window.cookie_import_btn.isEnabled())
        self.assertEqual(self.window.human_behavior_checkbox.text(), "下載間隨機休息（假裝自己不是機器人）")

    def test_open_save_share_one_worker_and_progress_returns_to_gui_thread(self):
        calls = []
        gui_thread = threading.get_ident()

        class Browser:
            def __init__(browser, profile, on_progress=None):
                browser.profile = profile
                browser.progress = on_progress
                calls.append(("create", threading.get_ident(), profile))

            def open(browser):
                calls.append(("open", threading.get_ident()))
                browser.progress("請在專用瀏覽器完成登入。")

            def save_cookies(browser, destination):
                calls.append(("save", threading.get_ident()))
                browser.progress("正在讀取 X 登入狀態。")
                destination.write_text("# fixture cookie file\n", encoding="utf-8")
                return 2

            def close(browser):
                calls.append(("close", threading.get_ident()))

        with patch.object(main, "LoginBrowser", Browser), patch.object(main.QFileDialog, "getOpenFileName") as chooser:
            self.window.cookie_btn.click()
            self.wait_until(lambda: self.window._login_browser_open and not self.window._login_busy)
            self.assertIn("先關窗再儲存", self.window.login_state_label.text())
            self.window.cookie_save_btn.click()
            self.wait_until(lambda: not self.window._login_busy)
            self.assertFalse(self.window._login_browser_open)
            self.assertEqual(self.window.login_state_label.text(), "已儲存登入資料（本機）")
            self.assertNotIn("已登入", self.window.login_state_label.text())
            self.assertIn("正在讀取 X 登入狀態。", self.window.status_box.toPlainText())
            self.window.close()
            self.wait_until(lambda: self.window._login_thread is None)
            chooser.assert_not_called()
        self.assertEqual([call[0] for call in calls], ["create", "open", "save", "close"])
        self.assertEqual(len({call[1] for call in calls}), 1)
        self.assertNotEqual(calls[0][1], gui_thread)
        self.assertEqual(calls[0][2], self.root / "data" / "browser-profile")
        self.assertTrue(self.window.progress_threads)
        self.assertEqual(set(self.window.progress_threads), {gui_thread})

    def test_existing_profile_can_save_after_app_restart_without_opening_or_importing(self):
        profile = self.root / "data" / "browser-profile"
        profile.mkdir(parents=True)
        (profile / "Local State").write_text("{}", encoding="utf-8")
        calls = []

        class Browser:
            def __init__(browser, path, on_progress=None):
                calls.append(("profile", path))

            def open(browser):
                raise AssertionError("Saving a persisted profile must not open an interactive login window")

            def save_cookies(browser, destination):
                calls.append(("save", destination))
                destination.write_text("# fixture cookie file\n", encoding="utf-8")
                return 2

            def close(browser):
                pass

        with patch.object(main, "LoginBrowser", Browser), patch.object(main.QFileDialog, "getOpenFileName") as chooser:
            self.assertFalse(self.window._login_browser_open)
            self.window.save_cookies()
            self.wait_until(lambda: not self.window._login_busy)
            self.assertEqual(calls, [("profile", profile), ("save", self.window.cookie_file)])
            chooser.assert_not_called()

    def test_save_failure_keeps_retry_available_and_never_opens_an_import_dialog(self):
        attempts = []

        class Browser:
            def __init__(browser, profile, on_progress=None):
                pass

            def save_cookies(browser, destination):
                attempts.append("save")
                if len(attempts) == 1:
                    raise RuntimeError("專用瀏覽器仍開著，請先關閉後重試。")
                destination.write_text("# fixture cookie file\n", encoding="utf-8")
                return 2

            def close(browser):
                pass

        with patch.object(main, "LoginBrowser", Browser), patch.object(main.QFileDialog, "getOpenFileName") as chooser, patch.object(main, "import_cookies") as importer:
            self.window.save_cookies()
            self.wait_until(lambda: not self.window._login_busy)
            self.assertIn("儲存未完成", self.window.login_state_label.text())
            self.assertTrue(self.window.cookie_save_btn.isEnabled())
            self.assertTrue(self.window.cookie_import_btn.isEnabled())
            chooser.assert_not_called()
            importer.assert_not_called()
            self.window.save_cookies()
            self.wait_until(lambda: not self.window._login_busy)
            self.assertEqual(attempts, ["save", "save"])
            self.assertTrue(self.window.cookie_file.is_file())

    def test_open_failure_returns_controls_without_choosing_a_cookie_file(self):
        with patch.object(main, "LoginBrowser") as browser, patch.object(main.QFileDialog, "getOpenFileName") as chooser:
            browser.return_value.open.side_effect = RuntimeError("找不到 Chrome 或 Edge。")
            self.window.open_browser()
            self.wait_until(lambda: not self.window._login_busy)
            self.assertFalse(self.window._login_browser_open)
            self.assertTrue(self.window.cookie_btn.isEnabled())
            self.assertTrue(self.window.cookie_save_btn.isEnabled())
            self.assertTrue(self.window.cookie_import_btn.isEnabled())
            self.assertIn("登入視窗未能開啟", self.window.login_state_label.text())
            chooser.assert_not_called()

    def test_direct_import_is_the_only_action_that_opens_the_file_chooser(self):
        source = self.root / "import.txt"
        source.write_text("# fixture\n", encoding="utf-8")

        def import_file(incoming, destination):
            self.assertEqual(incoming, source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("# imported fixture\n", encoding="utf-8")
            return 2

        with patch.object(main.QFileDialog, "getOpenFileName", return_value=(str(source), "Cookie 檔案 (*.txt *.json)")) as chooser, patch.object(main, "import_cookies", side_effect=import_file) as importer, patch.object(main, "LoginBrowser") as browser:
            self.window.cookie_import_btn.click()
            chooser.assert_called_once()
            importer.assert_called_once_with(source, self.window.cookie_file)
            browser.assert_not_called()
            self.assertIn("已匯入登入資料（本機）", self.window.login_state_label.text())
            self.assertIsNone(self.window._login_thread)

    def test_busy_save_disables_all_login_actions_and_close_waits_without_author_dialog(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()

        class Browser:
            def __init__(browser, profile, on_progress=None):
                pass

            def save_cookies(browser, destination):
                entered.set()
                release.wait(2)
                destination.write_text("# fixture cookie file\n", encoding="utf-8")
                return 2

            def close(browser):
                closed.set()

        self.window._show_notice = True  # Old close behavior used this flag.
        self.window.show()
        with patch.object(main, "LoginBrowser", Browser), patch.object(main.QFileDialog, "getOpenFileName") as chooser, patch.object(self.window, "show_author_info") as author:
            try:
                self.window.save_cookies()
                self.wait_until(entered.is_set)
                for button in (self.window.cookie_btn, self.window.cookie_save_btn, self.window.cookie_import_btn):
                    self.assertFalse(button.isEnabled())
                self.window.import_cookie_file()
                chooser.assert_not_called()
                before = time.monotonic()
                self.assertFalse(self.window.close())
                self.assertLess(time.monotonic() - before, 0.3)
                self.assertTrue(self.window._close_pending)
                self.assertTrue(self.window.isVisible())
            finally:
                release.set()
            self.wait_until(lambda: self.window._login_thread is None and not self.window.isVisible())
            self.assertTrue(closed.is_set())
            author.assert_not_called()

    def test_regular_close_does_not_show_author_and_header_button_still_opens_it(self):
        with patch.object(main, "AuthorInfoDialog") as author_dialog:
            self.window.about_btn.click()
            author_dialog.assert_called_once_with(self.window, theme=self.window.theme_mode)
            author_dialog.return_value.exec.assert_called_once()
            author_dialog.reset_mock()
            self.window._show_notice = True
            self.assertTrue(self.window.close())
            author_dialog.assert_not_called()

    def test_failed_cleanup_preserves_worker_and_second_close_can_finish(self):
        attempts = []

        class Browser:
            def __init__(browser, profile, on_progress=None):
                pass

            def open(browser):
                pass

            def close(browser):
                attempts.append("close")
                if len(attempts) == 1:
                    raise RuntimeError("背景擷取視窗尚未結束。")

        self.window.show()
        with patch.object(main, "LoginBrowser", Browser), patch.object(self.window, "show_author_info") as author:
            self.window.open_browser()
            self.wait_until(lambda: not self.window._login_busy)
            worker = self.window._login_worker
            browser = worker.browser
            self.assertFalse(self.window.close())
            self.wait_until(lambda: not self.window._close_pending)
            self.assertIs(self.window._login_worker, worker)
            self.assertIs(worker.browser, browser)
            self.assertTrue(self.window._login_thread.isRunning())
            self.assertFalse(self.window._login_shutdown_requested)
            self.assertTrue(self.window.isVisible())
            self.assertIn("尚未清理完成", self.window.status_box.toPlainText())
            QTest.qWait(40)
            self.assertEqual(attempts, ["close"])  # No automatic retry loop.
            self.assertFalse(self.window.close())
            self.wait_until(lambda: self.window._login_thread is None and not self.window.isVisible())
            self.assertEqual(attempts, ["close", "close"])
            author.assert_not_called()

    def test_console_save_existing_uses_same_profile_without_interactive_open(self):
        profile = self.root / "data" / "browser-profile"
        profile.mkdir(parents=True)
        with patch.object(console_login, "__file__", str(self.root / "tools" / "save_cookies.py")), patch.object(sys, "argv", ["save_cookies.py", "--save-existing"]), patch.object(console_login, "LoginBrowser") as browser, patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            browser.return_value.save_cookies.return_value = 2
            self.assertEqual(console_login.main(), 0)
            browser.assert_called_once_with(profile, on_progress=print)
            browser.return_value.open.assert_not_called()
            browser.return_value.save_cookies.assert_called_once_with(self.root / "data" / "cookies.txt")
            browser.return_value.close.assert_called_once()
            prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
