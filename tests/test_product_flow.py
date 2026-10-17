"""Public-facing defaults, optional guidance and opt-in website links."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
import main


class ProductFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.close()
            self.app.processEvents()
            self.window.deleteLater()
            self.app.processEvents()
        self.temporary.cleanup()

    def test_fresh_tag_is_blank_and_saved_personal_choice_survives(self):
        self.window = main.TwitterDownloader(show_notice=False, base_dir=self.root)
        self.assertEqual(self.window.hashtag_input.text(), '')
        self.window.hashtag_input.setText('#my_chosen_art')
        self.window._save_settings()
        self.window.close()
        self.window = main.TwitterDownloader(show_notice=False, base_dir=self.root)
        self.assertEqual(self.window.hashtag_input.text(), '#my_chosen_art')

    def test_first_run_guide_is_modeless_and_remembered(self):
        with patch.object(main.TwitterDownloader, '_auto_check_driver'), patch.object(main.TwitterDownloader, 'show_user_notice') as notice:
            self.window = main.TwitterDownloader(show_notice=True, base_dir=self.root)
            self.window.show()
            QTest.qWait(30)
            self.assertIsNotNone(self.window._usage_guide)
            self.assertTrue(self.window._usage_guide.isVisible())
            self.assertFalse(self.window._usage_guide.isModal())
            notice.assert_not_called()
            self.assertTrue(json.loads(self.window.settings_file.read_text(encoding='utf-8'))['usage_guide_seen'])
            self.window.close()
            self.window = main.TwitterDownloader(show_notice=True, base_dir=self.root)
            self.window.show()
            QTest.qWait(30)
            self.assertIsNone(self.window._usage_guide)
            self.window.help_btn.click()
            self.assertTrue(self.window._usage_guide.isVisible())

    def test_open_guide_tracks_theme_and_closes_with_main_window(self):
        self.window = main.TwitterDownloader(show_notice=False, base_dir=self.root)
        self.window.show_usage_guide()
        guide = self.window._usage_guide
        self.window.toggle_theme()
        self.assertEqual(guide.theme_mode, 'dark')
        self.window.close()
        self.assertFalse(guide.isVisible())

    def test_promotion_opens_only_on_explicit_activation(self):
        with patch('ui_design.QDesktopServices.openUrl', return_value=True) as open_url:
            self.window = main.TwitterDownloader(show_notice=False, base_dir=self.root)
            open_url.assert_not_called()
            self.assertIn('試試超讚的歌回直播軟體', self.window.promotion_link.text())
            self.window.promotion_link.linkActivated.emit('https://noonisawesome.dev/')
            self.assertEqual(open_url.call_args.args[0].toString(), 'https://noonisawesome.dev/')


if __name__ == '__main__':
    unittest.main()
