"""Exercise Start -> Stop -> Start through Qt and a real, synthetic helper."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))

from PyQt6.QtCore import QEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import main
from downloader import DownloadEngine
from gallery_client import GallerySource
from locale_fixture import traditional_chinese_system
from test_downloader import Session


class RestartTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait_until(self, condition, seconds=8):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition(), 'Owned background work did not finish')

    def test_stop_then_start_uses_fresh_event_and_helper_without_runtime_error(self):
        traditional_chinese_system(self)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            helper = root / 'fixture_helper.py'
            marker = root / 'first-helper-started'
            helper.write_text('''import json, pathlib, time
marker = pathlib.Path(__file__).with_name('first-helper-started')
first = not marker.exists()
marker.write_text('started', encoding='utf-8')
post = 91001 if first else 91002
metadata = {'tweet_id': post, 'num': 1, 'extension': 'png', 'type': 'photo',
    'author': {'name': 'fixture_artist', 'nick': 'Fixture artist'},
    'hashtags': ['fixture_art'], 'date': '2026-10-18T00:00:00+00:00'}
print(json.dumps({'type':'metadata', 'url':f'https://pbs.twimg.com/media/{post}.png',
                  'metadata':metadata}), flush=True)
if first:
    time.sleep(30)
print(json.dumps({'type':'done', 'ok':True, 'count':1}), flush=True)
''', encoding='utf-8')
            cookie = root / 'fixture-cookie.txt'
            cookie.write_text('# Synthetic fixture, no account credentials\n', encoding='utf-8')
            events, results = [], []
            first_saved = threading.Event()

            class FixtureEngine(DownloadEngine):
                def __init__(engine, options, log, stop_event, progress=None):
                    events.append(stop_event)
                    def notify(event):
                        if event.get('state') == 'downloaded':
                            first_saved.set()
                        if progress is not None:
                            progress(event)
                    super().__init__(options, log=log, stop_event=stop_event,
                        source=GallerySource(cookie, log, stop_event),
                        session=Session(), progress=notify)

                def run(engine):
                    outcome = super().run()
                    results.append(outcome)
                    return outcome

            window = main.TwitterDownloader(show_notice=False, base_dir=root)
            window.hashtag_input.setText('#fixture_art')
            window.download_path = str(root / 'downloads')
            window.author_media_checkbox.setChecked(False)
            window.retry_failed_checkbox.setChecked(False)
            window.reply_media_checkbox.setChecked(False)
            window.human_behavior_checkbox.setChecked(False)
            try:
                with patch.object(main, 'DownloadEngine', FixtureEngine), \
                     patch.object(GallerySource, '_command', return_value=[sys.executable, str(helper)]):
                    window.start_download_thread()
                    self.wait_until(first_saved.is_set)
                    first_worker = window._download_worker
                    self.assertTrue(marker.is_file())
                    window.stop_download()
                    self.wait_until(lambda: window._download_thread is None)
                    self.assertTrue(results[0].stopped)
                    self.assertTrue(events[0].is_set())
                    window.start_download_thread()
                    self.assertIsNot(window._download_worker, first_worker)
                    self.wait_until(lambda: window._download_thread is None)
                    self.assertEqual(len(events), 2)
                    self.assertIsNot(events[0], events[1])
                    self.assertFalse(events[1].is_set())
                    self.assertFalse(results[1].stopped)
                    self.assertEqual(results[1].downloaded, 1)
                    self.assertEqual(results[1].errors, [])
                    self.assertNotIn('RuntimeError', window.status_box.toPlainText())
                    self.assertNotIn('❌ 下載未完成', window.status_box.toPlainText())
                    self.assertNotIn('同作者留言補抓未完成', window.status_box.toPlainText())
                    self.assertTrue(window.download_btn.isEnabled())
                    self.assertFalse(window.is_downloading)
                    files = sorted(path.name for path in (root / 'downloads').rglob('*.png'))
                    self.assertEqual(files, ['91001_1.png', '91002_1.png'])
            finally:
                window.stop_download()
                self.wait_until(lambda: window._download_thread is None)
                window.close()
                window.deleteLater()
                self.app.processEvents()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
