"""Offline checks for management actions and the GUI/worker boundaries."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
from PyQt6.QtCore import QDate, QEvent, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
import main
from locale_fixture import traditional_chinese_system


class MainToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        traditional_chinese_system(self)
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name)
        self.window = main.TwitterDownloader(show_notice=False, base_dir=self.root)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), 'Management worker did not finish')

    def tearDown(self):
        self.window.close()
        self.wait_until(lambda: self.window._login_thread is None and self.window._update_thread is None)
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.directory.cleanup()

    def test_windowing_and_resume_preferences_survive_restart(self):
        self.window.hashtag_input.setText('#sampleart')
        self.window.limit_date_checkbox.setChecked(True)
        self.window.limit_date_input.setDate(QDate(2026, 1, 1))
        self.assertTrue(self.window.date_window_search_checkbox.isEnabled())
        self.window.date_window_search_checkbox.setChecked(False)
        self.window.resume_checkbox.setChecked(False)
        options = self.window.snapshot_options()
        self.assertFalse(options.date_window_search)
        self.assertFalse(options.resume)
        self.window._save_settings()
        other = main.TwitterDownloader(show_notice=False, base_dir=self.root)
        self.assertFalse(other.resume_checkbox.isChecked())
        self.assertFalse(other.date_window_search_checkbox.isChecked())
        other.close()
        other.deleteLater()

    def test_open_destination_uses_a_local_url_and_creates_only_the_selected_folder(self):
        # 特殊字元仍是資料夾名稱，不會變成 shell 指令。
        # Special characters remain part of a local folder name, never a shell command.
        folder = self.root / 'art work; sample'
        self.window.download_path = str(folder)
        self.window.is_downloading = True
        with patch.object(main.QDesktopServices, 'openUrl', return_value=True) as opened:
            self.window.open_download_folder_btn.click()
        self.assertTrue(folder.is_dir())
        self.assertEqual(opened.call_count, 1)
        url = opened.call_args.args[0]
        self.assertTrue(url.isLocalFile())
        self.assertEqual(Path(url.toLocalFile()), folder.resolve())
        self.window.is_downloading = False

    def test_open_destination_failure_preserves_an_existing_file(self):
        occupied = self.root / 'existing-file'
        occupied.write_bytes(b'keep original')
        self.window.download_path = str(occupied)
        with patch.object(main.QDesktopServices, 'openUrl') as opened:
            self.window.open_download_folder()
        opened.assert_not_called()
        self.assertEqual(occupied.read_bytes(), b'keep original')
        self.assertIn('無法開啟下載資料夾', self.window.status_box.toPlainText())

    def test_cancel_clear_login_preserves_local_files_and_starts_no_worker(self):
        self.window.data_dir.mkdir(parents=True)
        self.window.cookie_file.write_text('synthetic test value', encoding='utf-8')
        with patch.object(main, 'confirm_clear_login', return_value=False), patch.object(main, 'clear_login_data') as clear:
            self.window.clear_saved_login()
        clear.assert_not_called()
        self.assertIsNone(self.window._login_thread)
        self.assertEqual(self.window.cookie_file.read_text(encoding='utf-8'), 'synthetic test value')

    def test_confirmed_clear_runs_off_gui_thread_and_reports_clear_instead_of_save(self):
        calls = []
        def clear(profile, cookies):
            calls.append((profile, cookies, threading.get_ident()))
            return {'profile_deleted': False, 'cookies_deleted': False}
        with patch.object(main, 'confirm_clear_login', return_value=True), patch.object(main, 'clear_login_data', side_effect=clear):
            self.window.clear_saved_login()
            self.wait_until(lambda: not self.window._login_busy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], (self.window.data_dir / 'browser-profile', self.window.cookie_file))
        self.assertNotEqual(calls[0][2], threading.get_ident())
        self.assertIn('本機登入資料已清除', self.window.login_state_label.text())
        self.assertNotIn('已儲存 0 個', self.window.status_box.toPlainText())

    def test_selected_retry_ignores_blank_form_and_cannot_start_other_sources(self):
        self.window.hashtag_input.clear()
        with patch.object(self.window, '_start_download_options') as start:
            self.window._retry_history_items(['one', 'one', 'two'])
        options = start.call_args.args[0]
        self.assertEqual(options.retry_media_ids, ('one', 'two'))
        self.assertEqual(options.hashtag, '')
        self.assertEqual(options.hashtags, ())
        self.assertFalse(options.search_enabled)
        self.assertFalse(options.discover_authors)
        self.assertFalse(options.resume)
        self.assertEqual(options.authors, ())
        self.assertEqual(options.tweet_urls, ())
        with patch.object(self.window, '_start_download_options') as start:
            self.window._retry_history_items({'one': 'not a selection'})
        start.assert_not_called()

    def test_selected_redownload_uses_history_scope_and_preserves_the_current_form(self):
        self.window.hashtag_input.clear()
        self.window.authors_input.setPlainText('@unrelated')
        self.window.tweet_urls_input.setPlainText('https://x.com/unrelated/status/999')
        for mode in ('copy', 'overwrite'):
            with self.subTest(mode=mode), patch.object(self.window, '_start_download_options') as start:
                self.window._redownload_history_items(['one', 'one', 'two'], mode)
                options = start.call_args.args[0]
                self.assertEqual(options.redownload_media_ids, ('one', 'two'))
                self.assertEqual(options.redownload_mode, mode)
                self.assertEqual(options.retry_media_ids, ())
                self.assertEqual(options.hashtags, ())
                self.assertFalse(options.search_enabled)
                self.assertFalse(options.discover_authors)
                self.assertFalse(options.retry_failed)
                self.assertFalse(options.resume)
                self.assertEqual(options.authors, ())
                self.assertEqual(options.tweet_urls, ())
        self.assertEqual(self.window.authors_input.toPlainText(), '@unrelated')
        self.assertEqual(self.window.tweet_urls_input.toPlainText(), 'https://x.com/unrelated/status/999')

    def test_redownload_rejects_unknown_mode_invalid_selection_and_active_work(self):
        with patch.object(self.window, '_start_download_options') as start:
            for identifiers, mode in ((['one'], 'replace-anything'), ([], 'copy'),
                                      ({'one': True}, 'copy'), ([''], 'copy'), ([1], 'copy'), (['one'], [])):
                self.window._redownload_history_items(identifiers, mode)
            self.window.is_downloading = True
            self.window._redownload_history_items(['one'], 'copy')
            self.window.is_downloading = False
        start.assert_not_called()

    def test_redownload_summary_names_saved_destinations_instead_of_the_search_form(self):
        self.window.hashtag_input.clear()
        self.window.is_downloading = True
        self.window._active_redownload_count = 2
        self.window._active_redownload_mode = 'copy'
        self.window._refresh_summary()
        self.assertIn('重新下載', self.window.tag_summary_label.text())
        self.assertIn('2', self.window.source_summary_label.text())
        self.assertIn('另存副本', self.window.source_summary_label.text())
        self.assertEqual(self.window.path_label.text(), '使用各項目原儲存資料夾')
        self.window.is_downloading = False

    def test_history_redownload_signal_starts_the_selected_controller_action(self):
        self.window.show_download_history()
        with patch.object(self.window, '_start_download_options') as start:
            self.window._history_dialog.redownloadRequested.emit(['one'], 'copy')
        self.assertEqual(start.call_args.args[0].redownload_media_ids, ('one',))
        self.assertEqual(start.call_args.args[0].redownload_mode, 'copy')

    def test_unfinished_sources_cannot_look_like_full_success(self):
        result = SimpleNamespace(stopped=False, downloaded=1, skipped=0, failed=0, posts=1,
            filtered=0, errors=[], resumed=True, incomplete_sources=[{'state': 'queued'}])
        self.window._download_completed(result)
        text = self.window.status_box.toPlainText()
        self.assertIn('部分完成', self.window.run_state_label.text())
        self.assertIn('仍有 1 個來源未完成', text)
        self.assertNotIn('本輪處理完成', text)

    def test_close_cancels_update_and_waits_for_the_thread(self):
        instances = []
        class Worker(QObject):
            completed = pyqtSignal(object)
            failed = pyqtSignal(str)
            finished = pyqtSignal()
            def __init__(self):
                super().__init__()
                self.started, self.cancelled = threading.Event(), threading.Event()
                instances.append(self)
            def cancel(self):
                self.cancelled.set()
            @pyqtSlot()
            def run(self):
                self.started.set()
                self.cancelled.wait(3)
                self.finished.emit()
        with patch.object(main, 'UpdateCheckWorker', Worker):
            self.window.check_updates()
            self.wait_until(lambda: instances and instances[0].started.is_set())
            self.assertFalse(self.window.update_action.isEnabled())
            self.window.close()
            self.wait_until(lambda: self.window._update_thread is None)
        self.assertTrue(instances[0].cancelled.is_set())
        self.wait_until(lambda: self.window._allow_close)
