"""Offline checks for the guide's workflow, native keys and readable themes."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PyQt6.QtCore import QRect, Qt, QTimer
from PyQt6.QtGui import QDesktopServices, QFontDatabase, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel

from help_dialog import UsageGuideDialog
from ui_theme import theme_colors


class UsageGuideDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.name == "nt":
            # Qt's offscreen backend does not enumerate Windows system fonts.
            for filename in ("msjh.ttc", "msjhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
                path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))

    def setUp(self):
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

    def make_dialog(self, theme="light", *, show=True):
        dialog = UsageGuideDialog(theme=theme)
        self.dialogs.append(dialog)
        if show:
            dialog.show()
            self.app.setActiveWindow(dialog)
            self.app.processEvents()
        return dialog

    def test_guide_explains_shared_or_tag_gate_and_auto_vs_optional_manual_authors(self):
        dialog = self.make_dialog()
        first = dialog.step_bodies[0].text()
        self.assertIn("請自行填入", first)
        self.assertIn("任一", first)
        self.assertIn("OR", first)
        self.assertIn("搜尋、作者補抓與貼文網址", first)
        authors = dialog.step_titles[2].text() + dialog.step_bodies[2].text()
        self.assertIn("留空", authors)
        self.assertIn("自動", authors)
        self.assertIn("歷史紀錄", authors)
        self.assertIn("手動", authors)
        self.assertIn("@帳號", authors)
        self.assertIn("/media", authors)
        self.assertIn("不用數字 ID", authors)
        self.assertIn("任一完整目標標籤", authors)
        text = "\n".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertNotIn("noonle", text.lower())
        self.assertNotIn("noonvtuber", text.lower())
        self.assertEqual(dialog.findChildren(QCheckBox), [])

    def test_login_sequence_requires_closing_only_the_dedicated_window_before_save(self):
        dialog = self.make_dialog()
        sequence = dialog.login_sequence_label.text()
        self.assertLess(sequence.index("完成登入"), sequence.index("關閉該專用視窗"))
        self.assertLess(sequence.index("關閉該專用視窗"), sequence.index("回 App"))
        self.assertIn("儲存登入狀態", sequence)
        self.assertIn("Google", dialog.step_bodies[1].text())
        self.assertIn("一般瀏覽器", dialog.step_bodies[1].text())
        self.assertIn("保留日常瀏覽器", dialog.login_note_label.text())
        self.assertIn("重新開啟", dialog.login_note_label.text())
        self.assertIn("再由 X 確認", dialog.login_note_label.text())
        self.assertIn("專用瀏覽器設定檔與 X Cookie", dialog.privacy_label.text())
        self.assertIn("請勿上傳或分享", dialog.privacy_label.text())
        self.assertIn("向 X 傳送請求", dialog.privacy_label.text())
        self.assertNotIn("絕對安全", dialog.privacy_label.text())
        self.assertNotIn("Google 已驗證", dialog.privacy_label.text())

    def test_directory_stop_retained_history_and_advanced_limits_are_explained(self):
        dialog = self.make_dialog()
        text = dialog.step_bodies[3].text()
        for required in ("儲存目錄", "開始收集", "停止", "紀錄會保留", "台灣時間", "貼文數", "包含已下載者", "無法保證收齊"):
            self.assertIn(required, text)
        self.assertIn("著作權屬原作者", dialog.copyright_label.text())

    def test_themes_recolor_native_surfaces_text_and_primary_close_on_the_same_dialog(self):
        dialog = self.make_dialog()
        sequence = dialog.login_sequence_label.text()
        for theme in ("light", "dark", "light"):
            with self.subTest(theme=theme):
                dialog.set_theme(theme)
                self.app.processEvents()
                c = theme_colors(theme)
                for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                    self.assertEqual(dialog.palette().color(group, QPalette.ColorRole.Window).name().upper(), c["window"].upper())
                    self.assertEqual(dialog.step_bodies[0].palette().color(group, QPalette.ColorRole.WindowText).name().upper(), c["ink"].upper())
                    self.assertEqual(dialog.login_sequence_label.palette().color(group, QPalette.ColorRole.WindowText).name().upper(), c["accent_soft_ink"].upper())
                    self.assertEqual(dialog.close_button.palette().color(group, QPalette.ColorRole.ButtonText).name().upper(), c["accent_ink"].upper())
                self.assertEqual(dialog.copyright_label.palette().color(QPalette.ColorRole.WindowText).name().upper(), c["muted"].upper())
                self.assertEqual(dialog.login_sequence_label.text(), sequence)
                self.assertFalse(dialog.guide_icon.pixmap().isNull())
        dialog.apply_theme("invalid")
        self.assertEqual(dialog.theme_mode, "light")

    def test_small_screen_scrolls_complete_wrapped_plain_text_and_keeps_close_visible(self):
        screen = MagicMock()
        screen.availableGeometry.return_value = QRect(0, 0, 1020, 700)
        with patch.object(QApplication, "primaryScreen", return_value=screen):
            dialog = self.make_dialog()
        self.assertLessEqual(dialog.frameGeometry().height(), 700)
        self.assertLessEqual(dialog.width(), 760)
        dialog.resize(600, 440)
        self.app.processEvents()
        self.assertGreater(dialog.scroll_area.verticalScrollBar().maximum(), 0)
        self.assertEqual(dialog.scroll_area.horizontalScrollBar().maximum(), 0)
        center = dialog.close_button.mapTo(dialog, dialog.close_button.rect().center())
        self.assertTrue(dialog.rect().contains(center))
        for label in [*dialog.step_titles, *dialog.step_bodies, dialog.login_sequence_label,
                      dialog.login_note_label, dialog.privacy_label, dialog.copyright_label]:
            self.assertTrue(label.wordWrap())
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
            self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        dialog.scroll_area.setFocus()
        before = dialog.scroll_area.verticalScrollBar().value()
        QTest.keyClick(dialog.scroll_area, Qt.Key.Key_PageDown)
        self.app.processEvents()
        self.assertGreater(dialog.scroll_area.verticalScrollBar().value(), before)

    def test_nonmodal_first_run_and_modal_help_both_close_with_native_keyboard(self):
        dialog = self.make_dialog()
        self.assertFalse(dialog.isModal())
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertFalse(dialog.isVisible())
        modal = self.make_dialog(show=False)
        QTimer.singleShot(0, lambda: QTest.keyClick(modal, Qt.Key.Key_Escape))
        self.assertEqual(modal.exec(), QDialog.DialogCode.Rejected)
        self.assertFalse(modal.isVisible())
        accepted = self.make_dialog()
        accepted.close_button.setFocus()
        QTest.keyClick(accepted.close_button, Qt.Key.Key_Return)
        self.assertEqual(accepted.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(accepted.isVisible())

    def test_opening_and_closing_guide_never_starts_browser_network_or_external_link(self):
        with patch("subprocess.Popen") as process, patch("urllib.request.urlopen") as network, \
                patch.object(QDesktopServices, "openUrl") as opened:
            dialog = self.make_dialog()
            dialog.close_button.click()
            self.app.processEvents()
        process.assert_not_called()
        network.assert_not_called()
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
